import json
import os
import tempfile
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core import management
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connections

from ccAPPSdb.common.utils import get_databases


DEFAULT_CONSTRAINT = "capa,mfg_lt,po_lt"
DEFAULT_PLANTYPE = 1
DEFAULT_ENV = "supply"
FORWARD_SCHEDULE_MODE = "forward"


class PlanningServiceError(Exception):
    pass


def _json_default(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    return str(value)


def _clean_all(database):
    management.call_command("empty", database=database, all=True, verbosity=0)


def _write_payload_file(payload):
    folder = Path(settings.CCAPPS_LOGDIR) / "data" / "ccpl"
    folder.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix="ccpl_plan_", suffix=".json", dir=str(folder), text=True
    )
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, default=_json_default)
    return name


def _collect_plan_result(database):
    with connections[database].cursor() as cursor:
        cursor.execute(
            """
            select reference, type, operation_id, owner_id, demand_id, item_id,
              location_id, quantity, startdate, enddate, status, criticality,
              delay, source, batch, remark
            from operationplan
            order by startdate, enddate, reference
            """
        )
        operationplans = [
            {
                "reference": row[0],
                "type": row[1],
                "operation": row[2],
                "owner": row[3],
                "demand": row[4],
                "item": row[5],
                "location": row[6],
                "quantity": row[7],
                "start": row[8],
                "end": row[9],
                "status": row[10],
                "criticality": row[11],
                "delay": row[12],
                "source": row[13],
                "batch": row[14],
                "remark": row[15],
            }
            for row in cursor.fetchall()
        ]

        cursor.execute(
            """
            select operationplan_id, resource_id, quantity, setup, status
            from operationplanresource
            order by operationplan_id, resource_id
            """
        )
        resources = [
            {
                "operationplan": row[0],
                "resource": row[1],
                "quantity": row[2],
                "setup": row[3],
                "status": row[4],
            }
            for row in cursor.fetchall()
        ]

        cursor.execute(
            """
            select operationplan_id, item_id, location_id, flowdate, quantity,
              onhand, minimum, status
            from operationplanmaterial
            order by operationplan_id, flowdate, item_id, location_id
            """
        )
        materials = [
            {
                "operationplan": row[0],
                "item": row[1],
                "location": row[2],
                "flowdate": row[3],
                "quantity": row[4],
                "onhand": row[5],
                "minimum": row[6],
                "status": row[7],
            }
            for row in cursor.fetchall()
        ]

        cursor.execute(
            """
            select id, entity, owner, name, description, startdate, enddate
            from out_problem
            order by id
            """
        )
        problems = [
            {
                "id": row[0],
                "entity": row[1],
                "owner": row[2],
                "name": row[3],
                "description": row[4],
                "start": row[5],
                "end": row[6],
            }
            for row in cursor.fetchall()
        ]

        cursor.execute(
            """
            select id, demand, entity, owner, name, description, startdate,
              enddate, forecast, item
            from out_constraint
            order by id
            """
        )
        constraints = [
            {
                "id": row[0],
                "demand": row[1],
                "entity": row[2],
                "owner": row[3],
                "name": row[4],
                "description": row[5],
                "start": row[6],
                "end": row[7],
                "forecast": row[8],
                "item": row[9],
            }
            for row in cursor.fetchall()
        ]

    return {
        "summary": {
            "operationplans": len(operationplans),
            "operationplanresources": len(resources),
            "operationplanmaterials": len(materials),
            "problems": len(problems),
            "constraints": len(constraints),
        },
        "operationplans": operationplans,
        "resources": resources,
        "materials": materials,
        "problems": problems,
        "constraints": constraints,
    }


def _validate_database(database, user=None):
    if database not in get_databases():
        raise ValidationError("Unknown scenario '%s'" % database)
    if user and not user.is_anonymous:
        allowed = set(user.databases or [])
        if allowed and database not in allowed:
            raise PermissionDenied("No access to scenario '%s'" % database)


def run_ccpl_plan(payload, database, user=None):
    _validate_database(database, user)
    if not isinstance(payload, dict):
        raise ValidationError("Request body must be a JSON object")

    data = payload.get("data") or payload
    if not isinstance(data, dict):
        raise ValidationError("'data' must be a JSON object")
    if data.get("craftProcessNodes") or data.get("workSchedules"):
        raise ValidationError(
            "CCPL protocol 2.0 (craftProcessNodes/workSchedules) is no longer "
            "supported; send protocol 1.0 with 'sheets'"
        )
    is_standard_dataset = bool(data.get("items"))
    required_sections = (
        ("items", "operations", "demands")
        if is_standard_dataset
        else ("materials", "operations", "demands")
    )
    if any(not data.get(section) for section in required_sections):
        raise ValidationError(
            "Missing required sections: %s" % ", ".join(required_sections)
        )

    plan_options = payload.get("plan_options", {}) if is_standard_dataset else {}
    constraint = payload.get(
        "constraint", plan_options.get("constraint", DEFAULT_CONSTRAINT)
    )
    plantype = int(
        payload.get("plantype", plan_options.get("plantype", DEFAULT_PLANTYPE))
    )
    # CCPL is always a supply-planning request.  This is an execution-mode
    # flag, not an MRP/FAS/GA algorithm knob, so don't accept it from client
    # datasets.
    env = DEFAULT_ENV
    # CCPL requests are forward finite-capacity schedules by default.  Clients
    # can still explicitly request the MRP baseline or the GA sequencer.
    solver = str(payload.get("solver", plan_options.get("solver", "fas"))).lower()
    # ``ga_multi_obj`` is the public API name.  The C++ entry point remains
    # named ``ga`` because it already implements the NSGA-II multi-objective
    # optimizer.
    if solver == "ga_multi_obj":
        solver = "ga"
    if solver not in ("mrp", "fas", "ga", "cp"):
        raise ValidationError(
            "solver must be 'mrp', 'fas', 'ga_multi_obj' or 'cp'"
        )
    schedule_mode = str(payload.get("schedule_mode", "mrp")).lower()
    if schedule_mode not in ("mrp", FORWARD_SCHEDULE_MODE):
        raise ValidationError("schedule_mode must be 'mrp' or 'forward'")
    clean_all = payload.get("clean_all", False)
    clean_source = payload.get("clean", True)

    # Standard datasets contain their own import controls and must be written
    # intact. CCPL datasets retain the historical data-only import behavior.
    data_file = _write_payload_file(payload if is_standard_dataset else data)
    try:
        if clean_all and not is_standard_dataset:
            _clean_all(database)

        if is_standard_dataset:
            management.call_command(
                "import_standard_dataset", data_file, database=database, verbosity=0
            )
        else:
            management.call_command(
                "import_ccpl_dataset",
                data_file,
                database=database,
                clean_source=clean_source,
                verbosity=0,
            )
        if solver in ("fas", "ga", "cp"):
            # FAS, GA and CP are selected by the normal plan parameter so
            # runplan retains the same entry point and constraint parsing as MRP.
            solver_description = {
                "fas": "CCPL requested C++ forward scheduler",
                "ga": "CCPL requested C++ genetic algorithm scheduler",
                "cp": "CCPL requested CP-SAT flexible job shop",
            }[solver]
            with connections[database].cursor() as cursor:
                cursor.execute(
                    """
                    insert into common_parameter(name, value, description, lastmodified)
                    values ('plan.solver', %s, %s, now())
                    on conflict (name) do update
                    set value = excluded.value, description = excluded.description,
                        lastmodified = excluded.lastmodified
                    """,
                    [solver, solver_description],
                )
        management.call_command(
            "runplan",
            database=database,
            constraint=constraint,
            plantype=plantype,
            env=env,
            background=False,
            daemon=False,
            verbosity=0,
        )
        if schedule_mode == FORWARD_SCHEDULE_MODE:
            management.call_command(
                "forward_schedule",
                database=database,
                execute=True,
                verbosity=0,
            )
        result = _collect_plan_result(database)
        result.update(
            {
                "status": "success",
                "scenario": database,
                "request_id": payload.get("request_id"),
                "constraint": constraint,
                "plantype": plantype,
                "env": env,
                "schedule_mode": schedule_mode,
                "solver": solver,
            }
        )
        return result
    except Exception as exc:
        raise PlanningServiceError(str(exc)) from exc
    finally:
        try:
            os.remove(data_file)
        except OSError:
            pass

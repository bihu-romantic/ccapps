import json
import os
import tempfile
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
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
SUPPORTED_SOLVERS = ("mrp", "fas", "ga")


def _normalize_ccpl_plan_options(payload):
    """Normalize the public CCPL planOptions envelope before conversion.

    This is deliberately at the API boundary: all CCPL datasets then reach
    the adapter with the same validated plan-start and solver semantics.  The
    converter only translates process data into ccAPPS master data.
    """
    normalized = dict(payload)
    options = normalized.get("planOptions") or normalized.get("plan_options") or {}
    if not isinstance(options, dict):
        raise ValidationError("planOptions must be a JSON object")
    options = dict(options)

    solver = options.get("solver", "fas")
    if isinstance(solver, dict):
        solver_type = solver.get("type", solver.get("name"))
        if not solver_type:
            raise ValidationError("planOptions.solver.type is required")
    elif isinstance(solver, str):
        solver_type = solver
    else:
        raise ValidationError("planOptions.solver must be a string or JSON object")
    solver_type = solver_type.lower()
    if solver_type not in SUPPORTED_SOLVERS:
        raise ValidationError("planOptions.solver.type must be 'mrp', 'fas' or 'ga'")

    plan_start = normalized.get("planStart", options.get("currentdate"))
    if plan_start is not None:
        if not isinstance(plan_start, str):
            raise ValidationError("planStart must be an ISO date-time string")
        try:
            datetime.fromisoformat(plan_start.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("planStart must be an ISO date-time string") from exc
        normalized["planStart"] = plan_start

    constraint = options.get("constraint", DEFAULT_CONSTRAINT)
    if not isinstance(constraint, (str, int)):
        raise ValidationError("planOptions.constraint must be a constraint string or bit mask")
    try:
        plantype = int(options.get("plantype", DEFAULT_PLANTYPE))
    except (TypeError, ValueError) as exc:
        raise ValidationError("planOptions.plantype must be 1 or 2") from exc
    if plantype not in (1, 2):
        raise ValidationError("planOptions.plantype must be 1 or 2")

    # Retain the solver object (including GA's objectiveProfile) for the
    # converter, while placing defaults in the same public request shape.
    options["solver"] = solver
    options["constraint"] = constraint
    options["plantype"] = plantype
    normalized["planOptions"] = options
    normalized.pop("plan_options", None)
    return normalized


def _convert_long_ccpl_payload(payload, scenario):
    """Convert protocol 2.0 CCPL process data to the standard import format."""
    converter = Path(__file__).resolve().parents[3] / "scripts" / "build_battary_factory_standard.py"
    spec = spec_from_file_location("ccpl_standard_converter", converter)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    standard = module.build_standard(module.build_from_long_json(payload))
    # The URL/payload scenario remains authoritative for the target database.
    standard["scenario"] = scenario
    # Preserve planning controls supplied alongside the protocol 2.0 dataset.
    # Without this, a raw CCPL request can only run the converter defaults and
    # can't select the optional forward finite-capacity scheduling pass.
    for key in ("constraint", "plantype", "schedule_mode", "solver"):
        if key in payload:
            standard[key] = payload[key]
    return standard


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

    payload = _normalize_ccpl_plan_options(payload)

    data = payload.get("data") or payload
    if not isinstance(data, dict):
        raise ValidationError("'data' must be a JSON object")
    is_long_ccpl_dataset = bool(data.get("craftProcessNodes"))
    if is_long_ccpl_dataset:
        required_long_sections = (
            "craftProcessNodes",
            "workSchedules",
            "scheduleMaterials",
            "demandOrders",
        )
        if any(not data.get(section) for section in required_long_sections):
            raise ValidationError(
                "Missing required CCPL sections: %s"
                % ", ".join(required_long_sections)
            )
        # CCPL protocol 2.0 is the external API contract. Normalize it before
        # importing so the planning engine only receives standard records.
        payload = _convert_long_ccpl_payload(payload, database)
        data = payload["data"]
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
    if solver not in ("mrp", "fas", "ga"):
        raise ValidationError("solver must be 'mrp', 'fas' or 'ga_multi_obj'")
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
        if solver in ("fas", "ga"):
            # FAS and GA are selected by the normal plan parameter so runplan
            # retains the same entry point and constraint parsing as MRP.
            with connections[database].cursor() as cursor:
                cursor.execute(
                    """
                    insert into common_parameter(name, value, description, lastmodified)
                    values ('plan.solver', %s, %s, now())
                    on conflict (name) do update
                    set value = excluded.value, description = excluded.description,
                        lastmodified = excluded.lastmodified
                    """,
                    [
                        solver,
                        "CCPL requested C++ forward scheduler"
                        if solver == "fas"
                        else "CCPL requested C++ genetic algorithm scheduler",
                    ],
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

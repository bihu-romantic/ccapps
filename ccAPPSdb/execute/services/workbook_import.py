"""Workbook import service used by the CCPL HTTP API.

Mirrors the sheet-matching / parseExcelWorksheet path of
``execute.views.importWorkbook`` and the ``importworkbook`` management
command, but returns a structured JSON-friendly result instead of HTML.
"""

from __future__ import annotations

import logging
import tempfile
from datetime import datetime
from pathlib import Path

from django.contrib.auth import get_permission_codename
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DEFAULT_DB_ALIAS, transaction
from django.utils.encoding import force_str
from openpyxl import Workbook, load_workbook

from ccAPPSdb.boot import addAttributesFromDatabase
from ccAPPSdb.common.dataload import parseExcelWorksheet
from ccAPPSdb.common.middleware import _thread_locals
from ccAPPSdb.common.models import Parameter
from ccAPPSdb.common.report import (
    EXCLUDE_FROM_BULK_OPERATIONS,
    GridReport,
    matchesModelName,
)
from ccAPPSdb.common.utils import get_databases
from ccAPPSdb.execute.models import Task

logger = logging.getLogger(__name__)


class WorkbookImportError(Exception):
    """Raised for expected import failures (bad file, empty workbook…)."""


def _validate_database(database, user=None):
    if database not in get_databases():
        raise ValidationError("Unknown scenario '%s'" % database)
    if user and not user.is_anonymous:
        allowed = set(user.databases or [])
        if allowed and database not in allowed:
            raise PermissionDenied("No access to scenario '%s'" % database)


def sheets_to_workbook_file(sheets):
    """Write a CCPL ``sheets`` dict to a temporary .xlsx and return its path.

    English sheet keys (locations, demands, ...) are remapped to names that
    ``matchesModelName`` accepts for the importworkbook engine.
    """
    if not isinstance(sheets, dict) or not sheets:
        raise WorkbookImportError("'sheets' must be a non-empty JSON object")

    # Import-time aliases: API English key → workbook tab name for matching.
    sheet_aliases = {
        "locations": "locations",
        "calendars": "calendars",
        "calendarBuckets": "calendar buckets",
        "calendarbuckets": "calendar buckets",
        "customers": "customers",
        "items": "items",
        "buffers": "buffers",
        "skills": "skills",
        "resources": "resources",
        "resourceSkills": "resource skills",
        "resourceskills": "resource skills",
        "operations": "operations",
        "operationMaterials": "operation materials",
        "operationmaterials": "operation materials",
        "operationResources": "operation resources",
        "operationresources": "operation resources",
        "demands": "demand",
        "demand": "demand",
        "salesOrders": "sales orders",
        # Chinese originals still accepted
        "地点": "地点",
        "日历": "日历",
        "日历-排班表": "日历-排班表",
        "客户": "客户",
        "对象": "对象",
        "库存": "库存",
        "技能": "技能",
        "资源": "资源",
        "资源技能": "资源技能",
        "操作": "操作",
        "操作-物料": "操作-物料",
        "操作-资源": "操作-资源",
        "销售订单": "销售订单",
    }

    wb = Workbook()
    default = wb.active
    wb.remove(default)

    for sheet_name, rows in sheets.items():
        if not isinstance(sheet_name, str) or not sheet_name.strip():
            raise WorkbookImportError("Invalid sheet name: %r" % sheet_name)
        if not isinstance(rows, list):
            raise WorkbookImportError(
                "Sheet '%s' must be a JSON array of row objects" % sheet_name
            )
        tab_name = sheet_aliases.get(sheet_name, sheet_name)
        ws = wb.create_sheet(title=tab_name[:31])
        if not rows:
            continue
        headers = list(rows[0].keys())
        seen = set(headers)
        for row in rows[1:]:
            if not isinstance(row, dict):
                raise WorkbookImportError(
                    "Sheet '%s' rows must be JSON objects" % sheet_name
                )
            for key in row.keys():
                if key not in seen:
                    headers.append(key)
                    seen.add(key)
        ws.append(headers)
        for row in rows:
            if not isinstance(row, dict):
                raise WorkbookImportError(
                    "Sheet '%s' rows must be JSON objects" % sheet_name
                )
            ws.append([row.get(h) for h in headers])

    fd, tmp = tempfile.mkstemp(prefix="ccpl_sheets_", suffix=".xlsx")
    try:
        import os

        os.close(fd)
        wb.save(tmp)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    return tmp


def apply_parameters(database, parameters):
    """Upsert common_parameter rows from a JSON list of {name, value}."""
    if not parameters:
        return
    if not isinstance(parameters, list):
        raise WorkbookImportError("'parameters' must be a JSON array")
    for row in parameters:
        if not isinstance(row, dict) or not row.get("name"):
            raise WorkbookImportError(
                "Each parameter must be an object with a 'name' field"
            )
        Parameter.objects.using(database).update_or_create(
            name=row["name"],
            defaults={"value": "" if row.get("value") is None else str(row["value"])},
        )


def _save_upload_to_temp(upload):
    """Persist an uploaded file (or path) to a local .xlsx temp file."""
    if isinstance(upload, (str, Path)):
        path = Path(upload)
        if not path.is_file():
            raise WorkbookImportError("File not found: %s" % path)
        return str(path), False

    name = getattr(upload, "name", "") or "upload.xlsx"
    if name.lower().endswith(".xls") and not name.lower().endswith(".xlsx"):
        raise WorkbookImportError(
            "Files in the old .XLS excel format can't be read. "
            "Please convert them to the new .XLSX format."
        )
    content_type = getattr(upload, "content_type", None) or ""
    if content_type and content_type not in (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/octet-stream",
        "application/zip",
    ):
        # Browsers sometimes send octet-stream; still try to open as xlsx.
        if not name.lower().endswith(".xlsx"):
            raise WorkbookImportError("Unsupported file format: %s" % content_type)

    fd, tmp = tempfile.mkstemp(prefix="ccpl_import_", suffix=".xlsx")
    try:
        with open(fd, "wb") as out:
            for chunk in upload.chunks():
                out.write(chunk)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    return tmp, True


def _normalize_run_options(payload):
    """Fold plan_options / parameters into values the importer understands."""
    parameters = [dict(p) for p in (payload.get("parameters") or []) if isinstance(p, dict)]
    by_name = {p.get("name"): p for p in parameters if p.get("name")}

    def upsert(name, value):
        if value is None or value == "":
            return
        value = str(value)
        if name in by_name:
            by_name[name]["value"] = value
        else:
            row = {"name": name, "value": value}
            parameters.append(row)
            by_name[name] = row

    # Prefer explicit parameters.currentdate; fall back to planStart alias.
    plan_start = None
    if "currentdate" in by_name:
        plan_start = by_name["currentdate"].get("value")
    plan_start = plan_start or payload.get("planStart") or payload.get("plan_start")
    if plan_start:
        upsert("currentdate", plan_start)
        upsert("last_currentdate", plan_start)

    options = payload.get("plan_options") or payload.get("planOptions") or {}
    if not isinstance(options, dict):
        raise ValidationError("plan_options must be a JSON object")

    solver_cfg = options.get("solver", "mrp")
    objective_profile = None
    if isinstance(solver_cfg, dict):
        solver = solver_cfg.get("type") or solver_cfg.get("name") or "mrp"
        objective_profile = solver_cfg.get("objectiveProfile")
    else:
        solver = solver_cfg
    solver = str(solver).lower()
    if solver == "ga_multi_obj":
        solver = "ga"
    if solver not in ("mrp", "fas", "ga", "cp"):
        raise ValidationError("plan_options.solver.type must be mrp, fas, ga or cp")
    upsert("plan.solver", solver)
    if objective_profile:
        upsert("plan.objectiveProfile", objective_profile)

    plantype = options.get("plantype", 1)
    try:
        plantype = int(plantype)
    except (TypeError, ValueError) as exc:
        raise ValidationError("plan_options.plantype must be 1 or 2") from exc
    upsert("plan.plantype", plantype)

    objectives = options.get("objectives") or []
    if objectives and not isinstance(objectives, list):
        raise ValidationError("plan_options.objectives must be a JSON array")

    # run_plan defaults true when plan_options is present (protocol request).
    run_plan = payload.get("run_plan")
    if run_plan is None:
        run_plan = bool(options)

    return {
        "parameters": parameters,
        "solver": solver,
        "objective_profile": objective_profile,
        "objectives": objectives,
        "constraint": options.get("constraint", "capa,mfg_lt,po_lt"),
        "plantype": plantype,
        "env": options.get("env", "supply"),
        "run_plan": bool(run_plan),
        "clean_all": bool(payload.get("clean_all", False)),
        "mode": payload.get("mode") or "replace_source",
        "source": payload.get("source"),
        "request_id": payload.get("requestId"),
        "dataset_id": payload.get("datasetId"),
        "production_line_id": payload.get("productionLineId"),
        "version": payload.get("version", "1.0"),
    }


def import_ccpl_payload(payload, database, user=None):
    """Import a CCPL protocol JSON payload (envelope + workbook sheets).

    Envelope (protocol 1.0):
      version, requestId, datasetId, productionLineId, scenario, source,
      mode, clean_all, parameters, plan_options, sheets
    """
    if not isinstance(payload, dict):
        raise ValidationError("Request body must be a JSON object")
    sheets = payload.get("sheets")
    if not sheets:
        raise ValidationError("Missing required field 'sheets'")
    _validate_database(database, user)

    options = _normalize_run_options(payload)

    if options["clean_all"]:
        from django.core import management

        management.call_command("empty", database=database, all=True, verbosity=0)

    apply_parameters(database, options["parameters"])
    tmp = sheets_to_workbook_file(sheets)
    try:
        result = import_workbook(
            tmp,
            database=database,
            user=user,
            source_name=options["source"] or payload.get("datasetId") or "ccpl.json",
        )
    finally:
        Path(tmp).unlink(missing_ok=True)

    result.update(
        {
            "version": options["version"],
            "requestId": options["request_id"],
            "datasetId": options["dataset_id"],
            "productionLineId": options["production_line_id"],
            "source": options["source"],
            "mode": options["mode"],
            "clean_all": options["clean_all"],
            "run_plan": options["run_plan"],
            "plan_options": {
                "constraint": options["constraint"],
                "plantype": options["plantype"],
                "env": options["env"],
                "solver": {
                    "type": options["solver"],
                    **(
                        {"objectiveProfile": options["objective_profile"]}
                        if options["objective_profile"]
                        else {}
                    ),
                },
                "objectives": options["objectives"],
            },
        }
    )

    if not options["run_plan"]:
        return result

    from django.core import management

    from ccAPPSdb.execute.services.ccpl_adapter import _collect_plan_result

    management.call_command(
        "runplan",
        database=database,
        constraint=options["constraint"],
        plantype=options["plantype"],
        env=options["env"],
        background=False,
        daemon=False,
        verbosity=0,
    )
    # Same shape as long_2A_mrp_result_175.json / run_ccpl_plan.
    out = _collect_plan_result(database)
    out.update(
        {
            "status": "success",
            "scenario": database,
            "request_id": options["request_id"] or payload.get("request_id"),
            "constraint": options["constraint"],
            "plantype": options["plantype"],
            "env": options["env"],
            "schedule_mode": "mrp",
            "solver": options["solver"],
        }
    )
    return out


def import_workbook(upload, database, user=None, source_name=None):
    """Import an .xlsx workbook into ``database``.

    Parameters
    ----------
    upload:
        Django UploadedFile, filesystem path, or open file-like object with
        ``chunks()`` / readable bytes.
    database:
        Scenario / DB alias (e.g. ``scenario2``).
    user:
        Authenticated user for permission checks and Task ownership.
    source_name:
        Optional display name for the Task / response.

    Returns
    -------
    dict
        ``status``, ``scenario``, ``sheets``, ``errors``, ``warnings``,
        ``ignored``, ``task_id``, ``message``.
    """
    _validate_database(database, user)

    tmp_path, owned = _save_upload_to_temp(upload)
    display_name = source_name or getattr(upload, "name", None) or Path(tmp_path).name

    now = datetime.now()
    task = Task(
        name="importworkbook",
        submitted=now,
        started=now,
        status="0%",
        user=user if user and not user.is_anonymous else None,
        arguments=display_name,
    )
    task.save(using=database)

    excel_duration_in_days = (
        Parameter.getValue("excel_duration_in_days", database, "false").lower() == "true"
    )

    all_models = [
        (ct.model_class(), ct.pk)
        for ct in ContentType.objects.all()
        if ct.model_class()
    ]

    sheets = []
    ignored = []
    total_errors = 0
    total_warnings = 0
    old_thread_locals = getattr(_thread_locals, "database", None)
    old_request = getattr(_thread_locals, "request", None)

    try:
        setattr(_thread_locals, "database", database)
        # Prefer scenario from payload/arg over the URL-less API default DB.
        if old_request is not None:
            setattr(old_request, "database", database)
        try:
            wb = load_workbook(filename=tmp_path, data_only=True)
        except Exception as exc:
            raise WorkbookImportError("Error reading excel file: %s" % exc) from exc

        models = []
        for ws_name in wb.sheetnames:
            model = None
            contenttype_id = None
            for m, ct in all_models:
                if matchesModelName(ws_name, m):
                    model = m
                    contenttype_id = ct
                    break
            if not model or model in EXCLUDE_FROM_BULK_OPERATIONS:
                ignored.append({"sheet": ws_name, "reason": "no matching model"})
                continue
            if user and not user.is_anonymous:
                perm = "%s.%s" % (
                    model._meta.app_label,
                    get_permission_codename("add", model._meta),
                )
                if not user.has_perm(perm):
                    ignored.append(
                        {"sheet": ws_name, "reason": "missing permission %s" % perm}
                    )
                    continue
            deps = {model}
            GridReport.dependent_models(model, deps)
            models.append((ws_name, model, contenttype_id, deps))

        models = GridReport.sort_models(models)

        with transaction.atomic(using=database):
            for ws_name, model, contenttype_id, _deps in models:
                numerrors = 0
                numwarnings = 0
                row_messages = []
                ws = wb[ws_name]
                for error in parseExcelWorksheet(
                    model,
                    ws,
                    user=user,
                    database=database,
                    ping=False,
                    excel_duration_in_days=excel_duration_in_days,
                ):
                    level = error[0]
                    if level == logging.DEBUG:
                        continue
                    msg = {
                        "row": error[1],
                        "field": error[2],
                        "value": force_str(error[3]) if error[3] is not None else "",
                        "message": force_str(error[4]),
                    }
                    if level == logging.ERROR:
                        numerrors += 1
                        msg["level"] = "error"
                        row_messages.append(msg)
                    elif level == logging.WARNING:
                        numwarnings += 1
                        msg["level"] = "warning"
                        row_messages.append(msg)
                if str(model._meta) == "common.parameter":
                    excel_duration_in_days = (
                        Parameter.getValue(
                            "excel_duration_in_days", database, "false"
                        ).lower()
                        == "true"
                    )
                sheets.append(
                    {
                        "sheet": ws_name,
                        "model": str(model._meta),
                        "errors": numerrors,
                        "warnings": numwarnings,
                        "messages": row_messages[:50],
                    }
                )
                total_errors += numerrors
                total_warnings += numwarnings

        if database == DEFAULT_DB_ALIAS:
            addAttributesFromDatabase()

        try:
            from django.conf import settings

            if "ccAPPSdb.forecast" in settings.INSTALLED_APPS:
                from ccAPPSdb.forecast.models import ForecastPlan

                ForecastPlan.refreshTableColumns(database)
        except Exception:
            logger.exception("ForecastPlan.refreshTableColumns failed after import")

        task.status = "Done" if total_errors == 0 else "Done"
        task.message = (
            "Imported %s sheet(s), %s error(s), %s warning(s), ignored %s"
            % (len(sheets), total_errors, total_warnings, len(ignored))
        )
        task.finished = datetime.now()
        task.processid = None
        task.save(using=database)

        return {
            "status": "success" if total_errors == 0 else "partial",
            "scenario": database,
            "file": display_name,
            "sheets": sheets,
            "ignored": ignored,
            "errors": total_errors,
            "warnings": total_warnings,
            "task_id": task.id,
            "message": task.message,
        }
    except Exception as exc:
        task.status = "Failed"
        task.message = force_str(exc)
        task.finished = datetime.now()
        task.processid = None
        try:
            task.save(using=database)
        except Exception:
            logger.exception("Failed to persist importworkbook task failure state")
        raise
    finally:
        setattr(_thread_locals, "database", old_thread_locals)
        if owned:
            Path(tmp_path).unlink(missing_ok=True)

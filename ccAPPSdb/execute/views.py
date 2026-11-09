from datetime import datetime, timedelta
from importlib import import_module
from io import BytesIO
import json
from openpyxl import load_workbook, Workbook
from openpyxl.utils import get_column_letter
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import NamedStyle, PatternFill
from openpyxl.comments import Comment as CellComment
import os
import re
import shlex
from time import sleep, localtime, strftime
from zipfile import ZipFile, ZIP_DEFLATED

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_permission_codename
from django.contrib.contenttypes.models import ContentType
from django.db import connections, transaction
from django.db.models.fields import AutoField
from django.db.models.fields.related import ForeignKey
from django.views.decorators.cache import never_cache
from django.shortcuts import render
from django.utils.html import escape
from django.utils.translation import gettext_lazy as _
from django.db import DEFAULT_DB_ALIAS
from django.contrib.admin.views.decorators import staff_member_required
from django.views.decorators.csrf import csrf_protect, csrf_exempt
from django.http import (
    Http404,
    HttpResponseRedirect,
    HttpResponseServerError,
    HttpResponse,
    HttpResponseNotFound,
    HttpResponseForbidden,
    HttpResponseNotAllowed,
    JsonResponse,
    StreamingHttpResponse,
)
from django.contrib import messages
from django.utils.encoding import force_str
from django.utils.text import capfirst
from django.core.management import get_commands, call_command
from django.core.exceptions import PermissionDenied, ValidationError

from ccAPPSdb import __version__
from ccAPPSdb.admin import data_site
from ccAPPSdb.common.dataload import parseExcelWorksheet
from ccAPPSdb.common.models import Scenario, HierarchyModel, Parameter
from ccAPPSdb.common.report import (
    GridFieldDuration,
    GridFieldBool,
    GridFieldLocalDateTime,
    GridReport,
    GridFieldText,
    GridFieldInteger,
    EXCLUDE_FROM_BULK_OPERATIONS,
    _getCellValue,
    matchesModelName,
    sizeof_fmt,
)
from ccAPPSdb.common.utils import get_databases

from ccAPPSdb.common.utils import forceWsgiReload
from ccAPPSdb.common.views import sendStaticFile
from .utils import updateScenarioCount
from .models import Task, ScheduledTask, DataExport
from .management.commands.runworker import launchWorker
from .management.commands.runplan import parseConstraints, constraintString
from .management.commands.scheduletasks import scheduler
from .services.workbook_import import (
    WorkbookImportError,
    import_ccpl_payload,
    import_workbook as run_workbook_import,
)

import logging

logger = logging.getLogger(__name__)


@csrf_exempt
def CCPLPlanAPI(request):
    """Import planning master data via HTTP, then optionally run the plan.

    Public path: ``POST /api/ccpl/plan/`` (replaces ``/api/ccpl/import/``
    and the older four-table planner that previously lived on this URL).

    Two request shapes are supported:

    1) multipart/form-data
       - spreadsheet | file | workbook: .xlsx file
       - scenario (optional)

    2) application/json — protocol 1.0 envelope + 13 workbook ``sheets``

    When ``run_plan`` is true, the JSON body matches
    ``long_2A_mrp_result_175.json`` / ``run_ccpl_plan``:
    ``summary``, ``operationplans``, ``resources``, ``materials``,
    ``problems``, ``constraints``, plus ``status``, ``scenario``,
    ``request_id``, ``constraint``, ``plantype``, ``env``,
    ``schedule_mode``, ``solver``.

    Also accepts POST /scenario2/api/ccpl/plan/ without a scenario field.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not request.user.is_authenticated:
        return JsonResponse(
            {"status": "error", "message": "Authentication required"}, status=401
        )

    content_type = (request.content_type or "").split(";")[0].strip().lower()
    is_json = content_type == "application/json" or (
        not request.FILES and request.body and request.body[:1] in (b"{", b"[")
    )

    try:
        if is_json:
            try:
                payload = json.loads(
                    request.body.decode(request.encoding or settings.DEFAULT_CHARSET)
                )
            except json.JSONDecodeError:
                return JsonResponse(
                    {"status": "error", "message": "Invalid JSON request body"},
                    status=400,
                )
            database = (
                payload.get("scenario")
                or request.GET.get("scenario")
                or getattr(request, "database", None)
                or DEFAULT_DB_ALIAS
            )
            result = import_ccpl_payload(
                payload, database=database, user=request.user
            )
        else:
            upload = (
                request.FILES.get("spreadsheet")
                or request.FILES.get("file")
                or request.FILES.get("workbook")
            )
            if not upload:
                return JsonResponse(
                    {
                        "status": "error",
                        "message": "Missing workbook file (form field "
                        "'spreadsheet'/'file'/'workbook') or JSON body with 'sheets'",
                    },
                    status=400,
                )
            database = (
                request.POST.get("scenario")
                or request.GET.get("scenario")
                or getattr(request, "database", None)
                or DEFAULT_DB_ALIAS
            )
            result = run_workbook_import(
                upload,
                database=database,
                user=request.user,
                source_name=upload.name,
            )
        status_code = 200 if result.get("status") == "success" else 207
        return JsonResponse(
            result, status=status_code, json_dumps_params={"ensure_ascii": False}
        )
    except ValidationError as exc:
        return JsonResponse(
            {"status": "error", "message": "; ".join(exc.messages)}, status=400
        )
    except PermissionDenied as exc:
        return JsonResponse({"status": "error", "message": str(exc)}, status=403)
    except WorkbookImportError as exc:
        return JsonResponse({"status": "error", "message": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("CCPL workbook import failed")
        return JsonResponse({"status": "error", "message": str(exc)}, status=500)


class TaskReport(GridReport):
    """
    A list report to review the history of actions.
    """

    template = "execute/execute.html"
    title = _("Task status")
    basequeryset = (
        Task.objects.all()
        .extra(
            select={
                "duration": """case when (processid is not null or started is not null) and status not in ('Canceled','Failed') then
                   date_trunc('second', coalesce(finished::timestamp(0), now()) - started::timestamp(0))
                   end
                   """
            }
        )
        .select_related("user")
    )
    model = Task
    frozenColumns = 0
    multiselect = False
    editable = False
    height = 150
    default_sort = (0, "desc")
    help_url = "user-interface/execute.html"

    rows = (
        GridFieldInteger("id", title=_("identifier"), key=True),
        GridFieldText("name", title=_("name"), editable=False, align="center"),
        GridFieldLocalDateTime(
            "submitted", title=_("submitted"), editable=False, align="center"
        ),
        GridFieldLocalDateTime(
            "started", title=_("started"), editable=False, align="center"
        ),
        GridFieldLocalDateTime(
            "finished", title=_("finished"), editable=False, align="center"
        ),
        GridFieldDuration(
            "duration",
            title=_("duration"),
            search=False,
            editable=False,
            align="center",
        ),
        GridFieldText(
            "status",
            title=_("status"),
            editable=False,
            align="center",
            extra="formatter:status",
        ),
        GridFieldText(
            "logfile",
            title=_("log file"),
            width=80,
            editable=False,
            align="center",
            extra="formatter:logbutton",
        ),
        GridFieldText(
            "message",
            title=_("message"),
            editable=False,
            width=500,
            formatter="longstring",
        ),
        GridFieldText(
            "arguments", title=_("arguments"), formatter="longstring", editable=False
        ),
        GridFieldText(
            "user",
            title=_("user"),
            field_name="user__username",
            editable=False,
            align="center",
        ),
        GridFieldBool("cancelable", title="cancelable", hidden=True),
    )

    @classmethod
    def extra_context(reportclass, request, *args, **kwargs):
        # Get the list of all available commands
        all_commands = []
        cnt = 0
        for commandname, appname in get_commands().items():
            try:
                cmd = getattr(
                    import_module("%s.management.commands.%s" % (appname, commandname)),
                    "Command",
                )
                if getattr(cmd, "index", -1) >= 0 and getattr(cmd, "getHTML", None):
                    cmd.name = commandname
                    html = cmd.getHTML(request)
                    if html:
                        all_commands.append(
                            {
                                "command": cmd,
                                "options": {"collapsed": cmd.name != "runplan"},
                                "html": html,
                            }
                        )
                    cnt += 1
            except Exception as e:
                logger.warning(
                    "Couldn't import getHTML method from %s.management.commands.%s: %s"
                    % (appname, commandname, e)
                )

        # Keep only planning/scenario commands on the main execute page
        _keep_commands = {"runplan", "scheduletasks", "scenario_copy"}
        all_commands = [c for c in all_commands if c["command"].name in _keep_commands]

        # Use the preferences
        commandlist1 = []
        commandlist2 = []
        prefs = getattr(request, "prefs", None)
        if prefs:
            # Add preferences to the command lists
            widgets = prefs.get("widgets", [])
            for row in widgets:
                column = row.get("name", "")
                if column == "column1":
                    column = commandlist1
                elif column == "column2":
                    column = commandlist2
                else:
                    continue
                for c in row.get("cols", []):
                    for w in c.get("widgets", []):
                        cnt1 = 0
                        for cmd in all_commands:
                            if cmd["command"].name == w[0]:
                                cmd["options"] = w[1]
                                cnt += 1
                                column.append(cmd)
                                del all_commands[cnt1]
                                break
                            cnt1 += 1
            # Add any commands not yet in the preferences
            if widgets:
                for w in all_commands:
                    commandlist1.append(w)
        if not commandlist1 and not commandlist2:
            # No preferences, divide the commands equal over 2 columns
            all_commands.sort(key=lambda x: x["command"].index)
            mid = (len(all_commands) + 1) // 2
            commandlist1 = all_commands[:mid]
            commandlist2 = all_commands[mid:]

        # Send to template
        return {"commandlist1": commandlist1, "commandlist2": commandlist2}

    @classmethod
    def query(reportclass, request, basequery, sortsql="1 asc"):
        logfileslist = set(
            [
                x
                for x in os.listdir(settings.CCAPPS_LOGDIR)
                if x.endswith(".log")
                or (x.lower().endswith(".dump") and request.user.is_superuser)
            ]
        )

        Task.removeUnhealthyTasks(request.database)

        for rec in basequery:
            yield {
                "id": rec.id,
                "name": rec.name,
                "submitted": rec.submitted,
                "started": rec.started,
                "finished": rec.finished,
                "status": rec.status,
                "logfile": rec.logfile if rec.logfile in logfileslist else None,
                "message": rec.message,
                "arguments": rec.arguments,
                "user__username": rec.user.username if rec.user else None,
                "duration": rec.duration,
                "cancelable": rec.processid is not None or rec.status == "Waiting",
            }

    @classmethod
    def extraJSON(reportclass, request):
        try:
            lastCompletedTask = (
                Task.objects.all()
                .using(request.database)
                .filter(status="Done")
                .order_by("-id")
                .only("id")[0]
            )
            return '"lastcompleted":%d,\n' % lastCompletedTask.id
        except Exception:
            return '"lastcompleted":0,\n'


@csrf_exempt
def APITask(request, action):
    try:
        if action == "status":
            response = {}
            args = request.GET.getlist("id")
            if args:
                msg = "retrieving task status of %s" % ",".join(args)
                tasks = Task.objects.all().using(request.database).filter(id__in=args)
            else:
                msg = "retrieving task status of all running tasks"
                tasks = (
                    Task.objects.all()
                    .using(request.database)
                    .filter(finished__isnull=True)
                    .exclude(status="Canceled")
                )
            for t in tasks:
                row = {
                    "name": t.name,
                    "submitted": str(t.submitted),
                    "started": str(t.started),
                    "finished": str(t.finished),
                    "arguments": t.arguments,
                    "status": t.status,
                    "message": t.message,
                    "user": t.user.username if t.user else None,
                    "logfile": t.logfile,
                }
                # Local-reschedule observability: only attach when this task
                # actually ran local CP (avoid stale param on full MRP).
                if t.name == "runplan" and t.status in ("Done", "Failed"):
                    try:
                        message = t.message or ""
                        raw = (
                            message[len("local_outcome=") :]
                            if message.startswith("local_outcome=")
                            else ""
                        )
                        # Backward compatibility for tasks created before
                        # per-task outcome storage was introduced.
                        if not raw and message.startswith("local level_used="):
                            raw = Parameter.getValue(
                                "cp.local_last_outcome", request.database, ""
                            )
                        if raw:
                            outcome = json.loads(raw)
                            success = outcome.pop("status", None)
                            if not isinstance(success, bool):
                                # Compatibility with older stored outcomes.
                                success = outcome.pop("local_ok", False) is True
                            else:
                                outcome.pop("local_ok", None)
                            # Reschedule API exposes one terminal result only:
                            # true=success, false=failure.
                            row["status"] = success
                            row["local_outcome"] = outcome
                    except Exception:
                        pass
                response[t.id] = row
        elif action == "cancel":
            response = {}
            with transaction.atomic(using=request.database):
                args = request.GET.getlist("id")
                msg = "canceling task %s" % ",".join(args)
                for t in (
                    Task.objects.all()
                    .using(request.database)
                    .select_for_update()
                    .filter(id__in=args)
                ):
                    if request.user.is_superuser or t.user == request.user:
                        if t.processid or t.status == "Waiting":
                            t.killProcess()
                            sleep(1)  # Wait for it to die
                        else:
                            continue
                        response[t.id] = {
                            "name": t.name,
                            "submitted": str(t.submitted),
                            "started": str(t.started),
                            "finished": str(t.finished),
                            "arguments": t.arguments,
                            "status": t.status,
                            "message": t.message,
                            "user": t.user.username if t.user else None,
                        }
        elif action == "log":
            taskid = request.GET.get("id", None)
            if taskid is None:
                return HttpResponseNotFound("No task argument passed")
            try:
                msg = f"retrieving task log of {taskid}"
                task = (
                    Task.objects.all().using(request.database).filter(id=taskid).first()
                )
                if not task:
                    response = {"message": f"Task {taskid} not found"}
                elif not task.logfile:
                    response = {"message": f"Task {taskid} has no logfile"}
                else:
                    return sendStaticFile(
                        request,
                        settings.CCAPPS_LOGDIR,
                        task.logfile,
                        headers={
                            "Content-Type": "application/octet-stream",
                            "Content-Disposition": 'inline; filename="CCAPPS_%s_%s.log"'
                            % (request.database, taskid),
                        },
                    )
            except Exception as e:
                return HttpResponseNotFound("Task or log file not found")
        elif action == "cleanup":
            msg = "cleaning up db connections / stuck tasks"
            response = _cleanupDbResources(request)
        else:
            msg = "launching task"
            task = wrapTask(request, action)
            if task:
                response = {"taskid": task.id, "message": "Successfully launched task"}
            else:
                response = {"message": "No task was launched"}
    except Exception:
        response = {"message": "Exception %s" % msg}
    return HttpResponse(json.dumps(response), content_type="application/json")


_SCOPE_RELAX_ALIASES = {
    "neighbors": "neighbors", "neighbor": "neighbors", "l2": "neighbors", "邻居": "neighbors",
    "personnel": "personnel", "person": "personnel", "人员": "personnel",
    "window": "window", "in_window": "window", "in-window": "window", "窗内": "window", "窗口": "window",
    "all": "all", "global": "all", "l3": "all", "全局": "all",
}


def _parse_scope_relax(raw):
    """Normalize customer relax tokens for custom local scope."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.replace(";", ",").split(",")
    if not isinstance(raw, (list, tuple)):
        raise ValidationError("relax must be a list or comma-separated string")
    out = []
    for item in raw:
        key = str(item).strip()
        if not key:
            continue
        mapped = _SCOPE_RELAX_ALIASES.get(key.lower()) or _SCOPE_RELAX_ALIASES.get(key)
        if not mapped:
            raise ValidationError(f"unknown relax token '{item}'")
        if mapped not in out:
            out.append(mapped)
    return out


_ORDER_ACTION_ALIASES = {
    "insert": "insert",
    "new": "insert",
    "add": "insert",
    "插单": "insert",
    "新增": "insert",
    "delete": "delete",
    "remove": "delete",
    "删单": "delete",
    "删除": "delete",
    "change": "change",
    "update": "change",
    "改单": "change",
    "修改": "change",
    "变更": "change",
}


def _normalize_order_action(raw, name):
    key = str(raw or "").strip()
    mapped = _ORDER_ACTION_ALIASES.get(key.lower()) or _ORDER_ACTION_ALIASES.get(key)
    if mapped not in ("insert", "delete", "change"):
        raise ValidationError(
            f"order {name or '(missing name)'} action must be insert, delete, or change"
        )
    return mapped


def _order_actions_from_body(body, names):
    """One action per order. A request may mix insert, delete, and change.

    ``orders`` is ``[{name, action}, ...]``. The older ``new_orders`` /
    ``deleted_orders`` lists remain accepted when ``orders`` is omitted:
    name only in new → insert, only in deleted → delete, in both → change.
    """
    legacy_new = names("new_orders")
    legacy_deleted = names("deleted_orders")
    has_legacy = bool(legacy_new or legacy_deleted)
    if "orders" not in body or body.get("orders") is None:
        deleted_set = set(legacy_deleted)
        actions = []
        seen = set()
        for name in legacy_new:
            action = "change" if name in deleted_set else "insert"
            if name not in seen:
                actions.append({"name": name, "action": action})
                seen.add(name)
        for name in legacy_deleted:
            if name not in seen:
                actions.append({"name": name, "action": "delete"})
                seen.add(name)
        return actions
    if has_legacy:
        raise ValidationError(
            "pass orders with an action on each order, "
            "or new_orders/deleted_orders, not both"
        )
    raw = body.get("orders")
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        raise ValidationError("orders must be a list of {name, action}")
    actions = []
    seen = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ValidationError("orders must be a list of {name, action}")
        name = str(item.get("name") or item.get("order") or "").strip()
        if not name:
            raise ValidationError("each order needs a name")
        action = _normalize_order_action(
            item.get("action", item.get("type")), name
        )
        previous = seen.get(name)
        if previous and previous != action:
            raise ValidationError(
                f"order {name} has conflicting actions {previous} and {action}"
            )
        if previous:
            continue
        seen[name] = action
        row = {"name": name, "action": action}
        quantity = _optional_number(item, "quantity", "qty")
        due = _optional_text(item, "due")
        item_name = _optional_text(item, "item")
        if quantity is not None:
            row["quantity"] = quantity
        if due:
            row["due"] = due
        if item_name:
            row["item"] = item_name
        actions.append(row)
    return actions


def _optional_text(item, *keys):
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return None


def _optional_number(item, *keys):
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return None


def _parse_when(value, label):
    from django.utils.dateparse import parse_datetime

    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = parse_datetime(text)
    if parsed is None:
        raise ValidationError(f"{label} must be an ISO datetime")
    return parsed


def _change_entries(body, key):
    """Name strings stay seeds. Objects carry the values this request writes."""
    value = body.get(key) or []
    if isinstance(value, str):
        value = [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]
    if not isinstance(value, (list, tuple)):
        raise ValidationError(f"{key} must be a list")
    entries = []
    seen = set()
    for item in value:
        if isinstance(item, str):
            name = item.strip()
            if not name or name in seen:
                continue
            seen.add(name)
            entries.append({"name": name})
            continue
        if not isinstance(item, dict):
            raise ValidationError(f"{key} entries must be a name or an object")
        name = _optional_text(item, "name", "item", "resource", "order")
        if not name:
            raise ValidationError(f"each {key} entry needs a name")
        extra = {
            field: item[field]
            for field in item
            if field != "name" and item[field] not in (None, "")
        }
        # A bare name is only a seed, so a repeat adds nothing. A second
        # object for the same name is another change (on-hand plus a purchase
        # order, or a second capacity window) and has to be applied too.
        if name in seen and not extra:
            continue
        seen.add(name)
        entries.append({"name": name, **extra})
    return entries


def _reschedule_values(body):
    """Validate a reschedule request and return all trigger parameters."""
    if body.get("new_resources"):
        raise ValidationError(
            "new_resources are not supported yet; resource rescheduling only "
            "accepts existing resources whose capacity or calendar changed"
        )
    category = str(body.get("category") or "").strip().lower()
    category = {
        "订单": "order",
        "订单变化": "order",
        "物料": "material",
        "物料变化": "material",
        "资源": "resource",
        "资源变化": "resource",
        "全部": "all",
        "一起": "all",
        "混合": "all",
        "combined": "all",
        "mixed": "all",
    }.get(category, category)
    if category not in ("order", "material", "resource", "all"):
        raise ValidationError("category must be order, material, resource, or all")
    level = str(body.get("level") or "auto").strip().lower()
    level = {"l1": "1", "l2": "2", "l3": "3"}.get(level, level)
    if level not in ("auto", "1", "2", "3"):
        raise ValidationError("level must be auto, 1, 2, or 3; custom is not supported yet")
    if any(key in body for key in ("scope", "scope_base", "relax")):
        raise ValidationError(
            "custom reschedule scope is not supported yet; use level auto, 1, 2, or 3"
        )

    def names(key):
        value = body.get(key) or []
        if isinstance(value, str):
            value = value.replace(";", ",").split(",")
        if not isinstance(value, (list, tuple)):
            raise ValidationError(f"{key} must be a list or comma-separated string")
        return list(
            dict.fromkeys(str(v).strip() for v in value if str(v).strip())
        )

    order_actions = _order_actions_from_body(body, names)
    new_orders = [
        row["name"] for row in order_actions if row["action"] in ("insert", "change")
    ]
    deleted_orders = [
        row["name"] for row in order_actions if row["action"] in ("delete", "change")
    ]
    materials = _change_entries(body, "materials")
    resources = _change_entries(body, "resources")
    has_order = bool(new_orders or deleted_orders)
    has_material = bool(materials)
    has_resource = bool(resources)
    if category == "all" and not (has_order or has_material or has_resource):
        raise ValidationError("category=all requires at least one trigger")
    if category == "order" and not has_order:
        raise ValidationError(
            "order rescheduling requires orders, or new_orders/deleted_orders"
        )
    if category == "material" and not has_material:
        raise ValidationError("material rescheduling requires materials")
    if category == "resource" and not has_resource:
        raise ValidationError("resource rescheduling requires resources")
    supplied = {"order": has_order, "material": has_material, "resource": has_resource}
    ignored = [name for name, present in supplied.items() if present and name != category]
    if category != "all" and ignored:
        raise ValidationError(
            f"category={category} cannot include {', '.join(ignored)} triggers; "
            "use category=all to combine, or submit separately"
        )

    include_order = category in ("order", "all")
    include_material = category in ("material", "all")
    include_resource = category in ("resource", "all")
    solver = str(body.get("solver") or "mrp").strip().lower()
    solver = {
        "cpsat": "cp",
        "cp-sat": "cp",
        "局部cp": "cp",
        "fas": "fas",
        "forward": "fas",
        "heuristic": "mrp",
        "solver_mrp": "mrp",
        "nsga": "ga",
        "genetic": "ga",
    }.get(solver, solver)
    if solver not in ("cp", "fas", "mrp", "ga"):
        raise ValidationError("solver must be cp, fas, mrp, or ga")
    new_demands = ",".join(new_orders) if include_order else ""
    deleted_demands = ",".join(deleted_orders) if include_order else ""
    material_names = list(dict.fromkeys(row["name"] for row in materials))
    resource_names = list(dict.fromkeys(row["name"] for row in resources))
    changed_materials = ",".join(material_names) if include_material else ""
    changed_resources = ",".join(resource_names) if include_resource else ""
    # plan.skip_supply is the skip-rebuild switch. Other triggers stay on
    # local.* with a cp.local_* copy (engine still reads CP_LOCAL_*).
    values = {
        "plan.solver": solver,
        "plan.skip_supply": "1",
        "local.reschedule": "1",
        "local.insert_candidates": "1",
        "local.one_shot": "1",
        "local.reschedule_type": category,
        "local.reschedule_level": level,
        "local.new_demands": new_demands,
        "local.deleted_demands": deleted_demands,
        "local.changed_materials": changed_materials,
        "local.changed_resources": changed_resources,
        "cp.local_reschedule": "1",
        "cp.local_skip_supply": "1",
        "cp.local_insert_candidates": "1",
        "cp.local_one_shot": "1",
        "cp.local_reschedule_type": category,
        "cp.local_reschedule_level": level,
        "cp.local_new_demands": new_demands,
        "cp.local_deleted_demands": deleted_demands,
        "cp.local_changed_materials": changed_materials,
        "cp.local_changed_resources": changed_resources,
        "_orders": order_actions if include_order else [],
        "_new_orders": new_orders if include_order else [],
        "_deleted_orders": deleted_orders if include_order else [],
        "_materials": materials if include_material else [],
        "_resources": resources if include_resource else [],
        "_solver": solver,
    }
    values["local.scope_base"] = ""
    values["local.scope_relax"] = ""
    values["cp.local_scope_base"] = ""
    values["cp.local_scope_relax"] = ""
    return values


def _validate_reschedule_refs(database, values):
    """Reschedule assumes master data is already ready.

    Callers write/update demands (or materials/resources) first; this API only
    schedules. Trigger names must already resolve in DB:
      - order insert/change: new_orders → Demand exists
      - pure delete: Demand must exist; this API captures its affected scope
        and deletes the demand/plan tree atomically before scheduling
      - pure insert (in new_orders only): must NOT already have operationplans
      - material → Item exists
      - resource → Resource exists
    """
    category = values["cp.local_reschedule_type"]
    parts = []
    if category in ("order", "all"):
        new_names = values.get("_new_orders") or []
        deleted_names = values.get("_deleted_orders") or []
        deleted_set = set(deleted_names)
        names = list(dict.fromkeys([*new_names, *deleted_names]))
        if names:
            from ccAPPSdb.input.models.demand import Demand
            from ccAPPSdb.input.models.operationplan import OperationPlan

            found = set(
                Demand.objects.using(database)
                .filter(name__in=names)
                .values_list("name", flat=True)
            )
            missing_new = [n for n in new_names if n not in found]
            pure_deleted = [n for n in deleted_names if n not in set(new_names)]
            missing_deleted = [n for n in pure_deleted if n not in found]
            if missing_new:
                parts.append(
                    "new_orders not found in demand (write via /api/input/demand/ "
                    "or write_order first): " + ", ".join(missing_new)
                )
            if missing_deleted:
                parts.append(
                    "deleted_orders not found in demand (nothing was deleted; "
                    "check the order number): " + ", ".join(missing_deleted)
                )
            # Pure insert: demand ready, but historical plan must not already
            # contain this order (use change = both lists if it does).
            insert_only = [
                n for n in new_names if n not in deleted_set and n in found
            ]
            if insert_only:
                already = set(
                    OperationPlan.objects.using(database)
                    .filter(demand_id__in=insert_only)
                    .values_list("demand_id", flat=True)
                    .distinct()
                )
                if already:
                    parts.append(
                        "insert orders already present in historical plan "
                        "(use action=change, or clear the plan first): "
                        + ", ".join(
                            n for n in insert_only if n in already
                        )
                    )
            # FAS only creates supply for open demands. A closed row with no
            # plan used to be accepted, its due rewritten, and the solve then
            # matched no root work order. Scenario 9 has no open demand
            # without a plan: write a new open demand (same item, location,
            # customer and a due after currentdate) and insert that name.
            if values.get("_solver") == "fas" and insert_only:
                status_rows = dict(
                    Demand.objects.using(database)
                    .filter(name__in=insert_only)
                    .values_list("name", "status")
                )
                not_open = [
                    n for n in insert_only if status_rows.get(n) != "open"
                ]
                if not_open:
                    parts.append(
                        "insert orders must be open demands so FAS can create "
                        "a plan (set status=open on the demand first; closed "
                        "demands are not planned): "
                        + ", ".join(not_open)
                    )
    if category in ("material", "all"):
        names = [row["name"] for row in (values.get("_materials") or []) if row.get("name")]
        if names:
            from ccAPPSdb.input.models.item import Item

            found = set(
                Item.objects.using(database)
                .filter(name__in=names)
                .values_list("name", flat=True)
            )
            missing = [n for n in names if n not in found]
            if missing:
                parts.append(
                    "materials not found in item: " + ", ".join(missing)
                )
    if category in ("resource", "all"):
        names = [row["name"] for row in (values.get("_resources") or []) if row.get("name")]
        if names:
            from ccAPPSdb.input.models.resource import Resource

            found = set(
                Resource.objects.using(database)
                .filter(name__in=names)
                .values_list("name", flat=True)
            )
            missing = [n for n in names if n not in found]
            if missing:
                parts.append(
                    "resources not found in resource: " + ", ".join(missing)
                )
    if parts:
        raise ValidationError("; ".join(parts))


def _json_ready(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _apply_reschedule_changes(database, values):
    """Write this request's new quantities and windows before the solve.

    Orders update the demand. Materials update on-hand or a purchase order.
    Resources get a capacity-calendar window; capacity defaults to 0 (down).
    The previous values are kept so a failed solve can put them back. The
    plan export is already skipped on failure; without this restore the
    demand, on-hand and calendar would stay while the plan rolled back.
    """
    from ccAPPSdb.common.models import Parameter

    demands = _apply_order_changes(database, values.get("_orders") or [])
    buffers, purchases = _apply_material_changes(
        database, values.get("_materials") or []
    )
    resources = _apply_resource_changes(database, values.get("_resources") or [])
    # The snapshot is how a failed FAS solve puts master data back. Other
    # solvers do not use this rollback, so they must not write it.
    if str(values.get("_solver") or "") == "fas":
        Parameter.objects.using(database).update_or_create(
            name="local.revert_changes",
            defaults={
                "value": json.dumps(
                    {
                        "demands": demands,
                        "buffers": buffers,
                        "purchases": purchases,
                        "resources": resources,
                    }
                )
            },
        )


def _apply_order_changes(database, orders):
    from decimal import Decimal

    from ccAPPSdb.input.models.demand import Demand
    from ccAPPSdb.input.models.item import Item

    revert = []
    for row in orders:
        if row.get("action") == "delete":
            continue
        fields = {}
        if row.get("quantity") is not None:
            fields["quantity"] = Decimal(str(row["quantity"]))
        if row.get("due"):
            fields["due"] = _parse_when(row["due"], f"order {row['name']} due")
        if row.get("item"):
            if not Item.objects.using(database).filter(name=row["item"]).exists():
                raise ValidationError(f"order item not found: {row['item']}")
            fields["item_id"] = row["item"]
        if not fields:
            continue
        current = (
            Demand.objects.using(database)
            .filter(name=row["name"])
            .values("quantity", "due", "item_id")
            .first()
        )
        if current is None:
            raise ValidationError(f"order not found in demand: {row['name']}")
        revert.append(
            {
                "name": row["name"],
                "quantity": _json_ready(current["quantity"]),
                "due": _json_ready(current["due"]),
                "item": current["item_id"],
            }
        )
        Demand.objects.using(database).filter(name=row["name"]).update(**fields)
    return revert


def _apply_material_changes(database, materials):
    from decimal import Decimal

    from ccAPPSdb.input.models.buffer import Buffer
    from ccAPPSdb.input.models.operationplan import OperationPlan

    buffers_revert = []
    purchases_revert = []
    for row in materials:
        name = row["name"]
        if row.get("onhand") not in (None, ""):
            onhand = Decimal(str(row["onhand"]))
            buffers = Buffer.objects.using(database).filter(item_id=name)
            location = _optional_text(row, "location")
            if location:
                buffers = buffers.filter(location_id=location)
            matched = list(buffers)
            if not matched:
                raise ValidationError(f"no buffer for material {name}")
            if len(matched) > 1 and not location:
                raise ValidationError(
                    f"material {name} is in more than one location; pass location"
                )
            for buf in matched:
                buffers_revert.append(
                    {
                        "item": buf.item_id,
                        "location": buf.location_id,
                        "onhand": _json_ready(buf.onhand),
                    }
                )
            buffers.update(onhand=onhand)
        has_po = any(row.get(key) not in (None, "") for key in ("po", "quantity", "arrival"))
        if not has_po:
            continue
        po = _optional_text(row, "po")
        quantity = row.get("quantity")
        arrival = row.get("arrival")
        if not po or quantity in (None, "") or not arrival:
            raise ValidationError(
                f"material {name} purchase change needs po, quantity, and arrival"
            )
        current = (
            OperationPlan.objects.using(database)
            .filter(reference=po, type="PO", item_id=name)
            .values("quantity", "enddate")
            .first()
        )
        if current is None:
            raise ValidationError(
                f"purchase order {po} for material {name} was not found"
            )
        purchases_revert.append(
            {
                "po": po,
                "item": name,
                "quantity": _json_ready(current["quantity"]),
                "end": _json_ready(current["enddate"]),
            }
        )
        OperationPlan.objects.using(database).filter(
            reference=po, type="PO", item_id=name
        ).update(
            quantity=Decimal(str(quantity)),
            enddate=_parse_when(arrival, f"material {name} arrival"),
        )
    return buffers_revert, purchases_revert


def _apply_resource_changes(database, resources):
    from datetime import datetime, time
    from decimal import Decimal

    from django.db.models import Min

    from ccAPPSdb.input.models.calendar import Calendar, CalendarBucket
    from ccAPPSdb.input.models.resource import Resource

    def _bucket_days():
        return {
            "starttime": time(0, 0, 0),
            "endtime": time(23, 59, 59),
            "monday": True,
            "tuesday": True,
            "wednesday": True,
            "thursday": True,
            "friday": True,
            "saturday": True,
            "sunday": True,
        }

    revert = []
    for row in resources:
        if not row.get("start") and not row.get("end") and row.get("capacity") in (None, ""):
            continue
        if not row.get("start") or not row.get("end"):
            raise ValidationError(
                f"resource {row['name']} needs start and end for a capacity window"
            )
        start = _parse_when(row["start"], f"resource {row['name']} start")
        end = _parse_when(row["end"], f"resource {row['name']} end")
        if end <= start:
            raise ValidationError(f"resource {row['name']} end must be after start")
        capacity = Decimal(str(row["capacity"])) if row.get("capacity") not in (None, "") else Decimal("0")
        resource = Resource.objects.using(database).filter(name=row["name"]).first()
        if resource is None:
            raise ValidationError(f"resources not found in resource: {row['name']}")
        normal = resource.maximum if resource.maximum is not None else Decimal("1")
        calendar = resource.maximum_calendar
        created_calendar = False
        assigned_new = calendar is None
        previous_calendar = calendar.name if calendar is not None else None
        if calendar is None:
            calendar, created_calendar = Calendar.objects.using(database).get_or_create(
                name=f"{resource.name} capacity",
                defaults={
                    "defaultvalue": normal,
                    "description": "reschedule capacity",
                },
            )
            resource.maximum_calendar = calendar
            resource.save(using=database, update_fields=["maximum_calendar"])
        buckets = CalendarBucket.objects.using(database).filter(calendar=calendar)
        if not buckets.exists():
            # Normal capacity for the whole horizon. A smaller priority on the
            # downtime window overrides just that interval.
            CalendarBucket.objects.using(database).create(
                calendar=calendar,
                startdate=datetime(1971, 1, 1),
                enddate=datetime(2030, 12, 31),
                priority=100,
                value=normal,
                **_bucket_days(),
            )
        # Smaller priority wins. 0 beats the baseline 100. An existing shift
        # calendar already uses 0, so step one below its lowest.
        lowest = buckets.aggregate(lowest=Min("priority"))["lowest"]
        priority = 0 if lowest is None or int(lowest) > 0 else int(lowest) - 1
        existing = CalendarBucket.objects.using(database).filter(
            calendar=calendar,
            startdate=start,
            enddate=end,
            priority=priority,
        ).first()
        revert.append(
            {
                "resource": resource.name,
                "previous_calendar": previous_calendar,
                "created_calendar": created_calendar,
                "assigned_new": assigned_new,
                "calendar": calendar.name,
                "startdate": start.isoformat(),
                "enddate": end.isoformat(),
                "priority": priority,
                "existed": existing is not None,
                "value": _json_ready(existing.value) if existing is not None else None,
            }
        )
        CalendarBucket.objects.using(database).update_or_create(
            calendar=calendar,
            startdate=start,
            enddate=end,
            priority=priority,
            defaults={"value": capacity, **_bucket_days()},
        )
    return revert


def revert_reschedule_inputs(database):
    """Put demand, on-hand and capacity windows back after a failed solve.

    Returns True when a snapshot was applied. A successful solve clears the
    snapshot so a later run cannot undo a change that already shipped.
    """
    from decimal import Decimal

    from ccAPPSdb.common.models import Parameter
    from ccAPPSdb.input.models.buffer import Buffer
    from ccAPPSdb.input.models.calendar import Calendar, CalendarBucket
    from ccAPPSdb.input.models.demand import Demand
    from ccAPPSdb.input.models.operationplan import OperationPlan
    from ccAPPSdb.input.models.resource import Resource

    raw = Parameter.getValue("local.revert_changes", database, "")
    if not raw:
        return False
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return False
    for row in payload.get("demands") or []:
        Demand.objects.using(database).filter(name=row["name"]).update(
            quantity=Decimal(row["quantity"]) if row.get("quantity") is not None else None,
            due=_parse_when(row["due"], "revert due") if row.get("due") else None,
            item_id=row.get("item"),
        )
    for row in payload.get("buffers") or []:
        buffers = Buffer.objects.using(database).filter(item_id=row["item"])
        if row.get("location"):
            buffers = buffers.filter(location_id=row["location"])
        buffers.update(
            onhand=Decimal(row["onhand"]) if row.get("onhand") is not None else None
        )
    for row in payload.get("purchases") or []:
        OperationPlan.objects.using(database).filter(
            reference=row["po"], type="PO", item_id=row["item"]
        ).update(
            quantity=Decimal(row["quantity"]) if row.get("quantity") is not None else None,
            enddate=_parse_when(row["end"], "revert arrival") if row.get("end") else None,
        )
    for row in payload.get("resources") or []:
        resource = Resource.objects.using(database).filter(name=row["resource"]).first()
        if resource is None:
            continue
        if row.get("assigned_new") or row.get("created_calendar"):
            previous = None
            if row.get("previous_calendar"):
                previous = Calendar.objects.using(database).filter(
                    name=row["previous_calendar"]
                ).first()
            resource.maximum_calendar = previous
            resource.save(using=database, update_fields=["maximum_calendar"])
            if row.get("created_calendar"):
                Calendar.objects.using(database).filter(name=row["calendar"]).delete()
                continue
        buckets = CalendarBucket.objects.using(database).filter(
            calendar__name=row["calendar"],
            startdate=_parse_when(row["startdate"], "revert start"),
            enddate=_parse_when(row["enddate"], "revert end"),
            priority=row["priority"],
        )
        if row.get("existed"):
            buckets.update(
                value=Decimal(row["value"]) if row.get("value") is not None else None
            )
        else:
            buckets.delete()
    Parameter.objects.using(database).update_or_create(
        name="local.revert_changes", defaults={"value": ""}
    )
    return True


def clear_reschedule_revert(database):
    from ccAPPSdb.common.models import Parameter

    Parameter.objects.using(database).update_or_create(
        name="local.revert_changes", defaults={"value": ""}
    )


def _prepare_pure_order_deletes(database, values):
    """Capture the released resource scope and atomically delete pure orders."""
    if values.get("cp.local_reschedule_type") not in ("order", "all"):
        return []
    new_names = set(values.get("_new_orders") or [])
    pure_deleted = [
        name for name in (values.get("_deleted_orders") or []) if name not in new_names
    ]
    if not pure_deleted:
        return []

    from ccAPPSdb.input.models.demand import Demand

    # Lock the rows after validation so a concurrent writer can't change or
    # delete the tree between scope capture and deletion.
    demands = list(
        Demand.objects.using(database)
        .select_for_update()
        .filter(name__in=pure_deleted)
    )
    found = {d.name for d in demands}
    missing = [name for name in pure_deleted if name not in found]
    if missing:
        raise ValidationError(
            "deleted_orders disappeared before deletion: " + ", ".join(missing)
        )

    # A demand is normally attached to its DLVR root. Follow owner links down
    # the complete tree to retain every machine, mold and personnel assignment.
    with connections[database].cursor() as cursor:
        cursor.execute(
            """
            WITH RECURSIVE deleted_tree(reference) AS (
              SELECT reference
              FROM operationplan
              WHERE demand_id = ANY(%s)
              UNION
              SELECT child.reference
              FROM operationplan child
              JOIN deleted_tree parent ON child.owner_id = parent.reference
            )
            SELECT DISTINCT resource_id
            FROM operationplanresource
            WHERE operationplan_id IN (SELECT reference FROM deleted_tree)
              AND resource_id IS NOT NULL
            """,
            [pure_deleted],
        )
        affected_resources = [row[0] for row in cursor.fetchall()]

    for demand in demands:
        demand.delete(using=database)
    # Preserve the resources released by the deleted order for the order repair.
    existing = [
        name
        for name in str(values.get("cp.local_changed_resources") or "").split(",")
        if name.strip()
    ]
    merged = ",".join(dict.fromkeys([*existing, *affected_resources]))
    values["cp.local_changed_resources"] = merged
    values["local.changed_resources"] = merged
    return affected_resources


@csrf_exempt
def RescheduleAPI(request):
    """Enqueue one reschedule; pure deletion is handled atomically.

    Two steps:
      1) ready master data via existing APIs (``/api/input/demand/``, ``write_order``, …)
      2) POST here. Each order carries action=insert|delete|change, and one
         request may mix them.
         insert → demand exists and has no operationplans;
         delete → demand exists; this API captures its affected scope and
                  deletes it atomically;
         change → demand row stays (caller already updated it) and is replanned.
         new_orders/deleted_orders remain accepted when orders is omitted.

    Category: ``order`` | ``material`` | ``resource`` | ``all``.
    ``all`` combines triggers from multiple problem types in one solve.

    Solver: ``mrp`` (default) | ``cp`` | ``fas`` | ``ga``. Shared scope
    (triggers + hot/freeze); the chosen solver only repairs the movable set.

    Scope: ``level`` = auto|1|2|3. Custom scope is not currently supported.

    After Done, ``GET .../execute/api/status/?id=<taskid>`` includes
    boolean ``status`` plus ``local_outcome`` diagnostics (level_used, applied,
    incomplete, named_late, window_late, stage, mode). A failed reschedule
    trigger skips plan export.
    """
    if request.method != "POST":
        return JsonResponse(
            {"status": "error", "message": "Only POST is allowed"}, status=405
        )
    if not request.user.is_authenticated:
        return JsonResponse(
            {"status": "error", "message": "Authentication required"}, status=401
        )
    if not request.user.has_perm("auth.auth.generate_plan"):
        return JsonResponse(
            {"status": "error", "message": "Missing execution privileges"},
            status=403,
        )
    try:
        if "json" not in request.headers.get("content-type", "").lower():
            raise ValidationError("Content-Type must be application/json")
        body = json.loads(request.body or b"{}")
        if not isinstance(body, dict):
            raise ValidationError("JSON body must be an object")
        values = _reschedule_values(body)
        raw_constraint = body.get("constraint", "capa,mfg_lt,po_lt")
        if isinstance(raw_constraint, (list, tuple)):
            raw_constraint = ",".join(str(v) for v in raw_constraint)
        constraint_text = str(raw_constraint).strip()
        try:
            int(constraint_text)
        except ValueError:
            allowed_constraints = {"capa", "fence", "lt", "mfg_lt", "po_lt"}
            unknown_constraints = [
                token.strip()
                for token in constraint_text.split(",")
                if token.strip().lower() not in allowed_constraints
            ]
            if unknown_constraints:
                raise ValidationError(
                    "unknown constraint token(s): "
                    + ", ".join(unknown_constraints)
                )
        constraints = constraintString(parseConstraints(constraint_text))

        with transaction.atomic(using=request.database):
            # Serialize the empty-queue check and parameter/task creation.
            # select_for_update alone locks no row when the queue is empty,
            # allowing two simultaneous requests to overwrite each other's
            # trigger parameters before either task starts.
            with connections[request.database].cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    ["ccapps.local_reschedule"],
                )
            active = (
                Task.objects.using(request.database)
                .select_for_update()
                .filter(name="runplan", finished__isnull=True)
                .exclude(status__in=("Done", "Failed", "Canceled"))
                .first()
            )
            if active:
                return JsonResponse(
                    {
                        "status": "conflict",
                        "message": "A planning task is already active",
                        "taskid": active.id,
                    },
                    status=409,
                )
            _validate_reschedule_refs(request.database, values)
            _apply_reschedule_changes(request.database, values)
            # Pure deletes record freed resources internally. Callers do not
            # pass that set.
            _prepare_pure_order_deletes(request.database, values)
            # plan.solver is a scenario-wide setting, while a reschedule is a
            # one-shot task. Preserve the ordinary planner choice so cleanup
            # can restore it after success, failure, or a child-process crash.
            values["local.previous_solver"] = Parameter.getValue(
                "plan.solver", request.database, "mrp"
            )
            # Internal list helpers are not plan parameters.
            solver = values.pop("_solver", "mrp")
            orders = list(values.get("_orders") or [])
            materials = list(values.get("_materials") or [])
            resources = list(values.get("_resources") or [])
            for key in (
                "_orders",
                "_new_orders",
                "_deleted_orders",
                "_materials",
                "_resources",
            ):
                values.pop(key, None)
            for name, value in values.items():
                Parameter.objects.using(request.database).update_or_create(
                    name=name, defaults={"value": value}
                )
            arguments = (
                f"--constraint={constraints} --plantype=1 --env=supply"
            )
            if Parameter.getValue(
                "plan.webservice", request.database, "true"
            ).lower() == "true":
                arguments += " --background"
            task = Task(
                name="runplan",
                submitted=datetime.now(),
                status="Waiting",
                user=request.user,
                arguments=arguments,
            )
            task.save(using=request.database)
        launchWorker(database=request.database)
        return JsonResponse(
            {
                "status": "accepted",
                "taskid": task.id,
                "scenario": request.database,
                "trigger": {
                    "solver": solver,
                    "category": values["cp.local_reschedule_type"],
                    "level": values["cp.local_reschedule_level"],
                    "orders": orders,
                    "materials": materials,
                    "resources": resources,
                },
                "status_url": f"{request.prefix}/execute/api/status/?id={task.id}",
                "log_url": f"{request.prefix}/execute/api/log/?id={task.id}",
            },
            status=202,
        )
    except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
        message = exc.messages[0] if isinstance(exc, ValidationError) else str(exc)
        return JsonResponse({"status": "error", "message": message}, status=400)


# Backward-compatible alias
LocalRescheduleAPI = RescheduleAPI


def _truthy(val, default=False):
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "y", "on")


def _cleanupDbResources(request):
    """Terminate idle Postgres backends and optionally cancel stuck execute tasks.

    Used when max_connections is tight (e.g. 40) and workers fail with
    "sorry, too many clients already". Called via GET/POST
    ``/<scenario>/execute/api/cleanup/``.
    """
    if not request.user.is_authenticated:
        return {
            "ok": False,
            "message": "Authentication required",
        }
    if not (
        request.user.is_superuser
        or request.user.has_perm("auth.run_db")
        or request.user.has_perm("auth.auth.generate_plan")
    ):
        return {
            "ok": False,
            "message": "Missing cleanup privileges",
        }

    args = request.GET if request.method == "GET" else (request.POST or request.GET)
    dry_run = _truthy(args.get("dry_run"), False)
    aggressive = _truthy(args.get("aggressive"), False)
    cancel_stuck = _truthy(args.get("cancel_stuck"), True)
    try:
        idle_seconds = max(0, int(args.get("idle_seconds", 60)))
    except (TypeError, ValueError):
        idle_seconds = 60
    try:
        stale_active_minutes = max(0, int(args.get("stale_active_minutes", 30)))
    except (TypeError, ValueError):
        stale_active_minutes = 30
    try:
        min_age_hours = max(0.0, float(args.get("min_age_hours", 0.5)))
    except (TypeError, ValueError):
        min_age_hours = 0.5

    db = request.database
    out = {
        "ok": True,
        "database": db,
        "dry_run": dry_run,
        "aggressive": aggressive,
        "idle_seconds": idle_seconds,
        "stale_active_minutes": stale_active_minutes,
        "cancel_stuck": cancel_stuck,
        "min_age_hours": min_age_hours,
        "max_connections": None,
        "connections_before": None,
        "connections_after": None,
        "by_state_before": {},
        "by_state_after": {},
        "terminated": [],
        "canceled_tasks": [],
        "message": "",
    }

    # --- Postgres connection cleanup (cluster view filtered to this DB) ---
    try:
        with connections[db].cursor() as cur:
            cur.execute("SHOW max_connections")
            out["max_connections"] = cur.fetchone()[0]

            def _conn_stats():
                cur.execute(
                    """
                    SELECT coalesce(state, 'null'), count(*)
                    FROM pg_stat_activity
                    WHERE datname = current_database()
                    GROUP BY 1
                    """
                )
                by_state = {r[0]: r[1] for r in cur.fetchall()}
                cur.execute(
                    """
                    SELECT count(*) FROM pg_stat_activity
                    WHERE datname = current_database()
                    """
                )
                total = cur.fetchone()[0]
                return total, by_state

            before, by_before = _conn_stats()
            out["connections_before"] = before
            out["by_state_before"] = by_before

            # Candidates: idle / idle-in-transaction (+ optional stale active)
            if aggressive:
                cur.execute(
                    """
                    SELECT pid, usename, application_name, state,
                           extract(epoch from (now() - state_change))::int AS idle_s,
                           left(coalesce(query, ''), 100) AS query
                    FROM pg_stat_activity
                    WHERE backend_type = 'client backend'
                      AND pid <> pg_backend_pid()
                      AND datname = current_database()
                      AND (
                        state IN ('idle', 'idle in transaction',
                                  'idle in transaction (aborted)')
                        OR (
                          %s > 0
                          AND state = 'active'
                          AND state_change < now() - make_interval(mins => %s)
                        )
                      )
                    ORDER BY state_change
                    """,
                    [stale_active_minutes, stale_active_minutes],
                )
            else:
                cur.execute(
                    """
                    SELECT pid, usename, application_name, state,
                           extract(epoch from (now() - state_change))::int AS idle_s,
                           left(coalesce(query, ''), 100) AS query
                    FROM pg_stat_activity
                    WHERE backend_type = 'client backend'
                      AND pid <> pg_backend_pid()
                      AND datname = current_database()
                      AND (
                        (
                          state IN ('idle', 'idle in transaction',
                                    'idle in transaction (aborted)')
                          AND state_change < now() - make_interval(secs => %s)
                        )
                        OR (
                          %s > 0
                          AND state = 'active'
                          AND state_change < now() - make_interval(mins => %s)
                        )
                      )
                    ORDER BY state_change
                    """,
                    [idle_seconds, stale_active_minutes, stale_active_minutes],
                )

            candidates = []
            for pid, usename, app, state, idle_s, query in cur.fetchall():
                candidates.append(
                    {
                        "pid": pid,
                        "usename": usename,
                        "application_name": app,
                        "state": state,
                        "idle_seconds": idle_s,
                        "query": query,
                    }
                )

            terminated = []
            for c in candidates:
                if dry_run:
                    terminated.append({**c, "terminated": False})
                    continue
                cur.execute("SELECT pg_terminate_backend(%s)", [c["pid"]])
                ok = bool(cur.fetchone()[0])
                terminated.append({**c, "terminated": ok})
            out["terminated"] = terminated

            after, by_after = _conn_stats()
            out["connections_after"] = after
            out["by_state_after"] = by_after
    except Exception as e:
        out["ok"] = False
        out["message"] = f"connection cleanup error: {type(e).__name__}: {e}"
        logger.exception("cleanup connection error on %s", db)
        return out

    # --- Stuck / zombie execute tasks ---
    if cancel_stuck:
        cutoff = datetime.now() - timedelta(hours=min_age_hours)
        canceled = []
        try:
            with transaction.atomic(using=db):
                qs = (
                    Task.objects.all()
                    .using(db)
                    .select_for_update()
                    .filter(finished__isnull=True)
                    .exclude(status__in=("Done", "Failed", "Canceled"))
                )
                for t in qs:
                    age_ref = t.started or t.submitted
                    if age_ref and age_ref > cutoff:
                        continue
                    if not (
                        request.user.is_superuser or t.user_id == request.user.id
                    ):
                        continue
                    entry = {
                        "id": t.id,
                        "name": t.name,
                        "status": t.status,
                        "processid": t.processid,
                        "submitted": str(t.submitted) if t.submitted else None,
                        "started": str(t.started) if t.started else None,
                    }
                    if dry_run:
                        entry["canceled"] = False
                        canceled.append(entry)
                        continue
                    try:
                        # killProcess handles Waiting / live processid; zombies
                        # with status like "0%" and no pid need a forced cancel.
                        if t.processid or t.status == "Waiting":
                            try:
                                t.killProcess()
                            except Exception:
                                pass
                        t.refresh_from_db()
                        if t.status != "Canceled" or t.finished is None:
                            t.processid = None
                            t.status = "Canceled"
                            t.message = (t.message or "") + " [canceled by cleanup]"
                            t.finished = datetime.now()
                            t.save(
                                update_fields=[
                                    "processid",
                                    "status",
                                    "message",
                                    "finished",
                                ],
                                using=db,
                            )
                        entry["status"] = "Canceled"
                        entry["canceled"] = True
                    except Exception as e:
                        entry["canceled"] = False
                        entry["error"] = f"{type(e).__name__}: {e}"
                    canceled.append(entry)
            out["canceled_tasks"] = canceled
        except Exception as e:
            out["ok"] = False
            out["message"] = (
                out.get("message") or ""
            ) + f"; task cleanup error: {type(e).__name__}: {e}"
            logger.exception("cleanup task error on %s", db)

    killed_n = sum(1 for x in out["terminated"] if x.get("terminated") or dry_run)
    canceled_n = len(out["canceled_tasks"])
    out["message"] = (
        f"{'dry-run: would free' if dry_run else 'freed'} {killed_n} connection(s); "
        f"{'would cancel' if dry_run else 'canceled'} {canceled_n} task(s); "
        f"connections {out['connections_before']} → {out['connections_after']} "
        f"(max={out['max_connections']})"
    )
    return out


@staff_member_required
@never_cache
@csrf_protect
def LaunchTask(request, action):
    try:
        if action == "exportworkbook":
            return exportWorkbook(request)
        elif action == "importworkbook":
            return StreamingHttpResponse(
                content_type="text/plain; charset=%s" % settings.DEFAULT_CHARSET,
                streaming_content=importWorkbook(request),
            )
        elif action in ("CCAPPS_stop_web_service", "stopwebservice"):
            if not request.user.has_perm("auth.auth.generate_plan"):
                raise Exception("Missing execution privileges")
            from django.core import management

            management.call_command(
                "stopwebservice", force=True, database=request.database
            )
            return HttpResponseRedirect("%s/execute/" % request.prefix)
        else:
            wrapTask(request, action)
            return HttpResponseRedirect("%s/execute/" % request.prefix)
    except Exception as e:
        logger.error("Error launching task: %s" % e)
        messages.add_message(
            request, messages.ERROR, force_str(_("Failure launching task"))
        )
        return HttpResponseRedirect("%s/execute/" % request.prefix)


def wrapTask(request, action):
    # Allow only post
    if request.method != "POST":
        raise Exception("Only post requests allowed")
    # Parse the posted parameters as arguments for an asynchronous task to add to the queue.    TODO MAKE MODULAR WITH SEPERATE TASK CLASS
    worker_database = request.database

    now = datetime.now()
    task = None
    args = request.POST or request.GET

    # A
    # TODO remove special case - call runwebservice or runplan instead
    if action in ("runplan", "runwebservice"):
        if not request.user.has_perm("auth.auth.generate_plan"):
            raise Exception("Missing execution privileges")
        constraint = parseConstraints(",".join(args.getlist("constraint")))
        task = Task(name="runplan", submitted=now, status="Waiting", user=request.user)
        background = False
        if action in ("CCAPPS_start_web_service", "runwebservice"):
            # Load existing plan and run as a web service
            background = True
            env = []
            constraint = 4 + 16 + 32
            plantype = 1
            try:
                lastrun = (
                    Task.objects.all()
                    .using(request.database)
                    .filter(name="runplan")
                    .order_by("-id")[0]
                )
                for i in shlex.split(lastrun.arguments or ""):
                    if "=" in i:
                        key, val = i.split("=", 1)
                        if key == "--constraint":
                            constraint = parseConstraints(val)
                        elif key == "--plantype":
                            plantype = int(val)
            except Exception:
                pass
            task.arguments = "--constraint=%s --plantype=%s --background" % (
                constraintString(constraint),
                plantype,
            )
        else:
            # Create a new plan
            task.arguments = "--constraint=%s --plantype=%s" % (
                constraintString(constraint),
                args.get("plantype", 1),
            )
            env = []
            for value in args.getlist("env"):
                env.append(value)
            task.arguments = "--constraint=%s --plantype=%s" % (
                constraintString(constraint),
                args.get("plantype", 1),
            )
            if (
                Parameter.getValue("plan.webservice", request.database, "true").lower()
                == "true"
            ):
                task.arguments += " --background"
                background = True
        if background:
            # Avoid getting multiple waiting tasks in background mode
            cnt = Task.objects.all().filter(name="runplan", status="Waiting").count()
            if cnt > 0:
                return None
        if env:
            task.arguments += " --env=%s" % ",".join(env)
        else:
            task.arguments += " --env=loadplan"
        task.save(using=request.database)
    # C
    elif action == "empty":
        if not request.user.has_perm("auth.run_db"):
            raise Exception("Missing execution privileges")
        task = Task(name="empty", submitted=now, status="Waiting", user=request.user)
        models = ",".join(args.getlist("models"))
        if models:
            task.arguments = "--models=%s" % (models)
        elif "all" in args:
            task.arguments = "--all"
        task.save(using=request.database)
    # D
    elif action == "loaddata":
        if not request.user.has_perm("auth.run_db"):
            raise Exception("Missing execution privileges")

        # Step 1: optionally empty the database
        if args.get("emptybefore", False) == "true":
            task = Task(
                name="empty",
                submitted=now,
                status="Waiting",
                user=request.user,
                arguments="--all",
            )
            task.save(using=request.database)

        # Step 2: load the fixture
        task = Task(
            name="loaddata",
            submitted=now,
            status="Waiting",
            user=request.user,
            arguments=args["fixture"],
        )
        task.save(using=request.database)

        # Step 3: optionally run the plan
        if args.get("regenerateplan", False) == "true":
            active_modules = "supply"
            if "ccAPPSdb.forecast" in settings.INSTALLED_APPS:
                active_modules = "fcst," + active_modules
            task = Task(
                name="runplan", submitted=now, status="Waiting", user=request.user
            )
            task.arguments = (
                "--constraint=capa,mfg_lt,po_lt --plantype=1 --env=%s --background"
                % (active_modules,)
            )
            task.save(using=request.database)
    # E
    elif action == "scenario_copy":
        worker_database = DEFAULT_DB_ALIAS
        if "copy" in args:
            if not request.user.has_perm("common.copy_scenario"):
                raise Exception("Missing execution privileges")
            source = args.get("source", request.database)
            worker_database = source
            destination = args.get("destination", False)
            dumpfile = args.get("dumpfile")
            if destination and destination != DEFAULT_DB_ALIAS:
                force = args.get("force", False)
                arguments = "%s %s" % (source, destination)
                if dumpfile:
                    arguments = "--dumpfile=%s %s" % (dumpfile, arguments)
                if force:
                    arguments += " --force"
                task = Task(
                    name="scenario_copy",
                    submitted=now,
                    status="Waiting",
                    user=request.user,
                    arguments=arguments,
                )
                task.save(using=source)
        elif "release" in args:
            if not request.user.has_perm("common.release_scenario"):
                raise Exception("Missing execution privileges")

            released_db = args.get("database", request.database)
            if released_db != DEFAULT_DB_ALIAS:
                sc = Scenario.objects.using(DEFAULT_DB_ALIAS).get(name=released_db)
                if sc.status == "In use":
                    sc.status = "Free"
                    sc.save(using=DEFAULT_DB_ALIAS)
                with connections[DEFAULT_DB_ALIAS].cursor() as cursor:
                    cursor.execute(
                        "UPDATE common_user SET databases = array_remove(databases, %s) "
                        "WHERE %s = ANY(databases)",
                        (released_db, released_db),
                    )
                task = Task(
                    name="scenario_release",
                    submitted=now,
                    status="Waiting",
                    user=request.user,
                    arguments="--database=%s" % released_db,
                )
                task.save(using=request.database)
        elif "promote" in args:
            if not request.user.has_perm("common.promote_scenario"):
                raise Exception("Missing execution privileges")
            source = args.get("source", request.database)
            worker_database = source
            destination = args.get("destination", False)
            if destination and destination == DEFAULT_DB_ALIAS:
                arguments = "--promote %s %s" % (source, destination)
                task = Task(
                    name="scenario_copy",
                    submitted=now,
                    status="Waiting",
                    user=request.user,
                    arguments=arguments,
                )
                task.save(using=source)
        elif "update" in args:
            # Note: update is immediate and synchronous.
            if not request.user.has_perm("common.release_scenario"):
                raise Exception("Missing execution privileges")
            scenario = args["scenario"]
            sc = Scenario.objects.using(DEFAULT_DB_ALIAS).get(name=scenario)
            sc.description = args.get("description", None)
            sc.save(update_fields=["description"], using=DEFAULT_DB_ALIAS)
            return HttpResponse(content="OK")
        elif "theme" in args:
            # Note: update is immediate and synchronous.
            scenario = args.get("scenario", "").strip()
            theme = args["theme"].strip()
            if theme not in settings.THEMES:
                theme = None
            sc = Scenario.objects.using(DEFAULT_DB_ALIAS).get(name=scenario)
            if theme:
                if request.user.scenario_themes:
                    request.user.scenario_themes[scenario] = theme
                else:
                    request.user.scenario_themes = {scenario: theme}
            else:
                del request.user.scenario_themes[scenario]
            request.user.save(update_fields=["scenario_themes"], using=DEFAULT_DB_ALIAS)
            return HttpResponse(content="OK")
        else:
            raise Exception("Invalid scenario task")
    # G
    elif action == "createbuckets":
        if not request.user.has_perm("auth.run_db"):
            raise Exception("Missing execution privileges")
        task = Task(
            name="createbuckets", submitted=now, status="Waiting", user=request.user
        )
        arguments = []
        weekstart = args.get("weekstart", None)
        if weekstart:
            arguments.append("--weekstart=%s" % weekstart)
        format_day = args.get("format-day", None)
        if format_day:
            arguments.append('--format-day="%s"' % format_day)
        format_week = args.get("format-week", None)
        if format_week:
            arguments.append('--format-week="%s"' % format_week)
        format_month = args.get("format-month", None)
        if format_month:
            arguments.append('--format-month="%s"' % format_month)
        format_quarter = args.get("format-quarter", None)
        if format_quarter:
            arguments.append('--format-quarter="%s"' % format_quarter)
        format_year = args.get("format-year", None)
        if format_year:
            arguments.append('--format-year="%s"' % format_year)
        if arguments:
            task.arguments = " ".join(arguments)
        task.save(using=request.database)
    elif action == "measure_copy":
        if not request.user.has_perm("auth.run_db"):
            raise Exception("Missing execution privileges")
        task = Task(
            name="measure_copy",
            submitted=now,
            status="Waiting",
            user=request.user,
            arguments="%s %s" % (args["source"], args["destination"]),
        )
        task.save(using=request.database)
    else:
        # Generic task wrapper

        # Find the command and verify we have permissions to run it
        command = None
        for commandname, appname in get_commands().items():
            if commandname == action:
                try:
                    c = getattr(
                        import_module(
                            "%s.management.commands.%s" % (appname, commandname)
                        ),
                        "Command",
                    )
                    if c.index >= 0:
                        if getattr(c, "getHTML", None) and c.getHTML(request):
                            # Command class has getHTML method
                            command = c
                            break
                        else:
                            for p in c.__bases__:
                                # Parent command class has getHTML method
                                if getattr(p, "getHTML", None) and p.getHTML(request):
                                    command = c
                                    break
                            if command:
                                break
                except Exception:
                    pass  # Silently ignore failures
        if not command:
            raise Exception("Invalid task name '%s'" % action)
        # Create a task
        arguments = []
        for arg, val in args.lists():
            if arg != "csrfmiddlewaretoken":
                arguments.append("--%s=%s" % (arg, shlex.quote(",".join(val))))
        task = Task(name=action, submitted=now, status="Waiting", user=request.user)
        if arguments:
            task.arguments = " ".join(arguments)
        task.save(using=request.database)

    # Launch a worker process
    if task:
        launchWorker(database=worker_database)
    return task


@staff_member_required
@never_cache
@csrf_protect
def CancelTask(request, taskid):
    # Allow only post
    if (
        request.method != "POST"
        or request.headers.get("x-requested-with") != "XMLHttpRequest"
    ):
        raise Http404("Only ajax post requests allowed")
    try:
        task = Task.objects.all().using(request.database).get(pk=taskid)
        if task.processid or task.status == "Waiting":
            task.killProcess()
        else:
            return HttpResponseServerError("Task isn't running or waiting to run")
        # Just in case, to cover corner cases
        launchWorker(database=request.database)
        return HttpResponse(content="OK")
    except PermissionError:
        return HttpResponseServerError("No permission to kill this task")
    except Exception as e:
        # We don't know why it failed. We just clean things up.
        task.message = "Canceled process"
        task.processid = None
        task.status = "Canceled"
        task.save(using=request.database)
        return HttpResponse(content="OK")


@staff_member_required
@never_cache
def DownloadLogFile(request, taskid):
    filename = Task.objects.using(request.database).get(id=taskid).logfile
    if (
        not filename
        or (filename.lower().endswith(".dump") and not request.user.is_superuser)
        or not filename.lower().endswith((".log", ".dump"))
    ):
        return HttpResponseNotFound(force_str(_("Error")))
    return sendStaticFile(
        request,
        settings.CCAPPS_LOGDIR,
        filename,
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Disposition": 'inline; filename="%s"' % filename,
        },
    )


@staff_member_required
@never_cache
def DeleteLogFile(request, taskid):
    if request.method != "POST":
        return HttpResponseNotAllowed(
            ["post"], content="Only POST request method is allowed"
        )
    filename = Task.objects.using(request.database).get(id=taskid).logfile
    if (
        not filename
        or (filename.lower().endswith(".dump") and not request.user.is_superuser)
        or not filename.lower().endswith((".log", ".dump"))
    ):
        return HttpResponseNotFound(force_str(_("Error")))
    try:
        os.remove(os.path.join(settings.CCAPPS_LOGDIR, filename))
        Task.objects.using(request.database).filter(id=taskid).update(logfile=None)
        return HttpResponse(content="OK")
    except Exception as e:
        logger.error("Couldn't delete log file: %s" % e)
        return HttpResponseForbidden(force_str(_("Couldn't delete file")))


@staff_member_required
@never_cache
def logfile(request, taskid):
    """
    This view shows the ccAPPS log file of the last planning run in this database.
    """
    filesize = None
    try:
        filename = Task.objects.using(request.database).get(id=taskid).logfile
        if not filename.lower().endswith(".log"):
            return HttpResponseNotFound(force_str(_("Error")))

        f = open(os.path.join(settings.CCAPPS_LOGDIR, filename), "rb")
    except Exception:
        logdata = "File not found"
    else:
        try:
            f.seek(-1, os.SEEK_END)
            filesize = f.tell()
            if filesize >= 50000:
                # Too big to display completely
                f.seek(-50000, os.SEEK_END)
                d = f.read(50000)
                d = d[d.index(b"\n") :]  # Strip the first, incomplete line
                logdata = (
                    force_str(_("Displaying only the last 50K from the log file"))
                    + "...\n\n..."
                    + d.decode("utf8", "ignore")
                )
            else:
                # Displayed completely
                f.seek(0, os.SEEK_SET)
                logdata = f.read(50000).decode("utf8", "ignore")
        finally:
            f.close()

    return render(
        request,
        "execute/logccAPPS.html",
        {
            "title": f"{force_str(capfirst(_('log file')))} {taskid}",
            "logdata": logdata,
            "taskid": taskid,
            "filesize": sizeof_fmt(filesize),
        },
    )


class FileManager:
    """
    Class to upload and download files from a folder.
    The folder code argument indicates which folder to use:
    - 0: file upload folder
    - 1: export subdirectory of the file upload folder
    """

    all_extensions = (
        ".xlsx",
        ".xlsm",
        ".csv",
        ".csv.gz",
        ".cpy",
        ".cpy.gz",
        ".sql",
        ".sql.gz",
    )

    @staticmethod
    def getFolderInfo(request, foldercode):
        if foldercode == "0":
            # File upload folder
            return (
                get_databases()[request.database]["FILEUPLOADFOLDER"],
                FileManager.all_extensions,
            )
        elif foldercode == "1":
            # Export folder
            return (
                os.path.join(
                    get_databases()[request.database]["FILEUPLOADFOLDER"], "export"
                ),
                None,  # No upload here
            )
        else:
            raise Http404("Invalid folder code")

    @staticmethod
    def cleanFolder(foldercode, database=DEFAULT_DB_ALIAS):
        if foldercode == 0:
            folder = get_databases()[database]["FILEUPLOADFOLDER"]
            extensions = FileManager.all_extensions
        elif foldercode == 1:
            folder = os.path.join(
                get_databases()[database]["FILEUPLOADFOLDER"], "export"
            )
            extensions = None
        else:
            raise Exception("Invalid folder code")
        if os.path.isdir(folder):
            for filename in os.listdir(folder):
                if (
                    not extensions or filename.lower().endswith(extensions)
                ) and not filename.lower().endswith(".log"):
                    try:
                        os.remove(os.path.join(folder, filename))
                    except Exception:
                        pass

    @staticmethod
    @csrf_exempt
    @staff_member_required
    @never_cache
    def uploadFiletoFolder(request, foldercode):

        if request.method == "GET":
            # Return a list of available data files
            folder, extensions = FileManager.getFolderInfo(request, foldercode)
            filelist = {}
            if os.path.isdir(get_databases()[request.database]["FILEUPLOADFOLDER"]):
                for filename in os.listdir(folder):
                    clean_filename = re.split(r"/|:|\\", filename)[-1]
                    if (
                        not extensions or clean_filename.lower().endswith(extensions)
                    ) and not clean_filename.lower().endswith(".log"):
                        stat = os.stat(os.path.join(folder, clean_filename))
                        filelist[clean_filename] = {
                            "size_bytes": stat.st_size,
                            "timestamp_utc": strftime(
                                "%Y-%m-%d %H:%M:%S", localtime(stat.st_mtime)
                            ),
                        }

            return JsonResponse(filelist)
        elif request.method in ("POST", "PUT"):
            # Upload a new data file
            if len(list(request.FILES.items())) == 0:
                return HttpResponseNotFound("Missing file selection in request")
            errorcount = 0
            response = HttpResponse()
            folder, extensions = FileManager.getFolderInfo(request, foldercode)

            # Try to create the upload if doesn't exist yet
            if not os.path.isdir(get_databases()[request.database]["FILEUPLOADFOLDER"]):
                try:
                    os.makedirs(get_databases()[request.database]["FILEUPLOADFOLDER"])
                except Exception:
                    errorcount += 1
                    response.write("Upload folder doesn't exist")

            if not errorcount:
                # Directory exists and we can upload files into it
                for filename, content in request.FILES.items():
                    try:
                        # Validate file name
                        clean_filename = re.split(r"/|:|\\", filename)[-1]
                        cleanpath = os.path.normpath(
                            os.path.join(folder, clean_filename)
                        )
                        if (
                            not cleanpath.startswith(folder)
                            or not extensions
                            or not clean_filename.lower().endswith(extensions)
                        ):
                            logger.error(
                                "Failed file upload: incorrect file name '%s'"
                                % filename
                            )
                            response.write(
                                "%s: <strong>Error</strong> %s<br>\n"
                                % (
                                    escape(clean_filename),
                                    _("Filename extension must be among %(ext)s")
                                    % {"ext": ", ".join(extensions)},
                                )
                            )
                            errorcount += 1
                            continue

                        # Write to a file
                        with open(cleanpath, "wb") as thetarget:
                            for chunk in content.chunks():
                                thetarget.write(chunk)

                        response.write(
                            force_str(
                                "%s: <strong>%s</strong><br>"
                                % (escape(clean_filename), _("OK"))
                            )
                        )
                    except Exception as e:
                        logger.error("Failed file upload: %s" % e)
                        response.write(
                            "%s: <strong>Error</strong> %s<br>"
                            % (escape(clean_filename), _("Upload failed"))
                        )
                        errorcount += 1
            if errorcount:
                response.status_code = 400
                response.reason_phrase = (
                    "%s files failed to upload correctly" % errorcount
                )
            return response
        else:
            return HttpResponseNotAllowed(
                ["post", "put", "get"],
                content="Only GET, PUT and POST request methods are allowed",
            )

    @staticmethod
    @csrf_exempt
    @staff_member_required
    @never_cache
    def deleteFilefromFolder(request, foldercode, files):
        if request.method != "DELETE":
            return HttpResponseNotAllowed(
                ["delete"], content="Only DELETE request method is allowed"
            )
        folder, extensions = FileManager.getFolderInfo(request, foldercode)
        if extensions is None:
            extensions = FileManager.all_extensions

        fileerrors = force_str(_("Error deleting file"))
        errorcount = 0
        filelist = list()
        if files == "AllFiles":
            if os.path.isdir(folder):
                for filename in os.listdir(folder):
                    clean_filename = re.split(r"/|:|\\", filename)[-1]
                    if clean_filename.lower().endswith(
                        extensions
                    ) and not clean_filename.lower().endswith(".log"):
                        filelist.append(clean_filename)
        else:
            clean_filename = re.split(r"/|:|\\", files)[-1]
            if os.path.isdir(folder):
                filelist.append(clean_filename)

        for clean_filename in filelist:
            try:
                cleanpath = os.path.normpath(os.path.join(folder, clean_filename))
                if cleanpath.startswith(folder):
                    os.remove(cleanpath)
            except FileNotFoundError:
                # No error message needed
                pass
            except Exception as e:
                logger.error("Failed file deletion: %s" % e)
                errorcount += 1
                fileerrors = fileerrors + " / " + escape(clean_filename)

        if errorcount > 0:
            return HttpResponseServerError(fileerrors)
        else:
            return HttpResponse(content="OK")

    @staticmethod
    @csrf_exempt
    @staff_member_required
    @never_cache
    def downloadFilefromFolder(request, foldercode, filename=None):
        if request.method != "GET":
            return HttpResponseNotAllowed(
                ["get"], content="Only GET request method is allowed"
            )
        folder, extensions = FileManager.getFolderInfo(request, foldercode)
        if not extensions:
            extensions = FileManager.all_extensions
        if filename:
            # Download a single file
            try:
                clean_filename = re.split(r"/|:|\\", filename)[0]
                cleanpath = os.path.normpath(os.path.join(folder, clean_filename))
                if not cleanpath.startswith(folder):
                    logger.warning("Failed file download: %s" % filename)
                    return HttpResponseNotFound(force_str(_("Error")))
                if not os.path.isfile(cleanpath):
                    logger.warning("Failed file download: %s" % filename)
                    return HttpResponseNotFound(force_str(_("Error")))
                return sendStaticFile(
                    request,
                    folder,
                    clean_filename,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Disposition": 'inline; filename="%s"' % clean_filename,
                    },
                )
            except Exception as e:
                logger.error("Failed file download: %s" % e)
                return HttpResponseNotFound(force_str(_("Error")))
        else:
            # Download all files
            b = BytesIO()
            with ZipFile(file=b, mode="w", compression=ZIP_DEFLATED) as zf:
                if os.path.isdir(folder):
                    for filename in os.listdir(folder):
                        fullfilename = os.path.join(folder, filename)
                        if filename.endswith(extensions) and os.access(
                            fullfilename, os.R_OK
                        ):
                            zf.write(
                                filename=fullfilename,
                                arcname=os.path.basename(filename),
                            )
            response = HttpResponse(b.getvalue(), content_type="application/zip")
            response["Content-Disposition"] = 'attachment; filename="ccAPPS.zip"'
            return response


@staff_member_required
@never_cache
def scheduletasks(request):
    if request.headers.get(
        "x-requested-with"
    ) != "XMLHttpRequest" or request.method not in ("POST", "DELETE"):
        return HttpResponseNotAllowed("Only post and delete ajax requests are allowed")
    try:
        data = json.loads(
            request.body.decode(request.encoding or settings.DEFAULT_CHARSET)
        )
        oldname = data.get("oldname", None)
        name = data.get("name", None)
        if not name and not oldname:
            return HttpResponse("Missing name attribute", status=400)
        elif request.method == "POST":
            if not request.user.has_perm("execute.add_scheduledtask"):
                return HttpResponse("Couldn't add or update scheduled task", status=401)
            obj, created = ScheduledTask.objects.using(request.database).get_or_create(
                name=oldname if oldname else name
            )
            if created:
                obj.user = request.user
            elif (
                not request.user.is_superuser
                and obj.user.username != request.user.username
            ):
                return HttpResponse(
                    "This task can only be updated by superusers and %s"
                    % obj.user.username,
                    status=401,
                )
            fld = data.get("email_failure", None)
            if fld is not None:
                obj.email_failure = fld
            fld = data.get("email_success", None)
            if fld is not None:
                obj.email_success = fld
            obj.tz = data.get("timezone", settings.TIME_ZONE)
            fld = data.get("data", None)
            if isinstance(fld, dict):
                obj.data = fld
            if oldname and name and oldname != name:
                # Rename the task
                obj.name = name
                ScheduledTask.objects.using(request.database).filter(
                    name=oldname
                ).delete()
            obj.save(using=request.database)
            scheduler.waitNextEvent(database=request.database)
            obj.adjustForTimezone(GridReport.getTimezoneOffset(request))
            return HttpResponse(
                content=(
                    obj.next_run.strftime("%Y-%m-%d %H:%M:%S") if obj.next_run else ""
                )
            )
        elif request.method == "DELETE":
            if not request.user.has_perm("execute.delete_scheduledtask"):
                return HttpResponse("Couldn't delete scheduled task", status=401)
            else:
                try:
                    obj = (
                        ScheduledTask.objects.using(request.database)
                        .get(name=oldname if oldname else name)
                        .delete()
                    )
                    return HttpResponse(content="OK")
                except ScheduledTask.DoesNotExist:
                    return HttpResponse("Couldn't delete scheduled task", status=400)
    except Exception as e:
        logger.error("Error updating scheduled task: %s" % e)
        return HttpResponseServerError("Error updating scheduled task")


@csrf_exempt
@staff_member_required
@never_cache
def scenario_add(request):
    if request.method not in ("POST",):
        return HttpResponseNotAllowed("Only post requests are allowed")
    if not request.user.has_perm("auth.run_db"):
        return HttpResponse("没有添加场景的权限", status=401)
    try:
        error_code = updateScenarioCount(addition=True)
        if not error_code:
            # 同步更新内存 settings + DB 记录，无需重启
            cnt = Scenario.objects.using(DEFAULT_DB_ALIAS).count()
            name = f"scenario{cnt}"
            Scenario.objects.using(DEFAULT_DB_ALIAS).get_or_create(
                name=name, defaults={"status": "Free"}
            )
            # 复制最后一个场景的配置作为新场景的数据库配置
            last = f"scenario{cnt - 1}" if cnt > 1 else "default"
            cfg = settings.DATABASES[last].copy()
            cfg["NAME"] = re.sub(r"\d+$", str(cnt), cfg["NAME"])
            cfg["CCAPPS_PORT"] = f"0.0.0.0:{8000 + cnt}"
            cfg["FILEUPLOADFOLDER"] = os.path.join(
                os.path.dirname(cfg["FILEUPLOADFOLDER"]),
                name,
            ) if "FILEUPLOADFOLDER" in cfg else ""
            settings.DATABASES[name] = cfg
            connections.databases[name] = cfg
            return HttpResponse("成功添加新场景")
        elif error_code == 2:
            return HttpResponse(
                "已达到最大场景数量", status=401
            )
        elif error_code == 4:
            return HttpResponse("djangosettings.py 文件格式无效", status=401)
        else:
            return HttpResponse("发生未知错误", status=400)
    except Exception as e:
        logger.error("Error adding scenario: %s" % e)
        return HttpResponse(e, status=400)


@csrf_exempt
@staff_member_required
@never_cache
def scenario_delete(request):
    if request.method not in ("POST",):
        return HttpResponseNotAllowed("Only post requests are allowed")
    if not request.user.has_perm("auth.run_db"):
        return HttpResponse("没有删除场景的权限", status=401)
    try:
        cnt = Scenario.objects.using(DEFAULT_DB_ALIAS).count()
        last_name = f"scenario{cnt - 1}"
        error_code = updateScenarioCount(addition=False)
        if not error_code:
            # 同步删除内存 settings + DB 记录
            Scenario.objects.using(DEFAULT_DB_ALIAS).filter(
                name=last_name, status="Free"
            ).delete()
            settings.DATABASES.pop(last_name, None)
            return HttpResponse("成功删除场景")
        elif error_code == 1:
            return HttpResponse(
                "已达到最小场景数量", status=401
            )
        elif error_code == 3:
            return HttpResponse("请先释放最后一个场景再试", status=401)
        elif error_code == 4:
            return HttpResponse("djangosettings.py 文件格式无效", status=401)
        else:
            return HttpResponse("发生未知错误", status=400)
    except Exception as e:
        logger.error("Error deleting scenario: %s" % e)
        return HttpResponse(e, status=400)


def exportWorkbook(request):
    # Create a workbook
    wb = Workbook(write_only=True)
    wb.properties.creator = "ccAPPS %s" % __version__

    # Create a named style for the header row
    headerstyle = NamedStyle(name="headerstyle")
    headerstyle.fill = PatternFill(fill_type="solid", fgColor="70c4f4")
    wb.add_named_style(headerstyle)
    readlonlyheaderstyle = NamedStyle(name="readlonlyheaderstyle")
    readlonlyheaderstyle.fill = PatternFill(fill_type="solid", fgColor="d0ebfb")
    wb.add_named_style(readlonlyheaderstyle)

    # retrieve value of parameter excel_duration_in_days
    excel_duration_in_days = (
        Parameter.getValue("excel_duration_in_days", request.database, "false").lower()
        == "true"
    )

    # Loop over all selected entity types
    exportConfig = {"anonymous": request.POST.get("anonymous", False)}
    ok = False
    for entity_name in request.POST.getlist("entities"):
        try:
            # Initialize
            app_label, model_label = entity_name.split(".")
            model = apps.get_model(app_label, model_label)
            # Verify access rights
            permname = get_permission_codename("change", model._meta)
            if not request.user.has_perm("%s.%s" % (app_label, permname)):
                continue

            # Never export some special administrative models
            if model in EXCLUDE_FROM_BULK_OPERATIONS:
                continue

            # Create sheet
            ok = True
            ws = wb.create_sheet(title=force_str(model._meta.verbose_name))

            # Build a list of fields and properties
            fields = []
            modelfields = []
            header = []
            source = False
            lastmodified = False
            owner = False
            comment = None
            try:
                # The admin model of the class can define some fields to exclude from the export
                exclude = data_site._registry[model].exclude
            except Exception:
                exclude = None
            for i in model._meta.fields:
                if isinstance(i, AutoField) and i.primary_key:
                    # Don't export automatically generated primary keys.
                    # We rely on the natural for restoring the data.
                    continue
                elif i.name in ["lft", "rght", "lvl"]:
                    continue  # Skip some fields of HierarchyModel
                elif i.name == "source":
                    source = i  # Put the source field at the end
                elif i.name == "lastmodified":
                    lastmodified = i  # Put the last-modified field at the very end
                elif not (exclude and i.name in exclude):
                    fields.append(i.column)
                    modelfields.append(i)
                    cell = WriteOnlyCell(ws, value=force_str(i.verbose_name).title())
                    if i.editable:
                        cell.style = "headerstyle"
                        if isinstance(i, ForeignKey):
                            cell.comment = CellComment(
                                force_str(
                                    _("Values in this field must exist in the %s table")
                                    % force_str(i.remote_field.model._meta.verbose_name)
                                ),
                                "Author",
                            )
                        elif i.choices:
                            cell.comment = CellComment(
                                force_str(
                                    _("Accepted values are: %s")
                                    % ", ".join(c[0] for c in i.choices)
                                ),
                                "Author",
                            )
                    else:
                        cell.style = "readlonlyheaderstyle"
                        if not comment:
                            comment = CellComment(
                                force_str(_("Read only")),
                                "Author",
                                height=20,
                                width=80,
                            )
                        cell.comment = comment
                    header.append(cell)
                    if i.name == "owner":
                        owner = True
            if hasattr(model, "propertyFields"):
                if callable(model.propertyFields):
                    props = model.propertyFields(request)
                else:
                    props = model.propertyFields
                for i in props:
                    if i.export:
                        fields.append(i.name)
                        cell = WriteOnlyCell(
                            ws, value=force_str(i.verbose_name).title()
                        )
                        if i.editable:
                            cell.style = "headerstyle"
                            if isinstance(i, ForeignKey):
                                cell.comment = CellComment(
                                    force_str(
                                        _(
                                            "Values in this field must exist in the %s table"
                                        )
                                        % force_str(
                                            i.remote_field.model._meta.verbose_name
                                        )
                                    ),
                                    "Author",
                                )
                        elif i.choices:
                            cell.comment = CellComment(
                                force_str(
                                    _("Accepted values are: %s")
                                    % ", ".join(c[0] for c in i.choices)
                                ),
                                "Author",
                            )
                        else:
                            cell.style = "readlonlyheaderstyle"
                            if not comment:
                                comment = CellComment(
                                    force_str(_("Read only")),
                                    "Author",
                                    height=20,
                                    width=80,
                                )
                            cell.comment = comment
                        header.append(cell)
                        modelfields.append(i)
            if source:
                fields.append("source")
                cell = WriteOnlyCell(ws, value=force_str(_("source")).title())
                cell.style = "headerstyle"
                header.append(cell)
                modelfields.append(source)
            if lastmodified:
                fields.append("lastmodified")
                cell = WriteOnlyCell(ws, value=force_str(_("last modified")).title())
                cell.style = "readlonlyheaderstyle"
                if not comment:
                    comment = CellComment(
                        force_str(_("Read only")), "Author", height=20, width=80
                    )
                cell.comment = comment
                header.append(cell)
                modelfields.append(lastmodified)

            # Write a formatted header row
            ws.append(header)

            # Add an auto-filter to the table
            ws.auto_filter.ref = "A1:%s1048576" % get_column_letter(len(header))

            # Use the default manager
            if issubclass(model, HierarchyModel):
                model.rebuildHierarchy(database=request.database)
                query = (
                    model.objects.all().using(request.database).order_by("lvl", "pk")
                )
            elif owner:
                # First export records with empty owner field
                query = (
                    model.objects.all().using(request.database).order_by("-owner", "pk")
                )
            else:
                query = model.objects.all().using(request.database).order_by("pk")

            # Special annotation of the export query
            if hasattr(model, "export_objects"):
                query = model.export_objects(query, request)

            # Loop over all records
            for rec in query.values_list(*fields):
                cells = []
                fld = 0
                for f in rec:
                    cells.append(
                        _getCellValue(
                            f,
                            field=modelfields[fld],
                            exportConfig=exportConfig,
                            excel_duration_in_days=excel_duration_in_days,
                        )
                    )
                    fld += 1
                ws.append(cells)
        except Exception:
            pass  # Silently ignore the error and move on to the next entity.

    # Not a single entity to export
    if not ok:
        raise Exception(_("Nothing to export"))

    # Write the excel from memory to a string and then to a HTTP response
    output = BytesIO()
    wb.save(output)
    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        content=output.getvalue(),
    )
    response["Content-Disposition"] = 'attachment; filename="ccAPPS.xlsx"'
    response["Cache-Control"] = "no-cache, no-store"
    return response


def importWorkbook(request):
    """
    This method reads a spreadsheet in Office Open XML format (typically with
    the extension .xlsx or .ods).
    Each entity has a tab in the spreadsheet, and the first row contains
    the fields names.
    """
    # Build a list of all contenttypes
    all_models = [
        (ct.model_class(), ct.pk)
        for ct in ContentType.objects.all()
        if ct.model_class()
    ]

    # retrieve value of parameter excel_duration_in_days
    excel_duration_in_days = (
        Parameter.getValue("excel_duration_in_days", request.database, "false").lower()
        == "true"
    )

    try:
        # Find all models in the workbook
        for filename, file in request.FILES.items():
            yield "<strong>" + force_str(
                _("Processing file")
            ) + " " + filename + "</strong><br>"
            if filename.endswith(".xls"):
                yield _(
                    "Files in the old .XLS excel format can't be read.<br>Please convert them to the new .XLSX format."
                )
                continue
            elif (
                file.content_type
                != "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ):
                yield _("Unsupported file format.")
                continue
            try:
                wb = load_workbook(filename=file, data_only=True)
            except Exception:
                yield _("Error reading excel file.")
                continue
            models = []
            for ws_name in wb.sheetnames:
                # Find the model
                model = None
                contenttype_id = None
                for m, ct in all_models:
                    if matchesModelName(ws_name, m):
                        model = m
                        contenttype_id = ct
                        break
                if not model or model in EXCLUDE_FROM_BULK_OPERATIONS:
                    yield '<div class="alert alert-warning">' + force_str(
                        _("Ignoring data in worksheet: %s") % ws_name
                    ) + "</div>"
                elif not request.user.has_perm(
                    "%s.%s"
                    % (
                        model._meta.app_label,
                        get_permission_codename("add", model._meta),
                    )
                ):
                    # Check permissions
                    yield '<div class="alert alert-danger">' + force_str(
                        _("You don't permissions to add: %s") % ws_name
                    ) + "</div>"
                else:
                    deps = set([model])
                    GridReport.dependent_models(model, deps)
                    models.append((ws_name, model, contenttype_id, deps))

            # Sort the list of models, based on dependencies between models
            models = GridReport.sort_models(models)

            # Process all rows in each worksheet
            yield (
                '<div class="table-responsive">'
                '<table class="table table-sm" style="white-space: nowrap;"><tbody>'
            )
            for ws_name, model, contenttype_id, dependencies in models:
                with transaction.atomic(using=request.database):
                    yield '<tr><th colspan="5">%s %s<div class="recordcount"></div></th></tr>' % (
                        capfirst(_("worksheet")),
                        ws_name,
                    )
                    numerrors = 0
                    numwarnings = 0
                    firsterror = True
                    ws = wb[ws_name]
                    for error in parseExcelWorksheet(
                        model,
                        ws,
                        user=request.user,
                        database=request.database,
                        ping=True,
                        excel_duration_in_days=excel_duration_in_days,
                    ):
                        if error[0] == logging.DEBUG:
                            # Yield some result so we can detect disconnect clients and interrupt the upload
                            yield "<tr class='hidden' data-cnt='%s'>" % error[1]
                            continue
                        if firsterror and error[0] in (logging.ERROR, logging.WARNING):
                            yield '<tr><th class="sr-only">%s</th><th>%s</th><th>%s</th><th>%s</th><th>%s%s%s</th></tr>' % (
                                capfirst(_("worksheet")),
                                capfirst(_("row")),
                                capfirst(_("field")),
                                capfirst(_("value")),
                                capfirst(_("error")),
                                " / ",
                                capfirst(_("warning")),
                            )
                            firsterror = False
                        if error[0] == logging.ERROR:
                            yield '<tr><td class="sr-only">%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s: %s</td></tr>' % (
                                ws_name,
                                error[1] if error[1] else "",
                                error[2] if error[2] else "",
                                error[3] if error[3] else "",
                                capfirst(_("error")),
                                error[4],
                            )
                            numerrors += 1
                        elif error[1] == logging.WARNING:
                            yield '<tr><td class="sr-only">%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s: %s</td></tr>' % (
                                ws_name,
                                error[1] if error[1] else "",
                                error[2] if error[2] else "",
                                error[3] if error[3] else "",
                                capfirst(_("warning")),
                                error[4],
                            )
                            numwarnings += 1
                        else:
                            yield '<tr class=%s><td class="sr-only">%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
                                "danger" if numerrors > 0 else "success",
                                ws_name,
                                error[1] if error[1] else "",
                                error[2] if error[2] else "",
                                error[3] if error[3] else "",
                                error[4],
                            )

                    # Excel duration might have changed after reading the parameter tab
                    if str(model._meta) == "common.parameter":
                        # retrieve value of parameter excel_duration_in_days
                        excel_duration_in_days = (
                            Parameter.getValue(
                                "excel_duration_in_days", request.database, "false"
                            ).lower()
                            == "true"
                        )
            yield "</tbody></table></div>"
            yield "<div><strong>%s</strong><br><br></div>" % _("Done")
    except GeneratorExit:
        logger.warning("Connection Aborted")
    except Exception as e:
        yield "Import aborted: %s" % e
        logger.error("Exception importing workbook: %s" % e)


@staff_member_required
@never_cache
def exports(request):
    if request.headers.get(
        "x-requested-with"
    ) != "XMLHttpRequest" or request.method not in ("POST", "DELETE"):
        return HttpResponseNotAllowed("Only post ajax requests are allowed")
    if not request.user.is_superuser:
        return HttpResponseForbidden(force_str(_("Only superusers can do this")))

    try:
        data = json.loads(
            request.body.decode(request.encoding or settings.DEFAULT_CHARSET)
        )
        errors = []
        curname = data.get("currentname", None)
        if data.get("delete", None):
            deleted = (
                DataExport.objects.using(request.database)
                .filter(name=data["delete"])
                .delete()
            )
            if deleted:
                # Remove the data file for security reasons and to save disk space
                try:
                    os.remove(
                        os.path.join(
                            get_databases()[request.database]["FILEUPLOADFOLDER"],
                            "export",
                            data["delete"],
                        )
                    )
                except FileNotFoundError:
                    pass
        elif data.get("name", None) in (None, ".xlsx", ".csv", ".csv.gz"):
            errors.append("Name can't be blank<br>")
        else:
            if not data["name"].endswith((".xlsx", ".csv", ".csv.gz")):
                errors.append("Export must end with .xlsx, .csv or .csv.gz<br>")
            elif (
                data["name"] != curname
                and DataExport.objects.using(request.database)
                .filter(name=data["name"])
                .exists()
            ):
                errors.append("Export with this name already exists<br>")
            elif os.sep in data["name"]:
                errors.append("Export names can't contain %s" % os.sep)
            else:
                t = data.get("type", None)
                if t == "sql":
                    s = data.get("sql", None)
                    if s:
                        if curname:
                            DataExport.objects.using(request.database).filter(
                                name=curname
                            ).update(name=data["name"], sql=s)
                        else:
                            DataExport(name=data["name"], sql=s).save(
                                using=request.database
                            )
                    else:
                        errors.append("Missing sql query<br>")
                elif t == "report":
                    r = data.get("report", None)
                    if not r or r not in (
                        "ccAPPSdb.output.views.resource.OverviewReport",
                        "ccAPPSdb.output.views.demand.OverviewReport",
                        "ccAPPSdb.output.views.buffer.OverviewReport",
                        "ccAPPSdb.output.views.operation.OverviewReport",
                        "ccAPPSdb.output.views.operation.DistributionReport",
                        "ccAPPSdb.output.views.operation.PurchaseReport",
                        "ccAPPSdb.forecast.views.OverviewReport",
                    ):
                        errors.append("Invalid report<br>")
                    else:
                        if curname:
                            DataExport.objects.using(request.database).filter(
                                name=curname
                            ).update(
                                name=data["name"],
                                report=r,
                                sql=None,
                                arguments={
                                    "buckets": data.get("bucket"),
                                    "horizontype": True,
                                    "horizonunit": data.get("horizonbucket", "month"),
                                    "horizonlength": int(data.get("horizon", 6)),
                                },
                            )
                        else:
                            DataExport(
                                name=data["name"],
                                report=r,
                                arguments={
                                    "buckets": data.get("bucket"),
                                    "horizontype": True,
                                    "horizonunit": data.get("horizonbucket", "month"),
                                    "horizonlength": int(data.get("horizon", 6)),
                                },
                            ).save(using=request.database)
                elif t == "customreport":
                    from ccAPPSdb.reportmanager.models import SQLReport

                    r = data.get("report", None)
                    if (
                        r
                        and SQLReport.objects.using(request.database)
                        .filter(id=r)
                        .exists()
                    ):
                        if curname:
                            DataExport.objects.using(request.database).filter(
                                name=curname
                            ).update(
                                name=data["name"],
                                report="ccAPPSdb.reportmanager.models.SQLReport.%s"
                                % r,
                                sql=None,
                            )
                        else:
                            DataExport(
                                name=data["name"],
                                report="ccAPPSdb.reportmanager.models.SQLReport.%s"
                                % r,
                            ).save(using=request.database)
                    else:
                        errors.append("Invalid custom report<br>")
                else:
                    errors.append("Unknown export type<br>")
        if errors:
            return HttpResponseServerError(content="\n".join(errors))
        else:
            return HttpResponse(content="OK")
    except Exception as e:
        logger.error("Error updating export: %s" % e)
        return HttpResponseServerError("Error updating export")


from ccAPPSdb.input.models.operationplan import OperationPlan


def auto_resolve_conflicts(request):
    """AJAX endpoint: set solver=autofix in DB, trigger re-plan, return counts."""
    import json
    from django.http import JsonResponse
    from django.core import management
    from ccAPPSdb.common.models import Parameter

    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    database = request.META.get("ccAPPS_DATABASE", "scenario6")
    try:
        from django.db import connections
        try:
            payload = json.loads(request.body.decode(request.encoding or "utf-8") or "{}")
        except Exception:
            payload = {}
        raw_scope = payload.get("operationplans", [])
        if isinstance(raw_scope, str):
            raw_scope = [raw_scope]
        scope_refs = []
        for ref in raw_scope:
            ref = str(ref or "").strip()
            if ref and ref not in scope_refs and len(scope_refs) < 50:
                scope_refs.append(ref)
        if scope_refs:
            valid_refs = set(
                OperationPlan.objects.using(database)
                .filter(reference__in=scope_refs)
                .values_list("reference", flat=True)
            )
            scope_refs = [ref for ref in scope_refs if ref in valid_refs]

        with connections[database].cursor() as cur:
            cur.execute("SELECT count(*) FROM out_problem")
            before_total = cur.fetchone()[0]

        # Set solver mode in database so the subprocess picks it up
        old_solver = Parameter.getValue("plan.solver", database, "heuristic")
        p, _ = Parameter.objects.using(database).update_or_create(
            pk="plan.solver", defaults={"value": "autofix"})
        p.save(using=database)

        try:
            # Run plan with auto-fix
            env_parts = ["autofix_first=1", "autofix_max_iterations=3"]
            if scope_refs:
                env_parts.append("autofix_scope=%s" % "|".join(scope_refs))
            env = ",".join(env_parts)
            management.call_command(
                "runplan", database=database, constraint="capa,mfg_lt,po_lt",
                plantype=1, env=env, background=True, verbosity=0)
        finally:
            p.value = old_solver
            p.save(using=database)

        with connections[database].cursor() as cur:
            cur.execute("SELECT count(*) FROM out_problem")
            after_total = cur.fetchone()[0]

        return JsonResponse({
            "success": True,
            "before_total": before_total,
            "after_total": after_total,
            "scope_count": len(scope_refs),
            "message": "自动修复重排完成。作用域工单: %d，问题数: %d → %d" % (len(scope_refs), before_total, after_total)
        })
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)


class PlanEdit(GridReport):
    """Consolidated plan review and editing view across all operation plan types."""

    template = "execute/planedit.html"
    title = _("计划编辑器")
    model = OperationPlan
    basequeryset = (
        OperationPlan.objects.all()
        .filter(type__in=("MO", "PO", "DO", "DLVR"))
        .exclude(status="closed")
        .select_related("item", "location", "supplier", "operation")
    )
    editable = True
    multiselect = True
    filterable = False
    frozenColumns = 1
    height = 250
    hasTimeBuckets = False
    default_sort = (0, "asc")

    rows = (
        GridFieldText("reference", width=120),
        GridFieldText("type", width=50, editable=False),
        GridFieldText("item__name", width=150, editable=False),
        GridFieldText("quantity", width=80),
        GridFieldLocalDateTime("startdate", width=140),
        GridFieldLocalDateTime("enddate", width=140),
        GridFieldText("status", width=80, editable=False),
        GridFieldDuration("delay", width=80, editable=False, initially_hidden=True),
        GridFieldText("actions", width=80),
        GridFieldText("demand__name", width=120, editable=False, initially_hidden=True),
        GridFieldText(
            "supplier__name", width=120, editable=False, initially_hidden=True
        ),
        GridFieldText(
            "location__name", width=120, editable=False, initially_hidden=True
        ),
        GridFieldText("remark", width=150),
        GridFieldLocalDateTime("due", width=140, initially_hidden=True),
    )

    @classmethod
    def _generate_gantt_data(cls, request, *args, **kwargs):
        request.prefs = request.user.getPreference(
            cls.getKey(request, *args, **kwargs), database=request.database
        )
        board = request.GET.get("board", "resource")
        compact = request.GET.get("compact", "0").lower() in ("1", "true", "yes")
        itemgroup = request.GET.get("itemgroup")
        try:
            limit = int(request.GET.get("ganttlimit", 10000))
        except (TypeError, ValueError):
            limit = 10000
        limit = min(max(limit, 1), 10000)
        try:
            demand_limit = int(request.GET.get("demandlimit", 5000))
        except (TypeError, ValueError):
            demand_limit = 5000
        demand_limit = min(max(demand_limit, 1), 5000)

        if callable(cls.basequeryset):
            basequery = cls.basequeryset(request, *args, **kwargs)
        else:
            basequery = cls.basequeryset
        query = cls.filter_items(request, basequery, False).using(request.database)
        query = cls._apply_sort(request, query).prefetch_related(
            "resources__resource__owner"
        )
        opplans = list(query[:limit])

        def isoformat(dt):
            return dt.isoformat() if dt else None

        records = []
        records_total = None
        inventory = {}
        pegging_links = []
        filtered_groups = {}
        item_group_keys = []

        demand_groups = {}
        demand_groups_total = 0

        if board == "demand":

            from ccAPPSdb.input.models.demand import Demand

            from ccAPPSdb.input.models.operationplan import OperationPlanMaterial

            mat_base_qs = OperationPlanMaterial.objects.using(request.database).filter(
                flowdate__year__gte=2000
            )
            selected_item = selected_location = None
            if compact:
                item_group_rows = list(
                    mat_base_qs.values("item_id", "location_id")
                    .distinct()
                    .order_by("item_id", "location_id")
                )
                item_group_keys = [
                    "%s @ %s" % (r["item_id"], r["location_id"])
                    for r in item_group_rows
                ]
                selected_group = (
                    itemgroup if itemgroup in item_group_keys else (
                        item_group_keys[0] if item_group_keys else None
                    )
                )
                if selected_group:
                    selected_item, selected_location = selected_group.split(" @ ", 1)
                    mat_base_qs = mat_base_qs.filter(
                        item_id=selected_item,
                        location_id=selected_location,
                    )

            mat_qs = list(
                mat_base_qs.select_related("item", "location").order_by(
                    "flowdate", "-quantity"
                )
            )

            all_op_refs = set(m.operationplan_id for m in mat_qs)
            op_details = {}

            if all_op_refs:
                for op in (
                    OperationPlan.objects.using(request.database)
                    .filter(reference__in=list(all_op_refs))
                    .select_related("item", "location", "supplier", "operation")
                ):
                    op_details[op.reference] = {
                        "reference": op.reference,
                        "type": op.type,
                        "status": op.status or "",
                        "name": op.name or "",
                        "item": op.item_id or "",
                        "item_name": op.item.name if op.item else "",
                        "location": op.location_id or "",
                        "location_name": (
                            op.location.name if op.location else ""
                        ),
                        "quantity": (
                            float(op.quantity) if op.quantity is not None else 0
                        ),
                        "startdate": isoformat(op.startdate),
                        "enddate": isoformat(op.enddate),
                        "supplier_name": (
                            op.supplier.name if op.supplier else ""
                        ),
                        "operation_name": op.operation_id or "",
                        "operation_category": (
                            op.operation.category if op.operation else ""
                        ),
                        "delay": (
                            op.delay.total_seconds() if op.delay else None
                        ),
                        "criticality": (
                            float(op.criticality)
                            if op.criticality is not None
                            else None
                        ),
                        "demand_id": op.demand_id or "",
                        "pegging": op.plan.get("pegging", {})
                        if op.plan
                        else {},
                    }

            demand_objs = {}
            for d in (
                Demand.objects.using(request.database)
                .exclude(status__in=("closed", "canceled"))
                .select_related("item", "location", "customer")
            ):
                demand_objs[d.name] = d

            item_groups = {}
            for m in mat_qs:
                key = "%s @ %s" % (m.item_id, m.location_id)
                if key not in item_groups:
                    item_groups[key] = {"inventory": [], "demands": {}}

                qty = float(m.quantity) if m.quantity is not None else 0
                onhand = float(m.onhand) if m.onhand is not None else 0
                op_ref = m.operationplan_id
                has_date = m.flowdate and m.flowdate.year > 2000

                op_detail = op_details.get(op_ref, {})
                op_type = op_detail.get("type", "")
                op_start = op_detail.get("startdate")
                op_end = op_detail.get("enddate")
                op_status = op_detail.get("status", "")

                linked = []
                if has_date:
                    dname = op_detail.get("demand_id") or ""
                    if dname:
                        linked.append({"demand": dname, "qty": abs(qty)})
                    peg = op_detail.get("pegging", {})
                    for pdname, pqty in peg.items():
                        if pdname != dname:
                            linked.append(
                                {
                                    "demand": pdname,
                                    "qty": float(pqty) if pqty else 0,
                                }
                            )

                pt = {
                    "op_ref": op_ref,
                    "quantity": qty,
                    "onhand": onhand,
                    "date": isoformat(m.flowdate) if m.flowdate else None,
                    "flowdate": isoformat(m.flowdate)
                    if m.flowdate
                    else None,
                    "op_type": op_type,
                    "op_status": op_status,
                    "op_start": op_start,
                    "op_end": op_end,
                    "item_id": m.item_id,
                    "location_id": m.location_id,
                    "linked_demands": linked,
                }
                item_groups[key]["inventory"].append(pt)

            for key, grp in item_groups.items():
                seen_demands = {}
                for pt in grp["inventory"]:
                    for ld in pt["linked_demands"]:
                        dname = ld["demand"]
                        if dname not in seen_demands:
                            seen_demands[dname] = {
                                "supply_events": [],
                                "consumption_events": [],
                            }
                        if pt["quantity"] < 0:
                            seen_demands[dname]["consumption_events"].append(
                                {
                                    "op_ref": pt["op_ref"],
                                    "qty": abs(ld["qty"]),
                                    "date": pt["date"],
                                    "op_type": pt.get("op_type") or "",
                                    "op_status": pt.get("op_status") or "",
                                    "op_start": pt.get("op_start"),
                                    "op_end": pt.get("op_end"),
                                    "op_item": op_details.get(
                                        pt["op_ref"], {}
                                    ).get("item_name", ""),
                                    "operation_name": op_details.get(
                                        pt["op_ref"], {}
                                    ).get("operation_name", ""),
                                    "operation_category": op_details.get(
                                        pt["op_ref"], {}
                                    ).get("operation_category", ""),
                                }
                            )
                        elif pt["quantity"] > 0:
                            seen_demands[dname]["supply_events"].append(
                                {
                                    "op_ref": pt["op_ref"],
                                    "qty": ld["qty"],
                                    "date": pt["date"],
                                    "op_type": pt.get("op_type") or "",
                                    "op_status": pt.get("op_status") or "",
                                    "op_start": pt.get("op_start"),
                                    "op_end": pt.get("op_end"),
                                    "operation_name": op_details.get(
                                        pt["op_ref"], {}
                                    ).get("operation_name", ""),
                                    "operation_category": op_details.get(
                                        pt["op_ref"], {}
                                    ).get("operation_category", ""),
                                }
                            )
                for dname, events in seen_demands.items():
                    d = demand_objs.get(dname)
                    if d:
                        grp["demands"][dname] = {
                            "name": d.name,
                            "due": isoformat(d.due) if d.due else None,
                            "quantity": (
                                float(d.quantity)
                                if d.quantity is not None
                                else 0
                            ),
                            "customer": d.customer_id or "",
                            "item": d.item_id or "",
                            "status": d.status or "",
                            "mo_orders": events["consumption_events"],
                            "supply_events": events["supply_events"],
                        }

            filtered_groups = {}
            for key, grp in item_groups.items():
                has_any = False
                for pt in grp["inventory"]:
                    if pt.get("date") and pt["date"].startswith("20"):
                        has_any = True
                        break
                if not has_any:
                    continue
                opening_inventory = [
                    {
                        "date": pt["date"],
                        "onhand": float(pt["onhand"])
                        if pt["onhand"] is not None
                        else 0,
                    }
                    for pt in grp["inventory"]
                    if pt.get("date") and not pt.get("date", "").startswith("20")
                ]
                grp["opening_inventory"] = opening_inventory
                if compact:
                    grp["demands"] = {}
                    for pt in grp["inventory"]:
                        for field in (
                            "flowdate",
                            "op_status",
                            "op_start",
                            "op_end",
                            "item_id",
                            "location_id",
                            "linked_demands",
                        ):
                            pt.pop(field, None)
                filtered_groups[key] = grp

            if not compact:
                item_group_keys = sorted(filtered_groups.keys())

            all_plan_details = {}
            for p in (
                OperationPlan.objects.using(request.database)
                .filter(type__in=("MO", "WO", "PO", "DO", "DLVR"))
                .exclude(status="closed")
                .select_related("item", "location", "supplier", "operation")
            ):
                all_plan_details[p.reference] = {
                    "reference": p.reference,
                    "type": p.type,
                    "status": p.status or "",
                    "name": p.name or "",
                    "item_name": p.item.name if p.item else "",
                    "location_name": p.location.name if p.location else "",
                    "quantity": (
                        float(p.quantity) if p.quantity is not None else 0
                    ),
                    "startdate": isoformat(p.startdate),
                    "enddate": isoformat(p.enddate),
                    "pegging": p.plan.get("pegging", {})
                    if p.plan
                    else {},
                    "demand_id": p.demand_id or "",
                    "operation_name": p.operation_id or "",
                    "operation_category": (
                        p.operation.category if p.operation else ""
                    ),
                    "delay": p.delay.total_seconds() if p.delay else None,
                    "criticality": (
                        float(p.criticality)
                        if p.criticality is not None
                        else None
                    ),
                }

            demand_to_plans = {}
            for pref, pd in all_plan_details.items():
                for dname in pd["pegging"]:
                    demand_to_plans.setdefault(dname, set()).add(pref)
                if pd["demand_id"]:
                    demand_to_plans.setdefault(
                        pd["demand_id"], set()
                    ).add(pref)

            def collect_supply_chain(demand_name):
                return set(
                    demand_to_plans.get(demand_name, set())
                )

            if not compact:
                for key, grp in filtered_groups.items():
                    all_orders = []
                    seen_orders = set()
                    for dname in grp["demands"]:
                        chain = collect_supply_chain(dname)
                        for pref in chain:
                            if pref in seen_orders:
                                continue
                            seen_orders.add(pref)
                            pd = all_plan_details.get(pref)
                            if pd:
                                all_orders.append(
                                    {
                                        "op_ref": pref,
                                        "op_type": pd["type"],
                                        "op_status": pd["status"],
                                        "op_start": pd["startdate"],
                                        "op_end": pd["enddate"],
                                        "quantity": pd["quantity"],
                                        "item_name": pd["item_name"],
                                        "location_name": pd[
                                            "location_name"
                                        ],
                                        "operation_category": pd.get(
                                            "operation_category", ""
                                        ),
                                        "operation_name": pd.get(
                                            "operation_name", ""
                                        ),
                                        "delay": pd.get("delay"),
                                        "criticality": pd.get(
                                            "criticality"
                                        ),
                                    }
                                )
                    grp["all_orders"] = all_orders

            demand_groups = {}
            demand_groups_total = None
            demand_chain_cache = {}
            demand_names = sorted(demand_objs.keys()) if compact else demand_objs.keys()
            for dname in demand_names:
                d = demand_objs[dname]
                if compact and len(demand_groups) >= demand_limit:
                    break
                chain = demand_chain_cache.setdefault(dname, collect_supply_chain(dname))
                if not chain:
                    continue
                orders = []
                seen = set()
                for pref in chain:
                    if pref in seen:
                        continue
                    seen.add(pref)
                    pd = all_plan_details.get(pref)
                    if pd:
                        peg_qty = (
                            pd["pegging"].get(dname, None)
                            if pd["pegging"]
                            else None
                        )
                        order = {
                            "op_ref": pref,
                            "op_type": pd["type"],
                            "op_status": pd["status"],
                            "op_start": pd["startdate"],
                            "op_end": pd["enddate"],
                            "quantity": (
                                float(peg_qty)
                                if peg_qty is not None
                                else pd["quantity"]
                            ),
                            "operation_category": pd.get(
                                "operation_category", ""
                            ),
                        }
                        if not compact:
                            order.update(
                                {
                                    "item_name": pd["item_name"],
                                    "location_name": pd[
                                        "location_name"
                                    ],
                                    "name": pd["name"],
                                    "operation_name": pd.get(
                                        "operation_name", ""
                                    ),
                                    "delay": pd.get("delay"),
                                    "criticality": pd.get(
                                        "criticality"
                                    ),
                                }
                            )
                        orders.append(order)
                demand_groups[dname] = {
                    "name": d.name,
                    "due": isoformat(d.due) if d.due else None,
                    "quantity": (
                        float(d.quantity)
                        if d.quantity
                        else 0
                    ),
                    "customer": d.customer_id or "",
                    "item": d.item_id or "",
                    "status": d.status or "",
                    "all_orders": orders,
                }

            if compact:
                demand_groups_total = len(demand_groups)
                for dname in demand_names:
                    if dname in demand_groups:
                        continue
                    if demand_chain_cache.setdefault(dname, collect_supply_chain(dname)):
                        demand_groups_total += 1
            else:
                demand_groups_total = len(demand_groups)

        else:
            from ccAPPSdb.input.models.operationplan import (
                OperationPlanResource,
            )
            from django.db.models import Exists, OuterRef, Q

            personnel_loads = OperationPlanResource.objects.using(
                request.database
            ).filter(operationplan_id=OuterRef("operationplan_id")).filter(
                Q(resource__category="人员") | Q(resource__name__startswith="PERSON_")
            )

            opr_qs = (
                OperationPlanResource.objects.using(request.database)
                .filter(
                    operationplan__startdate__isnull=False,
                    operationplan__enddate__isnull=False,
                    operationplan__startdate__year__gte=2000,
                )
                .select_related(
                    "resource__owner",
                    "operationplan__item",
                    "operationplan__location",
                    "operationplan__supplier",
                    "operationplan__demand",
                    "operationplan__operation",
                )
                .annotate(has_personnel=Exists(personnel_loads))
                .exclude(
                    Q(operationplan__type__in=("MO", "WO"))
                    & Q(has_personnel=False)
                )
                .order_by(
                    "resource__owner_id",
                    "operationplan__startdate",
                    "operationplan__reference",
                )
            )
            records_total = opr_qs.count()
            all_oprs = list(opr_qs[:limit])

            from ccAPPSdb.input.models.resource import Resource

            res_ids = {
                opr.resource_id for opr in all_oprs if opr.resource_id
            }
            all_resources = {
                r["name"]: r
                for r in Resource.objects.using(request.database)
                .filter(lft__isnull=False)
                .values("name", "description", "category", "lft", "rght", "owner_id")
            }

            def display_operation(operation):
                """Return the business-facing operation name for a Gantt bar.

                CCPL keeps stable technical keys such as
                ``ccpl-operation-OP-01`` in the operation name.  Its
                description is the name a planner recognizes (eg ``卷绕``).
                Keep the key for APIs, but never make it the primary label.
                """
                if not operation:
                    return ""
                return operation.description or operation.name or ""

            CATEGORY_ORDER = {"设备": 0, "人员": 1, "模具": 2}

            def resource_category(resource):
                if not resource:
                    return "未分配资源"
                cat = (getattr(resource, "category", None) or "").strip()
                if cat in CATEGORY_ORDER:
                    return cat
                # Fallback when category is empty: infer from naming convention.
                rid = getattr(resource, "name", "") or ""
                if rid.startswith("PERSON_"):
                    return "人员"
                if rid.startswith("MOLD_"):
                    return "模具"
                return "设备"

            def display_resource(resource, operation):
                """Readable resource leaf label under its category group."""
                if not resource:
                    return "未分配资源"
                rid = resource.name or ""
                desc = (resource.description or "").strip()
                cat = resource_category(resource)
                if cat == "人员":
                    return desc or rid.replace("PERSON_", "", 1)
                if cat == "模具":
                    code = rid.replace("MOLD_", "", 1) if rid.startswith("MOLD_") else rid
                    return "%s %s" % (code, desc) if desc and desc != code else (desc or code)
                # 设备: keep machine code visible (B01), append model/workshop
                # only when it adds information. CCPL names already embed the
                # equipment label after " | " (eg. OP-03 | 全自动热压机-01),
                # so repeating description in parentheses is redundant.
                if desc and desc != rid and desc not in rid:
                    return "%s（%s）" % (rid, desc)
                return rid or desc

            def display_workorder(opplan):
                """Human-readable work-order label; never expose numeric ref."""
                operation_label = display_operation(opplan.operation)
                # Use a source/business name when one is present.  Generated
                # CCPL MOs merely repeat the technical operation key, in which
                # case item and quantity are the meaningful work-order label.
                # FJSP step plans (J140_O02) keep the bare operation name.
                technical = opplan.operation_id or ""
                if operation_label and operation_label == technical:
                    return technical
                if opplan.name and opplan.name != technical and not opplan.name.startswith("ccpl-operation-"):
                    return opplan.name
                item_label = (
                    (opplan.item.description or opplan.item.name)
                    if opplan.item
                    else ""
                )
                quantity = ""
                if opplan.quantity is not None:
                    quantity = "%g件" % opplan.quantity
                label = " | ".join(
                    x for x in (operation_label, item_label, quantity) if x
                )
                # The reference is still useful to distinguish repeated
                # batches with identical operation/item/quantity. Prefix it
                # as a work-order label instead of exposing a bare ID.
                return "%s | MO-%s" % (label, opplan.reference)

            resource_ancestors = {}
            for rid in res_ids:
                r = all_resources.get(rid)
                if not r:
                    resource_ancestors[rid] = [rid]
                    continue
                ancestors = sorted(
                    [
                        x["name"]
                        for x in all_resources.values()
                        if x["lft"] <= r["lft"]
                        and x["rght"] >= r["rght"]
                    ],
                    key=lambda n: all_resources[n]["lft"],
                )
                resource_ancestors[rid] = ancestors

            for opr in all_oprs:
                opplan = opr.operationplan
                rid = opr.resource_id or ""
                cat = resource_category(opr.resource)
                resource_label = display_resource(opr.resource, opplan.operation)
                records.append(
                    {
                        "operationplan__reference": opplan.reference,
                        "resource": rid,
                        # Group by 设备/人员/模具 so the board shows resource
                        # correspondence instead of a flat mold/item-like list.
                        "hierarchy": [cat, resource_label],
                        "resource_display": "%s / %s" % (cat, resource_label),
                        "workorder_display": display_workorder(opplan),
                        "operation_display": display_operation(opplan.operation),
                        "operation_name": opplan.operation_id or "",
                        "type": opplan.type,
                        "status": opplan.status or "",
                        "operation_category": (
                            opplan.operation.category
                            if opplan.operation
                            else ""
                        ),
                        "delay": (
                            opplan.delay.total_seconds()
                            if opplan.delay
                            else None
                        ),
                        "criticality": (
                            float(opplan.criticality)
                            if opplan.criticality is not None
                            else None
                        ),
                        "quantity": (
                            float(opplan.quantity)
                            if opplan.quantity is not None
                            else None
                        ),
                        "startdate": isoformat(opplan.startdate),
                        "enddate": isoformat(opplan.enddate),
                    }
                )
                if not compact:
                    records[-1].update(
                        {
                            "resource_parent": (
                                opr.resource.owner_id
                                if opr.resource
                                else None
                            ),
                            "demand": opplan.demand_id,
                            "name": opplan.name or "",
                            "item": opplan.item_id,
                            "item_name": (
                                opplan.item.description or opplan.item.name
                                if opplan.item
                                else None
                            ),
                            "location": opplan.location_id or "",
                            "location_name": (
                                opplan.location.name
                                if opplan.location
                                else None
                            ),
                            "supplier_name": (
                                opplan.supplier.name
                                if opplan.supplier
                                else None
                            ),
                            "operation_name": opplan.operation_id or "",
                        }
                    )

            personnel_for_plan = OperationPlanResource.objects.using(
                request.database
            ).filter(operationplan_id=OuterRef("reference")).filter(
                Q(resource__category="人员") | Q(resource__name__startswith="PERSON_")
            )

            # Alternate/routing parents have no loadplans by design; their
            # chosen time_per / step children already appear on 设备/人员/模具
            # rows. Keep them out of「未分配资源」so the board is not dominated
            # by false gaps (e.g. FJSP Jxxx_ROUTING containers).
            unassigned_qs = (
                OperationPlan.objects.using(request.database)
                .filter(
                    type__in=("MO", "WO"),
                    startdate__isnull=False,
                    enddate__isnull=False,
                    startdate__year__gte=2000,
                )
                .exclude(status="closed")
                .annotate(has_personnel=Exists(personnel_for_plan))
                .filter(has_personnel=False)
                .exclude(operation__type__in=("alternate", "routing"))
                .select_related(
                    "item", "location", "supplier", "demand", "operation"
                )
                .order_by("startdate", "reference")
            )
            unassigned_total = unassigned_qs.count()
            records_total = (records_total or 0) + unassigned_total
            remaining = max(limit - len(all_oprs), 0)
            unassigned_label = "未分配资源"
            for opplan in unassigned_qs[:remaining]:
                records.append(
                    {
                        "operationplan__reference": opplan.reference,
                        "resource": "",
                        "hierarchy": [unassigned_label],
                        "resource_display": unassigned_label,
                        "workorder_display": display_workorder(opplan),
                        "operation_display": display_operation(opplan.operation),
                        "operation_name": opplan.operation_id or "",
                        "type": opplan.type,
                        "status": opplan.status or "",
                        "operation_category": (
                            opplan.operation.category if opplan.operation else ""
                        ),
                        "delay": (
                            opplan.delay.total_seconds() if opplan.delay else None
                        ),
                        "criticality": (
                            float(opplan.criticality)
                            if opplan.criticality is not None
                            else None
                        ),
                        "quantity": (
                            float(opplan.quantity)
                            if opplan.quantity is not None
                            else None
                        ),
                        "startdate": isoformat(opplan.startdate),
                        "enddate": isoformat(opplan.enddate),
                    }
                )
                if not compact:
                    records[-1].update(
                        {
                            "resource_parent": None,
                            "demand": opplan.demand_id,
                            "name": opplan.name or "",
                            "item": opplan.item_id,
                            "item_name": (
                                opplan.item.description or opplan.item.name
                                if opplan.item
                                else None
                            ),
                            "location": opplan.location_id or "",
                            "location_name": (
                                opplan.location.name if opplan.location else None
                            ),
                            "supplier_name": (
                                opplan.supplier.name if opplan.supplier else None
                            ),
                            "operation_name": opplan.operation_id or "",
                        }
                    )

            def _gantt_sort_key(row):
                hier = row.get("hierarchy") or [""]
                root = hier[0] or ""
                leaf = hier[1] if len(hier) > 1 else ""
                return (
                    CATEGORY_ORDER.get(root, 9),
                    root,
                    leaf,
                    row.get("startdate") or "",
                    row.get("operationplan__reference") or "",
                )

            records.sort(key=_gantt_sort_key)

        payload = {
            "total": 1,
            "page": 1,
            "records": len(records),
            "records_total": records_total if records_total is not None else len(records),
            "board": board,
            "rows": records,
            "inventory": inventory,
            "pegging": pegging_links,
            "item_groups": filtered_groups,
            "item_group_keys": item_group_keys,
            "demand_groups": demand_groups,
            "demand_groups_total": demand_groups_total,
        }
        yield json.dumps(payload)


class GanttEmbed(PlanEdit):
    """Combined Gantt chart without admin chrome — for embedding in iframes."""

    template = "execute/gantt_embed.html"
    editable = False
    title = "Gantt Chart"


@staff_member_required
def data_management(request):
    """Data import/export management page."""
    from importlib import import_module

    from django.core.management import get_commands

    all_commands = []
    for commandname, appname in get_commands().items():
        try:
            cmd = getattr(
                import_module("%s.management.commands.%s" % (appname, commandname)),
                "Command",
            )
            if getattr(cmd, "index", -1) >= 0 and getattr(cmd, "getHTML", None):
                cmd.name = commandname
                html = cmd.getHTML(request)
                if html:
                    all_commands.append(
                        {
                            "command": cmd,
                            "options": {"collapsed": cmd.name != "runplan"},
                            "html": html,
                        }
                    )
        except Exception as e:
            logger.warning(
                "Couldn't import getHTML method from %s.management.commands.%s: %s"
                % (appname, commandname, e)
            )

    # Exclude commands already shown on the main execute page, plus FAS which is
    # only invoked programmatically (MCP / ccpl adapter), not from this page.
    _exclude = {
        "runplan", "scheduletasks", "scenario_copy", "runproductionplan", "runworker",
        "forward_schedule",
    }
    all_commands = [c for c in all_commands if c["command"].name not in _exclude]

    all_commands.sort(key=lambda x: x["command"].index)
    mid = (len(all_commands) + 1) // 2
    commandlist1 = all_commands[:mid]
    commandlist2 = all_commands[mid:]

    # 将导出Excel和导入Excel固定到右侧前两个位置
    _priority_names = ("exportworkbook", "importworkbook")
    _priority = [c for c in all_commands if c["command"].name in _priority_names]
    commandlist1 = [c for c in commandlist1 if c not in _priority]
    commandlist2 = [c for c in commandlist2 if c not in _priority]
    commandlist2 = _priority + commandlist2
    # 将右侧多余项目移回左侧，保持两栏平衡
    while len(commandlist1) < mid and len(commandlist2) > len(_priority):
        commandlist1.append(commandlist2.pop())

    return render(
        request,
        "execute/data_management.html",
        {"commandlist1": commandlist1, "commandlist2": commandlist2},
    )

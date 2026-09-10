"""agentic 助手的工具集（MVP）。

所有数据工具强制 scenario 入参 → .using(scenario)，禁止裸 .objects。
读工具自动执行；写工具（WRITE_TOOLS）需前端确认后才执行。
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

from django.core.management import call_command

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_CCAPPSCTL = os.path.join(_PROJECT_ROOT, "ccAPPSctl.py")


# ---------- 工具 schema（Anthropic tools 格式）----------
TOOL_SCHEMAS = [
    {
        "name": "query_buffers",
        "description": "查询某scenario的库存缓冲点：item/location/onhand(现有库存)/minimum(最小)/maximum(最大)。",
        "input_schema": {
            "type": "object",
            "properties": {
                "scenario": {"type": "string"},
                "item": {"type": "string", "description": "按物料名过滤(可选)"},
                "location": {"type": "string", "description": "按地点过滤(可选)"},
                "limit": {"type": "integer", "description": "返回行数(默认50，最大200)"},
            },
            "required": ["scenario"],
        },
    },
    {
        "name": "query_demands",
        "description": "查询某scenario的销售订单/需求：name/item/customer/quantity/due/status/priority。",
        "input_schema": {
            "type": "object",
            "properties": {
                "scenario": {"type": "string"},
                "item": {"type": "string", "description": "按物料过滤(可选)"},
                "status": {"type": "string", "description": "按状态过滤(可选)：open/closed/quote"},
                "limit": {"type": "integer", "description": "返回行数(默认50)"},
            },
            "required": ["scenario"],
        },
    },
    {
        "name": "query_problems",
        "description": "查询某scenario的排程问题报告(短缺/延迟/超载等)：name/entity/description/startdate/enddate。",
        "input_schema": {
            "type": "object",
            "properties": {
                "scenario": {"type": "string"},
                "limit": {"type": "integer", "description": "返回行数(默认50)"},
            },
            "required": ["scenario"],
        },
    },
    {
        "name": "run_plan",
        "description": "提交计划引擎(约束排程)任务。写操作：会重算并覆盖该scenario的排程结果，需要用户确认后才执行。提交后立即返回任务ID，不会等待排产完成。",
        "input_schema": {
            "type": "object",
            "properties": {
                "scenario": {"type": "string"},
                "constraint": {"type": "string", "description": "约束(可选)：capa/mfg_lt/po_lt/lt/fence，逗号分隔"},
            },
            "required": ["scenario"],
        },
    },
    {
        "name": "query_task_status",
        "description": "查询已提交排产任务的状态、进度和结果消息。用于检查 run_plan 返回的 task_id。",
        "input_schema": {
            "type": "object",
            "properties": {
                "scenario": {"type": "string"},
                "task_id": {"type": "integer"},
            },
            "required": ["scenario", "task_id"],
        },
    },
    {
        "name": "empty_database",
        "description": "清空某scenario的所有业务数据（高危写操作，需用户确认）。会删除该场景全部数据，不可恢复。",
        "input_schema": {
            "type": "object",
            "properties": {"scenario": {"type": "string"}},
            "required": ["scenario"],
        },
    },
    {
        "name": "load_fixture",
        "description": "加载 Django fixture JSON 文件到某scenario（写操作，覆盖同名主键数据，需用户确认）。fixture_path 是磁盘上 .json 文件的绝对路径。",
        "input_schema": {
            "type": "object",
            "properties": {
                "scenario": {"type": "string"},
                "fixture_path": {"type": "string", "description": "磁盘上 .json 文件的绝对路径，如 /home/c/ccAPPS/flow_line.json"},
            },
            "required": ["scenario", "fixture_path"],
        },
    },
    {
        "name": "run_command",
        "description": "通用元工具：执行任意 ccAPPSctl 管理命令（数据导入导出/计划/备份/场景管理等）。覆盖 data-parser 和 execute skill 的命令。写命令需用户确认。不允许 shell/runserver。",
        "input_schema": {
            "type": "object",
            "properties": {
                "scenario": {"type": "string"},
                "command": {"type": "string", "description": "命令名：importfromfolder/exporttofolder/createfixture/import_ccpl_dataset/loaddata/empty/runplan/simulation/backup/restore/migrate/createmodel/scenario_copy/scenario_release/runworker/scheduletasks"},
                "args": {"type": "array", "items": {"type": "string"}, "description": "位置参数(如 fixture 文件路径)"},
                "options": {"type": "object", "description": "关键字选项(如 constraint/source/target)", "additionalProperties": {"type": "string"}},
            },
            "required": ["scenario", "command"],
        },
    },
    {
        "name": "query_operationplans",
        "description": "查询某scenario的排程工单(operationplan)：reference/status/type/quantity/startdate/enddate/criticality 等。",
        "input_schema": {
            "type": "object",
            "properties": {
                "scenario": {"type": "string"},
                "status": {"type": "string", "description": "按状态过滤(可选)：proposed/approved/confirmed/completed/closed"},
                "limit": {"type": "integer"},
            },
            "required": ["scenario"],
        },
    },
    {
        "name": "query_resources",
        "description": "查询某scenario的产能利用率汇总(resource summary)：resource/available/load/free 等。",
        "input_schema": {
            "type": "object",
            "properties": {
                "scenario": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["scenario"],
        },
    },
    {
        "name": "query_constraints",
        "description": "查询某scenario的约束分析(constraint)：哪个需求被什么卡住。返回 demand/item/entity/description 等。",
        "input_schema": {
            "type": "object",
            "properties": {
                "scenario": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["scenario"],
        },
    },
]

# 需要用户确认的写工具
WRITE_TOOLS = {"run_plan", "empty_database", "load_fixture"}

# run_command 元工具的命令白名单（排除 shell/runserver 等危险或运维命令）
ALLOWED_COMMANDS = {
    "loaddata", "importfromfolder", "exporttofolder", "createfixture",
    "import_ccpl_dataset", "empty", "runplan", "simulation",
    "backup", "restore", "migrate", "createmodel",
    "scenario_copy", "scenario_release", "runworker", "scheduletasks",
}
# run_command 中的写命令（需用户确认）；其余(export/createfixture/backup)自动执行
WRITE_COMMANDS = {
    "loaddata", "importfromfolder", "import_ccpl_dataset", "empty",
    "restore", "migrate", "runplan", "simulation",
    "scenario_copy", "scenario_release", "createmodel",
}


def is_write_tool(name, tool_input):
    """判断工具调用是否为写操作（需用户确认）。"""
    if name in WRITE_TOOLS:
        return True
    if name == "run_command":
        return (tool_input or {}).get("command") in WRITE_COMMANDS
    return False


def _limit(tool_input, default=50, mx=200):
    n = tool_input.get("limit", default)
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, mx))


def _f(v):
    """Decimal/数值转 float（JSON 友好）。"""
    return float(v) if v is not None else None


def _tail(text, limit=4000):
    text = text or ""
    return text[-limit:]


def _run_ccappsctl(command, args=None, options=None, timeout=1800):
    """Run long/write management commands outside the web worker.

    Returning stdout/stderr tails makes failures visible in the assistant tool card
    instead of leaving the UI stuck at "running".
    """
    args = list(args or [])
    options = dict(options or {})
    cmd = [sys.executable, _CCAPPSCTL, command]
    for key, value in options.items():
        opt = "--%s" % key.replace("_", "-")
        if value is True:
            cmd.append(opt)
        elif value is False or value is None:
            continue
        else:
            cmd.append("%s=%s" % (opt, value))
    cmd.extend(args)

    env = os.environ.copy()
    env.setdefault("DJANGO_SETTINGS_MODULE", "ccAPPSdb.settings")
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=_PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "timeout",
            "error": "命令执行超过 %s 秒，已终止" % timeout,
            "command": command,
            "args": args,
            "options": options,
            "stdout_tail": _tail(exc.stdout),
            "stderr_tail": _tail(exc.stderr),
            "duration_seconds": round(time.time() - started, 1),
        }

    result = {
        "status": "done" if proc.returncode == 0 else "failed",
        "returncode": proc.returncode,
        "command": command,
        "args": args,
        "options": options,
        "stdout_tail": _tail(proc.stdout),
        "stderr_tail": _tail(proc.stderr),
        "duration_seconds": round(time.time() - started, 1),
    }
    if proc.returncode != 0:
        result["error"] = "命令返回非零状态: %s" % proc.returncode
    return result


def _t_query_buffers(tool_input, scenario):
    from ccAPPSdb.input.models import Buffer
    qs = Buffer.objects.using(scenario).all()
    if tool_input.get("item"):
        qs = qs.filter(item_id=tool_input["item"])
    if tool_input.get("location"):
        qs = qs.filter(location_id=tool_input["location"])
    total = qs.count()
    rows = []
    for b in qs[:_limit(tool_input)]:
        rows.append({
            "item": b.item_id, "location": b.location_id,
            "onhand": _f(b.onhand), "minimum": _f(b.minimum), "maximum": _f(b.maximum),
        })
    return {"count": total, "returned": len(rows), "rows": rows}


def _t_query_demands(tool_input, scenario):
    from ccAPPSdb.input.models import Demand
    qs = Demand.objects.using(scenario).all()
    if tool_input.get("item"):
        qs = qs.filter(item_id=tool_input["item"])
    if tool_input.get("status"):
        qs = qs.filter(status=tool_input["status"])
    total = qs.count()
    rows = []
    for d in qs[:_limit(tool_input)]:
        rows.append({
            "name": d.name, "item": d.item_id, "customer": d.customer_id,
            "quantity": _f(d.quantity),
            "due": d.due.isoformat() if d.due else None,
            "status": d.status, "priority": d.priority,
        })
    return {"count": total, "returned": len(rows), "rows": rows}


def _t_query_problems(tool_input, scenario):
    from ccAPPSdb.output.models import Problem
    qs = Problem.objects.using(scenario).all().order_by("startdate")
    total = qs.count()
    rows = []
    for p in qs[:_limit(tool_input)]:
        rows.append({
            "name": p.name, "entity": p.entity, "owner": p.owner,
            "description": p.description,
            "startdate": p.startdate.isoformat() if p.startdate else None,
            "enddate": p.enddate.isoformat() if p.enddate else None,
        })
    return {"count": total, "returned": len(rows), "rows": rows}


def _t_run_plan(tool_input, scenario):
    """Queue planning with the standard execute worker, not the chat request."""
    from ccAPPSdb.execute.management.commands.runworker import launchWorker
    from ccAPPSdb.execute.models import Task

    constraint = tool_input.get("constraint") or ""
    arguments = "--plantype=1 --env=loadplan"
    if constraint:
        arguments = "--constraint=%s %s" % (constraint, arguments)

    task = Task(
        name="runplan",
        submitted=datetime.now(),
        status="Waiting",
        arguments=arguments,
        message="Submitted by AI assistant",
    )
    task.save(using=scenario)
    launchWorker(scenario)
    return {
        "task_id": task.id,
        "status": task.status,
        "scenario": scenario,
        "constraint": constraint or None,
        "message": "排产任务已提交到后台队列。请稍后使用 query_task_status 查询进度。",
    }


def _t_query_task_status(tool_input, scenario):
    from ccAPPSdb.execute.models import Task

    task_id = tool_input.get("task_id")
    try:
        task = Task.objects.using(scenario).get(pk=task_id)
    except Task.DoesNotExist:
        return {"error": "任务不存在: %s" % task_id}
    return {
        "task_id": task.id,
        "name": task.name,
        "status": task.status,
        "submitted": task.submitted.isoformat() if task.submitted else None,
        "started": task.started.isoformat() if task.started else None,
        "finished": task.finished.isoformat() if task.finished else None,
        "message": task.message,
    }


def _t_empty_database(tool_input, scenario):
    result = _run_ccappsctl("empty", options={"all": True, "database": scenario})
    result.update({"scenario": scenario, "action": "emptied_all"})
    return result


def _prepare_fixture(fixture_path):
    """Prepare a fixture, auto-filling missing string primary keys when possible."""
    import tempfile
    fixed = 0
    try:
        with open(fixture_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for rec in data:
            if not rec.get("pk"):
                name = (rec.get("fields") or {}).get("name")
                if name:
                    rec["pk"] = name
                    fixed += 1
        if fixed:
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False, encoding="utf-8",
                dir=os.path.dirname(fixture_path) or ".")
            json.dump(data, tmp, ensure_ascii=False)
            tmp.close()
            fixture_path = tmp.name
    except Exception:
        pass  # 解析失败则用原文件，让 loaddata 报真实错误
    return fixture_path, fixed


def _safe_load_fixture(fixture_path, scenario):
    fixture_path, fixed = _prepare_fixture(fixture_path)
    result = _run_ccappsctl(
        "loaddata",
        args=[fixture_path],
        options={"database": scenario},
    )
    result["auto_fixed_pk"] = fixed
    return result


def _t_load_fixture(tool_input, scenario):
    fixture_path = tool_input.get("fixture_path")
    if not fixture_path:
        return {"error": "缺少 fixture_path 参数"}
    result = _safe_load_fixture(fixture_path, scenario)
    result.update({"scenario": scenario, "fixture": fixture_path})
    return result


def _t_run_command(tool_input, scenario):
    command = tool_input.get("command")
    if command not in ALLOWED_COMMANDS:
        return {"error": "不允许的命令: %s（允许: %s）" % (command, sorted(ALLOWED_COMMANDS))}
    args = list(tool_input.get("args") or [])
    options = dict(tool_input.get("options") or {})
    opts = {"database": scenario}
    opts.update(options)
    # loaddata 特殊处理：自动补缺 pk 的记录
    if command == "loaddata" and args:
        fixture_path, fixed = _prepare_fixture(args[0])
        args[0] = fixture_path
        result = _run_ccappsctl(command, args=args, options=opts)
        result.update({"scenario": scenario, "auto_fixed_pk": fixed})
        return result
    # empty 特殊处理：自动加 --all（清空所有表）
    if command == "empty":
        opts["all"] = True
    result = _run_ccappsctl(command, args=args, options=opts)
    result.update({"scenario": scenario})
    return result


def _t_query_operationplans(tool_input, scenario):
    from ccAPPSdb.input.models import OperationPlan
    qs = OperationPlan.objects.using(scenario).all()
    if tool_input.get("status"):
        qs = qs.filter(status=tool_input["status"])
    total = qs.count()
    rows = []
    for o in qs[:_limit(tool_input)]:
        rows.append({
            "reference": getattr(o, "reference", None),
            "status": getattr(o, "status", None),
            "type": getattr(o, "type", None),
            "quantity": _f(getattr(o, "quantity", None)),
            "startdate": o.startdate.isoformat() if getattr(o, "startdate", None) else None,
            "enddate": o.enddate.isoformat() if getattr(o, "enddate", None) else None,
            "criticality": _f(getattr(o, "criticality", None)),
        })
    return {"count": total, "returned": len(rows), "rows": rows}


def _t_query_resources(tool_input, scenario):
    from ccAPPSdb.output.models import ResourceSummary
    qs = ResourceSummary.objects.using(scenario).all()
    total = qs.count()
    rows = []
    for r in qs[:_limit(tool_input)]:
        rows.append({
            "resource": getattr(r, "resource", None),
            "startdate": r.startdate.isoformat() if getattr(r, "startdate", None) else None,
            "available": _f(getattr(r, "available", None)),
            "load": _f(getattr(r, "load", None)),
            "free": _f(getattr(r, "free", None)),
        })
    return {"count": total, "returned": len(rows), "rows": rows}


def _t_query_constraints(tool_input, scenario):
    from ccAPPSdb.output.models import Constraint
    qs = Constraint.objects.using(scenario).all().order_by("startdate")
    total = qs.count()
    rows = []
    for c in qs[:_limit(tool_input)]:
        rows.append({
            "demand": getattr(c, "demand", None) or "",
            "item": getattr(c, "item", None) or "",
            "entity": getattr(c, "entity", None) or "",
            "name": getattr(c, "name", None) or "",
            "description": getattr(c, "description", None) or "",
        })
    return {"count": total, "returned": len(rows), "rows": rows}


def execute(name, tool_input, scenario=None):
    """执行工具，返回 JSON 可序列化的 dict。异常被捕获返回 error。

    scenario 优先用 tool_input 里的（模型可能指定），否则用预解析的。
    """
    tool_input = tool_input or {}
    sc = tool_input.get("scenario") or scenario
    try:
        if name == "query_buffers":
            return _t_query_buffers(tool_input, sc)
        if name == "query_demands":
            return _t_query_demands(tool_input, sc)
        if name == "query_problems":
            return _t_query_problems(tool_input, sc)
        if name == "run_plan":
            return _t_run_plan(tool_input, sc)
        if name == "query_task_status":
            return _t_query_task_status(tool_input, sc)
        if name == "empty_database":
            return _t_empty_database(tool_input, sc)
        if name == "load_fixture":
            return _t_load_fixture(tool_input, sc)
        if name == "run_command":
            return _t_run_command(tool_input, sc)
        if name == "query_operationplans":
            return _t_query_operationplans(tool_input, sc)
        if name == "query_resources":
            return _t_query_resources(tool_input, sc)
        if name == "query_constraints":
            return _t_query_constraints(tool_input, sc)
        return {"error": "未知工具: %s" % name}
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}

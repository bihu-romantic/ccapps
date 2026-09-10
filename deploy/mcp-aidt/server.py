"""ccAPPS MRP/FAS MCP server — scheduling-focused tools.

Exposes two scheduling algorithms (MRP via runplan, FAS via forward_schedule)
plus planning analysis queries (problems, constraints, materials, operation
plans, resources, delays). FastMCP v2, streamable-http transport.

Run:  python server.py   (binds 0.0.0.0:9000, serves /mcp)
"""
import os
import time
from collections import Counter
from fastmcp import FastMCP

from ccapps_client import (
    CcappsClient,
    CcappsError,
    BearerMiddleware,
    parse_jqgrid,
    jqgrid_records,
    parse_list,
)

mcp = FastMCP("ccapps-mrp")
client = CcappsClient.from_env()
DEFAULT = client.default_scenario

_OP_ENDPOINTS = {
    "manufacturing": "api/input/manufacturingorder/",
    "workorder": "api/input/workorder/",
    "purchase": "api/input/purchaseorder/",
    "distribution": "api/input/distributionorder/",
    "delivery": "api/input/deliveryorder/",
}


# ----------------------------- helpers -----------------------------
def _is_delayed(row: dict) -> bool:
    """A row is late when its `delay` is a non-zero duration (string like
    '1 day, 3:00:00' or seconds). null/empty/00:00:00 means on time."""
    delay = row.get("delay")
    if delay in (None, "", "0:00:00", "00:00:00", 0, "0"):
        return False
    return True


def _jqgrid_get(scenario: str, kind: str, extra: dict | None = None):
    """GET problem/ or constraint/ (jqGrid). Returns (rows, total_records)."""
    params = {"format": "json"}
    params.update(extra or {})
    payload = client.request("GET", client.scen(scenario, f"{kind}/"), params=params)
    return parse_jqgrid(payload), jqgrid_records(payload)


def _topn(rows: list, n: int, **filters) -> list:
    """First n rows whose fields case-insensitively match every filter key==value."""
    picked = []
    for r in rows:
        if all(str(r.get(k, "")).lower() == str(v).lower() for k, v in filters.items()):
            picked.append(r)
            if len(picked) >= n:
                break
    return picked


def _simplify(rows: list, keys: tuple) -> list:
    """Project each row onto the subset of keys it actually has."""
    return [{k: r.get(k) for k in keys if k in r} for r in rows]


def _plan_summary(scenario: str) -> dict:
    """Post-plan summary: counts by category + top-5 shortages/overloads/late demands/late MOs."""
    s = scenario or DEFAULT
    out: dict = {}
    probs, cons = [], []
    try:
        probs = parse_jqgrid(client.request("GET", client.scen(s, "problem/"), params={"format": "json", "rows": 1000}))
        out["problem_counts"] = dict(Counter(p.get("name") or "unknown" for p in probs))
        out["problem_total"] = len(probs)
    except CcappsError as e:
        out["problem_counts"] = {"error": str(e)[:200]}
    try:
        cons = parse_jqgrid(client.request("GET", client.scen(s, "constraint/"), params={"format": "json", "rows": 1000}))
        out["constraint_counts"] = dict(Counter(c.get("name") or "unknown" for c in cons))
        out["constraint_total"] = len(cons)
    except CcappsError as e:
        out["constraint_counts"] = {"error": str(e)[:200]}
    try:
        out["top_shortages"] = _simplify(
            _topn(probs, 5, entity="buffer", name="material shortage"),
            ("owner", "name", "description", "startdate", "enddate"),
        )
        out["top_overloads"] = _simplify(
            _topn(probs, 5, entity="resource", name="overload"),
            ("owner", "name", "description", "startdate", "enddate"),
        )
    except Exception:
        pass
    try:
        out["top_late_demands"] = _simplify(
            _topn(cons, 5, name="late"),
            ("demand", "item", "owner", "name", "description", "startdate", "enddate"),
        )
    except Exception:
        pass
    try:
        mo = parse_list(client.request("GET", client.scen(s, "api/input/manufacturingorder/"), params={"format": "json", "rows": 200}))
        out["late_operationplan_count"] = sum(1 for r in mo if _is_delayed(r))
        out["top_late_operationplans"] = _simplify(
            [r for r in mo if _is_delayed(r)][:5],
            ("reference", "operation", "item", "enddate", "delay"),
        )
    except CcappsError:
        pass
    return out


def _run_mrp_blocking(s, constraint, plantype, env, poll_interval, poll_timeout) -> dict:
    """Launch runplan and block until Done/Failed/Canceled."""
    t0 = time.time()
    resp = client.launch_runplan(s, constraint, plantype, env)
    taskid = resp.get("taskid") if isinstance(resp, dict) else None
    if not taskid:
        # Background dedup: "No task was launched" if a Waiting runplan exists -> adopt it.
        all_tasks = client.task_status(s, None) or {}
        for tid, info in all_tasks.items():
            if isinstance(info, dict) and info.get("name") == "runplan" and info.get("status") not in ("Done", "Failed", "Canceled"):
                taskid = tid
                break
        if not taskid:
            return {"status": "no_task", "message": "No MRP task launched and none in-flight", "response": resp}
    final = client.poll_task(s, taskid, interval=poll_interval, timeout=poll_timeout)
    return {
        "taskid": str(taskid),
        "status": final.get("status"),
        "message": final.get("message"),
        "logfile": final.get("logfile"),
        "elapsed_s": round(time.time() - t0, 1),
        "timed_out": bool(final.get("timed_out")),
    }


def _launch_forward_schedule(s, execute=True, start=None) -> dict:
    """POST execute/api/forward_schedule/ (form-encoded). Returns dict (may have taskid).

    runworker parses --execute=true into kwargs['execute']='true' and Django
    call_command converts that for the store_true flag.
    """
    path = client.scen(s, "execute/api/forward_schedule/")
    data = {}
    if execute:
        data["execute"] = "true"
    if start:
        data["start"] = str(start)
    return client.request("POST", path, body=data, form=True)


# ----------------------------- scenarios -----------------------------
@mcp.tool
def list_scenarios() -> dict:
    """List accessible ccAPPS scenarios by probing each (scenario0..scenario8)."""
    out = []
    for i in range(int(os.environ.get("MAX_SCENARIO", "8")) + 1):
        name = "scenario0" if i == 0 else f"scenario{i}"
        try:
            client.request("GET", f"{name}/api/input/item/?format=json", params={"rows": 1})
            out.append(name)
        except CcappsError:
            pass
    return {"scenarios": out, "default": DEFAULT, "count": len(out)}


# ----------------------------- scheduling algorithms -----------------------------
@mcp.tool
def run_mrp(
    scenario: str | None = None,
    constraint: str = "capa,mfg_lt,po_lt",
    plantype: int = 1,
    env: str = "supply",
    poll_interval: float = 2.0,
    poll_timeout: float = 600.0,
) -> dict:
    """Run MRP (default constrained plan) on existing scenario data, then return a plan summary.

    Blocks until Done/Failed/Canceled.
      constraint: comma-joined capa,mfg_lt,po_lt,lt,fence.
      plantype: 1=constrained, 2=unconstrained. env: planning steps, e.g. 'supply'.
      poll_timeout: max seconds to wait; on timeout returns timed_out=True (does NOT raise).

    Returns task status + summary (problem/constraint counts + top shortages/
    overloads/late-demands/late-operationplans).
    """
    s = scenario or DEFAULT
    result = _run_mrp_blocking(s, constraint, plantype, env, poll_interval, poll_timeout)
    if result.get("status") == "Done":
        result["summary"] = _plan_summary(s)
    return {"algorithm": "MRP", "scenario": s, **result}


@mcp.tool
def run_fas(
    scenario: str | None = None,
    mrp_first: bool = True,
    execute: bool = True,
    start: str | None = None,
    constraint: str = "capa,mfg_lt,po_lt",
    plantype: int = 1,
    env: str = "supply",
    poll_interval: float = 2.0,
    poll_timeout: float = 600.0,
) -> dict:
    """Run FAS (forward finite-capacity scheduling) and persist, then return a plan summary.

    FAS does NOT replace MRP: it needs proposed manufacturing orders first, then
    forward-schedules them on their resources. With mrp_first=True (default) it
    runs MRP first, then forward_schedule; execute=True persists the schedule
    (operationplan source='FAS').

    Returns MRP + FAS task status + summary.
    """
    s = scenario or DEFAULT
    out: dict = {"algorithm": "FAS", "scenario": s, "mrp": None, "fas": None}
    if mrp_first:
        out["mrp"] = _run_mrp_blocking(s, constraint, plantype, env, poll_interval, poll_timeout)
        if out["mrp"].get("status") != "Done":
            out["status"] = "mrp_not_done"
            return out
    t0 = time.time()
    resp = _launch_forward_schedule(s, execute=execute, start=start)
    taskid = resp.get("taskid") if isinstance(resp, dict) else None
    if not taskid:
        out["fas"] = {"status": "no_task", "message": "No FAS task launched", "response": resp}
        out["status"] = "fas_no_task"
        return out
    final = client.poll_task(s, taskid, interval=poll_interval, timeout=poll_timeout)
    out["fas"] = {
        "taskid": str(taskid),
        "status": final.get("status"),
        "message": final.get("message"),
        "logfile": final.get("logfile"),
        "elapsed_s": round(time.time() - t0, 1),
        "timed_out": bool(final.get("timed_out")),
    }
    out["status"] = final.get("status")
    if final.get("status") == "Done":
        out["summary"] = _plan_summary(s)
    return out


# ----------------------------- analysis: problems / constraints -----------------------------
@mcp.tool
def get_problems(
    scenario: str | None = None,
    entity: str | None = None,
    name: str | None = None,
    limit: int = 100,
) -> dict:
    """List planning problems. Filter by entity (buffer/resource/demand/operation/forecast)
    and/or name (e.g. 'material shortage','overload','overdue','manufacturing lead time')."""
    s = scenario or DEFAULT
    extra = {"rows": max(limit, 100)}
    if entity:
        extra["entity"] = entity
    if name:
        extra["name"] = name
    rows, records = _jqgrid_get(s, "problem", extra)
    return {"records": records, "problems": rows[:limit]}


@mcp.tool
def get_constraints(
    scenario: str | None = None,
    entity: str | None = None,
    name: str | None = None,
    limit: int = 100,
) -> dict:
    """List planning constraints (why a demand is short/late/overloaded).
    entity: demand/material/capacity/operation. name: late/short/overload/excess/early/unplanned/..."""
    s = scenario or DEFAULT
    extra = {"rows": max(limit, 100)}
    if entity:
        extra["entity"] = entity
    if name:
        extra["name"] = name
    rows, records = _jqgrid_get(s, "constraint", extra)
    return {"records": records, "constraints": rows[:limit]}


# ----------------------------- analysis: materials -----------------------------
@mcp.tool
def get_materials(
    scenario: str | None = None,
    item: str | None = None,
    location: str | None = None,
    shortage_only: bool = False,
    limit: int = 200,
) -> dict:
    """Material view. shortage_only=True returns material-shortage problems
    (entity=buffer, name='material shortage'); otherwise returns planned inventory
    flows (on-hand by item/location from operationplanmaterial)."""
    s = scenario or DEFAULT
    if shortage_only:
        rows, records = _jqgrid_get(s, "problem", {"rows": max(limit, 100), "entity": "buffer", "name": "material shortage"})
        return {"scenario": s, "shortage_count": records, "shortages": rows[:limit]}
    params = {"format": "json", "rows": limit}
    if item:
        params["item"] = item
    if location:
        params["location"] = location
    rows = parse_list(client.request("GET", client.scen(s, "api/input/operationplanmaterial/"), params=params))
    return {"scenario": s, "records": len(rows), "inventory": rows[:limit]}


# ----------------------------- analysis: operation plans -----------------------------
@mcp.tool
def get_operationplans(
    scenario: str | None = None,
    type: str = "manufacturing",
    status: str | None = None,
    delayed_only: bool = False,
    limit: int = 200,
) -> dict:
    """List the schedule (operation plans). type: manufacturing|workorder|purchase|distribution|delivery.
    status: proposed/approved/confirmed/completed/closed. delayed_only: keep only delay>0 rows."""
    s = scenario or DEFAULT
    ep = _OP_ENDPOINTS.get(type, _OP_ENDPOINTS["manufacturing"])
    params = {"format": "json", "rows": limit}
    if status:
        params["status"] = status
    rows = parse_list(client.request("GET", client.scen(s, ep), params=params))
    if delayed_only:
        rows = [r for r in rows if _is_delayed(r)]
    return {"records": len(rows), "type": type, "operationplans": rows[:limit]}


# ----------------------------- analysis: resources -----------------------------
@mcp.tool
def get_resources(
    scenario: str | None = None,
    overloaded_only: bool = False,
    limit: int = 100,
) -> dict:
    """Resource load view. Returns capacity-overload problems (entity=resource,
    name='overload'). When overloaded_only=False, also returns the resource plan
    rows (load/available/free; free<0 means overloaded), best-effort."""
    s = scenario or DEFAULT
    rows, records = _jqgrid_get(s, "problem", {"rows": max(limit, 100), "entity": "resource", "name": "overload"})
    out: dict = {"scenario": s, "overload_count": records, "overloads": rows[:limit]}
    if not overloaded_only:
        try:
            out["resource_plan"] = parse_jqgrid(
                client.request("GET", client.scen(s, "resource/"), params={"format": "json", "rows": limit})
            )[:limit]
        except CcappsError:
            out["resource_plan"] = None
    return out


# ----------------------------- analysis: delays -----------------------------
@mcp.tool
def get_delays(scenario: str | None = None, limit: int = 100) -> dict:
    """Late-delivery analysis: late demands (constraint name='late'), late demands
    from demand master data (delay>0), and late manufacturing orders (delay>0)."""
    s = scenario or DEFAULT
    out: dict = {"scenario": s}
    try:
        rows, records = _jqgrid_get(s, "constraint", {"rows": max(limit, 100), "name": "late"})
        out["late_demand_count"] = records
        out["late_demands"] = rows[:limit]
    except CcappsError:
        out["late_demand_count"] = 0
        out["late_demands"] = []
    try:
        dem = parse_list(client.request("GET", client.scen(s, "api/input/demand/"), params={"format": "json", "rows": limit}))
        late = [d for d in dem if _is_delayed(d)]
        out["late_demand_master_count"] = len(late)
        out["late_demand_master"] = [
            {"name": d.get("name"), "item": d.get("item"), "due": d.get("due"), "delay": d.get("delay")}
            for d in late[:limit]
        ]
    except CcappsError:
        pass
    try:
        mo = parse_list(client.request("GET", client.scen(s, "api/input/manufacturingorder/"), params={"format": "json", "rows": limit}))
        late_mo = [r for r in mo if _is_delayed(r)]
        out["late_operationplan_count"] = len(late_mo)
        out["late_operationplans"] = [
            {"reference": r.get("reference"), "operation": r.get("operation"), "enddate": r.get("enddate"), "delay": r.get("delay")}
            for r in late_mo[:limit]
        ]
    except CcappsError:
        pass
    return out


# ----------------------------- tasks -----------------------------
@mcp.tool
def task_status(scenario: str | None = None, task_id: int | None = None) -> dict:
    """Status of one task (task_id given) or all in-flight tasks (task_id omitted)."""
    s = scenario or DEFAULT
    data = client.task_status(s, task_id)
    if task_id is not None and isinstance(data, dict):
        for k in (str(task_id), task_id):
            if k in data:
                return {"task_id": task_id, "task": data[k]}
    return {"tasks": data}


@mcp.tool
def get_task_log(scenario: str | None = None, task_id: int = 0, tail: int = 200) -> dict:
    """Fetch a task's logfile as text (last `tail` lines)."""
    s = scenario or DEFAULT
    text = client.request("GET", client.scen(s, "execute/api/log/"), params={"id": task_id}, raw=True)
    if isinstance(text, str) and tail > 0:
        text = "\n".join(text.splitlines()[-tail:])
    return {"task_id": task_id, "log": text}


# ----------------------------- escape hatch -----------------------------
@mcp.tool
def ccapps_request(
    method: str,
    path: str,
    params: dict | None = None,
    body: dict | str | None = None,
    raw: bool = False,
) -> dict:
    """Generic passthrough to ANY ccAPPS HTTP endpoint.

    `path` should include the scenario prefix, e.g. 'scenario2/api/input/item/'.
    method: GET|POST|PUT|DELETE. On non-2xx raises with status + body excerpt.
    """
    data = client.request(method, path, params=params, body=body, raw=raw)
    if raw:
        return {"status": "ok", "text": data}
    return {"data": data} if not isinstance(data, dict) else data


# ----------------------------- serve -----------------------------
MCP_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "9000"))

app = mcp.http_app(path="/mcp", transport="streamable-http")
if MCP_TOKEN:
    app = BearerMiddleware(app, MCP_TOKEN)


if __name__ == "__main__":
    import uvicorn
    print(
        f"ccAPPS MRP/FAS MCP serving on http://{MCP_HOST}:{MCP_PORT}/mcp  "
        f"(base={client.base_url}, default={DEFAULT}, auth={'on' if MCP_TOKEN else 'OFF'})"
    )
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)

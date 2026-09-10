"""ccAPPS MCP server — exposes ccAPPS planning/query API as MCP tools.

FastMCP v2, streamable-http transport. Run:
    python server.py   (binds 0.0.0.0:9000, serves /mcp)
"""
import os
import time
from fastmcp import FastMCP

from ccapps_client import (
    CcappsClient,
    CcappsError,
    BearerMiddleware,
    parse_jqgrid,
    jqgrid_records,
    parse_list,
)

mcp = FastMCP("ccapps")
client = CcappsClient.from_env()
DEFAULT = client.default_scenario

_OP_ENDPOINTS = {
    "manufacturing": "api/input/manufacturingorder/",
    "purchase": "api/input/purchaseorder/",
    "distribution": "api/input/distributionorder/",
    "delivery": "api/input/deliveryorder/",
}


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


# ----------------------------- planning -----------------------------
@mcp.tool
def run_plan(
    scenario: str | None = None,
    constraint: str = "capa,mfg_lt,po_lt",
    plantype: int = 1,
    env: str = "supply",
    poll_interval: float = 2.0,
    poll_timeout: float = 600.0,
) -> dict:
    """Replan EXISTING data in a scenario (async runplan). Blocks until Done/Failed/Canceled.

    Args:
        scenario: target scenario (default scenario0). constraint: comma-joined capa,mfg_lt,po_lt,lt,fence.
        plantype: 1=constrained, 2=unconstrained. env: planning steps, e.g. 'supply'.
        poll_timeout: max seconds to wait; on timeout returns timed_out=True (does NOT raise).

    Returns final task status + a small results summary (problem/constraint/late-demand counts).
    """
    s = scenario or DEFAULT
    t0 = time.time()
    resp = client.launch_runplan(s, constraint, plantype, env)
    taskid = resp.get("taskid") if isinstance(resp, dict) else None

    # Background dedup: "No task was launched" if a Waiting runplan exists -> adopt it.
    if not taskid:
        all_tasks = client.task_status(s, None) or {}
        for tid, info in all_tasks.items():
            if isinstance(info, dict) and info.get("name") == "runplan" and info.get("status") not in ("Done", "Failed", "Canceled"):
                taskid = tid
                break
        if not taskid:
            return {"status": "no_task", "message": "No task was launched and none in-flight", "response": resp}

    final = client.poll_task(s, taskid, interval=poll_interval, timeout=poll_timeout)
    summary = _result_summary(s) if final.get("status") == "Done" else {}
    return {
        "taskid": taskid,
        "status": final.get("status"),
        "message": final.get("message"),
        "logfile": final.get("logfile"),
        "elapsed_s": round(time.time() - t0, 1),
        "timed_out": bool(final.get("timed_out")),
        "summary": summary,
    }



# ----------------------------- queries -----------------------------
@mcp.tool
def list_problems(
    scenario: str | None = None,
    name_contains: str | None = None,
    limit: int = 100,
) -> dict:
    """List planning problems (entity/name/owner/description/dates)."""
    s = scenario or DEFAULT
    payload = client.request("GET", client.scen(s, "problem/"), params={"format": "json", "rows": limit})
    rows = parse_jqgrid(payload)
    if name_contains:
        rows = [r for r in rows if name_contains.lower() in str(r.get("name", "")).lower()]
    return {"records": jqgrid_records(payload), "problems": rows[:limit]}


@mcp.tool
def list_constraints(scenario: str | None = None, limit: int = 100) -> dict:
    """List planning constraints (why a demand is short/late)."""
    s = scenario or DEFAULT
    payload = client.request("GET", client.scen(s, "constraint/"), params={"format": "json", "rows": limit})
    rows = parse_jqgrid(payload)
    return {"records": jqgrid_records(payload), "constraints": rows[:limit]}


@mcp.tool
def list_demands(
    scenario: str | None = None,
    late_only: bool = False,
    item: str | None = None,
    limit: int = 200,
) -> dict:
    """List sales demands with due/quantity/plannedquantity/delay (delay != null means late)."""
    s = scenario or DEFAULT
    params = {"format": "json", "rows": limit}
    if item:
        params["item"] = item
    payload = client.request("GET", client.scen(s, "api/input/demand/"), params=params)
    rows = parse_list(payload)
    if late_only:
        rows = [d for d in rows if d.get("delay") not in (None, "", "0:00:00")]
    return {"records": len(rows), "demands": rows[:limit]}


# ----------------------------- quotes -----------------------------
def _quote_summary(demand: dict, detail: bool = False) -> dict:
    """Normalize the quote JSON embedded on a ccAPPS demand response."""
    # The standard demand API doesn't expose the internal JSON quote field on
    # all ccAPPS deployments. The caller may provide it after querying the
    # dedicated quote API; otherwise preserve the order-level information.
    quote = demand.get("_quote") or demand.get("quote") or {}
    result = {
        "order_number": demand.get("name"),
        "customer": demand.get("customer"),
        "item": demand.get("item"),
        "quantity": demand.get("quantity"),
        "due": demand.get("due"),
        "order_status": demand.get("status"),
        "quote_enabled": demand.get("quote_enabled", False),
        "quote_status": quote.get("quote_status"),
        "calculation_method": quote.get("calculation_method"),
        "quote_complete": quote.get("quote_complete"),
        "total_cost": quote.get("total_cost"),
        "total_with_tax": quote.get("total_with_tax"),
        "quote_generated_at": quote.get("quote_generated_at"),
    }
    if detail:
        result.update({
            "material_cost": quote.get("material_cost"),
            "resource_cost": quote.get("resource_cost"),
            "operation_cost": quote.get("operation_cost"),
            "overhead_cost": quote.get("overhead_cost"),
            "scrap_cost": quote.get("scrap_cost"),
            "profit_amount": quote.get("profit_amount"),
            "tax_amount": quote.get("tax_amount"),
            "subtotal_cost": quote.get("subtotal_cost"),
            "quote_valid_until": quote.get("quote_valid_until"),
            "quote_warnings": quote.get("quote_warnings", []),
            "history_calibration_enabled": quote.get("history_calibration_enabled"),
            "history_sample_count": quote.get("history_sample_count", 0),
            "history_calibration_factor": quote.get("history_calibration_factor"),
            "history_suggested_total": quote.get("history_suggested_total"),
            "cost_snapshot": quote.get("cost_snapshot", {}),
        })
    return result


def _get_demand(scenario: str, order_number: str) -> dict:
    # Prefer the quote API: unlike the standard demand serializer it exposes
    # quote_enabled and all generated quote values.
    try:
        quote_payload = client.request(
            "GET", client.scen(scenario, "api/quote/orders/%s/" % order_number)
        )
        quote_order = quote_payload.get("order") if isinstance(quote_payload, dict) else None
        if isinstance(quote_order, dict):
            demand = dict(quote_order)
            demand["name"] = quote_order.get("order_number")
            demand["status"] = quote_order.get("order_status")
            demand["quote"] = quote_order
            demand["_quote"] = quote_order
            return demand
    except CcappsError:
        pass
    payload = client.request(
        "GET", client.scen(scenario, "api/input/demand/"),
        params={"format": "json", "rows": 1, "name": order_number},
    )
    rows = parse_list(payload)
    for demand in rows:
        if demand.get("name") == order_number:
            return _enrich_quote(scenario, demand)
    # The standard list API doesn't guarantee name filtering on all releases.
    payload = client.request("GET", client.scen(scenario, "api/input/demand/"), params={"format": "json", "rows": 500})
    for demand in parse_list(payload):
        if demand.get("name") == order_number:
            return _enrich_quote(scenario, demand)
    raise CcappsError(404, "quote order not found: %s" % order_number)


def _enrich_quote(scenario: str, demand: dict) -> dict:
    """Load quote data through the public quote endpoint when available."""
    try:
        payload = client.request("GET", client.scen(scenario, "api/quote/orders/%s/" % demand["name"]))
        quote = payload.get("order") if isinstance(payload, dict) else None
        if isinstance(quote, dict):
            demand = dict(demand)
            demand["name"] = quote.get("order_number", demand.get("name"))
            demand["status"] = quote.get("order_status", demand.get("status"))
            demand["quote"] = quote
            demand["_quote"] = quote
    except CcappsError:
        pass
    return demand


@mcp.tool
def list_quote_orders(
    scenario: str | None = None,
    keyword: str | None = None,
    limit: int = 50,
) -> dict:
    """List active quote-enabled orders, ordered by due date, for a customer quote conversation."""
    s = scenario or DEFAULT
    # This endpoint is the authoritative source for quote state. The general
    # demand API deliberately omits the quote JSON on some ccAPPS versions.
    payload = client.request("GET", client.scen(s, "api/quote/orders/"))
    quote_orders = payload.get("orders") if isinstance(payload, dict) else None
    rows = []
    needle = (keyword or "").lower()
    for quote_order in quote_orders or []:
        demand = dict(quote_order)
        demand["name"] = quote_order.get("order_number")
        demand["status"] = quote_order.get("order_status")
        demand["quote"] = quote_order
        demand["_quote"] = quote_order
        if not demand.get("quote_enabled"):
            continue
        if needle and needle not in str(demand.get("name", "")).lower():
            continue
        rows.append(_quote_summary(_enrich_quote(s, demand)))
    rows.sort(key=lambda item: (str(item.get("due") or ""), str(item.get("order_number") or "")))
    return {"scenario": s, "count": len(rows), "orders": rows[:limit]}


@mcp.tool
def get_quote(
    order_number: str,
    scenario: str | None = None,
    detail: bool = False,
) -> dict:
    """Get one order's quote. detail=False is a compact customer-facing quote; true includes cost snapshots."""
    s = scenario or DEFAULT
    return {"scenario": s, "order": _quote_summary(_get_demand(s, order_number), detail=detail)}


@mcp.tool
def get_quote_history_calibration(order_number: str, scenario: str | None = None) -> dict:
    """Get similar confirmed quote samples and their historical calibration impact for an order."""
    s = scenario or DEFAULT
    demand = _get_demand(s, order_number)
    quote = demand.get("_quote") or demand.get("quote") or {}
    # Similar quotes are maintained by the quote service separately from the
    # compact order payload.
    similar = client.request("GET", client.scen(s, "api/quote/similar/%s/" % order_number))
    return {
        "scenario": s,
        "order_number": order_number,
        "history_calibration_enabled": quote.get("history_calibration_enabled"),
        "sample_count": similar.get("sample_count", quote.get("history_sample_count", 0)),
        "calibration_factor": quote.get("history_calibration_factor"),
        "suggested_total": similar.get("suggested_total", quote.get("history_suggested_total")),
        "suggested_adjustment": similar.get("suggested_adjustment", quote.get("history_suggested_adjustment")),
        "samples": similar.get("samples", quote.get("history_samples", [])),
    }


@mcp.tool
def batch_quote(
    scenario: str | None = None,
    confirmed: bool = False,
    poll_timeout: float = 600.0,
) -> dict:
    """Generate or refresh quotes for every quote-enabled order without running MRP.

    The ccAPPS quote step selects its own quote-enabled orders. When the
    scenario parameter `quote.history_calibration` is `true`, each quote is
    calibrated by the existing historical-quote engine. This writes quote
    records, so set confirmed=true only after explicit approval.
    """
    if not confirmed:
        return {
            "status": "confirmation_required",
            "message": "This will refresh quotes for all quote-enabled orders without running MRP. Set confirmed=true after explicit approval.",
        }
    s = scenario or DEFAULT
    calibration_enabled = False
    try:
        parameter = client.request(
            "GET",
            client.scen(s, "api/common/parameter/quote.history_calibration/"),
            params={"format": "json"},
        )
        calibration_enabled = str(parameter.get("value", "false")).lower() == "true"
    except CcappsError:
        pass
    before = client.request("GET", client.scen(s, "api/quote/orders/"))
    before_orders = before.get("orders", []) if isinstance(before, dict) else []
    response = client.launch_runplan(s, "", 1, "quote")
    taskid = response.get("taskid") if isinstance(response, dict) else None
    if not taskid:
        return {"status": "no_task", "scenario": s, "response": response}
    task = client.poll_task(s, taskid, interval=2.0, timeout=poll_timeout)
    if task.get("status") != "Done":
        return {
            "status": task.get("status"), "scenario": s, "taskid": taskid,
            "timed_out": bool(task.get("timed_out")), "message": task.get("message"),
        }
    after = client.request("GET", client.scen(s, "api/quote/orders/"))
    orders = after.get("orders", []) if isinstance(after, dict) else []
    result_orders = []
    for order in orders:
        result = dict(order)
        calibration = {
            "enabled": calibration_enabled,
            "sample_count": 0,
            "suggested_total": None,
            "suggested_adjustment": None,
            "similar_quotes": [],
        }
        if calibration_enabled and order.get("order_number"):
            try:
                similar = client.request(
                    "GET",
                    client.scen(s, "api/quote/similar/%s/" % order["order_number"]),
                )
                if isinstance(similar, dict):
                    calibration.update({
                        "sample_count": similar.get("sample_count", 0),
                        "suggested_total": similar.get("suggested_total"),
                        "suggested_adjustment": similar.get("suggested_adjustment"),
                        "similar_quotes": (similar.get("samples") or [])[:5],
                    })
            except CcappsError:
                pass
        result["history_calibration"] = calibration
        result_orders.append(result)
    return {
        "status": "Done",
        "scenario": s,
        "taskid": taskid,
        "calculation": "quote_only_no_mrp",
        "history_calibration_enabled": calibration_enabled,
        "quote_order_count": len(orders),
        "orders": result_orders,
        "previous_quote_order_count": len(before_orders),
    }


@mcp.tool
def confirm_quote(
    order_number: str,
    scenario: str | None = None,
    confirmed: bool = False,
) -> dict:
    """Mark a generated quote as confirmed. This writes business data; set confirmed=true only after explicit customer confirmation."""
    if not confirmed:
        return {"status": "confirmation_required", "message": "Set confirmed=true only after the customer explicitly confirms submission."}
    s = scenario or DEFAULT
    demand = _get_demand(s, order_number)
    quote = demand.get("quote") or {}
    if not quote:
        raise CcappsError(400, "no generated quote exists for: %s" % order_number)
    # quote_status 必须走 demand grid 的 parseJSONupload(input/views/sales.py):
    # /api/input/demand/ 的 quote 字段只读,直接 PATCH 写不进去。grid 接收
    # [{"id": <demand 主键=name>, "quote_status": <status>}],status ∈ {draft,confirmed,sent,expired,rejected}。
    import httpx
    base = client.base_url.rstrip("/")
    grid_url = f"{base}/{s}/data/input/demand/"
    headers = {"Accept": "application/json"}
    if client.token:
        headers["Authorization"] = f"Bearer {client.token}"
    with httpx.Client(headers=headers, timeout=client.timeout) as session:
        session.get(f"{base}/{s}/api/input/demand/", params={"format": "json", "rows": 1})
        write_headers = {
            "X-CSRFToken": session.cookies.get("csrftoken") or "",
            "Referer": f"{base}/",
        }
        response = session.post(grid_url, json=[{"id": order_number, "quote_status": "confirmed"}], headers=write_headers)
        if response.status_code >= 400:
            raise CcappsError(response.status_code, response.text)
    return {"status": "confirmed", "scenario": s, "order_number": order_number}


@mcp.tool
def analyze_quote_delivery_risk(
    scenario: str | None = None,
    confirmed: bool = False,
    poll_timeout: float = 600.0,
) -> dict:
    """Run MRP plus quote generation and return scenario-wide delivery risks.

    MRP determines its own eligible demand scope from the scenario data.
    Set confirmed=true only after explicit approval.
    """
    if not confirmed:
        return {
            "status": "confirmation_required",
            "message": "This will recalculate the scenario MRP plan and quote. Set confirmed=true only after explicit approval.",
    }
    s = scenario or DEFAULT
    t0 = time.time()
    response = client.launch_runplan(s, "capa,mfg_lt,po_lt", 1, "supply,quote")
    taskid = response.get("taskid") if isinstance(response, dict) else None
    if not taskid:
        return {"status": "no_task", "scenario": s, "response": response}
    task = client.poll_task(s, taskid, interval=2.0, timeout=poll_timeout)
    if task.get("status") != "Done":
        return {
            "status": task.get("status"), "scenario": s,
            "taskid": taskid, "timed_out": bool(task.get("timed_out")), "message": task.get("message"),
        }

    problems_payload = client.request("GET", client.scen(s, "problem/"), params={"format": "json", "rows": 500})
    constraints_payload = client.request("GET", client.scen(s, "constraint/"), params={"format": "json", "rows": 500})
    problems = parse_jqgrid(problems_payload)
    constraints = parse_jqgrid(constraints_payload)
    # Some deployments publish plan risks as constraints rather than problems.
    all_risks = problems + constraints
    capacity = [r for r in all_risks if str(r.get("entity", "")).lower() == "capacity" or "overload" in str(r.get("name", "")).lower()]
    material = [r for r in all_risks if str(r.get("entity", "")).lower() == "material" or "short" in str(r.get("name", "")).lower()]
    return {
        "status": "Done", "scenario": s, "taskid": taskid,
        "elapsed_s": round(time.time() - t0, 1),
        "problem_count": len(problems),
        "constraint_count": len(constraints),
        "material_risks": material,
        "capacity_risks": capacity,
    }


@mcp.tool
def list_operationplans(
    scenario: str | None = None,
    type: str = "manufacturing",
    status: str | None = None,
    limit: int = 200,
) -> dict:
    """List operation plans (the schedule). type: manufacturing|purchase|distribution|delivery."""
    s = scenario or DEFAULT
    ep = _OP_ENDPOINTS.get(type, _OP_ENDPOINTS["manufacturing"])
    params = {"format": "json", "rows": limit}
    if status:
        params["status"] = status
    payload = client.request("GET", client.scen(s, ep), params=params)
    rows = parse_list(payload)
    return {"records": len(rows), "type": type, "operationplans": rows[:limit]}


@mcp.tool
def get_inventory(
    scenario: str | None = None,
    item: str | None = None,
    location: str | None = None,
    limit: int = 200,
) -> dict:
    """Planned inventory snapshot (on-hand flows by item/location)."""
    s = scenario or DEFAULT
    params = {"format": "json", "rows": limit}
    if item:
        params["item"] = item
    if location:
        params["location"] = location
    payload = client.request("GET", client.scen(s, "api/input/operationplanmaterial/"), params=params)
    rows = parse_list(payload)
    return {"records": len(rows), "inventory": rows[:limit]}


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
    """Generic passthrough to ANY ccAPPS HTTP endpoint (covers all other interfaces).

    `path` should include the scenario prefix, e.g. 'scenario5/api/input/item/' or 'scenario0/problem/'.
    method: GET|POST|PUT|DELETE. On non-2xx raises with status + body excerpt.
    """
    data = client.request(method, path, params=params, body=body, raw=raw)
    if raw:
        return {"status": "ok", "text": data}
    return {"data": data} if not isinstance(data, dict) else data


@mcp.tool
def set_parameter(name: str, value: str, scenario: str | None = None) -> dict:
    """Set a scenario parameter used by planning or quotation algorithms.

    Values are stored as strings. This writes business configuration, so call
    it only after the requested parameter and value are explicitly confirmed.
    """
    import httpx

    s = scenario or DEFAULT
    base = client.base_url.rstrip("/")
    list_url = f"{base}/{s}/api/common/parameter/"
    detail_url = f"{list_url}{name}/"
    headers = {"Accept": "application/json"}
    if client.token:
        headers["Authorization"] = f"Bearer {client.token}"
    with httpx.Client(headers=headers, timeout=client.timeout) as session:
        # Retrieve the CSRF cookie required by the DRF write endpoints.
        session.get(list_url, params={"format": "json"})
        write_headers = {
            "X-CSRFToken": session.cookies.get("csrftoken") or "",
            "Referer": f"{base}/",
        }
        exists = session.get(detail_url, params={"format": "json"}).status_code != 404
        if exists:
            response = session.patch(detail_url, json={"value": str(value)}, headers=write_headers)
        else:
            response = session.post(list_url, json={"name": name, "value": str(value)}, headers=write_headers)
        if response.status_code >= 400:
            raise CcappsError(response.status_code, response.text)
        payload = response.json() if "json" in response.headers.get("content-type", "").lower() else {}
    return {"scenario": s, "name": name, "value": payload.get("value", str(value)), "created": not exists}


# ----------------------------- helpers -----------------------------
def _result_summary(scenario: str) -> dict:
    """Lightweight post-plan summary: problem/constraint/late-demand counts."""
    s = scenario or DEFAULT
    out = {}
    try:
        out["problems"] = jqgrid_records(client.request("GET", client.scen(s, "problem/?format=json"), params={"rows": 0}))
    except CcappsError:
        out["problems"] = None
    try:
        out["constraints"] = jqgrid_records(client.request("GET", client.scen(s, "constraint/?format=json"), params={"rows": 0}))
    except CcappsError:
        out["constraints"] = None
    try:
        dem = parse_list(client.request("GET", client.scen(s, "api/input/demand/"), params={"format": "json", "rows": 500}))
        out["late_demands"] = sum(1 for d in dem if d.get("delay") not in (None, "", "0:00:00"))
    except CcappsError:
        out["late_demands"] = None
    return out


# ----------------------------- serve -----------------------------
MCP_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "9000"))

app = mcp.http_app(path="/mcp", transport="streamable-http")
if MCP_TOKEN:
    app = BearerMiddleware(app, MCP_TOKEN)


if __name__ == "__main__":
    import uvicorn
    print(f"ccAPPS MCP serving on http://{MCP_HOST}:{MCP_PORT}/mcp  (base={client.base_url}, default={DEFAULT}, auth={'on' if MCP_TOKEN else 'OFF'})")
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)

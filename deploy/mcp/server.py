"""ccAPPS MCP server — exposes ccAPPS planning/query API as MCP tools.

FastMCP v2, streamable-http transport. Run:
    python server.py   (binds 0.0.0.0:9000, serves /mcp)
"""
import os
import re
import time
import urllib.parse
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
    headers = client._headers()
    with httpx.Client(headers=headers, timeout=client.timeout) as session:
        session.get(f"{base}/{s}/api/input/demand/", params={"format": "json", "rows": 1})
        write_headers = {
            **headers,
            "X-CSRFToken": session.cookies.get("csrftoken") or "",
            "Referer": f"{base}/",
        }
        response = session.post(grid_url, json=[{"id": order_number, "quote_status": "confirmed"}], headers=write_headers)
        if response.status_code >= 400:
            raise CcappsError(response.status_code, response.text)
    return {"status": "confirmed", "scenario": s, "order_number": order_number}


def _parse_delay_days(delay) -> int:
    if delay in (None, "", "0:00:00", 0):
        return 0
    if isinstance(delay, (int, float)):
        return abs(int(delay))
    text = str(delay)
    day_match = re.search(r"(-?\d+)\s*day", text, re.I)
    if day_match:
        return abs(int(day_match.group(1)))
    hm = re.match(r"(\d+):(\d+):", text)
    if hm and int(hm.group(1)) >= 24:
        return int(hm.group(1)) // 24
    return 0


def _collect_quote_html_data(scenario: str, order_number: str | None = None) -> dict:
    listed = list_quote_orders.fn(scenario=scenario, limit=50)
    orders = []
    for row in listed.get("orders") or []:
        name = row.get("order_number")
        if not name:
            continue
        detail = get_quote.fn(order_number=name, scenario=scenario, detail=True)
        orders.append(detail.get("order") or detail)
    delay_map = {}
    try:
        dem = list_demands.fn(scenario=scenario, limit=500)
        for demand in dem.get("demands") or []:
            delay_map[str(demand.get("name"))] = _parse_delay_days(demand.get("delay"))
    except CcappsError:
        pass
    for order in orders:
        order["delay_days"] = delay_map.get(str(order.get("order_number")), 0)
    names = [order.get("order_number") for order in orders if order.get("order_number")]
    chosen = order_number or (names[0] if names else None)
    current = next((order for order in orders if order.get("order_number") == chosen), orders[0] if orders else None)
    hist = None
    if chosen:
        try:
            hist = get_quote_history_calibration.fn(order_number=chosen, scenario=scenario)
        except CcappsError:
            hist = None
    return {
        "scenario": listed.get("scenario") or scenario,
        "orders": orders,
        "order": current,
        "order_names": names,
        "hist": hist,
        "delay": (current or {}).get("delay_days", 0),
    }


@mcp.tool
def generate_quote_html(
    kind: str,
    order_number: str | None = None,
    scenario: str | None = None,
    mcp_url: str | None = None,
) -> dict:
    """Generate one standalone quote HTML file from live inquiry orders.

    kind=batch writes 批量报价.html for all inquiry orders.
    kind=card writes 单订单报价卡.html for one order.
    Save returned html as filename. The page already contains current quote
    numbers and can still refresh via MCP.

    order_number: used by card; default is the first inquiry order.
    mcp_url: MCP endpoint written into the HTML, default public /cc-apps-mcp/mcp.
    """
    from quote_html import DEFAULT_MCP_URL, render_batch, render_card

    kind_norm = (kind or "").strip().lower()
    if kind_norm not in {"batch", "card"}:
        raise CcappsError(400, "kind must be batch or card")
    s = scenario or DEFAULT
    bundle = _collect_quote_html_data(s, order_number)
    if not bundle["orders"]:
        raise CcappsError(404, "no quote-enabled inquiry orders in %s" % s)
    if kind_norm == "card" and not bundle["order"]:
        raise CcappsError(404, "quote order not found: %s" % (order_number or ""))
    payload = dict(bundle)
    payload["mcp_url"] = (mcp_url or DEFAULT_MCP_URL).strip()
    if kind_norm == "batch":
        filename = "批量报价.html"
        html = render_batch(payload)
    else:
        filename = "单订单报价卡.html"
        html = render_card(payload)
    return {
        "status": "ok",
        "scenario": bundle["scenario"],
        "kind": kind_norm,
        "order_count": len(bundle["orders"]),
        "order_number": (bundle["order"] or {}).get("order_number") if kind_norm == "card" else None,
        "filename": filename,
        "html": html,
    }


def _demand_path(scenario: str, order_number: str) -> str:
    return client.scen(
        scenario,
        "api/input/demand/%s/" % urllib.parse.quote(order_number, safe=""),
    )


def _normalize_due(due: str) -> str:
    value = str(due or "").strip()
    if "T" not in value and len(value) >= 10 and value[4] == "-" and value[7] == "-":
        return value[:10] + "T18:00:00"
    return value


def _as_bool(value):
    if value is None or isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "是"}


def _existing_demand_names(scenario: str) -> set[str]:
    payload = client.request(
        "GET",
        client.scen(scenario, "api/input/demand/"),
        params={"format": "json", "rows": 5000},
    )
    return {str(row.get("name")) for row in parse_list(payload) if row.get("name")}


def _row_to_body(row: dict, *, creating: bool) -> tuple[str, dict, bool | None]:
    name = str(row.get("order_number") or row.get("name") or "").strip()
    if not name:
        raise CcappsError(400, "each order requires order_number or name")
    body = {"name": name}
    for src, dst in (
        ("customer", "customer"),
        ("item", "item"),
        ("location", "location"),
        ("status", "status"),
        ("description", "description"),
        ("priority", "priority"),
    ):
        if row.get(src) is not None:
            body[dst] = row[src]
    if row.get("quantity") is not None:
        body["quantity"] = str(row["quantity"])
    if row.get("due") is not None:
        body["due"] = _normalize_due(row["due"])
    quote_enabled = _as_bool(row["quote_enabled"]) if "quote_enabled" in row else None
    if creating:
        missing = [f for f in ("customer", "item", "location", "quantity", "due") if f not in body]
        if missing:
            raise CcappsError(400, "creating %s requires: %s" % (name, ", ".join(missing)))
        body.setdefault("status", "quote")
        body.setdefault("policy", "independent")
    return name, body, quote_enabled


def _set_quote_enabled_many(scenario: str, flags: list[tuple[str, bool]]) -> None:
    if not flags:
        return
    # DemandSerializer does not expose quote_enabled; the sales-order grid does.
    client.request_csrf(
        "POST",
        client.scen(scenario, "data/input/demand/"),
        body=[{"id": name, "quote_enabled": bool(enabled)} for name, enabled in flags],
    )


def _upsert_orders(scenario: str, rows: list[dict]) -> dict:
    if not rows:
        raise CcappsError(400, "orders is empty")
    existing = _existing_demand_names(scenario)
    creates = []
    updates = []
    quote_flags = []
    created_names = []
    updated_names = []
    for row in rows:
        if not isinstance(row, dict):
            raise CcappsError(400, "each order must be an object")
        creating = str(row.get("order_number") or row.get("name") or "").strip() not in existing
        name, body, quote_enabled = _row_to_body(row, creating=creating)
        if creating:
            creates.append(body)
            created_names.append(name)
            enable_quote = True if quote_enabled is None else quote_enabled
        else:
            patch = {k: v for k, v in body.items() if k != "name"}
            if patch:
                updates.append((name, patch))
            updated_names.append(name)
            enable_quote = quote_enabled
        if enable_quote is not None:
            quote_flags.append((name, bool(enable_quote)))
        existing.add(name)
    if creates:
        client.request_csrf("POST", client.scen(scenario, "api/input/demand/"), body=creates)
    for name, patch in updates:
        client.request_csrf("PATCH", _demand_path(scenario, name), body=patch)
    _set_quote_enabled_many(scenario, quote_flags)
    summaries = [_quote_summary(_get_demand(scenario, name), detail=False) for name in created_names + updated_names]
    return {
        "status": "ok",
        "scenario": scenario,
        "created_count": len(created_names),
        "updated_count": len(updated_names),
        "count": len(summaries),
        "created": created_names,
        "updated": updated_names,
        "orders": summaries,
    }


@mcp.tool
def write_order(
    orders: list[dict] | None = None,
    order_number: str | None = None,
    customer: str | None = None,
    item: str | None = None,
    location: str | None = None,
    quantity: float | None = None,
    due: str | None = None,
    status: str | None = None,
    quote_enabled: bool | None = None,
    description: str | None = None,
    priority: int | None = None,
    scenario: str | None = None,
    confirmed: bool = False,
) -> dict:
    """Create or update sales/quote orders in bulk.

    Prefer `orders`: a list of demand objects from an xlsx/import payload.
    Each item accepts: order_number/name, customer, item, location, quantity,
    due, status, quote_enabled, description, priority.
    Required to create: order_number, customer, item, location, quantity, due.
    New orders default quote_enabled=true. A single order can still be passed
    with the old top-level fields. Set confirmed=true after explicit approval.
    """
    if not confirmed:
        return {
            "status": "confirmation_required",
            "message": "This will create or update sales/quote orders. Set confirmed=true after explicit approval.",
        }
    s = scenario or DEFAULT
    rows = list(orders or [])
    if not rows:
        if not (order_number or "").strip():
            raise CcappsError(400, "pass orders=[...] for batch import, or order_number for one order")
        row = {"order_number": order_number}
        if customer is not None:
            row["customer"] = customer
        if item is not None:
            row["item"] = item
        if location is not None:
            row["location"] = location
        if quantity is not None:
            row["quantity"] = quantity
        if due is not None:
            row["due"] = due
        if status is not None:
            row["status"] = status
        if quote_enabled is not None:
            row["quote_enabled"] = quote_enabled
        if description is not None:
            row["description"] = description
        if priority is not None:
            row["priority"] = priority
        rows = [row]
    result = _upsert_orders(s, rows)
    if len(rows) == 1 and not orders:
        name = result["created"][0] if result["created"] else result["updated"][0]
        return {
            "status": "created" if result["created"] else "updated",
            "scenario": s,
            "order_number": name,
            "order": result["orders"][0] if result["orders"] else {},
        }
    return result


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
    headers = client._headers()
    with httpx.Client(headers=headers, timeout=client.timeout) as session:
        # Retrieve the CSRF cookie required by the DRF write endpoints.
        session.get(list_url, params={"format": "json"})
        write_headers = {
            **headers,
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

from starlette.middleware.cors import CORSMiddleware

app = mcp.http_app(path="/mcp", transport="streamable-http")
if MCP_TOKEN:
    app = BearerMiddleware(app, MCP_TOKEN)
app = CORSMiddleware(
    app,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["mcp-session-id", "Mcp-Session-Id"],
)


if __name__ == "__main__":
    import uvicorn
    print(f"ccAPPS MCP serving on http://{MCP_HOST}:{MCP_PORT}/mcp  (base={client.base_url}, default={DEFAULT}, auth={'on' if MCP_TOKEN else 'OFF'})")
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)

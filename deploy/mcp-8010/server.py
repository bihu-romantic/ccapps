"""ccAPPS query MCP for the 8010 app (ccapps-app).

Tools: list_scenarios, list_tables, query_table, ccapps_request.
"""
import os
from urllib.parse import quote

from fastmcp import FastMCP

from ccapps_client import (
    CcappsClient,
    CcappsError,
    BearerMiddleware,
    parse_jqgrid,
    jqgrid_records,
    parse_list,
)

mcp = FastMCP("ccapps-schedule-mcp-8010")
client = CcappsClient.from_env()
DEFAULT = client.default_scenario


def _jqgrid_get(scenario: str, kind: str, extra: dict | None = None):
    params = {"format": "json"}
    params.update(extra or {})
    payload = client.request("GET", client.scen(scenario, f"{kind}/"), params=params)
    return parse_jqgrid(payload), jqgrid_records(payload)


# name -> (kind, path). kind is "rest" (DRF list) or "grid" (jqGrid report).
_TABLES = {
    "item": ("rest", "api/input/item/"),
    "demand": ("rest", "api/input/demand/"),
    "operation": ("rest", "api/input/operation/"),
    "buffer": ("rest", "api/input/buffer/"),
    "resource": ("rest", "api/input/resource/"),
    "location": ("rest", "api/input/location/"),
    "customer": ("rest", "api/input/customer/"),
    "supplier": ("rest", "api/input/supplier/"),
    "calendar": ("rest", "api/input/calendar/"),
    "calendarbucket": ("rest", "api/input/calendarbucket/"),
    "skill": ("rest", "api/input/skill/"),
    "resourceskill": ("rest", "api/input/resourceskill/"),
    "operationmaterial": ("rest", "api/input/operationmaterial/"),
    "operationresource": ("rest", "api/input/operationresource/"),
    "suboperation": ("rest", "api/input/suboperation/"),
    "operationdependency": ("rest", "api/input/operationdependency/"),
    "itemsupplier": ("rest", "api/input/itemsupplier/"),
    "itemdistribution": ("rest", "api/input/itemdistribution/"),
    "setupmatrix": ("rest", "api/input/setupmatrix/"),
    "setuprule": ("rest", "api/input/setuprule/"),
    "manufacturingorder": ("rest", "api/input/manufacturingorder/"),
    "workorder": ("rest", "api/input/workorder/"),
    "purchaseorder": ("rest", "api/input/purchaseorder/"),
    "distributionorder": ("rest", "api/input/distributionorder/"),
    "deliveryorder": ("rest", "api/input/deliveryorder/"),
    "operationplanmaterial": ("rest", "api/input/operationplanmaterial/"),
    "operationplanresource": ("rest", "api/input/operationplanresource/"),
    "parameter": ("rest", "api/common/parameter/"),
    "attribute": ("rest", "api/common/attribute/"),
    "bucket": ("rest", "api/common/bucket/"),
    "bucketdetail": ("rest", "api/common/bucketdetail/"),
    "comment": ("rest", "api/common/comment/"),
    "problem": ("grid", "problem/"),
    "constraint": ("grid", "constraint/"),
    "resourceplan": ("grid", "resource/"),
}


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


@mcp.tool
def list_tables() -> dict:
    """列出可查询的表。返回表名，以及接口类型 rest 或 grid。"""
    tables = [
        {"name": name, "kind": kind}
        for name, (kind, _path) in sorted(_TABLES.items())
    ]
    return {"count": len(tables), "tables": tables}


@mcp.tool
def query_table(
    table: str,
    scenario: str | None = None,
    filters: dict | None = None,
    pk: str | None = None,
    fields: list[str] | None = None,
    limit: int = 50,
) -> dict:
    """按表名查询当前场景的数据。

    table: list_tables 返回的名称，如 item、demand、operation、parameter、problem。
    filters: 字段等值条件，如 {"name": "J001_FG", "location": "工厂"}。
    pk: 按主键取一行。
    fields: 只保留这些字段。limit 最大 200。
    """
    key = (table or "").strip().lower()
    spec = _TABLES.get(key)
    if not spec:
        return {
            "error": f"unknown table '{table}'",
            "tables": sorted(_TABLES),
        }
    kind, path = spec
    s = scenario or DEFAULT
    limit = max(1, min(int(limit or 50), 200))
    params = {"format": "json"}
    for fname, value in (filters or {}).items():
        if value is not None and value != "":
            params[str(fname)] = value

    if pk:
        target = client.scen(s, f"{path.rstrip('/')}/{quote(str(pk), safe='')}/")
        row = client.request("GET", target, params={"format": "json"})
        rows = [row] if isinstance(row, dict) else parse_list(row)
        total = len(rows)
    elif kind == "grid":
        rows, total = _jqgrid_get(s, path.rstrip("/"), {**params, "rows": limit})
    else:
        payload = client.request("GET", client.scen(s, path), params=params)
        rows = parse_list(payload)
        total = len(rows)

    rows = rows[:limit]
    if fields:
        keep = set(fields)
        rows = [{k: v for k, v in row.items() if k in keep} for row in rows if isinstance(row, dict)]
    return {
        "scenario": s,
        "table": key,
        "kind": kind,
        "matched": total,
        "returned": len(rows),
        "rows": rows,
    }



@mcp.tool
def ccapps_request(
    method: str,
    path: str,
    params: dict | None = None,
    body: dict | str | None = None,
    raw: bool = False,
) -> dict:
    """Generic passthrough to ANY ccAPPS HTTP endpoint.

    `path` should include the scenario prefix, e.g. 'scenario1/api/input/item/'.
    method: GET|POST|PUT|DELETE. On non-2xx raises with status + body excerpt.
    """
    data = client.request(method, path, params=params, body=body, raw=raw)
    if raw:
        return {"status": "ok", "text": data}
    return {"data": data} if not isinstance(data, dict) else data


MCP_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "9000"))

app = mcp.http_app(path="/mcp", transport="streamable-http")
if MCP_TOKEN:
    app = BearerMiddleware(app, MCP_TOKEN)


if __name__ == "__main__":
    import uvicorn

    print(
        f"ccapps-schedule-mcp-8010 serving on http://{MCP_HOST}:{MCP_PORT}/mcp  "
        f"(base={client.base_url}, default={DEFAULT}, auth={'on' if MCP_TOKEN else 'OFF'})"
    )
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)

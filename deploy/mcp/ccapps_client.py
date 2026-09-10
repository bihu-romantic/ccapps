"""ccAPPS HTTP client + jqGrid/list parsers + task polling + bearer middleware.

Single shared client used by all MCP tools. Pure HTTP — no DB, no engine.
"""
import os
import time
import json
import httpx


class CcappsError(Exception):
    """Raised when ccAPPS returns a non-2xx response (or a transport error)."""

    def __init__(self, status, body):
        self.status = status
        self.body = body
        excerpt = body[:300] if isinstance(body, str) else str(body)[:300]
        super().__init__(f"ccAPPS HTTP {status}: {excerpt}")


class CcappsClient:
    def __init__(self, base_url, token, default_scenario="scenario0", timeout=60.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.default_scenario = default_scenario
        self.timeout = timeout

    @classmethod
    def from_env(cls):
        return cls(
            base_url=os.environ.get("CCAPPS_BASE_URL", "http://ccapps-app:8001"),
            token=os.environ.get("CCAPPS_API_TOKEN", ""),
            default_scenario=os.environ.get("DEFAULT_SCENARIO", "scenario0"),
            timeout=float(os.environ.get("CCAPPS_TIMEOUT", "60")),
        )

    def scen(self, scenario, suffix):
        s = scenario or self.default_scenario
        return f"{s}/{suffix.lstrip('/')}"

    def request(self, method, path, *, params=None, body=None, raw=False, form=False):
        """Single HTTP chokepoint. Returns parsed JSON, or text if raw/non-json."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        # The internal ccAPPS deployment may not require a token. httpx rejects
        # an empty Bearer header, so only send authentication when configured.
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        kwargs = {"headers": headers, "timeout": self.timeout, "params": params}
        if form:
            kwargs["data"] = body
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            kwargs["json"] = body
            headers["Content-Type"] = "application/json"
        try:
            r = httpx.request(method, url, **kwargs)
        except httpx.HTTPError as e:
            raise CcappsError(0, f"transport error: {e}")
        if r.status_code >= 400:
            raise CcappsError(r.status_code, r.text)
        if raw:
            return r.text
        if "json" in r.headers.get("content-type", "").lower():
            try:
                return r.json()
            except ValueError:
                return r.text
        return r.text

    def launch_runplan(self, scenario, constraint, plantype, env):
        """POST /<s>/execute/api/runplan/ (form-encoded). Returns dict (may have no taskid)."""
        path = self.scen(scenario, "execute/api/runplan/")
        # constraint & env accept multiple form values; comma-strings also work via parseConstraints
        data = {
            "constraint": [c.strip() for c in constraint.split(",") if c.strip()],
            "plantype": str(plantype),
            "env": [e.strip() for e in env.split(",") if e.strip()],
        }
        return self.request("POST", path, body=data, form=True)

    def task_status(self, scenario, task_id=None):
        """GET /<s>/execute/api/status/?id=N (or all in-flight if task_id None)."""
        path = self.scen(scenario, "execute/api/status/")
        params = {"id": task_id} if task_id is not None else None
        return self.request("GET", path, params=params)

    def poll_task(self, scenario, task_id, interval=2.0, timeout=600.0):
        """Poll status until Done/Failed/Canceled. Returns task dict; on timeout
        returns {status, timed_out:True} WITHOUT raising."""
        path = self.scen(scenario, "execute/api/status/")
        deadline = time.time() + timeout
        last = {}
        while time.time() < deadline:
            data = self.request("GET", path, params={"id": task_id})
            last = self._pick_entry(data, task_id)
            if last.get("status") in ("Done", "Failed", "Canceled"):
                return last
            time.sleep(interval)
        return {"id": task_id, "status": last.get("status", "Running"), "timed_out": True}

    @staticmethod
    def _pick_entry(data, task_id):
        """status payload is keyed by string id; unwrap to the single entry."""
        if isinstance(data, dict):
            for k in (str(task_id), task_id):
                if k in data:
                    return data[k] or {}
            if data:
                return next(iter(data.values())) or {}
        return data or {}


def parse_jqgrid(payload):
    """jqGrid shape: {total,page,records,rows:[...]}. Return the rows list."""
    if isinstance(payload, dict):
        return payload.get("rows", []) or []
    return payload or []


def jqgrid_records(payload):
    if isinstance(payload, dict):
        rec = payload.get("records")
        if rec is not None:
            return rec
    return len(parse_jqgrid(payload))


def parse_list(payload):
    """DRF endpoints may return a bare list [...] or {results/rows:[...]}."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("results", "rows"):
            if k in payload and isinstance(payload[k], list):
                return payload[k]
    return []


class BearerMiddleware:
    """ASGI middleware: reject requests whose Authorization != Bearer <token>.
    If token is empty, passes everything through (auth disabled)."""

    def __init__(self, app, token):
        self.app = app
        self.token = (token or "").strip()

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not self.token:
            return await self.app(scope, receive, send)
        auth = ""
        for k, v in scope.get("headers", []):
            if k.decode().lower() == "authorization":
                auth = v.decode()
                break
        if auth == f"Bearer {self.token}":
            return await self.app(scope, receive, send)
        body = json.dumps({"jsonrpc": "2.0", "error": {"code": -32001, "message": "unauthorized"}}).encode()
        await send({"type": "http.response.start", "status": 401,
                    "headers": [[b"content-type", b"application/json"]]})
        await send({"type": "http.response.body", "body": body})

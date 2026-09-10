"""
HTTP 服务接口
=============

基于 Python 标准库实现的轻量 REST 接口，不引入 Web 框架，
以便在边缘网关等最小化环境中直接运行。

对外提供三类接口：

**查询类（GET）**
    ``/api/status``                 系统总体状态
    ``/api/devices``                设备列表与档案
    ``/api/devices/{id}/latest``    最新一帧数据
    ``/api/devices/{id}/condition`` 当前工况与工况库
    ``/api/devices/{id}/health``    最近一次健康评估
    ``/api/cycles``                 最近的决策周期
    ``/api/approvals``              待审批指令
    ``/api/audit``                  审计记录
    ``/api/events``                 事件流
    ``/api/trace/{trace_id}``       按链路号查询全链路留痕

**操作类（POST）**
    ``/api/devices/{id}/cycle``     手动触发一次决策
    ``/api/approvals/{cmd}/approve``  审批通过
    ``/api/approvals/{cmd}/reject``   审批驳回
    ``/api/mode``                   切换控制模式

写操作一律要求携带令牌（``X-Access-Token`` 头），令牌从环境变量读取，
不写入配置文件、不落日志。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from ..core.enums import ControlMode
from ..core.logging_setup import get_logger
from ..engine import ControlEngine

logger = get_logger("api.server")


class ApiRouter:
    """把 URL 路径映射到处理函数。支持 ``{参数}`` 形式的路径变量。"""

    def __init__(self) -> None:
        self._routes: List[Tuple[str, List[str], Callable[..., Any]]] = []

    def add(self, method: str, pattern: str, handler: Callable[..., Any]) -> None:
        self._routes.append((method.upper(), pattern.strip("/").split("/"), handler))

    def match(self, method: str, path: str) -> Tuple[Optional[Callable[..., Any]],
                                                     Dict[str, str]]:
        parts = path.strip("/").split("/")
        for m, pattern, handler in self._routes:
            if m != method.upper() or len(pattern) != len(parts):
                continue
            params: Dict[str, str] = {}
            ok = True
            for p, actual in zip(pattern, parts):
                if p.startswith("{") and p.endswith("}"):
                    params[p[1:-1]] = actual
                elif p != actual:
                    ok = False
                    break
            if ok:
                return handler, params
        return None, {}


class ApiService:
    """接口业务实现。与 HTTP 传输解耦，便于单元测试直接调用。"""

    def __init__(self, engine: ControlEngine):
        self.engine = engine

    # -- 查询 ----------------------------------------------------------
    def status(self, **_: Any) -> Dict[str, Any]:
        return self.engine.status()

    def devices(self, **_: Any) -> Dict[str, Any]:
        return self.engine.registry.to_dict()

    def latest(self, device_id: str = "", **_: Any) -> Dict[str, Any]:
        frame = self.engine.buffer.latest_frame(device_id)
        if frame is None:
            return {"error": "无数据", "device_id": device_id}
        return frame.to_dict()

    def condition(self, device_id: str = "", **_: Any) -> Dict[str, Any]:
        return {
            "current": self.engine.recognizer.current_condition(device_id),
            "library": self.engine.recognizer.library(device_id).to_dict(),
        }

    def health(self, device_id: str = "", **_: Any) -> Dict[str, Any]:
        for cycle in reversed(self.engine.recent_cycles(50)):
            if cycle.get("device_id") == device_id and cycle.get("health"):
                return cycle["health"]
        return {"error": "尚无评估结果", "device_id": device_id}

    def cycles(self, query: Optional[Dict[str, List[str]]] = None,
               **_: Any) -> Dict[str, Any]:
        limit = int((query or {}).get("limit", ["20"])[0])
        return {"items": self.engine.recent_cycles(limit)}

    def approvals(self, **_: Any) -> Dict[str, Any]:
        return {"items": self.engine.pending_approvals()}

    def audit(self, query: Optional[Dict[str, List[str]]] = None,
              **_: Any) -> Dict[str, Any]:
        q = query or {}
        return {"items": self.engine.audit.query(
            action=q.get("action", [""])[0],
            device_id=q.get("device_id", [""])[0],
            limit=int(q.get("limit", ["100"])[0]),
        )}

    def events(self, query: Optional[Dict[str, List[str]]] = None,
               **_: Any) -> Dict[str, Any]:
        q = query or {}
        items = self.engine.bus.history(
            device_id=q.get("device_id", [""])[0],
            limit=int(q.get("limit", ["50"])[0]),
        )
        return {"items": [e.to_dict() for e in items]}

    def trace(self, trace_id: str = "", **_: Any) -> Dict[str, Any]:
        record = self.engine.decision_record(trace_id)
        events = [e.to_dict() for e in self.engine.bus.trace(trace_id)]
        return {"trace_id": trace_id, "record": record, "events": events}

    # -- 操作 ----------------------------------------------------------
    def run_cycle(self, device_id: str = "", **_: Any) -> Dict[str, Any]:
        return self.engine.decision_cycle(device_id).to_dict()

    def approve(self, command_id: str = "", body: Optional[Dict[str, Any]] = None,
                **_: Any) -> Dict[str, Any]:
        body = body or {}
        device_id = str(body.get("device_id", ""))
        approver = str(body.get("approver", "operator"))
        ctx = self.engine.approve(device_id, command_id, approver)
        return ctx.to_dict()

    def reject(self, command_id: str = "", body: Optional[Dict[str, Any]] = None,
               **_: Any) -> Dict[str, Any]:
        body = body or {}
        ok = self.engine.reject(str(body.get("device_id", "")), command_id,
                                str(body.get("approver", "operator")),
                                str(body.get("reason", "人工驳回")))
        return {"ok": ok, "command_id": command_id}

    def set_mode(self, body: Optional[Dict[str, Any]] = None,
                 **_: Any) -> Dict[str, Any]:
        body = body or {}
        mode = ControlMode.from_text(body.get("mode"), None)
        if mode is None:
            return {"error": "非法的控制模式", "allowed":
                    [m.code for m in ControlMode]}
        self.engine.set_mode(mode)
        return {"mode": mode.code, "label": mode.label}


def build_router(service: ApiService) -> ApiRouter:
    router = ApiRouter()
    router.add("GET", "api/status", service.status)
    router.add("GET", "api/devices", service.devices)
    router.add("GET", "api/devices/{device_id}/latest", service.latest)
    router.add("GET", "api/devices/{device_id}/condition", service.condition)
    router.add("GET", "api/devices/{device_id}/health", service.health)
    router.add("GET", "api/cycles", service.cycles)
    router.add("GET", "api/approvals", service.approvals)
    router.add("GET", "api/audit", service.audit)
    router.add("GET", "api/events", service.events)
    router.add("GET", "api/trace/{trace_id}", service.trace)
    router.add("POST", "api/devices/{device_id}/cycle", service.run_cycle)
    router.add("POST", "api/approvals/{command_id}/approve", service.approve)
    router.add("POST", "api/approvals/{command_id}/reject", service.reject)
    router.add("POST", "api/mode", service.set_mode)
    return router


class _Handler(BaseHTTPRequestHandler):
    """HTTP 请求处理。"""

    router: ApiRouter
    access_token: str = ""
    server_version = "MACO/1.0"

    def do_GET(self) -> None:  # noqa: N802 - 标准库要求的命名
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        handler, params = self.router.match(method, parsed.path)
        if handler is None:
            self._respond(404, {"error": "接口不存在", "path": parsed.path})
            return
        # 写操作校验令牌
        if method == "POST" and self.access_token:
            if self.headers.get("X-Access-Token", "") != self.access_token:
                self._respond(403, {"error": "令牌校验失败"})
                return
        body: Dict[str, Any] = {}
        if method == "POST":
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length > 0:
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self._respond(400, {"error": "请求体不是合法 JSON"})
                    return
        try:
            payload = handler(query=parse_qs(parsed.query), body=body, **params)
            self._respond(200, payload)
        except KeyError as exc:
            self._respond(404, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 统一转成 500，不泄露堆栈
            logger.error("接口处理失败 %s: %s", parsed.path, exc)
            self._respond(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _respond(self, code: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:
        # 接管标准库日志，避免把查询串（可能含令牌）打进 stderr
        logger.debug("HTTP %s", fmt % args)


class ApiServer:
    """接口服务器封装。"""

    def __init__(self, engine: ControlEngine, host: str = "127.0.0.1",
                 port: int = 8780, access_token: str = ""):
        self.engine = engine
        self.host = host
        self.port = port
        self.service = ApiService(engine)
        handler = type("_BoundHandler", (_Handler,), {
            "router": build_router(self.service),
            "access_token": access_token,
        })
        self._httpd = ThreadingHTTPServer((host, port), handler)
        self._thread: Optional[threading.Thread] = None

    def start(self, background: bool = True) -> None:
        logger.info("接口服务已启动：http://%s:%d/api/status", self.host, self.port)
        if background:
            self._thread = threading.Thread(target=self._httpd.serve_forever,
                                            name="maco-api", daemon=True)
            self._thread.start()
        else:
            self._httpd.serve_forever()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        logger.info("接口服务已停止")


__all__ = ["ApiServer", "ApiService", "ApiRouter", "build_router"]

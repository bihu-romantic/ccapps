"""Forward /svc/<scenario>/ to that scenario's planning web service."""

import urllib.error
import urllib.request

from django.http import HttpResponse

from ccAPPSdb.common.utils import get_databases


def _service_port(scenario):
    cfg = get_databases().get(scenario) or {}
    server = cfg.get("CCAPPS_PORT") or ""
    if ":" not in server:
        return None
    return server.rsplit(":", 1)[1]


def webservice_proxy(request, scenario, path=""):
    port = _service_port(scenario)
    if not port:
        return HttpResponse(b"unknown scenario", status=404, content_type="text/plain")
    target = f"http://127.0.0.1:{port}/{path}"
    if request.META.get("QUERY_STRING"):
        target = f"{target}?{request.META['QUERY_STRING']}"
    headers = {}
    auth = request.META.get("HTTP_AUTHORIZATION")
    if auth:
        headers["Authorization"] = auth
    if request.content_type:
        headers["Content-Type"] = request.content_type
    body = request.body if request.method not in ("GET", "HEAD") else None
    req = urllib.request.Request(target, data=body, headers=headers, method=request.method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return HttpResponse(
                resp.read(),
                status=resp.status,
                content_type=resp.headers.get("Content-Type", "application/octet-stream"),
            )
    except urllib.error.HTTPError as exc:
        return HttpResponse(
            exc.read(),
            status=exc.code,
            content_type=exc.headers.get("Content-Type", "text/plain"),
        )
    except urllib.error.URLError:
        return HttpResponse(
            b"planning web service is not running",
            status=503,
            content_type="text/plain",
        )

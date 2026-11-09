#!/usr/bin/env python3
"""A small, expiring reverse proxy for the scenario 6 planning board."""

import argparse
import http.client
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


ALLOWED_EXACT = {
    "/scenario6/execute/planningboard/",
    "/scenario6/operationplan/",  # Read-only popup opened from a Gantt bar.
    "/execute/planningboard/",  # Login redirect with the scenario cookie.
    "/data/login/",
    "/data/logout/",
    "/data/jsi18n/",
}
ALLOWED_PREFIXES = ("/static/",)
HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}


class RestrictedProxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream_host = "127.0.0.1"
    upstream_port = 8001
    expires_at = 0

    def allowed(self, path):
        return path in ALLOWED_EXACT or path.startswith(ALLOWED_PREFIXES)

    def reject(self, status, message):
        body = (message + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def forward(self):
        if time.time() >= self.expires_at:
            return self.reject(410, "This planning-board link has expired")
        path = urlsplit(self.path).path
        if not self.allowed(path):
            return self.reject(403, "Only the scenario 6 planning board is available")
        if self.command not in {"GET", "HEAD", "POST"}:
            return self.reject(405, "Method not allowed")
        if path == "/scenario6/operationplan/" and self.command not in {"GET", "HEAD"}:
            return self.reject(405, "Method not allowed")
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 2 * 1024 * 1024:
            return self.reject(413, "Request too large")
        body = self.rfile.read(length) if length else None
        headers = {
            key: value for key, value in self.headers.items()
            if key.lower() not in HOP_HEADERS and key.lower() != "content-length"
        }
        if body is not None:
            headers["Content-Length"] = str(len(body))
        headers["X-Forwarded-Proto"] = "https"
        headers["X-Forwarded-For"] = self.client_address[0]
        conn = http.client.HTTPConnection(self.upstream_host, self.upstream_port, timeout=180)
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            response = conn.getresponse()
            payload = response.read()
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() not in HOP_HEADERS and key.lower() != "content-length":
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
        except (OSError, http.client.HTTPException):
            self.reject(502, "Planning service unavailable")
        finally:
            conn.close()

    do_GET = forward
    do_HEAD = forward
    do_POST = forward
    do_PUT = forward
    do_PATCH = forward
    do_DELETE = forward
    do_OPTIONS = forward

    def log_message(self, fmt, *args):
        print(time.strftime("%Y-%m-%d %H:%M:%S"), self.client_address[0], fmt % args, flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8011)
    parser.add_argument("--expires-at", type=int, required=True)
    args = parser.parse_args()
    RestrictedProxy.expires_at = args.expires_at
    server = ThreadingHTTPServer(("127.0.0.1", args.port), RestrictedProxy)
    print(f"restricted proxy on 127.0.0.1:{args.port}, expires {args.expires_at}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

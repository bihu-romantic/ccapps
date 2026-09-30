"""Render standalone quote HTML cards from live order payloads."""
from __future__ import annotations

import json
import re
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent / "web"
_EMBEDDED = re.compile(r"/\*EMBEDDED_JSON\*/.*?/\*END_EMBEDDED\*/", re.S)
DEFAULT_MCP_URL = "https://175.27.132.107/cc-apps-mcp/mcp"


def inject_embedded(html: str, data: dict) -> str:
    blob = json.dumps(data, ensure_ascii=False, default=str)
    if not _EMBEDDED.search(html):
        raise RuntimeError("quote HTML template missing EMBEDDED_JSON marker")
    return _EMBEDDED.sub("/*EMBEDDED_JSON*/ " + blob + " /*END_EMBEDDED*/", html, count=1)


def render_template(name: str, data: dict) -> str:
    path = WEB_DIR / name
    html = path.read_text(encoding="utf-8")
    return inject_embedded(html, data)


def render_batch(data: dict) -> str:
    return render_template("quote_batch_real.html", data)


def render_card(data: dict) -> str:
    return render_template("quote_card_real.html", data)

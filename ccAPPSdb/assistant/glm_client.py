"""直连 GLM (Anthropic 兼容 endpoint) 的 streaming 客户端。

绕过 claude CLI，直接调 /v1/messages 流式接口，获取 token 级 text_delta
和 tool_use 事件，用于 agentic 助手。
"""
import json
import os

import requests


def _config():
    return {
        "base_url": os.environ.get(
            "ANTHROPIC_BASE_URL", "https://open.bigmodel.cn/api/anthropic"
        ).rstrip("/"),
        "api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
        "model": (
            os.environ.get("ANTHROPIC_DEFAULT_HAIKU_MODEL")
            or os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL")
            or "glm-5.2"
        ),
    }


def is_configured():
    """Return True when the model endpoint has the required credentials."""
    return bool(_config()["api_key"])


def config_error_message():
    return "未配置 ANTHROPIC_API_KEY，AI助手无法连接 GLM 模型服务。请在启动 8001 服务前设置该环境变量。"


def stream_message(messages, tools=None, system=None, max_tokens=4096, timeout=600):
    """调用 GLM streaming API。

    生成器：逐 SSE 事件 yield (event_type, data_dict)。
    data_dict 是 `data:` 行 JSON 解析结果。
    非 200 响应抛 RuntimeError。
    """
    cfg = _config()
    if not cfg["api_key"]:
        raise RuntimeError(config_error_message())
    url = cfg["base_url"] + "/v1/messages"
    headers = {
        "x-api-key": cfg["api_key"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": cfg["model"],
        "max_tokens": max_tokens,
        "stream": True,
        "messages": messages,
    }
    if system:
        body["system"] = system
    if tools:
        body["tools"] = tools

    resp = requests.post(url, headers=headers, json=body, stream=True, timeout=timeout)
    if resp.status_code != 200:
        err = ""
        try:
            err = resp.text[:500]
        except Exception:
            pass
        raise RuntimeError("GLM API HTTP %s: %s" % (resp.status_code, err))

    event_type = None
    for raw in resp.iter_lines(decode_unicode=True):
        if not raw:
            continue
        if raw.startswith("event:"):
            event_type = raw[len("event:"):].strip()
        elif raw.startswith("data:"):
            data_str = raw[len("data:"):].lstrip()
            try:
                data = json.loads(data_str)
            except (ValueError, json.JSONDecodeError):
                continue
            if event_type:
                yield event_type, data
                event_type = None

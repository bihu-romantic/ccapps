import json
import logging
import os
import re
import threading
import time

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import agent_tools, glm_client

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_DEFAULT_SKILL_DIRS = [
    os.path.join(_PROJECT_ROOT, ".claude", "skills"),
]
_MAX_SELECTED_SKILLS = 2
_MAX_SKILL_CONTENT_CHARS = 12000

_SKILL_ALIASES = {
    "result-analysis": [
        "结果",
        "分析",
        "诊断",
        "健康度",
        "排程结果",
        "计划结果",
        "planning board",
        "甘特",
        "问题",
        "短缺",
        "延迟",
        "超载",
        "库存",
        "产能",
        "需求满足",
        "problem",
        "constraint",
        "pegging",
        "buffer",
        "resource",
    ],
    "execute": [
        "执行",
        "运行",
        "启动",
        "停止",
        "重启",
        "服务器",
        "runserver",
        "runplan",
        "worker",
        "任务",
        "队列",
        "定时",
        "迁移",
        "备份",
        "恢复",
        "清空",
        "仿真",
        "scenario",
    ],
    "data-parser": [
        "数据",
        "解析",
        "导入",
        "导出",
        "转换",
        "fixture",
        "json",
        "csv",
        "excel",
        "xlsx",
        "loaddata",
        "importfromfolder",
        "exporttofolder",
        "createfixture",
        "字段",
        "模型",
    ],
}


def _parse_skill_markdown(content, fallback_name):
    """Parse the small YAML-like front matter used by SKILL.md files."""
    metadata = {}
    body = content
    if content.startswith("---"):
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", content, re.S)
        if match:
            body = match.group(2)
            for line in match.group(1).splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                metadata[key.strip()] = value.strip().strip("\"'")

    return {
        "name": metadata.get("name") or fallback_name,
        "description": metadata.get("description", ""),
        "content": content,
        "body": body,
    }


def _skill_search_dirs():
    configured = getattr(settings, "CCAPPS_ASSISTANT_SKILL_DIRS", None)
    if configured:
        return [os.path.abspath(os.path.expanduser(p)) for p in configured]
    return _DEFAULT_SKILL_DIRS


def _discover_skills():
    """Discover project skills from configured skill folders."""
    skills = []
    for base_dir in _skill_search_dirs():
        if not os.path.isdir(base_dir):
            continue
        try:
            entries = sorted(os.listdir(base_dir))
        except OSError:
            continue
        for entry in entries:
            skill_path = os.path.join(base_dir, entry, "SKILL.md")
            if not os.path.isfile(skill_path):
                continue
            try:
                with open(skill_path, "r", encoding="utf-8") as skill_file:
                    content = skill_file.read()
            except OSError as exc:
                logger.warning("Unable to read assistant skill %s: %s", skill_path, exc)
                continue
            skill = _parse_skill_markdown(content, entry)
            skill["path"] = skill_path
            skills.append(skill)
    return skills


def _score_skill(skill, user_text, context_text):
    haystack = (user_text or "").lower()
    context_haystack = (context_text or "").lower()
    name = skill.get("name", "").lower()
    score = 0
    matches = []

    if name and name in haystack:
        score += 8
        matches.append({"term": name, "source": "用户问题", "weight": 8})
    elif name and name in context_haystack:
        score += 2
        matches.append({"term": name, "source": "页面上下文", "weight": 2})

    aliases = _SKILL_ALIASES.get(name, [])
    for alias in aliases:
        alias_l = alias.lower()
        if not alias_l:
            continue
        if alias_l in haystack:
            score += 3
            matches.append({"term": alias, "source": "用户问题", "weight": 3})
        elif alias_l in context_haystack:
            score += 1
            matches.append({"term": alias, "source": "页面上下文", "weight": 1})

    for token in re.findall(r"\w{2,}", skill.get("description", "").lower()):
        if token in haystack:
            score += 1
            matches.append({"term": token, "source": "skill 描述", "weight": 1})

    return score, matches


def _score_skill_context(skill, context_text):
    context_haystack = (context_text or "").lower()
    name = skill.get("name", "").lower()
    score = 0
    matches = []

    if name and name in context_haystack:
        score += 2
        matches.append({"term": name, "source": "页面上下文", "weight": 2})

    aliases = _SKILL_ALIASES.get(name, [])
    for alias in aliases:
        alias_l = alias.lower()
        if alias_l and alias_l in context_haystack:
            score += 1
            matches.append({"term": alias, "source": "页面上下文", "weight": 1})

    return score, matches


def _select_relevant_skills(user_message, page_title, page_url, context_data):
    skills = _discover_skills()
    if not skills:
        return [], [], "none"

    user_text = user_message or ""
    context_text = "\n".join([page_title or "", page_url or "", context_data or ""])
    scored = []
    for skill in skills:
        score, matches = _score_skill(skill, user_text, "")
        if score > 0:
            scored.append((score, skill, matches))
    match_source = "用户问题"

    if not scored:
        match_source = "页面上下文"
        for skill in skills:
            score, matches = _score_skill_context(skill, context_text)
            if score > 0:
                scored.append((score, skill, matches))

    scored.sort(key=lambda item: (-item[0], item[1].get("name", "")))
    selected = []
    for score, skill, matches in scored[:_MAX_SELECTED_SKILLS]:
        copied = dict(skill)
        copied["score"] = score
        copied["matches"] = matches[:8]
        selected.append(copied)
    return selected, skills, match_source


def _append_skill_prompt(prompt_parts, selected_skills, all_skills):
    if not all_skills:
        return

    prompt_parts.append("=== 可用 Skills ===")
    prompt_parts.append(
        "你可以根据用户问题选择以下项目技能。若已注入某个 skill，请优先遵循其流程、检查清单和命令建议。"
    )
    for skill in all_skills:
        prompt_parts.append("- %s: %s" % (skill["name"], skill.get("description", "")))
    prompt_parts.append("")

    if not selected_skills:
        prompt_parts.append("当前问题未明显匹配到具体 skill；如用户明确要求某个 skill，再按对应技能处理。")
        prompt_parts.append("")
        return

    prompt_parts.append("=== 本次匹配到的 Skill 指南 ===")
    for skill in selected_skills:
        content = skill.get("content", "")
        truncated = False
        if len(content) > _MAX_SKILL_CONTENT_CHARS:
            content = content[:_MAX_SKILL_CONTENT_CHARS]
            truncated = True
        prompt_parts.append("--- Skill: %s ---" % skill["name"])
        prompt_parts.append(content)
        if truncated:
            prompt_parts.append("[Skill 内容较长，已截断；优先使用以上核心说明。]")
        prompt_parts.append("")

def _sse_event(event, data):
    """格式化一个 SSE 帧；data 为 dict 时转 JSON。"""
    if not isinstance(data, str):
        data = json.dumps(data, ensure_ascii=False)
    # SSE 规范：data 内的换行需拆成多行 data: 前缀，帧以空行结束
    parts = "".join("data: %s\n" % seg for seg in data.split("\n"))
    return "event: %s\n%s\n" % (event, parts)


# ===== agentic 流式（直连 GLM + 工具循环）=====

_pending_confirms = {}   # request_id -> {"tool_use_id", "event":Event, "decision"}
_confirm_lock = threading.Lock()
_CONFIRM_TIMEOUT = 300   # 写操作确认等待超时（秒）
_cancelled_requests = set()
_cancel_lock = threading.Lock()


def _is_cancelled(request_id):
    with _cancel_lock:
        return request_id in _cancelled_requests


def _cancel_request(request_id):
    with _cancel_lock:
        _cancelled_requests.add(request_id)
    # A pending write confirmation must not keep the streaming request open.
    with _confirm_lock:
        entry = _pending_confirms.get(request_id)
        if entry and not entry.get("decision"):
            entry["decision"] = {"approved": False, "reason": "用户停止生成"}
            entry["event"].set()


def _clear_cancel(request_id):
    with _cancel_lock:
        _cancelled_requests.discard(request_id)


def _wait_for_confirm(request_id, tool_use_id, timeout=_CONFIRM_TIMEOUT):
    """阻塞等待前端对某次写工具调用的确认。返回 {"approved":bool,"reason":str} 或 None（超时）。"""
    ev = threading.Event()
    with _confirm_lock:
        _pending_confirms[request_id] = {
            "tool_use_id": tool_use_id, "event": ev, "decision": None,
        }
    ev.wait(timeout=timeout)
    with _confirm_lock:
        entry = _pending_confirms.pop(request_id, {})
    return entry.get("decision")


def _notify_confirm(request_id, tool_use_id, approved, reason=""):
    """前端确认/拒绝后调用，唤醒挂起的 generator。"""
    with _confirm_lock:
        entry = _pending_confirms.get(request_id)
        if entry and entry.get("tool_use_id") == tool_use_id:
            entry["decision"] = {
                "approved": bool(approved),
                "reason": reason or ("用户确认" if approved else "用户拒绝"),
            }
            entry["event"].set()
            return True
    return False


def _agent_system_prompt(scenario, page_url, user_message, page_title, context_data):
    prompt_parts = [
        "你是集成在 ccAPPS 供应链规划平台中的AI助手（agentic 模式）。",
        "你可以调用工具查询真实数据和运行命令。面对用户问题，请主动调用合适工具获取数据，"
        "基于真实结果再回答；不要编造数据。",
        "请使用中文回复，正文像对话一样逐步说明你在做什么、查到了什么。",
        "当前场景：%s。数据查询工具的 scenario 参数请使用 '%s'（除非用户明确指定其他场景）。"
        % (scenario, scenario),
        "页面URL：%s" % (page_url or "(未知)"),
        "写操作（如 run_plan）会要求用户在前端确认后才执行；若被拒绝，告知用户即可。",
        "run_plan 只会提交后台排产任务并返回 task_id。提交后告知用户任务已在后台运行，"
        "不要在同一轮反复轮询；用户要求查看进度时再调用 query_task_status。",
        "",
    ]
    # 匹配并注入项目 skills（复用现有逻辑），让模型遵循 skill 流程
    selected_skills, all_skills, _match_source = _select_relevant_skills(
        user_message, page_title, page_url, context_data
    )
    _append_skill_prompt(prompt_parts, selected_skills, all_skills)
    return "\n".join(prompt_parts)


def _agentic_sse(request_id, messages, scenario, system, timeout):
    """agentic 循环：GLM streaming → text/tool_use → 执行工具 → 结果回模型 → 循环。

    SSE 事件：start | text(逐字) | tool_call | tool_confirm | tool_running | tool_result | done | error
    """
    tools = agent_tools.TOOL_SCHEMAS
    # 超时看门狗：到点唤醒可能挂起的确认
    if timeout:
        def _watchdog():
            time.sleep(timeout)
            with _confirm_lock:
                entry = _pending_confirms.get(request_id)
                if entry and not entry.get("decision"):
                    entry["decision"] = {"approved": False, "reason": "超时自动拒绝"}
                    entry["event"].set()
        threading.Thread(target=_watchdog, daemon=True).start()

    try:
        yield _sse_event("start", {"request_id": request_id})

        while True:
            if _is_cancelled(request_id):
                yield _sse_event("done", {"request_id": request_id, "cancelled": True})
                break

            stop_reason = None
            text_parts = []
            tool_uses = []   # [{"id","name","input_raw"}]

            for ev, data in glm_client.stream_message(messages, tools, system, max_tokens=4096):
                if _is_cancelled(request_id):
                    yield _sse_event("done", {"request_id": request_id, "cancelled": True})
                    return
                if ev == "content_block_start":
                    block = (data.get("content_block") or {})
                    if block.get("type") == "tool_use":
                        tool_uses.append({"id": block.get("id"), "name": block.get("name"), "input_raw": ""})
                        yield _sse_event("tool_call", {"id": block.get("id"), "name": block.get("name")})
                elif ev == "content_block_delta":
                    delta = (data.get("delta") or {})
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        text_parts.append(delta["text"])
                        yield _sse_event("text", delta["text"])
                    elif delta.get("type") == "input_json_delta" and tool_uses:
                        tool_uses[-1]["input_raw"] += delta.get("partial_json", "")
                elif ev == "message_delta":
                    sr = (data.get("delta") or {}).get("stop_reason")
                    if sr:
                        stop_reason = sr
                elif ev == "message_stop":
                    break

            # 收集本轮 assistant message（text + tool_use blocks）
            assistant_content = []
            full_text = "".join(text_parts)
            if full_text:
                assistant_content.append({"type": "text", "text": full_text})
            for tu in tool_uses:
                try:
                    tu_input = json.loads(tu["input_raw"]) if tu["input_raw"] else {}
                except (ValueError, json.JSONDecodeError):
                    tu_input = {}
                tu["input"] = tu_input
                assistant_content.append({
                    "type": "tool_use", "id": tu["id"], "name": tu["name"], "input": tu_input,
                })
            messages.append({"role": "assistant", "content": assistant_content})

            if stop_reason != "tool_use" or not tool_uses:
                yield _sse_event("done", {"request_id": request_id})
                break

            # 处理每个 tool_use
            for tu in tool_uses:
                if _is_cancelled(request_id):
                    yield _sse_event("done", {"request_id": request_id, "cancelled": True})
                    return
                if agent_tools.is_write_tool(tu["name"], tu["input"]):
                    yield _sse_event("tool_confirm", {
                        "request_id": request_id,
                        "tool_use_id": tu["id"],
                        "name": tu["name"],
                        "input": tu["input"],
                    })
                    decision = _wait_for_confirm(request_id, tu["id"])
                    if _is_cancelled(request_id):
                        yield _sse_event("done", {"request_id": request_id, "cancelled": True})
                        return
                    if not (decision and decision.get("approved")):
                        reason = (decision or {}).get("reason", "用户拒绝")
                        messages.append({"role": "user", "content": [
                            {"type": "tool_result", "tool_use_id": tu["id"], "content": "已拒绝：%s" % reason}
                        ]})
                        yield _sse_event("tool_result", {"id": tu["id"], "name": tu["name"], "status": "rejected", "reason": reason})
                        continue
                yield _sse_event("tool_running", {"id": tu["id"], "name": tu["name"]})
                result_box = {}

                def _run_tool():
                    result_box["result"] = agent_tools.execute(tu["name"], tu["input"], scenario)

                started = time.time()
                worker = threading.Thread(target=_run_tool, daemon=True)
                worker.start()
                while worker.is_alive():
                    worker.join(timeout=5)
                    if _is_cancelled(request_id):
                        # A running Django/management command can't be safely killed here.
                        # Stop streaming and prevent another model/tool iteration instead.
                        yield _sse_event("done", {"request_id": request_id, "cancelled": True})
                        return
                    if worker.is_alive():
                        yield _sse_event("tool_progress", {
                            "id": tu["id"],
                            "name": tu["name"],
                            "elapsed_seconds": int(time.time() - started),
                            "message": "工具仍在执行，请稍候...",
                        })
                result = result_box.get("result", {"error": "工具执行未返回结果"})
                result_str = json.dumps(result, ensure_ascii=False, default=str)
                messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tu["id"], "content": result_str}
                ]})
                yield _sse_event("tool_result", {"id": tu["id"], "name": tu["name"], "result": result})
            # 继续 agentic 循环（模型基于工具结果再决策）
    except Exception as e:
        logger.exception("agentic stream error")
        yield _sse_event("error", {"error": "%s: %s" % (type(e).__name__, e)})
    finally:
        with _confirm_lock:
            _pending_confirms.pop(request_id, None)
        _clear_cancel(request_id)


@csrf_exempt
@require_POST
@login_required
def chat(request):
    request_id = None
    try:
        data = json.loads(request.body.decode("utf-8"))
        action = data.get("action", "")

        # Stop the GLM agent at its next stream or tool boundary.
        if action == "stop":
            request_id = data.get("request_id", "")
            if not request_id:
                return JsonResponse({"stopped": False, "error": "Missing request id"}, status=400)
            _cancel_request(request_id)
            return JsonResponse({"stopped": True})

        if not data.get("message", "").strip():
            return JsonResponse({"error": "Empty message"}, status=400)

        # 解析当前 scenario（从 page_url）
        match = re.search(r"/(scenario\d+|default)/", data.get("page_url", ""))
        scenario = match.group(1) if match else "default"

        # 构建 user message（含页面上下文 / 附件）
        user_content = data.get("message", "").strip()
        page_title = data.get("page_title", "")
        page_url = data.get("page_url", "")
        context_data = data.get("context_data", "")
        if page_title or page_url:
            user_content += "\n\n[当前页面] " + " | ".join([p for p in [page_title, page_url] if p])
        if context_data:
            user_content += "\n[页面数据] " + context_data[:2000]
        for f in data.get("files", []):
            fname = f.get("name", "upload.txt") or "upload.txt"
            fcontent = f.get("content", "") or ""
            saved_path = ""
            try:
                upload_dir = os.path.join(_PROJECT_ROOT, "ccAPPSdb", "input", "fixtures", "uploads")
                os.makedirs(upload_dir, exist_ok=True)
                saved_path = os.path.join(upload_dir, os.path.basename(fname))
                with open(saved_path, "w", encoding="utf-8") as fh:
                    fh.write(fcontent)
            except Exception:
                saved_path = ""
            if saved_path:
                user_content += "\n\n[附件: %s，已保存到磁盘: %s]\n内容预览(前1500字符):\n%s" % (fname, saved_path, fcontent[:1500])
            else:
                user_content += "\n\n[附件: %s]\n%s" % (fname, fcontent)

        messages = []
        for msg in (data.get("history") or [])[-10:]:
            messages.append({"role": msg.get("role", "user"), "content": msg.get("content", "")})
        messages.append({"role": "user", "content": user_content})

        system = _agent_system_prompt(scenario, page_url, data.get("message", ""), page_title, context_data)
        timeout = getattr(settings, "CCAPPS_ASSISTANT_TIMEOUT", 600)

        import uuid
        request_id = str(uuid.uuid4())[:8]

        if not glm_client.is_configured():
            def _config_error():
                yield _sse_event("start", {"request_id": request_id})
                yield _sse_event("error", {"error": glm_client.config_error_message()})

            response = StreamingHttpResponse(
                _config_error(),
                content_type="text/event-stream",
            )
            response["Cache-Control"] = "no-cache"
            response["X-Accel-Buffering"] = "no"
            return response

        # agentic 流式响应：GLM streaming + 工具循环
        response = StreamingHttpResponse(
            _agentic_sse(request_id, messages, scenario, system, timeout),
            content_type="text/event-stream",
        )
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"  # 禁用反向代理缓冲，保证实时
        return response

    except json.JSONDecodeError:
        return JsonResponse({"error": "请求无效"}, status=400)
    except Exception as e:
        logger.exception("Assistant chat error")
        return JsonResponse({"error": str(e)}, status=500)
@csrf_exempt
@require_POST
@login_required
def confirm(request):
    """前端对写工具调用的确认/拒绝。body: {request_id, tool_use_id, approved}"""
    try:
        data = json.loads(request.body.decode("utf-8"))
        request_id = data.get("request_id", "")
        tool_use_id = data.get("tool_use_id", "")
        approved = bool(data.get("approved"))
        ok = _notify_confirm(request_id, tool_use_id, approved, data.get("reason", ""))
        return JsonResponse({"ok": ok})
    except json.JSONDecodeError:
        return JsonResponse({"error": "请求无效"}, status=400)
    except Exception as e:
        logger.exception("Assistant confirm error")
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
def page_context(request):
    """Return current page context for debugging."""
    return JsonResponse(
        {
            "url": request.META.get("HTTP_REFERER", ""),
            "database": getattr(request, "database", None),
            "user": str(request.user),
        }
    )

"""ccAPPS FJSP 工具 MCP server —— 把 tools/ 下的脚本按描述文件暴露成 MCP 工具。

FastMCP v2, streamable-http transport. Run:
    python server.py   (binds 0.0.0.0:9000, serves /mcp)

设计要点（都是实测踩出来的，改动前请先读 scripts/deploy/README.md 与 plan）：
  * 工具是启动时从 tools/*.tool.yaml 扫出来的，加工具不用改本文件。
  * fastmcp 2.14 的 add_tool 只收 Tool 实例，不收函数；参数 schema 由
    FunctionTool(parameters=...) 提供，且**服务端不做校验**，所以这里自己校验。
  * 工具函数必须 async —— 同步 def 会阻塞事件循环（两个并发 3s 调用会变成 6s）。
  * 每次调用独立临时目录 + cwd，因为部分脚本按相对路径写文件。
"""
import asyncio
import base64
import binascii
import contextlib
import json
import os
import re
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.tools import FunctionTool

from ccapps_client import BearerMiddleware
import spec as specmod
from spec import SpecError

BASE_DIR = Path(__file__).resolve().parent
TOOLS_DIR = Path(os.environ.get("FJSP_TOOLS_DIR", BASE_DIR / "tools"))

MAX_OUTPUT = int(os.environ.get("FJSP_MAX_OUTPUT_BYTES", 256 * 1024))
MAX_UPLOAD = int(os.environ.get("FJSP_MAX_UPLOAD_BYTES", 12 * 1024 * 1024))

mcp = FastMCP("ccapps-fjsp")


# ----------------------------- 脱敏 -----------------------------
_SECRETS = [v for v in (os.environ.get("FJSP_API_PASSWORD", ""),
                        os.environ.get("FJSP_API_USER", "")) if len(v) >= 4]


def _redact(s: str) -> str:
    for sec in _SECRETS:
        s = s.replace(sec, "***")
    return re.sub(r"\b(Basic|Bearer)\s+[A-Za-z0-9+/=]{8,}", r"\1 ***", s)


def _truncate(s: str) -> str:
    if len(s.encode("utf-8", "replace")) <= MAX_OUTPUT:
        return s
    cut = s.encode("utf-8", "replace")[:MAX_OUTPUT].decode("utf-8", "ignore")
    return cut + "\n…[输出超 %d 字节已截断，原始 %d 字节]" % (MAX_OUTPUT, len(s))


def _fail(msg, **extra):
    return {"status": "error", "message": msg, **extra}


# ----------------------------- 上传文件落地 -----------------------------
def _write_upload(spec, param, value, workdir):
    """base64 → 临时文件。三类失败分别给出可操作的报错。"""
    raw = re.sub(r"\s+", "", str(value).strip())
    if raw.startswith("data:"):           # 容忍 data:application/...;base64,xxx
        raw = raw.split(",", 1)[-1]

    est = len(raw) * 3 // 4
    limit = int(param.get("file", {}).get("max_bytes") or MAX_UPLOAD)
    if est > limit:
        raise SpecError(
            "%s: 上传内容过大 —— base64 %d 字符，解码后约 %.1f MB，上限 %.1f MB。"
            "请精简工作簿后重试。" % (param["name"], len(raw), est / 1048576, limit / 1048576))

    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as e:
        raise SpecError(
            "%s: base64 解码失败(%s)。常见原因：传的是文件路径或 URL 而不是内容、"
            "内容被截断、或混入了说明文字。请让客户端以二进制方式读取 .xlsx 后整体 base64。"
            % (param["name"], e))

    if len(data) > limit:
        raise SpecError("%s: 解码后 %d 字节，超过上限 %d 字节" % (param["name"], len(data), limit))
    if not data.startswith(b"PK\x03\x04"):
        raise SpecError(
            "%s: 解码结果不是 .xlsx(zip) 文件(magic=%r)，看起来传成了文本或别的格式。"
            % (param["name"], data[:4]))

    dest = Path(workdir) / Path(param["file"]["as"]).name   # 只取 basename，防路径穿越
    dest.write_bytes(data)
    return str(dest), len(data)


# ----------------------------- 执行 -----------------------------
async def _execute(spec, kwargs):
    workdir = tempfile.mkdtemp(prefix="fjsp_%s_" % spec.name)
    try:
        values, uploaded = {}, {}
        for p in spec.params:
            name = p["name"]
            v = kwargs.get(name, p.get("default"))
            if v is None or (isinstance(v, str) and not v.strip()) or v == []:
                if p.get("required"):
                    return _fail("缺少必填参数 %s：%s" % (name, p.get("description", "")))
                values[name] = None
                continue
            if p.get("file"):
                try:
                    path, size = _write_upload(spec, p, v, workdir)
                except SpecError as e:
                    return _fail(str(e))
                values[name] = path
                uploaded[name] = size
            else:
                try:
                    values[name] = specmod.coerce(spec, p, v)
                except SpecError as e:
                    return _fail(str(e))

        # 把上传的文件额外落到脚本写死的绝对路径上（给没有入参入口的脚本用）
        for st in spec.stage:
            src = values.get(st["param"])
            if not src:
                return _fail("stage 需要参数 %s，但它没有值" % st["param"])
            dst = Path(st["path"])
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)

        try:
            argv = specmod.render_argv(spec, values)
        except SpecError as e:
            return _fail(str(e))

        env = os.environ.copy()
        env.update(specmod.render_env(spec, values))
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        script = TOOLS_DIR / spec.script
        t0 = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(script), *argv,
                cwd=workdir, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True)
        except OSError as e:
            return _fail("启动脚本失败: %s" % e)

        timed_out = False
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=spec.timeout_sec)
        except asyncio.TimeoutError:
            timed_out = True
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            with contextlib.suppress(Exception):
                out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=5)

        stdout = _truncate(_redact(out_b.decode("utf-8", "replace")))
        stderr = _truncate(_redact(err_b.decode("utf-8", "replace")))
        head = {"tool": spec.name, "elapsed_sec": round(time.time() - t0, 2)}
        if uploaded:
            head["uploaded_bytes"] = uploaded

        if timed_out:
            return {**head, "status": "timeout",
                    "message": "脚本超过 %.0fs 未结束，已终止。如需放宽，需同时调整本工具的 "
                               "timeout_sec 与 nginx 的 proxy_read_timeout(当前 360s)。"
                               % spec.timeout_sec,
                    "stdout": stdout, "stderr": stderr}

        head["exit_code"] = proc.returncode
        ok = proc.returncode in spec.ok_exit_codes
        if spec.output == "json" and stdout.strip():
            try:
                parsed = json.loads(stdout)
            except ValueError:
                return {**head, "status": "ok" if ok else "failed",
                        "note": "描述文件声明 output=json，但 stdout 不是合法 JSON，已原样返回。",
                        "stdout": stdout, "stderr": stderr}
            return {**head, "status": "ok" if ok else "report", "result": parsed, "stderr": stderr}
        return {**head, "status": "ok" if ok else "failed", "stdout": stdout, "stderr": stderr}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ----------------------------- 注册 -----------------------------
def _make_tool(s):
    async def _runner(**kwargs):
        return await _execute(s, kwargs)

    _runner.__name__ = "tool_" + s.name
    return FunctionTool(fn=_runner, name=s.name, description=s.description,
                        parameters=s.json_schema())


SPECS, ERRORS = specmod.load_specs(TOOLS_DIR)
if ERRORS:
    print("[fjsp] 描述文件有错误，拒绝启动：", file=sys.stderr)
    for e in ERRORS:
        print("  ✗ %s" % e, file=sys.stderr)
    sys.exit(1)

for _s in SPECS:
    mcp.add_tool(_make_tool(_s))
    print("[fjsp] 注册工具 %-24s write=%-5s params=%d timeout=%ss"
          % (_s.name, _s.write, len(_s.params), _s.timeout_sec))


# ----------------------------- serve -----------------------------
MCP_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "9000"))

app = mcp.http_app(path="/mcp", transport="streamable-http")
if MCP_TOKEN:
    app = BearerMiddleware(app, MCP_TOKEN)

if __name__ == "__main__":
    import uvicorn
    print("[fjsp] serving on http://%s:%s/mcp  tools=%d  auth=%s"
          % (MCP_HOST, MCP_PORT, len(SPECS), "on" if MCP_TOKEN else "OFF（未设 MCP_AUTH_TOKEN）"))
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)

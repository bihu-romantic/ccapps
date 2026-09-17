"""工具描述文件（*.tool.yaml）的加载、校验与 argv/env 渲染。

刻意不依赖 fastmcp —— 这样这部分逻辑可以用本地 venv 直接单测，
不需要起容器。server.py 只负责把它接到 FastMCP 上。
"""
import os
import re
from pathlib import Path

import yaml

# 参数名允许的字符（同时也是 argv 模板里 ${...} 的合法名字）
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# argv 模板的三种形态
_FILE_TMPL = re.compile(r"^\$\{file:([A-Za-z_][A-Za-z0-9_]*)(\?)?\}$")
_VAL_TMPL = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)(\?)?\}$")

VALID_TYPES = {"string", "boolean", "integer", "number", "array"}


class SpecError(Exception):
    """描述文件本身有问题（启动期）或调用参数不合法（运行期）。"""


class ToolSpec:
    def __init__(self, raw, source):
        self.source = source
        self.name = self._req(raw, "name")
        if not _NAME_RE.match(self.name):
            raise SpecError("name 只允许字母/数字/下划线: %r" % self.name)
        self.description = self._req(raw, "description").strip()
        self.script = self._req(raw, "script")

        self.params = raw.get("params") or []
        self.argv = raw.get("argv") or []
        self.env = raw.get("env") or {}
        self.timeout_sec = float(raw.get("timeout_sec", 300))
        self.ok_exit_codes = list(raw.get("ok_exit_codes", [0]))
        self.output = raw.get("output", "text")
        self.write = bool(raw.get("write", False))
        # 把上传的文件落到指定绝对路径（给「入参写死在脚本里」的工具用）
        self.stage = raw.get("stage") or []

        self._validate()

    @staticmethod
    def _req(raw, key):
        v = raw.get(key)
        if not v or not isinstance(v, str):
            raise SpecError("缺少必填字段 %s" % key)
        return v

    def _validate(self):
        if self.output not in ("text", "json"):
            raise SpecError("%s: output 只能是 text 或 json" % self.name)
        if self.timeout_sec >= 340:
            raise SpecError(
                "%s: timeout_sec=%s 必须 < 340（nginx proxy_read_timeout 是 360s，"
                "超了会被网关先掐断）" % (self.name, self.timeout_sec))

        seen = set()
        for p in self.params:
            n = p.get("name")
            if not n or not _NAME_RE.match(n):
                raise SpecError("%s: params[].name 非法: %r" % (self.name, n))
            if n in seen:
                raise SpecError("%s: 参数名重复: %s" % (self.name, n))
            seen.add(n)
            t = p.get("type", "string")
            if t not in VALID_TYPES:
                raise SpecError("%s.%s: 不支持的 type=%r" % (self.name, n, t))
            if not p.get("description"):
                raise SpecError("%s.%s: 缺少 description（LLM 靠它决定怎么传）" % (self.name, n))
            f = p.get("file")
            if f:
                if t != "string":
                    raise SpecError("%s.%s: 声明了 file 的参数 type 必须是 string" % (self.name, n))
                if not f.get("as"):
                    raise SpecError("%s.%s: file.as 必填（临时文件名）" % (self.name, n))

        # argv/env 里引用的参数必须存在
        for tmpl in self._templates():
            for ref in self._refs(tmpl):
                if ref not in seen:
                    raise SpecError("%s: 模板 %r 引用了未声明的参数 %s"
                                    % (self.name, tmpl, ref))

        for st in self.stage:
            if st.get("param") not in seen:
                raise SpecError("%s: stage.param 未声明: %r" % (self.name, st.get("param")))
            if not str(st.get("path", "")).startswith("/"):
                raise SpecError("%s: stage.path 必须是绝对路径" % self.name)

    def _templates(self):
        out = []
        for item in self.argv:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                if "flag" not in item:
                    raise SpecError("%s: argv 映射缺少 flag: %r" % (self.name, item))
                out.append(item.get("value") or item.get("bool") or "")
        out.extend(str(v) for v in self.env.values())
        return out

    @staticmethod
    def _refs(tmpl):
        refs = []
        for m in _FILE_TMPL.finditer(tmpl):
            refs.append(m.group(1))
        for m in _VAL_TMPL.finditer(tmpl):
            refs.append(m.group(1))
        return refs

    # ---- 给 LLM 看的 JSON Schema ----
    def json_schema(self):
        props, required = {}, []
        for p in self.params:
            t = p.get("type", "string")
            schema = {"type": t}
            if t == "array":
                schema["items"] = p.get("items") or {"type": "string"}
            if "enum" in p:
                schema["enum"] = p["enum"]
            if "default" in p and p["default"] is not None:
                schema["default"] = p["default"]
            schema["description"] = p["description"]
            props[p["name"]] = schema
            if p.get("required"):
                required.append(p["name"])
        out = {"type": "object", "properties": props}
        if required:
            out["required"] = required
        # 关掉额外字段：fastmcp 不做服务端校验，靠这个让客户端心里有数
        out["additionalProperties"] = False
        return out

    def file_params(self):
        return [p for p in self.params if p.get("file")]

    def param(self, name):
        for p in self.params:
            if p["name"] == name:
                return p
        return None


def load_specs(tools_dir):
    """扫描目录下所有 *.tool.yaml，返回 (specs, errors)。

    任何一份非法都会进 errors —— 调用方应当据此启动失败，而不是静默跳过：
    工具静默消失时，调用方完全不知道为什么。
    """
    specs, errors = [], []
    seen = {}
    for path in sorted(Path(tools_dir).glob("*.tool.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                raise SpecError("顶层必须是映射")
            s = ToolSpec(raw, path.name)
            if s.name in seen:
                raise SpecError("工具名与 %s 重复" % seen[s.name])
            seen[s.name] = path.name
            if not (Path(tools_dir) / s.script).exists():
                raise SpecError("script 不存在: %s" % s.script)
            specs.append(s)
        except Exception as e:  # noqa: BLE001 - 汇总所有错误一起报
            errors.append("%s: %s" % (path.name, e))
    return specs, errors


def coerce(spec, param, value):
    """把 LLM 传来的值转成脚本能用的形态。"""
    t = param.get("type", "string")
    if value is None:
        return None
    if t == "boolean":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "y", "on")
    if t == "integer":
        try:
            return int(value)
        except (TypeError, ValueError):
            raise SpecError("%s.%s: 期望整数，收到 %r" % (spec.name, param["name"], value))
    if t == "number":
        try:
            return float(value)
        except (TypeError, ValueError):
            raise SpecError("%s.%s: 期望数字，收到 %r" % (spec.name, param["name"], value))
    if t == "array":
        if isinstance(value, list):
            items = [str(v) for v in value]
        else:
            items = [s.strip() for s in str(value).split(",") if s.strip()]
        if "enum" in param:
            bad = [v for v in items if v not in param["enum"]]
            if bad:
                raise SpecError("%s.%s: 取值不在允许范围 %s: %s"
                                % (spec.name, param["name"], param["enum"], bad))
        return items
    s = str(value)
    if "enum" in param and s not in param["enum"]:
        raise SpecError("%s.%s: 取值必须是 %s 之一，收到 %r"
                        % (spec.name, param["name"], param["enum"], s))
    return s


def render_argv(spec, values):
    """把已校验/已落盘的值渲染成 argv。values 里 file 参数已是临时文件路径。"""
    argv = []
    for item in spec.argv:
        if isinstance(item, str):
            m = _FILE_TMPL.match(item)
            if m:
                v = values.get(m.group(1))
                if v:
                    argv.append(str(v))
                elif not m.group(2):
                    raise SpecError("%s: 缺少必填文件参数 %s" % (spec.name, m.group(1)))
                continue
            m = _VAL_TMPL.match(item)
            if m:
                v = values.get(m.group(1))
                if v is not None and v != "" and v != []:
                    argv.extend([str(x) for x in v] if isinstance(v, list) else [str(v)])
                elif not m.group(2):
                    raise SpecError("%s: 缺少必填参数 %s" % (spec.name, m.group(1)))
                continue
            argv.append(item)
            continue

        if isinstance(item, dict):
            if "bool" in item:
                if values.get(item["bool"]):
                    argv.append(item["flag"])
                continue
            v = values.get(item.get("value"))
            if v is None or v == "" or v == []:
                continue
            argv.append(item["flag"])
            argv.extend([str(x) for x in v] if isinstance(v, list) else [str(v)])
            continue

        raise SpecError("%s: argv 元素类型不支持: %r" % (spec.name, item))
    return argv


def render_env(spec, values):
    """渲染 env 段，支持内嵌 ${参数} / ${环境变量}（先查参数，再查环境变量）。"""
    def sub(m):
        name = m.group(1)
        if name in values and values[name] is not None:
            return str(values[name])
        return os.environ.get(name, "")

    return {k: re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", sub, str(tmpl))
            for k, tmpl in spec.env.items()}

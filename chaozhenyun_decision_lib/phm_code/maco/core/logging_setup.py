"""
日志设施模块
============

统一日志格式与输出目标，并提供两项工业场景必需的能力：

1. **链路标识注入**：日志记录自动携带 ``trace_id`` 与 ``device_id``，
   使得一条控制指令从采集到评估的所有日志可用同一标识串起来；
2. **敏感信息过滤**：对疑似密钥、令牌的字符串做掩码，防止现场调试时
   把接入凭据打进日志文件。
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

#: 线程局部的链路上下文
_context = threading.local()

_SECRET_RE = re.compile(
    r"(?i)(token|password|secret|api[_-]?key|credential)\s*[=:]\s*['\"]?([^\s,'\"}\]]+)"
)

_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-28s | %(trace)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False
_config_lock = threading.Lock()


def set_trace_context(trace_id: str = "", device_id: str = "") -> None:
    """设置当前线程的链路上下文。"""
    _context.trace_id = trace_id
    _context.device_id = device_id


def clear_trace_context() -> None:
    _context.trace_id = ""
    _context.device_id = ""


def current_trace() -> Dict[str, str]:
    return {
        "trace_id": getattr(_context, "trace_id", "") or "",
        "device_id": getattr(_context, "device_id", "") or "",
    }


class TraceFilter(logging.Filter):
    """把链路上下文注入日志记录。"""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = current_trace()
        trace_id = getattr(record, "trace_id", "") or ctx["trace_id"]
        device_id = getattr(record, "device_id", "") or ctx["device_id"]
        if trace_id or device_id:
            record.trace = f"{device_id or '-'}/{(trace_id or '-')[-12:]}"
        else:
            record.trace = "-"
        return True


class SecretMaskFilter(logging.Filter):
    """掩码日志中的疑似敏感值。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - 格式化失败时不阻断日志
            return True
        masked = _SECRET_RE.sub(lambda m: f"{m.group(1)}=***", message)
        if masked != message:
            record.msg = masked
            record.args = ()
        return True


def setup_logging(
    level: str = "INFO",
    log_dir: Optional[str] = None,
    console: bool = True,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Logger:
    """初始化根日志器。重复调用是幂等的。

    :param level: 日志级别名
    :param log_dir: 日志目录；为空则只输出到控制台
    :param console: 是否同时输出到标准输出
    """
    global _configured
    with _config_lock:
        root = logging.getLogger("maco")
        root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
        if _configured:
            return root
        root.handlers.clear()
        root.propagate = False
        formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)
        trace_filter = TraceFilter()
        mask_filter = SecretMaskFilter()

        if console:
            sh = logging.StreamHandler(stream=sys.stdout)
            sh.setFormatter(formatter)
            sh.addFilter(trace_filter)
            sh.addFilter(mask_filter)
            root.addHandler(sh)

        if log_dir:
            path = Path(log_dir)
            path.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                filename=str(path / "maco.log"),
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            fh.setFormatter(formatter)
            fh.addFilter(trace_filter)
            fh.addFilter(mask_filter)
            root.addHandler(fh)

            eh = logging.handlers.RotatingFileHandler(
                filename=str(path / "maco-error.log"),
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            eh.setLevel(logging.WARNING)
            eh.setFormatter(formatter)
            eh.addFilter(trace_filter)
            eh.addFilter(mask_filter)
            root.addHandler(eh)

        _configured = True
        return root


def get_logger(name: str) -> logging.Logger:
    """获取命名日志器，自动挂到 ``maco`` 命名空间下。"""
    if not name.startswith("maco"):
        name = f"maco.{name}"
    logger = logging.getLogger(name)
    if not _configured:
        setup_logging()
    return logger


class TraceScope:
    """链路上下文的上下文管理器。

    用法::

        with TraceScope(trace_id, device_id):
            logger.info("开始工况识别")
    """

    def __init__(self, trace_id: str = "", device_id: str = ""):
        self.trace_id = trace_id
        self.device_id = device_id
        self._saved: Dict[str, str] = {}

    def __enter__(self) -> "TraceScope":
        self._saved = current_trace()
        set_trace_context(self.trace_id, self.device_id)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        set_trace_context(self._saved.get("trace_id", ""), self._saved.get("device_id", ""))


__all__ = [
    "setup_logging",
    "get_logger",
    "TraceScope",
    "set_trace_context",
    "clear_trace_context",
    "current_trace",
]

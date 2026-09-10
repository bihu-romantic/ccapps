"""
审计追溯模块
============

工业控制系统的每一次写值都必须可追溯到 "谁、在什么工况、依据什么证据、
预期什么收益、实际结果如何"。本模块提供追加式审计账本：

* 采用 JSONL 追加写，单条记录自包含，不依赖数据库；
* 每条记录带序号与前序哈希，形成链式校验，事后可检出记录被篡改或缺失；
* 审计写入失败不阻断控制链路，但会升级为系统告警事件。

审计账本只写不改，任何更正都以新增 "更正记录" 的方式表达。
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .errors import StorageError
from .logging_setup import get_logger
from .types import new_id, now_ts

logger = get_logger("core.audit")

#: 审计动作类别
ACTION_ACQUIRE = "acquire"
ACTION_CONDITION = "condition"
ACTION_HEALTH = "health"
ACTION_ADVICE = "advice"
ACTION_SAFETY = "safety"
ACTION_DISPATCH = "dispatch"
ACTION_ROLLBACK = "rollback"
ACTION_EVALUATE = "evaluate"
ACTION_LEARN = "learn"
ACTION_CONFIG = "config"
ACTION_CORRECTION = "correction"


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AuditTrail:
    """链式审计账本。"""

    def __init__(self, file_path: str, enable: bool = True, memory_limit: int = 5000):
        self._path = Path(file_path)
        self._enable = enable
        self._lock = threading.RLock()
        self._seq = 0
        self._prev_hash = "0" * 64
        self._memory: List[Dict[str, Any]] = []
        self._memory_limit = memory_limit
        if self._enable:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._restore_tail()

    def _restore_tail(self) -> None:
        """从已有账本恢复序号与末尾哈希，保证重启后链条连续。"""
        if not self._path.exists():
            return
        last_line = ""
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        last_line = line
        except OSError as exc:  # pragma: no cover - 文件系统异常
            logger.warning("读取审计账本失败，将从头开始记录: %s", exc)
            return
        if not last_line:
            return
        try:
            record = json.loads(last_line)
            self._seq = int(record.get("seq", 0))
            self._prev_hash = str(record.get("hash", self._prev_hash))
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.warning("审计账本末行损坏，链条将从当前位置重新起算")

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def record(
        self,
        action: str,
        device_id: str = "",
        trace_id: str = "",
        actor: str = "system",
        detail: Optional[Dict[str, Any]] = None,
        result: str = "ok",
    ) -> Dict[str, Any]:
        """写一条审计记录，返回记录本体。"""
        with self._lock:
            self._seq += 1
            body = {
                "seq": self._seq,
                "record_id": new_id("aud"),
                "timestamp": now_ts(),
                "action": action,
                "device_id": device_id,
                "trace_id": trace_id,
                "actor": actor,
                "result": result,
                "detail": _sanitize(detail or {}),
                "prev_hash": self._prev_hash,
            }
            canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            body["hash"] = _digest(canonical)
            self._prev_hash = body["hash"]
            self._memory.append(body)
            if len(self._memory) > self._memory_limit:
                del self._memory[: len(self._memory) - self._memory_limit]
            if self._enable:
                self._append(body)
            return body

    def _append(self, body: Dict[str, Any]) -> None:
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(body, ensure_ascii=False) + "\n")
        except OSError as exc:
            # 审计写盘失败不阻断控制，但必须显式暴露
            logger.error("审计写入失败: %s", exc)
            raise StorageError("审计账本写入失败", {"path": str(self._path)}) from exc

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def query(
        self,
        action: str = "",
        device_id: str = "",
        trace_id: str = "",
        since: Optional[float] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """按条件倒序查询内存中的近期审计记录。"""
        with self._lock:
            items = list(self._memory)
        out: List[Dict[str, Any]] = []
        for rec in reversed(items):
            if action and rec.get("action") != action:
                continue
            if device_id and rec.get("device_id") != device_id:
                continue
            if trace_id and rec.get("trace_id") != trace_id:
                continue
            if since is not None and rec.get("timestamp", 0) < since:
                continue
            out.append(rec)
            if len(out) >= limit:
                break
        return out

    def iter_file(self) -> Iterator[Dict[str, Any]]:
        """流式读取全量账本（供离线核对使用）。"""
        if not self._path.exists():
            return
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def verify_chain(self) -> Dict[str, Any]:
        """校验账本链条完整性。

        :return: ``{"ok": bool, "checked": int, "broken_at": Optional[int], "reason": str}``
        """
        prev = "0" * 64
        expect_seq = 0
        checked = 0
        for record in self.iter_file():
            expect_seq += 1
            checked += 1
            if int(record.get("seq", -1)) != expect_seq:
                return {"ok": False, "checked": checked,
                        "broken_at": record.get("seq"), "reason": "序号不连续"}
            if record.get("prev_hash") != prev:
                return {"ok": False, "checked": checked,
                        "broken_at": record.get("seq"), "reason": "前序哈希不匹配"}
            body = {k: v for k, v in record.items() if k != "hash"}
            canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if _digest(canonical) != record.get("hash"):
                return {"ok": False, "checked": checked,
                        "broken_at": record.get("seq"), "reason": "记录内容与哈希不一致"}
            prev = record["hash"]
        return {"ok": True, "checked": checked, "broken_at": None, "reason": ""}

    def correct(self, target_record_id: str, reason: str, actor: str = "system") -> Dict[str, Any]:
        """对既有记录发起更正（新增记录，不修改原记录）。"""
        return self.record(
            action=ACTION_CORRECTION,
            actor=actor,
            detail={"target_record_id": target_record_id, "reason": reason},
        )

    @property
    def size(self) -> int:
        with self._lock:
            return self._seq


def _sanitize(detail: Dict[str, Any]) -> Dict[str, Any]:
    """把不可 JSON 序列化的对象转成可序列化形式。"""
    out: Dict[str, Any] = {}
    for key, value in detail.items():
        if hasattr(value, "to_dict"):
            out[key] = value.to_dict()
        elif isinstance(value, dict):
            out[key] = _sanitize(value)
        elif isinstance(value, (list, tuple)):
            out[key] = [
                v.to_dict() if hasattr(v, "to_dict") else
                (_sanitize(v) if isinstance(v, dict) else _plain(v))
                for v in value
            ]
        else:
            out[key] = _plain(value)
    return out


def _plain(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "code"):  # 枚举
        return value.code
    return repr(value)


__all__ = [
    "AuditTrail",
    "ACTION_ACQUIRE",
    "ACTION_CONDITION",
    "ACTION_HEALTH",
    "ACTION_ADVICE",
    "ACTION_SAFETY",
    "ACTION_DISPATCH",
    "ACTION_ROLLBACK",
    "ACTION_EVALUATE",
    "ACTION_LEARN",
    "ACTION_CONFIG",
    "ACTION_CORRECTION",
]

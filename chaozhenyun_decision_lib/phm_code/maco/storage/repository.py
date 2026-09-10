"""
持久化仓储模块
==============

系统以文件为存储介质，不引入数据库，以便在边缘网关等资源受限环境部署。
三类数据分开存放：

* **决策记录**（``decisions/<日期>.jsonl``）—— 全链路留痕，按日分片；
* **知识资产**（``knowledge/rules.json`` / ``cases.json``）—— 可导出迁移；
* **时序归档**（``timeseries/<设备>/<小时>.jsonl``）—— 按小时分片，按天清理。

所有写入均为追加式，避免并发写破坏已有数据；读取按需加载分片，
不把全量数据读进内存。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

from ..core.errors import StorageError
from ..core.logging_setup import get_logger
from ..core.types import DecisionRecord, SampleFrame, now_ts

logger = get_logger("storage.repository")


def _day_key(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d")


def _hour_key(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d%H")


class FileRepository:
    """基于文件的仓储实现。"""

    def __init__(self, root: str = "data/store", retain_days: int = 30):
        self.root = Path(root)
        self.retain_days = max(1, retain_days)
        self._lock = threading.RLock()
        for sub in ("decisions", "knowledge", "timeseries"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 决策记录
    # ------------------------------------------------------------------
    def save_decision(self, record: DecisionRecord) -> None:
        path = self.root / "decisions" / f"{_day_key(record.created_at)}.jsonl"
        self._append(path, record.to_dict())

    def load_decisions(self, day: Optional[str] = None,
                       device_id: str = "", limit: int = 200) -> List[Dict[str, Any]]:
        day = day or _day_key(now_ts())
        path = self.root / "decisions" / f"{day}.jsonl"
        out: List[Dict[str, Any]] = []
        for item in self._iter(path):
            if device_id and item.get("device_id") != device_id:
                continue
            out.append(item)
        return out[-limit:]

    def find_decision(self, trace_id: str, days_back: int = 7) -> Optional[Dict[str, Any]]:
        """按链路号跨分片检索一条决策记录。"""
        now = now_ts()
        for i in range(days_back):
            path = self.root / "decisions" / f"{_day_key(now - i * 86400)}.jsonl"
            for item in self._iter(path):
                if item.get("trace_id") == trace_id:
                    return item
        return None

    # ------------------------------------------------------------------
    # 时序归档
    # ------------------------------------------------------------------
    def archive_frame(self, frame: SampleFrame) -> None:
        folder = self.root / "timeseries" / frame.device_id
        folder.mkdir(parents=True, exist_ok=True)
        self._append(folder / f"{_hour_key(frame.timestamp)}.jsonl", frame.to_dict())

    def load_frames(self, device_id: str, hour_key: str) -> List[Dict[str, Any]]:
        path = self.root / "timeseries" / device_id / f"{hour_key}.jsonl"
        return list(self._iter(path))

    # ------------------------------------------------------------------
    # 知识资产
    # ------------------------------------------------------------------
    def save_json(self, name: str, payload: Any) -> None:
        path = self.root / "knowledge" / f"{name}.json"
        with self._lock:
            try:
                path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                encoding="utf-8")
            except OSError as exc:
                raise StorageError(f"写入 {path} 失败: {exc}", {"name": name}) from exc

    def load_json(self, name: str, default: Any = None) -> Any:
        path = self.root / "knowledge" / f"{name}.json"
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("读取 %s 失败: %s", path, exc)
            return default

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------
    def cleanup(self) -> Dict[str, int]:
        """清理超过保留期的分片。返回各类删除数量。"""
        cutoff = now_ts() - self.retain_days * 86400
        removed = {"decisions": 0, "timeseries": 0}
        for path in (self.root / "decisions").glob("*.jsonl"):
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed["decisions"] += 1
        for path in (self.root / "timeseries").rglob("*.jsonl"):
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed["timeseries"] += 1
        if any(removed.values()):
            logger.info("归档清理完成：%s", removed)
        return removed

    def usage(self) -> Dict[str, Any]:
        """存储占用统计。"""
        def size_of(folder: Path) -> int:
            return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())

        return {
            "root": str(self.root),
            "decisions_bytes": size_of(self.root / "decisions"),
            "timeseries_bytes": size_of(self.root / "timeseries"),
            "knowledge_bytes": size_of(self.root / "knowledge"),
            "retain_days": self.retain_days,
        }

    # ------------------------------------------------------------------
    def _append(self, path: Path, payload: Dict[str, Any]) -> None:
        with self._lock:
            try:
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            except OSError as exc:
                raise StorageError(f"追加写入 {path} 失败: {exc}") from exc

    @staticmethod
    def _iter(path: Path) -> Iterator[Dict[str, Any]]:
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


__all__ = ["FileRepository"]

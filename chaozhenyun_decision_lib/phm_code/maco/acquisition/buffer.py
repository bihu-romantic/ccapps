"""
时序缓冲模块
============

在内存中维护每个测点的近期时序数据，为三类下游提供数据窗口：

1. **特征提取** —— 取最近 N 秒的数据算时域/频域特征；
2. **稳态判定** —— 取窗口内的变异系数判断工况是否稳定；
3. **效果评估** —— 取控制指令下发前/后两段等长窗口做对比检验，
   这是反馈学习闭环能成立的数据基础。

实现要点：按时长而非条数裁剪（现场采样周期可能动态变化），
写入路径无锁竞争最小化，读取返回副本以避免上层修改影响缓冲。
"""

from __future__ import annotations

import bisect
import math
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..core.enums import Quality
from ..core.logging_setup import get_logger
from ..core.types import Sample, SampleFrame, now_ts
from ..core.utils import robust_stats

logger = get_logger("acquisition.buffer")


@dataclass
class SeriesWindow:
    """一段时序窗口。"""

    point_id: str
    timestamps: List[float]
    values: List[float]

    def __len__(self) -> int:
        return len(self.values)

    @property
    def empty(self) -> bool:
        return not self.values

    @property
    def duration(self) -> float:
        if len(self.timestamps) < 2:
            return 0.0
        return self.timestamps[-1] - self.timestamps[0]

    @property
    def sample_rate(self) -> float:
        """平均采样率（Hz）。"""
        d = self.duration
        return (len(self.values) - 1) / d if d > 1e-9 else 0.0

    def stats(self) -> Dict[str, float]:
        return robust_stats(self.values)

    def mean(self) -> float:
        return float(sum(self.values) / len(self.values)) if self.values else 0.0

    def latest(self) -> Optional[float]:
        return self.values[-1] if self.values else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "point_id": self.point_id,
            "count": len(self.values),
            "duration": round(self.duration, 3),
            "sample_rate": round(self.sample_rate, 3),
            "stats": {k: round(v, 6) for k, v in self.stats().items()},
        }


class _PointSeries:
    """单测点的环形时序。内部按时间有序，支持按时间窗二分查找。"""

    __slots__ = ("point_id", "timestamps", "values", "qualities", "retain_seconds",
                 "max_points", "_waveform", "_waveform_ts")

    def __init__(self, point_id: str, retain_seconds: float, max_points: int):
        self.point_id = point_id
        self.timestamps: List[float] = []
        self.values: List[float] = []
        self.qualities: List[Quality] = []
        self.retain_seconds = retain_seconds
        self.max_points = max_points
        self._waveform: Optional[List[float]] = None
        self._waveform_ts: float = 0.0

    def append(self, sample: Sample) -> None:
        ts = sample.timestamp
        value = float(sample.value) if sample.value is not None else math.nan
        # 绝大多数情况是追加到末尾，先判断避免二分开销
        if self.timestamps and ts < self.timestamps[-1]:
            idx = bisect.bisect_right(self.timestamps, ts)
            self.timestamps.insert(idx, ts)
            self.values.insert(idx, value)
            self.qualities.insert(idx, sample.quality)
        else:
            self.timestamps.append(ts)
            self.values.append(value)
            self.qualities.append(sample.quality)
        if sample.waveform is not None:
            self._waveform = list(sample.waveform)
            self._waveform_ts = ts
        self._trim()

    def _trim(self) -> None:
        if not self.timestamps:
            return
        cutoff = self.timestamps[-1] - self.retain_seconds
        drop = bisect.bisect_left(self.timestamps, cutoff)
        if drop > 0:
            del self.timestamps[:drop]
            del self.values[:drop]
            del self.qualities[:drop]
        overflow = len(self.timestamps) - self.max_points
        if overflow > 0:
            del self.timestamps[:overflow]
            del self.values[:overflow]
            del self.qualities[:overflow]

    def window(self, start: float, end: float, only_good: bool = True) -> SeriesWindow:
        lo = bisect.bisect_left(self.timestamps, start)
        hi = bisect.bisect_right(self.timestamps, end)
        ts_out: List[float] = []
        vs_out: List[float] = []
        for i in range(lo, hi):
            v = self.values[i]
            if not math.isfinite(v):
                continue
            if only_good and not self.qualities[i].usable:
                continue
            ts_out.append(self.timestamps[i])
            vs_out.append(v)
        return SeriesWindow(self.point_id, ts_out, vs_out)

    def last_n(self, n: int, only_good: bool = True) -> SeriesWindow:
        ts_out: List[float] = []
        vs_out: List[float] = []
        for i in range(len(self.values) - 1, -1, -1):
            v = self.values[i]
            if not math.isfinite(v):
                continue
            if only_good and not self.qualities[i].usable:
                continue
            ts_out.append(self.timestamps[i])
            vs_out.append(v)
            if len(vs_out) >= n:
                break
        ts_out.reverse()
        vs_out.reverse()
        return SeriesWindow(self.point_id, ts_out, vs_out)

    def latest_sample(self) -> Optional[Tuple[float, float, Quality]]:
        if not self.values:
            return None
        return self.timestamps[-1], self.values[-1], self.qualities[-1]

    def waveform(self, max_age: float = 60.0) -> Optional[List[float]]:
        if self._waveform is None:
            return None
        if now_ts() - self._waveform_ts > max_age:
            return None
        return list(self._waveform)


class TimeSeriesBuffer:
    """全设备时序缓冲。"""

    def __init__(self, retain_seconds: float = 1800.0, max_points_per_series: int = 20000):
        self._retain = max(retain_seconds, 1.0)
        self._max_points = max(max_points_per_series, 10)
        self._series: Dict[str, Dict[str, _PointSeries]] = {}
        self._lock = threading.RLock()
        self._frames: Dict[str, List[SampleFrame]] = {}
        self._frame_limit = 600
        self._appended = 0

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def append_frame(self, frame: SampleFrame) -> None:
        """写入一整帧。"""
        with self._lock:
            device_map = self._series.setdefault(frame.device_id, {})
            for point_id, sample in frame.samples.items():
                series = device_map.get(point_id)
                if series is None:
                    series = _PointSeries(point_id, self._retain, self._max_points)
                    device_map[point_id] = series
                series.append(sample)
            frames = self._frames.setdefault(frame.device_id, [])
            frames.append(frame)
            if len(frames) > self._frame_limit:
                del frames[: len(frames) - self._frame_limit]
            self._appended += 1

    def append_sample(self, device_id: str, sample: Sample) -> None:
        """写入单点。"""
        with self._lock:
            device_map = self._series.setdefault(device_id, {})
            series = device_map.get(sample.point_id)
            if series is None:
                series = _PointSeries(sample.point_id, self._retain, self._max_points)
                device_map[sample.point_id] = series
            series.append(sample)

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def window(self, device_id: str, point_id: str, seconds: float,
               end: Optional[float] = None, only_good: bool = True) -> SeriesWindow:
        """取最近 ``seconds`` 秒的窗口。"""
        end_ts = now_ts() if end is None else end
        return self.window_between(device_id, point_id, end_ts - seconds, end_ts, only_good)

    def window_between(self, device_id: str, point_id: str, start: float, end: float,
                       only_good: bool = True) -> SeriesWindow:
        """取指定时间区间的窗口。效果评估的前/后窗口由此获得。"""
        with self._lock:
            series = self._series.get(device_id, {}).get(point_id)
            if series is None:
                return SeriesWindow(point_id, [], [])
            return series.window(start, end, only_good)

    def last_n(self, device_id: str, point_id: str, n: int,
               only_good: bool = True) -> SeriesWindow:
        with self._lock:
            series = self._series.get(device_id, {}).get(point_id)
            if series is None:
                return SeriesWindow(point_id, [], [])
            return series.last_n(n, only_good)

    def latest(self, device_id: str, point_id: str) -> Optional[float]:
        with self._lock:
            series = self._series.get(device_id, {}).get(point_id)
            if series is None:
                return None
            item = series.latest_sample()
            return item[1] if item else None

    def latest_frame(self, device_id: str) -> Optional[SampleFrame]:
        with self._lock:
            frames = self._frames.get(device_id)
            return frames[-1] if frames else None

    def frames_between(self, device_id: str, start: float, end: float) -> List[SampleFrame]:
        with self._lock:
            return [f for f in self._frames.get(device_id, []) if start <= f.timestamp < end]

    def waveform(self, device_id: str, point_id: str,
                 max_age: float = 60.0) -> Optional[List[float]]:
        """取最近一次的高频波形。"""
        with self._lock:
            series = self._series.get(device_id, {}).get(point_id)
            return series.waveform(max_age) if series else None

    # ------------------------------------------------------------------
    # 元信息
    # ------------------------------------------------------------------
    def point_ids(self, device_id: str) -> List[str]:
        with self._lock:
            return sorted(self._series.get(device_id, {}))

    def device_ids(self) -> List[str]:
        with self._lock:
            return sorted(self._series)

    def coverage(self, device_id: str) -> Dict[str, Any]:
        """缓冲覆盖情况，用于判断能否做效果评估。"""
        with self._lock:
            device_map = self._series.get(device_id, {})
            if not device_map:
                return {"points": 0, "oldest": None, "newest": None, "span": 0.0}
            oldest = min((s.timestamps[0] for s in device_map.values() if s.timestamps),
                         default=None)
            newest = max((s.timestamps[-1] for s in device_map.values() if s.timestamps),
                         default=None)
            span = (newest - oldest) if (oldest is not None and newest is not None) else 0.0
            return {
                "points": len(device_map),
                "oldest": oldest,
                "newest": newest,
                "span": round(span, 2),
                "total_samples": sum(len(s.values) for s in device_map.values()),
            }

    def clear(self, device_id: Optional[str] = None) -> None:
        with self._lock:
            if device_id is None:
                self._series.clear()
                self._frames.clear()
            else:
                self._series.pop(device_id, None)
                self._frames.pop(device_id, None)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            total = sum(len(s.values) for m in self._series.values() for s in m.values())
            return {
                "devices": len(self._series),
                "series": sum(len(m) for m in self._series.values()),
                "samples": total,
                "frames_appended": self._appended,
                "retain_seconds": self._retain,
            }


__all__ = ["TimeSeriesBuffer", "SeriesWindow"]

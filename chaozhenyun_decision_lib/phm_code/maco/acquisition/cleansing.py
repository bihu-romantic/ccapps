"""
数据清洗模块
============

现场数据必然带噪：传感器掉线会出现死值，接线松动会出现尖峰，
变送器漂移会越量程，网络抖动会造成时标错乱。若不清洗直接进入健康评估，
会产生大量误报，最终导致操作工不再相信系统——这是工业软件落地失败的
典型原因，因此清洗必须放在链路最前端而非事后补救。

本模块按顺序执行五道处理，每道都可独立开关：

1. **量程校验** —— 超出变送器量程直接判坏值；
2. **死值检测** —— 连续 N 拍完全不变判定传感器僵死；
3. **尖峰剔除** —— 相对稳健标准差偏离超过 K 倍判为跳变；
4. **缺口插补** —— 短缺口线性插补，长缺口保留为坏值不臆造数据；
5. **时标对齐** —— 把同一帧内不同点的时标统一到帧时标。

清洗只改质量标记与被判定为噪声的数值，绝不静默修改正常数据，
并且每一次修改都写入 :class:`CleansingReport`，可回溯。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ..core.enums import Quality, SignalKind
from ..core.logging_setup import get_logger
from ..core.registry import DeviceProfile
from ..core.types import Sample, SampleFrame, SignalPoint
from ..core.utils import robust_stats
from .buffer import TimeSeriesBuffer

logger = get_logger("acquisition.cleansing")


@dataclass
class CleansingAction:
    """一次清洗动作的记录。"""

    point_id: str
    rule: str
    original: Optional[float]
    corrected: Optional[float]
    quality: Quality
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "point_id": self.point_id,
            "rule": self.rule,
            "original": self.original,
            "corrected": self.corrected,
            "quality": self.quality.code,
            "detail": self.detail,
        }


@dataclass
class CleansingReport:
    """一帧数据的清洗报告。"""

    device_id: str
    trace_id: str = ""
    actions: List[CleansingAction] = field(default_factory=list)
    total_points: int = 0

    @property
    def modified_count(self) -> int:
        return len(self.actions)

    @property
    def clean_ratio(self) -> float:
        if self.total_points == 0:
            return 1.0
        return 1.0 - self.modified_count / self.total_points

    def add(self, action: CleansingAction) -> None:
        self.actions.append(action)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "device_id": self.device_id,
            "trace_id": self.trace_id,
            "total_points": self.total_points,
            "modified_count": self.modified_count,
            "clean_ratio": round(self.clean_ratio, 4),
            "actions": [a.to_dict() for a in self.actions],
        }


class DataCleanser:
    """数据清洗器。

    :param options: 对应配置段 ``acquisition.cleansing``
    :param buffer: 时序缓冲，用于取历史窗口做死值/尖峰判定
    """

    def __init__(self, options: Optional[Mapping[str, Any]] = None,
                 buffer: Optional[TimeSeriesBuffer] = None):
        opts = dict(options or {})
        self.range_check: bool = bool(opts.get("range_check", True))
        self.dead_value_check: bool = bool(opts.get("dead_value_check", True))
        self.dead_value_window: int = int(opts.get("dead_value_window", 30))
        self.spike_check: bool = bool(opts.get("spike_check", True))
        self.spike_sigma: float = float(opts.get("spike_sigma", 5.0))
        self.interpolate_gap: bool = bool(opts.get("interpolate_gap", True))
        self.max_interpolate_points: int = int(opts.get("max_interpolate_points", 3))
        self.align_timestamp: bool = bool(opts.get("align_timestamp", True))
        self._buffer = buffer
        self._counters: Dict[str, int] = {}

    def attach_buffer(self, buffer: TimeSeriesBuffer) -> None:
        self._buffer = buffer

    # ------------------------------------------------------------------
    def cleanse(self, frame: SampleFrame, profile: DeviceProfile) -> CleansingReport:
        """就地清洗一帧数据，返回清洗报告。"""
        report = CleansingReport(device_id=frame.device_id, trace_id=frame.trace_id,
                                 total_points=len(frame.samples))
        for point_id in list(frame.samples):
            sample = frame.samples[point_id]
            point = profile.points.get(point_id)
            if point is None:
                continue
            cleaned = self._cleanse_sample(sample, point, frame, report)
            frame.samples[point_id] = cleaned
        if self.align_timestamp:
            self._align(frame, report)
        for action in report.actions:
            self._counters[action.rule] = self._counters.get(action.rule, 0) + 1
        if report.modified_count:
            logger.debug("设备 %s 清洗命中 %d 项（洁净率 %.1f%%）",
                         frame.device_id, report.modified_count, report.clean_ratio * 100)
        return report

    # ------------------------------------------------------------------
    def _cleanse_sample(self, sample: Sample, point: SignalPoint,
                        frame: SampleFrame, report: CleansingReport) -> Sample:
        # 通信失败的点直接标记，不做后续处理
        if sample.quality in (Quality.COMM_FAILURE, Quality.BAD):
            return sample
        value = sample.value

        # 1. 非有限值
        if value is None or not math.isfinite(value):
            report.add(CleansingAction(point.point_id, "non_finite", value, None,
                                       Quality.BAD, "数值为空或非有限值"))
            return Sample(point.point_id, math.nan, sample.timestamp, Quality.BAD,
                          sample.waveform)

        # 2. 量程校验
        if self.range_check and not point.in_range(value):
            report.add(CleansingAction(
                point.point_id, "range_check", value, None, Quality.OUT_OF_RANGE,
                f"超出量程 [{point.low_limit}, {point.high_limit}]",
            ))
            return Sample(point.point_id, value, sample.timestamp,
                          Quality.OUT_OF_RANGE, sample.waveform)

        # 设定值与开关量本就长期恒定，对它们做死值/尖峰判定必然误伤
        static_kind = point.kind in (SignalKind.SETPOINT, SignalKind.STATE)

        # 3. 死值检测
        if self.dead_value_check and not static_kind and self._buffer is not None:
            window = self._buffer.last_n(frame.device_id, point.point_id,
                                         self.dead_value_window)
            if len(window) >= self.dead_value_window:
                spread = max(window.values) - min(window.values)
                # 用量程的百万分之一作为 "完全不变" 的判据，兼容不同量纲
                span = point.high_limit - point.low_limit
                epsilon = abs(span) * 1e-6 if math.isfinite(span) else 1e-9
                if spread <= epsilon and abs(value - window.values[-1]) <= epsilon:
                    report.add(CleansingAction(
                        point.point_id, "dead_value", value, value, Quality.STALE,
                        f"连续 {len(window)} 拍无变化，疑似传感器僵死",
                    ))
                    return Sample(point.point_id, value, sample.timestamp,
                                  Quality.STALE, sample.waveform)

        # 4. 尖峰剔除
        if self.spike_check and not static_kind and self._buffer is not None:
            window = self._buffer.last_n(frame.device_id, point.point_id, 60)
            if len(window) >= 12:
                stats = window.stats()
                sigma = stats["mad"] if stats["mad"] > 1e-9 else stats["std"]
                if sigma > 1e-9:
                    deviation = abs(value - stats["median"]) / sigma
                    if deviation > self.spike_sigma:
                        replacement = stats["median"]
                        if self.interpolate_gap:
                            report.add(CleansingAction(
                                point.point_id, "spike", value, replacement,
                                Quality.SUBSTITUTED,
                                f"偏离中位数 {deviation:.1f}σ，已用中位数替代",
                            ))
                            return Sample(point.point_id, replacement, sample.timestamp,
                                          Quality.SUBSTITUTED, sample.waveform)
                        report.add(CleansingAction(
                            point.point_id, "spike", value, None, Quality.UNCERTAIN,
                            f"偏离中位数 {deviation:.1f}σ",
                        ))
                        return Sample(point.point_id, value, sample.timestamp,
                                      Quality.UNCERTAIN, sample.waveform)
        return sample

    def _align(self, frame: SampleFrame, report: CleansingReport) -> None:
        """把帧内偏离过大的时标统一到帧时标。"""
        base = frame.timestamp
        # 容差取帧内时标的稳健范围，避免把正常的采集时序差异当成异常
        offsets = [abs(s.timestamp - base) for s in frame.samples.values()]
        if not offsets:
            return
        tolerance = max(2.0, robust_stats(offsets)["median"] * 3 + 0.5)
        for point_id, sample in list(frame.samples.items()):
            if abs(sample.timestamp - base) > tolerance:
                report.add(CleansingAction(
                    point_id, "timestamp_align", sample.timestamp, base,
                    sample.quality,
                    f"时标偏离帧时标 {abs(sample.timestamp - base):.2f}s，已对齐",
                ))
                frame.samples[point_id] = Sample(point_id, sample.value, base,
                                                 sample.quality, sample.waveform)

    # ------------------------------------------------------------------
    def interpolate_series(self, timestamps: Sequence[float],
                           values: Sequence[float]) -> List[float]:
        """对序列中的短缺口做线性插补；长缺口保留 NaN。"""
        out = list(values)
        n = len(out)
        i = 0
        while i < n:
            if math.isfinite(out[i]):
                i += 1
                continue
            start = i
            while i < n and not math.isfinite(out[i]):
                i += 1
            end = i  # [start, end) 为缺口
            gap = end - start
            if gap > self.max_interpolate_points or start == 0 or end >= n:
                continue
            left_v, right_v = out[start - 1], out[end]
            left_t, right_t = timestamps[start - 1], timestamps[end]
            span = right_t - left_t
            if span <= 0:
                continue
            for k in range(start, end):
                ratio = (timestamps[k] - left_t) / span
                out[k] = left_v + (right_v - left_v) * ratio
        return out

    def counters(self) -> Dict[str, int]:
        """各清洗规则的累计命中次数。"""
        return dict(self._counters)

    def reset_counters(self) -> None:
        self._counters.clear()


__all__ = ["DataCleanser", "CleansingReport", "CleansingAction"]

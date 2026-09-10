"""
采集器与接入网关
================

**采集器**（:class:`Collector`）负责一台设备的周期取数：调用驱动读点、
做工程量换算与质量判定、执行清洗、写入缓冲、发布 ``FRAME_READY`` 事件。

**接入网关**（:class:`AcquisitionGateway`）统一管理多台设备、多种协议的
采集器，对外提供一个入口：注册设备 → 绑定驱动 → 启动采集。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from ..core.audit import ACTION_ACQUIRE, AuditTrail
from ..core.enums import EventTopic, Quality
from ..core.event_bus import EventBus
from ..core.logging_setup import get_logger
from ..core.registry import DeviceProfile, DeviceRegistry
from ..core.scheduler import Scheduler
from ..core.types import Sample, SampleFrame, new_id, now_ts
from .buffer import TimeSeriesBuffer
from .cleansing import DataCleanser
from .drivers.base import ProtocolDriver

logger = get_logger("acquisition.collector")


class Collector:
    """单设备采集器。"""

    def __init__(
        self,
        profile: DeviceProfile,
        driver: ProtocolDriver,
        buffer: TimeSeriesBuffer,
        cleanser: DataCleanser,
        bus: EventBus,
        audit: Optional[AuditTrail] = None,
        stale_threshold_s: float = 10.0,
    ):
        self.profile = profile
        self.driver = driver
        self.buffer = buffer
        self.cleanser = cleanser
        self.bus = bus
        self.audit = audit
        self.stale_threshold_s = stale_threshold_s
        self.last_frame: Optional[SampleFrame] = None
        self.cycles = 0
        self.failures = 0
        self._lock = threading.RLock()

    def poll_once(self) -> Optional[SampleFrame]:
        """执行一次采集。返回清洗后的数据帧。"""
        points = list(self.profile.points.values())
        if not points:
            return None
        start = time.perf_counter()
        with self._lock:
            self.cycles += 1
            try:
                readings = self.driver.read_points(points)
            except Exception as exc:  # noqa: BLE001 - 单设备故障不拖垮系统
                self.failures += 1
                logger.error("设备 %s 采集失败: %s", self.profile.device_id, exc)
                self.bus.publish(EventTopic.SYSTEM_ERROR,
                                 {"stage": "acquire", "error": str(exc)},
                                 device_id=self.profile.device_id)
                return None

            frame = SampleFrame(device_id=self.profile.device_id,
                                trace_id=new_id("trace"))
            now = now_ts()
            for reading in readings:
                point = self.profile.points.get(reading.point_id)
                if point is None:
                    continue
                if not reading.ok or reading.raw_value is None:
                    frame.put(Sample(reading.point_id, float("nan"), now,
                                     Quality.COMM_FAILURE))
                    continue
                value = point.scale_raw(reading.raw_value)
                quality = Quality.GOOD
                if now - reading.timestamp > self.stale_threshold_s:
                    quality = Quality.STALE
                frame.put(Sample(reading.point_id, value, reading.timestamp,
                                 quality, reading.waveform))
            frame.acquire_cost_ms = (time.perf_counter() - start) * 1000.0

            report = self.cleanser.cleanse(frame, self.profile)
            self.buffer.append_frame(frame)
            self.last_frame = frame

        self.bus.publish(EventTopic.FRAME_READY, frame,
                         device_id=frame.device_id, trace_id=frame.trace_id,
                         source="collector")
        if report.modified_count:
            self.bus.publish(EventTopic.FRAME_CLEANSED, report,
                             device_id=frame.device_id, trace_id=frame.trace_id)
        if self.audit is not None and self.cycles % 60 == 1:
            # 采集属高频动作，按抽样写审计，避免账本膨胀
            self.audit.record(ACTION_ACQUIRE, device_id=frame.device_id,
                              trace_id=frame.trace_id,
                              detail={"points": len(frame.samples),
                                      "good_ratio": round(frame.good_ratio, 3),
                                      "cost_ms": round(frame.acquire_cost_ms, 2)})
        return frame

    def write(self, point_id: str, value: float) -> bool:
        """通过驱动写单点（控制层调用）。"""
        point = self.profile.point(point_id)
        return self.driver.write_point(point, point.unscale(value))

    def read_back(self, point_id: str) -> Optional[float]:
        """回读单点当前值。"""
        point = self.profile.point(point_id)
        readings = self.driver.read_points([point])
        if not readings or not readings[0].ok or readings[0].raw_value is None:
            return None
        return point.scale_raw(readings[0].raw_value)

    def health(self) -> Dict[str, Any]:
        return {
            "device_id": self.profile.device_id,
            "cycles": self.cycles,
            "failures": self.failures,
            "last_frame_ts": self.last_frame.timestamp if self.last_frame else None,
            "good_ratio": round(self.last_frame.good_ratio, 3) if self.last_frame else 0.0,
            "driver": self.driver.health(),
        }


class AcquisitionGateway:
    """多设备接入网关。"""

    def __init__(
        self,
        registry: DeviceRegistry,
        buffer: TimeSeriesBuffer,
        bus: EventBus,
        scheduler: Scheduler,
        cleanser: Optional[DataCleanser] = None,
        audit: Optional[AuditTrail] = None,
        poll_interval_ms: int = 1000,
    ):
        self.registry = registry
        self.buffer = buffer
        self.bus = bus
        self.scheduler = scheduler
        self.cleanser = cleanser or DataCleanser(buffer=buffer)
        self.audit = audit
        self.poll_interval_ms = poll_interval_ms
        self._collectors: Dict[str, Collector] = {}
        self._task_ids: Dict[str, str] = {}

    def bind(self, device_id: str, driver: ProtocolDriver) -> Collector:
        """把驱动绑定到设备，创建采集器。"""
        profile = self.registry.get(device_id)
        collector = Collector(profile, driver, self.buffer, self.cleanser,
                              self.bus, self.audit)
        self._collectors[device_id] = collector
        logger.info("设备 %s 已绑定 %s 驱动", device_id, driver.protocol)
        return collector

    def collector(self, device_id: str) -> Optional[Collector]:
        return self._collectors.get(device_id)

    def start(self) -> None:
        """按各设备的采集周期注册周期任务。"""
        for device_id, collector in self._collectors.items():
            interval = self.poll_interval_ms / 1000.0
            intervals = [p.sample_interval_ms for p in collector.profile.points.values()]
            if intervals:
                interval = max(min(intervals) / 1000.0, 0.05)
            task_id = self.scheduler.every(
                interval, collector.poll_once,
                name=f"acquire:{device_id}", first_delay=0.0,
            )
            self._task_ids[device_id] = task_id
            logger.info("设备 %s 采集任务已注册，周期 %.2fs", device_id, interval)

    def stop(self) -> None:
        for device_id, task_id in self._task_ids.items():
            self.scheduler.cancel(task_id)
            collector = self._collectors.get(device_id)
            if collector:
                collector.driver.disconnect()
        self._task_ids.clear()

    def poll_all(self) -> Dict[str, Optional[SampleFrame]]:
        """同步采集所有设备一次（测试与演示用）。"""
        return {d: c.poll_once() for d, c in self._collectors.items()}

    def health(self) -> List[Dict[str, Any]]:
        return [c.health() for c in self._collectors.values()]


__all__ = ["Collector", "AcquisitionGateway"]

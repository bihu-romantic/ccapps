"""
仿真驱动
========

把设备仿真模型接入标准驱动接口，使得系统在没有现场设备时也能完整跑通
"采集 → 评估 → 优化 → 下发 → 反馈" 全链路。这是出厂自测、算法验证与
演示培训的基础设施，其接口与真实驱动完全一致，因此切换到现场只需改配置。

仿真驱动同时承担 "控制回写" 职责：优化建议下发的参数会写回仿真模型，
模型据此改变后续输出，从而形成真实的闭环反馈——这是验证反馈学习模块
是否有效的前提。
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, runtime_checkable

from ...core.errors import ConnectionFailure
from ...core.logging_setup import get_logger
from ...core.registry import DRIVER_REGISTRY
from ...core.types import SignalPoint, now_ts
from .base import ProtocolDriver, RawReading

logger = get_logger("acquisition.simulator")


@runtime_checkable
class SignalSource(Protocol):
    """仿真数据源接口。任何实现该协议的对象都可挂到仿真驱动上。"""

    def read_point(self, point_id: str) -> Optional[float]:
        """读一个标量测点当前值。"""

    def read_waveform(self, point_id: str) -> Optional[Sequence[float]]:
        """读一个高频测点的整段波形；非波形点返回 None。"""

    def write_point(self, point_id: str, value: float) -> bool:
        """写一个可写测点（控制回写）。"""

    def step(self, dt_seconds: float) -> None:
        """推进仿真时间。"""


@DRIVER_REGISTRY.decorator("simulator")
class SimulatorDriver(ProtocolDriver):
    """仿真驱动。

    :param options:
        ``source``      实现 :class:`SignalSource` 的仿真模型实例
        ``auto_step``   读取时是否自动推进仿真时间
        ``step_seconds`` 每次自动推进的时长
        ``fail_rate``   人为注入的读失败概率，用于测试容错路径
    """

    protocol = "simulator"

    def __init__(self, name: str, options: Optional[Mapping[str, Any]] = None):
        super().__init__(name, options)
        self._source: Optional[SignalSource] = self.options.get("source")
        self._auto_step = bool(self.options.get("auto_step", True))
        self._step_seconds = float(self.options.get("step_seconds", 1.0))
        self._fail_rate = float(self.options.get("fail_rate", 0.0))
        self._lock = threading.RLock()
        self._read_seq = 0

    def attach_source(self, source: SignalSource) -> None:
        """运行期挂载/替换仿真源。"""
        with self._lock:
            self._source = source

    def _do_connect(self) -> None:
        if self._source is None:
            raise ConnectionFailure("仿真驱动未挂载数据源", {"driver": self.name})

    def _do_disconnect(self) -> None:
        return None

    def _do_read(self, points: Sequence[SignalPoint]) -> List[RawReading]:
        with self._lock:
            source = self._source
            if source is None:
                raise ConnectionFailure("仿真驱动未挂载数据源", {"driver": self.name})
            if self._auto_step:
                source.step(self._step_seconds)
            self._read_seq += 1
            seq = self._read_seq

        out: List[RawReading] = []
        for idx, point in enumerate(points):
            # 用确定性方式注入失败，保证测试可复现（不使用随机数）
            if self._fail_rate > 0:
                period = max(int(round(1.0 / self._fail_rate)), 1)
                if (seq * 31 + idx * 17) % period == 0:
                    out.append(RawReading(point.point_id, None, ok=False,
                                          error="仿真注入的通信故障"))
                    continue
            waveform = None
            if point.kind.is_dynamic:
                waveform = source.read_waveform(point.point_id)
            value = source.read_point(point.point_id)
            if value is None and waveform is None:
                out.append(RawReading(point.point_id, None, ok=False,
                                      error="仿真源中不存在该测点"))
                continue
            if value is None and waveform is not None:
                # 波形点的标量值取有效值（RMS 在特征层再算，这里给瞬时代表值）
                value = float(sum(abs(v) for v in waveform) / max(len(waveform), 1))
            out.append(RawReading(point.point_id, float(value),  # type: ignore[arg-type]
                                  timestamp=now_ts(), waveform=waveform))
        return out

    def _do_write(self, point: SignalPoint, raw_value: float) -> bool:
        with self._lock:
            if self._source is None:
                raise ConnectionFailure("仿真驱动未挂载数据源", {"driver": self.name})
            ok = self._source.write_point(point.point_id, float(raw_value))
        logger.debug("仿真写入 %s = %.4f -> %s", point.point_id, raw_value, ok)
        return bool(ok)

    def describe(self) -> Dict[str, Any]:
        return {
            "protocol": self.protocol,
            "source": type(self._source).__name__ if self._source else None,
            "auto_step": self._auto_step,
            "step_seconds": self._step_seconds,
            "fail_rate": self._fail_rate,
            "connected": self.connected,
        }


__all__ = ["SimulatorDriver", "SignalSource"]

"""
控制系统适配器
==============

对应系统功能四的下半段：把优化建议真正写进 **PLC / SCADA / DCS**。

与数据接入驱动分开的原因：读和写的风险等级完全不同。读错一个点只是数据不准，
写错一个点可能导致停机甚至事故。因此写通道单独建模，强制具备四项能力：

1. **写前锁定** —— 确认目标点当前处于 "远程/自动" 且允许上位机写入；
2. **写后回读** —— 写完立即读回，比对是否真的生效；
3. **原值留存** —— 写前记录原值，任何一步失败都能原路回滚；
4. **写审计** —— 每次写值留痕，包含谁、何时、把什么改成了什么。

三种适配器对应三类现场：``plc`` 直连 PLC 寄存器，``scada`` 经 SCADA 点表，
``dcs`` 经 DCS 的 OPC UA 接口。它们共享同一套安全语义，差别只在寻址方式。
"""

from __future__ import annotations

import abc
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ..acquisition.drivers.base import ProtocolDriver
from ..core.errors import DispatchFailure, WritePermissionDenied
from ..core.logging_setup import get_logger
from ..core.registry import CONTROL_ADAPTER_REGISTRY, DeviceProfile
from ..core.types import SignalPoint, now_ts

logger = get_logger("control.adapter")


@dataclass
class WriteOutcome:
    """一次写操作的结果。"""

    point_id: str
    requested: float
    previous: Optional[float] = None
    readback: Optional[float] = None
    success: bool = False
    error: str = ""
    duration_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "point_id": self.point_id,
            "requested": self.requested,
            "previous": self.previous,
            "readback": self.readback,
            "success": self.success,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 3),
        }


class ControlAdapter(abc.ABC):
    """控制系统适配器基类。"""

    system_type = "abstract"

    def __init__(self, name: str, profile: DeviceProfile,
                 options: Optional[Mapping[str, Any]] = None):
        self.name = name
        self.profile = profile
        self.options: Dict[str, Any] = dict(options or {})
        self.readback_tolerance = float(self.options.get("readback_tolerance", 0.02))
        self.readback_timeout_s = float(self.options.get("readback_timeout_s", 10.0))
        self.readback_delay_s = float(self.options.get("readback_delay_s", 0.5))
        self._lock = threading.RLock()
        self.write_count = 0
        self.fail_count = 0

    # -- 子类实现 -------------------------------------------------------
    @abc.abstractmethod
    def _read(self, point: SignalPoint) -> Optional[float]: ...

    @abc.abstractmethod
    def _write(self, point: SignalPoint, value: float) -> bool: ...

    def remote_mode(self) -> bool:
        """目标系统是否处于允许上位机写入的模式。默认允许，子类可覆盖。"""
        return True

    # -- 通用写流程 -----------------------------------------------------
    def write(self, point_id: str, value: float) -> WriteOutcome:
        """执行 "锁定 → 留存原值 → 写入 → 回读校验" 的完整流程。"""
        start = time.perf_counter()
        point = self.profile.point(point_id)
        outcome = WriteOutcome(point_id=point_id, requested=value)

        if not point.writable:
            outcome.error = "该测点在测点表中标记为不可写"
            raise WritePermissionDenied(outcome.error, context={"point_id": point_id})
        if not self.remote_mode():
            outcome.error = "目标控制系统当前不在远程/自动模式，拒绝写入"
            raise WritePermissionDenied(outcome.error, context={"point_id": point_id})

        with self._lock:
            self.write_count += 1
            outcome.previous = self._read(point)
            try:
                ok = self._write(point, value)
            except Exception as exc:  # noqa: BLE001 - 统一转成下发失败
                self.fail_count += 1
                outcome.error = f"{type(exc).__name__}: {exc}"
                outcome.duration_ms = (time.perf_counter() - start) * 1000.0
                raise DispatchFailure(f"写入 {point_id} 失败: {exc}",
                                      {"point_id": point_id}) from exc
            if not ok:
                self.fail_count += 1
                outcome.error = "目标系统返回写入失败"
                outcome.duration_ms = (time.perf_counter() - start) * 1000.0
                return outcome

            outcome.readback = self._wait_readback(point, value)
            outcome.success = self._matches(value, outcome.readback)
            if not outcome.success:
                self.fail_count += 1
                outcome.error = (f"回读不一致：期望 {value:.4f}，"
                                 f"实际 {outcome.readback}")
        outcome.duration_ms = (time.perf_counter() - start) * 1000.0
        logger.info("写入 %s: %s → %.4f（回读 %s，%s）", point_id,
                    f"{outcome.previous:.4f}" if outcome.previous is not None else "未知",
                    value, outcome.readback, "成功" if outcome.success else "失败")
        return outcome

    def _wait_readback(self, point: SignalPoint, expected: float) -> Optional[float]:
        """轮询回读，直到值稳定或超时。"""
        deadline = now_ts() + self.readback_timeout_s
        last: Optional[float] = None
        while now_ts() < deadline:
            time.sleep(min(self.readback_delay_s, 0.5))
            last = self._read(point)
            if last is not None and self._matches(expected, last):
                return last
        return last

    def _matches(self, expected: float, actual: Optional[float]) -> bool:
        if actual is None:
            return False
        scale = max(abs(expected), 1e-6)
        return abs(actual - expected) / scale <= self.readback_tolerance

    def rollback(self, outcome: WriteOutcome) -> bool:
        """把点位写回原值。"""
        if outcome.previous is None:
            logger.error("测点 %s 无原值记录，无法回滚", outcome.point_id)
            return False
        point = self.profile.point(outcome.point_id)
        try:
            ok = self._write(point, outcome.previous)
        except Exception as exc:  # noqa: BLE001
            logger.error("回滚 %s 失败: %s", outcome.point_id, exc)
            return False
        logger.warning("已回滚 %s 至原值 %.4f", outcome.point_id, outcome.previous)
        return bool(ok)

    def read(self, point_id: str) -> Optional[float]:
        return self._read(self.profile.point(point_id))

    def health(self) -> Dict[str, Any]:
        return {
            "name": self.name, "system_type": self.system_type,
            "writes": self.write_count, "failures": self.fail_count,
            "remote_mode": self.remote_mode(),
            "readback_tolerance": self.readback_tolerance,
        }


class _DriverBackedAdapter(ControlAdapter):
    """以数据接入驱动为通道的适配器基类（PLC / DCS 共用）。"""

    def __init__(self, name: str, profile: DeviceProfile, driver: ProtocolDriver,
                 options: Optional[Mapping[str, Any]] = None):
        super().__init__(name, profile, options)
        self.driver = driver
        #: 远程/自动模式的判定点位（可选）
        self.mode_point_id: str = str(self.options.get("mode_point_id", ""))
        self.mode_remote_value: float = float(self.options.get("mode_remote_value", 1.0))

    def _read(self, point: SignalPoint) -> Optional[float]:
        readings = self.driver.read_points([point])
        if not readings or not readings[0].ok or readings[0].raw_value is None:
            return None
        return point.scale_raw(readings[0].raw_value)

    def _write(self, point: SignalPoint, value: float) -> bool:
        return self.driver.write_point(point, point.unscale(value))

    def remote_mode(self) -> bool:
        if not self.mode_point_id:
            return True
        point = self.profile.points.get(self.mode_point_id)
        if point is None:
            return True
        value = self._read(point)
        if value is None:
            logger.warning("无法读取模式点 %s，按不允许写入处理", self.mode_point_id)
            return False
        return abs(value - self.mode_remote_value) < 1e-6


@CONTROL_ADAPTER_REGISTRY.decorator("plc")
class PlcAdapter(_DriverBackedAdapter):
    """PLC 直连适配器（Modbus / S7 通道）。

    典型用法：优化建议中的 "风机变频器频率设定" 直接写到 PLC 保持寄存器，
    PLC 内部的 PID 与联锁逻辑保持不变——系统只改设定值，不接管调节回路，
    这是与现场控制系统安全共处的基本原则。
    """

    system_type = "plc"


@CONTROL_ADAPTER_REGISTRY.decorator("dcs")
class DcsAdapter(_DriverBackedAdapter):
    """DCS 适配器（一般经 OPC UA 通道）。

    DCS 场景额外要求：写入前确认回路处于 "串级/远程设定" 状态，
    否则设定值会被就地控制器覆盖，形成 "写进去但不起作用" 的假象。
    """

    system_type = "dcs"

    def __init__(self, name: str, profile: DeviceProfile, driver: ProtocolDriver,
                 options: Optional[Mapping[str, Any]] = None):
        super().__init__(name, profile, driver, options)
        #: 回路状态点（CAS/AUTO/MAN）
        self.loop_status_point: str = str(self.options.get("loop_status_point", ""))
        self.loop_allow_values: List[float] = [
            float(v) for v in self.options.get("loop_allow_values", [2.0, 3.0])
        ]

    def remote_mode(self) -> bool:
        if not super().remote_mode():
            return False
        if not self.loop_status_point:
            return True
        point = self.profile.points.get(self.loop_status_point)
        if point is None:
            return True
        value = self._read(point)
        if value is None:
            return False
        return any(abs(value - v) < 1e-6 for v in self.loop_allow_values)


@CONTROL_ADAPTER_REGISTRY.decorator("scada")
class ScadaAdapter(ControlAdapter):
    """SCADA 适配器：通过 SCADA 平台的点表接口间接下发。

    与直连 PLC 相比多一层：SCADA 通常有自己的操作权限与命令确认机制，
    因此本适配器把写操作建模为 "提交命令 → 等待 SCADA 确认 → 回读点值"。
    命令通道由 ``command_sink`` 注入，可对接不同厂商的 SCADA 接口。
    """

    system_type = "scada"

    def __init__(self, name: str, profile: DeviceProfile,
                 command_sink: Optional[Any] = None,
                 options: Optional[Mapping[str, Any]] = None):
        super().__init__(name, profile, options)
        #: 需实现 submit(tag, value) -> bool 与 read(tag) -> Optional[float]
        self.command_sink = command_sink
        self._local: Dict[str, float] = {}
        self.operator: str = str(self.options.get("operator", "maco-system"))

    def _read(self, point: SignalPoint) -> Optional[float]:
        if self.command_sink is not None and hasattr(self.command_sink, "read"):
            return self.command_sink.read(point.address or point.point_id)
        return self._local.get(point.point_id)

    def _write(self, point: SignalPoint, value: float) -> bool:
        tag = point.address or point.point_id
        if self.command_sink is not None and hasattr(self.command_sink, "submit"):
            ok = bool(self.command_sink.submit(tag, value))
            if ok:
                self._local[point.point_id] = value
            return ok
        # 无 SCADA 通道时退化为本地登记，供离线联调
        self._local[point.point_id] = value
        return True

    def health(self) -> Dict[str, Any]:
        base = super().health()
        base["sink"] = type(self.command_sink).__name__ if self.command_sink else "local"
        base["operator"] = self.operator
        return base


def build_adapter(system_type: str, name: str, profile: DeviceProfile,
                  **kwargs: Any) -> ControlAdapter:
    """按控制系统类型构造适配器。"""
    return CONTROL_ADAPTER_REGISTRY.create(system_type, name, profile, **kwargs)


__all__ = [
    "ControlAdapter", "PlcAdapter", "DcsAdapter", "ScadaAdapter",
    "WriteOutcome", "build_adapter",
]

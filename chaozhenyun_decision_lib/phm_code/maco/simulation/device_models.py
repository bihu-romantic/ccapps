"""
设备仿真模型
============

为离线联调、算法验证与演示提供可运行的虚拟设备。模型不追求高保真，
但必须具备三个真实特性，否则验证不出闭环是否有效：

1. **工况会变** —— 负载按预设曲线切换，系统必须能识别并分别建立基线；
2. **参数真起作用** —— 写下去的设定值确实改变后续输出（含一阶惯性延迟），
   否则反馈学习永远学不到东西；
3. **设备会劣化** —— 振动随运行时间缓慢增长，可注入轴承故障冲击，
   用于验证健康评估与劣化趋势能不能报出来。

内置两类模型：:class:`FanSimulator`（风机/泵类，遵循相似定律）与
:class:`CompressorSimulator`（压缩机，含加卸载逻辑）。
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..core.logging_setup import get_logger

logger = get_logger("simulation.device")


@dataclass
class LoadSegment:
    """负载曲线的一段。"""

    duration_s: float
    target_load: float
    label: str = ""


class BaseDeviceSimulator:
    """设备仿真基类，实现 ``SignalSource`` 协议。"""

    def __init__(self, device_id: str, seed: int = 20240101,
                 sampling_rate_hz: float = 5120.0, waveform_len: int = 2048):
        self.device_id = device_id
        self.rng = np.random.default_rng(seed)
        self.sampling_rate_hz = sampling_rate_hz
        self.waveform_len = waveform_len
        self.elapsed = 0.0
        self._lock = threading.RLock()
        #: 可写设定值（由控制层写入）
        self.setpoints: Dict[str, float] = {}
        #: 实际值（一阶跟随设定值）
        self.actuals: Dict[str, float] = {}
        #: 负载曲线
        self.load_profile: List[LoadSegment] = []
        self._profile_pos = 0.0
        #: 劣化程度 0~1
        self.degradation = 0.0
        self.degradation_rate_per_hour = 0.0
        #: 注入的故障：``bearing_outer`` / ``unbalance`` / ``overheat`` / None
        self.injected_fault: Optional[str] = None
        self.fault_severity = 0.0
        self.write_log: List[Tuple[float, str, float]] = []

    # ------------------------------------------------------------------
    def set_load_profile(self, segments: Sequence[LoadSegment]) -> None:
        self.load_profile = list(segments)
        self._profile_pos = 0.0

    def inject_fault(self, fault: str, severity: float = 0.5) -> None:
        """注入故障，用于验证健康评估是否报得出来。"""
        self.injected_fault = fault
        self.fault_severity = max(0.0, min(severity, 1.0))
        logger.info("仿真设备 %s 注入故障 %s（严重度 %.2f）",
                    self.device_id, fault, severity)

    def clear_fault(self) -> None:
        self.injected_fault = None
        self.fault_severity = 0.0

    def current_load_target(self) -> Tuple[float, str]:
        """按负载曲线返回当前目标负载率。"""
        if not self.load_profile:
            return 0.75, "恒定工况"
        total = sum(s.duration_s for s in self.load_profile)
        if total <= 0:
            return 0.75, "恒定工况"
        pos = self.elapsed % total
        acc = 0.0
        for seg in self.load_profile:
            acc += seg.duration_s
            if pos < acc:
                return seg.target_load, seg.label or "工况段"
        last = self.load_profile[-1]
        return last.target_load, last.label

    # ------------------------------------------------------------------
    def step(self, dt_seconds: float) -> None:
        """推进仿真时间。"""
        with self._lock:
            self.elapsed += max(dt_seconds, 0.0)
            self.degradation = min(
                1.0, self.degradation
                + self.degradation_rate_per_hour * dt_seconds / 3600.0
            )
            self._advance(dt_seconds)

    def _advance(self, dt: float) -> None:
        raise NotImplementedError

    def _first_order(self, key: str, target: float, dt: float,
                     tau: float = 8.0) -> float:
        """一阶惯性跟随：设定值不会瞬间生效，这是现场的真实行为。"""
        current = self.actuals.get(key, target)
        alpha = 1.0 - math.exp(-max(dt, 1e-6) / max(tau, 1e-6))
        value = current + (target - current) * alpha
        self.actuals[key] = value
        return value

    # ------------------------------------------------------------------
    # SignalSource 协议
    # ------------------------------------------------------------------
    def read_point(self, point_id: str) -> Optional[float]:
        with self._lock:
            return self.actuals.get(point_id)

    def read_waveform(self, point_id: str) -> Optional[Sequence[float]]:
        with self._lock:
            if not point_id.startswith("vib"):
                return None
            return self._generate_waveform(point_id)

    def write_point(self, point_id: str, value: float) -> bool:
        with self._lock:
            self.setpoints[point_id] = float(value)
            self.write_log.append((self.elapsed, point_id, float(value)))
            if len(self.write_log) > 500:
                del self.write_log[:100]
        logger.debug("仿真设备 %s 收到设定 %s = %.3f", self.device_id, point_id, value)
        return True

    def _generate_waveform(self, point_id: str) -> List[float]:
        """合成一段振动波形：基频 + 谐波 + 轴承冲击 + 随机噪声。"""
        rpm = self.actuals.get("speed", 1450.0)
        shaft_hz = max(rpm / 60.0, 0.1)
        n = self.waveform_len
        t = np.arange(n) / self.sampling_rate_hz
        load = self.actuals.get("load_ratio", 0.7)

        base_amp = 0.6 + 1.4 * load + 2.5 * self.degradation
        signal = base_amp * np.sin(2 * math.pi * shaft_hz * t)
        signal += base_amp * 0.25 * np.sin(2 * math.pi * 2 * shaft_hz * t + 0.7)
        signal += base_amp * 0.12 * np.sin(2 * math.pi * 3 * shaft_hz * t + 1.4)

        if self.injected_fault == "unbalance":
            signal += self.fault_severity * 4.0 * np.sin(2 * math.pi * shaft_hz * t + 0.2)
        elif self.injected_fault == "misalignment":
            signal += self.fault_severity * 3.0 * np.sin(2 * math.pi * 2 * shaft_hz * t)
        elif self.injected_fault in ("bearing_outer", "bearing_inner"):
            # 轴承故障表现为周期性冲击串，包络谱上出现特征频率
            bpf = shaft_hz * (3.58 if self.injected_fault == "bearing_outer" else 5.42)
            period = max(int(self.sampling_rate_hz / max(bpf, 1.0)), 4)
            impulse = np.zeros(n)
            decay = np.exp(-np.arange(period) / (period / 6.0))
            for start in range(0, n, period):
                end = min(start + period, n)
                impulse[start:end] += decay[: end - start]
            carrier = np.sin(2 * math.pi * 2400.0 * t)
            signal += self.fault_severity * 6.0 * impulse * carrier

        noise = self.rng.normal(0, 0.15 + 0.1 * self.degradation, size=n)
        return (signal + noise).tolist()

    # ------------------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            load, label = self.current_load_target()
            return {
                "device_id": self.device_id,
                "elapsed_s": round(self.elapsed, 1),
                "load_target": round(load, 3),
                "segment": label,
                "degradation": round(self.degradation, 4),
                "injected_fault": self.injected_fault,
                "fault_severity": round(self.fault_severity, 3),
                "setpoints": {k: round(v, 3) for k, v in self.setpoints.items()},
                "actuals": {k: round(v, 3) for k, v in self.actuals.items()},
                "writes": len(self.write_log),
            }


class FanSimulator(BaseDeviceSimulator):
    """风机 / 水泵仿真模型。

    遵循相似定律：流量 ∝ 转速，功率 ∝ 转速³。这带来一个真实的优化空间——
    转速降 10% 流量只降 10%，功率却降约 27%，因此在流量有余量时降速能显著省电。
    系统能否自动发现这一点，正是本仿真要验证的事。
    """

    def __init__(self, device_id: str, rated_speed_rpm: float = 1480.0,
                 rated_power_kw: float = 55.0, rated_flow: float = 1200.0,
                 rated_current_a: float = 105.0, seed: int = 20240101):
        super().__init__(device_id, seed)
        self.rated_speed = rated_speed_rpm
        self.rated_power = rated_power_kw
        self.rated_flow = rated_flow
        self.rated_current = rated_current_a
        self.ambient_c = 26.0
        self.degradation_rate_per_hour = 0.004
        # 初始设定：频率 45Hz（对应约 90% 转速）、导叶开度 80%
        self.setpoints = {"freq_setpoint": 45.0, "vane_setpoint": 80.0}
        self.actuals = {
            "speed": rated_speed_rpm * 0.9,
            "load_ratio": 0.7,
            "bearing_temp": 45.0,
            "motor_temp": 55.0,
            "ambient_temp": self.ambient_c,
            "current": rated_current_a * 0.7,
            "power": rated_power_kw * 0.7,
            "flow": rated_flow * 0.85,
            "vib_de": 2.2,
            "vib_nde": 1.8,
            "freq_setpoint": 45.0,
            "vane_setpoint": 80.0,
        }

    def _advance(self, dt: float) -> None:
        freq = self.setpoints.get("freq_setpoint", 45.0)
        vane = self.setpoints.get("vane_setpoint", 80.0)
        self.actuals["freq_setpoint"] = freq
        self.actuals["vane_setpoint"] = vane

        # 转速跟随变频器频率（50Hz 对应额定转速）
        speed_target = self.rated_speed * (freq / 50.0)
        speed = self._first_order("speed", speed_target, dt, tau=6.0)
        speed_ratio = speed / max(self.rated_speed, 1e-6)

        # 系统需求负载
        demand, _label = self.current_load_target()

        # 流量：转速与导叶开度共同决定
        flow_target = self.rated_flow * speed_ratio * (0.35 + 0.65 * vane / 100.0)
        flow = self._first_order("flow", flow_target, dt, tau=10.0)

        # 功率：三次方律 + 节流损失 + 劣化附加
        throttle_loss = 1.0 + 0.35 * (1.0 - vane / 100.0) ** 2
        eff_penalty = 1.0 + 0.28 * self.degradation
        power_target = (self.rated_power * (speed_ratio ** 3)
                        * throttle_loss * eff_penalty)
        # 负载需求不足时电机轻载运行，效率进一步下降
        if demand < speed_ratio * 0.85:
            power_target *= 1.0 + 0.15 * (speed_ratio - demand)
        power = self._first_order("power", max(power_target, 0.5), dt, tau=5.0)

        self.actuals["load_ratio"] = power / max(self.rated_power, 1e-6)
        self.actuals["current"] = self.rated_current * (power / max(self.rated_power, 1e-6)) \
            * (1.0 + 0.05 * self.degradation)

        # 温度：热平衡一阶模型
        heat = 25.0 * (power / max(self.rated_power, 1e-6)) ** 1.2
        fault_heat = 12.0 * self.fault_severity if self.injected_fault == "overheat" else 0.0
        bearing_target = self.ambient_c + heat * 0.9 + 18.0 * self.degradation + fault_heat
        motor_target = self.ambient_c + heat * 1.25 + 10.0 * self.degradation
        self._first_order("bearing_temp", bearing_target, dt, tau=180.0)
        self._first_order("motor_temp", motor_target, dt, tau=150.0)
        self.actuals["ambient_temp"] = self.ambient_c + self.rng.normal(0, 0.15)

        # 振动有效值：随转速与劣化增长
        vib_base = 0.9 + 2.6 * (speed_ratio ** 1.8) + 5.0 * self.degradation
        if self.injected_fault in ("unbalance", "misalignment",
                                   "bearing_outer", "bearing_inner"):
            vib_base += 4.5 * self.fault_severity
        self.actuals["vib_de"] = max(vib_base + self.rng.normal(0, 0.12), 0.1)
        self.actuals["vib_nde"] = max(vib_base * 0.78 + self.rng.normal(0, 0.1), 0.1)


class CompressorSimulator(BaseDeviceSimulator):
    """空压机仿真模型：含加卸载逻辑与储气罐压力动态。

    空压机的优化空间在于 "加卸载压力带"：压力带设得太窄会频繁加卸载，
    空载能耗白白浪费；设得太宽则压力波动大影响用气。系统需要在
    保证供气的前提下找到能耗最低的压力带，这与风机是完全不同的优化结构，
    用于验证系统的多场景适配能力。
    """

    def __init__(self, device_id: str, rated_power_kw: float = 75.0,
                 rated_flow: float = 13.0, seed: int = 20240202):
        super().__init__(device_id, seed)
        self.rated_power = rated_power_kw
        self.rated_flow = rated_flow
        self.ambient_c = 28.0
        self.degradation_rate_per_hour = 0.003
        self.tank_pressure = 0.72
        self.loaded = True
        self.setpoints = {"load_pressure": 0.65, "unload_pressure": 0.80}
        self.actuals = {
            "speed": 2950.0,
            "load_ratio": 0.8,
            "tank_pressure": 0.72,
            "outlet_temp": 78.0,
            "ambient_temp": self.ambient_c,
            "current": 128.0,
            "power": rated_power_kw * 0.8,
            "flow": rated_flow * 0.8,
            "vib_de": 2.6,
            "load_pressure": 0.65,
            "unload_pressure": 0.80,
            "load_cycles": 0.0,
        }
        self._cycles = 0

    def _advance(self, dt: float) -> None:
        p_load = self.setpoints.get("load_pressure", 0.65)
        p_unload = max(self.setpoints.get("unload_pressure", 0.80), p_load + 0.02)
        self.actuals["load_pressure"] = p_load
        self.actuals["unload_pressure"] = p_unload

        demand, _ = self.current_load_target()
        consumption = self.rated_flow * demand * dt / 60.0

        # 加卸载状态机
        if self.loaded and self.tank_pressure >= p_unload:
            self.loaded = False
            self._cycles += 1
        elif not self.loaded and self.tank_pressure <= p_load:
            self.loaded = True
            self._cycles += 1
        self.actuals["load_cycles"] = float(self._cycles)

        supply = self.rated_flow * dt / 60.0 if self.loaded else 0.0
        # 储气罐压力：进气减用气，容积系数简化为常数
        self.tank_pressure = max(0.1, self.tank_pressure + (supply - consumption) * 0.06)
        self.actuals["tank_pressure"] = self.tank_pressure

        # 功率：加载时接近额定并随排气压力升高，卸载时约 30% 空载功率
        if self.loaded:
            pressure_penalty = 1.0 + 0.45 * (self.tank_pressure - 0.6)
            power_target = self.rated_power * pressure_penalty * (1 + 0.2 * self.degradation)
        else:
            power_target = self.rated_power * 0.30
        power = self._first_order("power", max(power_target, 1.0), dt, tau=4.0)
        self.actuals["load_ratio"] = power / max(self.rated_power, 1e-6)
        self.actuals["current"] = 160.0 * (power / max(self.rated_power, 1e-6))
        self.actuals["flow"] = supply / max(dt / 60.0, 1e-9) if self.loaded else 0.0

        temp_target = (self.ambient_c + (48.0 if self.loaded else 18.0)
                       + 22.0 * self.degradation
                       + (14.0 * self.fault_severity
                          if self.injected_fault == "overheat" else 0.0))
        self._first_order("outlet_temp", temp_target, dt, tau=120.0)
        self.actuals["ambient_temp"] = self.ambient_c + self.rng.normal(0, 0.2)

        vib = 1.6 + 1.8 * self.actuals["load_ratio"] + 4.5 * self.degradation
        if self.injected_fault:
            vib += 3.5 * self.fault_severity
        self.actuals["vib_de"] = max(vib + self.rng.normal(0, 0.15), 0.1)
        self.actuals["speed"] = 2950.0 if self.loaded else 2900.0


def make_daily_load_profile(scale: float = 1.0) -> List[LoadSegment]:
    """一条典型的日负载曲线（压缩后用于演示）。"""
    return [
        LoadSegment(120 * scale, 0.35, "夜班低负荷"),
        LoadSegment(90 * scale, 0.62, "早班升负荷"),
        LoadSegment(180 * scale, 0.88, "白班满负荷"),
        LoadSegment(90 * scale, 0.55, "午间降负荷"),
        LoadSegment(150 * scale, 0.80, "晚班高负荷"),
    ]


__all__ = [
    "BaseDeviceSimulator", "FanSimulator", "CompressorSimulator",
    "LoadSegment", "make_daily_load_profile",
]

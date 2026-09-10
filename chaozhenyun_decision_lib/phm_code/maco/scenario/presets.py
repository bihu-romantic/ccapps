"""
场景配置与预置方案
==================

对应课题名称中的 **"多场景适配"**。一个场景把四样东西打包在一起：

* 测点表（采什么、从哪采、量程多少）
* 可调参数表（能调什么、调多大、写到哪个点）
* 优化目标与权重（这个场景优先省电还是优先保产）
* 场景专属规则与约束（这个场景特有的工程常识）

系统换一个应用场景只需换一份场景配置，内核代码完全不动——
这是 "多场景适配" 在工程上的具体含义。

内置四套场景：变频风机、空压机、循环水泵机组、数控机床主轴。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..core.enums import DeviceType, ObjectiveSense, ParamType, SignalKind
from ..core.logging_setup import get_logger
from ..core.registry import DeviceProfile
from ..core.types import ObjectiveSpec, ParameterSpec, SignalPoint
from ..knowledge.base import Constraint, Rule

logger = get_logger("scenario.presets")


@dataclass
class ScenarioProfile:
    """一套完整的场景配置。"""

    scenario_id: str
    name: str
    device_type: DeviceType
    description: str = ""
    objectives: List[ObjectiveSpec] = field(default_factory=list)
    objective_weights: Dict[str, float] = field(default_factory=dict)
    extra_rules: List[Rule] = field(default_factory=list)
    extra_constraints: List[Constraint] = field(default_factory=list)
    #: 该场景推荐的工况签名维度
    signature_keys: Tuple[str, ...] = ("dev.load_ratio", "dev.speed_ratio")
    #: 该场景推荐的控制参数
    control_overrides: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scenario_id": self.scenario_id, "name": self.name,
            "device_type": self.device_type.code, "description": self.description,
            "objectives": [o.to_dict() for o in self.objectives],
            "objective_weights": dict(self.objective_weights),
            "signature_keys": list(self.signature_keys),
            "rules": len(self.extra_rules),
            "constraints": len(self.extra_constraints),
            "control_overrides": dict(self.control_overrides),
        }


# ==========================================================================
# 通用测点构造辅助
# ==========================================================================
def _point(point_id: str, name: str, kind: SignalKind, unit: str,
           low: float, high: float, device_id: str, address: str = "",
           writable: bool = False, fs: Optional[float] = None,
           interval_ms: int = 1000) -> SignalPoint:
    return SignalPoint(
        point_id=point_id, name=name, kind=kind, unit=unit,
        low_limit=low, high_limit=high, sample_interval_ms=interval_ms,
        address=address or point_id, writable=writable,
        sampling_rate_hz=fs, device_id=device_id,
    )


# ==========================================================================
# 场景一：变频风机
# ==========================================================================
def build_fan_profile(device_id: str = "FAN-01", area: str = "一号车间") -> DeviceProfile:
    """变频引风机设备档案。"""
    profile = DeviceProfile(
        device_id=device_id, name="一号引风机", device_type=DeviceType.FAN,
        area=area, scenario="fan_vfd",
        nameplate={
            "rated_power_kw": 55.0, "rated_speed_rpm": 1480.0,
            "rated_current_a": 105.0, "rated_flow": 1200.0,
            "temp_alarm_c": 85.0, "vib_alarm_mms": 11.2,
        },
        mechanics={
            "n_rollers": 9, "roller_diameter": 12.0,
            "pitch_diameter": 60.0, "contact_angle": 0.0,
        },
        remark="变频驱动，风量由变频器频率与入口导叶共同调节",
    )
    for p in (
        _point("speed", "主轴转速", SignalKind.SPEED, "rpm", 0, 1800, device_id),
        _point("power", "有功功率", SignalKind.POWER, "kW", 0, 70, device_id),
        _point("current", "运行电流", SignalKind.CURRENT, "A", 0, 130, device_id),
        _point("flow", "风量", SignalKind.FLOW, "m³/h", 0, 1500, device_id),
        _point("bearing_temp", "轴承温度", SignalKind.TEMPERATURE, "℃", -20, 150, device_id),
        _point("motor_temp", "电机温度", SignalKind.TEMPERATURE, "℃", -20, 160, device_id),
        _point("ambient_temp", "环境温度", SignalKind.TEMPERATURE, "℃", -30, 60, device_id),
        _point("vib_de", "驱动端振动", SignalKind.VIBRATION, "mm/s", 0, 60, device_id,
               fs=5120.0, interval_ms=1000),
        _point("vib_nde", "非驱动端振动", SignalKind.VIBRATION, "mm/s", 0, 60, device_id,
               fs=5120.0, interval_ms=1000),
        _point("freq_setpoint", "变频器频率设定", SignalKind.SETPOINT, "Hz",
               25, 50, device_id, writable=True),
        _point("vane_setpoint", "入口导叶开度设定", SignalKind.SETPOINT, "%",
               30, 100, device_id, writable=True),
    ):
        profile.add_point(p)

    profile.add_parameter(ParameterSpec(
        param_id="freq", name="变频器频率", param_type=ParamType.CONTINUOUS,
        lower=30.0, upper=50.0, step=0.1, unit="Hz", max_delta=2.0,
        write_point_id="freq_setpoint", settle_seconds=60.0,
        description="降低频率可按三次方律显著降低功率，但会同时降低风量",
    ))
    profile.add_parameter(ParameterSpec(
        param_id="vane", name="入口导叶开度", param_type=ParamType.CONTINUOUS,
        lower=40.0, upper=100.0, step=1.0, unit="%", max_delta=5.0,
        write_point_id="vane_setpoint", settle_seconds=45.0,
        description="导叶节流会带来附加损失，优先用变频调节",
    ))
    return profile


def fan_scenario() -> ScenarioProfile:
    return ScenarioProfile(
        scenario_id="fan_vfd", name="变频风机节能场景",
        device_type=DeviceType.FAN,
        description=("以单位风量能耗最低为主目标，兼顾风量满足与轴承健康。"
                     "核心优化逻辑是用变频替代导叶节流。"),
        objectives=[
            ObjectiveSpec("energy", "单位风量能耗", ObjectiveSense.MINIMIZE, 0.50,
                          "kW/(m³/h)"),
            ObjectiveSpec("throughput", "风量", ObjectiveSense.MAXIMIZE, 0.25, "m³/h"),
            ObjectiveSpec("health_cost", "振动损耗", ObjectiveSense.MINIMIZE, 0.25,
                          "mm/s"),
        ],
        objective_weights={"energy": 0.50, "throughput": 0.25, "health_cost": 0.25},
        signature_keys=("dev.load_ratio", "dev.speed_ratio", "dev.temp_mean"),
        extra_rules=[
            Rule("R-FAN-01", "导叶开度不足时优先开导叶再降频",
                 "vane < 70 and load_ratio > 0.6",
                 {"adjust.vane": "+5", "note": "节流损失偏大，先开大导叶降低阻力"},
                 0.72, 65),
            Rule("R-FAN-02", "风量富余时降频节能",
                 "flow_margin > 0.15 and stability == 'steady' and health_score > 0.75",
                 {"adjust.freq": "-3%", "note": "风量存在富余，降频可按三次方律省电"},
                 0.78, 62),
        ],
        extra_constraints=[
            Constraint("C-FAN-01", "最低风量保障", "flow > 600", "hard", 1.0,
                       "风量不得低于工艺最低要求 600 m³/h"),
            Constraint("C-FAN-02", "变频器最低频率", "freq > 30", "hard", 1.0,
                       "低于 30Hz 电机散热不足"),
        ],
        control_overrides={"max_delta_ratio": 0.08, "min_dispatch_interval_s": 240.0},
    )


# ==========================================================================
# 场景二：空压机
# ==========================================================================
def build_compressor_profile(device_id: str = "AC-01",
                             area: str = "动力站") -> DeviceProfile:
    """螺杆空压机设备档案。"""
    profile = DeviceProfile(
        device_id=device_id, name="一号螺杆空压机",
        device_type=DeviceType.COMPRESSOR, area=area, scenario="compressor_band",
        nameplate={
            "rated_power_kw": 75.0, "rated_speed_rpm": 2950.0,
            "rated_current_a": 160.0, "rated_flow": 13.0,
            "temp_alarm_c": 105.0,
        },
        mechanics={"n_rollers": 8, "roller_diameter": 10.0, "pitch_diameter": 52.0},
        remark="加卸载控制，优化空间在压力带设置",
    )
    for p in (
        _point("speed", "主机转速", SignalKind.SPEED, "rpm", 0, 3200, device_id),
        _point("power", "有功功率", SignalKind.POWER, "kW", 0, 95, device_id),
        _point("current", "运行电流", SignalKind.CURRENT, "A", 0, 200, device_id),
        _point("flow", "排气量", SignalKind.FLOW, "m³/min", 0, 16, device_id),
        _point("tank_pressure", "储气罐压力", SignalKind.PRESSURE, "MPa", 0, 1.2, device_id),
        _point("outlet_temp", "排气温度", SignalKind.TEMPERATURE, "℃", -20, 150, device_id),
        _point("ambient_temp", "环境温度", SignalKind.TEMPERATURE, "℃", -30, 60, device_id),
        _point("vib_de", "主机振动", SignalKind.VIBRATION, "mm/s", 0, 60, device_id,
               fs=5120.0),
        _point("load_cycles", "加卸载次数", SignalKind.STATE, "次", 0, 1e9, device_id),
        _point("load_pressure", "加载压力设定", SignalKind.SETPOINT, "MPa",
               0.50, 0.75, device_id, writable=True),
        _point("unload_pressure", "卸载压力设定", SignalKind.SETPOINT, "MPa",
               0.65, 0.95, device_id, writable=True),
    ):
        profile.add_point(p)

    profile.add_parameter(ParameterSpec(
        param_id="load_pressure", name="加载压力", param_type=ParamType.CONTINUOUS,
        lower=0.50, upper=0.72, step=0.01, unit="MPa", max_delta=0.03,
        write_point_id="load_pressure", settle_seconds=180.0,
        description="提高加载压力可减少加载时间，但会增加压缩功耗",
    ))
    profile.add_parameter(ParameterSpec(
        param_id="unload_pressure", name="卸载压力", param_type=ParamType.CONTINUOUS,
        lower=0.68, upper=0.92, step=0.01, unit="MPa", max_delta=0.03,
        write_point_id="unload_pressure", settle_seconds=180.0,
        description="降低卸载压力可直接降低压缩功耗，但压力带过窄会频繁加卸载",
    ))
    return profile


def compressor_scenario() -> ScenarioProfile:
    return ScenarioProfile(
        scenario_id="compressor_band", name="空压机压力带优化场景",
        device_type=DeviceType.COMPRESSOR,
        description=("在保证末端用气压力的前提下压低卸载压力，"
                     "同时控制加卸载频次，避免频繁切换造成的空载浪费。"),
        objectives=[
            ObjectiveSpec("energy", "比功率", ObjectiveSense.MINIMIZE, 0.55,
                          "kW/(m³/min)"),
            ObjectiveSpec("throughput", "供气保障率", ObjectiveSense.MAXIMIZE, 0.30, ""),
            ObjectiveSpec("health_cost", "加卸载频次", ObjectiveSense.MINIMIZE, 0.15,
                          "次/h"),
        ],
        objective_weights={"energy": 0.55, "throughput": 0.30, "health_cost": 0.15},
        signature_keys=("dev.load_ratio", "tank_pressure.mean", "dev.temp_mean"),
        extra_rules=[
            Rule("R-AC-01", "压力带过窄导致频繁加卸载",
                 "load_cycles_rate > 12",
                 {"adjust.unload_pressure": "+0.02",
                  "note": "加卸载过于频繁，适当拉宽压力带"},
                 0.75, 70),
            Rule("R-AC-02", "末端压力富余时降低卸载压力",
                 "tank_pressure_margin > 0.08 and stability == 'steady'",
                 {"adjust.unload_pressure": "-0.02",
                  "note": "储气压力长期高于需求，降低卸载压力直接省电"},
                 0.80, 68),
        ],
        extra_constraints=[
            Constraint("C-AC-01", "最低供气压力", "tank_pressure > 0.55", "hard", 1.0,
                       "储气罐压力不得低于末端最低用气压力"),
            Constraint("C-AC-02", "压力带最小宽度",
                       "pressure_band > 0.08", "hard", 1.0,
                       "加卸载压差不得小于 0.08MPa，否则频繁切换"),
            Constraint("C-AC-03", "排气温度上限", "outlet_temp < 105", "hard", 1.0,
                       "排气温度不得超过 105℃"),
        ],
        control_overrides={"max_delta_ratio": 0.05, "min_dispatch_interval_s": 600.0},
    )


# ==========================================================================
# 场景三 / 四：水泵机组与机床主轴（配置骨架，测点按现场补全）
# ==========================================================================
def pump_scenario() -> ScenarioProfile:
    return ScenarioProfile(
        scenario_id="pump_group", name="循环水泵机组优化场景",
        device_type=DeviceType.PUMP,
        description=("多台泵并联运行时，在满足总流量的前提下选择开机台数与"
                     "各台转速的组合，使机组总能耗最低。"),
        objectives=[
            ObjectiveSpec("energy", "机组单位流量能耗", ObjectiveSense.MINIMIZE, 0.55,
                          "kW/(m³/h)"),
            ObjectiveSpec("throughput", "总流量", ObjectiveSense.MAXIMIZE, 0.30, "m³/h"),
            ObjectiveSpec("health_cost", "汽蚀风险", ObjectiveSense.MINIMIZE, 0.15, ""),
        ],
        objective_weights={"energy": 0.55, "throughput": 0.30, "health_cost": 0.15},
        signature_keys=("dev.load_ratio", "dev.speed_ratio"),
        extra_constraints=[
            Constraint("C-PUMP-01", "汽蚀余量", "npsh_margin > 0.5", "hard", 1.0,
                       "有效汽蚀余量须高于必需值 0.5m 以上"),
            Constraint("C-PUMP-02", "最小连续流量", "flow_ratio > 0.35", "hard", 1.0,
                       "低于最小连续流量会造成泵体过热"),
        ],
        control_overrides={"max_delta_ratio": 0.06},
    )


def machine_tool_scenario() -> ScenarioProfile:
    return ScenarioProfile(
        scenario_id="machine_tool", name="数控机床主轴工艺参数优化场景",
        device_type=DeviceType.MACHINE_TOOL,
        description=("在保证加工表面质量与刀具寿命的前提下，优化主轴转速与进给量，"
                     "缩短单件工时。与流体机械不同，此场景的核心约束来自工艺而非能耗。"),
        objectives=[
            ObjectiveSpec("cycle_time", "单件工时", ObjectiveSense.MINIMIZE, 0.45, "s"),
            ObjectiveSpec("quality", "表面质量", ObjectiveSense.MAXIMIZE, 0.35, ""),
            ObjectiveSpec("health_cost", "刀具磨损率", ObjectiveSense.MINIMIZE, 0.20, ""),
        ],
        objective_weights={"cycle_time": 0.45, "quality": 0.35, "health_cost": 0.20},
        signature_keys=("dev.load_ratio", "dev.speed_ratio", "material_code"),
        extra_constraints=[
            Constraint("C-MT-01", "主轴负载上限", "spindle_load < 0.85", "hard", 1.0,
                       "主轴负载率不得超过 85%"),
            Constraint("C-MT-02", "颤振抑制", "chatter_index < 0.6", "hard", 1.0,
                       "颤振指标超限会直接损伤表面质量"),
        ],
        control_overrides={"max_delta_ratio": 0.10, "min_dispatch_interval_s": 60.0},
    )


#: 场景注册表
SCENARIOS: Dict[str, Any] = {
    "fan_vfd": fan_scenario,
    "compressor_band": compressor_scenario,
    "pump_group": pump_scenario,
    "machine_tool": machine_tool_scenario,
}


def get_scenario(scenario_id: str) -> ScenarioProfile:
    """按名称取场景配置。"""
    factory = SCENARIOS.get(scenario_id)
    if factory is None:
        raise KeyError(f"未知场景: {scenario_id}（可选: {sorted(SCENARIOS)}）")
    return factory()


def list_scenarios() -> List[Dict[str, Any]]:
    return [get_scenario(k).to_dict() for k in sorted(SCENARIOS)]


__all__ = [
    "ScenarioProfile", "build_fan_profile", "build_compressor_profile",
    "fan_scenario", "compressor_scenario", "pump_scenario", "machine_tool_scenario",
    "get_scenario", "list_scenarios", "SCENARIOS",
]

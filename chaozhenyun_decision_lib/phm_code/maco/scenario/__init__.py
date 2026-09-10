"""
场景层（scenario）
==================

"多场景适配" 的落点：把测点表、可调参数、优化目标、场景规则与约束
打包成一份场景配置。换场景只换配置，内核代码不动。
"""

from .presets import (
    SCENARIOS,
    ScenarioProfile,
    build_compressor_profile,
    build_fan_profile,
    compressor_scenario,
    fan_scenario,
    get_scenario,
    list_scenarios,
    machine_tool_scenario,
    pump_scenario,
)

__all__ = [
    "ScenarioProfile", "build_fan_profile", "build_compressor_profile",
    "fan_scenario", "compressor_scenario", "pump_scenario",
    "machine_tool_scenario", "get_scenario", "list_scenarios", "SCENARIOS",
]

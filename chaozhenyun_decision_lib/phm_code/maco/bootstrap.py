"""
系统装配脚手架
==============

把 "引擎 + 设备档案 + 驱动 + 控制适配器 + 场景" 一次性装配好。
现场部署时替换其中的驱动与适配器即可，装配流程本身不变。

本模块提供两条装配路径：

* :func:`build_demo_system` —— 全仿真装配，无需任何现场设备即可跑通全链路；
* :func:`build_field_system` —— 现场装配，按配置选择真实协议驱动。
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

from .acquisition.drivers.simulator import SimulatorDriver
from .control.adapters import build_adapter
from .core.config import Config
from .core.enums import ControlMode
from .core.logging_setup import get_logger
from .core.registry import DRIVER_REGISTRY, DeviceProfile
from .engine import ControlEngine
from .scenario.presets import build_compressor_profile, build_fan_profile
from .simulation.device_models import (
    BaseDeviceSimulator,
    CompressorSimulator,
    FanSimulator,
    make_daily_load_profile,
)

logger = get_logger("bootstrap")


def build_demo_system(
    config: Optional[Config] = None,
    mode: ControlMode = ControlMode.AUTONOMOUS,
    with_compressor: bool = True,
    load_profile_scale: float = 1.0,
) -> Tuple[ControlEngine, Dict[str, BaseDeviceSimulator]]:
    """装配一套全仿真系统，返回 ``(引擎, {设备号: 仿真模型})``。

    这套装配用于出厂自测与演示：所有协议驱动指向内置仿真模型，
    控制指令真实写回模型并改变其后续输出，因此闭环与反馈学习都是真实生效的。
    """
    cfg = config or Config.load(overrides={
        "control": {"mode": mode.code,
                    "min_dispatch_interval_s": 0.0,
                    "max_dispatch_per_hour": 1000},
        "feedback": {"before_seconds": 60.0, "after_seconds": 60.0,
                     "settle_seconds": 5.0},
        "condition": {"steady_min_dwell_s": 5.0, "debounce_count": 1},
        "optimization": {"surrogate_min_samples": 8, "min_confidence": 0.30},
        "health": {"baseline_min_samples": 20},
        "storage": {"audit_file": "data/store/demo-audit.jsonl"},
    })
    engine = ControlEngine(cfg)
    simulators: Dict[str, BaseDeviceSimulator] = {}

    # ---- 设备一：变频风机 ----
    fan_profile = build_fan_profile("FAN-01")
    fan_sim = FanSimulator("FAN-01")
    fan_sim.set_load_profile(make_daily_load_profile(load_profile_scale))
    fan_driver = SimulatorDriver("sim-fan", {"source": fan_sim, "auto_step": True,
                                             "step_seconds": 5.0})
    fan_adapter = build_adapter("plc", "plc-fan", fan_profile, driver=fan_driver,
                                options={"readback_tolerance": 0.05,
                                         "readback_delay_s": 0.0,
                                         "readback_timeout_s": 0.5})
    engine.register_device(fan_profile, fan_driver, fan_adapter, "fan_vfd")
    simulators["FAN-01"] = fan_sim

    # ---- 设备二：空压机 ----
    if with_compressor:
        ac_profile = build_compressor_profile("AC-01")
        ac_sim = CompressorSimulator("AC-01")
        ac_sim.set_load_profile(make_daily_load_profile(load_profile_scale))
        ac_driver = SimulatorDriver("sim-ac", {"source": ac_sim, "auto_step": True,
                                               "step_seconds": 5.0})
        ac_adapter = build_adapter("dcs", "dcs-ac", ac_profile, driver=ac_driver,
                                   options={"readback_tolerance": 0.05,
                                            "readback_delay_s": 0.0,
                                            "readback_timeout_s": 0.5})
        engine.register_device(ac_profile, ac_driver, ac_adapter, "compressor_band")
        simulators["AC-01"] = ac_sim

    logger.info("演示系统装配完成：%d 台仿真设备", len(simulators))
    return engine, simulators


def build_field_system(config_path: str = "config/system.json") -> ControlEngine:
    """按配置文件装配现场系统。

    配置文件中 ``devices`` 段的每一项形如::

        {
          "device_id": "FAN-01",
          "profile": "fan",                     内置档案模板或自定义档案文件
          "scenario": "fan_vfd",
          "driver": {"type": "modbus_tcp",
                     "options": {"host": "10.1.2.30", "port": 502, "unit_id": 1}},
          "control": {"type": "plc",
                      "options": {"mode_point_id": "remote_mode"}}
        }
    """
    cfg = Config.load(config_path)
    engine = ControlEngine(cfg)
    devices = cfg.get("devices", [])
    if not isinstance(devices, list):
        raise ValueError("配置项 devices 必须是数组")

    builders = {"fan": build_fan_profile, "compressor": build_compressor_profile}
    for item in devices:
        device_id = str(item.get("device_id", "")).strip()
        if not device_id:
            logger.warning("跳过缺少 device_id 的设备配置项")
            continue
        template = str(item.get("profile", "fan"))
        builder = builders.get(template)
        if builder is None:
            logger.warning("设备 %s 使用了未知档案模板 %s，已跳过", device_id, template)
            continue
        profile: DeviceProfile = builder(device_id)

        driver_cfg: Mapping[str, Any] = item.get("driver") or {}
        driver_type = str(driver_cfg.get("type", "modbus_tcp"))
        driver = DRIVER_REGISTRY.create(driver_type, f"drv-{device_id}",
                                        driver_cfg.get("options") or {})

        control_cfg: Mapping[str, Any] = item.get("control") or {}
        adapter = None
        if control_cfg:
            adapter = build_adapter(
                str(control_cfg.get("type", "plc")), f"ctl-{device_id}", profile,
                driver=driver, options=control_cfg.get("options") or {},
            )
        engine.register_device(profile, driver, adapter,
                               str(item.get("scenario", profile.scenario)))
        logger.info("现场设备 %s 已装配：驱动=%s", device_id, driver_type)
    return engine


__all__ = ["build_demo_system", "build_field_system"]

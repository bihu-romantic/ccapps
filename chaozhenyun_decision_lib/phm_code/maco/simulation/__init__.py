"""
仿真层（simulation）
====================

提供可运行的虚拟设备，用于离线联调、算法验证与演示培训。
仿真驱动与真实协议驱动实现同一接口，因此切换到现场只需改配置。
"""

from .device_models import (
    BaseDeviceSimulator,
    CompressorSimulator,
    FanSimulator,
    LoadSegment,
    make_daily_load_profile,
)

__all__ = [
    "BaseDeviceSimulator", "FanSimulator", "CompressorSimulator",
    "LoadSegment", "make_daily_load_profile",
]

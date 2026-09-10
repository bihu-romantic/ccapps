"""
多工况自适应工业设备控制优化系统（MACO）
========================================

Multi-condition Adaptive Control Optimization system for industrial equipment.

面向工业设备的知识驱动自主控制决策软件。系统实时接入设备运行数据，
自动识别当前工况，在该工况下评估设备健康状态，结合机理知识、规则推理与
历史案例生成运行参数优化建议，经安全校验后与 PLC / SCADA / DCS 闭环联动，
并对执行效果做统计评估、把经验回写知识库，形成持续自我改进的闭环。

八个功能层
----------
================  ==================================================
层                职责
================  ==================================================
``core``          内核：数据结构、配置、事件总线、调度、审计
``acquisition``   数据接入：多协议驱动、缓冲、清洗
``features``      特征提取：时域 / 频域 / 热电能效
``condition``     工况识别：工况库、归属判定、稳态判定
``health``        健康评估：基线、异常、故障模式、退化趋势
``knowledge``     知识：规则推理、约束、案例复用
``optimization``  参数寻优：代理模型、多种算法、建议生成
``control``       控制联动：安全门、适配器、闭环状态机
``feedback``      反馈学习：效果评估、奖励、在线学习、知识回写
================  ==================================================

快速开始
--------
::

    from maco import ControlEngine, build_demo_system

    engine = build_demo_system()
    result = engine.decision_cycle("FAN-01")
    print(result.summary())
"""

from .core.config import Config
from .core.enums import ControlMode, HealthGrade, SignalKind
from .core.registry import DeviceProfile
from .engine import ControlEngine, CycleResult

__version__ = "1.0.0"
__software_name__ = "多工况自适应工业设备控制优化系统"
__all__ = [
    "ControlEngine", "CycleResult", "Config", "DeviceProfile",
    "ControlMode", "HealthGrade", "SignalKind",
    "__version__", "__software_name__",
]

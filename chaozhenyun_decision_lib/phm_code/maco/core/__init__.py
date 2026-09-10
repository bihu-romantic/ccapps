"""
内核层（core）
==============

本层提供全系统共用的基础设施，不含任何业务逻辑：

* :mod:`maco.core.enums` —— 全局枚举，离散语义的唯一来源
* :mod:`maco.core.types` —— 全链路数据结构
* :mod:`maco.core.errors` —— 分层异常体系
* :mod:`maco.core.config` —— 分层配置与校验
* :mod:`maco.core.event_bus` —— 发布订阅总线
* :mod:`maco.core.scheduler` —— 周期与延时任务调度
* :mod:`maco.core.registry` —— 组件与设备注册
* :mod:`maco.core.audit` —— 链式审计账本
* :mod:`maco.core.logging_setup` —— 日志与链路上下文
* :mod:`maco.core.utils` —— 无状态数学/时序工具
"""

from .audit import AuditTrail
from .config import Config
from .enums import (
    CommandState,
    ConditionStability,
    ControlMode,
    DeviceType,
    EventTopic,
    EvidenceKind,
    FaultPattern,
    HealthGrade,
    ObjectiveSense,
    ParamType,
    Quality,
    Severity,
    SignalKind,
    TrendDirection,
)
from .errors import MacoError, SafetyViolation
from .event_bus import Event, EventBus, get_default_bus
from .logging_setup import TraceScope, get_logger, setup_logging
from .registry import ComponentRegistry, DeviceProfile, DeviceRegistry
from .scheduler import Scheduler
from .types import (
    ConditionSnapshot,
    ControlCommand,
    DecisionRecord,
    EffectEvaluation,
    Evidence,
    ExecutionRecord,
    ExperienceCase,
    FeatureVector,
    HealthIndicator,
    HealthReport,
    MetricComparison,
    ObjectiveSpec,
    OptimizationAdvice,
    ParameterChange,
    ParameterSpec,
    Sample,
    SampleFrame,
    SignalPoint,
    WriteItem,
    new_id,
    now_ts,
)

__all__ = [
    "AuditTrail", "Config", "Event", "EventBus", "get_default_bus",
    "Scheduler", "ComponentRegistry", "DeviceProfile", "DeviceRegistry",
    "TraceScope", "get_logger", "setup_logging", "MacoError", "SafetyViolation",
    "SignalKind", "Quality", "DeviceType", "HealthGrade", "ConditionStability",
    "ControlMode", "CommandState", "Severity", "TrendDirection", "FaultPattern",
    "EventTopic", "ParamType", "ObjectiveSense", "EvidenceKind",
    "SignalPoint", "Sample", "SampleFrame", "FeatureVector", "ConditionSnapshot",
    "HealthIndicator", "HealthReport", "ParameterSpec", "ObjectiveSpec",
    "Evidence", "ParameterChange", "OptimizationAdvice", "WriteItem",
    "ControlCommand", "ExecutionRecord", "MetricComparison", "EffectEvaluation",
    "ExperienceCase", "DecisionRecord", "now_ts", "new_id",
]

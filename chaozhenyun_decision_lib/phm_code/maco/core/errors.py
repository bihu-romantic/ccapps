"""
异常体系模块
============

系统采用分层异常体系，所有自定义异常继承自 :class:`MacoError`。
分层的目的在于：上层引擎可以按异常类别决定 "重试 / 降级 / 熔断 / 上报"
四种不同的处置策略，而不需要解析异常文本。

处置策略约定：
* :class:`TransientError` 及其子类 —— 允许按退避策略重试；
* :class:`ConfigurationError` —— 不重试，直接上报并阻止组件启动；
* :class:`SafetyViolation` —— 立即终止本次控制链路，写审计并告警；
* :class:`KnowledgeError` —— 降级为纯数据驱动路径继续运行。
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class MacoError(Exception):
    """系统异常基类。

    :param message: 人类可读的错误描述
    :param context: 附加上下文，会被审计日志完整记录
    """

    #: 错误码前缀，子类覆盖
    code: str = "MACO-0000"

    def __init__(self, message: str, context: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.context: Dict[str, Any] = dict(context or {})

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典，供审计与接口返回。"""
        return {
            "code": self.code,
            "type": type(self).__name__,
            "message": self.message,
            "context": self.context,
        }

    def __str__(self) -> str:
        if self.context:
            items = ", ".join(f"{k}={v!r}" for k, v in sorted(self.context.items()))
            return f"[{self.code}] {self.message} ({items})"
        return f"[{self.code}] {self.message}"


# --------------------------------------------------------------------------
# 配置与装配类异常
# --------------------------------------------------------------------------
class ConfigurationError(MacoError):
    """配置缺失、类型错误或互相冲突。不可重试。"""

    code = "MACO-1001"


class ScenarioError(ConfigurationError):
    """场景配置无法装配为可运行实例。"""

    code = "MACO-1002"


class RegistryError(MacoError):
    """组件注册冲突或未注册组件被请求。"""

    code = "MACO-1003"


# --------------------------------------------------------------------------
# 数据接入类异常
# --------------------------------------------------------------------------
class TransientError(MacoError):
    """可重试的瞬时错误。"""

    code = "MACO-2000"


class ConnectionFailure(TransientError):
    """与现场设备/控制系统建立连接失败。"""

    code = "MACO-2001"


class ProtocolError(TransientError):
    """协议帧解析失败或从站返回异常码。"""

    code = "MACO-2002"


class TimeoutExpired(TransientError):
    """读写操作超时。"""

    code = "MACO-2003"


class PointNotFound(MacoError):
    """请求的测点在测点表中不存在。"""

    code = "MACO-2004"


class DataQualityError(MacoError):
    """数据质量不满足下游计算的最低要求。"""

    code = "MACO-2005"


# --------------------------------------------------------------------------
# 分析与知识类异常
# --------------------------------------------------------------------------
class FeatureError(MacoError):
    """特征提取失败，通常因样本长度不足或采样率缺失。"""

    code = "MACO-3001"


class ConditionError(MacoError):
    """工况识别失败。"""

    code = "MACO-3002"


class KnowledgeError(MacoError):
    """知识库加载或推理过程异常，可降级为纯数据驱动。"""

    code = "MACO-3003"


class RuleSyntaxError(KnowledgeError):
    """规则表达式语法错误。"""

    code = "MACO-3004"


class OptimizationError(MacoError):
    """寻优过程无法产生可行解。"""

    code = "MACO-3005"


class InfeasibleProblem(OptimizationError):
    """约束互相冲突导致可行域为空。"""

    code = "MACO-3006"


# --------------------------------------------------------------------------
# 控制与安全类异常
# --------------------------------------------------------------------------
class SafetyViolation(MacoError):
    """安全门拦截。任何情况下都不得被 ``except Exception`` 静默吞掉。"""

    code = "MACO-4001"

    def __init__(
        self,
        message: str,
        rule_id: str = "",
        context: Optional[Dict[str, Any]] = None,
    ):
        ctx = dict(context or {})
        if rule_id:
            ctx.setdefault("rule_id", rule_id)
        super().__init__(message, ctx)
        self.rule_id = rule_id


class InterlockActive(SafetyViolation):
    """互锁条件成立，禁止写入。"""

    code = "MACO-4002"


class WritePermissionDenied(SafetyViolation):
    """当前控制模式不允许写操作。"""

    code = "MACO-4003"


class DispatchFailure(MacoError):
    """指令下发失败（通信成功但目标系统拒绝或回读不一致）。"""

    code = "MACO-4004"


class RollbackFailure(MacoError):
    """回滚失败，属最高优先级告警。"""

    code = "MACO-4005"


# --------------------------------------------------------------------------
# 反馈学习类异常
# --------------------------------------------------------------------------
class ObservationError(MacoError):
    """效果观测窗数据不足，无法评估。"""

    code = "MACO-5001"


class LearningError(MacoError):
    """在线学习模型更新失败。"""

    code = "MACO-5002"


class StorageError(MacoError):
    """持久化读写失败。"""

    code = "MACO-6001"


__all__ = [
    "MacoError",
    "ConfigurationError",
    "ScenarioError",
    "RegistryError",
    "TransientError",
    "ConnectionFailure",
    "ProtocolError",
    "TimeoutExpired",
    "PointNotFound",
    "DataQualityError",
    "FeatureError",
    "ConditionError",
    "KnowledgeError",
    "RuleSyntaxError",
    "OptimizationError",
    "InfeasibleProblem",
    "SafetyViolation",
    "InterlockActive",
    "WritePermissionDenied",
    "DispatchFailure",
    "RollbackFailure",
    "ObservationError",
    "LearningError",
    "StorageError",
]

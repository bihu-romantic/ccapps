"""
枚举定义模块
============

本模块集中定义系统全局使用的枚举类型，覆盖信号语义、数据质量、设备类别、
健康等级、工况稳定性、控制模式、指令状态、事件主题等。所有跨层传递的
"离散语义" 均以此处枚举为唯一来源，避免各层使用魔法字符串导致的语义漂移。

设计约束：
1. 枚举值统一采用小写英文短标识，便于序列化与外部系统对接；
2. 每个枚举提供 ``label`` 中文名，供人机界面与报表直接使用；
3. 提供 ``from_text`` 容错解析，兼容外部系统大小写不一致的写法。
"""

from __future__ import annotations

from enum import Enum
from typing import Optional


class _LabeledEnum(Enum):
    """带中文标签的枚举基类。"""

    def __init__(self, code: str, label: str):
        self._code = code
        self._label = label

    @property
    def code(self) -> str:
        """机器可读标识。"""
        return self._code

    @property
    def label(self) -> str:
        """人机界面显示用的中文名称。"""
        return self._label

    def __str__(self) -> str:  # pragma: no cover - 简单委托
        return self._code

    @classmethod
    def from_text(cls, text: Optional[str], default=None):
        """容错解析：支持按 code、name、label 三种写法解析。"""
        if text is None:
            return default
        probe = str(text).strip().lower()
        for member in cls:
            if probe in (member.code.lower(), member.name.lower(), member.label):
                return member
        return default


class SignalKind(_LabeledEnum):
    """测点信号语义类别。

    该枚举决定了信号进入哪条特征提取通路：振动类走时频域分析，
    温度/电流/能耗类走稳态统计与热电特征通路。
    """

    TEMPERATURE = ("temperature", "温度")
    VIBRATION = ("vibration", "振动")
    CURRENT = ("current", "电流")
    VOLTAGE = ("voltage", "电压")
    SPEED = ("speed", "转速")
    ENERGY = ("energy", "能耗")
    POWER = ("power", "功率")
    PRESSURE = ("pressure", "压力")
    FLOW = ("flow", "流量")
    TORQUE = ("torque", "扭矩")
    POSITION = ("position", "位移")
    HUMIDITY = ("humidity", "湿度")
    STATE = ("state", "开关状态")
    SETPOINT = ("setpoint", "设定值")
    GENERIC = ("generic", "通用量")

    @property
    def is_dynamic(self) -> bool:
        """是否为高频动态信号（需要做时频域分析）。"""
        return self in (SignalKind.VIBRATION, SignalKind.TORQUE, SignalKind.POSITION)

    @property
    def is_electric(self) -> bool:
        """是否为电气量。"""
        return self in (
            SignalKind.CURRENT,
            SignalKind.VOLTAGE,
            SignalKind.POWER,
            SignalKind.ENERGY,
        )


class Quality(_LabeledEnum):
    """数据质量标记，语义参照 OPC UA StatusCode 简化模型。"""

    GOOD = ("good", "良好")
    UNCERTAIN = ("uncertain", "存疑")
    BAD = ("bad", "坏值")
    STALE = ("stale", "陈旧")
    OUT_OF_RANGE = ("out_of_range", "超量程")
    SUBSTITUTED = ("substituted", "替代值")
    COMM_FAILURE = ("comm_failure", "通信中断")

    @property
    def usable(self) -> bool:
        """该质量标记的数据是否可参与后续计算。"""
        return self in (Quality.GOOD, Quality.SUBSTITUTED, Quality.UNCERTAIN)


class DeviceType(_LabeledEnum):
    """设备大类。不同大类绑定不同的默认知识包与目标函数模板。"""

    FAN = ("fan", "风机")
    COMPRESSOR = ("compressor", "空压机")
    PUMP = ("pump", "水泵机组")
    MACHINE_TOOL = ("machine_tool", "数控机床")
    INJECTION_MOLDING = ("injection_molding", "注塑机")
    CONVEYOR = ("conveyor", "输送线")
    GEARBOX = ("gearbox", "齿轮箱")
    GENERIC = ("generic", "通用设备")


class HealthGrade(_LabeledEnum):
    """设备健康等级，对应 ISO 20816 振动烈度分区思想的四级划分。"""

    HEALTHY = ("healthy", "健康")
    ATTENTION = ("attention", "关注")
    DEGRADED = ("degraded", "劣化")
    CRITICAL = ("critical", "危险")

    @classmethod
    def from_score(cls, score: float) -> "HealthGrade":
        """由 0~1 健康度分值映射到等级（分值越高越健康）。"""
        if score >= 0.85:
            return cls.HEALTHY
        if score >= 0.65:
            return cls.ATTENTION
        if score >= 0.40:
            return cls.DEGRADED
        return cls.CRITICAL

    @property
    def rank(self) -> int:
        """等级序号，用于比较严重程度（越大越严重）。"""
        return {
            "healthy": 0,
            "attention": 1,
            "degraded": 2,
            "critical": 3,
        }[self.code]


class ConditionStability(_LabeledEnum):
    """工况稳定性判定结果。

    仅在 STEADY（稳态）下才允许触发参数寻优与控制下发，
    这是 "多工况自适应" 避免在过渡过程中误优化的关键闸门。
    """

    STEADY = ("steady", "稳态")
    TRANSIENT = ("transient", "过渡")
    STARTUP = ("startup", "启机")
    SHUTDOWN = ("shutdown", "停机")
    IDLE = ("idle", "空载")
    UNKNOWN = ("unknown", "未知")


class ControlMode(_LabeledEnum):
    """控制联动模式，决定优化建议能走到闭环的哪一步。"""

    OBSERVE = ("observe", "只读观察")
    ADVISORY = ("advisory", "建议模式")
    SUPERVISED = ("supervised", "半自动")
    AUTONOMOUS = ("autonomous", "自主闭环")

    @property
    def allows_write(self) -> bool:
        """该模式是否允许向控制系统写值。"""
        return self in (ControlMode.SUPERVISED, ControlMode.AUTONOMOUS)

    @property
    def requires_approval(self) -> bool:
        """该模式下写值是否需要人工确认。"""
        return self is ControlMode.SUPERVISED


class CommandState(_LabeledEnum):
    """控制指令生命周期状态。"""

    DRAFT = ("draft", "草拟")
    VALIDATED = ("validated", "已校验")
    REJECTED = ("rejected", "已驳回")
    PENDING_APPROVAL = ("pending_approval", "待审批")
    DISPATCHING = ("dispatching", "下发中")
    DISPATCHED = ("dispatched", "已下发")
    VERIFIED = ("verified", "回读确认")
    FAILED = ("failed", "下发失败")
    ROLLED_BACK = ("rolled_back", "已回滚")
    EVALUATED = ("evaluated", "已评估")

    @property
    def terminal(self) -> bool:
        """是否为终态。"""
        return self in (
            CommandState.REJECTED,
            CommandState.ROLLED_BACK,
            CommandState.EVALUATED,
            CommandState.FAILED,
        )


class Severity(_LabeledEnum):
    """告警/事件严重度。"""

    INFO = ("info", "提示")
    WARNING = ("warning", "预警")
    ALARM = ("alarm", "报警")
    FAULT = ("fault", "故障")

    @property
    def rank(self) -> int:
        return {"info": 0, "warning": 1, "alarm": 2, "fault": 3}[self.code]


class TrendDirection(_LabeledEnum):
    """退化趋势方向。"""

    IMPROVING = ("improving", "好转")
    STABLE = ("stable", "平稳")
    WORSENING = ("worsening", "恶化")
    VOLATILE = ("volatile", "波动")


class FaultPattern(_LabeledEnum):
    """典型故障模式。用于知识推理定位与建议生成，不作为最终诊断结论输出。"""

    NORMAL = ("normal", "正常")
    UNBALANCE = ("unbalance", "不平衡")
    MISALIGNMENT = ("misalignment", "不对中")
    LOOSENESS = ("looseness", "松动")
    BEARING_OUTER = ("bearing_outer", "轴承外圈异常")
    BEARING_INNER = ("bearing_inner", "轴承内圈异常")
    BEARING_ROLLER = ("bearing_roller", "滚动体异常")
    GEAR_WEAR = ("gear_wear", "齿轮磨损")
    CAVITATION = ("cavitation", "汽蚀")
    OVERHEAT = ("overheat", "过热")
    OVERLOAD = ("overload", "过载")
    FOULING = ("fouling", "结垢积灰")
    UNKNOWN = ("unknown", "未识别")


class EventTopic(_LabeledEnum):
    """事件总线主题。各层通过主题解耦，禁止跨层直接方法调用。"""

    FRAME_READY = ("frame.ready", "采集帧就绪")
    FRAME_CLEANSED = ("frame.cleansed", "数据清洗完成")
    FEATURE_READY = ("feature.ready", "特征就绪")
    CONDITION_CHANGED = ("condition.changed", "工况切换")
    CONDITION_DISCOVERED = ("condition.discovered", "发现新工况")
    HEALTH_UPDATED = ("health.updated", "健康评估更新")
    ANOMALY_DETECTED = ("anomaly.detected", "检测到异常")
    ADVICE_GENERATED = ("advice.generated", "生成优化建议")
    COMMAND_VALIDATED = ("command.validated", "指令通过安全校验")
    COMMAND_REJECTED = ("command.rejected", "指令被安全门驳回")
    COMMAND_DISPATCHED = ("command.dispatched", "指令已下发")
    COMMAND_VERIFIED = ("command.verified", "指令回读确认")
    COMMAND_ROLLED_BACK = ("command.rolled_back", "指令已回滚")
    EFFECT_EVALUATED = ("effect.evaluated", "执行效果评估完成")
    KNOWLEDGE_UPDATED = ("knowledge.updated", "知识库更新")
    ALARM_RAISED = ("alarm.raised", "告警产生")
    SYSTEM_ERROR = ("system.error", "系统异常")


class ParamType(_LabeledEnum):
    """可调运行参数的取值类型。"""

    CONTINUOUS = ("continuous", "连续量")
    INTEGER = ("integer", "整数量")
    CATEGORICAL = ("categorical", "枚举量")


class ObjectiveSense(_LabeledEnum):
    """目标函数优化方向。"""

    MINIMIZE = ("minimize", "越小越优")
    MAXIMIZE = ("maximize", "越大越优")


class EvidenceKind(_LabeledEnum):
    """建议依据的证据类型，用于生成可解释说明。"""

    RULE = ("rule", "规则推理")
    CASE = ("case", "历史案例")
    MODEL = ("model", "代理模型")
    CONSTRAINT = ("constraint", "约束边界")
    STATISTIC = ("statistic", "统计特征")
    PHYSICS = ("physics", "机理计算")


__all__ = [
    "SignalKind",
    "Quality",
    "DeviceType",
    "HealthGrade",
    "ConditionStability",
    "ControlMode",
    "CommandState",
    "Severity",
    "TrendDirection",
    "FaultPattern",
    "EventTopic",
    "ParamType",
    "ObjectiveSense",
    "EvidenceKind",
]

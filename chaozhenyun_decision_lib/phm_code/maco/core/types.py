"""
核心数据结构模块
================

本模块定义在 "感知 → 辨识 → 评估 → 决策 → 执行 → 学习" 全链路中流转的
不可变数据对象。全链路的每一次流转都携带 ``trace_id``，从而保证任何一条
控制指令都能反查到它所依据的原始采集帧、工况判定、健康评估与知识证据，
满足工业控制场景的可追溯要求。

约定：
1. 所有数据类均为 ``@dataclass``，跨层传递时按值传递、不做原地修改；
2. 时间戳统一使用 UTC 纪元秒（float），显示层再做本地化；
3. 每个数据类提供 ``to_dict``，用于持久化与接口序列化。
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .enums import (
    CommandState,
    ConditionStability,
    ControlMode,
    DeviceType,
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


def now_ts() -> float:
    """返回当前 UTC 纪元秒。集中封装便于测试注入。"""
    return time.time()


def new_id(prefix: str = "") -> str:
    """生成短唯一标识。"""
    token = uuid.uuid4().hex[:12]
    return f"{prefix}-{token}" if prefix else token


def _clean_float(value: Any) -> Any:
    """把 NaN / Inf 归一为 None，避免 JSON 序列化产生非法字面量。"""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


# ==========================================================================
# 一、数据接入层数据结构
# ==========================================================================
@dataclass(frozen=True)
class SignalPoint:
    """测点定义。

    描述一个物理测点的语义、工程量纲、量程与通信寻址方式。
    测点表是数据接入层与控制层共同的 "地址簿"。
    """

    point_id: str
    name: str
    kind: SignalKind = SignalKind.GENERIC
    unit: str = ""
    #: 工程量程下限，用于超量程判定
    low_limit: float = -math.inf
    #: 工程量程上限
    high_limit: float = math.inf
    #: 采样周期（毫秒）
    sample_interval_ms: int = 1000
    #: 通信地址，语义由具体驱动解释（如 Modbus 寄存器号、OPC UA NodeId）
    address: str = ""
    #: 线性标定：工程值 = 原始值 * scale + offset
    scale: float = 1.0
    offset: float = 0.0
    #: 是否可写（控制层只允许写 writable=True 的点）
    writable: bool = False
    #: 高频动态信号的采样率（Hz），振动类必填
    sampling_rate_hz: Optional[float] = None
    #: 所属设备
    device_id: str = ""
    #: 自由扩展字段
    meta: Mapping[str, Any] = field(default_factory=dict)

    def scale_raw(self, raw: float) -> float:
        """把驱动读回的原始值折算为工程值。"""
        return raw * self.scale + self.offset

    def unscale(self, engineering: float) -> float:
        """把工程值反算为下发给现场的原始值。"""
        if self.scale == 0:
            raise ZeroDivisionError(f"测点 {self.point_id} 的 scale 不能为 0")
        return (engineering - self.offset) / self.scale

    def in_range(self, value: float) -> bool:
        """量程校验。"""
        return self.low_limit <= value <= self.high_limit

    def to_dict(self) -> Dict[str, Any]:
        return {
            "point_id": self.point_id,
            "name": self.name,
            "kind": self.kind.code,
            "unit": self.unit,
            "low_limit": _clean_float(self.low_limit),
            "high_limit": _clean_float(self.high_limit),
            "sample_interval_ms": self.sample_interval_ms,
            "address": self.address,
            "writable": self.writable,
            "sampling_rate_hz": self.sampling_rate_hz,
            "device_id": self.device_id,
        }


@dataclass(frozen=True)
class Sample:
    """单点单次采样值。"""

    point_id: str
    value: float
    timestamp: float = field(default_factory=now_ts)
    quality: Quality = Quality.GOOD
    #: 高频信号的整段波形（振动类），标量信号为 None
    waveform: Optional[Sequence[float]] = None

    @property
    def usable(self) -> bool:
        return self.quality.usable and isinstance(self.value, (int, float))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "point_id": self.point_id,
            "value": _clean_float(self.value),
            "timestamp": self.timestamp,
            "quality": self.quality.code,
            "waveform_len": len(self.waveform) if self.waveform is not None else 0,
        }


@dataclass
class SampleFrame:
    """一次采集帧：同一时刻、同一设备的一组测点值。

    帧是数据接入层向上游交付的最小单元，也是全链路 ``trace_id`` 的起点。
    """

    device_id: str
    samples: Dict[str, Sample] = field(default_factory=dict)
    timestamp: float = field(default_factory=now_ts)
    trace_id: str = field(default_factory=lambda: new_id("trace"))
    #: 采集耗时（毫秒），用于监控采集链路健康
    acquire_cost_ms: float = 0.0

    def get(self, point_id: str) -> Optional[Sample]:
        return self.samples.get(point_id)

    def value_of(self, point_id: str, default: Optional[float] = None) -> Optional[float]:
        """取工程值；测点缺失或数据不可用时返回默认值。"""
        s = self.samples.get(point_id)
        if s is None or not s.usable:
            return default
        return s.value

    def put(self, sample: Sample) -> None:
        self.samples[sample.point_id] = sample

    @property
    def good_ratio(self) -> float:
        """良好率，作为该帧整体可信度的粗粒度指标。"""
        if not self.samples:
            return 0.0
        good = sum(1 for s in self.samples.values() if s.quality is Quality.GOOD)
        return good / len(self.samples)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "device_id": self.device_id,
            "timestamp": self.timestamp,
            "trace_id": self.trace_id,
            "good_ratio": round(self.good_ratio, 4),
            "acquire_cost_ms": round(self.acquire_cost_ms, 3),
            "samples": {k: v.to_dict() for k, v in self.samples.items()},
        }


# ==========================================================================
# 二、特征与工况层数据结构
# ==========================================================================
@dataclass
class FeatureVector:
    """特征向量。键为特征名（形如 ``vib_de.rms``），值为标量。"""

    device_id: str
    values: Dict[str, float] = field(default_factory=dict)
    timestamp: float = field(default_factory=now_ts)
    trace_id: str = ""
    #: 特征提取所依据的窗口长度（秒）
    window_seconds: float = 0.0

    def get(self, name: str, default: float = 0.0) -> float:
        v = self.values.get(name, default)
        return default if v is None or not math.isfinite(v) else float(v)

    def subset(self, names: Sequence[str]) -> List[float]:
        """按给定顺序取出子向量，缺失补 0，保证维度稳定。"""
        return [self.get(n) for n in names]

    def merge(self, other: "FeatureVector") -> "FeatureVector":
        merged = dict(self.values)
        merged.update(other.values)
        return FeatureVector(
            device_id=self.device_id,
            values=merged,
            timestamp=max(self.timestamp, other.timestamp),
            trace_id=self.trace_id or other.trace_id,
            window_seconds=max(self.window_seconds, other.window_seconds),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "device_id": self.device_id,
            "timestamp": self.timestamp,
            "trace_id": self.trace_id,
            "window_seconds": self.window_seconds,
            "values": {k: _clean_float(v) for k, v in self.values.items()},
        }


@dataclass
class ConditionSnapshot:
    """工况快照：当前设备处于哪个工况、置信度多少、是否稳态。"""

    device_id: str
    condition_id: str
    label: str = ""
    confidence: float = 0.0
    stability: ConditionStability = ConditionStability.UNKNOWN
    #: 工况判定所依据的特征名与值
    signature: Dict[str, float] = field(default_factory=dict)
    #: 距上一次工况切换已持续的秒数
    dwell_seconds: float = 0.0
    #: 是否为本次运行中新发现的工况
    is_new: bool = False
    timestamp: float = field(default_factory=now_ts)
    trace_id: str = ""

    @property
    def optimizable(self) -> bool:
        """只有稳态、且置信度足够的工况才允许触发参数寻优。"""
        return self.stability is ConditionStability.STEADY and self.confidence >= 0.6

    def to_dict(self) -> Dict[str, Any]:
        return {
            "device_id": self.device_id,
            "condition_id": self.condition_id,
            "label": self.label,
            "confidence": round(self.confidence, 4),
            "stability": self.stability.code,
            "dwell_seconds": round(self.dwell_seconds, 2),
            "is_new": self.is_new,
            "signature": {k: _clean_float(v) for k, v in self.signature.items()},
            "timestamp": self.timestamp,
            "trace_id": self.trace_id,
        }


# ==========================================================================
# 三、健康评估层数据结构
# ==========================================================================
@dataclass
class HealthIndicator:
    """单项健康指标。"""

    name: str
    label: str
    value: float
    #: 归一化到 0~1 的健康分（1 表示完全健康）
    score: float
    unit: str = ""
    #: 当前工况下的正常基线区间
    baseline_low: Optional[float] = None
    baseline_high: Optional[float] = None
    #: 相对基线的偏离倍数
    deviation: float = 0.0
    weight: float = 1.0
    severity: Severity = Severity.INFO
    remark: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "value": _clean_float(self.value),
            "score": round(self.score, 4),
            "unit": self.unit,
            "baseline": [_clean_float(self.baseline_low), _clean_float(self.baseline_high)],
            "deviation": round(self.deviation, 4),
            "weight": self.weight,
            "severity": self.severity.code,
            "remark": self.remark,
        }


@dataclass
class HealthReport:
    """设备健康评估报告（工况归一化后的结果）。"""

    device_id: str
    condition_id: str
    score: float = 1.0
    grade: HealthGrade = HealthGrade.HEALTHY
    indicators: List[HealthIndicator] = field(default_factory=list)
    #: 疑似故障模式及其置信度
    patterns: List[Tuple[FaultPattern, float]] = field(default_factory=list)
    trend: TrendDirection = TrendDirection.STABLE
    #: 退化速率（健康分/天，负值表示恶化）
    degradation_rate: float = 0.0
    #: 估计的剩余可用时间（小时），无法估计时为 None
    remaining_hours: Optional[float] = None
    anomalies: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=now_ts)
    trace_id: str = ""

    def worst_indicators(self, top_n: int = 3) -> List[HealthIndicator]:
        return sorted(self.indicators, key=lambda i: i.score)[:top_n]

    def indicator(self, name: str) -> Optional[HealthIndicator]:
        for ind in self.indicators:
            if ind.name == name:
                return ind
        return None

    @property
    def top_pattern(self) -> FaultPattern:
        if not self.patterns:
            return FaultPattern.NORMAL
        return max(self.patterns, key=lambda p: p[1])[0]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "device_id": self.device_id,
            "condition_id": self.condition_id,
            "score": round(self.score, 4),
            "grade": self.grade.code,
            "grade_label": self.grade.label,
            "trend": self.trend.code,
            "degradation_rate": round(self.degradation_rate, 6),
            "remaining_hours": self.remaining_hours,
            "anomalies": list(self.anomalies),
            "patterns": [{"pattern": p.code, "label": p.label, "confidence": round(c, 4)}
                         for p, c in self.patterns],
            "indicators": [i.to_dict() for i in self.indicators],
            "timestamp": self.timestamp,
            "trace_id": self.trace_id,
        }


# ==========================================================================
# 四、优化决策层数据结构
# ==========================================================================
@dataclass(frozen=True)
class ParameterSpec:
    """可调运行参数的定义（决策变量）。"""

    param_id: str
    name: str
    param_type: ParamType = ParamType.CONTINUOUS
    lower: float = 0.0
    upper: float = 1.0
    step: float = 0.0
    unit: str = ""
    #: 单次调整允许的最大变化量（工程值），安全门据此限速
    max_delta: float = math.inf
    #: 绑定的可写测点
    write_point_id: str = ""
    choices: Tuple[Any, ...] = ()
    #: 参数调整后现场生效所需时间（秒）
    settle_seconds: float = 30.0
    description: str = ""

    def clip(self, value: float) -> float:
        """裁剪到定义域，并按步长对齐。"""
        v = min(max(value, self.lower), self.upper)
        if self.param_type is ParamType.INTEGER:
            return float(round(v))
        if self.step and self.step > 0:
            n = round((v - self.lower) / self.step)
            v = self.lower + n * self.step
            v = min(max(v, self.lower), self.upper)
        return float(v)

    def span(self) -> float:
        return max(self.upper - self.lower, 1e-12)

    def normalize(self, value: float) -> float:
        """映射到 [0,1]，供优化算法在统一尺度上搜索。"""
        return (value - self.lower) / self.span()

    def denormalize(self, unit_value: float) -> float:
        return self.clip(self.lower + unit_value * self.span())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "param_id": self.param_id,
            "name": self.name,
            "type": self.param_type.code,
            "lower": _clean_float(self.lower),
            "upper": _clean_float(self.upper),
            "step": self.step,
            "unit": self.unit,
            "max_delta": _clean_float(self.max_delta),
            "write_point_id": self.write_point_id,
            "settle_seconds": self.settle_seconds,
            "description": self.description,
        }


@dataclass
class ObjectiveSpec:
    """优化目标定义。"""

    objective_id: str
    name: str
    sense: ObjectiveSense = ObjectiveSense.MINIMIZE
    weight: float = 1.0
    unit: str = ""
    #: 期望达到的目标值，用于计算达成率
    target: Optional[float] = None
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "objective_id": self.objective_id,
            "name": self.name,
            "sense": self.sense.code,
            "weight": self.weight,
            "unit": self.unit,
            "target": self.target,
            "description": self.description,
        }


@dataclass
class Evidence:
    """建议依据的一条证据，用于生成可解释说明。"""

    kind: EvidenceKind
    source_id: str
    statement: str
    confidence: float = 1.0
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.code,
            "kind_label": self.kind.label,
            "source_id": self.source_id,
            "statement": self.statement,
            "confidence": round(self.confidence, 4),
            "detail": self.detail,
        }


@dataclass
class ParameterChange:
    """一项参数的建议变更。"""

    param_id: str
    name: str
    current_value: float
    proposed_value: float
    unit: str = ""
    write_point_id: str = ""
    reason: str = ""

    @property
    def delta(self) -> float:
        return self.proposed_value - self.current_value

    @property
    def delta_ratio(self) -> float:
        base = abs(self.current_value)
        return self.delta / base if base > 1e-9 else math.inf

    def to_dict(self) -> Dict[str, Any]:
        return {
            "param_id": self.param_id,
            "name": self.name,
            "current_value": _clean_float(self.current_value),
            "proposed_value": _clean_float(self.proposed_value),
            "delta": _clean_float(self.delta),
            "unit": self.unit,
            "write_point_id": self.write_point_id,
            "reason": self.reason,
        }


@dataclass
class OptimizationAdvice:
    """运行参数优化建议。系统对外交付的核心决策产物。"""

    advice_id: str = field(default_factory=lambda: new_id("adv"))
    device_id: str = ""
    condition_id: str = ""
    changes: List[ParameterChange] = field(default_factory=list)
    #: 各目标的预测收益，如 {"energy": -0.043} 表示能耗预计下降 4.3%
    expected_gain: Dict[str, float] = field(default_factory=dict)
    #: 综合置信度
    confidence: float = 0.0
    #: 风险评级
    risk: Severity = Severity.INFO
    evidences: List[Evidence] = field(default_factory=list)
    explanation: str = ""
    #: 生成该建议所用的算法
    algorithm: str = ""
    health_score: float = 1.0
    timestamp: float = field(default_factory=now_ts)
    trace_id: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.changes

    def gain_of(self, objective_id: str) -> float:
        return self.expected_gain.get(objective_id, 0.0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "advice_id": self.advice_id,
            "device_id": self.device_id,
            "condition_id": self.condition_id,
            "changes": [c.to_dict() for c in self.changes],
            "expected_gain": {k: round(v, 6) for k, v in self.expected_gain.items()},
            "confidence": round(self.confidence, 4),
            "risk": self.risk.code,
            "algorithm": self.algorithm,
            "health_score": round(self.health_score, 4),
            "explanation": self.explanation,
            "evidences": [e.to_dict() for e in self.evidences],
            "timestamp": self.timestamp,
            "trace_id": self.trace_id,
        }


# ==========================================================================
# 五、控制执行层数据结构
# ==========================================================================
@dataclass
class WriteItem:
    """单点写入项。"""

    point_id: str
    value: float
    previous_value: Optional[float] = None
    unit: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "point_id": self.point_id,
            "value": _clean_float(self.value),
            "previous_value": _clean_float(self.previous_value),
            "unit": self.unit,
        }


@dataclass
class ControlCommand:
    """控制指令。由建议经安全校验后生成，是唯一允许触达现场的写载体。"""

    command_id: str = field(default_factory=lambda: new_id("cmd"))
    device_id: str = ""
    advice_id: str = ""
    items: List[WriteItem] = field(default_factory=list)
    mode: ControlMode = ControlMode.ADVISORY
    state: CommandState = CommandState.DRAFT
    #: 安全门给出的裁定说明
    safety_notes: List[str] = field(default_factory=list)
    #: 驳回原因（state=REJECTED 时有效）
    reject_reason: str = ""
    approver: str = ""
    created_at: float = field(default_factory=now_ts)
    dispatched_at: Optional[float] = None
    verified_at: Optional[float] = None
    trace_id: str = ""

    def transition(self, state: CommandState, note: str = "") -> None:
        """状态迁移并记录说明。"""
        self.state = state
        if note:
            self.safety_notes.append(f"[{state.label}] {note}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command_id": self.command_id,
            "device_id": self.device_id,
            "advice_id": self.advice_id,
            "items": [i.to_dict() for i in self.items],
            "mode": self.mode.code,
            "state": self.state.code,
            "state_label": self.state.label,
            "safety_notes": list(self.safety_notes),
            "reject_reason": self.reject_reason,
            "approver": self.approver,
            "created_at": self.created_at,
            "dispatched_at": self.dispatched_at,
            "verified_at": self.verified_at,
            "trace_id": self.trace_id,
        }


@dataclass
class ExecutionRecord:
    """指令执行结果记录。"""

    command_id: str
    success: bool
    #: 各点回读值
    readback: Dict[str, float] = field(default_factory=dict)
    #: 回读不一致的点
    mismatches: List[str] = field(default_factory=list)
    error: str = ""
    duration_ms: float = 0.0
    rolled_back: bool = False
    timestamp: float = field(default_factory=now_ts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command_id": self.command_id,
            "success": self.success,
            "readback": {k: _clean_float(v) for k, v in self.readback.items()},
            "mismatches": list(self.mismatches),
            "error": self.error,
            "duration_ms": round(self.duration_ms, 3),
            "rolled_back": self.rolled_back,
            "timestamp": self.timestamp,
        }


# ==========================================================================
# 六、反馈学习层数据结构
# ==========================================================================
@dataclass
class MetricComparison:
    """单项指标在执行前后的对比。"""

    metric: str
    label: str
    before_mean: float
    after_mean: float
    before_std: float = 0.0
    after_std: float = 0.0
    sample_before: int = 0
    sample_after: int = 0
    #: Welch t 检验统计量与显著性判定
    t_stat: float = 0.0
    significant: bool = False
    unit: str = ""

    @property
    def delta(self) -> float:
        return self.after_mean - self.before_mean

    @property
    def delta_ratio(self) -> float:
        base = abs(self.before_mean)
        return self.delta / base if base > 1e-9 else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric": self.metric,
            "label": self.label,
            "before_mean": _clean_float(self.before_mean),
            "after_mean": _clean_float(self.after_mean),
            "before_std": _clean_float(self.before_std),
            "after_std": _clean_float(self.after_std),
            "sample_before": self.sample_before,
            "sample_after": self.sample_after,
            "delta": _clean_float(self.delta),
            "delta_ratio": round(self.delta_ratio, 6),
            "t_stat": round(self.t_stat, 4),
            "significant": self.significant,
            "unit": self.unit,
        }


@dataclass
class EffectEvaluation:
    """执行效果评估结果，是反馈学习的输入。"""

    command_id: str
    advice_id: str = ""
    device_id: str = ""
    condition_id: str = ""
    comparisons: List[MetricComparison] = field(default_factory=list)
    #: 标量化奖励，正值表示本次调整有正收益
    reward: float = 0.0
    #: 预测收益与实际收益的偏差
    prediction_error: Dict[str, float] = field(default_factory=dict)
    accepted: bool = False
    verdict: str = ""
    observation_seconds: float = 0.0
    timestamp: float = field(default_factory=now_ts)
    trace_id: str = ""

    def comparison(self, metric: str) -> Optional[MetricComparison]:
        for c in self.comparisons:
            if c.metric == metric:
                return c
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command_id": self.command_id,
            "advice_id": self.advice_id,
            "device_id": self.device_id,
            "condition_id": self.condition_id,
            "reward": round(self.reward, 6),
            "accepted": self.accepted,
            "verdict": self.verdict,
            "observation_seconds": self.observation_seconds,
            "prediction_error": {k: round(v, 6) for k, v in self.prediction_error.items()},
            "comparisons": [c.to_dict() for c in self.comparisons],
            "timestamp": self.timestamp,
            "trace_id": self.trace_id,
        }


@dataclass
class ExperienceCase:
    """经验案例：一次完整闭环的浓缩记录，回写知识库供后续复用。"""

    case_id: str = field(default_factory=lambda: new_id("case"))
    device_id: str = ""
    device_type: DeviceType = DeviceType.GENERIC
    condition_id: str = ""
    #: 工况特征签名（相似度检索用）
    signature: Dict[str, float] = field(default_factory=dict)
    #: 调整前参数
    params_before: Dict[str, float] = field(default_factory=dict)
    #: 调整后参数
    params_after: Dict[str, float] = field(default_factory=dict)
    health_score: float = 1.0
    fault_pattern: FaultPattern = FaultPattern.NORMAL
    reward: float = 0.0
    accepted: bool = False
    #: 案例被复用的次数
    reuse_count: int = 0
    #: 案例可信度，随复用效果动态调整
    credibility: float = 0.5
    created_at: float = field(default_factory=now_ts)
    updated_at: float = field(default_factory=now_ts)
    remark: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "device_id": self.device_id,
            "device_type": self.device_type.code,
            "condition_id": self.condition_id,
            "signature": {k: _clean_float(v) for k, v in self.signature.items()},
            "params_before": {k: _clean_float(v) for k, v in self.params_before.items()},
            "params_after": {k: _clean_float(v) for k, v in self.params_after.items()},
            "health_score": round(self.health_score, 4),
            "fault_pattern": self.fault_pattern.code,
            "reward": round(self.reward, 6),
            "accepted": self.accepted,
            "reuse_count": self.reuse_count,
            "credibility": round(self.credibility, 4),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "remark": self.remark,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ExperienceCase":
        return cls(
            case_id=raw.get("case_id", new_id("case")),
            device_id=raw.get("device_id", ""),
            device_type=DeviceType.from_text(raw.get("device_type"), DeviceType.GENERIC),
            condition_id=raw.get("condition_id", ""),
            signature=dict(raw.get("signature") or {}),
            params_before=dict(raw.get("params_before") or {}),
            params_after=dict(raw.get("params_after") or {}),
            health_score=float(raw.get("health_score", 1.0)),
            fault_pattern=FaultPattern.from_text(raw.get("fault_pattern"), FaultPattern.NORMAL),
            reward=float(raw.get("reward", 0.0)),
            accepted=bool(raw.get("accepted", False)),
            reuse_count=int(raw.get("reuse_count", 0)),
            credibility=float(raw.get("credibility", 0.5)),
            created_at=float(raw.get("created_at", now_ts())),
            updated_at=float(raw.get("updated_at", now_ts())),
            remark=raw.get("remark", ""),
        )


# ==========================================================================
# 七、全链路决策记录
# ==========================================================================
@dataclass
class DecisionRecord:
    """一次完整决策的全链路留痕，供审计与追溯使用。"""

    trace_id: str
    device_id: str
    frame: Optional[Dict[str, Any]] = None
    condition: Optional[Dict[str, Any]] = None
    health: Optional[Dict[str, Any]] = None
    advice: Optional[Dict[str, Any]] = None
    command: Optional[Dict[str, Any]] = None
    execution: Optional[Dict[str, Any]] = None
    evaluation: Optional[Dict[str, Any]] = None
    created_at: float = field(default_factory=now_ts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "device_id": self.device_id,
            "created_at": self.created_at,
            "frame": self.frame,
            "condition": self.condition,
            "health": self.health,
            "advice": self.advice,
            "command": self.command,
            "execution": self.execution,
            "evaluation": self.evaluation,
        }


__all__ = [
    "now_ts",
    "new_id",
    "SignalPoint",
    "Sample",
    "SampleFrame",
    "FeatureVector",
    "ConditionSnapshot",
    "HealthIndicator",
    "HealthReport",
    "ParameterSpec",
    "ObjectiveSpec",
    "Evidence",
    "ParameterChange",
    "OptimizationAdvice",
    "WriteItem",
    "ControlCommand",
    "ExecutionRecord",
    "MetricComparison",
    "EffectEvaluation",
    "ExperienceCase",
    "DecisionRecord",
]

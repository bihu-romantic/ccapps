"""
执行效果评估与反馈学习模块
==========================

对应系统功能五。这是让系统 "越用越准" 的闭环最后一环，也是与
"只会给建议的分析软件" 的根本区别：每一次调整都要回答三个问题——

1. **到底有没有效？**（:class:`EffectEvaluator`）
   取下发前后两段等长窗口，对能耗、产量、振动、温度做统计检验。
   只看均值差会被工况漂移骗过去，因此必须做显著性判定：
   变化幅度大但方差更大，不算有效。

2. **这次经验值多少分？**（:class:`RewardCalculator`）
   把多个指标的变化折算成一个标量奖励，并对 "省了电但设备更抖" 这类
   拆东墙补西墙的调整施加惩罚。

3. **学到的东西写回哪里？**（:class:`FeedbackLearner`）
   四个去处：代理模型加一条真实样本、规则可信度升降、案例库入库、
   工况档案更新最优参数。下一次同工况再遇到，系统就直接用这次的经验。
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..acquisition.buffer import TimeSeriesBuffer
from ..core.audit import ACTION_EVALUATE, ACTION_LEARN, AuditTrail
from ..core.enums import DeviceType, EventTopic, SignalKind
from ..core.event_bus import EventBus
from ..core.logging_setup import get_logger
from ..core.registry import DeviceProfile
from ..core.scheduler import Scheduler
from ..core.types import (
    ControlCommand,
    EffectEvaluation,
    ExperienceCase,
    MetricComparison,
    OptimizationAdvice,
    now_ts,
)
from ..core.utils import clamp, robust_stats, safe_div, welch_t_test

logger = get_logger("feedback.learner")


# ==========================================================================
# 观测指标定义
# ==========================================================================
@dataclass
class ObservedMetric:
    """一个待观测的效果指标。"""

    metric: str
    label: str
    point_id: str = ""
    feature: str = ""
    unit: str = ""
    #: 期望方向：``down`` 越小越好 / ``up`` 越大越好
    better: str = "down"
    weight: float = 1.0


def default_metrics(profile: DeviceProfile) -> List[ObservedMetric]:
    """按设备测点表生成默认观测指标集。"""
    metrics: List[ObservedMetric] = []
    power = profile.first_point_of(SignalKind.POWER)
    if power:
        metrics.append(ObservedMetric("power", "有功功率", power.point_id,
                                      unit=power.unit or "kW", better="down", weight=1.0))
    flow = profile.first_point_of(SignalKind.FLOW)
    if flow:
        metrics.append(ObservedMetric("flow", "产出流量", flow.point_id,
                                      unit=flow.unit or "m³/h", better="up", weight=0.9))
    for vib in profile.points_of_kind(SignalKind.VIBRATION)[:2]:
        metrics.append(ObservedMetric(f"vib_{vib.point_id}", f"{vib.name}振动",
                                      vib.point_id, unit=vib.unit or "mm/s",
                                      better="down", weight=0.8))
    for temp in profile.points_of_kind(SignalKind.TEMPERATURE)[:2]:
        if "ambient" in temp.point_id:
            continue
        metrics.append(ObservedMetric(f"temp_{temp.point_id}", f"{temp.name}温度",
                                      temp.point_id, unit=temp.unit or "℃",
                                      better="down", weight=0.6))
    current = profile.first_point_of(SignalKind.CURRENT)
    if current:
        metrics.append(ObservedMetric("current", "运行电流", current.point_id,
                                      unit=current.unit or "A", better="down", weight=0.5))
    return metrics


# ==========================================================================
# 效果评估
# ==========================================================================
class EffectEvaluator:
    """执行效果评估器。"""

    def __init__(self, buffer: TimeSeriesBuffer, before_seconds: float = 600.0,
                 after_seconds: float = 600.0, settle_seconds: float = 120.0,
                 t_threshold: float = 2.0, min_samples: int = 8):
        self.buffer = buffer
        self.before_seconds = before_seconds
        self.after_seconds = after_seconds
        self.settle_seconds = settle_seconds
        self.t_threshold = t_threshold
        self.min_samples = min_samples

    def evaluate(self, profile: DeviceProfile, command: ControlCommand,
                 metrics: Optional[Sequence[ObservedMetric]] = None,
                 dispatch_ts: Optional[float] = None) -> EffectEvaluation:
        """对比下发前后的指标变化。"""
        metrics = list(metrics or default_metrics(profile))
        t0 = dispatch_ts or command.dispatched_at or command.created_at
        before_end = t0
        before_start = t0 - self.before_seconds
        after_start = t0 + self.settle_seconds
        after_end = after_start + self.after_seconds

        evaluation = EffectEvaluation(
            command_id=command.command_id, advice_id=command.advice_id,
            device_id=profile.device_id, trace_id=command.trace_id,
            observation_seconds=self.before_seconds + self.after_seconds,
        )

        for metric in metrics:
            if not metric.point_id:
                continue
            before = self.buffer.window_between(profile.device_id, metric.point_id,
                                                before_start, before_end)
            after = self.buffer.window_between(profile.device_id, metric.point_id,
                                               after_start, after_end)
            if len(before) < self.min_samples or len(after) < self.min_samples:
                logger.debug("指标 %s 样本不足（前 %d / 后 %d），跳过",
                             metric.metric, len(before), len(after))
                continue
            b_stats = robust_stats(before.values)
            a_stats = robust_stats(after.values)
            t_stat, _df = welch_t_test(after.values, before.values)
            evaluation.comparisons.append(MetricComparison(
                metric=metric.metric, label=metric.label,
                before_mean=b_stats["mean"], after_mean=a_stats["mean"],
                before_std=b_stats["std"], after_std=a_stats["std"],
                sample_before=len(before), sample_after=len(after),
                t_stat=t_stat, significant=abs(t_stat) >= self.t_threshold,
                unit=metric.unit,
            ))
        return evaluation

    def can_evaluate(self, device_id: str, dispatch_ts: float) -> bool:
        """判断观测窗数据是否已经攒够。"""
        need_until = dispatch_ts + self.settle_seconds + self.after_seconds
        coverage = self.buffer.coverage(device_id)
        newest = coverage.get("newest")
        return newest is not None and newest >= need_until


# ==========================================================================
# 奖励计算
# ==========================================================================
class RewardCalculator:
    """把多指标变化折算为标量奖励。"""

    def __init__(self, metric_weights: Optional[Dict[str, float]] = None,
                 penalty_factor: float = 1.5, significance_bonus: float = 0.2):
        self.metric_weights = metric_weights or {}
        #: 对负向变化的惩罚系数：变差比变好更该被记住
        self.penalty_factor = penalty_factor
        self.significance_bonus = significance_bonus

    def compute(self, evaluation: EffectEvaluation,
                metrics: Sequence[ObservedMetric]) -> float:
        """计算奖励。正值表示本次调整整体有益。"""
        spec = {m.metric: m for m in metrics}
        total = 0.0
        weight_sum = 0.0
        for comp in evaluation.comparisons:
            m = spec.get(comp.metric)
            if m is None:
                continue
            weight = float(self.metric_weights.get(comp.metric, m.weight))
            # 归一化的相对变化，并按期望方向定符号
            change = comp.delta_ratio
            gain = -change if m.better == "down" else change
            # 未通过显著性检验的变化打折，避免把噪声当成效果
            if not comp.significant:
                gain *= 0.35
            else:
                gain *= (1.0 + self.significance_bonus)
            if gain < 0:
                gain *= self.penalty_factor
            total += weight * gain
            weight_sum += weight
        reward = safe_div(total, weight_sum)
        return float(clamp(reward, -1.0, 1.0))

    def verdict(self, reward: float, evaluation: EffectEvaluation) -> str:
        """生成人类可读的评估结论。"""
        significant = [c for c in evaluation.comparisons if c.significant]
        if not evaluation.comparisons:
            return "观测窗数据不足，本次调整效果无法判定。"
        head = ("本次调整整体有效" if reward > 0.02 else
                "本次调整效果不明显" if reward > -0.02 else "本次调整产生负面影响")
        details = "；".join(
            f"{c.label}{'下降' if c.delta < 0 else '上升'}{abs(c.delta_ratio) * 100:.1f}%"
            f"（{'显著' if c.significant else '不显著'}）"
            for c in evaluation.comparisons[:4]
        )
        return f"{head}（奖励 {reward:+.3f}，{len(significant)} 项通过显著性检验）：{details}。"


# ==========================================================================
# 在线学习
# ==========================================================================
class OnlineLearner:
    """线性上下文老虎机（LinUCB 简化版）。

    用途：在若干候选调整方向之间做在线权衡。它不替代代理模型，
    而是解决 "同一工况下多种调法都说得通，先试哪个" 的问题，
    并保证探索比例可控——工业现场不允许无节制试错。
    """

    def __init__(self, dim: int, alpha: float = 0.8, ridge: float = 1.0):
        self.dim = max(1, dim)
        self.alpha = alpha
        self._A: Dict[str, Any] = {}
        self._b: Dict[str, Any] = {}
        self.ridge = ridge
        self._lock = threading.RLock()
        self.updates = 0

    def _ensure(self, arm: str) -> None:
        import numpy as np
        if arm not in self._A:
            self._A[arm] = np.eye(self.dim) * self.ridge
            self._b[arm] = np.zeros(self.dim)

    def score(self, arm: str, context: Sequence[float]) -> float:
        """置信上界得分 = 预测收益 + α × 不确定度。"""
        import numpy as np
        with self._lock:
            self._ensure(arm)
            A_inv = np.linalg.inv(self._A[arm])
            theta = A_inv @ self._b[arm]
        x = np.asarray(context, dtype=float)[: self.dim]
        if x.size < self.dim:
            x = np.pad(x, (0, self.dim - x.size))
        mean = float(theta @ x)
        bonus = self.alpha * math.sqrt(max(float(x @ A_inv @ x), 0.0))
        return mean + bonus

    def select(self, arms: Sequence[str], context: Sequence[float]) -> str:
        """选择得分最高的动作。"""
        if not arms:
            raise ValueError("候选动作不能为空")
        return max(arms, key=lambda a: self.score(a, context))

    def update(self, arm: str, context: Sequence[float], reward: float) -> None:
        """用实际奖励更新该动作的模型。"""
        import numpy as np
        x = np.asarray(context, dtype=float)[: self.dim]
        if x.size < self.dim:
            x = np.pad(x, (0, self.dim - x.size))
        with self._lock:
            self._ensure(arm)
            self._A[arm] = self._A[arm] + np.outer(x, x)
            self._b[arm] = self._b[arm] + reward * x
            self.updates += 1

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {"arms": sorted(self._A), "updates": self.updates,
                    "dim": self.dim, "alpha": self.alpha}


# ==========================================================================
# 反馈学习协调器
# ==========================================================================
class FeedbackLearner:
    """把评估结果分发到四个学习去处，并安排延时评估任务。"""

    def __init__(
        self,
        evaluator: EffectEvaluator,
        reward_calc: Optional[RewardCalculator] = None,
        scheduler: Optional[Scheduler] = None,
        bus: Optional[EventBus] = None,
        audit: Optional[AuditTrail] = None,
        accept_reward: float = 0.0,
        rule_credit_step: float = 0.05,
    ):
        self.evaluator = evaluator
        self.reward_calc = reward_calc or RewardCalculator()
        self.scheduler = scheduler
        self.bus = bus
        self.audit = audit
        self.accept_reward = accept_reward
        self.rule_credit_step = rule_credit_step
        self.learner = OnlineLearner(dim=6)
        #: 学习结果的下游钩子，由引擎注入
        self.on_learned: Optional[Callable[[EffectEvaluation, ExperienceCase], None]] = None
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._history: List[EffectEvaluation] = []
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    def schedule_evaluation(self, profile: DeviceProfile, command: ControlCommand,
                            advice: Optional[OptimizationAdvice] = None,
                            rule_ids: Optional[Sequence[str]] = None,
                            condition_id: str = "",
                            signature: Optional[Dict[str, float]] = None,
                            health_score: float = 1.0) -> Optional[str]:
        """下发成功后调用：登记待评估任务并安排延时回调。"""
        payload = {
            "profile": profile, "command": command, "advice": advice,
            "rule_ids": list(rule_ids or []), "condition_id": condition_id,
            "signature": dict(signature or {}), "health_score": health_score,
            "dispatch_ts": command.dispatched_at or now_ts(),
            "metrics": default_metrics(profile),
        }
        with self._lock:
            self._pending[command.command_id] = payload

        delay = (self.evaluator.settle_seconds + self.evaluator.after_seconds)
        if self.scheduler is None:
            logger.info("未接入调度器，指令 %s 需手动触发评估", command.command_id)
            return None
        task_id = self.scheduler.once(
            delay, lambda cid=command.command_id: self.run_evaluation(cid),
            name=f"evaluate:{command.command_id}",
        )
        logger.info("指令 %s 的效果评估已排期，%.0fs 后执行", command.command_id, delay)
        return task_id

    def run_evaluation(self, command_id: str) -> Optional[EffectEvaluation]:
        """执行一次效果评估并完成学习回写。"""
        with self._lock:
            payload = self._pending.pop(command_id, None)
        if payload is None:
            logger.warning("待评估队列中不存在指令 %s", command_id)
            return None

        profile: DeviceProfile = payload["profile"]
        command: ControlCommand = payload["command"]
        metrics: List[ObservedMetric] = payload["metrics"]

        evaluation = self.evaluator.evaluate(profile, command, metrics,
                                             payload["dispatch_ts"])
        evaluation.condition_id = payload["condition_id"]
        evaluation.reward = self.reward_calc.compute(evaluation, metrics)
        evaluation.accepted = evaluation.reward > self.accept_reward
        evaluation.verdict = self.reward_calc.verdict(evaluation.reward, evaluation)

        advice: Optional[OptimizationAdvice] = payload["advice"]
        if advice is not None:
            evaluation.prediction_error = self._prediction_error(advice, evaluation)

        case = self._build_case(profile, command, payload, evaluation)

        # 在线学习：用工况签名作为上下文，参数方向作为动作
        arm = self._arm_of(command)
        context = self._context_of(payload["signature"], payload["health_score"])
        self.learner.update(arm, context, evaluation.reward)

        with self._lock:
            self._history.append(evaluation)
            if len(self._history) > 500:
                del self._history[: len(self._history) - 500]

        if self.audit:
            self.audit.record(ACTION_EVALUATE, device_id=profile.device_id,
                              trace_id=command.trace_id,
                              result="accepted" if evaluation.accepted else "rejected",
                              detail={"evaluation": evaluation.to_dict()})
            self.audit.record(ACTION_LEARN, device_id=profile.device_id,
                              trace_id=command.trace_id,
                              detail={"case_id": case.case_id,
                                      "reward": round(evaluation.reward, 4),
                                      "rule_ids": payload["rule_ids"]})
        if self.bus:
            self.bus.publish(EventTopic.EFFECT_EVALUATED, evaluation,
                             device_id=profile.device_id, trace_id=command.trace_id)
            self.bus.publish(EventTopic.KNOWLEDGE_UPDATED,
                             {"case_id": case.case_id,
                              "accepted": evaluation.accepted,
                              "reward": round(evaluation.reward, 4)},
                             device_id=profile.device_id, trace_id=command.trace_id)

        if self.on_learned is not None:
            try:
                self.on_learned(evaluation, case)
            except Exception as exc:  # noqa: BLE001
                logger.error("学习回写钩子执行失败: %s", exc)

        logger.info("指令 %s 效果评估完成：%s", command_id, evaluation.verdict)
        return evaluation

    # ------------------------------------------------------------------
    @staticmethod
    def _prediction_error(advice: OptimizationAdvice,
                          evaluation: EffectEvaluation) -> Dict[str, float]:
        """预测收益与实际收益之差，用于校准代理模型可信度。"""
        actual: Dict[str, float] = {}
        for comp in evaluation.comparisons:
            if comp.metric in ("power", "current"):
                actual["energy"] = comp.delta_ratio
            elif comp.metric == "flow":
                actual["throughput"] = comp.delta_ratio
            elif comp.metric.startswith("vib_"):
                actual.setdefault("health_cost", comp.delta_ratio)
        return {k: actual[k] - advice.gain_of(k) for k in actual}

    def _build_case(self, profile: DeviceProfile, command: ControlCommand,
                    payload: Dict[str, Any],
                    evaluation: EffectEvaluation) -> ExperienceCase:
        """把本次闭环浓缩成一条经验案例。"""
        before = {i.point_id: (i.previous_value if i.previous_value is not None else 0.0)
                  for i in command.items}
        after = {i.point_id: i.value for i in command.items}
        # 把测点键换回参数键，便于案例复用时直接对上参数表
        point_to_param = {s.write_point_id: pid
                          for pid, s in profile.parameters.items() if s.write_point_id}
        params_before = {point_to_param.get(k, k): v for k, v in before.items()}
        params_after = {point_to_param.get(k, k): v for k, v in after.items()}
        return ExperienceCase(
            device_id=profile.device_id,
            device_type=profile.device_type,
            condition_id=payload["condition_id"],
            signature=payload["signature"],
            params_before=params_before,
            params_after=params_after,
            health_score=payload["health_score"],
            reward=evaluation.reward,
            accepted=evaluation.accepted,
            credibility=clamp(0.5 + evaluation.reward, 0.05, 0.95),
            remark=evaluation.verdict,
        )

    @staticmethod
    def _arm_of(command: ControlCommand) -> str:
        """把一次调整抽象成 "动作臂"：各参数的调整方向组合。"""
        parts = []
        for item in command.items:
            prev = item.previous_value
            if prev is None:
                parts.append(f"{item.point_id}:?")
            elif item.value > prev:
                parts.append(f"{item.point_id}:up")
            elif item.value < prev:
                parts.append(f"{item.point_id}:down")
            else:
                parts.append(f"{item.point_id}:hold")
        return "|".join(sorted(parts)) or "noop"

    @staticmethod
    def _context_of(signature: Dict[str, float], health_score: float) -> List[float]:
        """构造上下文向量：工况签名 + 健康度。"""
        keys = ("dev.load_ratio", "dev.speed_ratio", "dev.current_ratio",
                "dev.temp_mean")
        ctx = [float(signature.get(k, 0.0)) for k in keys]
        ctx.append(health_score)
        ctx.append(1.0)  # 偏置项
        return ctx

    # ------------------------------------------------------------------
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def pending_command_ids(self) -> List[str]:
        """列出待评估的指令号。接口层与手动触发评估时使用。"""
        with self._lock:
            return list(self._pending)

    def history(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            return [e.to_dict() for e in self._history[-limit:]]

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            evals = list(self._history)
        accepted = [e for e in evals if e.accepted]
        return {
            "evaluations": len(evals),
            "accepted": len(accepted),
            "accept_rate": round(len(accepted) / len(evals), 4) if evals else 0.0,
            "avg_reward": round(sum(e.reward for e in evals) / len(evals), 4)
            if evals else 0.0,
            "pending": self.pending_count(),
            "bandit": self.learner.stats(),
        }


__all__ = [
    "EffectEvaluator", "RewardCalculator", "OnlineLearner", "FeedbackLearner",
    "ObservedMetric", "default_metrics",
]

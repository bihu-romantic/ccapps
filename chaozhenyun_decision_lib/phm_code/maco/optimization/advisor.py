"""
优化建议生成模块
================

对应系统功能三：**运行参数优化建议**。本模块是决策层的汇合点，
把四路输入合成一条可执行、可解释的建议：

1. 工况快照 —— 决定在哪个上下文里优化，以及是否允许优化；
2. 健康报告 —— 决定优化目标偏向（劣化时以保设备为先，健康时以省能为先）；
3. 知识推理 —— 规则给出方向性动作与硬性禁令，案例给出历史有效参数；
4. 代理模型 —— 在参数邻域内做定量寻优。

产出的 :class:`OptimizationAdvice` 必带证据链，界面上能逐条展开
"为什么建议把风机频率从 45Hz 调到 43.6Hz"，这是工业软件被采纳的前提。
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..core.enums import EvidenceKind, HealthGrade, ObjectiveSense, Severity
from ..core.event_bus import EventBus
from ..core.enums import EventTopic
from ..core.logging_setup import get_logger
from ..core.registry import DeviceProfile
from ..core.types import (
    ConditionSnapshot,
    Evidence,
    HealthReport,
    ObjectiveSpec,
    OptimizationAdvice,
    ParameterChange,
    ParameterSpec,
    new_id,
)
from ..core.utils import clamp, safe_div
from ..knowledge.base import InferenceResult, KnowledgeBase
from .optimizer import OptimizeResult, SearchSpace, SurrogateModel, build_optimizer

logger = get_logger("optimization.advisor")

#: 规则动作里的调整量写法：``+5%`` / ``-2`` / ``=45``
_ADJUST_RE = re.compile(r"^\s*(?P<op>[+\-=])\s*(?P<num>\d+(?:\.\d+)?)\s*(?P<pct>%?)\s*$")


def parse_adjustment(current: float, expression: Any, spec: ParameterSpec) -> Optional[float]:
    """把规则给出的调整表达式折算为目标值。"""
    if isinstance(expression, (int, float)):
        return spec.clip(float(expression))
    m = _ADJUST_RE.match(str(expression))
    if not m:
        return None
    num = float(m.group("num"))
    op = m.group("op")
    if op == "=":
        return spec.clip(num)
    delta = current * num / 100.0 if m.group("pct") else num
    return spec.clip(current + delta if op == "+" else current - delta)


def default_objectives() -> List[ObjectiveSpec]:
    """默认三目标：省能、保产、护设备。"""
    return [
        ObjectiveSpec("energy", "单位能耗", ObjectiveSense.MINIMIZE, 0.45, "kW·h",
                      description="单位产出所消耗的电能，越低越好"),
        ObjectiveSpec("throughput", "产出量", ObjectiveSense.MAXIMIZE, 0.30, "m³/h",
                      description="单位时间产出，越高越好"),
        ObjectiveSpec("health_cost", "健康损耗", ObjectiveSense.MINIMIZE, 0.25, "",
                      description="本次运行对设备寿命的消耗，越低越好"),
    ]


class OptimizationAdvisor:
    """优化建议生成器。"""

    def __init__(
        self,
        knowledge: KnowledgeBase,
        bus: Optional[EventBus] = None,
        objectives: Optional[Sequence[ObjectiveSpec]] = None,
        algorithm: str = "auto",
        max_iterations: int = 40,
        max_delta_ratio: float = 0.10,
        max_changed_params: int = 3,
        min_confidence: float = 0.55,
        surrogate_min_samples: int = 12,
        objective_weights: Optional[Dict[str, float]] = None,
    ):
        self.knowledge = knowledge
        self.bus = bus
        self.objectives = list(objectives or default_objectives())
        self.algorithm = algorithm
        self.max_iterations = max_iterations
        self.max_delta_ratio = max_delta_ratio
        self.max_changed_params = max_changed_params
        self.min_confidence = min_confidence
        self.surrogate_min_samples = surrogate_min_samples
        self.objective_weights = objective_weights or {
            o.objective_id: o.weight for o in self.objectives
        }
        #: 每台设备一个代理模型
        self._surrogates: Dict[str, SurrogateModel] = {}

    # ------------------------------------------------------------------
    def surrogate(self, profile: DeviceProfile) -> SurrogateModel:
        key = profile.device_id
        model = self._surrogates.get(key)
        if model is None:
            space = SearchSpace(profile.parameter_list())
            model = SurrogateModel(space, self.objectives,
                                   min_samples=self.surrogate_min_samples)
            self._surrogates[key] = model
        return model

    def record_operating_point(self, profile: DeviceProfile,
                               params: Dict[str, float],
                               metrics: Dict[str, float]) -> None:
        """把一次实际运行工作点喂给代理模型。"""
        if not params:
            return
        self.surrogate(profile).add_sample(params, metrics)

    # ------------------------------------------------------------------
    def advise(
        self,
        profile: DeviceProfile,
        condition: ConditionSnapshot,
        health: HealthReport,
        current_params: Dict[str, float],
        metrics: Optional[Dict[str, float]] = None,
    ) -> OptimizationAdvice:
        """生成一条优化建议。"""
        advice = OptimizationAdvice(
            advice_id=new_id("adv"),
            device_id=profile.device_id,
            condition_id=condition.condition_id,
            health_score=health.score,
            trace_id=condition.trace_id,
        )
        if not profile.parameter_list():
            advice.explanation = "该设备未定义可调参数，无优化空间。"
            return advice

        # ---- 1. 知识推理：拿到禁令与方向 ----
        facts = self._build_facts(profile, condition, health, current_params, metrics)
        inference = self.knowledge.reason(facts)
        advice.evidences.extend(inference.evidences)

        blocked, reason = self._check_block(inference, condition, health)
        if blocked:
            advice.explanation = f"本轮不建议调整：{reason}"
            advice.confidence = inference.confidence
            advice.risk = Severity.INFO
            self._publish(advice)
            return advice

        # ---- 2. 约束检查 ----
        feasible, constraint_ev = self.knowledge.check_constraints(facts)
        advice.evidences.extend(constraint_ev)
        if not feasible:
            advice.explanation = "当前运行点已违反硬约束，须先恢复到安全区间再谈优化。"
            advice.risk = Severity.ALARM
            advice.confidence = 0.9
            self._publish(advice)
            return advice

        # ---- 3. 候选生成：知识动作 / 案例复用 / 模型寻优 ----
        candidates: List[Tuple[str, Dict[str, float], float]] = []

        rule_params = self._from_rules(profile, inference, current_params)
        if rule_params:
            candidates.append(("知识规则", rule_params, inference.confidence))

        case_params, case_ev = self.knowledge.recall(
            condition.signature, condition.condition_id, profile.device_type.code
        )
        if case_params:
            advice.evidences.extend(case_ev)
            merged = dict(current_params)
            merged.update({k: v for k, v in case_params.items()
                           if k in profile.parameters})
            conf = max((e.confidence for e in case_ev), default=0.5)
            candidates.append(("历史案例", merged, conf))

        model_result = self._optimize(profile, condition, health, current_params)
        if model_result is not None and model_result.best_params:
            candidates.append(("代理模型寻优", model_result.best_params,
                               model_result.confidence))
            advice.algorithm = model_result.algorithm
            advice.evidences.append(Evidence(
                kind=EvidenceKind.MODEL,
                source_id=model_result.algorithm,
                statement=(f"在本工况 {self.surrogate(profile).sample_count} 条历史样本上"
                           f"寻优，综合得分 {model_result.best_score:+.4f}"),
                confidence=model_result.confidence,
                detail=model_result.to_dict(),
            ))

        if not candidates:
            advice.explanation = "知识、案例与模型均未给出可行调整方向，维持当前参数。"
            self._publish(advice)
            return advice

        # ---- 4. 候选融合与限幅 ----
        chosen_source, chosen_params, chosen_conf = max(candidates, key=lambda c: c[2])
        changes = self._build_changes(profile, current_params, chosen_params, inference)
        if not changes:
            advice.explanation = "计算结果与当前参数一致，无需调整。"
            self._publish(advice)
            return advice

        advice.changes = changes
        advice.confidence = clamp(chosen_conf, 0.0, 1.0)
        advice.expected_gain = self._expected_gain(profile, current_params, chosen_params)
        advice.risk = self._risk_of(changes, health)
        advice.explanation = self._explain(chosen_source, changes, advice, health,
                                           condition, inference)

        # 置信度不足时降级为 "仅供参考"，不进入控制链路
        if advice.confidence < self.min_confidence:
            advice.explanation += (f"（置信度 {advice.confidence:.2f} 低于阈值 "
                                   f"{self.min_confidence:.2f}，仅作参考不自动下发）")
        self._publish(advice)
        return advice

    # ------------------------------------------------------------------
    def _build_facts(self, profile: DeviceProfile, condition: ConditionSnapshot,
                     health: HealthReport, params: Dict[str, float],
                     metrics: Optional[Dict[str, float]]) -> Dict[str, Any]:
        """组装供规则推理与约束检查使用的事实集。"""
        facts: Dict[str, Any] = {
            "device_type": profile.device_type.code,
            "condition_id": condition.condition_id,
            "condition_is_new": condition.is_new,
            "stability": condition.stability.code,
            "condition_confidence": condition.confidence,
            "health_score": health.score,
            "health_grade": health.grade.code,
            "degradation_rate": health.degradation_rate,
            "fault_pattern": health.top_pattern.code,
        }
        facts.update(condition.signature)
        for key, value in condition.signature.items():
            facts[key.split(".")[-1]] = value
        for ind in health.indicators:
            short = ind.name.split(".")[-1]
            facts[f"{short}_dev"] = ind.deviation
            facts[short] = ind.value
        for pattern, conf in health.patterns:
            facts[f"pattern_{pattern.code}"] = conf
        facts.update({f"param.{k}": v for k, v in params.items()})
        if metrics:
            facts.update(metrics)
        # 便于规则书写的简名
        facts.setdefault("load_ratio", condition.signature.get("dev.load_ratio", 0.0))
        facts.setdefault("speed_ratio", condition.signature.get("dev.speed_ratio", 0.0))
        return facts

    @staticmethod
    def _check_block(inference: InferenceResult, condition: ConditionSnapshot,
                     health: HealthReport) -> Tuple[bool, str]:
        """判断本轮是否应当放弃优化。"""
        if inference.conclusions.get("block.optimize"):
            return True, str(inference.conclusions.get("note", "知识规则给出禁令"))
        if not condition.optimizable:
            return True, (f"工况处于「{condition.stability.label}」"
                          f"（置信度 {condition.confidence:.2f}），等待稳态")
        if health.grade is HealthGrade.CRITICAL:
            return True, "设备健康度处于危险等级，应转入检修流程"
        return False, ""

    def _from_rules(self, profile: DeviceProfile, inference: InferenceResult,
                    current: Dict[str, float]) -> Dict[str, float]:
        """把规则动作里的 ``adjust.*`` 折算成具体参数值。"""
        out: Dict[str, float] = dict(current)
        changed = False
        for key, expression in inference.conclusions.items():
            if not key.startswith("adjust."):
                continue
            param_id = key.split(".", 1)[1]
            spec = profile.parameters.get(param_id)
            if spec is None:
                continue
            now = current.get(param_id, (spec.lower + spec.upper) / 2)
            target = parse_adjustment(now, expression, spec)
            if target is not None and abs(target - now) > 1e-9:
                out[param_id] = target
                changed = True
        return out if changed else {}

    def _optimize(self, profile: DeviceProfile, condition: ConditionSnapshot,
                  health: HealthReport,
                  current: Dict[str, float]) -> Optional[OptimizeResult]:
        """在代理模型上做邻域寻优。"""
        model = self.surrogate(profile)
        if not model.trained:
            logger.debug("设备 %s 代理模型样本不足（%d/%d），跳过模型寻优",
                         profile.device_id, model.sample_count, model.min_samples)
            return None
        space = model.space
        # 健康度越低，允许的调整幅度越小
        delta_ratio = self.max_delta_ratio * clamp(health.score, 0.3, 1.0)
        bounds = space.restrict(current, delta_ratio)
        weights = self._dynamic_weights(health)

        def evaluate(unit: np.ndarray) -> float:
            predicted, conf = model.predict(unit)
            if not predicted:
                return -math.inf
            score = model.scalarize(predicted, weights)
            # 置信度低的区域打折，抑制模型外推
            return score * clamp(conf, 0.1, 1.0)

        optimizer = build_optimizer(
            self.algorithm, space,
            max_iterations=self.max_iterations,
            objective_count=len(self.objectives),
            sample_count=model.sample_count,
        )
        if hasattr(optimizer, "uncertainty"):
            optimizer.uncertainty = model.uncertainty  # type: ignore[attr-defined]
        start = space.to_unit(current)
        result = optimizer.optimize(evaluate, bounds, start)
        predicted, conf = model.predict(space.to_unit(result.best_params))
        result.predicted = predicted
        result.confidence = conf
        return result

    def _dynamic_weights(self, health: HealthReport) -> Dict[str, float]:
        """按健康度动态调整目标权重：越劣化越偏向保设备。"""
        weights = dict(self.objective_weights)
        if health.score < 0.75:
            shift = (0.75 - health.score) * 0.8
            weights["health_cost"] = weights.get("health_cost", 0.25) + shift
            weights["energy"] = max(weights.get("energy", 0.45) - shift * 0.6, 0.05)
            weights["throughput"] = max(weights.get("throughput", 0.30) - shift * 0.4, 0.05)
        return weights

    def _build_changes(self, profile: DeviceProfile, current: Dict[str, float],
                       proposed: Dict[str, float],
                       inference: InferenceResult) -> List[ParameterChange]:
        """生成参数变更列表，并施加数量与幅度限制。"""
        limit_ratio = inference.conclusions.get("limit.delta_ratio")
        max_ratio = float(limit_ratio) if isinstance(limit_ratio, (int, float)) \
            else self.max_delta_ratio

        raw: List[ParameterChange] = []
        for param_id, spec in profile.parameters.items():
            now = current.get(param_id)
            target = proposed.get(param_id)
            if now is None or target is None:
                continue
            target = spec.clip(target)
            # 幅度限制：既受相对比例约束，也受参数自身 max_delta 约束
            cap = min(abs(now) * max_ratio if abs(now) > 1e-9 else spec.span() * max_ratio,
                      spec.max_delta)
            if abs(target - now) > cap:
                target = spec.clip(now + math.copysign(cap, target - now))
            if abs(target - now) < max(abs(now) * 1e-4, 1e-6):
                continue
            reason = str(inference.conclusions.get("note", "")) or "综合寻优结果"
            raw.append(ParameterChange(
                param_id=param_id, name=spec.name, current_value=now,
                proposed_value=target, unit=spec.unit,
                write_point_id=spec.write_point_id, reason=reason,
            ))
        # 只保留变化幅度最大的前 N 项，一次不动太多变量，便于归因
        raw.sort(key=lambda c: abs(c.delta_ratio), reverse=True)
        return raw[: self.max_changed_params]

    def _expected_gain(self, profile: DeviceProfile, current: Dict[str, float],
                       proposed: Dict[str, float]) -> Dict[str, float]:
        """预测各目标的相对改善量。"""
        model = self.surrogate(profile)
        if not model.trained:
            return {}
        before, _ = model.predict(model.space.to_unit(current))
        after, _ = model.predict(model.space.to_unit(proposed))
        gain: Dict[str, float] = {}
        for obj in self.objectives:
            b = before.get(obj.objective_id)
            a = after.get(obj.objective_id)
            if b is None or a is None or abs(b) < 1e-9:
                continue
            gain[obj.objective_id] = safe_div(a - b, abs(b))
        return gain

    @staticmethod
    def _risk_of(changes: Sequence[ParameterChange], health: HealthReport) -> Severity:
        """评估建议本身的风险等级。"""
        max_ratio = max((abs(c.delta_ratio) for c in changes), default=0.0)
        if health.grade is HealthGrade.DEGRADED or max_ratio > 0.15:
            return Severity.WARNING
        if max_ratio > 0.08:
            return Severity.INFO
        return Severity.INFO

    def _explain(self, source: str, changes: Sequence[ParameterChange],
                 advice: OptimizationAdvice, health: HealthReport,
                 condition: ConditionSnapshot,
                 inference: InferenceResult) -> str:
        """生成给操作工看的自然语言说明。"""
        items = "；".join(
            f"{c.name} {c.current_value:.2f}{c.unit} → {c.proposed_value:.2f}{c.unit}"
            f"（{c.delta:+.2f}）" for c in changes
        )
        gains = "、".join(
            f"{self._objective_name(k)}预计{'下降' if v < 0 else '提升'}{abs(v) * 100:.1f}%"
            for k, v in advice.expected_gain.items() if abs(v) > 1e-4
        )
        parts = [
            f"当前处于{condition.label}（{condition.stability.label}，"
            f"已持续{condition.dwell_seconds / 60:.0f}分钟），"
            f"健康度{health.score:.2f}（{health.grade.label}）。",
            f"依据{source}建议调整：{items}。",
        ]
        if gains:
            parts.append(f"预计效果：{gains}。")
        note = inference.conclusions.get("note")
        if note:
            parts.append(f"规则提示：{note}。")
        if health.top_pattern.code != "normal":
            parts.append(f"注意：当前疑似{health.top_pattern.label}，调整后需重点观察。")
        return "".join(parts)

    def _objective_name(self, objective_id: str) -> str:
        for obj in self.objectives:
            if obj.objective_id == objective_id:
                return obj.name
        return objective_id

    def _publish(self, advice: OptimizationAdvice) -> None:
        if self.bus:
            self.bus.publish(EventTopic.ADVICE_GENERATED, advice,
                             device_id=advice.device_id, trace_id=advice.trace_id)

    def stats(self) -> Dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "objectives": [o.to_dict() for o in self.objectives],
            "surrogates": {k: m.stats() for k, m in self._surrogates.items()},
            "min_confidence": self.min_confidence,
        }


__all__ = ["OptimizationAdvisor", "default_objectives", "parse_adjustment"]

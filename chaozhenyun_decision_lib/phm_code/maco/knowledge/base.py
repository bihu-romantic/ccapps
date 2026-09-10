"""
知识库与推理模块
================

对应课题名称中的 **"知识驱动"**。系统的决策不是纯黑箱模型输出，而是
"机理知识 + 规则推理 + 历史案例" 三者与数据模型共同作用的结果，
这样给出的建议才有可解释的依据，操作工才敢采纳。

本模块提供四件东西：

* :class:`Rule` / :class:`RuleEngine` —— 前向链规则推理，带置信度与推理链；
* :class:`ConstraintSet` —— 工艺与安全约束，划定参数可行域；
* :class:`CaseBase` —— 案例库（CBR），"以前遇到类似工况是怎么调的"；
* :class:`KnowledgeBase` —— 三者的统一门面，供优化层与控制层调用。

规则条件用受限表达式描述（只允许比较与逻辑运算，不执行任意代码），
既保证现场工程师可配置，又不引入代码注入风险。
"""

from __future__ import annotations

import json
import math
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..core.enums import EvidenceKind, FaultPattern, Severity
from ..core.errors import KnowledgeError, RuleSyntaxError
from ..core.logging_setup import get_logger
from ..core.types import Evidence, ExperienceCase, new_id, now_ts
from ..core.utils import clamp, weighted_similarity

logger = get_logger("knowledge.base")

#: 允许出现在规则条件中的记号（白名单，杜绝任意代码执行）
_TOKEN_RE = re.compile(
    r"""(?x)
    (?P<num>-?\d+(?:\.\d+)?)          |
    (?P<str>'[^']*'|"[^"]*")          |
    (?P<op>>=|<=|==|!=|>|<)           |
    (?P<logic>\band\b|\bor\b|\bnot\b) |
    (?P<paren>[()])                   |
    (?P<name>[A-Za-z_][\w.]*)         |
    (?P<space>\s+)
    """
)


# ==========================================================================
# 规则
# ==========================================================================
@dataclass
class Rule:
    """一条 IF-THEN 规则。

    :param condition: 条件表达式，如 ``"temp_dev > 3 and load_ratio > 0.9"``
    :param actions: 结论字典，如 ``{"suggest.fan_speed": "+5%", "pattern": "overheat"}``
    :param confidence: 规则可信度，会被反馈学习动态调整
    """

    rule_id: str
    name: str
    condition: str
    actions: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.8
    priority: int = 50
    severity: Severity = Severity.INFO
    description: str = ""
    enabled: bool = True
    #: 命中与被采纳的次数，供可信度自适应
    fire_count: int = 0
    accept_count: int = 0

    @property
    def credit(self) -> float:
        """采纳率。样本不足时回落到初始可信度。"""
        if self.fire_count < 3:
            return self.confidence
        return clamp(self.accept_count / self.fire_count, 0.05, 0.99)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id, "name": self.name, "condition": self.condition,
            "actions": dict(self.actions), "confidence": round(self.confidence, 4),
            "credit": round(self.credit, 4), "priority": self.priority,
            "severity": self.severity.code, "enabled": self.enabled,
            "fire_count": self.fire_count, "accept_count": self.accept_count,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Rule":
        return cls(
            rule_id=str(raw.get("rule_id") or new_id("rule")),
            name=str(raw.get("name", "")),
            condition=str(raw.get("condition", "")),
            actions=dict(raw.get("actions") or {}),
            confidence=float(raw.get("confidence", 0.8)),
            priority=int(raw.get("priority", 50)),
            severity=Severity.from_text(raw.get("severity"), Severity.INFO),
            description=str(raw.get("description", "")),
            enabled=bool(raw.get("enabled", True)),
        )


def evaluate_condition(expression: str, facts: Mapping[str, Any]) -> bool:
    """在受限语法下求值规则条件。

    只允许数字、字符串、比较符、and/or/not、括号与事实名，
    其它记号一律拒绝——因此不可能通过规则配置执行任意代码。
    """
    if not expression.strip():
        return False
    pieces: List[str] = []
    pos = 0
    while pos < len(expression):
        m = _TOKEN_RE.match(expression, pos)
        if not m or m.end() == pos:
            raise RuleSyntaxError(
                f"规则条件含非法记号，位置 {pos}: {expression[pos:pos + 12]!r}",
                {"expression": expression},
            )
        pos = m.end()
        kind = m.lastgroup
        text = m.group()
        if kind == "space":
            pieces.append(" ")
        elif kind == "name":
            value = facts.get(text)
            if value is None:
                # 事实缺失时该条件判为不成立，而不是报错中断整轮推理
                return False
            if isinstance(value, bool):
                pieces.append("True" if value else "False")
            elif isinstance(value, (int, float)):
                if not math.isfinite(float(value)):
                    return False
                pieces.append(repr(float(value)))
            else:
                pieces.append(repr(str(value)))
        else:
            pieces.append(text)
    safe_expr = "".join(pieces)
    try:
        # 已确认表达式只含白名单记号，且禁用全部内建
        return bool(eval(safe_expr, {"__builtins__": {}}, {}))  # noqa: S307
    except Exception as exc:  # noqa: BLE001
        raise RuleSyntaxError(f"规则条件求值失败: {exc}",
                              {"expression": expression}) from exc


@dataclass
class InferenceResult:
    """一次推理的结果。"""

    conclusions: Dict[str, Any] = field(default_factory=dict)
    fired_rules: List[Rule] = field(default_factory=list)
    evidences: List[Evidence] = field(default_factory=list)
    rounds: int = 0

    @property
    def confidence(self) -> float:
        if not self.fired_rules:
            return 0.0
        return sum(r.credit for r in self.fired_rules) / len(self.fired_rules)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "conclusions": dict(self.conclusions),
            "fired_rules": [r.rule_id for r in self.fired_rules],
            "confidence": round(self.confidence, 4),
            "rounds": self.rounds,
            "evidences": [e.to_dict() for e in self.evidences],
        }


class RuleEngine:
    """前向链推理机。"""

    def __init__(self, rules: Optional[Sequence[Rule]] = None,
                 max_rounds: int = 8):
        self._rules: Dict[str, Rule] = {r.rule_id: r for r in (rules or [])}
        self.max_rounds = max_rounds
        self._lock = threading.RLock()

    def add(self, rule: Rule) -> None:
        with self._lock:
            self._rules[rule.rule_id] = rule

    def remove(self, rule_id: str) -> bool:
        with self._lock:
            return self._rules.pop(rule_id, None) is not None

    def get(self, rule_id: str) -> Optional[Rule]:
        with self._lock:
            return self._rules.get(rule_id)

    def rules(self) -> List[Rule]:
        with self._lock:
            return sorted(self._rules.values(), key=lambda r: -r.priority)

    def infer(self, facts: Mapping[str, Any]) -> InferenceResult:
        """前向链推理：反复扫描规则集直到不再产生新结论。"""
        working: Dict[str, Any] = dict(facts)
        result = InferenceResult()
        fired_ids: set[str] = set()

        for round_no in range(1, self.max_rounds + 1):
            new_fired = False
            for rule in self.rules():
                if not rule.enabled or rule.rule_id in fired_ids:
                    continue
                try:
                    hit = evaluate_condition(rule.condition, working)
                except RuleSyntaxError as exc:
                    logger.warning("规则 %s 条件非法，已跳过: %s", rule.rule_id, exc)
                    rule.enabled = False
                    continue
                if not hit:
                    continue
                fired_ids.add(rule.rule_id)
                rule.fire_count += 1
                new_fired = True
                for key, value in rule.actions.items():
                    working[key] = value
                    result.conclusions[key] = value
                result.fired_rules.append(rule)
                result.evidences.append(Evidence(
                    kind=EvidenceKind.RULE,
                    source_id=rule.rule_id,
                    statement=f"规则「{rule.name}」命中：{rule.condition}",
                    confidence=rule.credit,
                    detail={"actions": dict(rule.actions),
                            "description": rule.description},
                ))
            result.rounds = round_no
            if not new_fired:
                break
        return result

    def reward(self, rule_ids: Sequence[str], accepted: bool, step: float = 0.05) -> None:
        """按执行效果调整规则可信度（反馈学习回写知识库）。"""
        with self._lock:
            for rid in rule_ids:
                rule = self._rules.get(rid)
                if rule is None:
                    continue
                if accepted:
                    rule.accept_count += 1
                    rule.confidence = clamp(rule.confidence + step, 0.05, 0.99)
                else:
                    rule.confidence = clamp(rule.confidence - step, 0.05, 0.99)

    # ------------------------------------------------------------------
    def load(self, path: str) -> int:
        """从 JSON 文件加载规则。"""
        file = Path(path)
        if not file.exists():
            logger.info("规则文件不存在，使用内置默认规则: %s", path)
            return 0
        try:
            raw = json.loads(file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise KnowledgeError(f"规则文件不是合法 JSON: {exc}", {"path": path}) from exc
        items = raw.get("rules") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            raise KnowledgeError("规则文件应为规则数组或含 rules 字段的对象", {"path": path})
        count = 0
        for item in items:
            try:
                self.add(Rule.from_dict(item))
                count += 1
            except (TypeError, ValueError) as exc:
                logger.warning("跳过非法规则条目: %s", exc)
        logger.info("已加载 %d 条规则: %s", count, path)
        return count

    def save(self, path: str) -> None:
        file = Path(path)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(
            json.dumps({"rules": [r.to_dict() for r in self.rules()]},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


# ==========================================================================
# 约束
# ==========================================================================
@dataclass
class Constraint:
    """一条工艺/安全约束。

    :param kind: ``hard`` 硬约束（不可违反）/ ``soft`` 软约束（可罚分）
    :param expression: 必须为真的表达式，如 ``"outlet_pressure < 0.85"``
    """

    constraint_id: str
    name: str
    expression: str
    kind: str = "hard"
    penalty: float = 1.0
    message: str = ""

    def check(self, facts: Mapping[str, Any]) -> bool:
        try:
            return evaluate_condition(self.expression, facts)
        except RuleSyntaxError as exc:
            logger.warning("约束 %s 表达式非法: %s", self.constraint_id, exc)
            return True  # 无法判定时不阻断，交由安全门兜底

    def to_dict(self) -> Dict[str, Any]:
        return {"constraint_id": self.constraint_id, "name": self.name,
                "expression": self.expression, "kind": self.kind,
                "penalty": self.penalty, "message": self.message}


class ConstraintSet:
    """约束集合。"""

    def __init__(self, constraints: Optional[Sequence[Constraint]] = None):
        self._items: List[Constraint] = list(constraints or [])

    def add(self, constraint: Constraint) -> None:
        self._items.append(constraint)

    def all(self) -> List[Constraint]:
        return list(self._items)

    def violations(self, facts: Mapping[str, Any]) -> List[Constraint]:
        """返回被违反的约束。"""
        return [c for c in self._items if not c.check(facts)]

    def feasible(self, facts: Mapping[str, Any]) -> bool:
        """是否满足全部硬约束。"""
        return not any(c.kind == "hard" for c in self.violations(facts))

    def penalty(self, facts: Mapping[str, Any]) -> float:
        """软约束罚分之和，供优化目标函数使用。"""
        return sum(c.penalty for c in self.violations(facts) if c.kind == "soft")

    def to_dict(self) -> Dict[str, Any]:
        return {"count": len(self._items), "items": [c.to_dict() for c in self._items]}


# ==========================================================================
# 案例库
# ==========================================================================
class CaseBase:
    """基于案例的推理（CBR）：检索 → 复用 → 修正 → 保留。"""

    def __init__(self, capacity: int = 2000, min_similarity: float = 0.65,
                 top_k: int = 5, initial_credibility: float = 0.5):
        self._cases: Dict[str, ExperienceCase] = {}
        self.capacity = capacity
        self.min_similarity = min_similarity
        self.top_k = top_k
        self.initial_credibility = initial_credibility
        self._lock = threading.RLock()

    def add(self, case: ExperienceCase) -> None:
        with self._lock:
            self._cases[case.case_id] = case
            if len(self._cases) > self.capacity:
                # 淘汰可信度最低且最久未复用的案例
                victims = sorted(self._cases.values(),
                                 key=lambda c: (c.credibility, c.updated_at))
                for c in victims[: len(self._cases) - self.capacity]:
                    self._cases.pop(c.case_id, None)

    def retrieve(self, signature: Dict[str, float], device_type: Optional[str] = None,
                 condition_id: str = "") -> List[Tuple[ExperienceCase, float]]:
        """按工况签名检索相似案例，返回 ``(案例, 相似度)`` 列表。"""
        scored: List[Tuple[ExperienceCase, float]] = []
        with self._lock:
            candidates = list(self._cases.values())
        for case in candidates:
            if device_type and case.device_type.code != device_type:
                continue
            sim = weighted_similarity(signature, case.signature)
            # 同工况加成，鼓励优先复用本工况经验
            if condition_id and case.condition_id == condition_id:
                sim = clamp(sim * 1.15, 0.0, 1.0)
            # 只复用被证明有效的案例
            if case.accepted and case.reward > 0:
                sim = clamp(sim * (0.85 + 0.3 * case.credibility), 0.0, 1.0)
            else:
                sim *= 0.6
            if sim >= self.min_similarity:
                scored.append((case, sim))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[: self.top_k]

    def reuse(self, signature: Dict[str, float], condition_id: str = "",
              device_type: Optional[str] = None) -> Optional[Dict[str, float]]:
        """复用：把 top-k 案例的参数按相似度加权融合成一组建议参数。"""
        matches = self.retrieve(signature, device_type, condition_id)
        if not matches:
            return None
        accumulator: Dict[str, float] = {}
        weight_sum: Dict[str, float] = {}
        for case, sim in matches:
            w = sim * max(case.credibility, 0.05)
            for key, value in case.params_after.items():
                accumulator[key] = accumulator.get(key, 0.0) + value * w
                weight_sum[key] = weight_sum.get(key, 0.0) + w
        merged = {k: accumulator[k] / weight_sum[k] for k in accumulator if weight_sum[k] > 0}
        with self._lock:
            for case, _ in matches:
                case.reuse_count += 1
                case.updated_at = now_ts()
        return merged or None

    def revise(self, case_id: str, reward: float, accepted: bool) -> None:
        """修正：按实际效果更新案例可信度。"""
        with self._lock:
            case = self._cases.get(case_id)
            if case is None:
                return
            case.reward = reward
            case.accepted = accepted
            delta = 0.1 if accepted else -0.15
            case.credibility = clamp(case.credibility + delta, 0.02, 0.99)
            case.updated_at = now_ts()

    def all(self) -> List[ExperienceCase]:
        with self._lock:
            return list(self._cases.values())

    def load(self, path: str) -> int:
        file = Path(path)
        if not file.exists():
            return 0
        try:
            raw = json.loads(file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise KnowledgeError(f"案例文件不是合法 JSON: {exc}", {"path": path}) from exc
        items = raw.get("cases") if isinstance(raw, dict) else raw
        count = 0
        for item in items or []:
            try:
                self.add(ExperienceCase.from_dict(item))
                count += 1
            except (TypeError, ValueError, KeyError):
                continue
        logger.info("已加载 %d 条经验案例", count)
        return count

    def save(self, path: str) -> None:
        file = Path(path)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(
            json.dumps({"cases": [c.to_dict() for c in self.all()]},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def stats(self) -> Dict[str, Any]:
        cases = self.all()
        accepted = [c for c in cases if c.accepted]
        return {
            "total": len(cases),
            "accepted": len(accepted),
            "avg_credibility": round(
                sum(c.credibility for c in cases) / len(cases), 4) if cases else 0.0,
            "avg_reward": round(
                sum(c.reward for c in accepted) / len(accepted), 4) if accepted else 0.0,
            "total_reuse": sum(c.reuse_count for c in cases),
        }


# ==========================================================================
# 知识库门面
# ==========================================================================
class KnowledgeBase:
    """知识库统一门面。"""

    def __init__(self, rule_engine: Optional[RuleEngine] = None,
                 case_base: Optional[CaseBase] = None,
                 constraints: Optional[ConstraintSet] = None):
        self.rules = rule_engine or RuleEngine()
        self.cases = case_base or CaseBase()
        self.constraints = constraints or ConstraintSet()

    def reason(self, facts: Mapping[str, Any]) -> InferenceResult:
        """规则推理。"""
        return self.rules.infer(facts)

    def recall(self, signature: Dict[str, float], condition_id: str = "",
               device_type: Optional[str] = None) -> Tuple[Optional[Dict[str, float]],
                                                           List[Evidence]]:
        """案例复用，并生成对应证据。"""
        matches = self.cases.retrieve(signature, device_type, condition_id)
        if not matches:
            return None, []
        params = self.cases.reuse(signature, condition_id, device_type)
        evidences = [
            Evidence(
                kind=EvidenceKind.CASE,
                source_id=case.case_id,
                statement=(f"历史案例相似度 {sim:.2f}：同类工况下调整后"
                           f"奖励 {case.reward:+.3f}"),
                confidence=sim * case.credibility,
                detail={"params_after": case.params_after,
                        "condition_id": case.condition_id,
                        "reuse_count": case.reuse_count},
            )
            for case, sim in matches
        ]
        return params, evidences

    def check_constraints(self, facts: Mapping[str, Any]) -> Tuple[bool, List[Evidence]]:
        """约束检查，返回是否可行与证据。"""
        violated = self.constraints.violations(facts)
        evidences = [
            Evidence(
                kind=EvidenceKind.CONSTRAINT,
                source_id=c.constraint_id,
                statement=f"约束「{c.name}」不满足：{c.expression}",
                confidence=1.0,
                detail={"kind": c.kind, "message": c.message},
            )
            for c in violated
        ]
        feasible = not any(c.kind == "hard" for c in violated)
        return feasible, evidences

    def learn(self, case: ExperienceCase, rule_ids: Sequence[str],
              accepted: bool, step: float = 0.05) -> None:
        """保留：把一次闭环结果写回知识库。"""
        self.cases.add(case)
        self.rules.reward(rule_ids, accepted, step)

    def load_all(self, rule_file: str = "", case_file: str = "") -> Dict[str, int]:
        out = {"rules": 0, "cases": 0}
        if rule_file:
            out["rules"] = self.rules.load(rule_file)
        if case_file:
            out["cases"] = self.cases.load(case_file)
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rules": [r.to_dict() for r in self.rules.rules()],
            "constraints": self.constraints.to_dict(),
            "cases": self.cases.stats(),
        }


__all__ = [
    "Rule", "RuleEngine", "InferenceResult", "evaluate_condition",
    "Constraint", "ConstraintSet", "CaseBase", "KnowledgeBase",
]

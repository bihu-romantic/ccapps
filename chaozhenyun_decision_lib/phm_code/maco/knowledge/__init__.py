"""
知识层（knowledge）
===================

课题名称中 "知识驱动" 的落点：规则推理 + 约束边界 + 案例复用，
与数据模型共同构成决策依据，并为每条建议产出可解释的证据链。
"""

from .base import (
    CaseBase,
    Constraint,
    ConstraintSet,
    InferenceResult,
    KnowledgeBase,
    Rule,
    RuleEngine,
    evaluate_condition,
)
from .presets import build_default_knowledge_base, default_constraints, default_rules

__all__ = [
    "Rule", "RuleEngine", "InferenceResult", "evaluate_condition",
    "Constraint", "ConstraintSet", "CaseBase", "KnowledgeBase",
    "default_rules", "default_constraints", "build_default_knowledge_base",
]

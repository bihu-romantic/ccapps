"""
反馈层（feedback）
==================

对应系统功能五：**执行效果的反馈学习**。下发后延时取窗口做统计检验，
折算奖励，再把经验回写到代理模型、规则可信度、案例库与工况档案，
形成 "越用越准" 的闭环。
"""

from .learner import (
    EffectEvaluator,
    FeedbackLearner,
    ObservedMetric,
    OnlineLearner,
    RewardCalculator,
    default_metrics,
)

__all__ = [
    "EffectEvaluator", "RewardCalculator", "OnlineLearner", "FeedbackLearner",
    "ObservedMetric", "default_metrics",
]

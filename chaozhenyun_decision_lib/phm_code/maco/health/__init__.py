"""
健康层（health）
================

对应系统功能二：**状态识别与健康评估**。按工况归一化后评估设备健康，
输出分项指标、总健康分、疑似故障模式、退化趋势与剩余可用时间。
"""

from .assessor import (
    Baseline,
    BaselineLibrary,
    HealthAssessor,
    PatternRule,
    default_pattern_rules,
)

__all__ = ["HealthAssessor", "BaselineLibrary", "Baseline", "PatternRule",
           "default_pattern_rules"]

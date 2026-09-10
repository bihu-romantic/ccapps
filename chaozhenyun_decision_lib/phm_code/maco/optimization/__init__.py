"""
优化层（optimization）
======================

对应系统功能三：**运行参数优化建议**。
:mod:`~maco.optimization.optimizer` 提供搜索空间、代理模型与三种寻优算法；
:mod:`~maco.optimization.advisor` 把知识推理、案例复用与模型寻优
合成一条带证据链的可执行建议。
"""

from .advisor import OptimizationAdvisor, default_objectives, parse_adjustment
from .optimizer import (
    BayesianOptimizer,
    NSGA2Optimizer,
    OptimizeResult,
    ParticleSwarmOptimizer,
    SearchSpace,
    SurrogateModel,
    build_optimizer,
)

__all__ = [
    "OptimizationAdvisor", "default_objectives", "parse_adjustment",
    "SearchSpace", "SurrogateModel", "OptimizeResult", "build_optimizer",
    "BayesianOptimizer", "ParticleSwarmOptimizer", "NSGA2Optimizer",
]

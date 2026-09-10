"""
参数寻优模块
============

在给定工况下，为可调运行参数搜索一组更优取值。系统采用
"代理模型 + 启发式搜索" 的组合：

* **代理模型**（:class:`SurrogateModel`）—— 用历史运行样本拟合
  "参数 → 目标" 的映射。现场不可能为了优化而反复试错，因此必须靠
  历史数据建模，在模型上搜索，只把最终少数几个候选拿到现场验证。
* **搜索算法** —— 三选一：
  - :class:`BayesianOptimizer` 采样成本高时用，靠置信上界平衡探索与利用；
  - :class:`ParticleSwarmOptimizer` 连续空间收敛快；
  - :class:`NSGA2Optimizer` 多目标冲突时给出帕累托前沿。

所有算法都在归一化的 [0,1] 空间中搜索，由 :class:`SearchSpace` 负责与
工程量互转，因此增删参数不影响算法实现。
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..core.enums import ObjectiveSense
from ..core.errors import InfeasibleProblem, OptimizationError
from ..core.logging_setup import get_logger
from ..core.registry import OPTIMIZER_REGISTRY
from ..core.types import ObjectiveSpec, ParameterSpec
from ..core.utils import clamp, safe_div

logger = get_logger("optimization.optimizer")

#: 目标函数签名：参数字典 → 各目标值字典
ObjectiveFunc = Callable[[Dict[str, float]], Dict[str, float]]


# ==========================================================================
# 搜索空间
# ==========================================================================
class SearchSpace:
    """决策变量空间，负责工程量与归一化坐标互转。"""

    def __init__(self, params: Sequence[ParameterSpec]):
        if not params:
            raise OptimizationError("搜索空间不能为空", {})
        self.params: List[ParameterSpec] = list(params)
        self.names: List[str] = [p.param_id for p in self.params]

    @property
    def dim(self) -> int:
        return len(self.params)

    def to_unit(self, values: Dict[str, float]) -> np.ndarray:
        return np.asarray([p.normalize(values.get(p.param_id, p.lower))
                           for p in self.params], dtype=float)

    def to_values(self, unit: Sequence[float]) -> Dict[str, float]:
        return {p.param_id: p.denormalize(float(u))
                for p, u in zip(self.params, unit)}

    def clip_unit(self, unit: Sequence[float]) -> np.ndarray:
        return np.clip(np.asarray(unit, dtype=float), 0.0, 1.0)

    def restrict(self, center: Dict[str, float], max_delta_ratio: float
                 ) -> List[Tuple[float, float]]:
        """以当前工作点为中心划出允许搜索的邻域（归一化区间）。

        这是工业场景与实验室优化的关键差别：不允许一次跳到远端最优点，
        必须小步走、边走边验证。
        """
        bounds: List[Tuple[float, float]] = []
        for p in self.params:
            now = center.get(p.param_id, (p.lower + p.upper) / 2)
            span = abs(now) * max_delta_ratio if abs(now) > 1e-9 else p.span() * max_delta_ratio
            span = min(span, p.max_delta)
            lo = p.normalize(p.clip(now - span))
            hi = p.normalize(p.clip(now + span))
            bounds.append((min(lo, hi), max(lo, hi)))
        return bounds

    def sample_grid(self, bounds: Sequence[Tuple[float, float]],
                    per_dim: int = 5) -> List[np.ndarray]:
        """在邻域内做拉丁超立方式的分层采样（确定性，便于复现）。"""
        per_dim = max(2, per_dim)
        columns: List[np.ndarray] = []
        for i, (lo, hi) in enumerate(bounds):
            base = np.linspace(lo, hi, per_dim)
            # 每维用不同的固定偏移打散，避免所有点落在对角线上
            offset = (i * 7 % per_dim)
            columns.append(np.roll(base, offset))
        return [np.array([col[k] for col in columns]) for k in range(per_dim)]


# ==========================================================================
# 代理模型
# ==========================================================================
class SurrogateModel:
    """加权最近邻 + 局部线性的轻量代理模型。

    选它而不是神经网络，是因为现场样本通常只有几十到几百条，
    复杂模型必然过拟合；而本模型样本少时退化为最近邻，
    样本多时自动接近局部线性回归，且训练成本可忽略。
    """

    def __init__(self, space: SearchSpace, objectives: Sequence[ObjectiveSpec],
                 bandwidth: float = 0.25, min_samples: int = 6):
        self.space = space
        self.objectives = list(objectives)
        self.bandwidth = bandwidth
        self.min_samples = min_samples
        self._x: List[np.ndarray] = []
        self._y: List[Dict[str, float]] = []
        self._lock = threading.RLock()

    def add_sample(self, params: Dict[str, float], results: Dict[str, float]) -> None:
        """加入一条历史运行样本。"""
        with self._lock:
            self._x.append(self.space.to_unit(params))
            self._y.append(dict(results))

    def bulk_add(self, samples: Sequence[Tuple[Dict[str, float], Dict[str, float]]]) -> None:
        for params, results in samples:
            self.add_sample(params, results)

    @property
    def sample_count(self) -> int:
        with self._lock:
            return len(self._x)

    @property
    def trained(self) -> bool:
        return self.sample_count >= self.min_samples

    def predict(self, unit: Sequence[float]) -> Tuple[Dict[str, float], float]:
        """预测各目标值与预测置信度。

        置信度由 "邻域内有效样本权重之和" 决定：外推到无样本区域时
        置信度趋近 0，上层据此拒绝采纳该建议——这是防止模型胡说的关键。
        """
        with self._lock:
            if not self._x:
                return {}, 0.0
            xs = np.asarray(self._x)
            ys = list(self._y)
        query = np.asarray(unit, dtype=float)
        dist = np.linalg.norm(xs - query, axis=1)
        weights = np.exp(-(dist / max(self.bandwidth, 1e-6)) ** 2)
        total = float(np.sum(weights))
        if total < 1e-9:
            return {}, 0.0
        out: Dict[str, float] = {}
        for obj in self.objectives:
            vals = np.asarray([y.get(obj.objective_id, math.nan) for y in ys])
            mask = np.isfinite(vals)
            if not np.any(mask):
                continue
            w = weights[mask]
            out[obj.objective_id] = float(np.sum(vals[mask] * w) / max(np.sum(w), 1e-9))
        # 有效样本数 = 权重和，折算成 0~1 置信度
        confidence = clamp(total / max(self.min_samples, 1), 0.0, 1.0)
        return out, confidence

    def uncertainty(self, unit: Sequence[float]) -> float:
        """预测不确定度：到最近样本的归一化距离。"""
        with self._lock:
            if not self._x:
                return 1.0
            xs = np.asarray(self._x)
        d = float(np.min(np.linalg.norm(xs - np.asarray(unit, dtype=float), axis=1)))
        return clamp(d / math.sqrt(max(self.space.dim, 1)), 0.0, 1.0)

    def scalarize(self, results: Dict[str, float], weights: Dict[str, float]) -> float:
        """把多目标折算为单一标量（越大越好）。"""
        total = 0.0
        wsum = 0.0
        for obj in self.objectives:
            if obj.objective_id not in results:
                continue
            w = float(weights.get(obj.objective_id, obj.weight))
            v = results[obj.objective_id]
            if not math.isfinite(v):
                continue
            # 统一为 "越大越好"，并用目标值做尺度归一
            scale = abs(obj.target) if obj.target else max(abs(v), 1e-6)
            norm = v / scale
            total += w * (-norm if obj.sense is ObjectiveSense.MINIMIZE else norm)
            wsum += abs(w)
        return safe_div(total, wsum)

    def stats(self) -> Dict[str, Any]:
        return {"samples": self.sample_count, "trained": self.trained,
                "bandwidth": self.bandwidth, "dim": self.space.dim}


# ==========================================================================
# 优化算法
# ==========================================================================
@dataclass
class OptimizeResult:
    """寻优结果。"""

    best_params: Dict[str, float] = field(default_factory=dict)
    best_score: float = -math.inf
    predicted: Dict[str, float] = field(default_factory=dict)
    confidence: float = 0.0
    iterations: int = 0
    algorithm: str = ""
    #: 多目标时的帕累托前沿
    pareto: List[Dict[str, Any]] = field(default_factory=list)
    feasible: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "best_params": {k: round(v, 6) for k, v in self.best_params.items()},
            "best_score": round(self.best_score, 6),
            "predicted": {k: round(v, 6) for k, v in self.predicted.items()},
            "confidence": round(self.confidence, 4),
            "iterations": self.iterations,
            "algorithm": self.algorithm,
            "feasible": self.feasible,
            "pareto_size": len(self.pareto),
        }


class BaseOptimizer:
    """优化器基类。"""

    name = "base"

    def __init__(self, space: SearchSpace, max_iterations: int = 60,
                 seed: int = 20240101):
        self.space = space
        self.max_iterations = max_iterations
        # 固定种子：同样输入必得同样建议，工业场景要求可复现可追溯
        self.rng = np.random.default_rng(seed)

    def optimize(self, evaluate: Callable[[np.ndarray], float],
                 bounds: Sequence[Tuple[float, float]],
                 start: Optional[np.ndarray] = None) -> OptimizeResult:
        raise NotImplementedError

    def _random_in(self, bounds: Sequence[Tuple[float, float]]) -> np.ndarray:
        return np.asarray([self.rng.uniform(lo, hi) for lo, hi in bounds])


@OPTIMIZER_REGISTRY.decorator("bayesian")
class BayesianOptimizer(BaseOptimizer):
    """置信上界式贝叶斯优化（用代理模型的不确定度代替高斯过程方差）。"""

    name = "bayesian"

    def __init__(self, space: SearchSpace, max_iterations: int = 40,
                 seed: int = 20240101, explore_weight: float = 0.6,
                 candidates_per_round: int = 24,
                 uncertainty: Optional[Callable[[np.ndarray], float]] = None):
        super().__init__(space, max_iterations, seed)
        self.explore_weight = explore_weight
        self.candidates_per_round = candidates_per_round
        self.uncertainty = uncertainty or (lambda _u: 0.0)

    def optimize(self, evaluate: Callable[[np.ndarray], float],
                 bounds: Sequence[Tuple[float, float]],
                 start: Optional[np.ndarray] = None) -> OptimizeResult:
        best_x = self.space.clip_unit(start) if start is not None else self._random_in(bounds)
        best_score = evaluate(best_x)
        evaluated = 1
        for _ in range(self.max_iterations):
            candidates = [self._random_in(bounds) for _ in range(self.candidates_per_round)]
            # 采集函数 = 预测收益 + 探索项 × 不确定度
            scored = [(c, evaluate(c) + self.explore_weight * self.uncertainty(c))
                      for c in candidates]
            evaluated += len(candidates)
            cand, acq = max(scored, key=lambda t: t[1])
            true_score = evaluate(cand)
            if true_score > best_score:
                best_score, best_x = true_score, cand
        return OptimizeResult(
            best_params=self.space.to_values(best_x),
            best_score=best_score, iterations=evaluated, algorithm=self.name,
        )


@OPTIMIZER_REGISTRY.decorator("pso")
class ParticleSwarmOptimizer(BaseOptimizer):
    """粒子群优化。连续参数空间收敛快，适合日常小步调优。"""

    name = "pso"

    def __init__(self, space: SearchSpace, max_iterations: int = 40,
                 seed: int = 20240101, swarm_size: int = 20,
                 inertia: float = 0.72, c1: float = 1.4, c2: float = 1.4):
        super().__init__(space, max_iterations, seed)
        self.swarm_size = swarm_size
        self.inertia = inertia
        self.c1 = c1
        self.c2 = c2

    def optimize(self, evaluate: Callable[[np.ndarray], float],
                 bounds: Sequence[Tuple[float, float]],
                 start: Optional[np.ndarray] = None) -> OptimizeResult:
        lo = np.asarray([b[0] for b in bounds])
        hi = np.asarray([b[1] for b in bounds])
        span = np.maximum(hi - lo, 1e-9)

        positions = [self._random_in(bounds) for _ in range(self.swarm_size)]
        if start is not None:
            positions[0] = np.clip(np.asarray(start, dtype=float), lo, hi)
        velocities = [self.rng.uniform(-0.1, 0.1, size=len(bounds)) * span
                      for _ in range(self.swarm_size)]
        p_best = [p.copy() for p in positions]
        p_scores = [evaluate(p) for p in positions]
        g_idx = int(np.argmax(p_scores))
        g_best = p_best[g_idx].copy()
        g_score = p_scores[g_idx]
        evaluated = self.swarm_size

        for _ in range(self.max_iterations):
            for i in range(self.swarm_size):
                r1 = self.rng.random(len(bounds))
                r2 = self.rng.random(len(bounds))
                velocities[i] = (self.inertia * velocities[i]
                                 + self.c1 * r1 * (p_best[i] - positions[i])
                                 + self.c2 * r2 * (g_best - positions[i]))
                positions[i] = np.clip(positions[i] + velocities[i], lo, hi)
                score = evaluate(positions[i])
                evaluated += 1
                if score > p_scores[i]:
                    p_scores[i] = score
                    p_best[i] = positions[i].copy()
                    if score > g_score:
                        g_score, g_best = score, positions[i].copy()
        return OptimizeResult(
            best_params=self.space.to_values(g_best),
            best_score=g_score, iterations=evaluated, algorithm=self.name,
        )


@OPTIMIZER_REGISTRY.decorator("nsga2")
class NSGA2Optimizer(BaseOptimizer):
    """NSGA-II 多目标优化，输出帕累托前沿。

    当 "省电" 与 "保产量" 直接冲突时，单目标加权会把取舍偷偷藏进权重里；
    帕累托前沿把取舍摊开给人看，由人选点——这更符合工业决策习惯。
    """

    name = "nsga2"

    def __init__(self, space: SearchSpace, max_iterations: int = 30,
                 seed: int = 20240101, population_size: int = 24,
                 mutation_rate: float = 0.2):
        super().__init__(space, max_iterations, seed)
        self.population_size = population_size
        self.mutation_rate = mutation_rate

    def optimize_multi(self, evaluate_multi: Callable[[np.ndarray], List[float]],
                       bounds: Sequence[Tuple[float, float]],
                       start: Optional[np.ndarray] = None) -> OptimizeResult:
        """多目标寻优。``evaluate_multi`` 返回各目标值（均为越大越好）。"""
        lo = np.asarray([b[0] for b in bounds])
        hi = np.asarray([b[1] for b in bounds])
        pop = [self._random_in(bounds) for _ in range(self.population_size)]
        if start is not None:
            pop[0] = np.clip(np.asarray(start, dtype=float), lo, hi)
        evaluated = 0

        for _ in range(self.max_iterations):
            scores = [evaluate_multi(ind) for ind in pop]
            evaluated += len(pop)
            fronts = self._fast_nondominated_sort(scores)
            new_pop: List[np.ndarray] = []
            for front in fronts:
                if len(new_pop) + len(front) <= self.population_size:
                    new_pop.extend(pop[i] for i in front)
                else:
                    # 用拥挤距离在最后一层内择优，保持前沿分布均匀
                    crowd = self._crowding_distance([scores[i] for i in front])
                    order = sorted(range(len(front)), key=lambda k: -crowd[k])
                    for k in order[: self.population_size - len(new_pop)]:
                        new_pop.append(pop[front[k]])
                    break
            # 交叉 + 变异产生下一代
            children: List[np.ndarray] = []
            while len(children) < self.population_size:
                a, b = self.rng.integers(0, len(new_pop), size=2)
                alpha = self.rng.random()
                child = alpha * new_pop[int(a)] + (1 - alpha) * new_pop[int(b)]
                if self.rng.random() < self.mutation_rate:
                    child = child + self.rng.normal(0, 0.05, size=len(bounds))
                children.append(np.clip(child, lo, hi))
            pop = children

        final_scores = [evaluate_multi(ind) for ind in pop]
        evaluated += len(pop)
        fronts = self._fast_nondominated_sort(final_scores)
        pareto = [{"params": self.space.to_values(pop[i]),
                   "objectives": final_scores[i]} for i in fronts[0]] if fronts else []
        best_idx = fronts[0][0] if fronts and fronts[0] else 0
        return OptimizeResult(
            best_params=self.space.to_values(pop[best_idx]),
            best_score=float(sum(final_scores[best_idx])),
            iterations=evaluated, algorithm=self.name, pareto=pareto,
        )

    def optimize(self, evaluate: Callable[[np.ndarray], float],
                 bounds: Sequence[Tuple[float, float]],
                 start: Optional[np.ndarray] = None) -> OptimizeResult:
        """单目标入口：把标量目标包装成单元素多目标。"""
        return self.optimize_multi(lambda u: [evaluate(u)], bounds, start)

    @staticmethod
    def _fast_nondominated_sort(scores: Sequence[Sequence[float]]) -> List[List[int]]:
        n = len(scores)
        fronts: List[List[int]] = [[]]
        dominated: List[List[int]] = [[] for _ in range(n)]
        counts = [0] * n
        for p in range(n):
            for q in range(n):
                if p == q:
                    continue
                if NSGA2Optimizer._dominates(scores[p], scores[q]):
                    dominated[p].append(q)
                elif NSGA2Optimizer._dominates(scores[q], scores[p]):
                    counts[p] += 1
            if counts[p] == 0:
                fronts[0].append(p)
        i = 0
        while i < len(fronts) and fronts[i]:
            nxt: List[int] = []
            for p in fronts[i]:
                for q in dominated[p]:
                    counts[q] -= 1
                    if counts[q] == 0:
                        nxt.append(q)
            i += 1
            if nxt:
                fronts.append(nxt)
        return [f for f in fronts if f]

    @staticmethod
    def _dominates(a: Sequence[float], b: Sequence[float]) -> bool:
        return all(x >= y for x, y in zip(a, b)) and any(x > y for x, y in zip(a, b))

    @staticmethod
    def _crowding_distance(scores: Sequence[Sequence[float]]) -> List[float]:
        n = len(scores)
        if n == 0:
            return []
        m = len(scores[0])
        distance = [0.0] * n
        for k in range(m):
            order = sorted(range(n), key=lambda i: scores[i][k])
            distance[order[0]] = math.inf
            distance[order[-1]] = math.inf
            lo, hi = scores[order[0]][k], scores[order[-1]][k]
            span = hi - lo
            if span < 1e-12:
                continue
            for idx in range(1, n - 1):
                nxt = scores[order[idx + 1]][k]
                prv = scores[order[idx - 1]][k]
                distance[order[idx]] += (nxt - prv) / span
        return distance


def build_optimizer(algorithm: str, space: SearchSpace, **kwargs: Any) -> BaseOptimizer:
    """按名称构造优化器。``auto`` 时按目标数与样本量自动选择。"""
    name = (algorithm or "auto").lower()
    if name == "auto":
        n_obj = int(kwargs.pop("objective_count", 1))
        samples = int(kwargs.pop("sample_count", 0))
        if n_obj >= 2:
            name = "nsga2"
        elif samples < 20:
            name = "bayesian"   # 样本少时更需要探索
        else:
            name = "pso"
    kwargs.pop("objective_count", None)
    kwargs.pop("sample_count", None)
    if not OPTIMIZER_REGISTRY.has(name):
        raise OptimizationError(f"未知的优化算法: {algorithm}",
                                {"available": OPTIMIZER_REGISTRY.names()})
    return OPTIMIZER_REGISTRY.create(name, space, **kwargs)


__all__ = [
    "SearchSpace", "SurrogateModel", "OptimizeResult", "BaseOptimizer",
    "BayesianOptimizer", "ParticleSwarmOptimizer", "NSGA2Optimizer",
    "build_optimizer", "InfeasibleProblem",
]

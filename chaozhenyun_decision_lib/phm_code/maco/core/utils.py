"""
通用工具模块
============

集中放置跨层复用的数学与时序小工具。这里只放 "无状态、无副作用" 的纯函数，
任何带业务语义的计算都应放到对应功能层，避免本模块膨胀成杂物堆。
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np


# ==========================================================================
# 数值工具
# ==========================================================================
def safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    """安全除法，分母趋零时返回默认值。"""
    if abs(denominator) < 1e-12:
        return default
    return numerator / denominator


def clamp(value: float, low: float, high: float) -> float:
    """裁剪到闭区间。"""
    if low > high:
        low, high = high, low
    return min(max(value, low), high)


def finite(value: Any, default: float = 0.0) -> float:
    """把 None/NaN/Inf 归一为默认值。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def normalize01(value: float, low: float, high: float) -> float:
    """线性归一到 [0,1]。"""
    if abs(high - low) < 1e-12:
        return 0.0
    return clamp((value - low) / (high - low), 0.0, 1.0)


def sigmoid(x: float, k: float = 1.0, x0: float = 0.0) -> float:
    """数值稳定的 Sigmoid。"""
    z = k * (x - x0)
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def score_from_deviation(deviation: float, tolerance: float = 1.0,
                         sharpness: float = 2.0) -> float:
    """把 "相对基线的偏离量" 映射为 0~1 健康分。

    偏离为 0 时得 1 分；偏离达到 ``tolerance`` 时约 0.5 分；
    ``sharpness`` 越大，超过容差后分数下降越陡。
    """
    d = abs(finite(deviation))
    t = max(tolerance, 1e-9)
    return clamp(1.0 / (1.0 + (d / t) ** sharpness), 0.0, 1.0)


def weighted_mean(values: Sequence[float], weights: Sequence[float]) -> float:
    """加权平均，权重和为 0 时退化为算术平均。"""
    if not values:
        return 0.0
    if len(values) != len(weights):
        raise ValueError("数值与权重长度不一致")
    total_w = sum(weights)
    if abs(total_w) < 1e-12:
        return float(np.mean(values))
    return float(sum(v * w for v, w in zip(values, weights)) / total_w)


# ==========================================================================
# 统计工具
# ==========================================================================
def robust_stats(values: Sequence[float]) -> Dict[str, float]:
    """稳健统计量：中位数、MAD、四分位距，抗离群点。"""
    arr = np.asarray([v for v in values if math.isfinite(v)], dtype=float)
    if arr.size == 0:
        return {"n": 0, "mean": 0.0, "std": 0.0, "median": 0.0,
                "mad": 0.0, "q1": 0.0, "q3": 0.0, "iqr": 0.0,
                "min": 0.0, "max": 0.0, "cv": 0.0}
    median = float(np.median(arr))
    mad = float(np.median(np.abs(arr - median)))
    q1, q3 = (float(x) for x in np.percentile(arr, [25, 75]))
    mean = float(np.mean(arr))
    std = float(np.std(arr, ddof=1)) if arr.size > 1 else 0.0
    return {
        "n": int(arr.size),
        "mean": mean,
        "std": std,
        "median": median,
        # 1.4826 为正态分布下 MAD 到标准差的换算系数
        "mad": mad * 1.4826,
        "q1": q1,
        "q3": q3,
        "iqr": q3 - q1,
        "min": float(np.min(arr)),
        "max": float(np.max(arr)),
        "cv": abs(safe_div(std, mean)),
    }


def welch_t_test(a: Sequence[float], b: Sequence[float]) -> Tuple[float, float]:
    """Welch t 检验（不假设方差齐性）。

    :return: ``(t 统计量, 自由度)``；样本不足时返回 ``(0.0, 0.0)``
    """
    x = np.asarray([v for v in a if math.isfinite(v)], dtype=float)
    y = np.asarray([v for v in b if math.isfinite(v)], dtype=float)
    if x.size < 2 or y.size < 2:
        return 0.0, 0.0
    vx = float(np.var(x, ddof=1))
    vy = float(np.var(y, ddof=1))
    nx, ny = x.size, y.size
    denom = vx / nx + vy / ny
    if denom <= 1e-18:
        return 0.0, float(nx + ny - 2)
    t = (float(np.mean(x)) - float(np.mean(y))) / math.sqrt(denom)
    num = denom ** 2
    den = (vx / nx) ** 2 / max(nx - 1, 1) + (vy / ny) ** 2 / max(ny - 1, 1)
    df = safe_div(num, den, float(nx + ny - 2))
    return float(t), float(df)


def mahalanobis(vector: Sequence[float], mean: Sequence[float],
                inv_cov: np.ndarray) -> float:
    """马氏距离。用于工况归属判定与多维异常检测。"""
    v = np.asarray(vector, dtype=float) - np.asarray(mean, dtype=float)
    try:
        d2 = float(v @ inv_cov @ v.T)
    except ValueError:
        return math.inf
    return math.sqrt(max(d2, 0.0))


def shrinkage_inverse(cov: np.ndarray, ridge: float = 1e-6) -> np.ndarray:
    """带收缩的协方差求逆，避免小样本下矩阵奇异。"""
    cov = np.asarray(cov, dtype=float)
    n = cov.shape[0]
    trace_mean = float(np.trace(cov)) / max(n, 1)
    shrunk = cov + np.eye(n) * max(ridge, trace_mean * ridge)
    try:
        return np.linalg.inv(shrunk)
    except np.linalg.LinAlgError:
        return np.linalg.pinv(shrunk)


def linear_regression(x: Sequence[float], y: Sequence[float]) -> Dict[str, float]:
    """最小二乘一元线性回归。用于退化趋势拟合。

    :return: ``{"slope","intercept","r2","n"}``
    """
    xs = np.asarray(x, dtype=float)
    ys = np.asarray(y, dtype=float)
    mask = np.isfinite(xs) & np.isfinite(ys)
    xs, ys = xs[mask], ys[mask]
    if xs.size < 2:
        return {"slope": 0.0, "intercept": float(ys[0]) if ys.size else 0.0,
                "r2": 0.0, "n": int(xs.size)}
    x_mean = float(np.mean(xs))
    y_mean = float(np.mean(ys))
    sxx = float(np.sum((xs - x_mean) ** 2))
    if sxx < 1e-18:
        return {"slope": 0.0, "intercept": y_mean, "r2": 0.0, "n": int(xs.size)}
    sxy = float(np.sum((xs - x_mean) * (ys - y_mean)))
    slope = sxy / sxx
    intercept = y_mean - slope * x_mean
    ss_tot = float(np.sum((ys - y_mean) ** 2))
    ss_res = float(np.sum((ys - (slope * xs + intercept)) ** 2))
    r2 = 1.0 - safe_div(ss_res, ss_tot, 0.0) if ss_tot > 1e-18 else 0.0
    return {"slope": slope, "intercept": intercept, "r2": clamp(r2, 0.0, 1.0),
            "n": int(xs.size)}


def ewma(values: Sequence[float], alpha: float = 0.2) -> List[float]:
    """指数加权移动平均序列。"""
    out: List[float] = []
    acc: Optional[float] = None
    for v in values:
        if not math.isfinite(v):
            continue
        acc = v if acc is None else alpha * v + (1 - alpha) * acc
        out.append(acc)
    return out


def cosine_similarity(a: Dict[str, float], b: Dict[str, float]) -> float:
    """稀疏字典向量的余弦相似度。案例检索使用。"""
    keys = set(a) & set(b)
    if not keys:
        return 0.0
    dot = sum(finite(a[k]) * finite(b[k]) for k in keys)
    na = math.sqrt(sum(finite(v) ** 2 for v in a.values()))
    nb = math.sqrt(sum(finite(v) ** 2 for v in b.values()))
    return clamp(safe_div(dot, na * nb), -1.0, 1.0)


def weighted_similarity(a: Dict[str, float], b: Dict[str, float],
                        scales: Optional[Dict[str, float]] = None) -> float:
    """归一化欧氏相似度：按各维尺度折算后取 1/(1+d)。

    相比余弦相似度，本函数对 "量级接近" 更敏感，适合工况签名匹配。
    """
    keys = set(a) & set(b)
    if not keys:
        return 0.0
    total = 0.0
    for k in keys:
        scale = abs(finite((scales or {}).get(k, 1.0), 1.0)) or 1.0
        total += ((finite(a[k]) - finite(b[k])) / scale) ** 2
    dist = math.sqrt(total / len(keys))
    return 1.0 / (1.0 + dist)


# ==========================================================================
# 时序工具
# ==========================================================================
def resample_uniform(timestamps: Sequence[float], values: Sequence[float],
                     interval: float) -> Tuple[List[float], List[float]]:
    """把不等间隔时序线性插值为等间隔序列。"""
    if len(timestamps) != len(values):
        raise ValueError("时间戳与数值长度不一致")
    pairs = sorted((t, v) for t, v in zip(timestamps, values)
                   if math.isfinite(t) and math.isfinite(v))
    if len(pairs) < 2 or interval <= 0:
        return [p[0] for p in pairs], [p[1] for p in pairs]
    ts = np.asarray([p[0] for p in pairs], dtype=float)
    vs = np.asarray([p[1] for p in pairs], dtype=float)
    grid = np.arange(ts[0], ts[-1] + interval * 0.5, interval)
    interp = np.interp(grid, ts, vs)
    return grid.tolist(), interp.tolist()


def slice_window(timestamps: Sequence[float], values: Sequence[float],
                 start: float, end: float) -> Tuple[List[float], List[float]]:
    """取出 ``[start, end)`` 时间窗内的数据。"""
    ts_out: List[float] = []
    vs_out: List[float] = []
    for t, v in zip(timestamps, values):
        if start <= t < end:
            ts_out.append(t)
            vs_out.append(v)
    return ts_out, vs_out


def rate_of_change(timestamps: Sequence[float], values: Sequence[float]) -> float:
    """单位时间变化率（每秒）。"""
    if len(timestamps) < 2:
        return 0.0
    fit = linear_regression(timestamps, values)
    return fit["slope"]


def detect_step_change(values: Sequence[float], min_gap: float = 3.0) -> Optional[int]:
    """检测序列中的阶跃突变点，返回索引；无突变返回 None。

    采用滑动均值差的最大值与稳健标准差比较。
    """
    arr = np.asarray([v for v in values if math.isfinite(v)], dtype=float)
    n = arr.size
    if n < 8:
        return None
    stats = robust_stats(arr.tolist())
    sigma = stats["mad"] if stats["mad"] > 1e-9 else stats["std"]
    if sigma < 1e-9:
        return None
    best_idx, best_score = None, 0.0
    for i in range(3, n - 3):
        left = float(np.mean(arr[:i]))
        right = float(np.mean(arr[i:]))
        score = abs(right - left) / sigma
        if score > best_score:
            best_score, best_idx = score, i
    return best_idx if best_score >= min_gap else None


def percentile_of(value: float, samples: Sequence[float]) -> float:
    """给定值在样本中的分位（0~1）。"""
    arr = np.asarray([v for v in samples if math.isfinite(v)], dtype=float)
    if arr.size == 0:
        return 0.5
    return float(np.sum(arr <= value) / arr.size)


def chunked(items: Sequence[Any], size: int) -> Iterable[List[Any]]:
    """按固定大小切块。"""
    size = max(1, size)
    for i in range(0, len(items), size):
        yield list(items[i:i + size])


def format_duration(seconds: float) -> str:
    """把秒数格式化为中文可读时长。"""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.0f}秒"
    if seconds < 3600:
        return f"{seconds / 60:.1f}分钟"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}小时"
    return f"{seconds / 86400:.1f}天"


__all__ = [
    "safe_div", "clamp", "finite", "normalize01", "sigmoid",
    "score_from_deviation", "weighted_mean", "robust_stats", "welch_t_test",
    "mahalanobis", "shrinkage_inverse", "linear_regression", "ewma",
    "cosine_similarity", "weighted_similarity", "resample_uniform",
    "slice_window", "rate_of_change", "detect_step_change", "percentile_of",
    "chunked", "format_duration",
]

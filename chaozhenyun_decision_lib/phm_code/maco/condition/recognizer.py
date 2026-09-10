"""
工况识别模块
============

**这是 "多工况自适应" 的立足点。** 同一台设备在 50% 负载和 100% 负载下，
振动、温度、电流的正常范围完全不同；若用一套固定阈值判断，低负载时漏报、
高负载时误报。因此系统先识别 "现在是哪个工况"，再在该工况的历史基线上
评估健康、在该工况的历史样本上做参数寻优。

识别分三步：
1. **签名提取** —— 从特征向量中取出决定工况的少数几个量（负载率、转速比、
   介质温度等），构成工况签名；
2. **归属判定** —— 用马氏距离把签名匹配到已知工况；超过新颖度阈值则
   判定为新工况并自动建档（工况库自增长）；
3. **稳态判定** —— 看签名在时间窗内的波动程度，只有稳态才允许触发优化，
   避免在升降负载的过渡过程中做出错误调整。
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..core.enums import ConditionStability, EventTopic
from ..core.event_bus import EventBus
from ..core.logging_setup import get_logger
from ..core.types import ConditionSnapshot, FeatureVector, now_ts
from ..core.utils import mahalanobis, shrinkage_inverse

logger = get_logger("condition.recognizer")

#: 默认工况签名维度（按重要性排序，缺失自动跳过）
DEFAULT_SIGNATURE_KEYS: Tuple[str, ...] = (
    "dev.load_ratio",
    "dev.speed_ratio",
    "dev.current_ratio",
    "dev.temp_mean",
)


@dataclass
class ConditionProfile:
    """工况档案：一个工况的统计中心与运行画像。"""

    condition_id: str
    label: str
    keys: Tuple[str, ...]
    mean: List[float] = field(default_factory=list)
    #: 各维方差（在线更新）
    var: List[float] = field(default_factory=list)
    count: int = 0
    created_at: float = field(default_factory=now_ts)
    updated_at: float = field(default_factory=now_ts)
    #: 该工况下的累计运行时长（秒）
    total_seconds: float = 0.0
    #: 该工况下的典型可调参数取值（由反馈学习回填）
    best_params: Dict[str, float] = field(default_factory=dict)
    best_reward: float = 0.0

    def update(self, vector: Sequence[float]) -> None:
        """Welford 在线更新均值与方差。"""
        v = list(vector)
        if not self.mean:
            self.mean = list(v)
            self.var = [0.0] * len(v)
            self.count = 1
            return
        self.count += 1
        for i, x in enumerate(v):
            if i >= len(self.mean):
                break
            delta = x - self.mean[i]
            self.mean[i] += delta / self.count
            self.var[i] += delta * (x - self.mean[i])

    #: 各维允许的最小相对标准差。没有这个下限，小样本会把方差压到接近 0，
    #: 马氏距离随之爆炸，结果是每一拍都被判成新工况——这是工况库失控的根因。
    MIN_RELATIVE_SIGMA = 0.03

    def covariance(self) -> np.ndarray:
        """对角协方差矩阵（工况维度间近似独立，且小样本下更稳）。"""
        n = len(self.mean)
        if n == 0:
            return np.eye(1)
        denom = max(self.count - 1, 1)
        diag: List[float] = []
        for i in range(n):
            raw = (self.var[i] / denom) if i < len(self.var) else 0.0
            floor = (abs(self.mean[i]) * self.MIN_RELATIVE_SIGMA) ** 2
            diag.append(max(raw, floor, 1e-6))
        return np.diag(diag)

    def distance(self, vector: Sequence[float]) -> float:
        """到本工况中心的距离。

        样本足够时用马氏距离；样本不足（<3）时协方差不可信，
        改用相对欧氏距离并折算到同一量纲上——10% 的相对偏差记为 1.0，
        因此可以和马氏距离共用同一个新颖度阈值。
        """
        if not self.mean:
            return math.inf
        if self.count < 3:
            total = 0.0
            for m, x in zip(self.mean, vector):
                scale = max(abs(m), 0.05)
                total += ((x - m) / scale) ** 2
            return math.sqrt(total / max(len(self.mean), 1)) * 10.0
        inv = shrinkage_inverse(self.covariance())
        return mahalanobis(vector, self.mean, inv)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "label": self.label,
            "keys": list(self.keys),
            "center": {k: round(v, 4) for k, v in zip(self.keys, self.mean)},
            "count": self.count,
            "total_seconds": round(self.total_seconds, 1),
            "best_params": {k: round(v, 4) for k, v in self.best_params.items()},
            "best_reward": round(self.best_reward, 4),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ConditionLibrary:
    """工况库：保存一台设备的全部已知工况。"""

    def __init__(self, device_id: str, max_conditions: int = 32):
        self.device_id = device_id
        self.max_conditions = max_conditions
        self._profiles: Dict[str, ConditionProfile] = {}
        self._lock = threading.RLock()
        self._seq = 0

    def create(self, keys: Tuple[str, ...], vector: Sequence[float],
               label: str = "") -> ConditionProfile:
        with self._lock:
            self._seq += 1
            cid = f"{self.device_id}-C{self._seq:02d}"
            profile = ConditionProfile(
                condition_id=cid,
                label=label or self._auto_label(keys, vector),
                keys=keys,
            )
            profile.update(vector)
            self._profiles[cid] = profile
            return profile

    @staticmethod
    def _auto_label(keys: Tuple[str, ...], vector: Sequence[float]) -> str:
        """按负载率自动命名，便于操作工理解。"""
        mapping = dict(zip(keys, vector))
        load = mapping.get("dev.load_ratio")
        if load is None:
            load = mapping.get("dev.speed_ratio")
        if load is None:
            return "未命名工况"
        if load < 0.25:
            return "低负载工况"
        if load < 0.55:
            return "中负载工况"
        if load < 0.85:
            return "高负载工况"
        return "满负载工况"

    def get(self, condition_id: str) -> Optional[ConditionProfile]:
        with self._lock:
            return self._profiles.get(condition_id)

    def all(self) -> List[ConditionProfile]:
        with self._lock:
            return list(self._profiles.values())

    def nearest(self, vector: Sequence[float]) -> Tuple[Optional[ConditionProfile], float]:
        """找出距离最近的工况及其马氏距离。"""
        best: Optional[ConditionProfile] = None
        best_d = math.inf
        for profile in self.all():
            d = profile.distance(vector)
            if d < best_d:
                best_d, best = d, profile
        return best, best_d

    def prune(self) -> None:
        """工况数超限时淘汰样本最少的工况，防止无限增长。"""
        with self._lock:
            if len(self._profiles) <= self.max_conditions:
                return
            victims = sorted(self._profiles.values(), key=lambda p: p.count)
            for p in victims[: len(self._profiles) - self.max_conditions]:
                self._profiles.pop(p.condition_id, None)
                logger.info("工况库淘汰低频工况 %s（样本 %d）", p.condition_id, p.count)

    def to_dict(self) -> Dict[str, Any]:
        return {"device_id": self.device_id,
                "count": len(self._profiles),
                "conditions": [p.to_dict() for p in self.all()]}


class ConditionRecognizer:
    """工况识别器。"""

    def __init__(
        self,
        bus: Optional[EventBus] = None,
        signature_keys: Sequence[str] = DEFAULT_SIGNATURE_KEYS,
        novelty_threshold: float = 3.5,
        steady_cv_threshold: float = 0.05,
        steady_min_dwell_s: float = 60.0,
        debounce_count: int = 3,
        max_conditions: int = 32,
        steady_min_samples: int = 12,
        window_points: int = 30,
    ):
        self.bus = bus
        self.signature_keys = tuple(signature_keys)
        self.novelty_threshold = novelty_threshold
        self.steady_cv_threshold = steady_cv_threshold
        self.steady_min_dwell_s = steady_min_dwell_s
        #: 观测充分性的另一条判据：采样条数。现场采样周期差异很大，
        #: 只卡时长会让高频采集场景等待过久，只卡条数又会让低频场景误判，
        #: 因此两者满足其一即认为观测已充分。
        self.steady_min_samples = max(3, steady_min_samples)
        self.window_points = max(5, window_points)
        self.debounce_count = max(1, debounce_count)
        self.max_conditions = max_conditions
        self._libraries: Dict[str, ConditionLibrary] = {}
        self._current: Dict[str, str] = {}
        self._since: Dict[str, float] = {}
        self._history: Dict[str, List[Tuple[float, List[float]]]] = {}
        self._pending: Dict[str, Tuple[str, int]] = {}
        self._last_seen: Dict[str, float] = {}
        self._lock = threading.RLock()

    def library(self, device_id: str) -> ConditionLibrary:
        with self._lock:
            lib = self._libraries.get(device_id)
            if lib is None:
                lib = ConditionLibrary(device_id, self.max_conditions)
                self._libraries[device_id] = lib
            return lib

    # ------------------------------------------------------------------
    def recognize(self, features: FeatureVector) -> ConditionSnapshot:
        """识别当前工况。"""
        device_id = features.device_id
        keys = tuple(k for k in self.signature_keys if k in features.values)
        if not keys:
            # 无签名维度时退化为单一默认工况，保证下游不中断
            return ConditionSnapshot(
                device_id=device_id, condition_id=f"{device_id}-C00",
                label="默认工况", confidence=0.3,
                stability=ConditionStability.UNKNOWN,
                timestamp=features.timestamp, trace_id=features.trace_id,
            )
        vector = [features.get(k) for k in keys]
        lib = self.library(device_id)

        profile, distance = lib.nearest(vector)
        is_new = False
        if profile is None or distance > self.novelty_threshold:
            profile = lib.create(keys, vector)
            lib.prune()
            is_new = True
            distance = 0.0
            logger.info("设备 %s 发现新工况 %s（%s）", device_id,
                        profile.condition_id, profile.label)
            if self.bus:
                self.bus.publish(EventTopic.CONDITION_DISCOVERED, profile.to_dict(),
                                 device_id=device_id, trace_id=features.trace_id)
        else:
            profile.update(vector)
            profile.updated_at = features.timestamp

        confidence = 1.0 / (1.0 + max(distance, 0.0))
        stability, dwell = self._judge_stability(device_id, features.timestamp, vector,
                                                 profile.condition_id)
        snapshot = ConditionSnapshot(
            device_id=device_id,
            condition_id=profile.condition_id,
            label=profile.label,
            confidence=confidence,
            stability=stability,
            signature=dict(zip(keys, vector)),
            dwell_seconds=dwell,
            is_new=is_new,
            timestamp=features.timestamp,
            trace_id=features.trace_id,
        )
        self._emit_change(snapshot, profile)
        return snapshot

    # ------------------------------------------------------------------
    def _judge_stability(self, device_id: str, ts: float, vector: List[float],
                         condition_id: str) -> Tuple[ConditionStability, float]:
        """按签名在时间窗内的波动判断稳态。"""
        with self._lock:
            hist = self._history.setdefault(device_id, [])
            hist.append((ts, list(vector)))
            # 双重裁剪：既限时间窗，也限条数。只限时间窗时，采样很密的场景
            # 会把跨工况的数据一起算进变异系数，稳态永远判不出来。
            cutoff = ts - max(self.steady_min_dwell_s * 2, 60.0)
            trimmed = [h for h in hist if h[0] >= cutoff][-self.window_points:]
            self._history[device_id] = trimmed
            hist = trimmed
            since = self._since.get(device_id)
            if self._current.get(device_id) != condition_id or since is None:
                since = ts
            dwell = ts - since

        if len(hist) < 3:
            return ConditionStability.UNKNOWN, dwell

        arr = np.asarray([h[1] for h in hist], dtype=float)
        means = np.abs(np.mean(arr, axis=0))
        stds = np.std(arr, axis=0)
        cvs = [float(s / m) if m > 1e-9 else 0.0 for s, m in zip(stds, means)]
        max_cv = max(cvs) if cvs else 0.0

        # 负载趋势判断启停机
        load = arr[:, 0]
        drift = float(load[-1] - load[0])
        level = float(np.mean(load))

        if level < 0.05:
            return ConditionStability.IDLE, dwell
        if max_cv > self.steady_cv_threshold * 4:
            if drift > 0.15:
                return ConditionStability.STARTUP, dwell
            if drift < -0.15:
                return ConditionStability.SHUTDOWN, dwell
            return ConditionStability.TRANSIENT, dwell
        if max_cv > self.steady_cv_threshold:
            return ConditionStability.TRANSIENT, dwell
        # 观测充分性：持续时长与采样条数满足其一即可
        if dwell < self.steady_min_dwell_s and len(hist) < self.steady_min_samples:
            return ConditionStability.TRANSIENT, dwell
        return ConditionStability.STEADY, dwell

    def _emit_change(self, snapshot: ConditionSnapshot,
                     profile: ConditionProfile) -> None:
        """带去抖动的工况切换通告：连续 N 次判为新工况才确认切换。"""
        device_id = snapshot.device_id
        with self._lock:
            previous = self._current.get(device_id)
            if previous == snapshot.condition_id:
                self._pending.pop(device_id, None)
                # 工况未变：累计该工况的运行时长，供工况画像统计
                last = self._last_seen.get(device_id)
                if last is not None and snapshot.timestamp > last:
                    profile.total_seconds += snapshot.timestamp - last
                self._last_seen[device_id] = snapshot.timestamp
                return
            self._last_seen[device_id] = snapshot.timestamp
            candidate, hits = self._pending.get(device_id, (snapshot.condition_id, 0))
            if candidate != snapshot.condition_id:
                candidate, hits = snapshot.condition_id, 0
            hits += 1
            self._pending[device_id] = (candidate, hits)
            if hits < self.debounce_count and previous is not None:
                return
            self._current[device_id] = snapshot.condition_id
            self._since[device_id] = snapshot.timestamp
            self._pending.pop(device_id, None)

        logger.info("设备 %s 工况切换：%s → %s（%s）", device_id,
                    previous or "-", snapshot.condition_id, snapshot.label)
        if self.bus:
            self.bus.publish(EventTopic.CONDITION_CHANGED,
                             {"from": previous, "to": snapshot.condition_id,
                              "label": snapshot.label,
                              "snapshot": snapshot.to_dict()},
                             device_id=device_id, trace_id=snapshot.trace_id)

    # ------------------------------------------------------------------
    def current_condition(self, device_id: str) -> Optional[str]:
        with self._lock:
            return self._current.get(device_id)

    def record_best(self, device_id: str, condition_id: str,
                    params: Dict[str, float], reward: float) -> None:
        """反馈学习回填：记录某工况下的最优参数组合。"""
        profile = self.library(device_id).get(condition_id)
        if profile is None:
            return
        if reward > profile.best_reward or not profile.best_params:
            profile.best_params = dict(params)
            profile.best_reward = reward
            logger.info("工况 %s 更新最优参数（奖励 %.4f）", condition_id, reward)

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "signature_keys": list(self.signature_keys),
                "novelty_threshold": self.novelty_threshold,
                "libraries": [lib.to_dict() for lib in self._libraries.values()],
                "current": dict(self._current),
            }


__all__ = ["ConditionRecognizer", "ConditionLibrary", "ConditionProfile",
           "DEFAULT_SIGNATURE_KEYS"]

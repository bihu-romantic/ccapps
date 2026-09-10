"""
状态识别与健康评估模块
======================

对应系统功能二。核心思想是 **工况归一化评估**：不比较绝对值，而是比较
"当前值相对本工况历史基线偏离了多少"。同一台风机在满负载下振动 4.5 mm/s
可能完全正常，在空载下 4.5 mm/s 却是明显异常——脱离工况谈阈值没有意义。

处理链：
1. **基线学习**（:class:`BaselineLibrary`）—— 按 (设备, 工况, 特征) 三元组
   在线累积稳健统计量，形成正常区间；
2. **指标计算** —— 把偏离量映射为 0~1 的分项健康分；
3. **异常检测** —— EWMA 控制图 + 多维马氏距离双通道；
4. **故障模式定位** —— 用规则化的机理判据给出疑似模式及置信度；
5. **退化趋势** —— 对健康分做回归，估算劣化速率与剩余可用时间；
6. **融合评级** —— 加权融合出总健康分与等级。
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..core.enums import (
    EventTopic,
    FaultPattern,
    HealthGrade,
    Severity,
    SignalKind,
    TrendDirection,
)
from ..core.event_bus import EventBus
from ..core.logging_setup import get_logger
from ..core.registry import DeviceProfile
from ..core.types import (
    ConditionSnapshot,
    FeatureVector,
    HealthIndicator,
    HealthReport,
    now_ts,
)
from ..core.utils import (
    clamp,
    linear_regression,
    robust_stats,
    safe_div,
    score_from_deviation,
    weighted_mean,
)

logger = get_logger("health.assessor")


# ==========================================================================
# 基线库
# ==========================================================================
@dataclass
class Baseline:
    """单个 (工况, 特征) 的正常基线。"""

    key: str
    values: List[float] = field(default_factory=list)
    capacity: int = 500
    #: 判定 "基线已建立" 所需的最少样本数
    min_samples: int = 20
    mean: float = 0.0
    std: float = 0.0
    median: float = 0.0
    mad: float = 0.0
    low: float = 0.0
    high: float = 0.0
    #: EWMA 控制图状态
    ewma: Optional[float] = None
    updated_at: float = field(default_factory=now_ts)

    @property
    def ready(self) -> bool:
        return len(self.values) >= self.min_samples

    def add(self, value: float) -> None:
        if not math.isfinite(value):
            return
        self.values.append(value)
        if len(self.values) > self.capacity:
            del self.values[: len(self.values) - self.capacity]
        stats = robust_stats(self.values)
        self.mean, self.std = stats["mean"], stats["std"]
        self.median, self.mad = stats["median"], stats["mad"]
        spread = self.mad if self.mad > 1e-9 else max(self.std, abs(self.median) * 0.05)
        self.low = self.median - 3.0 * spread
        self.high = self.median + 3.0 * spread
        self.updated_at = now_ts()

    def deviation(self, value: float) -> float:
        """相对基线的标准化偏离（单位：σ）。"""
        spread = self.mad if self.mad > 1e-9 else self.std
        if spread < 1e-9:
            spread = max(abs(self.median) * 0.05, 1e-6)
        return (value - self.median) / spread

    def update_ewma(self, value: float, alpha: float = 0.2) -> float:
        self.ewma = value if self.ewma is None else alpha * value + (1 - alpha) * self.ewma
        return self.ewma

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key, "n": len(self.values), "ready": self.ready,
            "median": round(self.median, 6), "mad": round(self.mad, 6),
            "low": round(self.low, 6), "high": round(self.high, 6),
        }


class BaselineLibrary:
    """基线库：按 设备/工况/特征 三级索引。"""

    def __init__(self, min_samples: int = 50):
        self.min_samples = max(5, int(min_samples))
        self._data: Dict[str, Dict[str, Dict[str, Baseline]]] = {}
        self._lock = threading.RLock()

    def get(self, device_id: str, condition_id: str, feature: str) -> Baseline:
        with self._lock:
            cond_map = self._data.setdefault(device_id, {}).setdefault(condition_id, {})
            bl = cond_map.get(feature)
            if bl is None:
                bl = Baseline(key=f"{device_id}/{condition_id}/{feature}",
                              min_samples=self.min_samples)
                cond_map[feature] = bl
            return bl

    def learn(self, device_id: str, condition_id: str,
              features: FeatureVector, only: Optional[Sequence[str]] = None) -> int:
        """把当前特征纳入基线（仅在设备被判定为健康时调用）。"""
        names = only if only is not None else list(features.values)
        count = 0
        for name in names:
            value = features.values.get(name)
            if value is None or not math.isfinite(value):
                continue
            self.get(device_id, condition_id, name).add(float(value))
            count += 1
        return count

    def ready(self, device_id: str, condition_id: str) -> bool:
        with self._lock:
            cond_map = self._data.get(device_id, {}).get(condition_id, {})
            if not cond_map:
                return False
            return sum(1 for b in cond_map.values() if b.ready) >= 3

    def summary(self, device_id: str) -> Dict[str, Any]:
        with self._lock:
            conds = self._data.get(device_id, {})
            return {
                "device_id": device_id,
                "conditions": {
                    cid: {"features": len(m),
                          "ready": sum(1 for b in m.values() if b.ready)}
                    for cid, m in conds.items()
                },
            }


# ==========================================================================
# 故障模式判据
# ==========================================================================
@dataclass
class PatternRule:
    """一条故障模式判据。

    :param conditions: ``[(特征名, 比较符, 阈值)]``，全部满足才命中
    """

    pattern: FaultPattern
    conditions: List[Tuple[str, str, float]]
    confidence: float = 0.7
    description: str = ""

    def evaluate(self, values: Dict[str, float]) -> Optional[float]:
        """返回命中置信度，未命中返回 None。"""
        hits = 0
        for name, op, threshold in self.conditions:
            v = values.get(name)
            if v is None or not math.isfinite(v):
                return None
            if op == ">" and not v > threshold:
                return None
            if op == "<" and not v < threshold:
                return None
            if op == ">=" and not v >= threshold:
                return None
            if op == "<=" and not v <= threshold:
                return None
            hits += 1
        return self.confidence if hits else None


def default_pattern_rules() -> List[PatternRule]:
    """内置的通用旋转机械故障判据（基于偏离度而非绝对值）。"""
    return [
        PatternRule(FaultPattern.UNBALANCE,
                    [("dev.vib_1x_ratio", ">", 3.0), ("dev.vib_2x_ratio", "<", 1.5)],
                    0.75, "1 倍频突出、2 倍频不高，典型转子不平衡"),
        PatternRule(FaultPattern.MISALIGNMENT,
                    [("dev.vib_2x_ratio", ">", 2.0), ("dev.vib_1x_ratio", ">", 1.5)],
                    0.72, "2 倍频显著，伴随 1 倍频升高，典型联轴器不对中"),
        PatternRule(FaultPattern.LOOSENESS,
                    [("dev.spectral_entropy_dev", ">", 2.5), ("dev.crest_dev", ">", 2.0)],
                    0.65, "谱熵与峰值因子同时升高，疑似结构松动"),
        PatternRule(FaultPattern.BEARING_OUTER,
                    [("dev.bpfo_ratio", ">", 4.0)],
                    0.78, "包络谱外圈通过频率处峰值突出"),
        PatternRule(FaultPattern.BEARING_INNER,
                    [("dev.bpfi_ratio", ">", 4.0)],
                    0.78, "包络谱内圈通过频率处峰值突出"),
        PatternRule(FaultPattern.BEARING_ROLLER,
                    [("dev.bsf_ratio", ">", 4.0)],
                    0.70, "包络谱滚动体频率处峰值突出"),
        PatternRule(FaultPattern.OVERHEAT,
                    [("dev.temp_dev", ">", 3.0)],
                    0.80, "温度显著高于本工况基线"),
        PatternRule(FaultPattern.OVERLOAD,
                    [("dev.current_dev", ">", 2.5), ("dev.load_ratio", ">", 0.95)],
                    0.70, "电流偏高且负载接近额定，疑似过载"),
        PatternRule(FaultPattern.FOULING,
                    [("dev.efficiency_dev", "<", -2.0), ("dev.temp_dev", ">", 1.5)],
                    0.60, "能效下降伴随温升，疑似结垢/积灰"),
    ]


# ==========================================================================
# 健康评估器
# ==========================================================================
class HealthAssessor:
    """健康评估器。"""

    #: 各分项在总分中的权重键
    DIMENSIONS = ("vibration", "temperature", "current", "efficiency")

    def __init__(
        self,
        baselines: Optional[BaselineLibrary] = None,
        bus: Optional[EventBus] = None,
        weights: Optional[Dict[str, float]] = None,
        ewma_alpha: float = 0.2,
        ewma_sigma_k: float = 3.0,
        trend_window_hours: float = 72.0,
        pattern_rules: Optional[List[PatternRule]] = None,
    ):
        self.baselines = baselines or BaselineLibrary()
        self.bus = bus
        self.weights = weights or {"vibration": 0.35, "temperature": 0.25,
                                   "current": 0.20, "efficiency": 0.20}
        self.ewma_alpha = ewma_alpha
        self.ewma_sigma_k = ewma_sigma_k
        self.trend_window_hours = trend_window_hours
        self.pattern_rules = pattern_rules or default_pattern_rules()
        self._history: Dict[str, List[Tuple[float, float]]] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    def assess(self, profile: DeviceProfile, features: FeatureVector,
               condition: ConditionSnapshot) -> HealthReport:
        """执行一次健康评估。"""
        device_id = profile.device_id
        cid = condition.condition_id
        indicators: List[HealthIndicator] = []
        anomalies: List[str] = []
        deviations: Dict[str, float] = {}

        # 1. 分维度指标
        indicators += self._vibration_indicators(profile, features, cid, deviations, anomalies)
        indicators += self._thermal_indicators(profile, features, cid, deviations, anomalies)
        indicators += self._electric_indicators(profile, features, cid, deviations, anomalies)
        indicators += self._efficiency_indicators(profile, features, cid, deviations, anomalies)

        # 2. 融合总分
        score = self._fuse(indicators)

        # 3. 故障模式定位
        pattern_input = dict(features.values)
        pattern_input.update(deviations)
        patterns = self._match_patterns(pattern_input)

        # 4. 趋势与剩余寿命
        trend, rate, remaining = self._update_trend(device_id, score, features.timestamp)

        report = HealthReport(
            device_id=device_id,
            condition_id=cid,
            score=score,
            grade=HealthGrade.from_score(score),
            indicators=indicators,
            patterns=patterns,
            trend=trend,
            degradation_rate=rate,
            remaining_hours=remaining,
            anomalies=anomalies,
            timestamp=features.timestamp,
            trace_id=features.trace_id,
        )

        # 5. 健康时反哺基线（劣化时不学，避免把故障当正常）
        if report.grade is HealthGrade.HEALTHY and condition.stability.code == "steady":
            self.baselines.learn(device_id, cid, features)

        if self.bus:
            self.bus.publish(EventTopic.HEALTH_UPDATED, report,
                             device_id=device_id, trace_id=features.trace_id)
            if anomalies:
                self.bus.publish(EventTopic.ANOMALY_DETECTED,
                                 {"count": len(anomalies), "items": anomalies,
                                  "grade": report.grade.code},
                                 device_id=device_id, trace_id=features.trace_id)
        return report

    # ------------------------------------------------------------------
    def _indicator(self, device_id: str, cid: str, feature: str, label: str,
                   value: Optional[float], unit: str, weight: float,
                   dev_key: str, deviations: Dict[str, float],
                   anomalies: List[str], tolerance: float = 3.0,
                   direction: str = "both") -> Optional[HealthIndicator]:
        """统一的分项指标构造：取基线 → 算偏离 → 折算健康分。"""
        if value is None or not math.isfinite(value):
            return None
        baseline = self.baselines.get(device_id, cid, feature)
        if not baseline.ready:
            baseline.add(value)
            return HealthIndicator(
                name=feature, label=label, value=value, score=1.0, unit=unit,
                weight=weight, severity=Severity.INFO, remark="基线学习中",
            )
        dev = baseline.deviation(value)
        deviations[dev_key] = dev
        # 单向指标：只有超出方向才算劣化（如温度只怕高不怕低）
        effective = dev if direction == "both" else (max(dev, 0.0) if direction == "high"
                                                     else min(dev, 0.0))
        score = score_from_deviation(effective / tolerance, 1.0, 2.0)
        severity = Severity.INFO
        if abs(effective) > tolerance * 1.5:
            severity = Severity.ALARM
        elif abs(effective) > tolerance:
            severity = Severity.WARNING

        # EWMA 控制图：连续小幅偏移也能检出
        smoothed = baseline.update_ewma(dev, self.ewma_alpha)
        if abs(smoothed) > self.ewma_sigma_k:
            anomalies.append(f"{label} 连续偏离基线（EWMA={smoothed:.2f}σ）")
        elif severity is not Severity.INFO:
            anomalies.append(f"{label} 偏离基线 {dev:.2f}σ（当前 {value:.3f}{unit}）")

        return HealthIndicator(
            name=feature, label=label, value=value, score=score, unit=unit,
            baseline_low=baseline.low, baseline_high=baseline.high,
            deviation=dev, weight=weight, severity=severity,
        )

    def _vibration_indicators(self, profile: DeviceProfile, features: FeatureVector,
                              cid: str, deviations: Dict[str, float],
                              anomalies: List[str]) -> List[HealthIndicator]:
        out: List[HealthIndicator] = []
        w = self.weights.get("vibration", 0.35)
        vib_points = profile.points_of_kind(SignalKind.VIBRATION)
        if not vib_points:
            return out
        per_point_w = w / len(vib_points)
        for p in vib_points:
            rms = features.values.get(f"{p.point_id}.rms")
            ind = self._indicator(profile.device_id, cid, f"{p.point_id}.rms",
                                  f"{p.name} 振动有效值", rms, p.unit or "mm/s",
                                  per_point_w * 0.6, "dev.vib_dev", deviations,
                                  anomalies, direction="high")
            if ind:
                out.append(ind)
            kurt = features.values.get(f"{p.point_id}.kurtosis")
            ind = self._indicator(profile.device_id, cid, f"{p.point_id}.kurtosis",
                                  f"{p.name} 峭度", kurt, "", per_point_w * 0.2,
                                  "dev.kurtosis_dev", deviations, anomalies,
                                  direction="high")
            if ind:
                out.append(ind)
            crest = features.values.get(f"{p.point_id}.crest_factor")
            ind = self._indicator(profile.device_id, cid, f"{p.point_id}.crest_factor",
                                  f"{p.name} 峰值因子", crest, "", per_point_w * 0.2,
                                  "dev.crest_dev", deviations, anomalies,
                                  direction="high")
            if ind:
                out.append(ind)
            # 把轴承特征频率比值直接透传给模式判据
            for tag in ("bpfo", "bpfi", "bsf", "ftf"):
                v = features.values.get(f"{p.point_id}.{tag}_ratio")
                if v is not None:
                    deviations[f"dev.{tag}_ratio"] = v
            for tag, key in (("spectral_entropy", "dev.spectral_entropy_dev"),):
                v = features.values.get(f"{p.point_id}.{tag}")
                if v is not None:
                    bl = self.baselines.get(profile.device_id, cid, f"{p.point_id}.{tag}")
                    if bl.ready:
                        deviations[key] = bl.deviation(v)
                    else:
                        bl.add(v)
        return out

    def _thermal_indicators(self, profile: DeviceProfile, features: FeatureVector,
                            cid: str, deviations: Dict[str, float],
                            anomalies: List[str]) -> List[HealthIndicator]:
        out: List[HealthIndicator] = []
        w = self.weights.get("temperature", 0.25)
        temp_max = features.values.get("dev.temp_max")
        ind = self._indicator(profile.device_id, cid, "dev.temp_max", "最高温度",
                              temp_max, "℃", w * 0.6, "dev.temp_dev", deviations,
                              anomalies, direction="high")
        if ind:
            out.append(ind)
        rise = features.values.get("dev.temp_rise")
        ind = self._indicator(profile.device_id, cid, "dev.temp_rise", "温升",
                              rise, "K", w * 0.4, "dev.temp_rise_dev", deviations,
                              anomalies, direction="high")
        if ind:
            out.append(ind)
        # 绝对温度红线：超过铭牌报警值直接判危险，不依赖基线
        limit = profile.nameplate_of("temp_alarm_c", 0.0)
        if temp_max is not None and limit > 0 and temp_max > limit:
            anomalies.append(f"温度 {temp_max:.1f}℃ 超过报警限 {limit:.1f}℃")
            out.append(HealthIndicator(
                name="dev.temp_limit", label="温度红线", value=temp_max, score=0.0,
                unit="℃", weight=w, severity=Severity.FAULT,
                remark=f"超过铭牌报警限 {limit:.1f}℃",
            ))
        return out

    def _electric_indicators(self, profile: DeviceProfile, features: FeatureVector,
                             cid: str, deviations: Dict[str, float],
                             anomalies: List[str]) -> List[HealthIndicator]:
        out: List[HealthIndicator] = []
        w = self.weights.get("current", 0.20)
        ratio = features.values.get("dev.current_ratio")
        ind = self._indicator(profile.device_id, cid, "dev.current_ratio", "电流负载率",
                              ratio, "", w * 0.6, "dev.current_dev", deviations,
                              anomalies, direction="high")
        if ind:
            out.append(ind)
        unbalance = features.values.get("dev.current_unbalance")
        if unbalance is not None:
            # 三相不平衡有国标限值（一般 <5%），用固定判据更直接
            score = clamp(1.0 - unbalance / 0.10, 0.0, 1.0)
            severity = Severity.INFO
            if unbalance > 0.05:
                severity = Severity.WARNING
                anomalies.append(f"三相电流不平衡度 {unbalance * 100:.1f}% 超过 5%")
            if unbalance > 0.10:
                severity = Severity.ALARM
            out.append(HealthIndicator(
                name="dev.current_unbalance", label="三相电流不平衡度",
                value=unbalance, score=score, unit="", weight=w * 0.4,
                severity=severity,
            ))
        if "dev.load_ratio" in features.values:
            deviations["dev.load_ratio"] = features.values["dev.load_ratio"]
        return out

    def _efficiency_indicators(self, profile: DeviceProfile, features: FeatureVector,
                               cid: str, deviations: Dict[str, float],
                               anomalies: List[str]) -> List[HealthIndicator]:
        out: List[HealthIndicator] = []
        w = self.weights.get("efficiency", 0.20)
        for feature, label, unit in (("dev.specific_energy", "单位产出能耗", "kW·h/m³"),
                                     ("dev.power_per_speed", "单位转速功率", "kW/rpm")):
            value = features.values.get(feature)
            ind = self._indicator(profile.device_id, cid, feature, label, value, unit,
                                  w / 2, "dev.efficiency_dev", deviations, anomalies,
                                  direction="high")
            if ind:
                out.append(ind)
        # 能效偏离取反：能耗升高意味着效率下降
        if "dev.efficiency_dev" in deviations:
            deviations["dev.efficiency_dev"] = -deviations["dev.efficiency_dev"]
        return out

    # ------------------------------------------------------------------
    def _fuse(self, indicators: List[HealthIndicator]) -> float:
        """加权融合，并对最差项施加拖拽，避免多数正常掩盖单点严重异常。"""
        if not indicators:
            return 1.0
        scores = [i.score for i in indicators]
        weights = [max(i.weight, 1e-6) for i in indicators]
        base = weighted_mean(scores, weights)
        worst = min(scores)
        # 最差项占 30% 权重：单一致命项不会被平均掉
        return clamp(0.7 * base + 0.3 * worst, 0.0, 1.0)

    def _match_patterns(self, values: Dict[str, float]) -> List[Tuple[FaultPattern, float]]:
        hits: List[Tuple[FaultPattern, float]] = []
        for rule in self.pattern_rules:
            conf = rule.evaluate(values)
            if conf is not None:
                hits.append((rule.pattern, conf))
        if not hits:
            return [(FaultPattern.NORMAL, 1.0)]
        hits.sort(key=lambda x: x[1], reverse=True)
        return hits[:3]

    def _update_trend(self, device_id: str, score: float,
                      ts: float) -> Tuple[TrendDirection, float, Optional[float]]:
        """维护健康分历史并拟合退化趋势。"""
        with self._lock:
            hist = self._history.setdefault(device_id, [])
            hist.append((ts, score))
            cutoff = ts - self.trend_window_hours * 3600.0
            self._history[device_id] = [h for h in hist if h[0] >= cutoff]
            hist = self._history[device_id]

        if len(hist) < 10:
            return TrendDirection.STABLE, 0.0, None

        times = [(h[0] - hist[0][0]) / 86400.0 for h in hist]  # 天
        scores = [h[1] for h in hist]
        fit = linear_regression(times, scores)
        slope = fit["slope"]  # 健康分/天
        stats = robust_stats(scores)

        if stats["std"] > 0.15 and fit["r2"] < 0.2:
            trend = TrendDirection.VOLATILE
        elif slope < -0.005:
            trend = TrendDirection.WORSENING
        elif slope > 0.005:
            trend = TrendDirection.IMPROVING
        else:
            trend = TrendDirection.STABLE

        remaining: Optional[float] = None
        if trend is TrendDirection.WORSENING and slope < -1e-6:
            # 外推到危险阈值 0.40 所需的天数
            days = (score - 0.40) / abs(slope)
            if 0 < days < 3650:
                remaining = days * 24.0
        return trend, slope, remaining

    # ------------------------------------------------------------------
    def history(self, device_id: str) -> List[Tuple[float, float]]:
        with self._lock:
            return list(self._history.get(device_id, []))

    def to_dict(self, device_id: str) -> Dict[str, Any]:
        return {
            "weights": dict(self.weights),
            "baselines": self.baselines.summary(device_id),
            "history_points": len(self.history(device_id)),
            "pattern_rules": len(self.pattern_rules),
        }


__all__ = ["HealthAssessor", "BaselineLibrary", "Baseline", "PatternRule",
           "default_pattern_rules"]

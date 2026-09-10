"""独立的工况识别与健康评估逻辑。"""

from __future__ import annotations

import cmath
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import mean, pstdev
from typing import Any, Iterable, Mapping, Sequence


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(max(value, low), high)


def time_domain_features(signal: Sequence[float]) -> dict[str, float]:
    """Extract time-domain features from a waveform."""

    values = [float(v) for v in signal if isinstance(v, (int, float)) and math.isfinite(v)]
    if len(values) < 2:
        return {}
    avg = mean(values)
    centered = [v - avg for v in values]
    abs_values = [abs(v) for v in values]
    rms = math.sqrt(mean(v * v for v in values))
    peak = max(abs_values)
    std = pstdev(values)
    mean_abs = mean(abs_values)
    sqrt_mean = mean(math.sqrt(v) for v in abs_values) ** 2
    return {
        "mean": avg,
        "std": std,
        "rms": rms,
        "peak": peak,
        "peak2peak": max(values) - min(values),
        "kurtosis": mean(((v / std) ** 4) for v in centered) if std > 1e-12 else 0.0,
        "skewness": mean(((v / std) ** 3) for v in centered) if std > 1e-12 else 0.0,
        "shape_factor": _safe_div(rms, mean_abs),
        "crest_factor": _safe_div(peak, rms),
        "clearance_factor": _safe_div(peak, sqrt_mean),
        "impulse_factor": _safe_div(peak, mean_abs),
        "cv": abs(_safe_div(std, avg)),
    }


def spectral_features(signal: Sequence[float], sampling_rate: float) -> dict[str, float]:
    """Extract compact frequency-domain features with a dependency-free DFT."""

    values = [float(v) for v in signal if isinstance(v, (int, float)) and math.isfinite(v)]
    n = len(values)
    if n < 8 or sampling_rate <= 0:
        return {}
    avg = mean(values)
    centered = [v - avg for v in values]
    limit = n // 2 + 1
    freqs: list[float] = []
    amps: list[float] = []
    for k in range(limit):
        acc = sum(centered[t] * cmath.exp(-2j * math.pi * k * t / n) for t in range(n))
        freqs.append(k * sampling_rate / n)
        amps.append(abs(acc) * 2.0 / n)
    if not amps:
        return {}
    total_amp = sum(amps) or 1.0
    powers = [a * a for a in amps]
    total_power = sum(powers) or 1.0
    dominant_idx = max(range(len(amps)), key=lambda i: amps[i])
    entropy_terms = [(p / total_power) for p in powers if p > 1e-15]
    entropy = 0.0
    if len(entropy_terms) > 1:
        entropy = -sum(p * math.log(p) for p in entropy_terms) / math.log(len(entropy_terms))
    return {
        "dominant_hz": freqs[dominant_idx],
        "dominant_amp": amps[dominant_idx],
        "centroid_hz": sum(f * a for f, a in zip(freqs, amps)) / total_amp,
        "spectral_entropy": entropy,
        "rms_spectrum": math.sqrt(mean(powers)),
    }


def bearing_frequencies(
    shaft_hz: float,
    n_rollers: int,
    roller_d: float,
    pitch_d: float,
    contact_angle_deg: float = 0.0,
) -> dict[str, float]:
    """Calculate common rolling-bearing characteristic frequencies."""

    if shaft_hz <= 0 or n_rollers <= 0 or roller_d <= 0 or pitch_d <= 0:
        return {}
    ratio = (roller_d / pitch_d) * math.cos(math.radians(contact_angle_deg))
    return {
        "bpfo": n_rollers / 2.0 * shaft_hz * (1.0 - ratio),
        "bpfi": n_rollers / 2.0 * shaft_hz * (1.0 + ratio),
        "bsf": pitch_d / (2.0 * roller_d) * shaft_hz * (1.0 - ratio * ratio),
        "ftf": shaft_hz / 2.0 * (1.0 - ratio),
    }


def _safe_div(left: float, right: float) -> float:
    return left / right if abs(right) > 1e-12 else 0.0


@dataclass(frozen=True)
class FeatureVector:
    """特征向量。"""

    device_id: str
    values: dict[str, float] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    trace_id: str = ""
    window_seconds: float = 0.0

    def get(self, name: str, default: float = 0.0) -> float:
        value = self.values.get(name, default)
        if value is None or not isinstance(value, (int, float)) or not math.isfinite(value):
            return default
        return float(value)

    def subset(self, names: Sequence[str]) -> list[float]:
        return [self.get(name) for name in names]


@dataclass(frozen=True)
class ConditionSnapshot:
    """工况快照。"""

    device_id: str
    condition_id: str
    label: str = ""
    confidence: float = 0.0
    stability: str = "unknown"
    signature: dict[str, float] = field(default_factory=dict)
    dwell_seconds: float = 0.0
    is_new: bool = False
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    trace_id: str = ""

    @property
    def optimizable(self) -> bool:
        return self.stability == "steady" and self.confidence >= 0.6

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "condition_id": self.condition_id,
            "label": self.label,
            "confidence": round(self.confidence, 4),
            "stability": self.stability,
            "signature": dict(self.signature),
            "dwell_seconds": round(self.dwell_seconds, 2),
            "is_new": self.is_new,
            "timestamp": self.timestamp.isoformat(),
            "trace_id": self.trace_id,
        }


@dataclass(frozen=True)
class HealthIndicator:
    """单项健康指标。"""

    name: str
    label: str
    value: float
    score: float
    unit: str = ""
    baseline_low: float | None = None
    baseline_high: float | None = None
    deviation: float = 0.0
    weight: float = 1.0
    severity: str = "info"
    remark: str = ""


@dataclass(frozen=True)
class HealthReport:
    """设备健康评估报告。"""

    device_id: str
    condition_id: str
    score: float = 1.0
    grade: str = "healthy"
    indicators: list[HealthIndicator] = field(default_factory=list)
    patterns: list[tuple[str, float]] = field(default_factory=list)
    trend: str = "stable"
    degradation_rate: float = 0.0
    remaining_hours: float | None = None
    anomalies: list[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    trace_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "condition_id": self.condition_id,
            "score": round(self.score, 4),
            "grade": self.grade,
            "trend": self.trend,
            "degradation_rate": round(self.degradation_rate, 6),
            "remaining_hours": self.remaining_hours,
            "anomalies": list(self.anomalies),
            "patterns": [{"pattern": p, "confidence": round(c, 4)} for p, c in self.patterns],
            "indicators": [indicator.__dict__ for indicator in self.indicators],
            "timestamp": self.timestamp.isoformat(),
            "trace_id": self.trace_id,
        }


class ConditionRecognizer:
    """轻量工况识别器。"""

    def __init__(
        self,
        signature_keys: Sequence[str] = ("dev.load_ratio", "dev.speed_ratio", "dev.current_ratio", "dev.temp_mean"),
        novelty_threshold: float = 3.5,
        steady_cv_threshold: float = 0.05,
        steady_min_dwell_s: float = 60.0,
        steady_min_samples: int = 12,
        window_points: int = 30,
    ) -> None:
        self.signature_keys = tuple(signature_keys)
        self.novelty_threshold = novelty_threshold
        self.steady_cv_threshold = steady_cv_threshold
        self.steady_min_dwell_s = steady_min_dwell_s
        self.steady_min_samples = max(3, steady_min_samples)
        self.window_points = max(5, window_points)
        self._history: dict[str, list[tuple[datetime, list[float]]]] = {}
        self._current: dict[str, str] = {}
        self._since: dict[str, datetime] = {}
        self._libraries: dict[str, dict[str, list[float]]] = {}
        self._labels: dict[str, dict[str, str]] = {}
        self._seq: dict[str, int] = {}

    def recognize(self, features: FeatureVector) -> ConditionSnapshot:
        device_id = features.device_id
        keys = tuple(key for key in self.signature_keys if key in features.values)
        if not keys:
            return ConditionSnapshot(
                device_id=device_id,
                condition_id=f"{device_id}-C00",
                label="默认工况",
                confidence=0.3,
                stability="unknown",
                timestamp=features.timestamp,
                trace_id=features.trace_id,
            )

        vector = [features.get(key) for key in keys]
        condition_id, label, distance, is_new = self._match(device_id, keys, vector)
        stability, dwell = self._judge_stability(device_id, features.timestamp, vector, condition_id)
        confidence = 1.0 / (1.0 + max(distance, 0.0))
        snapshot = ConditionSnapshot(
            device_id=device_id,
            condition_id=condition_id,
            label=label,
            confidence=confidence,
            stability=stability,
            signature=dict(zip(keys, vector)),
            dwell_seconds=dwell,
            is_new=is_new,
            timestamp=features.timestamp,
            trace_id=features.trace_id,
        )
        self._current[device_id] = condition_id
        self._since.setdefault(device_id, features.timestamp)
        return snapshot

    def _match(self, device_id: str, keys: Sequence[str], vector: Sequence[float]) -> tuple[str, str, float, bool]:
        library = self._libraries.setdefault(device_id, {})
        labels = self._labels.setdefault(device_id, {})
        best_id = ""
        best_distance = math.inf
        for condition_id, center in library.items():
            distance = self._distance(center, vector)
            if distance < best_distance:
                best_distance = distance
                best_id = condition_id
        if not best_id or best_distance > self.novelty_threshold:
            seq = self._seq.get(device_id, 0) + 1
            self._seq[device_id] = seq
            condition_id = f"{device_id}-C{seq:02d}"
            library[condition_id] = list(vector)
            labels[condition_id] = self._auto_label(keys, vector)
            return condition_id, labels[condition_id], 0.0, True
        library[best_id] = [0.8 * old + 0.2 * new for old, new in zip(library[best_id], vector)]
        if best_id not in labels:
            labels[best_id] = self._auto_label(keys, vector)
        return best_id, labels[best_id], best_distance, False

    def _distance(self, center: Sequence[float], vector: Sequence[float]) -> float:
        if not center:
            return math.inf
        if len(center) != len(vector):
            return math.inf
        total = 0.0
        for m, x in zip(center, vector):
            scale = max(abs(m), 0.05)
            total += ((x - m) / scale) ** 2
        return math.sqrt(total / max(len(center), 1)) * 10.0

    def _auto_label(self, keys: Sequence[str], vector: Sequence[float]) -> str:
        mapping = dict(zip(keys, vector))
        load = mapping.get("dev.load_ratio", mapping.get("dev.speed_ratio"))
        if load is None:
            return "未命名工况"
        if load < 0.25:
            return "低负载工况"
        if load < 0.55:
            return "中负载工况"
        if load < 0.85:
            return "高负载工况"
        return "满负载工况"

    def _judge_stability(self, device_id: str, ts: datetime, vector: list[float], condition_id: str) -> tuple[str, float]:
        hist = self._history.setdefault(device_id, [])
        hist.append((ts, list(vector)))
        cutoff = ts.timestamp() - max(self.steady_min_dwell_s * 2, 60.0)
        trimmed = [item for item in hist if item[0].timestamp() >= cutoff][-self.window_points :]
        self._history[device_id] = trimmed
        since = self._since.get(device_id, ts)
        if self._current.get(device_id) != condition_id:
            since = ts
        dwell = max(0.0, (ts - since).total_seconds())
        if len(trimmed) < 3:
            return "unknown", dwell
        rows = [row for _, row in trimmed]
        means = [abs(v) for v in _column_mean(rows)]
        stds = _column_std(rows)
        cvs = [s / m if m > 1e-9 else 0.0 for s, m in zip(stds, means)]
        max_cv = max(cvs) if cvs else 0.0
        level = _column_mean(rows)[0] if rows else 0.0
        if level < 0.05:
            return "idle", dwell
        if max_cv > self.steady_cv_threshold * 4:
            return "transient", dwell
        if max_cv > self.steady_cv_threshold:
            return "transient", dwell
        if dwell < self.steady_min_dwell_s and len(trimmed) < self.steady_min_samples:
            return "transient", dwell
        return "steady", dwell


class HealthAssessor:
    """Multi-condition health assessor with baselines, EWMA and rules."""

    def __init__(
        self,
        weights: Mapping[str, float] | None = None,
        trend_window_hours: float = 72.0,
        baseline_min_samples: int = 5,
        ewma_alpha: float = 0.2,
        ewma_sigma_k: float = 3.0,
    ) -> None:
        self.weights = dict(weights or {"vibration": 0.35, "temperature": 0.25, "current": 0.20, "efficiency": 0.20})
        self.trend_window_hours = trend_window_hours
        self.baseline_min_samples = max(3, baseline_min_samples)
        self.ewma_alpha = ewma_alpha
        self.ewma_sigma_k = ewma_sigma_k
        self._baselines: dict[str, list[float]] = {}
        self._ewma: dict[str, float] = {}
        self._score_history: dict[str, list[tuple[datetime, float]]] = {}

    def assess(self, features: FeatureVector, condition: ConditionSnapshot) -> HealthReport:
        indicators: list[HealthIndicator] = []
        anomalies: list[str] = []
        deviations: dict[str, float] = {}
        indicators += self._vibration_indicators(features, condition, deviations, anomalies)
        indicators += self._thermal_indicators(features, condition, deviations, anomalies)
        indicators += self._electric_indicators(features, condition, deviations, anomalies)
        indicators += self._efficiency_indicators(features, condition, deviations, anomalies)
        if "dev.load_ratio" in features.values:
            deviations["dev.load_ratio"] = features.get("dev.load_ratio")
        score = self._fuse(indicators)
        patterns = self._match_patterns(features.values, deviations)
        trend, rate, remaining = self._update_trend(features.device_id, score, features.timestamp)
        grade = self._grade(score)
        if score < 0.75:
            anomalies.append("health score below nominal threshold")
        return HealthReport(
            device_id=features.device_id,
            condition_id=condition.condition_id,
            score=score,
            grade=grade,
            indicators=indicators,
            patterns=patterns,
            trend=trend,
            degradation_rate=rate,
            remaining_hours=remaining,
            anomalies=sorted(set(anomalies)),
            timestamp=features.timestamp,
            trace_id=features.trace_id,
        )

    def _indicator(
        self,
        features: FeatureVector,
        condition: ConditionSnapshot,
        feature: str,
        label: str,
        unit: str,
        weight: float,
        deviations: dict[str, float],
        anomalies: list[str],
        high_only: bool = False,
    ) -> list[HealthIndicator]:
        value = features.values.get(feature)
        if value is None:
            return []
        value = float(value)
        baseline = self._baseline_for(features.device_id, condition.condition_id, feature)
        if not baseline["ready"]:
            baseline["values"].append(value)
            return [HealthIndicator(feature, label, value, 1.0, unit=unit, weight=weight, remark="基线学习中")]
        spread = max(baseline["mad"], baseline["std"], abs(baseline["median"]) * 0.05, 1e-6)
        deviation = (value - baseline["median"]) / spread
        deviations[self._deviation_key(feature)] = deviation
        eff = max(deviation, 0.0) if high_only else deviation
        score = _clamp(1.0 - abs(eff) / 6.0)
        severity = "warning" if abs(eff) > 3.0 else "info"
        if abs(eff) > 4.5:
            severity = "alarm"
        if severity != "info":
            anomalies.append(f"{label} deviation {deviation:.2f}σ")
        ewma_key = f"{features.device_id}:{condition.condition_id}:{feature}"
        ewma = self._ewma.get(ewma_key, deviation)
        ewma = self.ewma_alpha * deviation + (1.0 - self.ewma_alpha) * ewma
        self._ewma[ewma_key] = ewma
        if abs(ewma) > self.ewma_sigma_k:
            anomalies.append(f"{label} EWMA deviation {ewma:.2f}σ")
        return [
            HealthIndicator(
                name=feature,
                label=label,
                value=value,
                score=score,
                unit=unit,
                baseline_low=baseline["median"] - 3 * spread,
                baseline_high=baseline["median"] + 3 * spread,
                deviation=deviation,
                weight=weight,
                severity=severity,
            )
        ]

    def _vibration_indicators(
        self,
        features: FeatureVector,
        condition: ConditionSnapshot,
        deviations: dict[str, float],
        anomalies: list[str],
    ) -> list[HealthIndicator]:
        out: list[HealthIndicator] = []
        vib_names = sorted(
            {
                key.rsplit(".", 1)[0]
                for key in features.values
                if key.endswith((".rms", ".kurtosis", ".crest_factor", ".spectral_entropy"))
            }
        )
        if not vib_names and "dev.vib_dev" in features.values:
            deviations["dev.vib_dev"] = features.get("dev.vib_dev")
        per_point = self.weights["vibration"] / max(len(vib_names), 1)
        for name in vib_names:
            out += self._indicator(features, condition, f"{name}.rms", f"{name} 振动有效值", "mm/s", per_point * 0.5, deviations, anomalies, high_only=True)
            out += self._indicator(features, condition, f"{name}.kurtosis", f"{name} 峭度", "", per_point * 0.2, deviations, anomalies, high_only=True)
            out += self._indicator(features, condition, f"{name}.crest_factor", f"{name} 峰值因子", "", per_point * 0.2, deviations, anomalies, high_only=True)
            out += self._indicator(features, condition, f"{name}.spectral_entropy", f"{name} 谱熵", "", per_point * 0.1, deviations, anomalies, high_only=True)
            for tag in ("bpfo", "bpfi", "bsf", "ftf", "1x", "2x"):
                key = f"{name}.{tag}_ratio"
                if key in features.values:
                    deviations[f"dev.{tag}_ratio"] = features.get(key)
        return out

    def _thermal_indicators(self, features: FeatureVector, condition: ConditionSnapshot, deviations: dict[str, float], anomalies: list[str]) -> list[HealthIndicator]:
        out: list[HealthIndicator] = []
        out += self._indicator(features, condition, "dev.temp_max", "最高温度", "℃", self.weights["temperature"] * 0.6, deviations, anomalies, high_only=True)
        out += self._indicator(features, condition, "dev.temp_rise", "温升", "K", self.weights["temperature"] * 0.4, deviations, anomalies, high_only=True)
        limit = features.values.get("dev.temp_alarm_c")
        temp = features.values.get("dev.temp_max")
        if limit is not None and temp is not None and temp > limit:
            anomalies.append(f"温度 {temp:.1f}℃ 超过报警限 {limit:.1f}℃")
            out.append(HealthIndicator("dev.temp_limit", "温度红线", float(temp), 0.0, unit="℃", weight=self.weights["temperature"], severity="fault"))
        return out

    def _electric_indicators(self, features: FeatureVector, condition: ConditionSnapshot, deviations: dict[str, float], anomalies: list[str]) -> list[HealthIndicator]:
        out: list[HealthIndicator] = []
        out += self._indicator(features, condition, "dev.current_ratio", "电流负载率", "", self.weights["current"] * 0.6, deviations, anomalies, high_only=True)
        unbalance = features.values.get("dev.current_unbalance")
        if unbalance is not None:
            score = _clamp(1.0 - float(unbalance) / 0.10)
            severity = "alarm" if unbalance > 0.10 else "warning" if unbalance > 0.05 else "info"
            if severity != "info":
                anomalies.append(f"三相电流不平衡度 {unbalance * 100:.1f}%")
            out.append(HealthIndicator("dev.current_unbalance", "三相电流不平衡度", float(unbalance), score, weight=self.weights["current"] * 0.4, severity=severity))
        return out

    def _efficiency_indicators(self, features: FeatureVector, condition: ConditionSnapshot, deviations: dict[str, float], anomalies: list[str]) -> list[HealthIndicator]:
        out: list[HealthIndicator] = []
        out += self._indicator(features, condition, "dev.specific_energy", "单位产出能耗", "kW·h/m³", self.weights["efficiency"] * 0.5, deviations, anomalies, high_only=True)
        out += self._indicator(features, condition, "dev.power_per_speed", "单位转速功率", "kW/rpm", self.weights["efficiency"] * 0.5, deviations, anomalies, high_only=True)
        if "dev.efficiency_dev" in deviations:
            deviations["dev.efficiency_dev"] = -deviations["dev.efficiency_dev"]
        return out

    def _baseline_for(self, device_id: str, condition_id: str, feature: str) -> dict[str, Any]:
        library = self._baselines.setdefault(f"{device_id}:{condition_id}:{feature}", [])
        if len(library) < self.baseline_min_samples:
            return {"values": library, "ready": False, "median": 0.0, "mad": 0.0, "std": 0.0}
        values = list(library)
        med = _median(values)
        mad = _median([abs(v - med) for v in values])
        std = pstdev(values) if len(values) > 1 else 0.0
        return {"values": library, "ready": True, "median": med, "mad": mad, "std": std}

    def _deviation_key(self, feature: str) -> str:
        mapping = {
            "dev.temp_max": "dev.temp_dev",
            "dev.temp_rise": "dev.temp_rise_dev",
            "dev.current_ratio": "dev.current_dev",
            "dev.specific_energy": "dev.efficiency_dev",
            "dev.power_per_speed": "dev.efficiency_dev",
        }
        if feature.endswith(".rms"):
            return "dev.vib_dev"
        if feature.endswith(".kurtosis"):
            return "dev.kurtosis_dev"
        if feature.endswith(".crest_factor"):
            return "dev.crest_dev"
        if feature.endswith(".spectral_entropy"):
            return "dev.spectral_entropy_dev"
        return mapping.get(feature, f"dev.{feature.split('.')[-1]}")

    def _fuse(self, indicators: Sequence[HealthIndicator]) -> float:
        if not indicators:
            return 1.0
        scores = [item.score for item in indicators]
        weights = [max(item.weight, 1e-6) for item in indicators]
        weighted = sum(s * w for s, w in zip(scores, weights)) / sum(weights)
        return _clamp(0.7 * weighted + 0.3 * min(scores))

    def _match_patterns(self, values: Mapping[str, float], deviations: Mapping[str, float]) -> list[tuple[str, float]]:
        merged = dict(values)
        merged.update(deviations)
        patterns: list[tuple[str, float]] = []
        rules = [
            ("unbalance", [("dev.1x_ratio", 3.0, ">"), ("dev.2x_ratio", 1.5, "<")], 0.75),
            ("misalignment", [("dev.2x_ratio", 2.0, ">"), ("dev.1x_ratio", 1.5, ">")], 0.72),
            ("looseness", [("dev.spectral_entropy_dev", 2.5, ">"), ("dev.crest_dev", 2.0, ">")], 0.65),
            ("bearing_outer", [("dev.bpfo_ratio", 4.0, ">")], 0.78),
            ("bearing_inner", [("dev.bpfi_ratio", 4.0, ">")], 0.78),
            ("bearing_roller", [("dev.bsf_ratio", 4.0, ">")], 0.70),
            ("overheat", [("dev.temp_dev", 3.0, ">")], 0.80),
            ("overload", [("dev.current_dev", 2.5, ">"), ("dev.load_ratio", 0.95, ">")], 0.70),
            ("fouling", [("dev.efficiency_dev", -2.0, "<"), ("dev.temp_dev", 1.5, ">")], 0.60),
        ]
        for name, conditions, confidence in rules:
            ok = True
            for key, threshold, op in conditions:
                value = merged.get(key)
                if value is None:
                    ok = False
                    break
                if op == ">" and not value > threshold:
                    ok = False
                    break
                if op == "<" and not value < threshold:
                    ok = False
                    break
            if ok:
                patterns.append((name, confidence))
        return patterns or [("normal", 1.0)]

    def _update_trend(self, device_id: str, score: float, timestamp: datetime) -> tuple[str, float, float | None]:
        hist = self._score_history.setdefault(device_id, [])
        hist.append((timestamp, score))
        cutoff = timestamp.timestamp() - self.trend_window_hours * 3600.0
        hist[:] = [item for item in hist if item[0].timestamp() >= cutoff]
        if len(hist) < 10:
            return "stable", 0.0, None
        xs = [(item[0] - hist[0][0]).total_seconds() / 86400.0 for item in hist]
        ys = [item[1] for item in hist]
        slope = _linear_slope(xs, ys)
        if slope < -0.005:
            trend = "worsening"
        elif slope > 0.005:
            trend = "improving"
        else:
            trend = "stable"
        remaining = None
        if trend == "worsening" and slope < 0:
            days = (score - 0.40) / abs(slope)
            if 0 < days < 3650:
                remaining = days * 24.0
        return trend, slope, remaining

    def _grade(self, score: float) -> str:
        if score >= 0.95:
            return "healthy"
        if score >= 0.80:
            return "good"
        if score >= 0.60:
            return "warning"
        return "critical"


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _column_mean(rows: Sequence[Sequence[float]]) -> list[float]:
    if not rows:
        return []
    width = len(rows[0])
    return [mean(row[i] for row in rows) for i in range(width)]


def _column_std(rows: Sequence[Sequence[float]]) -> list[float]:
    if not rows:
        return []
    width = len(rows[0])
    out: list[float] = []
    for i in range(width):
        values = [row[i] for row in rows]
        out.append(pstdev(values) if len(values) > 1 else 0.0)
    return out


def _linear_slope(xs: Sequence[float], ys: Sequence[float]) -> float:
    if len(xs) != len(ys) or len(xs) < 2:
        return 0.0
    x_mean = mean(xs)
    y_mean = mean(ys)
    denom = sum((x - x_mean) ** 2 for x in xs)
    if denom <= 1e-12:
        return 0.0
    numer = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    return numer / denom

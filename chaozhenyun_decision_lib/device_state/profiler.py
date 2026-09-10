"""设备状态与能力画像生成器。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from .analysis import ConditionRecognizer, FeatureVector, HealthAssessor
from .models import DeviceAvailabilityWindow, DeviceCapability, DeviceStateSnapshot


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    raise TypeError(f"unsupported time value: {value!r}")


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


@dataclass(frozen=True)
class DeviceStateBundle:
    """输出给调度/协作层的一组设备画像结果。"""

    state: DeviceStateSnapshot
    capability: DeviceCapability
    time_windows: tuple[DeviceAvailabilityWindow, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.to_dict(),
            "capability": self.capability.to_dict(),
            "time_windows": [window.to_dict() for window in self.time_windows],
        }


class DeviceStateProfiler:
    """根据工况、健康、日历与名义产能生成设备画像。"""

    def __init__(
        self,
        recognizer: ConditionRecognizer | None = None,
        assessor: HealthAssessor | None = None,
    ) -> None:
        self.recognizer = recognizer or ConditionRecognizer()
        self.assessor = assessor or HealthAssessor()

    def profile(self, payload: Mapping[str, Any]) -> DeviceStateBundle:
        device_id = str(payload["device_id"])
        timestamp = _parse_time(payload.get("timestamp", datetime.now(timezone.utc)))
        condition = dict(payload.get("condition", {}))
        health = dict(payload.get("health", {}))
        runtime = dict(payload.get("runtime", {}))
        calendar = list(payload.get("calendar", []))

        features_payload = payload.get("features")
        if isinstance(features_payload, Mapping):
            feature_vector = FeatureVector(
                device_id=device_id,
                values={str(k): float(v) for k, v in dict(features_payload).items() if isinstance(v, (int, float))},
                timestamp=timestamp,
                trace_id=str(payload.get("trace_id", "")),
                window_seconds=float(payload.get("window_seconds", 0.0) or 0.0),
            )
            condition_snapshot = self.recognizer.recognize(feature_vector)
            health_report = self.assessor.assess(feature_vector, condition_snapshot)
            condition.update(condition_snapshot.to_dict())
            health.update(health_report.to_dict())
        else:
            condition.setdefault("condition_id", payload.get("condition_id", ""))
            condition.setdefault("label", payload.get("condition_label", ""))
            condition.setdefault("stability", payload.get("stability", ""))
            health.setdefault("score", payload.get("health_score", 0.0))
            health.setdefault("grade", payload.get("health_grade", ""))

        health_score = float(health.get("score", 0.0) or 0.0)
        health_grade = str(health.get("grade", ""))
        stability = str(condition.get("stability", ""))
        status = self._derive_status(health_grade, runtime, condition)
        anomaly_flags = _as_tuple(health.get("anomalies", []))
        fault_patterns = _as_tuple(health.get("fault_patterns", []))

        state = DeviceStateSnapshot(
            device_id=device_id,
            timestamp=timestamp,
            status=status,
            condition_id=str(condition.get("condition_id", "")),
            condition_label=str(condition.get("label", "")),
            stability=stability,
            health_score=health_score,
            health_grade=health_grade,
            anomaly_flags=anomaly_flags,
            fault_patterns=fault_patterns,
            source=str(payload.get("source", "profiled")),
            note=str(payload.get("note", "")),
        )

        nominal_capacity = float(payload.get("nominal_capacity", runtime.get("nominal_capacity", 0.0)) or 0.0)
        capacity_factor = self._derive_capacity_factor(state, payload, health)
        effective_capacity = nominal_capacity * capacity_factor
        supported_operations = _as_tuple(payload.get("supported_operations", payload.get("available_operations", [])))
        risk_level = str(payload.get("risk_level", self._risk_from_state(state, health_score)))
        capability = DeviceCapability(
            device_id=device_id,
            timestamp=timestamp,
            available=status not in {"fault", "unavailable", "maintenance"},
            nominal_capacity=nominal_capacity,
            effective_capacity=effective_capacity,
            capacity_factor=capacity_factor,
            supported_operations=supported_operations,
            risk_level=risk_level,
            confidence=self._confidence_from_health(health_score, status),
            reason=self._capability_reason(state, health, payload),
        )

        windows = self._build_windows(device_id, calendar, timestamp, capacity_factor, state)
        return DeviceStateBundle(state=state, capability=capability, time_windows=tuple(windows))

    def _derive_status(self, health_grade: str, runtime: Mapping[str, Any], condition: Mapping[str, Any]) -> str:
        if runtime.get("emergency_stop") or runtime.get("fault_active"):
            return "fault"
        if runtime.get("maintenance") or condition.get("label") == "maintenance":
            return "maintenance"
        grade = health_grade.lower()
        if grade in {"critical", "alarm", "poor"}:
            return "fault"
        if grade in {"degraded", "warning"}:
            return "degraded"
        if condition.get("stability") in {"transient", "unstable"}:
            return "transient"
        return "normal"

    def _derive_capacity_factor(self, state: DeviceStateSnapshot, payload: Mapping[str, Any], health: Mapping[str, Any]) -> float:
        nominal = float(payload.get("capacity_factor", 1.0) or 1.0)
        health_score = float(health.get("score", 100.0) or 100.0)
        if health_score > 1.0:
            health_score /= 100.0
        health_score = max(0.0, min(1.0, health_score))
        factor = nominal * health_score
        if state.status == "degraded":
            factor *= 0.8
        elif state.status in {"fault", "maintenance", "unavailable"}:
            factor = 0.0
        return max(0.0, min(1.0, factor))

    def _risk_from_state(self, state: DeviceStateSnapshot, health_score: float) -> str:
        if state.status in {"fault", "maintenance"}:
            return "high"
        if state.status == "degraded" or health_score < 0.75:
            return "medium"
        return "low"

    def _confidence_from_health(self, health_score: float, status: str) -> float:
        score = health_score if health_score <= 1.0 else health_score / 100.0
        score = max(0.0, min(1.0, score))
        if status in {"fault", "maintenance"}:
            return max(0.5, score)
        return max(0.7, score)

    def _capability_reason(self, state: DeviceStateSnapshot, health: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
        if state.status == "fault":
            return "health or runtime indicates fault"
        if state.status == "maintenance":
            return "device is in maintenance window"
        if state.status == "degraded":
            return "capacity reduced by health degradation"
        if payload.get("available_operations"):
            return "enabled for declared operations"
        return "available by current health profile"

    def _build_windows(
        self,
        device_id: str,
        calendar: list[Any],
        timestamp: datetime,
        capacity_factor: float,
        state: DeviceStateSnapshot,
    ) -> list[DeviceAvailabilityWindow]:
        windows: list[DeviceAvailabilityWindow] = []
        for item in calendar:
            if isinstance(item, Mapping):
                start = _parse_time(item["start"])
                end = _parse_time(item["end"])
                windows.append(
                    DeviceAvailabilityWindow(
                        device_id=device_id,
                        start=start,
                        end=end,
                        kind=str(item.get("kind", "available")),
                        capacity_factor=float(item.get("capacity_factor", capacity_factor)),
                        reason=str(item.get("reason", "")),
                    )
                )
        if not windows:
            far_end = timestamp + timedelta(days=365)
            windows.append(
                DeviceAvailabilityWindow(
                    device_id=device_id,
                    start=timestamp,
                    end=far_end,
                    kind="available" if state.status not in {"fault", "maintenance"} else state.status,
                    capacity_factor=capacity_factor,
                    reason="derived from current state",
                )
            )
        return windows


def build_device_state_bundle(payload: Mapping[str, Any]) -> dict[str, Any]:
    """便捷函数：直接返回可序列化的设备状态包。"""

    return DeviceStateProfiler().profile(payload).to_dict()

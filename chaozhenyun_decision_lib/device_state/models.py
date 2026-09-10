"""设备状态、能力与时间窗口模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable


@dataclass(frozen=True)
class DeviceStateSnapshot:
    """设备状态快照。"""

    device_id: str
    timestamp: datetime
    status: str
    condition_id: str = ""
    condition_label: str = ""
    stability: str = ""
    health_score: float = 0.0
    health_grade: str = ""
    anomaly_flags: tuple[str, ...] = field(default_factory=tuple)
    fault_patterns: tuple[str, ...] = field(default_factory=tuple)
    source: str = "profiled"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "timestamp": self.timestamp.isoformat(),
            "status": self.status,
            "condition_id": self.condition_id,
            "condition_label": self.condition_label,
            "stability": self.stability,
            "health_score": round(self.health_score, 4),
            "health_grade": self.health_grade,
            "anomaly_flags": list(self.anomaly_flags),
            "fault_patterns": list(self.fault_patterns),
            "source": self.source,
            "note": self.note,
        }


@dataclass(frozen=True)
class DeviceCapability:
    """设备能力画像。"""

    device_id: str
    timestamp: datetime
    available: bool
    nominal_capacity: float
    effective_capacity: float
    capacity_factor: float
    supported_operations: tuple[str, ...] = field(default_factory=tuple)
    risk_level: str = "low"
    confidence: float = 1.0
    reason: str = ""

    def supports(self, operation_id: str) -> bool:
        return self.available and (not self.supported_operations or operation_id in self.supported_operations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "timestamp": self.timestamp.isoformat(),
            "available": self.available,
            "nominal_capacity": round(self.nominal_capacity, 4),
            "effective_capacity": round(self.effective_capacity, 4),
            "capacity_factor": round(self.capacity_factor, 4),
            "supported_operations": list(self.supported_operations),
            "risk_level": self.risk_level,
            "confidence": round(self.confidence, 4),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class DeviceAvailabilityWindow:
    """设备可用时间窗口。"""

    device_id: str
    start: datetime
    end: datetime
    kind: str = "available"
    capacity_factor: float = 1.0
    reason: str = ""

    def contains(self, start: datetime, end: datetime) -> bool:
        return self.start <= start and end <= self.end

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "kind": self.kind,
            "capacity_factor": round(self.capacity_factor, 4),
            "reason": self.reason,
        }

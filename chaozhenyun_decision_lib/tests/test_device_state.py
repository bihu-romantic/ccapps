"""设备状态画像测试。"""

from chaozhenyun_decision_lib.device_state.profiler import DeviceStateProfiler


def test_device_state_reports_capacity_and_windows() -> None:
    result = DeviceStateProfiler().profile({
        "device_id": "EQ-01",
        "timestamp": "2026-08-07T08:00:00+08:00",
        "condition": {"condition_id": "cond-a", "label": "稳定运行", "stability": "steady"},
        "health": {"score": 0.92, "grade": "good", "anomalies": ["vibration"], "fault_patterns": []},
        "nominal_capacity": 8,
        "available_operations": ["OP-10", "OP-20"],
        "calendar": [{"start": "2026-08-07T08:00:00+08:00", "end": "2026-08-07T20:00:00+08:00", "kind": "available"}],
    }).to_dict()
    assert result["state"]["status"] == "normal"
    assert result["capability"]["available"] is True
    assert result["capability"]["effective_capacity"] > 7.0
    assert result["time_windows"][0]["kind"] == "available"


def test_fault_device_is_marked_unavailable() -> None:
    result = DeviceStateProfiler().profile({
        "device_id": "EQ-02",
        "health": {"score": 0.2, "grade": "critical", "anomalies": [], "fault_patterns": ["bearing"]},
        "runtime": {"emergency_stop": True},
    }).to_dict()
    assert result["state"]["status"] == "fault"
    assert result["capability"]["available"] is False
    assert result["capability"]["effective_capacity"] == 0.0

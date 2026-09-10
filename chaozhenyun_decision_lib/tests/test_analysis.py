"""独立工况与健康逻辑测试。"""

from chaozhenyun_decision_lib.device_state.analysis import (
    ConditionRecognizer,
    FeatureVector,
    HealthAssessor,
    bearing_frequencies,
    spectral_features,
    time_domain_features,
)


def test_condition_recognition_creates_stable_snapshot() -> None:
    recognizer = ConditionRecognizer()
    vector = FeatureVector(
        device_id="EQ-01",
        values={"dev.load_ratio": 0.8, "dev.speed_ratio": 0.75, "dev.current_ratio": 0.82, "dev.temp_mean": 62.0},
    )
    snapshot = recognizer.recognize(vector)
    assert snapshot.device_id == "EQ-01"
    assert snapshot.condition_id.startswith("EQ-01-C")
    assert snapshot.label


def test_health_assessor_returns_report() -> None:
    recognizer = ConditionRecognizer()
    assessor = HealthAssessor()
    vector = FeatureVector(
        device_id="EQ-01",
        values={
            "dev.load_ratio": 0.8,
            "dev.speed_ratio": 0.75,
            "dev.current_ratio": 0.82,
            "dev.temp_mean": 62.0,
            "dev.temp_max": 68.0,
            "dev.temp_rise": 4.0,
            "dev.current_ratio": 0.9,
            "dev.specific_energy": 1.2,
        },
    )
    condition = recognizer.recognize(vector)
    report = assessor.assess(vector, condition)
    assert report.device_id == "EQ-01"
    assert 0.0 <= report.score <= 1.0
    assert report.patterns


def test_waveform_feature_helpers_return_time_and_frequency_values() -> None:
    waveform = [0.0, 1.0, 0.0, -1.0] * 16
    time_features = time_domain_features(waveform)
    freq_features = spectral_features(waveform, sampling_rate=64.0)
    bearing = bearing_frequencies(shaft_hz=30.0, n_rollers=8, roller_d=8.0, pitch_d=40.0)
    assert time_features["rms"] > 0.0
    assert freq_features["dominant_amp"] > 0.0
    assert bearing["bpfo"] > 0.0


def test_health_assessor_detects_complex_fault_patterns_after_baseline() -> None:
    recognizer = ConditionRecognizer()
    assessor = HealthAssessor(baseline_min_samples=3)
    base_values = {
        "dev.load_ratio": 0.98,
        "dev.speed_ratio": 0.9,
        "dev.current_ratio": 0.7,
        "dev.temp_mean": 60.0,
        "dev.temp_max": 62.0,
        "dev.temp_rise": 3.0,
        "dev.specific_energy": 1.0,
        "vib_de.rms": 1.2,
        "vib_de.crest_factor": 2.0,
        "vib_de.spectral_entropy": 0.2,
    }
    for _ in range(4):
        vector = FeatureVector(device_id="EQ-02", values=base_values)
        assessor.assess(vector, recognizer.recognize(vector))

    faulty = {
        **base_values,
        "dev.current_ratio": 1.0,
        "dev.temp_max": 80.0,
        "vib_de.rms": 6.0,
        "vib_de.bpfo_ratio": 5.0,
        "vib_de.crest_factor": 8.0,
        "vib_de.spectral_entropy": 3.0,
    }
    vector = FeatureVector(device_id="EQ-02", values=faulty)
    report = assessor.assess(vector, recognizer.recognize(vector))
    pattern_names = {name for name, _ in report.patterns}
    assert "bearing_outer" in pattern_names
    assert report.score < 1.0
    assert report.anomalies

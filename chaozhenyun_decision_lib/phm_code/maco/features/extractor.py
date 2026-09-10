"""
特征提取模块
============

把原始时序转换成可用于工况识别、健康评估与优化建模的特征向量，包含三类：

* **时域特征** —— 有效值、峰值、峭度、裕度因子等，反映振动能量与冲击性；
* **频域特征** —— 频谱主频、分频带能量、包络谱，配合轴承特征频率定位故障源；
* **热电与能效特征** —— 温升、电流不平衡度、单位产出能耗，反映运行经济性。

特征名统一为 ``<测点或域>.<特征>``（如 ``vib_de.rms``、``plant.energy_ratio``），
下游按名取用，新增特征不影响既有模块。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from ..core.enums import SignalKind
from ..core.logging_setup import get_logger
from ..core.registry import DeviceProfile
from ..core.types import FeatureVector, SampleFrame, now_ts
from ..core.utils import safe_div
from ..acquisition.buffer import TimeSeriesBuffer

logger = get_logger("features.extractor")


# ==========================================================================
# 时域特征
# ==========================================================================
def time_domain_features(signal: Sequence[float]) -> Dict[str, float]:
    """计算时域统计特征。输入为一段波形或一段时序。"""
    arr = np.asarray([v for v in signal if math.isfinite(v)], dtype=float)
    if arr.size < 2:
        return {}
    abs_arr = np.abs(arr)
    mean = float(np.mean(arr))
    rms = float(np.sqrt(np.mean(arr ** 2)))
    peak = float(np.max(abs_arr))
    std = float(np.std(arr, ddof=1))
    # 方根幅值：裕度因子的分母
    sra = float(np.mean(np.sqrt(abs_arr)) ** 2)
    mean_abs = float(np.mean(abs_arr))
    return {
        "mean": mean,
        "std": std,
        "rms": rms,
        "peak": peak,
        "peak2peak": float(np.max(arr) - np.min(arr)),
        # 峭度：反映冲击成分，正常约 3，冲击性故障显著升高
        "kurtosis": _kurtosis(arr, mean, std),
        "skewness": _skewness(arr, mean, std),
        # 波形因子 = 有效值/整流平均值
        "shape_factor": safe_div(rms, mean_abs),
        # 峰值因子 = 峰值/有效值，早期冲击敏感
        "crest_factor": safe_div(peak, rms),
        # 裕度因子 = 峰值/方根幅值
        "clearance_factor": safe_div(peak, sra),
        # 脉冲因子 = 峰值/整流平均值
        "impulse_factor": safe_div(peak, mean_abs),
        "cv": abs(safe_div(std, mean)),
    }


def _kurtosis(arr: np.ndarray, mean: float, std: float) -> float:
    if std < 1e-12:
        return 0.0
    return float(np.mean(((arr - mean) / std) ** 4))


def _skewness(arr: np.ndarray, mean: float, std: float) -> float:
    if std < 1e-12:
        return 0.0
    return float(np.mean(((arr - mean) / std) ** 3))


# ==========================================================================
# 频域特征
# ==========================================================================
def spectrum(signal: Sequence[float], sampling_rate: float) -> Dict[str, np.ndarray]:
    """计算单边幅值谱。"""
    arr = np.asarray([v for v in signal if math.isfinite(v)], dtype=float)
    n = arr.size
    if n < 8 or sampling_rate <= 0:
        return {"freq": np.array([]), "amp": np.array([])}
    arr = arr - float(np.mean(arr))
    # 汉宁窗抑制频谱泄漏，并做幅值修正
    window = np.hanning(n)
    spec = np.fft.rfft(arr * window)
    amp = np.abs(spec) * 2.0 / (np.sum(window) or 1.0)
    freq = np.fft.rfftfreq(n, d=1.0 / sampling_rate)
    return {"freq": freq, "amp": amp}


def band_energy(freq: np.ndarray, amp: np.ndarray,
                bands: Sequence[Sequence[float]]) -> Dict[str, float]:
    """分频带能量占比。"""
    if freq.size == 0:
        return {}
    total = float(np.sum(amp ** 2)) or 1.0
    out: Dict[str, float] = {}
    for low, high in bands:
        mask = (freq >= low) & (freq < high)
        out[f"band_{int(low)}_{int(high)}"] = float(np.sum(amp[mask] ** 2) / total)
    return out


def envelope_spectrum(signal: Sequence[float], sampling_rate: float) -> Dict[str, np.ndarray]:
    """包络谱：先取绝对值做低通近似解调，再做频谱。

    对滚动轴承的周期性冲击最敏感，是定位轴承故障的常用手段。
    """
    arr = np.asarray([v for v in signal if math.isfinite(v)], dtype=float)
    if arr.size < 16:
        return {"freq": np.array([]), "amp": np.array([])}
    envelope = np.abs(arr - float(np.mean(arr)))
    # 简单滑动平均作为低通
    win = max(int(sampling_rate / 2000.0), 3) if sampling_rate > 0 else 3
    kernel = np.ones(win) / win
    smooth = np.convolve(envelope, kernel, mode="same")
    return spectrum(smooth, sampling_rate)


def bearing_frequencies(shaft_hz: float, n_rollers: int, roller_d: float,
                        pitch_d: float, contact_angle_deg: float = 0.0) -> Dict[str, float]:
    """轴承四大特征频率（BPFO/BPFI/BSF/FTF）。

    这是知识驱动的机理部分：不靠数据学习，直接由几何参数算出来，
    用于在包络谱中定点核对，显著降低误报。
    """
    if shaft_hz <= 0 or n_rollers <= 0 or pitch_d <= 0:
        return {}
    ratio = (roller_d / pitch_d) * math.cos(math.radians(contact_angle_deg))
    return {
        "bpfo": n_rollers / 2.0 * shaft_hz * (1 - ratio),   # 外圈
        "bpfi": n_rollers / 2.0 * shaft_hz * (1 + ratio),   # 内圈
        "bsf": pitch_d / (2.0 * roller_d) * shaft_hz * (1 - ratio ** 2),  # 滚动体
        "ftf": shaft_hz / 2.0 * (1 - ratio),                # 保持架
    }


def peak_at(freq: np.ndarray, amp: np.ndarray, target_hz: float,
            tolerance_ratio: float = 0.03) -> float:
    """取目标频率附近的谱峰幅值。"""
    if freq.size == 0 or target_hz <= 0:
        return 0.0
    tol = max(target_hz * tolerance_ratio, freq[1] - freq[0] if freq.size > 1 else 1.0)
    mask = (freq >= target_hz - tol) & (freq <= target_hz + tol)
    return float(np.max(amp[mask])) if np.any(mask) else 0.0


def spectral_features(signal: Sequence[float], sampling_rate: float,
                      bands: Sequence[Sequence[float]]) -> Dict[str, float]:
    """频域综合特征。"""
    sp = spectrum(signal, sampling_rate)
    freq, amp = sp["freq"], sp["amp"]
    if freq.size == 0:
        return {}
    total = float(np.sum(amp)) or 1.0
    centroid = float(np.sum(freq * amp) / total)
    out: Dict[str, float] = {
        "dominant_hz": float(freq[int(np.argmax(amp))]),
        "dominant_amp": float(np.max(amp)),
        "centroid_hz": centroid,
        # 谱熵：越大频率成分越分散，常与松动、摩擦相关
        "spectral_entropy": _spectral_entropy(amp),
        "rms_spectrum": float(np.sqrt(np.mean(amp ** 2))),
    }
    out.update(band_energy(freq, amp, bands))
    return out


def _spectral_entropy(amp: np.ndarray) -> float:
    power = amp ** 2
    total = float(np.sum(power))
    if total <= 1e-18:
        return 0.0
    p = power / total
    p = p[p > 1e-15]
    return float(-np.sum(p * np.log(p)) / math.log(len(p) or 2))


# ==========================================================================
# 特征提取器
# ==========================================================================
class FeatureExtractor:
    """按设备测点表提取完整特征向量。"""

    def __init__(self, buffer: TimeSeriesBuffer, window_seconds: float = 10.0,
                 bands: Optional[Sequence[Sequence[float]]] = None,
                 enable_spectrum: bool = True, enable_envelope: bool = True):
        self.buffer = buffer
        self.window_seconds = window_seconds
        self.bands = bands or [[0, 10], [10, 100], [100, 500], [500, 2000], [2000, 10000]]
        self.enable_spectrum = enable_spectrum
        self.enable_envelope = enable_envelope

    def extract(self, profile: DeviceProfile,
                frame: Optional[SampleFrame] = None) -> FeatureVector:
        """提取一台设备当前的特征向量。"""
        device_id = profile.device_id
        values: Dict[str, float] = {}

        for point in profile.points.values():
            prefix = point.point_id
            if point.kind.is_dynamic:
                values.update(self._dynamic_features(profile, point, prefix, frame))
            else:
                values.update(self._static_features(device_id, point, prefix))

        values.update(self._derived_features(profile, values, frame))
        return FeatureVector(
            device_id=device_id,
            values={k: v for k, v in values.items() if math.isfinite(v)},
            timestamp=now_ts(),
            trace_id=frame.trace_id if frame else "",
            window_seconds=self.window_seconds,
        )

    # ------------------------------------------------------------------
    def _static_features(self, device_id: str, point: Any, prefix: str) -> Dict[str, float]:
        """标量测点：取窗口统计。"""
        window = self.buffer.window(device_id, point.point_id, self.window_seconds)
        if window.empty:
            return {}
        stats = window.stats()
        out = {
            f"{prefix}.mean": stats["mean"],
            f"{prefix}.std": stats["std"],
            f"{prefix}.min": stats["min"],
            f"{prefix}.max": stats["max"],
            f"{prefix}.median": stats["median"],
            f"{prefix}.cv": stats["cv"],
            f"{prefix}.last": window.values[-1],
        }
        if len(window) >= 3:
            from ..core.utils import linear_regression
            fit = linear_regression(window.timestamps, window.values)
            out[f"{prefix}.slope"] = fit["slope"]
        return out

    def _dynamic_features(self, profile: DeviceProfile, point: Any, prefix: str,
                          frame: Optional[SampleFrame]) -> Dict[str, float]:
        """高频测点：时域 + 频域 + 包络。"""
        waveform: Optional[List[float]] = None
        if frame is not None:
            sample = frame.get(point.point_id)
            if sample is not None and sample.waveform is not None:
                waveform = list(sample.waveform)
        if waveform is None:
            waveform = self.buffer.waveform(profile.device_id, point.point_id)
        if not waveform:
            # 无整段波形时退化为窗口时序，仍可算时域特征
            window = self.buffer.window(profile.device_id, point.point_id,
                                        self.window_seconds)
            if window.empty:
                return {}
            waveform = window.values

        out = {f"{prefix}.{k}": v for k, v in time_domain_features(waveform).items()}
        fs = point.sampling_rate_hz or 0.0
        if self.enable_spectrum and fs > 0:
            out.update({f"{prefix}.{k}": v
                        for k, v in spectral_features(waveform, fs, self.bands).items()})
            if self.enable_envelope:
                out.update(self._bearing_features(profile, point, prefix, waveform, fs))
        return out

    def _bearing_features(self, profile: DeviceProfile, point: Any, prefix: str,
                          waveform: Sequence[float], fs: float) -> Dict[str, float]:
        """在包络谱上核对轴承特征频率，输出各故障频率的相对幅值。"""
        speed_point = profile.first_point_of(SignalKind.SPEED)
        rpm = None
        if speed_point is not None:
            rpm = self.buffer.latest(profile.device_id, speed_point.point_id)
        if rpm is None:
            rpm = profile.nameplate_of("rated_speed_rpm", 0.0)
        if not rpm or rpm <= 0:
            return {}
        shaft_hz = rpm / 60.0
        mech = profile.mechanics
        freqs = bearing_frequencies(
            shaft_hz=shaft_hz,
            n_rollers=int(mech.get("n_rollers", 0)),
            roller_d=float(mech.get("roller_diameter", 0.0)),
            pitch_d=float(mech.get("pitch_diameter", 0.0)),
            contact_angle_deg=float(mech.get("contact_angle", 0.0)),
        )
        if not freqs:
            return {}
        env = envelope_spectrum(waveform, fs)
        e_freq, e_amp = env["freq"], env["amp"]
        if e_freq.size == 0:
            return {}
        base = float(np.mean(e_amp)) or 1.0
        out = {f"{prefix}.shaft_hz": shaft_hz}
        for name, hz in freqs.items():
            out[f"{prefix}.{name}_ratio"] = peak_at(e_freq, e_amp, hz) / base
        return out

    def _derived_features(self, profile: DeviceProfile, values: Dict[str, float],
                          frame: Optional[SampleFrame]) -> Dict[str, float]:
        """跨测点派生特征：负载率、温升、电流不平衡度、单位能耗。"""
        out: Dict[str, float] = {}
        device_id = profile.device_id

        def latest_of(kind: SignalKind) -> Optional[float]:
            p = profile.first_point_of(kind)
            return self.buffer.latest(device_id, p.point_id) if p else None

        power = latest_of(SignalKind.POWER)
        rated_power = profile.nameplate_of("rated_power_kw")
        if power is not None and rated_power > 0:
            out["dev.load_ratio"] = power / rated_power

        speed = latest_of(SignalKind.SPEED)
        rated_speed = profile.nameplate_of("rated_speed_rpm")
        if speed is not None and rated_speed > 0:
            out["dev.speed_ratio"] = speed / rated_speed

        current = latest_of(SignalKind.CURRENT)
        rated_current = profile.nameplate_of("rated_current_a")
        if current is not None and rated_current > 0:
            out["dev.current_ratio"] = current / rated_current

        # 温升 = 设备温度 - 环境温度
        temps = profile.points_of_kind(SignalKind.TEMPERATURE)
        ambient = None
        body_temps: List[float] = []
        for tp in temps:
            v = self.buffer.latest(device_id, tp.point_id)
            if v is None:
                continue
            if "ambient" in tp.point_id or "环境" in tp.name:
                ambient = v
            else:
                body_temps.append(v)
        if body_temps:
            out["dev.temp_max"] = max(body_temps)
            out["dev.temp_mean"] = sum(body_temps) / len(body_temps)
            if ambient is not None:
                out["dev.temp_rise"] = max(body_temps) - ambient

        # 三相电流不平衡度
        currents = [self.buffer.latest(device_id, p.point_id)
                    for p in profile.points_of_kind(SignalKind.CURRENT)]
        currents = [c for c in currents if c is not None]
        if len(currents) >= 3:
            avg = sum(currents) / len(currents)
            if avg > 1e-6:
                out["dev.current_unbalance"] = max(abs(c - avg) for c in currents) / avg

        # 单位产出能耗（能效）
        flow = latest_of(SignalKind.FLOW)
        if power is not None and flow is not None and flow > 1e-6:
            out["dev.specific_energy"] = power / flow
        if power is not None and speed is not None and speed > 1e-6:
            out["dev.power_per_speed"] = power / speed

        return out


__all__ = [
    "FeatureExtractor",
    "time_domain_features",
    "spectral_features",
    "spectrum",
    "envelope_spectrum",
    "bearing_frequencies",
    "band_energy",
    "peak_at",
]

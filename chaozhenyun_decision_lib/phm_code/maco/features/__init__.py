"""
特征层（features）
==================

把原始时序转成特征向量，供工况识别、健康评估与优化建模共用。
时域 / 频域 / 热电能效三类特征在 :mod:`~maco.features.extractor` 中实现。
"""

from .extractor import (
    FeatureExtractor,
    band_energy,
    bearing_frequencies,
    envelope_spectrum,
    peak_at,
    spectral_features,
    spectrum,
    time_domain_features,
)

__all__ = [
    "FeatureExtractor", "time_domain_features", "spectral_features",
    "spectrum", "envelope_spectrum", "bearing_frequencies", "band_energy", "peak_at",
]

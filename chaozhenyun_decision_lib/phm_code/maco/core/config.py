"""
配置管理模块
============

提供分层配置能力：默认值 → 配置文件 → 环境变量 → 运行期覆盖，
后者依次覆盖前者。配置项通过点号路径访问（如 ``control.mode``），
并在装载时完成类型与取值范围校验，避免错误配置进入运行期才暴露。

安全约定：任何密钥类配置（``*_token`` / ``*_password`` / ``*_secret``）
在 :meth:`Config.dump` 与日志输出中一律脱敏，不落盘、不打印。
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from .errors import ConfigurationError

#: 需要脱敏的配置键模式
_SECRET_PATTERN = re.compile(r"(token|password|secret|apikey|api_key|credential)", re.I)

#: 环境变量前缀，例如 MACO_CONTROL__MODE=autonomous
_ENV_PREFIX = "MACO_"


DEFAULT_CONFIG: Dict[str, Any] = {
    "system": {
        "name": "多工况自适应工业设备控制优化系统",
        "version": "1.0.0",
        "instance_id": "maco-node-1",
        "timezone": "Asia/Shanghai",
        "log_level": "INFO",
        "log_dir": "logs",
    },
    "acquisition": {
        # 采集主循环周期（毫秒）
        "poll_interval_ms": 1000,
        # 单次读取超时
        "read_timeout_ms": 2000,
        # 通信失败重试次数与退避基数
        "retry_times": 3,
        "retry_backoff_ms": 200,
        # 数据被判定为陈旧的阈值
        "stale_threshold_s": 10.0,
        # 环形缓冲保留时长（秒）
        "buffer_seconds": 1800.0,
        # 清洗开关
        "cleansing": {
            "range_check": True,
            "dead_value_check": True,
            "dead_value_window": 30,
            "spike_check": True,
            "spike_sigma": 5.0,
            "interpolate_gap": True,
            "max_interpolate_points": 3,
        },
    },
    "features": {
        # 特征提取窗口（秒）
        "window_seconds": 10.0,
        # 频域分析开关与频带划分
        "enable_spectrum": True,
        "spectrum_bands": [[0, 10], [10, 100], [100, 500], [500, 2000], [2000, 10000]],
        "envelope_analysis": True,
    },
    "condition": {
        # 工况识别方式：rule / cluster / hybrid
        "mode": "hybrid",
        # 在线聚类新工况判定阈值（马氏距离）
        "novelty_threshold": 3.5,
        # 最大工况数
        "max_conditions": 32,
        # 稳态判定：窗口内变异系数上限
        "steady_cv_threshold": 0.05,
        # 稳态最短持续时间（秒）
        "steady_min_dwell_s": 60.0,
        # 观测充分性的另一条判据：稳态判定所需的最少采样条数
        "steady_min_samples": 12,
        # 稳态判定窗口保留的最大条数
        "window_points": 30,
        # 工况切换去抖动次数
        "debounce_count": 3,
    },
    "health": {
        # 基线学习所需最少样本
        "baseline_min_samples": 50,
        # 异常判定 EWMA 参数
        "ewma_alpha": 0.2,
        "ewma_sigma_k": 3.0,
        # 各项指标的融合权重
        "weights": {
            "vibration": 0.35,
            "temperature": 0.25,
            "current": 0.20,
            "efficiency": 0.20,
        },
        # 退化趋势拟合窗口（小时）
        "trend_window_hours": 72.0,
        # 触发劣化预警的健康分阈值
        "attention_threshold": 0.85,
        "degraded_threshold": 0.65,
        "critical_threshold": 0.40,
    },
    "knowledge": {
        "rule_file": "config/rules.json",
        "case_file": "data/cases.json",
        # 推理最大迭代轮次，防止规则互相触发导致死循环
        "max_inference_rounds": 8,
        # 案例检索返回条数与最低相似度
        "case_top_k": 5,
        "case_min_similarity": 0.65,
        # 新案例初始可信度
        "case_initial_credibility": 0.5,
    },
    "optimization": {
        # 算法：bayesian / nsga2 / pso / auto
        "algorithm": "auto",
        "max_iterations": 60,
        "population_size": 24,
        # 代理模型最少训练样本，不足时退化为知识规则寻优
        "surrogate_min_samples": 12,
        # 单次建议允许调整的参数个数上限
        "max_changed_params": 3,
        # 建议置信度低于此值不进入控制链路
        "min_confidence": 0.55,
        # 探索比例（用于在线学习的试探）
        "exploration_ratio": 0.15,
        "objective_weights": {
            "energy": 0.45,
            "throughput": 0.30,
            "health": 0.25,
        },
    },
    "control": {
        # observe / advisory / supervised / autonomous
        "mode": "advisory",
        # 单次调整的最大相对变化
        "max_delta_ratio": 0.10,
        # 相邻两次下发的最小间隔（秒）
        "min_dispatch_interval_s": 300.0,
        # 每小时最大下发次数
        "max_dispatch_per_hour": 6,
        # 回读校验容差（相对）
        "readback_tolerance": 0.02,
        "readback_timeout_s": 10.0,
        # 回读不一致时是否自动回滚
        "auto_rollback": True,
        # 健康等级低于该级别时禁止自动下发
        "block_when_grade": "degraded",
        # 需要人工审批的风险等级
        "approval_required_above": "warning",
    },
    "feedback": {
        # 观测窗：下发前/后各取多少秒的数据做对比
        "before_seconds": 600.0,
        "after_seconds": 600.0,
        # 参数生效等待时间
        "settle_seconds": 120.0,
        # 显著性判定阈值（|t| 上限）
        "t_threshold": 2.0,
        # 判定建议被采纳的最小奖励
        "accept_reward": 0.0,
        # 在线学习率
        "learning_rate": 0.15,
        # 规则置信度更新步长
        "rule_credit_step": 0.05,
        # LinUCB 探索系数
        "ucb_alpha": 0.8,
    },
    "storage": {
        "root": "data/store",
        "timeseries_shard_seconds": 3600,
        "retain_days": 30,
        "audit_file": "data/store/audit.jsonl",
    },
    "api": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 8780,
        # 只读令牌，从环境变量注入，不写入配置文件
        "access_token": "",
    },
}


def _deep_merge(base: Dict[str, Any], overlay: Mapping[str, Any]) -> Dict[str, Any]:
    """递归合并字典，overlay 覆盖 base。"""
    result = dict(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, Mapping):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _coerce(text: str) -> Any:
    """把环境变量字符串转成合适的 Python 类型。"""
    lowered = text.strip().lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "none", ""):
        return None
    try:
        if re.fullmatch(r"[+-]?\d+", text.strip()):
            return int(text)
        return float(text)
    except (TypeError, ValueError):
        pass
    if text.strip().startswith(("[", "{")):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return text


class Config:
    """分层配置容器。"""

    def __init__(self, data: Optional[Mapping[str, Any]] = None):
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = _deep_merge(DEFAULT_CONFIG, data or {})
        self._validators: List[Tuple[str, Callable[[Any], bool], str]] = []
        self._install_default_validators()

    # ------------------------------------------------------------------
    # 构造入口
    # ------------------------------------------------------------------
    @classmethod
    def load(
        cls,
        path: Optional[str] = None,
        overrides: Optional[Mapping[str, Any]] = None,
        use_env: bool = True,
    ) -> "Config":
        """按 默认值 → 文件 → 环境变量 → 显式覆盖 的顺序装载配置。"""
        data: Dict[str, Any] = {}
        if path:
            file_path = Path(path)
            if not file_path.exists():
                raise ConfigurationError(f"配置文件不存在: {path}", {"path": path})
            try:
                raw = json.loads(file_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ConfigurationError(
                    f"配置文件不是合法 JSON: {exc}", {"path": path}
                ) from exc
            if not isinstance(raw, dict):
                raise ConfigurationError("配置文件顶层必须是对象", {"path": path})
            data = _deep_merge(data, raw)
        if use_env:
            data = _deep_merge(data, cls._read_env())
        if overrides:
            data = _deep_merge(data, overrides)
        cfg = cls(data)
        cfg.validate()
        return cfg

    @staticmethod
    def _read_env() -> Dict[str, Any]:
        """读取 ``MACO_SECTION__KEY`` 形式的环境变量。"""
        out: Dict[str, Any] = {}
        for raw_key, raw_value in os.environ.items():
            if not raw_key.startswith(_ENV_PREFIX):
                continue
            path = raw_key[len(_ENV_PREFIX):].lower().split("__")
            if not path or not path[0]:
                continue
            cursor = out
            for part in path[:-1]:
                cursor = cursor.setdefault(part, {})
                if not isinstance(cursor, dict):
                    break
            else:
                cursor[path[-1]] = _coerce(raw_value)
        return out

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------
    def get(self, path: str, default: Any = None) -> Any:
        """按点号路径读取配置，如 ``config.get("control.mode")``。"""
        with self._lock:
            cursor: Any = self._data
            for part in path.split("."):
                if not isinstance(cursor, dict) or part not in cursor:
                    return default
                cursor = cursor[part]
            return copy.deepcopy(cursor) if isinstance(cursor, (dict, list)) else cursor

    def require(self, path: str) -> Any:
        """读取必填配置，缺失时抛出 :class:`ConfigurationError`。"""
        sentinel = object()
        value = self.get(path, sentinel)
        if value is sentinel or value is None:
            raise ConfigurationError(f"缺少必需配置项: {path}", {"path": path})
        return value

    def get_float(self, path: str, default: float = 0.0) -> float:
        try:
            return float(self.get(path, default))
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"配置项 {path} 不是数值", {"path": path}) from exc

    def get_int(self, path: str, default: int = 0) -> int:
        try:
            return int(self.get(path, default))
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"配置项 {path} 不是整数", {"path": path}) from exc

    def get_bool(self, path: str, default: bool = False) -> bool:
        value = self.get(path, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    def set(self, path: str, value: Any) -> None:
        """运行期覆盖配置。用于人工在界面上调整策略参数。"""
        parts = path.split(".")
        with self._lock:
            cursor = self._data
            for part in parts[:-1]:
                nxt = cursor.get(part)
                if not isinstance(nxt, dict):
                    nxt = {}
                    cursor[part] = nxt
                cursor = nxt
            cursor[parts[-1]] = value

    def section(self, path: str) -> Dict[str, Any]:
        """取出一整个配置段的深拷贝。"""
        value = self.get(path, {})
        return value if isinstance(value, dict) else {}

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    def add_validator(self, path: str, predicate: Callable[[Any], bool], message: str) -> None:
        self._validators.append((path, predicate, message))

    def _install_default_validators(self) -> None:
        self.add_validator("acquisition.poll_interval_ms",
                           lambda v: isinstance(v, int) and 10 <= v <= 600000,
                           "采集周期需在 10ms~600s 之间")
        self.add_validator("control.mode",
                           lambda v: v in ("observe", "advisory", "supervised", "autonomous"),
                           "控制模式取值非法")
        self.add_validator("control.max_delta_ratio",
                           lambda v: isinstance(v, (int, float)) and 0 < v <= 1.0,
                           "单次最大相对变化需在 (0,1] 之间")
        self.add_validator("control.readback_tolerance",
                           lambda v: isinstance(v, (int, float)) and 0 <= v < 1.0,
                           "回读容差需在 [0,1) 之间")
        self.add_validator("optimization.algorithm",
                           lambda v: v in ("bayesian", "nsga2", "pso", "auto"),
                           "优化算法取值非法")
        self.add_validator("optimization.min_confidence",
                           lambda v: isinstance(v, (int, float)) and 0 <= v <= 1,
                           "最小置信度需在 [0,1] 之间")
        self.add_validator("features.window_seconds",
                           lambda v: isinstance(v, (int, float)) and v > 0,
                           "特征窗口必须为正数")
        self.add_validator("feedback.learning_rate",
                           lambda v: isinstance(v, (int, float)) and 0 < v <= 1,
                           "学习率需在 (0,1] 之间")

    def validate(self) -> None:
        """执行全部校验器，一次性汇总所有问题后抛出。"""
        problems: List[str] = []
        for path, predicate, message in self._validators:
            sentinel = object()
            value = self.get(path, sentinel)
            if value is sentinel:
                continue
            try:
                ok = predicate(value)
            except Exception:  # noqa: BLE001 - 校验器本身出错也算配置问题
                ok = False
            if not ok:
                problems.append(f"{path}: {message}（当前值 {value!r}）")
        weights = self.get("optimization.objective_weights", {})
        if isinstance(weights, dict) and weights:
            total = sum(float(v) for v in weights.values())
            if total <= 0:
                problems.append("optimization.objective_weights: 权重之和必须大于 0")
        if problems:
            raise ConfigurationError("配置校验未通过：" + "；".join(problems),
                                     {"problem_count": len(problems)})

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------
    def dump(self, redact: bool = True) -> Dict[str, Any]:
        """导出配置快照；默认对密钥类字段脱敏。"""
        with self._lock:
            snapshot = copy.deepcopy(self._data)
        if redact:
            _redact_in_place(snapshot)
        return snapshot

    def save(self, path: str) -> None:
        """保存配置（始终脱敏，绝不把密钥写入文件）。"""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.dump(redact=True), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def keys(self, prefix: str = "") -> List[str]:
        """列出全部配置路径，便于界面渲染配置树。"""
        out: List[str] = []

        def walk(node: Any, path: str) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, f"{path}.{k}" if path else k)
            else:
                out.append(path)

        walk(self.get(prefix, self._data) if prefix else self._data, prefix)
        return sorted(out)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Config sections={sorted(self._data.keys())}>"


def _redact_in_place(node: Any) -> None:
    """就地脱敏。"""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, (dict, list)):
                _redact_in_place(value)
            elif _SECRET_PATTERN.search(str(key)) and value:
                node[key] = "***REDACTED***"
    elif isinstance(node, list):
        for item in node:
            _redact_in_place(item)


__all__ = ["Config", "DEFAULT_CONFIG"]

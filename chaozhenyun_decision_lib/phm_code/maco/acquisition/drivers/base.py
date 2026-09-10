"""
协议驱动基类
============

所有现场通信驱动实现同一套接口，使得数据接入层与具体协议无关。
新增一种协议只需继承 :class:`ProtocolDriver` 并注册到 ``DRIVER_REGISTRY``。

接口约定：
* :meth:`connect` / :meth:`disconnect` 幂等；
* :meth:`read_points` 批量读，驱动内部应尽量合并为最少的请求次数；
* :meth:`write_point` 单点写，返回是否成功，失败抛 :class:`TransientError` 子类；
* 所有方法必须是线程安全的，或在内部自行加锁。

驱动只负责 "取回原始数值"，不做量纲换算、不做质量判定、不做清洗——
这些由上层 :mod:`maco.acquisition.collector` 与
:mod:`maco.acquisition.cleansing` 完成，保证职责单一。
"""

from __future__ import annotations

import abc
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ...core.errors import ConnectionFailure, TimeoutExpired, TransientError
from ...core.logging_setup import get_logger
from ...core.types import SignalPoint, now_ts

logger = get_logger("acquisition.driver")


@dataclass
class RawReading:
    """驱动读回的原始值（未做工程量换算）。"""

    point_id: str
    raw_value: Optional[float]
    timestamp: float = field(default_factory=now_ts)
    ok: bool = True
    error: str = ""
    #: 高频波形（振动类点位一次读回整段）
    waveform: Optional[Sequence[float]] = None


@dataclass
class DriverStats:
    """驱动运行统计，用于链路健康自检。"""

    reads: int = 0
    read_failures: int = 0
    writes: int = 0
    write_failures: int = 0
    reconnects: int = 0
    last_error: str = ""
    last_success_ts: float = 0.0
    total_read_ms: float = 0.0

    @property
    def avg_read_ms(self) -> float:
        return self.total_read_ms / self.reads if self.reads else 0.0

    @property
    def success_rate(self) -> float:
        return 1.0 - (self.read_failures / self.reads) if self.reads else 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reads": self.reads,
            "read_failures": self.read_failures,
            "writes": self.writes,
            "write_failures": self.write_failures,
            "reconnects": self.reconnects,
            "success_rate": round(self.success_rate, 4),
            "avg_read_ms": round(self.avg_read_ms, 3),
            "last_error": self.last_error,
            "last_success_ts": self.last_success_ts,
        }


class ProtocolDriver(abc.ABC):
    """现场协议驱动抽象基类。"""

    #: 协议名，子类覆盖
    protocol: str = "abstract"

    def __init__(self, name: str, options: Optional[Mapping[str, Any]] = None):
        self.name = name
        self.options: Dict[str, Any] = dict(options or {})
        self.stats = DriverStats()
        self._connected = False
        self._lock = threading.RLock()
        self._retry_times = int(self.options.get("retry_times", 3))
        self._retry_backoff_ms = float(self.options.get("retry_backoff_ms", 200))
        self._timeout_ms = float(self.options.get("timeout_ms", 2000))

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        """建立连接（幂等）。"""
        with self._lock:
            if self._connected:
                return
            self._do_connect()
            self._connected = True
        logger.info("驱动 %s(%s) 已连接", self.name, self.protocol)

    def disconnect(self) -> None:
        """断开连接（幂等）。"""
        with self._lock:
            if not self._connected:
                return
            try:
                self._do_disconnect()
            finally:
                self._connected = False
        logger.info("驱动 %s(%s) 已断开", self.name, self.protocol)

    def reconnect(self) -> None:
        """重连。通信异常后由采集器调用。"""
        self.stats.reconnects += 1
        try:
            self.disconnect()
        except Exception:  # noqa: BLE001 - 断开失败不阻碍重连
            self._connected = False
        self.connect()

    # ------------------------------------------------------------------
    # 读写（带重试的模板方法）
    # ------------------------------------------------------------------
    def read_points(self, points: Sequence[SignalPoint]) -> List[RawReading]:
        """批量读点，内部带退避重试。"""
        if not points:
            return []
        if not self._connected:
            self.connect()
        start = time.perf_counter()
        last_exc: Optional[Exception] = None
        for attempt in range(self._retry_times + 1):
            try:
                readings = self._do_read(points)
                cost = (time.perf_counter() - start) * 1000.0
                self.stats.reads += 1
                self.stats.total_read_ms += cost
                self.stats.last_success_ts = now_ts()
                return readings
            except TransientError as exc:
                last_exc = exc
                self.stats.last_error = str(exc)
                if attempt < self._retry_times:
                    backoff = self._retry_backoff_ms * (2 ** attempt) / 1000.0
                    logger.warning("驱动 %s 读失败(第%d次)，%.2fs 后重试: %s",
                                   self.name, attempt + 1, backoff, exc)
                    time.sleep(backoff)
                    if isinstance(exc, (ConnectionFailure, TimeoutExpired)):
                        try:
                            self.reconnect()
                        except Exception:  # noqa: BLE001
                            pass
        self.stats.reads += 1
        self.stats.read_failures += 1
        # 重试耗尽：不抛异常，返回全部失败的读数，由上层标记质量为坏值，
        # 保证单台设备通信中断不会拖垮整个采集循环。
        message = str(last_exc) if last_exc else "未知读取错误"
        return [RawReading(point_id=p.point_id, raw_value=None, ok=False, error=message)
                for p in points]

    def write_point(self, point: SignalPoint, raw_value: float) -> bool:
        """写单点，内部带退避重试。"""
        if not point.writable:
            raise PermissionError(f"测点 {point.point_id} 不可写")
        if not self._connected:
            self.connect()
        last_exc: Optional[Exception] = None
        for attempt in range(self._retry_times + 1):
            try:
                ok = self._do_write(point, raw_value)
                self.stats.writes += 1
                if not ok:
                    self.stats.write_failures += 1
                return ok
            except TransientError as exc:
                last_exc = exc
                self.stats.last_error = str(exc)
                if attempt < self._retry_times:
                    time.sleep(self._retry_backoff_ms * (2 ** attempt) / 1000.0)
        self.stats.writes += 1
        self.stats.write_failures += 1
        raise last_exc if last_exc else TransientError("写入失败")

    def health(self) -> Dict[str, Any]:
        """驱动健康快照。"""
        return {
            "name": self.name,
            "protocol": self.protocol,
            "connected": self._connected,
            **self.stats.to_dict(),
        }

    # ------------------------------------------------------------------
    # 子类实现点
    # ------------------------------------------------------------------
    @abc.abstractmethod
    def _do_connect(self) -> None:
        """建立底层连接。失败应抛 :class:`ConnectionFailure`。"""

    @abc.abstractmethod
    def _do_disconnect(self) -> None:
        """释放底层连接。"""

    @abc.abstractmethod
    def _do_read(self, points: Sequence[SignalPoint]) -> List[RawReading]:
        """批量读取。瞬时失败应抛 :class:`TransientError` 的子类。"""

    @abc.abstractmethod
    def _do_write(self, point: SignalPoint, raw_value: float) -> bool:
        """写单点。"""

    def __enter__(self) -> "ProtocolDriver":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.disconnect()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} name={self.name} connected={self._connected}>"


__all__ = ["ProtocolDriver", "RawReading", "DriverStats"]

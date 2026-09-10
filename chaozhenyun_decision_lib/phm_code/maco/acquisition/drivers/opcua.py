"""
OPC UA 驱动
===========

面向 SCADA / DCS / 智能网关最常用的 OPC UA 接口。工业现场的 OPC UA 服务端
形态差异很大（原生二进制、HTTPS 网关、厂商 SDK），因此本驱动把
"会话通道" 抽象为 :class:`UaSession`，具体实现可插拔：

* :class:`InMemoryUaSession` —— 内置地址空间，用于离线联调与出厂自测；
* 现场部署时注入基于厂商 SDK 或开源栈的会话实现即可，驱动本身不改。

测点地址写法（``SignalPoint.address``）为标准 NodeId::

    "ns=2;s=Line1.Fan01.BearingTemp"
    "ns=3;i=1057"
    "ns=2;g=09087e75-8e5e-499b-954f-f2a8624db28a"

驱动实现了 OPC UA 的两种取数方式：
* **轮询读**（Read Service）——由采集器按周期驱动；
* **订阅推送**（Subscription + MonitoredItem）——服务端变化时回调，
  适合低时延要求的关键点位，可显著降低网络负载。
"""

from __future__ import annotations

import abc
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ...core.errors import ConnectionFailure, ProtocolError, TimeoutExpired
from ...core.logging_setup import get_logger
from ...core.registry import DRIVER_REGISTRY
from ...core.types import SignalPoint, now_ts
from .base import ProtocolDriver, RawReading

logger = get_logger("acquisition.opcua")

_NODEID_RE = re.compile(r"^ns=(\d+);(?P<kind>[sig]|b)=(?P<ident>.+)$", re.I)

#: OPC UA StatusCode 到系统质量标记的简化映射
UA_STATUS_GOOD = 0x00000000
UA_STATUS_UNCERTAIN = 0x40000000
UA_STATUS_BAD = 0x80000000


@dataclass(frozen=True)
class NodeId:
    """OPC UA 节点标识。"""

    namespace: int
    identifier: str
    #: s=字符串 / i=数值 / g=GUID / b=字节串
    kind: str = "s"

    def to_string(self) -> str:
        return f"ns={self.namespace};{self.kind}={self.identifier}"

    def __str__(self) -> str:  # pragma: no cover
        return self.to_string()


def parse_node_id(address: str) -> NodeId:
    """解析 NodeId 字符串。"""
    match = _NODEID_RE.match(str(address).strip())
    if not match:
        raise ProtocolError(f"OPC UA NodeId 格式非法: {address!r}", {"address": address})
    return NodeId(
        namespace=int(match.group(1)),
        identifier=match.group("ident"),
        kind=match.group("kind").lower(),
    )


@dataclass
class DataValue:
    """OPC UA DataValue 简化模型。"""

    value: Optional[float]
    status_code: int = UA_STATUS_GOOD
    source_timestamp: float = field(default_factory=now_ts)
    server_timestamp: float = field(default_factory=now_ts)

    @property
    def good(self) -> bool:
        return (self.status_code & 0xC0000000) == UA_STATUS_GOOD

    @property
    def uncertain(self) -> bool:
        return (self.status_code & 0xC0000000) == UA_STATUS_UNCERTAIN


class UaSession(abc.ABC):
    """OPC UA 会话抽象。现场实现只需覆盖这四个方法。"""

    @abc.abstractmethod
    def open(self, endpoint: str, options: Mapping[str, Any]) -> None: ...

    @abc.abstractmethod
    def close(self) -> None: ...

    @abc.abstractmethod
    def read(self, node_ids: Sequence[NodeId]) -> List[DataValue]: ...

    @abc.abstractmethod
    def write(self, node_id: NodeId, value: float) -> bool: ...

    def create_subscription(
        self,
        node_ids: Sequence[NodeId],
        callback: Callable[[NodeId, DataValue], None],
        publishing_interval_ms: float = 500.0,
    ) -> Optional[str]:
        """创建订阅。默认返回 None 表示该实现不支持订阅，驱动自动退化为轮询。"""
        return None

    def delete_subscription(self, subscription_id: str) -> None:
        """删除订阅。默认空实现。"""


class InMemoryUaSession(UaSession):
    """内置地址空间会话。

    用于离线联调、单元测试与出厂自测：不建立任何网络连接，
    节点值由外部通过 :meth:`set_value` 写入（仿真器即以此方式驱动）。
    """

    def __init__(self) -> None:
        self._values: Dict[str, DataValue] = {}
        self._opened = False
        self._lock = threading.RLock()
        self._subs: Dict[str, Tuple[List[NodeId], Callable[[NodeId, DataValue], None]]] = {}
        self._sub_seq = 0

    def open(self, endpoint: str, options: Mapping[str, Any]) -> None:
        self._opened = True
        logger.debug("内置 OPC UA 会话已打开: %s", endpoint)

    def close(self) -> None:
        self._opened = False
        with self._lock:
            self._subs.clear()

    def read(self, node_ids: Sequence[NodeId]) -> List[DataValue]:
        if not self._opened:
            raise ConnectionFailure("OPC UA 会话未打开", {})
        with self._lock:
            out: List[DataValue] = []
            for nid in node_ids:
                dv = self._values.get(nid.to_string())
                out.append(dv if dv is not None
                           else DataValue(None, status_code=UA_STATUS_BAD))
            return out

    def write(self, node_id: NodeId, value: float) -> bool:
        if not self._opened:
            raise ConnectionFailure("OPC UA 会话未打开", {})
        self.set_value(node_id.to_string(), float(value))
        return True

    def set_value(self, node_key: str, value: Optional[float],
                  status_code: int = UA_STATUS_GOOD) -> None:
        """外部写入节点值，并触发订阅回调。"""
        dv = DataValue(value=value, status_code=status_code)
        with self._lock:
            self._values[node_key] = dv
            subs = list(self._subs.values())
        for nodes, callback in subs:
            for nid in nodes:
                if nid.to_string() == node_key:
                    try:
                        callback(nid, dv)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("OPC UA 订阅回调异常: %s", exc)

    def create_subscription(
        self,
        node_ids: Sequence[NodeId],
        callback: Callable[[NodeId, DataValue], None],
        publishing_interval_ms: float = 500.0,
    ) -> Optional[str]:
        with self._lock:
            self._sub_seq += 1
            sub_id = f"sub-{self._sub_seq}"
            self._subs[sub_id] = (list(node_ids), callback)
        return sub_id

    def delete_subscription(self, subscription_id: str) -> None:
        with self._lock:
            self._subs.pop(subscription_id, None)

    def node_count(self) -> int:
        with self._lock:
            return len(self._values)


@DRIVER_REGISTRY.decorator("opcua")
class OpcUaDriver(ProtocolDriver):
    """OPC UA 客户端驱动。

    :param options:
        ``endpoint``      服务端地址，如 ``opc.tcp://192.168.1.20:4840``
        ``security_mode`` ``None`` / ``Sign`` / ``SignAndEncrypt``
        ``use_subscription`` 是否启用订阅推送
        ``publishing_interval_ms`` 订阅发布周期
        ``session``       注入的 :class:`UaSession` 实例（缺省用内置会话）
    """

    protocol = "opcua"

    def __init__(self, name: str, options: Optional[Mapping[str, Any]] = None):
        super().__init__(name, options)
        self._endpoint = str(self.options.get("endpoint", "opc.tcp://127.0.0.1:4840"))
        self._session: UaSession = self.options.get("session") or InMemoryUaSession()
        self._use_subscription = bool(self.options.get("use_subscription", False))
        self._publish_interval = float(self.options.get("publishing_interval_ms", 500.0))
        #: 订阅推送缓存：NodeId 字符串 -> 最新 DataValue
        self._push_cache: Dict[str, DataValue] = {}
        self._sub_id: Optional[str] = None
        self._cache_lock = threading.RLock()
        self._node_cache: Dict[str, NodeId] = {}

    # ------------------------------------------------------------------
    def _do_connect(self) -> None:
        try:
            self._session.open(self._endpoint, self.options)
        except ConnectionFailure:
            raise
        except Exception as exc:  # noqa: BLE001 - 会话实现可能抛任意异常
            raise ConnectionFailure(
                f"OPC UA 会话建立失败: {exc}", {"endpoint": self._endpoint}
            ) from exc

    def _do_disconnect(self) -> None:
        if self._sub_id:
            try:
                self._session.delete_subscription(self._sub_id)
            finally:
                self._sub_id = None
        self._session.close()

    def _node_of(self, point: SignalPoint) -> NodeId:
        cached = self._node_cache.get(point.address)
        if cached is None:
            cached = parse_node_id(point.address)
            self._node_cache[point.address] = cached
        return cached

    # ------------------------------------------------------------------
    def enable_subscription(self, points: Sequence[SignalPoint]) -> bool:
        """为给定测点开启订阅推送。返回是否成功启用。"""
        if not self._use_subscription:
            return False
        if not self.connected:
            self.connect()
        nodes = [self._node_of(p) for p in points]
        sub_id = self._session.create_subscription(
            nodes, self._on_data_change, self._publish_interval
        )
        if sub_id is None:
            logger.info("会话实现不支持订阅，驱动 %s 退化为轮询模式", self.name)
            return False
        self._sub_id = sub_id
        logger.info("驱动 %s 已建立 OPC UA 订阅，监视 %d 个节点", self.name, len(nodes))
        return True

    def _on_data_change(self, node_id: NodeId, data_value: DataValue) -> None:
        with self._cache_lock:
            self._push_cache[node_id.to_string()] = data_value

    # ------------------------------------------------------------------
    def _do_read(self, points: Sequence[SignalPoint]) -> List[RawReading]:
        nodes: List[NodeId] = []
        pending: List[SignalPoint] = []
        out: List[RawReading] = []

        # 订阅缓存命中的点无需发起网络读
        with self._cache_lock:
            for p in points:
                try:
                    nid = self._node_of(p)
                except ProtocolError as exc:
                    out.append(RawReading(p.point_id, None, ok=False, error=str(exc)))
                    continue
                cached = self._push_cache.get(nid.to_string())
                if cached is not None:
                    out.append(self._to_reading(p, cached))
                else:
                    nodes.append(nid)
                    pending.append(p)

        if nodes:
            try:
                values = self._session.read(nodes)
            except (ConnectionFailure, TimeoutExpired):
                raise
            except Exception as exc:  # noqa: BLE001
                raise ProtocolError(f"OPC UA 读服务失败: {exc}",
                                    {"endpoint": self._endpoint}) from exc
            if len(values) != len(pending):
                raise ProtocolError(
                    f"OPC UA 读返回数量不符：请求 {len(pending)}，返回 {len(values)}",
                    {"endpoint": self._endpoint},
                )
            for point, dv in zip(pending, values):
                out.append(self._to_reading(point, dv))
        return out

    @staticmethod
    def _to_reading(point: SignalPoint, dv: DataValue) -> RawReading:
        if dv.value is None or not dv.good and not dv.uncertain:
            return RawReading(
                point.point_id, None, timestamp=dv.source_timestamp, ok=False,
                error=f"StatusCode=0x{dv.status_code:08X}",
            )
        return RawReading(point.point_id, float(dv.value), timestamp=dv.source_timestamp)

    def _do_write(self, point: SignalPoint, raw_value: float) -> bool:
        node = self._node_of(point)
        try:
            return bool(self._session.write(node, float(raw_value)))
        except (ConnectionFailure, TimeoutExpired):
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProtocolError(f"OPC UA 写服务失败: {exc}",
                                {"node_id": node.to_string()}) from exc

    # ------------------------------------------------------------------
    @property
    def session(self) -> UaSession:
        """暴露会话实例，便于仿真器直接注入数据。"""
        return self._session

    def describe(self) -> Dict[str, Any]:
        return {
            "protocol": self.protocol,
            "endpoint": self._endpoint,
            "security_mode": self.options.get("security_mode", "None"),
            "subscription": bool(self._sub_id),
            "cached_nodes": len(self._push_cache),
            "connected": self.connected,
        }


__all__ = [
    "OpcUaDriver",
    "UaSession",
    "InMemoryUaSession",
    "NodeId",
    "DataValue",
    "parse_node_id",
]

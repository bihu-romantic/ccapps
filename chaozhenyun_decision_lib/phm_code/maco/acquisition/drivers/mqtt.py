"""
MQTT 驱动
=========

面向 IIoT 网关、边缘采集盒、无线传感器的推送式数据接入。与 Modbus / S7 / OPC UA
的 "主动轮询" 不同，MQTT 是 "被动订阅"，因此本驱动内部维护一份最新值缓存：
消息到达时更新缓存，采集器轮询时直接从缓存取数，并按超时判定数据是否陈旧。

测点地址写法（``SignalPoint.address``）为 ``主题[#JSON路径]``::

    "plant/line1/fan01/temp"                 整条消息即为数值
    "plant/line1/fan01/telemetry#bearing_t"  取 JSON 字段
    "plant/line1/fan01/telemetry#sensors.0.value"  支持点号路径与数组下标

传输层同样做了抽象（:class:`MqttTransport`），内置
:class:`LoopbackMqttTransport` 供离线联调，现场部署时注入真实客户端。
"""

from __future__ import annotations

import abc
import json
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ...core.errors import ConnectionFailure, ProtocolError
from ...core.logging_setup import get_logger
from ...core.registry import DRIVER_REGISTRY
from ...core.types import SignalPoint, now_ts
from .base import ProtocolDriver, RawReading

logger = get_logger("acquisition.mqtt")

#: 消息回调签名：(主题, 负载字节)
MessageCallback = Callable[[str, bytes], None]


class MqttTransport(abc.ABC):
    """MQTT 传输抽象。"""

    @abc.abstractmethod
    def connect(self, options: Mapping[str, Any]) -> None: ...

    @abc.abstractmethod
    def disconnect(self) -> None: ...

    @abc.abstractmethod
    def subscribe(self, topics: Sequence[str], callback: MessageCallback,
                  qos: int = 1) -> None: ...

    @abc.abstractmethod
    def publish(self, topic: str, payload: bytes, qos: int = 1,
                retain: bool = False) -> bool: ...


class LoopbackMqttTransport(MqttTransport):
    """回环传输：本地投递，不建立网络连接。用于联调与自测。"""

    def __init__(self) -> None:
        self._callbacks: List[Tuple[List[str], MessageCallback]] = []
        self._connected = False
        self._lock = threading.RLock()
        self._published: List[Tuple[str, bytes]] = []

    def connect(self, options: Mapping[str, Any]) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False
        with self._lock:
            self._callbacks.clear()

    def subscribe(self, topics: Sequence[str], callback: MessageCallback,
                  qos: int = 1) -> None:
        with self._lock:
            self._callbacks.append((list(topics), callback))

    def publish(self, topic: str, payload: bytes, qos: int = 1,
                retain: bool = False) -> bool:
        if not self._connected:
            raise ConnectionFailure("MQTT 未连接", {})
        with self._lock:
            self._published.append((topic, payload))
            targets = list(self._callbacks)
        for topics, callback in targets:
            if any(topic_matches(pattern, topic) for pattern in topics):
                callback(topic, payload)
        return True

    def published_messages(self) -> List[Tuple[str, bytes]]:
        with self._lock:
            return list(self._published)


def topic_matches(pattern: str, topic: str) -> bool:
    """MQTT 主题通配匹配（支持 ``+`` 单层与 ``#`` 多层）。"""
    p_parts = pattern.split("/")
    t_parts = topic.split("/")
    for i, p in enumerate(p_parts):
        if p == "#":
            return True
        if i >= len(t_parts):
            return False
        if p != "+" and p != t_parts[i]:
            return False
    return len(p_parts) == len(t_parts)


def extract_json_path(payload: Any, path: str) -> Any:
    """按点号路径从嵌套结构中取值，支持数组下标。"""
    cursor = payload
    for part in path.split("."):
        if part == "":
            continue
        if isinstance(cursor, dict):
            if part not in cursor:
                return None
            cursor = cursor[part]
        elif isinstance(cursor, (list, tuple)):
            try:
                cursor = cursor[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cursor


@dataclass
class _CachedValue:
    value: Optional[float]
    timestamp: float = field(default_factory=now_ts)
    raw_topic: str = ""


@DRIVER_REGISTRY.decorator("mqtt")
class MqttDriver(ProtocolDriver):
    """MQTT 订阅式数据接入驱动。

    :param options:
        ``broker`` / ``port`` / ``client_id`` / ``qos``
        ``stale_seconds`` 超过该时长未更新即判为陈旧
        ``transport`` 注入的 :class:`MqttTransport`（缺省用回环传输）
    """

    protocol = "mqtt"

    def __init__(self, name: str, options: Optional[Mapping[str, Any]] = None):
        super().__init__(name, options)
        self._broker = str(self.options.get("broker", "127.0.0.1"))
        self._port = int(self.options.get("port", 1883))
        self._qos = int(self.options.get("qos", 1))
        self._stale_seconds = float(self.options.get("stale_seconds", 30.0))
        self._transport: MqttTransport = self.options.get("transport") or LoopbackMqttTransport()
        self._cache: Dict[str, _CachedValue] = {}
        self._cache_lock = threading.RLock()
        #: 主题 -> 订阅该主题的测点地址列表
        self._topic_map: Dict[str, List[str]] = {}
        self._subscribed = False
        self._message_count = 0

    # ------------------------------------------------------------------
    def _do_connect(self) -> None:
        try:
            self._transport.connect({
                "broker": self._broker,
                "port": self._port,
                "client_id": self.options.get("client_id", f"maco-{self.name}"),
                "keepalive": self.options.get("keepalive", 60),
            })
        except ConnectionFailure:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ConnectionFailure(f"MQTT 连接失败: {exc}",
                                    {"broker": self._broker, "port": self._port}) from exc

    def _do_disconnect(self) -> None:
        self._transport.disconnect()
        self._subscribed = False

    # ------------------------------------------------------------------
    @staticmethod
    def split_address(address: str) -> Tuple[str, str]:
        """把地址拆成 ``(主题, JSON路径)``。"""
        text = str(address).strip()
        if "#" in text:
            topic, path = text.split("#", 1)
            return topic.strip(), path.strip()
        return text, ""

    def ensure_subscribed(self, points: Sequence[SignalPoint]) -> None:
        """按测点表建立订阅（幂等）。"""
        topics: List[str] = []
        with self._cache_lock:
            for p in points:
                topic, _ = self.split_address(p.address)
                if not topic:
                    continue
                self._topic_map.setdefault(topic, [])
                if p.address not in self._topic_map[topic]:
                    self._topic_map[topic].append(p.address)
                if topic not in topics:
                    topics.append(topic)
        if topics and not self._subscribed:
            self._transport.subscribe(topics, self._on_message, self._qos)
            self._subscribed = True
            logger.info("驱动 %s 已订阅 %d 个 MQTT 主题", self.name, len(topics))

    def _on_message(self, topic: str, payload: bytes) -> None:
        """消息到达回调：解析并写入缓存。"""
        self._message_count += 1
        try:
            text = payload.decode("utf-8").strip()
        except UnicodeDecodeError:
            logger.warning("MQTT 消息不是 UTF-8 文本，已丢弃：%s", topic)
            return
        parsed: Any = None
        if text.startswith(("{", "[")):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                logger.warning("MQTT 消息 JSON 解析失败：%s", topic)
                return
        with self._cache_lock:
            addresses = list(self._topic_map.get(topic, []))
            # 通配订阅：找出所有能匹配该主题的地址
            for pattern, addrs in self._topic_map.items():
                if pattern != topic and topic_matches(pattern, topic):
                    addresses.extend(addrs)
        for address in addresses:
            _, path = self.split_address(address)
            raw: Any
            if path:
                raw = extract_json_path(parsed if parsed is not None else {}, path)
            else:
                raw = parsed if parsed is not None else text
            value: Optional[float]
            try:
                value = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                value = None
            with self._cache_lock:
                self._cache[address] = _CachedValue(value=value, raw_topic=topic)

    # ------------------------------------------------------------------
    def _do_read(self, points: Sequence[SignalPoint]) -> List[RawReading]:
        self.ensure_subscribed(points)
        out: List[RawReading] = []
        now = now_ts()
        with self._cache_lock:
            for p in points:
                cached = self._cache.get(p.address)
                if cached is None:
                    out.append(RawReading(p.point_id, None, ok=False, error="尚未收到该主题消息"))
                    continue
                age = now - cached.timestamp
                if age > self._stale_seconds:
                    out.append(RawReading(
                        p.point_id, cached.value, timestamp=cached.timestamp,
                        ok=False, error=f"数据陈旧 {age:.1f}s",
                    ))
                elif cached.value is None:
                    out.append(RawReading(p.point_id, None, ok=False, error="消息中未找到该字段"))
                else:
                    out.append(RawReading(p.point_id, cached.value,
                                          timestamp=cached.timestamp))
        return out

    def _do_write(self, point: SignalPoint, raw_value: float) -> bool:
        """向下行主题发布控制值。下行主题约定为 ``<上行主题>/set``。"""
        topic, path = self.split_address(point.address)
        down_topic = str(self.options.get("command_topic") or f"{topic}/set")
        payload = (json.dumps({path: raw_value}, ensure_ascii=False).encode("utf-8")
                   if path else str(raw_value).encode("utf-8"))
        try:
            return bool(self._transport.publish(down_topic, payload, self._qos))
        except ConnectionFailure:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProtocolError(f"MQTT 发布失败: {exc}", {"topic": down_topic}) from exc

    # ------------------------------------------------------------------
    @property
    def transport(self) -> MqttTransport:
        return self._transport

    def describe(self) -> Dict[str, Any]:
        with self._cache_lock:
            cached = len(self._cache)
            topics = len(self._topic_map)
        return {
            "protocol": self.protocol,
            "broker": self._broker,
            "port": self._port,
            "qos": self._qos,
            "topics": topics,
            "cached_points": cached,
            "messages_received": self._message_count,
            "connected": self.connected,
        }


__all__ = [
    "MqttDriver",
    "MqttTransport",
    "LoopbackMqttTransport",
    "topic_matches",
    "extract_json_path",
]

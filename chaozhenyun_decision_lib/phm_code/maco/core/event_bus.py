"""
事件总线模块
============

系统采用 "发布-订阅" 方式解耦各功能层。数据接入层不知道谁在消费采集帧，
优化层不知道谁在执行指令，从而使得任一层可以独立替换、独立测试。

实现要点：
1. 线程安全：订阅表使用可重入锁保护，支持运行期动态订阅/退订；
2. 隔离故障：单个订阅者抛异常不影响其它订阅者，异常统一转成
   ``SYSTEM_ERROR`` 事件再发布一次，便于集中监控；
3. 同步/异步双模：同步模式便于确定性测试，异步模式用于现场高吞吐场景；
4. 事件留存：内置环形历史，供接口层查询最近事件（默认 2000 条）。
"""

from __future__ import annotations

import queue
import threading
import traceback
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional

from .enums import EventTopic
from .types import new_id, now_ts

#: 订阅回调签名
Handler = Callable[["Event"], None]


@dataclass
class Event:
    """总线事件。"""

    topic: EventTopic
    payload: Any = None
    device_id: str = ""
    trace_id: str = ""
    event_id: str = field(default_factory=lambda: new_id("evt"))
    timestamp: float = field(default_factory=now_ts)
    source: str = ""

    def to_dict(self) -> Dict[str, Any]:
        payload = self.payload
        if hasattr(payload, "to_dict"):
            payload = payload.to_dict()
        elif not isinstance(payload, (dict, list, str, int, float, bool, type(None))):
            payload = repr(payload)
        return {
            "event_id": self.event_id,
            "topic": self.topic.code,
            "topic_label": self.topic.label,
            "device_id": self.device_id,
            "trace_id": self.trace_id,
            "source": self.source,
            "timestamp": self.timestamp,
            "payload": payload,
        }


@dataclass
class _Subscription:
    """一条订阅记录。"""

    sub_id: str
    topic: Optional[EventTopic]
    handler: Handler
    device_filter: Optional[str] = None
    #: 订阅者名称，出错时用于定位
    name: str = ""
    #: 累计处理次数与失败次数
    calls: int = 0
    failures: int = 0

    def matches(self, event: Event) -> bool:
        if self.topic is not None and self.topic is not event.topic:
            return False
        if self.device_filter and event.device_id and self.device_filter != event.device_id:
            return False
        return True


class EventBus:
    """线程安全的发布-订阅总线。"""

    def __init__(self, history_size: int = 2000, async_mode: bool = False,
                 worker_count: int = 2):
        self._lock = threading.RLock()
        self._subs: List[_Subscription] = []
        self._history: Deque[Event] = deque(maxlen=history_size)
        self._async_mode = async_mode
        self._queue: "queue.Queue[Optional[Event]]" = queue.Queue()
        self._workers: List[threading.Thread] = []
        self._running = False
        self._worker_count = max(1, worker_count)
        self._published = 0
        self._error_depth = 0  # 防止错误事件递归风暴

    # ------------------------------------------------------------------
    # 订阅管理
    # ------------------------------------------------------------------
    def subscribe(
        self,
        topic: Optional[EventTopic],
        handler: Handler,
        device_filter: Optional[str] = None,
        name: str = "",
    ) -> str:
        """订阅事件。

        :param topic: 传 ``None`` 表示订阅所有主题（通配订阅）
        :param handler: 回调函数
        :param device_filter: 只接收指定设备的事件
        :param name: 订阅者标识，用于诊断
        :return: 订阅号，用于退订
        """
        sub = _Subscription(
            sub_id=new_id("sub"),
            topic=topic,
            handler=handler,
            device_filter=device_filter,
            name=name or getattr(handler, "__qualname__", "anonymous"),
        )
        with self._lock:
            self._subs.append(sub)
        return sub.sub_id

    def unsubscribe(self, sub_id: str) -> bool:
        """退订。"""
        with self._lock:
            for i, sub in enumerate(self._subs):
                if sub.sub_id == sub_id:
                    del self._subs[i]
                    return True
        return False

    def subscriber_stats(self) -> List[Dict[str, Any]]:
        """订阅者运行统计，供健康自检接口使用。"""
        with self._lock:
            return [
                {
                    "sub_id": s.sub_id,
                    "name": s.name,
                    "topic": s.topic.code if s.topic else "*",
                    "device_filter": s.device_filter or "*",
                    "calls": s.calls,
                    "failures": s.failures,
                }
                for s in self._subs
            ]

    # ------------------------------------------------------------------
    # 发布
    # ------------------------------------------------------------------
    def publish(
        self,
        topic: EventTopic,
        payload: Any = None,
        device_id: str = "",
        trace_id: str = "",
        source: str = "",
    ) -> Event:
        """发布事件。返回构造好的事件对象便于调用方留痕。"""
        event = Event(
            topic=topic,
            payload=payload,
            device_id=device_id,
            trace_id=trace_id,
            source=source,
        )
        self.publish_event(event)
        return event

    def publish_event(self, event: Event) -> None:
        with self._lock:
            self._history.append(event)
            self._published += 1
        if self._async_mode and self._running:
            self._queue.put(event)
        else:
            self._dispatch(event)

    # ------------------------------------------------------------------
    # 分发
    # ------------------------------------------------------------------
    def _dispatch(self, event: Event) -> None:
        with self._lock:
            targets = [s for s in self._subs if s.matches(event)]
        for sub in targets:
            try:
                sub.calls += 1
                sub.handler(event)
            except Exception as exc:  # noqa: BLE001 - 必须隔离订阅者故障
                sub.failures += 1
                self._report_handler_error(sub, event, exc)

    def _report_handler_error(self, sub: _Subscription, event: Event, exc: Exception) -> None:
        """把订阅者异常转成系统错误事件，同时避免递归风暴。"""
        if event.topic is EventTopic.SYSTEM_ERROR or self._error_depth > 0:
            return
        self._error_depth += 1
        try:
            self.publish(
                EventTopic.SYSTEM_ERROR,
                payload={
                    "subscriber": sub.name,
                    "sub_id": sub.sub_id,
                    "origin_topic": event.topic.code,
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(limit=6),
                },
                device_id=event.device_id,
                trace_id=event.trace_id,
                source="event_bus",
            )
        finally:
            self._error_depth -= 1

    # ------------------------------------------------------------------
    # 异步模式生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动异步分发线程。同步模式下为空操作。"""
        if not self._async_mode or self._running:
            return
        self._running = True
        for i in range(self._worker_count):
            t = threading.Thread(target=self._worker_loop, name=f"bus-worker-{i}", daemon=True)
            t.start()
            self._workers.append(t)

    def stop(self, timeout: float = 3.0) -> None:
        """停止异步分发线程并排空队列。"""
        if not self._running:
            return
        self._running = False
        for _ in self._workers:
            self._queue.put(None)
        for t in self._workers:
            t.join(timeout=timeout)
        self._workers.clear()

    def _worker_loop(self) -> None:
        while self._running:
            try:
                event = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if event is None:
                break
            self._dispatch(event)

    # ------------------------------------------------------------------
    # 历史查询
    # ------------------------------------------------------------------
    def history(
        self,
        topic: Optional[EventTopic] = None,
        device_id: str = "",
        trace_id: str = "",
        limit: int = 100,
    ) -> List[Event]:
        """按条件倒序查询最近事件。"""
        with self._lock:
            items = list(self._history)
        out: List[Event] = []
        for ev in reversed(items):
            if topic is not None and ev.topic is not topic:
                continue
            if device_id and ev.device_id != device_id:
                continue
            if trace_id and ev.trace_id != trace_id:
                continue
            out.append(ev)
            if len(out) >= limit:
                break
        return out

    def trace(self, trace_id: str) -> List[Event]:
        """按 trace_id 取出一条完整决策链的所有事件（正序）。"""
        with self._lock:
            return [ev for ev in self._history if ev.trace_id == trace_id]

    @property
    def published_count(self) -> int:
        return self._published

    def clear_history(self) -> None:
        with self._lock:
            self._history.clear()


#: 进程级默认总线。测试或多实例部署时可另行构造。
_default_bus: Optional[EventBus] = None
_default_lock = threading.Lock()


def get_default_bus() -> EventBus:
    """获取进程级默认事件总线（惰性单例）。"""
    global _default_bus
    if _default_bus is None:
        with _default_lock:
            if _default_bus is None:
                _default_bus = EventBus()
    return _default_bus


def reset_default_bus() -> None:
    """重置默认总线，仅供测试使用。"""
    global _default_bus
    with _default_lock:
        if _default_bus is not None:
            _default_bus.stop()
        _default_bus = None


__all__ = ["Event", "EventBus", "Handler", "get_default_bus", "reset_default_bus"]

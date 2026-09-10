"""
任务调度模块
============

系统内的周期性工作（采集轮询、特征提取、健康评估、趋势拟合、
效果评估回调）都通过本调度器统一驱动，而不是各自起线程。这样做的好处：

* 统一的启停、暂停、限流与异常处置；
* 任务执行耗时可观测，便于判断现场节拍是否吃紧；
* 支持一次性延时任务（如 "参数下发后 T 秒回来评估效果"），
  这是反馈学习闭环的时序基础。
"""

from __future__ import annotations

import heapq
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .logging_setup import get_logger
from .types import new_id, now_ts

logger = get_logger("core.scheduler")

TaskFunc = Callable[[], Any]


@dataclass(order=True)
class _ScheduledItem:
    """调度堆中的一项。按 ``next_run`` 排序。"""

    next_run: float
    seq: int
    task_id: str = field(compare=False)
    func: TaskFunc = field(compare=False)
    interval: Optional[float] = field(compare=False, default=None)
    name: str = field(compare=False, default="")
    #: 剩余执行次数；None 表示无限
    remaining: Optional[int] = field(compare=False, default=None)
    enabled: bool = field(compare=False, default=True)


@dataclass
class TaskStat:
    """任务运行统计。"""

    task_id: str
    name: str
    interval: Optional[float]
    runs: int = 0
    failures: int = 0
    last_run: Optional[float] = None
    last_cost_ms: float = 0.0
    max_cost_ms: float = 0.0
    avg_cost_ms: float = 0.0
    last_error: str = ""

    def record(self, cost_ms: float, error: str = "") -> None:
        self.runs += 1
        self.last_run = now_ts()
        self.last_cost_ms = cost_ms
        self.max_cost_ms = max(self.max_cost_ms, cost_ms)
        # 增量平均，避免保存全部历史
        self.avg_cost_ms += (cost_ms - self.avg_cost_ms) / self.runs
        if error:
            self.failures += 1
            self.last_error = error

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "interval": self.interval,
            "runs": self.runs,
            "failures": self.failures,
            "last_run": self.last_run,
            "last_cost_ms": round(self.last_cost_ms, 3),
            "max_cost_ms": round(self.max_cost_ms, 3),
            "avg_cost_ms": round(self.avg_cost_ms, 3),
            "last_error": self.last_error,
        }


class Scheduler:
    """基于最小堆的单线程调度器。

    单线程执行保证同一时刻只有一个任务在跑，避免共享状态竞态；
    对于耗时任务，调用方应自行提交到线程池，而不是阻塞调度线程。
    """

    def __init__(self, tick_seconds: float = 0.05, name: str = "maco-scheduler"):
        self._heap: List[_ScheduledItem] = []
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._seq = 0
        self._running = False
        self._paused = False
        self._thread: Optional[threading.Thread] = None
        self._tick = max(0.005, tick_seconds)
        self._name = name
        self._stats: Dict[str, TaskStat] = {}
        self._cancelled: set[str] = set()

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------
    def every(
        self,
        interval_seconds: float,
        func: TaskFunc,
        name: str = "",
        first_delay: Optional[float] = None,
        repeat: Optional[int] = None,
    ) -> str:
        """注册周期任务。

        :param interval_seconds: 执行周期
        :param first_delay: 首次执行的延时，默认等于一个周期
        :param repeat: 限定执行次数，None 表示无限
        """
        if interval_seconds <= 0:
            raise ValueError("周期必须为正数")
        delay = interval_seconds if first_delay is None else max(0.0, first_delay)
        return self._push(func, delay, interval_seconds, name, repeat)

    def once(self, delay_seconds: float, func: TaskFunc, name: str = "") -> str:
        """注册一次性延时任务。反馈学习的 "延时评估" 依赖此接口。"""
        return self._push(func, max(0.0, delay_seconds), None, name, 1)

    def _push(
        self,
        func: TaskFunc,
        delay: float,
        interval: Optional[float],
        name: str,
        repeat: Optional[int],
    ) -> str:
        task_id = new_id("task")
        with self._cv:
            self._seq += 1
            item = _ScheduledItem(
                next_run=time.monotonic() + delay,
                seq=self._seq,
                task_id=task_id,
                func=func,
                interval=interval,
                name=name or getattr(func, "__qualname__", "task"),
                remaining=repeat,
            )
            heapq.heappush(self._heap, item)
            self._stats[task_id] = TaskStat(task_id=task_id, name=item.name, interval=interval)
            self._cv.notify_all()
        return task_id

    def cancel(self, task_id: str) -> bool:
        """取消任务（惰性删除：到点时跳过）。"""
        with self._cv:
            if task_id in self._stats:
                self._cancelled.add(task_id)
                self._cv.notify_all()
                return True
        return False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        with self._cv:
            if self._running:
                return
            self._running = True
        self._thread = threading.Thread(target=self._loop, name=self._name, daemon=True)
        self._thread.start()
        logger.info("调度器已启动：%s", self._name)

    def stop(self, timeout: float = 5.0) -> None:
        with self._cv:
            if not self._running:
                return
            self._running = False
            self._cv.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        logger.info("调度器已停止：%s", self._name)

    def pause(self) -> None:
        """暂停派发（已到点的任务会积压，恢复后立即执行）。"""
        with self._cv:
            self._paused = True

    def resume(self) -> None:
        with self._cv:
            self._paused = False
            self._cv.notify_all()

    @property
    def running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def _loop(self) -> None:
        while True:
            with self._cv:
                if not self._running:
                    return
                if self._paused or not self._heap:
                    self._cv.wait(timeout=self._tick)
                    continue
                item = self._heap[0]
                delay = item.next_run - time.monotonic()
                if delay > 0:
                    self._cv.wait(timeout=min(delay, self._tick * 4))
                    continue
                heapq.heappop(self._heap)
                if item.task_id in self._cancelled:
                    self._cancelled.discard(item.task_id)
                    self._stats.pop(item.task_id, None)
                    continue
            self._run_item(item)

    def _run_item(self, item: _ScheduledItem) -> None:
        start = time.perf_counter()
        error = ""
        try:
            item.func()
        except Exception as exc:  # noqa: BLE001 - 单任务失败不得拖垮调度器
            error = f"{type(exc).__name__}: {exc}"
            logger.error("任务执行失败 %s: %s\n%s", item.name, error,
                         traceback.format_exc(limit=5))
        cost_ms = (time.perf_counter() - start) * 1000.0
        with self._cv:
            stat = self._stats.get(item.task_id)
            if stat is not None:
                stat.record(cost_ms, error)
            if item.remaining is not None:
                item.remaining -= 1
                if item.remaining <= 0:
                    self._stats.pop(item.task_id, None)
                    return
            if item.interval is None:
                self._stats.pop(item.task_id, None)
                return
            # 追赶策略：若单次执行超过一个周期，直接从当前时刻起算，避免雪崩
            nxt = item.next_run + item.interval
            now_mono = time.monotonic()
            if nxt < now_mono:
                nxt = now_mono + item.interval
            item.next_run = nxt
            self._seq += 1
            item.seq = self._seq
            heapq.heappush(self._heap, item)
            self._cv.notify_all()

    # ------------------------------------------------------------------
    # 运行期观测
    # ------------------------------------------------------------------
    def stats(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [s.to_dict() for s in self._stats.values()]

    def pending_count(self) -> int:
        with self._lock:
            return len(self._heap)

    def run_pending_now(self, max_tasks: int = 64) -> int:
        """同步执行所有已到点任务。供确定性测试使用，不启动线程。"""
        executed = 0
        while executed < max_tasks:
            with self._lock:
                if not self._heap or self._heap[0].next_run > time.monotonic():
                    break
                item = heapq.heappop(self._heap)
                if item.task_id in self._cancelled:
                    self._cancelled.discard(item.task_id)
                    continue
            self._run_item(item)
            executed += 1
        return executed


__all__ = ["Scheduler", "TaskStat"]

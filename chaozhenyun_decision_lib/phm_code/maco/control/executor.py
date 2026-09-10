"""
指令执行与闭环控制模块
======================

对应系统功能四：**与 PLC / SCADA / DCS 的闭环联动**。

:class:`CommandExecutor` 负责把一条已通过安全门的指令写到现场，
并保证 "要么全部生效，要么全部回滚"——工业场景不接受写了一半的状态。

:class:`ClosedLoopController` 是整个闭环的状态机，把七个阶段串起来::

    感知 → 评估 → 决策 → 安全校验 → （审批）→ 下发 → 验证 → 交反馈层学习

状态机显式化的价值在于：任何一次调整都能回答 "现在走到哪一步、
卡在谁那里、失败在哪个阶段"，这是运维可观测性的基础。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..core.audit import (
    ACTION_DISPATCH,
    ACTION_ROLLBACK,
    ACTION_SAFETY,
    AuditTrail,
)
from ..core.enums import CommandState, ControlMode, EventTopic
from ..core.errors import DispatchFailure, RollbackFailure
from ..core.event_bus import EventBus
from ..core.logging_setup import get_logger
from ..core.registry import DeviceProfile
from ..core.types import ControlCommand, ExecutionRecord, now_ts
from .adapters import ControlAdapter, WriteOutcome
from .safety import SafetyVerdict

logger = get_logger("control.executor")


class CommandExecutor:
    """指令执行器：分步下发 + 回读校验 + 失败整体回滚。"""

    def __init__(self, adapter: ControlAdapter, bus: Optional[EventBus] = None,
                 audit: Optional[AuditTrail] = None, auto_rollback: bool = True,
                 step_delay_s: float = 0.2):
        self.adapter = adapter
        self.bus = bus
        self.audit = audit
        self.auto_rollback = auto_rollback
        self.step_delay_s = step_delay_s
        self._lock = threading.RLock()

    def execute(self, command: ControlCommand) -> ExecutionRecord:
        """执行指令。"""
        if command.state not in (CommandState.VALIDATED, CommandState.DISPATCHING):
            raise DispatchFailure(
                f"指令状态为「{command.state.label}」，不允许下发",
                {"command_id": command.command_id},
            )
        start = time.perf_counter()
        done: List[WriteOutcome] = []
        record = ExecutionRecord(command_id=command.command_id, success=False)

        with self._lock:
            command.transition(CommandState.DISPATCHING, "开始逐点下发")
            try:
                for item in command.items:
                    outcome = self.adapter.write(item.point_id, item.value)
                    done.append(outcome)
                    record.readback[item.point_id] = (
                        outcome.readback if outcome.readback is not None else float("nan")
                    )
                    if not outcome.success:
                        record.mismatches.append(item.point_id)
                        raise DispatchFailure(
                            outcome.error or f"{item.point_id} 下发未生效",
                            {"command_id": command.command_id,
                             "point_id": item.point_id},
                        )
                    if self.step_delay_s > 0:
                        time.sleep(self.step_delay_s)
                record.success = True
                command.dispatched_at = now_ts()
                command.verified_at = now_ts()
                command.transition(CommandState.VERIFIED,
                                   f"{len(done)} 个点位回读一致")
            except Exception as exc:  # noqa: BLE001 - 任一步失败都进入回滚
                record.error = str(exc)
                command.transition(CommandState.FAILED, record.error)
                if self.auto_rollback and done:
                    record.rolled_back = self._rollback(command, done)
                else:
                    logger.error("指令 %s 下发失败且未回滚", command.command_id)

        record.duration_ms = (time.perf_counter() - start) * 1000.0
        self._publish(command, record)
        return record

    def _rollback(self, command: ControlCommand, done: List[WriteOutcome]) -> bool:
        """逆序回滚已写入的点位。"""
        logger.warning("指令 %s 触发回滚，共 %d 个点位", command.command_id, len(done))
        all_ok = True
        for outcome in reversed(done):
            if not self.adapter.rollback(outcome):
                all_ok = False
        command.transition(CommandState.ROLLED_BACK,
                           "已回滚" if all_ok else "回滚存在失败点位，需人工干预")
        if self.audit:
            self.audit.record(ACTION_ROLLBACK, device_id=command.device_id,
                              trace_id=command.trace_id,
                              result="ok" if all_ok else "partial",
                              detail={"command_id": command.command_id,
                                      "points": [o.point_id for o in done]})
        if self.bus:
            self.bus.publish(EventTopic.COMMAND_ROLLED_BACK,
                             {"command_id": command.command_id, "complete": all_ok},
                             device_id=command.device_id, trace_id=command.trace_id)
        if not all_ok:
            raise RollbackFailure("回滚未完全成功，现场可能处于不一致状态",
                                  {"command_id": command.command_id})
        return all_ok

    def _publish(self, command: ControlCommand, record: ExecutionRecord) -> None:
        if self.audit:
            self.audit.record(
                ACTION_DISPATCH, device_id=command.device_id,
                trace_id=command.trace_id,
                result="ok" if record.success else "fail",
                detail={"command": command.to_dict(), "execution": record.to_dict()},
            )
        if self.bus:
            topic = (EventTopic.COMMAND_VERIFIED if record.success
                     else EventTopic.SYSTEM_ERROR)
            self.bus.publish(topic, {"command": command.to_dict(),
                                     "execution": record.to_dict()},
                             device_id=command.device_id, trace_id=command.trace_id)


@dataclass
class LoopContext:
    """一次闭环执行的上下文。"""

    device_id: str
    trace_id: str = ""
    stage: str = "idle"
    command: Optional[ControlCommand] = None
    execution: Optional[ExecutionRecord] = None
    started_at: float = field(default_factory=now_ts)
    finished_at: Optional[float] = None
    outcome: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "device_id": self.device_id, "trace_id": self.trace_id,
            "stage": self.stage, "outcome": self.outcome,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "command": self.command.to_dict() if self.command else None,
            "execution": self.execution.to_dict() if self.execution else None,
        }


class ClosedLoopController:
    """闭环控制状态机。"""

    STAGES = ("idle", "validating", "awaiting_approval", "dispatching",
              "verifying", "learning", "done")

    def __init__(self, executor: CommandExecutor, bus: Optional[EventBus] = None,
                 audit: Optional[AuditTrail] = None,
                 on_dispatched: Optional[Callable[[ControlCommand, ExecutionRecord],
                                                  None]] = None):
        self.executor = executor
        self.bus = bus
        self.audit = audit
        #: 下发成功后的回调——反馈学习层在此挂钩，安排延时效果评估
        self.on_dispatched = on_dispatched
        self._pending: Dict[str, ControlCommand] = {}
        self._contexts: List[LoopContext] = []
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    def submit(self, verdict: SafetyVerdict, profile: DeviceProfile) -> LoopContext:
        """提交一条安全门裁定结果，推进闭环。"""
        command = verdict.command
        ctx = LoopContext(device_id=profile.device_id,
                          trace_id=command.trace_id if command else "",
                          command=command, stage="validating")

        if self.audit and command is not None:
            self.audit.record(ACTION_SAFETY, device_id=profile.device_id,
                              trace_id=command.trace_id,
                              result="pass" if verdict.passed else "reject",
                              detail=verdict.to_dict())

        if not verdict.passed or command is None:
            ctx.stage = "done"
            ctx.outcome = f"安全门驳回：{verdict.reason}"
            ctx.finished_at = now_ts()
            self._archive(ctx)
            if self.bus:
                self.bus.publish(EventTopic.COMMAND_REJECTED, verdict.to_dict(),
                                 device_id=profile.device_id, trace_id=ctx.trace_id)
            return ctx

        if self.bus:
            self.bus.publish(EventTopic.COMMAND_VALIDATED, command.to_dict(),
                             device_id=profile.device_id, trace_id=command.trace_id)

        if command.state is CommandState.PENDING_APPROVAL:
            with self._lock:
                self._pending[command.command_id] = command
            ctx.stage = "awaiting_approval"
            ctx.outcome = "等待人工审批"
            self._archive(ctx)
            logger.info("指令 %s 进入待审批队列", command.command_id)
            return ctx

        return self._dispatch(ctx, command)

    def approve(self, command_id: str, approver: str) -> LoopContext:
        """人工审批通过，继续下发。"""
        with self._lock:
            command = self._pending.pop(command_id, None)
        if command is None:
            raise DispatchFailure(f"待审批队列中不存在指令 {command_id}",
                                  {"command_id": command_id})
        command.approver = approver
        command.transition(CommandState.VALIDATED, f"由 {approver} 审批通过")
        ctx = LoopContext(device_id=command.device_id, trace_id=command.trace_id,
                          command=command, stage="dispatching")
        logger.info("指令 %s 已由 %s 审批通过", command_id, approver)
        return self._dispatch(ctx, command)

    def reject(self, command_id: str, approver: str, reason: str) -> bool:
        """人工驳回。"""
        with self._lock:
            command = self._pending.pop(command_id, None)
        if command is None:
            return False
        command.approver = approver
        command.reject_reason = reason
        command.transition(CommandState.REJECTED, f"由 {approver} 驳回：{reason}")
        if self.audit:
            self.audit.record(ACTION_SAFETY, device_id=command.device_id,
                              trace_id=command.trace_id, actor=approver,
                              result="reject", detail={"command_id": command_id,
                                                       "reason": reason})
        return True

    # ------------------------------------------------------------------
    def _dispatch(self, ctx: LoopContext, command: ControlCommand) -> LoopContext:
        ctx.stage = "dispatching"
        try:
            record = self.executor.execute(command)
        except Exception as exc:  # noqa: BLE001
            ctx.stage = "done"
            ctx.outcome = f"下发异常：{exc}"
            ctx.finished_at = now_ts()
            self._archive(ctx)
            return ctx

        ctx.execution = record
        ctx.stage = "verifying"
        if record.success:
            ctx.stage = "learning"
            ctx.outcome = "下发成功，等待效果评估"
            if self.bus:
                self.bus.publish(EventTopic.COMMAND_DISPATCHED, command.to_dict(),
                                 device_id=command.device_id, trace_id=command.trace_id)
            if self.on_dispatched is not None:
                try:
                    self.on_dispatched(command, record)
                except Exception as exc:  # noqa: BLE001 - 回调失败不影响下发结论
                    logger.error("下发回调执行失败: %s", exc)
        else:
            ctx.stage = "done"
            ctx.outcome = f"下发失败：{record.error}"
            ctx.finished_at = now_ts()
        self._archive(ctx)
        return ctx

    def _archive(self, ctx: LoopContext) -> None:
        with self._lock:
            self._contexts.append(ctx)
            if len(self._contexts) > 500:
                del self._contexts[: len(self._contexts) - 500]

    # ------------------------------------------------------------------
    def pending_commands(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [c.to_dict() for c in self._pending.values()]

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            return [c.to_dict() for c in self._contexts[-limit:]]

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            total = len(self._contexts)
            success = sum(1 for c in self._contexts
                          if c.execution and c.execution.success)
            return {
                "total_loops": total,
                "success": success,
                "pending_approval": len(self._pending),
                "success_rate": round(success / total, 4) if total else 0.0,
            }


__all__ = ["CommandExecutor", "ClosedLoopController", "LoopContext"]

"""
安全门模块
==========

**任何写向现场的指令都必须先通过本模块。** 优化算法追求的是目标最优，
它不天然懂得 "这个阀门每分钟只能开 2%"、"检修状态下禁止启机"，
安全门就是把这些工程常识固化成不可绕过的关卡。

七道检查按代价从低到高排列，任一不过立即驳回，不再往下走：

1. 模式检查 —— 当前控制模式是否允许写；
2. 建议有效性 —— 置信度、风险等级是否达标；
3. 健康闸门 —— 设备劣化到一定程度禁止自动调参；
4. 点位检查 —— 目标点是否存在、可写、在量程内；
5. 限幅与限速 —— 绝对区间、单次变化量、变化率；
6. 互锁检查 —— 互锁条件成立时禁止写；
7. 频次限流 —— 相邻下发间隔与每小时次数上限。

设计原则是 **fail-closed**：任何判定所需信息缺失时，一律拒绝而非放行。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from ..core.enums import CommandState, ControlMode, HealthGrade, Severity
from ..core.errors import InterlockActive, SafetyViolation, WritePermissionDenied
from ..core.logging_setup import get_logger
from ..core.registry import DeviceProfile
from ..core.types import (
    ControlCommand,
    HealthReport,
    OptimizationAdvice,
    WriteItem,
    new_id,
    now_ts,
)

logger = get_logger("control.safety")


@dataclass
class Interlock:
    """一条互锁条件。成立时禁止写入。"""

    interlock_id: str
    name: str
    #: 判定函数：接收事实字典，返回 True 表示互锁成立（禁止写）
    predicate: Callable[[Mapping[str, Any]], bool]
    message: str = ""
    blocks: Sequence[str] = field(default_factory=tuple)  # 空表示阻塞全部点位

    def applies_to(self, point_id: str) -> bool:
        return not self.blocks or point_id in self.blocks


@dataclass
class SafetyVerdict:
    """安全门裁定结果。"""

    passed: bool
    command: Optional[ControlCommand] = None
    reason: str = ""
    rule_id: str = ""
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "reason": self.reason,
            "rule_id": self.rule_id,
            "notes": list(self.notes),
            "command": self.command.to_dict() if self.command else None,
        }


class SafetyGate:
    """写前安全门。"""

    def __init__(
        self,
        mode: ControlMode = ControlMode.ADVISORY,
        max_delta_ratio: float = 0.10,
        min_dispatch_interval_s: float = 300.0,
        max_dispatch_per_hour: int = 6,
        min_confidence: float = 0.55,
        block_when_grade: str = "degraded",
        approval_required_above: str = "warning",
        interlocks: Optional[Sequence[Interlock]] = None,
    ):
        self.mode = mode
        self.max_delta_ratio = max_delta_ratio
        self.min_dispatch_interval_s = min_dispatch_interval_s
        self.max_dispatch_per_hour = max_dispatch_per_hour
        self.min_confidence = min_confidence
        self.block_grade = HealthGrade.from_text(block_when_grade, HealthGrade.DEGRADED)
        self.approval_severity = Severity.from_text(approval_required_above, Severity.WARNING)
        self._interlocks: List[Interlock] = list(interlocks or [])
        self._history: Dict[str, List[float]] = {}
        self._lock = threading.RLock()
        self.rejected_count = 0
        self.passed_count = 0

    # ------------------------------------------------------------------
    def add_interlock(self, interlock: Interlock) -> None:
        self._interlocks.append(interlock)

    def set_mode(self, mode: ControlMode) -> None:
        logger.warning("控制模式变更：%s → %s", self.mode.label, mode.label)
        self.mode = mode

    # ------------------------------------------------------------------
    def validate(
        self,
        advice: OptimizationAdvice,
        profile: DeviceProfile,
        health: HealthReport,
        facts: Optional[Mapping[str, Any]] = None,
        current_values: Optional[Mapping[str, float]] = None,
    ) -> SafetyVerdict:
        """校验建议并生成控制指令。"""
        facts = dict(facts or {})
        notes: List[str] = []

        # 1. 模式检查
        if not self.mode.allows_write:
            return self._reject(advice, "SG-01",
                                f"当前为「{self.mode.label}」，不执行写操作")

        # 2. 建议有效性
        if advice.is_empty:
            return self._reject(advice, "SG-02", "建议不含任何参数变更")
        if advice.confidence < self.min_confidence:
            return self._reject(advice, "SG-03",
                                f"建议置信度 {advice.confidence:.2f} 低于阈值 "
                                f"{self.min_confidence:.2f}")

        # 3. 健康闸门
        if health.grade.rank >= self.block_grade.rank:
            return self._reject(advice, "SG-04",
                                f"设备健康等级为「{health.grade.label}」，"
                                f"达到禁止自动调参的门限")

        # 4~6. 逐点检查
        items: List[WriteItem] = []
        for change in advice.changes:
            spec = profile.parameters.get(change.param_id)
            if spec is None:
                return self._reject(advice, "SG-05",
                                    f"参数 {change.param_id} 未在设备档案中定义")
            point_id = change.write_point_id or spec.write_point_id
            if not point_id:
                return self._reject(advice, "SG-06",
                                    f"参数 {spec.name} 未绑定可写测点")
            point = profile.points.get(point_id)
            if point is None:
                return self._reject(advice, "SG-07", f"测点 {point_id} 不存在")
            if not point.writable:
                return self._reject(advice, "SG-08", f"测点 {point_id} 不可写")

            target = change.proposed_value
            # 绝对区间
            if not (spec.lower - 1e-9 <= target <= spec.upper + 1e-9):
                return self._reject(advice, "SG-09",
                                    f"{spec.name} 目标值 {target:.3f} 超出允许区间 "
                                    f"[{spec.lower}, {spec.upper}]")
            if not point.in_range(target):
                return self._reject(advice, "SG-10",
                                    f"{spec.name} 目标值超出测点量程")

            # 单次变化量限速
            base = current_values.get(change.param_id) if current_values else None
            base = change.current_value if base is None else base
            delta = abs(target - base)
            if delta > spec.max_delta + 1e-9:
                return self._reject(advice, "SG-11",
                                    f"{spec.name} 单次变化 {delta:.3f} 超过限值 "
                                    f"{spec.max_delta:.3f}")
            ratio_cap = abs(base) * self.max_delta_ratio if abs(base) > 1e-9 else float("inf")
            if delta > ratio_cap + 1e-9:
                return self._reject(advice, "SG-12",
                                    f"{spec.name} 单次相对变化 {delta / abs(base) * 100:.1f}% "
                                    f"超过限值 {self.max_delta_ratio * 100:.1f}%")

            # 互锁
            for lock in self._interlocks:
                if lock.applies_to(point_id) and lock.predicate(facts):
                    self.rejected_count += 1
                    raise InterlockActive(
                        f"互锁「{lock.name}」成立，禁止写入 {point_id}：{lock.message}",
                        rule_id=lock.interlock_id,
                        context={"point_id": point_id, "advice_id": advice.advice_id},
                    )

            items.append(WriteItem(point_id=point_id, value=target,
                                   previous_value=base, unit=spec.unit))

        # 7. 频次限流
        limited, reason = self._check_rate(advice.device_id)
        if limited:
            return self._reject(advice, "SG-13", reason)

        # 生成指令
        command = ControlCommand(
            command_id=new_id("cmd"),
            device_id=advice.device_id,
            advice_id=advice.advice_id,
            items=items,
            mode=self.mode,
            state=CommandState.VALIDATED,
            trace_id=advice.trace_id,
        )
        if self.mode.requires_approval or advice.risk.rank >= self.approval_severity.rank:
            command.state = CommandState.PENDING_APPROVAL
            notes.append(f"风险等级「{advice.risk.label}」，需人工确认后下发")
        command.safety_notes.extend(notes)

        self.passed_count += 1
        logger.info("安全门通过：设备 %s，%d 个点位，状态 %s",
                    advice.device_id, len(items), command.state.label)
        return SafetyVerdict(passed=True, command=command, notes=notes)

    # ------------------------------------------------------------------
    def _check_rate(self, device_id: str) -> tuple[bool, str]:
        """下发频次限流。"""
        now = now_ts()
        with self._lock:
            history = [t for t in self._history.get(device_id, []) if now - t < 3600.0]
            self._history[device_id] = history
            if history and now - history[-1] < self.min_dispatch_interval_s:
                wait = self.min_dispatch_interval_s - (now - history[-1])
                return True, (f"距上次下发不足 {self.min_dispatch_interval_s:.0f}s，"
                              f"还需等待 {wait:.0f}s")
            if len(history) >= self.max_dispatch_per_hour:
                return True, (f"本小时已下发 {len(history)} 次，"
                              f"达到上限 {self.max_dispatch_per_hour} 次")
        return False, ""

    def record_dispatch(self, device_id: str) -> None:
        """登记一次实际下发，供限流统计。"""
        with self._lock:
            self._history.setdefault(device_id, []).append(now_ts())

    def _reject(self, advice: OptimizationAdvice, rule_id: str,
                reason: str) -> SafetyVerdict:
        self.rejected_count += 1
        logger.warning("安全门驳回[%s] 设备 %s：%s", rule_id, advice.device_id, reason)
        command = ControlCommand(
            device_id=advice.device_id, advice_id=advice.advice_id,
            mode=self.mode, state=CommandState.REJECTED,
            reject_reason=reason, trace_id=advice.trace_id,
        )
        command.safety_notes.append(f"[{rule_id}] {reason}")
        return SafetyVerdict(passed=False, command=command, reason=reason, rule_id=rule_id)

    def stats(self) -> Dict[str, Any]:
        return {
            "mode": self.mode.code,
            "passed": self.passed_count,
            "rejected": self.rejected_count,
            "interlocks": [{"id": i.interlock_id, "name": i.name}
                           for i in self._interlocks],
            "max_delta_ratio": self.max_delta_ratio,
            "max_dispatch_per_hour": self.max_dispatch_per_hour,
        }


def default_interlocks() -> List[Interlock]:
    """内置通用互锁。"""
    return [
        Interlock(
            "IL-01", "检修状态互锁",
            lambda f: bool(f.get("maintenance_mode", False)),
            "设备处于检修挂牌状态，禁止任何远程写入",
        ),
        Interlock(
            "IL-02", "急停互锁",
            lambda f: bool(f.get("emergency_stop", False)),
            "急停回路动作中，禁止任何远程写入",
        ),
        Interlock(
            "IL-03", "通信质量互锁",
            lambda f: float(f.get("good_ratio", 1.0)) < 0.8,
            "数据良好率低于 80%，判据不可靠，禁止写入",
        ),
        Interlock(
            "IL-04", "温度红线互锁",
            lambda f: bool(f.get("temp_over_limit", False)),
            "温度已超报警限，仅允许人工干预",
        ),
    ]


__all__ = ["SafetyGate", "SafetyVerdict", "Interlock", "default_interlocks",
           "SafetyViolation", "InterlockActive", "WritePermissionDenied"]

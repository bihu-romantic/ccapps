"""
自主控制决策引擎
================

系统总装模块，把八个功能层拼成一条完整的闭环：

    数据接入 → 特征提取 → 工况识别 → 健康评估 → 知识推理
        → 参数寻优 → 安全校验 → 控制下发 → 效果评估 → 知识回写

引擎本身不含算法，只负责 **装配、编排与留痕**。任何一层都可以替换实现，
只要满足各自接口——这是系统能适配不同场景、不同现场的结构基础。

一次完整决策由 :meth:`ControlEngine.decision_cycle` 驱动，
它是幂等的、可单独调用的，因此既能被调度器周期触发，
也能被接口层按需触发，还能在测试中确定性地逐步执行。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .acquisition.buffer import TimeSeriesBuffer
from .acquisition.cleansing import DataCleanser
from .acquisition.collector import AcquisitionGateway, Collector
from .acquisition.drivers.base import ProtocolDriver
from .condition.recognizer import ConditionRecognizer
from .control.adapters import ControlAdapter
from .control.executor import ClosedLoopController, CommandExecutor, LoopContext
from .control.safety import SafetyGate, default_interlocks
from .core.audit import ACTION_ADVICE, ACTION_CONDITION, ACTION_HEALTH, AuditTrail
from .core.config import Config
from .core.enums import ControlMode, EventTopic, SignalKind
from .core.event_bus import EventBus
from .core.logging_setup import TraceScope, get_logger, setup_logging
from .core.registry import DeviceProfile, DeviceRegistry
from .core.scheduler import Scheduler
from .core.types import (
    ConditionSnapshot,
    ControlCommand,
    DecisionRecord,
    ExecutionRecord,
    ExperienceCase,
    FeatureVector,
    HealthReport,
    OptimizationAdvice,
    SampleFrame,
    now_ts,
)
from .features.extractor import FeatureExtractor
from .feedback.learner import EffectEvaluator, FeedbackLearner, RewardCalculator
from .health.assessor import BaselineLibrary, HealthAssessor
from .knowledge.base import KnowledgeBase
from .knowledge.presets import build_default_knowledge_base
from .optimization.advisor import OptimizationAdvisor
from .scenario.presets import ScenarioProfile, get_scenario

logger = get_logger("engine")


@dataclass
class CycleResult:
    """一次决策周期的产物。"""

    device_id: str
    trace_id: str = ""
    frame: Optional[SampleFrame] = None
    features: Optional[FeatureVector] = None
    condition: Optional[ConditionSnapshot] = None
    health: Optional[HealthReport] = None
    advice: Optional[OptimizationAdvice] = None
    loop: Optional[LoopContext] = None
    stage: str = "idle"
    message: str = ""
    timestamp: float = field(default_factory=now_ts)

    @property
    def dispatched(self) -> bool:
        return bool(self.loop and self.loop.execution and self.loop.execution.success)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "device_id": self.device_id,
            "trace_id": self.trace_id,
            "stage": self.stage,
            "message": self.message,
            "timestamp": self.timestamp,
            "dispatched": self.dispatched,
            "condition": self.condition.to_dict() if self.condition else None,
            "health": self.health.to_dict() if self.health else None,
            "advice": self.advice.to_dict() if self.advice else None,
            "loop": self.loop.to_dict() if self.loop else None,
        }

    def summary(self) -> str:
        """一行摘要，供日志与命令行输出。"""
        parts = [f"[{self.device_id}]"]
        if self.condition:
            parts.append(f"工况={self.condition.label}"
                         f"({self.condition.stability.label})")
        if self.health:
            parts.append(f"健康={self.health.score:.3f}({self.health.grade.label})")
        if self.advice and self.advice.changes:
            parts.append(f"建议={len(self.advice.changes)}项")
        parts.append(f"阶段={self.stage}")
        if self.message:
            parts.append(self.message)
        return " ".join(parts)


class ControlEngine:
    """多工况自适应工业设备控制优化引擎。"""

    def __init__(self, config: Optional[Config] = None,
                 knowledge: Optional[KnowledgeBase] = None):
        self.config = config or Config.load()
        setup_logging(self.config.get("system.log_level", "INFO"),
                      self.config.get("system.log_dir") or None)

        # ---- 基础设施 ----
        self.bus = EventBus(history_size=4000)
        self.scheduler = Scheduler()
        self.registry = DeviceRegistry()
        self.audit = AuditTrail(self.config.get("storage.audit_file",
                                                "data/store/audit.jsonl"))
        self.buffer = TimeSeriesBuffer(
            retain_seconds=self.config.get_float("acquisition.buffer_seconds", 1800.0)
        )
        self.cleanser = DataCleanser(self.config.section("acquisition.cleansing"),
                                     self.buffer)

        # ---- 功能层 ----
        self.gateway = AcquisitionGateway(
            self.registry, self.buffer, self.bus, self.scheduler,
            self.cleanser, self.audit,
            poll_interval_ms=self.config.get_int("acquisition.poll_interval_ms", 1000),
        )
        self.extractor = FeatureExtractor(
            self.buffer,
            window_seconds=self.config.get_float("features.window_seconds", 10.0),
            bands=self.config.get("features.spectrum_bands"),
            enable_spectrum=self.config.get_bool("features.enable_spectrum", True),
            enable_envelope=self.config.get_bool("features.envelope_analysis", True),
        )
        self.recognizer = ConditionRecognizer(
            bus=self.bus,
            novelty_threshold=self.config.get_float("condition.novelty_threshold", 3.5),
            steady_cv_threshold=self.config.get_float("condition.steady_cv_threshold", 0.05),
            steady_min_dwell_s=self.config.get_float("condition.steady_min_dwell_s", 60.0),
            debounce_count=self.config.get_int("condition.debounce_count", 3),
            max_conditions=self.config.get_int("condition.max_conditions", 32),
            steady_min_samples=self.config.get_int("condition.steady_min_samples", 12),
            window_points=self.config.get_int("condition.window_points", 30),
        )
        self.baselines = BaselineLibrary(
            min_samples=self.config.get_int("health.baseline_min_samples", 50)
        )
        self.assessor = HealthAssessor(
            baselines=self.baselines, bus=self.bus,
            weights=self.config.get("health.weights"),
            ewma_alpha=self.config.get_float("health.ewma_alpha", 0.2),
            ewma_sigma_k=self.config.get_float("health.ewma_sigma_k", 3.0),
            trend_window_hours=self.config.get_float("health.trend_window_hours", 72.0),
        )
        self.knowledge = knowledge or build_default_knowledge_base(
            self.config.get("knowledge.rule_file", ""),
            self.config.get("knowledge.case_file", ""),
        )
        self.advisor = OptimizationAdvisor(
            knowledge=self.knowledge, bus=self.bus,
            algorithm=self.config.get("optimization.algorithm", "auto"),
            max_iterations=self.config.get_int("optimization.max_iterations", 60),
            max_delta_ratio=self.config.get_float("control.max_delta_ratio", 0.10),
            max_changed_params=self.config.get_int("optimization.max_changed_params", 3),
            min_confidence=self.config.get_float("optimization.min_confidence", 0.55),
            surrogate_min_samples=self.config.get_int(
                "optimization.surrogate_min_samples", 12),
            objective_weights=self.config.get("optimization.objective_weights"),
        )
        self.safety = SafetyGate(
            mode=ControlMode.from_text(self.config.get("control.mode"),
                                       ControlMode.ADVISORY),
            max_delta_ratio=self.config.get_float("control.max_delta_ratio", 0.10),
            min_dispatch_interval_s=self.config.get_float(
                "control.min_dispatch_interval_s", 300.0),
            max_dispatch_per_hour=self.config.get_int("control.max_dispatch_per_hour", 6),
            min_confidence=self.config.get_float("optimization.min_confidence", 0.55),
            block_when_grade=self.config.get("control.block_when_grade", "degraded"),
            approval_required_above=self.config.get(
                "control.approval_required_above", "warning"),
            interlocks=default_interlocks(),
        )
        self.evaluator = EffectEvaluator(
            self.buffer,
            before_seconds=self.config.get_float("feedback.before_seconds", 600.0),
            after_seconds=self.config.get_float("feedback.after_seconds", 600.0),
            settle_seconds=self.config.get_float("feedback.settle_seconds", 120.0),
            t_threshold=self.config.get_float("feedback.t_threshold", 2.0),
        )
        self.learner = FeedbackLearner(
            evaluator=self.evaluator,
            reward_calc=RewardCalculator(),
            scheduler=self.scheduler, bus=self.bus, audit=self.audit,
            accept_reward=self.config.get_float("feedback.accept_reward", 0.0),
            rule_credit_step=self.config.get_float("feedback.rule_credit_step", 0.05),
        )
        self.learner.on_learned = self._on_learned

        # ---- 运行期状态 ----
        self._adapters: Dict[str, ControlAdapter] = {}
        self._controllers: Dict[str, ClosedLoopController] = {}
        self._scenarios: Dict[str, ScenarioProfile] = {}
        self._records: Dict[str, DecisionRecord] = {}
        self._cycles: List[CycleResult] = []
        self._pending_context: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._running = False
        self.cycle_count = 0

        logger.info("控制引擎已初始化：模式=%s，算法=%s",
                    self.safety.mode.label, self.advisor.algorithm)

    # ==================================================================
    # 装配
    # ==================================================================
    def register_device(self, profile: DeviceProfile, driver: ProtocolDriver,
                        adapter: Optional[ControlAdapter] = None,
                        scenario_id: str = "") -> Collector:
        """注册一台设备：档案 + 采集驱动 + 控制适配器 + 场景配置。"""
        self.registry.register(profile, override=True)
        collector = self.gateway.bind(profile.device_id, driver)

        if adapter is not None:
            self._adapters[profile.device_id] = adapter
            executor = CommandExecutor(
                adapter, self.bus, self.audit,
                auto_rollback=self.config.get_bool("control.auto_rollback", True),
            )
            self._controllers[profile.device_id] = ClosedLoopController(
                executor, self.bus, self.audit, on_dispatched=self._on_dispatched
            )

        sid = scenario_id or profile.scenario
        if sid:
            try:
                scenario = get_scenario(sid)
                self.apply_scenario(profile.device_id, scenario)
            except KeyError as exc:
                logger.warning("场景装配失败: %s", exc)
        return collector

    def apply_scenario(self, device_id: str, scenario: ScenarioProfile) -> None:
        """把场景配置应用到指定设备。"""
        self._scenarios[device_id] = scenario
        for rule in scenario.extra_rules:
            self.knowledge.rules.add(rule)
        for constraint in scenario.extra_constraints:
            self.knowledge.constraints.add(constraint)
        if scenario.objectives:
            self.advisor.objectives = list(scenario.objectives)
        if scenario.objective_weights:
            self.advisor.objective_weights = dict(scenario.objective_weights)
        if scenario.signature_keys:
            self.recognizer.signature_keys = tuple(scenario.signature_keys)
        overrides = scenario.control_overrides
        if "max_delta_ratio" in overrides:
            self.safety.max_delta_ratio = float(overrides["max_delta_ratio"])
            self.advisor.max_delta_ratio = float(overrides["max_delta_ratio"])
        if "min_dispatch_interval_s" in overrides:
            self.safety.min_dispatch_interval_s = float(overrides["min_dispatch_interval_s"])
        logger.info("设备 %s 已装配场景「%s」：%d 条规则、%d 条约束",
                    device_id, scenario.name,
                    len(scenario.extra_rules), len(scenario.extra_constraints))

    def set_mode(self, mode: ControlMode) -> None:
        """切换控制模式。这是现场最重要的一个开关。"""
        self.safety.set_mode(mode)
        self.audit.record("config", detail={"control_mode": mode.code})

    # ==================================================================
    # 决策周期
    # ==================================================================
    def decision_cycle(self, device_id: str, poll: bool = True) -> CycleResult:
        """执行一次完整决策。"""
        profile = self.registry.get(device_id)
        result = CycleResult(device_id=device_id, stage="acquiring")
        collector = self.gateway.collector(device_id)

        # ---- 1. 数据接入 ----
        frame: Optional[SampleFrame] = None
        if poll and collector is not None:
            frame = collector.poll_once()
        if frame is None:
            frame = self.buffer.latest_frame(device_id)
        if frame is None:
            result.stage = "done"
            result.message = "无可用采集数据"
            return self._finish(result)
        result.frame = frame
        result.trace_id = frame.trace_id

        with TraceScope(frame.trace_id, device_id):
            # ---- 2. 特征提取 ----
            result.stage = "extracting"
            features = self.extractor.extract(profile, frame)
            result.features = features
            self.bus.publish(EventTopic.FEATURE_READY, features,
                             device_id=device_id, trace_id=frame.trace_id)

            # ---- 3. 工况识别 ----
            result.stage = "recognizing"
            condition = self.recognizer.recognize(features)
            result.condition = condition
            self.audit.record(ACTION_CONDITION, device_id=device_id,
                              trace_id=frame.trace_id,
                              detail={"condition": condition.to_dict()})

            # ---- 4. 健康评估 ----
            result.stage = "assessing"
            health = self.assessor.assess(profile, features, condition)
            result.health = health
            self.audit.record(ACTION_HEALTH, device_id=device_id,
                              trace_id=frame.trace_id,
                              detail={"score": round(health.score, 4),
                                      "grade": health.grade.code,
                                      "anomalies": health.anomalies[:5]})

            # ---- 5. 喂养代理模型 ----
            current_params = self._current_params(profile)
            metrics = self._current_metrics(profile, features)
            if condition.stability.code == "steady":
                self.advisor.record_operating_point(profile, current_params, metrics)

            # ---- 6. 生成建议 ----
            result.stage = "advising"
            advice = self.advisor.advise(profile, condition, health,
                                         current_params, metrics)
            result.advice = advice
            self.audit.record(ACTION_ADVICE, device_id=device_id,
                              trace_id=frame.trace_id,
                              detail={"advice": advice.to_dict()})

            self._save_record(result)

            if advice.is_empty:
                result.stage = "done"
                result.message = advice.explanation or "本轮无调整建议"
                return self._finish(result)

            # ---- 7. 安全校验 + 闭环下发 ----
            controller = self._controllers.get(device_id)
            if controller is None:
                result.stage = "done"
                result.message = "未配置控制适配器，建议仅作展示"
                return self._finish(result)

            result.stage = "validating"
            facts = self._safety_facts(profile, frame, features, health)
            verdict = self.safety.validate(advice, profile, health, facts,
                                           current_params)

            # 缓存上下文，供下发成功后安排效果评估
            if verdict.command is not None:
                with self._lock:
                    self._pending_context[verdict.command.command_id] = {
                        "profile": profile, "advice": advice,
                        "condition": condition, "health": health,
                        "rule_ids": [e.source_id for e in advice.evidences
                                     if e.kind.code == "rule"],
                    }

            loop = controller.submit(verdict, profile)
            result.loop = loop
            result.stage = loop.stage
            result.message = loop.outcome
            if loop.execution and loop.execution.success:
                self.safety.record_dispatch(device_id)
            self._save_record(result)
            return self._finish(result)

    # ==================================================================
    # 回调
    # ==================================================================
    def _on_dispatched(self, command: ControlCommand,
                       record: ExecutionRecord) -> None:
        """下发成功后安排延时效果评估。"""
        with self._lock:
            ctx = self._pending_context.pop(command.command_id, None)
        if ctx is None:
            logger.warning("指令 %s 缺少上下文，跳过效果评估排期", command.command_id)
            return
        condition: ConditionSnapshot = ctx["condition"]
        health: HealthReport = ctx["health"]
        self.learner.schedule_evaluation(
            profile=ctx["profile"], command=command, advice=ctx["advice"],
            rule_ids=ctx["rule_ids"], condition_id=condition.condition_id,
            signature=condition.signature, health_score=health.score,
        )

    def _on_learned(self, evaluation: Any, case: ExperienceCase) -> None:
        """学习结果回写：知识库 + 工况档案 + 代理模型。"""
        rule_ids: List[str] = []
        with self._lock:
            for ctx in self._pending_context.values():
                rule_ids.extend(ctx.get("rule_ids", []))
        self.knowledge.learn(case, rule_ids, evaluation.accepted,
                             self.learner.rule_credit_step)
        if case.condition_id:
            self.recognizer.record_best(case.device_id, case.condition_id,
                                        case.params_after, case.reward)
        logger.info("知识库已更新：案例 %s（奖励 %+.4f，%s）", case.case_id,
                    case.reward, "采纳" if case.accepted else "不采纳")

    # ==================================================================
    # 辅助
    # ==================================================================
    def _current_params(self, profile: DeviceProfile) -> Dict[str, float]:
        """读取各可调参数的当前值。"""
        out: Dict[str, float] = {}
        for param_id, spec in profile.parameters.items():
            value = None
            if spec.write_point_id:
                value = self.buffer.latest(profile.device_id, spec.write_point_id)
            if value is None:
                adapter = self._adapters.get(profile.device_id)
                if adapter is not None and spec.write_point_id:
                    try:
                        value = adapter.read(spec.write_point_id)
                    except Exception:  # noqa: BLE001
                        value = None
            out[param_id] = float(value) if value is not None \
                else (spec.lower + spec.upper) / 2
        return out

    def _current_metrics(self, profile: DeviceProfile,
                         features: FeatureVector) -> Dict[str, float]:
        """把当前运行指标折算成优化目标的观测值。"""
        device_id = profile.device_id
        power = self.buffer.latest(device_id, self._first_point(profile, SignalKind.POWER))
        flow = self.buffer.latest(device_id, self._first_point(profile, SignalKind.FLOW))
        vib = features.get("dev.temp_max", 0.0)
        metrics: Dict[str, float] = {}
        if power is not None and flow is not None and flow > 1e-6:
            metrics["energy"] = power / flow
        elif power is not None:
            metrics["energy"] = power
        if flow is not None:
            metrics["throughput"] = flow
        vib_points = profile.points_of_kind(SignalKind.VIBRATION)
        if vib_points:
            rms = features.get(f"{vib_points[0].point_id}.rms", 0.0)
            metrics["health_cost"] = rms
        metrics.setdefault("health_cost", vib)
        return metrics

    @staticmethod
    def _first_point(profile: DeviceProfile, kind: SignalKind) -> str:
        point = profile.first_point_of(kind)
        return point.point_id if point else ""

    def _safety_facts(self, profile: DeviceProfile, frame: SampleFrame,
                      features: FeatureVector, health: HealthReport) -> Dict[str, Any]:
        """组装安全门与互锁所需的事实。"""
        temp_limit = profile.nameplate_of("temp_alarm_c", 0.0)
        temp_max = features.get("dev.temp_max", 0.0)
        return {
            "good_ratio": frame.good_ratio,
            "maintenance_mode": bool(self.config.get("control.maintenance_mode", False)),
            "emergency_stop": bool(self.config.get("control.emergency_stop", False)),
            "temp_over_limit": bool(temp_limit > 0 and temp_max > temp_limit),
            "health_score": health.score,
            "health_grade": health.grade.code,
        }

    def _save_record(self, result: CycleResult) -> None:
        if not result.trace_id:
            return
        record = self._records.get(result.trace_id) or DecisionRecord(
            trace_id=result.trace_id, device_id=result.device_id
        )
        if result.frame:
            record.frame = {"timestamp": result.frame.timestamp,
                            "good_ratio": round(result.frame.good_ratio, 4),
                            "points": len(result.frame.samples)}
        if result.condition:
            record.condition = result.condition.to_dict()
        if result.health:
            record.health = result.health.to_dict()
        if result.advice:
            record.advice = result.advice.to_dict()
        if result.loop and result.loop.command:
            record.command = result.loop.command.to_dict()
        if result.loop and result.loop.execution:
            record.execution = result.loop.execution.to_dict()
        with self._lock:
            self._records[result.trace_id] = record
            if len(self._records) > 1000:
                for key in list(self._records)[:200]:
                    self._records.pop(key, None)

    def _finish(self, result: CycleResult) -> CycleResult:
        with self._lock:
            self.cycle_count += 1
            self._cycles.append(result)
            if len(self._cycles) > 200:
                del self._cycles[: len(self._cycles) - 200]
        logger.info("决策周期完成 %s", result.summary())
        return result

    # ==================================================================
    # 生命周期
    # ==================================================================
    def start(self, decision_interval_s: float = 30.0) -> None:
        """启动引擎：采集任务 + 决策任务。"""
        if self._running:
            return
        self._running = True
        self.scheduler.start()
        self.gateway.start()
        for device_id in self.registry.device_ids():
            self.scheduler.every(
                decision_interval_s,
                lambda d=device_id: self.decision_cycle(d, poll=False),
                name=f"decide:{device_id}", first_delay=decision_interval_s,
            )
        logger.info("引擎已启动：%d 台设备，决策周期 %.0fs",
                    len(self.registry), decision_interval_s)

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self.gateway.stop()
        self.scheduler.stop()
        self.bus.stop()
        logger.info("引擎已停止")

    @property
    def running(self) -> bool:
        return self._running

    # ==================================================================
    # 对外查询
    # ==================================================================
    def controller(self, device_id: str) -> Optional[ClosedLoopController]:
        return self._controllers.get(device_id)

    def approve(self, device_id: str, command_id: str, approver: str) -> LoopContext:
        """人工审批通过一条待下发指令。"""
        controller = self._controllers.get(device_id)
        if controller is None:
            raise KeyError(f"设备 {device_id} 未配置控制适配器")
        ctx = controller.approve(command_id, approver)
        if ctx.execution and ctx.execution.success:
            self.safety.record_dispatch(device_id)
        return ctx

    def reject(self, device_id: str, command_id: str, approver: str,
               reason: str) -> bool:
        controller = self._controllers.get(device_id)
        return bool(controller and controller.reject(command_id, approver, reason))

    def pending_approvals(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for device_id, controller in self._controllers.items():
            for cmd in controller.pending_commands():
                cmd["device_id"] = device_id
                out.append(cmd)
        return out

    def decision_record(self, trace_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            record = self._records.get(trace_id)
        return record.to_dict() if record else None

    def recent_cycles(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            return [c.to_dict() for c in self._cycles[-limit:]]

    def status(self) -> Dict[str, Any]:
        """系统总体状态，供接口层与命令行展示。"""
        return {
            "system": {
                "name": self.config.get("system.name"),
                "version": self.config.get("system.version"),
                "instance": self.config.get("system.instance_id"),
                "running": self._running,
                "cycles": self.cycle_count,
                "control_mode": self.safety.mode.code,
                "control_mode_label": self.safety.mode.label,
            },
            "devices": [d.to_dict() for d in self.registry.all()],
            "acquisition": {
                "collectors": self.gateway.health(),
                "buffer": self.buffer.stats(),
                "cleansing": self.cleanser.counters(),
            },
            "condition": self.recognizer.to_dict(),
            "safety": self.safety.stats(),
            "control": {d: c.stats() for d, c in self._controllers.items()},
            "optimization": self.advisor.stats(),
            "feedback": self.learner.stats(),
            "knowledge": {
                "rules": len(self.knowledge.rules.rules()),
                "constraints": len(self.knowledge.constraints.all()),
                "cases": self.knowledge.cases.stats(),
            },
            "scheduler": {"tasks": self.scheduler.stats(),
                          "pending": self.scheduler.pending_count()},
            "audit": {"records": self.audit.size},
            "events": {"published": self.bus.published_count},
        }


__all__ = ["ControlEngine", "CycleResult"]

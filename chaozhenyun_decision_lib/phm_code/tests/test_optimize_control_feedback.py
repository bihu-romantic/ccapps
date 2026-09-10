"""
优化、控制与反馈层测试
======================

覆盖：搜索空间与限幅、代理模型、寻优算法、安全门七道检查、
指令执行与回滚、效果评估与奖励、端到端闭环。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maco.bootstrap import build_demo_system                               # noqa: E402
from maco.control.safety import Interlock, SafetyGate                      # noqa: E402
from maco.core.enums import (                                              # noqa: E402
    CommandState, ControlMode, HealthGrade, ObjectiveSense, ParamType, Severity,
)
from maco.core.errors import InterlockActive                               # noqa: E402
from maco.core.registry import DeviceProfile                               # noqa: E402
from maco.core.types import (                                              # noqa: E402
    HealthReport, ObjectiveSpec, OptimizationAdvice, ParameterChange,
    ParameterSpec, SignalPoint,
)
from maco.core.enums import SignalKind                                     # noqa: E402
from maco.feedback.learner import RewardCalculator, ObservedMetric         # noqa: E402
from maco.core.types import EffectEvaluation, MetricComparison             # noqa: E402
from maco.optimization.advisor import parse_adjustment                     # noqa: E402
from maco.optimization.optimizer import (                                  # noqa: E402
    NSGA2Optimizer, ParticleSwarmOptimizer, SearchSpace, SurrogateModel,
    build_optimizer,
)


def _spec(pid="freq", lo=30.0, hi=50.0, delta=2.0) -> ParameterSpec:
    return ParameterSpec(param_id=pid, name="频率", lower=lo, upper=hi,
                         unit="Hz", max_delta=delta, write_point_id=f"{pid}_sp")


class TestSearchSpace(unittest.TestCase):
    def test_normalize_roundtrip(self):
        space = SearchSpace([_spec()])
        unit = space.to_unit({"freq": 40.0})
        self.assertAlmostEqual(unit[0], 0.5, places=6)
        self.assertAlmostEqual(space.to_values(unit)["freq"], 40.0, places=6)

    def test_restrict_limits_neighbourhood(self):
        space = SearchSpace([_spec()])
        bounds = space.restrict({"freq": 45.0}, 0.05)
        lo, hi = bounds[0]
        # 45 ± min(45*5%, max_delta=2) = 45 ± 2 → [43, 47] → 归一化 [0.65, 0.85]
        self.assertAlmostEqual(lo, 0.65, places=4)
        self.assertAlmostEqual(hi, 0.85, places=4)

    def test_clip_respects_bounds(self):
        spec = _spec()
        self.assertEqual(spec.clip(99.0), 50.0)
        self.assertEqual(spec.clip(1.0), 30.0)


class TestSurrogate(unittest.TestCase):
    def _model(self) -> SurrogateModel:
        space = SearchSpace([_spec()])
        objs = [ObjectiveSpec("energy", "能耗", ObjectiveSense.MINIMIZE, 1.0)]
        model = SurrogateModel(space, objs, min_samples=5)
        # 构造 "频率越低能耗越低" 的样本
        for freq in (32.0, 36.0, 40.0, 44.0, 48.0, 50.0):
            model.add_sample({"freq": freq}, {"energy": (freq / 50.0) ** 3 * 55.0})
        return model

    def test_prediction_follows_trend(self):
        model = self._model()
        low, _ = model.predict(model.space.to_unit({"freq": 33.0}))
        high, _ = model.predict(model.space.to_unit({"freq": 49.0}))
        self.assertLess(low["energy"], high["energy"])

    def test_confidence_drops_without_samples(self):
        space = SearchSpace([_spec()])
        empty = SurrogateModel(space, [ObjectiveSpec("energy", "能耗")], min_samples=5)
        _, conf = empty.predict([0.5])
        self.assertEqual(conf, 0.0)
        self.assertFalse(empty.trained)

    def test_scalarize_prefers_lower_cost(self):
        model = self._model()
        better = model.scalarize({"energy": 10.0}, {"energy": 1.0})
        worse = model.scalarize({"energy": 50.0}, {"energy": 1.0})
        self.assertGreater(better, worse)


class TestOptimizers(unittest.TestCase):
    def setUp(self):
        self.space = SearchSpace([_spec(lo=0.0, hi=1.0, delta=1.0)])

    def _objective(self, unit):
        # 最优点在 0.3 处
        return -abs(float(unit[0]) - 0.3)

    def test_pso_converges(self):
        opt = ParticleSwarmOptimizer(self.space, max_iterations=30, swarm_size=15)
        result = opt.optimize(self._objective, [(0.0, 1.0)])
        self.assertAlmostEqual(result.best_params["freq"], 0.3, delta=0.05)

    def test_nsga2_returns_pareto(self):
        opt = NSGA2Optimizer(self.space, max_iterations=10, population_size=16)
        result = opt.optimize_multi(
            lambda u: [-abs(float(u[0]) - 0.2), -abs(float(u[0]) - 0.8)],
            [(0.0, 1.0)],
        )
        self.assertGreater(len(result.pareto), 1)

    def test_auto_selects_by_objective_count(self):
        multi = build_optimizer("auto", self.space, objective_count=3, sample_count=50)
        self.assertEqual(multi.name, "nsga2")
        few = build_optimizer("auto", self.space, objective_count=1, sample_count=5)
        self.assertEqual(few.name, "bayesian")

    def test_results_are_reproducible(self):
        a = ParticleSwarmOptimizer(self.space, max_iterations=10).optimize(
            self._objective, [(0.0, 1.0)])
        b = ParticleSwarmOptimizer(self.space, max_iterations=10).optimize(
            self._objective, [(0.0, 1.0)])
        self.assertAlmostEqual(a.best_params["freq"], b.best_params["freq"], places=9)


class TestAdjustmentParsing(unittest.TestCase):
    def test_percent_and_absolute(self):
        spec = _spec()
        self.assertAlmostEqual(parse_adjustment(40.0, "-5%", spec), 38.0)
        self.assertAlmostEqual(parse_adjustment(40.0, "+2", spec), 42.0)
        self.assertAlmostEqual(parse_adjustment(40.0, "=46", spec), 46.0)

    def test_clipped_to_bounds(self):
        spec = _spec()
        self.assertEqual(parse_adjustment(48.0, "+10", spec), 50.0)

    def test_invalid_returns_none(self):
        self.assertIsNone(parse_adjustment(40.0, "增大一点", _spec()))


def _safety_profile() -> DeviceProfile:
    p = DeviceProfile(device_id="D1", name="设备")
    p.add_point(SignalPoint("freq_sp", "频率设定", SignalKind.SETPOINT, "Hz",
                            low_limit=30, high_limit=50, device_id="D1",
                            writable=True))
    p.add_parameter(_spec())
    return p


def _advice(current=45.0, proposed=44.0, confidence=0.8) -> OptimizationAdvice:
    return OptimizationAdvice(
        device_id="D1", confidence=confidence, risk=Severity.INFO,
        changes=[ParameterChange("freq", "频率", current, proposed, "Hz", "freq_sp")],
    )


def _health(score=0.9) -> HealthReport:
    return HealthReport(device_id="D1", condition_id="C1", score=score,
                        grade=HealthGrade.from_score(score))


class TestSafetyGate(unittest.TestCase):
    def test_observe_mode_blocks_write(self):
        gate = SafetyGate(mode=ControlMode.OBSERVE)
        v = gate.validate(_advice(), _safety_profile(), _health())
        self.assertFalse(v.passed)
        self.assertEqual(v.rule_id, "SG-01")

    def test_low_confidence_blocked(self):
        gate = SafetyGate(mode=ControlMode.AUTONOMOUS, min_confidence=0.7,
                          min_dispatch_interval_s=0)
        v = gate.validate(_advice(confidence=0.4), _safety_profile(), _health())
        self.assertFalse(v.passed)
        self.assertEqual(v.rule_id, "SG-03")

    def test_degraded_health_blocked(self):
        gate = SafetyGate(mode=ControlMode.AUTONOMOUS, min_dispatch_interval_s=0)
        v = gate.validate(_advice(), _safety_profile(), _health(score=0.5))
        self.assertFalse(v.passed)
        self.assertEqual(v.rule_id, "SG-04")

    def test_delta_limit_blocked(self):
        gate = SafetyGate(mode=ControlMode.AUTONOMOUS, min_dispatch_interval_s=0)
        v = gate.validate(_advice(proposed=35.0), _safety_profile(), _health())
        self.assertFalse(v.passed)
        self.assertIn(v.rule_id, ("SG-11", "SG-12"))

    def test_valid_advice_passes(self):
        gate = SafetyGate(mode=ControlMode.AUTONOMOUS, min_dispatch_interval_s=0,
                          max_delta_ratio=0.10)
        v = gate.validate(_advice(45.0, 44.0), _safety_profile(), _health())
        self.assertTrue(v.passed, v.reason)
        self.assertEqual(v.command.state, CommandState.VALIDATED)
        self.assertEqual(v.command.items[0].point_id, "freq_sp")

    def test_supervised_mode_requires_approval(self):
        gate = SafetyGate(mode=ControlMode.SUPERVISED, min_dispatch_interval_s=0)
        v = gate.validate(_advice(45.0, 44.0), _safety_profile(), _health())
        self.assertTrue(v.passed)
        self.assertEqual(v.command.state, CommandState.PENDING_APPROVAL)

    def test_interlock_raises(self):
        gate = SafetyGate(mode=ControlMode.AUTONOMOUS, min_dispatch_interval_s=0,
                          interlocks=[Interlock("IL", "检修",
                                                lambda f: f.get("maintenance", False),
                                                "检修中")])
        with self.assertRaises(InterlockActive):
            gate.validate(_advice(45.0, 44.0), _safety_profile(), _health(),
                          facts={"maintenance": True})

    def test_rate_limit(self):
        gate = SafetyGate(mode=ControlMode.AUTONOMOUS, min_dispatch_interval_s=600)
        gate.record_dispatch("D1")
        v = gate.validate(_advice(45.0, 44.0), _safety_profile(), _health())
        self.assertFalse(v.passed)
        self.assertEqual(v.rule_id, "SG-13")


class TestReward(unittest.TestCase):
    def test_energy_drop_gives_positive_reward(self):
        calc = RewardCalculator()
        ev = EffectEvaluation(command_id="C1")
        ev.comparisons.append(MetricComparison(
            metric="power", label="功率", before_mean=50.0, after_mean=45.0,
            t_stat=-4.0, significant=True,
        ))
        reward = calc.compute(ev, [ObservedMetric("power", "功率", better="down")])
        self.assertGreater(reward, 0)

    def test_penalty_is_asymmetric(self):
        calc = RewardCalculator(penalty_factor=2.0)
        good = EffectEvaluation(command_id="C1")
        good.comparisons.append(MetricComparison("power", "功率", 50.0, 45.0,
                                                 t_stat=-4.0, significant=True))
        bad = EffectEvaluation(command_id="C2")
        bad.comparisons.append(MetricComparison("power", "功率", 45.0, 50.0,
                                                t_stat=4.0, significant=True))
        metrics = [ObservedMetric("power", "功率", better="down")]
        self.assertGreater(abs(calc.compute(bad, metrics)),
                           abs(calc.compute(good, metrics)))

    def test_insignificant_change_is_discounted(self):
        calc = RewardCalculator()
        metrics = [ObservedMetric("power", "功率", better="down")]
        sig = EffectEvaluation(command_id="C1")
        sig.comparisons.append(MetricComparison("power", "功率", 50.0, 45.0,
                                                t_stat=-4.0, significant=True))
        insig = EffectEvaluation(command_id="C2")
        insig.comparisons.append(MetricComparison("power", "功率", 50.0, 45.0,
                                                  t_stat=-0.3, significant=False))
        self.assertGreater(calc.compute(sig, metrics), calc.compute(insig, metrics))


class TestEndToEnd(unittest.TestCase):
    """端到端：装配 → 采集 → 决策 → 闭环，验证链路贯通。"""

    def test_full_pipeline_runs(self):
        engine, sims = build_demo_system(mode=ControlMode.AUTONOMOUS)
        device = "FAN-01"
        collector = engine.gateway.collector(device)
        for _ in range(40):
            collector.poll_once()

        result = None
        for _ in range(15):
            result = engine.decision_cycle(device)

        self.assertIsNotNone(result.condition)
        self.assertIsNotNone(result.health)
        self.assertGreater(engine.buffer.coverage(device)["total_samples"], 100)
        self.assertGreaterEqual(len(engine.recognizer.library(device).all()), 1)
        # 工况库不得失控膨胀
        self.assertLess(len(engine.recognizer.library(device).all()), 20)
        self.assertTrue(engine.audit.verify_chain()["ok"])

    def test_fault_injection_is_detected(self):
        engine, sims = build_demo_system(mode=ControlMode.ADVISORY)
        device = "FAN-01"
        collector = engine.gateway.collector(device)
        for _ in range(30):
            collector.poll_once()
        for _ in range(10):
            engine.decision_cycle(device)

        sims[device].inject_fault("bearing_outer", severity=0.9)
        sims[device].degradation = 0.6
        result = None
        for _ in range(10):
            result = engine.decision_cycle(device)
        patterns = [p.code for p, _ in result.health.patterns]
        self.assertTrue(any("bearing" in p for p in patterns),
                        f"未识别出轴承故障：{patterns}")


if __name__ == "__main__":
    unittest.main(verbosity=2)

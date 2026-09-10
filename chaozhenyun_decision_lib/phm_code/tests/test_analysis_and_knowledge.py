"""
特征、工况、健康与知识层测试
============================

覆盖：时域/频域特征、轴承特征频率、工况识别与稳态判定、
健康基线与偏离评分、规则推理与约束、案例复用。
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maco.condition.recognizer import ConditionRecognizer                  # noqa: E402
from maco.core.enums import ConditionStability, HealthGrade, Severity      # noqa: E402
from maco.core.types import ExperienceCase, FeatureVector                  # noqa: E402
from maco.features.extractor import (                                      # noqa: E402
    bearing_frequencies, peak_at, spectral_features, spectrum,
    time_domain_features,
)
from maco.health.assessor import Baseline, BaselineLibrary                 # noqa: E402
from maco.knowledge.base import (                                          # noqa: E402
    CaseBase, Constraint, ConstraintSet, Rule, RuleEngine, evaluate_condition,
)
from maco.knowledge.presets import build_default_knowledge_base            # noqa: E402


class TestTimeDomain(unittest.TestCase):
    def test_rms_of_sine(self):
        t = np.linspace(0, 1, 2000, endpoint=False)
        sig = 2.0 * np.sin(2 * math.pi * 10 * t)
        f = time_domain_features(sig.tolist())
        # 正弦有效值 = 幅值 / √2
        self.assertAlmostEqual(f["rms"], 2.0 / math.sqrt(2), places=2)
        self.assertAlmostEqual(f["peak"], 2.0, places=2)

    def test_kurtosis_of_gaussian_is_near_three(self):
        rng = np.random.default_rng(42)
        f = time_domain_features(rng.normal(0, 1, 20000).tolist())
        self.assertAlmostEqual(f["kurtosis"], 3.0, delta=0.3)

    def test_impulsive_signal_has_high_kurtosis(self):
        sig = np.zeros(2000)
        sig[::200] = 10.0
        f = time_domain_features(sig.tolist())
        self.assertGreater(f["kurtosis"], 10.0)

    def test_short_input_returns_empty(self):
        self.assertEqual(time_domain_features([1.0]), {})


class TestFrequencyDomain(unittest.TestCase):
    def test_dominant_frequency(self):
        fs = 2000.0
        t = np.arange(0, 2.0, 1 / fs)
        sig = np.sin(2 * math.pi * 120 * t)
        f = spectral_features(sig.tolist(), fs, [[0, 100], [100, 200]])
        self.assertAlmostEqual(f["dominant_hz"], 120.0, delta=2.0)
        self.assertGreater(f["band_100_200"], f["band_0_100"])

    def test_peak_at_locates_component(self):
        fs = 2000.0
        t = np.arange(0, 2.0, 1 / fs)
        sig = np.sin(2 * math.pi * 50 * t) + 0.2 * np.sin(2 * math.pi * 300 * t)
        sp = spectrum(sig.tolist(), fs)
        self.assertGreater(peak_at(sp["freq"], sp["amp"], 50.0),
                           peak_at(sp["freq"], sp["amp"], 300.0))

    def test_bearing_frequency_relations(self):
        f = bearing_frequencies(shaft_hz=30.0, n_rollers=9, roller_d=12.0,
                                pitch_d=60.0)
        # 内圈通过频率恒大于外圈；保持架频率恒小于轴频
        self.assertGreater(f["bpfi"], f["bpfo"])
        self.assertLess(f["ftf"], 30.0)
        # 外圈 + 内圈 = 滚子数 × 轴频
        self.assertAlmostEqual(f["bpfo"] + f["bpfi"], 9 * 30.0, places=4)


class TestConditionRecognizer(unittest.TestCase):
    def _fv(self, load: float, speed: float, ts: float) -> FeatureVector:
        return FeatureVector(device_id="D1", timestamp=ts,
                             values={"dev.load_ratio": load, "dev.speed_ratio": speed})

    def test_same_condition_is_not_recreated(self):
        """同一工况反复出现时不得每拍都新建工况——这是工况库失控的判据。"""
        rec = ConditionRecognizer(steady_min_dwell_s=1.0, debounce_count=1)
        ids = set()
        for i in range(40):
            snap = rec.recognize(self._fv(0.80 + (i % 3) * 0.002,
                                          0.90, 1000.0 + i))
            ids.add(snap.condition_id)
        self.assertLessEqual(len(ids), 2, f"工况被反复新建：{ids}")

    def test_distinct_load_creates_new_condition(self):
        rec = ConditionRecognizer(debounce_count=1)
        low = rec.recognize(self._fv(0.30, 0.55, 1000.0))
        for i in range(6):
            rec.recognize(self._fv(0.30, 0.55, 1001.0 + i))
        high = rec.recognize(self._fv(0.95, 0.99, 1100.0))
        self.assertNotEqual(low.condition_id, high.condition_id)

    def test_steady_after_enough_samples(self):
        rec = ConditionRecognizer(steady_min_dwell_s=1e9, steady_min_samples=10,
                                  debounce_count=1)
        snap = None
        for i in range(25):
            snap = rec.recognize(self._fv(0.75, 0.88, 1000.0 + i))
        self.assertEqual(snap.stability, ConditionStability.STEADY)
        self.assertTrue(snap.optimizable)

    def test_fluctuating_load_is_transient(self):
        rec = ConditionRecognizer(debounce_count=1)
        snap = None
        for i in range(25):
            snap = rec.recognize(self._fv(0.5 + 0.3 * (i % 2), 0.9, 1000.0 + i))
        self.assertNotEqual(snap.stability, ConditionStability.STEADY)


class TestBaseline(unittest.TestCase):
    def test_deviation_scales_with_spread(self):
        bl = Baseline(key="k", min_samples=10)
        for v in [10.0, 10.2, 9.8, 10.1, 9.9, 10.0, 10.3, 9.7, 10.05, 9.95]:
            bl.add(v)
        self.assertTrue(bl.ready)
        self.assertLess(abs(bl.deviation(10.0)), 0.5)
        self.assertGreater(bl.deviation(12.0), 3.0)

    def test_library_isolates_conditions(self):
        lib = BaselineLibrary(min_samples=5)
        for i in range(10):
            lib.get("D1", "C1", "f").add(10.0)
            lib.get("D1", "C2", "f").add(50.0)
        self.assertAlmostEqual(lib.get("D1", "C1", "f").median, 10.0)
        self.assertAlmostEqual(lib.get("D1", "C2", "f").median, 50.0)

    def test_health_grade_mapping(self):
        self.assertEqual(HealthGrade.from_score(0.95), HealthGrade.HEALTHY)
        self.assertEqual(HealthGrade.from_score(0.70), HealthGrade.ATTENTION)
        self.assertEqual(HealthGrade.from_score(0.50), HealthGrade.DEGRADED)
        self.assertEqual(HealthGrade.from_score(0.20), HealthGrade.CRITICAL)


class TestRuleEngine(unittest.TestCase):
    def test_condition_evaluation(self):
        facts = {"temp": 90.0, "load": 0.95, "mode": "auto"}
        self.assertTrue(evaluate_condition("temp > 85 and load > 0.9", facts))
        self.assertFalse(evaluate_condition("temp < 85", facts))
        self.assertTrue(evaluate_condition("mode == 'auto'", facts))

    def test_missing_fact_is_false_not_error(self):
        self.assertFalse(evaluate_condition("unknown > 1", {}))

    def test_forward_chaining(self):
        engine = RuleEngine([
            Rule("R1", "一级", "temp > 80", {"hot": True}, priority=90),
            Rule("R2", "二级", "hot == True", {"action": "cool"}, priority=80),
        ])
        result = engine.infer({"temp": 95.0})
        self.assertEqual(result.conclusions.get("action"), "cool")
        self.assertEqual(len(result.fired_rules), 2)

    def test_credit_updates_with_feedback(self):
        engine = RuleEngine([Rule("R1", "n", "x > 1", {"a": 1}, confidence=0.5)])
        engine.infer({"x": 5})
        before = engine.get("R1").confidence
        engine.reward(["R1"], accepted=True, step=0.1)
        self.assertGreater(engine.get("R1").confidence, before)
        engine.reward(["R1"], accepted=False, step=0.1)
        self.assertLess(engine.get("R1").confidence,
                        engine.get("R1").confidence + 0.2)

    def test_injection_attempt_is_rejected(self):
        from maco.core.errors import RuleSyntaxError
        with self.assertRaises(RuleSyntaxError):
            evaluate_condition("__import__('os').system('ls')", {})


class TestConstraints(unittest.TestCase):
    def test_hard_constraint_blocks(self):
        cs = ConstraintSet([Constraint("C1", "温度", "temp < 85", "hard")])
        self.assertTrue(cs.feasible({"temp": 70}))
        self.assertFalse(cs.feasible({"temp": 95}))

    def test_soft_constraint_only_penalizes(self):
        cs = ConstraintSet([Constraint("C2", "能效", "eff > 0.8", "soft", 0.5)])
        self.assertTrue(cs.feasible({"eff": 0.5}))
        self.assertAlmostEqual(cs.penalty({"eff": 0.5}), 0.5)


class TestCaseBase(unittest.TestCase):
    def test_retrieve_similar_case(self):
        cb = CaseBase(min_similarity=0.5)
        cb.add(ExperienceCase(
            case_id="C1", condition_id="COND-1",
            signature={"dev.load_ratio": 0.8, "dev.speed_ratio": 0.9},
            params_after={"freq": 43.0}, reward=0.15, accepted=True,
            credibility=0.8,
        ))
        matches = cb.retrieve({"dev.load_ratio": 0.81, "dev.speed_ratio": 0.89},
                              condition_id="COND-1")
        self.assertEqual(len(matches), 1)
        params = cb.reuse({"dev.load_ratio": 0.81, "dev.speed_ratio": 0.89},
                          "COND-1")
        self.assertAlmostEqual(params["freq"], 43.0, places=3)

    def test_revise_lowers_credibility_on_failure(self):
        cb = CaseBase()
        case = ExperienceCase(case_id="C1", credibility=0.6)
        cb.add(case)
        cb.revise("C1", reward=-0.2, accepted=False)
        self.assertLess(cb.all()[0].credibility, 0.6)


class TestDefaultKnowledge(unittest.TestCase):
    def test_safety_rules_block_optimization(self):
        kb = build_default_knowledge_base()
        result = kb.reason({"health_score": 0.3, "stability": "steady"})
        self.assertTrue(result.conclusions.get("block.optimize"))

    def test_non_steady_blocks(self):
        kb = build_default_knowledge_base()
        result = kb.reason({"health_score": 0.9, "stability": "transient"})
        self.assertTrue(result.conclusions.get("block.optimize"))

    def test_healthy_steady_does_not_block(self):
        kb = build_default_knowledge_base()
        result = kb.reason({"health_score": 0.92, "stability": "steady",
                            "condition_is_new": False})
        self.assertFalse(result.conclusions.get("block.optimize", False))


if __name__ == "__main__":
    unittest.main(verbosity=2)

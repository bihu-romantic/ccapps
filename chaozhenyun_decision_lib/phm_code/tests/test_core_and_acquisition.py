"""
内核层与数据接入层测试
======================

覆盖：配置校验、事件总线、审计链、Modbus 编解码、时序缓冲、数据清洗。
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maco.acquisition.buffer import TimeSeriesBuffer                      # noqa: E402
from maco.acquisition.cleansing import DataCleanser                       # noqa: E402
from maco.acquisition.drivers.modbus_tcp import (                         # noqa: E402
    decode_registers, encode_value, parse_address,
)
from maco.core.audit import AuditTrail                                    # noqa: E402
from maco.core.config import Config                                       # noqa: E402
from maco.core.enums import EventTopic, Quality, SignalKind               # noqa: E402
from maco.core.errors import ConfigurationError                           # noqa: E402
from maco.core.event_bus import EventBus                                  # noqa: E402
from maco.core.registry import DeviceProfile                              # noqa: E402
from maco.core.types import Sample, SampleFrame, SignalPoint              # noqa: E402


class TestConfig(unittest.TestCase):
    def test_default_load(self):
        cfg = Config.load(use_env=False)
        self.assertEqual(cfg.get("control.mode"), "advisory")
        self.assertGreater(cfg.get_float("features.window_seconds"), 0)

    def test_override_and_dot_path(self):
        cfg = Config.load(use_env=False, overrides={"control": {"mode": "autonomous"}})
        self.assertEqual(cfg.get("control.mode"), "autonomous")
        cfg.set("control.max_delta_ratio", 0.2)
        self.assertAlmostEqual(cfg.get_float("control.max_delta_ratio"), 0.2)

    def test_validation_rejects_bad_mode(self):
        with self.assertRaises(ConfigurationError):
            Config.load(use_env=False, overrides={"control": {"mode": "wild"}})

    def test_secrets_are_redacted(self):
        cfg = Config.load(use_env=False, overrides={"api": {"access_token": "abc123"}})
        self.assertEqual(cfg.dump()["api"]["access_token"], "***REDACTED***")


class TestEventBus(unittest.TestCase):
    def test_publish_and_filter(self):
        bus = EventBus()
        received = []
        bus.subscribe(EventTopic.FRAME_READY, lambda e: received.append(e))
        bus.publish(EventTopic.FRAME_READY, {"a": 1}, device_id="D1")
        bus.publish(EventTopic.HEALTH_UPDATED, {"b": 2}, device_id="D1")
        self.assertEqual(len(received), 1)

    def test_handler_error_is_isolated(self):
        bus = EventBus()
        errors = []
        bus.subscribe(EventTopic.FRAME_READY, lambda e: 1 / 0)
        bus.subscribe(EventTopic.SYSTEM_ERROR, lambda e: errors.append(e))
        ok = []
        bus.subscribe(EventTopic.FRAME_READY, lambda e: ok.append(e))
        bus.publish(EventTopic.FRAME_READY, {})
        # 一个订阅者抛异常不得影响另一个，且要转成系统错误事件
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(errors), 1)

    def test_trace_query(self):
        bus = EventBus()
        bus.publish(EventTopic.FRAME_READY, {}, trace_id="T1")
        bus.publish(EventTopic.HEALTH_UPDATED, {}, trace_id="T1")
        bus.publish(EventTopic.FRAME_READY, {}, trace_id="T2")
        self.assertEqual(len(bus.trace("T1")), 2)


class TestAuditTrail(unittest.TestCase):
    def setUp(self):
        self.path = Path("/tmp/maco-test-audit.jsonl")
        if self.path.exists():
            self.path.unlink()
        self.trail = AuditTrail(str(self.path))

    def tearDown(self):
        if self.path.exists():
            self.path.unlink()

    def test_chain_is_valid(self):
        for i in range(5):
            self.trail.record("dispatch", device_id="D1", detail={"i": i})
        result = self.trail.verify_chain()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["checked"], 5)

    def test_tampering_is_detected(self):
        for i in range(3):
            self.trail.record("dispatch", detail={"i": i})
        lines = self.path.read_text(encoding="utf-8").splitlines()
        lines[1] = lines[1].replace('"i": 1', '"i": 999')
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = self.trail.verify_chain()
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "记录内容与哈希不一致")


class TestModbusCodec(unittest.TestCase):
    def test_parse_address(self):
        addr = parse_address("3:40001:float32:big")
        self.assertEqual(addr.area, 3)
        self.assertEqual(addr.register, 0)          # 40001 折算为偏移 0
        self.assertEqual(addr.register_count, 2)

    def test_float_roundtrip(self):
        addr = parse_address("3:100:float32")
        words = encode_value(45.75, addr)
        self.assertAlmostEqual(decode_registers(words, addr), 45.75, places=4)

    def test_int16_negative_roundtrip(self):
        addr = parse_address("3:10:int16")
        words = encode_value(-1234, addr)
        self.assertEqual(decode_registers(words, addr), -1234.0)

    def test_word_order(self):
        big = parse_address("3:100:float32:big:high_first")
        low = parse_address("3:100:float32:big:low_first")
        self.assertNotEqual(encode_value(1.5, big), encode_value(1.5, low))


def _profile() -> DeviceProfile:
    p = DeviceProfile(device_id="D1", name="测试设备")
    p.add_point(SignalPoint("t1", "温度", SignalKind.TEMPERATURE, "℃",
                            low_limit=0, high_limit=150, device_id="D1"))
    p.add_point(SignalPoint("sp", "设定值", SignalKind.SETPOINT, "Hz",
                            low_limit=0, high_limit=50, device_id="D1",
                            writable=True))
    return p


class TestBuffer(unittest.TestCase):
    def test_window_and_stats(self):
        buf = TimeSeriesBuffer(retain_seconds=100)
        for i in range(20):
            buf.append_sample("D1", Sample("t1", 50.0 + i, timestamp=1000.0 + i))
        w = buf.window_between("D1", "t1", 1000.0, 1010.0)
        self.assertEqual(len(w), 10)
        self.assertAlmostEqual(w.mean(), 54.5)

    def test_retention_trims_old_data(self):
        buf = TimeSeriesBuffer(retain_seconds=5)
        for i in range(50):
            buf.append_sample("D1", Sample("t1", float(i), timestamp=1000.0 + i))
        w = buf.window_between("D1", "t1", 0, 1e9)
        self.assertLessEqual(len(w), 6)


class TestCleansing(unittest.TestCase):
    def setUp(self):
        self.buf = TimeSeriesBuffer()
        self.cleanser = DataCleanser({"dead_value_window": 5}, self.buf)
        self.profile = _profile()

    def test_out_of_range_is_flagged(self):
        frame = SampleFrame(device_id="D1")
        frame.put(Sample("t1", 999.0))
        report = self.cleanser.cleanse(frame, self.profile)
        self.assertEqual(frame.get("t1").quality, Quality.OUT_OF_RANGE)
        self.assertEqual(report.modified_count, 1)

    def test_nan_becomes_bad(self):
        frame = SampleFrame(device_id="D1")
        frame.put(Sample("t1", float("nan")))
        self.cleanser.cleanse(frame, self.profile)
        self.assertEqual(frame.get("t1").quality, Quality.BAD)

    def test_setpoint_is_not_treated_as_dead_value(self):
        # 设定值长期恒定属正常，不得被判死值——这是清洗误伤的典型场景
        for i in range(10):
            self.buf.append_sample("D1", Sample("sp", 45.0, timestamp=1000.0 + i))
        frame = SampleFrame(device_id="D1")
        frame.put(Sample("sp", 45.0))
        self.cleanser.cleanse(frame, self.profile)
        self.assertEqual(frame.get("sp").quality, Quality.GOOD)

    def test_interpolate_short_gap(self):
        ts = [0.0, 1.0, 2.0, 3.0, 4.0]
        vs = [1.0, math.nan, math.nan, 4.0, 5.0]
        out = self.cleanser.interpolate_series(ts, vs)
        self.assertAlmostEqual(out[1], 2.0)
        self.assertAlmostEqual(out[2], 3.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

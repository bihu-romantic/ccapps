"""Planning-window defaults must also apply when the UI supplies no override."""
from datetime import datetime
import unittest

from ccAPPSdb.execute.planning_window import (
    capped_split_shipment,
    complete_order_minshipment,
    demand_category_tier,
    estimate_processing_seconds,
    exact_piece_shipment,
    exact_processing_over_hours,
    item_matches_split_scope,
    longest_processing_profile,
    planning_window_end,
    split_is_worthwhile,
    unit_split_shipment,
)


class CappedSplitShipmentTests(unittest.TestCase):
    def test_snp_only_caps_to_two_parts(self):
        # 300 / 2 → 150, rounded up to SNP 6 → 150
        self.assertEqual(capped_split_shipment(300, snp=6, max_parts=2), 150.0)

    def test_economic_preferred_as_unit(self):
        # floor 150, economic 111 → ceil to 2*111 = 222
        self.assertEqual(
            capped_split_shipment(300, snp=6, economic=111, max_parts=2), 222.0
        )

    def test_small_order_stays_whole(self):
        self.assertEqual(capped_split_shipment(50, snp=6, economic=111, max_parts=2), 50.0)

    def test_max_parts_one_keeps_full_quantity(self):
        self.assertEqual(capped_split_shipment(300, snp=6, max_parts=1), 300.0)

    def test_tiny_tail_below_snp_keeps_whole(self):
        # Economic 665 on 673 would leave a 8-unit tail; SNP 20 → keep whole.
        self.assertEqual(
            capped_split_shipment(673, snp=20, economic=665, max_parts=2), 673.0
        )

    def test_meaningful_tail_still_splits(self):
        # 300 with economic 111 → first lot 222, tail 78 >= snp 6.
        self.assertEqual(
            capped_split_shipment(300, snp=6, economic=111, max_parts=2), 222.0
        )

    def test_small_order_not_worthwhile(self):
        # 12 ≤ 2×SNP 6 → keep whole (no 6+6 noise).
        self.assertFalse(split_is_worthwhile(12, snp=6, economic=0))
        self.assertTrue(split_is_worthwhile(13, snp=6, economic=0))
        # Economic 必须拆: remaining > 2n.
        self.assertFalse(split_is_worthwhile(300, snp=20, economic=200))
        self.assertTrue(split_is_worthwhile(401, snp=20, economic=200))

    def test_exact_split_only_over_eight_hours(self):
        self.assertFalse(exact_processing_over_hours(8 * 3600, 8))
        self.assertTrue(exact_processing_over_hours(8 * 3600 + 1, 8))
        self.assertEqual(
            estimate_processing_seconds(10, duration=100, duration_per=50),
            600.0,
        )

    def test_longest_profile_depends_on_order_quantity(self):
        # High setup wins for one unit; high per-unit time wins for 100 units.
        profiles = [(1000, 1), (0, 20)]
        self.assertEqual(longest_processing_profile(1, profiles), (1000, 1))
        self.assertEqual(longest_processing_profile(100, profiles), (0, 20))
        self.assertEqual(longest_processing_profile(100, []), (0.0, 0.0))

    def test_unit_split_uncapped_repeats_one_lot(self):
        # >2n / >3n / >5n: first lot is always one n; no max_parts ceiling.
        n = 720
        self.assertEqual(unit_split_shipment(3 * n, snp=15, economic=n), n)
        self.assertEqual(unit_split_shipment(5 * n, snp=15, economic=n), n)
        self.assertEqual(unit_split_shipment(n, snp=15, economic=n), n)
        self.assertEqual(
            capped_split_shipment(5 * n, snp=15, economic=n, max_parts=0), n
        )

    def test_exact_piece_caps_hours_not_part_count(self):
        # 50s/pc → 8h holds 576 pcs; large remaining still one ≤8h piece.
        self.assertEqual(
            exact_piece_shipment(2000, duration=0, duration_per=50, threshold_hours=8),
            576.0,
        )

    def test_complete_order_minshipment_keeps_unsplit_order_whole(self):
        # The caller selects the >2n economic split path explicitly. All other
        # economic orders retain their full remaining quantity.
        self.assertEqual(
            complete_order_minshipment(1200, snp=15, economic=720), 1200.0
        )
        self.assertEqual(
            complete_order_minshipment(800, snp=60, economic=450), 800.0
        )
        self.assertEqual(
            complete_order_minshipment(720, snp=60, economic=480), 720.0
        )
        self.assertEqual(
            complete_order_minshipment(400, snp=60, economic=480), 400.0
        )
        self.assertEqual(
            complete_order_minshipment(720, snp=15, economic=720), 720.0
        )
        # No economic → whole remaining (legacy complete-order).
        self.assertEqual(
            complete_order_minshipment(1200, snp=15, economic=0), 1200.0
        )

    def test_split_scope_prefix_and_codes(self):
        self.assertTrue(item_matches_split_scope("01.52.00895", prefixes=["01.52"]))
        self.assertTrue(
            item_matches_split_scope("01.01.00309", item_codes={"01.01.00309"})
        )
        self.assertFalse(item_matches_split_scope("01.03.00733", prefixes=["01.52"]))

    def test_demand_category_tier_order(self):
        self.assertEqual(demand_category_tier("FC_ALARM_X", "预警"), 0)
        self.assertEqual(demand_category_tier("FC_UNIN_X", "未入库"), 1)
        self.assertEqual(demand_category_tier("FC_STOCK_X", "备库"), 2)
        self.assertLess(
            demand_category_tier("FC_ALARM_A"),
            demand_category_tier("FC_UNIN_B"),
        )
        self.assertLess(
            demand_category_tier("FC_UNIN_B"),
            demand_category_tier("FC_STOCK_C"),
        )


class PlanningWindowTests(unittest.TestCase):
    current = datetime(2026, 10, 16, 8)

    def test_ui_run_uses_scenario_horizon_without_environment_override(self):
        self.assertEqual(
            planning_window_end(self.current, configured_end="2026-10-30T00:00:00"),
            datetime(2026, 10, 30),
        )

    def test_explicit_run_window_takes_precedence(self):
        self.assertEqual(
            planning_window_end(self.current, "2026-10-25T08:00:00", "2026-10-30"),
            datetime(2026, 10, 25, 8),
        )

    def test_unconfigured_scenario_keeps_unbounded_behavior(self):
        self.assertIsNone(planning_window_end(self.current))

    def test_empty_override_uses_scenario_default(self):
        self.assertEqual(
            planning_window_end(self.current, "", "2026-10-30"),
            datetime(2026, 10, 30),
        )

    def test_invalid_or_expired_horizons_fail_before_solving(self):
        for value in ("bad date", "2026-10-15", "2026-10-16T08:00:00"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                planning_window_end(self.current, configured_end=value)


if __name__ == "__main__":
    unittest.main()

"""Alarm demand must be net of duplicate exact unfinished demand."""
import unittest

from build_scenario7_unin import (
    net_alarm_demands_against_unfinished,
    sync_lifecycle_limits_after_alarm_netting,
)


class AlarmUnfinishedNettingTests(unittest.TestCase):
    def test_unfinished_quantity_is_deducted_from_alarm_in_due_order(self):
        demands = [
            {"name": "FC_UNIN_A", "item": "A", "status": "open", "quantity": 12},
            {"name": "FC_ALARM_A_2", "item": "A", "status": "open", "quantity": 8,
             "due": "2026-10-18T00:00:00"},
            {"name": "FC_ALARM_A_1", "item": "A", "status": "open", "quantity": 9,
             "due": "2026-10-17T00:00:00"},
            {"name": "FC_UNIN_B", "item": "B", "status": "open", "quantity": 5},
            {"name": "FC_ALARM_B", "item": "B", "status": "open", "quantity": 5},
        ]
        changed, deducted = net_alarm_demands_against_unfinished(demands, {"A"})
        self.assertEqual((changed, deducted), (2, 12))
        self.assertEqual(demands[2]["quantity"], 0)
        self.assertEqual(demands[2]["status"], "closed")
        self.assertEqual(demands[1]["quantity"], 5)
        self.assertEqual(demands[1]["status"], "open")
        self.assertEqual(demands[4]["quantity"], 5)

    def test_real_alarm_remainder_is_preserved(self):
        demands = [
            {"name": "FC_UNIN_A", "item": "A", "status": "open", "quantity": 11},
            {"name": "FC_ALARM_A", "item": "A", "status": "open", "quantity": 23},
        ]
        changed, deducted = net_alarm_demands_against_unfinished(demands, {"A"})
        self.assertEqual((changed, deducted), (1, 11))
        self.assertEqual(demands[1]["quantity"], 12)
        self.assertEqual(demands[1]["status"], "open")

    def test_lifecycle_limit_rises_to_unin_plus_alarm_remainder(self):
        # 01.52.00588-style: unfinished 11 + alarm 23 → remainder 12 → limit 23.
        demands = [
            {"name": "FC_UNIN_A", "item": "A", "status": "open", "quantity": 11},
            {
                "name": "FC_ALARM_A",
                "item": "A",
                "status": "open",
                "quantity": 23,
                "description": "报警需求",
            },
        ]
        policy = {"items": {"A": {"exact": True, "snp": 1}}, "limits": {"A": 34.0}}
        net_alarm_demands_against_unfinished(demands, {"A"})
        updated = sync_lifecycle_limits_after_alarm_netting(policy, demands)
        self.assertEqual(demands[1]["quantity"], 12)
        # Convert may have summed unin+alarm before netting (34); sync must
        # rewrite to the open total after deduction (11+12=23).
        self.assertEqual(updated["A"], 23.0)
        self.assertEqual(policy["limits"]["A"], 23.0)

    def test_alarm_only_exact_limit_zero_rises_to_open_demand(self):
        # Unfinished net is 0 → convert wrote limit 0; open alarm must still run.
        demands = [
            {
                "name": "FC_ALARM_B",
                "item": "B",
                "status": "open",
                "quantity": 58,
                "description": "报警需求",
            },
        ]
        policy = {
            "items": {"B": {"exact": True, "snp": 1}, "C": {"exact": True, "snp": 1}},
            "limits": {"B": 0.0},
        }
        updated = sync_lifecycle_limits_after_alarm_netting(policy, demands)
        self.assertEqual(updated["B"], 58.0)
        self.assertEqual(policy["limits"]["B"], 58.0)
        # Coproduct-style exact item with no limit row stays untouched.
        self.assertNotIn("C", policy["limits"])

    def test_zero_limit_without_open_demand_is_removed_for_dependent_supply(self):
        # Component with limit=0 and no own order must not stay capped.
        demands = [
            {"name": "FC_ALARM_PARENT", "item": "P", "status": "open", "quantity": 3},
        ]
        policy = {
            "items": {
                "P": {"exact": True, "snp": 1},
                "C": {"exact": True, "snp": 1},
            },
            "limits": {"P": 3.0, "C": 0.0},
        }
        updated = sync_lifecycle_limits_after_alarm_netting(policy, demands)
        self.assertNotIn("C", policy["limits"])
        self.assertIsNone(updated["C"])
        self.assertEqual(policy["limits"]["P"], 3.0)


if __name__ == "__main__":
    unittest.main()

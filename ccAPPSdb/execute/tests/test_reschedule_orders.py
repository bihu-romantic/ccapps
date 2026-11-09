"""Each order in one reschedule request carries its own action."""
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from ccAPPSdb.execute.views import _reschedule_values


class RescheduleOrderActionTests(SimpleTestCase):
    def test_one_request_mixes_insert_delete_and_change(self):
        values = _reschedule_values(
            {
                "category": "order",
                "orders": [
                    {"name": "SO-1", "action": "insert"},
                    {"name": "SO-2", "action": "删单"},
                    {"name": "SO-3", "action": "change"},
                ],
            }
        )
        self.assertEqual(
            values["_orders"],
            [
                {"name": "SO-1", "action": "insert"},
                {"name": "SO-2", "action": "delete"},
                {"name": "SO-3", "action": "change"},
            ],
        )
        self.assertEqual(values["local.new_demands"], "SO-1,SO-3")
        self.assertEqual(values["local.deleted_demands"], "SO-2,SO-3")

    def test_legacy_lists_still_map_to_actions(self):
        values = _reschedule_values(
            {
                "category": "order",
                "new_orders": ["SO-NEW", "SO-CHG"],
                "deleted_orders": ["SO-CHG", "SO-DEL"],
            }
        )
        self.assertEqual(
            values["_orders"],
            [
                {"name": "SO-NEW", "action": "insert"},
                {"name": "SO-CHG", "action": "change"},
                {"name": "SO-DEL", "action": "delete"},
            ],
        )

    def test_same_order_cannot_carry_two_actions(self):
        with self.assertRaises(ValidationError):
            _reschedule_values(
                {
                    "category": "order",
                    "orders": [
                        {"name": "SO-1", "action": "insert"},
                        {"name": "SO-1", "action": "delete"},
                    ],
                }
            )

    def test_one_request_carries_supply_and_resource_windows(self):
        values = _reschedule_values(
            {
                "category": "all",
                "orders": [
                    {
                        "name": "SO-1",
                        "action": "change",
                        "quantity": 50,
                        "due": "2026-11-20T08:00:00",
                    }
                ],
                "materials": [
                    {
                        "name": "RAW-A",
                        "po": "PO-9",
                        "quantity": 20,
                        "arrival": "2026-11-18T08:00:00",
                    }
                ],
                "resources": [
                    {
                        "name": "M-01",
                        "start": "2026-11-16T08:00:00",
                        "end": "2026-11-17T08:00:00",
                    }
                ],
            }
        )
        self.assertEqual(values["_orders"][0]["quantity"], "50")
        self.assertEqual(values["local.changed_materials"], "RAW-A")
        self.assertEqual(values["local.changed_resources"], "M-01")
        self.assertEqual(values["_resources"][0]["end"], "2026-11-17T08:00:00")

    def test_orders_and_name_lists_are_not_combined(self):
        with self.assertRaises(ValidationError):
            _reschedule_values(
                {
                    "category": "order",
                    "orders": [{"name": "SO-1", "action": "insert"}],
                    "deleted_orders": ["SO-2"],
                }
            )

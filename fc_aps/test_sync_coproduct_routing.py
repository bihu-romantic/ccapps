"""Co-production: one driver demand of max(member nets) launches the shared lot."""
import json
import unittest
from pathlib import Path

SCENARIO8 = Path(__file__).resolve().parents[1] / "fc_data/20261016/fc_aps_scenario8.json"


class SyncCoproductRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not SCENARIO8.exists():
            raise unittest.SkipTest("scenario8 dataset missing")
        cls.data = json.loads(SCENARIO8.read_text(encoding="utf-8"))["data"]

    def test_w2311_pair_has_leaf_end_flows(self):
        ends = [
            row
            for row in self.data["operationMaterials"]
            if "W2311" in row.get("operation", "")
            and float(row.get("quantity") or 0) > 0
        ]
        ops = {row["operation"] for row in ends}
        items = {row["item"] for row in ends}
        self.assertIn("03.01.06009", items)
        self.assertIn("03.01.06010", items)
        # End flows stay on machine leaves (lifecycle caps apply there).
        self.assertTrue(all("__" in name for name in ops))

    def test_fcm_w2454_single_driver_lot_of_max_side(self):
        """Left 56 + right 66 → one open demand of 66; partner closed; no SS min."""
        buf = {
            b["item"]: float(b.get("minimum") or 0)
            for b in self.data["buffers"]
            if b.get("item") in ("01.52.00509", "01.52.00510")
        }
        self.assertEqual(buf["01.52.00509"], 0)
        self.assertEqual(buf["01.52.00510"], 0)

        open_rows = [
            d
            for d in self.data["demands"]
            if d.get("item") in ("01.52.00509", "01.52.00510")
            and d.get("status") == "open"
            and float(d.get("quantity") or 0) > 0
        ]
        closed_ride = [
            d
            for d in self.data["demands"]
            if d.get("item") in ("01.52.00509", "01.52.00510")
            and d.get("status") == "closed"
            and "共模随" in (d.get("description") or "")
        ]
        self.assertEqual(len(open_rows), 1)
        self.assertEqual(float(open_rows[0]["quantity"]), 66)
        self.assertEqual(open_rows[0]["item"], "01.52.00510")
        self.assertGreaterEqual(len(closed_ride), 1)


if __name__ == "__main__":
    unittest.main()

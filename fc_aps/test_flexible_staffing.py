"""Validate FC flexible-staff alternatives and engine capacity selection."""
import json
import sys
import unittest
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ccAPPS
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parent
SCENARIO8 = Path(__file__).resolve().parents[1] / "fc_data/20261016/fc_aps_scenario8.json"


class FlexibleStaffingTests(unittest.TestCase):
    def test_generated_alternatives(self):
        doc = json.loads((ROOT / "fc_aps_scenario6.json").read_text())
        data = doc["data"]
        source = ROOT
        standards = {
            str(r[0]): r
            for r in list(
                load_workbook(source / "标准工时.xlsx", read_only=True, data_only=True).active.values
            )[1:]
        }
        heat = {
            str(r[0])
            for r in list(
                load_workbook(source / "机台.xlsx", read_only=True, data_only=True).active.values
            )[1:]
            if r[1] == "热压"
        }
        outputs = defaultdict(set)
        for row in data["operationMaterials"]:
            if row.get("quantity", 0) > 0:
                outputs[row["operation"]].add(row["item"])
        policy = json.loads(
            next(p["value"] for p in doc["parameters"] if p["name"] == "fc_aps.batch_policy")
        )
        loads = defaultdict(list)
        for r in data["operationResources"]:
            loads[r["operation"]].append(r)
        families = defaultdict(list)
        for op in data["operations"]:
            if "__P" in op["name"]:
                family, people = op["name"].rsplit("__P", 1)
                families[family].append((op, int(people)))
        self.assertTrue(families)
        for family, variants in families.items():
            work = None
            nonpeople = None
            batch = None
            for op, n in variants:
                self.assertEqual(op["type"], "time_per")
                person = [r for r in loads[op["name"]] if r["resource"].startswith("PERSON_")]
                self.assertEqual(len(person), 1)
                self.assertEqual(person[0]["quantity"], n)
                actual = op["duration_per"] * n
                if work is None:
                    work = actual
                if any(r["resource"] in heat for r in loads[op["name"]]):
                    codes = outputs[op["name"]]
                    self.assertTrue(codes)
                    self.assertTrue(all(float(standards[c][5]) > 0 for c in codes))
                    standard_ct = max(float(standards[c][5]) for c in codes)
                    standard_people = max(float(standards[c][3]) for c in codes)
                    self.assertLessEqual(n, standard_people)
                    self.assertAlmostEqual(
                        op["duration_per"], standard_ct * standard_people / n, places=6
                    )
                else:
                    self.assertAlmostEqual(actual, work, places=6)
                resources = {
                    (r["resource"], r["quantity"])
                    for r in loads[op["name"]]
                    if not r["resource"].startswith("PERSON_")
                }
                if nonpeople is None:
                    nonpeople = resources
                self.assertEqual(resources, nonpeople)
                rule = policy["operations"][op["name"]]
                if batch is None:
                    batch = rule
                self.assertEqual(rule, batch)
            self.assertEqual(
                sorted(n for _, n in variants), list(range(1, max(n for _, n in variants) + 1))
            )

    def test_scenario8_workshop5_ratio_and_priority(self):
        """装配/热压少人方案：工时按比例拉长，并优先尝试满员。"""
        if not SCENARIO8.exists():
            self.skipTest("scenario8 dataset missing")
        data = json.loads(SCENARIO8.read_text(encoding="utf-8"))["data"]
        res = {r["name"]: r for r in data["resources"]}
        loads = defaultdict(list)
        for row in data["operationResources"]:
            loads[row["operation"]].append(row)
        families = defaultdict(list)
        for op in data["operations"]:
            if "__P" not in op["name"]:
                continue
            person = [
                row
                for row in loads[op["name"]]
                if res.get(row["resource"], {}).get("category") == "人员"
                and "制造部5工段" in row["resource"]
            ]
            if not person:
                continue
            base, people = op["name"].rsplit("__P", 1)
            families[base].append((op, int(people), float(person[0]["quantity"])))
        self.assertGreater(len(families), 0)
        for base, variants in families.items():
            variants = sorted(variants, key=lambda row: row[1], reverse=True)
            max_people = variants[0][1]
            work = variants[0][0]["duration_per"] * variants[0][1]
            # Prefer fuller staffing: lower priority number for more people.
            self.assertEqual(variants[0][0]["priority"], min(v[0]["priority"] for v in variants))
            self.assertGreaterEqual(variants[0][0]["priority"], 1)
            for op, people, qty in variants:
                self.assertEqual(people, int(qty))
                self.assertAlmostEqual(op["duration_per"] * people, work, places=6)
                self.assertEqual(
                    op["priority"] - variants[0][0]["priority"], max_people - people
                )

    def _build_staff_alts(self, available_people, standard_people=4):
        ccAPPS.erase(True)
        start = datetime(2026, 10, 17, 8)
        ccAPPS.settings.current = start
        loc = ccAPPS.location(name="factory")
        item = ccAPPS.item(name="fg", category="自制")
        staff = ccAPPS.resource(
            name="PERSON_制造部5工段", category="人员", maximum=available_people
        )
        machine = ccAPPS.resource(name="5工段装配线", category="设备", maximum=1)
        parent = ccAPPS.operation_alternate(
            name="make", item=item, location=loc, search="PRIORITY"
        )
        # Match convert_fc_aps.py: prefer more people, never use priority 0.
        for n in range(standard_people, 0, -1):
            op = ccAPPS.operation_time_per(
                name="make__P%d" % n,
                location=loc,
                duration_per=3600.0 * standard_people / n,
            )
            ccAPPS.suboperation(
                owner=parent, operation=op, priority=1000 + (standard_people - n)
            )
            ccAPPS.flow(
                operation=op, item=item, location=loc, quantity=1, type="flow_end"
            )
            ccAPPS.load(operation=op, resource=staff, quantity=n)
            ccAPPS.load(operation=op, resource=machine, quantity=1)
        ccAPPS.buffer(name="fg @ factory", item=item, location=loc)
        ccAPPS.demand(
            name="d",
            item=item,
            location=loc,
            quantity=1,
            status="open",
            due=start + timedelta(hours=12),
        )
        return start

    def test_solver_uses_available_people(self):
        self._build_staff_alts(available_people=2, standard_people=4)
        ccAPPS.solver_mrp(constraints=52, plantype=1, loglevel=0).solve()
        plans = [
            p
            for p in ccAPPS.operationplans()
            if p.operation.name.startswith("make__P")
        ]
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].operation.name, "make__P2")
        self.assertAlmostEqual((plans[0].end - plans[0].start).total_seconds(), 7200)

    def test_fas_selects_reduced_staff_when_headcount_is_scarce(self):
        self._build_staff_alts(available_people=2, standard_people=4)
        solver = ccAPPS.solverFAS(constraints=52, plantype=1, loglevel=0)
        solver.solve()
        solver.commit()
        plans = [
            p
            for p in ccAPPS.operationplans()
            if p.operation.name.startswith("make__P")
        ]
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].operation.name, "make__P2")
        # Standard 4 people => 1h; with 2 people duration doubles.
        self.assertAlmostEqual((plans[0].end - plans[0].start).total_seconds(), 7200)

    def test_fas_prefers_full_staff_when_headcount_allows(self):
        self._build_staff_alts(available_people=4, standard_people=4)
        solver = ccAPPS.solverFAS(constraints=52, plantype=1, loglevel=0)
        solver.solve()
        solver.commit()
        plans = [
            p
            for p in ccAPPS.operationplans()
            if p.operation.name.startswith("make__P")
        ]
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].operation.name, "make__P4")
        self.assertAlmostEqual((plans[0].end - plans[0].start).total_seconds(), 3600)


if __name__ == "__main__":
    unittest.main()

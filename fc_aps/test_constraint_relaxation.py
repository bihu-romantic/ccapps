"""Regression tests for capa/mfg_lt/po_lt relaxation semantics.

Each comparison keeps orders and master data identical and changes only the
constraint bitmask. Listed constraints are enforced; omitted constraints are
relaxed.
"""

import os
import unittest
from datetime import datetime, timedelta

import ccAPPS


ALL = 4 + 16 + 32
ALGORITHMS = (os.environ.get("CCAPPS_CONSTRAINT_TEST_ALGORITHM", "mrp"),)


class ConstraintRelaxationTests(unittest.TestCase):
    def setUp(self):
        ccAPPS.erase(True)
        self.current = datetime(2026, 1, 10, 8)
        ccAPPS.settings.current = self.current

    def solve(self, algorithm, constraints):
        tokens = [
            name
            for bit, name in ((4, "capa"), (16, "mfg_lt"), (32, "po_lt"))
            if constraints & bit
        ]
        os.environ.update(
            {
                # Empty means "not configured" to the engine and therefore
                # selects defaults. Numeric 0 explicitly relaxes all three.
                "CCAPPS_CONSTRAINT": ",".join(tokens) if tokens else "0",
                "CCAPPS_PLANTYPE": "1",
                "CP_TIME_LIMIT_SECONDS": "2",
                "CP_HORIZON_DAYS": "10",
                "CP_PLANNING_MODE": "all",
                "CP_MATERIAL_MODE": "all",
                "CP_OBJECTIVE_METHOD": "weighted",
                "GA_POPULATION": "8",
                "GA_GENERATIONS": "2",
                "GA_TIME_LIMIT_SECONDS": "2",
                "GA_STAGNATION_LIMIT": "1",
            }
        )
        os.environ.pop("LOCAL_RESCHEDULE", None)
        os.environ.pop("CP_LOCAL_RESCHEDULE", None)
        if algorithm == "fas":
            solver = ccAPPS.solverFAS(
                constraints=constraints, plantype=1, loglevel=0
            )
            solver.solve()
            solver.commit()
            return
        ccAPPS.solver_mrp(
            constraints=constraints, plantype=1, loglevel=0
        ).solve()
        if algorithm == "cp":
            ccAPPS.run_cp()
        elif algorithm == "ga":
            ccAPPS.run_ga()

    def build_capacity_case(self):
        location = ccAPPS.location(name="factory")
        machine = ccAPPS.resource(name="machine", maximum=1)
        for index in range(2):
            item = ccAPPS.item(name=f"fg{index}")
            operation = ccAPPS.operation_fixed_time(
                name=f"make{index}", location=location, duration=7200
            )
            ccAPPS.load(operation=operation, resource=machine, quantity=1)
            ccAPPS.flow(
                operation=operation,
                item=item,
                location=location,
                quantity=1,
                type="flow_end",
            )
            ccAPPS.demand(
                name=f"order{index}",
                item=item,
                location=location,
                quantity=1,
                due=self.current + timedelta(hours=2),
                status="open",
            )

    def build_mfg_case(self):
        location = ccAPPS.location(name="factory")
        item = ccAPPS.item(name="fg")
        operation = ccAPPS.operation_fixed_time(
            name="make", location=location, duration=7200
        )
        ccAPPS.load(
            operation=operation,
            resource=ccAPPS.resource(name="machine", maximum=1),
            quantity=1,
        )
        ccAPPS.flow(
            operation=operation,
            item=item,
            location=location,
            quantity=1,
            type="flow_end",
        )
        ccAPPS.demand(
            name="order",
            item=item,
            location=location,
            quantity=1,
            due=self.current + timedelta(hours=1),
            status="open",
        )

    def build_purchase_case(self, with_route=True):
        location = ccAPPS.location(name="factory")
        item = ccAPPS.item(name="raw")
        ccAPPS.buffer(
            name="raw @ factory", item=item, location=location, onhand=0
        )
        if with_route:
            supplier = ccAPPS.supplier(name="supplier")
            ccAPPS.itemsupplier(
                item=item,
                location=location,
                supplier=supplier,
                leadtime=5 * 86400,
            )
        return ccAPPS.demand(
            name="order",
            item=item,
            location=location,
            quantity=1,
            due=self.current + timedelta(days=1),
            status="open",
        )

    def build_combined_case(self):
        """One fixed order set exposing all three constraint classes."""
        location = ccAPPS.location(name="factory")
        shared = ccAPPS.resource(name="shared", maximum=1)
        for index in range(2):
            item = ccAPPS.item(name=f"capacity_fg{index}")
            operation = ccAPPS.operation_fixed_time(
                name=f"capacity_make{index}",
                location=location,
                duration=7200,
            )
            ccAPPS.load(operation=operation, resource=shared, quantity=1)
            ccAPPS.flow(
                operation=operation,
                item=item,
                location=location,
                quantity=1,
                type="flow_end",
            )
            ccAPPS.demand(
                name=f"capacity_order{index}",
                item=item,
                location=location,
                quantity=1,
                due=self.current + timedelta(hours=2),
                status="open",
            )

        mfg_item = ccAPPS.item(name="mfg_fg")
        mfg_operation = ccAPPS.operation_fixed_time(
            name="mfg_make", location=location, duration=7200
        )
        ccAPPS.load(
            operation=mfg_operation,
            resource=ccAPPS.resource(name="mfg_machine", maximum=1),
            quantity=1,
        )
        ccAPPS.flow(
            operation=mfg_operation,
            item=mfg_item,
            location=location,
            quantity=1,
            type="flow_end",
        )
        ccAPPS.demand(
            name="mfg_order",
            item=mfg_item,
            location=location,
            quantity=1,
            due=self.current + timedelta(hours=1),
            status="open",
        )

        raw = ccAPPS.item(name="purchase_raw")
        ccAPPS.buffer(
            name="purchase_raw @ factory",
            item=raw,
            location=location,
            onhand=0,
        )
        ccAPPS.itemsupplier(
            item=raw,
            location=location,
            supplier=ccAPPS.supplier(name="purchase_supplier"),
            leadtime=5 * 86400,
        )
        ccAPPS.demand(
            name="purchase_order",
            item=raw,
            location=location,
            quantity=1,
            due=self.current + timedelta(days=1),
            status="open",
        )

    def test_constraint_combination(self):
        algorithm = ALGORITHMS[0]
        constraints = int(os.environ.get("CCAPPS_CONSTRAINT_TEST_MASK", ALL))
        self.build_combined_case()
        self.solve(algorithm, constraints)

        capacity = [
            p
            for p in ccAPPS.operationplans()
            if p.operation.name.startswith("capacity_make")
        ]
        self.assertEqual(len(capacity), 2)
        overlaps = max(p.start for p in capacity) < min(p.end for p in capacity)
        self.assertEqual(overlaps, not bool(constraints & 4))

        manufacturing = next(
            p for p in ccAPPS.operationplans() if p.operation.name == "mfg_make"
        )
        self.assertEqual(
            manufacturing.start >= self.current, bool(constraints & 16)
        )

        purchase = next(
            p
            for p in ccAPPS.operationplans()
            if isinstance(p.operation, ccAPPS.operation_itemsupplier)
        )
        self.assertEqual(purchase.start >= self.current, bool(constraints & 32))

    def test_relaxing_capacity_allows_overlap(self):
        for algorithm in ALGORITHMS:
            with self.subTest(algorithm=algorithm):
                self.setUp()
                self.build_capacity_case()
                self.solve(algorithm, ALL)
                constrained = [p for p in ccAPPS.operationplans() if p.operation.name.startswith("make")]
                self.assertFalse(max(p.start for p in constrained) < min(p.end for p in constrained))
                self.setUp()
                self.build_capacity_case()
                self.solve(algorithm, ALL - 4)
                relaxed = [p for p in ccAPPS.operationplans() if p.operation.name.startswith("make")]
                self.assertTrue(max(p.start for p in relaxed) < min(p.end for p in relaxed))

    def test_relaxing_mfg_leadtime_allows_past_start(self):
        for algorithm in ALGORITHMS:
            with self.subTest(algorithm=algorithm):
                self.setUp()
                self.build_mfg_case()
                self.solve(algorithm, ALL)
                constrained = next(p for p in ccAPPS.operationplans() if p.operation.name == "make")
                self.assertGreaterEqual(constrained.start, self.current)
                self.setUp()
                self.build_mfg_case()
                self.solve(algorithm, ALL - 16)
                relaxed = next(p for p in ccAPPS.operationplans() if p.operation.name == "make")
                self.assertLess(relaxed.start, self.current)

    def test_relaxing_po_leadtime_allows_past_order_date(self):
        for algorithm in ALGORITHMS:
            with self.subTest(algorithm=algorithm):
                self.setUp()
                self.build_purchase_case()
                self.solve(algorithm, ALL)
                constrained = next(p for p in ccAPPS.operationplans() if isinstance(p.operation, ccAPPS.operation_itemsupplier))
                self.assertGreaterEqual(constrained.start, self.current)
                self.setUp()
                self.build_purchase_case()
                self.solve(algorithm, ALL - 32)
                relaxed = next(p for p in ccAPPS.operationplans() if isinstance(p.operation, ccAPPS.operation_itemsupplier))
                self.assertLess(relaxed.start, self.current)
                self.assertEqual(relaxed.end, self.current + timedelta(days=1))

    def test_relaxed_po_leadtime_does_not_invent_supply_path(self):
        for algorithm in ALGORITHMS:
            with self.subTest(algorithm=algorithm):
                self.setUp()
                demand = self.build_purchase_case(with_route=False)
                self.solve(algorithm, ALL - 32)
                purchases = [p for p in ccAPPS.operationplans() if isinstance(p.operation, ccAPPS.operation_itemsupplier)]
                self.assertEqual(purchases, [])
                self.assertEqual(demand.planned_quantity, 0)


if __name__ == "__main__":
    unittest.main()

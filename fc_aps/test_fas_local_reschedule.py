"""FAS local reschedule: material-only and a mixed stop.

Material on-hand with no manufacturing work order is a feasible no-op.
A mixed request must leave the stopped resource with no load in the
zero-capacity window and still report ok.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ccAPPS


ORIGIN = datetime(2026, 10, 16, 8)


def _clear_local_env():
    for key in (
        "LOCAL_RESCHEDULE",
        "CP_LOCAL_RESCHEDULE",
        "LOCAL_RESCHEDULE_TYPE",
        "CP_LOCAL_RESCHEDULE_TYPE",
        "LOCAL_RESCHEDULE_LEVEL",
        "CP_LOCAL_RESCHEDULE_LEVEL",
        "LOCAL_CHANGED_MATERIALS",
        "CP_LOCAL_CHANGED_MATERIALS",
        "LOCAL_CHANGED_RESOURCES",
        "CP_LOCAL_CHANGED_RESOURCES",
        "LOCAL_NEW_DEMANDS",
        "CP_LOCAL_NEW_DEMANDS",
        "LOCAL_DELETED_DEMANDS",
        "CP_LOCAL_DELETED_DEMANDS",
        "LOCAL_KEEP_EXPLICIT_LEVEL",
        "FAS_IMPROVE_SECONDS",
    ):
        os.environ.pop(key, None)


def _isolate(name):
    """Each case needs a fresh process.

    Creating an inventory buffer registers an operation_inventory plan, and a
    second erase in the same process aborts. The case itself is unchanged.
    """
    if os.environ.get("CCAPPS_FAS_CASE") == name:
        return False
    env = os.environ.copy()
    env["CCAPPS_FAS_CASE"] = name
    ortools = Path(__file__).resolve().parents[1] / "third_party" / "or-tools" / "lib"
    env["LD_LIBRARY_PATH"] = str(ortools) + ":" + env.get("LD_LIBRARY_PATH", "")
    proc = __import__("subprocess").run(
        [sys.executable, str(Path(__file__).resolve()), name],
        env=env,
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise AssertionError(
            "case %s failed\n%s\n%s" % (name, proc.stdout, proc.stderr)
        )
    return True


class FasLocalRescheduleTests(unittest.TestCase):
    def setUp(self):
        _clear_local_env()
        ccAPPS.erase(True)
        ccAPPS.settings.current = ORIGIN
        os.environ["CCAPPS_CONSTRAINT"] = "capa,mfg_lt,po_lt"
        os.environ["CCAPPS_PLANTYPE"] = "1"
        os.environ["CP_HORIZON_DAYS"] = "3"
        os.environ["FAS_IMPROVE_SECONDS"] = "0"

    def tearDown(self):
        _clear_local_env()

    def _local(self, **env):
        os.environ["LOCAL_RESCHEDULE"] = "1"
        os.environ["CP_LOCAL_RESCHEDULE"] = "1"
        for key, value in env.items():
            os.environ[key] = value
            if key.startswith("LOCAL_"):
                os.environ["CP_" + key[len("LOCAL_") :]] = value
                os.environ["CP_LOCAL_" + key[len("LOCAL_") :]] = value

    def test_material_onhand_without_work_order_is_ok(self):
        if _isolate("material_ok"):
            return
        location = ccAPPS.location(name="FC_FACTORY")
        item = ccAPPS.item(name="01.01.00660B", category="自制")
        ccAPPS.buffer(
            name="01.01.00660B @ FC_FACTORY",
            item=item,
            location=location,
            onhand=10,
        )
        self._local(
            LOCAL_RESCHEDULE_TYPE="material",
            LOCAL_RESCHEDULE_LEVEL="auto",
            LOCAL_CHANGED_MATERIALS="01.01.00660B",
        )
        ccAPPS.run_fas()
        outcome = ccAPPS.cp_last_local_outcome()
        self.assertTrue(
            outcome["local_ok"],
            msg="material-only with no work order should be feasible: %s" % outcome,
        )
        self.assertEqual(outcome["stage"], "local_material_feasible")
        # Opening stock is a closed inventory record, not a work order to move.
        manufacturing = [
            plan
            for plan in ccAPPS.operationplans()
            if str(getattr(plan, "ordertype", "") or "").upper() == "MO"
        ]
        self.assertEqual(manufacturing, [])

    def test_material_shortage_without_work_order_fails_clearly(self):
        if _isolate("material_short"):
            return
        location = ccAPPS.location(name="FC_FACTORY")
        item = ccAPPS.item(name="01.01.00660B", category="自制")
        ccAPPS.buffer(
            name="01.01.00660B @ FC_FACTORY",
            item=item,
            location=location,
            onhand=10,
            minimum=20,
        )
        self._local(
            LOCAL_RESCHEDULE_TYPE="material",
            LOCAL_RESCHEDULE_LEVEL="auto",
            LOCAL_CHANGED_MATERIALS="01.01.00660B",
        )
        ccAPPS.run_fas()
        outcome = ccAPPS.cp_last_local_outcome()
        self.assertFalse(outcome["local_ok"], msg=outcome)
        self.assertEqual(outcome["stage"], "local_material_no_root")
        self.assertIn("no work order", outcome.get("preflight_conflicts") or "")
        manufacturing = [
            plan
            for plan in ccAPPS.operationplans()
            if str(getattr(plan, "ordertype", "") or "").upper() == "MO"
        ]
        self.assertEqual(manufacturing, [])

    def test_mixed_stop_clears_h04_and_keeps_orders(self):
        if _isolate("mixed_stop"):
            return
        location = ccAPPS.location(name="FC_FACTORY")
        raw = ccAPPS.item(name="01.01.00660B", category="自制")
        finished = ccAPPS.item(name="FG", category="自制")
        component = ccAPPS.item(name="COMP", category="自制")
        ccAPPS.buffer(
            name="01.01.00660B @ FC_FACTORY",
            item=raw,
            location=location,
            onhand=10,
        )
        ccAPPS.buffer(name="COMP @ FC_FACTORY", item=component, location=location, onhand=0)
        ccAPPS.buffer(name="FG @ FC_FACTORY", item=finished, location=location, onhand=0)

        calendar = ccAPPS.calendar(name="H04 capacity", default=1)
        stop_end = ORIGIN + timedelta(days=1)
        calendar.setValue(ORIGIN, stop_end, 0)
        h04 = ccAPPS.resource(name="H04", maximum=1, maximum_calendar=calendar)
        other = ccAPPS.resource(name="other", maximum=1)

        produced = ccAPPS.operation_fixed_time(
            name="h04-0", location=location, duration=3600
        )
        ccAPPS.load(operation=produced, resource=h04, quantity=1)
        ccAPPS.flow(
            operation=produced,
            item=component,
            location=location,
            quantity=1,
            type="flow_end",
        )
        ccAPPS.operationplan(
            operation=produced,
            quantity=1,
            start=ORIGIN,
            end=ORIGIN + timedelta(hours=1),
            status="proposed",
        )

        for index in range(1, 5):
            operation = ccAPPS.operation_fixed_time(
                name="h04-%s" % index, location=location, duration=3600
            )
            ccAPPS.load(operation=operation, resource=h04, quantity=1)
            start = ORIGIN + timedelta(hours=index)
            ccAPPS.operationplan(
                operation=operation,
                quantity=1,
                start=start,
                end=start + timedelta(hours=1),
                status="proposed",
            )

        follow = ccAPPS.operation_fixed_time(
            name="after", location=location, duration=3600
        )
        ccAPPS.load(operation=follow, resource=other, quantity=1)
        ccAPPS.flow(
            operation=follow,
            item=component,
            location=location,
            quantity=-1,
            type="flow_start",
        )
        ccAPPS.flow(
            operation=follow,
            item=finished,
            location=location,
            quantity=1,
            type="flow_end",
        )
        follower = ccAPPS.operationplan(
            operation=follow,
            quantity=1,
            start=ORIGIN + timedelta(hours=1),
            end=ORIGIN + timedelta(hours=2),
            status="proposed",
        )

        demand = ccAPPS.demand(
            name="SO-1",
            item=finished,
            location=location,
            quantity=5,
            status="open",
            due=ORIGIN + timedelta(days=3),
            priority=1,
        )
        ship = ccAPPS.operation_delivery(
            name="ship", item=finished, location=location
        )
        ccAPPS.operationplan(
            operation=ship,
            quantity=5,
            demand=demand,
            start=demand.due,
            end=demand.due,
            status="proposed",
        )

        self._local(
            LOCAL_RESCHEDULE_TYPE="all",
            LOCAL_RESCHEDULE_LEVEL="2",
            LOCAL_CHANGED_MATERIALS="01.01.00660B",
            LOCAL_CHANGED_RESOURCES="H04",
            LOCAL_NEW_DEMANDS="SO-1",
            LOCAL_DELETED_DEMANDS="SO-1",
        )
        ccAPPS.run_fas()
        outcome = ccAPPS.cp_last_local_outcome()
        self.assertTrue(outcome["local_ok"], msg="mixed FAS reschedule: %s" % outcome)

        stopped = []
        for plan in ccAPPS.operationplans():
            if plan.operation is None or not str(plan.operation.name).startswith("h04-"):
                continue
            if plan.start < stop_end and plan.end > ORIGIN:
                stopped.append(
                    "%s %s..%s" % (plan.operation.name, plan.start, plan.end)
                )
        self.assertEqual(stopped, [], msg="H04 loads still in the stop: %s" % stopped)

        producer = None
        for plan in ccAPPS.operationplans():
            if plan.operation is not None and plan.operation.name == "h04-0":
                producer = plan
        self.assertIsNotNone(producer)
        self.assertGreaterEqual(follower.start, producer.end)


if __name__ == "__main__":
    case = os.environ.get("CCAPPS_FAS_CASE")
    if case and len(sys.argv) > 1 and sys.argv[1] == case:
        suite = {
            "material_ok": "test_material_onhand_without_work_order_is_ok",
            "material_short": "test_material_shortage_without_work_order_fails_clearly",
            "mixed_stop": "test_mixed_stop_clears_h04_and_keeps_orders",
        }[case]
        result = unittest.TextTestRunner(verbosity=2).run(
            FasLocalRescheduleTests(suite)
        )
        sys.exit(0 if result.wasSuccessful() else 1)
    unittest.main()

"""FAS reschedule matrix on a clean demo_data/fc_factory load.

Skipped unless PostgreSQL scenario9 can be restored from the fc_factory dump
and bin/libccAPPS.so was built from this source. The committed engine binary
does not contain the FAS local-insert path, so a CI run without that rebuild
skips instead of failing.
"""
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "bin" / "libccAPPS.so"
BASELINE = Path(__file__).with_name("test_fas_factory_baseline.py")

QTY = "FC_UNIN_QY_01.04.00792_SCQY2610000136_R00071"
PLANNED = "FC_ALARM_QYJG_01.04.00599_20261017_R00321"
CLOSED = "FC_ALARM_QY_01.04.00773_20261022_R00419"
DELETE = "FC_ALARM_QYJG_01.04.00648_20261017_R00362"
INSERT = "FC_INSERT_01.04.00599_20261023"
H04_START = "2026-10-16T08:00:00"
H04_END = "2026-10-17T08:00:00"


def _baseline():
    spec = importlib.util.spec_from_file_location("fas_factory_baseline", BASELINE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _skip_reason(base):
    if not ENGINE.is_file() or b"FAS local insert" not in ENGINE.read_bytes():
        return (
            "bin/libccAPPS.so has no FAS local-insert path; rebuild the engine "
            "before running the live matrix"
        )
    if not Path(base.DUMP).is_file():
        return "fc_factory dump is not at %s" % base.DUMP
    try:
        base._connect().close()
    except Exception as exc:
        return "scenario9 is not reachable: %s" % exc
    return None


class FactoryFasMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = _baseline()
        reason = _skip_reason(cls.base)
        if reason:
            raise unittest.SkipTest(reason)
        sys.path.insert(0, str(ROOT))
        os.environ.update(cls.base._env())
        import django

        django.setup()
        from ccAPPSdb.common.models import Scenario, User

        try:
            user = User.objects.using("default").get(username="admin")
        except Exception as exc:
            raise unittest.SkipTest("default database has no admin user: %s" % exc)
        if not user.is_superuser:
            raise unittest.SkipTest("admin is not a superuser on the default database")
        databases = list(user.databases or [])
        if "scenario9" not in databases:
            databases.append("scenario9")
            user.databases = databases
            user.save(using="default", update_fields=["databases"])
        Scenario.objects.using("default").update_or_create(
            name="scenario9",
            defaults={"status": "In use", "description": "fc_factory"},
        )
        cls.user = user

    def setUp(self):
        self.base._restore()

    def _post(self, body):
        from django.test import Client

        client = Client()
        client.force_login(self.user)
        response = client.post(
            "/scenario9/api/reschedule/",
            data=json.dumps(body),
            content_type="application/json",
        )
        payload = response.json()
        task = None
        outcome = None
        if response.status_code == 202:
            task_id = payload["taskid"]
            deadline = __import__("time").time() + 240
            while __import__("time").time() < deadline:
                rows = self.base._query(
                    """
                    select status, coalesce(message, '') as message,
                           finished is not null as done
                    from execute_log where id = %s
                    """,
                    (task_id,),
                )
                if rows and rows[0]["done"] and rows[0]["status"] in (
                    "Done",
                    "Failed",
                    "Canceled",
                ):
                    task = rows[0]
                    message = task["message"]
                    if message.startswith("local_outcome="):
                        outcome = json.loads(message[len("local_outcome=") :])
                    break
                __import__("time").sleep(0.5)
            self.assertIsNotNone(task, msg="reschedule task did not finish")
        return response.status_code, payload, task, outcome

    def _plans(self, name):
        return self.base._query(
            """
            select type, status, quantity::text as quantity,
                   startdate::text as startdate, enddate::text as enddate
            from operationplan
            where demand_id = %s
            order by type, startdate
            """,
            (name,),
        )

    def _onhand(self):
        rows = self.base._query(
            """
            select onhand::text as onhand from buffer
            where item_id = '01.01.00660B' and location_id = 'FC_FACTORY'
            """
        )
        return rows[0]["onhand"]

    def _overlap(self):
        rows = self.base._query(
            """
            select count(*)::int as n
            from operationplanresource opr
            join operationplan op on op.reference = opr.operationplan_id
            where opr.resource_id = 'H04'
              and op.startdate < '2026-10-17 08:00:00+02'
              and op.enddate > '2026-10-16 08:00:00+02'
            """
        )
        return rows[0]["n"]

    def _prepare_insert(self):
        self.base._query(
            """
            insert into demand (
                name, item_id, location_id, customer_id, due, status, quantity,
                priority, description, quote_enabled, policy, source
            ) values (
                %s, '01.04.00599', 'FC_FACTORY', 'FC_CUSTOMER',
                '2026-10-23 23:59:59+02', 'open', 2, 100,
                'fas matrix insert', false, 'independent', 'fas-matrix'
            )
            """,
            (INSERT,),
        )

    def test_open_insert_creates_a_delivery(self):
        self._prepare_insert()
        http, _payload, task, outcome = self._post(
            {
                "category": "order",
                "level": "auto",
                "solver": "fas",
                "orders": [
                    {
                        "name": INSERT,
                        "action": "insert",
                        "item": "01.04.00599",
                        "quantity": 2,
                        "due": "2026-10-23T23:59:59",
                    }
                ],
            }
        )
        self.assertEqual(http, 202)
        self.assertEqual(task["status"], "Done")
        self.assertTrue(outcome["status"])
        plans = self._plans(INSERT)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["type"], "DLVR")
        self.assertEqual(plans[0]["quantity"], "2.00000000")
        self.assertIn("2026-10-19 08:00:00", plans[0]["startdate"])

    def test_already_planned_and_closed_inserts_are_rejected(self):
        http, payload, _task, _outcome = self._post(
            {
                "category": "order",
                "solver": "fas",
                "orders": [{"name": PLANNED, "action": "insert", "quantity": 2}],
            }
        )
        self.assertEqual(http, 400)
        self.assertIn("already present", payload["message"])
        http, payload, _task, _outcome = self._post(
            {
                "category": "order",
                "solver": "fas",
                "orders": [{"name": CLOSED, "action": "insert", "quantity": 2}],
            }
        )
        self.assertEqual(http, 400)
        self.assertIn("must be open", payload["message"])
        demand = self.base._query(
            "select status, due::text as due from demand where name = %s",
            (CLOSED,),
        )[0]
        self.assertEqual(demand["status"], "closed")
        self.assertIn("2026-10-22", demand["due"])
        self.assertEqual(self._plans(CLOSED), [])

    def test_delete_removes_the_demand_and_its_plan(self):
        before = self._plans(DELETE)
        self.assertTrue(before)
        http, _payload, task, outcome = self._post(
            {
                "category": "order",
                "solver": "fas",
                "orders": [{"name": DELETE, "action": "delete"}],
            }
        )
        self.assertEqual(http, 202)
        self.assertEqual(task["status"], "Done")
        self.assertTrue(outcome["status"])
        self.assertEqual(
            self.base._query("select name from demand where name = %s", (DELETE,)),
            [],
        )
        self.assertEqual(self._plans(DELETE), [])

    def test_quantity_change_shrinks_the_manufacturing_order(self):
        http, _payload, task, outcome = self._post(
            {
                "category": "order",
                "solver": "fas",
                "orders": [{"name": QTY, "action": "change", "quantity": 300}],
            }
        )
        self.assertEqual(http, 202)
        self.assertEqual(task["status"], "Done")
        self.assertTrue(outcome["status"])
        plans = self._plans(QTY)
        self.assertEqual(plans[0]["quantity"], "300.00000000")
        self.assertIn("2026-10-18 18:40:58", plans[0]["startdate"])
        self.assertIn("2026-10-19 01:00:58", plans[0]["enddate"])

    def test_h04_stop_clears_the_window(self):
        self.assertGreater(self._overlap(), 0)
        http, _payload, task, outcome = self._post(
            {
                "category": "resource",
                "solver": "fas",
                "resources": [
                    {
                        "name": "H04",
                        "start": H04_START,
                        "end": H04_END,
                        "capacity": 0,
                    }
                ],
            }
        )
        self.assertEqual(http, 202)
        self.assertEqual(task["status"], "Done")
        self.assertTrue(outcome["status"])
        self.assertEqual(self._overlap(), 0)

    def test_onhand_change_is_kept(self):
        self.assertEqual(self._onhand(), "87.00000000")
        http, _payload, task, outcome = self._post(
            {
                "category": "material",
                "solver": "fas",
                "materials": [
                    {"name": "01.01.00660B", "location": "FC_FACTORY", "onhand": 10}
                ],
            }
        )
        self.assertEqual(http, 202)
        self.assertEqual(task["status"], "Done")
        self.assertTrue(outcome["status"])
        self.assertEqual(self._onhand(), "10.00000000")

    def test_mixed_level_2_applies_every_change(self):
        http, _payload, task, outcome = self._post(
            {
                "category": "all",
                "level": "2",
                "solver": "fas",
                "orders": [{"name": QTY, "action": "change", "quantity": 300}],
                "materials": [
                    {"name": "01.01.00660B", "location": "FC_FACTORY", "onhand": 10}
                ],
                "resources": [
                    {
                        "name": "H04",
                        "start": H04_START,
                        "end": H04_END,
                        "capacity": 0,
                    }
                ],
            }
        )
        self.assertEqual(http, 202)
        self.assertEqual(task["status"], "Done")
        self.assertTrue(outcome["status"])
        self.assertEqual(outcome["level_used"], 2)
        plans = self._plans(QTY)
        self.assertEqual(plans[0]["quantity"], "300.00000000")
        self.assertEqual(self._onhand(), "10.00000000")
        self.assertEqual(self._overlap(), 0)

    def test_invalid_requests_are_http_400(self):
        bodies = [
            {"solver": "fas", "orders": [{"name": PLANNED, "action": "delete"}]},
            {
                "category": "order",
                "solver": "fas",
                "orders": [{"name": QTY, "action": "change", "quantity": 300}],
                "materials": [{"name": "01.01.00660B", "onhand": 10}],
            },
            {
                "category": "order",
                "solver": "fas",
                "orders": [{"name": "NO_SUCH_ORDER", "action": "delete"}],
            },
            {
                "category": "resource",
                "solver": "fas",
                "resources": [{"name": "H04", "capacity": 0}],
            },
            {
                "category": "resource",
                "solver": "fas",
                "resources": [
                    {
                        "name": "NO_SUCH_RESOURCE",
                        "start": H04_START,
                        "end": H04_END,
                        "capacity": 0,
                    }
                ],
            },
            {
                "category": "material",
                "solver": "fas",
                "materials": [{"name": "NO_SUCH_ITEM", "onhand": 1}],
            },
            {
                "category": "order",
                "solver": "fas",
                "orders": [{"name": "NO_SUCH_DEMAND", "action": "insert", "quantity": 1}],
            },
            {
                "category": "resource",
                "solver": "fas",
                "resources": [
                    {
                        "name": "H04",
                        "start": H04_END,
                        "end": H04_START,
                        "capacity": 0,
                    }
                ],
            },
        ]
        for body in bodies:
            http, payload, _task, _outcome = self._post(body)
            self.assertEqual(http, 400, msg=payload)
            self.assertTrue(payload.get("message"))


if __name__ == "__main__":
    unittest.main()

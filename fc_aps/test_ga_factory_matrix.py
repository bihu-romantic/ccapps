"""GA reschedule matrix on a clean demo_data/fc_factory load.

Skipped unless PostgreSQL scenario9 can be restored from the fc_factory
dump and bin/libccAPPS.so contains the GA baseline gate. The committed
engine binary does not, so a run without that rebuild skips.

GA sequences work orders that already exist. It does not create a new
manufacturing order for an open insert, and it does not resize a work
order when the demand quantity changes. A combined request is accepted
when it creates no new shortage or overload on top of the loaded plan.
A failed solve puts the request's demand, on-hand and capacity writes
back. Closed inserts are rejected before the solve.
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "bin" / "libccAPPS.so"
BASELINE = Path(__file__).with_name("test_ga_factory_baseline.py")

QTY = "FC_UNIN_QY_01.04.00792_SCQY2610000136_R00071"
PLANNED = "FC_ALARM_QYJG_01.04.00599_20261017_R00321"
CLOSED = "FC_ALARM_QY_01.04.00773_20261022_R00419"
DELETE = "FC_ALARM_QYJG_01.04.00648_20261017_R00362"
INSERT = "FC_INSERT_01.04.00599_20261023"
H04_START = "2026-10-16T08:00:00"
H04_END = "2026-10-17T08:00:00"
QTY_ORIGINAL_START = "2026-10-18 18:40:58"
QTY_ORIGINAL_END = "2026-10-19 07:20:58"


def _baseline():
    spec = importlib.util.spec_from_file_location("ga_factory_baseline", BASELINE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _skip_reason(base):
    if not ENGINE.is_file() or b"GA local baseline gate" not in ENGINE.read_bytes():
        return (
            "bin/libccAPPS.so has no GA local baseline gate. The committed "
            "library is older than this source. Rebuild the engine locally; "
            "that binary is not committed."
        )
    if not Path(base.DUMP).is_file():
        return (
            "fc_factory dump is not at %s; load demo_data/fc_factory into "
            "scenario9 and pg_dump it before running this live check" % base.DUMP
        )
    try:
        base._connect().close()
    except Exception as exc:
        return "scenario9 is not reachable: %s" % exc
    return None


class FactoryGaMatrixTests(unittest.TestCase):
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
        self._quiesce()
        self.base._restore()
        self.base._pin_ga_parameters()
        self._copy_admin()

    def _quiesce(self):
        subprocess.run(
            ["pkill", "-f", "ccAPPSctl.py runworker --database=scenario9"],
            check=False,
            capture_output=True,
        )
        time.sleep(0.2)

    def _copy_admin(self):
        import psycopg2

        env = self.base._env()
        src = psycopg2.connect(
            host=env["POSTGRES_HOST"],
            dbname="ccapps0",
            user=env["POSTGRES_USER"],
            password=env["POSTGRES_PASSWORD"],
        )
        dst = psycopg2.connect(
            host=env["POSTGRES_HOST"],
            dbname="ccapps9",
            user=env["POSTGRES_USER"],
            password=env["POSTGRES_PASSWORD"],
        )
        src.autocommit = True
        dst.autocommit = True
        try:
            with src.cursor() as cur:
                cur.execute(
                    """
                    select column_name from information_schema.columns
                    where table_schema = 'public' and table_name = 'common_user'
                    order by ordinal_position
                    """
                )
                columns = [row[0] for row in cur.fetchall()]
                listed = ", ".join(columns)
                cur.execute(
                    "select %s from common_user where username = 'admin'" % listed
                )
                row = cur.fetchone()
            if row is None:
                self.skipTest("default database has no admin user")
            with dst.cursor() as cur:
                cur.execute("delete from common_user where username = 'admin'")
                cur.execute(
                    "insert into common_user (%s) values (%s)"
                    % (listed, ", ".join(["%s"] * len(columns))),
                    row,
                )
        finally:
            src.close()
            dst.close()

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
            deadline = time.time() + 180
            while time.time() < deadline:
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
                    else:
                        stored = self.base._query(
                            """
                            select value from common_parameter
                            where name = 'local.last_outcome'
                            """
                        )
                        if stored and stored[0]["value"]:
                            try:
                                parsed = json.loads(stored[0]["value"])
                            except json.JSONDecodeError:
                                parsed = None
                            if isinstance(parsed, dict) and parsed.get("solver") == "ga":
                                outcome = parsed
                    break
                time.sleep(0.5)
            self.assertIsNotNone(task, msg="reschedule task did not finish")
            self._quiesce()
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
        return self.base._query(
            """
            select count(*)::int as n
            from operationplanresource opr
            join operationplan op on op.reference = opr.operationplan_id
            where opr.resource_id = 'H04'
              and op.startdate < '2026-10-17 08:00:00+02'
              and op.enddate > '2026-10-16 08:00:00+02'
            """
        )[0]["n"]

    def _prepare_insert(self):
        self.base._query(
            """
            insert into demand (
                name, item_id, location_id, customer_id, due, status, quantity,
                priority, description, quote_enabled, policy, source
            ) values (
                %s, '01.04.00599', 'FC_FACTORY', 'FC_CUSTOMER',
                '2026-10-23 23:59:59+02', 'open', 2, 100,
                'ga matrix insert', false, 'independent', 'ga-matrix'
            )
            """,
            (INSERT,),
        )

    def test_open_insert_does_not_create_supply(self):
        self._prepare_insert()
        http, _payload, task, outcome = self._post(
            {
                "category": "order",
                "level": "auto",
                "solver": "ga",
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
        self.assertEqual(task["status"], "Failed")
        self.assertFalse(outcome["status"])
        self.assertEqual(outcome["stage"], "local_scope_empty")
        self.assertEqual(outcome["applied"], 0)
        demand = self.base._query(
            "select status, quantity::text as quantity from demand where name = %s",
            (INSERT,),
        )[0]
        self.assertEqual(demand["status"], "open")
        self.assertEqual(demand["quantity"], "2.00000000")
        self.assertEqual(self._plans(INSERT), [])
        plans = self._plans(QTY)
        self.assertEqual(plans[0]["quantity"], "600.00000000")
        self.assertIn(QTY_ORIGINAL_START, plans[0]["startdate"])
        self.assertIn(QTY_ORIGINAL_END, plans[0]["enddate"])

    def test_already_planned_and_closed_inserts_are_rejected(self):
        http, payload, _task, _outcome = self._post(
            {
                "category": "order",
                "solver": "ga",
                "orders": [{"name": PLANNED, "action": "insert", "quantity": 2}],
            }
        )
        self.assertEqual(http, 400)
        self.assertIn("already present", payload["message"])
        http, payload, _task, _outcome = self._post(
            {
                "category": "order",
                "solver": "ga",
                "orders": [{"name": CLOSED, "action": "insert", "quantity": 2}],
            }
        )
        self.assertEqual(http, 400)
        self.assertIn("must be open", payload["message"])
        self.assertIn("GA can sequence", payload["message"])
        demand = self.base._query(
            "select status, due::text as due from demand where name = %s",
            (CLOSED,),
        )[0]
        self.assertEqual(demand["status"], "closed")
        self.assertIn("2026-10-22", demand["due"])
        self.assertEqual(self._plans(CLOSED), [])

    def test_delete_removes_the_demand_and_its_plan(self):
        self.assertTrue(self._plans(DELETE))
        http, _payload, task, outcome = self._post(
            {
                "category": "order",
                "solver": "ga",
                "orders": [{"name": DELETE, "action": "delete"}],
            }
        )
        self.assertEqual(http, 202)
        self.assertEqual(task["status"], "Done")
        self.assertTrue(outcome["status"])
        self.assertEqual(outcome["stage"], "local_delete_complete")
        self.assertEqual(
            self.base._query("select name from demand where name = %s", (DELETE,)),
            [],
        )
        self.assertEqual(self._plans(DELETE), [])

    def test_quantity_change_moves_the_existing_order(self):
        http, _payload, task, outcome = self._post(
            {
                "category": "order",
                "solver": "ga",
                "orders": [{"name": QTY, "action": "change", "quantity": 300}],
            }
        )
        self.assertEqual(http, 202)
        self.assertEqual(task["status"], "Done")
        self.assertTrue(outcome["status"])
        self.assertEqual(outcome["level_used"], 1)
        self.assertEqual(outcome["applied"], 1)
        self.assertEqual(outcome["incomplete"], 0)
        self.assertEqual(outcome["named_late"], 1)
        demand = self.base._query(
            "select quantity::text as quantity from demand where name = %s",
            (QTY,),
        )[0]
        self.assertEqual(demand["quantity"], "300.00000000")
        plans = self._plans(QTY)
        self.assertEqual(plans[0]["type"], "MO")
        self.assertEqual(plans[0]["quantity"], "600.00000000")
        self.assertIn("2026-10-20 01:00:11", plans[0]["startdate"])
        self.assertIn("2026-10-20 13:40:11", plans[0]["enddate"])

    def test_h04_stop_clears_the_window(self):
        self.assertEqual(self._overlap(), 5)
        http, _payload, task, outcome = self._post(
            {
                "category": "resource",
                "solver": "ga",
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
        self.assertEqual(outcome["stage"], "local_ga_complete")
        self.assertEqual(self._overlap(), 0)
        calendar = self.base._query(
            "select maximum_calendar_id as calendar from resource where name = 'H04'"
        )[0]
        self.assertEqual(calendar["calendar"], "H04 capacity")
        stopped = self.base._query(
            """
            select value::text as value
            from calendarbucket
            where calendar_id = 'H04 capacity' and value = 0
            """
        )
        self.assertTrue(stopped)

    def test_onhand_change_without_a_consumer_is_kept(self):
        self.assertEqual(self._onhand(), "87.00000000")
        before = self._plans(QTY)
        http, _payload, task, outcome = self._post(
            {
                "category": "material",
                "solver": "ga",
                "materials": [
                    {"name": "01.01.00660B", "location": "FC_FACTORY", "onhand": 10}
                ],
            }
        )
        self.assertEqual(http, 202)
        self.assertEqual(task["status"], "Done")
        self.assertTrue(outcome["status"])
        self.assertEqual(outcome["stage"], "local_material_feasible")
        self.assertEqual(self._onhand(), "10.00000000")
        self.assertEqual(self._plans(QTY), before)

    def test_mixed_level_2_keeps_every_change(self):
        http, _payload, task, outcome = self._post(
            {
                "category": "all",
                "level": "2",
                "solver": "ga",
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
        self.assertEqual(outcome["incomplete"], 0)
        self.assertEqual(outcome["applied"], 1)
        demand = self.base._query(
            "select quantity::text as quantity from demand where name = %s",
            (QTY,),
        )[0]
        self.assertEqual(demand["quantity"], "300.00000000")
        plans = self._plans(QTY)
        self.assertEqual(plans[0]["quantity"], "600.00000000")
        self.assertIn("2026-10-20 08:10:25", plans[0]["startdate"])
        self.assertIn("2026-10-20 20:50:25", plans[0]["enddate"])
        self.assertEqual(self._onhand(), "10.00000000")
        self.assertEqual(self._overlap(), 0)
        calendar = self.base._query(
            "select maximum_calendar_id as calendar from resource where name = 'H04'"
        )[0]
        self.assertEqual(calendar["calendar"], "H04 capacity")

    def test_invalid_requests_are_http_400(self):
        bodies = [
            {"solver": "ga", "orders": [{"name": PLANNED, "action": "delete"}]},
            {
                "category": "order",
                "solver": "ga",
                "orders": [{"name": QTY, "action": "change", "quantity": 300}],
                "materials": [{"name": "01.01.00660B", "onhand": 10}],
            },
            {
                "category": "order",
                "solver": "ga",
                "orders": [{"name": "NO_SUCH_ORDER", "action": "delete"}],
            },
            {
                "category": "resource",
                "solver": "ga",
                "resources": [{"name": "H04", "capacity": 0}],
            },
            {
                "category": "resource",
                "solver": "ga",
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
                "solver": "ga",
                "materials": [{"name": "NO_SUCH_ITEM", "onhand": 1}],
            },
            {
                "category": "order",
                "solver": "ga",
                "orders": [{"name": "NO_SUCH_DEMAND", "action": "insert", "quantity": 1}],
            },
            {
                "category": "resource",
                "solver": "ga",
                "resources": [
                    {
                        "name": "H04",
                        "start": H04_END,
                        "end": H04_START,
                        "capacity": 0,
                    }
                ],
            },
            {
                "category": "order",
                "solver": "nsga",
                "orders": [{"name": "NO_SUCH_DEMAND", "action": "insert", "quantity": 1}],
            },
            {
                "category": "order",
                "solver": "genetic",
                "orders": [{"name": "NO_SUCH_DEMAND", "action": "insert", "quantity": 1}],
            },
        ]
        for body in bodies:
            before_onhand = self._onhand()
            before_qty = self.base._query(
                "select quantity::text as quantity, due::text as due from demand where name = %s",
                (QTY,),
            )
            before_calendar = self.base._query(
                "select maximum_calendar_id as calendar from resource where name = 'H04'"
            )
            http, payload, _task, _outcome = self._post(body)
            self.assertEqual(http, 400, msg=payload)
            self.assertTrue(payload.get("message"))
            self.assertNotIn("solver must be", payload["message"])
            self.assertEqual(self._onhand(), before_onhand)
            self.assertEqual(
                self.base._query(
                    "select quantity::text as quantity, due::text as due from demand where name = %s",
                    (QTY,),
                ),
                before_qty,
            )
            self.assertEqual(
                self.base._query(
                    "select maximum_calendar_id as calendar from resource where name = 'H04'"
                ),
                before_calendar,
            )


if __name__ == "__main__":
    unittest.main()

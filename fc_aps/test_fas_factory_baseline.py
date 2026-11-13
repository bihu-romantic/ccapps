"""Normal FAS plan and failed-input rollback on demo_data/fc_factory.

The comparison is the loaded scenario9 database, restored from the fc_factory
dump when that dump is present. A full plan with reschedule off must leave
plan counts, a sample of start/end/quantity/status, and problem categories
where the fixture left them.
"""
import hashlib
import json
import os
import subprocess
import unittest
from decimal import Decimal
from pathlib import Path

DUMP = os.environ.get("FC_FACTORY_DUMP", "/tmp/fc_factory_clean.dump")
ROOT = Path(__file__).resolve().parents[1]
DEMAND = "FC_UNIN_QY_01.04.00792_SCQY2610000136_R00071"
EXPECTED_COUNTS = {
    ("STCK", "closed"): 2404,
    ("MO", "proposed"): 172,
    ("DLVR", "proposed"): 70,
}
# Business columns of operationplan after one non-reschedule FAS run.
# Reference, owner and lastmodified are left out: a supply run rewrites
# proposed ids and still leaves this fingerprint unchanged. Wall-clock text
# is read in Europe/Brussels, matching CCAPPS_TIME_ZONE below.
BUSINESS_FINGERPRINT = (
    "11bd855e053f3b34e865c1adcb0edf59bcb53794cb16747cadfcb653ace316c5"
)


def _env():
    env = os.environ.copy()
    env.setdefault("POSTGRES_HOST", "127.0.0.1")
    env.setdefault("POSTGRES_USER", "ccapps")
    env.setdefault("POSTGRES_PASSWORD", "ccapps")
    env.setdefault("POSTGRES_DBNAME", "ccapps")
    env.setdefault("DJANGO_SETTINGS_MODULE", "ccAPPSdb.settings")
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PGPASSWORD"] = env["POSTGRES_PASSWORD"]
    # Pin the engine clock. A machine whose local zone is not Brussels would
    # otherwise change timestamptz text and this fingerprint.
    env["CCAPPS_TIME_ZONE"] = "Europe/Brussels"
    env["PGTZ"] = "Europe/Brussels"
    ortools = ROOT / "third_party" / "or-tools" / "lib"
    env["LD_LIBRARY_PATH"] = str(ortools) + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    return env


def _connect():
    import psycopg2

    env = _env()
    conn = psycopg2.connect(
        host=env["POSTGRES_HOST"],
        dbname="ccapps9",
        user=env["POSTGRES_USER"],
        password=env["POSTGRES_PASSWORD"],
    )
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("set time zone 'Europe/Brussels'")
    return conn


def _query(sql, args=None):
    conn = _connect()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql, args or ())
            if cur.description:
                cols = [c[0] for c in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
            return []
    finally:
        conn.close()


def _restore():
    env = _env()
    subprocess.run(
        [
            "dropdb",
            "-h",
            env["POSTGRES_HOST"],
            "-U",
            env["POSTGRES_USER"],
            "--force",
            "--if-exists",
            "ccapps9",
        ],
        check=True,
        env=env,
        capture_output=True,
    )
    subprocess.run(
        [
            "createdb",
            "-h",
            env["POSTGRES_HOST"],
            "-U",
            env["POSTGRES_USER"],
            "ccapps9",
        ],
        check=True,
        env=env,
        capture_output=True,
    )
    restored = subprocess.run(
        [
            "pg_restore",
            "-h",
            env["POSTGRES_HOST"],
            "-U",
            env["POSTGRES_USER"],
            "-d",
            "ccapps9",
            "--no-owner",
            "--no-privileges",
            DUMP,
        ],
        env=env,
        capture_output=True,
    )
    if restored.returncode not in (0, 1):
        raise subprocess.CalledProcessError(
            restored.returncode, restored.args, restored.stdout, restored.stderr
        )


def _business_fingerprint():
    conn = _connect()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("set time zone 'Europe/Brussels'")
            cur.execute(
                """
                select type, status, quantity::text, startdate::text, enddate::text,
                       coalesce(operation_id, ''), coalesce(item_id, ''),
                       coalesce(location_id, ''), coalesce(demand_id, ''),
                       coalesce(source, ''), coalesce(quantity_completed::text, ''),
                       coalesce(batch, ''), coalesce(name, ''), coalesce(due::text, ''),
                       coalesce(delay::text, ''), coalesce(criticality::text, ''),
                       coalesce(color::text, '')
                from operationplan
                order by type, status, coalesce(demand_id, ''),
                         coalesce(operation_id, ''), coalesce(item_id, ''),
                         coalesce(location_id, ''), startdate, enddate, quantity,
                         reference
                """
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    blob = "\n".join(
        "|".join("" if value is None else str(value) for value in row) for row in rows
    ).encode()
    return hashlib.sha256(blob).hexdigest()


def _signature():
    counts = {
        (row["type"], row["status"]): row["n"]
        for row in _query(
            """
            select type, status, count(*)::int as n
            from operationplan
            group by type, status
            """
        )
    }
    # Closed stock is loaded and must stay. Proposed manufacturing orders are
    # not loaded by a supply run, so a full FAS commit can rewrite some of
    # them; the comparison below locks the rows that the fixture run leaves
    # alone, including the order used for the quantity-change scenario.
    stock = _query(
        """
        select status, quantity::text as quantity,
               startdate::text as startdate, enddate::text as enddate,
               coalesce(item_id, '') as item,
               coalesce(location_id, '') as location
        from operationplan
        where type = 'STCK'
        order by item, location, startdate, enddate, quantity
        """
    )
    sample = _query(
        """
        select type, status, quantity::text as quantity,
               startdate::text as startdate, enddate::text as enddate,
               coalesce(operation_id, '') as operation
        from operationplan
        where demand_id = %s
        order by operation, startdate, enddate, quantity
        """,
        (DEMAND,),
    )
    return counts, stock, sample


class FactoryFasBaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import psycopg2  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("psycopg2 is not installed")
        if not Path(DUMP).is_file():
            raise unittest.SkipTest("fc_factory dump is not at %s" % DUMP)
        try:
            _connect().close()
        except Exception as exc:
            raise unittest.SkipTest("scenario9 is not reachable: %s" % exc)
        _restore()

    def test_normal_fas_plan_matches_loaded_fixture(self):
        before_counts, before_stock, before_sample = _signature()
        for key, count in EXPECTED_COUNTS.items():
            self.assertEqual(before_counts.get(key), count, msg=before_counts)
        self.assertTrue(before_sample, msg="quantity-change demand has no plan")
        self.assertEqual(before_sample[0]["quantity"], "600.00000000")
        self.assertIn("2026-10-18 18:40:58", before_sample[0]["startdate"])
        self.assertIn("2026-10-19 07:20:58", before_sample[0]["enddate"])
        self.assertEqual(before_sample[0]["status"], "proposed")

        env = _env()
        env["supply"] = "1"
        proc = subprocess.run(
            [
                "python3",
                "ccAPPSctl.py",
                "runplan",
                "--database=scenario9",
                "--constraint=capa,mfg_lt,po_lt",
                "--plantype=1",
                "--env=supply",
                "--user=admin",
            ],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            proc.returncode,
            0,
            msg=proc.stdout[-2000:] + "\n" + proc.stderr[-2000:],
        )
        after_counts, after_stock, after_sample = _signature()
        self.assertEqual(after_counts, before_counts)
        self.assertEqual(after_stock, before_stock)
        self.assertEqual(after_sample, before_sample)
        problems = {
            row["name"]: row["n"]
            for row in _query(
                "select name, count(*)::int as n from out_problem group by name"
            )
        }
        self.assertEqual(problems, {"invalid data": 9013})
        self.assertEqual(_business_fingerprint(), BUSINESS_FINGERPRINT)

    def test_failed_solve_restores_demand_and_onhand(self):
        os.environ.update(_env())
        import django

        django.setup()
        from ccAPPSdb.common.models import Parameter
        from ccAPPSdb.input.models.buffer import Buffer
        from ccAPPSdb.input.models.demand import Demand
        from ccAPPSdb.execute.views import revert_reschedule_inputs

        demand = Demand.objects.using("scenario9").get(name=DEMAND)
        original_qty = demand.quantity
        buffer = Buffer.objects.using("scenario9").get(
            item_id="01.01.00660B", location_id="FC_FACTORY"
        )
        original_onhand = buffer.onhand
        self.assertEqual(original_onhand, Decimal("87"))
        Parameter.objects.using("scenario9").update_or_create(
            name="local.revert_changes",
            defaults={
                "value": json.dumps(
                    {
                        "demands": [
                            {
                                "name": DEMAND,
                                "quantity": str(original_qty),
                                "due": demand.due.isoformat(),
                                "item": demand.item_id,
                            }
                        ],
                        "buffers": [
                            {
                                "item": "01.01.00660B",
                                "location": "FC_FACTORY",
                                "onhand": str(original_onhand),
                            }
                        ],
                        "purchases": [],
                        "resources": [],
                    }
                )
            },
        )
        Demand.objects.using("scenario9").filter(name=DEMAND).update(
            quantity=Decimal("1")
        )
        Buffer.objects.using("scenario9").filter(pk=buffer.pk).update(
            onhand=Decimal("1")
        )
        try:
            self.assertTrue(revert_reschedule_inputs("scenario9"))
            demand.refresh_from_db(using="scenario9")
            buffer.refresh_from_db(using="scenario9")
            self.assertEqual(demand.quantity, original_qty)
            self.assertEqual(buffer.onhand, original_onhand)
        finally:
            Demand.objects.using("scenario9").filter(name=DEMAND).update(
                quantity=original_qty
            )
            Buffer.objects.using("scenario9").filter(pk=buffer.pk).update(
                onhand=original_onhand
            )


if __name__ == "__main__":
    unittest.main()

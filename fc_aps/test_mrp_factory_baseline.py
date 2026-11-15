"""Non-reschedule MRP plan on a clean demo_data/fc_factory load.

Default CI does not execute this file. It runs ./test/runtest.py and
./frepplectl.py test freppledb. The live checks skip when the fc_factory
dump is absent; that dump is not committed.

The command under test is a supply run with plan.solver=mrp,
local.reschedule=0 and plan.skip_supply=0. Reference, owner and
lastmodified are left out of the fingerprint. A supply import does not
keep proposed manufacturing orders, so the committed plan is the MRP
result, not the fixture's proposed rows. The fingerprint below was
measured with the clock pinned to Europe/Brussels on the committed
library and on a Release rebuild of this source.
"""
import hashlib
import os
import subprocess
import sys
import unittest
from pathlib import Path

DUMP = os.environ.get("FC_FACTORY_DUMP", "/tmp/fc_factory_clean.dump")
ROOT = Path(__file__).resolve().parents[1]
BUSINESS_FINGERPRINT = (
    "8612f1dd557347cd232a600cd109f39d370daa439dc0049de26ab82ba0fa1a00"
)
EXPECTED_COUNTS = {
    ("DLVR", "proposed"): 70,
    ("MO", "proposed"): 198,
    ("STCK", "closed"): 2404,
}
DEMAND = "FC_UNIN_QY_01.04.00792_SCQY2610000136_R00071"


def _env():
    env = os.environ.copy()
    env.setdefault("POSTGRES_HOST", "127.0.0.1")
    env.setdefault("POSTGRES_USER", "ccapps")
    env.setdefault("POSTGRES_PASSWORD", "ccapps")
    env.setdefault("POSTGRES_DBNAME", "ccapps")
    env.setdefault("DJANGO_SETTINGS_MODULE", "ccAPPSdb.settings")
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PGPASSWORD"] = env["POSTGRES_PASSWORD"]
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


class FactoryMrpBaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import psycopg2  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("psycopg2 is not installed")
        if not Path(DUMP).is_file():
            raise unittest.SkipTest(
                "fc_factory dump is not at %s; load demo_data/fc_factory into "
                "scenario9 and pg_dump it before running this live check" % DUMP
            )
        try:
            _connect().close()
        except Exception as exc:
            raise unittest.SkipTest("scenario9 is not reachable: %s" % exc)

    def _prepare_mrp_parameters(self):
        _restore()
        _query(
            """
            update common_parameter set value = 'mrp' where name = 'plan.solver'
            """
        )
        _query(
            """
            update common_parameter set value = '0'
            where name in ('local.reschedule', 'plan.skip_supply')
            """
        )

    def test_non_reschedule_mrp_matches_fingerprint(self):
        self._prepare_mrp_parameters()
        env = _env()
        proc = subprocess.run(
            [
                sys.executable,
                "ccAPPSctl.py",
                "runplan",
                "--database=scenario9",
                "--constraint=capa,mfg_lt,po_lt",
                "--plantype=1",
                "--env=supply",
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
        tasks = _query(
            """
            select status from execute_log
            where name = 'runplan'
            order by id desc
            limit 1
            """
        )
        self.assertEqual(tasks[0]["status"], "Done")
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
        self.assertEqual(counts, EXPECTED_COUNTS)
        problems = {
            row["name"]: row["n"]
            for row in _query(
                "select name, count(*)::int as n from out_problem group by name"
            )
        }
        self.assertEqual(problems, {"invalid data": 9013})
        self.assertEqual(_business_fingerprint(), BUSINESS_FINGERPRINT)

    def test_failed_mrp_delete_restores_the_demand_and_plan(self):
        self._prepare_mrp_parameters()
        os.environ.update(_env())
        sys.path.insert(0, str(ROOT))
        import django

        django.setup()
        from ccAPPSdb.common.models import Parameter
        from ccAPPSdb.execute.views import (
            remember_deleted_mrp_orders,
            revert_reschedule_inputs,
        )
        from ccAPPSdb.input.models.demand import Demand

        before = _query(
            """
            select type, status, quantity::text as quantity
            from operationplan
            where demand_id = %s
            order by type, quantity
            """,
            (DEMAND,),
        )
        self.assertTrue(before)
        Parameter.objects.using("scenario9").update_or_create(
            name="local.revert_changes",
            defaults={"value": "{}"},
        )
        remember_deleted_mrp_orders("scenario9", [DEMAND])
        Demand.objects.using("scenario9").filter(name=DEMAND).delete()
        self.assertEqual(
            _query("select name from demand where name = %s", (DEMAND,)),
            [],
        )
        self.assertTrue(revert_reschedule_inputs("scenario9"))
        demand = _query(
            "select status, quantity::text as quantity from demand where name = %s",
            (DEMAND,),
        )
        self.assertEqual(demand[0]["status"], "open")
        self.assertEqual(demand[0]["quantity"], "600.00000000")
        self.assertEqual(
            _query(
                """
                select type, status, quantity::text as quantity
                from operationplan
                where demand_id = %s
                order by type, quantity
                """,
                (DEMAND,),
            ),
            before,
        )


if __name__ == "__main__":
    unittest.main()

"""Non-reschedule GA plan on a clean demo_data/fc_factory load.

Default CI does not execute this file. It runs ./test/runtest.py and
./frepplectl.py test freppledb. The live check skips when the fc_factory
dump is absent; that dump is not committed.

The command under test is a supply run with plan.solver=ga,
local.reschedule=0 and plan.skip_supply=0. GA's first stage is MRP
(ga.baseline=mrp). The genetic stage is pinned so two runs of one binary
match: seed 123456, population 20, generations 5, stagnation 10, VNS on
at ratio 0.02, time limit 3600s (it does not bind), log level 1.
Reference, owner and lastmodified are left out of the fingerprint. The
clock is Europe/Brussels.

The reported GA score, makespan and plan counts match the two runs taken
before the reschedule edit. Delivery commitments are aligned in reference
order so the operation-plan hash below is the same on a second run.
"""
import hashlib
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

DUMP = os.environ.get("FC_FACTORY_DUMP", "/tmp/fc_factory_clean.dump")
ROOT = Path(__file__).resolve().parents[1]
BUSINESS_FINGERPRINT = (
    "4ca06b4222000da0a5c980ad0a365edb5ebf3b4f3dad90cdaa6514e7638c880c"
)
EXPECTED_COUNTS = {
    ("DLVR", "proposed"): 70,
    ("MO", "proposed"): 192,
    ("STCK", "closed"): 2404,
}
GA_PARAMETERS = {
    "plan.solver": "ga",
    "local.reschedule": "0",
    "plan.skip_supply": "0",
    "ga.baseline": "mrp",
    "ga.seed": "123456",
    "ga.objective_profile": "due_date",
    "ga.material_mode": "postcheck",
    "ga.stagnation_limit": "10",
    "ga.timeout_seconds": "3600",
}


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
    env["GA_POPULATION"] = "20"
    env["GA_GENERATIONS"] = "5"
    env["GA_TIME_LIMIT_SECONDS"] = "3600"
    env["GA_STAGNATION_LIMIT"] = "10"
    env["GA_ENABLE_VNS"] = "true"
    env["GA_VNS_RATIO"] = "0.02"
    env["GA_LOGLEVEL"] = "1"
    env["GA_TIMEOUT_SECONDS"] = "3600"
    # Keep the engine on the bundled OR-Tools build. An extra directory on
    # LD_LIBRARY_PATH changes delivery-alignment order without changing the
    # GA score, so the fingerprint would not be stable.
    env["LD_LIBRARY_PATH"] = str(ROOT / "third_party" / "or-tools" / "lib")
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


def _pin_ga_parameters():
    for name, value in GA_PARAMETERS.items():
        _query(
            """
            insert into common_parameter (name, value, lastmodified)
            values (%s, %s, now())
            on conflict (name) do update set value = excluded.value
            """,
            (name, value),
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


class FactoryGaBaselineTests(unittest.TestCase):
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

    def test_non_reschedule_ga_matches_fingerprint(self):
        _restore()
        _pin_ga_parameters()
        env = _env()
        started = time.time()
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
        logs = [
            path.read_text(errors="replace")
            for path in (ROOT / "logs").glob("CCAPPS_scenario9-*.log")
            if path.stat().st_mtime >= started - 1
        ]
        self.assertTrue(
            any(
                "GA: score=0.0830729 (makespan=592212" in text
                and "stop=generations, steps=5" in text
                for text in logs
            ),
            msg="GA score line was not written",
        )


if __name__ == "__main__":
    unittest.main()

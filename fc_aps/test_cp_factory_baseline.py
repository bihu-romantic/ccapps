"""Non-reschedule CP plan on a clean demo_data/fc_factory load.

Default CI does not execute this file. It runs ./test/runtest.py and
./frepplectl.py test freppledb. The live check skips when the fc_factory
dump is absent, and when bin/libccAPPS.so was not rebuilt from this
source. That dump and the rebuilt library are not committed.

The command under test is a supply run with plan.solver=cp,
local.reschedule=0 and plan.skip_supply=0. CP then sequences the FAS
baseline (cp.baseline=fas). The fixture's own limits are used:
cp.time_limit_seconds=120 and cp.preflight_seconds=90. CP_WORKERS=1
is set here so the time-capped search is the same on every run. The
clock is Europe/Brussels. Reference, owner and lastmodified are left
out of the fingerprint.

Two runs of this command on the Release rebuild produced the same
fingerprint, counts, sample row and solver point.
"""
import hashlib
import os
import subprocess
import sys
import unittest
from pathlib import Path

DUMP = os.environ.get("FC_FACTORY_DUMP", "/tmp/fc_factory_clean.dump")
ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "bin" / "libccAPPS.so"
ENGINE_MARKER = b"CP local material: no work order uses the changed buffer"
BUSINESS_FINGERPRINT = (
    "f5525e506bda69bf77eb6193c8c5b59db0dbc6ceee8ffc1e772a1a1ec06823c3"
)
EXPECTED_COUNTS = {
    ("DLVR", "proposed"): 69,
    ("MO", "proposed"): 138,
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
    # One search worker. The parallel portfolio is not stable, and the
    # 120s fixture limit stops on a feasible point rather than a proof.
    env["CP_WORKERS"] = "1"
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


class FactoryCpBaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import psycopg2  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("psycopg2 is not installed")
        if not ENGINE.is_file() or ENGINE_MARKER not in ENGINE.read_bytes():
            raise unittest.SkipTest(
                "bin/libccAPPS.so has no CP no-root material path. The "
                "committed library is older than this source. Rebuild the "
                "engine locally; that binary is not committed. The fingerprint "
                "below was measured on that Release rebuild with CP_WORKERS=1."
            )
        if not Path(DUMP).is_file():
            raise unittest.SkipTest(
                "fc_factory dump is not at %s; load demo_data/fc_factory into "
                "scenario9 and pg_dump it before running this live check" % DUMP
            )
        try:
            _connect().close()
        except Exception as exc:
            raise unittest.SkipTest("scenario9 is not reachable: %s" % exc)

    def _prepare_cp_parameters(self):
        _restore()
        _query(
            """
            update common_parameter set value = 'cp' where name = 'plan.solver'
            """
        )
        _query(
            """
            update common_parameter set value = '0'
            where name in ('local.reschedule', 'plan.skip_supply')
            """
        )

    def test_non_reschedule_cp_matches_fingerprint(self):
        self._prepare_cp_parameters()
        limits = {
            row["name"]: row["value"]
            for row in _query(
                """
                select name, value from common_parameter
                where name in (
                    'cp.baseline', 'cp.time_limit_seconds', 'cp.preflight_seconds',
                    'cp.planning_mode', 'cp.horizon_days', 'local.reschedule',
                    'plan.skip_supply', 'plan.solver'
                )
                """
            )
        }
        self.assertEqual(limits["plan.solver"], "cp")
        self.assertEqual(limits["cp.baseline"], "fas")
        self.assertEqual(limits["cp.time_limit_seconds"], "120")
        self.assertEqual(limits["cp.preflight_seconds"], "90")
        self.assertEqual(limits["cp.planning_mode"], "throughput")
        self.assertEqual(limits["cp.horizon_days"], "3")
        self.assertEqual(limits["local.reschedule"], "0")
        self.assertEqual(limits["plan.skip_supply"], "0")
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
        sample = _query(
            """
            select type, status, quantity::text as quantity,
                   startdate::text as startdate, enddate::text as enddate
            from operationplan
            where demand_id = %s
            order by type, startdate
            """,
            (DEMAND,),
        )
        self.assertEqual(len(sample), 1)
        self.assertEqual(sample[0]["type"], "MO")
        self.assertEqual(sample[0]["status"], "proposed")
        self.assertEqual(sample[0]["quantity"], "600.00000000")
        self.assertIn("2026-10-17 05:47:30", sample[0]["startdate"])
        self.assertIn("2026-10-17 18:27:30", sample[0]["enddate"])
        problems = {
            row["name"]: row["n"]
            for row in _query(
                "select name, count(*)::int as n from out_problem group by name"
            )
        }
        self.assertEqual(problems, {"invalid data": 9013, "material shortage": 9})
        self.assertEqual(_business_fingerprint(), BUSINESS_FINGERPRINT)


if __name__ == "__main__":
    unittest.main()

"""Run the constraint-relaxation matrix with one engine process per case."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ALGORITHMS = ("mrp", "fas", "cp", "ga")
METHODS = (
    "test_relaxing_capacity_allows_overlap",
    "test_relaxing_mfg_leadtime_allows_past_start",
    "test_relaxing_po_leadtime_allows_past_order_date",
    "test_relaxed_po_leadtime_does_not_invent_supply_path",
)
MASKS = (0, 4, 16, 32, 20, 36, 48, 52)


def main() -> int:
    env = os.environ.copy()
    libraries = f"{ROOT / 'third_party/or-tools/lib'}:{ROOT / 'bin'}"
    env["LD_LIBRARY_PATH"] = (
        libraries + ":" + env["LD_LIBRARY_PATH"]
        if env.get("LD_LIBRARY_PATH")
        else libraries
    )
    passed = 0
    for algorithm in ALGORITHMS:
        env["CCAPPS_CONSTRAINT_TEST_ALGORITHM"] = algorithm
        for mask in MASKS:
            env["CCAPPS_CONSTRAINT_TEST_MASK"] = str(mask)
            target = (
                "fc_aps.test_constraint_relaxation."
                "ConstraintRelaxationTests.test_constraint_combination"
            )
            result = subprocess.run(
                [sys.executable, "-m", "unittest", target],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
            )
            if result.returncode:
                sys.stderr.write(result.stdout)
                sys.stderr.write(result.stderr)
                print(f"{algorithm}: failed constraint mask {mask}")
                return result.returncode
            passed += 1
        # Keep the no-supply-route boundary test in every algorithm.
        for method in (METHODS[-1],):
            target = (
                "fc_aps.test_constraint_relaxation."
                f"ConstraintRelaxationTests.{method}"
            )
            result = subprocess.run(
                [sys.executable, "-m", "unittest", target],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
            )
            if result.returncode:
                sys.stderr.write(result.stdout)
                sys.stderr.write(result.stderr)
                print(f"{algorithm}: failed at {method}")
                return result.returncode
            passed += 1
        print(f"{algorithm}: 8/8 combinations + no-route boundary passed")
    expected = len(ALGORITHMS) * (len(MASKS) + 1)
    print(f"matrix: {passed}/{expected} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

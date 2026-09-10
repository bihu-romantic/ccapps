import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path


DEFAULT_CASES = [
    "load_alternate/load_alternate.xml",
    "constraints_resource_1/constraints_resource_1.xml",
    "constraints_resource_2/constraints_resource_2.xml",
]


CHILD_SCRIPT = r"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, '/home/c/ccAPPS')
import ccAPPS

case = Path(sys.argv[1])
mode = sys.argv[2]
seed = int(sys.argv[3])

xml = case.read_text()
xml = re.sub(r'<\?python.*?\?>', '', xml, flags=re.S)
ccAPPS.readXMLdata(xml, False, False)

if mode == "mrp":
    solver = ccAPPS.solver_mrp(plantype=1, constraints=52, loglevel=0)
else:
    solver = ccAPPS.solverGA(plantype=1, constraints=52, loglevel=0)
    solver.setRunMRP(True)
    solver.setSeed(seed)

solver.solve()
try:
    solver.commit()
except Exception:
    pass

ops = []
for op in ccAPPS.operationplans():
    if getattr(op, "quantity", 0) and getattr(op, "operation", None):
        ops.append(
            {
                "reference": getattr(op, "reference", ""),
                "operation": getattr(op.operation, "name", None),
                "start": str(getattr(op, "start", "")),
                "end": str(getattr(op, "end", "")),
                "quantity": float(getattr(op, "quantity", 0.0)),
            }
        )

ops.sort(key=lambda x: (x["start"], x["end"], x["reference"]))
print(
    json.dumps(
        {
            "case": str(case),
            "mode": mode,
            "op_count": len(ops),
            "first_start": ops[0]["start"] if ops else None,
            "last_end": ops[-1]["end"] if ops else None,
            "ops": ops,
        },
        ensure_ascii=False,
    )
)
"""


def run_case(case_path: Path, mode: str, seed: int) -> dict:
    proc = subprocess.run(
        ["python3", "-c", CHILD_SCRIPT, str(case_path), mode, str(seed)],
        text=True,
        capture_output=True,
    )
    payload = {
        "returncode": proc.returncode,
        "stderr": proc.stderr.strip(),
    }
    if proc.stdout.strip():
        lines = [line for line in proc.stdout.strip().splitlines() if line.strip()]
        payload["stdout_lines"] = lines
        try:
            payload["result"] = json.loads(lines[0])
        except json.JSONDecodeError:
            payload["raw_stdout"] = proc.stdout.strip()
    return payload


def summarize(case_path: Path, mrp: dict, ga: dict) -> dict:
    mrp_result = mrp.get("result", {})
    ga_result = ga.get("result", {})
    mrp_ops = mrp_result.get("ops") or []
    ga_ops = ga_result.get("ops") or []

    def multiset_key(op: dict) -> tuple:
        return (
            op.get("reference"),
            op.get("operation"),
            op.get("start"),
            op.get("end"),
            op.get("quantity"),
        )

    mrp_counter = Counter(multiset_key(op) for op in mrp_ops)
    ga_counter = Counter(multiset_key(op) for op in ga_ops)
    return {
        "case": str(case_path),
        "mrp_rc": mrp["returncode"],
        "ga_rc": ga["returncode"],
        "mrp_op_count": mrp_result.get("op_count"),
        "ga_op_count": ga_result.get("op_count"),
        "mrp_first_start": mrp_result.get("first_start"),
        "ga_first_start": ga_result.get("first_start"),
        "mrp_last_end": mrp_result.get("last_end"),
        "ga_last_end": ga_result.get("last_end"),
        "same_schedule": mrp_result.get("ops") == ga_result.get("ops"),
        "same_time_window": (
            mrp_result.get("first_start") == ga_result.get("first_start")
            and mrp_result.get("last_end") == ga_result.get("last_end")
        ),
        "same_op_multiset": mrp_counter == ga_counter,
    }


def main() -> int:
    seed = 12345
    base = Path("/home/c/ccAPPS/test")
    cases = [base / rel for rel in DEFAULT_CASES]
    if len(sys.argv) > 1:
        cases = [
            Path(arg) if arg.startswith("/") else base / arg
            for arg in sys.argv[1:]
        ]

    summaries = []
    for case_path in cases:
        mrp = run_case(case_path, "mrp", seed)
        ga = run_case(case_path, "ga", seed)
        summaries.append(summarize(case_path, mrp, ga))

    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

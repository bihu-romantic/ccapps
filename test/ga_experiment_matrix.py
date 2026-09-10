import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path("/home/c/ccAPPS")
OUT_JSON = ROOT / "logs" / "ga_experiment_matrix.json"
OUT_CSV = ROOT / "logs" / "ga_experiment_matrix.csv"


EXPERIMENTS = [
    {
        "name": "mrp_baseline",
        "solver": "mrp",
        "seed": 123456,
        "enable_vns": False,
        "overlap_enabled": False,
        "overlap_batch_count": 1,
    },
    {
        "name": "ga_no_vns_no_split",
        "solver": "ga",
        "seed": 123456,
        "enable_vns": False,
        "overlap_enabled": False,
        "overlap_batch_count": 1,
    },
    {
        "name": "ga_vns_no_split",
        "solver": "ga",
        "seed": 123456,
        "enable_vns": True,
        "overlap_enabled": False,
        "overlap_batch_count": 1,
    },
    {
        "name": "ga_vns_split_k2",
        "solver": "ga",
        "seed": 123456,
        "enable_vns": True,
        "overlap_enabled": True,
        "overlap_batch_count": 2,
    },
]


CHILD_TEMPLATE = r"""
import json
import os
import sys

sys.path.insert(0, '/home/c/ccAPPS')

import ccAPPS
from ccAPPSdb.common.commands import PlanTaskRegistry
from ccAPPSdb.execute.commands import SupplyPlanning

database = "__DATABASE__"
solver_name = "__SOLVER__"
seed = __SEED__
enable_vns = __ENABLE_VNS__
overlap_enabled = __OVERLAP_ENABLED__
overlap_batch_count = __OVERLAP_BATCH_COUNT__

ccAPPS.erase(True)

for key in ['supply', 'quote', 'loadplan', 'noexport']:
    os.environ.pop(key, None)
for key in ['nowebservice', 'noexport', 'supply']:
    os.environ[key] = '1'

# Keep the "supply" load behavior so proposed opplans aren't reloaded,
# but disable the actual planning task. We'll invoke MRP/GA manually below.
SupplyPlanning.getWeight = classmethod(lambda cls, **kwargs: -1)

PlanTaskRegistry.run(database=database)

if solver_name == 'mrp':
    solver = ccAPPS.solver_mrp(
        constraints=52,
        plantype=1,
        loglevel=0,
    )
else:
    solver = ccAPPS.solverGA(
        constraints=52,
        plantype=1,
        loglevel=0,
        enable_vns=enable_vns,
        overlap_enabled=overlap_enabled,
        overlap_batch_count=overlap_batch_count,
    )
    solver.setRunMRP(True)
    solver.setSeed(seed)

solver.solve()
try:
    solver.commit()
except Exception:
    pass

def iso(x):
    return str(x) if x is not None else None

def delay_seconds(value):
    if value is None:
        return 0.0
    if hasattr(value, 'total_seconds'):
        return float(value.total_seconds())
    return float(value)

def is_leaf_operation_name(name):
    return bool(name and '_O' in name)

ops = []
for op in ccAPPS.operationplans():
    oper = getattr(op, 'operation', None)
    if not oper or not getattr(op, 'quantity', 0):
        continue
    ops.append(
        {
            'reference': getattr(op, 'reference', ''),
            'operation': getattr(oper, 'name', None),
            'start': iso(getattr(op, 'start', None)),
            'end': iso(getattr(op, 'end', None)),
            'quantity': float(getattr(op, 'quantity', 0.0)),
            'delay': max(0.0, delay_seconds(getattr(op, 'delay', None))),
        }
    )

ops.sort(key=lambda x: (x['start'] or '', x['end'] or '', x['reference']))

leaf_ops = [o for o in ops if is_leaf_operation_name(o['operation'])]
latest_end = max((o['end'] for o in leaf_ops), default=None)
earliest_start = min((o['start'] for o in leaf_ops), default=None)
delay_count = sum(1 for o in ops if o['delay'] and o['delay'] > 0)
max_delay_seconds = max((o['delay'] for o in ops), default=0.0)
sum_delay_seconds = sum((o['delay'] for o in ops), 0.0)

resource_stats = []
resource_load_sum = 0.0
resource_peak_load = 0.0
for res in ccAPPS.resources():
    loads = []
    for lp in res.loadplans:
        qty = float(getattr(lp, 'quantity', 0.0))
        if qty >= 0:
            continue
        opplan = getattr(lp, 'operationplan', None)
        if not opplan:
            continue
        start = iso(getattr(opplan, 'start', None))
        end = iso(getattr(opplan, 'end', None))
        if not start or not end:
            continue
        duration_hours = (
            getattr(opplan, 'end', None) - getattr(opplan, 'start', None)
        ).total_seconds() / 3600.0
        loads.append(duration_hours)
        resource_load_sum += duration_hours
    peak = sum(loads) if loads else 0.0
    resource_peak_load = max(resource_peak_load, peak)
    resource_stats.append(
        {
            'resource': getattr(res, 'name', ''),
            'operation_count': len(loads),
            'load_hours': peak,
        }
    )

span_hours = 0.0
if earliest_start and latest_end:
    try:
        start_dt = min(
            getattr(op, 'start', None)
            for op in ccAPPS.operationplans()
            if getattr(op, 'operation', None)
            and is_leaf_operation_name(getattr(getattr(op, 'operation', None), 'name', None))
            and getattr(op, 'start', None) is not None
        )
        end_dt = max(
            getattr(op, 'end', None)
            for op in ccAPPS.operationplans()
            if getattr(op, 'operation', None)
            and is_leaf_operation_name(getattr(getattr(op, 'operation', None), 'name', None))
            and getattr(op, 'end', None) is not None
        )
        span_hours = (end_dt - start_dt).total_seconds() / 3600.0
    except Exception:
        span_hours = 0.0

avg_utilization = 0.0
peak_utilization = 0.0
if span_hours > 0 and resource_stats:
    avg_utilization = resource_load_sum / (len(resource_stats) * span_hours)
    peak_utilization = resource_peak_load / span_hours

payload = {
    'solver': solver_name,
    'seed': seed,
    'enable_vns': enable_vns,
    'overlap_enabled': overlap_enabled,
    'overlap_batch_count': overlap_batch_count,
    'operationplan_count': len(ops),
    'leaf_count': len(leaf_ops),
    'earliest_leaf_start': earliest_start,
    'latest_leaf_end': latest_end,
    'delay_count': delay_count,
    'max_delay_seconds': max_delay_seconds,
    'sum_delay_seconds': sum_delay_seconds,
    'resource_load_sum_hours': resource_load_sum,
    'resource_peak_load_hours': resource_peak_load,
    'schedule_span_hours': span_hours,
    'average_resource_utilization': avg_utilization,
    'peak_resource_utilization': peak_utilization,
    'resource_count': len(resource_stats),
    'resource_stats_top10': sorted(
        resource_stats, key=lambda x: x['load_hours'], reverse=True
    )[:10],
}

print(json.dumps(payload, ensure_ascii=False))
"""


def extract_last_json_object(text: str) -> dict | None:
    anchor = text.rfind('{"solver":')
    if anchor < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    start = None
    for idx in range(anchor, len(text)):
        ch = text[idx]
        if start is None:
            if ch == "{":
                start = idx
                depth = 1
            continue
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : idx + 1])
                except json.JSONDecodeError:
                    return None
    return None


def run_one(database: str, config: dict) -> dict:
    child = (
        CHILD_TEMPLATE.replace("__DATABASE__", database)
        .replace("__SOLVER__", config["solver"])
        .replace("__SEED__", str(int(config["seed"])))
        .replace("__ENABLE_VNS__", "True" if config["enable_vns"] else "False")
        .replace(
            "__OVERLAP_ENABLED__",
            "True" if config["overlap_enabled"] else "False",
        )
        .replace(
            "__OVERLAP_BATCH_COUNT__", str(int(config["overlap_batch_count"]))
        )
    )
    proc = subprocess.run(
        [
            "./ccAPPSctl.py",
            "shell",
            "--settings=djangosettings",
            "-c",
            child,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    result = {
        "name": config["name"],
        "returncode": proc.returncode,
        "stderr": proc.stderr.strip(),
    }
    payload = extract_last_json_object(proc.stdout)
    if payload:
        result.update(payload)
    else:
        result["stdout"] = proc.stdout.strip()
    return result


def write_csv(rows):
    headers = [
        "name",
        "solver",
        "seed",
        "enable_vns",
        "overlap_enabled",
        "overlap_batch_count",
        "operationplan_count",
        "leaf_count",
        "earliest_leaf_start",
        "latest_leaf_end",
        "delay_count",
        "max_delay_seconds",
        "sum_delay_seconds",
        "resource_load_sum_hours",
        "resource_peak_load_hours",
        "resource_count",
        "returncode",
        "stderr",
    ]
    lines = [",".join(headers)]
    for row in rows:
        vals = []
        for h in headers:
            v = row.get(h, "")
            s = str(v).replace('"', '""')
            if "," in s or "\n" in s:
                s = f'"{s}"'
            vals.append(s)
        lines.append(",".join(vals))
    OUT_CSV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    database = sys.argv[1] if len(sys.argv) > 1 else "scenario2"
    rows = [run_one(database, cfg) for cfg in EXPERIMENTS]
    OUT_JSON.write_text(
        json.dumps({"database": database, "experiments": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_csv(rows)
    print(json.dumps({"json": str(OUT_JSON), "csv": str(OUT_CSV), "experiments": rows}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

import csv
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path


DEFAULT_DATABASE = "scenario2"
DEFAULT_GA_JSON = "/home/c/ccAPPS/logs/scenario2_ga_solution_mapped.json"
DEFAULT_DB_EXPORT_JSON = "/home/c/ccAPPS/logs/scenario2_db_operationplan_mapped.json"
DEFAULT_COMPARE_JSON = "/home/c/ccAPPS/logs/scenario2_ga_vs_db_schedule.json"
DEFAULT_COMPARE_CSV = "/home/c/ccAPPS/logs/scenario2_ga_vs_db_schedule.csv"


LABEL_RE = re.compile(r"^J(\d+)_O(\d+)_M(\d+)$")


def parse_label(label):
    if not label:
        return {
            "matched": False,
            "job_index": None,
            "operation_index": None,
            "machine_index": None,
        }
    m = LABEL_RE.match(label)
    if not m:
        return {
            "matched": False,
            "job_index": None,
            "operation_index": None,
            "machine_index": None,
        }
    return {
        "matched": True,
        "job_index": int(m.group(1)),
        "operation_index": int(m.group(2)),
        "machine_index": int(m.group(3)),
    }


def isoformat(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S") if dt else None


def seconds_from_anchor(dt, anchor):
    if not dt:
        return None
    return int((dt - anchor).total_seconds())


def load_ga_dump(path):
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    anchor = datetime.fromisoformat(payload["time_mapping"]["anchor"])
    ops_by_ref = {}
    for row in payload.get("operations", []):
        ops_by_ref[str(row["reference"])] = row
    return payload, anchor, ops_by_ref


def export_db_rows(database, anchor):
    child_script = r"""
import json
import re
from datetime import datetime
from ccAPPSdb.input.models import OperationPlanResource

LABEL_RE = re.compile(r"^J(\d+)_O(\d+)_M(\d+)$")
anchor = datetime.fromisoformat(%r)
database = %r

def parse_label(label):
    m = LABEL_RE.match(label or "")
    if not m:
        return None, None, None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))

def isoformat(dt):
    return dt.strftime("%%Y-%%m-%%dT%%H:%%M:%%S") if dt else None

def seconds_from_anchor(dt):
    return int((dt - anchor).total_seconds()) if dt else None

rows = []
qs = (
    OperationPlanResource.objects.using(database)
    .select_related("operationplan")
    .exclude(operationplan__operation_id=None)
    .order_by(
        "operationplan__startdate",
        "operationplan__enddate",
        "operationplan_id",
    )
)
for opr in qs.iterator():
    opplan = opr.operationplan
    job_index, operation_index, machine_index = parse_label(opplan.operation_id)
    rows.append(
        {
            "reference": str(opplan.reference),
            "operation": opplan.operation_id,
            "job_index": job_index,
            "operation_index": operation_index,
            "machine_index": machine_index,
            "assigned_machine_name": opr.resource_id,
            "assigned_machine_index": (
                int(opr.resource_id[1:])
                if opr.resource_id and opr.resource_id.startswith("M")
                else None
            ),
            "type": opplan.type,
            "owner_id": str(opplan.owner_id) if opplan.owner_id is not None else None,
            "status": opplan.status,
            "start": isoformat(opplan.startdate),
            "end": isoformat(opplan.enddate),
            "start_seconds_from_plan_current": seconds_from_anchor(opplan.startdate),
            "relative_start_seconds": seconds_from_anchor(opplan.startdate),
            "end_seconds_from_plan_current": seconds_from_anchor(opplan.enddate),
            "relative_end_seconds": seconds_from_anchor(opplan.enddate),
            "duration_seconds": (
                int((opplan.enddate - opplan.startdate).total_seconds())
                if opplan.startdate and opplan.enddate
                else None
            ),
            "resources": [opr.resource_id],
        }
    )

print(json.dumps(rows, ensure_ascii=False))
""" % (anchor.isoformat(), database)

    proc = subprocess.run(
        ["./ccAPPSctl.py", "shell", "--settings=djangosettings", "-c", child_script],
        cwd="/home/c/ccAPPS",
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "Failed to export DB rows:\n%s" % (proc.stderr or proc.stdout or "")
        )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        return []
    return json.loads(lines[-1])


def build_compare_rows(ga_payload, db_rows, ga_by_ref):
    compare_rows = []
    matched = 0
    missing_ga = []
    for db_row in db_rows:
        ref = db_row["reference"]
        ga_row = ga_by_ref.get(ref)
        if ga_row:
            matched += 1
        else:
            missing_ga.append(ref)
        compare_rows.append(
            {
                "reference": ref,
                "job_index": db_row["job_index"],
                "operation_index": db_row["operation_index"],
                "ga_operation": ga_row.get("operation") if ga_row else None,
                "db_operation": db_row["operation"],
                "ga_selected_operation": (
                    ga_row.get("selected_operation") if ga_row else None
                ),
                "db_assigned_machine": db_row["assigned_machine_name"],
                "ga_assigned_machine": (
                    ga_row.get("assigned_machine_name") if ga_row else None
                ),
                "machine_same": (
                    ga_row.get("assigned_machine_name") == db_row["assigned_machine_name"]
                    if ga_row
                    else False
                ),
                "ga_relative_start_seconds": (
                    ga_row.get("relative_start_seconds") if ga_row else None
                ),
                "db_relative_start_seconds": db_row["relative_start_seconds"],
                "start_diff_seconds": (
                    db_row["relative_start_seconds"] - ga_row.get("relative_start_seconds")
                    if ga_row
                    and ga_row.get("relative_start_seconds") is not None
                    and db_row["relative_start_seconds"] is not None
                    else None
                ),
                "ga_relative_end_seconds": (
                    ga_row.get("relative_end_seconds") if ga_row else None
                ),
                "db_relative_end_seconds": db_row["relative_end_seconds"],
                "end_diff_seconds": (
                    db_row["relative_end_seconds"] - ga_row.get("relative_end_seconds")
                    if ga_row
                    and ga_row.get("relative_end_seconds") is not None
                    and db_row["relative_end_seconds"] is not None
                    else None
                ),
                "ga_duration_seconds": (
                    ga_row.get("duration_seconds") if ga_row else None
                ),
                "db_duration_seconds": db_row["duration_seconds"],
                "duration_diff_seconds": (
                    db_row["duration_seconds"] - ga_row.get("duration_seconds")
                    if ga_row
                    and ga_row.get("duration_seconds") is not None
                    and db_row["duration_seconds"] is not None
                    else None
                ),
                "ga_start": ga_row.get("start") if ga_row else None,
                "db_start": db_row["start"],
                "ga_end": ga_row.get("end") if ga_row else None,
                "db_end": db_row["end"],
            }
        )

    db_ref_set = {row["reference"] for row in db_rows}
    missing_db = sorted(
        ref for ref in ga_by_ref.keys() if ref not in db_ref_set
    )
    compare_rows.sort(
        key=lambda row: (
            row["db_relative_start_seconds"]
            if row["db_relative_start_seconds"] is not None
            else 10**18,
            row["db_relative_end_seconds"]
            if row["db_relative_end_seconds"] is not None
            else 10**18,
            row["reference"],
        )
    )
    summary = {
        "ga_source": ga_payload.get("source"),
        "plan_current": ga_payload.get("plan_current"),
        "ga_operation_count": ga_payload.get("operation_count"),
        "db_operation_count": len(db_rows),
        "matched_reference_count": matched,
        "missing_in_ga_count": len(missing_ga),
        "missing_in_db_count": len(missing_db),
        "missing_in_ga_references": sorted(missing_ga),
        "missing_in_db_references": missing_db,
        "machine_diff_count": sum(
            1 for row in compare_rows if row["machine_same"] is False
        ),
        "start_diff_nonzero_count": sum(
            1 for row in compare_rows if row["start_diff_seconds"] not in (None, 0)
        ),
        "end_diff_nonzero_count": sum(
            1 for row in compare_rows if row["end_diff_seconds"] not in (None, 0)
        ),
        "duration_diff_nonzero_count": sum(
            1 for row in compare_rows if row["duration_diff_seconds"] not in (None, 0)
        ),
    }
    return summary, compare_rows


def write_json(path, payload):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def write_csv(path, rows):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "reference",
        "job_index",
        "operation_index",
        "ga_operation",
        "db_operation",
        "ga_selected_operation",
        "ga_assigned_machine",
        "db_assigned_machine",
        "machine_same",
        "ga_relative_start_seconds",
        "db_relative_start_seconds",
        "start_diff_seconds",
        "ga_relative_end_seconds",
        "db_relative_end_seconds",
        "end_diff_seconds",
        "ga_duration_seconds",
        "db_duration_seconds",
        "duration_diff_seconds",
        "ga_start",
        "db_start",
        "ga_end",
        "db_end",
    ]
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main():
    database = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DATABASE
    ga_json = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_GA_JSON
    db_export_json = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_DB_EXPORT_JSON
    compare_json = sys.argv[4] if len(sys.argv) > 4 else DEFAULT_COMPARE_JSON
    compare_csv = sys.argv[5] if len(sys.argv) > 5 else DEFAULT_COMPARE_CSV

    ga_payload, anchor, ga_by_ref = load_ga_dump(ga_json)
    db_rows = export_db_rows(database, anchor)
    db_export_payload = {
        "source": "operationplan_db",
        "database": database,
        "plan_current": ga_payload.get("plan_current"),
        "operation_count": len(db_rows),
        "time_mapping": ga_payload.get("time_mapping"),
        "operations": db_rows,
    }
    summary, compare_rows = build_compare_rows(ga_payload, db_rows, ga_by_ref)
    compare_payload = {
        "summary": summary,
        "db_export_file": db_export_json,
        "ga_file": ga_json,
        "rows": compare_rows,
    }

    write_json(db_export_json, db_export_payload)
    write_json(compare_json, compare_payload)
    write_csv(compare_csv, compare_rows)

    print(
        json.dumps(
            {
                "db_export_json": db_export_json,
                "compare_json": compare_json,
                "compare_csv": compare_csv,
                "summary": summary,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Inspect table-driven rescheduling results."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    return list(conn.execute(sql, args))


def print_rows(title: str, items: list[sqlite3.Row]) -> None:
    print(f"\n{title}")
    if not items:
        print("  <empty>")
        return
    columns = items[0].keys()
    print("  " + " | ".join(columns))
    for item in items:
        print("  " + " | ".join(str(item[column]) for column in columns))


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect SQLite runtime planning tables")
    parser.add_argument("--case", default="job_schedule")
    parser.add_argument("--db", default="")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else ROOT / "case_data" / args.case / "planning_runtime.sqlite"
    if not db_path.exists():
        raise SystemExit(f"database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    print(f"db={db_path}")
    print_rows(
        "case_input_tables",
        rows(conn, """
            select 'case_planning_dates' as table_name, count(*) as rows from case_planning_dates
            union all select 'case_demands', count(*) from case_demands
            union all select 'case_resources', count(*) from case_resources
            union all select 'case_operations', count(*) from case_operations
            union all select 'case_operation_resources', count(*) from case_operation_resources
            union all select 'case_operation_materials', count(*) from case_operation_materials
            union all select 'case_inventory', count(*) from case_inventory
            union all select 'case_purchasable_items', count(*) from case_purchasable_items
            union all select 'case_routes', count(*) from case_routes
            union all select 'case_operation_alternatives', count(*) from case_operation_alternatives
            union all select 'case_resource_calendars', count(*) from case_resource_calendars
            union all select 'device_resource_map', count(*) from device_resource_map
        """),
    )
    print_rows(
        "device_state_history",
        rows(conn, "select device_id,status,available,capacity_factor,risk_level,write_seconds from device_state_history order by device_id,write_seconds,id"),
    )
    print_rows(
        "device_state_updates",
        rows(conn, "select id,device_id,status,available,capacity_factor,risk_level,write_seconds,processed from device_state_updates order by id"),
    )
    print_rows(
        "reschedule_events",
        rows(conn, "select id,event_type,device_id,resource_id,start_seconds,end_seconds,handled,reason from reschedule_events order by id"),
    )
    print_rows(
        "schedule_versions",
        rows(conn, "select id,mode,event_id,now_seconds,result_path from schedule_versions order by id"),
    )
    print_rows(
        "task_status_by_version",
        rows(conn, "select version_id,status,count(*) as tasks from schedule_tasks group by version_id,status order by version_id,status"),
    )
    print_rows(
        "diff_status_by_version",
        rows(conn, "select version_id,status,count(*) as tasks from schedule_diffs group by version_id,status order by version_id,status"),
    )
    print("\nstate_boundary")
    print("  confirmed: task end_seconds <= event write_seconds, kept unchanged")
    print("  running: task start_seconds < event write_seconds < end_seconds, kept unchanged until original end")
    print("  planned: task starts after event write_seconds, can be recalculated when affected")
    print("  changed diff rows with old_start=-1 mean the recalculated batch has no exact old batch match")
    print_rows(
        "changed_tasks_sample",
        rows(conn, "select version_id,demand,operation,batch,old_start,new_start,old_end,new_end from schedule_diffs where status='changed' order by version_id,demand,operation,batch limit 10"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

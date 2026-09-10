#!/usr/bin/env python3
"""Periodic table monitor for device-state driven rescheduling.

External collectors can insert rows into device_state_updates. This monitor
polls the table, compares every unprocessed update with device_state_history,
creates rescheduling events only when relevant state fields changed, and writes
new schedule versions back to SQLite.
"""

from __future__ import annotations

import argparse
import sqlite3
import tempfile
import time
from pathlib import Path

from table_reschedule_workflow import (
    ROOT,
    DeviceUpdate,
    connect,
    ensure_initial_schedule,
    ensure_scheduler_built,
    init_tables,
    prepare_runtime_case_dir,
    run_reschedule_from_latest,
    write_device_state,
)


def pending_updates(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            select id, device_id, status, available, capacity_factor, risk_level, write_seconds
            from device_state_updates
            where processed = 0
            order by write_seconds, id
            limit ?
            """,
            (limit,),
        )
    )


def mark_processed(conn: sqlite3.Connection, update_id: int, processed_seconds: float) -> None:
    conn.execute(
        """
        update device_state_updates
        set processed = 1, processed_seconds = ?
        where id = ?
        """,
        (processed_seconds, update_id),
    )
    conn.commit()


def process_once(
    conn: sqlite3.Connection,
    case_dir: Path,
    mode: str,
    limit: int,
    processed_seconds: float,
) -> tuple[int, int, int]:
    updates = pending_updates(conn, limit)
    if not updates:
        return (0, 0, 0)

    processed = 0
    changed = 0
    rescheduled = 0
    with tempfile.TemporaryDirectory(prefix="chaozhenyun_monitor_") as temp_name:
        work_dir = Path(temp_name)
        runtime_dir = prepare_runtime_case_dir(conn, case_dir, work_dir)
        ensure_initial_schedule(conn, case_dir, runtime_dir, work_dir, mode)

        for row in updates:
            update = DeviceUpdate(
                device_id=row["device_id"],
                status=row["status"],
                available=int(row["available"]),
                capacity_factor=float(row["capacity_factor"]),
                risk_level=row["risk_level"] or "",
                write_seconds=float(row["write_seconds"]),
            )
            event_id = write_device_state(conn, update)
            processed += 1
            if event_id is not None:
                changed += 1
                runtime_dir = prepare_runtime_case_dir(conn, case_dir, work_dir)
                run_reschedule_from_latest(conn, case_dir, runtime_dir, work_dir, event_id, mode)
                rescheduled += 1
            mark_processed(conn, int(row["id"]), processed_seconds)
    return (processed, changed, rescheduled)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Poll device_state_updates and trigger table-driven rescheduling")
    parser.add_argument("--case", default="job_schedule")
    parser.add_argument("--db", default="")
    parser.add_argument("--mode", choices=["mrp", "ga"], default="mrp")
    parser.add_argument("--interval-seconds", type=float, default=300.0)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--reset", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    case_dir = ROOT / "case_data" / args.case
    if not case_dir.is_dir():
        raise SystemExit(f"case not found: {case_dir}")
    db_path = Path(args.db) if args.db else case_dir / "planning_runtime.sqlite"
    if args.reset and db_path.exists():
        db_path.unlink()

    ensure_scheduler_built()
    conn = connect(db_path)
    init_tables(conn, case_dir)

    while True:
        processed, changed, rescheduled = process_once(
            conn,
            case_dir,
            args.mode,
            args.limit,
            time.time(),
        )
        print(
            f"db={db_path} processed_updates={processed} "
            f"changed_updates={changed} rescheduled_versions={rescheduled}",
            flush=True,
        )
        if args.once:
            break
        time.sleep(max(1.0, args.interval_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

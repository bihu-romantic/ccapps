#!/usr/bin/env python3
"""Insert a device-state update into the table monitor input queue."""

from __future__ import annotations

import argparse
from pathlib import Path

from table_reschedule_workflow import ROOT, connect, init_tables


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write one device update row into SQLite")
    parser.add_argument("--case", default="job_schedule")
    parser.add_argument("--db", default="")
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--available", type=int, required=True)
    parser.add_argument("--capacity-factor", type=float, required=True)
    parser.add_argument("--risk-level", default="")
    parser.add_argument("--write-seconds", type=float, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    case_dir = ROOT / "case_data" / args.case
    if not case_dir.is_dir():
        raise SystemExit(f"case not found: {case_dir}")
    db_path = Path(args.db) if args.db else case_dir / "planning_runtime.sqlite"
    conn = connect(db_path)
    init_tables(conn, case_dir)
    cursor = conn.execute(
        """
        insert into device_state_updates
          (device_id, status, available, capacity_factor, risk_level, write_seconds)
        values (?, ?, ?, ?, ?, ?)
        """,
        (
            args.device_id,
            args.status,
            args.available,
            args.capacity_factor,
            args.risk_level,
            args.write_seconds,
        ),
    )
    conn.commit()
    print(f"db={db_path}")
    print(f"update_id={cursor.lastrowid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Table-driven device-state update and rescheduling workflow.

This script uses SQLite as a local table store. It keeps the algorithm package
file-based externally, but runs the production flow:

device state write -> detect changed capability -> create reschedule event
-> confirm tasks before write time -> reschedule tasks after write time.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEDULER = ROOT / "cpp" / "build" / "chaozhenyun_scheduler"


@dataclass
class DeviceUpdate:
    device_id: str
    status: str
    available: int
    capacity_factor: float
    risk_level: str
    write_seconds: float


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        create table if not exists device_resource_map (
          device_id text primary key,
          resource_id text not null
        );

        create table if not exists case_planning_dates (
          plan_start text,
          due_datetime text
        );

        create table if not exists case_demands (
          id text primary key,
          item text,
          quantity real,
          due_seconds real,
          priority integer,
          frozen integer
        );

        create table if not exists case_resources (
          id text primary key,
          capacity real
        );

        create table if not exists case_operations (
          id text primary key,
          output_item text,
          duration_seconds real,
          minimum real,
          multiple real,
          maximum real,
          allow_cross_shift text
        );

        create table if not exists case_operation_resources (
          operation text,
          resource text
        );

        create table if not exists case_operation_materials (
          operation text,
          item text,
          quantity real
        );

        create table if not exists case_inventory (
          item text,
          quantity real
        );

        create table if not exists case_purchasable_items (
          item text primary key,
          leadtime_seconds real,
          minimum real,
          multiple real
        );

        create table if not exists case_routes (
          item text,
          sequence integer,
          operation text
        );

        create table if not exists case_operation_alternatives (
          operation text,
          alternative_operation text
        );

        create table if not exists case_resource_calendars (
          resource text,
          start_seconds real,
          end_seconds real,
          weekday text,
          date text
        );

        create table if not exists device_state_history (
          id integer primary key autoincrement,
          device_id text not null,
          status text not null,
          available integer not null,
          capacity_factor real not null,
          risk_level text,
          write_seconds real not null
        );

        create table if not exists device_state_updates (
          id integer primary key autoincrement,
          device_id text not null,
          status text not null,
          available integer not null,
          capacity_factor real not null,
          risk_level text,
          write_seconds real not null,
          processed integer not null default 0,
          processed_seconds real
        );

        create table if not exists reschedule_events (
          id integer primary key autoincrement,
          event_type text not null,
          device_id text,
          resource_id text,
          start_seconds real not null,
          end_seconds real,
          reason text,
          handled integer not null default 0,
          created_seconds real not null
        );

        create table if not exists schedule_versions (
          id integer primary key autoincrement,
          case_name text not null,
          mode text not null,
          result_path text not null,
          event_id integer,
          now_seconds real not null,
          created_seconds real not null
        );

        create table if not exists schedule_tasks (
          version_id integer not null,
          demand text not null,
          operation text not null,
          resources text not null,
          quantity real not null,
          start_seconds real not null,
          end_seconds real not null,
          status text not null
        );

        create table if not exists schedule_problems (
          version_id integer not null,
          type text not null,
          severity text not null,
          demand text,
          operation text,
          object text,
          quantity real,
          risk_seconds real,
          recommended_action text
        );

        create table if not exists schedule_diffs (
          version_id integer not null,
          demand text not null,
          operation text not null,
          batch integer not null,
          old_start real,
          new_start real,
          old_end real,
          new_end real,
          status text not null
        );
        """
    )
    return conn


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def write_csv_rows(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def value(row: dict[str, str], key: str, default: str = "") -> str:
    return row.get(key, default) or default


def as_float(text: str, default: float = 0.0) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def as_int(text: str, default: int = 0) -> int:
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return default


def import_case_inputs(conn: sqlite3.Connection, case_dir: Path) -> None:
    """Import the core job-scheduling CSV inputs into SQLite tables."""

    loaders = [
        ("case_planning_dates", "planning_dates.csv", ["plan_start", "due_datetime"], lambda r: (value(r, "plan_start"), value(r, "due_datetime"))),
        ("case_demands", "demands.csv", ["id", "item", "quantity", "due_seconds", "priority", "frozen"], lambda r: (
            value(r, "id"), value(r, "item"), as_float(value(r, "quantity")), as_float(value(r, "due_seconds")), as_int(value(r, "priority")), as_int(value(r, "frozen")),
        )),
        ("case_resources", "resources.csv", ["id", "capacity"], lambda r: (value(r, "id"), as_float(value(r, "capacity")))),
        ("case_operations", "operations.csv", ["id", "output_item", "duration_seconds", "minimum", "multiple", "maximum", "allow_cross_shift"], lambda r: (
            value(r, "id"), value(r, "output_item"), as_float(value(r, "duration_seconds")), as_float(value(r, "minimum")),
            as_float(value(r, "multiple")), as_float(value(r, "maximum")), value(r, "allow_cross_shift"),
        )),
        ("case_operation_resources", "operation_resources.csv", ["operation", "resource"], lambda r: (value(r, "operation"), value(r, "resource"))),
        ("case_operation_materials", "operation_materials.csv", ["operation", "item", "quantity"], lambda r: (
            value(r, "operation"), value(r, "item"), as_float(value(r, "quantity")),
        )),
        ("case_inventory", "inventory.csv", ["item", "quantity"], lambda r: (value(r, "item"), as_float(value(r, "quantity")))),
        ("case_purchasable_items", "purchasable_items.csv", ["item", "leadtime_seconds", "minimum", "multiple"], lambda r: (
            value(r, "item"), as_float(value(r, "leadtime_seconds")), as_float(value(r, "minimum")), as_float(value(r, "multiple")),
        )),
        ("case_routes", "routes.csv", ["item", "sequence", "operation"], lambda r: (value(r, "item"), as_int(value(r, "sequence")), value(r, "operation"))),
        ("case_operation_alternatives", "operation_alternatives.csv", ["operation", "alternative_operation"], lambda r: (
            value(r, "operation"), value(r, "alternative_operation"),
        )),
        ("case_resource_calendars", "resource_calendars.csv", ["resource", "start_seconds", "end_seconds", "weekday", "date"], lambda r: (
            value(r, "resource"), as_float(value(r, "start_seconds")), as_float(value(r, "end_seconds")), value(r, "weekday"), value(r, "date"),
        )),
    ]
    for table, filename, columns, mapper in loaders:
        path = case_dir / filename
        conn.execute(f"delete from {table}")
        if not path.exists():
            continue
        placeholders = ",".join("?" for _ in columns)
        column_sql = ",".join(columns)
        for row in read_csv_rows(path):
            conn.execute(f"insert into {table}({column_sql}) values({placeholders})", mapper(row))
    conn.commit()


def init_tables(conn: sqlite3.Connection, case_dir: Path) -> None:
    import_case_inputs(conn, case_dir)

    map_file = case_dir / "device_resource_map.csv"
    if map_file.exists():
        for row in read_csv_rows(map_file):
            conn.execute(
                "insert or replace into device_resource_map(device_id, resource_id) values(?, ?)",
                (row["device_id"], row["resource_id"]),
            )

    state_file = case_dir / "device_states.csv"
    if state_file.exists():
        for row in read_csv_rows(state_file):
            device_id = row["device_id"]
            exists = conn.execute(
                "select 1 from device_state_history where device_id = ? limit 1",
                (device_id,),
            ).fetchone()
            if exists:
                continue
            conn.execute(
                """
                insert into device_state_history
                  (device_id, status, available, capacity_factor, risk_level, write_seconds)
                values (?, ?, ?, ?, ?, 0)
                """,
                (
                    device_id,
                    row.get("status") or row.get("health_grade") or "unknown",
                    int(str(row.get("available", "1")).lower() not in {"0", "false"}),
                    float(row.get("capacity_factor") or 1.0),
                    row.get("risk_level", ""),
                ),
            )
    conn.commit()


def export_table_csv(
    conn: sqlite3.Connection,
    table: str,
    filename: str,
    columns: list[str],
    runtime_dir: Path,
    order_by: str = "",
) -> None:
    sql = f"select {','.join(columns)} from {table}"
    if order_by:
        sql += f" order by {order_by}"
    rows = conn.execute(sql).fetchall()
    output_rows = [dict(row) for row in rows]
    if table == "case_demands":
        for row in output_rows:
            if not row.get("due_seconds"):
                row["due_seconds"] = ""
    write_csv_rows(runtime_dir / filename, columns, output_rows)


def export_runtime_case_inputs(conn: sqlite3.Connection, runtime_dir: Path) -> None:
    """Export scheduler input files from SQLite source tables.

    The table store is the workflow source of truth. These files are short-lived
    adapter files for the current C++ scheduling kernel and are removed with the
    temporary runtime directory.
    """

    exports = [
        ("case_planning_dates", "planning_dates.csv", ["plan_start", "due_datetime"], "plan_start"),
        ("case_demands", "demands.csv", ["id", "item", "quantity", "due_seconds", "priority", "frozen"], "priority desc, id"),
        ("case_resources", "resources.csv", ["id", "capacity"], "id"),
        ("case_operations", "operations.csv", ["id", "output_item", "duration_seconds", "minimum", "multiple", "maximum", "allow_cross_shift"], "id"),
        ("case_operation_resources", "operation_resources.csv", ["operation", "resource"], "operation, resource"),
        ("case_operation_materials", "operation_materials.csv", ["operation", "item", "quantity"], "operation, item"),
        ("case_inventory", "inventory.csv", ["item", "quantity"], "item"),
        ("case_purchasable_items", "purchasable_items.csv", ["item", "leadtime_seconds", "minimum", "multiple"], "item"),
        ("case_routes", "routes.csv", ["item", "sequence", "operation"], "item, sequence, operation"),
        ("case_operation_alternatives", "operation_alternatives.csv", ["operation", "alternative_operation"], "operation, alternative_operation"),
        ("case_resource_calendars", "resource_calendars.csv", ["resource", "start_seconds", "end_seconds", "weekday", "date"], "resource, start_seconds"),
    ]
    for table, filename, columns, order_by in exports:
        export_table_csv(conn, table, filename, columns, runtime_dir, order_by)


def latest_state(conn: sqlite3.Connection, device_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        select * from device_state_history
        where device_id = ?
        order by write_seconds desc, id desc
        limit 1
        """,
        (device_id,),
    ).fetchone()


def has_state_change(previous: sqlite3.Row | None, update: DeviceUpdate) -> bool:
    if previous is None:
        return True
    return (
        previous["status"] != update.status
        or int(previous["available"]) != update.available
        or abs(float(previous["capacity_factor"]) - update.capacity_factor) > 1e-9
        or (previous["risk_level"] or "") != update.risk_level
    )


def write_device_state(conn: sqlite3.Connection, update: DeviceUpdate) -> int | None:
    previous = latest_state(conn, update.device_id)
    changed = has_state_change(previous, update)
    conn.execute(
        """
        insert into device_state_history
          (device_id, status, available, capacity_factor, risk_level, write_seconds)
        values (?, ?, ?, ?, ?, ?)
        """,
        (
            update.device_id,
            update.status,
            update.available,
            update.capacity_factor,
            update.risk_level,
            update.write_seconds,
        ),
    )
    if not changed:
        conn.commit()
        return None

    mapped = conn.execute(
        "select resource_id from device_resource_map where device_id = ?",
        (update.device_id,),
    ).fetchone()
    resource_id = mapped["resource_id"] if mapped else update.device_id
    reason = f"device_state_changed:{update.status}:available={update.available}:capacity_factor={update.capacity_factor}"
    cursor = conn.execute(
        """
        insert into reschedule_events
          (event_type, device_id, resource_id, start_seconds, end_seconds, reason, created_seconds)
        values (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "equipment_unavailable" if not update.available else "equipment_capability_changed",
            update.device_id,
            resource_id,
            update.write_seconds,
            update.write_seconds if update.available else update.write_seconds + 86400,
            reason,
            update.write_seconds,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def export_runtime_device_files(conn: sqlite3.Connection, runtime_dir: Path) -> None:
    map_rows = conn.execute(
        "select device_id, resource_id from device_resource_map order by device_id"
    ).fetchall()
    write_csv_rows(
        runtime_dir / "device_resource_map.csv",
        ["device_id", "resource_id"],
        [dict(row) for row in map_rows],
    )
    rows = conn.execute(
        """
        select h.device_id, h.status, h.available, h.capacity_factor, h.risk_level
        from device_state_history h
        join (
          select device_id, max(write_seconds) as max_write
          from device_state_history
          group by device_id
        ) latest on latest.device_id = h.device_id and latest.max_write = h.write_seconds
        order by h.device_id
        """
    ).fetchall()
    write_csv_rows(
        runtime_dir / "device_states.csv",
        ["device_id", "status", "available", "capacity_factor", "risk_level"],
        [dict(row) for row in rows],
    )


def prepare_runtime_case_dir(conn: sqlite3.Connection, case_dir: Path, work_dir: Path) -> Path:
    runtime_dir = work_dir / "case_runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    export_runtime_case_inputs(conn, runtime_dir)
    export_runtime_device_files(conn, runtime_dir)
    return runtime_dir


def export_reschedule_event(conn: sqlite3.Connection, event_id: int, path: Path) -> sqlite3.Row:
    event = conn.execute("select * from reschedule_events where id = ?", (event_id,)).fetchone()
    if event is None:
        raise RuntimeError(f"event not found: {event_id}")
    write_csv_rows(
        path,
        ["event_type", "demand", "resource", "start_seconds", "end_seconds"],
        [{
            "event_type": event["event_type"],
            "demand": "",
            "resource": event["resource_id"],
            "start_seconds": event["start_seconds"],
            "end_seconds": event["end_seconds"] or "",
        }],
    )
    return event


def confirm_previous_schedule(previous: Path, confirmed: Path, now_seconds: float) -> None:
    rows = read_csv_rows(previous)
    for row in rows:
        start = float(row["start_seconds"])
        end = float(row["end_seconds"])
        if end <= now_seconds:
            row["status"] = "confirmed"
        elif start < now_seconds < end:
            row["status"] = "running"
        else:
            row["status"] = row.get("status") or "planned"
    write_csv_rows(confirmed, ["demand", "operation", "resources", "quantity", "start_seconds", "end_seconds", "status"], rows)


def ensure_scheduler_built() -> None:
    subprocess.run(["cmake", "-S", str(ROOT / "cpp"), "-B", str(ROOT / "cpp" / "build")], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["cmake", "--build", str(ROOT / "cpp" / "build")], check=True, stdout=subprocess.DEVNULL)


def run_initial_schedule(runtime_dir: Path, work_dir: Path, mode: str) -> Path:
    output = work_dir / f"initial_{mode}.csv"
    scheduler_mode = "ga_device" if mode == "ga" else "mrp_device"
    subprocess.run([str(SCHEDULER), str(runtime_dir), str(output), scheduler_mode], check=True)
    return output


def export_schedule_tasks(conn: sqlite3.Connection, version_id: int, path: Path) -> None:
    rows = conn.execute(
        """
        select demand, operation, resources, quantity, start_seconds, end_seconds, status
        from schedule_tasks
        where version_id = ?
        order by start_seconds, end_seconds, demand, operation
        """,
        (version_id,),
    ).fetchall()
    write_csv_rows(
        path,
        ["demand", "operation", "resources", "quantity", "start_seconds", "end_seconds", "status"],
        [dict(row) for row in rows],
    )


def latest_schedule_version(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        """
        select * from schedule_versions
        order by id desc
        limit 1
        """
    ).fetchone()


def ensure_initial_schedule(
    conn: sqlite3.Connection,
    case_dir: Path,
    runtime_dir: Path,
    work_dir: Path,
    mode: str,
) -> int:
    latest = latest_schedule_version(conn)
    if latest is not None:
        return int(latest["id"])
    previous = run_initial_schedule(runtime_dir, work_dir, mode)
    version_id = insert_schedule_version(conn, case_dir.name, f"{mode}_device_initial", "sqlite:schedule_tasks", None, 0)
    import_schedule_outputs(conn, version_id, previous)
    conn.commit()
    return version_id


def insert_schedule_version(
    conn: sqlite3.Connection,
    case_name: str,
    mode: str,
    result_ref: str,
    event_id: int | None,
    now_seconds: float,
) -> int:
    cursor = conn.execute(
        """
        insert into schedule_versions
          (case_name, mode, result_path, event_id, now_seconds, created_seconds)
        values (?, ?, ?, ?, ?, ?)
        """,
        (case_name, mode, result_ref, event_id, now_seconds, now_seconds),
    )
    return int(cursor.lastrowid)


def import_schedule_outputs(conn: sqlite3.Connection, version_id: int, result_path: Path) -> None:
    conn.execute("delete from schedule_tasks where version_id = ?", (version_id,))
    for row in read_csv_rows(result_path):
        conn.execute(
            """
            insert into schedule_tasks
              (version_id, demand, operation, resources, quantity, start_seconds, end_seconds, status)
            values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                row["demand"],
                row["operation"],
                row["resources"],
                float(row["quantity"]),
                float(row["start_seconds"]),
                float(row["end_seconds"]),
                row["status"],
            ),
        )

    problems_path = Path(str(result_path) + ".problems.csv")
    if problems_path.exists():
        conn.execute("delete from schedule_problems where version_id = ?", (version_id,))
        for row in read_csv_rows(problems_path):
            if not row.get("type"):
                continue
            conn.execute(
                """
                insert into schedule_problems
                  (version_id, type, severity, demand, operation, object, quantity, risk_seconds, recommended_action)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    row["type"],
                    row["severity"],
                    row.get("demand", ""),
                    row.get("operation", ""),
                    row.get("object", ""),
                    float(row.get("quantity") or 0),
                    float(row.get("risk_seconds") or 0),
                    row.get("recommended_action", ""),
                ),
            )

    diff_path = Path(str(result_path) + ".diff.csv")
    if diff_path.exists():
        conn.execute("delete from schedule_diffs where version_id = ?", (version_id,))
        for row in read_csv_rows(diff_path):
            conn.execute(
                """
                insert into schedule_diffs
                  (version_id, demand, operation, batch, old_start, new_start, old_end, new_end, status)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    row["demand"],
                    row["operation"],
                    int(row["batch"]),
                    float(row["old_start"]),
                    float(row["new_start"]),
                    float(row["old_end"]),
                    float(row["new_end"]),
                    row["status"],
                ),
            )


def run_reschedule(conn: sqlite3.Connection, case_dir: Path, runtime_dir: Path, work_dir: Path, previous: Path, event_id: int, mode: str) -> int:
    event_file = work_dir / f"event_{event_id}.csv"
    event = export_reschedule_event(conn, event_id, event_file)
    now_seconds = float(event["created_seconds"])
    confirmed = work_dir / f"confirmed_before_{event_id}.csv"
    confirm_previous_schedule(previous, confirmed, now_seconds)
    output = work_dir / f"reschedule_{event_id}_{mode}.csv"
    subprocess.run(
        [str(SCHEDULER), str(runtime_dir), str(output), "reschedule_device", str(confirmed), str(event_file), str(now_seconds)],
        check=True,
    )
    version_id = insert_schedule_version(conn, case_dir.name, f"{mode}_reschedule_device", "sqlite:schedule_tasks", event_id, now_seconds)
    import_schedule_outputs(conn, version_id, output)
    conn.execute("update reschedule_events set handled = 1 where id = ?", (event_id,))
    conn.commit()
    return version_id


def run_reschedule_from_latest(
    conn: sqlite3.Connection,
    case_dir: Path,
    runtime_dir: Path,
    work_dir: Path,
    event_id: int,
    mode: str,
) -> int:
    latest = latest_schedule_version(conn)
    if latest is None:
        raise RuntimeError("no previous schedule version found")
    previous = work_dir / f"previous_version_{latest['id']}.csv"
    export_schedule_tasks(conn, int(latest["id"]), previous)
    return run_reschedule(conn, case_dir, runtime_dir, work_dir, previous, event_id, mode)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SQLite table-driven device rescheduling workflow")
    parser.add_argument("--case", default="job_schedule")
    parser.add_argument("--db", default="")
    parser.add_argument("--mode", choices=["mrp", "ga"], default="mrp")
    parser.add_argument("--device-id", default="FAN-01")
    parser.add_argument("--status", default="fault")
    parser.add_argument("--available", type=int, default=0)
    parser.add_argument("--capacity-factor", type=float, default=0.0)
    parser.add_argument("--risk-level", default="high")
    parser.add_argument("--write-seconds", type=float, default=28800.0)
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

    with tempfile.TemporaryDirectory(prefix="chaozhenyun_schedule_") as temp_name:
        work_dir = Path(temp_name)
        runtime_dir = prepare_runtime_case_dir(conn, case_dir, work_dir)
        initial_version_id = ensure_initial_schedule(conn, case_dir, runtime_dir, work_dir, args.mode)
        update = DeviceUpdate(
            device_id=args.device_id,
            status=args.status,
            available=args.available,
            capacity_factor=args.capacity_factor,
            risk_level=args.risk_level,
            write_seconds=args.write_seconds,
        )
        event_id = write_device_state(conn, update)
        export_runtime_device_files(conn, runtime_dir)

        print(f"db={db_path}")
        print(f"initial_version_id={initial_version_id}")
        if event_id is None:
            print("change_detected=0")
            return 0

        reschedule_version_id = run_reschedule_from_latest(conn, case_dir, runtime_dir, work_dir, event_id, args.mode)
        print(f"change_detected=1 event_id={event_id} write_seconds={args.write_seconds}")
        print(f"reschedule_version_id={reschedule_version_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

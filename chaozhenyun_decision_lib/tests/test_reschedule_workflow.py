from pathlib import Path

from chaozhenyun_decision_lib.tools.schedule_monitor import claim_pending_updates
from chaozhenyun_decision_lib.tools.table_reschedule_workflow import (
    DeviceUpdate,
    connect,
    write_device_state,
)


def test_late_and_duplicate_device_updates_are_ignored(tmp_path: Path):
    conn = connect(tmp_path / "runtime.sqlite")
    current = DeviceUpdate("D1", "fault", 0, 0.0, "high", 200.0)
    assert write_device_state(conn, current) is not None

    late = DeviceUpdate("D1", "healthy", 1, 1.0, "low", 100.0)
    duplicate = DeviceUpdate("D1", "healthy", 1, 1.0, "low", 200.0)
    assert write_device_state(conn, late) is None
    assert write_device_state(conn, duplicate) is None
    assert conn.execute(
        "select count(*) from device_state_history where device_id='D1'"
    ).fetchone()[0] == 1
    assert conn.execute("select count(*) from reschedule_events").fetchone()[0] == 1


def test_queue_rows_are_claimed_once_and_stale_claims_are_recovered(tmp_path: Path):
    db_path = tmp_path / "runtime.sqlite"
    first = connect(db_path)
    second = connect(db_path)
    first.execute(
        """
        insert into device_state_updates
          (device_id,status,available,capacity_factor,risk_level,write_seconds)
        values ('D1','fault',0,0,'high',10)
        """
    )
    first.commit()

    token, rows = claim_pending_updates(first, 10, 1000.0)
    assert token
    assert [row["device_id"] for row in rows] == ["D1"]
    assert claim_pending_updates(second, 10, 1000.0)[1] == []

    recovered_token, recovered = claim_pending_updates(second, 10, 4601.0)
    assert recovered_token != token
    assert [row["device_id"] for row in recovered] == ["D1"]

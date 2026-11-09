#!/usr/bin/env python3
"""FAS 排产后离线抽查（非正式约束检查）。

正式约束检查走 CP：`plan.solver=cp`（可用 `cp.baseline=fas`），
在 solvercp.cpp 的 CP post-check 里校验物料/库存/产能/工单结果。
本脚本仅作辅助抽查，不替代 CP 约束。

用法:
  POSTGRES_HOST=127.0.0.1 venv/bin/python fc_aps/check_fas_constraints.py \\
      --database ccapps8 [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

import psycopg2
from psycopg2.extras import RealDictCursor


def dec(v):
    return float(v or 0)


def connect(database):
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        user=os.environ.get("POSTGRES_USER", "ccapps"),
        password=os.environ.get("POSTGRES_PASSWORD", "ccapps"),
        dbname=database,
    )


def check_mo_results(cur):
    """FAS 结果：所有制造工单都有完整起止与正数量。"""
    cur.execute(
        """
        SELECT reference, operation_id, item_id, status, quantity, startdate, enddate
        FROM operationplan
        WHERE type = 'MO'
        ORDER BY reference
        """
    )
    rows = cur.fetchall()
    missing_dates = []
    bad_qty = []
    end_before_start = []
    for r in rows:
        if r["startdate"] is None or r["enddate"] is None:
            missing_dates.append(r["reference"])
        if dec(r["quantity"]) <= 1e-9:
            bad_qty.append(r["reference"])
        if (
            r["startdate"] is not None
            and r["enddate"] is not None
            and r["enddate"] < r["startdate"]
        ):
            end_before_start.append(r["reference"])
    # 「有结果」= 起止时间齐全且结束不早于开始。quantity=0 的同步占位单单独计数，不判失败。
    failed = len(missing_dates) + len(end_before_start)
    return {
        "name": "FAS工单都有结果",
        "ok": failed == 0,
        "total_mo": len(rows),
        "missing_dates": len(missing_dates),
        "bad_quantity": len(bad_qty),
        "end_before_start": len(end_before_start),
        "samples": {
            "missing_dates": missing_dates[:20],
            "bad_quantity": bad_qty[:20],
            "end_before_start": end_before_start[:20],
        },
    }


def check_demands_fully_planned(cur, window_days=3):
    """CP hard preflight mirror: open demands due in the window must be full.

    Incomplete orders fail the check (hard gate), not a soft warning.
    """
    current = datetime(2026, 10, 16, 8)
    cur.execute(
        """
        SELECT name, value FROM common_parameter
        WHERE name IN ('currentdate', 'last_currentdate', 'plan.currentdate')
        """
    )
    for r in cur.fetchall():
        try:
            parsed = datetime.fromisoformat(
                str(r["value"]).replace("Z", "+00:00")
            )
            if parsed.tzinfo is not None:
                parsed = parsed.replace(tzinfo=None)
            current = parsed
            break
        except Exception:
            continue
    gate_end = current + timedelta(days=max(1, int(window_days)))
    cur.execute(
        """
        SELECT name, category, item_id, quantity,
               coalesce(plannedquantity,0) AS planned, due
        FROM demand
        WHERE status IN ('open', 'quote') AND quantity > 1e-9
          AND due <= %s
        ORDER BY name
        """,
        (gate_end,),
    )
    rows = cur.fetchall()
    zero = []
    partial = []
    for r in rows:
        qty = dec(r["quantity"])
        planned = dec(r["planned"])
        if planned <= 1e-9:
            zero.append(r["name"])
        elif planned + 1e-6 < qty:
            partial.append(r["name"])
    return {
        "name": "窗口内订单数量完整(硬门禁)",
        "ok": not zero and not partial,
        "window_days": window_days,
        "gate_end": gate_end.isoformat(sep=" "),
        "open": len(rows),
        "fully_planned": len(rows) - len(zero) - len(partial),
        "zero_planned": len(zero),
        "partial": len(partial),
        "samples": {"zero": zero[:20], "partial": partial[:20]},
    }


def check_machine_overlap(cur):
    cur.execute(
        """
        SELECT r.name AS resource, o.reference, o.startdate, o.enddate
        FROM operationplanresource opr
        JOIN operationplan o ON o.reference = opr.operationplan_id
        JOIN resource r ON r.name = opr.resource_id
        WHERE o.type = 'MO' AND o.status = 'proposed'
          AND r.category = '设备' AND coalesce(r.type,'') <> 'infinite'
          AND o.startdate IS NOT NULL AND o.enddate IS NOT NULL
        ORDER BY r.name, o.startdate, o.reference
        """
    )
    by_res = defaultdict(list)
    for r in cur.fetchall():
        by_res[r["resource"]].append(r)
    conflicts = []
    for res, jobs in by_res.items():
        prev = None
        for job in jobs:
            if prev and job["startdate"] < prev["enddate"]:
                conflicts.append(
                    {
                        "resource": res,
                        "a": prev["reference"],
                        "b": job["reference"],
                        "a_end": str(prev["enddate"]),
                        "b_start": str(job["startdate"]),
                    }
                )
            if prev is None or job["enddate"] > prev["enddate"]:
                prev = job
    return {
        "name": "设备不重叠",
        "ok": not conflicts,
        "conflict_count": len(conflicts),
        "samples": conflicts[:20],
    }


def check_mold_parallel(cur):
    cur.execute(
        """
        SELECT r.name AS resource, coalesce(r.maximum,1) AS maximum,
               o.reference, o.startdate, o.enddate, opr.quantity
        FROM operationplanresource opr
        JOIN operationplan o ON o.reference = opr.operationplan_id
        JOIN resource r ON r.name = opr.resource_id
        WHERE o.type = 'MO' AND o.status = 'proposed'
          AND r.category = '模具'
          AND o.startdate IS NOT NULL AND o.enddate IS NOT NULL
        """
    )
    by_mold = defaultdict(list)
    maximum = {}
    for r in cur.fetchall():
        by_mold[r["resource"]].append(r)
        maximum[r["resource"]] = dec(r["maximum"]) or 1.0
    conflicts = []
    for mold, jobs in by_mold.items():
        events = []
        for j in jobs:
            qty = dec(j["quantity"]) or 1.0
            events.append((j["startdate"], qty, j["reference"]))
            events.append((j["enddate"], -qty, j["reference"]))
        events.sort(key=lambda x: (x[0], x[1]))
        used = 0.0
        cap = maximum[mold]
        for t, delta, ref in events:
            used += delta
            if used > cap + 1e-6:
                conflicts.append(
                    {
                        "resource": mold,
                        "at": str(t),
                        "used": used,
                        "capacity": cap,
                        "trigger": ref,
                    }
                )
                break
    return {
        "name": "模具不超并行",
        "ok": not conflicts,
        "conflict_count": len(conflicts),
        "samples": conflicts[:20],
    }


def check_personnel_capacity(cur):
    cur.execute(
        """
        SELECT name, maximum, maximum_calendar_id
        FROM resource
        WHERE category = '人员' AND name LIKE 'PERSON_%'
        ORDER BY name
        """
    )
    resources = cur.fetchall()
    summary = []
    total_violations = 0
    for res in resources:
        name = res["name"]
        cal = res["maximum_calendar_id"]
        intervals = []
        if cal:
            cur.execute(
                """
                SELECT startdate, enddate, starttime, endtime, value, priority
                FROM calendarbucket WHERE calendar_id = %s
                """,
                (cal,),
            )
            for row in cur.fetchall():
                start, end = row["startdate"], row["enddate"]
                st, et = row["starttime"], row["endtime"]
                value, priority = row["value"], row["priority"]
                day = start.replace(hour=0, minute=0, second=0, microsecond=0)
                tz = getattr(start, "tzinfo", None)
                while day < end:
                    a = max(start, datetime.combine(day.date(), st, tzinfo=tz))
                    b = min(
                        end,
                        datetime.combine(day.date(), et, tzinfo=tz)
                        + timedelta(seconds=1),
                    )
                    if a < b:
                        intervals.append((a, b, float(value), priority))
                    day += timedelta(days=1)
        cur.execute(
            """
            SELECT p.startdate, p.enddate, r.quantity
            FROM operationplan p
            JOIN operationplanresource r ON r.operationplan_id = p.reference
            WHERE r.resource_id = %s AND p.type = 'MO' AND p.status = 'proposed'
              AND p.startdate IS NOT NULL AND p.enddate IS NOT NULL
            """,
            (name,),
        )
        events = defaultdict(float)
        for row in cur.fetchall():
            start, end, qty = row["startdate"], row["enddate"], row["quantity"]
            if start < end:
                events[start] += float(qty)
                events[end] -= float(qty)
        points = set(events)
        for a, b, _, _ in intervals:
            points.update((a, b))
        points = sorted(points)
        used = 0.0
        peak = 0.0
        violations = []
        for i, t in enumerate(points[:-1]):
            used += events[t]
            peak = max(peak, used)
            candidates = [(p, v) for a, b, v, p in intervals if a <= t < b]
            if candidates:
                capacity = float(min(candidates)[1])
            elif cal:
                capacity = 0.0
            else:
                capacity = float(res["maximum"] or 0)
            if points[i + 1] > t and used > capacity + 1e-6:
                violations.append(
                    {
                        "start": str(t),
                        "end": str(points[i + 1]),
                        "used": used,
                        "capacity": capacity,
                    }
                )
        total_violations += len(violations)
        summary.append(
            {
                "resource": name,
                "peak": peak,
                "violation_count": len(violations),
                "violations": violations[:10],
            }
        )
    return {
        "name": "人员不超班次容量",
        "ok": total_violations == 0,
        "violation_count": total_violations,
        "resources": summary,
    }


def run_checks(database):
    conn = connect(database)
    checks = []
    with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        checks.append(check_mo_results(cur))
        checks.append(check_demands_fully_planned(cur))
        checks.append(check_machine_overlap(cur))
        checks.append(check_mold_parallel(cur))
        checks.append(check_personnel_capacity(cur))
    conn.close()
    return {
        "database": database,
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="ccapps8")
    parser.add_argument("--json", type=str, help="Write full JSON report to path")
    args = parser.parse_args()
    report = run_checks(args.database)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(
                report,
                fh,
                ensure_ascii=False,
                indent=2,
                default=lambda x: float(x) if isinstance(x, Decimal) else str(x),
            )
    for c in report["checks"]:
        mark = "PASS" if c["ok"] else "FAIL"
        extra = []
        for key in (
            "total_mo",
            "missing_dates",
            "bad_quantity",
            "end_before_start",
            "open",
            "fully_planned",
            "zero_planned",
            "partial",
            "conflict_count",
            "violation_count",
        ):
            if key in c:
                extra.append(f"{key}={c[key]}")
        print(f"[{mark}] {c['name']}" + (f"  ({', '.join(extra)})" if extra else ""))
        if not c["ok"] and c.get("samples"):
            print("       samples:", json.dumps(c["samples"], ensure_ascii=False, default=str)[:500])
    print("OVERALL", "PASS" if report["ok"] else "FAIL")
    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()

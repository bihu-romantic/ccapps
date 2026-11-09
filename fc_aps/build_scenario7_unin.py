#!/usr/bin/env python3
"""Unfinished + alarm scenario rules (stage B after convert_fc_aps).

Prefer the generic entry ``build_fc_dataset.py``. This module keeps the
scenario logic; Demand.category is the rule switch:

* 未入库 equipment has three tiers:
  1) Concrete machines already filled on the source row stay as-is.
     Notes such as 「装配」「焊接」are not equipment.
  2) Otherwise a concrete machine from 物料机台对照表 is used; rated and
     spare alternatives become alternate choices.
  3) Otherwise an assembly-line pool is bound (from the map, the row, or
     the source workshop). Assembly lines are infinite and unconstrained.
  Finished-goods inventory is not consumed, and the self-made BOM is not
  exploded. A 1+1 mold produces the left and right items on one operation;
  a 2+2 mold produces every part in the group on one operation.
* 预警: deduct finished-goods inventory by organization first. Only the
  remaining quantity is produced, through the BOM and resource routes.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

from openpyxl import load_workbook
import convert_fc_aps as converter

from convert_fc_aps import (
    ASSEMBLY_LINES,
    add_bucket,
    align_sync_coproduct_demands,
    build,
    num,
    op_safe,
    op_suffix,
    parse_day,
    text,
)


ROOT = Path(__file__).parent
# ``/`` and commas are alternatives; ``&`` stays one inseparable group.
ALTERNATIVE_SPLIT = re.compile(r"[,，/]+")


def worksheet_rows(name):
    ws = load_workbook(ROOT / name, read_only=True, data_only=True).active
    return list(ws.iter_rows(values_only=True))[1:]


def iso(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if value in (None, ""):
        return None
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value).strip(), fmt).isoformat()
        except ValueError:
            pass
    return None


def sync_lifecycle_limits_after_alarm_netting(policy, demands):
    """Align exact-item ceilings with open demand after alarm/unin netting.

    Netting only rewrites demand rows. Without this sync:
    * unfinished-only ceilings stay at 未入库 (e.g. 11) and block alarm remainders;
    * alarm-only spare/breakpoint items keep limit 0 from zero unfinished net.

    Also drop limit=0 rows that have no open independent demand: those freeze
    dependent BOM components that upper-level orders still need to make.
    Coproduct members intentionally have no hard limit entry and stay untouched.
    """
    exact_items = {
        item
        for item, rule in (policy.get("items") or {}).items()
        if rule.get("exact")
    }
    open_qty = defaultdict(float)
    for demand in demands:
        item = demand.get("item")
        if (
            item not in exact_items
            or demand.get("status") != "open"
        ):
            continue
        open_qty[item] += max(num(demand.get("quantity"), 0), 0)
    netted_items = {
        demand.get("item")
        for demand in demands
        if demand.get("item") in exact_items
        and "已扣除同物料未入库需求" in (demand.get("description") or "")
    }
    limits = policy.setdefault("limits", {})
    # Netted items may need a new/raised ceiling; existing limit rows (including
    # limit=0 from unfinished net) must cover current open demand.
    candidates = set(netted_items) | {
        item for item in limits if item in exact_items
    }
    updated = {}
    for item in sorted(candidates):
        net_requirement = open_qty.get(item, 0)
        if net_requirement <= 1e-9:
            continue
        previous = num(limits.get(item), 0)
        # After unin/alarm netting the open total is authoritative; convert may
        # have summed both before deduction (e.g. 11+23 → 34 instead of 23).
        if item in netted_items:
            if abs(previous - net_requirement) > 1e-9:
                limits[item] = net_requirement
                updated[item] = net_requirement
        elif net_requirement > previous + 1e-9:
            limits[item] = net_requirement
            updated[item] = net_requirement
    # Remove zero ceilings with no open demand — dependent supply must remain
    # free for parents that still need the component.
    for item in list(limits):
        if item not in exact_items:
            continue
        if num(limits.get(item), 0) > 1e-9:
            continue
        if open_qty.get(item, 0) > 1e-9:
            continue
        del limits[item]
        updated[item] = None
    return updated


def net_alarm_demands_against_unfinished(demands, exact_items):
    """Remove unfinished-order quantities duplicated by exact alarm demand."""
    remaining = defaultdict(float)
    for demand in demands:
        if (
            demand.get("status") == "open"
            and demand.get("item") in exact_items
            and demand.get("name", "").startswith("FC_UNIN_")
        ):
            remaining[demand["item"]] += max(num(demand.get("quantity"), 0), 0)

    changed = 0
    deducted = 0.0
    alarms = sorted(
        (
            demand
            for demand in demands
            if demand.get("status") == "open"
            and demand.get("item") in exact_items
            and demand.get("name", "").startswith("FC_ALARM_")
        ),
        key=lambda demand: (
            demand.get("item", ""),
            demand.get("due") or "9999-12-31T23:59:59",
            demand.get("priority", 0),
            demand.get("name", ""),
        ),
    )
    for demand in alarms:
        item = demand["item"]
        overlap = min(remaining[item], max(num(demand.get("quantity"), 0), 0))
        if overlap <= 1e-9:
            continue
        remaining[item] -= overlap
        demand["quantity"] = max(num(demand.get("quantity"), 0) - overlap, 0)
        demand["description"] = (
            (demand.get("description") or "")
            + " 已扣除同物料未入库需求%s" % ("%g" % overlap)
        ).strip()
        if demand["quantity"] <= 1e-9:
            demand["quantity"] = 0
            demand["status"] = "closed"
        changed += 1
        deducted += overlap
    return changed, deducted


def build_unin_alarm_dataset(
    source_dir: Path | None = None,
    *,
    output: Path | None = None,
    planning_start: datetime | None = None,
    planning_end: datetime | None = None,
    exclude_stock_demands: bool = False,
) -> dict:
    """Stage B: scenario rules on top of convert_fc_aps.build().

    * Net unfinished / alarm / stock demands
    * Bind unfinished-order routes (machine tiers 1/2/3)
    * Co-mold grouping, calendars through planning_end
    * Write JSON when ``output`` is set; always return the document
    """
    global ROOT
    source_dir = (source_dir or ROOT).resolve()
    ROOT = source_dir
    converter.ROOT = ROOT
    if planning_end is None:
        planning_end = datetime(2027, 5, 1)
    if planning_start is None:
        planning_start = datetime.combine(
            min(parse_day(row[0]) for row in worksheet_rows("人数.xlsx") if text(row[1])),
            datetime.min.time().replace(hour=8),
        )
    if planning_end <= planning_start:
        raise ValueError("planning_end must be after planning_start")
    common_due = planning_end
    document = build(include_alarm_demands=True, planning_start=planning_start)
    document.pop("_meta", None)
    data = document["data"]
    # Do not wipe buffer.minimum for unrelated reasons. Sync/1+1 alignment
    # clears sync-primary minima and keeps one driver demand of max(member net).
    # Keep 预警 / 未入库 / 备库. 备库 stays lowest priority and is only used
    # to soak idle capacity after the first two classes are placed.
    data["demands"] = [
        d
        for d in data["demands"]
        if d["name"].startswith(("FC_UNIN_", "FC_ALARM_", "FC_STOCK_"))
    ]
    priority_window_end = planning_start + timedelta(days=3)
    # 人数.xlsx ends after three days. Repeat its last day's shift pattern
    # through the common due date, without changing the source workbook or
    # the calendars used by any other scenario.
    shift_rows = worksheet_rows("人数.xlsx")
    last_shift_day = max(parse_day(row[0]) for row in shift_rows if text(row[1]))
    daily_pattern = [row for row in shift_rows if parse_day(row[0]) == last_shift_day]
    day = last_shift_day + timedelta(days=1)
    while day < common_due.date():
        for _source_day, workshop, _shift, start, end, count in daily_pattern:
            workshop = text(workshop)
            count = num(count, 0)
            add_bucket(data["calendarBuckets"], "CAL_PERSON_" + workshop, count, day, start, end)
            if count > 0:
                add_bucket(data["calendarBuckets"], "CAL_SHIFT_" + workshop, 100, day, start, end)
        day += timedelta(days=1)
    for parameter in document["parameters"]:
        if parameter["name"] == "plan.solver":
            parameter["value"] = "fas"
    parameter_by_name = {p["name"]: p for p in document["parameters"]}
    # Schedule through the common due date. Near-term (3-day) dues stay whole
    # first; later orders may be SNP-split to fill leftover early capacity.
    for name, value in (
        ("plan.horizon_end", common_due.isoformat()),
        ("fc_aps.priority_window_end", priority_window_end.isoformat()),
        # Same due/priority: short jobs first so more lines clear on time.
        ("plan.fas_job_order", "spt"),
        ("lazydelay", "3600"),
        ("plan.minimumdelay", "3600"),
        ("plan.resourceiterationmax", "2000"),
        ("plan.lot_split", "0"),
        ("cp.lot_split", "0"),
        ("plan.solve_timeout", "900"),
    ):
        if name in parameter_by_name:
            parameter_by_name[name]["value"] = value
        else:
            document["parameters"].append({"name": name, "value": value})

    resources = {r["name"]: r for r in data["resources"]}
    # The original day-only workshop has a constant positive headcount:
    # availability already closes its zero-capacity night shift. Keep the
    # maximum calendar when staffed shifts have different headcounts, so a
    # later dataset's night shift cannot inherit the peak daytime capacity.
    workshop5_counts = {
        num(row[5]) for row in shift_rows
        if text(row[1]) == "制造部5工段" and num(row[5]) > 0
    }
    workshop5_day_only = all(
        num(row[5]) <= 0 or (
            converter.clock_seconds(row[3]) == 8 * 3600
            and converter.clock_seconds(row[4]) == 20 * 3600
        )
        for row in shift_rows if text(row[1]) == "制造部5工段"
    )
    if len(workshop5_counts) == 1:
        resources["PERSON_制造部5工段"].pop("maximum_calendar", None)
    operations = {r["name"]: r for r in data["operations"]}
    flows_by_operation = {}
    for flow in data["operationMaterials"]:
        flows_by_operation.setdefault(flow["operation"], []).append(flow)
    loads_by_operation = {}
    for load in data["operationResources"]:
        loads_by_operation.setdefault(load["operation"], []).append(load)

    producers = {}
    for operation, flows in flows_by_operation.items():
        if operations.get(operation, {}).get("type") != "time_per":
            continue
        for flow in flows:
            if num(flow.get("quantity")) > 0:
                producers.setdefault(flow["item"], []).append(operation)

    standard = {}
    staffing = {}
    for code, ct, _output, people, _cavities, single_ct, *_ in worksheet_rows("标准工时.xlsx"):
        code = text(code)
        if not code:
            continue
        standard[code] = num(single_ct) if num(single_ct) > 0 else num(ct)
        staffing[code] = max(num(people, 1), 1)
    machine_workshop = {
        text(machine): text(workshop)
        for machine, _model, workshop, *_ in worksheet_rows("机台.xlsx")
        if text(machine)
    }

    # Only unfinished-order demands share the _R##### suffix with 未入库.xlsx.
    # Including 预警/备库 here steals those suffixes and mis-attaches routes.
    demand_by_suffix = {
        d["name"].rsplit("_R", 1)[-1]: d
        for d in data["demands"]
        if d["name"].startswith("FC_UNIN_")
    }
    new_operations = []
    new_flows = []
    new_loads = []

    unin_source_rows = list(enumerate(worksheet_rows("未入库.xlsx"), 2))
    focus_start = planning_start
    focus_end = planning_start + timedelta(days=3)

    def priority_tier(row):
        start = datetime.fromisoformat(iso(row[5])) if iso(row[5]) else None
        end = datetime.fromisoformat(iso(row[6])) if iso(row[6]) else None
        # The source order interval overlaps the rescheduling window.
        if start and end and start < focus_end and end >= focus_start:
            return 0
        return 1

    # Rank: 预警 (highest) > 未入库 > 备库 (idle filler only).
    alarm_demands = [d for d in data["demands"] if d["name"].startswith("FC_ALARM_")]
    open_alarms = sorted(
        (d for d in alarm_demands if d["status"] == "open"),
        key=lambda d: (d.get("due") or "", d["name"]),
    )
    closed_alarms = [d for d in alarm_demands if d["status"] != "open"]
    next_priority = 1
    for demand in open_alarms:
        demand["priority"] = next_priority
        next_priority += 1
        # A date-only 发货日期 means delivery by the end of that day.
        ship_date = datetime.fromisoformat(demand["due"]).date()
        demand["due"] = (
            datetime.combine(ship_date + timedelta(days=1), datetime.min.time())
            - timedelta(seconds=1)
        ).isoformat()
    for demand in closed_alarms:
        demand["priority"] = next_priority
        next_priority += 1

    priority_by_row = {
        rownum: priority
        for priority, (rownum, row) in enumerate(
            sorted(
                unin_source_rows,
                key=lambda entry: (
                    priority_tier(entry[1]),
                    iso(entry[1][6]) or "9999-12-31T23:59:59",
                    iso(entry[1][5]) or "9999-12-31T23:59:59",
                    entry[0],
                ),
            ),
            next_priority,
        )
    }
    if priority_by_row:
        next_priority = max(priority_by_row.values()) + 1
    # 1+1 is one left and one right. 2+2 is every part in the mold group,
    # such as the four blades on FC-QY-HJ01. One shot produces one of each,
    # and the shot count is the larger unfinished quantity.
    group_members = defaultdict(list)
    for code, mold, cavity, group, *_rest in worksheet_rows("物料模具对照表.xlsx"):
        cavity = text(cavity).replace(" ", "")
        if cavity not in {"1+1", "2+2"}:
            continue
        code, mold, group = text(code), text(mold), text(group)
        if code and mold and group:
            group_members[(mold, group, cavity)].append(code)
    members_of = {}
    for (mold, group, cavity), members in group_members.items():
        members = sorted(set(members))
        if cavity == "1+1" and len(members) != 2:
            continue
        if cavity == "2+2" and len(members) < 2:
            continue
        for code in members:
            members_of[code] = ((mold, group, cavity), members, cavity, mold)
    def is_assembly_line(name):
        name = text(name)
        return bool(name) and (
            name in ASSEMBLY_LINES
            or (name.endswith("装配线") and resources.get(name, {}).get("category") == "设备")
        )

    def is_concrete_machine(name):
        name = text(name)
        return (
            bool(name)
            and resources.get(name, {}).get("category") == "设备"
            and not is_assembly_line(name)
        )

    def concrete_group(token):
        members = [part.strip() for part in text(token).split("&") if part.strip()]
        if members and all(is_concrete_machine(part) for part in members):
            return members
        return []

    def parse_concrete_alternatives(raw):
        out = []
        seen = set()
        for token in ALTERNATIVE_SPLIT.split(text(raw)):
            token = token.strip()
            members = concrete_group(token)
            if not members or token in seen:
                continue
            seen.add(token)
            out.append((token, members))
        return out

    def parse_assembly_line(raw):
        for token in ALTERNATIVE_SPLIT.split(text(raw)):
            token = token.strip()
            if is_assembly_line(token):
                return token
        return None

    # code -> rated alternatives, spare alternatives, assembly line name
    machine_map = {}
    for code, rated, spare, _primary, *_rest in worksheet_rows("物料机台对照表.xlsx"):
        code = text(code)
        if not code:
            continue
        machine_map[code] = {
            "rated": parse_concrete_alternatives(rated),
            "spare": parse_concrete_alternatives(spare),
            "assembly": parse_assembly_line(rated) or parse_assembly_line(spare),
        }
    joint_operation_names = set()
    tier1_orders = 0
    tier2_orders = 0
    tier3_orders = 0
    no_machine_orders = 0

    def row_release(row):
        source_start = datetime.fromisoformat(iso(row[5])) if iso(row[5]) else None
        source_end = datetime.fromisoformat(iso(row[6])) if iso(row[6]) else None
        is_focus_order = (
            source_start is not None
            and source_end is not None
            and source_start < focus_end
            and source_end >= focus_start
        )
        # Orders already in progress at the window boundary restart from the
        # boundary. Later orders keep their source planned start.
        return max(source_start, focus_start) if is_focus_order else source_start

    def emit_unin(entries, end_codes, mold=None):
        usable = []
        for rownum, row in entries:
            code = text(row[0])
            demand = demand_by_suffix.get(f"{rownum:05d}")
            if demand is None or num(row[2]) <= 0:
                continue
            usable.append((rownum, row, code, demand))
        if not usable:
            return
        usable.sort(key=lambda entry: (-num(entry[1][2]), entry[0]))
        rownum, row, code, demand = usable[0]
        base_name = next(
            (producers[item][0] for item in [code, *end_codes] if producers.get(item)),
            None,
        )
        if base_name is None:
            return
        base = operations[base_name]
        order_op = f"FC_UNIN_OP_R{rownum:05d}_{op_safe(code)}"
        if len(end_codes) > 1:
            order_op += "_1P1" if len(end_codes) == 2 else "_2P2"
            joint_operation_names.add(order_op)
        releases = [value for value in (row_release(entry[1]) for entry in usable) if value]
        release = max(releases) if releases else None
        release_calendar = f"CAL_UNIN_RELEASE_R{rownum:05d}"
        duration = max((standard.get(item) or 0) for item in end_codes)
        operation = deepcopy(base)
        operation.update(
            {
                "name": order_op,
                "owner": None,
                "item": code,
                "description": (
                    ("未入库2+2按多的一起产 " if len(end_codes) > 2 else "未入库1+1按多的一起产 ")
                    + "+".join(end_codes)
                    if len(end_codes) > 1
                    else f"未入库订单专用 {demand['name']}"
                ),
                "duration": None,
                "duration_per": duration or base.get("duration_per"),
                "effective_start": release.isoformat() if release else None,
                "available": release_calendar if release else None,
                # Directly assigned 未入库 demands still use this route, while
                # generic alarm supply should prefer the regular BOM routes.
                "priority": 100000,
            }
        )
        new_operations.append(operation)
        for entry_rownum, _entry_row, _entry_code, entry_demand in usable:
            entry_demand["operation"] = order_op
            entry_demand["priority"] = priority_by_row[entry_rownum]
            # The source planned end controls ordering only. Every candidate may
            # finish anywhere up to the common due date.
            entry_demand["due"] = common_due.isoformat()
        if release:
            data["calendars"].append(
                {
                    "name": release_calendar,
                    "description": f"未入库订单最早开工 R{rownum:05d}",
                    "defaultvalue": 0,
                    "source": "fc_aps",
                }
            )
            workshop = text(row[7])
            if workshop == "制造部5工段" and workshop5_day_only:
                # A long operation can pause at 20:00 and resume at 08:00 on
                # the next shift while remaining one unsplit manufacturing
                # order. The operation calendar must contain the shift gaps;
                # a continuously-open release calendar makes the resource
                # solver require one uninterrupted elapsed-time slot.
                shift_start = datetime.strptime("08:00:00", "%H:%M:%S").time()
                shift_end = datetime.strptime("20:00:00", "%H:%M:%S").time()
                if release.time() < shift_end:
                    data["calendarBuckets"].append(
                        {
                            "calendar": release_calendar,
                            "value": 100,
                            "startdate": release.date().isoformat(),
                            "enddate": release.date().isoformat(),
                            "starttime": max(release.time(), shift_start).isoformat(),
                            "endtime": shift_end.isoformat(),
                            "priority": 0,
                            "source": "fc_aps",
                        }
                    )
                following_day = release.date() + timedelta(days=1)
                data["calendarBuckets"].append(
                    {
                        "calendar": release_calendar,
                        "value": 100,
                        "startdate": following_day.isoformat(),
                        "enddate": "2035-12-31",
                        "starttime": shift_start.isoformat(),
                        "endtime": shift_end.isoformat(),
                        "priority": 1,
                        "source": "fc_aps",
                    }
                )
            else:
                data["calendarBuckets"].append(
                    {
                        "calendar": release_calendar,
                        "value": 100,
                        "startdate": release.date().isoformat(),
                        "enddate": release.date().isoformat(),
                        "starttime": release.time().isoformat(),
                        "endtime": "23:59:59",
                        "priority": 0,
                        "source": "fc_aps",
                    }
                )
                following_day = release.date() + timedelta(days=1)
                data["calendarBuckets"].append(
                    {
                        "calendar": release_calendar,
                        "value": 100,
                        "startdate": following_day.isoformat(),
                        "enddate": "2035-12-31",
                        "starttime": "00:00:00",
                        "endtime": "23:59:59",
                        "priority": 1,
                        "source": "fc_aps",
                    }
                )

        # Three equipment tiers for 未入库:
        # 1) concrete machines on the source row (junk notes like 装配 ignored)
        # 2) concrete machines from 物料机台对照表, including spare alternates
        # 3) assembly-line pool (infinite, unconstrained)
        row_alts = []
        row_seen = set()
        for _entry_rownum, entry_row, _entry_code, _entry_demand in usable:
            for token, members in parse_concrete_alternatives(entry_row[8]):
                if token not in row_seen:
                    row_seen.add(token)
                    row_alts.append((token, members, 1))
        candidates = []
        assembly_line = None
        tier = 0
        if row_alts:
            candidates = row_alts
            tier = 1
        else:
            for item_code in end_codes:
                info = machine_map.get(item_code)
                if not info:
                    continue
                map_alts = [(t, m, 1) for t, m in info["rated"]]
                map_alts.extend((t, m, 2) for t, m in info["spare"] if t not in {x[0] for x in map_alts})
                if map_alts:
                    candidates = map_alts
                    tier = 2
                    break
            if tier == 0:
                for item_code in end_codes:
                    assembly_line = (machine_map.get(item_code) or {}).get("assembly")
                    if assembly_line:
                        break
                if not assembly_line:
                    for _entry_rownum, entry_row, _entry_code, _entry_demand in usable:
                        assembly_line = parse_assembly_line(entry_row[8])
                        if assembly_line:
                            break
                # Still nothing: use 生产车间 → N工段装配线
                if not assembly_line:
                    workshop_to_line = {ws: line for line, ws in ASSEMBLY_LINES.items()}
                    for _entry_rownum, entry_row, _entry_code, _entry_demand in usable:
                        assembly_line = workshop_to_line.get(text(entry_row[7]))
                        if assembly_line and assembly_line in resources:
                            break
                        assembly_line = None
                if assembly_line:
                    tier = 3

        nonlocal tier1_orders, tier2_orders, tier3_orders, no_machine_orders
        if tier == 1:
            tier1_orders += 1
        elif tier == 2:
            tier2_orders += 1
        elif tier == 3:
            tier3_orders += 1
        else:
            no_machine_orders += 1

        positive_flows = {
            flow["item"]: flow
            for flow in flows_by_operation[base_name]
            if num(flow.get("quantity")) > 0
        }
        people = max(staffing.get(item, 1) for item in end_codes)

        def add_end_flows(op_name):
            for item_code in end_codes:
                source_flow = positive_flows.get(item_code) or next(iter(positive_flows.values()), None)
                if source_flow is None:
                    new_flows.append(
                        {
                            "operation": op_name,
                            "item": item_code,
                            "location": "FC_FACTORY",
                            "quantity": 1,
                            "type": "end",
                            "priority": 1,
                            "source": "fc_aps",
                        }
                    )
                else:
                    copied = deepcopy(source_flow)
                    copied["operation"] = op_name
                    copied["item"] = item_code
                    copied["quantity"] = 1
                    if copied.get("type") not in (None, "", "end"):
                        copied["type"] = "end"
                    new_flows.append(copied)

        def add_molds(op_name):
            copied_mold = False
            for load in loads_by_operation.get(base_name, []):
                resource = resources.get(load["resource"], {})
                if resource.get("category") == "模具":
                    copied = deepcopy(load)
                    copied["operation"] = op_name
                    new_loads.append(copied)
                    copied_mold = True
            if mold and not copied_mold and resources.get("MOLD_" + mold, {}).get("category") == "模具":
                new_loads.append(
                    {
                        "operation": op_name,
                        "resource": "MOLD_" + mold,
                        "quantity": 1,
                        "name": "模具",
                        "priority": 1,
                        "search": "PRIORITY",
                        "source": "fc_aps",
                    }
                )

        def add_people(op_name, workshops):
            for ws in workshops:
                person = "PERSON_" + ws
                if person in resources:
                    new_loads.append(
                        {
                            "operation": op_name,
                            "resource": person,
                            "quantity": people,
                            "name": "人员" if len(workshops) == 1 else f"人员_{ws}",
                            "priority": 1,
                            "search": "PRIORITY",
                            "source": "fc_aps",
                        }
                    )

        def add_machines(op_name, members):
            workshops = []
            for machine_name in members:
                new_loads.append(
                    {
                        "operation": op_name,
                        "resource": machine_name,
                        "quantity": 1,
                        "name": "设备" if len(members) == 1 else f"设备_{machine_name}",
                        "priority": 1,
                        "search": "PRIORITY",
                        "source": "fc_aps",
                    }
                )
                ws = machine_workshop.get(machine_name)
                if ws and ws not in workshops:
                    workshops.append(ws)
            return workshops

        def row_workshops():
            workshops = []
            for _entry_rownum, entry_row, _entry_code, _entry_demand in usable:
                ws = text(entry_row[7])
                if ws and ws not in workshops:
                    workshops.append(ws)
            return workshops

        if tier in (1, 2) and len(candidates) > 1:
            # Alternate parent selects among rated/spare concrete machines.
            operation["type"] = "alternate"
            operation["search"] = "PRIORITY"
            operation["duration"] = None
            operation["duration_per"] = None
            for token, members, prio in candidates:
                child = order_op + "__" + op_suffix(token)
                child_op = deepcopy(base)
                child_op.update(
                    {
                        "name": child,
                        "owner": order_op,
                        "item": None,
                        "description": (operation.get("description") or "") + " @ " + token,
                        "duration": None,
                        "duration_per": duration or base.get("duration_per"),
                        "effective_start": release.isoformat() if release else None,
                        "available": release_calendar if release else None,
                        "priority": prio * 1000,
                    }
                )
                new_operations.append(child_op)
                if order_op in joint_operation_names:
                    joint_operation_names.add(child)
                add_end_flows(child)
                add_molds(child)
                machine_workshops = add_machines(child, members)
                # Some valid row/map equipment (eg 包覆线1/2) isn't maintained
                # in 人数.xlsx under its detailed workshop name (eg
                # 制造部5工段-包覆/缝纫). In that case the 未入库 row's parent
                # production workshop is authoritative for the personnel pool.
                staffed_workshops = [
                    ws for ws in machine_workshops
                    if "PERSON_" + ws in resources
                ]
                add_people(child, staffed_workshops or row_workshops())
        elif tier in (1, 2):
            _token, members, _prio = candidates[0]
            add_end_flows(order_op)
            add_molds(order_op)
            machine_workshops = add_machines(order_op, members)
            staffed_workshops = [
                ws for ws in machine_workshops
                if "PERSON_" + ws in resources
            ]
            add_people(order_op, staffed_workshops or row_workshops())
        elif tier == 3:
            add_end_flows(order_op)
            add_molds(order_op)
            new_loads.append(
                {
                    "operation": order_op,
                    "resource": assembly_line,
                    "quantity": 1,
                    "name": "装配线",
                    "priority": 1,
                    "search": "PRIORITY",
                    "source": "fc_aps",
                }
            )
            ws = ASSEMBLY_LINES.get(assembly_line) or machine_workshop.get(assembly_line)
            workshops = [ws] if ws else row_workshops()
            add_people(order_op, workshops)
        else:
            add_end_flows(order_op)
            add_molds(order_op)
            add_people(order_op, row_workshops())

    buckets = defaultdict(list)
    solos = []
    for rownum, row in unin_source_rows:
        code = text(row[0])
        if not code or num(row[2]) <= 0:
            continue
        grouped = members_of.get(code)
        if grouped:
            buckets[grouped[0]].append((rownum, row))
        else:
            solos.append((rownum, row))
    # One shot makes one of every part in the mold group. The side with more
    # unfinished quantity sets the shot count; the other parts come out of
    # those same runs (共模多产允许, same rule as 预警 sync align).
    for entries in buckets.values():
        totals = defaultdict(float)
        for _rownum, row in entries:
            totals[text(row[0])] += num(row[2])
        sample = text(entries[0][1][0])
        _key, members, cavity, mold = members_of[sample]
        driver = max(members, key=lambda item: (totals.get(item, 0.0), item))
        for rownum, row in entries:
            code = text(row[0])
            if code == driver:
                emit_unin([(rownum, row)], members, mold if cavity == "2+2" else None)
                continue
            demand = demand_by_suffix.get(f"{rownum:05d}")
            if demand is None:
                continue
            demand["status"] = "closed"
            demand["description"] = (
                (demand.get("description") or "")
                + " %s随%s按%s一起产" % (cavity, driver, totals[driver])
            )
    for rownum, row in solos:
        emit_unin([(rownum, row)], [text(row[0])])

    # Alarm demands need the generic BOM/manufacturing routes for selfmade
    # components. Unfinished orders still use their own direct, component-
    # free routes, so their lower-level materials remain assumed available.
    data["operations"].extend(new_operations)
    data["operationMaterials"].extend(new_flows)
    data["operationResources"].extend(new_loads)
    for parameter in document["parameters"]:
        if parameter["name"] == "fc_aps.batch_policy":
            policy = json.loads(parameter["value"])
            # Inherit item SNP/economic. Previously every UNIN op was forced to
            # snp=1/economic=0, so CP lot-split saw a fake unit-1 grid and
            # almost nothing qualified under real SNP/economic rules.
            op_by_name = {operation["name"]: operation for operation in new_operations}
            item_rules = policy.get("items") or {}

            def resolve_unin_item(operation):
                current = operation
                seen = set()
                while current and current["name"] not in seen:
                    seen.add(current["name"])
                    item = current.get("item")
                    if item:
                        return item
                    owner = current.get("owner")
                    current = op_by_name.get(owner) if owner else None
                # Child names are FC_UNIN_OP_R#####_<item>_... — parse item code.
                match = re.search(
                    r"(01\.\d{2}\.[0-9A-Za-z]+)", operation.get("name") or ""
                )
                return match.group(1) if match else None

            unin_op_policies = {}
            for operation in new_operations:
                item = resolve_unin_item(operation)
                rule = item_rules.get(item or "", {}) if item else {}
                try:
                    snp = max(1, int(float(rule.get("snp") or 1)))
                except (TypeError, ValueError):
                    snp = 1
                try:
                    economic = float(rule.get("economic") or 0)
                except (TypeError, ValueError):
                    economic = 0.0
                unin_op_policies[operation["name"]] = {
                    "economic": economic,
                    "snp": snp,
                    # 未入库仍按订单精确数量；切点单位用上面的真实 SNP/经济批量。
                    "exact": True,
                    "coproduct": operation["name"] in joint_operation_names,
                }
            policy["operations"].update(unin_op_policies)
            policy["objective_ordering"]["enabled"] = True
            policy["objective_ordering"]["complete_orders_first"] = True
            policy["objective_ordering"]["use_economic_batch"] = True
            policy["objective_ordering"]["exact_split_over_hours"] = 8
            policy["objective_ordering"]["window_split_item_prefixes"] = ["01.52"]
            policy["objective_ordering"]["window_split_resources"] = [
                "PERSON_制造部2工段",
                "2工段装配线",
            ]
            policy["objective_ordering"]["pull_fillers_into_window"] = True
            policy["objective_ordering"]["preserve_demand_priority"] = True
            exact_items = {
                item
                for item, rule in policy.get("items", {}).items()
                if rule.get("exact")
            }
            net_alarm_demands_against_unfinished(data["demands"], exact_items)
            # The lifecycle limit originally reflects unfinished quantity only.
            # After netting, open demand is "未入库 + 剩余预警"; the ceiling
            # must rise to that total (e.g. 11 + 12 = 23), not stay at 11.
            sync_lifecycle_limits_after_alarm_netting(policy, data["demands"])
            parameter["value"] = json.dumps(policy, ensure_ascii=False)
            break
    # Re-assert 备库 as idle fillers after unfinished-order route binding.
    # Lowest priority (highest number) so they only soak leftover capacity.
    stock_rank = next_priority
    for demand in data["demands"]:
        if demand["name"].startswith(("FC_ALARM_", "FC_UNIN_")):
            stock_rank = max(stock_rank, int(demand.get("priority") or 0) + 1)
    stock_open = 0
    for demand in sorted(
        (d for d in data["demands"] if d["name"].startswith("FC_STOCK_")),
        key=lambda d: d["name"],
    ):
        demand["status"] = "closed" if exclude_stock_demands else "open"
        demand["category"] = "备库"
        demand["priority"] = stock_rank
        stock_rank += 1
        # 备库 has no customer promise inside the three-day priority window.
        # Give it the plan-horizon due date; the solver can still use it as
        # an idle-capacity filler, without reporting those filler jobs late.
        demand["due"] = common_due.isoformat()
        demand.pop("operation", None)
        if demand["status"] == "open":
            stock_open += 1
    # Final sync align: 预警 + any 未入库 sync pair not already closed by
    # 1+1/2+2 mold grouping above (共模多产允许 on both classes).
    align_sync_coproduct_demands(data, classes=("预警", "未入库"))
    summary = {
        "demands": len(data["demands"]),
        "alarm_rows": len(alarm_demands),
        "alarm_to_produce": sum(d["status"] == "open" for d in alarm_demands),
        "stock_open": stock_open,
        "order_operations": len(new_operations),
        "tier1_row_machines": tier1_orders,
        "tier2_map_machines": tier2_orders,
        "tier3_assembly_lines": tier3_orders,
        "no_machine_orders": no_machine_orders,
    }
    document["_build_summary"] = summary
    if output is not None:
        out = Path(output)
        out.parent.mkdir(parents=True, exist_ok=True)
        to_write = {k: v for k, v in document.items() if k != "_build_summary"}
        out.write_text(json.dumps(to_write, ensure_ascii=False), encoding="utf-8")
        summary = {**summary, "output": str(out)}
        print(json.dumps(summary, ensure_ascii=False))
    return document


def main(argv: list[str] | None = None) -> int:
    """Legacy CLI; prefer ``python build_fc_dataset.py --profile unin_alarm``."""
    parser = argparse.ArgumentParser(
        description="Legacy entry; prefer: python build_fc_dataset.py --profile unin_alarm"
    )
    parser.add_argument("--input-dir", "--source-dir", type=Path, default=ROOT, dest="source_dir")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--planning-start", type=datetime.fromisoformat)
    parser.add_argument("--planning-end", type=datetime.fromisoformat)
    parser.add_argument("--exclude-stock-demands", action="store_true")
    args = parser.parse_args(argv)
    source = args.source_dir.resolve()
    build_unin_alarm_dataset(
        source,
        output=args.output or (source / "fc_aps_scenario7_unin.json"),
        planning_start=args.planning_start,
        planning_end=args.planning_end,
        exclude_stock_demands=args.exclude_stock_demands,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

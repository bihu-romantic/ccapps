#!/usr/bin/env python3
"""Convert FC APS Excel workbooks into a standard ccAPPS dataset JSON.

ccAPPS mapping
==============
* 设备 / 模具 / 人员 are three resource categories. Personnel are one pool
  per 工段; size follows the shift calendar rather than a single maximum.
* 标准工时.CT时间 is cycle time in seconds (matches 小时产量 ≈ 3600/CT).
  That value is ``duration_per``. ``duration`` is setup time and stays empty.
* 物料机台对照表 separators: ``/`` (and comma) is a list of interchangeable
  machines and becomes alternate children. ``&`` is one inseparable group:
  one child that loads every member machine together (not pick-one).
* 「N工段装配线」 is a work-center pool, not an occupy-all group: no station
  list exists, so the line resource is infinite and parallel jobs are limited
  only by that 工段's PERSON calendar and 标准工时.配置人数.
* Every time_per leaf loads 设备 (only from 物料机台对照表) + 人员 (by 工段)
  + 模具. No neighbor/mold machine inference. 工段 comes from the loaded
  machines, else from 未入库.生产车间 when present.
* 制造部5工段 has no night shift (12h day). time_per operations there get
  ``sizemaximum`` so one MO is about one hour of work and must split.
* Routings are built only for open 报警表 order items that are 自制.
  BOM-descendant 自制 items are treated as stock-sufficient (buffer only,
  large onhand, no routing) for this planning cut.
* 产品外购 / 外购 items are supply only (buffer, no routing).
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook

ROOT = Path(__file__).parent
SOURCE = "fc_aps"
LOCATION = "FC_FACTORY"
MAKE_ATTRS = {"产品自制", "自制", "自制件"}
# Temporary planning assumption: BOM-exploded make items are covered by stock.
ASSUMED_MAKE_STOCK = 1_000_000
# 对照表里的「N工段装配线」是工段工作中心（资源池），不是机台.xlsx 里的单台，
# 也不是 B03&B04 那种整组锁定。无限产能，只靠该工段人员池限制并行。
ASSEMBLY_LINES = {
    "1工段装配线": "制造部1工段",
    "2工段装配线": "制造部2工段",
    "3工段装配线": "制造部3工段",
    "4工段装配线": "制造部4工段",
    "5工段装配线": "制造部5工段",
}
# Only ``/`` and commas are alternatives. ``&`` stays one group token.
MACHINE_SPLIT = re.compile(r"[,，/]+")


def rows(name):
    ws = load_workbook(ROOT / name, read_only=True, data_only=True).active
    return list(ws.iter_rows(values_only=True))[1:]


def num(x, default=0.0):
    try:
        if x is None or x == "":
            return float(default)
        return float(x)
    except (TypeError, ValueError):
        return float(default)


def text(x):
    if x is None:
        return ""
    return str(x).strip()


def iso_datetime(x):
    if not x:
        return None
    if isinstance(x, datetime):
        return x.isoformat()
    raw = str(x).strip()
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).isoformat()
        except ValueError:
            continue
    return raw


def split_machines(raw):
    if not raw:
        return []
    return [part.strip() for part in MACHINE_SPLIT.split(str(raw)) if part.strip()]


def machine_members(token):
    return [part.strip() for part in str(token).split("&") if part.strip()]


def op_suffix(token):
    return token.replace("&", "+")


def trailing_num(code):
    tail = code.split(".")[-1]
    digits = "".join(ch for ch in tail if ch.isdigit())
    return int(digits) if digits else 0


def overnight_windows(start, end):
    """Split a shift that crosses midnight into ccAPPS calendar windows."""
    start = str(start or "00:00")
    end = str(end or "23:59")
    if len(start) == 5:
        start += ":00"
    if len(end) == 5:
        end += ":00"
    if start < end:
        return [(start, end)]
    return [(start, "23:59:59"), ("00:00:00", end)]


def add_bucket(buckets, calendar, value, start, end):
    # Unique key is (calendar, startdate, enddate, priority). Day/night and
    # overnight splits must not share priority 0.
    used = {b["priority"] for b in buckets if b["calendar"] == calendar}
    priority = (max(used) + 1) if used else 0
    for starttime, endtime in overnight_windows(start, end):
        buckets.append(
            {
                "calendar": calendar,
                "value": value,
                "startdate": "2026-09-10",
                "enddate": "2035-12-31",
                "starttime": starttime,
                "endtime": endtime,
                "priority": priority,
                "monday": True,
                "tuesday": True,
                "wednesday": True,
                "thursday": True,
                "friday": True,
                "saturday": True,
                "sunday": True,
                "source": SOURCE,
            }
        )
        priority += 1


def op_resource(operation, resource, quantity, name, priority):
    return {
        "operation": operation,
        "resource": resource,
        "quantity": quantity,
        "name": name,
        "priority": priority,
        "search": "PRIORITY",
        "source": SOURCE,
    }


def build():
    item_name = {}
    item_category = {}
    make_codes = set()
    lot_size = {}
    for code, org, name, lifecycle, attr, econ, *_ in rows("物料.xlsx"):
        code = text(code)
        if not code:
            continue
        item_name[code] = text(name) or code
        attr = text(attr)
        if attr in MAKE_ATTRS:
            make_codes.add(code)
            item_category[code] = "自制"
        else:
            item_category.setdefault(code, attr or "外购")
        qty = num(econ, 0)
        if qty > 1:
            lot_size[code] = max(lot_size.get(code, 0), qty)

    machines = {}
    for name, model, workshop in rows("机台.xlsx"):
        name = text(name)
        if name:
            machines[name] = (text(workshop), text(model) or name)
    for line, workshop in ASSEMBLY_LINES.items():
        machines.setdefault(line, (workshop, line))

    molds = {}
    for mold, name, cavities, quantity in rows("模具.xlsx"):
        mold = text(mold)
        if mold:
            molds[mold] = (text(name) or mold, max(num(quantity, 1), 1))

    # (workshop, shift) -> (start, end, headcount)
    shifts = {}
    for day, workshop, shift, start, end, count in rows("人数.xlsx"):
        workshop = text(workshop)
        shift = text(shift) or "白班"
        if not workshop:
            continue
        key = (workshop, shift)
        shifts[key] = (start or "08:00", end or "20:00", num(count, 0))

    workshops = sorted({ws for ws, _ in shifts})
    duration_per = {}
    staffing = {}
    for code, ct, output, people, *_ in rows("标准工时.xlsx"):
        code = text(code)
        if not code:
            continue
        duration_per[code] = num(ct, 0)
        staffing[code] = num(people, 1)

    machine_priority = {}
    for code, rated, spare, primary in rows("物料机台对照表.xlsx"):
        code = text(code)
        if not code:
            continue
        ranked = machine_priority.setdefault(code, {})
        for raw, prio in ((rated, 1), (spare, 2)):
            for token in split_machines(raw):
                ranked[token] = min(ranked.get(token, prio), prio)
        # 主机台 is the preferred machine inside a group, not a new split.
        for token in split_machines(primary):
            members = machine_members(token)
            covered = False
            for existing, prio in list(ranked.items()):
                existing_members = machine_members(existing)
                if token == existing or (members and all(m in existing_members for m in members)):
                    ranked[existing] = min(prio, 1)
                    covered = True
            if not covered:
                ranked[token] = min(ranked.get(token, 1), 1)

    mold_map = {}
    for code, mold, *_ in rows("物料模具对照表.xlsx"):
        code, mold = text(code), text(mold)
        if code and mold:
            mold_map.setdefault(code, [])
            if mold not in mold_map[code]:
                mold_map[code].append(mold)
            molds.setdefault(mold, (mold, 1))

    # Explicit 工段 hint when 机台对照 is missing (人员按工段挂，不靠邻项推机台).
    item_workshop_votes = defaultdict(Counter)
    stock = {}
    for code, org, immediate, finished, unshipped in rows("库存.xlsx"):
        code = text(code)
        if code:
            stock[code] = stock.get(code, 0) + num(immediate) + num(finished)
    for code, name, qty, _org, _plan_qty, _start, _end, workshop, *_ in rows("未入库.xlsx"):
        code = text(code)
        if code:
            stock[code] = stock.get(code, 0) + num(qty)
        workshop = text(workshop)
        if code and workshop:
            item_workshop_votes[code][workshop] += 1
    item_workshop = {
        code: votes.most_common(1)[0][0]
        for code, votes in item_workshop_votes.items()
        if votes
    }

    bom_rows = []
    for parent, pname, child, cname, numerator, denominator, org, *_ in rows("BOM.xlsx"):
        parent, child = text(parent), text(child)
        if not parent or not child:
            continue
        bom_rows.append((parent, child, num(numerator, 1), max(num(denominator, 1), 1e-12), text(pname), text(cname)))
        item_name.setdefault(parent, pname or parent)
        item_name.setdefault(child, cname or child)
        if parent not in item_category:
            item_category[parent] = "自制"
        if child not in item_category:
            item_category[child] = "外购"

    bom_children = defaultdict(list)
    for parent, child, *_ in bom_rows:
        bom_children[parent].append(child)

    def is_make(code):
        if code in make_codes:
            return True
        return item_category.get(code) in MAKE_ATTRS or item_category.get(code) == "自制"

    # Open alarm 自制 → build routing. BOM-descendant 自制 → assume stock covers.
    order_items = set()
    for code, org, ship, qty in rows("报警表.xlsx"):
        code = text(code)
        if code and num(qty) > 0:
            order_items.add(code)
            item_name.setdefault(code, code)

    produce = {code for code in order_items if is_make(code)}

    bom_make = set()
    stack = list(produce)
    seen = set(produce)
    while stack:
        code = stack.pop()
        for child in bom_children.get(code, []):
            if child in seen:
                continue
            seen.add(child)
            if is_make(child):
                bom_make.add(child)
                stack.append(child)

    for code in bom_make:
        item_name.setdefault(code, code)
        item_category.setdefault(code, "自制")
        stock[code] = max(stock.get(code, 0), ASSUMED_MAKE_STOCK)

    item_codes = set(item_name) | set(stock) | {p for p, *_ in bom_rows} | {c for _, c, *_ in bom_rows} | produce | order_items | bom_make
    for code in produce:
        item_name.setdefault(code, code)
        item_category.setdefault(code, "自制")

    calendars = [
        {"name": "FC_24X7", "description": "默认可用", "defaultvalue": 100, "source": SOURCE},
    ]
    buckets = [
        {
            "calendar": "FC_24X7",
            "value": 100,
            "startdate": "2024-01-01",
            "enddate": "2035-12-31",
            "starttime": "00:00:00",
            "endtime": "23:59:59",
            "source": SOURCE,
        }
    ]
    for workshop in workshops:
        person_cal = "CAL_PERSON_" + workshop
        shift_cal = "CAL_SHIFT_" + workshop
        calendars.append({"name": person_cal, "description": workshop + "出勤人数", "defaultvalue": 0, "source": SOURCE})
        calendars.append({"name": shift_cal, "description": workshop + "班次", "defaultvalue": 0, "source": SOURCE})
        for shift in ("白班", "夜班"):
            if (workshop, shift) not in shifts:
                continue
            start, end, count = shifts[(workshop, shift)]
            add_bucket(buckets, person_cal, count, start, end)
            if count > 0:
                add_bucket(buckets, shift_cal, 100, start, end)

    resources = []
    for machine, (workshop, model) in machines.items():
        row = {
            "name": machine,
            "description": ("%s %s" % (workshop, model)).strip(),
            "category": "设备",
            "type": "default",
            "maximum": 1,
            "efficiency": 100,
            "constrained": True,
            "location": LOCATION,
            "source": SOURCE,
        }
        if workshop in workshops:
            row["available"] = "CAL_SHIFT_" + workshop
        if machine in ASSEMBLY_LINES:
            row["type"] = "infinite"
            row["constrained"] = False
            row["subcategory"] = "装配工作中心"
            row["description"] = workshop + "装配工作中心（资源池，不整组锁定；并行靠人员）"
        resources.append(row)
    for mold, (name, quantity) in molds.items():
        resources.append(
            {
                "name": "MOLD_" + mold,
                "description": name,
                "category": "模具",
                "type": "default",
                "maximum": quantity,
                "efficiency": 100,
                "constrained": True,
                "location": LOCATION,
                "source": SOURCE,
            }
        )
    for workshop in workshops:
        peak = max(count for (ws, shift), (_, _, count) in shifts.items() if ws == workshop)
        resources.append(
            {
                "name": "PERSON_" + workshop,
                "description": workshop + "人员",
                "category": "人员",
                "type": "default",
                "maximum": peak,
                "maximum_calendar": "CAL_PERSON_" + workshop,
                "available": "CAL_SHIFT_" + workshop,
                "efficiency": 100,
                "constrained": True,
                "location": LOCATION,
                "source": SOURCE,
            }
        )

    operations = []
    operation_resources = []
    operation_materials = []

    def attach_molds(op_name, code):
        for mold in mold_map.get(code, []):
            operation_resources.append(op_resource(op_name, "MOLD_" + mold, 1, "模具", 1))

    def known_members(token):
        return [m for m in machine_members(token) if m in machines]

    def attach_people(op_name, code, workshop):
        if workshop in workshops:
            operation_resources.append(
                op_resource(op_name, "PERSON_" + workshop, staffing.get(code, 1), "人员", 1)
            )

    def attach_people_list(op_name, code, workshop_list):
        seen = []
        for workshop in workshop_list:
            if workshop in workshops and workshop not in seen:
                seen.append(workshop)
        if len(seen) <= 1:
            if seen:
                attach_people(op_name, code, seen[0])
            return
        for workshop in seen:
            operation_resources.append(
                op_resource(op_name, "PERSON_" + workshop, staffing.get(code, 1), "人员_" + workshop, 1)
            )

    def attach_equipment(op_name, code, token):
        members = known_members(token)
        if not members:
            return
        if len(members) == 1:
            operation_resources.append(op_resource(op_name, members[0], 1, "设备", 1))
            attach_people(op_name, code, machines[members[0]][0])
            return
        # ``&`` group: unique names so every machine is required, not alternate.
        for machine in members:
            operation_resources.append(op_resource(op_name, machine, 1, "设备_" + machine, 1))
        attach_people_list(op_name, code, [machines[m][0] for m in members])

    def workshops_for_token(token):
        return {machines[m][0] for m in known_members(token)}

    def time_fields(code, workshops_used=None):
        ct = duration_per.get(code)
        row = {"duration": None, "duration_per": ct if ct and ct > 0 else None}
        if code in lot_size:
            row["sizemultiple"] = lot_size[code]
        # 5工段夜班为 0；工单按约 1 小时产量拆批，便于白班窗口内排入。
        if workshops_used and "制造部5工段" in workshops_used and ct and ct > 0:
            sizemax = max(1, int(3600 // ct))
            row["sizemaximum"] = sizemax
            if row.get("sizemultiple") and row["sizemultiple"] > sizemax:
                row.pop("sizemultiple")
        return row

    def attach_people_for_code(op_name, code, workshops_used=None):
        """人员按工段：优先用机台所属工段，否则用未入库.生产车间。"""
        if workshops_used:
            attach_people_list(op_name, code, list(workshops_used))
            return
        hint = item_workshop.get(code)
        if hint:
            attach_people(op_name, code, hint)

    no_equipment = []
    for code in sorted(produce):
        name = item_name.get(code, code)
        ranked = machine_priority.get(code, {})
        candidates = [m for m in ranked if known_members(m)]
        candidates.sort(key=lambda m: (ranked[m], m))
        parent = "FC_OP_" + code
        if not candidates:
            no_equipment.append(code)
        if len(candidates) <= 1:
            token = candidates[0] if candidates else None
            workshops_used = workshops_for_token(token) if token else set()
            if not workshops_used and item_workshop.get(code):
                workshops_used = {item_workshop[code]}
            extras = time_fields(code, workshops_used)
            operations.append(
                {
                    "name": parent,
                    "type": "time_per",
                    "description": name,
                    "item": code,
                    "location": LOCATION,
                    "priority": 1,
                    "sizeminimum": 1,
                    "source": SOURCE,
                    **extras,
                }
            )
            if token:
                attach_equipment(parent, code, token)
            else:
                attach_people_for_code(parent, code, workshops_used)
            attach_molds(parent, code)
            operation_materials.append(
                {"operation": parent, "item": code, "location": LOCATION, "quantity": 1, "type": "end", "priority": 1, "source": SOURCE}
            )
            continue

        operations.append(
            {
                "name": parent,
                "type": "alternate",
                "description": name,
                "item": code,
                "location": LOCATION,
                "priority": 1,
                "search": "PRIORITY",
                "sizeminimum": 1,
                "source": SOURCE,
            }
        )
        operation_materials.append(
            {"operation": parent, "item": code, "location": LOCATION, "quantity": 1, "type": "end", "priority": 1, "source": SOURCE}
        )
        for token in candidates:
            child = parent + "__" + op_suffix(token)
            extras = time_fields(code, workshops_for_token(token))
            operations.append(
                {
                    "name": child,
                    "type": "time_per",
                    "description": "%s @ %s" % (name, token),
                    "item": None,
                    "location": LOCATION,
                    "owner": parent,
                    "priority": ranked[token],
                    "sizeminimum": 1,
                    "source": SOURCE,
                    **extras,
                }
            )
            attach_equipment(child, code, token)
            attach_molds(child, code)

    producing_ops = {code: "FC_OP_" + code for code in produce}
    for parent, child, numerator, denominator, *_ in bom_rows:
        if parent not in producing_ops:
            continue
        operation_materials.append(
            {
                "operation": producing_ops[parent],
                "item": child,
                "location": LOCATION,
                "quantity": -numerator / denominator,
                "type": "start",
                "priority": 1,
                "source": SOURCE,
            }
        )

    buffers = []
    for code in sorted(item_codes):
        buffers.append(
            {
                "item": code,
                "location": LOCATION,
                "batch": "",
                "onhand": stock.get(code, 0),
                "source": SOURCE,
            }
        )

    demands = []
    for rownum, (code, org, ship, qty) in enumerate(rows("报警表.xlsx"), 2):
        code = text(code)
        if code not in item_codes or num(qty) <= 0:
            continue
        demands.append(
            {
                "name": "FC_ALARM_%s_%s_%s_R%05d" % (org, code, str(ship).replace("/", ""), rownum),
                "customer": "FC_CUSTOMER",
                "item": code,
                "location": LOCATION,
                "due": iso_datetime(ship),
                "status": "open",
                "quantity": num(qty),
                "priority": 10,
                "source": SOURCE,
            }
        )
    for rownum, (code, org, w1, w2, w3, total) in enumerate(rows("备库需求.xlsx"), 2):
        code = text(code)
        if code not in item_codes or num(total) <= 0:
            continue
        demands.append(
            {
                "name": "FC_STOCK_%s_%s_R%05d" % (org, code, rownum),
                "customer": "FC_CUSTOMER",
                "item": code,
                "location": LOCATION,
                "due": "2026-09-30T00:00:00",
                "status": "closed",
                "quantity": num(total),
                "priority": 20,
                "source": SOURCE,
            }
        )

    items = [
        {"name": code, "description": item_name.get(code, code), "category": item_category.get(code), "source": SOURCE}
        for code in sorted(item_codes)
    ]
    return {
        "clean_all": True,
        "parameters": [
            {"name": "currentdate", "value": "2026-09-10 08:00:00"},
            {"name": "plan.solver", "value": "mrp"},
        ],
        "data": {
            "locations": [{"name": LOCATION, "source": SOURCE}, {"name": "FC_RAW", "source": SOURCE}],
            "calendars": calendars,
            "calendarBuckets": buckets,
            "items": items,
            "resources": resources,
            "operations": operations,
            "operationResources": operation_resources,
            "operationMaterials": operation_materials,
            "buffers": buffers,
            "customers": [{"name": "FC_CUSTOMER", "source": SOURCE}],
            "demands": demands,
            "skills": [],
            "resourceSkills": [],
            "itemDistributions": [],
        },
        "_meta": {
            "bom_make_stocked": len(bom_make),
            "no_equipment": [
                {"code": code, "name": item_name.get(code, code), "workshop": item_workshop.get(code), "has_mold": bool(mold_map.get(code)), "has_ct": code in duration_per}
                for code in no_equipment
            ]
        },
    }


def main():
    doc = build()
    meta = doc.pop("_meta", {})
    out = ROOT / "fc_aps_scenario6.json"
    out.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    data = doc["data"]
    print(
        "items", len(data["items"]),
        "resources", len(data["resources"]),
        "operations", len(data["operations"]),
        "operationResources", len(data["operationResources"]),
        "operationMaterials", len(data["operationMaterials"]),
        "demands", len(data["demands"]),
        "calendars", len(data["calendars"]),
        "bom_make_stocked", meta.get("bom_make_stocked", 0),
    )
    missing = meta.get("no_equipment", [])
    print("no_equipment_on_order_items", len(missing))
    for row in missing:
        print(
            "  ",
            row["code"],
            row["name"],
            "工段=" + (row["workshop"] or "未知"),
            "模具=" + ("有" if row["has_mold"] else "无"),
            "CT=" + ("有" if row["has_ct"] else "无"),
        )
    csv_path = ROOT / "no_equipment.csv"
    with csv_path.open("w", encoding="utf-8") as f:
        f.write("物料编码,名称,工段,模具,CT\n")
        for row in missing:
            f.write(
                "%s,%s,%s,%s,%s\n"
                % (
                    row["code"],
                    row["name"].replace(",", "，"),
                    row["workshop"] or "未知",
                    "有" if row["has_mold"] else "无",
                    "有" if row["has_ct"] else "无",
                )
            )


if __name__ == "__main__":
    main()

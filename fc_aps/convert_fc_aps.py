#!/usr/bin/env python3
"""Convert FC APS Excel workbooks into a standard ccAPPS dataset JSON.

Stage A of the dataset pipeline (relationship checks + base tables).
Prefer the generic entry ``build_fc_dataset.py`` (see DATA_PIPELINE.md).

ccAPPS mapping
==============
* 设备 / 模具 / 人员 are three resource categories. Personnel are one pool
  per 工段; size follows the shift calendar rather than a single maximum.
* 标准工时优先使用单个CT；单个CT为空或0时使用CT时间。该值的单位
  是秒/件，并映射为 ``duration_per``。``duration`` is setup time and stays empty.
* 物料机台对照表 separators: ``/`` (and comma) is a list of interchangeable
  machines and becomes alternate children. ``&`` is one inseparable group:
  one child that loads every member machine together (not pick-one).
* 「N工段装配线」 is a work-center pool, not an occupy-all group: no station
  list exists, so the line resource is infinite and parallel jobs are limited
  by that 工段's PERSON calendar. Assembly and 热压 enumerate integer
  staffing alternatives from 1 to the workshop's peak attendance. Unit time
  is standard CT * standard staffing / allocated people; each MO keeps its
  chosen staffing. Heat presses still require their machine and mold.
* Every time_per leaf loads 设备 (only from 物料机台对照表) + 人员 (by 工段)
  + 模具. No neighbor/mold machine inference. 工段 comes from the loaded
  machines, else from 未入库.生产车间 when present.
* Alternate parents must not consume/produce material (ccAPPS deprecation).
  End-item and BOM start flows go on each time_per child; the parent only
  selects among children.
* Personnel calendars follow 人数.xlsx dates only (here 9/10 08:00 →
  9/13 08:00). Outside those windows headcount is 0. Night headcount 0
  means that window is not used. Same-organization 经济批量 > 1 defines
  full lots. Only the tail rounds up to SNP (0/missing means 1). Without
  an economic batch the complete requirement rounds to SNP, with no
  hourly cap. 备件 / 断点 / 断点件 never round up. Co-products use the
  smallest economic batch and common SNP multiple; restricted members
  impose an exact group quantity and a net-requirement production ceiling.
  Sync/1+1: 预警 and 未入库 both keep one open demand of max(left, right)
  per due/order; the partner closes and rides along (共模多产允许). No
  buffer.minimum double pull. The opt-in fc_aps.batch_policy parameter
  activates engine enforcement.
* 未入库.未入库数量 is the only open demand (priority 1). Quantity is that
  unfinished qty, not 计划数量. Due is 计划结束时间 with the clock time.
  If that end is already before 2026-09-10 08:00, due becomes
  2026-09-11 00:00. 报警表 opens when include_alarm_demands. 备库需求
  stays closed in the base convert; scenario builders may reopen it as the
  lowest-priority idle filler after 预警 and 未入库. 未入库数量 is NOT
  onhand. Workshop hint still comes from 生产车间.
* Order-delivery products keep only real warehouse ATP from 库存.xlsx:
  max(0, 即时库存 + 已完工未入库 - 未发货). 未发货 is reserved for ship
  orders and is not free stock. BOM expansion follows the order's 组织编码.
  未入库不读 BOM，只按未入库数量自制成品。预警先看物料表，自制才自制，
  再进入 BOM：跳层为 False 时只按物料表决定这个子件自不自制，不再往下拆；
  跳层为 True 时进入下一层，重新看物料表和跳层。  外购、外协不自制。
  没有产出工序的缓冲用无限供应；自制缓冲保留真实库存。不生成虚拟库存或兜底采购。
  FORCE_PURCHASED（缺机台/模具映射被迫外采）与按订单组织判为外购/外协的子件，
  category 必须落到外购/外协，不能残留「自制」，否则有限零库存又无无限供应，链路断开。
* 物料模具对照表: ``1*N`` same-cavity multi-shot uses 单个CT as duration_per;
  ``1+1`` 同一模具和分组编号左右一起产，模次按数量多的一边，两边都出这么多；
  配套编号 does not create co-production groups.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from openpyxl import load_workbook

ROOT = Path(__file__).parent
SOURCE = "fc_aps"
LOCATION = "FC_FACTORY"
MAKE_ATTRS = {"产品自制", "自制", "自制件"}
OUTSOURCE_ATTRS = {"产品外协", "外协"}
BUY_ATTRS = {"产品外购", "外购", "外购件"}
EXTERNAL_ATTRS = OUTSOURCE_ATTRS | BUY_ATTRS
# Temporary overrides until 物料机台对照表 is complete for these codes.
# Purchased: no machine/mold/person route available → treat as 外采.
FORCE_PURCHASED = {
    "05.15.00604",
    "02.02.01812",
    "02.02.02316",
    "02.02.02526",
    "02.02.02527",
    "02.02.02528",
    "02.02.02540",
    "02.02.02541",
    "03.01.02328",
    "03.01.10430",
    "03.01.10919",
    # Missing 标准工时 and no usable co-mold inherit → 外采
    "02.05.00140",
    "02.05.00141",
    "02.05.00142",
    "02.05.00143",
    "02.05.00301",
    "02.05.00302",
    "03.01.04274",
    "03.01.04275",
    # 2026-10-16 alarm BOM: standard time exists, but no machine/mold/person
    # mapping is available. Use external supply until the source mapping is added.
    "02.02.02449",
    "03.01.09823",
    "03.01.09824",
    "03.01.09826",
    "03.01.09828",
    "03.01.09831",
    "03.01.09834",
    "03.01.09836",
}
# Self-made but missing from the machine map: bind the machine already used
# on 未入库 orders (so alarm/BOM routes get the same equipment).
FORCE_RATED_MACHINE = {
    "02.02.02524": "A03",
    "02.02.02525": "A03",
    "02.02.02530": "E12",
    "02.02.02599": "C01",
    "03.01.10839": "E12",
    "03.01.10843": "F10",
}
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
OP_SAFE = re.compile(r"[^0-9A-Za-z_.+-]+")


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


def economic_batch(economic, snp):
    """Ignore the default economic batch of one; fall back to positive SNP."""
    economic, snp = num(economic), num(snp)
    if economic > 1:
        return economic
    return snp if snp > 0 else None


def _sync_demand_bucket(demand):
    """Bucket key so co-mold align does not merge unrelated orders.

    预警: same due date. 未入库: same source order id. Over-production on the
    smaller side is intentional (共模多产允许).
    """
    name = str(demand.get("name") or "")
    category = demand.get("category") or ""
    if category == "预警" or name.startswith("FC_ALARM_"):
        return ("预警", demand.get("due"))
    if category == "未入库" or name.startswith("FC_UNIN_"):
        match = re.search(r"_((?:SC|WWDD)[A-Z0-9]+)_R\d+$", name)
        return ("未入库", match.group(1) if match else name)
    return None


def align_sync_coproduct_demands(data, classes=("预警", "未入库")):
    """Align co-mold pairs to one open demand of max(left, right).

    Left 56 + right 66 in the same bucket → keep the larger side open at 66,
    close the other ("随共模一起产"). Smaller-side over-production is allowed
    for 预警 and 未入库. Buckets: 预警 by due, 未入库 by source order id.

    Call with ``classes=("预警",)`` from base convert so scenario-builder 1+1
    mold grouping can still own 未入库; call again at the end of the scenario
    build with both classes for any leftover open sync pairs.

    Clears sync-primary buffer.minimum so safety-stock does not stack a second
    lot.
    """
    allowed = set(classes)
    op_by_name = {op["name"]: op for op in data.get("operations") or []}
    groups = {}
    for flow in data.get("operationMaterials") or []:
        op_name = flow.get("operation") or ""
        if not op_name.startswith("FC_OP_SYNC_") or num(flow.get("quantity")) <= 0:
            continue
        base = op_name.split("__", 1)[0]
        grp = groups.setdefault(
            base,
            {"members": set(), "primary": (op_by_name.get(base) or {}).get("item")},
        )
        grp["members"].add(flow["item"])
        if not grp["primary"]:
            grp["primary"] = flow["item"]

    member_to_groups = defaultdict(list)
    for base, grp in groups.items():
        for code in grp["members"]:
            member_to_groups[code].append(base)

    primary_items = {grp["primary"] for grp in groups.values() if grp.get("primary")}
    aligned = {}

    # Bucket: (sync_base, class, key) -> open rows on that group's members.
    buckets = defaultdict(list)
    for demand in data.get("demands") or []:
        if demand.get("status") != "open":
            continue
        # Already closed/marked by scenario 1+1 / 2+2 mold grouping.
        desc = demand.get("description") or ""
        if re.search(r"[12]\+[12]随", desc):
            continue
        qty = num(demand.get("quantity"))
        if qty <= 1e-9:
            continue
        bucket = _sync_demand_bucket(demand)
        if not bucket or bucket[0] not in allowed:
            continue
        item = demand.get("item")
        for base in member_to_groups.get(item, ()):
            buckets[(base, bucket[0], bucket[1])].append(demand)

    for (base, klass, key), open_rows in buckets.items():
        grp = groups[base]
        primary = grp["primary"]
        members = grp["members"]
        open_rows = list({id(d): d for d in open_rows}.values())
        direct = defaultdict(float)
        for demand in open_rows:
            if demand.get("item") in members:
                direct[demand["item"]] += num(demand.get("quantity"))
        if not any(direct.get(c, 0) > 1e-9 for c in members):
            continue
        lot = max(direct.values(), default=0.0)
        if lot <= 1e-9:
            continue
        driver = max(
            members,
            key=lambda code: (
                direct.get(code, 0.0),
                code == primary,
                code,
            ),
        )
        driver_rows = [d for d in open_rows if d.get("item") == driver]
        if not driver_rows:
            main = max(open_rows, key=lambda d: (num(d.get("quantity")), d.get("name")))
            main["item"] = driver
            driver_rows = [main]
            open_rows = [main] + [d for d in open_rows if d is not main]
        main = max(driver_rows, key=lambda d: (num(d.get("quantity")), d.get("name")))
        main["quantity"] = lot
        lot_label = int(lot) if abs(lot - int(lot)) < 1e-9 else lot
        driver_note = " 共模前置整批%s(随%s)" % (lot_label, driver)
        desc = main.get("description") or ""
        if "共模前置整批" not in desc:
            main["description"] = desc + driver_note
        ride_note = " 共模随%s按%s一起产" % (driver, lot_label)
        for demand in open_rows:
            if demand is main:
                continue
            demand["status"] = "closed"
            desc = demand.get("description") or ""
            if "共模随" not in desc:
                demand["description"] = desc + ride_note
        aligned[(base, klass, key)] = {"driver": driver, "lot": lot, "primary": primary}

    for buf in data.get("buffers") or []:
        if buf.get("item") in primary_items:
            buf["minimum"] = 0
    return aligned


# Backwards-compatible name used by scenario builders.
apply_sync_prebuild_minima = align_sync_coproduct_demands


def parse_cavity(raw):
    """Return (kind, pieces) where kind is 'same' (*), 'diff' (+), or 'one'."""
    s = text(raw)
    if not s:
        return "one", 1
    if "*" in s:
        parts = [p.strip() for p in s.split("*") if p.strip()]
        try:
            pieces = 1
            for p in parts:
                pieces *= max(int(float(p)), 1)
            return "same", pieces
        except (TypeError, ValueError):
            return "same", 1
    if "+" in s:
        parts = [p.strip() for p in s.split("+") if p.strip()]
        try:
            return "diff", sum(max(int(float(p)), 1) for p in parts)
        except (TypeError, ValueError):
            return "diff", 2
    return "one", 1


def op_safe(token):
    return OP_SAFE.sub("_", token).strip("_") or "X"


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


def iso_date_only(x, days_offset=0):
    """Plan due as calendar date at 00:00:00 (drop time-of-day)."""
    if not x:
        return None
    if isinstance(x, datetime):
        d = x.date()
    else:
        raw = str(x).strip()
        d = None
        for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                d = datetime.strptime(raw, fmt).date()
                break
            except ValueError:
                continue
        if d is None:
            return None
    if days_offset:
        d = d + timedelta(days=days_offset)
    return d.isoformat() + "T00:00:00"


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


def parse_day(value):
    """Normalize 人数.xlsx dates like 2026/9/10 → date."""
    if isinstance(value, datetime):
        return value.date()
    text_v = str(value or "").strip().split()[0]
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text_v, fmt).date()
        except ValueError:
            continue
    raise ValueError("bad 人数 date: %r" % (value,))


def clock_seconds(t):
    parts = str(t or "00:00").strip().split(":")
    h = int(float(parts[0]))
    m = int(float(parts[1])) if len(parts) > 1 else 0
    s = int(float(parts[2])) if len(parts) > 2 else 0
    return h * 3600 + m * 60 + s


def normalize_clock(t, default="00:00:00"):
    text_v = str(t or default).strip()
    if len(text_v) == 5:
        text_v += ":00"
    return text_v


def overnight_windows(day, start, end):
    """Split a shift into dated ccAPPS windows (overnight ends next morning)."""
    start = normalize_clock(start, "00:00:00")
    end = normalize_clock(end, "23:59:59")
    if start < end:
        return [(day, start, end)]
    nxt = day + timedelta(days=1)
    return [(day, start, "23:59:59"), (nxt, "00:00:00", end)]


def add_bucket(buckets, calendar, value, day, start, end):
    # Unique key is (calendar, startdate, enddate, priority). Day/night and
    # overnight splits must not share priority 0.
    used = {b["priority"] for b in buckets if b["calendar"] == calendar}
    priority = (max(used) + 1) if used else 0
    for when, starttime, endtime in overnight_windows(day, start, end):
        day_s = when.isoformat()
        buckets.append(
            {
                "calendar": calendar,
                "value": value,
                "startdate": day_s,
                "enddate": day_s,
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


def build(include_alarm_demands=False, planning_start=None):
    planning_start = planning_start or datetime(2026, 9, 10, 8)
    item_name = {}
    item_category = {}
    item_attr = defaultdict(dict)
    item_batch = defaultdict(dict)
    item_snp = defaultdict(dict)
    item_lifecycle = defaultdict(dict)
    for code, org, name, lifecycle, attr, econ, _jit, snp, *_ in rows("物料.xlsx"):
        code = text(code)
        if not code:
            continue
        item_name[code] = text(name) or code
        org_s = text(org)
        attr = text(attr)
        if org_s:
            item_attr[code][org_s] = attr
            item_batch[code][org_s] = num(econ) if num(econ) > 1 else None
            item_snp[code][org_s] = max(1, num(snp))
            item_lifecycle[code][org_s] = text(lifecycle)
        if attr in MAKE_ATTRS:
            item_category[code] = "自制"
        else:
            item_category.setdefault(code, attr or "外购")
    # 缺机台/模具映射 → 强制外采：category 同步为外购，后面才能开无限供应。
    for code in FORCE_PURCHASED:
        item_category[code] = "产品外购"

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

    # Exact 人数 rows: plan only inside these dated windows (9/10 08:00→9/13 08:00).
    shift_rows = []
    for day, workshop, shift, start, end, count in rows("人数.xlsx"):
        workshop = text(workshop)
        shift = text(shift) or "白班"
        if not workshop:
            continue
        shift_rows.append(
            (
                parse_day(day),
                workshop,
                shift,
                start or "08:00",
                end or "20:00",
                num(count, 0),
            )
        )

    workshops = sorted({ws for _, ws, *_ in shift_rows})
    peak_people = {
        ws: max((int(count) for _day, workshop, _shift, _start, _end, count in shift_rows if workshop == ws), default=0)
        for ws in workshops
    }

    # Filled after mold_info so 1*N can scale shot CT → piece CT.
    duration_per = {}
    staffing = {}
    std_hours = {}
    for code, ct, output, people, cavities, single_ct, *_ in rows("标准工时.xlsx"):
        code = text(code)
        if not code:
            continue
        std_hours[code] = (num(ct, 0), num(single_ct, 0), num(output, 0), num(people, 1))
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
    # code -> {mold, cavity, group, kit, kind, pieces}
    mold_info = {}
    mold_map = {}
    for code, mold, cavity, group, kit, *_ in rows("物料模具对照表.xlsx"):
        code, mold = text(code), text(mold)
        if not code or not mold:
            continue
        kind, pieces = parse_cavity(cavity)
        mold_info[code] = {
            "mold": mold,
            "cavity": text(cavity),
            "group": text(group),
            "kit": text(kit),
            "kind": kind,
            "pieces": pieces,
        }
        mold_map.setdefault(code, [])
        if mold not in mold_map[code]:
            mold_map[code].append(mold)
        molds.setdefault(mold, (mold, 1))

    for code, (shot_ct, piece_ct, _out, _people) in std_hours.items():
        # FC standard-time rule: 单个CT is already the per-piece cycle time.
        # Only when it is empty or zero do we fall back to CT时间 directly.
        duration_per[code] = piece_ct if piece_ct > 0 else shot_ct

    # Explicit 工段 hint when 机台对照 is missing (人员按工段挂，不靠邻项推机台).
    # 未入库数量 = unfinished qty to re-plan (not onhand).
    item_workshop_votes = defaultdict(Counter)
    unin_rows = []
    stock = {}
    stock_imm = defaultdict(float)
    stock_fin = defaultdict(float)
    stock_uns = defaultdict(float)
    stock_by_org = defaultdict(float)
    for code, org, immediate, finished, unshipped in rows("库存.xlsx"):
        code = text(code)
        org = text(org)
        if not code:
            continue
        stock_imm[code] += num(immediate)
        stock_fin[code] += num(finished)
        stock_uns[code] += num(unshipped)
        stock_by_org[(code, org)] += num(immediate) + num(finished) - num(unshipped)
    for code in set(stock_imm) | set(stock_fin) | set(stock_uns):
        # ATP: on-hand + finished-not-receipted, minus reserved-to-ship.
        stock[code] = max(stock_imm[code] + stock_fin[code] - stock_uns[code], 0.0)
    # Preserve finished-goods ATP for the optional alarm experiment.  Open
    # 未入库 orders below deliberately zero their own buffers because they are
    # mandatory production, whereas alarm orders are netted against ATP first.
    alarm_atp = {key: max(quantity, 0.0) for key, quantity in stock_by_org.items()}
    for row in rows("未入库.xlsx"):
        code, name, qty, org, plan_qty, start, end, workshop, machine, order, *_ = list(row) + [None] * 10
        code = text(code)
        qty = num(qty)
        workshop = text(workshop)
        if code and workshop:
            item_workshop_votes[code][workshop] += 1
        if code and qty > 0:
            unin_rows.append(
                {
                    "code": code,
                    "name": text(name) or code,
                    "qty": qty,
                    "org": text(org),
                    "plan_qty": num(plan_qty),
                    "start": start,
                    "end": end,
                    "workshop": workshop,
                    "machine": text(machine),
                    "order": text(order),
                }
            )
            item_name.setdefault(code, text(name) or code)
    # Unreceived orders are mandatory production, not sales demand that can
    # be fulfilled from existing finished-goods stock. Apply the same stock
    # basis to buffers and lifecycle production ceilings below.
    for row in unin_rows:
        stock[row["code"]] = 0.0
    item_workshop = {
        code: votes.most_common(1)[0][0]
        for code, votes in item_workshop_votes.items()
        if votes
    }

    bom_rows = []
    bom_skip = {}
    for parent, pname, child, cname, numerator, denominator, org, skip, *_ in rows("BOM.xlsx"):
        parent, child = text(parent), text(child)
        # Some exports repeat the column header as the first data row.
        if (not parent or not child or
                (parent == "父项编码" and child == "子项编码")):
            continue
        bom_rows.append((parent, child, num(numerator, 1), max(num(denominator, 1), 1e-12), text(pname), text(cname), text(org)))
        bom_skip[(parent, child, text(org))] = text(skip).lower() in {"true", "1", "是"}
        item_name.setdefault(parent, pname or parent)
        item_name.setdefault(child, cname or child)
        if parent not in item_category:
            item_category[parent] = "自制"
        if child not in item_category:
            item_category[child] = "外购"

    bom_edges = defaultdict(list)
    for parent, child, *rest in bom_rows:
        numerator, denominator, _pname, _cname, org = rest
        bom_edges[(parent, org)].append(
            (child, numerator / denominator, bom_skip[(parent, child, org)])
        )

    def is_make(code, org):
        if text(code) in FORCE_PURCHASED:
            return False
        # Never borrow another organization's production attribute.
        attr = item_attr.get(code, {}).get(org, "")
        return not attr or attr in MAKE_ATTRS

    stock_source_rows = list(enumerate(rows("备库需求.xlsx"), 2))

    # Order roots use the matching organization's material attribute.
    # Missing root attributes default to make. 未入库不展开 BOM。
    # 预警子件再按物料表和跳层决定，外购、外协不自制。
    order_items = set()
    produce = set()
    for code, org, ship, qty in rows("报警表.xlsx"):
        code = text(code)
        if code and num(qty) > 0:
            order_items.add(code)
            if is_make(code, text(org)):
                produce.add(code)
            item_name.setdefault(code, code)
    for _rownum, (code, org, _w1, _w2, _w3, total) in stock_source_rows:
        code = text(code)
        org = text(org)
        if code and num(total) > 0:
            order_items.add(code)
            if is_make(code, org):
                produce.add(code)
            item_name.setdefault(code, code)
    for row in unin_rows:
        code = row["code"]
        order_items.add(code)
        if is_make(code, row["org"]):
            produce.add(code)
        item_name.setdefault(code, row["name"])

    # 未入库不读 BOM，只保留订单本身。预警先看物料表：自制才自制，然后再看 BOM。
    # 跳层为假时，子件是否自制仍由物料表决定，不再往下拆。
    # 跳层为真时进入下一层，重复「物料表 → 跳层」。
    bom_make = set()
    external = set()
    bom_components = set()
    unin_produce = {
        (row["code"], row["org"]) for row in unin_rows if is_make(row["code"], row["org"])
    }
    alarm_produce = {
        (text(code), text(org))
        for code, org, _ship, qty in rows("报警表.xlsx")
        if text(code) and num(qty) > 0 and is_make(text(code), text(org))
    } if include_alarm_demands else set()
    stock_produce = {
        (text(code), text(org))
        for _rownum, (code, org, _w1, _w2, _w3, total) in stock_source_rows
        if text(code) and num(total) > 0 and is_make(text(code), text(org))
    }
    expanded = {context: True for context in unin_produce}
    selected_bom = set()
    selected_bom_quantity = defaultdict(float)

    def expand_alarm_node(producing_parent, code, org, factor=1.0, ancestors=None):
        """Flatten 跳层 nodes and select the first non-skipped components."""
        ancestors = ancestors or set()
        if code in ancestors:
            return
        for child, ratio, skip in bom_edges.get((code, org), []):
            if skip:
                expand_alarm_node(
                    producing_parent,
                    child,
                    org,
                    factor * ratio,
                    ancestors | {code},
                )
                continue
            edge = (producing_parent, child, org)
            selected_bom.add(edge)
            selected_bom_quantity[edge] += factor * ratio
            bom_components.add(child)
            if is_make(child, org):
                if child not in produce:
                    bom_make.add(child)
                expanded[(child, org)] = True
            elif child not in produce:
                external.add(child)

    for code, org in alarm_produce:
        expanded[(code, org)] = True
        expand_alarm_node(code, code, org)
    # 备库 follows the same organization-specific make/purchase and BOM
    # expansion rules as 预警. The generated component flows consume the
    # remaining real buffer inventory before dependent supply is created.
    for code, org in stock_produce:
        expanded[(code, org)] = True
        expand_alarm_node(code, code, org)

    produce |= bom_make
    external -= produce
    # FORCE_PURCHASED 与订单组织下的外购/外协：保证在 external 且 category 非自制，
    # 否则「不建工序 + 自制有限零库存」会把成品装配卡死。
    for code in FORCE_PURCHASED:
        if code in produce:
            continue
        if code in bom_components or code in order_items or code in item_name:
            external.add(code)
            item_category[code] = "产品外购"
    for code in list(external):
        if item_category.get(code) == "自制":
            attrs = set(item_attr.get(code, {}).values())
            if attrs & OUTSOURCE_ATTRS:
                item_category[code] = "产品外协"
            elif attrs & BUY_ATTRS:
                item_category[code] = next(iter(attrs & BUY_ATTRS))
            else:
                item_category[code] = "产品外购"
    route_scope = produce

    # Resolve only organizations actually used in the selected order/BOM paths.
    batch_values = defaultdict(set)
    snp_values = defaultdict(set)
    code_contexts = defaultdict(set)
    restricted = set()
    for code, org in expanded:
        code_contexts[code].add(org)
        value = item_batch.get(code, {}).get(org)
        if value is not None:
            batch_values[code].add(value)
        snp_values[code].add(item_snp.get(code, {}).get(org, 1))
        if item_lifecycle.get(code, {}).get(org) in {"备件", "断点", "断点件"}:
            restricted.add(code)
    for code, values in batch_values.items():
        if len(values) > 1:
            raise ValueError("Conflicting economic batches across organizations for %s: %s" % (code, sorted(values)))
    batch_by_code = {code: next(iter(values)) for code, values in batch_values.items()}
    snp_by_code = {}
    for code, values in snp_values.items():
        if any(v != int(v) for v in values):
            raise ValueError("SNP must be an integer for %s" % code)
        snp_by_code[code] = math.lcm(*(int(v) for v in values))
    operation_policies = {}

    for code in bom_make:
        item_name.setdefault(code, code)
        item_category[code] = "自制"

    # Only same 模具+分组编号 creates a co-production group.
    # 配套编号 describes a kit, not simultaneous manufacturing.
    group_members = defaultdict(list)
    for code, info in mold_info.items():
        # A single-cavity 1*1 mold produces each material independently,
        # regardless of a group number maintained in the source workbook.
        if info["cavity"].replace(" ", "") == "1*1":
            continue
        if info["group"]:
            group_members[(info["mold"], info["group"])].append(code)

    sync_groups = []  # list of {id, members, molds, primary}
    seen_sync = set()
    unin_codes = {code for code, _org in unin_produce}
    for (mold, group), members in sorted(group_members.items()):
        members = sorted(set(members))
        if len(members) < 2:
            continue
        if not (set(members) & route_scope):
            continue
        gid = "G_%s_%s" % (op_safe(mold), op_safe(group))
        if gid in seen_sync:
            continue
        seen_sync.add(gid)
        active_members = set(members) & route_scope
        order_members = active_members & unin_codes
        sync_groups.append(
            {
                "id": gid,
                "members": members,
                "molds": [mold],
                # The alternate parent item must be an active open-order
                # output. Alarm-only partners don't reliably trigger the
                # shared routing when the demand is on the other output.
                "primary": sorted(order_members or active_members)[0],
            }
        )

    # Co-products without a selected order/BOM path inherit only the group's
    # actual organizations, never an unrelated organization's attributes.
    for grp in sync_groups:
        orgs = set().union(*(code_contexts[c] for c in grp["members"]))
        for code in grp["members"]:
            if code_contexts[code]:
                continue
            values = {item_batch.get(code, {}).get(o) for o in orgs} - {None}
            if len(values) > 1:
                raise ValueError("Conflicting co-product economic batches for %s" % code)
            if values:
                batch_by_code[code] = next(iter(values))
            snps = [item_snp.get(code, {}).get(o, 1) for o in orgs]
            if any(v != int(v) for v in snps):
                raise ValueError("SNP must be an integer for %s" % code)
            snp_by_code[code] = math.lcm(*(int(v) for v in snps))
            if any(item_lifecycle.get(code, {}).get(o) in {"备件", "断点", "断点件"} for o in orgs):
                restricted.add(code)

    sync_of = {}
    for grp in sync_groups:
        for code in grp["members"]:
            sync_of[code] = grp
            item_name.setdefault(code, code)
            item_category.setdefault(code, "自制")

    item_codes = set(item_name) | set(stock) | {p for p, *_ in bom_rows} | {c for _, c, *_ in bom_rows} | produce | order_items | bom_make
    for grp in sync_groups:
        item_codes.update(grp["members"])
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
        for day, ws, _shift, start, end, count in shift_rows:
            if ws != workshop:
                continue
            add_bucket(buckets, person_cal, count, day, start, end)
            if count > 0:
                add_bucket(buckets, shift_cal, 100, day, start, end)

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
        peak = max(count for _day, ws, _shift, _s, _e, count in shift_rows if ws == workshop)
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
        molds_for = mold_map.get(code, [])
        if len(molds_for) <= 1:
            for mold in molds_for:
                operation_resources.append(op_resource(op_name, "MOLD_" + mold, 1, "模具", 1))
            return
        for mold in molds_for:
            operation_resources.append(op_resource(op_name, "MOLD_" + mold, 1, "模具_" + op_safe(mold), 1))

    def attach_molds_list(op_name, mold_list):
        mold_list = [m for m in mold_list if m]
        if len(mold_list) <= 1:
            for mold in mold_list:
                operation_resources.append(op_resource(op_name, "MOLD_" + mold, 1, "模具", 1))
            return
        for mold in mold_list:
            operation_resources.append(op_resource(op_name, "MOLD_" + mold, 1, "模具_" + op_safe(mold), 1))

    def known_members(token):
        return [m for m in machine_members(token) if m in machines]

    def personnel_workshop(workshop):
        """Map detailed machine workshops to a maintained personnel pool."""
        workshop = text(workshop)
        if workshop in workshops:
            return workshop
        return next(
            (
                parent
                for parent in workshops
                if workshop.startswith(parent + "-")
            ),
            None,
        )

    def attach_people(op_name, code, workshop):
        workshop = personnel_workshop(workshop)
        if workshop:
            operation_resources.append(
                op_resource(op_name, "PERSON_" + workshop, staffing.get(code, 1), "人员", 1)
            )

    def attach_people_list(op_name, code, workshop_list):
        seen = []
        for workshop in workshop_list:
            workshop = personnel_workshop(workshop)
            if workshop and workshop not in seen:
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

    def time_fields(code, workshops_used=None, batch=None):
        ct = duration_per.get(code)
        row = {"duration": None, "duration_per": ct if ct and ct > 0 else None}
        # Economic batch remains in fc_aps.batch_policy as reference data.
        # It isn't a hard or preferred lot size in short-horizon planning;
        # actual quantities follow SNP and lifecycle rules.
        return row

    def attach_people_for_code(op_name, code, workshops_used=None):
        """人员按工段：优先用机台所属工段，否则用未入库.生产车间。"""
        if workshops_used:
            attach_people_list(op_name, code, list(workshops_used))
            return
        hint = item_workshop.get(code)
        if hint:
            attach_people(op_name, code, hint)

    def add_end_materials(op_name, codes):
        for code in codes:
            operation_materials.append(
                {
                    "operation": op_name,
                    "item": code,
                    "location": LOCATION,
                    "quantity": 1,
                    "type": "end",
                    "priority": 1,
                    "source": SOURCE,
                }
            )

    def build_routing_for(codes, op_base, description, mold_list, primary):
        """Build time_per / alternate routing producing all codes as co-products.

        Returns leaf time_per names that carry end/BOM material flows.
        Alternate parents only select among children and must not hold materials.
        Either co-product can still launch the shared lot: Buffer producer
        discovery collapses sibling alternate leaves to their common owner, and
        convert aligns open member demands to one max(member-net) driver lot.
        """
        ranked = machine_priority.get(primary, {})
        # Prefer a member that actually has machine map.
        for code in codes:
            if machine_priority.get(code):
                ranked = machine_priority[code]
                break
        candidates = [m for m in ranked if known_members(m)]
        candidates.sort(key=lambda m: (ranked[m], m))
        staff_code = max(codes, key=lambda c: staffing.get(c, 1))
        batches = [batch_by_code[c] for c in codes if c in batch_by_code]
        batch = min(batches) if batches else None
        exact = bool(set(codes) & restricted)
        snp = math.lcm(*(int(snp_by_code.get(c, 1)) for c in codes))
        policy = {
            "economic": batch or 0,
            "snp": snp,
            "exact": exact,
            "coproduct": len(codes) > 1,
        }
        # Shot/piece time: use max duration among members (conservative).
        cts = [duration_per[c] for c in codes if duration_per.get(c)]
        if cts:
            duration_per[primary] = max(cts)

        # Assembly work centers and heat presses can trade staffing for time.
        # Enumerate whole-person alternatives; the shared personnel calendar
        # enforces the actual simultaneous availability (including zero shifts).
        flexible = {}
        for token in candidates:
            members = known_members(token)
            if len(members) != 1:
                continue
            machine = members[0]
            workshop, model = machines[machine]
            if (machine in ASSEMBLY_LINES or model == "热压") and cts and staffing.get(staff_code, 0) > 0:
                peak = peak_people.get(workshop, 0)
                # Both assembly lines and heat presses may use fewer people
                # with proportionally longer processing time, but must never
                # exceed the material's standard staffing value.  Using the
                # whole workshop peak here creates invalid overstaffed choices
                # and an excessively large alternate-operation graph.
                peak = min(peak, math.floor(staffing[staff_code]))
                if peak > 0:
                    flexible[token] = list(range(peak, 0, -1))

        if not candidates:
            no_equipment.append(primary)
            extras = time_fields(primary, {item_workshop[primary]} if item_workshop.get(primary) else set(), batch)
            operations.append(
                {
                    "name": op_base,
                    "type": "time_per",
                    "description": description,
                    "item": primary,
                    "location": LOCATION,
                    "priority": 1,
                    "sizeminimum": 1,
                    "source": SOURCE,
                    **extras,
                }
            )
            attach_people_for_code(op_base, staff_code)
            attach_molds_list(op_base, mold_list)
            add_end_materials(op_base, codes)
            operation_policies[op_base] = policy
            return [op_base]

        if len(candidates) <= 1 and not flexible:
            token = candidates[0]
            workshops_used = workshops_for_token(token)
            extras = time_fields(primary, workshops_used, batch)
            operations.append(
                {
                    "name": op_base,
                    "type": "time_per",
                    "description": description,
                    "item": primary,
                    "location": LOCATION,
                    "priority": 1,
                    "sizeminimum": 1,
                    "source": SOURCE,
                    **extras,
                }
            )
            attach_equipment(op_base, staff_code, token)
            attach_molds_list(op_base, mold_list)
            add_end_materials(op_base, codes)
            operation_policies[op_base] = policy
            return [op_base]

        operations.append(
            {
                "name": op_base,
                "type": "alternate",
                "description": description,
                "item": primary,
                "location": LOCATION,
                "priority": 1,
                "search": "PRIORITY",
                "sizeminimum": 1,
                "source": SOURCE,
            }
        )
        leaves = []
        choices = [(token, people) for token in candidates for people in flexible.get(token, [None])]
        for token, people in choices:
            child = op_base + "__" + op_suffix(token) + ("__P%d" % people if people is not None else "")
            extras = time_fields(primary, workshops_for_token(token), batch)
            if people is not None:
                ratio = staffing[staff_code] / people
                extras["duration_per"] *= ratio
            operations.append(
                {
                    "name": child,
                    "type": "time_per",
                    "description": "%s @ %s%s" % (description, token, " / %d人" % people if people is not None else ""),
                    "item": None,
                    "location": LOCATION,
                    "owner": op_base,
                    "priority": ranked[token] * 1000 + (max(flexible[token]) - people if people is not None else 0),
                    "sizeminimum": 1,
                    "source": SOURCE,
                    **extras,
                }
            )
            resource_start = len(operation_resources)
            attach_equipment(child, staff_code, token)
            if people is not None:
                for load in operation_resources[resource_start:]:
                    if load["resource"].startswith("PERSON_"):
                        load["quantity"] = people
            attach_molds_list(child, mold_list)
            add_end_materials(child, codes)
            leaves.append(child)
            operation_policies[child] = policy
        return leaves

    no_equipment = []
    producing_ops = {}
    producing_leaves = {}
    handled = set()

    # 1) Co-produce sync groups (模具 + 分组编号 only).
    for grp in sync_groups:
        members = grp["members"]
        primary = grp["primary"]
        op_base = "FC_OP_SYNC_" + grp["id"]
        desc = "联产:" + "+".join(item_name.get(c, c) for c in members[:4])
        if len(members) > 4:
            desc += "…"
        leaves = build_routing_for(members, op_base, desc, grp["molds"], primary)
        for code in members:
            producing_ops[code] = op_base
            producing_leaves[code] = leaves
            handled.add(code)

    # 2) Remaining open-order make items (solo routings).
    for code in sorted(produce):
        if code in handled:
            continue
        name = item_name.get(code, code)
        op_base = "FC_OP_" + code
        leaves = build_routing_for([code], op_base, name, mold_map.get(code, []), code)
        producing_ops[code] = op_base
        producing_leaves[code] = leaves
        handled.add(code)

    for (parent, child, org), quantity in selected_bom_quantity.items():
        if parent not in producing_leaves:
            continue
        for leaf in producing_leaves[parent]:
            operation_materials.append(
                {
                    "operation": leaf,
                    "item": child,
                    "location": LOCATION,
                    "quantity": -quantity,
                    "type": "start",
                    "priority": 1,
                    "source": SOURCE,
                }
            )

    # A co-production operation carries the BOM of every output.  When two
    # outputs consume the same component, the standard data model permits a
    # single flow for that operation/item/effective-date key.  Sum those BOM
    # coefficients before import; otherwise update_or_create silently keeps
    # only the last member's coefficient and understates joint consumption.
    consolidated_materials = {}
    for flow in operation_materials:
        key = (
            flow["operation"], flow["item"], flow.get("location"),
            flow.get("type", "start"), flow.get("effective_start"),
        )
        existing = consolidated_materials.get(key)
        if existing is None:
            consolidated_materials[key] = dict(flow)
        else:
            existing["quantity"] = num(existing.get("quantity")) + num(flow.get("quantity"))
            if flow.get("quantity_fixed") is not None:
                existing["quantity_fixed"] = (
                    num(existing.get("quantity_fixed")) + num(flow.get("quantity_fixed"))
                )
    operation_materials = list(consolidated_materials.values())

    alarm_demands = []
    alarm_stock_used = 0.0
    alarm_production_required = 0.0
    alarm_available = defaultdict(float, alarm_atp)
    alarm_source_rows = list(enumerate(rows("报警表.xlsx"), 2))
    alarm_source_rows.sort(key=lambda entry: (iso_datetime(entry[1][2]) or "", entry[0]))
    for rownum, (code, org, ship, qty) in alarm_source_rows:
        code = text(code)
        requested = num(qty)
        if code not in item_codes or requested <= 0:
            continue
        stock_key = (code, text(org))
        used = min(requested, max(0.0, alarm_available[stock_key])) if include_alarm_demands else 0.0
        remaining = requested - used if include_alarm_demands else requested
        alarm_available[stock_key] -= used
        alarm_stock_used += used
        alarm_production_required += remaining if include_alarm_demands else 0.0
        alarm_demands.append(
            {
                "name": "FC_ALARM_%s_%s_%s_R%05d" % (org, code, str(ship).replace("/", ""), rownum),
                "category": "预警",
                "description": (
                    "报警需求 原始%s 库存抵扣%s 待生产%s" % (requested, used, remaining)
                    if include_alarm_demands else None
                ),
                "customer": "FC_CUSTOMER",
                "item": code,
                "location": LOCATION,
                "due": iso_datetime(ship),
                "status": "open" if include_alarm_demands and remaining > 1e-9 else "closed",
                "quantity": remaining if include_alarm_demands else requested,
                "priority": 10,
                "source": SOURCE,
            }
        )
    if include_alarm_demands:
        unin_codes_for_stock = {row["code"] for row in unin_rows}
        remaining_by_code = defaultdict(float)
        for (code, _org), available in alarm_available.items():
            remaining_by_code[code] += max(0.0, available)
        for code, available in remaining_by_code.items():
            if code not in unin_codes_for_stock:
                stock[code] = max(0.0, available)

    buffers = []
    for code in sorted(item_codes):
        buffers.append(
            {
                "item": code,
                "location": LOCATION,
                "batch": "",
                "onhand": stock.get(code, 0),
                # 自制 must stay finite so FAS/CP can enforce material timing.
                # Only true external (non-make) codes without a producing leaf
                # use infinite supply.
                "type": (
                    "infinite"
                    if (
                        code in external
                        and code not in producing_leaves
                        and item_category.get(code) != "自制"
                    )
                    else "default"
                ),
                "source": SOURCE,
            }
        )

    demands = list(alarm_demands)
    for rownum, row in enumerate(unin_rows, 2):
        code = row["code"]
        if code not in item_codes or row["qty"] <= 0:
            continue
        order = row["order"] or "NA"
        due = iso_datetime(row["end"]) or datetime.combine(
            planning_start.date() + timedelta(days=8), datetime.min.time()
        ).isoformat()
        end_dt = None
        if isinstance(row["end"], datetime):
            end_dt = row["end"]
        elif due:
            try:
                end_dt = datetime.fromisoformat(due)
            except ValueError:
                end_dt = None
        if end_dt is not None and end_dt < planning_start:
            due = datetime.combine(
                planning_start.date() + timedelta(days=1), datetime.min.time()
            ).isoformat()
        demands.append(
            {
                "name": "FC_UNIN_%s_%s_%s_R%05d" % (row["org"] or "NA", code, order, rownum),
                "category": "未入库",
                "description": "未入库待排 %s %s" % (order, row["machine"] or row["workshop"] or ""),
                "customer": "FC_CUSTOMER",
                "item": code,
                "location": LOCATION,
                "due": due,
                "status": "open",
                "quantity": row["qty"],
                "priority": 1,
                "source": SOURCE,
            }
        )
    for rownum, (code, org, w1, w2, w3, total) in stock_source_rows:
        code = text(code)
        if code not in item_codes or num(total) <= 0:
            continue
        demands.append(
            {
                "name": "FC_STOCK_%s_%s_R%05d" % (org, code, rownum),
                "category": "备库",
                "description": "备库需求 %s" % (org or ""),
                "customer": "FC_CUSTOMER",
                "item": code,
                "location": LOCATION,
                "due": datetime.combine(
                    planning_start.date() + timedelta(days=20), datetime.min.time()
                ).isoformat(),
                # Closed here; scenario builders reopen as idle-capacity filler
                # with priority after 预警/未入库.
                "status": "closed",
                "quantity": num(total),
                "priority": 100000,
                "source": SOURCE,
            }
        )

    items = [
        {"name": code, "description": item_name.get(code, code), "category": item_category.get(code), "source": SOURCE}
        for code in sorted(item_codes)
    ]
    # Net requirements over the selected manufacturing BOM. These are hard
    # production ceilings for spare/discontinued items, including co-products.
    edges = defaultdict(dict)
    indegree = defaultdict(int)
    for (parent, child, _org), quantity in selected_bom_quantity.items():
        if parent in producing_leaves:
            edges[parent][child] = edges[parent].get(child, 0) + quantity
    nodes = set(producing_leaves)
    for parent, children in edges.items():
        nodes.add(parent)
        for child in children:
            nodes.add(child)
            indegree[child] += 1
    requirements = defaultdict(float)
    for row in unin_rows:
        requirements[row["code"]] += row["qty"]
        nodes.add(row["code"])
    # Alarm finished-goods demand must also drive lifecycle ceilings down the
    # BOM. Otherwise a spare/breakpoint component with no independent order
    # gets limit=0 and cannot be made even when its parent still needs it.
    if include_alarm_demands:
        for demand in demands:
            if demand.get("category") not in {"预警", "备库"}:
                continue
            if demand.get("status") != "open":
                # Base conversion keeps 备库 closed; scenario builders reopen
                # it after this policy is built, so include its requirement.
                if demand.get("category") != "备库":
                    continue
            qty = float(demand.get("quantity") or 0)
            if qty <= 1e-9:
                continue
            code = demand["item"]
            requirements[code] += qty
            nodes.add(code)
    ready = sorted(c for c in nodes if not indegree[c])
    limits = {}
    # A 1+1 co-production group can create a lifecycle-restricted partner
    # with no independent demand.  That output is physically unavoidable:
    # keep exact sizing (no upward rounding), but don't let its zero demand
    # block production of the demanded partner.  Solo restricted items keep
    # a hard ceiling only when there is real net demand — never write limit=0,
    # which would freeze dependent BOM supply for upper-level orders.
    coproduct_codes = {
        code for grp in sync_groups for code in grp["members"]
    }
    visited = 0
    while ready:
        code = ready.pop()
        visited += 1
        net = max(0, requirements[code] - stock.get(code, 0))
        if (
            code in restricted
            and code in producing_leaves
            and code not in coproduct_codes
            and net > 1e-9
        ):
            limits[code] = net
        quantity = net
        if code not in restricted and net:
            econ = batch_by_code.get(code, 0)
            snp = snp_by_code.get(code, 1)
            tail = net % econ if econ else net
            quantity = net - tail + math.ceil(max(0, tail / snp - 1e-9)) * snp
        for child, ratio in edges.get(code, {}).items():
            requirements[child] += quantity * ratio
            indegree[child] -= 1
            if not indegree[child]:
                ready.append(child)
    if visited != len(nodes):
        raise ValueError("Cyclic BOM prevents lifecycle production-limit calculation")

    # Co-mold sync for 预警 only here. 未入库 keeps scenario-builder 1+1
    # mold grouping (same max-side rule; 共模多产允许), then a final align.
    align_sync_coproduct_demands(
        {
            "buffers": buffers,
            "demands": demands,
            "operations": operations,
            "operationMaterials": operation_materials,
        },
        classes=("预警",),
    )

    # Resource assignments are only considered complete when an operation has
    # a personnel load. If personnel isn't mapped, discard any partial machine
    # or mold mapping as well and let the operation run as fully unassigned.
    resource_operations = {row["operation"] for row in operation_resources}
    personnel_operations = {
        row["operation"]
        for row in operation_resources
        if row["resource"].startswith("PERSON_")
    }
    fully_unassigned_operations = resource_operations - personnel_operations
    operation_resources = [
        row
        for row in operation_resources
        if row["operation"] not in fully_unassigned_operations
    ]

    # Estimate labor content per finished unit over the complete manufacturing
    # tree. Alternate routings are ordered by preference, so use the first
    # leaf as the representative route for demand ranking. This estimate only
    # changes MRP sequencing; the solver still chooses the actual resource.
    operation_by_name = {row["name"]: row for row in operations}
    resources_by_operation = defaultdict(list)
    for row in operation_resources:
        if row["resource"].startswith("PERSON_"):
            resources_by_operation[row["operation"]].append(row)
    own_labor_seconds = {}
    for code, leaves in producing_leaves.items():
        labor = defaultdict(float)
        if leaves:
            leaf = leaves[0]
            operation = operation_by_name.get(leaf, {})
            seconds = num(operation.get("duration")) + num(
                operation.get("duration_per")
            )
            for load in resources_by_operation.get(leaf, []):
                labor[load["resource"]] += seconds * abs(num(load.get("quantity"), 1))
        own_labor_seconds[code] = labor

    labor_memo = {}

    def manufacturing_labor(code):
        if code in labor_memo:
            return labor_memo[code]
        total = defaultdict(float, own_labor_seconds.get(code, {}))
        labor_memo[code] = total
        for child, ratio in edges.get(code, {}).items():
            for resource, seconds in manufacturing_labor(child).items():
                total[resource] += ratio * seconds
        return total

    for code in producing_leaves:
        manufacturing_labor(code)

    # Item-level policy drives the generic finite-window fill pass.  Members
    # of a co-production group share the routing's common SNP.  Lifecycle
    # items remain exact when planned alone; unavoidable co-product excess is
    # allowed to remain in inventory and is therefore not given a hard limit.
    item_policies = {
        code: {
            "snp": max(1, int(snp_by_code.get(code, 1))),
            "economic": float(batch_by_code.get(code) or 0),
            "exact": code in restricted,
        }
        for code in item_codes
    }
    for grp in sync_groups:
        group_snp = math.lcm(
            *(int(snp_by_code.get(code, 1)) for code in grp["members"])
        )
        for code in grp["members"]:
            item_policies.setdefault(code, {})["snp"] = max(1, group_snp)
            item_policies[code]["exact"] = code in restricted
            if not item_policies[code].get("economic"):
                item_policies[code]["economic"] = float(batch_by_code.get(code) or 0)

    # Static dependency value for the rolling MRP demand selector.  The
    # actual readiness and remaining quantity are recalculated after every
    # planned lot; this metadata only describes how many open order lines can
    # benefit from a root's manufacturing tree and how deep that tree is.
    root_line_count = Counter(row["code"] for row in unin_rows)

    def manufacturing_tree(root):
        descendants = set()
        max_depth = 0
        stack = [(root, 0)]
        while stack:
            current, depth = stack.pop()
            if current in descendants:
                continue
            descendants.add(current)
            max_depth = max(max_depth, depth)
            stack.extend((child, depth + 1) for child in edges.get(current, {}))
        return descendants, max_depth

    root_trees = {
        code: manufacturing_tree(code)
        for code in root_line_count
    }
    component_order_lines = Counter()
    for root, (tree, _depth) in root_trees.items():
        for component in tree:
            component_order_lines[component] += root_line_count[root]
    rolling_items = {}
    for root, (tree, depth) in root_trees.items():
        rolling_items[root] = {
            "unlock_orders": max(
                (component_order_lines[component] for component in tree),
                default=root_line_count[root],
            ),
            "dependency_links": sum(
                component_order_lines[component] for component in tree
            ),
            "bom_depth": depth,
            "labor_hours_per_unit": {
                resource: round(seconds / 3600.0, 8)
                for resource, seconds in manufacturing_labor(root).items()
                if seconds > 0
            },
        }

    batch_policy = {
        "version": 3,
        "operations": operation_policies,
        "items": item_policies,
        "limits": limits,
        "rolling": {
            "enabled": False,
            "parts": 1,
            "items": rolling_items,
        },
        "objective_ordering": {
            # Lot shaping is controlled only by parameter plan.lot_split /
            # cp.lot_split (not by flags in this JSON).
            "enabled": True,
            "due_bucket_hours": 12,
            "complete_orders_first": True,
            "use_economic_batch": True,
            "exact_split_over_hours": 8,
            "window_split_item_prefixes": ["01.52"],
            "window_split_resources": ["PERSON_制造部2工段", "2工段装配线"],
            "pull_fillers_into_window": True,
            "preserve_demand_priority": True,
        },
    }
    return {
        "clean_all": True,
        "parameters": [
            {"name": "currentdate", "value": planning_start.isoformat(sep=" ")},
            {"name": "plan.solver", "value": "cp"},
            {"name": "cp.planning_mode", "value": "throughput"},
            {"name": "cp.horizon_days", "value": "3"},
            {"name": "cp.material_mode", "value": "selfmade"},
            {"name": "plan.fixBrokenSupplyPath", "value": "false"},
            {"name": "plan.lot_split", "value": "0"},
            {"name": "cp.lot_split", "value": "0"},
            {
                "name": "fc_aps.priority_window_end",
                "value": (planning_start + timedelta(days=3)).isoformat(),
            },
            {"name": "fc_aps.batch_policy", "value": json.dumps(batch_policy, ensure_ascii=False)},
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
            "bom_make_produced": len(bom_make),
            "bom_external_real_stock": len(external),
            "bom_outsource_stocked": len(external),
            "bom_components_stocked": len(bom_components),
            "sync_groups": len(sync_groups),
            "unin_rows": len(unin_rows),
            "unin_qty": sum(r["qty"] for r in unin_rows),
            "alarm_stock_used": alarm_stock_used,
            "alarm_production_required": alarm_production_required,
            "fully_unassigned_operations": len(fully_unassigned_operations),
            "no_equipment": [
                {"code": code, "name": item_name.get(code, code), "workshop": item_workshop.get(code), "has_mold": bool(mold_map.get(code)), "has_ct": code in duration_per}
                for code in no_equipment
            ]
        },
    }


def main() -> int:
    """Legacy CLI; prefer ``python build_fc_dataset.py --profile base``."""
    from build_fc_dataset import build_base_dataset

    out = ROOT / "fc_aps_scenario6.json"
    build_base_dataset(ROOT, output=out, include_alarm_demands=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Simple FC source-table relation checks (pre-import).

Covers: BOM links, resources, CT, make/buy, batch/SNP, inventory,
BOM ratios, and historical unfinished orders vs current routing rules.

Does **not** run solvers. Prefer::

    python build_fc_dataset.py --source-dir DIR --profile check
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import convert_fc_aps as converter
from convert_fc_aps import (
    ASSEMBLY_LINES,
    BUY_ATTRS,
    EXTERNAL_ATTRS,
    FORCE_PURCHASED,
    MAKE_ATTRS,
    OUTSOURCE_ATTRS,
    economic_batch,
    machine_members,
    num,
    rows,
    split_machines,
    text,
)

ROOT = Path(__file__).resolve().parent


def _load(source_dir: Path):
    converter.ROOT = source_dir.resolve()
    items = {}  # code -> {name, attrs{org}, batch, snp, lifecycle}
    for code, org, name, lifecycle, attr, econ, _jit, snp, *_ in rows("物料.xlsx"):
        code = text(code)
        if not code:
            continue
        row = items.setdefault(
            code,
            {
                "name": text(name) or code,
                "attrs": {},
                "batches": {},
                "snps": {},
                "lifecycles": {},
            },
        )
        if text(name):
            row["name"] = text(name)
        org_s = text(org)
        if org_s:
            row["attrs"][org_s] = text(attr)
            row["batches"][org_s] = economic_batch(econ, snp)
            row["snps"][org_s] = max(1, int(num(snp)) if num(snp) >= 1 else 1)
            row["lifecycles"][org_s] = text(lifecycle)

    machines = {}
    for name, model, workshop in rows("机台.xlsx"):
        name = text(name)
        if name:
            machines[name] = text(workshop)
    for line, workshop in ASSEMBLY_LINES.items():
        machines.setdefault(line, workshop)

    molds = set()
    for mold, *_ in rows("模具.xlsx"):
        mold = text(mold)
        if mold:
            molds.add(mold)

    std = {}
    for code, ct, output, people, cavities, single_ct, *_ in rows("标准工时.xlsx"):
        code = text(code)
        if code:
            std[code] = {
                "ct": num(ct),
                "single_ct": num(single_ct),
                "output": num(output),
                "people": num(people, 1),
                "cavities": text(cavities),
            }

    machine_map = {}
    for code, rated, spare, primary in rows("物料机台对照表.xlsx"):
        code = text(code)
        if not code:
            continue
        tokens = []
        for raw in (rated, spare, primary):
            tokens.extend(split_machines(raw))
        machine_map[code] = list(dict.fromkeys(tokens))

    mold_map = {}
    for code, mold, cavity, group, kit, *_ in rows("物料模具对照表.xlsx"):
        code, mold = text(code), text(mold)
        if code and mold:
            mold_map.setdefault(code, []).append(
                {"mold": mold, "cavity": text(cavity), "group": text(group), "kit": text(kit)}
            )
            molds.add(mold)

    stock = {}
    for code, org, immediate, finished, unshipped in rows("库存.xlsx"):
        code, org = text(code), text(org)
        if not code:
            continue
        key = (code, org)
        imm, fin, uns = num(immediate), num(finished), num(unshipped)
        stock[key] = {
            "immediate": imm,
            "finished_unin": fin,
            "unshipped": uns,
            "atp": imm + fin - uns,
        }

    bom = []
    for parent, pname, child, cname, numerator, denominator, org, skip, *_ in rows("BOM.xlsx"):
        parent, child = text(parent), text(child)
        if not parent or not child or (parent == "父项编码" and child == "子项编码"):
            continue
        bom.append(
            {
                "parent": parent,
                "pname": text(pname),
                "child": child,
                "cname": text(cname),
                "num": num(numerator, 1),
                "den": num(denominator, 1),
                "org": text(org),
                "skip": text(skip).lower() in {"true", "1", "是"},
            }
        )

    unin = []
    for row in rows("未入库.xlsx"):
        cells = list(row) + [None] * 10
        code, name, qty, org, plan_qty, start, end, workshop, machine, order = cells[:10]
        code = text(code)
        qty = num(qty)
        if code and qty > 0:
            unin.append(
                {
                    "code": code,
                    "name": text(name) or code,
                    "qty": qty,
                    "org": text(org),
                    "plan_qty": num(plan_qty),
                    "start": start,
                    "end": end,
                    "workshop": text(workshop),
                    "machine": text(machine),
                    "order": text(order),
                }
            )

    alarm = []
    for code, org, due, qty, *_ in rows("报警表.xlsx"):
        code = text(code)
        if code and num(qty) > 0:
            alarm.append({"code": code, "org": text(org), "due": due, "qty": num(qty)})

    arrivals = []
    for code, org, day, qty, *_ in rows("物料到货计划.xlsx"):
        code = text(code)
        if code and num(qty) > 0:
            arrivals.append({"code": code, "org": text(org), "day": day, "qty": num(qty)})

    return {
        "items": items,
        "machines": machines,
        "molds": molds,
        "std": std,
        "machine_map": machine_map,
        "mold_map": mold_map,
        "stock": stock,
        "bom": bom,
        "unin": unin,
        "alarm": alarm,
        "arrivals": arrivals,
    }


def _make_buy(attr: str) -> str:
    if attr in MAKE_ATTRS:
        return "自制"
    if attr in OUTSOURCE_ATTRS:
        return "外协"
    if attr in BUY_ATTRS or attr in EXTERNAL_ATTRS:
        return "外购"
    if not attr:
        return "未填"
    return attr


def _primary_attr(item: dict) -> str:
    attrs = [a for a in item["attrs"].values() if a]
    if not attrs:
        return ""
    # Prefer 自制 if any org says make.
    for a in attrs:
        if a in MAKE_ATTRS:
            return a
    return attrs[0]


def check_dataset(source_dir: Path) -> dict:
    """Return structured findings + markdown report."""
    data = _load(source_dir)
    items, machines = data["items"], data["machines"]
    std, machine_map, mold_map = data["std"], data["machine_map"], data["mold_map"]
    stock, bom, unin = data["stock"], data["bom"], data["unin"]
    alarm, arrivals = data["alarm"], data["arrivals"]

    item_codes = set(items)
    demand_codes = {r["code"] for r in unin} | {r["code"] for r in alarm}
    bom_parents = {r["parent"] for r in bom}

    findings: dict[str, list] = defaultdict(list)

    # ---- 1) BOM 关联 ----
    for r in bom:
        if r["parent"] not in item_codes:
            findings["bom_parent_missing_item"].append(r["parent"])
        if r["child"] not in item_codes:
            findings["bom_child_missing_item"].append(r["child"])
        if r["den"] <= 0:
            findings["bom_bad_denominator"].append(
                f"{r['parent']}→{r['child']} den={r['den']}"
            )
        if r["num"] <= 0:
            findings["bom_bad_numerator"].append(
                f"{r['parent']}→{r['child']} num={r['num']}"
            )

    # 自制且出现在需求/BOM父项，应有 BOM（未入库成品可不展开，标为提示）
    def _is_make(code: str) -> bool:
        it = items.get(code) or {"attrs": {}}
        return _make_buy(_primary_attr(it)) == "自制"

    make_demand = {c for c in demand_codes if _is_make(c)} - set(FORCE_PURCHASED)
    for code in sorted(make_demand):
        if code not in bom_parents and code in {r["code"] for r in alarm}:
            findings["alarm_make_no_bom"].append(code)

    # ---- 2) 资源关系 ----
    unknown_machine_tokens = []
    for code, tokens in machine_map.items():
        for token in tokens:
            members = machine_members(token) or [token]
            for m in members:
                if m not in machines and m not in ASSEMBLY_LINES:
                    unknown_machine_tokens.append(f"{code}:{m}")
    findings["machine_not_in_机台表"] = sorted(set(unknown_machine_tokens))[:50]

    mold_master = {text(mold) for mold, *_ in rows("模具.xlsx") if text(mold)}
    unknown_molds = []
    for code, rows_m in mold_map.items():
        for info in rows_m:
            if info["mold"] not in mold_master:
                unknown_molds.append(f"{code}:{info['mold']}")
    findings["mold_not_in_模具表"] = sorted(set(unknown_molds))[:50]

    make_need_route = sorted(make_demand | ({r["code"] for r in unin if _is_make(r["code"])} - set(FORCE_PURCHASED)))
    for code in make_need_route:
        if code not in machine_map and code not in ASSEMBLY_LINES:
            # unin may carry concrete machine
            unin_machines = [r["machine"] for r in unin if r["code"] == code and r["machine"]]
            if not unin_machines:
                findings["make_no_machine_map"].append(code)
        if code not in mold_map and code not in std:
            findings["make_no_mold_and_no_ct"].append(code)

    # ---- 3) 工时 ----
    for code in make_need_route:
        if code in FORCE_PURCHASED:
            continue
        s = std.get(code)
        if not s:
            findings["make_missing_ct"].append(code)
        elif s["single_ct"] <= 0 and s["ct"] <= 0:
            findings["make_zero_ct"].append(code)

    # ---- 4) 自制/外采 ----
    attr_dist = Counter()
    for code, it in items.items():
        attr_dist[_make_buy(_primary_attr(it))] += 1
    forced = sorted(c for c in FORCE_PURCHASED if c in item_codes or c in demand_codes)

    # ---- 5) 经济批量 / SNP ----
    batch_conflict = []
    snp_conflict = []
    has_econ = 0
    for code, it in items.items():
        batches = {v for v in it["batches"].values() if v and v > 1}
        snps = set(it["snps"].values())
        if len(batches) > 1:
            batch_conflict.append(f"{code}:{sorted(batches)}")
        if len(snps) > 1:
            snp_conflict.append(f"{code}:{sorted(snps)}")
        if any(v and v > 1 for v in it["batches"].values()):
            has_econ += 1
    findings["batch_org_conflict"] = batch_conflict[:30]
    findings["snp_org_conflict"] = snp_conflict[:30]

    # ---- 6) 库存 ----
    neg_atp = [
        f"{c}@{o}: ATP={v['atp']:.0f} (即时{v['immediate']:.0f}+未入{v['finished_unin']:.0f}-待发{v['unshipped']:.0f})"
        for (c, o), v in stock.items()
        if v["atp"] < -1e-6
    ]
    findings["negative_atp"] = neg_atp[:40]
    stock_codes = {c for c, _ in stock}
    unin_zeroed = sorted({r["code"] for r in unin} & stock_codes)

    # ---- 7) 投入产出比例 ----
    ratio_extreme = []
    for r in bom:
        ratio = r["num"] / r["den"] if r["den"] else 0
        if ratio <= 0 or ratio > 1000 or (ratio < 0.001 and ratio > 0):
            ratio_extreme.append(
                f"{r['parent']}→{r['child']} {r['num']}/{r['den']}={ratio:.6g}"
            )
    findings["bom_ratio_extreme"] = ratio_extreme[:40]

    cavity_odd = []
    for code, rows_m in mold_map.items():
        for info in rows_m:
            cav = info["cavity"]
            if cav and ("*" not in cav and "+" not in cav and not cav.isdigit()):
                cavity_odd.append(f"{code}:{cav}")
    findings["cavity_unusual"] = sorted(set(cavity_odd))[:30]

    # ---- 8) 历史排单（未入库 = 在制/历史计划）----
    hist_machine_mismatch = []
    hist_workshop_mismatch = []
    hist_plan_qty_diff = []
    adjustable = []
    for r in unin:
        code = r["code"]
        hist_m = r["machine"]
        mapped = machine_map.get(code, [])
        mapped_members = set()
        for token in mapped:
            mapped_members.update(machine_members(token) or [token])
            mapped_members.add(token)
        if hist_m:
            hist_members = set(machine_members(hist_m) or [hist_m])
            hist_members.add(hist_m)
            # note-like workshop words are not machines
            if hist_m not in {"装配", "焊接", "调试"} and mapped_members:
                if hist_members.isdisjoint(mapped_members) and hist_m not in ASSEMBLY_LINES:
                    hist_machine_mismatch.append(
                        f"{r['order']}:{code} 历史机台={hist_m} 对照={','.join(mapped[:3]) or '-'}"
                    )
            if mapped_members:
                adjustable.append(
                    {
                        "order": r["order"],
                        "code": code,
                        "hist_machine": hist_m,
                        "allowed_machines": sorted(mapped_members)[:12],
                        "qty": r["qty"],
                    }
                )
        if r["workshop"] and code in machine_map:
            # workshop vs machine master
            for token in machine_map[code]:
                for m in machine_members(token) or [token]:
                    ws = machines.get(m) or ASSEMBLY_LINES.get(m)
                    if ws and r["workshop"] and ws != r["workshop"] and m == hist_m:
                        hist_workshop_mismatch.append(
                            f"{r['order']}:{code} 历史车间={r['workshop']} 机台车间={ws}"
                        )
        if r["plan_qty"] > 0 and abs(r["plan_qty"] - r["qty"]) > 1e-6:
            hist_plan_qty_diff.append(
                f"{r['order']}:{code} 计划={r['plan_qty']:.0f} 未入库={r['qty']:.0f}"
            )

    findings["hist_machine_vs_map"] = hist_machine_mismatch[:40]
    findings["hist_workshop_vs_machine"] = hist_workshop_mismatch[:40]
    findings["hist_plan_vs_unin_qty"] = hist_plan_qty_diff[:40]

    # ---- summary counts ----
    summary = {
        "source_dir": str(source_dir.resolve()),
        "counts": {
            "items": len(items),
            "bom_edges": len(bom),
            "machines": len(machines),
            "molds_master": len(mold_master),
            "std_hours": len(std),
            "machine_map": len(machine_map),
            "mold_map": len(mold_map),
            "stock_rows": len(stock),
            "unin_rows": len(unin),
            "alarm_rows": len(alarm),
            "arrival_rows": len(arrivals),
            "make_buy_dist": dict(attr_dist),
            "force_purchased_hit": len(forced),
            "items_with_econ_batch": has_econ,
        },
        "issue_counts": {k: len(v) for k, v in findings.items() if v},
        "force_purchased": forced,
        "adjustable_unin_sample": adjustable[:20],
        "findings": {k: v for k, v in findings.items() if v},
    }

    md = _to_markdown(summary)
    summary["markdown"] = md
    return summary


def _to_markdown(summary: dict) -> str:
    c = summary["counts"]
    ic = summary["issue_counts"]
    lines = [
        "# FC 源表关系检查",
        "",
        f"- 目录: `{summary['source_dir']}`",
        f"- 物料 {c['items']} / BOM边 {c['bom_edges']} / 机台 {c['machines']} / 模具主数据 {c['molds_master']}",
        f"- 标准工时 {c['std_hours']} / 机台对照 {c['machine_map']} / 模具对照 {c['mold_map']}",
        f"- 库存行 {c['stock_rows']} / 未入库 {c['unin_rows']} / 报警 {c['alarm_rows']} / 到货计划 {c['arrival_rows']}",
        f"- 生产属性分布: {c['make_buy_dist']}",
        f"- 经济批量>1 的物料: {c['items_with_econ_batch']}；FORCE_PURCHASED 命中: {c['force_purchased_hit']}",
        "",
        "## 问题汇总",
        "",
    ]
    if not ic:
        lines.append("未发现上述规则下的阻断项（仍建议人工抽查）。")
    else:
        for k, n in sorted(ic.items(), key=lambda x: -x[1]):
            lines.append(f"- **{k}**: {n}")
    lines += ["", "## 分项（最多列样例）", ""]

    titles = {
        "bom_parent_missing_item": "1. BOM 父项不在物料表",
        "bom_child_missing_item": "1. BOM 子项不在物料表",
        "bom_bad_denominator": "1. BOM 分母异常",
        "bom_bad_numerator": "1. BOM 分子异常",
        "alarm_make_no_bom": "1. 报警自制件无 BOM（展开断链风险）",
        "machine_not_in_机台表": "2. 对照机台不在机台表",
        "mold_not_in_模具表": "2. 对照模具不在模具表",
        "make_no_machine_map": "2. 自制需求无机台对照（且未入库未带机台）",
        "make_no_mold_and_no_ct": "2. 自制无模具对照且无标准工时",
        "make_missing_ct": "3. 自制缺标准工时",
        "make_zero_ct": "3. 自制 CT/单个CT 均为 0",
        "batch_org_conflict": "5. 经济批量跨组织冲突",
        "snp_org_conflict": "5. SNP 跨组织冲突",
        "negative_atp": "6. ATP 为负（即时+已完工未入库-未发货）",
        "bom_ratio_extreme": "7. 投入产出比例异常",
        "cavity_unusual": "7. 模穴写法异常",
        "hist_machine_vs_map": "8. 历史未入库机台 ∉ 当前机台对照（可调范围外）",
        "hist_workshop_vs_machine": "8. 历史车间与机台所属车间不一致",
        "hist_plan_vs_unin_qty": "8. 计划数量 ≠ 未入库数量",
    }
    findings = summary.get("findings") or {}
    for key, title in titles.items():
        rows_f = findings.get(key) or []
        if not rows_f:
            continue
        lines.append(f"### {title} ({len(rows_f)})")
        for x in rows_f[:15]:
            lines.append(f"- `{x}`")
        if len(rows_f) > 15:
            lines.append(f"- … 另有 {len(rows_f) - 15} 条")
        lines.append("")

    lines += [
        "### 4. 自制 / 外采",
        f"- 分布: {c['make_buy_dist']}",
        f"- 强制外采清单命中 {c['force_purchased_hit']} 个（缺路由兜底）",
        "",
        "### 8. 历史排单可调整范围（样例）",
        "- 规则：未入库行上的机台/车间视为历史排法；当前 `物料机台对照表` 额定+备用+主机台为允许集合。",
        "- 落在对照集合内 → 可重排到集合内其它机台；落在集合外 → 上表「hist_machine_vs_map」。",
        "",
    ]
    for row in summary.get("adjustable_unin_sample") or []:
        lines.append(
            f"- 订单 `{row['order']}` `{row['code']}` 历史=`{row['hist_machine']}` "
            f"允许≈{row['allowed_machines']} qty={row['qty']:.0f}"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FC 源表关系简单检查")
    parser.add_argument("--source-dir", "--input-dir", type=Path, default=ROOT, dest="source_dir")
    parser.add_argument("--output", type=Path, help="写 Markdown 报告路径")
    parser.add_argument("--json", type=Path, help="写 JSON 摘要路径")
    args = parser.parse_args(argv)
    source = args.source_dir.resolve()
    if not source.is_dir():
        parser.error(f"source-dir not found: {source}")
    summary = check_dataset(source)
    md = summary.pop("markdown")
    out_md = args.output or (source / f"fc_relation_check_{datetime.now():%Y%m%d_%H%M%S}.md")
    out_md = Path(out_md)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(md, encoding="utf-8")
    payload = {k: v for k, v in summary.items() if k != "adjustable_unin_sample"}
    # keep sample in json too
    payload["adjustable_unin_sample"] = summary.get("adjustable_unin_sample")
    if args.json:
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"output": str(out_md), "issue_counts": summary["issue_counts"], "counts": summary["counts"]}, ensure_ascii=False))
    print(md[:2500])
    return 0


if __name__ == "__main__":
    sys.exit(main())

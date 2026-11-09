#!/usr/bin/env python3
"""Generic FC APS dataset builder (validate + convert + scenario → JSON).

Pipeline
========
::

    <source-dir>/*.xlsx
        │
        ├─ stage A  convert_fc_aps.build()
        │            · 表间关系校验（硬失败 / 降级 / 丢弃）
        │            · 写成引擎底稿（item/buffer/operation/resource/demand…）
        │
        └─ stage B  build_scenario7_unin.build_unin_alarm_dataset()   [profile=unin_alarm]
                     · 未入库三级机台、预警净库存、共模、日历延展…
                     · 定谁 open / closed
            │
            ▼
    <output>.json  →  import_standard_dataset  →  scenario

Profiles
========
* ``base``       — only stage A (no alarm reopen / unfinished route bind)
* ``unin_alarm`` — stage A + B（当前 scenario7/8 主路径）
* ``check``      — 源表关系简单检查（BOM/资源/工时/自制外采/批量/库存/比例/历史排单）

Examples
========
::

    # 与历史 scenario8 同口径（源表在 fc_data/20261016）
    python build_fc_dataset.py \\
        --source-dir ../fc_data/20261016 \\
        --output ../fc_data/20261016/fc_aps_scenario8.json \\
        --profile unin_alarm

    # 仅底稿转换
    python build_fc_dataset.py --source-dir ../fc_data/20261016 --profile base \\
        --output /tmp/fc_base.json

    # 导入前关系检查
    python build_fc_dataset.py --source-dir ../fc_data/20261016 --profile check
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import convert_fc_aps as converter
from convert_fc_aps import build as convert_build

ROOT = Path(__file__).resolve().parent


def build_base_dataset(
    source_dir: Path,
    *,
    output: Path | None = None,
    include_alarm_demands: bool = False,
    planning_start: datetime | None = None,
) -> dict:
    """Stage A only: relationship checks + base engine tables."""
    source_dir = source_dir.resolve()
    converter.ROOT = source_dir
    document = convert_build(
        include_alarm_demands=include_alarm_demands,
        planning_start=planning_start,
    )
    meta = document.pop("_meta", {}) or {}
    summary = {
        "profile": "base",
        "source_dir": str(source_dir),
        "items": len(document["data"]["items"]),
        "demands": len(document["data"]["demands"]),
        "operations": len(document["data"]["operations"]),
        "no_equipment_on_order_items": len(meta.get("no_equipment") or []),
        "unin_rows": meta.get("unin_rows", 0),
    }
    if output is not None:
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        summary["output"] = str(output)
        print(json.dumps(summary, ensure_ascii=False))
    document["_build_summary"] = summary
    return document


def build_dataset(
    source_dir: Path,
    *,
    profile: str = "unin_alarm",
    output: Path | None = None,
    planning_start: datetime | None = None,
    planning_end: datetime | None = None,
    exclude_stock_demands: bool = False,
) -> dict:
    """Run the selected profile and optionally write JSON."""
    profile = (profile or "unin_alarm").strip().lower()
    if profile in ("base", "convert", "a"):
        return build_base_dataset(
            source_dir,
            output=output,
            include_alarm_demands=False,
            planning_start=planning_start,
        )
    if profile in ("unin_alarm", "unin", "alarm", "scenario7", "scenario8", "b"):
        from build_scenario7_unin import build_unin_alarm_dataset

        document = build_unin_alarm_dataset(
            source_dir,
            output=output,
            planning_start=planning_start,
            planning_end=planning_end,
            exclude_stock_demands=exclude_stock_demands,
        )
        summary = document.get("_build_summary") or {}
        summary["profile"] = "unin_alarm"
        summary["source_dir"] = str(Path(source_dir).resolve())
        document["_build_summary"] = summary
        return document
    if profile in ("check", "relations", "validate"):
        from check_fc_dataset import check_dataset

        summary = check_dataset(source_dir)
        md = summary.get("markdown") or ""
        if output is not None:
            output = Path(output)
            output.parent.mkdir(parents=True, exist_ok=True)
            if output.suffix.lower() == ".json":
                payload = {k: v for k, v in summary.items() if k != "markdown"}
                output.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
            else:
                output.write_text(md, encoding="utf-8")
            print(
                json.dumps(
                    {
                        "profile": "check",
                        "output": str(output),
                        "issue_counts": summary.get("issue_counts"),
                    },
                    ensure_ascii=False,
                )
            )
        return summary
    raise ValueError(
        f"unknown profile {profile!r}; use base, unin_alarm, or check"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="FC APS: Excel → validated planning JSON (generic entry).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--source-dir",
        "--input-dir",
        type=Path,
        default=ROOT,
        dest="source_dir",
        help="Directory of Excel source tables (物料/BOM/未入库/…)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output JSON path (default under source-dir by profile)",
    )
    parser.add_argument(
        "--profile",
        choices=("base", "unin_alarm", "check"),
        default="unin_alarm",
        help="base=convert only; unin_alarm=convert+scenario; check=源表关系检查",
    )
    parser.add_argument("--planning-start", type=datetime.fromisoformat)
    parser.add_argument("--planning-end", type=datetime.fromisoformat)
    parser.add_argument(
        "--exclude-stock-demands",
        action="store_true",
        help="Keep 备库 closed (unin_alarm profile only)",
    )
    args = parser.parse_args(argv)
    source = args.source_dir.resolve()
    if not source.is_dir():
        parser.error(f"source-dir not found: {source}")
    if args.profile == "base":
        default_name = "fc_aps_base.json"
    elif args.profile == "check":
        default_name = "fc_relation_check.md"
    else:
        default_name = "fc_aps_scenario_unin_alarm.json"
    output = args.output or (source / default_name)
    build_dataset(
        source,
        profile=args.profile,
        output=output,
        planning_start=args.planning_start,
        planning_end=args.planning_end,
        exclude_stock_demands=args.exclude_stock_demands,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

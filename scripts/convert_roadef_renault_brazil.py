#!/usr/bin/env python3
"""
Convert a ROADEF 2005 Renault car-sequencing instance to ccAPPS fixtures.

Business-oriented mapping:

* the process has only 2 operations: painting first, assembly second;
* paint color is represented as the setup of the painting resource;
* HPRC/LPRC ratio constraints are represented as option resources and preserved
  in a sidecar mapping file, because standard ccAPPS resources can't express
  ROADEF's sliding-window ratio constraints exactly;
* every vehicle remains an individual demand of quantity 1.

The generated ccAPPS fixture is valid master/demand data.  For an exact ROADEF
solver, use the generated sidecar JSON to feed a custom sequencing constraint.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, time
from pathlib import Path

TARGET_CURRENT_DATE = "2026-07-01 00:00:00"
FIRST_DUE_DATE = "2026-07-01 23:59:00"
SECOND_DUE_DATE = "2026-07-04 23:59:00"
PAINT_DURATION_PER = "00:04:00"
ASSEMBLY_DURATION_PER = "00:03:00"


def add(records: list[dict], model: str, fields: dict, pk=None) -> None:
    rec = {"model": model, "fields": fields}
    if pk is not None:
        rec["pk"] = pk
    records.append(rec)


def iso_week_due(date_value: str) -> str:
    year, week, weekday = (int(part) for part in date_value.split())
    day = datetime.combine(datetime.fromisocalendar(year, week, weekday), time(23, 59))
    return day.strftime("%Y-%m-%d %H:%M:%S")


def mapped_due(date_value: str, first_source_date: str) -> str:
    return FIRST_DUE_DATE if date_value == first_source_date else SECOND_DUE_DATE


def read_ratios(instance_dir: Path) -> dict[str, dict[str, str]]:
    with (instance_dir / "ratios.txt").open(
        "r", encoding="utf-8-sig", newline=""
    ) as handle:
        reader = csv.DictReader(handle, delimiter=";")
        result = {}
        for row in reader:
            ident = row.get("Ident") or ""
            if not ident:
                continue
            result[ident] = {
                "ratio": row["Ratio"],
                "priority": "HPRC" if row["Prio"] == "1" else "LPRC",
            }
        return result


def read_paint_limit(instance_dir: Path) -> str:
    with (instance_dir / "paint_batch_limit.txt").open(
        "r", encoding="utf-8-sig"
    ) as handle:
        lines = [line.strip().strip(";") for line in handle if line.strip()]
    return lines[1] if len(lines) > 1 else ""


def read_vehicles(instance_dir: Path) -> list[dict[str, str]]:
    with (instance_dir / "vehicles.txt").open(
        "r", encoding="utf-8-sig", newline=""
    ) as handle:
        return list(csv.DictReader(handle, delimiter=";"))


def convert(instance_dir: Path, output: Path, sidecar_output: Path) -> None:
    vehicles = read_vehicles(instance_dir)
    if not vehicles:
        raise ValueError(f"No vehicles found in {instance_dir / 'vehicles.txt'}")

    ratios = read_ratios(instance_dir)
    paint_limit = read_paint_limit(instance_dir)
    option_columns = [
        col
        for col in vehicles[0]
        if col.startswith("HPRC") or col.startswith("LPRC")
    ]
    colors = sorted({row["Paint Color"] for row in vehicles}, key=lambda val: int(val))
    first_source_date = min({row["Date"] for row in vehicles})

    records: list[dict] = []
    location = "Renault Brazil plant"
    customer = "ROADEF customer"
    calendar = "ROADEF working calendar"
    paint_resource = "Paint shop"
    assembly_resource = "Assembly line"
    body_supplier = "Body supplier"
    paint_setup_matrix = "ROADEF paint color matrix"
    raw_item = "Unpainted vehicle"
    painted_item = "Painted vehicle"
    finished_item = "Finished vehicle"
    paint_operation = "Paint vehicle"
    assembly_operation = "Assemble vehicle"

    add(
        records,
        "common.parameter",
        {
            "value": TARGET_CURRENT_DATE,
            "description": "Current date for the converted ROADEF Renault Brazil fixture",
        },
        "currentdate",
    )
    add(
        records,
        "common.parameter",
        {
            "value": "0",
            "description": "No delivery duration for the converted ROADEF fixture",
        },
        "plan.deliveryDuration",
    )

    add(records, "input.calendar", {"defaultvalue": "1"}, calendar)
    add(
        records,
        "input.calendarbucket",
        {
            "calendar": calendar,
            "startdate": "2003-01-01 00:00:00",
            "enddate": "2030-12-31 00:00:00",
            "monday": True,
            "tuesday": True,
            "wednesday": True,
            "thursday": True,
            "friday": True,
            "saturday": True,
            "sunday": True,
            "value": "1",
            "priority": 0,
        },
    )
    add(records, "input.location", {"available": calendar}, location)
    add(records, "input.customer", {}, customer)
    add(records, "input.supplier", {"available": calendar}, body_supplier)

    for item, subcategory in [
        (raw_item, "raw"),
        (painted_item, "intermediate"),
        (finished_item, "finished"),
    ]:
        add(
            records,
            "input.item",
            {
                "description": f"ROADEF Renault Brazil {subcategory} vehicle item",
                "category": "ROADEF vehicle",
                "subcategory": subcategory,
                "type": "make to order",
                "uom": "vehicle",
            },
            item,
        )
        add(records, "input.buffer", {"item": item, "location": location})

    add(records, "input.setupmatrix", {}, paint_setup_matrix)
    for priority, color in enumerate(colors, start=1):
        add(
            records,
            "input.setuprule",
            {
                "setupmatrix": paint_setup_matrix,
                "priority": priority,
                "fromsetup": f"Color {color}",
                "tosetup": f"Color {color}",
                "duration": "00:00:00",
                "cost": "0",
            },
        )
    add(
        records,
        "input.setuprule",
        {
            "setupmatrix": paint_setup_matrix,
            "priority": 1000,
            "fromsetup": None,
            "tosetup": None,
            "duration": "00:05:00",
            "cost": "1",
        },
    )

    add(
        records,
        "input.resource",
        {
            "description": (
                "Painting resource. Vehicle color is represented as setup. "
                f"ROADEF paint batch limit: {paint_limit or 'not specified'}."
            ),
            "category": "ROADEF",
            "subcategory": "painting",
            "maximum": "1",
            "maximum_calendar": calendar,
            "location": location,
            "type": "default",
            "setupmatrix": paint_setup_matrix,
            "setup": f"Color {colors[0]}",
            "maxearly": "999 00:00:00",
        },
        paint_resource,
    )
    add(
        records,
        "input.resource",
        {
            "description": (
                "Assembly resource. HPRC/LPRC sliding-window constraints are "
                "stored as option resources and in the sidecar mapping file."
            ),
            "category": "ROADEF",
            "subcategory": "assembly",
            "maximum": "1",
            "maximum_calendar": calendar,
            "location": location,
            "type": "default",
            "maxearly": "999 00:00:00",
        },
        assembly_resource,
    )
    for option in option_columns:
        ratio = ratios.get(option, {})
        add(
            records,
            "input.resource",
            {
                "description": (
                    f"ROADEF {ratio.get('priority', 'option')} option {option}; "
                    f"sliding-window ratio {ratio.get('ratio', 'unknown')}."
                ),
                "category": "ROADEF option",
                "subcategory": ratio.get("priority", "option"),
                "location": location,
                "type": "infinite",
            },
            f"Option {option}",
        )

    add(
        records,
        "input.itemsupplier",
        {
            "item": raw_item,
            "location": location,
            "supplier": body_supplier,
            "leadtime": "00:00:00",
            "sizeminimum": "1",
            "sizemultiple": "1",
            "priority": 1,
            "resource_qty": "1",
            "fence": "00:00:00",
        },
    )

    add(
        records,
        "input.operation",
        {
            "description": "First ROADEF process step: paint the vehicle body",
            "category": "ROADEF process",
            "subcategory": "painting",
            "item": painted_item,
            "location": location,
            "type": "time_per",
            "duration_per": PAINT_DURATION_PER,
            "sizeminimum": "1",
            "sizemaximum": "1",
        },
        paint_operation,
    )
    add(
        records,
        "input.operation",
        {
            "description": (
                "Second ROADEF process step: assemble the painted vehicle. "
                "HPRC/LPRC option flags are per-vehicle sequencing attributes."
            ),
            "category": "ROADEF process",
            "subcategory": "assembly",
            "item": finished_item,
            "location": location,
            "type": "time_per",
            "duration_per": ASSEMBLY_DURATION_PER,
            "sizeminimum": "1",
            "sizemaximum": "1",
        },
        assembly_operation,
    )

    operation_resource_pk = 1
    for operation, resource in [
        (paint_operation, paint_resource),
        (assembly_operation, assembly_resource),
    ]:
        add(
            records,
            "input.operationresource",
            {
                "operation": operation,
                "resource": resource,
                "quantity": "1",
                "priority": 1,
            },
            operation_resource_pk,
        )
        operation_resource_pk += 1

    operation_material_pk = 1
    for operation, item, quantity, flow_type in [
        (paint_operation, raw_item, "-1", "start"),
        (paint_operation, painted_item, "1", "end"),
        (assembly_operation, painted_item, "-1", "start"),
        (assembly_operation, finished_item, "1", "end"),
    ]:
        add(
            records,
            "input.operationmaterial",
            {
                "operation": operation,
                "item": item,
                "quantity": quantity,
                "type": flow_type,
                "priority": 1,
            },
            operation_material_pk,
        )
        operation_material_pk += 1

    sidecar = {
        "source_instance": str(instance_dir),
        "process": [paint_operation, assembly_operation],
        "paint_batch_limit": paint_limit,
        "ratios": ratios,
        "vehicles": [],
    }
    for row in vehicles:
        enabled_options = [
            option for option in option_columns if row.get(option) == "1"
        ]
        add(
            records,
            "input.demand",
            {
                "description": (
                    f"Original ROADEF vehicle {row['Ident']}; date {row['Date']}; "
                    f"sequence rank {row['SeqRank']}; paint color {row['Paint Color']}; "
                    f"options {','.join(enabled_options) or 'none'}."
                ),
                "category": "ROADEF vehicle demand",
                "subcategory": f"Color {row['Paint Color']}",
                "item": finished_item,
                "customer": customer,
                "location": location,
                "operation": assembly_operation,
                "due": mapped_due(row["Date"], first_source_date),
                "priority": int(row["SeqRank"]),
                "quantity": "1",
                "minshipment": "1",
            },
            f"Vehicle {row['Ident']}",
        )
        sidecar["vehicles"].append(
            {
                "demand": f"Vehicle {row['Ident']}",
                "ident": row["Ident"],
                "date": row["Date"],
                "sequence_rank": int(row["SeqRank"]),
                "paint_color": row["Paint Color"],
                "options": {option: row[option] == "1" for option in option_columns},
            }
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        json.dump(records, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    sidecar_output.parent.mkdir(parents=True, exist_ok=True)
    with sidecar_output.open("w", encoding="utf-8") as handle:
        json.dump(sidecar, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    print(f"Wrote {len(records)} ccAPPS records to {output}")
    print(f"Wrote ROADEF sequencing sidecar to {sidecar_output}")
    print(f"Vehicles: {len(vehicles)}")
    print("Operations: 2")
    print(f"Paint colors: {len(colors)}")
    print(f"Options: {len(option_columns)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "instance_dir",
        nargs="?",
        type=Path,
        default=Path("RoadefRenaultBrazil/data/Instances_A/024_38_3_EP_ENP_RAF"),
        help="Directory containing vehicles.txt, ratios.txt and paint_batch_limit.txt",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("ccAPPSdb/input/fixtures/roadef_renault_brazil.json"),
        help="Output ccAPPS JSON fixture",
    )
    parser.add_argument(
        "--sidecar-output",
        type=Path,
        default=Path("ccAPPSdb/input/fixtures/roadef_renault_brazil_sidecar.json"),
        help="Output sidecar JSON with exact ROADEF vehicle color/option attributes",
    )
    args = parser.parse_args()
    convert(args.instance_dir, args.output, args.sidecar_output)


if __name__ == "__main__":
    main()

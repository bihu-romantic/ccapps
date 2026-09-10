#!/usr/bin/env python3
"""Generate jobshop.json fixture for a job shop manufacturing case."""

import json
from datetime import datetime, timedelta

fixture = []

# Helper
def add(model, **fields):
    fixture.append({"model": model, "fields": fields})

def dur(seconds):
    """Convert seconds to Django DurationField string: HH:MM:SS"""
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

# ============================================================
# 1. Calendars
# ============================================================
add("input.calendar", name="Standard Week", defaultvalue=100)
add("input.calendar", name="Working Hours", defaultvalue=100)
add("input.calendar", name="Heat Treat Calendar", defaultvalue=100)

# Standard Week bucket: Mon-Fri working, Sat-Sun off
cal_start = "2024-01-01 00:00:00"
add("input.calendarbucket",
    startdate=cal_start, enddate="2030-12-31 00:00:00",
    value=100, priority=0,
    monday=True, tuesday=True, wednesday=True, thursday=True, friday=True,
    saturday=False, sunday=False,
    starttime="00:00:00",
    calendar_id="Standard Week")

# Working Hours bucket: 8h/day (8:00-12:00, 13:00-17:00)
add("input.calendarbucket",
    startdate=cal_start, enddate="2030-12-31 00:00:00",
    value=100, priority=1,
    monday=True, tuesday=True, wednesday=True, thursday=True, friday=True,
    saturday=False, sunday=False,
    starttime="08:00:00",
    endtime="17:00:00",
    calendar_id="Working Hours")

# Heat Treat: runs 24h when active
add("input.calendarbucket",
    startdate=cal_start, enddate="2030-12-31 00:00:00",
    value=100, priority=0,
    monday=True, tuesday=True, wednesday=True, thursday=True, friday=True,
    saturday=True, sunday=True,
    starttime="00:00:00",
    calendar_id="Heat Treat Calendar")

# ============================================================
# 2. Locations
# ============================================================
add("input.location", name="All locations")
add("input.location", name="Factory", available_id="Standard Week", owner_id="All locations")
add("input.location", name="Customer A", available_id="Standard Week", owner_id="All locations")
add("input.location", name="Customer B", available_id="Standard Week", owner_id="All locations")

# ============================================================
# 3. Customers
# ============================================================
add("input.customer", name="Customer A Industries", category="industrial")
add("input.customer", name="Customer B Manufacturing", category="industrial")

# ============================================================
# 4. Items
# ============================================================
# Raw materials
add("input.item", name="Steel Rod Ø50mm", category="raw material", subcategory="steel", cost=120)
add("input.item", name="Steel Plate 10mm", category="raw material", subcategory="steel", cost=80)
add("input.item", name="Steel Block 100mm", category="raw material", subcategory="steel", cost=200)

# Intermediate items - Drive Shaft
add("input.item", name="Turned Shaft", category="WIP", subcategory="shaft", cost=150)
add("input.item", name="Milled Shaft", category="WIP", subcategory="shaft", cost=180)

# Intermediate items - Gear Blank
add("input.item", name="Turned Gear", category="WIP", subcategory="gear", cost=240)
add("input.item", name="Drilled Gear", category="WIP", subcategory="gear", cost=260)
add("input.item", name="Milled Gear Teeth", category="WIP", subcategory="gear", cost=320)
add("input.item", name="Hardened Gear", category="WIP", subcategory="gear", cost=350)
add("input.item", name="Ground Gear", category="WIP", subcategory="gear", cost=400)

# Intermediate items - Mounting Bracket
add("input.item", name="Milled Bracket", category="WIP", subcategory="bracket", cost=100)
add("input.item", name="Drilled Bracket", category="WIP", subcategory="bracket", cost=120)

# Intermediate items - Bearing Housing
add("input.item", name="Turned Housing", category="WIP", subcategory="housing", cost=220)
add("input.item", name="Milled Housing", category="WIP", subcategory="housing", cost=250)
add("input.item", name="Drilled Housing", category="WIP", subcategory="housing", cost=270)

# Finished goods
add("input.item", name="Drive Shaft", category="finished good", subcategory="shaft", cost=220)
add("input.item", name="Gear Blank", category="finished good", subcategory="gear", cost=450)
add("input.item", name="Mounting Bracket", category="finished good", subcategory="bracket", cost=150)
add("input.item", name="Bearing Housing", category="finished good", subcategory="housing", cost=300)

# ============================================================
# 5. Resources (Work Centers)
# ============================================================
add("input.resource", name="Lathe", category="machining", subcategory="turning",
    maximum=2, location_id="Factory", maximum_calendar_id="Working Hours")
add("input.resource", name="Milling Machine", category="machining", subcategory="milling",
    maximum=2, location_id="Factory", maximum_calendar_id="Working Hours")
add("input.resource", name="Drill Press", category="machining", subcategory="drilling",
    maximum=1, location_id="Factory", maximum_calendar_id="Working Hours")
add("input.resource", name="Grinder", category="machining", subcategory="grinding",
    maximum=1, location_id="Factory", maximum_calendar_id="Working Hours")
add("input.resource", name="Heat Treat Furnace", category="thermal", subcategory="hardening",
    maximum=1, location_id="Factory", maximum_calendar_id="Heat Treat Calendar")
add("input.resource", name="Inspection", category="quality", subcategory="inspection",
    maximum=1, location_id="Factory", maximum_calendar_id="Working Hours")

# ============================================================
# 6. Operations (one per process step)
# ============================================================
# --- Drive Shaft routing ---
add("input.operation", name="Turn Shaft", type="time_per", category="turning",
    duration_per=dur(1800), sizeminimum=1,
    item_id="Turned Shaft", location_id="Factory")
add("input.operation", name="Mill Shaft Keyway", type="time_per", category="milling",
    duration_per=dur(1200), sizeminimum=1,
    item_id="Milled Shaft", location_id="Factory")
add("input.operation", name="Grind Shaft", type="time_per", category="grinding",
    duration_per=dur(1500), sizeminimum=1,
    item_id="Drive Shaft", location_id="Factory")
add("input.operation", name="Inspect Shaft", type="time_per", category="inspection",
    duration_per=dur(600), sizeminimum=1,
    item_id="Drive Shaft", location_id="Factory")

# --- Gear Blank routing ---
add("input.operation", name="Turn Gear Blank", type="time_per", category="turning",
    duration_per=dur(2400), sizeminimum=1,
    item_id="Turned Gear", location_id="Factory")
add("input.operation", name="Drill Gear Center", type="time_per", category="drilling",
    duration_per=dur(900), sizeminimum=1,
    item_id="Drilled Gear", location_id="Factory")
add("input.operation", name="Mill Gear Teeth", type="time_per", category="milling",
    duration_per=dur(2700), sizeminimum=1,
    item_id="Milled Gear Teeth", location_id="Factory")
add("input.operation", name="Heat Treat Gear", type="fixed_time", category="thermal",
    duration=dur(7200), duration_per=dur(600), sizeminimum=1,
    item_id="Hardened Gear", location_id="Factory")
add("input.operation", name="Grind Gear", type="time_per", category="grinding",
    duration_per=dur(2100), sizeminimum=1,
    item_id="Ground Gear", location_id="Factory")
add("input.operation", name="Inspect Gear", type="time_per", category="inspection",
    duration_per=dur(900), sizeminimum=1,
    item_id="Gear Blank", location_id="Factory")

# --- Mounting Bracket routing ---
add("input.operation", name="Mill Bracket", type="time_per", category="milling",
    duration_per=dur(900), sizeminimum=1,
    item_id="Milled Bracket", location_id="Factory")
add("input.operation", name="Drill Bracket Holes", type="time_per", category="drilling",
    duration_per=dur(600), sizeminimum=1,
    item_id="Drilled Bracket", location_id="Factory")
add("input.operation", name="Inspect Bracket", type="time_per", category="inspection",
    duration_per=dur(480), sizeminimum=1,
    item_id="Mounting Bracket", location_id="Factory")

# --- Bearing Housing routing ---
add("input.operation", name="Turn Housing", type="time_per", category="turning",
    duration_per=dur(1500), sizeminimum=1,
    item_id="Turned Housing", location_id="Factory")
add("input.operation", name="Mill Housing Face", type="time_per", category="milling",
    duration_per=dur(1200), sizeminimum=1,
    item_id="Milled Housing", location_id="Factory")
add("input.operation", name="Drill Housing Bolt Holes", type="time_per", category="drilling",
    duration_per=dur(720), sizeminimum=1,
    item_id="Drilled Housing", location_id="Factory")
add("input.operation", name="Inspect Housing", type="time_per", category="inspection",
    duration_per=dur(600), sizeminimum=1,
    item_id="Bearing Housing", location_id="Factory")

# ============================================================
# 7. Operation Materials (BOM for each operation)
# ============================================================
# Drive Shaft BOM
add("input.operationmaterial", item_id="Turned Shaft", operation_id="Turn Shaft",
    quantity=1.0, type="end", priority=1)
add("input.operationmaterial", item_id="Steel Rod Ø50mm", operation_id="Turn Shaft",
    quantity=-1.0, type="start", priority=1)

add("input.operationmaterial", item_id="Milled Shaft", operation_id="Mill Shaft Keyway",
    quantity=1.0, type="end", priority=1)
add("input.operationmaterial", item_id="Turned Shaft", operation_id="Mill Shaft Keyway",
    quantity=-1.0, type="start", priority=1)

add("input.operationmaterial", item_id="Drive Shaft", operation_id="Grind Shaft",
    quantity=1.0, type="end", priority=1)
add("input.operationmaterial", item_id="Milled Shaft", operation_id="Grind Shaft",
    quantity=-1.0, type="start", priority=1)

add("input.operationmaterial", item_id="Drive Shaft", operation_id="Inspect Shaft",
    quantity=1.0, type="end", priority=1)
add("input.operationmaterial", item_id="Drive Shaft", operation_id="Inspect Shaft",
    quantity=-1.0, type="start", priority=1)

# Gear Blank BOM
add("input.operationmaterial", item_id="Turned Gear", operation_id="Turn Gear Blank",
    quantity=1.0, type="end", priority=1)
add("input.operationmaterial", item_id="Steel Block 100mm", operation_id="Turn Gear Blank",
    quantity=-1.0, type="start", priority=1)

add("input.operationmaterial", item_id="Drilled Gear", operation_id="Drill Gear Center",
    quantity=1.0, type="end", priority=1)
add("input.operationmaterial", item_id="Turned Gear", operation_id="Drill Gear Center",
    quantity=-1.0, type="start", priority=1)

add("input.operationmaterial", item_id="Milled Gear Teeth", operation_id="Mill Gear Teeth",
    quantity=1.0, type="end", priority=1)
add("input.operationmaterial", item_id="Drilled Gear", operation_id="Mill Gear Teeth",
    quantity=-1.0, type="start", priority=1)

add("input.operationmaterial", item_id="Hardened Gear", operation_id="Heat Treat Gear",
    quantity=1.0, type="end", priority=1)
add("input.operationmaterial", item_id="Milled Gear Teeth", operation_id="Heat Treat Gear",
    quantity=-1.0, type="start", priority=1)

add("input.operationmaterial", item_id="Ground Gear", operation_id="Grind Gear",
    quantity=1.0, type="end", priority=1)
add("input.operationmaterial", item_id="Hardened Gear", operation_id="Grind Gear",
    quantity=-1.0, type="start", priority=1)

add("input.operationmaterial", item_id="Gear Blank", operation_id="Inspect Gear",
    quantity=1.0, type="end", priority=1)
add("input.operationmaterial", item_id="Ground Gear", operation_id="Inspect Gear",
    quantity=-1.0, type="start", priority=1)

# Mounting Bracket BOM
add("input.operationmaterial", item_id="Milled Bracket", operation_id="Mill Bracket",
    quantity=1.0, type="end", priority=1)
add("input.operationmaterial", item_id="Steel Plate 10mm", operation_id="Mill Bracket",
    quantity=-1.0, type="start", priority=1)

add("input.operationmaterial", item_id="Drilled Bracket", operation_id="Drill Bracket Holes",
    quantity=1.0, type="end", priority=1)
add("input.operationmaterial", item_id="Milled Bracket", operation_id="Drill Bracket Holes",
    quantity=-1.0, type="start", priority=1)

add("input.operationmaterial", item_id="Mounting Bracket", operation_id="Inspect Bracket",
    quantity=1.0, type="end", priority=1)
add("input.operationmaterial", item_id="Drilled Bracket", operation_id="Inspect Bracket",
    quantity=-1.0, type="start", priority=1)

# Bearing Housing BOM
add("input.operationmaterial", item_id="Turned Housing", operation_id="Turn Housing",
    quantity=1.0, type="end", priority=1)
add("input.operationmaterial", item_id="Steel Block 100mm", operation_id="Turn Housing",
    quantity=-1.0, type="start", priority=1)

add("input.operationmaterial", item_id="Milled Housing", operation_id="Mill Housing Face",
    quantity=1.0, type="end", priority=1)
add("input.operationmaterial", item_id="Turned Housing", operation_id="Mill Housing Face",
    quantity=-1.0, type="start", priority=1)

add("input.operationmaterial", item_id="Drilled Housing", operation_id="Drill Housing Bolt Holes",
    quantity=1.0, type="end", priority=1)
add("input.operationmaterial", item_id="Milled Housing", operation_id="Drill Housing Bolt Holes",
    quantity=-1.0, type="start", priority=1)

add("input.operationmaterial", item_id="Bearing Housing", operation_id="Inspect Housing",
    quantity=1.0, type="end", priority=1)
add("input.operationmaterial", item_id="Drilled Housing", operation_id="Inspect Housing",
    quantity=-1.0, type="start", priority=1)

# ============================================================
# 8. Operation Resources
# ============================================================
# Drive Shaft
add("input.operationresource", operation_id="Turn Shaft", resource_id="Lathe", quantity=1, priority=1)
add("input.operationresource", operation_id="Mill Shaft Keyway", resource_id="Milling Machine", quantity=1, priority=1)
add("input.operationresource", operation_id="Grind Shaft", resource_id="Grinder", quantity=1, priority=1)
add("input.operationresource", operation_id="Inspect Shaft", resource_id="Inspection", quantity=1, priority=1)

# Gear Blank
add("input.operationresource", operation_id="Turn Gear Blank", resource_id="Lathe", quantity=1, priority=1)
add("input.operationresource", operation_id="Drill Gear Center", resource_id="Drill Press", quantity=1, priority=1)
add("input.operationresource", operation_id="Mill Gear Teeth", resource_id="Milling Machine", quantity=1, priority=1)
add("input.operationresource", operation_id="Heat Treat Gear", resource_id="Heat Treat Furnace", quantity=1, priority=1)
add("input.operationresource", operation_id="Grind Gear", resource_id="Grinder", quantity=1, priority=1)
add("input.operationresource", operation_id="Inspect Gear", resource_id="Inspection", quantity=1, priority=1)

# Mounting Bracket
add("input.operationresource", operation_id="Mill Bracket", resource_id="Milling Machine", quantity=1, priority=1)
add("input.operationresource", operation_id="Drill Bracket Holes", resource_id="Drill Press", quantity=1, priority=1)
add("input.operationresource", operation_id="Inspect Bracket", resource_id="Inspection", quantity=1, priority=1)

# Bearing Housing
add("input.operationresource", operation_id="Turn Housing", resource_id="Lathe", quantity=1, priority=1)
add("input.operationresource", operation_id="Mill Housing Face", resource_id="Milling Machine", quantity=1, priority=1)
add("input.operationresource", operation_id="Drill Housing Bolt Holes", resource_id="Drill Press", quantity=1, priority=1)
add("input.operationresource", operation_id="Inspect Housing", resource_id="Inspection", quantity=1, priority=1)

# ============================================================
# 9. Supplier
# ============================================================
add("input.supplier", name="Steel Supplier")

# ============================================================
# 10. Item Suppliers (procurement of raw materials)
# ============================================================
add("input.itemsupplier", item_id="Steel Rod Ø50mm", location_id="Factory",
    supplier_id="Steel Supplier", leadtime=dur(604800), sizeminimum=10, priority=1, resource_qty=1)
add("input.itemsupplier", item_id="Steel Plate 10mm", location_id="Factory",
    supplier_id="Steel Supplier", leadtime=dur(432000), sizeminimum=5, priority=1, resource_qty=1)
add("input.itemsupplier", item_id="Steel Block 100mm", location_id="Factory",
    supplier_id="Steel Supplier", leadtime=dur(518400), sizeminimum=5, priority=1, resource_qty=1)

# ============================================================
# 11. Buffers (initial inventory)
# ============================================================
# Raw material stock
add("input.buffer", item_id="Steel Rod Ø50mm", location_id="Factory", onhand=50, minimum=10)
add("input.buffer", item_id="Steel Plate 10mm", location_id="Factory", onhand=30, minimum=5)
add("input.buffer", item_id="Steel Block 100mm", location_id="Factory", onhand=20, minimum=5)

# Finished goods stock at factory
add("input.buffer", item_id="Drive Shaft", location_id="Factory", onhand=5, minimum=2)
add("input.buffer", item_id="Gear Blank", location_id="Factory", onhand=3, minimum=2)
add("input.buffer", item_id="Mounting Bracket", location_id="Factory", onhand=10, minimum=3)
add("input.buffer", item_id="Bearing Housing", location_id="Factory", onhand=4, minimum=2)

# ============================================================
# 12. Demands (history + forecast)
# ============================================================
# Generate monthly demand history for 24 months (2024-05 to 2026-04)
# and forecast demands for 12 months (2026-05 to 2027-04)

demands_config = [
    # (item, customer, location, avg_monthly_qty, variability)
    ("Drive Shaft", "Customer A Industries", "Factory", 40, 0.3),
    ("Drive Shaft", "Customer B Manufacturing", "Factory", 25, 0.25),
    ("Gear Blank", "Customer A Industries", "Factory", 30, 0.2),
    ("Gear Blank", "Customer B Manufacturing", "Factory", 20, 0.35),
    ("Mounting Bracket", "Customer A Industries", "Factory", 60, 0.25),
    ("Mounting Bracket", "Customer B Manufacturing", "Factory", 35, 0.3),
    ("Bearing Housing", "Customer A Industries", "Factory", 20, 0.2),
    ("Bearing Housing", "Customer B Manufacturing", "Factory", 15, 0.3),
]

import random
random.seed(42)

demand_id = 1
# History: 24 months
for month_offset in range(-24, 0):
    due = datetime(2026, 5, 1) + timedelta(days=month_offset * 30)
    due_str = due.strftime("%Y-%m-%d 00:00:00")
    for item, customer, location, avg_qty, var in demands_config:
        qty = max(1, int(random.gauss(avg_qty, avg_qty * var)))
        add("input.demand",
            name=f"History {demand_id}",
            due=due_str,
            status="closed",
            quantity=qty,
            priority=1,
            customer_id=customer,
            item_id=item,
            location_id=location)
        demand_id += 1

# Forecast demands: 12 months
for month_offset in range(0, 12):
    due = datetime(2026, 5, 1) + timedelta(days=month_offset * 30)
    due_str = due.strftime("%Y-%m-%d 00:00:00")
    for item, customer, location, avg_qty, var in demands_config:
        qty = max(1, int(random.gauss(avg_qty, avg_qty * var)))
        add("input.demand",
            name=f"Forecast {demand_id}",
            due=due_str,
            status="open",
            quantity=qty,
            priority=1,
            customer_id=customer,
            item_id=item,
            location_id=location)
        demand_id += 1

# ============================================================
# 13. Parameters
# ============================================================
add("common.parameter", name="currentdate", value="2026-05-09 00:00:00")
add("common.parameter", name="plan.webservice", value="true")
add("common.parameter", name="plan.iterationmax", value="100")
add("common.parameter", name="lazydelay", value="86400")
add("common.parameter", name="plan.minimumdelay", value="3600")

# ============================================================
# Write output
# ============================================================
output_path = "/home/c/frepple-master/ccAPPSdb/input/fixtures/jobshop.json"
with open(output_path, "w", encoding="utf-8") as f:
    json.dump(fixture, f, indent=2, ensure_ascii=False)

print(f"Generated {len(fixture)} records to {output_path}")
# Show summary
from collections import Counter
for model, cnt in Counter(d['model'] for d in fixture).most_common():
    print(f"  {model}: {cnt}")

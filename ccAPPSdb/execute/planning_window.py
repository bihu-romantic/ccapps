"""Resolve a scenario's finite planning window for every execution entry point."""
import math
from datetime import datetime


def planning_window_end(current, explicit_end=None, configured_end=None):
    """An explicit run override takes precedence over the scenario default."""
    value = explicit_end or configured_end
    if not value:
        return None
    try:
        end = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "supply_plan_end or plan.horizon_end must be an ISO date/time"
        ) from exc
    if end <= current:
        raise ValueError(
            "supply_plan_end or plan.horizon_end must be after the planning current date"
        )
    return end


def unit_split_shipment(remaining, snp=1.0, economic=0.0):
    """First lot = one split unit; no cap on how many later lots follow.

    Prefers economic batch as the unit when present; otherwise SNP.
    ``remaining > 2n`` (via ``split_is_worthwhile``) is the usual gate before
    calling this — once gated, lots of size ``n`` continue until done.
    """
    remaining = float(remaining or 0.0)
    snp = max(1.0, float(snp or 1.0))
    economic = float(economic or 0.0)
    unit = economic if economic > 1.0 else snp
    if remaining <= 1e-9:
        return unit
    if remaining <= unit + 1e-9:
        return remaining
    return unit


def exact_piece_shipment(
    remaining, duration=0.0, duration_per=0.0, threshold_hours=8.0
):
    """First exact piece sized so processing ≤ threshold hours; no part cap.

    Later remainder passes keep cutting until the leftover is ≤ threshold.
    If duration_per is unknown, keep the demand whole.
    """
    remaining = float(remaining or 0.0)
    if remaining <= 1e-9:
        return 1.0
    try:
        hours = float(threshold_hours if threshold_hours is not None else 8.0)
    except (TypeError, ValueError):
        hours = 8.0
    threshold = hours * 3600.0
    try:
        fixed = float(duration or 0.0)
    except (TypeError, ValueError):
        fixed = 0.0
    try:
        per = float(duration_per or 0.0)
    except (TypeError, ValueError):
        per = 0.0
    if per <= 1e-9:
        return remaining
    # largest qty with fixed + per*qty ≤ threshold (at least 1)
    max_qty = math.floor((threshold - fixed) / per + 1e-9)
    if max_qty < 1:
        max_qty = 1
    return min(remaining, float(max_qty))


def capped_split_shipment(remaining, snp=1.0, economic=0.0, max_parts=3):
    """Min shipment so one demand splits into at most ``max_parts`` lots.

    Prefer ``unit_split_shipment`` when the part count must not be capped.
    ``max_parts <= 0`` means uncapped (same as ``unit_split_shipment``).
    ``max_parts=2`` means at most one split (two pieces).

    If the leftover after the first lot would be smaller than SNP, keep the
    demand whole so we do not emit meaningless tails (e.g. 669 + 4).
    """
    remaining = float(remaining or 0.0)
    if remaining <= 1e-9:
        return max(1.0, float(snp or 1.0))
    try:
        max_parts = int(max_parts) if max_parts is not None else 0
    except (TypeError, ValueError):
        max_parts = 0
    if max_parts <= 0:
        return unit_split_shipment(remaining, snp=snp, economic=economic)
    max_parts = max(1, max_parts)
    snp = max(1.0, float(snp or 1.0))
    economic = float(economic or 0.0)
    unit = economic if economic > 1.0 else snp
    if max_parts <= 1 or remaining <= unit + 1e-9:
        return remaining
    floor_chunk = math.ceil(remaining / max_parts - 1e-9)
    chunk = math.ceil(max(floor_chunk, unit) / unit - 1e-9) * unit
    chunk = min(chunk, remaining)
    tail = remaining - chunk
    if 0 < tail < snp - 1e-9:
        return remaining
    return chunk


def split_is_worthwhile(remaining, snp=1.0, economic=0.0, min_parts_factor=2.0):
    """False when the order is too small to bother splitting.

    Default: keep whole when ``remaining <= 2 * split_unit`` (economic batch
    if present, otherwise SNP). Stops noise like 12 → 6 + 6.
    Economic 必须拆 uses this with ``economic=n`` (gate: remaining > 2n).
    """
    remaining = float(remaining or 0.0)
    if remaining <= 1e-9:
        return False
    snp = max(1.0, float(snp or 1.0))
    economic = float(economic or 0.0)
    unit = economic if economic > 1.0 else snp
    return remaining > float(min_parts_factor) * unit + 1e-9


def exact_processing_over_hours(
    processing_seconds, threshold_hours=8.0
):
    """True when 备件/断点 processing exceeds the split threshold (default 8h)."""
    try:
        seconds = float(processing_seconds or 0.0)
    except (TypeError, ValueError):
        seconds = 0.0
    try:
        hours = float(threshold_hours if threshold_hours is not None else 8.0)
    except (TypeError, ValueError):
        hours = 8.0
    return seconds > hours * 3600.0 + 1e-9


def estimate_processing_seconds(quantity, duration=0.0, duration_per=0.0):
    """Rough FAS pre-solve duration: fixed setup + per-unit × qty."""
    try:
        qty = float(quantity or 0.0)
    except (TypeError, ValueError):
        qty = 0.0
    try:
        fixed = float(duration or 0.0)
    except (TypeError, ValueError):
        fixed = 0.0
    try:
        per = float(duration_per or 0.0)
    except (TypeError, ValueError):
        per = 0.0
    if qty <= 1e-9:
        return max(0.0, fixed)
    return max(0.0, fixed + per * qty)


def longest_processing_profile(quantity, profiles):
    """Return the fixed/per-unit profile with the longest time for ``quantity``.

    Item-producing operations are often alternate or routing containers whose
    own duration is empty.  Their child operations contain the real duration.
    Selecting on the requested quantity also avoids comparing ``fixed + per``
    at one unit, which can pick the wrong profile for a larger order.
    """
    best = (0.0, 0.0)
    best_seconds = -1.0
    for fixed, per in profiles or ():
        seconds = estimate_processing_seconds(
            quantity, duration=fixed, duration_per=per
        )
        if seconds > best_seconds:
            best = (fixed, per)
            best_seconds = seconds
    return best


def complete_order_minshipment(remaining, snp=1.0, economic=0.0):
    """Keep an order whole when no split rule has been selected."""
    remaining = float(remaining or 0.0)
    snp = max(1.0, float(snp or 1.0))
    economic = float(economic or 0.0)
    if remaining <= 1e-9:
        return snp
    return remaining


def item_matches_split_scope(item_name, item_codes=None, prefixes=None):
    """True when the item is in an explicit code set or matches a prefix."""
    if not item_name:
        return False
    if item_codes and item_name in item_codes:
        return True
    for prefix in prefixes or ():
        if prefix and str(item_name).startswith(str(prefix)):
            return True
    return False


def demand_category_tier(demand_name, category=None):
    """Lower tier sorts first: 预警 (0) > 未入库 (1) > 备库 (2) > other (3)."""
    name = demand_name or ""
    cat = category or ""
    if name.startswith("FC_ALARM_") or cat == "预警":
        return 0
    if name.startswith("FC_UNIN_") or cat == "未入库":
        return 1
    if name.startswith("FC_STOCK_") or cat == "备库":
        return 2
    return 3


def load_items_for_resources(database, resource_names):
    """Item codes produced by operations that load any of ``resource_names``."""
    if not resource_names:
        return set()
    from django.db import connections

    resources = tuple(sorted({str(name) for name in resource_names if name}))
    if not resources:
        return set()
    placeholders = ",".join(["%s"] * len(resources))
    sql = (
        "SELECT DISTINCT coalesce(o.item_id, p.item_id) AS item_id "
        "FROM operationresource r "
        "JOIN operation o ON o.name = r.operation_id "
        "LEFT JOIN operation p ON p.name = o.owner_id "
        "WHERE r.resource_id IN (%s) "
        "AND coalesce(o.item_id, p.item_id) IS NOT NULL "
        "AND coalesce(o.item_id, p.item_id) <> ''" % placeholders
    )
    with connections[database].cursor() as cursor:
        cursor.execute(sql, resources)
        return {row[0] for row in cursor.fetchall() if row[0]}

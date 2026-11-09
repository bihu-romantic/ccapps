"""Inventory policy service. Computes targets, never invents sales forecasts."""

import math
from collections import defaultdict
from datetime import timedelta

from django.db import connections, transaction
from ccAPPSdb.common.models import Parameter
from ccAPPSdb.common.report import getCurrentDate
from ccAPPSdb.input.models import Buffer, ItemSupplier, ItemDistribution
from .models import Segment, BusinessRule, InventoryPolicy, InventoryResult
from .policy import DEFAULTS, calculate, compile_segment, matches, validate_parameters


def compute_inventory(database, apply=False):
    """Recompute an auditable snapshot, optionally publish buffer min/max."""
    current = getCurrentDate(database)
    history = int(Parameter.getValue("safetystock.history_days", database, "365"))
    if history < 2 or history > 3650:
        raise ValueError("历史窗口必须为2至3650天")
    default_lt = float(Parameter.getValue("safetystock.leadtime_days", database, "7"))
    if not math.isfinite(default_lt) or default_lt < 0:
        raise ValueError("默认提前期必须为非负有限数")
    global_params = {
        "fixed_order_cost": float(
            Parameter.getValue("inventoryplanning.fixed_order_cost", database, "20")
        ),
        "holding_cost": float(
            Parameter.getValue("inventoryplanning.holding_cost", database, "0.05")
        ),
    }
    validate_parameters(global_params)
    buffers = list(
        Buffer.objects.using(database)
        .exclude(type="infinite")
        .select_related("item", "location")
        .order_by("id")
    )
    segments = list(Segment.objects.using(database).order_by("name"))
    compiled = {s.pk: compile_segment(s.query) for s in segments}
    counts = defaultdict(int)
    overrides = {
        p.buffer_id: p.overrides for p in InventoryPolicy.objects.using(database).all()
    }
    rules = list(BusinessRule.objects.using(database).order_by("priority", "id"))
    for rule in rules:
        validate_parameters({rule.rule: rule.value})
    configs, protected = {}, set()
    for buffer in buffers:
        attrs = {}
        for entity in ("item", "location"):
            obj = getattr(buffer, entity)
            for field in ("name", "category", "subcategory", "cost", "description"):
                attrs[f"{entity}.{field}"] = getattr(obj, field, None)
        groups = {name for name, clauses in compiled.items() if matches(clauses, attrs)}
        for name in groups:
            counts[name] += 1
        params, sources = dict(global_params), {}
        selected = set()
        for rule in rules:
            if rule.segment_id in groups and rule.rule not in selected:
                params[rule.rule] = rule.value
                sources[rule.rule] = f"分组:{rule.segment_id} / 规则:{rule.pk}"
                selected.add(rule.rule)
        specific = overrides.get(buffer.pk, {})
        validate_parameters(specific)
        for key, value in specific.items():
            if value is not None:
                params[key], sources[key] = value, "单品覆盖"
        if (selected or specific) and not float(params.get("do_not_stock", 0)):
            protected.add((buffer.item_id, buffer.location_id))
        configs[buffer.pk] = (params, sources, groups)

    # Prefer the highest-priority effective route; alternate routes are not
    # summed, otherwise a single customer demand would be counted twice.
    paths = {}
    for model, method in (
        (ItemSupplier, "purchase"),
        (ItemDistribution, "distribution"),
    ):
        for route in (
            model.objects.using(database)
            .filter(priority__gt=0)
            .order_by("priority", "id")
        ):
            if route.effective_start and route.effective_start > current:
                continue
            if route.effective_end and route.effective_end <= current:
                continue
            key = (route.item_id, route.location_id)
            previous = paths.get(key)
            if previous is None or (route.priority, method, route.pk) < (
                previous[1].priority,
                previous[0],
                previous[1].pk,
            ):
                paths[key] = (method, route)

    def route_for(key):
        return paths.get(key) or paths.get((key[0], None))

    def parent(key):
        route = route_for(key)
        if route and route[0] == "distribution":
            return key[0], route[1].origin_id
        return None

    # Daily local sales + downstream dependent demand, including zero days
    # in the denominator. Only a single path is propagated per destination.
    series = defaultdict(lambda: defaultdict(float))
    with connections[database].cursor() as cursor:
        cursor.execute(
            """
            select item_id,location_id,due::date,sum(quantity)
            from demand where due >= %s and due < %s
              and coalesce(status,'open') not in ('canceled','quote')
            group by item_id,location_id,due::date
        """,
            [
                current.replace(hour=0, minute=0, second=0, microsecond=0)
                - timedelta(days=history),
                current.replace(hour=0, minute=0, second=0, microsecond=0),
            ],
        )
        for item, location, day, qty in cursor.fetchall():
            key, visited = (item, location), set()
            while key:
                if key in visited:
                    raise ValueError(f"调拨路径存在循环：{key}")
                visited.add(key)
                series[key][day] += float(qty)
                key = parent(key)

    def leadtime(key, visited=None):
        visited = set() if visited is None else set(visited)
        if key in visited:
            raise ValueError(f"提前期路径存在循环：{key}")
        visited.add(key)
        found = route_for(key)
        if not found:
            return default_lt, [
                "无有效采购/调拨路径，使用默认提前期；本版不展开制造BOM"
            ]
        method, route = found
        days = (route.leadtime or timedelta()).total_seconds() / 86400
        if method == "distribution" and parent(key) not in protected:
            extra, warnings = leadtime(parent(key), visited)
            return days + extra, warnings
        return days, []

    results = []
    for buffer in buffers:
        params, sources, groups = configs[buffer.pk]
        values = series[(buffer.item_id, buffer.location_id)]
        total = sum(values.values())
        mean = total / history
        variance = max(
            0,
            (sum(v * v for v in values.values()) - total * total / history)
            / (history - 1),
        )
        deviation = math.sqrt(variance)
        lt, warnings = leadtime((buffer.item_id, buffer.location_id))
        if params.get("leadtime_days") is not None:
            lt, warnings = float(params["leadtime_days"]), []
        computed = calculate(mean, deviation, lt, float(buffer.item.cost or 0), params)
        warnings += computed.pop("warnings")
        if buffer.batch:
            warnings.append("批次库存仅展示统计，不自动写入备货目标")
        if buffer.minimum_calendar_id or buffer.maximum_calendar_id:
            warnings.append("已有库存目标日历，仅展示统计，不覆盖日历")
        result = {
            "buffer_id": buffer.pk,
            "asof": current,
            "mean": mean,
            "deviation": deviation,
            "leadtime_days": lt,
            **computed,
            "details": {
                "history_days": history,
                "method": "历史日需求/正态周期服务水平/EOQ",
                "params": {**DEFAULTS, **params},
                "sources": sources,
                "segments": sorted(groups),
                "warnings": warnings,
                "apply_mode": "buffer.minimum=SS; buffer.maximum=SS+ROQ（补到目标上限）",
                "applied": False,
            },
        }
        results.append((buffer, result))

    # All validation is completed before publishing any result or target.
    with transaction.atomic(using=database):
        InventoryResult.objects.using(database).exclude(
            buffer_id__in=[b.pk for b, _ in results]
        ).delete()
        for buffer, result in results:
            if (
                apply
                and not buffer.batch
                and not buffer.minimum_calendar_id
                and not buffer.maximum_calendar_id
            ):
                Buffer.objects.using(database).filter(pk=buffer.pk).update(
                    minimum=result["ss"], maximum=result["ss"] + result["roq"]
                )
                result["details"]["applied"] = True
            key = result.pop("buffer_id")
            InventoryResult.objects.using(database).update_or_create(
                buffer_id=key, defaults=result
            )
        for segment in segments:
            Segment.objects.using(database).filter(pk=segment.pk).update(
                sku_count=counts[segment.pk]
            )
    return {
        "buffers": len(results),
        "applied": sum(r["details"]["applied"] for _, r in results),
    }

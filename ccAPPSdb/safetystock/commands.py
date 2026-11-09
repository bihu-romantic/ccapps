"""Safety stock from service level and historical demand variation.

SS = Z * (daily demand standard deviation) * sqrt(lead time in days).

Days with no orders count as zero. The lead time is the shortest purchasing
lead time of the item at that location, otherwise safetystock.leadtime_days.
Optional day-of-cover limits raise or cap the result. The value is stored on
buffer.minimum, which the supply plan already replenishes up to.
"""

import math
import os
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from django.db import DEFAULT_DB_ALIAS, connections
from django.utils.translation import gettext_lazy as _

from ccAPPSdb.common.commands import PlanTask, PlanTaskRegistry
from ccAPPSdb.common.models import Parameter
from ccAPPSdb.common.report import getCurrentDate

# Service level percent -> standard-normal quantile. Values in between are
# interpolated. 95% is about 1.64.
_Z = (
    (50.0, 0.0),
    (80.0, 0.8416212336),
    (90.0, 1.2815515655),
    (95.0, 1.6448536270),
    (97.5, 1.9599639845),
    (99.0, 2.3263478740),
    (99.5, 2.5758293035),
    (99.9, 3.0902323062),
)

_PARAMETERS = (
    (
        "safetystock.service_level",
        "95",
        "服务水平百分比。95 表示提前期内大约 95% 的时间不断料。",
    ),
    (
        "safetystock.history_days",
        "365",
        "用过去多少天的需求计算日均量和波动。没有订单的天按 0 计入。",
    ),
    (
        "safetystock.leadtime_days",
        "7",
        "物料在该地点没有采购提前期时使用的默认提前期，单位为天。",
    ),
    (
        "safetystock.min_cover_days",
        "0",
        "安全库存至少覆盖多少天的平均需求。0 表示不设下限。",
    ),
    (
        "safetystock.max_cover_days",
        "0",
        "安全库存最多覆盖多少天的平均需求。0 表示不设上限。上限低于下限时用上限。",
    ),
)


def _z(service_level):
    percent = float(service_level)
    if percent <= _Z[0][0]:
        return _Z[0][1]
    if percent >= _Z[-1][0]:
        return _Z[-1][1]
    for (low, z_low), (high, z_high) in zip(_Z, _Z[1:]):
        if low <= percent <= high:
            weight = (percent - low) / (high - low)
            return z_low + weight * (z_high - z_low)
    return _Z[-1][1]


def _number(value, default, smallest):
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    if number < smallest:
        return float(smallest)
    return number


def _ensure_parameters(database):
    for name, value, description in _PARAMETERS:
        Parameter.objects.using(database).get_or_create(
            name=name,
            defaults={"value": value, "description": description},
        )


def _lead_times(cursor):
    cursor.execute("""
        select item_id, location_id, extract(epoch from min(leadtime)) / 86400.0
        from itemsupplier
        where leadtime is not null and leadtime > interval '0'
        group by item_id, location_id
        """)
    specific = {}
    generic = {}
    for item_id, location_id, days in cursor.fetchall():
        if location_id:
            specific[(item_id, location_id)] = float(days)
        else:
            generic[item_id] = float(days)
    return specific, generic


def _cover(average, days):
    if days <= 0 or average <= 0:
        return None
    return average * days


@PlanTaskRegistry.register
class CalculateSafetyStock(PlanTask):
    description = "计算安全库存"
    sequence = 180
    label = (
        "safetystock",
        _("计算安全库存"),
        _("按服务水平和历史需求波动计算安全库存，写入缓冲的最低库存。"),
    )

    @classmethod
    def getWeight(cls, **kwargs):
        if "safetystock" in os.environ and "inventorypolicy" not in os.environ:
            return 1
        return -1

    @classmethod
    def run(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        import logging

        logger = logging.getLogger(__name__)
        _ensure_parameters(database)
        service_level = _number(
            Parameter.getValue("safetystock.service_level", database, "95"), 95, 50
        )
        history_days = int(
            _number(
                Parameter.getValue("safetystock.history_days", database, "365"),
                365,
                2,
            )
        )
        default_leadtime = _number(
            Parameter.getValue("safetystock.leadtime_days", database, "7"), 7, 1
        )
        min_cover_days = _number(
            Parameter.getValue("safetystock.min_cover_days", database, "0"), 0, 0
        )
        max_cover_days = _number(
            Parameter.getValue("safetystock.max_cover_days", database, "0"), 0, 0
        )
        z = _z(service_level)
        current = getCurrentDate(database)

        with connections[database].cursor() as cursor:
            specific, generic = _lead_times(cursor)
            cursor.execute(
                """
                with bounds as (
                    select %s::timestamptz as currentdate, %s::int as history_days
                ),
                days as (
                    select generate_series(
                        (select currentdate::date - history_days from bounds),
                        (select currentdate::date - 1 from bounds),
                        interval '1 day'
                    )::date as day
                ),
                sold as (
                    select demand.item_id, demand.location_id, demand.due::date as day,
                           sum(demand.quantity) as qty
                    from demand, bounds
                    where demand.due >= bounds.currentdate
                          - (bounds.history_days || ' days')::interval
                      and demand.due < bounds.currentdate
                      and coalesce(demand.status, 'open') not in ('canceled', 'quote')
                    group by demand.item_id, demand.location_id, demand.due::date
                )
                select buffer.item_id, buffer.location_id, coalesce(buffer.batch, ''),
                       avg(coalesce(sold.qty, 0)),
                       stddev_samp(coalesce(sold.qty, 0))
                from buffer
                cross join days
                left join sold
                  on sold.item_id = buffer.item_id
                 and sold.location_id = buffer.location_id
                 and sold.day = days.day
                where coalesce(buffer.type, 'default') <> 'infinite'
                group by buffer.item_id, buffer.location_id, coalesce(buffer.batch, '')
                """,
                [current, history_days],
            )
            rows = cursor.fetchall()

            updates = []
            for item_id, location_id, batch, average, deviation in rows:
                average = float(average or 0)
                deviation = float(deviation or 0)
                leadtime = specific.get((item_id, location_id))
                if leadtime is None:
                    leadtime = generic.get(item_id, default_leadtime)
                if leadtime < 1:
                    leadtime = default_leadtime
                safety = z * deviation * math.sqrt(leadtime)
                floor = _cover(average, min_cover_days)
                cap = _cover(average, max_cover_days)
                if floor is not None:
                    safety = max(safety, floor)
                if cap is not None:
                    safety = (
                        cap if floor is not None and cap < floor else min(safety, cap)
                    )
                quantized = Decimal(str(safety)).quantize(
                    Decimal("0.0001"), rounding=ROUND_HALF_UP
                )
                updates.append((quantized, datetime.now(), item_id, location_id, batch))

            if updates:
                cursor.executemany(
                    """
                    update buffer
                    set minimum = %s, lastmodified = %s
                    where item_id = %s and location_id = %s
                      and coalesce(batch, '') = %s
                    """,
                    updates,
                )

        logger.info(
            "Safety stock updated %s buffers at service level %s%% (Z=%s)",
            len(updates),
            service_level,
            round(z, 3),
        )


@PlanTaskRegistry.register
class CalculateInventoryPolicies(PlanTask):
    description = "计算库存策略 SS / ROQ"
    # Publish targets before the native buffer loader (sequence 101).
    sequence = 100.5
    label = (
        "inventorypolicy",
        _("计算库存策略 SS / ROQ"),
        _("应用物料分组、业务规则及单品覆盖，更新库存上下限后生成供应计划。"),
    )

    @classmethod
    def getWeight(cls, **kwargs):
        return 1 if "inventorypolicy" in os.environ else -1

    @classmethod
    def run(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        from .inventory import compute_inventory

        compute_inventory(database, apply=True)

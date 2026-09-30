"""Rewrite closed historical quotes to current 8% profit and 13% tax, then ±3% pretax jitter."""

from __future__ import annotations

import json
import os
import random
import sys
from decimal import Decimal, ROUND_HALF_UP

PROFIT_RATE = Decimal("0.08")
TAX_RATE = Decimal("0.13")
JITTER = Decimal("0.03")
SEED = 20260920
DATABASE = os.environ.get("REWRITE_DB", "scenario2")


def rd(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def money(quote: dict, *keys: str) -> Decimal:
    for key in keys:
        raw = quote.get(key)
        if raw not in (None, ""):
            try:
                return Decimal(str(raw))
            except Exception:
                continue
    return Decimal("0")


def rewrite_quote(quote: dict, demand_name: str) -> dict:
    data = dict(quote or {})
    cost = money(data, "subtotal_cost")
    if cost <= 0:
        cost = money(data, "total_cost") - money(data, "profit_amount")
    if cost <= 0:
        return data
    rng = random.Random(f"{SEED}:{demand_name}")
    factor = Decimal("1") + Decimal(str(rng.uniform(float(-JITTER), float(JITTER))))
    pretax = (cost * (Decimal("1") + PROFIT_RATE) * factor).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    profit = pretax - cost
    tax = (pretax * TAX_RATE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    taxed = pretax + tax
    applied = (profit / cost * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    data["subtotal_cost"] = rd(cost)
    data["profit_margin_applied"] = f"{applied}%"
    data["profit_amount"] = rd(profit)
    data["tax_rate_applied"] = "13%"
    data["tax_amount"] = rd(tax)
    data["total_cost"] = rd(pretax)
    data["total_with_tax"] = rd(taxed)
    return data


def run() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "djangosettings")
    sys.path.insert(0, "/app")
    import django
    from psycopg2.extras import Json

    django.setup()
    from django.db import connections, transaction

    conn = connections[DATABASE]
    updated_history = 0
    updated_demands = 0
    with transaction.atomic(using=DATABASE):
        with conn.cursor() as cursor:
            cursor.execute(
                """
                select h.ctid, h.demand_id, h.quote_data
                from quote_history h
                join demand d on d.name = h.demand_id
                where d.status = 'closed'
                """
            )
            rows = cursor.fetchall()
            for ctid, demand_id, quote_data in rows:
                quote = quote_data if isinstance(quote_data, dict) else json.loads(quote_data or "{}")
                patched = rewrite_quote(quote, demand_id)
                cursor.execute(
                    "update quote_history set quote_data = %s where ctid = %s",
                    [Json(patched), ctid],
                )
                updated_history += 1
            cursor.execute(
                """
                select name, quote
                from demand
                where status = 'closed' and quote is not null
                """
            )
            demands = cursor.fetchall()
            for name, quote in demands:
                data = quote if isinstance(quote, dict) else json.loads(quote or "{}")
                patched = rewrite_quote(data, name)
                cursor.execute(
                    "update demand set quote = %s where name = %s",
                    [Json(patched), name],
                )
                updated_demands += 1
    print(
        json.dumps(
            {
                "database": DATABASE,
                "profit_rate": str(PROFIT_RATE),
                "tax_rate": str(TAX_RATE),
                "jitter": str(JITTER),
                "updated_history": updated_history,
                "updated_demands": updated_demands,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    run()

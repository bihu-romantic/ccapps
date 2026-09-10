"""
Smart Quote Engine — intelligent pricing analysis and recommendation.

Uses historical quote data from QuoteHistory and closed Demand records
to provide data-driven pricing recommendations, volume discount tiers,
competitive comparisons, and customer pricing profiles.
"""

import math
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import mean, median, stdev

from django.db.models import Avg, Count, Max, Min, Q, StdDev, Sum
from django.utils import timezone

from ccAPPSdb.input.models.demand import Demand, QuoteHistory


class SmartQuoteEngine:
    """Stateless service for intelligent quote analysis."""

    # ------------------------------------------------------------------
    # 1. Historical Price Analysis
    # ------------------------------------------------------------------

    @staticmethod
    def analyze_historical_prices(database, item_id, customer_id=None, months=24):
        """
        Fetch and analyse historical quote data for a product.

        Returns:
            dict with:
            - item_id, sample_count
            - avg_unit_price, median_unit_price, min_price, max_price
            - price_stddev, cv (coefficient of variation)
            - monthly_trend: [{month, avg_price, count}, ...]
            - overall_trend: 'up'|'down'|'stable'
            - trend_pct: percentage change over period
        """
        cutoff = timezone.now() - timedelta(days=months * 30)

        qs = (
            QuoteHistory.objects
            .using(database)
            .filter(demand__item_id=item_id)
            .filter(created_at__gte=cutoff)
            .select_related('demand')
            .order_by('created_at')
        )

        if customer_id:
            qs = qs.filter(demand__customer_id=customer_id)

        records = list(qs)
        if not records:
            return {
                "item_id": item_id,
                "sample_count": 0,
                "message": "No historical quote data available for this product.",
                "confidence": "low",
            }

        # Extract unit prices
        prices = []
        for r in records:
            qty = float(r.demand.quantity or 1)
            total = float(r.quote_data.get("total_cost", 0))
            if total > 0:
                prices.append(total / qty)

        if not prices:
            return {
                "item_id": item_id,
                "sample_count": 0,
                "message": "Historical quotes lack valid total_cost data.",
                "confidence": "low",
            }

        n = len(prices)
        avg_price = mean(prices)
        med_price = median(prices)
        min_price = min(prices)
        max_price = max(prices)
        price_std = stdev(prices) if n >= 2 else 0.0
        cv = (price_std / avg_price * 100) if avg_price > 0 else 0.0

        # Monthly trend aggregation
        monthly = {}
        for r in records:
            total = float(r.quote_data.get("total_cost", 0))
            qty = float(r.demand.quantity or 1)
            if total > 0 and qty > 0:
                month_key = r.created_at.strftime("%Y-%m")
                monthly.setdefault(month_key, []).append(total / qty)

        monthly_trend = [
            {
                "month": m,
                "avg_unit_price": round(mean(vals), 2),
                "count": len(vals),
            }
            for m, vals in sorted(monthly.items())
        ]

        # Overall trend: compare first 3 months avg vs last 3 months avg
        if len(monthly_trend) >= 3:
            first_avg = mean(
                [m["avg_unit_price"] for m in monthly_trend[:3]]
            )
            last_avg = mean(
                [m["avg_unit_price"] for m in monthly_trend[-3:]]
            )
            if first_avg > 0:
                trend_pct = (last_avg - first_avg) / first_avg * 100
            else:
                trend_pct = 0.0
        else:
            trend_pct = 0.0

        if trend_pct > 5:
            direction = "up"
        elif trend_pct < -5:
            direction = "down"
        else:
            direction = "stable"

        confidence = "high" if n >= 20 else "medium" if n >= 5 else "low"

        return {
            "item_id": item_id,
            "sample_count": n,
            "avg_unit_price": round(avg_price, 2),
            "median_unit_price": round(med_price, 2),
            "min_price": round(min_price, 2),
            "max_price": round(max_price, 2),
            "price_stddev": round(price_std, 2),
            "cv_pct": round(cv, 1),
            "monthly_trend": monthly_trend,
            "overall_trend": direction,
            "trend_pct": round(trend_pct, 1),
            "confidence": confidence,
        }

    # ------------------------------------------------------------------
    # 2. Smart Pricing Recommendation
    # ------------------------------------------------------------------

    @staticmethod
    def recommend_pricing(database, demand_name):
        """
        Recommend optimal pricing for a specific demand order.

        Algorithm:
        1. Load the demand and its current quote data
        2. Find historical orders for same product
        3. Compute similarity-weighted average margin
        4. Adjust for quantity tier and customer history
        5. Return recommendation with confidence score

        Returns:
            dict with recommended_price, recommended_margin, confidence,
            historical_baseline, similar_orders_count, alternatives
        """
        try:
            demand = (
                Demand.objects.using(database)
                .select_related('customer', 'item')
                .get(name=demand_name)
            )
        except Demand.DoesNotExist:
            return {"error": f"Demand '{demand_name}' not found.", "confidence": "low"}

        item_id = demand.item_id
        quantity = float(demand.quantity or 1)
        customer_id = demand.customer_id

        # Get cost basis from existing quote or compute from item cost
        quote = demand.quote or {}
        cost_total = float(
            quote.get("subtotal_cost", 0)
            or quote.get("total_cost", 0)
        )
        if cost_total <= 0:
            # Fallback: use item cost * quantity
            item_cost = float(demand.item.cost or 0)
            cost_total = item_cost * quantity

        if cost_total <= 0:
            return {
                "demand": demand_name,
                "item": item_id,
                "quantity": quantity,
                "cost_total": 0,
                "message": "Cannot recommend pricing: no cost basis available.",
                "confidence": "low",
            }

        # Fetch historical data for this product
        cutoff = timezone.now() - timedelta(days=24 * 30)  # 24 months
        history = (
            QuoteHistory.objects
            .using(database)
            .filter(demand__item_id=item_id)
            .filter(created_at__gte=cutoff)
            .select_related('demand')
            .order_by('-created_at')
        )[:200]

        hist_records = list(history)
        if not hist_records:
            return {
                "demand": demand_name,
                "item": item_id,
                "quantity": quantity,
                "cost_total": round(cost_total, 2),
                "recommended_margin": 15.0,
                "recommended_price": round(cost_total * 1.15, 2),
                "message": "No historical data available. Using default 15% margin.",
                "confidence": "low",
                "historical_avg": None,
                "historical_range": None,
                "similar_orders_count": 0,
                "price_trend": "stable",
                "trend_pct": 0.0,
            }

        # Compute similarity-weighted margin
        margins = []
        weights = []
        qty_values = [float(r.demand.quantity or 1) for r in hist_records]
        max_log_qty = max(
            abs(math.log(max(qty_values))), abs(math.log(min(qty_values))), 1.0
        )

        for r in hist_records:
            qdata = r.quote_data or {}
            hist_qty = float(r.demand.quantity or 1)
            hist_total = float(qdata.get("total_cost", 0))
            hist_cost = float(qdata.get("subtotal_cost", 0) or hist_total * 0.85)

            if hist_cost <= 0:
                continue

            hist_margin = ((hist_total / hist_cost) - 1) * 100

            # Similarity score components
            product_match = 1.0  # same item
            qty_similarity = 1.0 - min(
                1.0,
                abs(math.log(quantity) - math.log(hist_qty)) / max_log_qty
            )
            customer_match = 1.0 if r.demand.customer_id == customer_id else 0.3
            months_ago = (timezone.now() - r.created_at).days / 30.0
            time_decay = math.exp(-months_ago / 12.0)

            score = (
                0.4 * product_match
                + 0.3 * qty_similarity
                + 0.15 * customer_match
                + 0.15 * time_decay
            )

            margins.append(hist_margin)
            weights.append(score)

        if not margins:
            return {
                "demand": demand_name,
                "item": item_id,
                "quantity": quantity,
                "cost_total": round(cost_total, 2),
                "recommended_margin": 15.0,
                "recommended_price": round(cost_total * 1.15, 2),
                "message": "Historical quotes lack valid cost data.",
                "confidence": "low",
            }

        # Weighted average margin
        total_weight = sum(weights)
        if total_weight > 0:
            weighted_margin = sum(m * w for m, w in zip(margins, weights)) / total_weight
        else:
            weighted_margin = mean(margins)

        # Clamp to reasonable range
        recommended_margin = max(5.0, min(50.0, weighted_margin))

        # Quantity adjustment
        if quantity >= 100:
            recommended_margin *= 0.85  # bulk discount
        elif quantity >= 50:
            recommended_margin *= 0.92

        recommended_price = cost_total * (1 + recommended_margin / 100.0)

        # Confidence
        n = len(margins)
        if n >= 20:
            confidence = "high"
        elif n >= 5:
            confidence = "medium"
        else:
            confidence = "low"

        # Historical baseline stats
        hist_totals = [
            float(r.quote_data.get("total_cost", 0))
            for r in hist_records
            if r.quote_data.get("total_cost")
        ]

        return {
            "demand": demand_name,
            "item": item_id,
            "customer": customer_id,
            "quantity": quantity,
            "cost_total": round(cost_total, 2),
            "recommended_margin": round(recommended_margin, 1),
            "recommended_price": round(recommended_price, 2),
            "recommended_unit_price": round(recommended_price / quantity, 2),
            "confidence": confidence,
            "historical_avg": round(mean(hist_totals), 2) if hist_totals else None,
            "historical_range": (
                [round(min(hist_totals), 2), round(max(hist_totals), 2)]
                if hist_totals
                else None
            ),
            "similar_orders_count": n,
            "price_trend": (
                "up" if recommended_margin > median(margins) + 3
                else "down" if recommended_margin < median(margins) - 3
                else "stable"
            ),
            "trend_pct": round(recommended_margin - median(margins), 1),
            "alternatives": [
                {
                    "scenario": "conservative",
                    "margin": round(recommended_margin * 1.15, 1),
                    "price": round(cost_total * (1 + recommended_margin * 1.15 / 100), 2),
                },
                {
                    "scenario": "balanced",
                    "margin": round(recommended_margin, 1),
                    "price": round(recommended_price, 2),
                },
                {
                    "scenario": "aggressive",
                    "margin": round(recommended_margin * 0.85, 1),
                    "price": round(cost_total * (1 + recommended_margin * 0.85 / 100), 2),
                },
            ],
        }

    # ------------------------------------------------------------------
    # 3. Volume Discount Calculation
    # ------------------------------------------------------------------

    @staticmethod
    def calculate_volume_discount(database, item_id, target_quantity):
        """
        Calculate recommended volume discount based on historical order patterns.

        Groups historical orders into quantity tiers and derives discount rates
        from actual transaction data.
        """
        cutoff = timezone.now() - timedelta(days=24 * 30)

        history = (
            QuoteHistory.objects
            .using(database)
            .filter(demand__item_id=item_id)
            .filter(created_at__gte=cutoff)
            .select_related('demand')
        )

        # Collect (quantity, unit_price, discount_rate) tuples
        data = []
        for r in history:
            qdata = r.quote_data or {}
            qty = float(r.demand.quantity or 1)
            total = float(qdata.get("total_cost", 0))
            discount = float(qdata.get("discount_amount", 0))
            price_before = float(
                qdata.get("total_cost", 0) + discount
            )
            if qty > 0 and total > 0:
                unit_price = total / qty
                discount_rate = (discount / price_before * 100) if price_before > 0 else 0
                data.append((qty, unit_price, discount_rate))

        if len(data) < 5:
            return {
                "item_id": item_id,
                "target_quantity": target_quantity,
                "message": "Insufficient historical data for volume discount analysis.",
                "tiers": [],
            }

        # Sort by quantity
        data.sort(key=lambda x: x[0])

        # Define tiers based on data distribution
        n = len(data)
        tier_boundaries = [
            data[int(n * 0.25)][0],
            data[int(n * 0.50)][0],
            data[int(n * 0.75)][0],
        ]

        tiers = []
        tier_labels = ["S", "M", "L", "XL"]
        tier_ranges = [
            (0, tier_boundaries[0]),
            (tier_boundaries[0], tier_boundaries[1]),
            (tier_boundaries[1], tier_boundaries[2]),
            (tier_boundaries[2], float("inf")),
        ]

        for label, (lo, hi) in zip(tier_labels, tier_ranges):
            tier_data = [d for d in data if lo <= d[0] < hi]
            if tier_data:
                avg_price = mean(d[1] for d in tier_data)
                avg_discount = mean(d[2] for d in tier_data)
                count = len(tier_data)
            else:
                avg_price = None
                avg_discount = None
                count = 0

            tiers.append({
                "tier": label,
                "min_qty": round(lo),
                "max_qty": round(hi) if hi != float("inf") else None,
                "avg_unit_price": round(avg_price, 2) if avg_price else None,
                "avg_discount_pct": round(avg_discount, 1) if avg_discount else None,
                "sample_count": count,
            })

        # Interpolate recommendation for target_quantity
        base_price = tiers[0]["avg_unit_price"] if tiers[0]["avg_unit_price"] else 0
        recommended_discount = 0.0

        for i, tier in enumerate(tiers):
            if (
                tier["min_qty"] <= target_quantity
                and (tier["max_qty"] is None or target_quantity < tier["max_qty"])
            ):
                recommended_discount = tier["avg_discount_pct"] or 0.0
                break

        if target_quantity >= tier_boundaries[-1]:
            recommended_discount = tiers[-1]["avg_discount_pct"] or 0.0

        return {
            "item_id": item_id,
            "target_quantity": target_quantity,
            "base_unit_price": round(base_price, 2),
            "recommended_discount_pct": round(recommended_discount, 1),
            "recommended_unit_price": (
                round(base_price * (1 - recommended_discount / 100), 2)
                if base_price > 0
                else None
            ),
            "tiers": tiers,
        }

    # ------------------------------------------------------------------
    # 4. Quote Comparison
    # ------------------------------------------------------------------

    @staticmethod
    def compare_quotes(database, demand_name):
        """
        Compare a demand's current quote against historical averages for the
        same product. Flags anomalies where key metrics deviate significantly.
        """
        try:
            demand = (
                Demand.objects.using(database)
                .select_related('item', 'customer')
                .get(name=demand_name)
            )
        except Demand.DoesNotExist:
            return {"error": f"Demand '{demand_name}' not found."}

        quote = demand.quote or {}
        if not quote or not quote.get("total_cost"):
            return {
                "demand": demand_name,
                "message": "This demand has no quote data yet. Run GenerateQuote first.",
            }

        cutoff = timezone.now() - timedelta(days=24 * 30)
        history = (
            QuoteHistory.objects
            .using(database)
            .filter(demand__item_id=demand.item_id)
            .filter(created_at__gte=cutoff)
            .exclude(demand__name=demand_name)
        )

        categories = [
            ("material_cost", "材料成本"),
            ("scrap_cost", "损耗成本"),
            ("resource_cost", "资源成本"),
            ("operation_cost", "工艺成本"),
            ("overhead_cost", "间接费用"),
            ("transport_cost", "运输成本"),
            ("profit_amount", "利润"),
            ("discount_amount", "折扣金额"),
            ("total_cost", "总金额"),
        ]

        comparison = {
            "demand": demand_name,
            "item": demand.item_id,
            "customer": demand.customer_id,
            "quantity": float(demand.quantity),
            "categories": {},
            "overall_assessment": "",
        }

        anomalies = []
        for key, label in categories:
            current_val = float(quote.get(key, 0))
            hist_vals = [
                float(h.quote_data.get(key, 0))
                for h in history
                if h.quote_data.get(key)
            ]

            if hist_vals and len(hist_vals) >= 3:
                avg_val = mean(hist_vals)
                std_val = stdev(hist_vals) if len(hist_vals) >= 2 else 0
                diff_pct = (
                    ((current_val - avg_val) / avg_val * 100)
                    if avg_val > 0
                    else 0
                )

                if std_val > 0 and abs(current_val - avg_val) > 2 * std_val:
                    flag = "significant_deviation"
                    anomalies.append(f"{label}显著偏离历史均值")
                elif std_val > 0 and abs(current_val - avg_val) > 1 * std_val:
                    flag = "minor_deviation"
                else:
                    flag = "normal"

                comparison["categories"][key] = {
                    "label": label,
                    "current": round(current_val, 2),
                    "historical_avg": round(avg_val, 2),
                    "historical_std": round(std_val, 2),
                    "diff_pct": round(diff_pct, 1),
                    "flag": flag,
                }
            else:
                comparison["categories"][key] = {
                    "label": label,
                    "current": round(current_val, 2),
                    "historical_avg": None,
                    "historical_std": None,
                    "diff_pct": None,
                    "flag": "no_data",
                }

        if anomalies:
            comparison["overall_assessment"] = f"发现 {len(anomalies)} 项异常：{'；'.join(anomalies)}"
        else:
            comparison["overall_assessment"] = "当前报价与历史均值一致"

        return comparison

    # ------------------------------------------------------------------
    # 5. Customer Pricing Profile
    # ------------------------------------------------------------------

    @staticmethod
    def analyze_customer_pricing(database, customer_id):
        """
        Build a pricing profile for a customer based on their order history.

        Returns:
            - total_orders, total_revenue
            - avg_margin (what margins they accept)
            - avg_discount (what discounts they receive)
            - price_sensitivity: 'low'|'medium'|'high'
            - preferred_products
            - margin_acceptance_range
        """
        cutoff = timezone.now() - timedelta(days=24 * 30)

        history = (
            QuoteHistory.objects
            .using(database)
            .filter(demand__customer_id=customer_id)
            .filter(created_at__gte=cutoff)
            .select_related('demand')
            .order_by('-created_at')
        )

        records = list(history)
        if not records:
            return {
                "customer": customer_id,
                "message": "No historical quote data for this customer.",
                "total_orders": 0,
            }

        margins = []
        discounts = []
        totals = []
        products = {}

        for r in records:
            qdata = r.quote_data or {}
            total = float(qdata.get("total_cost", 0))
            cost = float(qdata.get("subtotal_cost", 0))
            discount = float(qdata.get("discount_amount", 0))
            item = r.demand.item_id

            if total > 0:
                totals.append(total)
                if discount > 0:
                    discounts.append(discount)

            if cost > 0:
                margin = ((total / cost) - 1) * 100
                margins.append(margin)

            products[item] = products.get(item, 0) + 1

        n = len(totals)
        if n == 0:
            return {
                "customer": customer_id,
                "total_orders": n,
                "message": "No valid pricing data found.",
            }

        avg_margin = mean(margins) if margins else 0
        margin_range = [min(margins), max(margins)] if margins else [0, 0]

        # Price sensitivity: low variation in accepted margin = high sensitivity
        if len(margins) >= 3:
            margin_cv = stdev(margins) / abs(avg_margin) if avg_margin != 0 else 1
            if margin_cv < 0.15:
                sensitivity = "high"
            elif margin_cv < 0.30:
                sensitivity = "medium"
            else:
                sensitivity = "low"
        else:
            sensitivity = "unknown"

        # Preferred products (top 5)
        preferred = sorted(products.items(), key=lambda x: -x[1])[:5]

        # Customer tier
        total_revenue = sum(totals)
        if total_revenue > 100000:
            tier = "premium"
        elif total_revenue > 10000:
            tier = "standard"
        else:
            tier = "discount"

        return {
            "customer": customer_id,
            "total_orders": n,
            "total_revenue": round(total_revenue, 2),
            "customer_tier": tier,
            "avg_margin_pct": round(avg_margin, 1),
            "margin_acceptance_range": [round(m, 1) for m in margin_range],
            "avg_discount_amount": (
                round(mean(discounts), 2) if discounts else 0
            ),
            "price_sensitivity": sensitivity,
            "preferred_products": [
                {"item": p, "order_count": c} for p, c in preferred
            ],
            "sample_count": n,
        }

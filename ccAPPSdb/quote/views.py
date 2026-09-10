"""
REST API endpoints for Smart Quote.
"""

import json

from django.http import JsonResponse
from django.views import View

from ccAPPSdb.input.models.demand import Demand
from ccAPPSdb.input.models.customer import Customer
from .engine import SmartQuoteEngine


class SmartQuoteAnalyzeView(View):
    """GET /api/quote/analyze/<demand_name>/ — get smart recommendation."""

    def get(self, request, demand_name):
        engine = SmartQuoteEngine()
        result = engine.recommend_pricing(request.database, demand_name)
        return JsonResponse(result, json_dumps_params={"ensure_ascii": False})


class SmartQuoteHistoryView(View):
    """GET /api/quote/history/<item_id>/ — get historical price trend."""

    def get(self, request, item_id):
        months = int(request.GET.get("months", 24))
        customer_id = request.GET.get("customer_id")
        engine = SmartQuoteEngine()
        result = engine.analyze_historical_prices(
            request.database, item_id, customer_id=customer_id, months=months
        )
        return JsonResponse(result, json_dumps_params={"ensure_ascii": False})


class SmartQuoteCompareView(View):
    """GET /api/quote/compare/<demand_name>/ — compare quote vs. history."""

    def get(self, request, demand_name):
        engine = SmartQuoteEngine()
        result = engine.compare_quotes(request.database, demand_name)
        return JsonResponse(result, json_dumps_params={"ensure_ascii": False})


class SmartQuoteSimilarOrdersView(View):
    """GET /api/quote/similar/<demand_name>/ — audit history calibration samples."""

    def get(self, request, demand_name):
        try:
            demand = Demand.objects.using(request.database).get(name=demand_name)
        except Demand.DoesNotExist:
            return JsonResponse({"error": f"Demand '{demand_name}' not found."}, status=404)
        quote = demand.quote or {}
        return JsonResponse(
            {
                "demand": demand.name,
                "sample_count": quote.get("history_sample_count", 0),
                "suggested_total": quote.get("history_suggested_total"),
                "suggested_adjustment": quote.get("history_suggested_adjustment"),
                "samples": quote.get("history_samples", []),
            },
            json_dumps_params={"ensure_ascii": False},
        )


class SmartQuoteVolumeDiscountView(View):
    """GET /api/quote/volume-discount/<item_id>/ — volume discount analysis."""

    def get(self, request, item_id):
        try:
            quantity = float(request.GET.get("quantity", 1))
        except (ValueError, TypeError):
            quantity = 1.0
        engine = SmartQuoteEngine()
        result = engine.calculate_volume_discount(
            request.database, item_id, quantity
        )
        return JsonResponse(result, json_dumps_params={"ensure_ascii": False})


class SmartQuoteCustomerView(View):
    """GET /api/quote/customer/<customer_id>/ — customer pricing profile."""

    def get(self, request, customer_id):
        engine = SmartQuoteEngine()
        result = engine.analyze_customer_pricing(request.database, customer_id)
        return JsonResponse(result, json_dumps_params={"ensure_ascii": False})


class SmartQuoteApplyView(View):
    """POST /api/quote/apply/ — apply smart recommendation to a demand."""

    def post(self, request):
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON body."}, status=400)

        demand_name = data.get("demand_name")
        margin = data.get("margin_rate")
        discount = data.get("discount_rate")

        if not demand_name:
            return JsonResponse({"error": "demand_name is required."}, status=400)

        try:
            demand = Demand.objects.using(request.database).get(name=demand_name)
        except Demand.DoesNotExist:
            return JsonResponse(
                {"error": f"Demand '{demand_name}' not found."}, status=404
            )

        # Apply customer-level overrides
        customer = demand.customer
        if customer:
            if margin is not None:
                customer.margin_rate = margin
                customer.save(
                    using=request.database, update_fields=["margin_rate"]
                )
            if discount is not None:
                customer.discount_rate = discount
                customer.save(
                    using=request.database, update_fields=["discount_rate"]
                )

        return JsonResponse(
            {
                "status": "ok",
                "demand_name": demand_name,
                "applied_margin": margin,
                "applied_discount": discount,
                "message": "Pricing parameters applied. Re-run GenerateQuote to update the quote.",
            },
            json_dumps_params={"ensure_ascii": False},
        )

from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import PermissionDenied
from django.db import connections
from django.db.models import Sum
from django.db.models.functions import TruncMonth
from datetime import timedelta
from collections import defaultdict
from ccAPPSdb.input.models import Demand, OperationPlanMaterial
from django.shortcuts import render
from django.views.decorators.http import require_http_methods
from .inventory import compute_inventory
from .models import InventoryResult, Segment, BusinessRule


def dashboard_data(database, results):
    """Read real ledger and sales history in batches, without mock curves."""
    if not results:
        return {"rows": [], "transactions": 0, "purchases": 0, "transfers": 0}
    asof = max(r.asof for r in results)
    start = (asof.replace(day=1) - timedelta(days=365)).replace(day=1)
    history, events, orders = defaultdict(list), defaultdict(list), defaultdict(list)
    for sale in (
        Demand.objects.using(database)
        .filter(due__gte=start, due__lt=asof)
        .exclude(status__in=("canceled", "quote"))
        .annotate(month=TruncMonth("due"))
        .values("item_id", "location_id", "month")
        .annotate(qty=Sum("quantity"))
        .order_by("month")
    ):
        history[(sale["item_id"], sale["location_id"])].append(
            {"month": sale["month"].strftime("%Y-%m"), "quantity": float(sale["qty"])}
        )
    horizon = max(90, int(max(r.leadtime_days for r in results)) + 1)
    unique_orders, po, do = set(), set(), set()
    for flow in (
        OperationPlanMaterial.objects.using(database)
        .filter(flowdate__gte=asof, flowdate__lte=asof + timedelta(days=horizon))
        .exclude(operationplan__type="STCK")
        .select_related("operationplan")
        .order_by("flowdate", "id")
    ):
        key, order = (flow.item_id, flow.location_id), flow.operationplan
        events[key].append(
            {"date": flow.flowdate.isoformat(), "quantity": float(flow.quantity)}
        )
        if order.reference not in {o["reference"] for o in orders[key]}:
            orders[key].append(
                {
                    "reference": order.reference,
                    "type": order.type,
                    "status": order.status,
                    "quantity": float(flow.quantity),
                    "date": flow.flowdate.isoformat(),
                    "supplier": order.supplier_id or "",
                    "origin": order.origin_id or "",
                    "destination": order.location_id or "",
                }
            )
        unique_orders.add(order.reference)
        if order.type == "PO":
            po.add(order.reference)
        if order.type == "DO":
            do.add(order.reference)
    rows = []
    for result in results:
        b = result.buffer
        key = (b.item_id, b.location_id)
        onhand = float(b.onhand or 0)
        receipt_date = result.asof + timedelta(days=result.leadtime_days)
        projected = onhand + sum(
            e["quantity"] for e in events[key] if e["date"] <= receipt_date.isoformat()
        )
        ratio = min(onhand, projected) / result.ss if result.ss > 0 else None
        risk = (
            "critical"
            if ratio is not None and ratio < 0.25
            else ("attention" if ratio is not None and ratio < 1 else "healthy")
        )
        if risk == "healthy" and onhand > result.ss + result.roq and result.roq > 0:
            risk = "excess"
        rows.append(
            {
                "id": b.pk,
                "item": b.item_id,
                "location": b.location_id,
                "category": b.item.category or "未分类",
                "cost": float(b.item.cost or 0),
                "onhand": onhand,
                "projected": round(projected, 6),
                "ratio": ratio,
                "risk": risk,
                "mean": result.mean,
                "deviation": result.deviation,
                "leadtime": result.leadtime_days,
                "ss": result.ss,
                "roq": result.roq,
                "asof": result.asof.isoformat(),
                "calculated_at": result.calculated_at.isoformat(),
                "details": result.details,
                "history": history[key],
                "events": events[key],
                "orders": orders[key],
            }
        )
    return {
        "rows": rows,
        "transactions": len(unique_orders),
        "purchases": len(po),
        "transfers": len(do),
    }


@staff_member_required
@require_http_methods(["GET", "POST"])
def inventory_view(request):
    if not request.user.has_perm("safetystock.view_inventoryresult"):
        raise PermissionDenied
    database = request.database
    with connections[database].cursor() as cursor:
        tables = connections[database].introspection.table_names(cursor)
    if InventoryResult._meta.db_table not in tables:
        return render(
            request, "safetystock/inventory.html", {"unavailable": True}, status=503
        )
    context = {"title": "库存策略：安全库存与补货批量"}
    if request.method == "POST":
        if not request.user.has_perm("safetystock.change_inventorypolicy"):
            raise PermissionDenied
        action = request.POST.get("action")
        if action not in ("calculate", "apply"):
            return render(
                request, "safetystock/inventory.html", {"error": "未知操作"}, status=400
            )
        if action == "apply" and not request.user.has_perm("input.change_buffer"):
            raise PermissionDenied
        try:
            context["summary"] = compute_inventory(database, action == "apply")
        except (ValueError, TypeError) as exc:
            context["error"] = str(exc)
    context.update(
        results=InventoryResult.objects.using(database)
        .select_related("buffer__item", "buffer__location")
        .order_by("buffer__item_id", "buffer__location_id"),
        segments=Segment.objects.using(database).all(),
        rules=BusinessRule.objects.using(database).select_related("segment").all(),
    )
    context["dashboard"] = dashboard_data(database, list(context["results"]))
    context["can_calculate"] = request.user.has_perm(
        "safetystock.change_inventorypolicy"
    )
    context["can_apply"] = context["can_calculate"] and request.user.has_perm(
        "input.change_buffer"
    )
    return render(request, "safetystock/inventory.html", context)

from django.http import HttpResponse
from django.utils.html import conditional_escape
from django.utils.translation import gettext_lazy as _

from ccAPPSdb.common.dashboard import Dashboard, Widget


@Dashboard.register
class ExecuteWidget(Widget):
    name = "execute"
    title = _("创建计划")
    permissions = (("auth.generate_plan", "Can generate plans"),)
    tooltip = _("生成一个遵守所有约束的供应计划")
    asynchronous = True
    repeat = True
    size = 'md'
    url = "/execute/#runplan"

    javascript_before_repeat = """
        var tmp = [];
        $("div.card[data-cockpit-widget='execute'] div.card-body").find("input:checkbox, input:radio").each( function(i) {
          tmp.push($(this).is(":checked"));
        });
        """

    javascript_after_repeat = """
        $("div.card[data-cockpit-widget='execute'] div.card-body").find("input:checkbox, input:radio").each( function(i) {
          $(this).prop("checked", tmp[i]);
        });
        """

    @staticmethod
    def render(request):
        from ccAPPSdb.execute.management.commands.runplan import Command

        return HttpResponse(Command.getHTML(request, widget=True))


@Dashboard.register
class ExecuteTaskGroupWidget(Widget):
    name = "executegroup"
    title = _("Group tasks")
    permissions = (("auth.generate_plan", "Can generate plans"),)
    tooltip = _("Run a sequence of tasks.")
    asynchronous = True
    repeat = True
    size = 'md'
    url = "/execute/#scheduletasks"

    @staticmethod
    def render(request):
        from ccAPPSdb.execute.management.commands.scheduletasks import Command

        return HttpResponse(Command.getHTML(request, widget=True))


@Dashboard.register
class QuoteWidget(Widget):
    name = "quote"
    title = _("报价")
    tooltip = _("显示询价订单的成本组成：原材料、资源、工艺")
    asynchronous = True
    size = "lg"
    url = "/execute/#runplan"

    @staticmethod
    def render(request):
        from ccAPPSdb.input.models import Demand

        def esc(value):
            return conditional_escape("" if value is None else value)

        def money(value):
            try:
                return float(value or 0)
            except (TypeError, ValueError):
                return 0.0

        quote_demands = Demand.objects.using(request.database).filter(quote_enabled=True)

        if not quote_demands.exists():
            return HttpResponse(
                '<div class="alert alert-info">%s</div>'
                % _("暂无启用报价的销售订单。")
            )

        results = []
        for d in quote_demands:
            if d.quote and isinstance(d.quote, dict) and d.quote.get("total_cost"):
                results.append({"demand": d.name, **d.quote})
            else:
                # Show demand without quote data yet
                results.append({
                    "demand": d.name,
                    "customer": str(d.customer.name) if d.customer else "",
                    "item": str(d.item.name) if d.item else "",
                    "quantity": str(d.quantity),
                    "due": d.due.strftime("%Y-%m-%d %H:%M") if d.due else "",
                    "total_cost": "0",
                    "material_cost": "0",
                    "scrap_cost": "0",
                    "resource_cost": "0",
                    "operation_cost": "0",
                    "overhead_cost": "0",
                    "transport_cost": "0",
                    "profit_amount": "0",
                    "discount_amount": "0",
                    "material_breakdown": {},
                    "scrap_breakdown": {},
                    "resource_breakdown": {},
                    "operation_breakdown": {},
                    "overhead_breakdown": {},
                    "transport_breakdown": {},
                })

        if not results:
            return HttpResponse(
                '<div class="alert alert-info">%s</div>'
                % _("暂无启用报价的销售订单。")
            )

        # Build HTML table
        html = ['<div style="overflow-x:auto; font-size:13px;">']
        html.append(
            '<table class="table table-striped table-condensed" style="margin-bottom:0;">'
        )
        html.append(
            '<thead><tr>'
            '<th>%s</th>'
            '<th>%s</th>'
            '<th>%s</th>'
            '<th class="text-end">%s</th>'
            '<th class="text-end">%s</th>'
            '<th class="text-end">%s</th>'
            '<th class="text-end">%s</th>'
            '<th class="text-end">%s</th>'
            '<th class="text-end">%s</th>'
            '<th class="text-end">%s</th>'
            '<th class="text-end">%s</th>'
            '<th class="text-end">%s</th>'
            '<th class="text-end"><strong>%s</strong></th>'
            '<th>%s</th>'
            '</tr></thead><tbody>'
            % (
                _("订单"),
                _("客户"),
                _("产品"),
                _("数量"),
                _("材料"),
                _("损耗"),
                _("资源"),
                _("工艺"),
                _("制造费"),
                _("运输"),
                _("利润"),
                _("折扣"),
                _("未税总价"),
                _("交期"),
            )
        )

        for r in results:
            # --- Build breakdown row ---
            breakdown_parts = []
            # Material breakdown (blue)
            if r.get("material_breakdown"):
                items = ", ".join(
                    f"{esc(k)}:{esc(v)}" for k, v in r["material_breakdown"].items()
                )
                breakdown_parts.append(
                    '<span style="color:#337ab7;">▎%s: %s</span>'
                    % (esc(_("材料明细")), items)
                )
            # Scrap breakdown (red)
            if r.get("scrap_breakdown") and r.get("scrap_rate_applied", "0%") != "0%":
                items = ", ".join(
                    f"{esc(k)}:{esc(v)}" for k, v in r["scrap_breakdown"].items()
                )
                breakdown_parts.append(
                    '<span style="color:#d9534f;">▎%s (%s): %s</span>'
                    % (esc(_("损耗")), esc(r.get("scrap_rate_applied", "0%")), items)
                )
            # Resource breakdown (green)
            if r.get("resource_breakdown"):
                items = ", ".join(
                    f"{esc(k)}:{esc(v)}" for k, v in r["resource_breakdown"].items()
                )
                breakdown_parts.append(
                    '<span style="color:#5cb85c;">▎%s: %s</span>'
                    % (esc(_("资源明细")), items)
                )
            # Operation breakdown (orange)
            if r.get("operation_breakdown"):
                items = ", ".join(
                    f"{esc(k)}:{esc(v)}" for k, v in r["operation_breakdown"].items()
                )
                breakdown_parts.append(
                    '<span style="color:#f0ad4e;">▎%s: %s</span>'
                    % (esc(_("工艺明细")), items)
                )
            # Overhead info (purple)
            if r.get("overhead_rate_applied", "0%") != "0%":
                breakdown_parts.append(
                    '<span style="color:#8e44ad;">▎%s (%s): %s</span>'
                    % (esc(_("制造费用")), esc(r.get("overhead_rate_applied", "0%")), esc(r.get("overhead_cost", "0")))
                )
            # Transport breakdown (teal)
            if r.get("transport_breakdown"):
                items = ", ".join(
                    f"{esc(k)}:{esc(v)}" for k, v in r["transport_breakdown"].items()
                )
                breakdown_parts.append(
                    '<span style="color:#17a2b8;">▎%s: %s</span>'
                    % (esc(_("运输明细")), items)
                )
            # Profit info (dark green)
            if r.get("profit_margin_applied", "0%") != "0%":
                breakdown_parts.append(
                    '<span style="color:#28a745;">▎%s (%s): %s</span>'
                    % (esc(_("利润")), esc(r.get("profit_margin_applied", "0%")), esc(r.get("profit_amount", "0")))
                )
            # Discount info (dark red)
            if r.get("discount_rate_applied", "0%") != "0%":
                breakdown_parts.append(
                    '<span style="color:#c7254e;">▎%s (%s): %s</span>'
                    % (esc(_("折扣")), esc(r.get("discount_rate_applied", "0%")), esc(r.get("discount_amount", "0")))
                )
            if r.get("tax_rate_applied", "0%") != "0%":
                breakdown_parts.append(
                    '<span style="color:#6f42c1;">▎%s (%s): %s；%s: %s</span>'
                    % (
                        esc(_("税额")), esc(r.get("tax_rate_applied", "0%")),
                        esc(r.get("tax_amount", "0")), esc(_("含税总价")),
                        esc(r.get("total_with_tax", r.get("total_cost", "0"))),
                    )
                )
            if money(r.get("batch_inventory_value", 0)) > 0:
                breakdown_parts.append(
                    '<span style="color:#5f6b78;">▎%s: %s</span>'
                    % (
                        esc(_("批量剩余库存价值")),
                        esc(r.get("batch_inventory_value", "0")),
                    )
                )
            breakdown_html = "<br>".join(breakdown_parts)

            total = money(r.get("total_cost", 0))
            row_class = "table-warning" if total == 0 else ""

            html.append(
                '<tr class="%s">'
                '<td>%s</td>'
                '<td>%s</td>'
                '<td>%s</td>'
                '<td class="text-end">%s</td>'
                '<td class="text-end">%s</td>'
                '<td class="text-end">%s</td>'
                '<td class="text-end">%s</td>'
                '<td class="text-end">%s</td>'
                '<td class="text-end">%s</td>'
                '<td class="text-end">%s</td>'
                '<td class="text-end">%s</td>'
                '<td class="text-end">%s</td>'
                '<td class="text-end"><strong>%s</strong></td>'
                '<td>%s</td>'
                '</tr>'
                '<tr class="%s" style="font-size:11px;">'
                '<td colspan="14">%s</td>'
                '</tr>'
                % (
                    row_class,
                    esc(r.get("demand", "")),
                    esc(r.get("customer", "")),
                    esc(r.get("item", "")),
                    esc(r.get("quantity", "")),
                    esc(r.get("material_cost", "0")),
                    esc(r.get("scrap_cost", "0")),
                    esc(r.get("resource_cost", "0")),
                    esc(r.get("operation_cost", "0")),
                    esc(r.get("overhead_cost", "0")),
                    esc(r.get("transport_cost", "0")),
                    esc(r.get("profit_amount", "0")),
                    esc(r.get("discount_amount", "0")),
                    esc(r.get("total_cost", "0")),
                    esc(r.get("due", "")),
                    row_class,
                    breakdown_html if breakdown_html else esc(_("暂无成本明细")),
                )
            )

        html.append("</tbody></table></div>")
        return HttpResponse("\n".join(html))

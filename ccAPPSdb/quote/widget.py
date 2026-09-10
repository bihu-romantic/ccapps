"""
Smart Quote Dashboard Widget — intelligent pricing recommendations.
"""

from django.http import HttpResponse
from django.utils.html import conditional_escape
from django.utils.translation import gettext_lazy as _

from ccAPPSdb.common.dashboard import Dashboard, Widget
from ccAPPSdb.input.models.demand import Demand
from .engine import SmartQuoteEngine


@Dashboard.register
class SmartQuoteWidget(Widget):
    """Widget displaying smart pricing recommendations for quote-enabled demands."""

    name = "smartquote"
    title = _("智能报价分析")
    tooltip = _("基于历史数据的智能定价建议，包括利润率和批量折扣推荐")
    asynchronous = True
    size = "lg"

    @staticmethod
    def render(request):
        engine = SmartQuoteEngine()

        # Get all quote-enabled demands
        demands = (
            Demand.objects.using(request.database)
            .filter(quote_enabled=True)
            .select_related('customer', 'item')
            .order_by('due')
        )

        recommendations = []
        for d in demands:
            rec = engine.recommend_pricing(request.database, d.name)
            recommendations.append(rec)

        # Build HTML table
        html_parts = [
            '<div class="table-responsive" style="max-height:400px;overflow-y:auto;">',
            '<table class="table table-sm table-striped table-hover mb-0">',
            "<thead><tr>",
            '<th style="position:sticky;top:0;background:var(--bs-table-bg)">'
            + str(_("订单"))
            + "</th>",
            '<th style="position:sticky;top:0;background:var(--bs-table-bg)">'
            + str(_("产品"))
            + "</th>",
            '<th style="position:sticky;top:0;background:var(--bs-table-bg)">'
            + str(_("数量"))
            + "</th>",
            '<th style="position:sticky;top:0;background:var(--bs-table-bg)">'
            + str(_("当前成本"))
            + "</th>",
            '<th style="position:sticky;top:0;background:var(--bs-table-bg)">'
            + str(_("建议报价"))
            + "</th>",
            '<th style="position:sticky;top:0;background:var(--bs-table-bg)">'
            + str(_("建议利润率"))
            + "</th>",
            '<th style="position:sticky;top:0;background:var(--bs-table-bg)">'
            + str(_("历史均价"))
            + "</th>",
            '<th style="position:sticky;top:0;background:var(--bs-table-bg)">'
            + str(_("置信度"))
            + "</th>",
            "</tr></thead><tbody>",
        ]

        for rec in recommendations:
            if rec.get("error"):
                continue

            confidence = rec.get("confidence", "low")
            confidence_label = {"high": _("高"), "medium": _("中"), "low": _("低")}.get(
                confidence, confidence
            )

            # Color coding
            if confidence == "high":
                badge_class = "bg-success"
            elif confidence == "medium":
                badge_class = "bg-warning text-dark"
            else:
                badge_class = "bg-secondary"

            # Recommendation vs. historical avg
            rec_price = rec.get("recommended_price") or 0
            hist_avg = rec.get("historical_avg") or 0
            if hist_avg > 0:
                price_diff_pct = (rec_price - hist_avg) / hist_avg * 100
            else:
                price_diff_pct = 0

            diff_color = (
                "text-danger" if price_diff_pct > 10
                else "text-success" if price_diff_pct < -5
                else ""
            )

            html_parts.append("<tr>")
            html_parts.append(
                f"<td>{conditional_escape(rec.get('demand', ''))}</td>"
            )
            html_parts.append(
                f"<td>{conditional_escape(rec.get('item', ''))}</td>"
            )
            html_parts.append(
                f"<td class='text-end'>{rec.get('quantity', 0):.0f}</td>"
            )
            html_parts.append(
                f"<td class='text-end'>{rec.get('cost_total', 0):,.2f}</td>"
            )
            html_parts.append(
                f'<td class="text-end {diff_color}"><strong>{rec_price:,.2f}</strong></td>'
            )
            html_parts.append(
                f"<td class='text-end'>{rec.get('recommended_margin', 0):.1f}%</td>"
            )

            if hist_avg:
                html_parts.append(
                    f"<td class='text-end text-muted'>{hist_avg:,.2f}</td>"
                )
            else:
                html_parts.append(
                    '<td class="text-end text-muted">—</td>'
                )

            html_parts.append(
                f'<td class="text-center"><span class="badge {badge_class}">{conditional_escape(confidence_label)}</span>'
                f'<br><small class="text-muted">n={rec.get("similar_orders_count", 0)}</small></td>'
            )
            html_parts.append("</tr>")

            # Price trend arrow
            trend = rec.get("price_trend", "stable")
            if trend == "up":
                trend_icon = "&#9650;"
            elif trend == "down":
                trend_icon = "&#9660;"
            else:
                trend_icon = "&#9648;"

            trend_pct = rec.get("trend_pct", 0)

        html_parts.append("</tbody></table></div>")

        if not recommendations or all(r.get("error") for r in recommendations):
            html_parts = [
                '<div class="alert alert-info m-3">',
                str(_("暂无报价数据。请在销售订单中启用报价功能并运行计划。")),
                "</div>",
            ]

        return HttpResponse(
            "".join(html_parts) if isinstance(html_parts[0], str)
            else html_parts[0]
        )


@Dashboard.register
class SmartQuoteHistoryWidget(Widget):
    """Widget displaying historical pricing trends for products with quotes."""

    name = "smartquote_history"
    title = _("历史报价趋势")
    tooltip = _("各产品历史报价的价格走势和波动分析")
    asynchronous = True
    size = "md"

    @staticmethod
    def render(request):
        engine = SmartQuoteEngine()

        # Get distinct items that have quote_enabled demands
        items = (
            Demand.objects.using(request.database)
            .filter(quote_enabled=True)
            .values_list('item_id', flat=True)
            .distinct()
        )

        html_parts = [
            '<div class="p-2">',
            '<h6 class="mb-2">' + str(_("产品历史价格趋势")) + "</h6>",
        ]

        for item_id in items:
            if not item_id:
                continue
            analysis = engine.analyze_historical_prices(
                request.database, item_id, months=12
            )

            sample_count = analysis.get("sample_count", 0)
            if sample_count == 0:
                continue

            trend = analysis.get("overall_trend", "stable")
            if trend == "up":
                trend_color = "#dc3545"
                trend_icon = "&#9650;"
            elif trend == "down":
                trend_color = "#198754"
                trend_icon = "&#9660;"
            else:
                trend_color = "#6c757d"
                trend_icon = "&#9648;"

            html_parts.append(
                '<div class="card mb-2"><div class="card-body py-2 px-3">'
            )
            html_parts.append(
                f'<div class="d-flex justify-content-between align-items-center">'
            )
            html_parts.append(
                f"<strong>{conditional_escape(item_id)}</strong>"
            )
            html_parts.append(
                f'<span style="color:{trend_color}">{trend_icon} '
                f'{analysis.get("trend_pct", 0):+.1f}%</span>'
            )
            html_parts.append("</div>")

            # Mini stats row
            html_parts.append(
                '<div class="row small text-muted mt-1">'
            )
            html_parts.append(
                f'<div class="col-4">{_("均价")}: {analysis.get("avg_unit_price", 0):,.2f}</div>'
            )
            html_parts.append(
                f'<div class="col-4">{_("最低")}: {analysis.get("min_price", 0):,.2f}</div>'
            )
            html_parts.append(
                f'<div class="col-4">{_("最高")}: {analysis.get("max_price", 0):,.2f}</div>'
            )
            html_parts.append("</div>")

            html_parts.append(
                f'<div class="small text-muted">'
                f'{_("样本数")}: {sample_count} | '
                f'{_("波动率")}: {analysis.get("cv_pct", 0):.1f}%'
                f"</div>"
            )
            html_parts.append("</div></div>")

        html_parts.append("</div>")

        if not items:
            html_parts = [
                '<div class="alert alert-info m-3">',
                str(_("暂无报价数据。")),
                "</div>",
            ]

        return HttpResponse("".join(html_parts))

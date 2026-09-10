from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from ccAPPSdb.admin import data_site
from ccAPPSdb.boot import getAttributes
from ccAPPSdb.common.adminforms import MultiDBModelAdmin
from ccAPPSdb.forecast.models import Forecast, ForecastPlan, Measure


@admin.register(Forecast, site=data_site)
class Forecast_admin(MultiDBModelAdmin):
    model = Forecast
    raw_id_fields = ("customer", "item", "operation")
    help_url = "model-reference/forecast.html"
    fieldsets = (
        (
            None,
            {
                "fields": [
                    "name",
                    "item",
                    "location",
                    "customer",
                    "description",
                    "method",
                    "planned",
                    "discrete",
                    "category",
                    "subcategory",
                    "priority",
                    "minshipment",
                    "maxlateness",
                    "operation",
                ]
                + [a[0] for a in getAttributes(Forecast) if a[3]]
                + ["source"]
            },
        ),
    )
    tabs = [
        {
            "name": "edit",
            "label": _("edit"),
            "view": "admin:forecast_forecast_change",
            "permissions": "input.change_forecast",
        },
        {
            "name": "supplypath",
            "label": _("supply path"),
            "view": "supplypath_forecast",
        },
        {"name": "plan", "label": _("plan"), "view": "forecast_plan"},
        {
            "name": "detail",
            "label": _("plan detail"),
            "view": "forecast_plandetail",
        },
        {
            "name": "constraint",
            "label": _("why short or late?"),
            "view": "forecast_constraint",
        },
        {
            "name": "messages",
            "label": _("messages"),
            "view": "admin:forecast_forecast_comment",
        },
    ]
    save_on_top = True


@admin.register(ForecastPlan, site=data_site)
class ForecastPlan_admin(MultiDBModelAdmin):
    model = ForecastPlan
    exclude = ("id", "value")


@admin.register(Measure, site=data_site)
class Measure_admin(MultiDBModelAdmin):
    model = Measure
    help_url = "model-reference/forecast-measures.html"
    save_on_top = True
    fieldsets = (
        (
            None,
            {
                "fields": [
                    "name",
                    "label",
                    "description",
                    "type",
                    "mode_future",
                    "mode_past",
                    "compute_expression",
                    "update_expression",
                    "initially_hidden",
                    "formatter",
                    "discrete",
                    "defaultvalue",
                    "overrides",
                    "source",
                ]
            },
        ),
    )
    tabs = [
        {
            "name": "edit",
            "label": _("edit"),
            "view": "admin:forecast_measure_change",
            "permissions": "forecast.change_measure",
        },
        {
            "name": "messages",
            "label": _("messages"),
            "view": "admin:forecast_measure_comment",
        },
    ]

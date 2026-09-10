from django.utils.translation import gettext_lazy as _
from ccAPPSdb.menu import menu
from ccAPPSdb.forecast.models import Forecast, ForecastPlan, Measure
from ccAPPSdb.forecast.views import (
    OverviewReport,
    ForecastList,
    MeasureList,
    ForecastWizard,
)
from ccAPPSdb.input.models import Item, Location, Customer


# Adding reports.
menu.addItem(
    "supply_chain",
    "forecast report",
    url="/forecast/",
    report=OverviewReport,
    model=ForecastPlan,
    index=110,
    dependencies=[Forecast],
)
menu.addItem(
    "supply_chain",
    "forecast editor",
    url="/forecast/editor/",
    label=_("Forecast editor"),
    index=111,
    permission="auth.view_forecast_report",
    dependencies=[Forecast],
)
menu.addItem(
    "supply_chain",
    "forecast",
    url="/data/forecast/forecast/",
    report=ForecastList,
    index=1210,
    model=Forecast,
    dependencies=[Item, Location, Customer],
)
menu.addItem(
    "supply_chain",
    "measure",
    url="/data/forecast/measure/",
    report=MeasureList,
    index=1220,
    model=Measure,
    dependencies=[Item, Location, Customer],
    admin=True,
)

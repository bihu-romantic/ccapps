import os
from datetime import timedelta

import logging

from django.db import DEFAULT_DB_ALIAS
from django.utils.translation import gettext_lazy as _


from ccAPPSdb.common.commands import PlanTaskRegistry, PlanTask
from ccAPPSdb.common.models import Parameter
from ccAPPSdb.common.report import getCurrentDate


logger = logging.getLogger(__name__)


@PlanTaskRegistry.register
class ExportForecast(PlanTask):
    description = "Generate machine learning forecast data"
    sequence = 172

    @classmethod
    def getWeight(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        if "fcst" in os.environ and Parameter.getValue(
            "forecast.calendar", database, None
        ):
            return 1
        else:
            return -1

    @classmethod
    def run(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        import ccAPPS

        try:
            from orbit.models import ETS, DLT, KTR, LGT
            import pandas as pd
        except Exception:
            raise ImportError(
                "Please install the orbit-ml python package to use the ccAPPS ML forecasting module"
            )

        calendar = Parameter.getValue("forecast.calendar", database, None)
        currentdate = getCurrentDate(database)
        horizon_future = int(
            Parameter.getValue("forecast.Horizon_future", database, 365)
        )
        test = {
            "date": [
                i.start
                for i in ccAPPS.calendar(name=calendar).buckets
                if i.end >= currentdate
                and i.start <= currentdate + timedelta(days=horizon_future)
            ],
        }
        test["quantity"] = [None] * len(test["date"])
        test_pd = pd.DataFrame(test)

        minimal_training_size = 52 if calendar == "week" else 12

        for i in ccAPPS.demands():
            if isinstance(i, ccAPPS.demand_forecast) and i.methods == "automatic":
                train = {"date": [], "quantity": []}
                found = False

                for b in i.buckets:
                    if b.end >= currentdate:
                        break
                    if b.orderstotal + b.ordersadjustment > 0 or found:
                        if not found:
                            found = True
                        train["date"].append(b.start)
                        train["quantity"].append(b.orderstotal + b.ordersadjustment)
                    if not found:
                        continue

                if len(train["date"]) < minimal_training_size:
                    # too small to forecast, will be forecasted with statistical methods
                    continue

                try:
                    orbit_model = DLT(
                        response_col="quantity",
                        date_col="date",
                        seasonality=(52 if calendar == "week" else 12),
                    )

                    train_pd = pd.DataFrame(train)
                    orbit_model.fit(train_pd)
                    predicted_df = orbit_model.predict(df=test_pd, decompose=True)
                    index = 0
                    for j in i.members:
                        if j.start in test["date"]:
                            j.forecastbaseline = max(
                                0, predicted_df.iloc[index]["prediction"]
                            )
                            index += 1
                except Exception as e:
                    # silently move on and use the statistical forecast
                    print(
                        "skipping machine learning forecast calculation for %s: %s"
                        % (i.name, e)
                    )

import unittest

from django.conf import settings
from django.core import management

from ccAPPSdb.execute.tests import cookbooktest


class cookbook_forecast(cookbooktest):
    @unittest.skipUnless(
        "ccAPPSdb.forecast" in settings.INSTALLED_APPS, "App not activated"
    )
    def test_forecast_netting(self):
        self.loadExcel(
            settings.CCAPPS_HOME,
            "..",
            "doc",
            "examples",
            "forecasting",
            "forecast-netting.xlsx",
            webservice=True,
        )
        management.call_command(
            "runplan",
            plantype=1,
            constraint="capa,mfg_lt,po_lt",
            env="fcst,supply,nowebservice",
        )
        self.assertOperationplans(
            settings.CCAPPS_HOME,
            "..",
            "doc",
            "examples",
            "forecasting",
            "forecast-netting.expect",
        )

    @unittest.skipUnless(
        "ccAPPSdb.forecast" in settings.INSTALLED_APPS, "App not activated"
    )
    def test_middle_out_forecast(self):
        self.loadExcel(
            settings.CCAPPS_HOME,
            "..",
            "doc",
            "examples",
            "forecasting",
            "middle-out-forecast.xlsx",
            webservice=True,
        )
        management.call_command(
            "runplan",
            plantype=1,
            constraint="capa,mfg_lt,po_lt",
            env="fcst,supply,nowebservice",
        )
        self.assertOperationplans(
            settings.CCAPPS_HOME,
            "..",
            "doc",
            "examples",
            "forecasting",
            "middle-out-forecast.expect",
        )

    @unittest.skipUnless(
        "ccAPPSdb.forecast" in settings.INSTALLED_APPS, "App not activated"
    )
    def test_forecast_method(self):
        self.loadExcel(
            settings.CCAPPS_HOME,
            "..",
            "doc",
            "examples",
            "forecasting",
            "forecast-method.xlsx",
            webservice=True,
        )
        management.call_command(
            "runplan",
            plantype=1,
            constraint="capa,mfg_lt,po_lt",
            env="fcst,supply,nowebservice",
        )
        self.assertOperationplans(
            settings.CCAPPS_HOME,
            "..",
            "doc",
            "examples",
            "forecasting",
            "forecast-method.expect",
        )

    @unittest.skip("Temporarily disabled - intermittent failures")
    def test_forecast_with_missing_data(self):
        self.loadExcel(
            settings.CCAPPS_HOME,
            "..",
            "doc",
            "examples",
            "forecasting",
            "forecast-with-missing-data.xlsx",
            webservice=True,
        )
        management.call_command(
            "runplan",
            plantype=1,
            constraint="capa,mfg_lt,po_lt",
            env="fcst,supply,nowebservice",
        )
        self.assertOperationplans(
            settings.CCAPPS_HOME,
            "..",
            "doc",
            "examples",
            "forecasting",
            "forecast-with-missing-data.expect",
        )

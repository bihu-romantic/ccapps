from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class ForecastConfig(AppConfig):
    name = "ccAPPSdb.forecast"
    verbose_name = "forecasting"

    def ready(self):
        if "ccAPPSdb.webservice" not in settings.INSTALLED_APPS:
            raise ImproperlyConfigured(
                "Missing dependency: ccAPPSdb.forecast depends on ccAPPSdb.webservice"
            )

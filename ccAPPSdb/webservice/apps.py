import logging
import sys
import random
from threading import Thread
from time import sleep

from django.apps import AppConfig
from django.contrib.auth.signals import user_logged_in
from django.core import management
from django.db import DEFAULT_DB_ALIAS, connections, close_old_connections

from ccAPPSdb.common.utils import get_databases

logger = logging.getLogger(__name__)


def startWebService(request, **kwargs):
    """
    When a user logs in, we start loading all scenario models in memory if it isn't loaded already.
    """
    from ccAPPSdb.common.models import Scenario, Parameter

    # Random delay to avoid simultaneous web service starts
    sleep(random.uniform(0.0, 0.5))
    close_old_connections()

    active_scenarios = {
        i["name"]
        for i in Scenario.objects.using(DEFAULT_DB_ALIAS)
        .filter(status="In use")
        .values("name")
    }
    for i in get_databases():
        if (
            i in active_scenarios
            and Parameter.getValue("plan.webservice", i, "true").lower() == "true"
        ):
            try:
                management.call_command("runwebservice", database=i, daemon=True)
            except Exception as e:
                logger.warning("Can't launch webservice in scenario '%s': %s" % (i, e))
    connections.close_all()


def startWebServiceAsync(request, **kwargs):
    # Note: API requests don't start the web service
    if not hasattr(request, "api"):
        Thread(
            target=startWebService, args=(request,), kwargs=kwargs, daemon=True
        ).start()


class WebServiceConfig(AppConfig):
    name = "ccAPPSdb.webservice"
    verbose_name = "planning engine web service"

    def ready(self):
        # Register a signal handler when somebody logs in
        if "test" not in sys.argv:
            user_logged_in.connect(
                startWebServiceAsync, dispatch_uid=startWebServiceAsync
            )

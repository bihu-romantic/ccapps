import os
import sys

try:
    os.environ["LOCUST_SKIP_MONKEY_PATCH"] = "1"
    import gevent
    from locust import FastHttpUser, task, between
    from locust.env import Environment
    from locust.log import setup_logging
    import locust.stats

    locust_installed = True
except (ImportError, ValueError):
    # Skip of locust isn't installed
    locust_installed = False

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS

from ccAPPSdb import __version__


class Command(BaseCommand):
    help = """
        This command generates a stress test load on the ccAPPS webserver.

        You need to have the locust python library installed.
        """

    requires_system_checks = []

    def get_version(self):
        return __version__

    def add_arguments(self, parser):
        parser.add_argument(
            "--users", help="Number of simultaneous users.", type=int, default=10
        )
        parser.add_argument(
            "--server",
            help="URL of the server to test.",
            type=str,
            default="http://localhost:8000",
        )
        parser.add_argument(
            "--user",
            help="User for the test sessions.",
            type=str,
            default="admin",
        )
        parser.add_argument(
            "--password",
            help="Password for the test sessions.",
            type=str,
            default="admin",
        )

    def handle(self, **options):
        if not locust_installed:
            raise CommandError("The locust python libaray isn't available")

        locust.stats.CONSOLE_STATS_INTERVAL_SEC = 30

        user_classes = [StaticFiles, MaterialPlanner, ProductionPlanner]
        if "ccAPPSdb.forecast" in settings.INSTALLED_APPS:
            user_classes.append(DemandPlanner)
        setup_logging("INFO")
        ccAPPSUser.user = options["user"]
        ccAPPSUser.password = options["password"]
        ccAPPSUser.host = options["server"]
        env = Environment(user_classes=user_classes)
        runner = env.create_local_runner()
        sys.argv = sys.argv[:1]  # Needed because web ui parses sys.argv as well
        web_ui = env.create_web_ui("127.0.0.1", 8089)

        print("")
        print("Open your browser on http://127.0.0.1:8089/ to control your test.")
        print("")

        env.events.init.fire(environment=env, runner=runner, web_ui=web_ui)
        env.runner.start(options["users"], spawn_rate=100)
        gevent.spawn(locust.stats.stats_history, env.runner)
        gevent.spawn_later(30, locust.stats.stats_printer(env.stats))
        env.runner.greenlet.join()

    @staticmethod
    def getHTML(request):
        return None


if locust_installed:

    class ccAPPSUser(FastHttpUser):
        host = "http://localhost:8000"
        user = "admin"
        password = "admin"
        wait_time = between(0.1, 0.5)
        default_headers = {
            "accept-encoding": "gzip, deflate, br, zstd",
            "accept-language": "en",
            "Connection": "keep-alive",
        }

        def on_start(self):
            if (
                "ccAPPSdb.common.middleware.AutoLoginAsAdminUser"
                not in settings.MIDDLEWARE
            ):
                self.client.get("/", auth=(self.user, self.password))

    class StaticFiles(ccAPPSUser):
        weight = 1

        @task(1)
        def staticfiles(self):
            self.client.get(
                "/static/js/ccAPPS.min.js",
                headers=self.default_headers,
            )
            self.client.get(
                "/static/js/bootstrap.min.js",
                headers=self.default_headers,
            )
            self.client.get(
                "/static/js/jquery-3.6.0.min.js", headers=self.default_headers
            )
            self.client.get(
                "/static/js/jquery.jqgrid.min.js", headers=self.default_headers
            )
            self.client.get(
                "/static/css/earth/bootstrap.min.css",
                headers=self.default_headers,
            )
            self.client.get(
                "/static/img/ccAPPS.svg",
                headers=self.default_headers,
            )

    class MaterialPlanner(ccAPPSUser):
        weight = 5

        @task(10)
        def common(self):
            self.client.get(
                "/inbox/",
                headers=self.default_headers,
            )

        @task(10)
        def materialplannner(self):
            self.client.get(
                "/buffer/",
                headers=self.default_headers,
            )
            self.client.get(
                "/buffer/?format=json&rows=100&page=1",
                name="/buffer/?format=json",
                headers=self.default_headers,
            )

    class ProductionPlanner(ccAPPSUser):
        weight = 5

        @task(10)
        def SalesOrderList(self):
            self.client.get(
                "/data/input/demand/",
                headers=self.default_headers,
            )
            self.client.get(
                "/data/input/demand/?format=json&rows=100&page=1",
                name="/data/input/demand/?format=json",
                headers=self.default_headers,
            )

        @task(10)
        def ResourceReport(self):
            self.client.get(
                "/resource/",
                headers=self.default_headers,
            )
            self.client.get(
                "/resource/?format=json&rows=100&page=1",
                name="/resource/?format=json",
                headers=self.default_headers,
            )

        @task(1)
        def ExecutionScreen(self):
            self.client.get(
                "/execute/",
                headers=self.default_headers,
            )
            self.client.get(
                "/execute/?format=json&rows=100&page=1",
                name="/execute/?format=json",
                headers=self.default_headers,
            )

    class DemandPlanner(ccAPPSUser):
        weight = 5

        @task(1)
        def Forecastditor(self):
            self.client.get(
                "/forecast/",
                headers=self.default_headers,
            )
            self.client.get(
                "/forecast/?format=json&rows=100&page=1",
                name="/forecast/?format=json",
                headers=self.default_headers,
            )
            self.client.get(
                "/forecast/editor/",
                headers=self.default_headers,
            )
            self.client.get(
                "/forecast/locationtree/?measure=forecasttotal",
                headers=self.default_headers,
            )
            self.client.get(
                "/forecast/customertree/?measure=forecasttotal",
                headers=self.default_headers,
            )
            self.client.get(
                "/forecast/itemtree/?measure=forecasttotal",
                headers=self.default_headers,
            )
            self.client.get(
                "/forecast/detail/?measure=forecasttotal&item=&location=&customer=",
                name="/forecast/detail/",
                headers=self.default_headers,
            )

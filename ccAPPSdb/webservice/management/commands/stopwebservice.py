from datetime import datetime
import os

from http.client import HTTPConnection

from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS

from ccAPPSdb import VERSION
from ccAPPSdb.common.auth import getWebserviceAuthorization
from ccAPPSdb.common.middleware import _thread_locals
from ccAPPSdb.common.utils import get_databases
from ccAPPSdb.execute.models import Task
from ccAPPSdb.webservice.utils import checkRunning, waitTillNotRunning


class Command(BaseCommand):
    help = "This command stops the ccAPPS web service if it is running."

    requires_model_validation = False

    def get_version(self):
        return VERSION

    def add_arguments(self, parser):
        parser.add_argument(
            "--database", default=DEFAULT_DB_ALIAS, help="Nominates a specific database"
        )
        parser.add_argument(
            "--force",
            action="store_true",
            default=False,
            help="Force an immediate stop of the web service. By default the stop commands gracefully waits for the web service to be idle.",
        )
        parser.add_argument(
            "--wait",
            action="store_true",
            default=False,
            help="Wait until the service has effectively stopped",
        )
        parser.add_argument(
            "--task",
            type=int,
            help="Optional task identifier",
        )

    def handle(self, **options):
        # Pick up the options
        database = options["database"]
        if database not in get_databases():
            raise CommandError("No database settings known for '%s'" % database)

        # Update task if needed
        if options["task"]:
            try:
                task = Task.objects.all().using(database).get(pk=options["task"])
            except Exception:
                raise CommandError("Task identifier not found")
            if (
                task.started
                or task.finished
                or task.status != "Waiting"
                or task.name != "stopwebservice"
            ):
                raise CommandError("Invalid task identifier")
            task.status = "0%"
            task.started = datetime.now()
            task.processid = os.getpid()
            task.save(using=database)
        else:
            task = None

        # Connect to the url "/stop/"
        try:
            try:
                if "CCAPPS_TEST" in os.environ:
                    server = get_databases()[database]["TEST"].get("CCAPPS_PORT", None)
                else:
                    server = get_databases()[database].get("CCAPPS_PORT", None)
                if not server:
                    return
                conn = HTTPConnection(
                    server.replace("0.0.0.0", "localhost"), timeout=10
                )
                conn.request(
                    "POST",
                    "/stop/force/" if options["force"] else "/stop/",
                    headers={
                        "Authorization": "Bearer %s"
                        % getWebserviceAuthorization(
                            database=database, user="admin", exp=600
                        )
                    },
                )
                response = conn.getresponse()
                response.read()
                if not options["force"]:
                    conn.close()
            except Exception:
                # The service wasn't up
                if options["verbosity"] > 1:
                    print(
                        "Web service for database '%s' wasn't running or couldn't be shut down"
                        % database
                    )

            if options["wait"]:
                waitTillNotRunning(database=database)

            if checkRunning(database):
                try:
                    # Emergency brake
                    os.system("fuser -k %s/tcp" % server.split(":")[-1])
                except Exception:
                    pass

            if task:
                task.status = "Done"
        except Exception:
            if task:
                task.status = "Failed"
        finally:
            if task:
                task.finished = datetime.now()
                task.processid = None
                task.save(using=database)

    # Dummy index and getHTML method to assure the stopwebservice command
    # can be selected in the task list for the task scheduler
    index = 1504

    @staticmethod
    def getHTML(request):
        return None

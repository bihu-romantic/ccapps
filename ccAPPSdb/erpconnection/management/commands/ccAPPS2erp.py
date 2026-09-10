from datetime import datetime
import os
from psycopg2.extras import execute_batch

from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, connections
from django.template import Template, RequestContext
from django.utils.translation import gettext_lazy as _

from ccAPPSdb import __version__
from ccAPPSdb.common.models import User
from ccAPPSdb.common.utils import get_databases
from ccAPPSdb.execute.models import Task

from ...utils import getERPconnection


class Command(BaseCommand):
    help = """
      Update the ERP system with ccAPPS planning information.
      """

    # For the display in the execution screen
    title = _("Export data to %(erp)s") % {"erp": "erp"}

    # For the display in the execution screen
    index = 1500

    requires_system_checks = []

    def get_version(self):
        return __version__

    def add_arguments(self, parser):
        parser.add_argument("--user", help="User running the command")
        parser.add_argument(
            "--database",
            default=DEFAULT_DB_ALIAS,
            help="Nominates the ccAPPS database to load",
        )
        parser.add_argument(
            "--task",
            type=int,
            help="Task identifier (generated automatically if not provided)",
        )

    @staticmethod
    def getHTML(request):
        if "ccAPPSdb.erpconnection" in settings.INSTALLED_APPS:
            context = RequestContext(request)

            template = Template(
                """
        {% load i18n %}
        <form role="form" method="post" action="{{request.prefix}}/execute/launch/erp2ccAPPS/">{% csrf_token %}
        <table>
          <tr>
            <td style="vertical-align:top; padding: 15px">
               <button  class="btn btn-primary"  type="submit" value="{% trans "launch"|capfirst %}">{% trans "launch"|capfirst %}</button>
            </td>
            <td  style="padding: 0px 15px;">将 ccAPPS 排程数据导出到 ERP。
            </td>
          </tr>
        </table>
        </form>
      """
            )
            return template.render(context)
        else:
            return None

    def handle(self, **options):
        """
        Uploads approved operationplans to the ERP system.
        """

        # Select the correct ccAPPS scenario database
        self.database = options["database"]
        if self.database not in get_databases().keys():
            raise CommandError("No database settings known for '%s'" % self.database)
        self.cursor_ccAPPS = connections[self.database].cursor()

        # ccAPPS user running this task
        if options["user"]:
            try:
                self.user = (
                    User.objects.all()
                    .using(self.database)
                    .get(username=options["user"])
                )
            except Exception:
                raise CommandError("User '%s' not found" % options["user"])
        else:
            self.user = None

        # ccAPPS task identifier
        if options["task"]:
            try:
                self.task = (
                    Task.objects.all().using(self.database).get(pk=options["task"])
                )
            except Exception:
                raise CommandError("Task identifier not found")
            if (
                self.task.started
                or self.task.finished
                or self.task.status != "Waiting"
                or self.task.name != "ccAPPS2erp"
            ):
                raise CommandError("Invalid task identifier")
        else:
            now = datetime.now()
            self.task = Task(
                name="ccAPPS2erp",
                submitted=now,
                started=now,
                status="0%",
                user=self.user,
            )
        self.task.processid = os.getpid()
        self.task.save(using=self.database)

        try:
            # Open database connection
            print("Connecting to the ERP database")
            with getERPconnection() as erp_connection:
                self.cursor_erp = erp_connection.cursor(self.database)
                try:
                    self.extractPurchaseOrders()
                    self.task.status = "33%"
                    self.task.save(using=self.database)

                    self.extractDistributionOrders()
                    self.task.status = "66%"
                    self.task.save(using=self.database)

                    self.extractManufacturingOrders()
                    self.task.status = "100%"
                    self.task.save(using=self.database)

                    # Optional extra planning output the ERP might be interested in:
                    #  - planned delivery date of sales orders
                    #  - safety stock (Enterprise Edition only)
                    #  - reorder quantities (Enterprise Edition only)
                    #  - forecast (Enterprise Edition only)
                    self.task.status = "Done"
                finally:
                    self.cursor_erp.close()
        except Exception as e:
            self.task.status = "Failed"
            self.task.message = "Failed: %s" % e
        self.task.finished = datetime.now()
        self.task.processid = None
        self.task.save(using=self.database)
        self.cursor_ccAPPS.close()

    def extractPurchaseOrders(self):
        """
        Export purchase orders from ccAPPS.
        We export:
          - approved purchase orders.
          - proposed purchase orders that start within the next day and with a total cost less than 500$.
        """
        print("Start exporting purchase orders")
        self.cursor_ccAPPS.execute(
            """
      select
        item_id, location_id, supplier_id, quantity, startdate, enddate
      from operationplan
      inner join item on item_id = item.name
      where type = 'PO'
        and (
          status = 'approved'
          or (status = 'proposed' and quantity * cost < 500 and startdate < now() + interval '1 day')
          )
      order by supplier_id
      """
        )
        output = [i for i in self.cursor_ccAPPS.fetchall()]
        execute_batch(
            self.cursor_erp,
            """
            insert into test
            (item, location, location2, quantity, startdate, enddate)
            values (?, ?, ?, ?, ?, ?)
            """,
            output,
        )

    def extractDistributionOrders(self):
        """
        Export distribution orders from ccAPPS.
        We export:
          - approved distribution orders.
          - proposed distribution orders that start within the next day and with a total cost less than 500$.
        """
        print("Start exporting distribution orders")
        self.cursor_ccAPPS.execute(
            """
      select
        item_id, destination_id, origin_id, quantity, startdate, enddate
      from operationplan
      inner join item on item_id = item.name
      where type = 'DO'
        and (
          status = 'approved'
          or (status = 'proposed' and quantity * cost < 500 and startdate < now() + interval '1 day')
          )
      order by origin_id, destination_id
      """
        )
        output = [i for i in self.cursor_ccAPPS.fetchall()]
        execute_batch(
            self.cursor_erp,
            """
            insert into test
            (item, location, location2, quantity, startdate, enddate)
            values (%s, %s, %s, %s, %s, %s)
            """,
            output,
        )

    def extractManufacturingOrders(self):
        """
        Export manufacturing orders from ccAPPS.
        We export:
          - approved manufacturing orders.
          - proposed manufacturing orders that start within the next day.
        """
        print("Start exporting manufacturing orders")
        self.cursor_ccAPPS.execute(
            """
      select
        item_id, location_id, operation_id, quantity, startdate, enddate
      from operationplan
      where type = 'MO'
        and (
          status = 'approved'
          or (status = 'proposed' and startdate < now() + interval '1 day')
          )
      order by operation_id
      """
        )
        output = [i for i in self.cursor_ccAPPS.fetchall()]
        execute_batch(
            self.cursor_erp,
            """
            insert into test
            (item, location, location2, quantity, startdate, enddate)
            values (%s, %s, %s, %s, %s, %s)
            """,
            output,
        )

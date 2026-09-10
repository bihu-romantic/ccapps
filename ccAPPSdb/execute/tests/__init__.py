import os

from django.conf import settings
from django.core import management
from django.http.response import StreamingHttpResponse
from django.test import TransactionTestCase

from ccAPPSdb.common.models import User, Notification
import ccAPPSdb.input


class cookbooktest(TransactionTestCase):
    def setUp(self):
        os.environ["CCAPPS_TEST"] = "YES"
        if not User.objects.filter(username="admin").count():
            User.objects.create_superuser("admin", "your@company.com", "admin")
        super().setUp()

    def tearDown(self):
        Notification.wait()
        del os.environ["CCAPPS_TEST"]
        super().tearDown()

    def loadExcel(self, *filepath, webservice=False):
        # Login
        if webservice:
            os.environ["CCAPPS_TEST"] = "webservice"
            if "nowebservice" in os.environ:
                del os.environ["nowebservice"]
        else:
            os.environ["CCAPPS_TEST"] = "YES"
        if not User.objects.filter(username="admin").count():
            User.objects.create_superuser("admin", "your@company.com", "admin")
        self.client.login(username="admin", password="admin")
        try:
            with open(os.path.join(*filepath), "rb") as myfile:
                response = self.client.post(
                    "/execute/launch/importworkbook/", {"spreadsheet": myfile}
                )
                if not isinstance(response, StreamingHttpResponse):
                    raise Exception("expected a streaming response")
                for rec in response.streaming_content:
                    rec
        except Exception as e:
            self.fail("Can't load excel file: %s" % e)
        self.assertEqual(response.status_code, 200)
        self.client.logout()
        if webservice:
            management.call_command("stopwebservice", wait=True, force=True)

    def writeResults(self, resultpath, opplans):
        basename = resultpath[-1].replace(".expect", ".out")
        with open(os.path.join(settings.CCAPPS_LOGDIR, basename), "wt") as outputfile:
            print("\n  Wrote results to logs/%s" % basename)
            for i in opplans:
                print(i.strip(), file=outputfile)

    def assertOperationplans(self, *resultpath):
        opplans = sorted(
            [
                "%s,%s,%s,%s%s"
                % (
                    i.name,
                    i.startdate,
                    i.enddate,
                    round(i.quantity, 1),
                    ",%s " % i.batch if i.batch else "",
                )
                for i in ccAPPSdb.input.models.OperationPlan.objects.order_by(
                    "name", "startdate", "quantity", "batch"
                ).only("name", "startdate", "enddate", "quantity", "batch")
            ],
            key=lambda s: s.lower(),
        )
        row = 0
        maxrow = len(opplans)
        with open(os.path.join(*resultpath), "r") as f:
            for line in f:
                if not line.strip():
                    continue
                if row >= maxrow or opplans[row].strip() != line.strip():
                    self.writeResults(resultpath, opplans)
                    if row < maxrow:
                        self.fail(
                            "Difference in expected results on line %s" % (row + 1)
                        )
                    else:
                        self.fail("Less output rows than expected")
                row += 1
        if row != maxrow:
            self.writeResults(resultpath, opplans)
            self.fail("More output rows than expected")

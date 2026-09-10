from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from ccAPPSdb.common.menus import MenuItem
from ccAPPSdb.menu import menu

from .views import ReportList
from .models import SQLReport


def MyReports(request):
    if not request.user.has_perm(
        "reportmanager.view_sqlreport"
    ) and not request.user.has_perm("reportmanager.add_sqlreport"):
        return []
    result = []
    index = 1
    for x in (
        SQLReport.objects.all()
        .using(request.database)
        .filter(Q(user=request.user) | Q(public=True))
        .order_by("name")
    ):
        result.append(
            (
                index,
                x.name,
                MenuItem(
                    x.name,
                    url="/reportmanager/%s/" % x.id,
                    label=x.name,
                    index=index,
                    prefix=request.prefix,
                ),
            )
        )
        index += 1
    return result


menu.addGroup("myreports", label=_("my reports"), index=750)
menu.addItem("myreports", "myreports", callback=MyReports, index=100)
menu.addItem("myreports", "data", separator=True, index=1000)
menu.addItem(
    "myreports",
    "reportmanager",
    url="/data/reportmanager/sqlreport/",
    report=ReportList,
    permission="reportmanager.change_sqlreport",
    model=SQLReport,
    index=1100,
    admin=True,
)

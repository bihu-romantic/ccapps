from django.utils.translation import gettext_lazy as _

from ccAPPSdb.menu import menu
from .views import TaskReport, PlanEdit

menu.addItem(
    "execute",
    "data management",
    url="/execute/data/",
    label=_("数据导入导出"),
    index=90,
)

menu.addItem(
    "execute",
    "execute",
    url="/execute/",
    label=_("Execute"),
    report=TaskReport,
    index=100,
)

menu.addItem(
    "execute",
    "planningboard",
    url="/execute/planningboard/",
    label=_("计划编辑器"),
    report=PlanEdit,
    index=110,
)

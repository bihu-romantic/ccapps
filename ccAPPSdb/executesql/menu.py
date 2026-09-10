from django.utils.translation import gettext_lazy as _

from ccAPPSdb.menu import menu
from .views import ExecuteSQL

menu.addGroup("myreports", label=_("my reports"), index=750)
menu.addItem(
    "myreports",
    "executesql",
    url="/executesql/",
    report=ExecuteSQL,
    label=_("Execute SQL"),
    index=1200,
)

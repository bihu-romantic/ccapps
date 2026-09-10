from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from ccAPPSdb.admin import data_site
from ccAPPSdb.common.adminforms import MultiDBModelAdmin
from django.forms import ModelForm

from .models import SQLReport


class SQLReportForm(ModelForm):
    class Meta:
        model = SQLReport
        fields = "__all__"


@admin.register(SQLReport, site=data_site)
class SQLReport_admin(MultiDBModelAdmin):
    model = SQLReport
    exclude = ("id",)
    save_on_top = True
    fieldsets = ((None, {"fields": (("name"), ("description"), ("public"), ("sql"))}),)

    form = SQLReportForm

    tabs = [
        {
            "name": "edit",
            "label": _("edit"),
            "view": "admin:reportmanager_sqlreport_change",
            "permissions": "reportmanager.change_sqlreport",
        },
        {
            "name": "messages",
            "label": _("messages"),
            "view": "admin:reportmanager_sqlreport_comment",
        },
    ]

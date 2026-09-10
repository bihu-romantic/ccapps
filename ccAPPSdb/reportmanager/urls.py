from django.urls import re_path

from ccAPPSdb import mode

# Automatically add these URLs when the application is installed
autodiscover = True

if mode == "WSGI":
    from . import views

    urlpatterns = [
        re_path(
            r"^reportmanager/schema/$",
            views.getSchema,
            name="reportmanager_schema",
        ),
        re_path(
            r"^reportmanager/(.+)/$",
            views.ReportManager.as_view(),
            name="reportmanager_id",
        ),
        re_path(
            r"^reportmanager/$",
            views.ReportManager.as_view(),
        ),
        re_path(
            r"^data/reportmanager/sqlreport/add/$",
            views.ReportManager.as_view(),
        ),
        re_path(
            r"^data/reportmanager/sqlreport/$",
            views.ReportList.as_view(),
        ),
    ]

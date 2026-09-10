from django.urls import re_path

from ccAPPSdb import mode

# Automatically add these URLs when the application is installed
autodiscover = True

if mode == "WSGI":
    from . import views

    urlpatterns = [
        re_path(r"^executesql/$", views.ExecuteSQL.as_view(), name="executesql")
    ]

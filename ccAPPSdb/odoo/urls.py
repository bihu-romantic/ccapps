from django.urls import re_path

from ccAPPSdb import mode

# Automatically add these URLs when the application is installed
autodiscover = True

if mode == "WSGI":
    from . import views

    urlpatterns = [
        re_path(r"^erp/upload/$", views.Upload, name="erp_upload"),
        re_path(r"^odoo/submit/$", views.SubmittedFromOdoo, name="odoo_submitted"),
    ]

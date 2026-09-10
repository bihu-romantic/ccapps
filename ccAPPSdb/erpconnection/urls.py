from django.urls import re_path

from ccAPPSdb import mode

# Automatically add these URLs when the application is installed
autodiscover = True

if mode == "WSGI":
    from . import views

    rlpatterns = [
        # Model list reports, which override standard admin screens
        re_path(r"^erp/upload/$", views.Upload, name="erp_upload")
    ]

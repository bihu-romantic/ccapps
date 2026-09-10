from django.conf import settings
from django.urls import re_path, path, include
from django.views.generic.base import RedirectView

from ccAPPSdb import mode

from .views import login

autodiscover = True

if mode == "WSGI":

    if "allauth" in settings.INSTALLED_APPS:
        urlpatterns = [
            re_path(r"^data/logout/$", RedirectView.as_view(url="/accounts/logout/")),
            re_path(r"^data/login/$", login, name="user_login"),
            path("accounts/", include("allauth.urls")),
        ]

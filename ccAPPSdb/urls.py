from importlib import import_module

from django.urls import include, re_path
from django.conf import settings
from django.views.generic.base import RedirectView
from django.views.i18n import JavaScriptCatalog

from ccAPPSdb.admin import data_site
from ccAPPSdb.webservice.proxy import webservice_proxy

urlpatterns = [
    re_path(
        r"^svc/(?P<scenario>[^/]+)/(?P<path>.*)$",
        webservice_proxy,
    ),
    # Redirect admin index page /data/ to /
    re_path(r"^data/$", RedirectView.as_view(url="/")),
    # Handle browser icon and robots.txt
    re_path(r"favicon\.ico$", RedirectView.as_view(url="/static/favicon.ico")),
    re_path(r"robots\.txt$", RedirectView.as_view(url="/static/robots.txt")),
]
svcpatterns = []

# Custom handlers for error pages.
handler404 = "ccAPPSdb.common.views.handler404"
handler500 = "ccAPPSdb.common.views.handler500"

# Adding urls for each installed application.
for app in settings.INSTALLED_APPS:
    try:
        mod = import_module("%s.urls" % app)
        if getattr(mod, "autodiscover", False):
            if hasattr(mod, "urlpatterns"):
                urlpatterns += mod.urlpatterns
            if hasattr(mod, "svcpatterns"):
                svcpatterns += mod.svcpatterns
    except ImportError as e:
        # Silently ignore if the missing module is called urls
        if "urls" not in str(e):
            raise e

# Admin pages, and the Javascript i18n library.
# It needs to be added as the last item since the applications can
# hide/override some admin urls.
urlpatterns += [
    re_path(
        r"^data/jsi18n/$",
        JavaScriptCatalog.as_view(),
    ),
    re_path(
        r"^admin/jsi18n/$",
        JavaScriptCatalog.as_view(),
    ),
    re_path(r"^data/", data_site.urls),
    re_path(r"^api-auth/", include("rest_framework.urls", namespace="rest_framework")),
]

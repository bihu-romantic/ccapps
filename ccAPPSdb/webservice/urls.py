from django.urls import re_path

from ccAPPSdb import mode

autodiscover = True

if mode == "ASGI":
    from . import services

    svcpatterns = [
        re_path(r"^stop/$", services.StopService.as_asgi()),
        re_path(r"^stop/force/$", services.StopService.as_asgi()),  # No difference
        re_path(r"^ping/$", services.PingService.as_asgi()),
    ]

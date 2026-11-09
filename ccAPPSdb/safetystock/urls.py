from django.urls import re_path
from .views import inventory_view

autodiscover = True
urlpatterns = [
    re_path(r"^inventory/policies/$", inventory_view, name="inventory_policies")
]

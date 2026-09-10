from django.urls import re_path
from ccAPPSdb.assistant import views

autodiscover = True

urlpatterns = [
    re_path(r"^assistant/chat/$", views.chat, name="assistant_chat"),
    re_path(r"^assistant/confirm/$", views.confirm, name="assistant_confirm"),
    re_path(r"^assistant/context/$", views.page_context, name="assistant_context"),
]

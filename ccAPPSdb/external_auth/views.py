from django.conf import settings
from django.http import HttpResponseRedirect


def login(request, extra_context=None):
    next = request.GET.get("next", None)
    if next:
        request.session["next"] = next
    for provider in settings.SOCIALACCOUNT_PROVIDERS.keys():
        # We expect to find only 1 provider
        return HttpResponseRedirect("/accounts/%s/login/?process=login" % provider)

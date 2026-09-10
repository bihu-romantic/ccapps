import json

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseServerError
from django.views.decorators.csrf import csrf_protect

import logging

logger = logging.getLogger(__name__)


@login_required
@csrf_protect
def Upload(request):
    try:
        data = json.loads(request.body.decode("utf-8"))
        print(data)
        return HttpResponse("OK")
    except Exception as e:
        logger.error("Can't connect to the ERP: %s" % e)
        return HttpResponseServerError("Can't connect to the ERP")

from datetime import date, datetime
from itertools import chain

from django.conf import settings
from django.utils import encoding
from django.utils.translation import gettext_lazy as _


def parseLocalizedDate(data):
    if isinstance(data, date):
        return data
    elif isinstance(data, datetime):
        return data.date()
    else:
        data = encoding.smart_str(data).strip()
        for format in settings.DATE_INPUT_FORMATS:
            try:
                return datetime.strptime(data, format).date()
            except (ValueError, TypeError):
                continue
        raise Exception(_("Invalid date format"))


def parseLocalizedDateTime(data):
    if isinstance(data, date):
        return datetime.combine(data, datetime.min.time())
    elif isinstance(data, datetime):
        return data
    else:
        data = encoding.smart_str(data).strip()
        for format in chain(
            ["%Y-%m-%dT%H:%M:%S"],
            settings.DATETIME_INPUT_FORMATS,
            settings.DATE_INPUT_FORMATS,
        ):
            try:
                return datetime.strptime(data, format)
            except (ValueError, TypeError):
                continue
        raise Exception(_("Invalid date format"))

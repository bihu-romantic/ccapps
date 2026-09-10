from ccAPPSdb.boot import registerAttribute

from django.utils.translation import gettext_lazy as _


registerAttribute(
    "ccAPPSdb.input.models.Item",
    [
        ("latedemandcount", _("count of late demands"), "integer", False, False),
        ("latedemandquantity", _("quantity of late demands"), "number", False, False),
        ("latedemandvalue", _("value of late demand"), "number", False, False),
        (
            "unplanneddemandcount",
            _("count of unplanned demands"),
            "integer",
            False,
            True,
        ),
        (
            "unplanneddemandquantity",
            _("quantity of unplanned demands"),
            "number",
            False,
            True,
        ),
        (
            "unplanneddemandvalue",
            _("value of unplanned demands"),
            "number",
            False,
            True,
        ),
    ],
)

registerAttribute(
    "ccAPPSdb.input.models.Resource",
    [
        (
            "overloadcount",
            _("count of capacity overload problems"),
            "integer",
            False,
            True,
        )
    ],
)

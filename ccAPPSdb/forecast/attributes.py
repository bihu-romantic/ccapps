from ccAPPSdb.boot import registerAttribute

from django.utils.translation import gettext_lazy as _

# TODO we'ld actually want this field to be a foreign key, such that it can be use in queries
registerAttribute(
    "ccAPPSdb.input.models.OperationPlan",
    [("forecast", _("forecast"), "string", False, True)],
)
# Average Demand Interval and Coefficient of Variation Square
registerAttribute(
    "ccAPPSdb.input.models.Item",
    [
        ("demand_pattern", _("demand_pattern"), "string", False, True),
        ("adi", _("adi"), "number", False, True),
        ("cv2", _("cv2"), "number", False, True),
        ("outlier_1b", _("outliers last bucket"), "number", False, True),
        ("outlier_6b", _("outliers last 6 buckets"), "number", False, True),
        ("outlier_12b", _("outliers last 12 buckets"), "number", False, True),
    ],
)

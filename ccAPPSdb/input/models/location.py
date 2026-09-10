from django.db import models
from django.utils.translation import gettext_lazy as _

from ccAPPSdb.common.models import HierarchyModel, AuditModel

from ..models.calendar import Calendar


class Location(AuditModel, HierarchyModel):
    # Database fields
    description = models.CharField(_("description"), null=True, blank=True)
    category = models.CharField(_("category"), null=True, blank=True, db_index=True)
    subcategory = models.CharField(
        _("subcategory"), null=True, blank=True, db_index=True
    )
    available = models.ForeignKey(
        Calendar,
        verbose_name=_("available"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text=_("Calendar defining the working hours and holidays"),
    )

    def __str__(self):
        return self.name

    class Meta(AuditModel.Meta):
        db_table = "location"
        verbose_name = _("location")
        verbose_name_plural = _("locations")
        ordering = ["name"]

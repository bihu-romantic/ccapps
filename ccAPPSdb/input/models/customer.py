from django.db import models
from django.utils.translation import gettext_lazy as _

from ccAPPSdb.common.models import HierarchyModel, AuditModel


class Customer(AuditModel, HierarchyModel):
    # Database fields
    description = models.CharField(_("description"), null=True, blank=True)
    category = models.CharField(_("category"), null=True, blank=True, db_index=True)
    subcategory = models.CharField(
        _("subcategory"), null=True, blank=True, db_index=True
    )
    margin_rate = models.DecimalField(
        _("margin rate %"),
        null=True,
        blank=True,
        max_digits=5,
        decimal_places=2,
        help_text=_("Custom profit margin for this customer. Overrides global quote.profit_margin"),
    )
    discount_rate = models.DecimalField(
        _("discount rate %"),
        null=True,
        blank=True,
        max_digits=5,
        decimal_places=2,
        help_text=_("Discount rate for this customer"),
    )

    def __str__(self):
        return self.name

    class Meta(AuditModel.Meta):
        db_table = "customer"
        verbose_name = _("customer")
        verbose_name_plural = _("customers")
        ordering = ["name"]

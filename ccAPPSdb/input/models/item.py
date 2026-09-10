from django.db import models
from django.utils.translation import gettext_lazy as _

from ccAPPSdb.common.models import HierarchyModel, AuditModel, Parameter


class Item(AuditModel, HierarchyModel):
    types = (
        ("make to stock", _("make to stock")),
        ("make to order", _("make to order")),
    )

    # Database fields
    description = models.CharField(_("description"), null=True, blank=True)
    category = models.CharField(_("category"), null=True, blank=True, db_index=True)
    subcategory = models.CharField(
        _("subcategory"), null=True, blank=True, db_index=True
    )
    cost = models.DecimalField(
        _("cost"),
        null=True,
        blank=True,
        max_digits=20,
        decimal_places=8,
        help_text=_("Cost of the item"),
    )
    type = models.CharField(
        _("type"), null=True, blank=True, choices=types
    )
    weight = models.DecimalField(
        _("weight"),
        null=True,
        blank=True,
        max_digits=20,
        decimal_places=8,
        help_text=_("Weight of the item"),
    )
    volume = models.DecimalField(
        _("volume"),
        null=True,
        blank=True,
        max_digits=20,
        decimal_places=8,
        help_text=_("Volume of the item"),
    )
    periodofcover = models.IntegerField(
        _("period of cover"),
        null=True,
        blank=True,
        help_text=_("Period of cover in days"),
        editable=False,
    )
    uom = models.CharField(_("unit of measure"), null=True, blank=True)
    scrap_rate = models.DecimalField(
        _("scrap rate %"),
        null=True,
        blank=True,
        max_digits=5,
        decimal_places=2,
        help_text=_("Material loss percentage for this item. Overrides global quote.material_scrap_rate"),
    )

    extra_dependencies = [
        Parameter,
    ]

    def __str__(self):
        return self.name

    class Meta(AuditModel.Meta):
        db_table = "item"
        verbose_name = _("item")
        verbose_name_plural = _("items")
        ordering = ["name"]

from django.db import models
from django.utils.translation import gettext_lazy as _

from ccAPPSdb.common.models import AuditModel

from .customer import Customer
from .item import Item
from .location import Location
from .operation import Operation


class Demand(AuditModel):
    # Status
    demandstatus = (
        ("quote", _("quote")),
        ("open", _("open")),
        ("closed", _("closed")),
        ("canceled", _("canceled")),
    )

    # Delivery policies
    delivery_policies = (
        ("independent", _("independent")),
        ("alltogether", _("all together")),
        ("inratio", _("in ratio")),
    )

    # Database fields
    name = models.CharField(
        _("name"), primary_key=True, help_text=_("Unique identifier")
    )
    owner = models.CharField(_("owner"), null=True, blank=True, db_index=True)
    description = models.CharField(_("description"), null=True, blank=True)
    category = models.CharField(_("category"), null=True, blank=True, db_index=True)
    subcategory = models.CharField(
        _("subcategory"), null=True, blank=True, db_index=True
    )
    customer = models.ForeignKey(
        Customer, verbose_name=_("customer"), db_index=True, on_delete=models.CASCADE
    )
    # No index on the item field, because have an index on item + location + customer + due
    item = models.ForeignKey(
        Item,
        verbose_name=_("item"),
        on_delete=models.CASCADE,
        db_index=False,
    )
    location = models.ForeignKey(
        Location, verbose_name=_("location"), db_index=True, on_delete=models.CASCADE
    )
    due = models.DateTimeField(
        _("due"), help_text=_("Due date of the sales order"), db_index=True
    )
    status = models.CharField(
        _("status"),
        null=True,
        blank=True,
        choices=demandstatus,
        default="open",
        help_text=_(
            'Status of the demand. Only "open" and "quote" demands are planned'
        ),
    )
    operation = models.ForeignKey(
        Operation,
        verbose_name=_("delivery operation"),
        null=True,
        blank=True,
        related_name="used_demand",
        on_delete=models.SET_NULL,
        help_text=_("Operation used to satisfy this demand"),
    )
    quantity = models.DecimalField(_("quantity"), max_digits=20, decimal_places=8)
    priority = models.IntegerField(
        _("priority"),
        default=10,
        help_text=_(
            "Priority of the demand (lower numbers indicate more important demands)"
        ),
    )
    minshipment = models.DecimalField(
        _("minimum shipment"),
        null=True,
        blank=True,
        max_digits=20,
        decimal_places=8,
        help_text=_("Minimum shipment quantity when planning this demand"),
    )
    maxlateness = models.DurationField(
        _("maximum lateness"),
        null=True,
        blank=True,
        help_text=_("Maximum lateness allowed when planning this demand"),
    )
    policy = models.CharField(
        _("policy"),
        null=True,
        blank=True,
        choices=delivery_policies,
        default="independent",
        help_text=_("Defines how sales orders are shipped together"),
    )
    batch = models.CharField(
        _("batch"),
        null=True,
        blank=True,
        db_index=True,
        help_text=_("MTO batch name"),
    )
    delay = models.DurationField(_("delay"), null=True, blank=True, editable=False)
    plannedquantity = models.DecimalField(
        _("planned quantity"),
        max_digits=20,
        decimal_places=8,
        null=True,
        blank=True,
        editable=False,
        help_text=_("Quantity planned for delivery"),
    )
    deliverydate = models.DateTimeField(
        _("delivery date"),
        help_text=_("Delivery date of the demand"),
        null=True,
        blank=True,
        editable=False,
    )
    plan = models.JSONField(default=dict, null=True, blank=True, editable=False)
    quote_enabled = models.BooleanField(
        _("启用报价"),
        default=False,
        help_text=_("Enable quote cost calculation for this demand"),
    )
    quote = models.JSONField(
        _("quote"),
        default=dict,
        null=True,
        blank=True,
        help_text=_("Quote cost breakdown: material, resource, operation costs"),
    )

    # Convenience methods
    def __str__(self):
        return self.name

    class Meta(AuditModel.Meta):
        db_table = "demand"
        verbose_name = _("sales order")
        verbose_name_plural = _("sales orders")
        ordering = ["name"]
        indexes = [
            models.Index(
                fields=["item", "location", "customer", "due"], name="demand_sorted"
            )
        ]


class QuoteHistory(AuditModel):
    """Stores historical versions of generated quotes for audit trail."""

    demand = models.ForeignKey(
        Demand,
        verbose_name=_("demand"),
        on_delete=models.CASCADE,
        related_name="quote_history",
        help_text=_("The demand this quote was generated for"),
    )
    version = models.IntegerField(
        _("version"),
        help_text=_("Auto-incremented version number per demand"),
    )
    quote_data = models.JSONField(
        _("quote data"),
        default=dict,
        help_text=_("Full quote JSON snapshot at this version"),
    )
    created_at = models.DateTimeField(
        _("created at"),
        auto_now_add=True,
        help_text=_("When this quote version was generated"),
    )

    def __str__(self):
        return f"{self.demand.name} v{self.version}"

    class Meta(AuditModel.Meta):
        db_table = "quote_history"
        verbose_name = _("quote history")
        verbose_name_plural = _("quote histories")
        ordering = ["demand", "-version"]
        unique_together = (("demand", "version"),)


class QuoteBatchInventory(AuditModel):
    """Costed surplus created by confirmed batch-priced quotations only."""

    inventory_types = (
        ("raw_material", _("raw material")),
        ("semi_finished", _("semi-finished")),
        ("finished", _("finished good")),
    )
    source_demand = models.ForeignKey(
        Demand, on_delete=models.CASCADE, related_name="quote_batch_inventory"
    )
    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    location = models.ForeignKey(Location, on_delete=models.CASCADE)
    inventory_type = models.CharField(max_length=20, choices=inventory_types)
    available_quantity = models.DecimalField(max_digits=20, decimal_places=8)
    unit_cost = models.DecimalField(max_digits=20, decimal_places=8)
    status = models.CharField(max_length=12, default="available", db_index=True)
    consumed_by = models.ForeignKey(
        Demand, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="consumed_quote_batch_inventory"
    )

    class Meta(AuditModel.Meta):
        db_table = "quote_batch_inventory"
        indexes = [models.Index(fields=["item", "location", "status"])]

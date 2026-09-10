import json
from decimal import Decimal

from django.conf import settings
from django.contrib.admin.utils import unquote
from django.db.models.functions import Cast
from django.db.models import Case, IntegerField, Q, Value, When
from django.db.models.expressions import RawSQL
from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.template import Template
from django.utils.translation import gettext_lazy as _
from django.utils.encoding import force_str
from django.utils.text import format_lazy

from ccAPPSdb.boot import getAttributeFields
from ccAPPSdb.input.models import (
    Location,
    Customer,
    Demand,
    DeliveryOrder,
    Item,
    OperationPlan,
    QuoteBatchInventory,
)
from ccAPPSdb.common.report import (
    GridReport,
    GridFieldLastModified,
    GridFieldDateTime,
    GridFieldText,
    GridFieldHierarchicalText,
    GridFieldNumber,
    GridFieldInteger,
    GridFieldCurrency,
    GridFieldChoice,
    GridFieldDuration,
)

import logging

logger = logging.getLogger(__name__)


class LocationList(GridReport):
    title = _("locations")
    basequeryset = Location.objects.all()
    model = Location
    frozenColumns = 1
    help_url = "modeling-wizard/master-data/locations.html"
    message_when_empty = Template(
        """
        <h3>Define locations</h3>
        <br>
        A basic piece of master data is the list of locations where production is happening or inventory is kept.<br>
        <br><br>
        <div role="group" class="btn-group.btn-group-justified">
        <a href="{{request.prefix}}/data/input/location/add/" class="btn btn-primary">Create a single location<br>in a form</a>
        <a href="{{request.prefix}}/wizard/load/production/?currentstep=1" class="btn btn-primary">Wizard to upload locations<br>from a spreadsheet</a>
        </div>
        <br>
        """
    )

    rows = (
        GridFieldHierarchicalText(
            "name",
            title=_("name"),
            key=True,
            formatter="detail",
            extra='"role":"input/location"',
            model=Location,
        ),
        GridFieldText("description", title=_("description")),
        GridFieldText("category", title=_("category"), initially_hidden=True),
        GridFieldText("subcategory", title=_("subcategory"), initially_hidden=True),
        GridFieldText(
            "available",
            title=_("available"),
            field_name="available__name",
            formatter="detail",
            extra='"role":"input/calendar"',
        ),
        GridFieldText(
            "owner",
            title=_("owner"),
            field_name="owner__name",
            formatter="detail",
            extra='"role":"input/location"',
        ),
        GridFieldText("source", title=_("source"), initially_hidden=True),
        GridFieldLastModified("lastmodified"),
    )


class CustomerList(GridReport):
    title = _("customers")
    basequeryset = Customer.objects.all()
    model = Customer
    frozenColumns = 1
    help_url = "modeling-wizard/master-data/customers.html"
    message_when_empty = Template(
        """
        <h3>Define customers</h3>
        <br>
        A basic piece of master data is the customers buying items from us.<br>
        <br><br>
        <div role="group" class="btn-group.btn-group-justified">
        <a href="{{request.prefix}}/data/input/customer/add/" class="btn btn-primary">Create a single customer<br> in a form</a>
        <a href="{{request.prefix}}/wizard/load/production/?currentstep=1" class="btn btn-primary">Wizard to upload customers<br>from a spreadsheet</a>
        </div>
        <br>
        """
    )

    rows = (
        GridFieldHierarchicalText(
            "name",
            title=_("name"),
            key=True,
            formatter="detail",
            extra='"role":"input/customer"',
            model=Customer,
        ),
        GridFieldText("description", title=_("description")),
        GridFieldText("category", title=_("category"), initially_hidden=True),
        GridFieldText("subcategory", title=_("subcategory"), initially_hidden=True),
        GridFieldText(
            "owner",
            title=_("owner"),
            field_name="owner__name",
            formatter="detail",
            extra='"role":"input/customer"',
        ),
        GridFieldText("source", title=_("source"), initially_hidden=True),
        GridFieldLastModified("lastmodified"),
    )


class ItemList(GridReport):
    title = _("items")
    basequeryset = Item.objects.all()
    model = Item
    frozenColumns = 1
    editable = True
    help_url = "modeling-wizard/master-data/items.html"
    message_when_empty = Template(
        """
        <h3>Define items</h3>
        <br>
        A basic piece of master data is the list of items to plan.<br>
        End products, intermediate products and raw materials all need to be defined.<br>
        <br><br>
        <div role="group" class="btn-group.btn-group-justified">
        <a href="{{request.prefix}}/data/input/item/add/" class="btn btn-primary">Create a single item<br>in a form</a>
        <a href="{{request.prefix}}/wizard/load/production/?currentstep=1" class="btn btn-primary">Wizard to upload items<br>from a spreadsheet</a>
        </div>
        <br>
        """
    )

    rows = (
        GridFieldHierarchicalText(
            "name",
            title=_("name"),
            key=True,
            formatter="detail",
            extra='"role":"input/item"',
            model=Item,
        ),
        GridFieldText("description", title=_("description")),
        GridFieldText("category", title=_("category"), initially_hidden=True),
        GridFieldText("subcategory", title=_("subcategory"), initially_hidden=True),
        GridFieldText(
            "owner",
            title=_("owner"),
            field_name="owner__name",
            formatter="detail",
            extra='"role":"input/item"',
        ),
        GridFieldCurrency("cost", title=_("cost")),
        GridFieldNumber("weight", title=_("weight"), initially_hidden=True),
        GridFieldNumber("volume", title=_("volume"), initially_hidden=True),
        GridFieldText("uom", title=_("unit of measure"), initially_hidden=True),
        GridFieldInteger(
            "periodofcover", title=_("period of cover"), initially_hidden=True
        ),
        GridFieldChoice(
            "type", title=_("type"), choices=Item.types, initially_hidden=True
        ),
        GridFieldText("source", title=_("source"), initially_hidden=True),
        GridFieldLastModified("lastmodified"),
    )


class DemandList(GridReport):
    template = "input/demand.html"
    title = _("sales orders")
    model = Demand
    frozenColumns = 1
    editable = True
    # Keep quote-enabled sales orders at the top of the default list view.
    default_sort = (0, "desc", 1, "asc")
    help_url = "modeling-wizard/master-data/sales-orders.html"
    message_when_empty = Template(
        """
        <h3>Define sales orders</h3>
        <br>
        The sales orders table contains all the orders placed by your customers.<br><br>
        Orders in the status "open" are still be delivered and will be planned.<br><br>
        Orders in the status "closed" represent the sales history, and are used
        to compute a statistical sales forecast for the future.<br><br>
        <br><br>
        <div role="group" class="btn-group.btn-group-justified">
        <a href="{{request.prefix}}/data/input/demand/add/" class="btn btn-primary">Create a single sales order<br>in a form</a>
        <a href="{{request.prefix}}/wizard/load/production/?currentstep=2" class="btn btn-primary">Wizard to upload sale orders<br>from a spreadsheet</a>
        </div>
        <br>
        """
    )

    @classmethod
    def initialize(reportclass, request):
        if reportclass._attributes_added != 2:
            reportclass._attributes_added = 2
            reportclass.attr_sql = ""
            # Adding custom item attributes
            for f in getAttributeFields(
                Item, related_name_prefix="item", initially_hidden=True, editable=False
            ):
                reportclass.rows += (f,)
                reportclass.attr_sql += "item.%s, " % f.name.split("__")[-1]
            # Adding custom location attributes
            for f in getAttributeFields(
                Location,
                related_name_prefix="location",
                initially_hidden=True,
                editable=False,
            ):
                reportclass.rows += (f,)
                reportclass.attr_sql += "location.%s, " % f.name.split("__")[-1]
            # Adding custom customer attributes
            for f in getAttributeFields(
                Customer,
                related_name_prefix="customer",
                initially_hidden=True,
                editable=False,
            ):
                reportclass.rows += (f,)
                reportclass.attr_sql += "customer.%s, " % f.name.split("__")[-1]
            # Adding custom demand attributes
            for f in getAttributeFields(Demand, initially_hidden=True):
                reportclass.rows += (f,)
                reportclass.attr_sql += "demand.%s, " % f.name.split("__")[-1]

    @classmethod
    def basequeryset(reportclass, request, *args, **kwargs):
        q = Demand.objects.all()

        if "item" in request.GET:
            Item.rebuildHierarchy(request.database)
            item = Item.objects.using(request.database).get(
                name__exact=unquote(request.GET["item"])
            )
            q = q.filter(item__lft__gte=item.lft, item__lft__lt=item.rght)
        if "location" in request.GET:
            Location.rebuildHierarchy(request.database)
            location = Location.objects.using(request.database).get(
                name__exact=unquote(request.GET["location"])
            )
            q = q.filter(
                location__lft__gte=location.lft, location__lft__lt=location.rght
            )
        if "customer" in request.GET:
            Customer.rebuildHierarchy(request.database)
            customer = Customer.objects.using(request.database).get(
                name__exact=unquote(request.GET["customer"])
            )
            q = q.filter(customer_lft__gte=customer.lft, customer_lft__lt=customer.rght)
        if "status_in" in request.GET:
            status = unquote(request.GET["status_in"])
            q = q.filter(status__in=status.split(","))

        return q.annotate(
            quote_sort=Case(
                When(quote_enabled=True, then=Value(1)),
                default=Value(0),
                output_field=IntegerField(),
            ),
            plannedshort=RawSQL("quantity - plannedquantity", []),
            quote_total=RawSQL("(quote->>'total_cost')::numeric", []),
            quote_total_with_tax=RawSQL("(quote->>'total_with_tax')::numeric", []),
            quote_material=RawSQL("(quote->>'material_cost')::numeric", []),
            quote_scrap=RawSQL("(quote->>'scrap_cost')::numeric", []),
            quote_resource=RawSQL("(quote->>'resource_cost')::numeric", []),
            quote_operation=RawSQL("(quote->>'operation_cost')::numeric", []),
            quote_overhead=RawSQL("(quote->>'overhead_cost')::numeric", []),
            quote_transport=RawSQL("(quote->>'transport_cost')::numeric", []),
            quote_profit=RawSQL("(quote->>'profit_amount')::numeric", []),
            quote_discount=RawSQL("(quote->>'discount_amount')::numeric", []),
            quote_status=RawSQL("quote->>'quote_status'", []),
            quote_valid_until=RawSQL("quote->>'quote_valid_until'", []),
        )

    @classmethod
    def _apply_sort(reportclass, request, query):
        """Keep quote orders first even when a saved grid sort is active."""
        sorted_query = super()._apply_sort(request, query)
        return sorted_query.order_by("-quote_sort", *sorted_query.query.order_by)

    rows = (
        GridFieldInteger("quote_sort", title=_("quote priority"), initially_hidden=True),
        GridFieldText(
            "name",
            title=_("name"),
            key=True,
            formatter="detail",
            extra='"role":"input/demand"',
        ),
        GridFieldHierarchicalText(
            "item",
            title=_("item"),
            field_name="item__name",
            formatter="detail",
            extra='"role":"input/item"',
            model=Item,
        ),
        GridFieldText("batch", title=_("batch"), initially_hidden=True),
        GridFieldHierarchicalText(
            "location",
            title=_("location"),
            field_name="location__name",
            formatter="detail",
            extra='"role":"input/location"',
            model=Location,
        ),
        GridFieldHierarchicalText(
            "customer",
            title=_("customer"),
            field_name="customer__name",
            formatter="detail",
            extra='"role":"input/customer"',
            model=Customer,
        ),
        GridFieldChoice("status", title=_("status"), choices=Demand.demandstatus),
        GridFieldChoice(
            "quote_enabled",
            title=_("启用报价"),
            choices=((True, _("是")), (False, _("否"))),
        ),
        GridFieldChoice(
            "quote_status",
            title=_("报价状态"),
            choices=(
                ("draft", _("草稿")),
                ("confirmed", _("已确认")),
                ("sent", _("已发送")),
                ("expired", _("已失效")),
                ("rejected", _("已拒绝")),
            ),
        ),
        GridFieldText("quote_valid_until", title=_("报价有效至"), editable=False),
        GridFieldNumber(
            "quote_total",
            title=_("未税报价金额"),
            editable=False,
            extra='"formatoptions":{"defaultValue":""}',
        ),
        GridFieldNumber(
            "quote_total_with_tax",
            title=_("含税报价金额"),
            editable=False,
            extra='"formatoptions":{"defaultValue":""}',
        ),
        GridFieldNumber(
            "quote_material",
            title=_("材料成本"),
            editable=False,
            initially_hidden=True,
            extra='"formatoptions":{"defaultValue":""}',
        ),
        GridFieldNumber(
            "quote_scrap",
            title=_("损耗成本"),
            editable=False,
            initially_hidden=True,
            extra='"formatoptions":{"defaultValue":""}',
        ),
        GridFieldNumber(
            "quote_resource",
            title=_("资源成本"),
            editable=False,
            initially_hidden=True,
            extra='"formatoptions":{"defaultValue":""}',
        ),
        GridFieldNumber(
            "quote_operation",
            title=_("工艺成本"),
            editable=False,
            initially_hidden=True,
            extra='"formatoptions":{"defaultValue":""}',
        ),
        GridFieldNumber(
            "quote_overhead",
            title=_("间接费用"),
            editable=False,
            initially_hidden=True,
            extra='"formatoptions":{"defaultValue":""}',
        ),
        GridFieldNumber(
            "quote_transport",
            title=_("运输成本"),
            editable=False,
            initially_hidden=True,
            extra='"formatoptions":{"defaultValue":""}',
        ),
        GridFieldNumber(
            "quote_profit",
            title=_("利润"),
            editable=False,
            initially_hidden=True,
            extra='"formatoptions":{"defaultValue":""}',
        ),
        GridFieldNumber(
            "quote_discount",
            title=_("折扣金额"),
            editable=False,
            initially_hidden=True,
            extra='"formatoptions":{"defaultValue":""}',
        ),
        GridFieldNumber("quantity", title=_("quantity")),
        GridFieldDateTime("due", title=_("due")),
        GridFieldDuration(
            "delay", title=_("delay"), editable=False, extra='"formatter":delayfmt'
        ),
        GridFieldNumber(
            "plannedquantity",
            title=_("planned quantity"),
            editable=False,
            extra='"formatoptions":{"defaultValue":""}, "formatter":plannedquantitycellattr',
        ),
        GridFieldNumber(
            "plannedshort",
            title=_("quantity planned short"),
            editable=False,
            extra='"formatoptions":{"defaultValue":""}, "formatter":plannedquantitycellattr',
        ),
        GridFieldDateTime("deliverydate", title=_("delivery date"), editable=False),
        GridFieldText("description", title=_("description"), initially_hidden=True),
        GridFieldText("category", title=_("category"), initially_hidden=True),
        GridFieldText("subcategory", title=_("subcategory"), initially_hidden=True),
        GridFieldText(
            "operation",
            title=_("delivery operation"),
            field_name="operation__name",
            formatter="detail",
            extra='"role":"input/operation"',
            initially_hidden=True,
        ),
        GridFieldInteger("priority", title=_("priority")),
        GridFieldText(
            "owner",
            title=_("owner"),
            initially_hidden=True,
        ),
        GridFieldChoice(
            "policy",
            title=_("policy"),
            choices=Demand.delivery_policies,
            initially_hidden=True,
        ),
        GridFieldDuration(
            "maxlateness", title=_("maximum lateness"), initially_hidden=True
        ),
        GridFieldNumber(
            "minshipment", title=_("minimum shipment"), initially_hidden=True
        ),
        GridFieldText("source", title=_("source"), initially_hidden=True),
        GridFieldLastModified("lastmodified"),
        # Optional fields referencing the item
        GridFieldText(
            "item__type",
            title=format_lazy("{} - {}", _("item"), _("type")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldText(
            "item__description",
            title=format_lazy("{} - {}", _("item"), _("description")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldText(
            "item__category",
            title=format_lazy("{} - {}", _("item"), _("category")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldText(
            "item__subcategory",
            title=format_lazy("{} - {}", _("item"), _("subcategory")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldText(
            "item__owner",
            title=format_lazy("{} - {}", _("item"), _("owner")),
            field_name="item__owner__name",
            initially_hidden=True,
            editable=False,
            formatter="detail",
            extra='"role":"input/item"',
        ),
        GridFieldCurrency(
            "item__cost",
            title=format_lazy("{} - {}", _("item"), _("cost")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldNumber(
            "item__volume",
            title=format_lazy("{} - {}", _("item"), _("volume")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldNumber(
            "item__weight",
            title=format_lazy("{} - {}", _("item"), _("weight")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldText(
            "item__uom",
            title=format_lazy("{} - {}", _("item"), _("unit of measure")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldInteger(
            "item__periodofcover",
            title=format_lazy("{} - {}", _("item"), _("period of cover")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldText(
            "item__source",
            title=format_lazy("{} - {}", _("item"), _("source")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldLastModified(
            "item__lastmodified",
            title=format_lazy("{} - {}", _("item"), _("last modified")),
            initially_hidden=True,
            editable=False,
        ),
        # Optional fields referencing the location
        GridFieldText(
            "location__description",
            title=format_lazy("{} - {}", _("location"), _("description")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldText(
            "location__category",
            title=format_lazy("{} - {}", _("location"), _("category")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldText(
            "location__subcategory",
            title=format_lazy("{} - {}", _("location"), _("subcategory")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldText(
            "location__available",
            title=format_lazy("{} - {}", _("location"), _("available")),
            initially_hidden=True,
            field_name="location__available__name",
            formatter="detail",
            extra='"role":"input/calendar"',
            editable=False,
        ),
        GridFieldText(
            "location__owner",
            title=format_lazy("{} - {}", _("location"), _("owner")),
            initially_hidden=True,
            field_name="location__owner__name",
            formatter="detail",
            extra='"role":"input/location"',
            editable=False,
        ),
        GridFieldText(
            "location__source",
            title=format_lazy("{} - {}", _("location"), _("source")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldLastModified(
            "location__lastmodified",
            title=format_lazy("{} - {}", _("location"), _("last modified")),
            initially_hidden=True,
            editable=False,
        ),
        # Optional fields referencing the customer
        GridFieldText(
            "customer__description",
            title=format_lazy("{} - {}", _("customer"), _("description")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldText(
            "customer__category",
            title=format_lazy("{} - {}", _("customer"), _("category")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldText(
            "customer__subcategory",
            title=format_lazy("{} - {}", _("customer"), _("subcategory")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldText(
            "customer__owner",
            title=format_lazy("{} - {}", _("customer"), _("owner")),
            initially_hidden=True,
            field_name="customer__owner__name",
            formatter="detail",
            extra='"role":"input/customer"',
            editable=False,
        ),
        GridFieldText(
            "customer__source",
            title=format_lazy("{} - {}", _("customer"), _("source")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldLastModified(
            "customer__lastmodified",
            title=format_lazy("{} - {}", _("customer"), _("last modified")),
            initially_hidden=True,
            editable=False,
        ),
    )

    @classmethod
    def parseJSONupload(reportclass, request):
        """Store inline quote-status edits in the demand quote JSON snapshot."""
        try:
            records = json.loads(request.body.decode(request.encoding or "utf-8"))
        except (TypeError, ValueError):
            return HttpResponseBadRequest("Invalid update payload")

        allowed_statuses = {"draft", "confirmed", "sent", "expired", "rejected"}
        quote_status_updates = [
            record for record in records
            if "id" in record and set(record) == {"id", "quote_status"}
        ]
        if not quote_status_updates:
            return super().parseJSONupload(request)
        if len(quote_status_updates) != len(records):
            return HttpResponseBadRequest("Quote status must be updated separately")
        if not request.user.has_perm("input.change_demand"):
            return HttpResponseForbidden("Permission denied")

        for record in quote_status_updates:
            status = record["quote_status"]
            if status not in allowed_statuses:
                return HttpResponseBadRequest("Invalid quote status")
            with transaction.atomic(using=request.database):
                demand = Demand.objects.using(request.database).select_for_update().get(pk=record["id"])
                quote = dict(demand.quote or {})
                previous_status = quote.get("quote_status", "draft")
                quote["quote_status"] = status
                demand.quote = quote
                demand.save(using=request.database, update_fields=["quote"])
                if status == "confirmed" and previous_status != "confirmed":
                    # Only a confirmed quote creates a costed surplus pool.
                    for record_data in quote.get("cost_snapshot", {}).get("materials", []):
                        excess = Decimal(str(record_data.get("charged_quantity", 0))) - Decimal(
                            str(record_data.get("allocated_quantity", 0))
                        )
                        if excess <= 0 or not record_data.get("item"):
                            continue
                        QuoteBatchInventory.objects.using(request.database).create(
                            source_demand=demand,
                            item_id=record_data["item"], location_id=demand.location_id,
                            inventory_type=record_data.get("inventory_type", "raw_material"),
                            available_quantity=excess,
                            unit_cost=Decimal(str(record_data.get("unit_cost", 0))),
                        )
        return HttpResponse("OK")

    if settings.ERP_CONNECTOR:
        actions = [
            {
                "name": "erp_incr_export",
                "label": format_lazy(_("export to {erp}"), erp=settings.ERP_CONNECTOR),
                "function": "ERPconnection.SODepExport(jQuery('#grid'),'SO')",
            }
        ]
    else:
        actions = [
            {
                "name": "quote",
                "label": format_lazy(_("change status to {status}"), status=_("quote")),
                "function": "grid.setStatus('quote')",
            },
            {
                "name": "open",
                "label": format_lazy(_("change status to {status}"), status=_("open")),
                "function": "grid.setStatus('open')",
            },
            {
                "name": "closed",
                "label": format_lazy(
                    _("change status to {status}"), status=_("closed")
                ),
                "function": "grid.setStatus('closed')",
            },
            {
                "name": "canceled",
                "label": format_lazy(
                    _("change status to {status}"), status=_("canceled")
                ),
                "function": "grid.setStatus('canceled')",
            },
        ]


class DeliveryOrderList(GridReport):
    template = "input/deliveryorder.html"
    title = _("delivery orders")
    model = DeliveryOrder
    frozenColumns = 0
    editable = True
    multiselect = True
    help_url = "model-reference/delivery-orders.html"
    calendarmode = "start"

    rows = (
        GridFieldText(
            "reference",
            title=_("reference"),
            key=True,
            formatter="detail",
            extra='role:"input/deliveryorder"',
            editable=not settings.ERP_CONNECTOR,
        ),
        GridFieldText(
            "batch", title=_("batch"), editable="true", initially_hidden=True
        ),
        GridFieldText(
            "demand",
            title=_("demand"),
            field_name="demand__name",
            formatter="detail",
            extra='"role":"input/demand"',
        ),
        GridFieldHierarchicalText(
            "item",
            title=_("item"),
            field_name="item__name",
            formatter="detail",
            extra='"role":"input/item"',
            model=Item,
        ),
        GridFieldHierarchicalText(
            "cust",
            title=_("customer"),
            formatter="detail",
            extra='"role":"input/customer"',
            model=Customer,
        ),
        GridFieldHierarchicalText(
            "location",
            title=_("location"),
            field_name="location__name",
            formatter="detail",
            extra='"role":"input/location"',
            model=Location,
        ),
        GridFieldNumber("quantity", title=_("quantity")),
        GridFieldNumber("demandquantity", title=_("demand quantity"), editable=False),
        GridFieldDateTime("startdate", title=_("start date")),
        GridFieldDateTime(
            "enddate",
            title=_("end date"),
            extra=GridFieldDateTime.extra + ',"cellattr":enddatecellattr',
        ),
        GridFieldDateTime("duedate", title="due date", editable=False),
        GridFieldChoice(
            "status",
            title=_("status"),
            choices=OperationPlan.orderstatus,
            editable=not settings.ERP_CONNECTOR,
        ),
        GridFieldDuration(
            "delay",
            title=_("delay"),
            editable=False,
            initially_hidden=True,
            extra='"formatoptions":{"defaultValue":""}, "summaryType":"max"',
        ),
        GridFieldText("source", title=_("source"), initially_hidden=True),
        GridFieldLastModified("lastmodified", initially_hidden=True),
        # Optional fields referencing the item
        GridFieldText(
            "item__type",
            title=format_lazy("{} - {}", _("item"), _("type")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldText(
            "item__description",
            title=format_lazy("{} - {}", _("item"), _("description")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldText(
            "item__category",
            title=format_lazy("{} - {}", _("item"), _("category")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldText(
            "item__subcategory",
            title=format_lazy("{} - {}", _("item"), _("subcategory")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldCurrency(
            "item__cost",
            title=format_lazy("{} - {}", _("item"), _("cost")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldNumber(
            "item__volume",
            title=format_lazy("{} - {}", _("item"), _("volume")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldNumber(
            "item__weight",
            title=format_lazy("{} - {}", _("item"), _("weight")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldText(
            "item__uom",
            title=format_lazy("{} - {}", _("item"), _("unit of measure")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldInteger(
            "item__periodofcover",
            title=format_lazy("{} - {}", _("item"), _("period of cover")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldText(
            "item__owner",
            title=format_lazy("{} - {}", _("item"), _("owner")),
            field_name="item__owner__name",
            initially_hidden=True,
            editable=False,
        ),
        GridFieldText(
            "item__source",
            title=format_lazy("{} - {}", _("item"), _("source")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldLastModified(
            "item__lastmodified",
            title=format_lazy("{} - {}", _("item"), _("last modified")),
            initially_hidden=True,
            editable=False,
        ),
        # Optional fields referencing the location
        GridFieldText(
            "location__description",
            title=format_lazy("{} - {}", _("location"), _("description")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldText(
            "location__category",
            title=format_lazy("{} - {}", _("location"), _("category")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldText(
            "location__subcategory",
            title=format_lazy("{} - {}", _("location"), _("subcategory")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldText(
            "location__available",
            title=format_lazy("{} - {}", _("location"), _("available")),
            initially_hidden=True,
            field_name="location__available__name",
            formatter="detail",
            extra='"role":"input/calendar"',
            editable=False,
        ),
        GridFieldText(
            "location__owner",
            title=format_lazy("{} - {}", _("location"), _("owner")),
            initially_hidden=True,
            field_name="location__owner__name",
            formatter="detail",
            extra='"role":"input/location"',
            editable=False,
        ),
        GridFieldText(
            "location__source",
            title=format_lazy("{} - {}", _("location"), _("source")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldLastModified(
            "location__lastmodified",
            title=format_lazy("{} - {}", _("location"), _("last modified")),
            initially_hidden=True,
            editable=False,
        ),
        # Optional fields referencing the customer
        GridFieldText(
            "demand__customer__description",
            title=format_lazy("{} - {}", _("customer"), _("description")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldText(
            "demand__customer__category",
            title=format_lazy("{} - {}", _("customer"), _("category")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldText(
            "demand__customer__subcategory",
            title=format_lazy("{} - {}", _("customer"), _("subcategory")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldText(
            "demand__customer__owner",
            title=format_lazy("{} - {}", _("customer"), _("owner")),
            initially_hidden=True,
            field_name="supplier__owner__name",
            formatter="detail",
            extra='"role":"input/supplier"',
            editable=False,
        ),
        GridFieldText(
            "demand__customer__source",
            title=format_lazy("{} - {}", _("customer"), _("source")),
            initially_hidden=True,
            editable=False,
        ),
        GridFieldLastModified(
            "demand__customer__lastmodified",
            title=format_lazy("{} - {}", _("customer"), _("last modified")),
            initially_hidden=True,
            editable=False,
        ),
    )

    @classmethod
    def basequeryset(reportclass, request, *args, **kwargs):
        q = DeliveryOrder.objects.all()
        if "calendarstart" in request.GET:
            q = q.filter(
                Q(enddate__gte=request.GET["calendarstart"])
                | (
                    Q(enddate__isnull=True)
                    & Q(startdate__gte=request.GET["calendarstart"])
                )
            )
        if "calendarend" in request.GET:
            q = q.filter(
                Q(startdate__lte=request.GET["calendarend"])
                | (
                    Q(startdate__isnull=True)
                    & Q(enddate__lte=request.GET["calendarend"])
                )
            )

        # special keyword superop used for search field of operationplan
        if "parentreference" in request.GET:
            parentreference = request.GET["parentreference"]
            q = q.filter(reference=parentreference)

        if "ccAPPSdb.forecast" in settings.INSTALLED_APPS:
            q = q.annotate(
                cust=RawSQL(
                    """
                    coalesce(
                      (select customer_id from demand where demand.name = demand_id),
                      (select customer_id from forecast where forecast.name = forecast)
                      )
                    """,
                    (),
                ),
                cust_lft=Cast(
                    RawSQL(
                        """
                        select lft from customer where name =
                            coalesce(
                            (select customer_id from demand where demand.name = demand_id),
                            (select customer_id from forecast where forecast.name = forecast)
                            )
                        """,
                        [],
                    ),
                    IntegerField(),
                ),
                demandquantity=RawSQL(
                    """
                    coalesce(
                      (select quantity from demand where demand.name = demand_id),
                      (
                          select forecastplan.forecastnet
                          from forecastplan
                          inner join forecast
                            on forecastplan.item_id = forecast.item_id and forecastplan.location_id = forecast.location_id
                            and forecastplan.customer_id = forecast.customer_id
                          where forecast.name = forecast and operationplan.due >= startdate and operationplan.due < enddate
                      ))
                    """,
                    (),
                ),
                duedate=RawSQL(
                    """
                    coalesce(
                    (select due from demand where demand.name = demand_id),
                    (
                      select case when parameter.value = 'start' then startdate
                      when parameter.value = 'end' then enddate - interval '1 second'
                      else date_trunc('day', startdate + (enddate-startdate)/2) end
                      from forecastplan
                      inner join common_parameter parameter on name = 'forecast.DueWithinBucket'
                      inner join forecast
                        on forecastplan.item_id = forecast.item_id and forecastplan.location_id = forecast.location_id
                        and forecastplan.customer_id = forecast.customer_id
                      where forecast.name = forecast and operationplan.due >= startdate and operationplan.due < enddate
                    ))
                    """,
                    (),
                ),
            )

            # forecast report drill down
            if "item" in request.GET:
                item = Item.objects.using(request.database).get(
                    name__exact=unquote(request.GET["item"])
                )
                q = q.filter(item__lft__gte=item.lft, item__lft__lt=item.rght)
            if "location" in request.GET:
                location = Location.objects.using(request.database).get(
                    name__exact=unquote(request.GET["location"])
                )
                q = q.filter(
                    location__lft__gte=location.lft, location__lft__lt=location.rght
                )
            if "customer" in request.GET:
                customer = Customer.objects.using(request.database).get(
                    name__exact=unquote(request.GET["customer"])
                )
                q = q.filter(cust_lft__gte=customer.lft, cust_lft__lt=customer.rght)
            if "orders" in request.GET:
                orders = request.GET["orders"]
                q = q.filter(demand__isnull=(orders.lower() in ["false", "0"]))

        else:
            q = q.annotate(
                cust=RawSQL(
                    "(select demand.customer_id from demand where demand.name = operationplan.demand_id)",
                    (),
                ),
                demandquantity=RawSQL(
                    "(select demand.quantity from demand where demand.name = operationplan.demand_id)",
                    (),
                ),
                duedate=RawSQL(
                    "(select due from demand where demand.name = operationplan.demand_id)",
                    (),
                ),
            )

        if args and args[0]:
            path = request.path.split("/")[4]
            if path == "consumed":
                return q.filter(
                    item__name=args[0],
                    location__name=args[1],
                    enddate__gte=args[2],
                    enddate__lt=args[3],
                )
            else:
                try:
                    itm = Item.objects.all().using(request.database).get(name=args[0])
                    lft = itm.lft
                    rght = itm.rght
                except Item.DoesNotExist:
                    lft = 1
                    rght = 1
                q = q.filter(item__lft__gte=lft, item__rght__lte=rght)

        return q

    @classmethod
    def extra_context(reportclass, request, *args, **kwargs):
        if args and args[0]:
            request.session["lasttab"] = "deliveryorders"
            path = request.path.split("/")[4]
            if path == "consumed":
                return {
                    "active_tab": "deliveryorders",
                    "model": Item,
                    "title": force_str(Item._meta.verbose_name) + " " + args[0],
                    "post_title": force_str(
                        _("delivered from %(loc)s between %(date1)s and %(date2)s")
                        % {"loc": args[1], "date1": args[2], "date2": args[3]}
                    ),
                }
            else:
                return {
                    "active_tab": "deliveryorders",
                    "title": force_str(Item._meta.verbose_name) + " " + args[0],
                    "post_title": _("delivery orders"),
                }
        else:
            return {"active_tab": "deliveryorders"}

    @classmethod
    def initialize(reportclass, request):
        if reportclass._attributes_added != 2:
            reportclass._attributes_added = 2
            for f in getAttributeFields(DeliveryOrder):
                reportclass.rows += (f,)
            for f in getAttributeFields(
                Item, related_name_prefix="item", editable=False
            ):
                f.editable = False
                f.initially_hidden = True
                reportclass.rows += (f,)
            for f in getAttributeFields(
                Location, related_name_prefix="location", editable=False
            ):
                f.editable = False
                f.initially_hidden = True
                reportclass.rows += (f,)
            for f in getAttributeFields(
                Customer, related_name_prefix="demand__customer", editable=False
            ):
                f.editable = False
                f.initially_hidden = True
                reportclass.rows += (f,)

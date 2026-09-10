from django.utils.translation import gettext_lazy as _

from ccAPPSdb.menu import menu
from ccAPPSdb.input.utils import hasRoutingOperations

import ccAPPSdb.input.views
from ccAPPSdb.input.models import (
    Buffer,
    Calendar,
    CalendarBucket,
    Customer,
    DeliveryOrder,
    Demand,
    DistributionOrder,
    Item,
    ItemDistribution,
    ItemSupplier,
    Location,
    ManufacturingOrder,
    WorkOrder,
    Operation,
    OperationDependency,
    OperationMaterial,
    OperationPlanMaterial,
    OperationPlanResource,
    OperationResource,
    PurchaseOrder,
    Resource,
    ResourceSkill,
    SetupMatrix,
    SetupRule,
    Skill,
    SubOperation,
    Supplier,
)

# ── Sales ──
menu.addItem("supply_chain", "sales_section", section=True, label=_("sales"), index=10)

menu.addItem(
    "supply_chain",
    "demand",
    url="/data/input/demand/",
    report=ccAPPSdb.input.views.DemandList,
    index=100,
    model=Demand,
    dependencies=[Item, Location, Customer],
)
menu.addItem(
    "supply_chain",
    "delivery order",
    url="/data/input/deliveryorder/",
    report=ccAPPSdb.input.views.DeliveryOrderList,
    index=300,
    model=DeliveryOrder,
    dependencies=[Demand],
)
menu.addItem(
    "supply_chain",
    "item",
    url="/data/input/item/",
    report=ccAPPSdb.input.views.ItemList,
    index=1100,
    model=Item,
)
menu.addItem(
    "supply_chain",
    "locations",
    url="/data/input/location/",
    report=ccAPPSdb.input.views.LocationList,
    index=1150,
    model=Location,
)
menu.addItem(
    "supply_chain",
    "customer",
    url="/data/input/customer/",
    report=ccAPPSdb.input.views.CustomerList,
    index=1200,
    model=Customer,
)

# ── Inventory ──
menu.addItem("supply_chain", "inventory_section", section=True, label=_("inventory"), index=2000)

menu.addItem(
    "supply_chain",
    "inventory detail",
    url="/data/input/operationplanmaterial/",
    report=ccAPPSdb.input.views.InventoryDetail,
    index=2100,
    model=OperationPlanMaterial,
    dependencies=[Item, Location],
)
menu.addItem(
    "supply_chain",
    "distribution orders",
    url="/data/input/distributionorder/",
    report=ccAPPSdb.input.views.DistributionOrderList,
    index=2150,
    model=DistributionOrder,
    dependencies=[ItemDistribution],
)
menu.addItem(
    "supply_chain",
    "buffer admin",
    url="/data/input/buffer/",
    report=ccAPPSdb.input.views.BufferList,
    index=2200,
    model=Buffer,
    dependencies=[Item, Location],
)
menu.addItem(
    "supply_chain",
    "item distributions",
    url="/data/input/itemdistribution/",
    report=ccAPPSdb.input.views.ItemDistributionList,
    index=2300,
    model=ItemDistribution,
    dependencies=[Item, Location],
)

# ── Capacity ──
menu.addItem("supply_chain", "capacity_section", section=True, label=_("资源"), index=3000)

menu.addItem(
    "supply_chain",
    "resource detail report",
    url="/data/input/operationplanresource/",
    report=ccAPPSdb.input.views.ResourceDetail,
    index=3100,
    model=OperationPlanResource,
    dependencies=[Resource],
)
menu.addItem(
    "supply_chain",
    "resources",
    url="/data/input/resource/",
    report=ccAPPSdb.input.views.ResourceList,
    index=3200,
    model=Resource,
)
menu.addItem(
    "supply_chain",
    "skills",
    url="/data/input/skill/",
    report=ccAPPSdb.input.views.SkillList,
    index=3300,
    model=Skill,
    dependencies=[Resource],
)
menu.addItem(
    "supply_chain",
    "resource skills",
    url="/data/input/resourceskill/",
    report=ccAPPSdb.input.views.ResourceSkillList,
    index=3400,
    model=ResourceSkill,
    dependencies=[Resource, Skill],
)
menu.addItem(
    "supply_chain",
    "setup matrices",
    url="/data/input/setupmatrix/",
    report=ccAPPSdb.input.views.SetupMatrixList,
    index=3500,
    model=SetupMatrix,
    dependencies=[Resource],
)
menu.addItem(
    "supply_chain",
    "setup rules",
    url="/data/input/setuprule/",
    report=ccAPPSdb.input.views.SetupRuleList,
    index=3600,
    model=SetupRule,
    dependencies=[SetupMatrix],
)

# ── Purchasing ──
menu.addItem("supply_chain", "purchasing_section", section=True, label=_("purchasing"), index=4000)

menu.addItem(
    "supply_chain",
    "purchase orders",
    url="/data/input/purchaseorder/",
    report=ccAPPSdb.input.views.PurchaseOrderList,
    index=4100,
    model=PurchaseOrder,
    dependencies=[Item, Location, Supplier],
)
menu.addItem(
    "supply_chain",
    "suppliers",
    url="/data/input/supplier/",
    report=ccAPPSdb.input.views.SupplierList,
    index=4200,
    model=Supplier,
)
menu.addItem(
    "supply_chain",
    "item suppliers",
    url="/data/input/itemsupplier/",
    report=ccAPPSdb.input.views.ItemSupplierList,
    index=4300,
    model=ItemSupplier,
    dependencies=[Item, Location, Supplier],
)

# ── Manufacturing ──
menu.addItem("supply_chain", "manufacturing_section", section=True, label=_("manufacturing"), index=5000)

menu.addItem(
    "supply_chain",
    "manufacturing orders",
    url="/data/input/manufacturingorder/",
    report=ccAPPSdb.input.views.ManufacturingOrderList,
    index=5100,
    model=ManufacturingOrder,
    dependencies=[Operation],
)
menu.addItem(
    "supply_chain",
    "work orders",
    url="/data/input/workorder/",
    report=ccAPPSdb.input.views.WorkOrderList,
    index=5150,
    model=WorkOrder,
    dependencies=[
        hasRoutingOperations,
    ],
)
menu.addItem(
    "supply_chain",
    "calendars",
    url="/data/input/calendar/",
    report=ccAPPSdb.input.views.CalendarList,
    index=5200,
    model=Calendar,
)
menu.addItem(
    "supply_chain",
    "calendarbucket",
    url="/data/input/calendarbucket/",
    report=ccAPPSdb.input.views.CalendarBucketList,
    index=5300,
    model=CalendarBucket,
    dependencies=[Calendar],
)
menu.addItem(
    "supply_chain",
    "operations",
    url="/data/input/operation/",
    report=ccAPPSdb.input.views.OperationList,
    index=5400,
    model=Operation,
    dependencies=[Item, Location],
)
menu.addItem(
    "supply_chain",
    "operationmaterials",
    url="/data/input/operationmaterial/",
    report=ccAPPSdb.input.views.OperationMaterialList,
    index=5500,
    model=OperationMaterial,
    dependencies=[Operation],
)
menu.addItem(
    "supply_chain",
    "operationresources",
    url="/data/input/operationresource/",
    report=ccAPPSdb.input.views.OperationResourceList,
    index=5600,
    model=OperationResource,
    dependencies=[Operation, Resource],
)
menu.addItem(
    "supply_chain",
    "operationdependencies",
    url="/data/input/operationdependency/",
    report=ccAPPSdb.input.views.OperationDependencyList,
    index=5700,
    model=OperationDependency,
    dependencies=[Operation],
)
menu.addItem(
    "supply_chain",
    "suboperations",
    url="/data/input/suboperation/",
    report=ccAPPSdb.input.views.SubOperationList,
    index=5800,
    model=SubOperation,
    dependencies=[Operation],
)

from django.utils.translation import gettext_lazy as _
from ccAPPSdb.menu import menu
from .models import InventoryResult

menu.addItem(
    "supply_chain",
    "inventory policies",
    index=2070,
    label=_("库存策略 SS / ROQ"),
    url="/inventory/policies/",
    model=InventoryResult,
)

from django.contrib import admin
from ccAPPSdb.admin import data_site
from ccAPPSdb.common.adminforms import MultiDBModelAdmin
from .models import Segment, BusinessRule, InventoryPolicy


@admin.register(Segment, site=data_site)
class SegmentAdmin(MultiDBModelAdmin):
    list_display = ("name", "query", "sku_count")
    exclude = ("source", "lastmodified")


@admin.register(BusinessRule, site=data_site)
class RuleAdmin(MultiDBModelAdmin):
    list_display = ("segment", "rule", "value", "priority")
    exclude = ("source", "lastmodified")


@admin.register(InventoryPolicy, site=data_site)
class PolicyAdmin(MultiDBModelAdmin):
    list_display = ("buffer", "overrides")
    exclude = ("source", "lastmodified")

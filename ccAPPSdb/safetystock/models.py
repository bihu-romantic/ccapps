from django.core.exceptions import ValidationError
from django.db import models
from ccAPPSdb.common.models import AuditModel
from ccAPPSdb.input.models import Buffer
from .policy import DEFAULTS, compile_segment, validate_parameters


class Segment(AuditModel):
    name = models.CharField(max_length=200, primary_key=True)
    description = models.TextField(blank=True, default="")
    query = models.TextField(
        help_text="例如 item.cost <= 20 AND location.name = 'Tennis shop Paris'"
    )
    sku_count = models.PositiveIntegerField(default=0, editable=False)

    def clean(self):
        try:
            compile_segment(self.query)
        except ValueError as exc:
            raise ValidationError({"query": str(exc)})

    def __str__(self):
        return self.name

    class Meta:
        ordering = ("name",)
        verbose_name = "库存分组"
        verbose_name_plural = "库存分组"


class BusinessRule(AuditModel):
    segment = models.ForeignKey(Segment, on_delete=models.CASCADE)
    rule = models.CharField(max_length=40, choices=[(k, k) for k in DEFAULTS])
    value = models.FloatField()
    priority = models.IntegerField(
        default=0, help_text="数值越小越优先，同优先级按规则ID"
    )
    description = models.TextField(blank=True, default="")

    def clean(self):
        try:
            validate_parameters({self.rule: self.value})
        except (ValueError, TypeError) as exc:
            raise ValidationError(str(exc))

    class Meta:
        ordering = ("priority", "id")
        verbose_name = "库存业务规则"
        verbose_name_plural = "库存业务规则"


class InventoryPolicy(AuditModel):
    buffer = models.OneToOneField(Buffer, on_delete=models.CASCADE)
    overrides = models.JSONField(
        default=dict,
        blank=True,
        help_text='单品覆盖，例如 {"service_level":98,"ss_min_qty":2}；成本率0.05表示年5%',
    )

    def clean(self):
        try:
            validate_parameters(self.overrides)
        except (ValueError, TypeError) as exc:
            raise ValidationError({"overrides": str(exc)})

    class Meta:
        verbose_name = "单品库存策略"
        verbose_name_plural = "单品库存策略"


class InventoryResult(models.Model):
    buffer = models.OneToOneField(Buffer, primary_key=True, on_delete=models.CASCADE)
    calculated_at = models.DateTimeField(auto_now=True)
    asof = models.DateTimeField()
    mean = models.FloatField()
    deviation = models.FloatField()
    leadtime_days = models.FloatField()
    ss = models.FloatField()
    roq = models.FloatField()
    details = models.JSONField(default=dict)

    class Meta:
        verbose_name = "库存策略计算结果"
        verbose_name_plural = "库存策略计算结果"

from django.utils.translation import gettext_lazy as _
from django.db import models


class Problem(models.Model):
    allow_report_manager_access = True

    # Database fields
    entity = models.CharField(_("entity"), db_index=True)
    owner = models.CharField(_("owner"), db_index=True)
    name = models.CharField(_("name"), db_index=True)
    description = models.CharField(_("description"))
    startdate = models.DateTimeField(_("start date"), db_index=True)
    enddate = models.DateTimeField(_("end date"), db_index=True)

    def __str__(self):
        return str(self.description)

    class Meta:
        db_table = "out_problem"
        ordering = ["startdate"]
        verbose_name = _("problem")
        verbose_name_plural = _("problems")
        default_permissions = []


class Constraint(models.Model):
    allow_report_manager_access = True

    # Database fields
    demand = models.CharField(_("demand"), null=True, db_index=True)
    forecast = models.CharField(_("forecast"), null=True, db_index=True)
    item = models.CharField(_("item"), null=True, db_index=True)
    entity = models.CharField(_("entity"), db_index=True)
    owner = models.CharField(_("owner"), db_index=True)
    name = models.CharField(_("name"), db_index=True)
    description = models.CharField(_("description"))
    startdate = models.DateTimeField(_("start date"), db_index=True)
    enddate = models.DateTimeField(_("end date"), db_index=True)

    def __str__(self):
        return str(self.demand) + " " + str(self.description)

    class Meta:
        db_table = "out_constraint"
        ordering = ["item", "startdate"]
        verbose_name = _("constraint")
        verbose_name_plural = _("constraints")
        default_permissions = []


class ResourceSummary(models.Model):
    allow_report_manager_access = True

    resource = models.CharField(_("resource"))
    startdate = models.DateTimeField(_("startdate"))
    available = models.DecimalField(
        _("available"), max_digits=20, decimal_places=8, null=True
    )
    unavailable = models.DecimalField(
        _("unavailable"), max_digits=20, decimal_places=8, null=True
    )
    setup = models.DecimalField(_("setup"), max_digits=20, decimal_places=8, null=True)
    load = models.DecimalField(_("load"), max_digits=20, decimal_places=8, null=True)
    free = models.DecimalField(_("free"), max_digits=20, decimal_places=8, null=True)
    load_confirmed = models.DecimalField(
        _("confirmed load"), max_digits=20, decimal_places=8, null=True
    )

    class Meta:
        db_table = "out_resourceplan"
        ordering = ["resource", "startdate"]
        unique_together = (("resource", "startdate"),)
        verbose_name = (
            "resource summary"  # No need to translate these since only used internally
        )
        verbose_name_plural = "resource summaries"
        default_permissions = []

from django.db import models, DEFAULT_DB_ALIAS, connections
from django.utils.translation import gettext_lazy as _

from ccAPPSdb.common.models import AuditModel, User, MultiDBManager
from ccAPPSdb.common.utils import get_databases


class SQLReport(AuditModel):
    id = models.AutoField(_("identifier"), primary_key=True)
    name = models.CharField(_("name"), db_index=True)
    sql = models.TextField(_("SQL query"), null=True, blank=True)
    description = models.CharField(_("description"), null=True, blank=True)
    user = models.ForeignKey(
        User,
        verbose_name=_("user"),
        blank=False,
        null=True,
        editable=False,
        related_name="reports",
        on_delete=models.CASCADE,
    )
    public = models.BooleanField(blank=True, default=False)

    def refreshColumns(self):
        from ccAPPSdb.common.middleware import _thread_locals

        req = getattr(_thread_locals, "request", None)
        if req:
            db = getattr(req, "database", DEFAULT_DB_ALIAS)
        else:
            db = getattr(_thread_locals, "database", DEFAULT_DB_ALIAS)
        SQLColumn.objects.filter(report=self).using(db).delete()
        if self.sql:

            with connections[
                (db if f"{db}_report" not in get_databases(True) else f"{db}_report")
            ].cursor() as cursor:

                # The query is limited to 1 row. We just need the column names at this point.
                cursor.execute("select * from (%s) as Q limit 1" % self.sql)
                if self.id:
                    cols = []
                    seq = 1
                    for f in cursor.description:
                        if f[0] in cols:
                            raise Exception("Duplicate column name '%s'" % f[0])
                        cols.append(f[0])
                        if f[1] == 1700:
                            fmt = "number"
                        elif f[1] == 1184:
                            fmt = "datetime"
                        elif f[1] in (23, 20):
                            fmt = "integer"
                        elif f[1] == 1186:
                            fmt = "duration"
                        elif f[1] == 1043:
                            fmt = "text"
                        else:
                            fmt = "character"
                        SQLColumn(
                            report=self, sequence=seq, name=f[0], format=fmt
                        ).save(using=db)
                        seq += 1

    def save(self, *args, **kwargs):
        if self.sql and "pg_" in self.sql.lower():
            raise Exception("Uhuh... What are trying to do?")
        if not self.id:
            self.refreshColumns()
        super().save(*args, **kwargs)
        self.refreshColumns()

    class Meta:
        db_table = "reportmanager_report"
        ordering = ("name",)
        verbose_name = _("my report")
        verbose_name_plural = _("my reports")

    def __str__(self):
        return self.name


class SQLColumn(models.Model):
    id = models.AutoField(_("identifier"), primary_key=True)
    report = models.ForeignKey(
        SQLReport,
        verbose_name="report",
        related_name="columns",
        on_delete=models.CASCADE,
    )
    sequence = models.IntegerField("sequence", default=1)
    name = models.CharField("name")
    format = models.CharField("format", null=True, blank=True)

    class Manager(MultiDBManager):
        def get_by_natural_key(self, report, sequence):
            return self.get(report=report, sequence=sequence)

    def natural_key(self):
        return (self.report, self.sequence)

    def __str__(self):
        return "%s.%s" % (self.report.name if self.report else "", self.name)

    class Meta:
        default_permissions = []
        db_table = "reportmanager_column"
        ordering = ("report", "sequence")
        unique_together = (("report", "sequence"),)
        verbose_name = "report column"
        verbose_name_plural = "report columns"

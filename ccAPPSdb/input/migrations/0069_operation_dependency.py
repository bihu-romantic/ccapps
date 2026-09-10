from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("input", "0068_squash_70"),
    ]

    operations = [
        migrations.CreateModel(
            name="OperationDependency",
            fields=[
                (
                    "id",
                    models.AutoField(
                        primary_key=True, serialize=False, verbose_name="identifier"
                    ),
                ),
                (
                    "operation",
                    models.ForeignKey(
                        help_text="Operation",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="dependencies",
                        to="input.operation",
                        verbose_name="operation",
                    ),
                ),
                (
                    "blockedby",
                    models.ForeignKey(
                        help_text="Blocked by operation",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="dependents",
                        to="input.operation",
                        verbose_name="blocked by operation",
                    ),
                ),
                (
                    "quantity",
                    models.DecimalField(
                        blank=True,
                        decimal_places=8,
                        default="1.0",
                        help_text="Quantity relation between the operations",
                        max_digits=20,
                        null=True,
                        verbose_name="quantity",
                    ),
                ),
                (
                    "safety_leadtime",
                    models.DurationField(
                        blank=True,
                        help_text="soft safety lead time",
                        null=True,
                        verbose_name="soft safety lead time",
                    ),
                ),
                (
                    "hard_safety_leadtime",
                    models.DurationField(
                        blank=True,
                        help_text="hard safety lead time",
                        null=True,
                        verbose_name="hard safety lead time",
                    ),
                ),
                (
                    "source",
                    models.CharField(
                        blank=True,
                        db_index=True,
                        max_length=300,
                        null=True,
                        verbose_name="source",
                    ),
                ),
                (
                    "lastmodified",
                    models.DateTimeField(
                        db_index=True,
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="last modified",
                    ),
                ),
            ],
            options={
                "verbose_name": "operation dependency",
                "verbose_name_plural": "operation dependencies",
                "db_table": "operation_dependency",
                "ordering": ["operation", "blockedby"],
                "abstract": False,
                "unique_together": {("operation", "blockedby")},
            },
        ),
    ]

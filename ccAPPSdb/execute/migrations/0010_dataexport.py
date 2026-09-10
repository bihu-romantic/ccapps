from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("execute", "0009_parameter_plansafetystockfirst"),
    ]

    operations = [
        migrations.CreateModel(
            name="DataExport",
            fields=[
                (
                    "name",
                    models.CharField(
                        db_index=True,
                        max_length=300,
                        primary_key=True,
                        serialize=False,
                        verbose_name="name",
                    ),
                ),
                (
                    "sql",
                    models.TextField(blank=True, null=True, verbose_name="sql"),
                ),
                (
                    "report",
                    models.CharField(
                        blank=True, max_length=300, null=True, verbose_name="report"
                    ),
                ),
                ("arguments", models.JSONField(blank=True, null=True)),
            ],
            options={
                "verbose_name": "execute_exports",
                "verbose_name_plural": "execute_exports",
                "db_table": "execute_export",
            },
        ),
    ]

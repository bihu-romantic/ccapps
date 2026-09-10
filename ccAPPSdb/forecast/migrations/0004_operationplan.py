from __future__ import unicode_literals

from django.db import models, migrations

from ccAPPSdb.common.migrate import AttributeMigration


class Migration(AttributeMigration):
    dependencies = [("forecast", "0003_demand_pattern")]

    initial = True

    extends_app_label = "input"
    operations = [
        migrations.AddField(
            model_name="operationplan",
            name="forecast",
            field=models.CharField(
                db_index=True,
                null=True,
                verbose_name="forecast",
                blank=True,
                max_length=300,
            ),
        )
    ]

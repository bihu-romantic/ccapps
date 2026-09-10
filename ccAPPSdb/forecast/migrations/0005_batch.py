from __future__ import unicode_literals

from django.db import models, migrations

from ccAPPSdb.common.migrate import AttributeMigration


class Migration(AttributeMigration):
    dependencies = [("forecast", "0004_operationplan")]

    extends_app_label = "forecast"

    operations = [
        migrations.AddField(
            model_name="forecast",
            name="batch",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="MTO batch name",
                max_length=300,
                null=True,
                verbose_name="batch",
            ),
        ),
    ]

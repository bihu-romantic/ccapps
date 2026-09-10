from __future__ import unicode_literals

from django.db import models, migrations

from ccAPPSdb.common.migrate import AttributeMigration


class Migration(AttributeMigration):
    dependencies = [("forecast", "0007_exports")]

    initial = True

    extends_app_label = "input"
    operations = [
        migrations.AddField(
            model_name="item",
            name="outlier_1b",
            field=models.IntegerField(
                db_index=False,
                null=True,
                verbose_name="outliers last bucket",
                blank=True,
            ),
        ),
        migrations.AddField(
            model_name="item",
            name="outlier_6b",
            field=models.IntegerField(
                db_index=False,
                null=True,
                verbose_name="outliers last 6 buckets",
                blank=True,
            ),
        ),
        migrations.AddField(
            model_name="item",
            name="outlier_12b",
            field=models.IntegerField(
                db_index=False,
                null=True,
                verbose_name="outliers last 12 buckets",
                blank=True,
            ),
        ),
    ]

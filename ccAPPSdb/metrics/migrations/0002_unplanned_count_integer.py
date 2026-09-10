from django.db import migrations, models

from ccAPPSdb.common.migrate import AttributeMigration


class Migration(AttributeMigration):
    dependencies = [("metrics", "0001_item_resource_attributes")]

    operations = [
        migrations.AlterField(
            model_name="item",
            name="unplanneddemandcount",
            field=models.IntegerField(
                blank=True,
                db_index=True,
                null=True,
                verbose_name="count of unplanned demands",
            ),
        )
    ]

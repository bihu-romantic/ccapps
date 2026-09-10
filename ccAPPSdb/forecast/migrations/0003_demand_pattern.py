from django.db import migrations, models

from ccAPPSdb.common.migrate import AttributeMigration


class Migration(AttributeMigration):
    dependencies = [("forecast", "0002_cluster")]

    extends_app_label = "input"

    operations = [
        migrations.AddField(
            model_name="item",
            name="adi",
            field=models.DecimalField(
                verbose_name="adi",
                decimal_places=6,
                blank=True,
                db_index=False,
                null=True,
                max_digits=15,
            ),
        ),
        migrations.AddField(
            model_name="item",
            name="cv2",
            field=models.DecimalField(
                verbose_name="cv2",
                decimal_places=6,
                blank=True,
                db_index=False,
                null=True,
                max_digits=15,
            ),
        ),
        migrations.AddField(
            model_name="item",
            name="demand_pattern",
            field=models.CharField(
                verbose_name="demand_pattern",
                db_index=False,
                blank=True,
                null=True,
                max_length=32,
            ),
        ),
    ]

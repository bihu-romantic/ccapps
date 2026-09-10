from django.db import migrations, models

from ccAPPSdb.common.migrate import AttributeMigration


class Migration(AttributeMigration):
    extends_app_label = "input"

    dependencies = [("input", "0044_squashed_60")]

    operations = [
        migrations.AddField(
            model_name="item",
            name="latedemandcount",
            field=models.IntegerField(
                blank=True,
                db_index=True,
                null=True,
                verbose_name="count of late demands",
            ),
        ),
        migrations.AddField(
            model_name="item",
            name="latedemandquantity",
            field=models.DecimalField(
                blank=True,
                db_index=True,
                decimal_places=8,
                max_digits=20,
                null=True,
                verbose_name="quantity of late demands",
            ),
        ),
        migrations.AddField(
            model_name="item",
            name="latedemandvalue",
            field=models.DecimalField(
                blank=True,
                db_index=True,
                decimal_places=8,
                max_digits=20,
                null=True,
                verbose_name="value of late demand",
            ),
        ),
        migrations.AddField(
            model_name="item",
            name="unplanneddemandcount",
            field=models.DecimalField(
                blank=True,
                db_index=True,
                decimal_places=8,
                max_digits=20,
                null=True,
                verbose_name="count of unplanned demands",
            ),
        ),
        migrations.AddField(
            model_name="item",
            name="unplanneddemandquantity",
            field=models.DecimalField(
                blank=True,
                db_index=True,
                decimal_places=8,
                max_digits=20,
                null=True,
                verbose_name="quantity of unplanned demands",
            ),
        ),
        migrations.AddField(
            model_name="item",
            name="unplanneddemandvalue",
            field=models.DecimalField(
                blank=True,
                db_index=True,
                decimal_places=8,
                max_digits=20,
                null=True,
                verbose_name="value of unplanned demands",
            ),
        ),
        migrations.AddField(
            model_name="resource",
            name="overloadcount",
            field=models.IntegerField(
                blank=True,
                db_index=True,
                null=True,
                verbose_name="count of capacity overload problems",
            ),
        ),
    ]

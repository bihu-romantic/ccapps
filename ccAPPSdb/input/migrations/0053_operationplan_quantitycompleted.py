from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("input", "0052_item_periodofcover")]

    operations = [
        migrations.AddField(
            model_name="operationplan",
            name="quantity_completed",
            field=models.DecimalField(
                decimal_places=8,
                default="0.00",
                max_digits=20,
                verbose_name="completed quantity",
                null=True,
                blank=True,
            ),
        )
    ]

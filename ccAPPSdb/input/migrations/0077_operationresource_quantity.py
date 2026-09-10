from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("input", "0076_operationplan_remark"),
    ]

    operations = [
        migrations.AlterField(
            model_name="operationresource",
            name="quantity",
            field=models.DecimalField(
                blank=True,
                decimal_places=8,
                default="1.00",
                help_text="Required quantity of the resource",
                max_digits=20,
                null=True,
                verbose_name="quantity",
            ),
        ),
    ]

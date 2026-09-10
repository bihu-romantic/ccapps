from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("input", "0051_item_weight_volume")]

    operations = [
        migrations.AddField(
            model_name="item",
            name="periodofcover",
            field=models.IntegerField(
                blank=True,
                help_text="period of cover of the item (in days)",
                null=True,
                verbose_name="periodofcover",
            ),
        )
    ]

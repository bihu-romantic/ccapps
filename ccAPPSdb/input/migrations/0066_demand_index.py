from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("input", "0065_demand_owner"),
    ]

    operations = [
        migrations.AlterField(
            model_name="demand",
            name="item",
            field=models.ForeignKey(
                db_index=False,
                on_delete=django.db.models.deletion.CASCADE,
                to="input.item",
                verbose_name="item",
            ),
        ),
        migrations.AddIndex(
            model_name="demand",
            index=models.Index(
                fields=["item", "location", "customer", "due"], name="demand_sorted"
            ),
        ),
    ]

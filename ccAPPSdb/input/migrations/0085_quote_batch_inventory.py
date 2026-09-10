from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [("input", "0084_quote_enhance")]

    operations = [
        migrations.CreateModel(
            name="QuoteBatchInventory",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("inventory_type", models.CharField(choices=[("raw_material", "raw material"), ("semi_finished", "semi-finished"), ("finished", "finished good")], max_length=20)),
                ("available_quantity", models.DecimalField(decimal_places=8, max_digits=20)),
                ("unit_cost", models.DecimalField(decimal_places=8, max_digits=20)),
                ("status", models.CharField(db_index=True, default="available", max_length=12)),
                ("source", models.CharField(blank=True, db_index=True, max_length=300, null=True)),
                ("lastmodified", models.DateTimeField(db_index=True, default=django.utils.timezone.now, editable=False)),
                ("consumed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="consumed_quote_batch_inventory", to="input.demand")),
                ("item", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="input.item")),
                ("location", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="input.location")),
                ("source_demand", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="quote_batch_inventory", to="input.demand")),
            ],
            options={"db_table": "quote_batch_inventory"},
        ),
        migrations.AddIndex(
            model_name="quotebatchinventory",
            index=models.Index(fields=["item", "location", "status"], name="quote_batch_item_loc_status"),
        ),
    ]

import datetime
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("input", "0066_demand_index"),
    ]

    operations = [
        migrations.AlterField(
            model_name="itemdistribution",
            name="location",
            field=models.ForeignKey(
                help_text="Destination location to be replenished",
                on_delete=django.db.models.deletion.CASCADE,
                related_name="itemdistributions_destination",
                to="input.location",
                verbose_name="location",
            ),
        ),
    ]

import datetime
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("input", "0074_buffer_maximum"),
    ]

    operations = [
        migrations.AddField(
            model_name="operationmaterial",
            name="location",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="operationmaterials",
                to="input.location",
                verbose_name="location",
            ),
        ),
    ]

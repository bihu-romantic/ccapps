import datetime
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("input", "0053_operationplan_quantitycompleted"),
    ]

    operations = [
        migrations.AddField(
            model_name="itemsupplier",
            name="batchwindow",
            field=models.DurationField(
                blank=True,
                default=datetime.timedelta(days=7),
                help_text="Proposed purchase orders within this window will be grouped together",
                null=True,
                verbose_name="batching window",
            ),
        ),
    ]

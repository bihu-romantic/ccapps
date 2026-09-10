import datetime
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("input", "0054_itemsupplier_batchwindow"),
    ]

    operations = [
        migrations.AddField(
            model_name="itemdistribution",
            name="batchwindow",
            field=models.DurationField(
                blank=True,
                default=datetime.timedelta(days=7),
                help_text="Proposed distribution orders within this window will be grouped together",
                null=True,
                verbose_name="batching window",
            ),
        ),
    ]

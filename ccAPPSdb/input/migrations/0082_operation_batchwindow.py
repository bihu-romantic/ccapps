from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("input", "0081_work_orders"),
    ]

    operations = [
        migrations.AddField(
            model_name="operation",
            name="batchwindow",
            field=models.DurationField(
                blank=True,
                help_text="The solver algorithm will scan for opportunities to create batches within this time window before and after the requirement date",
                null=True,
                verbose_name="batching window",
            ),
        ),
    ]

import zoneinfo

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("execute", "0011_alter_model_options")]

    operations = [
        migrations.AddField(
            model_name="scheduledtask",
            name="tz",
            field=models.CharField(
                blank=True,
                choices=[(i, i) for i in zoneinfo.available_timezones()],
                max_length=40,
                null=True,
                verbose_name="time zone",
            ),
        )
    ]

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("input", "0049_parameter_completed")]

    operations = [
        migrations.AddField(
            model_name="operationmaterial",
            name="offset",
            field=models.DurationField(
                blank=True,
                null=True,
                verbose_name="offset",
                help_text="Time offset from the start or end to consume or produce material",
            ),
        )
    ]

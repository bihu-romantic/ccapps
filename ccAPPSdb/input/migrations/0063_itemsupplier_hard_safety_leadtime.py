from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("input", "0062_parameter_fix_supply_path"),
    ]

    operations = [
        migrations.AddField(
            model_name="itemsupplier",
            name="hard_safety_leadtime",
            field=models.DurationField(
                blank=True,
                help_text="hard safety lead time",
                null=True,
                verbose_name="hard safety lead time",
            ),
        ),
    ]

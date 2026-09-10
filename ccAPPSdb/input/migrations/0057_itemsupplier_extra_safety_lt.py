from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("input", "0056_parameter_wipproducefullquantity")]

    operations = [
        migrations.AddField(
            model_name="itemsupplier",
            name="extra_safety_leadtime",
            field=models.DurationField(
                blank=True,
                help_text="Extra safety lead time on top of standard lead time",
                null=True,
                verbose_name="extra safety leadtime",
            ),
        )
    ]

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("output", "0011_exports"),
    ]

    operations = [
        migrations.AddField(
            model_name="resourcesummary",
            name="load_confirmed",
            field=models.DecimalField(
                decimal_places=8,
                max_digits=20,
                null=True,
                verbose_name="confirmed load",
            ),
        ),
    ]

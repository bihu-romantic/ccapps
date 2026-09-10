import django.contrib.postgres.fields
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
        ("common", "0035_user_scenario_themes"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="databases",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.CharField(max_length=300, verbose_name="databases"),
                null=True,
                size=None,
            ),
        ),
    ]

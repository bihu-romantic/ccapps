from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("common", "0039_date_time_format"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="scenario",
            options={
                "default_permissions": ("copy", "release", "promote", "add", "delete"),
                "ordering": ["name"],
                "verbose_name": "scenario",
                "verbose_name_plural": "scenarios",
            },
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("execute", "0010_dataexport"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="dataexport",
            options={
                "default_permissions": [],
                "verbose_name": "execute_exports",
                "verbose_name_plural": "execute_exports",
            },
        ),
        migrations.AlterModelOptions(
            name="scheduledtask",
            options={
                "default_permissions": ("add", "delete"),
                "verbose_name": "scheduled task",
                "verbose_name_plural": "scheduled tasks",
            },
        ),
    ]

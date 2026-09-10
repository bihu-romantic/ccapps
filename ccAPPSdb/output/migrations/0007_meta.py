from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("output", "0006_squashed_60")]

    operations = [
        migrations.AlterModelOptions(
            name="constraint",
            options={
                "default_permissions": [],
                "ordering": ["demand", "startdate"],
                "verbose_name": "constraint",
                "verbose_name_plural": "constraints",
            },
        ),
        migrations.AlterModelOptions(
            name="problem",
            options={
                "default_permissions": [],
                "ordering": ["startdate"],
                "verbose_name": "problem",
                "verbose_name_plural": "problems",
            },
        ),
        migrations.AlterModelOptions(
            name="resourcesummary",
            options={
                "default_permissions": [],
                "ordering": ["resource", "startdate"],
                "verbose_name": "resource summary",
                "verbose_name_plural": "resource summaries",
            },
        ),
    ]

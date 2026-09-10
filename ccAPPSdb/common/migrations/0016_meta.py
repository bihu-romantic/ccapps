from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("common", "0014_squashed_60")]

    operations = [
        migrations.AlterModelOptions(
            name="comment",
            options={
                "default_permissions": [],
                "ordering": ("id",),
                "verbose_name": "comment",
                "verbose_name_plural": "comments",
            },
        ),
        migrations.AlterModelOptions(
            name="scenario",
            options={
                "default_permissions": ("copy", "release"),
                "ordering": ["name"],
                "verbose_name": "scenario",
                "verbose_name_plural": "scenarios",
            },
        ),
        migrations.AlterModelOptions(
            name="userpreference",
            options={
                "default_permissions": [],
                "verbose_name": "preference",
                "verbose_name_plural": "preferences",
            },
        ),
    ]

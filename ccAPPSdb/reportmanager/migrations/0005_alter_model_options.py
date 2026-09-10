from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("reportmanager", "0004_squash_70"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="sqlcolumn",
            options={
                "default_permissions": [],
                "ordering": ("report", "sequence"),
                "verbose_name": "report column",
                "verbose_name_plural": "report columns",
            },
        ),
        migrations.AlterModelOptions(
            name="sqlreport",
            options={
                "ordering": ("name",),
                "verbose_name": "my report",
                "verbose_name_plural": "my reports",
            },
        ),
        migrations.RemoveField(
            model_name="sqlcolumn",
            name="lastmodified",
        ),
        migrations.RemoveField(
            model_name="sqlcolumn",
            name="source",
        ),
    ]

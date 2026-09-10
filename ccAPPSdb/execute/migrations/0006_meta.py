from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("execute", "0005_squashed_60")]

    operations = [
        migrations.AlterModelOptions(
            name="task",
            options={
                "default_permissions": ["view"],
                "verbose_name": "task",
                "verbose_name_plural": "tasks",
            },
        )
    ]

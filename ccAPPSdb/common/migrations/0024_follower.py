from django.db import migrations, models

import ccAPPSdb.common.fields


class Migration(migrations.Migration):
    dependencies = [("common", "0023_notifications")]

    operations = [
        migrations.AddField(
            model_name="follower",
            name="args",
            field=ccAPPSdb.common.fields.JSONBField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="SystemMessage",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                )
            ],
            options={"managed": False, "default_permissions": ()},
        ),
    ]

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
        ("common", "0033_squash_70_post"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="comment",
            options={
                "default_permissions": ("add",),
                "ordering": ("id",),
                "verbose_name": "comment",
                "verbose_name_plural": "comments",
            },
        ),
        migrations.AlterModelOptions(
            name="follower",
            options={
                "default_permissions": (),
                "verbose_name": "follower",
                "verbose_name_plural": "followers",
            },
        ),
        migrations.AlterModelOptions(
            name="notification",
            options={
                "default_permissions": [],
                "verbose_name": "notification",
                "verbose_name_plural": "notifications",
            },
        ),
        migrations.AlterModelOptions(
            name="scenario",
            options={
                "default_permissions": ("copy", "release", "promote"),
                "ordering": ["name"],
                "verbose_name": "scenario",
                "verbose_name_plural": "scenarios",
            },
        ),
    ]

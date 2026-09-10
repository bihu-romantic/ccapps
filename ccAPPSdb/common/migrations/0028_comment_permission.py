from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("common", "0027_last_currentdate"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="comment",
            options={
                "ordering": ("id",),
                "default_permissions": ("add",),
            },
        ),
    ]

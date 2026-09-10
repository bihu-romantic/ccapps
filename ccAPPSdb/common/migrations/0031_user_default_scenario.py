from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
        ("common", "0030_remove_like_indexes"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="default_scenario",
            field=models.CharField(
                blank=True,
                null=True,
                verbose_name="default scenario",
                max_length=300,
            ),
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
        ("common", "0034_alter_model_options"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="scenario_themes",
            field=models.JSONField(blank=True, null=True),
        ),
    ]

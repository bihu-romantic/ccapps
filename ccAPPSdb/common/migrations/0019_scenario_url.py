from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("common", "0018_promote_scenario")]

    operations = [
        migrations.AddField(
            model_name="scenario",
            name="help_url",
            field=models.URLField(editable=False, null=True, verbose_name="help"),
        )
    ]

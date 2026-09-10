from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("common", "0024_follower")]

    operations = [
        migrations.AddField(
            model_name="user",
            name="horizonbefore",
            field=models.IntegerField(blank=True, default=0, null=True),
        )
    ]

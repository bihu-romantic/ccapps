from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("common", "0022_notifications")]

    operations = [
        migrations.AddField(
            model_name="notification",
            name="follower",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to="common.Follower",
                verbose_name="follower",
            ),
        )
    ]

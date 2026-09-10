from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("input", "0047_mto")]

    operations = [
        migrations.AddField(
            model_name="setuprule",
            name="resource",
            field=models.ForeignKey(
                blank=True,
                help_text="Extra resource used during this changeover",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="setuprules",
                to="input.Resource",
                verbose_name="resource",
            ),
        )
    ]

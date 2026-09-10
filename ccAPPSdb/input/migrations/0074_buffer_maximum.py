import datetime
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("input", "0073_operation_dependency"),
    ]

    operations = [
        migrations.AddField(
            model_name="buffer",
            name="maximum",
            field=models.DecimalField(
                blank=True,
                decimal_places=8,
                default="0.00",
                help_text="maximum stock",
                max_digits=20,
                null=True,
                verbose_name="maximum",
            ),
        ),
        migrations.AddField(
            model_name="buffer",
            name="maximum_calendar",
            field=models.ForeignKey(
                blank=True,
                help_text="Calendar storing a time-dependent maximum stock profile",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="buffermaxima",
                to="input.calendar",
                verbose_name="maximum calendar",
            ),
        ),
        migrations.AlterField(
            model_name="buffer",
            name="minimum_calendar",
            field=models.ForeignKey(
                blank=True,
                help_text="Calendar storing a time-dependent safety stock profile",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="bufferminima",
                to="input.calendar",
                verbose_name="minimum calendar",
            ),
        ),
    ]

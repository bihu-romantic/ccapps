from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("input", "0082_operation_batchwindow"),
    ]

    operations = [
        migrations.AddField(
            model_name="demand",
            name="quote_enabled",
            field=models.BooleanField(
                default=False,
                help_text="Enable quote cost calculation for this demand",
                verbose_name="启用报价",
            ),
        ),
        migrations.AddField(
            model_name="demand",
            name="quote",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Quote cost breakdown: material, resource, operation costs",
                null=True,
                verbose_name="quote",
            ),
        ),
    ]

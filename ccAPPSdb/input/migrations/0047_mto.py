from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("input", "0046_sql_role")]

    operations = [
        migrations.AlterModelOptions(
            name="buffer",
            options={
                "ordering": ["item", "location", "batch"],
                "verbose_name": "buffer",
                "verbose_name_plural": "buffers",
            },
        ),
        migrations.AddField(
            model_name="buffer",
            name="batch",
            field=models.CharField(
                blank=True, max_length=300, null=True, verbose_name="batch"
            ),
        ),
        migrations.AlterUniqueTogether(
            name="buffer", unique_together={("item", "location", "batch")}
        ),
        migrations.AddField(
            model_name="demand",
            name="batch",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="MTO batch name",
                max_length=300,
                null=True,
                verbose_name="batch",
            ),
        ),
        migrations.AddField(
            model_name="item",
            name="type",
            field=models.CharField(
                blank=True,
                choices=[
                    ("make to stock", "make to stock"),
                    ("make to order", "make to order"),
                ],
                max_length=20,
                null=True,
                verbose_name="type",
            ),
        ),
        migrations.AddField(
            model_name="operationplan",
            name="batch",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="MTO batch name",
                max_length=300,
                null=True,
                verbose_name="batch",
            ),
        ),
    ]

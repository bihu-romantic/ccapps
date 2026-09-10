from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("reportmanager", "0005_alter_model_options"),
    ]

    operations = [
        migrations.AlterField(
            model_name="sqlcolumn",
            name="format",
            field=models.CharField(blank=True, null=True, verbose_name="format"),
        ),
        migrations.AlterField(
            model_name="sqlcolumn",
            name="name",
            field=models.CharField(verbose_name="name"),
        ),
        migrations.AlterField(
            model_name="sqlreport",
            name="description",
            field=models.CharField(blank=True, null=True, verbose_name="description"),
        ),
        migrations.AlterField(
            model_name="sqlreport",
            name="name",
            field=models.CharField(db_index=True, verbose_name="name"),
        ),
        migrations.AlterField(
            model_name="sqlreport",
            name="public",
            field=models.BooleanField(blank=True, default=False),
        ),
        migrations.AlterField(
            model_name="sqlreport",
            name="source",
            field=models.CharField(
                blank=True, db_index=True, null=True, verbose_name="source"
            ),
        ),
    ]

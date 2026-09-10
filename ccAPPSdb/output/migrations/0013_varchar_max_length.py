from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("output", "0012_resourcesummary_load_confirmed"),
    ]

    operations = [
        migrations.AlterField(
            model_name="constraint",
            name="demand",
            field=models.CharField(db_index=True, null=True, verbose_name="demand"),
        ),
        migrations.AlterField(
            model_name="constraint",
            name="description",
            field=models.CharField(verbose_name="description"),
        ),
        migrations.AlterField(
            model_name="constraint",
            name="entity",
            field=models.CharField(db_index=True, verbose_name="entity"),
        ),
        migrations.AlterField(
            model_name="constraint",
            name="forecast",
            field=models.CharField(db_index=True, null=True, verbose_name="forecast"),
        ),
        migrations.AlterField(
            model_name="constraint",
            name="item",
            field=models.CharField(db_index=True, null=True, verbose_name="item"),
        ),
        migrations.AlterField(
            model_name="constraint",
            name="name",
            field=models.CharField(db_index=True, verbose_name="name"),
        ),
        migrations.AlterField(
            model_name="constraint",
            name="owner",
            field=models.CharField(db_index=True, verbose_name="owner"),
        ),
        migrations.AlterField(
            model_name="problem",
            name="description",
            field=models.CharField(verbose_name="description"),
        ),
        migrations.AlterField(
            model_name="problem",
            name="entity",
            field=models.CharField(db_index=True, verbose_name="entity"),
        ),
        migrations.AlterField(
            model_name="problem",
            name="name",
            field=models.CharField(db_index=True, verbose_name="name"),
        ),
        migrations.AlterField(
            model_name="problem",
            name="owner",
            field=models.CharField(db_index=True, verbose_name="owner"),
        ),
        migrations.AlterField(
            model_name="resourcesummary",
            name="resource",
            field=models.CharField(verbose_name="resource"),
        ),
    ]

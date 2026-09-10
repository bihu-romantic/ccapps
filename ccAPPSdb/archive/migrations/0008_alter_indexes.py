from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("archive", "0007_varchar_max_length"),
    ]

    operations = [
        migrations.AlterField(
            model_name="archivedbuffer",
            name="item",
            field=models.CharField(verbose_name="item"),
        ),
        migrations.AlterField(
            model_name="archivedbuffer",
            name="location",
            field=models.CharField(verbose_name="location"),
        ),
        migrations.AlterField(
            model_name="archiveddemand",
            name="customer",
            field=models.CharField(verbose_name="customer"),
        ),
        migrations.AlterField(
            model_name="archiveddemand",
            name="item",
            field=models.CharField(verbose_name="item"),
        ),
        migrations.AlterField(
            model_name="archiveddemand",
            name="location",
            field=models.CharField(verbose_name="location"),
        ),
        migrations.AlterField(
            model_name="archivedoperationplan",
            name="destination",
            field=models.CharField(blank=True, null=True, verbose_name="destination"),
        ),
        migrations.AlterField(
            model_name="archivedoperationplan",
            name="item",
            field=models.CharField(verbose_name="item"),
        ),
        migrations.AlterField(
            model_name="archivedoperationplan",
            name="name",
            field=models.CharField(blank=True, null=True, verbose_name="name"),
        ),
        migrations.AlterField(
            model_name="archivedoperationplan",
            name="type",
            field=models.CharField(verbose_name="type"),
        ),
        migrations.AddIndex(
            model_name="archivedbuffer",
            index=models.Index(fields=["item"], name="ax_buffer_item_idx"),
        ),
        migrations.AddIndex(
            model_name="archivedbuffer",
            index=models.Index(fields=["location"], name="ax_buffer_location_idx"),
        ),
        migrations.AddIndex(
            model_name="archiveddemand",
            index=models.Index(fields=["customer"], name="ax_demand_customer_idx"),
        ),
        migrations.AddIndex(
            model_name="archiveddemand",
            index=models.Index(fields=["item"], name="ax_demand_item_idx"),
        ),
        migrations.AddIndex(
            model_name="archiveddemand",
            index=models.Index(fields=["location"], name="ax_demand_location_idx"),
        ),
        migrations.AddIndex(
            model_name="archivedoperationplan",
            index=models.Index(
                fields=["destination"], name="ax_opplan_destination_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="archivedoperationplan",
            index=models.Index(fields=["item"], name="ax_opplan_item_idx"),
        ),
        migrations.AddIndex(
            model_name="archivedoperationplan",
            index=models.Index(fields=["type"], name="ax_opplan_type_idx"),
        ),
        migrations.AddIndex(
            model_name="archivedoperationplan",
            index=models.Index(fields=["name"], name="ax_opplan_name_idx"),
        ),
    ]

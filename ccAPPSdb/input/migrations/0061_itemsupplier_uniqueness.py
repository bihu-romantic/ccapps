from django.db import migrations, connections, models


def cleanBadRecords(apps, schema_editor):
    db = schema_editor.connection.alias
    with connections[db].cursor() as cursor:
        # delete records that will fail with the new indexes
        cursor.execute(
            """
            with cte as
            (
            select id, row_number() over(partition by item_id, supplier_id, effective_start order by cost) as rn
            from itemsupplier
            where location_id is null
            )
            delete from itemsupplier
            using cte
            where itemsupplier.id = cte.id
            and cte.rn > 1
            """
        )


class Migration(migrations.Migration):
    dependencies = [
        ("input", "0060_remove_like_indexes"),
    ]

    operations = [
        migrations.RunPython(cleanBadRecords),
        migrations.AddConstraint(
            model_name="itemsupplier",
            constraint=models.UniqueConstraint(
                condition=models.Q(location__isnull=True),
                fields=("item", "supplier", "effective_start"),
                name="itemsupplier_partial2",
            ),
        ),
    ]

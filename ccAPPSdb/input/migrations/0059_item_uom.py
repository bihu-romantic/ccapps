from django.db import migrations, models, connections


def rename_existing_columns(apps, schema_editor):
    db = schema_editor.connection.alias
    with connections[db].cursor() as cursor:
        cursor.execute(
            """
            select count(*)
            from information_schema.columns
            where table_name='item' and column_name='uom'
            """
        )
        if cursor.fetchone()[0]:
            cursor.execute("alter table item rename column uom to original_uom")


def copy_existing_data(apps, schema_editor):
    db = schema_editor.connection.alias
    with connections[db].cursor() as cursor:
        cursor.execute(
            """
            select column_name
            from information_schema.columns
            where table_name='item'
            and column_name='original_uom'
            """
        )
        cols = ["%s=%s" % (c[0][9:], c[0]) for c in cursor.fetchall()]
        if cols:
            cursor.execute("update item set %s" % ", ".join(cols))


def drop_temp_columns(apps, schema_editor):
    db = schema_editor.connection.alias
    with connections[db].cursor() as cursor:
        cursor.execute(
            """
            select column_name
            from information_schema.columns
            where table_name='item'
            and column_name='original_uom'
            """
        )
        cols = ["drop column %s" % c[0] for c in cursor.fetchall()]
        if cols:
            cursor.execute("alter table item %s" % ", ".join(cols))


class Migration(migrations.Migration):
    dependencies = [
        ("input", "0058_nullable_unique_fields"),
    ]

    operations = [
        migrations.RunPython(rename_existing_columns),
        migrations.AddField(
            model_name="item",
            name="uom",
            field=models.CharField(
                blank=True, max_length=20, null=True, verbose_name="unit of measure"
            ),
        ),
        migrations.RunPython(copy_existing_data),
        migrations.RunPython(drop_temp_columns),
    ]

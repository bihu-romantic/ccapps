from django.db import migrations, connections


def dropIndexLike(apps, schema_editor):
    db = schema_editor.connection.alias
    output = []
    with connections[db].cursor() as cursor:
        cursor.execute(
            """
            with cte as (
            select
            t.relname as table_name,
            i.relname as index_name,
            array_to_string(array_agg(a.attname), ', ') as column_names
            from
            pg_class t,
            pg_class i,
            pg_index ix,
            pg_attribute a
            where
            t.oid = ix.indrelid
            and i.oid = ix.indexrelid
            and a.attrelid = t.oid
            and a.attnum = ANY(ix.indkey)
            and t.relkind = 'r'
            and i.relname like '%like'
            group by
            t.relname,
            i.relname
            )
            select cte.index_name
            from information_schema.table_constraints tco
            join information_schema.key_column_usage kcu
            on tco.constraint_schema = kcu.constraint_schema
            and tco.constraint_name = kcu.constraint_name
            join information_schema.referential_constraints rco
            on tco.constraint_schema = rco.constraint_schema
            and tco.constraint_name = rco.constraint_name
            join information_schema.table_constraints rel_tco
            on rco.unique_constraint_schema = rel_tco.constraint_schema
            and rco.unique_constraint_name = rel_tco.constraint_name
            inner join cte on cte.table_name = kcu.table_name and cte.column_names = kcu.column_name
            where tco.constraint_type = 'FOREIGN KEY'
            and kcu.table_name in ('common_bucketdetail')
            group by
            kcu.table_name,
            kcu.column_name,
            cte.index_name
            """
        )
        for i in cursor:
            output.append(i[0])

        cursor.execute("drop index %s" % ",".join(output))


class Migration(migrations.Migration):
    dependencies = [
        ("common", "0029_attributes"),
    ]

    operations = [
        migrations.RunPython(dropIndexLike),
    ]

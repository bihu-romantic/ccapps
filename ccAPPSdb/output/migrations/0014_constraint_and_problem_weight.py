from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("output", "0013_varchar_max_length"),
    ]

    operations = [
        migrations.RemoveField(model_name="constraint", name="weight"),
        migrations.RemoveField(model_name="problem", name="weight"),
        migrations.RunSQL(
            """
            update execute_export
               set sql = 'select
                       entity, owner, name, description,
                       to_char(startdate,''%s HH24:MI:SS'') as startdate,
                       to_char(enddate,''%s HH24:MI:SS'') as enddate
                     from out_problem
                     where name <> ''material excess''
                     order by entity, name, startdate
                    '
            where name = 'problems.csv.gz'
            and regexp_replace(sql, '\\s', '', 'g') =
               regexp_replace('select
                       entity, owner, name, description,
                       to_char(startdate,''%s HH24:MI:SS'') as startdate,
                       to_char(enddate,''%s HH24:MI:SS'') as enddate,
                       weight
                     from out_problem
                     where name <> ''material excess''
                     order by entity, name, startdate
                     ', '\\s', '', 'g')
            """,
            """
            update execute_export
               set sql = 'select
                       entity, owner, name, description,
                       to_char(startdate,''%s HH24:MI:SS'') as startdate,
                       to_char(enddate,''%s HH24:MI:SS'') as enddate,
                       weight
                     from out_problem
                     where name <> ''material excess''
                     order by entity, name, startdate
                    '
            where name = 'problems.csv.gz'
            and regexp_replace(sql, '\\s', '', 'g') =
               regexp_replace('select
                       entity, owner, name, description,
                       to_char(startdate,''%s HH24:MI:SS'') as startdate,
                       to_char(enddate,''%s HH24:MI:SS'') as enddate
                     from out_problem
                     where name <> ''material excess''
                     order by entity, name, startdate
                    ', '\\s', '', 'g')
            """,
        ),
    ]

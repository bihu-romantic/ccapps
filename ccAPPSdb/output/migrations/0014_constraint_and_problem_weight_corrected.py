from django.conf import settings
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("output", "0014_constraint_and_problem_weight"),
    ]

    DATE_FORMAT = (
        settings.DATE_FORMAT_JS if hasattr(settings, "DATE_FORMAT_JS") else "YYYY-MM-DD"
    )
    operations = [
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
            """
            % ((DATE_FORMAT,) * 4),
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
            """
            % ((DATE_FORMAT,) * 4),
        ),
    ]

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("forecast", "0006_default_parameters"),
        ("execute", "0010_dataexport"),
    ]

    operations = [
        migrations.RunSQL(
            """
            insert into execute_export
               (name, sql, report, arguments)
               values (
                 'forecastreport.csv.gz',
                 null,
                 'ccAPPSdb.forecast.views.OverviewReport',
                 '{"format": "csvlist", "buckets": "month", "horizontype": true, "horizonunit": "month", "horizonlength": 6}'
               )
            on conflict (name) do nothing
            """,
            """
            delete from execute_export where name = 'forecastreport.csv.gz'
            """,
        ),
    ]

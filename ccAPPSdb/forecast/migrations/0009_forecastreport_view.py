from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("forecast", "0008_outliers"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
              drop materialized view if exists forecastreport_view;

              create materialized view forecastreport_view as
              select distinct
                   coalesce(forecast.name, forecastplan.item_id||' @ '||
                     forecastplan.location_id||' @ '||
                     forecastplan.customer_id) as name,
                   forecastplan.item_id,
                   forecastplan.location_id,
                   forecastplan.customer_id
              from forecastplan
              left outer join forecast
                on forecast.item_id = forecastplan.item_id
                and forecast.location_id = forecastplan.location_id
                and forecast.customer_id = forecastplan.customer_id;

            create unique index on forecastreport_view (item_id, location_id, customer_id);

            refresh materialized view forecastreport_view;
            """,
            reverse_sql="""
            drop materialized view if exists forecastreport_view;

              create materialized view forecastreport_view as
              select distinct
                   coalesce(forecast.name, forecastplan.item_id||' @ '||
                     forecastplan.location_id||' @ '||
                     forecastplan.customer_id) as name,
                   forecastplan.item_id,
                   forecastplan.location_id,
                   forecastplan.customer_id,
                   coalesce(forecast.method,'aggregate') as method,
                   coalesce(forecast.out_method,'aggregate') as out_method,
                   forecast.out_smape as out_smape
              from forecastplan
              left outer join forecast
                on forecast.item_id = forecastplan.item_id
                and forecast.location_id = forecastplan.location_id
                and forecast.customer_id = forecastplan.customer_id;

            create unique index on forecastreport_view (item_id, location_id, customer_id);
            """,
        ),
    ]

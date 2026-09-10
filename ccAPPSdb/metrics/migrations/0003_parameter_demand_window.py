from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("metrics", "0002_unplanned_count_integer")]

    operations = [
        migrations.RunSQL(
            """
            insert into common_parameter (name, value, lastmodified, description)
            values (
              'metrics.demand_window', '999', now(),
              'Time horizon (in days) over which the item attributes ''late demand count/quantity/value'' and ''unplanned demand count/quantity/value'' are computed.'
              )
            on conflict (name) do nothing
            """,
            """
            delete from common_parameter where name = 'metrics.demand_window'
            """,
        )
    ]

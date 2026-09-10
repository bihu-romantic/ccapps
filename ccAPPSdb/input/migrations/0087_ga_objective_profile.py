from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("input", "0086_remove_inquiry_demand_status")]

    operations = [
        migrations.RunSQL(
            """
            insert into common_parameter (name, value, lastmodified, description)
            values (
              'ga.objective_profile', 'due_date', now(),
              'NSGA-II Pareto solution preference. Values: due_date (default), efficiency, balance'
            ) on conflict (name) do nothing
            """,
            "delete from common_parameter where name = 'ga.objective_profile'",
        )
    ]

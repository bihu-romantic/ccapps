from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("input", "0087_ga_objective_profile")]

    operations = [
        migrations.RunSQL(
            """
            insert into common_parameter (name, value, lastmodified, description)
            values (
              'ga.stage2_solutions', '1', now(),
              'How many Pareto solutions are refined in GA stage 2. Default 1.'
            ) on conflict (name) do nothing
            """,
            "delete from common_parameter where name = 'ga.stage2_solutions'",
        )
    ]

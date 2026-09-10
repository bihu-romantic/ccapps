from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("input", "0079_varchar_max_length")]

    operations = [
        migrations.RunSQL(
            """
            insert into common_parameter (name, value, lastmodified, description)
            values (
              'plan.move_approved_early', '0', now(),
              'Controls rescheduling approved orders early. Values: 0 (inactive, default), 1 (active preserving existing resource assignments, 2 (active replanning resource assignments)'
              )
            on conflict (name) do nothing
            """,
            """
            delete from common_parameter where name = 'plan.move_approved_early'
            """,
        )
    ]

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("input", "0088_ga_stage2_solutions")]

    operations = [
        migrations.RunSQL(
            """
            insert into common_parameter (name, value, lastmodified, description)
            values (
              'cp.objective_method', 'weighted', now(),
              'CP多目标方法：weighted 加权法，lexicographic 层次序列法，epsilon ε-约束法'
            ) on conflict (name) do nothing
            """,
            "delete from common_parameter where name = 'cp.objective_method'",
        )
    ]

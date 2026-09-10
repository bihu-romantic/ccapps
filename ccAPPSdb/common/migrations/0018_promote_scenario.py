from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("common", "0017_sql_role")]

    operations = [
        migrations.AlterModelOptions(
            name="Scenario",
            options={
                "db_table": "common_scenario",
                "ordering": ["name"],
                "verbose_name_plural": "scenarios",
                "verbose_name": "scenario",
                "permissions": (
                    ("copy_scenario", "Can copy a scenario"),
                    ("release_scenario", "Can release a scenario"),
                    ("promote_scenario", "Can promote a scenario"),
                ),
            },
        )
    ]

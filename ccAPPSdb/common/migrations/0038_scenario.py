import django.contrib.postgres.fields
from django.db import migrations, transaction, models

from ..models import defaultdatabase


def updateScenarioInfo(apps, schema_editor):
    from ...execute.models import ScheduledTask

    try:
        if not schema_editor.connection.settings_dict.get(
            "TEST"
        ) or schema_editor.connection.settings_dict.get("TEST").get(
            "NAME"
        ) != schema_editor.connection.settings_dict.get(
            "NAME"
        ):
            with transaction.atomic():
                ScheduledTask.updateScenario(schema_editor.connection.alias)
    except Exception as e:
        # On a new schema, the execute app may not be installed yet.
        # This is not a problem as it won't have any scheduled tasks then.
        pass


class Migration(migrations.Migration):
    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
        ("common", "0037_user_databases"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="scenario",
            name="lastrefresh",
        ),
        migrations.AddField(
            model_name="scenario",
            name="info",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="user",
            name="databases",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.CharField(max_length=300, verbose_name="databases"),
                default=defaultdatabase,
                null=True,
                size=None,
            ),
        ),
        migrations.RunPython(
            code=updateScenarioInfo,
            reverse_code=migrations.RunPython.noop,
            hints={"allow_atomic": False},
        ),
    ]

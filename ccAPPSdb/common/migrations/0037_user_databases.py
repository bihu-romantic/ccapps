from django.db import DEFAULT_DB_ALIAS
from django.db.models import Q

from django.db import migrations


def populateDatabaseField(apps, schema_editor):
    db = schema_editor.connection.alias
    if db == DEFAULT_DB_ALIAS:
        scenario_model = apps.get_model("common", "Scenario")
        user_model = apps.get_model("common", "User")
        user_scenarios = {}
        for sc in scenario_model.objects.using(db).filter(
            Q(status="In use") & ~Q(name=DEFAULT_DB_ALIAS)
        ):
            for u in (
                user_model.objects.using(sc.name)
                .filter(is_active=True)
                .only("is_active")
            ):
                if u.pk in user_scenarios:
                    user_scenarios[u.pk].append(sc.name)
                else:
                    user_scenarios[u.pk] = [sc.name]
        for u in user_model.objects.using(DEFAULT_DB_ALIAS).filter(is_active=True):
            if u.pk in user_scenarios:
                user_scenarios[u.pk].append(DEFAULT_DB_ALIAS)
            else:
                user_scenarios[u.pk] = [DEFAULT_DB_ALIAS]
            u.databases = user_scenarios[u.pk]
            u.save(update_fields=["databases"], using=DEFAULT_DB_ALIAS)


class Migration(migrations.Migration):
    dependencies = [
        ("common", "0036_user_databases"),
    ]

    operations = [
        migrations.RunPython(
            code=populateDatabaseField, reverse_code=migrations.RunPython.noop
        ),
    ]

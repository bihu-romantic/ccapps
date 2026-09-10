from django.core.management import call_command
from django.db import migrations, connections


def loadParameters(apps, schema_editor):
    from django.core.management.commands.loaddata import Command

    call_command(
        Command(),
        "parameters.json",
        app_label="odoo",
        verbosity=0,
        database=schema_editor.connection.alias,
    )


def removeParameters(apps, schema_editor):
    with connections[schema_editor.connection.alias].cursor() as cursor:
        cursor.execute("delete from common_parameter where name like 'odoo.%'")


class Migration(migrations.Migration):
    replaces = [
        ("odoo", "0001_initial"),
        ("odoo", "0002_parameters"),
        ("odoo", "0003_parameter"),
        ("odoo", "0004_parameter"),
        ("odoo", "0005_parameter"),
    ]

    initial = True

    dependencies = [
        ("common", "0014_squashed_60"),
    ]

    operations = [
        migrations.RunPython(loadParameters, removeParameters),
    ]

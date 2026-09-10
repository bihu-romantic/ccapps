from django.core.management import call_command
from django.db import migrations


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
    from django.core.management.commands.loaddata import Command

    call_command(
        Command(),
        "parameters.json",
        app_label="odoo",
        verbosity=0,
        database=schema_editor.connection.alias,
    )


class Migration(migrations.Migration):
    dependencies = [("common", "0001_initial")]

    operations = [migrations.RunPython(loadParameters, removeParameters)]

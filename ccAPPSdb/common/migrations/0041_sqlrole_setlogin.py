from django.db import migrations, connections

from ccAPPSdb.common.utils import get_databases


class Migration(migrations.Migration):
    dependencies = [("common", "0040_add_delete_scenario_permissions")]

    def alterRoleSetLogin(apps, schema_editor):
        db = schema_editor.connection.alias
        if get_databases()[db].get("SQL_ROLE"):
            with connections[db].cursor() as cursor:
                cursor.execute(
                    f"alter role {get_databases()[db].get('SQL_ROLE')} with login password '{get_databases()[db].get('PASSWORD')}'"
                )

    def alterRoleSetNoLogin(apps, schema_editor):
        db = schema_editor.connection.alias
        if get_databases()[db].get("SQL_ROLE"):
            with connections[db].cursor() as cursor:
                cursor.execute(
                    f"alter role {get_databases()[db].get('SQL_ROLE')} with nologin"
                )

    operations = [
        migrations.RunPython(code=alterRoleSetLogin, reverse_code=alterRoleSetNoLogin),
    ]

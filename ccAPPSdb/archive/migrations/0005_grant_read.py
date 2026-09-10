from django.db import migrations, connections
from django.conf import settings


def grant_read_access(apps, schema_editor):
    db = schema_editor.connection.alias
    role = settings.DATABASES[db].get("SQL_ROLE", "report_role")
    if role:
        with connections[db].cursor() as cursor:
            cursor.execute("select count(*) from pg_roles where rolname = lower(%s)", (role,))
            if not cursor.fetchone()[0]:
                cursor.execute(
                    "create role %s with nologin noinherit role current_user" % (role,)
                )
            for table in ["ax_demand", "ax_buffer", "ax_operationplan"]:
                cursor.execute("grant select on table %s to %s" % (table, role))


class Migration(migrations.Migration):
    dependencies = [
        ("archive", "0004_truncate_archive"),
    ]

    operations = [
        migrations.RunPython(
            code=grant_read_access,
        ),
    ]

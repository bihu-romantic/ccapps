from django.db import migrations


class Migration(migrations.Migration):
    """
    Addition of a parameter to enable/disable log auditing on import from folder command
    """

    dependencies = [("execute", "0012_scheduledtask_tz")]
    operations = [
        migrations.RunSQL(
            sql="""
            insert into common_parameter
            (name, value, description, lastmodified)
            values
            ('import_skips_audit_log','true','When set to true, the import data files command doesn''t create any audit log message. Default:true', now())
            on conflict (name) do nothing
            """,
            reverse_sql="""
            delete from common_parameter where name = 'import_skips_audit_log';
            """,
        )
    ]

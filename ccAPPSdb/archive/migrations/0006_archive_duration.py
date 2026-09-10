from django.db import migrations


class Migration(migrations.Migration):
    """
    Addition of a parameter to delete archived data after a duration
    """

    dependencies = [("archive", "0005_grant_read")]
    operations = [
        migrations.RunSQL(
            sql="""
            insert into common_parameter
            (name, value, description, lastmodified)
            values
            ('archive.duration','365','Archived data older than this parameter in days will be deleted. Default:365', now())
            on conflict (name) do nothing
            """,
            reverse_sql="""
            delete from common_parameter where name = 'archive.duration';
            """,
        )
    ]

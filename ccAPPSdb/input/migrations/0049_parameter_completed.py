from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("input", "0048_setuprule_resource")]

    operations = [
        migrations.RunSQL(
            """
            insert into common_parameter (name, value, lastmodified, description)
            values (
              'COMPLETED.consume_material', 'true', now(),
              'Determines whether completed manufacturing orders and distribution orders consume material or not. Default is true.'
              )
            on conflict (name) do nothing
            """,
            """
            delete from common_parameter where name = 'COMPLETED.consume_material'
            """,
        )
    ]

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("input", "0061_itemsupplier_uniqueness")]

    operations = [
        migrations.RunSQL(
            """
            insert into common_parameter (name, value, lastmodified, description)
            values (
              'plan.fixBrokenSupplyPath', 'true', now(),
              'Creates item supplier records with Unknown supplier to fix broken supply path. Default is true.'
              )
            on conflict (name) do nothing
            """,
            """
            delete from common_parameter where name = 'plan.fixBrokenSupplyPath'
            """,
        )
    ]

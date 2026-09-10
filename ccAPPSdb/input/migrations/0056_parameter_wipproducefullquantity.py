from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("input", "0055_itemdistribution_batchwindow")]

    operations = [
        migrations.RunSQL(
            """
            insert into common_parameter (name, value, lastmodified, description)
            values (
              'WIP.produce_full_quantity', 'false', now(),
              'Determines whether partially completed manufacturing orders still produce the full quantity. Default is false, i.e. partially completed manufacturing orders produce only the remaining quantity.'
              )
            on conflict (name) do nothing
            """,
            """
            delete from common_parameter where name = 'WIP.produce_full_quantity'
            """,
        )
    ]

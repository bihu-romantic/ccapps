from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("input", "0077_operationresource_quantity")]

    operations = [
        migrations.RunSQL(
            """
            insert into common_parameter (name, value, lastmodified, description)
            values (
              'plan.deliveryDuration', '0', now(),
              'The duration (in working hours) for the final shipment of a sales order to the customer. Default: 0'
              )
            on conflict (name) do nothing
            """,
            """
            delete from common_parameter where name = 'plan.deliveryDuration'
            """,
        )
    ]

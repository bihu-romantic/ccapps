from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("odoo", "0002_parameters")]

    operations = [
        migrations.RunSQL(
            """
            insert into common_parameter
            (name, value, description, lastmodified)
            values
            ('odoo.loglevel','0','Odoo connector: Set to non-zero to get a verbose log. Default is 0.', now())
            on conflict(name) do nothing
            """
        )
    ]

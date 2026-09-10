from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("odoo", "0003_parameter")]

    operations = [
        migrations.RunSQL(
            """
            insert into common_parameter
            (name, value, description, lastmodified)
            values
            ('odoo.singlecompany','0','Odoo connector: When false (the default) the connector downloads all allowed companies. When true the connector only downloads the data of the configured odoo.company.', now())
            on conflict(name) do nothing
            """
        )
    ]

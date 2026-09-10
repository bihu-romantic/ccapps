from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("odoo", "0006_squash_70")]

    operations = [
        migrations.RunSQL(
            """
            insert into common_parameter
            (name, value, description, lastmodified)
            values
            ('odoo.delta','999','Odoo 连接器：仅拉取写入日期大于当前日期减去 odoo.delta 天数的销售订单行。默认：999（拉取全部历史需求）。', now())
            on conflict(name) do nothing
            """,
            "delete from common_parameter where name = 'odoo.delta'",
        ),
    ]

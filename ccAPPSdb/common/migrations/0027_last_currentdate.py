from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("common", "0026_permissions")]

    operations = [
        migrations.RunSQL(
            """
            insert into common_parameter (name, value, description, lastmodified)
            select
            'last_currentdate',value,'此参数自动填充，存储最近一次计划执行的日期。', now()
            from common_parameter
            where name = 'currentdate'
            ON CONFLICT (name) DO NOTHING
            """
        )
    ]

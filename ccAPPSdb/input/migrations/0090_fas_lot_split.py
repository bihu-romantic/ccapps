from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("input", "0089_cp_objective_method")]

    operations = [
        migrations.RunSQL(
            """
            insert into common_parameter (name, value, lastmodified, description)
            values (
              'fas.lot_split', '1', now(),
              'FAS拆单统一开关：0关闭窗口SNP拆，1开启（范围仍受batch_policy前缀/工段限制；经济批量与备件断点exact不受此开关控制）'
            ) on conflict (name) do nothing
            """,
            "delete from common_parameter where name = 'fas.lot_split'",
        )
    ]

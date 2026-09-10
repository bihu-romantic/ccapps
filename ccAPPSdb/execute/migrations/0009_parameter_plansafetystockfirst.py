from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("execute", "0008_squash_70")]

    operations = [
        migrations.RunSQL(
            """
            delete from common_parameter
            where name in ('plan.planSafetyStockFirst', 'plan.calendar', 'allowsplits')
            """
        )
    ]

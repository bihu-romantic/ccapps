from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("input", "0080_parameter_move_approved_early")]

    operations = [
        migrations.RunSQL(
            """
            update operationplan set type = 'WO'
            where type = 'MO' and exists
            (select 1 from operation inner join operationplan opplan on opplan.operation_id = operation.name where operation.type = 'routing' and opplan.reference = operationplan.owner_id)
            """,
            """
            update operationplan set type = 'MO' where type = 'WO'
            """,
        ),
        migrations.CreateModel(
            name="WorkOrder",
            fields=[],
            options={
                "verbose_name": "work order",
                "verbose_name_plural": "work orders",
                "proxy": True,
                "indexes": [],
                "constraints": [],
            },
            bases=("input.operationplan",),
        ),
        migrations.AlterField(
            model_name="operationplan",
            name="type",
            field=models.CharField(
                choices=[
                    ("STCK", "inventory"),
                    ("MO", "manufacturing order"),
                    ("WO", "work order"),
                    ("PO", "purchase order"),
                    ("DO", "distribution order"),
                    ("DLVR", "delivery order"),
                ],
                db_index=True,
                default="MO",
                help_text="Order type",
                verbose_name="type",
            ),
        ),
    ]

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("input", "0063_itemsupplier_hard_safety_leadtime")]

    operations = [
        migrations.RunSQL(
            """
            alter sequence operationplanmaterial_id_seq cycle;
            alter sequence operationplanresource_id_seq cycle;
            """,
        )
    ]

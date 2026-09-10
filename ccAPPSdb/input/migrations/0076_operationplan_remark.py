import datetime
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("input", "0075_operationmaterial_location"),
    ]

    operations = [
        migrations.RunSQL(
            "delete from common_attribute where name = 'remark' and model = 'operationplan'",
            migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            "alter table operationplan drop column if exists remark",
            migrations.RunSQL.noop,
        ),
        migrations.AddField(
            model_name="operationplan",
            name="remark",
            field=models.CharField(
                blank=True,
                help_text="remark",
                max_length=300,
                null=True,
                verbose_name="remark",
            ),
        ),
    ]

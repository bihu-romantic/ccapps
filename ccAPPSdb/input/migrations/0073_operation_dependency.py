from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("input", "0071_operation_dependency_access"),
    ]

    operations = [
        migrations.RunSQL(
            sql="alter table operation_dependency alter column lastmodified set default now()",
        ),
    ]

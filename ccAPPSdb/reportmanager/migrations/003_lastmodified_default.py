from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("reportmanager", "0002_column")]

    operations = [
        migrations.RunSQL(
            "alter table reportmanager_report alter column lastmodified set default now()"
        ),
        migrations.RunSQL(
            "alter table reportmanager_column alter column lastmodified set default now()"
        ),
    ]

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("input", "0069_operation_dependency"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="operationplanresource",
            name="enddate",
        ),
        migrations.RemoveField(
            model_name="operationplanresource",
            name="startdate",
        ),
    ]

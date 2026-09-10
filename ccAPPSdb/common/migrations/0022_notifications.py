from django.db import migrations, transaction
from django.contrib.admin.models import LogEntry


def migrateAdminLog(apps, schema_editor):
    Comment = apps.get_model("common", "Comment")

    db = schema_editor.connection.alias
    with transaction.atomic(using=db):
        for rec in LogEntry.objects.all().using(db):
            if rec.action_flag == 1:
                rectype = "add"
            elif rec.action_flag == 2:
                rectype = "change"
            elif rec.action_flag == 3:
                rectype = "deletion"
            else:
                rectype = "comment"
            Comment(
                object_pk=rec.object_id,
                comment=rec.get_change_message(),
                lastmodified=rec.action_time,
                content_type_id=rec.content_type_id,
                user_id=rec.user_id,
                object_repr=rec.object_repr,
                processed=True,
                type=rectype,
            ).save(using=db)


class Migration(migrations.Migration):
    dependencies = [("common", "0021_notifications")]

    operations = [
        migrations.RunSQL(
            "update common_comment set object_repr = object_pk, type='comment', processed=true"
        ),
        migrations.RunPython(migrateAdminLog),
        migrations.RunSQL("truncate table django_admin_log"),
    ]

from django.db import migrations, connections


def recreateConstraintsCascade(apps, schema_editor):
    with connections[schema_editor.connection.alias].cursor() as cursor:
        cursor.execute(
            """
            select conname from pg_constraint
            inner join pg_class opplan on opplan.oid = pg_constraint.confrelid and opplan.relname = 'common_comment'
            inner join pg_class opm on opm.oid = pg_constraint.conrelid and opm.relname = 'common_notification'
            inner join pg_attribute on attname = 'comment_id' and attrelid = opm.oid and pg_attribute.attnum = any(conkey)
            """
        )
        cursor.execute(
            "alter table common_notification drop constraint %s" % cursor.fetchone()[0]
        )
        cursor.execute(
            """
            alter table common_notification
            add foreign key (comment_id)
            references public.common_comment(id) match simple
            on update no action
            on delete cascade
            deferrable initially deferred
            """
        )


class Migration(migrations.Migration):
    dependencies = [
        ("common", "0032_squash_70"),
    ]

    operations = [
        migrations.RunPython(
            code=recreateConstraintsCascade,
        ),
        migrations.RunSQL(
            sql="update common_comment set object_repr = object_pk, type='comment', processed=true",
        ),
    ]

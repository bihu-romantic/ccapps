from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("input", "0085_quote_batch_inventory"),
    ]

    operations = [
        migrations.RunSQL(
            # Inquiry is retired. Preserve existing commercial work by
            # converting it to the remaining pre-confirmation status.
            sql="UPDATE demand SET status = 'quote' WHERE status = 'inquiry'",
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]

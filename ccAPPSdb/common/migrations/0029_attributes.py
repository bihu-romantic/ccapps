from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
        ("common", "0028_comment_permission"),
    ]

    operations = [
        migrations.CreateModel(
            name="Attribute",
            fields=[
                (
                    "source",
                    models.CharField(
                        blank=True,
                        db_index=True,
                        max_length=300,
                        null=True,
                        verbose_name="source",
                    ),
                ),
                (
                    "lastmodified",
                    models.DateTimeField(
                        db_index=True,
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="last modified",
                    ),
                ),
                (
                    "id",
                    models.AutoField(
                        primary_key=True, serialize=False, verbose_name="identifier"
                    ),
                ),
                (
                    "name",
                    models.CharField(
                        db_index=True, max_length=300, verbose_name="name"
                    ),
                ),
                (
                    "label",
                    models.CharField(
                        db_index=True, max_length=300, verbose_name="label"
                    ),
                ),
                (
                    "type",
                    models.CharField(
                        blank=False,
                        choices=[
                            ("string", "string"),
                            ("boolean", "boolean"),
                            ("number", "number"),
                            ("integer", "integer"),
                            ("date", "date"),
                            ("datetime", "datetime"),
                            ("duration", "duration"),
                            ("time", "time"),
                            ("jsonb", "JSON"),
                        ],
                        max_length=20,
                        null=False,
                        verbose_name="type",
                    ),
                ),
                (
                    "editable",
                    models.BooleanField(
                        blank=True, default=True, verbose_name="editable"
                    ),
                ),
                (
                    "initially_hidden",
                    models.BooleanField(
                        blank=True, default=False, verbose_name="initially hidden"
                    ),
                ),
                (
                    "model",
                    models.CharField(choices=[], max_length=300, verbose_name="model"),
                ),
            ],
            options={
                "verbose_name": "attribute",
                "verbose_name_plural": "attributes",
                "db_table": "common_attribute",
                "ordering": ["model", "name"],
                "unique_together": {("model", "name")},
            },
        ),
    ]

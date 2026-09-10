from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("input", "0064_opm_opr_sequence_cycle"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="demand",
            name="lft",
        ),
        migrations.RemoveField(
            model_name="demand",
            name="lvl",
        ),
        migrations.RemoveField(
            model_name="demand",
            name="rght",
        ),
        migrations.AddField(
            model_name="demand",
            name="policy",
            field=models.CharField(
                blank=True,
                choices=[
                    ("independent", "independent"),
                    ("alltogether", "all together"),
                    ("inratio", "in ratio"),
                ],
                default="independent",
                help_text="Defines how sales orders are shipped together",
                max_length=15,
                null=True,
                verbose_name="policy",
            ),
        ),
        migrations.AlterField(
            model_name="demand",
            name="owner",
            field=models.CharField(
                blank=True,
                db_index=True,
                max_length=300,
                null=True,
                verbose_name="owner",
            ),
        ),
    ]

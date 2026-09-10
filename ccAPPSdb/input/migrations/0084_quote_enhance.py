from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("input", "0083_demand_quote"),
    ]

    operations = [
        # --- Item: material scrap rate ---
        migrations.AddField(
            model_name="item",
            name="scrap_rate",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Material loss percentage for this item. Overrides global quote.material_scrap_rate",
                max_digits=5,
                null=True,
                verbose_name="scrap rate %",
            ),
        ),
        # --- Customer: margin and discount rates ---
        migrations.AddField(
            model_name="customer",
            name="margin_rate",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Custom profit margin for this customer. Overrides global quote.profit_margin",
                max_digits=5,
                null=True,
                verbose_name="margin rate %",
            ),
        ),
        migrations.AddField(
            model_name="customer",
            name="discount_rate",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Discount rate for this customer",
                max_digits=5,
                null=True,
                verbose_name="discount rate %",
            ),
        ),
        # --- ItemSupplier: transport cost per unit ---
        migrations.AddField(
            model_name="itemsupplier",
            name="transport_cost_per_unit",
            field=models.DecimalField(
                blank=True,
                decimal_places=8,
                help_text="Transportation cost per purchased unit from this supplier",
                max_digits=20,
                null=True,
                verbose_name="transport cost/unit",
            ),
        ),
        # --- QuoteHistory: versioned quote audit trail ---
        migrations.CreateModel(
            name="QuoteHistory",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "demand",
                    models.ForeignKey(
                        help_text="The demand this quote was generated for",
                        on_delete=models.deletion.CASCADE,
                        related_name="quote_history",
                        to="input.demand",
                        verbose_name="demand",
                    ),
                ),
                (
                    "version",
                    models.IntegerField(
                        help_text="Auto-incremented version number per demand",
                        verbose_name="version",
                    ),
                ),
                (
                    "quote_data",
                    models.JSONField(
                        default=dict,
                        help_text="Full quote JSON snapshot at this version",
                        verbose_name="quote data",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        auto_now_add=True,
                        help_text="When this quote version was generated",
                        verbose_name="created at",
                    ),
                ),
            ],
            options={
                "db_table": "quote_history",
                "verbose_name": "quote history",
                "verbose_name_plural": "quote histories",
                "ordering": ["demand", "-version"],
            },
        ),
        migrations.AddConstraint(
            model_name="quotehistory",
            constraint=models.UniqueConstraint(
                fields=["demand", "version"],
                name="quote_history_demand_version_unique",
            ),
        ),
    ]

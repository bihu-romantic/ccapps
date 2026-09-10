from django.db import migrations


def add_parameters(apps, schema_editor):
    Parameter = apps.get_model("common", "Parameter")
    # New parameter: odoo.filter_export_purchase_order
    param, created = Parameter.objects.get_or_create(
        name="odoo.filter_export_purchase_order"
    )
    if created:
        param.value = "True"
        param.description = (
            "Odoo connector: filter purchase orders for automatic exports"
        )
        param.save()
    # New parameter: odoo.filter_export_manufacturing_order
    param, created = Parameter.objects.get_or_create(
        name="odoo.filter_export_manufacturing_order"
    )
    if created:
        param.value = "True"
        param.description = (
            "Odoo connector: filter manufacturing orders for automatic exports"
        )
        param.save()
    # New parameter: odoo.filter_export_distribution_order
    param, created = Parameter.objects.get_or_create(
        name="odoo.filter_export_distribution_order"
    )
    if created:
        param.value = "True"
        param.description = (
            "Odoo connector: filter distribution orders for automatic exports"
        )
        param.save()


class Migration(migrations.Migration):
    dependencies = [("odoo", "0001_initial")]

    operations = [migrations.RunPython(add_parameters)]

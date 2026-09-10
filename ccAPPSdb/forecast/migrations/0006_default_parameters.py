from django.db import migrations, models
from django.db.models import Case, When, Value, IntegerField, Q
from django.db.models.deletion import CASCADE

from ccAPPSdb.common.models import Parameter
from ccAPPSdb.forecast.commands import default_forecast_parameters


def setDefaultParameters(apps, schema_editor):

    db = schema_editor.connection.alias
    calendar = Parameter.getValue("forecast.calendar", db)
    if calendar not in default_forecast_parameters:
        calendar = "month"

    for param in (
        Parameter.objects.annotate(
            custom_order=Case(
                When(name="forecast.calendar", then=Value(1)),
                When(
                    ~Q(name="forecast.calendar"),
                    then=Value(2),
                ),
                output_field=IntegerField(),
            )
        )
        .all()
        .using(db)
        .filter(name__startswith="forecast.")
        .exclude(name="forecast.populateForecastTable")
        .exclude(name="forecast.runnetting")
        .order_by("custom_order")
    ):
        if param.name in default_forecast_parameters.get(calendar):
            if param.value == default_forecast_parameters.get(calendar).get(param.name):
                param.value = "default"
                param.save()


class Migration(migrations.Migration):
    dependencies = [("forecast", "0005_batch")]

    operations = [
        migrations.RunPython(
            setDefaultParameters,
            migrations.RunPython.noop,
        ),
    ]

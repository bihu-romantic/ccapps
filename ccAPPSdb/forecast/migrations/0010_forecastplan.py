from django.db import migrations, connections


def getMeasures(cursor):
    measures = {
        "forecasttotal",
        "forecastconsumed",
        "forecastnet",
        "forecastbaseline",
        "forecastoverride",
        "orderstotal",
        "ordersadjustment",
        "ordersopen",
        "ordersplanned",
        "forecastplanned",
    }
    cursor.execute("select name from measure")
    for r in cursor.fetchall():
        measures.add(r[0])
    return measures


def fromJsonbToColumns(apps, schema_editor):
    with connections[schema_editor.connection.alias].cursor() as cursor:
        measures = getMeasures(cursor)

        # Add columns
        for name in measures:
            try:
                cursor.execute(
                    f"alter table forecastplan add column {name} numeric(20,8)"
                )
            except Exception:
                pass

        # Populate the columns
        cursor.execute(
            "update forecastplan set "
            + ",".join(f"{name} = (value->>'{name}')::numeric" for name in measures)
        )

        # Drop the value column
        cursor.execute("alter table forecastplan drop column value")

        # Vacuum the table
        cursor.execute("vacuum full forecastplan")


def fromColumnsToJsonb(apps, schema_editor):
    with connections[schema_editor.connection.alias].cursor() as cursor:
        measures = getMeasures(cursor)

        # Add the value column
        cursor.execute("alter table forecastplan add column value jsonb")

        # Populate the jsob column
        cursor.execute(
            "update forecastplan set value = jsonb_strip_nulls(jsonb_build_object("
            + ",".join(f"'{name}', {name}" for name in measures)
            + "))"
        )

        # Drop measure columns
        cursor.execute(
            "alter table forecastplan "
            + ",".join(f"drop column {name}" for name in measures)
        )

        # Vacuum the table
        cursor.execute("vacuum full forecastplan")


class Migration(migrations.Migration):
    dependencies = [
        ("forecast", "0009_forecastreport_view"),
    ]

    # Running without a transaction block is needed for the vacuum full command
    atomic = False

    operations = [migrations.RunPython(fromJsonbToColumns, fromColumnsToJsonb)]

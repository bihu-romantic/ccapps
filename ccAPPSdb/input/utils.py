from django.conf import settings
from django.db import connections, DEFAULT_DB_ALIAS

from ccAPPSdb.input.models import Operation


def countItemLocations(db=DEFAULT_DB_ALIAS):
    """
    Return the number of active item locations in a database.
    This count is a key metric for understanding the value a planning tool brings.

    It's used for the pricing of the Enterprise and Cloud Editions.
    """
    try:
        with connections[db].cursor() as cursor:
            cursor.execute(
                f"""
                select greatest(
                   (SELECT COUNT(*)
                    FROM (
                      SELECT DISTINCT item_id, location_id
                      FROM (
                        {"SELECT item_id, location_id FROM forecastplan UNION ALL"
                         if "ccAPPSdb.forecast" in settings.INSTALLED_APPS
                        else ""}
                        SELECT item_id, location_id FROM operationplanmaterial
                      ) t_all
                    ) t_distinct
                   ),
                   (select count(distinct operation_id) from operationplan)
                )
                """
            )
            return cursor.fetchone()[0]
    except Exception:
        return 0


def hasRoutingOperations(request):
    """
    Used to display or hide the "Work Order" menu.
    """
    return (
        Operation.objects.using(getattr(request, "database", DEFAULT_DB_ALIAS))
        .filter(type="routing")
        .exists()
    )

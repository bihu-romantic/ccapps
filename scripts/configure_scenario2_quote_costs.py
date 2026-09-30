"""Set sample hourly resource rates and quote defaults for scenario2."""

from decimal import Decimal

from ccAPPSdb.common.models import Parameter
from ccAPPSdb.input.models import Resource


DATABASE = "scenario2"
RATES = {
    "Line 1 station 1a": "110",
    "Line 1 station 1b": "110",
    "Line 1 station 2": "120",
    "Line 1 station 3": "135",
    "Line 1 station 4": "125",
    "Line 1 station 5": "140",
    "Line 1 station 6": "150",
    "Line 1 station 7": "130",
    "Operators line 1": "55",
}
PARAMETERS = {
    "quote.calculation_mode": "bom_routing",
    "quote.history_calibration": "false",
    "quote.profit_margin": "8",
    "quote.overhead_rate": "0.08",
    "quote.material_scrap_rate": "0.02",
    "quote.tax_rate": "13",
    "quote.rounding_precision": "2",
    "quote.rounding_increment": "0.01",
    "quote.supplier_selection": "lowest_cost",
}


def run():
    for name, value in RATES.items():
        Resource.objects.using(DATABASE).filter(name=name).update(cost=Decimal(value))
    for name, value in PARAMETERS.items():
        Parameter.objects.using(DATABASE).update_or_create(
            name=name, defaults={"value": value, "description": "Scenario2 quote demo"}
        )
    print("configured %s resource rates and %s quote parameters" % (len(RATES), len(PARAMETERS)))


if __name__ == "__main__":
    run()

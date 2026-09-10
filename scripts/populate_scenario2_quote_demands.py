"""Create a repeatable 500-order quote workload in scenario2."""

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from ccAPPSdb.input.models import Demand


DATABASE = "scenario2"
TARGET_TOTAL = 500
PREFIX = "Quote Order "


def run():
    existing = Demand.objects.using(DATABASE).count()
    needed = max(0, TARGET_TOTAL - existing)
    if not needed:
        print("scenario2 already has %s demands" % existing)
        return

    templates = list(Demand.objects.using(DATABASE).order_by("name")[:10])
    if not templates:
        raise RuntimeError("scenario2 has no demand template")

    today = timezone.now().replace(hour=18, minute=0, second=0, microsecond=0)
    demands = []
    for index in range(1, needed + 1):
        template = templates[(index - 1) % len(templates)]
        name = "%s%03d" % (PREFIX, index)
        if Demand.objects.using(DATABASE).filter(name=name).exists():
            continue
        demands.append(
            Demand(
                name=name,
                description="Generated quote workload",
                customer=template.customer,
                item=template.item,
                location=template.location,
                due=today + timedelta(days=7 + (index % 90)),
                status="open",
                quantity=Decimal(500 + (index % 10) * 500),
                priority=1 + (index % 5),
                minshipment=template.minshipment,
                policy="independent",
                quote_enabled=True,
                quote={},
            )
        )
    Demand.objects.using(DATABASE).bulk_create(demands, batch_size=200)
    print("created %s quote demands; total=%s" % (
        len(demands), Demand.objects.using(DATABASE).count()
    ))


if __name__ == "__main__":
    run()

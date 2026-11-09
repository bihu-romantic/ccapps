import json
from django.core.management.base import BaseCommand
from django.db import transaction
from ccAPPSdb.common.models import Parameter
from ccAPPSdb.safetystock.inventory import compute_inventory
from ccAPPSdb.safetystock.models import Segment, BusinessRule


class Command(BaseCommand):
    help = "计算SS/ROQ；--apply将结果作为库存上下限写入Buffer"

    def add_arguments(self, parser):
        parser.add_argument("--database", default="default")
        parser.add_argument("--apply", action="store_true")
        parser.add_argument(
            "--load-demo-rules",
            action="store_true",
            help="导入此前保存的官网示例分组规则",
        )

    def handle(self, **options):
        if options["load_demo_rules"]:
            db = options["database"]
            with transaction.atomic(using=db):
                for param in (
                    Parameter.objects.using(db)
                    .filter(name__startswith="demo.inventory.raw.segment.")
                    .order_by("name")
                ):
                    data = json.loads(param.value)
                    segment = Segment(
                        name=data["name"],
                        query=data["query"],
                        description=data.get("description") or "",
                    )
                    segment.clean()
                    Segment.objects.using(db).update_or_create(
                        name=segment.name,
                        defaults={
                            "query": segment.query,
                            "description": segment.description,
                        },
                    )
                for param in (
                    Parameter.objects.using(db)
                    .filter(name__startswith="demo.inventory.raw.rule.")
                    .order_by("name")
                ):
                    data = json.loads(param.value)
                    rule = BusinessRule(
                        segment_id=data["segment"],
                        rule=data["Business Rule"],
                        value=data["value"],
                    )
                    rule.clean()
                    BusinessRule.objects.using(db).update_or_create(
                        segment_id=rule.segment_id,
                        rule=rule.rule,
                        defaults={
                            "value": rule.value,
                            "description": data.get("description") or "",
                        },
                    )
        self.stdout.write(
            json.dumps(compute_inventory(options["database"], options["apply"]))
        )

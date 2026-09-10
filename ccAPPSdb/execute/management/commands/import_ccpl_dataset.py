#
# Import a compact CCPL scheduling JSON dataset into ccAPPS input tables.
#
import json
from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS, transaction

from ccAPPSdb.common.models import Parameter
from ccAPPSdb.common.utils import get_databases
from ccAPPSdb.input.models import (
    Buffer,
    Calendar,
    CalendarBucket,
    Customer,
    Demand,
    Item,
    ItemDistribution,
    Location,
    Operation,
    OperationDependency,
    OperationMaterial,
    OperationResource,
    Resource,
    ResourceSkill,
    Skill,
)


def parse_datetime(value):
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def parse_date(value):
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d")


def parse_time(value):
    if not value:
        return None
    return datetime.strptime(value, "%H:%M:%S").time()


def seconds(value):
    if value in (None, ""):
        return None
    # Preserve fractional process times from CCPL rather than truncating them.
    return timedelta(seconds=float(value))


def dec(value, default="0"):
    if value in (None, ""):
        return Decimal(default)
    return Decimal(str(value))


class Command(BaseCommand):
    help = "Import a CCPL scheduling JSON dataset into a scenario database"

    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("file", help="Path to the CCPL dataset JSON file")
        parser.add_argument(
            "--database",
            default=DEFAULT_DB_ALIAS,
            help="Target scenario database, eg scenario6",
        )
        parser.add_argument(
            "--clean-source",
            action="store_true",
            default=False,
            help="Delete existing records created from the same source before import",
        )

    def handle(self, **options):
        database = options["database"] or DEFAULT_DB_ALIAS
        if database not in get_databases():
            raise CommandError("No database settings known for '%s'" % database)

        path = Path(options["file"])
        if not path.exists():
            raise CommandError("File not found: %s" % path)

        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)

        source = data.get("source") or data.get("dataset", {}).get("id") or path.stem
        default_location_name = data.get("defaultLocationName") or (
            "ccpl-location-%s" % source
        )
        item_names = data.get("itemNames", {})
        resource_names = data.get("resourceNames", {})

        stats = {
            "locations": 0,
            "items": 0,
            "buffers": 0,
            "resources": 0,
            "skills": 0,
            "operations": 0,
            "operationdependencies": 0,
            "operationmaterials": 0,
            "operationresources": 0,
            "customers": 0,
            "demands": 0,
            "calendars": 0,
            "calendarbuckets": 0,
        }

        def upsert(model, key, defaults):
            obj, created = model.objects.using(database).update_or_create(
                **key, defaults=defaults
            )
            return obj, created

        with transaction.atomic(using=database):
            if options["clean_source"]:
                Demand.objects.using(database).filter(source=source).delete()
                OperationDependency.objects.using(database).filter(source=source).delete()
                OperationResource.objects.using(database).filter(source=source).delete()
                OperationMaterial.objects.using(database).filter(source=source).delete()
                Operation.objects.using(database).filter(source=source).delete()
                ItemDistribution.objects.using(database).filter(source=source).delete()
                Buffer.objects.using(database).filter(source=source).delete()
                ResourceSkill.objects.using(database).filter(source=source).delete()
                Resource.objects.using(database).filter(source=source).delete()
                Item.objects.using(database).filter(source=source).delete()
                Location.objects.using(database).filter(source=source).delete()
                Customer.objects.using(database).filter(source=source).delete()
                CalendarBucket.objects.using(database).filter(source=source).delete()
                Calendar.objects.using(database).filter(source=source).delete()

            plan_start = parse_datetime(data.get("request", {}).get("planStart"))
            if plan_start:
                Parameter.objects.using(database).update_or_create(
                    name="currentdate",
                    defaults={
                        "value": plan_start.strftime("%Y-%m-%d %H:%M:%S"),
                        "description": "Imported from %s" % source,
                    },
                )

            default_location, created = upsert(
                Location,
                {"name": default_location_name},
                {"description": "Default location for %s" % source, "source": source},
            )
            if created:
                stats["locations"] += 1

            location_cache = {default_location.name: default_location}

            def get_location(name):
                if not name:
                    return default_location
                if name not in location_cache:
                    obj, created = upsert(
                        Location,
                        {"name": name},
                        {"description": name, "source": source},
                    )
                    location_cache[name] = obj
                    if created:
                        stats["locations"] += 1
                return location_cache[name]

            calendar_cache = {}
            for res_id, rows in data.get("calendarsByResourceId", {}).items():
                cal_name = "ccpl-calendar-%s" % resource_names.get(res_id, res_id)
                cal, created = upsert(
                    Calendar,
                    {"name": cal_name},
                    {
                        "description": "Imported resource calendar %s" % res_id,
                        "defaultvalue": Decimal("0"),
                        "source": source,
                    },
                )
                calendar_cache[res_id] = cal
                if created:
                    stats["calendars"] += 1
                CalendarBucket.objects.using(database).filter(
                    calendar=cal, source=source
                ).delete()
                for idx, row in enumerate(rows):
                    day = parse_date(row.get("calendarDate"))
                    if not day:
                        continue
                    CalendarBucket.objects.using(database).create(
                        calendar=cal,
                        startdate=day,
                        enddate=day + timedelta(days=1),
                        starttime=parse_time(row.get("shiftStartTime")) or time.min,
                        endtime=parse_time(row.get("shiftEndTime")) or time.max,
                        value=dec(row.get("workCapacity"), "1"),
                        priority=idx,
                        monday=True,
                        tuesday=True,
                        wednesday=True,
                        thursday=True,
                        friday=True,
                        saturday=True,
                        sunday=True,
                        source=source,
                    )
                    stats["calendarbuckets"] += 1

            item_by_id = {}
            material_location_by_id = {}
            for row in data.get("materials", []):
                name = item_names.get(row.get("id")) or "ccpl-item-%s" % row.get(
                    "materialCode", row.get("id")
                )
                item, created = upsert(
                    Item,
                    {"name": name},
                    {
                        "description": row.get("materialName"),
                        "category": row.get("materialType"),
                        "cost": dec(row.get("unitCost"), "0"),
                        "uom": row.get("unitName"),
                        "source": source,
                    },
                )
                item_by_id[row["id"]] = item
                if created:
                    stats["items"] += 1

                loc = get_location(row.get("locationName"))
                material_location_by_id[row["id"]] = loc
                _, created = upsert(
                    Buffer,
                    {"item": item, "location": loc, "batch": ""},
                    {
                        "description": "%s @ %s" % (item.name, loc.name),
                        "onhand": dec(row.get("currentStock"), "0"),
                        "source": source,
                    },
                )
                if created:
                    stats["buffers"] += 1

            skill_by_name = {}
            for row in data.get("resources", []):
                skill_name = row.get("skillName")
                if skill_name and skill_name not in skill_by_name:
                    skill, created = upsert(
                        Skill,
                        {"name": skill_name},
                        {"source": source},
                    )
                    skill_by_name[skill_name] = skill
                    if created:
                        stats["skills"] += 1

            resource_by_id = {}
            for row in data.get("resources", []):
                name = resource_names.get(row.get("id")) or "ccpl-resource-%s" % row.get(
                    "resourceCode", row.get("id")
                )
                resource, created = upsert(
                    Resource,
                    {"name": name},
                    {
                        "description": row.get("resourceName"),
                        "type": row.get("resourceType") or "default",
                        "constrained": bool(row.get("isEnabled", 1)),
                        "maximum": dec(row.get("capacity"), "1"),
                        "efficiency": dec(row.get("efficiency"), "1") * Decimal("100"),
                        "location": get_location(row.get("locationName")),
                        "available": calendar_cache.get(row.get("id")),
                        "source": source,
                    },
                )
                resource_by_id[row["id"]] = resource
                if created:
                    stats["resources"] += 1
                skill_name = row.get("skillName")
                if skill_name:
                    _, created = upsert(
                        ResourceSkill,
                        {"resource": resource, "skill": skill_by_name[skill_name]},
                        {
                            "priority": 1,
                            "effective_start": datetime(1971, 1, 1),
                            "effective_end": datetime(2030, 12, 31),
                            "source": source,
                        },
                    )
                    if created:
                        stats["skills"] += 1

            operation_produced_item = {}
            operation_produced_location = {}
            producer_by_material_id = {}
            for op in data.get("operations", []):
                for mat in data.get("operationMaterialsByOperationId", {}).get(
                    op.get("id"), []
                ):
                    ref = mat.get("$ref", "")
                    if ref.startswith("$.operationMaterials["):
                        idx = int(ref.split("[", 1)[1].split("]", 1)[0])
                        row = data["operationMaterials"][idx]
                        if row.get("materialRole") == "produce":
                            operation_produced_item[op.get("id")] = item_by_id.get(
                                row.get("materialId")
                            )
                            operation_produced_location[op.get("id")] = (
                                material_location_by_id.get(row.get("materialId"))
                            )
                            producer_by_material_id[row.get("materialId")] = op.get("id")

            operation_by_id = {}
            for row in sorted(
                data.get("operations", []), key=lambda x: x.get("sequenceNo") or 0
            ):
                resource = resource_by_id.get(row.get("resourceId"))
                op_name = "ccpl-operation-%s" % row.get("operationCode", row["id"])
                op, created = upsert(
                    Operation,
                    {"name": op_name},
                    {
                        "type": "time_per",
                        "description": row.get("operationName"),
                        "item": operation_produced_item.get(row.get("id")),
                        "location": operation_produced_location.get(row.get("id"))
                        or (resource.location if resource else default_location),
                        "owner": None,
                        "priority": row.get("sequenceNo") or row.get("priority") or 1,
                        "duration": seconds(row.get("fixedDurationSeconds")),
                        "duration_per": seconds(row.get("unitDurationSeconds")),
                        "sizeminimum": dec(row.get("sizeMinimum"), "1"),
                        "sizemultiple": (
                            dec(row.get("sizeMultiple"))
                            if row.get("sizeMultiple") not in (None, "")
                            else None
                        ),
                        "sizemaximum": (
                            dec(row.get("sizeMaximum"))
                            if row.get("sizeMaximum") not in (None, "")
                            else None
                        ),
                        "source": source,
                    },
                )
                operation_by_id[row["id"]] = op
                if created:
                    stats["operations"] += 1

                if resource:
                    skill = skill_by_name.get(row.get("requiredSkillName"))
                    _, created = OperationResource.objects.using(
                        database
                    ).update_or_create(
                        operation=op,
                        resource=resource,
                        effective_start=datetime(1971, 1, 1),
                        defaults={
                            "skill": skill,
                            "quantity": dec(row.get("resourceQuantity"), "1"),
                            "effective_end": datetime(2030, 12, 31),
                            "priority": 1,
                            "source": source,
                        },
                    )
                    if created:
                        stats["operationresources"] += 1

            for row in data.get("operationMaterials", []):
                op = operation_by_id.get(row.get("operationId"))
                item = item_by_id.get(row.get("materialId"))
                if not op or not item:
                    continue
                quantity = dec(row.get("quantity"), "1")
                if row.get("materialRole") == "consume":
                    quantity = -quantity
                material_location = material_location_by_id.get(row.get("materialId"))
                # CCPL data can keep inventory in a warehouse/line-side store
                # while the resource is modeled at the production line. Create
                # an explicit replenishment lane instead of treating those as
                # unrelated material locations.
                if material_location and material_location != op.location:
                    if row.get("materialRole") == "consume":
                        origin, destination = material_location, op.location
                    else:
                        origin, destination = op.location, material_location
                    ItemDistribution.objects.using(database).update_or_create(
                        item=item,
                        origin=origin,
                        location=destination,
                        defaults={
                            "leadtime": timedelta(0),
                            "cost": Decimal("0"),
                            "priority": 1,
                            "source": source,
                        },
                    )
                _, created = OperationMaterial.objects.using(database).update_or_create(
                    operation=op,
                    item=item,
                    effective_start=datetime(1971, 1, 1),
                    defaults={
                        "location": op.location,
                        "quantity": quantity,
                        "type": (
                            "transfer_batch"
                            if row.get("issueTiming") in ("transfer_batch", "transferBatch")
                            else row.get("issueTiming") or "start"
                        ),
                        "transferbatch": (
                            dec(row.get("transferBatch"))
                            if row.get("transferBatch") not in (None, "")
                            else (
                                dec(row.get("transferbatch"))
                                if row.get("transferbatch") not in (None, "")
                                else None
                            )
                        ),
                        "offset": (
                            seconds(row.get("offsetSeconds"))
                            if row.get("offsetSeconds") not in (None, "")
                            else None
                        ),
                        "effective_end": datetime(2030, 12, 31),
                        "priority": row.get("priority") or 1,
                        "source": source,
                    },
                )
                if created:
                    stats["operationmaterials"] += 1

            def get_operation(row, id_fields, code_fields):
                for field in id_fields:
                    if row.get(field):
                        op = operation_by_id.get(row.get(field))
                        if op:
                            return op
                for field in code_fields:
                    if row.get(field):
                        return Operation.objects.using(database).filter(
                            name="ccpl-operation-%s" % row.get(field)
                        ).first()
                return None

            for row in data.get("operationDependencies", []):
                operation = get_operation(
                    row,
                    ("operationId", "operation_id"),
                    ("operationCode", "operation_code"),
                )
                blockedby = get_operation(
                    row,
                    (
                        "blockedByOperationId",
                        "blockedbyOperationId",
                        "blockedbyId",
                        "blockedby_id",
                    ),
                    (
                        "blockedByOperationCode",
                        "blockedbyOperationCode",
                        "blockedbyCode",
                        "blockedby_code",
                    ),
                )
                if not operation or not blockedby:
                    continue
                _, created = OperationDependency.objects.using(
                    database
                ).update_or_create(
                    operation=operation,
                    blockedby=blockedby,
                    defaults={
                        "quantity": dec(row.get("quantity"), "1"),
                        "safety_leadtime": seconds(row.get("safetyLeadtimeSeconds")),
                        "hard_safety_leadtime": seconds(
                            row.get("hardSafetyLeadtimeSeconds")
                        ),
                        "source": source,
                    },
                )
                if created:
                    stats["operationdependencies"] += 1

            customer_cache = {}
            for row in data.get("demands", []):
                customer_name = row.get("customerName") or "ccpl-customer"
                if customer_name not in customer_cache:
                    customer, created = upsert(
                        Customer,
                        {"name": customer_name},
                        {"description": customer_name, "source": source},
                    )
                    customer_cache[customer_name] = customer
                    if created:
                        stats["customers"] += 1

                item = item_by_id.get(row.get("materialId"))
                if not item:
                    continue
                operation = None
                explicit_operation_id = row.get("operationId")
                if explicit_operation_id:
                    operation = operation_by_id.get(explicit_operation_id)
                elif row.get("operationCode"):
                    operation = Operation.objects.using(database).filter(
                        name="ccpl-operation-%s" % row.get("operationCode")
                    ).first()
                _, created = upsert(
                    Demand,
                    {"name": row.get("demandCode") or row["id"]},
                    {
                        "description": row.get("remark"),
                        "customer": customer_cache[customer_name],
                        "item": item,
                        "location": get_location(row.get("deliveryLocationName"))
                        if row.get("deliveryLocationName")
                        else material_location_by_id.get(row.get("materialId")),
                        "due": parse_datetime(row.get("dueTime")),
                        "status": (row.get("status") or "open").lower(),
                        "operation": operation,
                        "quantity": dec(row.get("quantity"), "0"),
                        "priority": row.get("priority") or 10,
                        "source": source,
                    },
                )
                if created:
                    stats["demands"] += 1

        self.stdout.write(self.style.SUCCESS("Imported CCPL dataset into %s" % database))
        for key in sorted(stats):
            self.stdout.write("  %-22s %s" % (key + ":", stats[key]))
        self.stdout.write("")
        self.stdout.write("Structure summary:")

        operation_consumes = defaultdict(list)
        operation_produces = defaultdict(list)
        for row in data.get("operationMaterials", []):
            item = item_by_id.get(row.get("materialId"))
            if not item:
                continue
            loc = material_location_by_id.get(row.get("materialId"))
            material = "%s @ %s" % (item.name, loc.name if loc else "-")
            if row.get("materialRole") == "produce":
                operation_produces[row.get("operationId")].append(material)
            else:
                operation_consumes[row.get("operationId")].append(material)

        for row in sorted(
            data.get("operations", []), key=lambda x: x.get("sequenceNo") or 0
        ):
            op = operation_by_id.get(row.get("id"))
            if not op:
                continue
            consumes = ", ".join(operation_consumes.get(row.get("id"), [])) or "-"
            produces = ", ".join(operation_produces.get(row.get("id"), [])) or "-"
            self.stdout.write(
                "  operation %-36s consumes [%s] produces [%s]"
                % (op.name, consumes, produces)
            )

        resource_operations = defaultdict(list)
        for row in sorted(
            data.get("operations", []), key=lambda x: x.get("sequenceNo") or 0
        ):
            resource = resource_by_id.get(row.get("resourceId"))
            op = operation_by_id.get(row.get("id"))
            if resource and op:
                resource_operations[resource.name].append(
                    (op.name, dec(row.get("resourceQuantity"), "1"))
                )

        for resource_name, ops in sorted(resource_operations.items()):
            resource = Resource.objects.using(database).get(name=resource_name)
            if len(ops) > 1:
                op_names = ", ".join("%s(qty=%s)" % (name, qty) for name, qty in ops)
                if resource.constrained and resource.maximum == Decimal("1"):
                    self.stdout.write(
                        "  bottleneck %-37s shared by [%s]; finite capacity makes them serial"
                        % (resource_name, op_names)
                    )
                else:
                    self.stdout.write(
                        self.style.WARNING(
                            "  resource %-39s shared by [%s]; check maximum=%s constrained=%s"
                            % (
                                resource_name,
                                op_names,
                                resource.maximum,
                                resource.constrained,
                            )
                        )
                    )

        missing_demand_operations = []
        for row in data.get("demands", []):
            producer_id = producer_by_material_id.get(row.get("materialId"))
            if not producer_id:
                missing_demand_operations.append(row.get("demandCode") or row.get("id"))
        if missing_demand_operations:
            self.stdout.write(
                self.style.WARNING(
                    "  demands without producing operation: %s"
                    % ", ".join(missing_demand_operations)
                )
            )
        self.stdout.write("")
        self.stdout.write(
            "Next: python -m django runplan --database=%s "
            "--constraint=capa,mfg_lt,po_lt --plantype=1 --env=supply" % database
        )

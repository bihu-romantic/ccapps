"""Import a standard ccAPPS planning dataset without a CCPL conversion layer."""
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS, transaction

from ccAPPSdb.common.models import Parameter
from ccAPPSdb.common.utils import get_databases
from ccAPPSdb.input.models import (
    Buffer, Calendar, CalendarBucket, Customer, Demand, Item, ItemDistribution, Location,
    Operation, OperationMaterial, OperationResource, Resource, ResourceSkill, Skill,
)


def dec(value, default="0"):
    return Decimal(str(default if value in (None, "") else value))


def dt(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def duration(seconds):
    # CCPL process intervals can be fractional seconds (eg 0.03 min = 1.8 s).
    # Rounding them down silently shortens a high-volume operation by minutes.
    return timedelta(seconds=float(seconds)) if seconds not in (None, "") else None


class Command(BaseCommand):
    help = "Import a standard ccAPPS planning dataset JSON file"

    def add_arguments(self, parser):
        parser.add_argument("file")
        parser.add_argument("--database", default=DEFAULT_DB_ALIAS)
        parser.add_argument("--clean", action="store_true", help="Empty target scenario first")

    def handle(self, **options):
        database = options["database"]
        if database not in get_databases():
            raise CommandError("Unknown scenario '%s'" % database)
        document = json.loads(Path(options["file"]).read_text(encoding="utf-8"))
        data = document["data"]

        # The existing empty command runs VACUUM and must not be nested in the
        # atomic import transaction.
        if options["clean"] or document.get("clean_all", False):
            from django.core import management
            management.call_command("empty", database=database, all=True, verbosity=0)

        with transaction.atomic(using=database):
            for row in document.get("parameters", []):
                Parameter.objects.using(database).update_or_create(
                    name=row["name"], defaults={"value": row["value"]}
                )

            def put(model, name, values):
                return model.objects.using(database).update_or_create(name=name, defaults=values)[0]

            locations = {r["name"]: put(Location, r["name"], {"description": r.get("description"), "source": r.get("source")}) for r in data.get("locations", [])}
            calendars = {r["name"]: put(Calendar, r["name"], {"description": r.get("description"), "defaultvalue": dec(r.get("defaultvalue")), "source": r.get("source")}) for r in data.get("calendars", [])}
            for cal in calendars.values():
                CalendarBucket.objects.using(database).filter(calendar=cal).delete()
            for r in data.get("calendarBuckets", []):
                start = dt(r.get("startdate"))
                end = dt(r.get("enddate"))
                # Date-only end values mean the following midnight in ccAPPS.
                if start and end and start == end and len(r["enddate"]) == 10:
                    end += timedelta(days=1)
                CalendarBucket.objects.using(database).create(calendar_id=r["calendar"], startdate=start, enddate=end, starttime=r.get("starttime"), endtime=r.get("endtime"), value=dec(r.get("value"), "1"), priority=r.get("priority", 0), monday=r.get("monday", True), tuesday=r.get("tuesday", True), wednesday=r.get("wednesday", True), thursday=r.get("thursday", True), friday=r.get("friday", True), saturday=r.get("saturday", True), sunday=r.get("sunday", True), source=r.get("source"))
            items = {r["name"]: put(Item, r["name"], {"description": r.get("description"), "category": r.get("category"), "cost": dec(r.get("cost")), "uom": r.get("uom"), "source": r.get("source")}) for r in data.get("items", [])}
            skills = {r["name"]: put(Skill, r["name"], {"source": r.get("source")}) for r in data.get("skills", [])}
            resources = {r["name"]: put(Resource, r["name"], {"description": r.get("description"), "category": r.get("category"), "subcategory": r.get("subcategory"), "type": r.get("type") or "default", "maximum": dec(r.get("maximum"), "1"), "efficiency": dec(r.get("efficiency"), "100"), "constrained": r.get("constrained", True), "location_id": r["location"], "available_id": r.get("available"), "maximum_calendar_id": r.get("maximum_calendar"), "source": r.get("source")}) for r in data.get("resources", [])}
            for r in data.get("resourceSkills", []):
                ResourceSkill.objects.using(database).update_or_create(resource_id=r["resource"], skill_id=r["skill"], defaults={"priority": r.get("priority", 1), "effective_start": dt(r.get("effective_start")), "effective_end": dt(r.get("effective_end")), "source": r.get("source")})
            # Parents (alternate/routing) first so child owner_id FKs resolve.
            operation_rows = sorted(data.get("operations", []), key=lambda row: 0 if not row.get("owner") else 1)
            operations = {}
            for r in operation_rows:
                operations[r["name"]] = put(Operation, r["name"], {"type": r.get("type") or "time_per", "description": r.get("description"), "item_id": r.get("item"), "location_id": r["location"], "owner_id": r.get("owner"), "priority": r.get("priority", 1), "search": r.get("search"), "duration": duration(r.get("duration")), "duration_per": duration(r.get("duration_per")), "sizeminimum": dec(r.get("sizeminimum"), "1"), "sizemultiple": dec(r.get("sizemultiple")) if r.get("sizemultiple") is not None else None, "sizemaximum": dec(r.get("sizemaximum")) if r.get("sizemaximum") is not None else None, "source": r.get("source")})
            for r in data.get("operationResources", []):
                OperationResource.objects.using(database).update_or_create(operation_id=r["operation"], resource_id=r["resource"], effective_start=dt(r.get("effective_start")), defaults={"skill_id": r.get("skill"), "quantity": dec(r.get("quantity"), "1"), "priority": r.get("priority", 1), "name": r.get("name"), "search": r.get("search"), "effective_end": dt(r.get("effective_end")), "source": r.get("source")})
            for r in data.get("operationMaterials", []):
                OperationMaterial.objects.using(database).update_or_create(operation_id=r["operation"], item_id=r["item"], effective_start=dt(r.get("effective_start")), defaults={"location_id": r["location"], "quantity": dec(r.get("quantity"), "1"), "type": r.get("type") or "start", "transferbatch": dec(r.get("transferbatch")) if r.get("transferbatch") is not None else None, "offset": duration(r.get("offset")), "priority": r.get("priority", 1), "effective_end": dt(r.get("effective_end")), "source": r.get("source")})
            for r in data.get("itemDistributions", []):
                ItemDistribution.objects.using(database).update_or_create(item_id=r["item"], origin_id=r["origin"], location_id=r["location"], defaults={"leadtime": duration(r.get("leadtime")) or timedelta(0), "cost": dec(r.get("cost")), "priority": r.get("priority", 1), "source": r.get("source")})
            for r in data.get("buffers", []):
                Buffer.objects.using(database).update_or_create(item_id=r["item"], location_id=r["location"], batch=r.get("batch", ""), defaults={"description": r.get("description"), "onhand": dec(r.get("onhand")), "source": r.get("source")})
            customers = {r["name"]: put(Customer, r["name"], {"description": r.get("description"), "source": r.get("source")}) for r in data.get("customers", [])}
            for r in data.get("demands", []):
                Demand.objects.using(database).update_or_create(name=r["name"], defaults={"description": r.get("description"), "customer_id": r["customer"], "item_id": r["item"], "location_id": r["location"], "due": dt(r.get("due")), "status": r.get("status") or "open", "quantity": dec(r.get("quantity")), "priority": r.get("priority", 10), "source": r.get("source")})
        self.stdout.write(self.style.SUCCESS("Imported standard dataset into %s" % database))

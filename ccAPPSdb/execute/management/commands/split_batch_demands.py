from decimal import Decimal, ROUND_FLOOR

from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS, transaction

from ccAPPSdb.common.utils import get_databases
from ccAPPSdb.input.models import Demand, Operation, OperationDependency


class Command(BaseCommand):
    help = "Split large item demands into batch-sized child demands for a batch process operation"

    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument(
            "--database",
            default=DEFAULT_DB_ALIAS,
            help="Target scenario database, eg scenario7",
        )
        parser.add_argument(
            "--operation",
            required=True,
            help="Batch operation name, eg ccpl-operation-OP-12",
        )
        parser.add_argument(
            "--batch-size",
            type=Decimal,
            default=None,
            help="Produced item quantity per batch. Defaults to the operation's positive material quantity.",
        )
        parser.add_argument(
            "--tail-policy",
            choices=("partial", "pad"),
            default="partial",
            help=(
                "How to create the last non-full batch demand: "
                "partial keeps the original demand quantity, pad rounds demand up to a full batch."
            ),
        )
        parser.add_argument(
            "--parent-status",
            choices=("closed", "canceled"),
            default="closed",
            help="Status assigned to original parent demands after splitting.",
        )
        parser.add_argument(
            "--configure-operation",
            action="store_true",
            default=False,
            help="Set the batch operation size min/multiple/max to 1 batch.",
        )
        parser.add_argument(
            "--remove-dependencies",
            action="store_true",
            default=False,
            help="Remove operation dependencies where the batch operation is the dependent operation.",
        )
        parser.add_argument(
            "--source-suffix",
            default="batchsplit",
            help="Suffix appended to the parent demand source for generated child demands.",
        )

    def handle(self, **options):
        raise CommandError(
            "split_batch_demands is currently disabled. "
            "Batch-split demand generation has been commented out for experiments."
        )

        """
        Disabled experiment code kept here for reference only.

        database = options["database"] or DEFAULT_DB_ALIAS
        if database not in get_databases():
            raise CommandError("No database settings known for '%s'" % database)

        operation_name = options["operation"]
        try:
            operation = (
                Operation.objects.using(database)
                .prefetch_related("operationmaterials")
                .get(name=operation_name)
            )
        except Operation.DoesNotExist:
            raise CommandError("Operation not found: %s" % operation_name)

        produced_flows = [
            flow
            for flow in operation.operationmaterials.all()
            if flow.quantity and flow.quantity > 0
        ]
        if len(produced_flows) != 1 and options["batch_size"] is None:
            raise CommandError(
                "Operation %s must have exactly one produced material, or pass --batch-size"
                % operation_name
            )
        produced_flow = produced_flows[0] if produced_flows else None
        produced_item = operation.item or (produced_flow.item if produced_flow else None)
        produced_location = operation.location or (
            produced_flow.location if produced_flow else None
        )
        if not produced_item:
            raise CommandError("Can't determine produced item for %s" % operation_name)

        batch_size = options["batch_size"] or produced_flow.quantity
        if batch_size <= 0:
            raise CommandError("--batch-size must be positive")

        parent_status = options["parent_status"]
        tail_policy = options["tail_policy"]
        source_suffix = options["source_suffix"]

        with transaction.atomic(using=database):
            deleted_children, _ = (
                Demand.objects.using(database)
                .filter(category="batch-split", subcategory=operation_name)
                .delete()
            )

            if options["configure_operation"]:
                Operation.objects.using(database).filter(name=operation_name).update(
                    sizeminimum=Decimal("1"),
                    sizemultiple=Decimal("1"),
                    sizemaximum=Decimal("1"),
                )

            removed_dependencies = 0
            if options["remove_dependencies"]:
                removed_dependencies, _ = (
                    OperationDependency.objects.using(database)
                    .filter(operation=operation)
                    .delete()
                )

            parent_qs = (
                Demand.objects.using(database)
                .filter(item=produced_item)
                .exclude(category="batch-split")
                .filter(status__in=("open", "quote"))
                .order_by("due", "priority", "name")
            )
            if produced_location:
                parent_qs = parent_qs.filter(location=produced_location)

            parents = list(parent_qs)
            created_children = 0
            for parent in parents:
                chunks = self.get_chunks(parent.quantity, batch_size, tail_policy)
                if not chunks:
                    continue

                width = max(3, len(str(len(chunks))))
                child_source = "%s:%s" % (parent.source or "manual", source_suffix)
                for idx, qty in enumerate(chunks, start=1):
                    Demand.objects.using(database).create(
                        name="%s-BATCH-%0*d" % (parent.name, width, idx),
                        description="Batch split from %s for %s" % (
                            parent.name,
                            operation_name,
                        ),
                        category="batch-split",
                        subcategory=operation_name,
                        customer=parent.customer,
                        item=parent.item,
                        location=parent.location,
                        due=parent.due,
                        status=parent.status or "open",
                        operation=parent.operation,
                        quantity=qty,
                        priority=parent.priority,
                        minshipment=parent.minshipment,
                        maxlateness=parent.maxlateness,
                        batch=parent.name,
                        source=child_source,
                    )
                    created_children += 1

                parent.status = parent_status
                parent.description = self.append_note(
                    parent.description,
                    "Split into %d %s batch demand(s) of size %s"
                    % (len(chunks), operation_name, batch_size),
                )
                parent.save(update_fields=["status", "description"], using=database)

        self.stdout.write(
            self.style.SUCCESS(
                "Split %d parent demand(s) into %d batch demand(s)"
                % (len(parents), created_children)
            )
        )
        self.stdout.write("  deleted previous child demands: %d" % deleted_children)
        self.stdout.write("  removed operation dependencies: %d" % removed_dependencies)
        self.stdout.write("  batch size: %s" % batch_size)
        self.stdout.write("  tail policy: %s" % tail_policy)
        """

    @staticmethod
    def get_chunks(quantity, batch_size, tail_policy):
        if quantity is None or quantity <= 0:
            return []
        full_batches = int((quantity / batch_size).to_integral_value(rounding=ROUND_FLOOR))
        remainder = quantity - (Decimal(full_batches) * batch_size)
        chunks = [batch_size] * full_batches
        if remainder > 0:
            chunks.append(batch_size if tail_policy == "pad" else remainder)
        return chunks

    @staticmethod
    def append_note(description, note):
        if not description:
            return note
        if note in description:
            return description
        return "%s\n%s" % (description, note)

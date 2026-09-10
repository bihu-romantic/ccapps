"""Forward finite-capacity scheduling for MRP manufacturing orders.

This FAS pass doesn't replace MRP: MRP creates material-feasible orders first.
FAS then forwards the generated MO dates through a fixed production line.
"""

from collections import defaultdict
from datetime import datetime, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS, connections, transaction
from django.utils.dateparse import parse_datetime

from ccAPPSdb.common.models import Parameter
from ccAPPSdb.common.utils import get_databases


class Command(BaseCommand):
    help = "Forward-schedule MRP manufacturing orders on their assigned resources"
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--database", default=DEFAULT_DB_ALIAS)
        parser.add_argument(
            "--start", help="Start datetime. Defaults to common_parameter currentdate."
        )
        parser.add_argument(
            "--execute", action="store_true", help="Persist the calculated schedule."
        )
        # runworker.runTask always passes --task (the async task id); accept & ignore.
        parser.add_argument("--task", type=int, default=None, help="Async task id.")

    # Expose this command to the async task runner (execute/api/<action>/) and the
    # execute page. The generic wrapTask branch only accepts commands carrying an
    # index and a getHTML method; without them it raises "Invalid task name" and the
    # API answers "Exception launching task".
    index = 350
    title = "Forward schedule (FAS)"

    @staticmethod
    def getHTML(request):
        if not request.user.has_perm("auth.run_db"):
            return None
        return (
            '<input type="checkbox" name="execute" value="true"> execute&nbsp;&nbsp;'
            '<input type="text" name="start" placeholder="start datetime (optional)" '
            'class="form-control">'
        )

    @staticmethod
    def _parse_start(value, database):
        value = value or Parameter.getValue("currentdate", database, None)
        if not value:
            raise CommandError("Missing currentdate; pass --start")
        return parse_datetime(str(value)) or datetime.fromisoformat(str(value))

    @staticmethod
    def _duration(start, end, fixed, per_unit, quantity):
        if start and end and end > start:
            return end - start
        return (fixed or timedelta()) + (per_unit or timedelta()) * float(quantity or 0)

    def _load(self, cursor):
        cursor.execute(
            """
            select mo.reference, mo.operation_id, mo.quantity, mo.startdate, mo.enddate,
                   op.priority, op.duration, op.duration_per, opr.resource_id
            from operationplan mo
            join operation op on op.name = mo.operation_id
            left join lateral (
              select resource_id from operationplanresource
              where operationplan_id = mo.reference order by id limit 1
            ) opr on true
            where mo.type = 'MO' and mo.status = 'proposed'
            order by op.priority, mo.startdate, mo.reference
            """
        )
        plans = []
        for row in cursor.fetchall():
            plans.append(
                {
                    "reference": row[0], "operation": row[1], "quantity": row[2],
                    "original_start": row[3], "original_end": row[4],
                    "priority": row[5] or 999999, "resource": row[8],
                    "duration": self._duration(row[3], row[4], row[6], row[7], row[2]),
                }
            )

        cursor.execute(
            """
            select operation_id, item_id, quantity, type, transferbatch
            from operationmaterial
            where effective_start <= now() and effective_end > now()
            """
        )
        consumes, produces = defaultdict(dict), defaultdict(dict)
        for operation, item, quantity, flow_type, transfer_batch in cursor.fetchall():
            if quantity < 0:
                consumes[operation][item] = (flow_type, transfer_batch)
            elif quantity > 0:
                produces[operation][item] = (flow_type, transfer_batch)
        return plans, consumes, produces

    @staticmethod
    def _release(predecessor, consume, produce):
        """Use the first transfer lot for a continuous-flow predecessor."""
        flow_type, transfer = consume
        upstream_type, upstream_transfer = produce
        if flow_type != "transfer_batch" and upstream_type != "transfer_batch":
            return predecessor["end"]
        transfer = transfer or upstream_transfer
        if not transfer or not predecessor["quantity"]:
            return predecessor["end"]
        fraction = min(1, float(transfer) / float(predecessor["quantity"]))
        return predecessor["start"] + predecessor["duration"] * fraction

    @staticmethod
    def _operation_order(by_operation, predecessors):
        """Return operations in material-flow order, not source row order."""
        operations = set(by_operation)
        downstream = defaultdict(set)
        indegree = {operation: 0 for operation in operations}
        for operation, upstreams in predecessors.items():
            if operation not in operations:
                continue
            for upstream, _, _ in upstreams:
                if upstream in operations and operation not in downstream[upstream]:
                    downstream[upstream].add(operation)
                    indegree[operation] += 1

        key = lambda operation: (
            by_operation[operation][0]["priority"],
            operation,
        )
        ready = sorted(
            (operation for operation, count in indegree.items() if count == 0),
            key=key,
        )
        ordered = []
        while ready:
            operation = ready.pop(0)
            ordered.append(operation)
            for child in sorted(downstream[operation], key=key):
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
                    ready.sort(key=key)
        if len(ordered) != len(operations):
            raise CommandError("Circular material dependency prevents FAS scheduling")
        return ordered

    @staticmethod
    def _load_calendars(cursor):
        """Load resource availability calendars used by the FAS pass."""
        cursor.execute("select name, available_id from resource")
        resource_calendars = dict(cursor.fetchall())
        calendar_names = {name for name in resource_calendars.values() if name}
        if not calendar_names:
            return resource_calendars, {}
        cursor.execute(
            """
            select calendar_id, priority, value, startdate, enddate,
                   monday, tuesday, wednesday, thursday, friday, saturday, sunday,
                   starttime, endtime
            from calendarbucket
            where calendar_id = any(%s)
            order by calendar_id, priority, startdate
            """,
            [list(calendar_names)],
        )
        buckets = defaultdict(list)
        for row in cursor.fetchall():
            buckets[row[0]].append(row[1:])
        return resource_calendars, buckets

    @staticmethod
    def _working_intervals(moment, calendar, buckets):
        """Return the active positive-capacity intervals for moment's date."""
        if not calendar:
            return [(datetime.combine(moment.date(), datetime.min.time()), None)]
        weekday = moment.weekday()
        matches = []
        for bucket in buckets.get(calendar, []):
            priority, value, startdate, enddate = bucket[:4]
            days = bucket[4:11]
            starttime, endtime = bucket[11:]
            if (
                value <= 0
                or not days[weekday]
                or startdate.date() > moment.date()
                or (enddate and enddate.date() <= moment.date())
            ):
                continue
            start = datetime.combine(moment.date(), starttime)
            end = datetime.combine(moment.date(), endtime)
            if end > start:
                matches.append((priority, start, end))
        # Lower priority values override later, broader buckets.
        matches.sort(key=lambda row: (row[0], row[1]))
        return [(start, end) for _, start, end in matches]

    def _schedule_on_calendar(self, earliest, duration, resource, calendars, buckets):
        """Schedule duration of work across the resource's positive calendar buckets."""
        calendar = calendars.get(resource)
        if not calendar:
            return earliest, earliest + duration
        remaining = duration
        cursor = earliest
        start = None
        # A finite horizon prevents silently looping forever on an empty calendar.
        for _ in range(731):
            intervals = self._working_intervals(cursor, calendar, buckets)
            for interval_start, interval_end in intervals:
                work_start = max(cursor, interval_start)
                if work_start >= interval_end:
                    continue
                if start is None:
                    start = work_start
                available = interval_end - work_start
                if available >= remaining:
                    return start, work_start + remaining
                remaining -= available
                cursor = interval_end
            cursor = datetime.combine(
                cursor.date() + timedelta(days=1), datetime.min.time()
            )
        raise CommandError(
            "No working calendar capacity for resource '%s' after %s"
            % (resource, earliest.isoformat(sep=" "))
        )

    def handle(self, **options):
        database = options["database"]
        if database not in get_databases():
            raise CommandError("Unknown scenario '%s'" % database)
        start_date = self._parse_start(options.get("start"), database)

        with connections[database].cursor() as cursor:
            plans, consumes, produces = self._load(cursor)
            if not plans:
                raise CommandError("No proposed manufacturing orders found; run MRP first")
            calendars, calendar_buckets = self._load_calendars(cursor)

            by_operation = defaultdict(list)
            for plan in plans:
                by_operation[plan["operation"]].append(plan)
            for group in by_operation.values():
                group.sort(key=lambda p: (p["original_start"] or datetime.min, p["reference"]))

            # Determine WIP predecessors at operation level once.
            predecessors = defaultdict(list)
            for downstream, inputs in consumes.items():
                for upstream, outputs in produces.items():
                    for item, consume in inputs.items():
                        if item in outputs:
                            predecessors[downstream].append((upstream, consume, outputs[item]))

            operation_order = self._operation_order(by_operation, predecessors)
            resource_ready = defaultdict(lambda: start_date)
            scheduled = {}
            for operation in operation_order:
                group = by_operation[operation]
                for ordinal, plan in enumerate(group):
                    earliest = start_date
                    for upstream, consume, produce in predecessors[operation]:
                        upstream_group = by_operation.get(upstream, [])
                        if not upstream_group:
                            continue
                        upstream_plan = upstream_group[min(ordinal, len(upstream_group) - 1)]
                        previous = scheduled.get(upstream_plan["reference"])
                        if previous:
                            earliest = max(earliest, self._release(previous, consume, produce))
                    if plan["resource"]:
                        earliest = max(earliest, resource_ready[plan["resource"]])
                        plan["start"], plan["end"] = self._schedule_on_calendar(
                            earliest,
                            plan["duration"],
                            plan["resource"],
                            calendars,
                            calendar_buckets,
                        )
                    else:
                        plan["start"] = earliest
                        plan["end"] = earliest + plan["duration"]
                    scheduled[plan["reference"]] = plan
                    if plan["resource"]:
                        resource_ready[plan["resource"]] = plan["end"]

            changed = [
                plan for plan in plans
                if plan["start"] != plan["original_start"] or plan["end"] != plan["original_end"]
            ]
            if options["execute"]:
                with transaction.atomic(using=database):
                    for plan in changed:
                        cursor.execute(
                            """
                            update operationplan
                               set startdate=%s, enddate=%s, source='FAS',
                                   remark='FAS forward schedule', lastmodified=now()
                             where reference=%s
                            """,
                            [plan["start"], plan["end"], plan["reference"]],
                        )
                        if plan["original_start"]:
                            cursor.execute(
                                """
                                update operationplanmaterial
                                   set flowdate=flowdate + %s, source='FAS', lastmodified=now()
                                 where operationplan_id=%s
                                """,
                                [plan["start"] - plan["original_start"], plan["reference"]],
                            )

        self.stdout.write(
            self.style.SUCCESS(
                "%s FAS schedule: %s MOs, %s changed, start=%s" % (
                    "Applied" if options["execute"] else "Previewed",
                    len(plans), len(changed), start_date.isoformat(sep=" "),
                )
            )
        )

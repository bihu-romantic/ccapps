"""Run the CP-SAT flexible job shop on the plan loaded in ccAPPS.

Reads manufacturing operation plans from the in-memory engine, solves
machine choice and start times, then writes start, end and resource back.
Routing children keep their step order. An alternate operation becomes the
machine candidates. A plan that already sits on a specific machine keeps
that machine; a resource pool is expanded only when the plan has not been
assigned to a member yet. Confirmed, approved, completed and closed plans
stay where they are and block those machines.

Independent manufacturing orders, including a CCPL OP-01..OP-N line that
is not a routing, are separate one-operation jobs. This stage does not
rebuild the material ledger used by the genetic algorithm.
"""

import logging
import os
from datetime import timedelta

logger = logging.getLogger(__name__)

_SKIP_OPERATION_TYPES = {
    "operation_itemsupplier",
    "operation_itemdistribution",
    "operation_split",
    "operation_routing",
    "operation_alternate",
}


def run_cp(time_limit_seconds=None, workers=None):
    """Schedule the loaded plan. Returns the solver result, or None."""
    import ccAPPS

    from fjsp_cp.cpsat import solve_fjsp

    if time_limit_seconds is None:
        time_limit_seconds = int(os.environ.get("CP_TIME_LIMIT_SECONDS", "60"))
    if workers is None:
        workers = int(os.environ.get("CP_WORKERS", "8"))

    jobs, machines, occupancy, origin = extract(ccAPPS)
    operation_count = sum(len(job) for job in jobs)
    logger.info(
        "CP-SAT input: %s jobs, %s operations, %s machines, %s fixed intervals",
        len(jobs),
        operation_count,
        len(machines),
        len(occupancy),
    )
    if operation_count == 0:
        logger.info("CP-SAT skipped: no movable manufacturing operations")
        return None

    result = solve_fjsp(
        [_public_job(job) for job in jobs],
        len(machines),
        time_limit_seconds=time_limit_seconds,
        workers=workers,
        occupancy=occupancy,
        log_progress=False,
    )
    logger.info(
        "CP-SAT status=%s elapsed=%.1fs%s",
        result["status"],
        result["elapsed"],
        ""
        if "makespan" not in result
        else " makespan=%s" % int(result["makespan"]),
    )
    if result["status"] not in ("OPTIMAL", "FEASIBLE"):
        logger.warning("CP-SAT schedule was not applied: %s", result["status"])
        return result

    applied = apply(jobs, machines, result["assignments"], origin)
    logger.info("CP-SAT applied %s operation plans", applied)
    result["applied"] = applied
    return result


def extract(engine):
    """Build jobs, machine names, fixed occupancy and the time origin."""
    origin = engine.settings.current
    movable = []
    locked = []
    for plan in engine.operationplans():
        if not _is_schedulable_leaf(plan):
            continue
        if _is_locked(plan):
            locked.append(plan)
            if plan.start and plan.start < origin:
                origin = plan.start
        else:
            movable.append(plan)

    machines = []
    machine_index = {}

    def index_for(resource):
        name = resource.name
        if name not in machine_index:
            machine_index[name] = len(machines)
            machines.append(resource)
        return machine_index[name]

    grouped = {}
    for plan in movable:
        job_plan = _job_plan(plan)
        grouped.setdefault(job_plan.reference, []).append(plan)

    jobs = []
    for members in grouped.values():
        members.sort(key=lambda plan: _sequence_key(plan, _job_plan(plan)))
        job = []
        for plan in members:
            candidates = []
            for candidate in _candidates(plan):
                resource = candidate["resource"]
                if resource is None or not getattr(resource, "name", None):
                    continue
                candidates.append(
                    {
                        "machine": index_for(resource),
                        "duration": candidate["duration"],
                        "operation": candidate["operation"],
                        "plan": plan,
                    }
                )
            if candidates:
                job.append(candidates)
        if job:
            jobs.append(job)

    occupancy = []
    for plan in locked:
        start = _seconds_from(origin, plan.start)
        end = _seconds_from(origin, plan.end)
        if end <= start:
            continue
        seen = set()
        for loadplan in plan.loadplans:
            if getattr(loadplan, "quantity", 0) <= 0:
                continue
            resource = getattr(loadplan, "resource", None)
            if resource is None or not getattr(resource, "constrained", True):
                continue
            name = getattr(resource, "name", None)
            if not name or name in seen:
                continue
            seen.add(name)
            occupancy.append((index_for(resource), start, end))
    return jobs, machines, occupancy, origin


def apply(jobs, machines, assignments, origin):
    """Write chosen machine and dates onto the operation plans."""
    applied = 0
    chosen = {}
    for assignment in assignments:
        job = jobs[assignment["job"]][assignment["operation"]]
        match = next(
            (
                candidate
                for candidate in job
                if candidate["machine"] == assignment["machine"]
                and candidate["duration"] == assignment["duration"]
            ),
            job[0],
        )
        chosen[id(match["plan"])] = (match, assignment)

    for match, assignment in chosen.values():
        plan = match["plan"]
        quantity = plan.quantity
        operation = match["operation"]
        if operation is not None and plan.operation != operation:
            plan.operation = operation
        resource = machines[match["machine"]]
        for loadplan in plan.loadplans:
            if getattr(loadplan, "quantity", 0) <= 0:
                continue
            if getattr(loadplan, "resource", None) != resource:
                loadplan.resource = resource
        if plan.quantity != quantity:
            plan.quantity = quantity
        plan.start_force = origin + timedelta(seconds=int(assignment["start"]))
        plan.end_force = origin + timedelta(seconds=int(assignment["end"]))
        if plan.quantity != quantity:
            plan.quantity = quantity
        applied += 1
    return applied


def _public_job(job):
    return [
        [{"machine": candidate["machine"], "duration": candidate["duration"]} for candidate in op]
        for op in job
    ]


def _is_schedulable_leaf(plan):
    operation = getattr(plan, "operation", None)
    if operation is None or getattr(plan, "hidden", False):
        return False
    if getattr(plan, "quantity", 0) <= 0:
        return False
    if operation.__class__.__name__ in _SKIP_OPERATION_TYPES:
        return False
    if getattr(operation, "hidden", False):
        return False
    return bool(_candidates(plan))


def _is_locked(plan):
    return bool(
        getattr(plan, "confirmed", False)
        or getattr(plan, "approved", False)
        or getattr(plan, "completed", False)
        or getattr(plan, "closed", False)
    )


def _job_plan(plan):
    current = plan
    seen = set()
    routing = None
    while getattr(current, "owner", None) is not None and id(current) not in seen:
        seen.add(id(current))
        current = current.owner
        if current.operation.__class__.__name__ == "operation_routing":
            routing = current
    return routing or plan


def _sequence_key(plan, job_plan):
    if job_plan is plan or job_plan.operation.__class__.__name__ != "operation_routing":
        return (0, str(plan.reference))
    operation = plan.operation
    routing = job_plan.operation
    while getattr(operation, "owner", None) is not None and operation.owner != routing:
        operation = operation.owner
    priority = 10**6
    if getattr(operation, "owner", None) == routing:
        for sub in routing.suboperations:
            if sub.operation.name == operation.name:
                priority = sub.priority if sub.priority is not None else priority
                break
    return (priority, str(plan.reference))


def _candidates(plan):
    operation = plan.operation
    owner = getattr(operation, "owner", None)
    if owner is not None and owner.__class__.__name__ == "operation_alternate":
        found = []
        for sub in owner.suboperations:
            found.extend(_operation_candidates(sub.operation, plan.quantity))
        return found
    assigned = _assigned_leaf_resources(plan)
    if assigned:
        duration = _duration_seconds(operation, plan.quantity)
        return [
            {"resource": resource, "operation": operation, "duration": duration}
            for resource in assigned
        ]
    return _operation_candidates(operation, plan.quantity)


def _assigned_leaf_resources(plan):
    """Machines this plan already occupies. A resource group is not a choice."""
    found = []
    seen = set()
    for loadplan in plan.loadplans:
        if getattr(loadplan, "quantity", 0) <= 0:
            continue
        resource = getattr(loadplan, "resource", None)
        if resource is None or not getattr(resource, "constrained", True):
            continue
        if any(True for _member in resource.members):
            continue
        name = getattr(resource, "name", None)
        if not name or name in seen:
            continue
        seen.add(name)
        found.append(resource)
    return found


def _operation_candidates(operation, quantity):
    if operation is None or operation.__class__.__name__ in _SKIP_OPERATION_TYPES:
        return []
    loads = [load for load in operation.loads if not getattr(load, "hidden", False)]
    if not loads:
        return []
    duration = _duration_seconds(operation, quantity)
    resources = _leaf_resources(loads[0].resource)
    return [
        {"resource": resource, "operation": operation, "duration": duration}
        for resource in resources
    ]


def _leaf_resources(resource):
    if resource is None or not getattr(resource, "constrained", True):
        return []
    if getattr(resource, "hidden", False):
        return []
    members = [member for member in resource.members if member is not None]
    if not members:
        return [resource]
    found = []
    seen = set()
    for member in members:
        for leaf in _leaf_resources(member):
            if leaf.name in seen:
                continue
            seen.add(leaf.name)
            found.append(leaf)
    return found


def _duration_seconds(operation, quantity):
    duration = int(getattr(operation, "duration", 0) or 0)
    if operation.__class__.__name__ == "operation_time_per":
        per = float(getattr(operation, "duration_per", 0) or 0)
        duration += int(round(per * float(quantity or 0)))
    return max(1, duration)


def _seconds_from(origin, moment):
    if moment is None or origin is None:
        return 0
    return max(0, int((moment - origin).total_seconds()))

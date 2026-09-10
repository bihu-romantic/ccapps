"""
ccAPPS Python module shim.

Loads the C++ shared library, initializes the ccAPPS module, and re-exports
all types and functions so that "import ccAPPS" works transparently.
"""
import ctypes
import json
import os
import sys
from datetime import timedelta
from datetime import datetime
from pathlib import Path

# Set ccAPPS_HOME so the engine can find init files
os.environ.setdefault("ccAPPS_HOME", os.path.join(os.path.dirname(__file__), "bin"))

_RTLD_LAZY = 0x1
_RTLD_GLOBAL = 0x8

# Load the shared library. Python symbols are resolved from the python
# binary (which exports them globally) rather than from a separate
# libpython shared library.

_lib = ctypes.CDLL(
    os.path.join(os.path.dirname(__file__), "bin", "libccAPPS.so"),
    mode=_RTLD_LAZY | _RTLD_GLOBAL,
)

# Call ccAPPSInitialize to set up the Python module
# C++ mangled name for ccAPPSInitialize(bool) in Itanium ABI
_lib._Z16ccAPPSInitializeb.argtypes = [ctypes.c_bool]
_lib._Z16ccAPPSInitializeb.restype = None
_lib._Z16ccAPPSInitializeb(False)

# Re-export everything from the C++-created module into this namespace
import ccAPPS as _cpp_module
__all__ = [x for x in dir(_cpp_module) if not x.startswith("_")]
for _name in __all__:
    globals()[_name] = getattr(_cpp_module, _name)

# The C++ cache type is immutable, but the Django code expects a writable
# "maximum" attribute. Wrap it in a mutable proxy.
class _CacheProxy:
    def __init__(self, _cache_type):
        self.maximum = 1000000  # default value
        self._type = _cache_type
    def clear(self):
        return self._type.clear()
    def flush(self):
        return self._type.flush()
    def printStatus(self):
        return self._type.printStatus()
# Wrap the singleton instance (not the class), so flush/clear/printStatus
# work as bound methods. Capture it before the proxy clobbers the name below.
_cache_proxy = _CacheProxy(getattr(_cpp_module, "cache"))

# Fix truncated attribute names in the C++ module.
# The C++ code strips a module prefix but removes one character too many,
# leaving singular names without their first letter (e.g. "uffer" for "buffer").
# This mapping restores the correct names expected by the Django application.
_truncated_to_correct = {}
for _attr in dir(_cpp_module):
    if _attr.startswith("_") or len(_attr) < 3:
        continue
    # Compound names: truncated itemsupplier -> itemsupplier, etc.
    # The truncation only affects the FIRST segment, so:
    #   "tem_mts" -> "item_mts", "emand_forecast" -> "demand_forecast"
    #   "peration_fixed_time" -> "operation_fixed_time"
    # Plural/correct names already have the first letter, skip them.
    _obj = getattr(_cpp_module, _attr)
    if not callable(_obj):
        continue

# Map of known correct singular names to their truncated counterparts
_corrections = {
    # === Singular entity constructors (first letter truncated) ===
    "buffer": "uffer",
    "bucket": "ucket",
    "calendar": "alendar",
    "customer": "ustomer",
    "demand": "emand",
    "flow": "low",
    "interruption": "nterruption",
    "item": "tem",
    "load": "oad",
    "location": "ocation",
    "operation": "peration",
    "operationplan": "perationplan",
    "problem": "roblem",
    "resource": "esource",
    "setupmatrix": "etupmatrix",
    "skill": "kill",
    "solver": "olver",
    "suboperation": "uboperation",
    "supplier": "upplier",
    # === Default/alternate constructors ===
    "buffer_default": "uffer_default",
    "buffer_infinite": "uffer_infinite",
    "calendar_default": "alendar_default",
    "calendarEventIterator": "alendarEventIterator",
    "calendarIterator": "alendarIterator",
    "customer_default": "ustomer_default",
    "customerIterator": "ustomerIterator",
    "demand_default": "emand_default",
    "demand_forecast": "emand_forecast",
    "demand_forecastbucket": "emand_forecastbucket",
    "demand_forecastbucketIterator": "emand_forecastbucketIterator",
    "demand_group": "emand_group",
    "demandIterator": "emandIterator",
    "flow_transfer_batch": "low_transfer_batch",
    "flowIterator": "lowIterator",
    "flowplan": "lowplan",
    "flowplanIterator": "lowplanIterator",
    "item_mto": "tem_mto",
    "item_mts": "tem_mts",
    "itemIterator": "temIterator",
    "itemdistribution": "temdistribution",
    "itemdistributionIterator": "temdistributionIterator",
    "itemsupplier": "temsupplier",
    "itemsupplierIterator": "temsupplierIterator",
    "loadIterator": "oadIterator",
    "loadplan": "oadplan",
    "loadplanIterator": "oadplanIterator",
    "location_default": "ocation_default",
    "locationIterator": "ocationIterator",
    "measure": "easure",
    "measure_aggregated": "easure_aggregated",
    "measure_aggregatedplanned": "easure_aggregatedplanned",
    "measure_computed": "easure_computed",
    "measure_computedplanned": "easure_computedplanned",
    "measure_local": "easure_local",
    "measure_temp": "easure_temp",
    "operation_alternate": "peration_alternate",
    "operation_delivery": "peration_delivery",
    "operation_fixed_time": "peration_fixed_time",
    "operation_inventory": "peration_inventory",
    "operation_itemdistribution": "peration_itemdistribution",
    "operation_itemdistributionIterator": "peration_itemdistributionIterator",
    "operation_itemsupplier": "peration_itemsupplier",
    "operation_routing": "peration_routing",
    "operation_split": "peration_split",
    "operation_time_per": "peration_time_per",
    "operationIterator": "perationIterator",
    "operationdependency": "perationdependency",
    "operationdependencyIterator": "perationdependencyIterator",
    "operationplanIterator": "perationplanIterator",
    "operationplan_interruptionIterator": "perationplan interruptionIterator",
    "operationplandependency": "perationplandependency",
    "operationplandependencyIterator": "perationplandependencyIterator",
    "parameters": "arameters",
    "resource_buckets": "esource_buckets",
    "resource_default": "esource_default",
    "resource_infinite": "esource_infinite",
    "resourceIterator": "esourceIterator",
    "resourceplanIterator": "esourceplanIterator",
    "resourceskill": "esourceskill",
    "resourceskillIterator": "esourceskillIterator",
    "setupmatrix_default": "etupmatrix_default",
    "setupmatrixIterator": "etupmatrixIterator",
    "setupmatrixrule": "etupmatrixrule",
    "setupmatrixrule_default": "etupmatrixrule_default",
    "setupmatrixruleIterator": "etupmatrixruleIterator",
    "skill_default": "kill_default",
    "skillIterator": "killIterator",
    "solverACO": "olverACO",
    "solverGA": "olverGA",
    "solverVNS": "olverVNS",
    "solverExact": "olverExact",
    "solverFAS": "olverFAS",
    "solver_delete": "olver_delete",
    "solver_forecast": "olver_forecast",
    "solver_mrp": "olver_mrp",
    "solver_propagateStatus": "olver_propagateStatus",
    "supplier_default": "upplier_default",
    "supplierIterator": "upplierIterator",
    # === Other ===
    "cache": "ache",
    "commandmanager": "ommandmanager",
    "delete": "erase",
    "loggingDemandIterator": "eggingDemandIterator",
    "loggingIterator": "eggingIterator",
    "problemIterator": "roblemIterator",
    "setupevent": "etupevent",
    "run_exact": "run_exact",
    # === Alternate names ===
    "alternateOperationIterator": "lternateOperationIterator",
    "alternateResourceIterator": "lternateResourceIterator",
}

for _correct, _truncated in _corrections.items():
    if hasattr(_cpp_module, _truncated):
        # Set on both the C++ module (so import ccAPPS finds it) and
        # the current namespace (for internal use during this import)
        setattr(_cpp_module, _correct, getattr(_cpp_module, _truncated))
        globals()[_correct] = getattr(_cpp_module, _truncated)
        if _correct not in __all__:
            __all__.append(_correct)

# Replace the immutable cache type with the mutable proxy
setattr(_cpp_module, "cache", _cache_proxy)
globals()["cache"] = _cache_proxy
if "cache" not in __all__:
    __all__.append("cache")


def _vns_parse_ratio(name, data):
    ratio = data.get("ratio", "0/1")
    p_txt, q_txt = ratio.split("/", 1)
    return {
        "name": name,
        "p": int(p_txt),
        "q": int(q_txt),
        "priority": data.get("priority", "LPRC"),
    }


def _vns_ratios_from_sidecar(sidecar):
    ratios = [
        _vns_parse_ratio(name, data)
        for name, data in sidecar.get("ratios", {}).items()
    ]
    return sorted(ratios, key=lambda r: (r["priority"] != "HPRC", r["name"]))


def _vns_vehicles_from_sidecar(sidecar):
    vehicles = {}
    for row in sidecar.get("vehicles", []):
        demand = row["demand"]
        vehicles[demand] = {
            "demand": demand,
            "color": "Color %s" % row.get("paint_color", ""),
            "options": dict(row.get("options") or {}),
            "sequence_rank": int(row.get("sequence_rank") or 0),
        }
    return vehicles


def _vns_load_sidecar(sidecar_path):
    from django.core.management.base import CommandError

    sidecar = Path(sidecar_path)
    if not sidecar.exists():
        raise CommandError("Sidecar file not found: %s" % sidecar)
    with sidecar.open() as fh:
        return json.load(fh)


def _vns_single_pegged_demand(opplan):
    plan = opplan.plan or {}
    pegging = plan.get("pegging") or {}
    if not pegging:
        return None
    if len(pegging) == 1:
        return next(iter(pegging.keys()))
    return max(pegging.items(), key=lambda item: item[1] or 0)[0]


def _vns_load_mrp_sequence(database, paint_operation, assembly_operation, vehicles):
    from django.core.management.base import CommandError
    from ccAPPSdb.input.models import OperationPlan

    paint_by_demand = {}
    assembly_by_demand = {}

    paint_qs = (
        OperationPlan.objects.using(database)
        .filter(operation_id=paint_operation)
        .order_by("startdate", "reference")
    )
    for opplan in paint_qs:
        demand = _vns_single_pegged_demand(opplan)
        if demand and demand in vehicles:
            paint_by_demand[demand] = opplan

    assembly_qs = OperationPlan.objects.using(database).filter(
        operation_id=assembly_operation
    )
    for opplan in assembly_qs:
        demand = opplan.demand_id or _vns_single_pegged_demand(opplan)
        if demand and demand in vehicles:
            assembly_by_demand[demand] = opplan

    missing_assembly = [d for d in paint_by_demand if d not in assembly_by_demand]
    if missing_assembly:
        raise CommandError(
            "Missing assembly MO for %s painted vehicles, first: %s"
            % (len(missing_assembly), missing_assembly[0])
        )
    if not paint_by_demand:
        raise CommandError("No paint MRP results found for ROADEF vehicles")
    return paint_by_demand, assembly_by_demand


def _vns_duration(opplan):
    if opplan.startdate and opplan.enddate and opplan.enddate > opplan.startdate:
        return opplan.enddate - opplan.startdate
    return timedelta(minutes=1)


def _vns_rewrite_plan_links(opplan, upstream_ref=None, downstream_ref=None):
    plan = dict(opplan.plan or {})
    if upstream_ref is not None:
        plan["upstream_opplans"] = [[str(upstream_ref), 1.0, 0.0]]
    if downstream_ref is not None:
        plan["downstream_opplans"] = [[str(downstream_ref), 1.0, 0.0]]
    opplan.plan = plan


def _vns_updated_materials(database, opplan, start, end):
    from ccAPPSdb.input.models import OperationPlanMaterial

    result = []
    for material in OperationPlanMaterial.objects.using(database).filter(
        operationplan=opplan
    ):
        if material.quantity < 0:
            material.flowdate = start
        elif material.quantity > 0:
            material.flowdate = end
        result.append(material)
    return result


def _vns_direct_upstream_refs(opplan):
    plan = opplan.plan or {}
    refs = []
    for link in plan.get("upstream_opplans") or []:
        if not link:
            continue
        refs.append(str(link[0]))
    return refs


def _vns_update_upstream_supply(database, opplan, flowdate):
    from ccAPPSdb.input.models import OperationPlan, OperationPlanMaterial

    upstream_updates = []
    material_updates = []
    refs = _vns_direct_upstream_refs(opplan)
    if not refs:
        return upstream_updates, material_updates

    for upstream in OperationPlan.objects.using(database).filter(reference__in=refs):
        # These ROADEF supply plans are zero-duration PO records. Moving them
        # with the paint start keeps the material release constraint visible.
        if upstream.type != "PO":
            continue
        upstream.startdate = flowdate
        upstream.enddate = flowdate
        upstream_updates.append(upstream)
        for material in OperationPlanMaterial.objects.using(database).filter(
            operationplan=upstream
        ):
            material.flowdate = flowdate
            material_updates.append(material)

    return upstream_updates, material_updates


def _vns_demand_name(opplan):
    return opplan.demand_id or _vns_single_pegged_demand(opplan)


def _vns_calendar_value(calendar, moment):
    if calendar is None:
        return 1.0
    bucket = calendar.findBucket(moment)
    return float(bucket.value if bucket else calendar.defaultvalue)


def _vns_calendar_available(calendar, moment):
    return _vns_calendar_value(calendar, moment) > 0


def _vns_next_available_on_calendar(calendar, moment, horizon):
    if calendar is None or _vns_calendar_available(calendar, moment):
        return moment
    for start, end, _bucket_id, value, _prev_bucket_id, _prev_value in calendar.getEvents(
        moment, horizon
    ):
        if end <= moment or value <= 0:
            continue
        return max(start, moment)
    return None


def _vns_operation_calendars(database, paint_opplan):
    from ccAPPSdb.input.models import OperationResource

    calendars = []
    operation = paint_opplan.operation
    if operation and operation.available:
        calendars.append(operation.available)
    if operation:
        for opres in (
            OperationResource.objects.using(database)
            .filter(operation=operation)
            .select_related("resource__available")
        ):
            if opres.resource and opres.resource.available:
                calendars.append(opres.resource.available)
    return calendars


def _vns_earliest_processing_time(database, paint_opplan, start):
    calendars = _vns_operation_calendars(database, paint_opplan)
    if not calendars:
        return start

    horizon = start + timedelta(days=366)
    candidate = start
    while candidate < horizon:
        next_candidates = [
            _vns_next_available_on_calendar(calendar, candidate, horizon)
            for calendar in calendars
        ]
        if any(value is None for value in next_candidates):
            break
        next_candidate = max(next_candidates)
        if next_candidate == candidate and all(
            _vns_calendar_available(calendar, candidate) for calendar in calendars
        ):
            return candidate
        candidate = next_candidate

    return start


def _vns_schedule_anchor(database, anchor_mode, first_paint, first_paint_opplan=None):
    if anchor_mode != "earliest":
        return first_paint

    from ccAPPSdb.common.models import Parameter
    from django.utils.dateparse import parse_datetime

    current = Parameter.getValue("currentdate", database, None)
    if current is None:
        return first_paint
    if isinstance(current, datetime):
        anchor = current
    else:
        parsed = parse_datetime(str(current))
        anchor = parsed or first_paint
    if first_paint_opplan is None:
        return anchor
    return _vns_earliest_processing_time(database, first_paint_opplan, anchor)


def _vns_write_sequence(
    database,
    sequence,
    paint_by_demand,
    assembly_by_demand,
    frozen_prefix,
    anchor_mode="mrp",
):
    from django.core.management.base import CommandError
    from django.db import transaction
    from ccAPPSdb.input.models import OperationPlan, OperationPlanMaterial

    first_paint = min(
        (op.startdate for op in paint_by_demand.values() if op.startdate),
        default=None,
    )
    if not first_paint:
        raise CommandError("Paint MOs don't have start dates")

    paint_duration = _vns_duration(next(iter(paint_by_demand.values())))
    assembly_duration = _vns_duration(next(iter(assembly_by_demand.values())))

    paint_updates = []
    assembly_updates = []
    upstream_updates = []
    material_updates = []
    demand_delivery = {}

    first_paint_opplan = next(iter(paint_by_demand.values()))
    schedule_start = _vns_schedule_anchor(
        database, anchor_mode, first_paint, first_paint_opplan
    )
    frozen_prefix = min(max(0, frozen_prefix), len(sequence))
    frozen_sequence = sequence[:frozen_prefix]
    movable_sequence = sequence[frozen_prefix:]

    def schedule_vehicle(demand, paint_start, assembly_cursor):
        paint_op = paint_by_demand[demand]
        assembly_op = assembly_by_demand[demand]

        paint_end = paint_start + paint_duration
        assembly_start = max(assembly_cursor, paint_end)
        assembly_end = assembly_start + assembly_duration

        paint_op.startdate = paint_start
        paint_op.enddate = paint_end
        assembly_op.startdate = assembly_start
        assembly_op.enddate = assembly_end
        demand = _vns_demand_name(assembly_op)
        if demand:
            demand_delivery[demand] = assembly_end
        _vns_rewrite_plan_links(paint_op, downstream_ref=assembly_op.reference)
        _vns_rewrite_plan_links(assembly_op, upstream_ref=paint_op.reference)
        paint_updates.append(paint_op)
        assembly_updates.append(assembly_op)

        material_updates.extend(
            _vns_updated_materials(database, paint_op, paint_start, paint_end)
        )
        material_updates.extend(
            _vns_updated_materials(database, assembly_op, assembly_start, assembly_end)
        )
        upstream_ops, upstream_materials = _vns_update_upstream_supply(
            database, paint_op, paint_start
        )
        upstream_updates.extend(upstream_ops)
        material_updates.extend(upstream_materials)

        return paint_end, assembly_end

    if frozen_sequence:
        frozen_finish = schedule_start - timedelta(minutes=1)
        frozen_span = max(
            paint_duration * len(frozen_sequence) + assembly_duration,
            paint_duration + assembly_duration * len(frozen_sequence),
        )
        frozen_paint_start = frozen_finish - frozen_span
        frozen_assembly_cursor = frozen_paint_start
        for demand in frozen_sequence:
            frozen_paint_start, frozen_assembly_cursor = schedule_vehicle(
                demand, frozen_paint_start, frozen_assembly_cursor
            )

    paint_start = schedule_start
    assembly_cursor = schedule_start
    for demand in movable_sequence:
        paint_start, assembly_cursor = schedule_vehicle(
            demand, paint_start, assembly_cursor
        )

    with transaction.atomic(using=database):
        OperationPlan.objects.using(database).bulk_update(
            paint_updates + assembly_updates,
            ["startdate", "enddate", "plan"],
        )
        if upstream_updates:
            OperationPlan.objects.using(database).bulk_update(
                upstream_updates,
                ["startdate", "enddate"],
            )
        if material_updates:
            OperationPlanMaterial.objects.using(database).bulk_update(
                material_updates,
                ["flowdate"],
            )
        if demand_delivery:
            from ccAPPSdb.input.models import Demand

            demand_updates = []
            for demand in Demand.objects.using(database).filter(
                name__in=demand_delivery.keys()
            ):
                demand.deliverydate = demand_delivery[demand.name]
                demand.delay = (
                    demand.deliverydate - demand.due if demand.due else None
                )
                demand.plannedquantity = demand.quantity
                demand_updates.append(demand)
            if demand_updates:
                Demand.objects.using(database).bulk_update(
                    demand_updates,
                    ["deliverydate", "delay", "plannedquantity"],
                )

    return {
        "paint": len(paint_updates),
        "assembly": len(assembly_updates),
        "upstream": len(upstream_updates),
        "demands": len(demand_delivery),
        "source_start": first_paint,
        "start": schedule_start,
        "end": assembly_cursor,
        "anchor": anchor_mode,
    }


def _vns_load_runtime_config(
    database,
    sidecar_path=None,
    paint_operation=None,
    assembly_operation=None,
    time_limit=None,
    samples=None,
    seed=None,
    frozen_prefix=None,
):
    from ccAPPSdb.common.models import Parameter

    return {
        "sidecar_path": sidecar_path
        or Parameter.getValue(
            "vns.sidecar",
            database,
            "ccAPPSdb/input/fixtures/roadef_renault_brazil_sidecar.json",
        ),
        "paint_operation": paint_operation
        or Parameter.getValue("vns.paint_operation", database, "Paint vehicle"),
        "assembly_operation": assembly_operation
        or Parameter.getValue(
            "vns.assembly_operation", database, "Assemble vehicle"
        ),
        "time_limit": float(
            time_limit
            if time_limit is not None
            else Parameter.getValue("vns.time_limit", database, "30")
        ),
        "samples": int(
            samples
            if samples is not None
            else Parameter.getValue("vns.samples", database, "250")
        ),
        "seed": int(
            seed
            if seed is not None
            else Parameter.getValue("vns.random_seed", database, "7")
        ),
        "frozen_prefix": int(
            frozen_prefix
            if frozen_prefix is not None
            else Parameter.getValue("vns.frozen_prefix", database, "14")
        ),
        "anchor": Parameter.getValue("vns.anchor", database, "mrp").lower(),
    }


def _vns_build_payload(sidecar_data, initial_sequence, config):
    vehicles = _vns_vehicles_from_sidecar(sidecar_data)
    return {
        "vehicles": list(vehicles.values()),
        "ratios": _vns_ratios_from_sidecar(sidecar_data),
        "sequence": list(initial_sequence),
        "config": {
            "time_limit_seconds": float(config["time_limit"]),
            "samples_per_round": max(1, int(config["samples"])),
            "paint_batch_limit": int(sidecar_data.get("paint_batch_limit") or 0),
            "frozen_prefix": max(0, int(config["frozen_prefix"])),
            "random_seed": int(config["seed"]),
        },
    }, vehicles


def run_vns(
    database=None,
    dry_run=False,
    sidecar_path=None,
    paint_operation=None,
    assembly_operation=None,
    time_limit=None,
    samples=None,
    seed=None,
    frozen_prefix=None,
):
    from django.core.management.base import CommandError
    from django.db import DEFAULT_DB_ALIAS
    from ccAPPSdb.common.utils import get_databases

    database = database or DEFAULT_DB_ALIAS
    if database not in get_databases():
        raise CommandError("No database settings known for '%s'" % database)

    config = _vns_load_runtime_config(
        database=database,
        sidecar_path=sidecar_path,
        paint_operation=paint_operation,
        assembly_operation=assembly_operation,
        time_limit=time_limit,
        samples=samples,
        seed=seed,
        frozen_prefix=frozen_prefix,
    )
    sidecar_data = _vns_load_sidecar(config["sidecar_path"])

    paint_by_demand, assembly_by_demand = _vns_load_mrp_sequence(
        database,
        config["paint_operation"],
        config["assembly_operation"],
        _vns_vehicles_from_sidecar(sidecar_data),
    )
    initial_sequence = list(paint_by_demand.keys())
    if len(initial_sequence) < 2:
        raise CommandError("MRP paint sequence has fewer than 2 vehicles")

    payload, _ = _vns_build_payload(sidecar_data, initial_sequence, config)
    solver = solverVNS()
    result = json.loads(solver.optimizeJSON(json.dumps(payload)))

    summary = None
    if not dry_run:
        summary = _vns_write_sequence(
            database=database,
            sequence=result["sequence"],
            paint_by_demand=paint_by_demand,
            assembly_by_demand=assembly_by_demand,
            frozen_prefix=max(0, int(config["frozen_prefix"])),
            anchor_mode=config["anchor"],
        )

    return {
        "initial_score": result["initialScore"],
        "optimized_score": result["bestScore"],
        "sequence": result["sequence"],
        "summary": summary,
        "engine": "ccAPPS.solverVNS",
        "frozen_prefix": max(0, int(config["frozen_prefix"])),
    }


setattr(_cpp_module, "run_vns", run_vns)
globals()["run_vns"] = run_vns
if "run_vns" not in __all__:
    __all__.append("run_vns")

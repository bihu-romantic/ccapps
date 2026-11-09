from datetime import datetime, timedelta
import os
import logging
import re

from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, ProgrammingError, OperationalError, connections
from django.db.models import DecimalField, F, Q, Value
from django.db.models.functions import Coalesce
from django.utils.translation import gettext_lazy as _
from django.utils import timezone

from ccAPPSdb.common.commands import PlanTaskRegistry, PlanTask
from ccAPPSdb.common.models import Parameter, Bucket

logger = logging.getLogger(__name__)


@PlanTaskRegistry.register
class CheckBuckets(PlanTask):
    description = "Generation of time buckets"
    sequence = 3

    @classmethod
    def getWeight(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        return -1 if Bucket.objects.all().using(database).exists() else 0.1

    @classmethod
    def run(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        from django.core import management

        management.call_command("createbuckets", database=database, task=-1)


@PlanTaskRegistry.register
class UpdateLastCurrentDate(PlanTask):
    description = "Updates last_currentdate parameter"
    sequence = 450

    @classmethod
    def getWeight(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        return -1 if "loadplan" in os.environ or "noexport" in os.environ else 0.1

    @classmethod
    def run(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        import ccAPPS

        try:
            p, created = Parameter.objects.using(database).get_or_create(
                pk="last_currentdate"
            )
            newval = ccAPPS.settings.current.strftime("%Y-%m-%d %H:%M:%S")
            if p.value == newval:
                return
            p.value = newval
            if created:
                p.description = "This parameter is automatically populated. It stores the date of the last plan execution"
            p.save(using=database)
        except Exception:
            logger.warning("Failed to set last_currentdate parameter")


@PlanTaskRegistry.register
class MakePlanFeasible(PlanTask):
    description = "Initial plan problems"
    sequence = 199

    @classmethod
    def getWeight(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        if "supply" in os.environ or (
            "nowebservice" not in os.environ
            and Parameter.getValue("plan.webservice", database, "true").lower()
            == "true"
        ):
            return 1
        else:
            return -1

    @classmethod
    def run(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        import ccAPPS

        # Determine log level
        loglevel = int(Parameter.getValue("plan.loglevel", database, 0))

        # Clear previous info messages
        if "supply" in os.environ:
            for oper in ccAPPS.operations():
                for opplan in oper.operationplans:
                    if opplan.info:
                        opplan.info = ""

        # Propagate the operationplan status
        logger.info("Propagating work-in-progress status information")
        ccAPPS.solver_propagateStatus(loglevel=loglevel).solve()

        # Update the feasibility flag of all operationplans
        for oper in ccAPPS.operations():
            for opplan in oper.operationplans:
                opplan.updateFeasible()
                if loglevel > 0 and opplan.info:
                    print(f"Info on {opplan}:")
                    for line in opplan.info.splitlines():
                        print(f"      {line.strip()}")

        # Report the result
        print("Initial problems:")
        probs = {}
        for i in ccAPPS.problems():
            if i.name in probs:
                probs[i.name] += 1
            else:
                probs[i.name] = 1
        for i in sorted(probs.keys()):
            print("     %s: %s" % (i, probs[i]))



def follow_changed_demand_quantity(env, plans):
    """Shrink proposed supply pegged to a changed order.

    A quantity decrease is not a new-supply expansion. The loaded
    manufacturing order otherwise keeps the old quantity, so a time-per
    order also keeps its old duration. Scale the top-level plan; the
    engine copies that quantity onto routing steps and recomputes the end.
    Increases stay with supply expansion.
    """

    def split(key):
        return {
            part.strip()
            for part in str(env.get(key) or "").replace(";", ",").split(",")
            if part.strip()
        }

    changed = split("LOCAL_NEW_DEMANDS") & split("LOCAL_DELETED_DEMANDS")
    if not changed:
        return

    def top_plan(plan):
        while getattr(plan, "owner", None) is not None:
            plan = plan.owner
        return plan

    grouped = {}
    demands = {}
    for plan in plans:
        demand = getattr(plan, "demand", None)
        if demand is None or getattr(demand, "name", None) not in changed:
            continue
        root = top_plan(plan)
        grouped.setdefault(demand.name, {})[id(root)] = root
        demands[demand.name] = demand
    for name, roots in grouped.items():
        demand = demands.get(name)
        if demand is None:
            continue
        target = float(getattr(demand, "quantity", 0) or 0)
        movable = []
        fixed = 0.0
        for plan in roots.values():
            status = str(getattr(plan, "status", "") or "").lower()
            qty = float(getattr(plan, "quantity", 0) or 0)
            if status in ("proposed", "approved"):
                movable.append(plan)
            else:
                fixed += qty
        movable_qty = sum(float(getattr(plan, "quantity", 0) or 0) for plan in movable)
        desired = max(0.0, target - fixed)
        if not movable or movable_qty <= desired + 1e-4:
            continue
        scale = 0.0 if movable_qty <= 1e-9 else desired / movable_qty
        consumed = 0.0
        for index, plan in enumerate(movable):
            old = float(getattr(plan, "quantity", 0) or 0)
            new = max(0.0, desired - consumed) if index == len(movable) - 1 else old * scale
            plan.quantity = new
            consumed += float(getattr(plan, "quantity", 0) or 0)
            logger.info(
                "Local quantity follow: demand=%s ref=%s %s -> %s start=%s end=%s",
                name,
                getattr(plan, "reference", None),
                old,
                plan.quantity,
                plan.start,
                plan.end,
            )


def should_revert_fas_inputs(solver_mode, failed):
    """Master-data rollback is part of the FAS reschedule contract only."""
    return solver_mode == "fas" and bool(failed)


def finish_local_solve(solver_mode, database, solve):
    """Run one local solve and put FAS master data back if it fails.

    The failure may be a false outcome or an exception from the solver.
    MRP, CP, and GA do not use this snapshot.
    """
    try:
        return solve()
    except Exception:
        if should_revert_fas_inputs(solver_mode, True):
            try:
                from ccAPPSdb.execute.views import revert_reschedule_inputs

                if revert_reschedule_inputs(database):
                    logger.info(
                        "Reverted demand, on-hand and capacity "
                        "after FAS reschedule failed"
                    )
            except Exception:
                logger.exception("Failed to revert reschedule inputs")
        raise


@PlanTaskRegistry.register
class SupplyPlanning(PlanTask):
    description = "生成供应计划"
    sequence = 200
    label = (
        "supply",
        _("生成供应计划"),
        _(
            "计算有限物料和有限产能约束下的采购、库存和资源计划。"
        ),
    )

    @classmethod
    def getWeight(cls, **kwargs):
        if "supply" in os.environ:
            return 1
        else:
            return -1

    # Auxiliary functions for debugging
    # Rename to activate
    @classmethod
    def DISABLED_debugResource(cls, res, mode):
        # if res.name != 'my favorite resource': return
        print("=> Situation on resource %s" % res.name)
        for j in res.loadplans:
            print(
                "=>  %s %s %s %s %s %s %s"
                % (
                    j.quantity,
                    j.onhand,
                    j.startdate,
                    j.enddate,
                    j.operation.name,
                    j.operationplan.quantity,
                    j.setup,
                )
            )

    # Auxiliary functions for debugging
    # Rename to activate
    @classmethod
    def DISABLED_debugDemand(cls, dem, mode):
        if dem.name == "my favorite demand":
            print("=> Starting to plan demand %s" % dem.name)
            cls.solver.loglevel = 2
        else:
            cls.solver.loglevel = 0

    # Auxiliary functions for debugging
    # Rename to activate
    @classmethod
    def DISABLED_nextDemand(cls, cluster):
        # Build the list of demands to plan when accessed for the first time
        if not hasattr(cls, "demandlist"):
            import ccAPPS

            cls.demandlist = {}
            for d in ccAPPS.demands():
                if d.quantity > 0 and d.status in ("open", "quote"):
                    if d.cluster in cls.demandlist:
                        cls.demandlist[d.cluster].append(d)
                    else:
                        cls.demandlist[d.cluster] = [d]
            for cl in cls.demandlist:
                cls.demandlist[cl].sort(
                    key=lambda d: (d.priority, d.due, d.quantity, d.name)
                )
                for d in cls.demandlist[cl]:
                    print("sorted", cl, d.priority, d.due, d.quantity, d.name)

        # Pop a demand from the list on every call
        print("asked next demand for", cls.demandlist[cluster])
        if cls.demandlist.get(cluster, None):
            return cls.demandlist[cluster].pop(0)
        else:
            return None

    # Auxiliary functions for debugging
    # Rename to activate
    @classmethod
    def DISABLED_debugOperation(cls, oper, mode):
        return

    @classmethod
    def run(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        import ccAPPS

        # Determine log level
        loglevel = int(Parameter.getValue("plan.loglevel", database, 0))

        # Create a solver where the plan type is defined by an environment variable.
        try:
            plantype = int(os.environ["CCAPPS_PLANTYPE"])
        except Exception:
            plantype = 1  # Default is a constrained plan
        try:
            from ccAPPSdb.execute.management.commands.runplan import parseConstraints

            constraint = parseConstraints(os.environ["CCAPPS_CONSTRAINT"])
        except Exception:
            constraint = 4 + 16 + 32  # Default is with all constraints enabled
        solver_mode = Parameter.getValue("plan.solver", database, "mrp").lower().strip()
        # CP and GA are second-stage schedulers. Their first stage creates the
        # complete supply/manufacturing plan; the optimizer then sequences it.
        #   plan.solver=cp + cp.baseline=fas|mrp
        #   plan.solver=ga + ga.baseline=fas|mrp
        #   plan.solver=fas|mrp          (first stage only)
        cp_baseline = Parameter.getValue(
            "cp.baseline", database, os.environ.get("CP_BASELINE", "fas")
        ).lower().strip()
        if cp_baseline not in ("mrp", "fas"):
            logger.warning(
                "Unsupported cp.baseline '%s'; falling back to fas", cp_baseline
            )
            cp_baseline = "fas"
        ga_baseline = Parameter.getValue(
            "ga.baseline", database, os.environ.get("GA_BASELINE", "mrp")
        ).lower().strip()
        if ga_baseline not in ("mrp", "fas"):
            logger.warning(
                "Unsupported ga.baseline '%s'; falling back to mrp", ga_baseline
            )
            ga_baseline = "mrp"
        run_cp_after_supply = False
        run_ga_after_supply = False
        if solver_mode == "cp":
            supply_solver = cp_baseline
            run_cp_after_supply = True
        elif solver_mode == "fas":
            supply_solver = "fas"
        elif solver_mode == "ga":
            supply_solver = ga_baseline
            run_ga_after_supply = True
        else:
            supply_solver = "mrp"
            if solver_mode != "mrp":
                # Unknown labels fall through to the validator below.
                pass
        is_fas = supply_solver == "fas"
        policy_json = Parameter.getValue("fc_aps.batch_policy", database, "")
        policy = {}
        if policy_json:
            import json

            policy = json.loads(policy_json)
            use_economic_batch = bool(
                policy.get("objective_ordering", {}).get("use_economic_batch", False)
            )
            for op in ccAPPS.operations():
                rule = policy["operations"].get(op.name)
                if rule:
                    op.fc_batch_policy = True
                    # Economic lots are opt-in: when disabled they stay
                    # reference-only and sizing falls back to SNP / exact.
                    if use_economic_batch:
                        try:
                            op.fc_economic_batch = float(rule.get("economic") or 0)
                        except (TypeError, ValueError):
                            op.fc_economic_batch = 0.0
                    else:
                        op.fc_economic_batch = 0.0
                    op.fc_snp = float(rule["snp"])
                    op.fc_exact_quantity = bool(rule["exact"])
                    op.fc_allow_coproduct_excess = bool(rule.get("coproduct", False))
                    op.size_multiple = 0
                    # Older imported datasets stored the economic batch as a
                    # hard maximum.  Remove that limit in memory as well.
                    op.size_maximum = 1.0e30
                    if rule["exact"]:
                        op.size_minimum = 0
            for buf in ccAPPS.buffers():
                if buf.item.name in policy["limits"]:
                    buf.fc_production_limit = float(policy["limits"][buf.item.name])
            logger.info(
                "Applied FC SNP/lifecycle policy to %s operations "
                "(%s)",
                len(policy["operations"]),
                (
                    "economic batch enabled"
                    if use_economic_batch
                    else "economic batch is reference only"
                ),
            )
        else:
            use_economic_batch = False
        # Apply a scenario's configured horizon from UI and CLI runs alike.
        # A one-run environment override still takes precedence. Database
        # demand due dates remain unchanged.
        from ccAPPSdb.execute.planning_window import (
            demand_category_tier,
            estimate_processing_seconds,
            exact_piece_shipment,
            exact_processing_over_hours,
            item_matches_split_scope,
            longest_processing_profile,
            load_items_for_resources,
            planning_window_end,
            split_is_worthwhile,
            unit_split_shipment,
        )

        window_end = planning_window_end(
            ccAPPS.settings.current,
            explicit_end=os.environ.get("supply_plan_end"),
            configured_end=Parameter.getValue("plan.horizon_end", database, None),
        )
        # Complete-first applies only inside this nearer priority window.
        # Later orders may be SNP-split to fill leftover capacity. Defaults to
        # the first three days when unset, and never exceeds the plan horizon.
        priority_window_end = planning_window_end(
            ccAPPS.settings.current,
            explicit_end=os.environ.get("fc_aps_priority_window_end"),
            configured_end=Parameter.getValue(
                "fc_aps.priority_window_end", database, None
            ),
        )
        if priority_window_end is None and window_end is not None:
            from datetime import timedelta

            priority_window_end = ccAPPS.settings.current + timedelta(days=3)
        if (
            priority_window_end is not None
            and window_end is not None
            and priority_window_end > window_end
        ):
            priority_window_end = window_end
        rolling_enabled = False
        original_demand_due = {}
        original_demand_policy = {}
        fill_pulled = 0
        item_policies = policy.get("items", {}) if policy_json else {}
        # Only a finite planning window ranks or preserves demand priority.
        # FAS and an ordinary MRP run have no window, so this stays off.
        preserve_demand_priority = False
        # plan.lot_split / PLAN_LOT_SPLIT: shared by FAS and MRP supply.
        # Legacy aliases: fas.lot_split / FAS_LOT_SPLIT.
        # Gates exact>8h, eco>2n, C++ eco/SNP sizing, partial-shipment loops.
        # Window SNP fillers still need a planning window (below).
        def _lot_split_on():
            raw = os.environ.get("PLAN_LOT_SPLIT")
            if raw is None or str(raw).strip() == "":
                raw = os.environ.get("FAS_LOT_SPLIT")
            if raw is None or str(raw).strip() == "":
                raw = Parameter.getValue(
                    "plan.lot_split",
                    database,
                    Parameter.getValue("fas.lot_split", database, None),
                )
            if raw is None or str(raw).strip() == "":
                return False
            return str(raw).strip().lower() in ("1", "true", "yes", "on")

        lot_split = _lot_split_on()
        objective_rule = policy.get("objective_ordering", {}) if policy else {}
        try:
            exact_split_over_hours = float(
                objective_rule.get("exact_split_over_hours", 8) or 8
            )
        except (TypeError, ValueError):
            exact_split_over_hours = 8.0

        def _seconds(value):
            if value is None:
                return 0.0
            if hasattr(value, "total_seconds"):
                return float(value.total_seconds())
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0.0

        all_operations = list(ccAPPS.operations())
        item_duration = {}
        operation_item = {}
        for op in all_operations:
            item = getattr(op, "item", None)
            owner = getattr(op, "owner", None)
            seen = set()
            while (
                (item is None or not getattr(item, "name", None))
                and owner is not None
                and id(owner) not in seen
            ):
                seen.add(id(owner))
                item = getattr(owner, "item", None)
                owner = getattr(owner, "owner", None)
            if item is None or not getattr(item, "name", None):
                continue
            operation_item[op] = item.name
            fixed = _seconds(getattr(op, "duration", None))
            per = _seconds(getattr(op, "duration_per", None))
            if fixed > 0 or per > 0:
                item_duration.setdefault(item.name, []).append((fixed, per))
        open_demands = [
            demand
            for demand in ccAPPS.demands()
            if demand.status in ("open", "quote")
        ]
        economic_split_items = set()
        if lot_split:
            for demand in open_demands:
                rule = item_policies.get(demand.item.name, {})
                try:
                    economic = float(rule.get("economic", 0) or 0)
                except (TypeError, ValueError):
                    economic = 0.0
                remaining = max(0.0, demand.quantity - demand.planned_quantity)
                if economic > 1.0 and split_is_worthwhile(
                    remaining, snp=rule.get("snp", 1), economic=economic
                ):
                    economic_split_items.add(demand.item.name)
        for op in all_operations:
            if not getattr(op, "fc_batch_policy", False):
                continue
            op.fc_lot_split = bool(lot_split)
            # Legacy alias kept for older engine builds.
            op.fc_fas_lot_split = bool(lot_split)
            item_name = operation_item.get(op)
            if lot_split and item_name in economic_split_items:
                try:
                    eco = float(
                        item_policies.get(item_name, {}).get("economic", 0) or 0
                    )
                except (TypeError, ValueError):
                    eco = 0.0
                if eco <= 1.0:
                    eco = float(getattr(op, "fc_economic_batch", 0.0) or 0)
                op.fc_lot_economic_batch = eco
                op.fc_fas_economic_batch = eco
            else:
                op.fc_lot_economic_batch = 0.0
                op.fc_fas_economic_batch = 0.0
        for demand in open_demands:
            original_demand_due[demand.name] = demand.due
            # Off: forbid partial-shipment remainder loops after a capacity cut.
            if not lot_split:
                original_demand_policy[demand.name] = getattr(
                    demand, "policy", None
                )
                demand.policy = "alltogether"

        def _assign_lot_split_minshipment(demand, *, allow_window_snp=False):
            """Shared FAS/MRP shipment sizing under plan.lot_split."""
            item_name = demand.item.name
            item_rule = item_policies.get(item_name, {})
            remaining = max(0.0, demand.quantity - demand.planned_quantity)
            try:
                snp_size = float(item_rule.get("snp", 1) or 1)
            except (TypeError, ValueError):
                snp_size = 1.0
            if snp_size < 1:
                snp_size = 1.0
            try:
                economic_size = float(item_rule.get("economic", 0) or 0)
            except (TypeError, ValueError):
                economic_size = 0.0
            has_economic = economic_size > 1.0
            if not lot_split:
                demand.minshipment = remaining
                return "whole"
            if item_rule.get("exact", False):
                fixed, per = longest_processing_profile(
                    remaining, item_duration.get(item_name, ())
                )
                proc = estimate_processing_seconds(
                    remaining, duration=fixed, duration_per=per
                )
                if exact_processing_over_hours(proc, exact_split_over_hours):
                    demand.minshipment = exact_piece_shipment(
                        remaining,
                        duration=fixed,
                        duration_per=per,
                        threshold_hours=exact_split_over_hours,
                    )
                    return "exact_long"
                demand.minshipment = remaining
                return "exact_whole"
            if has_economic:
                if split_is_worthwhile(
                    remaining, snp=snp_size, economic=economic_size
                ):
                    demand.minshipment = unit_split_shipment(
                        remaining, snp=snp_size, economic=economic_size
                    )
                    return "economic_split"
                demand.minshipment = remaining
                return "economic_whole"
            if allow_window_snp and split_is_worthwhile(
                remaining, snp=snp_size, economic=0.0
            ):
                demand.minshipment = snp_size
                return "window_snp_split"
            demand.minshipment = remaining
            return "complete"

        if not window_end and lot_split:
            assign_kind_counts = {}
            for demand in open_demands:
                kind = _assign_lot_split_minshipment(
                    demand, allow_window_snp=False
                )
                assign_kind_counts[kind] = assign_kind_counts.get(kind, 0) + 1
            logger.info(
                "plan.lot_split on for %s (no plan window): exact>8h / eco>2n "
                "minshipment kinds=%s",
                supply_solver.upper(),
                assign_kind_counts,
            )
        elif not window_end:
            logger.info(
                "plan.lot_split off for %s (whole-order / alltogether)",
                supply_solver.upper(),
            )

        if window_end:
            # Generic partial-shipment granularity. Normal materials search
            # and repeat in SNP-sized lots (invalid SNP defaults to one).
            # Spare/discontinued demands use their exact remaining demand.
            complete_first = objective_rule.get("enabled", False) and objective_rule.get(
                "complete_orders_first", False
            )
            pull_fillers_into_window = bool(
                objective_rule.get("pull_fillers_into_window", True)
            )
            # Economic batch sizes on ops remain available for CP diagnostics
            # when the policy loads them; truncation follows plan.lot_split.
            use_economic_batch = bool(
                objective_rule.get("use_economic_batch", use_economic_batch)
            )
            split_prefixes = [
                str(p)
                for p in objective_rule.get("window_split_item_prefixes", ["01.52"])
                if p
            ]
            split_resources = [
                str(r)
                for r in objective_rule.get(
                    "window_split_resources",
                    ["PERSON_制造部2工段", "2工段装配线"],
                )
                if r
            ]
            split_scope_items = load_items_for_resources(database, split_resources)
            first_pass_partial_search = 0
            window_split_armed = 0
            filler_skipped_scope = 0
            stock_fillers = 0
            assign_kind_counts = {}
            # Rank: 预警 > 未入库 > 备库, then near-term before later within
            # each class. 备库 is an idle-capacity filler only.
            if complete_first and priority_window_end is not None:
                ordered = sorted(
                    open_demands,
                    key=lambda d: (
                        demand_category_tier(
                            d.name, getattr(d, "category", None)
                        ),
                        int(getattr(d, "priority", 0) or 0),
                        0
                        if original_demand_due[d.name] <= priority_window_end
                        else 1,
                        original_demand_due[d.name],
                        d.name,
                    ),
                )
                for rank, demand in enumerate(ordered, 1):
                    demand.priority = rank
            for demand in open_demands:
                original_due = original_demand_due[demand.name]
                item_name = demand.item.name
                item_rule = item_policies.get(item_name, {})
                tier = demand_category_tier(
                    demand.name, getattr(demand, "category", None)
                )
                is_stock = tier == 2
                due_in_priority = (
                    priority_window_end is not None
                    and original_due <= priority_window_end
                    and not is_stock
                )
                in_split_scope = item_matches_split_scope(
                    item_name,
                    item_codes=split_scope_items,
                    prefixes=split_prefixes,
                )
                # Shared FAS/MRP minshipment under plan.lot_split:
                # exact>8h / eco>2n / window SNP (in-scope).
                allow_window_snp = bool(
                    lot_split
                    and in_split_scope
                    and (
                        due_in_priority
                        or (complete_first and not due_in_priority)
                    )
                )

                # 备库: only after 预警/未入库, soaking idle slots in the
                # priority window. Its database due is restored after solving.
                if is_stock and priority_window_end is not None:
                    demand.due = priority_window_end
                    demand.maxlateness = 0
                    kind = _assign_lot_split_minshipment(
                        demand, allow_window_snp=allow_window_snp
                    )
                    if kind == "window_snp_split" and due_in_priority:
                        window_split_armed += 1
                    assign_kind_counts[kind] = assign_kind_counts.get(kind, 0) + 1
                    first_pass_partial_search += 1
                    stock_fillers += 1
                    continue

                demand.due = min(original_due, window_end)
                demand.maxlateness = max(0, (window_end - demand.due).total_seconds())
                kind = _assign_lot_split_minshipment(
                    demand, allow_window_snp=allow_window_snp
                )
                if kind == "window_snp_split" and due_in_priority:
                    window_split_armed += 1
                assign_kind_counts[kind] = assign_kind_counts.get(kind, 0) + 1
                first_pass_partial_search += 1
                # Exact: do not pull as window fillers.
                if item_rule.get("exact", False):
                    continue
                if (
                    complete_first
                    and pull_fillers_into_window
                    and not due_in_priority
                    and priority_window_end is not None
                ):
                    # Keep bottleneck-workshop fillers on their own due so
                    # they do not crowd the priority window.
                    if in_split_scope:
                        filler_skipped_scope += 1
                    else:
                        demand.due = priority_window_end
                        # The priority window is only where these orders are
                        # tried first. A zero allowance dropped whatever did
                        # not fit. The remainder may finish any time up to
                        # the planning horizon.
                        demand.maxlateness = max(
                            0,
                            (
                                window_end - priority_window_end
                            ).total_seconds(),
                        )
                        fill_pulled += 1
            logger.info("Supply planning window ends at %s", window_end)
            if assign_kind_counts:
                logger.info(
                    "FAS minshipment assign kinds: %s",
                    ", ".join(
                        "%s=%s" % (k, assign_kind_counts[k])
                        for k in sorted(assign_kind_counts)
                    ),
                )
            if priority_window_end is not None:
                logger.info(
                    "Complete-order priority window ends at %s; later "
                    "bottleneck-scope orders may split%s%s%s",
                    priority_window_end,
                    (
                        f" ({fill_pulled} pulled into window as fillers"
                        + (
                            f", {filler_skipped_scope} bottleneck fillers kept later"
                            if filler_skipped_scope
                            else ""
                        )
                        + (
                            f", {stock_fillers} 备库 idle fillers"
                            if stock_fillers
                            else ""
                        )
                        + ")"
                        if fill_pulled or filler_skipped_scope or stock_fillers
                        else " (fillers not pulled into window)"
                    ),
                    (
                        "; plan.lot_split=on for %s (exact>8h / eco>2n / window SNP; "
                        "near-term SNP floor %s demands, %s items / prefixes %s)"
                        % (
                            supply_solver.upper(),
                            window_split_armed,
                            len(split_scope_items),
                            ",".join(split_prefixes) or "-",
                        )
                        if lot_split
                        else "; plan.lot_split=off (whole-order)"
                    ),
                    "; demand rank 预警>未入库>备库",
                )
            logger.info(
                "Loaded generic SNP partial-shipment policy for %s materials "
                "(%s due-date searches)",
                len(item_policies),
                first_pass_partial_search,
            )
            rolling_enabled = bool(policy.get("rolling", {}).get("enabled", False))
            preserve_demand_priority = objective_rule.get(
                "preserve_demand_priority", False
            )
            if (
                objective_rule.get("enabled", False)
                and not rolling_enabled
                and not preserve_demand_priority
            ):
                try:
                    due_bucket_seconds = max(
                        3600.0,
                        float(objective_rule.get("due_bucket_hours", 24)) * 3600.0,
                    )
                except (TypeError, ValueError):
                    due_bucket_seconds = 86400.0
                objective_metadata = policy.get("rolling", {}).get("items", {})
                ranked_demands = []
                for demand in ccAPPS.demands():
                    if demand.status not in ("open", "quote") or demand.quantity <= 0:
                        continue
                    labor = objective_metadata.get(demand.item.name, {}).get(
                        "labor_hours_per_unit", {}
                    )
                    total_hours = sum(
                        max(0.0, float(value or 0))
                        for value in labor.values()
                    ) * float(demand.quantity)
                    due = original_demand_due.get(demand.name, demand.due)
                    due_bucket = int(
                        max(
                            0.0,
                            (due - ccAPPS.settings.current).total_seconds(),
                        )
                        // due_bucket_seconds
                    )
                    ranked_demands.append(
                        (
                            due_bucket,
                            total_hours,
                            due,
                            demand.name,
                            demand,
                        )
                    )
                ranked_demands.sort(key=lambda row: row[:-1])
                for rank, row in enumerate(ranked_demands, 1):
                    row[-1].priority = rank
                logger.info(
                    "Ranked %s demands by %sh delivery buckets, then total "
                    "labor hours; complete orders are attempted first",
                    len(ranked_demands),
                    due_bucket_seconds / 3600.0,
                )
            elif preserve_demand_priority:
                logger.info(
                    "Preserving imported demand priority; complete orders "
                    "are attempted first"
                )
        if solver_mode not in ("mrp", "fas", "ga", "cp"):
            logger.warning(
                "Unsupported plan.solver '%s'; falling back to mrp", solver_mode
            )
            solver_mode = "mrp"
            supply_solver = "mrp"
            is_fas = False
            run_cp_after_supply = False
            run_ga_after_supply = False
        # Local repair on an existing schedule: shared scope (triggers +
        # hot/freeze) then the chosen solver repairs only the movable set.
        # Params: local.* (canonical) or legacy cp.local_*.
        def _local_param(name, legacy, default="0"):
            return Parameter.getValue(
                name,
                database,
                Parameter.getValue(legacy, database, default),
            )

        local_enabled = (
            _local_param("local.reschedule", "cp.local_reschedule", "0").lower()
            in ("1", "true", "yes", "on")
            or os.environ.get("LOCAL_RESCHEDULE", "").lower()
            in ("1", "true", "yes", "on")
            or os.environ.get("CP_LOCAL_RESCHEDULE", "").lower()
            in ("1", "true", "yes", "on")
        )
        local_skip_supply = local_enabled and (
            _local_param("plan.skip_supply", "cp.local_skip_supply", "1").lower()
            in ("1", "true", "yes", "on")
            or os.environ.get("LOCAL_SKIP_SUPPLY", "").lower()
            in ("1", "true", "yes", "on")
            or os.environ.get("CP_LOCAL_SKIP_SUPPLY", "").lower()
            in ("1", "true", "yes", "on")
        )
        local_solver = solver_mode if local_enabled else ""
        if local_skip_supply:
            logger.info(
                "Local skip-supply: keeping loaded plan; shared scope + "
                "solver=%s (no FAS/MRP rebuild)",
                local_solver or solver_mode,
            )
            if solver_mode in ("cp", "ga"):
                logger.info(
                    "Local %s reschedule uses the existing plan directly; "
                    "%s.baseline=%s is ignored for supply planning",
                    solver_mode.upper(),
                    solver_mode,
                    cp_baseline if solver_mode == "cp" else ga_baseline,
                )
            # CP has its dedicated local pass; GA/FAS/MRP use the shared local
            # repair block below without rebuilding their supply baseline.
            run_cp_after_supply = solver_mode == "cp"
            run_ga_after_supply = False
        mrp_algorithm = (
            Parameter.getValue("plan.algorithm", database, "heuristic")
            .lower()
            .strip()
        )
        if mrp_algorithm not in ("heuristic", "heuristic_2"):
            logger.warning(
                "Unsupported plan.algorithm '%s'; falling back to heuristic",
                mrp_algorithm,
            )
            mrp_algorithm = "heuristic"
        solver_kwargs = dict(
            constraints=constraint,
            plantype=plantype,
            loglevel=loglevel,
            lazydelay=int(Parameter.getValue("lazydelay", database, "86400")),
            minimumdelay=int(Parameter.getValue("plan.minimumdelay", database, "3600")),
            rotateresources=(
                Parameter.getValue("plan.rotateResources", database, "true").lower()
                == "true"
            ),
            iterationmax=int(Parameter.getValue("plan.iterationmax", database, "0")),
            resourceiterationmax=int(
                Parameter.getValue("plan.resourceiterationmax", database, "500")
            ),
            administrativeleadtime=86400
            * float(Parameter.getValue("plan.administrativeLeadtime", database, "0")),
            autofence=86400
            * float(Parameter.getValue("plan.autoFenceOperations", database, "0")),
        )
        if not is_fas:
            # MRP heuristic variant: heuristic (default) or heuristic_2
            # (backward sweep + forward repair). Ignored by FAS/CP/GA engines.
            solver_kwargs["algorithm"] = mrp_algorithm
        cls.solver = (
            ccAPPS.solverFAS(**solver_kwargs)
            if is_fas
            else ccAPPS.solver_mrp(**solver_kwargs)
        )
        if local_skip_supply and solver_mode != "mrp":
            # CP/FAS/GA: keep loaded plans; their engines move the hot set.
            # MRP keeps its built-in erase-proposed-then-replan (see below).
            cls.solver.erasepreviousfirst = False
        if is_fas:
            os.environ["FAS_JOB_ORDER"] = Parameter.getValue(
                "plan.fas_job_order", database, "lpt"
            ).lower()
            os.environ["FAS_IMPROVE_SECONDS"] = str(
                Parameter.getValue("plan.fas_improve_seconds", database, "10")
            )
            # Cap forward placement at the reporting horizon when set.
            if window_end:
                os.environ["FAS_HORIZON_END"] = window_end.isoformat()
            else:
                os.environ.pop("FAS_HORIZON_END", None)
            if local_skip_supply:
                # The FAS object is only an unused compatibility holder on the
                # skip-supply path.  The loaded operationplans are the input to
                # the selected local solver; don't describe them as a newly
                # generated FAS baseline.
                pass
            elif run_cp_after_supply:
                logger.info(
                    "FAS baseline for CP: expanding demand supply and scheduling forward"
                )
            elif run_ga_after_supply:
                logger.info(
                    "FAS baseline for GA: expanding demand supply and scheduling forward"
                )
            else:
                logger.info(
                    "Standalone FAS: expanding demand supply and scheduling forward"
                )
        if solver_mode == "ga" and local_skip_supply:
            logger.info(
                "GA reschedule: preserving operationplans (plan.skip_supply)"
            )
        if hasattr(cls, "debugResource"):
            cls.solver.userexit_resource = cls.debugResource
        if hasattr(cls, "debugDemand"):
            cls.solver.userexit_demand = cls.debugDemand
        if hasattr(cls, "debugOperation"):
            cls.solver.userexit_operation = cls.debugOperation
        # Timed demand feeder is installed below after the deadline is known.
        logger.info("Plan type: %s" % plantype)
        if not is_fas:
            logger.info("Plan algorithm: %s" % mrp_algorithm)
        constraint_str = []
        if constraint & 4:
            constraint_str.append("capa")
        if constraint & 8:
            constraint_str.append("fence")
        if constraint & 16:
            constraint_str.append("mfg_lt")
        if constraint & 32:
            constraint_str.append("po_lt")
        if not constraint_str:
            constraint_str.append("-")
        logger.info("Constraints: %s" % ",".join(constraint_str))
        try:
            if loglevel <= 2 and not settings.DEBUG:
                # Solver output is limited to 300000 lines
                ccAPPS.settings.loglimit = 300000
            from threading import Thread

            def unlock_aco_locked():
                try:
                    for op in ccAPPS.operationplans():
                        if op.getAcoLocked():
                            op.setAcoLocked(False)
                except Exception as e:
                    logger.warning("Failed to unlock auto-fix locks: %s" % e)

            def commit_solver_state():
                try:
                    cls.solver.commit()
                except Exception:
                    pass

            def run_threaded(target, timeout, start_message, done_message, timeout_message):
                logger.info(start_message)
                worker_error = []

                def target_wrapper():
                    try:
                        target()
                    except Exception as exc:
                        worker_error.append(exc)
                        logger.exception("%s failed", start_message)

                worker = Thread(target=target_wrapper)
                worker.start()
                worker.join(timeout=timeout)
                if worker.is_alive():
                    logger.warning(timeout_message, timeout)
                    return False
                if worker_error:
                    return False
                logger.info(done_message)
                return True

            autofix_first = os.environ.get("autofix_first", "").lower() in (
                "1",
                "true",
                "yes",
            )
            # Only mrp, fas and ga are enabled planning algorithms.
            autofix_first = False

            solver_error = []

            rolling_original_quantities = {}
            rolling_state = None

            if solver_mode == "mrp" and window_end and rolling_enabled:
                rolling_rule = policy.get("rolling", {})
                rolling_metadata = rolling_rule.get("items", {})
                try:
                    rolling_parts = max(1, int(rolling_rule.get("parts", 1)))
                except (TypeError, ValueError):
                    rolling_parts = 1
                rolling_demands = [
                    demand
                    for demand in ccAPPS.demands()
                    if demand.status in ("open", "quote") and demand.quantity > 0
                ]
                rolling_original_quantities = {
                    demand.name: float(demand.quantity)
                    for demand in rolling_demands
                }
                max_unlock = max(
                    (
                        float(rule.get("unlock_orders", 1) or 1)
                        for rule in rolling_metadata.values()
                    ),
                    default=1.0,
                )
                max_depth = max(
                    (
                        float(rule.get("bom_depth", 0) or 0)
                        for rule in rolling_metadata.values()
                    ),
                    default=1.0,
                ) or 1.0
                horizon_seconds = max(
                    1.0, (window_end - ccAPPS.settings.current).total_seconds()
                )
                rolling_state = {
                    "active": None,
                    "before": 0.0,
                    "progress": 0,
                    "blocked": {},
                    "attempted": set(),
                    "lots": 0,
                }

                def next_rolling_demand(cluster):
                    # The previous capped demand has just been solved. Restore
                    # its true quantity and use the observed delivery increase
                    # to refresh the candidate set for this same MRP sweep.
                    active = rolling_state["active"]
                    if active is not None:
                        active.quantity = rolling_original_quantities[active.name]
                        rolling_state["attempted"].add(active.name)
                        after = float(active.planned_quantity)
                        if after > rolling_state["before"] + 1e-6:
                            rolling_state["progress"] += 1
                            rolling_state["lots"] += 1
                        else:
                            rolling_state["blocked"][active.name] = rolling_state[
                                "progress"
                            ]
                        rolling_state["active"] = None

                    candidates = []
                    for demand in rolling_demands:
                        if demand.name in rolling_state["attempted"]:
                            continue
                        original_quantity = rolling_original_quantities[demand.name]
                        planned = float(demand.planned_quantity)
                        remaining = original_quantity - planned
                        if remaining <= 1e-6:
                            continue
                        if rolling_state["blocked"].get(demand.name) == rolling_state[
                            "progress"
                        ]:
                            continue

                        metadata = rolling_metadata.get(demand.item.name, {})
                        due = original_demand_due.get(demand.name, demand.due)
                        slack = (due - ccAPPS.settings.current).total_seconds()
                        urgency = 1.0 - min(max(slack, 0.0), horizon_seconds) / horizon_seconds
                        completion = min(1.0, planned / original_quantity)
                        unlock = float(metadata.get("unlock_orders", 1) or 1) / max_unlock
                        depth = float(metadata.get("bom_depth", 0) or 0) / max_depth
                        # Delivery dominates, while shared/deep BOM supply and
                        # nearly completed orders receive enough weight to
                        # release capacity and finish more order lines.
                        score = (
                            0.50 * urgency
                            + 0.20 * unlock
                            + 0.20 * completion
                            + 0.10 * depth
                        )
                        candidates.append((score, due, demand.name, demand, remaining))

                    if not candidates:
                        return None
                    _score, _due, _name, demand, remaining = min(
                        candidates,
                        key=lambda row: (-row[0], row[1], row[2]),
                    )
                    item_rule = item_policies.get(demand.item.name, {})
                    if item_rule.get("exact", False):
                        chunk = remaining
                    else:
                        try:
                            snp = max(1.0, float(item_rule.get("snp", 1) or 1))
                        except (TypeError, ValueError):
                            snp = 1.0
                        import math

                        chunk = min(
                            remaining,
                            math.ceil((remaining / rolling_parts) / snp - 1e-9) * snp,
                        )
                        chunk = max(min(remaining, snp), chunk)
                    rolling_state["active"] = demand
                    rolling_state["before"] = float(demand.planned_quantity)
                    demand.quantity = min(
                        rolling_original_quantities[demand.name],
                        float(demand.planned_quantity) + chunk,
                    )
                    return demand

                # A callback owns the global selection order, so run the one
                # FC network cluster synchronously instead of starting an
                # empty callback loop for every model cluster.
                cls.solver.userexit_nextdemand = next_rolling_demand
                logger.info(
                    "Enabled single-pass rolling MRP for %s demands (%s-way lots)",
                    len(rolling_demands),
                    rolling_parts,
                )

            global_priority_queue = None
            if preserve_demand_priority and not rolling_enabled and not is_fas:
                global_priority_queue = sorted(
                    (
                        demand
                        for demand in ccAPPS.demands()
                        if demand.status in ("open", "quote")
                        and demand.quantity > 0
                    ),
                    key=lambda demand: (
                        demand.priority,
                        demand.due,
                        demand.name,
                    ),
                )

                def next_global_priority_demand(_cluster):
                    if global_priority_queue:
                        return global_priority_queue.pop(0)
                    return None

                cls.solver.userexit_nextdemand = next_global_priority_demand
                logger.info(
                    "Enabled one global MRP priority queue for %s demands",
                    len(global_priority_queue),
                )

            def solve_with_optional_unlock():
                # The solver runs in a worker thread.  Django's scenario
                # router is thread-local, so propagate the selected database
                # before SolverFAS (or MRP callbacks) reads any parameters.
                # Without this, a scenario run silently picked plan.solver
                # from the default database and executed MRP instead.
                from ccAPPSdb.common.middleware import _thread_locals

                previous_database = getattr(_thread_locals, "database", None)
                setattr(_thread_locals, "database", database)
                try:
                    if is_fas:
                        # Single FAS pass. No mop-up / tail retry — lot
                        # sizing is handled by plan.lot_split paths and the
                        # solver's own constrained quantity search.
                        cls.solver.solve()
                    elif rolling_enabled or global_priority_queue is not None:
                        cls.solver.solve(cluster=0)
                    else:
                        cls.solver.solve()
                    # A finite FC window gets a second, non-destructive
                    # fill pass.  The first pass remains anchored on the
                    # real due dates.  For quantities it couldn't finish,
                    # the second pass plans backwards from the window end.
                    # This lets the normal constrained quantity search
                    # fall back from a rejected economic lot to the largest
                    # feasible SNP-sized lot and repeat on the remainder.
                    # Existing on-time operationplans are preserved.
                    # CP must receive the exact same completed MRP window
                    # plan that is exported in MRP-only mode.  Skipping
                    # this fill pass gave CP a weaker seed and allowed the
                    # comparison baseline to differ between both modes.
                    # FAS is strictly single-pass. This fill pass belongs
                    # only to the ordinary MRP baseline.
                    if (
                        solver_mode in ("mrp", "cp")
                        and not is_fas
                        and window_end
                        and not rolling_enabled
                    ):
                        remaining = []
                        for demand in ccAPPS.demands():
                            if demand.status not in ("open", "quote"):
                                continue
                            if demand.planned_quantity + 1e-6 >= demand.quantity:
                                continue
                            remaining.append(
                                (
                                    demand,
                                    demand.due,
                                    demand.maxlateness,
                                    demand.priority,
                                )
                            )
                        if remaining:
                            logger.info(
                                "FC window fill pass for %s remaining demands",
                                len(remaining),
                            )
                            # Freeze the first-pass proposal while filling
                            # the unused tail of the window.  Otherwise a
                            # second MRP solve can replace an on-time lot
                            # with a larger lot ending at the window edge.
                            frozen_opplans = [
                                opplan
                                for opplan in ccAPPS.operationplans()
                                if opplan.status == "proposed"
                            ]
                            for opplan in frozen_opplans:
                                opplan.status = "approved"
                            # The preferred economic lots have already
                            # been attempted in the first pass.  Remove
                            # only that preference during the fill pass so
                            # constrained quantity search can select the
                            # largest feasible SNP multiple.  Exact
                            # lifecycle items remain exact and bounded by
                            # their production limit.
                            fallback_operations = []
                            for operation in ccAPPS.operations():
                                if not getattr(operation, "fc_batch_policy", False):
                                    continue
                                economic = getattr(
                                    operation, "fc_economic_batch", 0.0
                                )
                                if economic > 0:
                                    fallback_operations.append(
                                        (operation, economic)
                                    )
                                    operation.fc_economic_batch = 0.0
                            # All fill-pass due dates become the window
                            # end. Preserve the original priority/due order
                            # explicitly, so later orders don't take the
                            # resource gaps needed by earlier orders.
                            remaining.sort(
                                key=lambda row: (
                                    row[3],
                                    row[1],
                                    row[0].name,
                                )
                            )
                            for rank, (
                                demand,
                                _due,
                                _maxlateness,
                                _priority,
                            ) in enumerate(remaining, 1):
                                demand.due = window_end
                                demand.maxlateness = 0
                                demand.priority = rank
                                item_rule = item_policies.get(
                                    demand.item.name, {}
                                )
                                leftover = max(
                                    0.0,
                                    demand.quantity
                                    - demand.planned_quantity,
                                )
                                if item_rule.get("exact", False) or not lot_split:
                                    # plan.lot_split off / exact: no SNP chunking.
                                    partial_size = leftover
                                else:
                                    try:
                                        partial_size = float(
                                            item_rule.get("snp", 1) or 1
                                        )
                                    except (TypeError, ValueError):
                                        partial_size = 1.0
                                    if partial_size <= 0:
                                        partial_size = 1.0
                                demand.minshipment = partial_size
                            cls.solver.erasepreviousfirst = False
                            try:
                                if preserve_demand_priority:
                                    global_priority_queue.extend(
                                        row[0] for row in remaining
                                    )
                                    cls.solver.solve(cluster=0)
                                else:
                                    cls.solver.solve()
                            finally:
                                for operation, economic in fallback_operations:
                                    operation.fc_economic_batch = economic
                                for opplan in frozen_opplans:
                                    opplan.status = "proposed"
                                for demand, due, maxlateness, priority in remaining:
                                    demand.due = due
                                    demand.maxlateness = maxlateness
                                    demand.priority = priority
                except Exception as exc:
                    solver_error.append(exc)
                finally:
                    if rolling_state is not None:
                        active = rolling_state["active"]
                        if active is not None:
                            active.quantity = rolling_original_quantities[active.name]
                        cls.solver.userexit_nextdemand = None
                        logger.info(
                            "Single-pass rolling MRP created %s progressing lots",
                            rolling_state["lots"],
                        )
                    elif preserve_demand_priority and not is_fas:
                        cls.solver.userexit_nextdemand = None
                    if previous_database is None:
                        delattr(_thread_locals, "database")
                    else:
                        setattr(_thread_locals, "database", previous_database)
                    if autofix_first:
                        unlock_aco_locked()

            # Soft wall-clock limit for the supply solver (seconds).
            # Hard exit is a fallback if the worker never returns; prefer
            # finishing under the limit by keeping resource search short.
            import time as _time

            try:
                solve_timeout = int(
                    Parameter.getValue("plan.solve_timeout", database, "180")
                )
            except Exception:
                solve_timeout = 180
            if solve_timeout <= 0:
                solve_timeout = 1800
            hard_timeout = solve_timeout + 30
            # Cut engine log spam so short-horizon runs aren't disk-bound.
            try:
                ccAPPS.settings.loglimit = min(
                    int(getattr(ccAPPS.settings, "loglimit", 0) or 300000),
                    20000,
                )
            except Exception:
                pass
            if local_skip_supply:
                logger.info(
                    "Supply solve skipped (plan.skip_supply); "
                    "existing operationplans kept for local repair"
                )
            else:
                logger.info(
                    "Supply solve timeout: hard=%ss (loglimit=%s)",
                    hard_timeout,
                    getattr(ccAPPS.settings, "loglimit", None),
                )

                thread = Thread(target=solve_with_optional_unlock)
                thread.start()
                thread.join(timeout=hard_timeout)
                solver_timed_out = thread.is_alive()
                if solver_timed_out:
                    logger.warning(
                        "Skipping solver.solve(): call timed out after %s seconds",
                        hard_timeout,
                    )
                    # C++ MRP keeps running in the worker thread; do not export a
                    # half-built plan. Exit the planner process so the OS reclaims it.
                    os._exit(124)
                elif solver_error:
                    # Never export or commit a partial FAS command stream after a
                    # C++ solver failure.  The previous implementation let a
                    # thread exception pass unnoticed and reported a false commit.
                    if is_fas:
                        cls.solver.rollback()
                    raise solver_error[0]
                else:
                    logger.info("Solver completed")
            # Restore demand.policy after supply solve (alltogether was only
            # a plan.lot_split=0 search control for FAS/MRP).
            if original_demand_policy:
                for demand in ccAPPS.demands():
                    if demand.name in original_demand_policy:
                        demand.policy = original_demand_policy[demand.name]
            if policy_json:
                from collections import defaultdict

                produced = defaultdict(float)
                for opplan in ccAPPS.operationplans():
                    if opplan.operation.name not in policy["operations"]:
                        continue
                    for flowplan in opplan.flowplans:
                        if flowplan.quantity > 0:
                            produced[flowplan.buffer.item.name] += flowplan.quantity
                # Native local MRP deletes and rebuilds proposed plans below.
                # Loop variables otherwise keep the last C++ plan/flow alive
                # as Python references after their native objects are freed.
                # Release them while the loaded plan is still valid.
                opplan = flowplan = None
                violations = {
                    code: (produced[code], limit)
                    for code, limit in policy["limits"].items()
                    if produced[code] > limit + 1e-5
                }
                if violations:
                    raise ValueError("Lifecycle production limits exceeded: %s" % violations)
                logger.info("Lifecycle production limits verified for %s materials", len(policy["limits"]))
            # Run ACO/Exact/AutoFix after MRP if enabled
            run_cp_pass = run_cp_after_supply
            run_ga_pass = run_ga_after_supply
            if local_skip_supply:
                logger.info(
                    "Skip-supply path: no FAS/MRP commit; shared local scope + "
                    "solver=%s will repair the loaded schedule",
                    solver_mode,
                )
            elif solver_mode == "mrp":
                logger.info("MRP-only planning mode: skipping post-MRP optimization")
            elif is_fas:
                cls.solver.commit()
                logger.info(
                    "%s plan committed",
                    (
                        "FAS (CP baseline)"
                        if run_cp_after_supply
                        else "FAS (GA baseline)"
                        if run_ga_after_supply
                        else "Standalone SolverFAS"
                    ),
                )
                # Solver due dates are temporary search controls. Restore the
                # imported promises before export so filler demands don't show
                # artificial lateness against the three-day search window.
                for demand in ccAPPS.demands():
                    original_due = original_demand_due.get(demand.name)
                    if original_due is not None:
                        demand.due = original_due
            if run_ga_pass:
                ga_environment = {
                    "GA_SEED": Parameter.getValue(
                        "ga.seed", database, "123456"
                    ),
                    "GA_POPULATION": os.environ.get("GA_POPULATION", "500"),
                    "GA_GENERATIONS": os.environ.get("GA_GENERATIONS", "50"),
                    "GA_STAGNATION_LIMIT": Parameter.getValue(
                        "ga.stagnation_limit",
                        database,
                        os.environ.get("GA_STAGNATION_LIMIT", "10"),
                    ),
                    "GA_ENABLE_VNS": os.environ.get("GA_ENABLE_VNS", "true"),
                    "GA_VNS_RATIO": os.environ.get("GA_VNS_RATIO", "0.02"),
                    "GA_TIME_LIMIT_SECONDS": os.environ.get(
                        "GA_TIME_LIMIT_SECONDS", "60"
                    ),
                    "GA_OBJECTIVE_PROFILE": Parameter.getValue(
                        "ga.objective_profile", database, "due_date"
                    ),
                    "GA_MATERIAL_MODE": Parameter.getValue(
                        "ga.material_mode", database, "postcheck"
                    ),
                    "GA_LOGLEVEL": os.environ.get("GA_LOGLEVEL", "0"),
                }
                previous_ga_environment = {
                    key: os.environ.get(key) for key in ga_environment
                }
                try:
                    os.environ.update(
                        {key: str(value) for key, value in ga_environment.items()}
                    )
                    logger.info(
                        "Starting GA on the %s manufacturing plan",
                        "FAS" if is_fas else "MRP",
                    )
                    ga_timeout = int(
                        Parameter.getValue(
                            "ga.timeout_seconds",
                            database,
                            os.environ.get("GA_TIMEOUT_SECONDS", "70"),
                        )
                    )
                    run_threaded(
                        ccAPPS.run_ga,
                        ga_timeout,
                        "Full GA optimization...",
                        "Full GA optimization completed",
                        "Full GA optimization timed out after %s seconds",
                    )
                finally:
                    for key, value in previous_ga_environment.items():
                        if value is None:
                            os.environ.pop(key, None)
                        else:
                            os.environ[key] = value
            # Local repair for FAS / MRP / GA: shared scope in C++
            # (LocalScopeGuard), then the chosen solver only moves the
            # movable set. CP uses the dedicated run_cp_pass path below.
            if local_skip_supply and solver_mode in ("fas", "mrp", "ga"):
                def _lp(name, legacy, default=""):
                    return Parameter.getValue(
                        name,
                        database,
                        Parameter.getValue(legacy, database, default),
                    )

                local_repair_env = {
                    # Keep constraint mode aligned for FAS/GA/CP entry points
                    # that re-read CCAPPS_* instead of the Python solver object.
                    "CCAPPS_CONSTRAINT": os.environ.get(
                        "CCAPPS_CONSTRAINT",
                        "capa,mfg_lt,po_lt",
                    ),
                    "CCAPPS_PLANTYPE": str(plantype),
                    "LOCAL_RESCHEDULE": "1",
                    "CP_LOCAL_RESCHEDULE": "1",
                    "LOCAL_RESCHEDULE_TYPE": _lp(
                        "local.reschedule_type", "cp.local_reschedule_type", "order"
                    ),
                    "CP_LOCAL_RESCHEDULE_TYPE": _lp(
                        "local.reschedule_type", "cp.local_reschedule_type", "order"
                    ),
                    "LOCAL_RESCHEDULE_LEVEL": _lp(
                        "local.reschedule_level", "cp.local_reschedule_level", "auto"
                    ),
                    "CP_LOCAL_RESCHEDULE_LEVEL": _lp(
                        "local.reschedule_level", "cp.local_reschedule_level", "auto"
                    ),
                    "LOCAL_SCOPE_BASE": _lp(
                        "local.scope_base", "cp.local_scope_base", "1"
                    ),
                    "CP_LOCAL_SCOPE_BASE": _lp(
                        "local.scope_base", "cp.local_scope_base", "1"
                    ),
                    "LOCAL_SCOPE_RELAX": _lp(
                        "local.scope_relax", "cp.local_scope_relax", ""
                    ),
                    "CP_LOCAL_SCOPE_RELAX": _lp(
                        "local.scope_relax", "cp.local_scope_relax", ""
                    ),
                    "LOCAL_CHANGED_RESOURCES": _lp(
                        "local.changed_resources",
                        "cp.local_changed_resources",
                        "",
                    ),
                    "CP_LOCAL_CHANGED_RESOURCES": _lp(
                        "local.changed_resources",
                        "cp.local_changed_resources",
                        "",
                    ),
                    "LOCAL_CHANGED_MATERIALS": _lp(
                        "local.changed_materials",
                        "cp.local_changed_materials",
                        "",
                    ),
                    "CP_LOCAL_CHANGED_MATERIALS": _lp(
                        "local.changed_materials",
                        "cp.local_changed_materials",
                        "",
                    ),
                    "LOCAL_NEW_DEMANDS": _lp(
                        "local.new_demands", "cp.local_new_demands", ""
                    ),
                    "CP_LOCAL_NEW_DEMANDS": _lp(
                        "local.new_demands", "cp.local_new_demands", ""
                    ),
                    "LOCAL_DELETED_DEMANDS": _lp(
                        "local.deleted_demands", "cp.local_deleted_demands", ""
                    ),
                    "CP_LOCAL_DELETED_DEMANDS": _lp(
                        "local.deleted_demands", "cp.local_deleted_demands", ""
                    ),
                    "CP_HORIZON_DAYS": Parameter.getValue(
                        "cp.horizon_days", database, "3"
                    ),
                }
                if solver_mode == "ga":
                    local_repair_env.update(
                        {
                            "GA_SEED": Parameter.getValue(
                                "ga.seed", database, "123456"
                            ),
                            "GA_POPULATION": os.environ.get("GA_POPULATION", "500"),
                            "GA_GENERATIONS": os.environ.get("GA_GENERATIONS", "50"),
                            "GA_STAGNATION_LIMIT": Parameter.getValue(
                                "ga.stagnation_limit",
                                database,
                                os.environ.get("GA_STAGNATION_LIMIT", "10"),
                            ),
                            "GA_ENABLE_VNS": os.environ.get("GA_ENABLE_VNS", "true"),
                            "GA_VNS_RATIO": os.environ.get("GA_VNS_RATIO", "0.02"),
                            "GA_TIME_LIMIT_SECONDS": os.environ.get(
                                "GA_TIME_LIMIT_SECONDS", "60"
                            ),
                            "GA_OBJECTIVE_PROFILE": Parameter.getValue(
                                "ga.objective_profile", database, "due_date"
                            ),
                            "GA_MATERIAL_MODE": Parameter.getValue(
                                # postcheck sequences capacity first. External
                                # purchase shortages are reported afterwards.
                                # Internal WIP stays a hard decode constraint.
                                # Set ga.material_mode=precheck to reject a
                                # chromosome that is short any fixed supply.
                                "ga.material_mode", database, "postcheck"
                            ),
                            "GA_LOGLEVEL": os.environ.get("GA_LOGLEVEL", "0"),
                        }
                    )
                prev_env = {k: os.environ.get(k) for k in local_repair_env}
                try:
                    os.environ.update(
                        {k: str(v) for k, v in local_repair_env.items()}
                    )
                    logger.info(
                        "Local %s entry: shared scope + repair "
                        "(type=%s level=%s)",
                        solver_mode.upper(),
                        local_repair_env["LOCAL_RESCHEDULE_TYPE"],
                        local_repair_env["LOCAL_RESCHEDULE_LEVEL"],
                    )
                    def _supply_expansion_names(env):
                        """Orders allowed to receive new supply on this run.

                        Resource and material repairs, deletes, and priority
                        changes return nothing. Inserts and quantity increases
                        return only the names in local.new_demands. all follows
                        the same rule.
                        """
                        mode = str(
                            env.get("LOCAL_RESCHEDULE_TYPE") or "order"
                        ).strip().lower()
                        if mode in ("combined", "mixed"):
                            mode = "all"
                        if mode not in ("order", "all"):
                            return set()

                        def split(key):
                            return {
                                part.strip()
                                for part in str(env.get(key) or "").replace(
                                    ";", ","
                                ).split(",")
                                if part.strip()
                            }

                        new_names = split("LOCAL_NEW_DEMANDS")
                        deleted_names = split("LOCAL_DELETED_DEMANDS")
                        supplied = {}
                        for plan in ccAPPS.operationplans():
                            demand = getattr(plan, "demand", None)
                            if (
                                demand is None
                                or demand.name not in new_names
                                or str(
                                    getattr(plan, "ordertype", "") or ""
                                ).upper()
                                != "DLVR"
                            ):
                                continue
                            supplied[demand.name] = supplied.get(
                                demand.name, 0.0
                            ) + max(0.0, float(plan.quantity or 0))
                        by_name = {
                            demand.name: demand
                            for demand in ccAPPS.demands()
                            if demand.name in new_names
                        }
                        allow = set()
                        for name in new_names:
                            if name not in deleted_names:
                                allow.add(name)
                                continue
                            demand = by_name.get(name)
                            if demand is None:
                                continue
                            planned = max(
                                supplied.get(name, 0.0),
                                float(
                                    getattr(demand, "planned_quantity", 0) or 0
                                ),
                            )
                            if float(demand.quantity or 0) > planned + 1e-6:
                                allow.add(name)
                        return allow

                    expansion_names = _supply_expansion_names(local_repair_env)

                    # MRP and FAS may create work orders. GA and CP may not.
                    os.environ["LOCAL_SUPPLY_REGEN"] = (
                        "1" if solver_mode in ("mrp", "fas") else "0"
                    )
                    def _solve_and_record():
                        if solver_mode == "fas":
                            follow_changed_demand_quantity(
                                local_repair_env, ccAPPS.operationplans()
                            )
                            ccAPPS.run_fas()
                        elif solver_mode == "ga":
                            ga_quantity_audit = {
                                p.reference: float(p.quantity)
                                for p in ccAPPS.operationplans()
                            }
                            logger.info(
                                "Local GA: sequencing existing work orders; "
                                "not producing new ones"
                            )
                            ga_timeout = int(
                                Parameter.getValue(
                                    "ga.timeout_seconds",
                                    database,
                                    os.environ.get("GA_TIMEOUT_SECONDS", "70"),
                                )
                            )
                            run_threaded(
                                ccAPPS.run_ga,
                                ga_timeout,
                                "Local GA repair...",
                                "Local GA completed",
                                "Local GA timed out after %s seconds",
                            )
                            for audit_plan in ccAPPS.operationplans():
                                old_quantity = ga_quantity_audit.get(audit_plan.reference)
                                if old_quantity is not None and abs(old_quantity - float(audit_plan.quantity)) > 1e-6:
                                    logger.warning("GA quantity audit: ref=%s operation=%s before=%s after=%s", audit_plan.reference, audit_plan.operation.name, old_quantity, audit_plan.quantity)
                            audit_plan = None
                        else:
                            # Hot plans stay proposed and are rebuilt. Frozen plans
                            # were confirmed by the scope, so this erase leaves
                            # them locked.
                            # Must use cluster=0 when userexit_nextdemand is set
                            # (global priority queue): default solve() fans out
                            # one thread per cluster against the same Python
                            # queue and segfaults under concurrent pop.
                            # LOCAL_SOLVER selects the MRP gates in the shared
                            # scope helper. FAS sets the same variable itself.
                            os.environ["LOCAL_SOLVER"] = "mrp"
                            follow_changed_demand_quantity(
                                local_repair_env, ccAPPS.operationplans()
                            )

                            def _open_inserts_without_plan(env):
                                def split(key):
                                    return {
                                        part.strip()
                                        for part in str(env.get(key) or "")
                                        .replace(";", ",")
                                        .split(",")
                                        if part.strip()
                                    }

                                pure = split("LOCAL_NEW_DEMANDS") - split(
                                    "LOCAL_DELETED_DEMANDS"
                                )
                                if not pure:
                                    return []
                                present = set()
                                for plan in ccAPPS.operationplans():
                                    demand = getattr(plan, "demand", None)
                                    if (
                                        demand is not None
                                        and getattr(demand, "name", None) in pure
                                    ):
                                        present.add(demand.name)
                                return [
                                    demand
                                    for demand in ccAPPS.demands()
                                    if demand.name in pure
                                    and demand.name not in present
                                    and demand.status == "open"
                                    and float(demand.quantity or 0) > 0
                                ]

                            insert_queue = _open_inserts_without_plan(
                                local_repair_env
                            )
                            if insert_queue:
                                logger.info(
                                    "Local MRP insert: %s open demand(s) have "
                                    "no historical plan",
                                    len(insert_queue),
                                )

                                def next_insert_demand(
                                    _cluster, _queue=insert_queue
                                ):
                                    if _queue:
                                        return _queue.pop(0)
                                    return None

                                cls.solver.userexit_nextdemand = next_insert_demand
                            cls.solver.erasepreviousfirst = True
                            logger.info(
                                "Local MRP: frozen plans kept; remaining "
                                "proposed cleared and rebuilt"
                            )
                            try:
                                if getattr(cls.solver, "userexit_nextdemand", None):
                                    cls.solver.solve(cluster=0)
                                else:
                                    cls.solver.solve()
                            finally:
                                os.environ.pop("LOCAL_SOLVER", None)
                        local_outcome = {}
                        try:
                            raw_outcome = ccAPPS.cp_last_local_outcome()
                            if isinstance(raw_outcome, dict):
                                local_outcome = dict(raw_outcome)
                        except Exception:
                            local_outcome = {
                                "local_ok": bool(ccAPPS.cp_last_local_ok()),
                                "stage": str(ccAPPS.cp_last_run_stage()),
                                "applied": int(ccAPPS.cp_last_run_applied()),
                            }
                        local_outcome["solver"] = solver_mode
                        local_outcome["status"] = bool(
                            local_outcome.pop("local_ok", False)
                        )
                        if solver_mode == "fas":
                            # FAS completion is represented by the task status and
                            # boolean outcome status. It has no CP-style
                            # multi-stage lifecycle.
                            local_outcome.pop("stage", None)
                            logger.info(
                                "Local FAS result: status=%s applied=%s",
                                local_outcome.get("status"),
                                local_outcome.get("applied"),
                            )
                        else:
                            logger.info(
                                "Local %s result: status=%s stage=%s applied=%s",
                                solver_mode.upper(),
                                local_outcome.get("status"),
                                local_outcome.get("stage"),
                                local_outcome.get("applied"),
                            )
                        try:
                            import json as _json

                            outcome_json = _json.dumps(
                                local_outcome, ensure_ascii=False
                            )
                            Parameter.objects.using(database).update_or_create(
                                name="cp.local_last_outcome",
                                defaults={"value": outcome_json},
                            )
                            Parameter.objects.using(database).update_or_create(
                                name="local.last_outcome",
                                defaults={"value": outcome_json},
                            )
                            task_id = os.environ.get("CCAPPS_TASKID")
                            if task_id:
                                from ccAPPSdb.execute.models import Task

                                Task.objects.using(database).filter(
                                    id=int(task_id)
                                ).update(
                                    message="local_outcome="
                                    + _json.dumps(
                                        local_outcome,
                                        ensure_ascii=False,
                                        separators=(",", ":"),
                                    )
                                )
                        except Exception:
                            logger.exception("Failed to persist local.last_outcome")
                        # A local solver works on the in-memory copy loaded from
                        # the database.  Do not let the following export steps
                        # replace the last valid plan when the final outcome gate
                        # rejects that copy.  The diagnostic parameter above is
                        # written directly and remains available to the API.
                        if not local_outcome.get("status", False):
                            raise RuntimeError(
                                "%s local reschedule failed final validation: %s"
                                % (solver_mode.upper(), local_outcome)
                            )
                        if solver_mode == "fas":
                            try:
                                from ccAPPSdb.execute.views import clear_reschedule_revert

                                clear_reschedule_revert(database)
                            except Exception:
                                logger.exception(
                                    "Failed to clear reschedule revert snapshot"
                                )
                    finish_local_solve(solver_mode, database, _solve_and_record)
                finally:
                    for key, value in prev_env.items():
                        if value is None:
                            os.environ.pop(key, None)
                        else:
                            os.environ[key] = value
            # Pre-CP checks:
            #   soft → ccAPPS.problems() (always advisory)
            #   model preflight (C++): hard for seed continue-optimize;
            #     soft for local reschedule (never blocks repair entry)
            if run_cp_pass:
                soft_counts = {}
                for problem in ccAPPS.problems():
                    name = str(getattr(problem, "name", "") or "")
                    if not name:
                        continue
                    soft_counts[name] = soft_counts.get(name, 0) + 1
                problem = None
                soft_schedule = {
                    k: v
                    for k, v in soft_counts.items()
                    if k != "invalid data"
                }
                if soft_schedule:
                    logger.warning(
                        "CP soft preflight: engine problems before CP: %s "
                        "(advisory; hard gate is model preflight)",
                        soft_schedule,
                    )
                else:
                    logger.info(
                        "CP soft preflight: no schedule problems before CP%s",
                        (
                            " (invalid data=%s)"
                            % soft_counts.get("invalid data")
                            if soft_counts.get("invalid data")
                            else ""
                        ),
                    )
                try:
                    if local_skip_supply:
                        logger.info(
                            "Starting CP-SAT local reschedule on the existing plan"
                        )
                    else:
                        logger.info(
                            "Starting CP-SAT on the %s manufacturing plan",
                            "FAS" if is_fas else "MRP",
                        )
                    cp_baseline_plans = []
                    candidate_solver = None
                    expanded_plan_count = 0
                    insert_delivery_placeholders = []
                    insert_original_demand_qty = {}
                    generate_candidates = Parameter.getValue(
                        "cp.generate_candidates", database, "false"
                    ).lower() in ("1", "true", "yes")
                    # Order-local framework: NEW ∪ DELETED seed the same
                    # L1/L2/L3 freeze. 增=new; 删=deleted; 凡改(量/优先级/
                    # 属性)=同一张单先删再增 (同名进 deleted+new).
                    # Insert-candidates gated ONLY on cp.local_new_demands —
                    # pure-delete must not lock baseline.
                    insert_only_raw = (
                        Parameter.getValue(
                            "cp.local_new_demands", database, ""
                        )
                        or os.environ.get("CP_LOCAL_NEW_DEMANDS", "")
                        or ""
                    )
                    insert_only = {
                        n.strip()
                        for n in insert_only_raw.replace(";", ",").split(",")
                        if n.strip()
                    }
                    deleted_only_raw = (
                        Parameter.getValue(
                            "cp.local_deleted_demands", database, ""
                        )
                        or os.environ.get("CP_LOCAL_DELETED_DEMANDS", "")
                        or ""
                    )
                    deleted_only = {
                        n.strip()
                        for n in deleted_only_raw.replace(";", ",").split(",")
                        if n.strip()
                    }
                    pure_insert_names = insert_only - deleted_only
                    # NEW is the rescheduling trigger, not necessarily a need
                    # for fresh MRP supply.  A same-name NEW+DELETED demand is
                    # a change.  Reuse its complete existing tree when supply
                    # quantity still matches (priority/due changes); quantity
                    # changes and true inserts still require candidate MRP.
                    supplied_by_demand = {}
                    for plan in ccAPPS.operationplans():
                        demand = getattr(plan, "demand", None)
                        if (
                            demand is not None
                            and demand.name in insert_only
                            and str(getattr(plan, "ordertype", "") or "").upper()
                            == "DLVR"
                        ):
                            supplied_by_demand[demand.name] = (
                                supplied_by_demand.get(demand.name, 0.0)
                                + max(0.0, float(plan.quantity or 0))
                            )
                    demand_by_name = {
                        demand.name: demand for demand in ccAPPS.demands()
                        if demand.name in insert_only
                    }
                    candidate_insert_names = set(insert_only - deleted_only)
                    for name in insert_only & deleted_only:
                        demand = demand_by_name.get(name)
                        if demand is None:
                            continue
                        supplied = max(
                            supplied_by_demand.get(name, 0.0),
                            float(getattr(demand, "planned_quantity", 0) or 0),
                        )
                        if float(demand.quantity or 0) > supplied + 1e-6:
                            candidate_insert_names.add(name)
                    candidate_expansion_blocked = False
                    active_local_type = (
                        os.environ.get("CP_LOCAL_RESCHEDULE_TYPE")
                        or Parameter.getValue(
                            "cp.local_reschedule_type", database, "order"
                        )
                    ).strip().lower()
                    local_includes_order = active_local_type in (
                        "order",
                        "all",
                        "combined",
                        "mixed",
                    )
                    # CP reschedule only moves work orders that already exist.
                    if local_skip_supply:
                        generate_candidates = False
                        logger.info(
                            "CP local: sequencing existing work orders; "
                            "not producing new ones"
                        )
                    if generate_candidates and window_end:
                        # Preserve the accepted finite-capacity MRP plan as
                        # locked work. Build unconstrained proposal trees only
                        # for its remaining demand, so CP can add throughput
                        # without sacrificing the baseline.
                        cp_baseline_plans = [
                            plan for plan in ccAPPS.operationplans()
                            if plan.status == "proposed"
                        ]
                        for plan in cp_baseline_plans:
                            plan.status = "approved"
                        # A newly inserted order can arrive with a standalone
                        # DLVR placeholder. Its quantity makes planned_quantity
                        # look complete even though no manufacturing tree
                        # exists. Temporarily release that placeholder so MRP
                        # expands the inserted order's BOM. Restore it if CP
                        # fails; on success the newly expanded delivery/tree
                        # replaces it.
                        if candidate_insert_names:
                            for plan in cp_baseline_plans:
                                demand = getattr(plan, "demand", None)
                                if (
                                    demand is not None
                                    and demand.name in candidate_insert_names
                                    and str(
                                        getattr(plan, "ordertype", "") or ""
                                    ).upper() == "DLVR"
                                    and float(plan.quantity or 0) > 0
                                ):
                                    insert_delivery_placeholders.append(
                                        (plan, float(plan.quantity))
                                    )
                            if insert_delivery_placeholders:
                                # Make the placeholder quantity look newly
                                # unmet while MRP expands candidates. After
                                # expansion the old placeholder is zeroed and
                                # the new delivery/tree takes its place.
                                extra_by_demand = {}
                                for plan, quantity in insert_delivery_placeholders:
                                    demand = plan.demand
                                    extra_by_demand[demand] = (
                                        extra_by_demand.get(demand, 0.0)
                                        + quantity
                                    )
                                for demand, quantity in extra_by_demand.items():
                                    insert_original_demand_qty[demand] = float(
                                        demand.quantity
                                    )
                                    demand.quantity = float(demand.quantity) + quantity
                                logger.info(
                                    "CP local insert: exposed %s delivery "
                                    "placeholder(s) as unmet for BOM expansion",
                                    len(insert_delivery_placeholders),
                                )
                        candidate_demands = []
                        # When insert_only is set, fence every other open/quote
                        # demand so unconstrained MRP cannot grow unrelated
                        # trees (e.g. window-incomplete UNIN orders).
                        fenced_demands = []
                        for demand in ccAPPS.demands():
                            if demand.status not in ("open", "quote"):
                                continue
                            if (
                                candidate_insert_names
                                and demand.name not in candidate_insert_names
                            ):
                                fenced_demands.append(
                                    (demand, demand.status)
                                )
                                demand.status = "closed"
                                continue
                            if demand.planned_quantity + 1e-6 >= demand.quantity:
                                continue
                            candidate_demands.append(
                                (demand, demand.due, demand.maxlateness,
                                 demand.priority, demand.minshipment)
                            )
                            demand.due = window_end
                            demand.maxlateness = 0
                            demand.minshipment = max(
                                1.0, demand.quantity - demand.planned_quantity
                            )
                        if fenced_demands:
                            logger.info(
                                "CP local insert: fenced %s non-insert "
                                "open/quote demand(s) during candidate MRP",
                                len(fenced_demands),
                            )
                        if candidate_demands:
                            os.environ["CP_LOCAL_NEW_DEMANDS"] = ",".join(
                                d[0].name for d in candidate_demands
                            )
                            logger.info(
                                "CP local insert demands: %s",
                                os.environ["CP_LOCAL_NEW_DEMANDS"],
                            )
                        plans_before_candidates = list(ccAPPS.operationplans())
                        before_candidate_ids = {
                            id(plan) for plan in plans_before_candidates
                        }
                        before_candidates = len(plans_before_candidates)
                        candidate_solver = ccAPPS.solver_mrp(
                            constraints=0,
                            plantype=plantype,
                            loglevel=loglevel,
                            lazydelay=int(Parameter.getValue(
                                "lazydelay", database, "86400"
                            )),
                            minimumdelay=0,
                            rotateresources=True,
                            iterationmax=0,
                            resourceiterationmax=50,
                        )
                        candidate_solver.erasepreviousfirst = False
                        try:
                            candidate_solver.solve()
                        finally:
                            for demand, quantity in insert_original_demand_qty.items():
                                demand.quantity = quantity
                            for plan, _quantity in insert_delivery_placeholders:
                                plan.quantity = 0
                            for demand, due, lateness, priority, shipment in candidate_demands:
                                demand.due = due
                                demand.maxlateness = lateness
                                demand.priority = priority
                                demand.minshipment = shipment
                            for demand, status in fenced_demands:
                                demand.status = status
                        added_candidates = [
                            plan
                            for plan in ccAPPS.operationplans()
                            if id(plan) not in before_candidate_ids
                        ]
                        # Belt-and-suspenders: drop any newly created trees
                        # that peg to a demand outside the insert allow-list.
                        if candidate_insert_names and added_candidates:
                            stray_roots = []
                            for plan in added_candidates:
                                dem = getattr(plan, "demand", None)
                                if (
                                    dem is None
                                    or dem.name in candidate_insert_names
                                ):
                                    continue
                                root = plan
                                while getattr(root, "owner", None) is not None:
                                    root = root.owner
                                if root not in stray_roots:
                                    stray_roots.append(root)
                            for root in stray_roots:
                                try:
                                    root.delete()
                                except Exception:
                                    pass
                            if stray_roots:
                                logger.info(
                                    "CP local insert: deleted %s stray "
                                    "candidate root(s) outside named demands",
                                    len(stray_roots),
                                )
                            added_candidates = [
                                plan
                                for plan in ccAPPS.operationplans()
                                if id(plan) not in before_candidate_ids
                            ]
                        after_candidates = len(list(ccAPPS.operationplans()))
                        expanded_plan_count = after_candidates
                        logger.info(
                            "CP candidate expansion added %s operation plans for %s incomplete demands",
                            max(0, after_candidates - before_candidates),
                            len(candidate_demands),
                        )
                        insert_item_names = {
                            demand.item.name
                            for demand, *_rest in candidate_demands
                            if demand.item is not None
                        }
                        tagged_insert_roots = set()
                        tagged_insert_items = set()
                        for plan in added_candidates:
                            root = plan
                            while getattr(root, "owner", None) is not None:
                                root = root.owner
                            operation = getattr(root, "operation", None)
                            item = getattr(operation, "item", None)
                            if (
                                item is not None
                                and item.name in insert_item_names
                            ):
                                root.cp_local_insert = True
                                tagged_insert_roots.add(id(root))
                                tagged_insert_items.add(item.name)
                        logger.info(
                            "CP local insert: tagged %s manufacturing root(s)",
                            len(tagged_insert_roots),
                        )
                        shortage_items = set()
                        for problem in ccAPPS.problems():
                            if str(problem.name) != "material shortage":
                                continue
                            owner = getattr(problem, "owner", None)
                            item = getattr(owner, "item", None)
                            if item is not None:
                                shortage_items.add(item.name)
                        problem = owner = item = None
                        blocked_demands = [
                            demand.name
                            for demand, *_rest in candidate_demands
                            if (
                                demand.name in (insert_only - deleted_only)
                                and demand.item is not None
                                and demand.item.name not in tagged_insert_items
                                and demand.item.name in shortage_items
                            )
                        ]
                        if blocked_demands:
                            candidate_expansion_blocked = True
                            logger.error(
                                "CP local insert rejected after candidate "
                                "expansion: no manufacturing path and "
                                "insufficient inventory for %s",
                                blocked_demands,
                            )
                        if len(added_candidates) <= 20:
                            for plan in added_candidates:
                                owner = getattr(plan, "owner", None)
                                logger.info(
                                    "CP candidate detail: ref=%s op=%s "
                                    "demand=%s owner=%s owner_demand=%s",
                                    getattr(plan, "reference", ""),
                                    getattr(
                                        getattr(plan, "operation", None),
                                        "name",
                                        "",
                                    ),
                                    getattr(
                                        getattr(plan, "demand", None),
                                        "name",
                                        "",
                                    ),
                                    getattr(owner, "reference", ""),
                                    getattr(
                                        getattr(owner, "demand", None),
                                        "name",
                                        "",
                                    ),
                                )
                        all_candidates_variable = Parameter.getValue(
                            "cp.all_candidates_variable", database, "false"
                        ).lower() in ("1", "true", "yes")
                        if all_candidates_variable:
                            logger.info(
                                "CP marked the MRP baseline and exposed all baseline and supplemental operations as decision variables"
                            )
                        else:
                            all_candidates_variable = False
                    else:
                        all_candidates_variable = False
                    # Supply (MRP or FAS) has already materialized manufacturing
                    # orders. Don't call solver.commit() here on the MRP stream:
                    # on the FC network that commit re-expands the proposal graph
                    # before CP reads it. FAS already committed above.
                    #
                    # Same CP knobs for either baseline — throughput/auto packs
                    # the early window (personnel); no separate all-mode path.
                    # Lot-split (whole XOR head+tail) in the same CP model.
                    # Single switch: common_parameter cp.lot_split (0/1).
                    # Eligibility details stay as C++ defaults (exact>8h, economic).
                    # Prefer a process env override when set (shell / warm scripts);
                    # otherwise fall back to common_parameter, then default.
                    def _cp_env(env_key, db_key, default):
                        raw = os.environ.get(env_key)
                        if raw is not None and str(raw).strip() != "":
                            return str(raw).strip()
                        return Parameter.getValue(db_key, database, default)

                    cp_environment = {
                        "CCAPPS_CONSTRAINT": os.environ.get(
                            "CCAPPS_CONSTRAINT", "capa,mfg_lt,po_lt"
                        ),
                        "CCAPPS_PLANTYPE": str(plantype),
                        "CP_OBJECTIVE_METHOD": _cp_env(
                            "CP_OBJECTIVE_METHOD",
                            "cp.objective_method",
                            "weighted",
                        ),
                        # Default throughput so lex uses util window
                        # (current .. +horizon_days, e.g. 16 08:00→19 08:00).
                        "CP_PLANNING_MODE": _cp_env(
                            "CP_PLANNING_MODE",
                            "cp.planning_mode",
                            "throughput",
                        ),
                        "CP_MATERIAL_MODE": _cp_env(
                            "CP_MATERIAL_MODE",
                            "cp.material_mode",
                            "all",
                        ),
                        "CP_HORIZON_DAYS": _cp_env(
                            "CP_HORIZON_DAYS",
                            "cp.horizon_days",
                            "3",
                        ),
                        "CP_THROUGHPUT_OBJECTIVE": _cp_env(
                            "CP_THROUGHPUT_OBJECTIVE",
                            "cp.throughput_objective",
                            "personnel",
                        ),
                        "CP_TIME_LIMIT_SECONDS": _cp_env(
                            "CP_TIME_LIMIT_SECONDS",
                            "cp.time_limit_seconds",
                            "60",
                        ),
                        "CP_LOT_SPLIT": _cp_env(
                            "CP_LOT_SPLIT", "cp.lot_split", "0"
                        ),
                        # earlyPersonnel / util objective scope: resource name,
                        # or "all" for every 人员 workshop (PERSON_1..5).
                        "CP_EARLY_PERSONNEL_RESOURCE": _cp_env(
                            "CP_EARLY_PERSONNEL_RESOURCE",
                            "cp.early_personnel_resource",
                            "all",
                        ),
                        # Optional lots in the short util window (needed when
                        # XOR split would otherwise force a hard all-mode model).
                        "CP_WINDOW_SELECT": _cp_env(
                            "CP_WINDOW_SELECT",
                            "cp.window_select",
                            "0",
                        ),
                        "CP_DUE_FIRST": _cp_env(
                            "CP_DUE_FIRST", "cp.due_first", "0"
                        ),
                        "CP_WEIGHT_MAKESPAN": _cp_env(
                            "CP_WEIGHT_MAKESPAN",
                            "cp.weight_makespan",
                            "80",
                        ),
                        "CP_WEIGHT_LOAD": _cp_env(
                            "CP_WEIGHT_LOAD", "cp.weight_load", "10"
                        ),
                        "CP_WEIGHT_TARDINESS": _cp_env(
                            "CP_WEIGHT_TARDINESS",
                            "cp.weight_tardiness",
                            "10",
                        ),
                        "CP_EARLY_DAYS": _cp_env(
                            "CP_EARLY_DAYS", "cp.early_days", "3"
                        ),
                        # Lex/tardiness primary: only demands due in this many
                        # days (default follow CP_EARLY_DAYS / 3). 0 = all-horizon.
                        "CP_TARDY_WINDOW_DAYS": _cp_env(
                            "CP_TARDY_WINDOW_DAYS",
                            "cp.tardy_window_days",
                            "-1",
                        ),
                        "CP_WEIGHT_PERSONNEL": _cp_env(
                            "CP_WEIGHT_PERSONNEL",
                            "cp.weight_personnel",
                            "0",
                        ),
                        "CP_SEED_MRP": _cp_env(
                            "CP_SEED_MRP", "cp.seed_mrp", "1"
                        ),
                        # Hard model preflight: INFEASIBLE seed → block CP.
                        "CP_PREFLIGHT_ABORT_ON_CONFLICT": _cp_env(
                            "CP_PREFLIGHT_ABORT_ON_CONFLICT",
                            "cp.abort_on_conflict",
                            "1",
                        ),
                        # Fix any baseline seed; ask CP-SAT feasibility.
                        "CP_PREFLIGHT_MODEL": _cp_env(
                            "CP_PREFLIGHT_MODEL",
                            "cp.preflight_model",
                            "1",
                        ),
                        "CP_PREFLIGHT_SECONDS": _cp_env(
                            "CP_PREFLIGHT_SECONDS",
                            "cp.preflight_seconds",
                            "60",
                        ),
                        "CP_LEX_STAGES": _cp_env(
                            "CP_LEX_STAGES", "cp.lex_stages", "1"
                        ),
                        "CP_REPAIR_HINT": _cp_env(
                            "CP_REPAIR_HINT", "cp.repair_hint", "0"
                        ),
                        # Schedule-stability/change-rate is an on-demand
                        # feature.  Master switch defaults off so ordinary CP
                        # runs don't build deviation variables or score it.
                        "CP_CHANGE_RATE_ENABLED": _cp_env(
                            "CP_CHANGE_RATE_ENABLED",
                            "cp.change_rate_enabled",
                            "0",
                        ),
                        # When the master switch is on, this controls the
                        # soft-lex tertiary start-time anchor.
                        "CP_ANCHOR_SEED": _cp_env(
                            "CP_ANCHOR_SEED", "cp.anchor_seed", "1"
                        ),
                        # A root work order counts as changed only when at
                        # least one anchored leaf moves by more than this.
                        "CP_CHANGE_TOLERANCE_SECONDS": _cp_env(
                            "CP_CHANGE_TOLERANCE_SECONDS",
                            "cp.change_tolerance_seconds",
                            "60",
                        ),
                        "CP_CHANGE_MEDIUM_SECONDS": _cp_env(
                            "CP_CHANGE_MEDIUM_SECONDS",
                            "cp.change_medium_seconds",
                            "900",
                        ),
                        "CP_CHANGE_MAJOR_SECONDS": _cp_env(
                            "CP_CHANGE_MAJOR_SECONDS",
                            "cp.change_major_seconds",
                            "7200",
                        ),
                        # 重调度: freeze proposed/approved trees outside
                        # the short window; only reoptimize in-window / late
                        # / demand-pegged work (pairs with anchor-seed).
                        "CP_LOCAL_RESCHEDULE": _cp_env(
                            "CP_LOCAL_RESCHEDULE",
                            "cp.local_reschedule",
                            "0",
                        ),
                        "CP_LOCAL_RESCHEDULE_TYPE": _cp_env(
                            "CP_LOCAL_RESCHEDULE_TYPE",
                            "cp.local_reschedule_type",
                            "order",
                        ),
                        "CP_LOCAL_RESCHEDULE_LEVEL": _cp_env(
                            "CP_LOCAL_RESCHEDULE_LEVEL",
                            "cp.local_reschedule_level",
                            "auto",
                        ),
                        # level=custom: starting freeze (1|2|3) + optional
                        # customer 松绑 tokens (neighbors,personnel,window,all).
                        "CP_LOCAL_SCOPE_BASE": _cp_env(
                            "CP_LOCAL_SCOPE_BASE",
                            "cp.local_scope_base",
                            "1",
                        ),
                        "CP_LOCAL_SCOPE_RELAX": _cp_env(
                            "CP_LOCAL_SCOPE_RELAX",
                            "cp.local_scope_relax",
                            "",
                        ),
                        # Resource-change 统一口径: 点名设备/人员/模具
                        # (e.g. "H01,PERSON_制造部4工段"). 数据侧用
                        # maximum_calendar 容量×时段.
                        "CP_LOCAL_CHANGED_RESOURCES": _cp_env(
                            "CP_LOCAL_CHANGED_RESOURCES",
                            "cp.local_changed_resources",
                            "",
                        ),
                        # Material-change 统一口径: 点名物料/buffer
                        # (e.g. "04.09.00289"). 数据侧改 STCK/PO 到货/数量.
                        "CP_LOCAL_CHANGED_MATERIALS": _cp_env(
                            "CP_LOCAL_CHANGED_MATERIALS",
                            "cp.local_changed_materials",
                            "",
                        ),
                        # Order-local named seeds (one framework, two lists):
                        #   new     = 插单 / 凡改重插
                        #   deleted = 删单残留 / 凡改先删
                        # 凡改: same demand name in both lists.
                        "CP_LOCAL_NEW_DEMANDS": _cp_env(
                            "CP_LOCAL_NEW_DEMANDS",
                            "cp.local_new_demands",
                            os.environ.get("CP_LOCAL_NEW_DEMANDS", ""),
                        ),
                        "CP_LOCAL_DELETED_DEMANDS": _cp_env(
                            "CP_LOCAL_DELETED_DEMANDS",
                            "cp.local_deleted_demands",
                            os.environ.get("CP_LOCAL_DELETED_DEMANDS", ""),
                        ),
                        # L3/local: proposed DLVR are realigned after solve;
                        # do not pin their old drains as lockedMaterial while
                        # manufacturing is globally free. Closed/confirmed
                        # stay locked. Set 0 to keep deliveries fixed in CP.
                        "CP_LOCAL_FREE_DELIVERY_MATERIAL": _cp_env(
                            "CP_LOCAL_FREE_DELIVERY_MATERIAL",
                            "cp.local_free_delivery_material",
                            "1",
                        ),
                        # L1/L2: frozen trees are capacity floors (appendAfter);
                        # do not re-encode their material as lockedMaterial or
                        # later insert slots prove INFEASIBLE. Set 0 for strict.
                        "CP_LOCAL_SKIP_FROZEN_MATERIAL": _cp_env(
                            "CP_LOCAL_SKIP_FROZEN_MATERIAL",
                            "cp.local_skip_frozen_material",
                            "1",
                        ),
                        # Extra seconds past latest frozen use + hot durations
                        # so a new lot can start after a full util window.
                        "CP_LOCAL_INSERT_SLACK_SECONDS": _cp_env(
                            "CP_LOCAL_INSERT_SLACK_SECONDS",
                            "cp.local_insert_slack_seconds",
                            "86400",
                        ),
                        # When local repair is on, default-enable change-rate
                        # + FAS seed anchor unless explicitly set to 0.
                        "CP_LOCAL_CHANGE_RATE": _cp_env(
                            "CP_LOCAL_CHANGE_RATE",
                            "cp.local_change_rate",
                            "1",
                        ),
                        # L2 neighbor links: equipment/mold only by default.
                        "CP_LOCAL_L2_INCLUDE_PERSONNEL": _cp_env(
                            "CP_LOCAL_L2_INCLUDE_PERSONNEL",
                            "cp.local_l2_include_personnel",
                            "0",
                        ),
                        # Soft cap: L2 may add at most factor × L1 roots.
                        "CP_LOCAL_L2_MAX_FACTOR": _cp_env(
                            "CP_LOCAL_L2_MAX_FACTOR",
                            "cp.local_l2_max_factor",
                            "5",
                        ),
                        "CP_LOCAL_AUTO_MAX_ROOTS": _cp_env(
                            "CP_LOCAL_AUTO_MAX_ROOTS",
                            "cp.local_auto_max_roots",
                            "1500",
                        ),
                        "CP_LOCAL_AUTO_MAX_TASKS": _cp_env(
                            "CP_LOCAL_AUTO_MAX_TASKS",
                            "cp.local_auto_max_tasks",
                            "2500",
                        ),
                        "CP_LOCAL_AUTO_MAX_VARS": _cp_env(
                            "CP_LOCAL_AUTO_MAX_VARS",
                            "cp.local_auto_max_vars",
                            "60000",
                        ),
                        # Keep the supply baseline unless CP covers it. Name is
                        # historical (MRP); applies equally to an FAS baseline.
                        "CP_PROTECT_MRP_BASELINE": (
                            "0" if generate_candidates else "1"
                        ),
                        "CP_BASELINE_LOCKED": (
                            "1" if generate_candidates and not all_candidates_variable else "0"
                        ),
                        "CP_BASELINE_VARIABLE": (
                            "1" if generate_candidates and all_candidates_variable else "0"
                        ),
                        "CP_KEEP_MRP": (
                            "1" if all_candidates_variable or not generate_candidates else "0"
                        ),
                    }
                    # Do not freeze FAS lots past the CP window. All proposed
                    # lots stay movable; throughput may drop some, and
                    # CP_KEEP_MRP keeps the FAS baseline when CP covers less.
                    previous_cp_environment = {
                        key: os.environ.get(key) for key in cp_environment
                    }
                    os.environ.update(cp_environment)
                    logger.info(
                        "%s (%s, horizon %s days, objective %s, "
                        "throughput=%s, lot_split=%s)",
                        (
                            "Running CP-SAT local reschedule on existing plan"
                            if local_skip_supply
                            else "Running CP-SAT after %s"
                            % ("FAS" if is_fas else "MRP")
                        ),
                        cp_environment["CP_PLANNING_MODE"],
                        cp_environment["CP_HORIZON_DAYS"],
                        cp_environment["CP_OBJECTIVE_METHOD"],
                        cp_environment["CP_THROUGHPUT_OBJECTIVE"],
                        cp_environment["CP_LOT_SPLIT"],
                    )
                    try:
                        # Default: single CP solve.
                        # CP_PARTITION=1 → workshop-parallel run_cp_partitioned.
                        # CP_ALNS=1 → ALNS lot structure; CP workers stay
                        # partitioned (seed / LNS / final all use partition).
                        use_alns = os.environ.get(
                            "CP_ALNS", ""
                        ).lower() in ("1", "true", "yes")
                        use_partition = os.environ.get(
                            "CP_PARTITION", ""
                        ).lower() in ("1", "true", "yes")
                        local_enabled = cp_environment[
                            "CP_LOCAL_RESCHEDULE"
                        ].lower() in ("1", "true", "yes", "on")
                        local_level = cp_environment[
                            "CP_LOCAL_RESCHEDULE_LEVEL"
                        ].strip().lower()
                        if candidate_expansion_blocked:
                            ccAPPS.cp_mark_candidate_no_supply()
                        elif use_alns:
                            # Ensure ALNS CP workers see partition mode.
                            os.environ.setdefault("CP_PARTITION", "1")
                            logger.info(
                                "CP entry: run_cp_alns (partitioned CP workers)"
                            )
                            ccAPPS.run_cp_alns()
                        elif use_partition:
                            logger.info("CP entry: run_cp_partitioned")
                            ccAPPS.run_cp_partitioned()
                        else:
                            if local_enabled and local_level == "auto":
                                # Smallest repair first. Stop when CP has a
                                # solution that clears the trigger. Capacity is
                                # hard inside CP; local_ok judges the trigger.
                                ccAPPS.cp_local_snapshot()
                                try:
                                    for attempt_level in ("1", "2", "3"):
                                        os.environ[
                                            "CP_LOCAL_RESCHEDULE_LEVEL"
                                        ] = attempt_level
                                        logger.info(
                                            "CP local cascade: trying level %s",
                                            attempt_level,
                                        )
                                        ccAPPS.run_cp()
                                        applied = int(
                                            ccAPPS.cp_last_run_applied()
                                        )
                                        local_ok = bool(
                                            ccAPPS.cp_last_local_ok()
                                        )
                                        terminal_stage = str(
                                            ccAPPS.cp_last_run_stage()
                                        )
                                        delete_complete = (
                                            terminal_stage
                                            == "local_delete_complete"
                                        )
                                        if local_ok and (
                                            applied or delete_complete
                                        ):
                                            logger.info(
                                                "CP local cascade: level %s ok "
                                                "(%s assignments, trigger cleared, "
                                                "stage=%s)",
                                                attempt_level,
                                                applied,
                                                terminal_stage,
                                            )
                                            break
                                        if (
                                            terminal_stage
                                            == "local_delete_not_applied"
                                        ):
                                            logger.warning(
                                                "CP local cascade stopped: "
                                                "delete trigger demand still "
                                                "exists"
                                            )
                                            break
                                        if applied and not local_ok:
                                            logger.info(
                                                "CP local cascade: level %s "
                                                "applied %s but trigger "
                                                "unresolved; restoring baseline",
                                                attempt_level,
                                                applied,
                                            )
                                        else:
                                            logger.info(
                                                "CP local cascade: level %s "
                                                "produced no solution; "
                                                "restoring baseline",
                                                attempt_level,
                                            )
                                        ccAPPS.cp_local_restore()
                                finally:
                                    ccAPPS.cp_local_snapshot_clear()
                            else:
                                logger.info("CP entry: run_cp (no partition)")
                                # Fixed L1/L2/L3: snapshot so a trigger-failed
                                # apply can be undone (cascade already does).
                                # Snapshot any fixed-level order local run
                                # (insert / change / delete) so a failed
                                # trigger can restore in-memory plan.
                                fixed_local_guard = (
                                    local_enabled
                                    and local_level in ("1", "2", "3", "custom")
                                )
                                if fixed_local_guard:
                                    ccAPPS.cp_local_snapshot()
                                try:
                                    ccAPPS.run_cp()
                                    if fixed_local_guard and not bool(
                                        ccAPPS.cp_last_local_ok()
                                    ):
                                        logger.info(
                                            "CP local fixed level=%s trigger "
                                            "unresolved; restoring snapshot",
                                            local_level,
                                        )
                                        ccAPPS.cp_local_restore()
                                finally:
                                    if fixed_local_guard:
                                        ccAPPS.cp_local_snapshot_clear()
                        # Report the in-memory CP result before noexport can
                        # discard it. Invalid-data master-data warnings are
                        # excluded; these counters describe schedule quality.
                        cp_problem_counts = {}
                        cp_problem_details = []
                        for problem in ccAPPS.problems():
                            name = str(problem.name)
                            if name == "invalid data":
                                continue
                            cp_problem_counts[name] = (
                                cp_problem_counts.get(name, 0) + 1
                            )
                            if name in ("material shortage", "overload"):
                                owner = getattr(problem, "owner", None)
                                cp_problem_details.append(
                                    {
                                        "type": name,
                                        "owner": str(
                                            getattr(owner, "name", owner)
                                        ),
                                        "start": str(
                                            getattr(problem, "start", "")
                                        ),
                                        "end": str(
                                            getattr(problem, "end", "")
                                        ),
                                        "description": str(
                                            getattr(
                                                problem, "description", ""
                                            )
                                        ),
                                    }
                                )
                        # Export/truncation rebuilds native Problem objects.
                        # Python loop locals otherwise retain the final native
                        # problem/owner past its C++ deletion and corrupt the
                        # interpreter during process teardown.
                        problem = owner = None
                        cp_stage = str(ccAPPS.cp_last_run_stage())
                        logger.info(
                            "CP result validation: applied=%s local_ok=%s "
                            "stage=%s "
                            "schedule_problems=%s",
                            int(ccAPPS.cp_last_run_applied()),
                            bool(ccAPPS.cp_last_local_ok()),
                            cp_stage,
                            cp_problem_counts or {},
                        )
                        if cp_stage == "preflight_blocked":
                            try:
                                conflicts = str(
                                    ccAPPS.cp_last_preflight_conflicts() or ""
                                )
                            except Exception:
                                conflicts = ""
                            logger.error(
                                "CP preflight hard blocked; conflicts: %s",
                                conflicts or "(none mapped)",
                            )
                        if cp_problem_details:
                            logger.info(
                                "CP result problem details: %s",
                                cp_problem_details,
                            )
                        # Persist level_used + outcome for API / task status
                        # only on real reschedule runs (not full CP).
                        if local_enabled and local_skip_supply:
                            local_outcome = {}
                            try:
                                raw_outcome = ccAPPS.cp_last_local_outcome()
                                if isinstance(raw_outcome, dict):
                                    local_outcome = dict(raw_outcome)
                            except Exception:
                                local_outcome = {
                                    "applied": int(
                                        ccAPPS.cp_last_run_applied()
                                    ),
                                    "local_ok": bool(
                                        ccAPPS.cp_last_local_ok()
                                    ),
                                    "stage": str(ccAPPS.cp_last_run_stage()),
                                }
                            local_outcome["status"] = bool(
                                local_outcome.pop("local_ok", False)
                            )
                            local_outcome["schedule_problems"] = (
                                cp_problem_counts or {}
                            )
                            try:
                                import json as _json

                                Parameter.objects.using(
                                    database
                                ).update_or_create(
                                    name="cp.local_last_outcome",
                                    defaults={
                                        "value": _json.dumps(
                                            local_outcome, ensure_ascii=False
                                        )
                                    },
                                )
                                Parameter.objects.using(
                                    database
                                ).update_or_create(
                                    name="local.last_outcome",
                                    defaults={
                                        "value": _json.dumps(
                                            {**local_outcome, "solver": "cp"},
                                            ensure_ascii=False,
                                        )
                                    },
                                )
                            except Exception:
                                logger.exception(
                                    "Failed to persist cp.local_last_outcome"
                                )
                            task_id = os.environ.get("CCAPPS_TASKID")
                            if task_id:
                                try:
                                    from ccAPPSdb.execute.models import Task

                                    msg = "local_outcome=" + _json.dumps(
                                        local_outcome,
                                        ensure_ascii=False,
                                        separators=(",", ":"),
                                    )
                                    Task.objects.using(database).filter(
                                        id=int(task_id)
                                    ).update(message=msg)
                                except Exception:
                                    logger.exception(
                                        "Failed to write local outcome on task"
                                    )
                    finally:
                        # Roll back insert candidates when CP exported nothing
                        # OR the local trigger stayed unresolved (e.g. late
                        # fill). applied>0 alone is not success for local
                        # order insert/change.
                        cp_applied = 0
                        cp_local_ok = False
                        try:
                            cp_applied = int(ccAPPS.cp_last_run_applied())
                        except Exception:
                            cp_applied = 0
                        try:
                            cp_local_ok = bool(ccAPPS.cp_last_local_ok())
                        except Exception:
                            cp_local_ok = False
                        local_order_insert = bool(
                            local_skip_supply
                            and local_includes_order
                            and insert_only
                        )
                        # Local path: keep candidates only when trigger ok.
                        # Non-local CP: keep whenever applied > 0.
                        keep_candidates = (
                            candidate_solver is not None
                            and cp_applied > 0
                            and (cp_local_ok or not local_skip_supply)
                        )
                        if (
                            candidate_solver is not None
                            and expanded_plan_count
                            and not keep_candidates
                        ):
                            candidate_solver.rollback()
                            # A failed pure insert has no historical tree, so
                            # remove any candidate remnants. A failed change
                            # must retain its historical tree and restore the
                            # temporarily exposed delivery placeholder.
                            if pure_insert_names:
                                cleared = 0
                                for _ in range(8):
                                    roots = []
                                    for plan in list(ccAPPS.operationplans()):
                                        dem = getattr(plan, "demand", None)
                                        if (
                                            dem is None
                                            or dem.name not in pure_insert_names
                                        ):
                                            continue
                                        root = plan
                                        while (
                                            getattr(root, "owner", None)
                                            is not None
                                        ):
                                            root = root.owner
                                        if root not in roots:
                                            roots.append(root)
                                    if not roots:
                                        break
                                    for root in roots:
                                        try:
                                            root.delete()
                                            cleared += 1
                                        except Exception:
                                            pass
                                if cleared:
                                    logger.info(
                                        "CP local insert: cleared %s leftover "
                                        "plan root(s) after failed trigger",
                                        cleared,
                                    )
                            for plan, quantity in insert_delivery_placeholders:
                                try:
                                    demand = getattr(plan, "demand", None)
                                    if (
                                        demand is not None
                                        and demand.name in deleted_only
                                    ):
                                        plan.quantity = quantity
                                except Exception:
                                    pass
                            logger.info(
                                "Rolled back CP candidate expansion "
                                "(applied=%s local_ok=%s)",
                                cp_applied,
                                cp_local_ok,
                            )
                        elif keep_candidates:
                            logger.info(
                                "Kept CP candidate expansion (%s assignments applied)",
                                cp_applied,
                            )
                        # Failed local trigger: do not erase/export DB plan.
                        # In-memory restore already happened; keep prior DB plan.
                        if (
                            local_skip_supply
                            and Parameter.getValue(
                                "cp.local_reschedule", database, "0"
                            ).lower()
                            in ("1", "true", "yes", "on")
                            and not cp_local_ok
                        ):
                            os.environ["noexport"] = "1"
                            logger.info(
                                "CP local trigger unresolved "
                                "(applied=%s); skipping plan export "
                                "(noexport=1)",
                                cp_applied,
                            )
                        # CP partial export may delete unselected baseline trees.
                        # Never touch the pre-CP pointer list — those objects
                        # can already be freed. Demote whatever approved work
                        # is still alive after the solve.
                        try:
                            for plan in ccAPPS.operationplans():
                                if plan.status == "approved":
                                    plan.status = "proposed"
                        except Exception:
                            pass
                        for key, value in previous_cp_environment.items():
                            if value is None:
                                os.environ.pop(key, None)
                            else:
                                os.environ[key] = value
                        # Dedicated reschedule API calls are one-shot.
                        # Clear their persistent trigger after this engine run
                        # so a later ordinary runplan can't accidentally reuse
                        # the previous local scope. Keep local_last_outcome for
                        # task/API observability.
                        if (
                            local_skip_supply
                            and (
                                Parameter.getValue(
                                    "local.one_shot", database, "0"
                                ).lower()
                                in ("1", "true", "yes", "on")
                                or Parameter.getValue(
                                    "cp.local_one_shot", database, "0"
                                ).lower()
                                in ("1", "true", "yes", "on")
                            )
                        ):
                            previous_solver = Parameter.getValue(
                                "local.previous_solver", database, ""
                            ).strip()
                            if previous_solver:
                                Parameter.objects.using(database).update_or_create(
                                    name="plan.solver",
                                    defaults={"value": previous_solver},
                                )
                            for name, value in {
                                "plan.skip_supply": "0",
                                "local.reschedule": "0",
                                "local.insert_candidates": "0",
                                "local.one_shot": "0",
                                "local.new_demands": "",
                                "local.deleted_demands": "",
                                "local.changed_materials": "",
                                "local.changed_resources": "",
                                "local.scope_base": "",
                                "local.scope_relax": "",
                                "local.previous_solver": "",
                                "cp.local_reschedule": "0",
                                "cp.local_skip_supply": "0",
                                "cp.local_insert_candidates": "0",
                                "cp.local_one_shot": "0",
                                "cp.local_new_demands": "",
                                "cp.local_deleted_demands": "",
                                "cp.local_changed_materials": "",
                                "cp.local_changed_resources": "",
                                "cp.local_scope_base": "",
                                "cp.local_scope_relax": "",
                            }.items():
                                Parameter.objects.using(database).update_or_create(
                                    name=name, defaults={"value": value}
                                )
                            logger.info(
                                "Cleared one-shot reschedule trigger"
                            )
                except Exception:
                    logger.error(
                        "Exception caught during CP-SAT schedule",
                        exc_info=True,
                    )
                    raise
        finally:
            ccAPPS.settings.loglimit = 0
        thread = Thread(target=ccAPPS.printsize)
        thread.start()
        thread.join(timeout=30)
        if thread.is_alive():
            logger.warning(
                "Skipping post-solve printsize(): call timed out after 30 seconds"
            )


@PlanTaskRegistry.register
class RunVNSOptimization(PlanTask):
    description = "Run VNS optimization"
    sequence = 401.9

    @classmethod
    def getWeight(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        # VNS is retained in the codebase but isn't an enabled planning mode.
        return -1

    @classmethod
    def run(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        import ccAPPS
        from threading import Thread

        logger.info("Running VNS optimization...")
        vns_timeout = int(os.environ.get("VNS_TIMEOUT_SECONDS", "300"))
        worker_error = []

        def run_vns_entry():
            try:
                result = ccAPPS.run_vns(database=database, dry_run=False)
                logger.info(
                    "VNS completed: HPRC=%s LPRC=%s PCC=%s batch=%s objective=%s",
                    result["optimized_score"]["hprc"],
                    result["optimized_score"]["lprc"],
                    result["optimized_score"]["pcc"],
                    result["optimized_score"]["batch"],
                    result["optimized_score"]["objective"],
                )
                if result.get("summary"):
                    summary = result["summary"]
                    logger.info(
                        "VNS wrote sequence: anchor=%s start=%s source_start=%s end=%s paint=%s assembly=%s upstream=%s",
                        summary.get("anchor"),
                        summary.get("start"),
                        summary.get("source_start"),
                        summary.get("end"),
                        summary.get("paint"),
                        summary.get("assembly"),
                        summary.get("upstream"),
                    )
            except Exception as exc:
                worker_error.append(exc)
                logger.exception("VNS optimization failed")

        worker = Thread(target=run_vns_entry)
        worker.start()
        worker.join(timeout=vns_timeout)
        if worker.is_alive():
            logger.warning("VNS timed out after %s seconds", vns_timeout)
            return
        if worker_error:
            raise worker_error[0]
        logger.info("VNS optimization completed")


@PlanTaskRegistry.register
class GenerateGAFJSPMaterialFlow(PlanTask):
    description = "Generate GA FJSP material flow"
    sequence = (401, "export1", 2.5)
    export = True

    @classmethod
    def getWeight(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        if "supply" not in os.environ or "noexport" in os.environ:
            return -1
        try:
            from ccAPPSdb.input.models import Item

            return (
                1
                if Item.objects.using(database)
                .filter(name__regex=r"^J[0-9]{3}_WIP[0-9]{2}$")
                .exists()
                else -1
            )
        except Exception:
            logger.exception("Failed to inspect GA FJSP operationplans")
            return -1

    @classmethod
    def run(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        from decimal import Decimal
        from django.db import transaction
        from ccAPPSdb.input.models import OperationPlan
        from ccAPPSdb.input.models.operationplan import OperationPlanMaterial

        location = Parameter.getValue("ga.fjsp.location", database, "FJSP Factory")
        op_re = re.compile(r"^(J[0-9]{3})_O([0-9]{2})_")

        routing_plans = list(
            OperationPlan.objects.using(database)
            .filter(operation__name__regex=r"^J[0-9]{3}_ROUTING$")
            .order_by("operation_id")
        )
        if not routing_plans:
            logger.info("No GA FJSP routing operationplans found")
            return

        with transaction.atomic(using=database):
            OperationPlanMaterial.objects.using(database).filter(
                operationplan__operation__name__regex=r"^J[0-9]{3}_"
            ).delete()

            records = []
            for routing in routing_plans:
                job = routing.operation_id[:4]
                stage_plans = []
                for child in (
                    OperationPlan.objects.using(database)
                    .filter(owner=routing)
                    .select_related("operation")
                    .order_by("startdate", "enddate", "reference")
                ):
                    match = op_re.match(child.operation_id or "")
                    if not match or match.group(1) != job:
                        continue
                    op_idx = int(match.group(2))
                    if child.operation_id.endswith("_ALT") or child.type == "WO":
                        stage_plans.append((op_idx, child))

                seen = set()
                ordered_stages = []
                for op_idx, child in sorted(
                    stage_plans,
                    key=lambda item: (
                        item[0],
                        item[1].startdate or datetime.min,
                        item[1].reference,
                    ),
                ):
                    if op_idx in seen:
                        continue
                    seen.add(op_idx)
                    ordered_stages.append((op_idx, child))

                if not ordered_stages:
                    continue

                last_idx = max(op_idx for op_idx, _ in ordered_stages)
                for op_idx, opplan in ordered_stages:
                    input_item = f"{job}_RM" if op_idx == 1 else f"{job}_WIP{op_idx - 1:02d}"
                    output_item = f"{job}_FG" if op_idx == last_idx else f"{job}_WIP{op_idx:02d}"
                    records.append(
                        OperationPlanMaterial(
                            operationplan=opplan,
                            item_id=input_item,
                            location_id=location,
                            quantity=Decimal("-1"),
                            flowdate=opplan.startdate,
                            status=opplan.status or "proposed",
                        )
                    )
                    records.append(
                        OperationPlanMaterial(
                            operationplan=opplan,
                            item_id=output_item,
                            location_id=location,
                            quantity=Decimal("1"),
                            flowdate=opplan.enddate,
                            status=opplan.status or "proposed",
                        )
                    )

                records.append(
                    OperationPlanMaterial(
                        operationplan=routing,
                        item_id=f"{job}_FG",
                        location_id=location,
                        quantity=Decimal("1"),
                        flowdate=routing.enddate,
                        status=routing.status or "proposed",
                    )
                )

            if records:
                OperationPlanMaterial.objects.using(database).bulk_create(
                    records, batch_size=1000
                )

        logger.info(
            "Generated GA FJSP material flow: jobs=%s records=%s",
            len(routing_plans),
            len(records),
        )




@PlanTaskRegistry.register
class GenerateQuote(PlanTask):
    description = "Generate quote from plan costs"
    sequence = 401.95
    label = (
        "quote",
        _("生成报价"),
        _("计算询价订单的原材料、资源和工艺成本，生成报价明细。"),
    )

    @classmethod
    def getWeight(cls, **kwargs):
        if "quote" in os.environ:
            return 1
        return -1

    @classmethod
    def run(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        import json
        from collections import defaultdict
        from decimal import Decimal, ROUND_HALF_UP
        from ccAPPSdb.input.models import Demand, OperationPlan, Item, ItemSupplier, Customer, QuoteHistory
        from ccAPPSdb.input.models.operationplan import OperationPlanMaterial, OperationPlanResource

        logger.info("Generating quotes for quotation demands...")

        quote_demands = Demand.objects.using(database).filter(quote_enabled=True)
        if not quote_demands.exists():
            logger.info("No quotation demands found.")
            Parameter.objects.using(database).filter(pk="quote_data").delete()
            return

        # Read global quote configuration Parameters
        try:
            global_profit_margin = Decimal(
                Parameter.getValue("quote.profit_margin", database, "0")
            )
        except Exception:
            global_profit_margin = Decimal("0")
        try:
            global_overhead_rate = Decimal(
                Parameter.getValue("quote.overhead_rate", database, "0")
            )
        except Exception:
            global_overhead_rate = Decimal("0")
        try:
            global_scrap_rate = Decimal(
                Parameter.getValue("quote.material_scrap_rate", database, "0")
            )
        except Exception:
            global_scrap_rate = Decimal("0")
        quote_calculation_mode = Parameter.getValue(
            "quote.calculation_mode", database, "bom_routing"
        ).lower()
        if quote_calculation_mode not in ("bom_routing", "mrp"):
            logger.warning(
                "Unknown quote.calculation_mode '%s'; using bom_routing",
                quote_calculation_mode,
            )
            quote_calculation_mode = "bom_routing"
        quote_history_calibration = Parameter.getValue(
            "quote.history_calibration", database, "true"
        ).lower() == "true"
        operation_cost_includes_resources = Parameter.getValue(
            "quote.operation_cost_includes_resources", database, "false"
        ).lower() == "true"
        try:
            quote_validity_days = int(
                Parameter.getValue("quote.validity_days", database, "30")
            )
        except (TypeError, ValueError):
            quote_validity_days = 30
        try:
            quote_tax_rate = Decimal(
                Parameter.getValue("quote.tax_rate", database, "0")
            )
        except Exception:
            quote_tax_rate = Decimal("0")
        try:
            rounding_precision = max(0, int(
                Parameter.getValue("quote.rounding_precision", database, "2")
            ))
        except (TypeError, ValueError):
            rounding_precision = 2
        try:
            rounding_increment = Decimal(
                Parameter.getValue("quote.rounding_increment", database, "0.01")
            )
            if rounding_increment <= 0:
                raise ValueError
        except Exception:
            rounding_increment = Decimal("0.01")
        rounding_mode = Parameter.getValue(
            "quote.rounding_mode", database, "half_up"
        ).lower()
        if rounding_mode != "half_up":
            logger.warning("Unknown quote.rounding_mode '%s'; using half_up", rounding_mode)
            rounding_mode = "half_up"
        supplier_selection = Parameter.getValue(
            "quote.supplier_selection", database, "priority"
        ).lower()
        if supplier_selection not in ("priority", "lowest_cost"):
            logger.warning(
                "Unknown quote.supplier_selection '%s'; using priority", supplier_selection
            )
            supplier_selection = "priority"
        batch_cost_allocation = Parameter.getValue(
            "quote.batch_cost_allocation", database, "inventory_credit"
        ).lower()
        if batch_cost_allocation not in ("inventory_credit", "full_batch"):
            logger.warning(
                "Unknown quote.batch_cost_allocation '%s'; using inventory_credit",
                batch_cost_allocation,
            )
            batch_cost_allocation = "inventory_credit"

        def charge_quantity(required_quantity, minimum=None, multiple=None):
            """Apply a minimum quantity followed by the batch multiple."""
            quantity = abs(required_quantity or Decimal("0"))
            if minimum is not None and minimum > quantity:
                quantity = minimum
            if multiple is not None and multiple > 0:
                quantity = (
                    quantity / multiple
                ).to_integral_value(rounding="ROUND_CEILING") * multiple
            return quantity

        quote_date_filter = lambda quote_date: (
            (Q(effective_start__isnull=True) | Q(effective_start__lte=quote_date))
            & (Q(effective_end__isnull=True) | Q(effective_end__gte=quote_date))
        )

        quote_results = []
        quote_history_mode = cls._get_quote_history_mode(database)

        for demand in quote_demands:
            # A sent or confirmed price is a commercial commitment. Keep its
            # calculation snapshot intact until a user explicitly returns it to draft.
            if (demand.quote or {}).get("quote_status") in ("sent", "confirmed"):
                quote_results.append(demand.name)
                continue
            bom_costs = None
            mrp_snapshot = {"operations": [], "materials": [], "resources": [],
                            "supplier_selection": supplier_selection,
                            "batch_cost_allocation": batch_cost_allocation,
                            "inventory_cost_policy": "inventory_standard_cost"}
            batch_inventory_value = Decimal("0")
            if quote_calculation_mode == "bom_routing":
                # Quote directly from BOM and routing, without creating MRP plans.
                bom_costs = cls._calculate_bom_route_costs(
                    database, demand, supplier_selection=supplier_selection,
                    batch_cost_allocation=batch_cost_allocation,
                )
                if bom_costs is None:
                    logger.warning("No BOM or routing found for demand %s, skipping", demand.name)
                    if demand.quote:
                        demand.quote = {}
                        demand.save(using=database, update_fields=["quote"])
                    continue
                deliveries = []
            else:
                # MRP mode uses the delivered order's generated upstream plan chain.
                deliveries = list(
                    OperationPlan.objects.using(database)
                    .filter(demand_id=demand.name, type="DLVR")
                )
                if not deliveries:
                    sub_demands = Demand.objects.using(database).filter(
                        name__startswith=demand.name + "-"
                    )
                    for sub in sub_demands:
                        deliveries.extend(
                            OperationPlan.objects.using(database).filter(
                                demand_id=sub.name, type="DLVR"
                            )
                        )
                if not deliveries:
                    logger.warning("No MRP delivery found for demand %s, skipping", demand.name)
                    if demand.quote:
                        demand.quote = {}
                        demand.save(using=database, update_fields=["quote"])
                    continue

            # Collect and aggregate all upstream plans from all deliveries
            material_cost = Decimal("0")
            resource_cost = Decimal("0")
            operation_cost = Decimal("0")
            transport_cost = Decimal("0")
            material_bd = defaultdict(Decimal)
            resource_bd = defaultdict(Decimal)
            operation_bd = defaultdict(Decimal)
            transport_bd = defaultdict(Decimal)
            seen_plan_refs = {}

            for delivery in deliveries:
                all_plans = cls._collect_upstream_plans(delivery, database)

                for entry in all_plans:
                    if len(entry) == 3:
                        plan, required_qty, stock_item = entry
                    else:
                        plan, required_qty = entry
                        stock_item = None
                    if plan is None:
                        item = Item.objects.using(database).filter(name=stock_item).first()
                        if item:
                            mc = (item.cost or Decimal("0")) * abs(required_qty)
                            material_cost += mc
                            material_bd[item.name] += mc
                        continue
                    if plan.type == "STCK":
                        if plan.item:
                            mc = (plan.item.cost or Decimal("0")) * abs(required_qty)
                            material_cost += mc
                            material_bd[plan.item.name] += mc
                            mrp_snapshot["materials"].append({
                                "item": plan.item.name,
                                "required_quantity": str(abs(required_qty)),
                                "charged_quantity": str(abs(required_qty)),
                                "allocated_quantity": str(abs(required_qty)),
                                "unit_cost": str(plan.item.cost or Decimal("0")),
                                "cost_source": "inventory_standard_cost",
                                "supplier": None,
                            })
                        continue
                    plan_qty = plan.quantity or Decimal("1")
                    if plan_qty == 0:
                        continue
                    share = min(Decimal("1"), abs(required_qty) / abs(plan_qty))
                    old_share = seen_plan_refs.get(plan.reference, Decimal("0"))
                    share = min(share, Decimal("1") - old_share)
                    if share <= 0:
                        continue
                    seen_plan_refs[plan.reference] = old_share + share
                    qty = abs(plan_qty) * share

                    if plan.type in ("MO", "WO"):
                        charged_qty = charge_quantity(
                            qty,
                            plan.operation.sizeminimum if plan.operation else None,
                            plan.operation.sizemultiple if plan.operation else None,
                        )
                        # --- Operation cost ---
                        allocated_qty = qty if batch_cost_allocation == "inventory_credit" else charged_qty
                        if plan.operation and plan.operation.cost:
                            oc = plan.operation.cost * allocated_qty
                            operation_cost += oc
                            operation_bd[str(plan.operation.name)] += oc
                            batch_inventory_value += plan.operation.cost * (charged_qty - allocated_qty)
                        if plan.operation:
                            mrp_snapshot["operations"].append({
                                "operation": plan.operation.name,
                                "required_quantity": str(qty),
                                "charged_quantity": str(charged_qty),
                                "allocated_quantity": str(allocated_qty),
                                "unit_cost": str(plan.operation.cost or Decimal("0")),
                                "location": plan.operation.location_id,
                            })

                        # --- Resource cost ---
                        plan_resources = OperationPlanResource.objects.using(database).filter(
                            operationplan_id=plan.reference
                        )
                        # A plan's calendar span includes waiting and non-working
                        # time. Price resources from the routing's standard load.
                        standard_seconds = Decimal("0")
                        if plan.operation and plan.operation.duration:
                            standard_seconds += Decimal(
                                str(plan.operation.duration.total_seconds())
                            )
                        if plan.operation and plan.operation.duration_per:
                            standard_seconds += Decimal(
                                str(plan.operation.duration_per.total_seconds())
                            ) * charged_qty
                        standard_hours = standard_seconds / Decimal("3600")
                        for pr in plan_resources:
                            if operation_cost_includes_resources:
                                break
                            if pr.resource and pr.resource.cost:
                                allocation_ratio = allocated_qty / charged_qty if charged_qty else Decimal("0")
                                rc = (
                                    pr.resource.cost
                                    * (pr.quantity or Decimal("1"))
                                    * standard_hours
                                    * allocation_ratio
                                )
                                resource_cost += rc
                                resource_bd[str(pr.resource.name)] += rc
                                batch_inventory_value += (
                                    pr.resource.cost
                                    * (pr.quantity or Decimal("1"))
                                    * standard_hours
                                    * (Decimal("1") - allocation_ratio)
                                )

                        # Material value is counted only at PO/STCK leaves below.
                        # Counting consumed MO material here would duplicate its
                        # upstream purchase, inventory, or manufacturing supply.

                    elif plan.type == "PO":
                        # Direct PO cost (in case POs are in the upstream chain)
                        unit_cost = None
                        item_supplier = None
                        if plan.item:
                            try:
                                candidates = ItemSupplier.objects.using(database).filter(
                                    quote_date_filter(demand.due or timezone.now()),
                                    item_id=plan.item.name,
                                    location_id=demand.location_id,
                                )
                                if not candidates.exists():
                                    candidates = ItemSupplier.objects.using(database).filter(
                                        quote_date_filter(demand.due or timezone.now()),
                                        item_id=plan.item.name, location__isnull=True,
                                    )
                                if supplier_selection == "lowest_cost":
                                    candidates = candidates.annotate(
                                        landed_unit_cost=F("cost") + Coalesce(
                                            F("transport_cost_per_unit"), Value(0),
                                            output_field=DecimalField(),
                                        )
                                    ).order_by(
                                        F("landed_unit_cost").asc(nulls_last=True), "priority", "id"
                                    )
                                else:
                                    candidates = candidates.order_by("priority", "id")
                                item_supplier = candidates.first()
                                unit_cost = item_supplier.cost if item_supplier else None
                            except ItemSupplier.DoesNotExist:
                                pass
                            if unit_cost is None:
                                unit_cost = plan.item.cost or Decimal("0")
                        charged_qty = charge_quantity(
                            qty,
                            item_supplier.sizeminimum if item_supplier else None,
                            item_supplier.sizemultiple if item_supplier else None,
                        )
                        allocated_qty = qty if batch_cost_allocation == "inventory_credit" else charged_qty
                        mc = (unit_cost or Decimal("0")) * allocated_qty
                        material_cost += mc
                        material_bd[str(plan.item.name if plan.item else "unknown")] += mc
                        batch_inventory_value += (unit_cost or Decimal("0")) * (charged_qty - allocated_qty)
                        # Transport cost from supplier
                        if item_supplier and item_supplier.transport_cost_per_unit:
                            tc = item_supplier.transport_cost_per_unit * allocated_qty
                            transport_cost += tc
                            batch_inventory_value += item_supplier.transport_cost_per_unit * (charged_qty - allocated_qty)
                            transport_bd[str(plan.supplier.name) if plan.supplier else str(plan.item.name if plan.item else "unknown")] += tc
                        mrp_snapshot["materials"].append({
                            "item": plan.item.name if plan.item else None,
                            "required_quantity": str(qty), "charged_quantity": str(charged_qty),
                            "allocated_quantity": str(allocated_qty),
                            "unit_cost": str(unit_cost or Decimal("0")),
                            "supplier": item_supplier.supplier_id if item_supplier else None,
                            "supplier_selection": supplier_selection,
                            "cost_source": "supplier_purchase_cost" if item_supplier else "item_standard_cost",
                        })

                    elif plan.type == "DO":
                        # Distribution orders move inventory between locations.
                        # The purchased or manufactured cost is captured by the
                        # upstream PO/MO, so don't count the item value again here.
                        pass

            if bom_costs:
                material_cost += bom_costs["material_cost"]
                resource_cost += bom_costs["resource_cost"]
                operation_cost += bom_costs["operation_cost"]
                transport_cost += bom_costs["transport_cost"]
                batch_inventory_value += bom_costs["batch_inventory_value"]
                for name, value in bom_costs["material_breakdown"].items():
                    material_bd[name] += value
                for name, value in bom_costs["resource_breakdown"].items():
                    resource_bd[name] += value
                for name, value in bom_costs["operation_breakdown"].items():
                    operation_bd[name] += value
                for name, value in bom_costs["transport_breakdown"].items():
                    transport_bd[name] += value

            # --- Apply scrap rate to material cost ---
            # Item-level scrap rate overrides global
            item_scrap_rate = global_scrap_rate
            if demand.item and demand.item.scrap_rate is not None:
                item_scrap_rate = demand.item.scrap_rate
            scrap_cost = material_cost * (item_scrap_rate / Decimal("100"))
            material_with_scrap = material_cost + scrap_cost

            # --- Direct cost subtotal ---
            direct_subtotal = material_with_scrap + resource_cost + operation_cost

            # --- Overhead (indirect cost allocation) ---
            overhead_cost = direct_subtotal * (global_overhead_rate / Decimal("100"))

            # --- Cost total before profit ---
            cost_total = direct_subtotal + overhead_cost

            # --- Profit margin ---
            # Customer-level margin overrides global
            margin_rate = global_profit_margin
            margin_label = f"{global_profit_margin}%"
            if demand.customer and demand.customer.margin_rate is not None:
                margin_rate = demand.customer.margin_rate
                margin_label = f"{demand.customer.margin_rate}% (customer)"
            profit_amount = cost_total * (margin_rate / Decimal("100"))

            # --- Price before transport ---
            price_before_transport = cost_total + profit_amount

            # --- Customer discount ---
            discount_rate = Decimal("0")
            discount_label = "0%"
            if demand.customer and demand.customer.discount_rate is not None:
                discount_rate = demand.customer.discount_rate
                discount_label = f"{discount_rate}%"
            discount_amount = (price_before_transport + transport_cost) * (discount_rate / Decimal("100"))

            # --- Final total ---
            total = price_before_transport + transport_cost - discount_amount
            tax_amount = total * (quote_tax_rate / Decimal("100"))
            total_with_tax = total + tax_amount
            historical_calibration = (
                cls._calculate_history_calibration(
                    database, demand, total_with_tax,
                    tax_rate=quote_tax_rate,
                    calculation_mode=quote_calculation_mode,
                    batch_cost_allocation=batch_cost_allocation,
                    supplier_selection=supplier_selection,
                )
                if quote_history_calibration
                else {"factor": Decimal("1"), "sample_count": 0, "samples": []}
            )
            historical_suggested_adjustment = total * (
                historical_calibration["factor"] - Decimal("1")
            )

            # Round only the displayed monetary values. Calculations retain full precision.
            def rd(val):
                rounded = (val / rounding_increment).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                ) * rounding_increment
                increment_places = max(0, -rounding_increment.as_tuple().exponent)
                places = max(rounding_precision, increment_places)
                return str(rounded.quantize(Decimal("1").scaleb(-places)))

            # Store enhanced quote on Demand.quote field
            quote_data = {
                "calculation_method": quote_calculation_mode,
                "customer": str(demand.customer.name) if demand.customer else "",
                "item": str(demand.item.name) if demand.item else "",
                "quantity": str(demand.quantity),
                "due": demand.due.strftime("%Y-%m-%d %H:%M") if demand.due else "",
                # Cost breakdown
                "material_cost": rd(material_cost),
                "scrap_rate_applied": f"{item_scrap_rate}%",
                "scrap_cost": rd(scrap_cost),
                "resource_cost": rd(resource_cost),
                "operation_cost": rd(operation_cost),
                "overhead_rate_applied": f"{global_overhead_rate}%",
                "overhead_cost": rd(overhead_cost),
                "transport_cost": rd(transport_cost),
                "batch_cost_allocation": batch_cost_allocation,
                "batch_inventory_value": rd(batch_inventory_value),
                "inventory_cost_policy": (
                    "inventory_standard_cost" if quote_calculation_mode == "mrp"
                    else "supplier_or_item_cost"
                ),
                # Pricing
                "subtotal_cost": rd(cost_total),
                "profit_margin_applied": margin_label,
                "profit_amount": rd(profit_amount),
                "discount_rate_applied": discount_label,
                "discount_amount": rd(discount_amount),
                "tax_rate_applied": f"{quote_tax_rate}%",
                "tax_amount": rd(tax_amount),
                "total_with_tax": rd(total_with_tax),
                "rounding": {
                    "precision": rounding_precision,
                    "increment": str(rounding_increment),
                    "mode": rounding_mode,
                },
                "history_sample_count": historical_calibration["sample_count"],
                "history_calibration_enabled": quote_history_calibration,
                "history_calibration_factor": rd(historical_calibration["factor"]),
                "history_suggested_total": rd(total + historical_suggested_adjustment),
                "history_suggested_adjustment": rd(historical_suggested_adjustment),
                "history_samples": historical_calibration["samples"],
                # Regeneration must not silently reset a quote already sent or confirmed.
                "quote_status": (demand.quote or {}).get("quote_status", "draft"),
                "quote_generated_at": timezone.now().strftime("%Y-%m-%d %H:%M:%S"),
                "quote_valid_until": (
                    timezone.now() + timedelta(days=max(0, quote_validity_days))
                ).strftime("%Y-%m-%d"),
                "quote_complete": bom_costs["complete"] if bom_costs else True,
                "quote_warnings": bom_costs["warnings"] if bom_costs else [],
                "cost_snapshot": bom_costs["snapshot"] if bom_costs else mrp_snapshot,
                "total_cost": rd(total),
                # Breakdowns
                "material_breakdown": {k: rd(v) for k, v in material_bd.items()},
                "scrap_breakdown": {k: rd(v * (item_scrap_rate / Decimal("100"))) for k, v in material_bd.items()},
                "resource_breakdown": {k: rd(v) for k, v in resource_bd.items()},
                "operation_breakdown": {k: rd(v) for k, v in operation_bd.items()},
                "transport_breakdown": {k: rd(v) for k, v in transport_bd.items()},
            }
            demand.quote = quote_data
            demand.save(using=database, update_fields=["quote"])

            try:
                previous_quote = (
                    QuoteHistory.objects.using(database)
                    .filter(demand_id=demand.name)
                    .order_by("-version")
                    .values_list("quote_data", flat=True)
                    .first()
                )
                if cls._quote_history_payload(previous_quote) == cls._quote_history_payload(quote_data):
                    quote_results.append(demand.name)
                    continue
                last_version = (
                    QuoteHistory.objects.using(database)
                    .filter(demand_id=demand.name)
                    .order_by("-version")
                    .values_list("version", flat=True)
                    .first()
                )
                next_version = (last_version or 0) + 1
                cls._save_quote_history(
                    database=database,
                    demand=demand,
                    version=next_version,
                    quote_data=demand.quote,
                    mode=quote_history_mode,
                )
            except (ProgrammingError, OperationalError) as exc:
                logger.warning(
                    "Skipping quote history save for %s due to schema mismatch: %s",
                    demand.name,
                    exc,
                )

            quote_results.append(demand.name)

        # Clean up old Parameter-based quote data if exists
        Parameter.objects.using(database).filter(pk="quote_data").delete()

        logger.info(
            "Generated quotes for %d demands, stored on Demand.quote field",
            len(quote_results),
        )

    @staticmethod
    def _quote_history_payload(quote_data):
        """Exclude regenerated advisory fields from the auditable price payload."""
        quote_data = dict(quote_data or {})
        for key in (
            "history_sample_count", "history_calibration_enabled",
            "history_calibration_factor", "history_suggested_total",
            "history_suggested_adjustment", "history_samples",
            "quote_generated_at", "quote_valid_until",
        ):
            quote_data.pop(key, None)
        return quote_data

    @staticmethod
    def _calculate_bom_route_costs(
        database, demand, supplier_selection="priority", batch_cost_allocation="inventory_credit"
    ):
        """Expand a demand's BOM and routing without creating operation plans."""
        from collections import defaultdict
        from decimal import Decimal
        from django.db.models import F, Q
        from ccAPPSdb.input.models import ItemSupplier
        from ccAPPSdb.input.models.operation import Operation, OperationMaterial, OperationResource

        zero = Decimal("0")
        quote_date = demand.due or timezone.now()
        active_on_quote_date = (
            (Q(effective_start__isnull=True) | Q(effective_start__lte=quote_date))
            & (Q(effective_end__isnull=True) | Q(effective_end__gte=quote_date))
        )
        result = {
            "material_cost": zero,
            "resource_cost": zero,
            "operation_cost": zero,
            "transport_cost": zero,
            "material_breakdown": defaultdict(Decimal),
            "resource_breakdown": defaultdict(Decimal),
            "operation_breakdown": defaultdict(Decimal),
            "transport_breakdown": defaultdict(Decimal),
            "batch_inventory_value": zero,
            "complete": True,
            "warnings": [],
            "snapshot": {
                "operations": [], "materials": [], "resources": [],
                "supplier_selection": supplier_selection,
                "batch_cost_allocation": batch_cost_allocation,
            },
        }

        def warn(message):
            result["complete"] = False
            if message not in result["warnings"]:
                result["warnings"].append(message)

        def preferred_operation(item_id):
            operation = (
                Operation.objects.using(database)
                .filter(
                    active_on_quote_date,
                    item_id=item_id, location_id=demand.location_id, owner__isnull=True,
                )
                .order_by("priority", "name")
                .first()
            )
            if operation:
                return operation
            # Quotations can be requested from a sales location while the BOM is
            # maintained at a manufacturing location.
            return (
                Operation.objects.using(database)
                .filter(
                    active_on_quote_date,
                    item_id=item_id, owner__isnull=True,
                )
                .order_by("priority", "name")
                .first()
            )

        def charge_quantity(required_quantity, minimum=None, multiple=None):
            """Return the quantity charged after minimum and batch-multiple rules."""
            charged = abs(required_quantity or zero)
            if minimum is not None and minimum > charged:
                charged = minimum
            if multiple is not None and multiple > zero:
                charged = (charged / multiple).to_integral_value(rounding="ROUND_CEILING") * multiple
            return charged

        def supplier_order(queryset):
            if supplier_selection == "lowest_cost":
                return queryset.annotate(
                    landed_unit_cost=F("cost") + Coalesce(
                        F("transport_cost_per_unit"), Value(0),
                        output_field=DecimalField(),
                    )
                ).order_by(F("landed_unit_cost").asc(nulls_last=True), "priority", "id")
            return queryset.order_by("priority", "id")

        def purchase(item, quantity):
            supplier = supplier_order(
                ItemSupplier.objects.using(database)
                .filter(
                    active_on_quote_date,
                    item_id=item.name, location_id=demand.location_id,
                )
            ).first() or supplier_order(
                ItemSupplier.objects.using(database)
                .filter(
                    active_on_quote_date,
                    item_id=item.name, location__isnull=True,
                )
            ).first()
            charged_quantity = charge_quantity(
                quantity,
                supplier.sizeminimum if supplier else None,
                supplier.sizemultiple if supplier else None,
            )
            unit_cost = supplier.cost if supplier and supplier.cost is not None else item.cost
            if unit_cost is None:
                warn("Missing purchase or standard cost for item '%s'" % item.name)
                unit_cost = zero
            allocated_quantity = quantity if batch_cost_allocation == "inventory_credit" else charged_quantity
            cost = unit_cost * allocated_quantity
            result["material_cost"] += cost
            result["material_breakdown"][item.name] += cost
            result["batch_inventory_value"] += unit_cost * (charged_quantity - allocated_quantity)
            if supplier and supplier.transport_cost_per_unit:
                transport = supplier.transport_cost_per_unit * allocated_quantity
                result["transport_cost"] += transport
                result["transport_breakdown"][str(supplier.supplier_id)] += transport
                result["batch_inventory_value"] += supplier.transport_cost_per_unit * (
                    charged_quantity - allocated_quantity
                )
            result["snapshot"]["materials"].append({
                "item": item.name, "required_quantity": str(quantity),
                "charged_quantity": str(charged_quantity),
                "allocated_quantity": str(allocated_quantity), "unit_cost": str(unit_cost),
                "supplier": supplier.supplier_id if supplier else None,
                "supplier_priority": supplier.priority if supplier else None,
                "supplier_selection": supplier_selection,
                "cost_source": "supplier_purchase_cost" if supplier else "item_standard_cost",
                "transport_per_unit": str(supplier.transport_cost_per_unit or zero) if supplier else "0",
            })

        def expand(operation, quantity, item_stack):
            # Routings delegate their cost to their ordered child operations.
            children = list(
                Operation.objects.using(database)
                .filter(
                    active_on_quote_date,
                    owner_id=operation.name,
                )
                .order_by("priority", "name")
            )
            if operation.type == "routing" and children:
                for child in children:
                    expand(child, quantity, item_stack)
                return
            if operation.type == "alternate" and children:
                expand(children[0], quantity, item_stack)
                return

            charged_quantity = charge_quantity(
                quantity, operation.sizeminimum, operation.sizemultiple
            )
            allocated_quantity = quantity if batch_cost_allocation == "inventory_credit" else charged_quantity
            op_cost = (operation.cost or zero) * allocated_quantity
            if operation.cost is None:
                warn("Missing operation cost for '%s'" % operation.name)
            result["operation_cost"] += op_cost
            result["operation_breakdown"][operation.name] += op_cost
            result["batch_inventory_value"] += (operation.cost or zero) * (
                charged_quantity - allocated_quantity
            )
            result["snapshot"]["operations"].append({
                "operation": operation.name, "required_quantity": str(quantity),
                "charged_quantity": str(charged_quantity),
                "allocated_quantity": str(allocated_quantity),
                "excess_quantity": str(charged_quantity - allocated_quantity),
                "inventory_type": "semi_finished",
                "unit_cost": str(operation.cost or zero), "location": operation.location_id,
            })

            seconds = zero
            if operation.duration:
                seconds += Decimal(str(operation.duration.total_seconds()))
            if operation.duration_per:
                seconds += Decimal(str(operation.duration_per.total_seconds())) * charged_quantity
            hours = seconds / Decimal("3600")
            for requirement in OperationResource.objects.using(database).filter(
                active_on_quote_date,
                operation_id=operation.name,
            ):
                if requirement.resource.cost is None:
                    warn("Missing resource cost for '%s'" % requirement.resource_id)
                if requirement.resource.cost is not None:
                    usage = (requirement.quantity or Decimal("1")) * hours
                    full_cost = requirement.resource.cost * usage
                    allocation_ratio = allocated_quantity / charged_quantity if charged_quantity else zero
                    cost = full_cost * allocation_ratio
                    result["resource_cost"] += cost
                    result["resource_breakdown"][requirement.resource_id] += cost
                    result["batch_inventory_value"] += full_cost - cost
                result["snapshot"]["resources"].append({
                    "resource": requirement.resource_id,
                    "quantity": str(requirement.quantity or Decimal("1")),
                    "hours": str(hours), "rate": str(requirement.resource.cost or zero),
                })

            flows = OperationMaterial.objects.using(database).filter(
                active_on_quote_date,
                operation_id=operation.name, quantity__lt=0,
            ).order_by("name", "priority", "id")
            selected_flows = {}
            for flow in flows:
                selected_flows.setdefault(flow.name or str(flow.id), flow)
            for flow in selected_flows.values():
                material_quantity = (
                    charged_quantity if batch_cost_allocation == "full_batch" else quantity
                )
                required = (
                    abs(flow.quantity or zero) * material_quantity
                    + abs(flow.quantity_fixed or zero)
                    * (material_quantity / charged_quantity if charged_quantity else zero)
                )
                if required <= zero:
                    continue
                component = flow.item
                if component.name in item_stack:
                    # Avoid recursive BOM loops; price the cyclic component directly.
                    purchase(component, required)
                    continue
                component_operation = preferred_operation(component.name)
                if component_operation:
                    expand(component_operation, required, item_stack | {component.name})
                else:
                    purchase(component, required)

        root = demand.operation or preferred_operation(demand.item_id)
        if not root:
            return None
        expand(root, demand.quantity or Decimal("0"), {demand.item_id})
        # The root operation surplus is a finished item; lower-level purchase
        # surplus remains raw material and intermediate operations semi-finished.
        for record in result["snapshot"]["materials"]:
            record["inventory_type"] = "raw_material"
        return result

    @staticmethod
    def _calculate_history_calibration(
        database, demand, base_total, tax_rate, calculation_mode,
        batch_cost_allocation, supplier_selection,
    ):
        """Return a bounded, similarity-weighted price factor from prior quotes."""
        import json
        import math
        from datetime import timedelta
        from decimal import Decimal
        from django.utils import timezone

        if base_total <= 0 or not demand.quantity or demand.quantity <= 0:
            return {"factor": Decimal("1"), "sample_count": 0, "samples": []}

        current_unit_price = base_total / demand.quantity
        weighted_factors = []
        weights = []
        cutoff = timezone.now() - timedelta(days=730)
        # Some deployed scenarios retain the legacy quote_history schema while
        # the Django audit model expects newer columns. Query only shared fields.
        with connections[database].cursor() as cursor:
            demand_columns = {
                column.name
                for column in connections[database].introspection.get_table_description(
                    cursor, "demand"
                )
            }
            accepted_filter = "and d.quote_accepted" if "quote_accepted" in demand_columns else ""
            cursor.execute(
                """%s""" % (
                    """
                select h.demand_id, h.quote_data, h.created_at, d.quantity,
                       d.customer_id, d.status
                from (
                    select distinct on (demand_id)
                           demand_id, quote_data, created_at, version
                    from quote_history
                    order by demand_id, version desc, created_at desc
                ) h
                join demand d on d.name = h.demand_id
                where d.item_id = %%s and d.status = 'closed' %s
                  and h.demand_id <> %%s and h.created_at >= %%s
                order by h.created_at desc
                limit 200
                """ % accepted_filter
                ),
                [demand.item_id, demand.name, cutoff],
            )
            history = cursor.fetchall()
        samples = []
        for demand_id, quote_data, created_at, historical_quantity, customer_id, status in history:
            quote = json.loads(quote_data) if isinstance(quote_data, str) else (quote_data or {})
            try:
                # Compare commercial totals on the same tax basis. Legacy data
                # without an explicit tax rate is treated as a different rule.
                if Decimal(str(quote.get("tax_rate_applied", "0")).rstrip("%")) != tax_rate:
                    continue
                if quote.get("calculation_method") != calculation_mode:
                    continue
                if quote.get("batch_cost_allocation", "full_batch") != batch_cost_allocation:
                    continue
                snapshot = quote.get("cost_snapshot") or {}
                if snapshot.get("supplier_selection", "priority") != supplier_selection:
                    continue
                historical_total = Decimal(str(quote.get("total_with_tax", "0")))
                historical_quantity = historical_quantity or Decimal("0")
            except Exception:
                continue
            if historical_total <= 0 or historical_quantity <= 0:
                continue
            historical_unit_price = historical_total / historical_quantity
            factor = historical_unit_price / current_unit_price
            quantity_distance = abs(
                math.log(float(demand.quantity)) - math.log(float(historical_quantity))
            )
            quantity_similarity = max(0.0, 1.0 - quantity_distance)
            customer_weight = 1.0 if customer_id == demand.customer_id else 0.3
            months_ago = (timezone.now() - created_at).days / 30.0
            recency_weight = math.exp(-months_ago / 12.0)
            weight = 0.5 * quantity_similarity + 0.3 * customer_weight + 0.2 * recency_weight
            weights.append(weight)
            weighted_factors.append(factor)
            samples.append({
                "demand": demand_id,
                "customer": customer_id,
                "status": status,
                "quantity": str(historical_quantity),
                "unit_price": str(round(historical_unit_price, 2)),
                "quote_date": created_at.strftime("%Y-%m-%d"),
                "quantity_similarity": round(quantity_similarity, 3),
                "weight": round(weight, 3),
                "price_factor": str(round(factor, 4)),
            })

        samples.sort(key=lambda sample: sample["weight"], reverse=True)
        samples = samples[:20]

        # A small history is informational only; three records are the minimum
        # required before it can influence the generated customer quote.
        if len(weighted_factors) < 3:
            return {
                "factor": Decimal("1"),
                "sample_count": len(weighted_factors),
                "samples": samples,
            }
        total_weight = sum(weights)
        factor = (
            sum(value * Decimal(str(weight)) for value, weight in zip(weighted_factors, weights))
            / Decimal(str(total_weight))
        )
        return {
            "factor": max(Decimal("0.85"), min(Decimal("1.15"), factor)),
            "sample_count": len(weighted_factors),
            "samples": samples,
        }

    @staticmethod
    def _collect_upstream_plans(delivery, database):
        import json
        from decimal import Decimal
        from ccAPPSdb.input.models import OperationPlan

        visited = set()
        result = []
        queue = [(delivery, abs(delivery.quantity or Decimal("1")))]

        while queue:
            plan, required_qty = queue.pop(0)
            if (plan.reference, str(required_qty)) in visited:
                continue
            visited.add((plan.reference, str(required_qty)))
            result.append((plan, abs(required_qty)))

            try:
                plan_data = plan.plan
                if isinstance(plan_data, str):
                    plan_data = json.loads(plan_data)
                if isinstance(plan_data, dict):
                    for entry in plan_data.get("upstream_opplans", []):
                        # Entry can be a string (reference) or a list [reference, ...ratios]
                        ref = str(entry[0]) if isinstance(entry, list) else str(entry)
                        upstream_qty = (
                            Decimal(str(entry[1]))
                            if isinstance(entry, list) and len(entry) > 1
                            else abs(plan.quantity or Decimal("1"))
                        )
                        # Plan data stores the total quantity sourced from each
                        # upstream plan. Scale it to the portion needed here.
                        plan_qty = abs(plan.quantity or Decimal("1"))
                        qty = upstream_qty * min(Decimal("1"), required_qty / plan_qty)
                        up = OperationPlan.objects.using(database).filter(reference=ref).first()
                        if up:
                            queue.append((up, abs(qty)))
                        elif " @ " in ref:
                            item_name = ref.rsplit(" @ ", 1)[0]
                            result.append((None, abs(qty), item_name))
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass

        return result

    @staticmethod
    def _get_quote_history_mode(database):
        try:
            with connections[database].cursor() as cursor:
                cols = {
                    c.name
                    for c in connections[database].introspection.get_table_description(
                        cursor, "quote_history"
                    )
                }
        except Exception:
            return "missing"

        if {"source", "lastmodified"}.issubset(cols):
            return "audit"
        if {"demand_id", "version", "quote_data", "created_at"}.issubset(cols):
            return "legacy"
        return "unsupported"

    @staticmethod
    def _save_quote_history(database, demand, version, quote_data, mode):
        if mode == "audit":
            from ccAPPSdb.input.models import QuoteHistory

            QuoteHistory.objects.using(database).create(
                demand=demand,
                version=version,
                quote_data=quote_data,
            )
            return

        if mode == "legacy":
            import json

            with connections[database].cursor() as cursor:
                cursor.execute(
                    """
                    insert into quote_history
                    (demand_id, version, quote_data, created_at)
                    values (%s, %s, %s::jsonb, now())
                    """,
                    [demand.name, version, json.dumps(quote_data)],
                )
            return

        raise ProgrammingError("Unsupported quote_history schema")


@PlanTaskRegistry.register
class EraseModel(PlanTask):
    description = "Erase model"
    sequence = 700

    @classmethod
    def getWeight(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        # Task not active!
        return -1

    @classmethod
    def run(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        import ccAPPS

        ccAPPS.erase(True)
        ccAPPS.printsize()


@PlanTaskRegistry.register
class ActivatePythonDebugger(PlanTask):
    description = "Activate Python debugger"
    sequence = 50

    @classmethod
    def getWeight(cls, **kwargs):
        try:
            import debugpy

            return 0.1 if "debugpy" in os.environ else -1
        except ImportError:
            return -1

    @classmethod
    def run(cls, **kwargs):
        import debugpy

        debugpy.configure()
        debugpy.listen(("0.0.0.0", 17999))

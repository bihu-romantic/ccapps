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
        solver_mode = Parameter.getValue("plan.solver", database, "mrp").lower()
        if solver_mode not in ("mrp", "fas", "ga", "cp"):
            logger.warning(
                "Unsupported plan.solver '%s'; falling back to mrp", solver_mode
            )
            solver_mode = "mrp"
        # FAS: MRP creates the supply network, then C++ SolverFAS owns the
        # forward finite-capacity dates/resources. Standalone SolverFAS
        # (expand+forward) remains available when the model has no planned
        # open demand yet; the post-MRP path below invokes it in forward-only
        # mode after this MRP expansion.
        cls.solver = ccAPPS.solver_mrp(
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
        direct_ga = solver_mode == "ga" and Parameter.getValue(
            "ga.direct", database, os.environ.get("GA_DIRECT", "")
        ).lower() in ("1", "true", "yes")
        if direct_ga:
            # The complete previous plan is loaded by loadOperationPlans.
            # Never let the MRP base solver erase it before the GA pass.
            cls.solver.erasepreviousfirst = False
            logger.info("GA direct rescheduling: preserving existing operationplans")
        if hasattr(cls, "debugResource"):
            cls.solver.userexit_resource = cls.debugResource
        if hasattr(cls, "debugDemand"):
            cls.solver.userexit_demand = cls.debugDemand
        if hasattr(cls, "debugOperation"):
            cls.solver.userexit_operation = cls.debugOperation
        # Timed demand feeder is installed below after the deadline is known.
        logger.info("Plan type: %s" % plantype)
        constraint_str = []
        if constraint & 4:
            constraint_str.append("capa")
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
                    if not direct_ga:
                        cls.solver.solve()
                except Exception as exc:
                    solver_error.append(exc)
                finally:
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
                if solver_mode == "fas":
                    cls.solver.rollback()
                raise solver_error[0]
            else:
                logger.info("Solver completed")
            # Run ACO/Exact/AutoFix after MRP if enabled
            if solver_mode == "mrp":
                logger.info("MRP-only planning mode: skipping post-MRP optimization")
            elif solver_mode == "fas":
                try:
                    # FAS = MRP supply + C++ SolverFAS forward (calendar,
                    # material FIFO, pool search, job-order improve).
                    commit_solver_state()
                    fas_job_order = Parameter.getValue(
                        "plan.fas_job_order", database, "lpt"
                    ).lower()
                    os.environ["FAS_JOB_ORDER"] = fas_job_order
                    fas_improve = Parameter.getValue(
                        "plan.fas_improve_seconds", database, "10"
                    )
                    os.environ["FAS_IMPROVE_SECONDS"] = str(fas_improve)
                    fas_kwargs = {
                        "constraints": constraint,
                        "plantype": plantype,
                        "loglevel": loglevel,
                        "lazydelay": int(
                            Parameter.getValue("lazydelay", database, "86400")
                        ),
                        "minimumdelay": int(
                            Parameter.getValue(
                                "plan.minimumdelay", database, "3600"
                            )
                        ),
                        "rotateresources": (
                            Parameter.getValue(
                                "plan.rotateResources", database, "true"
                            ).lower()
                            == "true"
                        ),
                        "administrativeleadtime": 86400
                        * float(
                            Parameter.getValue(
                                "plan.administrativeLeadtime", database, "0"
                            )
                        ),
                        "autofence": 86400
                        * float(
                            Parameter.getValue(
                                "plan.autoFenceOperations", database, "0"
                            )
                        ),
                        "iterationmax": int(
                            Parameter.getValue(
                                "plan.iterationmax", database, "0"
                            )
                        ),
                        "resourceiterationmax": int(
                            Parameter.getValue(
                                "plan.resourceiterationmax", database, "500"
                            )
                        ),
                    }
                    fas_solver = ccAPPS.solverFAS(**fas_kwargs)
                    logger.info(
                        "Running SolverFAS forward schedule on MRP supply "
                        "(job_order=%s, improve_seconds=%s)",
                        fas_job_order,
                        fas_improve,
                    )
                    fas_solver.solve()
                    fas_solver.commit()
                    logger.info("SolverFAS forward schedule committed")
                except Exception:
                    logger.error(
                        "Exception caught during FAS forward schedule",
                        exc_info=True,
                    )
                    raise
            elif solver_mode == "ga":
                try:
                    # GA is a second-stage forward optimizer.  Its C++
                    # decoder must see the complete MRP-created MO/PO/DO
                    # network (and therefore the real FlowPlan quantities),
                    # not the partial command stream still owned by the MRP
                    # solver.  Commit the supply expansion before GA starts;
                    # this is especially required for CCPL WIP flow lines
                    # where OP-01..OP-N are independently split MOs.
                    commit_solver_state()
                    # NSGA-II keeps the Pareto front. This profile selects
                    # the customer-facing representative plan from it.
                    # Core search size is solver-owned. CCPL requests select
                    # only the GA objective profile; population/generations
                    # are stable C++ defaults rather than payload controls.
                    ga_environment = {
                        "GA_DIRECT": Parameter.getValue("ga.direct", database, "false"),
                        "GA_SEED": Parameter.getValue("ga.seed", database, "123456"),
                        # Aligned with D:\test Numba ga_runner defaults.
                        "GA_POPULATION": os.environ.get("GA_POPULATION", "500"),
                        "GA_GENERATIONS": os.environ.get("GA_GENERATIONS", "50"),
                        # Early stop: halt when best score has not improved for
                        # this many consecutive generations.
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
                        # How many Pareto solutions are sent to stage 2.
                        "GA_STAGE2_SOLUTIONS": Parameter.getValue(
                            "ga.stage2_solutions",
                            database,
                            os.environ.get("GA_STAGE2_SOLUTIONS", "1"),
                        ),
                        "GA_OBJECTIVE_PROFILE": Parameter.getValue("ga.objective_profile", database, "due_date"),
                        "GA_MATERIAL_MODE": Parameter.getValue("ga.material_mode", database, "postcheck"),
                        "GA_LOGLEVEL": os.environ.get("GA_LOGLEVEL", "0"),
                        # Diagnostic exports are opt-in and remain entirely
                        # outside the solver algorithm. They make it
                        # possible to verify that a rejected chromosome was
                        # rejected by the decode-time WIP gate, rather than
                        # mistaking the retained MRP plan for a GA result.
                        "GA_DUMP_APPLY_DECISION": os.environ.get("GA_DUMP_APPLY_DECISION", ""),
                        "GA_DUMP_SOLUTION": os.environ.get("GA_DUMP_SOLUTION", ""),
                        # Forward-only diagnostic for internal WIP FIFO.  It is
                        # intentionally opt-in so normal GA runs remain quiet.
                        "GA_WIP_TRACE": os.environ.get("GA_WIP_TRACE", ""),
                        # Opt-in trace of the exported chromosome's internal
                        # material gate decisions and final FIFO ledger.
                        "GA_GATE_TRACE": os.environ.get("GA_GATE_TRACE", ""),
                    }
                    previous_ga_environment = {key: os.environ.get(key) for key in ga_environment}
                    try:
                        os.environ.update(ga_environment)
                        # Outer thread kill must exceed the internal GA time
                        # limit so evolution can finish and apply the best plan.
                        ga_timeout = int(Parameter.getValue(
                            "ga.timeout_seconds", database,
                            os.environ.get("GA_TIMEOUT_SECONDS", "70"),
                        ))
                        if run_threaded(
                            ccAPPS.run_ga,
                            ga_timeout,
                            "Running GA optimization...",
                            "GA completed",
                            "GA timed out after %s seconds",
                        ):
                            # GA writes through its own command manager. The MRP
                            # stream was already committed above.
                            pass
                    finally:
                        for key, value in previous_ga_environment.items():
                            if value is None:
                                os.environ.pop(key, None)
                            else:
                                os.environ[key] = value
                except Exception as e:
                    logger.warning("GA failed: %s" % e)
            elif solver_mode == "cp":
                try:
                    # Same commit point as GA: CP-SAT reads the manufacturing
                    # orders MRP has already created, then rewrites their
                    # dates and resources.
                    commit_solver_state()
                    cp_method = Parameter.getValue(
                        "cp.objective_method",
                        database,
                        os.environ.get("CP_OBJECTIVE_METHOD", "weighted"),
                    )
                    previous_cp_method = os.environ.get("CP_OBJECTIVE_METHOD")
                    os.environ["CP_OBJECTIVE_METHOD"] = cp_method
                    logger.info(
                        "Running CP-SAT flexible job shop (%s)" % cp_method
                    )
                    try:
                        ccAPPS.run_cp()
                    finally:
                        if previous_cp_method is None:
                            os.environ.pop("CP_OBJECTIVE_METHOD", None)
                        else:
                            os.environ["CP_OBJECTIVE_METHOD"] = previous_cp_method
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

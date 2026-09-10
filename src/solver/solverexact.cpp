/***************************************************************************
 *                                                                         *
 * Copyright (C) 2007-2015 by ccAPPS bv                                   *
 *                                                                         *
 * Permission is hereby granted, free of charge, to any person obtaining   *
 * a copy of this software and associated documentation files (the         *
 * "Software"), to deal in the Software without restriction, including     *
 * without limitation the rights to use, copy, modify, merge, publish,     *
 * distribute, sublicense, and/or sell copies of the Software, and to      *
 * permit persons to whom the Software is furnished to do so, subject to   *
 * the following conditions:                                               *
 *                                                                         *
 * The above copyright notice and this permission notice shall be          *
 * included in all copies or substantial portions of the Software.         *
 *                                                                         *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,         *
 * EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF      *
 * MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND                   *
 * NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE  *
 * LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION  *
 * OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION   *
 * WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.         *
 *                                                                         *
 ***************************************************************************/

#include <algorithm>
#include <limits>
#include <unordered_set>

#include "ccAPPS/solverexact.h"

#ifdef HAVE_ORTOOLS
#include "absl/time/time.h"
#include "ortools/linear_solver/linear_solver.h"
#endif

namespace ccAPPS {

#ifdef HAVE_ORTOOLS
using operations_research::MPConstraint;
using operations_research::MPObjective;
using operations_research::MPSolver;
using operations_research::MPVariable;
#endif

namespace {

bool calendarHasPositiveValue(Calendar* cal, Date start, Date end) {
  if (!cal || end <= start) return true;
  Calendar::EventIterator iter(cal, start, true);
  Date cur = start;
  while (cur < end) {
    Date next = iter.getDate();
    if (next <= cur) {
      if (iter.getValue() > 0.0) return true;
      ++iter;
      continue;
    }
    double value = iter.getPrevValue();
    if (value > 0.0) return true;
    if (next >= end) return false;
    cur = next;
    ++iter;
  }
  return false;
}

bool resourceCalendarsAvailable(const Resource* res, Date start, Date end,
                                Duration required) {
  if (!res || end <= start) return false;
  if (required <= Duration(0L)) return true;
  if (res->getAvailable(start, end) < required)
    return false;
  if (res->getMaximumCalendar() &&
      !calendarHasPositiveValue(res->getMaximumCalendar(), start, end))
    return false;
  return true;
}

int bucketIndex(Date date, Date horizonStart, int bucketSeconds,
                int bucketCount) {
  long offset = (date - horizonStart).getSeconds();
  if (offset <= 0) return 0;
  return static_cast<int>(
      min<long>(bucketCount - 1, offset / bucketSeconds));
}

int bucketEndIndex(Date date, Date horizonStart, int bucketSeconds,
                   int bucketCount) {
  long offset = (date - horizonStart).getSeconds();
  if (offset <= 0) return 1;
  long bucket = (offset + bucketSeconds - 1) / bucketSeconds;
  return static_cast<int>(min<long>(bucketCount, max<long>(1, bucket)));
}

}  // namespace

const MetaClass* SolverExact::metadata;

// ==========================================================================
// SolverExact Python init
// ==========================================================================

// Enable/disable the final MRP propagation after exact sequencing.
static PyObject* solverExact_setRunMRP(PyObject* self, PyObject* args) {
  auto* solver = static_cast<SolverExact*>(self);
  int val;
  if (!PyArg_ParseTuple(args, "p", &val)) return nullptr;
  solver->setRunMRP(val != 0);
  Py_RETURN_NONE;
}

// Read the final MRP propagation flag.
static PyObject* solverExact_getRunMRP(PyObject* self, PyObject*) {
  auto* solver = static_cast<SolverExact*>(self);
  return PyBool_FromLong(solver->getRunMRP() ? 1 : 0);
}

// Configure purchase material availability for exact/ACO scoring.
static PyObject* solverExact_setPurchaseMaterialMode(
    PyObject* self, PyObject* args) {
  auto* solver = static_cast<SolverExact*>(self);
  int val;
  if (!PyArg_ParseTuple(args, "i", &val)) return nullptr;
  if (val < 0 || val > 1) {
    PyErr_SetString(PyExc_ValueError,
                    "Expected 0 (infinite) or 1 (leadtime)");
    return nullptr;
  }
  solver->setPurchaseMaterialMode(val);
  Py_RETURN_NONE;
}

// Read purchase material availability mode.
static PyObject* solverExact_getPurchaseMaterialMode(PyObject* self, PyObject*) {
  auto* solver = static_cast<SolverExact*>(self);
  return PyLong_FromLong(solver->getPurchaseMaterialMode());
}

/* Global function: run exact sequencing on constrained resources.
 * Called from Python as ccAPPS.run_exact(). */
PyObject* run_exact(PyObject*, PyObject*) {
  SolverExact exact;
  exact.setRunMRP(false);
  exact.setLogLevel(2);  // Enable diagnostic output
  void* v = nullptr;
  exact.solve(v);
  auto* cmdMgr = exact.getCommandManager();
  if (cmdMgr) cmdMgr->commit();
  Py_RETURN_NONE;
}

int SolverExact::initialize() {
  metadata = MetaClass::registerClass<SolverExact>(
      "solver", "solver_exact", Object::create<SolverExact>);
  registerFields<SolverExact>(const_cast<MetaClass*>(metadata));

  auto& x = PythonExtension<SolverExact>::getPythonType();
  x.setName("solverExact");
  x.setDoc("ccAPPS exact solver for bottleneck resource subproblems");
  x.supportgetattro();
  x.supportsetattro();
  x.supportcreate(SolverExact::create);
  x.addMethod("solve",
              static_cast<PyObject* (*)(PyObject*, PyObject*, PyObject*)>(
                  SolverCreate::solve),
              METH_VARARGS, "run the solver");
  x.addMethod("commit", SolverCreate::commit, METH_NOARGS,
              "commit the plan changes");
  x.addMethod("rollback", SolverCreate::rollback, METH_NOARGS,
              "rollback the plan changes");
  x.addMethod("markAutofence", SolverCreate::markAutofence, METH_NOARGS,
              "mark the autofence of buffers");
  x.addMethod("setRunMRP", solverExact_setRunMRP, METH_VARARGS,
              "Enable/disable MRP propagation after exact solving");
  x.addMethod("getRunMRP", solverExact_getRunMRP, METH_NOARGS,
              "Check if MRP propagation is enabled");
  x.addMethod("setPurchaseMaterialMode", solverExact_setPurchaseMaterialMode,
              METH_VARARGS, "Set exact purchase material mode");
  x.addMethod("getPurchaseMaterialMode", solverExact_getPurchaseMaterialMode,
              METH_NOARGS, "Get exact purchase material mode");

  SolverExact::metadata->setPythonClass(x);
  return x.typeReady();
}

PyObject* SolverExact::create(PyTypeObject*, PyObject*, PyObject*) {
  return Object::create<SolverExact>();
}

// ==========================================================================
// Exact configuration
// ==========================================================================

void SolverExact::setConfig(const ExactConfig& cfg) {
  config_ = cfg;
  syncACOConfig();
}

void SolverExact::syncACOConfig() {
  // Keep shared ACO helpers aligned: candidates and fitness.
  ACOConfig acoCfg = getConfig();
  acoCfg.runMRP = config_.runMRP;
  acoCfg.purchase_material_mode = config_.purchase_material_mode;
  acoCfg.weight_tardiness = config_.weight_tardiness;
  acoCfg.weight_cost = config_.weight_cost;
  acoCfg.weight_setup = config_.weight_setup;
  acoCfg.weight_priority = config_.weight_priority;
  acoCfg.weight_balance = config_.weight_balance;
  SolverACO::setConfig(acoCfg);
}

// ==========================================================================
// Candidate resource collection
// ==========================================================================

vector<const Resource*> SolverExact::collectBottlenecks() const {
  // Collect constrained resources that have at least 1 manufacturing plan.
  vector<const Resource*> result;
  for (auto res = Resource::begin(); res != Resource::end(); ++res) {
    if (!res->getConstrained()) continue;
    vector<OperationPlan*> plans;
    auto loadplans = res->getLoadPlans();
    for (auto it = loadplans.begin(); it != loadplans.end(); ++it) {
      OperationPlan* op = it->getOperationPlan();
      if (op && op->getOperation() && op->getQuantity() > 0.0 &&
          !op->getOperation()->hasType<OperationItemSupplier,
                                       OperationItemDistribution>() &&
          find(plans.begin(), plans.end(), op) == plans.end())
        plans.push_back(op);
    }
    if (!plans.empty()) result.push_back(&*res);
  }
  return result;
}

// ==========================================================================
// OR-Tools SCIP time-bucket MIP
// ==========================================================================

AntSolution SolverExact::solveTimeIndexedMIP(
    const vector<const Resource*>& resources,
    const vector<CandidateOp>& candidates) {
  // Build and solve an OR-Tools/SCIP time-bucket MIP. If OR-Tools isn't
  // available, the problem is too large, or SCIP fails to find a feasible
  // solution, this returns an invalid AntSolution and the MRP result is kept.
  //
  // Each binary variable chooses one candidate and one start bucket. Resource
  // bucket capacity prevents overlaps; pairwise cuts include setup time.
  AntSolution empty;
  empty.fitness = -numeric_limits<double>::max();

#ifndef HAVE_ORTOOLS
  logger << indentlevel
         << "Exact time-bucket MIP: unavailable (built without OR-Tools)\n";
  return empty;
#else
  if (resources.empty() || candidates.empty()) {
    logger << indentlevel << "Exact time-bucket MIP: skip ("
           << resources.size() << " resources, " << candidates.size()
           << " candidates)\n";
    return empty;
  }
  if (candidates.size() > static_cast<size_t>(config_.max_operations)) {
    logger << indentlevel << "Exact time-bucket MIP: skip ("
           << candidates.size() << " candidates > "
           << config_.max_operations << " max operations)\n";
    return empty;
  }
  int bucketSeconds = max(1, config_.time_bucket_seconds);
  Date current = Plan::instance().getCurrent();
  unordered_set<const Resource*> resourceSet;
  for (auto* r : resources) resourceSet.insert(r);

  unordered_set<const OperationPlan*> uniqueOps;
  Date horizonStart = Date::infiniteFuture;
  Date horizonEnd = current;
  for (const auto& c : candidates) {
    if (!c.op || c.allResources.empty()) continue;
    uniqueOps.insert(c.op);
    const Resource* durationResource =
        c.res ? c.res : c.allResources.front();
    Duration dur = estimateOperationDuration(c.op, durationResource);
    Date candidateStart = c.earliestStart;
    if (c.op->getStart() != Date::infinitePast &&
        c.op->getStart() != Date::infiniteFuture &&
        c.op->getStart() < candidateStart)
      candidateStart = c.op->getStart();
    if (candidateStart < horizonStart) horizonStart = candidateStart;
    Date candidateEnd = c.earliestStart + dur;
    if (c.op->getEnd() != Date::infinitePast &&
        c.op->getEnd() != Date::infiniteFuture &&
        c.op->getEnd() > candidateEnd)
      candidateEnd = c.op->getEnd();
    Demand* dmd = c.op->getTopOwner()->getDemand();
    if (dmd) {
      Date due = dmd->getDue();
      if (due != Date::infiniteFuture && due > candidateEnd)
        candidateEnd = due;
    }
    if (candidateEnd > horizonEnd) horizonEnd = candidateEnd;
  }
  if (horizonStart == Date::infiniteFuture || horizonStart < current)
    horizonStart = current;
  if (uniqueOps.empty() || horizonEnd <= horizonStart) {
    logger << indentlevel
           << "Exact time-bucket MIP: skip (empty operation set or horizon)\n";
    return empty;
  }

  long horizonSeconds = (horizonEnd - horizonStart).getSeconds();
  int bucketCount =
      static_cast<int>(max<long>(1, horizonSeconds / bucketSeconds + 1));
  logger << indentlevel << "Exact time-bucket MIP: horizon "
         << horizonStart << " - " << horizonEnd << ", " << bucketCount
         << " buckets of " << bucketSeconds << " seconds\n";
  if (bucketCount > 10000) {
    logger << indentlevel << "Exact time-bucket MIP: skip ("
           << bucketCount << " buckets > 10000)\n";
    return empty;
  }

  // --- Build SCIP MIP ---
  MPSolver solver("ccapps_exact_time_bucket",
                  MPSolver::SCIP_MIXED_INTEGER_PROGRAMMING);
  if (config_.mip_time_limit_seconds > 0)
    solver.SetTimeLimit(absl::Seconds(config_.mip_time_limit_seconds));

  struct VarSlot {
    const CandidateOp* candidate = nullptr;
    int startBucket = 0;
    int durationBuckets = 1;
    Date start;
    Date end;
    MPVariable* var = nullptr;
  };
  vector<VarSlot> slots;
  unordered_map<const OperationPlan*, vector<int>> opSlots;
  unordered_map<const Resource*, vector<vector<int>>> resourceBuckets;
  for (auto* r : resources) resourceBuckets[r].resize(bucketCount);
  size_t calendarFilteredSlots = 0;
  size_t horizonFilteredSlots = 0;
  size_t duplicateSlots = 0;
  unordered_set<string> seenSlots;

  for (const auto& c : candidates) {
    if (!c.op || c.allResources.empty()) continue;
    const Resource* durationResource =
        c.res ? c.res : c.allResources.front();
    Duration dur = estimateOperationDuration(c.op, durationResource);
    int durationBuckets = max(
        1, static_cast<int>((dur.getSeconds() + bucketSeconds - 1) /
                            bucketSeconds));
    long earliestOffset = max<long>(
        0, (c.earliestStart - horizonStart).getSeconds());
    int earliestBucket = static_cast<int>(
        min<long>(bucketCount - 1, earliestOffset / bucketSeconds));

    for (int b = earliestBucket; b + durationBuckets <= bucketCount; ++b) {
      Date rawStart = horizonStart + Duration(static_cast<long>(b) * bucketSeconds);
      Date start = rawStart;
      Date end = rawStart + dur;
      if (c.op->getOperation()) {
        DateRange range = c.op->getOperation()->calculateOperationTime(
            c.op, rawStart, dur, true);
        start = range.getStart();
        end = range.getEnd();
      }
      if (start == Date::infiniteFuture || end == Date::infiniteFuture)
        continue;
      if (end <= start || end > horizonEnd) {
        ++horizonFilteredSlots;
        continue;
      }

      bool calendarsAvailable = true;
      for (auto* r : c.allResources) {
        if (!resourceCalendarsAvailable(r, start, end, dur)) {
          calendarsAvailable = false;
          break;
        }
      }
      if (!calendarsAvailable) {
        ++calendarFilteredSlots;
        continue;
      }

      int startBucket = bucketIndex(start, horizonStart, bucketSeconds,
                                    bucketCount);
      int endBucket =
          bucketEndIndex(end, horizonStart, bucketSeconds, bucketCount);
      if (endBucket <= startBucket) endBucket = startBucket + 1;
      if (endBucket > bucketCount) {
        ++horizonFilteredSlots;
        continue;
      }
      string slotKey = to_string(c.candId) + "|" +
                       to_string(static_cast<long long>(
                           (start - horizonStart).getSeconds())) +
                       "|" +
                       to_string(static_cast<long long>(
                           (end - horizonStart).getSeconds()));
      if (!seenSlots.insert(slotKey).second) {
        ++duplicateSlots;
        continue;
      }

      VarSlot slot;
      slot.candidate = &c;
      slot.startBucket = startBucket;
      slot.durationBuckets = endBucket - startBucket;
      slot.start = start;
      slot.end = end;
      slot.var = solver.MakeBoolVar("");
      int idx = static_cast<int>(slots.size());
      slots.push_back(slot);
      opSlots[c.op].push_back(idx);

      for (auto* r : c.allResources) {
        if (!resourceSet.count(r)) continue;
        if (!resourceBuckets.count(r)) resourceBuckets[r].resize(bucketCount);
        for (int k = startBucket; k < endBucket && k < bucketCount; ++k)
          resourceBuckets[r][k].push_back(idx);
      }
    }
  }
  logger << indentlevel << "Exact time-bucket MIP: filtered "
         << calendarFilteredSlots << " calendar-infeasible slots, "
         << horizonFilteredSlots << " horizon-overrun slots, "
         << duplicateSlots << " duplicate slots\n";
  if (slots.empty()) {
    logger << indentlevel
           << "Exact time-bucket MIP: skip (no feasible time slots across "
           << uniqueOps.size() << " operations, " << bucketCount
           << " buckets of " << bucketSeconds << " seconds)\n";
    return empty;
  }

  // Pick exactly one candidate/start bucket per operation.
  for (auto* op : uniqueOps) {
    auto it = opSlots.find(op);
    if (it == opSlots.end() || it->second.empty()) {
      logger << indentlevel
             << "Exact time-bucket MIP: skip (operation '"
             << op->getOperation()->getName()
             << "' has no feasible time slot in " << bucketCount
             << " buckets of " << bucketSeconds << " seconds)\n";
      return empty;
    }
    MPConstraint* once = solver.MakeRowConstraint(1.0, 1.0);
    for (int idx : it->second) once->SetCoefficient(slots[idx].var, 1.0);
  }

  // Resource bucket capacity: at most one selected slot per resource bucket.
  for (const auto& kv : resourceBuckets) {
    for (int k = 0; k < bucketCount; ++k) {
      if (kv.second[k].empty()) continue;
      MPConstraint* cap = solver.MakeRowConstraint(0.0, 1.0);
      for (int idx : kv.second[k]) cap->SetCoefficient(slots[idx].var, 1.0);
    }
  }

  // Pairwise setup cuts for slots sharing a resource.
  for (size_t a = 0; a < slots.size(); ++a) {
    for (size_t b = a + 1; b < slots.size(); ++b) {
      const CandidateOp* ca = slots[a].candidate;
      const CandidateOp* cb = slots[b].candidate;
      if (!ca || !cb || ca->op == cb->op) continue;
      bool shareResource = false;
      for (auto* ra : ca->allResources) {
        if (!resourceSet.count(ra)) continue;
        for (auto* rb : cb->allResources) {
          if (ra == rb) {
            shareResource = true;
            break;
          }
        }
        if (shareResource) break;
      }
      if (!shareResource) continue;

      Duration setupAB = computeSetupTime(ca->op, cb->op);
      Duration setupBA = computeSetupTime(cb->op, ca->op);
      bool aBeforeB = slots[a].end + setupAB <= slots[b].start;
      bool bBeforeA = slots[b].end + setupBA <= slots[a].start;
      if (!aBeforeB && !bBeforeA) {
        MPConstraint* cut = solver.MakeRowConstraint(0.0, 1.0);
        cut->SetCoefficient(slots[a].var, 1.0);
        cut->SetCoefficient(slots[b].var, 1.0);
      }
    }
  }

  // Material balance by buffer and time bucket. Quantities come directly from
  // FlowPlans, so BOM-scaled consumption such as 20 chairs * 4 legs is enforced
  // as an actual quantity requirement. The lower bound is the buffer minimum
  // inventory target, using the minimum calendar when one is defined.
  unordered_set<const Buffer*> materialBuffers;
  for (auto* op : uniqueOps) {
    if (!op) continue;
    for (auto fp = op->beginFlowPlans(); fp != op->endFlowPlans(); ++fp) {
      const Buffer* buf = fp->getBuffer();
      if (buf && fabs(fp->getQuantity()) > ROUNDING_ERROR)
        materialBuffers.insert(buf);
    }
  }

  for (const Buffer* buf : materialBuffers) {
    vector<double> external(bucketCount, 0.0);
    external[0] += buf->getOnHand(current, false);

    for (auto fp = buf->getFlowPlans().begin();
         fp != buf->getFlowPlans().end(); ++fp) {
      OperationPlan* fpOp = fp->getOperationPlan();
      if (fpOp && uniqueOps.count(fpOp)) continue;
      if (fpOp && fpOp->getOperation() &&
          fpOp->getOperation()->hasType<OperationItemSupplier,
                                       OperationItemDistribution>())
        continue;
      if (fp->getDate() == Date::infiniteFuture) continue;
      if (fabs(fp->getQuantity()) <= ROUNDING_ERROR) continue;

      long offset = (fp->getDate() - horizonStart).getSeconds();
      int bucket = offset <= 0
          ? 0
          : static_cast<int>(
                min<long>(bucketCount - 1, offset / bucketSeconds));
      external[bucket] += fp->getQuantity();
    }

    double cumulativeExternal = 0.0;
    for (int bucket = 0; bucket < bucketCount; ++bucket) {
      cumulativeExternal += external[bucket];
      Date bucketDate = horizonStart + Duration(
          static_cast<long>(bucket) * static_cast<long>(bucketSeconds));
      double minimumInventory = buf->getMinimum();
      Calendar* minimumCalendar = buf->getMinimumCalendar();
      if (minimumCalendar) {
        CalendarBucket* minimumBucket =
            minimumCalendar->findBucket(bucketDate, true);
        minimumInventory = minimumBucket ? minimumBucket->getValue()
                                         : minimumCalendar->getDefault();
      }
      if (minimumInventory < 0.0) minimumInventory = 0.0;
      MPConstraint* balance =
          solver.MakeRowConstraint(minimumInventory - cumulativeExternal,
                                   solver.infinity());

      for (size_t idx = 0; idx < slots.size(); ++idx) {
        const CandidateOp* c = slots[idx].candidate;
        if (!c || !c->op) continue;
        double quantityThroughBucket = 0.0;
        for (auto fp = c->op->beginFlowPlans();
             fp != c->op->endFlowPlans(); ++fp) {
          if (fp->getBuffer() != buf ||
              fabs(fp->getQuantity()) <= ROUNDING_ERROR)
            continue;
          int eventBucket = fp->getQuantity() < 0.0
              ? slots[idx].startBucket
              : min(bucketCount - 1,
                    slots[idx].startBucket + slots[idx].durationBuckets);
          if (eventBucket <= bucket)
            quantityThroughBucket += fp->getQuantity();
        }
        if (fabs(quantityThroughBucket) > ROUNDING_ERROR)
          balance->SetCoefficient(slots[idx].var, quantityThroughBucket);
      }
    }
  }

  // Objective: minimize weighted tardiness and cost for selected slots.
  MPObjective* obj = solver.MutableObjective();
  obj->SetMinimization();
  for (const auto& slot : slots) {
    const CandidateOp* c = slot.candidate;
    const Resource* res = c->res ? c->res : c->allResources.front();
    double priorityFactor = 1.0;
    Demand* dmd = c->op->getTopOwner()->getDemand();
    if (dmd) {
      priorityFactor += config_.weight_priority /
          (1.0 + static_cast<double>(dmd->getPriority()));
    }
    double tardiness = 0.0;
    if (dmd && dmd->getDue() != Date::infiniteFuture &&
        slot.end > dmd->getDue())
      tardiness = static_cast<double>(
          (slot.end - dmd->getDue()).getSeconds()) / 3600.0;
    double durationHours = static_cast<double>(
        (slot.end - slot.start).getSeconds()) / 3600.0;
    double cost = c->op->getOperation()->getCost() * c->op->getQuantity() +
                  res->getCost() * max(0.0, durationHours);
    double compactness = static_cast<double>(slot.startBucket) * 0.001;
    obj->SetCoefficient(
        slot.var,
        config_.weight_tardiness * tardiness * priorityFactor +
            config_.weight_cost * cost + compactness);
  }

  // Solve
  logger << indentlevel << "Exact time-bucket MIP: " << uniqueOps.size()
         << " ops, " << slots.size() << " binary vars, "
         << bucketCount << " buckets of " << bucketSeconds << " seconds\n";

  MPSolver::ResultStatus status = solver.Solve();
  logger << indentlevel << "Exact time-bucket MIP SCIP status="
         << static_cast<int>(status);
  if (status == MPSolver::OPTIMAL || status == MPSolver::FEASIBLE)
    logger << " objective=" << obj->Value();
  logger << "\n";

  if (status != MPSolver::OPTIMAL && status != MPSolver::FEASIBLE) {
    logger << indentlevel << "Exact time-bucket MIP: no feasible solution\n";
    return empty;
  }

  AntSolution result;
  unordered_set<const OperationPlan*> scheduled;
  vector<const VarSlot*> selected;
  for (const auto& slot : slots)
    if (slot.var->solution_value() > 0.5) selected.push_back(&slot);
  sort(selected.begin(), selected.end(), [](const VarSlot* a,
                                            const VarSlot* b) {
    return a->start < b->start;
  });
  for (const auto* slot : selected) {
    const CandidateOp* c = slot->candidate;
    if (!c || !c->op || scheduled.count(c->op)) continue;
    for (auto* r : c->allResources) {
      result.sequences[r].push_back(c->op);
      result.startDates[r].push_back(slot->start);
      result.endDates[r].push_back(slot->end);
    }
    result.selectedResources[c->op] = c->allResources;
    result.selectedLoadAssignments[c->op] = c->loadAssignments;
    scheduled.insert(c->op);
  }
  result.fitness = evaluate(result);
  logger << indentlevel << "Exact time-bucket MIP: scheduled "
         << scheduled.size()
         << " ops, fitness=" << result.fitness << "\n";
  return result;
#endif
}

// ==========================================================================
// Keep MRP result
// ==========================================================================

void SolverExact::keepMRPResult(const char* reason) const {
  // Exact methods are bounded; if they fail, leave the MRP plan untouched.
  if (getLogLevel() < 0) return;
  logger << indentlevel << "Exact: keep MRP result";
  if (reason) logger << " (" << reason << ")";
  logger << "\n";
}

void SolverExact::solveJoint(const vector<const Resource*>& resources) {
  // Optimize all candidate resources as one coupled problem.
  if (resources.empty()) return;
  if (getLogLevel() >= 0)
    logger << indentlevel << "Exact joint: buildCandidates start, resources="
           << resources.size() << "\n";
  vector<CandidateOp> candidates = buildCandidates(resources);
  if (getLogLevel() >= 0)
    logger << indentlevel << "Exact joint: " << candidates.size()
           << " candidates across " << resources.size() << " resources\n";

  if (candidates.size() < 2) return;
  if (candidates.size() > static_cast<size_t>(config_.max_operations)) {
    if (getLogLevel() >= 0)
      logger << indentlevel << "Exact joint: too many candidates\n";
    keepMRPResult("joint candidate limit exceeded");
    return;
  }

  // --- Tier 1: Joint MIP over all resources ---
  AntSolution best;
  best.fitness = -numeric_limits<double>::max();
  if (config_.prefer_mip) {
    best = solveTimeIndexedMIP(resources, candidates);
    if (best.fitness != -numeric_limits<double>::max())
      logger << indentlevel << "Exact joint MIP: solved with fitness="
             << best.fitness << "\n";
  }

  // --- Tier 2: keep MRP result ---
  if (best.fitness == -numeric_limits<double>::max()) {
    if (getLogLevel() >= 0)
      logger << indentlevel << "Exact joint MIP: no feasible solution\n";
    keepMRPResult("joint MIP disabled or failed");
    return;
  }

  applyBestSolution(best);
  exactApplied_ = true;
  if (getLogLevel() >= 0)
    logger << indentlevel << "Exact joint: applied " << resources.size()
           << " resources, " << candidates.size()
           << " candidates, fitness=" << best.fitness << "\n";
}

// ==========================================================================
// Main exact flow
// ==========================================================================

void SolverExact::solve(void* v) {
  // Top-level flow:
  //   1. refresh inherited ACO settings
  //   2. collect candidate resources from the current LoadPlans
  //   3. run one exact optimization pass
  //   4. if exact changed the schedule, optionally run one locked MRP pass
  //   5. always clear AcoLocked, even when MRP throws
  //
  // There is intentionally no Exact -> MRP -> Exact loop here.
  syncACOConfig();
  exactApplied_ = false;
  vector<const Resource*> bottlenecks = collectBottlenecks();
  if (getLogLevel() >= 0)
    logger << indentlevel << "Exact: entry, found " << bottlenecks.size()
           << " candidate resources\n";

  if (bottlenecks.empty()) {
    if (config_.runMRP) SolverCreate::solve(v);
    return;
  }

  solveJoint(bottlenecks);

  try {
    if (exactApplied_ && config_.runMRP) SolverCreate::solve(v);
  } catch (...) {
    for (auto op = OperationPlan::begin(); op != OperationPlan::end(); ++op)
      op->setAcoLocked(false);
    throw;
  }
  for (auto op = OperationPlan::begin(); op != OperationPlan::end(); ++op)
    op->setAcoLocked(false);
}

}  // namespace ccAPPS

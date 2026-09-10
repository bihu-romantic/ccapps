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
#include <cctype>
#include <cfloat>
#include <cstdlib>
#include <functional>
#include <limits>
#include <unordered_set>

#include "ccAPPS/solveraco.h"

namespace ccAPPS {

const MetaClass* SolverACO::metadata;

namespace {

long long numericReference(const OperationPlan* op) {
  if (!op) return numeric_limits<long long>::max();
  const string& ref = op->getReference();
  if (ref.empty()) return numeric_limits<long long>::max();

  long long value = 0;
  bool seenDigit = false;
  for (char c : ref) {
    if (c >= '0' && c <= '9') {
      seenDigit = true;
      if (value <= (numeric_limits<long long>::max() - 9) / 10)
        value = value * 10 + (c - '0');
    }
  }
  return seenDigit ? value : numeric_limits<long long>::max();
}

bool operationPlanBatchLess(const OperationPlan* a, const OperationPlan* b) {
  if (a == b) return false;
  const Operation* opa = a ? a->getOperation() : nullptr;
  const Operation* opb = b ? b->getOperation() : nullptr;
  if (opa != opb) {
    const string empty;
    const string& nameA = opa ? opa->getName() : empty;
    const string& nameB = opb ? opb->getName() : empty;
    if (nameA != nameB) return nameA < nameB;
  }

  Demand* demandA = a ? a->getTopOwner()->getDemand() : nullptr;
  Demand* demandB = b ? b->getTopOwner()->getDemand() : nullptr;
  if (demandA != demandB) {
    const string empty;
    const string& nameA = demandA ? demandA->getName() : empty;
    const string& nameB = demandB ? demandB->getName() : empty;
    if (nameA != nameB) return nameA < nameB;
  }

  long long refA = numericReference(a);
  long long refB = numericReference(b);
  if (refA != refB) return refA < refB;
  string nameA = a ? a->getReference() : string();
  string nameB = b ? b->getReference() : string();
  if (nameA != nameB) return nameA < nameB;
  return a < b;
}

int getEnvInt(const char* name, int fallback, int minimum = 1) {
  const char* value = getenv(name);
  if (!value || !*value) return fallback;
  char* end = nullptr;
  long parsed = strtol(value, &end, 10);
  if (end == value || parsed < minimum) return fallback;
  if (parsed > numeric_limits<int>::max()) return fallback;
  return static_cast<int>(parsed);
}

struct ACOMaterialEvent {
  const Buffer* buffer = nullptr;
  Date date;
  double quantity = 0.0;
};

bool isTruthy(const char* value) {
  if (!value) return false;
  string normalized;
  for (const char* p = value; *p; ++p)
    normalized.push_back(static_cast<char>(
        tolower(static_cast<unsigned char>(*p))));
  return normalized == "1" || normalized == "true" ||
      normalized == "yes" || normalized == "on";
}

Date transferBatchProgressDate(const OperationPlan* op, Date start, Date end,
                               int batchIndex, int batchCount,
                               bool consumer) {
  if (!op || !op->getOperation() || batchCount <= 0)
    return consumer ? start : end;

  Duration opDelta;
  Date opDate =
      op->getOperation()->calculateOperationTime(op, start, end, &opDelta)
          .getStart();
  long totalSeconds = opDelta.getSeconds();
  long offsetSeconds = consumer
      ? totalSeconds * batchIndex / batchCount
      : totalSeconds * (batchIndex + 1) / batchCount;
  return op->getOperation()
      ->calculateOperationTime(op, opDate, Duration(offsetSeconds), true)
      .getEnd();
}

Date transferBatchEventDate(
    const FlowPlan* fp, Date start, Date end, bool useTransferBatch) {
  if (!fp || !fp->getFlow() || !fp->getOperationPlan()) return end;
  if (!useTransferBatch || !fp->getFlow()->hasType<FlowTransferBatch>()) {
    return fp->getQuantity() < 0.0 ? start : end;
  }

  int batchIndex = 0;
  int batchCount = 0;
  const Flow* flow = fp->getFlow();
  const OperationPlan* op = fp->getOperationPlan();
  for (auto it = op->beginFlowPlans(); it != op->endFlowPlans(); ++it) {
    if (it->getFlow() != flow) continue;
    if (&*it == fp) batchIndex = batchCount;
    ++batchCount;
  }
  if (batchCount <= 0) return fp->getQuantity() < 0.0 ? start : end;

  return transferBatchProgressDate(op, start, end, batchIndex, batchCount,
                                   fp->getQuantity() < 0.0);
}

void appendTransferBatchMaterialEvents(vector<ACOMaterialEvent>& events,
                                       const Flow* flow,
                                       const OperationPlan* op, Date start,
                                       Date end) {
  if (!flow || !op || !flow->hasType<FlowTransferBatch>()) return;
  const auto* transferFlow = static_cast<const FlowTransferBatch*>(flow);
  double transferBatch = transferFlow->getTransferBatch();

  const Buffer* buffer = nullptr;
  double totalQuantity = 0.0;
  for (auto fp = op->beginFlowPlans(); fp != op->endFlowPlans(); ++fp) {
    if (fp->getFlow() != flow) continue;
    if (!buffer) buffer = fp->getBuffer();
    totalQuantity += fp->getQuantity();
  }
  if (!buffer || fabs(totalQuantity) <= ROUNDING_ERROR) return;

  if (transferBatch <= ROUNDING_ERROR) {
    events.push_back(
        {buffer, totalQuantity < 0.0 ? start : end, totalQuantity});
    return;
  }

  double absTotal = fabs(totalQuantity);
  int batchCount = static_cast<int>(ceil(absTotal / transferBatch));
  if (batchCount < 1) batchCount = 1;
  double batchQuantity = transferBatch;
  if (batchCount > 50) {
    batchCount = 50;
    batchQuantity = absTotal / static_cast<double>(batchCount);
  }

  double sign = totalQuantity < 0.0 ? -1.0 : 1.0;
  for (int idx = 0; idx < batchCount; ++idx) {
    double remaining = absTotal - batchQuantity * static_cast<double>(idx);
    if (remaining <= ROUNDING_ERROR) break;
    double quantity = min(batchQuantity, remaining) * sign;
    Date eventDate = transferBatchProgressDate(
        op, start, end, idx, batchCount, quantity < 0.0);
    events.push_back({buffer, eventDate, quantity});
  }
}

vector<ACOMaterialEvent> materialEventsForOperation(
    const OperationPlan* op, Date start, Date end, bool useTransferBatch) {
  vector<ACOMaterialEvent> events;
  if (!op) return events;
  unordered_set<const Flow*> handledTransferFlows;
  for (auto fp = op->beginFlowPlans(); fp != op->endFlowPlans(); ++fp) {
    const Flow* flow = fp->getFlow();
    if (useTransferBatch && flow && flow->hasType<FlowTransferBatch>()) {
      if (handledTransferFlows.insert(flow).second)
        appendTransferBatchMaterialEvents(events, flow, op, start, end);
      continue;
    }
    const Buffer* buf = fp->getBuffer();
    if (!buf || fabs(fp->getQuantity()) <= ROUNDING_ERROR) continue;
    Date eventDate =
        transferBatchEventDate(&*fp, start, end, useTransferBatch);
    events.push_back({buf, eventDate, fp->getQuantity()});
  }
  return events;
}

}  // namespace

// ==========================================================================
// PheromoneMatrix
// ==========================================================================

double PheromoneMatrix::get(const OperationPlan* from,
                            const OperationPlan* to) const {
  auto it = matrix_.find({from, to});
  if (it != matrix_.end()) return it->second;
  return 0.0;
}

void PheromoneMatrix::set(const OperationPlan* from, const OperationPlan* to,
                          double value) {
  matrix_[{from, to}] = value;
}

void PheromoneMatrix::evaporate(double rho) {
  for (auto& kv : matrix_) kv.second *= (1.0 - rho);
}

void PheromoneMatrix::deposit(const vector<OperationPlan*>& sequence,
                              double amount) {
  const OperationPlan* prev = nullptr;
  for (const auto& op : sequence) {
    auto key = make_pair(prev, op);
    auto it = matrix_.find(key);
    if (it != matrix_.end())
      it->second += amount;
    else
      matrix_[key] = amount;
    prev = op;
  }
}

void PheromoneMatrix::reset(double /* tau0 */) { matrix_.clear(); }

// ==========================================================================
// SolverACO Python init
// ==========================================================================

static PyObject* solverACO_initPheromone(PyObject* self, PyObject* args) {
  auto* solver = static_cast<SolverACO*>(self);
  PyObject* py_resource = nullptr;
  if (!PyArg_ParseTuple(args, "O", &py_resource)) return nullptr;
  if (!py_resource || !PyObject_HasAttrString(py_resource, "getName")) {
    PyErr_SetString(PyExc_TypeError, "Expected a Resource object");
    return nullptr;
  }
  auto* res = static_cast<Resource*>(
      PyCapsule_GetPointer(py_resource, "ccAPPS.Resource"));
  if (!res) { PyErr_SetString(PyExc_TypeError, "Expected a Resource capsule"); return nullptr; }
  solver->initPheromone(res);
  Py_RETURN_NONE;
}

static PyObject* solverACO_setRunMRP(PyObject* self, PyObject* args) {
  auto* solver = static_cast<SolverACO*>(self);
  int val;
  if (!PyArg_ParseTuple(args, "p", &val)) return nullptr;
  solver->setRunMRP(val != 0);
  Py_RETURN_NONE;
}

static PyObject* solverACO_getRunMRP(PyObject* self, PyObject*) {
  auto* solver = static_cast<SolverACO*>(self);
  return PyBool_FromLong(solver->getRunMRP() ? 1 : 0);
}

static PyObject* solverACO_setPurchaseMaterialMode(
    PyObject* self, PyObject* args) {
  auto* solver = static_cast<SolverACO*>(self);
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

static PyObject* solverACO_getPurchaseMaterialMode(PyObject* self, PyObject*) {
  auto* solver = static_cast<SolverACO*>(self);
  return PyLong_FromLong(solver->getPurchaseMaterialMode());
}

static PyObject* solverACO_setTransferBatch(PyObject* self, PyObject* args) {
  auto* solver = static_cast<SolverACO*>(self);
  int val;
  if (!PyArg_ParseTuple(args, "p", &val)) return nullptr;
  solver->setTransferBatch(val != 0);
  Py_RETURN_NONE;
}

static PyObject* solverACO_getTransferBatch(PyObject* self, PyObject*) {
  auto* solver = static_cast<SolverACO*>(self);
  return PyBool_FromLong(solver->getTransferBatch() ? 1 : 0);
}

/* Global function: run ACO on constrained resources.
 * Called from Python as ccAPPS.run_aco(). */
PyObject* run_aco(PyObject*, PyObject*) {
  SolverACO aco;
  aco.setRunMRP(false);
  aco.setTransferBatch(isTruthy(getenv("ACO_TRANSFER_BATCH")));
  ACOConfig cfg = aco.getConfig();
  cfg.ants = getEnvInt("ACO_ANTS", cfg.ants);
  cfg.iterations = getEnvInt("ACO_ITERATIONS", cfg.iterations);
  cfg.elite_ants = getEnvInt("ACO_ELITE_ANTS", cfg.elite_ants);
  cfg.stagnation_limit = getEnvInt("ACO_STAGNATION_LIMIT", cfg.stagnation_limit);
  aco.setConfig(cfg);
  void* v = nullptr;
  aco.solve(v);
  // Persist ACO's schedule changes
  auto* cmdMgr = aco.getCommandManager();
  if (cmdMgr) cmdMgr->commit();
  Py_RETURN_NONE;
}

int SolverACO::initialize() {
  metadata = MetaClass::registerClass<SolverACO>("solver", "solver_aco",
                                                  Object::create<SolverACO>);
  registerFields<SolverACO>(const_cast<MetaClass*>(metadata));
  auto& x = PythonExtension<SolverACO>::getPythonType();
  x.setName("solverACO");
  x.setDoc("ccAPPS ant colony optimization solver");
  x.supportgetattro(); x.supportsetattro(); x.supportcreate(SolverACO::create);

  // Inherit essential methods from SolverCreate (same as solver_mrp)
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

  // ACO-specific methods
  x.addMethod("initPheromone", solverACO_initPheromone, METH_VARARGS,
              "Initialize pheromone matrix for a resource");
  x.addMethod("setRunMRP", solverACO_setRunMRP, METH_VARARGS,
              "Enable/disable MRP propagation after ACO sequencing");
  x.addMethod("getRunMRP", solverACO_getRunMRP, METH_NOARGS,
              "Check if MRP propagation is enabled");
  x.addMethod("setPurchaseMaterialMode", solverACO_setPurchaseMaterialMode,
              METH_VARARGS,
              "Set ACO purchase material mode: 0=infinite, 1=leadtime");
  x.addMethod("getPurchaseMaterialMode", solverACO_getPurchaseMaterialMode,
              METH_NOARGS, "Get ACO purchase material mode");
  x.addMethod("setTransferBatch", solverACO_setTransferBatch, METH_VARARGS,
              "Enable/disable FlowTransferBatch timings in ACO");
  x.addMethod("getTransferBatch", solverACO_getTransferBatch, METH_NOARGS,
              "Check if ACO FlowTransferBatch timings are enabled");

  SolverACO::metadata->setPythonClass(x);
  return x.typeReady();
}

PyObject* SolverACO::create(PyTypeObject*, PyObject*, PyObject*) {
  return Object::create<SolverACO>();
}

// ==========================================================================
// Setup time helper
// ==========================================================================

Duration SolverACO::computeSetupTime(const OperationPlan* from,
                                     const OperationPlan* to) const {
  if (!from || !to) return Duration(0L);
  if (from->getOperation() == to->getOperation()) return Duration(0L);
  PooledString fromSetup, toSetup;
  const SetupMatrix* matrix = nullptr;
  for (auto fl = from->beginLoadPlans(); fl != from->endLoadPlans(); ++fl) {
    if (fl->isStart() && fl->getResource() && fl->getResource()->getConstrained()) {
      fromSetup = fl->getSetupLoad();
      matrix = fl->getResource()->getSetupMatrix();
      break;
    }
  }
  for (auto tl = to->beginLoadPlans(); tl != to->endLoadPlans(); ++tl) {
    if (tl->isStart() && tl->getResource() && tl->getResource()->getConstrained()) {
      toSetup = tl->getSetupLoad();
      break;
    }
  }
  if (fromSetup.empty() && toSetup.empty()) return Duration(0L);
  if (!matrix) return Duration(0L);
  SetupMatrixRule* rule = matrix->calculateSetup(fromSetup, toSetup);
  return rule ? rule->getDuration() : Duration(0L);
}

// ==========================================================================
// Heuristic (factors 2+3: setup time + due date)
// ==========================================================================

double SolverACO::heuristic(const OperationPlan* from,
                            const OperationPlan* to) const {
  if (!to) return 0.0;
  double h = 1.0;
  // Factor 2: Setup time — prefer less setup
  if (from) {
    Duration setup = computeSetupTime(from, to);
    if (setup > Duration(0L))
      h *= 1.0 / (1.0 + static_cast<double>(setup.getSeconds()) / 3600.0);
  }
  // Factor: Demand priority — prefer higher-priority orders
  Demand* dmd = to->getTopOwner()->getDemand();
  if (dmd) {
    int dmdPrio = dmd->getPriority();
    h *= 1.0 + config_.weight_priority / (1.0 + static_cast<double>(dmdPrio));
  } else {
    // Fallback: operation-level priority
    double prio = to->getOperation()->getPriority();
    if (prio > 0.0) h *= (1.0 + prio * 0.1);
  }
  // Factor 3: Due date urgency — prefer more urgent
  if (dmd && dmd->getDue() != Date::infiniteFuture) {
    auto urgency = static_cast<double>(
        (dmd->getDue() - Plan::instance().getCurrent()).getSeconds()) / 86400.0;
    if (urgency > 0.0)
      h *= max(1.0, 10.0 / (1.0 + urgency));
    else
      h *= 100.0;  // already overdue → very high priority
  }
  return max(h, 1e-6);
}

// ==========================================================================
// Duration estimation
// ==========================================================================

Duration estimateOperationDuration(const OperationPlan* op, const Resource* res) {
  if (!op || !op->getOperation()) return Duration(3600L);
  if (op->getOperation()->hasType<OperationTimePer>()) {
    auto* oper = static_cast<OperationTimePer*>(op->getOperation());
    // Keep the optimizer's duration consistent with the model writeback.
    // A fractional duration-per-quantity must reserve the final partial
    // second. Truncating it made a downstream WIP consumer start one second
    // before its upstream FlowPlan receipt after export.
    Duration dur = oper->getDuration() + Duration(static_cast<long>(ceil(
        oper->getDurationPer() * op->getQuantity())));
    if (dur > Duration(0L)) return dur;
  } else if (op->getOperation()->hasType<OperationFixedTime>()) {
    auto* oper = static_cast<OperationFixedTime*>(op->getOperation());
    Duration dur = oper->getDuration();
    if (dur > Duration(0L)) return dur;
  }
  Duration ld = Duration(0L);
  for (auto l = op->getOperation()->getLoads().begin();
       l != op->getOperation()->getLoads().end(); ++l) {
    if (l->getResource() == res && l->getQuantity() > 0.0) {
      ld = Duration(static_cast<long>(l->getQuantity() * op->getQuantity() * 3600.0));
      if (ld > Duration(0L)) break;
    }
  }
  if (ld > Duration(0L)) return ld;
  if (op->getEnd() != Date::infiniteFuture &&
      op->getStart() > Date::infinitePast) {
    Duration dur = op->getEnd() - op->getStart();
    if (dur > Duration(0L)) return dur;
  }
  Date fence = op->getOperation()->getFence(op);
  if (fence != Date::infiniteFuture) {
    Duration d = fence - Plan::instance().getCurrent();
    if (d > Duration(0L)) return d;
  }
  return Duration(3600L);
}

// ==========================================================================
// Factor 5: Material availability
// ==========================================================================

Date SolverACO::purchaseMaterialAvailable(const Buffer* buf) const {
  if (!buf || !buf->getItem()) return Date::infiniteFuture;
  Date current = Plan::instance().getCurrent();
  Date earliest = Date::infiniteFuture;

  Item* item = buf->getItem();
  while (item) {
    Item::supplierlist::const_iterator supitem_iter =
        item->getSupplierIterator();
    while (ItemSupplier* supitem = supitem_iter.next()) {
      if (!supitem->getPriority()) continue;
      if (!supitem->getEffective().within(current)) continue;

      // Match the same location applicability rule used when buffers create
      // purchase operations from item suppliers.
      if (supitem->getLocation()) {
        if ((buf->getLocation() && buf->getLocation() != supitem->getLocation()) ||
            !buf->getLocation())
          continue;
      }

      Date available = config_.purchase_material_mode == 0
          ? current
          : current + supitem->getLeadTime();
      if (available < earliest) earliest = available;
    }
    item = item->getOwner();
  }

  return earliest;
}

Date SolverACO::earliestStart(const OperationPlan* op, const Resource* res) const {
  Date earliest = Plan::instance().getCurrent();

  // Check each consuming flowplan — find the latest upstream completion date
  for (auto fp = op->beginFlowPlans(); fp != op->endFlowPlans(); ++fp) {
    if (fp->getQuantity() >= 0.0) continue;  // skip producing flows
    Buffer* buf = fp->getBuffer();
    if (!buf) continue;
    double requiredQty = -fp->getQuantity();
    if (requiredQty <= ROUNDING_ERROR) continue;
    // Find when material first becomes available. Current stock can be used
    // immediately; purchasable material follows the configured ACO purchase
    // material mode instead of waiting for an existing PO flowplan.
    double currentOnhand =
        buf->getOnHand(Plan::instance().getCurrent(), false);
    if (currentOnhand + ROUNDING_ERROR >= requiredQty)
      continue;  // enough on hand now
    Date purchaseDate = purchaseMaterialAvailable(buf);
    if (purchaseDate != Date::infiniteFuture) {
      if (purchaseDate > earliest) earliest = purchaseDate;
      continue;
    }
    // Scan flowplans to find the first date where cumulative supply covers the
    // required quantity. A non-negative net balance isn't sufficient: an
    // operation can need 128 units while current stock is only 0.
    double balance = currentOnhand;
    for (auto sfp = buf->getFlowPlans().begin();
         sfp != buf->getFlowPlans().end(); ++sfp) {
      if (sfp->getOperationPlan() == op) continue;
      if (sfp->getDate() <= Plan::instance().getCurrent() ||
          sfp->getDate() == Date::infiniteFuture)
        continue;
      balance += sfp->getQuantity();
      if (balance + ROUNDING_ERROR >= requiredQty) {
        Date available = sfp->getDate();
        OperationPlan* supplier = sfp->getOperationPlan();
        if (supplier && supplier->getEnd() != Date::infiniteFuture &&
            supplier->getEnd() > available)
          available = supplier->getEnd();
        if (available > earliest) earliest = available;
        break;
      }
    }
  }

  // Factor 4: Upstream dependency — also check isUpstreamBlocked
  // This is handled more fully during construction via isUpstreamBlocked()

  return earliest;
}

// ==========================================================================
// Factor 5b: Dynamic material availability clock
// Computes earliest start based on the dynamic material clock updated during
// ant construction. As the ant places operations across resources, the
// material clock tracks when each buffer receives material from upstream
// operations already scheduled.
// ==========================================================================

Date SolverACO::dynamicEarliestStart(
    const OperationPlan* op,
    const unordered_map<const Buffer*, Date>& materialAvailable) const {
  Date earliest = Plan::instance().getCurrent();
  if (!op) return earliest;
  for (auto fp = op->beginFlowPlans(); fp != op->endFlowPlans(); ++fp) {
    if (fp->getQuantity() >= 0.0) continue;  // skip producing flows
    const Buffer* buf = fp->getBuffer();
    if (!buf) continue;
    auto it = materialAvailable.find(buf);
    if (it != materialAvailable.end() && it->second > earliest)
      earliest = it->second;
  }
  return earliest;
}

// ==========================================================================
// Factor 4: Cross-resource upstream blocking
// ==========================================================================

bool SolverACO::isUpstreamBlocked(
    const OperationPlan* op,
    const unordered_map<const Resource*, Date>& resourceTimes) const {
  if (!op) return false;
  for (auto fl = op->beginFlowPlans(); fl != op->endFlowPlans(); ++fl) {
    if (fl->getQuantity() >= 0.0) continue;
    Buffer* buf = fl->getBuffer();
    if (!buf) continue;
    for (auto pfp = buf->getFlowPlans().begin();
         pfp != buf->getFlowPlans().end(); ++pfp) {
      OperationPlan* producer = pfp->getOperationPlan();
      if (!producer || producer == op) continue;
      for (auto lp = producer->beginLoadPlans();
           lp != producer->endLoadPlans(); ++lp) {
        const Resource* prodRes = lp->getResource();
        if (!prodRes || !prodRes->getConstrained()) continue;
        Date refTime = Plan::instance().getCurrent();
        auto it = resourceTimes.find(prodRes);
        if (it != resourceTimes.end()) refTime = it->second;
        if (producer->getEnd() > refTime &&
            producer->getEnd() != Date::infiniteFuture)
          return true;
      }
    }
  }
  return false;
}

// ==========================================================================
// Factor 4b: Continuous upstream wait time
// Instead of a binary blocked/not-blocked signal, returns the cumulative
// wait duration until all upstream producers on constrained resources
// have completed. Used to compute a continuous readiness factor for
// smoother probability landscapes in ant construction.
// ==========================================================================

Duration SolverACO::computeUpstreamWait(
    const OperationPlan* op,
    const unordered_map<const Resource*, Date>& resourceTimes) const {
  if (!op) return Duration(0L);
  Duration totalWait(0L);
  for (auto fl = op->beginFlowPlans(); fl != op->endFlowPlans(); ++fl) {
    if (fl->getQuantity() >= 0.0) continue;
    Buffer* buf = fl->getBuffer();
    if (!buf) continue;
    for (auto pfp = buf->getFlowPlans().begin();
         pfp != buf->getFlowPlans().end(); ++pfp) {
      OperationPlan* producer = pfp->getOperationPlan();
      if (!producer || producer == op) continue;
      for (auto lp = producer->beginLoadPlans();
           lp != producer->endLoadPlans(); ++lp) {
        const Resource* prodRes = lp->getResource();
        if (!prodRes || !prodRes->getConstrained()) continue;
        // Use tracked ACO clock if available; otherwise fall back to plan
        // current time (resource not being optimized — MRP schedule untouched).
        Date refTime = Plan::instance().getCurrent();
        auto it = resourceTimes.find(prodRes);
        if (it != resourceTimes.end()) refTime = it->second;
        if (producer->getEnd() > refTime &&
            producer->getEnd() != Date::infiniteFuture) {
          Duration wait = producer->getEnd() - refTime;
          if (wait > totalWait) totalWait = wait;
        }
      }
    }
  }
  return totalWait;
}

// ==========================================================================
// Helper: expand constrained resource groups like MRP does
// ==========================================================================

static void collectQualifiedResources(const Resource* res, Skill* skill,
                                      Date start, Date end,
                                      vector<const Resource*>& result) {
  if (!res || !res->getConstrained()) return;
  if (res->isGroup()) {
    for (auto m = res->getMembers(); m != Resource::end(); ++m)
      collectQualifiedResources(&*m, skill, start, end, result);
    return;
  }
  if (skill && !res->hasSkill(skill, start, end)) return;
  if (find(result.begin(), result.end(), res) == result.end())
    result.push_back(res);
}

vector<const Resource*> SolverACO::getConstrainedResources(
    const OperationPlan* op) const {
  vector<const Resource*> result;
  if (!op || !op->getOperation()) return result;
  for (const auto& ld : op->getOperation()->getLoads()) {
    collectQualifiedResources(ld.getResource(), ld.getSkill(), op->getStart(),
                              op->getEnd(), result);
  }
  return result;
}

// ==========================================================================
// Factor 1: Build candidates from manufacturing orders
// Each MO with ≥1 resource load generates candidates
// ==========================================================================

vector<CandidateOp> SolverACO::buildCandidates(
    const vector<const Resource*>& resources) const {
  vector<CandidateOp> candidates;
  unordered_set<OperationPlan*> processed;
  int nextId = 0;

  for (auto* res : resources) {
    auto loadplans = res->getLoadPlans();
    for (auto it = loadplans.begin(); it != loadplans.end(); ++it) {
      OperationPlan* op = it->getOperationPlan();
      if (!op || op->getQuantity() <= 0.0) continue;
      // Skip non-manufacturing orders (purchases, distributions)
      if (op->getOperation()->hasType<OperationItemSupplier,
                                       OperationItemDistribution>())
        continue;

      if (processed.count(op)) continue;
      processed.insert(op);

      struct LoadAlternatives {
        const Load* load;
        vector<const Resource*> resources;
      };
      vector<LoadAlternatives> loadAlternatives;
      for (const auto& ld : op->getOperation()->getLoads()) {
        vector<const Resource*> alternatives;
        collectQualifiedResources(ld.getResource(), ld.getSkill(), op->getStart(),
                                  op->getEnd(), alternatives);
        if (alternatives.empty()) continue;
        sort(alternatives.begin(), alternatives.end(),
             [](const Resource* a, const Resource* b) {
               if (a->getEfficiency() == b->getEfficiency())
                 return a->getName() < b->getName();
               return a->getEfficiency() > b->getEfficiency();
             });
        loadAlternatives.push_back({&ld, move(alternatives)});
      }

      if (!loadAlternatives.empty()) {
        vector<pair<const Load*, const Resource*>> currentAssignments;
        vector<const Resource*> currentResources;
        size_t generatedForOp = 0;

        function<void(size_t)> buildCombinations = [&](size_t idx) {
          if (generatedForOp >=
              static_cast<size_t>(config_.max_candidate_combinations_per_op))
            return;
          if (idx == loadAlternatives.size()) {
            CandidateOp c;
            c.op = op;
            c.allResources = currentResources;
            c.loadAssignments = currentAssignments;
            c.res = !c.allResources.empty() ? c.allResources.back() : res;
            // In joint ACO we forward-schedule the complete candidate set.
            // Internal WIP dependencies are handled by the dynamic material
            // ledger during construction/compaction; using the MRP snapshot
            // here would anchor candidates to the old backward schedule.
            c.earliestStart = Plan::instance().getCurrent();
            c.candId = nextId++;
            candidates.push_back(c);
            ++generatedForOp;
            return;
          }

          const auto& alt = loadAlternatives[idx];
          for (auto* altRes : alt.resources) {
            bool alreadyAssigned = false;
            if (alt.load->getResource() && alt.load->getResource()->isGroup() &&
                Plan::instance().getIndividualPoolResources()) {
              for (auto* r : currentResources) {
                if (r == altRes) {
                  alreadyAssigned = true;
                  break;
                }
              }
            }
            if (alreadyAssigned) continue;

            currentAssignments.push_back({alt.load, altRes});
            if (find(currentResources.begin(), currentResources.end(), altRes) ==
                currentResources.end())
              currentResources.push_back(altRes);
            buildCombinations(idx + 1);
            currentAssignments.pop_back();
            bool stillUsed = false;
            for (const auto& assignment : currentAssignments) {
              if (assignment.second == altRes) {
                stillUsed = true;
                break;
              }
            }
            if (!stillUsed)
              currentResources.erase(
                  remove(currentResources.begin(), currentResources.end(), altRes),
                  currentResources.end());
          }
        };

        size_t before = candidates.size();
        buildCombinations(0);
        if (candidates.size() > before) continue;
      }

      {
        CandidateOp c;
        c.op = op;
        c.res = res;
        c.allResources.push_back(res);
        c.earliestStart = Plan::instance().getCurrent();
        c.candId = nextId;
        candidates.push_back(c);
      }
      ++nextId;
    }
  }
  return candidates;
}

// ==========================================================================
// Construct solution (single-resource legacy)
// ==========================================================================

AntSolution SolverACO::constructSolution(
    const Resource* res, const vector<OperationPlan*>& plans) {
  AntSolution ant;
  if (plans.empty()) return ant;

  vector<OperationPlan*> unvisited = plans;
  sort(unvisited.begin(), unvisited.end(), [](OperationPlan* a, OperationPlan* b) {
    return a->getOperation()->getPriority() < b->getOperation()->getPriority();
  });

  const OperationPlan* current = nullptr;
  Date cur = Plan::instance().getCurrent();

  // Build a minimal resource-times map so computeUpstreamWait can check
  // upstream dependencies on other constrained resources.
  unordered_map<const Resource*, Date> rt;
  rt[res] = cur;

  unordered_set<const OperationPlan*> planOps;
  for (auto* op : plans)
    if (op) planOps.insert(op);
  struct MaterialEvent {
    Date date;
    double quantity;
  };
  unordered_map<const Buffer*, vector<MaterialEvent>> materialLedger;
  unordered_set<const Buffer*> initializedBuffers;

  auto ensureMaterialLedger = [&](const Buffer* buf) -> void {
    if (!buf || initializedBuffers.count(buf)) return;
    initializedBuffers.insert(buf);
    Date currentDate = Plan::instance().getCurrent();
    auto& events = materialLedger[buf];
    double initial = buf->getOnHand(currentDate, false);
    if (fabs(initial) > ROUNDING_ERROR) events.push_back({currentDate, initial});
    for (auto fp = buf->getFlowPlans().begin();
         fp != buf->getFlowPlans().end(); ++fp) {
      OperationPlan* fpOp = fp->getOperationPlan();
      if (fpOp && planOps.count(fpOp)) continue;
      if (fp->getDate() <= currentDate ||
          fp->getDate() == Date::infiniteFuture)
        continue;
      if (fabs(fp->getQuantity()) <= ROUNDING_ERROR) continue;
      events.push_back({fp->getDate(), fp->getQuantity()});
    }
  };

  auto materialBalanceAt = [&](const Buffer* buf, Date date) -> double {
    ensureMaterialLedger(buf);
    double balance = 0.0;
    auto it = materialLedger.find(buf);
    if (it == materialLedger.end()) return balance;
    for (const auto& event : it->second) {
      if (event.date <= date)
        balance += event.quantity;
    }
    return balance;
  };

  auto materialAvailableDate =
      [&](const Buffer* buf, double requiredQty, Date fromDate) -> Date {
    ensureMaterialLedger(buf);
    Date purchaseDate = purchaseMaterialAvailable(buf);
    if (purchaseDate != Date::infiniteFuture && fromDate >= purchaseDate)
      return fromDate;

    vector<Date> dates;
    dates.push_back(fromDate);
    if (purchaseDate != Date::infiniteFuture && purchaseDate >= fromDate)
      dates.push_back(purchaseDate);
    auto it = materialLedger.find(buf);
    if (it != materialLedger.end())
      for (const auto& event : it->second)
        if (event.date >= fromDate) dates.push_back(event.date);

    sort(dates.begin(), dates.end());
    dates.erase(unique(dates.begin(), dates.end()), dates.end());
    for (Date date : dates) {
      if (purchaseDate != Date::infiniteFuture && date >= purchaseDate)
        return date;
      if (materialBalanceAt(buf, date) + ROUNDING_ERROR >= requiredQty)
        return date;
    }
    return Date::infiniteFuture;
  };

  auto operationMaterialDate = [&](const OperationPlan* op, Date fromDate) {
    Date materialDate = fromDate;
    if (!op) return materialDate;
    for (auto fl = op->beginFlowPlans(); fl != op->endFlowPlans(); ++fl) {
      if (fl->getQuantity() >= 0.0) continue;
      const Buffer* buf = fl->getBuffer();
      if (!buf) continue;
      Date available = materialAvailableDate(buf, -fl->getQuantity(), fromDate);
      if (available == Date::infiniteFuture) return available;
      if (available > materialDate) materialDate = available;
    }
    return materialDate;
  };

  while (!unvisited.empty()) {
    vector<double> probs(unvisited.size(), 0.0);
    double total = 0.0;
    for (size_t i = 0; i < unvisited.size(); ++i) {
      // Continuous readiness: 1/(1+waitHours) for upstream dependencies
      Duration upWait = computeUpstreamWait(unvisited[i], rt);
      double waitH = upWait > Duration(0L)
          ? static_cast<double>(upWait.getSeconds()) / 3600.0 : 0.0;
      double readiness = 1.0 / (1.0 + waitH);
      Date candidateStart =
          cur + computeSetupTime(current, unvisited[i]);
      Date materialDate = operationMaterialDate(unvisited[i], candidateStart);
      if (materialDate == Date::infiniteFuture) continue;
      double materialWaitH = materialDate > candidateStart
          ? static_cast<double>((materialDate - candidateStart).getSeconds()) /
                3600.0
          : 0.0;
      double materialReadiness = 1.0 / (1.0 + materialWaitH);
      double tau = pheromones_[res].get(current, unvisited[i]);
      if (tau <= 0.0) tau = config_.tau0;
      double eta = heuristic(current, unvisited[i]) * readiness *
          materialReadiness;
      probs[i] = pow(tau, config_.alpha) * pow(eta, config_.beta);
      total += probs[i];
    }
    if (total <= 0.0) {
      ant.unscheduledCount = unvisited.size();
      ant.unscheduledPenalty =
          static_cast<double>(unvisited.size()) * 500.0;
      break;
    }
    double r = uniform_real_distribution<double>(0.0, total)(rng_);
    double cum = 0.0;
    size_t sel = unvisited.size() - 1;
    for (size_t i = 0; i < unvisited.size(); ++i) {
      cum += probs[i];
      if (cum >= r) { sel = i; break; }
    }
    OperationPlan* next = unvisited[sel];
    Duration setup = computeSetupTime(current, next);
    Date rawStart = cur + setup;
    Date materialDate = operationMaterialDate(next, rawStart);
    if (materialDate == Date::infiniteFuture) {
      ant.unscheduledCount = unvisited.size();
      ant.unscheduledPenalty =
          static_cast<double>(unvisited.size()) * 500.0;
      break;
    }
    rawStart = max(rawStart, materialDate);
    Duration dur = estimateOperationDuration(next, res);
    // Apply calendar constraints: map start+duration through
    // operation/resource/location availability calendars
    Date start, end;
    if (next->getOperation()) {
      DateRange range = next->getOperation()->calculateOperationTime(
          next, rawStart, dur, true);
      start = range.getStart();
      end = range.getEnd();
    } else {
      start = rawStart;
      end = rawStart + dur;
    }

    ant.sequences[res].push_back(next);
    ant.startDates[res].push_back(start);
    ant.endDates[res].push_back(end);

    for (const auto& event : materialEventsForOperation(
             next, start, end, config_.transfer_batch)) {
      ensureMaterialLedger(event.buffer);
      materialLedger[event.buffer].push_back({event.date, event.quantity});
    }

    unvisited.erase(unvisited.begin() + sel);
    current = next;
    cur = end;
    rt[res] = cur;  // keep resource clock in sync for upstream wait checks
  }
  return ant;
}

// ==========================================================================
// Construct joint solution using candidate pool
// ==========================================================================

AntSolution SolverACO::constructJointSolution(
    const vector<const Resource*>& resources,
    const vector<CandidateOp>& allCandidates,
    const unordered_map<const Resource*, Date>& resourceTimes) {

  AntSolution ant;
  if (allCandidates.empty()) return ant;

  // Per-resource state
  unordered_map<const Resource*, const OperationPlan*> prevOp;
  unordered_map<const Resource*, Date> curTime;
  for (auto* r : resources) {
    auto it = resourceTimes.find(r);
    curTime[r] = (it != resourceTimes.end()) ? it->second
                  : Plan::instance().getCurrent();
    prevOp[r] = nullptr;
  }
  for (auto& c : allCandidates) {
    for (auto* r : c.allResources) {
      if (!curTime.count(r)) {
        auto it = resourceTimes.find(r);
        curTime[r] = (it != resourceTimes.end()) ? it->second
                    : Plan::instance().getCurrent();
      }
      if (!prevOp.count(r)) prevOp[r] = nullptr;
    }
  }

  // Upstream priority map: operations that produce buffers consumed by
  // other candidates get a scheduling bonus. This helps respect process
  // routing order (e.g., Saw before Assemble).
  unordered_map<const OperationPlan*, int> upstreamScore;
  {
    // Collect buffers consumed by candidates
    unordered_map<const Buffer*, int> consumerCount;
    for (auto& c : allCandidates) {
      if (!c.op) continue;
      for (auto fp = c.op->beginFlowPlans(); fp != c.op->endFlowPlans(); ++fp) {
        if (fp->getQuantity() >= 0.0) continue;  // consuming flows
        const Buffer* buf = fp->getBuffer();
        if (buf) consumerCount[buf]++;
      }
    }
    // Score each candidate by how many downstream ops depend on its output
    for (auto& c : allCandidates) {
      if (!c.op) continue;
      int score = 0;
      for (auto fp = c.op->beginFlowPlans(); fp != c.op->endFlowPlans(); ++fp) {
        if (fp->getQuantity() <= 0.0) continue;  // producing flows
        const Buffer* buf = fp->getBuffer();
        if (buf) {
          auto it = consumerCount.find(buf);
          if (it != consumerCount.end()) score = max(score, it->second);
        }
      }
      if (score > 0) upstreamScore[c.op] = score;
    }
  }

  // Track unique operation plans. A single operation can have multiple
  // candidate resource assignments; once one candidate is chosen, all other
  // candidates for the same operation are implicitly excluded.
  unordered_set<const OperationPlan*> candidateOps;
  for (auto& c : allCandidates)
    if (c.op) candidateOps.insert(c.op);
  unordered_set<const OperationPlan*> scheduledOps;

  struct MaterialEvent {
    Date date;
    double quantity;
  };
  unordered_map<const Buffer*, vector<MaterialEvent>> materialLedger;
  unordered_set<const Buffer*> initializedBuffers;

  auto ensureMaterialLedger = [&](const Buffer* buf) -> void {
    if (!buf || initializedBuffers.count(buf)) return;
    initializedBuffers.insert(buf);
    Date current = Plan::instance().getCurrent();
    auto& events = materialLedger[buf];
    double initial = buf->getOnHand(current, false);
    if (fabs(initial) > ROUNDING_ERROR)
      events.push_back({current, initial});

    // Seed the ledger with external future events only. Old flowplans of ACO
    // candidate operations are ignored; this ant will place those operations
    // itself and append their consumption/production events below.
    for (auto fp = buf->getFlowPlans().begin();
         fp != buf->getFlowPlans().end(); ++fp) {
      OperationPlan* fpOp = fp->getOperationPlan();
      if (fpOp && candidateOps.count(fpOp)) continue;
      if (fp->getDate() <= current ||
          fp->getDate() == Date::infiniteFuture)
        continue;
      if (fabs(fp->getQuantity()) <= ROUNDING_ERROR) continue;
      events.push_back({fp->getDate(), fp->getQuantity()});
    }
  };

  auto materialBalanceAt = [&](const Buffer* buf, Date date) -> double {
    ensureMaterialLedger(buf);
    double balance = 0.0;
    auto it = materialLedger.find(buf);
    if (it == materialLedger.end()) return balance;
    for (const auto& event : it->second) {
      if (event.date <= date)
        balance += event.quantity;
    }
    return balance;
  };

  auto materialAvailableDate =
      [&](const Buffer* buf, double requiredQty, Date fromDate) -> Date {
    ensureMaterialLedger(buf);
    Date purchaseDate = purchaseMaterialAvailable(buf);
    if (purchaseDate != Date::infiniteFuture && fromDate >= purchaseDate)
      return fromDate;

    vector<Date> dates;
    dates.push_back(fromDate);
    if (purchaseDate != Date::infiniteFuture && purchaseDate >= fromDate)
      dates.push_back(purchaseDate);
    auto it = materialLedger.find(buf);
    if (it != materialLedger.end())
      for (const auto& event : it->second)
        if (event.date >= fromDate) dates.push_back(event.date);

    sort(dates.begin(), dates.end());
    dates.erase(unique(dates.begin(), dates.end()), dates.end());

    for (Date date : dates) {
      if (purchaseDate != Date::infiniteFuture && date >= purchaseDate)
        return date;  // purchased material is unlimited from this date on
      if (materialBalanceAt(buf, date) + ROUNDING_ERROR >= requiredQty)
        return date;
    }
    return Date::infiniteFuture;
  };

  auto operationMaterialDate = [&](const OperationPlan* op, Date fromDate) {
    Date materialDate = fromDate;
    if (!op) return materialDate;
    for (auto fl = op->beginFlowPlans(); fl != op->endFlowPlans(); ++fl) {
      if (fl->getQuantity() >= 0.0) continue;
      const Buffer* buf = fl->getBuffer();
      if (!buf) continue;
      Date available = materialAvailableDate(buf, -fl->getQuantity(), fromDate);
      if (available == Date::infiniteFuture) return available;
      if (available > materialDate) materialDate = available;
    }
    return materialDate;
  };

  auto isReady = [&](const OperationPlan* op, Date startCandidate) -> bool {
    if (!op) return false;
    for (auto fl = op->beginFlowPlans(); fl != op->endFlowPlans(); ++fl) {
      if (fl->getQuantity() >= 0.0) continue;
      if (operationMaterialDate(op, startCandidate) == Date::infiniteFuture)
        return false;
    }
    return true;
  };

  // Total remaining unique operations.
  size_t remaining = candidateOps.size();
  while (remaining > 0) {
    vector<const CandidateOp*> readyCandidates;
    vector<double> probs;
    double total = 0.0;

    for (auto& c : allCandidates) {
      if (!c.op || scheduledOps.count(c.op) || c.allResources.empty())
        continue;

      // Continuous readiness still helps rank candidates that depend on
      // external producers with an existing completion date.
      Duration upWait = computeUpstreamWait(c.op, curTime);
      double waitH = upWait > Duration(0L)
          ? static_cast<double>(upWait.getSeconds()) / 3600.0 : 0.0;
      double readiness = 1.0 / (1.0 + waitH);
      Date candidateStart = c.earliestStart;
      for (auto* r : c.allResources) {
        Duration setup = computeSetupTime(prevOp[r], c.op);
        candidateStart = max(candidateStart, curTime[r] + setup);
      }
      Date materialDate = operationMaterialDate(c.op, candidateStart);
      if (materialDate == Date::infiniteFuture) continue;
      candidateStart = max(candidateStart, materialDate);
      if (!isReady(c.op, candidateStart)) continue;
      double startWaitH = candidateStart > Plan::instance().getCurrent()
          ? static_cast<double>(
                (candidateStart - Plan::instance().getCurrent()).getSeconds()) /
                3600.0
          : 0.0;
      double startFactor = 1.0 / (1.0 + startWaitH);

      // Duration factor: prefer shorter operations to unlock resources faster.
      // Shorter operations → higher probability → faster bottleneck release.
      const Resource* durRes = !c.allResources.empty() ? c.allResources[0] : nullptr;
      Duration estDur = durRes ? estimateOperationDuration(c.op, durRes) : Duration(3600L);
      double durHours = static_cast<double>(estDur.getSeconds()) / 3600.0;
      double durationFactor = max(0.2, 1.0 / (1.0 + durHours * 0.05));

      double tau = 0.0;
      double eta = 0.0;
      for (auto* r : c.allResources) {
        double t = pheromones_[r].get(prevOp[r], c.op);
        if (t <= 0.0) t = config_.tau0;
        tau += t;

        double curLoadH = static_cast<double>(
            (curTime[r] - Plan::instance().getCurrent()).getSeconds()) / 3600.0;
        double loadBalance = 1.0 / (1.0 + curLoadH * 2.0);
        eta += heuristic(prevOp[r], c.op) * loadBalance;
      }
      tau /= static_cast<double>(c.allResources.size());
      eta /= static_cast<double>(c.allResources.size());

      double upstreamFactor = 1.0;
      auto usIt = upstreamScore.find(c.op);
      if (usIt != upstreamScore.end())
        upstreamFactor = 1.0 + 0.3 * usIt->second;

      double probability = pow(tau, config_.alpha) *
          pow(max(eta * readiness * upstreamFactor * startFactor * durationFactor, 1e-9),
              config_.beta);
      if (probability <= 0.0) continue;

      readyCandidates.push_back(&c);
      probs.push_back(probability);
      total += probability;
    }

    if (readyCandidates.empty() || total <= 0.0)
      break;  // Remaining operations are blocked by unscheduled upstream work.

    // Roulette selection over ready operation candidates. This makes the
    // operation choose its resource assignment, instead of a resource choosing
    // the next operation from its own pool.
    double r = uniform_real_distribution<double>(0.0, total)(rng_);
    double cum = 0.0;
    size_t sel = readyCandidates.size() - 1;
    for (size_t i = 0; i < readyCandidates.size(); ++i) {
      cum += probs[i];
      if (cum >= r) { sel = i; break; }
    }

    CandidateOp chosen = *readyCandidates[sel];

    // Compute rawStart across ALL required resources. The operation can only
    // begin when every required resource is free and all scheduled upstream
    // producers have made their material available.
    Date rawStart = chosen.earliestStart;
    for (auto* r : chosen.allResources) {
      Duration setup = computeSetupTime(prevOp[r], chosen.op);
      rawStart = max(rawStart, curTime[r] + setup);
    }
    Date matEarliest = operationMaterialDate(chosen.op, rawStart);
    rawStart = max(rawStart, matEarliest);

    const Resource* durationResource = chosen.res ? chosen.res
        : chosen.allResources.front();
    Duration dur = estimateOperationDuration(chosen.op, durationResource);
    Date start, end;
    if (chosen.op->getOperation()) {
      DateRange range = chosen.op->getOperation()->calculateOperationTime(
          chosen.op, rawStart, dur, true);
      start = range.getStart();
      end = range.getEnd();
    } else {
      start = rawStart;
      end = rawStart + dur;
    }

    // Write to ALL resources this operation occupies.
    for (auto* r : chosen.allResources) {
      ant.sequences[r].push_back(chosen.op);
      ant.startDates[r].push_back(start);
      ant.endDates[r].push_back(end);
      prevOp[r] = chosen.op;
      curTime[r] = end;
    }
    ant.selectedResources[chosen.op] = chosen.allResources;
    ant.selectedLoadAssignments[chosen.op] = chosen.loadAssignments;

    // Update the material ledger with the chosen operation's timed
    // consumption and production. This prevents later operations from
    // consuming the same on-hand or incoming stock twice.
    for (const auto& event :
         materialEventsForOperation(
             chosen.op, start, end, config_.transfer_batch)) {
      ensureMaterialLedger(event.buffer);
      materialLedger[event.buffer].push_back({event.date, event.quantity});
    }

    scheduledOps.insert(chosen.op);
    --remaining;
  }

  // Penalize unscheduled operations: they represent deadlocked work.
  if (remaining > 0) {
    // Don't append blocked operations with their original MRP dates. Doing so
    // turns a material deadlock into a seemingly complete but infeasible ACO
    // solution, and later compaction/evaluation can only penalize it. Keeping
    // them unscheduled makes material feasibility a hard construction rule.
    if (getLogLevel() > 1)
      logger << indentlevel << "ACO joint: " << remaining
             << " operations blocked by material availability\n";
  }

  // Penalize unscheduled operations: they represent deadlocked work.
  ant.unscheduledCount = remaining;
  if (remaining > 0) {
    double totalPrioWeight = 0.0;
    for (auto& c : allCandidates) {
      if (c.op && !scheduledOps.count(c.op)) {
        Demand* dmd = c.op->getTopOwner()->getDemand();
        int prio = dmd ? dmd->getPriority() : 999;
        totalPrioWeight += 1.0 + config_.weight_priority / (1.0 + static_cast<double>(prio));
      }
    }
    ant.unscheduledPenalty = totalPrioWeight * 500.0;
  } else {
    ant.unscheduledPenalty = 0.0;
  }

  return ant;
}

// ==========================================================================
// Single-resource local search (legacy)
// ==========================================================================

void SolverACO::localSearch(const Resource*, AntSolution& sol) {
  if (sol.sequences.empty()) return;
  auto& kv = *sol.sequences.begin();
  const Resource* res = kv.first;
  auto& seq = kv.second;
  if (seq.size() < 2) return;

  auto rebuildSingleResource = [&](const vector<OperationPlan*>& order,
                                   vector<Date>& starts,
                                   vector<Date>& ends) -> bool {
    unordered_set<const OperationPlan*> orderOps;
    for (auto* op : order)
      if (op) orderOps.insert(op);
    struct MaterialEvent {
      Date date;
      double quantity;
    };
    unordered_map<const Buffer*, vector<MaterialEvent>> materialLedger;
    unordered_set<const Buffer*> initializedBuffers;

    auto ensureMaterialLedger = [&](const Buffer* buf) -> void {
      if (!buf || initializedBuffers.count(buf)) return;
      initializedBuffers.insert(buf);
      Date current = Plan::instance().getCurrent();
      auto& events = materialLedger[buf];
      double initial = buf->getOnHand(current, false);
      if (fabs(initial) > ROUNDING_ERROR) events.push_back({current, initial});
      for (auto fp = buf->getFlowPlans().begin();
           fp != buf->getFlowPlans().end(); ++fp) {
        OperationPlan* fpOp = fp->getOperationPlan();
        if (fpOp && orderOps.count(fpOp)) continue;
        if (fp->getDate() <= current || fp->getDate() == Date::infiniteFuture)
          continue;
        if (fabs(fp->getQuantity()) <= ROUNDING_ERROR) continue;
        events.push_back({fp->getDate(), fp->getQuantity()});
      }
    };

    auto materialBalanceAt = [&](const Buffer* buf, Date date) -> double {
      ensureMaterialLedger(buf);
      double balance = 0.0;
      auto it = materialLedger.find(buf);
      if (it == materialLedger.end()) return balance;
      for (const auto& event : it->second) {
        if (event.date <= date)
          balance += event.quantity;
      }
      return balance;
    };

    auto materialAvailableDate =
        [&](const Buffer* buf, double requiredQty, Date fromDate) -> Date {
      ensureMaterialLedger(buf);
      Date purchaseDate = purchaseMaterialAvailable(buf);
      if (purchaseDate != Date::infiniteFuture && fromDate >= purchaseDate)
        return fromDate;

      vector<Date> dates;
      dates.push_back(fromDate);
      if (purchaseDate != Date::infiniteFuture && purchaseDate >= fromDate)
        dates.push_back(purchaseDate);
      auto it = materialLedger.find(buf);
      if (it != materialLedger.end())
        for (const auto& event : it->second)
          if (event.date >= fromDate) dates.push_back(event.date);

      sort(dates.begin(), dates.end());
      dates.erase(unique(dates.begin(), dates.end()), dates.end());
      for (Date date : dates) {
        if (purchaseDate != Date::infiniteFuture && date >= purchaseDate)
          return date;
        if (materialBalanceAt(buf, date) + ROUNDING_ERROR >= requiredQty)
          return date;
      }
      return Date::infiniteFuture;
    };

    auto operationMaterialDate = [&](const OperationPlan* op, Date fromDate) {
      Date materialDate = fromDate;
      if (!op) return materialDate;
      for (auto fl = op->beginFlowPlans(); fl != op->endFlowPlans(); ++fl) {
        if (fl->getQuantity() >= 0.0) continue;
        const Buffer* buf = fl->getBuffer();
        if (!buf) continue;
        Date available =
            materialAvailableDate(buf, -fl->getQuantity(), fromDate);
        if (available == Date::infiniteFuture) return available;
        if (available > materialDate) materialDate = available;
      }
      return materialDate;
    };

    starts.clear();
    ends.clear();
    Date ct = Plan::instance().getCurrent();
    const OperationPlan* prev = nullptr;
    for (auto* op : order) {
      Date rawStart = ct + computeSetupTime(prev, op);
      Date materialDate = operationMaterialDate(op, rawStart);
      if (materialDate == Date::infiniteFuture) return false;
      rawStart = max(rawStart, materialDate);
      Duration dur = estimateOperationDuration(op, res);
      Date start, end;
      if (op->getOperation()) {
        DateRange range = op->getOperation()->calculateOperationTime(
            op, rawStart, dur, true);
        start = range.getStart();
        end = range.getEnd();
      } else {
        start = rawStart;
        end = rawStart + dur;
      }
      starts.push_back(start);
      ends.push_back(end);

      for (const auto& event : materialEventsForOperation(
               op, start, end, config_.transfer_batch)) {
        ensureMaterialLedger(event.buffer);
        materialLedger[event.buffer].push_back({event.date, event.quantity});
      }

      prev = op;
      ct = end;
    }
    return true;
  };

  bool improved = true;
  while (improved) {
    improved = false;
    for (size_t i = 0; i < seq.size() - 1; ++i) {
      for (size_t j = i + 1; j < seq.size(); ++j) {
        auto cseq = seq; swap(cseq[i], cseq[j]);
        vector<Date> cs, ce;
        if (!rebuildSingleResource(cseq, cs, ce)) continue;
        AntSolution cand;
        cand.sequences[res] = move(cseq);
        cand.startDates[res] = move(cs);
        cand.endDates[res] = move(ce);
        cand.fitness = evaluate(cand);
        if (cand.fitness > sol.fitness) { sol = move(cand); improved = true; }
      }
    }
  }
}

// ==========================================================================
// Schedule compaction: left-shift operations to fill idle gaps
// After constructive scheduling, resources may have idle periods because
// an operation was delayed waiting for another resource. This pass replays
// operations in start-time order, recalculating the earliest feasible
// start on all required resources simultaneously.
// ==========================================================================

void SolverACO::compactSchedule(
    AntSolution& ant,
    const vector<const Resource*>& resources,
    const unordered_map<const Resource*, Date>& resourceTimes,
    const vector<OperationPlan*>* preferredOrder) {
  // Collect all ops with their resources and current times
  struct OpEntry {
    OperationPlan* op;
    vector<const Resource*> resList;
    Date start, end;
  };
  vector<OpEntry> allOps;

  // Gather unique operations from all resources, deduplicate by pointer
  unordered_set<OperationPlan*> seen;
  for (auto* r : resources) {
    auto& seq = ant.sequences[r];
    auto& starts = ant.startDates[r];
    auto& ends = ant.endDates[r];
    for (size_t i = 0; i < seq.size(); ++i) {
      if (seen.count(seq[i])) continue;
      seen.insert(seq[i]);
      OpEntry e;
      e.op = seq[i];
      e.resList = getConstrainedResources(seq[i]);
      if (e.resList.empty()) e.resList.push_back(r);
      e.start = starts[i];
      e.end = ends[i];
      allOps.push_back(e);
    }
  }

  if (allOps.empty()) return;

  auto producesFor = [](const OperationPlan* producer,
                        const OperationPlan* consumer) -> bool {
    if (!producer || !consumer) return false;
    for (auto pf = producer->beginFlowPlans(); pf != producer->endFlowPlans(); ++pf) {
      if (pf->getQuantity() <= 0.0 || !pf->getBuffer()) continue;
      for (auto cf = consumer->beginFlowPlans(); cf != consumer->endFlowPlans(); ++cf) {
        if (cf->getQuantity() < 0.0 && cf->getBuffer() == pf->getBuffer())
          return true;
      }
    }
    return false;
  };

  unordered_map<const OperationPlan*, int> materialDepth;
  unordered_set<const OperationPlan*> visitingDepth;
  function<int(const OperationPlan*)> computeDepth = [&](const OperationPlan* op) {
    if (!op) return 0;
    auto it = materialDepth.find(op);
    if (it != materialDepth.end()) return it->second;
    if (visitingDepth.count(op)) return 0;
    visitingDepth.insert(op);
    int depth = 0;
    for (const auto& candidate : allOps) {
      if (candidate.op == op) continue;
      if (producesFor(candidate.op, op))
        depth = max(depth, computeDepth(candidate.op) + 1);
    }
    visitingDepth.erase(op);
    materialDepth[op] = depth;
    return depth;
  };
  for (const auto& e : allOps) computeDepth(e.op);

  unordered_map<const OperationPlan*, size_t> preferredRank;
  if (preferredOrder) {
    size_t rank = 0;
    for (auto* op : *preferredOrder)
      if (op && preferredRank.find(op) == preferredRank.end())
        preferredRank[op] = rank++;
  }

  // Sort by a material topological depth first, then by preferred search order
  // or current start time. The depth keeps producers before consumers.
  // This keeps producers before consumers without using a non-transitive
  // comparator in std::sort.
  sort(allOps.begin(), allOps.end(),
       [&](const OpEntry& a, const OpEntry& b) {
         int da = materialDepth[a.op];
         int db = materialDepth[b.op];
         if (da != db) return da < db;
         if (!preferredRank.empty()) {
           auto ia = preferredRank.find(a.op);
           auto ib = preferredRank.find(b.op);
           size_t ra = ia != preferredRank.end()
               ? ia->second : numeric_limits<size_t>::max();
           size_t rb = ib != preferredRank.end()
               ? ib->second : numeric_limits<size_t>::max();
           if (ra != rb) return ra < rb;
         }
         if (operationPlanBatchLess(a.op, b.op)) return true;
         if (operationPlanBatchLess(b.op, a.op)) return false;
         if (a.start != b.start) return a.start < b.start;
         if (a.end != b.end) return a.end < b.end;
         return a.op < b.op;
       });

  // Reset resource clocks and per-resource sequences
  unordered_map<const Resource*, Date> curTime;
  unordered_map<const Resource*, const OperationPlan*> prevOp;
  unordered_map<const Resource*, vector<OperationPlan*>> newSeq;
  unordered_map<const Resource*, vector<Date>> newStarts, newEnds;

  for (auto* r : resources) {
    auto it = resourceTimes.find(r);
    curTime[r] = (it != resourceTimes.end()) ? it->second
                  : Plan::instance().getCurrent();
    prevOp[r] = nullptr;
  }

  struct MaterialEvent {
    Date date;
    double quantity;
  };
  unordered_set<const OperationPlan*> compactOps;
  for (const auto& e : allOps)
    if (e.op) compactOps.insert(e.op);
  unordered_map<const Buffer*, vector<MaterialEvent>> materialLedger;
  unordered_set<const Buffer*> initializedBuffers;

  auto ensureMaterialLedger = [&](const Buffer* buf) -> void {
    if (!buf || initializedBuffers.count(buf)) return;
    initializedBuffers.insert(buf);
    Date current = Plan::instance().getCurrent();
    auto& events = materialLedger[buf];
    double initial = buf->getOnHand(current, false);
    if (fabs(initial) > ROUNDING_ERROR) events.push_back({current, initial});
    for (auto fp = buf->getFlowPlans().begin();
         fp != buf->getFlowPlans().end(); ++fp) {
      OperationPlan* fpOp = fp->getOperationPlan();
      if (fpOp && compactOps.count(fpOp)) continue;
      if (fp->getDate() <= current || fp->getDate() == Date::infiniteFuture)
        continue;
      if (fabs(fp->getQuantity()) <= ROUNDING_ERROR) continue;
      events.push_back({fp->getDate(), fp->getQuantity()});
    }
  };

  auto materialBalanceAt = [&](const Buffer* buf, Date date) -> double {
    ensureMaterialLedger(buf);
    double balance = 0.0;
    auto it = materialLedger.find(buf);
    if (it == materialLedger.end()) return balance;
    for (const auto& event : it->second) {
      if (event.date <= date)
        balance += event.quantity;
    }
    return balance;
  };

  auto materialAvailableDate =
      [&](const Buffer* buf, double requiredQty, Date fromDate) -> Date {
    ensureMaterialLedger(buf);
    Date purchaseDate = purchaseMaterialAvailable(buf);
    if (purchaseDate != Date::infiniteFuture && fromDate >= purchaseDate)
      return fromDate;

    vector<Date> dates;
    dates.push_back(fromDate);
    if (purchaseDate != Date::infiniteFuture && purchaseDate >= fromDate)
      dates.push_back(purchaseDate);
    auto it = materialLedger.find(buf);
    if (it != materialLedger.end())
      for (const auto& event : it->second)
        if (event.date >= fromDate) dates.push_back(event.date);

    sort(dates.begin(), dates.end());
    dates.erase(unique(dates.begin(), dates.end()), dates.end());
    for (Date date : dates) {
      if (purchaseDate != Date::infiniteFuture && date >= purchaseDate)
        return date;
      if (materialBalanceAt(buf, date) + ROUNDING_ERROR >= requiredQty)
        return date;
    }
    return Date::infiniteFuture;
  };

  auto operationMaterialDate = [&](const OperationPlan* op, Date fromDate) {
    Date materialDate = fromDate;
    if (!op) return materialDate;
    for (auto fl = op->beginFlowPlans(); fl != op->endFlowPlans(); ++fl) {
      if (fl->getQuantity() >= 0.0) continue;
      const Buffer* buf = fl->getBuffer();
      if (!buf) continue;
      Date available = materialAvailableDate(buf, -fl->getQuantity(), fromDate);
      if (available == Date::infiniteFuture) return available;
      if (available > materialDate) materialDate = available;
    }
    return materialDate;
  };

  // Replay operations in current order, but compacted and material-feasible.
  for (auto& e : allOps) {
    // Compute earliest feasible start across all required resources
    Date rawStart = Plan::instance().getCurrent();
    for (auto* r : e.resList) {
      Duration setup = computeSetupTime(prevOp[r], e.op);
      rawStart = max(rawStart, curTime[r] + setup);
    }
    Date matStart = operationMaterialDate(e.op, rawStart);
    if (matStart == Date::infiniteFuture) {
      ++ant.unscheduledCount;
      ant.unscheduledPenalty += 10000.0;
      continue;
    }
    rawStart = max(rawStart, matStart);

    Duration dur = estimateOperationDuration(e.op, e.resList[0]);
    Date start, end;
    if (e.op->getOperation()) {
      DateRange range = e.op->getOperation()->calculateOperationTime(
          e.op, rawStart, dur, true);
      start = range.getStart();
      end = range.getEnd();
    } else {
      start = rawStart;
      end = rawStart + dur;
    }

    // Commit to all resources
    for (auto* r : e.resList) {
      newSeq[r].push_back(e.op);
      newStarts[r].push_back(start);
      newEnds[r].push_back(end);
      prevOp[r] = e.op;
      curTime[r] = end;
    }

    for (const auto& event : materialEventsForOperation(
             e.op, start, end, config_.transfer_batch)) {
      ensureMaterialLedger(event.buffer);
      materialLedger[event.buffer].push_back({event.date, event.quantity});
    }
  }

  // Replace ant's sequences with compacted versions
  for (auto* r : resources) {
    ant.sequences[r] = move(newSeq[r]);
    ant.startDates[r] = move(newStarts[r]);
    ant.endDates[r] = move(newEnds[r]);
  }
}

// ==========================================================================
// Joint local search
// ==========================================================================

void SolverACO::localSearchJoint(AntSolution& sol) {
  // Disabled for now: operation-level joint local search needs tighter move
  // limits before it is practical on 500+ operation flow-line scenarios.
  (void)sol;
  return;
}

// ==========================================================================
// Evaluate solution (factors 1-5 all contribute)
// ==========================================================================

double SolverACO::evaluate(const AntSolution& sol) {
  double tardiness = 0.0, cost = 0.0, setup = 0.0;
  double makespan = 0.0;
  const double MATERIAL_PENALTY = 1000.0;  // heavy penalty for violating material constraint

  // Track seen operations to avoid double-counting multi-resource ops
  unordered_set<const OperationPlan*> seen;
  struct EvalOp {
    const OperationPlan* op;
    const Resource* res;
    Date start;
    Date end;
  };
  vector<EvalOp> evalOps;

  for (const auto& kv : sol.sequences) {
    const Resource* res = kv.first;
    const auto& seq = kv.second;
    const auto& starts = sol.startDates.at(res);
    const auto& ends = sol.endDates.at(res);

    const OperationPlan* prev = nullptr;
    for (size_t i = 0; i < seq.size(); ++i) {
      const OperationPlan* op = seq[i];

      // Deduplicate: multi-resource ops appear on every resource they occupy
      if (seen.count(op)) { prev = op; continue; }
      seen.insert(op);
      evalOps.push_back({op, res, starts[i], ends[i]});

      OperationPlanState currentState(op);
      if (auto it = sol.selectedOperations.find(op);
          it != sol.selectedOperations.end() && it->second &&
          op->getOperation() != it->second)
        const_cast<OperationPlan*>(op)->setOperation(it->second);
      OperationPlanState prevState;
      if (prev) {
        prevState = OperationPlanState(prev);
        if (auto it = sol.selectedOperations.find(prev);
            it != sol.selectedOperations.end() && it->second &&
            prev->getOperation() != it->second)
          const_cast<OperationPlan*>(prev)->setOperation(it->second);
      }

      // Compute priority factor: lower priority number = higher importance.
      // dmdPrio=0 → factor≈11x, dmdPrio=999 → factor≈1.0x
      Demand* dmd = op->getTopOwner()->getDemand();
      int dmdPrio = dmd ? dmd->getPriority() : 999;
      double priorityFactor = 1.0
          + config_.weight_priority / (1.0 + static_cast<double>(dmdPrio));

      // Factor 3: Due date tardiness (priority-weighted)
      if (dmd && dmd->getDue() != Date::infiniteFuture && ends[i] > dmd->getDue())
        tardiness += static_cast<double>(
            (ends[i] - dmd->getDue()).getSeconds()) / 3600.0 * priorityFactor;

      // Factor: Operation cost (operation base + resource hourly)
      cost += op->getOperation()->getCost() * op->getQuantity();
      // Add resource usage cost per hour for the actual scheduled duration
      Duration dur = ends[i] - starts[i];
      if (dur > Duration(0L))
        cost += res->getCost() *
            static_cast<double>(dur.getSeconds()) / 3600.0;

      // Factor 2: Setup time
      if (prev)
        setup += static_cast<double>(
            computeSetupTime(prev, op).getSeconds()) / 3600.0;

      if (prev) const_cast<OperationPlan*>(prev)->restore(prevState);
      const_cast<OperationPlan*>(op)->restore(currentState);
      prev = op;
    }
  }

  unordered_set<const OperationPlan*> evalOpSet;
  for (const auto& e : evalOps)
    if (e.op) evalOpSet.insert(e.op);

  if (!evalOps.empty()) {
    Date firstStart = evalOps.front().start;
    Date lastEnd = evalOps.front().end;
    for (const auto& e : evalOps) {
      if (e.start < firstStart) firstStart = e.start;
      if (e.end > lastEnd) lastEnd = e.end;
    }
    if (lastEnd > firstStart)
      makespan = static_cast<double>((lastEnd - firstStart).getSeconds()) /
          3600.0;
  }

  struct MaterialEvent {
    Date date;
    double quantity;
  };
  unordered_map<const Buffer*, vector<MaterialEvent>> materialLedger;
  unordered_set<const Buffer*> initializedBuffers;

  auto ensureMaterialLedger = [&](const Buffer* buf) -> void {
    if (!buf || initializedBuffers.count(buf)) return;
    initializedBuffers.insert(buf);
    Date current = Plan::instance().getCurrent();
    auto& events = materialLedger[buf];
    double initial = buf->getOnHand(current, false);
    if (fabs(initial) > ROUNDING_ERROR) events.push_back({current, initial});
    for (auto fp = buf->getFlowPlans().begin();
         fp != buf->getFlowPlans().end(); ++fp) {
      OperationPlan* fpOp = fp->getOperationPlan();
      if (fpOp && evalOpSet.count(fpOp)) continue;
      if (fp->getDate() <= current || fp->getDate() == Date::infiniteFuture)
        continue;
      if (fabs(fp->getQuantity()) <= ROUNDING_ERROR) continue;
      events.push_back({fp->getDate(), fp->getQuantity()});
    }
  };

  auto materialBalanceAt = [&](const Buffer* buf, Date date) -> double {
    ensureMaterialLedger(buf);
    double balance = 0.0;
    auto it = materialLedger.find(buf);
    if (it == materialLedger.end()) return balance;
    for (const auto& event : it->second) {
      if (event.date <= date)
        balance += event.quantity;
    }
    return balance;
  };

  sort(evalOps.begin(), evalOps.end(), [](const EvalOp& a, const EvalOp& b) {
    if (a.start != b.start) return a.start < b.start;
    if (a.end != b.end) return a.end < b.end;
    return a.op < b.op;
  });

  for (const auto& e : evalOps) {
    if (!e.op) continue;
    Demand* dmd = e.op->getTopOwner()->getDemand();
    int dmdPrio = dmd ? dmd->getPriority() : 999;
    double priorityFactor = 1.0
        + config_.weight_priority / (1.0 + static_cast<double>(dmdPrio));
    vector<ACOMaterialEvent> events = materialEventsForOperation(
        e.op, e.start, e.end, config_.transfer_batch);
    for (const auto& event : events) {
      if (!event.buffer || event.quantity >= 0.0) continue;
      double requiredQty = -event.quantity;
      double available = materialBalanceAt(event.buffer, event.date);
      if (available + ROUNDING_ERROR < requiredQty)
        tardiness += MATERIAL_PENALTY * priorityFactor *
            (requiredQty - available) / max(requiredQty, 1.0);
    }
    for (const auto& event : events) {
      ensureMaterialLedger(event.buffer);
      materialLedger[event.buffer].push_back({event.date, event.quantity});
    }
  }

  // Factor: Load balancing — penalize uneven resource utilization.
  // Sum of squared load-hours per resource: 5+5 → 25+25=50, 10+0 → 100+0=100
  double loadBalance = 0.0;
  for (const auto& kv : sol.sequences) {
    double resLoad = 0.0;
    const auto& ss = sol.startDates.at(kv.first);
    const auto& es = sol.endDates.at(kv.first);
    for (size_t i = 0; i < kv.second.size(); ++i)
      resLoad += static_cast<double>((es[i] - ss[i]).getSeconds()) / 3600.0;
    loadBalance += resLoad * resLoad;
  }

  // Heavy penalty for unscheduled operations (deadlocked work)
  double unscheduledPenalty = sol.unscheduledCount > 0
      ? (sol.unscheduledPenalty + 10000.0 * sol.unscheduledCount) : 0.0;

  return -(config_.weight_tardiness * tardiness +
           config_.weight_cost * cost +
           config_.weight_setup * setup +
           config_.weight_makespan * makespan +
           config_.weight_balance * loadBalance +
           unscheduledPenalty);
}

bool SolverACO::hasMaterialShortage(const AntSolution& sol) const {
  unordered_set<const OperationPlan*> seen;
  struct EvalOp {
    const OperationPlan* op;
    Date start;
    Date end;
  };
  vector<EvalOp> evalOps;

  for (const auto& kv : sol.sequences) {
    const auto& seq = kv.second;
    const auto& starts = sol.startDates.at(kv.first);
    const auto& ends = sol.endDates.at(kv.first);
    for (size_t i = 0; i < seq.size(); ++i) {
      const OperationPlan* op = seq[i];
      if (!op || seen.count(op)) continue;
      seen.insert(op);
      evalOps.push_back({op, starts[i], ends[i]});
    }
  }

  sort(evalOps.begin(), evalOps.end(), [](const EvalOp& a, const EvalOp& b) {
    if (a.start != b.start) return a.start < b.start;
    if (a.end != b.end) return a.end < b.end;
    return a.op < b.op;
  });

  struct MaterialEvent {
    const Buffer* buffer;
    Date date;
    double quantity;
  };
  vector<MaterialEvent> events;
  unordered_set<const Buffer*> touchedBuffers;

  for (const auto& e : evalOps) {
    vector<ACOMaterialEvent> opEvents = materialEventsForOperation(
        e.op, e.start, e.end, config_.transfer_batch);
    for (const auto& event : opEvents) {
      if (!event.buffer || fabs(event.quantity) <= ROUNDING_ERROR) continue;
      touchedBuffers.insert(event.buffer);
      events.push_back({event.buffer, event.date, event.quantity});
    }
  }

  Date current = Plan::instance().getCurrent();
  unordered_map<const Buffer*, double> balance;
  for (const Buffer* buf : touchedBuffers) {
    if (!buf) continue;
    balance[buf] = buf->getOnHand(current, false);
    for (auto fp = buf->getFlowPlans().begin();
         fp != buf->getFlowPlans().end(); ++fp) {
      OperationPlan* fpOp = fp->getOperationPlan();
      if (fpOp && seen.count(fpOp)) continue;
      if (fp->getDate() <= current || fp->getDate() == Date::infiniteFuture)
        continue;
      if (fabs(fp->getQuantity()) <= ROUNDING_ERROR) continue;
      events.push_back({buf, fp->getDate(), fp->getQuantity()});
    }
  }

  sort(events.begin(), events.end(), [](const MaterialEvent& a,
                                        const MaterialEvent& b) {
    if (a.date != b.date) return a.date < b.date;
    if (a.buffer != b.buffer) return a.buffer < b.buffer;
    return a.quantity > b.quantity;  // production before consumption at same timestamp
  });

  for (const auto& event : events) {
    double& bal = balance[event.buffer];
    if (event.quantity < 0.0 &&
        bal + event.quantity < -ROUNDING_ERROR)
      return true;
    bal += event.quantity;
  }
  return false;
}

// ==========================================================================
// Pheromone management
// ==========================================================================

void SolverACO::initPheromone(const Resource* res) {
  pheromones_[res].reset(config_.tau0);
}

const PheromoneMatrix* SolverACO::getPheromone(const Resource* res) const {
  auto it = pheromones_.find(res);
  return (it != pheromones_.end()) ? &it->second : nullptr;
}

// ==========================================================================
// Apply solution
// ==========================================================================

static void syncOwnerTimeEnvelope(OperationPlan* op) {
  for (OperationPlan* owner = op ? op->getOwner() : nullptr; owner;
       owner = owner->getOwner()) {
    Date start = Date::infiniteFuture;
    Date end = Date::infinitePast;
    bool hasChild = false;
    for (auto child = owner->getSubOperationPlans();
         child != OperationPlan::end(); ++child) {
      if (child->getStart() != Date::infinitePast &&
          child->getStart() < start)
        start = child->getStart();
      if (child->getEnd() != Date::infiniteFuture &&
          child->getEnd() > end)
        end = child->getEnd();
      hasChild = true;
    }
    if (!hasChild || start == Date::infiniteFuture ||
        end == Date::infinitePast)
      continue;
    owner->setStartForce(start);
    owner->setEndForce(end);
  }
}

void SolverACO::applyBestSolution(const AntSolution& best) {
  auto* cmdMgr = getCommandManager();
  // Track which operations we've already processed (multi-resource ops
  // appear in multiple resource sequences but should only be moved once).
  unordered_set<const OperationPlan*> processed;
  for (const auto& kv : best.sequences) {
    const Resource* seqRes = kv.first;
    const auto& seq = kv.second;
    const auto& starts = best.startDates.at(seqRes);
    const auto& ends = best.endDates.at(seqRes);

    for (size_t i = 0; i < seq.size(); ++i) {
      if (processed.count(seq[i])) continue;
      processed.insert(seq[i]);
      if (starts[i] == Date::infiniteFuture || ends[i] == Date::infiniteFuture)
        continue;
      OperationPlan* op = seq[i];

      auto oit = best.selectedOperations.find(op);
      if (oit != best.selectedOperations.end() && oit->second &&
          op->getOperation() != oit->second)
        op->setOperation(oit->second);

      // Apply exact load-to-resource assignments selected by ACO
      auto lit = best.selectedLoadAssignments.find(op);
      if (lit != best.selectedLoadAssignments.end()) {
        for (const auto& la : lit->second) {
          for (auto lp = op->beginLoadPlans(); lp != op->endLoadPlans(); ++lp) {
            if (lp->isStart() && lp->getLoad() == la.first &&
                lp->getResource() != la.second) {
              lp->setResource(const_cast<Resource*>(la.second), false, false);
              break;
            }
          }
        }
      }

      if (cmdMgr)
        cmdMgr->add(new CommandMoveOperationPlan(
            op, starts[i], Date::infinitePast, -1.0, false, false, false));
      else
        op->setOperationPlanParameters(
            op->getQuantity(), starts[i], Date::infinitePast, false, true,
            false);
      syncOwnerTimeEnvelope(op);
      lockOptimizedPlan(op);
    }
  }
}

bool SolverACO::shouldLockOptimizedPlan(const OperationPlan* op) const {
  if (!op || !op->getOperation()) return false;
  return !op->getOperation()->hasType<OperationDelivery, OperationItemSupplier,
                                      OperationItemDistribution>();
}

void SolverACO::lockOptimizedPlan(OperationPlan* op) {
  if (!op) return;
  if (shouldLockOptimizedPlan(op)) op->setAcoLocked(true);
  auto* top = op->getTopOwner();
  if (top != op && shouldLockOptimizedPlan(top)) top->setAcoLocked(true);
}

// ==========================================================================
// Collect operationplans
// ==========================================================================

static void collectResourcePlans(const Resource* res,
                                 vector<OperationPlan*>& plans) {
  auto loadplans = res->getLoadPlans();
  for (auto it = loadplans.begin(); it != loadplans.end(); ++it) {
    OperationPlan* op = it->getOperationPlan();
    if (op && op->getOperation() && op->getQuantity() > 0.0 &&
        !op->getOperation()->hasType<OperationItemSupplier,
                                     OperationItemDistribution>()) {
      bool found = false;
      for (const auto& e : plans) if (e == op) { found = true; break; }
      if (!found) plans.push_back(op);
    }
  }
}

// ==========================================================================
// Single-resource ACO (legacy fallback)
// ==========================================================================

void SolverACO::solve(const Resource* res, void* v) {
  if (mrpRefreshInProgress_) {
    SolverCreate::solve(res, v);
    return;
  }

  vector<OperationPlan*> plans;
  collectResourcePlans(res, plans);
  if (plans.size() < 2) { SolverCreate::solve(res, v); return; }
  if (pheromones_.find(res) == pheromones_.end()) initPheromone(res);

  PheromoneMatrix& phero = pheromones_[res];
  AntSolution best;
  best.fitness = -numeric_limits<double>::max();
  int stag = 0;

  for (int iter = 0; iter < config_.iterations; ++iter) {
    vector<AntSolution> ants(config_.ants);
    for (int a = 0; a < config_.ants; ++a) {
      ants[a] = constructSolution(res, plans);
      ants[a].fitness = evaluate(ants[a]);
      localSearch(res, ants[a]);
    }
    sort(ants.begin(), ants.end(), [](auto& a, auto& b) { return a.fitness > b.fitness; });
    if (ants[0].fitness > best.fitness) { best = move(ants[0]); stag = 0; }
    else ++stag;

    phero.evaporate(config_.evaporation);
    phero.deposit(best.sequences[res], config_.Q * (1.0 + max(0.0, best.fitness) * 0.01));
    for (int e = 0; e < config_.elite_ants && e < config_.ants; ++e)
      phero.deposit(ants[e].sequences[res],
                    config_.Q * 0.5 * (1.0 + max(0.0, ants[e].fitness) * 0.01));

    if (stag >= config_.stagnation_limit) {
      if (getLogLevel() > 1)
        logger << indentlevel << "ACO: stopped after " << (iter+1) << " iter (stagnation)\n";
      break;
    }
  }
  if (best.unscheduledCount > 0 || hasMaterialShortage(best)) {
    if (getLogLevel() >= 0)
      logger << indentlevel
             << "ACO joint: no complete material-feasible solution found; "
             << "keeping existing plan\n";
    lastBestFitness_ = best.fitness;
    stagnationOccurred_ = (stag >= config_.stagnation_limit);
    return;
  }

  applyBestSolution(best);
  lastBestFitness_ = best.fitness;
  if (getLogLevel() > 0)
    logger << indentlevel << "ACO on '" << res->getName()
           << "': " << plans.size() << " plans, fitness=" << best.fitness << "\n";
}

// ==========================================================================
// Joint ACO
// ==========================================================================

void SolverACO::solveJoint(const vector<const Resource*>& resources) {
  if (resources.empty()) return;

  // Build candidate pool (factor 1: multi-resource)
  vector<CandidateOp> candidates = buildCandidates(resources);
  if (getLogLevel() >= 0)
    logger << indentlevel << "ACO joint: " << candidates.size()
           << " candidates across " << resources.size() << " resources\n";
  if (candidates.empty()) return;

  for (auto* res : resources)
    if (pheromones_.find(res) == pheromones_.end()) initPheromone(res);

  unordered_map<const Resource*, Date> resTimes;
  for (auto* r : resources) resTimes[r] = Plan::instance().getCurrent();

  AntSolution best;
  best.fitness = -numeric_limits<double>::max();
  int stag = 0;

  for (int iter = 0; iter < config_.iterations; ++iter) {
    vector<AntSolution> ants(config_.ants);
    for (int a = 0; a < config_.ants; ++a) {
      ants[a] = constructJointSolution(resources, candidates, resTimes);
      vector<OperationPlan*> preferredOrder;
      unordered_set<OperationPlan*> seenPreferred;
      for (auto* r : resources) {
        auto seqIt = ants[a].sequences.find(r);
        if (seqIt == ants[a].sequences.end()) continue;
        for (auto* op : seqIt->second) {
          if (op && seenPreferred.insert(op).second)
            preferredOrder.push_back(op);
        }
      }
      compactSchedule(ants[a], resources, resTimes, &preferredOrder);
      ants[a].fitness = evaluate(ants[a]);
      localSearchJoint(ants[a]);
    }
    sort(ants.begin(), ants.end(), [](auto& a, auto& b) { return a.fitness > b.fitness; });
    if (ants[0].fitness > best.fitness) { best = move(ants[0]); stag = 0; }
    else ++stag;

    for (auto* r : resources) pheromones_[r].evaporate(config_.evaporation);
    for (auto& kv : best.sequences)
      pheromones_[kv.first].deposit(kv.second,
          config_.Q * (1.0 + max(0.0, best.fitness) * 0.01));
    for (int e = 0; e < config_.elite_ants && e < config_.ants; ++e)
      for (auto& kv : ants[e].sequences)
        pheromones_[kv.first].deposit(kv.second,
            config_.Q * 0.5 * (1.0 + max(0.0, ants[e].fitness) * 0.01));

    if (stag >= config_.stagnation_limit) {
      if (getLogLevel() > 1)
        logger << indentlevel << "ACO joint: stopped after " << (iter+1) << " iter\n";
      break;
    }
  }

  applyBestSolution(best);
  lastBestFitness_ = best.fitness;
  stagnationOccurred_ = (stag >= config_.stagnation_limit);
  if (getLogLevel() > 0)
    logger << indentlevel << "ACO joint: " << resources.size() << " resources, "
           << candidates.size() << " candidates, fitness=" << best.fitness << "\n";
}

// ==========================================================================
// Top-level entry
// ==========================================================================

void SolverACO::solve(void* v) {
  // Collect constrained resources that have at least 1 manufacturing plan.
  // Extracted as a lambda so the bottleneck set can be refreshed after each
  // MRP pass (MRP may create new supply ops, change resource assignments,
  // or update material availability dates).
    auto collectBottlenecks = [&]() -> vector<const Resource*> {
    vector<const Resource*> bn;
    unordered_set<const Resource*> activeGroups;
    for (auto res = Resource::begin(); res != Resource::end(); ++res) {
      if (!res->getConstrained()) continue;
      if (res->isGroup()) {
        for (auto m = res->getMembers(); m != Resource::end(); ++m) {
          if (!m->isGroup() && m->getConstrained()) {
            vector<OperationPlan*> plans;
            collectResourcePlans(&*m, plans);
            if (!plans.empty()) {
              bn.push_back(&*m);
              activeGroups.insert(&*res);
            }
          }
        }
      } else {
        vector<OperationPlan*> plans;
        collectResourcePlans(&*res, plans);
        if (!plans.empty()) bn.push_back(&*res);
      }
    }
    // Include ALL constrained members of active groups
    for (auto* grp : activeGroups) {
      for (auto m = grp->getMembers(); m != Resource::end(); ++m) {
        if (!m->isGroup() && m->getConstrained()) {
          bool alreadyIn = false;
          for (auto* r : bn) if (r == &*m) { alreadyIn = true; break; }
          if (!alreadyIn) bn.push_back(&*m);
        }
      }
    }
    return bn;
  };

vector<const Resource*> bottleneck = collectBottlenecks();

  // Diagnostic: always log that ACO entry was reached
  if (getLogLevel() >= 0)
    logger << indentlevel << "ACO: entry, found " << bottleneck.size()
           << " bottleneck resources\n";

  // No bottleneck resources → nothing for ACO to optimize.
  if (bottleneck.empty()) {
    if (config_.runMRP) SolverCreate::solve(v);
    return;
  }

  // ---- Phase 1: ACO optimization ----
  if (config_.joint_optimization)
    solveJoint(bottleneck);
  else if (bottleneck.size() == 1)
    solve(bottleneck[0], v);

  try {
    // ---- Phase 2: Material propagation ----
    if (config_.runMRP) {
      mrpRefreshInProgress_ = true;
      SolverCreate::solve(v);
      mrpRefreshInProgress_ = false;
    }
  } catch (...) {
    mrpRefreshInProgress_ = false;
    for (auto op = OperationPlan::begin(); op != OperationPlan::end(); ++op)
      op->setAcoLocked(false);
    throw;
  }

  // ---- Unlock ACO-optimized plans ----
  for (auto op = OperationPlan::begin(); op != OperationPlan::end(); ++op)
    op->setAcoLocked(false);
}

}  // namespace ccAPPS

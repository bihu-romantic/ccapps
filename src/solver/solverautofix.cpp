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
#include <cstdlib>
#include <climits>
#include <sstream>
#include <unordered_set>

#include "ccAPPS/solverautofix.h"

namespace ccAPPS {

const MetaClass* SolverAutoFix::metadata;

// ==========================================================================
// Python bindings
// ==========================================================================

static PyObject* solverAutoFix_setMaxIterations(PyObject* self,
                                                PyObject* args) {
  auto* solver = static_cast<SolverAutoFix*>(self);
  int val;
  if (!PyArg_ParseTuple(args, "i", &val)) return nullptr;
  AutoFixConfig cfg = solver->getConfig();
  cfg.maxIterations = max(1, val);
  solver->setConfig(cfg);
  Py_RETURN_NONE;
}

static PyObject* solverAutoFix_getMaxIterations(PyObject* self, PyObject*) {
  auto* solver = static_cast<SolverAutoFix*>(self);
  return PyLong_FromLong(solver->getConfig().maxIterations);
}

PyObject* fix_conflicts(PyObject*, PyObject*) {
  SolverAutoFix fixer;
  fixer.setLogLevel(1);
  void* pv = nullptr;
  fixer.solve(pv);
  if (fixer.commit()) Py_RETURN_TRUE;
  Py_RETURN_FALSE;
}

int SolverAutoFix::initialize() {
  metadata = MetaClass::registerClass<SolverAutoFix>(
      "solver", "solver_autofix", Object::create<SolverAutoFix>);
  registerFields<SolverAutoFix>(const_cast<MetaClass*>(metadata));

  auto& x = PythonExtension<SolverAutoFix>::getPythonType();
  x.setName("solverAutoFix");
  x.setDoc("Domain heuristic for planning conflict resolution");
  x.supportgetattro(); x.supportsetattro(); x.supportcreate(SolverAutoFix::create);
  x.addMethod("solve",
              static_cast<PyObject* (*)(PyObject*, PyObject*, PyObject*)>(
                  SolverCreate::solve),
              METH_VARARGS, "run the auto-fix solver");
  x.addMethod("commit", SolverCreate::commit, METH_NOARGS, "commit changes");
  x.addMethod("rollback", SolverCreate::rollback, METH_NOARGS, "rollback changes");
  x.addMethod("setMaxIterations", solverAutoFix_setMaxIterations, METH_VARARGS, "");
  x.addMethod("getMaxIterations", solverAutoFix_getMaxIterations, METH_NOARGS, "");

  SolverAutoFix::metadata->setPythonClass(x);
  return x.typeReady();
}

PyObject* SolverAutoFix::create(PyTypeObject*, PyObject*, PyObject*) {
  return Object::create<SolverAutoFix>();
}

bool SolverAutoFix::commit() {
  auto* cmdMgr = getCommandManager();
  if (!cmdMgr) return false;
  cmdMgr->commit();
  return true;
}

// ==========================================================================
// Helpers
// ==========================================================================

SolverAutoFix::FixCount SolverAutoFix::countProblems() const {
  FixCount c;
  for (auto p = Problem::begin(); p != Problem::end(); ++p) {
    const MetaClass& t = p->getType();
    if (&t == ProblemCapacityOverload::metadata) c.capacityOverload++;
    else if (&t == ProblemMaterialShortage::metadata) c.materialShortage++;
    else if (&t == ProblemPrecedence::metadata) c.precedence++;
    else if (&t == ProblemBeforeCurrent::metadata) c.beforeCurrent++;
    else if (&t == ConstraintOverdueDemand::metadata) c.overdueDemand++;
  }
  return c;
}

static bool isMovableOp(const OperationPlan* op, bool moveApproved) {
  if (!op || op->getConfirmed() || op->getClosed() || op->getCompleted())
    return false;
  if (!moveApproved && op->getApproved()) return false;
  return op->getQuantity() > 0.0;
}

static bool shouldProtectFromMrp(const OperationPlan* op) {
  if (!op || !op->getOperation()) return false;
  return !op->getOperation()->hasType<OperationDelivery, OperationItemSupplier,
                                      OperationItemDistribution>();
}

void SolverAutoFix::loadScopeFromEnvironment() {
  config_.scopeOperationPlans.clear();
  const char* raw = getenv("CCAPPS_AUTOFIX_SCOPE");
  if (!raw || !*raw) raw = getenv("autofix_scope");
  const char* maxIter = getenv("autofix_max_iterations");
  if (maxIter && *maxIter) {
    config_.maxIterations = max(1, atoi(maxIter));
  }
  if (!raw || !*raw) return;

  string token;
  stringstream stream(raw);
  while (getline(stream, token, '|')) {
    stringstream commaStream(token);
    string ref;
    while (getline(commaStream, ref, ',')) {
      auto first = ref.find_first_not_of(" \t\r\n");
      if (first == string::npos) continue;
      auto last = ref.find_last_not_of(" \t\r\n");
      config_.scopeOperationPlans.insert(ref.substr(first, last - first + 1));
    }
  }
}

bool SolverAutoFix::isScopeOperationPlan(const OperationPlan* op) const {
  if (!op) return false;
  return config_.scopeOperationPlans.find(op->getReference()) !=
         config_.scopeOperationPlans.end();
}

void SolverAutoFix::lockForMrpRefresh(OperationPlan* op) const {
  if (!op) return;
  auto* top = op->getTopOwner();
  if (shouldProtectFromMrp(top)) {
    top->setAcoLocked(true);
  } else if (shouldProtectFromMrp(op)) {
    op->setAcoLocked(true);
  }
}

void SolverAutoFix::collectScopedProblems(vector<const Problem*>& out) const {
  unordered_set<const Problem*> seen;
  for (auto op = OperationPlan::begin(); op != OperationPlan::end(); ++op) {
    if (!isScopeOperationPlan(&*op)) continue;
    for (auto p = op->getProblems(); p != Problem::end(); ++p) {
      if (!p->isFeasible() && seen.insert(&*p).second) out.push_back(&*p);
    }
  }
}

// ==========================================================================
// Conflict 1: Capacity Overload
//   Strategy: sort operations on the overloaded resource by priority
//             (lowest first), then right-shift them past the overload window.
// ==========================================================================

bool SolverAutoFix::fixCapacityOverload(const Problem* p) {
  auto* res = dynamic_cast<Resource*>(p->getOwner());
  if (!res || !res->getConstrained()) return false;

  // Collect movable operations overlapping the problem window
  vector<OperationPlan*> ops;
  vector<OperationPlan*> scopedOps;
  auto loadplans = res->getLoadPlans();
  for (auto it = loadplans.begin(); it != loadplans.end(); ++it) {
    OperationPlan* op = it->getOperationPlan();
    if (!op || !isMovableOp(op, config_.moveApproved)) continue;
    // Check overlap: op window intersects problem window
    if (op->getEnd() < p->getStart() || op->getStart() > p->getEnd()) continue;
    if (find(ops.begin(), ops.end(), op) == ops.end()) {
      ops.push_back(op);
      if (isScopeOperationPlan(op)) scopedOps.push_back(op);
    }
  }
  if (ops.empty()) return false;
  if (!scopedOps.empty()) ops.swap(scopedOps);

  // Sort by priority descending (= lowest priority first, moved first)
  sort(ops.begin(), ops.end(), [](OperationPlan* a, OperationPlan* b) {
    int pa = a->getPriority(), pb = b->getPriority();
    return pa != pb ? pa > pb : a->getStart() < b->getStart();
  });

  // Right-shift the lowest-priority operation after the problem ends
  auto* op = ops.front();
  Duration dur = op->getEnd() - op->getStart();
  if (dur <= Duration(0L)) dur = Duration(3600L);
  Date newStart = max(p->getEnd(), op->getEnd()) + Duration(3600L);
  op->setStartEndAndQuantity(newStart, newStart + dur, op->getQuantity());
  lockForMrpRefresh(op);

  if (getLogLevel() >= 0)
    logger << indentlevel << "AutoFix capacity: moved " << op->getReference()
           << " on " << res->getName() << " to " << newStart << "\n";
  return true;
}

// ==========================================================================
// Conflict 2: Material Shortage
//   Strategy: find upstream producing operations for the short buffer
//             and increase their quantity or advance their start date.
//   Without MRP, we increase the most recent upstream plan's quantity.
// ==========================================================================

bool SolverAutoFix::fixMaterialShortage(const Problem* p) {
  auto* buf = dynamic_cast<Buffer*>(p->getOwner());
  if (!buf) return false;

  // Find the producing operation
  Operation* prodOper = buf->getProducingOperation();
  if (!prodOper) return false;

  // Find the most recent (latest end) movable production plan
  OperationPlan* best = nullptr;
  OperationPlan* bestScoped = nullptr;
  for (auto op = OperationPlan::begin(); op != OperationPlan::end(); ++op) {
    if (op->getOperation() != prodOper) continue;
    if (!isMovableOp(&*op, config_.moveApproved)) continue;
    if (!best || op->getEnd() > best->getEnd()) best = &*op;
    if (isScopeOperationPlan(&*op) &&
        (!bestScoped || op->getEnd() > bestScoped->getEnd()))
      bestScoped = &*op;
  }
  if (bestScoped) best = bestScoped;

  if (!best) return false;

  // Try moving it earlier: set start to the shortage date or current, whichever later
  Date target = max(p->getStart(), Plan::instance().getCurrent());
  Duration dur = best->getEnd() - best->getStart();
  if (dur <= Duration(0L)) dur = Duration(3600L);
  best->setStartEndAndQuantity(target, target + dur, best->getQuantity());
  lockForMrpRefresh(best);

  if (getLogLevel() >= 0)
    logger << indentlevel << "AutoFix material: moved " << best->getReference()
           << " earlier to " << target << " for " << buf->getName() << "\n";
  return true;
}

// ==========================================================================
// Conflict 3: Precedence Violation
//   Strategy: right-shift the offending operation plan past its predecessor.
// ==========================================================================

bool SolverAutoFix::fixPrecedence(const Problem* p) {
  auto* op = dynamic_cast<OperationPlan*>(p->getOwner());
  if (!op || !isMovableOp(op, config_.moveApproved)) return false;

  // Push it one hour later and let compactSchedule / next iteration align
  Date curEnd = op->getEnd();
  if (curEnd <= Plan::instance().getCurrent())
    curEnd = Plan::instance().getCurrent();
  op->setStart(curEnd + Duration(3600L), false, false);
  lockForMrpRefresh(op);

  if (getLogLevel() >= 0)
    logger << indentlevel << "AutoFix precedence: right-shifted "
           << op->getReference() << "\n";
  return true;
}

// ==========================================================================
// Conflict 4: Before Current
//   Strategy: set start to current date + 1 hour.
// ==========================================================================

bool SolverAutoFix::fixBeforeCurrent(const Problem* p) {
  auto* op = dynamic_cast<OperationPlan*>(p->getOwner());
  if (!op || !isMovableOp(op, config_.moveApproved)) return false;

  Date cur = Plan::instance().getCurrent();
  if (op->getStart() >= cur) return false;

  Duration dur = op->getEnd() - op->getStart();
  if (dur <= Duration(0L)) dur = Duration(3600L);
  op->setStartEndAndQuantity(cur + Duration(3600L),
                              cur + Duration(3600L) + dur,
                              op->getQuantity());
  lockForMrpRefresh(op);

  if (getLogLevel() >= 0)
    logger << indentlevel << "AutoFix before-current: moved "
           << op->getReference() << " to " << cur << "\n";
  return true;
}

// ==========================================================================
// Conflict 5: Overdue Demand
//   Strategy: expedite the delivery operation plan to current date.
// ==========================================================================

bool SolverAutoFix::fixOverdueDemand(const Problem* p) {
  auto* dmd = dynamic_cast<Demand*>(p->getOwner());
  if (!dmd) return false;
  Operation* oper = dmd->getOperation();
  if (!oper) return false;

  for (auto op = OperationPlan::begin(); op != OperationPlan::end(); ++op) {
    if (op->getOperation() != oper) continue;
    if (!isMovableOp(&*op, config_.moveApproved)) continue;
    if (hasScope() && !isScopeOperationPlan(&*op)) continue;
    op->setStart(Plan::instance().getCurrent(), false, false);
    lockForMrpRefresh(&*op);
    return true;
  }
  return false;
}

// ==========================================================================
// Main solve: iterate through problems, fix each with domain heuristics.
// No MRP re-run — pure in-memory operation plan manipulation.
// ==========================================================================

void SolverAutoFix::solve(void* v) {
  (void)v;
  loadScopeFromEnvironment();

  FixCount before = countProblems();
  logger << indentlevel << "AutoFix: " << before.total()
         << " problems (cap=" << before.capacityOverload
         << " mat=" << before.materialShortage
         << " prec=" << before.precedence
         << " bcur=" << before.beforeCurrent
         << " odue=" << before.overdueDemand << ")\n";
  if (before.total() == 0) return;

  int totalFixed = 0;
  auto fixOne = [this, &totalFixed](const Problem* p) {
    const MetaClass& t = p->getType();
    bool ok = false;
    if (&t == ProblemCapacityOverload::metadata) ok = fixCapacityOverload(p);
    else if (&t == ProblemMaterialShortage::metadata) ok = fixMaterialShortage(p);
    else if (&t == ProblemPrecedence::metadata) ok = fixPrecedence(p);
    else if (&t == ProblemBeforeCurrent::metadata) ok = fixBeforeCurrent(p);
    else if (&t == ConstraintOverdueDemand::metadata) ok = fixOverdueDemand(p);
    if (ok) totalFixed++;
  };

  int previousRemaining = INT_MAX;
  for (int iter = 0; iter < max(1, config_.maxIterations); ++iter) {
    vector<const Problem*> problems;
    if (hasScope()) {
      collectScopedProblems(problems);
      logger << indentlevel << "AutoFix iteration " << (iter + 1)
             << ": scoped to " << config_.scopeOperationPlans.size()
             << " operationplans, " << problems.size()
             << " related problems\n";
    } else {
      for (auto p = Problem::begin(); p != Problem::end(); ++p)
        problems.push_back(&*p);
      logger << indentlevel << "AutoFix iteration " << (iter + 1)
             << ": " << problems.size() << " global problems\n";
    }

    if (problems.empty()) break;
    if (static_cast<int>(problems.size()) >= previousRemaining) {
      logger << indentlevel
             << "AutoFix: stopping, remaining problem count isn't decreasing\n";
      break;
    }
    previousRemaining = static_cast<int>(problems.size());

    int fixedBeforeIteration = totalFixed;
    for (auto p : problems) fixOne(p);

    // Refresh feasibility after each repair pass before collecting again.
    for (auto op = OperationPlan::begin(); op != OperationPlan::end(); ++op)
      op->updateFeasible();

    if (totalFixed == fixedBeforeIteration) {
      logger << indentlevel << "AutoFix: stopping, no applicable fix found\n";
      break;
    }
  }

  FixCount after = countProblems();
  logger << indentlevel << "AutoFix: fixed " << totalFixed
         << " → " << after.total() << " problems (cap=" << after.capacityOverload
         << " mat=" << after.materialShortage
         << " prec=" << after.precedence
         << " bcur=" << after.beforeCurrent
         << " odue=" << after.overdueDemand << ")\n";
}

}  // namespace ccAPPS

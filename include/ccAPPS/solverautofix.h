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

#pragma once
#ifndef SOLVERAUTOFIX_H
#define SOLVERAUTOFIX_H

#include <string>
#include <unordered_set>
#include <vector>

#include "ccAPPS/solver.h"

namespace ccAPPS {

/* Heuristic auto-resolution of planning conflicts.
 *
 * SolverAutoFix inspects the current plan for problems (capacity overload,
 * material shortage, precedence violations, before-current dates, overdue
 * demands) and applies targeted, local fixes.  It is designed to be called
 * interactively from the planning board via a user button.
 *
 * The solver iterates at most maxIterations times.  Each iteration collects
 * all current problems, fixes them one by one, then re-runs the MRP
 * constraint solver to propagate the changes.  Iteration stops when no
 * problems remain or the count stops decreasing.
 */
struct AutoFixConfig {
  int maxIterations = 10;           // Outer loop cap to prevent runaway fixes
  int maxFixesPerType = 50;         // Safety valve per problem type per iteration
  bool moveApproved = false;        // Allow moving Approved ops (default: no)
  bool logDetails = true;           // Log per-fix diagnostics
  std::unordered_set<std::string> scopeOperationPlans;
};

class SolverAutoFix : public SolverCreate {
 public:
  using SolverCreate::solve;

  SolverAutoFix() { initType(metadata); }
  ~SolverAutoFix() override {}

  static int initialize();
  static PyObject* create(PyTypeObject*, PyObject*, PyObject*);
  const MetaClass& getType() const override { return *metadata; }
  static const MetaClass* metadata;

  void setConfig(const AutoFixConfig& cfg) { config_ = cfg; }
  const AutoFixConfig& getConfig() const { return config_; }

  /* Main entry: collect, fix, iterate. */
  void solve(void* v = nullptr) override;

  /* Persist in-memory changes to the database. */
  bool commit();

 private:
  /* ---- Problem collection ---- */
  struct FixCount {
    int capacityOverload = 0;
    int materialShortage = 0;
    int precedence = 0;
    int beforeCurrent = 0;
    int overdueDemand = 0;
    int total() const {
      return capacityOverload + materialShortage + precedence +
             beforeCurrent + overdueDemand;
    }
  };
  FixCount countProblems() const;

  /* ---- Individual fix routines ---- */
  bool fixCapacityOverload(const Problem* p);
  bool fixMaterialShortage(const Problem* p);
  bool fixPrecedence(const Problem* p);
  bool fixBeforeCurrent(const Problem* p);
  bool fixOverdueDemand(const Problem* p);

  /* ---- Helpers ---- */
  void collectProblems(vector<const Problem*>& out) const;
  OperationPlan* getOwnerOperationPlan(const Problem* p) const;
  bool isMovable(const OperationPlan* op) const;
  void loadScopeFromEnvironment();
  bool hasScope() const { return !config_.scopeOperationPlans.empty(); }
  bool isScopeOperationPlan(const OperationPlan* op) const;
  void lockForMrpRefresh(OperationPlan* op) const;
  void collectScopedProblems(vector<const Problem*>& out) const;

  AutoFixConfig config_;
};

/* Global function exposed to Python: ccAPPS.fix_conflicts(). */
PyObject* fix_conflicts(PyObject*, PyObject*);

}  // namespace ccAPPS
#endif  // SOLVERAUTOFIX_H

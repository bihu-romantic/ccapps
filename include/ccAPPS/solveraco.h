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
#ifndef SOLVERACO_H
#define SOLVERACO_H

#include <atomic>
#include <cmath>
#include <random>
#include <unordered_map>
#include <vector>

#include "ccAPPS/solver.h"

namespace ccAPPS {

/* Configuration parameters for the Ant Colony Optimization solver. */
struct ACOConfig {
  int ants = 20;
  int iterations = 100;
  double alpha = 1.0;
  double beta = 2.0;
  double evaporation = 0.1;
  double Q = 100.0;
  double tau0 = 1.0;
  int stagnation_limit = 30;
  double weight_tardiness = 1.0;
  double weight_cost = 0.5;
  double weight_setup = 0.3;
  double weight_makespan = 10.0;
  double weight_priority = 10.0;
  double weight_balance = 0.5;
  int elite_ants = 3;
  bool runMRP = true;
  bool joint_optimization = true;
  int aco_mrp_iterations = 2;      // ACO↔MRP outer loops (1 = legacy single-pass)
  double aco_mrp_improvement = 0.01;  // Min relative fitness gain to continue
  int purchase_material_mode = 1;  // 0 = infinite, 1 = current + lead time
  bool transfer_batch = false;     // Honor FlowTransferBatch timings in ACO
  int max_candidate_combinations_per_op = 64;  // Beam width for multi-resource alternatives
};

/* A candidate operation plan that an ant can select.
 * For operations requiring multiple constrained resources simultaneously,
 * one candidate covers all required resources so the operation occupies
 * them all at the same time. */
struct CandidateOp {
  OperationPlan* op;
  Operation* selectedOperation = nullptr;  // Optional alternate leaf operation.
  const Resource* res;  // primary execution resource for duration/cost heuristics
  Date earliestStart;   // constrained by material availability + upstream deps
  int candId = 0;       // shared by candidates of the same operation plan
  vector<const Resource*> allResources;  // resources actually occupied by this candidate
  vector<pair<const Load*, const Resource*>> loadAssignments;  // selected resource for each constrained load
};

/* A single ant's solution for resource scheduling. */
struct AntSolution {
  // Per-resource ordered sequences
  unordered_map<const Resource*, vector<OperationPlan*>> sequences;
  // Per-resource start/end dates. startDates is the processing start.
  unordered_map<const Resource*, vector<Date>> startDates;
  unordered_map<const Resource*, vector<Date>> endDates;
  // Changeover start for a GA operation. Absent or equal to the processing
  // start when the setup matrix adds no time.
  unordered_map<const OperationPlan*, Date> setupStarts;
  // Actual resources selected for each operation in joint mode
  unordered_map<const OperationPlan*, vector<const Resource*>> selectedResources;
  // Exact load-to-resource mapping selected for each operation
  unordered_map<const OperationPlan*, vector<pair<const Load*, const Resource*>>>
      selectedLoadAssignments;
  // Optional operation definition selected for an alternate operationplan.
  unordered_map<const OperationPlan*, Operation*> selectedOperations;
  // Count of operations that couldn't be scheduled into the solution.
  size_t unscheduledCount = 0;
  // Weighted penalty contribution of operations that couldn't be scheduled.
  double unscheduledPenalty = 0.0;
  // The GA decoder's internal WIP ledger is always a hard feasibility
  // condition.  External purchased material can still use postcheck mode.
  bool internalMaterialFeasible = true;
  // Fitness value (higher is better)
  double fitness = -numeric_limits<double>::max();
};

Duration estimateOperationDuration(const OperationPlan* op, const Resource* res);

/* Pheromone matrix for transitions between operationplans on a resource. */
class PheromoneMatrix {
 public:
  PheromoneMatrix() = default;
  double get(const OperationPlan* from, const OperationPlan* to) const;
  void set(const OperationPlan* from, const OperationPlan* to, double value);
  void evaporate(double rho);
  void deposit(const vector<OperationPlan*>& sequence, double amount);
  void reset(double tau0);

 private:
  struct PairHash {
    size_t operator()(
        const pair<const OperationPlan*, const OperationPlan*>& p) const {
      return hash<const void*>()(p.first) ^
             (hash<const void*>()(p.second) << 1);
    }
  };
  unordered_map<pair<const OperationPlan*, const OperationPlan*>, double,
                PairHash> matrix_;
};

/* Ant Colony Optimization solver for resource scheduling.
 *
 * Takes manufacturing orders from MRP and produces an optimized schedule
 * per resource, considering:
 *   1. Resource selection (multi-resource operations)
 *   2. Setup times (SetupMatrix)
 *   3. Due dates (demand due)
 *   4. Upstream dependencies (cross-resource blocking)
 *   5. Material availability (purchase order arrival dates)
 */
class SolverACO : public SolverCreate {
 public:
  using SolverCreate::solve;

  SolverACO() : rng_(random_device{}()) { initType(metadata); }
  ~SolverACO() override {}

  static int initialize();
  static PyObject* create(PyTypeObject*, PyObject*, PyObject*);
  const MetaClass& getType() const override { return *metadata; }
  static const MetaClass* metadata;

  void setConfig(const ACOConfig& cfg) { config_ = cfg; }
  const ACOConfig& getConfig() const { return config_; }
  void setRunMRP(bool b) { config_.runMRP = b; }
  bool getRunMRP() const { return config_.runMRP; }
  void setPurchaseMaterialMode(int m) { config_.purchase_material_mode = m; }
  int getPurchaseMaterialMode() const { return config_.purchase_material_mode; }
  void setTransferBatch(bool b) { config_.transfer_batch = b; }
  bool getTransferBatch() const { return config_.transfer_batch; }

  void solve(void* v = nullptr) override;
  void solve(const Resource* res, void* v = nullptr) override;
  void solveJoint(const vector<const Resource*>& resources);
  bool preserveRawMRPResult() const override { return true; }

  void solve(const ResourceInfinite* r, void* v = nullptr) override {
    SolverCreate::solve(r, v);
  }
  void solve(const ResourceBuckets* r, void* v = nullptr) override {
    SolverCreate::solve(r, v);
  }

  void initPheromone(const Resource* res);
  const PheromoneMatrix* getPheromone(const Resource* res) const;

 protected:
  /* ---- Candidate building ---- */
  vector<CandidateOp> buildCandidates(
      const vector<const Resource*>& resources) const;

  Date earliestStart(const OperationPlan* op, const Resource* res) const;
  Date purchaseMaterialAvailable(const Buffer* buf) const;
  Date dynamicEarliestStart(
      const OperationPlan* op,
      const unordered_map<const Buffer*, Date>& materialAvailable) const;

  /* ---- Single-resource (legacy) ---- */
  AntSolution constructSolution(
      const Resource* res, const vector<OperationPlan*>& plans);
  void localSearch(const Resource* res, AntSolution& solution);
  double evaluate(const AntSolution& solution);
  bool hasMaterialShortage(const AntSolution& solution) const;

  /* ---- Multi-resource joint ---- */
  AntSolution constructJointSolution(
      const vector<const Resource*>& resources,
      const vector<CandidateOp>& allCandidates,
      const unordered_map<const Resource*, Date>& resourceTimes);
  void localSearchJoint(AntSolution& solution);
  void compactSchedule(AntSolution& ant,
      const vector<const Resource*>& resources,
      const unordered_map<const Resource*, Date>& resourceTimes,
      const vector<OperationPlan*>* preferredOrder = nullptr);

  /* ---- Shared helpers ---- */
  double heuristic(const OperationPlan* from, const OperationPlan* to) const;
  void applyBestSolution(const AntSolution& best);
  vector<const Resource*> getConstrainedResources(
      const OperationPlan* op) const;
  bool isUpstreamBlocked(
      const OperationPlan* op,
      const unordered_map<const Resource*, Date>& resourceTimes) const;
  Duration computeUpstreamWait(
      const OperationPlan* op,
      const unordered_map<const Resource*, Date>& resourceTimes) const;
  Duration computeSetupTime(const OperationPlan* from,
                            const OperationPlan* to) const;
  bool shouldLockOptimizedPlan(const OperationPlan* op) const;
  void lockOptimizedPlan(OperationPlan* op);

  ACOConfig config_;
  unordered_map<const Resource*, PheromoneMatrix> pheromones_;
  mt19937 rng_;
  double lastBestFitness_ = -numeric_limits<double>::max();
  bool stagnationOccurred_ = false;
  atomic<bool> mrpRefreshInProgress_{false};
};

/* Global function exposed to Python as ccAPPS.run_aco(). */
PyObject* run_aco(PyObject*, PyObject*);

}  // namespace ccAPPS
#endif  // SOLVERACO_H

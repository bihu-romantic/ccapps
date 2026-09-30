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
#ifndef SOLVERGA_H
#define SOLVERGA_H

#include <random>
#include <unordered_map>
#include <vector>

#include "ccAPPS/solveraco.h"

namespace ccAPPS {

/* Configuration parameters for the Genetic Algorithm solver.
 *
 * The GA reuses ACO's candidate building, constraint-aware scheduling,
 * evaluation and solution application logic. Only the metaheuristic search
 * strategy is different.
 */
struct GAConfig {
  // precheck: GA treats inventory and existing supply receipts as hard
  // constraints. A shortage rejects the GA result.
  // postcheck: GA sequences capacity only, then MRP refreshes proposed PO/DO.
  enum MaterialCheckMode { PRECHECK = 0, POSTCHECK = 1 };
  unsigned int seed = 123456U;
  // pop=500, generations=50; stagnation early-stop when no improvement.
  // VNS on at ratio 0.02. time_limit is a secondary stop.
  int population = 500;
  int generations = 50;
  int tournament_size = 4;
  double mutation_rate = 0.15;
  // Stop after this many consecutive generations without a new best score.
  int stagnation_limit = 10;
  bool enable_vns = true;
  double vns_ratio = 0.02;
  // Secondary stop condition (seconds). 0 = disabled.
  double time_limit_seconds = 60.0;
  double memory_size = 0.2;
  bool use_memory_tournament = true;
  bool use_random_tournament = false;
  bool use_uniform_ms_crossover = true;
  bool use_pox_os_crossover = true;
  bool use_random_os_crossover = false;
  bool use_random_ms_mutation = true;
  bool use_best_ms_mutation = false;
  bool use_random_os_mutation = true;
  bool use_auto_crossover = true;
  bool overlap_enabled = false;
  int overlap_batch_count = 1;
  bool runMRP = true;
  bool joint_optimization = true;
  int purchase_material_mode = 1;
  MaterialCheckMode material_check_mode = POSTCHECK;
  int max_candidate_combinations_per_op = 64;
  // Scalarization weights for the normalized multi-objective fitness.
  // Makespan, resource load balance and priority-weighted tardiness.
  double objective_makespan_weight = 0.6;
  double objective_balance_weight = 0.1;
  double objective_tardiness_weight = 0.3;
};

class SolverGA : public SolverCreate {
 public:
  using SolverCreate::solve;

  SolverGA() : rng_(12345U) { initType(metadata); }
  ~SolverGA() override {}

  static int initialize();
  static PyObject* create(PyTypeObject*, PyObject*, PyObject*);
  const MetaClass& getType() const override { return *metadata; }
  static const MetaClass* metadata;

  void setConfig(const GAConfig& cfg);
  const GAConfig& getGAConfig() const { return config_; }
  void setRunMRP(bool b) { config_.runMRP = b; }
  bool getRunMRP() const { return config_.runMRP; }
  void setSeed(unsigned int s) {
    config_.seed = s;
    rng_.seed(config_.seed);
  }
  unsigned int getSeed() const { return config_.seed; }
  void setPurchaseMaterialMode(int m) {
    config_.purchase_material_mode = m;
  }
  int getPurchaseMaterialMode() const { return config_.purchase_material_mode; }
  void setMaterialCheckMode(GAConfig::MaterialCheckMode m) {
    config_.material_check_mode = m;
  }
  GAConfig::MaterialCheckMode getMaterialCheckMode() const {
    return config_.material_check_mode;
  }

  void solve(void* v = nullptr) override;
  void solve(const Resource* res, void* v = nullptr) override;
  void solveJoint(const vector<const Resource*>& resources);
  bool preserveRawMRPResult() const override { return true; }
  Date gaEarliestStart(const OperationPlan* op, const Resource* res) const {
    return earliestStart(op, res);
  }
  bool gaHasMaterialShortage(const AntSolution& solution) const {
    return hasMaterialShortage(solution);
  }
  // Rebuild movable external PO/DO after an explicit full-network GA run.
  // Kept public for the C API entry point, which bypasses solve() when it
  // dispatches a recognized CCPL finished-product flow line directly.
  void refreshPostcheckSupply();
  // Enforce exported internal WIP FlowPlan availability after GA writeback.
  // This is a forward repair only: a consumer is moved when its actual
  // cumulative input is short; no operation is ever pulled earlier.
  void enforceInternalMaterialAvailability();
  // Align delivery promises after GA moves manufacturing: commitment =
  // max(batch receipt, demand due). Clears temporary FG shortages from
  // stale MRP delivery dates and drops promises that lagged past receipt.
  void alignDeliveryCommitments();
  // Move existing, movable purchase proposals to the first real post-GA
  // consumption of their receipt buffer.  This deliberately doesn't create
  // or delete supply and is safe for the CCPL joint forward path.
  void synchronizeProposedPurchases();
  void synchronizeProposedDistributions();

 private:
  // Former SolverACO helpers, copied in to decouple GA from ACO. See
  // solverga.cpp for the implementations.
  Date earliestStart(const OperationPlan* op, const Resource* res) const;
  Date purchaseMaterialAvailable(const Buffer* buf) const;
  vector<CandidateOp> buildCandidates(
      const vector<const Resource*>& resources) const;
  void applyBestSolution(const AntSolution& best);
  bool hasMaterialShortage(const AntSolution& sol) const;

  void runGAMRP(void* v);
  vector<const Buffer*> collectShortageBuffers() const;
  void refreshProposedSupply(const vector<const Buffer*>& buffers);
  vector<const Resource*> collectBottlenecks() const;
  vector<CandidateOp> buildSingleResourceCandidates(
      const Resource* res,
      const vector<OperationPlan*>& plans) const;
  AntSolution evolve(
      const vector<const Resource*>& resources,
      const vector<CandidateOp>& candidates,
      const unordered_map<const Resource*, Date>& resourceTimes);

  GAConfig config_;
  mt19937 rng_;
  double lastBestFitness_ = -numeric_limits<double>::max();
  bool stagnationOccurred_ = false;
  bool lastKeptBaseline_ = false;
};

PyObject* run_ga(PyObject*, PyObject*);

}  // namespace ccAPPS
#endif  // SOLVERGA_H

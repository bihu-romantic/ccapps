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
#ifndef SOLVEREXACT_H
#define SOLVEREXACT_H

#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "ccAPPS/solveraco.h"

namespace ccAPPS {

/* Configuration parameters for the exact resource scheduler.
 *
 * The exact solver optimizes the same candidate model and fitness function as
 * ACO, but searches the complete assignment/sequence space for small
 * bottleneck subproblems. Larger or infeasible problems keep the current MRP
 * result unchanged.
 */
struct ExactConfig {
  bool runMRP = true;
  int purchase_material_mode = 1;
  int max_operations = 500;
  bool prefer_mip = true;
  int time_bucket_seconds = 900;
  int mip_time_limit_seconds = 30;

  double weight_tardiness = 1.0;
  double weight_cost = 0.5;
  double weight_setup = 0.3;
  double weight_priority = 10.0;
  double weight_balance = 0.5;
};

class SolverExact : public SolverACO {
 public:
  using SolverACO::solve;

  SolverExact() { initType(metadata); }
  ~SolverExact() override {}

  static int initialize();
  static PyObject* create(PyTypeObject*, PyObject*, PyObject*);
  const MetaClass& getType() const override { return *metadata; }
  static const MetaClass* metadata;

  void setConfig(const ExactConfig& cfg);
  const ExactConfig& getExactConfig() const { return config_; }
  void setRunMRP(bool b) { config_.runMRP = b; syncACOConfig(); }
  bool getRunMRP() const { return config_.runMRP; }
  void setPurchaseMaterialMode(int m) {
    config_.purchase_material_mode = m;
    syncACOConfig();
  }
  int getPurchaseMaterialMode() const { return config_.purchase_material_mode; }

  void solve(void* v = nullptr) override;
  void solveJoint(const vector<const Resource*>& resources);

  void solve(const Resource* r, void* v = nullptr) override {
    SolverCreate::solve(r, v);
  }
  void solve(const ResourceInfinite* r, void* v = nullptr) override {
    SolverCreate::solve(r, v);
  }
  void solve(const ResourceBuckets* r, void* v = nullptr) override {
    SolverCreate::solve(r, v);
  }

 private:
  void syncACOConfig();
  vector<const Resource*> collectBottlenecks() const;
  AntSolution solveTimeIndexedMIP(
      const vector<const Resource*>& resources,
      const vector<CandidateOp>& candidates);
  void keepMRPResult(const char* reason) const;

  ExactConfig config_;
  bool exactApplied_ = false;
};

PyObject* run_exact(PyObject*, PyObject*);

}  // namespace ccAPPS
#endif  // SOLVEREXACT_H

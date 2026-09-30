/***************************************************************************
 * Deterministic forward finite-capacity scheduler.
 *
 * This solver owns its demand sequence and finite forward schedule. Supply
 * creation for each selected demand is delegated to the standard MRP
 * single-demand expansion kernel, so order-creation rules stay identical.
 ***************************************************************************/
#pragma once
#ifndef SOLVERFAS_H
#define SOLVERFAS_H

#include "ccAPPS/solver.h"

#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace ccAPPS {

class SolverFAS : public Solver {
 public:
  using Solver::solve;

  SolverFAS() { initType(metadata); }
  ~SolverFAS() override {}

  static int initialize();
  static PyObject* create(PyTypeObject*, PyObject*, PyObject*);
  static PyObject* commit(PyObject*, PyObject*);
  static PyObject* rollback(PyObject*, PyObject*);
  const MetaClass& getType() const override { return *metadata; }
  static const MetaClass* metadata;

  /* Only moves proposed supply orders by default. Approved orders can be
   * included explicitly, while confirmed, completed and closed orders are
   * immutable. This includes manufacturing, purchase and distribution supply. */
  bool getMoveApproved() const { return moveApproved; }
  void setMoveApproved(bool value) { moveApproved = value; }

  Duration getLazyDelay() const { return lazyDelay; }
  void setLazyDelay(Duration value) { lazyDelay = value; }
  Duration getMinimumDelay() const { return minimumDelay; }
  void setMinimumDelay(Duration value) { minimumDelay = value; }
  Duration getAdministrativeLeadTime() const { return administrativeLeadTime; }
  void setAdministrativeLeadTime(Duration value) { administrativeLeadTime = value; }
  bool getRotateResources() const { return rotateResources; }
  void setRotateResources(bool value) { rotateResources = value; }
  unsigned long getIterationMax() const { return iterationMax; }
  void setIterationMax(unsigned long value) { iterationMax = value; }
  unsigned long getResourceIterationMax() const { return resourceIterationMax; }
  void setResourceIterationMax(unsigned long value) { resourceIterationMax = value; }
  Duration getAutoFence() const { return Plan::instance().getAutoFence(); }
  void setAutoFence(Duration value) { Plan::instance().setAutoFence(value); }

  // Keep the public planning controls aligned with solver_mrp.  The values
  // are passed unchanged to the shared supply-expansion kernel and also
  // govern FAS capacity/lead-time scheduling behavior.
  short getPlanType() const { return planType; }
  void setPlanType(short value) {
    if (value < 1 || value > 3) throw DataException("Invalid plan type");
    planType = value;
  }
  short getConstraints() const { return constraints; }
  void setConstraints(short value) {
    constraints = value & (SolverCreate::CAPACITY |
                           SolverCreate::MFG_LEADTIME |
                           SolverCreate::PO_LEADTIME);
    if (value & SolverCreate::LEADTIME)
      constraints |= SolverCreate::MFG_LEADTIME | SolverCreate::PO_LEADTIME;
  }
  bool isCapacityConstrained() const {
    return constraints & SolverCreate::CAPACITY;
  }
  bool isLeadTimeConstrained(const Operation* operation) const {
    // Delegate classification (purchase, subcontracting, alternates, etc.)
    // to the exact same implementation used by the MRP supply expander.
    return supplyExpander.isLeadTimeConstrained(operation);
  }

  void solve(void* v = nullptr) override;
  bool commitPlan();
  void rollbackPlan();
  CommandManager* getCommandManager() { return &commands; }

  /* Run a reversible forward trial for a temporary supply network.  The
   * caller owns the command-manager bookmark: every resource assignment and
   * date move is appended to that same stream, so rolling the bookmark back
   * restores both the supply network and its resource timelines. */
  Date trialForwardNetwork(OperationPlan* root, CommandManager* transaction);

  /* Build standard supply for a selected order through the shared MRP
   * expansion kernel. This doesn't invoke the MRP global order loop. */
  void expandDemand(Demand* demand);

 private:
  static Date runForwardTrial(void*, OperationPlan*, CommandManager*);
  struct Reservation {
    Date start;
    Date end;
    double quantity;
    const OperationPlan* operationplan = nullptr;
  };
  std::unordered_set<OperationPlan*> collectNetwork(
      const std::vector<OperationPlan*>& roots) const;
  /* Executable supply network of the selected orders: the movable leaf plans
   * plus the sequence relations between them. The forward pass consumes the
   * edges directly instead of a precomputed order, so it can dispatch each
   * plan the moment its own prerequisites are ready. */
  struct ExecutionGraph {
    std::vector<OperationPlan*> work;
    std::unordered_map<OperationPlan*, std::vector<OperationPlan*>> downstream;
    std::unordered_map<OperationPlan*, size_t> indegree;
  };
  ExecutionGraph buildExecutionGraph(
      const std::unordered_set<OperationPlan*>& network,
      const std::unordered_set<OperationPlan*>& fixedPlans) const;
  void reserveFixedNetwork(
      const std::unordered_set<OperationPlan*>& inScope,
      std::unordered_map<const Resource*, std::vector<Reservation>>& plans) const;
  Date finishOf(const std::vector<OperationPlan*>& roots) const;
  /* Ranking key for a candidate alternate: *lower is better*. The shared MRP
   * kernel selects alternates by cost and priority, which is load blind, so
   * with equal costs every order of a pool picks the same machine. This probe
   * supplies a capacity- and load-aware ranking without running a complete
   * forward schedule per candidate.
   *
   * The key is not a physical completion date: it weighs the candidate's own
   * processing time against the load it would add to the machines it occupies.
   * See the definition for why the weighting is necessary. */
  Date probeAlternateCompletion(OperationPlan* root);
  /* Structural contention of one concrete resource: how much *other* work can
   * still be routed to it, estimated from the number of load definitions that
   * consume it. Memoised for the duration of a single solve(). Needed because
   * a pool that hasn't been planned yet looks completely idle, which makes an
   * unrelated single-step order pick it on processing time alone. */
  Duration contentionOf(const Resource* resource);
  /* Earliest date at which `resource` can host a `duration` interval of
   * `required` capacity starting from `start`, ignoring the candidate's own
   * plans. Returns Date::infiniteFuture when the window fits. */
  Date probeResourceConflict(
      const Resource* resource, Date start, Duration duration, double required,
      const std::unordered_set<const OperationPlan*>& own) const;
  bool isMovable(const OperationPlan* op) const;
  /* Python-style relocate local search over the job decision sequence.
   * Re-runs the forward pass (calendar + material) under trial bookmarks;
   * keeps a neighbour only when it shortens forwardCompletion. */
  void improveJobOrder(
      const std::vector<OperationPlan*>& roots,
      const std::unordered_set<OperationPlan*>& fixed,
      std::vector<OperationPlan*>& jobSequence);
  bool moveApproved = false;
  Duration lazyDelay = Duration(86400L);
  Duration minimumDelay = Duration(3600L);
  Duration administrativeLeadTime;
  bool rotateResources = true;
  unsigned long iterationMax = 0;
  unsigned long resourceIterationMax = 500;
  short planType = 1;
  short constraints = SolverCreate::CAPACITY | SolverCreate::MFG_LEADTIME |
                      SolverCreate::PO_LEADTIME;
  CommandManager commands;
  SolverCreate supplyExpander;
  // SolverCreate's queue kernel can commit its internal commands at demand
  // boundaries. Keep an explicit model snapshot so a failed FAS transaction
  // still removes every supply plan it created.
  std::unordered_set<OperationPlan*> plansAtStart;
  CommandManager* activeCommands = nullptr;
  std::vector<OperationPlan*> trialRoots;
  Date trialCompletion = Date::infiniteFuture;
  CommandManager* trialTransaction = nullptr;
  // Number of alternate-probe calls during the last supply expansion. Reported
  // at the end of a run so the cost of capacity-aware alternate selection
  // stays visible.
  unsigned long alternateProbes = 0;
  // Memoised contentionOf() results, keyed by resource. Cleared per solve().
  std::unordered_map<const Resource*, Duration> contentionCache;
  // Plans improveJobOrder trials must hold fixed (inherited safety-stock set).
  std::unordered_set<OperationPlan*> trialFixedPlans;
  // Latest end the forward pass placed, i.e. the completion of the scheduled
  // network. Improve rounds are compared on this rather than on routing-parent
  // dates (those carry the delivery promise, not the schedule).
  Date forwardCompletion = Date::infinitePast;
  // When non-empty, the forward pass uses this job sequence instead of
  // re-sorting by priority/due/LPT. Used by improveJobOrder trials.
  std::vector<OperationPlan*> overrideJobSequence;
};

PyObject* run_fas(PyObject*, PyObject*);

}  // namespace ccAPPS
#endif  // SOLVERFAS_H

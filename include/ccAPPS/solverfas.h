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

#include <unordered_set>

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
  std::vector<OperationPlan*> orderNetwork(
      const std::unordered_set<OperationPlan*>& network,
      const std::unordered_set<OperationPlan*>& fixedPlans) const;
  void reserveFixedNetwork(
      const std::unordered_set<OperationPlan*>& inScope,
      std::unordered_map<const Resource*, std::vector<Reservation>>& plans,
      std::unordered_map<const Resource*, Date>& serialReady) const;
  Date finishOf(const std::vector<OperationPlan*>& roots) const;
  static bool demandFirst(const OperationPlan* left,
                          const OperationPlan* right);
  bool isMovable(const OperationPlan* op) const;
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
};

PyObject* run_fas(PyObject*, PyObject*);

}  // namespace ccAPPS
#endif  // SOLVERFAS_H

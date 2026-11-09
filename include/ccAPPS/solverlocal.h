#pragma once
#ifndef SOLVERLOCAL_H
#define SOLVERLOCAL_H

#include "ccAPPS/model.h"

#include <string>
#include <unordered_set>
#include <utility>
#include <vector>

namespace ccAPPS {

/* Shared local-reschedule scope (订单/物料/资源触发 + hot/freeze).
 * Algorithms (CP / FAS / MRP / GA) only repair the movable set; they must
 * not rebuild trigger→scope logic.
 */
bool localRescheduleEnabled();

struct LocalScopeGuard {
  std::vector<std::pair<OperationPlan*, std::string>> frozen;
  size_t hotRoots = 0;
  size_t frozenRoots = 0;
  bool skipOptimization = false;
  bool active = false;
  std::string mode = "order";
  LocalScopeGuard();
  ~LocalScopeGuard();
};

void applyLocalRescheduleScope(LocalScopeGuard& guard);

/* Shared plan.constraint / plantype for MRP, FAS, CP, GA.
 * Reads CCAPPS_CONSTRAINT + CCAPPS_PLANTYPE (set by runplan / reschedule). */
short planConstraintsFromEnv();
short planTypeFromEnv();
bool planEnforceCapacity();
bool planEnforceMfgLeadTime();
bool planEnforcePoLeadTime();
bool planEnforceFence();
void applyPlanConstraintsToCreate(class SolverCreate& solver);
void applyPlanConstraintsToFas(class SolverFAS& solver);

/* Outcome / observability (solver-agnostic names; cp_* remain aliases). */
void localSetLastApplied(size_t n);
size_t localLastApplied();
void localSetLastOk(int ok);
int localLastOk();
void localSetStage(const std::string& stage);
std::string localLastStage();
void localSetPreflightConflicts(const std::string& text);
std::string localLastPreflightConflicts();

std::unordered_set<Buffer*> localChangedMaterials();
std::unordered_set<std::string> localNewDemands();
/* Demands allowed to receive new supply during a local reschedule.
 * Empty for resource, material, delete, and priority-only changes.
 * Inserts and quantity increases are limited to local.new_demands. */
std::unordered_set<std::string> localSupplyExpansionNames();

/* Resource-change repair: keep the existing sequence and push every movable
 * load on a changed resource to the next open capacity. A zero calendar is
 * a delay, not a reason to leave the old dates in the outage. */
size_t localDelayChangedResourcePlans();

PyObject* local_last_ok(PyObject*, PyObject*);
PyObject* local_last_outcome(PyObject*, PyObject*);
PyObject* local_last_stage(PyObject*, PyObject*);
PyObject* local_last_applied(PyObject*, PyObject*);
PyObject* local_last_preflight_conflicts(PyObject*, PyObject*);
PyObject* local_mark_candidate_no_supply(PyObject*, PyObject*);

}  // namespace ccAPPS
#endif

#include "ccAPPS/solvercp.h"

#include <algorithm>
#include <cstdlib>
#include <map>
#include <set>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace ccAPPS {
namespace {

int workshopOf(OperationPlan* plan) {
  int result = 0;
  for (auto load = plan->beginLoadPlans(); load != plan->endLoadPlans(); ++load) {
    Resource* resource = load->getResource();
    if (!load->isStart() || !resource || resource->getCategory() != "人员")
      continue;
    const string& name = resource->getName();
    if (name.find("制造部1工段") != string::npos) result = 1;
    else if (name.find("制造部2工段") != string::npos ||
             name.find("制造部3工段") != string::npos)
      result = 23;
    else if (name.find("制造部4工段") != string::npos) result = 4;
    else if (name.find("制造部5工段") != string::npos) result = 5;
  }
  return result;
}

OperationPlan* partitionRoot(OperationPlan* plan) {
  while (plan && plan->getOwner()) plan = plan->getOwner();
  return plan;
}

bool partitionMovable(const OperationPlan* plan) {
  return plan && (plan->getProposed() || plan->getApproved()) &&
         !plan->getConfirmed() && !plan->getCompleted() && !plan->getClosed();
}

}  // namespace

namespace {
size_t g_lastPartitionOptimized = 0;
}

size_t cpLastPartitionOptimized() { return g_lastPartitionOptimized; }
void cpSetLastPartitionOptimized(size_t n) { g_lastPartitionOptimized = n; }

PyObject* run_cp_partitioned(PyObject*, PyObject*) {
  g_lastPartitionOptimized = 0;
  if (getenv("CP_LOCAL_RESCHEDULE") &&
      string(getenv("CP_LOCAL_RESCHEDULE")) != "0")
    logger << "Partitioned CP: CP_LOCAL_RESCHEDULE is ignored; local "
              "reschedule currently supports the full run_cp entry only\n"
           << flush;
  logger << "Partitioned CP enter (lot_split="
         << (getenv("CP_LOT_SPLIT") ? getenv("CP_LOT_SPLIT") : "?")
         << " only="
         << (getenv("CP_PARTITION_ONLY") ? getenv("CP_PARTITION_ONLY") : "-")
         << ")\n"
         << flush;
  const Date origin = Plan::instance().getCurrent();
  int days = 3;
  if (const char* value = getenv("CP_HORIZON_DAYS"))
    try { days = max(1, stoi(value)); } catch (...) {}
  const Date windowEnd = origin + Duration(static_cast<long>(days) * 86400L);

  vector<OperationPlan*> tasks;
  unordered_map<OperationPlan*, int> workshop;
  unordered_map<OperationPlan*, vector<OperationPlan*>> byRoot;
  for (auto plan = OperationPlan::begin(); plan != OperationPlan::end(); ++plan) {
    if (!plan->getOperation() || plan->getQuantity() <= 0.0) continue;
    if (!plan->getOperation()->hasType<OperationTimePer, OperationFixedTime>())
      continue;
    if (plan->getOperation()->hasType<OperationItemSupplier,
                                      OperationItemDistribution, OperationSplit>() ||
        plan->getOperation()->hasType<OperationRouting, OperationAlternate>())
      continue;
    bool constrained = false;
    for (auto load = plan->beginLoadPlans(); load != plan->endLoadPlans(); ++load)
      if (load->isStart() && load->getResource() &&
          load->getResource()->getConstrained()) {
        constrained = true;
        break;
      }
    if (!constrained) continue;
    tasks.push_back(&*plan);
    workshop[&*plan] = workshopOf(&*plan);
    byRoot[partitionRoot(&*plan)].push_back(&*plan);
  }

  unordered_set<OperationPlan*> active;
  for (OperationPlan* task : tasks) {
    if (!partitionMovable(task)) continue;
    if (task->getStart() < windowEnd && task->getEnd() > origin)
      for (OperationPlan* sibling : byRoot[partitionRoot(task)])
        if (partitionMovable(sibling)) active.insert(sibling);
  }

  bool changed = true;
  while (changed) {
    changed = false;
    vector<Buffer*> required;
    for (OperationPlan* task : active)
      for (auto flow = task->beginFlowPlans(); flow != task->endFlowPlans(); ++flow)
        if (flow->getQuantity() < -ROUNDING_ERROR && flow->getBuffer())
          required.push_back(flow->getBuffer());
    sort(required.begin(), required.end());
    required.erase(unique(required.begin(), required.end()), required.end());
    for (OperationPlan* candidate : tasks) {
      if (!partitionMovable(candidate) || active.count(candidate)) continue;
      bool produces = false;
      for (auto flow = candidate->beginFlowPlans();
           flow != candidate->endFlowPlans(); ++flow)
        if (flow->getQuantity() > ROUNDING_ERROR && flow->getBuffer() &&
            binary_search(required.begin(), required.end(), flow->getBuffer())) {
          produces = true;
          break;
        }
      if (!produces) continue;
      for (OperationPlan* sibling : byRoot[partitionRoot(candidate)])
        if (partitionMovable(sibling) && active.insert(sibling).second)
          changed = true;
    }
  }

  unordered_map<Buffer*, set<int>> bufferWorkshops;
  unordered_map<Buffer*, vector<OperationPlan*>> bufferTasks;
  for (OperationPlan* task : tasks) {
    const int group = workshop[task];
    if (!group) continue;
    for (auto flow = task->beginFlowPlans(); flow != task->endFlowPlans(); ++flow) {
      Buffer* buffer = flow->getBuffer();
      if (!buffer || !buffer->getItem() ||
          buffer->getItem()->getCategory() != "自制")
        continue;
      bufferWorkshops[buffer].insert(group);
      bufferTasks[buffer].push_back(task);
    }
  }
  unordered_set<OperationPlan*> boundary;
  size_t boundaryBuffers = 0;
  for (const auto& entry : bufferWorkshops) {
    if (entry.second.size() <= 1) continue;
    ++boundaryBuffers;
    for (OperationPlan* task : bufferTasks[entry.first])
      if (active.count(task)) boundary.insert(task);
  }

  // Default: solve 2/3 before 1. Group 1 CP otherwise mutates shared
  // material/calendar state and makes the later 2/3 FAS seed infeasible.
  vector<int> groups{23, 1, 4, 5};
  if (const char* only = getenv("CP_PARTITION_ONLY")) {
    try {
      const int want = stoi(only);
      groups.clear();
      if (want > 0) groups.push_back(want);
    } catch (...) {}
  } else if (const char* order = getenv("CP_PARTITION_ORDER")) {
    groups.clear();
    string token;
    for (char ch : string(order) + ",") {
      if (ch == ',' || ch == ' ') {
        if (!token.empty()) {
          try { groups.push_back(stoi(token)); } catch (...) {}
          token.clear();
        }
      } else token.push_back(ch);
    }
  }
  size_t optimized = 0;
  string originalLimit;
  int totalLimit = 600;
  if (const char* value = getenv("CP_TIME_LIMIT_SECONDS")) {
    originalLimit = value;
    try { totalLimit = max(4, stoi(value)); } catch (...) {}
  }
  const string groupLimit = to_string(max(1, totalLimit / 4));
  setenv("CP_TIME_LIMIT_SECONDS", groupLimit.c_str(), 1);

  // Default off: confirming foreign boundary into AddNoOverlap made models
  // INFEASIBLE (FAS self-overlaps). Isolation is CP_PARTITION_GROUP in run_cp.
  const bool lockBoundary =
      getenv("CP_PARTITION_LOCK_BOUNDARY") != nullptr &&
      string(getenv("CP_PARTITION_LOCK_BOUNDARY")) == "1";

  string groupSaved;
  const char* previousGroup = getenv("CP_PARTITION_GROUP");
  if (previousGroup) groupSaved = previousGroup;

  logger << "Partitioned CP scope: " << active.size() << "/" << tasks.size()
         << " operations, " << boundaryBuffers << " cross-workshop selfmade "
         << "buffers, " << boundary.size() << " fixed boundary operations, "
         << groupLimit << "s per group\n";

  for (int group : groups) {
    vector<OperationPlan*> lockedNow;
    size_t groupTasks = 0;
    size_t groupAll = 0;
    for (OperationPlan* task : tasks) {
      if (workshop[task] == group) ++groupAll;
      if (workshop[task] == group && active.count(task) && !boundary.count(task))
        ++groupTasks;
      // Foreign workshops stay proposed; run_cp filters them via
      // CP_PARTITION_GROUP. Optional boundary freeze only when explicitly on.
      if (lockBoundary && boundary.count(task) && workshop[task] != group &&
          task->getProposed()) {
        task->setStatusRaw("confirmed");
        lockedNow.push_back(task);
      }
    }
    if (groupTasks) {
      logger << "Partitioned CP group " << group << ": " << groupTasks
             << " active movable / " << groupAll << " same-ws lots, "
             << lockedNow.size() << " foreign boundary frozen"
             << (lockBoundary ? "" : " (boundary lock off)") << "\n";
      setenv("CP_PARTITION_GROUP", to_string(group).c_str(), 1);
      run_cp(nullptr, nullptr);
      optimized += cpLastRunApplied();
    }
    for (OperationPlan* task : lockedNow) task->setStatusRaw("proposed");
  }

  if (groupSaved.empty())
    unsetenv("CP_PARTITION_GROUP");
  else
    setenv("CP_PARTITION_GROUP", groupSaved.c_str(), 1);

  if (originalLimit.empty())
    unsetenv("CP_TIME_LIMIT_SECONDS");
  else
    setenv("CP_TIME_LIMIT_SECONDS", originalLimit.c_str(), 1);
  cpFinalizeXorSplits();
  g_lastPartitionOptimized = optimized;
  logger << "Partitioned CP completed: " << optimized
         << " operation assignments optimized; FAS boundary dates preserved\n";
  Py_RETURN_NONE;
}

}  // namespace ccAPPS

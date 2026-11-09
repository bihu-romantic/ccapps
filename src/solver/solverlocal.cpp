#include "ccAPPS/solverlocal.h"

#include "ccAPPS/solver.h"
#include "ccAPPS/solverfas.h"

#include <algorithm>
#include <cctype>
#include <cstring>
#include <cstdlib>
#include <string>
#include <unordered_set>
#include <utility>
#include <vector>

namespace ccAPPS {

namespace {

size_t g_lastLocalApplied = 0;
int g_lastLocalOk = 0;
std::string g_lastLocalStage = "not_run";
int g_lastLocalLevel = 0;
size_t g_lastIncomplete = 0;
size_t g_lastNamedLate = 0;
size_t g_lastWindowLate = 0;
std::string g_lastLocalMode;
int g_lastUnlockInWindow = 0;
std::string g_lastPreflightConflicts;

int envInt(const char* name, int fallback) {
  const char* raw = getenv(name);
  if (!raw || !*raw) return fallback;
  try {
    return std::stoi(raw);
  } catch (...) {
    return fallback;
  }
}

std::string envString(const char* name, const char* fallback) {
  const char* raw = getenv(name);
  return (raw && *raw) ? std::string(raw) : std::string(fallback ? fallback : "");
}

int envLocalInt(const char* localKey, const char* cpKey, int fallback) {
  if (getenv(localKey) && *getenv(localKey)) return envInt(localKey, fallback);
  return envInt(cpKey, fallback);
}

std::string envLocalString(const char* localKey, const char* cpKey,
                           const char* fallback) {
  if (getenv(localKey) && *getenv(localKey))
    return envString(localKey, fallback);
  return envString(cpKey, fallback);
}

using string = std::string;
using std::max;
using std::min;
using std::flush;
using std::transform;
using std::tolower;
using std::stoi;
using std::vector;
using std::unordered_set;
using std::pair;

Demand* pegDemandOf(OperationPlan* plan) {
  for (OperationPlan* cursor = plan; cursor; cursor = cursor->getOwner())
    if (Demand* demand = cursor->getDemand()) return demand;
  return nullptr;
}

bool rootTouchesLocalWindow(OperationPlan* root, const Date& origin,
                            const Date& windowEnd) {
  if (!root) return false;
  vector<OperationPlan*> stack{root};
  while (!stack.empty()) {
    OperationPlan* node = stack.back();
    stack.pop_back();
    const Date start = node->getStart();
    const Date end = node->getEnd();
    const bool dated =
        start != Date::infinitePast && start != Date::infiniteFuture &&
        end != Date::infinitePast && end != Date::infiniteFuture;
    if (dated && start < windowEnd && end > origin) return true;
    for (OperationPlan::iterator child(node); child != node->end(); ++child)
      stack.push_back(&*child);
  }
  return false;
}

unordered_set<string> localNameListFromEnv(const char* envKey) {
  unordered_set<string> named;
  const string raw = envString(envKey, "");
  string token;
  for (size_t i = 0; i <= raw.size(); ++i) {
    const char ch = i < raw.size() ? raw[i] : ',';
    if (ch == ',' || ch == ';' || ch == '|' || ch == '\n') {
      size_t a = 0, b = token.size();
      while (a < b && isspace(static_cast<unsigned char>(token[a]))) ++a;
      while (b > a && isspace(static_cast<unsigned char>(token[b - 1]))) --b;
      if (a < b) named.insert(token.substr(a, b - a));
      token.clear();
      continue;
    }
    token.push_back(ch);
  }
  return named;
}

// ---------------------------------------------------------------------------
// Order-local reschedule framework (增 / 删 / 改 共用一套):
//   Only two named lists:
//     CP_LOCAL_NEW_DEMANDS  ∪  CP_LOCAL_DELETED_DEMANDS
//   (+ insert-tagged roots). Late / window-spill also seeds.
//   凡「改」(量 / 优先级 / 属性) = 同一张单先删再增:
//     DELETE ops+demand → INSERT 同名 (新字段);
//     deleted 与 new 填同一 demand 名.
//   New supply is narrower than the seed list:
//     resource / material → no demand expansion
//     delete / priority   → no demand expansion
//     insert / qty up     → only those names in NEW_DEMANDS
//     all                 → same, and only when an insert or increase exists
//   Safety stock is not replenished during a local reschedule.
//   Freeze is always the same L1/L2/L3 order scope.
// ---------------------------------------------------------------------------
unordered_set<string> localNewDemandNames() {
  {
  auto named = localNameListFromEnv("LOCAL_NEW_DEMANDS");
  if (named.empty()) named = localNameListFromEnv("CP_LOCAL_NEW_DEMANDS");
  return named;
}
}

unordered_set<string> localDeletedDemandNames() {
  {
  auto named = localNameListFromEnv("LOCAL_DELETED_DEMANDS");
  if (named.empty()) named = localNameListFromEnv("CP_LOCAL_DELETED_DEMANDS");
  return named;
}
}

unordered_set<string> localOrderNamedDemandNames() {
  unordered_set<string> named = localNewDemandNames();
  for (const string& name : localDeletedDemandNames()) named.insert(name);
  return named;
}

bool rootMatchesDemandNames(OperationPlan* root,
                            const unordered_set<string>& names) {
  if (!root || names.empty()) return false;
  vector<OperationPlan*> stack{root};
  while (!stack.empty()) {
    OperationPlan* node = stack.back();
    stack.pop_back();
    if (Demand* demand = pegDemandOf(node))
      if (names.count(demand->getName())) return true;
    for (OperationPlan::iterator child(node); child != node->end(); ++child)
      stack.push_back(&*child);
  }
  return false;
}

// Named order trigger: new ∪ deleted, or insert-tagged root.
bool rootIsLocalOrderNamedSeed(OperationPlan* root) {
  if (!root) return false;
  if (root->getBoolProperty("cp_local_insert", false)) return true;
  return rootMatchesDemandNames(root, localOrderNamedDemandNames());
}

// Late / window-spill seeds (general order repair).
bool rootIsLocalOrderLateSeed(OperationPlan* root, const Date& origin,
                              const Date& windowEnd) {
  if (!root) return false;
  vector<OperationPlan*> stack{root};
  while (!stack.empty()) {
    OperationPlan* node = stack.back();
    stack.pop_back();
    if (Demand* demand = pegDemandOf(node)) {
      const Date start = node->getStart();
      const Date end = node->getEnd();
      const bool dated =
          start != Date::infinitePast && start != Date::infiniteFuture &&
          end != Date::infinitePast && end != Date::infiniteFuture;
      const Date due = demand->getDue();
      if (due != Date::infinitePast && due != Date::infiniteFuture) {
        if (dated && end > due) return true;
        if (due >= origin && due < windowEnd) {
          if (!dated) return true;
          if (end > due || end > windowEnd) return true;
        }
      }
    }
    for (OperationPlan::iterator child(node); child != node->end(); ++child)
      stack.push_back(&*child);
  }
  return false;
}

// Order-mode L1 seed: named order changes ∪ late/window-spill.
// Everything else is locked at L1.
bool rootIsLocalOrderSeed(OperationPlan* root, const Date& origin,
                          const Date& windowEnd) {
  if (!root) return false;
  // An explicit change list is authoritative. Pulling every unrelated late
  // order into the same L1 repair makes insert/delete/change cease to be local.
  if (!localOrderNamedDemandNames().empty())
    return rootIsLocalOrderNamedSeed(root);
  return rootIsLocalOrderLateSeed(root, origin, windowEnd);
}

bool localResourceIsL2NeighborLink(Resource* resource) {
  if (!resource) return false;
  // Default L2 links only through equipment/mold. Personnel graphs are too
  // dense and collapse local repair into a near-global solve.
  if (envLocalInt("LOCAL_L2_INCLUDE_PERSONNEL", "CP_LOCAL_L2_INCLUDE_PERSONNEL", 0) != 0) return true;
  for (Resource* cursor = resource; cursor; cursor = cursor->getOwner()) {
    const string& cat = cursor->getCategory();
    if (cat == "设备" || cat == "模具") return true;
  }
  return false;
}

bool localRootHasMaterial(OperationPlan* root,
                          const unordered_set<Buffer*>& affected) {
  if (!root) return false;
  vector<OperationPlan*> stack{root};
  while (!stack.empty()) {
    OperationPlan* node = stack.back();
    stack.pop_back();
    for (auto flow = node->beginFlowPlans(); flow != node->endFlowPlans(); ++flow) {
      Buffer* buffer = flow->getBuffer();
      if (buffer && affected.count(buffer)) return true;
    }
    for (OperationPlan::iterator child(node); child != node->end(); ++child)
      stack.push_back(&*child);
  }
  return false;
}

bool localRootHasResource(OperationPlan* root,
                          const unordered_set<Resource*>& affected,
                          const Date& rangeStart, const Date& rangeEnd) {
  if (!root) return false;
  vector<OperationPlan*> stack{root};
  while (!stack.empty()) {
    OperationPlan* node = stack.back();
    stack.pop_back();
    const bool overlaps = node->getStart() < rangeEnd &&
                          node->getEnd() > rangeStart;
    if (overlaps)
      for (auto load = node->beginLoadPlans(); load != node->endLoadPlans();
           ++load) {
        Resource* resource = load->getResource();
        if (!load->isStart() || !resource) continue;
        for (Resource* cursor = resource; cursor; cursor = cursor->getOwner())
          if (affected.count(cursor)) return true;
      }
    for (OperationPlan::iterator child(node); child != node->end(); ++child)
      stack.push_back(&*child);
  }
  return false;
}

bool bufferHasMaterialShortage(Buffer* buffer) {
  if (!buffer) return false;
  for (auto problem = buffer->getProblems(); problem != Problem::end();
       ++problem)
    if (problem->hasType<ProblemMaterialShortage>()) return true;
  for (auto flow = buffer->getFlowPlans().begin();
       flow != buffer->getFlowPlans().end(); ++flow)
    if (flow->getOnhand() < -ROUNDING_ERROR) return true;
  return false;
}

unordered_set<Buffer*> localMaterialProblems() {
  unordered_set<Buffer*> result;
  for (auto buffer = Buffer::begin(); buffer != Buffer::end(); ++buffer) {
    if (!buffer->getItem() || buffer->getItem()->getCategory() != "自制")
      continue;
    if (bufferHasMaterialShortage(&*buffer)) result.insert(&*buffer);
  }
  return result;
}

// Material-change 统一口径 (parallel to resources):
//   declare item / buffer names in CP_LOCAL_CHANGED_MATERIALS, then express
//   the supply change in data (STCK/PO enddate=到货, quantity=数量).
//   Auto fallback: 自制 shortage buffers (legacy material repair).
unordered_set<Buffer*> localChangedMaterialsImpl() {
  unordered_set<Buffer*> result;
  const unordered_set<string> named =
      localNameListFromEnv("LOCAL_CHANGED_MATERIALS").empty()
          ? localNameListFromEnv("CP_LOCAL_CHANGED_MATERIALS")
          : localNameListFromEnv("LOCAL_CHANGED_MATERIALS");
  if (named.empty()) return result;
  for (auto buffer = Buffer::begin(); buffer != Buffer::end(); ++buffer) {
    Buffer* buf = &*buffer;
    if (!buf) continue;
    if (named.count(buf->getName())) {
      result.insert(buf);
      continue;
    }
    Item* item = buf->getItem();
    if (item && named.count(item->getName())) result.insert(buf);
  }
  return result;
}

// Resource-mode trigger = resource *change*, not casual overload. Unified 口径:
//   declare names in CP_LOCAL_CHANGED_RESOURCES (设备 / 人员 / 模具), then
//   express the change as 容量×时段 on maximum_calendar:
//     calendarbucket.value = capacity, startdate/enddate = window
//     (整窗/一天容量→0、白班人数下调 都是同一写法; 不改 resource.maximum)
//   Auto fallback: constrained 设备/模具 with maximum≈0 that still carry
//   proposed/approved loads in the repair window.
unordered_set<Resource*> localChangedResources(const Date& rangeStart,
                                               const Date& rangeEnd) {
  unordered_set<Resource*> result;
  unordered_set<string> named;
  const string raw = envLocalString("LOCAL_CHANGED_RESOURCES", "CP_LOCAL_CHANGED_RESOURCES", "");
  string token;
  for (size_t i = 0; i <= raw.size(); ++i) {
    const char ch = i < raw.size() ? raw[i] : ',';
    if (ch == ',' || ch == ';' || ch == '|' || ch == '\n') {
      // trim
      size_t a = 0, b = token.size();
      while (a < b && isspace(static_cast<unsigned char>(token[a]))) ++a;
      while (b > a && isspace(static_cast<unsigned char>(token[b - 1]))) --b;
      if (a < b) named.insert(token.substr(a, b - a));
      token.clear();
      continue;
    }
    token.push_back(ch);
  }
  for (auto resource = Resource::begin(); resource != Resource::end();
       ++resource) {
    Resource* res = &*resource;
    if (!res) continue;
    if (named.count(res->getName())) {
      result.insert(res);
      continue;
    }
    if (!res->getConstrained()) continue;
    if (res->getMaximum() > ROUNDING_ERROR) continue;
    bool equipOrMold = false;
    for (Resource* cursor = res; cursor; cursor = cursor->getOwner()) {
      const string& cat = cursor->getCategory();
      if (cat == "设备" || cat == "模具") {
        equipOrMold = true;
        break;
      }
    }
    if (!equipOrMold) continue;
    // Only treat max≈0 as a "change" when the live plan still loads it —
    // otherwise every unused zero-cap resource would flood the seed set.
    bool hasLoad = false;
    for (auto load = res->getLoadPlans().begin();
         load != res->getLoadPlans().end() && !hasLoad; ++load) {
      // Resource timeline: quantity>0 marks the load start event.
      OperationPlan* opplan = load->getOperationPlan();
      if (!opplan || load->getQuantity() <= ROUNDING_ERROR) continue;
      if (!opplan->getProposed() && !opplan->getApproved()) continue;
      if (opplan->getStart() < rangeEnd && opplan->getEnd() > rangeStart)
        hasLoad = true;
    }
    if (hasLoad) result.insert(res);
  }
  return result;
}

// Loads of proposed/approved work still sitting on changed resources inside
// the repair window. Zero means the break was fully reassigned away.
size_t countLoadsOnChangedResources(
    const unordered_set<Resource*>& changed, const Date& rangeStart,
    const Date& rangeEnd) {
  if (changed.empty()) return 0;
  size_t left = 0;
  for (Resource* res : changed) {
    if (!res) continue;
    for (auto load = res->getLoadPlans().begin();
         load != res->getLoadPlans().end(); ++load) {
      OperationPlan* opplan = load->getOperationPlan();
      if (!opplan || load->getQuantity() <= ROUNDING_ERROR) continue;
      if (!opplan->getProposed() && !opplan->getApproved()) continue;
      if (opplan->getStart() < rangeEnd && opplan->getEnd() > rangeStart)
        ++left;
    }
  }
  return left;
}

// Recursively include existing proposed/approved producers of selfmade
// buffers consumed by a hot tree. This keeps BOM predecessors movable while
// purchased/stock-only material remains a fixed boundary.
void expandLocalSelfmadePredecessors(
    unordered_set<OperationPlan*>& hot,
    const vector<OperationPlan*>& roots) {
  bool changed = true;
  while (changed) {
    changed = false;
    unordered_set<Buffer*> required;
    for (OperationPlan* root : hot) {
      vector<OperationPlan*> stack{root};
      while (!stack.empty()) {
        OperationPlan* node = stack.back();
        stack.pop_back();
        for (auto flow = node->beginFlowPlans(); flow != node->endFlowPlans();
             ++flow) {
          Buffer* buffer = flow->getBuffer();
          if (flow->getQuantity() < -ROUNDING_ERROR && buffer &&
              buffer->getItem() && buffer->getItem()->getCategory() == "自制")
            required.insert(buffer);
        }
        for (OperationPlan::iterator child(node); child != node->end(); ++child)
          stack.push_back(&*child);
      }
    }
    if (required.empty()) break;
    for (OperationPlan* root : roots) {
      if (!root || hot.count(root) ||
          (!root->getProposed() && !root->getApproved()))
        continue;
      bool produces = false;
      vector<OperationPlan*> stack{root};
      while (!stack.empty() && !produces) {
        OperationPlan* node = stack.back();
        stack.pop_back();
        for (auto flow = node->beginFlowPlans(); flow != node->endFlowPlans();
             ++flow)
          if (flow->getQuantity() > ROUNDING_ERROR && flow->getBuffer() &&
              required.count(flow->getBuffer())) {
            produces = true;
            break;
          }
        for (OperationPlan::iterator child(node); child != node->end(); ++child)
          stack.push_back(&*child);
      }
      if (produces && hot.insert(root).second) changed = true;
    }
  }
}

// A delivery that ships a buffer produced by a movable plan cannot stay
// frozen at its old timestamp. Otherwise the producer moves later and the
// shipment still leaves before the receipt, which is a negative onhand.
size_t unlockDeliveriesOfHotSupply(unordered_set<OperationPlan*>& hot,
                                   const vector<OperationPlan*>& roots) {
  unordered_set<Buffer*> produced;
  for (OperationPlan* root : hot) {
    vector<OperationPlan*> stack{root};
    while (!stack.empty()) {
      OperationPlan* node = stack.back();
      stack.pop_back();
      if (node && node->getOperation() &&
          !node->getOperation()->hasType<OperationDelivery>()) {
        for (auto flow = node->beginFlowPlans(); flow != node->endFlowPlans();
             ++flow)
          if (flow->getQuantity() > ROUNDING_ERROR && flow->getBuffer())
            produced.insert(flow->getBuffer());
      }
      for (OperationPlan::iterator child(node); child != node->end(); ++child)
        stack.push_back(&*child);
    }
  }
  if (produced.empty()) return 0;
  size_t added = 0;
  for (OperationPlan* root : roots) {
    if (!root || hot.count(root)) continue;
    if (!root->getProposed() && !root->getApproved()) continue;
    if (!root->getOperation() ||
        !root->getOperation()->hasType<OperationDelivery>())
      continue;
    bool consumes = false;
    vector<OperationPlan*> stack{root};
    while (!stack.empty() && !consumes) {
      OperationPlan* node = stack.back();
      stack.pop_back();
      for (auto flow = node->beginFlowPlans(); flow != node->endFlowPlans();
           ++flow)
        if (flow->getQuantity() < -ROUNDING_ERROR && flow->getBuffer() &&
            produced.count(flow->getBuffer()))
          consumes = true;
      for (OperationPlan::iterator child(node); child != node->end(); ++child)
        stack.push_back(&*child);
    }
    if (consumes && hot.insert(root).second) ++added;
  }
  return added;
}

bool localRootOverlaps(OperationPlan* root, const Date& start,
                       const Date& end) {
  if (!root) return false;
  vector<OperationPlan*> stack{root};
  while (!stack.empty()) {
    OperationPlan* node = stack.back();
    stack.pop_back();
    if (node->getStart() < end && node->getEnd() > start) return true;
    for (OperationPlan::iterator child(node); child != node->end(); ++child)
      stack.push_back(&*child);
  }
  return false;
}

// Level 2: one ring of in-window roots that share equipment/mold (not
// personnel by default) or a selfmade buffer with L1. One-hop only.
size_t expandLocalDirectNeighbors(unordered_set<OperationPlan*>& hot,
                                  const vector<OperationPlan*>& roots,
                                  const Date& rangeStart,
                                  const Date& rangeEnd) {
  unordered_set<Resource*> resources;
  unordered_set<Buffer*> buffers;
  for (OperationPlan* root : hot) {
    vector<OperationPlan*> stack{root};
    while (!stack.empty()) {
      OperationPlan* node = stack.back();
      stack.pop_back();
      for (auto load = node->beginLoadPlans(); load != node->endLoadPlans();
           ++load) {
        Resource* resource = load->getResource();
        if (!load->isStart() || !resource || !resource->getConstrained())
          continue;
        if (!localResourceIsL2NeighborLink(resource)) continue;
        for (Resource* cursor = resource; cursor; cursor = cursor->getOwner())
          resources.insert(cursor);
      }
      for (auto flow = node->beginFlowPlans(); flow != node->endFlowPlans();
           ++flow) {
        Buffer* buffer = flow->getBuffer();
        if (buffer && buffer->getItem() &&
            buffer->getItem()->getCategory() == "自制")
          buffers.insert(buffer);
      }
      for (OperationPlan::iterator child(node); child != node->end(); ++child)
        stack.push_back(&*child);
    }
  }
  // Rank candidates by overlap with seed resources so a soft size cap can
  // keep the most conflicting neighbors first.
  struct NeighborCand {
    OperationPlan* root = nullptr;
    long score = 0;
  };
  vector<NeighborCand> candidates;
  for (OperationPlan* root : roots) {
    if (!root || hot.count(root) ||
        (!root->getProposed() && !root->getApproved()) ||
        !localRootOverlaps(root, rangeStart, rangeEnd))
      continue;
    bool related = false;
    long score = 0;
    vector<OperationPlan*> stack{root};
    while (!stack.empty()) {
      OperationPlan* node = stack.back();
      stack.pop_back();
      for (auto load = node->beginLoadPlans(); load != node->endLoadPlans();
           ++load) {
        Resource* resource = load->getResource();
        if (!load->isStart() || !resource) continue;
        if (!localResourceIsL2NeighborLink(resource)) continue;
        for (Resource* cursor = resource; cursor; cursor = cursor->getOwner())
          if (resources.count(cursor)) {
            related = true;
            ++score;
            break;
          }
      }
      for (auto flow = node->beginFlowPlans(); flow != node->endFlowPlans();
           ++flow)
        if (flow->getBuffer() && buffers.count(flow->getBuffer())) {
          related = true;
          score += 2;
        }
      for (OperationPlan::iterator child(node); child != node->end(); ++child)
        stack.push_back(&*child);
    }
    if (related) candidates.push_back({root, score});
  }
  sort(candidates.begin(), candidates.end(),
       [](const NeighborCand& a, const NeighborCand& b) {
         if (a.score != b.score) return a.score > b.score;
         return a.root < b.root;
       });
  const size_t l1Size = hot.size();
  const size_t maxFactor =
      static_cast<size_t>(max(1, envLocalInt("LOCAL_L2_MAX_FACTOR", "CP_LOCAL_L2_MAX_FACTOR", 5)));
  const size_t maxAdd = max<size_t>(1, l1Size * maxFactor);
  size_t added = 0;
  for (const NeighborCand& cand : candidates) {
    if (added >= maxAdd) break;
    if (hot.insert(cand.root).second) ++added;
  }
  if (candidates.size() > added)
    logger << "CP local L2: capped neighbors " << added << "/"
           << candidates.size() << " (factor=" << maxFactor
           << " x L1=" << l1Size << ")\n";
  return added;
}

struct LocalScopeMetrics {
  size_t roots = 0;
  size_t leafTasks = 0;
  size_t constrainedLoads = 0;
  size_t estimatedVariables = 0;
};

LocalScopeMetrics localScopeMetrics(
    const unordered_set<OperationPlan*>& scope) {
  LocalScopeMetrics result;
  result.roots = scope.size();
  for (OperationPlan* root : scope) {
    if (!root) continue;
    vector<OperationPlan*> stack{root};
    while (!stack.empty()) {
      OperationPlan* node = stack.back();
      stack.pop_back();
      Operation* operation = node->getOperation();
      if (operation && node->getQuantity() > ROUNDING_ERROR &&
          operation->hasType<OperationTimePer, OperationFixedTime>() &&
          !operation->hasType<OperationItemSupplier,
                              OperationItemDistribution, OperationSplit>() &&
          !operation->hasType<OperationRouting, OperationAlternate>()) {
        ++result.leafTasks;
        for (auto load = node->beginLoadPlans(); load != node->endLoadPlans();
             ++load)
          if (load->isStart() && load->getResource() &&
              load->getResource()->getConstrained())
            ++result.constrainedLoads;
      }
      for (OperationPlan::iterator child(node); child != node->end(); ++child)
        stack.push_back(&*child);
    }
  }
  // Conservative pre-model estimate: selected/start/end/span/presence plus
  // resource-choice and cumulative helper variables.
  result.estimatedVariables =
      result.leafTasks * 6 + result.constrainedLoads * 3;
  return result;
}

bool localScopeFits(const LocalScopeMetrics& metrics, size_t maxRoots,
                    size_t maxTasks, size_t maxVariables) {
  return metrics.roots <= maxRoots && metrics.leafTasks <= maxTasks &&
         metrics.estimatedVariables <= maxVariables;
}

Date demandTreeCompletion(Demand* demand) {
  Date completion = Date::infinitePast;
  if (!demand) return completion;
  Demand::DeliveryIterator deliveries(demand);
  while (OperationPlan* delivery = deliveries.next()) {
    if (!delivery) continue;
    OperationPlan* root = delivery->getTopOwner();
    if (!root) root = delivery;
    vector<OperationPlan*> stack{root};
    while (!stack.empty()) {
      OperationPlan* node = stack.back();
      stack.pop_back();
      if (node->getEnd() != Date::infinitePast &&
          node->getEnd() != Date::infiniteFuture &&
          (completion == Date::infinitePast || node->getEnd() > completion))
        completion = node->getEnd();
      for (OperationPlan::iterator child(node); child != node->end(); ++child)
        stack.push_back(&*child);
    }
  }
  return completion;
}

double demandDeliveredOrPlanned(Demand* demand) {
  if (!demand) return 0.0;
  double delivered = 0.0;
  Demand::DeliveryIterator deliveries(demand);
  while (OperationPlan* delivery = deliveries.next())
    if (delivery) delivered += delivery->getQuantity();
  // Demand::plannedQuantity is imported from the database and isn't reduced
  // when an in-memory CP candidate drops a delivery tree.  The live delivery
  // iterator is authoritative for post-solve validation.
  return delivered;
}

size_t countGlobalOpenOrderIncomplete() {
  size_t incomplete = 0;
  for (auto demand = Demand::begin(); demand != Demand::end(); ++demand) {
    const unsigned short status = demand->getStatus();
    if (!(status & (Demand::STATUS_OPEN | Demand::STATUS_QUOTE))) continue;
    if (demand->getQuantity() > ROUNDING_ERROR &&
        demandDeliveredOrPlanned(&*demand) + ROUNDING_ERROR <
            demand->getQuantity())
      ++incomplete;
  }
  return incomplete;
}

size_t countLocalWindowLateDemands() {
  const Date origin = Plan::instance().getCurrent();
  const int days =
      max(1, envInt("CP_HORIZON_DAYS", envInt("CP_EARLY_DAYS", 3)));
  const Date windowEnd =
      origin + Duration(static_cast<long>(days) * 86400L);
  size_t late = 0;
  for (auto demand = Demand::begin(); demand != Demand::end(); ++demand) {
    const unsigned short status = demand->getStatus();
    if (!(status & (Demand::STATUS_OPEN | Demand::STATUS_QUOTE))) continue;
    const Date due = demand->getDue();
    if (due == Date::infinitePast || due == Date::infiniteFuture) continue;
    if (due < origin || due >= windowEnd) continue;
    const Date completion = demandTreeCompletion(&*demand);
    if (completion != Date::infinitePast && completion > due) ++late;
  }
  return late;
}

// Remember the resource/material-change set used for the latest local freeze
// so the destructor outcome check can re-evaluate the same objects.
static unordered_set<Resource*> g_lastChangedResources;
static unordered_set<Buffer*> g_lastChangedMaterials;
static Date g_lastLocalRangeStart;
static Date g_lastLocalRangeEnd;
// Plan state after the request is loaded and before this repair moves dates.
// Combined success is "did not create a new problem", because a factory plan
// can already carry shortages the local trigger was not asked to erase.
static bool g_haveLocalBaseline = false;
static unordered_set<string> g_baselineIncomplete;
static unordered_set<Buffer*> g_baselineShortageBuffers;
static unordered_set<Resource*> g_baselineOverloadResources;
bool isCombinedLocalMode(const string& mode) {
  return mode == "all" || mode == "combined" || mode == "mixed";
}

size_t countNamedMaterialShortages(const unordered_set<Buffer*>& check) {
  for (Buffer* buf : check)
    if (buf && buf->getDetectProblems()) buf->updateProblems();
  size_t left = 0;
  for (Buffer* buf : check)
    if (bufferHasMaterialShortage(buf)) ++left;
  return left;
}

size_t countChangedResourceOverloads() {
  for (Resource* res : g_lastChangedResources)
    if (res) res->updateProblems();
  size_t overloads = 0;
  for (Resource* res : g_lastChangedResources) {
    if (!res) continue;
    for (auto problem = res->getProblems(); problem != Problem::end();
         ++problem)
      if (problem->hasType<ProblemCapacityOverload>()) ++overloads;
  }
  return overloads;
}

// Purchased infinite buffers keep a running ledger that is allowed to go
// negative. That is not a shortage the local result has to clear.
bool purchasedInfiniteLedger(Buffer* buffer) {
  if (!buffer || !buffer->hasType<BufferInfinite>()) return false;
  if (buffer->getItem() && buffer->getItem()->getCategory() == "自制")
    return false;
  for (const Flow& flow : buffer->getFlows()) {
    if (flow.getQuantity() > ROUNDING_ERROR && flow.getOperation() &&
        flow.getOperation()->hasType<OperationTimePer, OperationFixedTime>())
      return false;
  }
  return true;
}

size_t countGlobalMaterialShortages() {
  size_t shortages = 0;
  for (auto buffer = Buffer::begin(); buffer != Buffer::end(); ++buffer) {
    if (purchasedInfiniteLedger(&*buffer)) continue;
    if (buffer->getDetectProblems()) buffer->updateProblems();
    if (bufferHasMaterialShortage(&*buffer)) {
      ++shortages;
      bool described = false;
      for (auto problem = buffer->getProblems(); problem != Problem::end();
           ++problem)
        if (problem->hasType<ProblemMaterialShortage>()) {
          logger << "Global validation material: "
                 << problem->getDescription() << " dates="
                 << problem->getDates().getStart() << ".."
                 << problem->getDates().getEnd() << "\n";
          described = true;
        }
      if (!described)
        logger << "Global validation material: negative ledger on buffer '"
               << buffer->getName() << "'\n";
    }
  }
  return shortages;
}

size_t countGlobalResourceOverloads() {
  size_t overloads = 0;
  for (auto resource = Resource::begin(); resource != Resource::end();
       ++resource) {
    resource->updateProblems();
    for (auto problem = resource->getProblems(); problem != Problem::end();
         ++problem)
      if (problem->hasType<ProblemCapacityOverload>()) {
        ++overloads;
        logger << "Global validation capacity: " << problem->getDescription()
               << " dates=" << problem->getDates().getStart() << ".."
               << problem->getDates().getEnd() << "\n";
      }
  }
  return overloads;
}

void evaluateOrderLocalOutcomePart(size_t& incomplete, size_t& namedLate,
                                   size_t& windowLate) {
  const unordered_set<string> namedNew = localNewDemandNames();
  windowLate = countLocalWindowLateDemands();
  incomplete = 0;
  namedLate = 0;
  if (namedNew.empty()) return;
  for (const string& name : namedNew) {
    Demand* dem = Demand::find(name);
    if (!dem) {
      ++incomplete;
      continue;
    }
    const double qty = dem->getQuantity();
    if (qty > ROUNDING_ERROR &&
        demandDeliveredOrPlanned(dem) + ROUNDING_ERROR < qty) {
      ++incomplete;
      continue;
    }
    const Date due = dem->getDue();
    const Date completion = demandTreeCompletion(dem);
    if (due != Date::infinitePast && due != Date::infiniteFuture &&
        completion != Date::infinitePast && completion > due)
      ++namedLate;
  }
}

// Lowest-priority bucket effective at this instant. Does not trust bucket
// list order: findBucket stops at the first later start date, so an outage
// bucket inserted after a far-future entry is invisible.
double calendarCapacityAt(Calendar* calendar, Date at, CalendarBucket** bucketOut) {
  if (bucketOut) *bucketOut = nullptr;
  if (!calendar) return 0.0;
  struct tm datedetail;
  at.getInfo(&datedetail);
  const int weekday = datedetail.tm_wday;
  const Duration clock(long(datedetail.tm_sec + datedetail.tm_min * 60 +
                            datedetail.tm_hour * 3600));
  CalendarBucket* best = nullptr;
  int bestPriority = INT_MAX;
  for (auto bucket = calendar->getBuckets();
       bucket != CalendarBucket::iterator::end(); ++bucket) {
    if (at < bucket->getStart() || at >= bucket->getEnd()) continue;
    if (!bucket->isContinuouslyEffective()) {
      if (!(bucket->getDays() & (1 << weekday))) continue;
      if (clock < bucket->getStartTime() || clock >= bucket->getEndTime())
        continue;
    }
    if (bucket->getPriority() < bestPriority) {
      bestPriority = bucket->getPriority();
      best = &*bucket;
    }
  }
  if (bucketOut) *bucketOut = best;
  return best ? best->getValue() : calendar->getDefault();
}

// infinitePast means the interval already fits. Any other date is the next
// start worth trying.
Date capacityRetry(OperationPlan* plan, Date start, Date end) {
  if (!plan || end <= start) return Date::infinitePast;
  for (auto load = plan->beginLoadPlans(); load != plan->endLoadPlans();
       ++load) {
    if (!load->isStart()) continue;
    Resource* resource = load->getResource();
    if (!resource || resource->isGroup() || !resource->getConstrained())
      continue;
    const double required = fabs(load->getQuantity());
    if (required <= ROUNDING_ERROR) continue;
    Calendar* calendar = resource->getMaximumCalendar();
    const double staticMaximum = resource->getMaximum();
    // A job that already starts inside a closed bucket moves to that
    // bucket's end. The event walk below treats one-second calendar
    // edges as ignorable holes and would otherwise walk the whole outage.
    if (calendar && g_lastChangedResources.count(resource)) {
      CalendarBucket* closed = nullptr;
      const double opening = calendarCapacityAt(calendar, start, &closed);
      if (opening + ROUNDING_ERROR < required) {
        if (closed && closed->getEnd() > start) return closed->getEnd();
        Date probe = start + Duration(3600L);
        const Date limit = start + Duration(40L * 86400L);
        while (probe < limit) {
          CalendarBucket* probed = nullptr;
          if (calendarCapacityAt(calendar, probe, &probed) + ROUNDING_ERROR >=
              required)
            return probe;
          if (probed && probed->getEnd() > probe) probe = probed->getEnd();
          else probe += Duration(3600L);
        }
      }
    }
    Date cursor = start;
    for (int guard = 0; guard < 20000 && cursor < end; ++guard) {
      Date next = end;
      if (calendar) {
        Calendar::EventIterator event(calendar, cursor + Duration(1L), true);
        Date previous = cursor;
        for (unsigned int n = 0; n < 10000; ++n) {
          const Date when = event.getDate();
          if (when == Date::infiniteFuture || when <= previous) break;
          if (when > cursor && when < next) next = when;
          if (when > cursor) break;
          previous = when;
          ++event;
        }
      }
      for (auto other = resource->getLoadPlans().begin();
           other != resource->getLoadPlans().end(); ++other) {
        if (other->getEventType() != 1) continue;
        OperationPlan* op = other->getOperationPlan();
        if (!op || op == plan) continue;
        if (op->getStart() > cursor && op->getStart() < next)
          next = op->getStart();
        if (op->getEnd() > cursor && op->getEnd() < next) next = op->getEnd();
      }
      if (next <= cursor) next = cursor + Duration(1L);
      CalendarBucket* bucket = nullptr;
      const double capacity = calendar ? calendarCapacityAt(calendar, cursor, &bucket)
                                       : staticMaximum;
      // Midnight bucket edges are one second of default value. They are not
      // a downtime the whole job has to jump over.
      if (capacity <= ROUNDING_ERROR && next - cursor <= Duration(2L)) {
        cursor = next;
        continue;
      }
      double used = 0.0;
      Date release = Date::infiniteFuture;
      for (auto other = resource->getLoadPlans().begin();
           other != resource->getLoadPlans().end(); ++other) {
        if (other->getEventType() != 1) continue;
        OperationPlan* op = other->getOperationPlan();
        if (!op || op == plan) continue;
        if (op->getStart() <= cursor && op->getEnd() > cursor) {
          used += fabs(other->getQuantity());
          release = min(release, op->getEnd());
        }
      }
      if (used + required <= capacity + ROUNDING_ERROR) {
        cursor = next;
        continue;
      }
      Date retry = next;
      if (capacity <= ROUNDING_ERROR && calendar) {
        // A zero-capacity bucket is a closed interval. Start after it instead
        // of stepping one overlapping job at a time inside the outage.
        if (bucket && bucket->getEnd() > cursor &&
            bucket->getValue() + ROUNDING_ERROR < required)
          retry = bucket->getEnd();
        else {
          Date probe = cursor + Duration(3600L);
          const Date limit = cursor + Duration(40L * 86400L);
          while (probe < limit) {
            CalendarBucket* probed = nullptr;
            const double value = calendarCapacityAt(calendar, probe, &probed);
            if (value + ROUNDING_ERROR >= required) {
              retry = probe;
              break;
            }
            if (probed && probed->getEnd() > probe &&
                probed->getValue() + ROUNDING_ERROR < required)
              probe = probed->getEnd();
            else
              probe += Duration(3600L);
          }
        }
      } else if (release != Date::infiniteFuture)
        retry = min(retry, release);
      if (retry <= cursor) retry = cursor + Duration(1L);
      return retry;
    }
  }
  return Date::infinitePast;
}

bool loadHitsZeroCapacity(Resource* resource, const Date& start, const Date& end);

struct SupplySlip {
  OperationPlan* plan = nullptr;
  Date oldEnd;
  Date newEnd;
};

// Local freeze marks a plan confirmed for this solve only. Downstream of a
// delayed producer has to move with it, including those temporarily confirmed
// plans. Real history and caller-confirmed work stay put.
bool prepareLocalMovable(OperationPlan* plan) {
  if (!plan || !plan->getOperation() || plan->getQuantity() <= ROUNDING_ERROR)
    return false;
  if (plan->getCompleted() || plan->getClosed()) return false;
  if (plan->getBoolProperty("local_history_frozen", false)) return false;
  if (plan->getConfirmed()) {
    const bool frozenHere = plan->getBoolProperty("local_frozen", false) ||
                            plan->getBoolProperty("cp_local_frozen", false);
    if (!frozenHere) return false;
    plan->setStatusRaw("proposed");
  }
  return plan->getProposed() || plan->getApproved();
}

void collectProducedBuffers(OperationPlan* plan,
                            unordered_set<Buffer*>& produced) {
  for (OperationPlan* cursor = plan; cursor; cursor = cursor->getOwner()) {
    for (auto flow = cursor->beginFlowPlans(); flow != cursor->endFlowPlans();
         ++flow)
      if (flow->getQuantity() > ROUNDING_ERROR && flow->getBuffer())
        produced.insert(flow->getBuffer());
  }
}

// Keep the gap between a delayed producer and the plans that were waiting on
// its receipt. Shifting only the machine load would start those consumers
// before the new receipt and fail the material gate.
size_t pushConsumersAfterSlips(vector<SupplySlip> slips) {
  if (slips.empty()) return 0;
  unordered_set<OperationPlan*> anchored;
  for (const SupplySlip& slip : slips)
    if (slip.plan) anchored.insert(slip.plan);
  size_t pushed = 0;
  for (int pass = 0; pass < 8 && !slips.empty(); ++pass) {
    vector<SupplySlip> extra;
    for (const SupplySlip& slip : slips) {
      if (!slip.plan) continue;
      const Duration delta = slip.newEnd - slip.oldEnd;
      if (delta <= Duration(0L)) continue;
      unordered_set<Buffer*> produced;
      collectProducedBuffers(slip.plan, produced);
      if (produced.empty()) continue;
      for (auto candidate = OperationPlan::begin();
           candidate != OperationPlan::end(); ++candidate) {
        OperationPlan* consumer = &*candidate;
        if (!consumer || anchored.count(consumer)) continue;
        if (consumer->getStart() < slip.oldEnd) continue;
        if (!prepareLocalMovable(consumer)) continue;
        bool uses = false;
        for (auto flow = consumer->beginFlowPlans();
             flow != consumer->endFlowPlans(); ++flow) {
          if (flow->getQuantity() < -ROUNDING_ERROR && flow->getBuffer() &&
              produced.count(flow->getBuffer())) {
            uses = true;
            break;
          }
        }
        if (!uses) continue;
        const Date oldStart = consumer->getStart();
        const Date oldEnd = consumer->getEnd();
        const Date ask = oldStart + delta;
        if (!ask || ask <= oldStart) continue;
        const Duration consumerDur =
            oldEnd > oldStart ? oldEnd - oldStart : Duration(0L);
        consumer->setStartAndEnd(ask, ask + consumerDur);
        if (consumer->getStart() == oldStart && consumer->getEnd() == oldEnd)
          continue;
        anchored.insert(consumer);
        extra.push_back({consumer, oldEnd, consumer->getEnd()});
        ++pushed;
      }
    }
    slips.swap(extra);
  }
  if (pushed)
    logger << "Local delay: pushed " << pushed
           << " downstream plan(s) to keep material after the moved producer\n"
           << flush;
  return pushed;
}

size_t delayChangedResourcePlansImpl() {
  if (g_lastChangedResources.empty()) return 0;
  for (Resource* resource : g_lastChangedResources) {
    if (!resource) continue;
    Calendar* calendar = resource->getMaximumCalendar();
    logger << "Local delay resource " << resource->getName()
           << " maximum=" << resource->getMaximum() << " calendar="
           << (calendar ? calendar->getName() : "none");
    if (calendar) {
      logger << " default=" << calendar->getDefault();
      int buckets = 0;
      for (auto bucket = calendar->getBuckets();
           bucket != CalendarBucket::iterator::end(); ++bucket) {
        ++buckets;
        logger << " | prio=" << bucket->getPriority()
               << " value=" << bucket->getValue()
               << " " << bucket->getStart() << ".." << bucket->getEnd()
               << " days=" << bucket->getDays()
               << " time=" << bucket->getStartTime() << ".."
               << bucket->getEndTime();
      }
      logger << " buckets=" << buckets;
    }
    logger << "\n" << flush;
  }
  struct Row {
    OperationPlan* plan = nullptr;
    Resource* resource = nullptr;
    Date start;
    string reference;
  };
  vector<Row> rows;
  unordered_set<OperationPlan*> seen;
  for (Resource* resource : g_lastChangedResources) {
    if (!resource) continue;
    for (auto load = resource->getLoadPlans().begin();
         load != resource->getLoadPlans().end(); ++load) {
      if (load->getEventType() != 1) continue;
      OperationPlan* plan = load->getOperationPlan();
      if (!plan || !plan->getOperation() || seen.count(plan)) continue;
      if (plan->getConfirmed() || plan->getCompleted() || plan->getClosed())
        continue;
      if (!plan->getProposed() && !plan->getApproved()) continue;
      seen.insert(plan);
      rows.push_back(
          {plan, resource, plan->getStart(), plan->getReference()});
    }
  }
  sort(rows.begin(), rows.end(), [](const Row& left, const Row& right) {
    if (left.start != right.start) return left.start < right.start;
    return left.reference < right.reference;
  });
  const Date horizon =
      Plan::instance().getCurrent() + Duration(366L * 86400L);
  size_t moved = 0;
  vector<SupplySlip> slips;
  for (const Row& row : rows) {
    OperationPlan* plan = row.plan;
    const Date originalStart = row.start;
    const Date originalEnd = plan->getEnd();
    const bool inOutage =
        loadHitsZeroCapacity(row.resource, originalStart, originalEnd);
    const Date retry0 = capacityRetry(plan, originalStart, originalEnd);
    if (!inOutage && retry0 == Date::infinitePast) continue;
    // A zero-capacity bucket is a closed interval. Start after it. Stepping
    // one second at a time never leaves a multi-hour stop inside the attempt
    // budget, which left the other loads sitting in the outage.
    Date ask = originalStart;
    if (inOutage) {
      CalendarBucket* closed = nullptr;
      if (row.resource && row.resource->getMaximumCalendar())
        calendarCapacityAt(row.resource->getMaximumCalendar(), originalStart,
                           &closed);
      if (closed && closed->getEnd() > originalStart)
        ask = closed->getEnd();
      else if (retry0 != Date::infinitePast && retry0 > originalStart)
        ask = retry0;
      else
        ask = originalEnd > originalStart ? originalEnd
                                          : originalStart + Duration(3600L);
    }
    if (!ask || ask < Plan::instance().getCurrent())
      ask = Plan::instance().getCurrent();
    const Duration dur = originalEnd > originalStart
                             ? originalEnd - originalStart
                             : Duration(3600L);
    bool placed = false;
    for (int attempt = 0; attempt < 500 && ask < horizon; ++attempt) {
      // setOperationPlanParameters searches backward and lands inside the
      // stop again. Pin the dates, then step past whatever still overlaps
      // the zero-capacity bucket.
      plan->setStartAndEnd(ask, ask + dur);
      const Date start = plan->getStart();
      const Date end = plan->getEnd();
      if (!start || !end || end <= start) break;
      if (!loadHitsZeroCapacity(row.resource, start, end)) {
        placed = true;
        break;
      }
      CalendarBucket* closed = nullptr;
      if (row.resource && row.resource->getMaximumCalendar())
        calendarCapacityAt(row.resource->getMaximumCalendar(), start, &closed);
      if (closed && closed->getEnd() > ask)
        ask = closed->getEnd();
      else
        ask = end > ask ? end : ask + Duration(3600L);
    }
    if (!placed) {
      plan->setOperationPlanParameters(plan->getQuantity(), originalStart,
                                       Date::infinitePast, false, true, false);
      continue;
    }
    if (plan->getStart() != originalStart || plan->getEnd() != originalEnd) {
      ++moved;
      slips.push_back({plan, originalEnd, plan->getEnd()});
    }
  }
  if (moved)
    logger << "Local delay: moved " << moved
           << " proposed operationplan(s) to the next open capacity on "
              "changed resources\n"
           << flush;
  return moved + pushConsumersAfterSlips(slips);
}

bool loadHitsZeroCapacity(Resource* resource, const Date& start,
                          const Date& end) {
  if (!resource || !start || !end || end <= start) return false;
  Calendar* calendar = resource->getMaximumCalendar();
  if (!calendar) return resource->getMaximum() <= ROUNDING_ERROR;
  Date cursor = start;
  for (int guard = 0; guard < 10000 && cursor < end; ++guard) {
    CalendarBucket* bucket = nullptr;
    const double capacity = calendarCapacityAt(calendar, cursor, &bucket);
    Date next = end;
    if (bucket && bucket->getEnd() > cursor && bucket->getEnd() < next)
      next = bucket->getEnd();
    if (next <= cursor) next = cursor + Duration(1L);
    // A one-second midnight edge is the calendar's default, not the stop.
    if (capacity <= ROUNDING_ERROR && next - cursor > Duration(2L))
      return true;
    cursor = next;
  }
  return false;
}

size_t countLoadsInChangedOutage() {
  if (g_lastChangedResources.empty()) return 0;
  size_t left = 0;
  unordered_set<OperationPlan*> seen;
  for (Resource* resource : g_lastChangedResources) {
    if (!resource) continue;
    for (auto load = resource->getLoadPlans().begin();
         load != resource->getLoadPlans().end(); ++load) {
      if (load->getEventType() != 1) continue;
      OperationPlan* plan = load->getOperationPlan();
      if (!plan || seen.count(plan)) continue;
      if (plan->getCompleted() || plan->getClosed() || plan->getConfirmed())
        continue;
      if (plan->getBoolProperty("local_history_frozen", false)) continue;
      if (!plan->getProposed() && !plan->getApproved()) continue;
      if (!loadHitsZeroCapacity(resource, plan->getStart(), plan->getEnd()))
        continue;
      seen.insert(plan);
      ++left;
      logger << "Changed resource outage still holds " << plan->getReference()
             << " on " << resource->getName() << " " << plan->getStart()
             << ".." << plan->getEnd() << "\n";
    }
  }
  return left;
}

void snapshotLocalBaseline() {
  g_baselineIncomplete.clear();
  g_baselineShortageBuffers.clear();
  g_baselineOverloadResources.clear();
  for (auto demand = Demand::begin(); demand != Demand::end(); ++demand) {
    const unsigned short status = demand->getStatus();
    if (!(status & (Demand::STATUS_OPEN | Demand::STATUS_QUOTE))) continue;
    if (demand->getQuantity() > ROUNDING_ERROR &&
        demandDeliveredOrPlanned(&*demand) + ROUNDING_ERROR <
            demand->getQuantity())
      g_baselineIncomplete.insert(demand->getName());
  }
  for (auto buffer = Buffer::begin(); buffer != Buffer::end(); ++buffer) {
    if (purchasedInfiniteLedger(&*buffer)) continue;
    if (buffer->getDetectProblems()) buffer->updateProblems();
    if (bufferHasMaterialShortage(&*buffer))
      g_baselineShortageBuffers.insert(&*buffer);
  }
  for (auto resource = Resource::begin(); resource != Resource::end();
       ++resource) {
    resource->updateProblems();
    for (auto problem = resource->getProblems(); problem != Problem::end();
         ++problem)
      if (problem->hasType<ProblemCapacityOverload>()) {
        g_baselineOverloadResources.insert(&*resource);
        break;
      }
  }
  g_haveLocalBaseline = true;
  logger << "Local baseline: incomplete=" << g_baselineIncomplete.size()
         << " material_shortages=" << g_baselineShortageBuffers.size()
         << " overloaded_resources=" << g_baselineOverloadResources.size()
         << "\n"
         << flush;
}

size_t countNewIncompleteOrders() {
  size_t incomplete = 0;
  for (auto demand = Demand::begin(); demand != Demand::end(); ++demand) {
    const unsigned short status = demand->getStatus();
    if (!(status & (Demand::STATUS_OPEN | Demand::STATUS_QUOTE))) continue;
    if (demand->getQuantity() <= ROUNDING_ERROR) continue;
    if (demandDeliveredOrPlanned(&*demand) + ROUNDING_ERROR >=
        demand->getQuantity())
      continue;
    if (g_baselineIncomplete.count(demand->getName())) continue;
    ++incomplete;
    logger << "New incomplete order '" << demand->getName() << "'\n";
  }
  return incomplete;
}

size_t countNewMaterialShortages() {
  size_t shortages = 0;
  for (auto buffer = Buffer::begin(); buffer != Buffer::end(); ++buffer) {
    if (purchasedInfiniteLedger(&*buffer)) continue;
    if (g_baselineShortageBuffers.count(&*buffer)) continue;
    if (g_lastChangedMaterials.count(&*buffer)) continue;
    if (buffer->getDetectProblems()) buffer->updateProblems();
    if (!bufferHasMaterialShortage(&*buffer)) continue;
    ++shortages;
    logger << "New material shortage on '" << buffer->getName() << "'\n";
  }
  return shortages;
}

size_t countNewResourceOverloads() {
  size_t overloads = 0;
  for (auto resource = Resource::begin(); resource != Resource::end();
       ++resource) {
    if (g_baselineOverloadResources.count(&*resource)) continue;
    resource->updateProblems();
    bool described = false;
    for (auto problem = resource->getProblems(); problem != Problem::end();
         ++problem)
      if (problem->hasType<ProblemCapacityOverload>()) {
        if (!described) {
          logger << "New capacity overload on '" << resource->getName()
                 << "'\n";
          described = true;
        }
        ++overloads;
      }
  }
  return overloads;
}

// Material no-root completion, material seeds outside the CP horizon, and
// the combined baseline/outage gate are FAS repair policy. CP, GA, and MRP
// call this same scope helper and must keep the previous rules.
bool fasLocalPolicy() {
  const char* raw = getenv("LOCAL_SOLVER");
  return raw && std::string(raw) == "fas";
}

bool mrpLocalPolicy() {
  const char* raw = getenv("LOCAL_SOLVER");
  return raw && std::string(raw) == "mrp";
}

void evaluateLocalRescheduleOutcome(const string& mode) {
  g_lastLocalOk = 0;
  const bool combined = isCombinedLocalMode(mode);
  const bool hasOrderTrigger =
      !localNewDemandNames().empty() || !localDeletedDemandNames().empty();
  const bool hasMaterialTrigger = !g_lastChangedMaterials.empty();
  const bool hasResourceTrigger = !g_lastChangedResources.empty();
  // Material-change may export zero placements when the only feasible repair
  // is to drop the short lot. Combined allows that only when material is the
  // sole active family (no order/resource trigger).
  const bool allowZeroApplied =
      mode == "material" ||
      (combined && hasMaterialTrigger && !hasOrderTrigger &&
       !hasResourceTrigger);
  if (g_lastLocalApplied == 0 && !allowZeroApplied) {
    logger << "CP local outcome: fail (no exported assignments) type=" << mode
           << "\n"
           << flush;
    return;
  }
  if (combined) {
    size_t orderIncomplete = 0;
    size_t namedLate = 0;
    size_t windowLate = 0;
    size_t materialLeft = 0;
    size_t overloads = 0;
    size_t loadsOnChanged = 0;
    bool ok = true;
    if (hasOrderTrigger) {
      evaluateOrderLocalOutcomePart(orderIncomplete, namedLate, windowLate);
    } else {
      windowLate = countLocalWindowLateDemands();
    }
    // FAS compares the exported plan with the loaded one and requires a
    // zero-capacity window to contain no proposed load. Other solvers keep
    // the global shortage and overload gate.
    size_t namedMaterial = 0;
    size_t outageLoads = 0;
    if (fasLocalPolicy() || mrpLocalPolicy()) {
      const size_t newlyIncomplete = countNewIncompleteOrders();
      orderIncomplete = max(orderIncomplete, newlyIncomplete);
      if (hasMaterialTrigger)
        namedMaterial = countNamedMaterialShortages(g_lastChangedMaterials);
      // FAS also rejects a shortage the loaded plan did not already have.
      // MRP keeps that report, but a timing gap on some other buffer must
      // not roll back a repair whose named order and stopped resource are
      // feasible.
      const size_t newMaterial =
          fasLocalPolicy() ? countNewMaterialShortages() : 0;
      if (mrpLocalPolicy() && countNewMaterialShortages())
        logger << "MRP local: other buffers still show a shortage after the "
                  "repair\n";
      materialLeft = namedMaterial + newMaterial;
      overloads = countNewResourceOverloads();
      if (hasResourceTrigger) {
        loadsOnChanged = countLoadsOnChangedResources(
            g_lastChangedResources, g_lastLocalRangeStart, g_lastLocalRangeEnd);
        outageLoads = countLoadsInChangedOutage();
      }
    } else {
      orderIncomplete = max(orderIncomplete, countGlobalOpenOrderIncomplete());
      materialLeft = countGlobalMaterialShortages();
      overloads = countGlobalResourceOverloads();
      if (hasResourceTrigger) {
        loadsOnChanged = countLoadsOnChangedResources(
            g_lastChangedResources, g_lastLocalRangeStart, g_lastLocalRangeEnd);
      }
    }
    if (orderIncomplete) ok = false;
    if (materialLeft) ok = false;
    if (overloads) ok = false;
    if (outageLoads) ok = false;
    g_lastIncomplete = orderIncomplete + materialLeft + overloads + outageLoads;
    g_lastNamedLate = namedLate;
    g_lastWindowLate = windowLate;
    g_lastLocalOk = ok ? 1 : 0;
    logger << "CP local outcome: type=all applied=" << g_lastLocalApplied
           << " order_incomplete=" << orderIncomplete
           << " material_shortages=" << materialLeft
           << " named_material_shortages=" << namedMaterial
           << " capacity_overloads=" << overloads
           << " outage_loads=" << outageLoads
           << " loads_on_changed=" << loadsOnChanged
           << " named_late=" << namedLate
           << " window_late=" << windowLate
           << (g_lastLocalOk ? " ok\n" : " unresolved\n")
           << flush;
    return;
  }
  if (mode == "material") {
    // Named supply-change: only those buffers must clear shortage.
    // Auto path: all 自制 shortage buffers (legacy).
    unordered_set<Buffer*> check = g_lastChangedMaterials;
    if (check.empty()) {
      for (auto buffer = Buffer::begin(); buffer != Buffer::end(); ++buffer)
        if (buffer->getItem() && buffer->getItem()->getCategory() == "自制" &&
            buffer->getDetectProblems())
          buffer->updateProblems();
      check = localMaterialProblems();
      g_lastIncomplete = check.size();
      g_lastNamedLate = 0;
      g_lastWindowLate = 0;
      g_lastLocalOk = check.empty() ? 1 : 0;
      logger << "CP local outcome: type=material applied=" << g_lastLocalApplied
             << " shortages_left=" << check.size()
             << (g_lastLocalOk ? " ok\n" : " unresolved\n")
             << flush;
      return;
    }
    const size_t left = countNamedMaterialShortages(check);
    g_lastIncomplete = left;
    g_lastNamedLate = 0;
    g_lastWindowLate = 0;
    g_lastLocalOk = left == 0 ? 1 : 0;
    logger << "CP local outcome: type=material applied=" << g_lastLocalApplied
           << " changed_materials=" << check.size()
           << " shortages_left=" << left
           << (g_lastLocalOk ? " ok\n" : " unresolved\n")
           << flush;
    return;
  }
  if (mode == "resource") {
    // Unified success: no capacity overload on the changed resources
    // (容量×时段 via maximum_calendar; max=0 scalar is legacy fallback).
    // "loads_left==0" is wrong for personnel / timed downtime — work may
    // remain on the resource outside the outage or at reduced headcount.
    const size_t overloads = countChangedResourceOverloads();
    const size_t left = countLoadsOnChangedResources(
        g_lastChangedResources, g_lastLocalRangeStart, g_lastLocalRangeEnd);
    g_lastIncomplete = overloads;
    g_lastNamedLate = 0;
    g_lastWindowLate = 0;
    g_lastLocalOk = overloads == 0 ? 1 : 0;
    logger << "CP local outcome: type=resource applied=" << g_lastLocalApplied
           << " changed_resources=" << g_lastChangedResources.size()
           << " capacity_overloads=" << overloads
           << " loads_on_changed=" << left
           << (g_lastLocalOk ? " ok\n" : " unresolved\n")
           << flush;
    return;
  }
  // Order-change (增/删/改共用): named trigger filled (quantity met).
  // Capacity is enforced inside CP (设备/人员/模具 Cumulative + NoOverlap).
  // Due lateness is NOT a local_ok failure — CP already optimizes tardiness;
  // a feasible placement that meets qty clears the trigger even if late.
  size_t incomplete = 0;
  size_t namedLate = 0;
  size_t windowLate = 0;
  evaluateOrderLocalOutcomePart(incomplete, namedLate, windowLate);
  if (!localNewDemandNames().empty()) {
    g_lastIncomplete = incomplete;
    g_lastNamedLate = namedLate;
    g_lastWindowLate = windowLate;
    g_lastLocalOk = incomplete == 0 ? 1 : 0;
    logger << "CP local outcome: type=order applied=" << g_lastLocalApplied
           << " named=" << localNewDemandNames().size()
           << " incomplete=" << incomplete
           << " named_late=" << namedLate
           << " window_late=" << windowLate
           << (g_lastLocalOk ? " ok\n" : " unresolved\n")
           << flush;
    return;
  }
  // Pure delete / residual-only: no named qty gate. Window lateness is
  // diagnostic only (same policy as named insert/change — due is not a
  // local_ok failure). applied==0 already failed above.
  g_lastIncomplete = 0;
  g_lastNamedLate = 0;
  g_lastWindowLate = windowLate;
  g_lastLocalOk = 1;
  logger << "CP local outcome: type=order applied=" << g_lastLocalApplied
         << " window_late=" << windowLate << " ok (delete/residual)\n"
         << flush;
}



}  // namespace

size_t localDelayChangedResourcePlans() {
  return delayChangedResourcePlansImpl();
}

bool localRescheduleEnabled() {
  return envLocalInt("LOCAL_RESCHEDULE", "CP_LOCAL_RESCHEDULE", 0) != 0;
}

short planConstraintsFromEnv() {
  // Same bitmask as runplan.parseConstraints / SolverCreate flags.
  const short defaults = SolverCreate::CAPACITY | SolverCreate::MFG_LEADTIME |
                         SolverCreate::PO_LEADTIME;
  const char* raw = getenv("CCAPPS_CONSTRAINT");
  if (!raw || !*raw) return defaults;
  try {
    // Numeric form ("52") or empty handled below.
    size_t idx = 0;
    const long parsed = std::stol(raw, &idx, 10);
    if (idx == std::strlen(raw))
      return static_cast<short>(parsed);
  } catch (...) {
  }
  short bits = 0;
  std::string token;
  for (const char* p = raw;; ++p) {
    if (*p && *p != ',') {
      token.push_back(static_cast<char>(std::tolower(*p)));
      continue;
    }
    if (token == "capa")
      bits |= SolverCreate::CAPACITY;
    else if (token == "fence")
      bits |= SolverCreate::FENCE;
    else if (token == "lt")
      bits |= SolverCreate::MFG_LEADTIME | SolverCreate::PO_LEADTIME;
    else if (token == "mfg_lt")
      bits |= SolverCreate::MFG_LEADTIME;
    else if (token == "po_lt")
      bits |= SolverCreate::PO_LEADTIME;
    token.clear();
    if (!*p) break;
  }
  return bits;
}

short planTypeFromEnv() {
  const int value = envInt("CCAPPS_PLANTYPE", 1);
  if (value < 1 || value > 3) return 1;
  return static_cast<short>(value);
}

bool planEnforceCapacity() {
  return planTypeFromEnv() == 1 &&
         (planConstraintsFromEnv() & SolverCreate::CAPACITY) != 0;
}

bool planEnforceMfgLeadTime() {
  return planTypeFromEnv() == 1 &&
         (planConstraintsFromEnv() & SolverCreate::MFG_LEADTIME) != 0;
}

bool planEnforcePoLeadTime() {
  return planTypeFromEnv() == 1 &&
         (planConstraintsFromEnv() & SolverCreate::PO_LEADTIME) != 0;
}

bool planEnforceFence() {
  return planTypeFromEnv() == 1 &&
         (planConstraintsFromEnv() & SolverCreate::FENCE) != 0;
}

void applyPlanConstraintsToCreate(SolverCreate& solver) {
  const short constraints = planConstraintsFromEnv();
  const short plantype = planTypeFromEnv();
  solver.setConstraints(constraints);
  solver.setPlanType(plantype);
  logger << "Plan constraints applied (MRP/GA): plantype=" << plantype
         << " bits=" << constraints
         << " capa=" << (planEnforceCapacity() ? 1 : 0)
         << " fence=" << (planEnforceFence() ? 1 : 0)
         << " mfg_lt=" << (planEnforceMfgLeadTime() ? 1 : 0)
         << " po_lt=" << (planEnforcePoLeadTime() ? 1 : 0) << "\n"
         << flush;
}

void applyPlanConstraintsToFas(SolverFAS& solver) {
  // capa and po_lt follow the request. Manufacturing always starts at
  // currentdate, so the mfg_lt bit stays on whether or not it was passed.
  const short constraints =
      planConstraintsFromEnv() | SolverCreate::MFG_LEADTIME;
  const short plantype = planTypeFromEnv();
  solver.setConstraints(constraints);
  solver.setPlanType(plantype);
  logger << "Plan constraints applied (FAS): plantype=" << plantype
         << " bits=" << constraints
         << " capa=" << (planEnforceCapacity() ? 1 : 0)
         << " fence=" << (planEnforceFence() ? 1 : 0)
         << " mfg_lt=1"
         << " po_lt=" << (planEnforcePoLeadTime() ? 1 : 0) << "\n"
         << flush;
}

void localSetLastApplied(size_t n) { g_lastLocalApplied = n; }
size_t localLastApplied() { return g_lastLocalApplied; }
void localSetLastOk(int ok) { g_lastLocalOk = ok; }
int localLastOk() { return g_lastLocalOk; }
void localSetStage(const std::string& stage) { g_lastLocalStage = stage; }
std::string localLastStage() { return g_lastLocalStage; }
void localSetPreflightConflicts(const std::string& text) {
  g_lastPreflightConflicts = text;
}
std::string localLastPreflightConflicts() { return g_lastPreflightConflicts; }

LocalScopeGuard::LocalScopeGuard() = default;

std::unordered_set<Buffer*> localChangedMaterials() {
  return localChangedMaterialsImpl();
}

std::unordered_set<std::string> localNewDemands() {
  return localNewDemandNames();
}

std::unordered_set<std::string> localSupplyExpansionNames() {
  std::unordered_set<std::string> allow;
  if (!localRescheduleEnabled()) return allow;
  std::string mode = envLocalString(
      "LOCAL_RESCHEDULE_TYPE", "CP_LOCAL_RESCHEDULE_TYPE", "order");
  std::transform(mode.begin(), mode.end(), mode.begin(), [](unsigned char ch) {
    return static_cast<char>(std::tolower(ch));
  });
  if (mode == "combined" || mode == "mixed") mode = "all";
  // Resource and material repairs move the existing plan. They do not open
  // ordinary orders or safety stock. Order delete and priority-only changes
  // are the same: a name in both lists whose quantity did not grow.
  if (mode != "order" && mode != "all") return allow;
  const auto created = localNewDemandNames();
  const auto removed = localDeletedDemandNames();
  for (const std::string& name : created) {
    Demand* demand = Demand::find(name);
    if (!demand) continue;
    if (!removed.count(name)) {
      allow.insert(name);
      continue;
    }
    if (demand->getQuantity() >
        demand->getPlannedQuantity() + ROUNDING_ERROR)
      allow.insert(name);
  }
  return allow;
}

PyObject* local_last_applied(PyObject*, PyObject*) {
  return PyLong_FromSize_t(g_lastLocalApplied);
}
PyObject* local_last_ok(PyObject*, PyObject*) {
  return PyBool_FromLong(g_lastLocalOk ? 1 : 0);
}
PyObject* local_last_stage(PyObject*, PyObject*) {
  return PyUnicode_FromString(g_lastLocalStage.c_str());
}
PyObject* local_last_outcome(PyObject*, PyObject*) {
  return Py_BuildValue(
      "{s:i,s:O,s:n,s:n,s:n,s:n,s:s,s:s,s:O,s:s}",
      "level_used", g_lastLocalLevel,
      "local_ok", g_lastLocalOk ? Py_True : Py_False,
      "applied", static_cast<Py_ssize_t>(g_lastLocalApplied),
      "incomplete", static_cast<Py_ssize_t>(g_lastIncomplete),
      "named_late", static_cast<Py_ssize_t>(g_lastNamedLate),
      "window_late", static_cast<Py_ssize_t>(g_lastWindowLate),
      "stage", g_lastLocalStage.c_str(),
      "mode", g_lastLocalMode.c_str(),
      "unlock_in_window", g_lastUnlockInWindow ? Py_True : Py_False,
      "preflight_conflicts", g_lastPreflightConflicts.c_str());
}
PyObject* local_last_preflight_conflicts(PyObject*, PyObject*) {
  return PyUnicode_FromString(g_lastPreflightConflicts.c_str());
}
PyObject* local_mark_candidate_no_supply(PyObject*, PyObject*) {
  g_lastLocalApplied = 0;
  g_lastLocalOk = 0;
  g_lastLocalStage = "candidate_no_supply";
  g_lastLocalLevel = 0;
  g_lastIncomplete = 0;
  g_lastNamedLate = 0;
  g_lastWindowLate = 0;
  g_lastPreflightConflicts.clear();
  Py_RETURN_NONE;
}

LocalScopeGuard::~LocalScopeGuard() {
  for (auto& entry : frozen) {
    OperationPlan* plan = entry.first;
    if (!plan) continue;
    try {
      // Freeze used setStatusRaw as well. Restore raw to avoid triggering
      // status-transition side effects after CP has already written dates.
      plan->setStatusRaw(entry.second.empty() ? "proposed" : entry.second);
    } catch (...) {
    }
    try {
      plan->deleteProperty("local_history_frozen");
      plan->deleteProperty("local_frozen");
      plan->deleteProperty("cp_local_frozen");
    } catch (...) {
    }
  }
  frozen.clear();
  // L1 lock-all / empty-seed skips leave dates untouched; do not mark fail.
  if (active && !skipOptimization) evaluateLocalRescheduleOutcome(mode);
}

void applyLocalRescheduleScope(LocalScopeGuard& guard) {
  if (!localRescheduleEnabled()) return;
  if (envInt("CP_PARTITION_GROUP", 0) > 0) return;
  const Date origin = Plan::instance().getCurrent();
  const int days =
      max(1, envInt("CP_HORIZON_DAYS", envInt("CP_EARLY_DAYS", 3)));
  const Date rangeStart = origin;
  const Date rangeEnd = origin + Duration(static_cast<long>(days) * 86400L);
  const string levelSetting = envLocalString("LOCAL_RESCHEDULE_LEVEL", "CP_LOCAL_RESCHEDULE_LEVEL", "auto");
  int explicitLevel = 0;
  auto relaxHas = [](const string& relaxCsv, const char* token) -> bool {
    if (!token || !*token || relaxCsv.empty()) return false;
    string cur;
    for (size_t i = 0; i <= relaxCsv.size(); ++i) {
      const char ch = i < relaxCsv.size() ? relaxCsv[i] : ',';
      if (ch == ',' || ch == ';' || ch == '|' || ch == ' ') {
        if (!cur.empty()) {
          for (char& c : cur) c = static_cast<char>(tolower(static_cast<unsigned char>(c)));
          if (cur == token) return true;
          cur.clear();
        }
      } else {
        cur.push_back(ch);
      }
    }
    return false;
  };
  // custom + relax=window: keep L1/L2 outside policy, but also put in-window
  // non-seeds into hot (do not freeze them). relax=all is true L3.
  bool unlockInWindowNonSeeds = false;
  if (levelSetting == "custom") {
    // Customer-defined freeze: start from scope_base, then apply 松绑 tokens.
    //   neighbors / personnel → at least L2 (+ personnel neighbor links)
    //   window               → unlock in-window non-seeds (not full L3)
    //   all / global / l3    → L3 global (no freeze)
    explicitLevel = max(1, min(3, envLocalInt("LOCAL_SCOPE_BASE", "CP_LOCAL_SCOPE_BASE", 1)));
    const string relax = envLocalString("LOCAL_SCOPE_RELAX", "CP_LOCAL_SCOPE_RELAX", "");
    if (relaxHas(relax, "all") || relaxHas(relax, "global") ||
        relaxHas(relax, "l3")) {
      explicitLevel = 3;
    } else {
      if (relaxHas(relax, "neighbors") || relaxHas(relax, "neighbor") ||
          relaxHas(relax, "personnel") || relaxHas(relax, "person") ||
          relaxHas(relax, "l2")) {
        explicitLevel = max(explicitLevel, 2);
      }
      if (relaxHas(relax, "window") || relaxHas(relax, "in_window") ||
          relaxHas(relax, "in-window")) {
        unlockInWindowNonSeeds = true;
      }
    }
    if (relaxHas(relax, "personnel") || relaxHas(relax, "person")) {
      // Prefer explicit env if already set; otherwise enable for this solve.
      if (getenv("CP_LOCAL_L2_INCLUDE_PERSONNEL") == nullptr)
        setenv("CP_LOCAL_L2_INCLUDE_PERSONNEL", "1", 0);
    }
    logger << "CP local custom scope: base="
           << envLocalInt("LOCAL_SCOPE_BASE", "CP_LOCAL_SCOPE_BASE", 1) << " relax='" << relax
           << "' -> level=" << explicitLevel
           << (unlockInWindowNonSeeds ? " unlock_in_window=1" : "")
           << "\n"
           << flush;
  } else if (levelSetting != "auto")
    try {
      explicitLevel = max(1, min(3, stoi(levelSetting)));
    } catch (...) {
      explicitLevel = 0;
    }
  string mode = envLocalString("LOCAL_RESCHEDULE_TYPE", "CP_LOCAL_RESCHEDULE_TYPE", "order");
  transform(mode.begin(), mode.end(), mode.begin(),
            [](unsigned char ch) { return static_cast<char>(tolower(ch)); });
  if (mode != "order" && mode != "material" && mode != "resource" &&
      !isCombinedLocalMode(mode)) {
    logger << "CP local reschedule: unsupported type='" << mode
           << "'; falling back to order\n";
    mode = "order";
  }
  if (mode == "combined" || mode == "mixed") mode = "all";
  const bool combined = isCombinedLocalMode(mode);
  guard.active = true;
  guard.mode = mode;
  g_lastLocalMode = mode;
  g_lastUnlockInWindow = unlockInWindowNonSeeds ? 1 : 0;
  g_lastLocalLevel = 0;
  g_lastIncomplete = 0;
  g_lastNamedLate = 0;
  g_lastWindowLate = 0;
  g_lastPreflightConflicts.clear();
  g_haveLocalBaseline = false;
  g_baselineIncomplete.clear();
  g_baselineShortageBuffers.clear();
  g_baselineOverloadResources.clear();
  // A delete trigger describes a database mutation that must already have
  // happened. Never let CP "reschedule" an order that the caller intended to
  // delete and then report success merely because it is on time.
  const bool hasOrderDeleteOnly =
      (mode == "order" || combined) && localNewDemandNames().empty() &&
      !localDeletedDemandNames().empty();
  if (hasOrderDeleteOnly) {
    size_t stillPresent = 0;
    for (const string& name : localDeletedDemandNames())
      if (Demand::find(name)) ++stillPresent;
    if (stillPresent) {
      guard.skipOptimization = true;
      g_lastLocalOk = 0;
      g_lastLocalStage = "local_delete_not_applied";
      logger << "CP local delete blocked: " << stillPresent
             << " demand(s) still exist; delete demand and operationplans "
                "before rescheduling\n"
             << flush;
      return;
    }
  }
  // Local repair defaults change-rate on (minimize churn). Disable with
  // CP_LOCAL_CHANGE_RATE=0.
  if (envLocalInt("LOCAL_CHANGE_RATE", "CP_LOCAL_CHANGE_RATE", 1) != 0) {
    setenv("CP_CHANGE_RATE_ENABLED", "1", 1);
    setenv("CP_ANCHOR_SEED", "1", 1);
    logger << "CP local: change-rate + anchor-seed enabled "
              "(CP_LOCAL_CHANGE_RATE=1)\n"
           << flush;
  }
  // Material: named supply-change buffers; material-only also allows 自制
  // shortage auto-seed. Combined requires an explicit material list.
  unordered_set<Buffer*> affectedMaterials;
  if (mode == "material" || combined) {
    affectedMaterials = localChangedMaterialsImpl();
    if (affectedMaterials.empty() && mode == "material") {
      for (auto buffer = Buffer::begin(); buffer != Buffer::end(); ++buffer)
        if (buffer->getItem() && buffer->getItem()->getCategory() == "自制" &&
            buffer->getDetectProblems())
          buffer->updateProblems();
      affectedMaterials = localMaterialProblems();
    } else {
      for (Buffer* buf : affectedMaterials)
        if (buf && buf->getDetectProblems()) buf->updateProblems();
    }
  }
  // Resource: machines that changed (broken / replaced), NOT overload.
  // Pure order deletion no longer has a demand tree to use as a seed.  The
  // API captures the deleted tree's resources before deleting it and passes
  // them through CP_LOCAL_CHANGED_RESOURCES.  Shared-resource roots then form
  // the repair scope and can move forward into the released capacity.
  const bool deletedOrderResourceScope = hasOrderDeleteOnly;
  const bool wantResourceSeeds =
      mode == "resource" || combined || deletedOrderResourceScope;
  const unordered_set<Resource*> affectedResources =
      wantResourceSeeds ? localChangedResources(rangeStart, rangeEnd)
                        : unordered_set<Resource*>();
  // Pure-order delete still uses affectedResources for seeding; leave
  // g_lastChangedResources empty so order-only outcome ignores overloads.
  g_lastChangedResources =
      (mode == "resource" || combined) ? affectedResources
                                       : unordered_set<Resource*>();
  g_lastChangedMaterials =
      (mode == "material" || combined) ? affectedMaterials
                                       : unordered_set<Buffer*>();
  g_lastLocalRangeStart = rangeStart;
  g_lastLocalRangeEnd = rangeEnd;

  // The plan current date is a hard execution boundary. Work that has
  // already started belongs to production history / WIP and must never be
  // moved by L1, L2, or L3 merely because its database status has not yet
  // caught up. Freeze the started portion before building any local scope.
  // Future siblings in the same routing remain eligible for repair.
  size_t historicalFrozen = 0;
  for (auto plan = OperationPlan::begin(); plan != OperationPlan::end(); ++plan) {
    OperationPlan* node = &*plan;
    if (!node || (!node->getProposed() && !node->getApproved())) continue;
    const Date start = node->getStart();
    if (start == Date::infinitePast || start == Date::infiniteFuture ||
        start >= origin)
      continue;
    const string status = node->getStatus();
    try {
      node->setBoolProperty("local_history_frozen", true);
      node->setBoolProperty("local_frozen", true);
      node->setBoolProperty("cp_local_frozen", true);
      node->setStatusRaw("confirmed");
      guard.frozen.push_back({node, status});
      ++historicalFrozen;
    } catch (...) {
    }
  }
  if (historicalFrozen)
    logger << "CP local history boundary: frozen " << historicalFrozen
           << " started operationplan(s) before " << origin << "\n"
           << flush;
  if ((mode == "material" || combined) && !affectedMaterials.empty()) {
    logger << "CP local material-change set:";
    for (Buffer* buf : affectedMaterials)
      if (buf) logger << ' ' << buf->getName();
    logger << "\n" << flush;
  }
  if ((mode == "resource" || combined || deletedOrderResourceScope) &&
      !affectedResources.empty()) {
    logger << "CP local resource-change set:";
    for (Resource* res : affectedResources)
      if (res) logger << ' ' << res->getName();
    logger << "\n" << flush;
  }
  vector<OperationPlan*> roots;
  for (auto plan = OperationPlan::begin(); plan != OperationPlan::end(); ++plan)
    if (!plan->getOwner()) roots.push_back(&*plan);
  // L1 movable = union of active trigger families:
  //   order    → named seeds (+ delete-released resources)
  //   material → roots consuming named shortage buffers
  //   resource → roots loading named changed resources
  // + 自制 BOM 前置 + 窗口外全部 proposed/approved（窗外放开）。
  // L2 movable = L1 + 设备/模具一跳邻居（+ BOM）；窗内非种子仍锁。
  // 仅锁：窗内、且不在可动集合中的单。
  const bool wantOrderSeeds =
      mode == "order" ||
      (combined && (!localNewDemandNames().empty() ||
                    !localDeletedDemandNames().empty()));
  const bool wantMaterialSeeds =
      mode == "material" || (combined && !affectedMaterials.empty());
  const bool wantResourceSelect =
      mode == "resource" ||
      (combined && !affectedResources.empty()) ||
      (deletedOrderResourceScope && !affectedResources.empty());
  unordered_set<OperationPlan*> hot;
  for (OperationPlan* root : roots) {
    bool selected = false;
    if (wantOrderSeeds) {
      selected = rootIsLocalOrderSeed(root, rangeStart, rangeEnd);
      if (!selected && deletedOrderResourceScope && !affectedResources.empty())
        selected = localRootHasResource(root, affectedResources, rangeStart,
                                        rangeEnd);
    }
    if (!selected && wantMaterialSeeds) {
      // FAS treats a buffer change as a seed even outside the short CP
      // horizon. Other solvers still require the root to overlap that window.
      selected = localRootHasMaterial(root, affectedMaterials);
      if (!fasLocalPolicy())
        selected = selected && localRootOverlaps(root, rangeStart, rangeEnd);
      if (selected && (root->getConfirmed() || root->getClosed() ||
                       root->getCompleted())) {
        const string ot = root->getOrderType();
        if (ot == "PO" || ot == "STCK") selected = false;
      }
    }
    if (!selected && wantResourceSelect) {
      selected = localRootHasResource(root, affectedResources, rangeStart,
                                      rangeEnd);
    }
    if (selected) hot.insert(root);
  }
  const size_t directRoots = hot.size();
  if (directRoots == 0) {
    // A deleted order with no resource assignments has no meaningful local
    // neighbor scope. The deletion itself is complete, so keep it a no-op.
    if (hasOrderDeleteOnly && !wantMaterialSeeds &&
        (mode == "order" || !wantResourceSelect || affectedResources.empty())) {
      g_lastLocalOk = 1;
      g_lastLocalStage = "local_delete_complete";
      guard.skipOptimization = true;
      logger << "CP local delete: no residual work-order tree; complete\n"
             << flush;
      return;
    }
    // Material on-hand with no manufacturing consumer or producer is a data
    // change, not a failed solve. Succeed when the named buffers are not
    // short. A shortage and no work order that can move is a real miss:
    // stop before the solver rewrites the plan and say why.
    const bool materialWithoutOtherSeeds =
        (fasLocalPolicy() || mrpLocalPolicy()) &&
        (mode == "material" || combined) && !wantOrderSeeds &&
        !wantResourceSelect;
    if (materialWithoutOtherSeeds) {
      const size_t left = affectedMaterials.empty()
                              ? 0
                              : countNamedMaterialShortages(affectedMaterials);
      g_lastIncomplete = left;
      g_lastNamedLate = 0;
      g_lastWindowLate = 0;
      guard.skipOptimization = true;
      if (left == 0) {
        g_lastLocalOk = 1;
        g_lastLocalStage = "local_material_feasible";
        logger << "CP local material: no work order uses the changed buffer; "
                  "on-hand change leaves no shortage; complete\n"
               << flush;
      } else {
        g_lastLocalOk = 0;
        g_lastLocalStage = "local_material_no_root";
        g_lastPreflightConflicts =
            "material on-hand change leaves a shortage and no work order can "
            "repair it";
        logger << "CP local material: shortages_left=" << left
               << " and no root work order can repair it\n"
               << flush;
      }
      return;
    }
    logger << "CP local reschedule: type=" << mode
           << " affected_materials=" << affectedMaterials.size()
           << " affected_resources=" << affectedResources.size()
           << " matched no root work orders; skip local freeze\n"
           << flush;
    guard.skipOptimization = true;
    return;
  }
  unordered_set<OperationPlan*> level1 = hot;
  expandLocalSelfmadePredecessors(level1, roots);
  size_t outsideUnlocked = 0;
  for (OperationPlan* root : roots) {
    if (!root || level1.count(root)) continue;
    if (!root->getProposed() && !root->getApproved()) continue;
    // Outside the repair window: leave movable (do not freeze).
    if (!localRootOverlaps(root, rangeStart, rangeEnd)) {
      if (level1.insert(root).second) ++outsideUnlocked;
    }
  }
  if (outsideUnlocked)
    logger << "CP local L1: unlocked " << outsideUnlocked
           << " root(s) outside window " << rangeStart << ".." << rangeEnd
           << "\n"
           << flush;
  // L2: inherit L1 (already includes 窗外放开) + one-hop neighbors / BOM.
  // Do NOT unlock remaining in-window non-seeds here.
  unordered_set<OperationPlan*> level2 = level1;
  const size_t level2Neighbors =
      expandLocalDirectNeighbors(level2, roots, rangeStart, rangeEnd);
  if (level2Neighbors)
    expandLocalSelfmadePredecessors(level2, roots);
  unordered_set<OperationPlan*> level3;
  for (OperationPlan* root : roots)
    if (root && (root->getProposed() || root->getApproved()))
      level3.insert(root);

  const LocalScopeMetrics metrics1 = localScopeMetrics(level1);
  const LocalScopeMetrics metrics2 = localScopeMetrics(level2);
  const LocalScopeMetrics metrics3 = localScopeMetrics(level3);
  const size_t maxRoots =
      max(1, envLocalInt("LOCAL_AUTO_MAX_ROOTS", "CP_LOCAL_AUTO_MAX_ROOTS", 1500));
  const size_t maxTasks =
      max(1, envLocalInt("LOCAL_AUTO_MAX_TASKS", "CP_LOCAL_AUTO_MAX_TASKS", 2500));
  const size_t maxVariables =
      max(1, envLocalInt("LOCAL_AUTO_MAX_VARS", "CP_LOCAL_AUTO_MAX_VARS", 60000));
  logger << "CP local auto metrics: L3 roots=" << metrics3.roots
         << " tasks=" << metrics3.leafTasks
         << " loads=" << metrics3.constrainedLoads
         << " est_vars=" << metrics3.estimatedVariables
         << "; L2 roots=" << metrics2.roots
         << " tasks=" << metrics2.leafTasks
         << " loads=" << metrics2.constrainedLoads
         << " est_vars=" << metrics2.estimatedVariables
         << "; L1 roots=" << metrics1.roots
         << " tasks=" << metrics1.leafTasks
         << " loads=" << metrics1.constrainedLoads
         << " est_vars=" << metrics1.estimatedVariables
         << "; caps roots=" << maxRoots << " tasks=" << maxTasks
         << " vars=" << maxVariables << "\n";

  int level = explicitLevel;
  if (!level) {
    // Prefer smallest scope that fits: L1 → L2 → L3.
    if (localScopeFits(metrics1, maxRoots, maxTasks, maxVariables))
      level = 1;
    else if (localScopeFits(metrics2, maxRoots, maxTasks, maxVariables))
      level = 2;
    else
      level = 3;
    logger << "CP local auto picked level=" << level
           << " from size caps (direct entry)\n";
  } else if (level < 3 && envInt("LOCAL_KEEP_EXPLICIT_LEVEL", 0) == 0) {
    const LocalScopeMetrics& chosen = level == 2 ? metrics2 : metrics1;
    if (!localScopeFits(chosen, maxRoots, maxTasks, maxVariables)) {
      logger << "CP local level=" << level
             << " exceeds size caps (roots=" << chosen.roots
             << " tasks=" << chosen.leafTasks
             << " est_vars=" << chosen.estimatedVariables
             << "); escalating toward global\n";
      if (level == 1 &&
          localScopeFits(metrics2, maxRoots, maxTasks, maxVariables))
        level = 2;
      else
        level = 3;
    }
  }
  if (level == 2 && metrics2.roots <= metrics1.roots) {
    logger << "CP local level=2 adds no neighbors; using level=1\n";
    level = 1;
  } else if (level == 2 && metrics2.roots >= metrics3.roots) {
    logger << "CP local level=2 already covers all movable roots; "
              "using level=3 global\n";
    level = 3;
  }
  g_lastLocalLevel = level;
  if (level == 3) {
    if (combined) snapshotLocalBaseline();
    logger << "CP local selected level=3 global; future work is movable, "
              "history remains frozen\n"
           << flush;
    return;
  }
  const size_t level1Roots = metrics1.roots;
  hot = level == 2 ? std::move(level2) : std::move(level1);
  if (unlockInWindowNonSeeds) {
    // custom relax=window: keep L1/L2 outside-window policy, but also make
    // every in-window proposed/approved root movable (skip freezing them).
    size_t added = 0;
    for (OperationPlan* root : roots) {
      if (!root || hot.count(root)) continue;
      if (!root->getProposed() && !root->getApproved()) continue;
      if (!localRootOverlaps(root, rangeStart, rangeEnd)) continue;
      if (hot.insert(root).second) ++added;
    }
    if (added)
      logger << "CP local custom window unlock: added " << added
             << " in-window non-seed root(s) to hot\n"
             << flush;
  }
  const size_t deliveryUnlocks = unlockDeliveriesOfHotSupply(hot, roots);
  if (deliveryUnlocks)
    logger << "CP local: unlocked " << deliveryUnlocks
           << " delivery root(s) that ship hot supply\n"
           << flush;
  guard.hotRoots = hot.size();
  {
    // Compact dump of the movable neighborhood for diagnostics.
    vector<OperationPlan*> ordered(hot.begin(), hot.end());
    sort(ordered.begin(), ordered.end(),
         [](OperationPlan* a, OperationPlan* b) {
           return a->getReference() < b->getReference();
         });
    logger << "CP local hot-root list (level=" << level
           << " n=" << ordered.size() << "):";
    size_t shown = 0;
    for (OperationPlan* root : ordered) {
      if (!root) continue;
      logger << " {" << root->getReference();
      if (root->getOperation())
        logger << " " << root->getOperation()->getName();
      logger << " qty=" << root->getQuantity();
      if (root->getBoolProperty("cp_local_insert", false))
        logger << " INSERT";
      logger << "}";
      if (++shown >= 80) {
        logger << " ...";
        break;
      }
    }
    logger << "\n" << flush;
  }
  // Freeze only in-window non-hot lots. Outside-window already in hot.
  for (OperationPlan* root : roots) {
    if (hot.count(root)) continue;
    if (!root->getProposed() && !root->getApproved()) continue;
    if (!localRootOverlaps(root, rangeStart, rangeEnd)) continue;
    vector<OperationPlan*> stack{root};
    bool frozeRoot = false;
    while (!stack.empty()) {
      OperationPlan* node = stack.back();
      stack.pop_back();
      if (node->getProposed() || node->getApproved()) {
        const string status = node->getStatus();
        try {
          node->setBoolProperty("local_frozen", true);
          node->setBoolProperty("cp_local_frozen", true);
          node->setStatusRaw("confirmed");
          guard.frozen.push_back({node, status});
          frozeRoot = true;
        } catch (...) {
        }
      }
      for (OperationPlan::iterator child(node); child != node->end(); ++child)
        stack.push_back(&*child);
    }
    if (frozeRoot) ++guard.frozenRoots;
  }
  // Local repair needs window selection, but schedule-stability/change-rate
  // remains an independent, explicitly enabled feature.
  if (getenv("CP_WINDOW_SELECT") == nullptr)
    setenv("CP_WINDOW_SELECT", "1", 0);
  if (combined) snapshotLocalBaseline();
  const long long bomExtra =
      level == 1 ? static_cast<long long>(guard.hotRoots) -
                       static_cast<long long>(directRoots)
                 : static_cast<long long>(level1Roots) -
                       static_cast<long long>(directRoots);
  logger << "CP local reschedule (重调度): type=" << mode
         << " level=" << level
         << (levelSetting == "custom"
                 ? " custom"
                 : (explicitLevel ? " explicit" : " auto"))
         << " affected_materials=" << affectedMaterials.size()
         << " affected_resources=" << affectedResources.size()
         << " range=" << rangeStart << ".."
         << rangeEnd << " direct_roots=" << directRoots
         << " level2_neighbors=" << level2Neighbors
         << " bom_expanded_roots=" << max(0LL, bomExtra)
         << " hot_roots=" << guard.hotRoots
         << " frozen_roots=" << guard.frozenRoots
         << " frozen_nodes=" << guard.frozen.size()
         << " (non-hot confirmed for this solve)\n"
         << flush;
}

}  // namespace ccAPPS

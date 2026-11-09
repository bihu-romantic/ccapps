#include "ccAPPS/solvercp.h"
#include "ccAPPS/solverlocal.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <limits>
#include <map>
#include <sstream>
#include <string>
#include <tuple>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#ifdef HAVE_ORTOOLS
#include "ortools/sat/cp_model.h"
#include "ortools/sat/cp_model_checker.h"
#include "ortools/sat/cp_model_solver.h"
#endif

namespace ccAPPS {

namespace {
// Local outcome/stage state lives in solverlocal.cpp (shared across solvers).

struct LocalCascadeLoadState {
  Load* load = nullptr;
  Resource* resource = nullptr;
};
struct LocalCascadePlanState {
  OperationPlan* plan = nullptr;
  Operation* operation = nullptr;
  Date start;
  Date end;
  double quantity = 0.0;
  string status;
  vector<LocalCascadeLoadState> loads;
};
vector<LocalCascadePlanState> g_localCascadeSnapshot;
unordered_set<OperationPlan*> g_localCascadePlans;
}

size_t cpLastRunApplied() { return localLastApplied(); }
void cpSetLastRunApplied(size_t n) { localSetLastApplied(n); }
PyObject* cp_last_run_applied(PyObject* self, PyObject* args) {
  return local_last_applied(self, args);
}
PyObject* cp_last_local_ok(PyObject* self, PyObject* args) {
  return local_last_ok(self, args);
}
PyObject* cp_last_run_stage(PyObject* self, PyObject* args) {
  return local_last_stage(self, args);
}
PyObject* cp_last_local_outcome(PyObject* self, PyObject* args) {
  return local_last_outcome(self, args);
}
PyObject* cp_last_preflight_conflicts(PyObject* self, PyObject* args) {
  return local_last_preflight_conflicts(self, args);
}
PyObject* cp_mark_candidate_no_supply(PyObject* self, PyObject* args) {
  return local_mark_candidate_no_supply(self, args);
}

namespace {

struct CapacityUse {
  const Load* load = nullptr;
  Resource* resource = nullptr;
  long long quantity = 0;
};

struct Candidate {
  Resource* resource = nullptr;
  Operation* operation = nullptr;
  int duration = 1;
  int machine = -1;
  vector<CapacityUse> resources;
};

struct Task {
  OperationPlan* plan = nullptr;
  int job = -1;
  int sequence = 0;
  bool baseline = false;
  vector<Candidate> candidates;
};

bool isLeafResource(const Resource* resource) {
  return resource && !resource->hasType<ResourceInfinite>() &&
         resource->getConstrained() && !resource->getFirstChild();
}

void collectLeaves(Resource* resource, vector<Resource*>& out) {
  if (!resource || resource->hasType<ResourceInfinite>() ||
      !resource->getConstrained()) return;
  if (!resource->getFirstChild()) {
    out.push_back(resource);
    return;
  }
  for (Resource* child = resource->getFirstChild(); child;
       child = child->getNextBrother())
    collectLeaves(child, out);
}

int durationSeconds(OperationPlan* plan, Operation* operation = nullptr) {
  if (!operation && plan) operation = plan->getOperation();
  if (!operation) return 1;
  // CP does not consult capa, mfg_lt, or po_lt.
  if (operation->hasType<OperationTimePer>()) {
    auto* timed = static_cast<OperationTimePer*>(operation);
    double seconds = timed->getDuration().getSeconds() +
                     timed->getDurationPer() * plan->getQuantity();
    // Keep MRP's instantaneous semantics for an un-timed manufacturing
    // operation. A constrained resource candidate is clamped below, since a
    // zero-duration load must never create free finite capacity.
    if (seconds <= 0.0) return 0;
    if (seconds < 1.0) seconds = 1.0;
    // The core planner stores sub-second calculated durations on whole-second
    // timestamps by truncating the fraction. Match that convention so two
    // back-to-back MRP lots don't overlap by one second in CP.
    return static_cast<int>(seconds);
  }
  if (operation->hasType<OperationFixedTime>()) {
    long seconds = static_cast<OperationFixedTime*>(operation)->getDuration();
    return static_cast<int>(seconds > 0 ? seconds : 1);
  }
  return 1;
}

vector<Candidate> candidatesFor(Operation* operation, OperationPlan* plan) {
  vector<Candidate> found;
  if (!operation) return found;
  found.push_back({nullptr, operation, durationSeconds(plan, operation), -1});
  const Date start = Plan::instance().getCurrent();
  const Date end = start + Duration(86400L);
  for (const Load& load : operation->getLoads()) {
    if (load.getAlternate() || !load.getResource() ||
        load.getResource()->hasType<ResourceInfinite>() ||
        !load.getResource()->getConstrained()) continue;
    vector<Resource*> leaves;
    collectLeaves(load.getResource(), leaves);
    vector<Candidate> expanded;
    const long long quantity = llround(load.getQuantity() * 1000.0);
    for (Resource* resource : leaves) {
      if (load.getSkill() && !resource->hasSkill(load.getSkill(), start, end))
        continue;
      for (const Candidate& partial : found) {
        Candidate candidate = partial;
        if (!candidate.resource) candidate.resource = resource;
        candidate.resources.push_back({&load, resource, quantity});
        expanded.push_back(std::move(candidate));
      }
    }
    found = std::move(expanded);
    if (found.empty()) break;
  }
  for (Candidate& candidate : found)
    if (!candidate.resources.empty() && candidate.duration <= 0)
      candidate.duration = 1;
  return found;
}

vector<Candidate> assignedLeaves(OperationPlan* plan) {
  vector<Candidate> found;
  for (auto load = plan->beginLoadPlans(); load != plan->endLoadPlans(); ++load) {
    if (!load->isStart() || load->getQuantity() <= 0) continue;
    Resource* resource = load->getResource();
    if (!isLeafResource(resource)) continue;
    bool seen = false;
    for (const Candidate& candidate : found)
      if (candidate.resource == resource) seen = true;
    if (seen) continue;
    found.push_back(
        {resource, plan->getOperation(), durationSeconds(plan), -1});
  }
  return found;
}

bool sameMaterialBalance(Operation* left, Operation* right) {
  using Signature = tuple<Buffer*, double, double, double, bool>;
  vector<Signature> a, b;
  auto collect = [](Operation* operation, vector<Signature>& result) {
    for (const Flow& flow : operation->getFlows()) {
      double batch = flow.hasType<FlowTransferBatch>()
          ? static_cast<const FlowTransferBatch&>(flow).getTransferBatch() : 0.0;
      result.emplace_back(flow.getBuffer(), flow.getQuantity(),
                          flow.getQuantityFixed(), batch, flow.hasType<FlowEnd>());
    }
    sort(result.begin(), result.end());
  };
  collect(left, a);
  collect(right, b);
  return a == b;
}

vector<Candidate> taskCandidates(OperationPlan* plan) {
  Operation* operation = plan->getOperation();
  if (operation && operation->getOwner() &&
      operation->getOwner()->hasType<OperationAlternate>()) {
    vector<Candidate> found;
    for (SubOperation* sub : operation->getOwner()->getSubOperations()) {
      if (!sub || !sub->getOperation()) continue;
      if (!sameMaterialBalance(operation, sub->getOperation())) continue;
      auto part = candidatesFor(sub->getOperation(), plan);
      found.insert(found.end(), part.begin(), part.end());
    }
    if (!found.empty()) return found;
  }
  vector<Candidate> expanded = candidatesFor(operation, plan);
  return expanded;
}

bool schedulable(OperationPlan* plan) {
  Operation* operation = plan->getOperation();
  if (!operation || plan->getQuantity() <= 0 ||
      !operation->hasType<OperationTimePer, OperationFixedTime>()) return false;
  if (operation->hasType<OperationItemSupplier, OperationItemDistribution,
                         OperationSplit>() ||
      operation->hasType<OperationRouting, OperationAlternate>())
    return false;
  return !taskCandidates(plan).empty();
}

int envInt(const char* name, int fallback);

bool locked(OperationPlan* plan) {
  // In the full candidate experiment an approved plan is the MRP baseline
  // marker. It remains a CP decision variable, while confirmed/completed
  // work stays fixed. Python restores the baseline status after CP exports.
  if (envInt("CP_BASELINE_VARIABLE", 0) != 0 && schedulable(plan) &&
      plan->getApproved() &&
      !plan->getConfirmed() && !plan->getCompleted() && !plan->getClosed())
    return false;
  // Order-local insert marks historical proposed → approved as a keep-marker
  // (not a freeze). applyLocalRescheduleScope then confirms non-hot roots.
  // Approved-but-not-confirmed must stay movable so L1/L2 hot neighborhoods
  // and L3 global can actually reschedule after 增/删/改.
  if (localRescheduleEnabled() && plan->getApproved() &&
      !plan->getConfirmed() && !plan->getCompleted() && !plan->getClosed())
    return false;
  return plan->getConfirmed() || plan->getApproved() || plan->getCompleted() ||
         plan->getClosed();
}


// Mirror of solvercp_partition.cpp workshop tagging: 1 / 23(=2+3) / 4 / 5.
int workshopGroupOf(OperationPlan* plan) {
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

// When CP_PARTITION_GROUP is set, only that workshop participates in the CP
// model. Sibling-workshop proposed AND locked/confirmed plans are skipped —
// FAS remainder freezes otherwise dump thousands of locked material events
// into every group's reservoir and make 1/23/4 INFEASIBLE.
bool partitionExcluded(OperationPlan* plan) {
  const int group = envInt("CP_PARTITION_GROUP", 0);
  if (group <= 0 || !plan) return false;
  return workshopGroupOf(plan) != group;
}

OperationPlan* jobPlan(OperationPlan* plan) {
  OperationPlan* routing = nullptr;
  for (OperationPlan* current = plan; current && current->getOwner();) {
    current = current->getOwner();
    if (current->getOperation() &&
        current->getOperation()->hasType<OperationRouting>())
      routing = current;
  }
  return routing ? routing : plan;
}

int sequencePriority(OperationPlan* plan, OperationPlan* job) {
  if (!job || job == plan || !job->getOperation() ||
      !job->getOperation()->hasType<OperationRouting>())
    return 0;
  Operation* routing = job->getOperation();
  Operation* operation = plan->getOperation();
  while (operation && operation->getOwner() && operation->getOwner() != routing)
    operation = operation->getOwner();
  if (!operation || operation->getOwner() != routing) return 1000000;
  for (SubOperation* sub : routing->getSubOperations())
    if (sub && sub->getOperation() == operation) return sub->getPriority();
  return 1000000;
}

int envInt(const char* name, int fallback) {
  const char* value = getenv(name);
  if (!value || !*value) return fallback;
  return atoi(value);
}

string envString(const char* name, const char* fallback) {
  const char* value = getenv(name);
  if (!value || !*value) return fallback;
  return value;
}

// earlyPersonnel / personnel-throughput metrics match util reporting.
// Default "all" = every category=人员 resource (all 工段). Override with a
// concrete name (e.g. PERSON_制造部4工段) to scope a single workshop.
string cpEarlyPersonnelResourceScope() {
  return envString("CP_EARLY_PERSONNEL_RESOURCE", "all");
}

bool cpCountsForEarlyPersonnel(Resource* resource) {
  if (!resource || resource->getCategory() != "人员") return false;
  const string scope = cpEarlyPersonnelResourceScope();
  if (scope == "all" || scope == "*") return true;
  return resource->getName() == scope;
}

// Constrain manufactured components. True external infinite buffers stay
// unlimited, but 自制 (or any buffer that already has a producing MO in the
// plan) must stay non-negative even when convert historically marked them
// infinite because no route leaf existed yet.
bool materialSupplyLimited(Buffer* buffer) {
  if (!buffer) return false;
  const bool selfMade =
      buffer->getItem() && buffer->getItem()->getCategory() == "自制";
  bool hasMaker = false;
  if (!selfMade) {
    for (const Flow& flow : buffer->getFlows())
      if (flow.getQuantity() > ROUNDING_ERROR && flow.getOperation() &&
          flow.getOperation()->hasType<OperationTimePer, OperationFixedTime>()) {
        hasMaker = true;
        break;
      }
  }
  if (buffer->hasType<BufferInfinite>() && !selfMade && !hasMaker)
    return false;
  const string mode = envString("CP_MATERIAL_MODE", "all");
  if (mode == "off" || mode == "none" || mode == "0") return false;
  if (mode != "selfmade") return true;
  if (selfMade) return true;
  return hasMaker;
}

// L3 / local reschedule moves manufacturing globally, then
// alignDeliveryCommitments() rewrites proposed deliveries to
// max(receipt, due). Pinning those same proposed DLVR as lockedMaterial
// during search fights that: old due drains stay hard while MOs move.
// Default-on with local reschedule; closed/confirmed stays locked via
// locked(). Set CP_LOCAL_FREE_DELIVERY_MATERIAL=0 to keep old behaviour.
bool cpLocalFreeProposedDeliveryMaterial() {
  if (!localRescheduleEnabled()) return false;
  return envInt("CP_LOCAL_FREE_DELIVERY_MATERIAL", 1) != 0;
}

// L1/L2 freeze most of the plan as confirmed occupancy and only append new
// work after the latest frozen equipment use. Re-encoding those frozen trees
// as lockedMaterial (often hundreds of events from a DB reload) commonly
// makes the reservoir INFEASIBLE even when a later equipment slot is free.
// Default-on with local reschedule: trust frozen history, model material only
// for movable lots. Set CP_LOCAL_SKIP_FROZEN_MATERIAL=0 to keep old behaviour.
bool cpLocalSkipFrozenMaterial() {
  if (!localRescheduleEnabled()) return false;
  return envInt("CP_LOCAL_SKIP_FROZEN_MATERIAL", 1) != 0;
}

struct Placement {
  OperationPlan* plan = nullptr;
  Resource* resource = nullptr;
  Operation* operation = nullptr;
  int duration = 1;
  long order = 0;
  vector<CapacityUse> resources;
};

struct SupplyEvent {
  Date when;
  double qty = 0.0;
};

int parallelSlots(Resource* resource) {
  if (!resource) return 1;
  const int slots = static_cast<int>(resource->getMaximum() + 1e-9);
  return slots < 1 ? 1 : slots;
}

bool sameResourcePool(Resource* loadResource, Resource* chosen) {
  if (!loadResource || !chosen) return false;
  return loadResource == chosen || chosen->isMemberOf(loadResource) ||
         loadResource->isMemberOf(chosen);
}

// The operation load names the pool. MRP may already have pinned the loadplan
// to one leaf, while CP selects another leaf of that same pool.
bool loadAcceptsChosen(const Load* declared, Resource* current, Resource* chosen) {
  if (!chosen) return false;
  Resource* pool = declared ? declared->getResource() : nullptr;
  if (pool && (chosen == pool || chosen->isMemberOf(pool))) return true;
  if (sameResourcePool(current, chosen)) return true;
  Resource* currentOwner = current ? current->getOwner() : nullptr;
  Resource* chosenOwner = chosen->getOwner();
  return currentOwner && currentOwner == chosenOwner;
}

bool bufferHasProducer(Buffer* buffer) {
  if (!buffer) return false;
  for (const Flow& flow : buffer->getFlows())
    if (flow.isProducer() && flow.getOperation()) return true;
  return false;
}

Date resourceFreeAt(const unordered_map<Resource*, vector<Date>>& busy,
                    Resource* resource) {
  auto found = busy.find(resource);
  if (found == busy.end() ||
      static_cast<int>(found->second.size()) < parallelSlots(resource))
    return Date::infinitePast;
  return *min_element(found->second.begin(), found->second.end());
}

void occupyResource(unordered_map<Resource*, vector<Date>>& busy,
                    Resource* resource, Date end) {
  if (!resource) return;
  vector<Date>& slots = busy[resource];
  if (static_cast<int>(slots.size()) >= parallelSlots(resource))
    *min_element(slots.begin(), slots.end()) = end;
  else
    slots.push_back(end);
}

bool resourceBusy(const unordered_map<Resource*, vector<Date>>& busy,
                  Resource* resource, Date start) {
  auto found = busy.find(resource);
  if (found == busy.end()) return false;
  int occupied = 0;
  for (Date end : found->second)
    if (end > start) ++occupied;
  return occupied >= parallelSlots(resource);
}

Date alignResourceCalendar(Resource* resource, Date earliest) {
  Calendar* calendar = resource ? resource->getAvailable() : nullptr;
  if (!calendar) return earliest;
  for (int step = 0; step < 64; ++step) {
    CalendarBucket* bucket = calendar->findBucket(earliest, true);
    if (!bucket || bucket->getValue() > 0.0) return earliest;
    if (bucket->getEnd() <= earliest) return earliest;
    earliest = bucket->getEnd();
  }
  return earliest;
}

PooledString requiredSetup(OperationPlan* plan, Resource* resource) {
  Operation* operation = plan ? plan->getOperation() : nullptr;
  if (!operation || !resource) return PooledString();
  for (const Load& load : operation->getLoads()) {
    Resource* pool = load.getResource();
    if (pool && (pool == resource || resource->isMemberOf(pool)))
      return load.getSetup();
  }
  return PooledString();
}

long setupSeconds(Resource* resource, const PooledString& from,
                  const PooledString& to) {
  if (!resource || !resource->getSetupMatrix()) return 0;
  SetupMatrixRule* rule =
      resource->getSetupMatrix()->calculateSetup(from, to, nullptr);
  if (!rule) return 0;
  long seconds = static_cast<long>(rule->getDuration());
  if (seconds < 0) return 0;
  const long cap = 366L * 86400L;
  return seconds > cap ? cap : seconds;
}

double transferFraction(OperationPlan* plan) {
  if (!plan || !plan->getOperation() || plan->getQuantity() <= 0.0) return 1.0;
  for (const Flow& flow : plan->getOperation()->getFlows()) {
    if (!flow.isProducer() || !flow.hasType<FlowTransferBatch>()) continue;
    const double batch =
        static_cast<const FlowTransferBatch&>(flow).getTransferBatch();
    if (batch <= 0.0) continue;
    return min(1.0, batch / plan->getQuantity());
  }
  return 1.0;
}

Date transferReady(OperationPlan* plan) {
  if (!plan || !plan->getOperation()) return plan ? plan->getEnd() : Date::infinitePast;
  double batch = 0.0;
  bool found = false;
  for (const Flow& flow : plan->getOperation()->getFlows()) {
    if (!flow.isProducer() || !flow.hasType<FlowTransferBatch>()) continue;
    batch = static_cast<const FlowTransferBatch&>(flow).getTransferBatch();
    found = batch > 0.0;
    if (found) break;
  }
  if (!found || plan->getQuantity() <= 0.0) return plan->getEnd();
  const double fraction = min(1.0, batch / plan->getQuantity());
  if (fraction >= 1.0 - 1e-9) return plan->getEnd();
  long seconds = static_cast<long>(
      (plan->getEnd() - plan->getStart()).getSeconds() * fraction + 0.5);
  if (seconds < 1) seconds = 1;
  return plan->getOperation()
      ->calculateOperationTime(plan, plan->getStart(), Duration(seconds), true)
      .getEnd();
}

Date materialReady(OperationPlan* plan, Date earliest,
                   const unordered_map<Buffer*, vector<SupplyEvent>>& supply) {
  Operation* operation = plan ? plan->getOperation() : nullptr;
  if (!operation) return earliest;
  for (const Flow& flow : operation->getFlows()) {
    if (!flow.isConsumer()) continue;
    Buffer* buffer = nullptr;
    try {
      buffer = flow.getBuffer();
    } catch (const DataException&) {
      continue;
    }
    if (!buffer || !bufferHasProducer(buffer)) continue;
    const double need =
        -(flow.getQuantity() * plan->getQuantity() + flow.getQuantityFixed());
    if (need <= ROUNDING_ERROR) continue;
    auto found = supply.find(buffer);
    if (found == supply.end()) continue;
    double have = 0.0;
    bool covered = false;
    for (const SupplyEvent& event : found->second) {
      have += event.qty;
      if (have + ROUNDING_ERROR >= need) {
        earliest = max(earliest, event.when);
        covered = true;
        break;
      }
    }
    (void)covered;
  }
  return earliest;
}

void recordSupply(OperationPlan* plan,
                  unordered_map<Buffer*, vector<SupplyEvent>>& supply) {
  Operation* operation = plan ? plan->getOperation() : nullptr;
  if (!operation) return;
  const Date released = transferReady(plan);
  for (const Flow& flow : operation->getFlows()) {
    if (!flow.isProducer()) continue;
    Buffer* buffer = nullptr;
    try {
      buffer = flow.getBuffer();
    } catch (const DataException&) {
      continue;
    }
    if (!buffer) continue;
    const double qty =
        flow.getQuantity() * plan->getQuantity() + flow.getQuantityFixed();
    if (qty <= ROUNDING_ERROR) continue;
    double batch = qty;
    if (flow.hasType<FlowTransferBatch>()) {
      const double size =
          static_cast<const FlowTransferBatch&>(flow).getTransferBatch();
      if (size > 0.0) batch = min(qty, size);
    }
    vector<SupplyEvent>& events = supply[buffer];
    if (batch + ROUNDING_ERROR < qty) {
      events.push_back({released, batch});
      events.push_back({plan->getEnd(), qty - batch});
    } else {
      events.push_back({released, qty});
    }
    sort(events.begin(), events.end(),
         [](const SupplyEvent& left, const SupplyEvent& right) {
           return left.when < right.when;
         });
  }
}

Resource* findQualifiedResource(
    const Load& load, Date start, Date end,
    const unordered_map<Resource*, vector<Date>>& busy,
    const unordered_map<Resource*, vector<pair<Date, Date>>>& blocked) {
  vector<Resource*> leaves;
  collectLeaves(load.getResource(), leaves);
  Skill* skill = load.getSkill();
  for (Resource* leaf : leaves) {
    if (skill && !leaf->hasSkill(skill, start, end)) continue;
    if (resourceBusy(busy, leaf, start)) continue;
    bool conflict = false;
    auto windows = blocked.find(leaf);
    if (windows != blocked.end() && parallelSlots(leaf) <= 1) {
      for (const auto& window : windows->second) {
        if (start < window.second && window.first < end) conflict = true;
      }
    }
    if (!conflict) return leaf;
  }
  return nullptr;
}

// Confirmed/completed/closed stay fixed. Proposed and the approved baseline
// marker used by CP candidate expansion remain movable for delivery realign
// and partial cleanup (locked() treats approved as fixed).
bool firmLocked(OperationPlan* plan) {
  return plan && (plan->getConfirmed() || plan->getCompleted() ||
                  plan->getClosed());
}

void alignDeliveryCommitments() {
  // CP rewrites manufacturing dates and leaves the MRP delivery where it was.
  // Same rule as the GA post-process: commitment = max(material receipt, due).
  // A same-batch manufacturing receipt is authoritative. Shared unbatched
  // buffers fall back to FIFO, otherwise an earlier unrelated lot can leave
  // this delivery on its due date while its own lot is still open.
  // Snapshot first. setStartAndEnd reorders the operationplan list, so
  // updating a delivery while iterating that list skips the ones it
  // bubbles past.
  vector<OperationPlan*> deliveries;
  for (auto plan = OperationPlan::begin(); plan != OperationPlan::end();
       ++plan) {
    OperationPlan* op = &*plan;
    if (!op->getOperation() ||
        !op->getOperation()->hasType<OperationDelivery>())
      continue;
    if (firmLocked(op)) continue;
    deliveries.push_back(op);
  }
  size_t moved = 0;
  for (OperationPlan* op : deliveries) {

    Date due = Date::infinitePast;
    if (Demand* demand = op->getDemand()) due = demand->getDue();
    const PooledString batch = op->getBatch();
    Item* item = nullptr;
    if (Demand* demand = op->getDemand()) item = demand->getItem();
    if (!item) {
      for (auto flow = op->beginFlowPlans(); flow != op->endFlowPlans(); ++flow)
        if (flow->getQuantity() < -ROUNDING_ERROR && flow->getBuffer() &&
            flow->getBuffer()->getItem()) {
          item = flow->getBuffer()->getItem();
          break;
        }
    }

    Date materialReady = Date::infinitePast;
    if (batch && item) {
      for (auto producer = OperationPlan::begin();
           producer != OperationPlan::end(); ++producer) {
        if (&*producer == op || producer->getBatch() != batch) continue;
        Item* producerItem = nullptr;
        Date receipt = Date::infinitePast;
        for (auto produce = producer->beginFlowPlans();
             produce != producer->endFlowPlans(); ++produce) {
          if (produce->getQuantity() <= ROUNDING_ERROR || !produce->getBuffer() ||
              !produce->getBuffer()->getItem())
            continue;
          if (!producerItem) producerItem = produce->getBuffer()->getItem();
          if (produce->getBuffer()->getItem() != item) continue;
          const Date posted = produce->getDate();
          if (posted != Date::infinitePast && posted != Date::infiniteFuture)
            receipt = max(receipt, posted);
        }
        if (!producerItem || producerItem != item) continue;
        Date available = producer->getEnd();
        if (producer->getSubOperationPlans(false) != OperationPlan::end()) {
          Date leafEnd = Date::infinitePast;
          vector<OperationPlan*> stack{&*producer};
          while (!stack.empty()) {
            OperationPlan* node = stack.back();
            stack.pop_back();
            bool hasChild = false;
            for (OperationPlan::iterator child(node); child != node->end();
                 ++child) {
              hasChild = true;
              stack.push_back(&*child);
            }
            if (!hasChild) leafEnd = max(leafEnd, node->getEnd());
          }
          if (leafEnd != Date::infinitePast) available = leafEnd;
        }
        if (receipt != Date::infinitePast) available = max(available, receipt);
        if (available != Date::infinitePast &&
            available != Date::infiniteFuture)
          materialReady = max(materialReady, available);
      }
    }
    if (materialReady == Date::infinitePast) {
      for (auto consume = op->beginFlowPlans(); consume != op->endFlowPlans();
           ++consume) {
        if (consume->getQuantity() >= -ROUNDING_ERROR || !consume->getBuffer())
          continue;
        Buffer* buffer = consume->getBuffer();
        double remaining = -consume->getQuantity();
        struct Receipt {
          Date available;
          double quantity;
          string reference;
        };
        vector<Receipt> candidates;
        for (auto producer = OperationPlan::begin();
             producer != OperationPlan::end(); ++producer) {
          if (&*producer == op) continue;
          for (auto produce = producer->beginFlowPlans();
               produce != producer->endFlowPlans(); ++produce) {
            if (produce->getQuantity() <= ROUNDING_ERROR ||
                produce->getBuffer() != buffer)
              continue;
            Date available = producer->getEnd();
            if (produce->getDate() != Date::infinitePast &&
                produce->getDate() != Date::infiniteFuture)
              available = max(available, produce->getDate());
            candidates.push_back(
                {available, produce->getQuantity(), producer->getReference()});
          }
        }
        stable_sort(candidates.begin(), candidates.end(),
                    [](const Receipt& left, const Receipt& right) {
                      if (left.available != right.available)
                        return left.available < right.available;
                      return left.reference < right.reference;
                    });
        Date batchReady = Date::infinitePast;
        for (const auto& candidate : candidates) {
          if (remaining <= ROUNDING_ERROR) break;
          const double take = min(remaining, candidate.quantity);
          if (take <= ROUNDING_ERROR) continue;
          remaining -= take;
          batchReady = max(batchReady, candidate.available);
        }
        if (batchReady != Date::infinitePast)
          materialReady = max(materialReady, batchReady);
      }
    }

    Date commitment = materialReady;
    if (due != Date::infinitePast) commitment = max(commitment, due);
    if (commitment == Date::infinitePast) continue;
    if (commitment == op->getStart() && commitment == op->getEnd()) continue;
    op->setStartAndEnd(commitment, commitment);
    ++moved;
  }

  // Reserve shared, unbatched supply cumulatively. The calculation above is
  // intentionally local to one delivery and can otherwise let multiple
  // deliveries reuse the same early receipts. Allocate inventory/receipts
  // once in demand order and postpone each delivery to its last allocated
  // receipt.
  struct FifoReceipt {
    Date available;
    double remaining;
    string reference;
  };
  struct FifoSupply {
    bool initialized = false;
    double onhand = 0.0;
    vector<FifoReceipt> receipts;
  };
  map<Buffer*, FifoSupply> fifo;
  stable_sort(deliveries.begin(), deliveries.end(),
              [](OperationPlan* left, OperationPlan* right) {
    Demand* a = left ? left->getDemand() : nullptr;
    Demand* b = right ? right->getDemand() : nullptr;
    if (a && b) {
      if (a->getPriority() != b->getPriority())
        return a->getPriority() < b->getPriority();
      if (a->getDue() != b->getDue()) return a->getDue() < b->getDue();
    }
    return left->getReference() < right->getReference();
  });
  const Date origin = Plan::instance().getCurrent();
  size_t fifoMoved = 0;
  for (OperationPlan* delivery : deliveries) {
    Date ready = Date::infinitePast;
    for (auto consume = delivery->beginFlowPlans();
         consume != delivery->endFlowPlans(); ++consume) {
      if (consume->getQuantity() >= -ROUNDING_ERROR || !consume->getBuffer())
        continue;
      Buffer* buffer = consume->getBuffer();
      FifoSupply& supply = fifo[buffer];
      if (!supply.initialized) {
        supply.initialized = true;
        supply.onhand = max(0.0, buffer->getOnHand());
        for (auto producer = OperationPlan::begin();
             producer != OperationPlan::end(); ++producer) {
          if (&*producer == delivery) continue;
          for (auto produce = producer->beginFlowPlans();
               produce != producer->endFlowPlans(); ++produce) {
            if (produce->getQuantity() <= ROUNDING_ERROR ||
                produce->getBuffer() != buffer)
              continue;
            Date available = produce->getDate();
            if (available == Date::infinitePast ||
                available == Date::infiniteFuture)
              available = producer->getEnd();
            if (available <= origin) {
              supply.onhand += produce->getQuantity();
              continue;
            }
            supply.receipts.push_back(
                {available, produce->getQuantity(), producer->getReference()});
          }
        }
        stable_sort(supply.receipts.begin(), supply.receipts.end(),
                    [](const FifoReceipt& a, const FifoReceipt& b) {
          if (a.available != b.available) return a.available < b.available;
          return a.reference < b.reference;
        });
      }
      double required = -consume->getQuantity();
      const double stock = min(required, supply.onhand);
      supply.onhand -= stock;
      required -= stock;
      for (FifoReceipt& receipt : supply.receipts) {
        if (required <= ROUNDING_ERROR) break;
        const double take = min(required, receipt.remaining);
        if (take <= ROUNDING_ERROR) continue;
        receipt.remaining -= take;
        required -= take;
        ready = max(ready, receipt.available);
      }
    }
    Date commitment = delivery->getEnd();
    if (Demand* demand = delivery->getDemand())
      commitment = max(commitment, demand->getDue());
    if (ready != Date::infinitePast) commitment = max(commitment, ready);
    if (commitment != delivery->getStart() || commitment != delivery->getEnd()) {
      delivery->setStartAndEnd(commitment, commitment);
      ++fifoMoved;
    }
  }
  if (moved)
    logger << "CP: aligned " << moved
           << " delivery commitments to max(receipt, due)\n";
  if (fifoMoved)
    logger << "CP: FIFO-reserved shared material across " << fifoMoved
           << " delivery commitments\n";
}

void clearResolvedConstraints(const vector<Placement>& rows) {
  // MRP records these while planning backwards onto the pool. CP then places
  // the work on leaves and starts at the current date, so those traces no
  // longer describe the exported timeline.
  unordered_set<Resource*> scheduled;
  for (const Placement& row : rows) {
    for (Resource* resource = row.resource; resource;
         resource = resource->getOwner())
      scheduled.insert(resource);
  }
  const Date origin = Plan::instance().getCurrent();
  size_t clearedCapacity = 0;
  size_t clearedLead = 0;
  for (auto& demand : Demand::all()) {
    auto& constraints = demand.getConstraints();
    for (auto constraint = constraints.begin();
         constraint != constraints.end();) {
      bool drop = false;
      if (constraint->hasType<ProblemCapacityOverload>()) {
        auto* overload = static_cast<ProblemCapacityOverload*>(&*constraint);
        Resource* resource = overload->getResource();
        bool live = false;
        if (resource && resource->getDetectProblems()) {
          for (auto problem = resource->getProblems();
               problem != Problem::end(); ++problem) {
            if (!problem->hasType<ProblemCapacityOverload>()) continue;
            live = true;
            break;
          }
        }
        drop = resource && scheduled.count(resource) && !live;
      } else if (constraint->hasType<ConstraintManufacturingLeadTime>() &&
                 constraint->getDates().getEnd() <= origin) {
        drop = true;
      }
      if (!drop) {
        ++constraint;
        continue;
      }
      auto* stale = &*constraint;
      const bool capacity = stale->hasType<ProblemCapacityOverload>();
      constraints.unlink(stale);
      ++constraint;
      delete stale;
      if (capacity)
        ++clearedCapacity;
      else
        ++clearedLead;
    }
    constraints.clean(&demand);
  }
  if (clearedCapacity || clearedLead)
    logger << "CP: cleared " << clearedCapacity
           << " capacity and " << clearedLead
           << " lead-time constraints left by MRP\n";
}

void tryDeleteOpPlan(OperationPlan* plan);

// Reserve delivery quantities against the selected manufacturing receipts.
// Demand quantities remain untouched: missing supply stays as unmet demand.
void trimPartialDeliveries(Date limit) {
  vector<OperationPlan*> deliveries;
  map<Buffer*, double> available;
  map<OperationPlan*, map<Buffer*, double>> requirements;
  for (auto plan = OperationPlan::begin(); plan != OperationPlan::end(); ++plan) {
    if (!plan->getOperation()->hasType<OperationDelivery>() || firmLocked(&*plan))
      continue;
    deliveries.push_back(&*plan);
    for (auto flow = plan->beginFlowPlans(); flow != plan->endFlowPlans(); ++flow) {
      if (flow->getQuantity() >= -ROUNDING_ERROR || !flow->getBuffer()) continue;
      Buffer* buffer = flow->getBuffer();
      if (!available.count(buffer)) available[buffer] = buffer->getOnHand(limit);
      if (flow->getDate() <= limit) available[buffer] -= flow->getQuantity();
      if (plan->getQuantity() > ROUNDING_ERROR)
        requirements[&*plan][buffer] += -flow->getQuantity()/plan->getQuantity();
    }
  }
  sort(deliveries.begin(),deliveries.end(),[](OperationPlan* a,OperationPlan* b) {
    Demand* left = a->getDemand(); Demand* right = b->getDemand();
    if (left && right) {
      if (left->getPriority() != right->getPriority()) return left->getPriority() < right->getPriority();
      if (left->getDue() != right->getDue()) return left->getDue() < right->getDue();
    }
    return a->getReference() < b->getReference();
  });
  size_t reduced = 0;
  for (OperationPlan* delivery : deliveries) {
    // A delivery already moved past this limit is covered by a later
    // receipt. Cutting it here drops a demand the search had supplied.
    bool inside = false;
    for (auto flow = delivery->beginFlowPlans();
         flow != delivery->endFlowPlans(); ++flow) {
      if (flow->getQuantity() < -ROUNDING_ERROR && flow->getDate() <= limit)
        inside = true;
    }
    if (!inside) continue;
    double quantity = delivery->getQuantity();
    for (const auto& use : requirements[delivery])
      if (materialSupplyLimited(use.first))
        quantity = min(quantity,max(0.0,available[use.first])/use.second);
    for (const auto& use : requirements[delivery]) available[use.first] -= quantity*use.second;
    if (quantity + ROUNDING_ERROR < delivery->getQuantity()) {
      ++reduced;
      if (quantity <= ROUNDING_ERROR) {
        if (delivery->getApproved()) delivery->setProposed(true);
        tryDeleteOpPlan(delivery);
      } else delivery->setQuantity(quantity,false);
    }
    // Dates are realigned by alignDeliveryCommitments() to max(receipt, due).
    // Do not pin every surviving delivery to the window end — that made
    // finished lots look late even when manufacturing finished on due day.
  }
  logger << "CP partial deliveries reduced=" << reduced
         << "; unmet demand remains open\n";
}

void placeOnCalendars(vector<Placement>& rows, bool partial = false, long horizon = 0) {
  const Date origin = Plan::instance().getCurrent();
  bool changeoverEnabled = false;
  for (const Placement& row : rows) {
    if (row.resource && row.resource->getSetupMatrix()) changeoverEnabled = true;
  }
  unordered_map<Resource*, vector<pair<Date, Date>>> blocked;
  for (auto plan = OperationPlan::begin(); plan != OperationPlan::end(); ++plan) {
    if (!locked(&*plan)) continue;
    for (auto load = plan->beginLoadPlans(); load != plan->endLoadPlans(); ++load) {
      if (!load->isStart() || !isLeafResource(load->getResource())) continue;
      blocked[load->getResource()].push_back({plan->getStart(), plan->getEnd()});
    }
  }
  unordered_map<Resource*, vector<Date>> busy;
  sort(rows.begin(), rows.end(), [](const Placement& left, const Placement& right) {
    if (left.order != right.order) return left.order < right.order;
    return left.plan->getReference() < right.plan->getReference();
  });
  int placed = 0;
  int resourceMiss = 0;
  for (Placement& row : rows) {
    if (!row.plan || !row.plan->getOperation()) continue;
    const double quantity = row.plan->getQuantity();
    if (row.operation && row.plan->getOperation() != row.operation)
      row.plan->setOperation(row.operation);
    // The CP model already placed calendar, changeover, transfer batch,
    // material and inventory capacity. Keep that start and end.
    Date start = origin + Duration(static_cast<long>(max(0L, row.order)));
    Date end = start + Duration(static_cast<long>(max(0, row.duration)));
    bool personnelOk = true;
    vector<pair<LoadPlan*, Resource*>> extras;
    if (!row.resources.empty()) {
      for (const CapacityUse& use : row.resources)
        for (auto load = row.plan->beginLoadPlans();
             load != row.plan->endLoadPlans(); ++load)
          if (load->isStart() && load->getLoad() == use.load)
            extras.push_back({&*load, use.resource});
    }
    for (int attempt = 0; row.resource && row.resources.empty() && attempt < 32; ++attempt) {
      personnelOk = true;
      extras.clear();
      bool conflict = false;
      if (parallelSlots(row.resource) <= 1) {
        for (const auto& window : blocked[row.resource]) {
          if (start < window.second && window.first < end) {
            start = max(start + Duration(1L), window.second);
            end = start + Duration(static_cast<long>(max(1, row.duration)));
            conflict = true;
            break;
          }
        }
      }
      if (conflict) continue;
      for (const Load& load : row.plan->getOperation()->getLoads()) {
        if (load.getAlternate() || !load.getResource()) continue;
        if (sameResourcePool(load.getResource(), row.resource)) continue;
        LoadPlan* loadPlan = nullptr;
        for (auto current = row.plan->beginLoadPlans();
             current != row.plan->endLoadPlans(); ++current) {
          if (current->isStart() && current->getLoad() == &load)
            loadPlan = &*current;
        }
        Resource* leaf =
            findQualifiedResource(load, start, end, busy, blocked);
        if (!leaf) {
          personnelOk = false;
          break;
        }
        if (loadPlan) extras.push_back({loadPlan, leaf});
      }
      if (!personnelOk) break;
      break;
    }
    for (auto load = row.plan->beginLoadPlans(); row.resources.empty() &&
         load != row.plan->endLoadPlans(); ++load) {
      if (!load->isStart() || load->getQuantity() <= 0) continue;
      if (loadAcceptsChosen(load->getLoad(), load->getResource(), row.resource) &&
          load->getResource() != row.resource)
        load->setResource(row.resource, false, true);
    }
    for (const auto& extra : extras) {
      if (extra.first->getResource() != extra.second)
        extra.first->setResource(extra.second, false, true);
    }
    if (!personnelOk) ++resourceMiss;
    // Apply the solved dates exactly, without merging other selected lots.
    row.plan->setStartEndAndQuantity(start,end,quantity);
    occupyResource(busy, row.resource, end);
    for (const auto& extra : extras) occupyResource(busy, extra.second, end);
    ++placed;
  }
  // Move deliveries onto their receipts before measuring stock. Trimming
  // first deleted shipments whose manufacturing had moved past the window.
  alignDeliveryCommitments();
  if (partial) trimPartialDeliveries(origin + Duration(horizon));
  int materialShortage = 0;
  for (auto buffer = Buffer::begin(); buffer != Buffer::end(); ++buffer) {
    if (!buffer->getDetectProblems() || !materialSupplyLimited(&*buffer)) continue;
    for (auto problem = buffer->getProblems(); problem != Problem::end();
         ++problem) {
      if (!problem->hasType<ProblemMaterialShortage>()) continue;
      if (materialShortage < 20)
        logger << "CP material shortage: " << problem->getDescription()
               << "\n";
      ++materialShortage;
    }
  }
  int inventoryExcess = 0;
  for (auto buffer = Buffer::begin(); buffer != Buffer::end(); ++buffer) {
    if (buffer->getMaximum() <= ROUNDING_ERROR) continue;
    for (auto flowplan = buffer->getFlowPlans().begin();
         flowplan != buffer->getFlowPlans().end(); ++flowplan) {
      if (flowplan->getOnhand() <= buffer->getMaximum() + ROUNDING_ERROR)
        continue;
      if (inventoryExcess < 20)
        logger << "CP inventory capacity: '" << buffer->getName()
               << "' onhand " << flowplan->getOnhand() << " above maximum "
               << buffer->getMaximum() << "\n";
      ++inventoryExcess;
      break;
    }
  }
  int capacityOverload = 0;
  for (auto& resource : Resource::all()) {
    if (!resource.getDetectProblems() || !resource.getConstrained()) continue;
    for (auto problem = resource.getProblems(); problem != Problem::end();
         ++problem) {
      if (!problem->hasType<ProblemCapacityOverload>()) continue;
      if (capacityOverload < 20)
        logger << "CP capacity overload: " << problem->getDescription()
               << "\n";
      ++capacityOverload;
    }
  }
  // All manufacturing work orders must carry a concrete result (start/end).
  int moTotal = 0;
  int moMissingResult = 0;
  for (auto plan = OperationPlan::begin(); plan != OperationPlan::end(); ++plan) {
    if (plan->getOrderType() != "MO") continue;
    if (plan->getQuantity() <= ROUNDING_ERROR) continue;
    ++moTotal;
    if (plan->getStart() == Date::infinitePast ||
        plan->getEnd() == Date::infinitePast ||
        plan->getEnd() == Date::infiniteFuture ||
        plan->getEnd() < plan->getStart()) {
      if (moMissingResult < 20)
        logger << "CP work-order missing result: " << plan->getReference()
               << " op="
               << (plan->getOperation() ? plan->getOperation()->getName() : string())
               << " qty=" << plan->getQuantity() << "\n";
      ++moMissingResult;
    }
  }
  logger << "CP-SAT placed " << placed
         << " operation plans on calendars\n";
  logger << "CP post-check material=" << materialShortage
         << " inventory=" << inventoryExcess
         << " capacity=" << capacityOverload
         << " resource=" << resourceMiss
         << " mo_missing_result=" << moMissingResult
         << "/" << moTotal << "\n";
  if (changeoverEnabled)
    logger << "CP changeover matrix is enforced in the CP model\n";
  clearResolvedConstraints(rows);
}

// Cap first-lot size so a demand/MO splits into at most maxParts pieces.
// Prefers economic batch as the unit when present; otherwise SNP. Mirrors
// ccAPPSdb/execute/planning_window.capped_split_shipment.
double cappedSplitShipment(double remaining, double snp, double economic,
                           int maxParts) {
  if (remaining <= ROUNDING_ERROR) return max(1.0, snp);
  maxParts = max(1, maxParts);
  snp = max(1.0, snp);
  const double unit = economic > 1.0 ? economic : snp;
  if (maxParts <= 1 || remaining <= unit + ROUNDING_ERROR) return remaining;
  const double floorChunk = ceil(remaining / maxParts - 1e-9);
  double chunk = ceil(max(floorChunk, unit) / unit - 1e-9) * unit;
  chunk = min(chunk, remaining);
  const double tail = remaining - chunk;
  if (tail > ROUNDING_ERROR && tail < snp - ROUNDING_ERROR) return remaining;
  return chunk;
}

bool splitIsWorthwhile(double remaining, double snp, double economic) {
  if (remaining <= ROUNDING_ERROR) return false;
  snp = max(1.0, snp);
  const double unit = economic > 1.0 ? economic : snp;
  return remaining > 2.0 * unit + ROUNDING_ERROR;
}

// Processing content of a manufacturing tree (sum of leaf timed ops).
long jobProcessingSeconds(OperationPlan* root) {
  if (!root) return 0;
  long total = 0;
  bool any = false;
  vector<OperationPlan*> stack{root};
  while (!stack.empty()) {
    OperationPlan* node = stack.back();
    stack.pop_back();
    Operation* op = node->getOperation();
    if (op && op->hasType<OperationTimePer, OperationFixedTime>() &&
        !op->hasType<OperationRouting, OperationAlternate, OperationSplit>() &&
        !op->hasType<OperationItemSupplier, OperationItemDistribution>()) {
      total += durationSeconds(node, op);
      any = true;
    }
    for (OperationPlan::iterator child(node); child != node->end(); ++child)
      stack.push_back(&*child);
  }
  return any ? total : durationSeconds(root);
}

Operation* policyOperationOf(OperationPlan* root) {
  if (!root || !root->getOperation()) return nullptr;
  Operation* op = root->getOperation();
  if (op->hasType<OperationAlternate, OperationRouting>()) {
    Operation* first = nullptr;
    for (OperationPlan::iterator child(root); child != root->end(); ++child) {
      Operation* sub = child->getOperation();
      if (!sub) continue;
      if (!first) first = sub;
      if (sub->getBoolProperty("fc_batch_policy", false)) return sub;
    }
    if (first) return first;
  }
  return op;
}

// Resize without FC economic snap-to-one-batch (which would collapse splits).
void forceLotQuantity(OperationPlan* plan, double qty) {
  if (!plan || !plan->getOperation() || qty <= ROUNDING_ERROR) return;
  vector<pair<Operation*, bool>> saved;
  vector<OperationPlan*> stack{plan};
  while (!stack.empty()) {
    OperationPlan* node = stack.back();
    stack.pop_back();
    Operation* op = node->getOperation();
    if (op && op->getBoolProperty("fc_batch_policy", false)) {
      saved.emplace_back(op, true);
      op->setBoolProperty("fc_batch_policy", false);
    }
    for (OperationPlan::iterator child(node); child != node->end(); ++child)
      stack.push_back(&*child);
  }
  plan->setQuantity(qty, false, true, true);
  for (auto& entry : saved) entry.first->setBoolProperty("fc_batch_policy", true);
}

void cloneSplitChildren(OperationPlan* source, OperationPlan* target,
                        double ratio) {
  if (!source || !target) return;
  // Some operation types instantiate their children automatically. In that
  // case resizing the root already propagated the split quantity.
  if (target->getSubOperationPlans(false) != OperationPlan::end()) return;
  for (OperationPlan::iterator child(source); child != source->end(); ++child) {
    Operation* operation = child->getOperation();
    if (!operation) continue;
    const double qty = max(ROUNDING_ERROR, child->getQuantity() * ratio);
    // No flows/loads: XOR head/tail stay off the live buffer/resource
    // timelines until CP exports a choice (see prepareCpXorSplits).
    OperationPlan* copy = operation->createOperationPlan(
        qty, child->getStart(), child->getEnd(), target->getBatch(),
        target->getDemand(), target, false, false);
    if (!copy) continue;
    if (fabs(copy->getQuantity() - qty) > ROUNDING_ERROR)
      forceLotQuantity(copy, qty);
    cloneSplitChildren(&*child, copy, ratio);
  }
}

Demand* pegDemandOf(OperationPlan* plan) {
  for (OperationPlan* node = plan; node; node = node->getOwner())
    if (node->getDemand()) return node->getDemand();
  return nullptr;
}

unordered_set<string> cpLocalRequiredDemandNames() {
  unordered_set<string> result;
  for (const char* key : {"LOCAL_NEW_DEMANDS", "CP_LOCAL_NEW_DEMANDS"}) {
    const char* raw = getenv(key);
    if (!raw || !*raw) continue;
    string token;
    for (size_t i = 0; i <= strlen(raw); ++i) {
      const char ch = raw[i];
      if (ch == ',' || ch == ';' || ch == '|' || ch == '\n' || ch == '\0') {
        size_t begin = 0, end = token.size();
        while (begin < end && isspace(static_cast<unsigned char>(token[begin])))
          ++begin;
        while (end > begin &&
               isspace(static_cast<unsigned char>(token[end - 1])))
          --end;
        if (begin < end) result.insert(token.substr(begin, end - begin));
        token.clear();
      } else {
        token.push_back(ch);
      }
    }
    if (!result.empty()) break;
  }
  return result;
}

// XOR only on 需求单 manufacturing lots — not buffer replenish or sync-group
// co-produce trees (those have no/weak demand peg and corrupt prepare at scale).
bool isDemandOrderLot(OperationPlan* whole) {
  if (!whole || !whole->getOperation()) return false;
  Demand* demand = pegDemandOf(whole);
  if (!demand) return false;
  const string& opName = whole->getOperation()->getName();
  if (opName.compare(0, 10, "Replenish ") == 0) return false;
  if (opName.compare(0, 11, "FC_OP_SYNC_") == 0) return false;
  return true;
}

// Model-side optional split: still one cut, but several cut-points.
// CP picks exactly one of {whole, cut0, cut1, ...}; each cut is head+tail.
struct CpXorCut {
  OperationPlan* head = nullptr;
  OperationPlan* tail = nullptr;
  double chunk = 0.0;
};
struct CpXorSplit {
  OperationPlan* whole = nullptr;
  vector<CpXorCut> cuts;
};
vector<CpXorSplit> g_cpXorSplits;

// Diverse first-piece sizes for a single cut. A lot belongs to exactly one
// rule (priority is decided by the caller). Generate a reasonably rich pool,
// then retain evenly spaced points across the head/tail ratio range.
vector<double> enumerateXorCutChunks(double qty, double snp, double economic,
                                     double preferredChunk, bool economicRule,
                                     bool snpRule, bool exactLongRule,
                                     size_t maxCuts) {
  struct Proposal { double chunk; unsigned sources; };
  vector<Proposal> proposals;
  if (qty <= 1.0 + ROUNDING_ERROR || maxCuts == 0) return {};
  snp = max(1.0, snp);
  auto consider = [&](double chunk, unsigned source) {
    if (chunk + ROUNDING_ERROR >= qty || chunk <= ROUNDING_ERROR) return;
    const double tail = qty - chunk;
    if (tail <= ROUNDING_ERROR || chunk + ROUNDING_ERROR < snp) return;
    if (tail + ROUNDING_ERROR < snp) return;
    for (Proposal& existing : proposals)
      if (fabs(existing.chunk - chunk) <= ROUNDING_ERROR) {
        existing.sources |= source;
        return;
      }
    proposals.push_back({chunk, source});
  };
  // Build the real grid of feasible cuts for the selected rule.
  const double unit = economicRule ? max(1.0, economic) : snp;
  const unsigned source = economicRule ? 1u : (snpRule ? 2u : 4u);
  // Reject tiny heads/tails (e.g. 1-piece cuts when SNP defaults to 1).
  // Both pieces must be at least max(unit, 20% of qty).
  const double minPiece = max(unit, qty * 0.20);
  // No real batch policy (unit collapses to 1): only the preferred one-cut.
  // Enumerating 1..qty-1 flooded CP with junk cuts and wrecked material.
  if (unit <= 1.0 + ROUNDING_ERROR && !economicRule) {
    if (preferredChunk + ROUNDING_ERROR >= minPiece &&
        qty - preferredChunk + ROUNDING_ERROR >= minPiece)
      return {preferredChunk};
    return {};
  }
  const long long first = max(
      1LL, static_cast<long long>(ceil(minPiece / unit - 1e-9)));
  const long long last = static_cast<long long>(
      floor((qty - minPiece) / unit + 1e-9));
  const long long count = max(0LL, last - first + 1);
  if (count <= 10000) {
    for (long long multiple = first; multiple <= last; ++multiple)
      consider(multiple * unit, source);
  } else {
    for (int i = 0; i < 101; ++i) {
      const long long multiple = first +
          static_cast<long long>(llround(static_cast<double>(i) *
                                         (count - 1) / 100.0));
      consider(multiple * unit, source);
    }
  }
  if (preferredChunk > ROUNDING_ERROR)
    consider(preferredChunk, source);
  if (proposals.empty()) {
    if (preferredChunk + ROUNDING_ERROR >= minPiece &&
        qty - preferredChunk + ROUNDING_ERROR >= minPiece)
      return {preferredChunk};
    return {};
  }

  sort(proposals.begin(), proposals.end(),
       [](const Proposal& a, const Proposal& b) { return a.chunk < b.chunk; });
  vector<Proposal> selected;
  const size_t keep = min(maxCuts, proposals.size());
  if (keep == 1) {
    selected.push_back(proposals[proposals.size() / 2]);
  } else {
    // Quantiles over the actual candidate list. If there are e.g. 10 valid
    // points, retain 5 spread across the list; if there are <=5, keep all.
    for (size_t i = 0; i < keep; ++i) {
      const size_t index = static_cast<size_t>(llround(
          static_cast<double>(i) * (proposals.size() - 1) / (keep - 1)));
      selected.push_back(proposals[index]);
    }
  }
  vector<double> chunks;
  for (const Proposal& proposal : selected) chunks.push_back(proposal.chunk);
  sort(chunks.begin(), chunks.end());
  return chunks;
}

string cpSplitRole(OperationPlan* plan) {
  if (!plan) return "";
  for (OperationPlan* node = plan; node; node = node->getOwner()) {
    const string role = node->getStringProperty("cp_split_role", "");
    if (!role.empty()) return role;
  }
  return "";
}

OperationPlan* cpChoiceRoot(OperationPlan* plan) {
  if (!plan) return nullptr;
  for (OperationPlan* node = plan; node; node = node->getOwner())
    if (!node->getStringProperty("cp_split_role", "").empty()) return node;
  return plan->getTopOwner();
}

bool cpIsXorSplitRoot(OperationPlan* plan) {
  const string role = cpSplitRole(plan);
  return role == "whole" || role == "head" || role == "tail";
}

bool cpIsXorSplitPart(OperationPlan* plan) {
  const string role = cpSplitRole(plan);
  return role == "head" || role == "tail";
}

bool opPlanTreeHasExternalRefs(OperationPlan* plan) {
  if (!plan) return false;
  vector<OperationPlan*> stack{plan};
  while (!stack.empty()) {
    OperationPlan* node = stack.back();
    stack.pop_back();
    if (static_cast<PyObject*>(static_cast<Object*>(node))->ob_refcnt > 1)
      return true;
    for (OperationPlan::iterator child(node); child != node->end(); ++child)
      stack.push_back(&*child);
  }
  return false;
}

void demoteOpPlanTree(OperationPlan* plan) {
  if (!plan) return;
  vector<OperationPlan*> stack{plan};
  while (!stack.empty()) {
    OperationPlan* node = stack.back();
    stack.pop_back();
    try {
      if (node->getApproved()) node->setProposed(true);
      node->setQuantity(0.0);
    } catch (...) {
    }
    for (OperationPlan::iterator child(node); child != node->end(); ++child)
      stack.push_back(&*child);
  }
}

void localCascadeSnapshot() {
  g_localCascadeSnapshot.clear();
  g_localCascadePlans.clear();
  for (auto it = OperationPlan::begin(); it != OperationPlan::end(); ++it) {
    OperationPlan* plan = &*it;
    LocalCascadePlanState state;
    state.plan = plan;
    state.operation = plan->getOperation();
    state.start = plan->getStart();
    state.end = plan->getEnd();
    state.quantity = plan->getQuantity();
    state.status = plan->getStatus();
    for (auto load = plan->beginLoadPlans(); load != plan->endLoadPlans();
         ++load)
      if (load->isStart())
        state.loads.push_back({load->getLoad(), load->getResource()});
    g_localCascadePlans.insert(plan);
    g_localCascadeSnapshot.push_back(std::move(state));
  }
  setenv("CP_LOCAL_CASCADE_ACTIVE", "1", 1);
  logger << "CP local cascade snapshot: " << g_localCascadeSnapshot.size()
         << " operationplans\n" << flush;
}

void localCascadeRestore() {
  if (g_localCascadeSnapshot.empty()) return;
  vector<OperationPlan*> extras;
  for (auto it = OperationPlan::begin(); it != OperationPlan::end(); ++it)
    if (!g_localCascadePlans.count(&*it)) extras.push_back(&*it);
  for (OperationPlan* plan : extras) demoteOpPlanTree(plan);

  size_t warnings = 0;
  for (LocalCascadePlanState& state : g_localCascadeSnapshot) {
    OperationPlan* plan = state.plan;
    if (!plan) continue;
    try {
      if (plan->getOperation() != state.operation)
        plan->setOperation(state.operation);
      plan->setStatusRaw(state.status);
      plan->setQuantity(state.quantity, false, true, true);
      for (const LocalCascadeLoadState& saved : state.loads) {
        if (!saved.resource) continue;
        for (auto load = plan->beginLoadPlans(); load != plan->endLoadPlans();
             ++load) {
          if (!load->isStart() || load->getLoad() != saved.load) continue;
          if (load->getResource() != saved.resource)
            load->setResource(saved.resource, false, true);
          break;
        }
      }
      // Resource reassignment can recalculate dates. Dates are restored last.
      plan->setStartEndAndQuantity(state.start, state.end, state.quantity);
      plan->setStatusRaw(state.status);
    } catch (...) {
      ++warnings;
    }
  }
  cpSetLastRunApplied(0);
  localSetLastOk(0);
  localSetStage("cascade_restored");
  logger << "CP local cascade restored: " << g_localCascadeSnapshot.size()
         << " operationplans, neutralized_extras=" << extras.size()
         << " warnings=" << warnings << "\n" << flush;
}

void localCascadeSnapshotClear() {
  g_localCascadeSnapshot.clear();
  g_localCascadePlans.clear();
  unsetenv("CP_LOCAL_CASCADE_ACTIVE");
}

void tryDeleteOpPlan(OperationPlan* plan) {
  if (!plan) return;
  // Candidate expansion / Python export may still hold a PyObject ref on the
  // root or a descendant. Hard-deleting then corrupts the interpreter on
  // process teardown (free(): corrupted / SIGABRT, runplan exit=-6).
  if (envInt("CP_LOCAL_CASCADE_ACTIVE", 0) != 0 ||
      opPlanTreeHasExternalRefs(plan)) {
    demoteOpPlanTree(plan);
    return;
  }
  try {
    CommandDeleteOperationPlan remove(plan);
    remove.commit();
  } catch (const DataException&) {
  } catch (...) {
  }
}

// Drop all cut alts when CP did not choose a split (or failed to export).
void abandonUnchosenXorSplits() {
  size_t droppedCuts = 0;
  for (CpXorSplit& alt : g_cpXorSplits) {
    if (!alt.whole) continue;
    if (cpSplitRole(alt.whole) != "whole") continue;
    for (CpXorCut& cut : alt.cuts) {
      tryDeleteOpPlan(cut.head);
      tryDeleteOpPlan(cut.tail);
      cut.head = cut.tail = nullptr;
      ++droppedCuts;
    }
    alt.cuts.clear();
    alt.whole->deleteProperty("cp_split_role");
    alt.whole->deleteProperty("cp_split_pair");
    alt.whole->deleteProperty("cp_lot_split_done");
  }
  if (droppedCuts)
    logger << "CP XOR split: abandoned " << droppedCuts
           << " unused cut pairs (kept whole)\n";
  g_cpXorSplits.clear();
}

void exportXorSplitChoices(const unordered_set<OperationPlan*>& chosen,
                           const unordered_set<OperationPlan*>& modelRoots) {
  size_t keptWhole = 0;
  size_t keptParts = 0;
  vector<CpXorSplit> remain;
  remain.reserve(g_cpXorSplits.size());
  size_t altIndex = 0;
  for (CpXorSplit& alt : g_cpXorSplits) {
    logger << "CP XOR export alt=" << altIndex++
           << " whole=" << (alt.whole ? alt.whole->getReference() : string("-"))
           << " cuts=" << alt.cuts.size() << "\n"
           << flush;
    bool inModel = alt.whole && modelRoots.count(alt.whole);
    for (const CpXorCut& cut : alt.cuts) {
      if ((cut.head && modelRoots.count(cut.head)) ||
          (cut.tail && modelRoots.count(cut.tail)))
        inModel = true;
    }
    if (!inModel) {
      remain.push_back(alt);
      continue;
    }
    const bool wholeOn = alt.whole && chosen.count(alt.whole);
    int chosenCut = -1;
    for (size_t i = 0; i < alt.cuts.size(); ++i) {
      if (alt.cuts[i].head && chosen.count(alt.cuts[i].head)) {
        chosenCut = static_cast<int>(i);
        break;
      }
    }
    logger << "CP XOR export decide wholeOn=" << wholeOn
           << " chosenCut=" << chosenCut << "\n"
           << flush;
    // A chosen cut must be activated to become the exported plan. Setting the
    // diagnostic override to 0 keeps the FAS whole instead.
    if (chosenCut >= 0 && !wholeOn &&
        envInt("CP_XOR_ACTIVATE_CUTS", 1) == 0) {
      logger << "CP XOR export: skip cut activate (CP_XOR_ACTIVATE_CUTS=0); "
                "keep whole and drop cuts\n"
             << flush;
      for (CpXorCut& cut : alt.cuts) {
        tryDeleteOpPlan(cut.head);
        tryDeleteOpPlan(cut.tail);
        cut.head = cut.tail = nullptr;
      }
      alt.cuts.clear();
      if (alt.whole) {
        alt.whole->deleteProperty("cp_split_role");
        alt.whole->deleteProperty("cp_split_pair");
      }
      ++keptWhole;
      continue;
    }
    if (chosenCut >= 0 && !wholeOn) {
      // Alternatives are created without demand/flows so they stay off the
      // live buffer timeline during search.  When a cut wins we must:
      //   1) activate head+tail (inserts into plan + createFlowLoads)
      //   2) transfer the demand so planned qty = head+tail
      //   3) only then delete the FAS whole
      // Skipping activate left a hole in material production → shortages
      // and under-planned demands.
      Demand* demand = pegDemandOf(alt.whole);
      OperationPlan* keepHead = alt.cuts[chosenCut].head;
      OperationPlan* keepTail = alt.cuts[chosenCut].tail;
      // activate() may delete `this` and return false — treat as nullptr.
      auto tryActivate = [](OperationPlan*& part) -> bool {
        if (!part) return false;
        if (part->getActivated()) return true;
        // Children were already cloned; do not re-instantiate routing steps.
        if (!part->activate(false, true)) {
          part = nullptr;
          return false;
        }
        return true;
      };
      const bool headOk = tryActivate(keepHead);
      const bool tailOk = tryActivate(keepTail);
      alt.cuts[chosenCut].head = keepHead;
      alt.cuts[chosenCut].tail = keepTail;
      if (!headOk || !tailOk) {
        // Activation failed — keep the FAS whole and drop all cuts.
        if (keepHead) tryDeleteOpPlan(keepHead);
        if (keepTail) tryDeleteOpPlan(keepTail);
        alt.cuts[chosenCut].head = alt.cuts[chosenCut].tail = nullptr;
        for (size_t i = 0; i < alt.cuts.size(); ++i) {
          if (static_cast<int>(i) == chosenCut) continue;
          tryDeleteOpPlan(alt.cuts[i].head);
          tryDeleteOpPlan(alt.cuts[i].tail);
        }
        if (alt.whole) {
          alt.whole->deleteProperty("cp_split_role");
          alt.whole->deleteProperty("cp_split_pair");
        }
        ++keptWhole;
        continue;
      }
      if (demand) {
        keepHead->setDemand(demand);
        keepTail->setDemand(demand);
      }
      keepHead->deleteProperty("cp_split_role");
      keepTail->deleteProperty("cp_split_role");
      tryDeleteOpPlan(alt.whole);
      for (size_t i = 0; i < alt.cuts.size(); ++i) {
        if (static_cast<int>(i) == chosenCut) continue;
        tryDeleteOpPlan(alt.cuts[i].head);
        tryDeleteOpPlan(alt.cuts[i].tail);
      }
      ++keptParts;
    } else {
      for (CpXorCut& cut : alt.cuts) {
        tryDeleteOpPlan(cut.head);
        tryDeleteOpPlan(cut.tail);
      }
      if (alt.whole) {
        alt.whole->deleteProperty("cp_split_role");
        alt.whole->deleteProperty("cp_split_pair");
      }
      ++keptWhole;
    }
  }
  g_cpXorSplits.swap(remain);
  if (keptWhole || keptParts)
    logger << "CP XOR split export: kept_whole=" << keptWhole
           << " kept_parts=" << keptParts
           << " pending_groups=" << g_cpXorSplits.size() << "\n";
}

/* Prepare optional one-cut alternatives for CP.
 *
 * Still only one cut per lot, but several cut-points on the economic/SNP
 * grid: CP chooses whole XOR cut0 XOR cut1 XOR ... (exactly one).
 *
 * Eligibility:
 *   - demand-order lots only (not Replenish / SYNC)
 *   - economic batch > 1, or SNP >= 1 (raw SNP == 0 → no SNP split)
 *   - or 备件/断点 with processing time > exact_split_over_hours
 */
size_t prepareCpXorSplits() {
  // Single switch: common_parameter cp.lot_split → env CP_LOT_SPLIT (default 0).
  if (envInt("CP_LOT_SPLIT", 0) == 0) {
    g_cpXorSplits.clear();
    logger << "CP XOR split: off (cp.lot_split=0)\n" << flush;
    return 0;
  }
  // Idempotent across partitioned group calls.
  if (!g_cpXorSplits.empty()) return g_cpXorSplits.size();

  logger << "CP XOR split prepare: begin (cp.lot_split=1)\n" << flush;
  const int maxParts = 2;  // one cut → two pieces
  // Fixed eligibility (not separate knobs): exact 备件/断点 >8h; economic/SNP.
  const long exactThreshold = 8L * 3600L;
  const bool useEconomic = true;
  // Keep the XOR model compact: an environment override may reduce this
  // number for diagnostics, but can never expand an order beyond five cuts.
  const size_t maxCuts = static_cast<size_t>(
      min(5, max(1, envInt("CP_LOT_SPLIT_MAX_CUTS", 5))));
  // When partitioned, only prepare XOR alts for the active workshop group.
  const int partitionGroup = envInt("CP_PARTITION_GROUP", 0);

  unordered_set<OperationPlan*> seen;
  vector<OperationPlan*> roots;
  for (auto plan = OperationPlan::begin(); plan != OperationPlan::end();
       ++plan) {
    if (plan->getQuantity() <= ROUNDING_ERROR) continue;
    if (plan->getConfirmed() || plan->getCompleted() || plan->getClosed())
      continue;
    if (!plan->getProposed() && !plan->getApproved()) continue;
    OperationPlan* root = jobPlan(&*plan);
    if (!root || !seen.insert(root).second) continue;
    if (cpIsXorSplitRoot(root)) continue;
    if (root->getBoolProperty("cp_lot_split_done", false)) continue;
    if (!root->getOperation()) continue;
    if (root->getOperation()->hasType<OperationItemSupplier,
                                      OperationItemDistribution>())
      continue;
    if (partitionGroup > 0 && workshopGroupOf(root) != partitionGroup) continue;
    roots.push_back(root);
  }
  logger << "CP XOR split prepare: candidates=" << roots.size()
         << " group=" << partitionGroup << "\n"
         << flush;

  // Cap = how many lots get one head/tail split each (not splits-per-lot).
  // Prefer longest processing first. Env override for experiments.
  const size_t maxAlts = static_cast<size_t>(max(1, envInt("CP_LOT_SPLIT_MAX_ALTS", 20)));

  struct SplitCand {
    OperationPlan* whole = nullptr;
    Demand* demand = nullptr;
    long processing = 0;
    double qty = 0.0;
    double preferredChunk = 0.0;
    double splitSnp = 1.0;
    double splitEconomic = 0.0;
    bool economicRule = false;
    bool snpRule = false;
    bool exactLongRule = false;
  };
  vector<SplitCand> eligible;
  size_t skippedSnpZero = 0;
  size_t skippedNoDemand = 0;
  for (OperationPlan* whole : roots) {
    // 只拆需求单：one XOR lot per open demand manufacturing root.
    if (!isDemandOrderLot(whole)) {
      ++skippedNoDemand;
      whole->setBoolProperty("cp_lot_split_done", true);
      continue;
    }
    Demand* demand = pegDemandOf(whole);
    Operation* policyOp = policyOperationOf(whole);
    if (!policyOp) continue;
    const bool exact = policyOp->getBoolProperty("fc_exact_quantity", false);
    const double economic =
        useEconomic ? policyOp->getDoubleProperty("fc_economic_batch", 0.0)
                    : 0.0;
    double rawSnp = policyOp->getDoubleProperty("fc_snp", 0.0);
    if (!policyOp->getBoolProperty("fc_batch_policy", false) &&
        policyOp->getSizeMultiple() > ROUNDING_ERROR)
      rawSnp = policyOp->getSizeMultiple();

    const double qty = whole->getQuantity();
    const long processing = jobProcessingSeconds(whole);
    double splitEconomic = 0.0;
    double splitSnp = 1.0;
    const bool economicEligible =
        economic > 1.0 + ROUNDING_ERROR &&
        splitIsWorthwhile(qty, max(1.0, rawSnp), economic);
    // SNP>=1 may split. Unit-1 grids only keep the preferred half-cut in
    // enumerateXorCutChunks (no 1 / qty-1 junk points).
    const bool snpEligible =
        rawSnp >= 1.0 - ROUNDING_ERROR &&
        splitIsWorthwhile(qty, rawSnp, 0.0);
    const bool exactLongEligible =
        exact && processing > exactThreshold && qty > 1.0 + ROUNDING_ERROR;
    // A lot has one and only one split rule. Priority:
    // long exact-quantity 备件/断点 > economic batch > SNP.
    const bool exactLongRule = exactLongEligible;
    const bool economicRule = !exactLongRule && economicEligible;
    const bool snpRule = !exactLongRule && !economicRule && snpEligible;
    const bool maySplit = exactLongRule || economicRule || snpRule;
    if (economicRule) {
      splitEconomic = economic;
      splitSnp = rawSnp >= 1.0 - ROUNDING_ERROR ? rawSnp : 1.0;
    }
    else if (snpRule) {
      splitSnp = rawSnp;
    }
    else if (exactLongRule) {
      splitSnp = rawSnp >= 1.0 - ROUNDING_ERROR ? rawSnp : 1.0;
    }
    if (!maySplit && rawSnp < 1.0 - ROUNDING_ERROR && !exact)
      ++skippedSnpZero;
    if (!maySplit) {
      whole->setBoolProperty("cp_lot_split_done", true);
      continue;
    }
    const double chunk =
        cappedSplitShipment(qty, splitSnp, splitEconomic, maxParts);
    if (chunk + ROUNDING_ERROR >= qty) {
      whole->setBoolProperty("cp_lot_split_done", true);
      continue;
    }
    const double tailQty = qty - chunk;
    if (tailQty <= ROUNDING_ERROR) {
      whole->setBoolProperty("cp_lot_split_done", true);
      continue;
    }
    if (!whole->getOperation()) continue;
    eligible.push_back({whole, demand, processing, qty, chunk, splitSnp,
                        splitEconomic, economicRule, snpRule, exactLongRule});
  }
  sort(eligible.begin(), eligible.end(),
       [](const SplitCand& a, const SplitCand& b) {
         if (a.processing != b.processing) return a.processing > b.processing;
         return a.qty > b.qty;
       });
  // At most one split lot per demand (keep the longest).
  {
    unordered_set<Demand*> seenDemand;
    vector<SplitCand> unique;
    unique.reserve(eligible.size());
    for (const SplitCand& cand : eligible) {
      if (!seenDemand.insert(cand.demand).second) {
        cand.whole->setBoolProperty("cp_lot_split_done", true);
        continue;
      }
      unique.push_back(cand);
    }
    eligible.swap(unique);
  }
  if (eligible.size() > maxAlts) {
    for (size_t i = maxAlts; i < eligible.size(); ++i)
      eligible[i].whole->setBoolProperty("cp_lot_split_done", true);
    logger << "CP XOR split prepare: eligible=" << eligible.size()
           << " capped_to=" << maxAlts << " lots (longest first)\n"
           << flush;
    eligible.resize(maxAlts);
  } else {
    logger << "CP XOR split prepare: eligible_lots=" << eligible.size()
           << " max=" << maxAlts << " no_demand_skip=" << skippedNoDemand
           << "\n"
           << flush;
  }
  for (const SplitCand& cand : eligible) {
    logger << "CP XOR split eligible: demand='"
           << (cand.demand ? cand.demand->getName() : string("-"))
           << "' operation='"
           << (cand.whole && cand.whole->getOperation()
                   ? cand.whole->getOperation()->getName()
                   : string("-"))
           << "' qty=" << cand.qty
           << " processing_h=" << (static_cast<double>(cand.processing) / 3600.0)
           << " snp=" << cand.splitSnp
           << " economic=" << cand.splitEconomic
           << " preferred_chunk=" << cand.preferredChunk << "\n";
  }

  size_t createdLots = 0;
  size_t createdCuts = 0;
  for (const SplitCand& cand : eligible) {
    OperationPlan* whole = cand.whole;
    const double qty = cand.qty;
    Operation* rootOp = whole->getOperation();
    if (!rootOp) continue;
    const vector<double> cutChunks = enumerateXorCutChunks(
        qty, cand.splitSnp, cand.splitEconomic, cand.preferredChunk,
        cand.economicRule, cand.snpRule, cand.exactLongRule, maxCuts);
    if (!cutChunks.empty()) {
      logger << "CP XOR split cuts: demand='"
             << (cand.demand ? cand.demand->getName() : string("-"))
             << "' rule=" << (cand.exactLongRule ? "exact-long"
                                  : cand.economicRule ? "economic" : "snp")
             << " head_qty=";
      for (size_t i = 0; i < cutChunks.size(); ++i) {
        if (i) logger << ",";
        logger << cutChunks[i];
      }
      logger << "\n";
    }
    if (cutChunks.empty()) {
      whole->setBoolProperty("cp_lot_split_done", true);
      continue;
    }
    const PooledString batch = whole->getBatch();
    const string lotId = string("cp_xor_") + whole->getReference();
    CpXorSplit lot;
    lot.whole = whole;

    auto markAlternative = [&](OperationPlan* part, const string& pairId) {
      // Optional CP alternative: stay proposed; no demand / no live flows.
      part->setStatus("proposed");
      for (OperationPlan::iterator child(part); child != part->end(); ++child)
        child->setStatus("proposed");
      part->setBoolProperty("cp_lot_split_done", true);
      part->setStringProperty("cp_split_pair", pairId);
    };

    for (double chunk : cutChunks) {
      const double tailQty = qty - chunk;
      Date cursor = whole->getStart() != Date::infinitePast
                        ? whole->getStart()
                        : Plan::instance().getCurrent();
      const string pairId =
          lotId + "_c" + to_string(static_cast<long long>(llround(chunk)));

      OperationPlan* head = rootOp->createOperationPlan(
          chunk, cursor, Date::infinitePast, batch, nullptr, nullptr, false,
          false);
      if (!head) continue;
      if (fabs(head->getQuantity() - chunk) > ROUNDING_ERROR)
        forceLotQuantity(head, chunk);
      cloneSplitChildren(whole, head, chunk / qty);
      rootOp->setOperationPlanParameters(head, chunk, cursor,
                                         Date::infinitePast, false, true,
                                         false);
      if (head->getEnd() != Date::infinitePast &&
          head->getEnd() != Date::infiniteFuture)
        cursor = head->getEnd();

      OperationPlan* tail = rootOp->createOperationPlan(
          tailQty, cursor, Date::infinitePast, batch, nullptr, nullptr, false,
          false);
      if (!tail) {
        tryDeleteOpPlan(head);
        continue;
      }
      if (fabs(tail->getQuantity() - tailQty) > ROUNDING_ERROR)
        forceLotQuantity(tail, tailQty);
      cloneSplitChildren(whole, tail, tailQty / qty);
      rootOp->setOperationPlanParameters(tail, tailQty, cursor,
                                         Date::infinitePast, false, true,
                                         false);

      markAlternative(head, pairId);
      head->setStringProperty("cp_split_role", "head");
      markAlternative(tail, pairId);
      tail->setStringProperty("cp_split_role", "tail");
      lot.cuts.push_back({head, tail, chunk});
      ++createdCuts;
    }

    if (lot.cuts.empty()) {
      whole->setBoolProperty("cp_lot_split_done", true);
      continue;
    }
    whole->setBoolProperty("cp_lot_split_done", true);
    whole->setStringProperty("cp_split_pair", lotId);
    whole->setStringProperty("cp_split_role", "whole");
    g_cpXorSplits.push_back(std::move(lot));
    ++createdLots;
  }
  if (createdLots || skippedSnpZero || skippedNoDemand)
    logger << "CP XOR split prepare: " << createdLots
           << " lots, " << createdCuts << " cut-points (one-cut each), max_lots="
           << maxAlts << " max_cuts=" << maxCuts << ", exact>"
           << (exactThreshold / 3600) << "h, snp0_skip=" << skippedSnpZero
           << ", no_demand_skip=" << skippedNoDemand << "\n"
           << flush;
  return createdLots;
}

// Back-compat name used by run_cp.
size_t applyCpLotSplits() { return prepareCpXorSplits(); }

#ifdef HAVE_ORTOOLS

enum class CpGoal {
  Makespan,
  Load,
  Tardiness,
  EarlyPersonnel,
  LexTardyEarly,
  Weighted,
  Throughput
};

const char* cpGoalName(CpGoal goal) {
  switch (goal) {
    case CpGoal::Throughput: return "throughput";
    case CpGoal::Load: return "load";
    case CpGoal::Tardiness: return "tardiness";
    case CpGoal::EarlyPersonnel: return "early_personnel";
    case CpGoal::LexTardyEarly: return "lex_tardy_early";
    case CpGoal::Weighted: return "weighted";
    default: return "makespan";
  }
}

string cpObjectiveMethod() {
  string method = envString("CP_OBJECTIVE_METHOD", "");
  for (char& ch : method) ch = static_cast<char>(tolower(static_cast<unsigned char>(ch)));
  if (method == "lex" || method == "hierarchical" || method == "层次" ||
      method == "层次序列" || method == "层次序列法")
    return "lexicographic";
  if (method == "eps" || method == "epsilon" || method == "ε" ||
      method == "约束" || method == "ε-约束法" || method == "ε约束法")
    return "epsilon";
  if (method == "weight" || method == "加权" || method == "加权法")
    return "weighted";
  if (method == "lexicographic" || method == "epsilon" || method == "weighted")
    return method;
  return "weighted";
}

struct CpChoice {
  int task = -1;
  int candidate = -1;
  long start = 0;
  long duration = 0;
};

struct CpPoint {
  bool feasible = false;
  bool provenInfeasible = false;
  long makespan = 0;
  long maxLoad = 0;
  long tardiness = 0;
  long loadCap = -1;
  bool partial = false;
  long horizon = 0;
  long selectedJobs = 0;
  long baselineJobs = 0;
  long earlyPersonnel = 0;
  // Change vs FAS/MRP seed (改变率): root work orders with at least one
  // anchored leaf moved beyond the configured tolerance or dropped.
  long changeCompared = 0;
  long changeMoved = 0;
  long long changeAbsDev = 0;  // sum |Δstart| seconds over compared lots
  int baselineConflicts = 0;
  vector<CpChoice> choices;
};

struct CpAugment {
  long loadRange = 1;
  long tardyRange = 1;
};

// One customer demand. Several manufacturing lots can fill it; lateness is
// the latest lot, not the sum of every lot.
struct CpDemand {
  long due = 0;
  int weight = 1;
  vector<int> jobs;
  string name;
};

bool dominates(const CpPoint& left, const CpPoint& right) {
  const bool all =
      left.makespan <= right.makespan && left.maxLoad <= right.maxLoad &&
      left.tardiness <= right.tardiness;
  const bool strict =
      left.makespan < right.makespan || left.maxLoad < right.maxLoad ||
      left.tardiness < right.tardiness;
  return all && strict;
}

// Lex order: lower tardiness wins; then higher early-personnel; then
// lower start-time deviation from the FAS/MRP seed (改变率). Change is only
// compared when both points were scored against a seed (changeCompared>0);
// FAS baseline from scoreCommittedBaseline leaves changeCompared=0.
bool lexAcceptsOverBaseline(const CpPoint& candidate, const CpPoint& baseline) {
  if (!candidate.feasible) return false;
  if (candidate.tardiness < baseline.tardiness) return true;
  if (candidate.tardiness > baseline.tardiness) return false;
  if (candidate.earlyPersonnel != baseline.earlyPersonnel)
    return candidate.earlyPersonnel >= baseline.earlyPersonnel;
  if (baseline.changeCompared > 0 && candidate.changeCompared > 0) {
    if (candidate.changeAbsDev != baseline.changeAbsDev)
      return candidate.changeAbsDev <= baseline.changeAbsDev;
    return candidate.changeMoved <= baseline.changeMoved;
  }
  return true;
}

// Days used for early-personnel and (when scoped) window tardiness.
// Lex / early-personnel force at least 3 when unset. CP_TARDY_WINDOW_DAYS
// overrides the tardiness demand filter; <0 means "same as early days",
// 0 disables the filter (all-horizon tardiness).
long cpEarlyDays(bool forceDefaultThree) {
  long earlyDays = max(0, envInt("CP_EARLY_DAYS", 3));
  if (forceDefaultThree && earlyDays <= 0) earlyDays = 3;
  return earlyDays;
}

long cpTardyWindowSecs(long horizonSecs) {
  const int overrideDays = envInt("CP_TARDY_WINDOW_DAYS", -1);
  const long days = overrideDays < 0 ? cpEarlyDays(true)
                                     : static_cast<long>(overrideDays);
  if (days <= 0 || horizonSecs <= 0) return 0;
  return min(horizonSecs, days * 86400L);
}

// Score the committed FAS/MRP dates already on movable plans (before CP export).
// Tardiness matches the lex objective: only demands due inside the early
// window (unless CP_TARDY_WINDOW_DAYS=0 disables the filter).
CpPoint scoreCommittedBaseline(const vector<Task>& movable,
                               const unordered_map<int, vector<size_t>>& byJob,
                               const vector<CpDemand>& demands, long earlyDays) {
  CpPoint point;
  point.feasible = true;
  const Date origin = Plan::instance().getCurrent();
  const long earlyHorizon =
      earlyDays > 0 ? earlyDays * 86400L : 0;
  const int overrideDays = envInt("CP_TARDY_WINDOW_DAYS", -1);
  const long tardyHorizon =
      overrideDays == 0
          ? 0
          : (overrideDays > 0 ? overrideDays * 86400L : earlyHorizon);
  // throughput/window-select: a FAS finish past the early window cannot stay
  // in the CP model — score it like a miss so baseline compares fairly.
  const bool windowSelect =
      (envString("CP_PLANNING_MODE", "auto") == "throughput" &&
       g_cpXorSplits.empty()) ||
      envInt("CP_WINDOW_SELECT", 0) != 0;
  long long tardy = 0;
  for (const CpDemand& demand : demands) {
    if (tardyHorizon > 0 && demand.due > tardyHorizon) continue;
    long completion = -1;
    for (int job : demand.jobs) {
      auto found = byJob.find(job);
      if (found == byJob.end()) continue;
      for (size_t index : found->second) {
        OperationPlan* plan = movable[index].plan;
        if (!plan || plan->getEnd() == Date::infinitePast ||
            plan->getEnd() == Date::infiniteFuture)
          continue;
        if (plan->getEnd() < origin) continue;
        const long end =
            static_cast<long>((plan->getEnd() - origin).getSeconds());
        completion = completion < 0 ? end : max(completion, end);
      }
    }
    if (completion < 0) {
      if (windowSelect && earlyHorizon > 0)
        tardy += static_cast<long long>(
                     max(1L, earlyHorizon - max(0L, demand.due))) *
                 max(1, demand.weight);
      continue;
    }
    if (windowSelect && earlyHorizon > 0 && completion > earlyHorizon) {
      tardy += static_cast<long long>(
                   max(1L, earlyHorizon - max(0L, demand.due))) *
               max(1, demand.weight);
      continue;
    }
    tardy += static_cast<long long>(max(0L, completion - demand.due)) *
             max(1, demand.weight);
  }
  point.tardiness = static_cast<long>(min(
      tardy, static_cast<long long>(numeric_limits<long>::max())));
  long long early = 0;
  if (earlyHorizon > 0) {
    for (const Task& task : movable) {
      OperationPlan* plan = task.plan;
      if (!plan || plan->getEnd() == Date::infinitePast ||
          plan->getEnd() == Date::infiniteFuture || plan->getEnd() < origin)
        continue;
      const long end =
          static_cast<long>((plan->getEnd() - origin).getSeconds());
      if (end < 0 || end > earlyHorizon) continue;
      const long duration = max(
          0L, static_cast<long>((plan->getEnd() - plan->getStart()).getSeconds()));
      if (duration <= 0) continue;
      double personQty = 0.0;
      for (auto load = plan->beginLoadPlans(); load != plan->endLoadPlans();
           ++load) {
        if (!load->isStart() || load->getQuantity() <= 0) continue;
        Resource* resource = load->getResource();
        // Same scope as util: CP_EARLY_PERSONNEL_RESOURCE (default all 人员).
        if (!cpCountsForEarlyPersonnel(resource)) continue;
        personQty += load->getQuantity();
      }
      if (personQty <= 0.0) continue;
      // Load quantity is milli-persons (same as CapacityUse in CP).
      // Only lots finishing inside earlyHorizon (CP_EARLY_DAYS, default 3d).
      early += llround(static_cast<double>(duration) * personQty / 1000.0);
    }
  }
  point.earlyPersonnel = static_cast<long>(min(
      early, static_cast<long long>(numeric_limits<long>::max())));
  return point;
}

double frontHypervolume(const vector<CpPoint>& points, long refMakespan,
                        long refLoad) {
  vector<CpPoint> front;
  for (const CpPoint& point : points) {
    if (!point.feasible || point.tardiness > 0) continue;
    bool dominated = false;
    for (const CpPoint& other : points) {
      if (&other == &point || !other.feasible || other.tardiness > 0) continue;
      if (dominates(other, point)) dominated = true;
    }
    if (!dominated) front.push_back(point);
  }
  sort(front.begin(), front.end(), [](const CpPoint& left, const CpPoint& right) {
    if (left.makespan != right.makespan) return left.makespan < right.makespan;
    return left.maxLoad < right.maxLoad;
  });
  double volume = 0.0;
  for (size_t index = 0; index < front.size(); ++index) {
    const long next = index + 1 < front.size() ? front[index + 1].makespan
                                               : refMakespan;
    if (next <= front[index].makespan || refLoad <= front[index].maxLoad)
      continue;
    volume += static_cast<double>(next - front[index].makespan) *
              static_cast<double>(refLoad - front[index].maxLoad);
  }
  return volume;
}


// Pass through CP-SAT preflight response fields for hard-block diagnosis.
string formatCpSolverConflicts(
    const operations_research::sat::CpSolverResponse& response,
    const operations_research::sat::CpModelProto* modelForValidate = nullptr) {
  ostringstream out;
  out << "status=" << static_cast<int>(response.status())
      << " wall=" << response.wall_time()
      << "s num_conflicts=" << response.num_conflicts()
      << " num_branches=" << response.num_branches()
      << " num_binary_propagations=" << response.num_binary_propagations();
  if (!response.solution_info().empty())
    out << " solution_info='" << response.solution_info() << "'";
  if (response.sufficient_assumptions_for_infeasibility_size() > 0) {
    out << " sufficient_assumptions=[";
    for (int i = 0;
         i < response.sufficient_assumptions_for_infeasibility_size(); ++i) {
      if (i) out << ",";
      out << response.sufficient_assumptions_for_infeasibility(i);
    }
    out << "]";
  }
  if (response.status() ==
          operations_research::sat::CpSolverStatus::MODEL_INVALID &&
      modelForValidate) {
    const string validate =
        operations_research::sat::ValidateCpModel(*modelForValidate);
    if (!validate.empty()) out << " validate='" << validate << "'";
  }
  return out.str();
}

CpPoint solveCpPoint(const vector<Task>& movable,
                     const vector<tuple<int, long, long>>& occupancy,
                     const unordered_map<int, vector<size_t>>& byJob,
                     const vector<CpDemand>& demands, long horizon,
                     CpGoal goal, long loadCap,
                     long tardyCap, long makespanCap, int timeLimit,
                     const vector<CpChoice>* hint,
                     const CpAugment* augment = nullptr,
                     bool keepBaseline = false) {
  using operations_research::sat::BoolVar;
  using operations_research::sat::CpModelBuilder;
  using operations_research::sat::CpSolverResponse;
  using operations_research::sat::CpSolverStatus;
  using operations_research::sat::IntervalVar;
  using operations_research::sat::IntVar;
  using operations_research::sat::LinearExpr;
  using operations_research::sat::Model;
  using operations_research::sat::NewFeasibleSolutionObserver;
  using operations_research::sat::NewSatParameters;
  using operations_research::sat::SatParameters;
  using operations_research::sat::SolutionBooleanValue;
  using operations_research::sat::SolutionIntegerValue;
  using operations_research::sat::SolveCpModel;
  using operations_research::sat::SolveWithParameters;
  using operations_research::Domain;

  CpPoint point;
  point.loadCap = loadCap;
  // throughput mode (or CP_WINDOW_SELECT): lots are optional in the short
  // window. Lex/tardiness can use the same select switch so we only keep
  // trees that fit the early days instead of forcing every FAS lot on.
  // Lex defaults onto the util window (CP_HORIZON_DAYS, default 3d =
  // current 08:00 → +3d 08:00) even when cp.planning_mode=all — otherwise
  // calendar openUntil expands the model to ~months and 3-day util collapses.
  const string planningMode = envString("CP_PLANNING_MODE", "auto");
  const bool lexShortWindow =
      (goal == CpGoal::LexTardyEarly || goal == CpGoal::EarlyPersonnel) &&
      g_cpXorSplits.empty() &&
      envInt("CP_LEX_SHORT_WINDOW", 1) != 0;
  const bool windowSelect =
                            (planningMode == "throughput" &&
                             g_cpXorSplits.empty()) ||
                            envInt("CP_WINDOW_SELECT", 0) != 0 ||
                            lexShortWindow;
  const bool partial =
      goal == CpGoal::Throughput ||
      (windowSelect && (goal == CpGoal::LexTardyEarly ||
                        goal == CpGoal::Tardiness ||
                        goal == CpGoal::EarlyPersonnel));
  const long windowHorizon = horizon;
  point.partial = partial;
  bool hasCalendar = false;
  const Date calendarOrigin = Plan::instance().getCurrent();
  long openUntil = 0;
  auto noteCalendar = [&](Calendar* calendar) {
    if (!calendar) return;
    hasCalendar = true;
    for (auto bucket = calendar->getBuckets();
         bucket != CalendarBucket::iterator::end(); ++bucket) {
      if (bucket->getValue() <= 0.0 || bucket->getEnd() <= calendarOrigin)
        continue;
      Date end = bucket->getEnd();
      if (end == Date::infiniteFuture)
        end = calendarOrigin + Duration(366L * 86400L);
      openUntil = max(openUntil, static_cast<long>((end - calendarOrigin).getSeconds()));
    }
  };
  for (const Task& task : movable) {
    Operation* operation = task.plan ? task.plan->getOperation() : nullptr;
    if (operation) {
      noteCalendar(operation->getAvailable());
      if (operation->getLocation())
        noteCalendar(operation->getLocation()->getAvailable());
    }
    for (const Candidate& candidate : task.candidates) {
      Resource* resource = candidate.resource;
      if (!resource) continue;
      for (const CapacityUse& use : candidate.resources) {
        noteCalendar(use.resource->getAvailable());
        noteCalendar(use.resource->getMaximumCalendar());
        if (use.resource->getLocation())
          noteCalendar(use.resource->getLocation()->getAvailable());
      }
      noteCalendar(resource->getAvailable());
      if (resource->getLocation())
        noteCalendar(resource->getLocation()->getAvailable());
    }
  }
  if (hasCalendar && !partial) horizon = max(horizon + 14L * 86400L, openUntil);
  // Locked orders keep their dates. Their future material events still have
  // to reserve or release the buffer, otherwise a movable order can start
  // before a locked upstream receipt or consume stock a locked order already
  // claimed. Local L1/L2 may skip frozen manufacturing material
  // (cpLocalSkipFrozenMaterial) when gap-fill insert would otherwise go
  // INFEASIBLE; named material-change buffers still pin.
  vector<pair<Buffer*, pair<long, long long>>> lockedFlows;
  {
    const Date origin = Plan::instance().getCurrent();
    const bool skipFrozenMat = cpLocalSkipFrozenMaterial();
    const unordered_set<Buffer*> namedMats = localChangedMaterials();
    auto pinFlowPlanEvents = [&](OperationPlan* plan) {
      if (!plan) return;
      if (partitionExcluded(plan)) return;
      if (plan->getStart() == Date::infinitePast ||
          plan->getEnd() == Date::infinitePast)
        return;
      const long start = max(
          0L, static_cast<long>((plan->getStart() - origin).getSeconds()));
      const long end = max(
          start, static_cast<long>((plan->getEnd() - origin).getSeconds()));
      for (auto fp = plan->beginFlowPlans(); fp != plan->endFlowPlans(); ++fp) {
        Buffer* buffer = fp->getBuffer();
        if (!buffer) continue;
        // When skip-frozen is on, still pin receipts onto named changed
        // materials so 到货变动 can bind.
        if (skipFrozenMat && namedMats.count(buffer) == 0) continue;
        long long quantity = llround(fp->getQuantity() * 1000.0);
        if (quantity == 0) continue;
        Date when = fp->getDate();
        if (when == Date::infinitePast || when == Date::infiniteFuture)
          when = quantity > 0 ? plan->getEnd() : plan->getStart();
        // Receipts at current (t=0) must stay — otherwise named PO arriving
        // exactly on Plan::current disappears and CP sees zero supply.
        if (when < origin && quantity > 0) continue;
        if (when < origin && quantity < 0) continue;
        const long t = max(
            0L, static_cast<long>((when - origin).getSeconds()));
        horizon = max(horizon, t);
        lockedFlows.push_back({buffer, {t, quantity}});
      }
    };
    for (auto plan = OperationPlan::begin(); plan != OperationPlan::end();
         ++plan) {
      Operation* operation = plan->getOperation();
      // Firm purchase / inventory receipts: pin qty at enddate (到货).
      // Resolve buffer via OperationItemSupplier / Inventory — PO reference
      // is not the buffer name. DB STCK rows are not loaded; confirmed PO is
      // the timed-supply path. Named material-change buffers still bind under
      // skip-frozen.
      const string orderType = plan->getOrderType();
      const bool stockOrPurchase =
          !operation || orderType == "STCK" || orderType == "PO";
      if (stockOrPurchase &&
          (locked(&*plan) || plan->getConfirmed() || plan->getClosed() ||
           plan->getCompleted())) {
        if (partitionExcluded(&*plan)) continue;
        if (plan->getEnd() == Date::infinitePast ||
            plan->getEnd() == Date::infiniteFuture ||
            plan->getEnd() < origin) {
          if (!operation) continue;
          // Past receipts: fall through to flowplan pin / inventory-at-current.
        } else {
          // end == origin → pin at t=0 (available from current).
          Buffer* buffer = nullptr;
          if (operation && operation->hasType<OperationItemSupplier>())
            buffer =
                static_cast<OperationItemSupplier*>(operation)->getBuffer();
          if (!buffer && operation &&
              operation->hasType<OperationInventory>())
            buffer = static_cast<OperationInventory*>(operation)->getBuffer();
          if (!buffer) {
            for (auto fp = plan->beginFlowPlans();
                 fp != plan->endFlowPlans() && !buffer; ++fp)
              if (fp->getQuantity() > ROUNDING_ERROR) buffer = fp->getBuffer();
          }
          if (!buffer) {
            try {
              buffer = Buffer::findFromName(plan->getReference());
            } catch (const DataException&) {
              buffer = nullptr;
            }
          }
          if (buffer) {
            if (!(skipFrozenMat && namedMats.count(buffer) == 0)) {
              bool hasReceiptFlow = false;
              for (auto fp = plan->beginFlowPlans();
                   fp != plan->endFlowPlans(); ++fp)
                if (fp->getBuffer() == buffer &&
                    fp->getQuantity() > ROUNDING_ERROR) {
                  hasReceiptFlow = true;
                  break;
                }
              if (hasReceiptFlow)
                pinFlowPlanEvents(&*plan);
              else {
                const long long quantity =
                    llround(max(0.0, plan->getQuantity()) * 1000.0);
                if (quantity > 0) {
                  const long t = max(
                      0L, static_cast<long>(
                              (plan->getEnd() - origin).getSeconds()));
                  horizon = max(horizon, t);
                  lockedFlows.push_back({buffer, {t, quantity}});
                }
              }
            }
            if (!operation || orderType == "STCK" || orderType == "PO")
              continue;
          } else if (!operation) {
            pinFlowPlanEvents(&*plan);
            continue;
          }
        }
      }
      if (!operation) continue;
      if (skipFrozenMat) {
        // Still pin named-material firm receipts via flowplans.
        bool touchesNamed = false;
        for (auto fp = plan->beginFlowPlans(); fp != plan->endFlowPlans();
             ++fp)
          if (fp->getBuffer() && namedMats.count(fp->getBuffer())) {
            touchesNamed = true;
            break;
          }
        if (touchesNamed && (locked(&*plan) || plan->getConfirmed() ||
                             plan->getClosed() || plan->getCompleted()))
          pinFlowPlanEvents(&*plan);
        continue;
      }
      // Alternate/routing parents aren't CP tasks, but their material flows
      // are part of the MRP proposal represented by their schedulable child.
      // Keep those flow events at the MRP timestamps. Delivery operations are
      // also fixed material consumers for the CP model: they aren't movable
      // tasks, but omitting them lets CP move receipts behind customer issues
      // and creates a shortage only after export.
      //
      // Local reschedule exception: candidate expansion marks baseline
      // proposed (incl. DLVR) as approved. locked() then pins those
      // non-schedulable deliveries even under CP_BASELINE_VARIABLE, while
      // L3 frees every MO and alignDeliveryCommitments() rewrites DLVR
      // after solve. Skip non-firm delivery drains in that mode only.
      bool fixedMaterial =
          locked(&*plan) ||
          (g_cpXorSplits.empty() && operation->hasType<OperationDelivery>());
      if (fixedMaterial && operation->hasType<OperationDelivery>() &&
          cpLocalFreeProposedDeliveryMaterial() && !plan->getConfirmed() &&
          !plan->getCompleted() && !plan->getClosed())
        fixedMaterial = false;
      if (!fixedMaterial) continue;
      // Same partition filter as movable/occupancy: foreign-workshop freezes
      // must not poison this group's material timeline.
      if (partitionExcluded(&*plan)) continue;
      if (plan->getStart() <= origin && plan->getEnd() <= origin)
        continue;
      if (plan->getStart() == Date::infinitePast ||
          plan->getEnd() == Date::infinitePast)
        continue;
      const long start = max(
          0L, static_cast<long>((plan->getStart() - origin).getSeconds()));
      const long end = max(
          start, static_cast<long>((plan->getEnd() - origin).getSeconds()));
      for (const Flow& flow : operation->getFlows()) {
        Buffer* buffer = nullptr;
        try {
          buffer = flow.getBuffer();
        } catch (const DataException&) {
          continue;
        }
        if (!buffer) continue;
        long long quantity = llround((flow.getQuantity() * plan->getQuantity() +
                                     flow.getQuantityFixed()) * 1000.0);
        if (quantity == 0) continue;
        if (quantity < 0) {
          if (plan->getStart() <= origin) continue;
          horizon = max(horizon, start);
          lockedFlows.push_back({buffer, {start, quantity}});
          continue;
        }
        if (plan->getEnd() <= origin) continue;
        const double fraction = transferFraction(&*plan);
        if (fraction < 1.0 - 1e-6) {
          const int num = max(1, static_cast<int>(llround(fraction * 1000.0)));
          const long long first = quantity * num / 1000;
          const long release = min(
              end, start + max(1L, static_cast<long>((end - start) * num / 1000)));
          if (first > 0) {
            horizon = max(horizon, release);
            lockedFlows.push_back({buffer, {release, first}});
            quantity -= first;
          }
        }
        if (quantity > 0) {
          horizon = max(horizon, end);
          lockedFlows.push_back({buffer, {end, quantity}});
        }
      }
    }
  }
  if (partial && !localRescheduleEnabled())
    horizon = windowHorizon;
  // Local reschedule places new work after the latest frozen use of each
  // machine (appendAfter below). The short util window is often already full
  // on the required equipment — extend the search horizon so the new lot can
  // start after that floor ("往后排") instead of proving INFEASIBLE.
  if (localRescheduleEnabled()) {
    long latestOcc = 0;
    for (const auto& fixed : occupancy)
      latestOcc = max(latestOcc, get<2>(fixed));
    // Several keep-required hot lots may share one machine after the same
    // appendAfter floor — reserve the sum of their longest candidates.
    long sumDur = 0;
    for (const Task& task : movable) {
      long taskDur = 0;
      for (const Candidate& cand : task.candidates)
        taskDur = max(taskDur, static_cast<long>(max(0, cand.duration)));
      sumDur += taskDur;
    }
    if (sumDur <= 0) sumDur = 1;
    const long slack = max(
        86400L, static_cast<long>(envInt("CP_LOCAL_INSERT_SLACK_SECONDS", 86400)));
    const long insertHorizon = latestOcc + sumDur + slack;
    if (insertHorizon > horizon) {
      logger << "CP local insert: extend horizon " << horizon << "s -> "
             << insertHorizon << "s (latest_frozen=" << latestOcc
             << "s sum_dur=" << sumDur << "s slack=" << slack << "s)\n";
      horizon = insertHorizon;
    }
  }
  point.horizon = horizon;
  // Heap-allocate and leak the builder (same rationale as cpSolveModel): with
  // XOR alternatives, destroying CpModelBuilder after a feasible solve has
  // segfaulted inside OR-Tools teardown on this dataset.
  CpModelBuilder& model = *(new CpModelBuilder());
  vector<BoolVar> selected;
  map<int, BoolVar> jobSelected;
  map<OperationPlan*, BoolVar> rootSelected;
  vector<IntVar> starts;
  vector<IntVar> ends;
  vector<IntVar> spans;
  vector<BoolVar> presences;
  vector<int> presenceTask;
  vector<int> presenceCandidate;
  map<pair<int, int>, BoolVar> onMachine;
  vector<Resource*> machineOf;
  unordered_map<int, vector<IntervalVar>> machineIntervals;
  unordered_map<int, LinearExpr> machineWork;
  map<Resource*, vector<pair<IntervalVar, long long>>> capacityIntervals;
  LinearExpr personnelWork(0);
  map<Resource*, LinearExpr> personnelWorkByResource;
  map<Resource*, long long> personnelTermsByResource;
  long personnelChoices = 0;
  starts.reserve(movable.size());
  ends.reserve(movable.size());
  spans.reserve(movable.size());
  const auto requiredDemands = cpLocalRequiredDemandNames();
  const string localMode = envString(
      "LOCAL_RESCHEDULE_TYPE",
      envString("CP_LOCAL_RESCHEDULE_TYPE", "").c_str());
  const bool requireAllOpenOrders =
      localRescheduleEnabled() &&
      (localMode == "all" || localMode == "combined" || localMode == "mixed");
  for (size_t index = 0; index < movable.size(); ++index) {
    const Task& task = movable[index];
    const Domain window(0, horizon);
    IntVar start = model.NewIntVar(window);
    IntVar end = model.NewIntVar(window);
    IntVar span = model.NewIntVar(Domain(0, horizon));
    BoolVar included = model.NewBoolVar();
    selected.push_back(included);
    OperationPlan* root = task.plan ? cpChoiceRoot(task.plan) : nullptr;
    const bool xorOptional = root && cpIsXorSplitRoot(root);
    if (partial || xorOptional) {
      auto found = rootSelected.find(root);
      if (found == rootSelected.end()) rootSelected.emplace(root, included);
      else model.AddEquality(included, found->second);
      jobSelected.emplace(task.job, included);
      // Existing locked work in the same tree cannot be removed.
      bool keep = locked(root) ||
                  (root && root->getBoolProperty("cp_local_insert", false));
      // A local "all" repair starts from a complete FAS/MRP supply network.
      // Many synchronized/replenishment roots have no direct Demand peg, but
      // are still upstream dependencies of open orders.  Treat the complete
      // existing movable network as the conservative pegging/BOM closure:
      // CP may resequence or reassign it, but the short-window selector may
      // not drop any of those supply trees.
      if (requireAllOpenOrders) keep = true;
      if (Demand* demand = pegDemandOf(root)) {
        const unsigned short status = demand->getStatus();
        if (requiredDemands.count(demand->getName()) ||
            (requireAllOpenOrders &&
             (status & (Demand::STATUS_OPEN | Demand::STATUS_QUOTE))))
          keep = true;
      }
      for (OperationPlan::iterator child(root); child != root->end(); ++child)
        if (locked(&*child)) keep = true;
      // XOR split trees stay optional even when locked flags are off.
      if (keep && !xorOptional) model.AddEquality(included, 1);
      model.AddEquality(start, 0).OnlyEnforceIf(
          operations_research::sat::Not(included));
      model.AddEquality(end, 0).OnlyEnforceIf(
          operations_research::sat::Not(included));
    } else {
      model.AddEquality(included, 1);
    }
    vector<BoolVar> one;
    for (int candidateIndex = 0;
         candidateIndex < static_cast<int>(task.candidates.size());
         ++candidateIndex) {
      const Candidate& candidate = task.candidates[candidateIndex];
      BoolVar presence = model.NewBoolVar();
      // One interval per lot. It may cross a shift boundary. Shift calendars
      // are not used to split the lot; headcount is enforced on the cumulative.
      if (candidate.duration > horizon) {
        model.AddEquality(presence,0);
      } else if (candidate.duration == 0) {
        // Logical co-production steps with no standard time are material
        // events. They preserve precedence without consuming capacity.
        model.AddEquality(end,start).OnlyEnforceIf(presence);
      } else {
        model.AddEquality(span, candidate.duration).OnlyEnforceIf(presence);
        // Broken / zero-capacity equipment cannot be chosen. Local mode used
        // to skip ALL cumulatives, so max=0 never blocked H01-style machines.
        bool deadEquipment = false;
        for (const CapacityUse& use : candidate.resources) {
          for (Resource* cursor = use.resource; cursor;
               cursor = cursor->getOwner())
            if (cursor->getCategory() == "设备" &&
                cursor->getMaximum() <= ROUNDING_ERROR) {
              deadEquipment = true;
              break;
            }
          if (deadEquipment) break;
        }
        if (deadEquipment) {
          model.AddEquality(presence, 0);
        } else {
          auto interval =
              model.NewOptionalIntervalVar(start, span, end, presence);
          if (candidate.resource)
            machineIntervals[candidate.machine].push_back(interval);
          // Hard capacity for 设备 / 人员 / 模具 — same as a normal CP solve.
          // Local repair (order / material / resource) must NOT drop
          // cumulatives; relaxing them leaves export-time overloads.
          for (const CapacityUse& use : candidate.resources) {
            capacityIntervals[use.resource].push_back(
                {interval, use.quantity});
          }
        }
      }
      if (candidate.resource) machineWork[candidate.machine] +=
          LinearExpr::Term(presence, candidate.duration);
      long long candidatePersonnelSeconds = 0;
      for (const CapacityUse& use : candidate.resources) {
        if (!use.resource || use.resource->getCategory() != "人员") continue;
        const long long resourceSeconds = llround(
            static_cast<double>(candidate.duration) *
            static_cast<double>(llabs(use.quantity)) / 1000.0);
        if (resourceSeconds > 0) {
          // Capacity bounds still cover every workshop.
          personnelWorkByResource[use.resource] +=
              LinearExpr::Term(presence, resourceSeconds);
          ++personnelTermsByResource[use.resource];
        }
        // Throughput / util objective: only scoped resource (default 4工段).
        if (resourceSeconds > 0 && cpCountsForEarlyPersonnel(use.resource))
          candidatePersonnelSeconds += resourceSeconds;
      }
      if (candidatePersonnelSeconds > 0) {
        personnelWork += LinearExpr::Term(presence, candidatePersonnelSeconds);
        ++personnelChoices;
      }
      one.push_back(presence);
      const auto key = make_pair(static_cast<int>(index), candidate.machine);
      auto existing = onMachine.find(key);
      if (existing == onMachine.end()) {
        onMachine.emplace(key, presence);
      } else {
        BoolVar combined = model.NewBoolVar();
        model.AddEquality(LinearExpr(combined),
                          LinearExpr(existing->second) + presence);
        existing->second = combined;
      }
      if (candidate.resource) {
        if (candidate.machine >= static_cast<int>(machineOf.size()))
          machineOf.resize(candidate.machine + 1, nullptr);
        machineOf[candidate.machine] = candidate.resource;
      }
      presences.push_back(presence);
      presenceTask.push_back(static_cast<int>(index));
      presenceCandidate.push_back(candidateIndex);
    }
    LinearExpr choiceSum(0);
    for (BoolVar choice : one) choiceSum += choice;
    model.AddEquality(choiceSum,included);
    model.AddEquality(end,start+span);
    starts.push_back(start);
    ends.push_back(end);
    spans.push_back(span);
  }
  // Optional lot-split: exactly one of {whole, cut0, cut1, ...}; each cut
  // is a head+tail pair (still one knife cut, several cut-points).
  // Keep the constraint range so preflight can exclude representation-only
  // XOR relations while the formal CP search still enforces all of them.
  const int xorConstraintStart = model.MutableProto()->constraints_size();
  size_t xorConstraints = 0;
  size_t xorCutChoices = 0;
  for (const CpXorSplit& alt : g_cpXorSplits) {
    auto wholeVar = rootSelected.find(alt.whole);
    const bool hasW = wholeVar != rootSelected.end();
    vector<BoolVar> cutVars;
    for (const CpXorCut& cut : alt.cuts) {
      auto headVar = rootSelected.find(cut.head);
      auto tailVar = rootSelected.find(cut.tail);
      if (headVar == rootSelected.end() || tailVar == rootSelected.end())
        continue;
      model.AddEquality(headVar->second, tailVar->second);
      cutVars.push_back(headVar->second);
    }
    if (hasW && !cutVars.empty()) {
      LinearExpr choice = LinearExpr::Term(wholeVar->second, 1);
      for (BoolVar cut : cutVars) choice += LinearExpr::Term(cut, 1);
      // Full horizon: cover with exactly one of {whole, cut*}.
      // Window-select/partial: allow dropping the lot entirely (0), otherwise
      // forcing =1 fights subset search and often proves INFEASIBLE in 3d.
      if (partial)
        model.AddLessOrEqual(choice, 1);
      else
        model.AddEquality(choice, 1);
      ++xorConstraints;
      xorCutChoices += cutVars.size();
    } else if (hasW && cutVars.empty()) {
      if (!partial) model.AddEquality(wholeVar->second, 1);
    } else if (!hasW && cutVars.size() == 1) {
      if (!partial) model.AddEquality(cutVars.front(), 1);
      ++xorConstraints;
      xorCutChoices += 1;
    } else if (!hasW && cutVars.size() > 1) {
      LinearExpr choice(0);
      for (BoolVar cut : cutVars) choice += LinearExpr::Term(cut, 1);
      if (partial)
        model.AddLessOrEqual(choice, 1);
      else
        model.AddEquality(choice, 1);
      ++xorConstraints;
      xorCutChoices += cutVars.size();
    }
  }
  if (xorConstraints)
    logger << "CP XOR split constraints: " << xorConstraints
           << " lots, cut_choices=" << xorCutChoices << "\n";
  const int xorConstraintEnd = model.MutableProto()->constraints_size();
  vector<pair<int, vector<size_t>>> jobOrder(byJob.begin(), byJob.end());
  int transferLinks = 0;
  for (const auto& entry : jobOrder) {
    auto members = entry.second;
    sort(members.begin(), members.end(), [&](size_t left, size_t right) {
      return movable[left].sequence < movable[right].sequence;
    });
    for (size_t step = 1; step < members.size(); ++step) {
      const size_t previous = members[step - 1];
      const size_t next = members[step];
      const double fraction = transferFraction(movable[previous].plan);
      if (fraction >= 1.0 - 1e-6) {
        model.AddGreaterOrEqual(starts[next], ends[previous])
            .OnlyEnforceIf({selected[previous],selected[next]});
      } else {
        const int num = max(1, static_cast<int>(llround(fraction * 1000.0)));
        model.AddGreaterOrEqual(LinearExpr::Term(starts[next], 1000),
                                LinearExpr::Term(starts[previous], 1000) +
                                    LinearExpr::Term(spans[previous], num))
            .OnlyEnforceIf({selected[previous],selected[next]});
        ++transferLinks;
      }
    }
  }
  // Lex / pure tardiness / early-personnel: score only demands due inside the
  // early window (default 3 days). Window-end orders stay in the model for
  // capacity, but do not drive the primary objective. Set CP_TARDY_WINDOW_DAYS=0
  // to restore all-horizon tardiness.
  const bool windowTardyGoal =
      goal == CpGoal::LexTardyEarly || goal == CpGoal::Tardiness ||
      goal == CpGoal::EarlyPersonnel;
  const long tardyWindowSecs =
      windowTardyGoal ? cpTardyWindowSecs(horizon) : 0;
  vector<IntVar> lates;
  vector<IntVar> lateCompletions;
  vector<int> lateWeight;
  vector<long> lateDue;
  vector<string> lateNames;
  int tardyDemandsAll = 0;
  int tardyDemandsUsed = 0;
  int tardyMissPenalty = 0;
  for (const CpDemand& demand : demands) {
    vector<IntVar> completions;
    vector<BoolVar> demandSelected;
    for (int job : demand.jobs) {
      auto found = byJob.find(job);
      if (found == byJob.end() || found->second.empty()) continue;
      size_t last = found->second.front();
      for (size_t member : found->second) {
        if (movable[member].sequence >= movable[last].sequence) last = member;
        if (partial) demandSelected.push_back(selected[member]);
      }
      completions.push_back(ends[last]);
    }
    if (completions.empty()) continue;
    ++tardyDemandsAll;
    if (tardyWindowSecs > 0 && demand.due > tardyWindowSecs) continue;
    ++tardyDemandsUsed;
    IntVar completion = model.NewIntVar(Domain(0, horizon));
    if (completions.size() == 1)
      model.AddEquality(completion, completions.front());
    else
      model.AddMaxEquality(completion, completions);
    // Missed window demand (unselected tree) counts as finishing at the
    // window end — never free. Otherwise optional select would zero out
    // tardiness by dropping every due-in-window order.
    IntVar late = model.NewIntVar(Domain(0, horizon));
    if (partial && !demandSelected.empty()) {
      BoolVar covered = model.NewBoolVar();
      for (BoolVar bit : demandSelected)
        model.AddImplication(bit, covered);
      model.AddBoolOr(demandSelected).OnlyEnforceIf(covered);
      model.AddGreaterOrEqual(late + demand.due, completion)
          .OnlyEnforceIf(covered);
      const long missLate = max(1L, horizon - max(0L, demand.due));
      model.AddEquality(late, missLate)
          .OnlyEnforceIf(operations_research::sat::Not(covered));
      ++tardyMissPenalty;
    } else {
      model.AddGreaterOrEqual(late + demand.due, completion);
    }
    lates.push_back(late);
    lateCompletions.push_back(completion);
    lateWeight.push_back(max(1, demand.weight));
    lateDue.push_back(demand.due);
    lateNames.push_back(demand.name.empty() ? string("-") : demand.name);
  }
  if (tardyWindowSecs > 0 || tardyMissPenalty > 0)
    logger << "CP tardiness window="
           << ((tardyWindowSecs > 0 ? tardyWindowSecs : horizon) / 86400L)
           << "d demands=" << tardyDemandsUsed << "/" << tardyDemandsAll
           << " missPenalty=" << tardyMissPenalty << "\n";
  LinearExpr tardySum(0);
  int maxWeight = 1;
  for (size_t index = 0; index < lates.size(); ++index) {
    tardySum += LinearExpr::Term(lates[index], lateWeight[index]);
    maxWeight = max(maxWeight, lateWeight[index]);
  }
  const long demandCount = max(1L, static_cast<long>(lates.size()));
  const long long totalTardyCap =
      static_cast<long long>(horizon) * demandCount * maxWeight;
  IntVar totalTardy = model.NewIntVar(Domain(0, totalTardyCap));
  model.AddEquality(totalTardy, tardySum);
  vector<IntVar> loads;
  for (const auto& work : machineWork) {
    IntVar load = model.NewIntVar(Domain(0, horizon * max(1L, static_cast<long>(movable.size()))));
    model.AddEquality(load, work.second);
    loads.push_back(load);
  }
  IntVar maxLoad = model.NewIntVar(Domain(0, horizon * max(1L, static_cast<long>(movable.size()))));
  if (!loads.empty()) model.AddMaxEquality(maxLoad, loads);
  else model.AddEquality(maxLoad, 0);
  IntVar makespan = model.NewIntVar(Domain(0, horizon));
  model.AddMaxEquality(makespan, ends);
  // Fixed locked occupancy uses the same NoOverlap encoding for local and
  // global CP. Do not appendAfter-only: that forbids gap-filling and is not
  // equivalent to hard capacity against frozen loads.
  for (const auto& fixed : occupancy) {
    long start = get<1>(fixed);
    long end = get<2>(fixed);
    if (partial) end = min(end, horizon);
    if (end <= start) continue;
    machineIntervals[get<0>(fixed)].push_back(model.NewIntervalVar(
        model.NewConstant(start), end - start, model.NewConstant(end)));
  }
  // Shift closures are capacity, not downtime inserted into a lot. MRP dates
  // also sit one second past a bucket end (20:00:01, and the 23:59:59/00:00:00
  // hole), so a one-second boundary is left open.
  int calendarBreaks = 0;
  int changeoverArcs = 0;
  int personnelEnergyBounds = 0;
  using operations_research::sat::Not;
  for (size_t machine = 0; machine < machineOf.size(); ++machine) {
    Resource* resource = machineOf[machine];
    if (!resource || !resource->getSetupMatrix()) continue;
    vector<int> tasks;
    for (const auto& entry : onMachine)
      if (entry.first.second == static_cast<int>(machine))
        tasks.push_back(entry.first.first);
    if (tasks.size() < 2 || tasks.size() > 40) continue;
    auto circuit = model.AddCircuitConstraint();
    BoolVar emptyCircuit = model.NewBoolVar();
    circuit.AddArc(0, 0, emptyCircuit);
    for (int index : tasks)
      model.AddImplication(emptyCircuit, Not(onMachine[{index, static_cast<int>(machine)}]));
    for (int tail : tasks) {
      BoolVar present = onMachine[{tail, static_cast<int>(machine)}];
      circuit.AddArc(0, tail + 1, model.NewBoolVar());
      circuit.AddArc(tail + 1, 0, model.NewBoolVar());
      circuit.AddArc(tail + 1, tail + 1, Not(present));
      for (int head : tasks) {
        if (tail == head) continue;
        BoolVar arc = model.NewBoolVar();
        circuit.AddArc(tail + 1, head + 1, arc);
        const long setup = setupSeconds(
            resource, requiredSetup(movable[tail].plan, resource),
            requiredSetup(movable[head].plan, resource));
        if (setup <= 0) continue;
        model
            .AddGreaterOrEqual(starts[head],
                               LinearExpr(ends[tail]) + static_cast<int64_t>(setup))
            .OnlyEnforceIf(arc);
        ++changeoverArcs;
      }
    }
  }
  struct MaterialEvent {
    int task = -1;
    long fixedTime = 0;
    long long quantity = 0;
    bool produce = false;
    double fraction = 1.0;
  };
  map<Buffer*, vector<MaterialEvent>> materialEvents;
  for (size_t index = 0; index < movable.size(); ++index) {
    Operation* operation = movable[index].plan->getOperation();
    if (!operation) continue;
    for (const Flow& flow : operation->getFlows()) {
      Buffer* buffer = nullptr;
      try {
        buffer = flow.getBuffer();
      } catch (const DataException&) {
        continue;
      }
      if (!buffer) continue;
      const double raw = flow.getQuantity() * movable[index].plan->getQuantity() +
                         flow.getQuantityFixed();
      const long long quantity = llround(raw * 1000.0);
      if (quantity == 0) continue;
      MaterialEvent event;
      event.task = static_cast<int>(index);
      event.quantity = quantity;
      event.produce = quantity > 0;
      event.fraction = event.produce ? transferFraction(movable[index].plan) : 1.0;
      materialEvents[buffer].push_back(event);
    }
  }
  // Routing and alternate parents can own the material flows while their
  // time/resource load sits on schedulable children. Bind parent consumption
  // to the first child and parent production to the last child, so the whole
  // tree remains optional as one CP decision instead of becoming fixed stock.
  map<OperationPlan*, pair<int,int>> parentTasks;
  for (size_t index = 0; index < movable.size(); ++index) {
    for (OperationPlan* parent = movable[index].plan->getOwner(); parent;
         parent = parent->getOwner()) {
      auto found = parentTasks.find(parent);
      if (found == parentTasks.end())
        parentTasks[parent] = {static_cast<int>(index), static_cast<int>(index)};
      else {
        if (movable[index].sequence < movable[found->second.first].sequence)
          found->second.first = static_cast<int>(index);
        if (movable[index].sequence > movable[found->second.second].sequence)
          found->second.second = static_cast<int>(index);
      }
    }
  }
  for (const auto& link : parentTasks) {
    OperationPlan* parent = link.first;
    if (!parent || locked(parent) || schedulable(parent) ||
        !parent->getOperation()) continue;
    for (const Flow& flow : parent->getOperation()->getFlows()) {
      Buffer* buffer = nullptr;
      try { buffer = flow.getBuffer(); }
      catch (const DataException&) { continue; }
      if (!buffer) continue;
      const long long quantity = llround(
          (flow.getQuantity()*parent->getQuantity()+flow.getQuantityFixed())*1000.0);
      if (!quantity) continue;
      MaterialEvent event;
      event.task = quantity > 0 ? link.second.second : link.second.first;
      event.quantity = quantity;
      event.produce = quantity > 0;
      event.fraction = event.produce ? transferFraction(parent) : 1.0;
      materialEvents[buffer].push_back(event);
    }
  }
  for (const auto& lockedFlow : lockedFlows) {
    MaterialEvent event;
    event.task = -1;
    event.fixedTime = lockedFlow.second.first;
    event.quantity = lockedFlow.second.second;
    event.produce = event.quantity > 0;
    materialEvents[lockedFlow.first].push_back(event);
  }
  // Material-change: operation getFlows() can miss purchase drains that only
  // show up on flowplans (routing child / start-type). Map named-buffer
  // flowplans onto a movable task in the same root tree.
  const unordered_set<Buffer*> namedMatsForCp = localChangedMaterials();
  if (!namedMatsForCp.empty()) {
    unordered_map<OperationPlan*, int> taskOf;
    for (size_t index = 0; index < movable.size(); ++index)
      if (movable[index].plan) taskOf[movable[index].plan] = static_cast<int>(index);
    auto taskForNode = [&](OperationPlan* node) -> int {
      if (!node) return -1;
      for (OperationPlan* cursor = node; cursor; cursor = cursor->getOwner()) {
        auto found = taskOf.find(cursor);
        if (found != taskOf.end()) return found->second;
      }
      OperationPlan* top = node->getTopOwner() ? node->getTopOwner() : node;
      for (const auto& entry : taskOf) {
        OperationPlan* leaf = entry.first;
        OperationPlan* leafTop = leaf->getTopOwner() ? leaf->getTopOwner() : leaf;
        if (leafTop == top) return entry.second;
      }
      return -1;
    };
    for (auto plan = OperationPlan::begin(); plan != OperationPlan::end();
         ++plan) {
      if (partitionExcluded(&*plan)) continue;
      for (auto fp = plan->beginFlowPlans(); fp != plan->endFlowPlans(); ++fp) {
        Buffer* buffer = fp->getBuffer();
        if (!buffer || namedMatsForCp.count(buffer) == 0) continue;
        const long long quantity = llround(fp->getQuantity() * 1000.0);
        if (quantity == 0) continue;
        const int index = taskForNode(&*plan);
        if (index < 0) continue;
        bool exists = false;
        for (const MaterialEvent& prior : materialEvents[buffer])
          if (prior.task == index && prior.quantity == quantity) {
            exists = true;
            break;
          }
        if (exists) continue;
        MaterialEvent event;
        event.task = index;
        event.quantity = quantity;
        event.produce = quantity > 0;
        event.fraction = 1.0;
        materialEvents[buffer].push_back(event);
      }
    }
  }
  // Named supply-change with onhand≈0: consumer starts cannot precede the
  // earliest locked receipt (到货). Reservoir should imply this, but L3 seed
  // often keeps the FAS early start when the consume event was missing —
  // enforce explicitly once consumers are bound.
  unordered_map<Buffer*, long> namedEarliestReceipt;
  for (const auto& lockedFlow : lockedFlows) {
    Buffer* buffer = lockedFlow.first;
    if (!buffer || namedMatsForCp.count(buffer) == 0) continue;
    if (lockedFlow.second.second <= 0) continue;
    if (buffer->getOnHand() > ROUNDING_ERROR) continue;
    auto found = namedEarliestReceipt.find(buffer);
    if (found == namedEarliestReceipt.end())
      namedEarliestReceipt[buffer] = lockedFlow.second.first;
    else
      found->second = min(found->second, lockedFlow.second.first);
  }
  int namedArrivalBounds = 0;
  for (const auto& entry : materialEvents) {
    auto receipt = namedEarliestReceipt.find(entry.first);
    if (receipt == namedEarliestReceipt.end()) continue;
    for (const MaterialEvent& event : entry.second) {
      if (event.task < 0 || event.produce || event.quantity >= 0) continue;
      model
          .AddGreaterOrEqual(starts[event.task],
                             static_cast<int64_t>(receipt->second))
          .OnlyEnforceIf(selected[event.task]);
      ++namedArrivalBounds;
    }
  }
  if (namedArrivalBounds)
    logger << "CP local material arrival bounds=" << namedArrivalBounds
           << "\n";
  int materialBuffers = 0;
  int inventoryBuffers = 0;
  int producerOnlyBuffers = 0;
  for (auto& entry : materialEvents) {
    Buffer* buffer = entry.first;
    const bool namedChange = namedMatsForCp.count(buffer) != 0;
    const bool limited = materialSupplyLimited(buffer) || namedChange;
    const bool capped = buffer->getMaximum() > ROUNDING_ERROR;
    if (!limited && !capped) continue;
    bool producer = false;
    bool consumer = false;
    long long positive = llround(buffer->getOnHand() * 1000.0);
    if (positive < 0) positive = 0;
    for (const MaterialEvent& event : entry.second) {
      if (event.produce) {
        producer = true;
        positive += event.quantity;
      } else {
        consumer = true;
      }
    }
    if (!producer && consumer && !partial && limited && !namedChange) continue;
    if (!consumer && !capped) {
      // With no consumption and no maximum stock, positive production can
      // never violate the lower inventory bound.  Such a reservoir is always
      // feasible and only adds presolve work. Named material-change still
      // logs the skipped receipt so lockedMaterial diagnosis stays clear.
      if (producer) ++producerOnlyBuffers;
      continue;
    }
    long long maximum = positive + 1;
    if (capped) {
      maximum = llround(buffer->getMaximum() * 1000.0);
      if (maximum < positive) maximum = positive;
      if (maximum < 1) maximum = 1;
      ++inventoryBuffers;
    }
    if (limited && consumer) ++materialBuffers;
    long long minimum = 0;
    if (!limited) {
      for (const MaterialEvent& event : entry.second)
        if (event.quantity < 0) minimum += event.quantity;
      minimum += min(0LL,llround(buffer->getOnHand()*1000.0));
    }
    auto reservoir = model.AddReservoirConstraint(minimum, maximum);
    const long long initial = llround(buffer->getOnHand() * 1000.0);
    if (initial > 0) reservoir.AddEvent(LinearExpr(0), initial);
    for (const MaterialEvent& event : entry.second) {
      if (event.task < 0) {
        if (!partial || event.fixedTime <= horizon)
          reservoir.AddEvent(LinearExpr(static_cast<int64_t>(event.fixedTime)),
                             event.quantity);
        continue;
      }
      if (!event.produce) {
        reservoir.AddOptionalEvent(starts[event.task], event.quantity,selected[event.task]);
        continue;
      }
      if (event.fraction >= 1.0 - 1e-6) {
        reservoir.AddOptionalEvent(ends[event.task], event.quantity,selected[event.task]);
        continue;
      }
      const int num = max(1, static_cast<int>(llround(event.fraction * 1000.0)));
      const long long first = event.quantity * num / 1000;
      IntVar release = model.NewIntVar(Domain(0, horizon));
      model.AddEquality(LinearExpr::Term(release, 1000),
                        LinearExpr::Term(starts[event.task], 1000) +
                            LinearExpr::Term(spans[event.task], num));
      reservoir.AddOptionalEvent(release, first,selected[event.task]);
      if (event.quantity > first)
        reservoir.AddOptionalEvent(ends[event.task], event.quantity - first,selected[event.task]);
    }
  }
  // Capacity units are scaled so fractional staffing (e.g. 1.5) is exact.
  for (auto& entry : capacityIntervals) {
    Resource* resource = entry.first;
    const long long capacity = llround(resource->getMaximum() * 1000.0);
    auto cumulative = model.AddCumulative(capacity);
    for (const auto& use : entry.second)
      cumulative.AddDemand(use.first, use.second);
    // Headcount follows the maximum calendar. The lot stays one interval
    // across a shift change; only the available people change. A one-second
    // bucket boundary is left open so the midnight hole is not a real closure.
    if (Calendar* calendar = resource->getMaximumCalendar()) {
      Calendar::EventIterator event(calendar, calendarOrigin, true);
      Date cursor = calendarOrigin;
      const Date limit = calendarOrigin + Duration(horizon);
      double value = event.getValue();
      while (cursor < limit) {
        ++event;
        Date next = min(event.getDate(), limit);
        if (next <= cursor) break;
        const long long reduction = max(0LL, capacity - llround(max(0.0, value) * 1000.0));
        long start = (cursor - calendarOrigin).getSeconds();
        long end = (next - calendarOrigin).getSeconds();
        // One lot interval may cross a shift. Headcount still has to hold at
        // every moment, so a lower-attendance bucket consumes the difference.
        // A one-second bucket boundary is left open.
        if (reduction && end > start + 1) {
          start += 1;
          auto interval = model.NewIntervalVar(model.NewConstant(start), end-start,
                                               model.NewConstant(end));
          cumulative.AddDemand(interval, reduction);
          ++calendarBreaks;
        }
        cursor = next;
        value = event.getValue();
      }
    }
    for (auto plan = OperationPlan::begin(); plan != OperationPlan::end(); ++plan) {
      if (!locked(&*plan)) continue;
      long start = max(0L, static_cast<long>((plan->getStart() - calendarOrigin).getSeconds()));
      long end = static_cast<long>((plan->getEnd() - calendarOrigin).getSeconds());
      if (end <= start) continue;
      for (auto load = plan->beginLoadPlans(); load != plan->endLoadPlans(); ++load) {
        if (!load->isStart() || load->getResource() != resource) continue;
        auto interval = model.NewIntervalVar(model.NewConstant(start), end - start,
                                             model.NewConstant(end));
        cumulative.AddDemand(interval, llround(fabs(load->getQuantity()) * 1000.0));
      }
    }
  }
  // Redundant energetic bounds by workshop. The cumulative constraints above
  // prove feasibility at every instant, but their relaxation gives a loose
  // global objective bound when thousands of intervals are optional. This
  // linear knapsack bound states directly that selected person-seconds in a
  // workshop can't exceed that workshop's shift capacity in the horizon.
  // For lex/early, capacity matches the util window (default 3d from current),
  // not an expanded all-mode openUntil horizon.
  long capacityHorizon = horizon;
  if (partial &&
      (goal == CpGoal::LexTardyEarly || goal == CpGoal::EarlyPersonnel)) {
    long utilDays = max(1, min(365, envInt("CP_HORIZON_DAYS", 3)));
    long earlyDays = max(0, envInt("CP_EARLY_DAYS", 3));
    if (earlyDays <= 0) earlyDays = utilDays;
    capacityHorizon =
        min(horizon, min(utilDays, earlyDays) * 86400L);
  }
  for (auto& entry : personnelWorkByResource) {
    Resource* resource = entry.first;
    if (!resource) continue;
    // Order-local insert may push work past the util window — skip the
    // 3-day personnel energy cap there. Resource-change (and all that
    // includes resource triggers) keeps it so headcount cuts bind.
    if (localRescheduleEnabled()) {
      string typ = envString("CP_LOCAL_RESCHEDULE_TYPE", "order");
      transform(typ.begin(), typ.end(), typ.begin(),
                [](unsigned char ch) { return static_cast<char>(tolower(ch)); });
      if (typ != "resource" && typ != "all" && typ != "combined" &&
          typ != "mixed")
        continue;
    }
    long long availableSeconds = 0;
    if (Calendar* calendar = resource->getMaximumCalendar()) {
      Calendar::EventIterator event(calendar, calendarOrigin, true);
      Date cursor = calendarOrigin;
      const Date limit = calendarOrigin + Duration(capacityHorizon);
      double value = event.getValue();
      while (cursor < limit) {
        ++event;
        Date next = min(event.getDate(), limit);
        if (next <= cursor) break;
        const long seconds = (next - cursor).getSeconds();
        availableSeconds += llround(
            static_cast<double>(seconds) * max(0.0, value));
        cursor = next;
        value = event.getValue();
      }
    } else {
      availableSeconds = llround(
          static_cast<double>(capacityHorizon) *
          max(0.0, resource->getMaximum()));
    }
    // Candidate coefficients are rounded independently to whole
    // person-seconds. Keep a one-second allowance per term so this redundant
    // constraint can never remove a schedule accepted by the cumulative.
    availableSeconds += personnelTermsByResource[resource];
    model.AddLessOrEqual(entry.second, availableSeconds);
    ++personnelEnergyBounds;
    logger << "CP personnel bound resource='" << resource->getName()
           << "' available=" << availableSeconds / 3600.0 << "h terms="
           << personnelTermsByResource[resource] << "\n";
  }
  logger << "CP model calendar=" << calendarBreaks
         << " changeover=" << changeoverArcs
         << " personnelBounds=" << personnelEnergyBounds
         << " transfer=" << transferLinks
         << " material=" << materialBuffers
         << " inventory=" << inventoryBuffers
         << " producerOnlySkipped=" << producerOnlyBuffers
         << " lockedMaterial=" << lockedFlows.size() << "\n";
  for (auto& entry : machineIntervals) {
    Resource* resource = machineOf[entry.first];
    if (!resource || !capacityIntervals.count(resource))
      model.AddNoOverlap(entry.second);
  }
  if (makespanCap >= 0) model.AddLessOrEqual(makespan, makespanCap);
  const bool augmecon = augment && goal == CpGoal::Makespan &&
                        (loadCap >= 0 || tardyCap >= 0);
  IntVar loadSlack;
  IntVar tardySlack;
  bool hasLoadSlack = false;
  bool hasTardySlack = false;
  if (augmecon && loadCap >= 0) {
    loadSlack = model.NewIntVar(Domain(0, max(0L, loadCap)));
    model.AddEquality(LinearExpr::Term(maxLoad, 1) + loadSlack, loadCap);
    hasLoadSlack = true;
  } else if (loadCap >= 0) {
    model.AddLessOrEqual(maxLoad, loadCap);
  }
  if (augmecon && tardyCap >= 0) {
    tardySlack = model.NewIntVar(Domain(0, max(0L, tardyCap)));
    model.AddEquality(LinearExpr::Term(totalTardy, 1) + tardySlack, tardyCap);
    hasTardySlack = true;
  } else if (tardyCap >= 0) {
    model.AddLessOrEqual(totalTardy, tardyCap);
  }
  const int weightMakespan = max(0, envInt("CP_WEIGHT_MAKESPAN", 80));
  const int weightLoad = max(0, envInt("CP_WEIGHT_LOAD", 10));
  const int weightTardiness = max(0, envInt("CP_WEIGHT_TARDINESS", 10));
  const bool maximizePersonnel =
      envString("CP_THROUGHPUT_OBJECTIVE", "lots") == "personnel";
  // Shared with solution export when EarlyPersonnel / lex / due-first runs.
  IntVar earlyPersonnelVar = model.NewIntVar(Domain(0, 0));
  bool hasEarlyPersonnel = false;
  long long earlyPersonnelCap = 0;
  bool deferLexObjective = goal == CpGoal::LexTardyEarly;
  // Pure throughput packs the window. Lex/tardiness with windowSelect still
  // sets partial=true for optional trees, but must use the lex objective —
  // not the personnel-max objective below.
  if (partial && goal == CpGoal::Throughput) {
    // More lots first, then more pieces. A shorter makespan is not rewarded:
    // that pulled every lot onto the first morning.
    unordered_map<int, long long> jobQty;
    for (size_t index = 0; index < movable.size(); ++index) {
      if (!movable[index].plan) continue;
      long long qty = llround(max(0.0, movable[index].plan->getQuantity()) * 1000.0);
      if (qty < 1) qty = 1;
      long long& slot = jobQty[movable[index].job];
      if (qty > slot) slot = qty;
    }
    long long qtySpan = 1;
    for (const auto& entry : jobQty) qtySpan += entry.second;
    LinearExpr lotObjective(0);
    for (const auto& entry : jobSelected) {
      long long qty = 1;
      auto found = jobQty.find(entry.first);
      if (found != jobQty.end()) qty = found->second;
      lotObjective += LinearExpr::Term(entry.second, qtySpan + qty);
    }
    if (maximizePersonnel) {
      // Personnel utilization is the primary target. MRP baseline jobs are
      // mandatory; among feasible additions, one extra person-second
      // outweighs the entire selected-job tie breaker.
      const int64_t personnelFactor =
          static_cast<int64_t>(jobSelected.size()) + 1;
      LinearExpr selectedJobs(0);
      for (const auto& entry : jobSelected) selectedJobs += entry.second;
      model.Maximize(personnelWork * personnelFactor + selectedJobs);
      logger << "CP throughput objective=personnel utilization"
             << " resource='" << cpEarlyPersonnelResourceScope() << "'"
             << " choices=" << personnelChoices << "\n";
    } else {
      model.Maximize(lotObjective);
    }
  } else if (augmecon) {
    const long long loadRange = max(1L, augment->loadRange);
    const long long tardyRange = max(1L, augment->tardyRange);
    const long long loadFactor = hasLoadSlack ? loadRange : 1;
    const long long tardyFactor = hasTardySlack ? tardyRange : 1;
    long long maxBonus = 0;
    if (hasLoadSlack)
      maxBonus += max(0L, loadCap) * (hasTardySlack ? tardyRange : 1);
    if (hasTardySlack)
      maxBonus += max(0L, tardyCap) * (hasLoadSlack ? loadRange : 1);
    long long primary = loadFactor * tardyFactor * 10000;
    if (primary <= maxBonus) primary = maxBonus + 1;
    LinearExpr objective = LinearExpr::Term(makespan, primary);
    if (hasLoadSlack)
      objective += LinearExpr::Term(loadSlack, -(hasTardySlack ? tardyRange : 1));
    if (hasTardySlack)
      objective += LinearExpr::Term(tardySlack, -(hasLoadSlack ? loadRange : 1));
    model.Minimize(objective);
  } else if (goal == CpGoal::Load) {
    model.Minimize(maxLoad);
  } else if (goal == CpGoal::Tardiness) {
    model.Minimize(totalTardy);
  } else if (goal == CpGoal::EarlyPersonnel || goal == CpGoal::LexTardyEarly ||
             goal == CpGoal::Weighted) {
    // Early-window personnel: person-seconds on the scoped resource
    // (default all 人员 / CP_EARLY_PERSONNEL_RESOURCE) for lots that finish
    // within CP_EARLY_DAYS (default 3). Aligns with util = load/available on
    // that pool over the same 3-day window. Lex uses this as level-2.
    long earlyDays = max(0, envInt("CP_EARLY_DAYS", 3));
    if ((goal == CpGoal::EarlyPersonnel || goal == CpGoal::LexTardyEarly) &&
        earlyDays <= 0)
      earlyDays = 3;
    const long earlyHorizonSecs =
        earlyDays > 0 ? min(horizon, earlyDays * 86400L) : 0;
    const string earlyResourceScope = cpEarlyPersonnelResourceScope();
    const int weightPersonnel = max(0, envInt("CP_WEIGHT_PERSONNEL", 1));
    LinearExpr earlyPersonnelExpr(0);
    const bool wantEarly =
        earlyHorizonSecs > 0 && !presences.empty() &&
        (goal == CpGoal::EarlyPersonnel || goal == CpGoal::LexTardyEarly ||
         weightPersonnel > 0);
    if (wantEarly) {
      vector<BoolVar> doneEarly;
      doneEarly.reserve(ends.size());
      for (size_t task = 0; task < ends.size(); ++task) {
        // early <=> selected && end <= earlyHorizon (3d window).
        // Do not force end > horizon when unselected: partial sets end=0,
        // and that encoding made the whole window-select model infeasible.
        BoolVar early = model.NewBoolVar();
        model.AddImplication(early, selected[task]);
        model.AddLessOrEqual(ends[task], earlyHorizonSecs).OnlyEnforceIf(early);
        model.AddGreaterThan(ends[task], earlyHorizonSecs)
            .OnlyEnforceIf(
                {selected[task], operations_research::sat::Not(early)});
        doneEarly.push_back(early);
      }
      for (size_t index = 0; index < presences.size(); ++index) {
        const int task = presenceTask[index];
        const int cand = presenceCandidate[index];
        if (task < 0 || cand < 0 ||
            task >= static_cast<int>(movable.size()) ||
            cand >= static_cast<int>(movable[task].candidates.size()))
          continue;
        long long personSeconds = 0;
        for (const CapacityUse& use :
             movable[task].candidates[cand].resources) {
          if (!cpCountsForEarlyPersonnel(use.resource)) continue;
          personSeconds += llround(
              static_cast<double>(movable[task].candidates[cand].duration) *
              static_cast<double>(llabs(use.quantity)) / 1000.0);
        }
        if (personSeconds <= 0) continue;
        earlyPersonnelCap += personSeconds;
        BoolVar both = model.NewBoolVar();
        model.AddGreaterOrEqual(presences[index], both);
        model.AddGreaterOrEqual(doneEarly[task], both);
        model.AddGreaterOrEqual(both + 1, LinearExpr(presences[index]) +
                                              doneEarly[task]);
        earlyPersonnelExpr += LinearExpr::Term(both, personSeconds);
      }
      if (earlyPersonnelCap > 0) {
        earlyPersonnelVar =
            model.NewIntVar(Domain(0, earlyPersonnelCap));
        model.AddEquality(earlyPersonnelVar, earlyPersonnelExpr);
        hasEarlyPersonnel = true;
      }
    }
    if (goal == CpGoal::EarlyPersonnel) {
      if (hasEarlyPersonnel) {
        model.Maximize(earlyPersonnelVar);
        logger << "CP early-personnel maximize days=" << earlyDays
               << " resource='" << earlyResourceScope << "'"
               << " cap=" << earlyPersonnelCap << "\n";
      } else {
        model.Minimize(makespan);
        logger << "CP early-personnel skipped (no personnel in early window)\n";
      }
    } else if (goal == CpGoal::LexTardyEarly) {
      // Soft lex is applied after FAS/MRP seeding so we can add a tertiary
      // anchor-to-seed term (改变率 / minimize start move vs seed).
      // Placeholder keeps the model valid if seeding is skipped.
      model.Minimize(totalTardy);
      logger << "CP lex soft: build early-personnel days=" << earlyDays
             << " resource='" << earlyResourceScope << "'"
             << " cap=" << earlyPersonnelCap
             << " (objective finalized after seed)\n";
    } else {
      // Weighted: optional soft lex via large tardiness coefficient.
      const bool dueFirst = envInt("CP_DUE_FIRST", 0) != 0 ||
                            (weightTardiness > 0 &&
                             weightTardiness >= max(1, weightMakespan) * 5);
      if (dueFirst) {
        const long long tardyPrimary = max(
            1LL, earlyPersonnelCap * max(1, weightPersonnel) +
                     static_cast<long long>(horizon) + 1);
        LinearExpr dueObjective =
            LinearExpr::Term(totalTardy, tardyPrimary) +
            LinearExpr::Term(makespan, max(1, weightMakespan));
        if (hasEarlyPersonnel && weightPersonnel > 0)
          dueObjective -= LinearExpr::Term(earlyPersonnelVar, weightPersonnel);
        if (weightLoad > 0)
          dueObjective += LinearExpr::Term(maxLoad, weightLoad);
        model.Minimize(dueObjective);
        logger << "CP weighted due-first: tardiness primary, early-personnel "
               << "days=" << earlyDays << " weight=" << weightPersonnel
               << " cap=" << earlyPersonnelCap << ", makespan "
               << weightMakespan << ", load " << weightLoad << "\n";
      } else {
        model.Minimize(
            LinearExpr::Term(makespan, static_cast<long long>(weightMakespan) *
                                           demandCount) +
            LinearExpr::Term(maxLoad,
                             static_cast<long long>(weightLoad) * demandCount) +
            LinearExpr::Term(totalTardy, weightTardiness));
      }
    }
  } else {
    model.Minimize(makespan);
  }
  vector<CpChoice> seeded;
  const vector<CpChoice>* activeHint = hint;
  // Tasks that form complete, window-fitting baseline trees.  Stability must
  // only anchor these tasks: a partial tree cannot be selected by CP and
  // penalizing its unavoidable absence distorts the objective/change rate.
  unordered_set<int> anchoredTasks;
  // Seed from the committed MRP/FAS dates in both throughput (partial) and
  // full-horizon modes. Without this, all-mode CP restarts from an empty
  // hint and routinely worsens early util and lateness under a time limit.
  if (!activeHint && envInt("CP_SEED_MRP", 1) != 0) {
    // MRP/FAS has already built a schedule. Seed CP with every dated lot
    // that fits in the current search horizon.
    const Date origin = Plan::instance().getCurrent();
    const bool markedBaseline = envInt("CP_BASELINE_VARIABLE", 0) != 0;
    for (size_t index = 0; index < movable.size(); ++index) {
      OperationPlan* plan = movable[index].plan;
      if (!plan || movable[index].candidates.empty()) continue;
      // XOR head/tail are alternatives to the FAS whole; seed only whole so
      // preflight matches the original feasible FAS assignment.
      if (cpIsXorSplitPart(plan)) continue;
      // Supplemental candidates already have dates from their unconstrained
      // MRP generation. Those dates aren't part of the feasible MRP starting
      // solution. Hint them absent and seed only the marked baseline.
      if (markedBaseline && !movable[index].baseline) continue;
      unordered_set<Resource*> assignedResources;
      for (auto load = plan->beginLoadPlans(); load != plan->endLoadPlans();
           ++load) {
        if (!load->isStart() || load->getQuantity() <= 0) continue;
        if (load->getResource()) assignedResources.insert(load->getResource());
      }
      int candidate = 0;
      int bestScore = numeric_limits<int>::min();
      for (int indexCandidate = 0;
           indexCandidate < static_cast<int>(movable[index].candidates.size());
           ++indexCandidate) {
        const Candidate& option = movable[index].candidates[indexCandidate];
        if (option.operation != plan->getOperation()) continue;
        int score = 0;
        for (const CapacityUse& use : option.resources) {
          Resource* proposed = use.resource;
          bool match = assignedResources.count(proposed) != 0;
          if (!match)
            for (Resource* assigned : assignedResources)
              if (assigned && proposed &&
                  (assigned->isMemberOf(proposed) ||
                   proposed->isMemberOf(assigned))) {
                match = true;
                break;
              }
          score += match ? 10 : -100;
        }
        if (score > bestScore) {
          bestScore = score;
          candidate = indexCandidate;
        }
      }
      if (envInt("CP_LOG_HINT_MISMATCH", 0) != 0) {
        const Candidate& option = movable[index].candidates[candidate];
        for (const CapacityUse& use : option.resources)
          if (!assignedResources.count(use.resource))
            logger << "CP hint resource mismatch plan=" << plan->getReference()
                   << " proposed='" << use.resource->getName() << "'\n";
      }
      if (plan->getStart() < origin ||
          plan->getStart() == Date::infinitePast ||
          plan->getEnd() == Date::infiniteFuture)
        continue;
      const long start =
          static_cast<long>((plan->getStart() - origin).getSeconds());
      const long end =
          static_cast<long>((plan->getEnd() - origin).getSeconds());
      if (start < 0 || end < start || end > horizon) continue;
      // MRP start/end timestamps include closed-shift gaps.  CP intervals use
      // pure processing duration and enforce calendars separately.  Hinting
      // the elapsed MRP span contradicts the interval-duration equality and
      // makes a large baseline actively harmful to presolve.
      seeded.push_back({static_cast<int>(index), candidate, start,
                        movable[index].candidates[candidate].duration});
    }
    if (!seeded.empty()) {
      activeHint = &seeded;
      logger << "CP seeded " << seeded.size()
             << " lots from the current baseline plan\n";
      // Feasibility of this baseline seed (any source) is checked later by
      // fixing the CP hints and asking CP-SAT whether that assignment is a
      // feasible solution of the real model.
      unordered_set<int> seededTask;
      for (const CpChoice& choice : seeded) {
        if (choice.duration > horizon) continue;
        seededTask.insert(choice.task);
      }
      unordered_map<OperationPlan*, int> rootTasks, rootSeeded;
      for (size_t index = 0; index < movable.size(); ++index) {
        if (!movable[index].plan) continue;
        OperationPlan* root = cpChoiceRoot(movable[index].plan);
        rootTasks[root]++;
        if (seededTask.count(static_cast<int>(index))) rootSeeded[root]++;
      }
      unordered_set<int> baselineTasks;
      unordered_set<int> baselineJobs;
      size_t xorWholeRoots = 0, xorWholePartial = 0;
      for (const auto& entry : rootTasks) {
        if (!entry.first || cpSplitRole(entry.first) != "whole") continue;
        ++xorWholeRoots;
        const int seededCount = rootSeeded[entry.first];
        if (seededCount != entry.second) ++xorWholePartial;
      }
      if (xorWholeRoots)
        logger << "CP XOR split seed roots: whole=" << xorWholeRoots
               << " partial_or_absent=" << xorWholePartial << "\n";
      for (size_t index = 0; index < movable.size(); ++index) {
        if (!movable[index].plan || !seededTask.count(static_cast<int>(index)))
          continue;
        OperationPlan* root = cpChoiceRoot(movable[index].plan);
        if (rootTasks[root] == 0 || rootSeeded[root] != rootTasks[root]) continue;
        baselineTasks.insert(static_cast<int>(index));
        baselineJobs.insert(movable[index].job);
      }
      point.baselineJobs = static_cast<long>(baselineJobs.size());
      if (keepBaseline && !baselineTasks.empty()) {
        for (int index : baselineTasks) model.AddEquality(selected[index], 1);
        logger << "CP keeps " << baselineTasks.size() << " MRP lots\n";
      }
    } else {
      activeHint = &seeded;
      // When partial/window-select, the shared block below already writes a
      // full selected=false hint set. Adding zeros here first makes CP-SAT
      // reject the model as MODEL_INVALID (duplicate hints) — common for
      // local-insert where the new lot is not yet a dated FAS seed.
      if (!partial) {
        for (size_t index = 0; index < starts.size(); ++index) {
          model.AddHint(selected[index], 0);
          model.AddHint(starts[index], 0);
          model.AddHint(ends[index], 0);
          model.AddHint(spans[index], 0);
        }
        model.AddHint(makespan, 0);
        model.AddHint(maxLoad, 0);
        model.AddHint(totalTardy, 0);
      }
    }
  } else if (partial && !activeHint) {
    activeHint = &seeded;
    // Same as above: leave zero-hints to the shared partial block.
    logger << "CP seeded an empty schedule\n";
  }
  if (activeHint) {
    // Window-select / partial: only hint trees whose every schedulable step
    // fits the short horizon. Hinting a partial routing as selected=1 fights
    // the root shared-selected switch and makes fixed-hint preflight
    // infeasible even though a subset schedule exists.
    unordered_set<int> completeRootTasks;
    if (partial) {
      unordered_set<int> seededTask;
      for (const CpChoice& choice : *activeHint) {
        if (choice.task < 0 || choice.task >= static_cast<int>(starts.size()))
          continue;
        if (choice.duration > horizon) continue;
        if (choice.start < 0 || choice.start + choice.duration > horizon)
          continue;
        seededTask.insert(choice.task);
      }
      unordered_map<OperationPlan*, int> rootTasks, rootSeeded;
      for (size_t index = 0; index < movable.size(); ++index) {
        if (!movable[index].plan) continue;
        OperationPlan* root = cpChoiceRoot(movable[index].plan);
        rootTasks[root]++;
        if (seededTask.count(static_cast<int>(index))) rootSeeded[root]++;
      }
      for (size_t index = 0; index < movable.size(); ++index) {
        if (!movable[index].plan || !seededTask.count(static_cast<int>(index)))
          continue;
        OperationPlan* root = cpChoiceRoot(movable[index].plan);
        if (rootTasks[root] > 0 && rootSeeded[root] == rootTasks[root])
          completeRootTasks.insert(static_cast<int>(index));
      }
      logger << "CP window-select seed: complete-tree lots="
             << completeRootTasks.size() << "/" << activeHint->size() << "\n";
    }
    unordered_set<int> hintedTasks;
    for (const CpChoice& choice : *activeHint) {
      if (choice.task < 0 || choice.task >= static_cast<int>(starts.size()))
        continue;
      if (choice.start < 0 || choice.start > horizon) continue;
      long start = choice.start;
      const long duration = max(0L, choice.duration);
      if (duration > horizon) continue;
      if (start + duration > horizon) start = horizon - duration;
      if (start < 0) continue;
      if (partial && !completeRootTasks.count(choice.task)) continue;
      hintedTasks.insert(choice.task);
      anchoredTasks.insert(choice.task);
      model.AddHint(selected[choice.task], true);
      model.AddHint(starts[choice.task], start);
      model.AddHint(ends[choice.task], start + duration);
      model.AddHint(spans[choice.task], duration);
    }
    // Give CP a complete feasible incumbent: keep window-fitting trees and
    // drop the rest (advisory). Needed for partial even without baseline
    // expansion — otherwise unhinted selected vars leave preflight with no
    // full assignment.
    if (partial) {
      for (size_t index = 0; index < starts.size(); ++index) {
        if (hintedTasks.count(static_cast<int>(index))) continue;
        // XOR head/tail are hinted once below; duplicating them here makes
        // CP-SAT reject the whole model as MODEL_INVALID (duplicate hints).
        if (cpIsXorSplitPart(movable[index].plan)) continue;
        model.AddHint(selected[index], false);
        model.AddHint(starts[index], 0);
        model.AddHint(ends[index], 0);
        model.AddHint(spans[index], 0);
      }
    }
    // XOR head/tail: hint absent so preflight matches FAS whole seed.
    for (size_t index = 0; index < movable.size(); ++index) {
      if (!cpIsXorSplitPart(movable[index].plan)) continue;
      if (hintedTasks.count(static_cast<int>(index))) continue;
      model.AddHint(selected[index], false);
      model.AddHint(starts[index], 0);
      model.AddHint(ends[index], 0);
      model.AddHint(spans[index], 0);
    }
    for (size_t index = 0; index < presences.size(); ++index) {
      bool on = false;
      if (!partial || hintedTasks.count(presenceTask[index])) {
        for (const CpChoice& choice : *activeHint)
          if (choice.task == presenceTask[index] &&
              choice.candidate == presenceCandidate[index] &&
              (!partial || completeRootTasks.count(choice.task)))
            on = true;
      }
      model.AddHint(presences[index], on);
    }
  }
  if (partial && activeHint && !activeHint->empty() && !maximizePersonnel &&
      envInt("CP_CHANGE_RATE_ENABLED", 0) != 0 &&
      envInt("CP_ANCHOR_SEED", 1) != 0) {
    LinearExpr deviation(0);
    unordered_set<int> seenTasks;
    for (const CpChoice& choice : *activeHint) {
      if (choice.task < 0 || choice.task >= static_cast<int>(starts.size()) ||
          !anchoredTasks.count(choice.task) ||
          !seenTasks.insert(choice.task).second)
        continue;
      IntVar distance = model.NewIntVar(Domain(0, horizon));
      model.AddAbsEquality(
          distance, LinearExpr(starts[choice.task]) - choice.start);
      deviation += distance;
    }
    // Lexicographic objective: maximize selected lots first, then minimize
    // movement from the feasible MRP baseline. One additional lot outweighs
    // the largest possible total start-time deviation.
    const int64_t primary =
        static_cast<int64_t>(horizon) * max<size_t>(1, starts.size()) + 1;
    LinearExpr protectedThroughput(0);
    for (const auto& entry : jobSelected)
      protectedThroughput += LinearExpr::Term(entry.second, primary);
    model.Maximize(protectedThroughput - deviation);
  }
  if (deferLexObjective) {
    // Soft 3-level lex in one search (full time budget):
    //   (1) min demand tardiness (seconds, weighted)
    //   (2) max early-window personnel
    //   (3) min start-time deviation from FAS/MRP seed (改变率)
    //
    // Normalize all three objective tiers to hours. A single weighted
    // expression in raw seconds can overflow int64 on local runs whose
    // horizon is extended past frozen work: tardiness * personnel * seed
    // deviation produced coefficients around 6e13 on scenario9, which made
    // an otherwise feasible CP-SAT model MODEL_INVALID. The constraints stay
    // second-accurate; only the soft ranking objective is normalized.
    const long scale = 3600;
    const long long tardyCapScaled =
        max(1LL, (totalTardyCap + scale - 1) / scale);
    IntVar tardyScaled = model.NewIntVar(Domain(0, tardyCapScaled));
    model.AddDivisionEquality(tardyScaled, totalTardy, scale);
    long long earlyCapScaled = 0;
    IntVar earlyScaled = model.NewIntVar(Domain(0, 0));
    if (hasEarlyPersonnel) {
      earlyCapScaled = max(1LL, (earlyPersonnelCap + scale - 1) / scale);
      earlyScaled = model.NewIntVar(Domain(0, earlyCapScaled));
      model.AddDivisionEquality(earlyScaled, earlyPersonnelVar, scale);
    }
    // Tertiary: sum |start - seed_start| over seeded lots (CP_ANCHOR_SEED=1).
    LinearExpr deviation(0);
    int64_t maxDeviation = 0;
    IntVar deviationVar = model.NewIntVar(Domain(0, 0));
    IntVar deviationScaled = model.NewIntVar(Domain(0, 0));
    bool hasDeviation = false;
    if (envInt("CP_CHANGE_RATE_ENABLED", 0) != 0 &&
        activeHint && !activeHint->empty() &&
        envInt("CP_ANCHOR_SEED", 1) != 0) {
      unordered_set<int> seenTasks;
      for (const CpChoice& choice : *activeHint) {
        if (choice.task < 0 ||
            choice.task >= static_cast<int>(starts.size()) ||
            !anchoredTasks.count(choice.task) ||
            !seenTasks.insert(choice.task).second)
          continue;
        IntVar distance = model.NewIntVar(Domain(0, horizon));
        model.AddAbsEquality(
            distance, LinearExpr(starts[choice.task]) - choice.start);
        deviation += distance;
        maxDeviation += horizon;
      }
      if (maxDeviation > 0) {
        deviationVar = model.NewIntVar(Domain(0, maxDeviation));
        model.AddEquality(deviationVar, deviation);
        const int64_t maxDeviationScaled =
            (maxDeviation + scale - 1) / scale;
        deviationScaled =
            model.NewIntVar(Domain(0, maxDeviationScaled));
        model.AddDivisionEquality(deviationScaled, deviationVar, scale);
        maxDeviation = maxDeviationScaled;
        hasDeviation = true;
      }
    }
    // Coefficients: 1 early-minute beats all deviation; 1s tardiness beats
    // all early-minutes (+ deviation).
    const long long earlyPrimary =
        hasDeviation ? (maxDeviation + 1) : 1;
    const long long tardyPrimary =
        earlyCapScaled * earlyPrimary + 1;
    LinearExpr lexObjective =
        LinearExpr::Term(tardyScaled, max(1LL, tardyPrimary));
    if (hasEarlyPersonnel)
      lexObjective -= LinearExpr::Term(earlyScaled, earlyPrimary);
    if (hasDeviation) lexObjective += deviationScaled;
    model.Minimize(lexObjective);
    logger << "CP lex soft objective: tardiness >> early-personnel"
           << (hasDeviation ? " >> anchor-seed (改变率)" : "")
           << " resource='" << cpEarlyPersonnelResourceScope() << "'"
           << " days=" << envInt("CP_EARLY_DAYS", 3)
           << " objectiveUnit=" << scale << "s earlyCap=" << earlyCapScaled
           << " earlyPrimary=" << earlyPrimary
           << " tardyPrimary=" << tardyPrimary
           << " maxDevUnits=" << maxDeviation << "\n";
  }
  // Expanding hundreds of reservoir constraints creates a Boolean precedence
  // for nearly every pair of material events.  On the FC data set that turns
  // a 33k-variable model into more than 230k variables before search starts.
  const bool expandReservoir = envInt("CP_EXPAND_RESERVOIR", 0) != 0;
  const auto builtModel = model.Build();

  // Model preflight: fix whatever baseline seed is active (any source) and
  // ask CP-SAT if it is feasible under all CP constraints.
  //   hard — seed continue-optimize (normal FAS/MRP→CP): INFEASIBLE → block
  //   soft — local reschedule (CP_LOCAL_RESCHEDULE): log and keep searching
  // Soft problems() in Python is always advisory.
  if (activeHint && !activeHint->empty() &&
      envInt("CP_PREFLIGHT_MODEL", 1) != 0) {
    SatParameters preflight;
    const int preflightSeconds = max(
        1, envInt("CP_PREFLIGHT_SECONDS",
                  max(5, min(60, timeLimit > 0 ? timeLimit : 60))));
    preflight.set_max_time_in_seconds(preflightSeconds);
    preflight.set_num_workers(envInt("CP_WORKERS", 8));
    preflight.set_hint_conflict_limit(1000000);
    preflight.set_log_search_progress(envInt("CP_LOG_PROGRESS", 0) != 0);
    preflight.set_fix_variables_to_their_hinted_value(true);
    preflight.set_repair_hint(false);
    preflight.set_expand_reservoir_constraints(expandReservoir);
    auto preflightModel = builtModel;
    const int xorConstraintCount = xorConstraintEnd - xorConstraintStart;
    if (xorConstraintCount > 0) {
      // Preserve constraint indices: cumulative/no-overlap constraints refer
      // to interval constraints by index. Deleting entries would shift those
      // references and make the copied model invalid. Replace each XOR
      // relation in place with the tautology 0 == 0 instead.
      for (int index = xorConstraintStart; index < xorConstraintEnd; ++index) {
        auto* constraint = preflightModel.mutable_constraints(index);
        constraint->Clear();
        auto* linear = constraint->mutable_linear();
        linear->add_domain(0);
        linear->add_domain(0);
      }
      logger << "CP preflight core-only: neutralized " << xorConstraintCount
             << " XOR representation constraints\n";
    }
    const auto preflightResponse =
        SolveWithParameters(preflightModel, preflight);
    const bool seedFeasible =
        preflightResponse.status() == CpSolverStatus::OPTIMAL ||
        preflightResponse.status() == CpSolverStatus::FEASIBLE;
    const bool seedInfeasible =
        preflightResponse.status() == CpSolverStatus::INFEASIBLE;
    point.baselineConflicts = seedInfeasible ? 1 : 0;
    logger << "CP preflight model check: status="
           << static_cast<int>(preflightResponse.status())
           << " wall=" << preflightResponse.wall_time()
           << "s seed_feasible=" << (seedFeasible ? 1 : 0)
           << " conflicts=" << point.baselineConflicts << "\n";
    const bool localReschedule = localRescheduleEnabled();
    const bool abortHard =
        envInt("CP_PREFLIGHT_ABORT_ON_CONFLICT", 1) != 0;
    if (!seedFeasible) {
      // Hard/soft both surface the raw CP-SAT conflict payload.
      localSetPreflightConflicts(formatCpSolverConflicts(
          preflightResponse, &builtModel));
      logger << "CP preflight solver conflicts: " << localLastPreflightConflicts()
             << "\n"
             << flush;
    }
    if (seedInfeasible) {
      if (localReschedule) {
        // Reschedule repairs an already-conflicting plan — soft entry.
        logger << "CP preflight soft (local reschedule): baseline seed not "
                  "feasible; continuing search\n";
      } else if (abortHard) {
        // Seed continue-optimize — hard: refuse to search on a bad seed.
        logger << "CP preflight hard blocked: baseline seed is not a "
                  "feasible CP solution; do not start CP-SAT search\n";
        localSetStage("preflight_blocked");
        point.provenInfeasible = true;
        return point;
      } else {
        logger << "CP preflight: baseline seed not feasible; continuing "
                  "(CP_PREFLIGHT_ABORT_ON_CONFLICT=0)\n";
      }
    } else if (!seedFeasible) {
      if (!localReschedule && abortHard) {
        // UNKNOWN/MODEL_INVALID is not proof that the baseline is valid.
        // The normal FAS/MRP -> CP contract is "preflight clean, then
        // optimize", so don't silently enter the expensive search after an
        // inconclusive gate. Local rescheduling remains soft because its job
        // is precisely to repair an existing or uncertain baseline.
        if (localLastPreflightConflicts().empty()) {
          ostringstream detail;
          detail << "preflight inconclusive: status="
                 << static_cast<int>(preflightResponse.status())
                 << " wall=" << preflightResponse.wall_time() << "s";
          localSetPreflightConflicts(detail.str());
        }
        logger << "CP preflight hard blocked: baseline feasibility was not "
                  "proven (status="
               << static_cast<int>(preflightResponse.status()) << ")\n"
               << flush;
        localSetStage("preflight_blocked");
        return point;
      }
      logger << "CP preflight inconclusive (status="
             << static_cast<int>(preflightResponse.status())
             << "); continuing for local/advisory repair\n";
    }
  }

  SatParameters parameters;
  // CP_TIME_LIMIT_MS enables sub-second LNS workers (ALNS / neighborhood repair).
  double limitSeconds = max(1.0, static_cast<double>(timeLimit));
  if (const char* msEnv = getenv("CP_TIME_LIMIT_MS")) {
    try {
      limitSeconds = max(0.05, stod(string(msEnv)) / 1000.0);
    } catch (...) {
    }
  }
  parameters.set_max_time_in_seconds(limitSeconds);
  parameters.set_num_workers(envInt("CP_WORKERS", 8));
  parameters.set_hint_conflict_limit(1000000);
  parameters.set_log_search_progress(envInt("CP_LOG_PROGRESS", 0) != 0);
  parameters.set_fix_variables_to_their_hinted_value(
      envInt("CP_FIX_HINT", 0) != 0);
  // Keep hints advisory by default. Forced repair on FAS seeds with calendar
  // / material mismatches often jumps far from the baseline under a time limit.
  if (activeHint && !activeHint->empty())
    parameters.set_repair_hint(envInt("CP_REPAIR_HINT", 0) != 0);
  parameters.set_expand_reservoir_constraints(expandReservoir);
  // Keep partial hints advisory. Forced repair can fail on presolved shift models.
  // Trace improving solutions over wall time (for util / earlyPersonnel curves).
  const bool progressTrace = envInt("CP_PROGRESS_TRACE", 1) != 0;
  int progressCount = 0;
  ofstream progressFile;
  const char* progressPathEnv = getenv("CP_PROGRESS_CSV");
  string progressPath =
      progressPathEnv && progressPathEnv[0]
          ? string(progressPathEnv)
          : string("fc_data/20261016/cp_progress_trace.csv");
  if (progressTrace) {
    progressFile.open(progressPath.c_str(), ios::out | ios::trunc);
    if (progressFile)
      progressFile
          << "wall_s,objective,tardiness,early_personnel,early_fill_pct\n";
    logger << "CP progress trace: csv=" << progressPath << "\n" << flush;
  }
  Model cpSolveModel;
  cpSolveModel.Add(NewSatParameters(parameters));
  if (progressTrace) {
    cpSolveModel.Add(NewFeasibleSolutionObserver(
        [&](const CpSolverResponse& r) {
          ++progressCount;
          const long tardy = SolutionIntegerValue(r, totalTardy);
          const long early =
              hasEarlyPersonnel
                  ? SolutionIntegerValue(r, earlyPersonnelVar)
                  : 0L;
          double fillPct = 0.0;
          if (hasEarlyPersonnel && earlyPersonnelCap > 0)
            fillPct = 100.0 * static_cast<double>(early) /
                      static_cast<double>(earlyPersonnelCap);
          logger << "CP progress: n=" << progressCount
                 << " wall=" << fixed << setprecision(3) << r.wall_time()
                 << "s obj=" << setprecision(6) << r.objective_value()
                 << " tardiness=" << tardy
                 << " earlyPersonnel=" << early
                 << " earlyFill=" << setprecision(2) << fillPct << "%\n"
                 << flush;
          if (progressFile) {
            progressFile << setprecision(6) << r.wall_time() << ","
                         << r.objective_value() << "," << tardy << "," << early
                         << "," << setprecision(4) << fillPct << "\n";
            progressFile.flush();
          }
        }));
  }
  const auto response = SolveCpModel(builtModel, &cpSolveModel);
  logger << "CP-SAT solve returned status="
         << static_cast<int>(response.status())
         << " wall=" << response.wall_time() << "s\n"
         << flush;
  if (progressFile) {
    progressFile.close();
    logger << "CP progress trace: solutions=" << progressCount
           << " csv=" << progressPath << "\n" << flush;
  }
  point.feasible = response.status() == CpSolverStatus::OPTIMAL ||
                   response.status() == CpSolverStatus::FEASIBLE;
  point.provenInfeasible = response.status() == CpSolverStatus::INFEASIBLE;
  logger << "CP-SAT status=" << static_cast<int>(response.status())
         << " wall=" << response.wall_time() << "s"
         << " goal=" << (augmecon ? "augmecon" : cpGoalName(goal));
  if (loadCap >= 0) logger << " loadCap=" << loadCap;
  if (tardyCap >= 0) logger << " tardyCap=" << tardyCap;
  if (makespanCap >= 0) logger << " makespanCap=" << makespanCap;
  logger << "\n" << flush;
  if (response.status() == CpSolverStatus::MODEL_INVALID) {
    const string validate = operations_research::sat::ValidateCpModel(builtModel);
    logger << "CP-SAT MODEL_INVALID validate='" << validate << "'\n";
    if (!response.solution_info().empty())
      logger << "CP-SAT solution_info='" << response.solution_info() << "'\n";
  }
  if (!point.feasible) return point;
  point.makespan = SolutionIntegerValue(response, makespan);
  for (const auto& entry : jobSelected)
    if (SolutionBooleanValue(response,entry.second)) ++point.selectedJobs;
  point.maxLoad = SolutionIntegerValue(response, maxLoad);
  point.tardiness = SolutionIntegerValue(response, totalTardy);
  if (hasEarlyPersonnel)
    point.earlyPersonnel = SolutionIntegerValue(response, earlyPersonnelVar);
  // Audit: compare objective totalTardy vs schedule-implied lateness. A gap
  // means late vars floated above max(0, completion-due) (soft-lex slack).
  if (!lates.empty() &&
      (point.tardiness > 0 || envInt("CP_TARDY_AUDIT", 1) != 0)) {
    long long implied = 0;
    int positive = 0;
    for (size_t index = 0; index < lates.size(); ++index) {
      const long lateVal = SolutionIntegerValue(response, lates[index]);
      const long completion =
          SolutionIntegerValue(response, lateCompletions[index]);
      const long need = max(0L, completion - lateDue[index]);
      implied += static_cast<long long>(need) * lateWeight[index];
      if (lateVal > 0 || need > 0) {
        ++positive;
        if (positive <= 20)
          logger << "CP tardy detail: demand='" << lateNames[index]
                 << "' due_sec=" << lateDue[index]
                 << " completion_sec=" << completion
                 << " need_late=" << need
                 << " var_late=" << lateVal
                 << " weight=" << lateWeight[index] << "\n";
      }
    }
    if (point.tardiness > 0 || implied > 0 || positive > 0)
      logger << "CP tardy audit: objective=" << point.tardiness
             << " schedule_implied=" << implied
             << " positive_rows=" << positive << "/" << lates.size() << "\n";
    // Prefer schedule-implied lateness for lex compare / export reporting.
    if (implied != point.tardiness && implied >= 0 &&
        implied <= numeric_limits<long>::max()) {
      logger << "CP tardy audit: replacing objective tardiness="
             << point.tardiness << " with schedule_implied=" << implied
             << "\n";
      point.tardiness = static_cast<long>(implied);
    }
  }
  for (size_t index = 0; index < presences.size(); ++index) {
    if (!SolutionBooleanValue(response, presences[index])) continue;
    point.choices.push_back(
        {presenceTask[index], presenceCandidate[index],
         SolutionIntegerValue(response, starts[presenceTask[index]]),
         SolutionIntegerValue(response, spans[presenceTask[index]])});
  }
  // 改变率 vs FAS/MRP seed: share of anchored root work orders whose start
  // changed materially or whose selected tree was dropped.  A work order is
  // changed when any of its leaf tasks moves beyond the tolerance.  Grouping
  // by Task::job avoids reporting a multi-step routing as several orders.
  if (envInt("CP_CHANGE_RATE_ENABLED", 0) != 0 &&
      activeHint && !activeHint->empty()) {
    unordered_map<int, CpChoice> seedChoice;
    for (const CpChoice& choice : *activeHint) {
      if (choice.task < 0 || choice.task >= static_cast<int>(starts.size()))
        continue;
      if (!anchoredTasks.count(choice.task)) continue;
      if (!seedChoice.count(choice.task)) seedChoice[choice.task] = choice;
    }
    unordered_map<int, CpChoice> solChoice;
    for (const CpChoice& choice : point.choices)
      solChoice[choice.task] = choice;
    const long tolerance =
        max(0, envInt("CP_CHANGE_TOLERANCE_SECONDS", 60));
    const long mediumShift = max(
        tolerance + 1L, static_cast<long>(envInt("CP_CHANGE_MEDIUM_SECONDS", 900)));
    const long majorShift = max(
        mediumShift + 1L, static_cast<long>(envInt("CP_CHANGE_MAJOR_SECONDS", 7200)));
    struct JobChange {
      bool time = false;
      bool assignment = false;
      bool dropped = false;
      long maxShift = 0;
      double quantity = 0.0;
    };
    unordered_map<int, JobChange> jobChange;
    unordered_set<int> comparedJobs;
    long long absDev = 0;
    for (const auto& entry : seedChoice) {
      const int job = movable[entry.first].job;
      comparedJobs.insert(job);
      JobChange& detail = jobChange[job];
      if (detail.quantity <= 0.0) {
        OperationPlan* root = jobPlan(movable[entry.first].plan);
        detail.quantity = root ? max(0.0, root->getQuantity()) : 0.0;
      }
      auto found = solChoice.find(entry.first);
      if (found == solChoice.end()) {
        detail.dropped = true;
        absDev += llabs(static_cast<long long>(horizon));
        detail.maxShift = max(detail.maxShift, horizon);
        continue;
      }
      const long long delta =
          llabs(static_cast<long long>(found->second.start) - entry.second.start);
      const long long endDelta = llabs(
          static_cast<long long>(found->second.start + found->second.duration) -
          static_cast<long long>(entry.second.start + entry.second.duration));
      absDev += delta;
      detail.maxShift = max(
          detail.maxShift,
          static_cast<long>(min(max(delta, endDelta),
                                static_cast<long long>(numeric_limits<long>::max()))));
      if (delta > tolerance || endDelta > tolerance) detail.time = true;
      if (found->second.candidate != entry.second.candidate)
        detail.assignment = true;
    }
    const long compared = static_cast<long>(comparedJobs.size());
    long moved = 0, timeChanged = 0, assignmentChanged = 0, dropped = 0;
    long minor = 0, medium = 0, major = 0;
    long long sumJobMaxShift = 0;
    double totalQuantity = 0.0, changedQuantity = 0.0;
    for (const auto& entry : jobChange) {
      const JobChange& detail = entry.second;
      const bool changed = detail.time || detail.assignment || detail.dropped;
      totalQuantity += detail.quantity;
      if (detail.time) ++timeChanged;
      if (detail.assignment) ++assignmentChanged;
      if (detail.dropped) ++dropped;
      if (!changed) continue;
      ++moved;
      changedQuantity += detail.quantity;
      sumJobMaxShift += detail.maxShift;
      if (detail.maxShift >= majorShift) ++major;
      else if (detail.maxShift >= mediumShift) ++medium;
      else ++minor;
    }
    unordered_set<int> solutionJobs;
    for (const auto& entry : solChoice)
      if (entry.first >= 0 &&
          entry.first < static_cast<int>(movable.size()))
        solutionJobs.insert(movable[entry.first].job);
    long added = 0;
    for (int job : solutionJobs)
      if (!comparedJobs.count(job)) ++added;
    point.changeCompared = compared;
    point.changeMoved = moved;
    point.changeAbsDev = absDev;
    const double changeRate =
        compared > 0 ? 100.0 * static_cast<double>(moved) /
                           static_cast<double>(compared)
                     : 0.0;
    logger << "CP change rate: " << fixed << setprecision(1) << changeRate
           << "% (" << moved << "/" << compared
           << " work orders changed vs seed, tolerance=" << tolerance
           << "s), absStartDev=" << absDev << "s"
           << " avgChangedJobMaxShift=" << setprecision(1)
           << (moved > 0 ? static_cast<double>(sumJobMaxShift) /
                               static_cast<double>(moved)
                         : 0.0)
           << "s quantityWeighted="
           << (totalQuantity > 0.0 ? 100.0 * changedQuantity / totalQuantity
                                   : 0.0)
           << "%\n";
    logger << "CP change detail: unchanged=" << max(0L, compared - moved)
           << " time_changed=" << timeChanged
           << " assignment_changed=" << assignmentChanged
           << " dropped=" << dropped << " added=" << added
           << " shift_buckets=(>" << tolerance << "s:" << minor
           << ", >=" << mediumShift << "s:" << medium
           << ", >=" << majorShift << "s:" << major
           << ") (reason counts may overlap)\n";
  }
  logger << "CP-SAT point makespan=" << point.makespan
         << " maxLoad=" << point.maxLoad
         << " tardiness=" << point.tardiness;
  if (hasEarlyPersonnel)
    logger << " earlyPersonnel=" << point.earlyPersonnel;
  if (point.changeCompared > 0)
    logger << " changeRate=" << fixed << setprecision(1)
           << (100.0 * static_cast<double>(point.changeMoved) /
               static_cast<double>(point.changeCompared))
           << "%";
  logger << "\n";
  logger << "CP response decode: return\n" << flush;
  return point;
}

void exportCpPoint(const vector<Task>& movable, const CpPoint& point) {
  logger << "CP export: begin choices=" << point.choices.size()
         << " movable=" << movable.size() << "\n"
         << flush;
  cpSetLastRunApplied(point.choices.size());
  unordered_set<OperationPlan*> chosenRoots;
  unordered_set<OperationPlan*> modelRoots;
  for (const Task& task : movable)
    if (task.plan) modelRoots.insert(cpChoiceRoot(task.plan));
  for (const CpChoice& choice : point.choices) {
    if (choice.task < 0 || choice.task >= static_cast<int>(movable.size()))
      continue;
    if (movable[choice.task].plan)
      chosenRoots.insert(cpChoiceRoot(movable[choice.task].plan));
  }
  logger << "CP export: chosenRoots=" << chosenRoots.size()
         << " xor=" << g_cpXorSplits.size() << "\n"
         << flush;
  if (point.partial) {
    auto baselineMarked = [](OperationPlan* plan) {
      return plan && plan->getApproved() && !plan->getConfirmed() &&
             !plan->getCompleted() && !plan->getClosed();
    };
    // Firm (confirmed/completed/closed) trees stay. The approved baseline
    // marker alone must NOT protect a lot that CP left unselected — keeping
    // its old dates double-books capacity against newly placed neighbors.
    auto firmTree = [&](OperationPlan* root) {
      if (!root) return false;
      if (firmLocked(root)) return true;
      for (OperationPlan::iterator child(root); child != root->end(); ++child)
        if (firmLocked(&*child)) return true;
      return false;
    };
    unordered_set<OperationPlan*> chosen;
    for (const CpChoice& choice : point.choices)
      chosen.insert(cpChoiceRoot(movable[choice.task].plan));
    unordered_set<OperationPlan*> skipped;
    for (const Task& task : movable) {
      OperationPlan* root = cpChoiceRoot(task.plan);
      // XOR alternatives remain live until the partial cleanup is complete.
      // Their selected/unselected deletion is owned by exportXorSplitChoices.
      if (cpIsXorSplitRoot(root)) continue;
      // Unselected model roots: remove even when approved (baseline marker).
      if (!chosen.count(root) && !firmLocked(root) && !firmTree(root))
        skipped.insert(root);
    }
    // Remove empty or unsupported proposals outside the selected trees.
    // Positive unconstrained manufacturing is now included in the CP model.
    size_t unsupported = 0;
    for (auto plan = OperationPlan::begin(); plan != OperationPlan::end();
         ++plan) {
      Operation* operation = plan->getOperation();
      if (!operation || firmLocked(&*plan) ||
          !operation->hasType<OperationTimePer, OperationFixedTime>())
        continue;
      OperationPlan* root = cpChoiceRoot(&*plan);
      if (cpIsXorSplitRoot(root)) continue;
      if (chosen.count(root) || firmTree(root) || skipped.count(root)) continue;
      // Outside-model approved baseline (not a CP task this solve) stays.
      if (baselineMarked(&*plan) && !modelRoots.count(root)) continue;
      if (plan->getQuantity() <= ROUNDING_ERROR ||
          operation->getName().compare(0, 6, "FC_OP_") == 0) {
        bool hasFirmChild = false;
        for (OperationPlan::iterator child(root); child != root->end(); ++child)
          if (firmLocked(&*child)) hasFirmChild = true;
        if (!hasFirmChild) {
          skipped.insert(root);
          ++unsupported;
        }
      }
    }
    logger << "CP partial omitted unsupported/empty trees=" << unsupported
           << "\n";
    size_t protectedSkips = 0;
    size_t removed = 0;
    for (OperationPlan* root : skipped) {
      if (firmTree(root)) {
        ++protectedSkips;
        continue;
      }
      try {
        // CommandDeleteOperationPlan treats approved as locked; demote the
        // baseline marker so an unselected CP decision can actually go away.
        if (root->getApproved()) root->setProposed(true);
        for (OperationPlan::iterator child(root); child != root->end(); ++child)
          if (child->getApproved()) child->setProposed(true);
        // Prefer tryDeleteOpPlan: skips hard-delete when Python still holds
        // refs (candidate expansion), avoiding exit-time heap corruption.
        tryDeleteOpPlan(root);
        ++removed;
      } catch (const DataException&) {
        // A locked descendant can be hidden below an alternate/routing node.
        // Preserve that complete tree instead of aborting the valid CP result.
        ++protectedSkips;
      }
    }
    logger << "CP partial removed=" << removed
           << " unselected trees; deferred=" << skipped.size() << "\n";
    if (protectedSkips)
      logger << "CP partial preserved=" << protectedSkips
             << " trees containing firm locked work\n";
  }
  if (!g_cpXorSplits.empty())
    exportXorSplitChoices(chosenRoots, modelRoots);
  logger << "CP export: XOR finalized\n" << flush;
  // Build placement rows AFTER XOR mutation. Cut clones may have been deleted
  // (or activated); placing from pre-XOR pointers segfaults.
  vector<Placement> rows;
  for (const CpChoice& choice : point.choices) {
    if (choice.task < 0 ||
        choice.task >= static_cast<int>(movable.size()) ||
        choice.candidate < 0 ||
        choice.candidate >=
            static_cast<int>(movable[choice.task].candidates.size()))
      continue;
    OperationPlan* plan = movable[choice.task].plan;
    if (!plan) continue;
    // With CP_XOR_ACTIVATE_CUTS=0, chosen cuts are dropped and FAS wholes
    // kept — never place deleted head/tail pointers.
    if (cpIsXorSplitPart(plan)) continue;
    const Candidate& candidate =
        movable[choice.task].candidates[choice.candidate];
    rows.push_back({plan, candidate.resource, candidate.operation,
                    static_cast<int>(choice.duration), choice.start,
                    candidate.resources});
  }
  logger << "CP export: place rows=" << rows.size() << "\n" << flush;
  placeOnCalendars(rows, point.partial, point.horizon);
}

void scheduleWithCp(vector<Task>& movable,
                    const vector<tuple<int, long, long>>& occupancy,
                    const vector<OperationPlan*>& jobs, long horizon,
                    Date origin) {
  unordered_map<int, vector<size_t>> byJob;
  for (size_t index = 0; index < movable.size(); ++index)
    byJob[movable[index].job].push_back(index);
  unordered_map<const string*, Demand*> demandByBatch;
  for (auto plan = OperationPlan::begin(); plan != OperationPlan::end();
       ++plan) {
    Demand* demand = plan->getDemand();
    if (!demand || !plan->getBatch()) continue;
    const string* key = &static_cast<const string&>(plan->getBatch());
    auto [found, inserted] = demandByBatch.emplace(key, demand);
    if (!inserted && demand->getDue() < found->second->getDue())
      found->second = demand;
  }
  unordered_map<Demand*, size_t> groupOf;
  vector<CpDemand> demands;
  int datedLots = 0;
  for (size_t job = 0; job < jobs.size(); ++job) {
    Demand* demand = jobs[job]->getDemand();
    if (!demand && jobs[job]->getBatch()) {
      auto found = demandByBatch.find(
          &static_cast<const string&>(jobs[job]->getBatch()));
      if (found != demandByBatch.end()) demand = found->second;
    }
    unordered_set<Demand*> linked;
    if (demand) linked.insert(demand);
    // Stock production has no direct demand pointer or batch. Follow its
    // downstream material links, including co-products serving several orders.
    for (PeggingIterator peg(jobs[job], true); peg; ++peg) {
      OperationPlan* downstream = peg.getOperationPlan();
      if (downstream && downstream->getDemand()) linked.insert(downstream->getDemand());
    }
    if (linked.empty()) continue;
    ++datedLots;
    for (Demand* linkedDemand : linked) {
      const Date due = linkedDemand->getDue();
      if (due == Date::infinitePast || due == Date::infiniteFuture) continue;
      long seconds = max(0L, static_cast<long>((due - origin).getSeconds()));
      int weight = linkedDemand->getPriority() > 0.0
          ? max(1, static_cast<int>(linkedDemand->getPriority()+0.5)) : 1;
      auto [group, inserted] = groupOf.emplace(linkedDemand, demands.size());
      if (inserted) {
        CpDemand entry;
        entry.due = seconds;
        entry.weight = weight;
        entry.name = linkedDemand->getName();
        demands.push_back(std::move(entry));
      }
      demands[group->second].jobs.push_back(static_cast<int>(job));
    }
  }
  logger << "CP demand dues: " << demands.size() << " demands from "
         << datedLots << "/" << jobs.size() << " lots\n";
  const int timeLimit = envInt("CP_TIME_LIMIT_SECONDS", 60);
  const string method = cpObjectiveMethod();
  bool fc = false;
  for (const Task& task : movable)
    if (task.plan->getOperation()->getName().compare(0,6,"FC_OP_") == 0) fc = true;
  string mode = envString("CP_PLANNING_MODE","auto");
  if (mode == "auto") mode = fc ? "throughput" : "all";
  // XOR split is a representation choice for a complete order, not a license
  // to discard work outside the three-day score window.
  if (!g_cpXorSplits.empty()) mode = "all";
  if (mode == "throughput") {
    horizon = static_cast<long>(max(1,min(365,envInt("CP_HORIZON_DAYS",3)))) * 86400L;
    logger << "CP planning mode: throughput, window=" << origin << " to "
           << origin+Duration(horizon) << ", candidate lots=" << jobs.size() << "\n";
    // With lexicographic: keep window-select (optional trees in the short
    // horizon) but optimize Tardiness >> EarlyPersonnel — "due-in-window
    // on time first, then personnel fill". Pure throughput packing is only
    // used when objective_method is not lexicographic.
    if (method == "lexicographic") {
      logger << "CP throughput+lex: window select, "
                "Tardiness(window) >> EarlyPersonnel\n";
    } else {
      const bool keepMrp = envInt("CP_KEEP_MRP", 1) != 0;
      CpPoint point = solveCpPoint(movable,occupancy,byJob,demands,horizon,
                                  CpGoal::Throughput,-1,-1,-1,timeLimit,nullptr,
                                  nullptr,keepMrp);
      const long baseline = point.baselineJobs;
      if (point.provenInfeasible && keepMrp) {
        logger << "CP MRP baseline does not fit; scheduling a feasible subset\n";
        point = solveCpPoint(movable,occupancy,byJob,demands,horizon,
                            CpGoal::Throughput,-1,-1,-1,timeLimit,nullptr);
      }
      if (!point.feasible) {
        logger << "CP kept the MRP baseline\n";
        return;
      }
      logger << "CP throughput selected=" << point.selectedJobs << "/" << jobs.size()
             << " lots, operations=" << point.choices.size() << "/" << movable.size()
             << "\n";
      // A time-limited search can return an early feasible solution with far
      // fewer lots than MRP. Keep the MRP plan unless CP covers at least as many.
      if (keepMrp && baseline > 0 && point.selectedJobs < baseline) {
        logger << "CP throughput " << point.selectedJobs
               << " is below the MRP baseline " << baseline
               << "; schedule was not applied\n";
        return;
      }
      if (envInt("CP_DRY_RUN", 0) != 0) {
        logger << "CP dry run; feasible schedule was not applied\n";
        return;
      }
      exportCpPoint(movable,point);
      return;
    }
  }
  logger << "CP objective method: " << method << "\n" << flush;
  if (method == "weighted") {
    CpPoint point = solveCpPoint(movable, occupancy, byJob, demands,
                                 horizon, CpGoal::Weighted, -1, -1, -1,
                                 timeLimit, nullptr);
    if (!point.feasible) {
      logger << "CP-SAT schedule was not applied\n";
      return;
    }
    const long demandCount = max(1L, static_cast<long>(demands.size()));
    logger << "CP weighted export makespan=" << point.makespan
           << " maxLoad=" << point.maxLoad
           << " demandTardinessSum=" << point.tardiness
           << " demandTardinessAvg=" << point.tardiness / demandCount << "\n";
    exportCpPoint(movable, point);
    return;
  }
  if (method == "lexicographic") {
    // Two-level hierarchy Tardiness → EarlyPersonnel.
    // Preflight owns baseline conflict rejection; lex never forces hint repair.
    // Always score the FAS/MRP baseline first; reject CP if it is lex-worse
    // so a time-limited search cannot destroy a strong seed.
    //
    // Pin the search to the util window: Plan::current .. +CP_HORIZON_DAYS
    // (default 3 → e.g. 2026-10-16 08:00 to 2026-10-19 08:00). Without this,
    // planning_mode=all expands horizon via calendar openUntil and 3-day util
    // collapses even while earlyPersonnel only scores the short window.
    const long utilDays = max(1, min(365, envInt("CP_HORIZON_DAYS", 3)));
    horizon = utilDays * 86400L;
    logger << "CP lex util-window=" << origin << " to "
           << origin + Duration(horizon) << " (" << utilDays << "d)\n";
    const long earlyDays = cpEarlyDays(true);
    const CpPoint baseline =
        scoreCommittedBaseline(movable, byJob, demands, earlyDays);
    logger << "CP lex baseline (FAS/MRP) tardiness=" << baseline.tardiness
           << " earlyPersonnel=" << baseline.earlyPersonnel << "\n";
    auto maybeExport = [&](const CpPoint& chosen) {
      logger << "CP maybeExport: feasible=" << chosen.feasible
             << " conflicts=" << chosen.baselineConflicts
             << " tardy=" << chosen.tardiness
             << " early=" << chosen.earlyPersonnel << "\n"
             << flush;
      if (chosen.baselineConflicts > 0 && chosen.feasible) {
        logger << "CP baseline has " << chosen.baselineConflicts
               << " preflight conflicts; accepting the feasible CP repair "
                  "instead of retaining the conflicting baseline\n"
               << flush;
        exportCpPoint(movable, chosen);
        return;
      }
      if (!lexAcceptsOverBaseline(chosen, baseline)) {
        logger << "CP lex kept FAS/MRP baseline; rejected CP tardiness="
               << chosen.tardiness << " earlyPersonnel="
               << chosen.earlyPersonnel << "\n"
               << flush;
        return;
      }
      logger << "CP lexicographic export tardiness=" << chosen.tardiness
             << " earlyPersonnel=" << chosen.earlyPersonnel
             << " makespan=" << chosen.makespan
             << " maxLoad=" << chosen.maxLoad;
      if (chosen.changeCompared > 0)
        logger << " changeRate=" << fixed << setprecision(1)
               << (100.0 * static_cast<double>(chosen.changeMoved) /
                   static_cast<double>(chosen.changeCompared))
               << "% (" << chosen.changeMoved << "/" << chosen.changeCompared
               << ")";
      logger << "\n" << flush;
      exportCpPoint(movable, chosen);
    };
    // Preflight already rejected conflicting FAS/MRP baselines. Do not force
    // hint repair on a clean seed — that drifts away from FAS under a time
    // limit. Default: one soft-lex solve
    // Tardiness >> EarlyPersonnel, with optional on-demand anchor-seed
    // stability/change-rate. CP_LEX_STAGES=2 = sequential min-tardy then
    // soft-lex (still no repair_hint). =3 = hard two-stage.
    const int stages = max(1, envInt("CP_LEX_STAGES", 1));
    if (stages == 1) {
      logger << "CP lexicographic: soft-lex Tardiness >> EarlyPersonnel"
             << (envInt("CP_CHANGE_RATE_ENABLED", 0) != 0 &&
                         envInt("CP_ANCHOR_SEED", 1) != 0
                     ? " >> anchor-seed/change-rate"
                     : " (change-rate off)")
             << " (single solve, no hint repair, window-select "
             << utilDays << "d)\n";
      CpPoint point = solveCpPoint(movable, occupancy, byJob, demands, horizon,
                                   CpGoal::LexTardyEarly, -1, -1, -1, timeLimit,
                                   nullptr);
      logger << "CP after solveCpPoint feasible=" << point.feasible
             << " choices=" << point.choices.size() << "\n"
             << flush;
      if (!point.feasible) {
        logger << "CP-SAT schedule was not applied\n";
        return;
      }
      maybeExport(point);
      return;
    }
    if (stages >= 3) {
      logger << "CP lexicographic: Tardiness -> EarlyPersonnel (2 hard stages)\n";
      const int stage1 = max(1, (timeLimit * 2 + 2) / 3);
      const int stage2 = max(1, timeLimit - stage1 + timeLimit / 3);
      CpPoint tardy = solveCpPoint(movable, occupancy, byJob, demands, horizon,
                                   CpGoal::Tardiness, -1, -1, -1, stage1,
                                   nullptr);
      if (!tardy.feasible) {
        logger << "CP-SAT schedule was not applied\n";
        return;
      }
      CpPoint early = solveCpPoint(
          movable, occupancy, byJob, demands, horizon, CpGoal::EarlyPersonnel,
          -1, tardy.tardiness, -1, stage2, &tardy.choices);
      early.baselineConflicts = tardy.baselineConflicts;
      maybeExport(early.feasible ? early : tardy);
      return;
    }
    // stages == 2: sequential hierarchy without forced hint repair.
    const int stage1 = max(15, timeLimit / 3);
    const int stage2 = max(15, timeLimit - stage1);
    logger << "CP lexicographic: min tardiness " << stage1
           << "s then soft-lex " << stage2 << "s (no hint repair)\n";
    const char* previousRepair = getenv("CP_REPAIR_HINT");
    setenv("CP_REPAIR_HINT", "0", 1);
    CpPoint tardyFirst =
        solveCpPoint(movable, occupancy, byJob, demands, horizon,
                     CpGoal::Tardiness, -1, -1, -1, stage1, nullptr);
    if (!tardyFirst.feasible) {
      if (previousRepair)
        setenv("CP_REPAIR_HINT", previousRepair, 1);
      else
        unsetenv("CP_REPAIR_HINT");
      logger << "CP lex stage-1 found no feasible schedule; keeping baseline\n";
      return;
    }
    logger << "CP lex stage-1 tardiness=" << tardyFirst.tardiness
           << " choices=" << tardyFirst.choices.size() << "\n";
    CpPoint improved = solveCpPoint(
        movable, occupancy, byJob, demands, horizon, CpGoal::LexTardyEarly, -1,
        -1, -1, stage2, &tardyFirst.choices);
    improved.baselineConflicts = tardyFirst.baselineConflicts;
    if (previousRepair)
      setenv("CP_REPAIR_HINT", previousRepair, 1);
    else
      unsetenv("CP_REPAIR_HINT");
    const CpPoint& chosen =
        (improved.feasible &&
         lexAcceptsOverBaseline(improved, tardyFirst))
            ? improved
            : tardyFirst;
    maybeExport(chosen);
    return;
  }

  const int slice = max(1, envInt("CP_EPSILON_SLICE_SECONDS", 15));
  const int budget = max(3, envInt("CP_EPSILON_BUDGET", 12));
  logger << "CP AUGMECON primary=makespan eps=1e-4\n";
  CpPoint minSpan = solveCpPoint(movable, occupancy, byJob, demands,
                                 horizon, CpGoal::Makespan, -1, -1, -1, slice,
                                 nullptr);
  const vector<CpChoice>* payoffHint =
      minSpan.feasible ? &minSpan.choices : nullptr;
  CpPoint minLoad = solveCpPoint(movable, occupancy, byJob, demands,
                                 horizon, CpGoal::Load, -1, -1, -1, slice,
                                 payoffHint);
  CpPoint minTardyPoint = solveCpPoint(movable, occupancy, byJob, demands,
                                       horizon, CpGoal::Tardiness, -1, -1, -1,
                                       slice, payoffHint);
  if (!minSpan.feasible && !minLoad.feasible && !minTardyPoint.feasible) {
    logger << "CP-SAT schedule was not applied\n";
    return;
  }
  vector<CpPoint> points;
  if (minSpan.feasible) points.push_back(minSpan);
  if (minLoad.feasible) points.push_back(minLoad);
  if (minTardyPoint.feasible) points.push_back(minTardyPoint);
  long loadLo = points[0].maxLoad;
  long loadHi = points[0].maxLoad;
  long tardyLo = points[0].tardiness;
  long tardyHi = points[0].tardiness;
  for (const CpPoint& point : points) {
    loadLo = min(loadLo, point.maxLoad);
    loadHi = max(loadHi, point.maxLoad);
    tardyLo = min(tardyLo, point.tardiness);
    tardyHi = max(tardyHi, point.tardiness);
  }
  logger << "CP AUGMECON range load=" << loadLo << ".." << loadHi
         << " tardiness=" << tardyLo << ".." << tardyHi << "\n";
  CpAugment augment;
  augment.loadRange = max(1L, loadHi - loadLo);
  augment.tardyRange = max(1L, tardyHi - tardyLo);
  vector<long> tried;
  auto already = [&](long cap) {
    for (long previous : tried)
      if (llabs(previous - cap) <= 1) return true;
    return false;
  };
  const int grid = 7;
  const long step = max(1L, (loadHi - loadLo) / max(1, grid - 1));
  for (long cap = loadHi;
       cap >= loadLo && static_cast<int>(points.size()) < budget;) {
    if (already(cap)) {
      cap -= step;
      continue;
    }
    tried.push_back(cap);
    const vector<CpChoice>* hint =
        points.empty() ? nullptr : &points.back().choices;
    CpPoint point = solveCpPoint(movable, occupancy, byJob, demands,
                                 horizon, CpGoal::Makespan, cap, tardyHi, -1,
                                 slice, hint, &augment);
    if (!point.feasible) {
      if (point.provenInfeasible) {
        logger << "CP AUGMECON stop: tighter load caps are infeasible\n";
        break;
      }
      cap -= step;
      continue;
    }
    points.push_back(point);
    cap = min(cap - step, point.maxLoad - step);
  }
  const long refMakespan = max(minSpan.feasible ? minSpan.makespan : 0L,
                               minLoad.feasible ? minLoad.makespan : 0L) +
                           1;
  const long refLoad = loadHi + 1;
  const double makespanSpan = max(1.0, static_cast<double>(refMakespan));
  const double loadSpan = max(1.0, static_cast<double>(loadHi - loadLo));
  while (static_cast<int>(points.size()) < budget) {
    vector<CpPoint> curve;
    for (const CpPoint& point : points)
      if (point.feasible) curve.push_back(point);
    sort(curve.begin(), curve.end(), [](const CpPoint& left, const CpPoint& right) {
      if (left.maxLoad != right.maxLoad) return left.maxLoad < right.maxLoad;
      return left.makespan < right.makespan;
    });
    double widest = 0.0;
    long leftLoad = -1;
    long rightLoad = -1;
    for (size_t index = 1; index < curve.size(); ++index) {
      const double gap = hypot(
          (curve[index].makespan - curve[index - 1].makespan) / makespanSpan,
          (curve[index].maxLoad - curve[index - 1].maxLoad) / loadSpan);
      if (gap > widest) {
        widest = gap;
        leftLoad = curve[index - 1].maxLoad;
        rightLoad = curve[index].maxLoad;
      }
    }
    if (widest < 0.005 || rightLoad - leftLoad <= 1) break;
    const long cap = (leftLoad + rightLoad) / 2;
    if (already(cap)) break;
    tried.push_back(cap);
    const double before = frontHypervolume(points, refMakespan, refLoad);
    const vector<CpChoice>* hint =
        points.empty() ? nullptr : &points.back().choices;
    CpPoint point = solveCpPoint(movable, occupancy, byJob, demands,
                                 horizon, CpGoal::Makespan, cap, tardyHi, -1,
                                 slice, hint, &augment);
    if (!point.feasible) break;
    points.push_back(point);
    const double after = frontHypervolume(points, refMakespan, refLoad);
    if (after > 0.0 && (after - before) / after < 0.001) break;
  }

  vector<CpPoint> front;
  for (const CpPoint& point : points) {
    if (!point.feasible) continue;
    bool dominated = false;
    for (const CpPoint& other : points) {
      if (!other.feasible || &other == &point) continue;
      if (dominates(other, point)) dominated = true;
    }
    if (!dominated) front.push_back(point);
  }
  logger << "CP augmecon front: " << front.size() << " points\n";
  for (const CpPoint& point : front)
    logger << "CP augmecon pareto makespan=" << point.makespan
           << " maxLoad=" << point.maxLoad
           << " tardiness=" << point.tardiness << "\n";
  if (front.empty()) {
    logger << "CP-SAT schedule was not applied\n";
    return;
  }
  auto norm = [](long value, long low, long high) {
    if (high <= low) return 0.0;
    return static_cast<double>(value - low) / static_cast<double>(high - low);
  };
  long minMakespan = front[0].makespan, maxMakespan = front[0].makespan;
  long minLoadSeen = front[0].maxLoad, maxLoadSeen = front[0].maxLoad;
  long minTardy = front[0].tardiness, maxTardy = front[0].tardiness;
  for (const CpPoint& point : front) {
    minMakespan = min(minMakespan, point.makespan);
    maxMakespan = max(maxMakespan, point.makespan);
    minLoadSeen = min(minLoadSeen, point.maxLoad);
    maxLoadSeen = max(maxLoadSeen, point.maxLoad);
    minTardy = min(minTardy, point.tardiness);
    maxTardy = max(maxTardy, point.tardiness);
  }
  const CpPoint* selected = &front[0];
  double bestScore = numeric_limits<double>::max();
  for (const CpPoint& point : front) {
    const double score =
        0.80 * norm(point.makespan, minMakespan, maxMakespan) +
        0.10 * norm(point.maxLoad, minLoadSeen, maxLoadSeen) +
        0.10 * norm(point.tardiness, minTardy, maxTardy);
    if (score < bestScore) {
      bestScore = score;
      selected = &point;
    }
  }
  logger << "CP augmecon export makespan=" << selected->makespan
         << " maxLoad=" << selected->maxLoad
         << " tardiness=" << selected->tardiness
         << " score=" << bestScore << "\n";
  exportCpPoint(movable, *selected);
}

#endif


}  // namespace


PyObject* cp_local_snapshot(PyObject*, PyObject*) {
  localCascadeSnapshot();
  Py_RETURN_NONE;
}

PyObject* cp_local_restore(PyObject*, PyObject*) {
  localCascadeRestore();
  Py_RETURN_NONE;
}

PyObject* cp_local_snapshot_clear(PyObject*, PyObject*) {
  localCascadeSnapshotClear();
  Py_RETURN_NONE;
}

PyObject* run_cp(PyObject*, PyObject*) {
#ifndef HAVE_ORTOOLS
  logger << "CP-SAT skipped: this engine was built without OR-Tools\n";
  Py_RETURN_NONE;
#else
  // Always clear before local freeze/skip so a Python cascade cannot reuse a
  // stale applied count / ok flag from a previous solve (or from a no-op skip).
  cpSetLastRunApplied(0);
  localSetLastOk(0);
  localSetStage("local_scope");
  // Shared local scope (hot/freeze). Algorithms only repair the movable set.
  LocalScopeGuard localFreeze;
  applyLocalRescheduleScope(localFreeze);
  if (localFreeze.skipOptimization) {
    if (localLastStage() != "local_l1_lock_all" &&
        localLastStage() != "local_delete_complete" &&
        localLastStage() != "local_delete_not_applied")
      localSetStage("local_scope_empty");
    Py_RETURN_NONE;
  }
  // Soft problems() logged in Python. Model preflight after Build():
  // hard for seed continue-optimize, soft for local reschedule.
  // No order-qty / coverage / personnel / fixed-task gate here.
  localSetStage("collect_tasks");
  // Optional one-split alts: whole XOR (head+tail). Idempotent for partition.
  prepareCpXorSplits();

  vector<Task> movable;
  vector<tuple<Resource*, Date, Date>> lockedWindows;
  unordered_map<OperationPlan*, int> jobIndex;
  vector<OperationPlan*> jobs;
  size_t xorSchedHead = 0, xorSchedTail = 0;
  size_t xorLockedHead = 0, xorLockedTail = 0;
  unordered_set<OperationPlan*> collected;

  auto collectTask = [&](OperationPlan* plan) {
    if (!plan || !collected.insert(plan).second || !schedulable(plan)) return;
    if (partitionExcluded(plan)) return;
    const string splitRole = cpSplitRole(plan);
    if (splitRole == "head") ++xorSchedHead;
    else if (splitRole == "tail") ++xorSchedTail;
    const bool baseline = envInt("CP_BASELINE_VARIABLE", 0) != 0 &&
                          plan->getApproved() && !plan->getConfirmed() &&
                          !plan->getCompleted() && !plan->getClosed();
    if (locked(plan)) {
      if (splitRole == "head") ++xorLockedHead;
      else if (splitRole == "tail") ++xorLockedTail;
      for (auto load = plan->beginLoadPlans(); load != plan->endLoadPlans(); ++load) {
        if (load->isStart() && isLeafResource(load->getResource()))
          lockedWindows.emplace_back(load->getResource(), plan->getStart(),
                                     plan->getEnd());
      }
      return;
    }
    Task task;
    task.plan = plan;
    task.baseline = baseline;
    OperationPlan* job = jobPlan(plan);
    auto inserted = jobIndex.emplace(job, static_cast<int>(jobs.size()));
    if (inserted.second) jobs.push_back(job);
    task.job = inserted.first->second;
    task.sequence = sequencePriority(plan, job);
    task.candidates = taskCandidates(plan);
    if (!task.candidates.empty()) movable.push_back(std::move(task));
  };
  for (auto plan = OperationPlan::begin(); plan != OperationPlan::end(); ++plan)
    collectTask(&*plan);
  // Head/tail alternatives deliberately remain outside the global operation
  // plan list: FAS cleanup owns that list and can delete optional trees while
  // CP still holds pointers.  Collect their schedulable descendants directly.
  for (const CpXorSplit& alt : g_cpXorSplits) {
    for (const CpXorCut& cut : alt.cuts) {
      for (OperationPlan* root : {cut.head, cut.tail}) {
        if (!root) continue;
        collectTask(root);
        for (OperationPlan::iterator child(root); child != root->end(); ++child)
          collectTask(&*child);
      }
    }
  }
  if (xorSchedHead || xorSchedTail)
    logger << "CP XOR split schedulable: head=" << xorSchedHead
           << " tail=" << xorSchedTail << " locked_head=" << xorLockedHead
           << " locked_tail=" << xorLockedTail << "\n";
  vector<Resource*> machines;
  size_t xorWholeTasks = 0;
  size_t xorHeadTasks = 0;
  size_t xorTailTasks = 0;
  for (const Task& task : movable) {
    const string role = cpSplitRole(task.plan);
    if (role == "whole") ++xorWholeTasks;
    else if (role == "head") ++xorHeadTasks;
    else if (role == "tail") ++xorTailTasks;
  }
  if (xorWholeTasks || xorHeadTasks || xorTailTasks)
    logger << "CP XOR split model tasks: whole=" << xorWholeTasks
           << " head=" << xorHeadTasks << " tail=" << xorTailTasks << "\n";
  unordered_map<Resource*, int> machineIndex;
  auto indexOf = [&](Resource* resource) {
    auto found = machineIndex.find(resource);
    if (found != machineIndex.end()) return found->second;
    int index = static_cast<int>(machines.size());
    machines.push_back(resource);
    machineIndex.emplace(resource, index);
    return index;
  };
  for (Task& task : movable)
    for (Candidate& candidate : task.candidates)
      candidate.machine = candidate.resource ? indexOf(candidate.resource) : -1;
  const Date origin = Plan::instance().getCurrent();
  vector<tuple<int, long, long>> occupancy;
  for (const auto& window : lockedWindows) {
    auto found = machineIndex.find(get<0>(window));
    if (found == machineIndex.end()) continue;
    long start = (get<1>(window) - origin).getSeconds();
    long end = (get<2>(window) - origin).getSeconds();
    if (end > start) occupancy.emplace_back(found->second, max(0L, start), end);
  }
  // Keep historical fixed intervals as-is. Merging overlaps would hide
  // capacity violations already present in the locked plan.
  logger << "CP-SAT input: " << jobs.size() << " jobs, " << movable.size()
         << " operations, " << machines.size() << " machines, "
         << occupancy.size() << " fixed intervals\n"
         << flush;
  if (movable.empty()) {
    localSetStage("no_movable_tasks");
    Py_RETURN_NONE;
  }

  long horizon = 1;
  for (const auto& window : lockedWindows)
    horizon = max(horizon, static_cast<long>((get<2>(window) - origin).getSeconds()));
  for (const Task& task : movable) {
    int longest = 1;
    for (const Candidate& candidate : task.candidates)
      longest = max(longest, candidate.duration);
    horizon += longest;
    if (task.plan && task.plan->getEnd() > origin) {
      horizon = max(horizon, static_cast<long>(
                                 (task.plan->getEnd() - origin).getSeconds()));
    }
  }
  for (const auto& fixed : occupancy) horizon = max(horizon, get<2>(fixed));

  localSetPreflightConflicts("");
  localSetStage("solving");
  scheduleWithCp(movable, occupancy, jobs, horizon, origin);
  // Keep preflight_blocked so callers can read conflict summary.
  if (localLastStage() != "preflight_blocked")
    localSetStage(localLastApplied() ? "solution_applied" : "solver_no_solution");
  // Single-run: drop unused head/tail. Partitioned runs finalize after all
  // groups (see run_cp_partitioned → cpFinalizeXorSplits).
  if (envInt("CP_PARTITION_GROUP", 0) <= 0) abandonUnchosenXorSplits();
  Py_RETURN_NONE;
#endif
}

void cpFinalizeXorSplits() {
#ifdef HAVE_ORTOOLS
  abandonUnchosenXorSplits();
#endif
}

}  // namespace ccAPPS

#include "ccAPPS/solvercp.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdlib>
#include <limits>
#include <map>
#include <tuple>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#ifdef HAVE_ORTOOLS
#include "ortools/sat/cp_model.h"
#include "ortools/sat/cp_model_solver.h"
#endif

namespace ccAPPS {

namespace {

struct Candidate {
  Resource* resource = nullptr;
  Operation* operation = nullptr;
  int duration = 1;
  int machine = -1;
};

struct Task {
  OperationPlan* plan = nullptr;
  int job = -1;
  int sequence = 0;
  vector<Candidate> candidates;
};

bool isLeafResource(const Resource* resource) {
  return resource && resource->getConstrained() && !resource->getFirstChild();
}

void collectLeaves(Resource* resource, vector<Resource*>& out) {
  if (!resource || !resource->getConstrained()) return;
  if (!resource->getFirstChild()) {
    out.push_back(resource);
    return;
  }
  for (Resource* child = resource->getFirstChild(); child;
       child = child->getNextBrother())
    collectLeaves(child, out);
}

int durationSeconds(OperationPlan* plan) {
  Operation* operation = plan->getOperation();
  if (!operation) return 1;
  if (operation->hasType<OperationTimePer>()) {
    auto* timed = static_cast<OperationTimePer*>(operation);
    double seconds = timed->getDuration().getSeconds() +
                     timed->getDurationPer() * plan->getQuantity();
    if (seconds < 1.0) seconds = 1.0;
    return static_cast<int>(seconds + 0.5);
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
  const Load* first = nullptr;
  for (const Load& load : operation->getLoads()) {
    if (load.getResource()) {
      first = &load;
      break;
    }
  }
  if (!first) return found;
  vector<Resource*> leaves;
  collectLeaves(first->getResource(), leaves);
  const int duration = durationSeconds(plan);
  Skill* skill = first->getSkill();
  const Date start = Plan::instance().getCurrent();
  const Date end = start + Duration(86400L);
  for (Resource* resource : leaves) {
    if (skill && !resource->hasSkill(skill, start, end)) continue;
    found.push_back({resource, operation, duration, -1});
  }
  if (!found.empty() || !skill) return found;
  for (Resource* resource : leaves)
    found.push_back({resource, operation, duration, -1});
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

vector<Candidate> taskCandidates(OperationPlan* plan) {
  Operation* operation = plan->getOperation();
  if (operation && operation->getOwner() &&
      operation->getOwner()->hasType<OperationAlternate>()) {
    vector<Candidate> found;
    for (SubOperation* sub : operation->getOwner()->getSubOperations()) {
      if (!sub || !sub->getOperation()) continue;
      auto part = candidatesFor(sub->getOperation(), plan);
      found.insert(found.end(), part.begin(), part.end());
    }
    if (!found.empty()) return found;
  }
  vector<Candidate> expanded = candidatesFor(operation, plan);
  if (!expanded.empty()) return expanded;
  return assignedLeaves(plan);
}

bool schedulable(OperationPlan* plan) {
  Operation* operation = plan->getOperation();
  if (!operation || plan->getQuantity() <= 0) return false;
  if (operation->hasType<OperationItemSupplier, OperationItemDistribution,
                         OperationSplit>() ||
      operation->hasType<OperationRouting, OperationAlternate>())
    return false;
  return !taskCandidates(plan).empty();
}

bool locked(OperationPlan* plan) {
  return plan->getConfirmed() || plan->getApproved() || plan->getCompleted() ||
         plan->getClosed();
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

struct Placement {
  OperationPlan* plan = nullptr;
  Resource* resource = nullptr;
  Operation* operation = nullptr;
  int duration = 1;
  long order = 0;
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
    if (op->getConfirmed() || op->getCompleted() || op->getClosed()) continue;
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
  if (moved)
    logger << "CP: aligned " << moved
           << " delivery commitments to max(receipt, due)\n";
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

void placeOnCalendars(vector<Placement>& rows) {
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
    if (!row.plan || !row.resource || !row.plan->getOperation()) continue;
    const double quantity = row.plan->getQuantity();
    if (row.operation && row.plan->getOperation() != row.operation)
      row.plan->setOperation(row.operation);
    // The CP model already placed calendar, changeover, transfer batch,
    // material and inventory capacity. Keep that start and end.
    Date start = origin + Duration(static_cast<long>(max(0L, row.order)));
    Date end = start + Duration(static_cast<long>(max(1, row.duration)));
    bool personnelOk = true;
    vector<pair<LoadPlan*, Resource*>> extras;
    for (int attempt = 0; attempt < 32; ++attempt) {
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
    for (auto load = row.plan->beginLoadPlans(); load != row.plan->endLoadPlans();
         ++load) {
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
    auto* move = new CommandMoveOperationPlan(row.plan, start, end, -1.0, false,
                                              false, false);
    if (fabs(row.plan->getQuantity() - quantity) > ROUNDING_ERROR)
      row.plan->setQuantity(quantity);
    move->commit();
    delete move;
    occupyResource(busy, row.resource, end);
    for (const auto& extra : extras) occupyResource(busy, extra.second, end);
    ++placed;
  }
  alignDeliveryCommitments();
  int materialShortage = 0;
  for (auto buffer = Buffer::begin(); buffer != Buffer::end(); ++buffer) {
    if (!buffer->getDetectProblems()) continue;
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
  logger << "CP-SAT placed " << placed
         << " operation plans on calendars\n";
  logger << "CP post-check material=" << materialShortage
         << " inventory=" << inventoryExcess
         << " capacity=" << capacityOverload
         << " resource=" << resourceMiss << "\n";
  if (changeoverEnabled)
    logger << "CP changeover matrix is enforced in the CP model\n";
  clearResolvedConstraints(rows);
}

#ifdef HAVE_ORTOOLS

enum class CpGoal { Makespan, Load, Tardiness, Weighted };

const char* cpGoalName(CpGoal goal) {
  switch (goal) {
    case CpGoal::Load: return "load";
    case CpGoal::Tardiness: return "tardiness";
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
};

struct CpPoint {
  bool feasible = false;
  bool provenInfeasible = false;
  long makespan = 0;
  long maxLoad = 0;
  long tardiness = 0;
  long loadCap = -1;
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

CpPoint solveCpPoint(const vector<Task>& movable,
                     const vector<tuple<int, long, long>>& occupancy,
                     const unordered_map<int, vector<size_t>>& byJob,
                     const vector<CpDemand>& demands, long horizon,
                     CpGoal goal, long loadCap,
                     long tardyCap, long makespanCap, int timeLimit,
                     const vector<CpChoice>* hint,
                     const CpAugment* augment = nullptr) {
  using operations_research::sat::BoolVar;
  using operations_research::sat::CpModelBuilder;
  using operations_research::sat::CpSolverStatus;
  using operations_research::sat::IntervalVar;
  using operations_research::sat::IntVar;
  using operations_research::sat::LinearExpr;
  using operations_research::sat::SatParameters;
  using operations_research::sat::SolutionBooleanValue;
  using operations_research::sat::SolutionIntegerValue;
  using operations_research::sat::SolveWithParameters;
  using operations_research::Domain;

  CpPoint point;
  point.loadCap = loadCap;
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
      noteCalendar(resource->getAvailable());
      if (resource->getLocation())
        noteCalendar(resource->getLocation()->getAvailable());
    }
  }
  if (hasCalendar) horizon = max(horizon + 14L * 86400L, openUntil);
  // Locked orders keep their dates. Their future material events still have
  // to reserve or release the buffer, otherwise a movable order can start
  // before a locked upstream receipt or consume stock a locked order already
  // claimed.
  vector<pair<Buffer*, pair<long, long long>>> lockedFlows;
  {
    const Date origin = Plan::instance().getCurrent();
    for (auto plan = OperationPlan::begin(); plan != OperationPlan::end();
         ++plan) {
      if (!schedulable(&*plan) || !locked(&*plan)) continue;
      Operation* operation = plan->getOperation();
      if (!operation ||
          (plan->getStart() <= origin && plan->getEnd() <= origin))
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
        long long quantity = llround(flow.getQuantity() * plan->getQuantity() +
                                     flow.getQuantityFixed());
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
  CpModelBuilder model;
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
  starts.reserve(movable.size());
  ends.reserve(movable.size());
  spans.reserve(movable.size());
  for (size_t index = 0; index < movable.size(); ++index) {
    const Task& task = movable[index];
    const Domain window(0, horizon);
    IntVar start = model.NewIntVar(window);
    IntVar end = model.NewIntVar(window);
    IntVar span = model.NewIntVar(Domain(1, horizon));
    vector<BoolVar> one;
    LinearExpr durationSum(0);
    for (int candidateIndex = 0;
         candidateIndex < static_cast<int>(task.candidates.size());
         ++candidateIndex) {
      const Candidate& candidate = task.candidates[candidateIndex];
      BoolVar presence = model.NewBoolVar();
      machineIntervals[candidate.machine].push_back(model.NewOptionalIntervalVar(
          start, candidate.duration, end, presence));
      machineWork[candidate.machine] +=
          LinearExpr::Term(presence, candidate.duration);
      one.push_back(presence);
      durationSum += LinearExpr::Term(presence, candidate.duration);
      onMachine[{static_cast<int>(index), candidate.machine}] = presence;
      if (candidate.machine >= static_cast<int>(machineOf.size()))
        machineOf.resize(candidate.machine + 1, nullptr);
      machineOf[candidate.machine] = candidate.resource;
      presences.push_back(presence);
      presenceTask.push_back(static_cast<int>(index));
      presenceCandidate.push_back(candidateIndex);
    }
    model.AddExactlyOne(one);
    model.AddEquality(span, durationSum);
    starts.push_back(start);
    ends.push_back(end);
    spans.push_back(span);
  }
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
        model.AddGreaterOrEqual(starts[next], ends[previous]);
      } else {
        const int num = max(1, static_cast<int>(llround(fraction * 1000.0)));
        model.AddGreaterOrEqual(LinearExpr::Term(starts[next], 1000),
                                LinearExpr::Term(starts[previous], 1000) +
                                    LinearExpr::Term(spans[previous], num));
        ++transferLinks;
      }
    }
  }
  vector<IntVar> lates;
  vector<int> lateWeight;
  for (const CpDemand& demand : demands) {
    vector<IntVar> completions;
    for (int job : demand.jobs) {
      auto found = byJob.find(job);
      if (found == byJob.end() || found->second.empty()) continue;
      size_t last = found->second.front();
      for (size_t member : found->second)
        if (movable[member].sequence >= movable[last].sequence) last = member;
      completions.push_back(ends[last]);
    }
    if (completions.empty()) continue;
    IntVar completion = model.NewIntVar(Domain(0, horizon));
    if (completions.size() == 1)
      model.AddEquality(completion, completions.front());
    else
      model.AddMaxEquality(completion, completions);
    IntVar late = model.NewIntVar(Domain(0, horizon));
    model.AddGreaterOrEqual(late + demand.due, completion);
    lates.push_back(late);
    lateWeight.push_back(max(1, demand.weight));
  }
  LinearExpr tardySum(0);
  int maxWeight = 1;
  for (size_t index = 0; index < lates.size(); ++index) {
    tardySum += LinearExpr::Term(lates[index], lateWeight[index]);
    maxWeight = max(maxWeight, lateWeight[index]);
  }
  const long demandCount = max(1L, static_cast<long>(lates.size()));
  IntVar totalTardy = model.NewIntVar(
      Domain(0, horizon * demandCount * maxWeight));
  model.AddEquality(totalTardy, tardySum);
  vector<IntVar> loads;
  for (const auto& work : machineWork) {
    IntVar load = model.NewIntVar(Domain(0, horizon));
    model.AddEquality(load, work.second);
    loads.push_back(load);
  }
  IntVar maxLoad = model.NewIntVar(Domain(0, horizon));
  if (!loads.empty()) model.AddMaxEquality(maxLoad, loads);
  else model.AddEquality(maxLoad, 0);
  IntVar makespan = model.NewIntVar(Domain(0, horizon));
  model.AddMaxEquality(makespan, ends);
  for (const auto& fixed : occupancy) {
    long start = get<1>(fixed);
    long end = get<2>(fixed);
    if (end <= start) continue;
    machineIntervals[get<0>(fixed)].push_back(model.NewIntervalVar(
        model.NewConstant(start), end - start, model.NewConstant(end)));
  }
  map<Calendar*, vector<pair<long, long>>> closedCache;
  auto closedSpans = [&](Calendar* calendar) -> const vector<pair<long, long>>& {
    auto found = closedCache.find(calendar);
    if (found != closedCache.end()) return found->second;
    vector<pair<long, long>> spans;
    if (calendar && horizon > 0) {
      const Date calendarOrigin = Plan::instance().getCurrent();
      const Date limit = calendarOrigin + Duration(horizon);
      // A date with no bucket uses the calendar default. Scenario calendars
      // default to closed and only list the open shifts, so those leading
      // and trailing gaps have to be closed time as well.
      Calendar::EventIterator event(calendar, calendarOrigin, true);
      Date cursor = calendarOrigin;
      double value = event.getValue();
      for (int guard = 0; guard < 4000 && cursor < limit && spans.size() < 400;
           ++guard) {
        ++event;
        Date next = event.getDate();
        if (next <= cursor) break;
        if (next > limit) next = limit;
        if (value <= 0.0) {
          const long from = max(
              0L, static_cast<long>((cursor - calendarOrigin).getSeconds()));
          const long to = min(
              horizon,
              static_cast<long>((next - calendarOrigin).getSeconds()));
          if (to > from) {
            if (!spans.empty() && spans.back().second >= from)
              spans.back().second = max(spans.back().second, to);
            else
              spans.push_back({from, to});
          }
        }
        if (next >= limit) break;
        cursor = next;
        value = event.getValue();
      }
    }
    return closedCache.emplace(calendar, std::move(spans)).first->second;
  };
  int calendarBreaks = 0;
  for (size_t machine = 0; machine < machineOf.size(); ++machine) {
    Resource* resource = machineOf[machine];
    if (!resource) continue;
    Calendar* calendar = resource->getAvailable();
    if (!calendar && resource->getLocation())
      calendar = resource->getLocation()->getAvailable();
    if (!calendar) continue;
    const vector<pair<long, long>>& closed = closedSpans(calendar);
    if (closed.empty()) continue;
    for (const auto& window : closed) {
      if (window.second <= window.first) continue;
      machineIntervals[machine].push_back(model.NewIntervalVar(
          model.NewConstant(window.first), window.second - window.first,
          model.NewConstant(window.second)));
      ++calendarBreaks;
    }
    if (closed.front().first <= 0) {
      for (size_t index = 0; index < movable.size(); ++index) {
        if (movable[index].candidates.size() != 1) continue;
        if (movable[index].candidates.front().machine != static_cast<int>(machine))
          continue;
        model.AddGreaterOrEqual(starts[index], closed.front().second);
      }
    }
  }
  for (size_t index = 0; index < movable.size(); ++index) {
    Operation* operation = movable[index].plan->getOperation();
    Calendar* calendar = operation ? operation->getAvailable() : nullptr;
    if (!calendar && operation && operation->getLocation())
      calendar = operation->getLocation()->getAvailable();
    if (!calendar) continue;
    const vector<pair<long, long>>& closed = closedSpans(calendar);
    if (closed.empty()) continue;
    vector<IntervalVar> windows;
    windows.push_back(model.NewIntervalVar(starts[index], spans[index], ends[index]));
    for (const auto& window : closed) {
      if (window.second <= window.first) continue;
      windows.push_back(model.NewIntervalVar(
          model.NewConstant(window.first), window.second - window.first,
          model.NewConstant(window.second)));
      ++calendarBreaks;
    }
    if (windows.size() > 1) model.AddNoOverlap(windows);
  }
  int changeoverArcs = 0;
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
  for (const auto& lockedFlow : lockedFlows) {
    MaterialEvent event;
    event.task = -1;
    event.fixedTime = lockedFlow.second.first;
    event.quantity = lockedFlow.second.second;
    event.produce = event.quantity > 0;
    materialEvents[lockedFlow.first].push_back(event);
  }
  int materialBuffers = 0;
  int inventoryBuffers = 0;
  for (auto& entry : materialEvents) {
    Buffer* buffer = entry.first;
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
    const bool capped = buffer->getMaximum() > ROUNDING_ERROR;
    if (!producer && consumer) continue;
    if (!producer && !capped) continue;
    long long maximum = positive + 1;
    if (capped) {
      maximum = llround(buffer->getMaximum() * 1000.0);
      if (maximum < positive) maximum = positive;
      if (maximum < 1) maximum = 1;
      ++inventoryBuffers;
    }
    if (producer && consumer) ++materialBuffers;
    auto reservoir = model.AddReservoirConstraint(0, maximum);
    const long long initial = llround(buffer->getOnHand() * 1000.0);
    if (initial > 0) reservoir.AddEvent(LinearExpr(0), initial);
    for (const MaterialEvent& event : entry.second) {
      if (event.task < 0) {
        reservoir.AddEvent(LinearExpr(static_cast<int64_t>(event.fixedTime)),
                           event.quantity);
        continue;
      }
      if (!event.produce) {
        reservoir.AddEvent(starts[event.task], event.quantity);
        continue;
      }
      if (event.fraction >= 1.0 - 1e-6) {
        reservoir.AddEvent(ends[event.task], event.quantity);
        continue;
      }
      const int num = max(1, static_cast<int>(llround(event.fraction * 1000.0)));
      const long long first = event.quantity * num / 1000;
      IntVar release = model.NewIntVar(Domain(0, horizon));
      model.AddEquality(LinearExpr::Term(release, 1000),
                        LinearExpr::Term(starts[event.task], 1000) +
                            LinearExpr::Term(spans[event.task], num));
      reservoir.AddEvent(release, first);
      if (event.quantity > first)
        reservoir.AddEvent(ends[event.task], event.quantity - first);
    }
  }
  logger << "CP model calendar=" << calendarBreaks
         << " changeover=" << changeoverArcs
         << " transfer=" << transferLinks
         << " material=" << materialBuffers
         << " inventory=" << inventoryBuffers
         << " lockedMaterial=" << lockedFlows.size() << "\n";
  for (auto& entry : machineIntervals) model.AddNoOverlap(entry.second);
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
  if (augmecon) {
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
  } else if (goal == CpGoal::Weighted) {
    // 80/10/10 compares three durations in seconds. totalTardy is the sum of
    // demand lateness, so scale makespan and load by the demand count and
    // the tardiness coefficient then applies to the average.
    model.Minimize(
        LinearExpr::Term(makespan, static_cast<long long>(weightMakespan) *
                                       demandCount) +
        LinearExpr::Term(maxLoad,
                         static_cast<long long>(weightLoad) * demandCount) +
        LinearExpr::Term(totalTardy, weightTardiness));
  } else {
    model.Minimize(makespan);
  }
  vector<CpChoice> seeded;
  const vector<CpChoice>* activeHint = hint;
  if (!activeHint) {
    const Date origin = Plan::instance().getCurrent();
    for (size_t index = 0; index < movable.size(); ++index) {
      OperationPlan* plan = movable[index].plan;
      if (!plan || movable[index].candidates.empty()) continue;
      Resource* assigned = nullptr;
      for (auto load = plan->beginLoadPlans(); load != plan->endLoadPlans();
           ++load) {
        if (!load->isStart() || load->getQuantity() <= 0) continue;
        assigned = load->getResource();
        break;
      }
      int candidate = 0;
      if (assigned) {
        for (int indexCandidate = 0;
             indexCandidate < static_cast<int>(movable[index].candidates.size());
             ++indexCandidate) {
          Resource* resource = movable[index].candidates[indexCandidate].resource;
          if (!resource) continue;
          if (resource == assigned || assigned->isMemberOf(resource) ||
              resource->isMemberOf(assigned) ||
              (resource->getOwner() &&
               resource->getOwner() == assigned->getOwner())) {
            candidate = indexCandidate;
            break;
          }
        }
      }
      long start = 0;
      if (plan->getStart() <= origin ||
          plan->getStart() == Date::infinitePast)
        continue;
      start = static_cast<long>((plan->getStart() - origin).getSeconds());
      if (start < 0 || start > horizon) continue;
      seeded.push_back(
          {static_cast<int>(index), candidate, start});
    }
    if (!seeded.empty()) {
      activeHint = &seeded;
      logger << "CP seeded " << seeded.size()
             << " starts from the current plan\n";
    }
  }
  if (activeHint) {
    for (const CpChoice& choice : *activeHint) {
      if (choice.task < 0 || choice.task >= static_cast<int>(starts.size()))
        continue;
      if (choice.start < 0 || choice.start > horizon) continue;
      model.AddHint(starts[choice.task], choice.start);
    }
    for (size_t index = 0; index < presences.size(); ++index) {
      bool selected = false;
      for (const CpChoice& choice : *activeHint)
        if (choice.task == presenceTask[index] &&
            choice.candidate == presenceCandidate[index])
          selected = true;
      model.AddHint(presences[index], selected);
    }
  }
  SatParameters parameters;
  parameters.set_max_time_in_seconds(max(1, timeLimit));
  parameters.set_num_workers(envInt("CP_WORKERS", 8));
  if (activeHint && !activeHint->empty()) parameters.set_repair_hint(true);
  const auto response = SolveWithParameters(model.Build(), parameters);
  point.feasible = response.status() == CpSolverStatus::OPTIMAL ||
                   response.status() == CpSolverStatus::FEASIBLE;
  point.provenInfeasible = response.status() == CpSolverStatus::INFEASIBLE;
  logger << "CP-SAT status=" << static_cast<int>(response.status())
         << " wall=" << response.wall_time() << "s"
         << " goal=" << (augmecon ? "augmecon" : cpGoalName(goal));
  if (loadCap >= 0) logger << " loadCap=" << loadCap;
  if (tardyCap >= 0) logger << " tardyCap=" << tardyCap;
  if (makespanCap >= 0) logger << " makespanCap=" << makespanCap;
  logger << "\n";
  if (!point.feasible) return point;
  point.makespan = SolutionIntegerValue(response, makespan);
  point.maxLoad = SolutionIntegerValue(response, maxLoad);
  point.tardiness = SolutionIntegerValue(response, totalTardy);
  for (size_t index = 0; index < presences.size(); ++index) {
    if (!SolutionBooleanValue(response, presences[index])) continue;
    point.choices.push_back(
        {presenceTask[index], presenceCandidate[index],
         SolutionIntegerValue(response, starts[presenceTask[index]])});
  }
  logger << "CP-SAT point makespan=" << point.makespan
         << " maxLoad=" << point.maxLoad
         << " tardiness=" << point.tardiness << "\n";
  return point;
}

void exportCpPoint(const vector<Task>& movable, const CpPoint& point) {
  vector<Placement> rows;
  for (const CpChoice& choice : point.choices) {
    const Candidate& candidate =
        movable[choice.task].candidates[choice.candidate];
    rows.push_back({movable[choice.task].plan, candidate.resource,
                    candidate.operation, candidate.duration, choice.start});
  }
  placeOnCalendars(rows);
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
    if (!demand) continue;
    const Date due = demand->getDue();
    if (due == Date::infinitePast || due == Date::infiniteFuture) continue;
    long seconds = static_cast<long>((due - origin).getSeconds());
    if (seconds < 0) seconds = 0;
    int weight = 1;
    if (demand->getPriority() > 0.0)
      weight = max(1, static_cast<int>(demand->getPriority() + 0.5));
    auto [group, inserted] = groupOf.emplace(demand, demands.size());
    if (inserted) demands.push_back(CpDemand{seconds, weight, {}});
    demands[group->second].jobs.push_back(static_cast<int>(job));
    ++datedLots;
  }
  logger << "CP demand dues: " << demands.size() << " demands from "
         << datedLots << "/" << jobs.size() << " lots\n";
  const int timeLimit = envInt("CP_TIME_LIMIT_SECONDS", 60);
  const string method = cpObjectiveMethod();
  logger << "CP objective method: " << method << "\n";
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
    CpPoint span = solveCpPoint(movable, occupancy, byJob, demands,
                                horizon, CpGoal::Makespan, -1, -1, -1,
                                timeLimit, nullptr);
    if (!span.feasible) {
      logger << "CP-SAT schedule was not applied\n";
      return;
    }
    CpPoint tardy = solveCpPoint(
        movable, occupancy, byJob, demands, horizon,
        CpGoal::Tardiness, -1, -1, span.makespan, timeLimit, &span.choices);
    if (!tardy.feasible) tardy = span;
    CpPoint load = solveCpPoint(
        movable, occupancy, byJob, demands, horizon, CpGoal::Load,
        -1, tardy.tardiness, span.makespan, timeLimit, &tardy.choices);
    const CpPoint& chosen = load.feasible ? load : tardy;
    logger << "CP lexicographic export makespan=" << chosen.makespan
           << " maxLoad=" << chosen.maxLoad
           << " tardiness=" << chosen.tardiness << "\n";
    exportCpPoint(movable, chosen);
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

PyObject* run_cp(PyObject*, PyObject*) {
#ifndef HAVE_ORTOOLS
  logger << "CP-SAT skipped: this engine was built without OR-Tools\n";
  Py_RETURN_NONE;
#else
  vector<Task> movable;
  vector<tuple<Resource*, Date, Date>> lockedWindows;
  unordered_map<OperationPlan*, int> jobIndex;
  vector<OperationPlan*> jobs;
  for (auto plan = OperationPlan::begin(); plan != OperationPlan::end(); ++plan) {
    if (!schedulable(&*plan)) continue;
    if (locked(&*plan)) {
      for (auto load = plan->beginLoadPlans(); load != plan->endLoadPlans(); ++load) {
        if (load->isStart() && isLeafResource(load->getResource()))
          lockedWindows.emplace_back(load->getResource(), plan->getStart(),
                                     plan->getEnd());
      }
      continue;
    }
    Task task;
    task.plan = &*plan;
    OperationPlan* job = jobPlan(&*plan);
    auto inserted = jobIndex.emplace(job, static_cast<int>(jobs.size()));
    if (inserted.second) jobs.push_back(job);
    task.job = inserted.first->second;
    task.sequence = sequencePriority(&*plan, job);
    task.candidates = taskCandidates(&*plan);
    if (!task.candidates.empty()) movable.push_back(std::move(task));
  }
  vector<Resource*> machines;
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
      candidate.machine = indexOf(candidate.resource);
  const Date origin = Plan::instance().getCurrent();
  vector<tuple<int, long, long>> occupancy;
  for (const auto& window : lockedWindows) {
    auto found = machineIndex.find(get<0>(window));
    if (found == machineIndex.end()) continue;
    long start = (get<1>(window) - origin).getSeconds();
    long end = (get<2>(window) - origin).getSeconds();
    if (end > start) occupancy.emplace_back(found->second, max(0L, start), end);
  }
  logger << "CP-SAT input: " << jobs.size() << " jobs, " << movable.size()
         << " operations, " << machines.size() << " machines, "
         << occupancy.size() << " fixed intervals\n";
  if (movable.empty()) {
    Py_RETURN_NONE;
  }

  long horizon = 1;
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

  scheduleWithCp(movable, occupancy, jobs, horizon, origin);
  Py_RETURN_NONE;
#endif
}

}  // namespace ccAPPS

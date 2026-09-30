/***************************************************************************
 * Deterministic forward finite-capacity supply scheduler.
 ***************************************************************************/

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cstdlib>
#include <deque>
#include <limits>
#include <queue>
#include <random>
#include <unordered_map>
#include <unordered_set>

#include "ccAPPS/solverfas.h"

namespace ccAPPS {

const MetaClass* SolverFAS::metadata;

namespace {

// Job sequencing: demand priority → due, then tie-break.
// Default "lpt" = longest total processing time first (stable, strong makespan).
// "name" = routing operation name; "reference" = caixin MO.reference string.
// Priority/due always come first: business urgency before processing length.
enum class FASJobTieBreak { Lpt, Spt, Name, Reference };

FASJobTieBreak fasJobTieBreakFromEnv() {
  const char* value = getenv("FAS_JOB_ORDER");
  if (!value || !*value) return FASJobTieBreak::Lpt;
  string mode(value);
  for (char& ch : mode) ch = static_cast<char>(tolower(ch));
  if (mode == "reference" || mode == "ref" || mode == "id" || mode == "priority")
    return FASJobTieBreak::Reference;
  if (mode == "name") return FASJobTieBreak::Name;
  if (mode == "spt") return FASJobTieBreak::Spt;
  return FASJobTieBreak::Lpt;
}

string fasJobSortName(const OperationPlan* job) {
  if (!job) return string();
  if (job->getOperation()) return job->getOperation()->getName();
  return string();
}

Duration fasJobTotalDuration(const OperationPlan* job,
                             const unordered_map<OperationPlan*, Duration>&
                                 durationByJob) {
  if (!job) return Duration(0L);
  const auto found = durationByJob.find(const_cast<OperationPlan*>(job));
  return found == durationByJob.end() ? Duration(0L) : found->second;
}

bool fasJobTieLess(const OperationPlan* left, const OperationPlan* right,
                   FASJobTieBreak tieBreak,
                   const unordered_map<OperationPlan*, Duration>& durationByJob) {
  if (!left) return false;
  if (!right) return true;
  if (tieBreak == FASJobTieBreak::Reference)
    return left->getReference() < right->getReference();
  if (tieBreak == FASJobTieBreak::Lpt || tieBreak == FASJobTieBreak::Spt) {
    const long leftSec =
        fasJobTotalDuration(left, durationByJob).getSeconds();
    const long rightSec =
        fasJobTotalDuration(right, durationByJob).getSeconds();
    if (leftSec != rightSec) {
      if (tieBreak == FASJobTieBreak::Lpt) return leftSec > rightSec;
      return leftSec < rightSec;
    }
  }
  const string leftName = fasJobSortName(left);
  const string rightName = fasJobSortName(right);
  if (leftName != rightName) return leftName < rightName;
  return left->getReference() < right->getReference();
}

// Resource assignment is part of the in-memory planning transaction just as
// dates are.  LoadPlan::setResource updates timelines and setup events
// immediately, hence a forward scheduler must have a matching undo action.
class CommandFASSetResource final : public Command {
 public:
  CommandFASSetResource(LoadPlan* loadplan, Resource* resource)
      : loadplan(loadplan), original(resource ? loadplan->getResource()
                                              : nullptr),
        state(loadplan ? loadplan->getOperationPlan() : nullptr) {
    if (loadplan && resource && original != resource)
      loadplan->setResource(resource, false, false);
    else
      loadplan = nullptr;
  }

  void commit() override { loadplan = nullptr; }

  void rollback() override {
    if (!loadplan) return;
    OperationPlan* operationplan = loadplan->getOperationPlan();
    operationplan->clearSetupEvent();
    loadplan->setResource(original, false, false);
    operationplan->restore(state);
    loadplan = nullptr;
  }

  ~CommandFASSetResource() override { rollback(); }
  short getType() const override { return 8; }

 private:
  LoadPlan* loadplan = nullptr;
  Resource* original = nullptr;
  OperationPlanState state;
};

// Collect the executable leaves of a candidate alternate in execution order. A
// selected alternate can wrap its work in a routing or in a nested alternate
// level, so the plan that actually loads a resource isn't necessarily a direct
// child of the candidate.
void collectExecutableLeaves(OperationPlan* plan, vector<OperationPlan*>& leaves) {
  OperationPlan::iterator child(plan);
  if (child == plan->end()) {
    leaves.push_back(plan);
    return;
  }
  for (; child != plan->end(); ++child) collectExecutableLeaves(&*child, leaves);
}

// Alternate ranking weights, used by SolverFAS::probeAlternateCompletion.
//
// Aligned with caixin.zip `fjsp_forward_schedule`: pick the candidate whose
// earliest feasible slot finishes first (score ≈ slot_end, then start).
// queueWeight=1 means the ranking key is essentially that probed completion
// (plus structural contention as a weak tie-break). Weight > 1 inflates the
// candidate's own processing time and prefers "fast" machines even when they
// finish later — that diverged from the reference decoder (~9.37h vs ~8.58h
// on scenario2). SolverFAS still keeps calendar probing, material FIFO,
// contention and refinement rounds; only this alternate score matches the
// reference earliest-finish rule.
constexpr long kFASQueueWeight = 1L;
// contentionUnit: seconds credited per competing load definition. Only the
// presence of the term matters on the reference scenario - the ranking is
// identical for any value between 5 minutes and 2 hours, because it is
// compared between machines with 7 and 84 competing definitions. Do not
// replace it with a sum of definition durations: that rewards the machine
// which is fastest for the most operations, and drove the pool's peak load
// from 9.9h to 11.1h.
constexpr long kFASContentionUnit = 1800L;

}  // namespace

PyObject* run_fas(PyObject*, PyObject*) {
  SolverFAS fas;
  fas.setLogLevel(1);
  fas.solve();
  fas.commitPlan();
  Py_RETURN_NONE;
}

int SolverFAS::initialize() {
  metadata = MetaClass::registerClass<SolverFAS>(
      "solver", "solver_fas", Object::create<SolverFAS>);
  registerFields<SolverFAS>(const_cast<MetaClass*>(metadata));
  auto* fields = const_cast<MetaClass*>(metadata);
  fields->addShortField<SolverFAS>(Tags::constraints,
                                   &SolverFAS::getConstraints,
                                   &SolverFAS::setConstraints);
  fields->addShortField<SolverFAS>(Tags::plantype,
                                   &SolverFAS::getPlanType,
                                   &SolverFAS::setPlanType);
  fields->addDurationField<SolverFAS>(Keyword::find("lazydelay"),
                                      &SolverFAS::getLazyDelay,
                                      &SolverFAS::setLazyDelay);
  fields->addDurationField<SolverFAS>(Keyword::find("minimumdelay"),
                                      &SolverFAS::getMinimumDelay,
                                      &SolverFAS::setMinimumDelay);
  fields->addDurationField<SolverFAS>(Keyword::find("administrativeleadtime"),
                                      &SolverFAS::getAdministrativeLeadTime,
                                      &SolverFAS::setAdministrativeLeadTime);
  fields->addBoolField<SolverFAS>(Keyword::find("rotateresources"),
                                  &SolverFAS::getRotateResources,
                                  &SolverFAS::setRotateResources);
  fields->addUnsignedLongField<SolverFAS>(Keyword::find("iterationmax"),
                                          &SolverFAS::getIterationMax,
                                          &SolverFAS::setIterationMax);
  fields->addUnsignedLongField<SolverFAS>(Keyword::find("resourceiterationmax"),
                                          &SolverFAS::getResourceIterationMax,
                                          &SolverFAS::setResourceIterationMax);
  fields->addDurationField<SolverFAS>(Tags::autofence,
                                      &SolverFAS::getAutoFence,
                                      &SolverFAS::setAutoFence);
  auto& x = PythonExtension<SolverFAS>::getPythonType();
  x.setName("solverFAS");
  x.setDoc("Deterministic forward finite-capacity supply scheduler");
  x.supportgetattro(); x.supportsetattro(); x.supportcreate(SolverFAS::create);
  x.addMethod("solve", Solver::solve, METH_NOARGS, "run FAS");
  x.addMethod("commit", SolverFAS::commit, METH_NOARGS, "commit FAS changes");
  x.addMethod("rollback", SolverFAS::rollback, METH_NOARGS,
              "rollback FAS changes");
  metadata->setPythonClass(x);
  return x.typeReady();
}

PyObject* SolverFAS::create(PyTypeObject*, PyObject*, PyObject* kwds) {
  try {
    auto* solver = new SolverFAS();
    if (kwds) {
      PyObject *key, *value;
      Py_ssize_t pos = 0;
      while (PyDict_Next(kwds, &pos, &key, &value)) {
        PythonData field(value);
        PyObject* key_utf8 = PyUnicode_AsUTF8String(key);
        DataKeyword attr(PyBytes_AsString(key_utf8));
        Py_DECREF(key_utf8);
        const MetaFieldBase* fmeta = metadata->findField(attr.getHash());
        if (!fmeta) fmeta = Solver::metadata->findField(attr.getHash());
        if (fmeta)
          fmeta->setField(solver, field);
        else
          solver->setProperty(attr.getName(), value);
      }
    }
    return static_cast<PyObject*>(solver);
  } catch (...) {
    PythonType::evalException();
    return nullptr;
  }
}

bool SolverFAS::commitPlan() {
  // Supply creation uses the standard MRP command stream; FAS time moves use
  // its own stream. Both are transactional and neither invokes MRP's global
  // demand selection loop.
  supplyExpander.getCommandManager()->commit();
  commands.commit();
  plansAtStart.clear();
  return true;
}

void SolverFAS::rollbackPlan() {
  commands.rollback();
  supplyExpander.getCommandManager()->rollback();
  // The MRP queue intentionally commits some internal bookkeeping while it
  // creates supply.  Those plan objects aren't necessarily represented in
  // its remaining command list, so remove only top-level plans absent from
  // the pre-FAS snapshot. Deleting an owner recursively removes children.
  vector<OperationPlan*> created;
  unordered_set<OperationPlan*> seen;
  for (auto plan = OperationPlan::begin(); plan != OperationPlan::end(); ++plan) {
    OperationPlan* top = plan->getTopOwner();
    if (top && !plansAtStart.count(top) && seen.insert(top).second)
      created.push_back(top);
  }
  for (auto* plan : created)
    if (plan->getProposed()) delete plan;
  plansAtStart.clear();
}

PyObject* SolverFAS::commit(PyObject* self, PyObject*) {
  try {
    return static_cast<SolverFAS*>(self)->commitPlan() ? Py_BuildValue("")
                                                       : nullptr;
  } catch (...) {
    PythonType::evalException();
    return nullptr;
  }
}

PyObject* SolverFAS::rollback(PyObject* self, PyObject*) {
  try {
    static_cast<SolverFAS*>(self)->rollbackPlan();
    return Py_BuildValue("");
  } catch (...) {
    PythonType::evalException();
    return nullptr;
  }
}

void SolverFAS::expandDemand(Demand* demand) {
  if (!demand) return;
  // Match the normal constrained supply rules. The FAS scheduler owns the
  // order sequence; the expander owns only BOM/lot-size/source semantics.
  supplyExpander.setConstraints(getConstraints());
  supplyExpander.setPlanType(getPlanType());
  // Supply expansion mutates the process-global planning model. Keep this
  // internal phase single-worker even when FAS itself runs with loglevel 0;
  // parallel cluster workers race on shared routing/alternate state.
  supplyExpander.setLogLevel(std::max<short>(1, getLogLevel()));
  supplyExpander.setLazyDelay(getLazyDelay());
  supplyExpander.setMinimumDelay(getMinimumDelay());
  supplyExpander.setAdministrativeLeadTime(getAdministrativeLeadTime());
  supplyExpander.setRotateResources(getRotateResources());
  supplyExpander.setIterationMax(getIterationMax());
  supplyExpander.setResourceIterationMax(getResourceIterationMax());
  supplyExpander.setAutoFence(getAutoFence());
  supplyExpander.setErasePreviousFirst(false);
  supplyExpander.planDemand(demand);
}

bool SolverFAS::isMovable(const OperationPlan* op) const {
  if (!op || !op->getOperation() || op->getQuantity() <= 0.0) return false;
  // Composite operations own the instantiation and synchronization of their
  // child plans. Never move the container itself: an operation_alternate can
  // temporarily have no instantiated child while it is still a composite,
  // so testing only the current child list is insufficient.
  if (op->getOperation()->hasSubOperations()) return false;
  if (op->getConfirmed() || op->getCompleted() || op->getClosed()) return false;
  if (!moveApproved && op->getApproved()) return false;
  // Purchase and distribution receipts are supply operations. Their source,
  // lot-size and lead-time rules are selected by the shared MRP kernel, but
  // their executable dates are owned by FAS just like manufacturing orders.
  // Delivery plans don't create supply, but their timestamp is the standard
  // demand commitment written by the normal export pipeline.  They therefore
  // join the last scheduling step so a commitment can never precede its
  // pegged final receipt.
  return true;
}

unordered_set<OperationPlan*> SolverFAS::collectNetwork(
    const vector<OperationPlan*>& roots) const {
  // Demand pegging and alternate/routing ownership can point to a composite
  // parent. Close every supplied root over its execution tree first, then
  // close it over concrete prerequisite dependencies. This is intentionally
  // identical for the committed all-order schedule and for a temporary
  // alternate-path trial.
  unordered_set<OperationPlan*> network;
  vector<OperationPlan*> pending;
  const auto includeTree = [&network, &pending](OperationPlan* root) {
    if (!root || !network.insert(root).second) return;
    pending.push_back(root);
    while (!pending.empty()) {
      OperationPlan* parent = pending.back();
      pending.pop_back();
      for (OperationPlan::iterator child(parent); child != parent->end();
           ++child) {
        OperationPlan* sub = &*child;
        if (network.insert(sub).second) pending.push_back(sub);
      }
    }
  };
  for (auto* root : roots) includeTree(root);

  bool expanded = true;
  while (expanded) {
    expanded = false;
    const vector<OperationPlan*> snapshot(network.begin(), network.end());
    for (auto* consumer : snapshot) {
      auto dependency = consumer->getBlockedbyIterator();
      while (auto* link = dependency.next()) {
        OperationPlan* producer = link->getFirst();
        if (!producer || network.count(producer)) continue;
        includeTree(producer);
        expanded = true;
      }
    }
  }
  return network;
}

SolverFAS::ExecutionGraph SolverFAS::buildExecutionGraph(
    const unordered_set<OperationPlan*>& network,
    const unordered_set<OperationPlan*>& fixedPlans) const {
  ExecutionGraph graph;
  vector<OperationPlan*>& work = graph.work;
  for (auto* op : network) {
    if (!isMovable(op) || fixedPlans.count(op)) continue;
    if (op->getSubOperationPlans() != OperationPlan::end()) continue;
    work.push_back(op);
  }
  unordered_set<OperationPlan*> inScope(work.begin(), work.end());
  unordered_map<OperationPlan*, vector<OperationPlan*>>& downstream =
      graph.downstream;
  unordered_map<OperationPlan*, size_t>& indegree = graph.indegree;
  for (auto* op : work) indegree[op] = 0;

  const auto addEdge = [&downstream, &indegree](OperationPlan* producer,
                                                 OperationPlan* consumer) {
    auto& successors = downstream[producer];
    if (find(successors.begin(), successors.end(), consumer) !=
        successors.end())
      return;
    successors.push_back(consumer);
    ++indegree[consumer];
  };
  // FG (and similar) receipts are often posted on a routing/alternate parent,
  // which is not itself an executable FAS leaf.  Sequence the consumer after
  // the parent's last in-scope leaf so delivery cannot precede batch receipt.
  const auto lastExecutableLeaf =
      [&inScope](OperationPlan* root) -> OperationPlan* {
    if (!root) return nullptr;
    if (inScope.count(root)) return root;
    // Descend to the first leaf, then walk next-suboperation to the end of
    // the routing/alternate chain — the same leaf FAS will finish last.
    OperationPlan* probe = root;
    while (probe->getSubOperationPlans(false) != OperationPlan::end())
      probe = &*probe->getSubOperationPlans(false);
    OperationPlan* last = inScope.count(probe) ? probe : nullptr;
    while (probe) {
      OperationPlan* owner = probe;
      OperationPlan* next = nullptr;
      while (owner && !(next = owner->getNextSubOpplan()))
        owner = owner->getOwner();
      if (!next) break;
      probe = next;
      while (probe->getSubOperationPlans(false) != OperationPlan::end())
        probe = &*probe->getSubOperationPlans(false);
      if (inScope.count(probe)) last = probe;
    }
    return last;
  };
  const auto addProducerEdge =
      [&](OperationPlan* producer, OperationPlan* consumer,
          unordered_set<OperationPlan*>& seen) {
        OperationPlan* leaf = lastExecutableLeaf(producer);
        if (!leaf || leaf == consumer || !inScope.count(leaf)) return;
        if (!seen.insert(leaf).second) return;
        addEdge(leaf, consumer);
      };
  // Return the executable leaf immediately preceding a leaf in a nested
  // routing/alternate tree.  Selected alternates add an extra structural
  // level, while unwrapped single-choice steps can be direct routing
  // children, so the predecessor isn't necessarily a direct sibling.
  const auto routingPredecessor = [](OperationPlan* leaf) {
    OperationPlan* node = leaf;
    while (node && node->getOwner()) {
      if (OperationPlan* previous = node->getPrevSubOpplan()) {
        node = previous;
        while (node->getSubOperationPlans(false) != OperationPlan::end())
          node = &*node->getSubOperationPlans(false);
        return node;
      }
      node = node->getOwner();
    }
    return static_cast<OperationPlan*>(nullptr);
  };
  for (auto* consumer : work) {
    unordered_set<OperationPlan*> seen;
    // Build the execution graph from every *consuming flowplan*.  An
    // operationplan-level pegging iterator is intentionally aggregated at
    // routing/owner level and can therefore lose the leaf-to-leaf batch link.
    // Losing that link incorrectly turns a downstream line operation into a
    // source candidate, which lets it reserve shared labour before its input
    // batch is actually released.
    for (auto consume = consumer->beginFlowPlans();
         consume != consumer->endFlowPlans(); ++consume) {
      if (consume->getQuantity() >= -ROUNDING_ERROR) continue;
      for (PeggingIterator peg(&*consume, false); peg; --peg)
        addProducerEdge(peg.getOperationPlan(), consumer, seen);
    }
    for (PeggingIterator peg(consumer, false, 1); peg; --peg)
      addProducerEdge(peg.getOperationPlan(), consumer, seen);
    // MRP's pegging iterator can omit the leaf link for a generated
    // distribution order. Complete that relation directly from the material
    // buffers, so a WIP DO is sequenced after its producing MO rather than
    // being treated as an external source supply.  Also close delivery plans
    // onto the last leaf of the FG-producing routing: the receipt flow often
    // sits on the non-executable parent, which would otherwise leave DLVR
    // with indegree 0 and let FAS promise before the batch is finished.
    for (auto consume = consumer->beginFlowPlans();
         consume != consumer->endFlowPlans(); ++consume) {
      if (consume->getQuantity() >= -ROUNDING_ERROR || !consume->getBuffer())
        continue;
      Buffer* buffer = consume->getBuffer();
      for (auto* producer : network) {
        if (producer == consumer) continue;
        for (auto produce = producer->beginFlowPlans();
             produce != producer->endFlowPlans(); ++produce)
          if (produce->getQuantity() > ROUNDING_ERROR &&
              produce->getBuffer() == buffer) {
            addProducerEdge(producer, consumer, seen);
            break;
          }
      }
    }
    auto dependency = consumer->getBlockedbyIterator();
    while (auto* link = dependency.next())
      addProducerEdge(link->getFirst(), consumer, seen);
  }
  for (auto* producer : work) {
    OperationPlan* consumer = producer->getNextSubOpplan();
    if (consumer && inScope.count(consumer)) addEdge(producer, consumer);
  }
  for (auto* consumer : work) {
    OperationPlan* producer = routingPredecessor(consumer);
    if (producer && inScope.count(producer)) addEdge(producer, consumer);
  }

  // The forward pass consumes these edges directly: a plan is dispatched the
  // moment its last producer is placed, so the schedule follows the actual
  // readiness of the work instead of a sequence fixed before any date was
  // known.
  return graph;
}

void SolverFAS::reserveFixedNetwork(
    const unordered_set<OperationPlan*>& inScope,
    unordered_map<const Resource*, vector<Reservation>>& reservations) const {
  // Anything outside the trial/committed FAS network is immutable capacity.
  // This includes prior FAS reservations when a single alternate path is
  // trialled after earlier orders, as well as approved and confirmed plans.
  for (auto op = OperationPlan::begin(); op != OperationPlan::end(); ++op) {
    if (!op->getOperation() ||
        (inScope.count(&*op) && isMovable(&*op)))
      continue;
    for (auto load = op->beginLoadPlans(); load != op->endLoadPlans(); ++load) {
      const Resource* resource = load->getResource();
      if (!isCapacityConstrained() || !resource || !resource->getConstrained() ||
          resource->isGroup() || op->getEnd() == Date::infiniteFuture)
        continue;
      // A plan can contain more than one loadplan pointing to the same
      // resource (eg. an inherited/expanded routing load).  The model
      // timeline has already consolidated that into the actual load demand;
      // reserve it only once in the FAS shadow timeline as well.
      const auto alreadyReserved = find_if(
          reservations[resource].begin(), reservations[resource].end(),
          [&op](const Reservation& entry) { return entry.operationplan == &*op; });
      if (alreadyReserved == reservations[resource].end())
        reservations[resource].push_back(
            {op->getStart(), op->getEnd(), fabs(load->getQuantity()), &*op});
    }
  }
}

Date SolverFAS::finishOf(const vector<OperationPlan*>& roots) const {
  Date result = Date::infinitePast;
  for (auto* plan : roots) {
    if (!plan) continue;
    result = max(result, plan->getEnd());
    for (OperationPlan::iterator child(plan); child != plan->end(); ++child)
      result = max(result, child->getEnd());
  }
  return result;
}

void SolverFAS::solve(void*) {
  // A normal FAS run is a complete replan, matching SolverCreate/MRP
  // semantics.  Trial runs (used while evaluating an alternate) must retain
  // the surrounding model and therefore skip this cleanup.
  // When open demands are already fully planned (caixin-style MRP then
  // forward), keep that supply network and only re-date / re-pick resources.
  if (trialRoots.empty()) {
    bool hasFullyPlannedOpenDemand = false;
    for (auto demand = Demand::begin(); demand != Demand::end(); ++demand) {
      if (demand->getStatus() == Demand::STATUS_OPEN &&
          demand->getDue() < Date::infiniteFuture &&
          demand->getQuantity() - demand->getPlannedQuantity() <=
              ROUNDING_ERROR) {
        hasFullyPlannedOpenDemand = true;
        break;
      }
    }
    if (!hasFullyPlannedOpenDemand) {
      for (auto& operation : Operation::all()) operation.deleteOperationPlans();
    } else if (getLogLevel() >= 0) {
      logger << "FAS: existing planned supply retained; forward schedule only\n";
    }
  }
  activeCommands = trialTransaction ? trialTransaction : &commands;
  alternateProbes = 0;
  contentionCache.clear();
  forwardCompletion = Date::infinitePast;
  const bool trial = !trialRoots.empty();
  // FAS owns the order pool. It selects firm orders first, then invokes the
  // shared single-demand supply expansion kernel for each selected order.
  // That kernel preserves MRP's BOM, lot-size, alternate, purchase and
  // distribution semantics without entering MRP's global demand loop.
  // Keep the selected order set separate from the set that still needs new
  // supply.  A second FAS run must continue to own and reschedule the supply
  // network of already-planned open orders, without creating it again.
  plansAtStart.clear();
  for (auto plan = OperationPlan::begin(); plan != OperationPlan::end(); ++plan)
    plansAtStart.insert(&*plan);
  vector<Demand*> demands;
  vector<Demand*> demandsToExpand;
  for (auto demand = Demand::begin(); !trial && demand != Demand::end(); ++demand) {
    if (demand->getStatus() == Demand::STATUS_OPEN &&
        demand->getDue() < Date::infiniteFuture) {
      demands.push_back(&*demand);
      if (demand->getQuantity() - demand->getPlannedQuantity() > ROUNDING_ERROR)
        demandsToExpand.push_back(&*demand);
    }
  }
  const auto demandOrder = [](const Demand* left, const Demand* right) {
    if (left->getPriority() != right->getPriority())
      return left->getPriority() < right->getPriority();
    if (left->getDue() != right->getDue())
      return left->getDue() < right->getDue();
    // Keep the supply-creation tie break identical to SolverCreate's MRP
    // demand_comparison. This lets the same batch-window requests coalesce;
    // the FAS-specific forward ordering happens only after expansion.
    if (left->getQuantity() != right->getQuantity())
      return left->getQuantity() < right->getQuantity();
    return left->getName() < right->getName();
  };
  stable_sort(demands.begin(), demands.end(), demandOrder);
  // The preceding implementation sorted the selected order list but then
  // expanded a separately collected, unsorted list.  Supply rules with lot
  // sizes and batch windows are order-sensitive, so that made FAS create a
  // different (and often needlessly fragmented) supply plan before its
  // forward scheduler even started.
  stable_sort(demandsToExpand.begin(), demandsToExpand.end(), demandOrder);
  // Let the shared MRP queue kernel expand exactly FAS's selected queue. It
  // preserves MRP's batching and purchasing rules without taking over order
  // selection or scheduling dates.
  supplyExpander.setConstraints(getConstraints());
  supplyExpander.setPlanType(getPlanType());
  supplyExpander.setLogLevel(std::max<short>(1, getLogLevel()));
  // `planSelectedDemands` executes SolverCreate's normal queue kernel.
  // Keep every scheduling control in sync with solver_mrp before entering
  // that kernel; otherwise a FAS run can silently use SolverCreate defaults
  // for retries, resource rotation or administrative lead time.
  supplyExpander.setLazyDelay(getLazyDelay());
  supplyExpander.setMinimumDelay(getMinimumDelay());
  supplyExpander.setAdministrativeLeadTime(getAdministrativeLeadTime());
  supplyExpander.setRotateResources(getRotateResources());
  supplyExpander.setIterationMax(getIterationMax());
  supplyExpander.setResourceIterationMax(getResourceIterationMax());
  supplyExpander.setAutoFence(getAutoFence());
  supplyExpander.setErasePreviousFirst(false);
  // Alternate evaluation runs inside the caller's bookmark transaction.
  // Keep all operationplans created by that trial reversible; only the
  // top-level FAS expansion may autocommit at demand boundaries.
  supplyExpander.setAutocommit(!trial);
  // Alternate candidates are ranked by the forward completion their resources
  // can actually offer. Without this hook the selection falls back to cost and
  // priority, which are equal for a pool of identical machines: every order
  // then picks the first member and the remaining machines never get load.
  // `runForwardTrial` deliberately probes instead of scheduling the whole
  // network, so this stays affordable at one call per candidate.
  supplyExpander.setForwardTrial(&SolverFAS::runForwardTrial, this);
  // MRP performs netting and excess cleanup per supply-network cluster.
  // Keep that boundary when expanding FAS-selected orders: combining
  // unrelated clusters changes their inventory balance and can create small
  // but real excess quantities.
  map<int, vector<Demand*>> demandsByCluster;
  for (auto* demand : demandsToExpand)
    demandsByCluster[demand->getCluster()].push_back(demand);
  // Match the MRP lifecycle: safety stock is replenished before customer
  // demand consumes the buffer. Include clusters with already-planned orders
  // too, since a repeat FAS run must still maintain their inventory targets.
  set<int> selectedClusters;
  for (auto* demand : demands) selectedClusters.insert(demand->getCluster());
  unordered_set<OperationPlan*> plansBeforeSafetyStock;
  for (auto plan = OperationPlan::begin(); plan != OperationPlan::end(); ++plan)
    plansBeforeSafetyStock.insert(&*plan);
  for (const auto cluster : selectedClusters) {
    if (getLogLevel() >= 1)
      logger << "FAS: planning safety stock for cluster " << cluster << '\n';
    supplyExpander.planSafetyStock(cluster);
  }
  unordered_set<OperationPlan*> safetyStockPlans;
  for (auto plan = OperationPlan::begin(); plan != OperationPlan::end(); ++plan)
    if (!plansBeforeSafetyStock.count(&*plan)) safetyStockPlans.insert(&*plan);
  // A refinement round re-runs the forward pass over the network that is
  // already in the model, so it does not expand supply and must inherit the
  // set of plans the first round held fixed instead of recomputing it from a
  // safety-stock pass it never runs.
  if (trial && !trialFixedPlans.empty()) safetyStockPlans = trialFixedPlans;
  if (!trial && getLogLevel() >= 0) {
    logger << "FAS: expanding " << demandsToExpand.size() << " orders in "
           << demandsByCluster.size() << " supply clusters\n";
  }
  if (!trial) alternateProbes = 0;
  for (auto& entry : demandsByCluster) {
    if (getLogLevel() >= 1)
      logger << "FAS: expanding cluster " << entry.first << " with "
             << entry.second.size() << " orders\n";
    supplyExpander.planSelectedDemands(entry.second, entry.first);
  }

  if (!trial && getLogLevel() >= 0) {
    size_t expandedManufacturing = 0;
    for (auto plan = OperationPlan::begin(); plan != OperationPlan::end(); ++plan)
      if (plan->getOperation() &&
          plan->getOperation()->hasType<OperationFixedTime, OperationTimePer>() &&
          !plan->getOperation()->hasType<OperationDelivery>())
        ++expandedManufacturing;
    logger << "FAS: supply expansion created " << expandedManufacturing
           << " manufacturing operationplans before forward scheduling\n";
  }

  // Build the executable scheduling graph from the supply network now
  // generated by FAS. Manufacturing, purchase and distribution receipts all
  // participate, so their dates are determined in one forward pass.
  const Date current = Plan::instance().getCurrent();
  // Scope FAS strictly to the supply network pegged to the selected orders.
  // In particular, don't move a proposed plan which was created by a normal
  // MRP run for a different order.  Shared supply remains included naturally
  // when it is pegged to at least one selected demand.
  vector<OperationPlan*> networkRoots = trialRoots;
  if (!trial)
    for (auto* demand : demands)
      for (PeggingIterator peg(demand); peg; --peg) {
        OperationPlan* op = peg.getOperationPlan();
        if (op) networkRoots.push_back(op);
      }
  unordered_set<OperationPlan*> selectedNetwork = collectNetwork(networkRoots);
  // Build the external part of the FAS supply graph from material buffers.
  // PO/DO plans aren't connected by an OperationDependency, so relying on
  // MRP pegging here would retain their backward-planned dates. The scan is
  // read-only and is repeated only as the set grows: PO -> DO -> MO chains
  // are therefore closed without mutating a model iterator.
  bool addedSupply = true;
  while (addedSupply) {
    addedSupply = false;
    unordered_set<Buffer*> required;
    for (auto* plan : selectedNetwork)
      for (auto flow = plan->beginFlowPlans(); flow != plan->endFlowPlans();
           ++flow)
        if (flow->getQuantity() < -ROUNDING_ERROR && flow->getBuffer())
          required.insert(flow->getBuffer());
    vector<OperationPlan*> additions;
    for (auto plan = OperationPlan::begin(); plan != OperationPlan::end(); ++plan) {
      OperationPlan* supply = &*plan;
      if (selectedNetwork.count(supply) || !isMovable(supply) ||
          (supply->getOrderType() != "PO" && supply->getOrderType() != "DO"))
        continue;
      for (auto flow = supply->beginFlowPlans(); flow != supply->endFlowPlans();
           ++flow)
        if (flow->getQuantity() > ROUNDING_ERROR && flow->getBuffer() &&
            required.count(flow->getBuffer())) {
          additions.push_back(supply);
          break;
        }
    }
    for (auto* supply : additions)
      if (selectedNetwork.insert(supply).second) addedSupply = true;
  }
  // Safety-stock replenishment is deliberately not pegged to a selected
  // sales order. SolverCreate has already placed it with the normal MRP
  // capacity/bucket rules. Keep that independent network fixed: moving it
  // together with an order-owned FAS graph can cross an alternate/split
  // structure outside the selected pegging tree. It must never be treated as
  // an immutable *capacity gap* though; the reservation scan below retains
  // it as existing load for all customer-order plans.
  // A project-style operation dependency isn't necessarily represented by a
  // material peg.  Close the selected network over its prerequisite links so
  // FAS schedules the actual predecessor instead of treating its old MRP
  // date as an immutable boundary.  Traverse only blocked-by links: adding
  // successors here could pull unrelated orders into this order-owned pass.
  // A routing/split/alternate parent is structural; schedule only executable
  // leaves, in the same dependency-aware order for committed and trial runs.
  const ExecutionGraph graph =
      buildExecutionGraph(selectedNetwork, safetyStockPlans);
  const vector<OperationPlan*>& work = graph.work;
  if (work.empty()) {
    if (trial) trialCompletion = finishOf(networkRoots);
    return;
  }
  const unordered_set<OperationPlan*> inScope(work.begin(), work.end());
  bool hasUnscheduledBucketSupply = false;

  unordered_map<const Resource*, vector<Reservation>> reservations;
  reserveFixedNetwork(inScope, reservations);
  size_t moved = 0;
  // Move commands are applied only at commit.  Keep the newly selected dates
  // separately so direct material and operation-dependency successors see
  // their predecessor's forward date in this same pass.
  unordered_map<OperationPlan*, Date> scheduledEnd;
  unordered_map<OperationPlan*, Date> scheduledStart;
  // Quantity already allocated from each producing flowplan to an earlier
  // FAS consumer.  Material availability must be batch-specific: an
  // operationplan-wide pegging iterator can traverse unrelated flows of the
  // same network and make a downstream batch wait for the wrong operation.
  unordered_map<const FlowPlan*, double> allocatedSupply;
  // Index producing flowplans by buffer once.  materialReady is called for
  // every dispatch key and again on place; scanning the global OperationPlan
  // list each time is O(N×M) and dominated CCPL runs once lot counts grow.
  unordered_map<const Buffer*, vector<FlowPlan*>> supplyByBuffer;
  for (auto candidate = OperationPlan::begin();
       candidate != OperationPlan::end(); ++candidate) {
    OperationPlan* producer = &*candidate;
    if (!producer->getOperation()) continue;
    for (auto produce = producer->beginFlowPlans();
         produce != producer->endFlowPlans(); ++produce) {
      if (produce->getQuantity() <= ROUNDING_ERROR || !produce->getBuffer())
        continue;
      supplyByBuffer[produce->getBuffer()].push_back(&*produce);
    }
  }
  const auto materialAvailable =
      [&scheduledEnd](OperationPlan* producer, OperationPlan* consumer) {
        if (!producer || !consumer) return Date::infiniteFuture;
        // Routing parents post the FG receipt but aren't FAS-placed.  Prefer
        // the latest already-scheduled executable leaf under the parent so a
        // delivery never reads a stale MRP parent end.
        Date producerEnd = Date::infinitePast;
        bool sawScheduledLeaf = false;
        if (producer->getSubOperationPlans(false) != OperationPlan::end()) {
          vector<OperationPlan*> stack{producer};
          while (!stack.empty()) {
            OperationPlan* node = stack.back();
            stack.pop_back();
            bool hasChild = false;
            for (OperationPlan::iterator child(node); child != node->end();
                 ++child) {
              hasChild = true;
              stack.push_back(&*child);
            }
            if (hasChild) continue;
            auto scheduled = scheduledEnd.find(node);
            if (scheduled != scheduledEnd.end()) {
              producerEnd = max(producerEnd, scheduled->second);
              sawScheduledLeaf = true;
            }
          }
        }
        if (!sawScheduledLeaf) {
          const auto end = scheduledEnd.find(producer);
          producerEnd = end != scheduledEnd.end() ? end->second
                                                  : producer->getEnd();
        }
        Date available = Date::infiniteFuture;
        // Pegging tells us which producer contributes to this consumer. Find
        // the matching positive flow and use its own material event date.
        // A FlowStart output can be available at operation start (plus its
        // offset); FlowEnd remains available at operation completion. A
        // transfer-batch flow owns multiple material events spread over the
        // operation duration. The first positive event is the earliest point
        // at which the downstream operation can start consuming this pegged
        // supply; later batches continue to arrive through the normal buffer
        // timeline while both operations overlap.
        for (auto consume = consumer->beginFlowPlans();
             consume != consumer->endFlowPlans(); ++consume) {
          if (consume->getQuantity() >= -ROUNDING_ERROR) continue;
          for (auto produce = producer->beginFlowPlans();
               produce != producer->endFlowPlans(); ++produce) {
            if (produce->getQuantity() <= ROUNDING_ERROR ||
                produce->getBuffer() != consume->getBuffer())
              continue;
            Flow* flow = produce->getFlow();
            Date date = producerEnd;
            if (flow && flow->hasType<FlowTransferBatch>()) {
              // A consumer with a FlowStart input consumes its full material
              // requirement when it starts. Such a consumer can't be
              // released merely because a smaller first producer batch has
              // arrived. FlowEnd consumers consume at completion and can
              // safely overlap once the first transfer batch is available.
              bool consumesAtStart = false;
              for (auto input = consumer->beginFlowPlans();
                   input != consumer->endFlowPlans(); ++input)
                if (input->getQuantity() < -ROUNDING_ERROR &&
                    input->getBuffer() == consume->getBuffer() &&
                    input->getFlow() && input->getFlow()->hasType<FlowStart>()) {
                  consumesAtStart = true;
                  break;
                }
              Date firstBatch = Date::infiniteFuture;
              double firstBatchQuantity = 0.0;
              for (auto batch = producer->beginFlowPlans();
                   batch != producer->endFlowPlans(); ++batch)
                if (batch->getFlow() == flow &&
                    batch->getQuantity() > ROUNDING_ERROR &&
                    batch->getDate() < firstBatch) {
                  firstBatch = batch->getDate();
                  firstBatchQuantity = batch->getQuantity();
                }
              if (firstBatch != Date::infiniteFuture &&
                  (!consumesAtStart ||
                   firstBatchQuantity + ROUNDING_ERROR >=
                       fabs(consume->getQuantity())))
                date = firstBatch;
            } else if (flow && (flow->hasType<FlowStart>() ||
                                flow->hasType<FlowEnd>()))
              // The model's flow date is an inventory-accounting instant.
              // For a normal (non-transfer) production lot FAS must release
              // the downstream operation only after the lot is completed.
              // Do not use computeOperationToFlowDate here: a FlowStart
              // output maps that date back to the producer start.
              date = producerEnd;
            available = min(available, date);
          }
        }
        // A structural/upstream iterator can include a plan from a different
        // flow of the same network. It isn't material supply for this
        // consumer unless a positive flow on the exact same buffer matched
        // above. Hard operation dependencies are handled separately below.
        return available;
      };
  // Date at which the batches this operation consumes are supplied. Called
  // read-only to compute the dispatch key of the forward pass, and again with
  // `allocate` while the operation is placed, where the batches it takes are
  // recorded so a later consumer sees the remaining supply.
  const auto materialReady = [&](OperationPlan* op, bool allocate) {
    Date result = Date::infinitePast;
    // Allocate the exact material batch in forward FIFO order.  Only a
    // positive flowplan on the same buffer can supply this consumption.
    // This is deliberately independent of MRP's backward pegging dates.
    for (auto consume = op->beginFlowPlans(); consume != op->endFlowPlans();
         ++consume) {
      if (consume->getQuantity() >= -ROUNDING_ERROR) continue;
      double remaining = -consume->getQuantity();
      struct SupplyCandidate {
        FlowPlan* flowplan;
        OperationPlan* operationplan;
        Date available;
      };
      vector<SupplyCandidate> candidates;
      const auto supply = supplyByBuffer.find(consume->getBuffer());
      if (supply != supplyByBuffer.end()) {
        for (FlowPlan* produce : supply->second) {
          OperationPlan* producer = produce->getOperationPlan();
          if (!producer || producer == op || !producer->getOperation()) continue;
          // A producer that hasn't been placed yet still represents finite
          // supply only after FAS stamps it. Using its stale MRP completion
          // here lets a delivery/orphan consumer promise before the batch is
          // actually finished (seen as temporary FG buffer shortages).
          Date available = Date::infiniteFuture;
          if (isMovable(producer) && inScope.count(producer) &&
              !scheduledEnd.count(producer))
            available = Date::infiniteFuture;
          else
            available = materialAvailable(producer, op);
          candidates.push_back({produce, producer, available});
        }
      }
      stable_sort(candidates.begin(), candidates.end(),
                  [](const SupplyCandidate& left,
                     const SupplyCandidate& right) {
                    if (left.available != right.available)
                      return left.available < right.available;
                    return left.operationplan->getReference() <
                           right.operationplan->getReference();
                  });
      Date materialDate = Date::infinitePast;
      bool waitingOnUnscheduled = false;
      for (const auto& candidate : candidates) {
        if (candidate.available == Date::infiniteFuture) {
          waitingOnUnscheduled = true;
          continue;
        }
        const double availableQuantity =
            candidate.flowplan->getQuantity() -
            allocatedSupply[candidate.flowplan];
        if (availableQuantity <= ROUNDING_ERROR) continue;
        const double allocated = min(remaining, availableQuantity);
        if (allocate) allocatedSupply[candidate.flowplan] += allocated;
        remaining -= allocated;
        materialDate = max(materialDate, candidate.available);
        if (remaining <= ROUNDING_ERROR) break;
      }
      // Leave an unpegged/infinite-buffer input to the standard model.  A
      // finite input with known FAS supply must not start before its allocated
      // batch has completed.  If the remaining requirement still sits on an
      // in-scope producer FAS hasn't placed yet, block this consumer.
      if (remaining > ROUNDING_ERROR && waitingOnUnscheduled)
        result = Date::infiniteFuture;
      else if (materialDate != Date::infinitePast)
        result = max(result, materialDate);
    }
    return result;
  };
  // Everything that makes an operation ready to run, excluding machine
  // availability: that is resolved by the capacity search when the operation
  // is placed. Including a machine term here would delay the dispatch of
  // fully released work behind a queue it does not actually wait for.
  const auto readyDate = [&](OperationPlan* op) {
    Date ready = isLeadTimeConstrained(op->getOperation())
                     ? current
                     : op->getStart();
    // Routing operations form an explicit sequential chain.  Material pegging
    // and alternate structures can release a child independently, so the
    // model's authoritative previous-suboperation boundary is enforced here.
    if (OperationPlan* previous = op->getPrevSubOpplan()) {
      auto scheduled = scheduledEnd.find(previous);
      const Date predecessorEnd = scheduled != scheduledEnd.end()
                                      ? scheduled->second
                                      : previous->getEnd();
      if (predecessorEnd != Date::infiniteFuture)
        ready = max(ready, predecessorEnd);
    }
    // Alternate/routing nodes are non-executable parents. Their sibling links
    // sit one level above the movable leaf plans, so a leaf may have no direct
    // prevSubOpplan even though its alternate station must follow the
    // preceding station. Walk to the previous sibling and use its last leaf
    // completion as an additional hard boundary.
    OperationPlan* routingNode = op;
    OperationPlan* previousLeaf = nullptr;
    while (routingNode && routingNode->getOwner()) {
      if (OperationPlan* previous = routingNode->getPrevSubOpplan()) {
        previousLeaf = previous;
        while (previousLeaf->getSubOperationPlans(false) !=
               OperationPlan::end())
          previousLeaf = &*previousLeaf->getSubOperationPlans(false);
        break;
      }
      routingNode = routingNode->getOwner();
    }
    if (previousLeaf && previousLeaf != op) {
      auto scheduled = scheduledEnd.find(previousLeaf);
      const Date predecessorEnd = scheduled != scheduledEnd.end()
                                      ? scheduled->second
                                      : previousLeaf->getEnd();
      if (predecessorEnd != Date::infiniteFuture)
        ready = max(ready, predecessorEnd);
    }
    ready = max(ready, materialReady(op, false));
    // Respect hard operation-dependency safety time in the forward direction.
    // Soft safety lead time is a preference in MRP, not a feasibility rule;
    // applying it here would wrongly turn a soft warning into a hard delay.
    auto dependency = op->getBlockedbyIterator();
    while (auto* link = dependency.next()) {
      OperationPlan* producer = link->getFirst();
      if (!producer) continue;
      auto scheduled = scheduledEnd.find(producer);
      const Date producerEnd = scheduled != scheduledEnd.end()
                                   ? scheduled->second
                                   : producer->getEnd();
      if (producerEnd == Date::infiniteFuture) continue;
      Duration safety;
      if (auto* definition = link->getOperationDependency())
        safety = definition->getHardSafetyLeadtime();
      ready = max(ready, producerEnd + safety);
    }
    return ready;
  };

  // A bucket load consumes a quantity at one model-calculated instant, rather
  // than over a continuous interval.  Trial moves deliberately use the model
  // itself to map a bucket instant back to an operationplan date.  This keeps
  // LoadBucketizedFromStart/FromEnd/Percentage offsets, resource efficiency
  // and operation calendars identical to SolverCreate.
  const auto hasBucketLoad = [](const OperationPlan* candidate) {
    for (auto load = candidate->beginLoadPlans();
         load != candidate->endLoadPlans(); ++load)
      if (load->getQuantity() < -ROUNDING_ERROR && load->getResource() &&
          load->getResource()->hasType<ResourceBuckets>())
        return true;
    return false;
  };
  const auto isBucketAllocatable = [](const OperationPlan* candidate) {
    // A time-per operation is already split by the shared supply expander at
    // its normal lot-size boundaries. FAS owns the forward placement of each
    // resulting executable piece, without changing its quantity.
    unsigned int buckets = 0;
    for (auto load = candidate->beginLoadPlans();
         load != candidate->endLoadPlans(); ++load) {
      if (load->getQuantity() >= -ROUNDING_ERROR || !load->getResource())
        continue;
      if (load->getResource()->hasType<ResourceBuckets>()) {
        ++buckets;
        continue;
      }
    }
    // Multiple bucket resources are safe here: every trial maps the candidate
    // through every load offset and accepts it only when every resulting
    // bucket timeline remains non-negative.  On a failure it advances to the
    // earliest overloaded bucket boundary and tries again.
    if (!buckets) return false;
    return true;
  };
  const auto bucketMoveIsFeasible = [](const OperationPlan* candidate) {
    for (auto load = candidate->beginLoadPlans();
         load != candidate->endLoadPlans(); ++load) {
      if (load->getQuantity() >= -ROUNDING_ERROR || !load->getResource() ||
          !load->getResource()->hasType<ResourceBuckets>())
        continue;
      // The first value is the lowest remaining capacity until the next
      // bucket boundary. It includes the just-tried load and all existing
      // loads, efficiency and fixed load quantity.
      if (get<0>(load->getBucketEnd()) < -ROUNDING_ERROR) return false;
    }
    return true;
  };
  const auto continuousMoveIsFeasible =
      [&](const OperationPlan* candidate, Date& retry) {
        retry = Date::infiniteFuture;
        for (auto load = candidate->beginLoadPlans();
             load != candidate->endLoadPlans(); ++load) {
          const Resource* resource = load->getResource();
          if (!isCapacityConstrained() || !resource ||
              !resource->getConstrained() || resource->isGroup() ||
              resource->hasType<ResourceBuckets>())
            continue;
          const double required = fabs(load->getQuantity());
          vector<Date> points{candidate->getStart(), candidate->getEnd()};
          Calendar* calendar = resource->getMaximumCalendar();
          if (calendar) {
            Calendar::EventIterator event(calendar, candidate->getStart(), true);
            Date previous = event.getDate();
            for (unsigned int n = 0; n < 10000 &&
                                      event.getDate() < candidate->getEnd(); ++n) {
              if (event.getDate() > candidate->getStart())
                points.push_back(event.getDate());
              ++event;
              if (event.getDate() <= previous) break;
              previous = event.getDate();
            }
          }
          for (const auto& reservation : reservations[resource])
            if (reservation.start < candidate->getEnd() &&
                reservation.end > candidate->getStart()) {
              points.push_back(max(reservation.start, candidate->getStart()));
              points.push_back(min(reservation.end, candidate->getEnd()));
            }
          sort(points.begin(), points.end());
          points.erase(unique(points.begin(), points.end()), points.end());
          for (size_t p = 0; p + 1 < points.size(); ++p) {
            const Date check = points[p];
            double used = 0.0;
            Date release = Date::infiniteFuture;
            for (const auto& reservation : reservations[resource])
              if (reservation.start <= check && reservation.end > check) {
                used += reservation.quantity;
                release = min(release, reservation.end);
              }
            const double capacity = calendar ? calendar->getValue(check)
                                             : resource->getMaximum();
            // calculateOperationTime already skips zero-capacity calendar
            // intervals. They are non-working gaps in a sparse CCPL calendar,
            // not overloaded production intervals to reject here.
            if (capacity > ROUNDING_ERROR &&
                used + required > capacity + ROUNDING_ERROR) {
              // A shortage can be caused either by an existing reservation
              // or by a capacity-calendar interval.  In the latter case
              // there is no reservation release, so the next calendar
              // boundary is the first meaningful retry point.
              retry = min(retry,
                          min(release, points[p + 1]));
              return false;
            }
          }
        }
        return true;
      };
  // Forward dispatch. Place manufacturing orders in LPT (or configured)
  // decision order; within each job place ops by operation name. Orphan
  // PO/DO are placed first so material receipts exist before consumers.
  // Placement still applies materialReady + capacity calendars.
  const auto jobOf = [](const OperationPlan* op) -> OperationPlan* {
    return op ? op->getTopOwner() : nullptr;
  };
  const FASJobTieBreak jobTieBreak = fasJobTieBreakFromEnv();
  // Match Python WorkOrder.duration: fixed + duration_per * qty (not the
  // calendar-stretched MRP window). Wrong durations scramble LPT order.
  const auto leafProcessingDuration = [](const OperationPlan* op) -> Duration {
    if (!op || !op->getOperation()) return Duration(1L);
    Operation* oper = op->getOperation();
    long seconds = 0;
    if (oper->hasType<OperationTimePer>()) {
      auto* timePer = static_cast<OperationTimePer*>(oper);
      seconds = timePer->getDuration().getSeconds();
      const double per = timePer->getDurationPer();
      if (per > 0.0)
        seconds += static_cast<long>(per * op->getQuantity() + 0.5);
    } else if (oper->hasType<OperationFixedTime>()) {
      seconds = static_cast<OperationFixedTime*>(oper)->getDuration().getSeconds();
    } else if (op->getEnd() > op->getStart()) {
      seconds = (op->getEnd() - op->getStart()).getSeconds();
    }
    if (seconds < 1) seconds = 1;
    return Duration(seconds);
  };
  unordered_map<OperationPlan*, Duration> durationByJob;
  for (auto* op : work) {
    OperationPlan* job = jobOf(op);
    if (!job || !job->getOperation()) continue;
    // Only routing MO leaves contribute to the LPT key (same as Python WOs).
    if (job->getOperation()->getName().find("_ROUTING") == string::npos) continue;
    if (!op->getOperation()->hasType<OperationFixedTime, OperationTimePer>())
      continue;
    durationByJob[job] = durationByJob[job] + leafProcessingDuration(op);
  }
  if (!trial && getLogLevel() >= 0) {
    const char* label = "lpt";
    if (jobTieBreak == FASJobTieBreak::Reference) label = "reference";
    else if (jobTieBreak == FASJobTieBreak::Name) label = "name";
    else if (jobTieBreak == FASJobTieBreak::Spt) label = "spt";
    logger << "FAS: job tie-break = " << label
           << ", LPT jobs with duration=" << durationByJob.size() << '\n';
    if (!durationByJob.empty() && jobTieBreak == FASJobTieBreak::Lpt) {
      vector<pair<long, string>> ranked;
      ranked.reserve(durationByJob.size());
      for (const auto& entry : durationByJob)
        ranked.emplace_back(
            entry.second.getSeconds(),
            entry.first->getOperation() ? entry.first->getOperation()->getName()
                                        : entry.first->getReference());
      sort(ranked.begin(), ranked.end(),
           [](const auto& a, const auto& b) {
             if (a.first != b.first) return a.first > b.first;
             return a.second < b.second;
           });
      logger << "FAS: LPT top";
      for (size_t i = 0; i < min<size_t>(5, ranked.size()); ++i)
        logger << ' ' << ranked[i].second << '(' << ranked[i].first << "s)";
      logger << '\n';
    }
  }
  // Match Python's DLVR→demand join via pegging (MO.demand_id is often null).
  unordered_map<OperationPlan*, Demand*> demandByJob;
  for (auto demand = Demand::begin(); demand != Demand::end(); ++demand) {
    if (demand->getStatus() != Demand::STATUS_OPEN) continue;
    for (PeggingIterator peg(&*demand); peg; --peg) {
      OperationPlan* plan = peg.getOperationPlan();
      if (!plan) continue;
      OperationPlan* top = plan->getTopOwner();
      if (top) demandByJob.emplace(top, &*demand);
    }
  }
  const auto demandOfJob =
      [&demandByJob](const OperationPlan* job) -> Demand* {
    if (!job) return nullptr;
    if (Demand* direct = job->getDemand()) return direct;
    const auto found = demandByJob.find(const_cast<OperationPlan*>(job));
    return found == demandByJob.end() ? nullptr : found->second;
  };
  const auto planOrder = [&demandOfJob, jobTieBreak, &durationByJob](
                             const OperationPlan* left,
                             const OperationPlan* right) {
    const OperationPlan* leftJob = left->getTopOwner();
    const OperationPlan* rightJob = right->getTopOwner();
    Demand* leftDemand = demandOfJob(leftJob);
    Demand* rightDemand = demandOfJob(rightJob);
    if (leftDemand != rightDemand) {
      if (!leftDemand) return false;
      if (!rightDemand) return true;
      if (leftDemand->getPriority() != rightDemand->getPriority())
        return leftDemand->getPriority() < rightDemand->getPriority();
      if (leftDemand->getDue() != rightDemand->getDue())
        return leftDemand->getDue() < rightDemand->getDue();
    }
    if (leftJob != rightJob)
      return fasJobTieLess(leftJob, rightJob, jobTieBreak, durationByJob);
    return left->getReference() < right->getReference();
  };
  const auto jobOrder = [&demandOfJob, jobTieBreak, &durationByJob](
                            const OperationPlan* left,
                            const OperationPlan* right) {
    Demand* leftDemand = demandOfJob(left);
    Demand* rightDemand = demandOfJob(right);
    if (leftDemand || rightDemand) {
      if (!leftDemand) return false;
      if (!rightDemand) return true;
      if (leftDemand->getPriority() != rightDemand->getPriority())
        return leftDemand->getPriority() < rightDemand->getPriority();
      if (leftDemand->getDue() != rightDemand->getDue())
        return leftDemand->getDue() < rightDemand->getDue();
    }
    return fasJobTieLess(left, right, jobTieBreak, durationByJob);
  };
  vector<OperationPlan*> jobSequence;
  unordered_set<OperationPlan*> seenJob;
  unordered_set<OperationPlan*> sequencedJobs;
  const auto isSequencedJob = [](const OperationPlan* job) {
    if (!job || !job->getOperation()) return false;
    // FJSP routing MOs look like J001_ROUTING. Purchase/distribution supply
    // stays always-eligible so it can clear before the next MO needs material.
    const string& name = job->getOperation()->getName();
    return name.find("_ROUTING") != string::npos;
  };
  for (auto* op : work) {
    OperationPlan* job = jobOf(op);
    if (!job || !isSequencedJob(job)) continue;
    sequencedJobs.insert(job);
    if (seenJob.insert(job).second) jobSequence.push_back(job);
  }
  if (!overrideJobSequence.empty()) {
    // Trial / improve pass: keep the caller-supplied decision order.
    jobSequence = overrideJobSequence;
  } else {
    stable_sort(jobSequence.begin(), jobSequence.end(), jobOrder);
  }
  // Preferred LPT decision order (rank among ready ops). Deliveries are
  // ranked after manufacturing so a ready-queue tie still finishes the
  // producing leaf before promising the batch.
  unordered_map<OperationPlan*, vector<OperationPlan*>> opsByJob;
  vector<OperationPlan*> orphans;
  vector<OperationPlan*> deliveries;
  for (auto* op : work) {
    OperationPlan* job = jobOf(op);
    if (job && sequencedJobs.count(job))
      opsByJob[job].push_back(op);
    else if (op->getOperation() &&
             op->getOperation()->hasType<OperationDelivery>())
      deliveries.push_back(op);
    else
      orphans.push_back(op);
  }
  const auto withinJobOrder = [](const OperationPlan* left,
                                   const OperationPlan* right) {
    const auto seq = [](const OperationPlan* op) -> int {
      if (!op || !op->getOperation()) return 0;
      const string& name = op->getOperation()->getName();
      // J001_O12 / ..._O03 → numeric stage, not lexicographic (O10 < O09).
      const size_t pos = name.rfind("_O");
      if (pos == string::npos) return 0;
      try {
        return stoi(name.substr(pos + 2));
      } catch (...) {
        return 0;
      }
    };
    const int leftSeq = seq(left);
    const int rightSeq = seq(right);
    if (leftSeq != rightSeq) return leftSeq < rightSeq;
    return left->getReference() < right->getReference();
  };
  stable_sort(orphans.begin(), orphans.end(), planOrder);
  stable_sort(deliveries.begin(), deliveries.end(), planOrder);
  vector<OperationPlan*> preferredOrder;
  preferredOrder.reserve(work.size());
  for (auto* op : orphans) preferredOrder.push_back(op);
  for (auto* job : jobSequence) {
    auto& ops = opsByJob[job];
    stable_sort(ops.begin(), ops.end(), withinJobOrder);
    for (auto* op : ops) preferredOrder.push_back(op);
  }
  for (auto* op : deliveries) preferredOrder.push_back(op);
  unordered_map<OperationPlan*, size_t> placeRank;
  for (size_t i = 0; i < preferredOrder.size(); ++i)
    placeRank[preferredOrder[i]] = i;
  // Dispatch by execution-graph readiness (header contract): an op becomes
  // eligible only after every material/routing predecessor leaf is placed.
  // Among ready ops keep the LPT preferred order.  Without this, orphan
  // deliveries were stamped on stale MRP receipt dates and then left early
  // when improve/forward delayed the producing routing.
  unordered_map<OperationPlan*, size_t> remainingIndegree = graph.indegree;
  const auto rankOf = [&placeRank](const OperationPlan* op) -> size_t {
    const auto found = placeRank.find(const_cast<OperationPlan*>(op));
    return found == placeRank.end() ? numeric_limits<size_t>::max()
                                    : found->second;
  };
  auto readyLess = [&rankOf](const OperationPlan* left,
                             const OperationPlan* right) {
    const size_t leftRank = rankOf(left);
    const size_t rightRank = rankOf(right);
    if (leftRank != rightRank) return leftRank > rightRank;  // min-heap
    return left->getReference() > right->getReference();
  };
  priority_queue<OperationPlan*, vector<OperationPlan*>, decltype(readyLess)>
      ready(readyLess);
  for (auto* op : work)
    if (remainingIndegree[op] == 0) ready.push(op);
  vector<OperationPlan*> placeOrder;
  placeOrder.reserve(work.size());
  unordered_set<OperationPlan*> queued;
  while (!ready.empty()) {
    OperationPlan* op = ready.top();
    ready.pop();
    if (!queued.insert(op).second) continue;
    placeOrder.push_back(op);
    const auto successors = graph.downstream.find(op);
    if (successors == graph.downstream.end()) continue;
    for (auto* succ : successors->second) {
      if (remainingIndegree[succ] == 0) continue;
      if (--remainingIndegree[succ] == 0) ready.push(succ);
    }
  }
  for (auto* op : preferredOrder)
    if (queued.insert(op).second) placeOrder.push_back(op);
  unordered_set<OperationPlan*> dispatched;
  for (auto* op : placeOrder) {
    if (!op || dispatched.count(op)) continue;
    dispatched.insert(op);
    // Choose pooled resources as one combination.  Greedily picking the first
    // available operator and then the first available tool can select the
    // same shared member twice, or leave a later common slot unused.  FAS
    // keeps MRP's load definitions and eligibility rules, but searches the
    // bounded Cartesian product of qualified concrete members to minimize the
    // earliest finish — matching caixin `fjsp_forward_schedule` score
    // (slot_end, slot_start, …). Precedence/material ready must be known
    // before the choice; previously members were probed from `current` and
    // a late-predecessor op could pick a busy early machine.
    const Date opReadyBound =
        max(readyDate(op), materialReady(op, false));
    struct PoolChoices {
      LoadPlan* load;
      vector<pair<Resource*, Date>> members;  // resource -> earliest start
    };
    vector<PoolChoices> pooledLoads;
    for (auto load = op->beginLoadPlans(); load != op->endLoadPlans(); ++load) {
      Load* definition = load->getLoad();
      if (!definition || load->getQuantity() >= -ROUNDING_ERROR ||
          !definition->getResource()->isGroup())
        continue;
      PoolChoices choices{&*load, {}};
      // Estimate the first interval in which this member can host the whole
      // operation via reservation gaps and capacity calendars.
      Duration opDuration;
      op->getOperation()->calculateOperationTime(op, op->getStart(), op->getEnd(),
                                                 &opDuration, true);
      // The pool load defines the required capacity.  Don't derive it from
      // the member currently selected by MRP: a different member needn't
      // have a matching loadplan, and falling back to 1 would make the FAS
      // trial incorrectly accept an undersized member.
      const double required = fabs(load->getQuantity());
      const auto memberReady = [&](Resource* memberResource) {
        // Start from precedence/material ready, then walk reservation gaps
        // (and calendars) so idle slots on unary machines can be filled.
        Date ready = max(opReadyBound, current);
        Calendar* calendar = memberResource->getMaximumCalendar();
        for (unsigned short attempt = 0; attempt < 1000; ++attempt) {
          vector<Date> points{ready, ready + opDuration};
          if (calendar) {
            Calendar::EventIterator event(calendar, ready, true);
            Date previous = event.getDate();
            for (unsigned int n = 0; n < 10000 &&
                                      event.getDate() < ready + opDuration; ++n) {
              if (event.getDate() > ready) points.push_back(event.getDate());
              ++event;
              if (event.getDate() <= previous) break;
              previous = event.getDate();
            }
          }
          for (const auto& reservation : reservations[memberResource])
            if (reservation.end > ready && reservation.start < ready + opDuration) {
              points.push_back(max(ready, reservation.start));
              points.push_back(min(ready + opDuration, reservation.end));
            }
          sort(points.begin(), points.end());
          points.erase(unique(points.begin(), points.end()), points.end());
          Date retry = Date::infiniteFuture;
          bool feasible = true;
          for (size_t p = 0; p + 1 < points.size(); ++p) {
            const Date check = points[p];
            double used = 0.0;
            Date release = Date::infiniteFuture;
            for (const auto& reservation : reservations[memberResource])
              if (reservation.start <= check && reservation.end > check) {
                used += reservation.quantity;
                release = min(release, reservation.end);
              }
            const double capacity = calendar ? calendar->getValue(check)
                                             : memberResource->getMaximum();
            // Zero capacity means this interval is outside the actual
            // processing window (night/non-working date). Calendar-aware
            // operation timing handles it; don't treat it as overload.
            if (capacity > ROUNDING_ERROR &&
                used + required > capacity + ROUNDING_ERROR) {
              feasible = false;
              retry = min(retry, min(release, points[p + 1]));
              break;
            }
          }
          if (feasible) return ready;
          if (retry == Date::infiniteFuture || retry <= ready) break;
          ready = retry;
        }
        return Date::infiniteFuture;
      };
      const auto addMember = [&](Resource* candidate) {
        if (!candidate || candidate->isGroup() || !candidate->getConstrained() ||
            (definition->getSkill() &&
             !candidate->hasSkill(definition->getSkill(), opReadyBound,
                                  opReadyBound + opDuration)))
          return;
        const Date ready = memberReady(candidate);
        if (ready != Date::infiniteFuture)
          choices.members.emplace_back(candidate, ready);
      };
      addMember(load->getResource());
      for (Resource::memberRecursiveIterator member(
               definition->getResource());
           !member.empty(); ++member) {
        addMember(&*member);
      }
      // Prefer earliest finish, then earliest start, then name (reference FAS).
      sort(choices.members.begin(), choices.members.end(),
           [&](const auto& left, const auto& right) {
             const Date leftEnd = left.second + opDuration;
             const Date rightEnd = right.second + opDuration;
             if (leftEnd != rightEnd) return leftEnd < rightEnd;
             if (left.second != right.second) return left.second < right.second;
             return left.first->getName() < right.first->getName();
           });
      choices.members.erase(unique(choices.members.begin(), choices.members.end(),
                                   [](const auto& left, const auto& right) {
                                     return left.first == right.first;
                                   }), choices.members.end());
      if (!choices.members.empty()) pooledLoads.push_back(std::move(choices));
    }
    if (!pooledLoads.empty()) {
      vector<Resource*> selected(pooledLoads.size());
      vector<Resource*> best;
      unordered_set<Resource*> used;
      Date bestFinish = Date::infiniteFuture;
      Date bestReady = Date::infiniteFuture;
      vector<size_t> searchOrder(pooledLoads.size());
      for (size_t index = 0; index < searchOrder.size(); ++index)
        searchOrder[index] = index;
      // The smallest/most restrictive pools first exposes impossible member
      // overlaps early and reduces a large Cartesian product dramatically.
      stable_sort(searchOrder.begin(), searchOrder.end(),
                  [&](size_t left, size_t right) {
                    return pooledLoads[left].members.size() <
                           pooledLoads[right].members.size();
                  });
      unsigned long nodes = 0;
      // Reuse the public resource search budget.  A pool-combination node is
      // cheaper than a full resource timeline retry, hence allow a useful
      // bounded multiple while still guaranteeing linear memory use.
      const unsigned long nodeBudget = max(
          1000UL, min(100000UL, min(1000UL, getResourceIterationMax()) * 100UL));
      Duration comboDuration;
      op->getOperation()->calculateOperationTime(op, op->getStart(), op->getEnd(),
                                                 &comboDuration, true);
      const auto choose = [&](auto&& self, size_t depth, Date ready) -> void {
        if (++nodes > nodeBudget) return;
        const Date finish = ready + comboDuration;
        if (bestFinish != Date::infiniteFuture && finish > bestFinish) return;
        if (depth == searchOrder.size()) {
          if (finish < bestFinish ||
              (finish == bestFinish && ready < bestReady) ||
              (finish == bestFinish && ready == bestReady &&
               lexicographical_compare(selected.begin(), selected.end(),
                 best.begin(), best.end(), [](Resource* left, Resource* right) {
                   return left->getName() < right->getName();
                 }))) {
            bestFinish = finish;
            bestReady = ready;
            best = selected;
          }
          return;
        }
        const size_t index = searchOrder[depth];
        for (const auto& candidate : pooledLoads[index].members) {
          // A concrete member supplies one load of this operation. Assigning
          // it twice would double-count it in independent reservation lists.
          if (used.count(candidate.first)) continue;
          if (bestFinish != Date::infiniteFuture &&
              candidate.second + comboDuration > bestFinish)
            break;
          selected[index] = candidate.first;
          used.insert(candidate.first);
          self(self, depth + 1, max(ready, candidate.second));
          used.erase(candidate.first);
          if (nodes > nodeBudget) return;
        }
      };
      choose(choose, 0, opReadyBound);
      for (size_t index = 0; index < best.size(); ++index)
        if (best[index] != pooledLoads[index].load->getResource())
          activeCommands->add(
              new CommandFASSetResource(pooledLoads[index].load, best[index]));
    }
    // Forward scheduling starts at the plan current date, not at MRP's
    // backward-planned start date. MRP remains the source of supply and
    // material pegging; FAS owns only the executable time placement.
    // With the corresponding MRP lead-time condition enabled, FAS doesn't
    // create new supply in the past. When disabled, preserve the date from
    // the supply expansion as the lower boundary, matching MRP semantics.
    Date earliest = readyDate(op);
    // Gap insertion is handled by the reservation scan below (same idea as
    // Python _find_earliest_slot): do not append-only on unary machines.
    // Allocate the material batches this operation consumes. The dispatch key
    // above only reads their readiness; the actual FIFO allocation is part of
    // placing the operation.
    earliest = max(earliest, materialReady(op, true));
    Date newStart = Date::infinitePast;
    Date newEnd = Date::infinitePast;
    if (op->getOperation()->hasType<OperationDelivery>()) {
      // Instantaneous commitment: never before material is ready, and never
      // before the demand due date. Do not keep MRP's old delivery timestamp
      // (`op->getEnd()`): after FAS moves the producing batch, that stale
      // promise leaves DLVR days after the real receipt (or, historically,
      // before it when orphans were placed first).
      Date due = Date::infinitePast;
      if (Demand* demand = op->getDemand())
        due = demand->getDue();
      else if (Demand* demand = demandOfJob(jobOf(op)))
        due = demand->getDue();
      const Date commitment = max(earliest, due);
      newStart = commitment;
      newEnd = commitment;
    } else if (hasBucketLoad(op) && isBucketAllocatable(op)) {
      // A real forward allocator for bucketized capacity.  It tries the
      // earliest material/precedence-feasible bucket, then advances to the
      // next actual bucket boundary after an overload.  The command bookmark
      // makes failed trials fully reversible, including resource timelines.
      Date candidate = earliest;
      bool placed = false;
      for (unsigned short attempt = 0; attempt < 1000; ++attempt) {
        Date mappedStart = candidate;
        for (auto load = op->beginLoadPlans(); load != op->endLoadPlans();
             ++load) {
          if (load->getQuantity() >= -ROUNDING_ERROR || !load->getResource() ||
              !load->getResource()->hasType<ResourceBuckets>())
            continue;
          mappedStart = max(mappedStart,
              load->getLoad()->getOperationPlanDate(&*load, candidate, true));
        }
        // `candidate` is the bucket load instant, whereas `earliest` is an
        // operationplan start boundary.  Loads at the end (or at a positive
        // percentage) can map an early bucket instant to a start before
        // material availability.  Advance the bucket instant by that exact
        // difference before trialling; otherwise an offset load could violate
        // the forward material/precedence boundary.
        if (mappedStart < earliest) {
          candidate += earliest - mappedStart;
          continue;
        }
        auto* bookmark = activeCommands->setBookmark();
        // Pass only the start.  This is important: calculateOperationTime
        // with a stale MRP end can swap a later start/end pair and search in
        // the wrong direction.  OperationTime's native parameter setter
        // computes the calendar-aware end from this forward start instead.
        activeCommands->add(new CommandMoveOperationPlan(
            op, mappedStart, Date::infinitePast, -1.0, false, false, false));
        Date continuousRetry;
        if (bucketMoveIsFeasible(op) &&
            continuousMoveIsFeasible(op, continuousRetry)) {
          newStart = op->getStart();
          newEnd = op->getEnd();
          ++moved;
          placed = true;
          break;
        }
        Date next = Date::infiniteFuture;
        for (auto load = op->beginLoadPlans(); load != op->endLoadPlans();
             ++load) {
          if (load->getQuantity() >= -ROUNDING_ERROR || !load->getResource() ||
              !load->getResource()->hasType<ResourceBuckets>())
            continue;
          const auto bucket = load->getBucketEnd();
          if (get<0>(bucket) < -ROUNDING_ERROR)
            next = min(next, get<1>(bucket));
        }
        activeCommands->rollback(bookmark);
        if (continuousRetry != Date::infiniteFuture)
          next = min(next, continuousRetry);
        if (next == Date::infiniteFuture) break;
        candidate = max(candidate + Duration(1L), next);
      }
      if (!placed)
        throw DataException("FAS bucket allocator couldn't find feasible capacity");
      // The successful trial has already moved the operationplan and is kept
      // in the command manager.  Suppress the generic move below.
      newStart = op->getStart();
      newEnd = op->getEnd();
    } else {
      if (hasBucketLoad(op)) hasUnscheduledBucketSupply = true;
      Duration duration;
      op->getOperation()->calculateOperationTime(op, op->getStart(), op->getEnd(),
                                                  &duration, true);
      if (duration <= Duration(0L)) {
        // A zero-lead PO/DO is an instantaneous receipt. Its former MRP
        // backward timestamp isn't a feasibility restriction in FAS, but a
        // DO can consume internally produced WIP. `earliest` already holds
        // that material-ready time: external PO stays at plan current while
        // a WIP DO cannot precede its producing MO.  Other zero-duration
        // leaves must still be stamped at `earliest` so successors release;
        // skipping them after marking dispatched left the ready queue empty
        // and hung the forward pass on CCPL WIP chains.
        newStart = earliest;
        newEnd = earliest;
      } else {
      // The operation-time calculation already intersects operation,
      // location and resource availability calendars.  Find a forward slot
      // with enough *remaining* resource capacity on every interval. This
      // is the forward equivalent of MRP's LoadPlan timeline scan: calendar
      // changes and existing load start/end events are all breakpoints.
      for (unsigned short attempt = 0; attempt < 1000; ++attempt) {
        DateRange slot = op->getOperation()->calculateOperationTime(
            op, earliest, duration, true, nullptr, true);
        Date nextEarliest = earliest;
        bool valid = true;
        for (auto load = op->beginLoadPlans(); load != op->endLoadPlans(); ++load) {
          const Resource* resource = load->getResource();
          if (!isCapacityConstrained() || !resource || !resource->getConstrained() ||
              resource->isGroup())
            continue;
          const double required = fabs(load->getQuantity());
          vector<Date> points{slot.getStart(), slot.getEnd()};
          // Capacity calendars in the planning model are bucket based. Add
          // daily checkpoints so a multi-day operation is checked across
          // every capacity-calendar day, even without another load event.
          if (resource->getMaximumCalendar())
            for (Date day = slot.getStart() + Duration(86400L);
                 day < slot.getEnd(); day += Duration(86400L))
              points.push_back(day);
          // Include every intraday capacity-calendar transition. Iterate
          // only while building the immutable candidate list; operationplan
          // moves happen after this loop, so the calendar event cache cannot
          // be invalidated during iteration.
          if (resource->getMaximumCalendar()) {
            Calendar* capcal = resource->getMaximumCalendar();
            Calendar::EventIterator event(capcal, slot.getStart(), true);
            Date previous = event.getDate();
            for (unsigned int n = 0; n < 10000 &&
                                      event.getDate() < slot.getEnd(); ++n) {
              Date boundary = event.getDate();
              if (boundary > slot.getStart()) points.push_back(boundary);
              ++event;
              if (event.getDate() <= previous) break;
              previous = event.getDate();
            }
          }
          for (const auto& reservation : reservations[resource]) {
            if (reservation.end > slot.getStart() && reservation.start < slot.getEnd()) {
              points.push_back(max(reservation.start, slot.getStart()));
              points.push_back(min(reservation.end, slot.getEnd()));
            }
          }
          Calendar* calendar = resource->getMaximumCalendar();
          sort(points.begin(), points.end());
          points.erase(unique(points.begin(), points.end()), points.end());
          for (size_t p = 0; p + 1 < points.size(); ++p) {
            const Date check = points[p];
            double used = 0.0;
            Date release = Date::infiniteFuture;
            for (const auto& reservation : reservations[resource])
              if (reservation.start <= check && reservation.end > check) {
                used += reservation.quantity;
                release = min(release, reservation.end);
              }
            const double capacity = calendar ? calendar->getValue(check)
                                             : resource->getMaximum();
            if (used + required > capacity + ROUNDING_ERROR) {
              // The next candidate is either when a current reservation
              // releases, or the next calendar day. Calendar values are
              // evaluated on every candidate interval; using a day step here
              // deliberately avoids retaining Calendar event iterators while
              // operationplan moves rebuild their calendar-dependent data.
              Date calendarRetry = check + Duration(86400L);
              if (calendar && capacity <= ROUNDING_ERROR) {
                Calendar::EventIterator next(calendar, check + Duration(1L), true);
                for (unsigned int n = 0; n < 10000 &&
                                          next.getDate() < Date::infiniteFuture; ++n) {
                  if (next.getDate() > check && next.getValue() > ROUNDING_ERROR) {
                    calendarRetry = next.getDate();
                    break;
                  }
                  Date before = next.getDate();
                  ++next;
                  if (next.getDate() <= before) break;
                }
              }
              nextEarliest = max(nextEarliest,
                                 min(release, max(points[p + 1], calendarRetry)));
              valid = false;
              break;
            }
          }
          if (!valid) break;
        }
        if (valid) {
          newStart = slot.getStart();
          newEnd = slot.getEnd();
          break;
        }
        if (nextEarliest <= earliest) {
          // EventIterator can legitimately be positioned on the same
          // boundary when a sparse calendar starts with a zero-capacity
          // interval. Find the next strictly-later positive-capacity event
          // before declaring the calendar impossible.
          Date forcedRetry = Date::infiniteFuture;
          for (auto load = op->beginLoadPlans();
               load != op->endLoadPlans(); ++load) {
            const Resource* resource = load->getResource();
            Calendar* calendar = resource ? resource->getMaximumCalendar() : nullptr;
            if (!calendar || !resource || resource->isGroup() ||
                load->getQuantity() >= -ROUNDING_ERROR)
              continue;
            Calendar::EventIterator event(calendar, earliest, true);
            Date previous = event.getDate();
            for (unsigned int n = 0; n < 10000 &&
                                      event.getDate() < Date::infiniteFuture; ++n) {
              if (event.getDate() > earliest &&
                  event.getValue() > ROUNDING_ERROR) {
                forcedRetry = min(forcedRetry, event.getDate());
                break;
              }
              ++event;
              if (event.getDate() <= previous) break;
              previous = event.getDate();
            }
          }
          if (forcedRetry == Date::infiniteFuture) {
            // A positive-capacity interval can still be fully occupied while
            // its next reservation/calendar event is outside the generated
            // event window. Move by one calendar day and let the next outer
            // iteration re-evaluate the real sparse calendar. This preserves
            // non-working dates as unavailable and avoids a false hard error.
            forcedRetry = earliest + Duration(86400L);
          }
          nextEarliest = forcedRetry;
        }
        earliest = nextEarliest;
      }
      if (newEnd == Date::infinitePast)
        throw DataException("FAS capacity-calendar search exceeded its iteration limit");
      }
    }
    if (newStart != op->getStart() || newEnd != op->getEnd()) {
      activeCommands->add(new CommandMoveOperationPlan(
          // `newEnd` is the calendar-aware endpoint selected above.  Passing
          // only a start lets setOperationPlanParameters recompute an end
          // from the original duration and can shift it away from the slot
          // that was validated against reservations.
          op, newStart, newEnd, -1.0, false, false, false));
      ++moved;
    }
    // CommandMoveOperationPlan executes immediately and is transactionally
    // reversible.  Its move goes through Operation::setOperationPlanParameters,
    // which adds the setup-matrix duration before production and updates the
    // SetupEvent on the resource timeline.  Use those actual dates below:
    // reserving the pre-setup candidate would let the next job overlap the
    // changeover even though the model has correctly expanded this job.
    const Date reservedStart = op->getStart();
    const Date reservedEnd = op->getEnd();
    // Commands are transactional: CommandMoveOperationPlan doesn't mutate
    // the persisted database until commit(), but it does mutate the in-memory
    // model. Reserve the final setup-aware dates selected in this pass.
    for (auto load = op->beginLoadPlans(); load != op->endLoadPlans(); ++load) {
      const Resource* resource = load->getResource();
      if (isCapacityConstrained() && resource && resource->getConstrained() && !resource->isGroup()) {
        const auto alreadyReserved = find_if(
            reservations[resource].begin(), reservations[resource].end(),
            [op](const Reservation& entry) { return entry.operationplan == op; });
        if (alreadyReserved == reservations[resource].end())
          reservations[resource].push_back({reservedStart, reservedEnd,
                                            fabs(load->getQuantity()), op});
      }
    }
    scheduledEnd[op] = reservedEnd;
    scheduledStart[op] = reservedStart;
    // A delivery plan is pinned to the promise date rather than to capacity,
    // so letting it into the completion would mask the schedule entirely.
    if (!op->getOperation()->hasType<OperationDelivery>())
      forwardCompletion = max(forwardCompletion, reservedEnd);
    // Successors see this op's new end through readyDate()/scheduledEnd on
    // later iterations of placeOrder — no ready-queue refill needed.
  }
  if (!trial && getLogLevel() >= 0)
    logger << "FAS: selected " << demands.size() << " orders (expanded "
           << demandsToExpand.size() << "), forward-scheduled "
           << moved << " of " << work.size() << " supply operationplans on "
           << reservations.size() << " resources, alternate probes "
           << alternateProbes << '\n';

  if (trial) {
    trialCompletion = finishOf(networkRoots);
    return;
  }

  // The forward pass is greedy: it walks the executable network once and never
  // revisits a decision, so an operation it placed behind a queue that did not
  // need to run first keeps that delay for the rest of the schedule.
  // Improve by relocating jobs in the decision sequence (same decoder, new
  // order) — matching Python fjsp_forward_schedule._improve_job_order.
  improveJobOrder(networkRoots, safetyStockPlans, jobSequence);

  // SolverCreate records capacity shortages encountered while it expands
  // demand backwards. Those are planning-trace constraints attached to the
  // demand, not live resource overload records, and they aren't updated when
  // FAS subsequently moves the affected leaf plans into valid forward slots.
  // Remove only that stale capacity trace for orders owned by this FAS pass;
  // material, lead-time and all constraints on unselected orders remain
  // untouched and continue through the normal export pipeline.
  size_t clearedCapacityConstraints = 0;
  for (auto* demand : demands) {
    // A bucketized resource wasn't moved by FAS, so its MRP feasibility
    // trace remains authoritative.  Do not silently remove it merely because
    // unrelated continuous resources were successfully forward-scheduled.
    if (hasUnscheduledBucketSupply) continue;
    auto& demandConstraints = demand->getConstraints();
    for (auto constraint = demandConstraints.begin();
         constraint != demandConstraints.end();) {
      if (!constraint->hasType<ProblemCapacityOverload>()) {
        ++constraint;
        continue;
      }
      auto* stale = &*constraint;
      demandConstraints.unlink(stale);
      ++constraint;
      delete stale;
      ++clearedCapacityConstraints;
    }
    demandConstraints.clean(demand);
  }
  if (getLogLevel() >= 0 && clearedCapacityConstraints)
    logger << "FAS: cleared " << clearedCapacityConstraints
           << " resolved capacity constraints\n";
}

void SolverFAS::improveJobOrder(
    const vector<OperationPlan*>& roots,
    const unordered_set<OperationPlan*>& fixed,
    vector<OperationPlan*>& jobSequence) {
  double improveSeconds = 10.0;
  if (const char* env = getenv("FAS_IMPROVE_SECONDS")) {
    char* end = nullptr;
    const double parsed = strtod(env, &end);
    if (end != env && parsed >= 0.0) improveSeconds = parsed;
  }
  if (improveSeconds <= 0.0 || jobSequence.size() < 2) {
    if (getLogLevel() >= 0)
      logger << "FAS: job-order improve skipped "
             << "(FAS_IMPROVE_SECONDS=" << improveSeconds << ", jobs="
             << jobSequence.size() << ")\n";
    return;
  }

  Date best = forwardCompletion;
  if (best == Date::infinitePast) return;
  const Date initial = best;
  size_t moves = 0;
  const auto clockStart = chrono::steady_clock::now();
  const auto deadline =
      clockStart + chrono::duration_cast<chrono::steady_clock::duration>(
                       chrono::duration<double>(improveSeconds));
  mt19937 rng(123456u);
  const size_t n = jobSequence.size();

  const auto timeLeft = [&]() {
    return chrono::steady_clock::now() < deadline;
  };

  const auto evaluate = [&](const vector<OperationPlan*>& trialSeq) -> Date {
    CommandManager* manager = activeCommands;
    if (!manager) return Date::infiniteFuture;
    auto* bookmark = manager->setBookmark();
    overrideJobSequence = trialSeq;
    trialFixedPlans = fixed;
    trialRoots = roots;
    CommandManager* outerTransaction = trialTransaction;
    trialTransaction = manager;
    Date reached = Date::infiniteFuture;
    try {
      solve();
      reached = forwardCompletion;
    } catch (...) {
      trialRoots.clear();
      trialTransaction = outerTransaction;
      trialFixedPlans.clear();
      overrideJobSequence.clear();
      forwardCompletion = best;
      manager->rollback(bookmark);
      throw;
    }
    trialRoots.clear();
    trialTransaction = outerTransaction;
    trialFixedPlans.clear();
    overrideJobSequence.clear();
    if (reached == Date::infinitePast || !(reached < best)) {
      manager->rollback(bookmark);
      forwardCompletion = best;
      return reached;
    }
    return reached;
  };

  // Cheap adjacent-swap pass first (same as Python).
  bool changed = true;
  while (changed && timeLeft()) {
    changed = false;
    for (size_t i = 0; i + 1 < n; ++i) {
      if (!timeLeft()) break;
      vector<OperationPlan*> trial = jobSequence;
      swap(trial[i], trial[i + 1]);
      const Date reached = evaluate(trial);
      if (reached < best) {
        jobSequence = trial;
        best = reached;
        ++moves;
        changed = true;
        break;
      }
    }
  }

  // Relocate: pull one job and reinsert elsewhere.
  uniform_int_distribution<size_t> pick(0, n - 1);
  while (timeLeft()) {
    const size_t i = pick(rng);
    size_t j = pick(rng);
    if (i == j) continue;
    vector<OperationPlan*> trial = jobSequence;
    OperationPlan* job = trial[i];
    trial.erase(trial.begin() + static_cast<long>(i));
    trial.insert(trial.begin() + static_cast<long>(j), job);
    const Date reached = evaluate(trial);
    if (reached < best) {
      jobSequence = trial;
      best = reached;
      ++moves;
    }
  }

  forwardCompletion = best;
  if (getLogLevel() >= 0) {
    const auto elapsed = chrono::duration<double>(
                             chrono::steady_clock::now() - clockStart)
                             .count();
    logger << "FAS: job-order improve moves=" << moves
           << " initial=" << initial << " best=" << best
           << " elapsed=" << elapsed << "s\n";
  }
}

Date SolverFAS::probeResourceConflict(
    const Resource* resource, Date start, Duration duration, double required,
    const unordered_set<const OperationPlan*>& own) const {
  const Date end = start + duration;
  const Calendar* calendar = resource->getMaximumCalendar();
  const double maximum = resource->getMaximum();
  // Capacity already taken when the candidate would start. The timeline holds
  // a positive event when a load starts and a matching negative event when it
  // releases, so summing them yields the used capacity of an interval. Setup
  // events carry no quantity and don't affect the sum.
  double used = 0.0;
  Date cursor = start;
  Date conflict = Date::infiniteFuture;
  for (const auto& event : resource->getLoadPlans()) {
    const Date date = event.getDate();
    if (date > start) {
      if (conflict == Date::infiniteFuture && date > cursor) {
        const double capacity = calendar ? calendar->getValue(cursor) : maximum;
        // A zero-capacity interval is a non-working gap in a sparse calendar,
        // not an overloaded production interval.
        if (capacity > ROUNDING_ERROR && used + required > capacity + ROUNDING_ERROR)
          conflict = cursor;
      }
      cursor = date;
    }
    if (date >= end) break;
    if (!own.count(event.getOperationPlan())) used += event.getQuantity();
  }
  if (conflict == Date::infiniteFuture && end > cursor) {
    const double capacity = calendar ? calendar->getValue(cursor) : maximum;
    if (capacity > ROUNDING_ERROR && used + required > capacity + ROUNDING_ERROR)
      conflict = cursor;
  }
  if (conflict == Date::infiniteFuture) return Date::infiniteFuture;
  // The earliest point at which a running load gives capacity back. This is
  // the only event type that can resolve the shortage; a new load starting
  // only makes it worse.
  for (const auto& event : resource->getLoadPlans()) {
    if (event.getDate() <= conflict) continue;
    if (event.getQuantity() < -ROUNDING_ERROR &&
        !own.count(event.getOperationPlan()))
      return event.getDate();
  }
  return Date::infiniteFuture;
}

Duration SolverFAS::contentionOf(const Resource* resource) {
  const auto cached = contentionCache.find(resource);
  if (cached != contentionCache.end()) return cached->second;
  // Count the load definitions that can consume this resource. This is a
  // structural measure of the work that can still be routed here, and it is
  // deliberately a *count*: the pool of a job family shares one definition
  // per step, so every member reports the same number and the term cancels
  // between them, while a machine outside the family reports a much smaller
  // figure and is preferred by default. A sum of definition durations would
  // instead reward the machine that is fastest for the most operations, which
  // concentrates even more work on the machine that is already the busiest.
  size_t definitions = 0;
  for (const auto& load : resource->getLoads())
    if (load.getOperation()) ++definitions;
  // A resource running two loads in parallel is contended by the same
  // definitions but has twice the room.
  const Duration result(kFASContentionUnit * definitions /
                        std::max(1.0, resource->getMaximum()));
  contentionCache[resource] = result;
  return result;
}

Date SolverFAS::probeAlternateCompletion(OperationPlan* root) {
  if (!root) return Date::infiniteFuture;
  if (!isCapacityConstrained()) return root->getEnd();
  ++alternateProbes;
  vector<OperationPlan*> leaves;
  collectExecutableLeaves(root, leaves);
  // The plans created inside the alternate bookmark are already on the
  // resource timelines; they are the work being placed, not existing load.
  unordered_set<const OperationPlan*> own;
  own.insert(root);
  for (auto* leaf : leaves) own.insert(leaf);

  const Date current = Plan::instance().getCurrent();
  // This is a forward-placement ranking, so it starts from the plan current
  // date.  The supply kernel dates every candidate of a routing step backwards
  // from the same due date, so their model dates are equal and much later than
  // the horizon this pass schedules in; letting them into the score hides the
  // queue estimate below and reduces the choice to "the fastest machine".
  Date ready = current;
  Date firstCompletion = Date::infiniteFuture;
  Duration firstDuration(0L);
  Duration contention(0L);
  for (size_t index = 0; index < leaves.size(); ++index) {
    OperationPlan* leaf = leaves[index];
    Duration duration;
    leaf->getOperation()->calculateOperationTime(leaf, leaf->getStart(),
                                                 leaf->getEnd(), &duration, true);
    if (duration < Duration(0L)) duration = Duration(0L);
    Date start = ready;
    // During supply expansion all newly committed plans still carry their
    // MRP backward dates, commonly before `current`.  A conflict-only probe
    // would therefore see every alternate machine as free at `current` and
    // repeatedly select the first alternate.  Include the work already
    // assigned to each candidate resource as a forward queue estimate.  The
    // real scheduler below will find exact calendar slots; this score only
    // distributes equal-priority alternates across their available machines.
    for (auto load = leaf->beginLoadPlans(); load != leaf->endLoadPlans(); ++load) {
      const Resource* resource = load->getResource();
      if (!resource || !resource->getConstrained() || resource->isGroup() ||
          resource->hasType<ResourceBuckets>() ||
          load->getQuantity() >= -ROUNDING_ERROR)
        continue;
      unordered_set<const OperationPlan*> counted;
      Duration queued(0L);
      for (const auto& event : resource->getLoadPlans()) {
        const OperationPlan* assigned = event.getOperationPlan();
        if (!assigned || own.count(assigned) || !counted.insert(assigned).second)
          continue;
        Duration assignedDuration;
        assigned->getOperation()->calculateOperationTime(
            assigned, assigned->getStart(), assigned->getEnd(),
            &assignedDuration, true);
        if (assignedDuration > Duration(0L)) queued += assignedDuration;
      }
      // `start` stays a physical date: it is handed to probeResourceConflict
      // below, which reads calendars and reservations at that instant. The
      // structural contention is added to the returned ranking key instead,
      // where it cannot move a feasibility probe onto the wrong shift.
      start = max(start, current + queued);
      contention = max(contention, contentionOf(resource));
    }
    // Queue behind the load that is already committed to this machine. The
    // resulting completion is what ranks the alternates: the shared MRP kernel
    // picks by cost and priority only, so with equal costs every order of a
    // pool lands on the same machine and the other members stay idle.
    for (unsigned short attempt = 0; attempt < 100; ++attempt) {
      Date conflict = Date::infiniteFuture;
      for (auto load = leaf->beginLoadPlans(); load != leaf->endLoadPlans(); ++load) {
        const Resource* resource = load->getResource();
        if (!resource || !resource->getConstrained() || resource->isGroup() ||
            resource->hasType<ResourceBuckets>() ||
            load->getQuantity() >= -ROUNDING_ERROR)
          continue;
        conflict = min(conflict, probeResourceConflict(resource, start, duration,
                                                      fabs(load->getQuantity()), own));
      }
      // No conflict means the candidate fits at `start`; a conflict that can't
      // be pushed forward (no release event) is treated the same way, since
      // retrying it would loop forever.
      if (conflict == Date::infiniteFuture || conflict <= start) break;
      start = conflict;
    }
    ready = start + duration;
    // Ranking by the earliest *start* is not enough.  The machines of one pool
    // differ widely in processing time (a transfer line mixes operations of a
    // few seconds with operations of an hour), so the machine that starts a
    // minute earlier but runs three times as long is the worse choice.
    if (!index) {
      firstCompletion = ready;
      firstDuration = duration;
    }
  }
  // Ranking key ≈ reference FAS earliest finish (slot_end). With
  // kFASQueueWeight==1 this is firstCompletion (+ contention tie-break).
  // Weight>1 would add extra multiples of firstDuration and prefer shorter
  // ops even when their queued finish is later.
  return firstCompletion +
         Duration((kFASQueueWeight - 1L) * firstDuration.getSeconds()) +
         contention;
}

Date SolverFAS::trialForwardNetwork(OperationPlan* root,
                                    CommandManager* transaction) {
  if (!root || !transaction) return Date::infiniteFuture;
  if (!trialRoots.empty())
    throw LogicException("Nested FAS alternate-path trials aren't supported");
  trialRoots.push_back(root);
  trialTransaction = transaction;
  trialCompletion = Date::infiniteFuture;
  try {
    solve();
  } catch (...) {
    trialRoots.clear();
    trialTransaction = nullptr;
    trialCompletion = Date::infiniteFuture;
    throw;
  }
  trialRoots.clear();
  trialTransaction = nullptr;
  return trialCompletion;
}

Date SolverFAS::runForwardTrial(void* context, OperationPlan* root,
                                CommandManager*) {
  // The alternate hook runs once per candidate machine of every routing step,
  // so it must stay cheap. Scheduling the complete supply network here (what
  // trialForwardNetwork does) costs a full forward pass per candidate and
  // makes expansion orders of magnitude slower; probing the resources the
  // candidate would occupy gives the same ranking signal for a fraction of it.
  return static_cast<SolverFAS*>(context)->probeAlternateCompletion(root);
}

}  // namespace ccAPPS

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
#include "ccAPPS/solverlocal.h"

namespace ccAPPS {

const MetaClass* SolverFAS::metadata;

namespace {

// Personnel pools are reusable while an operation is paused. Equipment and
// molds remain assigned across a pause to preserve their existing semantics.
vector<DateRange> capacityWindows(const Resource* resource,
                                  const OperationPlan* op, Date start, Date end) {
  if (end <= start) return {};
  if (resource->getCategory() != "人员") return {DateRange(start, end)};
  Calendar::EventIterator calendars[10];
  const auto count = op->getOperation()->collectCalendars(calendars, start, op);
  vector<Date> points{start, end};
  for (unsigned short i = 0; i < count; ++i) {
    Date previous = calendars[i].getDate();
    for (unsigned int n = 0; n < 10000 && calendars[i].getDate() < end; ++n) {
      if (calendars[i].getDate() > start) points.push_back(calendars[i].getDate());
      ++calendars[i];
      if (calendars[i].getDate() <= previous) break;
      previous = calendars[i].getDate();
    }
  }
  sort(points.begin(), points.end());
  points.erase(unique(points.begin(), points.end()), points.end());
  vector<DateRange> result;
  for (size_t p = 0; p + 1 < points.size(); ++p) {
    bool available = true;
    for (unsigned short i = 0; i < count; ++i)
      if (!calendars[i].getCalendar()->getValue(points[p])) {
        available = false;
        break;
      }
    if (!available) continue;
    if (!result.empty() && result.back().getEnd() == points[p])
      result.back() = DateRange(result.back().getStart(), points[p + 1]);
    else
      result.emplace_back(points[p], points[p + 1]);
  }
  return result;
}

bool windowActive(const vector<DateRange>& windows, Date date) {
  return any_of(windows.begin(), windows.end(), [date](const DateRange& w) {
    return w.getStart() <= date && date < w.getEnd();
  });
}

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

// A pure insert has no historical work order, so the shared scope matches
// nothing and would skip the solve. FAS can still create that order when the
// named demand is open and short.
bool fasPendingOpenInsert() {
  if (!localRescheduleEnabled()) return false;
  const auto allow = localSupplyExpansionNames();
  for (const string& name : allow) {
    Demand* demand = Demand::find(name);
    if (!demand || demand->getStatus() != Demand::STATUS_OPEN) continue;
    if (demand->getDue() == Date::infinitePast ||
        demand->getDue() == Date::infiniteFuture)
      continue;
    if (demand->getQuantity() - demand->getPlannedQuantity() > ROUNDING_ERROR)
      return true;
  }
  return false;
}

}  // namespace

PyObject* run_fas(PyObject*, PyObject*) {
  // Shared local scope (hot/freeze). FAS only repairs the movable set when
  // local.reschedule / cp.local_reschedule is on; confirmed trees stay pinned.
  LocalScopeGuard localScope;
  applyLocalRescheduleScope(localScope);
  const bool insertWithoutRoots =
      localScope.skipOptimization && fasPendingOpenInsert();
  if (insertWithoutRoots) {
    // Pin the loaded plan. New supply created below stays proposed and is
    // the only work this pass places.
    size_t frozen = 0;
    for (auto plan = OperationPlan::begin(); plan != OperationPlan::end();
         ++plan) {
      OperationPlan* node = &*plan;
      if (!node || node->getConfirmed() || node->getCompleted() ||
          node->getClosed())
        continue;
      if (!node->getProposed() && !node->getApproved()) continue;
      const string status = node->getStatus();
      try {
        node->setBoolProperty("local_frozen", true);
        node->setBoolProperty("cp_local_frozen", true);
        node->setStatusRaw("confirmed");
        localScope.frozen.push_back({node, status});
        ++frozen;
      } catch (...) {
      }
    }
    localScope.skipOptimization = false;
    setenv("FAS_LOCAL_INSERT_ONLY", "1", 1);
    logger << "FAS local insert: no historical root work order; froze "
           << frozen
           << " loaded operationplan(s) and creating supply for the open insert\n"
           << flush;
  } else if (localScope.skipOptimization) {
    if (localLastStage() != "local_l1_lock_all" &&
        localLastStage() != "local_delete_complete" &&
        localLastStage() != "local_delete_not_applied")
      localSetStage("local_scope_empty");
    Py_RETURN_NONE;
  }
  SolverFAS fas;
  applyPlanConstraintsToFas(fas);
  fas.setLogLevel(1);
  fas.solve();
  fas.commitPlan();
  unsetenv("FAS_LOCAL_INSERT_ONLY");
  if (localRescheduleEnabled()) {
    // Outcome destructor needs a non-zero applied gate for order/resource.
    if (localLastApplied() == 0) localSetLastApplied(1);
  }
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
  // Build the full supply network even when capacity is tight. FAS owns
  // finite-capacity placement and delays work past due rather than planning
  // short / leaving orders unplanned during expansion.
  const short expandConstraints =
      getConstraints() & ~static_cast<short>(SolverCreate::CAPACITY);
  supplyExpander.setConstraints(expandConstraints);
  supplyExpander.setPlanType(2);
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
    // Don't derive execution edges from pegging iterators. Both flowplan- and
    // operationplan-level walks can cross a routing owner and return a
    // downstream sibling. Treating that result as a producer creates a false
    // cycle, eg Apply varnish -> Drying varnish from routing order plus a
    // reverse Drying -> Apply edge from pegging. Material edges are built by
    // matching positive and negative flows on the same buffer below; routing
    // and explicit dependencies are handled separately.
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
        for (const auto& w : capacityWindows(resource, &*op, op->getStart(), op->getEnd()))
          reservations[resource].push_back(
              {w.getStart(), w.getEnd(), fabs(load->getQuantity()), &*op});
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
  const bool localRepair = localRescheduleEnabled();
  if (trialRoots.empty() && !localRepair) {
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
      // FAS may produce a new work order wherever the loaded plan is short.
      // GA and CP do not; they only move work orders that already exist.
      if (demand->getQuantity() - demand->getPlannedQuantity() >
          ROUNDING_ERROR)
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
  // Insert with no historical root: expand and schedule only the named open
  // order. Other local repairs keep the previous "every short open demand"
  // selection.
  const bool localInsertOnly = [&]() {
    if (!localRepair) return false;
    const char* raw = getenv("FAS_LOCAL_INSERT_ONLY");
    return raw && *raw && *raw != '0';
  }();
  if (localInsertOnly) {
    const auto allow = localSupplyExpansionNames();
    auto named = [&](Demand* demand) {
      return demand && allow.count(demand->getName());
    };
    demands.erase(remove_if(demands.begin(), demands.end(),
                            [&](Demand* demand) { return !named(demand); }),
                  demands.end());
    demandsToExpand.erase(
        remove_if(demandsToExpand.begin(), demandsToExpand.end(),
                  [&](Demand* demand) { return !named(demand); }),
        demandsToExpand.end());
    if (getLogLevel() >= 0)
      logger << "FAS local insert: scheduling " << demands.size()
             << " open demand(s), expanding " << demandsToExpand.size()
             << "\n";
  }
  // Let the shared MRP queue kernel expand exactly FAS's selected queue. It
  // preserves MRP's batching and purchasing rules without taking over order
  // selection or scheduling dates.
  // Create the full supply network without capacity cuts; the forward pass
  // applies capa and delays work instead of planning short.
  const short expandConstraints =
      getConstraints() & ~static_cast<short>(SolverCreate::CAPACITY);
  supplyExpander.setConstraints(expandConstraints);
  supplyExpander.setPlanType(2);
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
  if (localInsertOnly) {
    // An insert adds one order onto the loaded plan. Cluster-wide excess
    // cleanup and purchase rebuild would delete unrelated proposed supply.
    // getLocalSupplyRegen() also reads LOCAL_SUPPLY_REGEN, which the
    // reschedule path sets to 1 for every FAS run, so clear it for this
    // expansion only.
    supplyExpander.setLocalSupplyRegen(false);
    setenv("LOCAL_SUPPLY_REGEN", "0", 1);
  } else {
    supplyExpander.setLocalSupplyRegen(true);
  }
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
  if (!localRepair) {
    for (const auto cluster : selectedClusters) {
      if (getLogLevel() >= 1)
        logger << "FAS: planning safety stock for cluster " << cluster << '\n';
      supplyExpander.planSafetyStock(cluster);
    }
  } else if (getLogLevel() >= 0) {
    logger << "FAS local repair: producing new work orders for "
           << demandsToExpand.size()
           << " short demand(s); safety stock off\n";
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
  if (localInsertOnly) setenv("LOCAL_SUPPLY_REGEN", "1", 1);

  // Close tiny replenishment residues on discrete, integer customer demand.
  // SolverCreate can stop after its retry limit with e.g. 17.99399399 of an
  // 18-piece demand.  That is a numerical/iteration residue, not a valid
  // partial piece.  Adjust both the pegged production root and delivery so
  // material remains balanced.  Real shortages (>= tolerance) are untouched.
  if (!trial && !localRepair) {
    double residualTolerance = 0.01;
    if (const char* raw = getenv("FAS_INTEGER_RESIDUAL_TOLERANCE"))
      try {
        residualTolerance = max(ROUNDING_ERROR, stod(raw));
      } catch (...) {
      }
    size_t closedResidues = 0;
    for (Demand* demand : demands) {
      if (!demand || demand->getStatus() != Demand::STATUS_OPEN) continue;
      const double requested = demand->getQuantity();
      if (fabs(requested - round(requested)) > ROUNDING_ERROR) continue;
      const double planned = demand->getPlannedQuantity();
      const double residue = requested - planned;
      if (residue <= ROUNDING_ERROR || residue >= residualTolerance) continue;

      OperationPlan* delivery = nullptr;
      Demand::DeliveryIterator deliveries(demand);
      while (OperationPlan* candidate = deliveries.next())
        if (candidate && candidate->getProposed()) delivery = candidate;
      if (!delivery) continue;

      OperationPlan* productionRoot = nullptr;
      double outputPerRoot = 0.0;
      unordered_set<OperationPlan*> seenRoots;
      for (PeggingIterator peg(demand); peg; --peg) {
        OperationPlan* node = peg.getOperationPlan();
        if (!node) continue;
        OperationPlan* root = node->getTopOwner();
        if (!root) root = node;
        if (!seenRoots.insert(root).second || root == delivery ||
            root->getQuantity() <= ROUNDING_ERROR)
          continue;
        double produced = 0.0;
        vector<OperationPlan*> stack{root};
        while (!stack.empty()) {
          OperationPlan* current = stack.back();
          stack.pop_back();
          for (auto flow = current->beginFlowPlans();
               flow != current->endFlowPlans(); ++flow)
            if (flow->getQuantity() > ROUNDING_ERROR && flow->getBuffer() &&
                flow->getBuffer()->getItem() == demand->getItem())
              produced += flow->getQuantity();
          for (OperationPlan::iterator child(current); child != current->end();
               ++child)
            stack.push_back(&*child);
        }
        if (produced <= ROUNDING_ERROR) continue;
        productionRoot = root;
        outputPerRoot = produced / root->getQuantity();
        break;
      }
      if (!productionRoot || outputPerRoot <= ROUNDING_ERROR) continue;

      const double oldProduction = productionRoot->getQuantity();
      productionRoot->setQuantity(oldProduction + residue / outputPerRoot,
                                  false, true, true);
      delivery->setQuantity(delivery->getQuantity() + residue, false, true,
                            true);
      ++closedResidues;
      logger << "FAS: closed integer replenishment residue demand='"
             << demand->getName() << "' residue=" << residue
             << " production=" << oldProduction << "->"
             << productionRoot->getQuantity() << " delivery="
             << delivery->getQuantity() << '\n';
    }
    if (closedResidues)
      logger << "FAS: closed " << closedResidues
             << " integer replenishment residue(s), tolerance="
             << residualTolerance << '\n';
  }

  if (!trial && getLogLevel() >= 0) {
    size_t expandedManufacturing = 0;
    for (auto plan = OperationPlan::begin(); plan != OperationPlan::end(); ++plan)
      if (plan->getOperation() &&
          plan->getOperation()->hasType<OperationFixedTime, OperationTimePer>() &&
          !plan->getOperation()->hasType<OperationDelivery>())
        ++expandedManufacturing;
    logger << (localRepair ? "FAS local repair: existing network contains "
                           : "FAS: supply expansion created ")
           << expandedManufacturing
           << " manufacturing operationplans before forward scheduling\n";
  }

  // Build the executable scheduling graph from the supply network now
  // generated by FAS. Manufacturing, purchase and distribution receipts all
  // participate, so their dates are determined in one forward pass.
  const Date current = Plan::instance().getCurrent();
  Date horizonEnd = Date::infiniteFuture;
  if (const char* rawHorizon = getenv("FAS_HORIZON_END"))
    if (*rawHorizon) horizonEnd = Date(rawHorizon);
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
  // PO/DO/MO plans aren't always connected by pegging (shared lots, sync
  // orphans). Close required buffers over every movable producer so a
  // delivery can wait for real FG instead of failing material.
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
    for (auto plan = OperationPlan::begin(); plan != OperationPlan::end();
         ++plan) {
      OperationPlan* supply = &*plan;
      if (selectedNetwork.count(supply) || !isMovable(supply)) continue;
      const string orderType = supply->getOrderType();
      if (orderType != "PO" && orderType != "DO" && orderType != "MO") continue;
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
  // Initial buffer onhand claimed by earlier FAS consumers in this pass.
  unordered_map<const Buffer*, double> allocatedOnhand;
  // Index producing flowplans by buffer once.  materialReady is called for
  // every dispatch key and again on place; scanning the global OperationPlan
  // list each time is O(N×M) and dominated CCPL runs once lot counts grow.
  unordered_map<const Buffer*, vector<FlowPlan*>> supplyByBuffer;
  for (auto candidate = OperationPlan::begin();
       candidate != OperationPlan::end(); ++candidate) {
    OperationPlan* producer = &*candidate;
    if (!producer->getOperation()) continue;
    // Opening inventory is already claimed through buffer->getOnHand().
    // Indexing the STCK flowplan as FIFO supply double-counts the same
    // quantity and lets a consumer start before real MO/PO receipts arrive.
    if (producer->getOperation()->hasType<OperationInventory>() ||
        producer->getOrderType() == "STCK")
      continue;
    for (auto produce = producer->beginFlowPlans();
         produce != producer->endFlowPlans(); ++produce) {
      if (produce->getQuantity() <= ROUNDING_ERROR || !produce->getBuffer())
        continue;
      supplyByBuffer[produce->getBuffer()].push_back(&*produce);
    }
  }
  // Reserve material already consumed by plans outside this FAS execution
  // graph.  These plans remain in the exported buffer timeline, so leaving
  // them out of allocatedSupply lets an in-scope consumer reuse the same
  // production batch.  Start with opening stock and then consume positive
  // flowplans FIFO.  The dates are intentionally irrelevant for this
  // reservation step: fixed demand owns its quantity even when its existing
  // dates expose a pre-existing shortage.
  unordered_map<const Buffer*, vector<FlowPlan*>> fixedConsumptionByBuffer;
  for (auto candidate = OperationPlan::begin();
       candidate != OperationPlan::end(); ++candidate) {
    OperationPlan* consumer = &*candidate;
    if (!consumer->getOperation()) continue;
    // Routing/alternate parents aren't executable graph leaves, but their
    // flows still belong to the selected FAS network. Treating them as fixed
    // external demand reserves the same material again for their children.
    // Safety-stock plans remain fixed and therefore keep their reservation.
    if (selectedNetwork.count(consumer) &&
        !safetyStockPlans.count(consumer))
      continue;
    for (auto consume = consumer->beginFlowPlans();
         consume != consumer->endFlowPlans(); ++consume)
      if (consume->getQuantity() < -ROUNDING_ERROR && consume->getBuffer())
        fixedConsumptionByBuffer[consume->getBuffer()].push_back(&*consume);
  }
  for (auto& entry : supplyByBuffer)
    stable_sort(entry.second.begin(), entry.second.end(),
                [](const FlowPlan* left, const FlowPlan* right) {
                  if (left->getDate() != right->getDate())
                    return left->getDate() < right->getDate();
                  return left->getOperationPlan()->getReference() <
                         right->getOperationPlan()->getReference();
                });
  for (auto& entry : fixedConsumptionByBuffer) {
    const Buffer* buffer = entry.first;
    stable_sort(entry.second.begin(), entry.second.end(),
                [](const FlowPlan* left, const FlowPlan* right) {
                  if (left->getDate() != right->getDate())
                    return left->getDate() < right->getDate();
                  return left->getOperationPlan()->getReference() <
                         right->getOperationPlan()->getReference();
                });
    double opening =
        max(0.0, buffer->getOnHand(current, false) - allocatedOnhand[buffer]);
    auto supply = supplyByBuffer.find(buffer);
    size_t supplyIndex = 0;
    for (FlowPlan* consume : entry.second) {
      double remaining = -consume->getQuantity();
      const double fromOpening = min(opening, remaining);
      opening -= fromOpening;
      allocatedOnhand[buffer] += fromOpening;
      remaining -= fromOpening;
      while (remaining > ROUNDING_ERROR &&
             supply != supplyByBuffer.end() &&
             supplyIndex < supply->second.size()) {
        FlowPlan* produce = supply->second[supplyIndex];
        const double available =
            produce->getQuantity() - allocatedSupply[produce];
        if (available <= ROUNDING_ERROR) {
          ++supplyIndex;
          continue;
        }
        const double allocated = min(remaining, available);
        allocatedSupply[produce] += allocated;
        remaining -= allocated;
        if (allocated + ROUNDING_ERROR >= available) ++supplyIndex;
      }
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
          if (end != scheduledEnd.end())
            producerEnd = end->second;
          else if (producer->getSubOperationPlans(false) !=
                   OperationPlan::end())
            // Composite receipt with no FAS-placed leaf yet: MRP's parent
            // end is not a real release. Wait / block instead of trusting it.
            return Date::infiniteFuture;
          else
            producerEnd = producer->getEnd();
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
  const auto materialReady = [&](OperationPlan* op, bool allocate,
                                 bool allowUnscheduledEnds = false) {
    Date result = Date::infinitePast;
    // Allocate the exact material batch in forward FIFO order.  Only a
    // positive flowplan on the same buffer can supply this consumption.
    // This is deliberately independent of MRP's backward pegging dates.
    for (auto consume = op->beginFlowPlans(); consume != op->endFlowPlans();
         ++consume) {
      if (consume->getQuantity() >= -ROUNDING_ERROR) continue;
      Buffer* buffer = consume->getBuffer();
      // FC marks some 自制 codes as BufferInfinite when no route leaf exists.
      // Those still must not go negative: enforce whenever the item is 自制
      // or the buffer already has producing batches in this plan.
      const bool infiniteBuffer =
          !buffer || buffer->hasType<BufferInfinite>();
      const bool selfMade =
          buffer && buffer->getItem() &&
          buffer->getItem()->getCategory() == "自制";
      const auto supplyLookup = supplyByBuffer.find(buffer);
      const bool hasMakers = supplyLookup != supplyByBuffer.end() &&
                             !supplyLookup->second.empty();
      const bool enforceMaterial =
          buffer && (!infiniteBuffer || selfMade || hasMakers);
      double remaining = -consume->getQuantity();
      // Initial onhand is available immediately (plan current / past).
      if (enforceMaterial && buffer) {
        const double freeOnhand =
            buffer->getOnHand() - allocatedOnhand[buffer];
        if (freeOnhand > ROUNDING_ERROR) {
          const double take = min(remaining, freeOnhand);
          if (allocate) allocatedOnhand[buffer] += take;
          remaining -= take;
        }
      }
      struct SupplyCandidate {
        FlowPlan* flowplan;
        OperationPlan* operationplan;
        Date available;
      };
      vector<SupplyCandidate> candidates;
      const auto& supply = supplyLookup;
      if (supply != supplyByBuffer.end()) {
        for (FlowPlan* produce : supply->second) {
          OperationPlan* producer = produce->getOperationPlan();
          if (!producer || producer == op || !producer->getOperation()) continue;
          if (producer->getOperation()->hasType<OperationInventory>() ||
              producer->getOrderType() == "STCK")
            continue;
          // In-scope makers must be FAS-stamped before they release material.
          // Outside-scope lots keep their current end (shared / already fixed).
          // allowUnscheduledEnds is a last-resort delay path: wait on the
          // maker's current end instead of unplanning the consumer.
          Date available = Date::infiniteFuture;
          if (isMovable(producer) && !scheduledEnd.count(producer)) {
            if (inScope.count(producer) && !allowUnscheduledEnds)
              available = Date::infiniteFuture;
            else
              available = materialAvailable(producer, op);
          } else
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
      const char* traceMaterial = getenv("FAS_TRACE_MATERIAL");
      const bool traceThisMaterial =
          traceMaterial && *traceMaterial && buffer && buffer->getItem() &&
          string(traceMaterial).find(buffer->getItem()->getName()) !=
              string::npos;
      for (const auto& candidate : candidates) {
        if (candidate.available == Date::infiniteFuture) continue;
        const double availableQuantity =
            candidate.flowplan->getQuantity() -
            allocatedSupply[candidate.flowplan];
        if (availableQuantity <= ROUNDING_ERROR) continue;
        const double allocated = min(remaining, availableQuantity);
        if (allocate) allocatedSupply[candidate.flowplan] += allocated;
        if (allocate && traceThisMaterial)
          logger << "FAS material allocation buffer='" << buffer->getName()
                 << "' consumer=" << op->getReference() << " producer="
                 << candidate.operationplan->getReference() << " qty="
                 << allocated << " available=" << candidate.available
                 << " producer_end=" << candidate.operationplan->getEnd()
                 << "\n";
        remaining -= allocated;
        materialDate = max(materialDate, candidate.available);
        if (remaining <= ROUNDING_ERROR) break;
      }
      // True external infinite buffers may start short. Everything we
      // enforce must cover the full requirement from onhand + placed batches.
      if (remaining > ROUNDING_ERROR && enforceMaterial) {
        if (traceThisMaterial) {
          logger << "FAS material unavailable buffer='" << buffer->getName()
                 << "' consumer=" << op->getReference()
                 << " required=" << -consume->getQuantity()
                 << " remaining=" << remaining
                 << " onhand=" << buffer->getOnHand()
                 << " allocated_onhand=" << allocatedOnhand[buffer]
                 << " producers=";
          bool first = true;
          for (const auto& candidate : candidates) {
            if (!first) logger << ',';
            logger << candidate.operationplan->getReference()
                   << "(qty=" << candidate.flowplan->getQuantity()
                   << ",allocated=" << allocatedSupply[candidate.flowplan]
                   << ",available=" << candidate.available
                   << ",scheduled="
                   << (scheduledEnd.count(candidate.operationplan) ? "yes" : "no")
                   << ')';
            first = false;
          }
          logger << '\n';
        }
        result = Date::infiniteFuture;
      } else if (materialDate != Date::infinitePast)
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
          const auto working = capacityWindows(resource, candidate,
                                               candidate->getStart(), candidate->getEnd());
          for (const auto& w : working) {
            points.push_back(w.getStart());
            points.push_back(w.getEnd());
          }
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
            if (!windowActive(working, check)) continue;
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
      if (top) {
        auto existing = demandByJob.find(top);
        if (existing == demandByJob.end() ||
            demand->getPriority() < existing->second->getPriority() ||
            (demand->getPriority() == existing->second->getPriority() &&
             demand->getDue() < existing->second->getDue()))
          demandByJob[top] = &*demand;
      }
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
  if (queued.size() != work.size() && getLogLevel() >= 1) {
    logger << "FAS: execution graph has " << (work.size() - queued.size())
           << " operationplans outside topological order\n";
    for (auto* op : work) {
      if (!op || queued.count(op)) continue;
      logger << "FAS: graph blocked ref=" << op->getReference()
             << " operation="
             << (op->getOperation() ? op->getOperation()->getName() : "")
             << " indegree=" << remainingIndegree[op] << " predecessors=";
      bool first = true;
      for (const auto& entry : graph.downstream)
        if (find(entry.second.begin(), entry.second.end(), op) !=
            entry.second.end()) {
          if (!first) logger << ',';
          logger << (entry.first ? entry.first->getReference() : "");
          first = false;
        }
      logger << '\n';
    }
  }
  for (auto* op : preferredOrder)
    if (queued.insert(op).second) placeOrder.push_back(op);
  unordered_set<OperationPlan*> dispatched;
  unordered_map<OperationPlan*, int> materialDeferCount;
  unordered_set<Demand*> failedDemands;
  unordered_set<OperationPlan*> failedRoots;
  unordered_map<string, size_t> failedReasons;
  const auto markFailed = [&](OperationPlan* op, const char* reason) {
    if (!op) return;
    ++failedReasons[reason];
    if (Demand* demand = demandOfJob(jobOf(op))) failedDemands.insert(demand);
    if (OperationPlan* root = op->getTopOwner()) failedRoots.insert(root);
  };
  // Index loop so material-deferred ops can be appended and retried after
  // their makers have been placed — prefer delay over unplanning.
  for (size_t placeIndex = 0; placeIndex < placeOrder.size(); ++placeIndex) {
    OperationPlan* op = placeOrder[placeIndex];
    if (!op || dispatched.count(op)) continue;
    if (Demand* demand = demandOfJob(jobOf(op));
        demand && failedDemands.count(demand))
      continue;
    if (OperationPlan* root = op->getTopOwner();
        root && failedRoots.count(root))
      continue;
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
        for (unsigned int attempt = 0; attempt < 10000; ++attempt) {
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
    // placing the operation. Snapshot so a failed capacity/material search
    // can release the claim for a later retry or another demand.
    const auto supplyBeforeAllocate = allocatedSupply;
    const auto onhandBeforeAllocate = allocatedOnhand;
    earliest = max(earliest, materialReady(op, true));
    auto releaseMaterialClaim = [&]() {
      allocatedSupply = supplyBeforeAllocate;
      allocatedOnhand = onhandBeforeAllocate;
    };
    if (earliest == Date::infiniteFuture) {
      releaseMaterialClaim();
      // Wait for in-scope makers that have not been stamped yet.
      bool waitingMaker = false;
      for (auto consume = op->beginFlowPlans(); consume != op->endFlowPlans();
           ++consume) {
        if (consume->getQuantity() >= -ROUNDING_ERROR || !consume->getBuffer())
          continue;
        const auto supply = supplyByBuffer.find(consume->getBuffer());
        if (supply == supplyByBuffer.end()) continue;
        for (FlowPlan* produce : supply->second) {
          OperationPlan* producer = produce->getOperationPlan();
          if (!producer || producer == op || !inScope.count(producer)) continue;
          if (isMovable(producer) && !scheduledEnd.count(producer)) {
            waitingMaker = true;
            break;
          }
        }
        if (waitingMaker) break;
      }
      // A shared-material producer can sit far behind this consumer in a
      // network containing thousands of executable leaves.  Eight retries
      // incorrectly classified that ordering delay as a material shortage.
      // Give every original work item a chance to be placed first.
      if (waitingMaker && materialDeferCount[op] < work.size()) {
        ++materialDeferCount[op];
        dispatched.erase(op);
        placeOrder.push_back(op);
        if (getLogLevel() >= 1) {
          Demand* demand = demandOfJob(jobOf(op));
          logger << "FAS: defer for material "
                 << (demand ? demand->getName() : op->getReference()) << " on "
                 << op->getOperation()->getName() << '\n';
        }
        continue;
      }
      // Delay onto current maker ends instead of unplanning the order.
      earliest = max(readyDate(op), materialReady(op, true, true));
      if (earliest == Date::infiniteFuture) {
        releaseMaterialClaim();
        // No producer batch exists at all for an enforced buffer: cannot
        // invent supply by delaying. Otherwise keep the expanded network.
        bool hasAnyMaker = false;
        for (auto consume = op->beginFlowPlans();
             consume != op->endFlowPlans(); ++consume) {
          if (consume->getQuantity() >= -ROUNDING_ERROR || !consume->getBuffer())
            continue;
          const auto supply = supplyByBuffer.find(consume->getBuffer());
          if (supply != supplyByBuffer.end() && !supply->second.empty()) {
            hasAnyMaker = true;
            break;
          }
        }
        if (!hasAnyMaker) {
          markFailed(op, "material-no-maker");
          if (getLogLevel() >= 0) {
            Demand* demand = demandOfJob(jobOf(op));
            logger << "FAS: material shortage blocks "
                   << (demand ? demand->getName() : op->getReference())
                   << " on " << op->getOperation()->getName() << '\n';
          }
        } else
          markFailed(op, "material-unavailable");
        continue;
      }
    }
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
      for (unsigned int attempt = 0; attempt < 10000; ++attempt) {
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
      if (!placed) {
        releaseMaterialClaim();
        markFailed(op, "bucket-capacity");
        continue;
      }
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
      for (unsigned int attempt = 0; attempt < 10000; ++attempt) {
        if (earliest >= horizonEnd) break;
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
          const auto working = capacityWindows(resource, op, slot.getStart(), slot.getEnd());
          for (const auto& w : working) {
            points.push_back(w.getStart());
            points.push_back(w.getEnd());
          }
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
            if (!windowActive(working, check)) continue;
            double used = 0.0;
            Date release = Date::infiniteFuture;
            for (const auto& reservation : reservations[resource])
              if (reservation.start <= check && reservation.end > check) {
                used += reservation.quantity;
                release = min(release, reservation.end);
              }
            const double capacity = calendar ? calendar->getValue(check)
                                             : resource->getMaximum();
            // The operation time calculation pauses work when its availability
            // calendar closes. A zero-capacity interval inside that pause is
            // not an overload: the remaining processing resumes next shift.
            if (capacity <= ROUNDING_ERROR &&
                op->getOperation()->getAvailable() &&
                op->getOperation()->getAvailable()->getValue(check) <=
                    ROUNDING_ERROR)
              continue;
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
        if (valid && slot.getEnd() <= horizonEnd) {
          newStart = slot.getStart();
          newEnd = slot.getEnd();
          break;
        }
        if (valid) break;
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
      if (newEnd == Date::infinitePast) {
        releaseMaterialClaim();
        markFailed(op, "continuous-capacity");
        continue;
      }
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
          for (const auto& w : capacityWindows(resource, op, reservedStart, reservedEnd))
            reservations[resource].push_back({w.getStart(), w.getEnd(),
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

  // A finite scheduling window is a selection boundary.  Orders for which
  // the forward pass finds no complete slot before that boundary are valid
  // unplanned orders, not a fatal solver error. Remove their complete pegged
  // supply networks after dispatch so no iterator is invalidated mid-pass.
  if (!failedDemands.empty() || !failedRoots.empty()) {
    unordered_set<OperationPlan*> seenRoots = failedRoots;
    for (const auto& entry : demandByJob)
      if (entry.second && failedDemands.count(entry.second)) {
        OperationPlan* root = entry.first ? entry.first->getTopOwner() : nullptr;
        if (root) seenRoots.insert(root);
      }
    // A failed demand can also reference a locked stock/confirmed plan.
    // Those plans are inputs to this run and can't be rolled back with the
    // proposed supply created by the expander.
    size_t removed = 0;
    for (auto* root : seenRoots)
      if (root && root->getProposed()) {
        activeCommands->add(new CommandDeleteOperationPlan(root));
        ++removed;
      }
    if (getLogLevel() >= 0)
      logger << "FAS: left " << failedDemands.size()
             << " orders unplanned (" << removed
             << " proposed supply networks removed)\n";
    if (getLogLevel() >= 0)
      for (const auto& reason : failedReasons)
        logger << "FAS: unplanned reason " << reason.first << '='
               << reason.second << '\n';
  }

  if (trial) {
    trialCompletion = finishOf(networkRoots);
    return;
  }

  // The forward pass is greedy: it walks the executable network once and never
  // revisits a decision, so an operation it placed behind a queue that did not
  // need to run first keeps that delay for the rest of the schedule.
  // Improve by relocating jobs in the decision sequence (same decoder, new
  // order) — matching Python fjsp_forward_schedule._improve_job_order.
  if (failedDemands.empty())
    improveJobOrder(networkRoots, safetyStockPlans, jobSequence);

  // Final material sweep. The greedy forward pass (and job-order improve)
  // can still leave a 自制 / finite buffer negative when a producer was
  // outside the dispatch key, a composite MRP end was trusted historically,
  // or capacity later stretched a producer past its consumer. Delay each
  // violating consumer to the first future receipt that restores a
  // non-negative balance; if no receipt exists, drop the proposed network.
  {
    const auto enforceBuffer = [](Buffer* buffer) {
      if (!buffer) return false;
      if (!buffer->hasType<BufferInfinite>()) return true;
      if (buffer->getItem() && buffer->getItem()->getCategory() == "自制")
        return true;
      for (const Flow& flow : buffer->getFlows())
        if (flow.getQuantity() > ROUNDING_ERROR && flow.getOperation() &&
            flow.getOperation()->hasType<OperationTimePer, OperationFixedTime>())
          return true;
      return false;
    };
    size_t repaired = 0;
    size_t removed = 0;
    const unsigned int maxPasses = 50000;
    for (unsigned int pass = 0; pass < maxPasses; ++pass) {
      bool changed = false;
      for (auto bufferIt = Buffer::begin(); bufferIt != Buffer::end();
           ++bufferIt) {
        Buffer* buffer = &*bufferIt;
        if (!enforceBuffer(buffer)) continue;
        for (auto flow = buffer->getFlowPlans().begin();
             flow != buffer->getFlowPlans().end(); ++flow) {
          if (flow->getEventType() != 1) continue;
          auto* fp = static_cast<FlowPlan*>(&*flow);
          if (fp->getQuantity() >= -ROUNDING_ERROR) continue;
          if (fp->getOnhand() >= -ROUNDING_ERROR) continue;
          OperationPlan* consumer = fp->getOperationPlan();
          if (!consumer || !isMovable(consumer)) continue;
          // Start from the balance *after* this consumption; walk forward
          // until later receipts restore a non-negative onhand.
          double available = fp->getOnhand();
          Date candidate = Date::infiniteFuture;
          for (auto future = flow; future != buffer->getFlowPlans().end();
               ++future) {
            if (future->getDate() <= fp->getDate() ||
                fabs(future->getQuantity()) <= ROUNDING_ERROR)
              continue;
            available += future->getQuantity();
            if (available >= -ROUNDING_ERROR) {
              candidate = future->getDate();
              break;
            }
          }
          if (candidate == Date::infiniteFuture) {
            // Timing cannot be repaired and inventing supply is out of scope.
            // Keep the expanded network so the order stays planned; the
            // material problem remains visible on the buffer timeline.
            continue;
          }
          if (candidate <= consumer->getStart()) continue;
          const Duration dur = consumer->getEnd() - consumer->getStart();
          const DateRange mapped =
              consumer->getOperation()->calculateOperationTime(
                  consumer, candidate, dur, true);
          if (mapped.getStart() <= consumer->getStart()) continue;
          if (getLogLevel() >= 0)
            logger << "FAS: material repair delay consumer="
                   << consumer->getReference() << " op="
                   << consumer->getOperation()->getName() << " buffer='"
                   << buffer->getName() << "' from " << consumer->getStart()
                   << " to " << mapped.getStart() << " (onhand was "
                   << fp->getOnhand() << ")\n";
          activeCommands->add(new CommandMoveOperationPlan(
              consumer, mapped.getStart(), mapped.getEnd(), -1.0, false, true,
              false));
          // Material events must reflect the new start before the next
          // consumer on this buffer is inspected.
          activeCommands->commit();
          ++repaired;
          changed = true;
          break;
        }
        if (changed) break;
      }
      if (!changed) break;
      if (pass + 1 == maxPasses && getLogLevel() >= 0)
        logger << "Warning: FAS material repair reached its move limit\n";
    }
    if (getLogLevel() >= 0 && (repaired || removed))
      logger << "FAS: material repair delayed " << repaired
             << " consumers, removed " << removed
             << " uncovered proposed networks\n";
  }

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
  Calendar* calendar = resource->getMaximumCalendar();
  const double maximum = resource->getMaximum();
  if (resource->getCategory() == "人员") {
    vector<pair<Date, double>> events;
    vector<Date> points{start, end};
    for (const auto& event : resource->getLoadPlans()) {
      const auto* plan = event.getOperationPlan();
      if (event.getEventType() != 1 || event.getQuantity() >= 0 || !plan ||
          own.count(plan)) continue;
      for (const auto& w : capacityWindows(resource, plan, plan->getStart(), plan->getEnd())) {
        events.emplace_back(w.getStart(), -event.getQuantity());
        events.emplace_back(w.getEnd(), event.getQuantity());
        if (w.getStart() > start && w.getStart() < end) points.push_back(w.getStart());
        if (w.getEnd() > start && w.getEnd() < end) points.push_back(w.getEnd());
      }
    }
    if (calendar) {
      Calendar::EventIterator change(calendar, start, true);
      Date previous = change.getDate();
      for (unsigned int n = 0; n < 10000 && change.getDate() < end; ++n) {
        if (change.getDate() > start) points.push_back(change.getDate());
        ++change;
        if (change.getDate() <= previous) break;
        previous = change.getDate();
      }
    }
    sort(events.begin(), events.end());
    sort(points.begin(), points.end());
    points.erase(unique(points.begin(), points.end()), points.end());
    size_t index = 0;
    double used = 0;
    for (size_t p = 0; p + 1 < points.size(); ++p) {
      while (index < events.size() && events[index].first <= points[p])
        used += events[index++].second;
      const double capacity = calendar ? calendar->getValue(points[p]) : maximum;
      if (capacity <= ROUNDING_ERROR || used + required <= capacity + ROUNDING_ERROR)
        continue;
      for (size_t e = index; e < events.size(); ++e)
        if (events[e].second < -ROUNDING_ERROR) return events[e].first;
      return Date::infiniteFuture;
    }
    return Date::infiniteFuture;
  }
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
  bool pooledPersonnel = false;
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
      // Personnel pools run many jobs concurrently. Summing their durations
      // as a serial queue hides the queue difference between eligible machines.
      // The conflict probe below still enforces their actual shift capacity.
      if (resource->getCategory() == "人员") {
        pooledPersonnel = true;
        continue;
      }
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
  // For personnel-backed routes, eligibility counts aren't occupied capacity:
  // a large structural penalty can prefer a busy machine over an idle one.
  return firstCompletion +
         Duration((kFASQueueWeight - 1L) * firstDuration.getSeconds()) +
         (pooledPersonnel ? Duration(0L) : contention);
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

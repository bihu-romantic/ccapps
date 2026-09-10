/***************************************************************************
 * Deterministic forward finite-capacity supply scheduler.
 ***************************************************************************/

#include <algorithm>
#include <deque>
#include <unordered_map>
#include <unordered_set>

#include "ccAPPS/solverfas.h"

namespace ccAPPS {

const MetaClass* SolverFAS::metadata;

namespace {

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
  fields->addDurationField<SolverFAS>(Keyword("lazydelay"),
                                      &SolverFAS::getLazyDelay,
                                      &SolverFAS::setLazyDelay);
  fields->addDurationField<SolverFAS>(Keyword("minimumdelay"),
                                      &SolverFAS::getMinimumDelay,
                                      &SolverFAS::setMinimumDelay);
  fields->addDurationField<SolverFAS>(Keyword("administrativeleadtime"),
                                      &SolverFAS::getAdministrativeLeadTime,
                                      &SolverFAS::setAdministrativeLeadTime);
  fields->addBoolField<SolverFAS>(Keyword("rotateresources"),
                                  &SolverFAS::getRotateResources,
                                  &SolverFAS::setRotateResources);
  fields->addUnsignedLongField<SolverFAS>(Keyword("iterationmax"),
                                          &SolverFAS::getIterationMax,
                                          &SolverFAS::setIterationMax);
  fields->addUnsignedLongField<SolverFAS>(Keyword("resourceiterationmax"),
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
  supplyExpander.setLogLevel(getLogLevel());
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

bool SolverFAS::demandFirst(const OperationPlan* left,
                            const OperationPlan* right) {
  Demand* leftDemand = left ? left->getTopOwner()->getDemand() : nullptr;
  Demand* rightDemand = right ? right->getTopOwner()->getDemand() : nullptr;
  if (leftDemand != rightDemand) {
    if (!leftDemand) return false;
    if (!rightDemand) return true;
    if (leftDemand->getPriority() != rightDemand->getPriority())
      return leftDemand->getPriority() < rightDemand->getPriority();
    if (leftDemand->getDue() != rightDemand->getDue())
      return leftDemand->getDue() < rightDemand->getDue();
    if (leftDemand->getName() != rightDemand->getName())
      return leftDemand->getName() < rightDemand->getName();
  }
  if (left->getStart() != right->getStart())
    return left->getStart() < right->getStart();
  return left->getReference() < right->getReference();
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

vector<OperationPlan*> SolverFAS::orderNetwork(
    const unordered_set<OperationPlan*>& network,
    const unordered_set<OperationPlan*>& fixedPlans) const {
  vector<OperationPlan*> work;
  for (auto* op : network) {
    if (!isMovable(op) || fixedPlans.count(op)) continue;
    if (op->getSubOperationPlans() != OperationPlan::end()) continue;
    work.push_back(op);
  }
  unordered_set<OperationPlan*> inScope(work.begin(), work.end());
  unordered_map<OperationPlan*, vector<OperationPlan*>> downstream;
  unordered_map<OperationPlan*, size_t> indegree;
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
      for (PeggingIterator peg(&*consume, false); peg; --peg) {
        OperationPlan* producer = peg.getOperationPlan();
        if (producer && producer != consumer && inScope.count(producer) &&
            seen.insert(producer).second)
          addEdge(producer, consumer);
      }
    }
    for (PeggingIterator peg(consumer, false, 1); peg; --peg) {
      OperationPlan* producer = peg.getOperationPlan();
      if (producer && producer != consumer && inScope.count(producer) &&
          seen.insert(producer).second)
        addEdge(producer, consumer);
    }
    // MRP's pegging iterator can omit the leaf link for a generated
    // distribution order. Complete that relation directly from the material
    // buffers, so a WIP DO is sequenced after its producing MO rather than
    // being treated as an external source supply.
    for (auto consume = consumer->beginFlowPlans();
         consume != consumer->endFlowPlans(); ++consume) {
      if (consume->getQuantity() >= -ROUNDING_ERROR || !consume->getBuffer())
        continue;
      for (auto candidate : work) {
        if (candidate == consumer || seen.count(candidate)) continue;
        for (auto produce = candidate->beginFlowPlans();
             produce != candidate->endFlowPlans(); ++produce)
          if (produce->getQuantity() > ROUNDING_ERROR &&
              produce->getBuffer() == consume->getBuffer()) {
            seen.insert(candidate);
            addEdge(candidate, consumer);
            break;
          }
      }
    }
    auto dependency = consumer->getBlockedbyIterator();
    while (auto* link = dependency.next()) {
      OperationPlan* producer = link->getFirst();
      if (producer && inScope.count(producer)) addEdge(producer, consumer);
    }
  }
  for (auto* producer : work) {
    OperationPlan* consumer = producer->getNextSubOpplan();
    if (consumer && inScope.count(consumer)) addEdge(producer, consumer);
  }

  vector<OperationPlan*> ready;
  // Keep entry stations continuously supplied.  Their successors still use
  // their actual material-ready timestamp below, so this release ordering
  // doesn't make a downstream batch wait for all source batches.
  unordered_set<OperationPlan*> sourceOperations;
  for (auto* op : work)
    if (!indegree[op]) {
      ready.push_back(op);
      sourceOperations.insert(op);
    }
  // Pick one deterministic ingress stream.  Giving *every* source operation
  // priority can reserve all shared labour with unrelated entry stations
  // before the first downstream batch is considered.  That produces an
  // artificial hole in a flow line even though its immediate predecessor has
  // completed.  Keep the selected stream continuously fed, but let released
  // downstream work run before independent source streams.
  stable_sort(ready.begin(), ready.end(), demandFirst);
  const Resource* ingressResource = nullptr;
  if (!ready.empty())
    for (auto load = ready.front()->beginLoadPlans();
         load != ready.front()->endLoadPlans(); ++load)
      if (load->getQuantity() < -ROUNDING_ERROR && load->getResource() &&
          load->getResource()->getConstrained() &&
          !load->getResource()->isGroup()) {
        ingressResource = load->getResource();
        break;
      }
  const auto isIngressSource = [&sourceOperations, ingressResource](
                                   const OperationPlan* candidate) {
    if (!ingressResource ||
        !sourceOperations.count(const_cast<OperationPlan*>(candidate)))
      return false;
    for (auto load = candidate->beginLoadPlans();
         load != candidate->endLoadPlans(); ++load)
      if (load->getQuantity() < -ROUNDING_ERROR &&
          load->getResource() == ingressResource)
        return true;
    return false;
  };
  const auto releaseFirst = [&isIngressSource, &sourceOperations](
                                const OperationPlan* left,
                                const OperationPlan* right) {
    const bool leftIngress = isIngressSource(left);
    const bool rightIngress = isIngressSource(right);
    if (leftIngress != rightIngress) return leftIngress;
    const bool leftSource = sourceOperations.count(
                                const_cast<OperationPlan*>(left)) > 0;
    const bool rightSource = sourceOperations.count(
                                 const_cast<OperationPlan*>(right)) > 0;
    if (leftSource != rightSource) return !leftSource;
    return SolverFAS::demandFirst(left, right);
  };
  stable_sort(ready.begin(), ready.end(), releaseFirst);
  vector<OperationPlan*> ordered;
  // Allow the ingress station to look one batch ahead.  This is enough to
  // reserve its next continuous slot, while preventing a long source queue
  // from consuming all shared capacity before the first released downstream
  // batch gets a chance to reserve its own slot.
  constexpr unsigned short ingressLookahead = 2;
  unsigned short consecutiveIngress = 0;
  while (!ready.empty()) {
    if (consecutiveIngress >= ingressLookahead) {
      auto released = find_if(ready.begin(), ready.end(),
          [&sourceOperations](OperationPlan* candidate) {
            return !sourceOperations.count(candidate);
          });
      if (released != ready.end())
        rotate(ready.begin(), released, released + 1);
    }
    OperationPlan* op = ready.front();
    ready.erase(ready.begin());
    ordered.push_back(op);
    if (isIngressSource(op))
      ++consecutiveIngress;
    else if (!sourceOperations.count(op))
      consecutiveIngress = 0;
    for (auto* child : downstream[op])
      if (--indegree[child] == 0) {
        ready.push_back(child);
        stable_sort(ready.begin(), ready.end(), releaseFirst);
      }
  }
  if (ordered.size() != work.size()) {
    unordered_set<OperationPlan*> scheduled(ordered.begin(), ordered.end());
    vector<OperationPlan*> remainder;
    for (auto* op : work)
      if (!scheduled.count(op)) remainder.push_back(op);
    stable_sort(remainder.begin(), remainder.end(), demandFirst);
    ordered.insert(ordered.end(), remainder.begin(), remainder.end());
  }
  return ordered;
}

void SolverFAS::reserveFixedNetwork(
    const unordered_set<OperationPlan*>& inScope,
    unordered_map<const Resource*, vector<Reservation>>& reservations,
    unordered_map<const Resource*, Date>& serialReady) const {
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
      if (resource->getMaximum() <= 1.0 + ROUNDING_ERROR)
        serialReady[resource] = max(serialReady[resource], op->getEnd());
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
  activeCommands = trialTransaction ? trialTransaction : &commands;
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
  supplyExpander.setLogLevel(getLogLevel());
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
  if (!trial && getLogLevel() >= 0) {
    logger << "FAS: expanding " << demandsToExpand.size() << " orders in "
           << demandsByCluster.size() << " supply clusters\n";
  }
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
  const vector<OperationPlan*> work =
      orderNetwork(selectedNetwork, safetyStockPlans);
  if (work.empty()) {
    if (trial) trialCompletion = finishOf(networkRoots);
    return;
  }
  const unordered_set<OperationPlan*> inScope(work.begin(), work.end());
  const vector<OperationPlan*> ordered = work;
  bool hasUnscheduledBucketSupply = false;

  unordered_map<const Resource*, vector<Reservation>> reservations;
  unordered_map<const Resource*, Date> serialReady;
  reserveFixedNetwork(inScope, reservations, serialReady);
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
  const auto materialAvailable =
      [&scheduledStart, &scheduledEnd](OperationPlan* producer,
                                       OperationPlan* consumer) {
        if (!producer || !consumer) return Date::infiniteFuture;
        const auto end = scheduledEnd.find(producer);
        const Date producerEnd = end != scheduledEnd.end()
                                     ? end->second
                                     : producer->getEnd();
        const auto start = scheduledStart.find(producer);
        const Date producerStart = start != scheduledStart.end()
                                       ? start->second
                                       : producer->getStart();
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
              Date calendarRetry = Date::infiniteFuture;
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
              retry = min(retry,
                          min(release, max(points[p + 1], calendarRetry)));
              return false;
            }
          }
        }
        return true;
      };
  for (auto* op : ordered) {
    // Choose pooled resources as one combination.  Greedily picking the first
    // available operator and then the first available tool can select the
    // same shared member twice, or leave a later common slot unused.  FAS
    // keeps MRP's load definitions and eligibility rules, but searches the
    // bounded Cartesian product of qualified concrete members to minimize the
    // common ready date.  The capacity-calendar search below remains the
    // authoritative full-interval feasibility check.
    struct PoolChoices {
      LoadPlan* load;
      vector<pair<Resource*, Date>> members;
    };
    vector<PoolChoices> pooledLoads;
    for (auto load = op->beginLoadPlans(); load != op->endLoadPlans(); ++load) {
      Load* definition = load->getLoad();
      if (!definition || load->getQuantity() >= -ROUNDING_ERROR ||
          !definition->getResource()->isGroup())
        continue;
      PoolChoices choices{&*load, {}};
      // Estimate the first interval in which this member can host the whole
      // operation. Looking only at serialReady misses members with a
      // parallel maximum, capacity-calendar gaps, or reservations already in
      // the FAS reservation stream.
      Duration opDuration;
      op->getOperation()->calculateOperationTime(op, op->getStart(), op->getEnd(),
                                                 &opDuration, true);
      // The pool load defines the required capacity.  Don't derive it from
      // the member currently selected by MRP: a different member needn't
      // have a matching loadplan, and falling back to 1 would make the FAS
      // trial incorrectly accept an undersized member.
      const double required = fabs(load->getQuantity());
      const auto memberReady = [&](Resource* memberResource) {
        Date ready = max(current, serialReady[memberResource]);
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
             !candidate->hasSkill(definition->getSkill(), op->getStart(),
                                  op->getEnd())))
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
      sort(choices.members.begin(), choices.members.end(),
           [](const auto& left, const auto& right) {
             return left.second != right.second ? left.second < right.second
                                                : left.first->getName() < right.first->getName();
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
      const auto choose = [&](auto&& self, size_t depth, Date ready) -> void {
        if (++nodes > nodeBudget) return;
        if (bestReady != Date::infiniteFuture && ready > bestReady) return;
        if (depth == searchOrder.size()) {
          if (ready < bestReady ||
              (ready == bestReady &&
               lexicographical_compare(selected.begin(), selected.end(),
                 best.begin(), best.end(), [](Resource* left, Resource* right) {
                   return left->getName() < right->getName();
                 }))) {
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
          if (bestReady != Date::infiniteFuture && candidate.second > bestReady)
            break;
          selected[index] = candidate.first;
          used.insert(candidate.first);
          self(self, depth + 1, max(ready, candidate.second));
          used.erase(candidate.first);
          if (nodes > nodeBudget) return;
        }
      };
      choose(choose, 0, current);
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
    Date earliest = isLeadTimeConstrained(op->getOperation())
                        ? current
                        : op->getStart();
    for (auto load = op->beginLoadPlans(); load != op->endLoadPlans(); ++load) {
      const Resource* resource = load->getResource();
      if (isCapacityConstrained() && resource && resource->getConstrained() &&
          !resource->isGroup() &&
          resource->getMaximum() <= 1.0 + ROUNDING_ERROR) {
        earliest = max(earliest, serialReady[resource]);
      }
    }
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
      for (auto candidate = OperationPlan::begin();
           candidate != OperationPlan::end(); ++candidate) {
        OperationPlan* producer = &*candidate;
        if (producer == op || !producer->getOperation()) continue;
        for (auto produce = producer->beginFlowPlans();
             produce != producer->endFlowPlans(); ++produce) {
          if (produce->getQuantity() <= ROUNDING_ERROR ||
              produce->getBuffer() != consume->getBuffer())
            continue;
          // A predecessor can be encountered later in the static dispatch
          // order.  It still represents finite supply: use its existing
          // model completion as a conservative boundary until its FAS date is
          // available.  Treating it as absent incorrectly releases a consumer
          // at the plan current date (as seen on CCPL OP-12).
          const Date available = isMovable(producer) && !scheduledEnd.count(producer)
                                     ? producer->getEnd()
                                     : materialAvailable(producer, op);
          candidates.push_back({&*produce, producer, available});
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
      for (const auto& candidate : candidates) {
        const double availableQuantity =
            candidate.flowplan->getQuantity() -
            allocatedSupply[candidate.flowplan];
        if (availableQuantity <= ROUNDING_ERROR) continue;
        const double allocated = min(remaining, availableQuantity);
        allocatedSupply[candidate.flowplan] += allocated;
        remaining -= allocated;
        materialDate = max(materialDate, candidate.available);
        if (remaining <= ROUNDING_ERROR) break;
      }
      // Leave an unpegged/infinite-buffer input to the standard model.  A
      // finite input with known FAS supply must not start before its allocated
      // batch has completed.
      if (materialDate != Date::infinitePast)
        earliest = max(earliest, materialDate);
    }
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
      earliest = max(earliest, producerEnd + safety);
    }
    Date newStart = Date::infinitePast;
    Date newEnd = Date::infinitePast;
    if (op->getOperation()->hasType<OperationDelivery>()) {
      // A delivery is instantaneous.  Its commitment is constrained by both
      // the final pegged receipt and the requested due date: supply can be
      // available early, but that must not be written as an earlier promise.
      const Date commitment = max(earliest, op->getEnd());
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
        // a WIP DO cannot precede its producing MO.
        if (op->getOrderType() == "PO" || op->getOrderType() == "DO") {
          newStart = earliest;
          newEnd = earliest;
        } else
          continue;
      }
      if (duration <= Duration(0L)) {
        // The instantaneous external receipt has been placed above.
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
      if (isCapacityConstrained() && resource && resource->getConstrained() &&
          !resource->isGroup() &&
          resource->getMaximum() <= 1.0 + ROUNDING_ERROR)
        serialReady[resource] = max(serialReady[resource], reservedEnd);
    }
    scheduledEnd[op] = reservedEnd;
    scheduledStart[op] = reservedStart;
  }
  if (!trial && getLogLevel() >= 0)
    logger << "FAS: selected " << demands.size() << " orders (expanded "
           << demandsToExpand.size() << "), forward-scheduled "
           << moved << " of " << work.size() << " supply operationplans\n";

  if (trial) {
    trialCompletion = finishOf(networkRoots);
    return;
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
                                CommandManager* transaction) {
  return static_cast<SolverFAS*>(context)->trialForwardNetwork(root,
                                                               transaction);
}

}  // namespace ccAPPS

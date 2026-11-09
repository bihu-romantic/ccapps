/***************************************************************************
 * ALNS + CP-SAT hybrid for large FJSP (lot structure + schedule).
 *
 * Formal defaults (override via env):
 *   - CP_ALNS_SPLITS_PER_MOVE=3     at most 3 orders split per round
 *   - CP_ALNS_SPLIT_PROB=0.65       split operator pick probability
 *   - Split only if SNP / economic-batch / long-hours eligible
 *   - The ≤3 orders must share one workshop (no cross-workshop splits);
 *     repair is PARTITION_ONLY for that workshop
 *   - Keep only on global score improve; else roll back ALL splits
 *
 * Enable: CP_ALNS=1  (commands.py).  CP workers stay partitioned.
 ***************************************************************************/

#include "ccAPPS/solvercp.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdlib>
#include <random>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace ccAPPS {
namespace {

constexpr double kEps = 1e-6;

int envInt(const char* name, int fallback) {
  const char* value = getenv(name);
  if (!value || !*value) return fallback;
  return atoi(value);
}

double envDouble(const char* name, double fallback) {
  const char* value = getenv(name);
  if (!value || !*value) return fallback;
  try {
    return stod(value);
  } catch (...) {
    return fallback;
  }
}

string envString(const char* name, const char* fallback) {
  const char* value = getenv(name);
  if (!value || !*value) return fallback;
  return value;
}

void setEnv(const char* name, const string& value) {
  setenv(name, value.c_str(), 1);
}

OperationPlan* topRoot(OperationPlan* plan) {
  while (plan && plan->getOwner()) plan = plan->getOwner();
  return plan;
}

Demand* demandOf(OperationPlan* plan) {
  for (OperationPlan* node = plan; node; node = node->getOwner())
    if (node->getDemand()) return node->getDemand();
  return nullptr;
}

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

void collectWorkshops(OperationPlan* root, unordered_set<int>& out) {
  if (!root) return;
  vector<OperationPlan*> stack{root};
  while (!stack.empty()) {
    OperationPlan* node = stack.back();
    stack.pop_back();
    const int ws = workshopOf(node);
    if (ws > 0) out.insert(ws);
    for (OperationPlan::iterator child(node); child != node->end(); ++child)
      stack.push_back(&*child);
  }
}

bool touchesWorkshop(OperationPlan* root, int workshop) {
  if (!root || workshop <= 0) return false;
  vector<OperationPlan*> stack{root};
  while (!stack.empty()) {
    OperationPlan* node = stack.back();
    stack.pop_back();
    if (workshopOf(node) == workshop) return true;
    for (OperationPlan::iterator child(node); child != node->end(); ++child)
      stack.push_back(&*child);
  }
  return false;
}

void collectResources(OperationPlan* root, unordered_set<Resource*>& out) {
  if (!root) return;
  vector<OperationPlan*> stack{root};
  while (!stack.empty()) {
    OperationPlan* node = stack.back();
    stack.pop_back();
    for (auto load = node->beginLoadPlans(); load != node->endLoadPlans(); ++load)
      if (load->isStart() && load->getResource()) out.insert(load->getResource());
    for (OperationPlan::iterator child(node); child != node->end(); ++child)
      stack.push_back(&*child);
  }
}

Item* itemOf(OperationPlan* root) {
  if (!root) return nullptr;
  for (auto flow = root->beginFlowPlans(); flow != root->endFlowPlans(); ++flow)
    if (flow->getBuffer() && flow->getBuffer()->getItem() &&
        flow->getQuantity() > kEps)
      return flow->getBuffer()->getItem();
  for (OperationPlan::iterator child(root); child != root->end(); ++child) {
    Item* item = itemOf(&*child);
    if (item) return item;
  }
  return nullptr;
}

bool isExecutableLeaf(OperationPlan* plan) {
  if (!plan || !plan->getOperation() || plan->getQuantity() <= kEps) return false;
  Operation* op = plan->getOperation();
  if (!op->hasType<OperationTimePer, OperationFixedTime>()) return false;
  if (op->hasType<OperationItemSupplier, OperationItemDistribution, OperationSplit>() ||
      op->hasType<OperationRouting, OperationAlternate>())
    return false;
  return true;
}

bool isMovableRoot(OperationPlan* plan) {
  if (!plan || plan->getOwner()) return false;
  if (plan->getConfirmed() || plan->getCompleted() || plan->getClosed())
    return false;
  if (!plan->getProposed() && !plan->getApproved()) return false;
  if (!plan->getOperation()) return false;
  if (plan->getOperation()->hasType<OperationItemSupplier,
                                    OperationItemDistribution>())
    return false;
  if (plan->getStringProperty("alns_role", "") == "whole_parked") return false;
  return plan->getQuantity() > kEps;
}

long durationSeconds(OperationPlan* plan) {
  if (!plan || !plan->getOperation()) return 0;
  if (plan->getEnd() != Date::infinitePast &&
      plan->getStart() != Date::infinitePast && plan->getEnd() > plan->getStart())
    return static_cast<long>((plan->getEnd() - plan->getStart()).getSeconds());
  Operation* operation = plan->getOperation();
  if (operation->hasType<OperationTimePer>()) {
    auto* timed = static_cast<OperationTimePer*>(operation);
    double seconds = timed->getDuration().getSeconds() +
                     timed->getDurationPer() * plan->getQuantity();
    if (seconds <= 0.0) return 0;
    return max(1L, static_cast<long>(seconds));
  }
  if (operation->hasType<OperationFixedTime>()) {
    auto* timed = static_cast<OperationFixedTime*>(operation);
    return max(0L, static_cast<long>(timed->getDuration().getSeconds()));
  }
  return 0;
}

long processingSeconds(OperationPlan* root) {
  if (!root) return 0;
  long total = 0;
  bool any = false;
  vector<OperationPlan*> stack{root};
  while (!stack.empty()) {
    OperationPlan* node = stack.back();
    stack.pop_back();
    if (isExecutableLeaf(node)) {
      total += durationSeconds(node);
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

double cappedSplitShipment(double remaining, double snp, double economic,
                           int maxParts) {
  if (remaining <= kEps) return max(1.0, snp);
  maxParts = max(1, maxParts);
  snp = max(1.0, snp);
  const double unit = economic > 1.0 ? economic : snp;
  if (maxParts <= 1 || remaining <= unit + kEps) return remaining;
  const double floorChunk = ceil(remaining / maxParts - 1e-9);
  double chunk = ceil(max(floorChunk, unit) / unit - 1e-9) * unit;
  chunk = min(chunk, remaining);
  const double tail = remaining - chunk;
  if (tail > kEps && tail < snp - kEps) return remaining;
  return chunk;
}

void forceLotQuantity(OperationPlan* plan, double qty) {
  if (!plan || !plan->getOperation() || qty <= kEps) return;
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
  if (target->getSubOperationPlans(false) != OperationPlan::end()) return;
  for (OperationPlan::iterator child(source); child != source->end(); ++child) {
    Operation* operation = child->getOperation();
    if (!operation) continue;
    const double qty = max(kEps, child->getQuantity() * ratio);
    OperationPlan* copy = operation->createOperationPlan(
        qty, child->getStart(), child->getEnd(), target->getBatch(),
        target->getDemand(), target, true, false);
    if (!copy) continue;
    if (fabs(copy->getQuantity() - qty) > kEps) forceLotQuantity(copy, qty);
    cloneSplitChildren(&*child, copy, ratio);
  }
}

void tryDeleteOpPlan(OperationPlan* plan) {
  if (!plan) return;
  try {
    CommandDeleteOperationPlan remove(plan);
    remove.commit();
  } catch (const DataException&) {
  } catch (...) {
  }
}

struct Score {
  size_t missing = 0;
  long tardiness = 0;
  double utilPct = 0.0;   // early-window 人员 load/available * 100
  double loadH = 0.0;
  double availH = 0.0;
  long makespan = 0;
  size_t lots = 0;
  size_t splits = 0;

  // Primary: missing → tardiness ↓ → util ↑. Makespan is tie-break only.
  double scalar() const {
    return static_cast<double>(missing) * 1.0e15 +
           static_cast<double>(tardiness) * 1000.0 -
           utilPct * 1.0e4 +
           static_cast<double>(makespan) * 0.001;
  }

  bool betterThan(const Score& other) const {
    if (missing != other.missing) return missing < other.missing;
    if (tardiness != other.tardiness) return tardiness < other.tardiness;
    if (fabs(utilPct - other.utilPct) > 0.05)
      return utilPct > other.utilPct;
    if (makespan != other.makespan) return makespan < other.makespan;
    return splits < other.splits;
  }
};

void accumulatePersonnelWindow(const Date& origin, const Date& windowEnd,
                               double& loadH, double& availH) {
  loadH = 0.0;
  availH = 0.0;
  // Available person-hours from 人员 calendars in [origin, windowEnd).
  for (auto res = Resource::begin(); res != Resource::end(); ++res) {
    if (res->getCategory() != "人员") continue;
    Calendar* cal = res->getAvailable();
    if (!cal) continue;
    Date cursor = origin;
    int guard = 0;
    while (cursor < windowEnd && ++guard < 100000) {
      CalendarBucket* bucket = cal->findBucket(cursor, true);
      if (!bucket) break;
      Date bucketEnd = bucket->getEnd();
      if (bucketEnd == Date::infinitePast || bucketEnd <= cursor)
        bucketEnd = cursor + Duration(3600L);
      Date segEnd = min(windowEnd, bucketEnd);
      if (segEnd <= cursor) break;
      const double value = bucket->getValue();
      if (value > kEps)
        availH += static_cast<double>((segEnd - cursor).getSeconds()) *
                  value / 3600.0;
      cursor = segEnd;
    }
  }
  // Load person-hours from manufacturing loads overlapping the window.
  for (auto plan = OperationPlan::begin(); plan != OperationPlan::end();
       ++plan) {
    if (plan->getQuantity() <= kEps) continue;
    if (plan->getStringProperty("alns_role", "") == "whole_parked") continue;
    if (plan->getStart() == Date::infinitePast ||
        plan->getEnd() == Date::infinitePast)
      continue;
    if (plan->getEnd() <= origin || plan->getStart() >= windowEnd) continue;
    for (auto load = plan->beginLoadPlans(); load != plan->endLoadPlans();
         ++load) {
      if (!load->isStart()) continue;
      Resource* resource = load->getResource();
      if (!resource || resource->getCategory() != "人员") continue;
      Date lo = max(origin, plan->getStart());
      Date hi = min(windowEnd, plan->getEnd());
      if (hi <= lo) continue;
      const double qty = fabs(load->getQuantity());
      loadH += static_cast<double>((hi - lo).getSeconds()) *
               max(1.0, qty) / 3600.0;
    }
  }
}

Score evaluatePlan(const Date& origin, int horizonDays) {
  Score score;
  const Date windowEnd =
      origin + Duration(static_cast<long>(max(1, horizonDays)) * 86400L);
  unordered_map<Demand*, Date> lastEnd;
  for (auto plan = OperationPlan::begin(); plan != OperationPlan::end();
       ++plan) {
    if (plan->getQuantity() <= kEps) continue;
    if (plan->getOwner()) continue;
    if (plan->getStringProperty("alns_role", "") == "whole_parked") continue;
    ++score.lots;
    const string role = plan->getStringProperty("alns_role", "");
    if (role == "head" || role == "tail") ++score.splits;
    if (plan->getEnd() != Date::infinitePast &&
        plan->getEnd() != Date::infiniteFuture && plan->getEnd() > origin &&
        plan->getStart() < windowEnd)
      score.makespan =
          max(score.makespan,
              static_cast<long>((plan->getEnd() - origin).getSeconds()));
    Demand* demand = demandOf(&*plan);
    if (!demand) continue;
    Date& slot = lastEnd[demand];
    if (plan->getEnd() > slot) slot = plan->getEnd();
  }
  for (const auto& entry : lastEnd) {
    Demand* demand = entry.first;
    if (!demand) continue;
    const Date due = demand->getDue();
    if (due == Date::infinitePast || due == Date::infiniteFuture) continue;
    if (entry.second > due)
      score.tardiness +=
          static_cast<long>((entry.second - due).getSeconds());
  }
  // A plan can only claim zero tardiness when every open demand in the
  // three-day evaluation window still has a delivery-bearing plan.
  for (auto demand = Demand::begin(); demand != Demand::end(); ++demand) {
    if (!(demand->getStatus() & Demand::STATUS_OPEN) ||
        demand->getQuantity() <= kEps)
      continue;
    const Date due = demand->getDue();
    if (due == Date::infinitePast || due == Date::infiniteFuture ||
        due > windowEnd)
      continue;
    auto found = lastEnd.find(&*demand);
    if (found == lastEnd.end() || found->second == Date::infinitePast)
      ++score.missing;
  }
  accumulatePersonnelWindow(origin, windowEnd, score.loadH, score.availH);
  if (score.availH > kEps)
    score.utilPct = 100.0 * score.loadH / score.availH;
  return score;
}

struct SnapshotNode {
  OperationPlan* plan = nullptr;
  Date start;
  Date end;
  double quantity = 0.0;
  string status;
};

vector<SnapshotNode> snapshotRoots(
    const unordered_set<OperationPlan*>& roots) {
  vector<SnapshotNode> out;
  out.reserve(roots.size() * 4);
  for (OperationPlan* root : roots) {
    if (!root) continue;
    vector<OperationPlan*> stack{root};
    while (!stack.empty()) {
      OperationPlan* node = stack.back();
      stack.pop_back();
      out.push_back({node, node->getStart(), node->getEnd(), node->getQuantity(),
                     node->getStatus()});
      for (OperationPlan::iterator child(node); child != node->end(); ++child)
        stack.push_back(&*child);
    }
  }
  return out;
}

void restoreSnapshot(const vector<SnapshotNode>& snap) {
  for (const SnapshotNode& node : snap) {
    if (!node.plan) continue;
    try {
      if (fabs(node.plan->getQuantity() - node.quantity) > kEps)
        forceLotQuantity(node.plan, node.quantity);
      if (node.start != Date::infinitePast && node.end != Date::infinitePast)
        node.plan->setStartAndEnd(node.start, node.end);
      if (!node.status.empty()) node.plan->setStatus(node.status);
    } catch (...) {
    }
  }
}

struct SplitPair {
  OperationPlan* whole = nullptr;
  OperationPlan* head = nullptr;
  OperationPlan* tail = nullptr;
  string id;
  double originalQuantity = 0.0;
};

vector<SplitPair> g_liveSplits;
size_t g_splitSeq = 0;

bool maySplitRoot(OperationPlan* whole, double* outChunk) {
  // Eligible: economic > 1, or SNP ≥ 1, or 备件/断点 with long processing hours.
  if (!isMovableRoot(whole)) return false;
  if (!whole->getStringProperty("alns_role", "").empty()) return false;
  Operation* policyOp = policyOperationOf(whole);
  if (!policyOp) return false;
  const bool useEconomic = envInt("CP_LOT_SPLIT_USE_ECONOMIC", 1) != 0;
  const long exactThreshold =
      static_cast<long>(max(1, envInt("CP_LOT_SPLIT_EXACT_HOURS", 8))) * 3600L;
  const bool exact = policyOp->getBoolProperty("fc_exact_quantity", false);
  const double economic =
      useEconomic ? policyOp->getDoubleProperty("fc_economic_batch", 0.0) : 0.0;
  double rawSnp = policyOp->getDoubleProperty("fc_snp", 0.0);
  if (!policyOp->getBoolProperty("fc_batch_policy", false) &&
      policyOp->getSizeMultiple() > kEps)
    rawSnp = policyOp->getSizeMultiple();
  const double qty = whole->getQuantity();
  const long processing = processingSeconds(whole);
  double splitEconomic = 0.0;
  double splitSnp = 1.0;
  bool ok = false;
  if (economic > 1.0 + kEps) {
    splitEconomic = economic;
    splitSnp = rawSnp >= 1.0 - kEps ? rawSnp : 1.0;
    ok = qty > 2.0 * max(1.0, splitEconomic) + kEps;
  } else if (rawSnp >= 1.0 - kEps) {
    splitSnp = rawSnp;
    ok = qty > 2.0 * splitSnp + kEps;
  }
  // Long-hours: only 备件/断点 (fc_exact_quantity), same as XOR split policy.
  if (!ok && exact && processing > exactThreshold && qty > 1.0 + kEps) {
    ok = true;
    splitSnp = rawSnp >= 1.0 - kEps ? rawSnp : 1.0;
  }
  if (!ok) return false;
  const double chunk = cappedSplitShipment(qty, splitSnp, splitEconomic, 2);
  if (chunk + kEps >= qty || qty - chunk <= kEps) return false;
  if (outChunk) *outChunk = chunk;
  return true;
}

long tardinessOf(OperationPlan* root) {
  Demand* demand = demandOf(root);
  if (!demand) return 0;
  const Date due = demand->getDue();
  const Date end = root->getEnd();
  if (due == Date::infinitePast || due == Date::infiniteFuture) return 0;
  if (end == Date::infinitePast || end <= due) return 0;
  return static_cast<long>((end - due).getSeconds());
}

long destroyPriority(OperationPlan* root) {
  const long late = tardinessOf(root);
  const long size =
      static_cast<long>(llround(max(0.0, root->getQuantity()) * 100.0));
  return late * 10 + size + processingSeconds(root) / 60;
}

SplitPair* applySplit(OperationPlan* whole, double chunk) {
  if (!whole || chunk <= kEps) return nullptr;
  const double qty = whole->getQuantity();
  const double tailQty = qty - chunk;
  Operation* rootOp = whole->getOperation();
  if (!rootOp || tailQty <= kEps) return nullptr;
  Date cursor = whole->getStart() != Date::infinitePast
                    ? whole->getStart()
                    : Plan::instance().getCurrent();
  const PooledString batch = whole->getBatch();
  Demand* demand = demandOf(whole);
  const string id = string("alns_split_") + to_string(++g_splitSeq);

  // Reuse the live whole plan as the head.  This keeps all pegging and owner
  // links valid and creates only one new global plan, the tail.
  forceLotQuantity(whole, chunk);
  rootOp->setOperationPlanParameters(whole, chunk, cursor, Date::infinitePast,
                                     false, true, false);
  if (whole->getEnd() != Date::infinitePast &&
      whole->getEnd() != Date::infiniteFuture)
    cursor = whole->getEnd();

  OperationPlan* tail = rootOp->createOperationPlan(
      tailQty, cursor, Date::infinitePast, batch, demand, nullptr, true, false);
  if (!tail) {
    forceLotQuantity(whole, qty);
    whole->deleteProperty("alns_role");
    whole->deleteProperty("alns_split_id");
    return nullptr;
  }
  if (fabs(tail->getQuantity() - tailQty) > kEps)
    forceLotQuantity(tail, tailQty);
  cloneSplitChildren(whole, tail, tailQty / max(kEps, chunk));
  rootOp->setOperationPlanParameters(tail, tailQty, cursor, Date::infinitePast,
                                     false, true, false);

  auto mark = [&](OperationPlan* part, const char* role) {
    part->setStatus("proposed");
    for (OperationPlan::iterator child(part); child != part->end(); ++child)
      child->setStatus("proposed");
    part->setStringProperty("alns_role", role);
    part->setStringProperty("alns_split_id", id);
  };
  mark(whole, "head");
  mark(tail, "tail");
  // createOperationPlan is lazy: activation links the tail into the global
  // list scanned by partitioned CP and by the score function.
  if (!tail->activate(false)) {
    forceLotQuantity(whole, qty);
    whole->deleteProperty("alns_role");
    whole->deleteProperty("alns_split_id");
    return nullptr;
  }

  g_liveSplits.push_back({whole, whole, tail, id, qty});
  return &g_liveSplits.back();
}

bool applyMerge(SplitPair& pair) {
  if (!pair.whole || !pair.head || !pair.tail) return false;
  const double merged = pair.originalQuantity > kEps
                            ? pair.originalQuantity
                            : pair.head->getQuantity() + pair.tail->getQuantity();
  forceLotQuantity(pair.whole, merged);
  pair.whole->deleteProperty("alns_role");
  pair.whole->deleteProperty("alns_split_id");
  pair.whole->setStatus("proposed");
  for (OperationPlan::iterator child(pair.whole); child != pair.whole->end();
       ++child)
    if (child->getConfirmed()) child->setStatus("proposed");
  tryDeleteOpPlan(pair.tail);
  pair.head = pair.whole;
  pair.tail = nullptr;
  return true;
}

vector<OperationPlan*> collectMovableRoots() {
  vector<OperationPlan*> roots;
  for (auto plan = OperationPlan::begin(); plan != OperationPlan::end();
       ++plan)
    if (isMovableRoot(&*plan)) roots.push_back(&*plan);
  return roots;
}

size_t destroyBudget(size_t population, mt19937& rng, double minFrac,
                     double maxFrac) {
  if (population == 0) return 0;
  minFrac = max(0.05, min(0.9, minFrac));
  maxFrac = max(minFrac, min(0.9, maxFrac));
  uniform_real_distribution<double> frac(minFrac, maxFrac);
  size_t k = static_cast<size_t>(llround(frac(rng) * population));
  // Large-neighborhood floor: at least 8 lots or 10% when population allows.
  k = max(k, min(population, max<size_t>(8, population / 10)));
  return min(population, k);
}

// --- Destroy operators (LARGE neighborhoods) --------------------------------

unordered_set<OperationPlan*> destroyWorst(
    const vector<OperationPlan*>& pool, size_t k, mt19937& rng) {
  vector<pair<long, OperationPlan*>> ranked;
  ranked.reserve(pool.size());
  for (OperationPlan* root : pool)
    ranked.emplace_back(destroyPriority(root), root);
  sort(ranked.begin(), ranked.end(),
       [](const auto& a, const auto& b) { return a.first > b.first; });
  // Roulette among top 2k to keep diversity.
  const size_t band = min(ranked.size(), max(k * 2, k));
  unordered_set<OperationPlan*> destroyed;
  while (destroyed.size() < k && destroyed.size() < band) {
    // Linear bias toward worse ranks.
    uniform_real_distribution<double> uni(0.0, 1.0);
    const double u = uni(rng);
    const size_t idx =
        min(band - 1, static_cast<size_t>(floor(u * u * band)));
    destroyed.insert(ranked[idx].second);
  }
  return destroyed;
}

unordered_set<OperationPlan*> destroyRandom(
    const vector<OperationPlan*>& pool, size_t k, mt19937& rng) {
  vector<OperationPlan*> copy = pool;
  shuffle(copy.begin(), copy.end(), rng);
  unordered_set<OperationPlan*> destroyed;
  for (size_t i = 0; i < copy.size() && destroyed.size() < k; ++i)
    destroyed.insert(copy[i]);
  return destroyed;
}

double shawRelatedness(OperationPlan* a, OperationPlan* b, const Date& origin) {
  if (!a || !b || a == b) return 0.0;
  double score = 0.0;
  if (workshopOf(a) && workshopOf(a) == workshopOf(b)) score += 3.0;
  if (itemOf(a) && itemOf(a) == itemOf(b)) score += 2.0;
  if (demandOf(a) && demandOf(a) == demandOf(b)) score += 4.0;
  unordered_set<Resource*> ra, rb;
  collectResources(a, ra);
  collectResources(b, rb);
  for (Resource* r : ra)
    if (rb.count(r)) {
      score += 1.5;
      break;
    }
  if (a->getStart() != Date::infinitePast &&
      b->getStart() != Date::infinitePast) {
    const long gap = labs(static_cast<long>(
        (a->getStart() - b->getStart()).getSeconds()));
    const double days = gap / 86400.0;
    score += max(0.0, 2.0 - days);
  }
  (void)origin;
  return score;
}

unordered_set<OperationPlan*> destroyShaw(
    const vector<OperationPlan*>& pool, size_t k, mt19937& rng,
    const Date& origin) {
  if (pool.empty() || k == 0) return {};
  uniform_int_distribution<size_t> pick(0, pool.size() - 1);
  OperationPlan* seed = pool[pick(rng)];
  // Bias seed toward late lots half the time.
  if (uniform_int_distribution<int>(0, 1)(rng) == 0) {
    auto worst = destroyWorst(pool, 1, rng);
    if (!worst.empty()) seed = *worst.begin();
  }
  unordered_set<OperationPlan*> destroyed{seed};
  while (destroyed.size() < k) {
    OperationPlan* pivot = seed;
    {
      uniform_int_distribution<size_t> among(0, destroyed.size() - 1);
      size_t i = 0;
      const size_t want = among(rng);
      for (OperationPlan* p : destroyed) {
        if (i++ == want) {
          pivot = p;
          break;
        }
      }
    }
    vector<pair<double, OperationPlan*>> related;
    for (OperationPlan* cand : pool) {
      if (destroyed.count(cand)) continue;
      related.emplace_back(shawRelatedness(pivot, cand, origin), cand);
    }
    if (related.empty()) break;
    sort(related.begin(), related.end(),
         [](const auto& a, const auto& b) { return a.first > b.first; });
    const size_t band = min(related.size(), max<size_t>(5, k / 2));
    uniform_real_distribution<double> uni(0.0, 1.0);
    const double u = uni(rng);
    const size_t idx =
        min(band - 1, static_cast<size_t>(floor(u * u * band)));
    destroyed.insert(related[idx].second);
  }
  return destroyed;
}

unordered_set<OperationPlan*> destroyTimeSlice(
    const vector<OperationPlan*>& pool, size_t k, mt19937& rng,
    const Date& origin, int horizonDays) {
  if (pool.empty()) return {};
  const long horizon =
      static_cast<long>(max(1, horizonDays)) * 86400L;
  // Pick a random window covering ~destroy fraction of the horizon, then
  // take up to k lots that overlap it (LARGE contiguous block).
  uniform_real_distribution<double> uni(0.0, 1.0);
  const double widthFrac = max(0.15, min(0.5, static_cast<double>(k) /
                                                 max<size_t>(1, pool.size())));
  const long width = max(3600L, static_cast<long>(widthFrac * horizon));
  const long startOff =
      static_cast<long>(uni(rng) * max(0.0, static_cast<double>(horizon - width)));
  const Date lo = origin + Duration(startOff);
  const Date hi = origin + Duration(startOff + width);

  vector<OperationPlan*> hit;
  for (OperationPlan* root : pool) {
    if (root->getEnd() == Date::infinitePast) continue;
    if (root->getEnd() < lo || root->getStart() > hi) continue;
    hit.push_back(root);
  }
  if (hit.size() < k) {
    // Fill with Shaw around a hit seed so size stays LARGE.
    auto base = destroyShaw(pool, k, rng, origin);
    for (OperationPlan* p : hit) base.insert(p);
    while (base.size() > k) base.erase(base.begin());
    return base;
  }
  shuffle(hit.begin(), hit.end(), rng);
  unordered_set<OperationPlan*> destroyed;
  for (size_t i = 0; i < hit.size() && destroyed.size() < k; ++i)
    destroyed.insert(hit[i]);
  return destroyed;
}

unordered_set<OperationPlan*> destroyWorkshop(
    const vector<OperationPlan*>& pool, size_t k, mt19937& rng) {
  array<int, 4> groups{23, 1, 4, 5};
  shuffle(groups.begin(), groups.end(), rng);
  for (int ws : groups) {
    vector<OperationPlan*> inGroup;
    for (OperationPlan* root : pool)
      if (workshopOf(root) == ws) inGroup.push_back(root);
    if (inGroup.size() < max<size_t>(4, k / 4)) continue;
    // Destroy a LARGE share of this workshop (at least k, up to all in group).
    size_t take = min(inGroup.size(), max(k, inGroup.size() / 2));
    shuffle(inGroup.begin(), inGroup.end(), rng);
    unordered_set<OperationPlan*> destroyed;
    for (size_t i = 0; i < take; ++i) destroyed.insert(inGroup[i]);
    return destroyed;
  }
  return destroyRandom(pool, k, rng);
}

// Destroy all lots of the most tardy demands until size ≈ k (cluster LNS).
unordered_set<OperationPlan*> destroyLateDemand(
    const vector<OperationPlan*>& pool, size_t k, mt19937& rng) {
  unordered_map<Demand*, long> tardy;
  unordered_map<Demand*, vector<OperationPlan*>> byDemand;
  for (OperationPlan* root : pool) {
    Demand* demand = demandOf(root);
    if (!demand) continue;
    byDemand[demand].push_back(root);
    tardy[demand] = max(tardy[demand], tardinessOf(root));
  }
  vector<pair<long, Demand*>> ranked;
  for (const auto& entry : tardy)
    if (entry.second > 0) ranked.emplace_back(entry.second, entry.first);
  if (ranked.empty()) return destroyWorst(pool, k, rng);
  sort(ranked.begin(), ranked.end(),
       [](const auto& a, const auto& b) { return a.first > b.first; });
  // Shuffle among equally late bands a bit.
  shuffle(ranked.begin(),
          ranked.begin() + min(ranked.size(), max<size_t>(3, ranked.size() / 4)),
          rng);
  unordered_set<OperationPlan*> destroyed;
  for (const auto& entry : ranked) {
    for (OperationPlan* root : byDemand[entry.second]) {
      destroyed.insert(root);
      if (destroyed.size() >= k) return destroyed;
    }
  }
  if (destroyed.size() < k) {
    auto extra = destroyShaw(pool, k, rng, Plan::instance().getCurrent());
    destroyed.insert(extra.begin(), extra.end());
  }
  while (destroyed.size() > k) destroyed.erase(destroyed.begin());
  return destroyed;
}

// True LNS destroy: drop the incumbent seed so CP must retime the
// neighborhood.  Keep a positive in-window span — zero-length dates at
// origin fall out of partitioned CP's active set (end > origin required).
void clearDestroyedSchedule(const unordered_set<OperationPlan*>& destroyed,
                            const Date& origin) {
  for (OperationPlan* root : destroyed) {
    if (!root) continue;
    vector<OperationPlan*> stack{root};
    while (!stack.empty()) {
      OperationPlan* node = stack.back();
      stack.pop_back();
      try {
        if (node->getProposed() || node->getApproved()) {
          long span = max(1L, durationSeconds(node));
          if (span <= 1) span = max(1L, processingSeconds(node));
          if (span <= 1) span = 60L;
          node->setStartAndEnd(origin, origin + Duration(span));
        }
      } catch (...) {
      }
      for (OperationPlan::iterator child(node); child != node->end(); ++child)
        stack.push_back(&*child);
    }
  }
}

struct FrozenNode {
  OperationPlan* plan = nullptr;
  string status;
};

vector<FrozenNode> freezeOutside(
    const unordered_set<OperationPlan*>& hot) {
  vector<FrozenNode> frozen;
  for (auto plan = OperationPlan::begin(); plan != OperationPlan::end();
       ++plan) {
    if (plan->getOwner()) continue;
    if (plan->getStringProperty("alns_role", "") == "whole_parked") continue;
    if (hot.count(&*plan)) continue;
    if (!plan->getProposed() && !plan->getApproved()) continue;
    // Freeze the whole tree so partition CP cannot move siblings of a
    // confirmed root that stayed proposed.
    vector<OperationPlan*> stack{&*plan};
    while (!stack.empty()) {
      OperationPlan* node = stack.back();
      stack.pop_back();
      if (node->getProposed() || node->getApproved()) {
        const string status = node->getStatus();
        node->setBoolProperty("alns_frozen", true);
        node->setStatusRaw("confirmed");
        frozen.push_back({node, status});
      }
      for (OperationPlan::iterator child(node); child != node->end(); ++child)
        stack.push_back(&*child);
    }
  }
  return frozen;
}

void unfreeze(const vector<FrozenNode>& frozen) {
  for (const FrozenNode& saved : frozen) {
    if (!saved.plan) continue;
    saved.plan->setStatus(saved.status.empty() ? "proposed" : saved.status);
    saved.plan->deleteProperty("alns_frozen");
  }
}

struct OperatorStats {
  double weight = 1.0;
  double score = 0.0;
  size_t used = 0;
  size_t improved = 0;
  size_t accepted = 0;
};

// Destroy operators + structural split/merge.
enum class OpKind {
  Worst = 0,
  Random = 1,
  Shaw = 2,
  TimeSlice = 3,
  Workshop = 4,
  Demand = 5,
  Split = 6,
  Merge = 7,
  Count = 8
};

constexpr int kOpCount = static_cast<int>(OpKind::Count);

const char* opName(OpKind kind) {
  switch (kind) {
    case OpKind::Worst:
      return "destroy_worst";
    case OpKind::Random:
      return "destroy_random";
    case OpKind::Shaw:
      return "destroy_shaw";
    case OpKind::TimeSlice:
      return "destroy_time";
    case OpKind::Workshop:
      return "destroy_workshop";
    case OpKind::Demand:
      return "destroy_demand";
    case OpKind::Split:
      return "struct_split";
    case OpKind::Merge:
      return "struct_merge";
    default:
      return "unknown";
  }
}

OpKind roulette(mt19937& rng, const array<OperatorStats, kOpCount>& stats) {
  double sum = 0.0;
  for (const OperatorStats& s : stats) sum += max(0.05, s.weight);
  uniform_real_distribution<double> uni(0.0, sum);
  double draw = uni(rng);
  for (int i = 0; i < kOpCount; ++i) {
    draw -= max(0.05, stats[i].weight);
    if (draw <= 0.0) return static_cast<OpKind>(i);
  }
  return OpKind::Shaw;
}

void noteOutcome(OperatorStats& stat, int tier) {
  // tier: 3 = new best, 2 = improved, 1 = SA-accepted worse, 0 = rejected
  ++stat.used;
  static const double sigma[4] = {0.0, 5.0, 9.0, 33.0};
  stat.score += sigma[max(0, min(3, tier))];
  if (tier >= 2) ++stat.improved;
  if (tier >= 1) ++stat.accepted;
}

void updateWeights(array<OperatorStats, kOpCount>& stats, double reaction,
                   double decay) {
  for (OperatorStats& s : stats) {
    if (s.used == 0) continue;
    const double observed = s.score / static_cast<double>(s.used);
    s.weight = decay * s.weight + reaction * observed;
    s.weight = min(50.0, max(0.2, s.weight));
    s.score = 0.0;
    s.used = 0;
  }
}

void logWeights(const array<OperatorStats, kOpCount>& stats) {
  logger << "CP-ALNS weights:";
  for (int i = 0; i < kOpCount; ++i)
    logger << " " << opName(static_cast<OpKind>(i)) << "="
           << stats[i].weight;
  logger << "\n";
}

struct EnvGuard {
  unordered_map<string, string> saved;
  unordered_set<string> missing;
  void put(const char* key, const string& value) {
    const char* prev = getenv(key);
    if (prev)
      saved[key] = prev;
    else
      missing.insert(key);
    setEnv(key, value);
  }
  ~EnvGuard() {
    for (const auto& entry : saved) setEnv(entry.first.c_str(), entry.second);
    for (const string& key : missing) unsetenv(key.c_str());
  }
};

OperationPlan* pickSplitTarget(mt19937& rng, const vector<OperationPlan*>& pool) {
  vector<pair<long, OperationPlan*>> ranked;
  for (OperationPlan* root : pool) {
    if (!maySplitRoot(root, nullptr)) continue;
    ranked.emplace_back(destroyPriority(root), root);
  }
  if (ranked.empty()) return nullptr;
  sort(ranked.begin(), ranked.end(),
       [](const auto& a, const auto& b) { return a.first > b.first; });
  const size_t top = min<size_t>(12, ranked.size());
  uniform_int_distribution<size_t> pick(0, top - 1);
  return ranked[pick(rng)].second;
}

SplitPair* pickMergeTarget(mt19937& rng) {
  vector<SplitPair*> live;
  for (SplitPair& pair : g_liveSplits)
    if (pair.whole && pair.head && pair.tail) live.push_back(&pair);
  if (live.empty()) return nullptr;
  uniform_int_distribution<size_t> pick(0, live.size() - 1);
  return live[pick(rng)];
}

void runPartitionedCp() { run_cp_partitioned(nullptr, nullptr); }

void runLnsRepair(int limitSeconds,
                  const unordered_set<OperationPlan*>& destroyed,
                  bool seedCurrentSchedule) {
  EnvGuard guard;
  guard.put("CP_LOT_SPLIT", "0");
  guard.put("CP_PREFLIGHT_MODEL", "0");
  guard.put("CP_REPAIR_HINT", "0");
  guard.put("CP_SEED_MRP", seedCurrentSchedule ? "1" : "0");
  guard.put("CP_LEX_STAGES", "1");
  unsetenv("CP_TIME_LIMIT_MS");

  unordered_set<int> workshops;
  for (OperationPlan* root : destroyed) collectWorkshops(root, workshops);
  const int budget = max(4, limitSeconds);
  // Partition divides the env budget by 4 groups → pass 4× so each stage
  // gets ~budget seconds (CP_ALNS_REPAIR_* / seed use the same convention).
  if (workshops.size() == 1) {
    guard.put("CP_PARTITION_ONLY", to_string(*workshops.begin()));
    guard.put("CP_TIME_LIMIT_SECONDS", to_string(budget * 4));
  } else {
    unsetenv("CP_PARTITION_ONLY");
    guard.put("CP_TIME_LIMIT_SECONDS", to_string(max(4, budget * 4)));
  }
  runPartitionedCp();
  if (cpLastPartitionOptimized() == 0)
    logger << "CP-ALNS repair warn: partition optimized 0 ops (destroyed="
           << destroyed.size() << ") — treat as no-op\n";
}

}  // namespace

PyObject* run_cp_alns(PyObject*, PyObject*) {
#ifndef HAVE_ORTOOLS
  logger << "CP-ALNS skipped: this engine was built without OR-Tools\n";
  Py_RETURN_NONE;
#else
  g_liveSplits.clear();
  g_splitSeq = 0;

  const Date origin = Plan::instance().getCurrent();
  const int horizonDays = max(1, envInt("CP_HORIZON_DAYS", 3));
  const int iterations = max(1, envInt("CP_ALNS_ITERATIONS", 40));
  const int segmentSize = max(5, envInt("CP_ALNS_SEGMENT", 10));
  const int patienceSeg = max(1, envInt("CP_ALNS_PATIENCE", 4));
  // Large-neighborhood destroy fraction (of movable roots).
  const double destroyMin = envDouble("CP_ALNS_DESTROY_MIN", 0.15);
  const double destroyMax = envDouble("CP_ALNS_DESTROY_MAX", 0.40);
  // Seconds per workshop stage (partition divides total by 4).
  // Prefer CP_ALNS_STAGE_S so DB/Parameter CP_TIME_LIMIT_SECONDS cannot
  // silently keep a 150s/group seed.
  const int stageSec = max(1, envInt("CP_ALNS_STAGE_S", 10));
  const int stageBudget = stageSec * 4;
  // Repair budget scales with neighborhood size, capped at stageSec.
  const int repairBaseSec =
      max(1, envInt("CP_ALNS_REPAIR_BASE_S", stageSec));
  const double repairPerLotSec = envDouble("CP_ALNS_REPAIR_PER_LOT_S", 0.0);
  const int repairCapSec =
      max(repairBaseSec, envInt("CP_ALNS_REPAIR_CAP_S", stageSec));
  const int splitRepairSec =
      max(repairCapSec, envInt("CP_ALNS_SPLIT_REPAIR_S", 30));
  const double reaction = envDouble("CP_ALNS_REACTION", 0.4);
  const double decay = envDouble("CP_ALNS_DECAY", 0.8);
  const double saStart = envDouble("CP_ALNS_SA_T0", 0.05);  // relative to scalar
  const double saCool = envDouble("CP_ALNS_SA_COOL", 0.97);
  const double saReheat = envDouble("CP_ALNS_SA_REHEAT", 1.5);
  const int maxSplitsPerMove = max(1, envInt("CP_ALNS_SPLITS_PER_MOVE", 3));
  const double splitProbability =
      max(0.0, min(1.0, envDouble("CP_ALNS_SPLIT_PROB", 0.65)));
  const unsigned seed = static_cast<unsigned>(envInt("CP_ALNS_SEED", 7));
  mt19937 rng(seed);

  array<OperatorStats, kOpCount> stats;
  Score incumbent = evaluatePlan(origin, horizonDays);
  Score best = incumbent;
  vector<SnapshotNode> bestSnap;
  double temperature = max(1.0, incumbent.scalar() * saStart);
  const double t0 = temperature;

  logger << "CP-ALNS start (LARGE neighborhood): destroy=["
         << destroyMin << "," << destroyMax << "] iters=" << iterations
         << " segment=" << segmentSize << " patience=" << patienceSeg
         << " stage=" << stageSec << "s/group"
         << " repair_base=" << repairBaseSec << "s cap=" << repairCapSec << "s"
         << " split_repair=" << splitRepairSec << "s"
         << " split_prob=" << splitProbability
         << " max_splits=" << maxSplitsPerMove
         << " missing=" << incumbent.missing
         << " tardy=" << incumbent.tardiness
         << " util=" << incumbent.utilPct << "%"
         << " makespan=" << incumbent.makespan << "\n";

  // Seed: full workshop-parallel CP; ALNS owns subsequent structure.
  {
    EnvGuard guard;
    guard.put("CP_LOT_SPLIT", "0");
    guard.put("CP_PREFLIGHT_MODEL", "0");
    guard.put("CP_TIME_LIMIT_SECONDS", to_string(stageBudget));
    unsetenv("CP_TIME_LIMIT_MS");
    unsetenv("CP_PARTITION_ONLY");
    logger << "CP-ALNS seed: run_cp_partitioned (" << stageSec
           << "s/group, budget=" << stageBudget << ")\n";
    runPartitionedCp();
  }
  incumbent = evaluatePlan(origin, horizonDays);
  best = incumbent;
  const Score seedScore = incumbent;
  {
    unordered_set<OperationPlan*> all;
    for (OperationPlan* root : collectMovableRoots()) all.insert(root);
    bestSnap = snapshotRoots(all);
  }
  temperature = max(1.0, incumbent.scalar() * saStart);
  logger << "CP-ALNS after seed: missing=" << incumbent.missing
         << " tardy=" << incumbent.tardiness
         << " util=" << incumbent.utilPct << "% loadH=" << incumbent.loadH
         << " availH=" << incumbent.availH
         << " makespan=" << incumbent.makespan << " lots=" << incumbent.lots
         << "\n";

  size_t accepted = 0;
  size_t saAccepted = 0;
  size_t newBests = 0;
  size_t splitsDone = 0;
  size_t mergesDone = 0;
  int noImproveSeg = 0;
  bool improvedThisSeg = false;

  for (int iter = 0; iter < iterations; ++iter) {
    vector<OperationPlan*> pool = collectMovableRoots();
    if (pool.empty()) break;

    OpKind kind = uniform_real_distribution<double>(0.0, 1.0)(rng) <
                          splitProbability
                      ? OpKind::Split
                      : roulette(rng, stats);
    // ALNS is intentionally split-led.  Accepted splits remain stable for
    // the rest of the search; ordinary destroy operators retime both parts.
    if (kind == OpKind::Merge) kind = OpKind::Split;
    if (kind == OpKind::Split && !pickSplitTarget(rng, pool))
      kind = OpKind::Shaw;

    const size_t k = destroyBudget(pool.size(), rng, destroyMin, destroyMax);
    unordered_set<OperationPlan*> destroyed;
    vector<SplitPair> createdSplits;
    SplitPair createdMerge;
    bool mergeLive = false;
    double chunk = 0.0;

    // Snapshot full movable set before structural edits (safe reject).
    unordered_set<OperationPlan*> prePool;
    for (OperationPlan* root : pool) prePool.insert(root);
    vector<SnapshotNode> beforeAll = snapshotRoots(prePool);

    if (kind == OpKind::Split) {
      // Up to N eligible orders, all locked to ONE workshop so repair stays
      // PARTITION_ONLY and does not cross workshop boundaries.
      int moveWorkshop = 0;
      unordered_set<OperationPlan*> pickedWholes;
      for (int s = 0; s < maxSplitsPerMove; ++s) {
        pool = collectMovableRoots();
        vector<OperationPlan*> eligible;
        eligible.reserve(pool.size());
        for (OperationPlan* root : pool) {
          if (pickedWholes.count(root)) continue;
          if (!maySplitRoot(root, nullptr)) continue;
          unordered_set<int> rootWorkshops;
          collectWorkshops(root, rootWorkshops);
          if (rootWorkshops.size() != 1) continue;
          if (moveWorkshop > 0 && !rootWorkshops.count(moveWorkshop)) continue;
          eligible.push_back(root);
        }
        OperationPlan* target = pickSplitTarget(rng, eligible);
        if (!target || !maySplitRoot(target, &chunk)) break;
        if (moveWorkshop == 0) {
          unordered_set<int> rootWorkshops;
          collectWorkshops(target, rootWorkshops);
          if (rootWorkshops.size() != 1) break;
          moveWorkshop = *rootWorkshops.begin();
        }
        SplitPair* pair = applySplit(target, chunk);
        if (!pair) break;
        pickedWholes.insert(target);
        createdSplits.push_back(*pair);
      }
      if (createdSplits.empty()) {
        noteOutcome(stats[static_cast<int>(kind)], 0);
        continue;
      }
      pool = collectMovableRoots();
      // Repair the whole affected workshop only.
      for (OperationPlan* root : pool)
        if (touchesWorkshop(root, moveWorkshop)) destroyed.insert(root);
      for (const SplitPair& pair : createdSplits) {
        if (pair.head) destroyed.insert(pair.head);
        if (pair.tail) destroyed.insert(pair.tail);
      }
      logger << "CP-ALNS split move: workshop=" << moveWorkshop
             << " orders=" << createdSplits.size()
             << " repair_roots=" << destroyed.size() << "\n";
    } else if (kind == OpKind::Merge) {
      SplitPair* mergePair = pickMergeTarget(rng);
      if (!mergePair) {
        noteOutcome(stats[static_cast<int>(kind)], 0);
        continue;
      }
      createdMerge = *mergePair;
      if (!applyMerge(*mergePair)) {
        noteOutcome(stats[static_cast<int>(kind)], 0);
        continue;
      }
      mergeLive = true;
      g_liveSplits.erase(
          remove_if(g_liveSplits.begin(), g_liveSplits.end(),
                    [&](const SplitPair& p) {
                      return p.id == createdMerge.id;
                    }),
          g_liveSplits.end());
      pool = collectMovableRoots();
      destroyed = destroyShaw(pool, k, rng, origin);
      if (createdMerge.whole) destroyed.insert(createdMerge.whole);
    } else if (kind == OpKind::Worst) {
      destroyed = destroyWorst(pool, k, rng);
    } else if (kind == OpKind::Random) {
      destroyed = destroyRandom(pool, k, rng);
    } else if (kind == OpKind::Shaw) {
      destroyed = destroyShaw(pool, k, rng, origin);
    } else if (kind == OpKind::TimeSlice) {
      destroyed = destroyTimeSlice(pool, k, rng, origin, horizonDays);
    } else if (kind == OpKind::Demand) {
      destroyed = destroyLateDemand(pool, k, rng);
    } else {
      destroyed = destroyWorkshop(pool, k, rng);
    }

    if (destroyed.empty()) {
      noteOutcome(stats[static_cast<int>(kind)], 0);
      continue;
    }

    const double destroyPct =
        100.0 * static_cast<double>(destroyed.size()) /
        static_cast<double>(max<size_t>(1, pool.size()));
    vector<SnapshotNode> beforeDestroyed = snapshotRoots(destroyed);
    const Score beforeScore = incumbent;

    // DESTROY dates, freeze complement, CP-REPAIR neighborhood.
    if (kind != OpKind::Split) clearDestroyedSchedule(destroyed, origin);
    vector<FrozenNode> frozen = freezeOutside(destroyed);
    const int repairSec = kind == OpKind::Split
                              ? splitRepairSec
                              : min(repairCapSec,
                                    repairBaseSec + static_cast<int>(llround(
                                        repairPerLotSec * static_cast<double>(
                                                              destroyed.size()))));
    runLnsRepair(repairSec, destroyed, kind == OpKind::Split);
    unfreeze(frozen);
    const size_t repaired = cpLastPartitionOptimized();

    Score after = evaluatePlan(origin, horizonDays);
    // No-op repair must not be accepted (placeholder dates look "early").
    if (repaired == 0) after = beforeScore;
    const bool improved = repaired > 0 && after.betterThan(beforeScore);
    const bool newBest = repaired > 0 && after.betterThan(best);
    const bool structural =
        kind == OpKind::Split || kind == OpKind::Merge;
    // Split/merge: keep ONLY on global improve; otherwise roll back the
    // whole batch (all ≤3 splits). Pure LNS may SA-accept a worse schedule.
    bool take = improved;
    if (!take && !structural && repaired > 0) {
      const double delta = after.scalar() - beforeScore.scalar();
      if (delta > 0.0) {
        uniform_real_distribution<double> uni(0.0, 1.0);
        const double prob = exp(-delta / max(1e-6, temperature));
        if (uni(rng) < prob) take = true;
      }
    }
    if (repaired == 0) take = false;
    if (structural && !improved) take = false;

    int tier = 0;
    if (take) {
      incumbent = after;
      ++accepted;
      improvedThisSeg = improvedThisSeg || improved;
      if (newBest) {
        best = after;
        ++newBests;
        tier = 3;
        unordered_set<OperationPlan*> all;
        for (OperationPlan* root : collectMovableRoots()) all.insert(root);
        bestSnap = snapshotRoots(all);
        temperature = max(temperature, t0);  // mild reheat on new best
        noImproveSeg = 0;
      } else if (improved) {
        tier = 2;
      } else {
        ++saAccepted;
        tier = 1;
      }
      if (kind == OpKind::Split) splitsDone += createdSplits.size();
      if (kind == OpKind::Merge) ++mergesDone;
    } else {
      // Reject: restore destroyed dates; undo structural edits.
      restoreSnapshot(beforeDestroyed);
      if (kind == OpKind::Split && !createdSplits.empty()) {
        for (SplitPair& created : createdSplits) {
          applyMerge(created);
          g_liveSplits.erase(
              remove_if(g_liveSplits.begin(), g_liveSplits.end(),
                        [&](const SplitPair& p) {
                          return p.id == created.id;
                        }),
              g_liveSplits.end());
        }
        restoreSnapshot(beforeAll);
      } else if (kind == OpKind::Merge && mergeLive) {
        if (createdMerge.whole &&
            maySplitRoot(createdMerge.whole, &chunk))
          applySplit(createdMerge.whole, chunk);
        restoreSnapshot(beforeAll);
      }
      tier = 0;
    }
    noteOutcome(stats[static_cast<int>(kind)], tier);
    temperature *= saCool;

    if ((iter + 1) % segmentSize == 0) {
      updateWeights(stats, reaction, decay);
      if (!improvedThisSeg) {
        ++noImproveSeg;
        temperature = max(temperature, t0 * saReheat);
        logger << "CP-ALNS stagnate seg=" << noImproveSeg
               << "/" << patienceSeg << " reheat T=" << temperature << "\n";
        if (noImproveSeg >= patienceSeg) {
          logger << "CP-ALNS early stop after " << (iter + 1)
                 << " iters (no improve for " << patienceSeg
                 << " segments)\n";
          break;
        }
      } else {
        noImproveSeg = 0;
      }
      improvedThisSeg = false;
      logWeights(stats);
    }

    if ((iter + 1) % segmentSize == 0 || iter + 1 == iterations)
      logger << "CP-ALNS iter=" << (iter + 1) << "/" << iterations
             << " op=" << opName(kind) << " destroy=" << destroyed.size()
             << "/" << pool.size() << " (" << destroyPct << "%)"
             << " repair=" << repairSec << "s optimized=" << repaired
             << (take ? (newBest ? " BEST" : (improved ? " accept" : " SA"))
                      : (repaired == 0 ? " noop" : " reject"))
             << " missing=" << incumbent.missing
             << " tardy=" << incumbent.tardiness
             << " util=" << incumbent.utilPct << "%"
             << " best_tardy=" << best.tardiness
             << " best_util=" << best.utilPct << "%"
             << " T=" << temperature << "\n";
  }

  // Restore BEST archive if SA drifted away.
  if (best.betterThan(incumbent) && !bestSnap.empty()) {
    logger << "CP-ALNS restore BEST archive (tardy " << incumbent.tardiness
           << " -> " << best.tardiness << ", util " << incumbent.utilPct
           << "% -> " << best.utilPct << "%)\n";
    restoreSnapshot(bestSnap);
    incumbent = evaluatePlan(origin, horizonDays);
  }

  // Commit accepted splits: both live head and tail are already global plans.
  size_t committedSplits = 0;
  for (SplitPair& pair : g_liveSplits) {
    if (!pair.whole || !pair.head || !pair.tail) continue;
    if (pair.head) {
      pair.head->deleteProperty("alns_role");
      pair.head->deleteProperty("alns_split_id");
    }
    if (pair.tail) {
      pair.tail->deleteProperty("alns_role");
      pair.tail->deleteProperty("alns_split_id");
    }
    ++committedSplits;
  }
  g_liveSplits.clear();

  if (accepted > 0 || committedSplits > 0) {
    EnvGuard guard;
    guard.put("CP_LOT_SPLIT", "0");
    guard.put("CP_PREFLIGHT_MODEL", "0");
    guard.put("CP_TIME_LIMIT_SECONDS", to_string(stageBudget));
    unsetenv("CP_TIME_LIMIT_MS");
    unsetenv("CP_PARTITION_ONLY");
    logger << "CP-ALNS final: run_cp_partitioned (" << stageSec
           << "s/group)\n";
    runPartitionedCp();
  } else {
    logger << "CP-ALNS final: skipped (no accepted neighborhood or split); "
              "keeping seed CP schedule\n";
  }

  Score finalScore = evaluatePlan(origin, horizonDays);
  logWeights(stats);
  logger << "CP-ALNS done: accepted=" << accepted << " SA=" << saAccepted
         << " bests=" << newBests << " splits_committed=" << committedSplits
         << " splits_moves=" << splitsDone << " merges=" << mergesDone
         << " missing=" << finalScore.missing
         << " tardy=" << finalScore.tardiness
         << " util=" << finalScore.utilPct << "%"
         << " makespan=" << finalScore.makespan << " lots=" << finalScore.lots
         << "\n";
  logger << "CP-ALNS vs seed: tardy " << seedScore.tardiness << " -> "
         << finalScore.tardiness << " ("
         << (seedScore.tardiness
                 ? (100.0 *
                    (static_cast<double>(seedScore.tardiness -
                                        finalScore.tardiness) /
                     static_cast<double>(seedScore.tardiness)))
                 : 0.0)
         << "%), util " << seedScore.utilPct << "% -> " << finalScore.utilPct
         << "% (Δ " << (finalScore.utilPct - seedScore.utilPct) << ")\n";
  Py_RETURN_NONE;
#endif
}

}  // namespace ccAPPS

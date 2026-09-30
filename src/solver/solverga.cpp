#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <deque>
#include <fstream>
#include <iomanip>
#include <numeric>
#include <queue>
#include <sstream>
#include <unordered_set>

#include "ccAPPS/solverga.h"

namespace ccAPPS {

const MetaClass* SolverGA::metadata;

namespace {

constexpr int kGAInfDuration = numeric_limits<int>::max() / 4;

int getEnvInt(const char* name, int fallback, int minimum = 1) {
  const char* value = getenv(name);
  if (!value || !*value) return fallback;
  char* end = nullptr;
  long parsed = strtol(value, &end, 10);
  if (end == value || parsed < minimum) return fallback;
  if (parsed > numeric_limits<int>::max()) return fallback;
  return static_cast<int>(parsed);
}

double getEnvDouble(const char* name, double fallback, double minimum = 0.0) {
  const char* value = getenv(name);
  if (!value || !*value) return fallback;
  char* end = nullptr;
  const double parsed = strtod(value, &end);
  if (end == value || !isfinite(parsed) || parsed < minimum) return fallback;
  return parsed;
}

GAConfig::MaterialCheckMode getMaterialCheckModeFromEnv(
    GAConfig::MaterialCheckMode fallback) {
  const char* value = getenv("GA_MATERIAL_MODE");
  if (!value || !*value) return fallback;
  string mode(value);
  transform(mode.begin(), mode.end(), mode.begin(),
            [](unsigned char c) { return static_cast<char>(tolower(c)); });
  if (mode == "precheck") return GAConfig::PRECHECK;
  if (mode == "postcheck") return GAConfig::POSTCHECK;
  return fallback;
}

string jsonEscape(const string& value) {
  string result;
  result.reserve(value.size() + 8);
  for (char ch : value) {
    switch (ch) {
      case '\\':
        result += "\\\\";
        break;
      case '"':
        result += "\\\"";
        break;
      case '\n':
        result += "\\n";
        break;
      case '\r':
        result += "\\r";
        break;
      case '\t':
        result += "\\t";
        break;
      default:
        result += ch;
        break;
    }
  }
  return result;
}

bool getEnvBool(const char* name, bool fallback = false) {
  const char* value = getenv(name);
  if (!value || !*value) return fallback;
  string parsed(value);
  transform(parsed.begin(), parsed.end(), parsed.begin(),
            [](unsigned char c) { return static_cast<char>(tolower(c)); });
  return parsed == "1" || parsed == "true" || parsed == "yes" ||
         parsed == "on";
}

string jsonString(const string& value) {
  return "\"" + jsonEscape(value) + "\"";
}

string dateString(Date date) {
  if (date == Date::infinitePast) return "infinitePast";
  if (date == Date::infiniteFuture) return "infiniteFuture";
  ostringstream out;
  out << date;
  return out.str();
}

string resourceName(const Resource* resource) {
  return resource ? resource->getName() : "";
}

string operationName(const OperationPlan* op) {
  return op && op->getOperation() ? op->getOperation()->getName() : "";
}

string topOwnerName(const OperationPlan* op) {
  auto* top = op ? op->getTopOwner() : nullptr;
  return operationName(top);
}

string opReference(const OperationPlan* op) {
  return op ? op->getReference() : "";
}

string operationDefinitionName(const Operation* op) {
  return op ? op->getName() : "";
}

struct GAInputOption {
  int machineIndex = -1;
  Operation* selectedOperation = nullptr;
  const Resource* primaryResource = nullptr;
  vector<const Resource*> allResources;
  vector<pair<const Load*, const Resource*>> loadAssignments;
  Date earliestStart = Date::infinitePast;
  Duration estimatedDuration = Duration(0L);
};

struct GAInputOperation {
  int geneId = -1;
  int jobIndex = -1;
  int operationIndex = -1;
  OperationPlan* op = nullptr;
  OperationPlan* topOwner = nullptr;
  Demand* demand = nullptr;
  int priority = 999;
  Date due = Date::infiniteFuture;
  vector<GAInputOption> options;
};

struct GAInputJob {
  int jobIndex = -1;
  OperationPlan* topOwner = nullptr;
  Demand* demand = nullptr;
  vector<size_t> operationIndices;
};

struct GAStandardInput {
  int jobsNum = 0;
  int machinesNum = 0;
  int maxOperations = 0;
  int maxCandidateOptions = 0;
  Date schedulingAnchor = Date::infinitePast;
  vector<const Resource*> resources;
  unordered_map<const Resource*, int> machineIndexByResource;
  unordered_map<string, int> machineIndexByName;
  unordered_map<const Resource*, Date> resourceTimes;
  vector<GAInputJob> jobs;
  vector<GAInputOperation> operations;
  vector<CandidateOp> candidates;
  vector<int> jobsOperations;
  vector<vector<int>> geneByJobOperation;
  vector<vector<int>> operationIndexByJobOperation;
  vector<vector<OperationPlan*>> operationPlanByJobOperation;
  vector<vector<int>> candidateMachineCount;
  vector<vector<vector<int>>> candidateMachines;
  vector<vector<vector<int>>> candidateMachineDurations;

  size_t optionCount() const { return candidates.size(); }
  int totalOperations() const {
    return accumulate(jobsOperations.begin(), jobsOperations.end(), 0);
  }
};

// Defined with the solution validators below. Kept as a forward declaration
// because solution application precedes those helpers in this translation unit.
bool isQuantityFlowLineSolution(const AntSolution& solution);

void dumpGAInputIfRequested(
    const vector<CandidateOp>& rawCandidates,
    const GAStandardInput& input) {
  const char* dumpPath = getenv("GA_DUMP_CANDIDATES");
  if (!dumpPath || !*dumpPath) return;

  ofstream out(dumpPath, ios::out | ios::trunc);
  if (!out.is_open()) return;

  out << "{\n";
  out << "  \"raw_candidates\": [\n";
  for (size_t idx = 0; idx < rawCandidates.size(); ++idx) {
    const auto& candidate = rawCandidates[idx];
    out << "    {\n";
    out << "      \"cand_id\": " << candidate.candId << ",\n";
    out << "      \"reference\": " << jsonString(opReference(candidate.op)) << ",\n";
    out << "      \"operation\": " << jsonString(operationName(candidate.op)) << ",\n";
    out << "      \"selected_operation\": "
        << jsonString(operationDefinitionName(candidate.selectedOperation)) << ",\n";
    out << "      \"top_owner\": " << jsonString(topOwnerName(candidate.op)) << ",\n";
    out << "      \"primary_resource\": "
        << jsonString(resourceName(candidate.res)) << ",\n";
    out << "      \"earliest_start\": "
        << jsonString(dateString(candidate.earliestStart)) << ",\n";
    out << "      \"all_resources\": [";
    for (size_t r = 0; r < candidate.allResources.size(); ++r) {
      if (r) out << ", ";
      out << jsonString(resourceName(candidate.allResources[r]));
    }
    out << "]\n";
    out << "    }";
    if (idx + 1 != rawCandidates.size()) out << ",";
    out << "\n";
  }
  out << "  ],\n";
  out << "  \"ga_jobs\": [\n";
  for (size_t jobIdx = 0; jobIdx < input.jobs.size(); ++jobIdx) {
    const auto& job = input.jobs[jobIdx];
    out << "    {\n";
    out << "      \"job_index\": " << job.jobIndex << ",\n";
    out << "      \"top_owner\": " << jsonString(operationName(job.topOwner)) << ",\n";
    out << "      \"operations\": [\n";
    for (size_t pos = 0; pos < job.operationIndices.size(); ++pos) {
      const auto& operation = input.operations[job.operationIndices[pos]];
      out << "        {\n";
      out << "          \"sequence\": " << operation.operationIndex << ",\n";
      out << "          \"gene_id\": " << operation.geneId << ",\n";
      out << "          \"reference\": " << jsonString(opReference(operation.op)) << ",\n";
      out << "          \"operation\": " << jsonString(operationName(operation.op)) << ",\n";
      out << "          \"option_count\": " << operation.options.size() << ",\n";
      out << "          \"options\": [\n";
      for (size_t optIdx = 0; optIdx < operation.options.size(); ++optIdx) {
        const auto& option = operation.options[optIdx];
        out << "            {\n";
        out << "              \"option_index\": " << optIdx << ",\n";
        out << "              \"machine_index\": " << option.machineIndex << ",\n";
        out << "              \"selected_operation\": "
            << jsonString(operationDefinitionName(option.selectedOperation)) << ",\n";
        out << "              \"primary_resource\": "
            << jsonString(resourceName(option.primaryResource)) << ",\n";
        out << "              \"earliest_start\": "
            << jsonString(dateString(option.earliestStart)) << ",\n";
        out << "              \"duration_seconds\": "
            << option.estimatedDuration.getSeconds() << ",\n";
        out << "              \"all_resources\": [";
        for (size_t r = 0; r < option.allResources.size(); ++r) {
          if (r) out << ", ";
          out << jsonString(resourceName(option.allResources[r]));
        }
        out << "]\n";
        out << "            }";
        if (optIdx + 1 != operation.options.size()) out << ",";
        out << "\n";
      }
      out << "          ]\n";
      out << "        }";
      if (pos + 1 != job.operationIndices.size()) out << ",";
      out << "\n";
    }
    out << "      ]\n";
    out << "    }";
    if (jobIdx + 1 != input.jobs.size()) out << ",";
    out << "\n";
  }
  out << "  ]\n";
  out << "}\n";
}

bool isSplitRelatedOperation(const Operation* operation) {
  if (!operation) return false;
  if (operation->hasType<OperationSplit>()) return true;
  for (auto& candidateParent : Operation::all()) {
    if (!candidateParent.hasType<OperationSplit>()) continue;
    for (auto& sub : candidateParent.getSubOperations())
      if (sub->getOperation() == operation) return true;
  }
  return false;
}

bool isGAEligibleOperationPlan(const OperationPlan* op) {
  if (!op || !op->getOperation() || op->getQuantity() <= 0.0) return false;
  if (op->getOperation()->hasType<OperationItemSupplier,
                                   OperationItemDistribution>())
    return false;
  if (isSplitRelatedOperation(op->getOperation())) return false;
  return true;
}

// A plan can be part of the rolling OS while its status makes it immutable.
// Keeping it in the chromosome gives the decoder an explicit locked prefix;
// only its own dates/resources are fixed during decoding and writeback.
bool isGALockedOperationPlan(const OperationPlan* op) {
  if (!op || !isGAEligibleOperationPlan(op)) return false;
  // Match MRP's movement semantics: confirmed/completed/closed plans are
  // immutable; approved plans are mutable only when MRP itself is allowed to
  // move them early.
  return op->getConfirmed() || op->getCompleted() || op->getClosed() ||
      (op->getApproved() && !Plan::instance().getMoveApprovedEarly());
}

static void collectQualifiedResourcesLocal(const Resource* res, Skill* skill,
                                           Date start, Date end,
                                           vector<const Resource*>& result) {
  if (!res || !res->getConstrained()) return;
  if (res->isGroup()) {
    for (auto m = res->getMembers(); m != Resource::end(); ++m)
      collectQualifiedResourcesLocal(&*m, skill, start, end, result);
    return;
  }
  if (skill && !res->hasSkill(skill, start, end)) return;
  if (find(result.begin(), result.end(), res) == result.end())
    result.push_back(res);
}

bool operationUsesPrimaryResource(
    const Operation* operation, const Resource* resource) {
  if (!operation || !resource) return false;
  for (const auto& ld : operation->getLoads()) {
    vector<const Resource*> alternatives;
    collectQualifiedResourcesLocal(
        ld.getResource(), ld.getSkill(),
        Plan::instance().getCurrent(), Plan::instance().getCurrent(),
        alternatives);
    if (find(alternatives.begin(), alternatives.end(), resource) !=
        alternatives.end())
      return true;
  }
  return false;
}

vector<Operation*> collectAlternateLeafOperations(OperationPlan* op) {
  vector<Operation*> result;
  if (!op || !op->getOperation()) return result;

  auto appendUnique = [&](Operation* candidate) {
    if (!candidate) return;
    if (find(result.begin(), result.end(), candidate) == result.end())
      result.push_back(candidate);
  };

  appendUnique(op->getOperation());
  for (auto alt = op->getAlternates(); Operation* candidate = alt.next();)
    appendUnique(candidate);

  sort(result.begin(), result.end(), [](Operation* a, Operation* b) {
    if (!a || !b) return a < b;
    return a->getName() < b->getName();
  });
  return result;
}

int parseCCPLStage(const OperationPlan* op) {
  if (!op || !op->getOperation()) return -1;
  const string& name = op->getOperation()->getName();
  const size_t marker = name.rfind("OP-");
  if (marker == string::npos || marker + 3 >= name.size()) return -1;
  char* end = nullptr;
  const long stage = strtol(name.c_str() + marker + 3, &end, 10);
  if (end == name.c_str() + marker + 3 || stage <= 0 || stage > 999)
    return -1;
  return static_cast<int>(stage);
}

// A CCPL production line is identified by the item on an open customer
// demand, not by assuming that a particular OP number is the final step.
// All preceding produced items are WIP and are linked by their consuming
// flows. The OP-n suffix remains only a stable ordering tie-breaker for the
// current CCPL import convention.
unordered_set<const Item*> demandedFinishedItems() {
  unordered_set<const Item*> result;
  for (auto& demand : Demand::all()) {
    if (!demand.getItem() || demand.getQuantity() <= ROUNDING_ERROR) continue;
    if (demand.getStatus() == Demand::STATUS_CLOSED ||
        demand.getStatus() == Demand::STATUS_CANCELED)
      continue;
    result.insert(demand.getItem());
  }
  return result;
}

bool isDemandFinishedOperation(const OperationPlan* op,
                               const unordered_set<const Item*>& finished) {
  return op && op->getOperation() && op->getOperation()->getItem() &&
      finished.count(op->getOperation()->getItem());
}

size_t computeOperationOrderWithinJob(OperationPlan* op, OperationPlan* topOwner) {
  if (!op) return 0;
  if (!topOwner || topOwner == op) return 0;

  // Prefer the static operation definition. Runtime operationplan hierarchies
  // can be flattened for single-candidate alternates, or reordered after moves.
  const Operation* operation = op->getOperation();
  while (operation && operation->getOwner() &&
         operation->getOwner() != topOwner->getOperation()) {
    operation = operation->getOwner();
  }
  if (operation && operation->getOwner() == topOwner->getOperation() &&
      operation->getPriority() >= 1)
    return static_cast<size_t>(operation->getPriority() - 1);

  // Nested structures such as routing -> alternate -> machine-specific leaf
  // need to be mapped back to the direct child of the routing top owner.
  OperationPlan* directChild = op;
  while (directChild && directChild->getOwner() &&
         directChild->getOwner() != topOwner) {
    directChild = directChild->getOwner();
  }
  if (!directChild || directChild->getOwner() != topOwner)
    return numeric_limits<size_t>::max() / 2;

  size_t order = 0;
  for (auto sub = topOwner->getSubOperationPlans();
       sub != OperationPlan::end(); ++sub) {
    if (&*sub == directChild) return order;
    ++order;
  }
  return numeric_limits<size_t>::max() / 2;
}

GAStandardInput buildGAStandardInput(
    const vector<const Resource*>& resources,
    const vector<CandidateOp>& rawCandidates,
    const unordered_map<const Resource*, Date>& resourceTimes) {
  GAStandardInput input;
  input.resourceTimes = resourceTimes;
  unordered_map<string, const Resource*> resourceByName;

  auto addResource = [&](const Resource* resource) {
    if (!resource) return;
    const string name = resource->getName();
    if (name.empty()) return;
    resourceByName.emplace(name, resource);
  };

  for (const auto* resource : resources) addResource(resource);
  for (const auto& raw : rawCandidates) {
    addResource(raw.res);
    for (const auto* resource : raw.allResources) addResource(resource);
  }

  vector<string> machineNames;
  machineNames.reserve(resourceByName.size());
  for (const auto& kv : resourceByName) machineNames.push_back(kv.first);
  sort(machineNames.begin(), machineNames.end());

  input.resources.reserve(machineNames.size());
  for (size_t idx = 0; idx < machineNames.size(); ++idx) {
    const string& name = machineNames[idx];
    const Resource* resource = resourceByName[name];
    input.resources.push_back(resource);
    input.machineIndexByResource[resource] = static_cast<int>(idx);
    input.machineIndexByName.emplace(name, static_cast<int>(idx));
  }

  unordered_map<OperationPlan*, size_t> operationIndex;
  int nextGeneId = 0;
  for (const auto& raw : rawCandidates) {
    if (!raw.op) continue;

    size_t opIndex = 0;
    auto it = operationIndex.find(raw.op);
    if (it == operationIndex.end()) {
      opIndex = input.operations.size();
      operationIndex.emplace(raw.op, opIndex);

      GAInputOperation operation;
      operation.geneId = nextGeneId++;
      operation.op = raw.op;
      operation.topOwner = raw.op->getTopOwner();
      operation.demand = raw.op->getTopOwner()
          ? raw.op->getTopOwner()->getDemand()
          : nullptr;
      operation.priority = operation.demand
          ? operation.demand->getPriority()
          : 999;
      operation.due = operation.demand
          ? operation.demand->getDue()
          : Date::infiniteFuture;
      input.operations.push_back(std::move(operation));
    } else {
      opIndex = it->second;
    }

    GAInputOption option;
    auto machineIt = input.machineIndexByResource.find(raw.res);
    if (machineIt != input.machineIndexByResource.end()) {
      option.machineIndex = machineIt->second;
    } else {
      const string name = resourceName(raw.res);
      auto nameIt = input.machineIndexByName.find(name);
      option.machineIndex =
          nameIt != input.machineIndexByName.end() ? nameIt->second : -1;
    }
    if (option.machineIndex < 0) {
      for (const Resource* candidateResource : raw.allResources) {
        auto candidateIt = input.machineIndexByResource.find(candidateResource);
        if (candidateIt != input.machineIndexByResource.end()) {
          option.machineIndex = candidateIt->second;
          break;
        }
      }
    }
    // A chromosome option without a machine index would later address
    // machine[-1]. Ignore such a malformed candidate instead of crashing the
    // complete GA run; another qualified resource option can still survive.
    if (option.machineIndex < 0 ||
        option.machineIndex >= static_cast<int>(input.resources.size()))
      continue;
    // A fixed CCPL stage has no process alternate. Calling setOperation on
    // it during a chromosome trial rebuilds its flowplans and can invoke the
    // generic proportional supply split, even when the selected operation is
    // later restored.  Keep this line strictly read-only during GA decoding.
    const bool fixedCCPLStage = parseCCPLStage(raw.op) > 0;
    option.selectedOperation = fixedCCPLStage && raw.op
        ? raw.op->getOperation() : raw.selectedOperation;
    option.primaryResource = raw.res;
    option.allResources = raw.allResources;
    option.loadAssignments = raw.loadAssignments;
    option.earliestStart = raw.earliestStart;
    const Resource* durationResource =
        raw.res ? raw.res
                : (raw.allResources.empty() ? nullptr : raw.allResources.front());
    OperationPlanState originalState(raw.op);
    if (!fixedCCPLStage && raw.selectedOperation &&
        raw.op->getOperation() != raw.selectedOperation)
      raw.op->setOperation(raw.selectedOperation);
    option.estimatedDuration =
        estimateOperationDuration(raw.op, durationResource);
    raw.op->restore(originalState);
    input.operations[opIndex].options.push_back(std::move(option));

    CandidateOp normalized = raw;
    normalized.candId = input.operations[opIndex].geneId;
    input.candidates.push_back(std::move(normalized));
  }

  // The GA chromosome owns the movable dates.  Never anchor a CCPL flow line
  // to the MRP snapshot's first operation start: that would silently freeze
  // every decoded job at the MRP date (eg. 2026-08-06).  Use plan.current as
  // the lower bound; the real resource calendar then advances each operation
  // to its first permitted interval (eg. 2026-08-01 08:00).
  input.schedulingAnchor = Plan::instance().getCurrent();

  // Build jobs from material-pegging connected components. For the standard
  // CCPL line naming convention we can reconstruct batch pipelines much more
  // efficiently by aligning OP-01..OP-N plans in stable date/reference order.
  bool ccplLine = !input.operations.empty();
  map<int, vector<size_t>> ccplStages;
  for (size_t idx = 0; idx < input.operations.size(); ++idx) {
    const int stage = parseCCPLStage(input.operations[idx].op);
    if (stage < 1) {
      ccplLine = false;
      break;
    }
    ccplStages[stage].push_back(idx);
  }
  vector<size_t> parent(input.operations.size());
  iota(parent.begin(), parent.end(), 0);
  function<size_t(size_t)> findRoot = [&](size_t x) -> size_t {
    return parent[x] == x ? x : parent[x] = findRoot(parent[x]);
  };
  auto unite = [&](size_t a, size_t b) {
    a = findRoot(a);
    b = findRoot(b);
    if (a != b) parent[b] = a;
  };
  if (ccplLine && ccplStages.size() > 1) {
    for (auto& entry : ccplStages)
      stable_sort(entry.second.begin(), entry.second.end(), [&](size_t a, size_t b) {
        OperationPlan* left = input.operations[a].op;
        OperationPlan* right = input.operations[b].op;
        if (left->getStart() != right->getStart())
          return left->getStart() < right->getStart();
        return left->getReference() < right->getReference();
      });
    // Stage plan counts and quantities can differ. Positional pairing would
    // invent dependencies. Keep each MO as its own GA node and let the
    // cumulative material ledger establish the real quantity dependency.
  } else {
    // A standard routing is the natural GA job. The earlier implementation
    // relied only on material pegging to connect its leaf plans. Jobshop
    // routings commonly have no intermediate material flow, which split a
    // 12-step routing into twelve independent one-operation jobs and made an
    // OS chromosome unable to represent its true precedence. Group every
    // executable leaf of the same top routing first; material pegging then
    // additionally connects shared supply when needed.
    unordered_map<OperationPlan*, size_t> firstByTopOwner;
    for (size_t operationIdx = 0; operationIdx < input.operations.size();
         ++operationIdx) {
      OperationPlan* topOwner = input.operations[operationIdx].topOwner;
      if (!topOwner) continue;
      const auto found = firstByTopOwner.emplace(topOwner, operationIdx);
      if (!found.second) unite(operationIdx, found.first->second);
    }
    for (size_t consumerIdx = 0; consumerIdx < input.operations.size();
         ++consumerIdx) {
      OperationPlan* consumer = input.operations[consumerIdx].op;
      if (!consumer) continue;
      for (auto flow = consumer->beginFlowPlans();
           flow != consumer->endFlowPlans(); ++flow) {
        if (flow->getQuantity() >= -ROUNDING_ERROR) continue;
        for (PeggingIterator peg(&*flow, false); peg; --peg) {
          auto producerIt = operationIndex.find(peg.getOperationPlan());
          if (producerIt != operationIndex.end() &&
              producerIt->second != consumerIdx) {
            unite(consumerIdx, producerIt->second);
            break;
          }
        }
      }
    }
  }
  unordered_map<size_t, int> jobByRoot;
  for (size_t opIdx = 0; opIdx < input.operations.size(); ++opIdx) {
    const size_t root = findRoot(opIdx);
    auto inserted = jobByRoot.emplace(root, static_cast<int>(input.jobs.size()));
    if (inserted.second) {
      GAInputJob job;
      job.jobIndex = inserted.first->second;
      job.topOwner = input.operations[opIdx].topOwner;
      job.demand = input.operations[opIdx].demand;
      input.jobs.push_back(std::move(job));
    }
    input.operations[opIdx].jobIndex = inserted.first->second;
  }

  for (size_t opIdx = 0; opIdx < input.operations.size(); ++opIdx)
    if (input.operations[opIdx].jobIndex >= 0)
      input.jobs[static_cast<size_t>(input.operations[opIdx].jobIndex)]
          .operationIndices.push_back(opIdx);

  for (auto& job : input.jobs) {
    stable_sort(job.operationIndices.begin(), job.operationIndices.end(),
                [&](size_t lhs, size_t rhs) {
                  const auto& left = input.operations[lhs];
                  const auto& right = input.operations[rhs];
                  const int leftStage = parseCCPLStage(left.op);
                  const int rightStage = parseCCPLStage(right.op);
                  if (leftStage > 0 && rightStage > 0 &&
                      leftStage != rightStage)
                    return leftStage < rightStage;
                  size_t leftOrder =
                      computeOperationOrderWithinJob(left.op, job.topOwner);
                  size_t rightOrder =
                      computeOperationOrderWithinJob(right.op, job.topOwner);
                  if (leftOrder != rightOrder) return leftOrder < rightOrder;
                  return left.geneId < right.geneId;
                });

    for (size_t seq = 0; seq < job.operationIndices.size(); ++seq)
      input.operations[job.operationIndices[seq]].operationIndex =
          static_cast<int>(seq);
  }

  input.jobsNum = static_cast<int>(input.jobs.size());
  input.machinesNum = static_cast<int>(input.resources.size());
  input.jobsOperations.assign(input.jobsNum, 0);
  for (const auto& job : input.jobs) {
    if (job.jobIndex >= 0)
      input.jobsOperations[static_cast<size_t>(job.jobIndex)] =
          static_cast<int>(job.operationIndices.size());
    input.maxOperations = max(
        input.maxOperations,
        static_cast<int>(job.operationIndices.size()));
  }

  for (const auto& operation : input.operations)
    input.maxCandidateOptions = max(
        input.maxCandidateOptions,
        static_cast<int>(operation.options.size()));

  input.geneByJobOperation.assign(
      input.jobsNum, vector<int>(input.maxOperations, -1));
  input.operationIndexByJobOperation.assign(
      input.jobsNum, vector<int>(input.maxOperations, -1));
  input.operationPlanByJobOperation.assign(
      input.jobsNum, vector<OperationPlan*>(input.maxOperations, nullptr));
  input.candidateMachineCount.assign(
      input.jobsNum, vector<int>(input.maxOperations, 0));
  input.candidateMachines.assign(
      input.jobsNum,
      vector<vector<int>>(input.maxOperations,
                          vector<int>(input.maxCandidateOptions, -1)));
  input.candidateMachineDurations.assign(
      input.jobsNum,
      vector<vector<int>>(input.maxOperations,
                          vector<int>(input.maxCandidateOptions, 0)));

  for (const auto& operation : input.operations) {
    if (operation.jobIndex < 0 || operation.operationIndex < 0) continue;
    const size_t jobIdx = static_cast<size_t>(operation.jobIndex);
    const size_t opIdx = static_cast<size_t>(operation.operationIndex);
    input.geneByJobOperation[jobIdx][opIdx] = operation.geneId;
    input.operationIndexByJobOperation[jobIdx][opIdx] =
        static_cast<int>(&operation - input.operations.data());
    input.operationPlanByJobOperation[jobIdx][opIdx] = operation.op;
    input.candidateMachineCount[jobIdx][opIdx] =
        static_cast<int>(operation.options.size());

    for (size_t optionIdx = 0; optionIdx < operation.options.size(); ++optionIdx) {
      const auto& option = operation.options[optionIdx];
      input.candidateMachines[jobIdx][opIdx][optionIdx] = option.machineIndex;
      long seconds = option.estimatedDuration.getSeconds();
      if (seconds < 0) seconds = 0;
      if (seconds > numeric_limits<int>::max())
        seconds = numeric_limits<int>::max();
      input.candidateMachineDurations[jobIdx][opIdx][optionIdx] =
          static_cast<int>(seconds);
    }
  }

  dumpGAInputIfRequested(rawCandidates, input);
  return input;
}

constexpr long long kGAInfTime = 2147483640LL;

// Fixed normalization baseline for the multi-objective fitness.
// Computed once from the initial population at the start of runGA and kept
// constant for the whole run so that scores stay comparable across generations.
struct GANormBaseline {
  double mkMin = 0.0, mkMax = 1.0;  // makespan range
  double cvMin = 0.0, cvMax = 1.0;  // load-balance CV range
  double tdMin = 0.0, tdMax = 1.0;  // weighted tardiness range
};

// Weights of the three objectives in the scalarized fitness.
// fitness = wMakespan*norm(makespan) + wBalance*norm(CV) + wTardiness*norm(tardiness)
struct GAObjectiveWeights {
  double wMakespan = 0.6;
  double wBalance = 0.1;
  double wTardiness = 0.3;
};

struct GAData {
  struct MaterialFlow {
    const Buffer* buffer = nullptr;
    double quantity = 0.0;
    // Positive when the operation material is a transfer batch. Zero means
    // the whole quantity is consumed at the start or produced at the end.
    double transferBatch = 0.0;
  };
  struct FixedMaterialEvent {
    const Buffer* buffer = nullptr;
    long long time = 0;
    double quantity = 0.0;
  };
  // Movable WIP distribution orders belong to the same forward material
  // ledger as their producing operations.  Keeping their exact batch sizes
  // here avoids treating every source receipt as an unlimited receipt at all
  // destination warehouses.
  struct DistributionLink {
    const Buffer* source = nullptr;
    const Buffer* destination = nullptr;
    double quantity = 0.0;
    Duration transit;
    long long requested = 0;
    string reference;
  };
  int jobsNum = 0;
  int machinesNum = 0;
  int maxMachinePerOperation = 0;
  vector<int> jobsOperations;
  vector<vector<vector<int>>> jobsOperationsDetail;
  vector<vector<vector<int>>> candidateMachine;
  vector<vector<int>> candidateMachineCount;
  vector<vector<vector<int>>> candidateMachineTime;
  bool overlapEnabled = false;
  int overlapBatchCount = 1;
  vector<vector<int>> transferTime;
  // Multi-objective data (jobDueSeconds/jobWeight filled by buildGAData;
  // normBaseline filled once at the start of runGA).
  vector<long long> jobDueSeconds;  // per-job due date as seconds from anchor
  vector<double> jobWeight;         // per-job priority weight (higher=more urgent)
  GANormBaseline normBaseline;
  GAObjectiveWeights objWeights;
  bool materialPrecheck = false;
  // Stage 1 still uses the fast machine search. Internal WIP is enforced on
  // that same decode whenever the operation has a material flow. External
  // purchase and distribution shortages stay in the postcheck after writeback.
  bool fastDecode = true;
  // False when no scheduled resource has a setup matrix. The decoder then
  // keeps the original machine packing and does not inspect setups.
  bool changeoverEnabled = false;
  // Buffers produced and consumed inside the GA manufacturing network are
  // always quantity-constrained, including in the default postcheck mode.
  unordered_set<const Buffer*> internalMaterialBuffers;
  // Buffer object identity can differ after a supply-network rebuild even
  // when the business item is the same WIP at the same location. Keep the
  // item key too, so the decoder doesn't silently drop a regenerated WIP
  // FlowPlan from its FIFO ledger.
  unordered_set<string> internalMaterialItems;
  vector<vector<vector<MaterialFlow>>> operationFlows;
  vector<vector<int>> operationStage;
  vector<FixedMaterialEvent> fixedMaterialEvents;
  // Non-GA plans (setup, firm WO, etc.) reserved once in buildGAData so each
  // chromosome decode does not re-walk the global OperationPlan list.
  struct FixedReservation {
    const Resource* resource = nullptr;
    Date start;
    Date end;
    double quantity = 1.0;
    PooledString setup;
  };
  vector<FixedReservation> fixedReservations;
  vector<DistributionLink> proposedDistributions;
  // Global OS prefix reserved for immutable operationplans.  The suffix is
  // the normal mutable GA dispatch sequence.
  vector<int> lockedOSPrefix;
  unordered_map<const Buffer*, double> initialOnhand;
  // Read-only model references used by the decoder to turn an OS/MS
  // chromosome into real calendar dates.  No date is corrected afterwards
  // during solution writeback.
  Date schedulingAnchor = Date::infinitePast;
  vector<const Resource*> resources;
  vector<vector<const OperationPlan*>> operationPlans;
  vector<vector<vector<Operation*>>> candidateOperations;
  vector<vector<bool>> lockedOperation;
  int lockedOSPrefixLength = 0;

  int maxOperations() const {
    return jobsOperations.empty()
        ? 0 : *max_element(jobsOperations.begin(), jobsOperations.end());
  }

  int totalOperations() const {
    return accumulate(jobsOperations.begin(), jobsOperations.end(), 0);
  }
};

struct GAScheduleInterval {
  int begin = 0;
  int end = 0;
  int job = 0;
  int operation = 0;
};

struct GADecodedSchedule {
  vector<vector<int>> selectedMachine;
  vector<vector<int>> selectedMachineTime;
  vector<vector<int>> selectedOption;
  vector<vector<int>> begin;
  vector<vector<int>> end;
  vector<vector<Date>> calendarBegin;
  vector<vector<Date>> calendarEnd;
  // Start of the changeover that precedes processing. Equals calendarBegin
  // when the resource has no setup to perform.
  vector<vector<Date>> calendarSetupBegin;
  vector<vector<GAScheduleInterval>> machineIntervals;
  long long makespan = 0;
  // Objective makespan: only the movable suffix. Locked operations are hard
  // reservations, not an optimization result, and mustn't flatten the
  // makespan ranking merely because one fixed order ends far in the future.
  long long movableMakespan = 0;
  // Aggregated multi-objective signals (filled in gaDecodeOne).
  vector<long long> machineLoad;      // total busy seconds per machine
  vector<long long> jobCompletion;    // completion time (seconds) per job
  bool materialFeasible = true;
  unsigned int materialShortageCount = 0;

  void init(const GAData& data) {
    const int maxOps = data.maxOperations();
    selectedMachine.assign(data.jobsNum, vector<int>(maxOps, 0));
    selectedMachineTime.assign(data.jobsNum, vector<int>(maxOps, 0));
    selectedOption.assign(data.jobsNum, vector<int>(maxOps, 0));
    begin.assign(data.jobsNum, vector<int>(maxOps, -1));
    end.assign(data.jobsNum, vector<int>(maxOps, -1));
    calendarBegin.assign(data.jobsNum,
                         vector<Date>(maxOps, Date::infinitePast));
    calendarEnd.assign(data.jobsNum,
                       vector<Date>(maxOps, Date::infinitePast));
    calendarSetupBegin.assign(data.jobsNum,
                              vector<Date>(maxOps, Date::infinitePast));
    machineIntervals.assign(data.machinesNum, {});
    for (auto& intervals : machineIntervals)
      intervals.reserve(data.totalOperations());
    machineLoad.assign(data.machinesNum, 0);
    jobCompletion.assign(data.jobsNum, 0);
    makespan = 0;
    movableMakespan = 0;
    materialFeasible = true;
    materialShortageCount = 0;
  }
};

struct GASolverConfig {
  int populationSize = 500;
  int maxStep = 50;
  int maxNoNewBest = 10;
  int tournamentM = 4;
  double memorySize = 0.2;
  double crossover = 1.0;
  double mutationScale = 0.15;
  double vnsRatio = 0.02;
  // Secondary stop (seconds). 0 disables time-based stopping.
  double timeLimitSeconds = 60.0;
  bool useMemoryTournament = true;
  bool useRandomTournament = false;
  bool useUniformMSCrossover = true;
  bool usePoxOSCrossover = true;
  bool useRandomOSCrossover = false;
  bool useRandomMSMutation = true;
  bool useBestMSMutation = false;
  bool useRandomOSMutation = true;
  bool useAutoCrossover = true;
  bool enableVNS = true;
};

struct GARunResult {
  vector<int> bestMS;
  vector<int> bestOS;
  GADecodedSchedule schedule;
  double bestScore = numeric_limits<double>::max();
  double bestMakespan = 0.0;    // raw makespan of the best individual (diagnostic)
  double bestCV = 0.0;          // load-balance CV of the best individual (diagnostic)
  double bestTardiness = 0.0;   // weighted tardiness of the best individual (diagnostic)
  int bestStep = -1;
  int stepsRun = 0;
  // "generations" | "stagnation" | "time_limit"
  string stopReason = "generations";
  double runtimeSeconds = 0.0;
};

struct RelativeTimeWindow {
  long startSeconds = 0L;
  long endSeconds = 0L;
  Date start = Date::infinitePast;
  Date end = Date::infinitePast;
};

struct ParsedLabel {
  bool matched = false;
  int job = -1;
  int operation = -1;
  int machine = -1;
};

RelativeTimeWindow mapRelativeWindow(const Date& anchor, int begin, int end) {
  RelativeTimeWindow mapped;
  mapped.startSeconds = static_cast<long>(begin);
  mapped.endSeconds = static_cast<long>(end);
  mapped.start = anchor + Duration(mapped.startSeconds);
  mapped.end = anchor + Duration(mapped.endSeconds);
  return mapped;
}

ParsedLabel parseLabel(const string& name) {
  ParsedLabel parsed;
  if (name.size() < 11 || name[0] != 'J') return parsed;

  const size_t oPos = name.find("_O");
  const size_t mPos = name.find("_M");
  if (oPos == string::npos || mPos == string::npos || oPos <= 1 || mPos <= oPos + 2)
    return parsed;

  auto parsePart = [&](size_t begin, size_t end, int& target) -> bool {
    if (begin >= end) return false;
    int value = 0;
    for (size_t idx = begin; idx < end; ++idx) {
      const char ch = name[idx];
      if (ch < '0' || ch > '9') return false;
      value = value * 10 + (ch - '0');
    }
    target = value;
    return true;
  };

  if (!parsePart(1, oPos, parsed.job)) return parsed;
  if (!parsePart(oPos + 2, mPos, parsed.operation)) return parsed;
  if (!parsePart(mPos + 2, name.size(), parsed.machine)) return parsed;
  parsed.matched = true;
  return parsed;
}

struct GARNG {
  mt19937 eng;

  explicit GARNG(uint64_t seed)
      : eng(static_cast<mt19937::result_type>(seed)) {}

  int randint(int lowInclusive, int highExclusive) {
    if (highExclusive <= lowInclusive) return lowInclusive;
    uniform_int_distribution<int> dist(lowInclusive, highExclusive - 1);
    return dist(eng);
  }

  double rand01() {
    uniform_real_distribution<double> dist(0.0, 1.0);
    return dist(eng);
  }

  template <class T>
  void shuffle(vector<T>& values) {
    std::shuffle(values.begin(), values.end(), eng);
  }
};

vector<int> gaSampleWithReplacementUnique(
    GARNG& rng, int highExclusive, int sampleCount) {
  vector<int> sampled;
  sampled.reserve(max(1, sampleCount));
  for (int i = 0; i < sampleCount; ++i)
    sampled.push_back(rng.randint(0, highExclusive));
  sort(sampled.begin(), sampled.end());
  sampled.erase(unique(sampled.begin(), sampled.end()), sampled.end());
  return sampled;
}

GAData buildGAData(
    const GAStandardInput& input, const GAConfig& config) {
  GAData data;
  data.schedulingAnchor = input.schedulingAnchor;
  data.resources = input.resources;
  data.jobsNum = input.jobsNum;
  data.machinesNum = input.machinesNum;
  data.maxMachinePerOperation = input.maxCandidateOptions;
  data.jobsOperations = input.jobsOperations;
  data.overlapEnabled = config.overlap_enabled;
  data.overlapBatchCount = max(1, config.overlap_batch_count);
  data.objWeights.wMakespan = config.objective_makespan_weight;
  data.objWeights.wBalance = config.objective_balance_weight;
  data.objWeights.wTardiness = config.objective_tardiness_weight;
  const double totalObjectiveWeight = data.objWeights.wMakespan +
      data.objWeights.wBalance + data.objWeights.wTardiness;
  if (totalObjectiveWeight > ROUNDING_ERROR) {
    data.objWeights.wMakespan /= totalObjectiveWeight;
    data.objWeights.wBalance /= totalObjectiveWeight;
    data.objWeights.wTardiness /= totalObjectiveWeight;
  } else {
    data.objWeights = GAObjectiveWeights();
  }
  const int maxOps = data.maxOperations();
  data.operationPlans.assign(data.jobsNum,
      vector<const OperationPlan*>(maxOps, nullptr));
  data.lockedOperation.assign(data.jobsNum, vector<bool>(maxOps, false));
  for (const auto& operation : input.operations)
    if (operation.jobIndex >= 0 && operation.operationIndex >= 0) {
      data.operationPlans[operation.jobIndex][operation.operationIndex] =
          operation.op;
      data.lockedOperation[operation.jobIndex][operation.operationIndex] =
          isGALockedOperationPlan(operation.op);
    }

  data.jobsOperationsDetail.assign(
      data.jobsNum,
      vector<vector<int>>(maxOps, vector<int>(data.machinesNum, 0)));
  data.candidateMachine.assign(
      data.jobsNum,
      vector<vector<int>>(maxOps, vector<int>(data.maxMachinePerOperation, 0)));
  data.candidateMachineCount.assign(
      data.jobsNum, vector<int>(maxOps, 0));
  data.candidateMachineTime.assign(
      data.jobsNum,
      vector<vector<int>>(maxOps, vector<int>(data.maxMachinePerOperation, 0)));
  data.candidateOperations.assign(
      data.jobsNum, vector<vector<Operation*>>(
                        maxOps, vector<Operation*>(data.maxMachinePerOperation,
                                                   nullptr)));
  data.transferTime.assign(
      data.jobsNum, vector<int>(maxOps, 0));

  for (int job = 0; job < data.jobsNum; ++job) {
    for (int op = 0; op < data.jobsOperations[job]; ++op) {
      const int count = input.candidateMachineCount[job][op];
      data.candidateMachineCount[job][op] = count;
      for (int idx = 0; idx < count; ++idx) {
        const int machine = input.candidateMachines[job][op][idx];
        const int duration = input.candidateMachineDurations[job][op][idx];
        data.candidateMachine[job][op][idx] = machine;
        data.candidateMachineTime[job][op][idx] = duration;
        const int operationIndex = input.operationIndexByJobOperation[job][op];
        if (operationIndex >= 0 &&
            operationIndex < static_cast<int>(input.operations.size()) &&
            idx < static_cast<int>(input.operations[operationIndex].options.size()))
          data.candidateOperations[job][op][idx] =
              input.operations[operationIndex].options[idx].selectedOperation;
        if (machine >= 0 && machine < data.machinesNum)
          data.jobsOperationsDetail[job][op][machine] = duration;
      }
    }
  }

  // Multi-objective: per-job due date (seconds from anchor) and priority weight.
  // frePPLe semantics: a lower Demand priority number means more urgent, so the
  // weight is inverted (maxPrio - prio + 1) to give urgent jobs a larger weight
  // in the weighted-tardiness objective.
  const Date anchor = input.schedulingAnchor;
  data.jobDueSeconds.assign(data.jobsNum, numeric_limits<long long>::max());
  data.jobWeight.assign(data.jobsNum, 1.0);
  int maxPrio = 0;
  for (int job = 0; job < data.jobsNum; ++job) {
    const Demand* dmd = input.jobs[job].demand;
    if (!dmd) continue;
    const int prio = dmd->getPriority();
    if (prio > maxPrio) maxPrio = prio;
    const Date due = dmd->getDue();
    if (due != Date::infiniteFuture)
      data.jobDueSeconds[job] =
          static_cast<long long>((due - anchor).getSeconds());
  }
  for (int job = 0; job < data.jobsNum; ++job) {
    const Demand* dmd = input.jobs[job].demand;
    const int prio = dmd ? dmd->getPriority() : maxPrio;
    data.jobWeight[job] = static_cast<double>(maxPrio - prio + 1);
  }

  data.materialPrecheck =
      config.material_check_mode == GAConfig::PRECHECK;
  data.operationFlows.assign(
      data.jobsNum, vector<vector<GAData::MaterialFlow>>(maxOps));
  data.operationStage.assign(data.jobsNum, vector<int>(maxOps, -1));
  unordered_set<const OperationPlan*> gaOperations;
  unordered_set<const Buffer*> producedByGA;
  int transferBatchFlows = 0;
  for (const auto& inOp : input.operations) {
    if (!inOp.op || inOp.jobIndex < 0 || inOp.operationIndex < 0) continue;
    gaOperations.insert(inOp.op);
    data.operationStage[inOp.jobIndex][inOp.operationIndex] =
        parseCCPLStage(inOp.op);
    auto& flows = data.operationFlows[inOp.jobIndex][inOp.operationIndex];
    unordered_map<const Flow*, size_t> flowIndex;
    for (auto fp = inOp.op->beginFlowPlans(); fp != inOp.op->endFlowPlans(); ++fp) {
      if (!fp->getBuffer() || fabs(fp->getQuantity()) <= ROUNDING_ERROR) continue;
      const Flow* flow = fp->getFlow();
      double transferBatch = 0.0;
      if (flow && flow->hasType<FlowTransferBatch>())
        transferBatch =
            static_cast<const FlowTransferBatch*>(flow)->getTransferBatch();
      const auto found = flow ? flowIndex.find(flow) : flowIndex.end();
      if (flow && found != flowIndex.end()) {
        flows[found->second].quantity += fp->getQuantity();
      } else {
        if (flow) flowIndex.emplace(flow, flows.size());
        flows.push_back({fp->getBuffer(), fp->getQuantity(), transferBatch});
        if (transferBatch > ROUNDING_ERROR) ++transferBatchFlows;
      }
      if (fp->getQuantity() > ROUNDING_ERROR) {
        producedByGA.insert(fp->getBuffer());
        if (fp->getBuffer()->getItem())
          data.internalMaterialItems.insert(fp->getBuffer()->getItem()->getName());
      }
    }
    if (getEnvBool("GA_WIP_TRACE") && parseCCPLStage(inOp.op) > 0) {
      logger << "GA WIP input " << inOp.op->getReference()
             << " stage=" << parseCCPLStage(inOp.op)
             << " flows=" << flows.size() << "\n";
      for (const auto& flow : flows)
        logger << "  " << (flow.buffer ? flow.buffer->getName() : string())
               << " qty=" << flow.quantity << "\n";
    }
  }
  logger << "GA transfer-batch flows: " << transferBatchFlows << "\n";
  int setupMatrixResources = 0;
  for (const Resource* resource : data.resources)
    if (resource && resource->getSetupMatrix()) ++setupMatrixResources;
  data.changeoverEnabled = setupMatrixResources > 0;
  logger << "GA setup-matrix resources: " << setupMatrixResources << "\n";
  // A manufacturing operation can produce WIP in one buffer while the next
  // operation consumes the same item from another location.  The connecting
  // proposed DO is part of the generated supply network, not external
  // material.  Include its destination buffer in the GA's internal ledger
  // as well, otherwise the decoder correctly sees the source production but
  // treats the downstream warehouse consumption as an unconstrained external
  // flow.  Follow chains (source -> destination -> destination) because a
  // multi-location route can contain more than one distribution leg.
  unordered_multimap<const Buffer*, const Buffer*> distributionLinks;
  for (auto plan = OperationPlan::begin(); plan != OperationPlan::end(); ++plan) {
    if (!plan->getOperation() || !plan->getProposed() ||
        !plan->getOperation()->hasType<OperationItemDistribution>())
      continue;
    // A proposal already due at the planning current date represents opening
    // stock in transit/at the receiving side.  It isn't a future transport
    // to be re-pegged to newly scheduled production. Moving it consumed that
    // initial WIP from the ledger and made the first downstream batch short.
    if (plan->getEnd() <= Plan::instance().getCurrent()) continue;
    const Buffer* source = nullptr;
    const Buffer* destination = nullptr;
    double quantity = 0.0;
    for (auto fp = plan->beginFlowPlans(); fp != plan->endFlowPlans(); ++fp) {
      if (!fp->getBuffer()) continue;
      if (fp->getQuantity() < -ROUNDING_ERROR) {
        source = fp->getBuffer();
        quantity = -fp->getQuantity();
      }
      else if (fp->getQuantity() > ROUNDING_ERROR) destination = fp->getBuffer();
    }
    if (source && destination) {
      data.proposedDistributions.push_back(
          {source, destination, quantity > ROUNDING_ERROR ? quantity : 0.0,
           plan->getEnd() - plan->getStart(),
           static_cast<long long>((plan->getEnd() - input.schedulingAnchor).getSeconds()),
           plan->getReference()});
      distributionLinks.emplace(source, destination);
    }
  }
  vector<const Buffer*> pendingBuffers(producedByGA.begin(), producedByGA.end());
  for (size_t pos = 0; pos < pendingBuffers.size(); ++pos) {
    const Buffer* source = pendingBuffers[pos];
    const auto range = distributionLinks.equal_range(source);
    for (auto link = range.first; link != range.second; ++link)
      if (producedByGA.insert(link->second).second)
        pendingBuffers.push_back(link->second);
  }
  data.internalMaterialBuffers = move(producedByGA);

  const auto isInternalMaterial = [&data](const Buffer* buffer) {
    return buffer && (data.internalMaterialBuffers.count(buffer) ||
        (buffer->getItem() &&
         data.internalMaterialItems.count(buffer->getItem()->getName())));
  };

  if (data.materialPrecheck || !data.internalMaterialItems.empty()) {
    const Date anchor = input.schedulingAnchor;
    unordered_set<const Buffer*> buffers;
    for (const auto& byJob : data.operationFlows)
      for (const auto& flows : byJob)
        for (const auto& flow : flows)
          if (flow.buffer &&
              (data.materialPrecheck ||
               isInternalMaterial(flow.buffer)))
            buffers.insert(flow.buffer);
    for (const Buffer* buffer : buffers) {
      data.initialOnhand[buffer] = buffer->getOnHand(anchor, false);
      for (auto it = buffer->getFlowPlans().begin();
           it != buffer->getFlowPlans().end(); ++it) {
        OperationPlan* owner = it->getOperationPlan();
        // A CCPL flow line is rescheduled as one coupled network.  Some
        // plans can be absent from the resource candidate iterator after an
        // MRP rollback, but their *old* MRP FlowPlans must never become fixed
        // receipts in the GA ledger: the decoder will generate their new
        // receipts itself.  Counting both made a later OP-03 batch appear
        // feasible in the chromosome, then short after GA writeback removed
        // the stale MRP timing.
        if (owner && (gaOperations.count(owner) || parseCCPLStage(owner) > 0))
          continue;
        // Proposed distribution orders are reconstructed by the forward
        // decoder from source production. Their old source/receipt events
        // would otherwise be counted twice and permit early downstream work.
        if (owner && owner->getProposed() && owner->getOperation() &&
            owner->getOperation()->hasType<OperationItemDistribution>())
          continue;
        if (it->getDate() <= anchor ||
            it->getDate() == Date::infiniteFuture ||
            fabs(it->getQuantity()) <= ROUNDING_ERROR)
          continue;
        data.fixedMaterialEvents.push_back(
            {buffer, static_cast<long long>((it->getDate() - anchor).getSeconds()),
             it->getQuantity()});
      }
    }
  }

  // Snapshot fixed resource loads once.  gaDecodeOne copies this list into its
  // per-chromosome shadow timeline instead of rescanning every OperationPlan.
  {
    unordered_set<const OperationPlan*> gaPlans;
    for (const auto& byJob : data.operationPlans)
      for (const auto* plan : byJob)
        if (plan) gaPlans.insert(plan);
    for (auto plan = OperationPlan::begin(); plan != OperationPlan::end(); ++plan) {
      if (!plan->getOperation() || gaPlans.count(&*plan) ||
          plan->getEnd() == Date::infiniteFuture)
        continue;
      unordered_set<const Resource*> seenResources;
      for (auto load = plan->beginLoadPlans(); load != plan->endLoadPlans(); ++load) {
        const Resource* resource = load->getResource();
        if (!resource || resource->isGroup() || !resource->getConstrained() ||
            !seenResources.insert(resource).second)
          continue;
        PooledString setup;
        if (load->getLoad()) setup = load->getLoad()->getSetup();
        data.fixedReservations.push_back(
            {resource, plan->getStart(), plan->getEnd(),
             fabs(load->getQuantity()), setup});
      }
    }
  }

  return data;
}

vector<vector<vector<int>>> gaInitialJobsOperationsDetail(
    const GAData& data) {
  auto detail = data.jobsOperationsDetail;
  for (int job = 0; job < data.jobsNum; ++job)
    for (int op = 0; op < data.jobsOperations[job]; ++op)
      for (int machine = 0; machine < data.machinesNum; ++machine)
        if (detail[job][op][machine] == 0)
          detail[job][op][machine] = static_cast<int>(kGAInfTime);
  return detail;
}

vector<vector<int>> gaBuildMSPositions(const GAData& data) {
  vector<vector<int>> positions(
      data.jobsNum, vector<int>(data.maxOperations(), -1));
  int position = 0;
  for (int job = 0; job < data.jobsNum; ++job)
    for (int op = 0; op < data.jobsOperations[job]; ++op)
      positions[job][op] = position++;
  return positions;
}

vector<vector<int>> gaBuildBestMS(
    const vector<vector<vector<int>>>& detailInf,
    const GAData& data) {
  vector<vector<int>> bestMS(
      data.jobsNum, vector<int>(data.maxOperations(), 0));
  for (int job = 0; job < data.jobsNum; ++job) {
    for (int op = 0; op < data.jobsOperations[job]; ++op) {
      int bestMachine = 0;
      int bestTime = detailInf[job][op][0];
      for (int machine = 1; machine < data.machinesNum; ++machine) {
        if (detailInf[job][op][machine] < bestTime) {
          bestTime = detailInf[job][op][machine];
          bestMachine = machine;
        }
      }
      int code = 0;
      for (int idx = 0; idx < data.candidateMachineCount[job][op]; ++idx)
        if (data.candidateMachine[job][op][idx] == bestMachine) {
          code = idx;
          break;
        }
      bestMS[job][op] = code;
    }
  }
  return bestMS;
}

int gaFindMachineCode(
    const GAData& data, int job, int op, int machine) {
  const int count = data.candidateMachineCount[job][op];
  for (int idx = 0; idx < count; ++idx)
    if (data.candidateMachine[job][op][idx] == machine) return idx;
  return 0;
}

void gaInitialMSGlobalSelection(
    vector<vector<int>>& ms, int firstIndex, int rearIndex,
    const vector<vector<int>>& msPositions, vector<int> jobsOrder,
    const GAData& data,
    const vector<vector<vector<int>>>& detailInf, GARNG& rng) {
  vector<long long> machineTime(data.machinesNum, 0);
  for (int position = 0; position < rearIndex - firstIndex; ++position) {
    const int msIndex = position + firstIndex;
    rng.shuffle(jobsOrder);
    fill(machineTime.begin(), machineTime.end(), 0);
    for (int job : jobsOrder) {
      for (int op = 0; op < data.jobsOperations[job]; ++op) {
        int bestMachine = 0;
        long long bestValue = numeric_limits<long long>::max();
        for (int machine = 0; machine < data.machinesNum; ++machine) {
          const int processTime = detailInf[job][op][machine];
          const long long score = machineTime[machine] + processTime;
          if (score < bestValue) {
            bestValue = score;
            bestMachine = machine;
          }
        }
        machineTime[bestMachine] += detailInf[job][op][bestMachine];
        ms[msIndex][msPositions[job][op]] =
            gaFindMachineCode(data, job, op, bestMachine);
      }
    }
  }
}

void gaInitialMSLocalSelection(
    vector<vector<int>>& ms, vector<int>& msTemplate,
    int firstIndex, int rearIndex, const GAData& data,
    const vector<vector<vector<int>>>& detailInf) {
  vector<long long> machineTime(data.machinesNum, 0);
  int msPosition = 0;
  for (int job = 0; job < data.jobsNum; ++job) {
    for (int op = 0; op < data.jobsOperations[job]; ++op) {
      int bestMachine = 0;
      long long bestValue = numeric_limits<long long>::max();
      for (int machine = 0; machine < data.machinesNum; ++machine) {
        const int processTime = detailInf[job][op][machine];
        const long long score = machineTime[machine] + processTime;
        if (score < bestValue) {
          bestValue = score;
          bestMachine = machine;
        }
      }
      machineTime[bestMachine] += detailInf[job][op][bestMachine];
      msTemplate[msPosition++] =
          gaFindMachineCode(data, job, op, bestMachine);
    }
    fill(machineTime.begin(), machineTime.end(), 0);
  }
  for (int idx = firstIndex; idx < rearIndex; ++idx) ms[idx] = msTemplate;
}

void gaInitialMSRandomSelection(
    vector<vector<int>>& ms, int firstIndex, int rearIndex,
    const GAData& data, GARNG& rng) {
  for (int position = 0; position < rearIndex - firstIndex; ++position) {
    const int idx = firstIndex + position;
    int msPosition = 0;
    for (int job = 0; job < data.jobsNum; ++job)
      for (int op = 0; op < data.jobsOperations[job]; ++op) {
        const int candidateCount = data.candidateMachineCount[job][op];
        ms[idx][msPosition++] = rng.randint(0, candidateCount);
      }
  }
}

void gaInitialOS(
    vector<vector<int>>& os, const GAData& data, GARNG& rng) {
  // Classic FJSP OS: job-id repetition (same as Numba / D:\test ga_comparison).
  // Locked operations, if any, form a chronological job-id prefix; the
  // remaining job occurrences are shuffled as the mutable suffix.
  struct Row { int job; Date start; Date end; string reference; };
  vector<Row> locked;
  vector<int> lockedCount(data.jobsNum, 0);
  for (int job = 0; job < data.jobsNum; ++job)
    for (int op = 0; op < data.jobsOperations[job]; ++op) {
      const OperationPlan* plan = data.operationPlans[job][op];
      if (data.lockedOperation[job][op] && plan) {
        locked.push_back({job, plan->getStart(), plan->getEnd(),
                          plan->getReference()});
        ++lockedCount[job];
      }
    }
  sort(locked.begin(), locked.end(), [](const Row& a, const Row& b) {
    if (a.start != b.start) return a.start < b.start;
    if (a.end != b.end) return a.end < b.end;
    return a.reference < b.reference;
  });
  const_cast<GAData&>(data).lockedOSPrefixLength = locked.size();

  vector<int> suffix;
  suffix.reserve(data.totalOperations() - locked.size());
  for (int job = 0; job < data.jobsNum; ++job) {
    const int remain = data.jobsOperations[job] - lockedCount[job];
    for (int i = 0; i < remain; ++i) suffix.push_back(job);
  }

  for (auto& chromosome : os) {
    vector<int> mutableSuffix = suffix;
    rng.shuffle(mutableSuffix);
    chromosome.clear();
    chromosome.reserve(locked.size() + mutableSuffix.size());
    for (const auto& row : locked) chromosome.push_back(row.job);
    chromosome.insert(
        chromosome.end(), mutableSuffix.begin(), mutableSuffix.end());
  }
}

// Expand job-repetition OS into explicit (job,op) genes used by the decoder's
// calendar / routing helpers. Occurrence k of job j becomes op k.
vector<int> gaExpandJobOSToGenes(const vector<int>& os, const GAData& data) {
  vector<int> nextOp(data.jobsNum, 0);
  vector<int> genes;
  genes.reserve(os.size());
  for (int job : os) {
    if (job < 0 || job >= data.jobsNum) continue;
    const int op = nextOp[job]++;
    if (op >= data.jobsOperations[job]) continue;
    genes.push_back(job * data.maxOperations() + op);
  }
  return genes;
}

int gaOperationGene(const GAData& data, int job, int operation) {
  return job * data.maxOperations() + operation;
}

// Position of the op-th occurrence of job in a job-repetition OS.
// Setup required by the load that places this operation on the resource.
// A pool load contributes its setup to every member machine.
PooledString gaLoadSetup(const Operation* operation, const Resource* resource) {
  if (!operation) return PooledString();
  const Load* fallback = nullptr;
  for (const Load& load : operation->getLoads()) {
    if (!load.getResource()) continue;
    if (!fallback) fallback = &load;
    if (resource &&
        (resource == load.getResource() ||
         resource->isMemberOf(load.getResource())))
      return load.getSetup();
  }
  return fallback ? fallback->getSetup() : PooledString();
}

// Seconds of changeover on this resource. No matrix, or the same setup on
// both sides, adds nothing. An undefined conversion uses the matrix rule
// for a disallowed changeover.
long gaChangeoverSeconds(const Resource* resource, const PooledString& from,
                         const PooledString& to) {
  if (!resource || from == to) return 0;
  if (from.empty() && to.empty()) return 0;
  SetupMatrix* matrix = resource->getSetupMatrix();
  if (!matrix) return 0;
  SetupMatrixRule* rule = matrix->calculateSetup(from, to);
  if (!rule) return 0;
  const double seconds = rule->getDuration().getSeconds();
  if (seconds <= 0.0) return 0;
  if (seconds > 366.0 * 86400.0) return 366L * 86400L;
  return static_cast<long>(seconds + 0.5);
}

Date gaAfterChangeover(const OperationPlan* operation, Date ready,
                       long setupSeconds) {
  if (setupSeconds <= 0 || !operation || !operation->getOperation() ||
      ready == Date::infinitePast || ready == Date::infiniteFuture)
    return ready;
  return operation->getOperation()
      ->calculateOperationTime(operation, ready, Duration(setupSeconds), true)
      .getEnd();
}

int gaJobOpOSPosition(const vector<int>& os, int job, int operation) {
  int seen = 0;
  for (size_t i = 0; i < os.size(); ++i) {
    if (os[i] != job) continue;
    if (seen == operation) return static_cast<int>(i);
    ++seen;
  }
  return -1;
}

int gaGenePosition(const vector<int>& os, int gene) {
  const auto found = find(os.begin(), os.end(), gene);
  return found == os.end() ? -1 : static_cast<int>(found - os.begin());
}

void gaInsertIntervalSorted(
    vector<GAScheduleInterval>& intervals, GAScheduleInterval item) {
  auto it = lower_bound(
      intervals.begin(), intervals.end(), item.begin,
      [](const GAScheduleInterval& lhs, int value) {
        return lhs.begin < value;
      });
  intervals.insert(it, item);
}

struct GAMaterialSlice {
  long long offset = 0;
  double quantity = 0.0;
};

// Split a transfer-batch flow across the operation. A producer releases lot
// i at fraction (i+1)/n. A consumer takes lot i at fraction i/n, so the next
// step can start when the first lot exists. Any other flow stays as one
// event: consumption at the start, production at the end.
vector<GAMaterialSlice> gaMaterialSlices(const GAData::MaterialFlow& flow,
                                        long long duration) {
  vector<GAMaterialSlice> slices;
  if (duration < 0) duration = 0;
  if (flow.transferBatch <= ROUNDING_ERROR ||
      fabs(flow.quantity) <= ROUNDING_ERROR) {
    slices.push_back(
        {flow.quantity < 0.0 ? 0 : duration, flow.quantity});
    return slices;
  }
  const double absTotal = fabs(flow.quantity);
  int batches = static_cast<int>(ceil(absTotal / flow.transferBatch));
  if (batches < 1) batches = 1;
  if (batches > 50) batches = 50;
  double lot = flow.transferBatch;
  if (batches == 50) lot = absTotal / static_cast<double>(batches);
  const bool consumer = flow.quantity < 0.0;
  double placed = 0.0;
  for (int index = 0; index < batches; ++index) {
    const double remaining = absTotal - placed;
    if (remaining <= ROUNDING_ERROR) break;
    double quantity = min(lot, remaining);
    placed += quantity;
    if (consumer) quantity = -quantity;
    const long long offset = consumer
        ? duration * static_cast<long long>(index) / batches
        : duration * static_cast<long long>(index + 1) / batches;
    slices.push_back({offset, quantity});
  }
  if (slices.empty())
    slices.push_back({consumer ? 0 : duration, flow.quantity});
  return slices;
}

// Seconds after the producer starts when this consumer may start. A producing
// transfer batch releases its first lot before the operation ends. A consumer
// that takes the whole quantity at its start waits until enough lots exist.
// No transfer batch on the producer keeps the historical full-lot release.
int gaTransferReleaseOffset(const vector<GAData::MaterialFlow>& producer,
                            const vector<GAData::MaterialFlow>& consumer,
                            int producerDuration) {
  if (producerDuration <= 0) return max(0, producerDuration);
  int offset = 0;
  bool linked = false;
  for (const auto& need : consumer) {
    if (!need.buffer || need.quantity >= -ROUNDING_ERROR) continue;
    const GAData::MaterialFlow* supply = nullptr;
    for (const auto& made : producer) {
      if (made.buffer == need.buffer && made.quantity > ROUNDING_ERROR) {
        supply = &made;
        break;
      }
    }
    if (!supply) continue;
    if (supply->transferBatch <= ROUNDING_ERROR) return producerDuration;
    linked = true;
    const double produced = fabs(supply->quantity);
    int batches = static_cast<int>(ceil(produced / supply->transferBatch));
    if (batches < 1) batches = 1;
    if (batches > 50) batches = 50;
    const double lot = batches == 50
        ? produced / static_cast<double>(batches)
        : supply->transferBatch;
    double firstNeed = fabs(need.quantity);
    if (need.transferBatch > ROUNDING_ERROR)
      firstNeed = min(firstNeed, need.transferBatch);
    int needed = static_cast<int>(ceil(firstNeed / lot));
    if (needed < 1) needed = 1;
    if (needed > batches) needed = batches;
    const int release = static_cast<int>(
        (static_cast<long long>(producerDuration) * needed) / batches);
    offset = max(offset, release);
  }
  if (!linked) return producerDuration;
  if (offset < 0) return 0;
  if (offset > producerDuration) return producerDuration;
  return offset;
}

bool gaTransferSlicesFit(
    double initial, const vector<pair<long long, double>>& supply,
    const vector<GAMaterialSlice>& slices, long long start) {
  struct Event {
    long long time;
    double quantity;
    int consume;
  };
  vector<Event> events;
  events.reserve(supply.size() + slices.size());
  for (const auto& item : supply)
    events.push_back({item.first, item.second, 0});
  for (const auto& slice : slices) {
    if (slice.quantity >= -ROUNDING_ERROR) continue;
    events.push_back({start + slice.offset, slice.quantity, 1});
  }
  sort(events.begin(), events.end(), [](const Event& left, const Event& right) {
    if (left.time != right.time) return left.time < right.time;
    return left.consume < right.consume;
  });
  double balance = initial;
  for (const auto& event : events) {
    balance += event.quantity;
    if (balance < -ROUNDING_ERROR) return false;
  }
  return true;
}

long long gaEarliestTransferStart(
    double initial, const vector<pair<long long, double>>& supply,
    const vector<GAMaterialSlice>& slices, long long requested) {
  if (gaTransferSlicesFit(initial, supply, slices, requested)) return requested;
  vector<long long> candidates;
  candidates.push_back(requested);
  for (const auto& item : supply) {
    if (item.second <= ROUNDING_ERROR) continue;
    if (item.first > requested) candidates.push_back(item.first);
    for (const auto& slice : slices) {
      if (slice.quantity >= -ROUNDING_ERROR) continue;
      const long long start = item.first - slice.offset;
      if (start > requested) candidates.push_back(start);
    }
  }
  sort(candidates.begin(), candidates.end());
  candidates.erase(unique(candidates.begin(), candidates.end()), candidates.end());
  for (const long long start : candidates)
    if (gaTransferSlicesFit(initial, supply, slices, start)) return start;
  return numeric_limits<long long>::max() / 4;
}

long long gaMaterialReadyTime(
    const vector<GAData::FixedMaterialEvent>& events,
    const unordered_map<const Buffer*, double>& initialOnhand,
    const vector<GAData::MaterialFlow>& flows, long long requested,
    long long duration) {
  long long ready = requested;
  for (const auto& flow : flows) {
    if (!flow.buffer || flow.quantity >= -ROUNDING_ERROR) continue;
    const double required = -flow.quantity;
    double balance = 0.0;
    auto initial = initialOnhand.find(flow.buffer);
    if (initial != initialOnhand.end()) balance = initial->second;
    vector<GAData::FixedMaterialEvent> timeline;
    for (const auto& event : events)
      if (event.buffer == flow.buffer) timeline.push_back(event);
    sort(timeline.begin(), timeline.end(), [](const auto& a, const auto& b) {
      if (a.time != b.time) return a.time < b.time;
      return a.quantity > b.quantity;
    });
    if (flow.transferBatch > ROUNDING_ERROR) {
      vector<pair<long long, double>> supply;
      supply.reserve(timeline.size());
      for (const auto& event : timeline)
        supply.emplace_back(event.time, event.quantity);
      const long long start = gaEarliestTransferStart(
          balance, supply, gaMaterialSlices(flow, duration), ready);
      if (start >= numeric_limits<long long>::max() / 8)
        return numeric_limits<long long>::max() / 4;
      ready = max(ready, start);
      continue;
    }
    size_t idx = 0;
    // Apply all receipts and reservations up to the requested start first.
    while (idx < timeline.size() && timeline[idx].time <= ready) {
      const auto timestamp = timeline[idx].time;
      while (idx < timeline.size() && timeline[idx].time == timestamp)
        balance += timeline[idx++].quantity;
    }
    if (balance + ROUNDING_ERROR < required) {
      bool found = false;
      // If quantity becomes sufficient only at a future production event,
      // that event time is the material-ready time. The old code detected
      // sufficient future quantity but incorrectly returned 'requested'.
      while (idx < timeline.size()) {
        const auto timestamp = timeline[idx].time;
        while (idx < timeline.size() && timeline[idx].time == timestamp)
          balance += timeline[idx++].quantity;
        if (balance + ROUNDING_ERROR >= required) {
          ready = max(ready, timestamp);
          found = true;
          break;
        }
      }
      if (!found) return numeric_limits<long long>::max() / 4;
    }
  }
  return ready;
}

// Set only around the single final decode of the exported chromosome, so the
// internal-material gate decisions of the winning solution (not of every
// fitness evaluation) can be dumped for shortage diagnosis.
static bool gGAFinalGateTrace = false;

GADecodedSchedule gaDecodeOne(
    const vector<int>& ms, const vector<int>& os, const GAData& data) {
  GADecodedSchedule schedule;
  schedule.init(data);

  int msPosition = 0;
  for (int job = 0; job < data.jobsNum; ++job) {
    for (int op = 0; op < data.jobsOperations[job]; ++op) {
      int candidateCode = ms[msPosition++];
      const int count = data.candidateMachineCount[job][op];
      if (count <= 0) {
        schedule.materialFeasible = false;
        ++schedule.materialShortageCount;
        continue;
      }
      if (candidateCode < 0) candidateCode = 0;
      if (candidateCode >= count) candidateCode = count - 1;
      const int machine = data.candidateMachine[job][op][candidateCode];
      if (machine < 0 || machine >= data.machinesNum) {
        schedule.materialFeasible = false;
        ++schedule.materialShortageCount;
        continue;
      }
      schedule.selectedOption[job][op] = candidateCode;
      schedule.selectedMachine[job][op] = machine;
      schedule.selectedMachineTime[job][op] =
          data.jobsOperationsDetail[job][op][machine];
    }
  }

  // Apply the chromosome's selected alternate before decoding.  Routing
  // precedence, operation calendars and load definitions can differ between
  // alternatives, so deferring this switch until writeback would score a
  // different plan than the one ultimately exported.
  unordered_map<OperationPlan*, Operation*> originalOperations;
  for (int job = 0; job < data.jobsNum; ++job)
    for (int op = 0; op < data.jobsOperations[job]; ++op) {
      OperationPlan* plan = const_cast<OperationPlan*>(data.operationPlans[job][op]);
      const int option = schedule.selectedOption[job][op];
      Operation* selected = option >= 0 &&
          option < static_cast<int>(data.candidateOperations[job][op].size())
              ? data.candidateOperations[job][op][option] : nullptr;
      // CCPL flow-line stages are fixed operations. Their calendar and load
      // are already represented by the candidate option, and mutating the
      // live plan here rebuilds FlowPlans during every GA fitness evaluation.
      if (!plan || parseCCPLStage(plan) > 0 || !selected ||
          plan->getOperation() == selected)
        continue;
      originalOperations.emplace(plan, plan->getOperation());
      plan->setOperation(selected);
    }

  vector<GAData::FixedMaterialEvent> materialEvents = data.fixedMaterialEvents;
  // The chromosome's machine sequence is mapped to actual operation and
  // resource calendars here. Thus fitness and final writeback share exactly
  // the same start/end values. Existing fixed plans remain on the model
  // calendar. Each resource gets a shadow reservation timeline: this models
  // parallel maximum capacity and already-fixed plans without mutating the
  // model while evaluating a chromosome.
  struct CalendarReservation {
    Date start;
    Date end;
    double quantity;
    PooledString setup;
  };
  unordered_map<const Resource*, vector<CalendarReservation>> reservations;
  for (const auto& fixed : data.fixedReservations)
    reservations[fixed.resource].push_back(
        {fixed.start, fixed.end, fixed.quantity, fixed.setup});
  for (auto& entry : reservations)
    sort(entry.second.begin(), entry.second.end(),
         [](const CalendarReservation& left, const CalendarReservation& right) {
           if (left.start != right.start) return left.start < right.start;
           return left.end < right.end;
         });
  const auto changeoverReady =
      [&](const OperationPlan* operationplan, const Resource* resource,
          const PooledString& toSetup, Date ready) {
        if (!data.changeoverEnabled || !operationplan ||
            !operationplan->getOperation() || !resource ||
            !resource->getSetupMatrix() || ready == Date::infinitePast ||
            ready == Date::infiniteFuture)
          return ready;
        PooledString fromSetup = resource->getSetup();
        Date predecessorEnd = Date::infinitePast;
        for (const auto& entry : reservations[resource]) {
          if (entry.end > ready) continue;
          if (predecessorEnd == Date::infinitePast || entry.end > predecessorEnd) {
            predecessorEnd = entry.end;
            fromSetup = entry.setup;
          }
        }
        const long setup =
            gaChangeoverSeconds(resource, fromSetup, toSetup);
        if (setup <= 0) return ready;
        if (predecessorEnd != Date::infinitePast && predecessorEnd <= ready) {
          const Date setupStart =
              operationplan->getOperation()
                  ->calculateOperationTime(operationplan, ready,
                                           Duration(setup), false)
                  .getStart();
          if (setupStart >= predecessorEnd) return ready;
        }
        const Date base = predecessorEnd != Date::infinitePast
                              ? max(ready, predecessorEnd)
                              : ready;
        return gaAfterChangeover(operationplan, base, setup);
      };
  const auto insertReservation =
      [&](const Resource* resource, Date start, Date end, double quantity,
          const PooledString& setup = PooledString()) {
        auto& list = reservations[resource];
        CalendarReservation item{start, end, quantity, setup};
        auto pos = lower_bound(
            list.begin(), list.end(), item,
            [](const CalendarReservation& left, const CalendarReservation& right) {
              return left.start < right.start;
            });
        list.insert(pos, item);
      };
  // Classic FJSP (and most single-machine APS resources) only need "any
  // overlap blocks".  The previous event-point grid was O(points^2) per
  // attempt and dominated decode time once calendars had thousands of buckets.
  const auto earliestCapacitySlot =
      [&](const OperationPlan* operationplan, const Resource* resource,
          double required, Date earliest, Duration duration,
          const PooledString& incomingSetup) {
        if (!resource || !resource->getConstrained() ||
            resource->isGroup() || required <= ROUNDING_ERROR)
          return earliest;
        Date candidate = earliest;
        Calendar* calendar = resource->getMaximumCalendar();
        const double staticMaximum = resource->getMaximum();
        const bool unitCapacity =
            staticMaximum <= 1.0 + ROUNDING_ERROR &&
            required + ROUNDING_ERROR >= staticMaximum;
        auto& reserved = reservations[resource];
        for (unsigned short attempt = 0; attempt < 1000; ++attempt) {
          const DateRange mapped = operationplan->getOperation()->calculateOperationTime(
              operationplan, candidate, duration, true);
          const Date mappedStart = mapped.getStart();
          const Date mappedEnd = mapped.getEnd();
          Date retry = Date::infiniteFuture;

          if (unitCapacity) {
            // Fast path: one overlapping reservation is enough to reject the
            // slot.  Still honour calendar zeros without building a point grid
            // from every reservation boundary.
            for (const auto& entry : reserved) {
              if (entry.start >= mappedEnd) break;
              if (entry.end > mappedStart && entry.start < mappedEnd)
                retry = min(retry, data.changeoverEnabled
                    ? changeoverReady(operationplan, resource, incomingSetup,
                                      entry.end)
                    : entry.end);
            }
            if (calendar) {
            Calendar::EventIterator event(calendar, mappedStart, true);
            Date previous = event.getDate();
            for (unsigned int n = 0; n < 10000 &&
                 event.getDate() < mappedEnd; ++n) {
              const Date at = event.getDate();
                const double maximum = calendar->getValue(at);
                ++event;
                const Date next = event.getDate();
                if (at >= mappedStart &&
                    maximum + ROUNDING_ERROR < required)
                  retry = min(retry, next);
                if (next <= previous) break;
                previous = next;
              }
            }
            if (retry == Date::infiniteFuture) return candidate;
            if (retry <= candidate) break;
            candidate = retry;
            continue;
          }

          vector<Date> points{mappedStart, mappedEnd};
          if (calendar) {
            Calendar::EventIterator event(calendar, mappedStart, true);
            Date previous = event.getDate();
            for (unsigned int n = 0; n < 10000 &&
                 event.getDate() < mappedEnd; ++n) {
              if (event.getDate() > mappedStart) points.push_back(event.getDate());
              ++event;
              if (event.getDate() <= previous) break;
              previous = event.getDate();
            }
          }
          for (const auto& entry : reserved)
            if (entry.start < mappedEnd && entry.end > mappedStart) {
              points.push_back(max(entry.start, mappedStart));
              points.push_back(min(entry.end, mappedEnd));
            }
          sort(points.begin(), points.end());
          points.erase(unique(points.begin(), points.end()), points.end());
          bool feasible = true;
          for (size_t point = 0; point + 1 < points.size(); ++point) {
            double used = 0.0;
            Date release = Date::infiniteFuture;
            for (const auto& entry : reserved)
              if (entry.start <= points[point] && entry.end > points[point]) {
                used += entry.quantity;
                release = min(release, data.changeoverEnabled
                    ? changeoverReady(operationplan, resource, incomingSetup,
                                      entry.end)
                    : entry.end);
              }
            const double maximum = calendar ? calendar->getValue(points[point])
                                            : staticMaximum;
            if (used + required > maximum + ROUNDING_ERROR) {
              feasible = false;
              retry = min(retry, min(release, points[point + 1]));
              break;
            }
          }
          if (feasible) return candidate;
          if (retry == Date::infiniteFuture || retry <= candidate) break;
          candidate = retry;
        }
        return Date::infiniteFuture;
      };
  unordered_map<const OperationPlan*, Date> calendarCompletion;
  struct CalendarMaterialEvent { Date date; double quantity; };
  unordered_map<const Buffer*, vector<CalendarMaterialEvent>> calendarMaterial;
  // A rebuilt supply network can expose equivalent WIP buffers through
  // different object identities. Keep an item-level ledger as well; this is
  // the same cumulative quantity view MRP uses when deciding a tail batch.
  unordered_map<string, vector<CalendarMaterialEvent>> calendarMaterialByItem;
  const auto isInternalMaterial = [&data](const Buffer* buffer) {
    return buffer && (data.internalMaterialBuffers.count(buffer) ||
        (buffer->getItem() &&
         data.internalMaterialItems.count(buffer->getItem()->getName())));
  };
  // Stage 2 always keeps the ledger. Stage 1 keeps it as soon as any GA
  // operation produces or consumes an internal buffer, or precheck is on.
  const bool enforceInventory = !data.fastDecode || data.materialPrecheck ||
      !data.internalMaterialItems.empty();
  // A CCPL line produces WIP at the line-side location while the next stage
  // can consume it from a warehouse buffer.  MRP represents that leg as a
  // proposed zero-lead-time distribution order.  The GA decoder must expose
  // the same receipt event while it scores a chromosome; otherwise it sees
  // the producer and consumer as unrelated buffers and falsely reports a
  // shortage after writeback.
  unordered_map<const Buffer*, vector<const Buffer*>> distributionDestinations;
  if (enforceInventory)
  for (auto plan = OperationPlan::begin(); plan != OperationPlan::end(); ++plan) {
    if (!plan->getOperation() ||
        !plan->getOperation()->hasType<OperationItemDistribution>())
      continue;
    const Buffer* source = nullptr;
    const Buffer* destination = nullptr;
    for (auto flow = plan->beginFlowPlans(); flow != plan->endFlowPlans(); ++flow) {
      if (!flow->getBuffer()) continue;
      if (flow->getQuantity() < -ROUNDING_ERROR) source = flow->getBuffer();
      else if (flow->getQuantity() > ROUNDING_ERROR) destination = flow->getBuffer();
    }
    if (source && destination)
      distributionDestinations[source].push_back(destination);
  }
  for (auto& entry : distributionDestinations) {
    auto& destinations = entry.second;
    sort(destinations.begin(), destinations.end());
    destinations.erase(unique(destinations.begin(), destinations.end()),
                       destinations.end());
  }
  const auto addCalendarMaterialEvent =
      [&](const Buffer* buffer, Date date, double quantity) {
        if (!buffer || fabs(quantity) <= ROUNDING_ERROR) return;
        calendarMaterial[buffer].push_back({date, quantity});
        if (buffer->getItem())
          calendarMaterialByItem[buffer->getItem()->getName()].push_back(
              {date, quantity});
      };
  if (enforceInventory)
  for (const auto& event : data.fixedMaterialEvents)
    addCalendarMaterialEvent(
        event.buffer,
        data.schedulingAnchor + Duration(static_cast<long>(event.time)),
        event.quantity);
  const auto calendarMaterialReady = [&](const vector<GAData::MaterialFlow>& flows,
                                         Date requested,
                                         const OperationPlan* consumer,
                                         long long processSeconds) {
    Date ready = requested;
    for (const auto& flow : flows) {
      if (!flow.buffer || flow.quantity >= -ROUNDING_ERROR) continue;
      if (!data.materialPrecheck &&
          !isInternalMaterial(flow.buffer))
        continue;
      double balance = 0.0;
      const auto initial = data.initialOnhand.find(flow.buffer);
      if (initial != data.initialOnhand.end()) balance = initial->second;
      auto timeline = calendarMaterial[flow.buffer];
      if (isInternalMaterial(flow.buffer) && flow.buffer->getItem()) {
        auto byItem = calendarMaterialByItem.find(
            flow.buffer->getItem()->getName());
        if (byItem != calendarMaterialByItem.end())
          timeline = byItem->second;
      }
      // Reconstruct proposed DO receipts from the actual source ledger. A
      // source receipt is not automatically a receipt at every warehouse:
      // each DO reserves its own batch FIFO and reaches its destination only
      // after its transport duration. This is intentionally the same
      // quantity rule used by synchronizeProposedDistributions after export.
      for (const auto& link : data.proposedDistributions) {
        if (link.destination != flow.buffer || !link.source ||
            link.quantity <= ROUNDING_ERROR)
          continue;
        vector<const GAData::DistributionLink*> siblings;
        for (const auto& candidate : data.proposedDistributions)
          if (candidate.source == link.source &&
              candidate.quantity > ROUNDING_ERROR)
            siblings.push_back(&candidate);
        sort(siblings.begin(), siblings.end(), [](const auto* left, const auto* right) {
          if (left->requested != right->requested)
            return left->requested < right->requested;
          return left->reference < right->reference;
        });
        vector<CalendarMaterialEvent> sourceTimeline = calendarMaterial[link.source];
        stable_sort(sourceTimeline.begin(), sourceTimeline.end(),
                    [](const auto& left, const auto& right) {
                      if (left.date != right.date) return left.date < right.date;
                      return left.quantity > right.quantity;
                    });
        double sourceBalance = 0.0;
        const auto sourceInitial = data.initialOnhand.find(link.source);
        if (sourceInitial != data.initialOnhand.end()) sourceBalance = sourceInitial->second;
        size_t sourcePosition = 0;
        for (const auto* sibling : siblings) {
          Date available = data.schedulingAnchor;
          while (sourceBalance + ROUNDING_ERROR < sibling->quantity &&
                 sourcePosition < sourceTimeline.size()) {
            const Date at = sourceTimeline[sourcePosition].date;
            while (sourcePosition < sourceTimeline.size() &&
                   sourceTimeline[sourcePosition].date == at)
              sourceBalance += sourceTimeline[sourcePosition++].quantity;
            available = max(available, at);
          }
          if (sourceBalance + ROUNDING_ERROR < sibling->quantity) continue;
          sourceBalance -= sibling->quantity;
          if (sibling == &link)
            timeline.push_back({available + sibling->transit, sibling->quantity});
        }
      }
      stable_sort(timeline.begin(), timeline.end(),
                  [](const CalendarMaterialEvent& left,
                     const CalendarMaterialEvent& right) {
                    if (left.date != right.date) return left.date < right.date;
                    return left.quantity > right.quantity;
                  });
      if (flow.transferBatch > ROUNDING_ERROR && consumer &&
          consumer->getOperation()) {
        const auto slices = gaMaterialSlices(flow, processSeconds);
        const auto consumeAt = [&](Date start, long long offset) {
          if (offset <= 0) return start;
          return consumer->getOperation()->calculateOperationTime(
              consumer, start, Duration(static_cast<long>(offset)), true)
              .getEnd();
        };
        const auto fits = [&](Date start) {
          struct Event {
            Date date;
            double quantity;
            int consume;
          };
          vector<Event> events;
          events.reserve(timeline.size() + slices.size());
          for (const auto& item : timeline)
            events.push_back({item.date, item.quantity, 0});
          for (const auto& slice : slices) {
            if (slice.quantity >= -ROUNDING_ERROR) continue;
            events.push_back(
                {consumeAt(start, slice.offset), slice.quantity, 1});
          }
          sort(events.begin(), events.end(),
               [](const Event& left, const Event& right) {
                 if (left.date != right.date) return left.date < right.date;
                 return left.consume < right.consume;
               });
          double running = balance;
          for (const auto& event : events) {
            running += event.quantity;
            if (running < -ROUNDING_ERROR) return false;
          }
          return true;
        };
        if (!fits(ready)) {
          vector<Date> candidates;
          candidates.push_back(ready);
          for (const auto& item : timeline) {
            if (item.quantity <= ROUNDING_ERROR) continue;
            if (item.date > ready) candidates.push_back(item.date);
            for (const auto& slice : slices) {
              if (slice.quantity >= -ROUNDING_ERROR || slice.offset <= 0)
                continue;
              const Date aligned =
                  item.date - Duration(static_cast<long>(slice.offset));
              if (aligned > ready) candidates.push_back(aligned);
            }
          }
          sort(candidates.begin(), candidates.end());
          candidates.erase(unique(candidates.begin(), candidates.end()),
                           candidates.end());
          Date found = Date::infiniteFuture;
          for (const Date start : candidates)
            if (fits(start)) {
              found = start;
              break;
            }
          if (found == Date::infiniteFuture) return Date::infiniteFuture;
          ready = max(ready, found);
        }
        continue;
      }
      size_t position = 0;
      while (position < timeline.size() && timeline[position].date <= ready)
        balance += timeline[position++].quantity;
      while (balance + ROUNDING_ERROR < -flow.quantity &&
             position < timeline.size()) {
        const Date eventDate = timeline[position].date;
        while (position < timeline.size() &&
               timeline[position].date == eventDate)
          balance += timeline[position++].quantity;
        if (balance + ROUNDING_ERROR >= -flow.quantity)
          ready = max(ready, eventDate);
      }
      if (balance + ROUNDING_ERROR < -flow.quantity)
        return Date::infiniteFuture;
      // Opt-in trace for diagnosing a decoded WIP release. This remains in
      // the C++ decoder and is intentionally disabled in normal planning.
      if (getEnvBool("GA_WIP_TRACE"))
        logger << "GA WIP FIFO " << flow.buffer->getName()
               << " requested=" << requested << " release=" << ready
               << " qty=" << -flow.quantity
               << " balance=" << balance
               << " events=" << timeline.size() << "\n";
    }
    return ready;
  };
  vector<int> executionOrder = gaExpandJobOSToGenes(os, data);
  // A consuming stage must be decoded only after all possible upstream
  // production events are known.  Within a fixed CCPL stage we must also
  // decode in its existing chronological release order.  Otherwise an OS
  // gene with a late material release can be processed first, while a later
  // gene is inserted into an earlier resource gap. Its consumption is then
  // absent from the FIFO ledger when the late gene is checked, producing a
  // false-feasible chromosome and a shortage after writeback.
  //
  // This does not freeze the line: GA still chooses resource alternatives
  // and the order between independent non-flow-line jobs. It merely gives a
  // single, cumulative WIP stream the same FIFO event ordering as MRP.
  if (static_cast<int>(executionOrder.size()) != data.totalOperations()) {
    schedule.materialFeasible = false;
    schedule.materialShortageCount +=
        abs(data.totalOperations() - static_cast<int>(executionOrder.size()));
    return schedule;
  }
  if (enforceInventory)
  stable_sort(executionOrder.begin(), executionOrder.end(),
              [&](int left, int right) {
                const int leftJob = left / data.maxOperations();
                const int rightJob = right / data.maxOperations();
                const int leftOp = left % data.maxOperations();
                const int rightOp = right % data.maxOperations();
                const int leftStage = leftJob >= 0 && leftJob < data.jobsNum &&
                    leftOp < data.jobsOperations[leftJob]
                    ? data.operationStage[leftJob][leftOp] : -1;
                const int rightStage = rightJob >= 0 && rightJob < data.jobsNum &&
                    rightOp < data.jobsOperations[rightJob]
                    ? data.operationStage[rightJob][rightOp] : -1;
                if (leftStage > 0 && rightStage > 0 && leftStage != rightStage)
                  return leftStage < rightStage;
                if (leftStage > 0 && rightStage > 0) {
                  const OperationPlan* leftPlan =
                      data.operationPlans[leftJob][leftOp];
                  const OperationPlan* rightPlan =
                      data.operationPlans[rightJob][rightOp];
                  if (leftPlan && rightPlan &&
                      leftPlan->getStart() != rightPlan->getStart())
                    return leftPlan->getStart() < rightPlan->getStart();
                  if (leftPlan && rightPlan)
                    return leftPlan->getReference() < rightPlan->getReference();
                }
                return false;
              });
  unordered_set<const OperationPlan*> decodedNetwork;
  vector<int> nextJobOperation(data.jobsNum, 0);
  for (const auto& byJob : data.operationPlans)
    for (const auto* plan : byJob)
      if (plan) decodedNetwork.insert(plan);
  // Routing predecessors are often alternate/container operationplans that
  // aren't GA genes themselves.  Their completion must nevertheless follow
  // the newly decoded leaf child, not their old database end date.
  const auto completionOf = [&](const OperationPlan* plan,
                                const auto& self) -> Date {
    if (!plan) return Date::infinitePast;
    const auto mapped = calendarCompletion.find(plan);
    if (mapped != calendarCompletion.end()) return mapped->second;
    Date completion = Date::infinitePast;
    for (auto child = plan->getSubOperationPlans();
         child != OperationPlan::end(); ++child)
      completion = max(completion, self(&*child, self));
    if (completion != Date::infinitePast) return completion;
    return plan->getEnd();
  };
  const auto mutableDescendantsDecoded = [&](const OperationPlan* plan,
                                             const auto& self) -> bool {
    if (!plan) return true;
    bool hasChild = false;
    for (auto child = plan->getSubOperationPlans();
         child != OperationPlan::end(); ++child) {
      hasChild = true;
      if (!self(&*child, self)) return false;
    }
    if (hasChild) return true;
    return !decodedNetwork.count(plan) || calendarCompletion.count(plan);
  };
  // An OS gene is a priority among ready operations, never permission to
  // violate an explicit routing predecessor. Keep entries pending until the
  // model predecessor has received its actual calendar completion.
  deque<int> pending(executionOrder.begin(), executionOrder.end());
  if (data.fastDecode) {
    // Job operations are already in routing order. The next ready gene is the
    // earliest remaining OS position of each job, so the pending scan does
    // not walk the routing tree or reshuffle the vector on every operation.
    const int width = data.maxOperations();
    vector<vector<int>> position(data.jobsNum);
    for (int index = 0; index < static_cast<int>(executionOrder.size()); ++index) {
      const int job = executionOrder[index] / width;
      if (job >= 0 && job < data.jobsNum) position[job].push_back(index);
    }
    using Rank = pair<int, int>;
    priority_queue<Rank, vector<Rank>, greater<Rank>> ready;
    vector<int> cursor(data.jobsNum, 0);
    for (int job = 0; job < data.jobsNum; ++job)
      if (!position[job].empty()) ready.emplace(position[job][0], job);
    pending.clear();
    while (!ready.empty()) {
      const int job = ready.top().second;
      ready.pop();
      pending.push_back(executionOrder[position[job][cursor[job]++]]);
      if (cursor[job] < static_cast<int>(position[job].size()))
        ready.emplace(position[job][cursor[job]], job);
    }
  }
  while (!pending.empty()) {
    auto selected = pending.end();
    for (auto candidate = pending.begin(); candidate != pending.end(); ++candidate) {
      const int candidateJob = *candidate / data.maxOperations();
      const int candidateOp = *candidate % data.maxOperations();
      if (candidateJob < 0 || candidateJob >= data.jobsNum ||
          candidateOp != nextJobOperation[candidateJob])
        continue;
      if (data.fastDecode) {
        selected = candidate;
        break;
      }
      const OperationPlan* candidatePlan =
          candidateOp >= 0 && candidateOp < data.jobsOperations[candidateJob]
              ? data.operationPlans[candidateJob][candidateOp] : nullptr;
      const OperationPlan* predecessor =
          candidatePlan ? candidatePlan->getPrevSubOpplan() : nullptr;
      if (!predecessor || !decodedNetwork.count(predecessor) ||
          mutableDescendantsDecoded(predecessor, mutableDescendantsDecoded)) {
        selected = candidate;
        break;
      }
    }
    // Never violate a routing dependency merely to consume an OS gene. A
    // broken/cyclic network is infeasible for this chromosome and must not be
    // exported as a precedence-invalid direct reschedule.
    if (selected == pending.end()) {
      schedule.materialFeasible = false;
      schedule.materialShortageCount += static_cast<int>(pending.size());
      break;
    }
    const int gene = *selected;
    const int job = gene / data.maxOperations();
    const int op = gene % data.maxOperations();
    pending.erase(selected);
    if (job < 0 || job >= data.jobsNum || op < 0 ||
        op >= data.jobsOperations[job]) continue;
    ++nextJobOperation[job];
    const int machine = schedule.selectedMachine[job][op];
    if (machine < 0 || machine >= data.machinesNum) {
      schedule.materialFeasible = false;
      ++schedule.materialShortageCount;
      continue;
    }
    const int duration = schedule.selectedMachineTime[job][op];
    int operationBeginTime = 0;
    if (op != 0) {
      if (!data.overlapEnabled) {
        const int previousBegin = schedule.begin[job][op - 1];
        const int previousEnd = schedule.end[job][op - 1];
        const int previousDuration = max(0, previousEnd - previousBegin);
        const int release = gaTransferReleaseOffset(
            data.operationFlows[job][op - 1],
            data.operationFlows[job][op], previousDuration);
        operationBeginTime = release >= previousDuration
            ? previousEnd
            : previousBegin + release;
      } else {
        const int previousBeginTime = schedule.begin[job][op - 1];
        const int previousProcessingTime =
            schedule.selectedMachineTime[job][op - 1];
        const int currentProcessingTime = duration;
        const int batchTimePrev =
            (previousProcessingTime + data.overlapBatchCount - 1) /
            data.overlapBatchCount;
        const int batchTimeCurr =
            (currentProcessingTime + data.overlapBatchCount - 1) /
            data.overlapBatchCount;
        int overlapRelease =
            previousProcessingTime - currentProcessingTime + batchTimeCurr;
        overlapRelease = max(overlapRelease, batchTimePrev);
        operationBeginTime =
            previousBeginTime + data.transferTime[job][op - 1] +
            overlapRelease;
      }
    }

    if (data.materialPrecheck || !data.internalMaterialItems.empty()) {
      vector<GAData::MaterialFlow> constrainedFlows;
      for (const auto& flow : data.operationFlows[job][op])
        if (data.materialPrecheck ||
            isInternalMaterial(flow.buffer))
          constrainedFlows.push_back(flow);
      const auto& flows = data.operationFlows[job][op];
      const long long materialReady = gaMaterialReadyTime(
          materialEvents, data.initialOnhand, constrainedFlows,
          operationBeginTime, duration);
      if (materialReady >= numeric_limits<long long>::max() / 8) {
        // External-material postcheck may remain permissive. Internal WIP is
        // different: it is part of this decoded chromosome and an unavailable
        // WIP batch must never be exported. The real-calendar loop below
        // normally resolves it; retain infeasibility here only as a guard for
        // a broken internal event chain.
        bool hasInternalWip = false;
        for (const auto& flow : constrainedFlows)
          if (flow.buffer && flow.quantity < -ROUNDING_ERROR &&
              isInternalMaterial(flow.buffer)) {
            hasInternalWip = true;
            break;
          }
        if (data.materialPrecheck || hasInternalWip) {
          schedule.materialFeasible = false;
          ++schedule.materialShortageCount;
        }
      } else {
        operationBeginTime = max(operationBeginTime,
                                 static_cast<int>(min<long long>(
                                     materialReady, numeric_limits<int>::max() / 4)));
      }
    }

    auto& intervals = schedule.machineIntervals[machine];
    PooledString toSetup;
    int processing = operationBeginTime;
    int setup = 0;
    if (!data.changeoverEnabled) {
      for (const auto& interval : intervals) {
        if (processing >= interval.end) continue;
        if (interval.begin - processing >= duration) break;
        processing = interval.end;
      }
    } else {
    const Resource* setupResource =
        machine >= 0 && machine < static_cast<int>(data.resources.size())
            ? data.resources[machine] : nullptr;
    const OperationPlan* setupPlan = data.operationPlans[job][op];
    toSetup = gaLoadSetup(
        setupPlan ? setupPlan->getOperation() : nullptr, setupResource);
    for (int guard = 0; guard < 64; ++guard) {
      int predecessorEnd = -1;
      int predecessorJob = -1;
      int predecessorOp = -1;
      for (const auto& interval : intervals) {
        if (interval.end <= processing && interval.end > predecessorEnd) {
          predecessorEnd = interval.end;
          predecessorJob = interval.job;
          predecessorOp = interval.operation;
        }
      }
      PooledString fromSetup;
      if (predecessorJob >= 0) {
        const OperationPlan* previous =
            data.operationPlans[predecessorJob][predecessorOp];
        fromSetup = gaLoadSetup(
            previous ? previous->getOperation() : nullptr, setupResource);
      } else if (setupResource)
        fromSetup = setupResource->getSetup();
      setup = static_cast<int>(gaChangeoverSeconds(
          setupResource, fromSetup, toSetup));
      int needed = processing;
      if (predecessorEnd >= 0)
        needed = max(processing, predecessorEnd + setup);
      else if (setup > 0)
        needed = max(processing, setup);
      const int occupation = needed - setup;
      bool blocked = false;
      for (const auto& interval : intervals) {
        if (occupation >= interval.end) continue;
        if (interval.begin - occupation >= setup + duration) break;
        processing = interval.end;
        blocked = true;
        break;
      }
      if (!blocked) {
        processing = needed;
        break;
      }
    }
    }
    GAScheduleInterval item{processing - setup, processing + duration, job, op};
    gaInsertIntervalSorted(intervals, item);
    schedule.begin[job][op] = processing;
    schedule.end[job][op] = processing + duration;
    schedule.machineLoad[machine] += duration + setup;
    if (data.materialPrecheck || !data.internalMaterialItems.empty()) {
      for (const auto& flow : data.operationFlows[job][op]) {
        if (!data.materialPrecheck &&
            !isInternalMaterial(flow.buffer))
          continue;
        for (const auto& slice : gaMaterialSlices(flow, duration))
          materialEvents.push_back(
              {flow.buffer,
               processing + static_cast<int>(slice.offset),
               slice.quantity});
      }
    }

    const OperationPlan* modelOp = data.operationPlans[job][op];
    const Resource* modelRes = data.resources[machine];
    if (!modelOp || !modelOp->getOperation() || !modelRes) {
      schedule.materialFeasible = false;
      ++schedule.materialShortageCount;
      continue;
    }
    if (data.lockedOperation[job][op]) {
      // Locked OS prefix: preserve the dispatched operation verbatim.  It
      // still contributes its actual completion, resource reservation and
      // material events to the decoded suffix.
      schedule.calendarBegin[job][op] = modelOp->getStart();
      schedule.calendarEnd[job][op] = modelOp->getEnd();
      calendarCompletion[modelOp] = modelOp->getEnd();
      insertReservation(modelRes, modelOp->getStart(), modelOp->getEnd(), 1.0,
                        gaLoadSetup(modelOp->getOperation(), modelRes));
      const long long lockedEnd =
          (modelOp->getEnd() - data.schedulingAnchor).getSeconds();
      schedule.makespan = max(schedule.makespan, lockedEnd);
      schedule.jobCompletion[job] = max(schedule.jobCompletion[job], lockedEnd);
      const long long lockedDuration = max(
          0LL, static_cast<long long>(
              (modelOp->getEnd() - modelOp->getStart()).getSeconds()));
      for (const auto& flow : data.operationFlows[job][op]) {
        if (!flow.buffer || fabs(flow.quantity) <= ROUNDING_ERROR) continue;
        for (const auto& slice : gaMaterialSlices(flow, lockedDuration)) {
          Date at = modelOp->getEnd();
          if (slice.offset <= 0)
            at = modelOp->getStart();
          else if (slice.offset < lockedDuration)
            at = modelOp->getOperation()->calculateOperationTime(
                modelOp, modelOp->getStart(),
                Duration(static_cast<long>(slice.offset)), true)
                .getEnd();
          addCalendarMaterialEvent(flow.buffer, at, slice.quantity);
        }
      }
      continue;
    }
    Date requested = data.schedulingAnchor +
        Duration(static_cast<long>(item.begin));
    const auto transferBoundary = [&](int previousJob, int previousOp,
                                      Date previousStart, Date previousEnd) {
      if (previousStart == Date::infinitePast ||
          previousEnd == Date::infinitePast ||
          previousEnd == Date::infiniteFuture)
        return previousEnd;
      const int previousDuration = max(
          0, schedule.end[previousJob][previousOp] -
                 schedule.begin[previousJob][previousOp]);
      const int release = gaTransferReleaseOffset(
          data.operationFlows[previousJob][previousOp],
          data.operationFlows[job][op], previousDuration);
      if (release >= previousDuration) return previousEnd;
      if (release <= 0) return previousStart;
      const OperationPlan* previousPlan =
          data.operationPlans[previousJob][previousOp];
      if (!previousPlan || !previousPlan->getOperation()) return previousEnd;
      return previousPlan->getOperation()->calculateOperationTime(
          previousPlan, previousStart,
          Duration(static_cast<long>(release)), true).getEnd();
    };
    if (op > 0 && schedule.calendarEnd[job][op - 1] != Date::infinitePast)
      requested = max(requested, transferBoundary(
          job, op - 1, schedule.calendarBegin[job][op - 1],
          schedule.calendarEnd[job][op - 1]));
    // The GA job component isn't guaranteed to be identical to the model's
    // routing tree (eg an alternate can flatten the hierarchy). Preserve the
    // model's explicit previous-step relation as the authoritative extra
    // precedence boundary whenever it has already been decoded. A producing
    // transfer batch releases that boundary at the first lot instead of the
    // predecessor's completion.
    if (!data.fastDecode)
    if (const OperationPlan* previous = modelOp->getPrevSubOpplan()) {
      int previousJob = -1;
      int previousOp = -1;
      for (int scanJob = 0; scanJob < data.jobsNum && previousJob < 0; ++scanJob)
        for (int scanOp = 0; scanOp < data.jobsOperations[scanJob]; ++scanOp)
          if (data.operationPlans[scanJob][scanOp] == previous) {
            previousJob = scanJob;
            previousOp = scanOp;
            break;
          }
      Date boundary = completionOf(previous, completionOf);
      if (previousJob >= 0 &&
          schedule.calendarEnd[previousJob][previousOp] != Date::infinitePast)
        boundary = transferBoundary(
            previousJob, previousOp,
            schedule.calendarBegin[previousJob][previousOp],
            schedule.calendarEnd[previousJob][previousOp]);
      if (boundary != Date::infinitePast && boundary != Date::infiniteFuture)
        requested = max(requested, boundary);
    }
    double required = 1.0;
    for (auto load = modelOp->beginLoadPlans(); load != modelOp->endLoadPlans(); ++load)
      if (load->getResource() == modelRes && load->getQuantity() < -ROUNDING_ERROR) {
        required = fabs(load->getQuantity());
        break;
    }
    const Duration processDuration(static_cast<long>(duration));
    // No internal material: place the operation on the first free machine
    // slot. With internal WIP, alternate material and capacity until the
    // start covers every consume lot, including a transfer batch.
    // Changeover sits before processing and consumes working time.
    if (!enforceInventory) {
      if (data.changeoverEnabled)
        requested = changeoverReady(modelOp, modelRes, toSetup, requested);
      requested = earliestCapacitySlot(
          modelOp, modelRes, required, requested, processDuration, toSetup);
    } else
    // Resource availability can push a consumer past another already-decoded
    // WIP reservation. Alternate between material and capacity boundaries
    // until neither moves. Checking material only before the resource search
    // made a chromosome look feasible and then export with a buffer shortage.
    for (unsigned short pass = 0; pass < 32; ++pass) {
      const Date materialReady = calendarMaterialReady(
          data.operationFlows[job][op], requested, modelOp,
          static_cast<long long>(processDuration.getSeconds()));
      if (gGAFinalGateTrace) {
        // Diagnostics for the exported chromosome: log every internal-material
        // consumption decision of the calendar gate, plus the balance the
        // ledger held at the requested instant.
        for (const auto& flow : data.operationFlows[job][op]) {
          if (!flow.buffer || flow.quantity >= -ROUNDING_ERROR ||
              !isInternalMaterial(flow.buffer))
            continue;
          logger << "GATE ref=" << modelOp->getReference()
                 << " op=" << modelOp->getOperation()->getName()
                 << " pass=" << pass
                 << " flow=" << flow.buffer->getName()
                 << " qty=" << -flow.quantity
                 << " requested=" << requested
                 << " ready=" << materialReady << "\n";
        }
      }
      if (materialReady == Date::infiniteFuture) {
        requested = Date::infiniteFuture;
        break;
      }
      const Date withSetup = data.changeoverEnabled
          ? changeoverReady(modelOp, modelRes, toSetup, materialReady)
          : materialReady;
      const Date capacityReady = earliestCapacitySlot(
          modelOp, modelRes, required, withSetup, processDuration, toSetup);
      if (capacityReady == Date::infiniteFuture) {
        requested = Date::infiniteFuture;
        break;
      }
      if (capacityReady <= requested) {
        requested = capacityReady;
        break;
      }
      requested = capacityReady;
    }
    if (requested == Date::infiniteFuture) {
      schedule.materialFeasible = false;
      ++schedule.materialShortageCount;
      continue;
    }
    const DateRange mapped = modelOp->getOperation()->calculateOperationTime(
        modelOp, requested, processDuration, true);
    const Date mappedStart = mapped.getStart();
    const Date adjustedEnd = mapped.getEnd();
    schedule.calendarBegin[job][op] = mappedStart;
    schedule.calendarEnd[job][op] = adjustedEnd;
    calendarCompletion[modelOp] = adjustedEnd;
    Date setupStart = mappedStart;
    if (data.changeoverEnabled && modelRes && modelRes->getSetupMatrix()) {
      PooledString fromSetup = modelRes->getSetup();
      Date predecessorEnd = Date::infinitePast;
      for (const auto& entry : reservations[modelRes]) {
        if (entry.end > mappedStart) continue;
        if (predecessorEnd == Date::infinitePast || entry.end > predecessorEnd) {
          predecessorEnd = entry.end;
          fromSetup = entry.setup;
        }
      }
      const long setup = gaChangeoverSeconds(modelRes, fromSetup, toSetup);
      if (setup > 0) {
        setupStart = modelOp->getOperation()
                         ->calculateOperationTime(modelOp, mappedStart,
                                                  Duration(setup), false)
                         .getStart();
        if (setupStart < mappedStart)
          insertReservation(modelRes, setupStart, mappedStart, required,
                            toSetup);
      }
    }
    schedule.calendarSetupBegin[job][op] = setupStart;
    // The model recalculates an operation duration during writeback and can
    // round a quantity-dependent interval one second differently from the
    // calendar mapping above.  Reserve that boundary inclusively in the GA
    // shadow timeline.  This is a conservative one-second guard only; it
    // prevents a later plan from being decoded at an instant the model still
    // considers occupied, which otherwise exports as a capacity overload.
    insertReservation(
        modelRes, mappedStart,
        adjustedEnd + Duration(static_cast<long>(1)), required, toSetup);
    const long long mappedEnd =
        (schedule.calendarEnd[job][op] - data.schedulingAnchor).getSeconds();
    schedule.makespan = max(schedule.makespan, mappedEnd);
    schedule.movableMakespan = max(schedule.movableMakespan, mappedEnd);
    schedule.jobCompletion[job] = max(schedule.jobCompletion[job], mappedEnd);
    if (enforceInventory)
    for (const auto& flow : data.operationFlows[job][op]) {
      if (!flow.buffer || fabs(flow.quantity) <= ROUNDING_ERROR ||
          (!data.materialPrecheck && !isInternalMaterial(flow.buffer)))
        continue;
      const long long producedDuration = max(
          0LL, static_cast<long long>(
              (schedule.calendarEnd[job][op] - mappedStart).getSeconds()));
      for (const auto& slice : gaMaterialSlices(flow, producedDuration)) {
        Date at = schedule.calendarEnd[job][op];
        if (slice.offset <= 0)
          at = mappedStart;
        else if (slice.offset < producedDuration)
          at = modelOp->getOperation()->calculateOperationTime(
              modelOp, mappedStart, Duration(static_cast<long>(slice.offset)),
              true).getEnd();
        addCalendarMaterialEvent(flow.buffer, at, slice.quantity);
      }
    }
  }
  if (gGAFinalGateTrace) {
    // Dump the final decoded FIFO ledger per internal buffer so a reported
    // shortage can be replayed against exactly what the decoder saw, in
    // decode order rather than chronological order.
    for (const auto& entry : calendarMaterial) {
      if (!entry.first || !isInternalMaterial(entry.first)) continue;
      logger << "LEDGER buffer=" << entry.first->getName();
      for (const auto& event : entry.second)
        logger << " [" << event.date << " " << event.quantity << "]";
      logger << "\n";
    }
  }
  for (const auto& entry : originalOperations)
    if (entry.first && entry.first->getOperation() != entry.second)
      entry.first->setOperation(entry.second);
  return schedule;
}

void gaApplyDecodedWindow(GADecodedSchedule& schedule, const GAData& data,
                          int job, int op, Date start, Date end) {
  const Date oldBegin = schedule.calendarBegin[job][op];
  const Date oldSetup = schedule.calendarSetupBegin[job][op];
  schedule.calendarBegin[job][op] = start;
  schedule.calendarEnd[job][op] = end;
  if (oldSetup != Date::infinitePast && oldBegin != Date::infinitePast &&
      oldSetup < oldBegin)
    schedule.calendarSetupBegin[job][op] = start - (oldBegin - oldSetup);
  else
    schedule.calendarSetupBegin[job][op] = start;
  const long long beginSeconds = (start - data.schedulingAnchor).getSeconds();
  const long long endSeconds = (end - data.schedulingAnchor).getSeconds();
  schedule.begin[job][op] = static_cast<int>(beginSeconds);
  schedule.end[job][op] = static_cast<int>(endSeconds);
  const int machine = schedule.selectedMachine[job][op];
  if (machine < 0 || machine >= data.machinesNum) return;
  for (auto& interval : schedule.machineIntervals[machine])
    if (interval.job == job && interval.operation == op) {
      const Date setupBegin = schedule.calendarSetupBegin[job][op];
      interval.begin = static_cast<int>(
          (setupBegin - data.schedulingAnchor).getSeconds());
      interval.end = schedule.end[job][op];
    }
  const long long completion = max(0LL, endSeconds);
  schedule.jobCompletion[job] = max(schedule.jobCompletion[job], completion);
  schedule.makespan = max(schedule.makespan, completion);
  schedule.movableMakespan = max(schedule.movableMakespan, completion);
}

// Stage 2. Right-shift every operation except a job's last step, up to the
// next step and the next occupant of the same machine. The job completion
// stays put, so idle gaps between steps (WIP waiting) shrink.
int gaJitRightShift(GADecodedSchedule& schedule, const GAData& data) {
  int moved = 0;
  for (int pass = 0; pass < 4; ++pass) {
    int passMoved = 0;
    for (int job = 0; job < data.jobsNum; ++job) {
      const int operations = data.jobsOperations[job];
      for (int op = operations - 2; op >= 0; --op) {
        const Date start = schedule.calendarBegin[job][op];
        const Date end = schedule.calendarEnd[job][op];
        const Date nextStart = schedule.calendarBegin[job][op + 1];
        if (start == Date::infinitePast || end == Date::infinitePast ||
            nextStart == Date::infinitePast || end <= start ||
            nextStart <= end)
          continue;
        const Duration duration = end - start;
        Date limit = nextStart;
        const int machine = schedule.selectedMachine[job][op];
        if (machine >= 0 && machine < data.machinesNum) {
          Date nextMachine = Date::infiniteFuture;
          int nextJob = -1;
          int nextOp = -1;
          for (int otherJob = 0; otherJob < data.jobsNum; ++otherJob) {
            for (int otherOp = 0; otherOp < data.jobsOperations[otherJob];
                 ++otherOp) {
              if (otherJob == job && otherOp == op) continue;
              if (schedule.selectedMachine[otherJob][otherOp] != machine)
                continue;
              const Date otherStart = schedule.calendarBegin[otherJob][otherOp];
              if (otherStart != Date::infinitePast && otherStart > start &&
                  otherStart < nextMachine) {
                nextMachine = otherStart;
                nextJob = otherJob;
                nextOp = otherOp;
              }
            }
          }
          if (nextJob >= 0) {
            long setup = 0;
            if (data.changeoverEnabled) {
              const OperationPlan* selfPlan = data.operationPlans[job][op];
              const OperationPlan* otherPlan =
                  data.operationPlans[nextJob][nextOp];
              setup = gaChangeoverSeconds(
                  data.resources[machine],
                  gaLoadSetup(selfPlan ? selfPlan->getOperation() : nullptr,
                              data.resources[machine]),
                  gaLoadSetup(otherPlan ? otherPlan->getOperation() : nullptr,
                              data.resources[machine]));
            }
            const Date gap = nextMachine - Duration(setup);
            if (gap < limit) limit = gap;
          }
        }
        Date newEnd = limit;
        Date newStart = newEnd - duration;
        if (op > 0) {
          const Date previousEnd = schedule.calendarEnd[job][op - 1];
          if (previousEnd != Date::infinitePast && previousEnd > newStart) {
            newStart = previousEnd;
            newEnd = newStart + duration;
          }
        }
        if (machine >= 0 && machine < data.machinesNum) {
          Date machinePrevious = Date::infinitePast;
          Date latestEnd = Date::infinitePast;
          int previousJob = -1;
          int previousOp = -1;
          for (int otherJob = 0; otherJob < data.jobsNum; ++otherJob) {
            for (int otherOp = 0; otherOp < data.jobsOperations[otherJob];
                 ++otherOp) {
              if (schedule.selectedMachine[otherJob][otherOp] != machine)
                continue;
              const Date otherEnd = schedule.calendarEnd[otherJob][otherOp];
              const Date otherStart = schedule.calendarBegin[otherJob][otherOp];
              if (otherEnd != Date::infinitePast && otherStart < start &&
                  otherEnd > latestEnd) {
                latestEnd = otherEnd;
                previousJob = otherJob;
                previousOp = otherOp;
              }
            }
          }
          if (previousJob >= 0) {
            long setup = 0;
            if (data.changeoverEnabled) {
              const OperationPlan* selfPlan = data.operationPlans[job][op];
              const OperationPlan* otherPlan =
                  data.operationPlans[previousJob][previousOp];
              setup = gaChangeoverSeconds(
                  data.resources[machine],
                  gaLoadSetup(otherPlan ? otherPlan->getOperation() : nullptr,
                              data.resources[machine]),
                  gaLoadSetup(selfPlan ? selfPlan->getOperation() : nullptr,
                              data.resources[machine]));
            }
            machinePrevious = latestEnd + Duration(setup);
          }
          if (machinePrevious != Date::infinitePast &&
              machinePrevious > newStart) {
            newStart = machinePrevious;
            newEnd = newStart + duration;
          }
        }
        if (newStart <= start || newEnd > limit) continue;
        gaApplyDecodedWindow(schedule, data, job, op, newStart, newEnd);
        ++passMoved;
      }
    }
    moved += passMoved;
    if (!passMoved) break;
  }
  return moved;
}

void gaRefineDecodedSchedule(GADecodedSchedule& schedule, const GAData& data,
                              int candidate, int candidateCount) {
  if (!data.fastDecode) return;
  const int jitMoves = gaJitRightShift(schedule, data);
  long long span = 0;
  for (int job = 0; job < data.jobsNum; ++job)
    for (int op = 0; op < data.jobsOperations[job]; ++op) {
      if (schedule.calendarEnd[job][op] == Date::infinitePast) continue;
      span = max(span, static_cast<long long>(
                          (schedule.calendarEnd[job][op] - data.schedulingAnchor)
                              .getSeconds()));
    }
  schedule.makespan = span;
  schedule.movableMakespan = span;
  logger << "GA stage2: candidate " << candidate << "/" << candidateCount
         << " JIT right-shifts=" << jitMoves
         << " makespan=" << span << "s\n";
}

// --- Multi-objective evaluation -------------------------------------------
// Three objectives, all "smaller is better":
//   makespan  : total schedule length (already on GADecodedSchedule)
//   balance   : load-balance coefficient of variation across machines (0=even)
//   tardiness : sum of priority-weighted lateness over jobs
// Each is min-max normalized against the fixed baseline (GAData::normBaseline,
// computed once from the initial population) and combined with weights
// (GAData::objWeights) into a single scalar fitness that the GA minimizes.

double gaBalanceCV(const GADecodedSchedule& schedule, const GAData& data) {
  // Compare utilization, not raw busy time.  A machine on one short shift or
  // with capacity 2 mustn't be judged against a 24/7 single-capacity machine
  // by the same denominator.
  if (schedule.movableMakespan <= 0) return 0.0;
  const Date windowStart = data.schedulingAnchor;
  const Date windowEnd = windowStart + Duration(
      static_cast<long>(schedule.movableMakespan));
  vector<double> utilization;
  utilization.reserve(schedule.machineLoad.size());
  for (size_t machine = 0; machine < schedule.machineLoad.size(); ++machine) {
    if (schedule.machineLoad[machine] <= 0 ||
        machine >= data.resources.size() || !data.resources[machine])
      continue;
    const Resource* resource = data.resources[machine];
    if (resource->isGroup() || !resource->getConstrained()) continue;
    vector<Date> points{windowStart, windowEnd};
    if (Calendar* maximum = resource->getMaximumCalendar()) {
      Calendar::EventIterator event(maximum, windowStart, true);
      Date previous = event.getDate();
      for (unsigned int n = 0; n < 10000 && event.getDate() < windowEnd; ++n) {
        if (event.getDate() > windowStart) points.push_back(event.getDate());
        ++event;
        if (event.getDate() <= previous) break;
        previous = event.getDate();
      }
    }
    sort(points.begin(), points.end());
    points.erase(unique(points.begin(), points.end()), points.end());
    double capacitySeconds = 0.0;
    for (size_t p = 0; p + 1 < points.size(); ++p) {
      const Duration available = resource->getAvailable(points[p], points[p + 1]);
      const double maximum = resource->getMaximumCalendar()
          ? resource->getMaximumCalendar()->getValue(points[p])
          : resource->getMaximum();
      capacitySeconds += max(0.0, maximum) *
          static_cast<double>(max<long>(0L, available.getSeconds()));
    }
    if (capacitySeconds > 1e-9)
      utilization.push_back(
          static_cast<double>(schedule.machineLoad[machine]) / capacitySeconds);
  }
  const int n = static_cast<int>(utilization.size());
  if (n <= 1) return 0.0;
  double sum = 0.0;
  for (auto load : utilization) sum += load;
  const double mean = sum / n;
  if (mean < 1e-9) return 0.0;
  double variance = 0.0;
  for (auto load : utilization) {
    const double diff = load - mean;
    variance += diff * diff;
  }
  variance /= n;
  return sqrt(variance) / mean;
}

double gaTardiness(const GADecodedSchedule& schedule, const GAData& data) {
  double total = 0.0;
  for (int job = 0; job < data.jobsNum; ++job) {
    const long long completion = schedule.jobCompletion[job];
    const long long due = data.jobDueSeconds[job];
    if (completion > due)
      total += data.jobWeight[job] * static_cast<double>(completion - due);
  }
  return total;
}

unsigned int gaTardyJobCount(const GADecodedSchedule& schedule,
                             const GAData& data) {
  unsigned int count = 0;
  for (int job = 0; job < data.jobsNum; ++job)
    if (schedule.jobCompletion[job] > data.jobDueSeconds[job]) ++count;
  return count;
}

double gaTardinessObjective(const GADecodedSchedule& schedule,
                            const GAData& data) {
  // Lexicographic packing: one additional late final order always outweighs
  // weighted lateness.  1e15 seconds is safely above any supported horizon.
  return static_cast<double>(gaTardyJobCount(schedule, data)) * 1.0e15 +
      gaTardiness(schedule, data);
}

inline double gaNormalize(double value, double lo, double hi) {
  if (hi - lo < 1e-12) return 0.0;  // degenerate range: dimension contributes 0
  return (value - lo) / (hi - lo);
}

double gaEvaluate(const GADecodedSchedule& schedule, const GAData& data) {
  // Internal WIP is part of the decoded production network in both modes.
  // Only external purchase/distribution availability is deferred in
  // postcheck mode, so an internally material-infeasible chromosome must
  // never outrank a feasible one.
  if (!schedule.materialFeasible)
    return 1.0e12 + static_cast<double>(schedule.materialShortageCount) * 1.0e9;
  const double mk = static_cast<double>(schedule.movableMakespan);
  const double cv = gaBalanceCV(schedule, data);
  const double td = gaTardinessObjective(schedule, data);
  const GANormBaseline& b = data.normBaseline;
  const GAObjectiveWeights& w = data.objWeights;
  // Stable score for archive/VNS. Population selection uses a true
  // lexicographic tardiness rank below, where late-order count dominates.
  return w.wMakespan * gaNormalize(mk, b.mkMin, b.mkMax) +
         w.wBalance * gaNormalize(cv, b.cvMin, b.cvMax) +
         w.wTardiness * gaNormalize(td, b.tdMin, b.tdMax);
}

vector<double> gaDecodePopulation(
    const vector<vector<int>>& ms, const vector<vector<int>>& os,
    const GAData& data) {
  // Rank normalization is deliberately population-local.  The three raw
  // objectives have unrelated units (seconds, utilization CV and weighted
  // seconds late), so objective magnitudes must not influence selection.
  // Equal values receive the average rank.  The resulting percentile is in
  // [0, 1] and is combined only for population selection.
  const size_t count = ms.size();
  vector<GADecodedSchedule> schedules;
  schedules.reserve(count);
  vector<double> results(count, 0.0);
  vector<bool> feasible(count, true);
  vector<double> makespan(count), balance(count), tardiness(count);
  for (size_t i = 0; i < count; ++i) {
    schedules.push_back(gaDecodeOne(ms[i], os[i], data));
    feasible[i] = schedules.back().materialFeasible;
    makespan[i] = static_cast<double>(schedules.back().movableMakespan);
    balance[i] = gaBalanceCV(schedules.back(), data);
    tardiness[i] = gaTardinessObjective(schedules.back(), data);
  }
  auto ranks = [count, &feasible](const vector<double>& values) {
    vector<double> result(count, 1.0);
    vector<size_t> order;
    for (size_t i = 0; i < count; ++i)
      if (feasible[i]) order.push_back(i);
    sort(order.begin(), order.end(), [&values](size_t a, size_t b) {
      return values[a] < values[b];
    });
    const double denom = order.size() > 1 ? static_cast<double>(order.size() - 1) : 1.0;
    for (size_t begin = 0; begin < order.size();) {
      size_t end = begin + 1;
      while (end < order.size() &&
             fabs(values[order[end]] - values[order[begin]]) < 1e-9)
        ++end;
      const double averageRank = (static_cast<double>(begin + end - 1) * 0.5) / denom;
      for (size_t p = begin; p < end; ++p) result[order[p]] = averageRank;
      begin = end;
    }
    return result;
  };
  const auto mkRank = ranks(makespan);
  const auto cvRank = ranks(balance);
  const auto tdRank = ranks(tardiness);
  const auto& w = data.objWeights;
  const double weightSum = w.wMakespan + w.wBalance + w.wTardiness;
  const double wm = weightSum > 1e-12 ? w.wMakespan / weightSum : 1.0 / 3.0;
  const double wb = weightSum > 1e-12 ? w.wBalance / weightSum : 1.0 / 3.0;
  const double wt = weightSum > 1e-12 ? w.wTardiness / weightSum : 1.0 / 3.0;
  for (size_t i = 0; i < count; ++i) {
    if (!feasible[i])
      results[i] = 1.0e12 + static_cast<double>(schedules[i].materialShortageCount) * 1.0e9;
    else
      results[i] = wm * mkRank[i] + wb * cvRank[i] + wt * tdRank[i];
  }
  return results;
}

// Unlike rank scores, this scalar is stable across generations.  It is used
// only for the best-so-far archive and one-solution VNS acceptance; genetic
// selection always uses gaDecodePopulation() above.
vector<double> gaEvaluatePopulationRaw(
    const vector<vector<int>>& ms, const vector<vector<int>>& os,
    const GAData& data) {
  vector<double> results(ms.size(), numeric_limits<double>::max());
  for (size_t i = 0; i < ms.size(); ++i)
    results[i] = gaEvaluate(gaDecodeOne(ms[i], os[i], data), data);
  return results;
}

struct GANSGAInfo {
  int front = numeric_limits<int>::max();
  double crowding = 0.0;
  bool feasible = false;
};

struct GAEvaluation {
  vector<GADecodedSchedule> schedules;
  vector<array<double, 3>> objectives;
  vector<double> rawScores;
  vector<bool> feasible;
};

GAEvaluation gaEvaluatePopulationCached(const vector<vector<int>>& ms,
                                        const vector<vector<int>>& os,
                                        const GAData& data) {
  GAEvaluation result;
  const size_t n = ms.size();
  result.schedules.reserve(n); result.objectives.reserve(n);
  result.rawScores.reserve(n); result.feasible.reserve(n);
  for (size_t i = 0; i < n; ++i) {
    result.schedules.push_back(gaDecodeOne(ms[i], os[i], data));
    const auto& s = result.schedules.back();
    result.feasible.push_back(s.materialFeasible);
    const double makespan = static_cast<double>(s.movableMakespan);
    result.objectives.push_back(
        {makespan, gaBalanceCV(s, data), gaTardinessObjective(s, data)});
    result.rawScores.push_back(gaEvaluate(s, data));
  }
  return result;
}

// NSGA-II ranking in the raw three-objective space.  Crowding is calculated
// after per-front min/max normalization, so no objective unit can dominate
// diversity merely because it is measured in seconds.
vector<GANSGAInfo> gaNSGA2Rank(const GAEvaluation& evaluation) {
  const size_t n = evaluation.schedules.size();
  vector<GANSGAInfo> info(n);
  const auto& schedules = evaluation.schedules;
  const auto& obj = evaluation.objectives;
  for (size_t i = 0; i < n; ++i) info[i].feasible = evaluation.feasible[i];
  auto dominates = [&](size_t a, size_t b) {
    if (info[a].feasible != info[b].feasible) return info[a].feasible;
    if (!info[a].feasible) return schedules[a].materialShortageCount < schedules[b].materialShortageCount;
    bool strict = false;
    for (int k = 0; k < 3; ++k) {
      if (obj[a][k] > obj[b][k] + 1e-9) return false;
      if (obj[a][k] + 1e-9 < obj[b][k]) strict = true;
    }
    return strict;
  };
  vector<vector<size_t>> dominated(n), fronts(1);
  vector<int> dominationCount(n, 0);
  for (size_t a = 0; a < n; ++a) for (size_t b = 0; b < n; ++b) if (a != b) {
    if (dominates(a, b)) dominated[a].push_back(b);
    else if (dominates(b, a)) ++dominationCount[a];
  }
  for (size_t i = 0; i < n; ++i) if (!dominationCount[i]) { info[i].front = 0; fronts[0].push_back(i); }
  for (size_t f = 0; f < fronts.size(); ++f) {
    vector<size_t> next;
    for (size_t i : fronts[f]) for (size_t j : dominated[i])
      if (--dominationCount[j] == 0) { info[j].front = static_cast<int>(f + 1); next.push_back(j); }
    if (!next.empty()) fronts.push_back(std::move(next));
  }
  for (const auto& front : fronts) {
    if (front.size() <= 2) { for (auto i : front) info[i].crowding = numeric_limits<double>::infinity(); continue; }
    for (int k = 0; k < 3; ++k) {
      auto ordered = front;
      sort(ordered.begin(), ordered.end(), [&](size_t a, size_t b) { return obj[a][k] < obj[b][k]; });
      info[ordered.front()].crowding = info[ordered.back()].crowding = numeric_limits<double>::infinity();
      const double span = obj[ordered.back()][k] - obj[ordered.front()][k];
      if (span < 1e-12) continue;
      for (size_t p = 1; p + 1 < ordered.size(); ++p)
        if (isfinite(info[ordered[p]].crowding))
          info[ordered[p]].crowding += (obj[ordered[p + 1]][k] - obj[ordered[p - 1]][k]) / span;
    }
  }
  return info;
}

vector<GANSGAInfo> gaNSGA2Rank(const vector<vector<int>>& ms,
                               const vector<vector<int>>& os,
                               const GAData& data) {
  return gaNSGA2Rank(gaEvaluatePopulationCached(ms, os, data));
}

vector<double> gaNSGA2SelectionScore(const vector<GANSGAInfo>& info) {
  vector<double> score(info.size());
  for (size_t i = 0; i < info.size(); ++i)
    score[i] = static_cast<double>(info[i].front) -
        (isfinite(info[i].crowding) ? min(0.999, info[i].crowding / 10.0) : 1.0);
  return score;
}

void gaNSGA2Survive(vector<vector<int>>& ms, vector<vector<int>>& os,
                    int target, const GAEvaluation& evaluation) {
  const auto info = gaNSGA2Rank(evaluation);
  vector<size_t> order(ms.size()); iota(order.begin(), order.end(), 0);
  sort(order.begin(), order.end(), [&](size_t a, size_t b) {
    if (info[a].front != info[b].front) return info[a].front < info[b].front;
    return info[a].crowding > info[b].crowding;
  });
  vector<vector<int>> keptMS, keptOS; keptMS.reserve(target); keptOS.reserve(target);
  for (int i = 0; i < target && i < static_cast<int>(order.size()); ++i) {
    keptMS.push_back(std::move(ms[order[i]])); keptOS.push_back(std::move(os[order[i]]));
  }
  ms = std::move(keptMS); os = std::move(keptOS);
}

void gaTournament(
    const vector<vector<int>>& ms, const vector<vector<int>>& os,
    vector<vector<int>>& newMS, vector<vector<int>>& newOS,
    int tournamentM, const vector<double>& decodeResults, GARNG& rng) {
  const int size = static_cast<int>(ms.size());
  for (int idx = 0; idx < size; ++idx) {
    auto selected =
        gaSampleWithReplacementUnique(rng, size, tournamentM);
    int bestCode = selected.front();
    for (int candidate : selected)
      if (decodeResults[candidate] < decodeResults[bestCode])
        bestCode = candidate;
    newMS[idx] = ms[bestCode];
    newOS[idx] = os[bestCode];
  }
}

void gaTournamentMemory(
    const vector<vector<int>>& ms, const vector<vector<int>>& os,
    const vector<vector<int>>& memoryMS,
    const vector<vector<int>>& memoryOS,
    vector<vector<int>>& newMS, vector<vector<int>>& newOS,
    int tournamentM, const vector<double>& decodeResults,
    const vector<double>& memoryResults, int step, int maxStep,
    GARNG& rng) {
  const int size = static_cast<int>(ms.size());
  const double p =
      cos(M_PI * static_cast<double>(step) /
          static_cast<double>(maxStep) / 2.0);
  const int memorySize = static_cast<int>(memoryMS.size());
  for (int idx = 0; idx < size; ++idx) {
    if (rng.rand01() > p || memorySize <= 0) {
      auto selected =
          gaSampleWithReplacementUnique(rng, size, tournamentM);
      int bestCode = selected.front();
      for (int candidate : selected)
        if (decodeResults[candidate] < decodeResults[bestCode])
          bestCode = candidate;
      newMS[idx] = ms[bestCode];
      newOS[idx] = os[bestCode];
    } else {
      const int target = max(1, tournamentM - 1);
      auto selected =
          gaSampleWithReplacementUnique(rng, size, target);
      int bestCode = selected.front();
      for (int candidate : selected)
        if (decodeResults[candidate] < decodeResults[bestCode])
          bestCode = candidate;
      const int memoryCode = rng.randint(0, memorySize);
      if (decodeResults[bestCode] > memoryResults[memoryCode]) {
        newMS[idx] = ms[bestCode];
        newOS[idx] = os[bestCode];
      } else {
        newMS[idx] = memoryMS[memoryCode];
        newOS[idx] = memoryOS[memoryCode];
      }
    }
  }
}

void gaTournamentRandom(
    const vector<vector<int>>& ms, const vector<vector<int>>& os,
    vector<vector<int>>& newMS, vector<vector<int>>& newOS,
    int tournamentM, const vector<double>& decodeResults,
    const GAData& data, GARNG& rng) {
  const int size = static_cast<int>(ms.size());
  vector<int> recodes(size, -1);
  for (int idx = 0; idx < size; ++idx) {
    auto selected =
        gaSampleWithReplacementUnique(rng, size, tournamentM);
    int bestCode = selected.front();
    for (int candidate : selected)
      if (decodeResults[candidate] < decodeResults[bestCode])
        bestCode = candidate;
    recodes[idx] = bestCode;
  }
  vector<int> uniqueRecodes = recodes;
  sort(uniqueRecodes.begin(), uniqueRecodes.end());
  uniqueRecodes.erase(
      unique(uniqueRecodes.begin(), uniqueRecodes.end()),
      uniqueRecodes.end());
  int idx = 0;
  for (int code : uniqueRecodes) {
    newMS[idx] = ms[code];
    newOS[idx] = os[code];
    ++idx;
  }
  gaInitialOS(newOS, data, rng);
  gaInitialMSRandomSelection(newMS, idx, size, data, rng);
}

vector<float> gaGetCrossoverP(
    const vector<double>& decodeResults, double bestScore) {
  vector<float> crossoverP(decodeResults.size(), 1.0f);
  const double average =
      accumulate(decodeResults.begin(), decodeResults.end(), 0.0) /
      static_cast<double>(decodeResults.size());
  for (size_t i = 0; i < decodeResults.size(); ++i) {
    if (decodeResults[i] >= average) {
      crossoverP[i] = 1.0f;
    } else if (average - static_cast<double>(bestScore) == 0.0) {
      crossoverP[i] = 1.0f;
    } else {
      crossoverP[i] =
          static_cast<float>(
              (average - static_cast<double>(decodeResults[i])) /
              (average - static_cast<double>(bestScore)));
    }
  }
  return crossoverP;
}

void gaUniformCrossover(
    vector<vector<int>>& code, const vector<float>& crossoverP,
    GARNG& rng) {
  const int codeSize = static_cast<int>(code.size());
  if (codeSize < 2) return;
  const int codeLength = static_cast<int>(code[0].size());
  for (int pair = 0; pair < codeSize / 2; ++pair) {
    const int idx1 = pair * 2;
    const int idx2 = idx1 + 1;
    const double p = max(crossoverP[idx1], crossoverP[idx2]);
    if (rng.rand01() >= p) continue;
    for (int pos = 0; pos < codeLength; ++pos) {
      if (code[idx1][pos] == code[idx2][pos]) continue;
      if (rng.rand01() >= 0.2) continue;
      swap(code[idx1][pos], code[idx2][pos]);
    }
  }
}

void gaRandomCrossover(
    vector<vector<int>>& code, const vector<float>& crossoverP,
    const GAData& data, GARNG& rng) {
  for (size_t idx = 0; idx < code.size(); ++idx) {
    if (rng.rand01() >= crossoverP[idx]) continue;
    if (data.lockedOSPrefixLength < static_cast<int>(code[idx].size())) {
      vector<int> suffix(code[idx].begin() + data.lockedOSPrefixLength,
                         code[idx].end());
      rng.shuffle(suffix);
      copy(suffix.begin(), suffix.end(),
           code[idx].begin() + data.lockedOSPrefixLength);
    }
  }
}

void gaPoxCrossover(
    vector<vector<int>>& code, const vector<float>& crossoverP,
    const GAData& data, GARNG& rng) {
  // Numba POX: pick a random job set; keep those job-ids in parent1 order,
  // fill the remaining slots from parent2 order (skipping the job set).
  const int codeSize = static_cast<int>(code.size());
  if (codeSize < 2 || data.jobsNum <= 1) return;
  const int codeLength = static_cast<int>(code[0].size());
  for (int idx = 0; idx < codeSize - 1; ++idx) {
    if (rng.rand01() >= crossoverP[idx]) continue;
    const int prefix = data.lockedOSPrefixLength;
    if (prefix >= codeLength) continue;
    unordered_set<int> jobSet;
    const int setSize = rng.randint(1, data.jobsNum);
    while (static_cast<int>(jobSet.size()) < setSize)
      jobSet.insert(rng.randint(0, data.jobsNum));
    auto& child = code[idx];
    const auto& donor = code[idx + 1];
    int donorPos = prefix;
    for (int pos = prefix; pos < codeLength; ++pos) {
      if (jobSet.count(child[pos])) continue;
      while (donorPos < codeLength && jobSet.count(donor[donorPos]))
        ++donorPos;
      if (donorPos >= codeLength) break;
      child[pos] = donor[donorPos++];
    }
  }
}

void gaRandomMutation(
    vector<vector<int>>& code, const vector<float>& mutationP,
    const GAData& data, GARNG& rng) {
  if (code.empty()) return;
  const int codeLength = static_cast<int>(code[0].size());
  for (size_t idx = 0; idx < code.size(); ++idx) {
    if (rng.rand01() >= mutationP[idx]) continue;
    if (data.lockedOSPrefixLength >= codeLength) continue;
    const int p1 = rng.randint(data.lockedOSPrefixLength, codeLength);
    const int p2 = rng.randint(data.lockedOSPrefixLength, codeLength);
    swap(code[idx][p1], code[idx][p2]);
  }
}

void gaBestMSMutations(
    vector<vector<int>>& ms, const vector<float>& mutationP,
    const vector<vector<int>>& msPositions,
    const vector<vector<int>>& bestMS,
    const GAData& data, GARNG& rng) {
  for (size_t idx = 0; idx < ms.size(); ++idx) {
    if (rng.rand01() >= mutationP[idx]) continue;
    for (int job = 0; job < data.jobsNum; ++job)
      for (int op = 0; op < data.jobsOperations[job]; ++op) {
        if (rng.rand01() >= 0.2) continue;
        ms[idx][msPositions[job][op]] = bestMS[job][op];
      }
  }
}

void gaRandomMSMutations(
    vector<vector<int>>& ms, const vector<float>& mutationP,
    const vector<vector<int>>& msPositions,
    const GAData& data, GARNG& rng) {
  for (size_t idx = 0; idx < ms.size(); ++idx) {
    if (rng.rand01() >= mutationP[idx]) continue;
    for (int job = 0; job < data.jobsNum; ++job)
      for (int op = 0; op < data.jobsOperations[job]; ++op) {
        if (rng.rand01() >= 0.2) continue;
        const int count = data.candidateMachineCount[job][op];
        ms[idx][msPositions[job][op]] = rng.randint(0, count);
      }
  }
}

void gaUpdateMemoryLib(
    vector<vector<int>>& memoryMS, vector<vector<int>>& memoryOS,
    vector<double>& memoryResults,
    const vector<vector<int>>& ms, const vector<vector<int>>& os,
    const vector<double>& decodeResults) {
  vector<int> sortedIndices(decodeResults.size(), 0);
  iota(sortedIndices.begin(), sortedIndices.end(), 0);
  sort(sortedIndices.begin(), sortedIndices.end(),
       [&](int lhs, int rhs) {
         return decodeResults[lhs] < decodeResults[rhs];
       });
  auto maxIt = max_element(memoryResults.begin(), memoryResults.end());
  int maxIndex = static_cast<int>(distance(memoryResults.begin(), maxIt));
  for (int codeIndex : sortedIndices) {
    if (decodeResults[codeIndex] < memoryResults[maxIndex]) {
      memoryMS[maxIndex] = ms[codeIndex];
      memoryOS[maxIndex] = os[codeIndex];
      memoryResults[maxIndex] = decodeResults[codeIndex];
      maxIt = max_element(memoryResults.begin(), memoryResults.end());
      maxIndex = static_cast<int>(distance(memoryResults.begin(), maxIt));
    } else {
      break;
    }
  }
}

bool gaCanSwapAdjacentOnMachine(
    const GADecodedSchedule& schedule, const GAData& data,
    int machine, int leftIdx, int rightIdx) {
  if (leftIdx < 0 ||
      rightIdx >= static_cast<int>(schedule.machineIntervals[machine].size()))
    return false;
  const auto& left = schedule.machineIntervals[machine][leftIdx];
  const auto& right = schedule.machineIntervals[machine][rightIdx];
  if (left.job == right.job) return false;
  int rightPrevEnd = 0;
  if (right.operation != 0)
    rightPrevEnd = schedule.end[right.job][right.operation - 1];
  int leftNextBegin = numeric_limits<int>::max() / 4;
  if (left.operation + 1 < data.jobsOperations[left.job])
    leftNextBegin = schedule.begin[left.job][left.operation + 1];
  const int candidateStart = max(left.begin, rightPrevEnd);
  const int candidateEnd =
      candidateStart +
      schedule.selectedMachineTime[right.job][right.operation];
  const int shiftedLeftEnd =
      candidateEnd +
      schedule.selectedMachineTime[left.job][left.operation];
  return shiftedLeftEnd <= leftNextBegin;
}

bool gaTryVNSOne(
    vector<int>& ms, vector<int>& os, double& currentScore,
    const GAData& data, const vector<vector<int>>& msPositions,
    GARNG& rng, int moveBudget = 0) {
  vector<int> jobsOrder(data.jobsNum);
  iota(jobsOrder.begin(), jobsOrder.end(), 0);
  bool improved = false;
  bool keepSearching = true;
  int acceptedMoves = 0;
  if (moveBudget <= 0)
    moveBudget = max(1, min(32, data.totalOperations() / 8));
  while (keepSearching && acceptedMoves < moveBudget) {
    keepSearching = false;
    auto schedule = gaDecodeOne(ms, os, data);
    rng.shuffle(jobsOrder);
    bool accepted = false;
    for (int job : jobsOrder) {
      for (int op = 0; op < data.jobsOperations[job]; ++op) {
        const int selectedMachine = schedule.selectedMachine[job][op];
        const int lastEndTime = op == 0 ? 0 : schedule.end[job][op - 1];
        const int currentEnd = schedule.end[job][op];
        const int candidateCount = data.candidateMachineCount[job][op];
        for (int idx = 0; idx < candidateCount; ++idx) {
          const int machine = data.candidateMachine[job][op][idx];
          if (machine == selectedMachine) continue;
          const int operationTime =
              data.jobsOperationsDetail[job][op][machine];
          int beginTime = lastEndTime;
          const auto& intervals = schedule.machineIntervals[machine];
          for (const auto& interval : intervals) {
            if (lastEndTime >= interval.end) continue;
            if (interval.begin + operationTime >= currentEnd) break;
            if (beginTime + operationTime <= interval.begin) {
              auto candidateMS = ms;
              auto candidateOS = os;
              candidateMS[msPositions[job][op]] = idx;
              const int osPosition = gaJobOpOSPosition(candidateOS, job, op);
              const int oldOSPosition = gaJobOpOSPosition(
                  candidateOS, interval.job, interval.operation);
              if (osPosition >= data.lockedOSPrefixLength &&
                  oldOSPosition >= data.lockedOSPrefixLength &&
                  oldOSPosition < osPosition) {
                const int value = candidateOS[osPosition];
                for (int p = osPosition; p > oldOSPosition; --p)
                  candidateOS[p] = candidateOS[p - 1];
                candidateOS[oldOSPosition] = value;
              }
              const auto candidateSchedule =
                  gaDecodeOne(candidateMS, candidateOS, data);
              const double candidateScore = gaEvaluate(candidateSchedule, data);
              if (candidateScore < currentScore) {
                ms = std::move(candidateMS);
                os = std::move(candidateOS);
                currentScore = candidateScore;
                keepSearching = true;
                improved = true;
                accepted = true;
                ++acceptedMoves;
              }
              break;
            }
            beginTime = interval.end;
          }
          if (accepted) break;
        }
        if (accepted) break;
      }
      if (accepted) break;
    }
  }
  return improved;
}

bool gaTryVNSTwo(
    vector<int>& ms, vector<int>& os, double& currentScore,
    const GAData& data, GARNG& rng, int moveBudget = 0) {
  bool improved = false;
  bool keepSearching = true;
  int acceptedMoves = 0;
  if (moveBudget <= 0)
    moveBudget = max(1, min(32, data.totalOperations() / 8));
  vector<int> machineOrder(data.machinesNum);
  iota(machineOrder.begin(), machineOrder.end(), 0);
  while (keepSearching && acceptedMoves < moveBudget) {
    keepSearching = false;
    auto schedule = gaDecodeOne(ms, os, data);
    rng.shuffle(machineOrder);
    bool accepted = false;
    for (int machine : machineOrder) {
      const auto& intervals = schedule.machineIntervals[machine];
      if (intervals.size() < 2) continue;
      int blockStart = 0;
      while (blockStart + 1 < static_cast<int>(intervals.size())) {
        int blockEnd = blockStart;
        while (blockEnd + 1 < static_cast<int>(intervals.size()) &&
               intervals[blockEnd + 1].begin == intervals[blockEnd].end)
          ++blockEnd;
        if (blockEnd > blockStart) {
          vector<pair<int, int>> tryPairs;
          tryPairs.emplace_back(blockEnd - 1, blockEnd);
          if (blockEnd - blockStart >= 2)
            tryPairs.emplace_back(blockStart, blockStart + 1);
          for (const auto& tryPair : tryPairs) {
            const int leftIdx = tryPair.first;
            const int rightIdx = tryPair.second;
            if (!gaCanSwapAdjacentOnMachine(
                    schedule, data, machine, leftIdx, rightIdx))
              continue;
            auto candidateOS = os;
            const auto& left = intervals[leftIdx];
            const auto& right = intervals[rightIdx];
            const int leftPos = gaJobOpOSPosition(
                candidateOS, left.job, left.operation);
            const int rightPos = gaJobOpOSPosition(
                candidateOS, right.job, right.operation);
            if (leftPos < data.lockedOSPrefixLength ||
                rightPos < data.lockedOSPrefixLength) continue;
            swap(candidateOS[leftPos], candidateOS[rightPos]);
            const auto candidateSchedule =
                gaDecodeOne(ms, candidateOS, data);
            const double candidateScore = gaEvaluate(candidateSchedule, data);
            if (candidateScore < currentScore) {
              os = std::move(candidateOS);
              currentScore = candidateScore;
              keepSearching = true;
              improved = true;
              accepted = true;
              ++acceptedMoves;
              break;
            }
          }
        }
        if (accepted) break;
        blockStart = blockEnd + 1;
      }
      if (accepted) break;
    }
  }
  return improved;
}

void gaApplyVNS(
    vector<vector<int>>& ms, vector<vector<int>>& os,
    vector<double>& decodeResults, const vector<int>& indices,
    const GAData& data, const vector<vector<int>>& msPositions,
    GARNG& rng) {
  bool hasMachineAlternates = false;
  bool hasMultiOperationJobs = false;
  for (int job = 0; job < data.jobsNum; ++job) {
    if (data.jobsOperations[job] > 1) hasMultiOperationJobs = true;
    for (int op = 0; op < data.jobsOperations[job]; ++op)
      if (data.candidateMachineCount[job][op] > 1)
        hasMachineAlternates = true;
  }
  for (int index : indices) {
    double score = decodeResults[index];
    // Neighborhood 1 only changes machine assignment, so it is pointless
    // when every operation has a single eligible machine.
    if (hasMachineAlternates)
      gaTryVNSOne(ms[index], os[index], score, data, msPositions, rng);
    // For a pure one-operation-per-job dispatching model, adjacent swaps are
    // already explored efficiently by OS crossover/mutation. Repeated
    // first-improvement swaps can require hundreds of full decodes.
    if (hasMultiOperationJobs)
      gaTryVNSTwo(ms[index], os[index], score, data, rng);
    decodeResults[index] = gaEvaluate(gaDecodeOne(ms[index], os[index], data), data);
  }
}

// Stage 2 search. A few first-improvement rounds on one exported chromosome:
// move one operation onto another eligible machine, or swap two adjacent
// operations on the same machine. The caller passes a fully constrained
// decoder, so resource calendars, routing precedence and internal WIP are
// all scored. Only a strictly better weighted score is kept. JIT runs
// afterwards on the accepted chromosome.
void gaPolishFinalChromosome(
    vector<int>& ms, vector<int>& os, double& score, const GAData& data,
    const vector<vector<int>>& msPositions, GARNG& rng, int candidate,
    int candidateCount) {
  if (ms.empty() || os.empty()) return;
  bool hasMachineAlternates = false;
  bool hasMultiOperationJobs = false;
  for (int job = 0; job < data.jobsNum; ++job) {
    if (data.jobsOperations[job] > 1) hasMultiOperationJobs = true;
    for (int op = 0; op < data.jobsOperations[job]; ++op)
      if (data.candidateMachineCount[job][op] > 1)
        hasMachineAlternates = true;
  }
  const double before = score;
  int roundsUsed = 0;
  for (int round = 0; round < 3; ++round) {
    const double roundStart = score;
    ++roundsUsed;
    if (hasMachineAlternates)
      gaTryVNSOne(ms, os, score, data, msPositions, rng, 8);
    if (hasMultiOperationJobs)
      gaTryVNSTwo(ms, os, score, data, rng, 8);
    if (score >= roundStart - 1e-9) break;
  }
  logger << "GA stage2: candidate " << candidate << "/" << candidateCount
         << " neighborhood rounds=" << roundsUsed
         << " score " << before << " -> " << score << "\n";
}

struct GAPopulation {
  explicit GAPopulation(GAData dataIn, uint64_t seed)
      : data(std::move(dataIn)),
        length(data.totalOperations()),
        rng(seed) {}

  GAData data;
  int length = 0;
  int size = 0;
  int globalSize = 0;
  int localSize = 0;
  int randomSize = 0;
  vector<vector<int>> ms;
  vector<vector<int>> os;
  vector<int> bestMS;
  vector<int> bestOS;
  double bestScore = numeric_limits<double>::max();
  int bestStep = -1;
  GARNG rng;

  void initial(int requestedSize) {
    size = max(3, requestedSize);
    bestMS.clear();
    bestOS.clear();
    bestScore = numeric_limits<double>::max();
    bestStep = -1;

    const int realSize = size;
    globalSize = max(1, realSize / 3);
    localSize = max(1, realSize / 3);
    randomSize = realSize - globalSize - localSize;
    if (randomSize < 1) {
      randomSize = 1;
      if (globalSize > localSize)
        --globalSize;
      else
        --localSize;
    }
    initialOSMS();

    // Compute the fixed normalization baseline from this initial population
    // (covers global + local + random strategies) and keep it constant for the
    // whole run so fitness values stay comparable across generations.
    {
      double mkMin = numeric_limits<double>::max(), mkMax = -mkMin;
      double cvMin = numeric_limits<double>::max(), cvMax = -cvMin;
      double tdMin = numeric_limits<double>::max(), tdMax = -tdMin;
      for (int i = 0; i < size; ++i) {
        const auto s = gaDecodeOne(ms[i], os[i], data);
        const double mk = static_cast<double>(s.makespan);
        const double cv = gaBalanceCV(s, data);
        const double td = gaTardinessObjective(s, data);
        if (mk < mkMin) mkMin = mk;
        if (mk > mkMax) mkMax = mk;
        if (cv < cvMin) cvMin = cv;
        if (cv > cvMax) cvMax = cv;
        if (td < tdMin) tdMin = td;
        if (td > tdMax) tdMax = td;
      }
      // Degenerate ranges (all individuals identical on a dimension) get a unit
      // span so that dimension contributes 0 instead of dividing by zero.
      if (mkMax - mkMin < 1e-9) mkMax = mkMin + 1.0;
      if (cvMax - cvMin < 1e-9) cvMax = cvMin + 1.0;
      if (tdMax - tdMin < 1e-9) tdMax = tdMin + 1.0;
      data.normBaseline.mkMin = mkMin;
      data.normBaseline.mkMax = mkMax;
      data.normBaseline.cvMin = cvMin;
      data.normBaseline.cvMax = cvMax;
      data.normBaseline.tdMin = tdMin;
      data.normBaseline.tdMax = tdMax;
    }

    const auto scores = gaDecodePopulation(ms, os, data);
    const auto inverseMeanScore = [&scores](int begin, int end) {
      if (begin < 0) begin = 0;
      if (end > static_cast<int>(scores.size()))
        end = static_cast<int>(scores.size());
      if (end <= begin) return 0.0;
      const double total =
          accumulate(scores.begin() + begin, scores.begin() + end, 0.0);
      return 1.0 / max(1e-12, total / static_cast<double>(end - begin));
    };
    const double globalScore = inverseMeanScore(0, globalSize);
    const double localScore =
        inverseMeanScore(globalSize, globalSize + localSize);
    const double randomScore =
        inverseMeanScore(globalSize + localSize, size);
    const double strategyScore = globalScore + localScore + randomScore;
    if (strategyScore <= 0.0) return;
    globalSize = static_cast<int>(ceil(
        realSize * globalScore / strategyScore));
    localSize = static_cast<int>(ceil(
        (realSize - globalSize) * localScore / (localScore + randomScore)));
    randomSize = realSize - localSize - globalSize;
    size = realSize;
    initialOSMS();
  }

  void initialOSMS() {
    ms.assign(size, vector<int>(length, 0));
    os.assign(size, vector<int>(length, 0));
    gaInitialOS(os, data, rng);
    const auto detailInf = gaInitialJobsOperationsDetail(data);
    const auto msPositions = gaBuildMSPositions(data);
    vector<int> jobsOrder(data.jobsNum);
    iota(jobsOrder.begin(), jobsOrder.end(), 0);
    gaInitialMSGlobalSelection(
        ms, 0, globalSize, msPositions, jobsOrder, data, detailInf, rng);
    vector<int> msTemplate(length, 0);
    gaInitialMSLocalSelection(
        ms, msTemplate, globalSize, globalSize + localSize, data, detailInf);
    gaInitialMSRandomSelection(
        ms, globalSize + localSize, size, data, rng);
  }

  GARunResult runGA(const GASolverConfig& config) {
    auto start = chrono::steady_clock::now();
    int noNewBest = 0;
    const auto detailInf = gaInitialJobsOperationsDetail(data);
    const auto msPositions = gaBuildMSPositions(data);
    const auto bestMSByDuration = gaBuildBestMS(detailInf, data);
    vector<vector<int>> memoryMS(
        static_cast<size_t>(ceil(size * config.memorySize)),
        vector<int>(length, 0));
    vector<vector<int>> memoryOS(
        static_cast<size_t>(ceil(size * config.memorySize)),
        vector<int>(length, 0));
    vector<double> memoryResults(memoryMS.size(), 1e18);
    const int vnsNum = static_cast<int>(ceil(size * config.vnsRatio));

    string stopReason = "generations";
    int stepsRun = 0;
    for (int step = 0; step < config.maxStep; ++step) {
      stepsRun = step + 1;
      const double elapsedSeconds =
          chrono::duration<double>(chrono::steady_clock::now() - start).count();
      if (config.timeLimitSeconds > 0.0 &&
          elapsedSeconds >= config.timeLimitSeconds) {
        stopReason = "time_limit";
        break;
      }

      // NSGA-II: front rank + crowding for parent selection / survival.
      // Weighted scalar (rawScores) is only for archive / VNS / tie-break.
      // Calendar decoding stays inside gaDecodeOne for every evaluation.
      auto evaluation = gaEvaluatePopulationCached(ms, os, data);
      auto nsgaInfo = gaNSGA2Rank(evaluation);
      auto decodeResults = gaNSGA2SelectionScore(nsgaInfo);
      auto rawResults = evaluation.rawScores;
      const bool lateByStep = step > config.maxStep * 0.7;
      const bool lateByStagnation = noNewBest > config.maxNoNewBest / 2;
      const bool lateByTime =
          config.timeLimitSeconds > 0.0 &&
          elapsedSeconds > config.timeLimitSeconds * 0.7;
      if (config.enableVNS && (lateByStep || lateByStagnation || lateByTime)) {
        vector<int> sortedIndices(decodeResults.size(), 0);
        iota(sortedIndices.begin(), sortedIndices.end(), 0);
        sort(sortedIndices.begin(), sortedIndices.end(),
             [&](int lhs, int rhs) {
               return decodeResults[lhs] < decodeResults[rhs];
             });
        sortedIndices.resize(
            min(vnsNum, static_cast<int>(sortedIndices.size())));
        gaApplyVNS(
            ms, os, rawResults, sortedIndices, data, msPositions, rng);
        evaluation = gaEvaluatePopulationCached(ms, os, data);
        nsgaInfo = gaNSGA2Rank(evaluation);
        decodeResults = gaNSGA2SelectionScore(nsgaInfo);
        rawResults = evaluation.rawScores;
      }

      const int bestIndex = static_cast<int>(
          min_element(rawResults.begin(), rawResults.end()) -
          rawResults.begin());
      if (rawResults[bestIndex] < bestScore) {
        bestScore = rawResults[bestIndex];
        bestStep = step + 1;
        bestMS = ms[bestIndex];
        bestOS = os[bestIndex];
        noNewBest = 0;
      } else {
        ++noNewBest;
      }

      if (config.memorySize > 0.0 && !memoryResults.empty())
        gaUpdateMemoryLib(
            memoryMS, memoryOS, memoryResults, ms, os, decodeResults);
      // Early stop: no improvement for stagnation_limit consecutive generations.
      if (noNewBest >= config.maxNoNewBest) {
        stopReason = "stagnation";
        break;
      }

      vector<vector<int>> newMS(size, vector<int>(length, 0));
      vector<vector<int>> newOS(size, vector<int>(length, 0));
      if (config.useRandomTournament) {
        gaTournamentRandom(
            ms, os, newMS, newOS, config.tournamentM,
            decodeResults, data, rng);
      } else if (config.useMemoryTournament && config.memorySize > 0.0 &&
                 !memoryMS.empty()) {
        gaTournamentMemory(
            ms, os, memoryMS, memoryOS, newMS, newOS,
            config.tournamentM, decodeResults, memoryResults,
            step, config.maxStep, rng);
      } else {
        gaTournament(
            ms, os, newMS, newOS, config.tournamentM, decodeResults, rng);
      }

      vector<float> crossoverP =
          config.useAutoCrossover
              ? gaGetCrossoverP(
                    decodeResults,
                    *min_element(decodeResults.begin(), decodeResults.end()))
              : vector<float>(size, static_cast<float>(config.crossover));
      if (config.useUniformMSCrossover)
        gaUniformCrossover(newMS, crossoverP, rng);
      if (config.useRandomOSCrossover)
        gaRandomCrossover(newOS, crossoverP, data, rng);
      else if (config.usePoxOSCrossover)
        gaPoxCrossover(newOS, crossoverP, data, rng);

      vector<float> mutationP(crossoverP.size(), 0.0f);
      for (size_t i = 0; i < crossoverP.size(); ++i)
        mutationP[i] =
            static_cast<float>(crossoverP[i] * config.mutationScale);
      if (config.useBestMSMutation)
        gaBestMSMutations(
            newMS, mutationP, msPositions, bestMSByDuration, data, rng);
      else if (config.useRandomMSMutation)
        gaRandomMSMutations(newMS, mutationP, msPositions, data, rng);
      if (config.useRandomOSMutation)
        gaRandomMutation(newOS, mutationP, data, rng);

      // Elitist NSGA-II survival (μ+λ): merge parents + offspring, keep the
      // first complete non-dominated fronts (crowding on the last front).
      ms.insert(ms.end(), make_move_iterator(newMS.begin()),
                make_move_iterator(newMS.end()));
      os.insert(os.end(), make_move_iterator(newOS.begin()),
                make_move_iterator(newOS.end()));
      auto children = gaEvaluatePopulationCached(
          vector<vector<int>>(ms.begin() + size, ms.end()),
          vector<vector<int>>(os.begin() + size, os.end()), data);
      evaluation.schedules.insert(evaluation.schedules.end(),
          make_move_iterator(children.schedules.begin()),
          make_move_iterator(children.schedules.end()));
      evaluation.objectives.insert(evaluation.objectives.end(),
          make_move_iterator(children.objectives.begin()),
          make_move_iterator(children.objectives.end()));
      evaluation.rawScores.insert(evaluation.rawScores.end(),
          make_move_iterator(children.rawScores.begin()),
          make_move_iterator(children.rawScores.end()));
      evaluation.feasible.insert(evaluation.feasible.end(),
          make_move_iterator(children.feasible.begin()),
          make_move_iterator(children.feasible.end()));
      gaNSGA2Survive(ms, os, size, evaluation);
    }

    // Stage 2 receives the first N Pareto solutions. Front rank comes first,
    // then the weighted score. Each candidate is polished and right-shifted
    // on its own; the exported plan is the best score after that repair.
    const auto finalEvaluation = gaEvaluatePopulationCached(ms, os, data);
    const auto finalNSGA = gaNSGA2Rank(finalEvaluation);
    const auto& finalRaw = finalEvaluation.rawScores;
    vector<int> ranked(finalNSGA.size());
    iota(ranked.begin(), ranked.end(), 0);
    sort(ranked.begin(), ranked.end(), [&](int left, int right) {
      if (finalNSGA[left].front != finalNSGA[right].front)
        return finalNSGA[left].front < finalNSGA[right].front;
      return finalRaw[left] < finalRaw[right];
    });
    const int stage2Count = min(
        getEnvInt("GA_STAGE2_SOLUTIONS", 1),
        static_cast<int>(ranked.size()));
    GADecodedSchedule finalSchedule;
    int keptCandidate = 0;
    if (stage2Count > 0) {
      double bestRefined = numeric_limits<double>::max();
      for (int n = 0; n < stage2Count; ++n) {
        const int index = ranked[n];
        vector<int> candidateMS = ms[index];
        vector<int> candidateOS = os[index];
        // Few chromosomes reach this point, so each neighborhood trial also
        // walks the routing predecessor tree. Stage 1 already enforces the
        // internal WIP ledger when the operations carry material.
        data.fastDecode = false;
        double score = gaEvaluate(
            gaDecodeOne(candidateMS, candidateOS, data), data);
        gaPolishFinalChromosome(
            candidateMS, candidateOS, score, data, msPositions, rng,
            n + 1, stage2Count);
        auto schedule = gaDecodeOne(candidateMS, candidateOS, data);
        data.fastDecode = true;
        gaRefineDecodedSchedule(schedule, data, n + 1, stage2Count);
        const double refined = gaEvaluate(schedule, data);
        if (refined < bestRefined) {
          bestRefined = refined;
          bestMS = std::move(candidateMS);
          bestOS = std::move(candidateOS);
          bestScore = refined;
          finalSchedule = std::move(schedule);
          keptCandidate = n + 1;
        }
      }
      logger << "GA stage2: kept candidate " << keptCandidate
             << "/" << stage2Count << "\n";
    } else {
      gGAFinalGateTrace = getEnvBool("GA_GATE_TRACE");
      finalSchedule = gaDecodeOne(bestMS, bestOS, data);
      gGAFinalGateTrace = false;
      gaRefineDecodedSchedule(finalSchedule, data, 1, 1);
    }
    auto end = chrono::steady_clock::now();
    GARunResult result;
    result.bestMS = bestMS;
    result.bestOS = bestOS;
    result.bestMakespan = static_cast<double>(finalSchedule.makespan);
    result.bestCV = gaBalanceCV(finalSchedule, data);
    result.bestTardiness = gaTardiness(finalSchedule, data);
    result.schedule = std::move(finalSchedule);
    result.bestScore = bestScore;
    result.bestStep = bestStep;
    result.stepsRun = stepsRun;
    result.stopReason = stopReason;
    result.runtimeSeconds = chrono::duration<double>(end - start).count();
    return result;
  }
};

GASolverConfig buildGASolverConfig(const GAConfig& config) {
  GASolverConfig ga;
  ga.populationSize = max(1, config.population);
  ga.maxStep = max(1, config.generations);
  ga.maxNoNewBest = max(1, config.stagnation_limit);
  ga.tournamentM = max(1, config.tournament_size);
  ga.memorySize = max(0.0, config.memory_size);
  ga.mutationScale = max(0.0, config.mutation_rate);
  ga.vnsRatio = max(0.0, config.vns_ratio);
  ga.timeLimitSeconds = max(0.0, config.time_limit_seconds);
  ga.useMemoryTournament = config.use_memory_tournament;
  ga.useRandomTournament = config.use_random_tournament;
  ga.useUniformMSCrossover = config.use_uniform_ms_crossover;
  ga.usePoxOSCrossover = config.use_pox_os_crossover;
  ga.useRandomOSCrossover = config.use_random_os_crossover;
  ga.useRandomMSMutation = config.use_random_ms_mutation;
  ga.useBestMSMutation = config.use_best_ms_mutation;
  ga.useRandomOSMutation = config.use_random_os_mutation;
  ga.useAutoCrossover = config.use_auto_crossover;
  ga.enableVNS = config.enable_vns;
  return ga;
}

AntSolution buildAntSolutionFromGASchedule(
    const GAStandardInput& input, const GARunResult& result) {
  AntSolution solution;
  const Date anchor = input.schedulingAnchor;
  unordered_set<OperationPlan*> scheduledOps;

  for (int machine = 0; machine < input.machinesNum; ++machine) {
    const Resource* resource = input.resources[machine];
    auto intervals = result.schedule.machineIntervals[machine];
    sort(intervals.begin(), intervals.end(),
         [](const GAScheduleInterval& lhs, const GAScheduleInterval& rhs) {
           if (lhs.begin != rhs.begin) return lhs.begin < rhs.begin;
           if (lhs.end != rhs.end) return lhs.end < rhs.end;
           if (lhs.job != rhs.job) return lhs.job < rhs.job;
           return lhs.operation < rhs.operation;
         });
    for (const auto& interval : intervals) {
      const int operationIndex =
          input.operationIndexByJobOperation[interval.job][interval.operation];
      if (operationIndex < 0 ||
          operationIndex >= static_cast<int>(input.operations.size()))
        continue;
      const auto& operation = input.operations[operationIndex];
      OperationPlan* op = operation.op;
      if (!op || operation.options.empty()) continue;
      int optionIndex = result.schedule.selectedOption
          [interval.job][interval.operation];
      if (optionIndex < 0 ||
          optionIndex >= static_cast<int>(operation.options.size()))
        optionIndex = 0;
      const auto& option = operation.options[optionIndex];
      const RelativeTimeWindow mapped =
          mapRelativeWindow(anchor, interval.begin, interval.end);
      const Date decodedStart = result.schedule.calendarBegin
          [interval.job][interval.operation];
      const Date decodedEnd = result.schedule.calendarEnd
          [interval.job][interval.operation];

      solution.sequences[resource].push_back(op);
      const Date setupStart = result.schedule.calendarSetupBegin
          [interval.job][interval.operation];
      if (op && setupStart != Date::infinitePast)
        solution.setupStarts[op] = setupStart;
      solution.startDates[resource].push_back(
          decodedStart != Date::infinitePast ? decodedStart : mapped.start);
      solution.endDates[resource].push_back(
          decodedEnd != Date::infinitePast ? decodedEnd : mapped.end);
      solution.selectedResources[op] =
          option.allResources.empty()
              ? vector<const Resource*>{resource}
              : option.allResources;
      solution.selectedLoadAssignments[op] = option.loadAssignments;
      if (option.selectedOperation)
        solution.selectedOperations[op] = option.selectedOperation;
      scheduledOps.insert(op);
    }
  }

  solution.unscheduledCount =
      input.operations.size() > scheduledOps.size()
          ? input.operations.size() - scheduledOps.size()
          : 0;
  if (solution.unscheduledCount > 0)
    solution.unscheduledPenalty = 500.0 * solution.unscheduledCount;
  solution.internalMaterialFeasible = result.schedule.materialFeasible;
  solution.fitness = -result.bestScore;

  // Calendar mapping now happens in gaDecodeOne. No post-decode retiming is
  // permitted: objectives and exported dates must remain identical.
#if 0
  // Historical CCPL post-processing. It is intentionally compiled out: it
  // produced a second timeline after GA had already evaluated the first.
  {
    struct Row {
      OperationPlan* op;
      const Resource* resource;
      Date desired;
      Date start;
      Date end;
      int stage;
    };
    vector<Row> rows;
    unordered_set<const Buffer*> internal;
    unordered_set<const Item*> internalItems;
    for (const auto* op : scheduledOps)
      for (auto fp = op->beginFlowPlans(); fp != op->endFlowPlans(); ++fp)
        if (fp->getBuffer() && fp->getQuantity() > ROUNDING_ERROR) {
          internal.insert(fp->getBuffer());
          if (fp->getBuffer()->getItem())
            internalItems.insert(fp->getBuffer()->getItem());
        }
    for (const auto& entry : solution.sequences) {
      const Resource* resource = entry.first;
      const auto& starts = solution.startDates[resource];
      for (size_t i = 0; i < entry.second.size(); ++i)
        rows.push_back({entry.second[i], resource, starts[i], starts[i],
                        starts[i], parseCCPLStage(entry.second[i])});
    }
    sort(rows.begin(), rows.end(), [](const Row& a, const Row& b) {
      if (a.stage != b.stage) return a.stage < b.stage;
      if (a.desired != b.desired) return a.desired < b.desired;
      return a.op->getReference() < b.op->getReference();
    });
    struct MaterialEvent { Date date; double quantity; };
    unordered_map<const Buffer*, vector<MaterialEvent>> events;
    unordered_map<const Item*, vector<MaterialEvent>> itemEvents;
    unordered_map<const Resource*, Date> resourceFree;
    // Destination buffers of proposed internal distribution orders. They
    // bridge an item produced at one location and consumed at the next line
    // station. The order itself is created by the normal MRP network; GA only
    // supplies the forward receipt event when its source material is released.
  unordered_map<const Buffer*, vector<const Buffer*>> distributionDestinations;
  for (auto plan = OperationPlan::begin(); plan != OperationPlan::end(); ++plan) {
      if (!plan->getOperation() ||
          !plan->getOperation()->hasType<OperationItemDistribution>())
        continue;
      const Buffer* source = nullptr;
      const Buffer* destination = nullptr;
      for (auto fp = plan->beginFlowPlans(); fp != plan->endFlowPlans(); ++fp) {
        if (!fp->getBuffer()) continue;
        if (fp->getQuantity() < -ROUNDING_ERROR) source = fp->getBuffer();
        if (fp->getQuantity() > ROUNDING_ERROR) destination = fp->getBuffer();
      }
      if (source && destination && internal.count(source))
        distributionDestinations[source].push_back(destination);
    }
    for (auto& entry : distributionDestinations) {
      auto& destinations = entry.second;
      sort(destinations.begin(), destinations.end());
      destinations.erase(unique(destinations.begin(), destinations.end()),
                         destinations.end());
    }
    auto materialReady = [&](OperationPlan* op, Date requested) {
      Date ready = requested;
      for (auto fp = op->beginFlowPlans(); fp != op->endFlowPlans(); ++fp) {
        const Buffer* buf = fp->getBuffer();
        if (!buf || fp->getQuantity() >= -ROUNDING_ERROR ||
            !internalItems.count(buf->getItem())) continue;
        double balance = buf->getOnHand(input.schedulingAnchor, false);
        vector<MaterialEvent> timeline;
        // CCPL routes can cross a zero-lead-time distribution from one
        // location buffer to the next. For the forward feasibility ledger,
        // production of that item is available to the receiving buffer at the
        // same event time; the actual DO is still retained by MRP.
        const Item* item = buf->getItem();
        if (item) {
          const auto byItem = itemEvents.find(item);
          if (byItem != itemEvents.end())
            timeline = byItem->second;
        } else
          timeline = events[buf];
        sort(timeline.begin(), timeline.end(), [](const auto& a, const auto& b) {
          return a.date < b.date;
        });
        size_t idx = 0;
        while (idx < timeline.size() && timeline[idx].date <= ready)
          balance += timeline[idx++].quantity;
        while (balance + ROUNDING_ERROR < -fp->getQuantity() &&
               idx < timeline.size()) {
          const Date eventDate = timeline[idx].date;
          while (idx < timeline.size() && timeline[idx].date == eventDate)
            balance += timeline[idx++].quantity;
          if (balance + ROUNDING_ERROR >= -fp->getQuantity())
            ready = max(ready, eventDate);
        }
        // Internal WIP is a hard condition. Never silently retain the
        // requested date when the accumulated production is still short.
        if (balance + ROUNDING_ERROR < -fp->getQuantity())
          return Date::infiniteFuture;
      }
      return ready;
    };
    for (auto& row : rows) {
      Date earliest = row.desired;
      auto available = resourceFree.find(row.resource);
      if (available != resourceFree.end()) earliest = max(earliest, available->second);
      earliest = materialReady(row.op, earliest);
      if (earliest == Date::infiniteFuture)
        throw DataException("GA quantity flow line has an unsatisfied internal material dependency");
      const Duration duration = estimateOperationDuration(row.op, row.resource);
      const DateRange actual = row.op->getOperation()->calculateOperationTime(
          row.op, earliest, duration, true);
      row.start = actual.getStart();
      row.end = actual.getEnd();
      resourceFree[row.resource] = row.end;
      for (auto fp = row.op->beginFlowPlans(); fp != row.op->endFlowPlans(); ++fp) {
        if (!fp->getBuffer() || !internal.count(fp->getBuffer()) ||
            fabs(fp->getQuantity()) <= ROUNDING_ERROR)
          continue;
        events[fp->getBuffer()].push_back(
            {fp->getQuantity() < 0.0 ? row.start : row.end, fp->getQuantity()});
        if (fp->getBuffer()->getItem())
          itemEvents[fp->getBuffer()->getItem()].push_back(
              {fp->getQuantity() < 0.0 ? row.start : row.end,
               fp->getQuantity()});
        if (fp->getQuantity() > ROUNDING_ERROR) {
          auto destinations = distributionDestinations.find(fp->getBuffer());
          if (destinations != distributionDestinations.end())
            for (const Buffer* destination : destinations->second)
              events[destination].push_back({row.end, fp->getQuantity()});
        }
      }
    }
    unordered_map<OperationPlan*, pair<Date, Date>> mapped;
    for (const auto& row : rows) mapped[row.op] = {row.start, row.end};
    for (auto& entry : solution.sequences) {
      auto& starts = solution.startDates[entry.first];
      auto& ends = solution.endDates[entry.first];
      for (size_t i = 0; i < entry.second.size(); ++i) {
        const auto found = mapped.find(entry.second[i]);
        if (found != mapped.end()) {
          starts[i] = found->second.first;
          ends[i] = found->second.second;
        }
      }
    }
  }
#endif
  return solution;
}

// ---------------------------------------------------------------------------
// Former SolverACO anonymous-namespace helpers, copied for GA decoupling.
// ---------------------------------------------------------------------------

struct ACOMaterialEvent {
  const Buffer* buffer = nullptr;
  Date date;
  double quantity = 0.0;
};

// Simplified from SolverACO::materialEventsForOperation: the transfer-batch
// branch is dropped (GA never enables transfer batches), so a flow plan's
// event date is the operation start for consuming flows and the end for
// producing flows.
vector<ACOMaterialEvent> materialEventsForOperation(const OperationPlan* op,
                                                    Date start, Date end) {
  vector<ACOMaterialEvent> events;
  if (!op) return events;
  for (auto fp = op->beginFlowPlans(); fp != op->endFlowPlans(); ++fp) {
    const Buffer* buf = fp->getBuffer();
    if (!buf || fabs(fp->getQuantity()) <= ROUNDING_ERROR) continue;
    Date eventDate = fp->getQuantity() < 0.0 ? start : end;
    events.push_back({buf, eventDate, fp->getQuantity()});
  }
  return events;
}

void syncOwnerTimeEnvelope(OperationPlan* op) {
  for (OperationPlan* owner = op ? op->getOwner() : nullptr; owner;
       owner = owner->getOwner()) {
    Date start = Date::infiniteFuture;
    Date end = Date::infinitePast;
    bool hasChild = false;
    for (auto child = owner->getSubOperationPlans();
         child != OperationPlan::end(); ++child) {
      if (child->getStart() != Date::infinitePast &&
          child->getStart() < start)
        start = child->getStart();
      if (child->getEnd() != Date::infiniteFuture &&
          child->getEnd() > end)
        end = child->getEnd();
      hasChild = true;
    }
    if (!hasChild || start == Date::infiniteFuture ||
        end == Date::infinitePast)
      continue;
    owner->setStartForce(start);
    owner->setEndForce(end);
  }
}

void syncAllRoutingTimeEnvelopes() {
  // Direct rescheduling can move a leaf whose alternate/routing container
  // isn't itself in the GA chromosome.  Recompute every container bottom-up
  // after writeback so exported precedence checks see the child timeline,
  // rather than the stale MRP envelope.
  vector<OperationPlan*> owners;
  for (auto plan = OperationPlan::begin(); plan != OperationPlan::end(); ++plan)
    if (plan->getSubOperationPlans() != OperationPlan::end())
      owners.push_back(&*plan);
  sort(owners.begin(), owners.end(), [](const OperationPlan* a,
                                       const OperationPlan* b) {
    int da = 0, db = 0;
    for (auto p = a->getOwner(); p; p = p->getOwner()) ++da;
    for (auto p = b->getOwner(); p; p = p->getOwner()) ++db;
    return da > db;
  });
  for (OperationPlan* owner : owners) {
    Date start = Date::infiniteFuture;
    Date end = Date::infinitePast;
    for (auto child = owner->getSubOperationPlans();
         child != OperationPlan::end(); ++child) {
      start = min(start, child->getStart());
      end = max(end, child->getEnd());
    }
    if (start != Date::infiniteFuture && end != Date::infinitePast) {
      owner->setStartForce(start);
      owner->setEndForce(end);
    }
  }
}

bool shouldLockOptimizedPlan(const OperationPlan* op) {
  if (!op || !op->getOperation()) return false;
  return !op->getOperation()->hasType<OperationDelivery, OperationItemSupplier,
                                      OperationItemDistribution>();
}

void lockOptimizedPlan(OperationPlan* op) {
  if (!op) return;
  if (shouldLockOptimizedPlan(op)) op->setAcoLocked(true);
  auto* top = op->getTopOwner();
  if (top != op && shouldLockOptimizedPlan(top)) top->setAcoLocked(true);
}

}  // namespace

// ==========================================================================
// Python bindings
// ==========================================================================

static PyObject* solverGA_setRunMRP(PyObject* self, PyObject* args) {
  auto* solver = static_cast<SolverGA*>(self);
  int val;
  if (!PyArg_ParseTuple(args, "p", &val)) return nullptr;
  solver->setRunMRP(val != 0);
  Py_RETURN_NONE;
}

static PyObject* solverGA_getRunMRP(PyObject* self, PyObject*) {
  auto* solver = static_cast<SolverGA*>(self);
  return PyBool_FromLong(solver->getRunMRP() ? 1 : 0);
}

static PyObject* solverGA_setSeed(PyObject* self, PyObject* args) {
  auto* solver = static_cast<SolverGA*>(self);
  unsigned int val;
  if (!PyArg_ParseTuple(args, "I", &val)) return nullptr;
  solver->setSeed(val);
  Py_RETURN_NONE;
}

static PyObject* solverGA_getSeed(PyObject* self, PyObject*) {
  auto* solver = static_cast<SolverGA*>(self);
  return PyLong_FromUnsignedLong(solver->getSeed());
}

static PyObject* solverGA_setPurchaseMaterialMode(
    PyObject* self, PyObject* args) {
  auto* solver = static_cast<SolverGA*>(self);
  int val;
  if (!PyArg_ParseTuple(args, "i", &val)) return nullptr;
  if (val < 0 || val > 1) {
    PyErr_SetString(PyExc_ValueError,
                    "Expected 0 (infinite) or 1 (leadtime)");
    return nullptr;
  }
  solver->setPurchaseMaterialMode(val);
  Py_RETURN_NONE;
}

static PyObject* solverGA_getPurchaseMaterialMode(PyObject* self, PyObject*) {
  auto* solver = static_cast<SolverGA*>(self);
  return PyLong_FromLong(solver->getPurchaseMaterialMode());
}

PyObject* run_ga(PyObject*, PyObject*) {
  SolverGA ga;
  ga.setRunMRP(false);
  ga.setLogLevel(getEnvInt("GA_LOGLEVEL", 0, 0));
  GAConfig cfg = ga.getGAConfig();
  cfg.seed = static_cast<unsigned int>(getEnvInt("GA_SEED", cfg.seed, 0));
  cfg.population = getEnvInt("GA_POPULATION", cfg.population);
  cfg.generations = getEnvInt("GA_GENERATIONS", cfg.generations);
  cfg.stagnation_limit =
      getEnvInt("GA_STAGNATION_LIMIT", cfg.stagnation_limit);
  cfg.enable_vns = getEnvBool("GA_ENABLE_VNS", cfg.enable_vns);
  cfg.vns_ratio = getEnvDouble("GA_VNS_RATIO", cfg.vns_ratio);
  cfg.time_limit_seconds = getEnvDouble(
      "GA_TIME_LIMIT_SECONDS", cfg.time_limit_seconds, 0.0);
  const string objectiveProfile = []() {
    const char* value = getenv("GA_OBJECTIVE_PROFILE");
    return value ? string(value) : string("due_date");
  }();
  if (objectiveProfile == "makespan") {
    // Pure Cmax — for fair comparison with classic FJSP GA (e.g. Numba).
    cfg.objective_makespan_weight = 1.0;
    cfg.objective_balance_weight = 0.0;
    cfg.objective_tardiness_weight = 0.0;
  } else if (objectiveProfile == "efficiency") {
    cfg.objective_makespan_weight = 0.80;
    cfg.objective_balance_weight = 0.10;
    cfg.objective_tardiness_weight = 0.10;
  } else if (objectiveProfile == "balance") {
    cfg.objective_makespan_weight = 0.20;
    cfg.objective_balance_weight = 0.65;
    cfg.objective_tardiness_weight = 0.15;
  } else if (objectiveProfile != "due_date") {
    logger << "Warning: Unknown ga.objective_profile '" << objectiveProfile
           << "', using due_date\n";
    cfg.objective_makespan_weight = 0.80;
    cfg.objective_balance_weight = 0.10;
    cfg.objective_tardiness_weight = 0.10;
  } else {
    cfg.objective_makespan_weight = 0.80;
    cfg.objective_balance_weight = 0.10;
    cfg.objective_tardiness_weight = 0.10;
  }
  // Explicit env weights override the profile (experiment / A-B switches).
  if (getenv("GA_OBJECTIVE_MAKESPAN_WEIGHT"))
    cfg.objective_makespan_weight = getEnvDouble(
        "GA_OBJECTIVE_MAKESPAN_WEIGHT", cfg.objective_makespan_weight);
  if (getenv("GA_OBJECTIVE_BALANCE_WEIGHT"))
    cfg.objective_balance_weight = getEnvDouble(
        "GA_OBJECTIVE_BALANCE_WEIGHT", cfg.objective_balance_weight);
  if (getenv("GA_OBJECTIVE_TARDINESS_WEIGHT"))
    cfg.objective_tardiness_weight = getEnvDouble(
        "GA_OBJECTIVE_TARDINESS_WEIGHT", cfg.objective_tardiness_weight);
  cfg.material_check_mode =
      getMaterialCheckModeFromEnv(cfg.material_check_mode);
  ga.setConfig(cfg);
  void* v = nullptr;
  // CCPL lines are independently split MOs linked by WIP. Optimizing only
  // the resource that happened to be a current MRP bottleneck loses the
  // upstream quantity events. Detect their demanded finished product and
  // run exactly one all-resource joint decode; buildGAStandardInput filters
  // the candidate set to the actual OP-01..OP-N plans.
  const auto finishedItems = demandedFinishedItems();
  bool hasDemandFinishedCCPLPlan = false;
  bool hasCCPLPlan = false;
  unsigned int ccplStageCount = 0;
  for (auto op = OperationPlan::begin(); op != OperationPlan::end(); ++op)
    if (parseCCPLStage(&*op) > 0) {
      hasCCPLPlan = true;
      ++ccplStageCount;
      if (isDemandFinishedOperation(&*op, finishedItems))
        hasDemandFinishedCCPLPlan = true;
    }
  // Some CCPL source documents bind the last process operation to its WIP
  // input instead of the finished output item. A complete OP-01..OP-N chain
  // is still a flow line and must use the all-resource material decoder.
  const bool hasCompleteCCPLFlowLine = hasDemandFinishedCCPLPlan ||
      ccplStageCount >= 2;
  if (hasCompleteCCPLFlowLine) {
    vector<const Resource*> resources;
    for (auto resource = Resource::begin(); resource != Resource::end();
         ++resource)
      if (!resource->isGroup() && resource->getConstrained())
        resources.push_back(&*resource);
    ga.solveJoint(resources);
  } else
    ga.solve(v);
  // POSTCHECK is deliberately split in two phases:
  //   1. gaDecodeOne has already made internally produced WIP a hard FIFO
  //      feasibility condition. Never "repair" that by moving MOs after
  //      selection: GA's objectives must describe the exported timeline.
  //   2. External procurement and distribution are post-processing supply.
  //      Their movable proposals are synchronized to the selected demand
  //      dates only after the GA result has been written.
  if (hasCCPLPlan) {
    ga.synchronizeProposedPurchases();
    ga.synchronizeProposedDistributions();
  }
  auto* cmdMgr = ga.getCommandManager();
  if (cmdMgr) cmdMgr->commit();
  if (hasCompleteCCPLFlowLine) {
    // Commands generated by applyBestSolution only become visible in the
    // model's Buffer::flowplanlist after this first commit.  Run the WIP
    // guard now (not during applyBestSolution) so its cumulative scan sees
    // the actual exported GA dates rather than the old MRP timeline.
    ga.enforceInternalMaterialAvailability();
    ga.synchronizeProposedDistributions();
    if (cmdMgr) cmdMgr->commit();
  }
  // WIP repair / DO sync can still shift manufacturing receipts.  Re-align
  // deliveries for both CCPL and FJSP GA paths.
  ga.alignDeliveryCommitments();
  if (cmdMgr) cmdMgr->commit();
  // Moving plans through CommandManager can recreate the original MRP
  // capacity trace after applyBestSolution has cleaned it.  The joint CCPL
  // decoder has already verified the final calendar, so remove only those
  // post-commit traces belonging to OP-* resources.  This deliberately does
  // not touch material, lead-time, or non-CCPL resource constraints.
  if (hasCompleteCCPLFlowLine) {
    for (auto& demand : Demand::all()) {
      auto& constraints = demand.getConstraints();
      for (auto constraint = constraints.begin();
           constraint != constraints.end();) {
        if (!constraint->hasType<ProblemCapacityOverload>()) {
          ++constraint;
          continue;
        }
        auto* overload = static_cast<ProblemCapacityOverload*>(&*constraint);
        Resource* resource = overload->getResource();
        const string name = resource ? resource->getName() : string();
        if (name.size() <= 3 || name.compare(0, 3, "OP-") != 0) {
          ++constraint;
          continue;
        }
        auto* stale = &*constraint;
        constraints.unlink(stale);
        ++constraint;
        delete stale;
      }
      constraints.clean(&demand);
    }
  }
  Py_RETURN_NONE;
}

int SolverGA::initialize() {
  metadata = MetaClass::registerClass<SolverGA>("solver", "solver_ga",
                                                Object::create<SolverGA>);
  registerFields<SolverGA>(const_cast<MetaClass*>(metadata));
  auto& x = PythonExtension<SolverGA>::getPythonType();
  x.setName("solverGA");
  x.setDoc("ccAPPS genetic algorithm solver");
  x.supportgetattro();
  x.supportsetattro();
  x.supportcreate(SolverGA::create);

  x.addMethod("solve",
              static_cast<PyObject* (*)(PyObject*, PyObject*, PyObject*)>(
                  SolverCreate::solve),
              METH_VARARGS, "run the solver");
  x.addMethod("commit", SolverCreate::commit, METH_NOARGS,
              "commit the plan changes");
  x.addMethod("rollback", SolverCreate::rollback, METH_NOARGS,
              "rollback the plan changes");
  x.addMethod("markAutofence", SolverCreate::markAutofence, METH_NOARGS,
              "mark the autofence of buffers");

  x.addMethod("setRunMRP", solverGA_setRunMRP, METH_VARARGS,
              "Enable/disable MRP propagation after GA sequencing");
  x.addMethod("getRunMRP", solverGA_getRunMRP, METH_NOARGS,
              "Check if MRP propagation is enabled");
  x.addMethod("setSeed", solverGA_setSeed, METH_VARARGS,
              "Set deterministic GA random seed");
  x.addMethod("getSeed", solverGA_getSeed, METH_NOARGS,
              "Get deterministic GA random seed");
  x.addMethod("setPurchaseMaterialMode", solverGA_setPurchaseMaterialMode,
              METH_VARARGS,
              "Set GA purchase material mode: 0=infinite, 1=leadtime");
  x.addMethod("getPurchaseMaterialMode", solverGA_getPurchaseMaterialMode,
              METH_NOARGS, "Get GA purchase material mode");

  SolverGA::metadata->setPythonClass(x);
  return x.typeReady();
}

PyObject* SolverGA::create(PyTypeObject*, PyObject*, PyObject* kwds) {
  try {
    auto* s = new SolverGA();

    if (kwds) {
      PyObject *key, *value;
      Py_ssize_t pos = 0;
      while (PyDict_Next(kwds, &pos, &key, &value)) {
        PythonData field(value);
        PyObject* key_utf8 = PyUnicode_AsUTF8String(key);
        DataKeyword attr(PyBytes_AsString(key_utf8));
        Py_DECREF(key_utf8);
        const MetaFieldBase* fmeta = s->getType().findField(attr.getHash());
        if (!fmeta) fmeta = s->getType().category->findField(attr.getHash());
        if (!fmeta) fmeta = Solver::metadata->findField(attr.getHash());
        if (fmeta)
          fmeta->setField(s, field);
        else
          s->setProperty(attr.getName(), value);
      }
    }

    return static_cast<PyObject*>(s);
  } catch (...) {
    PythonType::evalException();
    return nullptr;
  }
}

// ==========================================================================
// Configuration bridge to reused ACO evaluator/helpers
// ==========================================================================

void SolverGA::setConfig(const GAConfig& cfg) {
  config_ = cfg;
  rng_.seed(config_.seed);
}

// ==========================================================================
// Former SolverACO member helpers, copied in to decouple GA from ACO.
// These were inherited protected methods; they now live entirely in GA.
// ==========================================================================

Date SolverGA::purchaseMaterialAvailable(const Buffer* buf) const {
  if (!buf || !buf->getItem()) return Date::infiniteFuture;
  Date current = Plan::instance().getCurrent();
  Date earliest = Date::infiniteFuture;

  Item* item = buf->getItem();
  while (item) {
    Item::supplierlist::const_iterator supitem_iter =
        item->getSupplierIterator();
    while (ItemSupplier* supitem = supitem_iter.next()) {
      if (!supitem->getPriority()) continue;
      if (!supitem->getEffective().within(current)) continue;

      // Match the same location applicability rule used when buffers create
      // purchase operations from item suppliers.
      if (supitem->getLocation()) {
        if ((buf->getLocation() && buf->getLocation() != supitem->getLocation()) ||
            !buf->getLocation())
          continue;
      }

      Date available = config_.purchase_material_mode == 0
          ? current
          : current + supitem->getLeadTime();
      if (available < earliest) earliest = available;
    }
    item = item->getOwner();
  }

  return earliest;
}

Date SolverGA::earliestStart(const OperationPlan* op, const Resource* res) const {
  (void)res;  // kept for interface parity with the former ACO method
  Date earliest = Plan::instance().getCurrent();

  // Postcheck explicitly optimizes capacity only. Material is re-netted by
  // the MRP refresh after the GA schedule has been applied.
  if (config_.material_check_mode == GAConfig::POSTCHECK) return earliest;

  // Check each consuming flowplan — find the latest upstream completion date.
  for (auto fp = op->beginFlowPlans(); fp != op->endFlowPlans(); ++fp) {
    if (fp->getQuantity() >= 0.0) continue;  // skip producing flows
    Buffer* buf = fp->getBuffer();
    if (!buf) continue;
    double requiredQty = -fp->getQuantity();
    if (requiredQty <= ROUNDING_ERROR) continue;
    double currentOnhand =
        buf->getOnHand(Plan::instance().getCurrent(), false);
    if (currentOnhand + ROUNDING_ERROR >= requiredQty)
      continue;  // enough on hand now
    // Precheck mode only accepts real inventory and existing receipt events.
    // It must never invent a purchasable date: a shortage then remains a
    // genuine failure rather than a fictitiously feasible chromosome.
    // Scan flowplans to find the first date where cumulative supply covers the
    // required quantity.
    double balance = currentOnhand;
    for (auto sfp = buf->getFlowPlans().begin();
         sfp != buf->getFlowPlans().end(); ++sfp) {
      if (sfp->getOperationPlan() == op) continue;
      if (sfp->getDate() <= Plan::instance().getCurrent() ||
          sfp->getDate() == Date::infiniteFuture)
        continue;
      balance += sfp->getQuantity();
      if (balance + ROUNDING_ERROR >= requiredQty) {
        Date available = sfp->getDate();
        OperationPlan* supplier = sfp->getOperationPlan();
        if (supplier && supplier->getEnd() != Date::infiniteFuture &&
            supplier->getEnd() > available)
          available = supplier->getEnd();
        if (available > earliest) earliest = available;
        break;
      }
    }
  }

  return earliest;
}

vector<CandidateOp> SolverGA::buildCandidates(
    const vector<const Resource*>& resources) const {
  vector<CandidateOp> candidates;
  unordered_set<OperationPlan*> processed;
  int nextId = 0;

  for (auto* res : resources) {
    auto loadplans = res->getLoadPlans();
    for (auto it = loadplans.begin(); it != loadplans.end(); ++it) {
      OperationPlan* op = it->getOperationPlan();
      if (!op || op->getQuantity() <= 0.0) continue;
      // Skip non-manufacturing orders (purchases, distributions)
      if (op->getOperation()->hasType<OperationItemSupplier,
                                       OperationItemDistribution>())
        continue;

      if (processed.count(op)) continue;
      processed.insert(op);

      // CCPL flow-line plans already carry their product-specific resource
      // assignment.  OP-12 may have two physical machines, but a particular
      // product/order is mapped to one of them, so this isn't a GA choice.
      if (parseCCPLStage(op) > 0) {
        CandidateOp c;
        c.op = op;
        for (auto lp = op->beginLoadPlans(); lp != op->endLoadPlans(); ++lp) {
          if (!lp->isStart() || !lp->getLoad() || !lp->getResource()) continue;
          c.loadAssignments.push_back({lp->getLoad(), lp->getResource()});
          if (find(c.allResources.begin(), c.allResources.end(),
                   lp->getResource()) == c.allResources.end())
            c.allResources.push_back(lp->getResource());
        }
        if (!c.allResources.empty()) {
          c.res = c.allResources.front();
          c.earliestStart = Plan::instance().getCurrent();
          c.candId = nextId++;
          candidates.push_back(move(c));
          continue;
        }
      }

      // Fast path for a fixed flow line.  When every load already points to
      // one concrete resource there is no resource-choice problem to solve:
      // emit the single assignment directly.  The GA then only sequences the
      // batches through the line while the decoder enforces OP-n -> OP-(n+1).
      bool fixedResources = true;
      vector<pair<const Load*, const Resource*>> fixedAssignments;
      vector<const Resource*> fixedResourceList;
      for (const auto& ld : op->getOperation()->getLoads()) {
        const Resource* loadResource = ld.getResource();
        if (!loadResource || loadResource->isGroup() || ld.getSkill()) {
          fixedResources = false;
          break;
        }
        fixedAssignments.push_back({&ld, loadResource});
        if (find(fixedResourceList.begin(), fixedResourceList.end(),
                 loadResource) == fixedResourceList.end())
          fixedResourceList.push_back(loadResource);
      }
      if (fixedResources && !fixedAssignments.empty()) {
        CandidateOp c;
        c.op = op;
        c.res = fixedResourceList.front();
        c.allResources = move(fixedResourceList);
        c.loadAssignments = move(fixedAssignments);
        c.earliestStart = Plan::instance().getCurrent();
        c.candId = nextId++;
        candidates.push_back(move(c));
        continue;
      }

      struct LoadAlternatives {
        const Load* load;
        vector<const Resource*> resources;
      };
      vector<LoadAlternatives> loadAlternatives;
      for (const auto& ld : op->getOperation()->getLoads()) {
        vector<const Resource*> alternatives;
        collectQualifiedResourcesLocal(ld.getResource(), ld.getSkill(),
                                       op->getStart(), op->getEnd(),
                                       alternatives);
        if (alternatives.empty()) continue;
        sort(alternatives.begin(), alternatives.end(),
             [](const Resource* a, const Resource* b) {
               if (a->getEfficiency() == b->getEfficiency())
                 return a->getName() < b->getName();
               return a->getEfficiency() > b->getEfficiency();
             });
        loadAlternatives.push_back({&ld, move(alternatives)});
      }

      if (!loadAlternatives.empty()) {
        vector<pair<const Load*, const Resource*>> currentAssignments;
        vector<const Resource*> currentResources;
        size_t generatedForOp = 0;

        function<void(size_t)> buildCombinations = [&](size_t idx) {
          if (generatedForOp >=
              static_cast<size_t>(config_.max_candidate_combinations_per_op))
            return;
          if (idx == loadAlternatives.size()) {
            CandidateOp c;
            c.op = op;
            c.allResources = currentResources;
            c.loadAssignments = currentAssignments;
            c.res = !c.allResources.empty() ? c.allResources.back() : res;
            // The GA forward-schedules the complete candidate set. Internal
            // WIP dependencies are handled by the material ledger during
            // construction; anchoring to the MRP snapshot here would pin
            // candidates to the old backward schedule.
            c.earliestStart = Plan::instance().getCurrent();
            c.candId = nextId++;
            candidates.push_back(c);
            ++generatedForOp;
            return;
          }

          const auto& alt = loadAlternatives[idx];
          for (auto* altRes : alt.resources) {
            bool alreadyAssigned = false;
            if (alt.load->getResource() && alt.load->getResource()->isGroup() &&
                Plan::instance().getIndividualPoolResources()) {
              for (auto* r : currentResources) {
                if (r == altRes) {
                  alreadyAssigned = true;
                  break;
                }
              }
            }
            if (alreadyAssigned) continue;

            currentAssignments.push_back({alt.load, altRes});
            if (find(currentResources.begin(), currentResources.end(), altRes) ==
                currentResources.end())
              currentResources.push_back(altRes);
            buildCombinations(idx + 1);
            currentAssignments.pop_back();
            bool stillUsed = false;
            for (const auto& assignment : currentAssignments) {
              if (assignment.second == altRes) {
                stillUsed = true;
                break;
              }
            }
            if (!stillUsed)
              currentResources.erase(
                  remove(currentResources.begin(), currentResources.end(), altRes),
                  currentResources.end());
          }
        };

        size_t before = candidates.size();
        buildCombinations(0);
        if (candidates.size() > before) continue;
      }

      {
        CandidateOp c;
        c.op = op;
        c.res = res;
        c.allResources.push_back(res);
        c.earliestStart = Plan::instance().getCurrent();
        c.candId = nextId;
        candidates.push_back(c);
      }
      ++nextId;
    }
  }
  return candidates;
}

bool SolverGA::hasMaterialShortage(const AntSolution& sol) const {
  unordered_set<const OperationPlan*> seen;
  struct EvalOp {
    const OperationPlan* op;
    Date start;
    Date end;
  };
  vector<EvalOp> evalOps;

  for (const auto& kv : sol.sequences) {
    const auto& seq = kv.second;
    const auto& starts = sol.startDates.at(kv.first);
    const auto& ends = sol.endDates.at(kv.first);
    for (size_t i = 0; i < seq.size(); ++i) {
      const OperationPlan* op = seq[i];
      if (!op || seen.count(op)) continue;
      seen.insert(op);
      evalOps.push_back({op, starts[i], ends[i]});
    }
  }

  sort(evalOps.begin(), evalOps.end(), [](const EvalOp& a, const EvalOp& b) {
    if (a.start != b.start) return a.start < b.start;
    if (a.end != b.end) return a.end < b.end;
    return a.op < b.op;
  });

  struct MaterialEvent {
    const Buffer* buffer;
    Date date;
    double quantity;
  };
  vector<MaterialEvent> events;
  unordered_set<const Buffer*> touchedBuffers;

  for (const auto& e : evalOps) {
    vector<ACOMaterialEvent> opEvents =
        materialEventsForOperation(e.op, e.start, e.end);
    for (const auto& event : opEvents) {
      if (!event.buffer || fabs(event.quantity) <= ROUNDING_ERROR) continue;
      touchedBuffers.insert(event.buffer);
      events.push_back({event.buffer, event.date, event.quantity});
    }
  }

  Date current = Plan::instance().getCurrent();
  unordered_map<const Buffer*, double> balance;
  for (const Buffer* buf : touchedBuffers) {
    if (!buf) continue;
    balance[buf] = buf->getOnHand(current, false);
    for (auto fp = buf->getFlowPlans().begin();
         fp != buf->getFlowPlans().end(); ++fp) {
      OperationPlan* fpOp = fp->getOperationPlan();
      if (fpOp && seen.count(fpOp)) continue;
      if (fp->getDate() <= current || fp->getDate() == Date::infiniteFuture)
        continue;
      if (fabs(fp->getQuantity()) <= ROUNDING_ERROR) continue;
      events.push_back({buf, fp->getDate(), fp->getQuantity()});
    }
  }

  sort(events.begin(), events.end(), [](const MaterialEvent& a,
                                        const MaterialEvent& b) {
    if (a.date != b.date) return a.date < b.date;
    if (a.buffer != b.buffer) return a.buffer < b.buffer;
    return a.quantity > b.quantity;  // production before consumption at same timestamp
  });

  for (const auto& event : events) {
    double& bal = balance[event.buffer];
    if (event.quantity < 0.0 &&
        bal + event.quantity < -ROUNDING_ERROR)
      return true;
    bal += event.quantity;
  }
  return false;
}

void SolverGA::applyBestSolution(const AntSolution& best) {
  const auto applyStarted = chrono::steady_clock::now();
  if (getLogLevel() >= 0)
    logger << indentlevel << "GA timing: applying best solution\n";
  auto* cmdMgr = getCommandManager();
  const bool quantityFlowLine = isQuantityFlowLineSolution(best);
  // Track which operations we've already processed (multi-resource ops
  // appear in multiple resource sequences but should only be moved once).
  unordered_set<const OperationPlan*> processed;
  struct ApplyRow {
    const Resource* resource;
    OperationPlan* op;
    Date start;
    Date end;
  };
  vector<ApplyRow> applyRows;
  for (const auto& kv : best.sequences) {
    const Resource* seqRes = kv.first;
    const auto& seq = kv.second;
    const auto& starts = best.startDates.at(seqRes);
    const auto& ends = best.endDates.at(seqRes);
    for (size_t i = 0; i < seq.size(); ++i)
      applyRows.push_back({seqRes, seq[i], starts[i], ends[i]});
  }
  if (quantityFlowLine)
    stable_sort(applyRows.begin(), applyRows.end(), [](const ApplyRow& a,
                                                        const ApplyRow& b) {
      const int aStage = parseCCPLStage(a.op);
      const int bStage = parseCCPLStage(b.op);
      if (aStage != bStage) return aStage < bStage;
      if (a.start != b.start) return a.start < b.start;
      return a.op->getReference() < b.op->getReference();
    });
  for (const auto& row : applyRows) {
      const Date start = row.start;
      const Date end = row.end;
      OperationPlan* current = row.op;
      if (processed.count(current)) continue;
      processed.insert(current);
      if (isGALockedOperationPlan(current)) continue;
      if (start == Date::infiniteFuture || end == Date::infiniteFuture)
        continue;
      OperationPlan* op = current;
      // A CCPL GA pass is a pure sequencing pass over the supply network
      // which MRP has already created.  Moving an operationplan through the
      // generic model API can return a partial material reply and silently
      // shrink an integer tray (128 -> 127.777...). Preserve the original
      // batch quantity after its calendar move; material feasibility is
      // independently enforced by the GA cumulative FlowPlan ledger.
      const double originalQuantity = op->getQuantity();
      const bool preserveCCPLQuantity = parseCCPLStage(op) > 0;
      // gaDecodeOne has already performed the complete forward allocation on
      // real calendars, including its shadow capacity reservations.  Do not
      // serialize it again here: a second resource-by-resource pass loses
      // the material-ledger order and makes the exported plan differ from
      // the fitness that the GA selected.
      Date appliedStart = start;
      const auto setupIt = best.setupStarts.find(op);
      if (setupIt != best.setupStarts.end() &&
          setupIt->second != Date::infinitePast &&
          setupIt->second < appliedStart)
        appliedStart = setupIt->second;

      auto oit = best.selectedOperations.find(op);
      if (parseCCPLStage(op) < 1 &&
          oit != best.selectedOperations.end() && oit->second &&
          op->getOperation() != oit->second)
        op->setOperation(oit->second);

      // Apply exact load-to-resource assignments selected by the GA
      auto lit = best.selectedLoadAssignments.find(op);
      if (lit != best.selectedLoadAssignments.end()) {
        for (const auto& la : lit->second) {
          for (auto lp = op->beginLoadPlans(); lp != op->endLoadPlans(); ++lp) {
            if (lp->isStart() && lp->getLoad() == la.first &&
                lp->getResource() != la.second) {
              lp->setResource(const_cast<Resource*>(la.second), false, false);
              break;
            }
          }
        }
      }

      if (cmdMgr) {
        // CommandMoveOperationPlan re-runs the model timing calculation.
        // A calculated boundary can be rounded down by one second on a
        // calendar transition, even though the decoded shadow reservation
        // ended exactly at that boundary.  Move the next plan from the
        // decoded window and let the command's normal ``later`` semantics
        // preserve the non-overlap invariant at those boundaries.
        auto* move = new CommandMoveOperationPlan(
            // Dates were already mapped to the real calendar in gaDecodeOne.
            // Preserve that decoded window: writeback must not schedule it a
            // second time with a different date calculation.
            op, appliedStart, end, -1.0, false, true, false);
        if (preserveCCPLQuantity &&
            fabs(op->getQuantity() - originalQuantity) > ROUNDING_ERROR)
          move->setQuantity(originalQuantity);
        cmdMgr->add(move);
      } else {
        op->setOperationPlanParameters(
            op->getQuantity(), appliedStart, end, false, true, false);
        if (preserveCCPLQuantity &&
            fabs(op->getQuantity() - originalQuantity) > ROUNDING_ERROR)
          op->setQuantity(originalQuantity);
      }
      syncOwnerTimeEnvelope(op);
  }
  if (quantityFlowLine) {
    // SolverCreate records overloads while it creates this supply network
    // backwards.  The quantity flow-line GA subsequently writes the same
    // network forward on the real resource calendars, so those trace
    // constraints no longer describe the final timeline.  Remove only
    // capacity traces attached to demands in this verified CCPL network.
    // Lead-time, material and constraints of any other demand are retained.
    unordered_set<const Resource*> flowResources;
    for (const auto& row : applyRows) {
      if (!row.op || parseCCPLStage(row.op) < 1) continue;
      if (row.resource) flowResources.insert(row.resource);
      // ApplyRow::resource is only the primary sequence resource.  A CCPL
      // operation can carry its real assignment in selectedResources or in
      // a load plan (resource pools and multi-load operations), so use the
      // final model assignment as the authoritative cleanup scope.
      auto selected = best.selectedResources.find(row.op);
      if (selected != best.selectedResources.end())
        for (const Resource* resource : selected->second)
          if (resource) flowResources.insert(resource);
      for (auto load = row.op->beginLoadPlans();
           load != row.op->endLoadPlans(); ++load)
        if (load->getResource()) flowResources.insert(load->getResource());
    }
    // These MOs are independent supply plans, rather than children of a
    // delivery operation, and therefore getTopOwner()->getDemand() is null.
    // Match their trace by resource instead: only erase overloads on a
    // resource that GA just calendar-scheduled, and only for a run made
    // entirely of CCPL stages.
    for (auto& demand : Demand::all()) {
      Demand* demandPtr = &demand;
      auto& constraints = demandPtr->getConstraints();
      for (auto constraint = constraints.begin();
           constraint != constraints.end();) {
        if (!constraint->hasType<ProblemCapacityOverload>()) {
          ++constraint;
          continue;
        }
        auto* overload = static_cast<ProblemCapacityOverload*>(&*constraint);
        const Resource* resource = overload->getResource();
        bool ccplResource = flowResources.count(resource) != 0;
        // Some capacity traces are attached to the resource object created
        // during backward expansion and don't retain the exact load-plan
        // pointer used by the forward pass.  CCPL resources have a stable
        // OP-xx naming convention; use it only for a quantity flow-line run
        // so unrelated resource constraints remain untouched.
        if (!ccplResource && resource) {
          const string name = resource->getName();
          ccplResource = name.size() > 3 && name.compare(0, 3, "OP-") == 0;
        }
        if (!ccplResource) {
          ++constraint;
          continue;
        }
        auto* stale = &*constraint;
        constraints.unlink(stale);
        ++constraint;
        delete stale;
      }
      constraints.clean(demandPtr);
    }

    // GA post-processing for a WIP flow line. All manufacturing dates above
    // now reflect the selected chromosome, so proposed inter-location DOs
    // can be synchronized from the real source FlowPlan quantities. This is
    // intentionally not the generic MRP shortage refresh: rerunning MRP
    // here would rebuild a local manufacturing network and overwrite the
    // full forward GA schedule we have just applied.
    synchronizeProposedDistributions();

    // The chromosome is decoded against a shadow FIFO ledger, but moving an
    // OperationPlan through the model can still alter a boundary by a
    // calendar/quantity rounding increment.  Validate the *exported*
    // FlowPlan ledger before committing it.  This is not a second scheduling
    // strategy: it is a forward-only guard that moves a downstream WIP
    // consumer to the next actual cumulative receipt when the model's own
    // timeline says its complete batch isn't available yet.
    //
    // In particular it fixes the uneven 150 -> 128 tails on the CCPL line:
    // after consuming the first 128, the next 128 cannot start until the
    // subsequent 150-piece OP-02 receipt, rather than merely after the
    // first receipt became visible.
  }
  syncAllRoutingTimeEnvelopes();
  // Manufacturing dates are final for this chromosome writeback.  Re-pin
  // deliveries to max(FG receipt, due) so a delayed MO cannot leave a stale
  // early (or late) MRP promise on the batch buffer.
  alignDeliveryCommitments();
  for (const auto& row : applyRows)
    if (row.op) lockOptimizedPlan(row.op);
  if (getLogLevel() >= 0)
    logger << indentlevel << "GA timing: apply completed in "
           << chrono::duration<double>(chrono::steady_clock::now() - applyStarted).count()
           << "s\n";
  const char* verifyPath = getenv("GA_DUMP_APPLY_VERIFY");
  if (verifyPath && *verifyPath) {
    ofstream out(verifyPath, ios::out | ios::trunc);
    if (out.is_open()) {
      out << "{\n  \"moved\": [\n";
      bool first = true;
      unordered_set<const OperationPlan*> reported;
      for (const auto& entry : best.sequences) {
        for (OperationPlan* op : entry.second) {
          if (!op || !reported.insert(op).second) continue;
          if (!first) out << ",\n";
          first = false;
          out << "    {\"reference\": " << jsonString(op->getReference())
              << ", \"operation\": " << jsonString(operationName(op))
              << ", \"start\": " << jsonString(dateString(op->getStart()))
              << ", \"end\": " << jsonString(dateString(op->getEnd())) << "}";
        }
      }
      out << "\n  ]\n}\n";
    }
  }
}

void SolverGA::runGAMRP(void* v) {
  SolverCreate mrp;
  mrp.setCluster(getCluster());
  mrp.setConstraints(getConstraints());
  mrp.setPlanType(getPlanType());
  mrp.setLogLevel(getLogLevel());
  mrp.setAutocommit(getAutocommit());
  mrp.setCreateDeliveries(getCreateDeliveries());
  mrp.setPropagate(getPropagate());
  mrp.setBatchGrouping(getBatchGrouping());
  mrp.setRotateResources(getRotateResources());
  mrp.setAdministrativeLeadTime(getAdministrativeLeadTime());
  mrp.setLazyDelay(getLazyDelay());
  mrp.setMinimumDelay(getMinimumDelay());
  mrp.setIterationThreshold(getIterationThreshold());
  mrp.setIterationAccuracy(getIterationAccuracy());
  mrp.setIterationMax(getIterationMax());
  mrp.setResourceIterationMax(getResourceIterationMax());
  mrp.setErasePreviousFirst(getErasePreviousFirst());
  mrp.setUserExitFlow(getUserExitFlow());
  mrp.setUserExitDemand(getUserExitDemand());
  mrp.setUserExitNextDemand(getUserExitNextDemand());
  mrp.setUserExitBuffer(getUserExitBuffer());
  mrp.setUserExitResource(getUserExitResource());
  mrp.setUserExitOperation(getUserExitOperation());
  mrp.solve(v);
}

vector<const Buffer*> SolverGA::collectShortageBuffers() const {
  vector<const Buffer*> result;
  unordered_set<const Buffer*> gaProducedBuffers;
  unordered_set<const Buffer*> gaConsumedBuffers;
  for (auto op = OperationPlan::begin(); op != OperationPlan::end(); ++op) {
    if (!isGAEligibleOperationPlan(&*op)) continue;
    for (auto fp = op->beginFlowPlans(); fp != op->endFlowPlans(); ++fp) {
      if (!fp->getBuffer()) continue;
      if (fp->getQuantity() > ROUNDING_ERROR)
        gaProducedBuffers.insert(fp->getBuffer());
      else if (fp->getQuantity() < -ROUNDING_ERROR)
        gaConsumedBuffers.insert(fp->getBuffer());
    }
  }
  for (const auto& buf : Buffer::all()) {
    const Operation* producing = buf.getProducingOperation();
    // The postcheck refresh owns external, movable supply only. Internal
    // manufacturing remains the GA network and must never be deleted here.
    if (buf.hasType<BufferInfinite>() || !producing ||
        !gaConsumedBuffers.count(&buf) || gaProducedBuffers.count(&buf) ||
        !producing->hasType<OperationItemSupplier,
                              OperationItemDistribution>())
      continue;
    for (const auto& event : buf.getFlowPlans()) {
      if (event.getDate() < Plan::instance().getCurrent()) continue;
      if (event.getOnhand() < -ROUNDING_ERROR) {
        result.push_back(&buf);
        break;
      }
    }
  }
  sort(result.begin(), result.end(), [](const Buffer* a, const Buffer* b) {
    if (a->getLevel() != b->getLevel()) return a->getLevel() < b->getLevel();
    return a->getName() < b->getName();
  });
  return result;
}

void SolverGA::refreshProposedSupply(const vector<const Buffer*>& buffers) {
  if (buffers.empty()) return;

  // Delete the previous MRP proposals only on the affected buffer supply
  // paths. Locked/approved/confirmed/completed/closed orders survive because
  // deleteOperationPlans(false) removes proposed plans only.
  unordered_set<const Operation*> cleared;
  for (const auto* buffer : buffers) {
    const Operation* operation = buffer ? buffer->getProducingOperation() : nullptr;
    if (operation && cleared.insert(operation).second)
      const_cast<Operation*>(operation)->deleteOperationPlans(false);
  }

  SolverData refresh(this, -1, nullptr);
  refresh.setCommandManager(getCommandManager());
  refresh.solveSelectedBufferShortages(buffers);
}

void SolverGA::refreshPostcheckSupply() {
  if (config_.material_check_mode != GAConfig::POSTCHECK) return;
  refreshProposedSupply(collectShortageBuffers());
}

void SolverGA::enforceInternalMaterialAvailability() {
  // Reuse the MRP material model directly. Buffer::flowplanlist is the
  // authoritative, date-sorted timeline and already maintains onhand and
  // cumulative produced/consumed values, including FlowTransferBatch dates.
  // GA only supplies a different operation sequence; it must not create a
  // second, subtly different inventory ledger.
  auto* cmdMgr = getCommandManager();
  // A WIP can be produced at the line-side location and then moved by a DO
  // to the consuming location.  Determine WIP at item level, not from the
  // immediate producer of the destination buffer (which is then the DO).
  unordered_set<string> producedWipItems;
  for (auto plan = OperationPlan::begin(); plan != OperationPlan::end(); ++plan) {
    if (parseCCPLStage(&*plan) < 1) continue;
    for (auto flow = plan->beginFlowPlans(); flow != plan->endFlowPlans(); ++flow)
      if (flow->getQuantity() > ROUNDING_ERROR && flow->getBuffer() &&
          flow->getBuffer()->getItem())
        producedWipItems.insert(flow->getBuffer()->getItem()->getName());
  }
  const unsigned int maxMoves = 50000;
  unordered_map<OperationPlan*, unsigned int> moveCount;
  for (unsigned int pass = 0; pass < maxMoves; ++pass) {
    bool moved = false;
    for (auto plan = OperationPlan::begin(); plan != OperationPlan::end(); ++plan) {
      OperationPlan* consumer = &*plan;
      if (parseCCPLStage(consumer) < 1 || !isGAEligibleOperationPlan(consumer) ||
          moveCount[consumer] >= 8)
        continue;
      Date release = consumer->getStart();
      for (auto flow = consumer->beginFlowPlans(); flow != consumer->endFlowPlans(); ++flow) {
        if (flow->getQuantity() >= -ROUNDING_ERROR || !flow->getBuffer()) continue;
        Buffer* buffer = flow->getBuffer();
        // Only enforce internally manufactured WIP. Purchased material is
        // still owned by proposed PO/DO synchronization.
        if (!buffer->getItem() ||
            !producedWipItems.count(buffer->getItem()->getName()))
          continue;
        // The event immediately before this consuming FlowPlan contains the
        // MRP-maintained available onhand.  If it cannot cover this complete
        // consumption, scan the same timeline forward until a positive event
        // makes the real onhand nonnegative.  This covers uneven 120/128
        // tails without treating the partial 120 as a complete batch.
        auto pos = buffer->getFlowPlans().begin(&*flow);
        // getOnHand(date) includes the current consumption event.  Walk the
        // subsequent FlowPlans with our own running balance: FlowPlan's
        // cached onhand is calculated before the GA writeback transaction and
        // can therefore describe the old MRP timeline.
        double available = buffer->getOnHand(flow->getDate(), false);
        // ``available`` is the balance *after* this consumer was applied.
        // A nonnegative balance means the complete input batch was already
        // available at its start.
        if (available >= -ROUNDING_ERROR) continue;
        Date candidate = Date::infiniteFuture;
        for (auto future = pos; future != buffer->getFlowPlans().end(); ++future) {
          if (future->getDate() <= flow->getDate() ||
              fabs(future->getQuantity()) <= ROUNDING_ERROR)
            continue;
          available += future->getQuantity();
          // The complete current batch is feasible once all prior shortage
          // has been replenished.  Do not require a single receipt to be as
          // large as the batch: a 120-piece receipt followed by the next
          // 150-piece receipt is a valid cumulative FIFO release.
          if (available >= -ROUNDING_ERROR) {
            candidate = future->getDate();
            break;
          }
        }
        if (candidate == Date::infiniteFuture) continue;
        release = max(release, candidate);
      }
      if (release <= consumer->getStart()) continue;
      const Duration dur = consumer->getEnd() - consumer->getStart();
      const DateRange mapped = consumer->getOperation()->calculateOperationTime(
          consumer, release, dur, true);
      if (mapped.getStart() <= consumer->getStart()) continue;
      if (cmdMgr) {
        cmdMgr->add(new CommandMoveOperationPlan(
            consumer, mapped.getStart(), mapped.getEnd(), -1.0, false, true, false));
        // The next WIP consumer must be checked against this repaired
        // FlowPlan timeline.  CommandManager deliberately defers model
        // mutation until commit, so queuing all repairs first made every
        // iteration inspect the same obsolete negative balance.
        cmdMgr->commit();
      } else
        consumer->setOperationPlanParameters(consumer->getQuantity(),
            mapped.getStart(), mapped.getEnd(), false, true, false, true);
      ++moveCount[consumer];
      moved = true;
      break;
    }
    if (!moved) return;
  }
  logger << "Warning: GA internal material repair reached its move limit\n";
}

void SolverGA::alignDeliveryCommitments() {
  // GA only rewrites manufacturing (and optionally DO/PO). Delivery plans
  // keep MRP timestamps unless re-pinned here. Same rule as SolverFAS:
  // commitment = max(material receipt, demand due).
  //
  // Prefer the manufacturing order that shares the delivery's batch id.
  // Shared unbatched FG buffers otherwise let an early unrelated lot cover
  // the FIFO check and leave this delivery on its due date while its own
  // batch is still open (temporary shortage on the shared buffer).
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
    // 1) Same-batch manufacturing receipt (authoritative for FJSP/lot splits).
    if (batch && item) {
      for (auto producer = OperationPlan::begin();
           producer != OperationPlan::end(); ++producer) {
        if (&*producer == op || producer->getBatch() != batch) continue;
        Item* producerItem = nullptr;
        for (auto produce = producer->beginFlowPlans();
             produce != producer->endFlowPlans(); ++produce)
          if (produce->getQuantity() > ROUNDING_ERROR && produce->getBuffer() &&
              produce->getBuffer()->getItem()) {
            producerItem = produce->getBuffer()->getItem();
            break;
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
        if (available != Date::infinitePast &&
            available != Date::infiniteFuture)
          materialReady = max(materialReady, available);
      }
    }
    // 2) Fallback: FIFO on the consuming buffer (unbatched / no batch id).
    if (materialReady == Date::infinitePast) {
      for (auto consume = op->beginFlowPlans(); consume != op->endFlowPlans();
           ++consume) {
        if (consume->getQuantity() >= -ROUNDING_ERROR || !consume->getBuffer())
          continue;
        Buffer* buffer = consume->getBuffer();
        double remaining = -consume->getQuantity();
        struct Candidate {
          Date available;
          double quantity;
          string reference;
        };
        vector<Candidate> candidates;
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
                    [](const Candidate& left, const Candidate& right) {
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
    // Force the promise date. setOperationPlanParameters / CommandMove can
    // leave deliveries on the MRP due date (esp. with later=true); this is a
    // zero-duration commitment with no capacity, so stamp it directly.
    op->setStartAndEnd(commitment, commitment);
    ++moved;
  }
  if (moved && getLogLevel() >= 0)
    logger << indentlevel << "GA: aligned " << moved
           << " delivery commitments to max(receipt, due)\n";
}

void SolverGA::synchronizeProposedPurchases() {
  // GA changes manufacturing dates, but the MRP-created PO can still carry
  // its former backward-scheduled receipt date.  Move only an existing
  // proposed PO to the first actual consumption in its destination buffer.
  // No supply network is rebuilt here.
  struct Purchase {
    OperationPlan* plan = nullptr;
    const Buffer* buffer = nullptr;
    Duration leadtime;
  };
  vector<Purchase> purchases;
  unordered_multimap<const Buffer*, const Buffer*> distributionDestinations;
  for (auto plan = OperationPlan::begin(); plan != OperationPlan::end(); ++plan) {
    if (!plan->getProposed() || !plan->getOperation() ||
        !plan->getOperation()->hasType<OperationItemDistribution>())
      continue;
    const Buffer* source = nullptr;
    const Buffer* destination = nullptr;
    for (auto flow = plan->beginFlowPlans(); flow != plan->endFlowPlans(); ++flow) {
      if (!flow->getBuffer()) continue;
      if (flow->getQuantity() < -ROUNDING_ERROR) source = flow->getBuffer();
      else if (flow->getQuantity() > ROUNDING_ERROR) destination = flow->getBuffer();
    }
    if (source && destination) distributionDestinations.emplace(source, destination);
  }
  for (auto plan = OperationPlan::begin(); plan != OperationPlan::end(); ++plan) {
    if (!plan->getProposed() || !plan->getOperation() ||
        !plan->getOperation()->hasType<OperationItemSupplier>())
      continue;
    const Buffer* receipt = nullptr;
    for (auto flow = plan->beginFlowPlans(); flow != plan->endFlowPlans(); ++flow)
      if (flow->getBuffer() && flow->getQuantity() > ROUNDING_ERROR) {
        receipt = flow->getBuffer();
        break;
      }
    if (receipt)
      purchases.push_back({&*plan, receipt, plan->getEnd() - plan->getStart()});
  }
  auto* cmdMgr = getCommandManager();
  const Date current = Plan::instance().getCurrent();
  for (const auto& purchase : purchases) {
    Date firstNeed = Date::infiniteFuture;
    vector<const Buffer*> targets{purchase.buffer};
    const auto linked = distributionDestinations.equal_range(purchase.buffer);
    for (auto it = linked.first; it != linked.second; ++it)
      targets.push_back(it->second);
    for (const Buffer* target : targets)
      for (auto flow = target->getFlowPlans().begin();
           flow != target->getFlowPlans().end(); ++flow) {
        OperationPlan* owner = flow->getOperationPlan();
        if (!owner || owner == purchase.plan ||
            (owner->getOperation() &&
             owner->getOperation()->hasType<OperationItemDistribution>()) ||
            flow->getQuantity() >= -ROUNDING_ERROR || flow->getDate() < current)
          continue;
        firstNeed = min(firstNeed, flow->getDate());
      }
    if (firstNeed == Date::infiniteFuture) continue;
    const Date receipt = max(current, firstNeed);
    const Date start = receipt - purchase.leadtime;
    if (cmdMgr)
      cmdMgr->add(new CommandMoveOperationPlan(
          purchase.plan, start, receipt, -1.0, false, false, false));
    else
      purchase.plan->setOperationPlanParameters(
          purchase.plan->getQuantity(), start, receipt, false, true, false);
  }
}

void SolverGA::synchronizeProposedDistributions() {
  // Do not re-run MRP after the full forward decode: that creates a new
  // partial manufacturing network and overwrites the GA timeline. Instead
  // move only existing, movable distribution orders from their source's
  // actual post-GA material events. Every source ledger starts at its real
  // current on-hand balance and then consumes/produces FlowPlan quantities
  // in timestamp order (receipts before consumption at one timestamp).
  // Approved and confirmed transport commitments remain untouched.
  struct Event { Date date; double quantity; };
  struct Distribution {
    OperationPlan* plan = nullptr;
    const Buffer* source = nullptr;
    const Buffer* destination = nullptr;
    double quantity = 0.0;
    Duration transit;
  };
  vector<Distribution> distributions;
  unordered_set<const OperationPlan*> movable;
  for (auto plan = OperationPlan::begin(); plan != OperationPlan::end(); ++plan) {
    if (!plan->getOperation() || !plan->getProposed() ||
        !plan->getOperation()->hasType<OperationItemDistribution>())
      continue;
    if (plan->getEnd() <= Plan::instance().getCurrent()) continue;
    const Buffer* source = nullptr;
    const Buffer* destination = nullptr;
    double quantity = 0.0;
    for (auto flow = plan->beginFlowPlans(); flow != plan->endFlowPlans(); ++flow)
      if (flow->getBuffer() && flow->getQuantity() < -ROUNDING_ERROR) {
        source = flow->getBuffer();
        quantity = -flow->getQuantity();
      } else if (flow->getBuffer() && flow->getQuantity() > ROUNDING_ERROR)
        destination = flow->getBuffer();
    if (source && quantity > ROUNDING_ERROR) {
      distributions.push_back(
          {&*plan, source, destination, quantity,
           plan->getEnd() - plan->getStart()});
      movable.insert(&*plan);
    }
  }
  sort(distributions.begin(), distributions.end(),
       [](const Distribution& a, const Distribution& b) {
         if (a.plan->getEnd() != b.plan->getEnd())
           return a.plan->getEnd() < b.plan->getEnd();
         return a.plan->getReference() < b.plan->getReference();
       });

  unordered_map<const Buffer*, vector<Event>> events;
  for (const auto& distribution : distributions) {
    if (events.count(distribution.source)) continue;
    auto& timeline = events[distribution.source];
    const Date current = Plan::instance().getCurrent();
    for (auto flow = distribution.source->getFlowPlans().begin();
         flow != distribution.source->getFlowPlans().end(); ++flow) {
      OperationPlan* owner = flow->getOperationPlan();
      if (owner && movable.count(owner)) continue;
      // getOnHand(current) is the opening balance of this ledger: it
      // already includes every event at or before the planning current
      // date.  Adding those events again double-counts initial stock and
      // can make a proposed DO appear feasible before its actual source
      // production.  Only future source events belong in this timeline.
      if (flow->getDate() <= current ||
          flow->getDate() == Date::infiniteFuture ||
          fabs(flow->getQuantity()) <= ROUNDING_ERROR) continue;
      timeline.push_back({flow->getDate(), flow->getQuantity()});
    }
    sort(timeline.begin(), timeline.end(), [](const Event& a, const Event& b) {
      if (a.date != b.date) return a.date < b.date;
      return a.quantity > b.quantity;
    });
  }

  unordered_map<const Buffer*, double> balance;
  unordered_map<const Buffer*, size_t> position;
  const Date current = Plan::instance().getCurrent();
  for (const auto& distribution : distributions)
    if (!balance.count(distribution.source))
      balance[distribution.source] =
          distribution.source->getOnHand(current, false);
  auto* cmdMgr = getCommandManager();
  for (const auto& distribution : distributions) {
    const Buffer* source = distribution.source;
    auto& timeline = events[source];
    size_t& idx = position[source];
    Date available = current;
    while (balance[source] + ROUNDING_ERROR < distribution.quantity &&
           idx < timeline.size()) {
      const Date at = timeline[idx].date;
      while (idx < timeline.size() && timeline[idx].date == at)
        balance[source] += timeline[idx++].quantity;
      available = max(available, at);
    }
    // A source shortage is left for the normal postcheck report; never move
    // a DO to an invented date. Otherwise reserve its exact actual quantity.
    if (balance[source] + ROUNDING_ERROR < distribution.quantity) continue;
    balance[source] -= distribution.quantity;
    // Don't consume source WIP merely because it is already on hand. A
    // movable DO is a just-in-time event: its destination's first actual
    // manufacturing consumption is the lower bound, while source material
    // availability and transit remain hard constraints.
    Date targetNeed = current;
    if (distribution.destination)
      for (auto flow = distribution.destination->getFlowPlans().begin();
           flow != distribution.destination->getFlowPlans().end(); ++flow) {
        OperationPlan* owner = flow->getOperationPlan();
        if (!owner || owner == distribution.plan ||
            (owner->getOperation() &&
             owner->getOperation()->hasType<OperationItemDistribution>()) ||
            flow->getQuantity() >= -ROUNDING_ERROR || flow->getDate() < current)
          continue;
        targetNeed = min(targetNeed == current ? flow->getDate() : targetNeed,
                         flow->getDate());
      }
    const Date end = max(available + distribution.transit, targetNeed);
    const Date start = end - distribution.transit;
    if (cmdMgr)
      cmdMgr->add(new CommandMoveOperationPlan(
          distribution.plan, start, end, -1.0, false, false, false));
    else
      distribution.plan->setOperationPlanParameters(
          distribution.plan->getQuantity(), start, end, false, true, false);
  }
}

// ==========================================================================
// Candidate and chromosome helpers
// ==========================================================================

vector<const Resource*> SolverGA::collectBottlenecks() const {
  vector<const Resource*> result;
  unordered_set<const Resource*> activeGroups;

  auto collectResourcePlansLocal = [](const Resource* res) {
    vector<OperationPlan*> plans;
    auto loadplans = res->getLoadPlans();
    for (auto it = loadplans.begin(); it != loadplans.end(); ++it) {
      OperationPlan* op = it->getOperationPlan();
      if (isGAEligibleOperationPlan(op) &&
          find(plans.begin(), plans.end(), op) == plans.end())
        plans.push_back(op);
    }
    return plans;
  };

  for (auto res = Resource::begin(); res != Resource::end(); ++res) {
    if (!res->getConstrained()) continue;
    if (res->isGroup()) {
      for (auto m = res->getMembers(); m != Resource::end(); ++m) {
        if (!m->isGroup() && m->getConstrained() &&
            collectResourcePlansLocal(&*m).size() >= 2) {
          result.push_back(&*m);
          activeGroups.insert(&*res);
        }
      }
    } else if (collectResourcePlansLocal(&*res).size() >= 2) {
      result.push_back(&*res);
    }
  }

  for (auto* group : activeGroups) {
    for (auto m = group->getMembers(); m != Resource::end(); ++m) {
      if (m->isGroup() || !m->getConstrained()) continue;
      if (find(result.begin(), result.end(), &*m) == result.end())
        result.push_back(&*m);
    }
  }
  return result;
}

vector<CandidateOp> SolverGA::buildSingleResourceCandidates(
    const Resource* res,
    const vector<OperationPlan*>& plans) const {
  vector<CandidateOp> candidates;
  int id = 0;
  for (auto* op : plans) {
    if (!isGAEligibleOperationPlan(op)) continue;
    CandidateOp c;
    c.op = op;
    c.res = res;
    c.earliestStart = earliestStart(op, res);
    if (config_.runMRP && op && op->getStart() > Date::infinitePast)
      c.earliestStart = max(c.earliestStart, op->getStart());
    c.candId = id++;
    c.allResources.push_back(res);
    candidates.push_back(c);
  }
  return candidates;
}

static vector<const Resource*> expandResourcesForCompleteJobs(
    const vector<const Resource*>& resources) {
  // A CCPL OP-01..OP-N flow line has a fixed product-to-machine mapping.
  // All participating stage resources are already present in the bottleneck
  // list, and recursively walking owner networks adds no decision variable.
  bool fixedFlowLine = !resources.empty();
  bool sawPlan = false;
  for (auto* res : resources) {
    if (!res) continue;
    for (auto lp = res->getLoadPlans().begin();
         lp != res->getLoadPlans().end(); ++lp) {
      OperationPlan* op = lp->getOperationPlan();
      if (!isGAEligibleOperationPlan(op)) continue;
      sawPlan = true;
      if (parseCCPLStage(op) < 1) {
        fixedFlowLine = false;
        break;
      }
    }
    if (!fixedFlowLine) break;
  }
  if (fixedFlowLine && sawPlan) {
    vector<const Resource*> result = resources;
    sort(result.begin(), result.end(), [](const Resource* a, const Resource* b) {
      if (!a || !b) return a < b;
      return a->getName() < b->getName();
    });
    result.erase(unique(result.begin(), result.end()), result.end());
    return result;
  }

  vector<const Resource*> result = resources;
  unordered_set<const Resource*> resourceSet(resources.begin(), resources.end());
  unordered_set<OperationPlan*> topOwners;

  auto addResource = [&](const Resource* res) {
    if (!res || resourceSet.count(res)) return;
    resourceSet.insert(res);
    result.push_back(res);
  };

  for (auto* res : resources) {
    auto loadplans = res->getLoadPlans();
    for (auto it = loadplans.begin(); it != loadplans.end(); ++it) {
      OperationPlan* op = it->getOperationPlan();
      if (!isGAEligibleOperationPlan(op)) continue;
      OperationPlan* topOwner = op->getTopOwner();
      if (topOwner) topOwners.insert(topOwner);
    }
  }

  for (auto* topOwner : topOwners) {
    if (!topOwner) continue;
    for (auto child = topOwner->getSubOperationPlans();
         child != OperationPlan::end(); ++child) {
      vector<OperationPlan*> stack{&*child};
      while (!stack.empty()) {
        OperationPlan* op = stack.back();
        stack.pop_back();
        if (!op) continue;
        if (isGAEligibleOperationPlan(op)) {
          for (auto lp = op->beginLoadPlans(); lp != op->endLoadPlans(); ++lp)
            if (lp->isStart()) addResource(lp->getResource());
        }
        for (auto sub = op->getSubOperationPlans();
             sub != OperationPlan::end(); ++sub)
          stack.push_back(&*sub);
      }
    }
  }

  sort(result.begin(), result.end(), [](const Resource* a, const Resource* b) {
    if (!a || !b) return a < b;
    return a->getName() < b->getName();
  });
  return result;
}

namespace {

vector<CandidateOp> expandGAAlternateCandidates(
    SolverGA* solver,
    const vector<const Resource*>& resources,
    const vector<CandidateOp>& baseCandidates) {
  vector<CandidateOp> expanded;
  unordered_set<const Resource*> resourceSet(resources.begin(), resources.end());
  int nextId = 0;

  for (const auto& base : baseCandidates) {
    if (!isGAEligibleOperationPlan(base.op)) continue;

    vector<Operation*> alternatives = collectAlternateLeafOperations(base.op);
    if (alternatives.empty()) alternatives.push_back(base.op->getOperation());

    int sharedId = nextId++;
    // The common flow-line case has no alternate operation.  Preserve the
    // concrete assignments built above instead of performing the same load
    // and resource expansion for a second time.
    if (alternatives.size() == 1 &&
        alternatives.front() == base.op->getOperation()) {
      CandidateOp candidate = base;
      candidate.selectedOperation = alternatives.front();
      candidate.candId = sharedId;
      expanded.push_back(move(candidate));
      continue;
    }
    bool emitted = false;
    for (auto* operation : alternatives) {
      if (!operation) continue;
      CandidateOp candidate = base;
      candidate.selectedOperation = operation;
      candidate.candId = sharedId;
      candidate.allResources.clear();
      candidate.loadAssignments.clear();

      for (const auto& ld : operation->getLoads()) {
        vector<const Resource*> qualified;
        collectQualifiedResourcesLocal(
            ld.getResource(), ld.getSkill(),
            Plan::instance().getCurrent(), Plan::instance().getCurrent(),
            qualified);
        if (qualified.empty()) continue;

        sort(qualified.begin(), qualified.end(),
             [](const Resource* a, const Resource* b) {
               if (a->getEfficiency() == b->getEfficiency())
                 return a->getName() < b->getName();
               return a->getEfficiency() > b->getEfficiency();
             });

        const Resource* chosen = nullptr;
        for (auto* res : qualified) {
          if (resourceSet.count(res)) {
            chosen = res;
            break;
          }
        }
        if (!chosen) chosen = qualified.front();

        candidate.loadAssignments.push_back({&ld, chosen});
        if (find(candidate.allResources.begin(), candidate.allResources.end(),
                 chosen) == candidate.allResources.end())
          candidate.allResources.push_back(chosen);
      }

      if (candidate.allResources.empty()) continue;

      candidate.res = nullptr;
      for (auto* res : candidate.allResources) {
        if (operationUsesPrimaryResource(operation, res)) {
          candidate.res = res;
          break;
        }
      }
      if (!candidate.res) candidate.res = candidate.allResources.front();
      candidate.earliestStart = Plan::instance().getCurrent();
      if (solver->getGAConfig().runMRP &&
          base.op->getStart() > Date::infinitePast)
        candidate.earliestStart =
            max(candidate.earliestStart, base.op->getStart());

      expanded.push_back(std::move(candidate));
      emitted = true;
    }

    if (!emitted) {
      CandidateOp candidate = base;
      candidate.selectedOperation = base.op ? base.op->getOperation() : nullptr;
      candidate.candId = sharedId;
      expanded.push_back(std::move(candidate));
    }
  }

  return expanded;
}

unordered_map<OperationPlan*, Operation*> snapshotOriginalOperations(
    const GAStandardInput& input) {
  unordered_map<OperationPlan*, Operation*> originals;
  for (const auto& operation : input.operations) {
    if (!operation.op || originals.count(operation.op)) continue;
    originals.emplace(operation.op, operation.op->getOperation());
  }
  return originals;
}

void restoreOriginalOperations(
    const unordered_map<OperationPlan*, Operation*>& originals) {
  for (const auto& kv : originals) {
    OperationPlan* op = kv.first;
    Operation* original = kv.second;
    if (!op || op->getOperation() == original) continue;
    op->setOperation(original);
  }
}

bool hasPrecedenceViolation(const AntSolution& solution) {
  unordered_map<const OperationPlan*, pair<Date, Date>> timings;
  unordered_set<const OperationPlan*> seen;
  for (const auto& kv : solution.sequences) {
    const auto& seq = kv.second;
    const auto& starts = solution.startDates.at(kv.first);
    const auto& ends = solution.endDates.at(kv.first);
    for (size_t i = 0; i < seq.size(); ++i) {
      const OperationPlan* op = seq[i];
      if (!op || seen.count(op)) continue;
      seen.insert(op);
      timings.emplace(op, make_pair(starts[i], ends[i]));
    }
  }

  auto lookup = [&](const OperationPlan* op) -> const pair<Date, Date>* {
    auto it = timings.find(op);
    return it == timings.end() ? nullptr : &it->second;
  };

  for (const auto& kv : timings) {
    const OperationPlan* op = kv.first;
    const Date start = kv.second.first;
    const Date end = kv.second.second;

    if (const OperationPlan* next = op->getNextSubOpplan()) {
      if (const auto* nextTiming = lookup(next)) {
        if (end > nextTiming->first + Duration(1L))
          return true;
      }
    }

    for (auto dep : op->getDependencies()) {
      if (!dep || dep->getSecond() != op) continue;
      const OperationPlan* first = dep->getFirst();
      const auto* firstTiming = lookup(first);
      if (!firstTiming) continue;
      Date required = firstTiming->second;
      if (dep->getOperationDependency())
        required += dep->getOperationDependency()->getHardSafetyLeadtime();
      if (required > start + Duration(1L))
        return true;
    }
  }
  return false;
}

// CCPL flow lines are linked by cumulative intermediate-material quantities,
// not by OperationPlan::getNextSubOpplan's one-to-one hierarchy. Their
// quantity constraints are enforced during GA decoding, so applying the old
// hierarchy validator would reject a valid 22 -> 21 -> 24 batch schedule.
bool isQuantityFlowLineSolution(const AntSolution& solution) {
  bool sawOperation = false;
  unordered_set<const OperationPlan*> seen;
  for (const auto& entry : solution.sequences) {
    for (const OperationPlan* op : entry.second) {
      if (!op || !seen.insert(op).second) continue;
      sawOperation = true;
      if (parseCCPLStage(op) < 1) return false;
    }
  }
  return sawOperation;
}

void dumpGAApplyDecisionIfRequested(const AntSolution& solution,
                                    bool baselineKept,
                                    bool materialFeasible,
                                    bool precedenceFeasible) {
  const char* path = getenv("GA_DUMP_APPLY_DECISION");
  if (!path || !*path) return;
  ofstream out(path, ios::out | ios::trunc);
  if (!out.is_open()) return;
  out << "{\n"
      << "  \"baseline_kept\": " << (baselineKept ? "true" : "false") << ",\n"
      << "  \"unscheduled_count\": " << solution.unscheduledCount << ",\n"
      << "  \"material_feasible\": " << (materialFeasible ? "true" : "false") << ",\n"
      << "  \"precedence_feasible\": " << (precedenceFeasible ? "true" : "false") << ",\n"
      << "  \"quantity_flow_line\": "
      << (isQuantityFlowLineSolution(solution) ? "true" : "false") << "\n}\n";
}

void dumpGASolutionIfRequested(const AntSolution& solution,
                               const char* sourceTag) {
  const char* dumpPath = getenv("GA_DUMP_SOLUTION");
  if (!dumpPath || !*dumpPath) return;

  struct SolutionRow {
    OperationPlan* op = nullptr;
    Date start = Date::infinitePast;
    Date end = Date::infinitePast;
  };

  vector<SolutionRow> rows;
  unordered_set<const OperationPlan*> seen;
  for (const auto& kv : solution.sequences) {
    const auto& seq = kv.second;
    const auto& starts = solution.startDates.at(kv.first);
    const auto& ends = solution.endDates.at(kv.first);
    for (size_t i = 0; i < seq.size(); ++i) {
      OperationPlan* op = seq[i];
      if (!op || seen.count(op)) continue;
      seen.insert(op);
      rows.push_back({op, starts[i], ends[i]});
    }
  }

  sort(rows.begin(), rows.end(), [](const SolutionRow& a, const SolutionRow& b) {
    if (a.start != b.start) return a.start < b.start;
    if (a.end != b.end) return a.end < b.end;
    return a.op < b.op;
  });

  ofstream out(dumpPath, ios::out | ios::trunc);
  if (!out.is_open()) return;

  Date planCurrent = Plan::instance().getCurrent();
  out << "{\n";
  out << "  \"source\": " << jsonString(sourceTag ? sourceTag : "") << ",\n";
  out << "  \"plan_current\": " << jsonString(dateString(planCurrent)) << ",\n";
  out << "  \"operation_count\": " << rows.size() << ",\n";
  out << "  \"time_mapping\": {\n";
  out << "    \"anchor\": " << jsonString(dateString(planCurrent)) << ",\n";
  out << "    \"formula\": "
      << jsonString("mapped_time = plan_current + relative_seconds") << "\n";
  out << "  },\n";
  out << "  \"operations\": [\n";

  for (size_t idx = 0; idx < rows.size(); ++idx) {
    const auto& row = rows[idx];
    OperationPlan* op = row.op;
    Demand* dmd = op ? op->getTopOwner()->getDemand() : nullptr;
    const ParsedLabel parsed = parseLabel(operationName(op));
    auto selectedOpIt = solution.selectedOperations.find(op);
    const Operation* selectedOperation =
        selectedOpIt != solution.selectedOperations.end()
        ? selectedOpIt->second : nullptr;
    auto selectedResIt = solution.selectedResources.find(op);
    const vector<const Resource*>* selectedResources =
        selectedResIt != solution.selectedResources.end()
        ? &selectedResIt->second : nullptr;
    const string assignedMachineName =
        selectedResources && !selectedResources->empty()
            ? resourceName((*selectedResources)[0])
            : "";
    out << "    {\n";
    out << "      \"reference\": " << jsonString(opReference(op)) << ",\n";
    out << "      \"operation\": " << jsonString(operationName(op)) << ",\n";
    out << "      \"job_index\": ";
    if (parsed.matched)
      out << parsed.job << ",\n";
    else
      out << "null,\n";
    out << "      \"operation_index\": ";
    if (parsed.matched)
      out << parsed.operation << ",\n";
    else
      out << "null,\n";
    out << "      \"machine_index\": ";
    if (parsed.matched)
      out << parsed.machine << ",\n";
    else
      out << "null,\n";
    out << "      \"assigned_machine_name\": "
        << jsonString(assignedMachineName) << ",\n";
    out << "      \"assigned_machine_index\": ";
    if (assignedMachineName.size() == 4 && assignedMachineName[0] == 'M') {
      out << atoi(assignedMachineName.c_str() + 1) << ",\n";
    } else {
      out << "null,\n";
    }
    out << "      \"selected_operation\": "
        << jsonString(operationDefinitionName(selectedOperation)) << ",\n";
    out << "      \"top_owner\": " << jsonString(topOwnerName(op)) << ",\n";
    out << "      \"demand\": "
        << jsonString(dmd ? dmd->getName() : "") << ",\n";
    out << "      \"start\": " << jsonString(dateString(row.start)) << ",\n";
    out << "      \"end\": " << jsonString(dateString(row.end)) << ",\n";
    out << "      \"start_seconds_from_plan_current\": ";
    if (row.start == Date::infinitePast || row.start == Date::infiniteFuture)
      out << "null,\n";
    else
      out << (row.start - planCurrent).getSeconds() << ",\n";
    out << "      \"relative_start_seconds\": ";
    if (row.start == Date::infinitePast || row.start == Date::infiniteFuture)
      out << "null,\n";
    else
      out << (row.start - planCurrent).getSeconds() << ",\n";
    out << "      \"end_seconds_from_plan_current\": ";
    if (row.end == Date::infinitePast || row.end == Date::infiniteFuture)
      out << "null,\n";
    else
      out << (row.end - planCurrent).getSeconds() << ",\n";
    out << "      \"relative_end_seconds\": ";
    if (row.end == Date::infinitePast || row.end == Date::infiniteFuture)
      out << "null,\n";
    else
      out << (row.end - planCurrent).getSeconds() << ",\n";
    out << "      \"duration_seconds\": ";
    if (row.start == Date::infinitePast || row.end == Date::infinitePast ||
        row.start == Date::infiniteFuture || row.end == Date::infiniteFuture)
      out << "null,\n";
    else
      out << (row.end - row.start).getSeconds() << ",\n";
    out << "      \"resources\": [";
    if (selectedResources) {
      for (size_t r = 0; r < selectedResources->size(); ++r) {
        if (r) out << ", ";
        out << jsonString(resourceName((*selectedResources)[r]));
      }
    }
    out << "]\n";
    out << "    }";
    if (idx + 1 != rows.size()) out << ",";
    out << "\n";
  }

  out << "  ]\n";
  out << "}\n";
}

}  // namespace

AntSolution SolverGA::evolve(
    const vector<const Resource*>& resources,
    const vector<CandidateOp>& candidates,
    const unordered_map<const Resource*, Date>& resourceTimes) {
  const auto evolveStarted = chrono::steady_clock::now();
  GAStandardInput input =
      buildGAStandardInput(resources, candidates, resourceTimes);
  AntSolution bestSolution;
  if (input.operations.empty()) return bestSolution;

  // A CCPL flow line can be completely deterministic: every generated MO has
  // one eligible machine and its material FIFO fixes the stage sequence. In
  // that situation MS contains only zeroes and the decoder deliberately
  // restores the established FIFO order, so a population of chromosomes is
  // identical.  Avoid spending generations repeatedly decoding that same
  // real-calendar schedule.  This is not a heuristic shortcut: use the same
  // decoder and writeback path as a normal GA result.
  bool deterministicFlowLine = !input.operations.empty();
  for (const auto& operation : input.operations) {
    if (parseCCPLStage(operation.op) <= 0 || operation.options.size() != 1) {
      deterministicFlowLine = false;
      break;
    }
  }
  if (deterministicFlowLine) {
    GAData gaData = buildGAData(input, config_);
    vector<int> deterministicMS(gaData.totalOperations(), 0);
    // Locked occurrences are decoded first, in start order, so their machine
    // and material reservations exist before a movable order looks for a slot.
    // The unlocked suffix stays in job order; this path does not shuffle.
    struct LockedRow { int job; Date start; Date end; string reference; };
    vector<LockedRow> lockedRows;
    vector<int> lockedCount(gaData.jobsNum, 0);
    for (int job = 0; job < gaData.jobsNum; ++job)
      for (int op = 0; op < gaData.jobsOperations[job]; ++op) {
        const OperationPlan* plan = gaData.operationPlans[job][op];
        if (!gaData.lockedOperation[job][op] || !plan) continue;
        lockedRows.push_back(
            {job, plan->getStart(), plan->getEnd(), plan->getReference()});
        ++lockedCount[job];
      }
    sort(lockedRows.begin(), lockedRows.end(),
         [](const LockedRow& a, const LockedRow& b) {
           if (a.start != b.start) return a.start < b.start;
           if (a.end != b.end) return a.end < b.end;
           return a.reference < b.reference;
         });
    gaData.lockedOSPrefixLength = lockedRows.size();
    vector<int> deterministicOS;
    deterministicOS.reserve(gaData.totalOperations());
    for (const LockedRow& row : lockedRows) deterministicOS.push_back(row.job);
    for (int job = 0; job < gaData.jobsNum; ++job) {
      const int remain = gaData.jobsOperations[job] - lockedCount[job];
      for (int i = 0; i < remain; ++i) deterministicOS.push_back(job);
    }
    const auto started = chrono::steady_clock::now();
    const auto schedule = gaDecodeOne(deterministicMS, deterministicOS, gaData);
    GARunResult result;
    result.bestMS = std::move(deterministicMS);
    result.bestOS = std::move(deterministicOS);
    result.bestMakespan = static_cast<double>(schedule.makespan);
    result.bestCV = gaBalanceCV(schedule, gaData);
    result.bestTardiness = gaTardiness(schedule, gaData);
    result.bestScore = gaEvaluate(schedule, gaData);
    result.bestStep = 0;
    result.runtimeSeconds =
        chrono::duration<double>(chrono::steady_clock::now() - started).count();
    result.schedule = schedule;
    if (getLogLevel() >= 0)
      logger << indentlevel
             << "GA: deterministic CCPL flow line; skipping population evolution\n";
    bestSolution = buildAntSolutionFromGASchedule(input, result);
    dumpGASolutionIfRequested(bestSolution, "ga");
    lastKeptBaseline_ = false;
    lastBestFitness_ = bestSolution.fitness;
    return bestSolution;
  }

  // Current solver path uses only the ga* GA/ pipeline.
  const auto originalOperations = snapshotOriginalOperations(input);
  GAData gaData = buildGAData(input, config_);
  GAPopulation population(std::move(gaData), config_.seed);
  if (getLogLevel() >= 0)
    logger << indentlevel << "GA timing: input built in "
           << chrono::duration<double>(chrono::steady_clock::now() - evolveStarted).count()
           << "s, jobs=" << input.jobsNum << ", operations="
           << input.operations.size() << "\n";
  const auto initialStarted = chrono::steady_clock::now();
  population.initial(max(1, config_.population));
  if (getLogLevel() >= 0)
    logger << indentlevel << "GA timing: population initialized in "
           << chrono::duration<double>(chrono::steady_clock::now() - initialStarted).count()
           << "s\n";
  const auto runStarted = chrono::steady_clock::now();
  GARunResult gaResult = population.runGA(buildGASolverConfig(config_));
  if (getLogLevel() >= 0) {
    logger << indentlevel << "GA timing: evolution completed in "
           << chrono::duration<double>(chrono::steady_clock::now() - runStarted).count()
           << "s (stop=" << gaResult.stopReason
           << ", steps=" << gaResult.stepsRun
           << ", best_step=" << gaResult.bestStep << ")\n";
  }

  restoreOriginalOperations(originalOperations);
  bestSolution = buildAntSolutionFromGASchedule(input, gaResult);
  dumpGASolutionIfRequested(bestSolution, "ga");
  lastKeptBaseline_ = false;
  lastBestFitness_ = bestSolution.fitness;

  if (getLogLevel() > 0) {
    logger << indentlevel << "GA: score=" << gaResult.bestScore
           << " (makespan=" << gaResult.bestMakespan
           << ", balanceCV=" << gaResult.bestCV
           << ", tardiness=" << gaResult.bestTardiness << ")"
           << ", best_step=" << gaResult.bestStep
           << ", stop=" << gaResult.stopReason
           << ", steps=" << gaResult.stepsRun
           << ", runtime=" << gaResult.runtimeSeconds << "s\n";
  }
  return bestSolution;
}

// ==========================================================================
// Solver entry points
// ==========================================================================

void SolverGA::solve(const Resource* res, void* v) {
  vector<OperationPlan*> plans;
  auto loadplans = res->getLoadPlans();
  for (auto it = loadplans.begin(); it != loadplans.end(); ++it) {
    OperationPlan* op = it->getOperationPlan();
    if (isGAEligibleOperationPlan(op) &&
        find(plans.begin(), plans.end(), op) == plans.end())
      plans.push_back(op);
  }
  if (plans.size() < 2) {
    return;
  }

  vector<const Resource*> resources{res};
  unordered_map<const Resource*, Date> resourceTimes;
  resourceTimes[res] = Plan::instance().getCurrent();
  vector<CandidateOp> rawCandidates = buildSingleResourceCandidates(res, plans);
  GAStandardInput gaInput =
      buildGAStandardInput(resources, rawCandidates, resourceTimes);
  if (gaInput.operations.size() < 2 || gaInput.candidates.empty()) {
    return;
  }

  AntSolution best = evolve(gaInput.resources, gaInput.candidates,
                            gaInput.resourceTimes);
  // POSTCHECK defers only external PO/DO supply. Internal WIP feasibility is
  // a decoder invariant in both modes; PRECHECK additionally validates all
  // external receipts before accepting the chromosome.
  const bool materialFeasible = best.internalMaterialFeasible &&
      (config_.material_check_mode == GAConfig::POSTCHECK ||
       !gaHasMaterialShortage(best));
  const bool precedenceFeasible =
      isQuantityFlowLineSolution(best) || !hasPrecedenceViolation(best);
  dumpGAApplyDecisionIfRequested(best, lastKeptBaseline_, materialFeasible,
                                 precedenceFeasible);
  if (!lastKeptBaseline_ && best.unscheduledCount == 0 && materialFeasible &&
      precedenceFeasible)
    applyBestSolution(best);
  else if ((!materialFeasible || !precedenceFeasible) && getLogLevel() >= 0)
    logger << indentlevel
           << "GA schedule was not applied: "
           << (!materialFeasible ? "insufficient fixed material supply"
                                 : "precedence validation failed")
           << "\n";

  if (getLogLevel() > 0)
    logger << indentlevel << "GA on '" << res->getName()
           << "': " << gaInput.operations.size() << " ops, "
           << gaInput.optionCount() << " options, fitness="
           << best.fitness << "\n";
}

void SolverGA::solveJoint(const vector<const Resource*>& resources) {
  if (resources.empty()) return;
  const auto jointStarted = chrono::steady_clock::now();
  vector<const Resource*> gaResources = expandResourcesForCompleteJobs(resources);
  const auto resourcesExpanded = chrono::steady_clock::now();
  vector<CandidateOp> baseCandidates = buildCandidates(gaResources);
  // Resource loadplan iterators are intentionally local views in a number of
  // MRP rollback paths.  For a material-coupled CCPL line that can omit an
  // otherwise valid upstream station from the GA input. Build its fixed
  // candidates directly from the complete operationplan network instead.
  // The flow quantities below are the actual FlowPlan quantities of every MO;
  // no one-to-one batch pairing is introduced here.
  int firstFlowStage = numeric_limits<int>::max();
  int lastFlowStage = -1;
  unsigned int flowPlanCount = 0;
  for (auto plan = OperationPlan::begin(); plan != OperationPlan::end(); ++plan) {
    const int stage = parseCCPLStage(&*plan);
    if (!isGAEligibleOperationPlan(&*plan) || stage < 1) continue;
    firstFlowStage = min(firstFlowStage, stage);
    lastFlowStage = max(lastFlowStage, stage);
    ++flowPlanCount;
  }
  // The boundary is deliberately determined from the process chain (OP-01
  // to its final OP-N), rather than from whichever resource happened to be
  // overloaded when MRP called GA.
  if (flowPlanCount >= 2 && lastFlowStage >= firstFlowStage) {
    baseCandidates.erase(
        remove_if(baseCandidates.begin(), baseCandidates.end(),
                  [](const CandidateOp& candidate) {
                    return parseCCPLStage(candidate.op) > 0;
                  }),
        baseCandidates.end());
    unordered_set<OperationPlan*> seen;
    int nextFlowCandidate = 0;
    for (const auto& candidate : baseCandidates)
      nextFlowCandidate = max(nextFlowCandidate, candidate.candId + 1);
    for (auto plan = OperationPlan::begin(); plan != OperationPlan::end(); ++plan) {
      OperationPlan* op = &*plan;
      const int stage = parseCCPLStage(op);
      if (!isGAEligibleOperationPlan(op) || stage < firstFlowStage ||
          stage > lastFlowStage ||
          !seen.insert(op).second)
        continue;
      CandidateOp candidate;
      candidate.op = op;
      candidate.earliestStart = Plan::instance().getCurrent();
      candidate.candId = nextFlowCandidate++;
      for (auto load = op->beginLoadPlans(); load != op->endLoadPlans(); ++load) {
        if (!load->isStart() || !load->getLoad() || !load->getResource()) continue;
        candidate.loadAssignments.push_back({load->getLoad(), load->getResource()});
        if (find(candidate.allResources.begin(), candidate.allResources.end(),
                 load->getResource()) == candidate.allResources.end())
          candidate.allResources.push_back(load->getResource());
      }
      if (!candidate.allResources.empty()) {
        candidate.res = candidate.allResources.front();
        baseCandidates.push_back(move(candidate));
      }
    }
  }
  const auto candidatesBuilt = chrono::steady_clock::now();
  baseCandidates.erase(
      remove_if(
          baseCandidates.begin(), baseCandidates.end(),
          [](const CandidateOp& candidate) {
            return !isGAEligibleOperationPlan(candidate.op);
          }),
      baseCandidates.end());
  vector<CandidateOp> rawCandidates =
      expandGAAlternateCandidates(this, gaResources, baseCandidates);
  const auto alternatesExpanded = chrono::steady_clock::now();
  GAStandardInput gaInput =
      buildGAStandardInput(gaResources, rawCandidates,
                           unordered_map<const Resource*, Date>());
  if (getLogLevel() >= 0)
    logger << indentlevel
           << "GA timing: resources="
           << chrono::duration<double>(resourcesExpanded - jointStarted).count()
           << "s, candidates="
           << chrono::duration<double>(candidatesBuilt - resourcesExpanded).count()
           << "s, alternates="
           << chrono::duration<double>(alternatesExpanded - candidatesBuilt).count()
           << "s, input="
           << chrono::duration<double>(chrono::steady_clock::now() - alternatesExpanded).count()
           << "s\n";
  if (gaInput.operations.size() < 2 || gaInput.candidates.empty()) return;

  for (auto* res : gaResources)
    gaInput.resourceTimes[res] = Plan::instance().getCurrent();

  AntSolution best = evolve(gaInput.resources, gaInput.candidates,
                            gaInput.resourceTimes);
  const bool materialFeasible = best.internalMaterialFeasible &&
      (config_.material_check_mode == GAConfig::POSTCHECK ||
       !gaHasMaterialShortage(best));
  const bool precedenceFeasible =
      isQuantityFlowLineSolution(best) || !hasPrecedenceViolation(best);
  dumpGAApplyDecisionIfRequested(best, lastKeptBaseline_, materialFeasible,
                                 precedenceFeasible);
  if (!lastKeptBaseline_ && best.unscheduledCount == 0 && materialFeasible &&
      precedenceFeasible)
    applyBestSolution(best);
  else if ((!materialFeasible || !precedenceFeasible) && getLogLevel() >= 0)
    logger << indentlevel
           << "GA schedule was not applied: "
           << (!materialFeasible ? "insufficient fixed material supply"
                                 : "precedence validation failed")
           << "\n";

  if (getLogLevel() > 0)
    logger << indentlevel << "GA joint: " << gaResources.size()
           << " resources, " << gaInput.operations.size()
           << " ops, " << gaInput.optionCount()
           << " options, fitness=" << best.fitness << "\n";
}

void SolverGA::solve(void* v) {
  try {
    if (config_.runMRP) runGAMRP(v);
  } catch (...) {
    for (auto op = OperationPlan::begin(); op != OperationPlan::end(); ++op)
      op->setAcoLocked(false);
    throw;
  }

  // A CCPL OP-01..OP-N line is one material-coupled scheduling problem even
  // when MRP happens to overload only a single downstream station.  Starting
  // GA from that bottleneck alone excludes its upstream WIP producers, so no
  // decoder can calculate the real cumulative material release. Detect the
  // fixed line globally and hand all participating stations to one joint
  // forward decode.
  vector<const Resource*> flowLineResources;
  unordered_set<const Resource*> flowLineResourceSet;
  // Inspect operationplans rather than Resource::getLoadPlans(). At this
  // point SolverCreate can have rolled back a local resource bookmark after
  // a shortage, leaving that resource iterator incomplete even though the
  // actual CCPL MO and its loadplan still exist in the network.
  bool hasCCPLFlowLine = false;
  unsigned int ccplStageCount = 0;
  for (auto op = OperationPlan::begin(); op != OperationPlan::end(); ++op)
    if (parseCCPLStage(&*op) > 0) ++ccplStageCount;
  hasCCPLFlowLine = ccplStageCount >= 2;
  if (hasCCPLFlowLine) {
    // The line is a single scheduling scope. Include every finite concrete
    // resource: the subsequent candidate collector filters to the actual
    // OP-01..OP-N operationplans, while this avoids relying on an incomplete
    // resource loadplan iterator left by MRP rollback bookkeeping.
    for (auto res = Resource::begin(); res != Resource::end(); ++res)
      if (!res->isGroup() && res->getConstrained() &&
          flowLineResourceSet.insert(&*res).second)
        flowLineResources.push_back(&*res);
  }
  sort(flowLineResources.begin(), flowLineResources.end(),
       [](const Resource* a, const Resource* b) { return a->getName() < b->getName(); });

  if (flowLineResources.size() >= 2) {
    if (getLogLevel() >= 0)
      logger << indentlevel << "GA: scheduling CCPL flow line jointly on "
             << flowLineResources.size() << " resources\n";
    solveJoint(flowLineResources);
    // applyBestSolution has already performed the GA-specific postprocess:
    // it synchronizes movable WIP distribution orders from the actual
    // forward-decoded FlowPlan quantities.  Do NOT invoke the generic MRP
    // shortage refresh here: it rebuilds a local backwards manufacturing
    // network and can overwrite the full CCPL forward schedule just chosen
    // by GA.  Remaining external-material shortages are intentionally
    // reported as postcheck results.
    for (auto op = OperationPlan::begin(); op != OperationPlan::end(); ++op)
      op->setAcoLocked(false);
    return;
  }

  // Some MRP failure paths temporarily detach loadplans from their resource
  // iterator. The process chain is still visible on the operationplans, but
  // no longer yields two resources above. Build the concrete resource scope
  // from the operation definitions so the OP-01..OP-N chain is not silently
  // downgraded to a local bottleneck GA run.
  if (hasCCPLFlowLine) {
    vector<const Resource*> declaredResources;
    unordered_set<const Resource*> declaredSet;
    for (auto operation = Operation::begin(); operation != Operation::end();
         ++operation) {
      const string& name = operation->getName();
      const size_t marker = name.rfind("OP-");
      if (marker == string::npos) continue;
      char* end = nullptr;
      const long stage = strtol(name.c_str() + marker + 3, &end, 10);
      if (end == name.c_str() + marker + 3 || stage < 1) continue;
      for (const auto& load : operation->getLoads()) {
        const Resource* resource = load.getResource();
        if (resource && !resource->isGroup() && resource->getConstrained() &&
            declaredSet.insert(resource).second)
          declaredResources.push_back(resource);
      }
    }
    if (!declaredResources.empty()) {
      sort(declaredResources.begin(), declaredResources.end(),
           [](const Resource* a, const Resource* b) {
             return a->getName() < b->getName();
           });
      solveJoint(declaredResources);
      for (auto op = OperationPlan::begin(); op != OperationPlan::end(); ++op)
        op->setAcoLocked(false);
      return;
    }
  }

  vector<const Resource*> bottlenecks = collectBottlenecks();

  if (getLogLevel() >= 0)
    logger << indentlevel << "GA: entry after MRP, found "
           << bottlenecks.size() << " bottleneck resources\n";

  if (bottlenecks.empty()) {
    for (auto op = OperationPlan::begin(); op != OperationPlan::end(); ++op)
      op->setAcoLocked(false);
    return;
  }

  if (bottlenecks.size() >= 2 && config_.joint_optimization)
    solveJoint(bottlenecks);
  else if (bottlenecks.size() == 1)
    solve(bottlenecks[0], v);

  if (config_.material_check_mode == GAConfig::POSTCHECK) {
    if (getEnvBool("GA_SKIP_POSTCHECK", false)) {
      if (getLogLevel() >= 0)
        logger << indentlevel << "GA postcheck: skipped by diagnostic flag\n";
    } else {
    const auto shortages = collectShortageBuffers();
    if (getLogLevel() >= 0)
      logger << indentlevel << "GA postcheck: " << shortages.size()
             << " external shortage buffer(s); rebuilding affected proposed PO/DO\n";
    refreshProposedSupply(shortages);
    }
  }

  // Unlock GA-locked plans (set by SolverGA::applyBestSolution via
  // lockOptimizedPlan, which calls OperationPlan::setAcoLocked).
  for (auto op = OperationPlan::begin(); op != OperationPlan::end(); ++op)
    op->setAcoLocked(false);
}

}  // namespace ccAPPS

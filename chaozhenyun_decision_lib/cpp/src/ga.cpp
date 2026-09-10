#include "ga.h"

#include <algorithm>
#include <limits>
#include <numeric>
#include <random>
#include <stdexcept>

namespace chaozhenyun::ga {
namespace {

struct Chromosome {
  std::vector<int> ms;
  std::vector<int> os;
  long long makespan = std::numeric_limits<long long>::max();
};

int operation_count(const std::vector<Job>& jobs) {
  int count = 0;
  for (const auto& job : jobs) count += static_cast<int>(job.operations.size());
  return count;
}

std::vector<int> offsets(const std::vector<Job>& jobs) {
  std::vector<int> result(jobs.size());
  for (size_t i = 1; i < jobs.size(); ++i)
    result[i] = result[i - 1] + static_cast<int>(jobs[i - 1].operations.size());
  return result;
}

long long decode(Chromosome& code, const std::vector<Job>& jobs, int machine_count) {
  std::vector<int> next_operation(jobs.size(), 0), job_available(jobs.size(), 0);
  std::vector<int> machine_available(machine_count, 0);
  const auto job_offset = offsets(jobs);
  long long makespan = 0;
  for (int job_index : code.os) {
    if (job_index < 0 || job_index >= static_cast<int>(jobs.size())) throw std::runtime_error("invalid operation-sequence gene");
    const int operation_index = next_operation[job_index]++;
    if (operation_index >= static_cast<int>(jobs[job_index].operations.size())) throw std::runtime_error("invalid operation-sequence occurrence count");
    const auto& operation = jobs[job_index].operations[operation_index];
    if (operation.machines.empty() || operation.machines.size() != operation.durations.size()) throw std::runtime_error("invalid equipment candidates");
    const int gene = job_offset[job_index] + operation_index;
    code.ms[gene] = std::clamp(code.ms[gene], 0, static_cast<int>(operation.machines.size()) - 1);
    const int machine = operation.machines[code.ms[gene]];
    if (machine < 0 || machine >= machine_count) throw std::runtime_error("equipment index out of range");
    const int end = std::max(job_available[job_index], machine_available[machine]) + operation.durations[code.ms[gene]];
    job_available[job_index] = end;
    machine_available[machine] = end;
    makespan = std::max<long long>(makespan, end);
  }
  code.makespan = makespan;
  return makespan;
}

Chromosome initialize(const std::vector<Job>& jobs, std::mt19937& rng, bool shortest_machine) {
  Chromosome value;
  for (size_t job = 0; job < jobs.size(); ++job) {
    for (const auto& operation : jobs[job].operations) {
      if (operation.machines.empty()) throw std::runtime_error("operation has no equipment candidate");
      int choice = 0;
      if (shortest_machine)
        choice = static_cast<int>(std::min_element(operation.durations.begin(), operation.durations.end()) - operation.durations.begin());
      else
        choice = std::uniform_int_distribution<int>(0, static_cast<int>(operation.machines.size()) - 1)(rng);
      value.ms.push_back(choice);
      value.os.push_back(static_cast<int>(job));
    }
  }
  std::shuffle(value.os.begin(), value.os.end(), rng);
  return value;
}

int tournament(const std::vector<Chromosome>& population, int size, std::mt19937& rng) {
  std::uniform_int_distribution<int> pick(0, static_cast<int>(population.size()) - 1);
  int best = pick(rng);
  for (int count = 1; count < std::max(1, size); ++count) {
    const int candidate = pick(rng);
    if (population[candidate].makespan < population[best].makespan) best = candidate;
  }
  return best;
}

void pox_crossover(std::vector<int>& child, const std::vector<int>& parent, int jobs, std::mt19937& rng) {
  std::vector<bool> keep(jobs, false);
  std::bernoulli_distribution choose(0.5);
  for (auto&& item : keep) item = choose(rng);
  size_t parent_position = 0;
  for (size_t position = 0; position < child.size(); ++position) {
    if (keep[child[position]]) continue;
    while (parent_position < parent.size() && keep[parent[parent_position]]) ++parent_position;
    if (parent_position < parent.size()) child[position] = parent[parent_position++];
  }
}

void local_search(Chromosome& best, const std::vector<Job>& jobs, int machines, std::mt19937& rng) {
  if (best.os.size() < 2) return;
  std::uniform_int_distribution<int> position(0, static_cast<int>(best.os.size()) - 1);
  const auto job_offset = offsets(jobs);
  for (int trial = 0; trial < 12; ++trial) {
    Chromosome candidate = best;
    std::swap(candidate.os[position(rng)], candidate.os[position(rng)]);
    const int gene = position(rng);
    int job = 0;
    while (job + 1 < static_cast<int>(jobs.size()) && gene >= job_offset[job + 1]) ++job;
    const int operation = gene - job_offset[job];
    const int choices = static_cast<int>(jobs[job].operations[operation].machines.size());
    if (choices > 1) candidate.ms[gene] = (candidate.ms[gene] + 1) % choices;
    decode(candidate, jobs, machines);
    if (candidate.makespan < best.makespan) best = std::move(candidate);
  }
}
}  // namespace

Result solve(const std::vector<Job>& jobs, int machine_count, const Config& config) {
  if (jobs.empty() || operation_count(jobs) == 0 || machine_count <= 0) return {};
  std::mt19937 rng(config.seed);
  const int population_size = std::max(4, config.population_size);
  std::vector<Chromosome> population;
  population.reserve(population_size);
  for (int i = 0; i < population_size; ++i) {
    population.push_back(initialize(jobs, rng, i < population_size / 3));
    decode(population.back(), jobs, machine_count);
  }
  Chromosome best = *std::min_element(population.begin(), population.end(), [](const auto& left, const auto& right) { return left.makespan < right.makespan; });
  int best_generation = 0, unchanged = 0;
  for (int generation = 1; generation <= config.generations && unchanged < config.stagnation_limit; ++generation) {
    std::sort(population.begin(), population.end(), [](const auto& left, const auto& right) { return left.makespan < right.makespan; });
    std::vector<Chromosome> next = {population.front()};  // Elitism.
    while (static_cast<int>(next.size()) < population_size) {
      Chromosome child = population[tournament(population, config.tournament_size, rng)];
      const auto& mate = population[tournament(population, config.tournament_size, rng)];
      pox_crossover(child.os, mate.os, static_cast<int>(jobs.size()), rng);
      for (size_t gene = 0; gene < child.ms.size(); ++gene)
        if (std::bernoulli_distribution(0.2)(rng)) child.ms[gene] = mate.ms[gene];
      if (std::bernoulli_distribution(config.mutation_rate)(rng))
        std::swap(child.os[std::uniform_int_distribution<int>(0, static_cast<int>(child.os.size()) - 1)(rng)], child.os[std::uniform_int_distribution<int>(0, static_cast<int>(child.os.size()) - 1)(rng)]);
      decode(child, jobs, machine_count);
      next.push_back(std::move(child));
    }
    population = std::move(next);
    const auto generation_best = *std::min_element(population.begin(), population.end(), [](const auto& left, const auto& right) { return left.makespan < right.makespan; });
    if (generation_best.makespan < best.makespan) { best = generation_best; best_generation = generation; unchanged = 0; }
    else ++unchanged;
    if (generation > config.generations * 7 / 10) local_search(best, jobs, machine_count, rng);
  }
  return {best.ms, best.os, best.makespan, best_generation};
}
}  // namespace chaozhenyun::ga

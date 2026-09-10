#pragma once

#include <cstdint>
#include <vector>

namespace chaozhenyun::ga {

// Independent resource-selection input. An operation can be processed by one
// of several eligible equipment alternatives; all durations use seconds.
struct Operation {
  std::vector<int> machines;
  std::vector<int> durations;
};

struct Job { std::vector<Operation> operations; };

struct Config {
  int population_size = 80;
  int generations = 100;
  int stagnation_limit = 20;
  int tournament_size = 4;
  double mutation_rate = 0.15;
  std::uint32_t seed = 20260807U;
};

struct Result {
  // MS: machine-option selection for every job operation. OS: job sequence.
  std::vector<int> machine_selection;
  std::vector<int> operation_sequence;
  long long makespan = 0;
  int best_generation = 0;
};

Result solve(const std::vector<Job>& jobs, int machine_count, const Config& config = {});

}  // namespace chaozhenyun::ga

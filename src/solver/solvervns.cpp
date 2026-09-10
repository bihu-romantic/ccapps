/***************************************************************************
 *                                                                         *
 * Copyright (C) 2007-2026 by ccAPPS bv                                   *
 *                                                                         *
 * Permission is hereby granted, free of charge, to any person obtaining   *
 * a copy of this software and associated documentation files (the         *
 * "Software"), to deal in the Software without restriction, including     *
 * without limitation the rights to use, copy, modify, merge, publish,     *
 * distribute, sublicense, and/or sell copies of the Software, and to      *
 * permit persons to whom the Software is furnished to do so, subject to   *
 * the following conditions:                                               *
 *                                                                         *
 * The above copyright notice and this permission notice shall be          *
 * included in all copies or substantial portions of the Software.         *
 *                                                                         *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,         *
 * EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF      *
 * MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND                   *
 * NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE  *
 * LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION  *
 * OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION   *
 * WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.         *
 *                                                                         *
 ***************************************************************************/

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <limits>
#include <random>
#include <string>
#include <tuple>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

#include "ccAPPS/solvervns.h"
#include "rapidjson/document.h"
#include "rapidjson/stringbuffer.h"
#include "rapidjson/writer.h"

namespace ccAPPS {

namespace roadef_vns {

using DemandId = std::string;

struct RatioConstraint {
  std::string name;
  int p = 0;
  int q = 1;
  bool highPriority = false;
};

struct Vehicle {
  DemandId demand;
  std::string color;
  std::unordered_map<std::string, bool> options;
  int sequenceRank = 0;
};

struct Score {
  int hprc = 0;
  int lprc = 0;
  int pcc = 0;
  int batch = 0;
  std::int64_t objective = 0;
};

struct Weights {
  std::int64_t hprc = 1000;
  std::int64_t lprc = 1;
  std::int64_t pcc = 1000000;
  std::int64_t batch = 1000000000;
};

struct Config {
  double timeLimitSeconds = 30.0;
  int maxNoImprovement = 250;
  int samplesPerRound = 250;
  int paintBatchLimit = 0;
  int frozenPrefix = 0;
  unsigned int randomSeed = 7;
  Weights weights;

  double timePartIls = 0.90;
  double timePartVnsLprc = 0.96;
  int alphaIls = 25;
  int betaIls = 50;
  int stoppingCriteriaIlsHprc = 3;
  int nbCarPerturbation = 5;
  int nbCarDiversification = 50;
  int vnsLprcMaxNonImprovement = 150;
  int vnsLprcMinInsert = 3;
  int vnsLprcMaxInsert = 8;
  int vnsLprcMinExchange = 5;
  int vnsLprcMaxExchange = 12;
  int vnsPccMinInsert = 2;
  int vnsPccMaxInsert = 8;
  int vnsPccMinExchange = 2;
  int vnsPccMaxExchange = 10;

  int graspIterations = 8;
  int graspRclSize = 8;
  int globalVnsNeighborhoods = 4;
  int shakingStrength = 50;
  int blockMaxLength = 8;
  int colorPolishRounds = 6;
};

struct Result {
  std::vector<DemandId> sequence;
  Score initialScore;
  Score bestScore;
};

enum class MoveKind { Swap, Insert, BlockInsert, BlockSwap };
enum class Stage { Hprc, Lprc, Pcc, Overall };

struct Move {
  MoveKind kind = MoveKind::Swap;
  std::size_t from = 0;
  std::size_t to = 0;
  std::size_t length = 1;
  std::size_t otherLength = 1;
};

struct ColorRun {
  std::size_t start = 0;
  std::size_t length = 0;
  std::string color;
};

class SequenceVNS {
 public:
  SequenceVNS(std::unordered_map<DemandId, Vehicle> vehicles,
              std::vector<RatioConstraint> ratios, Config config)
      : vehicles_(std::move(vehicles)),
        ratios_(std::move(ratios)),
        config_(config),
        rng_(config.randomSeed) {
    for (const auto& ratio : ratios_) {
      if (ratio.highPriority)
        hprcRatios_.push_back(&ratio);
      else
        lprcRatios_.push_back(&ratio);
    }
  }

  Result optimize(const std::vector<DemandId>& initialSequence) {
    using Clock = std::chrono::steady_clock;
    const auto start = Clock::now();
    const auto deadline =
        start + std::chrono::duration_cast<Clock::duration>(
                    std::chrono::duration<double>(
                        std::max(0.1, config_.timeLimitSeconds)));

    std::vector<DemandId> greedyInitial =
        constructRoadefGreedyPccSequence(initialSequence);
    greedyInitial = repairPaintBatches(std::move(greedyInitial), deadline);

    std::vector<DemandId> best = greedyInitial;
    Score bestScore = score(best);

    auto acceptCandidate = [&](std::vector<DemandId> candidate) {
      candidate = repairPaintBatches(std::move(candidate), deadline);
      const Score candidateScore = score(candidate);
      if (candidateScore.objective < bestScore.objective) {
        best = std::move(candidate);
        bestScore = candidateScore;
      }
    };

    auto roadefPcc = optimizePccStage(std::move(greedyInitial), deadline);
    acceptCandidate(std::move(roadefPcc));

    auto ratioPolished = ratioPolishKeepingPaint(best, deadline);
    acceptCandidate(std::move(ratioPolished));

    return Result{best, score(constructRoadefGreedyPccSequence(initialSequence)),
                  bestScore};
  }

  Score score(const std::vector<DemandId>& sequence) const {
    Score result;
    result.hprc = ratioViolation(sequence, hprcRatios_);
    result.lprc = ratioViolation(sequence, lprcRatios_);
    const auto paint = paintCost(sequence);
    result.pcc = paint.first;
    result.batch = paint.second;
    result.objective = static_cast<std::int64_t>(result.hprc) *
                           config_.weights.hprc +
                       static_cast<std::int64_t>(result.lprc) *
                           config_.weights.lprc +
                       static_cast<std::int64_t>(result.pcc) *
                           config_.weights.pcc;
    return result;
  }

 private:
  std::vector<DemandId> constructRoadefGreedyPccSequence(
      const std::vector<DemandId>& seedSequence) {
    const std::size_t movableStart = firstMovableIndex(seedSequence);
    if (movableStart >= seedSequence.size()) return seedSequence;

    std::vector<DemandId> result(seedSequence.begin(),
                                 seedSequence.begin() +
                                     static_cast<std::ptrdiff_t>(movableStart));
    result.reserve(seedSequence.size());

    std::vector<DemandId> remaining(seedSequence.begin() +
                                        static_cast<std::ptrdiff_t>(movableStart),
                                    seedSequence.end());

    std::unordered_map<std::string, std::vector<DemandId>> byColor;
    for (const auto& demand : remaining) byColor[colorOf(demand)].push_back(demand);

    auto sortedColorGroups = [&]() {
      std::vector<std::pair<std::string, int>> groups;
      groups.reserve(byColor.size());
      for (const auto& entry : byColor) {
        if (!entry.second.empty())
          groups.emplace_back(entry.first, static_cast<int>(entry.second.size()));
      }
      std::sort(groups.begin(), groups.end(), [](const auto& left,
                                                 const auto& right) {
        if (left.second != right.second) return left.second > right.second;
        return left.first < right.first;
      });
      return groups;
    };

    std::unordered_map<std::string, int> rv;
    for (const auto& demand : seedSequence) {
      for (const auto* ratio : hprcRatios_) {
        if (hasOption(demand, ratio->name)) ++rv[ratio->name];
      }
    }

    std::unordered_map<std::string, int> rpi;
    for (const auto& demand : result) {
      for (const auto* ratio : hprcRatios_) {
        if (hasOption(demand, ratio->name)) ++rpi[ratio->name];
      }
    }

    int batchSize = 0;
    std::string currentColor;
    std::string activeColor;

    while (!remaining.empty()) {
      auto groups = sortedColorGroups();
      if (groups.empty()) break;
      if (activeColor.empty() || !byColor.contains(activeColor) ||
          byColor[activeColor].empty()) {
        activeColor = groups.front().first;
      }
      std::string targetColor = activeColor;
      auto candidates = demandsOfColor(remaining, targetColor);
      if (candidates.empty()) candidates = remaining;

      candidates = filterMinInt(
          candidates, [&](const DemandId& demand) {
            return newViolationIfAppended(result, demand, hprcRatios_);
          });

      if (candidates.size() > 1 && !hprcRatios_.empty()) {
        const double prefixLength =
            std::max<double>(1.0, static_cast<double>(result.size()));
        const double remainingLength =
            std::max<double>(1.0, static_cast<double>(remaining.size()));
        candidates = filterMaxDouble(candidates, [&](const DemandId& demand) {
          double tieBreak = 0.0;
          for (const auto* ratio : hprcRatios_) {
            const bool cond1 = !hasOption(demand, ratio->name);
            const bool cond2 =
                (static_cast<double>(rv[ratio->name] - rpi[ratio->name]) /
                 remainingLength) >
                (static_cast<double>(rpi[ratio->name]) / prefixLength);
            if (cond1 != cond2) tieBreak += 1.0;
          }
          return tieBreak;
        });
      }

      if (candidates.size() > 1 && !hprcRatios_.empty()) {
        const double remainingLength =
            std::max<double>(1.0, static_cast<double>(remaining.size()));
        candidates = filterMaxDouble(candidates, [&](const DemandId& demand) {
          double tieBreak = 0.0;
          for (const auto* ratio : hprcRatios_) {
            if (ratio->p <= 0 || ratio->q <= 0) continue;
            const double utilization =
                (static_cast<double>(rv[ratio->name] - rpi[ratio->name]) /
                 remainingLength) /
                (static_cast<double>(ratio->p) / static_cast<double>(ratio->q));
            if (hasOption(demand, ratio->name)) tieBreak += utilization;
          }
          return tieBreak;
        });
      }

      if (candidates.size() > 1 && !lprcRatios_.empty()) {
        candidates = filterMinInt(
            candidates, [&](const DemandId& demand) {
              return newViolationIfAppended(result, demand, lprcRatios_);
            });
      }

      const auto chosen = candidates.front();
      result.push_back(chosen);
      eraseFirst(remaining, chosen);
      auto colorIt = byColor.find(colorOf(chosen));
      if (colorIt != byColor.end()) eraseFirst(colorIt->second, chosen);

      for (const auto* ratio : hprcRatios_) {
        if (hasOption(chosen, ratio->name)) ++rpi[ratio->name];
      }

      if (currentColor.empty() || currentColor != colorOf(chosen)) {
        currentColor = colorOf(chosen);
        batchSize = 1;
      } else {
        ++batchSize;
      }

      if (colorIt != byColor.end() && colorIt->second.empty())
        byColor.erase(colorIt);

      if (config_.paintBatchLimit > 0 &&
          batchSize >= config_.paintBatchLimit) {
        batchSize = 0;
        auto nextGroups = sortedColorGroups();
        activeColor.clear();
        if (!nextGroups.empty() && nextGroups.front().first == currentColor &&
            nextGroups.size() > 1)
          activeColor = nextGroups[1].first;
      }
    }

    return result.size() == seedSequence.size() ? result : seedSequence;
  }

  std::vector<DemandId> constructGraspInitialSequence(
      const std::vector<DemandId>& seedSequence,
      const std::chrono::steady_clock::time_point& deadline) {
    using Clock = std::chrono::steady_clock;
    std::vector<DemandId> best = seedSequence;
    Score bestScore = score(best);
    const std::size_t movableStart = firstMovableIndex(seedSequence);
    if (movableStart >= seedSequence.size()) return best;

    const int iterations = std::max(1, config_.graspIterations);
    for (int iter = 0; iter < iterations && Clock::now() < deadline; ++iter) {
      std::vector<DemandId> remaining(seedSequence.begin() +
                                          static_cast<std::ptrdiff_t>(
                                              movableStart),
                                      seedSequence.end());
      std::shuffle(remaining.begin(), remaining.end(), rng_);
      std::vector<DemandId> candidate(seedSequence.begin(),
                                      seedSequence.begin() +
                                          static_cast<std::ptrdiff_t>(
                                              movableStart));
      candidate.reserve(seedSequence.size());

      while (!remaining.empty() && Clock::now() < deadline) {
        const int scanCount =
            std::min(static_cast<int>(remaining.size()),
                     std::max(1, config_.graspRclSize * 3));
        std::vector<std::pair<Score, std::size_t>> ranked;
        ranked.reserve(scanCount);

        for (int idx = 0; idx < scanCount; ++idx) {
          auto trial = candidate;
          trial.push_back(remaining[idx]);
          ranked.emplace_back(score(trial), static_cast<std::size_t>(idx));
        }
        std::sort(ranked.begin(), ranked.end(),
                  [](const auto& left, const auto& right) {
                    return left.first.objective < right.first.objective;
                  });

        const int rclSize =
            std::min(static_cast<int>(ranked.size()),
                     std::max(1, config_.graspRclSize));
        std::uniform_int_distribution<int> pick(0, rclSize - 1);
        const std::size_t chosenIdx = ranked[pick(rng_)].second;
        candidate.push_back(remaining[chosenIdx]);
        remaining.erase(remaining.begin() +
                        static_cast<std::ptrdiff_t>(chosenIdx));
      }

      if (candidate.size() == seedSequence.size() &&
          score(candidate).objective < bestScore.objective) {
        best = std::move(candidate);
        bestScore = score(best);
      }
    }

    if (bestScore.objective <= score(seedSequence).objective) return best;
    return seedSequence;
  }

  std::vector<DemandId> globalVns(
      std::vector<DemandId> sequence,
      const std::chrono::steady_clock::time_point& deadline) {
    using Clock = std::chrono::steady_clock;
    std::vector<DemandId> best = sequence;
    Score bestScore = score(best);
    int noImprovement = 0;

    while (Clock::now() < deadline &&
           noImprovement < config_.maxNoImprovement) {
      auto shaken = strongShake(best);
      shaken = colorPolish(std::move(shaken), deadline);
      auto candidate = variableNeighborhoodDescent(std::move(shaken),
                                                   deadline);
      candidate = colorPolish(std::move(candidate), deadline);
      candidate = repairPaintBatches(std::move(candidate), deadline);
      const Score candidateScore = score(candidate);

      if (candidateScore.objective < bestScore.objective) {
        best = std::move(candidate);
        bestScore = candidateScore;
        sequence = best;
        noImprovement = 0;
      } else {
        ++noImprovement;
      }
    }

    return best;
  }

  std::vector<DemandId> colorPolish(
      std::vector<DemandId> sequence,
      const std::chrono::steady_clock::time_point& deadline) {
    using Clock = std::chrono::steady_clock;
    const std::size_t movableStart = firstMovableIndex(sequence);
    if (sequence.size() - movableStart < 3) return sequence;

    Score currentScore = score(sequence);
    const int rounds = std::max(1, config_.colorPolishRounds);
    for (int round = 0; round < rounds && Clock::now() < deadline; ++round) {
      bool improved = false;
      auto runs = colorRuns(sequence);

      // Work on short/split color runs first; merging them is the most direct
      // way to reduce paint color changes.
      std::sort(runs.begin(), runs.end(), [](const auto& left, const auto& right) {
        if (left.length != right.length) return left.length < right.length;
        return left.start < right.start;
      });

      for (const auto& run : runs) {
        if (Clock::now() >= deadline) break;
        if (run.length == 0 || run.start >= sequence.size() ||
            run.start < movableStart)
          continue;

        auto targets = sameColorInsertionTargets(sequence, run);
        for (const auto target : targets) {
          if (Clock::now() >= deadline) break;
          if (target >= run.start && target <= run.start + run.length) continue;
          auto candidate =
              applyMove(sequence, Move{MoveKind::BlockInsert, run.start, target,
                                       run.length, 1});
          if (config_.paintBatchLimit > 0 && !isBatchValid(candidate)) continue;
          const Score candidateScore = score(candidate);
          if (candidateScore.objective < currentScore.objective) {
            sequence = std::move(candidate);
            currentScore = candidateScore;
            improved = true;
            break;
          }
        }
        if (improved) break;
      }

      if (!improved) break;
    }

    return singleVehicleColorPolish(std::move(sequence), deadline);
  }

  std::vector<DemandId> singleVehicleColorPolish(
      std::vector<DemandId> sequence,
      const std::chrono::steady_clock::time_point& deadline) {
    using Clock = std::chrono::steady_clock;
    Score currentScore = score(sequence);
    const std::size_t movableStart = firstMovableIndex(sequence);

    for (int round = 0; round < std::max(1, config_.colorPolishRounds) &&
                        Clock::now() < deadline;
         ++round) {
      bool improved = false;
      auto boundaries = colorBoundaryPositions(sequence);
      std::shuffle(boundaries.begin(), boundaries.end(), rng_);

      for (const auto boundary : boundaries) {
        if (Clock::now() >= deadline) break;
        std::vector<std::size_t> sourcePositions;
        if (boundary > movableStart) sourcePositions.push_back(boundary - 1);
        if (boundary < sequence.size()) sourcePositions.push_back(boundary);

        for (const auto source : sourcePositions) {
          ColorRun run;
          run.start = source;
          run.length = 1;
          run.color = colorOf(sequence[source]);
          auto targets = sameColorInsertionTargets(sequence, run);
          std::shuffle(targets.begin(), targets.end(), rng_);
          for (const auto target : targets) {
            if (target == source || target == source + 1) continue;
            if (target < movableStart) continue;
            auto candidate =
                applyMove(sequence, Move{MoveKind::Insert, source, target, 1, 1});
            if (config_.paintBatchLimit > 0 && !isBatchValid(candidate))
              continue;
            const Score candidateScore = score(candidate);
            if (candidateScore.objective < currentScore.objective) {
              sequence = std::move(candidate);
              currentScore = candidateScore;
              improved = true;
              break;
            }
          }
          if (improved) break;
        }
        if (improved) break;
      }

      if (!improved) break;
    }

    return sequence;
  }

  std::vector<DemandId> strongShake(
      const std::vector<DemandId>& sequence) {
    std::vector<DemandId> result = sequence;
    if (movableCount(result) < 2) return result;

    const int strength = std::max(1, config_.shakingStrength);
    for (int iter = 0; iter < strength; ++iter) {
      const int neighborhood = 1 + (iter % std::max(1, config_.globalVnsNeighborhoods));
      auto moves = candidateMovesByNeighborhood(result, neighborhood,
                                                std::max(1, config_.samplesPerRound / 8));
      if (moves.empty()) continue;
      std::uniform_int_distribution<std::size_t> moveDist(0, moves.size() - 1);
      auto neighbor = applyMove(result, moves[moveDist(rng_)]);
      if (config_.paintBatchLimit > 0 && !isBatchValid(neighbor)) continue;
      result = std::move(neighbor);
    }

    return result;
  }

  std::vector<DemandId> variableNeighborhoodDescent(
      std::vector<DemandId> sequence,
      const std::chrono::steady_clock::time_point& deadline) {
    using Clock = std::chrono::steady_clock;
    int k = 1;
    const int maxNeighborhood =
        std::max(1, config_.globalVnsNeighborhoods);
    Score currentScore = score(sequence);

    while (Clock::now() < deadline && k <= maxNeighborhood) {
      auto neighbor = bestNeighbor(sequence, k, deadline);
      const Score neighborScore = score(neighbor);
      if (neighborScore.objective < currentScore.objective) {
        sequence = std::move(neighbor);
        currentScore = neighborScore;
        k = 1;
      } else {
        ++k;
      }
    }

    return sequence;
  }

  std::vector<DemandId> bestNeighbor(
      const std::vector<DemandId>& sequence, int neighborhood,
      const std::chrono::steady_clock::time_point& deadline) {
    using Clock = std::chrono::steady_clock;
    std::vector<DemandId> best = sequence;
    Score bestScore = score(sequence);
    auto moves = candidateMovesByNeighborhood(
        sequence, neighborhood, std::max(1, config_.samplesPerRound));

    for (const auto& move : moves) {
      if (Clock::now() >= deadline) break;
      auto candidate = applyMove(sequence, move);
      if (config_.paintBatchLimit > 0 && !isBatchValid(candidate)) continue;
      const Score candidateScore = score(candidate);
      if (candidateScore.objective < bestScore.objective) {
        best = std::move(candidate);
        bestScore = candidateScore;
      }
    }

    return best;
  }

  std::vector<Move> candidateMovesByNeighborhood(
      const std::vector<DemandId>& sequence, int neighborhood,
      int sampleCount) {
    std::vector<Move> moves;
    const std::size_t movableStart = firstMovableIndex(sequence);
    if (sequence.size() - movableStart < 2) return moves;

    std::uniform_int_distribution<std::size_t> anyPos(movableStart,
                                                      sequence.size() - 1);
    const int samples = std::max(1, sampleCount);

    for (int iter = 0; iter < samples; ++iter) {
      std::size_t first = anyPos(rng_);
      std::size_t second = anyPos(rng_);
      if (first == second) continue;

      if (neighborhood <= 1) {
        moves.push_back(Move{MoveKind::Swap, first, second, 1, 1});
      } else if (neighborhood == 2) {
        moves.push_back(Move{MoveKind::Insert, first, second, 1, 1});
      } else if (neighborhood == 3) {
        const std::size_t maxLen =
            std::min<std::size_t>(config_.blockMaxLength,
                                  sequence.size() - first);
        if (maxLen == 0) continue;
        std::uniform_int_distribution<std::size_t> lenDist(1, maxLen);
        moves.push_back(
            Move{MoveKind::BlockInsert, first, second, lenDist(rng_), 1});
      } else {
        if (first > second) std::swap(first, second);
        const std::size_t firstMax =
            std::min<std::size_t>(config_.blockMaxLength, second - first);
        const std::size_t secondMax =
            std::min<std::size_t>(config_.blockMaxLength,
                                  sequence.size() - second);
        if (firstMax == 0 || secondMax == 0) continue;
        std::uniform_int_distribution<std::size_t> firstLenDist(1, firstMax);
        std::uniform_int_distribution<std::size_t> secondLenDist(1, secondMax);
        moves.push_back(Move{MoveKind::BlockSwap, first, second,
                             firstLenDist(rng_), secondLenDist(rng_)});
      }
    }

    if (neighborhood >= 3) {
      auto runs = colorRuns(sequence);
      std::shuffle(runs.begin(), runs.end(), rng_);
      int added = 0;
      for (const auto& run : runs) {
        if (added >= samples / 2) break;
        if (run.length == 0 || run.start < movableStart) continue;
        auto targets = sameColorInsertionTargets(sequence, run);
        std::shuffle(targets.begin(), targets.end(), rng_);
        for (const auto target : targets) {
          if (target >= run.start && target <= run.start + run.length)
            continue;
          moves.push_back(
              Move{MoveKind::BlockInsert, run.start, target, run.length, 1});
          if (++added >= samples / 2) break;
        }
      }
    }

    std::shuffle(moves.begin(), moves.end(), rng_);
    return moves;
  }

  std::vector<DemandId> optimizeHprcStage(
      std::vector<DemandId> sequence,
      const std::chrono::steady_clock::time_point& deadline) {
    using Clock = std::chrono::steady_clock;
    std::vector<DemandId> best = sequence;
    Score bestScore = score(best);
    int iterationsSinceImprovement = 0;
    int stableBestRounds = 0;
    int strongPerturbations = 0;
    Score lastBest = bestScore;

    while (Clock::now() < deadline && bestScore.hprc != 0 &&
           stableBestRounds < config_.stoppingCriteriaIlsHprc) {
      auto critical = criticalDemandsForStage(sequence, Stage::Hprc);
      auto neighbor = removeCriticalAndGreedyReinsert(
          sequence, Stage::Hprc, config_.nbCarPerturbation, false, critical);
      neighbor = localSearchStage(std::move(neighbor), Stage::Hprc, deadline,
                                  false, true, false);
      const Score neighborScore = score(neighbor);
      const Score currentScore = score(sequence);

      if (isBetterForStage(neighborScore, currentScore, Stage::Hprc, false)) {
        sequence = std::move(neighbor);
      }

      if (iterationsSinceImprovement == config_.alphaIls) {
        sequence = localSearchStage(std::move(sequence), Stage::Hprc, deadline,
                                    false, true, false);
      }

      if (iterationsSinceImprovement == config_.betaIls) {
        ++stableBestRounds;
        if (lastBest.hprc > bestScore.hprc) {
          lastBest = bestScore;
          stableBestRounds = 0;
        }

        const Score currentAfterIntensification = score(sequence);
        if (currentAfterIntensification.hprc > bestScore.hprc) {
          sequence = best;
        } else if (strongPerturbations < 3) {
          sequence = removeCriticalAndGreedyReinsert(
              sequence, Stage::Hprc, config_.nbCarDiversification, false,
              criticalDemandsForStage(sequence, Stage::Hprc));
          ++strongPerturbations;
        } else {
          sequence = best;
          strongPerturbations = 0;
        }
        iterationsSinceImprovement = 0;
      }

      const Score sequenceScore = score(sequence);
      if (sequenceScore.hprc < bestScore.hprc) {
        best = sequence;
        bestScore = sequenceScore;
        iterationsSinceImprovement = 0;
      } else {
        ++iterationsSinceImprovement;
      }
    }

    return best;
  }

  std::vector<DemandId> optimizeLprcStage(
      std::vector<DemandId> sequence,
      const std::chrono::steady_clock::time_point& deadline) {
    using Clock = std::chrono::steady_clock;
    std::vector<DemandId> best = sequence;
    int perturbation = 0;
    int k = config_.vnsLprcMinInsert;
    int intensificationsWithoutImprovement = 0;

    while (Clock::now() < deadline &&
           intensificationsWithoutImprovement <
               config_.vnsLprcMaxNonImprovement) {
      const int kMin = perturbation == 0 ? config_.vnsLprcMinInsert
                                         : config_.vnsLprcMinExchange;
      const int kMax = perturbation == 0 ? config_.vnsLprcMaxInsert
                                         : config_.vnsLprcMaxExchange;
      k = std::max(k, kMin);

      while (Clock::now() < deadline && k <= kMax) {
        auto neighbor =
            perturbation == 0
                ? removeCriticalAndGreedyReinsert(
                      best, Stage::Lprc, k, false,
                      criticalDemandsForStage(best, Stage::Lprc))
                : perturbSameHprcExchange(best, k, false);
        neighbor = localSearchStage(std::move(neighbor), Stage::Lprc, deadline,
                                    perturbation == 1, true, false);
        const Score neighborScore = score(neighbor);
        const Score bestScore = score(best);

        if (isBetterForStage(neighborScore, bestScore, Stage::Lprc, true)) {
          k = kMin;
          intensificationsWithoutImprovement = 0;
        } else {
          ++k;
        }
        if (isBetterForStage(neighborScore, bestScore, Stage::Lprc, false)) {
          best = std::move(neighbor);
        }
      }

      const Score beforeIntensification = score(best);
      best = localSearchStage(std::move(best), Stage::Lprc, deadline, false,
                              true, false);
      if (!isBetterForStage(score(best), beforeIntensification, Stage::Lprc,
                            true)) {
        ++intensificationsWithoutImprovement;
      }
      perturbation = 1 - perturbation;
      k = perturbation == 0 ? config_.vnsLprcMinInsert
                            : config_.vnsLprcMinExchange;
    }

    return best;
  }

  std::vector<DemandId> repairPaintBatches(
      std::vector<DemandId> sequence,
      const std::chrono::steady_clock::time_point& deadline) {
    if (config_.paintBatchLimit <= 0) return sequence;

    using Clock = std::chrono::steady_clock;
    const std::size_t movableStart = firstMovableIndex(sequence);
    int guard = static_cast<int>(sequence.size()) * 2;
    while (Clock::now() < deadline && guard-- > 0) {
      const auto violation = firstBatchViolation(sequence);
      if (violation >= sequence.size()) break;
      if (violation < movableStart) break;

      const auto demand = sequence[violation];
      std::vector<DemandId> reduced = sequence;
      reduced.erase(reduced.begin() + static_cast<std::ptrdiff_t>(violation));

      std::vector<DemandId> bestCandidate;
      Score bestScore;
      bool hasCandidate = false;
      const std::size_t insertStart = firstMovableIndex(reduced);
      for (std::size_t pos = insertStart; pos <= reduced.size(); ++pos) {
        auto candidate = insertDemandAt(reduced, demand, pos);
        const Score candidateScore = score(candidate);
        if (!hasCandidate ||
            std::tie(candidateScore.batch, candidateScore.objective) <
                std::tie(bestScore.batch, bestScore.objective)) {
          bestCandidate = std::move(candidate);
          bestScore = candidateScore;
          hasCandidate = true;
        }
      }

      if (!hasCandidate || score(bestCandidate).batch >= score(sequence).batch)
        break;
      sequence = std::move(bestCandidate);
    }

    return sequence;
  }

  std::vector<DemandId> optimizePccStage(
      std::vector<DemandId> sequence,
      const std::chrono::steady_clock::time_point& deadline) {
    using Clock = std::chrono::steady_clock;
    std::vector<DemandId> best = sequence;
    int perturbation = 1;
    int k = config_.vnsPccMinExchange;

    while (Clock::now() < deadline) {
      const int kMin = perturbation == 0 ? config_.vnsPccMinInsert
                                         : config_.vnsPccMinExchange;
      const int kMax = perturbation == 0 ? config_.vnsPccMaxInsert
                                         : config_.vnsPccMaxExchange;
      k = std::max(k, kMin);

      while (Clock::now() < deadline && k <= kMax) {
        auto neighbor =
            perturbation == 0
                ? removeCriticalAndGreedyReinsert(
                      best, Stage::Pcc, k, true,
                      criticalDemandsForStage(best, Stage::Lprc))
                : perturbSameHprcExchange(best, k, true);
        if (score(neighbor).hprc > score(best).hprc) neighbor = best;

        neighbor = roadefLocalSearchPcc(std::move(neighbor), deadline);
        const Score neighborScore = score(neighbor);
        const Score bestScore = score(best);

        if (isBetterForStage(neighborScore, bestScore, Stage::Pcc, true)) {
          k = kMin;
        } else {
          ++k;
        }
        if (isBetterForStage(neighborScore, bestScore, Stage::Pcc, false)) {
          best = std::move(neighbor);
        }
      }

      best = roadefIntensificationPcc(std::move(best), deadline);
      perturbation = 1 - perturbation;
      k = perturbation == 0 ? config_.vnsPccMinInsert
                            : config_.vnsPccMinExchange;
    }

    return best;
  }

  std::vector<DemandId> localSearchStage(
      std::vector<DemandId> sequence, Stage stage,
      const std::chrono::steady_clock::time_point& deadline,
      bool sameHprcOnly, bool criticalOnly, bool enforceBatch) {
    using Clock = std::chrono::steady_clock;
    Score currentScore = score(sequence);
    bool improved = true;

    while (improved && Clock::now() < deadline) {
      improved = false;
      auto moves = candidateMovesForStage(sequence, stage, sameHprcOnly,
                                          criticalOnly);
      for (const auto& move : moves) {
        if (Clock::now() >= deadline) break;
        auto neighbor = applyMove(sequence, move);
        if (enforceBatch && config_.paintBatchLimit > 0 &&
            !isBatchValid(neighbor))
          continue;
        const Score neighborScore = score(neighbor);
        if (isBetterForStage(neighborScore, currentScore, stage, true)) {
          sequence = std::move(neighbor);
          currentScore = neighborScore;
          improved = true;
          break;
        }
      }
    }

    return sequence;
  }

  std::vector<DemandId> roadefLocalSearchPcc(
      std::vector<DemandId> sequence,
      const std::chrono::steady_clock::time_point& deadline) {
    using Clock = std::chrono::steady_clock;
    bool improved = true;
    while (improved && Clock::now() < deadline) {
      improved = false;
      const auto groups = positionsByHprc(sequence);
      const std::size_t start = firstMovableIndex(sequence);
      for (std::size_t first = start; first < sequence.size() &&
                                  Clock::now() < deadline;
           ++first) {
        const Score currentScore = score(sequence);
        std::vector<std::size_t> bestPositions;
        Score bestScore = currentScore;
        const auto groupIt = groups.find(hprcKey(sequence[first]));
        if (groupIt == groups.end()) continue;
        for (const auto second : groupIt->second) {
          if (first == second || second < start) continue;
          auto candidate = sequence;
          std::swap(candidate[first], candidate[second]);
          if (config_.paintBatchLimit > 0 && !isBatchValid(candidate))
            continue;
          const Score candidateScore = score(candidate);
          if (candidateScore.objective < bestScore.objective &&
              candidateScore.hprc <= currentScore.hprc) {
            bestPositions.clear();
            bestPositions.push_back(second);
            bestScore = candidateScore;
          } else if (candidateScore.objective == bestScore.objective &&
                     candidateScore.objective < currentScore.objective &&
                     candidateScore.hprc <= currentScore.hprc) {
            bestPositions.push_back(second);
          }
        }
        if (!bestPositions.empty()) {
          std::uniform_int_distribution<std::size_t> pick(
              0, bestPositions.size() - 1);
          std::swap(sequence[first], sequence[bestPositions[pick(rng_)]]);
          improved = true;
        }
      }
    }
    return sequence;
  }

  std::vector<DemandId> roadefIntensificationPcc(
      std::vector<DemandId> sequence,
      const std::chrono::steady_clock::time_point& deadline) {
    sequence = roadefInsertionIntensification(std::move(sequence), deadline);
    sequence = roadefExchangeIntensification(std::move(sequence), deadline);
    return sequence;
  }

  std::vector<DemandId> roadefInsertionIntensification(
      std::vector<DemandId> sequence,
      const std::chrono::steady_clock::time_point& deadline) {
    using Clock = std::chrono::steady_clock;
    bool improved = true;
    while (improved && Clock::now() < deadline) {
      improved = false;
      const std::size_t start = firstMovableIndex(sequence);
      for (std::size_t from = start; from < sequence.size() &&
                                 Clock::now() < deadline;
           ++from) {
        const Score currentScore = score(sequence);
        std::vector<std::size_t> bestPositions;
        Score bestScore = currentScore;
        for (std::size_t to = start; to < sequence.size(); ++to) {
          if (from == to) continue;
          auto candidate = applyMove(sequence, Move{MoveKind::Insert, from, to});
          if (candidate == sequence) continue;
          if (config_.paintBatchLimit > 0 && !isBatchValid(candidate))
            continue;
          const Score candidateScore = score(candidate);
          if (candidateScore.objective < bestScore.objective &&
              candidateScore.hprc <= currentScore.hprc) {
            bestPositions.clear();
            bestPositions.push_back(to);
            bestScore = candidateScore;
          } else if (candidateScore.objective == bestScore.objective &&
                     candidateScore.objective < currentScore.objective &&
                     candidateScore.hprc <= currentScore.hprc) {
            bestPositions.push_back(to);
          }
        }
        if (!bestPositions.empty()) {
          std::uniform_int_distribution<std::size_t> pick(
              0, bestPositions.size() - 1);
          sequence =
              applyMove(sequence, Move{MoveKind::Insert, from,
                                       bestPositions[pick(rng_)]});
          improved = true;
        }
      }
    }
    return sequence;
  }

  std::vector<DemandId> roadefExchangeIntensification(
      std::vector<DemandId> sequence,
      const std::chrono::steady_clock::time_point& deadline) {
    using Clock = std::chrono::steady_clock;
    bool improved = true;
    while (improved && Clock::now() < deadline) {
      improved = false;
      const auto critical = criticalDemandsForStage(sequence, Stage::Lprc);
      std::unordered_set<DemandId> criticalSet(critical.begin(), critical.end());
      const std::size_t start = firstMovableIndex(sequence);
      for (std::size_t first = start; first < sequence.size() &&
                                  Clock::now() < deadline;
           ++first) {
        if (!criticalSet.empty() && !criticalSet.contains(sequence[first]))
          continue;
        const Score currentScore = score(sequence);
        std::vector<std::size_t> bestPositions;
        Score bestScore = currentScore;
        for (std::size_t second = start; second < sequence.size(); ++second) {
          if (first == second) continue;
          auto candidate = sequence;
          std::swap(candidate[first], candidate[second]);
          if (config_.paintBatchLimit > 0 && !isBatchValid(candidate))
            continue;
          const Score candidateScore = score(candidate);
          if (candidateScore.objective < bestScore.objective &&
              candidateScore.hprc <= currentScore.hprc) {
            bestPositions.clear();
            bestPositions.push_back(second);
            bestScore = candidateScore;
          } else if (candidateScore.objective == bestScore.objective &&
                     candidateScore.objective < currentScore.objective &&
                     candidateScore.hprc <= currentScore.hprc) {
            bestPositions.push_back(second);
          }
        }
        if (!bestPositions.empty()) {
          std::uniform_int_distribution<std::size_t> pick(
              0, bestPositions.size() - 1);
          std::swap(sequence[first], sequence[bestPositions[pick(rng_)]]);
          improved = true;
        }
      }
    }
    return sequence;
  }

  std::vector<DemandId> ratioPolishKeepingPaint(
      std::vector<DemandId> sequence,
      const std::chrono::steady_clock::time_point& deadline) {
    using Clock = std::chrono::steady_clock;
    bool improved = true;
    while (improved && Clock::now() < deadline) {
      improved = false;
      const std::size_t start = firstMovableIndex(sequence);
      const Score current = score(sequence);
      for (std::size_t first = start; first < sequence.size() &&
                                  Clock::now() < deadline && !improved;
           ++first) {
        Score bestScore = current;
        std::vector<DemandId> bestCandidate;
        for (std::size_t second = start; second < sequence.size(); ++second) {
          if (first == second) continue;
          auto candidate = sequence;
          std::swap(candidate[first], candidate[second]);
          const Score candidateScore = score(candidate);
          if (candidateScore.pcc == current.pcc &&
              candidateScore.batch == current.batch &&
              candidateScore.objective < bestScore.objective) {
            bestScore = candidateScore;
            bestCandidate = std::move(candidate);
          }
        }
        if (!bestCandidate.empty()) {
          sequence = std::move(bestCandidate);
          improved = true;
          break;
        }
      }
      for (std::size_t first = start; first < sequence.size() &&
                                  Clock::now() < deadline && !improved;
           ++first) {
        Score bestScore = current;
        std::vector<DemandId> bestCandidate;
        for (std::size_t second = start; second < sequence.size(); ++second) {
          if (first == second) continue;
          auto candidate = applyMove(sequence, Move{MoveKind::Insert, first, second});
          if (candidate == sequence) continue;
          const Score candidateScore = score(candidate);
          if (candidateScore.pcc == current.pcc &&
              candidateScore.batch == current.batch &&
              candidateScore.objective < bestScore.objective) {
            bestScore = candidateScore;
            bestCandidate = std::move(candidate);
          }
        }
        if (!bestCandidate.empty()) {
          sequence = std::move(bestCandidate);
          improved = true;
          break;
        }
      }
    }
    return sequence;
  }

  std::vector<DemandId> removeCriticalAndGreedyReinsert(
      const std::vector<DemandId>& sequence, Stage stage, int count,
      bool enforceBatch, std::vector<DemandId> critical) {
    if (sequence.empty() || count <= 0) return sequence;
    if (critical.empty()) {
      const auto movableStart = firstMovableIndex(sequence);
      critical.assign(sequence.begin() + static_cast<std::ptrdiff_t>(movableStart),
                      sequence.end());
    }
    std::shuffle(critical.begin(), critical.end(), rng_);

    std::vector<DemandId> reduced = sequence;
    std::vector<DemandId> removed;
    const std::size_t movableStart = firstMovableIndex(sequence);
    const int removeCount =
        std::min(count, static_cast<int>(critical.size()));
    for (int idx = 0; idx < removeCount; ++idx) {
      const auto pos = positionOf(reduced, critical[idx]);
      if (pos >= reduced.size() || pos < movableStart) continue;
      removed.push_back(reduced[pos]);
      reduced.erase(reduced.begin() + static_cast<std::ptrdiff_t>(pos));
    }

    for (const auto& demand : removed) {
      reduced = greedyInsertDemand(reduced, demand, stage, enforceBatch);
    }
    return reduced;
  }

  std::vector<DemandId> greedyInsertDemand(
      const std::vector<DemandId>& sequence, const DemandId& demand,
      Stage stage, bool enforceBatch) {
    std::vector<DemandId> best;
    Score bestScore;
    bool hasBest = false;

    const std::size_t movableStart = firstMovableIndex(sequence);
    for (std::size_t pos = movableStart; pos <= sequence.size(); ++pos) {
      auto candidate = insertDemandAt(sequence, demand, pos);
      if (enforceBatch && config_.paintBatchLimit > 0 &&
          !isBatchValid(candidate))
        continue;
      const Score candidateScore = score(candidate);
      if (!hasBest ||
          isBetterForStage(candidateScore, bestScore, stage, true)) {
        best = std::move(candidate);
        bestScore = candidateScore;
        hasBest = true;
      }
    }

    if (hasBest) return best;
    return insertDemandAt(sequence, demand, sequence.size());
  }

  std::vector<DemandId> perturbSameHprcExchange(
      const std::vector<DemandId>& sequence, int count, bool enforceBatch) {
    std::vector<DemandId> result = sequence;
    if (movableCount(result) < 2 || count <= 0) return result;

    for (int iter = 0; iter < count; ++iter) {
      auto groups = positionsByHprc(result);
      std::vector<std::vector<std::size_t>> candidates;
      for (const auto& group : groups) {
        if (group.second.size() >= 2) candidates.push_back(group.second);
      }
      if (candidates.empty()) break;

      std::uniform_int_distribution<std::size_t> groupDist(
          0, candidates.size() - 1);
      const auto& group = candidates[groupDist(rng_)];
      std::uniform_int_distribution<std::size_t> posDist(0, group.size() - 1);
      std::size_t first = group[posDist(rng_)];
      std::size_t second = group[posDist(rng_)];
      if (first == second) continue;
      const std::size_t movableStart = firstMovableIndex(result);
      if (first < movableStart || second < movableStart) continue;

      std::vector<DemandId> neighbor = result;
      std::swap(neighbor[first], neighbor[second]);
      if (enforceBatch && config_.paintBatchLimit > 0 &&
          !isBatchValid(neighbor))
        continue;
      result = std::move(neighbor);
    }

    return result;
  }

  std::vector<Move> candidateMovesForStage(
      const std::vector<DemandId>& sequence, Stage stage, bool sameHprcOnly,
      bool criticalOnly) {
    std::vector<Move> moves;
    if (movableCount(sequence) < 2) return moves;

    std::vector<std::size_t> firstPositions;
    if (criticalOnly) {
      auto critical = criticalDemandsForStage(sequence, stage);
      if (critical.empty()) critical = sequence;
      std::unordered_set<DemandId> criticalSet(critical.begin(),
                                               critical.end());
      const std::size_t movableStart = firstMovableIndex(sequence);
      for (std::size_t idx = movableStart; idx < sequence.size(); ++idx) {
        if (criticalSet.contains(sequence[idx])) firstPositions.push_back(idx);
      }
    }
    if (firstPositions.empty()) {
      const std::size_t movableStart = firstMovableIndex(sequence);
      if (movableStart >= sequence.size()) return moves;
      firstPositions.clear();
      firstPositions.reserve(sequence.size() - movableStart);
      for (std::size_t idx = movableStart; idx < sequence.size(); ++idx)
        firstPositions.push_back(idx);
    }

    const auto hprcGroups = positionsByHprc(sequence);
    std::uniform_int_distribution<std::size_t> firstDist(
        0, firstPositions.size() - 1);
    const std::size_t movableStart = firstMovableIndex(sequence);
    std::uniform_int_distribution<std::size_t> anyPos(movableStart,
                                                      sequence.size() - 1);
    std::uniform_real_distribution<double> probability(0.0, 1.0);

    for (int iter = 0; iter < std::max(1, config_.samplesPerRound); ++iter) {
      const std::size_t first = firstPositions[firstDist(rng_)];
      std::size_t second = first;
      if (sameHprcOnly) {
        const auto groupIt = hprcGroups.find(hprcKey(sequence[first]));
        if (groupIt == hprcGroups.end() || groupIt->second.size() < 2)
          continue;
        std::uniform_int_distribution<std::size_t> inGroup(
            0, groupIt->second.size() - 1);
        second = groupIt->second[inGroup(rng_)];
      } else {
        second = anyPos(rng_);
      }
      if (first == second) continue;

      const MoveKind kind =
          sameHprcOnly || probability(rng_) < 0.45 ? MoveKind::Swap
                                                   : MoveKind::Insert;
      moves.push_back(Move{kind, first, second});
    }

    if (stage == Stage::Pcc || stage == Stage::Overall) {
      for (const auto idx : colorBoundaryPositions(sequence)) {
        if (idx > 0) moves.push_back(Move{MoveKind::Swap, idx, idx - 1});
        if (idx + 1 < sequence.size())
          moves.push_back(Move{MoveKind::Swap, idx, idx + 1});
        if (static_cast<int>(moves.size()) > config_.samplesPerRound * 2)
          break;
      }
    }

    std::shuffle(moves.begin(), moves.end(), rng_);
    return moves;
  }

  std::vector<DemandId> criticalDemandsForStage(
      const std::vector<DemandId>& sequence, Stage stage) const {
    std::vector<const RatioConstraint*> selectedRatios;
    if (stage == Stage::Hprc) {
      selectedRatios = hprcRatios_;
    } else if (stage == Stage::Lprc || stage == Stage::Pcc) {
      selectedRatios = lprcRatios_;
    } else {
      selectedRatios.reserve(ratios_.size());
      for (const auto& ratio : ratios_) selectedRatios.push_back(&ratio);
    }

    std::unordered_set<DemandId> critical;
    for (const auto* ratio : selectedRatios) {
      if (!ratio || ratio->q <= 0) continue;
      for (std::size_t idx = 0; idx < sequence.size(); ++idx) {
        int count = 0;
        const std::size_t end =
            std::min(sequence.size(),
                     idx + static_cast<std::size_t>(ratio->q));
        for (std::size_t j = idx; j < end; ++j) {
          if (hasOption(sequence[j], ratio->name)) ++count;
        }
        if (count > ratio->p) {
          const std::size_t criticalEnd =
              idx + std::min<std::size_t>(
                        static_cast<std::size_t>(std::max(0, ratio->p)),
                        sequence.size() - 1 - idx);
          for (std::size_t j = idx; j <= criticalEnd; ++j) {
            if (hasOption(sequence[j], ratio->name)) critical.insert(sequence[j]);
          }
        }
      }
    }

    if ((stage == Stage::Pcc || stage == Stage::Overall) &&
        critical.empty()) {
      for (const auto idx : colorBoundaryPositions(sequence)) {
        critical.insert(sequence[idx]);
        if (idx > 0) critical.insert(sequence[idx - 1]);
      }
    }

    return std::vector<DemandId>(critical.begin(), critical.end());
  }

  bool isBetterForStage(const Score& left, const Score& right, Stage stage,
                        bool strict) const {
    if (stage == Stage::Hprc) {
      return strict ? left.hprc < right.hprc : left.hprc <= right.hprc;
    }

    if (stage == Stage::Lprc) {
      const auto leftWeighted =
          static_cast<std::int64_t>(left.hprc) * config_.weights.hprc +
          static_cast<std::int64_t>(left.lprc) * config_.weights.lprc;
      const auto rightWeighted =
          static_cast<std::int64_t>(right.hprc) * config_.weights.hprc +
          static_cast<std::int64_t>(right.lprc) * config_.weights.lprc;
      const bool hprcSafe = left.hprc <= right.hprc;
      return hprcSafe &&
             (strict ? leftWeighted < rightWeighted
                     : leftWeighted <= rightWeighted);
    }

    if (stage == Stage::Pcc) {
      const bool hprcSafe = left.hprc <= right.hprc;
      const bool batchSafe = left.batch == 0 || left.batch <= right.batch;
      return hprcSafe && batchSafe &&
             (strict ? left.objective < right.objective
                     : left.objective <= right.objective);
    }

    return strict ? left.objective < right.objective
                  : left.objective <= right.objective;
  }

  std::size_t firstBatchViolation(
      const std::vector<DemandId>& sequence) const {
    if (config_.paintBatchLimit <= 0 || sequence.empty())
      return sequence.size();
    const std::size_t start = firstMovableIndex(sequence);
    if (start >= sequence.size()) return sequence.size();

    int run = 1;
    std::string previousColor = colorOf(sequence[start]);
    for (std::size_t idx = start + 1; idx < sequence.size(); ++idx) {
      const auto color = colorOf(sequence[idx]);
      if (color == previousColor) {
        ++run;
      } else {
        run = 1;
        previousColor = color;
      }
      if (run > config_.paintBatchLimit) return idx;
    }
    return sequence.size();
  }

  std::vector<DemandId> insertDemandAt(
      const std::vector<DemandId>& sequence, const DemandId& demand,
      std::size_t position) const {
    std::vector<DemandId> result = sequence;
    position = std::min(position, result.size());
    result.insert(result.begin() + static_cast<std::ptrdiff_t>(position),
                  demand);
    return result;
  }

  std::vector<DemandId> localSearch(
      std::vector<DemandId> sequence,
      const std::chrono::steady_clock::time_point& deadline) {
    using Clock = std::chrono::steady_clock;
    Score currentScore = score(sequence);
    bool improved = true;

    while (improved && Clock::now() < deadline) {
      improved = false;
      auto moves = candidateMoves(sequence);
      for (const auto& move : moves) {
        if (Clock::now() >= deadline) break;
        auto neighbor = applyMove(sequence, move);
        if (config_.paintBatchLimit > 0 && !isBatchValid(neighbor)) continue;
        const Score neighborScore = score(neighbor);
        if (isBetter(neighborScore, currentScore)) {
          sequence = std::move(neighbor);
          currentScore = neighborScore;
          improved = true;
          break;
        }
      }
    }

    return sequence;
  }

  std::vector<DemandId> shake(const std::vector<DemandId>& sequence,
                              int strength) {
    std::vector<DemandId> result = sequence;
    if (result.size() < 2) return result;

    auto critical = criticalDemands(result);
    if (critical.empty()) critical = result;
    auto hprcGroups = positionsByHprc(result);
    std::uniform_int_distribution<std::size_t> anyPos(0, result.size() - 1);

    for (int iter = 0; iter < std::max(1, strength); ++iter) {
      const int mode = iter % 3;
      if (mode == 0) {
        std::vector<std::vector<std::size_t>> groups;
        for (const auto& group : hprcGroups) {
          if (group.second.size() > 1) groups.push_back(group.second);
        }
        if (groups.empty()) continue;
        std::uniform_int_distribution<std::size_t> groupDist(0,
                                                             groups.size() - 1);
        const auto& group = groups[groupDist(rng_)];
        std::uniform_int_distribution<std::size_t> inGroup(0, group.size() - 1);
        std::size_t first = group[inGroup(rng_)];
        std::size_t second = group[inGroup(rng_)];
        if (first != second) std::swap(result[first], result[second]);
      } else {
        std::size_t first = positionOf(result, randomDemand(critical));
        std::size_t second = anyPos(rng_);
        if (first == second) continue;
        if (mode == 1)
          result = applyMove(result, Move{MoveKind::Insert, first, second});
        else
          std::swap(result[first], result[second]);
      }
    }

    return result;
  }

  std::vector<Move> candidateMoves(const std::vector<DemandId>& sequence) {
    std::vector<Move> moves;
    if (sequence.size() < 2) return moves;

    auto critical = criticalDemands(sequence);
    if (critical.empty()) critical = sequence;
    std::vector<std::size_t> criticalPositions;
    std::unordered_set<DemandId> criticalSet(critical.begin(), critical.end());
    for (std::size_t idx = 0; idx < sequence.size(); ++idx) {
      if (criticalSet.contains(sequence[idx])) criticalPositions.push_back(idx);
    }
    if (criticalPositions.empty()) {
      criticalPositions.resize(sequence.size());
      for (std::size_t idx = 0; idx < sequence.size(); ++idx)
        criticalPositions[idx] = idx;
    }

    auto hprcGroups = positionsByHprc(sequence);
    std::uniform_int_distribution<std::size_t> criticalDist(
        0, criticalPositions.size() - 1);
    std::uniform_int_distribution<std::size_t> anyPos(0, sequence.size() - 1);
    std::uniform_real_distribution<double> probability(0.0, 1.0);

    for (int iter = 0; iter < std::max(1, config_.samplesPerRound); ++iter) {
      const std::size_t first = criticalPositions[criticalDist(rng_)];
      const auto key = hprcKey(sequence[first]);
      const auto groupIt = hprcGroups.find(key);
      if (groupIt != hprcGroups.end() && groupIt->second.size() > 1 &&
          probability(rng_) < 0.45) {
        std::uniform_int_distribution<std::size_t> inGroup(
            0, groupIt->second.size() - 1);
        const std::size_t second = groupIt->second[inGroup(rng_)];
        if (first != second) moves.push_back(Move{MoveKind::Swap, first, second});
        continue;
      }

      const std::size_t second = anyPos(rng_);
      if (first == second) continue;
      moves.push_back(Move{probability(rng_) < 0.55 ? MoveKind::Insert
                                                     : MoveKind::Swap,
                           first, second});
    }

    for (const auto idx : colorBoundaryPositions(sequence)) {
      if (idx > 0) moves.push_back(Move{MoveKind::Swap, idx, idx - 1});
      if (idx + 1 < sequence.size())
        moves.push_back(Move{MoveKind::Swap, idx, idx + 1});
      if (static_cast<int>(moves.size()) > config_.samplesPerRound * 2) break;
    }

    std::shuffle(moves.begin(), moves.end(), rng_);
    return moves;
  }

  std::vector<DemandId> applyMove(const std::vector<DemandId>& sequence,
                                  const Move& move) const {
    std::vector<DemandId> result = sequence;
    const std::size_t movableStart = firstMovableIndex(result);
    if (move.from == move.to || move.from >= result.size() ||
        move.to > result.size() || move.from < movableStart ||
        move.to < movableStart)
      return result;
    if ((move.kind == MoveKind::Swap || move.kind == MoveKind::BlockSwap) &&
        move.to >= result.size())
      return result;
    if (move.kind == MoveKind::Insert) {
      auto demand = result[move.from];
      result.erase(result.begin() + static_cast<std::ptrdiff_t>(move.from));
      std::size_t target = move.to;
      if (move.to > move.from) target = move.to - 1;
      target = std::min(target, result.size());
      result.insert(result.begin() + static_cast<std::ptrdiff_t>(target),
                    std::move(demand));
    } else if (move.kind == MoveKind::BlockInsert) {
      const auto length = std::min(move.length, result.size() - move.from);
      if (length == 0) return result;
      auto blockBegin = result.begin() + static_cast<std::ptrdiff_t>(move.from);
      auto blockEnd = blockBegin + static_cast<std::ptrdiff_t>(length);
      std::vector<DemandId> block(blockBegin, blockEnd);
      result.erase(blockBegin, blockEnd);
      std::size_t target = move.to;
      if (move.to > move.from)
        target = move.to > length ? move.to - length : 0;
      target = std::min(target, result.size());
      result.insert(result.begin() + static_cast<std::ptrdiff_t>(target),
                    block.begin(), block.end());
    } else if (move.kind == MoveKind::BlockSwap) {
      if (move.from > move.to) return result;
      const auto firstLength =
          std::min(move.length, move.to - move.from);
      const auto secondLength =
          std::min(move.otherLength, result.size() - move.to);
      if (firstLength == 0 || secondLength == 0 ||
          move.from + firstLength > move.to)
        return result;

      std::vector<DemandId> firstBlock(
          result.begin() + static_cast<std::ptrdiff_t>(move.from),
          result.begin() +
              static_cast<std::ptrdiff_t>(move.from + firstLength));
      std::vector<DemandId> middle(
          result.begin() +
              static_cast<std::ptrdiff_t>(move.from + firstLength),
          result.begin() + static_cast<std::ptrdiff_t>(move.to));
      std::vector<DemandId> secondBlock(
          result.begin() + static_cast<std::ptrdiff_t>(move.to),
          result.begin() +
              static_cast<std::ptrdiff_t>(move.to + secondLength));

      std::vector<DemandId> rebuilt;
      rebuilt.reserve(result.size());
      rebuilt.insert(rebuilt.end(), result.begin(),
                     result.begin() + static_cast<std::ptrdiff_t>(move.from));
      rebuilt.insert(rebuilt.end(), secondBlock.begin(), secondBlock.end());
      rebuilt.insert(rebuilt.end(), middle.begin(), middle.end());
      rebuilt.insert(rebuilt.end(), firstBlock.begin(), firstBlock.end());
      rebuilt.insert(rebuilt.end(),
                     result.begin() +
                         static_cast<std::ptrdiff_t>(move.to + secondLength),
                     result.end());
      result = std::move(rebuilt);
    } else {
      std::swap(result[move.from], result[move.to]);
    }
    return result;
  }

  std::vector<DemandId> criticalDemands(
      const std::vector<DemandId>& sequence) const {
    std::unordered_set<DemandId> critical;
    for (const auto& ratio : ratios_) {
      if (ratio.q <= 0) continue;
      for (std::size_t idx = 0; idx < sequence.size(); ++idx) {
        int count = 0;
        const std::size_t end =
            std::min(sequence.size(),
                     idx + static_cast<std::size_t>(ratio.q));
        for (std::size_t j = idx; j < end; ++j) {
          if (hasOption(sequence[j], ratio.name)) ++count;
        }
        if (count > ratio.p) {
          const std::size_t criticalEnd =
              idx + std::min<std::size_t>(
                        static_cast<std::size_t>(std::max(0, ratio.p)),
                        sequence.size() - 1 - idx);
          for (std::size_t j = idx; j <= criticalEnd; ++j) {
            if (hasOption(sequence[j], ratio.name)) critical.insert(sequence[j]);
          }
        }
      }
    }
    return std::vector<DemandId>(critical.begin(), critical.end());
  }

  int ratioViolation(
      const std::vector<DemandId>& sequence,
      const std::vector<const RatioConstraint*>& ratios) const {
    int total = 0;
    for (const auto* ratio : ratios) {
      if (!ratio || ratio->q <= 0) continue;
      for (std::size_t idx = 0; idx < sequence.size(); ++idx) {
        int count = 0;
        const std::size_t end =
            std::min(sequence.size(),
                     idx + static_cast<std::size_t>(ratio->q));
        for (std::size_t j = idx; j < end; ++j) {
          if (hasOption(sequence[j], ratio->name)) ++count;
        }
        total += std::max(0, count - ratio->p);
      }
    }
    return total;
  }

  std::pair<int, int> paintCost(const std::vector<DemandId>& sequence) const {
    if (sequence.empty()) return {0, 0};
    const std::size_t start = firstMovableIndex(sequence);
    if (start >= sequence.size()) return {0, 0};
    int pcc = 0;
    int batchViolation = 0;
    int run = 1;
    std::string previousColor = colorOf(sequence[start]);
    for (std::size_t idx = start + 1; idx < sequence.size(); ++idx) {
      const auto color = colorOf(sequence[idx]);
      if (color == previousColor) {
        ++run;
      } else {
        ++pcc;
        if (config_.paintBatchLimit > 0)
          batchViolation += std::max(0, run - config_.paintBatchLimit);
        run = 1;
        previousColor = color;
      }
    }
    if (config_.paintBatchLimit > 0)
      batchViolation += std::max(0, run - config_.paintBatchLimit);
    return {pcc, batchViolation};
  }

  bool isBatchValid(const std::vector<DemandId>& sequence) const {
    return paintCost(sequence).second == 0;
  }

  bool isBetter(const Score& left, const Score& right) const {
    const bool noWorse = left.batch <= right.batch && left.hprc <= right.hprc &&
                         left.lprc <= right.lprc && left.pcc <= right.pcc;
    const bool strictlyBetter =
        left.batch < right.batch || left.hprc < right.hprc ||
        left.lprc < right.lprc || left.pcc < right.pcc ||
        left.objective < right.objective;
    return noWorse && strictlyBetter;
  }

  bool hasOption(const DemandId& demand, const std::string& option) const {
    const auto vehicleIt = vehicles_.find(demand);
    if (vehicleIt == vehicles_.end()) return false;
    const auto optionIt = vehicleIt->second.options.find(option);
    return optionIt != vehicleIt->second.options.end() && optionIt->second;
  }

  std::string colorOf(const DemandId& demand) const {
    const auto vehicleIt = vehicles_.find(demand);
    return vehicleIt == vehicles_.end() ? std::string() : vehicleIt->second.color;
  }

  std::string hprcKey(const DemandId& demand) const {
    std::string key;
    key.reserve(hprcRatios_.size());
    for (const auto* ratio : hprcRatios_) {
      key.push_back(hasOption(demand, ratio->name) ? '1' : '0');
    }
    return key;
  }

  std::unordered_map<std::string, std::vector<std::size_t>> positionsByHprc(
      const std::vector<DemandId>& sequence) const {
    std::unordered_map<std::string, std::vector<std::size_t>> groups;
    for (std::size_t idx = firstMovableIndex(sequence); idx < sequence.size();
         ++idx) {
      groups[hprcKey(sequence[idx])].push_back(idx);
    }
    return groups;
  }

  std::vector<std::size_t> colorBoundaryPositions(
      const std::vector<DemandId>& sequence) const {
    std::vector<std::size_t> result;
    const std::size_t start = firstMovableIndex(sequence);
    for (std::size_t idx = start + 1; idx < sequence.size(); ++idx) {
      if (colorOf(sequence[idx]) != colorOf(sequence[idx - 1]))
        result.push_back(idx);
    }
    return result;
  }

  std::vector<ColorRun> colorRuns(
      const std::vector<DemandId>& sequence) const {
    std::vector<ColorRun> result;
    if (sequence.empty()) return result;

    ColorRun current;
    const std::size_t start = firstMovableIndex(sequence);
    if (start >= sequence.size()) return result;

    current.start = start;
    current.length = 1;
    current.color = colorOf(sequence[start]);
    for (std::size_t idx = start + 1; idx < sequence.size(); ++idx) {
      const auto color = colorOf(sequence[idx]);
      if (color == current.color) {
        ++current.length;
      } else {
        result.push_back(current);
        current.start = idx;
        current.length = 1;
        current.color = color;
      }
    }
    result.push_back(current);
    return result;
  }

  std::vector<std::size_t> sameColorInsertionTargets(
      const std::vector<DemandId>& sequence, const ColorRun& source) const {
    std::vector<std::size_t> targets;
    if (source.length == 0 || source.start >= sequence.size()) return targets;

    const auto runs = colorRuns(sequence);
    for (const auto& run : runs) {
      if (run.color != source.color || run.start == source.start) continue;
      targets.push_back(run.start);
      targets.push_back(run.start + run.length);
    }

    // Boundary-adjacent alternatives help when an exact merge would violate
    // paint batch size but a near merge still removes one color transition.
    for (const auto boundary : colorBoundaryPositions(sequence)) {
      if (boundary < sequence.size() && colorOf(sequence[boundary]) == source.color)
        targets.push_back(boundary);
      if (boundary > 0 && colorOf(sequence[boundary - 1]) == source.color)
        targets.push_back(boundary);
    }

    std::sort(targets.begin(), targets.end());
    targets.erase(std::unique(targets.begin(), targets.end()), targets.end());
    return targets;
  }

  std::vector<DemandId> demandsOfColor(const std::vector<DemandId>& demands,
                                       const std::string& color) const {
    std::vector<DemandId> result;
    for (const auto& demand : demands) {
      if (colorOf(demand) == color) result.push_back(demand);
    }
    return result;
  }

  template <typename Fn>
  std::vector<DemandId> filterMinInt(const std::vector<DemandId>& candidates,
                                     Fn criterion) const {
    if (candidates.empty()) return candidates;
    std::vector<DemandId> result;
    int best = std::numeric_limits<int>::max();
    for (const auto& demand : candidates) {
      const int value = criterion(demand);
      if (value < best) {
        result.clear();
        best = value;
      }
      if (value == best) result.push_back(demand);
    }
    return result;
  }

  template <typename Fn>
  std::vector<DemandId> filterMaxDouble(const std::vector<DemandId>& candidates,
                                        Fn criterion) const {
    if (candidates.empty()) return candidates;
    std::vector<DemandId> result;
    double best = -std::numeric_limits<double>::infinity();
    for (const auto& demand : candidates) {
      const double value = criterion(demand);
      if (value > best) {
        result.clear();
        best = value;
      }
      if (value == best) result.push_back(demand);
    }
    return result;
  }

  int newViolationIfAppended(
      const std::vector<DemandId>& prefix, const DemandId& demand,
      const std::vector<const RatioConstraint*>& ratios) const {
    int total = 0;
    for (const auto* ratio : ratios) {
      if (!ratio || ratio->q <= 0 || !hasOption(demand, ratio->name)) continue;
      const std::size_t position = prefix.size();
      const std::size_t firstStart =
          position + 1 > static_cast<std::size_t>(ratio->q)
              ? position + 1 - static_cast<std::size_t>(ratio->q)
              : 0;
      if (position == 0) continue;
      for (std::size_t start = firstStart; start < position; ++start) {
        int count = 0;
        const std::size_t end =
            std::min(position, start + static_cast<std::size_t>(ratio->q) - 1);
        for (std::size_t idx = start; idx < end; ++idx) {
          if (hasOption(prefix[idx], ratio->name)) ++count;
        }
        if (count >= ratio->p) ++total;
      }
    }
    return total;
  }

  static void eraseFirst(std::vector<DemandId>& demands,
                         const DemandId& demand) {
    const auto it = std::find(demands.begin(), demands.end(), demand);
    if (it != demands.end()) demands.erase(it);
  }

  std::size_t firstMovableIndex(const std::vector<DemandId>& sequence) const {
    return std::min<std::size_t>(
        sequence.size(), static_cast<std::size_t>(std::max(0, config_.frozenPrefix)));
  }

  std::size_t movableCount(const std::vector<DemandId>& sequence) const {
    const auto start = firstMovableIndex(sequence);
    return start >= sequence.size() ? 0 : sequence.size() - start;
  }

  DemandId randomDemand(const std::vector<DemandId>& demands) {
    std::uniform_int_distribution<std::size_t> dist(0, demands.size() - 1);
    return demands[dist(rng_)];
  }

  static std::size_t positionOf(const std::vector<DemandId>& sequence,
                                const DemandId& demand) {
    const auto it = std::find(sequence.begin(), sequence.end(), demand);
    return it == sequence.end()
               ? 0
               : static_cast<std::size_t>(std::distance(sequence.begin(), it));
  }

  std::unordered_map<DemandId, Vehicle> vehicles_;
  std::vector<RatioConstraint> ratios_;
  Config config_;
  std::mt19937 rng_;
  std::vector<const RatioConstraint*> hprcRatios_;
  std::vector<const RatioConstraint*> lprcRatios_;
};

Result optimize(const std::unordered_map<DemandId, Vehicle>& vehicles,
                const std::vector<RatioConstraint>& ratios,
                const std::vector<DemandId>& initialSequence,
                const Config& config) {
  SequenceVNS solver(vehicles, ratios, config);
  return solver.optimize(initialSequence);
}

Score score(const std::unordered_map<DemandId, Vehicle>& vehicles,
            const std::vector<RatioConstraint>& ratios,
            const std::vector<DemandId>& sequence, const Config& config) {
  SequenceVNS solver(vehicles, ratios, config);
  return solver.score(sequence);
}

}  // namespace roadef_vns

const MetaClass* SolverVNS::metadata;

namespace {

using roadef_vns::Config;
using roadef_vns::DemandId;
using roadef_vns::RatioConstraint;
using roadef_vns::Score;
using roadef_vns::Vehicle;

std::string getString(const rapidjson::Value& value, const char* member,
                      const std::string& fallback = std::string()) {
  if (!value.IsObject() || !value.HasMember(member) ||
      !value[member].IsString())
    return fallback;
  return value[member].GetString();
}

int getInt(const rapidjson::Value& value, const char* member, int fallback) {
  if (!value.IsObject() || !value.HasMember(member)) return fallback;
  const auto& memberValue = value[member];
  if (memberValue.IsInt()) return memberValue.GetInt();
  if (memberValue.IsUint()) return static_cast<int>(memberValue.GetUint());
  return fallback;
}

double getDouble(const rapidjson::Value& value, const char* member,
                 double fallback) {
  if (!value.IsObject() || !value.HasMember(member) ||
      !value[member].IsNumber())
    return fallback;
  return value[member].GetDouble();
}

void writeScore(rapidjson::Writer<rapidjson::StringBuffer>& writer,
                const Score& score) {
  writer.StartObject();
  writer.Key("hprc");
  writer.Int(score.hprc);
  writer.Key("lprc");
  writer.Int(score.lprc);
  writer.Key("pcc");
  writer.Int(score.pcc);
  writer.Key("batch");
  writer.Int(score.batch);
  writer.Key("objective");
  writer.Int64(score.objective);
  writer.EndObject();
}

PyObject* solverVNS_optimizeJSON(PyObject*, PyObject* args) {
  const char* input = nullptr;
  if (!PyArg_ParseTuple(args, "s:optimizeJSON", &input)) return nullptr;

  rapidjson::Document document;
  document.Parse(input);
  if (document.HasParseError() || !document.IsObject()) {
    PyErr_SetString(PyExc_ValueError, "Invalid JSON input");
    return nullptr;
  }

  std::unordered_map<DemandId, Vehicle> vehicles;
  if (document.HasMember("vehicles") && document["vehicles"].IsArray()) {
    for (const auto& row : document["vehicles"].GetArray()) {
      Vehicle vehicle;
      vehicle.demand = getString(row, "demand");
      vehicle.color = getString(row, "color");
      vehicle.sequenceRank = getInt(row, "sequence_rank", 0);
      if (row.HasMember("options") && row["options"].IsObject()) {
        for (const auto& option : row["options"].GetObject()) {
          vehicle.options[option.name.GetString()] =
              option.value.IsBool() && option.value.GetBool();
        }
      }
      if (!vehicle.demand.empty()) vehicles[vehicle.demand] = std::move(vehicle);
    }
  }

  std::vector<RatioConstraint> ratios;
  if (document.HasMember("ratios") && document["ratios"].IsArray()) {
    for (const auto& row : document["ratios"].GetArray()) {
      RatioConstraint ratio;
      ratio.name = getString(row, "name");
      ratio.p = getInt(row, "p", 0);
      ratio.q = getInt(row, "q", 1);
      ratio.highPriority = getString(row, "priority") == "HPRC";
      if (!ratio.name.empty()) ratios.push_back(std::move(ratio));
    }
  }

  std::vector<DemandId> sequence;
  if (document.HasMember("sequence") && document["sequence"].IsArray()) {
    for (const auto& demand : document["sequence"].GetArray()) {
      if (demand.IsString()) sequence.emplace_back(demand.GetString());
    }
  }

  Config config;
  if (document.HasMember("config") && document["config"].IsObject()) {
    const auto& cfg = document["config"];
    config.timeLimitSeconds =
        getDouble(cfg, "time_limit_seconds", config.timeLimitSeconds);
    config.samplesPerRound =
        getInt(cfg, "samples_per_round", config.samplesPerRound);
    config.maxNoImprovement =
        getInt(cfg, "max_no_improvement", config.maxNoImprovement);
    config.paintBatchLimit =
        getInt(cfg, "paint_batch_limit", config.paintBatchLimit);
    config.frozenPrefix = getInt(cfg, "frozen_prefix", config.frozenPrefix);
    config.randomSeed =
        static_cast<unsigned int>(getInt(cfg, "random_seed", config.randomSeed));
    config.graspIterations =
        getInt(cfg, "grasp_iterations", config.graspIterations);
    config.graspRclSize = getInt(cfg, "grasp_rcl_size", config.graspRclSize);
    config.globalVnsNeighborhoods =
        getInt(cfg, "global_vns_neighborhoods", config.globalVnsNeighborhoods);
    config.shakingStrength =
        getInt(cfg, "shaking_strength", config.shakingStrength);
    config.blockMaxLength = getInt(cfg, "block_max_length", config.blockMaxLength);
  }

  if (vehicles.empty() || sequence.size() < 2) {
    PyErr_SetString(PyExc_ValueError, "Missing vehicles or sequence");
    return nullptr;
  }

  const auto result = roadef_vns::optimize(vehicles, ratios, sequence, config);

  rapidjson::StringBuffer buffer;
  rapidjson::Writer<rapidjson::StringBuffer> writer(buffer);
  writer.StartObject();
  writer.Key("sequence");
  writer.StartArray();
  for (const auto& demand : result.sequence) writer.String(demand.c_str());
  writer.EndArray();
  writer.Key("initialScore");
  writeScore(writer, result.initialScore);
  writer.Key("bestScore");
  writeScore(writer, result.bestScore);
  writer.EndObject();

  return PyUnicode_FromString(buffer.GetString());
}

}  // namespace

int SolverVNS::initialize() {
  metadata = MetaClass::registerClass<SolverVNS>(
      "solver", "solver_vns", Object::create<SolverVNS>);
  registerFields<SolverVNS>(const_cast<MetaClass*>(metadata));

  auto& x = PythonExtension<SolverVNS>::getPythonType();
  x.setName("solverVNS");
  x.setDoc("ccAPPS ROADEF/VNS sequence optimizer");
  x.supportgetattro();
  x.supportsetattro();
  x.supportcreate(SolverVNS::create);
  x.addMethod("solve",
              static_cast<PyObject* (*)(PyObject*, PyObject*, PyObject*)>(
                  SolverCreate::solve),
              METH_VARARGS, "run the base MRP solver");
  x.addMethod("commit", SolverCreate::commit, METH_NOARGS,
              "commit the plan changes");
  x.addMethod("rollback", SolverCreate::rollback, METH_NOARGS,
              "rollback the plan changes");
  x.addMethod("optimizeJSON", solverVNS_optimizeJSON, METH_VARARGS,
              "Optimize a ROADEF sequence problem encoded as JSON");

  SolverVNS::metadata->setPythonClass(x);
  return x.typeReady();
}

PyObject* SolverVNS::create(PyTypeObject*, PyObject*, PyObject*) {
  return Object::create<SolverVNS>();
}

}  // namespace ccAPPS

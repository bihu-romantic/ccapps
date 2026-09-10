// Independent MRP plus genetic-algorithm scheduler for flow-line cases.

#include <algorithm>
#include <chrono>
#include <cmath>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iomanip>
#include <iostream>
#include <map>
#include <numeric>
#include <random>
#include <set>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>

#include "ga.h"

namespace fs = std::filesystem;

struct Demand { std::string id, item; double quantity, due; int priority; bool frozen; };
struct Operation { std::string id, item; double seconds, minimum, multiple, maximum; bool allow_cross_shift; };
struct Flow { std::string operation, item; double quantity; };
struct Scheduled { std::string demand, operation, status; std::vector<std::string> resources; double quantity, start, end; };
struct PurchaseRule { double leadtime, minimum, multiple; };
struct PurchaseOrder { std::string demand, item; double quantity, recommended_order, required_receipt, feasible_receipt; bool late; };
struct FrozenTask { std::string resource; double start, end; };
struct RescheduleEvent { std::string type, demand, resource; double start = 0, end = 0; };
struct SupplyLot { double available, quantity; bool shortage = false; };
struct DeviceState {
  std::string device, resource, status, risk;
  bool available = true;
  double capacity_factor = 1;
};
struct Problem {
  std::string type, severity, demand, operation, object, action;
  double quantity = 0, risk_seconds = 0;
};
struct Result { double fitness = 0; std::vector<Scheduled> tasks; std::vector<PurchaseOrder> purchases; std::vector<std::string> diagnostics; std::vector<Problem> problems; };

std::vector<std::string> split(const std::string& line) {
  std::vector<std::string> fields; std::string current; bool quoted = false;
  for (char c : line) {
    if (c == '"') quoted = !quoted;
    else if (c == ',' && !quoted) { fields.push_back(current); current.clear(); }
    else current += c;
  }
  fields.push_back(current);
  for (auto& field : fields) {
    if (!field.empty() && field.back() == '\r') field.pop_back();
  }
  return fields;
}

std::vector<std::unordered_map<std::string, std::string>> csv(const fs::path& path) {
  std::ifstream input(path); std::string line;
  if (!std::getline(input, line)) throw std::runtime_error("cannot read " + path.string());
  auto header = split(line); std::vector<std::unordered_map<std::string, std::string>> rows;
  while (std::getline(input, line)) {
    auto values = split(line); std::unordered_map<std::string, std::string> row;
    for (size_t i = 0; i < header.size() && i < values.size(); ++i) row[header[i]] = values[i];
    rows.push_back(std::move(row));
  }
  return rows;
}

int line_number(const std::string& value) {
  // flow_line item names follow "product NN-SS".
  if (value.size() < 10) return -1;
  try { return std::stoi(value.substr(8, 2)); } catch (...) { return -1; }
}

int operation_line_number(const std::string& value) {
  try { return std::stoi(value.substr(0, 2)); } catch (...) { return -1; }
}

int step_number(const std::string& value) {
  // flow_line operation names follow "NN step SS".
  if (value.size() < 10) return -1;
  try { return std::stoi(value.substr(value.size() - 2)); } catch (...) { return -1; }
}

class FlowLineScheduler {
 public:
  explicit FlowLineScheduler(const fs::path& data_dir, bool enable_device_state = false) { load(data_dir, enable_device_state); }

  Result run_ga() {
    // MRP remains the source of truth for materials, calendars and all
    // simultaneously-required resources. GA searches only the resource order
    // and primary-machine choices, which are decoded through those constraints.
    std::vector<chaozhenyun::ga::Job> jobs;
    std::vector<std::string> machine_names;
    std::unordered_map<std::string, int> machine_index;
    auto machine = [&](const std::string& name) {
      auto [item, inserted] = machine_index.emplace(name, static_cast<int>(machine_names.size()));
      if (inserted) machine_names.push_back(name);
      return item->second;
    };
    for (const auto& demand : demands_) {
      chaozhenyun::ga::Job job;
      for (const auto& operation_id : route_for(demand)) {
        chaozhenyun::ga::Operation operation;
        for (const auto& option : resource_options(operation_id)) {
          // The first resource is the equipment-selection gene. Remaining resources
          // (operators, tools) remain hard constraints in evaluate().
          if (!option.empty()) {
            operation.machines.push_back(machine(option.front()));
            operation.durations.push_back(std::max(1, static_cast<int>(operations_.at(operation_id).seconds * demand.quantity)));
          }
        }
        if (!operation.machines.empty()) job.operations.push_back(std::move(operation));
      }
      jobs.push_back(std::move(job));
    }
    const auto ga_result = chaozhenyun::ga::solve(jobs, static_cast<int>(machine_names.size()));
    std::vector<int> order;
    std::set<int> inserted;
    // First occurrence in the OS chromosome defines a precedence-safe order
    // for the MRP batch decoder; repeated occurrences represent later routes.
    for (int job : ga_result.operation_sequence)
      if (inserted.insert(job).second) order.push_back(job);
    for (int job = 0; job < static_cast<int>(demands_.size()); ++job)
      if (inserted.insert(job).second) order.push_back(job);
    return evaluate(order, false);
  }

  Result run_mrp(bool backward = false) const {
    std::vector<int> order(demands_.size());
    std::iota(order.begin(), order.end(), 0);
    // MRP baseline: retain input order and only expand material and routing constraints.
    return evaluate(order, backward);
  }

  Result run_reschedule(const fs::path& previous_schedule, const fs::path& events_file, double now) {
    const auto previous = read_schedule(previous_schedule);
    const auto events = read_events(events_file);
    std::set<std::string> affected;
    for (const auto& event : events) {
      if (!event.demand.empty()) affected.insert(event.demand);
      if (!event.resource.empty()) {
        for (const auto& task : previous)
          if (std::find(task.resources.begin(), task.resources.end(), event.resource) != task.resources.end() && task.end > event.start)
            affected.insert(task.demand);
      }
    }
    if (affected.empty()) throw std::runtime_error("reschedule events do not affect any demand");

    frozen_tasks_.clear();
    outages_.clear();
    historical_tasks_.clear();
    historical_batch_count_.clear();
    for (const auto& event : events)
      if (event.type == "equipment_unavailable" && !event.resource.empty() && event.end > event.start)
        outages_[event.resource].push_back({event.start, event.end});
    std::vector<Scheduled> preserved;
    for (auto task : previous) {
      if (task.status == "planned") {
        if (task.end <= now) task.status = "confirmed";
        else if (task.start < now) task.status = "running";
      }
      // Confirmed/completed and running work is immutable, even when its order is affected.
      // Released and planned work of an affected order can be recalculated.
      const bool immutable = task.status == "confirmed" || task.status == "completed" || task.status == "running" || task.end <= now;
      const bool cancelled = task.status == "cancelled";
      if (cancelled) continue;
      if (immutable) {
        historical_tasks_.push_back(task);
        historical_batch_count_[{task.demand, task.operation}]++;
      }
      if (affected.contains(task.demand) && !immutable) continue;
      preserved.push_back(task);
      for (const auto& resource : task.resources) frozen_tasks_.push_back({resource, task.start, task.end});
    }
    std::vector<int> order(demands_.size());
    std::iota(order.begin(), order.end(), 0);
    Result result = evaluate(order, false, &affected);
    result.tasks.insert(result.tasks.end(), preserved.begin(), preserved.end());
    std::sort(result.tasks.begin(), result.tasks.end(), [](const auto& left, const auto& right) {
      return left.start == right.start ? left.demand < right.demand : left.start < right.start;
    });
    result.diagnostics.push_back("reschedule: affected_demands=" + std::to_string(affected.size()) + ", frozen_tasks=" + std::to_string(preserved.size()) + ", now=" + std::to_string(now));
    return result;
  }

  static std::vector<Scheduled> read_schedule(const fs::path& path) {
    std::vector<Scheduled> tasks;
    for (const auto& row : csv(path)) {
      std::vector<std::string> resources; std::stringstream stream(row.at("resources")); std::string resource;
      while (std::getline(stream, resource, '+')) resources.push_back(resource);
      tasks.push_back({row.at("demand"), row.at("operation"), row.count("status") ? row.at("status") : "planned", resources, std::stod(row.at("quantity")), std::stod(row.at("start_seconds")), std::stod(row.at("end_seconds"))});
    }
    return tasks;
  }

 private:
  std::vector<Demand> demands_;
  std::unordered_map<std::string, Operation> operations_;
  std::unordered_map<std::string, std::vector<std::string>> resources_;
  std::unordered_map<std::string, std::vector<std::pair<std::string, std::string>>> alternatives_;
  std::unordered_map<std::string, std::vector<std::string>> operation_alternatives_;
  std::unordered_map<std::string, int> capacity_;
  struct CalendarWindow { int weekday = -1; std::string date; double start, end; };
  std::unordered_map<std::string, std::vector<CalendarWindow>> calendars_;
  std::unordered_map<std::string, std::vector<Flow>> flows_;
  std::unordered_map<std::string, double> inventory_;
  std::unordered_map<std::string, PurchaseRule> purchasable_;
  // Optional supply-path rules allow a material to select a manufacturing
  // route before falling back to purchasing it.
  std::unordered_map<std::string, std::vector<std::string>> manufacturing_paths_;
  std::unordered_map<std::string, std::vector<std::string>> explicit_routes_;
  std::vector<FrozenTask> frozen_tasks_;
  std::unordered_map<std::string, std::vector<std::pair<double, double>>> outages_;
  std::unordered_map<std::string, DeviceState> device_states_;
  std::vector<Scheduled> historical_tasks_;
  std::map<std::pair<std::string, std::string>, size_t> historical_batch_count_;

  static std::vector<RescheduleEvent> read_events(const fs::path& path) {
    std::vector<RescheduleEvent> events;
    for (const auto& row : csv(path)) events.push_back({
      row.at("event_type"), row.count("demand") ? row.at("demand") : "", row.count("resource") ? row.at("resource") : "",
      row.count("start_seconds") && !row.at("start_seconds").empty() ? std::stod(row.at("start_seconds")) : 0,
      row.count("end_seconds") && !row.at("end_seconds").empty() ? std::stod(row.at("end_seconds")) : 0});
    return events;
  }

  void load(const fs::path& dir, bool enable_device_state) {
    double default_due_seconds = 0;
    const auto planning_dates = dir / "planning_dates.csv";
    if (fs::exists(planning_dates)) {
      const auto dates = csv(planning_dates);
      if (!dates.empty()) {
        const auto& row = dates.front();
        const auto start = std::chrono::system_clock::from_time_t(0);
        (void)start;
        // ISO dates in the case profile are converted in UTC. The solver
        // itself keeps its time axis as seconds from plan_start.
        std::tm begin{}, due{};
        std::istringstream begin_stream(row.at("plan_start")), due_stream(row.at("due_datetime"));
        begin_stream >> std::get_time(&begin, "%Y-%m-%dT%H:%M:%S");
        due_stream >> std::get_time(&due, "%Y-%m-%dT%H:%M:%S");
        default_due_seconds = std::difftime(std::mktime(&due), std::mktime(&begin));
      }
    }
    for (const auto& row : csv(dir / "demands.csv")) demands_.push_back({row.at("id"), row.at("item"), std::stod(row.at("quantity")), row.count("due_seconds") && !row.at("due_seconds").empty() ? std::stod(row.at("due_seconds")) : default_due_seconds, std::stoi(row.at("priority")), row.count("frozen") && row.at("frozen") == "1"});
    for (const auto& row : csv(dir / "resources.csv")) capacity_[row.at("id")] = std::max(1, static_cast<int>(std::stod(row.at("capacity"))));
    const auto capacity_overrides = dir / "resource_capacity_overrides.csv";
    if (fs::exists(capacity_overrides))
      for (const auto& row : csv(capacity_overrides))
        capacity_[row.at("resource")] = std::max(1, std::stoi(row.at("capacity")));
    for (const auto& row : csv(dir / "operations.csv")) operations_[row.at("id")] = {
      row.at("id"), row.at("output_item"), std::stod(row.at("duration_seconds")),
      std::stod(row.at("minimum")), std::stod(row.at("multiple")), std::stod(row.at("maximum")),
      row.count("allow_cross_shift") && (row.at("allow_cross_shift") == "1" || row.at("allow_cross_shift") == "true")};
    for (const auto& row : csv(dir / "operation_resources.csv")) resources_[row.at("operation")].push_back(row.at("resource"));
    const auto alternative_path = dir / "resource_alternatives.csv";
    if (fs::exists(alternative_path)) {
      for (const auto& row : csv(alternative_path))
        alternatives_[row.at("operation")].push_back({row.at("resource"), row.at("alternative_resource")});
    }
    const auto operation_alternatives = dir / "operation_alternatives.csv";
    if (fs::exists(operation_alternatives))
      for (const auto& row : csv(operation_alternatives))
        operation_alternatives_[row.at("operation")].push_back(row.at("alternative_operation"));
    for (const auto& row : csv(dir / "resource_calendars.csv")) calendars_[row.at("resource")].push_back({
      row.count("weekday") && !row.at("weekday").empty() ? std::stoi(row.at("weekday")) : -1,
      row.count("date") ? row.at("date") : "", std::stod(row.at("start_seconds")), std::stod(row.at("end_seconds"))});
    for (const auto& row : csv(dir / "operation_materials.csv")) flows_[row.at("operation")].push_back({row.at("operation"), row.at("item"), std::stod(row.at("quantity"))});
    for (const auto& row : csv(dir / "inventory.csv")) inventory_[row.at("item")] += std::stod(row.at("quantity"));
    for (const auto& row : csv(dir / "purchasable_items.csv")) purchasable_[row.at("item")] = {std::stod(row.at("leadtime_seconds")), std::stod(row.at("minimum")), std::stod(row.at("multiple"))};
    const auto paths = dir / "supply_paths.csv";
    if (fs::exists(paths))
      for (const auto& row : csv(paths))
        if (row.count("item") && row.count("operation") && row.at("path_type") == "manufacture")
          manufacturing_paths_[row.at("item")].push_back(row.at("operation"));
    const auto routes = dir / "routes.csv";
    if (fs::exists(routes)) {
      std::map<std::string, std::vector<std::pair<int, std::string>>> ordered;
      for (const auto& row : csv(routes)) ordered[row.at("item")].push_back({std::stoi(row.at("sequence")), row.at("operation")});
      for (auto& [item, entries] : ordered) {
        std::sort(entries.begin(), entries.end());
        for (const auto& entry : entries) explicit_routes_[item].push_back(entry.second);
      }
    }
    const auto frozen_path = dir / "frozen_tasks.csv";
    if (fs::exists(frozen_path))
      for (const auto& row : csv(frozen_path)) frozen_tasks_.push_back({row.at("resource"), std::stod(row.at("start_seconds")), std::stod(row.at("end_seconds"))});
    if (enable_device_state) load_device_states(dir);
  }

  void load_device_states(const fs::path& dir) {
    const auto map_path = dir / "device_resource_map.csv";
    const auto state_path = dir / "device_states.csv";
    if (!fs::exists(map_path) || !fs::exists(state_path)) return;

    std::unordered_map<std::string, std::string> device_to_resource;
    for (const auto& row : csv(map_path)) {
      if (!row.count("device_id") || !row.count("resource_id")) continue;
      device_to_resource[row.at("device_id")] = row.at("resource_id");
    }

    for (const auto& row : csv(state_path)) {
      if (!row.count("device_id")) continue;
      const auto mapped = device_to_resource.find(row.at("device_id"));
      if (mapped == device_to_resource.end()) continue;
      DeviceState state;
      state.device = row.at("device_id");
      state.resource = mapped->second;
      if (row.count("status") && !row.at("status").empty()) state.status = row.at("status");
      else if (row.count("health_grade") && !row.at("health_grade").empty()) state.status = row.at("health_grade");
      else if (row.count("fault_patterns") && !row.at("fault_patterns").empty()) state.status = row.at("fault_patterns");
      else state.status = "unknown";
      state.risk = row.count("risk_level") ? row.at("risk_level") : "";
      state.available = !(row.count("available") && (row.at("available") == "0" || row.at("available") == "false"));
      if (row.count("capacity_factor") && !row.at("capacity_factor").empty())
        state.capacity_factor = std::stod(row.at("capacity_factor"));
      if (state.capacity_factor <= 0) state.available = false;
      device_states_[state.resource] = state;  // Later rows become the latest snapshot.
    }
  }

  bool is_working_day(const CalendarWindow& window, double day) const {
    // Day zero is Monday. Date-specific exceptions use a day offset in the input
    // so the solver remains independent from a particular wall-clock epoch.
    if (!window.date.empty()) return std::stod(window.date) == day;
    return window.weekday < 0 || window.weekday == static_cast<int>(std::fmod(day, 7.0));
  }

  double next_slot(const std::string& resource, double requested, double duration, bool allow_cross_shift) const {
    const auto found = calendars_.find(resource);
    // A resource without an exported calendar remains continuously available.
    if (found == calendars_.end() || found->second.empty()) return requested;
    double candidate = requested;
    for (;;) {
      const double day = std::floor(candidate / 86400.0);
      const double time_of_day = candidate - day * 86400.0;
      for (const auto& window : found->second) {
        if (!is_working_day(window, day)) continue;
        const double start = day * 86400.0 + std::max(time_of_day, window.start);
        if (!allow_cross_shift && start + duration <= day * 86400.0 + window.end) return start;
        if (allow_cross_shift && start < day * 86400.0 + window.end) return start;
      }
      candidate = (day + 1) * 86400.0;
    }
  }

  double finish_slot(const std::string& resource, double start, double duration, bool allow_cross_shift) const {
    if (!allow_cross_shift) return start + duration;
    const auto found = calendars_.find(resource);
    if (found == calendars_.end() || found->second.empty()) return start + duration;
    double cursor = start, remaining = duration;
    while (remaining > 1e-9) {
      const double day = std::floor(cursor / 86400.0);
      const double time_of_day = cursor - day * 86400.0;
      bool consumed = false;
      for (const auto& window : found->second) {
        if (!is_working_day(window, day) || time_of_day >= window.end) continue;
        const double segment_start = day * 86400.0 + std::max(time_of_day, window.start);
        if (segment_start >= day * 86400.0 + window.end) continue;
        const double usable = day * 86400.0 + window.end - segment_start;
        if (remaining <= usable) return segment_start + remaining;
        remaining -= usable;
        cursor = (day + 1) * 86400.0;
        consumed = true;
        break;
      }
      if (!consumed) cursor = (day + 1) * 86400.0;
    }
    return cursor;
  }

  double outage_end(const std::string& resource, double start, double end) const {
    const auto found = outages_.find(resource);
    if (found == outages_.end()) return 0;
    double conflict_end = 0;
    for (const auto& outage : found->second)
      if (start < outage.second && end > outage.first) conflict_end = std::max(conflict_end, outage.second);
    return conflict_end;
  }

  bool device_available(const std::vector<std::string>& candidate) const {
    for (const auto& resource : candidate) {
      const auto found = device_states_.find(resource);
      if (found != device_states_.end() && !found->second.available) return false;
    }
    return true;
  }

  double effective_duration(const std::vector<std::string>& candidate, double base_duration) const {
    double factor = 1;
    for (const auto& resource : candidate) {
      const auto found = device_states_.find(resource);
      if (found == device_states_.end()) continue;
      factor = std::min(factor, std::max(0.0, found->second.capacity_factor));
    }
    if (factor <= 1e-9) return std::numeric_limits<double>::infinity();
    return base_duration / factor;
  }

  double production_quantity(const Operation& operation, double requested) const {
    double quantity = std::max(requested, operation.minimum);
    if (operation.multiple > 0) quantity = std::ceil(quantity / operation.multiple) * operation.multiple;
    return quantity;
  }

  std::vector<double> split_batches(const Operation& operation, double quantity) const {
    const double maximum = operation.maximum > 0 ? operation.maximum : quantity;
    std::vector<double> batches;
    // Round total output first, then distribute it into compliant batches.
    // This prevents an invalid final remainder below the minimum or multiple.
    quantity = production_quantity(operation, quantity);
    while (quantity > 1e-9) {
      double batch = std::min(quantity, maximum);
      if (operation.multiple > 0) batch = std::floor(batch / operation.multiple) * operation.multiple;
      if (batch < operation.minimum) batch = std::min(quantity, std::max(operation.minimum, operation.multiple));
      if (batch <= 1e-9) throw std::runtime_error("invalid manufacturing batch rule for " + operation.id);
      batches.push_back(batch);
      quantity -= batch;
    }
    return batches;
  }

  std::vector<std::string> route_for(const Demand& demand) const {
    const auto explicit_route = explicit_routes_.find(demand.item);
    if (explicit_route != explicit_routes_.end()) return explicit_route->second;
    const int line = line_number(demand.item); std::vector<std::pair<int, std::string>> route;
    for (const auto& [name, operation] : operations_) if (operation_line_number(name) == line) route.push_back({step_number(name), name});
    // flow_line represents the finished demand as step 01; its material flow
    // is therefore planned from the highest numbered upstream step downward.
    std::sort(route.rbegin(), route.rend()); std::vector<std::string> output;
    for (const auto& item : route) output.push_back(item.second);
    if (!output.empty()) return output;

    // Generic path expansion: material rules can nominate a manufacturing
    // operation; otherwise the first operation producing the item is used.
    std::set<std::string> visiting, appended;
    std::function<void(const std::string&)> expand = [&](const std::string& item) {
      if (!visiting.insert(item).second) throw std::runtime_error("manufacturing path cycle at " + item);
      std::string selected;
      const auto configured = manufacturing_paths_.find(item);
      if (configured != manufacturing_paths_.end() && !configured->second.empty()) selected = configured->second.front();
      if (selected.empty()) for (const auto& [id, operation] : operations_) if (operation.item == item) { selected = id; break; }
      if (!selected.empty()) {
        const auto flow = flows_.find(selected);
        if (flow != flows_.end()) for (const auto& material : flow->second)
          if (material.quantity < 0 && manufacturing_paths_.contains(material.item)) expand(material.item);
        if (appended.insert(selected).second) output.push_back(selected);
      }
      visiting.erase(item);
    };
    expand(demand.item);
    return output;
  }

  std::vector<std::vector<std::string>> resource_options(const std::string& operation) const {
    std::vector<std::vector<std::string>> options = {resources_.at(operation)};
    const auto operation_alts = operation_alternatives_.find(operation);
    if (operation_alts != operation_alternatives_.end())
      for (const auto& alternate : operation_alts->second) {
        const auto resources = resources_.find(alternate);
        if (resources != resources_.end()) options.push_back(resources->second);
      }
    const auto alternatives = alternatives_.find(operation);
    if (alternatives == alternatives_.end()) return options;
    // Each replacement keeps the other simultaneously-required resources intact.
    for (const auto& [required, replacement] : alternatives->second) {
      auto candidate = resources_.at(operation);
      auto position = std::find(candidate.begin(), candidate.end(), required);
      if (position != candidate.end()) { *position = replacement; options.push_back(candidate); }
    }
    return options;
  }

  bool select_resources(
      const std::string& operation, double requested, double duration,
      const std::unordered_map<std::string, std::vector<double>>& available,
      double& start, double& end, std::vector<std::string>& selected) const {
    bool found = false;
    double earliest_end = 0;
    for (const auto& candidate : resource_options(operation)) {
      if (!device_available(candidate)) continue;
      const double adjusted_duration = effective_duration(candidate, duration);
      if (!std::isfinite(adjusted_duration)) continue;
      double aligned_start = requested;
      for (;;) {
        double next_start = aligned_start;
        for (const auto& resource : candidate) {
          const auto availability = available.find(resource);
          const double free_at = availability == available.end() ? 0 : *std::min_element(availability->second.begin(), availability->second.end());
          next_start = std::max(next_start, next_slot(resource, std::max(aligned_start, free_at), adjusted_duration, operations_.at(operation).allow_cross_shift));
        }
        if (next_start == aligned_start) break;
        aligned_start = next_start;
      }
      double candidate_end = aligned_start + adjusted_duration;
      for (const auto& resource : candidate)
        candidate_end = std::max(candidate_end, finish_slot(resource, aligned_start, adjusted_duration, operations_.at(operation).allow_cross_shift));
      double blocked_until = 0;
      for (const auto& resource : candidate) blocked_until = std::max(blocked_until, outage_end(resource, aligned_start, candidate_end));
      if (blocked_until > 0) {
        aligned_start = blocked_until;
        continue;
      }
      if (!found || candidate_end < earliest_end) {
        found = true;
        earliest_end = candidate_end;
        start = aligned_start;
        end = candidate_end;
        selected = candidate;
      }
    }
    return found;
  }

  double route_duration(const Demand& demand, const std::vector<std::string>& route) const {
    double total = 0;
    for (const auto& operation_id : route) {
      const auto& operation = operations_.at(operation_id);
      for (double batch : split_batches(operation, production_quantity(operation, demand.quantity))) total += operation.seconds * batch;
    }
    return total;
  }

  Result evaluate(const std::vector<int>& chromosome, bool backward, const std::set<std::string>* active_demands = nullptr) const {
    // Material lots retain their availability time. This lets completed work
    // feed downstream work immediately, while running work feeds it on finish.
    std::unordered_map<std::string, std::vector<SupplyLot>> supplies;
    for (const auto& [item, quantity] : inventory_) supplies[item].push_back({0, quantity, false});
    auto consume = [&](const std::string& item, double quantity) {
      for (auto& lot : supplies[item]) {
        if (quantity <= 1e-9) break;
        const double used = std::min(quantity, lot.quantity);
        lot.quantity -= used;
        quantity -= used;
      }
    };
    auto historical = historical_tasks_;
    std::sort(historical.begin(), historical.end(), [](const auto& left, const auto& right) { return left.start < right.start; });
    for (const auto& task : historical) {
      const auto flow_iter = flows_.find(task.operation);
      if (flow_iter == flows_.end()) continue;
      const double available_at = task.status == "running" ? task.end : 0;
      for (const auto& flow : flow_iter->second) {
        if (flow.quantity < 0) consume(flow.item, -flow.quantity * task.quantity);
        if (flow.quantity > 0) supplies[flow.item].push_back({available_at, flow.quantity * task.quantity, false});
      }
    }
    for (auto& [item, lots] : supplies)
      std::sort(lots.begin(), lots.end(), [](const auto& left, const auto& right) { return left.available < right.available; });
    std::unordered_map<std::string, std::vector<double>> available; Result result;
    for (const auto& [resource, slots] : capacity_) available[resource] = std::vector<double>(slots, 0);
    // Freeze confirmed or started tasks by reserving a resource slot until their end.
    for (const auto& task : frozen_tasks_) {
      auto& slots = available[task.resource];
      if (slots.empty()) slots.push_back(0);
      // The current finite-capacity representation stores next availability;
      // reserve a future outage only when it intersects the slot timeline.
      auto slot = std::min_element(slots.begin(), slots.end());
      *slot = std::max(*slot, task.end);
    }
    for (const auto& [operation, entries] : alternatives_)
      for (const auto& [required, replacement] : entries) available.try_emplace(replacement, 1, 0);
    for (int index : chromosome) {
      const auto& demand = demands_[index]; const auto route = route_for(demand);
      if (active_demands && !active_demands->contains(demand.id)) continue;
      if (route.empty()) { result.diagnostics.push_back(demand.id + ": route not found"); result.problems.push_back({"route_not_found", "error", demand.id, "", demand.item, "configure a manufacturing route"}); result.fitness += 1e9; continue; }
      const double initial_release = backward && demand.due > 0 ? std::max(0.0, demand.due - route_duration(demand, route)) : 0;
      std::vector<double> predecessor_batch_ends;
      double demand_completion = initial_release;
      for (const auto& operation_id : route) {
        const auto& operation = operations_.at(operation_id); const double total_quantity = production_quantity(operation, demand.quantity); bool scheduling_error = false;
        const auto flow_iter = flows_.find(operation_id);
        const auto resource_iter = resources_.find(operation_id);
        if (resource_iter == resources_.end()) {
          result.diagnostics.push_back(demand.id + ": incomplete route data for " + operation_id);
          result.problems.push_back({"incomplete_route_data", "error", demand.id, operation_id, operation_id, "assign at least one resource"});
          result.fitness += 1e8;
          break;
        }
        size_t batch_index = 0;
        std::vector<double> current_batch_ends;
        for (double quantity : split_batches(operation, total_quantity)) {
          const auto historical = historical_batch_count_.find({demand.id, operation_id});
          const size_t historical_count = historical == historical_batch_count_.end() ? 0 : historical->second;
          const size_t current_batch = batch_index++;
          if (current_batch < historical_count) continue;
          double material_ready = 0;
          bool material_shortage = false;
          static const std::vector<Flow> no_flows;
          const auto& operation_flows = flow_iter == flows_.end() ? no_flows : flow_iter->second;
          // Negative flow quantities consume material and positive quantities produce it.
          for (const auto& flow : operation_flows) if (flow.quantity < 0) {
            const double required = -flow.quantity * quantity;
            double remaining = required;
            double supply_ready = 0;
            for (auto& lot : supplies[flow.item]) {
              if (remaining <= 1e-9) break;
              const double used = std::min(remaining, lot.quantity);
              lot.quantity -= used;
              remaining -= used;
              if (used > 0) { supply_ready = std::max(supply_ready, lot.available); material_shortage = material_shortage || lot.shortage; }
            }
            material_ready = std::max(material_ready, supply_ready);
            if (remaining > 1e-9) {
              const double shortage_quantity = remaining;
              const auto supplier = purchasable_.find(flow.item);
              if (supplier == purchasable_.end()) {
                // Keep a constrained planning task instead of dropping the
                // order. Its output carries the shortage to later operations.
                result.diagnostics.push_back(demand.id + ": material shortage " + flow.item + ", planned with shortage");
                result.problems.push_back({"material_shortage", "warning", demand.id, operation_id, flow.item, "create purchase or manufacturing supply", shortage_quantity, 0});
                material_shortage = true;
              } else {
                const auto rule = supplier->second;
                double purchase_quantity = std::max(shortage_quantity, rule.minimum);
                if (rule.multiple > 0) purchase_quantity = std::ceil(purchase_quantity / rule.multiple) * rule.multiple;
                // The purchase receipt is needed before the consuming operation
                // and is backward-planned from the demand due date when known.
                const double required_receipt = demand.due > 0 ? std::max(0.0, demand.due - operation.seconds * quantity) : rule.leadtime;
                const double recommended_order = required_receipt - rule.leadtime;
                const bool late = recommended_order < 0;
                if (late) {
                  result.diagnostics.push_back(demand.id + ": purchase lead-time risk for " + flow.item + ", late by " + std::to_string(-recommended_order) + " seconds");
                  result.problems.push_back({"purchase_lead_time_risk", "warning", demand.id, operation_id, flow.item, "expedite purchase or revise due date", shortage_quantity, -recommended_order});
                }
                result.purchases.push_back({demand.id, flow.item, purchase_quantity, std::max(0.0, recommended_order), required_receipt, rule.leadtime, late});
                supplies[flow.item].push_back({rule.leadtime, purchase_quantity, false});
                // The purchase lot is consumed by the current requirement.
                supplies[flow.item].back().quantity -= shortage_quantity;
                material_ready = std::max(material_ready, rule.leadtime);
              }
            }
          }
          double aligned_start = 0, scheduled_end = 0;
          std::vector<std::string> selected_resources;
          // A downstream batch can start as soon as its corresponding upstream
          // batch is available. This creates true flow-line overlap instead of
          // waiting for every batch of the preceding operation to finish.
          const double predecessor_ready = current_batch < predecessor_batch_ends.size()
              ? predecessor_batch_ends[current_batch] : initial_release;
          if (!select_resources(operation_id, std::max(predecessor_ready, material_ready), operation.seconds * quantity, available, aligned_start, scheduled_end, selected_resources)) {
            result.diagnostics.push_back(demand.id + ": no feasible resource candidate for " + operation_id);
            result.problems.push_back({"resource_unavailable", "error", demand.id, operation_id, "resource combination", "release capacity, change calendar, or use an alternate resource"});
            scheduling_error = true;
            break;
          }
          for (const auto& resource : selected_resources) {
            auto& slots = available[resource];
            *std::min_element(slots.begin(), slots.end()) = scheduled_end;
          }
          for (const auto& flow : operation_flows)
            if (flow.quantity > 0) supplies[flow.item].push_back({scheduled_end, flow.quantity * quantity, material_shortage});
          result.tasks.push_back({demand.id, operation_id, material_shortage ? "material_shortage" : "planned", selected_resources, quantity, aligned_start, scheduled_end});
          current_batch_ends.push_back(scheduled_end);
          demand_completion = std::max(demand_completion, scheduled_end);
        }
        predecessor_batch_ends = std::move(current_batch_ends);
        if (scheduling_error) { result.fitness += 1e8; break; }
      }
      if (demand.due > 0 && demand_completion > demand.due) {
        result.diagnostics.push_back(demand.id + ": due-date risk, late by " + std::to_string(demand_completion - demand.due) + " seconds");
        result.problems.push_back({"due_date_risk", "warning", demand.id, "", demand.item, "expedite supply or revise the delivery commitment", 0, demand_completion - demand.due});
      }
      result.fitness += demand_completion;
    }
    return result;
  }

  static std::vector<int> crossover(const std::vector<int>& left, const std::vector<int>& right, std::mt19937& rng) {
    const size_t cut = left.empty() ? 0 : rng() % left.size(); std::vector<int> child(left.begin(), left.begin() + cut);
    for (int item : right) if (std::find(child.begin(), child.end(), item) == child.end()) child.push_back(item);
    if (child.size() > 1 && rng() % 100 < 20) std::swap(child[rng() % child.size()], child[rng() % child.size()]);
    return child;
  }
};

int main(int argc, char* argv[]) {
  if (argc < 3 || argc > 7) { std::cerr << "usage: chaozhenyun_scheduler <case_dir> <result.csv> [mrp|mrp_device|backward|ga|ga_device|reschedule|reschedule_device] [previous.csv events.csv now_seconds]\n"; return 2; }
  try {
    const std::string mode = argc >= 4 ? argv[3] : "ga";
    if (mode != "mrp" && mode != "mrp_device" && mode != "backward" && mode != "ga" && mode != "ga_device" && mode != "reschedule" && mode != "reschedule_device") throw std::runtime_error("unknown mode");
    if ((mode == "reschedule" || mode == "reschedule_device") && argc != 7) throw std::runtime_error("reschedule requires previous.csv, events.csv and now_seconds");
    const bool enable_device_state = mode == "mrp_device" || mode == "ga_device" || mode == "reschedule_device";
    FlowLineScheduler scheduler(argv[1], enable_device_state);
    const Result result = (mode == "ga" || mode == "ga_device") ? scheduler.run_ga() : (mode == "reschedule" || mode == "reschedule_device") ? scheduler.run_reschedule(argv[4], argv[5], std::stod(argv[6])) : scheduler.run_mrp(mode == "backward"); std::ofstream output(argv[2]);
    output << "demand,operation,resources,quantity,start_seconds,end_seconds,status\n";
    for (const auto& task : result.tasks) { std::ostringstream resources; for (size_t i = 0; i < task.resources.size(); ++i) { if (i) resources << '+'; resources << task.resources[i]; } output << task.demand << ',' << task.operation << ',' << resources.str() << ',' << task.quantity << ',' << task.start << ',' << task.end << ',' << task.status << '\n'; }
    std::ofstream purchase_output(std::string(argv[2]) + ".purchases.csv"); purchase_output << "demand,item,quantity,recommended_order_seconds,required_receipt_seconds,feasible_receipt_seconds,lead_time_risk\n";
    std::map<std::pair<std::string, std::string>, PurchaseOrder> consolidated;
    for (const auto& purchase : result.purchases) {
      auto key = std::make_pair(purchase.demand, purchase.item);
      auto [item, inserted] = consolidated.emplace(key, purchase);
      if (!inserted) { item->second.quantity += purchase.quantity; item->second.recommended_order = std::min(item->second.recommended_order, purchase.recommended_order); item->second.required_receipt = std::min(item->second.required_receipt, purchase.required_receipt); item->second.feasible_receipt = std::max(item->second.feasible_receipt, purchase.feasible_receipt); item->second.late = item->second.late || purchase.late; }
    }
    for (const auto& [key, purchase] : consolidated) purchase_output << purchase.demand << ',' << purchase.item << ',' << purchase.quantity << ',' << purchase.recommended_order << ',' << purchase.required_receipt << ',' << purchase.feasible_receipt << ',' << (purchase.late ? "risk" : "ok") << '\n';
    std::ofstream problems_output(std::string(argv[2]) + ".problems.csv");
    problems_output << "type,severity,demand,operation,object,quantity,risk_seconds,recommended_action\n";
    for (const auto& problem : result.problems)
      problems_output << problem.type << ',' << problem.severity << ',' << problem.demand << ',' << problem.operation << ',' << problem.object << ',' << problem.quantity << ',' << problem.risk_seconds << ',' << problem.action << '\n';
    if (mode == "reschedule" || mode == "reschedule_device") {
      const auto previous = FlowLineScheduler::read_schedule(argv[4]);
      std::map<std::pair<std::string, std::string>, std::vector<Scheduled>> old_index;
      std::map<std::pair<std::string, std::string>, std::vector<Scheduled>> new_index;
      for (const auto& task : previous) old_index[{task.demand, task.operation}].push_back(task);
      for (const auto& task : result.tasks) new_index[{task.demand, task.operation}].push_back(task);
      std::ofstream diff_output(std::string(argv[2]) + ".diff.csv");
      diff_output << "demand,operation,batch,old_start,new_start,old_end,new_end,status\n";
      std::map<std::pair<std::string, std::string>, size_t> batch_number;
      for (const auto& task : result.tasks) {
        auto key = std::make_pair(task.demand, task.operation);
        auto& candidates = old_index[key];
        auto old = std::find_if(candidates.begin(), candidates.end(), [&](const auto& item) { return item.quantity == task.quantity && item.start == task.start && item.end == task.end; });
        const bool preserved = old != candidates.end();
        const size_t batch = ++batch_number[key];
        diff_output << task.demand << ',' << task.operation << ',' << batch << ',' << (preserved ? old->start : -1) << ',' << task.start << ',' << (preserved ? old->end : -1) << ',' << task.end << ',' << (preserved ? "preserved" : "changed") << '\n';
      }
    }
    std::cout << "mode=" << mode << " scheduled_tasks=" << result.tasks.size() << " planned_purchases=" << result.purchases.size() << " diagnostics=" << result.diagnostics.size() << " fitness=" << result.fitness << '\n';
    for (const auto& diagnostic : result.diagnostics) std::cout << diagnostic << '\n';
  } catch (const std::exception& error) { std::cerr << "error: " << error.what() << '\n'; return 1; }
}

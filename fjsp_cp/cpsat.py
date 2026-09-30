"""CP-SAT solver for a flexible job shop.

Jointly chooses a machine for each operation and a start time, then
minimizes makespan. Operation order inside a job is fixed. Machines
cannot overlap. ``solve_fjsp`` is the entry used by the ccAPPS planning
stage; ``read_data_final`` remains for the standalone text format.
"""

import argparse
import re
import time
from pathlib import Path

from ortools.sat.python import cp_model

DATA_FINAL = Path(__file__).with_name("data_final.txt")


class ProgressPrinter(cp_model.CpSolverSolutionCallback):
    def __init__(self):
        super().__init__()
        self.count = 0

    def on_solution_callback(self):
        self.count += 1
        objective = self.ObjectiveValue()
        bound = self.BestObjectiveBound()
        gap = 0.0 if objective == 0 else (objective - bound) / objective * 100
        print(
            f"incumbent#{self.count} time={self.WallTime():.2f}s "
            f"makespan={objective:.0f} lower_bound={bound:.0f} gap={gap:.2f}%",
            flush=True,
        )


def read_data_final(path):
    """Read the legacy 0-based FJSP format used by data_final.txt."""
    lines = Path(path).read_text().splitlines()
    jobs_count, machines, _ = map(int, lines[0].split())
    jobs_data = []
    for line in lines[1 : jobs_count + 1]:
        values = list(map(int, re.findall(r"\d+", line)))
        position, job = 1, []
        while position < len(values):
            count = values[position]
            job.append(
                [
                    {
                        "machine": values[position + 1 + 2 * index],
                        "duration": values[position + 2 + 2 * index],
                    }
                    for index in range(count)
                ]
            )
            position += 1 + 2 * count
        jobs_data.append(job)
    return jobs_data, machines


def solve_fjsp(
    jobs_data,
    num_machines,
    time_limit_seconds=600,
    workers=8,
    horizon=None,
    occupancy=None,
    log_progress=True,
):
    """Minimize makespan. Returns status name, makespan, bound, and assignments.

    ``occupancy`` is a list of ``(machine, start, end)`` intervals already
    taken by plans this solver must not move. Times are seconds from the
    same origin as the decision variables.
    """
    num_jobs = len(jobs_data)
    occupancy = occupancy or []
    if horizon is None:
        horizon = sum(
            max(candidate["duration"] for candidate in op)
            for job in jobs_data
            for op in job
        ) or 1
    if occupancy:
        horizon = max(horizon, max(end for _, _, end in occupancy))

    model = cp_model.CpModel()
    tasks = {}
    choices = {}
    machine_intervals = {m: [] for m in range(num_machines)}

    for j, job in enumerate(jobs_data):
        for o, op in enumerate(job):
            start = model.NewIntVar(0, horizon, f"s_{j}_{o}")
            end = model.NewIntVar(0, horizon, f"e_{j}_{o}")
            duration = model.NewIntVar(1, horizon, f"d_{j}_{o}")
            model.NewIntervalVar(start, duration, end, f"i_{j}_{o}")
            tasks[j, o] = (start, end, duration)

            presences = []
            for candidate in op:
                presence = model.NewBoolVar(f"x_{j}_{o}_{candidate['machine']}")
                optional = model.NewOptionalIntervalVar(
                    start,
                    candidate["duration"],
                    end,
                    presence,
                    f"oi_{j}_{o}_{candidate['machine']}",
                )
                presences.append(presence)
                machine_intervals[candidate["machine"]].append(optional)
            model.AddExactlyOne(presences)
            model.Add(
                duration
                == sum(presence * candidate["duration"] for presence, candidate in zip(presences, op))
            )
            choices[j, o] = list(zip(presences, op))

        for o in range(len(job) - 1):
            model.Add(tasks[j, o + 1][0] >= tasks[j, o][1])

    for index, (machine, start, end) in enumerate(occupancy):
        if machine not in machine_intervals or end <= start:
            continue
        machine_intervals[machine].append(
            model.NewIntervalVar(start, end - start, end, f"fixed_{index}")
        )

    for machine in range(num_machines):
        model.AddNoOverlap(machine_intervals[machine])

    makespan = model.NewIntVar(0, horizon, "makespan")
    model.AddMaxEquality(
        makespan, [tasks[j, len(jobs_data[j]) - 1][1] for j in range(num_jobs)]
    )
    model.Minimize(makespan)

    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = workers
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.log_search_progress = log_progress

    started = time.time()
    status = (
        solver.Solve(model, ProgressPrinter())
        if log_progress
        else solver.Solve(model)
    )
    elapsed = time.time() - started
    result = {
        "status": solver.StatusName(status),
        "elapsed": elapsed,
        "assignments": [],
    }
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        result["makespan"] = solver.ObjectiveValue()
        result["lower_bound"] = solver.BestObjectiveBound()
        for (j, o), (start, end, _duration) in tasks.items():
            machine = duration = None
            for presence, candidate in choices[j, o]:
                if solver.Value(presence):
                    machine = candidate["machine"]
                    duration = candidate["duration"]
                    break
            result["assignments"].append(
                {
                    "job": j,
                    "operation": o,
                    "machine": machine,
                    "start": solver.Value(start),
                    "end": solver.Value(end),
                    "duration": duration,
                }
            )
    return result


def main():
    parser = argparse.ArgumentParser(description="Solve data_final.txt with CP-SAT.")
    parser.add_argument("--input", type=Path, default=DATA_FINAL)
    parser.add_argument("--time-limit", type=int, default=600)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    jobs, machines = read_data_final(args.input)
    print(
        f"读取 {args.input.name}: {len(jobs)} jobs, "
        f"{sum(len(job) for job in jobs)} operations, {machines} machines"
    )
    result = solve_fjsp(
        jobs,
        machines,
        time_limit_seconds=args.time_limit,
        workers=args.workers,
    )
    print(f"状态: {result['status']}  耗时 {result['elapsed']:.1f}s")
    if "makespan" in result:
        print(f"makespan = {result['makespan']:.0f}  下界 = {result['lower_bound']:.0f}")


if __name__ == "__main__":
    main()

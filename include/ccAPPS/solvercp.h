#pragma once
#ifndef SOLVERCP_H
#define SOLVERCP_H

#include "ccAPPS/model.h"

namespace ccAPPS {

/* CP-SAT scheduling of MRP manufacturing lots. Alternatives select complete
 * resource combinations. Processing accumulates across open shifts, with
 * simultaneous capacity constraints for equipment, tooling and personnel,
 * including time-varying headcount. Solved wall-clock dates are applied directly.
 */
PyObject* run_cp(PyObject*, PyObject*);
PyObject* cp_last_run_applied(PyObject*, PyObject*);
/* True when the latest local-reschedule CP run exported a schedule that
 * cleared the trigger (order lateness / material shortage / capacity overload). */
PyObject* cp_last_local_ok(PyObject*, PyObject*);
PyObject* cp_last_run_stage(PyObject*, PyObject*);
/* Dict: level_used, local_ok, applied, incomplete, named_late, window_late,
 * stage, mode, unlock_in_window — for API / task observability. */
PyObject* cp_last_local_outcome(PyObject*, PyObject*);
/* Human-readable seed conflict lines from the latest model preflight. */
PyObject* cp_last_preflight_conflicts(PyObject*, PyObject*);
PyObject* cp_mark_candidate_no_supply(PyObject*, PyObject*);
PyObject* cp_local_snapshot(PyObject*, PyObject*);
PyObject* cp_local_restore(PyObject*, PyObject*);
PyObject* cp_local_snapshot_clear(PyObject*, PyObject*);
PyObject* run_cp_partitioned(PyObject*, PyObject*);
/* ALNS (adaptive large neighborhood) evolves lot-split structure; each move
 * is evaluated by a short-horizon CP-SAT LNS worker on the affected neighborhood.
 */
PyObject* run_cp_alns(PyObject*, PyObject*);
/* Drop unused XOR head/tail trees after partitioned/single CP. */
void cpFinalizeXorSplits();
/* Last run_cp_partitioned movable assignments touched (for ALNS no-op detect). */
size_t cpLastPartitionOptimized();
void cpSetLastPartitionOptimized(size_t n);
/* Number of operation assignments actually exported by the last run_cp. */
size_t cpLastRunApplied();
void cpSetLastRunApplied(size_t n);

}  // namespace ccAPPS
#endif

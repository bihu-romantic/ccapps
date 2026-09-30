#pragma once
#ifndef SOLVERCP_H
#define SOLVERCP_H

#include "ccAPPS/model.h"

namespace ccAPPS {

/* CP-SAT flexible job shop. Machine choice and sequence are decided in
 * working time. Dates are then placed with Operation::calculateOperationTime
 * so operation, location and resource calendars determine the wall-clock
 * start and end.
 */
PyObject* run_cp(PyObject*, PyObject*);

}  // namespace ccAPPS
#endif

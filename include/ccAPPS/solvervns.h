/***************************************************************************
 *                                                                         *
 * Copyright (C) 2007-2026 by ccAPPS bv                                   *
 *                                                                         *
 ***************************************************************************/

#pragma once
#ifndef SOLVERVNS_H
#define SOLVERVNS_H

#include "ccAPPS/solver.h"

namespace ccAPPS {

class SolverVNS : public SolverCreate {
 public:
  using SolverCreate::solve;

  SolverVNS() { initType(metadata); }
  ~SolverVNS() override {}

  static int initialize();
  static PyObject* create(PyTypeObject*, PyObject*, PyObject*);
  const MetaClass& getType() const override { return *metadata; }
  static const MetaClass* metadata;

  void solve(void* v = nullptr) override { SolverCreate::solve(v); }
};

}  // namespace ccAPPS

#endif  // SOLVERVNS_H

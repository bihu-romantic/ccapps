"""Regression test for FAS forward allocation on a bucketized resource.

The XML command runner currently has an unrelated embedded-Python startup
failure.  Run the same engine model through the Python extension instead and
assert the scheduled load dates directly.
"""

from pathlib import Path

import ccAPPS


ccAPPS.readXMLfile(str(Path(__file__).with_name("fas_bucket.xmltest")), False, False)
solver = ccAPPS.solverFAS(plantype=1, constraints=20, loglevel=0)
solver.solve()
solver.commit()

bucket_loads = sorted(
    (load.resource.name, str(load.date), -load.quantity)
    for operationplan in ccAPPS.operationplans()
    for load in operationplan.loadplans
    if load.resource.name.endswith("bucket") or load.resource.name.startswith("daily bucket")
)

# Both orders need the entire capacity of a daily bucket.  MRP creates them
# for the due date; FAS must forward-place the first at current and advance
# the second to the next actual bucket after the trial overload.
assert bucket_loads == [
    ("combined bucket", "2020-01-02 00:00:00", 10.0),
    ("combined bucket", "2020-01-03 00:00:00", 10.0),
    ("daily bucket", "2020-01-01 00:00:00", 10.0),
    ("daily bucket", "2020-01-02 00:00:00", 10.0),
    ("daily bucket 2", "2020-01-01 00:00:00", 10.0),
    ("daily bucket 2", "2020-01-02 00:00:00", 10.0),
    ("offset bucket", "2020-01-03 00:00:00", 10.0),
    ("offset bucket", "2020-01-05 00:00:00", 10.0),
    ("percentage bucket", "2020-01-02 00:00:00", 10.0),
    ("percentage bucket", "2020-01-04 00:00:00", 10.0),
    ("start bucket", "2020-01-02 00:00:00", 10.0),
    ("start bucket", "2020-01-04 00:00:00", 10.0),
    ("time per bucket", "2020-01-01 00:00:00", 8.0),
    ("time per bucket", "2020-01-09 00:00:00", 8.0),
    ("time per bucket", "2020-01-17 00:00:00", 8.0),
], bucket_loads

# The same two plans also share a unit-capacity continuous machine.  The
# bucket choice is accepted only when its full operation interval is free on
# that resource too.
machine_loads = sorted(
    (str(load.operationplan.start), str(load.operationplan.end), abs(load.quantity))
    for operationplan in ccAPPS.operationplans()
    for load in operationplan.loadplans
    if load.resource.name == "combined machine" and load.quantity < 0
)
assert machine_loads == [
    ("2020-01-02 00:00:00", "2020-01-03 00:00:00", 1.0),
    ("2020-01-03 00:00:00", "2020-01-04 00:00:00", 1.0),
], machine_loads

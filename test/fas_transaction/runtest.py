"""FAS rollback removes all supply created by its shared MRP expansion."""
from pathlib import Path
import ccAPPS

ccAPPS.readXMLfile(str(Path(__file__).with_name("transaction.xmltest")), False, False)
solver = ccAPPS.solverFAS(plantype=1, constraints=20, loglevel=0)
solver.solve()
assert list(ccAPPS.operationplans())
solver.rollback()
plans = list(ccAPPS.operationplans())
assert len(plans) == 1 and plans[0].reference == "legacy-proposed", plans
assert str(plans[0].start) == "2020-01-03 00:00:00", plans[0].start

solver = ccAPPS.solverFAS(plantype=1, constraints=20, loglevel=0)
solver.solve()
solver.commit()
plans = {op.operation.name: op for op in ccAPPS.operationplans()}
assert plans["make component"].end <= plans["assemble"].start
assert plans["legacy make"].reference == "legacy-proposed"
assert str(plans["legacy make"].start) == "2020-01-03 00:00:00"

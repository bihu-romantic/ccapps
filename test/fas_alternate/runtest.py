"""FAS selects the earliest feasible alternate, rather than MRP's cheapest."""
from pathlib import Path
import ccAPPS

ccAPPS.readXMLfile(str(Path(__file__).with_name("operation_alternate.xmltest")), False, False)
solver = ccAPPS.solverFAS(plantype=1, constraints=20, loglevel=0)
solver.solve()
solver.commit()
names = [op.operation.name for op in ccAPPS.operationplans()]
assert "make B" in names, names
assert "make A" not in names, names
assert not any(name == "buy from supplier D" for name in names)

"""FAS demand tie-breaker is deterministic regardless of input order."""
from pathlib import Path
import ccAPPS

model = str(Path(__file__).with_name("determinism.xmltest"))

def run():
    solver = ccAPPS.solverFAS(plantype=1, constraints=20, loglevel=0)
    solver.solve()
    result = sorted(
        (op.demand.name if op.demand else "", op.operation.name,
         op.quantity, str(op.start), str(op.end))
        for op in ccAPPS.operationplans()
    )
    solver.rollback()
    return result

ccAPPS.readXMLfile(model, False, False)
first = run()
second = run()
assert first == second, (first, second)

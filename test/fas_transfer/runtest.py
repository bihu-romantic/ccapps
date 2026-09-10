"""FAS releases a downstream operation on the first transfer batch."""
from pathlib import Path

import ccAPPS

ccAPPS.readXMLfile(str(Path(__file__).with_name("transfer.xmltest")), False, False)
solver = ccAPPS.solverFAS(plantype=1, constraints=20, loglevel=0)
solver.solve()
solver.commit()
plans = list(ccAPPS.operationplans())
producer = next(plan for plan in plans if plan.operation.name == "make component")
consumers = sorted((plan for plan in plans if plan.operation.name == "assemble"), key=lambda plan: plan.start)

# The 10-piece order is produced in two transfer batches of 5. The first batch
# is available halfway through the two-day producer, so the consumer starts
# before the producer has fully completed.
positive_batches = sorted(
    flow.date for flow in producer.flowplans
    if flow.quantity > 0 and flow.buffer.name == "component"
)
assert len(positive_batches) == 2, positive_batches
assert len(consumers) == 2, consumers
assert consumers[0].quantity == 5.0 and consumers[1].quantity == 5.0
assert consumers[0].start == positive_batches[0], (consumers[0].start, positive_batches)
assert consumers[0].start < producer.end, (consumers[0].start, producer.end)

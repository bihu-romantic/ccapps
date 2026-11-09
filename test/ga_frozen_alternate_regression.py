"""Regression: GA must preserve frozen alternate operation/resource assignments.

Run with venv/bin/python test/ga_frozen_alternate_regression.py.
"""

import os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ccAPPS

ccAPPS.readXMLdata(
    """<plan xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><current>2026-10-16T08:00:00</current><calendars><calendar name="outage" default="1"><buckets><bucket start="2026-10-17T08:00:00" end="2026-10-18T08:00:00" value="0"/></buckets></calendar></calendars><resources><resource name="H02" maximum="1"/><resource name="H03" maximum="1"><maximum_calendar name="outage"/></resource><resource name="PERSON" maximum="1"/></resources><operations>
<operation name="A_H02" xsi:type="operation_fixed_time" duration="PT1H"><loads><load><resource name="H02"/></load><load><resource name="PERSON"/></load></loads></operation>
<operation name="B_H03" xsi:type="operation_fixed_time" duration="PT1H"><loads><load><resource name="H03"/></load><load><resource name="PERSON"/></load></loads></operation>
<operation name="ALT" xsi:type="operation_alternate"><suboperations><suboperation priority="1"><operation name="A_H02"/></suboperation><suboperation priority="2"><operation name="B_H03"/></suboperation></suboperations></operation>
<operation name="work" xsi:type="operation_fixed_time" duration="PT2H"><loads><load><resource name="H03"/></load><load><resource name="PERSON"/></load></loads></operation>
</operations></plan>""",
    False,
    False,
)
p = ccAPPS.operationplan(
    reference="frozen_parent",
    operation=ccAPPS.operation(name="ALT"),
    quantity=1,
    start="2026-10-17T09:00:00",
    status="confirmed",
)
for child in ccAPPS.operationplans():
    if child.owner == p:
        child.status = "confirmed"
ccAPPS.operationplan(
    reference="move1",
    operation=ccAPPS.operation(name="work"),
    quantity=1,
    start="2026-10-17T09:00:00",
)
ccAPPS.operationplan(
    reference="move2",
    operation=ccAPPS.operation(name="work"),
    quantity=1,
    start="2026-10-17T09:00:00",
)


def snapshot():
    return {
        str(o.reference): (
            o.operation.name,
            str(o.start),
            str(o.end),
            o.quantity,
            tuple(sorted(l.resource.name for l in o.loadplans)),
        )
        for o in ccAPPS.operationplans()
        if o.status == "confirmed"
    }


before = snapshot()
print("BEFORE", before, flush=True)
os.environ.update(
    GA_POPULATION="10", GA_GENERATIONS="1", GA_TIME_LIMIT_SECONDS="2", GA_SEED="123456"
)
ccAPPS.run_ga()
after = snapshot()
print("AFTER", after, flush=True)
assert before == after, (before, after)

print("PASS: frozen operation, resources, dates and quantity preserved")

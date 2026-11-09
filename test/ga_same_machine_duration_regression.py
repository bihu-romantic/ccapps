"""Same machine, different process options must retain their own duration."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ccAPPS

ccAPPS.readXMLdata("""<plan xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<current>2026-10-16T08:00:00</current>
<resources><resource name="H01" maximum="1"/>
<resource name="PERSON" maximum="1"/></resources>
<operations>
<operation name="A_SLOW" xsi:type="operation_time_per" duration_per="PT6M">
<loads><load><resource name="H01"/></load>
<load quantity="1"><resource name="PERSON"/></load></loads></operation>
<operation name="B_FAST" xsi:type="operation_time_per" duration_per="PT1M">
<loads><load><resource name="H01"/></load>
<load quantity="2"><resource name="PERSON"/></load></loads></operation>
<operation name="ALT" xsi:type="operation_alternate"><suboperations>
<suboperation priority="1"><operation name="A_SLOW"/></suboperation>
<suboperation priority="2"><operation name="B_FAST"/></suboperation>
</suboperations></operation></operations></plan>""", False, False)
ccAPPS.operationplan(reference="batch", operation=ccAPPS.operation(name="ALT"),
                     quantity=12, start="2026-10-16T08:00:00")
ccAPPS.operationplan(reference="batch2", operation=ccAPPS.operation(name="ALT"),
                     quantity=12, start="2026-10-16T08:00:00")
before = {p.reference: float(p.quantity) for p in ccAPPS.operationplans()}
os.environ.update(GA_POPULATION="20", GA_GENERATIONS="3",
                  GA_TIME_LIMIT_SECONDS="2", GA_SEED="123456")
ccAPPS.run_ga()
for plan in ccAPPS.operationplans():
    assert abs(plan.quantity - before[plan.reference]) < 1e-6, (
        plan.reference, before[plan.reference], plan.quantity)
    if plan.operation.name == "A_SLOW":
        assert (plan.end - plan.start).total_seconds() >= 12 * 360
print("PASS: same-machine candidate duration and batch quantity preserved")

"""Run one algorithm/mask against a CCAPPS built-in XML example."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import ccAPPS


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    example, algorithm, raw_mask = sys.argv[1:4]
    mask = int(raw_mask)
    source = ROOT / "test" / example / f"{example}.xml"
    xml = re.sub(r"<\?python.*?\?>", "", source.read_text(), flags=re.S)
    ccAPPS.erase(True)
    ccAPPS.readXMLdata(xml, False, False, 0, None, False)

    tokens = [
        name
        for bit, name in ((4, "capa"), (16, "mfg_lt"), (32, "po_lt"))
        if mask & bit
    ]
    os.environ.update(
        {
            "CCAPPS_CONSTRAINT": ",".join(tokens) if tokens else "0",
            "CCAPPS_PLANTYPE": "1",
            "CP_TIME_LIMIT_SECONDS": "5",
            "CP_HORIZON_DAYS": "365",
            "CP_PLANNING_MODE": "all",
            "CP_MATERIAL_MODE": "all",
            "CP_OBJECTIVE_METHOD": "weighted",
            "GA_POPULATION": "12",
            "GA_GENERATIONS": "3",
            "GA_TIME_LIMIT_SECONDS": "3",
            "GA_STAGNATION_LIMIT": "2",
        }
    )
    if algorithm == "fas":
        solver = ccAPPS.solverFAS(constraints=mask, plantype=1, loglevel=0)
        solver.solve()
        solver.commit()
    else:
        ccAPPS.solver_mrp(constraints=mask, plantype=1, loglevel=0).solve()
        if algorithm == "cp":
            ccAPPS.run_cp()
        elif algorithm == "ga":
            ccAPPS.run_ga()

    current = ccAPPS.settings.current
    plans = list(ccAPPS.operationplans())
    purchases = [
        p for p in plans if isinstance(p.operation, ccAPPS.operation_itemsupplier)
    ]
    manufacturing = [
        p
        for p in plans
        if not isinstance(
            p.operation,
            (
                ccAPPS.operation_itemsupplier,
                ccAPPS.operation_itemdistribution,
                ccAPPS.operation_delivery,
            ),
        )
    ]
    demands = list(ccAPPS.demands())
    result = {
        "example": example,
        "algorithm": algorithm,
        "mask": mask,
        "plans": len(plans),
        "planned": sum(float(d.planned_quantity) for d in demands),
        "unplanned": sum(
            max(0.0, float(d.quantity) - float(d.planned_quantity)) for d in demands
        ),
        "past_mfg": sum(p.start < current for p in manufacturing),
        "past_po": sum(p.start < current for p in purchases),
        "purchase_plans": len(purchases),
        "capacity_problems": sum(
            getattr(problem, "entity", "") == "capacity"
            for problem in ccAPPS.problems()
        ),
    }
    print("RESULT=" + json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

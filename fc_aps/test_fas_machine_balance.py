"""Shared staff workload must not hide an idle eligible machine in FAS."""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ccAPPS


class MachineBalanceTests(unittest.TestCase):
    def schedule(self, busy):
        ccAPPS.erase(True)
        start = datetime(2026, 10, 16, 8)
        ccAPPS.settings.current = start
        staff = ccAPPS.resource(name='staff', category='人员', maximum=42)
        machines = {n:ccAPPS.resource(name=n, category='设备', maximum=1)
                    for n in ('m1', 'm2')}
        for i in range(6):
            op = ccAPPS.operation_fixed_time(name=f'other{i}', duration=20*3600)
            ccAPPS.load(operation=op, resource=staff, quantity=1)
            plan = ccAPPS.operationplan(operation=op, quantity=1, start=start)
            plan.status = 'confirmed'
        op = ccAPPS.operation_fixed_time(name='busy', duration=12*3600)
        ccAPPS.load(operation=op, resource=staff, quantity=1)
        ccAPPS.load(operation=op, resource=machines[busy], quantity=1)
        plan = ccAPPS.operationplan(operation=op, quantity=1, start=start)
        plan.status = 'confirmed'
        # Different counts of definitions do not imply actual reservations.
        for i in range(12):
            op = ccAPPS.operation_fixed_time(name=f'eligibility{i}', duration=3600)
            ccAPPS.load(operation=op, resource=machines['m1'], quantity=1)
        location = ccAPPS.location(name='factory')
        item = ccAPPS.item(name='item')
        root = ccAPPS.operation_alternate(name='FC_UNIN_TEST', location=location,
                                          item=item, search='PRIORITY')
        for i,n in enumerate(('m1','m2'),1):
            op = ccAPPS.operation_fixed_time(name=f'make_{n}', duration=3600,
                                             location=location)
            ccAPPS.suboperation(owner=root, operation=op, priority=i)
            ccAPPS.load(operation=op, resource=staff, quantity=1)
            ccAPPS.load(operation=op, resource=machines[n], quantity=1)
            ccAPPS.flow(operation=op, item=item, location=location,
                        quantity=1, type='flow_end')
        ccAPPS.demand(name='order', item=item, location=location, quantity=1,
                      status='open', due=start+timedelta(days=3))
        solver = ccAPPS.solverFAS(constraints=52, plantype=1, loglevel=0)
        solver.solve()
        solver.commit()
        result = next(p for p in ccAPPS.operationplans()
                      if p.operation.name.startswith('make_'))
        return result.operation.name, result.start, start

    def test_idle_primary_is_not_hidden_by_common_staff_queue(self):
        name,begin,start = self.schedule('m2')
        self.assertEqual(name, 'make_m1')
        self.assertEqual(begin, start)

    def test_busy_primary_does_not_block_idle_backup(self):
        name,begin,start = self.schedule('m1')
        self.assertEqual(name, 'make_m2')
        self.assertEqual(begin, start)


if __name__ == '__main__':
    unittest.main()

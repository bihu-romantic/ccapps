"""Infinite assembly lines must not serialize finite shared staff capacity."""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ccAPPS


class FasInfiniteResourceTests(unittest.TestCase):
    def setUp(self):
        ccAPPS.erase(True)
        self.origin = datetime(2026, 9, 10, 8)
        ccAPPS.settings.current = self.origin

    def test_infinite_resource_stays_unconstrained(self):
        line = ccAPPS.resource_infinite(name='assembly', constrained=False)
        self.assertFalse(line.constrained)
        line.constrained = True
        self.assertFalse(line.constrained)
        finite = ccAPPS.resource(name='machine', constrained=True)
        self.assertTrue(finite.constrained)
        finite.constrained = False
        self.assertFalse(finite.constrained)
        finite.constrained = True
        self.assertTrue(finite.constrained)

    def schedule(self, staff_capacity, day_shifts=False):
        location = ccAPPS.location(name='factory')
        staff = ccAPPS.resource(name='staff', maximum=staff_capacity)
        if day_shifts:
            calendar = ccAPPS.calendar(name='day_shifts', default=0)
            for day in range(5):
                start = self.origin + timedelta(days=day)
                calendar.setValue(start, start + timedelta(hours=12), 1)
            staff.available = calendar
        line = ccAPPS.resource_infinite(name='assembly', constrained=False)
        for i in range(2):
            item = ccAPPS.item(name=f'item{i}')
            op = ccAPPS.operation_time_per(name=f'make{i}', location=location,
                                           duration_per=7200 if day_shifts else 3600)
            ccAPPS.load(operation=op, resource=staff, quantity=1)
            ccAPPS.load(operation=op, resource=line, quantity=1)
            ccAPPS.flow(operation=op, item=item, location=location,
                        quantity=1, type='flow_end')
            ccAPPS.demand(name=f'order{i}', item=item, location=location,
                          quantity=10, status='open',
                          due=self.origin + timedelta(days=3), priority=i+1)
        solver = ccAPPS.solverFAS(constraints=52, plantype=1, loglevel=0)
        solver.solve()
        solver.commit()
        return sorted((p.start, p.end, p.quantity)
                      for p in ccAPPS.operationplans()
                      if p.operation.name.startswith('make'))

    def test_assembly_jobs_overlap_when_staff_is_available(self):
        plans = self.schedule(76)
        self.assertEqual(plans, [(self.origin,
                                 self.origin + timedelta(hours=10), 10)] * 2)

    def test_finite_staff_still_limits_parallel_work(self):
        plans = self.schedule(1)
        self.assertEqual(len(plans), 2)
        self.assertEqual(plans[0], (self.origin,
                                  self.origin + timedelta(hours=10), 10))
        self.assertEqual(plans[1], (plans[0][1],
                                  self.origin + timedelta(hours=20), 10))

    def test_parallel_long_jobs_pause_outside_day_shift(self):
        plans = self.schedule(76, day_shifts=True)
        self.assertEqual(plans, [(self.origin,
                                 self.origin + timedelta(days=1, hours=8), 10)] * 2)


if __name__ == '__main__':
    unittest.main()

"""Paused manufacturing orders release pooled staff, while machines stay booked."""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ccAPPS


class PersonnelPauseTests(unittest.TestCase):
    def setUp(self):
        ccAPPS.erase(True)
        self.start = datetime(2026, 10, 16, 8)
        ccAPPS.settings.current = self.start
        self.staff = ccAPPS.resource(name='staff', category='人员', maximum=1)
        self.machine = ccAPPS.resource(name='machine', category='设备', maximum=1)
        self.day = ccAPPS.calendar(name='day', default=0)
        self.night = ccAPPS.calendar(name='night', default=0)
        for n in range(5):
            begin = self.start + timedelta(days=n)
            self.day.setValue(begin, begin + timedelta(hours=12), 1)
            self.night.setValue(begin + timedelta(hours=12),
                                begin + timedelta(days=1), 1)
        self.location = ccAPPS.location(name='factory')
        op = ccAPPS.operation_fixed_time(name='locked_day', duration=20*3600,
                                         available=self.day, location=self.location)
        ccAPPS.load(operation=op, resource=self.staff, quantity=1)
        ccAPPS.load(operation=op, resource=self.machine, quantity=1)
        self.locked = ccAPPS.operationplan(operation=op, quantity=1, start=self.start)
        self.locked.status = 'confirmed'

    def schedule(self, calendar, hours, same_machine=False):
        item = ccAPPS.item(name='item')
        op = ccAPPS.operation_fixed_time(name='move', duration=hours*3600,
                                         available=calendar, location=self.location)
        ccAPPS.load(operation=op, resource=self.staff, quantity=1)
        if same_machine:
            ccAPPS.load(operation=op, resource=self.machine, quantity=1)
        ccAPPS.flow(operation=op, item=item, location=self.location,
                    quantity=1, type='flow_end')
        ccAPPS.demand(name='order', item=item, location=self.location, quantity=1,
                      status='open', due=self.start+timedelta(days=3))
        solver = ccAPPS.solverFAS(constraints=52, plantype=1, loglevel=0)
        solver.solve()
        solver.commit()
        return next(p for p in ccAPPS.operationplans() if p.operation.name == 'move')

    def test_other_job_uses_staff_during_fixed_job_night_pause(self):
        plan = self.schedule(self.night, 8)
        self.assertEqual(plan.start, self.start+timedelta(hours=12))
        self.assertEqual(plan.end, self.start+timedelta(hours=20))

    def test_daytime_staff_is_still_reserved(self):
        plan = self.schedule(self.day, 2)
        self.assertGreaterEqual(plan.start, self.locked.end)

    def test_machine_is_still_reserved_through_pause(self):
        plan = self.schedule(self.night, 8, same_machine=True)
        self.assertGreaterEqual(plan.start, self.locked.end)

    def test_resource_report_counts_processing_not_paused_staff(self):
        bucket = list(self.staff.plan([self.start, self.start+timedelta(days=1)]))[0]
        self.assertAlmostEqual(bucket['load'], 12)
        self.assertAlmostEqual(bucket['load_confirmed'], 12)
        self.assertAlmostEqual(bucket['available'], 24)
        self.assertAlmostEqual(bucket['free'], 12)
        machine = list(self.machine.plan([self.start, self.start+timedelta(days=1)]))[0]
        self.assertAlmostEqual(machine['load'], 24)

    def test_movable_day_job_can_pause_around_already_placed_night_job(self):
        staff = ccAPPS.resource(name='other_staff', category='人员', maximum=1)
        for name, calendar, hours, priority in (
            ('night_first', self.night, 8, 1),
            ('day_second', self.day, 20, 2),
        ):
            item = ccAPPS.item(name=name)
            op = ccAPPS.operation_fixed_time(name=name, location=self.location,
                                             available=calendar, duration=hours*3600)
            ccAPPS.load(operation=op, resource=staff, quantity=1)
            ccAPPS.flow(operation=op, item=item, location=self.location,
                        quantity=1, type='flow_end')
            ccAPPS.demand(name=name, item=item, location=self.location, quantity=1,
                          status='open', priority=priority,
                          due=self.start+timedelta(days=3))
        solver = ccAPPS.solverFAS(constraints=52, plantype=1, loglevel=0)
        solver.solve()
        solver.commit()
        plans = {p.operation.name:p for p in ccAPPS.operationplans()}
        self.assertEqual(plans['night_first'].start, self.start+timedelta(hours=12))
        self.assertEqual(plans['day_second'].start, self.start)
        self.assertEqual(plans['day_second'].end, self.start+timedelta(days=1, hours=8))


if __name__ == '__main__':
    unittest.main()

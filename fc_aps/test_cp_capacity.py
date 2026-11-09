"""CP regression checks for scenario 6 simultaneous resource constraints."""
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ccAPPS

os.environ['CP_TIME_LIMIT_SECONDS'] = '5'
os.environ['CP_OBJECTIVE_METHOD'] = 'weighted'
os.environ['CP_KEEP_MRP'] = '0'

class CpCapacityTests(unittest.TestCase):
    def setUp(self):
        ccAPPS.erase(True)
        os.environ["CP_PLANNING_MODE"] = "all"
        os.environ["CP_MATERIAL_MODE"] = "all"
        self.origin = datetime(2026, 9, 10, 8)
        ccAPPS.settings.current = self.origin

    def make(self, name, resources, seconds=3600):
        op = ccAPPS.operation_fixed_time(name=name, duration=seconds)
        for resource, quantity in resources:
            ccAPPS.load(operation=op, resource=resource, quantity=quantity)
        return ccAPPS.operationplan(operation=op, quantity=1,
                                   start=self.origin, end=self.origin+timedelta(seconds=seconds))

    def partial(self):
        os.environ['CP_PLANNING_MODE'] = 'throughput'
        os.environ['CP_HORIZON_DAYS'] = '3'
        ccAPPS.run_cp()

    def test_three_days_selects_three_of_four_lots(self):
        machine = ccAPPS.resource(name='machine',maximum=1)
        for i in range(4): self.make('lot'+str(i),[(machine,1)],86400)
        self.partial()
        plans = [p for p in ccAPPS.operationplans() if p.operation.name.startswith('lot')]
        self.assertEqual(len(plans),3)
        self.assertTrue(all(self.origin <= p.start < p.end <= self.origin+timedelta(days=3) for p in plans))
        self.assertTrue(all(p.quantity == 1 for p in plans))

    def test_partial_respects_locked_capacity(self):
        machine = ccAPPS.resource(name='machine',maximum=1)
        locked = self.make('locked',[(machine,1)],172800)
        locked.status = 'confirmed'
        for i in range(3): self.make('lot'+str(i),[(machine,1)],86400)
        self.partial()
        plans = [p for p in ccAPPS.operationplans() if p.operation.name.startswith('lot')]
        self.assertEqual(len(plans),1)
        self.assertGreaterEqual(plans[0].start,locked.end)
        self.assertEqual(locked.start,self.origin)

    def test_partial_keeps_material_dependencies(self):
        machine = ccAPPS.resource(name='machine',maximum=1)
        location = ccAPPS.location(name='factory')
        item = ccAPPS.item(name='component')
        upstream = ccAPPS.operation_fixed_time(name='up',duration=172800,location=location)
        downstream = ccAPPS.operation_fixed_time(name='down',duration=86400,location=location)
        for op in [upstream,downstream]: ccAPPS.load(operation=op,resource=machine,quantity=1)
        ccAPPS.flow(operation=upstream,item=item,location=location,quantity=1,type='flow_end')
        ccAPPS.flow(operation=downstream,item=item,location=location,quantity=-1,type='flow_start')
        ccAPPS.operationplan(operation=upstream,quantity=1,start=self.origin)
        ccAPPS.operationplan(operation=downstream,quantity=1,start=self.origin)
        self.partial()
        plans = {p.operation.name:p for p in ccAPPS.operationplans()}
        self.assertEqual(set(plans),{'up','down'})
        self.assertLessEqual(plans['up'].end,plans['down'].start)

    def test_partial_cannot_consume_skipped_supply(self):
        machine = ccAPPS.resource(name='machine',maximum=1)
        location = ccAPPS.location(name='factory')
        item = ccAPPS.item(name='component')
        upstream = ccAPPS.operation_fixed_time(name='up',duration=345600,location=location)
        downstream = ccAPPS.operation_fixed_time(name='down',duration=86400,location=location)
        for op in [upstream,downstream]: ccAPPS.load(operation=op,resource=machine,quantity=1)
        ccAPPS.flow(operation=upstream,item=item,location=location,quantity=1,type='flow_end')
        ccAPPS.flow(operation=downstream,item=item,location=location,quantity=-1,type='flow_start')
        ccAPPS.operationplan(operation=upstream,quantity=1,start=self.origin)
        ccAPPS.operationplan(operation=downstream,quantity=1,start=self.origin)
        self.make('other',[(machine,1)],86400)
        self.partial()
        self.assertEqual([p.operation.name for p in ccAPPS.operationplans()],['other'])

    def test_partial_delivery_does_not_claim_missing_lots(self):
        location = ccAPPS.location(name='factory')
        item = ccAPPS.item(name='product')
        machine = ccAPPS.resource(name='machine',maximum=1)
        op = ccAPPS.operation_time_per(name='make',item=item,location=location,
                                       duration_per=86400,size_maximum=1)
        ccAPPS.load(operation=op,resource=machine,quantity=1)
        ccAPPS.flow(operation=op,item=item,location=location,quantity=1,type='flow_end')
        demand = ccAPPS.demand(name='order',item=item,location=location,quantity=4,
                               due=self.origin+timedelta(days=4),status='open')
        ccAPPS.solver_mrp(constraints=0,plantype=1,loglevel=0).solve()
        self.partial()
        production = [p for p in ccAPPS.operationplans() if p.operation.name == 'make']
        deliveries = [p for p in ccAPPS.operationplans() if p.demand is not None]
        self.assertEqual(sum(p.quantity for p in production),3)
        self.assertEqual(sum(p.quantity for p in deliveries),3)
        self.assertEqual(demand.quantity,4)

    def test_partial_includes_unconstrained_material_supplier(self):
        item = ccAPPS.item(name='component')
        location = ccAPPS.location(name='factory')
        missing = ccAPPS.operation_time_per(name='FC_OP_unassigned',duration_per=10,location=location)
        ccAPPS.flow(operation=missing,item=item,location=location,quantity=1,type='flow_end')
        ccAPPS.operationplan(operation=missing,quantity=1,start=self.origin)
        machine = ccAPPS.resource(name='machine',maximum=1)
        consumer = ccAPPS.operation_fixed_time(name='consumer',duration=3600,location=location)
        ccAPPS.load(operation=consumer,resource=machine,quantity=1)
        ccAPPS.flow(operation=consumer,item=item,location=location,quantity=-1,type='flow_start')
        ccAPPS.operationplan(operation=consumer,quantity=1,start=self.origin)
        self.make('other',[(machine,1)],3600)
        self.partial()
        plans = {p.operation.name:p for p in ccAPPS.operationplans()}
        self.assertEqual(set(plans),{'FC_OP_unassigned','consumer','other'})
        self.assertLessEqual(plans['FC_OP_unassigned'].end,plans['consumer'].start)
        self.assertEqual((plans['FC_OP_unassigned'].end-plans['FC_OP_unassigned'].start).total_seconds(),10)

    def test_unconstrained_batches_overlap_but_obey_horizon(self):
        for name,seconds in [('a',200000),('b',200000),('too_long',300000)]:
            op = ccAPPS.operation_fixed_time(name=name,duration=seconds)
            ccAPPS.operationplan(operation=op,quantity=1,start=self.origin)
        self.partial()
        plans = {p.operation.name:p for p in ccAPPS.operationplans()}
        self.assertEqual(set(plans),{'a','b'})
        self.assertLess(max(plans['a'].start,plans['b'].start),min(plans['a'].end,plans['b'].end))
        for p in plans.values():
            self.assertEqual((p.end-p.start).total_seconds(),200000)
            self.assertLessEqual(p.end,self.origin+timedelta(days=3))

    def test_shift_gap_does_not_split_the_lot(self):
        shift = ccAPPS.calendar(name='operation_shift',default=0)
        shift.setValue(self.origin,self.origin+timedelta(hours=1),1)
        shift.setValue(self.origin+timedelta(hours=2),self.origin+timedelta(hours=3),1)
        infinite = ccAPPS.resource_infinite(name='unlimited')
        ignored = ccAPPS.resource(name='ignored',maximum=1,constrained=False)
        for name in ['a','b']:
            op = ccAPPS.operation_fixed_time(name=name,duration=7200,available=shift)
            ccAPPS.load(operation=op,resource=infinite,quantity=1)
            ccAPPS.load(operation=op,resource=ignored,quantity=10)
            ccAPPS.operationplan(operation=op,quantity=1,start=self.origin)
        self.partial()
        plans = list(ccAPPS.operationplans())
        self.assertEqual(len(plans),2)
        for p in plans:
            self.assertEqual(p.start,self.origin)
            self.assertEqual(p.end,self.origin+timedelta(hours=2))
            self.assertEqual((p.end-p.start).total_seconds(),7200)

    def test_selfmade_mode_ignores_purchased_stock_but_requires_selfmade(self):
        os.environ['CP_MATERIAL_MODE'] = 'selfmade'
        loc = ccAPPS.location(name='factory')
        raw = ccAPPS.item(name='raw',category='外购')
        intermediate = ccAPPS.item(name='intermediate',category='自制')
        ccAPPS.buffer(name="raw @ factory",item=raw,location=loc,onhand=0)
        ccAPPS.buffer(name="intermediate @ factory",item=intermediate,location=loc,onhand=0)
        for name,item in [('raw_user',raw),('selfmade_user',intermediate)]:
            op = ccAPPS.operation_fixed_time(name=name,duration=3600,location=loc)
            ccAPPS.flow(operation=op,item=item,location=loc,quantity=-1,type='flow_start')
            ccAPPS.operationplan(operation=op,quantity=1,start=self.origin)
        self.partial()
        self.assertEqual([p.operation.name for p in ccAPPS.operationplans()],['raw_user'])

    def test_selfmade_mode_ignores_infinite_buffer_even_if_category_is_selfmade(self):
        os.environ['CP_MATERIAL_MODE'] = 'selfmade'
        loc = ccAPPS.location(name='factory')
        external = ccAPPS.item(name='skipped',category='自制')
        ccAPPS.buffer_infinite(name="skipped @ factory",item=external,location=loc,onhand=0)
        op = ccAPPS.operation_fixed_time(name='user',duration=3600,location=loc)
        ccAPPS.flow(operation=op,item=external,location=loc,quantity=-1,type='flow_start')
        ccAPPS.operationplan(operation=op,quantity=1,start=self.origin)
        self.partial()
        self.assertEqual([p.operation.name for p in ccAPPS.operationplans()],['user'])

    def test_selfmade_mode_preserves_upstream_timing(self):
        os.environ['CP_MATERIAL_MODE'] = 'selfmade'
        loc = ccAPPS.location(name='factory')
        raw = ccAPPS.item(name='raw',category='产品外购')
        part = ccAPPS.item(name='part',category='自制')
        up = ccAPPS.operation_fixed_time(name='up',duration=3600,location=loc)
        down = ccAPPS.operation_fixed_time(name='down',duration=3600,location=loc)
        ccAPPS.flow(operation=up,item=raw,location=loc,quantity=-1,type='flow_start')
        ccAPPS.flow(operation=up,item=part,location=loc,quantity=1,type='flow_end')
        ccAPPS.flow(operation=down,item=part,location=loc,quantity=-1,type='flow_start')
        for op in [up,down]:ccAPPS.operationplan(operation=op,quantity=1,start=self.origin)
        self.partial()
        plans = {p.operation.name:p for p in ccAPPS.operationplans()}
        self.assertEqual(set(plans),{'up','down'})
        self.assertLessEqual(plans['up'].end,plans['down'].start)

    def test_unconstrained_zero_time_coproduct_stays_instantaneous(self):
        os.environ['CP_MATERIAL_MODE'] = 'selfmade'
        loc = ccAPPS.location(name='factory')
        left = ccAPPS.item(name='left',category='自制')
        right = ccAPPS.item(name='right',category='自制')
        sync = ccAPPS.operation_time_per(name='sync',location=loc)
        ccAPPS.flow(operation=sync,item=left,location=loc,quantity=1,type='flow_end')
        ccAPPS.flow(operation=sync,item=right,location=loc,quantity=1,type='flow_end')
        sync_plan = ccAPPS.operationplan(operation=sync,quantity=720,start=self.origin)
        assembly = ccAPPS.operation_fixed_time(name='assembly',duration=3600,location=loc)
        ccAPPS.flow(operation=assembly,item=left,location=loc,quantity=-1,type='flow_start')
        ccAPPS.operationplan(operation=assembly,quantity=720,start=self.origin)
        self.partial()
        plans = {p.operation.name:p for p in ccAPPS.operationplans()}
        self.assertEqual(set(plans),{'sync','assembly'})
        self.assertEqual(plans['sync'].start,plans['sync'].end)
        self.assertLessEqual(plans['sync'].end,plans['assembly'].start)

    def test_staff_pause_is_not_capacity_overload(self):
        shift = ccAPPS.calendar(name='shift',default=0)
        people = ccAPPS.calendar(name='people_count',default=0)
        for start,end in [(0,1),(2,4)]:
            shift.setValue(self.origin+timedelta(hours=start),self.origin+timedelta(hours=end),1)
            people.setValue(self.origin+timedelta(hours=start),self.origin+timedelta(hours=end),2)
        machine = ccAPPS.resource(name='machine',maximum=1,available=shift)
        staff = ccAPPS.resource(name='people',maximum=2,available=shift,maximum_calendar=people)
        op = ccAPPS.operation_fixed_time(name='long',duration=7200)
        ccAPPS.load(operation=op,resource=machine,quantity=1)
        ccAPPS.load(operation=op,resource=staff,quantity=1.5)
        plan = ccAPPS.operationplan(operation=op,quantity=1,end=self.origin+timedelta(hours=3))
        self.partial()
        # The gap is not worked through. The lot keeps a 2-hour wall span and
        # sits in the later shift, which is long enough.
        self.assertEqual(plan.start,self.origin+timedelta(hours=2))
        self.assertEqual(plan.end,self.origin+timedelta(hours=4))
        self.assertFalse(any(p.entity == 'capacity' for p in ccAPPS.problems()))

    def test_open_shift_overload_is_reported(self):
        shift = ccAPPS.calendar(name='shift',default=0)
        shift.setValue(self.origin,self.origin+timedelta(hours=3),1)
        staff = ccAPPS.resource(name='people',maximum=2,available=shift)
        for i in range(2):self.make('lot'+str(i),[(staff,1.5)],3600)
        self.assertTrue(any(p.entity == 'capacity' for p in ccAPPS.problems()))

    def test_partial_delivery_reserves_future_manufacturing_consumption(self):
        location = ccAPPS.location(name='factory')
        component = ccAPPS.item(name='component')
        product = ccAPPS.item(name='product')
        make = ccAPPS.operation_time_per(name='make',item=component,location=location,
                                         duration_per=86400,size_maximum=1)
        finish = ccAPPS.operation_fixed_time(name='finish',item=product,location=location,duration=86400)
        ccAPPS.load(operation=make,resource=ccAPPS.resource(name='press'),quantity=1)
        ccAPPS.load(operation=finish,resource=ccAPPS.resource(name='assembly'),quantity=1)
        ccAPPS.flow(operation=make,item=component,location=location,quantity=1,type='flow_end')
        ccAPPS.flow(operation=finish,item=component,location=location,quantity=-1,type='flow_start')
        ccAPPS.flow(operation=finish,item=product,location=location,quantity=1,type='flow_end')
        for name,item,due in [('part',component,1),('order',product,2)]:
            ccAPPS.demand(name=name,item=item,location=location,quantity=1,
                          due=self.origin+timedelta(days=due),status='open')
        ccAPPS.solver_mrp(constraints=0,plantype=1,loglevel=0).solve()
        self.partial()
        deliveries = [p for p in ccAPPS.operationplans() if p.demand is not None and p.quantity>0]
        self.assertEqual(len(deliveries),2)
        self.assertTrue(all(p.start >= self.origin+timedelta(days=3) for p in deliveries))
        self.assertFalse(any('material shortage' in p.description for p in ccAPPS.problems()))

    def test_shared_mold_serializes_different_machines(self):
        mold = ccAPPS.resource(name='mold', maximum=1)
        a = self.make('a', [(ccAPPS.resource(name='m1'),1),(mold,1)])
        b = self.make('b', [(ccAPPS.resource(name='m2'),1),(mold,1)])
        ccAPPS.run_cp()
        self.assertTrue(a.end <= b.start or b.end <= a.start)

    def test_fractional_people_capacity(self):
        staff = ccAPPS.resource(name='people', maximum=3)
        plans = [self.make(str(i), [(ccAPPS.resource(name='m'+str(i)),1),
                                  (staff,1.5)]) for i in range(3)]
        ccAPPS.run_cp()
        self.assertEqual(max(p.end for p in plans), self.origin+timedelta(hours=2))
        self.assertEqual(sum(p.start == self.origin for p in plans),2)

    def test_locked_staff_reserves_actual_quantity(self):
        staff = ccAPPS.resource(name='people', maximum=3)
        locked = self.make('locked', [(ccAPPS.resource(name='m0'),1),(staff,2)])
        locked.status = 'confirmed'
        movable = self.make('move', [(ccAPPS.resource(name='m1'),1),(staff,2)])
        ccAPPS.run_cp()
        self.assertGreaterEqual(movable.start,locked.end)
        self.assertEqual(locked.start,self.origin)

    def test_shift_headcount_calendar(self):
        calendar = ccAPPS.calendar(name='attendance', default=0)
        calendar.setValue(self.origin,self.origin+timedelta(hours=1),1)
        calendar.setValue(self.origin+timedelta(hours=1),self.origin+timedelta(hours=3),3)
        staff = ccAPPS.resource(name='people',maximum=3,maximum_calendar=calendar)
        plan = self.make('move',[(ccAPPS.resource(name='machine'),1),(staff,2)])
        ccAPPS.run_cp()
        self.assertEqual(plan.start,self.origin+timedelta(hours=1))

    def test_lot_crosses_shift_as_one_interval(self):
        calendar = ccAPPS.calendar(name='attendance', default=0)
        calendar.setValue(self.origin,self.origin+timedelta(hours=2),2)
        calendar.setValue(self.origin+timedelta(hours=2),self.origin+timedelta(hours=5),2)
        staff = ccAPPS.resource(name='people',maximum=2,maximum_calendar=calendar)
        plan = self.make('cross',[(ccAPPS.resource(name='machine'),1),(staff,1)],10800)
        ccAPPS.run_cp()
        self.assertEqual(plan.start,self.origin)
        self.assertEqual(plan.end,self.origin+timedelta(hours=3))
        self.assertEqual((plan.end-plan.start).total_seconds(),10800)

    def test_processing_keeps_wall_clock_duration(self):
        calendar = ccAPPS.calendar(name='shifts',default=0)
        calendar.setValue(self.origin,self.origin+timedelta(hours=1),1)
        calendar.setValue(self.origin+timedelta(hours=2),self.origin+timedelta(hours=4),1)
        machine = ccAPPS.resource(name='machine',maximum=1,available=calendar)
        op = ccAPPS.operation_fixed_time(name='long',duration=7200)
        ccAPPS.load(operation=op,resource=machine,quantity=1)
        plan = ccAPPS.operationplan(operation=op,quantity=1,end=self.origin+timedelta(hours=3))
        ccAPPS.run_cp()
        self.assertEqual(plan.start,self.origin)
        self.assertEqual(plan.end,self.origin+timedelta(hours=2))
        self.assertEqual((plan.end-plan.start).total_seconds(),7200)
        self.assertEqual(plan.quantity,1)

    def test_locked_order_can_cross_closed_shift(self):
        calendar = ccAPPS.calendar(name='shifts',default=0)
        calendar.setValue(self.origin,self.origin+timedelta(hours=1),1)
        calendar.setValue(self.origin+timedelta(hours=2),self.origin+timedelta(hours=5),1)
        machine = ccAPPS.resource(name='machine',maximum=1,available=calendar)
        op = ccAPPS.operation_fixed_time(name='locked',duration=7200)
        ccAPPS.load(operation=op,resource=machine,quantity=1)
        locked = ccAPPS.operationplan(operation=op,quantity=1,end=self.origin+timedelta(hours=3))
        locked.status = 'confirmed'
        movable = self.make('move',[(machine,1)])
        ccAPPS.run_cp()
        self.assertEqual(movable.start,self.origin+timedelta(hours=3))
        self.assertEqual(locked.end,self.origin+timedelta(hours=3))

    def test_scenario6_real_loads(self):
        import json
        data = json.loads((Path(__file__).parent/'fc_aps_scenario6.json').read_text())['data']
        by_op = {}
        for row in data['operationResources']:
            by_op.setdefault(row['operation'],[]).append(row)
        # Select two actual alternatives sharing a mold but using different machines.
        rows = [o for o in data['operations'] if o['name'].startswith('FC_OP_SYNC_G_FC-M-02955_1__')][:2]
        specs = {r['name']:r for r in data['resources']}
        resources = {}
        plans = []
        for row in rows:
            loads = []
            for load in by_op[row['name']]:
                name = load['resource']
                if name not in resources:
                    resources[name] = ccAPPS.resource(name=name,maximum=specs[name]['maximum'])
                loads.append((resources[name],load['quantity']))
            plans.append(self.make(row['name'],loads,int(row['duration_per']*10)))
        ccAPPS.run_cp()
        a,b = plans
        self.assertTrue(a.end <= b.start or b.end <= a.start)
        self.assertEqual(a.quantity,1)
        self.assertEqual(b.quantity,1)

    def test_alternatives_preserve_coproduct_balance(self):
        machine = ccAPPS.resource(name='machine')
        item = ccAPPS.item(name='a')
        partner = ccAPPS.item(name='b')
        location = ccAPPS.location(name='factory')
        parent = ccAPPS.operation_alternate(name='choice',location=location)
        slow = ccAPPS.operation_fixed_time(name='slow',duration=7200,location=location)
        fast = ccAPPS.operation_fixed_time(name='fast',duration=1800,location=location)
        for priority,op in enumerate([slow,fast],1):
            ccAPPS.suboperation(owner=parent,operation=op,priority=priority)
            ccAPPS.load(operation=op,resource=machine,quantity=1)
            ccAPPS.flow(operation=op,item=item,location=location,quantity=1,type='flow_end')
        ccAPPS.flow(operation=slow,item=partner,location=location,quantity=1,type='flow_end')
        plan = ccAPPS.operationplan(operation=slow,quantity=1,start=self.origin)
        ccAPPS.run_cp()
        self.assertEqual(plan.operation.name,'slow')
        self.assertEqual(plan.quantity,1)

    def test_alternate_duration_uses_selected_operation(self):
        machine = ccAPPS.resource(name='machine')
        parent = ccAPPS.operation_alternate(name='choice')
        slow = ccAPPS.operation_fixed_time(name='slow', duration=7200)
        fast = ccAPPS.operation_fixed_time(name='fast', duration=1800)
        for priority, op in enumerate([slow,fast],1):
            ccAPPS.suboperation(owner=parent, operation=op, priority=priority)
            ccAPPS.load(operation=op,resource=machine,quantity=1)
        plan = ccAPPS.operationplan(operation=slow,quantity=1,start=self.origin)
        ccAPPS.run_cp()
        self.assertEqual(plan.operation.name,'fast')
        self.assertEqual((plan.end-plan.start).total_seconds(),1800)

if __name__ == '__main__':
    # Engine objects are process-global; isolate cases from erase/lifetime effects.
    if len(sys.argv) == 1:
        import subprocess
        failures = 0
        for name in unittest.defaultTestLoader.getTestCaseNames(CpCapacityTests):
            result = subprocess.run([sys.executable, __file__, 'CpCapacityTests.'+name])
            failures += result.returncode != 0
        sys.exit(bool(failures))
    unittest.main()

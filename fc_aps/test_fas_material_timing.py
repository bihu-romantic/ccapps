"""FAS must not start a consumer before allocated FIFO supply is ready."""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ccAPPS


class FasMaterialTimingTests(unittest.TestCase):
    def setUp(self):
        ccAPPS.erase(True)
        self.origin = datetime(2026, 10, 16, 8)
        ccAPPS.settings.current = self.origin

    def test_consumer_waits_for_producer_completion(self):
        location = ccAPPS.location(name="factory")
        machine = ccAPPS.resource(name="press", maximum=1)
        component = ccAPPS.item(name="comp", category="自制")
        finished = ccAPPS.item(name="fg", category="自制")
        ccAPPS.buffer(name="comp @ factory", item=component, location=location, onhand=0)
        ccAPPS.buffer(name="fg @ factory", item=finished, location=location, onhand=0)

        make_comp = ccAPPS.operation_time_per(
            name="make_comp", location=location, duration_per=3600
        )
        ccAPPS.load(operation=make_comp, resource=machine, quantity=1)
        ccAPPS.flow(
            operation=make_comp,
            item=component,
            location=location,
            quantity=1,
            type="flow_end",
        )

        make_fg = ccAPPS.operation_time_per(
            name="make_fg", location=location, duration_per=3600
        )
        ccAPPS.load(operation=make_fg, resource=machine, quantity=1)
        ccAPPS.flow(
            operation=make_fg,
            item=component,
            location=location,
            quantity=-1,
            type="flow_start",
        )
        ccAPPS.flow(
            operation=make_fg,
            item=finished,
            location=location,
            quantity=1,
            type="flow_end",
        )

        # Two FG orders share one component buffer. Total component need is 20;
        # supply expansion creates component lots that FAS must sequence so
        # onhand never goes negative at a consumer start.
        for index in range(2):
            ccAPPS.demand(
                name=f"order{index}",
                item=finished,
                location=location,
                quantity=10,
                status="open",
                due=self.origin + timedelta(days=2),
                priority=1,
            )

        solver = ccAPPS.solverFAS(constraints=52, plantype=1, loglevel=0)
        solver.solve()
        solver.commit()

        min_onhand = 0.0
        for buffer in ccAPPS.buffers():
            if buffer.item and buffer.item.name != "comp":
                continue
            for flowplan in buffer.flowplans:
                min_onhand = min(min_onhand, float(flowplan.onhand))
        self.assertGreaterEqual(min_onhand, -1e-6, msg=f"comp min onhand={min_onhand}")

        consumers = [
            plan
            for plan in ccAPPS.operationplans()
            if plan.operation and plan.operation.name == "make_fg"
        ]
        producers = [
            plan
            for plan in ccAPPS.operationplans()
            if plan.operation and plan.operation.name == "make_comp"
        ]
        self.assertTrue(consumers)
        self.assertTrue(producers)
        for consumer in consumers:
            covering = [
                producer.end
                for producer in producers
                if producer.end <= consumer.start
            ]
            # At least one completed producer batch must be available at start;
            # with a unary machine the full FIFO chain is serialized.
            self.assertTrue(
                covering or any(p.end <= consumer.start for p in producers),
                msg=f"consumer {consumer.reference} starts {consumer.start} "
                f"before component supply",
            )

    def test_onhand_not_double_counted_with_stock_flowplan(self):
        """Opening stock is getOnHand only; STCK must not also supply FIFO."""
        location = ccAPPS.location(name="factory")
        machine = ccAPPS.resource(name="press", maximum=1)
        component = ccAPPS.item(name="comp_stock", category="自制")
        finished = ccAPPS.item(name="fg_stock", category="自制")
        ccAPPS.buffer(
            name="comp_stock @ factory",
            item=component,
            location=location,
            onhand=10,
        )
        ccAPPS.buffer(
            name="fg_stock @ factory", item=finished, location=location, onhand=0
        )

        make_comp = ccAPPS.operation_time_per(
            name="make_comp_stock", location=location, duration_per=3600
        )
        ccAPPS.load(operation=make_comp, resource=machine, quantity=1)
        ccAPPS.flow(
            operation=make_comp,
            item=component,
            location=location,
            quantity=1,
            type="flow_end",
        )

        make_fg = ccAPPS.operation_time_per(
            name="make_fg_stock", location=location, duration_per=3600
        )
        ccAPPS.load(operation=make_fg, resource=machine, quantity=1)
        ccAPPS.flow(
            operation=make_fg,
            item=component,
            location=location,
            quantity=-1,
            type="flow_start",
        )
        ccAPPS.flow(
            operation=make_fg,
            item=finished,
            location=location,
            quantity=1,
            type="flow_end",
        )
        # Need 20 components: 10 onhand + 10 from make_comp. Starting the FG
        # lot before make_comp finishes means STCK was double-counted.
        ccAPPS.demand(
            name="order_stock",
            item=finished,
            location=location,
            quantity=20,
            status="open",
            due=self.origin + timedelta(days=3),
            priority=1,
        )

        solver = ccAPPS.solverFAS(constraints=52, plantype=1, loglevel=0)
        solver.solve()
        solver.commit()

        min_onhand = 0.0
        for buffer in ccAPPS.buffers():
            if buffer.item and buffer.item.name != "comp_stock":
                continue
            for flowplan in buffer.flowplans:
                min_onhand = min(min_onhand, float(flowplan.onhand))
        self.assertGreaterEqual(
            min_onhand, -1e-6, msg=f"comp_stock min onhand={min_onhand}"
        )

        consumers = [
            plan
            for plan in ccAPPS.operationplans()
            if plan.operation and plan.operation.name == "make_fg_stock"
        ]
        producers = [
            plan
            for plan in ccAPPS.operationplans()
            if plan.operation and plan.operation.name == "make_comp_stock"
        ]
        self.assertTrue(consumers)
        self.assertTrue(producers)
        for consumer in consumers:
            self.assertTrue(
                any(p.end <= consumer.start for p in producers),
                msg=f"FG {consumer.reference} starts {consumer.start} before "
                f"component lots {[str(p.end) for p in producers]}",
            )

    def test_selfmade_infinite_buffer_without_maker_is_unplanned(self):
        """自制 marked infinite with no producer must not go negative."""
        location = ccAPPS.location(name="factory")
        machine = ccAPPS.resource(name="press", maximum=1)
        missing = ccAPPS.item(name="missing_comp", category="自制")
        finished = ccAPPS.item(name="fg2", category="自制")
        ccAPPS.buffer_infinite(
            name="missing_comp @ factory",
            item=missing,
            location=location,
            onhand=0,
        )
        ccAPPS.buffer(
            name="fg2 @ factory", item=finished, location=location, onhand=0
        )

        make_fg = ccAPPS.operation_time_per(
            name="make_fg2", location=location, duration_per=3600
        )
        ccAPPS.load(operation=make_fg, resource=machine, quantity=1)
        ccAPPS.flow(
            operation=make_fg,
            item=missing,
            location=location,
            quantity=-1,
            type="flow_start",
        )
        ccAPPS.flow(
            operation=make_fg,
            item=finished,
            location=location,
            quantity=1,
            type="flow_end",
        )
        ccAPPS.demand(
            name="order_missing",
            item=finished,
            location=location,
            quantity=5,
            status="open",
            due=self.origin + timedelta(days=1),
            priority=1,
        )

        solver = ccAPPS.solverFAS(constraints=52, plantype=1, loglevel=0)
        solver.solve()
        solver.commit()

        min_onhand = 0.0
        for buffer in ccAPPS.buffers():
            if buffer.item and buffer.item.name != "missing_comp":
                continue
            for flowplan in buffer.flowplans:
                min_onhand = min(min_onhand, float(flowplan.onhand))
        self.assertGreaterEqual(
            min_onhand, -1e-6, msg=f"missing_comp min onhand={min_onhand}"
        )


if __name__ == "__main__":
    unittest.main()

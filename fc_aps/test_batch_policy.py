"""Run with venv Python and engine libraries on LD_LIBRARY_PATH."""
import sys
import unittest
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ccAPPS

class BatchPolicyTests(unittest.TestCase):
    def setUp(self):
        ccAPPS.erase(True)
        ccAPPS.settings.current = datetime(2026, 9, 10, 8)
        self.loc = ccAPPS.location(name='factory')

    def model(self, economic=50, snp=20, exact=False, quantity=130, limit=None):
        item = ccAPPS.item(name='A')
        buf = ccAPPS.buffer(name='A @ factory', item=item, location=self.loc)
        op = ccAPPS.operation_time_per(name='make', duration_per=1, location=self.loc, item=item)
        ccAPPS.flow(operation=op, item=item, location=self.loc, quantity=1, type='flow_end')
        op.fc_batch_policy = True
        op.fc_economic_batch = float(economic)
        op.fc_snp = float(snp or 1)
        op.fc_exact_quantity = exact
        op.size_maximum = economic if economic else 1e12
        if limit is not None:
            buf.fc_production_limit = float(limit)
        ccAPPS.demand(name='d', item=item, location=self.loc, quantity=quantity,
                      due=datetime(2026,9,12), status='open', maxlateness=0)
        return op

    def solve(self):
        ccAPPS.solver_mrp(constraints=52, plantype=1, loglevel=0).solve()
        return sorted(p.quantity for p in ccAPPS.operationplans() if p.operation.name == 'make')

    def test_economic_lots_and_rounded_tail(self):
        self.model()
        self.assertEqual(self.solve(), [40,50,50])

    def test_exact_lifecycle_tail(self):
        self.model(exact=True, limit=130)
        self.assertEqual(self.solve(), [30,50,50])

    def test_economic_is_not_rounded_to_snp(self):
        self.model(quantity=120)
        self.assertEqual(self.solve(), [20,50,50])

    def test_zero_coproduct_need_blocks_group(self):
        self.model(exact=True, limit=0)
        self.assertEqual(self.solve(), [])

    def test_alternate_routing(self):
        op = self.model()
        parent = ccAPPS.operation_alternate(name='alternate', item=ccAPPS.item(name='A'), location=self.loc)
        ccAPPS.suboperation(owner=parent, operation=op, priority=1)
        op.item = None
        self.assertEqual(self.solve(), [40,50,50])

    def test_legacy_multiple_is_unchanged(self):
        op = self.model(economic=0)
        op.fc_batch_policy = False
        op.size_multiple = 20
        self.assertEqual(self.solve(), [140])

    def test_alternate_tail_larger_than_economic(self):
        op = self.model(quantity=49)
        parent = ccAPPS.operation_alternate(name='alternate', item=ccAPPS.item(name='A'), location=self.loc)
        ccAPPS.suboperation(owner=parent, operation=op, priority=1)
        op.item = None
        self.assertEqual(self.solve(), [60])

    def test_no_economic_uses_snp(self):
        self.model(economic=0)
        self.assertEqual(self.solve(), [140])

    def test_zero_snp_uses_one(self):
        self.model(economic=0, snp=0)
        self.assertEqual(self.solve(), [130])

    def test_exact_without_economic(self):
        self.model(economic=0, exact=True, limit=130)
        self.assertEqual(self.solve(), [130])

    def test_rounded_tail_may_exceed_economic(self):
        self.model(quantity=49)
        self.assertEqual(self.solve(), [60])

    def test_coproduct_limit(self):
        op = self.model(exact=True, quantity=130)
        item = ccAPPS.item(name='B')
        buf = ccAPPS.buffer(name='B @ factory', item=item, location=self.loc)
        buf.fc_production_limit = 70.0
        ccAPPS.flow(operation=op, item=item, location=self.loc, quantity=1, type='flow_end')
        self.assertLessEqual(sum(self.solve()), 70)
        self.assertGreater(sum(p.quantity for p in ccAPPS.operationplans() if p.operation.name=='make'),0)

    def test_unavoidable_coproduct_excess_keeps_required_plan(self):
        op = self.model(economic=0, snp=1, quantity=52)
        op.fc_allow_coproduct_excess = True
        partner = ccAPPS.item(name='B')
        partner_buf = ccAPPS.buffer(name='B @ factory', item=partner, location=self.loc)
        partner_buf.onhand = 108
        ccAPPS.flow(operation=op, item=partner, location=self.loc, quantity=1, type='flow_end')
        self.assertEqual(self.solve(), [52])

    def test_existing_stock_reduces_restricted_supply(self):
        self.model(exact=True, limit=50, quantity=130)
        ccAPPS.buffer(name='A @ factory').onhand = 80
        self.assertEqual(self.solve(), [50])

if __name__ == '__main__':
    unittest.main()

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ccAPPSdb.safetystock.views import dashboard_data


class DashboardDataTests(unittest.TestCase):
    def test_projection_does_not_double_count_opening_stock(self):
        result = SimpleNamespace(
            asof=datetime(2021, 2, 1), calculated_at=datetime(2021, 2, 1),
            buffer=SimpleNamespace(pk=1, item_id="ball", location_id="RDC",
                onhand=25, item=SimpleNamespace(category="sport", cost=10)),
            leadtime_days=7, ss=90, roq=300, mean=3, deviation=2,
            details={"segments": [], "sources": {}, "params": {}, "history_days":365},
        )
        order = SimpleNamespace(reference="po1", type="PO", status="proposed",
                                supplier_id="supplier", origin_id=None, location_id="RDC")
        flow = SimpleNamespace(item_id="ball", location_id="RDC", operationplan=order,
                               flowdate=datetime(2021, 2, 8), quantity=100)
        sales, ledger = MagicMock(), MagicMock()
        sales.using.return_value.filter.return_value.exclude.return_value.annotate.return_value.values.return_value.annotate.return_value.order_by.return_value = []
        ledger.using.return_value.filter.return_value.exclude.return_value.select_related.return_value.order_by.return_value = [flow]
        with patch("ccAPPSdb.safetystock.views.Demand.objects", sales), patch("ccAPPSdb.safetystock.views.OperationPlanMaterial.objects", ledger):
            data = dashboard_data("scenario2", [result])
        row = data["rows"][0]
        self.assertEqual(row["projected"], 125)
        self.assertEqual(row["onhand"], 25)
        self.assertEqual(row["risk"], "attention")
        self.assertEqual(data["purchases"], 1)
        ledger.using.return_value.filter.return_value.exclude.assert_called_once_with(operationplan__type="STCK")

    def test_empty_results(self):
        self.assertEqual(dashboard_data("scenario2", [])["rows"], [])

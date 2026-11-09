import math
import unittest
from ccAPPSdb.safetystock.policy import (
    calculate,
    compile_segment,
    matches,
    validate_parameters,
)


class PolicyMathTests(unittest.TestCase):
    def test_eoq_and_uncertainty(self):
        result = calculate(
            12000 / 365, 3, 5, 50, {"fixed_order_cost": 100, "holding_cost": 0.2}
        )
        self.assertAlmostEqual(result["roq"], math.sqrt(240000), places=5)
        self.assertAlmostEqual(result["ss"], 1.64485362695 * 3 * math.sqrt(5), places=5)

    def test_supply_variation(self):
        baseline = calculate(10, 3, 5, 50, {})
        variable = calculate(10, 3, 5, 50, {"leadtime_deviation": 2})
        self.assertGreater(variable["ss"], baseline["ss"])

    def test_published_bound_rules_and_maximum_wins(self):
        result = calculate(
            10,
            0,
            5,
            50,
            {"ss_min_qty": 100, "ss_min_poc": 2, "ss_max_qty": 5, "ss_max_poc": 1},
        )
        self.assertEqual(result["ss"], 10)

    def test_zero_is_an_explicit_upper_bound(self):
        self.assertEqual(calculate(10, 3, 5, 50, {"ss_max_qty": 0})["ss"], 0)

    def test_do_not_stock(self):
        result = calculate(10, 3, 5, 50, {"do_not_stock": 1})
        self.assertEqual((result["ss"], result["roq"]), (0, 1))

    def test_zero_cost_has_explanation(self):
        result = calculate(10, 3, 5, 0, {"roq_min_qty": 20})
        self.assertEqual(result["roq"], 20)
        self.assertTrue(result["warnings"])

    def test_invalid_configuration(self):
        for config in (
            {"service_level": 100},
            {"roq_min_qty": -1},
            {"holding_cost": float("nan")},
            {"unknown": 2},
        ):
            with self.assertRaises(ValueError):
                validate_parameters(config)

    def test_segment_cost_and_location(self):
        predicate = compile_segment("item.cost <= 20 AND location.name = 'Paris'")
        self.assertTrue(matches(predicate, {"item.cost": 15, "location.name": "Paris"}))
        self.assertFalse(
            matches(predicate, {"item.cost": 30, "location.name": "Paris"})
        )
        self.assertFalse(
            matches(predicate, {"item.cost": None, "location.name": "Paris"})
        )

    def test_like(self):
        self.assertTrue(
            matches(
                compile_segment("location.name like '%shop%'"),
                {"location.name": "Tennis shop Paris"},
            )
        )

    def test_no_sql_execution(self):
        for query in (
            "1=1",
            "item.cost=2; drop table item",
            "item.cost=2 OR 1=1",
            "item.password='x'",
            "item.cost=2 AND ",
        ):
            with self.assertRaises(ValueError):
                compile_segment(query)


if __name__ == "__main__":
    unittest.main()

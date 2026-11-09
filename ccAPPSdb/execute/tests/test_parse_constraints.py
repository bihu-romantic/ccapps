"""Constraint flag parsing must use bitwise OR, not arithmetic add."""
from django.test import SimpleTestCase

from ccAPPSdb.execute.management.commands.runplan import (
    constraintString,
    parseConstraints,
)


class ParseConstraintsTests(SimpleTestCase):
    def test_mfg_lt_and_lt_together_keep_manufacturing_leadtime(self):
        # Regression: += turned 16+16 into 32 and dropped mfg_lt (task 195).
        value = parseConstraints("capa,mfg_lt,po_lt,fence,lt")
        self.assertTrue(value & 4, "capa")
        self.assertTrue(value & 8, "fence")
        self.assertTrue(value & 16, "mfg_lt")
        self.assertTrue(value & 32, "po_lt")
        text = constraintString(value)
        self.assertIn("mfg_lt", text)
        self.assertIn("po_lt", text)
        self.assertIn("capa", text)

    def test_mfg_lt_alone(self):
        value = parseConstraints("capa,mfg_lt,po_lt")
        self.assertEqual(constraintString(value), "capa,mfg_lt,po_lt")

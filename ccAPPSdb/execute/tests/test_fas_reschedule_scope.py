"""FAS reschedule gates that must not change other solvers."""
from unittest.mock import patch

from django.test import SimpleTestCase

from ccAPPSdb.common.commands import (
    planning_success_status,
    should_preserve_task_message,
)
from ccAPPSdb.execute.commands import (
    follow_changed_demand_quantity,
    should_revert_fas_inputs,
)


class Message(object):
    def __init__(self, name, quantity, status="proposed", owner=None):
        self.demand = type("Demand", (), {"name": name, "quantity": quantity})()
        self.quantity = quantity
        self.status = status
        self.owner = owner
        self.reference = name
        self.start = "start"
        self.end = "end"


class FasScopeTests(SimpleTestCase):
    def test_outcome_text_is_kept_and_step_text_is_not(self):
        self.assertTrue(should_preserve_task_message("local_outcome={\"status\":true}"))
        self.assertFalse(should_preserve_task_message("Creating buckets"))
        self.assertFalse(should_preserve_task_message(""))
        self.assertEqual(
            planning_success_status('local_outcome={"status":false}'),
            ("Failed", 'local_outcome={"status":false}'),
        )
        self.assertEqual(planning_success_status("Creating buckets"), ("Done", ""))

    def test_rollback_predicate_is_fas_only(self):
        self.assertTrue(should_revert_fas_inputs("fas", True))
        self.assertFalse(should_revert_fas_inputs("fas", False))
        self.assertFalse(should_revert_fas_inputs("mrp", True))
        self.assertFalse(should_revert_fas_inputs("cp", True))
        self.assertFalse(should_revert_fas_inputs("ga", True))

    def test_quantity_follow_sets_the_top_plan(self):
        parent = Message("SO-QTY", 600)
        child = Message("SO-QTY", 600, owner=parent)
        parent.demand.quantity = 300
        child.demand = parent.demand
        def once():
            yield child

        follow_changed_demand_quantity(
            {
                "LOCAL_NEW_DEMANDS": "SO-QTY",
                "LOCAL_DELETED_DEMANDS": "SO-QTY",
            },
            once(),
        )
        self.assertEqual(parent.quantity, 300)

    def test_quantity_follow_leaves_an_increase_alone(self):
        parent = Message("SO-QTY", 300)
        parent.demand.quantity = 600
        follow_changed_demand_quantity(
            {
                "LOCAL_NEW_DEMANDS": "SO-QTY",
                "LOCAL_DELETED_DEMANDS": "SO-QTY",
            },
            [parent],
        )
        self.assertEqual(parent.quantity, 300)

    def test_failed_fas_solve_reverts_and_other_solvers_do_not(self):
        from ccAPPSdb.execute import commands

        def boom():
            raise RuntimeError("solve exploded")

        with patch(
            "ccAPPSdb.execute.views.revert_reschedule_inputs", return_value=True
        ) as revert:
            with self.assertRaises(RuntimeError):
                commands.finish_local_solve("fas", "scenario9", boom)
            revert.assert_called_once_with("scenario9")
            revert.reset_mock()
            with self.assertRaises(RuntimeError):
                commands.finish_local_solve("mrp", "scenario9", boom)
            revert.assert_not_called()
            revert.reset_mock()
            with self.assertRaises(RuntimeError):
                commands.finish_local_solve("cp", "scenario9", boom)
            revert.assert_not_called()

    def test_revert_snapshot_is_written_only_for_fas(self):
        from ccAPPSdb.execute import views

        with patch.object(views, "_apply_order_changes", return_value=[]), patch.object(
            views, "_apply_material_changes", return_value=([], [])
        ), patch.object(views, "_apply_resource_changes", return_value=[]), patch(
            "ccAPPSdb.common.models.Parameter"
        ) as parameter:
            views._apply_reschedule_changes("scenario9", {"_solver": "mrp"})
            parameter.objects.using.assert_not_called()
            views._apply_reschedule_changes("scenario9", {"_solver": "cp"})
            parameter.objects.using.assert_not_called()
            views._apply_reschedule_changes(
                "scenario9", {"_solver": "fas", "_orders": []}
            )
            parameter.objects.using.assert_called_with("scenario9")

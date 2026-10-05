"""Start / due dates and blocked-by. v0.236.0 (approved queue item 2)."""

import datetime

import frappe

from erpnext_mcp import compliance_rules, task_dates
from erpnext_mcp.alerts import engine

from .harness import STORE
from .test_dispatch import ALL_ON, DispatchTestCase

DATES_ON = {**ALL_ON, "allow_set_task_dates": 1, "allow_list_overdue_tasks": 1, "allow_link_farm_tasks": 1,
            "allow_get_farm_task": 1}


def day(offset=0):
	return (datetime.date.fromisoformat(str(frappe.utils.today())[:10]) + datetime.timedelta(days=offset)).isoformat()


class DatesOnTasks(DispatchTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **DATES_ON)

	def test_dates_are_set_described_and_overdue_is_computed(self):
		task = self.a_task(due_date=day(-3), start_date=day(-10))
		self.assertEqual(task["due_date"], day(-3))
		self.assertTrue(task["overdue"])
		self.assertEqual(task["days_until_due"], -3)
		overdue = self.tool_data("list_overdue_tasks", {})
		self.assertIn(task["name"], [row["name"] for row in overdue["tasks"]])

	def test_a_due_date_before_the_start_is_refused(self):
		error = self.tool_error(
			"create_farm_task",
			{"task_name": "Prune", "task_type": "Inspection", "evidence_required": {"photos": True},
			 "start_date": day(5), "due_date": day(1)},
		)
		self.assertIn("before start_date", error)

	def test_set_task_dates_moves_them(self):
		task = self.a_task()["name"]
		data = self.tool_data("set_task_dates", {"task": task, "due_date": day(2), "starts_after": "after the hail net is up"})
		self.assertTrue(data["after"]["due_soon"])
		self.assertEqual(data["after"]["starts_after"], "after the hail net is up")

	def test_the_overdue_rule_raises_warning_then_critical(self):
		self.a_task(due_date=day(1))
		self.a_task(task_name="Late walk", due_date=day(-1))
		self.a_task(task_name="Far off", due_date=day(30))
		doc = compliance_rules.build_rule(
			{**task_dates.rule_spec(), "enabled": 1, "human_approved_by": "Administrator",
			 "human_approved_on": frappe.utils.now()}
		)
		doc.insert(ignore_permissions=True)
		result = engine.preview(compliance_rules.rule_row(doc.name), {"today": str(frappe.utils.today()), "company": ""})
		self.assertEqual(sorted(o["severity"] for o in result["observations"]), ["Critical", "Warning"])


class BlockedBy(DispatchTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **DATES_ON)

	def test_an_unfinished_blocker_stops_the_start_and_a_cancelled_one_does_not(self):
		blocker = self.a_task(task_name="Hang the hail net")["name"]
		work = self.claimed(task_name="Thin Block 7")
		self.tool_data("link_farm_tasks", {"task": work, "linked_task": blocker, "relationship": "blocked_by"})
		error = self.tool_error("start_farm_task", {"task": work, "worker_id": "EMP-001"})
		self.assertIn("is waiting on Hang the hail net", error)
		STORE.get_raw("Farm Task", blocker)["state"] = "Cancelled"
		started = self.tool_data("start_farm_task", {"task": work, "worker_id": "EMP-001"})
		self.assertIn("No longer waiting on Hang the hail net", started["unblocked_note"])


class ATemplateGivesItsDates(DispatchTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **{**DATES_ON, "allow_create_farm_task_template": 1, "allow_create_task_from_template": 1})

	def test_a_task_raised_from_a_template_takes_its_offsets(self):
		template = self.tool_data(
			"create_farm_task_template",
			{"template_name": "Dormant pruning", "task_type": "Maintenance", "evidence_required": {"photos": True},
			 "default_start_after_days": 1, "default_due_after_days": 14},
		)["name"]
		task = self.tool_data("create_task_from_template", {"template": template})
		task = task.get("task", task)
		self.assertEqual(task["start_date"], day(1))
		self.assertEqual(task["due_date"], day(14))

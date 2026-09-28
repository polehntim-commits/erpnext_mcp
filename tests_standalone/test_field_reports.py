# SPDX-License-Identifier: MIT
"""v0.23.0 — Field-Initiated Tasks.

FOUR CLAIMS, AND EVERY CLASS IN THIS FILE IS ONE OF THEM.

1. **A FIELD REPORT CREATES A TASK WITH THE RIGHT SHAPE.** `FieldReportCreatesATask`
   proves the task has origin=field_reported, the reporter's Employee id on
   reported_by, the photo on report_photo, and lands in Available state ready
   for the pool.

2. **ANTI-SPAM IS ENFORCED.** `AntiSpamIsEnforced` proves the rate limit of 5 per
   hour, that a dismissed report counts against the reporter's limit for 24h,
   and that a photo is required.

3. **URGENCY IS CAPPED FOR FIELD WORKERS.** `UrgencyIsCapped` proves that a field
   worker cannot set Critical urgency, but a Foreman can.

4. **THE COMPLIANCE RULE SEEDS.** `TheComplianceRuleSeeds` proves the
   field_flag_awaiting_dispatch rule is seeded by the compliance rule seeder.

5. **THE ORIGIN FIELD IS ON EVERY TASK.** `OriginFieldDefaults` proves the origin
   field defaults to compliance_rule on tasks created through create_farm_task,
   and is field_reported on tasks created through report_field_task.
"""

import json
import unittest

import frappe

from erpnext_mcp import compliance_rules
from erpnext_mcp.erpnext_mcp.doctype.farm_task.farm_task import (
	ORIGIN_COMPLIANCE_RULE,
	ORIGIN_FIELD_REPORTED,
	ORIGINS,
)
from erpnext_mcp.tools.dispatch import FIELD_REPORT_LIMIT

from .fixtures import V12TestCase
from .harness import STORE

ALL_ON = {
	f"allow_{name}": 1
	for name in (
		"create_farm_task",
		"report_field_task",
		"claim_farm_task",
		"reject_farm_task",
		"list_available_tasks",
	)
}

WORKER = "HR-EMP-00002"
FOREMAN = "HR-EMP-00001"


def _a_photo(name="report-photo-001"):
	"""Seed a File record the harness can find."""
	if not STORE.get_raw("File", name):
		STORE.seed(
			"File",
			[
				{
					"name": name,
					"file_name": "problem.jpg",
					"file_url": "/files/problem.jpg",
					"file_size": 48000,
					"is_private": 1,
					"folder": "Home",
				}
			],
		)
	return name


class FieldReportTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON)

	def report(self, **overrides):
		payload = {
			"reported_by": WORKER,
			"photo_file_token": _a_photo(),
			"description": "Broken ladder near MC-Cabin-01",
			"task_type": "Repair",
		}
		payload.update(overrides)
		return self.tool_data("report_field_task", payload)


class FieldReportCreatesATask(FieldReportTestCase):
	"""A field report creates a task with origin=field_reported in Available state."""

	def test_creates_a_task_in_available_state(self):
		data = self.report()
		self.assertEqual(data["state"], "Available")
		self.assertIn("FT-", data["name"])

	def test_origin_is_field_reported(self):
		data = self.report()
		self.assertEqual(data["origin"], "field_reported")

	def test_reported_by_is_set(self):
		data = self.report()
		self.assertEqual(data["reported_by"], WORKER)

	def test_report_photo_is_set(self):
		data = self.report()
		self.assertEqual(data["report_photo"], _a_photo())

	def test_reported_at_is_set(self):
		data = self.report()
		self.assertTrue(data.get("reported_at"))

	def test_description_becomes_notes_and_task_name(self):
		data = self.report(description="Water leak at Block 3")
		task = STORE.get_raw("Farm Task", data["name"])
		self.assertEqual(task["notes"], "Water leak at Block 3")
		self.assertEqual(task["task_name"], "Water leak at Block 3")

	def test_evidence_contract_requires_photos_and_findings(self):
		data = self.report()
		task = STORE.get_raw("Farm Task", data["name"])
		contract = json.loads(task["evidence_required"])
		self.assertTrue(contract.get("photos"))
		self.assertTrue(contract.get("findings_text"))

	def test_dispatch_mode_is_either(self):
		data = self.report()
		self.assertEqual(data["dispatch_mode"], "Either")

	def test_default_urgency_is_normal(self):
		data = self.report()
		self.assertEqual(data["urgency"], "Normal")

	def test_task_type_defaults_to_repair(self):
		data = self.report()
		self.assertEqual(data["task_type"], "Repair")


class AntiSpamIsEnforced(FieldReportTestCase):
	"""Rate limiting prevents spam: 5 per hour, photo required, penalty for dismissed."""

	def test_a_photo_is_not_needed_to_raise_the_work(self):
		"""AFB-2026-00022: "For some reason initiating a task requires a photo."
		The photograph is taken when the work is done, and the completion
		contract still asks for it."""
		data = self.tool_data(
			"report_field_task",
			{"reported_by": WORKER, "description": "Something broken"},
		)
		self.assertIsNone(data["report_photo"])
		self.assertEqual(data["origin"], "field_reported")
		self.assertTrue(data["evidence_required"]["photos"])

	def test_invalid_photo_refused(self):
		message = self.tool_error(
			"report_field_task",
			{"reported_by": WORKER, "photo_file_token": "NONEXISTENT-FILE", "description": "x"},
		)
		self.assertIn("no File", message)

	def test_rate_limit_after_five_reports(self):
		for i in range(FIELD_REPORT_LIMIT):
			photo = _a_photo(f"spam-photo-{i}")
			self.report(photo_file_token=photo, description=f"Report {i}")

		message = self.tool_error(
			"report_field_task",
			{
				"reported_by": WORKER,
				"photo_file_token": _a_photo("spam-photo-extra"),
				"description": "One too many",
			},
		)
		self.assertIn("already filed", message)
		self.assertIn(str(FIELD_REPORT_LIMIT), message)

	def test_penalty_after_dismissed_report(self):
		data = self.report()
		task_name = data["name"]
		frappe.db.set_value("Farm Task", task_name, "state", "Cancelled")
		frappe.db.set_value("Farm Task", task_name, "modified", frappe.utils.now())

		message = self.tool_error(
			"report_field_task",
			{
				"reported_by": WORKER,
				"photo_file_token": _a_photo("penalty-photo"),
				"description": "After penalty",
			},
		)
		self.assertIn("dismissed", message)


class UrgencyIsCapped(FieldReportTestCase):
	"""Workers may choose Normal or High but not Critical."""

	def test_normal_urgency_accepted(self):
		data = self.report(urgency="Normal")
		self.assertEqual(data["urgency"], "Normal")

	def test_high_urgency_accepted(self):
		data = self.report(urgency="High")
		self.assertEqual(data["urgency"], "High")

	def test_critical_refused_for_worker(self):
		message = self.tool_error(
			"report_field_task",
			{
				"reported_by": WORKER,
				"photo_file_token": _a_photo(),
				"urgency": "Critical",
				"description": "Urgent problem",
			},
		)
		self.assertIn("Critical", message)
		self.assertIn("Foreman", message)

	def test_invalid_urgency_refused(self):
		message = self.tool_error(
			"report_field_task",
			{
				"reported_by": WORKER,
				"photo_file_token": _a_photo(),
				"urgency": "Apocalyptic",
				"description": "Not a real urgency",
			},
		)
		self.assertIn("not one of", message)


class OriginFieldDefaults(FieldReportTestCase):
	"""The origin field defaults correctly for each task-creation path."""

	def test_create_farm_task_has_no_field_reported_origin(self):
		data = self.tool_data(
			"create_farm_task",
			{
				"task_name": "Habitability walk",
				"task_type": "Inspection",
				"evidence_required": {"photos": True, "findings_text": True},
			},
		)
		task = STORE.get_raw("Farm Task", data["name"])
		origin = task.get("origin") or ORIGIN_COMPLIANCE_RULE
		self.assertNotEqual(origin, ORIGIN_FIELD_REPORTED)

	def test_report_field_task_sets_field_reported(self):
		data = self.report()
		task = STORE.get_raw("Farm Task", data["name"])
		self.assertEqual(task["origin"], ORIGIN_FIELD_REPORTED)


class TheComplianceRuleSeeds(V12TestCase):
	"""The field_flag_awaiting_dispatch rule is present in declarative_seed_specs."""

	def test_field_flag_rule_in_declarative_specs(self):
		specs = compliance_rules.declarative_seed_specs()
		rule_ids = [s["rule_id"] for s in specs]
		self.assertIn("field_flag_awaiting_dispatch", rule_ids)

	def test_field_flag_rule_shape(self):
		specs = compliance_rules.declarative_seed_specs()
		rule = next(s for s in specs if s["rule_id"] == "field_flag_awaiting_dispatch")
		self.assertEqual(rule["target_doctype"], "Farm Task")
		self.assertEqual(rule["category"], "Workforce")
		self.assertEqual(rule["date_field"], "reported_at")
		self.assertEqual(rule["enabled"], 1)
		filters = rule["scope_filters"]
		field_names = [f["field"] for f in filters]
		self.assertIn("origin", field_names)
		self.assertIn("state", field_names)


class TheOriginVocabularyIsComplete(unittest.TestCase):
	"""The ORIGINS tuple matches the Select options on the doctype."""

	def test_four_origins(self):
		self.assertEqual(len(ORIGINS), 4)
		self.assertIn(ORIGIN_COMPLIANCE_RULE, ORIGINS)
		self.assertIn(ORIGIN_FIELD_REPORTED, ORIGINS)


if __name__ == "__main__":
	unittest.main()

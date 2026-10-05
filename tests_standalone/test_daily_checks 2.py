"""Daily equipment checks: Farm Tasks, base + add-ons by asset type. v0.237.0 (queue item 3, decision 33)."""

import json

from erpnext_mcp import daily_checks

from .harness import STORE
from .test_dispatch import ALL_ON, DispatchTestCase


class TheOperatorChecksTheMachine(DispatchTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **{**ALL_ON, "daily_checks_enabled": 1, "allow_create_farm_task_template": 1, "allow_create_task_from_template": 1})
		daily_checks.seed()
		STORE.seed("Asset Register", [{"name": "TC-TRAKHOE-1", "asset_type": "Mini Excavator", "company": None},
		                              {"name": "MC-Pump-01", "asset_type": "General", "company": None}])

	def items(self, task):
		raw = STORE.get_raw("Farm Task", task)["checklist_status"]
		return [i["item_name"] for i in (json.loads(raw) if isinstance(raw, str) else raw)["items"]]

	def test_the_track_hoe_gets_the_base_check_plus_its_add_on(self):
		work = self.claimed(task_name="Dig the drain", asset="TC-TRAKHOE-1")
		started = self.tool_data("start_farm_task", {"task": work, "worker_id": "EMP-001"})
		check = started["daily_check"]
		self.assertTrue(check["created"])
		names = self.items(check["task"])
		self.assertEqual(names[0], "Walk round: no fresh leaks under the machine")
		self.assertIn("Quick coupler locked", names)
		self.assertNotIn("PTO shield in place", names, "the Tractor add-on is not a Mini Excavator's")
		row = STORE.get_raw("Farm Task", check["task"])
		self.assertEqual((row["assigned_to"], row["asset"]), ("EMP-001", "TC-TRAKHOE-1"))

	def test_once_per_operator_per_machine_per_day(self):
		first = self.claimed(task_name="Dig", asset="TC-TRAKHOE-1")
		self.tool_data("start_farm_task", {"task": first, "worker_id": "EMP-001"})
		self.tool_data("complete_farm_task", {"task": first, "worker_id": "EMP-001", "evidence_files": [
			{"file_url": "/files/x.jpg", "evidence_type": "Photo"}], "signature_file": "/files/s.png",
			"findings_text": "ok", "completion_narrative": "dug"})
		second = self.claimed(task_name="Backfill", asset="TC-TRAKHOE-1")
		again = self.tool_data("start_farm_task", {"task": second, "worker_id": "EMP-001"})["daily_check"]
		self.assertFalse(again["created"])

	def test_off_by_default_and_nothing_for_a_task_without_a_machine(self):
		self.configure(enabled=1, **{**ALL_ON, "daily_checks_enabled": 0})
		work = self.claimed(task_name="Dig", asset="TC-TRAKHOE-1")
		self.assertNotIn("daily_check", self.tool_data("start_farm_task", {"task": work, "worker_id": "EMP-001"}))
		self.configure(enabled=1, **{**ALL_ON, "daily_checks_enabled": 1})
		plain = self.claimed(task_name="Walk the cabin")
		self.assertNotIn("daily_check", self.tool_data("start_farm_task", {"task": plain, "worker_id": "EMP-001"}))

	def test_an_add_on_is_data(self):
		self.tool_data(
			"create_farm_task_template",
			{"template_name": "Daily check add-on — General", "task_type": "Inspection", "daily_check": "Start of Day",
			 "applies_to_asset_types": ["General"], "evidence_required": {"signature": True},
			 "checklist": ["Pump primed"]},
		)
		composed = daily_checks.compose("Start of Day", "General")
		self.assertIn("Pump primed", [i["item_name"] for i in composed["items"]])

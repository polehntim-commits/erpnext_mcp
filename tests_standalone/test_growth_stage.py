"""Crop stage capture and timeline. v0.241.0 (approved queue item 5; decisions 25, 27, 28)."""

import datetime

import frappe

from erpnext_mcp import growth_stage

from .harness import STORE
from .test_dispatch import ALL_ON, DispatchTestCase

ON = {**ALL_ON, "allow_record_growth_stage": 1, "allow_get_stage_timeline": 1}


def day(offset=0):
	return (datetime.date.fromisoformat(str(frappe.utils.today())[:10]) + datetime.timedelta(days=offset)).isoformat()


class StageTestCase(DispatchTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		STORE.seed("Field", [{"name": "B7", "field_name": "B7"}])

	def stage(self, **args):
		return self.tool_data("record_growth_stage", {"block": "B7", **args})


class RecordingAStage(StageTestCase):
	def test_a_code_is_filed_as_a_growth_stage_with_its_words(self):
		data = self.stage(bbch="55", observed_at=f"{day(-2)} 09:00:00")
		self.assertEqual(data["bbch"], "55")
		self.assertTrue(data["stage"])
		self.assertEqual(data["flags"], [])
		row = STORE.get_raw("Crop Observation", data["observation"])
		self.assertEqual((row["observation_type"], row["block"]), ("Growth Stage", "B7"))

	def test_no_code_over_mcp_is_kept_and_flagged(self):
		data = self.stage(stage="tight cluster, just past")
		self.assertIn("No BBCH code", data["flags"][0])
		self.assertIn("tight cluster", data["flags"][0])
		self.assertIn("not a BBCH code", self.stage(bbch="petal-ish")["flags"][0])

	def test_backwards_is_flagged_not_refused(self):
		self.stage(bbch="65", observed_at=f"{day(-3)} 09:00:00")
		data = self.stage(bbch="57", observed_at=f"{day(-1)} 09:00:00")
		self.assertIn("Earlier than BBCH 65", data["flags"][0])

	def test_nothing_at_all_is_refused(self):
		self.assertIn("BBCH code", self.tool_error("record_growth_stage", {"block": "B7"}))


class TheTimeline(StageTestCase):
	def test_oldest_first_with_the_current_stage_and_its_age(self):
		self.stage(bbch="53", observed_at=f"{day(-10)} 09:00:00")
		self.stage(bbch="55", observed_at=f"{day(-3)} 09:00:00")
		data = self.tool_data("get_stage_timeline", {"block": "B7"})
		self.assertEqual([e["bbch"] for e in data["entries"]], ["53", "55"])
		self.assertEqual((data["current"]["bbch"], data["current"]["age_days"]), ("55", 3))
		self.assertFalse(data["stale"])


class ThePrompt(StageTestCase):
	def task_on_b7(self):
		task = self.claimed(task_name="Thin B7", task_type="Maintenance")
		STORE.get_raw("Farm Task", task).update(location_doctype="Field", location="B7")
		return task

	def test_the_start_asks_when_no_stage_is_on_record_and_not_when_it_is_fresh(self):
		task = self.task_on_b7()
		started = self.tool_data("start_farm_task", {"task": task, "worker_id": "EMP-001"})
		self.assertIn("What stage is B7 at", started["stage_prompt"]["question"]["en"])
		self.assertEqual(started["stage_prompt"]["moment"], "task_start")
		self.stage(bbch="55")
		self.assertIsNone(growth_stage.prompt({"location_doctype": "Field", "location": "B7"}, "end_of_day"))

	def test_a_week_old_stage_is_asked_again(self):
		self.stage(bbch="55", observed_at=f"{day(-9)} 09:00:00")
		ask = growth_stage.prompt({"location_doctype": "Field", "location": "B7"}, "end_of_day")
		self.assertEqual(ask["last_bbch"], "55")
		self.assertIn("Última: BBCH 55", ask["question"]["es"])

	def test_a_task_off_a_block_is_never_asked(self):
		self.assertIsNone(growth_stage.prompt({"location_doctype": "Housing Unit", "location": "C1"}, "task_start"))

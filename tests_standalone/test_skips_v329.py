# SPDX-License-Identifier: MIT
"""v0.329.0 — "Can't take this": a step the template allows to be skipped is met by a skip with a reason.

Tim, after the insurance-photo field test: "some photos aren't hard requirements — if one can't be taken, the worker
needs a way to log that." Required / Skippable with reason / Optional per checklist step, on the template.
"""

import json

import frappe

from erpnext_mcp import task_progress
from erpnext_mcp.tools import dispatch

from .harness import STORE
from .test_task_templates import A_PHOTO, TemplateTestCase

SIDES = ["FRONT photo taken", "BACK photo taken", "LEFT side photo taken", "RIGHT side photo taken"]


class SkipCase(TemplateTestCase):
	def insurance_template(self, requirement="Skippable with reason", follow_up=False):
		return self.a_template(
			template_name="Insurance photo set — 4 sides", task_type="Inspection",
			evidence_required={"photos": True},
			checklist=[{"item_name": side, "evidence_type": "Photo", "requirement": requirement,
			            "example_hint": "Stand back far enough for the whole wall.",
			            "follow_up_on_skip": follow_up} for side in SIDES])

	def started(self, **template):
		self.insurance_template(**template)
		task = self.a_task_from("Insurance photo set — 4 sides")["name"]
		self.tool_data("claim_farm_task", {"task": task, "worker_id": "EMP-001", "worker_name": "Ana"})
		self.tool_data("start_farm_task", {"task": task, "worker_id": "EMP-001"})
		return task

	def items(self, task):
		return {i["item_name"]: i for i in json.loads(STORE.get_raw("Farm Task", task)["checklist_status"])["items"]}

	def assignment(self, task):
		return frappe.db.get_value("Farm Task Assignment", {"task": task}, "name")


class TheTemplateSaysHowStrict(SkipCase):
	def test_requirement_hint_and_follow_up_are_snapshotted(self):
		task = self.started(follow_up=True)
		front = self.items(task)["FRONT photo taken"]
		self.assertEqual(front["requirement"], "Skippable with reason")
		self.assertTrue(front["required"], "an older reader still treats it as one to do")
		self.assertEqual(front["example_hint"], "Stand back far enough for the whole wall.")
		self.assertTrue(front["follow_up_on_skip"])

	def test_a_misspelt_requirement_is_refused(self):
		self.assertIn("requirement is one of", self.tool_error("create_farm_task_template", {
			"template_name": "X", "task_type": "Inspection", "evidence_required": {"photos": True},
			"checklist": [{"item_name": "A", "requirement": "maybe"}]}))

	def test_update_takes_them_too(self):
		self.insurance_template(requirement="Required")
		self.tool_data("update_farm_task_template", {"template": "Insurance photo set — 4 sides", "checklist": [
			{"item_name": side, "evidence_type": "Photo", "requirement": "skippable"} for side in SIDES]})
		task = self.a_task_from("Insurance photo set — 4 sides")["name"]
		self.assertEqual(self.items(task)["BACK photo taken"]["requirement"], "Skippable with reason")


class ASkipWithAReasonMeetsTheStep(SkipCase):
	def test_complete_with_three_photos_and_one_skip(self):
		task = self.started()
		checklist = SIDES[:3] + [{"item_name": SIDES[3], "skipped": True, "skip_reason": "equipment_in_way",
		                          "skip_note": "Tractor parked against the wall"}]
		data = self.tool_data("complete_farm_task", {"task": task, "worker_id": "EMP-001",
		                                             "evidence_files": list(A_PHOTO), "checklist": checklist})
		self.assertEqual([s["item_name"] for s in data["skipped_steps"]], [SIDES[3]])
		right = self.items(task)[SIDES[3]]
		self.assertTrue(right["skipped"])
		self.assertFalse(right["done"])
		self.assertEqual(right["skip_reason"], "equipment_in_way")

	def test_a_required_step_cannot_be_skipped(self):
		task = self.started(requirement="Required")
		message = self.tool_error("complete_farm_task", {"task": task, "worker_id": "EMP-001",
		                                                 "evidence_files": list(A_PHOTO), "checklist": SIDES[:3] + [
			{"item_name": SIDES[3], "skipped": True, "skip_reason": "unsafe"}]})
		self.assertIn("is Required and cannot be skipped", message)

	def test_a_skip_says_why(self):
		task = self.started()
		self.assertIn("skip_reason is one of", self.tool_error("complete_farm_task", {
			"task": task, "worker_id": "EMP-001", "evidence_files": list(A_PHOTO),
			"checklist": [{"item_name": SIDES[0], "skipped": True}]}))
		self.assertIn("needs a note", self.tool_error("complete_farm_task", {
			"task": task, "worker_id": "EMP-001", "evidence_files": list(A_PHOTO),
			"checklist": [{"item_name": SIDES[0], "skipped": True, "skip_reason": "other"}]}))

	def test_a_follow_up_task_is_raised_once(self):
		task = self.started(follow_up=True)
		data = self.tool_data("complete_farm_task", {"task": task, "worker_id": "EMP-001",
		                                             "evidence_files": list(A_PHOTO), "checklist": SIDES[:3] + [
			{"item_name": SIDES[3], "skipped": True, "skip_reason": "blocked"}]})
		follow = data["follow_up_tasks"][0]
		row = STORE.get_raw("Farm Task", follow["task"])
		self.assertIn("RIGHT side photo taken", row["task_name"])
		self.assertIn(f"Follow-up of {task}", row["notes"])
		self.assertEqual(json.loads(row["checklist_status"])["items"][0]["requirement"], "Required")
		self.assertEqual(dispatch._skip_follow_ups(dispatch.task_row(task), [self.items(task)[SIDES[3]]])[0].get(
			"existing"), True)


class ThePhoneSkipsOffline(SkipCase):
	def test_a_skip_piece_with_a_photo_of_the_obstruction(self):
		task = self.started()
		out = task_progress.save(task, self.assignment(task), [
			{"client_item_id": "s1", "kind": "skip", "item_name": SIDES[1], "skip_reason": "not_on_asset",
			 "photo": {"file_url": "/files/no-back-wall.jpg"}}], worker="EMP-001")
		self.assertEqual(out["results"][0]["status"], "saved")
		back = self.items(task)[SIDES[1]]
		self.assertTrue(back["skipped"])
		self.assertEqual(back["skip_reason"], "not_on_asset")
		described = out["progress"]["checklist"]
		back_row = next(r for r in described if r["item_name"] == SIDES[1])
		self.assertTrue(back_row["met"])
		self.assertEqual(back_row["requirement"], "Skippable with reason")
		self.assertEqual(out["progress"]["evidence"][0]["checklist_item"], SIDES[1])

	def test_a_tick_after_a_skip_clears_the_skip(self):
		task = self.started()
		task_progress.save(task, self.assignment(task), [
			{"client_item_id": "s1", "kind": "skip", "item_name": SIDES[0], "skip_reason": "weather_light"},
			{"client_item_id": "t1", "kind": "tick", "item_name": SIDES[0]}], worker="EMP-001")
		front = self.items(task)[SIDES[0]]
		self.assertTrue(front["done"])
		self.assertNotIn("skipped", front)

	def test_get_farm_task_shows_the_skip(self):
		task = self.started()
		task_progress.save(task, self.assignment(task), [
			{"client_item_id": "s1", "kind": "skip", "item_name": SIDES[2], "skip_reason": "unsafe",
			 "skip_note": "Wasp nest"}], worker="EMP-001")
		checklist = self.tool_data("get_farm_task", {"task": task})["checklist"]
		left = next(i for i in checklist if i["item_name"] == SIDES[2])
		self.assertEqual((left["skip_reason"], left["skip_note"]), ("unsafe", "Wasp nest"))

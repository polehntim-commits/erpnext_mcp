# SPDX-License-Identifier: MIT
"""v0.198.0 — starting work, the hour meter, and what a photo knows.

OML App Feedback AFB-2026-00022, and FT-2026-09-00004 read back: an inspection
of wind machine 40-WM-SE raised from its asset screen. Contract:
`docs/design/task_workflow.md`.
"""

import frappe

from erpnext_mcp.api import shape
from erpnext_mcp.tools import dispatch

from .fixtures import MAIN
from .harness import STORE
from .test_dispatch import ALL_ON as DISPATCH_ON
from .test_dispatch import DispatchTestCase
from .test_field_reports import WORKER

ALL_ON = {
	**DISPATCH_ON,
	**{
		f"allow_{name}": 1
		for name in ("register_asset", "report_field_task", "report_asset_issue", "get_engine_hours_summary")
	},
}

WIND_MACHINE = "40-WM-SE"
SHED = "40-SH-01"


def a_photo_file(name, file_name):
	STORE.seed(
		"File",
		[{"name": name, "file_name": file_name, "file_url": f"/private/files/{file_name}", "is_private": 1}],
	)
	return name


class WorkflowTestCase(DispatchTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON)
		STORE.singles["System Settings"] = {"time_zone": "America/Los_Angeles"}
		self.tool_data(
			"register_asset", {"name": WIND_MACHINE, "asset_type": "Wind Machine", "company": MAIN}
		)

	def a_report(self, **extra):
		return self.tool_data(
			"report_field_task",
			{
				"reported_by": WORKER,
				"task_type": "Inspection",
				"description": "Routine walk\n\nTake pictures of the wind machine and document hours",
				"asset": WIND_MACHINE,
				"company": MAIN,
				**extra,
			},
		)

	def claimed_report(self):
		task = self.a_report()["name"]
		self.tool_data("claim_farm_task", {"task": task, "worker_id": "EMP-001", "worker_name": "Ana"})
		return task

	def finish(self, task, **extra):
		payload = {
			"task": task,
			"worker_id": "EMP-001",
			"evidence_files": [{"file_url": "/files/after.jpg", "evidence_type": "Photo"}],
			"findings_text": "Looks operable",
		}
		payload.update(extra)
		return self.tool_data("complete_farm_task", payload)


# ── 1. starting work needs no photo ─────────────────────────────────────────
class StartingWorkNeedsNoPhoto(WorkflowTestCase):
	def test_an_inspection_is_raised_from_the_asset_screen_without_a_photo(self):
		data = self.a_report()
		self.assertIsNone(data["report_photo"])
		self.assertTrue(data["evidence_required"]["photos"], "the photo is asked for at completion")

	def test_report_asset_issue_takes_no_photo_either(self):
		data = self.tool_data(
			"report_asset_issue", {"asset_name": WIND_MACHINE, "reported_by": WORKER, "description": "Noisy"}
		)
		self.assertEqual(data["asset"], WIND_MACHINE)

	def test_a_photo_that_is_sent_is_still_checked_and_kept(self):
		self.assertIn(
			"no File", self.tool_error("report_field_task", {"reported_by": WORKER, "photo_file_token": "X"})
		)
		photo = a_photo_file("b12f99c64b", "scan-report.jpg")
		self.assertEqual(self.a_report(photo_file_token=photo)["report_photo"], photo)


# ── 2. a task reads back as it was stored ───────────────────────────────────
class ATaskReadsBackAsStored(WorkflowTestCase):
	def test_origin_and_reporter_come_back_from_get_farm_task(self):
		"""FT-2026-09-00004 is stored field_reported by HR-EMP-00001 and read back
		as compliance_rule with no reporter, because the read never selected them."""
		task = self.a_report()["name"]
		read = self.tool_data("get_farm_task", {"task": task})
		self.assertEqual(read["origin"], "field_reported")
		self.assertEqual(read["reported_by"], WORKER)
		self.assertIsNotNone(read["reported_at"])

	def test_the_phone_shape_carries_them_too(self):
		task = self.a_report()["name"]
		phone = shape.task(dispatch._describe_task(dispatch.task_row(task)))
		self.assertEqual(phone["origin"], "field_reported")
		self.assertEqual(phone["reported_by"], WORKER)
		self.assertTrue(phone["asset_hours"]["has_hour_meter"])

	def test_a_task_raised_by_an_asset_screen_action_is_field_reported(self):
		data = dispatch.create_farm_task(
			{
				"task_name": "Stock in",
				"task_type": "Other",
				"company": MAIN,
				"evidence_required": {"findings_text": True},
				"reported_by": WORKER,
			},
			origin=dispatch.ORIGIN_FIELD_REPORTED,
		).data
		row = STORE.get_raw("Farm Task", data["name"])
		self.assertEqual(row["origin"], "field_reported")
		self.assertEqual(row["reported_by"], WORKER)

	def test_an_mcp_create_keeps_its_old_origin(self):
		task = self.a_task()["name"]
		self.assertEqual(self.tool_data("get_farm_task", {"task": task})["origin"], "compliance_rule")
		self.assertIsNone(self.tool_data("get_farm_task", {"task": task})["reported_by"])


# ── 3. the hour meter ───────────────────────────────────────────────────────
class TheHourMeterIsRead(WorkflowTestCase):
	def test_a_report_on_a_metered_asset_asks_for_hours(self):
		self.assertTrue(self.a_report()["evidence_required"]["hours"])

	def test_the_completion_is_refused_without_the_reading(self):
		task = self.claimed_report()
		message = self.tool_error(
			"complete_farm_task",
			{
				"task": task,
				"worker_id": "EMP-001",
				"evidence_files": [{"file_url": "/files/a.jpg"}],
				"findings_text": "ok",
			},
		)
		self.assertIn("hours:", message)
		self.assertIn("hours_reading", message)

	def test_the_reading_is_filed_through_the_engine_hours_path(self):
		task = self.claimed_report()
		answer = self.finish(task, hours_reading="1234.5")
		self.assertTrue(answer["hours_reading"]["recorded"])
		logs = [row for row in STORE.rows("Asset State Log") if row.get("action") == "log_hours"]
		self.assertEqual(len(logs), 1)
		self.assertEqual(logs[0]["engine_hours"], 1234.5)
		self.assertIn(task, logs[0]["notes"])
		asset = STORE.get_raw("Asset Register", WIND_MACHINE)
		self.assertEqual(asset["current_hours"], 1234.5)
		self.assertIsNotNone(asset["hours_updated_at"])
		self.assertEqual(answer["task"]["asset_hours"]["current_hours"], 1234.5)

	def test_a_lower_reading_files_the_work_and_says_the_reading_was_not_kept(self):
		first = self.claimed_report()
		self.finish(first, hours_reading=1234.5)
		second = self.claimed_report()
		answer = self.finish(second, hours_reading=123.4)
		self.assertFalse(answer["hours_reading"]["recorded"])
		self.assertIn("completion was filed", answer["hours_reading"]["reason"])
		self.assertEqual(STORE.get_raw("Asset Register", WIND_MACHINE)["current_hours"], 1234.5)
		reset = self.claimed_report()
		self.assertTrue(
			self.finish(reset, hours_reading=12.0, allow_meter_reset=True)["hours_reading"]["recorded"]
		)

	def test_a_reading_that_is_not_a_number_is_refused_before_anything_is_written(self):
		task = self.claimed_report()
		message = self.tool_error(
			"complete_farm_task",
			{"task": task, "worker_id": "EMP-001", "findings_text": "", "hours_reading": "lots"},
		)
		self.assertIn("hours_reading must be the number", message)
		self.assertFalse(STORE.rows("Asset State Log"))

	def test_hours_on_an_asset_with_no_meter_is_ignored(self):
		self.tool_data("register_asset", {"name": SHED, "asset_type": "Storage", "company": MAIN})
		task = self.tool_data(
			"report_field_task", {"reported_by": WORKER, "asset": SHED, "company": MAIN, "description": "x"}
		)["name"]
		self.assertNotIn("hours", self.tool_data("get_farm_task", {"task": task})["evidence_required"])
		self.tool_data("claim_farm_task", {"task": task, "worker_id": "EMP-001", "worker_name": "Ana"})
		answer = self.finish(task, hours_reading=5)
		self.assertFalse(answer["hours_reading"]["recorded"])
		self.assertIn("no hour meter", answer["hours_reading"]["reason"])

	def test_a_wind_machine_now_has_an_hours_summary(self):
		task = self.claimed_report()
		self.finish(task, hours_reading=40)
		self.assertTrue(self.tool_data("get_engine_hours_summary", {"asset_name": WIND_MACHINE})["metered"])


# ── 4. what a photo knows ───────────────────────────────────────────────────
class WhatAPhotoKnows(WorkflowTestCase):
	def test_phase_time_and_place_are_kept(self):
		task = self.claimed_report()
		before = a_photo_file("4fa19fbde9", f"{task}_photo_before_307F.jpg")
		after = a_photo_file("07322400f9", f"{task}_photo_after_28D1.jpg")
		answer = self.finish(
			task,
			hours_reading=1,
			evidence_files=[
				# An old build: nothing but the name.
				{"file_token": before, "caption": f"{task}_photo_before_307F.jpg", "kind": "photo"},
				# A new build: the phase, the instant and the fix.
				{
					"file_token": after,
					"kind": "photo",
					"phase": "after",
					"captured_at": "2026-09-27T23:58:40.000Z",
					"latitude": 45.5152,
					"longitude": -122.6784,
				},
			],
		)
		assignment = STORE.get_raw("Farm Task Assignment", answer["assignment"]["name"])
		files = {row["file"]: row for row in assignment["evidence_files"]}
		self.assertEqual(files[before]["phase"], "before", "read off the file name")
		self.assertEqual(files[after]["phase"], "after")
		self.assertEqual(str(files[after]["captured_on"]), "2026-09-27 16:58:40", "in the site's zone")
		self.assertAlmostEqual(files[after]["gps_latitude"], 45.5152)
		self.assertEqual(assignment["farm_location_gps"], "45.5152000,-122.6784000", "from the photo")

	def test_an_explicit_location_still_wins(self):
		task = self.claimed_report()
		answer = self.finish(
			task,
			hours_reading=1,
			farm_location_gps="Pump house",
			evidence_files=[{"file_url": "/files/a.jpg", "latitude": 1.0, "longitude": 2.0}],
		)
		self.assertEqual(
			STORE.get_raw("Farm Task Assignment", answer["assignment"]["name"])["farm_location_gps"],
			"Pump house",
		)

	def test_an_unreadable_stamp_does_not_fail_the_completion(self):
		task = self.claimed_report()
		self.finish(
			task, hours_reading=1, evidence_files=[{"file_url": "/files/a.jpg", "captured_at": "yesterday"}]
		)
		self.assertTrue(frappe.db.exists("Farm Task", task))


class BothReadersAgreeOnOrigin(WorkflowTestCase):
	"""On OML, `get_asset_detail` showed FT-2026-09-00004 and -00005 as
	field_reported and `get_farm_task` said compliance_rule for both. The rows
	were right; `get_farm_task` never selected the column and printed its
	fallback. The two readers must say the same thing, and it must be the row."""

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON, allow_get_asset_detail=1)

	def test_the_asset_screen_and_the_task_read_the_same_origin(self):
		task = self.a_report()["name"]
		stored = STORE.get_raw("Farm Task", task)["origin"]
		listed = {
			row["name"]: row
			for row in self.tool_data("get_asset_detail", {"asset_name": WIND_MACHINE})["open_tasks"]
		}
		self.assertEqual(stored, "field_reported")
		self.assertEqual(listed[task]["origin"], stored)
		self.assertEqual(self.tool_data("get_farm_task", {"task": task})["origin"], stored)

	def test_completion_does_not_change_the_origin(self):
		task = self.claimed_report()
		self.finish(task, hours_reading=10)
		self.assertEqual(STORE.get_raw("Farm Task", task)["origin"], "field_reported")
		self.assertEqual(self.tool_data("get_farm_task", {"task": task})["origin"], "field_reported")

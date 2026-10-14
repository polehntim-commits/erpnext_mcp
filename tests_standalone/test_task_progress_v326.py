# SPDX-License-Identifier: MIT
"""v0.326.0 — partial progress saves on its own; a paused job can be finished; a refused Complete keeps its evidence.

AFB-2026-00035 (insurance photos, 2026-10-13): five tasks done offline were refused at Complete — four as "Paused"
(the next task's start reached the server first and auto-paused the finished one), one for four unticked checklist
items — and because the completion was the only call carrying the photos, the server held none of them.
"""

import json

import frappe

from erpnext_mcp import task_progress
from erpnext_mcp.api import guard
from erpnext_mcp.tools import dispatch

from .harness import STORE
from .test_dispatch import A_PHOTO, ALL_ON, DispatchTestCase

FOUR = ["FRONT photo taken", "BACK photo taken", "LEFT side photo taken", "RIGHT side photo taken"]
PHOTOS = {"photos": True}


def photo(cid, item="", name=None):
	entry = {"client_item_id": cid, "kind": "photo", "file_url": f"/files/{name or cid}.jpg", "phase": "after",
	         "captured_at": "2026-10-13T09:53:24.000-07:00", "latitude": 45.58, "longitude": -121.22}
	if item:
		entry["checklist_item"] = item
	return entry


class ProgressCase(DispatchTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON, allow_pause_farm_task=1, allow_resume_farm_task=1)

	def insurance_task(self, worker="EMP-001", start=True):
		name = self.claimed(worker, evidence_required=dict(PHOTOS), task_name="Insurance photos (4 sides)")
		frappe.db.set_value("Farm Task", name, "checklist_status", json.dumps(
			{"items": [{"item_name": item, "required": True, "done": False} for item in FOUR]}))
		STORE.commit()
		if start:
			self.tool_data("start_farm_task", {"task": name, "worker_id": worker})
		return name

	def assignment(self, task):
		return frappe.db.get_value("Farm Task Assignment", {"task": task}, "name")

	def save(self, task, items, worker="EMP-001"):
		return task_progress.save(task, self.assignment(task), items, worker=worker)

	def evidence(self, task=None):
		rows = []
		for row in STORE.rows("Farm Task Assignment"):
			if task is None or row.get("task") == task:
				rows.extend(row.get("evidence_files") or [])
		return rows

	def ticks(self, task):
		raw = STORE.get_raw("Farm Task", task)["checklist_status"]
		return {i["item_name"]: bool(i.get("done")) for i in json.loads(raw)["items"]}


class EachPieceSavesOnItsOwn(ProgressCase):
	def test_a_photo_on_a_step_is_filed_and_ticks_the_step(self):
		task = self.insurance_task()
		out = self.save(task, [photo("p1", "FRONT photo taken")])
		self.assertEqual(out["results"][0]["status"], "saved")
		self.assertTrue(self.ticks(task)["FRONT photo taken"])
		self.assertFalse(self.ticks(task)["BACK photo taken"])
		rows = self.evidence()
		self.assertEqual([(r["client_item_id"], r["checklist_item"]) for r in rows], [("p1", "FRONT photo taken")])

	def test_a_retry_is_a_duplicate_not_a_second_photo(self):
		task = self.insurance_task()
		self.save(task, [photo("p1", "FRONT photo taken")])
		again = self.save(task, [photo("p1", "FRONT photo taken")])
		self.assertEqual(again["results"][0]["status"], "duplicate")
		self.assertEqual(len(self.evidence()), 1)

	def test_one_bad_item_never_refuses_the_batch(self):
		task = self.insurance_task()
		out = self.save(task, [{"client_item_id": "t1", "kind": "tick", "item_name": "No such step"},
		                       {"client_item_id": "t2", "kind": "tick", "item_name": "BACK photo taken"},
		                       {"kind": "tick", "item_name": "LEFT side photo taken"}])
		self.assertEqual([r["status"] for r in out["results"]], ["refused", "saved", "refused"])
		self.assertIn("no item called", out["results"][0]["reason"])
		self.assertTrue(self.ticks(task)["BACK photo taken"])

	def test_findings_gps_and_answers_are_drafts_latest_wins(self):
		task = self.insurance_task()
		self.save(task, [{"client_item_id": "f2", "kind": "findings", "text": "Cleaned up", "at": "2026-10-13 09:50:00"},
		                 {"client_item_id": "f1", "kind": "findings", "text": "older", "at": "2026-10-13 09:40:00"},
		                 {"client_item_id": "g1", "kind": "gps", "latitude": 45.5819, "longitude": -121.2278},
		                 {"client_item_id": "a1", "kind": "answers", "answers": {"damage": "none"}}])
		progress = task_progress.describe(self.assignment(task))
		self.assertEqual(progress["findings_draft"], "Cleaned up")
		self.assertEqual(progress["gps"], "45.5819,-121.2278")
		self.assertEqual(progress["draft_answers"], {"damage": "none"})

	def test_progress_saves_on_a_paused_job(self):
		task = self.insurance_task()
		self.tool_data("pause_farm_task", {"task": task, "worker_id": "EMP-001"})
		self.assertEqual(self.save(task, [photo("p1")])["results"][0]["status"], "saved")

	def test_not_somebody_elses_job(self):
		task = self.insurance_task()
		with self.assertRaises(task_progress.ProgressError):
			self.save(task, [photo("p1")], worker="EMP-002")


class APausedJobCanBeFinished(ProgressCase):
	def test_the_next_start_auto_pauses_the_finished_job_and_complete_still_lands(self):
		first = self.insurance_task()
		second = self.claimed("EMP-001", task_name="Insurance photos — shop", evidence_required=dict(PHOTOS))
		self.tool_data("start_farm_task", {"task": second, "worker_id": "EMP-001"})
		self.assertEqual(STORE.get_raw("Farm Task Assignment", self.assignment(first))["state"], "Paused")
		self.tool_data("complete_farm_task", {"task": first, "worker_id": "EMP-001", "evidence_files": list(A_PHOTO),
		                                      "checklist": FOUR, "findings_text": "Got them"})
		row = STORE.get_raw("Farm Task Assignment", self.assignment(first))
		self.assertEqual(row["state"], "Completed")
		self.assertIsNone(row.get("paused_at"))

	def test_the_clock_stops_at_the_finish_not_at_the_late_pause(self):
		task = self.insurance_task()
		doc = frappe.get_doc("Farm Task Assignment", self.assignment(task))
		doc.time_segments = []
		doc.append("time_segments", {"started_at": "2026-10-13 09:11:45", "ended_at": "2026-10-13 09:16:12",
		                             "minutes": 4, "ended_by": "auto_pause"})
		doc.state = "Paused"
		doc.paused_at = "2026-10-13 09:16:12"
		doc.auto_paused = 1
		doc.save(ignore_permissions=True)
		STORE.commit()
		self.tool_data("complete_farm_task", {"task": task, "worker_id": "EMP-001", "evidence_files": list(A_PHOTO),
		                                      "checklist": FOUR, "completed_at": "2026-10-13 09:14:33"})
		row = STORE.get_raw("Farm Task Assignment", self.assignment(task))
		self.assertEqual(row["actual_duration_minutes"], 3)


class ARefusedCompleteKeepsEverything(ProgressCase):
	def test_missing_steps_are_named_as_data_and_the_photos_are_kept(self):
		task = self.insurance_task()
		args = {"task": task, "worker_id": "EMP-001", "findings_text": "Cleaning up",
		        "evidence_files": [{"file_url": "/files/a.jpg", "client_item_id": "c1"},
		                           {"file_url": "/files/b.jpg", "client_item_id": "c2"}]}
		with self.assertRaises(dispatch.ToolError) as caught:
			dispatch.complete_farm_task(dict(args))
		detail = caught.exception.detail
		self.assertEqual(detail["missing"], FOUR)
		self.assertEqual(detail["kept"]["evidence"], 2)
		self.assertEqual(detail["progress"]["evidence_count"], 2)
		self.assertEqual(STORE.get_raw("Farm Task Assignment", self.assignment(task))["state"], "In-Progress")
		# Ticking the steps afterwards finishes it with the photos already on file, filed once.
		self.save(task, [{"client_item_id": f"t{i}", "kind": "tick", "item_name": item} for i, item in enumerate(FOUR)])
		self.tool_data("complete_farm_task", args)
		self.assertEqual(len(self.evidence()), 2)
		self.assertEqual(STORE.get_raw("Farm Task Assignment", self.assignment(task))["findings_text"], "Cleaning up")

	def test_saved_progress_meets_the_contract_without_resending(self):
		task = self.insurance_task()
		self.save(task, [photo(f"p{i}", item) for i, item in enumerate(FOUR)]
		          + [{"client_item_id": "f", "kind": "findings", "text": "All four sides"}])
		self.tool_data("complete_farm_task", {"task": task, "worker_id": "EMP-001"})
		row = STORE.get_raw("Farm Task Assignment", self.assignment(task))
		self.assertEqual(row["state"], "Completed")
		self.assertEqual(row["findings_text"], "All four sides")
		self.assertEqual(len(self.evidence()), 4)

	def test_a_photo_tagged_at_complete_ticks_its_step(self):
		task = self.insurance_task()
		self.tool_data("complete_farm_task", {
			"task": task, "worker_id": "EMP-001",
			"evidence_files": [{"file_url": f"/files/{i}.jpg", "checklist_item": item} for i, item in enumerate(FOUR)]})
		self.assertTrue(all(self.ticks(task).values()))
		tagged = sorted(r["checklist_item"] for r in self.evidence())
		self.assertEqual(tagged, sorted(FOUR))

	def test_the_phone_reads_the_missing_list_beside_the_message(self):
		exc = dispatch._missing("FT-1 cannot be completed", ["FRONT photo taken"])
		frappe.local.response = {}
		guard._stamp_error(exc, "error.validation.failed")
		self.assertEqual(frappe.local.response["error_detail"]["missing"], ["FRONT photo taken"])


class TheDeskSeesPartialWork(ProgressCase):
	def test_get_farm_task_shows_progress_on_an_open_job(self):
		task = self.insurance_task()
		self.save(task, [photo("p1", "FRONT photo taken")])
		data = self.tool_data("get_farm_task", {"task": task})
		progress = data["assignments"][0]["progress"]
		self.assertEqual(progress["checklist_done"], 1)
		self.assertEqual(progress["evidence"][0]["checklist_item"], "FRONT photo taken")
		self.assertEqual(data["assignments"][0]["evidence"][0]["client_item_id"], "p1")


class FarmOps066PhotosMeetPhotoSteps(ProgressCase):
	"""What 0.66 sends: a before photo, four untagged after photos, no checklist. The retry button must drain."""

	def photo_task(self):
		task = self.insurance_task()
		frappe.db.set_value("Farm Task", task, "checklist_status", json.dumps(
			{"items": [{"item_name": item, "required": True, "done": False, "evidence_type": "Photo"} for item in FOUR]}))
		STORE.commit()
		return task

	def as_066(self, task, afters=4):
		files = [{"file_url": "/files/before.jpg", "phase": "before", "captured_at": "2026-10-13 09:11:00"}]
		files += [{"file_url": f"/files/after{i}.jpg", "phase": "after", "captured_at": f"2026-10-13 09:1{2 + i}:00"}
		          for i in range(afters)]
		return {"task": task, "worker_id": "EMP-001", "evidence_files": files, "findings_text": "Got them"}

	def test_untagged_after_photos_tick_the_photo_steps_in_order(self):
		task = self.photo_task()
		data = self.tool_data("complete_farm_task", self.as_066(task))
		self.assertEqual([m["step"] for m in data["photo_matches"]], FOUR)
		self.assertEqual(data["photo_matches"][0]["file"], "/files/after0.jpg")
		self.assertTrue(all(self.ticks(task).values()))
		tagged = {r.get("file_url"): r.get("checklist_item") for r in self.evidence(task)}
		self.assertIsNone(tagged["/files/before.jpg"])
		self.assertEqual(tagged["/files/after3.jpg"], "RIGHT side photo taken")

	def test_too_few_photos_match_nothing_and_say_what_is_missing(self):
		task = self.photo_task()
		with self.assertRaises(dispatch.ToolError) as caught:
			dispatch.complete_farm_task(self.as_066(task, afters=3))
		self.assertEqual(caught.exception.detail["missing"], FOUR)
		self.assertEqual(caught.exception.detail["kept"]["evidence"], 4)

	def test_a_step_that_is_not_a_photo_step_is_never_auto_ticked(self):
		task = self.insurance_task()  # evidence_type absent: "None"
		with self.assertRaises(dispatch.ToolError):
			dispatch.complete_farm_task(self.as_066(task))

	def test_paused_and_untagged_together_is_tims_case(self):
		first = self.photo_task()
		second = self.claimed("EMP-001", task_name="Shop", evidence_required=dict(PHOTOS))
		self.tool_data("start_farm_task", {"task": second, "worker_id": "EMP-001"})
		self.tool_data("complete_farm_task", self.as_066(first))
		self.assertEqual(STORE.get_raw("Farm Task Assignment", self.assignment(first))["state"], "Completed")


class LateEvidenceIsKept(ProgressCase):
	def test_a_completion_for_a_rejected_assignment_keeps_its_photos_and_settles(self):
		task = self.insurance_task()
		name = self.assignment(task)
		self.tool_data("reject_farm_task", {"task": task, "worker_id": "EMP-001", "reason": "ladder broken"})
		data = self.tool_data("complete_farm_task", {
			"task": task, "worker_id": "EMP-001", "assignment": name, "accept_stale_evidence": True,
			"evidence_files": [{"file_url": "/files/x.jpg"}, {"file_url": "/files/y.jpg"}]})
		self.assertEqual(data["settled"], "evidence_only")
		self.assertEqual(data["evidence_kept"], 2)
		self.assertEqual(data["kept_on"], name)
		self.assertIn("Rejected", data["mismatch"])
		self.assertEqual(STORE.get_raw("Farm Task Assignment", name)["state"], "Rejected")
		again = self.tool_data("complete_farm_task", {
			"task": task, "worker_id": "EMP-001", "assignment": name, "accept_stale_evidence": True,
			"evidence_files": [{"file_url": "/files/x.jpg"}]})
		self.assertEqual(again["evidence_kept"], 0, "a retry files nothing twice")

	def test_an_mcp_caller_still_gets_the_refusal(self):
		task = self.insurance_task()
		name = self.assignment(task)
		self.tool_data("reject_farm_task", {"task": task, "worker_id": "EMP-001", "reason": "ladder broken"})
		self.assertIn("cannot be completed", self.tool_error("complete_farm_task", {
			"task": task, "worker_id": "EMP-001", "assignment": name, "evidence_files": list(A_PHOTO)}))

	def test_a_refused_phone_write_is_an_error_log_row_once(self):
		task_progress._RECENT_REFUSALS.clear()
		before = len(STORE.errors)
		task_progress.log_sync_refusal("complete_task_via_mobile", "uuid-1", "FTA-1 is Paused")
		task_progress.log_sync_refusal("complete_task_via_mobile", "uuid-1", "FTA-1 is Paused")
		rows = STORE.errors[before:]
		self.assertEqual(len(rows), 1)
		self.assertIn("uuid-1", rows[0]["message"])
		self.assertIn("FarmOps sync refused", rows[0]["title"])


class TheOrphansAreRelinked(ProgressCase):
	def test_unattached_task_photos_go_to_the_latest_assignment_once(self):
		from erpnext_mcp.patches import link_orphan_task_photos

		task = self.insurance_task()
		STORE.seed("File", [
			{"name": "f1", "file_name": f"{task}_photo_before_A.jpg", "file_url": "/private/files/a.jpg"},
			{"name": "f2", "file_name": f"{task}_photo_after_B.jpg", "file_url": "/private/files/b.jpg"},
			{"name": "f3", "file_name": "FT-2099-01-00001_photo_C.jpg", "file_url": "/private/files/c.jpg"},
			{"name": "f4", "file_name": "receipt.jpg", "file_url": "/private/files/d.jpg"},
		])
		STORE.commit()
		self.assertEqual(link_orphan_task_photos.execute()["linked"], 2)
		self.assertEqual(STORE.get_raw("File", "f1")["attached_to_name"], self.assignment(task))
		self.assertFalse(STORE.get_raw("File", "f4").get("attached_to_name"))
		self.assertEqual(link_orphan_task_photos.execute()["linked"], 0)


class AWorkersPartialTicksAreRespected(FarmOps066PhotosMeetPhotoSteps):
	def test_one_photo_step_ticked_means_no_matching(self):
		task = self.photo_task()
		args = self.as_066(task)
		args["checklist"] = ["FRONT photo taken"]
		with self.assertRaises(dispatch.ToolError) as caught:
			dispatch.complete_farm_task(args)
		self.assertEqual(caught.exception.detail["missing"], FOUR[1:])

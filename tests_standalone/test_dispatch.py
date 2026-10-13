# SPDX-License-Identifier: MIT
"""Farm Task Dispatch — Sprint 8 Features A and C.

FIVE CLAIMS, AND EVERY CLASS IN THIS FILE IS ONE OF THEM.

1. **A TASK CANNOT EXIST WITHOUT AN EVIDENCE CONTRACT.** `EvidenceIsMandatory`
   proves there is no path to a task somebody can close by saying they did it —
   not a missing contract, not an empty one, not one whose every requirement is
   false, and not one with a misspelt key, because `{"photo": true}` asks for
   nothing while looking exactly like a photograph requirement.

2. **THE CONTRACT IS ENFORCED AT COMPLETION.** `TheContractIsEnforced` walks each
   of the four requirements separately and shows the refusal names the one that is
   short. The findings_text case is the subtle one and gets three tests: passing
   an EMPTY STRING satisfies it — a clean inspection is a positive statement —
   while leaving the argument out records that nobody was asked.

3. **COMPLETING WRITES THE COMPLIANCE RECORD, NOT A STATUS.** `TheLoopCloses` runs
   the whole thing end to end on a camp: alerts fire, tasks are generated, a
   worker claims and completes one, a Housing Inspection appears with the
   photographs on it, the unit's inspection date moves, and the alert
   auto-dismisses on the next sweep with nobody having touched it. That test is
   the release.

4. **THE POOL CANNOT BE HOARDED, AND DISPATCHED WORK CANNOT BE SELF-PICKED.**
   `TheClaimRules`. Three at once, and completing one frees a slot in the same
   instant — a hoarding limit, not a productivity one.

5. **REJECTION IS A FIRST-CLASS STATE.** `RejectionIsAnAnswer`. The reason is
   mandatory, the task goes back to the pool, and the rejected assignment STAYS —
   it is the proof somebody was sent, went, and could not do it, which answers an
   auditor in a way an absence never does.

`GeneratingFromAlerts` covers Feature C, and the test that matters most there is
idempotence: running it twice must not put two people in front of the same cabin.
"""

import contextlib
import io
import json

import frappe

from erpnext_mcp import dashboard, records
from erpnext_mcp.erpnext_mcp.doctype.farm_task.farm_task import MAX_CONCURRENT_CLAIMS
from erpnext_mcp.tools import dispatch

from .fixtures import MAIN, V12TestCase
from .harness import INSTALLED_DOCTYPES, META, STORE

ALL_ON = {
	f"allow_{name}": 1
	for name in (
		"create_parcel",
		"create_field",
		"create_irrigation_zone",
		"create_housing_unit",
		"get_housing_unit",
		"create_farm_task",
		"assign_farm_task",
		"claim_farm_task",
		"start_farm_task",
		"complete_farm_task",
		"reject_farm_task",
		"generate_tasks_from_compliance_alerts",
		"list_available_tasks",
		"list_dispatched_tasks",
		"list_dispatch_board",
		"get_farm_task",
		"refresh_compliance_alerts",
		"get_compliance_calendar",
		"list_housing_inspections",
		"get_housing_inspection",
		"create_housing_inspection",
		"list_water_tests",
	)
}

TODAY = "2026-07-24"

#: The commonest contract in the whole system: a habitability walk.
WALK = {"photos": True, "signature": True, "findings_text": True}

#: One photograph, filed as a URL so no File record is needed.
A_PHOTO = [{"file_url": "/files/north-wall.jpg", "evidence_type": "Photo", "caption": "north wall"}]


class DispatchTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON)

	def a_camp(self, unit_name="MC-Cabin-01", **overrides):
		if not STORE.rows("Parcel"):
			self.tool_data(
				"create_parcel", {"owning_entity": MAIN, "parcel_name": "Mill Creek", "acreage": 131.43}
			)
		payload = {
			"parcel": "Mill Creek",
			"unit_name": unit_name,
			"unit_type": "Cabin",
			"square_footage": 400,
			"capacity": 4,
			"fsma_worker_facility": True,
		}
		payload.update(overrides)
		return self.tool_data("create_housing_unit", payload)["name"]

	def a_task(self, **overrides):
		payload = {
			"task_name": "Habitability walk — MC-Cabin-01",
			"task_type": "Inspection",
			"evidence_required": dict(WALK),
			"skill_required": "camp_maintenance",
		}
		payload.update(overrides)
		return self.tool_data("create_farm_task", payload)

	def claimed(self, worker="EMP-001", **overrides):
		task = self.a_task(**overrides)
		self.tool_data("claim_farm_task", {"task": task["name"], "worker_id": worker, "worker_name": "Ana"})
		return task["name"]

	def complete(self, task, worker="EMP-001", **overrides):
		payload = {
			"task": task,
			"worker_id": worker,
			"evidence_files": list(A_PHOTO),
			"signature_file": "/files/sig.png",
			"findings_text": "",
			"completion_narrative": "walked it",
		}
		payload.update(overrides)
		return self.tool_data("complete_farm_task", payload)


# ── 1 ───────────────────────────────────────────────────────────────────────
class EvidenceIsMandatory(DispatchTestCase):
	"""There is no path to a task somebody can close with a tick in a box."""

	def test_a_task_with_no_contract_is_refused(self):
		message = self.tool_error("create_farm_task", {"task_name": "Walk it", "task_type": "Inspection"})
		self.assertIn("evidence_required is required", message)
		self.assertIn("tick in a box", message)
		self.assertIn("Nothing was created", message)

	def test_an_empty_contract_is_refused(self):
		message = self.tool_error(
			"create_farm_task",
			{"task_name": "Walk it", "task_type": "Inspection", "evidence_required": {}},
		)
		self.assertIn("Evidence Required is empty", message)
		self.assertIn("tick in a box", message)

	def test_a_contract_with_everything_false_is_refused(self):
		"""Well-formed JSON that asks for nothing is the same as asking nothing."""
		message = self.tool_error(
			"create_farm_task",
			{
				"task_name": "Walk it",
				"task_type": "Inspection",
				"evidence_required": {"photos": False, "signature": False},
			},
		)
		self.assertIn("every requirement switched off", message)

	def test_a_misspelt_key_is_refused_rather_than_ignored(self):
		"""`{"photo": true}` asks for nothing and looks like it asks for something."""
		message = self.tool_error(
			"create_farm_task",
			{"task_name": "Walk it", "task_type": "Inspection", "evidence_required": {"photo": True}},
		)
		self.assertIn("'photo'", message)
		self.assertIn("nothing checks", message)

	def test_a_valid_contract_is_stored_and_read_back_as_sentences(self):
		task = self.a_task()
		self.assertEqual(task["evidence_required"], WALK)
		self.assertIn("photograph", task["evidence_required_summary"])
		self.assertIn("signature", task["evidence_required_summary"])

	def test_the_contract_survives_a_json_string(self):
		task = self.a_task(evidence_required=json.dumps({"photos": True}))
		self.assertEqual(task["evidence_required"], {"photos": True})

	def test_nonsense_json_is_refused(self):
		message = self.tool_error(
			"create_farm_task",
			{"task_name": "Walk it", "task_type": "Inspection", "evidence_required": "{not json"},
		)
		self.assertIn("not valid JSON", message)


class WhatCreateRefuses(DispatchTestCase):
	def test_a_creates_record_this_site_does_not_have_is_refused(self):
		"""A task promising a record nobody can write is a promise that fails in
		front of a worker stood in a cabin."""
		message = self.tool_error(
			"create_farm_task",
			{
				"task_name": "Walk it",
				"task_type": "Inspection",
				"evidence_required": dict(WALK),
				"creates_record": "Invented Inspection",
			},
		)
		self.assertIn("does not have that DocType", message)
		self.assertIn("Housing Inspection", message)

	def test_a_location_that_does_not_exist_is_refused(self):
		message = self.tool_error(
			"create_farm_task",
			{
				"task_name": "Walk it",
				"task_type": "Inspection",
				"evidence_required": dict(WALK),
				"location_doctype": "Housing Unit",
				"location": "MC-Cabin-99 - MC",
			},
		)
		self.assertIn("no Housing Unit called", message)

	def test_a_location_without_its_doctype_is_refused(self):
		message = self.tool_error(
			"create_farm_task",
			{
				"task_name": "Walk it",
				"task_type": "Inspection",
				"evidence_required": dict(WALK),
				"location": "MC-Cabin-01 - MC",
			},
		)
		self.assertIn("no location_doctype", message)

	def test_a_second_task_for_one_alert_is_refused(self):
		"""One alert is one job. Two people sent to walk the same cabin is what
		the source link exists to prevent."""
		self.a_camp()
		self.tool_data("refresh_compliance_alerts", {"today": TODAY})
		alert = STORE.rows("Compliance Alert")[0]["name"]
		self.a_task(source_alert=alert)
		message = self.tool_error(
			"create_farm_task",
			{
				"task_name": "Walk it again",
				"task_type": "Inspection",
				"evidence_required": dict(WALK),
				"source_alert": alert,
			},
		)
		self.assertIn("already answers alert", message)

	def test_a_dispatched_task_with_nobody_on_it_warns(self):
		task = self.a_task(dispatch_mode="Dispatched")
		self.assertTrue(any("no worker can claim it" in note for note in task["warnings"]))

	def test_a_task_with_no_location_warns_but_is_allowed(self):
		"""Desk work is legitimate — a certificate renewal happens at a desk."""
		task = self.a_task(task_type="Compliance-Audit")
		self.assertEqual(task["state"], "Available")
		self.assertTrue(any("names no location" in note for note in task["warnings"]))

	def test_a_draft_is_not_in_the_pool(self):
		self.a_task(draft=True)
		self.assertEqual(self.tool_data("list_available_tasks", {})["count"], 0)


# ── 2 ───────────────────────────────────────────────────────────────────────
class TheContractIsEnforced(DispatchTestCase):
	"""The refusal the whole doctype exists for, one requirement at a time."""

	def test_missing_photos_are_named(self):
		task = self.claimed()
		message = self.tool_error(
			"complete_farm_task",
			{"task": task, "worker_id": "EMP-001", "signature_file": "/files/s.png", "findings_text": ""},
		)
		self.assertIn("photos:", message)
		self.assertIn("no compliance record was written", message)

	def test_a_missing_signature_is_named(self):
		task = self.claimed()
		message = self.tool_error(
			"complete_farm_task",
			{"task": task, "worker_id": "EMP-001", "evidence_files": list(A_PHOTO), "findings_text": ""},
		)
		self.assertIn("signature:", message)

	def test_a_signature_may_arrive_as_an_evidence_row(self):
		task = self.claimed()
		done = self.complete(
			task,
			signature_file="",
			evidence_files=[*A_PHOTO, {"file_url": "/files/s.png", "evidence_type": "Signature"}],
		)
		self.assertEqual(done["final_state"], "Completed")

	def test_omitting_findings_text_entirely_is_refused(self):
		"""Leaving the argument out records that nobody was asked."""
		task = self.claimed()
		message = self.tool_error(
			"complete_farm_task",
			{
				"task": task,
				"worker_id": "EMP-001",
				"evidence_files": list(A_PHOTO),
				"signature_file": "/files/s.png",
			},
		)
		self.assertIn("findings_text:", message)
		self.assertIn("PASS AN EMPTY STRING", message)

	def test_an_empty_findings_text_satisfies_it(self):
		"""A clean inspection is a positive statement, and this is how it is made."""
		task = self.claimed()
		done = self.complete(task, findings_text="")
		self.assertEqual(done["final_state"], "Completed")

	def test_a_witness_requirement_is_enforced(self):
		task = self.claimed(evidence_required={"photos": True, "witness": True})
		message = self.tool_error(
			"complete_farm_task",
			{"task": task, "worker_id": "EMP-001", "evidence_files": list(A_PHOTO)},
		)
		self.assertIn("witness:", message)
		self.assertIn("one person's word is not the standard", message)

	def test_every_shortfall_is_reported_at_once(self):
		task = self.claimed()
		message = self.tool_error("complete_farm_task", {"task": task, "worker_id": "EMP-001"})
		for requirement in ("photos:", "signature:", "findings_text:"):
			self.assertIn(requirement, message)

	def test_a_refused_completion_writes_nothing(self):
		task = self.claimed()
		self.tool_error("complete_farm_task", {"task": task, "worker_id": "EMP-001"})
		self.assertFalse(STORE.rows("Housing Inspection"))
		self.assertEqual(self.tool_data("get_farm_task", {"task": task})["state"], "Claimed")

	def test_a_completion_by_somebody_who_was_not_there_is_refused(self):
		"""Not a chain of custody — a rumour, and the first thing an auditor pulls."""
		task = self.claimed(worker="EMP-001")
		message = self.tool_error(
			"complete_farm_task",
			{
				"task": task,
				"worker_id": "EMP-002",
				"evidence_files": list(A_PHOTO),
				"signature_file": "/files/s.png",
				"findings_text": "",
			},
		)
		self.assertIn("it is a rumour", message)
		self.assertIn("Nothing was changed", message)

	def test_a_task_nobody_holds_cannot_be_completed(self):
		task = self.a_task()["name"]
		message = self.tool_error(
			"complete_farm_task", {"task": task, "worker_id": "EMP-001", "findings_text": ""}
		)
		self.assertIn("has nobody holding it", message)


# ── 3 ───────────────────────────────────────────────────────────────────────
class TheLoopCloses(DispatchTestCase):
	"""Sprint 7 could say fifty-four things were wrong. This is the half that fixes one."""

	def test_alert_to_task_to_record_to_auto_dismissal(self):
		unit = self.a_camp()
		first = self.tool_data("refresh_compliance_alerts", {"today": TODAY})
		self.assertEqual(first["created"], 2)

		generated = self.tool_data(
			"generate_tasks_from_compliance_alerts",
			{"company": MAIN, "alert_types": ["housing_inspection_overdue"]},
		)
		self.assertEqual(generated["created_count"], 1)
		task = generated["created"][0]["task"]

		self.tool_data("claim_farm_task", {"task": task, "worker_id": "EMP-001", "worker_name": "Ana"})
		self.tool_data("start_farm_task", {"task": task, "worker_id": "EMP-001"})
		done = self.complete(task)

		# A real Housing Inspection, with the photograph on it.
		self.assertEqual(done["produced_record_doctype"], "Housing Inspection")
		inspection = self.tool_data("get_housing_inspection", {"record": done["produced_record"]})
		self.assertEqual(inspection["unit"], unit)
		self.assertEqual(inspection["workflow_state"], records.RECORDED)
		self.assertEqual(inspection["evidence"][0]["caption"], "north wall")
		self.assertEqual(inspection["source_task"], task)

		# The register moved, which is the only honest way an alert goes away.
		self.assertEqual(
			self.tool_data("get_housing_unit", {"unit": unit})["last_habitability_inspection"], TODAY
		)

		# v0.64.0. THE COMPLETION ITSELF NOW ASKS THE RULE TO LOOK AGAIN, so the
		# dismissal lands here rather than on the next scheduled pass. The
		# mechanism did not change — the rule re-scanned and found its condition
		# untrue — only the moment did, which is why the assertion moved from the
		# second sweep's report onto the completion's own.
		asked = done["compliance_evaluation"]["rules_asked"]
		# The rule that RAISED the task, and the rules that READ the register it
		# wrote to. `housing_corrective_action_open` is the second kind and it
		# belongs here: this completion filed a Housing Inspection, which is
		# exactly the doctype that rule scans, so its answer may have moved too.
		# Nobody linked it to the task — the RECORD is the link.
		self.assertIn("housing_inspection_overdue", asked)
		self.assertIn("housing_corrective_action_open", asked)
		self.assertTrue(done["compliance_evaluation"]["auto_dismissed"])

		# And the scheduled sweep is now a no-op against this rule, because there
		# is nothing left for it to notice. A second dismissal of an already
		# dismissed alert would be the calendar double-counting its own work.
		second = self.tool_data("refresh_compliance_alerts", {"today": TODAY})
		self.assertFalse(
			[
				entry
				for entry in second["alerts"]
				if entry["alert_type"] == "housing_inspection_overdue"
				and entry["outcome"] == "auto_dismissed"
			]
		)
		self.assertIn("auto-dismissed", self.tool_data("get_farm_task", {"task": task})["loop_closed"])

	def test_nothing_in_the_completion_dismissed_an_alert_by_hand(self):
		"""The alert goes at completion now. THE SWEEP still decides, not the tool.

		v0.64.0 moved WHEN the calendar notices and deliberately did not move WHO
		decides. The distinction is the whole posture: a completion that set
		`dismissed` itself would be the app asserting a condition had resolved
		because somebody filed paperwork about it, which is the failure the
		auto-dismissal flag exists to make visible. So the alert IS dismissed
		here — and it carries `auto_dismissed`, with no dismissing user and no
		reason, which is exactly the shape `_auto_dismiss` writes and exactly the
		shape a hand dismissal cannot have.
		"""
		self.a_camp()
		self.tool_data("refresh_compliance_alerts", {"today": TODAY})
		generated = self.tool_data(
			"generate_tasks_from_compliance_alerts",
			{"company": MAIN, "alert_types": ["housing_inspection_overdue"]},
		)
		task = generated["created"][0]["task"]
		self.tool_data("claim_farm_task", {"task": task, "worker_id": "EMP-001"})
		done = self.complete(task)
		self.assertIn("only honest way", done["dismissal_note"])

		alert = STORE.get_raw("Compliance Alert", generated["created"][0]["alert"])
		self.assertTrue(int(alert["dismissed"]))
		self.assertTrue(int(alert["auto_dismissed"]))
		# The two columns a hand dismissal fills and an automatic one must not.
		self.assertFalse(alert.get("dismissed_by"))
		self.assertFalse(alert.get("dismissed_reason"))

	def test_a_completion_that_answers_no_rule_evaluates_nothing(self):
		"""No source alert and no produced record is nothing for a rule to re-read.

		The narrowing has to be able to come back EMPTY, or it is not a narrowing.
		A hand-raised task that files no record cannot have changed any rule's
		answer, and running the whole rule set on the off-chance would be this app
		doing a site's compliance sweep every time somebody ticked off a job.
		"""
		task = self.claimed()
		done = self.complete(task)
		self.assertIsNone(done["compliance_evaluation"])

	def test_a_hand_raised_walk_clears_the_alert_nobody_linked_it_to(self):
		"""v0.64.1. THE RECORD IS THE LINK — and it was not, for the three rules
		that matter most.

		The task here comes from NO alert: a foreman raised a habitability walk
		himself, which is the ordinary case on a site where nobody has run
		`generate_tasks_from_compliance_alerts` (a manual tool, default off). So
		the `source_alert` half of the narrowing has nothing to say, and the
		produced-record half is the whole of it.

		It used to find nothing. `housing_inspection_overdue` scans HOUSING UNIT
		and the completion produced a HOUSING INSPECTION, and the test was
		`produced_doctype in rule.requires` — so the only rule that matched was
		`housing_corrective_action_open`, which reads the new record to raise a
		NEW problem. The walk re-ran the rule that opens findings against it and
		never the rule whose alert asked for the walk, and the alert sat on the
		worker's phone until the hourly sweep.

		The register moves by write-back — recording an inspection advances the
		unit's `last_habitability_inspection` — so what a rule answers to is not
		readable off `requires`. `ALERT_TASK_MAP` already states it, and this is
		that table read backwards.
		"""
		unit = self.a_camp()
		raised = self.tool_data("refresh_compliance_alerts", {"today": TODAY})
		alerts = [
			entry["name"] for entry in raised["alerts"] if entry["alert_type"] == "housing_inspection_overdue"
		]
		self.assertEqual(len(alerts), 1)

		task = self.claimed(
			creates_record="Housing Inspection",
			location_doctype="Housing Unit",
			location=unit,
		)
		self.assertFalse(STORE.get_raw("Farm Task", task).get("source_alert"))
		done = self.complete(task)

		evaluation = done["compliance_evaluation"]
		self.assertIn("housing_inspection_overdue", evaluation["rules_asked"])
		self.assertEqual(evaluation["auto_dismissed"], alerts)
		self.assertTrue(int(STORE.get_raw("Compliance Alert", alerts[0])["auto_dismissed"]))

	def test_the_register_a_rule_reads_is_not_the_doctype_it_scans(self):
		"""The mapping under the fix, stated as the thing it is: a produced record
		names the rules that record discharges, and for all three of the field-work
		rules that is a DIFFERENT doctype from the one the rule scans."""
		from erpnext_mcp.tools.dispatch import _rules_reading_register

		self.assertEqual(_rules_reading_register("Housing Inspection"), ["housing_inspection_overdue"])
		self.assertEqual(_rules_reading_register("Detector Test"), ["housing_detector_test_stale"])
		# TWO rules are discharged through a Water Test, and both belong: the one
		# that says the block has not been sampled lately, and the one that says
		# the last sample failed and wants a clean one. A produced record answers
		# every rule whose work produces it, not the first.
		self.assertEqual(
			_rules_reading_register("Water Test"),
			["water_test_contamination", "water_test_stale"],
		)
		# A doctype no recipe produces names nothing, rather than everything.
		self.assertEqual(_rules_reading_register("Journal Entry"), [])
		self.assertEqual(_rules_reading_register(""), [])

	def test_a_finding_lands_the_task_in_awaiting_review(self):
		"""The work is done AND something needs a person. Both are true."""
		unit = self.a_camp()
		task = self.claimed(
			creates_record="Housing Inspection",
			location_doctype="Housing Unit",
			location=unit,
		)
		done = self.complete(task, findings_text="no smoke detector in the back bedroom")
		self.assertEqual(done["final_state"], "Awaiting-Review")
		self.assertEqual(done["produced_record_state"], records.CORRECTIVE_ACTION_REQUIRED)
		self.assertIn("two different facts", done["review_note"])
		self.assertEqual(
			self.tool_data("get_housing_unit", {"unit": unit})["last_habitability_inspection"], TODAY
		)

	def test_a_task_that_produces_nothing_just_completes(self):
		task = self.claimed(task_type="Compliance-Audit")
		done = self.complete(task)
		self.assertEqual(done["final_state"], "Completed")
		self.assertIsNone(done["produced_record"])

	def test_record_data_reaches_the_produced_record(self):
		unit = self.a_camp()
		task = self.claimed(
			task_type="Test",
			evidence_required={"photos": True, "findings_text": True},
			creates_record="Detector Test",
			location_doctype="Housing Unit",
			location=unit,
		)
		done = self.complete(task, signature_file="", record_data={"co_detector_result": "Fail"})
		self.assertEqual(done["final_state"], "Awaiting-Review")
		test = STORE.get_raw("Detector Test", done["produced_record"])
		self.assertEqual(test["co_detector_result"], "Fail")
		self.assertEqual(test["tester"], "EMP-001")

	def test_a_water_task_on_a_block_asks_the_worker_which_zone(self):
		"""One block can have several zones, so this app will not guess."""
		self.a_camp()
		self.tool_data("create_field", {"parcel": "Mill Creek", "field_name": "YC3", "acreage": 10})
		self.tool_data(
			"create_irrigation_zone", {"field": "YC3", "zone_name": "YC3-Zone2", "water_source": "creek"}
		)
		task = self.claimed(
			task_type="Water-Sampling",
			evidence_required={"photos": True, "findings_text": True},
			creates_record="Water Test",
			location_doctype="Field",
			location="YC3 - MC",
		)
		message = self.tool_error(
			"complete_farm_task",
			{
				"task": task,
				"worker_id": "EMP-001",
				"evidence_files": list(A_PHOTO),
				"findings_text": "",
			},
		)
		self.assertIn("source is required", message)

		done = self.complete(
			task,
			signature_file="",
			record_data={"source": "YC3-Zone2 - MC", "coliform_result": "Absent"},
		)
		self.assertEqual(done["produced_record_doctype"], "Water Test")
		self.assertEqual(STORE.get_raw("Water Test", done["produced_record"])["source"], "YC3-Zone2 - MC")


# ── 4 ───────────────────────────────────────────────────────────────────────
class TheClaimRules(DispatchTestCase):
	def test_three_at_once_and_no_more(self):
		tasks = [self.a_task(task_name=f"Walk {index}")["name"] for index in range(4)]
		for task in tasks[:MAX_CONCURRENT_CLAIMS]:
			self.tool_data("claim_farm_task", {"task": task, "worker_id": "EMP-001"})
		message = self.tool_error(
			"claim_farm_task", {"task": tasks[MAX_CONCURRENT_CLAIMS], "worker_id": "EMP-001"}
		)
		self.assertIn(f"limit is {MAX_CONCURRENT_CLAIMS}", message)
		self.assertIn("hoarding limit and not a productivity one", message)

	def test_completing_one_frees_a_slot_in_the_same_instant(self):
		tasks = [self.a_task(task_name=f"Walk {index}")["name"] for index in range(4)]
		for task in tasks[:MAX_CONCURRENT_CLAIMS]:
			self.tool_data("claim_farm_task", {"task": task, "worker_id": "EMP-001"})
		self.complete(tasks[0])
		claimed = self.tool_data(
			"claim_farm_task", {"task": tasks[MAX_CONCURRENT_CLAIMS], "worker_id": "EMP-001"}
		)
		self.assertEqual(claimed["concurrent_claims"], MAX_CONCURRENT_CLAIMS)

	def test_the_limit_is_per_worker(self):
		tasks = [self.a_task(task_name=f"Walk {index}")["name"] for index in range(4)]
		for task in tasks[:MAX_CONCURRENT_CLAIMS]:
			self.tool_data("claim_farm_task", {"task": task, "worker_id": "EMP-001"})
		other = self.tool_data(
			"claim_farm_task", {"task": tasks[MAX_CONCURRENT_CLAIMS], "worker_id": "EMP-002"}
		)
		self.assertEqual(other["concurrent_claims"], 1)

	def test_dispatched_work_cannot_be_self_picked(self):
		task = self.a_task(dispatch_mode="Dispatched", assigned_to="")["name"]
		message = self.tool_error("claim_farm_task", {"task": task, "worker_id": "EMP-001"})
		self.assertIn("somebody has to be SENT to it by name", message)
		self.assertIn("wrong person's name on a regulated record", message)

	def test_dispatched_work_is_absent_from_the_pool_entirely(self):
		self.a_task(dispatch_mode="Dispatched")
		self.a_task(task_name="Self-pick one", dispatch_mode="Self-pick")
		pool = self.tool_data("list_available_tasks", {})
		self.assertEqual([task["task_name"] for task in pool["tasks"]], ["Self-pick one"])
		self.assertIn("deliberately absent from the pool", pool["note"])

	def test_a_task_somebody_else_holds_cannot_be_claimed(self):
		task = self.claimed(worker="EMP-001")
		message = self.tool_error("claim_farm_task", {"task": task, "worker_id": "EMP-002"})
		self.assertIn("both believing it is theirs", message)

	def test_a_draft_cannot_be_claimed(self):
		task = self.a_task(draft=True)["name"]
		message = self.tool_error("claim_farm_task", {"task": task, "worker_id": "EMP-001"})
		self.assertIn("still a Draft", message)

	def test_the_pool_tells_a_worker_what_they_will_need(self):
		self.a_task()
		pool = self.tool_data("list_available_tasks", {"worker_id": "EMP-001"})
		self.assertTrue(pool["may_claim"])
		self.assertEqual(pool["claims_remaining"], MAX_CONCURRENT_CLAIMS)

	def test_claiming_returns_the_evidence_the_worker_will_need(self):
		task = self.a_task()["name"]
		claimed = self.tool_data("claim_farm_task", {"task": task, "worker_id": "EMP-001"})
		self.assertIn("photograph", claimed["evidence_you_will_need"])

	def test_the_pool_filters_on_skill_and_location(self):
		unit = self.a_camp()
		self.a_task(skill_required="applicator_license")
		self.a_task(
			task_name="Camp walk",
			skill_required="camp_maintenance",
			location_doctype="Housing Unit",
			location=unit,
		)
		by_skill = self.tool_data("list_available_tasks", {"skill": "camp_maintenance"})
		self.assertEqual([task["task_name"] for task in by_skill["tasks"]], ["Camp walk"])
		by_place = self.tool_data("list_available_tasks", {"location": unit})
		self.assertEqual(by_place["count"], 1)

	def test_the_pool_is_ordered_worst_urgency_first(self):
		self.a_task(task_name="Low one", urgency="Low")
		self.a_task(task_name="Critical one", urgency="Critical")
		self.a_task(task_name="Normal one", urgency="Normal")
		pool = self.tool_data("list_available_tasks", {})
		self.assertEqual(
			[task["task_name"] for task in pool["tasks"]], ["Critical one", "Normal one", "Low one"]
		)


class StartingIsTheClockIn(DispatchTestCase):
	def test_starting_twice_is_refused(self):
		task = self.claimed()
		self.tool_data("start_farm_task", {"task": task, "worker_id": "EMP-001"})
		message = self.tool_error("start_farm_task", {"task": task, "worker_id": "EMP-001"})
		self.assertIn("shorten the hour actually spent", message)

	def test_the_elapsed_time_is_computed_from_the_two_stamps(self):
		task = self.claimed()
		self.tool_data(
			"start_farm_task", {"task": task, "worker_id": "EMP-001", "started_at": "2026-07-24 10:00:00"}
		)
		done = self.complete(task, completed_at="2026-07-24 10:25:00")
		self.assertEqual(done["assignment"]["actual_duration_minutes"], 25)

	def test_a_worker_can_give_the_duration_themselves(self):
		"""A phone in a pocket all morning is not a clock."""
		task = self.claimed()
		done = self.complete(task, actual_duration_minutes=45)
		self.assertEqual(done["assignment"]["actual_duration_minutes"], 45)

	def test_starting_somebody_elses_task_is_refused(self):
		task = self.claimed(worker="EMP-001")
		message = self.tool_error("start_farm_task", {"task": task, "worker_id": "EMP-002"})
		self.assertIn("is held by", message)


# ── 5 ───────────────────────────────────────────────────────────────────────
class RejectionIsAnAnswer(DispatchTestCase):
	def test_a_rejection_without_a_reason_is_refused(self):
		task = self.claimed()
		message = self.tool_error("reject_farm_task", {"task": task, "worker_id": "EMP-001", "reason": ""})
		self.assertIn("dispatch never followed up", message)

	def test_a_rejection_returns_the_task_to_the_pool_with_the_name_cleared(self):
		task = self.claimed()
		data = self.tool_data(
			"reject_farm_task",
			{"task": task, "worker_id": "EMP-001", "reason": "the ladder is broken"},
		)
		self.assertEqual(data["returned_to_state"], "Available")
		self.assertIsNone(data["task"]["assigned_to"])
		self.assertEqual(self.tool_data("list_available_tasks", {})["count"], 1)

	def test_the_rejected_assignment_stays_on_the_record(self):
		task = self.claimed()
		self.tool_data(
			"reject_farm_task",
			{"task": task, "worker_id": "EMP-001", "reason": "the ladder is broken"},
		)
		history = self.tool_data("get_farm_task", {"task": task})
		self.assertEqual(len(history["rejections"]), 1)
		self.assertEqual(history["rejections"][0]["rejection_reason"], "the ladder is broken")

	def test_somebody_else_can_then_take_it(self):
		task = self.claimed(worker="EMP-001")
		self.tool_data("reject_farm_task", {"task": task, "worker_id": "EMP-001", "reason": "no ladder"})
		self.tool_data("claim_farm_task", {"task": task, "worker_id": "EMP-002"})
		detail = self.tool_data("get_farm_task", {"task": task})
		self.assertEqual(detail["assignment_count"], 2)
		self.assertEqual(detail["live_assignment"]["assigned_to"], "EMP-002")

	def test_a_rejection_can_cancel_the_task_instead(self):
		task = self.claimed()
		data = self.tool_data(
			"reject_farm_task",
			{"task": task, "worker_id": "EMP-001", "reason": "the cabin was demolished", "cancel": True},
		)
		self.assertEqual(data["returned_to_state"], "Cancelled")


class DispatchingByName(DispatchTestCase):
	def test_a_foreman_can_send_somebody(self):
		task = self.a_task(dispatch_mode="Dispatched")["name"]
		data = self.tool_data("assign_farm_task", {"task": task, "assigned_to": "EMP-007"})
		self.assertEqual(data["state"], "Claimed")
		self.assertTrue(data["assignment"]["dispatched_by_foreman"])

	def test_a_self_pick_claim_is_not_marked_as_dispatched(self):
		"""The distinction is worth keeping: two different kinds of work."""
		task = self.claimed()
		detail = self.tool_data("get_farm_task", {"task": task})
		self.assertFalse(detail["live_assignment"]["dispatched_by_foreman"])

	def test_taking_work_off_somebody_needs_saying_so(self):
		task = self.claimed(worker="EMP-001")
		message = self.tool_error("assign_farm_task", {"task": task, "assigned_to": "EMP-002"})
		self.assertIn("pass reassign=true if you mean it", message)

	def test_reassigning_needs_a_reason_written_onto_their_assignment(self):
		task = self.claimed(worker="EMP-001")
		message = self.tool_error(
			"assign_farm_task", {"task": task, "assigned_to": "EMP-002", "reassign": True}
		)
		self.assertIn("needs a reason", message)

		data = self.tool_data(
			"assign_farm_task",
			{
				"task": task,
				"assigned_to": "EMP-002",
				"reassign": True,
				"reason": "EMP-001 is on the other camp all week",
			},
		)
		self.assertEqual(data["reassigned_from"], "Ana")
		history = self.tool_data("get_farm_task", {"task": task})
		self.assertIn("other camp all week", history["rejections"][0]["rejection_reason"])

	def test_finished_work_cannot_be_reassigned(self):
		task = self.claimed()
		self.complete(task)
		message = self.tool_error("assign_farm_task", {"task": task, "assigned_to": "EMP-002"})
		self.assertIn("rewrite history", message)

	def test_assigning_somebody_who_already_holds_it_is_refused(self):
		task = self.claimed(worker="EMP-001")
		message = self.tool_error(
			"assign_farm_task", {"task": task, "assigned_to": "EMP-001", "reassign": True}
		)
		self.assertIn("already held by", message)


class OneLiveAssignmentPerTask(DispatchTestCase):
	"""Two people in front of the same work is what a board exists to prevent."""

	def test_the_controller_refuses_a_second_live_assignment(self):
		import frappe

		task = self.claimed(worker="EMP-001")
		second = frappe.new_doc("Farm Task Assignment")
		second.task = task
		second.assigned_to = "EMP-002"
		second.state = "Claimed"
		with self.assertRaises(Exception) as caught:
			second.insert(ignore_permissions=True)
		self.assertIn("One live assignment per task", str(caught.exception))

	def test_a_completed_assignment_does_not_block_a_new_one(self):
		task = self.claimed(worker="EMP-001")
		self.complete(task)
		# The task is Completed, so nothing should claim it — but the assignment
		# itself is no longer 'live', which is the state the rule is about.
		from erpnext_mcp.erpnext_mcp.doctype.farm_task_assignment.farm_task_assignment import (
			live_assignment,
		)

		self.assertIsNone(live_assignment(task))


# ── the board ───────────────────────────────────────────────────────────────
class TheBoard(DispatchTestCase):
	def test_it_groups_by_state(self):
		self.a_task(task_name="Pooled")
		claimed = self.claimed(task_name="Held")
		self.tool_data("start_farm_task", {"task": claimed, "worker_id": "EMP-001"})
		board = self.tool_data("list_dispatch_board", {})
		self.assertEqual(board["by_state"]["Available"], 1)
		self.assertEqual(board["by_state"]["In-Progress"], 1)
		self.assertEqual([task["task_name"] for task in board["columns"]["Available"]], ["Pooled"])

	def test_closed_work_is_off_the_board_unless_asked_for(self):
		task = self.claimed()
		self.complete(task)
		self.assertEqual(self.tool_data("list_dispatch_board", {})["count"], 0)
		self.assertEqual(self.tool_data("list_dispatch_board", {"include_closed": True})["count"], 1)

	def test_a_state_filter_that_is_not_a_state_is_refused(self):
		message = self.tool_error("list_dispatch_board", {"state_filter": "Nearly Done"})
		self.assertIn("not a Farm Task state", message)

	def test_it_reports_how_much_of_the_board_came_from_the_calendar(self):
		"""The honest measure of whether the calendar drives work or is ignored."""
		self.a_camp()
		self.tool_data("refresh_compliance_alerts", {"today": TODAY})
		self.tool_data("generate_tasks_from_compliance_alerts", {"company": MAIN})
		self.a_task(task_name="Somebody's own idea", company=MAIN)
		board = self.tool_data("list_dispatch_board", {"company": MAIN})
		self.assertEqual(board["generated_from_alerts"], 2)
		self.assertEqual(board["count"], 3)

	def test_a_workers_own_list_is_theirs_alone(self):
		mine = self.claimed(task_name="Mine", worker="EMP-001")
		self.claimed(task_name="Theirs", worker="EMP-002")
		data = self.tool_data("list_dispatched_tasks", {"worker_id": "EMP-001"})
		self.assertEqual([entry["task"] for entry in data["assignments"]], [mine])
		self.assertEqual(data["holding_now"], 1)

	def test_finished_assignments_are_hidden_from_a_workers_list_by_default(self):
		task = self.claimed()
		self.complete(task)
		self.assertEqual(self.tool_data("list_dispatched_tasks", {"worker_id": "EMP-001"})["count"], 0)
		self.assertEqual(
			self.tool_data("list_dispatched_tasks", {"worker_id": "EMP-001", "include_finished": True})[
				"count"
			],
			1,
		)

	def test_get_farm_task_carries_the_evidence_filed_against_each_assignment(self):
		task = self.claimed()
		self.complete(task)
		detail = self.tool_data("get_farm_task", {"task": task})
		self.assertEqual(detail["assignments"][0]["evidence"][0]["caption"], "north wall")


# ── the Kanban ──────────────────────────────────────────────────────────────
class TheKanbanBoard(DispatchTestCase):
	"""No custom UI in this release, and none needed: Frappe's own Kanban writes
	the state field when a foreman drags a card."""

	def test_it_builds_a_board_over_the_state_field(self):
		report = dashboard.install_dispatch_board()
		self.assertTrue(report["available"])
		self.assertTrue(report["created"])
		board = STORE.get_raw("Kanban Board", "Farm Task Dispatch")
		self.assertEqual(board["reference_doctype"], "Farm Task")
		self.assertEqual(board["field_name"], "state")

	def test_every_state_gets_a_column_including_rejected(self):
		"""A board that hid rejections would hide the one thing a foreman must act on."""
		dashboard.install_dispatch_board()
		board = STORE.get_raw("Kanban Board", "Farm Task Dispatch")
		self.assertEqual(
			[column["column_name"] for column in board["columns"]],
			[
				"Draft",
				"Available",
				"Claimed",
				"In-Progress",
				"Paused",
				"On Hold",
				"Awaiting-Review",
				"Completed",
				"Rejected",
				"Cancelled",
				"Merged",
			],
		)

	def test_the_columns_match_the_doctypes_own_states(self):
		from erpnext_mcp.erpnext_mcp.doctype.farm_task.farm_task import STATES

		self.assertEqual(tuple(state for state, _colour in dashboard.DISPATCH_COLUMNS), STATES)

	def test_running_it_again_changes_nothing(self):
		dashboard.install_dispatch_board()
		before = len(STORE.rows("Kanban Board"))
		report = dashboard.install_dispatch_board()
		self.assertTrue(report["existed"])
		self.assertFalse(report["created"])
		self.assertEqual(len(STORE.rows("Kanban Board")), before)

	def test_an_operators_rearranged_board_is_left_alone(self):
		dashboard.install_dispatch_board()
		board = STORE.get_raw("Kanban Board", "Farm Task Dispatch")
		board["columns"] = board["columns"][:3]
		dashboard.install_dispatch_board()
		self.assertEqual(len(STORE.get_raw("Kanban Board", "Farm Task Dispatch")["columns"]), 3)

	def test_it_builds_the_landing_workspace_too(self):
		report = dashboard.install_dispatch_board()
		self.assertTrue(report["workspace_created"])
		self.assertEqual(report["workspace_route"], "/app/farm-task-dispatch")
		workspace = STORE.get_raw("Workspace", "Farm Task Dispatch")
		self.assertIn("Farm Task", [row["link_to"] for row in workspace["shortcuts"]])

	def test_the_board_is_named_the_name_the_route_documents(self):
		"""THE v0.16.0 REGRESSION, stated as plainly as it can be. The route is
		documented in the README, the tool catalogue and list_dispatch_board's own
		payload; a board under any other name is a board nobody finds."""
		report = dashboard.install_dispatch_board()
		self.assertEqual(report["board_name"], "Farm Task Dispatch")
		self.assertTrue(frappe.db.exists("Kanban Board", "Farm Task Dispatch"))
		self.assertEqual(report["route"], "/app/farm-task/view/kanban/Farm Task Dispatch")

	def test_a_site_without_the_kanban_doctype_is_told_rather_than_broken(self):
		from .harness import INSTALLED_DOCTYPES

		INSTALLED_DOCTYPES.discard("Kanban Board")
		report = dashboard.install_dispatch_board()
		self.assertFalse(report["available"])
		self.assertIn("list_dispatch_board", report["note"])
		self.assertFalse(report["failed"])

	def test_the_board_route_is_reported_by_the_tool_that_returns_the_same_columns(self):
		self.assertEqual(
			self.tool_data("list_dispatch_board", {})["kanban_route"],
			"/app/farm-task/view/kanban/Farm Task Dispatch",
		)

	def test_install_and_migrate_both_build_it(self):
		from erpnext_mcp import install

		install.after_migrate()
		self.assertTrue(STORE.get_raw("Kanban Board", "Farm Task Dispatch"))


class TheIndicatorPaletteIsNotAssumed(DispatchTestCase):
	"""WHY v0.16.1 EXISTS.

	`Kanban Board Column.indicator` is FRAPPE'S field, not this app's, and its
	option list has been spelled differently across the versions this app
	supports. v0.16.0 hardcoded `"gray"`, the site's options were capitalised,
	`doc.insert()` threw, `install.py` discarded the report, and the migration
	said nothing. Tim opened the documented route a week later and Frappe offered
	him a "New Kanban Board" dialog.

	The fix is not a better guess. It is not guessing: the options are read off
	the site and matched case-insensitively, and a value with no match is DROPPED
	rather than sent. These tests re-declare the field three incompatible ways and
	require a working board from all three.
	"""

	def repalette(self, options):
		META["Kanban Board Column"].get_field("indicator")["options"] = options

	def board(self):
		report = dashboard.install_dispatch_board()
		self.assertEqual(report["failed"], [], report["failed"])
		self.assertTrue(report["created"])
		return STORE.get_raw("Kanban Board", "Farm Task Dispatch")

	def test_capitalised_options_are_matched_and_stored_in_the_sites_own_casing(self):
		"""The exact shape that broke Tim's site."""
		self.repalette("Blue\nOrange\nRed\nGreen\nGray\nPurple\nYellow")
		self.assertEqual(
			[column["indicator"] for column in self.board()["columns"]],
			["Gray", "Blue", "Purple", "Orange", "Yellow", "Gray", "Red", "Green", "Red", "Gray", "Gray"],
		)

	def test_a_colour_this_sites_palette_lacks_is_dropped_and_its_column_survives(self):
		"""v0.79.0 added Paused in yellow, and a site whose Kanban palette predates
		yellow must still get the column — without it a board loses the state that
		says who is actually working."""
		self.repalette("Blue\nOrange\nRed\nGreen\nGray\nPurple")
		columns = self.board()["columns"]
		paused = next(column for column in columns if column["column_name"] == "Paused")
		self.assertFalse(paused.get("indicator"))
		self.assertEqual(len(columns), 11)  # v0.324.0: + On Hold

	def test_lowercase_options_are_matched_too(self):
		self.repalette("blue\norange\nred\ngreen\ngray\npurple")
		self.assertEqual(self.board()["columns"][0]["indicator"], "gray")

	def test_a_palette_this_app_has_never_heard_of_drops_the_colour_and_keeps_the_board(self):
		"""A column with no colour is cosmetic. A board that does not exist is not."""
		self.repalette("#4287f5\n#f54242\n#42f554")
		board = self.board()
		self.assertEqual(len(board["columns"]), 11)  # v0.324.0: + On Hold
		self.assertFalse(any(column.get("indicator") for column in board["columns"]))

	def test_a_select_with_no_options_at_all_takes_the_value_unchanged(self):
		"""A customised or runtime-populated Select is not policed by Frappe
		either, so refusing it here would invent a refusal the site does not make."""
		self.repalette("")
		self.assertEqual(self.board()["columns"][0]["indicator"], "gray")

	def test_the_double_now_polices_selects_which_is_what_was_missing(self):
		"""The suite passed 2864 tests through the bad value because it never
		looked at a Select. This asserts the double would now catch it."""
		self.repalette("Blue\nGray")
		doc = frappe.new_doc("Kanban Board")
		doc.kanban_board_name = "Hand Rolled"
		doc.reference_doctype = "Farm Task"
		doc.field_name = "state"
		doc.append("columns", {"column_name": "Draft", "indicator": "gray"})
		with self.assertRaises(Exception) as caught:
			doc.insert(ignore_permissions=True)
		self.assertIn("not a valid value", str(caught.exception))


class TheBoardSurvivesAFrappeThatRefusesTheColumns(DispatchTestCase):
	def refuse_columns(self):
		"""A Frappe whose column_name will not take our states, whatever we send."""
		META["Kanban Board Column"].get_field("column_name")["fieldtype"] = "Select"
		META["Kanban Board Column"].get_field("column_name")["options"] = "To Do\nDoing\nDone"

	def test_it_falls_back_to_a_board_with_no_columns(self):
		"""Frappe builds the columns from the distinct values of the field the
		first time somebody opens the board. Degrade; do not vanish."""
		self.refuse_columns()
		report = dashboard.install_dispatch_board()
		self.assertTrue(report["created"])
		self.assertEqual(report["columns_written"], 0)
		self.assertIn("WITHOUT its columns", report["note_columns"])
		self.assertTrue(frappe.db.exists("Kanban Board", "Farm Task Dispatch"))

	def test_the_reason_the_first_attempt_failed_is_kept(self):
		self.refuse_columns()
		report = dashboard.install_dispatch_board()
		self.assertIn("not a valid value", report["note_columns"])

	def test_a_total_failure_is_reported_rather_than_swallowed(self):
		META["Kanban Board"].get_field("field_name")["fieldtype"] = "Select"
		META["Kanban Board"].get_field("field_name")["options"] = "nothing_we_would_send"
		report = dashboard.install_dispatch_board()
		self.assertFalse(report["created"])
		self.assertTrue(report["failed"])
		self.assertIn("list_dispatch_board", report["failed"][0]["reason"])


class MigrateSaysWhatItCouldNotBuild(DispatchTestCase):
	"""THE ROOT CAUSE, AND THE MOST IMPORTANT CLASS IN THIS FILE.

	Both bugs v0.16.1 fixes were survivable. What made them ship was that
	`install.py` called an installer which cannot raise, and then discarded its
	report — so a failure printed nothing, `bench migrate` exited zero, and the
	first anybody knew was a missing page a week later.
	"""

	def migrate_output(self):
		from erpnext_mcp import install

		buffer = io.StringIO()
		with contextlib.redirect_stdout(buffer):
			install.after_migrate()
		return buffer.getvalue()

	def test_a_clean_migrate_says_nothing_about_the_board(self):
		self.assertNotIn("Farm Task Dispatch", self.migrate_output())

	def test_a_board_that_could_not_be_built_is_named_on_stdout(self):
		META["Kanban Board"].get_field("field_name")["fieldtype"] = "Select"
		META["Kanban Board"].get_field("field_name")["options"] = "nothing_we_would_send"
		output = self.migrate_output()
		self.assertIn("could not build Farm Task Dispatch", output)
		self.assertIn("list_dispatch_board", output)

	def test_the_command_center_reports_the_same_way(self):
		"""One helper, both dashboards — so the next silent installer is not one
		somebody has to remember to wire up."""
		from erpnext_mcp import dashboard as dashboard_module

		self.assertEqual(dashboard_module.install_command_center.__module__, "erpnext_mcp.dashboard")
		output = self.migrate_output()
		self.assertNotIn("could not build", output)


class TheWorkspaceHasSomethingOnIt(DispatchTestCase):
	"""THE SECOND v0.16.0 BUG, and a different misunderstanding from the first.

	A modern Frappe Workspace renders ONLY what its `content` block list names.
	The `shortcuts`, `links`, `number_cards` and `charts` child tables supply the
	data; `content` decides what appears. v0.16.0 wrote the child rows and then
	set `content` to `[]` — a page with a title and nothing else, which is exactly
	what Tim opened.
	"""

	def workspace(self):
		dashboard.install_dispatch_board()
		return STORE.get_raw("Workspace", "Farm Task Dispatch")

	def content(self):
		return json.loads(self.workspace()["content"])

	def test_the_content_is_not_empty(self):
		self.assertTrue(self.content())

	def test_every_child_row_is_named_by_a_block_that_renders_it(self):
		"""A shortcut row with no block is invisible; a block naming a row that is
		not there is a rendering error. They are written in one pass so neither
		can happen."""
		workspace = self.workspace()
		content = json.loads(workspace["content"])
		named = {
			block["data"].get("shortcut_name")
			or block["data"].get("number_card_name")
			or block["data"].get("chart_name")
			or block["data"].get("card_name")
			for block in content
			if block["type"] != "header"
		}
		for row in workspace["shortcuts"]:
			self.assertIn(row["label"], named)
		for row in workspace["number_cards"]:
			self.assertIn(row["number_card_name"], named)
		for row in workspace["charts"]:
			self.assertIn(row["chart_name"], named)

	def test_there_is_a_quick_add_button(self):
		"""The commonest thing a foreman does on this page is put work on the
		board, and making them open a list first to find New is the friction that
		ends with the job being shouted across a yard instead."""
		shortcuts = {row["label"]: row for row in self.workspace()["shortcuts"]}
		self.assertEqual(shortcuts["Raise a Task"]["doc_view"], "New")
		self.assertEqual(shortcuts["Raise a Task"]["link_to"], "Farm Task")

	def test_the_board_shortcut_points_at_the_board_that_was_just_built(self):
		shortcuts = {row["label"]: row for row in self.workspace()["shortcuts"]}
		self.assertEqual(shortcuts["Dispatch Board"]["kanban_board"], "Farm Task Dispatch")

	def test_the_number_cards_are_real_records_and_are_on_the_page(self):
		workspace = self.workspace()
		on_page = [row["number_card_name"] for row in workspace["number_cards"]]
		self.assertIn("Tasks in the Pool", on_page)
		self.assertIn("Open Critical Tasks", on_page)
		self.assertIn("Tasks Awaiting Review", on_page)
		for label in on_page:
			with self.subTest(card=label):
				self.assertTrue(frappe.db.exists("Number Card", label))

	def test_the_alert_provenance_pair_is_both_halves_or_neither(self):
		"""A Number Card counts one collection and cannot divide two, so the
		fraction that came from the calendar is shown as two counts side by side
		rather than as a percentage the card would have to invent."""
		on_page = [row["number_card_name"] for row in self.workspace()["number_cards"]]
		self.assertIn("Tasks Raised From Alerts", on_page)
		self.assertIn("Tasks Raised By Hand", on_page)

	def test_the_charts_cover_type_and_urgency(self):
		charts = [row["chart_name"] for row in self.workspace()["charts"]]
		self.assertEqual(charts, ["Farm Tasks by Type", "Farm Tasks by Urgency"])
		for name in charts:
			with self.subTest(chart=name):
				self.assertTrue(frappe.db.exists("Dashboard Chart", name))

	def test_the_link_cards_name_the_records_a_completion_writes(self):
		links = self.workspace()["links"]
		breaks = [row["label"] for row in links if row["type"] == "Card Break"]
		self.assertIn("Compliance Records", breaks)
		targets = [row["link_to"] for row in links if row["type"] == "Link"]
		for doctype in ("Housing Inspection", "Detector Test", "Water Test"):
			self.assertIn(doctype, targets)

	def test_an_empty_workspace_from_v0_16_0_is_repaired_on_the_next_migrate(self):
		"""THE UPGRADE PATH. Tim's site already has the blank page, and a plain
		existence check would have skipped it forever."""
		blank = frappe.new_doc("Workspace")
		blank.name = "Farm Task Dispatch"
		blank.flags.name_set = True
		blank.title = "Farm Task Dispatch"
		blank.label = "Farm Task Dispatch"
		blank.content = "[]"
		blank.insert(ignore_permissions=True)

		report = dashboard.install_dispatch_board()
		self.assertTrue(report["workspace_filled"])
		self.assertFalse(report["workspace_created"])
		self.assertTrue(json.loads(STORE.get_raw("Workspace", "Farm Task Dispatch")["content"]))

	def test_a_page_somebody_arranged_is_never_touched(self):
		dashboard.install_dispatch_board()
		workspace = frappe.get_doc("Workspace", "Farm Task Dispatch")
		workspace.content = json.dumps(
			[{"id": "mine", "type": "header", "data": {"text": "Mine", "col": 12}}]
		)
		workspace.save(ignore_permissions=True)

		report = dashboard.install_dispatch_board()
		self.assertTrue(report["workspace_existed"])
		self.assertFalse(report["workspace_filled"])
		self.assertEqual(
			json.loads(STORE.get_raw("Workspace", "Farm Task Dispatch")["content"])[0]["id"], "mine"
		)

	def test_repairing_does_not_double_the_child_rows(self):
		dashboard.install_dispatch_board()
		before = len(STORE.get_raw("Workspace", "Farm Task Dispatch")["shortcuts"])
		frappe.db.set_value("Workspace", "Farm Task Dispatch", "content", "[]")
		dashboard.install_dispatch_board()
		self.assertEqual(len(STORE.get_raw("Workspace", "Farm Task Dispatch")["shortcuts"]), before)

	def test_a_site_with_no_workspace_doctype_is_told_rather_than_broken(self):
		INSTALLED_DOCTYPES.discard("Workspace")
		report = dashboard.install_dispatch_board()
		self.assertIn("no Workspace doctype", report["workspace_note"])
		self.assertFalse(report["failed"])
		self.assertTrue(report["created"])


# ── Feature C ───────────────────────────────────────────────────────────────
class GeneratingFromAlerts(DispatchTestCase):
	def a_camp_with_alerts(self, units=3):
		for index in range(1, units + 1):
			self.a_camp(f"MC-Cabin-{index:02d}")
		return self.tool_data("refresh_compliance_alerts", {"today": TODAY})

	def test_every_open_alert_becomes_one_task(self):
		self.a_camp_with_alerts(units=3)
		generated = self.tool_data("generate_tasks_from_compliance_alerts", {"company": MAIN})
		self.assertEqual(generated["created_count"], 6)
		self.assertEqual(
			generated["by_alert_type"],
			{"housing_detector_test_stale": 3, "housing_inspection_overdue": 3},
		)

	def test_running_it_twice_does_not_send_two_people_to_one_cabin(self):
		"""The property that makes this safe to run whenever somebody wonders."""
		self.a_camp_with_alerts(units=2)
		self.tool_data("generate_tasks_from_compliance_alerts", {"company": MAIN})
		again = self.tool_data("generate_tasks_from_compliance_alerts", {"company": MAIN})
		self.assertEqual(again["created_count"], 0)
		self.assertEqual(len(again["skipped_already_answered"]), 4)
		self.assertEqual(len(STORE.rows("Farm Task")), 4)

	def test_re_running_after_half_the_work_raises_only_what_is_left(self):
		self.a_camp_with_alerts(units=2)
		self.tool_data("generate_tasks_from_compliance_alerts", {"company": MAIN})
		# A third cabin arrives and its alerts fire.
		self.a_camp("MC-Cabin-03")
		self.tool_data("refresh_compliance_alerts", {"today": TODAY})
		again = self.tool_data("generate_tasks_from_compliance_alerts", {"company": MAIN})
		self.assertEqual(again["created_count"], 2)

	def test_a_dry_run_writes_nothing_and_says_what_it_would_do(self):
		self.a_camp_with_alerts(units=1)
		report = self.tool_data("generate_tasks_from_compliance_alerts", {"company": MAIN, "dry_run": True})
		self.assertEqual(report["created_count"], 2)
		self.assertFalse(STORE.rows("Farm Task"))
		self.assertIn("DRY RUN", report["note"])
		self.assertTrue(all(entry["task"] is None for entry in report["created"]))

	def test_the_recipe_decides_the_shape_of_the_work(self):
		self.a_camp_with_alerts(units=1)
		report = self.tool_data(
			"generate_tasks_from_compliance_alerts",
			{"company": MAIN, "alert_types": ["housing_inspection_overdue"]},
		)
		task = self.tool_data("get_farm_task", {"task": report["created"][0]["task"]})
		self.assertEqual(task["task_type"], "Inspection")
		self.assertEqual(task["creates_record"], "Housing Inspection")
		self.assertEqual(task["skill_required"], "camp_maintenance")
		self.assertEqual(task["dispatch_mode"], "Self-pick")
		self.assertEqual(task["evidence_required"], WALK)
		self.assertEqual(task["location_doctype"], "Housing Unit")

	def test_severity_becomes_urgency_without_making_everything_critical(self):
		"""A board where everything is Critical is a board nobody reads."""
		self.a_camp_with_alerts(units=1)
		report = self.tool_data("generate_tasks_from_compliance_alerts", {"company": MAIN})
		urgencies = {entry["severity"]: entry["urgency"] for entry in report["created"]}
		self.assertEqual(urgencies, {"Warning": "Normal"})

	def test_a_critical_alert_becomes_high_rather_than_critical(self):
		from erpnext_mcp.tools.dispatch import SEVERITY_URGENCY

		self.assertEqual(SEVERITY_URGENCY["Critical"], "High")
		self.assertEqual(SEVERITY_URGENCY["Info"], "Low")

	def test_every_generated_task_carries_the_alert_that_produced_it(self):
		self.a_camp_with_alerts(units=1)
		report = self.tool_data("generate_tasks_from_compliance_alerts", {"company": MAIN})
		for entry in report["created"]:
			with self.subTest(alert=entry["alert"]):
				self.assertEqual(STORE.get_raw("Farm Task", entry["task"])["source_alert"], entry["alert"])

	def test_an_alert_type_filter_narrows_it(self):
		self.a_camp_with_alerts(units=2)
		report = self.tool_data(
			"generate_tasks_from_compliance_alerts",
			{"company": MAIN, "alert_types": ["housing_detector_test_stale"]},
		)
		self.assertEqual(report["created_count"], 2)
		self.assertEqual(list(report["by_alert_type"]), ["housing_detector_test_stale"])

	def test_an_unknown_alert_type_is_refused_with_the_mapped_ones_named(self):
		message = self.tool_error(
			"generate_tasks_from_compliance_alerts", {"company": MAIN, "alert_types": ["invented_rule"]}
		)
		self.assertIn("'invented_rule'", message)
		self.assertIn("housing_inspection_overdue", message)

	def test_an_unmapped_alert_type_is_reported_rather_than_faked(self):
		"""A task with a made-up contract produces a record nobody can rely on."""
		import erpnext_mcp.tools.dispatch as dispatch_module

		self.a_camp_with_alerts(units=1)
		recipe = dispatch_module.ALERT_TASK_MAP.pop("housing_inspection_overdue")
		try:
			report = self.tool_data("generate_tasks_from_compliance_alerts", {"company": MAIN})
		finally:
			dispatch_module.ALERT_TASK_MAP["housing_inspection_overdue"] = recipe
		self.assertEqual(report["created_count"], 1)
		self.assertEqual(len(report["skipped_unmapped"]), 1)
		self.assertIn("no task recipe", report["skipped_unmapped"][0]["reason"])

	def test_a_dismissed_alert_raises_no_task(self):
		self.a_camp_with_alerts(units=1)
		for row in STORE.rows("Compliance Alert"):
			row["dismissed"] = 1
		report = self.tool_data("generate_tasks_from_compliance_alerts", {"company": MAIN})
		self.assertEqual(report["created_count"], 0)

	def test_every_mapped_recipe_asks_for_at_least_one_piece_of_evidence(self):
		"""A recipe with an empty contract would be refused at creation anyway —
		this fails in the table rather than at four in the afternoon on a camp."""
		from erpnext_mcp.tools.dispatch import ALERT_TASK_MAP

		for alert_type, recipe in ALERT_TASK_MAP.items():
			with self.subTest(alert_type=alert_type):
				self.assertTrue(any(recipe["evidence"].values()), alert_type)

	def test_every_mapped_recipe_names_a_rule_that_exists(self):
		from erpnext_mcp import alerts
		from erpnext_mcp.tools.dispatch import ALERT_TASK_MAP

		self.assertEqual(set(ALERT_TASK_MAP) - set(alerts.names()), set())

	def test_every_recipe_that_names_a_record_names_one_this_app_can_build(self):
		from erpnext_mcp.tools.dispatch import ALERT_TASK_MAP
		from erpnext_mcp.tools.inspections import BUILDERS

		for alert_type, recipe in ALERT_TASK_MAP.items():
			with self.subTest(alert_type=alert_type):
				if recipe["creates_record"]:
					self.assertIn(recipe["creates_record"], BUILDERS)

	def test_the_shape_of_the_real_camp(self):
		"""Twenty-seven cabins with no inspection and no detector test on record —
		which is the state Highland's Mill Creek camp was actually in when Sprint 7
		finished — produce fifty-four alerts, fifty-four dispatchable tasks in ONE
		call, and nothing at all on the second call. That last number is the one
		that matters: it is what stops two people being sent to walk one cabin."""
		self.tool_data(
			"create_parcel", {"owning_entity": MAIN, "parcel_name": "Mill Creek", "acreage": 131.43}
		)
		for index in range(1, 28):
			self.tool_data(
				"create_housing_unit",
				{
					"parcel": "Mill Creek",
					"unit_name": f"MC-Cabin-{index:02d}",
					"unit_type": "Cabin",
					"square_footage": 400,
					"capacity": 4,
					"fsma_worker_facility": True,
				},
			)
		self.assertEqual(self.tool_data("refresh_compliance_alerts", {"today": TODAY})["created"], 54)

		generated = self.tool_data("generate_tasks_from_compliance_alerts", {"company": MAIN})
		self.assertEqual(generated["created_count"], 54)
		self.assertEqual(
			generated["by_alert_type"],
			{"housing_detector_test_stale": 27, "housing_inspection_overdue": 27},
		)

		board = self.tool_data("list_dispatch_board", {"company": MAIN})
		self.assertEqual(board["by_state"]["Available"], 54)
		self.assertEqual(board["generated_from_alerts"], 54)

		self.assertEqual(
			self.tool_data("generate_tasks_from_compliance_alerts", {"company": MAIN})["created_count"], 0
		)

	def test_licensed_work_is_dispatched_and_general_labour_is_self_pick(self):
		"""Fifty-four walks a foreman has to assign by hand are fifty-four walks
		that do not happen."""
		from erpnext_mcp.tools.dispatch import ALERT_TASK_MAP

		self.assertEqual(ALERT_TASK_MAP["housing_inspection_overdue"]["dispatch"], "Self-pick")
		self.assertEqual(ALERT_TASK_MAP["i9_expired"]["dispatch"], "Dispatched")
		self.assertEqual(ALERT_TASK_MAP["flc_license_expiring"]["dispatch"], "Dispatched")


class TheKillSwitches(DispatchTestCase):
	def test_every_mutating_dispatch_tool_ships_off(self):
		from erpnext_mcp import registry

		for name in (
			"create_farm_task",
			"assign_farm_task",
			"claim_farm_task",
			"start_farm_task",
			"complete_farm_task",
			"reject_farm_task",
			"generate_tasks_from_compliance_alerts",
			"create_housing_inspection",
			"update_housing_inspection",
			"create_detector_test",
			"update_detector_test",
			"create_water_test",
			"update_water_test",
		):
			with self.subTest(tool=name):
				self.assertIn(name, registry.MUTATING_TOOLS)
				self.assertNotIn(name, registry.DEFAULT_ON_MUTATING_TOOLS)

	def test_a_disabled_bridge_names_the_switch_to_tick(self):
		self.configure(enabled=1, allow_generate_tasks_from_compliance_alerts=0)
		message = self.tool_error("generate_tasks_from_compliance_alerts", {"company": MAIN})
		self.assertIn("allow_generate_tasks_from_compliance_alerts", message)

	def test_the_reads_ship_on(self):
		from erpnext_mcp import registry

		for name in (
			"list_available_tasks",
			"list_dispatched_tasks",
			"list_dispatch_board",
			"get_farm_task",
			"list_housing_inspections",
			"get_housing_inspection",
			"list_detector_tests",
			"get_detector_test",
			"list_water_tests",
			"get_water_test",
		):
			with self.subTest(tool=name):
				self.assertIn(name, registry.READ_TOOLS)


# ── S8 ──────────────────────────────────────────────────────────────────────
class TwoWorkersOneTask(DispatchTestCase):
	"""S8. The sub-second race, and the property that closes it.

	`test_a_task_somebody_else_holds_cannot_be_claimed` above already covers the
	SEQUENTIAL case — the second worker taps a second later and is refused by
	name. What it cannot cover is the simultaneous one: two handsets that read
	`Available` in the same instant both passed the check and both wrote, and the
	one who tapped first was told in a 200 that the task was theirs while
	somebody else's name went onto the record.

	THIS DOUBLE IS SINGLE-THREADED, SO THERE IS NO CONCURRENCY TO TEST. What
	there IS to test is the property that makes the lock work on a real bench:
	`claim_farm_task` must read the task's state AFTER taking the row lock, not
	before. So these tests stand in for the other transaction by mutating the row
	from inside `lock_task` — which is exactly the moment a real second claimer's
	commit becomes visible — and assert the refusal comes back.

	Both fail against the code before the fix, which had already read `state`
	into `row` by the time anything was locked.
	"""

	def racing(self, mutate):
		"""Run `mutate` ONCE, at the moment the row lock is taken.

		ONCE IS LOAD-BEARING, NOT TIDINESS. `mutate` is how a test stands in for
		the other transaction, and the most realistic `mutate` — "a worker claims
		it" — calls `claim_farm_task`, which takes the lock again. Without the
		one-shot guard that re-enters this hook forever, which is a recursion
		error dressed up as ninety identical claim failures.
		"""
		original = dispatch.lock_task
		fired = []

		def lock_then_race(task: str) -> None:
			original(task)
			if not fired:
				fired.append(task)
				mutate(task)

		dispatch.lock_task = lock_then_race
		self.addCleanup(setattr, dispatch, "lock_task", original)

	def test_a_claim_that_lands_during_the_lock_wait_is_refused(self):
		task = self.a_task()["name"]

		def somebody_else_gets_there_first(name: str) -> None:
			doc = frappe.get_doc("Farm Task", name)
			doc.state = "Claimed"
			doc.assigned_to = "EMP-002"
			doc.assigned_to_name = "Beatriz"
			doc.save(ignore_permissions=True)

		self.racing(somebody_else_gets_there_first)
		message = self.tool_error("claim_farm_task", {"task": task, "worker_id": "EMP-001"})
		self.assertIn("Beatriz", message)
		self.assertIn("both believing it is theirs", message)
		self.assertIn("Nothing was changed", message)

	def test_an_assignment_that_lands_during_the_lock_wait_sees_the_claim(self):
		"""The foreman's half. `reassign` exists to stop work being taken off
		somebody already standing in front of it, and that guard is a read — so a
		claim landing in the gap made it return nothing when there WAS a holder,
		and the flag was never asked for."""
		task = self.a_task()["name"]

		def a_worker_claims_it(name: str) -> None:
			self.tool_data(
				"claim_farm_task", {"task": name, "worker_id": "EMP-002", "worker_name": "Beatriz"}
			)

		self.racing(a_worker_claims_it)
		message = self.tool_error(
			"assign_farm_task", {"task": task, "assigned_to": "EMP-001", "assigned_to_name": "Ana"}
		)
		self.assertIn("Beatriz", message)
		self.assertIn("reassign=true", message)

	def test_the_lock_is_taken_before_the_state_is_read(self):
		"""Asserted on the ORDER rather than on an outcome, because the outcome
		above could also be produced by a re-read that happened to be late. If a
		future edit moves the state read back above `lock_task`, this fails."""
		seen = []
		original = dispatch.lock_task
		self.addCleanup(setattr, dispatch, "lock_task", original)

		def note(task: str) -> None:
			seen.append(("locked", frappe.db.get_value("Farm Task", task, "state")))
			original(task)

		dispatch.lock_task = note
		task = self.a_task()["name"]
		self.tool_data("claim_farm_task", {"task": task, "worker_id": "EMP-001"})
		self.assertEqual([entry[0] for entry in seen], ["locked"])
		self.assertEqual(seen[0][1], "Available")

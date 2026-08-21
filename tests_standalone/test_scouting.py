# SPDX-License-Identifier: MIT
"""The scouting round, from the task that asked for it to the register. v0.115.0.

A farm was already walking its blocks and the Crop Observation register was
empty: the growth stage, the Brix, the photograph and the coordinate all landed
on a Farm Task Assignment, and nothing landed where the pest-pressure engine and
the harvest-readiness overlay read from. Each class below is one claim about the
join that closes that, in the order of how much damage getting it wrong would do.

1. **THE SWEEP DOES NOT WRITE TWICE.** `TheSweepIsIdempotent`. Two observations
   of one round double the block's pest pressure, and nothing downstream can tell
   which was the duplicate. This is why the key is the REGISTER
   (`Crop Observation.source_task`) and not the task's own flag — including the
   case where the flag was cleared and the row is still there, which the sweep
   repairs rather than re-filing.

2. **A MATURITY ROUND IS NOT A PEST COUNT OF ZERO.**
   `TheRoundSaysWhatItWasFor`. Filing a harvest-readiness walk with a fabricated
   threat puts a round that was not looking for the pest into that pest's
   pressure trend, which is the one thing this register must not do. So the
   threat, the category and the count are mandatory on a Pest Scout and only
   there — asserted in both directions.

3. **ONE BAD ROW MUST NOT COST THE WINDOW.** `TheSweepSurvivesABadRow`. A sweep
   that discarded a week of scouting over one mistyped Brix is a sweep an
   operator turns off, and then nothing is indexed at all.

4. **BOTH HALVES OR THE RECORD IS HALF A RECORD.** `TheSweepReadsBothHalves`.
   The measurements are on the task, the photograph and the coordinate are on the
   assignment, and reading either alone produces a well-formed row missing the
   thing somebody will later need.

5. **THE PIPELINE IS ONE PIPELINE.** `TheThresholdEngineRunsOnAPestScout`. A
   sweep that re-implemented the threshold lookup would evaluate an observation
   differently depending on which door it came through, and the two would drift
   silently because both produce a valid record either way.

6. **A CONTRACT CAN ASK FOR THE FIX NOBODY TYPES.** `TheGpsEvidenceKey`. A
   handset takes the coordinate on its own, so a client that never learned to
   send one closes the task perfectly happily — which is exactly why it has to be
   refusable.

7. **BRIX IS A MEASUREMENT WITH A METHOD.** `TheBrixReadingCarriesItsMethod`. A
   refractometer figure and somebody's estimate must never average together.
"""

import json

import frappe

from erpnext_mcp import task_templates
from erpnext_mcp.erpnext_mcp.doctype.crop_observation.crop_observation import BRIX_CEILING
from erpnext_mcp.patches import backfill_observation_type as backfill

from .fixtures import MAIN, V12TestCase, seed_masters
from .harness import STORE

BLOCK = "Yellow Camp Block 3 - MC"
BLOCK_TWO = "Yellow Camp Block 4 - MC"
CHERRY = "Cherry"
MOTH = "Codling Moth"

TODAY = "2026-07-24"
YESTERDAY = "2026-07-23"

OBSERVATION = "Crop Observation"
FARM_TASK = "Farm Task"

#: The contract the shipped Field Scouting template carries.
SCOUT_CONTRACT = {"photos": True, "findings_text": True, "gps": True}

A_PHOTO = [{"file_url": "/files/bing-canopy.jpg", "evidence_type": "Photo", "caption": "east row"}]
A_FIX = "45.5152,-121.1787"

ALL_ON = {
	f"allow_{name}": 1
	for name in (
		"create_parcel",
		"create_field",
		"create_farm_task",
		"create_task_from_template",
		"list_farm_task_templates",
		"claim_farm_task",
		"complete_farm_task",
		"get_farm_task",
		"index_scouting_observations",
		"create_crop_observation",
		"list_crop_observations",
		"set_pest_action_threshold",
		"list_pest_pressures",
		"get_ipm_recommendation",
	)
}


class ScoutingTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		seed_masters()
		self.configure(enabled=1, **ALL_ON)
		self._farm()

	def _farm(self):
		self.tool_data(
			"create_parcel",
			{"owning_entity": MAIN, "parcel_name": "Mill Creek", "acreage": 131.43},
		)
		for name in ("Yellow Camp Block 3", "Yellow Camp Block 4"):
			self.tool_data(
				"create_field",
				{
					"parcel": "Mill Creek",
					"field_name": name,
					"acreage": 12.5,
					"crop": CHERRY,
					"variety": "Bing",
					"planting_year": 1998,
					"condition": "Good",
				},
			)

	# ── helpers ────────────────────────────────────────────────────────────
	def a_scouting_task(self, block=BLOCK, **overrides):
		payload = {
			"task_name": f"Scout — {block}",
			"task_type": "Scouting",
			"evidence_required": dict(SCOUT_CONTRACT),
			"location_doctype": "Field",
			"location": block,
			"creates_record": OBSERVATION,
			"creates_record_data": {"observation_type": "General", "scouting_method": "Visual"},
			"company": MAIN,
		}
		payload.update(overrides)
		return self.tool_data("create_farm_task", payload)["name"]

	def complete(self, task, record_data=None, worker="EMP-001", **overrides):
		self.tool_data("claim_farm_task", {"task": task, "worker_id": worker, "worker_name": "Ana"})
		payload = {
			"task": task,
			"worker_id": worker,
			"evidence_files": list(A_PHOTO),
			"findings_text": "east row a week behind the rest",
			"farm_location_gps": A_FIX,
			"completed_at": f"{TODAY} 09:30:00",
		}
		if record_data is not None:
			payload["record_data"] = record_data
		payload.update(overrides)
		return self.tool_data("complete_farm_task", payload)

	def sweep(self, date_from=TODAY, date_to=TODAY, **overrides):
		payload = {"date_from": date_from, "date_to": date_to}
		payload.update(overrides)
		return self.tool_data("index_scouting_observations", payload)

	def observation_rows(self):
		return list(STORE.tables.get(OBSERVATION, {}).values())

	def a_walked_round(self, record_data=None, block=BLOCK, **overrides):
		"""One scouting task, claimed, completed and swept. Returns its observation."""
		task = self.a_scouting_task(block=block)
		self.complete(task, record_data=record_data, **overrides)
		written = self.sweep()["observations_written"]
		self.assertEqual(len(written), 1, "expected exactly one observation from one round")
		return task, written[0]


# ── 1. the sweep does not write twice ───────────────────────────────────────
class TheSweepIsIdempotent(ScoutingTestCase):
	def test_a_second_sweep_over_the_same_window_writes_nothing(self):
		self.a_walked_round({"growth_stage_code": "81", "brix_reading": 14.5, "brix_method": "Refractometer"})
		again = self.sweep()
		self.assertEqual(again["counts"]["observations_written"], 0)
		self.assertEqual(again["counts"]["observations_already_present"], 1)
		self.assertEqual(len(self.observation_rows()), 1)

	def test_the_task_is_pointed_at_the_observation_it_produced(self):
		task, written = self.a_walked_round()
		self.assertEqual(frappe.db.get_value(FARM_TASK, task, "produced_record"), written["observation"])

	def test_a_cleared_flag_repairs_rather_than_filing_a_second_row(self):
		"""The failure a `doc_events` hook cannot see.

		A hook keyed on the flag re-fires the moment somebody blanks it, and the
		block's pest pressure doubles with nothing on either record saying which
		was the duplicate. The register is the authority precisely so that this
		case is a repair.
		"""
		task, written = self.a_walked_round()
		frappe.db.set_value(FARM_TASK, task, "produced_record", "")

		again = self.sweep()
		self.assertEqual(again["counts"]["observations_written"], 0)
		self.assertEqual(again["counts"]["flags_repaired"], 1)
		self.assertEqual(again["flags_repaired"][0]["observation"], written["observation"])
		self.assertEqual(len(self.observation_rows()), 1)
		self.assertEqual(frappe.db.get_value(FARM_TASK, task, "produced_record"), written["observation"])

	def test_a_window_that_does_not_cover_the_completion_writes_nothing(self):
		task = self.a_scouting_task()
		self.complete(task)
		empty = self.sweep(date_from=YESTERDAY, date_to=YESTERDAY)
		self.assertEqual(empty["counts"]["observations_written"], 0)
		self.assertEqual(self.observation_rows(), [])

	def test_a_sweep_with_no_window_is_refused(self):
		message = self.tool_error("index_scouting_observations", {"company": MAIN})
		self.assertIn("date_from and date_to are both required", message)
		self.assertIn("Nothing was written", message)

	def test_a_backwards_window_is_refused(self):
		message = self.tool_error("index_scouting_observations", {"date_from": TODAY, "date_to": YESTERDAY})
		self.assertIn("is after date_to", message)


# ── 2. the round says what it was for ───────────────────────────────────────
class TheRoundSaysWhatItWasFor(ScoutingTestCase):
	def test_a_harvest_readiness_round_needs_no_threat_and_no_count(self):
		"""The whole reason `observation_type` exists.

		Before v0.115.0 this round had three homes and all of them were bad:
		refused, filed with an invented threat, or kept in a spreadsheet.
		"""
		_task, written = self.a_walked_round(
			{
				"observation_type": "Harvest Readiness",
				"growth_stage_code": "87",
				"brix_reading": 18.4,
				"brix_method": "Refractometer",
			}
		)
		row = frappe.db.get_value(
			OBSERVATION,
			written["observation"],
			["observation_type", "threat", "threat_category", "brix_reading", "growth_stage_code"],
			as_dict=True,
		)
		self.assertEqual(row["observation_type"], "Harvest Readiness")
		self.assertFalse(row["threat"])
		self.assertFalse(row["threat_category"])
		self.assertEqual(float(row["brix_reading"]), 18.4)
		self.assertEqual(row["growth_stage_code"], "87")

	def test_a_pest_scout_with_no_threat_is_refused(self):
		"""And the refusal names the way out, rather than just saying no."""
		message = self.tool_error(
			"create_crop_observation",
			{"block": BLOCK, "threat_category": "Insect", "count_observed": 3, "company": MAIN},
		)
		self.assertIn("threat", message)

	def test_the_controller_refuses_a_pest_scout_with_no_threat(self):
		doc = frappe.new_doc(OBSERVATION)
		doc.company = MAIN
		doc.block_doctype = "Field"
		doc.block = BLOCK
		doc.observation_type = "Pest Scout"
		doc.observed_on = TODAY
		doc.threat_category = "Insect"
		doc.count_observed = 3
		with self.assertRaises(Exception) as caught:
			doc.insert(ignore_permissions=True)
		self.assertIn("Name the threat", str(caught.exception))
		# and it names the way out rather than only saying no
		self.assertIn("Harvest Readiness", str(caught.exception))

	def test_the_controller_refuses_a_pest_scout_with_no_category(self):
		doc = frappe.new_doc(OBSERVATION)
		doc.company = MAIN
		doc.block_doctype = "Field"
		doc.block = BLOCK
		doc.observation_type = "Pest Scout"
		doc.observed_on = TODAY
		doc.threat = MOTH
		doc.count_observed = 3
		with self.assertRaises(Exception) as caught:
			doc.insert(ignore_permissions=True)
		self.assertIn("Threat Category is required on a Pest Scout", str(caught.exception))

	def test_the_controller_refuses_a_pest_scout_with_no_count(self):
		doc = frappe.new_doc(OBSERVATION)
		doc.company = MAIN
		doc.block_doctype = "Field"
		doc.block = BLOCK
		doc.observation_type = "Pest Scout"
		doc.observed_on = TODAY
		doc.threat = MOTH
		doc.threat_category = "Insect"
		with self.assertRaises(Exception) as caught:
			doc.insert(ignore_permissions=True)
		self.assertIn("Count Observed is required", str(caught.exception))

	def test_a_threat_named_on_a_maturity_round_is_kept(self):
		"""What is relaxed is the obligation, not the vocabulary.

		A harvest walk that also noticed cherry fruit fly is a better record than
		one that dropped the sighting.
		"""
		_task, written = self.a_walked_round(
			{
				"observation_type": "Harvest Readiness",
				"threat": MOTH,
				"threat_category": "Insect",
				"brix_reading": 17.0,
				"brix_method": "Estimate",
			}
		)
		self.assertEqual(frappe.db.get_value(OBSERVATION, written["observation"], "threat"), MOTH)

	def test_a_threat_with_no_category_is_refused_even_on_a_maturity_round(self):
		"""A threat with no category matches no threshold and files under no
		pressure record — a sighting nothing downstream can read."""
		doc = frappe.new_doc(OBSERVATION)
		doc.company = MAIN
		doc.block_doctype = "Field"
		doc.block = BLOCK
		doc.observation_type = "General"
		doc.observed_on = TODAY
		doc.threat = MOTH
		with self.assertRaises(Exception) as caught:
			doc.insert(ignore_permissions=True)
		self.assertIn("no Threat Category", str(caught.exception))

	def test_an_unknown_observation_type_is_refused_by_the_sweep(self):
		task = self.a_scouting_task()
		self.complete(task, record_data={"observation_type": "Vibes"})
		report = self.sweep()
		self.assertEqual(report["counts"]["observations_written"], 0)
		self.assertEqual(report["counts"]["refused"], 1)
		self.assertIn("Vibes", report["refused"][0]["reason"])

	def test_the_backfill_calls_every_pre_existing_row_a_pest_scout(self):
		"""They all named a threat and carried a count — the DocType refused one
		that did not — so this records what they always were."""
		self.tool_data(
			"create_crop_observation",
			{
				"block": BLOCK,
				"threat": MOTH,
				"threat_category": "Insect",
				"crop": CHERRY,
				"count_observed": 2,
				"company": MAIN,
			},
		)
		name = self.observation_rows()[0]["name"]
		frappe.db.set_value(OBSERVATION, name, "observation_type", "")

		report = backfill.backfill_observation_type()
		self.assertEqual(report["filled"], 1)
		self.assertEqual(frappe.db.get_value(OBSERVATION, name, "observation_type"), "Pest Scout")

		again = backfill.backfill_observation_type()
		self.assertEqual(again["filled"], 0)
		self.assertEqual(again["already_set"], 1)

	def test_the_backfill_never_rewrites_a_type_somebody_set(self):
		self.a_walked_round({"observation_type": "Harvest Readiness"})
		backfill.backfill_observation_type()
		self.assertEqual(self.observation_rows()[0]["observation_type"], "Harvest Readiness")


# ── 3. one bad row must not cost the window ─────────────────────────────────
class TheSweepSurvivesABadRow(ScoutingTestCase):
	def test_a_refused_round_does_not_stop_the_ones_after_it(self):
		bad = self.a_scouting_task(block=BLOCK)
		self.complete(bad, record_data={"brix_reading": 900, "brix_method": "Refractometer"})
		good = self.a_scouting_task(block=BLOCK_TWO)
		self.complete(
			good, record_data={"brix_reading": 15.0, "brix_method": "Refractometer"}, worker="EMP-002"
		)

		report = self.sweep()
		self.assertEqual(report["counts"]["observations_written"], 1)
		self.assertEqual(report["counts"]["refused"], 1)
		self.assertEqual(report["refused"][0]["task"], bad)
		self.assertEqual(report["observations_written"][0]["block"], BLOCK_TWO)
		self.assertEqual(len(self.observation_rows()), 1)

	def test_the_refusal_says_why_and_the_completion_stands(self):
		task = self.a_scouting_task()
		self.complete(task, record_data={"brix_reading": 900, "brix_method": "Refractometer"})
		report = self.sweep()
		self.assertIn(str(int(BRIX_CEILING)), report["refused"][0]["reason"])
		self.assertEqual(frappe.db.get_value(FARM_TASK, task, "state"), "Completed")
		self.assertIn("could not be filed", " ".join(report["notes"]))

	def test_a_task_with_no_location_is_skipped_and_counted(self):
		task = self.a_scouting_task()
		frappe.db.set_value(FARM_TASK, task, {"location": "", "location_doctype": ""})
		self.complete(task)
		report = self.sweep()
		self.assertEqual(report["skipped"]["completions_without_a_block"], 1)
		self.assertEqual(report["counts"]["observations_written"], 0)
		self.assertIn("An observation IS a block", " ".join(report["notes"]))

	def test_completions_of_other_work_are_counted_not_indexed(self):
		other = self.tool_data(
			"create_farm_task",
			{
				"task_name": "Fix the gate",
				"task_type": "Repair",
				"evidence_required": {"findings_text": True},
				"company": MAIN,
			},
		)["name"]
		self.tool_data("claim_farm_task", {"task": other, "worker_id": "EMP-003", "worker_name": "Luis"})
		self.tool_data(
			"complete_farm_task",
			{
				"task": other,
				"worker_id": "EMP-003",
				"findings_text": "",
				"completed_at": f"{TODAY} 11:00:00",
			},
		)
		report = self.sweep()
		self.assertEqual(report["skipped"]["completions_of_other_work"], 1)
		self.assertEqual(report["counts"]["observations_written"], 0)


# ── 4. both halves or the record is half a record ───────────────────────────
class TheSweepReadsBothHalves(ScoutingTestCase):
	def setUp(self):
		super().setUp()
		self.task, self.written = self.a_walked_round(
			{
				"observation_type": "Harvest Readiness",
				"growth_stage_code": "85",
				"brix_reading": 16.25,
				"brix_method": "Refractometer",
				"crop": CHERRY,
			}
		)
		self.row = frappe.db.get_value(
			OBSERVATION,
			self.written["observation"],
			[
				"block",
				"block_doctype",
				"observed_on",
				"observation_type",
				"scouting_method",
				"growth_stage_code",
				"brix_reading",
				"brix_method",
				"observed_gps",
				"photo",
				"notes",
				"source_task",
			],
			as_dict=True,
		)

	def test_the_measurements_come_off_the_task(self):
		self.assertEqual(self.row["growth_stage_code"], "85")
		self.assertEqual(float(self.row["brix_reading"]), 16.25)
		self.assertEqual(self.row["brix_method"], "Refractometer")

	def test_the_location_fix_comes_off_the_assignment(self):
		self.assertEqual(self.row["observed_gps"], A_FIX)

	def test_the_photograph_comes_off_the_assignment(self):
		self.assertEqual(self.row["photo"], A_PHOTO[0]["file_url"])

	def test_the_block_and_the_day_come_off_the_task_and_the_completion(self):
		self.assertEqual(self.row["block"], BLOCK)
		self.assertEqual(self.row["block_doctype"], "Field")
		self.assertEqual(str(self.row["observed_on"]), TODAY)

	def test_the_workers_own_words_are_first_in_the_notes(self):
		"""The only part of this record somebody wrote rather than the app
		assembled, and burying it under a provenance line is how a note stops
		being read."""
		self.assertTrue(self.row["notes"].startswith("east row a week behind the rest"))

	def test_the_notes_name_the_task_the_round_came_from(self):
		self.assertIn(self.task, self.row["notes"])
		self.assertIn("index_scouting_observations", self.row["notes"])

	def test_the_observation_points_back_at_its_task(self):
		self.assertEqual(self.row["source_task"], self.task)

	def test_a_completion_overrides_the_templates_default(self):
		"""The task states what is usually true; the submission states what
		actually happened."""
		self.assertEqual(self.row["observation_type"], "Harvest Readiness")
		self.assertEqual(self.row["scouting_method"], "Visual")

	def test_an_unrecognised_record_data_key_is_kept_in_the_notes(self):
		"""A silently dropped `brix_readng` is indistinguishable from a round
		where nobody took a reading."""
		task = self.a_scouting_task(block=BLOCK_TWO)
		self.complete(task, record_data={"brix_readng": 19}, worker="EMP-002")
		written = self.sweep()["observations_written"]
		entry = next(row for row in written if row["task"] == task)
		self.assertEqual(entry["unrecognised_record_data_keys"], ["brix_readng"])
		self.assertIn("brix_readng", frappe.db.get_value(OBSERVATION, entry["observation"], "notes"))

	def test_a_read_only_evaluation_column_cannot_be_written_from_a_completion(self):
		"""`creates_record_data` is client-supplied. An open merge would let a
		handset claim an evaluation nobody made."""
		task = self.a_scouting_task(block=BLOCK_TWO)
		self.complete(task, record_data={"threshold_exceeded": 1}, worker="EMP-002")
		written = self.sweep()["observations_written"]
		entry = next(row for row in written if row["task"] == task)
		self.assertEqual(
			int(frappe.db.get_value(OBSERVATION, entry["observation"], "threshold_exceeded") or 0), 0
		)

	def test_the_completion_reports_that_the_record_is_written_by_a_sweep(self):
		"""A caller told 'no builder for this' would go looking for a record that
		is coming, which is the wrong of the two answers."""
		task = self.a_scouting_task(block=BLOCK_TWO)
		result = self.complete(task, worker="EMP-002")
		self.assertIn("index_scouting_observations", result["record_note"])
		self.assertIsNone(result["produced_record"])

	def test_the_completion_stamps_the_submission_onto_the_task(self):
		task = self.a_scouting_task(block=BLOCK_TWO)
		self.complete(task, record_data={"brix_reading": 21.0, "brix_method": "Estimate"}, worker="EMP-002")
		stamped = json.loads(frappe.db.get_value(FARM_TASK, task, "creates_record_data") or "{}")
		self.assertEqual(stamped["brix_reading"], 21.0)
		# the template's own default survives underneath the submission
		self.assertEqual(stamped["scouting_method"], "Visual")


# ── 4b. the shipped path, end to end ────────────────────────────────────────
class TheSeededTemplateWalksTheWholeWay(ScoutingTestCase):
	"""Template → task → completion → sweep → register, through the shipped door.

	Every other class here builds its task with `create_farm_task`, which is the
	honest way to isolate the sweep and the wrong way to find out whether the
	thing an operation actually uses works. This is the path a foreman takes: the
	template seeded by `bench migrate`, raised with `create_task_from_template`.
	It is the test that would have caught a snapshot that dropped
	`creates_record_data` or a contract that lost its `gps` key on the way onto
	the task.
	"""

	def setUp(self):
		super().setUp()
		task_templates.seed_farm_task_templates()
		self.task = self.tool_data(
			"create_task_from_template",
			{
				"template": "Field Scouting",
				"location_doctype": "Field",
				"location": BLOCK,
				"company": MAIN,
			},
		)["name"]

	def test_the_contract_and_the_defaults_arrive_on_the_task(self):
		row = frappe.db.get_value(
			FARM_TASK,
			self.task,
			["task_type", "creates_record", "evidence_required", "creates_record_data"],
			as_dict=True,
		)
		self.assertEqual(row["task_type"], "Scouting")
		self.assertEqual(row["creates_record"], OBSERVATION)
		self.assertTrue(json.loads(row["evidence_required"])["gps"])
		self.assertEqual(json.loads(row["creates_record_data"])["scouting_method"], "Visual")

	def test_a_round_raised_from_the_template_reaches_the_register(self):
		self.complete(
			self.task,
			record_data={
				"observation_type": "Harvest Readiness",
				"growth_stage_code": "87",
				"brix_reading": 18.0,
				"brix_method": "Refractometer",
			},
			checklist=[
				"Growth stage read and recorded as a BBCH code",
				"Brix read, with the method it was read by",
				"Representative photograph of the fruit or the canopy",
				"What the block looked like, in words",
			],
		)
		written = self.sweep()["observations_written"]
		self.assertEqual(len(written), 1)
		row = frappe.db.get_value(
			OBSERVATION,
			written[0]["observation"],
			["observation_type", "brix_reading", "growth_stage_code", "observed_gps", "photo", "block"],
			as_dict=True,
		)
		self.assertEqual(row["observation_type"], "Harvest Readiness")
		self.assertEqual(float(row["brix_reading"]), 18.0)
		self.assertEqual(row["growth_stage_code"], "87")
		self.assertEqual(row["observed_gps"], A_FIX)
		self.assertEqual(row["photo"], A_PHOTO[0]["file_url"])
		self.assertEqual(row["block"], BLOCK)

	def test_the_template_round_cannot_be_closed_without_a_fix(self):
		"""The contract the template ships with, refusing at the only moment it
		can — while the phone that has the coordinate is still in somebody's
		hand."""
		self.tool_data("claim_farm_task", {"task": self.task, "worker_id": "EMP-001", "worker_name": "Ana"})
		message = self.tool_error(
			"complete_farm_task",
			{
				"task": self.task,
				"worker_id": "EMP-001",
				"evidence_files": list(A_PHOTO),
				"findings_text": "",
				"checklist": [
					"Growth stage read and recorded as a BBCH code",
					"Brix read, with the method it was read by",
					"Representative photograph of the fruit or the canopy",
					"What the block looked like, in words",
				],
			},
		)
		self.assertIn("gps:", message)


# ── 5. the pipeline is one pipeline ─────────────────────────────────────────
class TheThresholdEngineRunsOnAPestScout(ScoutingTestCase):
	def setUp(self):
		super().setUp()
		self.tool_data(
			"set_pest_action_threshold",
			{
				"crop": CHERRY,
				"threat": MOTH,
				"threat_category": "Insect",
				"action_threshold": 5,
				"sample_unit": "Per Trap",
				"company": MAIN,
				"recommended_methods": (
					"Cultural: remove alternate hosts on the headland\nChemical: cover spray at label rate"
				),
			},
		)

	def _scouted(self, count):
		return self.a_walked_round(
			{
				"observation_type": "Pest Scout",
				"threat": MOTH,
				"threat_category": "Insect",
				"crop": CHERRY,
				"count_observed": count,
				"sample_unit": "Per Trap",
				"sample_size": 10,
			}
		)

	def test_a_count_over_the_threshold_moves_the_pressure_and_recommends(self):
		_task, written = self._scouted(8)
		self.assertTrue(written["pest_pressure"])
		self.assertTrue(written["ipm_recommendation"])
		self.assertEqual(
			int(frappe.db.get_value(OBSERVATION, written["observation"], "threshold_exceeded") or 0), 1
		)

	def test_a_count_under_the_threshold_generates_nothing(self):
		_task, written = self._scouted(2)
		self.assertTrue(written["pest_pressure"], "a clean walk still moves the pressure record")
		self.assertIsNone(written["ipm_recommendation"])

	def test_the_threshold_that_evaluated_it_is_copied_onto_the_record(self):
		"""So the decision is re-readable years later against the number that
		actually made it."""
		_task, written = self._scouted(8)
		row = frappe.db.get_value(
			OBSERVATION, written["observation"], ["threshold", "threshold_value"], as_dict=True
		)
		self.assertTrue(row["threshold"])
		self.assertEqual(float(row["threshold_value"]), 5.0)

	def test_a_maturity_round_is_never_evaluated(self):
		"""Evaluating one would either find no threshold, or match a threshold
		for a pest nobody was looking for."""
		_task, written = self.a_walked_round(
			{
				"observation_type": "Harvest Readiness",
				"crop": CHERRY,
				"brix_reading": 19.0,
				"brix_method": "Refractometer",
			}
		)
		self.assertIsNone(written["pest_pressure"])
		self.assertIsNone(written["ipm_recommendation"])
		self.assertEqual(
			int(frappe.db.get_value(OBSERVATION, written["observation"], "threshold_exceeded") or 0), 0
		)

	def test_both_doors_evaluate_the_same_count_the_same_way(self):
		"""Two implementations of one lookup drift silently, because both
		produce a well-formed record either way."""
		typed = self.tool_data(
			"create_crop_observation",
			{
				"block": BLOCK_TWO,
				"threat": MOTH,
				"threat_category": "Insect",
				"crop": CHERRY,
				"count_observed": 8,
				"sample_unit": "Per Trap",
				"sample_size": 10,
				"company": MAIN,
			},
		)
		_task, swept = self._scouted(8)
		swept_row = frappe.db.get_value(
			OBSERVATION,
			swept["observation"],
			["threshold", "threshold_value", "threshold_comparison", "threshold_exceeded"],
			as_dict=True,
		)
		self.assertEqual(swept_row["threshold"], typed["threshold"])
		self.assertEqual(float(swept_row["threshold_value"]), float(typed["threshold_value"]))
		self.assertEqual(swept_row["threshold_comparison"], typed["threshold_comparison"])
		self.assertEqual(int(swept_row["threshold_exceeded"] or 0), 1)


# ── 6. the fix nobody types ─────────────────────────────────────────────────
class TheGpsEvidenceKey(ScoutingTestCase):
	def test_a_completion_with_no_fix_is_refused(self):
		task = self.a_scouting_task()
		self.tool_data("claim_farm_task", {"task": task, "worker_id": "EMP-001", "worker_name": "Ana"})
		message = self.tool_error(
			"complete_farm_task",
			{
				"task": task,
				"worker_id": "EMP-001",
				"evidence_files": list(A_PHOTO),
				"findings_text": "",
			},
		)
		self.assertIn("gps:", message)
		self.assertIn("farm_location_gps", message)
		self.assertIn("Nothing was changed", message)

	def test_a_fix_already_on_the_assignment_is_not_demanded_a_second_time(self):
		"""Demanding a second copy would refuse a submission whose evidence is
		already on file.

		No shipped surface writes this before completion today — it is set at
		completion or by a Desk edit — so the row is written directly here.
		That is the case the fallback exists for, and the alternative is a
		refusal that reads as "you did not send a coordinate" while the
		coordinate is sitting on the record it is asking about.
		"""
		task = self.a_scouting_task()
		self.tool_data("claim_farm_task", {"task": task, "worker_id": "EMP-001", "worker_name": "Ana"})
		assignment = next(
			row
			for row in STORE.rows("Farm Task Assignment")
			if row.get("task") == task and row.get("state") == "Claimed"
		)
		frappe.db.set_value("Farm Task Assignment", assignment["name"], "farm_location_gps", A_FIX)

		self.tool_data(
			"complete_farm_task",
			{
				"task": task,
				"worker_id": "EMP-001",
				"evidence_files": list(A_PHOTO),
				"findings_text": "",
				"completed_at": f"{TODAY} 09:30:00",
			},
		)
		self.assertEqual(frappe.db.get_value(FARM_TASK, task, "state"), "Completed")

	def test_a_contract_that_does_not_ask_for_a_fix_does_not_get_one_demanded(self):
		"""Additive. No task already on a board tightened when this shipped."""
		task = self.a_scouting_task(evidence_required={"photos": True, "findings_text": True})
		self.tool_data("claim_farm_task", {"task": task, "worker_id": "EMP-001", "worker_name": "Ana"})
		self.tool_data(
			"complete_farm_task",
			{
				"task": task,
				"worker_id": "EMP-001",
				"evidence_files": list(A_PHOTO),
				"findings_text": "",
				"completed_at": f"{TODAY} 09:30:00",
			},
		)
		self.assertEqual(frappe.db.get_value(FARM_TASK, task, "state"), "Completed")

	def test_a_misspelt_evidence_key_is_still_refused(self):
		"""`gps` joining the vocabulary must not turn `gpss` into a silent no-op."""
		message = self.tool_error(
			"create_farm_task",
			{
				"task_name": "Scout it",
				"task_type": "Scouting",
				"evidence_required": {"gpss": True},
				"company": MAIN,
			},
		)
		self.assertIn("gpss", message)


# ── 7. brix is a measurement with a method ──────────────────────────────────
class TheBrixReadingCarriesItsMethod(ScoutingTestCase):
	def _observation(self, **fields):
		doc = frappe.new_doc(OBSERVATION)
		doc.company = MAIN
		doc.block_doctype = "Field"
		doc.block = BLOCK
		doc.observation_type = "Harvest Readiness"
		doc.observed_on = TODAY
		for key, value in fields.items():
			doc.set(key, value)
		return doc

	def test_a_reading_with_no_method_is_refused(self):
		with self.assertRaises(Exception) as caught:
			self._observation(brix_reading=18.0).insert(ignore_permissions=True)
		self.assertIn("no Brix Method", str(caught.exception))

	def test_a_method_with_no_reading_is_refused(self):
		with self.assertRaises(Exception) as caught:
			self._observation(brix_method="Refractometer").insert(ignore_permissions=True)
		self.assertIn("no Brix reading", str(caught.exception))

	def test_a_negative_reading_is_refused(self):
		with self.assertRaises(Exception) as caught:
			self._observation(brix_reading=-1, brix_method="Estimate").insert(ignore_permissions=True)
		self.assertIn("negative", str(caught.exception))

	def test_a_misplaced_decimal_point_is_refused(self):
		with self.assertRaises(Exception) as caught:
			self._observation(brix_reading=185, brix_method="Refractometer").insert(ignore_permissions=True)
		self.assertIn(str(int(BRIX_CEILING)), str(caught.exception))

	def test_an_unknown_method_is_refused(self):
		with self.assertRaises(Exception) as caught:
			self._observation(brix_reading=18, brix_method="Eyeball").insert(ignore_permissions=True)
		self.assertIn("Brix Method must be one of", str(caught.exception))

	def test_an_estimate_is_recorded_as_an_estimate(self):
		"""It is a useful record of a walk with no instrument, and a useless
		input to a contract specification — which is why the two are kept apart
		rather than averaged."""
		doc = self._observation(brix_reading=17.0, brix_method="Estimate")
		doc.insert(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value(OBSERVATION, doc.name, "brix_method"), "Estimate")

	def test_a_round_with_no_brix_at_all_is_fine(self):
		doc = self._observation(growth_stage_code="75")
		doc.insert(ignore_permissions=True)
		self.assertFalse(frappe.db.get_value(OBSERVATION, doc.name, "brix_reading"))

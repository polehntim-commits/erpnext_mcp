# SPDX-License-Identifier: MIT
"""Accident investigation: opened at the scene, finished over days.

FIVE CLAIMS.

1. **THE CREATE CALL ASKS FOR FIVE THINGS.** `TheSceneCallIsShort`. A create that
   demanded a root cause and a recordability determination is one nobody makes
   until the evening, and the account written in the evening is worth a fraction
   of the one written at the scene.

2. **IT IS NOT A SINGLE SESSION.** `ItSpansDays`. Updates append; nothing
   auto-closes; the narrative keeps each day's entry stamped with the day it was
   written, because "what did we think on Tuesday" is a question a hearing asks.

3. **RECORDABILITY IS A PERSON'S DETERMINATION.** `RecordabilityIsDecidedNotInferred`.
   It defaults to Undetermined, cannot be changed without the basis, and blocks
   closure while it stands.

4. **CLOSING MEANS SOMETHING.** `ClosingIsChecked`. No corrective action, no
   follow-up, an untaken witness statement or an open step and it does not close
   — and every reason is named at once rather than one refusal at a time.

5. **WITNESSES ARE ROWS.** `WitnessesAreRowsNotAString`. 'We still have not
   interviewed Miguel' is the most useful thing a half-finished investigation
   knows, and a comma-separated string cannot say it.
"""

import json

from .fixtures import MAIN, V12TestCase
from .harness import STORE, frappe

ALL_ON = {
	f"allow_{name}": 1
	for name in (
		"create_accident_report",
		"update_accident_investigation",
		"close_accident_investigation",
		"get_accident_report",
		"list_accident_reports",
		"create_farm_task",
		"claim_farm_task",
		"start_farm_task",
		"complete_farm_task",
		"reject_farm_task",
		"add_task_note",
		"attach_audio_note",
		"list_task_notes",
	)
}

WORKER = "HR-EMP-00001"
FOREMAN = "HR-EMP-00009"


def _hours_ago(hours: int) -> str:
	return str(frappe.utils.add_to_date(frappe.utils.now(), hours=-hours))


class AccidentTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON)
		STORE.seed(
			"Employee",
			[
				{"name": WORKER, "employee_name": "Ana Ramos", "company": MAIN, "status": "Active"},
				{"name": FOREMAN, "employee_name": "Flor Diaz", "company": MAIN, "status": "Active"},
			],
		)

	def a_report(self, **overrides):
		payload = {
			"occurred_at": _hours_ago(2),
			"incident_description": "Ana caught her hand between the sorter belt and the guard rail.",
			"severity": "Medical Treatment",
			"injured_person": WORKER,
			"medical_treatment": "Medical Treatment Beyond First Aid",
			"immediate_actions": "Line locked out, first aid given, driven to the clinic.",
			"witnesses": ["Miguel Soto"],
			"company": MAIN,
			"reported_by": FOREMAN,
		}
		payload.update(overrides)
		return self.tool_data("create_accident_report", payload)


# ── 1. the scene call is short ──────────────────────────────────────────────
class TheSceneCallIsShort(AccidentTestCase):
	def test_it_opens_with_when_and_what_alone(self):
		data = self.tool_data(
			"create_accident_report",
			{
				"occurred_at": _hours_ago(1),
				"incident_description": "Slipped on the wet floor by the wash line.",
				"company": MAIN,
			},
		)
		self.assertEqual(data["status"], "Open")
		self.assertTrue(data["name"])

	def test_a_report_with_no_time_is_refused(self):
		"""Every clock this record is measured against runs from it."""
		error = self.tool_error("create_accident_report", {"incident_description": "something happened"})
		self.assertIn("occurred_at is required", error)

	def test_a_report_with_no_account_is_refused(self):
		error = self.tool_error("create_accident_report", {"occurred_at": _hours_ago(1)})
		self.assertIn("worth less every hour it is not written", error)

	def test_recordability_starts_undetermined(self):
		"""A record that defaulted to No would put an unmade decision on the
		300 log's right side."""
		self.assertEqual(self.a_report()["osha_recordable"], "Undetermined")

	def test_the_reporting_lag_is_computed(self):
		"""The gap between when it happened and when it was reported is the
		first thing a claim measures."""
		data = self.a_report(occurred_at=_hours_ago(5))
		self.assertGreater(data["reporting_lag_hours"], 4)

	def test_a_fatality_carries_the_telephone_obligation(self):
		data = self.a_report(severity="Fatality")
		self.assertTrue(data["urgent_obligations"])
		self.assertIn("1904.39", data["urgent_obligations"][0])
		self.assertIn("8 hours", data["urgent_obligations"][0])

	def test_a_hospitalisation_carries_the_24_hour_one(self):
		data = self.a_report(severity="Hospitalisation")
		self.assertIn("24 hours", data["urgent_obligations"][0])

	def test_a_first_aid_case_carries_none(self):
		self.assertEqual(self.a_report(severity="First Aid")["urgent_obligations"], [])

	def test_the_suggested_steps_are_offered_not_created(self):
		"""A server that silently created four tasks on every near miss would
		fill a dispatch board with work nobody chose."""
		data = self.a_report()
		self.assertTrue(data["suggested_subtasks"])
		self.assertEqual(STORE.tables.get("Farm Task", {}), {})

	def test_a_report_before_it_happened_is_refused(self):
		self.assertIn(
			"before the incident happened",
			self.tool_error(
				"create_accident_report",
				{
					"occurred_at": str(frappe.utils.add_to_date(frappe.utils.now(), hours=5)),
					"incident_description": "x",
				},
			),
		)


# ── 2. it spans days ────────────────────────────────────────────────────────
class ItSpansDays(AccidentTestCase):
	def test_an_update_moves_it_off_open(self):
		"""A board where everything reads Open cannot show what is being
		investigated and what was filed and forgotten."""
		report = self.a_report()["name"]
		data = self.tool_data(
			"update_accident_investigation",
			{"report": report, "narrative": "Interviewed Miguel. He was on the far side."},
		)
		self.assertEqual(data["status"], "In Progress")

	def test_the_narrative_appends_rather_than_replaces(self):
		report = self.a_report()["name"]
		for text in ("Day one: took Miguel's account.", "Day two: pulled the maintenance log."):
			self.tool_data("update_accident_investigation", {"report": report, "narrative": text})
		notes = self.tool_data("list_task_notes", {"doctype": "Accident Report", "name": report})["notes"]
		self.assertEqual(len(notes), 2)

	def test_a_revised_root_cause_replaces_and_the_old_one_survives_in_the_narrative(self):
		"""'What did we think on Tuesday' is a question a hearing asks."""
		report = self.a_report()["name"]
		self.tool_data(
			"update_accident_investigation",
			{"report": report, "root_cause": "Operator error.", "narrative": "First read: operator error."},
		)
		self.tool_data(
			"update_accident_investigation",
			{
				"report": report,
				"root_cause": "The guard was removed in June to clear a jam and nobody owns the checklist.",
				"narrative": "Revised: the guard had been off since June.",
			},
		)
		data = self.tool_data("get_accident_report", {"report": report})
		self.assertIn("checklist", data["root_cause"])
		self.assertTrue(any("operator error" in note["narrative"].lower() for note in data["notes"]))

	def test_the_activity_log_groups_by_day(self):
		"""'Nothing happened between the 3rd and the 11th' is a finding about
		the investigation."""
		report = self.a_report(narrative="Opened at the scene.")["name"]
		self.tool_data("update_accident_investigation", {"report": report, "narrative": "More."})
		data = self.tool_data("get_accident_report", {"report": report})
		self.assertGreaterEqual(data["days_active"], 1)
		self.assertTrue(data["activity_by_day"])

	def test_an_update_with_nothing_in_it_is_refused(self):
		report = self.a_report()["name"]
		self.assertIn(
			"nothing to update",
			self.tool_error("update_accident_investigation", {"report": report}),
		)

	def test_sub_tasks_hold_the_work_and_block_the_close(self):
		report = self.a_report()["name"]
		self.tool_data(
			"create_farm_task",
			{
				"task_name": "Pull the camera footage",
				"task_type": "Other",
				"evidence_required": {"photos": True},
				"company": MAIN,
				"parent_task": report,
			},
		)
		data = self.tool_data("get_accident_report", {"report": report})
		self.assertEqual(data["subtask_count"], 1)
		self.assertIn("open_subtask", [item["item"] for item in data["outstanding"]])

	def test_nothing_closes_it_automatically(self):
		report = self.a_report()["name"]
		from erpnext_mcp import hooks

		flat = [
			path
			for group in hooks.scheduler_events.values()
			for path in (group if isinstance(group, list) else [p for paths in group.values() for p in paths])
		]
		self.assertFalse([path for path in flat if "accident" in path])
		self.assertEqual(STORE.get_raw("Accident Report", report)["status"], "Open")


# ── 3. recordability is decided, not inferred ───────────────────────────────
class RecordabilityIsDecidedNotInferred(AccidentTestCase):
	def test_deciding_it_requires_the_basis(self):
		report = self.a_report()["name"]
		self.assertIn(
			"osha_determination_basis is required",
			self.tool_error("update_accident_investigation", {"report": report, "osha_recordable": "Yes"}),
		)

	def test_the_basis_is_stored_with_the_determination(self):
		report = self.a_report()["name"]
		data = self.tool_data(
			"update_accident_investigation",
			{
				"report": report,
				"osha_recordable": "Yes",
				"osha_determination_basis": "Sutures — medical treatment beyond first aid, 1904.7(b)(5).",
			},
		)
		self.assertEqual(data["osha_recordable"], "Yes")
		self.assertIn("1904.7", data["osha_determination_basis"])

	def test_severity_does_not_decide_it(self):
		"""It would be easy to map severity onto the 300 log and wrong."""
		self.assertEqual(self.a_report(severity="Hospitalisation")["osha_recordable"], "Undetermined")

	def test_an_undetermined_report_is_named_in_the_register(self):
		self.a_report()
		data = self.tool_data("list_accident_reports", {"company": MAIN})
		self.assertEqual(data["undetermined_count"], 1)
		self.assertTrue(data["undetermined_recordability"])

	def test_an_unknown_value_is_refused(self):
		report = self.a_report()["name"]
		self.assertIn(
			"Undetermined, Yes or No",
			self.tool_error("update_accident_investigation", {"report": report, "osha_recordable": "Maybe"}),
		)


# ── 4. closing is checked ───────────────────────────────────────────────────
class ClosingIsChecked(AccidentTestCase):
	def _ready(self, **overrides):
		report = self.a_report(**overrides)["name"]
		self.tool_data(
			"update_accident_investigation",
			{
				"report": report,
				"root_cause": "The guard had been off since June.",
				"corrective_actions": "Guard refitted; the line checklist now names an owner.",
				"osha_recordable": "Yes",
				"osha_determination_basis": "Sutures, 1904.7(b)(5).",
				"followup_date": str(frappe.utils.add_days(frappe.utils.today(), 30)),
				"statement_taken_from": "Miguel Soto",
			},
		)
		return report

	def test_it_closes_when_everything_is_there(self):
		report = self._ready()
		data = self.tool_data("close_accident_investigation", {"report": report})
		self.assertEqual(data["status"], "Closed")

	def test_it_will_not_close_with_no_corrective_action(self):
		report = self.a_report()["name"]
		error = self.tool_error("close_accident_investigation", {"report": report})
		self.assertIn("corrective_actions", error)

	def test_the_refusal_names_everything_at_once(self):
		"""A close that fails four times, each naming one more missing field, is
		how somebody learns to stop closing things."""
		report = self.a_report()["name"]
		error = self.tool_error("close_accident_investigation", {"report": report})
		for item in ("osha_determination", "root_cause", "corrective_actions", "followup_date"):
			self.assertIn(item, error)

	def test_an_untaken_witness_statement_blocks_it(self):
		"""'We knew they saw it and never asked' is the finding that survives
		every other one."""
		report = self.a_report()["name"]
		error = self.tool_error(
			"close_accident_investigation",
			{
				"report": report,
				"corrective_actions": "x",
				"followup_date": str(frappe.utils.add_days(frappe.utils.today(), 30)),
				"osha_recordable": "No",
				"osha_determination_basis": "First aid only.",
			},
		)
		self.assertIn("Miguel Soto", error)

	def test_an_open_step_blocks_it(self):
		report = self._ready()
		self.tool_data(
			"create_farm_task",
			{
				"task_name": "Retrain the crew",
				"task_type": "Training",
				"evidence_required": {"photos": True},
				"company": MAIN,
				"parent_task": report,
			},
		)
		self.assertIn(
			"still",
			self.tool_error("close_accident_investigation", {"report": report}),
		)

	def test_closing_writes_a_narrative_entry(self):
		report = self._ready()
		self.tool_data("close_accident_investigation", {"report": report})
		notes = self.tool_data("list_task_notes", {"doctype": "Accident Report", "name": report})["notes"]
		self.assertTrue(any(note["note_type"] == "Corrective Action" for note in notes))

	def test_closing_twice_is_refused(self):
		report = self._ready()
		self.tool_data("close_accident_investigation", {"report": report})
		self.assertIn(
			"already closed",
			self.tool_error("close_accident_investigation", {"report": report}),
		)

	def test_a_closed_report_refuses_a_quiet_update(self):
		report = self._ready()
		self.tool_data("close_accident_investigation", {"report": report})
		self.assertIn(
			"reopen=true",
			self.tool_error(
				"update_accident_investigation", {"report": report, "narrative": "one more thing"}
			),
		)

	def test_it_can_be_reopened_when_new_information_arrives(self):
		report = self._ready()
		self.tool_data("close_accident_investigation", {"report": report})
		data = self.tool_data(
			"update_accident_investigation",
			{"report": report, "reopen": True, "narrative": "The guard came off again in September."},
		)
		self.assertEqual(data["status"], "Corrective Actions Pending")

	def test_status_cannot_be_set_to_closed_by_hand(self):
		report = self.a_report()["name"]
		self.assertIn(
			"close_accident_investigation",
			self.tool_error("update_accident_investigation", {"report": report, "status": "Closed"}),
		)


# ── 5. witnesses are rows ───────────────────────────────────────────────────
class WitnessesAreRowsNotAString(AccidentTestCase):
	def test_a_list_becomes_rows(self):
		data = self.a_report(witnesses=["Miguel Soto", "Rosa Vela"])
		self.assertEqual(len(data["witnesses"]), 2)

	def test_a_comma_separated_string_is_split_rather_than_refused(self):
		"""A handset with only a text box should not be refused — but each name
		still becomes a row, so the outstanding-statement flag works."""
		data = self.a_report(witnesses="Miguel Soto, Rosa Vela")
		self.assertEqual(len(data["witnesses"]), 2)

	def test_a_statement_can_be_marked_taken(self):
		report = self.a_report()["name"]
		self.tool_data(
			"update_accident_investigation",
			{"report": report, "statement_taken_from": "Miguel Soto"},
		)
		witnesses = self.tool_data("get_accident_report", {"report": report})["witnesses"]
		self.assertTrue(witnesses[0]["statement_taken"])

	def test_marking_a_statement_from_somebody_who_is_not_a_witness_is_refused(self):
		report = self.a_report()["name"]
		self.assertIn(
			"is not a witness",
			self.tool_error(
				"update_accident_investigation",
				{"report": report, "statement_taken_from": "Nobody"},
			),
		)

	def test_a_witness_added_later_joins_the_list(self):
		report = self.a_report()["name"]
		self.tool_data("update_accident_investigation", {"report": report, "witnesses": ["Rosa Vela"]})
		self.assertEqual(len(self.tool_data("get_accident_report", {"report": report})["witnesses"]), 2)

	def test_an_outstanding_statement_is_named_on_every_read(self):
		"""An investigation that tells you on day three what it is waiting for is
		one somebody finishes."""
		report = self.a_report()["name"]
		data = self.tool_data("get_accident_report", {"report": report})
		self.assertIn("witness_statement", [item["item"] for item in data["outstanding"]])


# ── 6. the register ─────────────────────────────────────────────────────────
class TheRegisterFilters(AccidentTestCase):
	def test_open_only_excludes_the_closed(self):
		self.a_report()
		data = self.tool_data("list_accident_reports", {"company": MAIN, "open_only": True})
		self.assertEqual(data["open_count"], 1)

	def test_it_filters_by_severity(self):
		self.a_report(severity="Near Miss")
		self.a_report(severity="Lost Time")
		data = self.tool_data("list_accident_reports", {"company": MAIN, "severity": "Near Miss"})
		self.assertEqual(data["report_count"], 1)

	def test_it_totals_the_days_away(self):
		report = self.a_report()["name"]
		self.tool_data("update_accident_investigation", {"report": report, "days_away_from_work": 4})
		data = self.tool_data("list_accident_reports", {"company": MAIN})
		self.assertEqual(data["days_away_total"], 4)

	def test_an_unknown_status_is_refused_with_the_list(self):
		error = self.tool_error("list_accident_reports", {"status": "Filed"})
		self.assertIn("Corrective Actions Pending", error)


# ── v0.211.0: where it happened (quick_wins_2026_10.md §1) ──────────────────
class WhereItHappened(AccidentTestCase):
	def test_a_fix_is_stored_and_read_back(self):
		name = self.a_report(latitude=45.601234567, longitude=-121.184321, location_accuracy_m=8.4)["name"]
		row = STORE.get_raw("Accident Report", name)
		self.assertEqual(
			(row["latitude"], row["longitude"], row["location_accuracy_m"]), (45.601235, -121.184321, 8.4)
		)
		read = self.tool_data("get_accident_report", {"report": name})
		self.assertEqual((read["latitude"], read["longitude"]), (45.601235, -121.184321))

	def test_a_forms_pin_is_the_same_fact(self):
		pin = {"type": "Point", "coordinates": [-121.18, 45.6]}
		for point in (pin, json.dumps(pin), "45.6, -121.18"):
			name = self.a_report(location_point=point)["name"]
			row = STORE.get_raw("Accident Report", name)
			self.assertEqual((row["latitude"], row["longitude"]), (45.6, -121.18), point)
			self.assertFalse(row.get("location_accuracy_m"))

	def test_no_fix_is_ordinary(self):
		read = self.tool_data("get_accident_report", {"report": self.a_report()["name"]})
		self.assertEqual(
			(read["latitude"], read["longitude"], read["location_accuracy_m"]), (None, None, None)
		)

	def test_half_a_fix_and_nowhere_are_refused(self):
		for bad, words in (
			({"latitude": 45.6}, "both or neither"),
			({"latitude": 145.6, "longitude": -121.0}, "not a place"),
			({"latitude": "north", "longitude": -121.0}, "must be a number"),
			({"location_point": "somewhere"}, "GeoJSON Point"),
		):
			payload = {
				"occurred_at": _hours_ago(1),
				"incident_description": "A ladder slipped.",
				**bad,
			}
			self.assertIn(words, self.tool_error("create_accident_report", payload))
		self.assertEqual(STORE.rows("Accident Report"), [])

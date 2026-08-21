# SPDX-License-Identifier: MIT
"""The shift — v0.19.3's ten tools, the thirteenth rule, and the Attendance bridge.

THE CLAIM BEHIND THE RELEASE is that compliance anchors to a SHIFT rather than to
a task. A task completion carries a point-in-time reading; a shift carries a
timeline. Oregon OSHA does not ask what the temperature was when one job closed —
it asks whether the July 15 shift complied with OAR 437-004-1131 from start to
finish, and only a record spanning the exposure period can answer.

TEN CLAIMS.

1. `TheForemanFormsTheCrew` — start_shift creates the record, rosters everybody
   with joined_at at the SHIFT's start rather than at the moment the call landed,
   and refuses a crew crossing entities before writing anything.

2. `TheCrewIsAnEnvelope` — a worker added mid-shift joins at NOW, a worker
   removed keeps their row with left_at set, and neither can be duplicated. The
   row is the only record that somebody was on the shift at all.

3. `TheTimelineIsTheEvidence` — log_shift_event writes ordered events with
   producer references, and an event outside the shift is kept and reported
   rather than refused.

4. `TheCloseIsAnAttestation` — end_shift refuses without a signature, sets the
   status and the review timestamp with one, and refuses a second close.

5. `TheAttendanceBridge` — one submitted Attendance per crew member spanning
   THAT PERSON'S own joined_at to their own left_at, not the shift's. The one
   worth testing is the worker who joined at T+1h and left at T+3h.

6. `TheHeatRecord` — one per shift, the second refused by name; unsigned
   refused; signs observed with no response and no explanation refused; a
   training claim the register contradicts refused.

7. `TheSupervisorReviewRule` — the thirteenth rule fires Warning at a fortnight,
   Critical past thirty days, and auto-dismisses the moment somebody signs.

8. `TheGuards` — the role gate, the company scope and the kill switch, on all
   ten tools.

9. `ReadingItBack` — list_shifts and get_shift, including the computed status
   and the evidence chain get_shift is for.

10. `TwoPhonesOneCrew` — every tool that saves the shift document reads its
    state AFTER taking the row lock, so a close, a cancel or a second roster
    landing in the gap is seen rather than written over.
"""

import frappe

from erpnext_mcp import compliance_fields, registry, shifts
from erpnext_mcp.alerts import base as alerts_base
from erpnext_mcp.alerts import rules as alert_rules

from .fixtures import MAIN, OTHER, V12TestCase, install_hrms
from .harness import ROLES, STORE, set_roles

#: Every switch this suite needs. Listed rather than globbed so that turning one
#: off in a test is visibly a change from the on-by-default posture.
ON = {
	f"allow_{name}": 1
	for name in (
		"start_shift",
		"add_worker_to_shift",
		"remove_worker_from_shift",
		"log_shift_event",
		"end_shift",
		"cancel_shift",
		"create_heat_exposure_event",
		"list_shifts",
		"get_shift",
		"list_heat_exposure_events",
		"get_heat_exposure_event",
		"record_training",
		"sign_training_supervisor_review",
		"refresh_compliance_alerts",
		"get_compliance_calendar",
		"get_attendance_summary",
	)
}

FOREMAN = "HR-EMP-00001"  # Ada Orchard, Active, at MAIN
WORKER = "HR-EMP-00002"  # Ben Packhouse, Active, at MAIN

#: Four more at MAIN, so a five-person crew is a real five-person crew rather
#: than the same two people counted twice.
CREW = ("HR-EMP-00010", "HR-EMP-00011", "HR-EMP-00012", "HR-EMP-00013")

SIGNATURE = "/files/ada-shift-signature.png"


def at(hour: int, minute: int = 0, day: str = "") -> str:
	"""A timestamp on a fixed day, so a span assertion is arithmetic not luck."""
	day = day or frappe.utils.today()
	return f"{day} {hour:02d}:{minute:02d}:00"


def days_out(count: int) -> str:
	return str(frappe.utils.add_days(frappe.utils.today(), count))


class ShiftTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		# The double ships without an Employee register — Frappe HR is a separate
		# app, and every one of these tools refuses on a site that has none.
		install_hrms()
		STORE.seed(
			"Employee",
			[
				{
					"name": name,
					"employee_name": f"Picker {index}",
					"status": "Active",
					"date_of_joining": "2026-06-01",
					"company": MAIN,
				}
				for index, name in enumerate(CREW, start=1)
			],
		)
		# The Attendance bridge writes `Attendance.farm_shift`, which is a Custom
		# Field this app installs on every migrate. Installing it here rather than
		# registering the column by hand is what makes the bridge's own
		# "this site has no such column" branch reachable AND the happy path real:
		# a fixture that hand-registered the field would test a schema no site has.
		compliance_fields.install_compliance_fields(respect_switch=False)
		self._roles_before = {user: list(held) for user, held in ROLES.items()}
		self.addCleanup(self._restore_roles)

	def _restore_roles(self):
		ROLES.clear()
		ROLES.update(self._roles_before)

	# -- helpers -------------------------------------------------------------
	def start(self, **overrides):
		payload = {
			"foreman": FOREMAN,
			"location": "Block 7 North",
			"shift_type": "Harvest",
			"farm_location_gps": "45.52,-122.68",
			"start_datetime": at(6),
			"crew_employees": [WORKER, *CREW],
		}
		payload.update(overrides)
		return self.tool_data("start_shift", payload)

	def start_error(self, **overrides):
		payload = {
			"foreman": FOREMAN,
			"location": "Block 7 North",
			"shift_type": "Harvest",
			"start_datetime": at(6),
			"crew_employees": [WORKER],
		}
		payload.update(overrides)
		return self.tool_error("start_shift", payload)

	def close(self, shift: str, **overrides):
		payload = {
			"shift": shift,
			"end_datetime": at(15),
			"supervisor_signature_file_token": SIGNATURE,
		}
		payload.update(overrides)
		return self.tool_data("end_shift", payload)

	def raw(self, name: str) -> dict:
		return dict(STORE.get_raw(shifts.DOCTYPE, name) or {})

	def crew_rows(self, name: str) -> list:
		return list(self.raw(name).get("crew") or [])

	def attendance(self) -> list:
		return [dict(row) for row in STORE.rows("Attendance") if row.get("farm_shift")]

	def heat(self, shift: str, **overrides):
		payload = {
			"farm_shift": shift,
			"supervisor_signature_file_token": SIGNATURE,
			"water_provided": True,
			"shade_provided": True,
			"mandatory_rest_taken": True,
			"heat_illness_signs_observed": False,
			"worker_reported_symptoms": False,
			"emergency_response_activated": False,
			"training_verified": False,
			"max_temp_f": 96,
			"max_heat_index_f": 101,
		}
		payload.update(overrides)
		return self.tool_data("create_heat_exposure_event", payload)

	def heat_error(self, shift: str, **overrides):
		payload = {
			"farm_shift": shift,
			"supervisor_signature_file_token": SIGNATURE,
			"water_provided": True,
			"shade_provided": True,
			"mandatory_rest_taken": True,
			"training_verified": False,
		}
		payload.update(overrides)
		return self.tool_error("create_heat_exposure_event", payload)

	def sweep(self, alert_type="supervisor_review_lapsed") -> list:
		"""Run the compliance sweep and return this rule's live alerts."""
		self.tool_data("refresh_compliance_alerts", {"company": MAIN})
		return [
			row
			for row in STORE.rows("Compliance Alert")
			if row.get("alert_type") == alert_type
			and str(row.get("dismissed") or "0").strip().lower() in ("0", "", "false", "none")
		]


# ── 1 ───────────────────────────────────────────────────────────────────────
class TheForemanFormsTheCrew(ShiftTestCase):
	def test_five_on_the_crew_becomes_five_rows_at_the_shifts_own_start(self):
		"""THE DEFAULT THAT MATTERS. Everybody rostered at the beginning was there
		at the beginning; stamping them with the moment the API call landed would
		shave minutes off every one of their days."""
		data = self.start()
		rows = self.crew_rows(data["name"])
		self.assertEqual(len(rows), 5)
		self.assertEqual({row["employee"] for row in rows}, {WORKER, *CREW})
		for row in rows:
			self.assertEqual(str(row["joined_at"]), at(6))
			self.assertIn(row["role"], ("Worker",))
			self.assertFalse(row.get("left_at"))

	def test_the_docname_is_the_year_the_shift_started(self):
		data = self.start(start_datetime="2026-12-31 22:00:00")
		self.assertTrue(data["name"].startswith("SHIFT-2026-"), data["name"])

	def test_the_shift_is_open_and_its_status_says_so(self):
		data = self.start()
		self.assertTrue(data["open"])
		self.assertEqual(data["status"], shifts.STATUS_ACTIVE)
		self.assertIsNone(data["end_datetime"])

	def test_the_foreman_and_the_company_are_snapshotted_off_the_employee(self):
		data = self.start()
		row = self.raw(data["name"])
		self.assertEqual(row["foreman"], FOREMAN)
		self.assertEqual(row["foreman_name"], "Ada Orchard")
		self.assertEqual(row["company"], MAIN)

	def test_a_shift_with_no_crew_is_allowed_and_says_what_that_means(self):
		"""A foreman opening a shift at five in the morning before the crew has
		arrived is the ordinary case, not an error."""
		data = self.start(crew_employees=[])
		self.assertEqual(data["crew_size"], 0)
		self.assertIn("NOBODY IS ON THIS CREW YET", data["crew_note"])

	def test_a_crew_member_from_another_entity_is_refused_before_anything_is_written(self):
		"""Refused BEFORE the insert, because a shift half-created and then
		rejected leaves an open shift nobody meant to open."""
		STORE.seed(
			"Employee",
			[
				{
					"name": "HR-EMP-00099",
					"employee_name": "Outsider",
					"status": "Active",
					"company": OTHER,
				}
			],
		)
		message = self.start_error(crew_employees=[WORKER, "HR-EMP-00099"])
		self.assertIn("HR-EMP-00099", message)
		self.assertIn("Nothing was created", message)
		self.assertEqual(STORE.rows(shifts.DOCTYPE), [])

	def test_a_foreman_from_another_entity_is_refused(self):
		message = self.start_error(company=OTHER)
		self.assertIn("A shift belongs to the entity whose crew worked it", message)

	def test_a_shift_with_no_gps_is_told_it_will_get_no_weather_timeline(self):
		"""A point-in-time temperature is a data point and a timeline is a
		defence — and the fetch asks Open-Meteo about a PLACE."""
		data = self.start(farm_location_gps=None)
		self.assertIn("NO weather timeline", data["weather_note"])

	def test_a_crew_past_the_cap_is_refused_with_the_reason(self):
		message = self.start_error(crew_employees=[WORKER] * 61)
		self.assertIn("past the 60 cap", message)
		self.assertIn("wrong Attendance row", message)


# ── 2 ───────────────────────────────────────────────────────────────────────
class TheCrewIsAnEnvelope(ShiftTestCase):
	def test_a_worker_added_after_the_start_joins_now_not_at_the_start(self):
		"""The opposite default from start_shift, and right for the same reason:
		a worker added mid-shift arrived when somebody said so."""
		shift = self.start(crew_employees=[WORKER])["name"]
		data = self.tool_data(
			"add_worker_to_shift", {"shift": shift, "employee": CREW[0], "joined_at": at(9, 30)}
		)
		rows = self.crew_rows(shift)
		self.assertEqual(len(rows), 2)
		added = next(row for row in rows if row["employee"] == CREW[0])
		self.assertEqual(str(added["joined_at"]), at(9, 30))
		self.assertEqual(data["added"]["hours_after_shift_start"], 3.5)

	def test_the_sixth_crew_member_is_the_sixth_row(self):
		shift = self.start()["name"]
		self.tool_data("add_worker_to_shift", {"shift": shift, "employee": FOREMAN})
		self.assertEqual(len(self.crew_rows(shift)), 6)

	def test_joined_at_defaults_to_now_when_nobody_says(self):
		shift = self.start(crew_employees=[WORKER])["name"]
		self.tool_data("add_worker_to_shift", {"shift": shift, "employee": CREW[0]})
		added = next(row for row in self.crew_rows(shift) if row["employee"] == CREW[0])
		self.assertTrue(str(added["joined_at"]).startswith(frappe.utils.today()))

	def test_removing_a_worker_populates_left_at_and_keeps_the_row(self):
		"""THE WHOLE POINT OF THE TOOL'S SHAPE. The row is the only record that
		this person was on this shift at all — which is what a wage claim turns
		on, and what says who was exposed before they were sent home."""
		shift = self.start()["name"]
		self.tool_data("add_worker_to_shift", {"shift": shift, "employee": FOREMAN, "joined_at": at(7)})
		data = self.tool_data(
			"remove_worker_from_shift", {"shift": shift, "employee": FOREMAN, "left_at": at(11)}
		)
		rows = self.crew_rows(shift)
		self.assertEqual(len(rows), 6, "the row must survive the removal")
		gone = next(row for row in rows if row["employee"] == FOREMAN)
		self.assertEqual(str(gone["left_at"]), at(11))
		self.assertEqual(data["removed"]["hours_present"], 4.0)
		self.assertIn("THE ROW IS STILL THERE", data["note"])

	def test_the_same_person_twice_is_refused_with_what_it_would_have_cost(self):
		shift = self.start()["name"]
		message = self.tool_error("add_worker_to_shift", {"shift": shift, "employee": WORKER})
		self.assertIn("already on this crew", message)
		self.assertIn("two Attendance days", message)

	def test_removing_somebody_twice_without_a_time_is_refused(self):
		"""A silent second call would move a departure that has already happened
		to now, and lengthen a day that has already ended."""
		shift = self.start()["name"]
		self.tool_data("remove_worker_from_shift", {"shift": shift, "employee": WORKER, "left_at": at(11)})
		message = self.tool_error("remove_worker_from_shift", {"shift": shift, "employee": WORKER})
		self.assertIn("already left", message)
		self.assertIn("lengthen a day that has already ended", message)

	def test_removing_somebody_who_is_not_on_the_crew_is_refused(self):
		shift = self.start(crew_employees=[WORKER])["name"]
		message = self.tool_error("remove_worker_from_shift", {"shift": shift, "employee": CREW[0]})
		self.assertIn("is not on the crew", message)

	def test_adding_to_a_closed_shift_is_refused(self):
		shift = self.start()["name"]
		self.close(shift)
		message = self.tool_error("add_worker_to_shift", {"shift": shift, "employee": FOREMAN})
		self.assertIn("Nobody joins a shift that is over", message)

	def test_a_crew_row_that_left_before_it_joined_is_refused_by_the_controller(self):
		shift = self.start(crew_employees=[WORKER])["name"]
		message = self.tool_error(
			"remove_worker_from_shift", {"shift": shift, "employee": WORKER, "left_at": at(4)}
		)
		self.assertIn("negative span", message)


# ── 3 ───────────────────────────────────────────────────────────────────────
class TheTimelineIsTheEvidence(ShiftTestCase):
	def test_a_water_break_becomes_one_event_with_its_time(self):
		shift = self.start()["name"]
		data = self.tool_data(
			"log_shift_event",
			{"shift": shift, "event_type": "Water Break", "event_datetime": at(9, 15)},
		)
		self.assertEqual(data["compliance_event_count"], 1)
		event = data["compliance_events"][0]
		self.assertEqual(event["event_type"], "Water Break")
		self.assertEqual(event["event_datetime"], at(9, 15))
		self.assertEqual(event["logged_by"], FOREMAN)

	def test_the_event_datetime_defaults_to_now_rather_than_to_a_date(self):
		"""Everything on a shift is answered at the minute: an hour between water
		breaks and three hours between them are different shifts."""
		shift = self.start()["name"]
		data = self.tool_data("log_shift_event", {"shift": shift, "event_type": "Shade Break"})
		stamp = data["compliance_events"][0]["event_datetime"]
		self.assertTrue(stamp.startswith(frappe.utils.today()))
		self.assertNotEqual(stamp[11:], "00:00:00")

	def test_a_producer_record_reference_is_kept_in_both_halves(self):
		shift = self.start()["name"]
		data = self.tool_data(
			"log_shift_event",
			{
				"shift": shift,
				"event_type": "Supervisor Observation",
				"event_datetime": at(13, 5),
				"producer_record_doctype": "Farm Task Assignment",
				"producer_record_name": "FTA-00004",
				"description": "Walked the row, nobody labouring.",
			},
		)
		event = data["compliance_events"][0]
		self.assertEqual(event["producer_record_doctype"], "Farm Task Assignment")
		self.assertEqual(event["producer_record_name"], "FTA-00004")
		self.assertEqual(event["producer_record"], "Farm Task Assignment FTA-00004")

	def test_a_docname_with_no_doctype_is_refused(self):
		"""A name with nowhere to look it up is a string, not a reference."""
		shift = self.start()["name"]
		message = self.tool_error(
			"log_shift_event",
			{"shift": shift, "event_type": "Other", "producer_record_name": "FTA-00004"},
		)
		self.assertIn("cannot be followed to anything", message)

	def test_the_timeline_comes_back_in_time_order_not_entry_order(self):
		"""An event logged late in the afternoon about the morning belongs where
		it happened."""
		shift = self.start()["name"]
		for hour in (13, 9, 11):
			self.tool_data(
				"log_shift_event",
				{"shift": shift, "event_type": "Water Break", "event_datetime": at(hour)},
			)
		data = self.tool_data("get_shift", {"name": shift})
		stamps = [event["event_datetime"] for event in data["compliance_events"]]
		self.assertEqual(stamps, sorted(stamps))

	def test_an_event_before_the_shift_started_is_kept_and_reported(self):
		"""A clock five minutes out is not a false record, and refusing would
		mean the break goes unlogged rather than logged approximately."""
		shift = self.start()["name"]
		data = self.tool_data(
			"log_shift_event",
			{"shift": shift, "event_type": "Water Break", "event_datetime": at(5, 55)},
		)
		self.assertEqual(data["compliance_event_count"], 1)
		self.assertIn("before the shift began", data["timing_note"])

	def test_an_evidence_file_that_is_not_on_this_site_is_refused(self):
		shift = self.start()["name"]
		message = self.tool_error(
			"log_shift_event",
			{"shift": shift, "event_type": "Water Break", "evidence_file_token": "not-a-real-file"},
		)
		self.assertIn("not a File on this site", message)

	def test_a_lead_worker_can_be_named_instead_of_the_foreman(self):
		"""A lead worker calling a break at the far end of the block is the
		ordinary case, and attributing it to the foreman would be wrong in
		exactly the place an investigator looks."""
		shift = self.start()["name"]
		data = self.tool_data(
			"log_shift_event", {"shift": shift, "event_type": "Water Break", "logged_by": WORKER}
		)
		self.assertEqual(data["compliance_events"][0]["logged_by"], WORKER)


# ── 3b ──────────────────────────────────────────────────────────────────────
class TheThirdEndingIsACancellation(ShiftTestCase):
	"""`cancel_shift`: the shift was formed and then not worked.

	THE TWO ENDINGS BEFORE IT WERE BOTH WRONG FOR THIS DAY. Left open, the shift
	is walked by the weather sweep for ever and reported by `list_shifts` as work
	in progress. Closed with a signature, it files a §112.161(b) attestation that
	a day happened and writes an Attendance row per crew member for a day nobody
	worked — which payroll pays.
	"""

	def cancel(self, shift: str, **overrides):
		payload = {"shift": shift, "cancellation_reason": "crew stood down at 06:40, heat index 94 °F"}
		payload.update(overrides)
		return self.tool_data("cancel_shift", payload)

	def test_a_cancelled_shift_is_cancelled_not_closed_and_pays_nobody(self):
		shift = self.start()["name"]
		data = self.cancel(shift, cancelled_at=at(6, 40))

		row = self.raw(shift)
		self.assertEqual(shifts.describe(row)["status"], shifts.STATUS_CANCELLED)
		self.assertTrue(row["cancelled"])
		self.assertIn("heat index", row["cancellation_reason"])
		# THE ASSERTION THE WHOLE TOOL EXISTS FOR.
		self.assertEqual(self.attendance(), [])
		self.assertEqual(data["attendance_created"], 0)

	def test_the_end_time_is_set_or_the_shift_would_still_be_active(self):
		"""`status_for` reads `end_datetime` FIRST — a Cancelled tick with no end
		time is an Active shift the weather sweep keeps walking."""
		shift = self.start()["name"]
		self.cancel(shift, cancelled_at=at(6, 40))
		self.assertEqual(self.raw(shift)["end_datetime"], at(6, 40))
		self.assertFalse(shifts.is_open(self.raw(shift)))

	def test_the_crew_rows_survive_because_they_turned_up(self):
		"""'They were rostered and stood down' is what answers a wage claim from
		somebody who drove in for nothing."""
		shift = self.start()["name"]
		self.cancel(shift)
		self.assertEqual(len(self.crew_rows(shift)), len([WORKER, *CREW]))

	def test_a_cancellation_with_no_reason_is_refused_and_the_shift_stays_open(self):
		shift = self.start()["name"]
		message = self.tool_error("cancel_shift", {"shift": shift})
		self.assertIn("cancellation_reason is required", message)
		self.assertIn("THE SHIFT IS STILL OPEN", message)
		self.assertEqual(shifts.describe(self.raw(shift))["status"], shifts.STATUS_ACTIVE)

	def test_a_closed_shift_cannot_be_cancelled_afterwards(self):
		"""The Attendance rows are already written. Cancelling would claim the day
		was not worked while the rows saying it was stay on the register."""
		shift = self.start()["name"]
		self.close(shift)
		message = self.tool_error("cancel_shift", {"shift": shift, "cancellation_reason": "changed my mind"})
		self.assertIn("was CLOSED", message)
		self.assertIn("Attendance", message)
		self.assertEqual(len(self.attendance()), len([WORKER, *CREW]))

	def test_a_second_cancellation_is_refused_and_names_the_first_reason(self):
		shift = self.start()["name"]
		self.cancel(shift)
		message = self.tool_error("cancel_shift", {"shift": shift, "cancellation_reason": "again"})
		self.assertIn("already cancelled", message)
		self.assertIn("heat index", message)

	def test_a_cancellation_before_the_shift_started_is_refused(self):
		shift = self.start()["name"]
		message = self.tool_error(
			"cancel_shift",
			{"shift": shift, "cancellation_reason": "never happened", "cancelled_at": at(5)},
		)
		self.assertIn("before it was formed", message)

	def test_a_timeline_on_a_cancelled_day_is_kept_and_flagged(self):
		"""A water break called before the stand-down happened, and a
		cancellation does not unhappen it — but a crew that needed water may be
		owed the hours."""
		shift = self.start()["name"]
		self.tool_data("log_shift_event", {"shift": shift, "event_type": "Water Break"})
		data = self.cancel(shift)
		self.assertEqual(len(data["compliance_events"]), 1)
		self.assertIn("may be owed the hours", data["timeline_note"])

	def test_the_crew_is_free_to_be_rostered_again_once_it_is_cancelled(self):
		"""The point of cancelling rather than leaving it open: a stood-down crew
		is not on a shift, so the day can be started again when the weather
		lifts."""
		first = self.start()["name"]
		self.cancel(first)
		second = self.start(start_datetime=at(10))
		self.assertNotEqual(second["name"], first)
		self.assertEqual(second["crew_size"], len([WORKER, *CREW]))


# ── 3c ──────────────────────────────────────────────────────────────────────
class NobodyIsOnTwoOpenShiftsAtOnce(ShiftTestCase):
	"""The cross-shift guard, and why the same-crew dedup does not cover it.

	The controller refuses one Employee twice on ONE crew — two rows on one form,
	visible to whoever is looking. This is the other shape: the block foreman
	rosters Ana at six, the packing shed's lead rosters her at ten on a shift of
	their own, and NEITHER FORM SHOWS THE OTHER. Both close, the bridge writes one
	Attendance row per crew row, and Ana is paid twice for one day out of two
	records that each look correct.
	"""

	def test_a_second_open_shift_cannot_roster_somebody_already_on_one(self):
		first = self.start()["name"]
		message = self.start_error(start_datetime=at(10), crew_employees=[WORKER])
		self.assertIn(first, message)
		self.assertIn("already on", message)
		self.assertIn("two overlapping days", message)

	def test_the_refusal_leaves_no_half_built_shift_behind(self):
		"""Checked in the same pre-write pass as the company check, and for the
		same reason: a shift refused on its ninth crew member must not leave an
		open shift nobody meant to open."""
		before = len(STORE.rows(shifts.DOCTYPE))
		self.start()
		self.start_error(start_datetime=at(10), crew_employees=[WORKER])
		self.assertEqual(len(STORE.rows(shifts.DOCTYPE)), before + 1)

	def test_add_worker_to_shift_asks_the_same_question_of_every_other_shift(self):
		"""The loop over `doc.crew` cannot answer it: a worker on a second crew is
		invisible from the shift being added to."""
		first = self.start()["name"]
		second = self.start(start_datetime=at(10), crew_employees=[])["name"]
		message = self.tool_error("add_worker_to_shift", {"shift": second, "employee": WORKER})
		self.assertIn(first, message)
		self.assertIn("remove_worker_from_shift", message)

	def test_somebody_clocked_out_of_the_first_shift_may_join_the_second(self):
		"""`left_at` is what ends a span. Somebody sent home from the block at
		eleven and put on the packing line at noon is one person working one day,
		and refusing that would be a guard nobody could work around."""
		first = self.start()["name"]
		self.tool_data("remove_worker_from_shift", {"shift": first, "employee": WORKER, "left_at": at(11)})
		second = self.start(start_datetime=at(12), crew_employees=[WORKER])
		self.assertEqual(second["crew_size"], 1)

	def test_a_closed_shift_holds_nobody(self):
		first = self.start()["name"]
		self.close(first)
		self.assertEqual(self.start(start_datetime=at(16))["crew_size"], len([WORKER, *CREW]))

	def test_the_same_shift_is_not_its_own_second_shift(self):
		"""`exclude` on the add path. Without it every add_worker_to_shift call
		would refuse against the shift it is adding to."""
		shift = self.start(crew_employees=[])["name"]
		self.assertTrue(self.tool_data("add_worker_to_shift", {"shift": shift, "employee": WORKER}))


# ── 4 ───────────────────────────────────────────────────────────────────────
class TheCloseIsAnAttestation(ShiftTestCase):
	def test_a_close_without_a_signature_is_refused_and_the_shift_stays_open(self):
		"""An unsigned close is an UPDATE setting a timestamp. §112.161(b) asks
		for a review that is dated AND signed."""
		shift = self.start()["name"]
		message = self.tool_error("end_shift", {"shift": shift, "end_datetime": at(15)})
		self.assertIn("supervisor_signature_file_token is required", message)
		self.assertIn("THE SHIFT IS STILL OPEN", message)
		self.assertEqual(shifts.describe(self.raw(shift))["status"], shifts.STATUS_ACTIVE)

	def test_a_signed_close_sets_the_status_and_the_review_timestamp(self):
		shift = self.start()["name"]
		data = self.close(shift)
		row = self.raw(shift)
		self.assertEqual(data["status"], shifts.STATUS_CLOSED)
		self.assertFalse(data["open"])
		self.assertEqual(str(row["end_datetime"]), at(15))
		self.assertEqual(row["supervisor_review_signature"], SIGNATURE)
		self.assertTrue(row["supervisor_review_on"])
		self.assertTrue(data["supervisor_reviewed"])

	def test_closing_twice_is_refused_before_a_second_set_of_payroll_rows(self):
		shift = self.start()["name"]
		self.close(shift)
		message = self.tool_error("end_shift", {"shift": shift, "supervisor_signature_file_token": SIGNATURE})
		self.assertIn("already closed", message)
		self.assertIn("second set of Attendance rows", message)

	def test_an_end_before_the_start_is_refused(self):
		shift = self.start()["name"]
		message = self.tool_error(
			"end_shift",
			{"shift": shift, "end_datetime": at(4), "supervisor_signature_file_token": SIGNATURE},
		)
		self.assertIn("finished before it began", message)
		self.assertIn("negative span", message)

	def test_an_end_before_somebody_left_is_refused(self):
		shift = self.start()["name"]
		self.tool_data("remove_worker_from_shift", {"shift": shift, "employee": WORKER, "left_at": at(14)})
		message = self.tool_error(
			"end_shift",
			{"shift": shift, "end_datetime": at(12), "supervisor_signature_file_token": SIGNATURE},
		)
		self.assertIn("Nobody is on a shift that is over", message)

	def test_a_close_with_an_empty_timeline_says_what_that_reads_as(self):
		"""Recorded rather than refused — a shift where nothing needed logging is
		a real shift — but on a hot day it is the absence an inspector reads as
		'no water breaks were called'."""
		shift = self.start()["name"]
		data = self.close(shift)
		self.assertIn("NOTHING WAS LOGGED", data["timeline_note"])

	def test_the_shift_hours_are_reported(self):
		shift = self.start()["name"]
		self.assertEqual(self.close(shift)["shift_hours"], 9.0)


# ── 5 ───────────────────────────────────────────────────────────────────────
class TheAttendanceBridge(ShiftTestCase):
	def test_closing_writes_one_submitted_attendance_per_crew_member(self):
		shift = self.start()["name"]
		data = self.close(shift)
		rows = self.attendance()
		self.assertEqual(len(rows), 5)
		self.assertEqual(data["attendance_created"], 5)
		self.assertEqual({row["employee"] for row in rows}, {WORKER, *CREW})
		for row in rows:
			self.assertEqual(row["farm_shift"], shift)
			self.assertEqual(row["status"], shifts.ATTENDANCE_STATUS)
			self.assertEqual(row["company"], MAIN)
			# Submitted, because `get_attendance_summary` counts docstatus 1 only —
			# a bridge that left drafts would produce a payroll register that read
			# as empty for every shift-formed day.
			self.assertEqual(int(row["docstatus"]), 1)

	def test_everybody_who_stayed_to_the_end_spans_the_whole_shift(self):
		shift = self.start()["name"]
		self.close(shift)
		for row in self.attendance():
			self.assertEqual(str(row["in_time"]), at(6))
			self.assertEqual(str(row["out_time"]), at(15))
			self.assertEqual(row["working_hours"], 9.0)

	def test_a_worker_who_joined_at_one_hour_and_left_at_three_gets_their_own_span(self):
		"""THE TEST THIS BRIDGE EXISTS FOR. A worker who arrived an hour late and
		left two hours early worked two hours of a nine-hour shift, and a row
		claiming nine is wrong in the employer's favour — which is the direction
		that gets litigated."""
		shift = self.start(crew_employees=[WORKER])["name"]
		self.tool_data("add_worker_to_shift", {"shift": shift, "employee": CREW[0], "joined_at": at(7)})
		self.tool_data("remove_worker_from_shift", {"shift": shift, "employee": CREW[0], "left_at": at(9)})
		self.close(shift)

		rows = {row["employee"]: row for row in self.attendance()}
		partial = rows[CREW[0]]
		self.assertEqual(str(partial["in_time"]), at(7))
		self.assertEqual(str(partial["out_time"]), at(9))
		self.assertEqual(partial["working_hours"], 2.0)
		# And the person who stayed is untouched by the other's early departure.
		self.assertEqual(str(rows[WORKER]["out_time"]), at(15))
		self.assertEqual(rows[WORKER]["working_hours"], 9.0)

	def test_the_attendance_date_is_the_day_the_person_started(self):
		shift = self.start()["name"]
		self.close(shift)
		for row in self.attendance():
			self.assertEqual(str(row["attendance_date"]), frappe.utils.today())

	def test_a_site_without_frappe_hr_still_closes_the_shift_and_says_why_no_rows(self):
		"""The signature is the compliance act and the payroll row is the
		convenience. Refusing the close would lose the first to save the second."""
		from .harness import INSTALLED_DOCTYPES

		shift = self.start()["name"]
		INSTALLED_DOCTYPES.discard("Attendance")
		self.addCleanup(INSTALLED_DOCTYPES.add, "Attendance")
		data = self.close(shift)
		self.assertEqual(data["status"], shifts.STATUS_CLOSED)
		self.assertFalse(data["attendance"]["available"])
		self.assertIn("no Attendance DocType", data["attendance"]["note"])

	def test_the_bridge_is_the_only_writer_of_shift_linked_attendance(self):
		"""Nothing else on the site writes `Attendance.farm_shift`, which is what
		lets a re-closed shift be recognised rather than duplicated."""
		shift = self.start()["name"]
		self.close(shift)
		self.assertEqual(len(self.attendance()), 5)
		# A second close is refused outright, so the duplicate cannot happen; the
		# `farm_shift` guard in the bridge is the belt for an amended shift.
		self.tool_error("end_shift", {"shift": shift, "supervisor_signature_file_token": SIGNATURE})
		self.assertEqual(len(self.attendance()), 5)


# ── 6 ───────────────────────────────────────────────────────────────────────
class TheHeatRecord(ShiftTestCase):
	def test_it_documents_a_shift_and_names_itself_by_the_shifts_year(self):
		shift = self.start()["name"]
		data = self.heat(shift)
		self.assertTrue(data["name"].startswith("HEAT-"), data["name"])
		self.assertEqual(data["farm_shift"], shift)
		self.assertEqual(data["foreman"], FOREMAN)
		self.assertEqual(data["company"], MAIN)
		self.assertEqual(data["event_date"], frappe.utils.today())
		self.assertEqual(data["regulation_citation"], shifts.CITATION)
		self.assertTrue(data["submitted"])
		self.assertTrue(data["supervisor_signed_on"])

	def test_a_second_record_for_the_same_shift_is_refused_by_name(self):
		"""Two records about one exposure period will disagree, and the one an
		inspector finds will be whichever was filed second."""
		shift = self.start()["name"]
		first = self.heat(shift)
		message = self.heat_error(shift)
		self.assertIn(first["name"], message)
		self.assertIn("ONE SHIFT HAS AT MOST ONE HEAT RECORD", message)
		self.assertEqual(len(STORE.rows(shifts.HEAT_DOCTYPE)), 1)

	def test_an_unsigned_record_is_refused(self):
		shift = self.start()["name"]
		message = self.tool_error("create_heat_exposure_event", {"farm_shift": shift, "water_provided": True})
		self.assertIn("supervisor_signature_file_token is required", message)
		self.assertIn("nobody behind it", message)
		self.assertEqual(STORE.rows(shifts.HEAT_DOCTYPE), [])

	def test_signs_observed_with_no_response_and_no_notes_is_refused(self):
		"""Signs seen and nothing done is the sequence that kills people. What is
		refused is the silence, not the combination — there are legitimate
		versions and every one of them is a sentence somebody can write."""
		shift = self.start()["name"]
		message = self.heat_error(shift, heat_illness_signs_observed=True, emergency_response_activated=False)
		self.assertIn("SIGNS SEEN AND NOTHING DONE", message)
		self.assertIn("Nothing was created", message)

	def test_signs_observed_with_an_explanation_is_accepted(self):
		shift = self.start()["name"]
		data = self.heat(
			shift,
			heat_illness_signs_observed=True,
			emergency_response_activated=False,
			notes="Recovered in shade inside four minutes and declined further help.",
		)
		self.assertTrue(data["heat_illness_signs_observed"])
		self.assertFalse(data["emergency_response_activated"])

	def test_signs_observed_with_a_response_needs_no_explanation(self):
		shift = self.start()["name"]
		data = self.heat(shift, heat_illness_signs_observed=True, emergency_response_activated=True)
		self.assertTrue(data["emergency_response_activated"])

	def test_an_unmet_obligation_is_recorded_with_the_gap_stated(self):
		"""A day where the shade trailer broke down is a real shift with a real
		gap, and a system that would not let it be recorded would produce either
		a false record or no record."""
		shift = self.start()["name"]
		data = self.heat(shift, shade_provided=False)
		self.assertFalse(data["shade_provided"])
		self.assertTrue(any("shade" in gap for gap in data["obligation_gaps"]))
		self.assertIn("worth more under investigation", data["gap_note"])

	def test_a_verified_training_claim_the_register_contradicts_is_refused(self):
		"""The same audit packet carries both this record and the register, and a
		packet that contradicts itself is worse than one with a gap."""
		shift = self.start()["name"]
		message = self.heat_error(shift, training_verified=True)
		self.assertIn("no current heat illness prevention training", message)
		self.assertIn("CONTRADICTS ITSELF", message.upper())
		self.assertEqual(STORE.rows(shifts.HEAT_DOCTYPE), [])

	def test_a_verified_claim_the_register_supports_is_accepted(self):
		for person in (WORKER, *CREW):
			self.tool_data(
				"record_training",
				{
					"employee": person,
					"training_type": "Heat Illness Prevention",
					"completed_date": days_out(-10),
					"expires_date": days_out(355),
					"regimes": ["OR-OSHA"],
					"content_topics_covered": "Heat, water, shade, symptoms, reporting, emergency response",
				},
			)
		shift = self.start()["name"]
		data = self.heat(shift, training_verified=True)
		self.assertTrue(data["training_verified"])
		self.assertEqual(len(data["crew_training"]["with_current_training"]), 5)
		self.assertEqual(data["crew_training"]["without_current_training"], [])

	def test_the_training_check_runs_as_of_the_day_of_the_shift_not_today(self):
		"""A card that expired last week was current in July, and a check run
		against today would report a July shift as non-compliant because of
		something that happened after it."""
		self.tool_data(
			"record_training",
			{
				"employee": WORKER,
				"training_type": "Heat Illness Prevention",
				"completed_date": days_out(-400),
				"expires_date": days_out(-30),
				"regimes": ["OR-OSHA"],
				"content_topics_covered": "Heat, water, shade, symptoms, reporting, emergency response",
			},
		)
		shift = self.start(crew_employees=[WORKER], start_datetime=at(6, 0, days_out(-60)))["name"]
		data = self.heat(shift, training_verified=True, event_date=days_out(-60))
		self.assertEqual(data["crew_training"]["without_current_training"], [])

	def test_an_acclimatization_plan_naming_somebody_off_the_crew_is_refused(self):
		"""-1131(g) asks for a plan for the unacclimatized workers ON THIS SHIFT,
		and one naming somebody who was not there reads as filled in from a list
		rather than from the block."""
		shift = self.start(crew_employees=[WORKER])["name"]
		message = self.heat_error(shift, acclimatization_plan=[CREW[0]])
		self.assertIn("not on the crew", message)

	def test_an_acclimatization_plan_of_crew_members_is_kept_by_name(self):
		shift = self.start()["name"]
		data = self.heat(shift, acclimatization_plan=[CREW[0], CREW[1]])
		self.assertEqual(data["acclimatization_plan"], [CREW[0], CREW[1]])
		self.assertEqual(data["unacclimatized_workers"], 2)

	def test_a_record_on_a_shift_with_an_empty_timeline_says_the_checkboxes_are_bare(self):
		"""The checkboxes are the assertion and the timeline is the evidence for
		it, and an inspector will ask for the second."""
		shift = self.start()["name"]
		data = self.heat(shift)
		self.assertIn("EMPTY EVENT TIMELINE", data["timeline_note"])

	def test_a_shift_that_does_not_exist_is_refused(self):
		message = self.tool_error(
			"create_heat_exposure_event",
			{"farm_shift": "SHIFT-1999-0001", "supervisor_signature_file_token": SIGNATURE},
		)
		self.assertIn("no Farm Shift called", message)


# ── 7 ───────────────────────────────────────────────────────────────────────
class TheSupervisorReviewRule(ShiftTestCase):
	"""The thirteenth rule: a signature that was never put on a record.

	FSMA §112.161(b) is the most commonly cited finding against farms whose
	actual practice is fine. USDA GAP does not ask for a supervisor's review, so
	an operation with an immaculate GAP binder has every training delivered,
	every trainee signature, and no second pair of eyes on any of it.
	"""

	def a_training(self, age_days: int) -> str:
		"""File a training record and age it, as if it had been written then.

		`creation` is set directly because the RULE'S CLOCK RUNS FROM WHEN THE
		RECORD WAS MADE — §112.161(b)'s own phrase is "after the records are
		made" — and a test that aged `completed_date` instead would be testing a
		different rule from the one that ships.
		"""
		data = self.tool_data(
			"record_training",
			{
				"employee": WORKER,
				"training_type": "Heat Illness Prevention",
				"completed_date": days_out(-age_days),
				"regimes": ["FSMA"],
				"content_topics_covered": "Hygiene, illness reporting, handwashing",
			},
		)
		STORE.tables["Employee Training Record"][data["name"]]["creation"] = f"{days_out(-age_days)} 09:00:00"
		return data["name"]

	def test_a_record_a_fortnight_old_with_no_review_raises_a_warning(self):
		name = self.a_training(15)
		alerts = self.sweep()
		self.assertEqual(len(alerts), 1)
		self.assertEqual(alerts[0]["severity"], alerts_base.SEVERITY_WARNING)
		self.assertEqual(alerts[0]["source_docname"], name)
		self.assertIn("15 days with no supervisor review", alerts[0]["alert_message"])
		self.assertIn("112.161(b)", alerts[0]["alert_message"])

	def test_a_record_past_thirty_days_raises_critical(self):
		self.a_training(31)
		alerts = self.sweep()
		self.assertEqual(len(alerts), 1)
		self.assertEqual(alerts[0]["severity"], alerts_base.SEVERITY_CRITICAL)
		self.assertIn("no longer a reading of", alerts[0]["alert_message"])

	def test_a_record_written_this_morning_raises_nothing(self):
		"""KAIROTIC. The gate is the age of an unsigned record, not a date — so
		it raises nothing however many times the sweep runs."""
		self.a_training(0)
		self.assertEqual(self.sweep(), [])
		self.assertEqual(self.sweep(), [])

	def test_a_record_thirteen_days_old_is_still_inside_the_reasonable_window(self):
		self.a_training(13)
		self.assertEqual(self.sweep(), [])

	def test_signing_the_review_auto_dismisses_the_alert(self):
		"""Nobody should have to remember to switch off a reminder about
		something that already happened."""
		name = self.a_training(20)
		self.assertEqual(len(self.sweep()), 1)
		self.tool_data("sign_training_supervisor_review", {"name": name, "supervisor": FOREMAN})
		self.assertEqual(self.sweep(), [])
		dismissed = [
			row
			for row in STORE.rows("Compliance Alert")
			if row.get("alert_type") == "supervisor_review_lapsed"
		]
		self.assertEqual(len(dismissed), 1)
		self.assertTrue(int(dismissed[0]["auto_dismissed"]))

	def test_the_alert_is_tagged_from_the_record_rather_than_from_the_rule(self):
		self.a_training(20)
		alerts = self.sweep()
		tags = alerts_base.regimes_for_alerts([alerts[0]["name"]])
		self.assertEqual(tags.get(alerts[0]["name"]), ["FSMA"])

	def test_the_due_date_is_when_it_went_past_its_review_window(self):
		"""A calendar sorted on due date should put the record that went overdue
		first, and that is fourteen days after it was made — not the day it was
		written."""
		self.a_training(20)
		alerts = self.sweep()
		self.assertEqual(str(alerts[0]["due_date"]), days_out(-6))

	def test_the_rule_is_tagged_fsma_and_only_fsma(self):
		"""§112.161(b) is FSMA and only FSMA. The other regimes ask for the record
		and none of them asks for this signature, which is exactly why the gap
		exists — tagging it wider would dilute the one packet that raises it."""
		self.assertEqual(alerts_base.RULES["supervisor_review_lapsed"].regimes, ("FSMA",))

	def test_the_rule_walks_a_table_so_the_next_doctype_is_one_row(self):
		"""§112.161(b) is not about training. It reaches every activity record
		under the part, and four more of this app's doctypes will grow the
		columns."""
		self.assertEqual(len(alert_rules.REVIEW_TARGETS), 1)
		self.assertEqual(alert_rules.REVIEW_TARGETS[0].doctype, "Employee Training Record")
		self.assertEqual(alert_rules.REVIEW_TARGETS[0].reviewed_by_field, "supervisor_reviewed_by")


# ── 8 ───────────────────────────────────────────────────────────────────────
class TheGuards(ShiftTestCase):
	TOOLS = (
		("start_shift", {"foreman": FOREMAN}),
		("add_worker_to_shift", {"shift": "SHIFT-2026-0001", "employee": WORKER}),
		("remove_worker_from_shift", {"shift": "SHIFT-2026-0001", "employee": WORKER}),
		("log_shift_event", {"shift": "SHIFT-2026-0001", "event_type": "Water Break"}),
		("end_shift", {"shift": "SHIFT-2026-0001", "supervisor_signature_file_token": SIGNATURE}),
		(
			"create_heat_exposure_event",
			{"farm_shift": "SHIFT-2026-0001", "supervisor_signature_file_token": SIGNATURE},
		),
		("list_shifts", {}),
		("get_shift", {"name": "SHIFT-2026-0001"}),
		("list_heat_exposure_events", {}),
		("get_heat_exposure_event", {"name": "HEAT-2026-0001"}),
	)

	#: The seven of them that are the SHIFT, gated on `employee.SHIFT_ROLES`. The
	#: other three in `TOOLS` are the heat record and its register, which stay on
	#: `employee.HR_ROLES` — see below.
	SHIFT_TOOLS = (
		"start_shift",
		"add_worker_to_shift",
		"remove_worker_from_shift",
		"log_shift_event",
		"end_shift",
		"list_shifts",
		"get_shift",
	)

	def test_an_account_with_no_role_at_all_is_refused_by_every_one_of_them(self):
		set_roles(frappe.session.user, ["Accounts Manager"])
		for name, arguments in self.TOOLS:
			with self.subTest(tool=name):
				message = self.tool_error(name, arguments)
				expected = (
					"may not form or close a crew shift"
					if name in self.SHIFT_TOOLS
					else "may not change the personnel register"
				)
				self.assertIn(expected, message)

	def test_a_foreman_may_run_a_shift_end_to_end(self):
		"""THE GATE THE HANDSET'S OWN BUTTON WAS REFUSED BY.

		`ShiftToolsToolbar` offers Crew Clock to Foreman and Crew Leader; the
		tools gated on `HR_ROLES`, which has neither — so the supervisor OAR
		437-004-1131 names was told they may not change the personnel register
		for the one record that is unambiguously theirs. The close is the half
		that mattered most: it is what writes the crew's Attendance rows, so a
		foreman who could not close was a crew with no wage record for the day.
		"""
		set_roles(frappe.session.user, ["Foreman"])
		shift = self.start()["name"]
		self.tool_data("log_shift_event", {"shift": shift, "event_type": "Water Break"})
		self.close(shift)

		attendance = self.attendance()
		self.assertEqual(len(attendance), len([WORKER, *CREW]))
		self.assertTrue(all(row.get("farm_shift") == shift for row in attendance))

	def test_a_crew_leader_may_too_even_though_this_app_does_not_ship_the_role(self):
		"""It is a role operators create by hand where the crew lead is not the
		foreman. Naming it costs a site that has not got one nothing, and buys a
		site that has one a server that agrees with its own iOS client."""
		set_roles(frappe.session.user, ["Crew Leader"])
		shift = self.start()["name"]
		self.assertTrue(self.close(shift)["name"])

	def test_running_a_shift_IS_permission_to_hire_since_v0_94_0(self):
		"""THE POLICY THIS TEST USED TO ASSERT WAS REVERSED ON PURPOSE, and the
		reversal is the subject of the release rather than a side effect.

		The old claim was that widening the shift gate to the crew's own
		supervisor is not widening the personnel register's. That is still true of
		the REGISTER — see the test below — but it was being used to keep the
		foreman out of the HIRE, and there is no personnel office on a farm this
		size to send him to. `HIRING_ROLES` is `SHIFT_ROLES`: the same supervisor
		who forms the crew brings people onto it.
		"""
		self.configure(enabled=1, **ON, allow_create_employee=1)
		set_roles(frappe.session.user, ["Foreman"])
		self.start()
		created = self.tool_data(
			"create_employee", {"employee_name": "New Hire", "company": MAIN, "date_of_joining": at(6)[:10]}
		)
		self.assertTrue(created["employee"])

	def test_but_it_is_still_not_permission_to_read_the_register(self):
		"""THE HALF THAT DID NOT MOVE, and the reason `HR_ROLES` and
		`HIRING_ROLES` are still two lists rather than one. Bringing somebody onto
		the farm is field work with a compliance record behind it; reading the
		entity's personnel register — names, hire dates, who has left — is
		somebody else's PII, and a foreman has no more claim on it than before."""
		self.configure(enabled=1, **ON, allow_create_employee=1, allow_update_employee=1)
		set_roles(frappe.session.user, ["Foreman"])
		created = self.tool_data(
			"create_employee", {"employee_name": "New Hire", "company": MAIN, "date_of_joining": at(6)[:10]}
		)["employee"]
		message = self.tool_error("update_employee", {"employee": created, "department": "Harvest"})
		self.assertIn("may not change the personnel register", message)

	def test_and_a_picker_may_do_neither(self):
		"""The negative control for both. `SHIFT_ROLES` gained a hire; it did not
		gain a member."""
		self.configure(enabled=1, **ON, allow_create_employee=1)
		set_roles(frappe.session.user, ["Field Worker"])
		self.assertIn(
			"may not bring a person onto the farm",
			self.tool_error(
				"create_employee",
				{"employee_name": "New Hire", "company": MAIN, "date_of_joining": at(6)[:10]},
			),
		)

	def test_every_switch_turns_its_tool_off_individually(self):
		for name, arguments in self.TOOLS:
			with self.subTest(tool=name):
				self.configure(enabled=1, **{**ON, f"allow_{name}": 0})
				message = self.tool_error(name, arguments)
				self.assertIn(f"allow_{name}", message)
				self.assertIn("switched off", message)

	def test_a_scoped_account_cannot_start_a_shift_for_an_entity_it_cannot_see(self):
		STORE.seed(
			"User Permission",
			[
				{
					"name": "UP-SHIFT-1",
					"user": frappe.session.user,
					"allow": "Company",
					"for_value": OTHER,
				}
			],
		)
		message = self.start_error()
		self.assertIn("no access to company", message)

	def test_a_scoped_account_reading_the_register_gets_its_own_entity(self):
		self.start()
		STORE.seed(
			"User Permission",
			[
				{
					"name": "UP-SHIFT-2",
					"user": frappe.session.user,
					"allow": "Company",
					"for_value": OTHER,
				}
			],
		)
		self.assertEqual(self.tool_data("list_shifts", {})["count"], 0)

	def test_a_scoped_account_cannot_read_one_shift_of_another_entity(self):
		shift = self.start()["name"]
		STORE.seed(
			"User Permission",
			[
				{
					"name": "UP-SHIFT-3",
					"user": frappe.session.user,
					"allow": "Company",
					"for_value": OTHER,
				}
			],
		)
		self.assertIn("no access to company", self.tool_error("get_shift", {"name": shift}))

	def test_every_mutating_tool_ships_off_and_every_read_ships_on(self):
		from .harness import _load_app_doctype

		by_name = {field["fieldname"]: field for field in _load_app_doctype("erpnext_mcp_settings")["fields"]}
		for name, _arguments in self.TOOLS:
			with self.subTest(tool=name):
				self.assertEqual(
					by_name[f"allow_{name}"]["default"],
					"0" if registry.TOOLS[name]["mutating"] else "1",
				)


# ── 9 ───────────────────────────────────────────────────────────────────────
class ReadingItBack(ShiftTestCase):
	def test_the_register_reports_what_is_still_open(self):
		open_shift = self.start()["name"]
		# THE SECOND SHIFT CARRIES NO CREW, because nobody is on two open shifts
		# at once — see `NobodyIsOnTwoOpenShiftsAtOnce`. This test is about the
		# register, and a crew it does not read would only be a way of tripping
		# a guard it is not testing.
		closed = self.start(start_datetime=at(5), crew_employees=[])["name"]
		self.close(closed, end_datetime=at(5, 30))
		data = self.tool_data("list_shifts", {"company": MAIN})
		self.assertEqual(data["count"], 2)
		self.assertEqual(data["open"], [open_shift])

	def test_the_status_filter_is_computed_rather_than_read_off_the_column(self):
		self.start()
		closed = self.start(start_datetime=at(5), crew_employees=[])["name"]
		self.close(closed, end_datetime=at(5, 30))
		self.assertEqual(self.tool_data("list_shifts", {"status": "Closed"})["count"], 1)
		self.assertEqual(self.tool_data("list_shifts", {"status": "Active"})["count"], 1)

	def test_an_unknown_status_is_refused_with_the_three_that_exist(self):
		message = self.tool_error("list_shifts", {"status": "Finished"})
		self.assertIn("Active, Closed, Cancelled", message)

	def test_the_employee_filter_walks_the_crew_tables(self):
		with_them = self.start(crew_employees=[WORKER])["name"]
		self.start(crew_employees=[CREW[0]], start_datetime=at(5))
		data = self.tool_data("list_shifts", {"employee": WORKER})
		self.assertEqual([entry["name"] for entry in data["shifts"]], [with_them])

	def test_a_shift_closed_without_a_signature_is_named(self):
		"""end_shift cannot produce one, so anything on that list was closed in
		the Desk or by an import."""
		shift = self.start()["name"]
		STORE.tables[shifts.DOCTYPE][shift]["end_datetime"] = at(15)
		data = self.tool_data("list_shifts", {})
		self.assertEqual(data["closed_without_a_signature"], [shift])
		self.assertIn("A signature added now is dated now", data["signature_note"])

	def test_get_shift_is_the_evidence_chain(self):
		shift = self.start()["name"]
		self.tool_data(
			"log_shift_event",
			{"shift": shift, "event_type": "Water Break", "event_datetime": at(9)},
		)
		self.tool_data("remove_worker_from_shift", {"shift": shift, "employee": WORKER, "left_at": at(11)})
		self.close(shift)
		self.heat(shift)

		data = self.tool_data("get_shift", {"name": shift})
		self.assertEqual(data["crew_size"], 5)
		self.assertEqual(data["compliance_event_count"], 1)
		self.assertEqual(data["heat_exposure_event"]["farm_shift"], shift)
		left = next(row for row in data["crew"] if row["employee"] == WORKER)
		stayed = next(row for row in data["crew"] if row["employee"] == CREW[0])
		self.assertEqual(left["present_until"], at(11))
		self.assertTrue(left["left_early"])
		# The honest reading of an empty left_at: they were there to the end. It
		# is COMPUTED, so it cannot go stale when the end time moves.
		self.assertEqual(stayed["present_until"], at(15))
		self.assertFalse(stayed["left_early"])

	def test_get_shift_says_the_weather_timeline_is_empty_and_why(self):
		data = self.tool_data("get_shift", {"name": self.start()["name"]})
		self.assertEqual(data["weather_timeline"], [])
		self.assertIn("v0.19.4", data["weather_note"])

	def test_the_heat_register_names_the_records_without_verified_training(self):
		shift = self.start()["name"]
		self.heat(shift)
		data = self.tool_data("list_heat_exposure_events", {"company": MAIN})
		self.assertEqual(data["count"], 1)
		self.assertEqual(data["without_verified_training"], [data["records"][0]["name"]])
		self.assertIn("first hot morning", data["training_note"])

	def test_the_heat_register_names_the_shifts_where_signs_were_seen(self):
		shift = self.start()["name"]
		self.heat(shift, heat_illness_signs_observed=True, emergency_response_activated=True)
		data = self.tool_data("list_heat_exposure_events", {})
		self.assertEqual(len(data["with_signs_observed"]), 1)
		self.assertIn("observing is the obligation working rather than failing", data["note"])

	def test_with_gaps_only_is_the_worklist(self):
		full = self.start()["name"]
		# Its own crew, for the reason above: the heat record is about the shift
		# and two open shifts may not hold the same person.
		short = self.start(start_datetime=at(5), crew_employees=[])["name"]
		for person in (WORKER, *CREW):
			self.tool_data(
				"record_training",
				{
					"employee": person,
					"training_type": "Heat Illness Prevention",
					"completed_date": days_out(-10),
					"expires_date": days_out(355),
					"regimes": ["OR-OSHA"],
					"content_topics_covered": "Heat, water, shade, symptoms, reporting, emergency response",
				},
			)
		self.heat(full, training_verified=True)
		self.heat(short, shade_provided=False, training_verified=True)
		data = self.tool_data("list_heat_exposure_events", {"with_gaps_only": True})
		self.assertEqual(data["count"], 1)
		self.assertEqual(data["records"][0]["farm_shift"], short)

	def test_get_heat_exposure_event_carries_the_shift_behind_it(self):
		shift = self.start()["name"]
		created = self.heat(shift)
		data = self.tool_data("get_heat_exposure_event", {"name": created["name"]})
		self.assertEqual(data["shift"]["name"], shift)
		self.assertEqual(data["shift"]["crew_size"], 5)
		self.assertIn("evidence_chain", data)
		self.assertIn("prove that shift complied", data["evidence_chain"])

	def test_a_docname_that_does_not_exist_is_refused_with_where_to_look(self):
		self.assertIn(
			"list_shifts has the register", self.tool_error("get_shift", {"name": "SHIFT-1999-0001"})
		)
		self.assertIn(
			"list_heat_exposure_events has the register",
			self.tool_error("get_heat_exposure_event", {"name": "HEAT-1999-0001"}),
		)


# ── 10 ──────────────────────────────────────────────────────────────────────
class TwoPhonesOneCrew(ShiftTestCase):
	"""The roster race, and the property that closes it.

	`TheCrewIsAnEnvelope` above already covers the SEQUENTIAL case — a join onto
	a shift that is already closed is refused by name. What it cannot cover is
	the simultaneous one: two handsets that read the same open shift in the same
	instant both pass every guard and both save, and because Frappe rewrites a
	child table by deleting its rows and re-inserting them, the second commit
	takes the first one's crew row with it. The worker it drops is picking in the
	block with no Attendance row and no payroll day, off a scan that answered 200.

	THIS DOUBLE IS SINGLE-THREADED, SO THERE IS NO CONCURRENCY TO TEST. What
	there IS to test is the property that makes the lock work on a real bench:
	every tool that saves the shift must read its state AFTER taking the row
	lock, not before. So these tests stand in for the other transaction by
	mutating the row from inside `shifts.lock_shift` — which is exactly the
	moment a real second writer's commit becomes visible — and assert the guard
	that was raced now fires.

	All three fail against the code before the fix, which had read `end_datetime`
	into `row` by the time anything was locked.
	"""

	def racing(self, mutate):
		"""Run `mutate` ONCE, at the moment the shift's row lock is taken.

		ONCE IS LOAD-BEARING. `mutate` stands in for the other transaction and
		the realistic ones call a tool that takes the lock again; without the
		one-shot guard that re-enters this hook forever.
		"""
		original = shifts.lock_shift
		fired = []

		def lock_then_race(name: str) -> None:
			original(name)
			if not fired:
				fired.append(name)
				mutate(name)

		shifts.lock_shift = lock_then_race
		self.addCleanup(setattr, shifts, "lock_shift", original)
		return fired

	def closed_during_the_wait(self, name: str) -> None:
		"""What the other transaction did: signed the shift closed."""
		doc = frappe.get_doc(shifts.DOCTYPE, name)
		doc.end_datetime = at(15)
		doc.status = shifts.STATUS_CLOSED
		doc.save(ignore_permissions=True)

	def test_a_join_that_lands_during_the_lock_wait_sees_the_close(self):
		"""The worker this drops is the whole cost of the bug: a crew row on a
		shift whose Attendance has already been written is a person with no
		payroll day, and nothing on the record afterwards shows there were two
		callers."""
		shift = self.start(crew_employees=[WORKER])["name"]
		self.racing(self.closed_during_the_wait)
		message = self.tool_error("add_worker_to_shift", {"shift": shift, "employee": CREW[0]})
		self.assertIn("Nobody joins a shift that is over", message)
		self.assertIn("Nothing was changed", message)
		self.assertEqual(len(self.crew_rows(shift)), 1)

	def test_a_close_that_lands_during_the_lock_wait_is_not_written_twice(self):
		"""Two closes write two sets of Attendance rows for one day, which is the
		refusal `end_shift` already carries for the sequential case."""
		shift = self.start(crew_employees=[WORKER])["name"]
		self.racing(self.closed_during_the_wait)
		message = self.tool_error(
			"end_shift",
			{"shift": shift, "end_datetime": at(16), "supervisor_signature_file_token": SIGNATURE},
		)
		self.assertIn("already closed", message)
		self.assertIn("second set of Attendance rows", message)

	def test_a_cancel_that_lands_during_the_lock_wait_sees_the_close(self):
		shift = self.start(crew_employees=[WORKER])["name"]
		self.racing(self.closed_during_the_wait)
		message = self.tool_error(
			"cancel_shift",
			{"shift": shift, "cancellation_reason": "the block was still wet at seven"},
		)
		self.assertIn("Nothing was changed", message)

	def test_the_lock_is_taken_on_the_shift_that_was_named(self):
		shift = self.start(crew_employees=[WORKER])["name"]
		locked = self.racing(lambda name: None)
		self.tool_data("add_worker_to_shift", {"shift": shift, "employee": CREW[0]})
		self.assertEqual(locked, [shift])

	def test_every_tool_that_saves_the_shift_takes_the_lock(self):
		"""A LOCK ONLY SERIALISES THE WRITERS THAT TAKE IT. One tool left outside
		it is enough to drop a worker off a roster, and the next tool to load the
		shift document and save it will be written by somebody who has not read
		`shifts.lock_shift`. So this asserts the rule rather than the seven names:
		anything that does `frappe.get_doc(DOCTYPE, ...)` and saves must resolve
		through `_resolve_shift_for_update`.
		"""
		import ast
		import inspect

		from erpnext_mcp.tools import shifts as shift_tools

		source = inspect.getsource(shift_tools)
		lines = source.split("\n")
		unlocked = []
		for node in ast.parse(source).body:
			if not isinstance(node, ast.FunctionDef):
				continue
			body = "\n".join(lines[node.lineno - 1 : node.end_lineno])
			if "frappe.get_doc(DOCTYPE" not in body or ".save(" not in body:
				continue
			if "_resolve_shift_for_update(" not in body:
				unlocked.append(node.name)
		self.assertEqual(unlocked, [], f"these save the shift without locking it: {unlocked}")

# SPDX-License-Identifier: MIT
"""Punch times from phones with no signal. v0.227.0 — docs/design/offline_mill_creek.md §4.

The phone's tap is the official time; the server's receipt is kept beside it; a
gap over the offline window goes to a manager; only a person changes an official
time, and neither witness is ever overwritten.
"""

import datetime

import frappe

from erpnext_mcp import punch_times, training_courses
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.errors import ToolError

from .harness import STORE, set_roles
from .test_api_mobile import WORKER
from .test_training_courses import CourseCase, days_out
from .test_training_sessions import TRAINEE
from .test_work_actions import CAL, DEE, WorkActionsCase


def now() -> datetime.datetime:
	"""The harness's clock — `frappe.utils.now()` is what the server stamps receipts with."""
	return datetime.datetime.fromisoformat(str(frappe.utils.now())[:19])


def stamp(delta_hours: float) -> str:
	return (now() - datetime.timedelta(hours=delta_hours)).strftime("%Y-%m-%d %H:%M:%S")


class Judging(WorkActionsCase):
	def test_inside_the_window_is_fine_and_outside_or_ahead_is_flagged(self):
		received = stamp(0)
		self.assertEqual(punch_times.judge(stamp(2), received), "")
		self.assertIn("offline window is 12 hours", punch_times.judge(stamp(20), received))
		self.assertIn("phone clock may be wrong", punch_times.judge(stamp(-1), received))
		self.configure(enabled=1, offline_punch_window_hours=24)
		self.assertEqual(punch_times.judge(stamp(20), received), "")

	def test_an_offset_is_taken_into_the_site_zone(self):
		self.assertEqual(punch_times.device_time("2026-10-03T13:00:00Z")[:10], "2026-10-03")
		self.assertEqual(punch_times.device_time(""), "")


class CrewPunches(WorkActionsCase):
	def crew_row(self, shift, person):
		rows = frappe.get_doc("Farm Shift", shift).as_dict()["crew"]
		return next(r for r in rows if r["employee"] == person)

	def test_the_tap_is_official_the_receipt_is_kept_and_a_late_one_is_flagged(self):
		self.foreman()
		tapped = stamp(20)
		answer = mobile_api.clock_in_crew(
			employees=[CAL], location="Block 7 North", client_request_id="req-p1", tapped_at=tapped
		)
		row = self.crew_row(answer["shift"], CAL)
		self.assertEqual(str(row["joined_at"])[:19], tapped)
		self.assertEqual(str(row["joined_device_at"])[:19], tapped)
		self.assertTrue(row["joined_received_at"])
		self.assertEqual(row["punch_review"], 1)
		self.assertIn("offline window", row["punch_review_reason"])

	def test_a_prompt_tap_is_not_flagged_and_an_old_app_is_stamped_as_before(self):
		self.foreman()
		answer = mobile_api.clock_in_crew(employees=[CAL], location="Yard", tapped_at=stamp(0.5))
		self.assertFalse(self.crew_row(answer["shift"], CAL).get("punch_review"))
		mobile_api.clock_in_crew(employees=[DEE], shift=answer["shift"])
		old = self.crew_row(answer["shift"], DEE)
		self.assertTrue(old["joined_received_at"])
		self.assertFalse(old.get("joined_device_at"))
		self.assertFalse(old.get("punch_review"))

	def test_a_late_clock_out_is_flagged_too(self):
		"""Clocked in 15 hours ago and out 30 minutes later, both received now."""
		self.foreman()
		shift = mobile_api.clock_in_crew(employees=[CAL], location="Yard", tapped_at=stamp(15))["shift"]
		out = stamp(14.5)
		mobile_api.clock_out_worker(shift=shift, employee=CAL, left_at=out)
		row = self.crew_row(shift, CAL)
		self.assertEqual(str(row["left_device_at"])[:19], out)
		self.assertEqual(str(row["left_at"])[:19], out)
		self.assertTrue(row["left_received_at"])
		self.assertEqual(row["punch_review_reason"].count("offline window"), 2, "the in and the out")

	def test_a_manager_decides_and_neither_witness_is_overwritten(self):
		self.foreman()
		tapped = stamp(20)
		shift = mobile_api.clock_in_crew(employees=[CAL], location="Yard", tapped_at=tapped)["shift"]
		row = self.crew_row(shift, CAL)
		STORE.commit()

		with self.assertRaises(Exception):
			mobile_api.list_punch_reviews()  # a foreman is not a reviewer
		set_roles(WORKER, ["Field Worker", "Farm Manager"])
		self.be()
		waiting = mobile_api.list_punch_reviews()
		self.assertEqual([p["row"] for p in waiting["punches"]], [row["name"]])
		self.assertEqual(waiting["punches"][0]["device"]["joined_device_at"][:19], tapped)

		done = mobile_api.resolve_punch_review(
			row=row["name"], resolution="Server time used", note="phone was off"
		)
		after = self.crew_row(shift, CAL)
		self.assertEqual(str(after["joined_at"])[:19], str(row["joined_received_at"])[:19])
		self.assertEqual(str(after["joined_device_at"])[:19], tapped, "the phone's time is kept")
		self.assertEqual(after["punch_resolution"], "Server time used")
		self.assertEqual(after["punch_reviewed_by"], WORKER)
		self.assertIn("joined_at", done["changed"])
		self.assertEqual(mobile_api.list_punch_reviews()["count"], 0)

	def test_phone_time_stands_changes_nothing_and_corrected_needs_a_time(self):
		self.foreman()
		tapped = stamp(20)
		shift = mobile_api.clock_in_crew(employees=[CAL], location="Yard", tapped_at=tapped)["shift"]
		name = self.crew_row(shift, CAL)["name"]
		set_roles(WORKER, ["Field Worker", "Farm Manager"])
		with self.assertRaises(ToolError):
			punch_times.resolve(WORKER, name, "Corrected", kind="in")
		punch_times.resolve(WORKER, name, "Phone time stands")
		row = self.crew_row(shift, CAL)
		self.assertEqual(str(row["joined_at"])[:19], tapped)
		self.assertEqual(row["punch_resolution"], "Phone time stands")
		with self.assertRaises(ToolError):
			punch_times.resolve(WORKER, "nope", "Phone time stands")

	def test_corrected_sets_only_the_official_time(self):
		self.foreman()
		tapped = stamp(20)
		shift = mobile_api.clock_in_crew(employees=[CAL], location="Yard", tapped_at=tapped)["shift"]
		row = self.crew_row(shift, CAL)
		set_roles(WORKER, ["Field Worker", "HR Manager"])
		fixed = stamp(19)
		punch_times.resolve(WORKER, row["name"], "Corrected", kind="in", corrected_at=fixed, note="crew log")
		after = self.crew_row(shift, CAL)
		self.assertEqual(str(after["joined_at"])[:19], fixed)
		self.assertEqual(str(after["joined_device_at"])[:19], tapped)
		self.assertEqual(str(after["joined_received_at"]), str(row["joined_received_at"]))

	def test_the_tile_counts_them(self):
		from erpnext_mcp import tile_queries

		self.foreman()
		mobile_api.clock_in_crew(employees=[CAL], location="Yard", tapped_at=stamp(20))
		self.assertEqual(tile_queries.punch_reviews(WORKER, "", {})["count"], 1)


class ClassCheckIns(CourseCase):
	def test_the_tapped_time_is_official_and_judged_against_the_window(self):
		from .test_training_courses import ON as COURSE_ON
		from .test_training_sessions import ON as BASE

		self.configure(enabled=1, offline_punch_window_hours=0.01, **BASE, **COURSE_ON)
		session = self.open_session(session_date=days_out(0))["name"]
		self.tool_data("add_session_attendee", {"session": session, "employee": TRAINEE, "attended": False})
		clock = now()
		tap = max(clock - datetime.timedelta(minutes=5), clock.replace(hour=0, minute=0, second=1))
		tapped = tap.strftime("%Y-%m-%d %H:%M:%S")
		done = training_courses.check_in(session, "u", TRAINEE, tapped_at=tapped)
		self.assertEqual(done["checked_in_at"][:19], tapped)
		row = next(
			r
			for r in frappe.get_doc("Training Session", session).as_dict()["attendees"]
			if r["employee"] == TRAINEE
		)
		self.assertEqual(str(row["device_scanned_at"])[:19], tapped)
		self.assertTrue(row["received_at"])
		if (clock - tap).total_seconds() > 40:
			self.assertEqual(row["punch_review"], 1)
			self.assertTrue(done["punch_review"])

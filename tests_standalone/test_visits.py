# SPDX-License-Identifier: MIT
"""The trip, rather than the tasks it contained — v0.20.1's one new tool.

A WORKER DOES NOT GO TO A TASK, THEY GO SOMEWHERE. They drive to the north block,
walk three cabins, close three task assignments and drive back. The dispatch
board records three completions with three timestamps, and every question about
the morning — how long was the trip, was the drive worth it, how many places did
we send somebody to twice today — has to be reconstructed by guessing which
completions belonged together.

`list_visits` does not guess. The handset mints a `visit_id` when the worker
arrives and reuses it for every task closed before they leave, because the phone
is the only thing that was there.

────────────────────────────────────────────────────────────────────────────
FIVE CLAIMS
────────────────────────────────────────────────────────────────────────────

1. **THE GROUPING IS THE HANDSET'S.** `TheRollup`. Three completions carrying
   one `visit_id` are one visit with three tasks, whatever their timestamps say;
   two visit_ids are two visits, however close together they were filed.

2. **A COMPLETION WITH NO `visit_id` IS IN NO VISIT.** `WhatIsNotAVisit`. Not a
   synthetic one-task visit and not an "unassigned" bucket dressed as a trip —
   everything filed before v0.20.1 has the column blank, and inventing visits
   for those would put fabricated trips beside real ones with nothing to tell
   them apart. It is COUNTED, which is the honest version of the same fact.

3. **THE DERIVED FIGURES SAY WHAT THEY MEASURE.** `TheDerivedFigures`.
   `duration_minutes` is first completion to last and excludes the drive;
   `total_evidence_files` counts distinct FILES, because one signature filed
   against three cabins is one photograph.

4. **IT IS SCOPED AND SWITCHED LIKE EVERY OTHER READ.** `TheGuards`. The HR role
   gate, the company scope, the kill switch, and the individual switch.

5. **IT WRITES NOTHING.** `TheGuards.test_it_writes_nothing` — a read tool that
   grew a side effect is the failure a read-only claim exists to prevent.

6. **NOTHING BUT AN IDENTIFIER GETS INTO THE COLUMN.** `TheIdentifierIsChecked`.
   The rollup above is only as good as the grouping key, and every claim on this
   list is a claim about exact-value grouping: a `visit_id` that is not the UUID
   the handset mints does not read as a bad row here, it reads as ANOTHER VISIT.
   That is the failure this suite could not otherwise see — claim 1 still passes
   while the report is wrong, because the wrong answer has the same shape as the
   right one. v0.20.1 accepted whatever arrived, deliberately, while the app's
   format was unconfirmed; it is confirmed now, and the check is at the doors.
"""

import frappe

from .fixtures import MAIN, OTHER, install_hrms
from .harness import ROLES, STORE, set_roles
from .test_dispatch import A_PHOTO
from .test_fieldwork import WORKER_EMPLOYEE, FieldworkTestCase

#: A second photograph, so a two-task visit can file two distinct files.
ANOTHER_PHOTO = [{"file_url": "/files/south-wall.jpg", "evidence_type": "Photo", "caption": "south"}]

MORNING = "5C1F0A64-2B3D-4E5F-8A9B-0C1D2E3F4A5B"
AFTERNOON = "9E8D7C6B-5A49-4382-91F0-A1B2C3D4E5F6"


class VisitTestCase(FieldworkTestCase):
	def setUp(self):
		super().setUp()
		install_hrms()
		self.configure(
			enabled=1,
			public_url="https://umbrel.tail4a2b.ts.net",
			allow_list_visits=1,
			allow_start_shift=1,
			allow_create_mobile_user=1,
			allow_generate_api_token=1,
			allow_create_parcel=1,
			allow_create_housing_unit=1,
			allow_create_farm_task=1,
			allow_claim_task_via_mobile=1,
			allow_start_task_via_mobile=1,
			allow_complete_task_via_mobile=1,
		)
		self._roles_before = {user: list(held) for user, held in ROLES.items()}
		self.addCleanup(self._restore_roles)

	def _restore_roles(self):
		ROLES.clear()
		ROLES.update(self._roles_before)

	# -- helpers -------------------------------------------------------------
	def a_completed_task(self, visit_id=None, title="Habitability walk", **overrides):
		"""One task, claimed, started and completed by Ana. Optionally in a visit."""
		task = self.a_task(task_name=title, evidence_required={"findings_text": True}, **overrides)
		self.worker_data("claim_task_via_mobile", {"task_name": task["name"]})
		self.worker_data("start_task_via_mobile", {"task_name": task["name"]})
		body = {"task_name": task["name"], "findings_text": "", "evidence": list(A_PHOTO)}
		if visit_id:
			body["visit_id"] = visit_id
		return self.worker_data("complete_task_via_mobile", body)

	def visits(self, **arguments) -> list:
		return self.tool_data("list_visits", arguments)["visits"]

	def sole(self, **arguments) -> dict:
		found = self.visits(**arguments)
		self.assertEqual(len(found), 1, found)
		return found[0]


class TheRollup(VisitTestCase):
	def test_three_completions_in_one_visit_are_one_visit_with_three_tasks(self):
		"""The spec's shape: a shift opens, a worker does a round, one trip."""
		self.tool_data(
			"start_shift",
			{
				"foreman": "HR-EMP-00001",
				"location": "Block 7 North",
				"shift_type": "Housing Work",
				"crew_employees": ["HR-EMP-00002"],
			},
		)
		for index in range(3):
			self.a_completed_task(visit_id=MORNING, title=f"Cabin {index + 1}")

		visit = self.sole()
		self.assertEqual(visit["visit_id"], MORNING)
		self.assertEqual(visit["total_tasks"], 3)
		self.assertEqual(len(visit["task_assignment_names"]), 3)
		self.assertEqual(visit["completing_user"], WORKER_EMPLOYEE)
		self.assertEqual(visit["company"], MAIN)

	def test_two_visit_ids_are_two_visits(self):
		self.a_completed_task(visit_id=MORNING, title="Cabin 1")
		self.a_completed_task(visit_id=MORNING, title="Cabin 2")
		self.a_completed_task(visit_id=AFTERNOON, title="Pump house")

		found = self.visits()
		self.assertEqual(len(found), 2)
		by_id = {entry["visit_id"]: entry for entry in found}
		self.assertEqual(by_id[MORNING]["total_tasks"], 2)
		self.assertEqual(by_id[AFTERNOON]["total_tasks"], 1)

	def test_a_one_task_visit_is_returned_and_counted(self):
		"""Somebody drove out, did one job and drove back. That is a trip, and it
		is exactly what a question about wasted travel is looking for."""
		self.a_completed_task(visit_id=AFTERNOON)
		data = self.tool_data("list_visits")
		self.assertEqual(data["count"], 1)
		self.assertEqual(data["single_task_visits"], 1)
		self.assertIn("wasted travel", data["single_task_note"])

	def test_the_assignment_names_are_the_ones_that_were_completed(self):
		first = self.a_completed_task(visit_id=MORNING, title="Cabin 1")
		second = self.a_completed_task(visit_id=MORNING, title="Cabin 2")
		self.assertEqual(
			sorted(self.sole()["task_assignment_names"]),
			sorted([first["assignment"]["name"], second["assignment"]["name"]]),
		)

	def test_visits_come_back_earliest_first(self):
		self.a_completed_task(visit_id=MORNING)
		self.a_completed_task(visit_id=AFTERNOON)
		stamps = [entry["first_completion_datetime"] for entry in self.visits()]
		self.assertEqual(stamps, sorted(stamps))


class WhatIsNotAVisit(VisitTestCase):
	def test_a_completion_with_no_visit_id_is_in_no_visit(self):
		self.a_completed_task()
		data = self.tool_data("list_visits")
		self.assertEqual(data["visits"], [])
		self.assertEqual(data["count"], 0)
		self.assertEqual(data["ungrouped_completions"], 1)

	def test_the_ungrouped_ones_are_counted_and_explained(self):
		self.a_completed_task(title="Cabin 1")
		self.a_completed_task(visit_id=MORNING, title="Cabin 2")
		data = self.tool_data("list_visits")
		self.assertEqual(data["count"], 1)
		self.assertEqual(data["ungrouped_completions"], 1)
		self.assertIn("before v0.20.1", data["ungrouped_note"])

	def test_an_unfinished_task_is_not_in_a_visit_even_carrying_one(self):
		"""A visit is made of COMPLETIONS. A claimed task is somebody's intention."""
		task = self.a_task(evidence_required={"findings_text": True})
		self.worker_data("claim_task_via_mobile", {"task_name": task["name"]})
		frappe.db.set_value(
			"Farm Task Assignment", STORE.rows("Farm Task Assignment")[0]["name"], "visit_id", MORNING
		)
		self.assertEqual(self.visits(), [])

	def test_a_site_with_no_completions_says_so_without_inventing_a_visit(self):
		data = self.tool_data("list_visits")
		self.assertEqual(data["visits"], [])
		self.assertEqual(data["ungrouped_completions"], 0)
		self.assertNotIn("ungrouped_note", data)


class TheFilters(VisitTestCase):
	def test_company_scope_is_respected(self):
		self.a_completed_task(visit_id=MORNING)
		self.assertEqual(len(self.visits(company=MAIN)), 1)
		self.assertEqual(self.visits(company=OTHER), [])

	def test_a_scoped_account_sees_only_its_own_entity(self):
		"""The tool reads through `frappe.db.get_all`, which does NOT consult
		User Permissions — the scoping has to be explicit or it is not there."""
		self.a_completed_task(visit_id=MORNING)
		STORE.seed(
			"User Permission",
			[
				{
					"name": "UP-VISIT-1",
					"user": frappe.session.user,
					"allow": "Company",
					"for_value": OTHER,
				}
			],
		)
		self.assertEqual(self.visits(), [])

	def test_the_worker_filter_names_one_person(self):
		self.a_completed_task(visit_id=MORNING)
		self.assertEqual(len(self.visits(worker=WORKER_EMPLOYEE)), 1)
		self.assertEqual(self.visits(worker="EMP-BEN"), [])

	def test_the_period_filter_bounds_the_completions(self):
		self.a_completed_task(visit_id=MORNING)
		today = frappe.utils.today()
		self.assertEqual(len(self.visits(from_date=today, to_date=today)), 1)
		tomorrow = str(frappe.utils.add_days(today, 1))
		self.assertEqual(self.visits(from_date=tomorrow), [])

	def test_the_location_filter_returns_the_visit_whole(self):
		"""A trip that also went elsewhere is returned with all of its work.
		Reporting a visit missing half its tasks would answer a different
		question from the one that was asked."""
		unit = self.a_camp("MC-Cabin-01")
		other = self.a_camp("MC-Cabin-02")
		self.a_completed_task(
			visit_id=MORNING, title="Cabin 1", location_doctype="Housing Unit", location=unit
		)
		self.a_completed_task(
			visit_id=MORNING, title="Cabin 2", location_doctype="Housing Unit", location=other
		)

		visit = self.sole(location=unit)
		self.assertEqual(visit["total_tasks"], 2)
		self.assertEqual(sorted(visit["locations"]), sorted([unit, other]))
		self.assertIsNone(visit["location"], "a visit spanning two places names neither")

	def test_a_location_nothing_was_done_at_returns_nothing(self):
		unit = self.a_camp("MC-Cabin-01")
		self.a_completed_task(visit_id=MORNING, location_doctype="Housing Unit", location=unit)
		self.assertEqual(self.visits(location="MC-Cabin-99"), [])

	def test_a_single_place_visit_names_it(self):
		unit = self.a_camp("MC-Cabin-01")
		self.a_completed_task(
			visit_id=MORNING, title="Cabin 1", location_doctype="Housing Unit", location=unit
		)
		self.a_completed_task(
			visit_id=MORNING, title="Cabin 1 again", location_doctype="Housing Unit", location=unit
		)
		self.assertEqual(self.sole()["location"], unit)


class TheDerivedFigures(VisitTestCase):
	def test_the_span_runs_first_completion_to_last(self):
		self.a_completed_task(visit_id=MORNING, title="Cabin 1")
		self.a_completed_task(visit_id=MORNING, title="Cabin 2")
		visit = self.sole()
		self.assertLessEqual(visit["first_completion_datetime"], visit["last_completion_datetime"])
		self.assertGreaterEqual(visit["duration_minutes"], 0)

	def test_a_one_task_visit_measures_zero_rather_than_guessing(self):
		"""One completion is one instant. The only honest thing this record can
		say about how long that trip took is nothing."""
		self.a_completed_task(visit_id=AFTERNOON)
		self.assertEqual(self.sole()["duration_minutes"], 0)

	def test_evidence_is_counted_by_file_and_not_by_row(self):
		"""One signature filed against two cabins is ONE photograph. Counting
		rows would report two and make the trip look better evidenced."""
		self.a_completed_task(visit_id=MORNING, title="Cabin 1")
		self.a_completed_task(visit_id=MORNING, title="Cabin 2")
		self.assertEqual(self.sole()["total_evidence_files"], 1)

	def test_two_different_files_are_two(self):
		task = self.a_task(task_name="Cabin 1", evidence_required={"photos": True})
		self.worker_data("claim_task_via_mobile", {"task_name": task["name"]})
		self.worker_data("start_task_via_mobile", {"task_name": task["name"]})
		self.worker_data(
			"complete_task_via_mobile",
			{
				"task_name": task["name"],
				"evidence": list(A_PHOTO) + list(ANOTHER_PHOTO),
				"visit_id": MORNING,
			},
		)
		self.assertEqual(self.sole()["total_evidence_files"], 2)

	def test_the_logged_duration_is_reported_beside_the_span_and_is_not_it(self):
		"""What the workers recorded per task and how long the trip took are two
		different numbers, and a report that showed one as the other would be
		wrong in whichever direction the walking took."""
		self.a_completed_task(visit_id=MORNING, title="Cabin 1")
		visit = self.sole()
		self.assertIn("logged_duration_minutes", visit)
		self.assertIn("duration_minutes", visit)

	def test_the_total_task_count_spans_every_visit(self):
		self.a_completed_task(visit_id=MORNING, title="Cabin 1")
		self.a_completed_task(visit_id=MORNING, title="Cabin 2")
		self.a_completed_task(visit_id=AFTERNOON, title="Pump house")
		self.assertEqual(self.tool_data("list_visits")["total_tasks"], 3)

	def test_a_visit_two_workers_used_names_neither_but_lists_both(self):
		"""A client CAN produce this and it is not this tool's to adjudicate.
		Reporting None beside the full list is the answer that picks no winner."""
		self.a_completed_task(visit_id=MORNING, title="Cabin 1")
		self.enrol(email="ben@example.test", name="Ben Ortiz")
		frappe.db.set_value(
			"Farm Task Assignment", STORE.rows("Farm Task Assignment")[0]["name"], "assigned_to", "EMP-BEN"
		)
		self.a_completed_task(visit_id=MORNING, title="Cabin 2")

		visit = self.sole()
		self.assertIsNone(visit["completing_user"])
		self.assertEqual(sorted(visit["completing_users"]), sorted(["EMP-BEN", WORKER_EMPLOYEE]))


class TheGuards(VisitTestCase):
	def test_an_account_with_no_hr_role_is_refused(self):
		set_roles(frappe.session.user, ["Accounts Manager"])
		message = self.tool_error("list_visits", {})
		self.assertIn("may not change the personnel register", message)

	def test_the_switch_turns_it_off_individually(self):
		self.configure(enabled=1, allow_list_visits=0)
		message = self.tool_error("list_visits", {})
		self.assertIn("allow_list_visits", message)
		self.assertIn("switched off", message)

	def test_it_is_on_by_default_because_it_is_a_read(self):
		from erpnext_mcp import registry

		self.assertFalse(registry.TOOLS["list_visits"]["mutating"])
		self.assertIn("list_visits", registry.READ_TOOLS)

	def test_it_writes_nothing(self):
		self.a_completed_task(visit_id=MORNING, title="Cabin 1")
		self.a_completed_task(visit_id=MORNING, title="Cabin 2")
		before = {doctype: len(rows) for doctype, rows in STORE.tables.items() if doctype != "MCP Action Log"}
		self.tool_data("list_visits")
		self.tool_data("list_visits", {"company": MAIN})
		after = {doctype: len(rows) for doctype, rows in STORE.tables.items() if doctype != "MCP Action Log"}
		self.assertEqual(before, after)

	def test_the_limit_is_capped_rather_than_taken_on_trust(self):
		self.a_completed_task(visit_id=MORNING)
		self.assertEqual(self.tool_data("list_visits", {"limit": 100000})["limit"], 500)


class TheIdentifierIsChecked(VisitTestCase):
	"""Claim 6 — the shapes the grouping key will not take.

	Each of these is a real client bug rather than an invented one: a UUID with
	its hyphens stripped, one that lost a character on its way through a URL, one
	still wearing the braces some platforms print, and a human label somebody
	typed because the field looked like a note. Every one of them would have been
	stored verbatim before this, and every one would have become its own visit.
	"""

	#: (what a client did, what arrived)
	REFUSED = (
		("hyphens stripped", "5C1F0A642B3D4E5F8A9B0C1D2E3F4A5B"),
		("a character short", "5C1F0A64-2B3D-4E5F-8A9B-0C1D2E3F4A5"),
		("a character over", "5C1F0A64-2B3D-4E5F-8A9B-0C1D2E3F4A5BC"),
		("not hex", "5C1F0A64-2B3D-4E5F-8A9B-0C1D2E3F4A5Z"),
		("regrouped", "5C1F0A642-B3D-4E5F-8A9B-0C1D2E3F4A5B"),
		("braces kept", "{5C1F0A64-2B3D-4E5F-8A9B-0C1D2E3F4A5B}"),
		("something appended", "5C1F0A64-2B3D-4E5F-8A9B-0C1D2E3F4A5B north"),
		("a label somebody typed", "north-block-am"),
	)

	def a_started_task(self, title="Habitability walk"):
		task = self.a_task(task_name=title, evidence_required={"findings_text": True})
		self.worker_data("claim_task_via_mobile", {"task_name": task["name"]})
		self.worker_data("start_task_via_mobile", {"task_name": task["name"]})
		return task["name"]

	def a_completion(self, task, visit_id):
		return {
			"task_name": task,
			"findings_text": "",
			"evidence": list(A_PHOTO),
			"visit_id": visit_id,
		}

	def test_none_of_them_is_accepted_and_the_message_names_the_value(self):
		"""ONE TASK, EIGHT ATTEMPTS. A refused completion writes nothing at all, so
		the task is still in the worker's hands and still completable — which is
		also the shape of the real failure: a handset retrying a bad payload has
		not lost the work."""
		task = self.a_started_task()
		for what, value in self.REFUSED:
			with self.subTest(what):
				message = self.worker_error("complete_task_via_mobile", self.a_completion(task, value))
				self.assertIn(value, message)
				self.assertIn("8-4-4-4-12", message)

	def test_and_so_none_of_them_becomes_a_visit(self):
		"""The point of the whole class in one assertion: after eight refusals and
		one good completion the rollup is ONE trip, not nine."""
		task = self.a_started_task()
		for _what, value in self.REFUSED:
			self.worker_error("complete_task_via_mobile", self.a_completion(task, value))
		self.assertEqual(self.visits(), [])
		self.worker_data("complete_task_via_mobile", self.a_completion(task, MORNING))
		self.assertEqual(self.sole()["visit_id"], MORNING)

	def test_the_identifier_the_handset_mints_still_goes_straight_through(self):
		"""The check is worth having only if it is invisible to the real client."""
		self.a_completed_task(visit_id=MORNING, title="Cabin 1")
		self.assertEqual(self.sole()["visit_id"], MORNING)


if __name__ == "__main__":
	import unittest

	unittest.main()

# SPDX-License-Identifier: MIT
"""The crew drill-down — department → crew → worker, and the fourth ending.

SEVEN CLAIMS.

1. `TheHierarchyIsDepartmentCrewWorker` — the overview groups open shifts under
   the department of their FOREMAN, puts the crew under each, and carries the
   job each person is on and the buckets they have picked.

2. `TheForemanSeesTheirOwnDepartment` — the claim the release is actually about.
   A Foreman over Harvest sees Harvest's crew and NOT the Packhouse's, on a
   single-company farm where entity scoping separates nothing. A Farm Manager
   sees both. The negative half is the one that matters: the Packhouse crew must
   be ABSENT, not merely unmarked.

3. `TheEmptyScopeSaysWhy` — three ways to have no department scope, and none of
   them falls open to the whole farm. An account with no linked Employee, and an
   Employee with no department, both get an empty overview WITH a sentence.

4. `TheDepartmentTreeIsWalkedDown` — a child department's crews are visible to
   the parent's foreman and the child's foreman does not see the parent's.

5. `TheWorkerDetailWithholds` — the PII claim, asserted as an ABSENCE. Date of
   birth, home address and every I-9 document field must not appear anywhere in
   the payload, and the I-9 must contribute its status word and nothing else.

6. `TheStaleShiftIsFenced` — a shift younger than the threshold is refused BY
   NAME with `end_shift` named; so are a closed shift, a missing reason, a
   missing end time, an end before the start and an end in the future.

7. `TheStaleCloseReleasesTheCrew` — the close sets `left_at` on everybody still
   on the shift, writes one Attendance row each, and reports the supervisor
   review as OWED when no signature was passed.

8. `TheActiveShiftBoard` — the flat read carries the six columns the brief asks
   for, applies the SAME entity and department scoping as the nested one, and
   AGREES WITH IT on which shifts are open. The agreement is the test worth
   having: two functions each deriving "which shifts are open" is how a farm
   ends up with a board showing four crews and a drill-down showing three.
"""

import json

import frappe

from erpnext_mcp import compliance_fields, shifts
from erpnext_mcp.tools import crew_view
from erpnext_mcp.tools import employee as personnel

from .fixtures import MAIN, OTHER, V12TestCase, install_hrms
from .harness import ROLES, STORE, set_roles

#: Every switch this suite needs, listed rather than globbed so that turning one
#: off in a test is visibly a change from the on-by-default posture.
ON = {
	f"allow_{name}": 1
	for name in (
		"get_crew_overview",
		"list_active_shifts",
		"get_worker_detail",
		"end_stale_shift",
		"start_shift",
		"end_shift",
		"get_shift",
		"list_shifts",
	)
}

HARVEST = "Harvest - ETC"
HARVEST_NIGHT = "Harvest Night - ETC"
PACKHOUSE = "Packhouse - ETC"

#: The two foremen and the two crews they run. Both at MAIN, which is the point:
#: entity scoping cannot separate them and department scoping must.
HARVEST_FOREMAN = "HR-EMP-CV001"
PACKHOUSE_FOREMAN = "HR-EMP-CV002"
NIGHT_FOREMAN = "HR-EMP-CV003"
HARVEST_PICKER = "HR-EMP-CV010"
HARVEST_PICKER_TWO = "HR-EMP-CV011"
PACKHOUSE_HAND = "HR-EMP-CV020"
NIGHT_PICKER = "HR-EMP-CV030"

HARVEST_LOGIN = "harvest.foreman@example.test"
PACKHOUSE_LOGIN = "packhouse.foreman@example.test"
NIGHT_LOGIN = "night.foreman@example.test"
UNLINKED_LOGIN = "nobody.linked@example.test"

SIGNATURE = "/files/crew-view-signature.png"


class CrewViewTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		install_hrms()
		compliance_fields.install_compliance_fields(respect_switch=False)

		# THE CLOCK DRIFTS, so every fixture stamp is computed BACKWARDS from the
		# harness's own `now` captured here. `harness._now()` is a fixed base plus
		# one second per call and the fixture site burns hundreds of those before
		# this line runs, so a hardcoded timestamp lands an unpredictable distance
		# from "now" — often in the future, where every backwards window matches
		# everything and a staleness assertion passes against a tool that
		# subtracts nothing.
		self.now = frappe.utils.now()

		STORE.seed(
			"Department",
			[
				{
					"name": HARVEST,
					"department_name": "Harvest",
					"company": MAIN,
					"is_group": 1,
					"parent_department": None,
				},
				{
					"name": HARVEST_NIGHT,
					"department_name": "Harvest Night",
					"company": MAIN,
					"is_group": 0,
					"parent_department": HARVEST,
				},
				{
					"name": PACKHOUSE,
					"department_name": "Packhouse",
					"company": MAIN,
					"is_group": 0,
					"parent_department": None,
				},
			],
		)
		STORE.seed(
			"Employee",
			[
				self._person(HARVEST_FOREMAN, "Ada Orchard", HARVEST, "Foreman", user_id=HARVEST_LOGIN),
				self._person(
					PACKHOUSE_FOREMAN, "Bo Packhouse", PACKHOUSE, "Foreman", user_id=PACKHOUSE_LOGIN
				),
				self._person(NIGHT_FOREMAN, "Cy Nightshift", HARVEST_NIGHT, "Foreman", user_id=NIGHT_LOGIN),
				self._person(HARVEST_PICKER, "Ana Ramos", HARVEST, "Picker"),
				self._person(HARVEST_PICKER_TWO, "Ben Cruz", HARVEST, "Picker"),
				self._person(PACKHOUSE_HAND, "Dee Sorter", PACKHOUSE, "Sorter"),
				self._person(NIGHT_PICKER, "Eli Moon", HARVEST_NIGHT, "Picker"),
			],
		)
		self._roles_before = {user: list(held) for user, held in ROLES.items()}
		self.addCleanup(self._restore_roles)

	def _restore_roles(self):
		ROLES.clear()
		ROLES.update(self._roles_before)

	def _person(self, name, display, department, designation, user_id=""):
		row = {
			"name": name,
			"employee_name": display,
			"status": "Active",
			"date_of_joining": "2026-06-01",
			"company": MAIN,
			"department": department,
			"designation": designation,
			# SEEDED SO THE WITHHOLDING TEST HAS SOMETHING TO WITHHOLD. A payload
			# that omits a date of birth nobody stored proves nothing.
			"date_of_birth": "1994-03-02",
		}
		if user_id:
			row["user_id"] = user_id
		return row

	# -- helpers -------------------------------------------------------------
	def hours_ago(self, hours: float) -> str:
		return frappe.utils.add_to_date(self.now, hours=-hours, as_string=True, as_datetime=True)

	def open_shift(self, name, foreman, crew, started_hours_ago=3.0, **overrides):
		"""One OPEN Farm Shift, seeded directly so its age is exactly stated.

		SEEDED RATHER THAN DRIVEN THROUGH `start_shift`, which is the right call
		here and not a shortcut: `start_shift` stamps `joined_at` from the shift's
		own start and refuses a second open shift per person, and what these tests
		need is a shift of a PRECISE AGE — twenty hours old for the stale case,
		two for the fenced one. Driving the tool would put the age at the mercy of
		the drifting harness clock, which is the thing the fixture is pinned
		against.
		"""
		start = self.hours_ago(started_hours_ago)
		row = {
			"name": name,
			"foreman": foreman,
			"foreman_name": "",
			"company": MAIN,
			"shift_type": "Harvest",
			"location": "Block 7 North",
			"start_datetime": start,
			"end_datetime": None,
			"cancelled": 0,
			"crew": [
				{
					"employee": person,
					"employee_name": "",
					"role": "Worker",
					"joined_at": start,
					"left_at": None,
				}
				for person in crew
			],
		}
		row.update(overrides)
		STORE.seed(shifts.DOCTYPE, [row])
		return name

	# -- who this test is acting as ------------------------------------------
	#
	# THE IDENTITY ONLY SURVIVES ONE CALL AND THAT IS THE APP'S OWN BEHAVIOUR,
	# not a defect in the double. `security.capture_calling_user` saves
	# `session.user` at the start of a request and `mcp.handle` then runs
	# `frappe.set_user(effective_user())`, which on this transport is the MCP
	# System User — Administrator here. So the second call of a test would read
	# `caller_identity()` as Administrator unless the session is re-asserted, and
	# Administrator holds a management role.
	#
	# THAT IS EXACTLY THE VACUOUS PASS THIS OVERRIDE EXISTS TO PREVENT. Setting
	# `session.user` once in a helper and then making three calls tests the
	# intended principal on the first and the system user on the other two, and a
	# scoping assertion that runs as a manager passes for the wrong reason. It
	# was caught by a Crew Leader being allowed a worker detail the first version
	# of these tests claimed to refuse.
	def acting(self, login: str, roles: list):
		self._acting = (login, list(roles))
		frappe.local.session.user = login
		set_roles(login, roles)
		return login

	def call(self, *args, **kwargs):
		"""Re-assert the acting principal before every JSON-RPC call."""
		login, roles = getattr(self, "_acting", (None, None))
		if login:
			frappe.local.session.user = login
			set_roles(login, roles)
		return super().call(*args, **kwargs)

	def as_foreman(self, login=HARVEST_LOGIN):
		"""Act as a Foreman with a linked Employee record, which is the case the
		department scope is computed for."""
		return self.acting(login, ["Foreman"])

	def as_manager(self, login="manager@example.test"):
		return self.acting(login, ["Farm Manager"])

	def raw(self, name: str) -> dict:
		return dict(STORE.get_raw(shifts.DOCTYPE, name) or {})

	def crew_rows(self, name: str) -> list:
		return list(self.raw(name).get("crew") or [])

	def overview(self, **arguments):
		return self.tool_data("get_crew_overview", arguments)

	def detail(self, employee, **arguments):
		return self.tool_data("get_worker_detail", {"employee": employee, **arguments})

	def crews_in(self, data) -> dict:
		"""shift docname → the crew block, flattened across departments."""
		return {
			crew["shift"]: crew
			for department in data.get("departments") or []
			for crew in department.get("crews") or []
		}


class TheHierarchyIsDepartmentCrewWorker(CrewViewTestCase):
	"""Departments hold crews, crews hold workers, and the department of a crew
	is read through its foreman."""

	def test_the_overview_groups_open_shifts_under_their_foremans_department(self):
		self.as_manager()
		self.open_shift("SHIFT-CV-0001", HARVEST_FOREMAN, [HARVEST_PICKER, HARVEST_PICKER_TWO])
		self.open_shift("SHIFT-CV-0002", PACKHOUSE_FOREMAN, [PACKHOUSE_HAND])

		data = self.overview()
		by_department = {entry["department"]: entry for entry in data["departments"]}
		self.assertEqual(set(by_department), {HARVEST, PACKHOUSE})
		self.assertEqual(
			[crew["shift"] for crew in by_department[HARVEST]["crews"]],
			["SHIFT-CV-0001"],
		)
		self.assertEqual(
			[crew["shift"] for crew in by_department[PACKHOUSE]["crews"]],
			["SHIFT-CV-0002"],
		)
		self.assertEqual(by_department[HARVEST]["department_name"], "Harvest")

	def test_a_crew_carries_its_leader_start_time_and_worker_count(self):
		self.as_manager()
		self.open_shift("SHIFT-CV-0003", HARVEST_FOREMAN, [HARVEST_PICKER, HARVEST_PICKER_TWO])

		crew = self.crews_in(self.overview())["SHIFT-CV-0003"]
		self.assertEqual(crew["crew_leader"], HARVEST_FOREMAN)
		self.assertEqual(crew["crew_leader_name"], "Ada Orchard")
		self.assertEqual(crew["worker_count"], 2)
		self.assertEqual(crew["still_on_shift"], 2)
		self.assertIsNotNone(crew["start_datetime"])
		self.assertIsNotNone(crew["hours_open"])
		self.assertEqual(
			{worker["employee"] for worker in crew["workers"]}, {HARVEST_PICKER, HARVEST_PICKER_TWO}
		)

	def test_a_worker_carries_the_job_they_are_on_and_their_bucket_count(self):
		self.as_manager()
		self.open_shift("SHIFT-CV-0004", HARVEST_FOREMAN, [HARVEST_PICKER, HARVEST_PICKER_TWO])
		STORE.seed(
			"Farm Task Assignment",
			[
				{
					"name": "FTA-CV-0001",
					"task": "TASK-CV-0001",
					"task_name": "Pick Block 7 North",
					"assigned_to": HARVEST_PICKER,
					"state": "In-Progress",
					"company": MAIN,
					"farm_shift": "SHIFT-CV-0004",
				}
			],
		)
		STORE.seed(
			"Bucket Log Entry",
			[
				{
					"name": f"BLE-CV-{index:04d}",
					"employee": HARVEST_PICKER,
					"shift": "SHIFT-CV-0004",
					"verdict": "Accepted",
					"company": MAIN,
				}
				for index in range(3)
			],
		)

		workers = {
			worker["employee"]: worker
			for worker in self.crews_in(self.overview())["SHIFT-CV-0004"]["workers"]
		}
		self.assertEqual(workers[HARVEST_PICKER]["current_task"]["task_name"], "Pick Block 7 North")
		self.assertEqual(workers[HARVEST_PICKER]["current_task"]["state"], "In-Progress")
		self.assertEqual(workers[HARVEST_PICKER]["bucket_count"], 3)
		# The worker with no assignment and no buckets is REPORTED with nothing
		# against their name rather than dropped — an idle picker is on the crew.
		self.assertIsNone(workers[HARVEST_PICKER_TWO]["current_task"])
		self.assertEqual(workers[HARVEST_PICKER_TWO]["bucket_count"], 0)

	def test_a_closed_shift_is_not_a_crew(self):
		"""The overview is who is out RIGHT NOW. A shift with an end time is not."""
		self.as_manager()
		self.open_shift(
			"SHIFT-CV-0005",
			HARVEST_FOREMAN,
			[HARVEST_PICKER],
			end_datetime=self.hours_ago(1),
		)
		self.assertEqual(self.overview()["crew_count"], 0)

	def test_a_cancelled_shift_is_not_a_crew_either(self):
		self.as_manager()
		self.open_shift("SHIFT-CV-0006", HARVEST_FOREMAN, [HARVEST_PICKER], cancelled=1)
		self.assertEqual(self.overview()["crew_count"], 0)

	def test_a_crew_open_past_the_threshold_is_flagged_stale_and_listed(self):
		self.as_manager()
		self.open_shift("SHIFT-CV-0007", HARVEST_FOREMAN, [HARVEST_PICKER], started_hours_ago=20)
		self.open_shift("SHIFT-CV-0008", PACKHOUSE_FOREMAN, [PACKHOUSE_HAND], started_hours_ago=2)

		data = self.overview()
		crews = self.crews_in(data)
		self.assertTrue(crews["SHIFT-CV-0007"]["stale"])
		self.assertFalse(crews["SHIFT-CV-0008"]["stale"])
		self.assertEqual(data["stale_shifts"], ["SHIFT-CV-0007"])
		self.assertIn("end_stale_shift", data["stale_note"])


class TheForemanSeesTheirOwnDepartment(CrewViewTestCase):
	"""The claim the release is about, on a single-company farm where entity
	scoping separates nothing at all."""

	def test_a_foreman_sees_their_own_departments_crew(self):
		self.as_foreman(HARVEST_LOGIN)
		self.open_shift("SHIFT-CV-0100", HARVEST_FOREMAN, [HARVEST_PICKER])

		data = self.overview()
		self.assertEqual([entry["department"] for entry in data["departments"]], [HARVEST])
		self.assertEqual(data["scope"]["kind"], "department")
		self.assertEqual(data["scope"]["actor_department"], HARVEST)

	def test_a_foreman_does_not_see_another_departments_crew(self):
		"""THE NEGATIVE HALF, AND THE ONE THAT MATTERS. Both crews are at MAIN,
		so `require_company_scope` passes on both and only the department scope
		can tell them apart."""
		self.as_foreman(HARVEST_LOGIN)
		self.open_shift("SHIFT-CV-0101", HARVEST_FOREMAN, [HARVEST_PICKER])
		self.open_shift("SHIFT-CV-0102", PACKHOUSE_FOREMAN, [PACKHOUSE_HAND])

		data = self.overview()
		crews = self.crews_in(data)
		self.assertIn("SHIFT-CV-0101", crews)
		self.assertNotIn("SHIFT-CV-0102", crews)
		# The Packhouse hand's NAME must not appear anywhere in the payload —
		# a crew that is merely unlabelled is still a crew that leaked.
		self.assertNotIn("Dee Sorter", json.dumps(data, default=str))
		self.assertIn("1 open crew(s)", data["withheld_note"])

	def test_a_manager_sees_every_department_in_their_entities(self):
		self.as_manager()
		self.open_shift("SHIFT-CV-0103", HARVEST_FOREMAN, [HARVEST_PICKER])
		self.open_shift("SHIFT-CV-0104", PACKHOUSE_FOREMAN, [PACKHOUSE_HAND])

		data = self.overview()
		self.assertEqual(data["scope"]["kind"], "entity")
		self.assertIsNone(data["scope"]["departments"])
		self.assertEqual(set(self.crews_in(data)), {"SHIFT-CV-0103", "SHIFT-CV-0104"})
		self.assertNotIn("withheld_note", data)

	def test_the_entity_scope_still_bites_underneath_the_department_one(self):
		"""Department scoping is a SECOND restriction and not a replacement."""
		self.as_manager()
		STORE.seed(
			"Employee",
			[
				{
					"name": "HR-EMP-CV099",
					"employee_name": "Fay Elsewhere",
					"status": "Active",
					"company": OTHER,
					"department": "Harvest - SEL",
					"date_of_joining": "2026-06-01",
				}
			],
		)
		self.open_shift("SHIFT-CV-0105", HARVEST_FOREMAN, [HARVEST_PICKER])
		self.open_shift("SHIFT-CV-0106", "HR-EMP-CV099", ["HR-EMP-CV099"], company=OTHER)

		crews = self.crews_in(self.overview(company=MAIN))
		self.assertIn("SHIFT-CV-0105", crews)
		self.assertNotIn("SHIFT-CV-0106", crews)


class TheEmptyScopeSaysWhy(CrewViewTestCase):
	"""Three ways to have no department scope, and none of them opens the farm."""

	def test_a_login_with_no_employee_record_gets_nothing_and_a_sentence(self):
		self.acting(UNLINKED_LOGIN, ["Foreman"])
		self.open_shift("SHIFT-CV-0200", HARVEST_FOREMAN, [HARVEST_PICKER])

		data = self.overview()
		self.assertEqual(data["departments"], [])
		self.assertEqual(data["scope"]["departments"], [])
		self.assertIn("not linked to an Employee record", data["scope"]["reason"])
		self.assertIn("link_employee_to_user", data["note"])
		# THE POINT: it did not fall open. The crew exists and was withheld.
		self.assertEqual(data["crew_count"], 0)
		self.assertIn("1 open crew(s)", data["withheld_note"])

	def test_an_employee_with_no_department_gets_nothing_and_a_sentence(self):
		STORE.seed(
			"Employee",
			[
				{
					"name": "HR-EMP-CV050",
					"employee_name": "Gus Nodept",
					"status": "Active",
					"company": MAIN,
					"department": None,
					"date_of_joining": "2026-06-01",
					"user_id": "gus@example.test",
				}
			],
		)
		self.acting("gus@example.test", ["Foreman"])
		self.open_shift("SHIFT-CV-0201", HARVEST_FOREMAN, [HARVEST_PICKER])

		data = self.overview()
		self.assertEqual(data["departments"], [])
		self.assertEqual(data["scope"]["actor_employee"], "HR-EMP-CV050")
		self.assertIsNone(data["scope"]["actor_department"])
		self.assertIn("no department on", data["scope"]["reason"])
		self.assertIn("NOT widened to the whole farm", data["scope"]["reason"])

	def test_a_quiet_morning_is_worded_differently_from_a_scoping_problem(self):
		"""Both answers are an empty list and they are different facts."""
		self.as_manager()
		data = self.overview()
		self.assertEqual(data["departments"], [])
		self.assertIn("No crew is open on this farm right now", data["note"])
		self.assertNotIn("withheld_note", data)


class TheDepartmentTreeIsWalkedDown(CrewViewTestCase):
	"""`parent_department`, downward only."""

	def test_a_parents_foreman_sees_a_child_departments_crew(self):
		self.as_foreman(HARVEST_LOGIN)
		self.open_shift("SHIFT-CV-0300", HARVEST_FOREMAN, [HARVEST_PICKER])
		self.open_shift("SHIFT-CV-0301", NIGHT_FOREMAN, [NIGHT_PICKER])

		data = self.overview()
		self.assertEqual({entry["department"] for entry in data["departments"]}, {HARVEST, HARVEST_NIGHT})
		self.assertEqual(data["scope"]["departments"], sorted([HARVEST, HARVEST_NIGHT]))

	def test_a_childs_foreman_does_not_see_the_parents_crew(self):
		"""THE WALK IS DOWNWARD AND ONLY DOWNWARD. A night-shift lead does not
		acquire the day crew by being underneath it."""
		self.as_foreman(NIGHT_LOGIN)
		self.open_shift("SHIFT-CV-0302", HARVEST_FOREMAN, [HARVEST_PICKER])
		self.open_shift("SHIFT-CV-0303", NIGHT_FOREMAN, [NIGHT_PICKER])

		crews = self.crews_in(self.overview())
		self.assertEqual(set(crews), {"SHIFT-CV-0303"})

	def test_the_walk_terminates_on_a_cycle(self):
		"""A hand-edited tree can point a parent at its own child."""
		STORE.seed(
			"Department",
			[
				{
					"name": HARVEST,
					"department_name": "Harvest",
					"company": MAIN,
					"parent_department": HARVEST_NIGHT,
				}
			],
		)
		self.assertEqual(sorted(crew_view._descendants_of(HARVEST)), sorted([HARVEST, HARVEST_NIGHT]))


class TheWorkerDetailWithholds(CrewViewTestCase):
	"""The PII claim, asserted as an absence rather than as a shape."""

	def test_it_answers_the_crew_screen_questions(self):
		self.as_foreman(HARVEST_LOGIN)
		self.open_shift("SHIFT-CV-0400", HARVEST_FOREMAN, [HARVEST_PICKER])

		data = self.detail(HARVEST_PICKER)
		self.assertEqual(data["employee"], HARVEST_PICKER)
		self.assertEqual(data["employee_name"], "Ana Ramos")
		self.assertEqual(data["designation"], "Picker")
		self.assertEqual(data["department"], HARVEST)
		self.assertEqual(data["current_shift"]["shift"], "SHIFT-CV-0400")
		self.assertEqual(data["current_shift"]["crew_leader_name"], "Ada Orchard")
		self.assertTrue(data["current_shift"]["still_on_shift"])

	def test_the_payload_carries_no_date_of_birth_and_no_home_address(self):
		"""SEEDED AND THEN ABSENT. `date_of_birth` is on the fixture record, so
		this is a statement about what the tool withholds rather than about what
		the double happens not to store."""
		self.as_foreman(HARVEST_LOGIN)
		self.open_shift("SHIFT-CV-0401", HARVEST_FOREMAN, [HARVEST_PICKER])

		self.assertEqual(frappe.db.get_value("Employee", HARVEST_PICKER, "date_of_birth"), "1994-03-02")
		body = json.dumps(self.detail(HARVEST_PICKER), default=str)
		self.assertNotIn("1994-03-02", body)
		self.assertNotIn("date_of_birth", body)
		for field in ("ssn", "current_address", "bank_ac_no", "ctc", "salary"):
			self.assertNotIn(field, body, f"{field} reached a crew screen")

	def test_the_i9_contributes_its_status_word_and_nothing_else(self):
		self.as_foreman(HARVEST_LOGIN)
		self.open_shift("SHIFT-CV-0402", HARVEST_FOREMAN, [HARVEST_PICKER])
		STORE.seed(
			"I-9 Form",
			[
				{
					"name": "I9-CV-0001",
					"employee": HARVEST_PICKER,
					"employee_name": "Ana Ramos",
					"company": MAIN,
					"status": "Reverification Needed",
					"ssn_full": "123-45-6789",
					"ssn_last_four": "6789",
					"date_of_birth": "1994-03-02",
					"foreign_passport_number": "X1234567",
					"alien_work_authorization_expiry": "2026-11-30",
				}
			],
		)

		data = self.detail(HARVEST_PICKER)
		self.assertEqual(data["compliance"]["i9"]["status"], "Reverification Needed")
		self.assertTrue(data["compliance"]["i9"]["reverification_needed"])
		self.assertEqual(data["compliance"]["i9"]["work_authorization_expires"], "2026-11-30")

		body = json.dumps(data, default=str)
		self.assertNotIn("123-45-6789", body)
		self.assertNotIn("6789", body)
		self.assertNotIn("X1234567", body)

	def test_a_destroyed_i9_never_answers_for_a_live_one(self):
		"""THE REHIRE PATH PUTS TWO ROWS ON ONE WORKER BY CONSTRUCTION.
		`destroy_i9` sets the status and SAVES the row rather than deleting it,
		and `create_i9_form` refuses a new form until the old one is Destroyed —
		so a rehired worker has two, and one of them is the one nothing should
		read.

		THE DIRECTION OF FAILURE IS WHY THIS IS A TEST AND NOT A TIDY-UP. The
		destroyed row here says `Complete` and the live one says `Reverification
		Needed`. Reading the wrong one shows a worker who may not legally work
		today as clear, on the screen a foreman uses to decide exactly that.

		SEEDED DESTROYED-FIRST so that a reader taking whichever row comes back
		first would take the destroyed one, which is what the standalone double's
		insertion order does. A one-row fixture proves nothing here."""
		self.as_foreman(HARVEST_LOGIN)
		STORE.seed(
			"I-9 Form",
			[
				{
					"name": "I9-CV-0010",
					"employee": HARVEST_PICKER,
					"company": MAIN,
					"status": "Destroyed",
					"alien_work_authorization_expiry": "2020-01-01",
				},
				{
					"name": "I9-CV-0011",
					"employee": HARVEST_PICKER,
					"company": MAIN,
					"status": "Reverification Needed",
					"alien_work_authorization_expiry": "2026-11-30",
				},
			],
		)
		i9 = self.detail(HARVEST_PICKER)["compliance"]["i9"]
		self.assertEqual(i9["form"], "I9-CV-0011")
		self.assertEqual(i9["status"], "Reverification Needed")
		self.assertTrue(i9["reverification_needed"])
		self.assertEqual(i9["work_authorization_expires"], "2026-11-30")

	def test_a_worker_whose_only_i9_is_destroyed_reads_as_having_none(self):
		"""A destroyed form is not a form on file, and saying otherwise would
		report a retention-expired record as current work authorisation."""
		self.as_foreman(HARVEST_LOGIN)
		STORE.seed(
			"I-9 Form",
			[
				{
					"name": "I9-CV-0012",
					"employee": HARVEST_PICKER,
					"company": MAIN,
					"status": "Destroyed",
				}
			],
		)
		i9 = self.detail(HARVEST_PICKER)["compliance"]["i9"]
		self.assertIsNone(i9["status"])
		self.assertIn("No I-9 is on file", i9["note"])

	def test_a_missing_i9_is_reported_rather_than_left_blank(self):
		self.as_foreman(HARVEST_LOGIN)
		data = self.detail(HARVEST_PICKER)
		self.assertIsNone(data["compliance"]["i9"]["status"])
		self.assertIn("No I-9 is on file", data["compliance"]["i9"]["note"])

	def test_training_currency_comes_back_per_curriculum(self):
		"""The same four words `get_training_compliance_report` uses, through the
		same cell function, so the two cannot disagree about what expired means."""
		self.as_foreman(HARVEST_LOGIN)
		STORE.seed(
			"Training Type",
			[
				{"name": "WPS Worker", "active": 1, "retention_years": 2},
				{"name": "Ladder Safety", "active": 1, "retention_years": 2},
			],
		)
		STORE.seed(
			"Employee Training Record",
			[
				{
					"name": "ETR-CV-0001",
					"employee": HARVEST_PICKER,
					"training_type": "WPS Worker",
					"completed_date": "2026-01-10",
					"expires_date": "2028-01-10",
				}
			],
		)

		training = self.detail(HARVEST_PICKER)["compliance"]["training"]
		self.assertEqual(training["cells"]["WPS Worker"]["status"], "current")
		# MISSING IS A STATUS AND IT IS THE POINT — the curriculum with no record
		# at all is the one an inspector finds first, and it cannot be computed
		# from the training register alone.
		self.assertEqual(training["cells"]["Ladder Safety"]["status"], "missing")
		self.assertEqual(training["missing"], ["Ladder Safety"])
		self.assertEqual(self.detail(HARVEST_PICKER)["compliance"]["summary"]["standing"], "non_compliant")

	def test_an_expired_course_puts_the_worker_out_of_standing(self):
		self.as_foreman(HARVEST_LOGIN)
		STORE.seed("Training Type", [{"name": "WPS Worker", "active": 1, "retention_years": 2}])
		STORE.seed(
			"Employee Training Record",
			[
				{
					"name": "ETR-CV-0002",
					"employee": HARVEST_PICKER,
					"training_type": "WPS Worker",
					"completed_date": "2023-01-10",
					"expires_date": "2024-01-10",
				}
			],
		)
		compliance = self.detail(HARVEST_PICKER)["compliance"]
		self.assertEqual(compliance["training"]["expired"], ["WPS Worker"])
		self.assertEqual(compliance["summary"]["standing"], "non_compliant")

	def test_certificates_are_matched_on_the_free_text_holder(self):
		"""`Certification.holder` is a Data column and not a Link, so both
		spellings a person appears under are tried and the match is reported."""
		self.as_foreman(HARVEST_LOGIN)
		STORE.seed(
			"Certification",
			[
				{
					"name": "CERT-CV-0001",
					"cert_name": "Applicator License 2026",
					"cert_type": "Applicator License",
					"status": "Active",
					"holder": "Ana Ramos",
					"company": MAIN,
					"expiration_date": "2027-04-30",
				},
				{
					"name": "CERT-CV-0002",
					"cert_name": "Somebody else's ticket",
					"cert_type": "Organic",
					"status": "Active",
					"holder": "Dee Sorter",
					"company": MAIN,
				},
			],
		)
		data = self.detail(HARVEST_PICKER)
		self.assertEqual(
			[row["certification"] for row in data["compliance"]["certifications"]], ["CERT-CV-0001"]
		)
		self.assertEqual(data["compliance"]["certifications_matched_by"], ["Ana Ramos"])
		self.assertNotIn("CERT-CV-0002", json.dumps(data, default=str))

	def test_an_expired_certificate_is_counted_in_the_summary(self):
		self.as_foreman(HARVEST_LOGIN)
		STORE.seed(
			"Certification",
			[
				{
					"name": "CERT-CV-0003",
					"cert_name": "Applicator License 2024",
					"cert_type": "Applicator License",
					"status": "Expired",
					"holder": HARVEST_PICKER,
					"company": MAIN,
					"expiration_date": "2025-04-30",
				}
			],
		)
		data = self.detail(HARVEST_PICKER)
		self.assertEqual(data["compliance"]["summary"]["certifications_expired"], 1)
		# Matched on the DOCNAME here rather than the display name, which is the
		# other spelling the register is typed with.
		self.assertEqual(data["compliance"]["certifications_matched_by"], [HARVEST_PICKER])

	def test_a_worker_in_another_department_is_refused(self):
		self.as_foreman(HARVEST_LOGIN)
		message = self.tool_error("get_worker_detail", {"employee": PACKHOUSE_HAND})
		self.assertIn("outside this account's scope", message)
		self.assertIn("Nothing was read", message)

	def test_a_manager_may_read_any_department(self):
		self.as_manager()
		self.assertEqual(self.detail(PACKHOUSE_HAND)["department"], PACKHOUSE)

	def test_todays_assignments_include_the_finished_ones(self):
		"""'What has Ana done today' and 'what is Ana doing' are both asked."""
		self.as_foreman(HARVEST_LOGIN)
		STORE.seed(
			"Farm Task Assignment",
			[
				{
					"name": "FTA-CV-0100",
					"task": "TASK-CV-0100",
					"task_name": "Morning pick",
					"assigned_to": HARVEST_PICKER,
					"state": "Completed",
					"company": MAIN,
				},
				{
					"name": "FTA-CV-0101",
					"task": "TASK-CV-0101",
					"task_name": "Afternoon pick",
					"assigned_to": HARVEST_PICKER,
					"state": "In-Progress",
					"company": MAIN,
				},
			],
		)
		states = {
			row["assignment"]: row["state"] for row in self.detail(HARVEST_PICKER)["task_assignments_today"]
		}
		self.assertEqual(states, {"FTA-CV-0100": "Completed", "FTA-CV-0101": "In-Progress"})


class TheStaleShiftIsFenced(CrewViewTestCase):
	"""Six refusals, and the first one is the one that keeps this tool honest."""

	def close(self, shift, **overrides):
		payload = {"shift": shift, "end_datetime": self.hours_ago(1), "reason": "phone died at 14:00"}
		payload.update(overrides)
		return self.tool_error("end_stale_shift", payload)

	def test_a_shift_inside_the_threshold_is_refused_and_end_shift_is_named(self):
		"""THE FENCE. Without it this is a signature-free `end_shift`."""
		self.as_manager()
		self.open_shift("SHIFT-CV-0500", HARVEST_FOREMAN, [HARVEST_PICKER], started_hours_ago=2)
		message = self.close("SHIFT-CV-0500")
		self.assertIn("staleness threshold", message)
		self.assertIn("end_shift", message)
		self.assertIn("Nothing was changed", message)
		self.assertIsNone(self.raw("SHIFT-CV-0500").get("end_datetime"))

	def test_the_threshold_can_be_lowered_deliberately(self):
		self.as_manager()
		self.open_shift("SHIFT-CV-0501", HARVEST_FOREMAN, [HARVEST_PICKER], started_hours_ago=5)
		data = self.tool_data(
			"end_stale_shift",
			{
				"shift": "SHIFT-CV-0501",
				"end_datetime": self.hours_ago(1),
				"reason": "crew went home at noon",
				"stale_after_hours": 4,
			},
		)
		self.assertEqual(data["stale_after_hours"], 4)

	def test_a_threshold_of_zero_is_refused(self):
		self.as_manager()
		self.open_shift("SHIFT-CV-0502", HARVEST_FOREMAN, [HARVEST_PICKER], started_hours_ago=20)
		self.assertIn("every open shift stale", self.close("SHIFT-CV-0502", stale_after_hours=0))

	def test_a_closed_shift_is_refused(self):
		self.as_manager()
		self.open_shift(
			"SHIFT-CV-0503",
			HARVEST_FOREMAN,
			[HARVEST_PICKER],
			started_hours_ago=20,
			end_datetime=self.hours_ago(2),
		)
		self.assertIn("is not open", self.close("SHIFT-CV-0503"))

	def test_the_reason_is_required(self):
		self.as_manager()
		self.open_shift("SHIFT-CV-0504", HARVEST_FOREMAN, [HARVEST_PICKER], started_hours_ago=20)
		message = self.tool_error(
			"end_stale_shift", {"shift": "SHIFT-CV-0504", "end_datetime": self.hours_ago(1)}
		)
		self.assertIn("reason is required", message)
		self.assertIn("STILL OPEN", message)

	def test_the_end_time_is_required_and_never_defaulted(self):
		"""A default would pay a Tuesday crew for Wednesday and Thursday."""
		self.as_manager()
		self.open_shift("SHIFT-CV-0505", HARVEST_FOREMAN, [HARVEST_PICKER], started_hours_ago=20)
		message = self.tool_error("end_stale_shift", {"shift": "SHIFT-CV-0505", "reason": "phone died"})
		self.assertIn("end_datetime is required", message)
		self.assertIn("NOT defaulted to now", message)

	def test_an_end_before_the_start_is_refused(self):
		self.as_manager()
		self.open_shift("SHIFT-CV-0506", HARVEST_FOREMAN, [HARVEST_PICKER], started_hours_ago=20)
		self.assertIn(
			"finished before it began", self.close("SHIFT-CV-0506", end_datetime=self.hours_ago(30))
		)

	def test_an_end_in_the_future_is_refused(self):
		self.as_manager()
		self.open_shift("SHIFT-CV-0507", HARVEST_FOREMAN, [HARVEST_PICKER], started_hours_ago=20)
		self.assertIn(
			"in the future",
			self.close(
				"SHIFT-CV-0507",
				end_datetime=frappe.utils.add_to_date(self.now, hours=4, as_string=True, as_datetime=True),
			),
		)

	def test_a_worker_recorded_as_leaving_later_is_refused(self):
		self.as_manager()
		self.open_shift("SHIFT-CV-0508", HARVEST_FOREMAN, [HARVEST_PICKER], started_hours_ago=20)
		row = self.raw("SHIFT-CV-0508")
		row["crew"][0]["left_at"] = self.hours_ago(0.5)
		STORE.seed(shifts.DOCTYPE, [row])
		self.assertIn(
			"recorded as leaving after", self.close("SHIFT-CV-0508", end_datetime=self.hours_ago(3))
		)


class TheStaleCloseReleasesTheCrew(CrewViewTestCase):
	"""The close writes an ending, a departure per worker, and their pay."""

	def stale(self, name="SHIFT-CV-0600", crew=None):
		return self.open_shift(
			name, HARVEST_FOREMAN, crew or [HARVEST_PICKER, HARVEST_PICKER_TWO], started_hours_ago=20
		)

	def test_the_shift_is_closed_at_the_stated_time(self):
		self.as_manager()
		self.stale()
		end = self.hours_ago(6)
		data = self.tool_data(
			"end_stale_shift", {"shift": "SHIFT-CV-0600", "end_datetime": end, "reason": "phone died"}
		)
		self.assertEqual(data["status"], "Closed")
		self.assertEqual(self.raw("SHIFT-CV-0600")["end_datetime"], end)

	def test_every_worker_still_on_the_shift_is_released_at_that_time(self):
		self.as_manager()
		self.stale()
		end = self.hours_ago(6)
		data = self.tool_data(
			"end_stale_shift", {"shift": "SHIFT-CV-0600", "end_datetime": end, "reason": "phone died"}
		)
		self.assertEqual(data["workers_released_count"], 2)
		self.assertEqual({row["left_at"] for row in self.crew_rows("SHIFT-CV-0600")}, {end})
		# THE ROWS ARE KEPT. Releasing is not deleting.
		self.assertEqual(len(self.crew_rows("SHIFT-CV-0600")), 2)

	def test_a_worker_who_already_left_keeps_their_own_departure_time(self):
		self.as_manager()
		self.stale()
		theirs = self.hours_ago(14)
		row = self.raw("SHIFT-CV-0600")
		row["crew"][0]["left_at"] = theirs
		STORE.seed(shifts.DOCTYPE, [row])

		end = self.hours_ago(6)
		data = self.tool_data(
			"end_stale_shift", {"shift": "SHIFT-CV-0600", "end_datetime": end, "reason": "phone died"}
		)
		self.assertEqual(data["workers_released_count"], 1)
		left = {row["employee"]: row["left_at"] for row in self.crew_rows("SHIFT-CV-0600")}
		self.assertEqual(left[HARVEST_PICKER], theirs)
		self.assertEqual(left[HARVEST_PICKER_TWO], end)

	def test_the_crew_is_paid(self):
		"""A close that wrote no Attendance would be wrong in the employer's
		favour on a day the crew actually worked."""
		self.as_manager()
		self.stale()
		data = self.tool_data(
			"end_stale_shift",
			{"shift": "SHIFT-CV-0600", "end_datetime": self.hours_ago(6), "reason": "phone died"},
		)
		self.assertEqual(data["attendance_created"], 2)

	def test_the_supervisor_review_is_reported_as_owed_when_unsigned(self):
		self.as_manager()
		self.stale()
		data = self.tool_data(
			"end_stale_shift",
			{"shift": "SHIFT-CV-0600", "end_datetime": self.hours_ago(6), "reason": "phone died"},
		)
		self.assertTrue(data["supervisor_review_owed"])
		self.assertFalse(data["supervisor_reviewed"])
		self.assertFalse(self.raw("SHIFT-CV-0600").get("supervisor_review_signature"))
		self.assertIn("NO SUPERVISOR SIGNATURE", data["review_note"])
		self.assertIn("OWED", data["review_note"])

	def test_a_signature_makes_it_an_attestation(self):
		self.as_manager()
		self.stale()
		STORE.seed("File", [{"name": "FILE-CV-0001", "file_url": SIGNATURE}])
		data = self.tool_data(
			"end_stale_shift",
			{
				"shift": "SHIFT-CV-0600",
				"end_datetime": self.hours_ago(6),
				"reason": "supervisor available on Thursday",
				"supervisor_signature_file_token": SIGNATURE,
			},
		)
		self.assertFalse(data["supervisor_review_owed"])
		self.assertTrue(data["supervisor_reviewed"])
		self.assertIn("attestation", data["review_note"])

	def test_the_reason_lands_on_the_record(self):
		"""The sentence is the only account of why somebody who was not there
		ended the shift, so it must survive on the document."""
		self.as_manager()
		self.stale()
		self.tool_data(
			"end_stale_shift",
			{
				"shift": "SHIFT-CV-0600",
				"end_datetime": self.hours_ago(6),
				"reason": "crew clocked out at the packhouse, phone died at 14:00",
			},
		)
		notes = str(self.raw("SHIFT-CV-0600").get("foreman_notes") or "")
		self.assertIn("ADMINISTRATIVE CLOSE", notes)
		self.assertIn("phone died at 14:00", notes)

	def test_the_closed_shift_leaves_the_overview(self):
		"""End to end: the runaway crew stops being reported as out."""
		self.as_manager()
		self.stale()
		self.assertEqual(self.overview()["stale_shifts"], ["SHIFT-CV-0600"])
		self.tool_data(
			"end_stale_shift",
			{"shift": "SHIFT-CV-0600", "end_datetime": self.hours_ago(6), "reason": "phone died"},
		)
		after = self.overview()
		self.assertEqual(after["crew_count"], 0)
		self.assertEqual(after["stale_shifts"], [])


class TheActiveShiftBoard(CrewViewTestCase):
	"""The light read, and the fact that it cannot disagree with the heavy one."""

	def board(self, **arguments):
		return self.tool_data("list_active_shifts", arguments)

	def test_it_carries_the_six_columns_the_board_needs(self):
		self.as_manager()
		self.open_shift(
			"SHIFT-CV-1000", HARVEST_FOREMAN, [HARVEST_PICKER, HARVEST_PICKER_TWO], started_hours_ago=4
		)

		row = self.board()["shifts"][0]
		self.assertEqual(row["shift"], "SHIFT-CV-1000")
		self.assertEqual(row["crew_leader"], HARVEST_FOREMAN)
		self.assertEqual(row["crew_leader_name"], "Ada Orchard")
		self.assertEqual(row["department"], HARVEST)
		self.assertEqual(row["department_name"], "Harvest")
		self.assertIsNotNone(row["start_datetime"])
		self.assertEqual(row["worker_count"], 2)
		self.assertEqual(row["still_on_shift"], 2)
		self.assertEqual(row["location"], "Block 7 North")

	def test_it_does_not_carry_the_crew(self):
		"""THE WHOLE REASON IT IS A SEPARATE TOOL. A board that shipped every
		worker on every crew would be `get_crew_overview` with a different
		shape, and the phone polling it would pay for the drill-down it did not
		ask for."""
		self.as_manager()
		self.open_shift("SHIFT-CV-1001", HARVEST_FOREMAN, [HARVEST_PICKER], started_hours_ago=4)

		row = self.board()["shifts"][0]
		self.assertNotIn("workers", row)
		self.assertNotIn("Ana Ramos", json.dumps(self.board(), default=str))

	def test_a_closed_or_cancelled_shift_is_not_running(self):
		self.as_manager()
		self.open_shift("SHIFT-CV-1002", HARVEST_FOREMAN, [HARVEST_PICKER], end_datetime=self.hours_ago(1))
		self.open_shift("SHIFT-CV-1003", HARVEST_FOREMAN, [HARVEST_PICKER_TWO], cancelled=1)
		self.assertEqual(self.board()["count"], 0)

	def test_the_board_and_the_overview_agree_on_what_is_open(self):
		"""THE AGREEMENT IS THE POINT. Two functions each deriving 'which shifts
		are open' is how a farm gets a board showing four crews and a drill-down
		showing three, so both go through one shared read and this asserts it."""
		self.as_foreman(HARVEST_LOGIN)
		self.open_shift("SHIFT-CV-1010", HARVEST_FOREMAN, [HARVEST_PICKER], started_hours_ago=3)
		self.open_shift("SHIFT-CV-1011", NIGHT_FOREMAN, [NIGHT_PICKER], started_hours_ago=20)
		self.open_shift("SHIFT-CV-1012", PACKHOUSE_FOREMAN, [PACKHOUSE_HAND], started_hours_ago=5)

		board = {row["shift"] for row in self.board()["shifts"]}
		overview = set(self.crews_in(self.overview()))
		self.assertEqual(board, overview)
		# And it is not vacuously equal because both are empty.
		self.assertEqual(board, {"SHIFT-CV-1010", "SHIFT-CV-1011"})

	def test_the_department_scope_applies_to_the_board_too(self):
		self.as_foreman(HARVEST_LOGIN)
		self.open_shift("SHIFT-CV-1020", HARVEST_FOREMAN, [HARVEST_PICKER])
		self.open_shift("SHIFT-CV-1021", PACKHOUSE_FOREMAN, [PACKHOUSE_HAND])

		data = self.board()
		self.assertEqual([row["shift"] for row in data["shifts"]], ["SHIFT-CV-1020"])
		self.assertNotIn("Bo Packhouse", json.dumps(data, default=str))
		self.assertIn("1 open crew(s)", data["withheld_note"])

	def test_an_account_with_no_department_scope_gets_an_empty_board_and_a_reason(self):
		self.acting(UNLINKED_LOGIN, ["Foreman"])
		self.open_shift("SHIFT-CV-1022", HARVEST_FOREMAN, [HARVEST_PICKER])
		data = self.board()
		self.assertEqual(data["shifts"], [])
		self.assertIn("not linked to an Employee record", data["note"])

	def test_the_department_argument_narrows_and_cannot_widen(self):
		"""Naming somebody else's department returns nothing rather than
		reaching into it."""
		self.as_foreman(HARVEST_LOGIN)
		self.open_shift("SHIFT-CV-1030", HARVEST_FOREMAN, [HARVEST_PICKER])
		self.open_shift("SHIFT-CV-1031", NIGHT_FOREMAN, [NIGHT_PICKER])
		self.open_shift("SHIFT-CV-1032", PACKHOUSE_FOREMAN, [PACKHOUSE_HAND])

		self.assertEqual(
			[row["shift"] for row in self.board(department=HARVEST_NIGHT)["shifts"]],
			["SHIFT-CV-1031"],
		)
		# The Packhouse is outside this foreman's scope; naming it explicitly
		# must not reach into it.
		self.assertEqual(self.board(department=PACKHOUSE)["shifts"], [])

	def test_stale_only_is_the_runaway_worklist(self):
		self.as_manager()
		self.open_shift("SHIFT-CV-1040", HARVEST_FOREMAN, [HARVEST_PICKER], started_hours_ago=20)
		self.open_shift("SHIFT-CV-1041", PACKHOUSE_FOREMAN, [PACKHOUSE_HAND], started_hours_ago=2)

		everything = self.board()
		self.assertEqual(everything["count"], 2)
		self.assertEqual(everything["stale_shifts"], ["SHIFT-CV-1040"])
		self.assertIn("end_stale_shift", everything["stale_note"])

		runaways = self.board(stale_only=True)
		self.assertEqual([row["shift"] for row in runaways["shifts"]], ["SHIFT-CV-1040"])
		self.assertTrue(runaways["stale_only"])

	def test_an_empty_stale_list_is_worded_differently_from_an_empty_farm(self):
		self.as_manager()
		self.open_shift("SHIFT-CV-1042", HARVEST_FOREMAN, [HARVEST_PICKER], started_hours_ago=2)
		self.assertIn("Nothing is running away", self.board(stale_only=True)["note"])

	def test_the_threshold_can_be_lowered_and_an_explicit_zero_is_refused(self):
		"""The same zero-drop trap `end_stale_shift` carries, on the read."""
		self.as_manager()
		self.open_shift("SHIFT-CV-1050", HARVEST_FOREMAN, [HARVEST_PICKER], started_hours_ago=5)
		self.assertEqual(self.board()["stale_shifts"], [])
		self.assertEqual(self.board(stale_after_hours=4)["stale_shifts"], ["SHIFT-CV-1050"])
		self.assertEqual(self.board(stale_after_hours=4)["stale_after_hours"], 4)
		# `as_int(...) or DEFAULT` would silently answer 16 here.
		self.assertEqual(self.board(stale_after_hours=0)["stale_after_hours"], 0)

	def test_the_board_closes_after_a_stale_shift_is_ended(self):
		self.as_manager()
		self.open_shift("SHIFT-CV-1060", HARVEST_FOREMAN, [HARVEST_PICKER], started_hours_ago=20)
		self.assertEqual(self.board(stale_only=True)["count"], 1)
		self.tool_data(
			"end_stale_shift",
			{"shift": "SHIFT-CV-1060", "end_datetime": self.hours_ago(6), "reason": "phone died"},
		)
		self.assertEqual(self.board()["count"], 0)


class TheGuards(CrewViewTestCase):
	"""The role gate and the entity scope, on all three."""

	def test_a_picker_may_not_read_the_crew_overview(self):
		self.acting("picker@example.test", ["Field Worker"])
		self.assertIn("may not survey a department", self.tool_error("get_crew_overview", {}))

	def test_a_picker_may_not_read_a_worker_detail(self):
		self.acting("picker@example.test", ["Field Worker"])
		self.assertIn(
			"may not survey a department",
			self.tool_error("get_worker_detail", {"employee": HARVEST_PICKER}),
		)

	def test_a_picker_may_not_close_a_stale_shift(self):
		self.as_manager()
		self.open_shift("SHIFT-CV-0700", HARVEST_FOREMAN, [HARVEST_PICKER], started_hours_ago=20)
		self.acting("picker@example.test", ["Field Worker"])
		self.assertIn(
			"may not survey a department",
			self.tool_error(
				"end_stale_shift",
				{"shift": "SHIFT-CV-0700", "end_datetime": self.hours_ago(1), "reason": "x"},
			),
		)
		self.assertIsNone(self.raw("SHIFT-CV-0700").get("end_datetime"))

	def test_a_crew_leader_is_refused_on_both_transports(self):
		"""THE TWO TRANSPORTS MUST AGREE. The mobile wrappers gate on
		`guard.require_dispatch_role` = {Foreman, Farm Manager}. Gating the tool
		bodies on `SHIFT_ROLES` would have refused a Crew Leader on the handset
		and allowed them over MCP — the exact shape SHIFT_ROLES' own comment says
		it exists to prevent, running backwards. It matters most on
		`end_stale_shift`, which is a write that deliberately skips the FSMA
		§112.161(b) signature."""
		self.assertNotIn("Crew Leader", crew_view.CREW_VIEW_ROLES)
		self.assertIn("Crew Leader", personnel.SHIFT_ROLES)

		self.acting("lead@example.test", ["Crew Leader"])
		for name, arguments in (
			("get_crew_overview", {}),
			("list_active_shifts", {}),
			("get_worker_detail", {"employee": HARVEST_PICKER}),
			("end_stale_shift", {"shift": "SHIFT-CV-0900", "end_datetime": self.now, "reason": "x"}),
		):
			with self.subTest(tool=name):
				self.assertIn("may not survey a department", self.tool_error(name, arguments))

	def test_a_foreman_and_a_farm_manager_both_pass_the_gate(self):
		"""The negative control on the test above: the narrowing must not have
		taken out the two roles the brief is actually about."""
		for login, role in (("f@example.test", "Foreman"), ("m@example.test", "Farm Manager")):
			with self.subTest(role=role):
				self.acting(login, [role])
				self.assertIn("scope", self.tool_data("get_crew_overview", {}))

	def test_the_three_switches_close_the_mcp_door(self):
		"""They gate `registry.dispatch` and NOT the mobile transport — see
		`api/mobile.py`. This asserts the half they do gate."""
		self.as_manager()
		self.configure(
			enabled=1,
			allow_get_crew_overview=0,
			allow_list_active_shifts=0,
			allow_get_worker_detail=0,
			allow_end_stale_shift=0,
		)
		for name, arguments in (
			("get_crew_overview", {}),
			("list_active_shifts", {}),
			("get_worker_detail", {"employee": HARVEST_PICKER}),
			("end_stale_shift", {"shift": "SHIFT-CV-0800", "end_datetime": self.now, "reason": "x"}),
		):
			with self.subTest(tool=name):
				self.assertIn("switched off on this site", self.tool_error(name, arguments))

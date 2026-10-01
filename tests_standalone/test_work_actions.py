# SPDX-License-Identifier: MIT
"""Crew clock-in and assign from the Work screen (v0.212.0, AFB-2026-00030).

docs/design/quick_wins_2026_10.md, Amendment 2.
"""

import frappe

from erpnext_mcp import compliance_fields, tiles
from erpnext_mcp.api import mobile as mobile_api

from .fixtures import MAIN, install_hrms
from .harness import STORE, set_roles
from .test_api_mobile import OUTSIDER_EMPLOYEE, WORKER, WORKER_EMPLOYEE, MobileAPITestCase

CAL = "EMP-CAL"
DEE = "EMP-DEE"
BADGES = "Bucket Log Badge Map"


class WorkActionsCase(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		install_hrms()
		compliance_fields.install_compliance_fields(respect_switch=False)
		STORE.seed(
			"Employee",
			[
				{"name": CAL, "employee_name": "Cal Reyes", "company": MAIN, "status": "Active"},
				{"name": DEE, "employee_name": "Dee Soto", "company": MAIN, "status": "Active"},
			],
		)
		STORE.seed(
			BADGES,
			[{"name": "ETC-0100", "badge_id": "ETC-0100", "company": MAIN, "employee": DEE, "active": 1}],
		)
		STORE.commit()

	def foreman(self):
		set_roles(WORKER, ["Field Worker", "Foreman"])
		return self.be()


class ClockInCrew(WorkActionsCase):
	def test_it_starts_a_shift_and_clocks_everybody_in_by_name_or_badge(self):
		self.foreman()
		answer = mobile_api.clock_in_crew(
			employees=[CAL], badge_ids=["ETC-0100"], location="Block 7 North", client_request_id="req-1"
		)
		self.assertTrue(answer["started"])
		self.assertEqual((answer["added"], answer["already"], answer["refused"]), (2, 0, 0))
		self.assertEqual({r["employee"] for r in answer["results"]}, {CAL, DEE})
		self.assertEqual(next(r for r in answer["results"] if r["employee"] == DEE)["badge_id"], "ETC-0100")
		shift = answer["shift"]
		self.assertEqual(STORE.get_raw("Farm Shift", shift)["foreman"], WORKER_EMPLOYEE)

		# The same request again — a lost reply, a drained queue — starts nothing new.
		again = mobile_api.clock_in_crew(
			employees=[CAL], badge_ids=["ETC-0100"], location="Block 7 North", client_request_id="req-1"
		)
		self.assertEqual((again["shift"], again["started"]), (shift, False))
		self.assertEqual((again["added"], again["already"]), (0, 2))
		self.assertEqual(len(STORE.rows("Farm Shift")), 1)

	def test_it_joins_the_shift_the_caller_already_has_open(self):
		self.foreman()
		shift = mobile_api.start_shift(location="Block 2", shift_type="Harvest", company=MAIN)["name"]
		answer = mobile_api.clock_in_crew(employees=[CAL])
		self.assertEqual((answer["shift"], answer["started"], answer["added"]), (shift, False, 1))
		self.assertEqual(mobile_api.clock_in_crew(employees=[DEE], shift=shift)["added"], 1)

	def test_one_refusal_does_not_stop_the_rest(self):
		self.foreman()
		answer = mobile_api.clock_in_crew(
			employees=[CAL, OUTSIDER_EMPLOYEE], badge_ids=["NOT-A-BADGE"], location="Block 7"
		)
		self.assertEqual((answer["added"], answer["refused"]), (1, 2))
		refused = [r for r in answer["results"] if r["outcome"] == "refused"]
		self.assertTrue(all(r["reason"] for r in refused))

	def test_no_shift_and_no_location_is_refused_and_so_is_nobody(self):
		self.foreman()
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.clock_in_crew(employees=[CAL])
		self.assertIn("needs a location", str(caught.exception))
		with self.assertRaises(frappe.ValidationError):
			mobile_api.clock_in_crew(location="Block 7")
		self.assertEqual(STORE.rows("Farm Shift"), [])

	def test_a_picker_may_not_clock_anybody_in_or_read_the_list(self):
		self.be()
		for call in (
			lambda: mobile_api.clock_in_crew(employees=[CAL], location="Block 7"),
			lambda: mobile_api.list_crew_candidates(),
		):
			with self.assertRaises(frappe.ValidationError) as caught:
				call()
			self.assertIn("Foreman", str(caught.exception))
		self.assertEqual(STORE.rows("Farm Shift"), [])

	def test_the_candidate_list_is_the_callers_entity_and_says_who_is_on_a_shift(self):
		self.foreman()
		shift = mobile_api.clock_in_crew(employees=[CAL], location="Block 7")["shift"]
		people = {p["employee"]: p for p in mobile_api.list_crew_candidates(shift=shift)["people"]}
		self.assertNotIn(OUTSIDER_EMPLOYEE, people, "another entity's people are not offered")
		self.assertEqual((people[CAL]["on_shift"], people[CAL]["on_this_shift"]), (shift, True))
		self.assertIsNone(people[DEE]["on_shift"])
		self.assertEqual(
			set(people[DEE]),
			{"employee", "employee_name", "designation", "company", "on_shift", "on_this_shift"},
		)
		found = mobile_api.list_crew_candidates(search="dee")["people"]
		self.assertEqual([p["employee"] for p in found], [DEE])


class AssignFromWork(WorkActionsCase):
	def a_task(self, **overrides):
		self.be("Administrator")
		name = super().a_task(**overrides)
		STORE.commit()
		return name

	def test_the_list_marks_who_holds_the_certification_and_assign_agrees(self):
		task = self.a_task()
		frappe.db.set_value("Farm Task", task, "required_certification", "Applicator License")
		STORE.seed(
			"Certification",
			[
				{
					"name": "Cal — applicator",
					"cert_name": "Cal — applicator",
					"cert_type": "Applicator License",
					"holder": CAL,
					"company": MAIN,
					"status": "Active",
					"expiration_date": str(frappe.utils.add_days(frappe.utils.today(), 200)),
				}
			],
		)
		STORE.commit()
		self.foreman()
		answer = mobile_api.list_assignable_workers(task=task)
		self.assertEqual(answer["requirements"], ["Applicator License"])
		people = {p["employee"]: p for p in answer["people"]}
		self.assertTrue(people[CAL]["qualified"])
		self.assertEqual((people[DEE]["qualified"], people[DEE]["missing"]), (False, ["Applicator License"]))
		self.assertNotIn(OUTSIDER_EMPLOYEE, people)
		# The refusal is the same check, with no way round it.
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.assign_farm_task(task=task, assigned_to=DEE)
		self.assertIn("Applicator License", str(caught.exception))
		done = mobile_api.assign_farm_task(task=task, assigned_to=CAL, client_request_id="a-1")
		self.assertEqual((done["assigned_to"], done["already"]), (CAL, False))

	def test_assigning_twice_is_an_answer_not_a_refusal(self):
		task = self.a_task()
		self.foreman()
		mobile_api.assign_farm_task(task=task, assigned_to=CAL, client_request_id="a-2")
		again = mobile_api.assign_farm_task(task=task, assigned_to=CAL, client_request_id="a-2")
		self.assertTrue(again["already"])
		self.assertEqual(again["assigned_to"], CAL)
		# Somebody ELSE holding it is still a refusal without `reassign`.
		with self.assertRaises(frappe.ValidationError):
			mobile_api.assign_farm_task(task=task, assigned_to=DEE)

	def test_a_task_with_no_requirement_offers_everybody_crew_first(self):
		task = self.a_task()
		self.foreman()
		mobile_api.clock_in_crew(employees=[DEE], location="Block 7")
		people = mobile_api.list_assignable_workers(task=task)["people"]
		self.assertTrue(all(p["qualified"] for p in people))
		self.assertEqual(people[0]["employee"] in (DEE, WORKER_EMPLOYEE), True)
		self.assertTrue(next(p for p in people if p["employee"] == DEE)["on_crew"])

	def test_a_picker_is_refused_the_list(self):
		task = self.a_task()
		self.be()
		with self.assertRaises(frappe.PermissionError):
			mobile_api.list_assignable_workers(task=task)


class TheWorkTiles(WorkActionsCase):
	def test_two_tiles_are_seeded_on_work_for_the_roles_the_routes_allow(self):
		self.assertIn("crew_clock_in", tiles.REPORTS)
		self.assertIn("assign_tasks", tiles.REPORTS)
		self.assertEqual({body["surface"] for body in tiles.WORK_TILES.values()}, {"work"})
		for key, body in tiles.WORK_TILES.items():
			# Every part of the body is valid; whether anybody holds the roles yet is
			# a fact about a site, checked at publish, not about the tile.
			errors = [e for e in tiles.validate(body)["errors"] if "resolves to nobody" not in e]
			self.assertEqual(errors, [], key)
			self.assertEqual(body["min_app_version"], "0.24.0")
		self.assertEqual(tiles.WORK_TILES["assign_tasks"]["audience"]["roles"], ["Foreman", "Farm Manager"])
		self.be("Administrator")
		made = tiles.seed()
		self.assertIn("tile:crew_clock_in@1", made)
		self.assertIn("tile:assign_tasks@1", made)
		STORE.commit()
		self.foreman()
		served = mobile_api.get_tiles(surface="work", app_version="0.24.0")["tiles"]
		self.assertEqual([t["key"] for t in served], ["crew_clock_in", "assign_tasks"])
		self.assertEqual(mobile_api.get_tiles(surface="work", app_version="0.23.0")["tiles"], [])
		set_roles(WORKER, ["Field Worker"])
		self.be()
		self.assertEqual(mobile_api.get_tiles(surface="work", app_version="0.24.0")["tiles"], [])

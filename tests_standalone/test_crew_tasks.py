# SPDX-License-Identifier: MIT
"""Crew tasks: many people on one Farm Task (v0.213.0).

docs/design/crew_tasks.md.
"""

import datetime

import frappe

from erpnext_mcp import compliance_fields, crew_tasks, registry, tiles
from erpnext_mcp.api import mobile as mobile_api

from .fixtures import MAIN, install_hrms
from .harness import STORE, set_roles
from .test_api_mobile import OUTSIDER_EMPLOYEE, WORKER, WORKER_EMPLOYEE, MobileAPITestCase

CAL = "EMP-CAL"
DEE = "EMP-DEE"
EVA = "EMP-EVA"
CAL_USER = "cal@example.test"
BADGES = "Bucket Log Badge Map"
ASSIGNMENT = "Farm Task Assignment"
SECTIONS = [{"from_row": 1, "to_row": 20}, {"from_row": 21, "to_row": 40}]
CREW_SWITCHES = {
	"allow_add_to_crew_task": 1,
	"allow_remove_from_crew_task": 1,
	"allow_update_crew_task_member": 1,
	"allow_update_crew_task_sections": 1,
}


class CrewTaskCase(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		install_hrms()
		compliance_fields.install_compliance_fields(respect_switch=False)
		STORE.seed(
			"Employee",
			[
				{
					"name": CAL,
					"employee_name": "Cal Reyes",
					"company": MAIN,
					"status": "Active",
					"user_id": CAL_USER,
				},
				{"name": DEE, "employee_name": "Dee Soto", "company": MAIN, "status": "Active"},
				{"name": EVA, "employee_name": "Eva Lund", "company": MAIN, "status": "Active"},
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

	def picker(self):
		set_roles(WORKER, ["Field Worker"])
		return self.be()

	def a_crew_task(self, **overrides):
		self.be("Administrator")
		payload = {
			"task_name": "Prune Block 4",
			"task_type": "Other",
			"work_mode": "Crew",
			"piece_unit": "trees",
			"evidence_required": {"findings_text": True},
		}
		payload.update(overrides)
		name = self.a_task(**payload)
		STORE.commit()
		return name

	def an_individual_task(self):
		self.be("Administrator")
		name = self.a_task(evidence_required={"findings_text": True})
		STORE.commit()
		return name

	def rows(self, task, state=None):
		return [
			row
			for row in STORE.rows(ASSIGNMENT)
			if row["task"] == task and (state is None or row["state"] == state)
		]


class TheModel(CrewTaskCase):
	def test_a_crew_task_says_so_and_an_individual_task_is_what_it_was(self):
		crew = self.a_crew_task(sections=SECTIONS)
		row = STORE.get_raw("Farm Task", crew)
		self.assertEqual(
			(row["work_mode"], row["is_crew_task"], row["dispatch_mode"]), ("Crew", 1, "Dispatched")
		)
		described = self.tool_data("get_farm_task", {"task": crew})
		self.assertTrue(described["is_crew_task"])
		self.assertEqual(described["crew"]["piece_unit"], "trees")
		self.assertEqual([s["label"] for s in described["crew"]["sections"]], ["Rows 1–20", "Rows 21–40"])
		self.assertEqual((described["crew"]["on_now"], described["crew"]["members"]), (0, []))

		plain = self.an_individual_task()
		self.assertEqual(STORE.get_raw("Farm Task", plain)["work_mode"], "Individual")
		described = self.tool_data("get_farm_task", {"task": plain})
		self.assertNotIn("crew", described)
		self.assertNotIn("is_crew_task", described)
		self.foreman()
		shaped = mobile_api.get_task(task=plain)
		self.assertEqual((shaped["is_crew_task"], shaped["work_mode"]), (False, "Individual"))
		self.assertNotIn("crew", shaped)

	def test_is_crew_task_true_is_the_same_as_work_mode_crew(self):
		name = self.a_crew_task(work_mode=None, is_crew_task=True)
		self.assertEqual(STORE.get_raw("Farm Task", name)["work_mode"], "Crew")
		self.assertIn(
			"work_mode is Individual or Crew",
			self.tool_error(
				"create_farm_task",
				{
					"task_name": "x",
					"task_type": "Other",
					"evidence_required": {"findings_text": True},
					"work_mode": "Gang",
				},
			),
		)

	def test_the_two_crew_states_are_not_live_states(self):
		from erpnext_mcp.erpnext_mcp.doctype.farm_task_assignment import farm_task_assignment as controller

		self.assertNotIn(crew_tasks.ON_CREW, controller.LIVE_STATES)
		self.assertIn(crew_tasks.ON_CREW, controller.STATES)
		self.assertIn(crew_tasks.OFF_CREW, controller.STATES)


class AddingPeople(CrewTaskCase):
	def test_named_people_and_a_badge_go_on_and_the_caller_leads(self):
		task = self.a_crew_task()
		self.foreman()
		answer = mobile_api.add_to_crew_task(
			task=task, employees=[CAL], badge_ids=["ETC-0100"], client_request_id="c-1"
		)
		self.assertEqual((answer["added"], answer["already"], answer["refused"]), (2, 0, 0))
		self.assertEqual((answer["lead"], answer["started"]), (WORKER_EMPLOYEE, True))
		self.assertEqual(next(r for r in answer["results"] if r["employee"] == DEE)["badge_id"], "ETC-0100")
		self.assertEqual(STORE.get_raw("Farm Task", task)["state"], "In-Progress")
		on = self.rows(task, crew_tasks.ON_CREW)
		self.assertEqual({r["assigned_to"] for r in on}, {CAL, DEE})
		self.assertTrue(all(r["started_at"] and r["client_request_id"] == "c-1" for r in on))
		self.assertEqual(len(self.rows(task, "In-Progress")), 1, "the lead holds it; the crew work it")
		self.assertEqual(answer["crew"]["on_now"], 2)
		self.assertEqual(STORE.get_raw("Farm Task", task)["crew_on_now"], 2)

		# The same request again — a drained queue — adds nobody twice.
		again = mobile_api.add_to_crew_task(
			task=task, employees=[CAL], badge_ids=["ETC-0100"], client_request_id="c-1"
		)
		self.assertEqual((again["added"], again["already"], again["started"]), (0, 2, False))
		self.assertEqual(len(self.rows(task, crew_tasks.ON_CREW)), 2)

	def test_a_whole_shift_goes_on_in_one_call_and_each_row_names_it(self):
		task = self.a_crew_task()
		self.foreman()
		shift = mobile_api.clock_in_crew(employees=[CAL, DEE], location="Block 4")["shift"]
		answer = mobile_api.add_to_crew_task(task=task, shift=shift)
		self.assertEqual(answer["added"], 2)
		self.assertEqual({r["farm_shift"] for r in self.rows(task, crew_tasks.ON_CREW)}, {shift})
		self.assertTrue(all("warning" not in r for r in answer["results"]))

	def test_somebody_on_no_shift_is_added_and_told_so(self):
		task = self.a_crew_task()
		self.foreman()
		line = mobile_api.add_to_crew_task(task=task, employees=[CAL])["results"][0]
		self.assertEqual((line["outcome"], line["farm_shift"]), ("added", None))
		self.assertIn("not clocked into a shift", line["warning"])

	def test_the_certification_is_checked_per_person_and_one_refusal_stops_nobody(self):
		task = self.a_crew_task()
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
				},
				{
					"name": "Ana — applicator",
					"cert_name": "Ana — applicator",
					"cert_type": "Applicator License",
					"holder": WORKER_EMPLOYEE,
					"company": MAIN,
					"status": "Active",
					"expiration_date": str(frappe.utils.add_days(frappe.utils.today(), 200)),
				},
			],
		)
		STORE.commit()
		self.foreman()
		answer = mobile_api.add_to_crew_task(task=task, employees=[CAL, DEE, OUTSIDER_EMPLOYEE, "EMP-NOBODY"])
		self.assertEqual((answer["added"], answer["refused"]), (1, 3))
		refused = {r["employee"]: r["reason"] for r in answer["results"] if r["outcome"] == "refused"}
		self.assertIn("Applicator License", refused[DEE])
		self.assertEqual({r["assigned_to"] for r in self.rows(task, crew_tasks.ON_CREW)}, {CAL})
		people = {p["employee"]: p for p in mobile_api.list_assignable_workers(task=task)["people"]}
		self.assertEqual((people[CAL]["on_this_task"], people[DEE]["on_this_task"]), (True, False))
		self.assertFalse(people[DEE]["qualified"])

	def test_a_person_is_on_one_crew_task_at_a_time(self):
		first, second = self.a_crew_task(), self.a_crew_task(task_name="Prune Block 5")
		self.foreman()
		mobile_api.add_to_crew_task(task=first, employees=[CAL])
		line = mobile_api.add_to_crew_task(task=second, employees=[CAL])["results"][0]
		self.assertEqual((line["outcome"], line["moved_from"]), ("added", first))
		closed = self.rows(first, crew_tasks.OFF_CREW)[0]
		self.assertEqual(closed["end_reason"], f"Moved to {second}")
		self.assertTrue(closed["completed_at"])
		# And the foreman leads both without either being paused.
		self.assertEqual({STORE.get_raw("Farm Task", t)["state"] for t in (first, second)}, {"In-Progress"})

	def test_an_individual_task_and_a_finished_one_are_refused(self):
		plain = self.an_individual_task()
		self.foreman()
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.add_to_crew_task(task=plain, employees=[CAL])
		self.assertIn("individual task", str(caught.exception))
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.add_to_crew_task(task=self.a_crew_task() and plain)
		task = self.a_crew_task()
		self.foreman()
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.add_to_crew_task(task=task)
		self.assertIn("who is going on the crew", str(caught.exception))

	def test_assign_on_a_crew_task_adds_and_takes_nobody_off(self):
		task = self.a_crew_task()
		self.foreman()
		first = mobile_api.assign_farm_task(task=task, assigned_to=CAL, client_request_id="a-1")
		self.assertEqual((first["already"], first["is_crew_task"]), (False, True))
		second = mobile_api.assign_farm_task(task=task, assigned_to=DEE)
		self.assertEqual(second["crew"]["on_now"], 2)
		self.assertTrue(mobile_api.assign_farm_task(task=task, assigned_to=CAL)["already"])
		self.assertEqual(STORE.get_raw("Farm Task", task)["assigned_to"], WORKER_EMPLOYEE)

	def test_nobody_claims_a_crew_task_from_the_pool(self):
		task = self.a_crew_task()
		self.picker()
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.claim_task(task=task)
		self.assertIn("crew task", str(caught.exception))


class RemovingPeople(CrewTaskCase):
	def test_the_row_is_closed_with_its_time_and_reason_and_kept(self):
		task = self.a_crew_task()
		self.foreman()
		mobile_api.add_to_crew_task(task=task, employees=[CAL, DEE])
		row = self.rows(task, crew_tasks.ON_CREW)[0]
		earlier = str(frappe.utils.get_datetime(str(row["started_at"])) - datetime.timedelta(hours=2))
		frappe.db.set_value(ASSIGNMENT, row["name"], "started_at", earlier)
		frappe.db.set_value(ASSIGNMENT, row["name"], "claimed_at", earlier)
		STORE.commit()
		answer = mobile_api.remove_from_crew_task(
			task=task, employees=[row["assigned_to"]], reason="Went home sick"
		)
		line = answer["results"][0]
		self.assertEqual(line["outcome"], "removed")
		self.assertGreaterEqual(line["minutes"], 119)
		closed = STORE.get_raw(ASSIGNMENT, row["name"])
		self.assertEqual(
			(closed["state"], closed["end_reason"], closed["ended_by"]),
			("Off Crew", "Went home sick", WORKER),
		)
		self.assertEqual(closed["actual_duration_minutes"], line["minutes"])
		self.assertEqual(answer["crew"]["on_now"], 1)
		self.assertEqual(answer["crew"]["headcount"], 2)
		self.assertGreaterEqual(STORE.get_raw("Farm Task", task)["crew_person_minutes"], 119)

		# Again is an answer; coming back is a new row.
		again = mobile_api.remove_from_crew_task(task=task, employees=[row["assigned_to"]])
		self.assertEqual(again["results"][0]["outcome"], "already")
		mobile_api.add_to_crew_task(task=task, employees=[row["assigned_to"]])
		theirs = [r for r in self.rows(task) if r["assigned_to"] == row["assigned_to"]]
		self.assertEqual(sorted(r["state"] for r in theirs), ["Off Crew", "On Crew"])

	def test_all_takes_everybody_off(self):
		task = self.a_crew_task()
		self.foreman()
		mobile_api.add_to_crew_task(task=task, employees=[CAL, DEE, EVA])
		answer = mobile_api.remove_from_crew_task(task=task, all=True, reason="Rain")
		self.assertEqual((answer["removed"], answer["crew"]["on_now"]), (3, 0))

	def test_leaving_the_shift_ends_their_time_on_the_task(self):
		task = self.a_crew_task()
		self.foreman()
		shift = mobile_api.clock_in_crew(employees=[CAL, DEE], location="Block 4")["shift"]
		mobile_api.add_to_crew_task(task=task, shift=shift)
		mobile_api.clock_out_worker(shift=shift, employee=CAL)
		gone = [r for r in self.rows(task, crew_tasks.OFF_CREW)]
		self.assertEqual([(r["assigned_to"], r["end_reason"]) for r in gone], [(CAL, "Left the shift")])
		self.assertEqual(len(self.rows(task, crew_tasks.ON_CREW)), 1)


class WhoMay(CrewTaskCase):
	def test_a_picker_may_not_add_remove_set_sections_or_read_the_board(self):
		task = self.a_crew_task(sections=SECTIONS)
		self.picker()
		for call in (
			lambda: mobile_api.add_to_crew_task(task=task, employees=[CAL]),
			lambda: mobile_api.remove_from_crew_task(task=task, all=True),
			lambda: mobile_api.update_crew_task_sections(task=task, section="rows_1_20"),
			lambda: mobile_api.list_crew_tasks(),
			lambda: mobile_api.list_crew_task_members(task=task),
			lambda: mobile_api.assign_farm_task(task=task, assigned_to=CAL),
		):
			with self.assertRaises((frappe.ValidationError, frappe.PermissionError)):
				call()
		self.assertEqual(self.rows(task), [])

	def test_a_crew_leader_may(self):
		task = self.a_crew_task()
		set_roles(WORKER, ["Field Worker", "Crew Leader"])
		self.be()
		self.assertEqual(mobile_api.add_to_crew_task(task=task, employees=[CAL])["added"], 1)
		self.assertEqual(mobile_api.list_crew_tasks()["count"], 1)
		# The certification list is theirs too on a crew task — and only there.
		people = mobile_api.list_assignable_workers(task=task)
		self.assertTrue(people["is_crew_task"])
		self.assertEqual(mobile_api.get_task(task=task)["my_crew_role"], "lead")
		plain = self.an_individual_task()
		set_roles(WORKER, ["Field Worker", "Crew Leader"])
		self.be()
		with self.assertRaises(frappe.PermissionError):
			mobile_api.list_assignable_workers(task=plain)

	def test_a_member_sees_the_task_reads_the_crew_and_changes_only_their_own_row(self):
		task = self.a_crew_task()
		self.foreman()
		mobile_api.add_to_crew_task(task=task, employees=[WORKER_EMPLOYEE, CAL], lead=EVA)
		self.picker()
		mine = [t for t in mobile_api.list_my_tasks()["tasks"] if t["name"] == task]
		self.assertEqual(len(mine), 1)
		self.assertEqual((mine[0]["my_crew_role"], mine[0]["assignment"]), ("member", None))
		self.assertEqual(mobile_api.list_crew_task_members(task=task)["on_now"], 2)
		self.assertEqual(mobile_api.get_task(task=task)["my_crew_role"], "member")

		mobile_api.update_crew_task_member(task=task, pieces=12, notes="South rows")
		mobile_api.update_crew_task_member(task=task, add_pieces=8)
		STORE.commit()
		with self.assertRaises(frappe.ValidationError):
			mobile_api.update_crew_task_member(task=task, employee=CAL, pieces=99)
		with self.assertRaises(frappe.ValidationError):
			mobile_api.complete_task_via_mobile(task=task, findings_text="done")

		done = mobile_api.update_crew_task_member(task=task, part_done=True)
		member = done["member"]
		self.assertEqual(
			(member["pieces"], member["piece_unit"], member["notes"]), (20.0, "trees", "South rows")
		)
		self.assertEqual(
			(member["part_done"], member["on_now"], member["end_reason"]), (True, False, "Part done")
		)
		self.assertEqual(done["crew"]["pieces"], 20.0)
		self.assertEqual(STORE.get_raw("Farm Task", task)["state"], "In-Progress", "the task stays open")
		self.assertEqual(
			[t for t in mobile_api.list_my_tasks()["tasks"] if t["name"] == task], [], "they are off it"
		)


class Closing(CrewTaskCase):
	def test_the_lead_closes_it_once_and_the_crews_time_ends_with_it(self):
		task = self.a_crew_task()
		self.foreman()
		mobile_api.add_to_crew_task(task=task, employees=[CAL, DEE])
		mine = next(t for t in mobile_api.list_my_tasks()["tasks"] if t["name"] == task)
		self.assertEqual(mine["my_crew_role"], "lead")
		mobile_api.complete_task_via_mobile(task=task, findings_text="Block 4 pruned")
		self.assertEqual(STORE.get_raw("Farm Task", task)["state"], "Completed")
		closed = self.rows(task, crew_tasks.OFF_CREW)
		self.assertEqual(
			{(r["assigned_to"], r["end_reason"]) for r in closed},
			{(CAL, "Task completed"), (DEE, "Task completed")},
		)
		self.assertEqual(self.rows(task, crew_tasks.ON_CREW), [])
		self.assertEqual(len(self.rows(task, "Completed")), 1, "one completion, the lead's")
		self.assertEqual(STORE.get_raw("Farm Task", task)["crew_headcount"], 2)
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.add_to_crew_task(task=task, employees=[EVA])
		self.assertIn("finished", str(caught.exception))

	def test_a_supervisor_who_is_not_the_lead_takes_it_to_close(self):
		task = self.a_crew_task()
		self.foreman()
		mobile_api.add_to_crew_task(task=task, employees=[DEE], lead=EVA)
		self.assertEqual(STORE.get_raw("Farm Task", task)["assigned_to"], EVA)
		mobile_api.complete_task_via_mobile(task=task, findings_text="Closed by the foreman")
		row = STORE.get_raw("Farm Task", task)
		self.assertEqual((row["state"], row["assigned_to"]), ("Completed", WORKER_EMPLOYEE))
		passed = [r for r in self.rows(task, "Rejected") if r["assigned_to"] == EVA]
		self.assertIn("Took the lead to close the task", passed[0]["rejection_reason"])

	def test_handing_the_task_back_takes_the_crew_off(self):
		task = self.a_crew_task()
		self.foreman()
		mobile_api.add_to_crew_task(task=task, employees=[CAL])
		mobile_api.reject_task(task=task, reason="Too wet to prune")
		self.assertEqual(self.rows(task, crew_tasks.OFF_CREW)[0]["end_reason"], "Task handed back")

	def test_a_crew_row_has_what_the_block_cost_split_reads(self):
		task = self.a_crew_task()
		self.foreman()
		mobile_api.add_to_crew_task(task=task, employees=[CAL])
		mobile_api.remove_from_crew_task(task=task, employees=[CAL])
		row = self.rows(task, crew_tasks.OFF_CREW)[0]
		for column in ("assigned_to", "started_at", "completed_at", "task"):
			self.assertTrue(row[column], column)
		self.assertIn("actual_duration_minutes", row)
		self.assertNotIn("piece_units", row, "crew pieces are not a column the payroll run reads")


class Sections(CrewTaskCase):
	def test_ticking_a_section_reports_progress_in_words(self):
		task = self.a_crew_task(sections=SECTIONS)
		self.foreman()
		answer = mobile_api.update_crew_task_sections(task=task, section="rows_1_20")
		self.assertEqual((answer["progress"]["done"], answer["progress"]["total"]), (1, 2))
		self.assertEqual(answer["progress"]["summary"], "Rows 1–20 done; Rows 21–40 open")
		self.assertEqual(answer["sections"][0]["done_by"], WORKER)
		# Replacing the list keeps what was already done.
		kept = mobile_api.update_crew_task_sections(task=task, sections=[*SECTIONS, {"label": "Headland"}])
		self.assertEqual([s["done"] for s in kept["sections"]], [True, False, False])
		self.assertEqual(
			mobile_api.update_crew_task_sections(task=task, section="rows_1_20", done=False)["progress"][
				"done"
			],
			0,
		)
		with self.assertRaises(frappe.ValidationError):
			mobile_api.update_crew_task_sections(task=task, section="nope")
		with self.assertRaises(frappe.ValidationError):
			mobile_api.update_crew_task_sections(task=task, sections=[{"from_row": 9, "to_row": 3}])

	def test_people_can_be_put_on_a_section(self):
		task = self.a_crew_task(sections=SECTIONS)
		self.foreman()
		mobile_api.add_to_crew_task(task=task, employees=[CAL], section="rows_21_40")
		self.assertEqual(mobile_api.list_crew_task_members(task=task)["members"][0]["section"], "rows_21_40")


class Templates(CrewTaskCase):
	def test_a_crew_template_raises_a_crew_task(self):
		self.be("Administrator")
		self.configure(enabled=1, allow_create_farm_task_template=1, allow_create_task_from_template=1)
		template = self.tool_data(
			"create_farm_task_template",
			{
				"template_name": "Winter pruning",
				"task_type": "Other",
				"evidence_required": {"findings_text": True},
				"work_mode": "Crew",
				"piece_unit": "trees",
				"sections": ["North half", "South half"],
			},
		)
		self.assertTrue(template["is_crew_task"])
		task = self.tool_data("create_task_from_template", {"template": template["name"], "company": MAIN})
		self.assertTrue(task["is_crew_task"])
		self.assertEqual(task["crew"]["piece_unit"], "trees")
		self.assertEqual([s["label"] for s in task["crew"]["sections"]], ["North half", "South half"])


class TheSurface(CrewTaskCase):
	def test_the_four_writes_are_off_by_default_and_the_read_is_on(self):
		for name in (
			"add_to_crew_task",
			"remove_from_crew_task",
			"update_crew_task_member",
			"update_crew_task_sections",
		):
			self.assertIn(name, registry.MUTATING_TOOLS)
		self.assertIn("list_crew_task_members", registry.READ_TOOLS)
		task = self.a_crew_task()
		self.assertIn(
			"allow_add_to_crew_task", self.tool_error("add_to_crew_task", {"task": task, "employees": [CAL]})
		)

	def test_the_mcp_path_runs_the_same_tool_and_needs_a_lead(self):
		task = self.a_crew_task()
		self.be("Administrator")
		self.configure(enabled=1, **CREW_SWITCHES)
		self.assertIn(
			"has no lead yet", self.tool_error("add_to_crew_task", {"task": task, "employees": [CAL]})
		)
		added = self.tool_data("add_to_crew_task", {"task": task, "employees": [CAL, DEE], "lead": EVA})
		self.assertEqual((added["added"], added["lead"]), (2, EVA))
		listed = self.tool_data("list_crew_task_members", {"task": task})
		self.assertEqual((listed["on_now"], listed["lead"]), (2, EVA))
		self.tool_data("update_crew_task_member", {"task": task, "employee": CAL, "pieces": 30})
		removed = self.tool_data("remove_from_crew_task", {"task": task, "employees": [CAL]})
		self.assertEqual((removed["removed"], removed["crew"]["pieces"]), (1, 30.0))

	def test_the_board_lists_open_crew_tasks_only(self):
		crew, _plain = self.a_crew_task(), self.an_individual_task()
		self.foreman()
		board = mobile_api.list_crew_tasks()
		self.assertEqual([t["name"] for t in board["tasks"]], [crew])
		self.assertTrue(board["tasks"][0]["is_crew_task"])

	def test_the_tile_is_seeded_on_work_for_the_supervising_roles(self):
		self.assertIn("crew_tasks", tiles.REPORTS)
		body = tiles.CREW_TILES["crew_tasks"]
		errors = [e for e in tiles.validate(body)["errors"] if "resolves to nobody" not in e]
		self.assertEqual(errors, [])
		self.assertEqual((body["surface"], body["min_app_version"]), ("work", "0.25.0"))
		self.be("Administrator")
		self.assertIn("tile:crew_tasks@1", tiles.seed())
		STORE.commit()
		self.foreman()
		keys = [t["key"] for t in mobile_api.get_tiles(surface="work", app_version="0.25.0")["tiles"]]
		self.assertIn("crew_tasks", keys)
		self.assertNotIn(
			"crew_tasks",
			[t["key"] for t in mobile_api.get_tiles(surface="work", app_version="0.24.0")["tiles"]],
		)

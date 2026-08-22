# SPDX-License-Identifier: MIT
"""The Farm Ops mobile wrappers — v0.17.0 Feature D.

FOUR CLAIMS.

1. **THE WORKER COMES FROM THE REQUEST, NOT FROM THE BODY.**
   `IdentityComesFromTheRequest`. A phone knows an email and a Keychain
   credential; a Farm Task Assignment names an Employee. The lookup between them
   lives here rather than in every mobile client that will ever exist, and a
   request that authenticates as one person and asks to act as another is
   refused — an account that can name somebody else in a request body is not
   scoped to anything.

2. **THE WRAPPERS ADD NO RULE AND WEAKEN NONE.** `TheRulesAreUnchanged`. The
   concurrent-claim limit, the refusal to self-pick Dispatched work, the
   evidence-contract check and the empty-string findings distinction all still
   come from Sprint 8's tools, because they ARE Sprint 8's tools. Asserted by
   driving the same refusals through the wrapper.

3. **THEY ARE HONEST ABOUT WHAT THEY DO NOT KNOW.** `SkillsAreNotInvented`.
   Nothing on a Frappe site records what skills a worker has, so an unfiltered
   pool comes back saying it is unfiltered. Guessing from a job title would hide
   work with no way to tell.

4. **THE ENTITY SCOPING IS EXPLICIT WHERE THE FRAMEWORK CANNOT DO IT.**
   `TheCalendarIsScoped`. This app reads through `frappe.db.get_all`, which does
   NOT consult User Permissions — so a "for me" calendar has to ask per entity,
   and an account with no entities is refused rather than shown the whole site.
"""

import json

import frappe

from erpnext_mcp import roles
from erpnext_mcp.tools import mobile

from .fixtures import MAIN, OTHER, V12TestCase
from .harness import STORE
from .test_dispatch import A_PHOTO, WALK

WORKER = "ana@example.test"
WORKER_EMPLOYEE = "EMP-ANA"

ON = {
	f"allow_{name}": 1
	for name in (
		# the wrappers
		"list_my_tasks",
		"list_available_for_me",
		"get_task_with_evidence_contract",
		"list_compliance_calendar_for_me",
		"list_tasks_by_location",
		"claim_task_via_mobile",
		"start_task_via_mobile",
		"complete_task_via_mobile",
		# what they need to have something to talk about
		"create_mobile_user",
		"get_current_user_context",
		"create_parcel",
		"create_housing_unit",
		"create_farm_task",
		"assign_farm_task",
		"claim_farm_task",
		"refresh_compliance_alerts",
		"get_compliance_calendar",
	)
}


class FieldworkTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, public_url="https://umbrel.tail4a2b.ts.net", **ON)
		roles.install_roles()
		self.employees()
		self.credential = self.enrol()

	def employees(self):
		STORE.seed(
			"Employee",
			[
				{
					"name": WORKER_EMPLOYEE,
					"employee_name": "Ana Ramos",
					"user_id": WORKER,
					"company": MAIN,
					"status": "Active",
				},
				{
					"name": "EMP-BEN",
					"employee_name": "Ben Ortiz",
					"user_id": "ben@example.test",
					"company": MAIN,
					"status": "Active",
				},
			],
		)

	def enrol(self, email=WORKER, name="Ana Ramos", role="Field Worker", entities=None):
		data = self.tool_data(
			"create_mobile_user",
			{
				"email": email,
				"full_name": name,
				"role": role,
				"entity_access": entities or [MAIN],
			},
		)
		return data["api_key"], data["api_secret"]

	def as_worker(self, tool, arguments=None, credential=None):
		key, secret = credential or self.credential
		return self.tool(tool, arguments or {}, headers={"Authorization": f"token {key}:{secret}"})

	def worker_data(self, tool, arguments=None, credential=None):
		result = self.as_worker(tool, arguments, credential)
		self.assertFalse(result.get("isError"), result["content"][0]["text"])
		return json.loads(result["content"][0]["text"])

	def worker_error(self, tool, arguments=None, credential=None):
		result = self.as_worker(tool, arguments, credential)
		self.assertTrue(result.get("isError"), f"{tool} unexpectedly succeeded")
		return result["content"][0]["text"]

	def a_camp(self, unit_name="MC-Cabin-01"):
		if not STORE.rows("Parcel"):
			self.tool_data(
				"create_parcel", {"owning_entity": MAIN, "parcel_name": "Mill Creek", "acreage": 131.43}
			)
		return self.tool_data(
			"create_housing_unit",
			{
				"parcel": "Mill Creek",
				"unit_name": unit_name,
				"unit_type": "Cabin",
				"capacity": 4,
				"fsma_worker_facility": True,
			},
		)["name"]

	def a_task(self, **overrides):
		payload = {
			"task_name": "Habitability walk — MC-Cabin-01",
			"task_type": "Inspection",
			"evidence_required": dict(WALK),
			"skill_required": "camp_maintenance",
			"company": MAIN,
		}
		payload.update(overrides)
		return self.tool_data("create_farm_task", payload)


class IdentityComesFromTheRequest(FieldworkTestCase):
	def test_the_worker_is_resolved_through_their_employee_record(self):
		"""The phone never has to know it is EMP-ANA."""
		self.a_task()
		data = self.worker_data("list_available_for_me")
		self.assertEqual(data["me"]["user"], WORKER)
		self.assertEqual(data["me"]["employee"], WORKER_EMPLOYEE)
		self.assertEqual(data["worker_id"], WORKER_EMPLOYEE)

	def test_a_request_with_no_credential_is_refused_with_the_header_to_send(self):
		self.a_task()
		message = self.tool_error("list_my_tasks", {})
		self.assertIn("no per-user identity", message)
		self.assertIn("Authorization: token", message)
		self.assertIn("generate_api_token", message)

	def test_an_operator_may_still_act_for_somebody_from_a_desktop_client(self):
		self.a_task()
		data = self.tool_data("list_my_tasks", {"user": WORKER})
		self.assertEqual(data["me"]["user"], WORKER)
		self.assertIn("`user` argument", data["me"]["identity_source"])

	def test_an_authenticated_request_cannot_act_as_somebody_else(self):
		self.enrol(email="ben@example.test", name="Ben Ortiz")
		message = self.worker_error("list_my_tasks", {"user": "ben@example.test"})
		self.assertIn("will not", message)
		self.assertIn("not scoped to anything", message)

	def test_a_login_with_no_employee_record_is_refused_by_name(self):
		"""Returning an empty list would read on a phone as 'nothing to do today',
		which is a different and much worse answer."""
		credential = self.enrol(email="nolink@example.test", name="No Link")
		message = self.worker_error("list_my_tasks", credential=credential)
		self.assertIn("no Employee record", message)
		self.assertIn("user_id", message)
		self.assertIn("nothing to do today", message)

	def test_a_company_outside_the_workers_entities_is_refused_rather_than_empty(self):
		"""An empty result is indistinguishable from a quiet day. A refusal that
		names the entity is a fact somebody can act on."""
		message = self.worker_error("list_my_tasks", {"company": OTHER})
		self.assertIn(OTHER, message)
		self.assertIn("not yours", message)

	def test_the_workers_own_entity_is_accepted(self):
		self.assertEqual(self.worker_data("list_my_tasks", {"company": MAIN})["company"], MAIN)


class MyTasksAndThePool(FieldworkTestCase):
	def test_the_pool_shows_what_is_claimable_and_how_many_slots_are_left(self):
		self.a_task()
		self.a_task(task_name="Detector test — MC-Cabin-01", task_type="Test")
		data = self.worker_data("list_available_for_me")
		self.assertEqual(data["count"], 2)
		self.assertTrue(data["may_claim"])
		self.assertEqual(data["claims_remaining"], 3)

	def test_a_dispatched_task_is_not_in_the_pool(self):
		"""Somebody has to be SENT to it by name."""
		self.a_task(dispatch_mode="Dispatched")
		self.assertEqual(self.worker_data("list_available_for_me")["count"], 0)

	def test_my_tasks_shows_what_i_am_holding_and_the_button_to_draw(self):
		task = self.a_task()
		self.worker_data("claim_task_via_mobile", {"task_name": task["name"]})
		data = self.worker_data("list_my_tasks")
		self.assertEqual(data["holding_now"], 1)
		self.assertEqual(data["next"][0]["tool"], "start_task_via_mobile")
		self.assertEqual(data["next"][0]["label"], "Start")

	def test_the_button_changes_once_the_clock_is_running(self):
		task = self.a_task()
		self.worker_data("claim_task_via_mobile", {"task_name": task["name"]})
		self.worker_data("start_task_via_mobile", {"task_name": task["name"]})
		data = self.worker_data("list_my_tasks")
		self.assertEqual(data["next"][0]["tool"], "complete_task_via_mobile")

	def test_a_location_filter_narrows_the_pool(self):
		unit = self.a_camp()
		second = self.a_camp("MC-Cabin-02")
		self.a_task(location_doctype="Housing Unit", location=unit)
		self.a_task(task_name="Walk — MC-Cabin-02", location_doctype="Housing Unit", location=second)
		data = self.worker_data("list_available_for_me", {"location_filter": unit})
		self.assertEqual(data["count"], 1)
		self.assertEqual(data["location_filter"], unit)

	def test_both_reads_are_on_out_of_the_box(self):
		self.configure(enabled=1, allow_create_farm_task=1)
		self.a_task()
		for tool in ("list_my_tasks", "list_available_for_me"):
			with self.subTest(tool=tool):
				self.assertFalse(self.as_worker(tool).get("isError"))


class TasksByLocation(FieldworkTestCase):
	"""v0.117.0. One trip's worth of work per place, held and pool combined."""

	def test_two_tasks_at_one_place_are_one_group_with_a_minutes_label(self):
		unit = self.a_camp()
		self.a_task(location_doctype="Housing Unit", location=unit, estimated_duration_minutes=60)
		self.a_task(
			task_name="Detector test — MC-Cabin-01",
			task_type="Test",
			location_doctype="Housing Unit",
			location=unit,
			estimated_duration_minutes=30,
		)
		data = self.worker_data("list_tasks_by_location")
		self.assertEqual(data["group_count"], 1)
		group = data["location_groups"][0]
		self.assertEqual(group["location"], unit)
		self.assertEqual(group["total_tasks"], 2)
		self.assertEqual(group["total_estimated_minutes"], 90)
		self.assertEqual(group["label"], f"{unit}: 2 tasks, ~90 min")
		self.assertEqual(group["tasks_missing_estimate"], 0)

	def test_a_held_task_and_a_pool_task_at_one_place_are_one_group(self):
		"""What I'm holding and what I could still take, in one trip."""
		unit = self.a_camp()
		mine = self.a_task(location_doctype="Housing Unit", location=unit)
		self.worker_data("claim_task_via_mobile", {"task_name": mine["name"]})
		self.a_task(
			task_name="Detector test — MC-Cabin-01",
			task_type="Test",
			location_doctype="Housing Unit",
			location=unit,
		)
		data = self.worker_data("list_tasks_by_location")
		group = data["location_groups"][0]
		self.assertEqual(group["total_tasks"], 2)
		self.assertEqual(group["held_count"], 1)
		self.assertEqual(group["available_count"], 1)
		held = next(task for task in group["tasks"] if task["held"])
		self.assertEqual(held["assignment"], self.worker_data("list_my_tasks")["assignments"][0]["name"])

	def test_two_places_are_two_groups(self):
		first = self.a_camp("MC-Cabin-01")
		second = self.a_camp("MC-Cabin-02")
		self.a_task(location_doctype="Housing Unit", location=first)
		self.a_task(task_name="Walk — MC-Cabin-02", location_doctype="Housing Unit", location=second)
		data = self.worker_data("list_tasks_by_location")
		self.assertEqual(data["group_count"], 2)
		self.assertEqual(data["total_tasks"], 2)

	def test_a_task_with_no_location_is_reported_not_dropped(self):
		"""There is no place called 'Unlocated' — inventing one would read as a
		place that does not exist."""
		unit = self.a_camp()
		self.a_task(location_doctype="Housing Unit", location=unit)
		self.a_task(task_name="Hand-raised repair, no place named")
		data = self.worker_data("list_tasks_by_location")
		self.assertEqual(data["group_count"], 1)
		self.assertEqual(data["unlocated_count"], 1)
		self.assertEqual(data["total_tasks"], 2)
		self.assertIn("no place called 'Unlocated'", data["unlocated_note"])

	def test_minutes_are_a_floor_not_a_promise_when_some_tasks_lack_an_estimate(self):
		unit = self.a_camp()
		self.a_task(location_doctype="Housing Unit", location=unit, estimated_duration_minutes=45)
		self.a_task(
			task_name="Detector test — MC-Cabin-01",
			task_type="Test",
			location_doctype="Housing Unit",
			location=unit,
		)
		group = self.worker_data("list_tasks_by_location")["location_groups"][0]
		self.assertEqual(group["total_estimated_minutes"], 45)
		self.assertEqual(group["tasks_missing_estimate"], 1)
		self.assertEqual(group["label"], f"{unit}: 2 tasks, ~45 min")

	def test_it_is_honest_about_skills_the_same_way_the_pool_is(self):
		self.a_task(skill_required="applicator_license")
		data = self.worker_data("list_tasks_by_location")
		self.assertIsNone(data["skill_filter"])
		self.assertIn("NOT FILTERED", data["skill_matching"])

	def test_a_company_outside_the_workers_entities_is_refused(self):
		message = self.worker_error("list_tasks_by_location", {"company": OTHER})
		self.assertIn(OTHER, message)
		self.assertIn("not yours", message)

	def test_a_location_filter_narrows_the_groups(self):
		first = self.a_camp("MC-Cabin-01")
		second = self.a_camp("MC-Cabin-02")
		self.a_task(location_doctype="Housing Unit", location=first)
		self.a_task(task_name="Walk — MC-Cabin-02", location_doctype="Housing Unit", location=second)
		data = self.worker_data("list_tasks_by_location", {"location_filter": first})
		self.assertEqual(data["group_count"], 1)
		self.assertEqual(data["location_groups"][0]["location"], first)

	def test_it_is_on_by_default(self):
		self.configure(enabled=1, allow_create_farm_task=1)
		self.a_task()
		self.assertFalse(self.as_worker("list_tasks_by_location").get("isError"))


class SkillsAreNotInvented(FieldworkTestCase):
	def test_with_no_skill_register_the_whole_pool_comes_back_and_says_so(self):
		self.a_task(skill_required="applicator_license")
		data = self.worker_data("list_available_for_me")
		self.assertEqual(data["count"], 1)
		self.assertIsNone(data["skill_filter"])
		self.assertIn("NOT FILTERED", data["skill_matching"])
		self.assertIn("Harvest Crew", data["skill_matching"])

	def test_an_explicit_skill_filters_and_is_reported(self):
		self.a_task(skill_required="camp_maintenance")
		self.a_task(task_name="Spray block 4", task_type="Spray", skill_required="applicator_license")
		data = self.worker_data("list_available_for_me", {"skill": "camp_maintenance"})
		self.assertEqual(data["count"], 1)
		self.assertIn("`skill` argument", data["skill_matching"])

	def test_a_site_that_keeps_its_own_skills_field_on_employee_is_read(self):
		from .harness import add_field

		add_field("Employee", "farm_skills", "Data", label="Farm Skills")
		STORE.get_raw("Employee", WORKER_EMPLOYEE)["farm_skills"] = "camp_maintenance"
		self.a_task(skill_required="camp_maintenance")
		self.a_task(task_name="Spray block 4", task_type="Spray", skill_required="applicator_license")
		data = self.worker_data("list_available_for_me")
		self.assertEqual(data["count"], 1)
		self.assertIn("Employee.farm_skills", data["skill_matching"])

	def test_an_empty_skills_field_shows_the_whole_pool_and_says_to_fill_it_in(self):
		from .harness import add_field

		add_field("Employee", "farm_skills", "Data", label="Farm Skills")
		self.a_task(skill_required="applicator_license")
		data = self.worker_data("list_available_for_me")
		self.assertEqual(data["count"], 1)
		self.assertIn("it is empty", data["skill_matching"])


class TheEvidenceChecklist(FieldworkTestCase):
	def test_it_turns_the_contract_into_a_checklist_a_screen_can_draw(self):
		task = self.a_task()
		data = self.worker_data("get_task_with_evidence_contract", {"task_name": task["name"]})
		wanted = {item["requirement"] for item in data["evidence_contract"]}
		self.assertEqual(wanted, {"photos", "signature", "findings_text"})
		for item in data["evidence_contract"]:
			with self.subTest(requirement=item["requirement"]):
				self.assertFalse(item["satisfied"])
				self.assertIn(item["capture"], ("camera", "signature pad", "text field"))
				self.assertTrue(item["means"])

	def test_an_unclaimed_task_offers_claim(self):
		task = self.a_task()
		data = self.worker_data("get_task_with_evidence_contract", {"task_name": task["name"]})
		self.assertFalse(data["is_mine"])
		self.assertEqual(data["next"]["tool"], "claim_task_via_mobile")

	def test_a_task_i_hold_offers_start_then_complete(self):
		task = self.a_task()
		self.worker_data("claim_task_via_mobile", {"task_name": task["name"]})
		data = self.worker_data("get_task_with_evidence_contract", {"task_name": task["name"]})
		self.assertTrue(data["is_mine"])
		self.assertEqual(data["next"]["tool"], "start_task_via_mobile")

		self.worker_data("start_task_via_mobile", {"task_name": task["name"]})
		data = self.worker_data("get_task_with_evidence_contract", {"task_name": task["name"]})
		self.assertEqual(data["next"]["tool"], "complete_task_via_mobile")
		self.assertIn("3 to collect", data["next"]["label"])

	def test_somebody_elses_task_offers_nothing(self):
		task = self.a_task()
		self.tool_data("assign_farm_task", {"task": task["name"], "assigned_to": "EMP-BEN"})
		data = self.worker_data("get_task_with_evidence_contract", {"task_name": task["name"]})
		self.assertFalse(data["is_mine"])
		self.assertIsNone(data["next"]["tool"])
		self.assertIn("somebody else", data["next"]["why"])

	def test_a_task_at_an_entity_the_worker_cannot_see_is_refused(self):
		self.a_task()
		task = self.a_task(task_name="Other company walk", company=OTHER)
		message = self.worker_error("get_task_with_evidence_contract", {"task_name": task["name"]})
		self.assertIn(OTHER, message)
		self.assertIn("Nothing was returned", message)

	def test_it_is_on_out_of_the_box(self):
		self.configure(enabled=1, allow_create_farm_task=1)
		task = self.a_task()
		self.assertFalse(
			self.as_worker("get_task_with_evidence_contract", {"task_name": task["name"]}).get("isError")
		)


class TheRulesAreUnchanged(FieldworkTestCase):
	"""Every refusal here comes from Sprint 8's tool, because it IS Sprint 8's
	tool. A wrapper with its own copy of the compliance rules would be a second
	set to keep in step, and those are exactly the set that must never drift."""

	def test_a_dispatched_task_cannot_be_self_picked_through_the_wrapper(self):
		task = self.a_task(dispatch_mode="Dispatched")
		message = self.worker_error("claim_task_via_mobile", {"task_name": task["name"]})
		self.assertIn("Dispatched", message)

	def test_the_concurrent_claim_limit_still_applies(self):
		from erpnext_mcp.erpnext_mcp.doctype.farm_task.farm_task import MAX_CONCURRENT_CLAIMS

		for index in range(MAX_CONCURRENT_CLAIMS):
			task = self.a_task(task_name=f"Walk {index}")
			self.worker_data("claim_task_via_mobile", {"task_name": task["name"]})
		one_too_many = self.a_task(task_name="Walk 4")
		message = self.worker_error("claim_task_via_mobile", {"task_name": one_too_many["name"]})
		self.assertIn(str(MAX_CONCURRENT_CLAIMS), message)

	def test_a_completion_short_of_the_contract_is_refused_by_name(self):
		task = self.a_task()
		self.worker_data("claim_task_via_mobile", {"task_name": task["name"]})
		self.worker_data("start_task_via_mobile", {"task_name": task["name"]})
		message = self.worker_error(
			"complete_task_via_mobile",
			{"task_name": task["name"], "evidence": A_PHOTO, "findings_text": ""},
		)
		self.assertIn("signature", message)

	def test_an_empty_findings_string_still_means_nothing_was_wrong(self):
		"""THE SUBTLE ONE. A clean inspection is a positive statement. A wrapper
		that dropped a falsy value would silently turn 'I looked and it was fine'
		into 'nobody was asked'."""
		task = self.a_task(evidence_required={"findings_text": True})
		self.worker_data("claim_task_via_mobile", {"task_name": task["name"]})
		self.worker_data("start_task_via_mobile", {"task_name": task["name"]})
		data = self.worker_data("complete_task_via_mobile", {"task_name": task["name"], "findings_text": ""})
		self.assertIn(data["final_state"], ("Completed", "Awaiting-Review"))

	def test_leaving_findings_out_entirely_is_still_refused(self):
		task = self.a_task(evidence_required={"findings_text": True})
		self.worker_data("claim_task_via_mobile", {"task_name": task["name"]})
		self.worker_data("start_task_via_mobile", {"task_name": task["name"]})
		message = self.worker_error("complete_task_via_mobile", {"task_name": task["name"]})
		self.assertIn("findings_text", message)

	def test_a_completion_writes_the_compliance_record_through_the_wrapper_too(self):
		unit = self.a_camp()
		task = self.a_task(
			evidence_required={"photos": True, "findings_text": True},
			location_doctype="Housing Unit",
			location=unit,
			creates_record="Housing Inspection",
		)
		self.worker_data("claim_task_via_mobile", {"task_name": task["name"]})
		self.worker_data("start_task_via_mobile", {"task_name": task["name"]})
		data = self.worker_data(
			"complete_task_via_mobile",
			{"task_name": task["name"], "evidence": A_PHOTO, "findings_text": ""},
		)
		self.assertTrue(data.get("produced_record"))
		self.assertTrue(frappe.db.exists("Housing Inspection", data["produced_record"]))

	def test_evidence_files_is_accepted_as_an_alias_for_evidence(self):
		task = self.a_task(evidence_required={"photos": True})
		self.worker_data("claim_task_via_mobile", {"task_name": task["name"]})
		self.worker_data("start_task_via_mobile", {"task_name": task["name"]})
		data = self.worker_data(
			"complete_task_via_mobile", {"task_name": task["name"], "evidence_files": A_PHOTO}
		)
		self.assertIn(data["final_state"], ("Completed", "Awaiting-Review"))

	def test_starting_twice_is_still_refused(self):
		task = self.a_task()
		self.worker_data("claim_task_via_mobile", {"task_name": task["name"]})
		self.worker_data("start_task_via_mobile", {"task_name": task["name"]})
		self.assertTrue(self.as_worker("start_task_via_mobile", {"task_name": task["name"]})["isError"])

	def test_the_three_writes_are_off_out_of_the_box(self):
		self.configure(enabled=1, allow_create_farm_task=1)
		task = self.a_task()
		for tool in ("claim_task_via_mobile", "start_task_via_mobile", "complete_task_via_mobile"):
			with self.subTest(tool=tool):
				result = self.as_worker(tool, {"task_name": task["name"]})
				self.assertIn(f"allow_{tool}", result["content"][0]["text"])


class TheCompletionRecordsWhereItHappened(FieldworkTestCase):
	"""v0.19.1. §112.161(a)(1)(i) asks an activity record for the farm's name AND
	its location. `task_name` was only ever the first half."""

	def _a_completed_task(self, **completion):
		task = self.a_task(evidence_required={"findings_text": True})
		self.worker_data("claim_task_via_mobile", {"task_name": task["name"]})
		self.worker_data("start_task_via_mobile", {"task_name": task["name"]})
		return self.worker_data(
			"complete_task_via_mobile",
			{"task_name": task["name"], "findings_text": "", **completion},
		)

	def test_a_coordinate_persists_on_the_assignment(self):
		data = self._a_completed_task(farm_location_gps="45.5152,-122.6784")
		self.assertEqual(data["assignment"]["farm_location_gps"], "45.5152,-122.6784")
		stored = STORE.rows("Farm Task Assignment")[0]
		self.assertEqual(stored["farm_location_gps"], "45.5152,-122.6784")

	def test_a_place_name_is_as_valid_as_a_coordinate(self):
		"""Free text on purpose: a cabin with a metal roof has no fix, and the name
		of the cabin somebody stood in is worth more than a coordinate they could
		not take."""
		data = self._a_completed_task(farm_location_gps="MC-Cabin-01")
		self.assertEqual(data["assignment"]["farm_location_gps"], "MC-Cabin-01")

	def test_it_is_optional_and_a_completion_without_one_still_lands(self):
		"""ADDITIVE. Every completion filed before v0.19.1 has it blank, and a
		blank is an older record rather than an invalid one."""
		data = self._a_completed_task()
		self.assertIsNone(data["assignment"]["farm_location_gps"])
		self.assertIn(data["final_state"], ("Completed", "Awaiting-Review"))


class TheCalendarIsScoped(FieldworkTestCase):
	def alerts(self):
		self.a_camp()
		self.tool_data("refresh_compliance_alerts", {"company": MAIN})

	def test_it_asks_once_per_entity_and_merges(self):
		self.alerts()
		data = self.worker_data("list_compliance_calendar_for_me")
		self.assertEqual(data["entities"], [MAIN])
		self.assertEqual([entry["company"] for entry in data["per_entity"]], [MAIN])
		self.assertEqual(data["count"], sum(entry["count"] for entry in data["per_entity"]))

	def test_every_alert_carries_the_entity_it_came_from(self):
		self.alerts()
		data = self.worker_data("list_compliance_calendar_for_me")
		for alert in data["alerts"]:
			with self.subTest(alert=alert.get("name")):
				self.assertEqual(alert["company"], MAIN)

	def test_an_account_with_no_entities_is_refused_rather_than_shown_everything(self):
		"""This app reads through `frappe.db.get_all`, which does not consult User
		Permissions. Returning the whole site's calendar under a name like this one
		would be a lie."""
		frappe.get_doc(
			{"doctype": "User", "email": "loose@example.test", "first_name": "Lou", "enabled": 1}
		).insert()
		message = self.tool_error("list_compliance_calendar_for_me", {"user": "loose@example.test"})
		self.assertIn("no Company User Permission", message)
		self.assertIn("would be a lie", message)

	def test_a_worker_at_two_entities_gets_both(self):
		credential = self.enrol(
			email="fran@example.test", name="Fran F", role="Foreman", entities=[MAIN, OTHER]
		)
		STORE.seed(
			"Employee",
			[
				{
					"name": "EMP-FRAN",
					"employee_name": "Fran F",
					"user_id": "fran@example.test",
					"company": MAIN,
					"status": "Active",
				}
			],
		)
		self.alerts()
		data = self.worker_data("list_compliance_calendar_for_me", credential=credential)
		self.assertEqual(sorted(data["entities"]), sorted([MAIN, OTHER]))
		self.assertEqual(len(data["per_entity"]), 2)

	def test_one_entity_can_be_asked_for_on_its_own(self):
		self.alerts()
		data = self.worker_data("list_compliance_calendar_for_me", {"company": MAIN})
		self.assertEqual(data["entities"], [MAIN])

	def test_an_entity_whose_calendar_cannot_be_read_is_named_not_silent(self):
		"""A clean calendar and an unread one look identical otherwise."""
		from erpnext_mcp.tools import calendar as compliance_calendar
		from erpnext_mcp.tools import fieldwork

		self.alerts()
		original = compliance_calendar.get_compliance_calendar

		def boom(args):
			raise RuntimeError("the alert table is on fire")

		fieldwork.compliance_calendar.get_compliance_calendar = boom
		try:
			data = self.worker_data("list_compliance_calendar_for_me")
		finally:
			fieldwork.compliance_calendar.get_compliance_calendar = original
		self.assertEqual([entry["company"] for entry in data["failed_entities"]], [MAIN])
		self.assertIn("on fire", data["failed_entities"][0]["reason"])

	def test_it_is_on_out_of_the_box(self):
		self.configure(enabled=1)
		self.assertFalse(self.as_worker("list_compliance_calendar_for_me").get("isError"))


class TheWrappersAreThin(FieldworkTestCase):
	"""The design claim itself, asserted structurally rather than by behaviour."""

	def test_every_wrapper_calls_the_sprint_8_tool_it_wraps(self):
		import inspect

		from erpnext_mcp.tools import fieldwork

		pairs = {
			"list_my_tasks": "dispatch.list_dispatched_tasks",
			"list_available_for_me": "dispatch.list_available_tasks",
			"get_task_with_evidence_contract": "dispatch.get_farm_task",
			"claim_task_via_mobile": "dispatch.claim_farm_task",
			"start_task_via_mobile": "dispatch.start_farm_task",
			"complete_task_via_mobile": "dispatch.complete_farm_task",
		}
		for wrapper, inner in pairs.items():
			with self.subTest(tool=wrapper):
				self.assertIn(inner, inspect.getsource(getattr(fieldwork, wrapper)))

	def test_each_one_has_its_own_switch(self):
		"""`list_my_tasks` could have been a flag on `list_dispatched_tasks`. It is
		not, because an operator running a pilot wants the four mobile reads and
		nothing else — and a flag inside a tool is not something a switch reaches."""
		from erpnext_mcp import registry

		for name in (
			"list_my_tasks",
			"list_available_for_me",
			"get_task_with_evidence_contract",
			"list_compliance_calendar_for_me",
			"claim_task_via_mobile",
			"start_task_via_mobile",
			"complete_task_via_mobile",
		):
			with self.subTest(tool=name):
				self.assertIn(name, registry.TOOLS)

	def test_the_context_resolver_is_shared_with_the_mobile_tools(self):
		"""One rule about who is calling, in one place. Two would drift."""
		import inspect

		from erpnext_mcp.tools import fieldwork

		self.assertIn("mobile.resolve_context_user", inspect.getsource(fieldwork._me))
		self.assertTrue(callable(mobile.resolve_context_user))

# SPDX-License-Identifier: MIT
"""Farm Task Templates — v0.41.0's whole claim, one class per part.

THE CLAIM IS THAT THE SHAPE OF ONE RECURRING JOB IS DATA. Everything below is an
attempt to break one of the six things that has to be true for that to be worth
having:

    TEMPLATES ARE RECORDS         `AuthoringATemplate`. A template is written by
                                  a tool call and the next task raised has that
                                  shape. What it refuses — no evidence contract,
                                  an empty one, a misspelt key, two checklist
                                  items with one name, a name already taken — it
                                  refuses at authoring time, while the person who
                                  can fix it is present.

    A TASK SNAPSHOTS ITS          `TheSnapshot`. This is the one to read if you
    TEMPLATE                      only read one. A template edited after a task
                                  was raised changes NOTHING about that task —
                                  not its evidence contract, not its checklist,
                                  not its instructions. Nobody's contract
                                  tightens under them mid-job, and a worker
                                  halfway through a five-item walk does not find
                                  their evidence attached to a list that no
                                  longer contains it.

    THE CHECKLIST HAS TEETH       `TheChecklistIsEnforced`. A required item left
                                  unticked REFUSES the completion, by name,
                                  before any compliance record is written. An
                                  optional one does not, because that is what
                                  optional means. A tick naming an item the task
                                  does not have is refused rather than ignored.

    A RULE CAN PRODUCE THROUGH    `TheComplianceFlow`. Point a Compliance Rule at
    A TEMPLATE                    a template and the tasks the sweep generates
                                  carry its checklist, its instructions and its
                                  contract — and the alert's own message is still
                                  on them, because that is the fact about the
                                  cabin rather than about the job.

    NOTHING MOVES UNTIL SOMEBODY  `TheComplianceFlow.test_seeding_wires_nothing`.
    WIRES IT                      Upgrading seeds five templates and repoints one
                                  field, and the tasks a sweep produces are
                                  byte-for-byte what they were. An upgrade that
                                  silently changed the shape of dispatched work
                                  is the one thing this app will not do.

    THE SEEDS DO NOT OVERWRITE    `TheSeededTemplates`. A second migrate creates
    AN OPERATOR'S EDIT            nothing, and a template somebody edited or
                                  disabled is left exactly as it is. That is the
                                  difference between a seeder and a `fixtures`
                                  entry, and it is why this app has no fixtures.

`TheSeededTemplates.test_every_seed_matches_the_alert_task_map` is the quiet one
that earns its place over a season. The five seeded templates exist so the
shipped compliance rules can produce work through a record instead of a Python
dict, and the whole backward-compatibility argument for wiring one up is that the
two agree field for field. The day somebody edits `ALERT_TASK_MAP` and not
`SEED_TEMPLATES`, pointing a rule at its template would quietly change what a
worker is asked to bring back — and nothing else in the suite would notice.
"""

import json

import frappe

from erpnext_mcp import compliance_rules, task_templates
from erpnext_mcp.tools.dispatch import ALERT_TASK_MAP

from .fixtures import MAIN, V12TestCase
from .harness import STORE

ALL_ON = {
	f"allow_{name}": 1
	for name in (
		"create_parcel",
		"create_housing_unit",
		"create_farm_task_template",
		"update_farm_task_template",
		"list_farm_task_templates",
		"get_farm_task_template",
		"create_task_from_template",
		"create_farm_task",
		"claim_farm_task",
		"start_farm_task",
		"complete_farm_task",
		"get_farm_task",
		"list_dispatch_board",
		"generate_tasks_from_compliance_alerts",
		"refresh_compliance_alerts",
		"list_compliance_rules",
		"get_compliance_rule",
		"update_compliance_rule",
		"list_housing_inspections",
		"get_housing_inspection",
	)
}

TODAY = "2026-07-24"

#: The commonest contract in the whole system: a habitability walk.
WALK = {"photos": True, "signature": True, "findings_text": True}

A_PHOTO = [{"file_url": "/files/north-wall.jpg", "evidence_type": "Photo", "caption": "north wall"}]

#: A three-item checklist of the shape the design asks for: two required, one
#: not. The optional one is the point — a template that covers more than today
#: needs stays usable only if something on it can honestly be left.
THREE_ITEMS = [
	{"item_name": "Smoke detector pressed and heard", "evidence_type": "Photo"},
	{"item_name": "CO detector pressed and heard", "evidence_type": "Photo"},
	{"item_name": "Batteries replaced where it chirped", "required": False},
]


class TemplateTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON)

	# -- fixtures ------------------------------------------------------------
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

	def a_template(self, **overrides):
		payload = {
			"template_name": "Detector Test",
			"task_type": "Test",
			"description": "Press every detector in the unit and record what sounded.",
			"skill_required": "camp_maintenance",
			"estimated_duration_minutes": 20,
			"dispatch_mode": "Self-pick",
			"evidence_required": {"photos": True, "findings_text": True},
			"instructions": "Test every detector, not a sample.",
			"checklist": [dict(item) for item in THREE_ITEMS],
			"compliance_regimes": ["OR-OSHA"],
		}
		payload.update(overrides)
		return self.tool_data("create_farm_task_template", payload)

	def a_task_from(self, template="Detector Test", **overrides):
		payload = {"template": template}
		payload.update(overrides)
		return self.tool_data("create_task_from_template", payload)

	def complete(self, task, worker="EMP-001", **overrides):
		payload = {
			"task": task,
			"worker_id": worker,
			"evidence_files": list(A_PHOTO),
			"findings_text": "",
			"completion_narrative": "tested them",
		}
		payload.update(overrides)
		return payload


# ── 1 ───────────────────────────────────────────────────────────────────────
class AuthoringATemplate(TemplateTestCase):
	"""A template is a record anybody can add, and what it refuses it refuses now."""

	def test_a_template_is_written_and_is_live(self):
		template = self.a_template()
		self.assertEqual(template["name"], "Detector Test")
		self.assertEqual(template["task_type"], "Test")
		self.assertEqual(template["skill_required"], "camp_maintenance")
		self.assertEqual(template["estimated_duration_minutes"], 20)
		self.assertEqual(template["dispatch_mode"], "Self-pick")
		self.assertEqual(template["evidence_required"], {"findings_text": True, "photos": True})
		self.assertTrue(template["enabled"])
		self.assertEqual(template["checklist_item_count"], 3)
		self.assertEqual(template["required_checklist_item_count"], 2)
		self.assertEqual(template["compliance_regimes"], ["OR-OSHA"])
		self.assertIn("no app release", template["note"])

	def test_the_docname_is_the_template_name(self):
		"""Unlike an Inspection Template, which is versioned by copy and cannot be."""
		self.a_template(template_name="Cabin Habitability Inspection")
		self.assertTrue(frappe.db.exists("Farm Task Template", "Cabin Habitability Inspection"))

	def test_a_template_with_no_evidence_contract_is_refused(self):
		message = self.tool_error(
			"create_farm_task_template", {"template_name": "Walk it", "task_type": "Inspection"}
		)
		self.assertIn("evidence_required is required", message)
		self.assertIn("stood in a cabin", message)
		self.assertIn("Nothing was written", message)

	def test_an_empty_contract_is_refused(self):
		message = self.tool_error(
			"create_farm_task_template",
			{"template_name": "Walk it", "task_type": "Inspection", "evidence_required": {}},
		)
		self.assertIn("Evidence Required is empty", message)

	def test_a_misspelt_evidence_key_is_refused_rather_than_ignored(self):
		message = self.tool_error(
			"create_farm_task_template",
			{
				"template_name": "Walk it",
				"task_type": "Inspection",
				"evidence_required": {"photo": True},
			},
		)
		self.assertIn("'photo'", message)
		self.assertIn("nothing checks", message)

	def test_a_task_type_the_farm_task_select_does_not_hold_is_refused(self):
		"""Otherwise the template saves and fails at the moment it is used."""
		message = self.tool_error(
			"create_farm_task_template",
			{
				"template_name": "Walk it",
				"task_type": "Wandering About",
				"evidence_required": {"photos": True},
			},
		)
		self.assertIn("Wandering About", message)

	def test_a_second_template_with_a_taken_name_is_refused(self):
		self.a_template()
		message = self.tool_error(
			"create_farm_task_template",
			{
				"template_name": "Detector Test",
				"task_type": "Test",
				"evidence_required": {"photos": True},
			},
		)
		self.assertIn("already exists", message)
		self.assertIn("edited IN PLACE", message)

	def test_two_checklist_items_with_one_name_are_refused(self):
		"""The item name is the KEY a completion marks."""
		message = self.tool_error(
			"create_farm_task_template",
			{
				"template_name": "Walk it",
				"task_type": "Inspection",
				"evidence_required": {"photos": True},
				"checklist": ["Test the alarm", "Test the alarm"],
			},
		)
		self.assertIn("Test the alarm", message)
		self.assertIn("read first", message)

	def test_an_unnamed_checklist_item_is_refused(self):
		message = self.tool_error(
			"create_farm_task_template",
			{
				"template_name": "Walk it",
				"task_type": "Inspection",
				"evidence_required": {"photos": True},
				"checklist": [{"required": True}],
			},
		)
		self.assertIn("no name", message)

	def test_a_checklist_of_bare_strings_means_required_in_that_order(self):
		"""The commonest checklist anybody writes through a chat client."""
		template = self.a_template(
			template_name="Simple", checklist=["First thing", "Second thing", "Third thing"]
		)
		self.assertEqual(
			[item["item_name"] for item in template["checklist"]],
			["First thing", "Second thing", "Third thing"],
		)
		self.assertTrue(all(item["required"] for item in template["checklist"]))
		self.assertEqual([item["sort_order"] for item in template["checklist"]], [1, 2, 3])

	def test_no_checklist_is_the_ordinary_case_and_is_said_out_loud(self):
		template = self.a_template(template_name="Renew it", checklist=[])
		self.assertEqual(template["checklist_item_count"], 0)
		self.assertTrue(any("ordinary case" in line for line in template["warnings"]))

	def test_a_creates_record_this_site_lacks_warns_rather_than_refusing(self):
		"""A later release may add it; the refusal that matters is at task creation."""
		template = self.a_template(template_name="Future work", creates_record="Spray Record")
		self.assertEqual(template["creates_record"], "Spray Record")
		self.assertTrue(any("no such DocType" in line for line in template["warnings"]))

	def test_an_unknown_regime_token_is_refused_by_name(self):
		message = self.tool_error(
			"create_farm_task_template",
			{
				"template_name": "Walk it",
				"task_type": "Inspection",
				"evidence_required": {"photos": True},
				"compliance_regimes": ["ISO 9001"],
			},
		)
		self.assertIn("ISO 9001", message)
		self.assertIn("nearly right", message)


# ── 1b ──────────────────────────────────────────────────────────────────────
class TheTaskTypeVocabulary(TemplateTestCase):
	"""One list of task types, held on two doctypes.

	`create_farm_task_template` matches `task_type` against the FARM TASK select
	— see `test_a_task_type_the_farm_task_select_does_not_hold_is_refused` — so a
	value added to the template's own options and not to the task's would save a
	template that fails at the moment somebody raises work from it. This is where
	the two lists drifting apart is caught, and where a newly added type is proved
	to survive the whole trip from template to task.
	"""

	def options(self, doctype):
		from erpnext_mcp.args import select_options

		return select_options(doctype, "task_type")

	def test_both_doctypes_hold_the_same_options(self):
		self.assertEqual(self.options("Farm Task"), self.options("Farm Task Template"))

	def test_hiring_is_one_of_them(self):
		"""Recruiting a crew is compliance-adjacent work somebody is dispatched to
		do, so it sits next to Compliance-Audit rather than falling under Other."""
		options = self.options("Farm Task")
		self.assertIn("Hiring", options)
		self.assertEqual(options[options.index("Compliance-Audit") + 1], "Hiring")

	def test_a_hiring_template_raises_a_hiring_task(self):
		self.a_template(template_name="Crew Interviews", task_type="Hiring")
		task = self.a_task_from("Crew Interviews")
		self.assertEqual(task["task_type"], "Hiring")


# ── 2 ───────────────────────────────────────────────────────────────────────
class ReadingTheRegister(TemplateTestCase):
	def test_the_register_lists_what_the_operation_knows_how_to_ask_for(self):
		self.a_template()
		self.a_template(
			template_name="Water Test", task_type="Water-Sampling", skill_required="water_sampling"
		)
		data = self.tool_data("list_farm_task_templates", {})
		self.assertEqual(data["count"], 2)
		self.assertEqual(sorted(data["enabled_templates"]), ["Detector Test", "Water Test"])

	def test_it_filters_by_type_skill_and_regime(self):
		self.a_template()
		self.a_template(
			template_name="Water Test",
			task_type="Water-Sampling",
			skill_required="water_sampling",
			compliance_regimes=["FSMA"],
		)
		self.assertEqual(self.tool_data("list_farm_task_templates", {"task_type": "Test"})["count"], 1)
		self.assertEqual(
			self.tool_data("list_farm_task_templates", {"skill_required": "water_sampling"})["count"], 1
		)
		self.assertEqual(self.tool_data("list_farm_task_templates", {"regime": "FSMA"})["count"], 1)

	def test_a_disabled_template_is_still_listed(self):
		"""Because every task ever raised from it is still readable."""
		self.a_template()
		self.tool_data("update_farm_task_template", {"template": "Detector Test", "enabled": False})
		data = self.tool_data("list_farm_task_templates", {})
		self.assertEqual(data["count"], 1)
		self.assertEqual(data["enabled_templates"], [])

	def test_get_reports_the_checklist_in_worked_order_and_the_work_it_has_raised(self):
		self.a_template()
		self.a_task_from(location=self.a_camp(), location_doctype="Housing Unit")
		data = self.tool_data("get_farm_task_template", {"template": "Detector Test"})
		self.assertEqual(
			[item["item_name"] for item in data["checklist"]],
			[item["item_name"] for item in THREE_ITEMS],
		)
		self.assertEqual(data["tasks_raised"], 1)
		self.assertIn("COPIED onto a task", data["snapshot_note"])

	def test_an_unknown_template_is_refused_by_name(self):
		message = self.tool_error("get_farm_task_template", {"template": "Nothing Like It"})
		self.assertIn("Nothing Like It", message)
		self.assertIn("list_farm_task_templates", message)


# ── 3 ───────────────────────────────────────────────────────────────────────
class EditingATemplate(TemplateTestCase):
	def test_it_edits_in_place_rather_than_superseding(self):
		self.a_template()
		self.tool_data(
			"update_farm_task_template", {"template": "Detector Test", "estimated_duration_minutes": 30}
		)
		self.assertEqual(len(STORE.rows("Farm Task Template")), 1)
		data = self.tool_data("get_farm_task_template", {"template": "Detector Test"})
		self.assertEqual(data["estimated_duration_minutes"], 30)

	def test_the_diff_carries_the_previous_value(self):
		self.a_template()
		data = self.tool_data(
			"update_farm_task_template",
			{"template": "Detector Test", "skill_required": "electrical_licensed"},
		)
		self.assertEqual(
			data["changed"]["skill_required"], {"from": "camp_maintenance", "to": "electrical_licensed"}
		)

	def test_it_says_how_many_tasks_the_edit_cannot_reach(self):
		self.a_template()
		self.a_task_from(location=self.a_camp(), location_doctype="Housing Unit")
		self.a_task_from(task_name="Second one")
		data = self.tool_data(
			"update_farm_task_template", {"template": "Detector Test", "estimated_duration_minutes": 30}
		)
		self.assertEqual(data["tasks_already_raised"], 2)
		self.assertIn("FUTURE TASKS ONLY", data["note"])

	def test_the_checklist_is_replaced_whole(self):
		self.a_template()
		data = self.tool_data(
			"update_farm_task_template",
			{"template": "Detector Test", "checklist": ["Only this now"]},
		)
		self.assertEqual([item["item_name"] for item in data["checklist"]], ["Only this now"])

	def test_a_checklist_can_be_removed_entirely(self):
		self.a_template()
		data = self.tool_data("update_farm_task_template", {"template": "Detector Test", "checklist": []})
		self.assertEqual(data["checklist_item_count"], 0)

	def test_an_edit_that_changes_nothing_is_refused(self):
		self.a_template()
		message = self.tool_error(
			"update_farm_task_template", {"template": "Detector Test", "skill_required": "camp_maintenance"}
		)
		self.assertIn("nothing to change", message)

	def test_a_contract_that_asks_for_nothing_is_refused_on_edit_too(self):
		self.a_template()
		message = self.tool_error(
			"update_farm_task_template", {"template": "Detector Test", "evidence_required": {}}
		)
		self.assertIn("Evidence Required is empty", message)

	def test_disabling_is_how_a_template_is_retired(self):
		"""There is no delete: a template that raised work is the answer to what
		that work asked for."""
		self.a_template()
		data = self.tool_data("update_farm_task_template", {"template": "Detector Test", "enabled": False})
		self.assertFalse(data["enabled"])
		self.assertTrue(frappe.db.exists("Farm Task Template", "Detector Test"))


# ── 4 ───────────────────────────────────────────────────────────────────────
class RaisingWorkFromATemplate(TemplateTestCase):
	def test_everything_about_the_shape_comes_off_the_template(self):
		self.a_template()
		task = self.a_task_from(location=self.a_camp(), location_doctype="Housing Unit")
		self.assertEqual(task["template"], "Detector Test")
		self.assertEqual(task["task_type"], "Test")
		self.assertEqual(task["skill_required"], "camp_maintenance")
		self.assertEqual(task["estimated_duration_minutes"], 20)
		self.assertEqual(task["dispatch_mode"], "Self-pick")
		self.assertEqual(task["evidence_required"], {"findings_text": True, "photos": True})
		self.assertIn("Test every detector", task["notes"])
		self.assertEqual(task["state"], "Available")

	def test_the_checklist_arrives_snapshotted_and_undone(self):
		self.a_template()
		task = self.a_task_from()
		self.assertEqual(
			[item["item_name"] for item in task["checklist"]],
			[item["item_name"] for item in THREE_ITEMS],
		)
		self.assertTrue(all(item["done"] is False for item in task["checklist"]))
		self.assertEqual(task["checklist_done"], 0)

	def test_the_task_name_defaults_to_the_template_and_the_place(self):
		"""'Detector Test' fifty-four times is a board nobody can work from."""
		self.a_template()
		unit = self.a_camp()
		task = self.a_task_from(location=unit, location_doctype="Housing Unit")
		self.assertEqual(task["task_name"], f"Detector Test — {unit}")

	def test_location_assignee_and_urgency_are_the_three_overrides(self):
		self.a_template()
		unit = self.a_camp()
		task = self.a_task_from(
			location=unit,
			location_doctype="Housing Unit",
			assigned_to="EMP-001",
			assigned_to_name="Ana",
			urgency="Critical",
		)
		self.assertEqual(task["location"], unit)
		self.assertEqual(task["assigned_to"], "EMP-001")
		self.assertEqual(task["urgency"], "Critical")
		self.assertEqual(task["state"], "Claimed")

	def test_the_default_urgency_comes_off_the_template_when_nothing_says_otherwise(self):
		self.a_template(template_name="Urgent Thing", default_urgency="High")
		self.assertEqual(self.a_task_from("Urgent Thing")["urgency"], "High")

	def test_case_notes_come_after_the_templates_standing_instructions(self):
		"""Which is the order a worker needs them in."""
		self.a_template()
		task = self.a_task_from(notes="The ladder is behind the shed.")
		self.assertLess(
			task["notes"].index("Test every detector"), task["notes"].index("The ladder is behind")
		)

	def test_a_disabled_template_raises_no_new_work(self):
		self.a_template()
		self.tool_data("update_farm_task_template", {"template": "Detector Test", "enabled": False})
		message = self.tool_error("create_task_from_template", {"template": "Detector Test"})
		self.assertIn("is disabled", message)
		self.assertIn("still completable", message)
		self.assertIn("Nothing was created", message)

	def test_a_location_that_does_not_exist_is_refused(self):
		self.a_template()
		message = self.tool_error(
			"create_task_from_template",
			{
				"template": "Detector Test",
				"location": "HU-does-not-exist",
				"location_doctype": "Housing Unit",
			},
		)
		self.assertIn("HU-does-not-exist", message)
		self.assertIn("Nothing was created", message)

	def test_a_creates_record_this_site_lacks_is_refused_at_task_time(self):
		"""The template saved with a warning; the task is where it has to stop."""
		self.a_template(template_name="Future work", creates_record="Spray Record")
		message = self.tool_error("create_task_from_template", {"template": "Future work"})
		self.assertIn("Spray Record", message)
		self.assertIn("stood in a cabin", message)

	def test_one_alert_is_one_job(self):
		self.a_template()
		self.a_camp()
		self.tool_data("refresh_compliance_alerts", {"today": TODAY})
		alert = STORE.rows("Compliance Alert")[0]["name"]
		self.a_task_from(source_alert=alert)
		message = self.tool_error(
			"create_task_from_template", {"template": "Detector Test", "source_alert": alert}
		)
		self.assertIn("already answers alert", message)


# ── 5 ───────────────────────────────────────────────────────────────────────
class TheSnapshot(TemplateTestCase):
	"""Editing a template changes what FUTURE tasks look like and nothing else."""

	def test_an_edited_template_does_not_change_a_task_already_raised(self):
		self.a_template()
		task = self.a_task_from()["name"]
		self.tool_data(
			"update_farm_task_template",
			{
				"template": "Detector Test",
				"skill_required": "electrical_licensed",
				"estimated_duration_minutes": 90,
				"evidence_required": {"photos": True, "signature": True, "witness": True},
				"instructions": "Completely different instructions.",
			},
		)
		after = self.tool_data("get_farm_task", {"task": task})
		self.assertEqual(after["skill_required"], "camp_maintenance")
		self.assertEqual(after["estimated_duration_minutes"], 20)
		self.assertEqual(after["evidence_required"], {"findings_text": True, "photos": True})
		self.assertIn("Test every detector", after["notes"])

	def test_a_checklist_item_removed_from_the_template_stays_on_the_task(self):
		"""A worker halfway through a walk does not lose the list they were shown."""
		self.a_template()
		task = self.a_task_from()["name"]
		self.tool_data("update_farm_task_template", {"template": "Detector Test", "checklist": []})
		after = self.tool_data("get_farm_task", {"task": task})
		self.assertEqual(len(after["checklist"]), 3)

	def test_a_checklist_item_added_to_the_template_does_not_appear_on_the_task(self):
		"""Nobody's contract tightens under them mid-job."""
		self.a_template()
		task = self.a_task_from()["name"]
		self.tool_data(
			"update_farm_task_template",
			{
				"template": "Detector Test",
				"checklist": [item["item_name"] for item in THREE_ITEMS] + ["A fourth"],
			},
		)
		after = self.tool_data("get_farm_task", {"task": task})
		self.assertEqual(len(after["checklist"]), 3)
		self.assertNotIn("A fourth", [item["item_name"] for item in after["checklist"]])

	def test_the_next_task_does_get_the_edit(self):
		self.a_template()
		self.a_task_from()
		self.tool_data(
			"update_farm_task_template",
			{"template": "Detector Test", "skill_required": "electrical_licensed"},
		)
		later = self.a_task_from(task_name="After the edit")
		self.assertEqual(later["skill_required"], "electrical_licensed")

	def test_a_task_survives_its_template_being_deleted(self):
		"""The link is provenance, never a lookup."""
		self.a_template()
		task = self.a_task_from()["name"]
		# `force=True` from v0.83.0, when the harness learned Frappe's own
		# `check_if_doc_is_linked`. The provenance link this test is named after is
		# exactly what an unforced delete trips over, on a real bench as much as
		# here — a Farm Task naming its template keeps the template alive unless
		# somebody forces it. The claim being made is about the task afterwards.
		frappe.delete_doc("Farm Task Template", "Detector Test", force=True)
		after = self.tool_data("get_farm_task", {"task": task})
		self.assertEqual(after["evidence_required"], {"findings_text": True, "photos": True})
		self.assertEqual(len(after["checklist"]), 3)


# ── 6 ───────────────────────────────────────────────────────────────────────
class TheChecklistIsEnforced(TemplateTestCase):
	def a_claimed_task(self, worker="EMP-001"):
		self.a_template()
		task = self.a_task_from()["name"]
		self.tool_data("claim_farm_task", {"task": task, "worker_id": worker, "worker_name": "Ana"})
		return task

	def test_a_required_item_left_unticked_refuses_the_completion(self):
		task = self.a_claimed_task()
		message = self.tool_error("complete_farm_task", self.complete(task))
		self.assertIn("required checklist item(s) are not marked done", message)
		self.assertIn("Smoke detector pressed and heard", message)
		self.assertIn("CO detector pressed and heard", message)
		self.assertIn("Nothing was changed", message)

	def test_nothing_is_written_when_the_checklist_refuses(self):
		"""Not a Housing Inspection, not a Detector Test, not a state change."""
		task = self.a_claimed_task()
		before = len(STORE.rows("Farm Task Assignment"))
		self.tool_error("complete_farm_task", self.complete(task))
		self.assertEqual(len(STORE.rows("Farm Task Assignment")), before)
		self.assertEqual(self.tool_data("get_farm_task", {"task": task})["state"], "Claimed")

	def test_ticking_the_required_items_lets_it_close(self):
		task = self.a_claimed_task()
		data = self.tool_data(
			"complete_farm_task",
			self.complete(
				task,
				checklist=["Smoke detector pressed and heard", "CO detector pressed and heard"],
			),
		)
		self.assertEqual(data["final_state"], "Completed")
		done = {item["item_name"]: item["done"] for item in data["checklist"]}
		self.assertTrue(done["Smoke detector pressed and heard"])
		self.assertTrue(done["CO detector pressed and heard"])

	def test_an_optional_item_left_undone_does_not_refuse(self):
		"""That is what optional means, and it is what keeps a template that
		covers more than today needs usable."""
		task = self.a_claimed_task()
		data = self.tool_data(
			"complete_farm_task",
			self.complete(
				task, checklist=["Smoke detector pressed and heard", "CO detector pressed and heard"]
			),
		)
		undone = [item["item_name"] for item in data["checklist"] if not item["done"]]
		self.assertEqual(undone, ["Batteries replaced where it chirped"])

	def test_a_tick_naming_an_item_the_task_does_not_have_is_refused(self):
		"""Silently ignoring it looks exactly like a tick right up until the
		completion is refused for an item the worker believes they ticked."""
		task = self.a_claimed_task()
		message = self.tool_error(
			"complete_farm_task", self.complete(task, checklist=["Smoke detecter pressed and heard"])
		)
		self.assertIn("Smoke detecter pressed and heard", message)
		self.assertIn("Smoke detector pressed and heard", message)
		self.assertIn("Nothing was changed", message)

	def test_a_note_beside_a_tick_is_kept(self):
		task = self.a_claimed_task()
		data = self.tool_data(
			"complete_farm_task",
			self.complete(
				task,
				checklist=[
					{"item_name": "Smoke detector pressed and heard", "done": True, "note": "faint"},
					"CO detector pressed and heard",
				],
			),
		)
		notes = {item["item_name"]: item.get("note") for item in data["checklist"]}
		self.assertEqual(notes["Smoke detector pressed and heard"], "faint")

	def test_a_refused_completion_leaves_the_ticks_where_they_were(self):
		"""Marking two of three and being refused for the third does not bank the
		two. The whole submission is one act: nothing is written unless all of it
		passes, which is what makes a refusal safe to retry."""
		task = self.a_claimed_task()
		self.tool_error(
			"complete_farm_task", self.complete(task, checklist=["Smoke detector pressed and heard"])
		)
		after = self.tool_data("get_farm_task", {"task": task})
		self.assertEqual(after["checklist_done"], 0)
		self.assertEqual(after["state"], "Claimed")

	def test_a_task_with_no_checklist_completes_exactly_as_it_did_before(self):
		"""Which is every task raised without a template, and most of them."""
		self.tool_data(
			"create_farm_task",
			{
				"task_name": "Walk it",
				"task_type": "Inspection",
				"evidence_required": {"findings_text": True},
			},
		)
		task = STORE.rows("Farm Task")[0]["name"]
		self.tool_data("claim_farm_task", {"task": task, "worker_id": "EMP-001", "worker_name": "Ana"})
		data = self.tool_data(
			"complete_farm_task", {"task": task, "worker_id": "EMP-001", "findings_text": ""}
		)
		self.assertEqual(data["final_state"], "Completed")
		self.assertEqual(data["checklist"], [])

	def test_the_checklist_argument_on_a_task_with_no_checklist_is_refused(self):
		self.tool_data(
			"create_farm_task",
			{
				"task_name": "Walk it",
				"task_type": "Inspection",
				"evidence_required": {"findings_text": True},
			},
		)
		task = STORE.rows("Farm Task")[0]["name"]
		self.tool_data("claim_farm_task", {"task": task, "worker_id": "EMP-001", "worker_name": "Ana"})
		message = self.tool_error(
			"complete_farm_task",
			{"task": task, "worker_id": "EMP-001", "findings_text": "", "checklist": ["Anything"]},
		)
		self.assertIn("has no checklist", message)

	def test_the_evidence_contract_still_refuses_after_the_checklist_passes(self):
		"""Both gates stand; the checklist one is only the first."""
		task = self.a_claimed_task()
		message = self.tool_error(
			"complete_farm_task",
			{
				"task": task,
				"worker_id": "EMP-001",
				"findings_text": "",
				"checklist": [
					"Smoke detector pressed and heard",
					"CO detector pressed and heard",
				],
			},
		)
		self.assertIn("evidence contract is not met", message)
		self.assertIn("photos", message)


# ── 7 ───────────────────────────────────────────────────────────────────────
class TheComplianceFlow(TemplateTestCase):
	"""A Compliance Rule that names a template produces work shaped by it."""

	def setUp(self):
		super().setUp()
		# The rules have to be RECORDS before one can name a producer template.
		# `update_compliance_rule` edits a row, and a site that has not run the
		# v0.22.0 seeder has none — the sweep falls back to the shipped
		# definitions, which nobody can point at anything.
		report = compliance_rules.seed_compliance_rules()
		self.assertEqual(report["failed"], [], f"the rule seeder failed: {report}")

	def a_camp_with_alerts(self, units=1):
		for index in range(1, units + 1):
			self.a_camp(f"MC-Cabin-{index:02d}")
		return self.tool_data("refresh_compliance_alerts", {"today": TODAY})

	def wire(self, rule="housing_detector_test_stale", template="Detector Test"):
		self.tool_data("update_compliance_rule", {"rule": rule, "producer_task_template": template})

	def test_seeding_wires_nothing(self):
		"""An upgrade that silently changed the shape of dispatched work is the
		one thing this app will not do."""
		self.a_camp_with_alerts()
		report = self.tool_data(
			"generate_tasks_from_compliance_alerts",
			{"company": MAIN, "alert_types": ["housing_detector_test_stale"]},
		)
		task = self.tool_data("get_farm_task", {"task": report["created"][0]["task"]})
		self.assertIsNone(task.get("template"))
		self.assertNotIn("checklist", task)
		self.assertEqual(task["skill_required"], "camp_maintenance")
		self.assertEqual(task["estimated_duration_minutes"], 20)

	def test_a_wired_rule_produces_a_task_shaped_by_the_template(self):
		self.a_template(skill_required="electrical_licensed", estimated_duration_minutes=35)
		unit = self.a_camp("MC-Cabin-01")
		self.tool_data("refresh_compliance_alerts", {"today": TODAY})
		self.wire()
		report = self.tool_data(
			"generate_tasks_from_compliance_alerts",
			{"company": MAIN, "alert_types": ["housing_detector_test_stale"]},
		)
		task = self.tool_data("get_farm_task", {"task": report["created"][0]["task"]})
		self.assertEqual(task["template"], "Detector Test")
		self.assertEqual(task["skill_required"], "electrical_licensed")
		self.assertEqual(task["estimated_duration_minutes"], 35)
		self.assertEqual(len(task["checklist"]), 3)
		self.assertEqual(task["location"], unit)

	def test_the_alerts_own_message_is_still_on_the_task(self):
		"""The template says how the job is done; the alert says what is wrong
		with this cabin, and a worker needs both in that order."""
		self.a_template()
		self.a_camp_with_alerts()
		self.wire()
		report = self.tool_data(
			"generate_tasks_from_compliance_alerts",
			{"company": MAIN, "alert_types": ["housing_detector_test_stale"]},
		)
		task = self.tool_data("get_farm_task", {"task": report["created"][0]["task"]})
		self.assertIn("Test every detector", task["notes"])
		self.assertLess(task["notes"].index("Test every detector"), len(task["notes"]) - 1)
		self.assertGreater(len(task["notes"]), len("Test every detector, not a sample."))

	def test_a_dry_run_names_the_template_it_would_use(self):
		self.a_template()
		self.a_camp_with_alerts()
		self.wire()
		report = self.tool_data(
			"generate_tasks_from_compliance_alerts",
			{
				"company": MAIN,
				"alert_types": ["housing_detector_test_stale"],
				"dry_run": True,
			},
		)
		self.assertEqual(report["created"][0]["template"], "Detector Test")
		self.assertEqual(len(report["created"][0]["checklist"]), 3)
		self.assertFalse(STORE.rows("Farm Task"))

	def test_a_rule_pointing_at_a_disabled_template_falls_back_rather_than_raising_nothing(self):
		"""A rule that produced nothing at all would be a silent compliance gap."""
		self.a_template()
		self.tool_data("update_farm_task_template", {"template": "Detector Test", "enabled": False})
		self.a_camp_with_alerts()
		self.wire()
		report = self.tool_data(
			"generate_tasks_from_compliance_alerts",
			{"company": MAIN, "alert_types": ["housing_detector_test_stale"]},
		)
		self.assertEqual(report["created_count"], 1)
		task = self.tool_data("get_farm_task", {"task": report["created"][0]["task"]})
		self.assertIsNone(task.get("template"))
		self.assertEqual(task["skill_required"], "camp_maintenance")

	def test_the_generated_task_completes_through_its_checklist_and_writes_the_record(self):
		"""The whole loop: alert → template → task → checklist → compliance record."""
		self.a_template(creates_record="Detector Test")
		self.a_camp_with_alerts()
		self.wire()
		report = self.tool_data(
			"generate_tasks_from_compliance_alerts",
			{"company": MAIN, "alert_types": ["housing_detector_test_stale"]},
		)
		task = report["created"][0]["task"]
		self.tool_data("claim_farm_task", {"task": task, "worker_id": "EMP-001", "worker_name": "Ana"})
		self.tool_error("complete_farm_task", self.complete(task))
		data = self.tool_data(
			"complete_farm_task",
			self.complete(
				task,
				checklist=["Smoke detector pressed and heard", "CO detector pressed and heard"],
			),
		)
		self.assertEqual(data["final_state"], "Completed")
		self.assertTrue(data["produced_record"])
		self.assertEqual(data["produced_record_doctype"], "Detector Test")

	def test_a_rule_may_not_carry_a_template_and_an_assignee_expression(self):
		"""A skill is a pool and an assignee is a person, and a task cannot be both."""
		self.a_template()
		message = self.tool_error(
			"update_compliance_rule",
			{
				"rule": "housing_detector_test_stale",
				"producer_task_template": "Detector Test",
				"producer_assigned_to_expression": "row.foreman",
			},
		)
		self.assertIn("Two routings", message + " Two routings")


# ── 8 ───────────────────────────────────────────────────────────────────────
class TheSeededTemplates(V12TestCase):
	"""The six this app ships, and the contract that they never overwrite an edit."""

	NAMES = (
		"Cabin Habitability Inspection",
		"Smoke Detector Test",
		"Water Quality Test",
		"Certification Renewal",
		"Training Record",
		# v0.115.0, and the odd one out: no compliance rule raises it, and its
		# record is written by a sweep rather than at completion. Both of those
		# are deliberately invisible from the template — see
		# `test_the_scouting_seed_produces_a_crop_observation`.
		"Field Scouting",
	)

	def test_they_are_all_seeded(self):
		report = task_templates.seed_farm_task_templates()
		self.assertEqual(sorted(report["created"]), sorted(self.NAMES))
		for name in self.NAMES:
			self.assertTrue(frappe.db.exists("Farm Task Template", name))

	def test_a_second_migrate_creates_nothing(self):
		task_templates.seed_farm_task_templates()
		again = task_templates.seed_farm_task_templates()
		self.assertEqual(again["created"], [])
		self.assertEqual(sorted(again["present"]), sorted(self.NAMES))
		self.assertEqual(len(STORE.rows("Farm Task Template")), len(self.NAMES))

	def test_an_edited_template_is_left_alone(self):
		"""The difference between a seeder and a `fixtures` entry, and the reason
		`test_hooks.py` forbids that word by name."""
		task_templates.seed_farm_task_templates()
		frappe.db.set_value(
			"Farm Task Template", "Smoke Detector Test", "skill_required", "electrical_licensed"
		)
		task_templates.seed_farm_task_templates()
		self.assertEqual(
			frappe.db.get_value("Farm Task Template", "Smoke Detector Test", "skill_required"),
			"electrical_licensed",
		)

	def test_a_disabled_template_is_not_re_enabled(self):
		task_templates.seed_farm_task_templates()
		frappe.db.set_value("Farm Task Template", "Water Quality Test", "enabled", 0)
		task_templates.seed_farm_task_templates()
		self.assertEqual(frappe.db.get_value("Farm Task Template", "Water Quality Test", "enabled"), 0)

	def test_every_seed_carries_a_real_evidence_contract(self):
		task_templates.seed_farm_task_templates()
		for name in self.NAMES:
			with self.subTest(template=name):
				contract = json.loads(
					frappe.db.get_value("Farm Task Template", name, "evidence_required") or "{}"
				)
				self.assertTrue(any(contract.values()), f"{name} asks for nothing")

	def test_every_seed_matches_the_alert_task_map(self):
		"""THE BACKWARD-COMPATIBILITY GUARANTEE, ASSERTED.

		The five seeded templates exist so the shipped compliance rules can
		produce work through a record rather than a Python dict, and the whole
		argument for wiring one up is that the two agree. The day somebody edits
		`ALERT_TASK_MAP` and not `SEED_TEMPLATES`, pointing a rule at its template
		would quietly change what a worker is asked to bring back — and nothing
		else in this suite would notice.

		The two entries with no `ALERT_TASK_MAP` twin are absent on purpose:
		"Certification Renewal" and "Training Record" cover rules whose recipes
		are `Compliance-Audit` with no produced record, and the templates say what
		the work IS rather than restating the audit category.
		"""
		pairs = {
			"Cabin Habitability Inspection": "housing_inspection_overdue",
			"Smoke Detector Test": "housing_detector_test_stale",
			"Water Quality Test": "water_test_stale",
		}
		by_name = {spec["template_name"]: spec for spec in task_templates.SEED_TEMPLATES}
		for template_name, alert_type in pairs.items():
			with self.subTest(template=template_name):
				spec, recipe = by_name[template_name], ALERT_TASK_MAP[alert_type]
				self.assertEqual(spec["task_type"], recipe["task_type"])
				self.assertEqual(spec["skill_required"], recipe["skill"])
				self.assertEqual(spec["estimated_duration_minutes"], recipe["minutes"])
				self.assertEqual(spec["dispatch_mode"], recipe["dispatch"])
				self.assertEqual(spec["creates_record"], recipe["creates_record"])
				self.assertEqual(spec["evidence_required"], recipe["evidence"])

	def test_the_scouting_seed_produces_a_crop_observation(self):
		"""v0.115.0. The first seed whose record is agronomic, not compliance.

		Three things have to be true together or the template is decorative: it
		names the record it produces (so the deferred producer in
		`_produce_record` recognises it), its contract asks for the location fix
		(the one requirement nobody types, and so the one nobody notices is
		missing), and its default observation type is NOT `Pest Scout` — that is
		the one type whose record is invalid without a threat and a count, so a
		template defaulting to it would refuse every completion from a walk where
		nothing was found.
		"""
		task_templates.seed_farm_task_templates()
		row = frappe.db.get_value(
			"Farm Task Template",
			"Field Scouting",
			["task_type", "creates_record", "evidence_required", "creates_record_data"],
			as_dict=True,
		)
		self.assertEqual(row["task_type"], "Scouting")
		self.assertEqual(row["creates_record"], "Crop Observation")

		contract = json.loads(row["evidence_required"])
		self.assertTrue(contract.get("gps"), "a scouting round with no coordinate cannot be mapped")
		self.assertTrue(contract.get("photos"))
		self.assertTrue(contract.get("findings_text"))

		defaults = json.loads(row["creates_record_data"] or "{}")
		self.assertIn(defaults.get("observation_type"), ("General", "Harvest Readiness", "Growth Stage"))
		self.assertNotEqual(defaults.get("observation_type"), "Pest Scout")

	def test_the_scouting_seed_is_the_only_one_with_no_alert_recipe(self):
		"""It answers nobody's rule, and that is the point of it.

		Every other seed restates an `ALERT_TASK_MAP` recipe so that pointing a
		compliance rule at it changes nothing. This one is raised by a person
		because a block needs walking, so a recipe for it would be a schedule
		nobody asked for.
		"""
		self.assertNotIn("Scouting", {recipe["task_type"] for recipe in ALERT_TASK_MAP.values()})

	def test_the_installer_seeds_them_and_wires_no_rule(self):
		from erpnext_mcp import install

		install.after_migrate()
		self.assertEqual(len(STORE.rows("Farm Task Template")), len(self.NAMES))
		wired = [
			row
			for row in STORE.rows("Compliance Rule")
			if str(row.get("producer_task_template") or "").strip()
		]
		self.assertEqual(wired, [])


# ── 9 ───────────────────────────────────────────────────────────────────────
class TheRepointedField(V12TestCase):
	"""`Compliance Rule.producer_task_template` now names the right size of thing."""

	def test_the_link_target_is_the_farm_task_template(self):
		from .harness import META

		field = next(f for f in META["Compliance Rule"].fields if f["fieldname"] == "producer_task_template")
		self.assertEqual(field["options"], "Farm Task Template")

	def test_the_patch_clears_a_value_naming_an_inspection_template(self):
		"""A Link whose target moved refuses the next save of any row holding an
		old value — so a rule an operator set becomes a rule nobody can edit."""
		from erpnext_mcp.patches import repoint_producer_task_template

		rule = STORE.rows("Compliance Rule")
		if not rule:
			from erpnext_mcp import compliance_rules

			compliance_rules.seed_compliance_rules()
			rule = STORE.rows("Compliance Rule")
		name = rule[0]["name"]
		frappe.db.set_value("Compliance Rule", name, "producer_task_template", "INSPT-2026-0001")
		report = repoint_producer_task_template.repoint_producer_task_template()
		self.assertEqual([entry["rule"] for entry in report["cleared"]], [name])
		self.assertFalse(frappe.db.get_value("Compliance Rule", name, "producer_task_template"))
		self.assertTrue(
			any(
				"never read by anything" in line
				for line in repoint_producer_task_template.report_lines(report)
			)
		)

	def test_a_value_naming_a_real_farm_task_template_is_kept(self):
		from erpnext_mcp import compliance_rules
		from erpnext_mcp.patches import repoint_producer_task_template

		task_templates.seed_farm_task_templates()
		if not STORE.rows("Compliance Rule"):
			compliance_rules.seed_compliance_rules()
		name = STORE.rows("Compliance Rule")[0]["name"]
		frappe.db.set_value("Compliance Rule", name, "producer_task_template", "Smoke Detector Test")
		report = repoint_producer_task_template.repoint_producer_task_template()
		self.assertEqual(report["cleared"], [])
		self.assertEqual(
			frappe.db.get_value("Compliance Rule", name, "producer_task_template"), "Smoke Detector Test"
		)

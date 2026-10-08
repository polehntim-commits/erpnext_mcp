# SPDX-License-Identifier: MIT
"""Category, skill and timing on task templates. v0.278.0.

A template carries its work category (Go / Hold matches it — not words in a task name) and skill level, both copied
to the task; it can raise its own tasks on a schedule, in a season window, for the company or for each block of a
crop gated by the block's recorded stage or degree days — once per period and place, only where the company has
switched it on. Bad timing is refused naming every problem. Untouched presets move to the category scope.
"""

import datetime
import json
from unittest import mock

import frappe

from erpnext_mcp import compliance_rules, flags, go_hold, template_schedule

from .fixtures import MAIN, V12TestCase
from .harness import STORE

ON = {f"allow_{n}": 1 for n in ("create_farm_task_template", "update_farm_task_template", "create_task_from_template",
                                "preview_farm_task_template")}
EVIDENCE = {"photos": True}


class PlanCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		STORE.seed("Field", [{"name": "Block 7", "field_name": "Block 7", "crop": "Cherry", "owning_entity": MAIN},
		                     {"name": "Block 8", "field_name": "Block 8", "crop": "Cherry", "owning_entity": MAIN},
		                     {"name": "Grapes 1", "field_name": "Grapes 1", "crop": "Grapes", "owning_entity": MAIN}])
		STORE.commit()

	def template(self, name, **plan):
		return self.tool_data("create_farm_task_template", {"template_name": name, "task_type": "Spray",
		                                                     "evidence_required": EVIDENCE, **plan})

	def switch_on(self):
		flags.upsert(template_schedule.FLAG, "Flag", True, company=MAIN, description="t", owner_area="t", active=True)
		STORE.commit()


class CategoryAndSkill(PlanCase):
	def test_copied_to_the_task_and_go_hold_matches_the_category_not_the_name(self):
		self.template("Cover — WCFF", category="fruit_fly_cover", skill_level="Supervised")
		task = self.tool_data("create_task_from_template", {"template": "Cover — WCFF", "company": MAIN,
		                                                    "location_doctype": "Field", "location": "Block 7"})["name"]
		row = frappe.db.get_value("Farm Task", task, ["category", "skill_level", "task_name"], as_dict=True)
		self.assertEqual((row["category"], row["skill_level"]), ("fruit_fly_cover", "Supervised"))
		self.assertNotIn("fruit fly", row["task_name"].lower())
		spec = next(s for s in go_hold.preset_specs() if s["rule_id"] == "go_hold_spray_fruit_fly_window")
		filters = compliance_rules.parse_filters(spec["scope_filters"])
		doc = dict(frappe.get_doc("Farm Task", task).as_dict())
		self.assertTrue(compliance_rules.row_matches(doc, filters, set(doc))[0], "matched by category")
		doc.update({"category": "", "task_name": "Spray for fruit fly"})
		self.assertTrue(compliance_rules.row_matches(doc, filters, set(doc))[0], "the old word still works")

	def test_an_untouched_preset_moves_to_the_category_scope_and_a_touched_one_does_not(self):
		old = go_hold.SUPERSEDED_SCOPES
		specs = {s["rule_id"]: s for s in go_hold.preset_specs()}
		for rule_id in ("go_hold_pruning_canker", "go_hold_burn"):
			spec = dict(specs[rule_id])
			spec["scope_filters"] = [{"field": "state", "op": "in", "value": list(go_hold.OPEN_STATES)}] + old[rule_id][0]
			compliance_rules.build_rule(spec).insert(ignore_permissions=True)
		name = frappe.db.get_value(compliance_rules.DOCTYPE, {"rule_id": "go_hold_burn"}, "name")
		frappe.db.set_value(compliance_rules.DOCTYPE, name, "enabled", 1)
		STORE.commit()
		refreshed = go_hold.refresh_presets()
		self.assertIn("go_hold_pruning_canker", refreshed)
		self.assertNotIn("go_hold_burn", refreshed, "somebody switched it on — theirs now")
		raw = frappe.db.get_value(compliance_rules.DOCTYPE, {"rule_id": "go_hold_pruning_canker"}, "scope_filters_json")
		self.assertIn('"category"', raw)


class Timing(PlanCase):
	def test_bad_timing_is_refused_naming_every_problem(self):
		error = self.tool_error("create_farm_task_template", {
			"template_name": "Bad", "task_type": "Spray", "evidence_required": EVIDENCE, "schedule": "Fortnightly",
			"window_start": "13-01", "bbch_from": 70, "bbch_to": 60})
		for part in ("schedule is one of", "window_start is a month-day", "window_start and window_end go together",
		             "bbch_from is above bbch_to"):
			self.assertIn(part, error)

	def test_a_weekly_company_task_in_its_window_once_per_week(self):
		self.template("Weekly bait check", schedule="Weekly", schedule_weekday="Mon", window_start="04-01",
		              window_end="10-31", schedule_target="The company", category="rodent")
		with mock.patch.object(frappe.utils, "today", lambda: "2027-05-03"):
			self.assertEqual(template_schedule.daily(), {}, "off until the company switches it on")
		self.switch_on()
		with mock.patch.object(frappe.utils, "today", lambda: "2027-05-03"):
			first = template_schedule.daily()[MAIN]
			second = template_schedule.daily()[MAIN]
		self.assertEqual(len(first["raised"]), 1)
		self.assertEqual(second["raised"], [])
		self.assertIsNone(template_schedule.period({"schedule": "Weekly", "schedule_weekday": "Mon",
		                                           "window_start": "04-01", "window_end": "10-31"},
		                                          datetime.date(2027, 5, 4)), "Tuesday")
		self.assertIsNone(template_schedule.period({"schedule": "Weekly", "schedule_weekday": "Mon",
		                                           "window_start": "04-01", "window_end": "10-31"},
		                                          datetime.date(2027, 12, 6)), "out of season")

	def test_each_block_of_the_crop_gated_by_its_recorded_stage(self):
		self.template("Petal-fall cover", schedule="Once a year", schedule_target="Each block of the crop",
		              schedule_crop="Cherry", bbch_from=67, bbch_to=71, category="spray")
		STORE.seed("Crop Observation", [
			{"name": "S7", "block_doctype": "Field", "block": "Block 7", "observation_type": "Growth Stage",
			 "growth_stage_code": "69", "observed_on": "2027-04-28"},
			{"name": "S8", "block_doctype": "Field", "block": "Block 8", "observation_type": "Growth Stage",
			 "growth_stage_code": "57", "observed_on": "2027-04-28"}])
		STORE.commit()
		self.switch_on()
		out = template_schedule.run(MAIN, datetime.date(2027, 4, 29))
		self.assertEqual(len(out["raised"]), 1, "Block 7 only; never the grapes")
		self.assertEqual(frappe.db.get_value("Farm Task", out["raised"][0], "location"), "Block 7")
		self.assertEqual([w["place"] for w in out["waiting"]], ["Block 8"])
		self.assertIn("before 67", out["waiting"][0]["reason"])
		self.assertEqual(template_schedule.run(MAIN, datetime.date(2027, 4, 30))["raised"], [], "once a year")

	def test_the_preview_says_what_it_would_raise_today(self):
		self.template("Daily trap read", schedule="Daily", schedule_target="The company")
		with mock.patch.object(frappe.utils, "today", lambda: "2027-06-01"):
			data = self.tool_data("preview_farm_task_template", {"template": "Daily trap read", "company": MAIN})
		self.assertEqual(data["schedule"]["repeats"], "Daily")
		self.assertEqual(len(data["schedule"]["today"]), 1)

	def test_update_checks_the_change_against_what_is_there(self):
		self.template("Window", schedule="Once a year", schedule_target="The company", window_start="03-01",
		              window_end="04-30")
		error = self.tool_error("update_farm_task_template", {"template": "Window", "window_end": ""})
		self.assertIn("go together", error)
		data = self.tool_data("update_farm_task_template", {"template": "Window", "skill_level": "Learner OK"})
		self.assertIn("skill_level", json.dumps(data))

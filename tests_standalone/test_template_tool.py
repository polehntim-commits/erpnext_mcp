# SPDX-License-Identifier: MIT
"""One tool for every template kind. v0.279.0 (consolidation phase 1).

`template` lists and describes every kind; each action runs the kind's existing tool under that tool's own switch
(so nothing widens); farm task, training type and the config kinds are built, edited, previewed, forked, versioned;
an AI-drafted config version is still published only in the Desk; and the old per-kind tools are untouched.
"""

import frappe

from erpnext_mcp import job_links, phone_config, seasonal, task_templates

from .fixtures import V12TestCase
from .harness import STORE

EVIDENCE = {"photos": True}
WRITES = ("create_farm_task_template", "update_farm_task_template", "create_training_type", "update_training_type",
          "deactivate_training_type", "draft_config", "publish_config")


class TemplateToolCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, allow_template=1, **{f"allow_{n}": 1 for n in WRITES})
		task_templates.seed_farm_task_templates()
		job_links.seed()
		seasonal.seed()
		STORE.commit()

	def t(self, **args):
		return self.tool_data("template", args)


class Discovery(TemplateToolCase):
	def test_every_kind_listed_and_described_from_the_existing_schemas(self):
		kinds = {k["kind"]: k for k in self.t(action="kinds")["kinds"]}
		for kind in ("farm_task", "inspection", "training_type", "trade_document", "reporting", "asset_type", "wizard",
		             "job_template", "seasonal_program", "market_commodity", "supplier_connector", "input_plan",
		             "pollination_plan"):
			self.assertIn(kind, kinds)
		farm = self.t(action="describe", kind="farm_task")
		self.assertIn("template_name", farm["body"]["properties"])
		self.assertIn("schedule", farm["body"]["properties"], "v0.278.0's fields arrive without a code change")
		self.assertEqual(farm["calls"]["create"], "create_farm_task_template")
		job = self.t(action="describe", kind="job_template")
		self.assertIn("link_days", job["body"]["fields"])
		self.assertIn("fork", job["actions"])


class TheSwitchesStillDecide(TemplateToolCase):
	def test_a_write_needs_the_kinds_own_switch_too(self):
		self.configure(enabled=1, allow_template=1, **{f"allow_{n}": 1 for n in WRITES if n != "create_farm_task_template"})
		error = self.tool_error("template", {"action": "create", "kind": "farm_task",
		                                     "body": {"template_name": "X", "task_type": "Spray", "evidence_required": EVIDENCE}})
		self.assertIn("allow_create_farm_task_template", error)
		self.assertFalse(frappe.db.exists("Farm Task Template", {"template_name": "X"}))

	def test_the_tool_itself_is_off_by_default(self):
		self.configure(allow_template=0)
		self.assertIn("allow_template", self.tool_error("template", {"action": "kinds"}))


class FarmTaskThroughOneTool(TemplateToolCase):
	def test_create_get_update_preview_disable_fork_history(self):
		self.t(action="create", kind="farm_task", body={"template_name": "Petal-fall cover", "task_type": "Spray",
		                                                "evidence_required": EVIDENCE, "category": "spray"})
		got = self.t(action="get", kind="farm_task", name="Petal-fall cover")
		self.assertIn("Petal-fall cover", str(got))
		self.t(action="update", kind="farm_task", name="Petal-fall cover", body={"skill_level": "Supervised"})
		row = frappe.db.get_value("Farm Task Template", {"template_name": "Petal-fall cover"},
		                          ["skill_level", "enabled", "name"], as_dict=True)
		self.assertEqual(row["skill_level"], "Supervised")
		preview = self.t(action="preview", kind="farm_task", body={"template_name": "Draft walk", "task_type": "Inspection",
		                                                            "evidence_required": EVIDENCE})
		self.assertIn("form", preview)
		self.t(action="disable", kind="farm_task", name="Petal-fall cover")
		self.assertFalse(frappe.db.get_value("Farm Task Template", row["name"], "enabled"))
		forked = self.t(action="fork", kind="farm_task", name="Petal-fall cover", new_name="Shuck-fall cover",
		                overrides={"category": "spray"})
		self.assertEqual(forked["forked_from"], "Petal-fall cover")
		copy = frappe.db.get_value("Farm Task Template", {"template_name": "Shuck-fall cover"},
		                           ["task_type", "skill_level"], as_dict=True)
		self.assertEqual((copy["task_type"], copy["skill_level"]), ("Spray", "Supervised"))
		self.assertEqual(self.t(action="history", kind="farm_task", name="Petal-fall cover")["name"], "Petal-fall cover")

	def test_register_only_kinds_and_a_disable_with_its_reason(self):
		self.t(action="create", kind="training_type", body={"name": "Ladder safety"})
		listed = self.t(action="list", kind="training_type")
		self.assertIn("Ladder safety", str(listed))
		self.t(action="disable", kind="training_type", name="Ladder safety", reason="folded into WPS")
		self.assertIn("does not support", self.tool_error("template", {"action": "publish", "kind": "training_type",
		                                                               "name": "Ladder safety"}).replace(
			"is a register, not versioned config", "does not support"))


class ConfigKindsThroughOneTool(TemplateToolCase):
	def test_draft_list_preview_fork_and_the_desk_still_publishes_ai_drafts(self):
		body = dict(phone_config.body_of(phone_config.doc_of(job_links.KIND, "supplier_delivery",
		                                                     status=phone_config.PUBLISHED)))
		forked = self.t(action="fork", kind="job_template", name="supplier_delivery", new_name="fuel_delivery",
		                overrides={"default_scope": "Fill the tanks at the shop."})
		self.assertEqual(forked["forked_from"], "supplier_delivery")
		self.assertEqual(phone_config.rows(job_links.KIND, "fuel_delivery")[0]["status"], "Draft")
		bad = dict(body, link_days=0)
		report = self.t(action="preview", kind="job_template", name="fuel_delivery", body=bad)
		self.assertTrue(any("link_days" in e for e in report["errors"]))
		history = self.t(action="history", kind="job_template", name="fuel_delivery")
		self.assertIn("fuel_delivery", str(history))
		error = self.tool_error("template", {"action": "publish", "kind": "job_template", "name": "fuel_delivery",
		                                     "version": 1, "change_note": "try"})
		self.assertTrue("Desk" in error or "AI" in error, error)

	def test_seasonal_program_is_editable_and_diffable(self):
		program = dict(phone_config.body_of(phone_config.doc_of(seasonal.KIND, seasonal.FALL)))
		program["trigger"] = {**program["trigger"], "fixed_mmdd": "10-20"}
		self.t(action="update", kind="seasonal_program", name=seasonal.FALL, body=program)
		got = self.t(action="get", kind="seasonal_program", name=seasonal.FALL)
		self.assertEqual(got["config"]["body"]["trigger"]["fixed_mmdd"], "10-20", "a draft is edited in place")
		self.t(action="fork", kind="seasonal_program", name=seasonal.FALL, new_name="fall_winterize_hill")
		diff = self.t(action="diff", kind="seasonal_program", name="fall_winterize_hill", from_version=1, to_version=1)
		self.assertEqual(diff["changed"], 0)


class TheOldToolsAreUntouched(TemplateToolCase):
	def test_still_listed_and_still_working(self):
		from erpnext_mcp import registry

		for name in ("create_farm_task_template", "list_inspection_templates", "create_training_type", "draft_config"):
			self.assertIn(name, registry.TOOLS)
		self.assertTrue(self.tool_data("create_farm_task_template", {"template_name": "Direct", "task_type": "Repair",
		                                                              "evidence_required": EVIDENCE}))

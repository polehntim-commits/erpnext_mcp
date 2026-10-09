# SPDX-License-Identifier: MIT
"""One tool for every config kind. v0.280.0 (consolidation phase 2).

`config` lists and describes every kind with its rules; each action runs the existing lifecycle tool under its own
switch, so the kinds' rules hold (AI drafts published only by a person, payroll only in the Desk); the old tools are
untouched.
"""

from erpnext_mcp import job_links, phone_config

from .fixtures import V12TestCase
from .harness import STORE

ON = ("draft_config", "stage_config", "publish_config", "rollback_config")


class ConfigToolCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, allow_config=1, **{f"allow_{n}": 1 for n in ON})
		job_links.seed()
		STORE.commit()

	def c(self, **args):
		return self.tool_data("config", args)


class Discovery(ConfigToolCase):
	def test_every_kind_with_its_rules_and_a_shape(self):
		kinds = {k["kind"]: k for k in self.c(action="kinds")["kinds"]}
		for kind in ("wizard", "compliance_rule", "overtime_rule", "pest_dd_models", "job_template", "input_plan"):
			self.assertIn(kind, kinds)
		self.assertTrue(any("Desk" in r for r in kinds["overtime_rule"]["rules"]))
		job = self.c(action="describe", kind="job_template")
		self.assertIn("link_days", job["fields"])
		self.assertIn("body", job["arguments"]["draft"])


class TheLifecycleRulesHold(ConfigToolCase):
	def test_draft_list_get_diff_and_an_ai_draft_is_published_by_a_person(self):
		body = dict(phone_config.body_of(phone_config.doc_of(job_links.KIND, "supplier_pickup",
		                                                     status=phone_config.PUBLISHED)))
		body["link_days"] = 21
		drafted = self.c(action="draft", kind="job_template", name="supplier_pickup", body=body)
		self.assertEqual(drafted["authored_by"], "AI-proposed")
		listed = self.c(action="list", kind="job_template", name="supplier_pickup")
		self.assertIn("supplier_pickup", str(listed))
		diff = self.c(action="diff", kind="job_template", name="supplier_pickup", from_version=1, to_version=2)
		self.assertIn("link_days", str(diff["changes"]))
		error = self.tool_error("config", {"action": "publish", "kind": "job_template", "name": "supplier_pickup",
		                                   "version": 2, "change_note": "longer"})
		self.assertTrue("Desk" in error or "AI" in error, error)

	def test_a_write_needs_its_own_switch_too(self):
		self.configure(enabled=1, allow_config=1)
		self.assertIn("allow_draft_config", self.tool_error("config", {"action": "draft", "kind": "job_template",
		                                                              "name": "x", "body": {}}))

	def test_off_by_default_and_the_old_tools_stay(self):
		from erpnext_mcp import registry

		self.configure(enabled=1)
		self.assertIn("allow_config", self.tool_error("config", {"action": "kinds"}))
		for name in ("list_configs", "draft_config", "publish_config", "rollback_config"):
			self.assertIn(name, registry.TOOLS)

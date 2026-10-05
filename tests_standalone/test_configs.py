"""One config lifecycle, read side. v0.234.0."""

import frappe

from erpnext_mcp import compliance_rules

from .test_alerts import TODAY
from .test_alerts import ALL_ON
from .test_compliance_rule_engine import RULE_TOOLS, RuleEngineTestCase

CONFIG_READS = {f"allow_{t}": 1 for t in ("list_configs", "get_config", "diff_config", "preview_config")}


class TheGenericReads(RuleEngineTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **{**ALL_ON, **RULE_TOOLS, **CONFIG_READS})
		spec = {
			"rule_id": "policy_tree",
			"title": "Policy under review",
			"category": "Records",
			"target_doctype": "Compliance Policy",
			"kairotic_gate_description": "A tree over the policy.",
			"regimes": ["Internal"],
			"enabled": 1,
			"condition_tree": {"id": "far_off", "path": "record.review_due_date", "op": "gt", "value": "2099-01-01"},
			"human_approved_by": "Administrator",
			"human_approved_on": frappe.utils.now(),
		}
		doc = compliance_rules.build_rule(spec)
		doc.insert(ignore_permissions=True)
		self.rule = doc.name

	def test_an_unknown_kind_is_refused(self):
		self.assertIn("kind is one of", self.tool_error("list_configs", {"kind": "spreadsheet"}))

	def test_list_and_get_route_to_the_rule_tools(self):
		listed = self.tool_data("list_configs", {"kind": "compliance_rule"})
		self.assertEqual(listed["store"], "Compliance Rule")
		got = self.tool_data("get_config", {"kind": "compliance_rule", "key": "policy_tree"})
		self.assertEqual(got["config"]["rule_id"], "policy_tree")

	def test_a_patch_is_previewed_over_days_without_being_saved(self):
		self.a_policy(review_in_days=10)
		data = self.tool_data(
			"preview_config",
			{
				"kind": "compliance_rule",
				"key": "policy_tree",
				"as_of": TODAY,
				"days": 3,
				"compare_to": "live",
				"patch": {"condition_tree": {"path": "record.review_due_date", "op": "gt", "value": "2000-01-01"}},
			},
		)["preview"]
		self.assertEqual([d["observed"] for d in data["days"]], [0, 0, 0], "the patched tree passes, so nothing")
		self.assertEqual([d["live_observed"] for d in data["days"]], [1, 1, 1], "the live tree still fails")
		self.assertEqual(data["patched"], ["condition_tree"])
		live = compliance_rules.rule_row(self.rule)
		self.assertIn("2099-01-01", live["condition_tree_json"], "nothing was written")

	def test_a_bad_patch_is_refused(self):
		error = self.tool_error(
			"preview_config",
			{"kind": "compliance_rule", "key": "policy_tree", "patch": {"condition_tree": {"path": "nope.x", "op": "eq", "value": 1}}},
		)
		self.assertIn("no provider 'nope'", error)

	def test_diff_shows_what_a_new_version_changed(self):
		self.tool_data(
			"update_compliance_rule",
			{"name": self.rule, "title": "Policy under review (v2)", "reason": "Retitled for the calendar view."},
		)
		data = self.tool_data("diff_config", {"kind": "compliance_rule", "key": "policy_tree", "from_version": 1})
		self.assertIn("title", data["changes"])
		self.assertEqual(data["changes"]["title"]["to"], "Policy under review (v2)")

"""One config lifecycle, write side, and decision 5. v0.234.1."""

from erpnext_mcp import business_cards, compliance_rules, config_lifecycle

from .harness import set_roles
from .test_alerts import ALL_ON
from .test_compliance_rule_engine import RULE_TOOLS, RuleEngineTestCase

CONFIG_TOOLS = {f"allow_{t}": 1 for t in ("list_configs", "get_config", "diff_config", "preview_config",
                                          "draft_config", "stage_config", "publish_config", "rollback_config")}


class AnAIDraftGoesLiveOnlyInTheDesk(RuleEngineTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **{**ALL_ON, **RULE_TOOLS, **CONFIG_TOOLS})

	def draft(self):
		return self.tool_data(
			"draft_config",
			{
				"kind": "trigger_rule",
				"key": "pruning_window",
				"fields": {
					"title": "Pruning window",
					"target_doctype": "Farm Task",
					"kairotic_gate_description": "Open pruning tasks outside the dormant window.",
					"ai_source_citation": "PNW Plant Disease Management Handbook, cherry bacterial canker",
					"condition_tree": {"id": "in_season", "path": "calendar.mmdd", "op": "between", "value": ["01-01", "03-15"]},
				},
			},
		)

	def test_a_draft_over_mcp_is_ai_proposed_and_off(self):
		data = self.draft()
		self.assertEqual(data["authored_by"], "AI-proposed")
		live = [r for r in compliance_rules.rule_rows(include_inactive=True) if r.get("rule_id") == "pruning_window"]
		self.assertEqual(len(live), 1)
		self.assertEqual(live[0].get("category"), "Work Timing")
		self.assertFalse(int(live[0].get("enabled") or 0))

	def test_publishing_it_over_mcp_is_refused_by_both_doors(self):
		self.draft()
		for tool, args in (("publish_config", {"kind": "trigger_rule", "key": "pruning_window"}),
		                   ("approve_compliance_rule", {"name": "pruning_window"})):
			with self.subTest(tool=tool):
				self.assertIn("decision 5", self.tool_error(tool, args))

	def test_the_desk_button_publishes_it_as_the_person_clicking(self):
		self.draft()
		name = compliance_rules.resolve("pruning_window")
		set_roles("Administrator", ["System Manager"])
		config_lifecycle.approve_rule(name)
		self.assertTrue(int(compliance_rules.rule_row(name).get("enabled") or 0))

	def test_a_kind_off_the_allow_list_is_refused(self):
		self.configure(enabled=1, **{**ALL_ON, **RULE_TOOLS, **CONFIG_TOOLS, "config_draft_kinds": "wizard\ntile"})
		self.assertIn("not allowed for trigger_rule", self.tool_error("draft_config", {"kind": "trigger_rule", "key": "x", "fields": {}}))

	def test_rollback_to_none_switches_a_rule_off_with_the_reason(self):
		self.draft()
		name = compliance_rules.resolve("pruning_window")
		set_roles("Administrator", ["System Manager"])
		config_lifecycle.approve_rule(name)
		self.tool_data("rollback_config", {"kind": "trigger_rule", "key": "pruning_window", "to": "none",
		                                  "change_note": "Season over; switching it off."})
		self.assertFalse(int(compliance_rules.rule_row(name).get("enabled") or 0))


class ForemanAndUpScanCards(RuleEngineTestCase):
	def test_foreman_yes_crew_leader_no(self):
		set_roles("foreman@farm.test", ["Foreman"])
		business_cards.require_role("foreman@farm.test")
		set_roles("lead@farm.test", ["Crew Leader"])
		with self.assertRaises(Exception) as caught:
			business_cards.require_role("lead@farm.test")
		self.assertIn("is restricted to", str(caught.exception))

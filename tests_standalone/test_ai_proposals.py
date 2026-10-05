# SPDX-License-Identifier: MIT
"""AI-proposed compliance rules and inspection templates — v0.37.0.

THE CLAIM IS NARROW AND THE TESTS ARE ABOUT THE EDGES OF IT. A model may draft a
compliance rule or an inspection form; it may not turn one on, sign one, describe
itself as somebody else, stand a running rule down, or slip a program past a
reviewer. Everything below is an attempt to break one of those.

WHAT IS NOT TESTED HERE, BECAUSE IT DOES NOT EXIST: a model call. These tools
take structured arguments and write a record. The proposer is the MCP client, so
what is testable — and what matters — is the shape of what lands and what the
tool refuses to write, which is exactly what a validator-and-gate should be
judged on.

`ApprovingIsWhereTheTeethAre` is the class to read if you only read one. A draft
carrying `custom_python` cannot be approved by a caller who has not said, in an
argument named for the thing, that they read the program. That is the difference
between a review and a click, and it is the only defence against the failure this
whole release is shaped around: a plausible program, from a model, in a field
that runs.
"""

import json

import frappe

from erpnext_mcp import compliance_rules, proposals, sessions

from .harness import STORE
from .test_alerts import ALL_ON, days_from_today
from .test_compliance_rule_engine import RULE_TOOLS, RuleEngineTestCase
from .test_inspection_templates import ALL_ON as TEMPLATE_TOOLS
from .test_inspection_templates import THREE_SECTIONS, SessionTestCase

PROPOSAL_TOOLS = {
	f"allow_{name}": 1
	for name in ("propose_compliance_rule", "approve_inspection_template", "list_inspection_templates")
}

#: A declarative draft of the shape a proposer should be producing: every
#: condition a field, nothing a program. Used wherever the test is about the
#: rails rather than about the flags.
A_DRAFT = {
	"rule_id": "policy_signature_missing",
	"title": "Policy adopted with nobody's signature on it",
	"category": "Policies",
	"target_doctype": "Compliance Policy",
	"date_field": "review_due_date",
	"cadence_days": 365,
	"kairotic_gate_description": (
		"Ripe when a policy is past its review date. Quiet while the review date is in the future, "
		"and quiet for ever on a policy with no review date at all."
	),
	"message_template": "{{ name }} is past review",
	"regimes": ["OR-OSHA"],
	"regulation_url": "https://osha.oregon.gov/OSHARules/div4/div4.pdf",
	"regulation_section": "OAR 437-004-1131(3)",
}


#: A drafted form of the shape a proposer should be producing: two sections,
#: each asking for something a worker can actually come back with.
A_TEMPLATE_DRAFT = {
	"template_name": "Heat Illness Readiness",
	"description": "The shade, water and rest check on a hot afternoon.",
	"applies_to_asset_type": "General",
	"regulation_url": "https://osha.oregon.gov/OSHARules/div4/div4.pdf",
	"regulation_section": "OAR 437-004-1131(6)",
	"sections": [
		{
			"section_name": "Shade",
			"renderer_hint": "photo",
			"required": True,
			"evidence_contract": {"photos": True},
		},
		{
			"section_name": "Water",
			"renderer_hint": "checklist",
			"required": True,
			"evidence_contract": {"checklist_items": ["water_within_reach"]},
		},
	],
}


class ProposalTestCase(RuleEngineTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **{**ALL_ON, **RULE_TOOLS, **PROPOSAL_TOOLS})

	def propose(self, **overrides):
		payload = {**A_DRAFT, **overrides}
		return self.tool_data("propose_compliance_rule", payload)

	def rule_row(self, name):
		return compliance_rules.rule_row(name)


# ── 1 ───────────────────────────────────────────────────────────────────────
class TheRailsARuleProposalArrivesOn(ProposalTestCase):
	def test_it_lands_disabled(self):
		"""The whole surface is only safe to expose because of this line."""
		data = self.propose()
		self.assertFalse(data["enabled"])
		self.assertIsNone(data["human_approved_by"])
		self.assertIsNone(data["human_approved_on"])
		self.assertFalse(compliance_rules.rule_row(data["name"])["active_row_flag"])

	def test_a_draft_asking_to_be_enabled_is_still_disabled(self):
		"""`enabled` is not an argument this tool honours, whatever is passed —
		the gate is a property, not a default."""
		data = self.propose(enabled=True, active_row_flag=1)
		self.assertFalse(data["enabled"])

	def test_it_says_a_model_wrote_it(self):
		data = self.propose()
		self.assertEqual(data["authored_by"], compliance_rules.AUTHOR_AI)

	def test_it_will_not_call_itself_operator_authored(self):
		"""Refused rather than quietly corrected: that argument is an attempt to
		launder provenance, and silently fixing it teaches the caller nothing."""
		message = self.tool_error("propose_compliance_rule", {**A_DRAFT, "authored_by": "Operator"})
		self.assertIn("will not write", message)
		self.assertIn("create_compliance_rule", message)
		self.assertFalse(compliance_rules.resolve(A_DRAFT["rule_id"]))

	def test_the_citation_carries_the_section_and_the_url(self):
		citation = self.propose()["definition"]["ai_source_citation"]
		self.assertIn("OAR 437-004-1131(3)", citation)
		self.assertIn("osha.oregon.gov", citation)

	def test_it_quotes_the_regulation_it_read(self):
		text = "The employer shall ensure that each employee is provided shade when the heat index is 80."
		citation = self.propose(regulation_text=text)["definition"]["ai_source_citation"]
		self.assertIn("provided shade", citation)

	def test_a_proposal_with_no_source_at_all_is_refused(self):
		payload = {key: value for key, value in A_DRAFT.items() if not key.startswith("regulation_")}
		message = self.tool_error("propose_compliance_rule", payload)
		self.assertIn("where it was read from", message)
		self.assertIn("Nothing was written", message)
		self.assertFalse(compliance_rules.resolve(A_DRAFT["rule_id"]))

	def test_a_citation_written_out_by_hand_is_accepted(self):
		payload = {key: value for key, value in A_DRAFT.items() if not key.startswith("regulation_")}
		payload["ai_source_citation"] = "29 CFR 1910.142(b)(3), read from the printed CFR"
		self.assertIn(
			"1910.142", self.tool_data("propose_compliance_rule", payload)["definition"]["ai_source_citation"]
		)

	def test_it_cannot_sign_its_own_approval(self):
		for field in ("human_approved_by", "human_approved_on", "approver_employee"):
			with self.subTest(field=field):
				message = self.tool_error(
					"propose_compliance_rule", {**A_DRAFT, field: "somebody@example.com"}
				)
				self.assertIn(field, message)
				self.assertIn("approve_compliance_rule", message)

	def test_a_target_doctype_this_site_has_not_got_is_refused(self):
		"""Same refusal create_compliance_rule makes, at the same door: a rule
		scanning nothing scans nothing quietly, for ever."""
		message = self.tool_error(
			"propose_compliance_rule", {**A_DRAFT, "target_doctype": "Widget Inspection"}
		)
		self.assertIn("no DocType called", message)

	def test_the_sandbox_still_refuses_what_it_refuses(self):
		message = self.tool_error(
			"propose_compliance_rule", {**A_DRAFT, "custom_python": "import os\nrows = []"}
		)
		self.assertIn("sandbox refused", message)
		self.assertFalse(compliance_rules.resolve(A_DRAFT["rule_id"]))

	def test_the_proposal_is_in_the_action_log_with_its_arguments(self):
		self.propose()
		logged = [
			row for row in STORE.rows("MCP Action Log") if row.get("tool_name") == "propose_compliance_rule"
		]
		self.assertTrue(logged)
		self.assertIn("policy_signature_missing", json.dumps(logged[-1].get("arguments_json") or ""))

	def test_the_tool_is_off_until_an_operator_turns_it_on(self):
		self.configure(enabled=1, **{**ALL_ON, **RULE_TOOLS, "allow_propose_compliance_rule": 0})
		self.assertIn("switched off", self.tool_error("propose_compliance_rule", A_DRAFT))


# ── 2 ───────────────────────────────────────────────────────────────────────
class TheReviewFlags(ProposalTestCase):
	def test_a_program_a_model_wrote_is_flagged(self):
		data = self.propose(custom_python="rows = []\nfor row in policies:\n\trows.append(row)")
		self.assertIn(proposals.FLAG_CUSTOM_PYTHON, data["ai_review_flags"])
		self.assertEqual(data["shape"], compliance_rules.SHAPE_CUSTOM)

	def test_the_flag_comes_with_the_sentence_saying_why(self):
		data = self.propose(custom_python="rows = []")
		note = next(entry for entry in data["review_flags"] if entry["flag"] == proposals.FLAG_CUSTOM_PYTHON)
		self.assertIn("right question", note["note"])

	def test_an_assignee_expression_is_flagged_too(self):
		"""Same sandbox, same reading job: an expression that resolves to nobody
		is a task that lands on nobody."""
		data = self.propose(producer_assigned_to_expression="row.foreman", producer_skill_required="")
		self.assertIn(proposals.FLAG_PRODUCER_EXPRESSION, data["ai_review_flags"])

	def test_a_draft_naming_no_regime_says_so(self):
		"""Not a refusal — a rule nobody outside is coming to inspect is a real
		answer — but it is one the approver should have to look at, because a rule
		tagged with nothing appears in no regime-filtered sweep and no packet."""
		payload = {key: value for key, value in A_DRAFT.items() if key != "regimes"}
		data = self.tool_data("propose_compliance_rule", payload)
		self.assertIn(proposals.FLAG_NO_REGIMES, data["ai_review_flags"])

	def test_a_clean_declarative_draft_carries_no_flags(self):
		self.assertEqual(self.propose()["ai_review_flags"], [])

	def test_the_flags_are_on_the_row_and_not_only_in_the_answer(self):
		"""The approver reads the record, not the tool result from an hour ago."""
		name = self.propose(custom_python="rows = []")["name"]
		stored = frappe.db.get_value(compliance_rules.DOCTYPE, name, "ai_review_flags")
		self.assertEqual(proposals.read_flags(stored), [proposals.FLAG_CUSTOM_PYTHON])

	def test_superseding_a_flagged_draft_does_not_launder_it(self):
		"""update_compliance_rule writes a new row; if the flags did not travel
		with it, an edit would be a way round the acknowledgement."""
		name = self.propose(custom_python="rows = []")["name"]
		data = self.tool_data(
			"update_compliance_rule",
			{"name": name, "title": "A better title", "reason": "clearer on the calendar"},
		)
		self.assertIn(proposals.FLAG_CUSTOM_PYTHON, data["ai_review_flags"])
		self.assertEqual(data["authored_by"], compliance_rules.AUTHOR_AI)


# ── 3 ───────────────────────────────────────────────────────────────────────
class ApprovingIsWhereTheTeethAre(ProposalTestCase):

	def setUp(self):
		super().setUp()
		# v0.234.1 (Tim's decision 5): an AI-proposed version is published only in the Desk;
		# these tests are about what publishing does, so they publish as the Desk does.
		from erpnext_mcp import config_lifecycle

		desk = config_lifecycle.desk_action()
		desk.__enter__()
		self.addCleanup(desk.__exit__, None, None, None)
	def test_a_clean_draft_approves_in_one_call(self):
		name = self.propose()["name"]
		data = self.tool_data("approve_compliance_rule", {"name": name})
		self.assertTrue(data["enabled"])
		self.assertTrue(data["human_approved_by"])

	def test_approval_does_not_rewrite_who_authored_it(self):
		"""What approval adds is a name, not a change of authorship. A rule that
		read `Operator` after approval would erase the fact worth keeping."""
		name = self.propose()["name"]
		self.assertEqual(
			self.tool_data("approve_compliance_rule", {"name": name})["authored_by"],
			compliance_rules.AUTHOR_AI,
		)

	def test_a_draft_carrying_a_program_cannot_be_approved_by_clicking(self):
		name = self.propose(custom_python="rows = []")["name"]
		message = self.tool_error("approve_compliance_rule", {"name": name})
		self.assertIn("accept_ai_authored_code", message)
		self.assertFalse(compliance_rules.rule_row(name)["enabled"])

	def test_the_refusal_prints_the_program_back(self):
		"""An acknowledgement of code nobody displayed is not one."""
		name = self.propose(custom_python="rows = []\nobserved = len(rows)")["name"]
		message = self.tool_error("approve_compliance_rule", {"name": name})
		self.assertIn("observed = len(rows)", message)

	def test_acknowledging_it_lets_the_approval_through(self):
		name = self.propose(custom_python="rows = []")["name"]
		data = self.tool_data("approve_compliance_rule", {"name": name, "accept_ai_authored_code": True})
		self.assertTrue(data["enabled"])
		self.assertEqual(data["acknowledged_review_flags"], [proposals.FLAG_CUSTOM_PYTHON])

	def test_an_operator_authored_program_is_not_gated_by_this(self):
		"""The gate is about code a MODEL wrote. A rule somebody typed here was
		already read by the person typing it, and gating that too would train
		everybody to pass the argument every time — which is how a gate stops
		being one."""
		self.tool_data(
			"create_compliance_rule",
			{
				**{key: value for key, value in A_DRAFT.items() if not key.startswith("regulation_")},
				"rule_id": "typed_by_a_person",
				"custom_python": "rows = []",
			},
		)
		self.assertTrue(self.tool_data("approve_compliance_rule", {"name": "typed_by_a_person"})["enabled"])


# ── 4 ───────────────────────────────────────────────────────────────────────
class ProposingAChangeToARuleThatIsRunning(ProposalTestCase):

	def setUp(self):
		super().setUp()
		# v0.234.1 (Tim's decision 5): an AI-proposed version is published only in the Desk;
		# these tests are about what publishing does, so they publish as the Desk does.
		from erpnext_mcp import config_lifecycle

		desk = config_lifecycle.desk_action()
		desk.__enter__()
		self.addCleanup(desk.__exit__, None, None, None)
	def a_live_rule(self) -> dict:
		self.seed_rules()
		return compliance_rules.rule_row(compliance_rules.resolve("policy_review_overdue"))

	def test_the_draft_is_written_at_the_next_version(self):
		live = self.a_live_rule()
		data = self.propose(rule_id="policy_review_overdue", threshold_warning_days=45)
		self.assertEqual(data["version"], int(live["version"]) + 1)
		self.assertNotEqual(data["name"], live["name"])

	def test_the_running_rule_is_untouched(self):
		"""AN AI PROPOSAL STANDS NOTHING DOWN. This is the whole of the 'propose
		new or propose an update, never a delete or a disable' constraint."""
		live = self.a_live_rule()
		self.propose(rule_id="policy_review_overdue", threshold_warning_days=45)
		after = compliance_rules.rule_row(live["name"])
		self.assertTrue(after["enabled"])
		self.assertTrue(after["active_row_flag"])
		self.assertFalse(str(after["superseded_by"] or ""))

	def test_the_result_is_a_diff_rather_than_a_wall_of_draft(self):
		self.a_live_rule()
		data = self.propose(rule_id="policy_review_overdue", threshold_warning_days=45)
		self.assertIn("threshold_warning_days", data["changes"])
		self.assertEqual(data["changes"]["threshold_warning_days"]["after"], 45)
		self.assertNotIn("enabled", data["changes"])

	def test_it_is_flagged_as_a_replacement_for_something_live(self):
		self.a_live_rule()
		data = self.propose(rule_id="policy_review_overdue", threshold_warning_days=45)
		self.assertIn(proposals.FLAG_SUPERSEDES_LIVE_RULE, data["ai_review_flags"])
		self.assertTrue(data["supersedes_on_approval"])

	def test_approving_the_replacement_is_what_supersedes_the_old_one(self):
		live = self.a_live_rule()
		draft = self.propose(rule_id="policy_review_overdue", threshold_warning_days=45)["name"]
		data = self.tool_data("approve_compliance_rule", {"name": draft})
		self.assertEqual(data["supersedes"], live["name"])
		old = compliance_rules.rule_row(live["name"])
		self.assertFalse(old["enabled"])
		self.assertEqual(old["superseded_by"], draft)
		self.assertTrue(compliance_rules.rule_row(draft)["enabled"])

	def test_the_alerts_the_old_rule_raised_are_left_standing(self):
		"""Superseding a rule is not evidence that anybody did the work — the same
		reading `deactivate_compliance_rule` takes, for the same reason."""
		self.a_policy(review_in_days=-30)
		self.a_live_rule()
		self.sweep()
		standing = self.live("policy_review_overdue")
		self.assertTrue(standing)
		draft = self.propose(rule_id="policy_review_overdue", threshold_warning_days=45)["name"]
		self.tool_data("approve_compliance_rule", {"name": draft})
		self.assertEqual(
			[alert["name"] for alert in self.live("policy_review_overdue")],
			[alert["name"] for alert in standing],
		)

	def test_two_proposals_in_the_queue_do_not_share_a_version(self):
		self.a_live_rule()
		first = self.propose(rule_id="policy_review_overdue", threshold_warning_days=45)
		second = self.propose(rule_id="policy_review_overdue", threshold_warning_days=60)
		self.assertEqual(second["version"], first["version"] + 1)


# ── 5 ───────────────────────────────────────────────────────────────────────
class ProposingATemplate(SessionTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **{**TEMPLATE_TOOLS, **PROPOSAL_TOOLS})

	def propose(self, **overrides):
		return self.tool_data(
			"propose_inspection_template_from_regulation", {**A_TEMPLATE_DRAFT, **overrides}
		)

	def test_it_lands_inactive(self):
		"""No handset fetches it, and the rule engine cannot match it, until a
		person has read the sections against the regulation."""
		data = self.propose()
		self.assertFalse(data["active"])
		self.assertIsNone(data["human_approved_by"])
		self.assertNotIn(data["name"], self.tool_data("list_inspection_templates", {})["live_templates"])

	def test_it_says_a_model_wrote_it_and_where_it_read(self):
		data = self.propose()
		self.assertEqual(data["authored_by"], proposals.AUTHOR_AI)
		self.assertIn("OAR 437-004-1131(6)", data["ai_source_citation"])

	def test_it_will_not_call_itself_operator_authored(self):
		message = self.tool_error(
			"propose_inspection_template_from_regulation", {**A_TEMPLATE_DRAFT, "authored_by": "Operator"}
		)
		self.assertIn("create_inspection_template", message)

	def test_it_cannot_sign_its_own_approval(self):
		message = self.tool_error(
			"propose_inspection_template_from_regulation",
			{**A_TEMPLATE_DRAFT, "human_approved_by": "somebody@example.com"},
		)
		self.assertIn("approve_inspection_template", message)

	def test_a_proposal_with_no_source_is_refused(self):
		payload = {key: value for key, value in A_TEMPLATE_DRAFT.items() if not key.startswith("regulation_")}
		self.assertIn(
			"where it was read from",
			self.tool_error("propose_inspection_template_from_regulation", payload),
		)

	def test_a_section_asking_for_no_evidence_is_flagged(self):
		"""A section with an empty contract can be filed empty and still looks
		complete, which is the failure a template exists to prevent."""
		sections = [dict(A_TEMPLATE_DRAFT["sections"][0]), {"section_name": "Rest", "evidence_contract": {}}]
		data = self.propose(sections=sections)
		self.assertIn(proposals.FLAG_SECTIONS_WITHOUT_CONTRACT, data["ai_review_flags"])

	def test_a_complete_draft_carries_no_flags(self):
		self.assertEqual(self.propose()["ai_review_flags"], [])

	def test_the_controllers_refusals_still_apply(self):
		self.assertIn(
			"sections must be a non-empty list",
			self.tool_error(
				"propose_inspection_template_from_regulation", {**A_TEMPLATE_DRAFT, "sections": []}
			),
		)

	def test_the_seeded_templates_say_the_app_shipped_them(self):
		"""`AI-proposed` only means something if the other two words are said."""
		sessions.seed_inspection_templates()
		row = frappe.db.get_all(
			sessions.TEMPLATE_DOCTYPE,
			filters={"template_name": "Post-harvest Cabin Close-down"},
			fields=["authored_by"],
			limit=1,
		)
		self.assertEqual(row[0]["authored_by"], proposals.AUTHOR_SYSTEM)


# ── 6 ───────────────────────────────────────────────────────────────────────
class ApprovingATemplate(ProposingATemplate):

	def setUp(self):
		super().setUp()
		# v0.234.1 (Tim's decision 5): an AI-proposed version is published only in the Desk;
		# these tests are about what publishing does, so they publish as the Desk does.
		from erpnext_mcp import config_lifecycle

		desk = config_lifecycle.desk_action()
		desk.__enter__()
		self.addCleanup(desk.__exit__, None, None, None)
	def test_approval_activates_it_and_names_the_approver(self):
		name = self.propose()["name"]
		data = self.tool_data("approve_inspection_template", {"name": name})
		self.assertTrue(data["active"])
		self.assertTrue(data["human_approved_by"])
		self.assertEqual(data["authored_by"], proposals.AUTHOR_AI)

	def test_an_already_active_template_is_refused(self):
		name = self.propose()["name"]
		self.tool_data("approve_inspection_template", {"name": name})
		self.assertIn("already active", self.tool_error("approve_inspection_template", {"name": name}))

	def test_a_draft_against_a_live_name_leaves_the_live_one_working(self):
		live = self.a_template()["name"]
		data = self.propose(template_name="Mid-season Habitability", sections=[dict(THREE_SECTIONS[0])])
		self.assertIn(proposals.FLAG_SUPERSEDES_LIVE_TEMPLATE, data["ai_review_flags"])
		self.assertEqual(data["supersedes_on_approval"], live)
		self.assertEqual(sessions.live_template("Mid-season Habitability"), live)

	def test_approving_it_supersedes_the_live_one(self):
		live = self.a_template()["name"]
		draft = self.propose(template_name="Mid-season Habitability", sections=[dict(THREE_SECTIONS[0])])[
			"name"
		]
		data = self.tool_data("approve_inspection_template", {"name": draft})
		self.assertEqual(data["supersedes"], live)
		old = sessions.template_row(live)
		self.assertFalse(old["active"])
		self.assertEqual(old["superseded_by"], draft)
		self.assertEqual(sessions.live_template("Mid-season Habitability"), draft)

	def test_a_session_worked_from_the_old_version_is_still_readable(self):
		"""Superseding by copy, exactly as update_inspection_template does it."""
		unit = self.a_camp()
		live = self.a_template()["name"]
		session = self.tool_data("start_inspection_session", {"template": live, "location": unit})
		draft = self.propose(template_name="Mid-season Habitability", sections=[dict(THREE_SECTIONS[0])])[
			"name"
		]
		self.tool_data("approve_inspection_template", {"name": draft})
		read = self.tool_data("get_inspection_session", {"name": session["name"]})
		self.assertEqual(read["template"], live)
		self.assertEqual(len(self.tool_data("get_inspection_template", {"name": live})["sections"]), 3)

	def test_the_tool_is_off_until_an_operator_turns_it_on(self):
		name = self.propose()["name"]
		self.configure(
			enabled=1, **{**TEMPLATE_TOOLS, **PROPOSAL_TOOLS, "allow_approve_inspection_template": 0}
		)
		self.assertIn("switched off", self.tool_error("approve_inspection_template", {"name": name}))


# ── 7 ───────────────────────────────────────────────────────────────────────
class TheRailsThemselves(RuleEngineTestCase):
	"""`erpnext_mcp/proposals.py` is pure, and this is where that is worth it."""

	def test_a_citation_is_the_section_and_the_url(self):
		self.assertEqual(
			proposals.citation(url="https://x.test/a.pdf", section="OAR 437-004-1131"),
			"OAR 437-004-1131 — https://x.test/a.pdf",
		)

	def test_a_section_alone_is_enough(self):
		self.assertEqual(proposals.citation(section="29 CFR 1910.142"), "29 CFR 1910.142")

	def test_nothing_at_all_is_not(self):
		with self.assertRaises(ValueError):
			proposals.citation()

	def test_the_read_date_goes_on_it(self):
		self.assertIn("(read 2026-08-05)", proposals.citation(section="x", read_on="2026-08-05"))

	def test_a_quoted_excerpt_is_capped(self):
		long_text = "word " * 200
		self.assertLessEqual(len(proposals.excerpt(long_text)), proposals.EXCERPT_CAP)
		self.assertTrue(proposals.excerpt(long_text).endswith("…"))

	def test_only_the_code_shaped_flags_need_acknowledging(self):
		flags = [
			proposals.FLAG_CUSTOM_PYTHON,
			proposals.FLAG_SUPERSEDES_LIVE_RULE,
			proposals.FLAG_NO_REGIMES,
		]
		self.assertEqual(proposals.code_flags(flags), [proposals.FLAG_CUSTOM_PYTHON])

	def test_every_flag_carries_a_sentence(self):
		"""A flag with no explanation is a word on a form nobody can act on."""
		for flag in (
			proposals.FLAG_CUSTOM_PYTHON,
			proposals.FLAG_PRODUCER_EXPRESSION,
			proposals.FLAG_SUPERSEDES_LIVE_RULE,
			proposals.FLAG_SUPERSEDES_LIVE_TEMPLATE,
			proposals.FLAG_SECTIONS_WITHOUT_CONTRACT,
			proposals.FLAG_NO_REGIMES,
		):
			with self.subTest(flag=flag):
				self.assertTrue(proposals.FLAG_NOTES.get(flag))

	def test_flags_read_back_off_a_stored_column(self):
		self.assertEqual(proposals.read_flags('["custom_python"]'), ["custom_python"])
		self.assertEqual(proposals.read_flags(""), [])
		self.assertEqual(proposals.read_flags(None), [])
		self.assertEqual(
			proposals.read_flags("custom_python, no_regime_named"),
			[
				"custom_python",
				"no_regime_named",
			],
		)

	def test_an_approval_field_offered_by_a_proposal_is_named(self):
		self.assertEqual(
			proposals.offered_approval_fields({"human_approved_by": "a@b.test", "title": "x"}),
			["human_approved_by"],
		)
		self.assertEqual(proposals.offered_approval_fields({"human_approved_by": ""}), [])

	def test_a_days_helper_the_other_files_share_still_works(self):
		"""Guards the import above rather than the helper: this module reads the
		alert fixtures, and a rename there should fail here loudly."""
		self.assertTrue(days_from_today(-1) < days_from_today(1))

# SPDX-License-Identifier: MIT
"""Three policy values Tim changes without a release. v0.312.0.

(a) the OR-W-4 e-signature acceptance flag ("awaiting DOR written confirmation") and the forwarding rule's days and
    basis — payroll setting `oregon_withholding`;
(b) the Oregon bonus-in-regular-rate flag (default include) — `pay_item_policy`;
(c) the garnishment priority order — `garnishment_policy` — and the debt-type limit tables, dated rows —
    `garnishment_limits`.
Each goes through the config lifecycle: drafted (over MCP an AI draft), previewed, published ONLY by a person in the
Desk, and every version kept with who drafted and who published it. The code reads whatever is published.
"""

from erpnext_mcp import config_lifecycle, payroll_deductions, payroll_settings, phone_config
from erpnext_mcp.tools import w4

from .fixtures import V12TestCase
from .harness import STORE

KINDS = ("oregon_withholding", "pay_item_policy", "garnishment_policy", "garnishment_limits")


class PolicyCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, allow_config=1, allow_draft_config=1, allow_stage_config=1,
		               allow_publish_config=1, allow_rollback_config=1)

	def draft(self, kind, body):
		"""Over MCP: the AI drafts."""
		got = self.tool_data("config", {"action": "draft", "kind": kind, "key": kind, "body": body})
		STORE.commit()
		return got["draft"]

	def publish_in_the_desk(self, kind, version):
		with config_lifecycle.desk_action():
			phone_config.publish(payroll_settings.KIND, kind, version, "approved for the farm", "Administrator")
		STORE.commit()

	def seed(self, kind):
		return {**payroll_settings.seed_body(kind), "effective_from": "2025-01-01"}

	def versions(self, kind):
		return STORE.rows("Farm Config Version")


class OverMcpOnlyADraft(PolicyCase):
	def test_mcp_cannot_publish_any_of_the_three(self):
		for kind in KINDS:
			with self.subTest(kind=kind):
				draft = self.draft(kind, self.seed(kind))
				error = self.tool_error("config", {"action": "publish", "kind": kind, "key": kind,
				                                   "version": draft["version"]})
				self.assertIn("Desk", error)


class TheESignatureFlag(PolicyCase):
	def test_confirming_dor_acceptance_is_a_published_policy_value(self):
		self.assertFalse(w4.esignature_status()["confirmed"])
		body = self.seed("oregon_withholding")
		body["esignature"] = {**body["esignature"], "confirmed": True,
		                      "note": "DOR letter 2026-11-02, ref WH-1234, accepts e-signed OR-W-4s"}
		draft = self.draft("oregon_withholding", body)
		self.publish_in_the_desk("oregon_withholding", draft["version"])
		got = w4.esignature_status()
		self.assertEqual((got["confirmed"], got["status"]), (True, "confirmed by the Oregon DOR"))

	def test_confirmed_needs_the_dors_words_and_the_signer_rule_cannot_go(self):
		body = self.seed("oregon_withholding")
		body["esignature"] = {**body["esignature"], "confirmed": True, "note": "", "require_verified_signer": False}
		errors = " ".join(payroll_settings.validate(body, payroll_settings.OREGON_KEY)["errors"])
		self.assertIn("how the DOR confirmed", errors)
		self.assertIn("require_verified_signer", errors)

	def test_the_forwarding_days_and_basis_are_policy(self):
		values = {"or_allowances": 12}
		before = w4.or_forwarding(values, "EMP-X", {"or_received_on": "2026-10-20"}, "2026-10-01")
		self.assertEqual(before["or_forward_due"], "2026-10-21", "20 days from the signature")
		body = self.seed("oregon_withholding")
		body["forwarding"] = {**body["forwarding"], "days": 30, "basis": "received"}
		draft = self.draft("oregon_withholding", body)
		self.publish_in_the_desk("oregon_withholding", draft["version"])
		after = w4.or_forwarding(values, "EMP-X", {"or_received_on": "2026-10-20"}, "2026-10-01")
		self.assertEqual(after["or_forward_due"], "2026-11-19", "30 days from the day received")

	def test_an_older_body_without_the_new_keys_still_validates(self):
		body = {k: v for k, v in self.seed("oregon_withholding").items() if k not in ("esignature", "no_or_w4_rule")}
		self.assertEqual(payroll_settings.validate(body, payroll_settings.OREGON_KEY)["errors"], [])


class TheOregonBonusFlag(PolicyCase):
	def test_leaving_bonuses_out_is_a_published_policy_value(self):
		self.assertTrue(payroll_settings.bonuses_in_regular_rate("OR"))
		body = self.seed("pay_item_policy")
		body["states"] = {**body["states"], "OR": {**body["states"]["OR"], "bonuses_in_regular_rate": False}}
		draft = self.draft("pay_item_policy", body)
		self.publish_in_the_desk("pay_item_policy", draft["version"])
		self.assertFalse(payroll_settings.bonuses_in_regular_rate("OR"))
		self.assertTrue(payroll_settings.bonuses_in_regular_rate("WA"), "Washington's never moves")


class TheGarnishmentOrderAndTables(PolicyCase):
	def test_the_priority_order_is_a_published_policy_value(self):
		body = self.seed("garnishment_policy")
		body["priority"] = ["child_support", "tax_levy", "wage_garnishment", "student_loan"]
		draft = self.draft("garnishment_policy", body)
		self.publish_in_the_desk("garnishment_policy", draft["version"])
		self.assertEqual(payroll_settings.garnishment_policy()["priority"][2], "wage_garnishment")

	def test_support_must_stay_first(self):
		body = {**self.seed("garnishment_policy"), "priority": ["tax_levy", "child_support"]}
		self.assertTrue(payroll_settings.validate(body, payroll_settings.GARNISHMENT_POLICY_KEY)["errors"])

	def test_a_new_dated_row_is_a_published_policy_value(self):
		body = self.seed("garnishment_limits")
		body["rows"] = [*body["rows"], {"scope": "state", "state": "OR", "effective_from": "2027-07-01",
		                                "source": "ORS 18.385 (2027 adjustment)", "ordinary_rate": 0.25,
		                                "floors": {"Weekly": 420, "Biweekly": 874, "Semimonthly": 957,
		                                           "Monthly": 1882},
		                                "support_floors": {"Weekly": 254, "Biweekly": 509, "Semimonthly": 545,
		                                                   "Monthly": 1090}}]
		draft = self.draft("garnishment_limits", body)
		self.publish_in_the_desk("garnishment_limits", draft["version"])
		self.assertEqual(payroll_settings.garnishment_limits("2027-08-01", "OR")["state"]["floors"]["Weekly"], 420)
		self.assertEqual(payroll_settings.garnishment_limits("2026-08-01", "OR")["state"]["floors"]["Weekly"], 400)
		got = payroll_deductions.apply_garnishments(
			500, [{"name": "G", "deduction_type": "Garnishment", "deduction_category": "Wage Garnishment",
			       "debt_type": "Other judgment", "amount_type": "Fixed", "amount": 300, "status": "Active",
			       "effective_from": "2026-01-01"}],
			pay_frequency="Weekly", limits=payroll_settings.garnishment_limits("2027-08-01", "OR"))
		self.assertEqual(got["lines"][0]["amount"], 80.0, "500 - 420")

	def test_a_malformed_table_is_refused(self):
		body = self.seed("garnishment_limits")
		rows = [dict(r) for r in body["rows"]]
		wa = next(r for r in rows if r.get("state") == "WA")
		wa["private_student_loan"] = {"rate": 0.15, "source": "RCW 6.27.150"}
		body["rows"] = rows
		errors = " ".join(payroll_settings.validate(body, payroll_settings.GARNISHMENT_LIMITS_KEY)["errors"])
		self.assertIn("highest_local_minimum_wage", errors)


class EveryVersionIsKept(PolicyCase):
	def test_who_drafted_and_who_published(self):
		draft = self.draft("pay_item_policy", self.seed("pay_item_policy"))
		self.publish_in_the_desk("pay_item_policy", draft["version"])
		rows = [r for r in STORE.rows("Farm Config Version") if r.get("config_key") == "pay_item_policy"]
		self.assertTrue(rows)
		row = rows[-1]
		self.assertEqual(row.get("authored_by"), "AI-proposed")
		self.assertEqual(row.get("status"), "Published")

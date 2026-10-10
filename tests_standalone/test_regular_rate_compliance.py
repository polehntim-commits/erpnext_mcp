# SPDX-License-Identifier: MIT
"""Bonuses, overtime thresholds and the federal trap, as dated policy. v0.309.0.

Washington: non-discretionary and production bonuses are always in the regular rate (L&I ES.A.8.1 §§4.1, 5.3, 6.2);
farm overtime after 40 hours since 2024 (RCW 49.46.130(6)(c)). Oregon: included by default — the conservative choice —
with the OAR 839-020-0030(1) exclusion an attorney's call; farm overtime 48 hours through 2026, 40 from 2027. BOLI's
piece-rate exemptions are data flags, off, never applied. Handling another farm's crop loses the FLSA farm exemption:
overtime after 40 with bonuses in. OAR 839-020-0013: bonus terms are disclosed, on the item or on the kind.
"""

from erpnext_mcp import pay_items, payroll_settings

from .fixtures import V12TestCase

FLSA_ON = {**payroll_settings.DEFAULT_PAY_POLICY,
           "flsa": {**payroll_settings.DEFAULT_PAY_POLICY["flsa"], "handles_other_farms_crop": True}}


def item_policy(**states):
	base = payroll_settings.DEFAULT_PAY_ITEM_POLICY
	return {**base, "states": {**base["states"], **{k: {**base["states"][k], **v} for k, v in states.items()}},
	        "effective_from": "2026-01-01"}


class Bonuses(V12TestCase):
	def test_washington_cannot_leave_them_out(self):
		got = payroll_settings.validate(item_policy(WA={"bonuses_in_regular_rate": False}),
		                                payroll_settings.PAY_ITEM_POLICY_KEY)
		self.assertTrue(any("ES.A.8.1" in e for e in got["errors"]), got)

	def test_oregon_may_on_an_attorneys_advice_and_is_warned(self):
		got = payroll_settings.validate(item_policy(OR={"bonuses_in_regular_rate": False}),
		                                payroll_settings.PAY_ITEM_POLICY_KEY)
		self.assertFalse(got["errors"])
		self.assertTrue(any("attorney" in w for w in got["warnings"]))

	def test_the_defaults(self):
		self.assertTrue(payroll_settings.bonuses_in_regular_rate("OR"))
		self.assertTrue(payroll_settings.bonuses_in_regular_rate("WA"))


class Thresholds(V12TestCase):
	def test_oregon_48_through_2026_then_40(self):
		self.assertEqual(payroll_settings.ag_overtime_threshold("OR", "2026-12-31"), 48.0)
		self.assertEqual(payroll_settings.ag_overtime_threshold("OR", "2027-01-01"), 40.0)
		self.assertIn("HB 4002", next(r["source"] for r in payroll_settings.SEED_FLOORS
		                              if r["state"] == "OR" and r["kind"] == "ag_overtime_threshold"))

	def test_washington_40_since_2024_under_rcw_49_46_130(self):
		row = next(r for r in payroll_settings.SEED_FLOORS if r["state"] == "WA" and r["kind"] == "ag_overtime_threshold")
		self.assertEqual((row["hours"], row["effective_from"]), (40, "2024-01-01"))
		self.assertIn("49.46.130(6)(c)", row["source"])


class TheFederalTrap(V12TestCase):
	def test_another_farms_crop_means_40_hours_and_bonuses_in(self):
		with payroll_settings.overlay(payroll_settings.PAY_POLICY_KEY, payroll_settings.DEFAULT_PAY_POLICY):
			self.assertEqual(payroll_settings.overtime("2026-06-01", state="OR")["weekly_threshold_hours"], 48.0)
		with payroll_settings.overlay(payroll_settings.PAY_POLICY_KEY, FLSA_ON):
			rule = payroll_settings.overtime("2026-06-01", state="OR")
			self.assertEqual((rule["weekly_threshold_hours"], rule.get("flsa_non_ag")), (40.0, True))
			excluded = item_policy(OR={"bonuses_in_regular_rate": False})
			self.assertTrue(payroll_settings.bonuses_in_regular_rate("OR", policy=excluded))
			self.assertTrue(any("780.141" in w for w in payroll_settings.compliance_warnings("2026-06-01")))

	def test_off_by_default_and_silent(self):
		self.assertFalse(payroll_settings.flsa_non_ag())
		self.assertEqual(payroll_settings.compliance_warnings(), [])


class PieceRateExemptions(V12TestCase):
	def test_off_by_default_and_never_applied(self):
		flags = payroll_settings.pay_policy()["boli_piece_rate_exemptions"]
		self.assertFalse(any(v for k, v in flags.items() if k != "source"))
		bad = {**payroll_settings.DEFAULT_PAY_POLICY, "effective_from": "2026-01-01",
		       "boli_piece_rate_exemptions": {**flags, "applied_by_payroll": True}}
		got = payroll_settings.validate(bad, payroll_settings.PAY_POLICY_KEY)
		self.assertTrue(any("never applies" in e for e in got["errors"]), got)

	def test_a_flag_on_is_reported_not_applied(self):
		flags = {**payroll_settings.DEFAULT_PAY_POLICY["boli_piece_rate_exemptions"], "immediate_family": True}
		with payroll_settings.overlay(payroll_settings.PAY_POLICY_KEY,
		                              {**payroll_settings.DEFAULT_PAY_POLICY, "boli_piece_rate_exemptions": flags}):
			warnings = payroll_settings.compliance_warnings()
		self.assertTrue(any("immediate_family" in w and "still pays" in w for w in warnings))


class BonusTermsDisclosed(V12TestCase):
	def items(self, **row):
		return {"EMP-1": [{"name": "PI-1", "kind": "Production Bonus", "amount": 100, "earned_from": "2026-07-01",
		                   **row}]}

	def disclosed(self, posted_on):
		kinds = {**payroll_settings.DEFAULT_PAY_ITEM_POLICY["kinds"],
		         "Production Bonus": {"discretionary": False,
		                              "disclosure": {"terms": "$0.50 a bin over 40 bins a day, paid weekly",
		                                             "posted_on": posted_on, "language": "es, en",
		                                             "method": "posted at the shop"}}}
		return {**payroll_settings.DEFAULT_PAY_ITEM_POLICY, "kinds": kinds, "effective_from": "2026-01-01"}

	def test_undisclosed_is_listed(self):
		self.assertEqual(len(pay_items.unposted_bonus_terms(self.items())), 1)

	def test_a_kind_disclosed_before_it_was_earned_covers_it(self):
		with payroll_settings.overlay(payroll_settings.PAY_ITEM_POLICY_KEY, self.disclosed("2026-06-15")):
			self.assertEqual(pay_items.unposted_bonus_terms(self.items()), [])

	def test_disclosed_after_it_was_earned_does_not(self):
		with payroll_settings.overlay(payroll_settings.PAY_ITEM_POLICY_KEY, self.disclosed("2026-07-15")):
			self.assertEqual(len(pay_items.unposted_bonus_terms(self.items())), 1)

	def test_a_disclosure_needs_its_terms_and_date(self):
		policy = self.disclosed("not a date")
		got = payroll_settings.validate(policy, payroll_settings.PAY_ITEM_POLICY_KEY)
		self.assertTrue(any("posted_on" in e for e in got["errors"]), got)

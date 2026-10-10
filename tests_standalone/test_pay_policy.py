# SPDX-License-Identifier: MIT
"""Daily pay as policies: the Pay Policy and the Wage Floor. v0.287.0 (docs/design/daily_pay.md §9a).

The floor is dated, sourced law and a run never pays below it; the Pay Policy holds Tim's eight decisions and its
overtime folds in the old rule without moving a cent; a farm threshold less generous than the law is refused; a run
reads the settings in force for ITS period; the v0.235.0 publish guard now actually fires.
"""

import datetime

from erpnext_mcp import daily_pay, payroll_settings, phone_config
from erpnext_mcp.tools import payroll

from .fixtures import MAIN
from .harness import STORE, frappe
from .test_payroll_integration import PERIOD_END, PERIOD_START, WORKER, IntegrationTestCase, member, shift

PS = payroll_settings


def seed(key):
	return phone_config.seed(PS.KIND, key, PS.seed_body(key), "test")


class TheWageFloor(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		seed(PS.WAGE_FLOOR_KEY)
		STORE.commit()

	def test_the_rate_in_force_follows_the_date(self):
		self.assertEqual(PS.minimum_wages("2026-06-30")["OR"],
		                 {"standard": 15.05, "portland_metro": 16.30, "non_urban": 14.05})
		self.assertEqual(PS.minimum_wages("2026-07-01")["OR"],
		                 {"standard": 15.55, "portland_metro": 16.80, "non_urban": 14.55})
		self.assertEqual(PS.minimum_wages("2026-10-09")["WA"], {"standard": 17.13})
		self.assertEqual(PS.ag_overtime_threshold("OR", "2026-10-09"), 48.0)
		self.assertEqual(PS.ag_overtime_threshold("OR", "2027-01-01"), 40.0)

	def test_a_run_never_pays_below_it(self):
		stale = {"OR": {"standard": 14.70, "non_urban": 13.70, "portland_metro": 15.95}, "WA": {"standard": 18.00}}
		got = PS.apply_wage_floor(stale, "2026-10-09")
		self.assertEqual(got["OR"]["standard"], 15.55, "the shipped 2024 rate is lifted to the law")
		self.assertEqual(got["WA"]["standard"], 18.00, "a site's higher rate stands")

	def test_every_row_names_its_source(self):
		body = PS.seed_body(PS.WAGE_FLOOR_KEY)
		self.assertEqual(PS.validate(body, PS.WAGE_FLOOR_KEY)["errors"], [])
		body["rows"][0] = {**body["rows"][0], "source": ""}
		self.assertIn("source", " ".join(PS.validate(body, PS.WAGE_FLOOR_KEY)["errors"]))


class ThePayPolicy(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		seed(PS.WAGE_FLOOR_KEY)
		# Forty-five hours in the first week, the overtime rule's own fixture: five hours of overtime to pay.
		self.seed_shifts(*[shift(f"S{i}", f"2025-06-{2 + i:02d}", state="OR", end="15:00:00", crew=[member(WORKER)])
		                   for i in range(5)])
		self.structure(WORKER, pay_type="Hourly", rate=20.0)
		STORE.commit()
		self.args = {"company": MAIN, "pay_period_start": PERIOD_START, "pay_period_end": PERIOD_END}

	def test_the_seed_is_tims_eight_decisions_and_valid(self):
		body = PS.seed_body(PS.PAY_POLICY_KEY)
		self.assertEqual(PS.validate(body, PS.PAY_POLICY_KEY)["errors"], [])
		for field, value in (("model", "daily_periods"), ("workweek_start", "Sunday"), ("daily_cutoff", "18:00"),
		                     ("ach_timing", "next_business_day"), ("no_direct_deposit", "weekly_only")):
			self.assertEqual(body[field], value)
		# v0.298.0 (Tim, 2026-10-10): overtime at the state's legal threshold (ORS 653.272 / BOLI).
		self.assertEqual(body["overtime"], {"mode": "state_floor", "threshold_hours": 40, "multiplier": 1.5})

	def test_the_old_farm_rule_is_still_the_old_rule_to_the_cent(self):
		body = {**PS.seed_body(PS.PAY_POLICY_KEY), "effective_from": "2020-01-01",
		        "overtime": {"mode": "never_below_state_floor", "threshold_hours": 40, "multiplier": 1.5}}
		got = PS.preview(PS.PAY_POLICY_KEY, body, self.args)
		self.assertTrue(got["employees"], "the period has someone to pay")
		_c, slips, _t = payroll._period_run(dict(self.args), creating=False)
		self.assertGreater(float(slips[0].get("overtime_hours") or 0), 0, "and overtime to pay")
		self.assertTrue(got["identical"], got["employees"])

	def test_the_legal_floor_moves_a_45_hour_oregon_week_off_overtime(self):
		"""v0.298.0. The seed is `state_floor`: an Oregon 45-hour week in 2025 is under ORS 653.272's 48 hours, and
		the preview says so per employee before anyone publishes it."""
		body = {**PS.seed_body(PS.PAY_POLICY_KEY), "effective_from": "2020-01-01"}
		got = PS.preview(PS.PAY_POLICY_KEY, body, self.args)
		change = got["employees"][0]["changes"]["overtime_hours"]
		self.assertEqual((change["live"], change["staged"]), (5.0, 0.0))

	def test_never_less_generous_than_the_state(self):
		seed(PS.PAY_POLICY_KEY)
		STORE.commit()
		# v0.298.0: the seed is the legal floor itself — 48 in Oregon through 2026.
		self.assertEqual(PS.overtime("2026-10-09", state="OR")["weekly_threshold_hours"], 48.0)
		with PS.overlay(PS.PAY_POLICY_KEY, {**PS.seed_body(PS.PAY_POLICY_KEY),
		                                    "overtime": {"mode": "never_below_state_floor", "threshold_hours": 50,
		                                                 "multiplier": 1.5}}):
			self.assertEqual(PS.overtime("2026-10-09", state="OR")["weekly_threshold_hours"], 48.0)
			self.assertEqual(PS.overtime("2027-02-01", state="OR")["weekly_threshold_hours"], 40.0)

	def test_a_farm_threshold_below_the_law_is_refused(self):
		body = {**PS.seed_body(PS.PAY_POLICY_KEY), "overtime": {"mode": "farm_threshold", "threshold_hours": 44,
		                                                       "multiplier": 1.5}}
		errors = " ".join(PS.validate(body, PS.PAY_POLICY_KEY)["errors"])
		self.assertIn("below the floor", errors, "Oregon's 40 hours from 2027 binds a 44-hour farm threshold")
		body["overtime"]["threshold_hours"] = 40
		self.assertEqual(PS.validate(body, PS.PAY_POLICY_KEY)["errors"], [])
		body["overtime"]["multiplier"] = 1.25
		self.assertIn("1.5–3.0", " ".join(PS.validate(body, PS.PAY_POLICY_KEY)["errors"]))

	def test_option_b_is_not_built(self):
		body = {**PS.seed_body(PS.PAY_POLICY_KEY), "model": "weekly_advances"}
		self.assertIn("not built", " ".join(PS.validate(body, PS.PAY_POLICY_KEY)["errors"]))


class TheRunReadsItsOwnPeriod(IntegrationTestCase):
	def test_a_period_reads_the_setting_in_force_then(self):
		phone_config.seed(PS.KIND, PS.OVERTIME_KEY, {**PS.seed_body(), "effective_from": "2020-01-01"}, "test")
		STORE.commit()
		later = {**PS.seed_body(), "multiplier": 2.0, "effective_from": "2099-01-01"}
		doc, _ = phone_config.save_draft(PS.KIND, PS.OVERTIME_KEY, later, "test", "Operator")
		from erpnext_mcp import config_lifecycle

		with config_lifecycle.desk_action():
			phone_config.publish(doc.config_kind, doc.config_key, doc.version, "test", "Administrator")
		STORE.commit()
		self.assertEqual(PS.ot_multiplier(), 1.5)
		with PS.period("2099-03-01"):
			self.assertEqual(PS.ot_multiplier(), 2.0, "inside a 2099 run, the 2099 rule")
		self.assertEqual(PS.ot_multiplier(), 1.5)

	def test_the_publish_guard_now_fires(self):
		STORE.seed("Farm Payroll Entry", [{"name": "FPE-1", "company": MAIN, "status": "Submitted",
		                                   "pay_period_start": "2026-09-01", "pay_period_end": "2026-09-14",
		                                   "pay_frequency": "Biweekly"}])
		STORE.commit()
		body = {**PS.seed_body(), "effective_from": "2026-09-10"}
		self.assertIn("FPE-1", " ".join(PS.validate(body, PS.OVERTIME_KEY, for_publish=True)["errors"]))


class Eligibility(IntegrationTestCase):
	def test_every_reason_in_words(self):
		got = daily_pay.eligibility(WORKER, "2026-10-09")
		self.assertFalse(got["eligible"])
		self.assertTrue(any("daily_pay_enabled" in r for r in got["reasons"]))
		self.assertTrue(any("opted in" in r for r in got["reasons"]))

	def test_the_workweek_starts_sunday(self):
		self.assertEqual(daily_pay.workweek_start(datetime.date(2026, 10, 9)), datetime.date(2026, 10, 4))
		self.assertEqual(daily_pay.workweek_start(datetime.date(2026, 10, 4)), datetime.date(2026, 10, 4))
		self.assertEqual(daily_pay.workweek_start(datetime.date(2026, 10, 10)), datetime.date(2026, 10, 4))


del frappe

# SPDX-License-Identifier: MIT
"""Washington paid sick leave's rate. v0.307.0 (WAC 296-128-670; L&I Administrative Policy ES.B.2).

The greater of the Washington minimum wage and normal hourly compensation, one method per job class (670(6)).
Piece rate: total earnings / total hours in the most recent workweek of identical or substantially similar work —
piece, hourly, paid rest and non-discretionary bonuses in; the overtime premium and discretionary pay out. No such week:
the greater of the most recent agricultural week and the minimum wage, flagged for attorney review. The week used and
the arithmetic are kept on the pay item.
"""

import json

from erpnext_mcp import payroll_settings, sick_time

from .fixtures import MAIN, V12TestCase
from .harness import STORE, frappe
from .test_payroll_integration import ON, PICKER, WORKER, IntegrationTestCase, member, shift
from .test_piecework_rules import capture

PIECE = {"pay_type": "Piece Rate", "base_rate": 37.5}
WA_MIN = 17.13


def days(prefix, first, count, units=6, **extra):
	"""`count` Washington days from `first`, each with `units` accepted bucket captures (one row, one bucket)."""
	import datetime

	start = datetime.date.fromisoformat(first)
	rows, logs = [], []
	for i in range(count):
		day = str(start + datetime.timedelta(days=i))
		rows.append(shift(f"{prefix}{i}", day, state="WA", crew=[member(PICKER)], **extra))
		logs += [capture(hash((prefix, i, n)) % 10**7, day=day, at=f"{day} {8 + n:02d}:00:00") for n in range(units)]
	STORE.seed("Farm Shift", rows)
	if logs:
		STORE.seed("Bucket Log Entry", logs)
	STORE.commit()


class TheRate(V12TestCase):
	def setUp(self):
		super().setUp()
		STORE.seed("Employee", [{"name": PICKER, "employee_name": "Beto", "company": MAIN, "status": "Active",
		                         "date_of_joining": "2024-01-01"}])
		STORE.commit()

	def rate(self, day="2026-06-24", structure=PIECE):
		return sick_time.wa_rate(MAIN, PICKER, day, structure, WA_MIN)

	def test_the_most_recent_identical_week(self):
		days("A", "2026-06-07", 5)  # Sun-Thu: 45 h, 30 buckets x 37.50
		STORE.commit()
		rate, evidence = self.rate()
		self.assertEqual(rate, 25.0)
		self.assertEqual((evidence["tier"], evidence["week_start"], evidence["hours"], evidence["straight_time_pay"]),
		                 ("identical", "2026-06-07", 45.0, 1125.0))
		self.assertFalse(evidence["attorney_review"])
		self.assertEqual(evidence["source"], "WAC 296-128-670; L&I Administrative Policy ES.B.2")

	def test_a_production_bonus_earned_that_week_is_earnings(self):
		days("A", "2026-06-07", 5)
		STORE.seed("Farm Pay Item", [{"name": "PI-1", "employee": PICKER, "company": MAIN, "kind": "Production Bonus",
		                              "amount": 90, "earned_from": "2026-06-07", "earned_to": "2026-06-13",
		                              "status": "Paid", "treatment": "Wages"}])
		STORE.commit()
		rate, evidence = self.rate()
		self.assertEqual(rate, 27.0, "(1125 + 90) / 45")
		self.assertEqual(evidence["bonus"], 90.0)

	def test_a_discretionary_bonus_is_not(self):
		days("A", "2026-06-07", 5)
		STORE.seed("Farm Pay Item", [{"name": "PI-1", "employee": PICKER, "company": MAIN, "kind": "Bonus",
		                              "amount": 90, "earned_from": "2026-06-07", "earned_to": "2026-06-13",
		                              "status": "Paid", "treatment": "Wages"}])
		STORE.commit()
		policy = {**payroll_settings.DEFAULT_PAY_ITEM_POLICY,
		          "kinds": {**payroll_settings.DEFAULT_PAY_ITEM_POLICY.get("kinds", {}), "Bonus": {"discretionary": True}}}
		with payroll_settings.overlay(payroll_settings.PAY_ITEM_POLICY_KEY, policy):
			rate, _evidence = self.rate()
		self.assertEqual(rate, 25.0)

	def test_never_below_the_washington_minimum_wage(self):
		days("A", "2026-06-07", 5, units=1)  # 37.50 / 9 h a day
		STORE.commit()
		self.assertEqual(self.rate()[0], WA_MIN)

	def test_no_similar_week_falls_back_and_is_flagged(self):
		days("H", "2026-06-07", 2, units=0, pay_type="Hourly", pay_rate=19.0)
		STORE.commit()
		rate, evidence = self.rate()
		self.assertEqual((evidence["tier"], evidence["attorney_review"]), ("fallback_recent_ag_week", True))
		self.assertEqual(rate, 19.0)

	def test_no_history_at_all_is_the_minimum_wage_and_flagged(self):
		rate, evidence = self.rate()
		self.assertEqual((rate, evidence["tier"], evidence["attorney_review"]), (WA_MIN, "fallback_minimum_wage", True))

	def test_oregon_hours_are_not_washington_work(self):
		days("A", "2026-06-07", 5, work_state="OR")
		self.assertEqual(self.rate()[1]["tier"], "fallback_minimum_wage")

	def test_an_hourly_job_class_uses_the_hourly_rate(self):
		rate, evidence = self.rate(structure={"pay_type": "Hourly", "base_rate": 21.5})
		self.assertEqual((rate, evidence["method"]), (21.5, "hourly_rate"))

	def test_one_method_per_job_class_and_piece_work_has_no_hourly_rate(self):
		got = payroll_settings.validate({"effective_from": "2026-01-01",
		                                 "washington": {"rate_method": {"Piece Rate": "hourly_rate"}}},
		                                payroll_settings.SICK_TIME_POLICY_KEY)
		self.assertTrue(any("Piece Rate" in e for e in got["errors"]), got)


class TheEvidenceIsKept(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **{**ON, "allow_create_leave_request": 1})
		self.structure(WORKER, rate=20.0)
		frappe.db.set_value("Employee", WORKER, "date_of_joining", "2025-01-15")
		STORE.commit()

	def test_a_saved_run_writes_how_the_rate_was_set(self):
		import datetime

		def week(prefix, first):
			start = datetime.date.fromisoformat(first)
			self.seed_shifts(*[shift(f"{prefix}{i}", str(start + datetime.timedelta(days=i)), state="WA",
			                         crew=[member(WORKER)]) for i in range(5)])
			STORE.commit()

		week("A", "2025-06-02")
		self.tool_data("run_payroll_for_period", {"company": MAIN, "pay_period_start": "2025-06-01",
		                                          "pay_period_end": "2025-06-14"})
		STORE.commit()
		filed = self.tool_data("create_leave_request", {"employee": WORKER, "leave_type": "Washington Paid Sick Leave",
		                                                "from_date": "2025-06-20", "hours": 1})
		STORE.commit()
		week("B", "2025-06-16")
		self.tool_data("run_payroll_for_period", {"company": MAIN, "pay_period_start": "2025-06-15",
		                                          "pay_period_end": "2025-06-28"})
		STORE.commit()
		evidence = json.loads(frappe.db.get_value("Farm Pay Item", filed["pay_item"], "rate_evidence"))
		self.assertEqual((evidence["method"], evidence["rate"]), ("hourly_rate", 20.0))

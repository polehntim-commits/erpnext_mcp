# SPDX-License-Identifier: MIT
"""Daily pay, step 2: the daily run. v0.288.0 (docs/design/daily_pay.md §3).

`run_payroll_for_period(run_kind=Daily)` pays ONE day to the workers on daily pay — straight time, the day's
minimum-wage makeup, taxes only — and only for reviewed punches; everybody else waits, with the reason. A Regular run
leaves daily-pay workers out, so nobody is paid twice and nobody falls between the two runs.
"""

import datetime

from erpnext_mcp import daily_pay, flags

from .fixtures import MAIN
from .harness import STORE, frappe
from .test_payroll_integration import PICKER, WORKER, IntegrationTestCase, member, shift

DAY = "2025-06-03"


class DailyRunCase(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		daily_pay.install_fields()
		flags.upsert(daily_pay.FLAG, "Flag", True, company=MAIN, description="test", owner_area="payroll", active=True)
		for employee in (WORKER, PICKER):
			frappe.db.set_value("Employee", employee, "employment_type", "Seasonal")
		frappe.db.set_value("Employee", WORKER, "daily_pay", 1)
		frappe.db.set_value("Employee", WORKER, "daily_pay_since", "2025-06-01")
		STORE.seed("Employee Bank Account", [{"name": "EBA-W", "employee": WORKER, "status": "Active", "priority": 1,
		                                      "account_type": "Checking"}])
		self.seed_shifts(shift("S1", DAY, crew=[member(WORKER, time_review_status="Approved"),
		                                        member(PICKER, time_review_status="Approved")]))
		self.structure(WORKER, pay_type="Hourly", rate=20.0)
		self.structure(PICKER, pay_type="Hourly", rate=20.0)
		STORE.commit()

	def daily(self, **extra):
		return self.tool_data("run_payroll_for_period", {"company": MAIN, "pay_period_start": DAY,
		                                                 "pay_period_end": DAY, "run_kind": "Daily", **extra})


class TheDay(DailyRunCase):
	def test_pays_the_day_to_the_daily_worker_only(self):
		got = self.daily()
		self.assertEqual([s["employee"] for s in got["slips"]], [WORKER])
		self.assertEqual((got["run_kind"], got["pay_frequency"]), ("Daily", "Daily"))
		self.assertEqual(got["slips"][0]["overtime_hours"], 0)
		self.assertGreater(got["slips"][0]["gross_pay"], 0)
		self.assertEqual(got["slips"][0]["garnishment_total"], 0)
		self.assertIn("taxes only", got["deductions"]["note"])
		self.assertEqual(got["ach_effective_date"], "2025-06-04")
		entry = frappe.get_doc("Farm Payroll Entry", got["name"])
		self.assertEqual((entry.run_kind, entry.pay_frequency), ("Daily", "Daily"))

	def test_never_twice_for_one_day(self):
		self.daily()
		STORE.commit()
		message = self.tool_error("run_payroll_for_period", {"company": MAIN, "pay_period_start": DAY,
		                                                     "pay_period_end": DAY, "run_kind": "Daily"})
		self.assertIn("already paid", message)

	def test_one_day_only_and_off_until_turned_on(self):
		self.assertIn("one day", self.tool_error("run_payroll_for_period", {
			"company": MAIN, "pay_period_start": DAY, "pay_period_end": "2025-06-04", "run_kind": "Daily"}))
		flags.upsert(daily_pay.FLAG, "Flag", False, company=MAIN, description="test", owner_area="payroll", active=True)
		STORE.commit()
		self.assertIn("daily_pay_enabled", self.tool_error("run_payroll_for_period", {
			"company": MAIN, "pay_period_start": DAY, "pay_period_end": DAY, "run_kind": "Daily"}))


class WhoWaits(DailyRunCase):
	def test_an_unreviewed_punch_waits(self):
		self.seed_shifts(shift("S1", DAY, crew=[member(WORKER, time_review_status="Pending")]))
		STORE.commit()
		plan = daily_pay.day_plan(MAIN, DAY)
		self.assertEqual(plan["pay"], [])
		self.assertIn("not reviewed", " ".join(plan["held"][0]["reasons"]))

	def test_no_direct_deposit_means_the_weekly_run(self):
		frappe.db.set_value("Employee Bank Account", "EBA-W", "status", "Inactive")
		STORE.commit()
		plan = daily_pay.day_plan(MAIN, DAY)
		self.assertIn("weekly run", " ".join(plan["held"][0]["reasons"]))
		self.assertFalse(daily_pay.paid_daily(WORKER, DAY, MAIN), "so the weekly run still pays them")

	def test_today_waits_for_the_cutoff(self):
		morning = datetime.datetime(2025, 6, 3, 10, 0)
		with self.assertRaisesRegex(daily_pay.DailyPayError, "18:00"):
			daily_pay.day_plan(MAIN, DAY, now=morning)
		self.assertTrue(daily_pay.day_plan(MAIN, DAY, now=datetime.datetime(2025, 6, 3, 18, 30))["pay"])


class TheRegularRun(DailyRunCase):
	def test_leaves_daily_workers_out(self):
		got = self.tool_data("preview_payroll_for_period", {"company": MAIN, "pay_period_start": "2025-06-02",
		                                                    "pay_period_end": "2025-06-15"})
		paid = [s["employee"] for s in got["slips"]]
		self.assertNotIn(WORKER, paid)
		self.assertIn(PICKER, paid)
		self.assertEqual(got["daily_pay_left_out"], [WORKER])

	def test_a_switch_inside_the_period_is_held_not_guessed(self):
		frappe.db.set_value("Employee", WORKER, "daily_pay_since", "2025-06-08")
		STORE.commit()
		got = self.tool_data("preview_payroll_for_period", {"company": MAIN, "pay_period_start": "2025-06-02",
		                                                    "pay_period_end": "2025-06-15"})
		self.assertNotIn(WORKER, [s["employee"] for s in got["slips"]])
		self.assertIn("switched", " ".join(got["daily_pay_held"][0]["reasons"]))

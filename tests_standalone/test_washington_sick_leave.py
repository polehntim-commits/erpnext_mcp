# SPDX-License-Identifier: MIT
"""Washington paid sick leave, and opening balances. v0.305.0 (RCW 49.46.210; WAC 296-128-600 to -770).

A separate balance from Oregon sick time, for hours worked in Washington: 1 hour per 40, no annual accrual or use cap,
up to 40 carried into the next year, usable from the 90th day, restored on rehire within 12 months, and paid at every
employer size. Opening balances carried in from before the ledger go through `set_opening_balance kind=sick_time`.
"""

import datetime

from erpnext_mcp import payroll_settings, sick_time

from .fixtures import MAIN, V12TestCase
from .harness import STORE, frappe
from .test_payroll_integration import ON, PICKER, WORKER, IntegrationTestCase, member, shift


def wa_week(case, prefix, first_day, days=5, state="WA"):
	start = datetime.date.fromisoformat(first_day)
	case.seed_shifts(*[shift(f"{prefix}{i}", str(start + datetime.timedelta(days=i)), state=state,
	                         crew=[member(WORKER)]) for i in range(days)])
	STORE.commit()


class ThePolicy(V12TestCase):
	def test_washington_defaults_are_the_statute(self):
		wa = payroll_settings.sick_time_policy()["washington"]
		self.assertEqual((wa["accrual_per_hours_worked"], wa["annual_accrual_cap"], wa["carryover_cap"],
		                  wa["annual_use_cap"], wa["paid"]), (40.0, None, 40.0, None, True))

	def test_the_washington_policy_cannot_go_below_the_law(self):
		for bad in ({"accrual_per_hours_worked": 50}, {"carryover_cap": 20}, {"rehire_restore_days": 180},
		            {"paid": False}):
			got = payroll_settings.validate({"effective_from": "2026-01-01", "washington": bad},
			                                payroll_settings.SICK_TIME_POLICY_KEY)
			self.assertTrue(got["errors"], bad)
		ok = payroll_settings.validate({"effective_from": "2026-01-01", "washington": {"carryover_cap": 80}},
		                               payroll_settings.SICK_TIME_POLICY_KEY)
		self.assertFalse([e for e in ok["errors"] if "washington" in e], ok)

	def test_a_policy_without_washington_reads_the_defaults(self):
		with payroll_settings.overlay(payroll_settings.SICK_TIME_POLICY_KEY,
		                              {k: v for k, v in payroll_settings.DEFAULT_SICK_TIME_POLICY.items()
		                               if k != "washington"}):
			self.assertEqual(payroll_settings.sick_time_policy()["washington"]["accrual_per_hours_worked"], 40.0)


class ThroughRuns(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **{**ON, "allow_create_leave_request": 1, "allow_get_leave_balance": 1})
		self.structure(WORKER, rate=20.0)
		frappe.db.set_value("Employee", WORKER, "date_of_joining", "2025-01-15")
		STORE.commit()

	def pay(self, start, end):
		got = self.tool_data("run_payroll_for_period", {"company": MAIN, "pay_period_start": start, "pay_period_end": end})
		STORE.commit()
		return got

	def test_washington_hours_accrue_one_per_forty_into_their_own_balance(self):
		wa_week(self, "W", "2025-06-02")  # 45 h in Washington
		self.pay("2025-06-01", "2025-06-14")
		self.assertAlmostEqual(sick_time.balance(WORKER, "2025-06-14", "WA")["balance"], 1.125, places=2)
		self.assertEqual(sick_time.balance(WORKER, "2025-06-14", "OR")["balance"], 0.0)

	def test_both_states_in_one_period_accrue_separately(self):
		wa_week(self, "W", "2025-06-02")
		wa_week(self, "O", "2025-06-09", state="OR")
		self.pay("2025-06-01", "2025-06-14")
		self.assertAlmostEqual(sick_time.balance(WORKER, "2025-06-14", "WA")["balance"], 1.125, places=2)
		self.assertAlmostEqual(sick_time.balance(WORKER, "2025-06-14", "OR")["balance"], 1.5)

	def test_filed_and_paid_against_the_washington_balance_whatever_the_size(self):
		wa_week(self, "A", "2025-06-02")
		self.pay("2025-06-01", "2025-06-14")
		filed = self.tool_data("create_leave_request", {"employee": WORKER, "leave_type": "Washington Paid Sick Leave",
		                                                "from_date": "2025-06-20", "hours": 1})
		self.assertEqual(frappe.db.get_value("Farm Pay Item", filed["pay_item"], "work_state"), "WA")
		STORE.commit()
		wa_week(self, "B", "2025-06-16")
		got = self.pay("2025-06-15", "2025-06-28")
		sick = [i for i in got["slips"][0]["pay_items"] if i.get("kind") == "Sick Time"]
		self.assertEqual((sick[0]["hours"], sick[0]["amount"]), (1.0, 20.0), "paid: Washington pays at every size")
		bal = self.tool_data("get_leave_balance", {"employee": WORKER, "leave_type": "Washington Paid Sick Leave",
		                                           "as_of": "2025-06-28"})
		self.assertAlmostEqual(bal["balance"], 1.125 * 2 - 1, places=3)
		self.assertEqual(bal["leave_type"], "Washington Paid Sick Leave")

	def test_a_mismatched_work_state_is_refused(self):
		error = self.tool_error("create_leave_request", {"employee": WORKER, "leave_type": "Washington Paid Sick Leave",
		                                                 "from_date": "2025-06-20", "hours": 1, "work_state": "OR"})
		self.assertIn("does not match", error)


class TheBalance(V12TestCase):
	def seed(self, rows, joined="2023-01-01"):
		STORE.seed("Employee", [{"name": PICKER, "employee_name": "Beto", "company": MAIN, "status": "Active",
		                         "date_of_joining": joined}])
		STORE.seed("Farm Sick Time Ledger", [{"name": f"L{i}", "employee": PICKER, "company": MAIN, "work_state": "WA",
		                                      **row} for i, row in enumerate(rows)])
		STORE.commit()

	def test_no_annual_accrual_cap_and_forty_carried(self):
		self.seed([{"posting_date": "2024-06-01", "kind": "Accrual", "hours": 30},
		           {"posting_date": "2024-11-01", "kind": "Accrual", "hours": 30}])
		self.assertEqual(sick_time.balance(PICKER, "2024-12-31", "WA")["balance"], 60.0, "no 40 h accrual cap")
		got = sick_time.balance(PICKER, "2025-01-02", "WA")
		self.assertEqual((got["carried_in"], got["balance"]), (40.0, 40.0))

	def test_rows_before_v0_305_are_oregon(self):
		STORE.seed("Employee", [{"name": PICKER, "employee_name": "Beto", "company": MAIN, "status": "Active",
		                         "date_of_joining": "2023-01-01"}])
		STORE.seed("Farm Sick Time Ledger", [{"name": "L0", "employee": PICKER, "company": MAIN,
		                                      "posting_date": "2025-02-01", "kind": "Accrual", "hours": 5}])
		STORE.commit()
		self.assertEqual(sick_time.balance(PICKER, "2025-03-01", "OR")["balance"], 5.0)
		self.assertEqual(sick_time.balance(PICKER, "2025-03-01", "WA")["balance"], 0.0)

	def test_restored_on_rehire_within_twelve_months(self):
		self.seed([{"posting_date": "2025-02-01", "kind": "Accrual", "hours": 10}])
		frappe.db.set_value("Employee", PICKER, {"relieving_date": "2025-03-15", "date_of_joining": "2025-12-01"})
		STORE.commit()
		self.assertEqual(sick_time.balance(PICKER, "2025-12-02", "WA")["balance"], 10.0, "Oregon would reset at 180 days")


class OpeningBalances(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, allow_set_opening_balance=1)
		STORE.seed("Employee", [{"name": PICKER, "employee_name": "Beto", "company": MAIN, "status": "Active",
		                         "date_of_joining": "2023-01-01"},
		                        {"name": "HR-EMP-OTHER", "employee_name": "Otra", "company": "Elsewhere LLC",
		                         "status": "Active", "date_of_joining": "2023-01-01"}])
		STORE.commit()

	def opening(self, balances, **extra):
		return {"company": MAIN, "posting_date": "2025-12-31", "kind": "sick_time",
		        "user_remark": "Balances from the old payroll provider, 2025-12-31 register", "balances": balances,
		        **extra}

	def test_opening_balances_land_in_each_state_and_not_in_the_accrual(self):
		got = self.tool_data("set_opening_balance", self.opening([
			{"employee": PICKER, "hours": 24}, {"employee": PICKER, "hours": 12.5, "work_state": "WA"}]))
		self.assertEqual(got["total_hours"], 36.5)
		STORE.commit()
		oregon = sick_time.balance(PICKER, "2025-12-31", "OR")
		self.assertEqual((oregon["balance"], oregon["accrued_this_year"]), (24.0, 0.0))
		self.assertEqual(sick_time.balance(PICKER, "2025-12-31", "WA")["balance"], 12.5)

	def test_nothing_is_written_when_one_row_is_bad(self):
		error = self.tool_error("set_opening_balance", self.opening([
			{"employee": PICKER, "hours": 8}, {"employee": "HR-EMP-OTHER", "hours": 8}]))
		self.assertIn("works for Elsewhere LLC", error)
		self.assertEqual(frappe.db.get_all("Farm Sick Time Ledger", pluck="name"), [])

	def test_over_the_oregon_balance_cap_and_a_second_opening_are_refused(self):
		self.assertIn("balance cap", self.tool_error("set_opening_balance", self.opening([
			{"employee": PICKER, "hours": 90}])))
		self.tool_data("set_opening_balance", self.opening([{"employee": PICKER, "hours": 8}]))
		STORE.commit()
		self.assertIn("already has an opening", self.tool_error("set_opening_balance", self.opening([
			{"employee": PICKER, "hours": 8}])))

	def test_the_ledger_kind_still_needs_entries(self):
		got = self.opening(None)
		got.pop("kind")
		got.pop("balances")
		self.assertIn("entries must be", self.tool_error("set_opening_balance", got))

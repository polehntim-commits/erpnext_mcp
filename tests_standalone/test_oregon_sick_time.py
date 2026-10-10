# SPDX-License-Identifier: MIT
"""Oregon sick time. v0.301.0 (Tim, 2026-10-10: align with BOLI; ORS 653.601-.661, OAR 839-007).

Accrued by payroll at 1 hour per 30 Oregon hours (capped 40 a year); usable from day 91; paid at the regular hourly
rate, piece-only workers at least minimum wage; never more than the balance; restored on rehire within 180 days.
"""

from erpnext_mcp import payroll_settings, sick_time

from .fixtures import MAIN, V12TestCase
from .harness import STORE, frappe
from .test_payroll_integration import ON, PICKER, WORKER, IntegrationTestCase, member, shift


class TheRate(V12TestCase):
	def test_oar_839_007_0000_7(self):
		self.assertEqual(sick_time.rate({"pay_type": "Hourly", "base_rate": 20}, 15.05), 20.0)
		self.assertEqual(sick_time.rate({"pay_type": "Piece Rate", "base_rate": 1.5}, 15.05), 15.05)
		self.assertEqual(sick_time.rate({"pay_type": "Piece Rate", "base_rate": 1.5, "hourly_rate": 18}, 15.05), 18.0)

	def test_the_policy_cannot_go_below_the_law(self):
		bad = payroll_settings.validate({"accrual_per_hours_worked": 40, "effective_from": "2026-01-01"},
		                                payroll_settings.SICK_TIME_POLICY_KEY)
		self.assertTrue(any("law's floor" in e for e in bad["errors"]))


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

	def week(self, prefix, first_day, days=5):
		import datetime

		start = datetime.date.fromisoformat(first_day)
		self.seed_shifts(*[shift(f"{prefix}{i}", str(start + datetime.timedelta(days=i)), crew=[member(WORKER)])
		                   for i in range(days)])
		STORE.commit()

	def test_accrues_one_hour_per_thirty_oregon_hours(self):
		self.week("A", "2025-06-02")  # 45 h
		got = self.pay("2025-06-01", "2025-06-14")
		self.assertEqual(got["sick_time"][0]["kind"], "Accrual")
		self.assertAlmostEqual(sick_time.balance(WORKER, "2025-06-14")["balance"], 1.5)

	def test_used_sick_time_is_paid_at_the_rate_within_the_balance(self):
		self.week("B", "2025-06-02")
		self.pay("2025-06-01", "2025-06-14")  # +1.5 h
		self.week("C", "2025-06-16")
		filed = self.tool_data("create_leave_request", {"employee": WORKER, "leave_type": "Oregon Sick Time",
		                                                "from_date": "2025-06-20", "hours": 4})
		self.assertTrue(filed["pay_item"])
		STORE.commit()
		paid = {**payroll_settings.DEFAULT_SICK_TIME_POLICY, "paid": True}
		with payroll_settings.overlay(payroll_settings.SICK_TIME_POLICY_KEY, paid):
			got = self.pay("2025-06-15", "2025-06-28")
		slip = got["slips"][0]
		sick = [i for i in slip["pay_items"] if i.get("kind") == "Sick Time"]
		self.assertEqual(sick[0]["hours"], 1.5, "only the balance is paid")
		self.assertEqual(sick[0]["amount"], 30.0, "1.5 h x $20")
		balance = self.tool_data("get_leave_balance", {"employee": WORKER, "leave_type": "Oregon Sick Time",
		                                               "as_of": "2025-06-28"})
		self.assertAlmostEqual(balance["balance"], 1.5, msg="1.5 accrued this period, 1.5 used")

	def test_not_usable_before_day_91(self):
		frappe.db.set_value("Employee", WORKER, "date_of_joining", "2025-05-01")
		STORE.commit()
		self.week("D", "2025-06-02")
		self.pay("2025-06-01", "2025-06-14")
		self.tool_data("create_leave_request", {"employee": WORKER, "leave_type": "Oregon Sick Time",
		                                        "from_date": "2025-06-20", "hours": 1})
		STORE.commit()
		got = self.tool_data("preview_payroll_for_period", {"company": MAIN, "pay_period_start": "2025-06-15",
		                                                    "pay_period_end": "2025-06-28"})
		self.assertIn("91st day", got["pay_items_waiting"][0]["reason"])

	def test_a_cancelled_run_accrues_nothing(self):
		self.week("E", "2025-06-02")
		got = self.pay("2025-06-01", "2025-06-14")
		frappe.db.set_value("Farm Payroll Entry", got["name"], "status", "Cancelled")
		STORE.commit()
		self.assertEqual(sick_time.balance(WORKER, "2025-06-14")["balance"], 0.0)

	def test_washington_hours_do_not_accrue_oregon_time(self):
		import datetime

		start = datetime.date.fromisoformat("2025-06-02")
		self.seed_shifts(*[shift(f"W{i}", str(start + datetime.timedelta(days=i)), state="WA",
		                         crew=[member(WORKER)]) for i in range(5)])
		STORE.commit()
		self.pay("2025-06-01", "2025-06-14")
		self.assertEqual(sick_time.balance(WORKER, "2025-06-14")["balance"], 0.0)


class TheBalance(V12TestCase):
	def test_carryover_and_rehire(self):
		STORE.seed("Employee", [{"name": PICKER, "employee_name": "Beto", "company": MAIN, "status": "Active",
		                         "date_of_joining": "2024-01-01"}])
		STORE.seed("Farm Sick Time Ledger", [
			{"name": "L1", "employee": PICKER, "company": MAIN, "posting_date": "2024-12-01", "kind": "Accrual",
			 "hours": 40},
			{"name": "L2", "employee": PICKER, "company": MAIN, "posting_date": "2025-02-01", "kind": "Accrual",
			 "hours": 10}])
		STORE.commit()
		got = sick_time.balance(PICKER, "2025-03-01")
		self.assertEqual((got["carried_in"], got["balance"]), (40.0, 50.0))
		frappe.db.set_value("Employee", PICKER, {"relieving_date": "2025-03-15", "date_of_joining": "2025-12-01"})
		STORE.commit()
		self.assertEqual(sick_time.balance(PICKER, "2025-12-02")["balance"], 0.0, "rehired after 180 days")


class ASmallEmployer(ThroughRuns):
	"""Fewer than 10 employees: sick time is job-protected but unpaid (ORS 653.606(1)(b)); the hours still come off."""

	test_accrues_one_hour_per_thirty_oregon_hours = None
	test_not_usable_before_day_91 = None
	test_a_cancelled_run_accrues_nothing = None
	test_washington_hours_do_not_accrue_oregon_time = None
	test_used_sick_time_is_paid_at_the_rate_within_the_balance = None

	def test_unpaid_but_counted(self):
		self.week("F", "2025-06-02")
		self.pay("2025-06-01", "2025-06-14")
		self.tool_data("create_leave_request", {"employee": WORKER, "leave_type": "Oregon Sick Time",
		                                        "from_date": "2025-06-20", "hours": 1})
		STORE.commit()
		got = self.pay("2025-06-15", "2025-06-28")
		sick = [i for i in got["slips"][0]["pay_items"] if i.get("kind") == "Sick Time"]
		self.assertEqual((sick[0]["hours"], sick[0]["amount"]), (1.0, 0.0))
		self.assertTrue(any(r["kind"] == "Use" for r in got["sick_time"]))

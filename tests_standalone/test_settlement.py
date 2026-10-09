# SPDX-License-Identifier: MIT
"""Daily pay, step 3: the weekly settlement. v0.289.0 (docs/design/daily_pay.md §4–§5).

Five days paid daily at $20 an hour, nine hours a day: 45 hours in the workweek, 5 over 40. Straight time for all 45
was paid by the daily runs, so the settlement owes the half-time premium on the week's regular rate:
5 × ($900 ÷ 45) × 0.5 = **$50.00**. Deductions and garnishments are taken against the WEEK's disposable earnings,
out of the settlement's own net — a daily payment is never taken back; what does not fit is a shortfall, in words.
"""

from erpnext_mcp import daily_pay

from .fixtures import MAIN
from .harness import STORE, frappe
from .test_daily_run import DailyRunCase
from .test_payroll_integration import PICKER, WORKER, member, shift

WEEK = ("2025-06-01", "2025-06-07")  # Sunday to Saturday
DAYS = ("2025-06-02", "2025-06-03", "2025-06-04", "2025-06-05", "2025-06-06")
ON = {f"allow_{n}": 1 for n in ("run_payroll_for_period", "preview_payroll_for_period", "create_payroll_deduction")}


class SettlementCase(DailyRunCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **{**ON, **{f"allow_{n}": 1 for n in (
			"get_employee_timesheet_summary", "get_salary_structure", "preview_payroll", "get_payroll_entry")}})
		# 2025-06-03's shift is the parent fixture's S1; the other four days are new — one nine-hour shift a day.
		self.seed_shifts(*[shift("S1" if day == "2025-06-03" else f"D{i}", day,
		                         crew=[member(WORKER, time_review_status="Approved"),
		                               member(PICKER, time_review_status="Approved")])
		                   for i, day in enumerate(DAYS)])
		STORE.commit()
		self.daily_slips = []
		for day in DAYS:
			got = self.tool_data("run_payroll_for_period", {"company": MAIN, "pay_period_start": day,
			                                                "pay_period_end": day, "run_kind": "Daily"})
			self.daily_slips.append(got["slips"][0])
			STORE.commit()

	def settle(self, run=False):
		return self.tool_data("run_payroll_for_period" if run else "preview_payroll_for_period",
		                      {"company": MAIN, "pay_period_start": WEEK[0], "pay_period_end": WEEK[1],
		                       "run_kind": "Settlement"})


class ThePremium(SettlementCase):
	def test_half_time_on_the_weeks_regular_rate(self):
		hours = round(sum(s["total_hours"] for s in self.daily_slips), 2)
		straight = round(sum(s["gross_pay"] for s in self.daily_slips), 2)
		self.assertEqual(sum(s["overtime_hours"] for s in self.daily_slips), 0, "the daily runs paid no premium")
		got = self.settle()
		slip = got["slips"][0]
		over = round(hours - 40, 2)
		self.assertGreater(over, 0)
		self.assertEqual(slip["overtime_hours"], over)
		self.assertEqual(slip["gross_pay"], round(over * (straight / hours) * 0.5, 2))
		self.assertEqual((hours, straight), (45.0, 900.0), "five nine-hour days at $20")
		self.assertEqual(slip["gross_pay"], 50.00)
		self.assertEqual(slip["settlement"]["regular_rate"], 20.0)
		self.assertEqual(got["run_kind"], "Settlement")
		self.assertEqual([s["employee"] for s in got["slips"]], [WORKER], "only the worker paid daily")

	def test_income_tax_by_the_aggregate_method(self):
		# Alone, $50 a week is under every bracket and withholds no federal income tax; as part of a $950 week it
		# does. The settlement withholds the week's tax less what the five days withheld (Pub 15 §7).
		slip = self.settle()["slips"][0]
		self.assertGreater(slip["federal_withholding"], 0)
		self.assertEqual(slip["net_pay"], round(slip["gross_pay"] - slip["total_deductions"], 2))

	def test_stored_once_per_week(self):
		got = self.settle(run=True)
		self.assertEqual(frappe.db.get_value("Farm Payroll Entry", got["name"], "run_kind"), "Settlement")
		STORE.commit()
		again = self.settle()
		self.assertEqual(again["slips"], [])
		self.assertIn("already settled", " ".join(again["daily_pay_held"][0]["reasons"]))

	def test_one_workweek_only(self):
		message = self.tool_error("preview_payroll_for_period", {"company": MAIN, "pay_period_start": "2025-06-02",
		                                                         "pay_period_end": "2025-06-08", "run_kind": "Settlement"})
		self.assertIn("one workweek", message)


class Garnishments(SettlementCase):
	def garnish(self, amount):
		self.tool_data("create_payroll_deduction", {"employee": WORKER, "company": MAIN,
		                                            "deduction_category": "Child Support", "amount": amount,
		                                            "effective_from": "2025-01-01", "reference": "CASE-1"})
		STORE.commit()

	def test_taken_from_the_settlement_against_the_weeks_disposable(self):
		self.garnish(10.0)
		slip = self.settle()["slips"][0]
		self.assertEqual(slip["garnishment_total"], 10.0)
		self.assertGreater(slip["disposable_earnings"], 500, "the week's disposable, not the premium's")
		self.assertEqual(slip["net_pay"], round(slip["gross_pay"] - slip["total_deductions"], 2))

	def test_what_the_settlement_cannot_cover_is_a_shortfall_never_a_clawback(self):
		self.garnish(400.0)
		slip = self.settle()["slips"][0]
		self.assertGreaterEqual(slip["net_pay"], 0)
		self.assertLessEqual(slip["garnishment_total"], slip["gross_pay"])
		reasons = " ".join(r.get("shortfall_reason", "") for r in slip["deduction_shortfalls"])
		self.assertIn("never taken back", reasons)


del daily_pay

# SPDX-License-Identifier: MIT
"""Garnishment: every applicable limit, the most protective for the worker taken. v0.311.0.

Federal 15 U.S.C. 1673: 25% or 30 × $7.25 ($217.50 a week); support 50/60% (+5% over 12 weeks in arrears).
Oregon ORS 18.385 (SB 1595): 75% exempt or the floor — $400 a week / $832 / $912 / $1,792 from 2026-07-01, dated rows;
support writs $254. Washington RCW 6.27.150: the greater of 35 × the federal minimum ($253.75) or 75%; consumer debt
the greater of 35 × the WA minimum ($599.55) or 80%; private student loans 50 × the highest local minimum or 85%;
spousal maintenance 50%. Every garnishment carries its debt type; support comes first.
"""

from erpnext_mcp import payroll_deductions, payroll_settings

from .fixtures import V12TestCase


def order(category, amount, debt_type="", **extra):
	return {"name": f"G-{debt_type or category}", "deduction_type": "Garnishment", "deduction_category": category,
	        "amount_type": "Fixed", "amount": amount, "status": "Active", "effective_from": "2026-01-01",
	        **({"debt_type": debt_type} if debt_type else {}), **extra}


def take(disposable, rows, state, day="2026-08-01", frequency="Weekly"):
	limits = payroll_settings.garnishment_limits(day, state)
	return payroll_deductions.apply_garnishments(disposable, rows, pay_frequency=frequency, limits=limits)


class TheDatedRows(V12TestCase):
	def test_oregon_floors_from_july_2026_and_the_support_floor(self):
		row = payroll_settings.garnishment_limits("2026-08-01", "OR")["state"]
		self.assertEqual(row["floors"], {"Weekly": 400, "Biweekly": 832, "Semimonthly": 912, "Monthly": 1792})
		self.assertEqual(row["support_floors"]["Weekly"], 254)
		self.assertEqual(payroll_settings.garnishment_limits("2026-06-30", "OR")["state"]["floors"]["Weekly"], 338)

	def test_washington(self):
		row = payroll_settings.garnishment_limits("2026-08-01", "WA")["state"]
		self.assertEqual((row["floors"]["Weekly"], row["consumer"]["floors"]["Weekly"]), (253.75, 599.55))
		self.assertEqual(row["spousal_maintenance"]["rate"], 0.5)
		self.assertEqual(row["private_student_loan"]["floor_multiple"], 50)


class TheMostProtective(V12TestCase):
	def test_oregons_floor_beats_federal(self):
		got = take(500, [order("Wage Garnishment", 300, "Other judgment")], "OR")
		self.assertEqual(got["lines"][0]["amount"], 100.0, "500 - 400 (ORS 18.385) is less than 25% (125)")

	def test_oregon_support_takes_the_lower_of_federal_and_the_support_floor(self):
		got = take(400, [order("Child Support", 400, "Child support")], "OR")
		self.assertEqual(got["lines"][0]["amount"], 146.0, "400 - 254 is less than 50% (200)")
		self.assertEqual(len(got["child_support"]["limits_considered"]), 2)
		got = take(600, [order("Child Support", 400, "Child support")], "OR")
		self.assertEqual(got["lines"][0]["amount"], 300.0, "50% (300) is less than 600 - 254")

	def test_washington_spousal_maintenance_is_half(self):
		rows = [order("Child Support", 900, "Spousal support", supports_other_dependents=0)]
		got = take(1000, rows, "WA")
		self.assertEqual(got["lines"][0]["amount"], 500.0, "WA 50% beats the federal 60%")

	def test_washington_consumer_debt(self):
		got = take(800, [order("Wage Garnishment", 400, "Consumer debt")], "WA")
		self.assertEqual(got["lines"][0]["amount"], 160.0, "20% (160) is less than 800 - 599.55")

	def test_washington_private_student_loan(self):
		got = take(1200, [order("Student Loan", 400, "Private student loan")], "WA")
		self.assertEqual(got["lines"][0]["amount"], 117.5, "1200 - 50 × 21.65 is less than 15% (180)")
		self.assertIn("private student loans", got["lines"][0]["limits_considered"][-1]["rule"])

	def test_a_federal_student_loan_is_fifteen_percent(self):
		got = take(1000, [order("Student Loan", 400, "Federal student loan (AWG)")], "WA")
		self.assertEqual(got["lines"][0]["amount"], 150.0)

	def test_support_comes_first(self):
		rows = [order("Wage Garnishment", 300, "Other judgment"), order("Child Support", 200, "Child support")]
		got = take(1000, rows, "OR")
		self.assertEqual([line["debt_type"] for line in got["lines"]], ["Child support", "Other judgment"])
		self.assertEqual(got["lines"][1]["amount"], 50.0, "the 25% pool (250) less the 200 support took")


class TheDebtType(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, allow_create_payroll_deduction=1)
		from .harness import STORE

		STORE.seed("Employee", [{"name": "EMP-1", "employee_name": "Beto", "company": "Example Trading Co",
		                         "status": "Active"}])
		STORE.commit()

	def create(self, **args):
		return self.tool("create_payroll_deduction", {"employee": "EMP-1", "company": "Example Trading Co",
		                                              "amount": 100, **args})

	def test_a_creditor_order_must_say_what_it_is_for(self):
		got = self.create(deduction_category="Wage Garnishment", reference="C-1")
		self.assertTrue(got.get("isError"))
		self.assertIn("debt_type is required", got["content"][0]["text"])

	def test_support_is_filled_in(self):
		got = self.tool_data("create_payroll_deduction", {"employee": "EMP-1", "company": "Example Trading Co",
		                                                  "amount": 100, "deduction_category": "Child Support",
		                                                  "reference": "S-1"})
		self.assertEqual(got["deduction"]["debt_type"], "Child support")

	def test_an_older_row_without_one_is_derived_and_flagged(self):
		got = take(800, [order("Wage Garnishment", 100, consumer_debt=1)], "WA")
		self.assertEqual(got["lines"][0]["debt_type"], "Consumer debt")
		self.assertEqual(len(got["debt_type_derived"]), 1)

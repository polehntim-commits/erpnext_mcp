# SPDX-License-Identifier: MIT
"""Washington's "highest minimum wage law in the state" for private student-loan garnishments, and its yearly review.
v0.315.0 (Tim, 2026-10-11: "go with the most accurate").

2026: Burien's large-employer rate, $21.78 (the city's own page; council clarification 2026-05-18) — the highest current
official figure and the most protective. L&I's February table ($21.63) predates the council action; litigation is
pending. The row carries its source and note, and two review reminders on the payroll calendar: October 15 (cities
publish next year's rates — done once a row dated January 1 is published) and January 31 (L&I's table — done once
lni_checked_on is recorded that year).
"""

from unittest import mock

import frappe

from erpnext_mcp import payroll_calendar, payroll_deductions, payroll_settings

from .fixtures import MAIN, V12TestCase


def wa_row(day="2026-08-01"):
	return payroll_settings.garnishment_limits(day, "WA")["state"]


class TheFigure(V12TestCase):
	def test_burien_2026_with_its_source_and_the_discrepancy_noted(self):
		loan = wa_row()["private_student_loan"]
		self.assertEqual((loan["highest_local_minimum_wage"], loan["highest_local_minimum_wage_city"]), (21.78, "Burien"))
		self.assertEqual(loan["highest_local_minimum_wage_source"],
		                 "https://burienwa.gov/city_hall/laws_regulations/minimum_wage")
		self.assertIn("$21.63", loan["highest_local_minimum_wage_note"])
		self.assertIn("litigation", loan["highest_local_minimum_wage_note"])

	def test_the_exemption_floor_is_fifty_times_it(self):
		got = payroll_deductions.apply_garnishments(
			1200, [{"name": "G", "deduction_type": "Garnishment", "deduction_category": "Student Loan",
			        "debt_type": "Private student loan", "amount_type": "Fixed", "amount": 400, "status": "Active",
			        "effective_from": "2026-01-01"}],
			pay_frequency="Weekly", limits=payroll_settings.garnishment_limits("2026-08-01", "WA"))
		self.assertEqual(got["lines"][0]["amount"], 111.0, "1200 − 50 × 21.78")

	def test_a_figure_without_its_source_is_refused(self):
		body = {**payroll_settings.seed_body(payroll_settings.GARNISHMENT_LIMITS_KEY), "effective_from": "2025-01-01"}
		rows = [dict(r) for r in body["rows"]]
		wa = next(r for r in rows if r.get("state") == "WA")
		wa["private_student_loan"] = {k: v for k, v in wa["private_student_loan"].items()
		                              if k != "highest_local_minimum_wage_source"}
		body["rows"] = rows
		errors = " ".join(payroll_settings.validate(body, payroll_settings.GARNISHMENT_LIMITS_KEY)["errors"])
		self.assertIn("highest_local_minimum_wage_source", errors)


class TheReview(V12TestCase):
	def setUp(self):
		super().setUp()
		patcher = mock.patch.object(frappe.utils, "today", lambda: "2026-10-11")
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_october_15_is_on_the_payroll_calendar_and_due(self):
		rows = [r for r in payroll_calendar.items([MAIN]) if r.get("kind") == "garnishment_limit_review"]
		october = next(r for r in rows if r["due_date"] == "2026-10-15")
		self.assertEqual(october["state"], "due")
		self.assertIn("October 15", october["subtitle"])
		self.assertEqual(len([r for r in rows if r["name"] == october["name"]]), 1, "one row, not one per company")

	def test_a_2027_row_clears_it_and_january_is_already_checked_for_2026(self):
		rows = payroll_settings.SEED_GARNISHMENT_LIMITS
		wa = next(r for r in rows if r.get("state") == "WA")
		with_2027 = [*rows, {**wa, "effective_from": "2027-01-01", "reviews": wa["reviews"]}]
		with mock.patch.object(payroll_settings, "SEED_GARNISHMENT_LIMITS", with_2027):
			got = payroll_settings.limit_review_rows(2026, "2026-01-01", "2026-12-31")
		october = next(r for r in got if r["due_date"] == "2026-10-15")
		self.assertTrue(october["done"])
		january = payroll_settings.limit_review_rows(2026, "2026-01-01", "2026-12-31")
		self.assertTrue(next(r for r in january if r["due_date"] == "2026-01-31")["done"], "lni_checked_on 2026-10-11")

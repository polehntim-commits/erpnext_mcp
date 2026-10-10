# SPDX-License-Identifier: MIT
"""Pay statements as Oregon requires them. v0.299.0 (Tim, 2026-10-10: align with BOLI).

ORS 652.610(1)(b) / OAR 839-020-0012: the date of payment and the dates of work; the employer's name, ID, address and
PHONE; each rate and whether by the hour or piece; for piece rate, the pieces completed at each rate and the pay for
each; regular and overtime hours, rates and pay; the amount AND PURPOSE of each deduction. ORS 652.610(5): a written
explanation of earnings and deductions at hire, updated each January.
"""

import unittest

from erpnext_mcp import form_pdf_renderer, pay_stub_pdf
from erpnext_mcp.tools import payroll

from .fixtures import MAIN
from .harness import STORE, frappe
from .test_payroll_integration import ON, WORKER, IntegrationTestCase, member, shift

needs_reportlab = unittest.skipUnless(form_pdf_renderer.available(), "reportlab is not installed on this bench")


def lines(stub):
	return {line["label"]: line for line in pay_stub_pdf.earnings_lines(stub)}


class TheItemisation(unittest.TestCase):
	def test_piece_work_rest_and_the_overtime_rate(self):
		stub = {"pay_type": "Piece Rate", "earned_gross": 832.0, "total_hours": 50, "overtime_hours": 2,
		        "earnings_detail": {"pay_type": "Piece Rate", "piece_units": 500, "piece_rate": 1.5,
		                            "piece_earnings": 750.0, "break_pay": 67.0, "overtime_pay": 15.0,
		                            "overtime_regular_rate": 15.0, "overtime_hours": 2}}
		got = lines(stub)
		self.assertEqual((got["Piece work"]["quantity"], got["Piece work"]["amount"]), ("500.00", 750.0))
		self.assertEqual(got["Paid rest periods (at the average piece rate)"]["amount"], 67.0)
		ot = next(v for k, v in got.items() if k.startswith("Overtime premium"))
		self.assertIn("15.00", ot["label"])
		self.assertEqual((ot["quantity"], ot["amount"]), (pay_stub_pdf._hours(2), 15.0))
		self.assertNotIn("Other earnings", got, "nothing left unexplained")

	def test_each_piece_rate_on_its_own_line(self):
		stub = {"pay_type": "Mixed", "earned_gross": 425.0,
		        "earnings_detail": {"pay_type": "Mixed", "segments": [
		            {"pay_type": "Piece Rate", "rate": 1.5, "piece_units": 100, "hours": 5},
		            {"pay_type": "Piece Rate", "rate": 2.0, "piece_units": 50, "hours": 3},
		            {"pay_type": "Hourly", "rate": 20.0, "hours": 5}]}}
		piece = [line for line in pay_stub_pdf.earnings_lines(stub) if line["label"] == "Piece work"]
		self.assertEqual([(p["rate"], p["amount"]) for p in piece], [("1.50", 150.0), ("2.00", 100.0)])

	def test_bonuses_are_their_own_lines(self):
		stub = {"earnings_detail": {"pay_items": [{"kind": "Production Bonus", "amount": 200, "treatment": "Wages"},
		                                          {"kind": "Profit Share", "amount": 900,
		                                           "treatment": "Not subject to withholding"}]}}
		self.assertEqual([i["label"] for i in pay_stub_pdf.pay_item_lines(stub)],
		                 ["Production Bonus", "Profit Share (not subject to withholding)"])

	def test_each_deduction_says_what_it_is_for(self):
		stub = {"deduction_lines": [{"label": "Child support", "amount": 120, "deduction_category": "child_support",
		                             "deduction_type": "garnishment", "reference": "CASE-9"},
		                            {"label": "Union dues", "amount": 10, "deduction_category": "union_dues"}]}
		labels = [line["label"] for line in pay_stub_pdf.deduction_lines(stub)]
		self.assertIn("Child support — court-ordered child support, case CASE-9", labels)
		self.assertEqual(labels[-1], "Union dues", "a label that already says it is not repeated")


class ThroughARun(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **{**ON, "allow_render_pay_stub": 1})
		self.structure(WORKER, rate=20.0)
		self.seed_shifts(*[shift(f"P{i}", f"2025-06-0{2 + i}", crew=[member(WORKER)]) for i in range(5)])
		STORE.commit()
		self.run = self.tool_data("run_payroll_for_period", {"company": MAIN, "pay_period_start": "2025-06-01",
		                                                     "pay_period_end": "2025-06-14", "pay_date": "2025-06-17"})
		STORE.commit()

	def test_the_run_records_the_pay_date(self):
		self.assertEqual(str(frappe.db.get_value("Farm Payroll Entry", self.run["name"], "pay_date")), "2025-06-17")

	def test_the_stored_slip_carries_its_itemisation(self):
		slip, _name = payroll._stub_slip(frappe.get_doc("Farm Payroll Entry", self.run["name"]), WORKER)
		self.assertEqual(slip["earnings_detail"]["pay_type"], "Hourly")
		self.assertIn("regular_rate_by_week", slip["earnings_detail"])

	def test_the_employer_block_has_a_phone(self):
		info = payroll._stub_company_info(MAIN, {"company_phone": "541-555-0100"})
		self.assertEqual(info["phone"], "541-555-0100")

	@needs_reportlab
	def test_the_stub_renders(self):
		got = self.tool_data("render_pay_stub", {"payroll_entry": self.run["name"], "employee": WORKER})
		self.assertTrue(got["file_url"])

	@needs_reportlab
	def test_the_written_explanation(self):
		got = self.tool_data("render_pay_stub", {"employee": WORKER, "explanation": 1, "payday": "every other Friday"})
		self.assertIn("652.610(5)", got["source"])
		self.assertTrue(got["file_url"])

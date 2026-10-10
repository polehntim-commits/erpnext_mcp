# SPDX-License-Identifier: MIT
"""Deductions, paydays and final pay as Oregon law requires. v0.300.0 (Tim, 2026-10-10: align with BOLI).

* ORS 652.610(3): a voluntary deduction needs the worker's written authorization (or a collective bargaining
  agreement); court orders are required by law. Without one it is not taken, and the run says why.
* ORS 652.120: payday at least every 35 days.
* ORS 652.140 / 652.145: final pay deadlines; seasonal farmworkers are paid immediately.
"""

from erpnext_mcp import final_pay, payroll_deductions

from .fixtures import MAIN, V12TestCase
from .harness import STORE, frappe
from .test_payroll_integration import ON, PERIOD_END, PERIOD_START, WORKER, IntegrationTestCase, member, shift


class TheAuthorizationRule(V12TestCase):
	def test_an_election_needs_a_signature(self):
		row = {"deduction_type": "Voluntary", "deduction_category": "Union Dues"}
		self.assertIn("652.610(3)", payroll_deductions.authorization_missing(row))
		self.assertEqual(payroll_deductions.authorization_missing({**row, "authorized_on": "2026-01-05"}), "")
		self.assertEqual(payroll_deductions.authorization_missing(
			{**row, "legal_authority": "Collective bargaining agreement"}), "")

	def test_a_court_order_is_required_by_law(self):
		self.assertEqual(payroll_deductions.authorization_missing(
			{"deduction_type": "Garnishment", "deduction_category": "Child Support"}), "")


class InARun(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **{**ON, "allow_create_payroll_deduction": 1, "allow_update_payroll_deduction": 1})
		self.structure(WORKER, rate=20.0)
		self.seed_shifts(*[shift(f"D{i}", f"2025-06-0{2 + i}", crew=[member(WORKER)]) for i in range(5)])
		STORE.commit()

	def preview(self, **extra):
		return self.tool_data("preview_payroll_for_period", {"company": MAIN, "pay_period_start": PERIOD_START,
		                                                     "pay_period_end": PERIOD_END, **extra})

	def test_unsigned_dues_are_not_taken_and_the_run_says_why(self):
		self.tool_data("create_payroll_deduction", {"employee": WORKER, "company": MAIN, "deduction_category":
		                                            "Union Dues", "amount": 15, "effective_from": "2025-01-01"})
		STORE.commit()
		got = self.preview()
		slip = got["slips"][0]
		self.assertEqual(slip.get("voluntary_deduction_total") or 0, 0)
		self.assertIn("652.610(3)", got["deductions"]["not_taken_unauthorized"][0]["reason"])

	def test_signed_dues_are_taken(self):
		self.tool_data("create_payroll_deduction", {"employee": WORKER, "company": MAIN, "deduction_category":
		                                            "Union Dues", "amount": 15, "effective_from": "2025-01-01",
		                                            "authorized_on": "2025-01-02"})
		STORE.commit()
		self.assertEqual(self.preview()["deductions"]["not_taken_unauthorized"], [])

	def test_a_court_order_defaults_to_required_by_law(self):
		got = self.tool_data("create_payroll_deduction", {"employee": WORKER, "company": MAIN, "deduction_category":
		                                                  "Child Support", "amount": 50, "effective_from": "2025-01-01"})
		self.assertEqual(got["deduction"]["legal_authority"], "Required by law")

	def test_a_pay_period_over_35_days_is_refused(self):
		message = self.tool_error("preview_payroll_for_period", {"company": MAIN, "pay_period_start": "2025-06-01",
		                                                         "pay_period_end": "2025-07-10"})
		self.assertIn("652.120", message)


class FinalPay(V12TestCase):
	def test_discharged_by_the_next_business_day(self):
		got = final_pay.due("2026-10-09", "Discharged")  # a Friday
		self.assertEqual(got["due_date"], "2026-10-12")
		self.assertIn("652.140(1)", got["rule"])

	def test_quit_with_notice_is_the_last_day(self):
		self.assertEqual(final_pay.due("2026-10-09", "Quit with 48+ hours notice")["due_date"], "2026-10-09")

	def test_quit_without_notice_is_five_business_days(self):
		self.assertEqual(final_pay.due("2026-10-09", "Quit without notice")["due_date"], "2026-10-16")

	def test_a_seasonal_farmworker_is_paid_immediately(self):
		got = final_pay.due("2026-10-09", "End of season", seasonal=True)
		self.assertEqual(got["due_date"], "2026-10-09")
		self.assertIn("652.145", got["rule"])
		self.assertEqual(final_pay.due("2026-10-09", "Quit without notice", seasonal=True)["due_date"], "2026-10-11")


class OnTheCalendar(IntegrationTestCase):
	def test_the_deadline_shows_until_paid(self):
		final_pay.install_fields()
		today = str(frappe.utils.today())[:10]
		frappe.db.set_value("Employee", WORKER, {"relieving_date": today, "seasonal_farmworker": 1})
		STORE.commit()
		rows = final_pay.calendar_rows(MAIN, "2000-01-01", "2100-01-01")
		self.assertEqual(rows[0]["due_date"], today)
		frappe.db.set_value("Employee", WORKER, "final_paid_on", today)
		STORE.commit()
		self.assertEqual(final_pay.calendar_rows(MAIN, "2000-01-01", "2100-01-01"), [])


class TheSignatureBox(V12TestCase):
	def test_the_worker_signs_their_own_authorization(self):
		from erpnext_mcp.tools import signatures

		box = signatures._box({"doctype": "Farm Payroll Deduction"})
		self.assertEqual((box.field, box.signer_role), ("authorization_signature", "employee"))
		self.assertIn("652.610(3)", box.form_label)

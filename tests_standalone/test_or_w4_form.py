# SPDX-License-Identifier: MIT
"""Form OR-W-4 as its own form kind. v0.294.0.

The OR-W-4 is recorded on its own (`submit_w4 form=or_w4`) with line 4a, the Redetermination box, its own signature
date, and what Oregon withholding is based on — the 2026 instructions' fallback order, or a refused certificate. A
certificate the DOR must see is flagged with a send-by date. `list_employees_missing_w4 form=or_w4` lists who is on
the flat 8% and what is still to send.
"""

import unittest

from erpnext_mcp import or_w4_pdf
from erpnext_mcp.tools import signatures, w4

from .fixtures import MAIN
from .harness import STORE, frappe
from .test_payroll_integration import (
	PERIOD_END,
	PERIOD_START,
	PICKER,
	WORKER,
	IntegrationTestCase,
	member,
	shift,
)

ON = {f"allow_{n}": 1 for n in ("submit_w4", "preview_payroll_for_period", "create_salary_structure",
                                "list_employees_missing_w4", "get_w4", "render_w4_pdf")}
FEDERAL = {"employee": WORKER, "company": MAIN, "tax_year": 2025, "filing_status": "Single or Married Filing Separately"}


class OrW4Case(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		frappe.db.set_value("State Tax Configuration", "STC-OR-2025", "or_income_tax_enabled", 1)
		self.seed_shifts(*[shift(f"S{i}", f"2025-06-{2 + i:02d}", state="OR", end="14:00:00", crew=[member(WORKER)])
		                   for i in range(5)])
		self.structure(WORKER, pay_type="Hourly", rate=20.0)
		STORE.commit()

	def or_w4(self, **extra):
		got = self.tool_data("submit_w4", {"form": "or_w4", "employee": WORKER, "company": MAIN, **extra})
		STORE.commit()
		return got

	def income(self):
		got = self.tool_data("preview_payroll_for_period", {"company": MAIN, "pay_period_start": PERIOD_START,
		                                                    "pay_period_end": PERIOD_END, "detail": 1})
		slip = next(s for s in got["slips"] if s["employee"] == WORKER)
		return got, slip["state_taxes_detail"]["OR"]["computation_detail"]["income_tax"]


class OnItsOwn(OrW4Case):
	def test_an_or_w4_alone_moves_oregon_off_eight_percent(self):
		got = self.or_w4(or_filing_status="Single", or_allowances=2)
		self.assertEqual(got["form"], "OR-W-4")
		run, income = self.income()
		self.assertEqual((income["method"], income["allowances"], income["based_on"]), ("formula", 2, "OR-W-4"))
		self.assertEqual(run["oregon_flat_rate_no_or_w4"], [])

	def test_it_is_not_a_federal_w4(self):
		self.or_w4(or_filing_status="Single", or_allowances=1)
		self.assertEqual(w4.federal_row_name(WORKER), "")
		missing = self.tool_data("list_employees_missing_w4", {"company": MAIN})
		self.assertIn(WORKER, [e["name"] for e in missing["employees"]], "still no federal W-4")

	def test_a_federal_w4_does_not_supersede_it(self):
		first = self.or_w4(or_filing_status="Married", or_allowances=3)
		self.tool_data("submit_w4", FEDERAL)
		STORE.commit()
		self.assertEqual(frappe.db.get_value("W-4 Form", first["name"], "status"), "Active")
		self.assertEqual(w4.oregon_for(WORKER)["allowances"], 3)

	def test_a_new_or_w4_supersedes_the_old(self):
		first = self.or_w4(or_filing_status="Single", or_allowances=1)
		second = self.or_w4(or_filing_status="Single", or_allowances=0, or_redetermination=1)
		self.assertEqual(frappe.db.get_value("W-4 Form", first["name"], "status"), "Superseded")
		self.assertEqual(second["superseded"], [first["name"]])
		self.assertEqual(frappe.db.get_value("W-4 Form", second["name"], "or_redetermination"), 1)
		self.assertTrue(frappe.db.get_value("W-4 Form", second["name"], "or_signed_at"))

	def test_its_own_signature_box(self):
		box = signatures._box({"doctype": "W-4 Form", "field": "or_signature"})
		self.assertEqual((box.signed_at_field, box.signer_role), ("or_signed_at", "employee"))
		self.assertEqual(signatures._box({"doctype": "W-4 Form"}).field, "signature", "unnamed is still the federal box")

	@unittest.skipUnless(or_w4_pdf.HAVE_PYPDF, "pypdf is not installed on this bench")
	def test_the_dors_own_form_is_rendered(self):
		got = self.or_w4(or_filing_status="Single", or_allowances=2)
		STORE.seed("File", [])
		rendered = w4.render_w4_pdf({"form": "or_w4", "employee": WORKER}).data
		self.assertEqual((rendered["form"], rendered["name"]), ("OR-W-4", got["name"]))
		self.assertIn("150-101-402", rendered["edition"])
		self.assertFalse(rendered["esignature"]["confirmed"])

	def test_the_federal_lookup_never_lands_on_an_or_w4(self):
		self.or_w4(or_filing_status="Single", or_allowances=2)
		with self.assertRaisesRegex(Exception, "no active W-4 Form"):
			w4._resolve_w4({"employee": WORKER})


class FallbackOrder(OrW4Case):
	def test_a_pre_2020_federal_w4_is_used_before_eight_percent(self):
		self.or_w4(or_source="Pre-2020 federal W-4", or_filing_status="Married", or_allowances=4)
		_run, income = self.income()
		self.assertEqual((income["method"], income["allowances"]), ("formula", 4))
		self.assertEqual(income["based_on"], "Pre-2020 federal W-4")

	def test_a_refused_certificate_is_single_with_none(self):
		self.or_w4(or_source="Refused", or_filing_status="Married", or_allowances=5)
		_run, income = self.income()
		self.assertEqual((income["method"], income["allowances"], income["based_on"]), ("formula", 0, "Refused"))

	def test_an_unknown_source_is_refused(self):
		message = self.tool_error("submit_w4", {"form": "or_w4", "employee": WORKER, "company": MAIN,
		                                        "or_source": "a note from home"})
		self.assertIn("or_source is one of", message)


class SendToTheDOR(OrW4Case):
	def test_more_than_ten_allowances(self):
		got = self.or_w4(or_filing_status="Single", or_allowances=11)
		self.assertTrue(got["send_to_dor"])
		self.assertIn("11 allowances", got["send_to_dor_reasons"])
		self.assertEqual(got["send_by"], str(frappe.utils.add_days(frappe.utils.today(), 20))[:10])

	def test_exempt_over_two_hundred_a_week(self):
		got = self.or_w4(or_exempt=1)  # 40 hours at $20 = $800 a week
		self.assertIn("$800.00 a week", got["send_to_dor_reasons"])

	def test_exempt_under_the_threshold_is_not_sent(self):
		got = self.or_w4(or_exempt=1, or_expected_weekly_wages=150)
		self.assertFalse(got["send_to_dor"])

	def test_ten_allowances_is_not_sent(self):
		self.assertFalse(self.or_w4(or_filing_status="Single", or_allowances=10)["send_to_dor"])

	def test_the_missing_list_names_the_flat_rate_and_what_to_send(self):
		self.or_w4(or_filing_status="Single", or_allowances=12)
		got = self.tool_data("list_employees_missing_w4", {"company": MAIN, "form": "or_w4"})
		self.assertIn(PICKER, [e["name"] for e in got["employees"]])
		self.assertNotIn(WORKER, [e["name"] for e in got["employees"]])
		self.assertEqual(got["send_to_dor"][0]["employee"], WORKER)
		frappe.db.set_value("W-4 Form", got["send_to_dor"][0]["form"], "or_forwarded_on", "2025-06-20")
		STORE.commit()
		self.assertEqual(self.tool_data("list_employees_missing_w4", {"company": MAIN, "form": "or_w4"})["send_to_dor"],
		                 [])


class TheEightPercentRules(OrW4Case):
	"""DOR Publication 150-211-602 (Rev. 02-18-22): withhold 8% for an Oregon-only exemption, or an expired one."""

	def test_an_oregon_only_exemption_is_eight_percent_and_sent(self):
		got = self.or_w4(or_exempt=1, or_exemption_code="L", or_federally_exempt="No")
		self.assertIn("Oregon-only", got["send_to_dor_reasons"])
		_run, income = self.income()
		self.assertEqual((income["method"], income["rate"]), ("flat", 8.0))

	def test_exempt_both_ways_is_exempt(self):
		self.or_w4(or_exempt=1, or_exemption_code="L", or_federally_exempt="Yes", or_expected_weekly_wages=150)
		_run, income = self.income()
		self.assertEqual(income["method"], "exempt")

	def test_an_expired_exemption_is_eight_percent(self):
		self.or_w4(or_exempt=1, or_exemption_code="L", or_federally_exempt="Yes", tax_year=2024)
		_run, income = self.income()  # wages paid in 2025
		self.assertEqual(income["method"], "flat")
		self.assertIn("expired", income["note"])

	def test_not_recorded_is_a_warning_not_eight_percent(self):
		got = self.or_w4(or_exempt=1, or_exemption_code="L", or_expected_weekly_wages=150)
		self.assertIn("not recorded", got["warnings"][0])
		_run, income = self.income()
		self.assertEqual(income["method"], "exempt")

	def test_the_exemption_code_comes_off_the_chart(self):
		message = self.tool_error("submit_w4", {"form": "or_w4", "employee": WORKER, "company": MAIN, "or_exempt": 1,
		                                        "or_exemption_code": "Z"})
		self.assertIn("exemption chart", message)
		self.assertEqual(self.or_w4(or_exempt=1, or_exemption_code="l", or_federally_exempt="Yes")["or_exempt"], True)

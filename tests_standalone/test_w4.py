# SPDX-License-Identifier: MIT
"""W-4 + Federal Withholding Engine — the 10 tools, the calc engine, the supersession chain.

NINE CLAIMS.

1. `PureCalcEngine` — the withholding function is pure and deterministic.
2. `SSWageBaseCap` — Social Security stops at the wage base, mid-year.
3. `AdditionalMedicare` — the 0.9% kicks in over the $200k YTD threshold.
4. `FUTACalc` — employer-only, capped at the wage base minus state credit.
5. `ReadTools` — every read tool returns the right data.
6. `SubmitAndSupersede` — the W-4 superseding pattern works.
7. `CalcToolsEndToEnd` — the calc/preview tools read a W-4 and produce results.
8. `EdgeCases` — zero income, multiple jobs, extra withholding, head of household.
9. `The2026DependentsCredit` — $2,200 a qualifying child on the 2026 form, and
   the patch that restates the rows filed at $2,000 before v0.48.1.
"""

import frappe

from erpnext_mcp.withholding import (
	ANNUAL_BRACKETS,
	PERIODS_PER_YEAR,
	calculate_federal_withholding,
)

from .fixtures import MAIN, V12TestCase, install_hrms
from .harness import STORE

W4_TOOLS_ON = {
	f"allow_{name}": 1
	for name in (
		"get_w4",
		"list_w4_forms",
		"get_fica_config",
		"get_federal_tax_table",
		"preview_federal_withholding",
		"list_employees_missing_w4",
		"calculate_payroll_taxes",
		"submit_w4",
		"update_fica_config",
		"import_federal_tax_table",
	)
}


class W4TestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **W4_TOOLS_ON)
		install_hrms()
		self._seed_fica()
		self._seed_brackets()
		self._seed_employee()

	def _seed_fica(self):
		STORE.singles["FICA Configuration"] = {
			"doctype": "FICA Configuration",
			"tax_year": "2025",
			"social_security_rate_employee": "6.2",
			"social_security_rate_employer": "6.2",
			"social_security_wage_base": "176100",
			"medicare_rate_employee": "1.45",
			"medicare_rate_employer": "1.45",
			"additional_medicare_threshold": "200000",
			"additional_medicare_rate": "0.9",
			"futa_rate": "6.0",
			"futa_wage_base": "7000",
			"futa_state_credit_max": "5.4",
		}

	def _seed_brackets(self):
		brackets = []
		for filing_status, annual in ANNUAL_BRACKETS.items():
			for period_name, periods in PERIODS_PER_YEAR.items():
				for bracket in annual:
					floor = bracket["bracket_floor"] / periods
					ceiling = bracket["bracket_ceiling"] / periods if bracket["bracket_ceiling"] else None
					base = bracket["base_tax"] / periods
					brackets.append(
						{
							"name": f"TTB-{filing_status[:3]}-{period_name[:3]}-{floor:.0f}",
							"tax_year": 2025,
							"filing_status": filing_status,
							"payroll_period": period_name,
							"bracket_floor": round(floor, 2),
							"bracket_ceiling": round(ceiling, 2) if ceiling else None,
							"base_tax": round(base, 2),
							"marginal_rate": bracket["marginal_rate"],
						}
					)
		STORE.seed("Federal Tax Table", brackets)

	def _seed_employee(self):
		STORE.seed(
			"Employee",
			[
				{
					"name": "HR-EMP-00001",
					"employee_name": "Test Worker",
					"company": MAIN,
					"status": "Active",
					"date_of_joining": "2025-01-15",
				},
				{
					"name": "HR-EMP-00002",
					"employee_name": "Another Worker",
					"company": MAIN,
					"status": "Active",
					"date_of_joining": "2025-03-01",
				},
			],
		)

	def _submit_w4(
		self,
		employee="HR-EMP-00001",
		filing_status="Single or Married Filing Separately",
		tax_year=2025,
		**kwargs,
	):
		args = {
			"employee": employee,
			"company": MAIN,
			"tax_year": tax_year,
			"filing_status": filing_status,
			**kwargs,
		}
		return self.tool_data("submit_w4", args)

	def _default_w4_data(self):
		return {
			"filing_status": "Single",
			"multiple_jobs": False,
			"additional_income_from_other_jobs": 0,
			"dependents_under_17_count": 0,
			"other_dependents_count": 0,
			"total_dependents_credit": 0,
			"other_income": 0,
			"deductions": 0,
			"extra_withholding_per_period": 0,
		}

	def _default_fica(self):
		return {
			"social_security_rate_employee": 6.2,
			"social_security_rate_employer": 6.2,
			"social_security_wage_base": 176100,
			"medicare_rate_employee": 1.45,
			"medicare_rate_employer": 1.45,
			"additional_medicare_threshold": 200000,
			"additional_medicare_rate": 0.9,
			"futa_rate": 6.0,
			"futa_wage_base": 7000,
			"futa_state_credit_max": 5.4,
		}


# ── Claim 1: the withholding function is pure and deterministic ──────────


class PureCalcEngine(W4TestCase):
	"""The calc engine is a pure function with no side effects."""

	def test_basic_single_biweekly(self):
		"""A single filer earning $2,500 biweekly gets reasonable withholding."""
		w4 = self._default_w4_data()
		fica = self._default_fica()
		brackets = ANNUAL_BRACKETS["Single"]

		result = calculate_federal_withholding(
			gross_pay=2500,
			pay_frequency="Biweekly",
			w4_data=w4,
			ytd_gross=0,
			ytd_ss_withheld=0,
			fica_config=fica,
			tax_table=brackets,
		)

		self.assertIn("federal_income_tax", result)
		self.assertIn("social_security_employee", result)
		self.assertIn("medicare_employee", result)
		self.assertIn("total_employee_tax", result)
		self.assertIn("computation_detail", result)
		self.assertGreater(result["federal_income_tax"], 0)
		self.assertGreater(result["social_security_employee"], 0)
		self.assertGreater(result["medicare_employee"], 0)

	def test_deterministic(self):
		"""Same inputs produce the same outputs every time."""
		w4 = self._default_w4_data()
		fica = self._default_fica()
		brackets = ANNUAL_BRACKETS["Single"]
		kwargs = dict(
			gross_pay=3000,
			pay_frequency="Biweekly",
			w4_data=w4,
			ytd_gross=50000,
			ytd_ss_withheld=3100,
			fica_config=fica,
			tax_table=brackets,
		)
		r1 = calculate_federal_withholding(**kwargs)
		r2 = calculate_federal_withholding(**kwargs)
		self.assertEqual(r1["federal_income_tax"], r2["federal_income_tax"])
		self.assertEqual(r1["social_security_employee"], r2["social_security_employee"])
		self.assertEqual(r1["total_employee_tax"], r2["total_employee_tax"])

	def test_married_filing_jointly_lower_tax(self):
		"""Married filing jointly pays less tax than single on the same income."""
		fica = self._default_fica()
		single_w4 = self._default_w4_data()
		single_w4["filing_status"] = "Single"
		mfj_w4 = self._default_w4_data()
		mfj_w4["filing_status"] = "Married Filing Jointly"

		single = calculate_federal_withholding(
			5000,
			"Biweekly",
			single_w4,
			0,
			0,
			fica,
			ANNUAL_BRACKETS["Single"],
		)
		mfj = calculate_federal_withholding(
			5000,
			"Biweekly",
			mfj_w4,
			0,
			0,
			fica,
			ANNUAL_BRACKETS["Married Filing Jointly"],
		)
		self.assertGreater(single["federal_income_tax"], mfj["federal_income_tax"])

	def test_dependents_reduce_tax(self):
		"""Dependents credits reduce the federal income tax."""
		fica = self._default_fica()
		no_deps = self._default_w4_data()
		with_deps = self._default_w4_data()
		with_deps["dependents_under_17_count"] = 2
		with_deps["total_dependents_credit"] = 4400  # 2 × $2,200

		brackets = ANNUAL_BRACKETS["Single"]
		r_no = calculate_federal_withholding(3000, "Biweekly", no_deps, 0, 0, fica, brackets)
		r_yes = calculate_federal_withholding(3000, "Biweekly", with_deps, 0, 0, fica, brackets)
		self.assertGreater(r_no["federal_income_tax"], r_yes["federal_income_tax"])


# ── Claim 2: SS wage base cap ────────────────────────────────────────────


class SSWageBaseCap(W4TestCase):
	"""Social Security stops at the wage base ($176,100 for 2025)."""

	def test_under_cap(self):
		"""Under the cap, full SS is withheld."""
		result = calculate_federal_withholding(
			5000,
			"Biweekly",
			self._default_w4_data(),
			0,
			0,
			self._default_fica(),
			ANNUAL_BRACKETS["Single"],
		)
		self.assertEqual(result["social_security_employee"], round(5000 * 0.062, 2))

	def test_at_cap_mid_year(self):
		"""When YTD gross is near the cap, only the remaining base is taxed."""
		ytd = 175000  # $1,100 remaining before cap
		result = calculate_federal_withholding(
			5000,
			"Biweekly",
			self._default_w4_data(),
			ytd,
			ytd * 0.062,
			self._default_fica(),
			ANNUAL_BRACKETS["Single"],
		)
		expected_ss = round(1100 * 0.062, 2)
		self.assertEqual(result["social_security_employee"], expected_ss)

	def test_over_cap(self):
		"""When YTD gross exceeds the cap, SS is zero."""
		ytd = 180000
		result = calculate_federal_withholding(
			5000,
			"Biweekly",
			self._default_w4_data(),
			ytd,
			ytd * 0.062,
			self._default_fica(),
			ANNUAL_BRACKETS["Single"],
		)
		self.assertEqual(result["social_security_employee"], 0)
		self.assertEqual(result["social_security_employer"], 0)


# ── Claim 3: Additional Medicare ─────────────────────────────────────────


class AdditionalMedicare(W4TestCase):
	"""The 0.9% additional Medicare kicks in over the $200k YTD threshold."""

	def test_under_threshold(self):
		"""No additional Medicare when under $200k."""
		result = calculate_federal_withholding(
			5000,
			"Biweekly",
			self._default_w4_data(),
			100000,
			0,
			self._default_fica(),
			ANNUAL_BRACKETS["Single"],
		)
		self.assertEqual(result["additional_medicare"], 0)

	def test_crossing_threshold(self):
		"""Additional Medicare on the portion over $200k."""
		ytd = 198000
		gross = 5000  # new YTD = 203,000 → $3,000 over threshold
		result = calculate_federal_withholding(
			gross,
			"Biweekly",
			self._default_w4_data(),
			ytd,
			0,
			self._default_fica(),
			ANNUAL_BRACKETS["Single"],
		)
		expected_addl = round(3000 * 0.009, 2)
		self.assertEqual(result["additional_medicare"], expected_addl)

	def test_fully_over_threshold(self):
		"""All gross is subject to additional Medicare when already over $200k."""
		ytd = 250000
		gross = 5000
		result = calculate_federal_withholding(
			gross,
			"Biweekly",
			self._default_w4_data(),
			ytd,
			0,
			self._default_fica(),
			ANNUAL_BRACKETS["Single"],
		)
		expected_addl = round(5000 * 0.009, 2)
		self.assertEqual(result["additional_medicare"], expected_addl)


# ── Claim 4: FUTA ────────────────────────────────────────────────────────


class FUTACalc(W4TestCase):
	"""FUTA: employer-only, on first $7k, 6% minus 5.4% state credit = 0.6%."""

	def test_under_futa_base(self):
		"""Full FUTA when under the wage base."""
		result = calculate_federal_withholding(
			2000,
			"Biweekly",
			self._default_w4_data(),
			0,
			0,
			self._default_fica(),
			ANNUAL_BRACKETS["Single"],
		)
		expected = round(2000 * 0.006, 2)
		self.assertEqual(result["futa_employer"], expected)

	def test_over_futa_base(self):
		"""No FUTA when YTD exceeds the $7k base."""
		result = calculate_federal_withholding(
			2000,
			"Biweekly",
			self._default_w4_data(),
			8000,
			0,
			self._default_fica(),
			ANNUAL_BRACKETS["Single"],
		)
		self.assertEqual(result["futa_employer"], 0)

	def test_crossing_futa_base(self):
		"""FUTA only on the portion under the base."""
		ytd = 6000
		gross = 2000  # only $1,000 remaining
		result = calculate_federal_withholding(
			gross,
			"Biweekly",
			self._default_w4_data(),
			ytd,
			0,
			self._default_fica(),
			ANNUAL_BRACKETS["Single"],
		)
		expected = round(1000 * 0.006, 2)
		self.assertEqual(result["futa_employer"], expected)


# ── Claim 5: read tools ─────────────────────────────────────────────────


class ReadTools(W4TestCase):
	"""Every read tool returns the right data in the right shape."""

	def test_get_fica_config(self):
		data = self.tool_data("get_fica_config", {})
		self.assertEqual(data["tax_year"], 2025)
		self.assertEqual(data["social_security_rate_employee"], 6.2)
		self.assertEqual(data["futa_rate"], 6.0)

	def test_get_federal_tax_table(self):
		data = self.tool_data(
			"get_federal_tax_table",
			{
				"tax_year": 2025,
				"filing_status": "Single",
				"payroll_period": "Annual",
			},
		)
		self.assertGreater(data["count"], 0)
		self.assertEqual(len(data["brackets"]), data["count"])

	def test_list_employees_missing_w4(self):
		data = self.tool_data("list_employees_missing_w4", {"company": MAIN})
		self.assertEqual(data["count"], 2)

	def test_list_w4_forms_empty(self):
		data = self.tool_data("list_w4_forms", {})
		self.assertEqual(data["count"], 0)


# ── Claim 6: submit and supersede ────────────────────────────────────────


class SubmitAndSupersede(W4TestCase):
	"""The W-4 superseding pattern works: new W-4 supersedes the old one."""

	def test_submit_w4(self):
		data = self._submit_w4()
		self.assertEqual(data["status"], "Active")
		self.assertEqual(data["filing_status"], "Single or Married Filing Separately")
		self.assertEqual(data["tax_year"], 2025)

	def test_get_w4_after_submit(self):
		self._submit_w4()
		data = self.tool_data("get_w4", {"employee": "HR-EMP-00001"})
		self.assertEqual(data["status"], "Active")

	def test_supersede_replaces_old(self):
		first = self._submit_w4()
		second = self._submit_w4(filing_status="Married Filing Jointly")
		self.assertEqual(second["superseded"], [first["name"]])

		old = frappe.db.get_value("W-4 Form", first["name"], ["status", "superseded_by"], as_dict=True)
		self.assertEqual(old["status"], "Superseded")
		self.assertEqual(old["superseded_by"], second["name"])

	def test_list_after_submit(self):
		self._submit_w4()
		data = self.tool_data("list_w4_forms", {"company": MAIN})
		self.assertEqual(data["count"], 1)

	def test_missing_w4_decreases(self):
		before = self.tool_data("list_employees_missing_w4", {"company": MAIN})
		self.assertEqual(before["count"], 2)

		self._submit_w4(employee="HR-EMP-00001")
		after = self.tool_data("list_employees_missing_w4", {"company": MAIN})
		self.assertEqual(after["count"], 1)

	def test_dependents_computed(self):
		"""The 2025 edition of Step 3: $2,000 a child under 17, $500 an other."""
		self._submit_w4(tax_year=2025, dependents_under_17_count=3, other_dependents_count=1)
		data = self.tool_data("get_w4", {"employee": "HR-EMP-00001"})
		self.assertEqual(data["dependents_under_17_amount"], "6000")
		self.assertEqual(data["other_dependents_amount"], "500")
		self.assertEqual(data["total_dependents_credit"], "6500")

	def test_dependents_computed_on_the_2026_form(self):
		"""v0.48.1. The IRS raised the under-17 credit to $2,200 for 2026.

		THE YEAR IS WHAT PICKS THE AMOUNT, not the calendar — a 2025 form
		re-saved today still restates itself at $2,000, which the test above
		pins. Only the qualifying-child figure moved; the other-dependent credit
		is still $500 on the 2026 form.
		"""
		self._submit_w4(tax_year=2026, dependents_under_17_count=3, other_dependents_count=1)
		data = self.tool_data("get_w4", {"employee": "HR-EMP-00001"})
		self.assertEqual(data["dependents_under_17_amount"], "6600")
		self.assertEqual(data["other_dependents_amount"], "500")
		self.assertEqual(data["total_dependents_credit"], "7100")


# ── Claim 7: calc tools end to end ───────────────────────────────────────


class CalcToolsEndToEnd(W4TestCase):
	"""The calculate/preview tools read a W-4 and produce results."""

	def test_preview_withholding(self):
		self._submit_w4()
		data = self.tool_data(
			"preview_federal_withholding",
			{
				"employee": "HR-EMP-00001",
				"gross_pay": 2500,
				"pay_frequency": "Biweekly",
			},
		)
		self.assertGreater(data["federal_income_tax"], 0)
		self.assertGreater(data["social_security_employee"], 0)
		self.assertGreater(data["total_employee_tax"], 0)
		self.assertIn("computation_detail", data)

	def test_calculate_payroll_taxes(self):
		self._submit_w4()
		data = self.tool_data(
			"calculate_payroll_taxes",
			{
				"employee": "HR-EMP-00001",
				"gross_pay": 3000,
				"pay_frequency": "Biweekly",
			},
		)
		self.assertGreater(data["federal_income_tax"], 0)
		self.assertEqual(data["social_security_employee"], round(3000 * 0.062, 2))
		self.assertEqual(data["medicare_employee"], round(3000 * 0.0145, 2))

	def test_calc_with_ytd(self):
		self._submit_w4()
		data = self.tool_data(
			"calculate_payroll_taxes",
			{
				"employee": "HR-EMP-00001",
				"gross_pay": 3000,
				"pay_frequency": "Biweekly",
				"ytd_gross": 175000,
			},
		)
		# SS should be capped
		self.assertLess(data["social_security_employee"], round(3000 * 0.062, 2))

	def test_no_w4_errors(self):
		text = self.tool_error(
			"preview_federal_withholding",
			{
				"employee": "HR-EMP-00001",
				"gross_pay": 2500,
				"pay_frequency": "Biweekly",
			},
		)
		self.assertIn("no active W-4", text)


# ── Claim 8: edge cases ─────────────────────────────────────────────────


class EdgeCases(W4TestCase):
	"""Zero income, multiple jobs, extra withholding, head of household."""

	def test_zero_gross(self):
		"""Zero gross pay produces zero taxes."""
		result = calculate_federal_withholding(
			0,
			"Biweekly",
			self._default_w4_data(),
			0,
			0,
			self._default_fica(),
			ANNUAL_BRACKETS["Single"],
		)
		self.assertEqual(result["federal_income_tax"], 0)
		self.assertEqual(result["social_security_employee"], 0)
		self.assertEqual(result["medicare_employee"], 0)
		self.assertEqual(result["total_employee_tax"], 0)

	def test_extra_withholding(self):
		"""Extra withholding per period is added to federal income tax."""
		w4 = self._default_w4_data()
		w4_extra = self._default_w4_data()
		w4_extra["extra_withholding_per_period"] = 50

		fica = self._default_fica()
		brackets = ANNUAL_BRACKETS["Single"]
		r_base = calculate_federal_withholding(3000, "Biweekly", w4, 0, 0, fica, brackets)
		r_extra = calculate_federal_withholding(3000, "Biweekly", w4_extra, 0, 0, fica, brackets)
		self.assertAlmostEqual(
			r_extra["federal_income_tax"] - r_base["federal_income_tax"],
			50,
			places=2,
		)

	def test_deductions_reduce_tax(self):
		"""Step 4(b) deductions reduce taxable income."""
		w4_no = self._default_w4_data()
		w4_ded = self._default_w4_data()
		w4_ded["deductions"] = 10000

		fica = self._default_fica()
		brackets = ANNUAL_BRACKETS["Single"]
		r_no = calculate_federal_withholding(3000, "Biweekly", w4_no, 0, 0, fica, brackets)
		r_ded = calculate_federal_withholding(3000, "Biweekly", w4_ded, 0, 0, fica, brackets)
		self.assertGreater(r_no["federal_income_tax"], r_ded["federal_income_tax"])

	def test_other_income_increases_tax(self):
		"""Step 4(a) other income increases the taxable income."""
		w4_no = self._default_w4_data()
		w4_oi = self._default_w4_data()
		w4_oi["other_income"] = 5000

		fica = self._default_fica()
		brackets = ANNUAL_BRACKETS["Single"]
		r_no = calculate_federal_withholding(3000, "Biweekly", w4_no, 0, 0, fica, brackets)
		r_oi = calculate_federal_withholding(3000, "Biweekly", w4_oi, 0, 0, fica, brackets)
		self.assertGreater(r_oi["federal_income_tax"], r_no["federal_income_tax"])

	def test_head_of_household(self):
		"""Head of household gets a wider zero bracket than single."""
		fica = self._default_fica()
		single = calculate_federal_withholding(
			2000,
			"Biweekly",
			self._default_w4_data(),
			0,
			0,
			fica,
			ANNUAL_BRACKETS["Single"],
		)
		hoh_w4 = self._default_w4_data()
		hoh_w4["filing_status"] = "Head of Household"
		hoh = calculate_federal_withholding(
			2000,
			"Biweekly",
			hoh_w4,
			0,
			0,
			fica,
			ANNUAL_BRACKETS["Head of Household"],
		)
		self.assertGreaterEqual(single["federal_income_tax"], hoh["federal_income_tax"])

	def test_all_periods(self):
		"""The engine works for every pay frequency."""
		w4 = self._default_w4_data()
		fica = self._default_fica()
		brackets = ANNUAL_BRACKETS["Single"]
		for period in PERIODS_PER_YEAR:
			result = calculate_federal_withholding(
				5000,
				period,
				w4,
				0,
				0,
				fica,
				brackets,
			)
			self.assertIn("federal_income_tax", result, f"failed for {period}")

	def test_effective_rate(self):
		"""Effective rate is total employee tax / gross pay × 100."""
		result = calculate_federal_withholding(
			5000,
			"Biweekly",
			self._default_w4_data(),
			0,
			0,
			self._default_fica(),
			ANNUAL_BRACKETS["Single"],
		)
		expected = round(result["total_employee_tax"] / 5000 * 100, 4)
		# To 3 places: `total_employee_tax` is already rounded to the cent, the rate is not (v0.286.1).
		self.assertAlmostEqual(result["effective_rate"], expected, places=3)

	def test_empty_brackets(self):
		"""Empty bracket table produces zero federal income tax."""
		result = calculate_federal_withholding(
			3000,
			"Biweekly",
			self._default_w4_data(),
			0,
			0,
			self._default_fica(),
			[],
		)
		self.assertEqual(result["federal_income_tax"], 0)


# ── Claim 9: the 2026 credit, and the rows filed before it was right ─────


class The2026DependentsCredit(W4TestCase):
	"""v0.48.1. $2,200 a qualifying child on the 2026 form, and the backfill.

	The controller computed a flat $2,000 — the 2020-2025 figure — so a 2026 W-4
	filed before this release holds a Step 3 total that is $200 a child short.
	Two things read that STORED number rather than recomputing it: the
	withholding engine, which subtracts it from the tentative tax, and
	`w4_pdf`, which prints it into box 3 of the government form.
	"""

	def _w4_row(self):
		return next(iter(STORE.rows("W-4 Form")))

	def test_a_2026_form_credits_2200_a_child(self):
		self._submit_w4(tax_year=2026, dependents_under_17_count=2)
		self.assertEqual(float(self._w4_row()["dependents_under_17_amount"]), 4400.0)

	def test_a_2025_form_still_credits_2000_a_child(self):
		"""The amount belongs to the edition, not to the calendar."""
		self._submit_w4(tax_year=2025, dependents_under_17_count=2)
		self.assertEqual(float(self._w4_row()["dependents_under_17_amount"]), 4000.0)

	def test_the_other_dependent_credit_is_500_on_both_editions(self):
		"""The 2026 form did not move this one — only the under-17 figure."""
		self._submit_w4(tax_year=2025, other_dependents_count=3)
		self._submit_w4(tax_year=2026, other_dependents_count=3)
		for year in (2025, 2026):
			with self.subTest(tax_year=year):
				row = next(r for r in STORE.rows("W-4 Form") if int(r["tax_year"]) == year)
				self.assertEqual(float(row["other_dependents_amount"]), 1500.0)

	def test_the_patch_restates_a_row_filed_at_the_old_amount(self):
		from erpnext_mcp.patches import recompute_2026_dependents_credit as patch

		self._submit_w4(tax_year=2026, dependents_under_17_count=2, other_dependents_count=1)
		name = self._w4_row()["name"]
		# The row exactly as v0.48.0 wrote it: 2 × $2,000 + 1 × $500.
		frappe.db.set_value("W-4 Form", name, "dependents_under_17_amount", 4000, update_modified=False)
		frappe.db.set_value("W-4 Form", name, "total_dependents_credit", 4500, update_modified=False)

		report = patch.recompute_2026_dependents_credit()

		row = self._w4_row()
		self.assertEqual(float(row["dependents_under_17_amount"]), 4400.0)
		self.assertEqual(float(row["total_dependents_credit"]), 4900.0)
		self.assertEqual(len(report["restated"]), 1)
		self.assertEqual(report["restated"][0]["was"], 4500.0)
		self.assertEqual(report["restated"][0]["now"], 4900.0)

	def test_the_patch_leaves_a_2025_row_alone(self):
		from erpnext_mcp.patches import recompute_2026_dependents_credit as patch

		self._submit_w4(tax_year=2025, dependents_under_17_count=2)
		patch.recompute_2026_dependents_credit()
		self.assertEqual(float(self._w4_row()["dependents_under_17_amount"]), 4000.0)

	def test_the_patch_is_a_no_op_on_a_row_that_is_already_right(self):
		from erpnext_mcp.patches import recompute_2026_dependents_credit as patch

		self._submit_w4(tax_year=2026, dependents_under_17_count=2)
		report = patch.recompute_2026_dependents_credit()
		self.assertEqual(report["restated"], [])
		self.assertEqual(report["already_right"], 1)

	def test_the_migrate_names_every_restated_form_and_warns_about_posted_slips(self):
		"""Silence would leave an operator with money already withheld and no notice."""
		from erpnext_mcp.patches import recompute_2026_dependents_credit as patch

		self._submit_w4(tax_year=2026, dependents_under_17_count=2)
		name = self._w4_row()["name"]
		frappe.db.set_value("W-4 Form", name, "total_dependents_credit", 4000, update_modified=False)
		lines = patch.report_lines(patch.recompute_2026_dependents_credit())
		printed = "\n".join(lines)
		self.assertIn(name, printed)
		self.assertIn("ALREADY POSTED", printed)
		self.assertIn("$2,200", printed)

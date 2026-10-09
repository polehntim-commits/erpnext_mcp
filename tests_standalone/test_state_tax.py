# SPDX-License-Identifier: MIT
"""State Tax Engines (Oregon + Washington) — v0.29.0.

TEN CLAIMS.

1. `OregonCalcEngine` — Oregon withholding is correct: income tax from brackets,
   transit tax, paid leave, workers comp.
2. `WashingtonCalcEngine` — Washington withholding is correct: PFML, WA Cares,
   L&I. No income tax.
3. `CrossStatePeriod` — an employee with shifts in both OR and WA in the same
   pay period gets each shift's gross under its own state's rules.
4. `CombinedFederalState` — the combined engine runs federal + state together
   and grand totals are correct.
5. `OregonBrackets` — the seeded Oregon 2025 brackets produce the right tax at
   boundary values.
6. `SmallEmployer` — an Oregon small employer (under 25) pays no employer share
   of Paid Leave.
7. `EdgeCases` — zero gross, missing brackets, unsupported state.
8. `ReadTools` — the read-only MCP tools return the right data.
9. `MutatingTools` — create, update, and import tools work correctly.
10. `CombinedPreview` — the preview_total_payroll_taxes tool runs federal + state.
"""

from typing import ClassVar

from erpnext_mcp.state_withholding import (
	OR_ANNUAL_BRACKETS,
	PERIODS_PER_YEAR,
	calculate_all_payroll_taxes,
	calculate_oregon_withholding,
	calculate_state_withholding,
	calculate_washington_withholding,
	seed_or_brackets,
)
from erpnext_mcp.withholding import ANNUAL_BRACKETS

from .fixtures import MAIN, V12TestCase, install_hrms
from .harness import STORE

STATE_TAX_TOOLS_ON = {
	f"allow_{name}": 1
	for name in (
		"get_state_tax_config",
		"list_state_tax_configs",
		"get_state_tax_table",
		"preview_state_withholding",
		"preview_total_payroll_taxes",
		"list_employees_by_work_state",
		"create_state_tax_config",
		"update_state_tax_config",
		"import_state_tax_table",
		# Federal tools needed for combined preview
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


class StateTaxTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **STATE_TAX_TOOLS_ON)
		install_hrms()
		self._seed_fica()
		self._seed_federal_brackets()
		self._seed_employees()
		self._seed_or_config()
		self._seed_wa_config()
		self._seed_or_brackets()

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

	def _seed_federal_brackets(self):
		from erpnext_mcp.withholding import ANNUAL_BRACKETS as FED_BRACKETS

		brackets = []
		for filing_status, annual in FED_BRACKETS.items():
			for period_name, periods in PERIODS_PER_YEAR.items():
				for bracket in annual:
					floor = bracket["bracket_floor"] / periods
					ceiling = bracket["bracket_ceiling"] / periods if bracket["bracket_ceiling"] else None
					base = bracket["base_tax"] / periods
					brackets.append(
						{
							"name": f"FTB-{filing_status[:3]}-{period_name[:3]}-{floor:.0f}",
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

	def _seed_employees(self):
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
					"employee_name": "WA Worker",
					"company": MAIN,
					"status": "Active",
					"date_of_joining": "2025-03-01",
				},
			],
		)

	def _seed_or_config(self):
		STORE.seed(
			"State Tax Configuration",
			[
				{
					"name": "STC-OR-2025",
					"company": MAIN,
					"state": "OR",
					"tax_year": 2025,
					"status": "Active",
					"or_income_tax_enabled": 1,
					"or_transit_tax_rate": 0.1,
					"or_paid_leave_rate": 1.0,
					"or_paid_leave_employee_share": 60,
					"or_paid_leave_employer_share": 40,
					"or_paid_leave_small_employer": 0,
					"or_workers_comp_rate": 1.5,
				}
			],
		)

	def _seed_wa_config(self):
		STORE.seed(
			"State Tax Configuration",
			[
				{
					"name": "STC-WA-2025",
					"company": MAIN,
					"state": "WA",
					"tax_year": 2025,
					"status": "Active",
					"wa_pfml_rate": 0.92,
					"wa_pfml_employee_share": 72.76,
					"wa_pfml_employer_share": 27.24,
					"wa_pfml_wage_base": 176100,
					"wa_cares_rate": 0.58,
					"wa_cares_employee_only": 1,
					"wa_cares_exempt_employees": "",
					"wa_li_rate_employee": 0.25,
					"wa_li_rate_employer": 0.35,
				}
			],
		)

	def _seed_or_brackets(self):
		brackets = []
		for filing_status, annual in OR_ANNUAL_BRACKETS.items():
			for bracket in annual:
				brackets.append(
					{
						"name": f"STB-OR-{filing_status[:3]}-{bracket['bracket_floor']:.0f}",
						"state": "OR",
						"tax_year": 2025,
						"filing_status": filing_status,
						"bracket_floor": bracket["bracket_floor"],
						"bracket_ceiling": bracket["bracket_ceiling"],
						"base_tax": bracket["base_tax"],
						"marginal_rate": bracket["marginal_rate"],
					}
				)
		STORE.seed("State Tax Table", brackets)

	def _default_or_config(self):
		return {
			"or_income_tax_enabled": 1,
			"or_transit_tax_rate": 0.1,
			"or_paid_leave_rate": 1.0,
			"or_paid_leave_employee_share": 60,
			"or_paid_leave_employer_share": 40,
			"or_paid_leave_small_employer": 0,
			"or_workers_comp_rate": 1.5,
		}

	def _default_wa_config(self):
		return {
			"wa_pfml_rate": 0.92,
			"wa_pfml_employee_share": 72.76,
			"wa_pfml_employer_share": 27.24,
			"wa_pfml_wage_base": 176100,
			"wa_cares_rate": 0.58,
			"wa_cares_employee_only": 1,
			"wa_li_rate_employee": 0.25,
			"wa_li_rate_employer": 0.35,
		}

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


# ── Claim 1: Oregon calc engine ────────────────────────────────────────


class OregonCalcEngine(StateTaxTestCase):
	"""Oregon withholding is correct."""

	def test_basic_or_biweekly(self):
		"""Oregon withholding on $2,500 biweekly produces all components."""
		config = self._default_or_config()
		brackets = OR_ANNUAL_BRACKETS["Single"]

		result = calculate_oregon_withholding(
			gross_pay=2500,
			pay_frequency="Biweekly",
			filing_status="Single",
			state_config=config,
			state_tax_table=brackets,
		)

		self.assertIn("or_income_tax", result)
		self.assertIn("or_transit_tax", result)
		self.assertIn("or_paid_leave_employee", result)
		self.assertIn("or_paid_leave_employer", result)
		self.assertIn("or_workers_comp", result)
		self.assertIn("total_or_employee", result)
		self.assertIn("total_or_employer", result)
		self.assertGreater(result["or_income_tax"], 0)
		self.assertEqual(result["state"], "OR")

	def test_transit_tax(self):
		"""Transit tax is 0.1% of gross, no cap."""
		config = self._default_or_config()
		result = calculate_oregon_withholding(
			1000,
			"Biweekly",
			"Single",
			config,
			OR_ANNUAL_BRACKETS["Single"],
		)
		self.assertEqual(result["or_transit_tax"], round(1000 * 0.001, 2))

	def test_paid_leave_split(self):
		"""Paid leave is 1% split 60/40 employee/employer."""
		config = self._default_or_config()
		result = calculate_oregon_withholding(
			1000,
			"Biweekly",
			"Single",
			config,
			OR_ANNUAL_BRACKETS["Single"],
		)
		total_paid_leave = 1000 * 0.01
		self.assertEqual(result["or_paid_leave_employee"], round(total_paid_leave * 0.60, 2))
		self.assertEqual(result["or_paid_leave_employer"], round(total_paid_leave * 0.40, 2))

	def test_workers_comp(self):
		"""Workers' comp is the employer-entered rate on gross."""
		config = self._default_or_config()
		result = calculate_oregon_withholding(
			1000,
			"Biweekly",
			"Single",
			config,
			OR_ANNUAL_BRACKETS["Single"],
		)
		self.assertEqual(result["or_workers_comp"], round(1000 * 0.015, 2))

	def test_totals_add_up(self):
		"""Employee and employer totals are the sum of their parts."""
		config = self._default_or_config()
		result = calculate_oregon_withholding(
			3000,
			"Biweekly",
			"Single",
			config,
			OR_ANNUAL_BRACKETS["Single"],
		)
		expected_ee = round(
			result["or_income_tax"] + result["or_transit_tax"] + result["or_paid_leave_employee"], 2
		)
		expected_er = round(result["or_paid_leave_employer"] + result["or_workers_comp"], 2)
		self.assertEqual(result["total_or_employee"], expected_ee)
		self.assertEqual(result["total_or_employer"], expected_er)

	def test_mfj_lower_tax(self):
		"""Married Filing Jointly pays less Oregon income tax than Single."""
		config = self._default_or_config()
		# v0.291.0: the OR-W-4's status decides, not the federal one.
		single = calculate_oregon_withholding(5000, "Biweekly", "Single", config, [],
		                                      or_w4={"on_file": True, "filing_status": "Single"})
		mfj = calculate_oregon_withholding(5000, "Biweekly", "Married Filing Jointly", config, [],
		                                   or_w4={"on_file": True, "filing_status": "Married"})
		self.assertGreater(single["or_income_tax"], mfj["or_income_tax"])


# ── Claim 2: Washington calc engine ────────────────────────────────────


class WashingtonCalcEngine(StateTaxTestCase):
	"""Washington withholding is correct — no income tax."""

	def test_basic_wa(self):
		"""Washington withholding produces PFML, Cares, L&I — no income tax."""
		config = self._default_wa_config()
		result = calculate_washington_withholding(2500, config)

		self.assertIn("wa_pfml_employee", result)
		self.assertIn("wa_pfml_employer", result)
		self.assertIn("wa_cares_employee", result)
		self.assertIn("wa_li_employee", result)
		self.assertIn("wa_li_employer", result)
		self.assertNotIn("income_tax", result)
		self.assertEqual(result["state"], "WA")

	def test_pfml_split(self):
		"""PFML is 0.92% split ~72.76/27.24 employee/employer."""
		config = self._default_wa_config()
		result = calculate_washington_withholding(1000, config)
		total_pfml = 1000 * 0.0092
		self.assertEqual(result["wa_pfml_employee"], round(total_pfml * 0.7276, 2))
		self.assertEqual(result["wa_pfml_employer"], round(total_pfml * 0.2724, 2))

	def test_cares_employee_only(self):
		"""WA Cares is 0.58%, employee-only."""
		config = self._default_wa_config()
		result = calculate_washington_withholding(1000, config)
		self.assertEqual(result["wa_cares_employee"], round(1000 * 0.0058, 2))

	def test_li_rates(self):
		"""L&I uses employer-entered rates for both employee and employer."""
		config = self._default_wa_config()
		result = calculate_washington_withholding(1000, config)
		self.assertEqual(result["wa_li_employee"], round(1000 * 0.0025, 2))
		self.assertEqual(result["wa_li_employer"], round(1000 * 0.0035, 2))

	def test_totals_add_up(self):
		"""WA totals are the sum of their parts."""
		config = self._default_wa_config()
		result = calculate_washington_withholding(3000, config)
		expected_ee = round(
			result["wa_pfml_employee"] + result["wa_cares_employee"] + result["wa_li_employee"], 2
		)
		expected_er = round(result["wa_pfml_employer"] + result["wa_li_employer"], 2)
		self.assertEqual(result["total_wa_employee"], expected_ee)
		self.assertEqual(result["total_wa_employer"], expected_er)


# ── Claim 3: Cross-state pay period ────────────────────────────────────


class CrossStatePeriod(StateTaxTestCase):
	"""An employee with shifts in both OR and WA gets each state's calc."""

	def test_separate_calcs_per_state(self):
		"""OR and WA produce different tax shapes on the same gross."""
		or_config = self._default_or_config()
		wa_config = self._default_wa_config()

		or_result = calculate_state_withholding(
			2000,
			"Biweekly",
			"OR",
			"Single",
			or_config,
			OR_ANNUAL_BRACKETS["Single"],
		)
		wa_result = calculate_state_withholding(
			1000,
			"Biweekly",
			"WA",
			"Single",
			wa_config,
			None,
		)

		self.assertEqual(or_result["state"], "OR")
		self.assertEqual(wa_result["state"], "WA")
		self.assertIn("or_income_tax", or_result)
		self.assertNotIn("or_income_tax", wa_result)
		self.assertIn("wa_pfml_employee", wa_result)
		self.assertNotIn("wa_pfml_employee", or_result)

	def test_summed_totals_for_pay_period(self):
		"""Both states' employee totals sum correctly for a split pay period."""
		or_config = self._default_or_config()
		wa_config = self._default_wa_config()

		or_result = calculate_state_withholding(
			2000,
			"Biweekly",
			"OR",
			"Single",
			or_config,
			OR_ANNUAL_BRACKETS["Single"],
		)
		wa_result = calculate_state_withholding(
			1000,
			"Biweekly",
			"WA",
			"Single",
			wa_config,
			None,
		)

		combined_ee = or_result["total_or_employee"] + wa_result["total_wa_employee"]
		combined_er = or_result["total_or_employer"] + wa_result["total_wa_employer"]
		self.assertGreater(combined_ee, 0)
		self.assertGreater(combined_er, 0)


# ── Claim 4: Combined federal + state ──────────────────────────────────


class CombinedFederalState(StateTaxTestCase):
	"""The combined engine runs federal + state together."""

	def test_combined_or(self):
		"""Federal + Oregon combined calc produces grand totals."""
		result = calculate_all_payroll_taxes(
			gross_pay=2500,
			pay_frequency="Biweekly",
			work_state="OR",
			filing_status="Single",
			w4_data=self._default_w4_data(),
			ytd_gross=0,
			ytd_ss_withheld=0,
			fica_config=self._default_fica(),
			federal_tax_table=ANNUAL_BRACKETS["Single"],
			state_config=self._default_or_config(),
			state_tax_table=OR_ANNUAL_BRACKETS["Single"],
		)

		self.assertIn("federal", result)
		self.assertIn("state", result)
		self.assertEqual(result["work_state"], "OR")
		self.assertEqual(
			result["grand_total_employee"],
			round(result["federal"]["total_employee_tax"] + result["state"]["total_or_employee"], 2),
		)
		self.assertEqual(
			result["grand_total_employer"],
			round(result["federal"]["total_employer_tax"] + result["state"]["total_or_employer"], 2),
		)

	def test_combined_wa(self):
		"""Federal + Washington combined calc produces grand totals."""
		result = calculate_all_payroll_taxes(
			gross_pay=2500,
			pay_frequency="Biweekly",
			work_state="WA",
			filing_status="Single",
			w4_data=self._default_w4_data(),
			ytd_gross=0,
			ytd_ss_withheld=0,
			fica_config=self._default_fica(),
			federal_tax_table=ANNUAL_BRACKETS["Single"],
			state_config=self._default_wa_config(),
			state_tax_table=None,
		)

		self.assertIn("federal", result)
		self.assertIn("state", result)
		self.assertEqual(result["work_state"], "WA")
		self.assertEqual(
			result["grand_total_employee"],
			round(result["federal"]["total_employee_tax"] + result["state"]["total_wa_employee"], 2),
		)

	def test_grand_total_all(self):
		"""grand_total_all = grand_total_employee + grand_total_employer."""
		result = calculate_all_payroll_taxes(
			3000,
			"Biweekly",
			"OR",
			"Single",
			self._default_w4_data(),
			0,
			0,
			self._default_fica(),
			ANNUAL_BRACKETS["Single"],
			self._default_or_config(),
			OR_ANNUAL_BRACKETS["Single"],
		)
		self.assertEqual(
			result["grand_total_all"],
			round(result["grand_total_employee"] + result["grand_total_employer"], 2),
		)


# ── Claim 5: Oregon bracket boundaries ────────────────────────────────


class OregonBrackets(StateTaxTestCase):
	"""v0.291.0. Oregon withholds by the DOR's computer formula (150-206-436, effective 2026-01-01) on the employee's
	OR-W-4 — not by the income-tax brackets applied to gross. Worked from the publication, annual figures."""

	SINGLE = {"on_file": True, "filing_status": "Single", "allowances": 0}

	def _annual(self, wages, federal, w4):
		return calculate_oregon_withholding(wages, "Annual", "Single", self._default_or_config(), [],
		                                    or_w4=w4, federal_income_tax=federal)["or_income_tax"]

	def test_the_dor_example(self):
		# 150-206-436 Example 1: $25,000, single, 0 allowances, $1,000 federal → BASE 21,090 → $1,789 a year.
		self.assertEqual(round(self._annual(25000, 1000, self.SINGLE)), 1789)

	def test_lowest_rows_carry_the_built_in_credit(self):
		# BASE 3,000 − 2,910 = 90 → 263 + 90 × 4.75% = 267.275, less 263 for one allowance = 4.275.
		self.assertAlmostEqual(self._annual(3000, 0, self.SINGLE), 267.275, delta=0.01)
		self.assertAlmostEqual(self._annual(3000, 0, {**self.SINGLE, "allowances": 1}), 4.275, delta=0.01)

	def test_high_wages_phase_out_the_federal_subtraction(self):
		# $150,000 single: no federal subtraction (≥ $145,000), no allowances (> $100,000).
		# BASE 150,000 − 2,910 = 147,090 → 10,618 + 22,090 × 9.9% = 12,804.91.
		self.assertEqual(self._annual(150000, 30000, {**self.SINGLE, "allowances": 2}), 12804.91)

	def test_married_and_three_allowances_use_the_married_schedule(self):
		married = self._annual(40000, 2000, {"on_file": True, "filing_status": "Married", "allowances": 0})
		# BASE 40,000 − 2,000 − 5,820 = 32,180 → 1,620 + 9,380 × 8.75% = 2,440.75.
		self.assertEqual(married, 2440.75)
		self.assertLess(self._annual(40000, 2000, {**self.SINGLE, "allowances": 3}), self._annual(40000, 2000, self.SINGLE))

	def test_no_or_w4_is_a_flat_eight_percent(self):
		result = calculate_oregon_withholding(1000, "Weekly", "Single", self._default_or_config(), [])
		self.assertEqual(result["or_income_tax"], 80.0)
		self.assertIn("HB 2119", result["computation_detail"]["income_tax"]["note"])


# ── Claim 6: Small employer ────────────────────────────────────────────


class SmallEmployer(StateTaxTestCase):
	"""Oregon small employer pays no employer share of Paid Leave."""

	def test_small_employer_no_er_share(self):
		config = self._default_or_config()
		config["or_paid_leave_small_employer"] = 1

		result = calculate_oregon_withholding(
			2000,
			"Biweekly",
			"Single",
			config,
			OR_ANNUAL_BRACKETS["Single"],
		)
		self.assertEqual(result["or_paid_leave_employer"], 0.0)
		self.assertGreater(result["or_paid_leave_employee"], 0)

	def test_normal_employer_has_er_share(self):
		config = self._default_or_config()
		config["or_paid_leave_small_employer"] = 0

		result = calculate_oregon_withholding(
			2000,
			"Biweekly",
			"Single",
			config,
			OR_ANNUAL_BRACKETS["Single"],
		)
		self.assertGreater(result["or_paid_leave_employer"], 0)


# ── Claim 7: Edge cases ────────────────────────────────────────────────


class EdgeCases(StateTaxTestCase):
	"""Zero gross, missing brackets, unsupported state."""

	def test_zero_gross_or(self):
		result = calculate_oregon_withholding(
			0,
			"Biweekly",
			"Single",
			self._default_or_config(),
			OR_ANNUAL_BRACKETS["Single"],
		)
		self.assertEqual(result["total_or_employee"], 0)
		self.assertEqual(result["total_or_employer"], 0)

	def test_zero_gross_wa(self):
		result = calculate_washington_withholding(0, self._default_wa_config())
		self.assertEqual(result["total_wa_employee"], 0)
		self.assertEqual(result["total_wa_employer"], 0)

	def test_no_brackets_or(self):
		"""v0.291.0: the State Tax Table brackets are no longer read for Oregon income tax — the DOR formula is."""
		config = self._default_or_config()
		result = calculate_oregon_withholding(3000, "Biweekly", "Single", config, [],
		                                      or_w4={"on_file": True, "filing_status": "Single"})
		self.assertGreater(result["or_income_tax"], 0)
		self.assertEqual(result["computation_detail"]["income_tax"]["method"], "formula")
		self.assertGreater(result["or_transit_tax"], 0)

	def test_income_tax_disabled(self):
		"""Disabling Oregon income tax zeroes it out."""
		config = self._default_or_config()
		config["or_income_tax_enabled"] = 0
		result = calculate_oregon_withholding(
			3000,
			"Biweekly",
			"Single",
			config,
			OR_ANNUAL_BRACKETS["Single"],
		)
		self.assertEqual(result["or_income_tax"], 0)

	def test_unsupported_state(self):
		result = calculate_state_withholding(
			1000,
			"Biweekly",
			"CA",
			"Single",
			{},
			[],
		)
		self.assertIn("error", result)

	def test_deterministic(self):
		"""Same inputs produce the same outputs."""
		config = self._default_or_config()
		brackets = OR_ANNUAL_BRACKETS["Single"]
		kwargs = dict(
			gross_pay=3000,
			pay_frequency="Biweekly",
			filing_status="Single",
			state_config=config,
			state_tax_table=brackets,
		)
		r1 = calculate_oregon_withholding(**kwargs)
		r2 = calculate_oregon_withholding(**kwargs)
		self.assertEqual(r1["or_income_tax"], r2["or_income_tax"])
		self.assertEqual(r1["total_or_employee"], r2["total_or_employee"])

	def test_all_periods_or(self):
		"""Oregon engine works for every pay frequency."""
		config = self._default_or_config()
		brackets = OR_ANNUAL_BRACKETS["Single"]
		for period in PERIODS_PER_YEAR:
			result = calculate_oregon_withholding(5000, period, "Single", config, brackets)
			self.assertIn("or_income_tax", result, f"failed for {period}")

	def test_seed_or_brackets(self):
		"""seed_or_brackets returns rows for all filing statuses and periods."""
		rows = seed_or_brackets()
		self.assertGreater(len(rows), 0)
		statuses = {r["filing_status"] for r in rows}
		self.assertIn("Single", statuses)
		self.assertIn("Married Filing Jointly", statuses)
		self.assertIn("Head of Household", statuses)
		self.assertTrue(all(r["state"] == "OR" for r in rows))


# ── Claim 8: Read tools ───────────────────────────────────────────────


class ReadTools(StateTaxTestCase):
	"""Read-only MCP tools return the right data."""

	def test_get_state_tax_config_or(self):
		data = self.tool_data(
			"get_state_tax_config",
			{
				"company": MAIN,
				"state": "OR",
				"tax_year": 2025,
			},
		)
		self.assertEqual(data["state"], "OR")
		self.assertEqual(data["company"], MAIN)

	def test_get_state_tax_config_wa(self):
		data = self.tool_data(
			"get_state_tax_config",
			{
				"company": MAIN,
				"state": "WA",
				"tax_year": 2025,
			},
		)
		self.assertEqual(data["state"], "WA")

	def test_list_state_tax_configs(self):
		data = self.tool_data("list_state_tax_configs", {"company": MAIN})
		self.assertEqual(data["count"], 2)

	def test_get_state_tax_table_or(self):
		data = self.tool_data(
			"get_state_tax_table",
			{
				"state": "OR",
				"tax_year": 2025,
				"filing_status": "Single",
			},
		)
		self.assertGreater(data["count"], 0)

	def test_get_state_tax_table_wa_empty(self):
		data = self.tool_data(
			"get_state_tax_table",
			{
				"state": "WA",
				"tax_year": 2025,
				"filing_status": "Single",
			},
		)
		self.assertEqual(data["count"], 0)
		self.assertIn("note", data)

	def test_missing_config_errors(self):
		text = self.tool_error(
			"get_state_tax_config",
			{
				"company": MAIN,
				"state": "OR",
				"tax_year": 2030,
			},
		)
		self.assertIn("no active", text)


# ── Claim 9: Mutating tools ───────────────────────────────────────────


class MutatingTools(StateTaxTestCase):
	"""Create, update, and import tools work correctly."""

	def test_create_state_tax_config(self):
		data = self.tool_data(
			"create_state_tax_config",
			{
				"company": MAIN,
				"state": "OR",
				"tax_year": 2026,
				"or_transit_tax_rate": 0.1,
			},
		)
		self.assertEqual(data["state"], "OR")
		self.assertEqual(data["tax_year"], 2026)

	def test_create_duplicate_fails(self):
		text = self.tool_error(
			"create_state_tax_config",
			{
				"company": MAIN,
				"state": "OR",
				"tax_year": 2025,
			},
		)
		self.assertIn("already exists", text)

	def test_update_state_tax_config(self):
		data = self.tool_data(
			"update_state_tax_config",
			{
				"company": MAIN,
				"state": "OR",
				"tax_year": 2025,
				"or_transit_tax_rate": 0.15,
			},
		)
		self.assertIn("or_transit_tax_rate", data["updated_fields"])

	def test_import_state_tax_table(self):
		data = self.tool_data(
			"import_state_tax_table",
			{
				"state": "OR",
				"tax_year": 2026,
				"brackets": [
					{
						"filing_status": "Single",
						"bracket_floor": 0,
						"bracket_ceiling": 5000,
						"base_tax": 0,
						"marginal_rate": 5.0,
					},
					{
						"filing_status": "Single",
						"bracket_floor": 5000,
						"bracket_ceiling": None,
						"base_tax": 250,
						"marginal_rate": 9.0,
					},
				],
			},
		)
		self.assertEqual(data["created"], 2)


# ── Claim 10: Combined preview tool ───────────────────────────────────


class CombinedPreview(StateTaxTestCase):
	"""The preview_total_payroll_taxes tool runs federal + state."""

	def test_preview_total_or(self):
		self._submit_w4()
		data = self.tool_data(
			"preview_total_payroll_taxes",
			{
				"employee": "HR-EMP-00001",
				"gross_pay": 2500,
				"pay_frequency": "Biweekly",
				"work_state": "OR",
			},
		)
		self.assertIn("federal", data)
		self.assertIn("state", data)
		self.assertEqual(data["work_state"], "OR")
		self.assertGreater(data["grand_total_employee"], 0)
		self.assertGreater(data["grand_total_employer"], 0)

	def test_preview_total_wa(self):
		self._submit_w4(employee="HR-EMP-00002")
		data = self.tool_data(
			"preview_total_payroll_taxes",
			{
				"employee": "HR-EMP-00002",
				"gross_pay": 2500,
				"pay_frequency": "Biweekly",
				"work_state": "WA",
			},
		)
		self.assertEqual(data["work_state"], "WA")
		self.assertGreater(data["grand_total_employee"], 0)

	def test_preview_state_only_or(self):
		self._submit_w4()
		data = self.tool_data(
			"preview_state_withholding",
			{
				"employee": "HR-EMP-00001",
				"gross_pay": 2500,
				"pay_frequency": "Biweekly",
				"work_state": "OR",
			},
		)
		self.assertEqual(data["state"], "OR")
		self.assertGreater(data["total_or_employee"], 0)


# ── Claim 11: The Oregon brackets are data, not constants ─────────────


class SeededOregonBrackets(StateTaxTestCase):
	"""`seed_or_brackets` produces what the engine actually reads.

	v0.91.0. Until this release the seeder divided every bracket by every entry
	in PERIODS_PER_YEAR and emitted a row per pay frequency — but State Tax Table
	has no `payroll_period` column to tell them apart, and the engine annualizes
	gross before looking a bracket up. Both halves are asserted here, because
	either one alone would have let the bug back in: the shape of the rows, and
	the tax they produce once they are the table.
	"""

	def test_seed_is_annual_not_per_period(self):
		"""One row per bracket per filing status. No period fan-out."""
		rows = seed_or_brackets()
		expected = sum(len(b) for b in OR_ANNUAL_BRACKETS.values())
		self.assertEqual(len(rows), expected)
		# The old seeder produced this many, which is the number to stay away from.
		self.assertNotEqual(len(rows), expected * len(PERIODS_PER_YEAR))

	def test_seeded_floors_are_the_annual_floors(self):
		"""A seeded floor is the statutory annual figure, undivided."""
		rows = seed_or_brackets()
		single = sorted(
			(r for r in rows if r["filing_status"] == "Single"),
			key=lambda r: r["bracket_floor"],
		)
		self.assertEqual(
			[r["bracket_floor"] for r in single],
			[b["bracket_floor"] for b in OR_ANNUAL_BRACKETS["Single"]],
		)
		self.assertEqual(
			[r["base_tax"] for r in single],
			[b["base_tax"] for b in OR_ANNUAL_BRACKETS["Single"]],
		)

	def test_top_bracket_stays_open(self):
		"""The open-ended top bracket survives seeding as a real None."""
		rows = seed_or_brackets()
		for status in OR_ANNUAL_BRACKETS:
			group = [r for r in rows if r["filing_status"] == status]
			open_top = [r for r in group if r["bracket_ceiling"] is None]
			self.assertEqual(len(open_top), 1, f"{status}: {open_top}")

	def test_no_duplicate_keys(self):
		"""No two seeded rows share (filing_status, bracket_floor).

		This is the assertion the old seeder failed. `_load_state_table` filters
		on state + tax_year + filing_status and nothing else, so two rows with the
		same floor are two brackets the walk cannot choose between.
		"""
		rows = seed_or_brackets()
		keys = [(r["filing_status"], r["bracket_floor"]) for r in rows]
		self.assertEqual(len(keys), len(set(keys)))

	def test_seed_year_is_stamped(self):
		rows = seed_or_brackets(2027)
		self.assertTrue(all(r["tax_year"] == 2027 for r in rows))
		self.assertTrue(all(r["state"] == "OR" for r in rows))

	def test_seeded_table_matches_the_constants_through_the_engine(self):
		"""Seeded rows and the constants withhold the same amount.

		The end-to-end claim: whatever the seeder writes, an Oregon cheque comes
		out the same as it would from the in-module brackets. A per-period seed
		failed this by roughly the period count.
		"""
		config = self._default_or_config()
		seeded = [r for r in seed_or_brackets() if r["filing_status"] == "Single"]
		for gross, period in ((1000, "Biweekly"), (3000, "Monthly"), (60000, "Annual")):
			from_seed = calculate_oregon_withholding(gross, period, "Single", config, seeded)
			from_const = calculate_oregon_withholding(
				gross,
				period,
				"Single",
				config,
				OR_ANNUAL_BRACKETS["Single"],
			)
			self.assertEqual(
				from_seed["or_income_tax"],
				from_const["or_income_tax"],
				f"{gross} {period}",
			)
			self.assertGreater(from_seed["or_income_tax"], 0, f"{gross} {period}")

	def test_seed_passes_its_own_import_validation(self):
		"""The shipped brackets satisfy the rules the import tool enforces.

		A seeder that wrote rows `import_state_tax_table` would reject would mean
		the app disagreed with itself about what a valid bracket table is.
		"""
		data = self.tool_data(
			"import_state_tax_table",
			{
				"state": "OR",
				"tax_year": 2031,
				"brackets": [
					{k: v for k, v in row.items() if k not in ("state", "tax_year")}
					for row in seed_or_brackets()
				],
			},
		)
		self.assertEqual(data["created"], len(seed_or_brackets()))


class ImportStateTaxTable(StateTaxTestCase):
	"""Claim 12: a new tax year loads without a release, and safely."""

	SINGLE_2026: ClassVar[list] = [
		{
			"filing_status": "Single",
			"bracket_floor": 0,
			"bracket_ceiling": 4300,
			"base_tax": 0,
			"marginal_rate": 4.75,
		},
		{
			"filing_status": "Single",
			"bracket_floor": 4300,
			"bracket_ceiling": None,
			"base_tax": 204.25,
			"marginal_rate": 6.75,
		},
	]

	def test_import_then_reimport_refuses(self):
		"""A second import of the same year is refused, not duplicated."""
		self.tool_data(
			"import_state_tax_table",
			{
				"state": "OR",
				"tax_year": 2026,
				"brackets": self.SINGLE_2026,
			},
		)
		text = self.tool_error(
			"import_state_tax_table",
			{
				"state": "OR",
				"tax_year": 2026,
				"brackets": self.SINGLE_2026,
			},
		)
		self.assertIn("already exist", text)
		self.assertIn("replace=true", text)
		rows = STORE.rows("State Tax Table")
		self.assertEqual(len([r for r in rows if r.get("tax_year") == 2026]), 2)

	def test_replace_rewrites_the_year(self):
		self.tool_data(
			"import_state_tax_table",
			{
				"state": "OR",
				"tax_year": 2026,
				"brackets": self.SINGLE_2026,
			},
		)
		revised = [
			{
				"filing_status": "Single",
				"bracket_floor": 0,
				"bracket_ceiling": 4400,
				"base_tax": 0,
				"marginal_rate": 4.75,
			},
			{
				"filing_status": "Single",
				"bracket_floor": 4400,
				"bracket_ceiling": None,
				"base_tax": 209.0,
				"marginal_rate": 6.75,
			},
		]
		data = self.tool_data(
			"import_state_tax_table",
			{
				"state": "OR",
				"tax_year": 2026,
				"brackets": revised,
				"replace": True,
			},
		)
		self.assertEqual(data["deleted"], 2)
		self.assertEqual(data["created"], 2)
		self.assertTrue(data["replaced"])
		rows = [r for r in STORE.rows("State Tax Table") if r.get("tax_year") == 2026]
		self.assertEqual(len(rows), 2)
		self.assertEqual(sorted(float(r["bracket_floor"]) for r in rows), [0.0, 4400.0])

	def test_replace_leaves_other_years_alone(self):
		self.tool_data(
			"import_state_tax_table",
			{
				"state": "OR",
				"tax_year": 2026,
				"brackets": self.SINGLE_2026,
				"replace": True,
			},
		)
		before = len([r for r in STORE.rows("State Tax Table") if r.get("tax_year") == 2025])
		self.assertGreater(before, 0)
		self.tool_data(
			"import_state_tax_table",
			{
				"state": "OR",
				"tax_year": 2026,
				"brackets": self.SINGLE_2026,
				"replace": True,
			},
		)
		after = len([r for r in STORE.rows("State Tax Table") if r.get("tax_year") == 2025])
		self.assertEqual(before, after)

	def test_rejects_unknown_filing_status(self):
		text = self.tool_error(
			"import_state_tax_table",
			{
				"state": "OR",
				"tax_year": 2029,
				"brackets": [
					{
						"filing_status": "Married Filing Separately",
						"bracket_floor": 0,
						"bracket_ceiling": None,
						"base_tax": 0,
						"marginal_rate": 5,
					}
				],
			},
		)
		self.assertIn("does not accept", text)

	def test_rejects_a_gap(self):
		text = self.tool_error(
			"import_state_tax_table",
			{
				"state": "OR",
				"tax_year": 2029,
				"brackets": [
					{
						"filing_status": "Single",
						"bracket_floor": 0,
						"bracket_ceiling": 4000,
						"base_tax": 0,
						"marginal_rate": 4.75,
					},
					{
						"filing_status": "Single",
						"bracket_floor": 5000,
						"bracket_ceiling": None,
						"base_tax": 190,
						"marginal_rate": 6.75,
					},
				],
			},
		)
		self.assertIn("do not meet", text)

	def test_rejects_not_starting_at_zero(self):
		text = self.tool_error(
			"import_state_tax_table",
			{
				"state": "OR",
				"tax_year": 2029,
				"brackets": [
					{
						"filing_status": "Single",
						"bracket_floor": 1000,
						"bracket_ceiling": None,
						"base_tax": 0,
						"marginal_rate": 4.75,
					},
				],
			},
		)
		self.assertIn("start", text)

	def test_rejects_no_open_top_bracket(self):
		text = self.tool_error(
			"import_state_tax_table",
			{
				"state": "OR",
				"tax_year": 2029,
				"brackets": [
					{
						"filing_status": "Single",
						"bracket_floor": 0,
						"bracket_ceiling": 4000,
						"base_tax": 0,
						"marginal_rate": 4.75,
					},
				],
			},
		)
		self.assertIn("above the highest", text)

	def test_rejects_two_open_top_brackets(self):
		text = self.tool_error(
			"import_state_tax_table",
			{
				"state": "OR",
				"tax_year": 2029,
				"brackets": [
					{
						"filing_status": "Single",
						"bracket_floor": 0,
						"bracket_ceiling": None,
						"base_tax": 0,
						"marginal_rate": 4.75,
					},
					{
						"filing_status": "Single",
						"bracket_floor": 4000,
						"bracket_ceiling": None,
						"base_tax": 190,
						"marginal_rate": 6.75,
					},
				],
			},
		)
		self.assertIn("blank bracket_ceiling", text)

	def test_rejects_rate_as_a_fraction(self):
		"""9.9% stated as 0.099 is accepted as a number and wrong as a rate.

		This one cannot be caught by range alone — 0.099 IS between 0 and 100 —
		so it is here as a reminder of what the range check does and does not buy.
		What it does catch is 990.
		"""
		text = self.tool_error(
			"import_state_tax_table",
			{
				"state": "OR",
				"tax_year": 2029,
				"brackets": [
					{
						"filing_status": "Single",
						"bracket_floor": 0,
						"bracket_ceiling": None,
						"base_tax": 0,
						"marginal_rate": 990,
					},
				],
			},
		)
		self.assertIn("percentage", text)

	def test_rejects_ceiling_below_floor(self):
		text = self.tool_error(
			"import_state_tax_table",
			{
				"state": "OR",
				"tax_year": 2029,
				"brackets": [
					{
						"filing_status": "Single",
						"bracket_floor": 0,
						"bracket_ceiling": 5000,
						"base_tax": 0,
						"marginal_rate": 4.75,
					},
					{
						"filing_status": "Single",
						"bracket_floor": 5000,
						"bracket_ceiling": 4000,
						"base_tax": 190,
						"marginal_rate": 6.75,
					},
				],
			},
		)
		self.assertIn("at or below", text)

	def test_nothing_is_written_when_validation_fails(self):
		"""A rejected payload leaves the table exactly as it was."""
		before = len(STORE.rows("State Tax Table"))
		self.tool_error(
			"import_state_tax_table",
			{
				"state": "OR",
				"tax_year": 2029,
				"brackets": [
					{
						"filing_status": "Single",
						"bracket_floor": 0,
						"bracket_ceiling": 4000,
						"base_tax": 0,
						"marginal_rate": 4.75,
					},
					{
						"filing_status": "Single",
						"bracket_floor": 9000,
						"bracket_ceiling": None,
						"base_tax": 190,
						"marginal_rate": 6.75,
					},
				],
			},
		)
		self.assertEqual(len(STORE.rows("State Tax Table")), before)

	def test_a_replace_that_fails_validation_deletes_nothing(self):
		"""The delete happens after validation, never before it."""
		before = [r for r in STORE.rows("State Tax Table") if r.get("tax_year") == 2025]
		self.assertGreater(len(before), 0)
		self.tool_error(
			"import_state_tax_table",
			{
				"state": "OR",
				"tax_year": 2025,
				"replace": True,
				"brackets": [
					{
						"filing_status": "Single",
						"bracket_floor": 500,
						"bracket_ceiling": None,
						"base_tax": 0,
						"marginal_rate": 4.75,
					},
				],
			},
		)
		after = [r for r in STORE.rows("State Tax Table") if r.get("tax_year") == 2025]
		self.assertEqual(len(before), len(after))

	def test_imported_year_is_what_withholding_reads(self):
		"""The end of the point: import a year, and that year's tax applies."""
		self.tool_data(
			"import_state_tax_table",
			{
				"state": "OR",
				"tax_year": 2026,
				"brackets": [
					{
						"filing_status": "Single",
						"bracket_floor": 0,
						"bracket_ceiling": None,
						"base_tax": 0,
						"marginal_rate": 10.0,
					},
				],
			},
		)
		data = self.tool_data(
			"get_state_tax_table",
			{
				"state": "OR",
				"tax_year": 2026,
				"filing_status": "Single",
			},
		)
		self.assertEqual(data["count"], 1)
		self.assertEqual(float(data["brackets"][0]["marginal_rate"]), 10.0)


# ── Claim 13: the advertised contract matches the handler ─────────────


class TheSchemaMatchesTheHandler(StateTaxTestCase):
	"""Every argument the handler reads is one the schema lets a caller send.

	THIS IS THE TEST THAT WAS MISSING. `import_state_tax_table` shipped in
	v0.91.0 reading a `replace` argument that its inputSchema did not declare,
	and nothing caught it: the count assertions count entries rather than look
	inside them, and the tool's own tests call the handler through the
	dispatcher, which does not validate arguments against the schema. So a
	lenient caller got through, the suite went green, and a client that
	generates its call from `tools/list` — the documented contract, carrying
	`additionalProperties: false` — could never send it. The tool's refusal on
	an already-imported year meanwhile said "Pass replace=true", naming a
	parameter the schema forbids.

	The arguments are read out of the handler's own AST rather than listed here,
	so the assertion cannot go stale the way a hand-maintained list would.
	"""

	#: Reading an argument through one of these means the handler accepts it.
	READERS = ("as_str", "as_int", "as_bool", "as_float", "as_limit", "_as_float")

	def _arguments_read_by(self, module_path, function_name):
		import ast
		import pathlib

		source = pathlib.Path(module_path).read_text(encoding="utf-8")
		tree = ast.parse(source)
		function = next(
			node
			for node in ast.walk(tree)
			if isinstance(node, ast.FunctionDef) and node.name == function_name
		)

		found = set()
		for node in ast.walk(function):
			if not isinstance(node, ast.Call):
				continue
			# args.get("x")
			if (
				isinstance(node.func, ast.Attribute)
				and node.func.attr == "get"
				and isinstance(node.func.value, ast.Name)
				and node.func.value.id == "args"
				and node.args
				and isinstance(node.args[0], ast.Constant)
				and isinstance(node.args[0].value, str)
			):
				found.add(node.args[0].value)
			# as_str(args, "x") and friends
			if (
				isinstance(node.func, ast.Name)
				and node.func.id in self.READERS
				and len(node.args) >= 2
				and isinstance(node.args[0], ast.Name)
				and node.args[0].id == "args"
				and isinstance(node.args[1], ast.Constant)
				and isinstance(node.args[1].value, str)
			):
				found.add(node.args[1].value)
		return found

	#: Handlers that do NOT read `args` key by key, and how to resolve what they
	#: do read. `create_state_tax_config` and `update_state_tax_config` iterate
	#: `_config_rate_fields(state)` and pull each name off `args` through a loop
	#: variable, so the AST walk above sees only their three literal arguments
	#: out of thirteen and fourteen.
	#:
	#: THAT IS WORSE THAN SEEING NONE. A walk that finds nothing is obviously
	#: broken; one that finds a quarter of the surface reports "every argument
	#: declared" and is believed. The entry below resolves the tuple by calling
	#: the function, and `test_the_walk_can_see_every_required_argument` is what
	#: catches the next handler that grows a shape this file cannot read.
	_INDIRECT_READERS: ClassVar[dict] = {
		"create_state_tax_config": ("_config_rate_fields", ("OR", "WA")),
		"update_state_tax_config": ("_config_rate_fields", ("OR", "WA")),
	}

	def _indirect_arguments(self, function_name):
		import erpnext_mcp.tools.state_tax as module

		entry = self._INDIRECT_READERS.get(function_name)
		if not entry:
			return set()
		resolver_name, arguments = entry
		resolver = getattr(module, resolver_name)
		names = set()
		for argument in arguments:
			names.update(resolver(argument))
		return names

	def _all_arguments_read_by(self, function_name):
		import erpnext_mcp.tools.state_tax as module

		return self._arguments_read_by(module.__file__, function_name) | self._indirect_arguments(
			function_name
		)

	def _assert_declared(self, tool_name, function_name):
		from erpnext_mcp import registry

		read = self._all_arguments_read_by(function_name)
		declared = set(registry.TOOLS[tool_name]["inputSchema"]["properties"])
		undeclared = read - declared
		self.assertEqual(
			undeclared,
			set(),
			f"{tool_name}'s handler reads {sorted(undeclared)}, which its inputSchema does "
			f"not declare. The schema carries additionalProperties:false, so a caller that "
			f"honours the advertised contract cannot send them.",
		)

	def test_import_state_tax_table_declares_replace(self):
		"""The specific regression: `replace` reachable from the contract."""
		from erpnext_mcp import registry

		schema = registry.TOOLS["import_state_tax_table"]["inputSchema"]
		self.assertIn("replace", schema["properties"])
		self.assertEqual(schema["properties"]["replace"]["type"], "boolean")
		self.assertNotIn("replace", schema["required"])

	def test_import_state_tax_table_schema_covers_its_handler(self):
		self._assert_declared("import_state_tax_table", "import_state_tax_table")

	#: Every state-tax tool and the handler behind it. Named once so the two
	#: tests below cannot drift apart about which tools are covered.
	COVERED = (
		("get_state_tax_config", "get_state_tax_config"),
		("list_state_tax_configs", "list_state_tax_configs"),
		("get_state_tax_table", "get_state_tax_table"),
		("preview_state_withholding", "preview_state_withholding"),
		("preview_total_payroll_taxes", "preview_total_payroll_taxes"),
		("list_employees_by_work_state", "list_employees_by_work_state"),
		("create_state_tax_config", "create_state_tax_config"),
		("update_state_tax_config", "update_state_tax_config"),
	)

	def test_the_other_state_tax_tools_too(self):
		for tool_name, function_name in self.COVERED:
			with self.subTest(tool=tool_name):
				self._assert_declared(tool_name, function_name)

	def test_the_walk_can_see_every_required_argument(self):
		"""The vacuity canary. A blind check must fail, not pass.

		If a handler stops reading its arguments in a shape this file can read,
		`_assert_declared` goes on passing — it compares an empty set against the
		schema and finds nothing undeclared. That is how a guard rots into
		decoration.

		A REQUIRED argument is one the handler certainly consumes, so the walk
		being unable to see it means the walk is blind rather than the tool being
		clean.

		WHAT THIS DOES NOT CATCH, stated because the first draft of this
		docstring claimed otherwise. It would NOT have caught the
		`create_state_tax_config` blindness that prompted it: that handler reads
		`state` and `tax_year` — its only required arguments — as literals, and
		it was the ten optional rate fields behind `_config_rate_fields` that
		were invisible. The test below is what fails in that case, because a read
		set missing those fields makes them look declared-but-ignored. This one
		covers the sharper regression where a handler's required arguments go
		behind a helper too, which is the shape that would leave
		`_assert_declared` comparing an empty set and passing.
		"""
		from erpnext_mcp import registry

		for tool_name, function_name in self.COVERED:
			with self.subTest(tool=tool_name):
				required = set(registry.TOOLS[tool_name]["inputSchema"]["required"])
				if not required:
					continue
				read = self._all_arguments_read_by(function_name)
				invisible = required - read
				self.assertEqual(
					invisible,
					set(),
					f"{tool_name} marks {sorted(invisible)} required, but this test cannot see "
					f"the handler read them — so it is not actually checking this tool. The "
					f"handler probably reads its arguments through a loop or a helper; add it "
					f"to _INDIRECT_READERS rather than letting the check pass blind.",
				)

	def test_the_config_tools_declare_nothing_they_ignore(self):
		"""The other direction, for the two tools whose reads are fully known.

		A schema field no handler consumes is accepted, documented, and silently
		dropped — the caller is told it worked. Only asserted where the read set
		is complete; on a tool the walk covers partially it would be noise.
		"""
		from erpnext_mcp import registry

		for tool_name, function_name in (
			("create_state_tax_config", "create_state_tax_config"),
			("update_state_tax_config", "update_state_tax_config"),
		):
			with self.subTest(tool=tool_name):
				declared = set(registry.TOOLS[tool_name]["inputSchema"]["properties"])
				read = self._all_arguments_read_by(function_name)
				ignored = declared - read
				self.assertEqual(
					ignored,
					set(),
					f"{tool_name}'s schema declares {sorted(ignored)}, which its handler never "
					f"reads. A caller that sends one is told the call succeeded and the value "
					f"is dropped.",
				)

	def test_the_refusal_names_an_argument_the_schema_allows(self):
		"""The refusal says "Pass replace=true"; the schema has to permit that.

		Asserted together because the pairing is the actual defect — an error
		message is only actionable if the contract admits the action it names.
		"""
		from erpnext_mcp import registry

		self.tool_data(
			"import_state_tax_table",
			{
				"state": "OR",
				"tax_year": 2026,
				"brackets": [
					{
						"filing_status": "Single",
						"bracket_floor": 0,
						"bracket_ceiling": None,
						"base_tax": 0,
						"marginal_rate": 5.0,
					}
				],
			},
		)
		text = self.tool_error(
			"import_state_tax_table",
			{
				"state": "OR",
				"tax_year": 2026,
				"brackets": [
					{
						"filing_status": "Single",
						"bracket_floor": 0,
						"bracket_ceiling": None,
						"base_tax": 0,
						"marginal_rate": 5.0,
					}
				],
			},
		)
		self.assertIn("replace=true", text)
		self.assertIn("replace", registry.TOOLS["import_state_tax_table"]["inputSchema"]["properties"])

# SPDX-License-Identifier: MIT
"""Payroll tax corrections. v0.291.0.

1. FEDERAL, W-4 STEP 2 CHECKED: the Pub 15-T Step 2 checkbox schedule, not the standard one (which under-withheld).
2. FEDERAL 2025: the real 2025 schedule (the table labelled 2025 held 2024's), and a migrate that swaps the old seed
   only where nobody touched it.
3. OREGON: the DOR's computer formula (150-206-436) on the employee's OR-W-4 — wages less federal tax withheld (capped,
   phased out) less the standard deduction, the published rates, less $263 an allowance — and a flat 8% when no
   OR-W-4 is on file (HB 2119). Not the income-tax brackets on gross, and not the Single table for everybody.
"""

import itertools

from erpnext_mcp import install, withholding as w
from erpnext_mcp.state_withholding import OR_WITHHOLDING_2026, oregon_annual_withholding

from .fixtures import MAIN
from .harness import STORE, frappe
from .test_payroll_integration import PERIOD_END, PERIOD_START, WORKER, IntegrationTestCase, member, shift

FICA = {"social_security_rate_employee": 6.2, "social_security_wage_base": 184500, "medicare_rate_employee": 1.45,
        "additional_medicare_threshold": 200000, "additional_medicare_rate": 0.9}


def fit(gross, status, rows, **w4):
	return w.calculate_federal_withholding(gross, "Weekly", {"filing_status": status, **w4}, 0, 0, FICA, rows)


class EveryTableIsConsistent(IntegrationTestCase):
	def test_each_row_follows_from_the_one_before(self):
		for year, schedules in w.SEEDED_YEARS.items():
			for schedule, table in schedules.items():
				for status, rows in table.items():
					for before, row in itertools.pairwise(rows):
						with self.subTest(year=year, schedule=schedule, status=status, floor=row["bracket_floor"]):
							self.assertEqual(before["bracket_ceiling"], row["bracket_floor"])
							width = before["bracket_ceiling"] - before["bracket_floor"]
							# The checkbox schedules are printed as half the married-joint figures, rounded, so they chain
							# only to within about $0.20; the standard ones chain to the cent.
							self.assertAlmostEqual(before["base_tax"] + width * before["marginal_rate"] / 100,
							                       row["base_tax"], delta=0.25 if schedule == w.CHECKBOX else 0.02)


class Federal(IntegrationTestCase):
	def test_2025_is_the_2025_schedule(self):
		# $1,000 a week, single, 2025: 52,000 − 8,600 = 43,400 → 1,192.50 + 12% × 25,075 = 4,201.50 → ÷ 52 = 80.80.
		self.assertEqual(fit(1000.0, "Single or Married Filing Separately", w.ANNUAL_BRACKETS["Single"])
		                 ["federal_income_tax"], 80.80)

	def test_step_2_checked_uses_the_checkbox_schedule(self):
		# 2026 checkbox, single: 52,000 (no line 1(g)) → 2,900 + 22% × 18,750 = 7,025 → ÷ 52 = 135.10.
		rows = [{**r, "schedule": w.CHECKBOX} for r in w.ANNUAL_BRACKETS_2026_CHECKBOX["Single"]]
		got = fit(1000.0, "Single or Married Filing Separately", rows, multiple_jobs=True)
		self.assertEqual(got["federal_income_tax"], 135.10)
		self.assertNotIn("step_2c_checkbox_not_applied", got["computation_detail"]["federal_income_tax"])

	def test_checked_without_a_checkbox_table_says_so(self):
		got = fit(1000.0, "Single or Married Filing Separately", w.ANNUAL_BRACKETS_2026["Single"], multiple_jobs=True)
		self.assertIn("step_2c_checkbox_not_applied", got["computation_detail"]["federal_income_tax"])

	def test_the_loader_picks_the_schedule_by_the_w4(self):
		from erpnext_mcp.tools import payroll

		STORE.seed("Federal Tax Table", [{"name": f"FT-{s}-{i}", **r} for s in (w.STANDARD, w.CHECKBOX)
		                                 for i, r in enumerate(w.seed_brackets(2026, s))])
		STORE.commit()
		standard = payroll._load_federal_tax_table(2026, "Single", "Weekly")
		checkbox = payroll._load_federal_tax_table(2026, "Single", "Weekly", checkbox=True)
		self.assertEqual({r["schedule"] for r in standard}, {w.STANDARD})
		self.assertEqual({r["schedule"] for r in checkbox}, {w.CHECKBOX})
		self.assertEqual(standard[1]["bracket_floor"], 7500)
		self.assertEqual(checkbox[1]["bracket_floor"], 8050)


class TheMislabelled2025Seed(IntegrationTestCase):
	def legacy(self):
		for name in frappe.db.get_all("Federal Tax Table", filters={"tax_year": 2025}, pluck="name"):
			frappe.delete_doc("Federal Tax Table", name, force=True)
		rows = []
		for status, periods in (("Single", w.PERIODS_PER_YEAR),):
			for period, n in periods.items():
				for i, b in enumerate(w.LEGACY_2025_SEED[status]):
					rows.append({"name": f"L-{period}-{i}", "tax_year": 2025, "filing_status": status,
					             "payroll_period": period, "bracket_floor": round(b["bracket_floor"] / n, 2),
					             "bracket_ceiling": round(b["bracket_ceiling"] / n, 2) if b["bracket_ceiling"] else None,
					             "base_tax": round(b["base_tax"] / n, 2), "marginal_rate": b["marginal_rate"]})
		STORE.seed("Federal Tax Table", rows)
		STORE.commit()

	def test_an_untouched_old_seed_is_replaced(self):
		self.legacy()
		install._replace_legacy_2025()
		self.assertEqual(frappe.db.count("Federal Tax Table", {"tax_year": 2025}), 0)

	def test_an_edited_table_is_left_alone(self):
		self.legacy()
		frappe.db.set_value("Federal Tax Table", "L-Annual-1", "bracket_floor", 6100)
		STORE.commit()
		install._replace_legacy_2025()
		self.assertGreater(frappe.db.count("Federal Tax Table", {"tax_year": 2025}), 0)


class OregonFormula(IntegrationTestCase):
	def test_the_published_example_and_the_phase_out(self):
		got = oregon_annual_withholding(25000, 1000, {"filing_status": "Single", "allowances": 0}, OR_WITHHOLDING_2026)
		self.assertEqual(round(got["annual"]), 1789)
		high = oregon_annual_withholding(132000, 21098, {"filing_status": "Single", "allowances": 4}, OR_WITHHOLDING_2026)
		self.assertEqual((high["federal_subtraction"], high["allowances"]), (5250.0, 0))

	def test_higher_single_rate_uses_single_figures(self):
		got = oregon_annual_withholding(60000, 3000, {"filing_status": "Married, withhold at higher Single rate",
		                                               "allowances": 4}, OR_WITHHOLDING_2026)
		self.assertEqual((got["group"], got["phase_out"]), ("single", "single"))


class OregonInAPayRun(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		frappe.db.set_value("State Tax Configuration", "STC-OR-2025", "or_income_tax_enabled", 1)
		self.seed_shifts(*[shift(f"S{i}", f"2025-06-{2 + i:02d}", state="OR", end="14:00:00", crew=[member(WORKER)])
		                   for i in range(5)])
		self.structure(WORKER, pay_type="Hourly", rate=20.0)
		STORE.commit()
		self.args = {"company": MAIN, "pay_period_start": PERIOD_START, "pay_period_end": PERIOD_END}

	def oregon_tax(self):
		got = self.tool_data("preview_payroll_for_period", {**self.args, "detail": 1})
		slip = next(s for s in got["slips"] if s["employee"] == WORKER)
		return got, slip["state_taxes_detail"]["OR"]["computation_detail"]["income_tax"]

	def test_no_or_w4_is_eight_percent_and_named(self):
		got, income = self.oregon_tax()
		self.assertEqual((income["method"], income["rate"]), ("flat", 8.0))
		self.assertTrue(got["oregon_flat_rate_no_or_w4"])

	def test_with_an_or_w4_the_formula_applies(self):
		self.configure(enabled=1, allow_submit_w4=1, allow_preview_payroll_for_period=1, allow_create_salary_structure=1)
		self.tool_data("submit_w4", {"employee": WORKER, "company": MAIN, "tax_year": 2025,
		                             "filing_status": "Single or Married Filing Separately",
		                             "or_filing_status": "Single", "or_allowances": 1})
		STORE.commit()
		got, income = self.oregon_tax()
		self.assertEqual(income["method"], "formula")
		self.assertEqual(income["allowances"], 1)
		self.assertEqual(got["oregon_flat_rate_no_or_w4"], [])

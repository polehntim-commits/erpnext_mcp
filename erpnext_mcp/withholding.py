# SPDX-License-Identifier: MIT
"""Federal withholding calculation engine — a PURE FUNCTION.

No database reads, no side effects, fully testable with deterministic inputs.

Implements the IRS percentage method (2020+ W-4) from Publication 15-T, plus
FICA (Social Security, Medicare, Additional Medicare) and FUTA.
"""

from __future__ import annotations

PERIODS_PER_YEAR = {
	"Annual": 1,
	"Monthly": 12,
	"Semimonthly": 24,
	"Biweekly": 26,
	"Weekly": 52,
	"Daily": 260,
}

# Standard deduction amounts by filing status (2025 — used when the W-4
# does not specify Step 4(b) deductions). These are the amounts built into
# the percentage method tables, so they are NOT subtracted again.
#
# The percentage method tables already incorporate the standard deduction.
# Step 4(b) is for ADDITIONAL deductions beyond the standard deduction.


def calculate_federal_withholding(
	gross_pay: float,
	pay_frequency: str,
	w4_data: dict,
	ytd_gross: float,
	ytd_ss_withheld: float,
	fica_config: dict,
	tax_table: list[dict],
	fica_gross: float | None = None,
) -> dict:
	"""Calculate all federal payroll taxes for one pay period.

	Args:
	    gross_pay: Gross pay for this pay period. Where pre-tax deductions are
	        in play this is the INCOME TAX base — gross less every pre-tax
	        election — because that is what the percentage method annualizes.
	    pay_frequency: One of the PERIODS_PER_YEAR keys.
	    w4_data: Dict with W-4 fields: filing_status, multiple_jobs,
	        dependents_under_17_count, other_dependents_count,
	        total_dependents_credit, other_income, deductions,
	        extra_withholding_per_period, additional_income_from_other_jobs.
	    ytd_gross: Year-to-date gross pay BEFORE this pay period.
	    ytd_ss_withheld: Year-to-date Social Security tax already withheld
	        (employee share).
	    fica_config: Dict with: social_security_rate_employee,
	        social_security_rate_employer, social_security_wage_base,
	        medicare_rate_employee, medicare_rate_employer,
	        additional_medicare_threshold, additional_medicare_rate,
	        futa_rate, futa_wage_base, futa_state_credit_max.
	    tax_table: List of bracket dicts for the matching filing_status and
	        payroll_period, sorted by bracket_floor ascending. Each has:
	        bracket_floor, bracket_ceiling (None for top), base_tax,
	        marginal_rate.
	    fica_gross: The Social Security, Medicare and FUTA base, where it is
	        NOT the same figure as `gross_pay`. Defaults to `gross_pay`, which
	        is right on every slip carrying no pre-tax deduction and was the
	        only possible answer before they existed.

	        THE TWO BASES DIFFER BY EXACTLY THE 401(k). A traditional elective
	        deferral leaves the income tax base under IRC §402(e)(3) and STAYS
	        in the FICA base under §3121(v)(1)(A) — taxed for Social Security
	        and Medicare in the year deferred and never again. A Section 125
	        cafeteria plan benefit leaves both. Running FICA on the income tax
	        base under-withholds every deferral, an error that reconciles
	        cleanly all year and surfaces on a W-2, where Box 1 and Box 3 are
	        supposed to differ by the deferral and instead agree.

	Returns:
	    Dict with all tax amounts and a computation_detail breakdown.
	"""
	periods = PERIODS_PER_YEAR.get(pay_frequency, 1)
	detail = {"pay_frequency": pay_frequency, "periods_per_year": periods}
	fica_base = gross_pay if fica_gross is None else max(float(fica_gross), 0.0)
	if fica_base != gross_pay:
		detail["fica_base"] = round(fica_base, 2)
		detail["fica_base_note"] = (
			"Social Security, Medicare and FUTA were computed on a different base from "
			"federal income tax. The gap is the pre-tax deductions that leave the income "
			"tax base without leaving the FICA one — a traditional 401(k) deferral is the "
			"ordinary case (IRC 402(e)(3) defers the income tax; 3121(v)(1)(A) keeps the "
			"deferral in the FICA wage base)."
		)

	# ── Federal Income Tax (Percentage Method, 2020+ W-4) ─────────────
	fit, fit_detail = _calc_federal_income_tax(
		gross_pay,
		periods,
		w4_data,
		tax_table,
	)
	detail["federal_income_tax"] = fit_detail

	# ── FICA ──────────────────────────────────────────────────────────
	ss_employee, ss_employer, ss_detail = _calc_social_security(
		fica_base,
		ytd_gross,
		ytd_ss_withheld,
		fica_config,
		periods,
	)
	detail["social_security"] = ss_detail

	med_employee, med_employer, addl_med, med_detail = _calc_medicare(
		fica_base,
		ytd_gross,
		fica_config,
	)
	detail["medicare"] = med_detail

	futa, futa_detail = _calc_futa(fica_base, ytd_gross, fica_config)
	detail["futa"] = futa_detail

	total_employee = fit + ss_employee + med_employee + addl_med
	total_employer = ss_employer + med_employer + futa

	effective_rate = 0.0
	if gross_pay > 0:
		effective_rate = round(total_employee / gross_pay * 100, 4)

	return {
		"federal_income_tax": round(fit, 2),
		"social_security_employee": round(ss_employee, 2),
		"social_security_employer": round(ss_employer, 2),
		"medicare_employee": round(med_employee, 2),
		"medicare_employer": round(med_employer, 2),
		"additional_medicare": round(addl_med, 2),
		"futa_employer": round(futa, 2),
		"total_employee_tax": round(total_employee, 2),
		"total_employer_tax": round(total_employer, 2),
		"effective_rate": effective_rate,
		"computation_detail": detail,
	}


def _calc_federal_income_tax(
	gross_pay: float,
	periods: int,
	w4_data: dict,
	tax_table: list[dict],
) -> tuple[float, dict]:
	"""The percentage method for a 2020+ W-4."""
	detail: dict = {}

	# Step 2(c): the box asking the IRS's Higher Withholding Rate Schedule to
	# be used instead of the standard percentage-method table below — NOT
	# implemented. This app has one table per filing_status/pay_frequency/
	# tax_year and no second, higher-rate variant, so a W-4 with the box
	# checked is computed exactly as if it were unchecked. That under-withholds
	# a two-income household, silently, unless a caller reads this flag: the
	# checkbox exists on the form (w4_data["multiple_jobs"]), is stored
	# (tools/w4.py), and is simply not read past this point.
	# Step 2(c), v0.291.0: with the box checked the tables must be the Step 2 CHECKBOX schedules (Pub 15-T, right-hand
	# columns), which the caller loads for this employee. A standard table here means none was loaded for the year:
	# that under-withholds, and the slip says so.
	checkbox = bool(w4_data.get("multiple_jobs"))
	if checkbox and not any(row.get("schedule") == CHECKBOX for row in tax_table or []):
		detail["step_2c_checkbox_not_applied"] = (
			"Step 2(c) is checked on this employee's W-4, but no Step 2 checkbox table is loaded for this tax year, so "
			"federal income tax was computed on the standard table — which under-withholds. Load the year's checkbox "
			"schedule (import_federal_tax_table, schedule Step 2 Checkbox).")
	elif checkbox:
		detail["schedule"] = CHECKBOX

	# Step 1: Annualize gross pay
	annual_gross = gross_pay * periods
	detail["annual_gross"] = round(annual_gross, 2)

	# Step 2: Subtract Step 4(b) deductions — and, v0.286.1, Worksheet 1A line 1(g): $12,900 (married filing jointly)
	# or $8,600 (otherwise) when the Step 2 box is NOT checked, 0 when it is. With the table's 0% band it makes up
	# the standard deduction ($8,600 + $7,500 = $16,100 single in 2026); without it every paycheck over-withheld.
	deductions = float(w4_data.get("deductions") or 0)
	line_1g = 0.0 if w4_data.get("multiple_jobs") else _line_1g(w4_data)
	adjusted = max(annual_gross - deductions - line_1g, 0)
	detail["step_4b_deductions"] = round(deductions, 2)
	detail["worksheet_1a_line_1g"] = line_1g
	detail["adjusted_annual_income"] = round(adjusted, 2)

	# Step 2 multiple-jobs: add other-jobs income
	other_jobs = float(w4_data.get("additional_income_from_other_jobs") or 0)
	if other_jobs > 0:
		adjusted += other_jobs
		detail["step_2_other_jobs_income"] = round(other_jobs, 2)
		detail["adjusted_with_other_jobs"] = round(adjusted, 2)

	# Step 4(a): Add other income
	other_income = float(w4_data.get("other_income") or 0)
	if other_income > 0:
		adjusted += other_income
		detail["step_4a_other_income"] = round(other_income, 2)
		detail["adjusted_with_other_income"] = round(adjusted, 2)

	# Step 3: Look up tentative withholding from tax table — ANNUAL brackets, because the income is annualized
	# above. v0.286.1: a stored per-period row (the install seeds Weekly, Biweekly, … rows divided down from the
	# annual table) is scaled back up; walking an annual income through per-period brackets withheld more than
	# three times the tax ($353.57 for $1,000 a week, single, against $101.69).
	tentative, bracket_detail = _apply_tax_table(adjusted, annual_brackets(tax_table))
	detail["tentative_annual_tax"] = round(tentative, 2)
	detail["bracket_applied"] = bracket_detail

	# Step 4: Subtract dependents credit
	dependents_credit = float(w4_data.get("total_dependents_credit") or 0)
	after_credits = max(tentative - dependents_credit, 0)
	detail["dependents_credit"] = round(dependents_credit, 2)
	detail["after_credits"] = round(after_credits, 2)

	# Convert back to per-period
	per_period = after_credits / periods
	detail["per_period_before_extra"] = round(per_period, 2)

	# Step 5: Add Step 4(c) extra withholding
	extra = float(w4_data.get("extra_withholding_per_period") or 0)
	per_period += extra
	detail["extra_withholding"] = round(extra, 2)

	result = max(per_period, 0)
	detail["final_per_period"] = round(result, 2)

	return result, detail


#: Pub 15-T Worksheet 1A line 1(g), unchanged 2020–2026: subtracted when the W-4 Step 2 box is not checked.
LINE_1G = {"Married Filing Jointly": 12900.0}
LINE_1G_OTHERWISE = 8600.0


def _line_1g(w4_data: dict) -> float:
	status = FILING_STATUS_MAP.get(str(w4_data.get("filing_status") or ""), str(w4_data.get("filing_status") or ""))
	return LINE_1G.get(status, LINE_1G_OTHERWISE)


def annual_brackets(tax_table: list[dict]) -> list[dict]:
	"""The brackets in annual dollars. A row that names a non-annual `payroll_period` is scaled by that period's
	count; a row that names none is taken as annual (the shape every caller passed before v0.286.1)."""
	out = []
	for row in tax_table or []:
		periods = PERIODS_PER_YEAR.get(str(row.get("payroll_period") or "Annual"), 1)
		if periods == 1:
			out.append(row)
			continue
		ceiling = row.get("bracket_ceiling")
		out.append({**row, "payroll_period": "Annual",
		            "bracket_floor": round(float(row.get("bracket_floor") or 0) * periods, 2),
		            "bracket_ceiling": round(float(ceiling) * periods, 2) if ceiling else None,
		            "base_tax": round(float(row.get("base_tax") or 0) * periods, 2)})
	return out


def _apply_tax_table(annual_income: float, brackets: list[dict]) -> tuple[float, dict]:
	"""Walk the brackets, return the tax and the bracket that applied."""
	if not brackets:
		return 0.0, {"error": "no brackets available"}

	sorted_brackets = sorted(brackets, key=lambda b: float(b.get("bracket_floor", 0)))

	applied = sorted_brackets[0]
	for bracket in sorted_brackets:
		floor = float(bracket.get("bracket_floor", 0))
		if annual_income >= floor:
			applied = bracket
		else:
			break

	floor = float(applied.get("bracket_floor", 0))
	base = float(applied.get("base_tax", 0))
	rate = float(applied.get("marginal_rate", 0)) / 100.0
	ceiling = applied.get("bracket_ceiling")

	taxable_in_bracket = annual_income - floor
	if ceiling is not None and ceiling:
		taxable_in_bracket = min(taxable_in_bracket, float(ceiling) - floor)
	taxable_in_bracket = max(taxable_in_bracket, 0)

	tax = base + taxable_in_bracket * rate

	return tax, {
		"bracket_floor": floor,
		"bracket_ceiling": ceiling,
		"base_tax": base,
		"marginal_rate": rate * 100,
		"taxable_in_bracket": round(taxable_in_bracket, 2),
		"computed_tax": round(tax, 2),
	}


def _calc_social_security(
	gross_pay: float,
	ytd_gross: float,
	ytd_ss_withheld: float,
	config: dict,
	periods: int,
) -> tuple[float, float, dict]:
	"""SS tax: 6.2% on gross up to the wage base, checking YTD."""
	wage_base = float(config.get("social_security_wage_base", 176100))
	ee_rate = float(config.get("social_security_rate_employee", 6.2)) / 100.0
	er_rate = float(config.get("social_security_rate_employer", 6.2)) / 100.0

	remaining_base = max(wage_base - ytd_gross, 0)
	taxable = min(gross_pay, remaining_base)

	ee = taxable * ee_rate
	er = taxable * er_rate

	return (
		ee,
		er,
		{
			"wage_base": wage_base,
			"ytd_gross": ytd_gross,
			"remaining_base": round(remaining_base, 2),
			"taxable_this_period": round(taxable, 2),
			"employee_rate": ee_rate * 100,
			"employer_rate": er_rate * 100,
			"employee_tax": round(ee, 2),
			"employer_tax": round(er, 2),
		},
	)


def _calc_medicare(
	gross_pay: float,
	ytd_gross: float,
	config: dict,
) -> tuple[float, float, float, dict]:
	"""Medicare: 1.45% on all gross + additional 0.9% over $200k YTD threshold."""
	ee_rate = float(config.get("medicare_rate_employee", 1.45)) / 100.0
	er_rate = float(config.get("medicare_rate_employer", 1.45)) / 100.0
	addl_threshold = float(config.get("additional_medicare_threshold", 200000))
	addl_rate = float(config.get("additional_medicare_rate", 0.9)) / 100.0

	ee = gross_pay * ee_rate
	er = gross_pay * er_rate

	# Additional Medicare: employee-only, on gross over $200k YTD
	addl = 0.0
	new_ytd = ytd_gross + gross_pay
	if new_ytd > addl_threshold:
		addl_base = new_ytd - max(ytd_gross, addl_threshold)
		addl_base = max(addl_base, 0)
		addl = addl_base * addl_rate

	return (
		ee,
		er,
		addl,
		{
			"employee_rate": ee_rate * 100,
			"employer_rate": er_rate * 100,
			"employee_tax": round(ee, 2),
			"employer_tax": round(er, 2),
			"additional_medicare_threshold": addl_threshold,
			"additional_medicare_rate": addl_rate * 100,
			"ytd_gross_before": ytd_gross,
			"ytd_gross_after": new_ytd,
			"additional_medicare_base": round(new_ytd - max(ytd_gross, addl_threshold), 2)
			if new_ytd > addl_threshold
			else 0,
			"additional_medicare_tax": round(addl, 2),
		},
	)


def _calc_futa(
	gross_pay: float,
	ytd_gross: float,
	config: dict,
) -> tuple[float, dict]:
	"""FUTA: employer-only, 6.0% on first $7k per employee minus state credit."""
	futa_rate = float(config.get("futa_rate", 6.0)) / 100.0
	futa_wage_base = float(config.get("futa_wage_base", 7000))
	state_credit = float(config.get("futa_state_credit_max", 5.4)) / 100.0
	effective_rate = max(futa_rate - state_credit, 0)

	remaining = max(futa_wage_base - ytd_gross, 0)
	taxable = min(gross_pay, remaining)

	tax = taxable * effective_rate

	return tax, {
		"futa_rate": futa_rate * 100,
		"state_credit": state_credit * 100,
		"effective_rate": effective_rate * 100,
		"wage_base": futa_wage_base,
		"ytd_gross": ytd_gross,
		"remaining_base": round(remaining, 2),
		"taxable_this_period": round(taxable, 2),
		"futa_tax": round(tax, 2),
	}


# ── Federal tax brackets (IRS Publication 15-T, Percentage Method, ANNUAL tables) ──
# v0.291.0. Transcribed as printed from the year's Pub 15-T, "Percentage Method Tables for Automated Payroll
# Systems", both schedules: STANDARD (W-4 Step 2 box NOT checked; used with Worksheet 1A line 1(g)) and STEP 2
# CHECKBOX (box checked; line 1(g) is zero). `test_withholding_2026` checks every row follows from the one before.
# Until v0.291.0 the table labelled 2025 held the 2024 schedule (6,000 / 17,600 / 53,150 …).
def _rows(*spec) -> list:
	return [{"bracket_floor": f, "bracket_ceiling": c, "base_tax": b, "marginal_rate": r} for f, c, b, r in spec]


STANDARD = "Standard"
CHECKBOX = "Step 2 Checkbox"

#: Pub 15-T (2025), page 11.
ANNUAL_BRACKETS = {
	"Married Filing Jointly": _rows((0, 17100, 0, 0), (17100, 40950, 0, 10), (40950, 114050, 2385.00, 12),
	                                (114050, 223800, 11157.00, 22), (223800, 411700, 35302.00, 24),
	                                (411700, 518150, 80398.00, 32), (518150, 768700, 114462.00, 35),
	                                (768700, None, 202154.50, 37)),
	"Single": _rows((0, 6400, 0, 0), (6400, 18325, 0, 10), (18325, 54875, 1192.50, 12), (54875, 109750, 5578.50, 22),
	                (109750, 203700, 17651.00, 24), (203700, 256925, 40199.00, 32), (256925, 632750, 57231.00, 35),
	                (632750, None, 188769.75, 37)),
	"Head of Household": _rows((0, 13900, 0, 0), (13900, 30900, 0, 10), (30900, 78750, 1700.00, 12),
	                           (78750, 117250, 7442.00, 22), (117250, 211200, 15912.00, 24),
	                           (211200, 264400, 38460.00, 32), (264400, 640250, 55484.00, 35),
	                           (640250, None, 187031.50, 37)),
}
ANNUAL_BRACKETS_2025_CHECKBOX = {
	"Married Filing Jointly": _rows((0, 15000, 0, 0), (15000, 26925, 0, 10), (26925, 63475, 1192.50, 12),
	                                (63475, 118350, 5578.50, 22), (118350, 212300, 17651.00, 24),
	                                (212300, 265525, 40199.00, 32), (265525, 390800, 57231.00, 35),
	                                (390800, None, 101077.25, 37)),
	"Single": _rows((0, 7500, 0, 0), (7500, 13463, 0, 10), (13463, 31738, 596.25, 12), (31738, 59175, 2789.25, 22),
	                (59175, 106150, 8825.50, 24), (106150, 132763, 20099.50, 32), (132763, 320675, 28615.50, 35),
	                (320675, None, 94384.88, 37)),
	"Head of Household": _rows((0, 11250, 0, 0), (11250, 19750, 0, 10), (19750, 43675, 850.00, 12),
	                           (43675, 62925, 3721.00, 22), (62925, 109900, 7956.00, 24), (109900, 136500, 19230.00, 32),
	                           (136500, 324425, 27742.00, 35), (324425, None, 93515.75, 37)),
}

#: What the install seeded as "2025" before v0.291.0 (the 2024 schedule). Kept only so a migrate can recognise those
#: rows, untouched by any operator, and replace them with the real 2025 table.
LEGACY_2025_SEED = {
	"Single": _rows((0, 6000, 0, 0), (6000, 17600, 0, 10), (17600, 53150, 1160, 12), (53150, 106525, 5426, 22),
	                (106525, 197950, 17168.50, 24), (197950, 252500, 39110.50, 32), (252500, 631900, 56576.50, 35),
	                (631900, None, 189365.50, 37)),
}

SEED_TAX_YEAR = 2026


# Mapping from W-4 filing status to tax table filing status
FILING_STATUS_MAP = {
	"Single or Married Filing Separately": "Single",
	"Married Filing Jointly": "Married Filing Jointly",
	"Head of Household": "Head of Household",
}


def _row(floor, ceiling, base, rate) -> dict:
	return {"bracket_floor": floor, "bracket_ceiling": ceiling, "base_tax": base, "marginal_rate": rate}


#: v0.286.1. IRS Publication 15-T (2026), "2026 Percentage Method Tables for Automated Payroll Systems", ANNUAL,
#: STANDARD withholding rate schedules (W-4 Step 2 box NOT checked), page 12, transcribed as printed. Used with
#: Worksheet 1A line 1(g) (LINE_1G). Each row's base is the previous row's base plus its rate times its width —
#: `test_withholding_2026` checks every one.
ANNUAL_BRACKETS_2026 = {
	"Married Filing Jointly": [
		_row(0, 19300, 0, 0), _row(19300, 44100, 0, 10), _row(44100, 120100, 2480.00, 12),
		_row(120100, 230700, 11600.00, 22), _row(230700, 422850, 35932.00, 24), _row(422850, 531750, 82048.00, 32),
		_row(531750, 788000, 116896.00, 35), _row(788000, None, 206583.50, 37),
	],
	"Single": [
		_row(0, 7500, 0, 0), _row(7500, 19900, 0, 10), _row(19900, 57900, 1240.00, 12),
		_row(57900, 113200, 5800.00, 22), _row(113200, 209275, 17966.00, 24), _row(209275, 263725, 41024.00, 32),
		_row(263725, 648100, 58448.00, 35), _row(648100, None, 192979.25, 37),
	],
	"Head of Household": [
		_row(0, 15550, 0, 0), _row(15550, 33250, 0, 10), _row(33250, 83000, 1770.00, 12),
		_row(83000, 121250, 7740.00, 22), _row(121250, 217300, 16155.00, 24), _row(217300, 271750, 39207.00, 32),
		_row(271750, 656150, 56631.00, 35), _row(656150, None, 191171.00, 37),
	],
}
#: Pub 15-T (2026), page 12, the right-hand (Step 2 checkbox) schedules.
ANNUAL_BRACKETS_2026_CHECKBOX = {
	"Married Filing Jointly": [
		_row(0, 16100, 0, 0), _row(16100, 28500, 0, 10), _row(28500, 66500, 1240.00, 12),
		_row(66500, 121800, 5800.00, 22), _row(121800, 217875, 17966.00, 24), _row(217875, 272325, 41024.00, 32),
		_row(272325, 400450, 58448.00, 35), _row(400450, None, 103291.75, 37),
	],
	"Single": [
		_row(0, 8050, 0, 0), _row(8050, 14250, 0, 10), _row(14250, 33250, 620.00, 12),
		_row(33250, 60900, 2900.00, 22), _row(60900, 108938, 8983.00, 24), _row(108938, 136163, 20512.00, 32),
		_row(136163, 328350, 29224.00, 35), _row(328350, None, 96489.63, 37),
	],
	"Head of Household": [
		_row(0, 12075, 0, 0), _row(12075, 20925, 0, 10), _row(20925, 45800, 885.00, 12),
		_row(45800, 64925, 3870.00, 22), _row(64925, 112950, 8077.50, 24), _row(112950, 140175, 19603.50, 32),
		_row(140175, 332375, 28315.50, 35), _row(332375, None, 95585.50, 37),
	],
}
#: Every (year, schedule) this app seeds.
SEEDED_YEARS = {
	2025: {STANDARD: ANNUAL_BRACKETS, CHECKBOX: ANNUAL_BRACKETS_2025_CHECKBOX},
	2026: {STANDARD: ANNUAL_BRACKETS_2026, CHECKBOX: ANNUAL_BRACKETS_2026_CHECKBOX},
}


def seed_brackets(tax_year: int = SEED_TAX_YEAR, schedule: str = STANDARD) -> list[dict]:
	"""Return the full set of seed brackets for all periods of one tax year and schedule, ready for insertion."""
	rows = []
	for filing_status, annual in SEEDED_YEARS[tax_year][schedule].items():
		for period_name, periods in PERIODS_PER_YEAR.items():
			for bracket in annual:
				floor = bracket["bracket_floor"] / periods
				ceiling = bracket["bracket_ceiling"] / periods if bracket["bracket_ceiling"] else None
				base = bracket["base_tax"] / periods
				rows.append(
					{
						"tax_year": tax_year,
						"schedule": schedule,
						"filing_status": filing_status,
						"payroll_period": period_name,
						"bracket_floor": round(floor, 2),
						"bracket_ceiling": round(ceiling, 2) if ceiling else None,
						"base_tax": round(base, 2),
						"marginal_rate": bracket["marginal_rate"],
					}
				)
	return rows

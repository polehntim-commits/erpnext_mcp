# SPDX-License-Identifier: MIT
"""State withholding calculation engines — PURE FUNCTIONS.

No database reads, no side effects, fully testable with deterministic inputs.

v0.29.0. Oregon and Washington. The critical design decision: wage law follows
WORK LOCATION per shift, not employer HQ. An employee who picks cherries in
Hood River on Monday and in White Salmon on Tuesday owes Oregon tax on Monday's
gross and Washington programs on Tuesday's.

Oregon: income tax (ORS 316.037 brackets), Statewide Transit Tax (0.1%),
Paid Leave Oregon (ORS 657B), Workers' Compensation (employer-entered rate).

Washington: Paid Family & Medical Leave (RCW 50A), WA Cares Fund (RCW 50B),
Labor & Industries (workers' comp, employer-entered rates). NO income tax.

v0.40.0 adds STATE UNEMPLOYMENT (SUTA) to both, and it is employer-entered for
the same reason workers' compensation is: a SUTA rate is assigned to one
employer by one state agency out of that employer's own experience rating, and
there is no table anybody could ship. The rate defaults to ZERO, which means a
site that has not entered one computes exactly what it computed before this
release — the addition is a place to put a number, not a new charge.

It sits beside FUTA rather than inside it on purpose: FUTA is federal, filed on
the 940, and its credit already assumes state unemployment tax was paid. The
two are separate liabilities to separate agencies and they reconcile against
each other, which they cannot do from one account.
"""

from __future__ import annotations

from .withholding import PERIODS_PER_YEAR

SUPPORTED_STATES = ("OR", "WA")

# ── 2025 Oregon Income Tax Brackets (ORS 316.037) ─────────────────────────
# Annual brackets. The engine annualizes gross, looks up the bracket, then
# de-annualizes back to the pay period — same pattern as the federal engine.
#
# THESE ARE SEED DATA, NOT THE TAX TABLE. v0.91.0. No calculation in this module
# reads them: `calculate_oregon_withholding` is handed a bracket list by its
# caller, and on a live site that list comes from the State Tax Table doctype via
# `tools/state_tax._load_state_table`. What these constants are for is
# `seed_or_brackets`, which `install._state_tax_table` inserts once on a site
# whose table is empty — and after that an operator owns the rows.
#
# So a new tax year does NOT come back here. It arrives through
# `import_state_tax_table` with the year's own floors, which is the point of the
# doctype: Oregon publishes its brackets in December and a farm should not need a
# release to withhold correctly in January. What this file still pins is the
# 2025 figures a fresh install starts from, and the year it says they belong to.

OR_SEED_TAX_YEAR = 2025

OR_ANNUAL_BRACKETS = {
	"Single": [
		{"bracket_floor": 0, "bracket_ceiling": 4050, "base_tax": 0, "marginal_rate": 4.75},
		{"bracket_floor": 4050, "bracket_ceiling": 10200, "base_tax": 192.38, "marginal_rate": 6.75},
		{"bracket_floor": 10200, "bracket_ceiling": 125000, "base_tax": 607.50, "marginal_rate": 8.75},
		{"bracket_floor": 125000, "bracket_ceiling": None, "base_tax": 10657.50, "marginal_rate": 9.9},
	],
	"Married Filing Jointly": [
		{"bracket_floor": 0, "bracket_ceiling": 8100, "base_tax": 0, "marginal_rate": 4.75},
		{"bracket_floor": 8100, "bracket_ceiling": 20400, "base_tax": 384.75, "marginal_rate": 6.75},
		{"bracket_floor": 20400, "bracket_ceiling": 250000, "base_tax": 1215.00, "marginal_rate": 8.75},
		{"bracket_floor": 250000, "bracket_ceiling": None, "base_tax": 21315.00, "marginal_rate": 9.9},
	],
	"Head of Household": [
		{"bracket_floor": 0, "bracket_ceiling": 4050, "base_tax": 0, "marginal_rate": 4.75},
		{"bracket_floor": 4050, "bracket_ceiling": 10200, "base_tax": 192.38, "marginal_rate": 6.75},
		{"bracket_floor": 10200, "bracket_ceiling": 125000, "base_tax": 607.50, "marginal_rate": 8.75},
		{"bracket_floor": 125000, "bracket_ceiling": None, "base_tax": 10657.50, "marginal_rate": 9.9},
	],
}

OR_FILING_STATUS_MAP = {
	"Single or Married Filing Separately": "Single",
	"Married Filing Jointly": "Married Filing Jointly",
	"Head of Household": "Head of Household",
}


def calculate_suta(
	gross_pay: float,
	ytd_gross: float,
	state_config: dict,
) -> tuple[float, dict]:
	"""State unemployment insurance: employer-only, rated, wage-based.

	Zero unless the employer has entered its own assigned rate, because there is
	no rate this app could know. Oregon's 2025 taxable wage base is $54,300 and
	Washington's is $72,800, but both move every year and both are per-employer
	facts once the experience rating is applied — so the base is a field too,
	and a base of zero means "no cap", which is the honest reading of a site that
	entered a rate and left the base blank.

	The wage base is consumed by YEAR-TO-DATE GROSS, exactly as FUTA's is in
	`withholding._calc_futa`. A period computed in isolation would restart it and
	over-collect on anybody who has already crossed it.
	"""
	rate = float(state_config.get("suta_rate", 0) or 0) / 100.0
	wage_base = float(state_config.get("suta_wage_base", 0) or 0)

	if rate <= 0:
		return 0.0, {
			"rate": 0.0,
			"wage_base": wage_base,
			"taxable_wages": 0.0,
			"tax": 0.0,
			"note": (
				"no SUTA rate configured for this state, so none is computed. The rate "
				"is assigned to this employer by the state agency and cannot be inferred."
			),
		}

	if wage_base > 0:
		remaining = max(wage_base - float(ytd_gross or 0), 0.0)
		taxable = min(gross_pay, remaining)
	else:
		taxable = gross_pay

	tax = round(max(taxable, 0.0) * rate, 2)
	return tax, {
		"rate": rate * 100,
		"wage_base": wage_base,
		"ytd_gross": round(float(ytd_gross or 0), 2),
		"taxable_wages": round(max(taxable, 0.0), 2),
		"tax": tax,
		"wage_base_exhausted": bool(wage_base > 0 and taxable < gross_pay),
	}


# ── Oregon withholding: the Department of Revenue's computer formula (v0.291.0) ──────────────────────────────────
#: Oregon Withholding Tax Formulas, 150-206-436 (Rev. 12-31-25), effective 2026-01-01, as printed. Each schedule row is
#: (BASE from, BASE minus, constant, rate %): WH = constant + (BASE − minus) × rate − credit × allowances, annual.
#: "low" applies to annual wages under $50,000 (its constants carry one exemption credit); "high" to $50,000 and up.
#: Stored as data (payroll setting `oregon_withholding`); this is the seed and the pure-function default.
OR_WITHHOLDING_2026 = {
	"effective_from": "2026-01-01",
	"source": "Oregon DOR, Withholding Tax Formulas 150-206-436 (Rev. 12-31-25), effective January 1, 2026",
	"standard_deduction": {"single": 2910.0, "married": 5820.0},
	"exemption_credit": 263.0,
	"high_wage_threshold": 50000.0,
	"allowances_zeroed_above": {"single": 100000.0, "married": 200000.0},
	"federal_subtraction": {
		"single": [[0, 8750], [125000, 7000], [130000, 5250], [135000, 3500], [140000, 1750], [145000, 0]],
		"married": [[0, 8750], [250000, 7000], [260000, 5250], [270000, 3500], [280000, 1750], [290000, 0]],
	},
	"schedules": {
		"single_low": [[0, 0, 263, 4.75], [4550, 4550, 479, 6.75], [11400, 11400, 941, 8.75]],
		"single_high": [[0, 11400, 678, 8.75], [125000, 125000, 10618, 9.9]],
		"married_low": [[0, 0, 263, 4.75], [9100, 9100, 695, 6.75], [22800, 22800, 1620, 8.75]],
		"married_high": [[0, 22800, 1357, 8.75], [250000, 250000, 21237, 9.9]],
	},
	#: HB 2119 (2019): no OR-W-4 (withholding statement or exemption certificate) on file → a flat 8% of wages.
	"default_flat_rate": 8.0,
	#: v0.294.0. Certificates the employer sends to the DOR (OAR 150-316-0290(4)): more than 10 allowances, exempt
	#: with expected wages over $200 a week, or a state-only exemption. "Within 20 days" — of receipt, read here as
	#: of the employee's signature. Have the CPA confirm the reading.
	"forwarding": {"max_allowances": 10, "exempt_weekly_wages": 200.0, "days": 20,
	               #: v0.312.0. What the days count from: "signature" (the employee's signature date — Tim's
	               #: conservative reading, 2026-10-09) or "received" (the day the employer received the form).
	               "basis": "signature",
	               "source": "OAR 150-316-0290(4)"},
	#: v0.295.0. Whether the DOR has confirmed it accepts electronically signed OR-W-4s. Unconfirmed until Tim/the CPA
	#: hears from the DOR; every OR-W-4 signed or rendered says so.
	#: v0.308.0. E-signed W-4 / OR-W-4. Oregon has no explicit rule: ORS 84.019 (UETA) generally gives an electronic
	#: signature legal effect, ORS 84.052 lets an agency opt out, and the DOR has done neither in writing — so the
	#: flow is built to the IRS standard for electronic W-4s, 26 CFR 31.3402(f)(5)-1(c), and `confirmed` stays false
	#: until the DOR confirms in writing.
	"esignature": {
		"confirmed": False,
		"status": "awaiting DOR written confirmation",
		"note": "",
		"standard": "26 CFR 31.3402(f)(5)-1(c)",
		"state_law": "ORS 84.019 (UETA); ORS 84.052 (agency opt-out) — the Oregon DOR has published neither an "
		             "acceptance nor an opt-out for Form OR-W-4",
		"applies_to": ["signature", "or_signature"],
		#: (1) The signer is authenticated: a badge the server resolves, or an in-person photo-ID check attested by a
		#: signed-in hiring/HR user ("Employee ID"). A photograph alone is client-attested and is not enough.
		"require_verified_signer": True,
		"verified_methods": ["Badge QR", "Employee ID"],
		#: (2) The perjury statement immediately precedes the signature — the box's own words, recorded on the
		#: evidence row. (3) The signature is the final entry: a signed form's elections are locked.
		"lock_after_signature": True,
		#: (4) Every read or render of a signed form is logged (Frappe's Access Log).
		"access_log": True,
		#: (5) A hard copy reproducible on the official form: render_w4_pdf (form=or_w4 for the DOR's 150-101-402).
		"hard_copy": "render_w4_pdf",
	},
	#: v0.308.0. No OR-W-4 (and no pre-2020 W-4 in the fallback order) — including when a 2020-or-later federal W-4
	#: is on file — Oregon withholding is a flat 8%, not Single with no allowances.
	"no_or_w4_rule": "OAR 150-316-0234; ORS 316.182 (HB 2119)",
	#: v0.295.0. When the employer withholds a flat 8% instead of using the certificate (DOR Publication 150-211-602,
	#: Rev. 02-18-22): an Oregon-only exemption, and an exemption that expired with no new OR-W-4.
	"flat_rate_rules_source": "Oregon DOR Publication 150-211-602, W-4 Information for Employers (Rev. 02-18-22)",
}

#: OR-W-4 filing statuses (Form OR-W-4 line 1).
OR_SINGLE, OR_MARRIED, OR_MARRIED_SINGLE_RATE = "Single", "Married", "Married, withhold at higher Single rate"


def oregon_annual_withholding(annual_wages: float, annual_federal_tax: float, or_w4: dict, params: dict) -> dict:
	"""The DOR formula on annual figures: {annual, base, group, federal_subtraction, allowances, …}. Pure."""
	status = str(or_w4.get("filing_status") or OR_SINGLE)
	allowances = max(int(or_w4.get("allowances") or 0), 0)
	married_box = status == OR_MARRIED
	# Which rate group: married, or single with three or more allowances, use the married schedule and deduction;
	# "married, withhold at the higher single rate" and head of household are single.
	group = "married" if (married_box or (status != OR_MARRIED_SINGLE_RATE and allowances >= 3)) else "single"
	# The phase-out follows the BOX: only a married box uses the married phase-out (FAQ 4 and 7).
	phase = "married" if married_box else "single"
	limit_key = "married" if married_box else "single"
	if annual_wages > float(params["allowances_zeroed_above"][limit_key]):
		allowances = 0
	cap = 0.0
	for start, amount in params["federal_subtraction"][phase]:
		if annual_wages >= float(start):
			cap = float(amount)
	subtraction = min(max(float(annual_federal_tax), 0.0), cap)
	base = max(annual_wages - subtraction - float(params["standard_deduction"][group]), 0.0)
	rows = params["schedules"][f"{group}_{'high' if annual_wages >= float(params['high_wage_threshold']) else 'low'}"]
	applied = rows[0]
	for row in rows:
		if base >= float(row[0]):
			applied = row
	start, minus, constant, rate = (float(x) for x in applied)
	tax = constant + (base - minus) * rate / 100.0 - float(params["exemption_credit"]) * allowances
	return {"annual": round(max(tax, 0.0), 2), "base": round(base, 2), "group": group, "phase_out": phase,
	        "federal_subtraction": round(subtraction, 2), "allowances": allowances,
	        "row": {"from": start, "minus": minus, "constant": constant, "rate": rate}}


def calculate_oregon_withholding(
	gross_pay: float,
	pay_frequency: str,
	filing_status: str,
	state_config: dict,
	state_tax_table: list[dict],
	ytd_gross: float = 0.0,
	*,
	or_w4: dict | None = None,
	federal_income_tax: float = 0.0,
	params: dict | None = None,
	pay_year: int | None = None,
) -> dict:
	"""Calculate all Oregon payroll taxes for one pay period.

	Args:
	    gross_pay: Gross pay for this pay period.
	    pay_frequency: One of the PERIODS_PER_YEAR keys.
	    filing_status: The tax-table filing status (Single, Married Filing
	        Jointly, Head of Household).
	    state_config: Dict with OR-specific config fields.
	    state_tax_table: List of bracket dicts for the matching filing_status,
	        sorted by bracket_floor ascending.
	    ytd_gross: Year-to-date gross BEFORE this period, for the SUTA wage
	        base. Zero — the default — is right for the first period of a year
	        and understates how much of the base is already consumed after it.

	Returns:
	    Dict with all OR tax amounts and a computation_detail breakdown.
	"""
	periods = PERIODS_PER_YEAR.get(pay_frequency, 1)
	detail = {"state": "OR", "pay_frequency": pay_frequency, "periods_per_year": periods}

	# ── Oregon Income Tax ─────────────────────────────────────────────
	# v0.291.0: the DOR's computer formula (150-206-436) on the employee's OR-W-4 — wages less the federal tax
	# withheld (capped, phased out) less the standard deduction, the published rates, less the exemption credit per
	# allowance — not the income-tax brackets applied to gross. No OR-W-4 on file: a flat 8% (HB 2119). An exemption
	# certificate lapses on February 15 of the next year. `state_tax_table` is no longer read for income tax.
	or_income_tax = 0.0
	income_enabled = bool(int(state_config.get("or_income_tax_enabled", 1)))
	params = params or OR_WITHHOLDING_2026
	w4 = dict(or_w4 or {})
	if not income_enabled:
		detail["income_tax"] = {"note": "disabled on this site's State Tax Configuration"}
	elif not w4.get("on_file"):
		rate = float(params.get("default_flat_rate", 8.0))
		or_income_tax = round(gross_pay * rate / 100.0, 2)
		rule = params.get("no_or_w4_rule") or "OAR 150-316-0234; ORS 316.182 (HB 2119)"
		why = ("A 2020-or-later federal W-4 is on file but no Form OR-W-4 — the federal form carries no Oregon "
		       "allowances, so Oregon withholding is a flat 8%, not Single with no allowances"
		       if w4.get("federal_2020_plus") else "No Form OR-W-4 on file for this employee, so Oregon requires a "
		       "flat 8%")
		detail["income_tax"] = {"method": "flat", "rate": rate, "rule": rule,
		                        "based_on": w4.get("reason") or ("2020+ federal W-4, no OR-W-4"
		                                                         if w4.get("federal_2020_plus") else "no OR-W-4"),
		                        "note": f"{why} ({rule}). It stops once the employee's OR-W-4 is entered."}
	elif w4.get("exempt") and (not w4.get("federally_exempt", True) or not _exemption_in_force(w4, pay_year)):
		# v0.295.0. DOR Publication 150-211-602: an Oregon-only exemption, or one that expired with no new OR-W-4, is
		# withheld at 8% rather than by the certificate.
		rate = float(params.get("default_flat_rate", 8.0))
		or_income_tax = round(gross_pay * rate / 100.0, 2)
		why = ("the OR-W-4 claims an Oregon-only exemption" if not w4.get("federally_exempt", True)
		       else "the OR-W-4 exemption expired (February 15) and no new OR-W-4 was filed")
		detail["income_tax"] = {"method": "flat", "rate": rate, "based_on": w4.get("source") or "OR-W-4",
		                        "note": f"{why} — withhold 8% ({params.get('flat_rate_rules_source')})."}
	elif w4.get("exempt"):
		detail["income_tax"] = {"method": "exempt", "note": "OR-W-4 exemption certificate in force."}
	else:
		annual = oregon_annual_withholding(gross_pay * periods, float(federal_income_tax or 0) * periods, w4, params)
		extra = float(w4.get("additional_withholding") or 0)
		or_income_tax = round(annual["annual"] / periods + extra, 2)
		detail["income_tax"] = {"method": "formula", "source": params.get("source"), **annual,
		                        "additional_per_period": extra, "per_period": or_income_tax,
		                        # v0.294.0. What the elections came from: the OR-W-4, a pre-2020 W-4 in the fallback
		                        # order, or a refused certificate (Single, 0 — OAR 150-316-0290).
		                        "based_on": w4.get("source") or "OR-W-4"}

	# ── Statewide Transit Tax (0.1% of gross, no cap) ─────────────────
	transit_rate = float(state_config.get("or_transit_tax_rate", 0.1)) / 100.0
	or_transit_tax = round(gross_pay * transit_rate, 2)
	detail["transit_tax"] = {
		"rate": transit_rate * 100,
		"tax": or_transit_tax,
	}

	# ── Paid Leave Oregon (ORS 657B) ──────────────────────────────────
	total_rate = float(state_config.get("or_paid_leave_rate", 1.0)) / 100.0
	ee_share_pct = float(state_config.get("or_paid_leave_employee_share", 60.0)) / 100.0
	er_share_pct = float(state_config.get("or_paid_leave_employer_share", 40.0)) / 100.0
	small_employer = bool(int(state_config.get("or_paid_leave_small_employer", 0)))

	total_paid_leave = gross_pay * total_rate
	or_paid_leave_employee = round(total_paid_leave * ee_share_pct, 2)
	if small_employer:
		or_paid_leave_employer = 0.0
	else:
		or_paid_leave_employer = round(total_paid_leave * er_share_pct, 2)

	detail["paid_leave"] = {
		"total_rate": total_rate * 100,
		"employee_share_pct": ee_share_pct * 100,
		"employer_share_pct": er_share_pct * 100,
		"small_employer": small_employer,
		"employee_tax": or_paid_leave_employee,
		"employer_tax": or_paid_leave_employer,
	}

	# ── Workers' Compensation (employer-entered rate) ─────────────────
	wc_rate = float(state_config.get("or_workers_comp_rate", 0)) / 100.0
	or_workers_comp = round(gross_pay * wc_rate, 2)
	detail["workers_comp"] = {
		"rate": wc_rate * 100,
		"tax": or_workers_comp,
	}

	# ── State Unemployment (employer-only, employer-rated) ────────────
	or_suta, suta_detail = calculate_suta(gross_pay, ytd_gross, state_config)
	detail["suta"] = suta_detail

	total_employee = round(or_income_tax + or_transit_tax + or_paid_leave_employee, 2)
	total_employer = round(or_paid_leave_employer + or_workers_comp + or_suta, 2)

	return {
		"state": "OR",
		"or_income_tax": round(or_income_tax, 2),
		"or_transit_tax": or_transit_tax,
		"or_paid_leave_employee": or_paid_leave_employee,
		"or_paid_leave_employer": or_paid_leave_employer,
		"or_workers_comp": or_workers_comp,
		"or_suta": or_suta,
		# `suta` unprefixed as well as `or_suta`, so a caller aggregating across
		# states reads one key rather than switching on the state code. Same
		# figure, and the prefixed one stays because every other amount here is
		# prefixed and dropping the pattern for one field would be the surprise.
		"suta": or_suta,
		"employer_other": round(or_paid_leave_employer + or_workers_comp, 2),
		"total_or_employee": total_employee,
		"total_or_employer": total_employer,
		"computation_detail": detail,
	}


def calculate_washington_withholding(
	gross_pay: float,
	state_config: dict,
	ytd_gross: float = 0.0,
) -> dict:
	"""Calculate all Washington payroll taxes for one pay period.

	Washington has NO income tax — only flat-rate programs.

	Args:
	    gross_pay: Gross pay for this pay period.
	    state_config: Dict with WA-specific config fields.
	    ytd_gross: Year-to-date gross BEFORE this period, for the SUTA wage base.

	Returns:
	    Dict with all WA tax amounts and a computation_detail breakdown.
	"""
	detail = {"state": "WA"}

	# ── Paid Family & Medical Leave (RCW 50A) ─────────────────────────
	pfml_rate = float(state_config.get("wa_pfml_rate", 0.92)) / 100.0
	pfml_ee_share = float(state_config.get("wa_pfml_employee_share", 72.76)) / 100.0
	pfml_er_share = float(state_config.get("wa_pfml_employer_share", 27.24)) / 100.0

	total_pfml = gross_pay * pfml_rate
	wa_pfml_employee = round(total_pfml * pfml_ee_share, 2)
	wa_pfml_employer = round(total_pfml * pfml_er_share, 2)

	detail["pfml"] = {
		"rate": pfml_rate * 100,
		"employee_share_pct": pfml_ee_share * 100,
		"employer_share_pct": pfml_er_share * 100,
		"employee_tax": wa_pfml_employee,
		"employer_tax": wa_pfml_employer,
	}

	# ── WA Cares Fund (RCW 50B) ───────────────────────────────────────
	cares_rate = float(state_config.get("wa_cares_rate", 0.58)) / 100.0
	wa_cares_employee = round(gross_pay * cares_rate, 2)
	detail["cares"] = {
		"rate": cares_rate * 100,
		"employee_only": True,
		"tax": wa_cares_employee,
	}

	# ── Labor & Industries (workers' comp) ────────────────────────────
	li_ee_rate = float(state_config.get("wa_li_rate_employee", 0)) / 100.0
	li_er_rate = float(state_config.get("wa_li_rate_employer", 0)) / 100.0
	wa_li_employee = round(gross_pay * li_ee_rate, 2)
	wa_li_employer = round(gross_pay * li_er_rate, 2)
	detail["labor_and_industries"] = {
		"employee_rate": li_ee_rate * 100,
		"employer_rate": li_er_rate * 100,
		"employee_tax": wa_li_employee,
		"employer_tax": wa_li_employer,
	}

	# ── State Unemployment (employer-only, employer-rated) ────────────
	wa_suta, suta_detail = calculate_suta(gross_pay, ytd_gross, state_config)
	detail["suta"] = suta_detail

	total_employee = round(wa_pfml_employee + wa_cares_employee + wa_li_employee, 2)
	total_employer = round(wa_pfml_employer + wa_li_employer + wa_suta, 2)

	return {
		"state": "WA",
		"wa_pfml_employee": wa_pfml_employee,
		"wa_pfml_employer": wa_pfml_employer,
		"wa_cares_employee": wa_cares_employee,
		"wa_li_employee": wa_li_employee,
		"wa_li_employer": wa_li_employer,
		"wa_suta": wa_suta,
		"suta": wa_suta,
		"employer_other": round(wa_pfml_employer + wa_li_employer, 2),
		"total_wa_employee": total_employee,
		"total_wa_employer": total_employer,
		"computation_detail": detail,
	}


def calculate_state_withholding(
	gross_pay: float,
	pay_frequency: str,
	state: str,
	filing_status: str,
	state_config: dict,
	state_tax_table: list[dict] | None = None,
	ytd_gross: float = 0.0,
	oregon: dict | None = None,
) -> dict:
	"""Route to the correct state engine. The dispatch function. `oregon` (v0.291.0): or_w4, federal_income_tax,
	params, pay_year for the Oregon formula.

	Args:
	    gross_pay: Gross pay for this pay period.
	    pay_frequency: One of the PERIODS_PER_YEAR keys.
	    state: "OR" or "WA".
	    filing_status: Tax-table filing status.
	    state_config: State-specific config dict.
	    state_tax_table: Income tax brackets (OR only; ignored for WA).
	    ytd_gross: Year-to-date gross BEFORE this period, for the SUTA wage base.

	Returns:
	    State withholding result dict.
	"""
	if state == "OR":
		return calculate_oregon_withholding(
			gross_pay,
			pay_frequency,
			filing_status,
			state_config,
			state_tax_table or [],
			ytd_gross,
			**(oregon or {}),
		)
	elif state == "WA":
		return calculate_washington_withholding(gross_pay, state_config, ytd_gross)
	else:
		return {
			"state": state,
			"error": f"unsupported state: {state}",
			"total_employee": 0.0,
			"total_employer": 0.0,
			"suta": 0.0,
			"employer_other": 0.0,
		}


def calculate_all_payroll_taxes(
	gross_pay: float,
	pay_frequency: str,
	work_state: str,
	filing_status: str,
	w4_data: dict,
	ytd_gross: float,
	ytd_ss_withheld: float,
	fica_config: dict,
	federal_tax_table: list[dict],
	state_config: dict,
	state_tax_table: list[dict] | None = None,
) -> dict:
	"""Combined federal + state payroll tax calculation.

	Calls the federal engine (from v0.28.0) and the appropriate state engine,
	returns a unified breakdown.
	"""
	from .withholding import calculate_federal_withholding

	federal = calculate_federal_withholding(
		gross_pay,
		pay_frequency,
		w4_data,
		ytd_gross,
		ytd_ss_withheld,
		fica_config,
		federal_tax_table,
	)

	state = calculate_state_withholding(
		gross_pay,
		pay_frequency,
		work_state,
		filing_status,
		state_config,
		state_tax_table,
		ytd_gross,
	)

	state_ee_key = f"total_{work_state.lower()}_employee"
	state_er_key = f"total_{work_state.lower()}_employer"
	state_employee = state.get(state_ee_key, 0.0)
	state_employer = state.get(state_er_key, 0.0)

	grand_total_employee = round(federal["total_employee_tax"] + state_employee, 2)
	grand_total_employer = round(federal["total_employer_tax"] + state_employer, 2)

	return {
		"federal": federal,
		"state": state,
		"work_state": work_state,
		"grand_total_employee": grand_total_employee,
		"grand_total_employer": grand_total_employer,
		"grand_total_all": round(grand_total_employee + grand_total_employer, 2),
	}


# ── Internal helpers ───────────────────────────────────────────────────────


def _exemption_in_force(w4: dict, pay_year: int | None) -> bool:
	"""An OR-W-4 exemption covers the year it was claimed and lapses February 15 of the next (here: the pay year
	must be the claim year; the caller re-collects the certificate each year, as the DOR requires)."""
	claimed = w4.get("tax_year")
	return pay_year is None or claimed is None or int(claimed) >= int(pay_year)


def _calc_state_income_tax(
	gross_pay: float,
	periods: int,
	tax_table: list[dict],
) -> tuple[float, dict]:
	"""Annualize, walk brackets, de-annualize — same method as federal."""
	annual_gross = gross_pay * periods
	detail = {"annual_gross": round(annual_gross, 2)}

	if not tax_table:
		return 0.0, {**detail, "error": "no brackets available"}

	sorted_brackets = sorted(tax_table, key=lambda b: float(b.get("bracket_floor", 0)))

	applied = sorted_brackets[0]
	for bracket in sorted_brackets:
		floor = float(bracket.get("bracket_floor", 0))
		if annual_gross >= floor:
			applied = bracket
		else:
			break

	floor = float(applied.get("bracket_floor", 0))
	base = float(applied.get("base_tax", 0))
	rate = float(applied.get("marginal_rate", 0)) / 100.0
	ceiling = applied.get("bracket_ceiling")

	taxable_in_bracket = annual_gross - floor
	if ceiling is not None and ceiling:
		taxable_in_bracket = min(taxable_in_bracket, float(ceiling) - floor)
	taxable_in_bracket = max(taxable_in_bracket, 0)

	annual_tax = base + taxable_in_bracket * rate
	per_period = annual_tax / periods

	detail["bracket_applied"] = {
		"bracket_floor": floor,
		"bracket_ceiling": ceiling,
		"base_tax": base,
		"marginal_rate": rate * 100,
		"taxable_in_bracket": round(taxable_in_bracket, 2),
		"annual_tax": round(annual_tax, 2),
	}
	detail["per_period"] = round(per_period, 2)

	return max(per_period, 0), detail


def seed_or_brackets(tax_year: int = OR_SEED_TAX_YEAR) -> list[dict]:
	"""Return Oregon bracket rows for one tax year, ready for insertion.

	THE ROWS ARE ANNUAL, AND THAT IS THE WHOLE CONTRACT. `_calc_state_income_tax`
	annualizes the period's gross, walks the brackets against that annual figure,
	and divides the annual tax back down — so a bracket table it reads has to be
	stated in annual dollars. Nothing else in this module de-annualizes anything.

	Until v0.91.0 this function divided each bracket by every entry in
	PERIODS_PER_YEAR and emitted a row per frequency, which was wrong twice over.
	State Tax Table has no `payroll_period` column — Federal Tax Table does, and
	that is the difference the code had borrowed without checking — so the six
	frequencies all landed under the same (state, tax_year, filing_status) key and
	`_load_state_table` read back 24 overlapping rows for one filing status. And
	even a table that could tell them apart would have been storing per-period
	floors for an engine that only ever asks in annual dollars.

	The `tax_year` argument is what makes a new year a data change rather than a
	code change: the seeder ships 2025, and January's rates arrive through
	`import_state_tax_table` against a year this function never has to know about.
	"""
	rows = []
	for filing_status, annual in OR_ANNUAL_BRACKETS.items():
		for bracket in annual:
			rows.append(
				{
					"state": "OR",
					"tax_year": int(tax_year),
					"filing_status": filing_status,
					"bracket_floor": float(bracket["bracket_floor"]),
					"bracket_ceiling": (
						float(bracket["bracket_ceiling"]) if bracket["bracket_ceiling"] is not None else None
					),
					"base_tax": float(bracket["base_tax"]),
					"marginal_rate": float(bracket["marginal_rate"]),
				}
			)
	return rows

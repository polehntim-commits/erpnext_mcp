# SPDX-License-Identifier: MIT
"""Tax form generators — PURE FUNCTIONS.

No database reads, no side effects, fully testable with deterministic inputs.
Same contract as `payroll_calc.py` and `withholding.py`: everything a form needs
arrives as an argument, so a form can be recomputed from a fixture and checked
against a number somebody worked out on paper.

v0.34.0. The payroll engine (v0.30.0) computes what each worker earned and what
was held back. These turn a year or a quarter of that arithmetic into the six
forms an agricultural employer in Oregon and Washington actually files:

  W-2       per employee per calendar year        (IRS)
  1099-NEC  per contractor per calendar year      (IRS)
  941       per company per quarter               (IRS)
  OR-WR     per company per year                  (Oregon DOR)
  OQ        per company per quarter               (Oregon DOR / OED)
  WA-ESD    per company per quarter               (WA Employment Security)

WHAT A SLIP LOOKS LIKE GOING IN. Every generator here takes a list of *slip
dicts* — the shape `payroll_calc.calculate_full_payroll` returns and the shape
a Farm Payroll Slip child row stores, plus the two period dates off its parent
entry:

    {
      "employee": "HR-EMP-00001",      "employee_name": "A Worker",
      "work_state": "OR",              "total_hours": 80.0,
      "gross_pay": 1600.00,
      "federal_withholding": 142.00,   "state_withholding": 96.40,
      "social_security": 99.20,        "medicare": 23.20,
      "additional_medicare": 0.0,      # optional, see below
      "period_start": "2025-01-01",    "period_end": "2025-01-14",
      "state_taxes_detail": {"OR": {...}},   # what the state engine returned
      "state_wages": {"OR": 1600.00},        # optional, see below
    }

Only `gross_pay` is truly required; every other key falls back to zero or to a
sensible default, because a slip that predates a field should produce a form
with a hole in it rather than a traceback.

THREE PLACES THIS ASKS THE CALLER FOR SOMETHING IT CANNOT WORK OUT ITSELF, and
each one is a real limit rather than an unfinished edge:

  * **Which state a dollar was earned in.** A slip carries one `work_state` —
    the state the worker spent the most hours in that period — but a
    cross-state period allocates gross between the two. `state_wages` on the
    slip is that allocation when the caller has it; without it the whole gross
    lands on `work_state`, and the form says so in `warnings`.

  * **Year-to-date wages before the first slip in hand.** The Social Security
    wage base and Washington's UI taxable wage base are annual caps, and a
    quarterly form cannot see the quarters before it. `ytd_wages_by_employee`
    in the company info carries prior-period wages per employee; absent, it is
    treated as zero, which is exactly right for Q1 and understates the cap
    consumed in every quarter after it. The form says which it was.

  * **Additional Medicare, separately.** A slip's `medicare` is the ordinary
    1.45% and the 0.9% surcharge added together, because that is what comes out
    of a paycheck. Form 941 line 5d needs the surcharge on its own, so slips may
    carry `additional_medicare`; where none do, line 5d is zero and the form
    says the surcharge was not separable.

WHAT NONE OF THESE DO. Produce a filing. Every generator returns computed box
and line values — the arithmetic, with the inputs that made it — and nothing
here signs, transmits, or renders an official red-ink Copy A. A person reads
these numbers onto the real form or into the state's portal. The `warnings`
list on every result is the part to read first: it is where a form says which
of its numbers is a floor rather than a figure.
"""

from __future__ import annotations

import calendar
from datetime import date

#: The six forms this module knows how to compute, and what each is scoped to.
#: `period` is "year" or "quarter"; `scope` is "employee" or "company".
FORM_TYPES = {
	"W-2": {"period": "year", "scope": "employee", "label": "Wage and Tax Statement"},
	"1099-NEC": {"period": "year", "scope": "employee", "label": "Nonemployee Compensation"},
	"941": {"period": "quarter", "scope": "company", "label": "Employer's Quarterly Federal Tax Return"},
	"OR-WR": {"period": "year", "scope": "company", "label": "Oregon Annual Withholding Tax Reconciliation"},
	"OQ": {"period": "quarter", "scope": "company", "label": "Oregon Quarterly Tax Report"},
	"WA-ESD": {"period": "quarter", "scope": "company", "label": "Washington ESD Quarterly Report"},
	# v0.259.0. The two that completed the payroll filing set.
	"940": {"period": "year", "scope": "company", "label": "Employer's Annual Federal Unemployment (FUTA) Tax Return"},
	"W-3": {"period": "year", "scope": "company", "label": "Transmittal of Wage and Tax Statements"},
}

#: W-3 box b. 943 is the agricultural employer's annual return (farmworkers' FICA and withholding).
KINDS_OF_PAYER = ("941", "943", "944", "Military", "CT-1", "Hshld. emp.", "Medicare govt. emp.")

QUARTERS = ("Q1", "Q2", "Q3", "Q4")

#: Calendar months in each quarter, in order. Used for the per-month employee
#: counts both OQ and the WA ESD report ask for.
QUARTER_MONTHS = {
	"Q1": (1, 2, 3),
	"Q2": (4, 5, 6),
	"Q3": (7, 8, 9),
	"Q4": (10, 11, 12),
}

#: Filing deadlines. Q4 is due in *January of the following year* — the one
#: entry in this table that is not the month after its own quarter, and the one
#: a hand-written due date gets wrong.
QUARTER_DUE = {
	"Q1": (4, 30, 0),
	"Q2": (7, 31, 0),
	"Q3": (10, 31, 0),
	"Q4": (1, 31, 1),
}

#: 2025 federal rates. Employer and employee halves together, because that is
#: what Form 941 line 5 is computed at.
SS_COMBINED_RATE = 0.124
MEDICARE_COMBINED_RATE = 0.029
ADDITIONAL_MEDICARE_RATE = 0.009
SS_EMPLOYEE_RATE = 0.062
MEDICARE_EMPLOYEE_RATE = 0.0145

#: 2025 Social Security wage base (IRS). Overridable through company_info.
SS_WAGE_BASE = 176100.0

#: 2025 Washington unemployment-insurance taxable wage base (ESD). Overridable.
WA_UI_WAGE_BASE = 72800.0

#: Which key in a state engine's result is *income tax* — the only state number
#: that belongs in W-2 box 17. Washington has no income tax and never will have
#: an entry here, which is why the mapping exists rather than a string format.
STATE_INCOME_TAX_KEY = {"OR": "or_income_tax", "WA": None}

#: W-2 box 14 is free-text "other", and it is where the state programs that are
#: withheld but are not income tax belong. The codes are the ones Oregon and
#: Washington ask payroll providers to use.
BOX14_ITEMS = (
	("OR", "or_transit_tax", "ORSTT W/H", "Oregon Statewide Transit Tax withheld"),
	("OR", "or_paid_leave_employee", "ORPFML", "Paid Leave Oregon employee contribution"),
	("WA", "wa_pfml_employee", "WAPFML", "WA Paid Family & Medical Leave employee premium"),
	("WA", "wa_cares_employee", "WACARES", "WA Cares Fund employee premium"),
	("WA", "wa_li_employee", "WALI", "WA Labor & Industries employee portion"),
)


# ── Period helpers ────────────────────────────────────────────────────────


def quarter_period(quarter: str, year: int) -> tuple[str, str]:
	"""The first and last day of a quarter as ISO date strings."""
	months = QUARTER_MONTHS.get(quarter)
	if not months:
		raise ValueError(f"quarter must be one of {', '.join(QUARTERS)}, got {quarter!r}.")
	first, last = months[0], months[-1]
	end_day = calendar.monthrange(int(year), last)[1]
	return (
		date(int(year), first, 1).isoformat(),
		date(int(year), last, end_day).isoformat(),
	)


def year_period(year: int) -> tuple[str, str]:
	"""January 1 to December 31 of a calendar year, as ISO date strings."""
	return date(int(year), 1, 1).isoformat(), date(int(year), 12, 31).isoformat()


def quarter_due_date(quarter: str, year: int) -> str:
	"""When a quarterly return is due. Q4's deadline is in the next year."""
	entry = QUARTER_DUE.get(quarter)
	if not entry:
		raise ValueError(f"quarter must be one of {', '.join(QUARTERS)}, got {quarter!r}.")
	month, day, year_offset = entry
	return date(int(year) + year_offset, month, day).isoformat()


def quarter_of_date(value) -> str | None:
	"""Which quarter an ISO date (or date object) falls in, or None if unreadable."""
	month = _month_of(value)
	if month is None:
		return None
	for quarter, months in QUARTER_MONTHS.items():
		if month in months:
			return quarter
	return None


# ── W-2 ───────────────────────────────────────────────────────────────────


def generate_w2_data(
	employee_info: dict,
	payroll_slips: list[dict],
	company_info: dict,
	year: int,
) -> dict:
	"""Compute all W-2 box values for one employee for one calendar year.

	Args:
		employee_info: `employee`, `employee_name`, `ssn_last4`, `address`.
		payroll_slips: Every slip for this employee with a period ending in
			`year`. See the module docstring for the shape.
		company_info: `name`, `ein`, `address`, optional `ss_wage_base` and
			`state_ids` ({"OR": "12345678-9"}).
		year: The calendar year the form covers.

	Returns:
		Dict with box1 through box20, the employer and employee blocks, and a
		`warnings` list naming anything that had to be assumed.
	"""
	warnings: list[str] = []
	slips = list(payroll_slips or [])

	wages = _sum(slips, "gross_pay")
	federal_withheld = _sum(slips, "federal_withholding")
	ss_withheld = _sum(slips, "social_security")
	medicare_withheld = _sum(slips, "medicare")

	ss_wage_base = float(company_info.get("ss_wage_base") or SS_WAGE_BASE)
	ss_wages = min(wages, ss_wage_base)
	if wages > ss_wage_base:
		warnings.append(
			f"box 3 is capped at the {year} Social Security wage base of "
			f"${ss_wage_base:,.2f}; box 1 wages of ${wages:,.2f} exceed it."
		)

	# Box 5 has no ceiling — Medicare applies to every dollar.
	medicare_wages = wages

	state_wages, split_assumed = _state_wage_split(slips)
	if split_assumed:
		warnings.append(
			"one or more slips carried no state_wages allocation, so their whole "
			"gross was attributed to the slip's work_state. A cross-state pay "
			"period split by hours will not be reflected in boxes 16 and 17."
		)

	state_ids = company_info.get("state_ids") or {}
	state_boxes = []
	for state in sorted(state_wages):
		income_tax = _state_income_tax(slips, state)
		state_boxes.append(
			{
				"box15_state": state,
				"box15_employer_state_id": str(state_ids.get(state) or ""),
				"box16_state_wages": _money(state_wages[state]),
				"box17_state_income_tax": _money(income_tax),
				"box18_local_wages": 0.0,
				"box19_local_income_tax": 0.0,
				"box20_locality_name": "",
			}
		)
		if not state_ids.get(state):
			warnings.append(f"no employer state ID recorded for {state}; box 15 will print blank.")

	box14 = _box14_items(slips)

	# The reconciliation an employer's own review does first: withholding at the
	# statutory rate against what actually came out. A cent or two is rounding
	# across many pay periods; a dollar is a slip somebody should look at.
	expected_ss = _money(ss_wages * SS_EMPLOYEE_RATE)
	if abs(expected_ss - ss_withheld) > 1.0:
		warnings.append(
			f"box 4 (${ss_withheld:,.2f}) differs from 6.2% of box 3 "
			f"(${expected_ss:,.2f}) by more than a dollar."
		)
	expected_medicare = _money(medicare_wages * MEDICARE_EMPLOYEE_RATE)
	if abs(expected_medicare - medicare_withheld) > 1.0:
		warnings.append(
			f"box 6 (${medicare_withheld:,.2f}) differs from 1.45% of box 5 "
			f"(${expected_medicare:,.2f}) by more than a dollar. Additional "
			f"Medicare withholding on wages over the threshold is included in "
			f"box 6 and explains a difference in that direction."
		)

	if not slips:
		warnings.append(f"no payroll slips found for {year}; every box is zero.")

	period_start, period_end = year_period(year)

	return {
		"form_type": "W-2",
		"tax_year": int(year),
		"period_start": period_start,
		"period_end": period_end,
		"employer": _employer_block(company_info),
		"employee": {
			"employee": employee_info.get("employee", ""),
			"name": employee_info.get("employee_name", ""),
			"ssn_display": _ssn_display(employee_info.get("ssn_last4")),
			"address": employee_info.get("address", "") or "",
		},
		"box1_wages": _money(wages),
		"box2_federal_income_tax_withheld": _money(federal_withheld),
		"box3_social_security_wages": _money(ss_wages),
		"box4_social_security_tax_withheld": _money(ss_withheld),
		"box5_medicare_wages": _money(medicare_wages),
		"box6_medicare_tax_withheld": _money(medicare_withheld),
		"box7_social_security_tips": 0.0,
		"box8_allocated_tips": 0.0,
		"box10_dependent_care_benefits": 0.0,
		"box11_nonqualified_plans": 0.0,
		"box12_codes": [],
		"box13": {
			"statutory_employee": False,
			"retirement_plan": False,
			"third_party_sick_pay": False,
		},
		"box14_other": box14,
		"state_boxes": state_boxes,
		"slip_count": len(slips),
		"total_hours": _money(_sum(slips, "total_hours")),
		"warnings": warnings,
	}


# ── 1099-NEC ──────────────────────────────────────────────────────────────


def generate_1099_nec_data(
	contractor_info: dict,
	payments: list[dict],
	company_info: dict,
	year: int,
) -> dict:
	"""Compute 1099-NEC values for one contractor for one calendar year.

	Args:
		contractor_info: `party`, `party_name`, `party_type`, `tin_last4`,
			`address`.
		payments: `[{amount, date, federal_withholding, state, state_withholding,
			reference}, …]`. Only `amount` is required.
		company_info: `name`, `ein`, `address`, optional `state_ids` and
			`nec_threshold` (default 600).
		year: The calendar year the form covers.

	Returns:
		Dict with box 1 through box 7 and a `reportable` flag against the
		threshold. The threshold decides whether a form has to be *issued*; the
		numbers are computed either way, because an employer deciding not to
		file still wants to see what the total was.
	"""
	warnings: list[str] = []
	rows = list(payments or [])

	total = _sum(rows, "amount")
	backup_withholding = _sum(rows, "federal_withholding")

	by_state: dict[str, dict] = {}
	for row in rows:
		state = str(row.get("state") or "").strip()
		if not state:
			continue
		entry = by_state.setdefault(state, {"income": 0.0, "withheld": 0.0})
		entry["income"] += _float(row.get("amount"))
		entry["withheld"] += _float(row.get("state_withholding"))

	state_ids = company_info.get("state_ids") or {}
	state_boxes = [
		{
			"box5_state_tax_withheld": _money(entry["withheld"]),
			"box6_state": state,
			"box6_payer_state_number": str(state_ids.get(state) or ""),
			"box7_state_income": _money(entry["income"]),
		}
		for state, entry in sorted(by_state.items())
	]

	threshold = float(company_info.get("nec_threshold") or 600.0)
	reportable = total >= threshold or backup_withholding > 0

	if not reportable:
		warnings.append(
			f"total nonemployee compensation of ${total:,.2f} is under the "
			f"${threshold:,.2f} reporting threshold and no backup withholding was "
			f"taken, so no 1099-NEC is required. The figures are computed anyway."
		)
	if not contractor_info.get("tin_last4"):
		warnings.append(
			"no taxpayer identification number on file for this recipient — the "
			"TIN comes off the signed W-9 and is completed by hand."
		)
	if not rows:
		warnings.append(f"no payments found for {year}; every box is zero.")

	period_start, period_end = year_period(year)

	return {
		"form_type": "1099-NEC",
		"tax_year": int(year),
		"period_start": period_start,
		"period_end": period_end,
		"payer": _employer_block(company_info),
		"recipient": {
			"party": contractor_info.get("party", ""),
			"name": contractor_info.get("party_name", ""),
			"party_type": contractor_info.get("party_type", ""),
			"tin_display": _tin_display(contractor_info.get("tin_last4")),
			"address": contractor_info.get("address", "") or "",
		},
		"box1_nonemployee_compensation": _money(total),
		"box2_direct_sales_indicator": False,
		"box4_federal_income_tax_withheld": _money(backup_withholding),
		"state_boxes": state_boxes,
		"payment_count": len(rows),
		"reporting_threshold": _money(threshold),
		"reportable": bool(reportable),
		"warnings": warnings,
	}


# ── Form 941 ──────────────────────────────────────────────────────────────


def generate_941_data(
	all_slips_for_quarter: list[dict],
	company_info: dict,
	quarter: str,
	year: int,
) -> dict:
	"""Compute Form 941 lines 1 through 15 for one company for one quarter.

	Args:
		all_slips_for_quarter: Every slip for every employee whose pay period
			ends inside the quarter.
		company_info: `name`, `ein`, `address`, optional `ss_wage_base`,
			`ytd_wages_by_employee`, `deposits` and the three adjustment lines
			`sick_pay_adjustment`, `tips_and_group_term_life_adjustment`,
			`small_business_payroll_tax_credit`.
		quarter: Q1, Q2, Q3 or Q4.
		year: The calendar year.

	Returns:
		Dict with `line1` through `line15`, the deposit reconciliation, and
		`warnings`.
	"""
	warnings: list[str] = []
	slips = list(all_slips_for_quarter or [])
	period_start, period_end = quarter_period(quarter, year)

	wages = _sum(slips, "gross_pay")
	federal_withheld = _sum(slips, "federal_withholding")
	ss_withheld_employee = _sum(slips, "social_security")
	medicare_withheld_employee = _sum(slips, "medicare")
	additional_medicare = _sum(slips, "additional_medicare")

	# Line 1 counts the employees who were paid in the pay period including the
	# 12th of the quarter's *last* month. Counting distinct employees paid in
	# the quarter is the near-enough answer a payroll register can give, and it
	# is the one an employer with a steady crew would write down anyway.
	employees = {s.get("employee") for s in slips if s.get("employee")}
	line1 = len(employees)

	# Social Security is capped per employee per year, so the cap has to be
	# consumed employee by employee, not against the quarter's grand total.
	ss_wage_base = float(company_info.get("ss_wage_base") or SS_WAGE_BASE)
	ytd = company_info.get("ytd_wages_by_employee") or {}
	ss_wages, capped_employees = _capped_wages_by_employee(slips, ss_wage_base, ytd)
	if capped_employees:
		warnings.append(
			f"{len(capped_employees)} employee(s) reached the ${ss_wage_base:,.2f} "
			f"Social Security wage base inside this quarter: "
			f"{', '.join(sorted(capped_employees))}."
		)
	if not ytd:
		warnings.append(
			"no year-to-date wages supplied, so the Social Security wage base was "
			"applied to this quarter's wages alone. Correct for Q1; for a later "
			"quarter it overstates line 5a for anybody who had already reached the "
			"base."
		)

	medicare_wages = wages
	line5a_wages = _money(ss_wages)
	line5a_tax = _money(ss_wages * SS_COMBINED_RATE)
	line5c_wages = _money(medicare_wages)
	line5c_tax = _money(medicare_wages * MEDICARE_COMBINED_RATE)

	# 5d wants the wages the 0.9% surcharge was taken on. Slips carry the amount
	# withheld, not the wage it was taken on, so it is divided back out.
	line5d_wages = _money(additional_medicare / ADDITIONAL_MEDICARE_RATE) if additional_medicare else 0.0
	line5d_tax = _money(additional_medicare)
	if not additional_medicare and wages > 0:
		warnings.append(
			"no slip carried an `additional_medicare` amount, so line 5d is zero. "
			"A slip's `medicare` field is the ordinary 1.45% and the 0.9% surcharge "
			"combined; where the two were not stored apart, the surcharge cannot be "
			"reported on its own line."
		)

	line5e = _money(line5a_tax + line5c_tax + line5d_tax)
	line5f = _money(company_info.get("section_3121q_notice"))
	line6 = _money(federal_withheld + line5e + line5f)

	# Line 7 is the fractions-of-cents adjustment, and it is exactly this: what
	# was actually withheld and matched, against what the form's own rates
	# produce on the quarter's totals.
	actual_fica = _money(
		ss_withheld_employee * 2
		+ (medicare_withheld_employee - additional_medicare) * 2
		+ additional_medicare
	)
	line7 = _money(actual_fica - line5e)

	line8 = _money(company_info.get("sick_pay_adjustment"))
	line9 = _money(company_info.get("tips_and_group_term_life_adjustment"))
	line10 = _money(line6 + line7 + line8 + line9)
	line11 = _money(company_info.get("small_business_payroll_tax_credit"))
	line12 = _money(line10 - line11)
	line13 = _money(company_info.get("deposits"))
	balance = _money(line12 - line13)
	line14 = _money(balance) if balance > 0 else 0.0
	line15 = _money(-balance) if balance < 0 else 0.0

	if not company_info.get("deposits"):
		warnings.append(
			"no deposit total supplied, so line 13 is zero and the whole liability "
			"shows as a balance due on line 14. Deposits are made through EFTPS and "
			"this app does not see them."
		)
	if not company_info.get("ein"):
		warnings.append("no EIN recorded on the company — a 941 cannot be filed without one.")
	if not slips:
		warnings.append(f"no payroll slips found for {quarter} {year}; every line is zero.")

	return {
		"form_type": "941",
		"tax_year": int(year),
		"quarter": quarter,
		"period_start": period_start,
		"period_end": period_end,
		"due_date": quarter_due_date(quarter, year),
		"employer": _employer_block(company_info),
		"line1_number_of_employees": line1,
		"line2_wages_tips_other_compensation": _money(wages),
		"line3_federal_income_tax_withheld": _money(federal_withheld),
		"line4_no_wages_subject_to_ss_medicare": wages <= 0,
		"line5a_taxable_social_security_wages": line5a_wages,
		"line5a_tax": line5a_tax,
		"line5b_taxable_social_security_tips": 0.0,
		"line5b_tax": 0.0,
		"line5c_taxable_medicare_wages": line5c_wages,
		"line5c_tax": line5c_tax,
		"line5d_wages_subject_to_additional_medicare": line5d_wages,
		"line5d_tax": line5d_tax,
		"line5e_total_social_security_and_medicare": line5e,
		"line5f_section_3121q_notice_and_demand": line5f,
		"line6_total_taxes_before_adjustments": line6,
		"line7_fractions_of_cents_adjustment": line7,
		"line8_sick_pay_adjustment": line8,
		"line9_tips_and_group_term_life_adjustment": line9,
		"line10_total_taxes_after_adjustments": line10,
		"line11_small_business_payroll_tax_credit": line11,
		"line12_total_taxes_after_credits": line12,
		"line13_total_deposits": line13,
		"line14_balance_due": line14,
		"line15_overpayment": line15,
		"slip_count": len(slips),
		"employee_count": line1,
		"warnings": warnings,
	}


# ── Oregon Form OR-WR ─────────────────────────────────────────────────────


def generate_or_wr_data(or_slips: list[dict], company_info: dict, year: int) -> dict:
	"""Oregon's annual withholding reconciliation for one company.

	OR-WR is a reconciliation and nothing else: the four quarterly OQ reports on
	one side, the annual W-2 totals on the other, and a line for the difference.
	So this buckets the year's Oregon slips by quarter *and* totals them, and
	says whether the two agree.

	Args:
		or_slips: Every slip with Oregon wages whose period ends in `year`.
		company_info: `name`, `ein`, `address`, optional `state_ids`, and
			optional `oq_reported` ({"Q1": {"income_tax": …, "transit_tax": …,
			"paid_leave": …}}) — what was actually filed on each OQ.
		year: The calendar year.

	Returns:
		Dict with per-quarter and annual Oregon totals and a `reconciles` flag.
	"""
	warnings: list[str] = []
	slips = [s for s in (or_slips or []) if _has_state(s, "OR")]

	by_quarter = {q: _or_totals([s for s in slips if quarter_of_date(_period_end(s)) == q]) for q in QUARTERS}
	annual = _or_totals(slips)

	undated = [s for s in slips if quarter_of_date(_period_end(s)) is None]
	if undated:
		warnings.append(
			f"{len(undated)} slip(s) carried no readable period_end and could not be "
			f"placed in a quarter. They are in the annual totals and in no quarter, "
			f"which is why the quarterly column will not add up."
		)

	quarters_sum = {
		key: _money(sum(by_quarter[q][key] for q in QUARTERS))
		for key in (
			"or_wages",
			"or_income_tax",
			"or_transit_tax",
			"or_paid_leave_employee",
			"or_paid_leave_employer",
		)
	}

	reported = company_info.get("oq_reported") or {}
	differences = {}
	for key, annual_value in (
		("or_income_tax", annual["or_income_tax"]),
		("or_transit_tax", annual["or_transit_tax"]),
		("or_paid_leave_employee", annual["or_paid_leave_employee"]),
	):
		filed = _money(sum(_float((reported.get(q) or {}).get(key)) for q in QUARTERS))
		differences[key] = {
			"annual_total": _money(annual_value),
			"quarterly_filed": filed,
			"difference": _money(annual_value - filed),
		}

	has_reported = bool(reported)
	reconciles = all(abs(d["difference"]) < 0.01 for d in differences.values()) if has_reported else None
	if not has_reported:
		warnings.append(
			"no `oq_reported` figures supplied, so this is the computed annual total "
			"with nothing to reconcile against. OR-WR's whole purpose is comparing "
			"the four filed OQ reports to the year — supply what was filed to get an "
			"answer rather than a total."
		)
	elif reconciles is False:
		warnings.append(
			"the computed annual totals do not match the OQ figures supplied. See "
			"`reconciliation` for which line differs and by how much — an OR-WR filed "
			"on an unexplained difference is an assessment letter."
		)

	period_start, period_end = year_period(year)
	state_ids = company_info.get("state_ids") or {}
	if not state_ids.get("OR"):
		warnings.append("no Oregon business identification number recorded on the company.")
	if not slips:
		warnings.append(f"no Oregon payroll slips found for {year}; every total is zero.")

	return {
		"form_type": "OR-WR",
		"tax_year": int(year),
		"period_start": period_start,
		"period_end": period_end,
		"due_date": date(int(year) + 1, 1, 31).isoformat(),
		"employer": _employer_block(company_info),
		"oregon_bin": str(state_ids.get("OR") or ""),
		"annual": {k: _money(v) for k, v in annual.items()},
		"by_quarter": {q: {k: _money(v) for k, v in by_quarter[q].items()} for q in QUARTERS},
		"quarters_sum": quarters_sum,
		"reconciliation": differences,
		"reconciles": reconciles,
		"employee_count": len({s.get("employee") for s in slips if s.get("employee")}),
		"slip_count": len(slips),
		"warnings": warnings,
	}


# ── Oregon Form OQ ────────────────────────────────────────────────────────


def generate_or_oq_data(
	or_slips_for_quarter: list[dict],
	company_info: dict,
	quarter: str,
	year: int,
) -> dict:
	"""Oregon's combined quarterly tax report for one company.

	One form covering four programs: withholding, unemployment insurance, the
	Statewide Transit Tax and Paid Leave Oregon. The per-month employee counts
	are asked for by name on the form and are computed from the slips' period
	end dates.

	Args:
		or_slips_for_quarter: Every slip with Oregon wages whose period ends in
			the quarter.
		company_info: `name`, `ein`, `address`, optional `state_ids`,
			`ui_rate` (percent) and `or_ui_wage_base`.
		quarter: Q1, Q2, Q3 or Q4.
		year: The calendar year.

	Returns:
		Dict with subject wages, the four programs' amounts, per-month employee
		counts and `warnings`.
	"""
	warnings: list[str] = []
	slips = [s for s in (or_slips_for_quarter or []) if _has_state(s, "OR")]
	period_start, period_end = quarter_period(quarter, year)

	totals = _or_totals(slips)
	subject_wages = totals["or_wages"]

	monthly_counts = {}
	for month in QUARTER_MONTHS[quarter]:
		paid = {s.get("employee") for s in slips if s.get("employee") and _month_of(_period_end(s)) == month}
		monthly_counts[calendar.month_name[month]] = len(paid)

	ui_rate = float(company_info.get("ui_rate") or 0.0) / 100.0
	ui_base = float(company_info.get("or_ui_wage_base") or 0.0)
	ui_subject, ui_capped = (
		_capped_wages_by_employee(slips, ui_base, company_info.get("ytd_wages_by_employee") or {}, state="OR")
		if ui_base > 0
		else (subject_wages, set())
	)
	ui_tax = _money(ui_subject * ui_rate)
	if ui_rate <= 0:
		warnings.append(
			"no unemployment-insurance rate supplied, so the UI tax is zero. Oregon "
			"assigns each employer its own rate each year and this app does not know it."
		)
	if ui_base <= 0:
		warnings.append(
			"no Oregon UI taxable wage base supplied, so UI subject wages are the "
			"quarter's whole Oregon gross with no per-employee cap applied."
		)
	elif ui_capped:
		warnings.append(
			f"{len(ui_capped)} employee(s) reached the ${ui_base:,.2f} Oregon UI "
			f"wage base inside this quarter."
		)

	state_ids = company_info.get("state_ids") or {}
	if not state_ids.get("OR"):
		warnings.append("no Oregon business identification number recorded on the company.")
	if not slips:
		warnings.append(f"no Oregon payroll slips found for {quarter} {year}; every total is zero.")

	return {
		"form_type": "OQ",
		"tax_year": int(year),
		"quarter": quarter,
		"period_start": period_start,
		"period_end": period_end,
		"due_date": quarter_due_date(quarter, year),
		"employer": _employer_block(company_info),
		"oregon_bin": str(state_ids.get("OR") or ""),
		"subject_wages": _money(subject_wages),
		"state_withholding": _money(totals["or_income_tax"]),
		"statewide_transit_tax": _money(totals["or_transit_tax"]),
		"transit_tax_subject_wages": _money(subject_wages),
		"paid_leave_employee": _money(totals["or_paid_leave_employee"]),
		"paid_leave_employer": _money(totals["or_paid_leave_employer"]),
		"paid_leave_total": _money(totals["or_paid_leave_employee"] + totals["or_paid_leave_employer"]),
		"workers_comp": _money(totals["or_workers_comp"]),
		"ui_subject_wages": _money(ui_subject),
		"ui_rate": _money(ui_rate * 100),
		"ui_tax": ui_tax,
		"total_due": _money(
			totals["or_income_tax"]
			+ totals["or_transit_tax"]
			+ totals["or_paid_leave_employee"]
			+ totals["or_paid_leave_employer"]
			+ ui_tax
		),
		"monthly_employee_counts": monthly_counts,
		"employee_count": len({s.get("employee") for s in slips if s.get("employee")}),
		"total_hours": _money(_sum(slips, "total_hours")),
		"slip_count": len(slips),
		"warnings": warnings,
	}


# ── Washington ESD quarterly report ───────────────────────────────────────


def generate_wa_esd_data(
	wa_slips_for_quarter: list[dict],
	company_info: dict,
	quarter: str,
	year: int,
) -> dict:
	"""Washington's Employment Security Department quarterly report.

	Washington has no income tax, so this report is wages, HOURS and three
	programs: unemployment insurance, Paid Family & Medical Leave and WA Cares.

	Hours are not incidental here. Washington's ESD report is one of the few
	filings in the country that requires hours worked per employee — benefit
	eligibility is an hours test, so a report filed with wages and no hours is
	incomplete rather than approximate.

	Args:
		wa_slips_for_quarter: Every slip with Washington wages whose period ends
			in the quarter.
		company_info: `name`, `ein`, `address`, optional `state_ids`,
			`ui_rate` (percent), `wa_ui_wage_base`, `ytd_wages_by_employee` and
			`ssn_last4_by_employee`.
		quarter: Q1, Q2, Q3 or Q4.
		year: The calendar year.

	Returns:
		Dict with the per-employee wage-and-hour detail ESD asks for, the three
		programs' totals, and `warnings`.
	"""
	warnings: list[str] = []
	slips = [s for s in (wa_slips_for_quarter or []) if _has_state(s, "WA")]
	period_start, period_end = quarter_period(quarter, year)

	ssn_map = company_info.get("ssn_last4_by_employee") or {}
	ytd = company_info.get("ytd_wages_by_employee") or {}
	ui_base = float(company_info.get("wa_ui_wage_base") or WA_UI_WAGE_BASE)
	ui_rate = float(company_info.get("ui_rate") or 0.0) / 100.0

	per_employee: dict[str, dict] = {}
	for slip in slips:
		employee = slip.get("employee") or ""
		if not employee:
			continue
		wages = _state_wages_of(slip, "WA")
		row = per_employee.setdefault(
			employee,
			{
				"employee": employee,
				"employee_name": slip.get("employee_name") or "",
				"ssn_display": _ssn_display(ssn_map.get(employee)),
				"wages": 0.0,
				"hours": 0.0,
			},
		)
		row["wages"] += wages
		row["hours"] += _float(slip.get("total_hours"))

	capped = set()
	for employee, row in per_employee.items():
		prior = _float(ytd.get(employee))
		remaining = max(ui_base - prior, 0.0)
		taxable = min(row["wages"], remaining)
		if taxable < row["wages"]:
			capped.add(employee)
		row["taxable_wages"] = _money(taxable)
		row["excess_wages"] = _money(row["wages"] - taxable)
		row["wages"] = _money(row["wages"])
		# ESD wants whole hours, rounded down — an hour is either worked or it
		# is not, and rounding up manufactures eligibility.
		row["hours"] = int(row["hours"])

	rows = [per_employee[k] for k in sorted(per_employee)]

	total_wages = _money(sum(r["wages"] for r in rows))
	total_taxable = _money(sum(r["taxable_wages"] for r in rows))
	total_hours = sum(r["hours"] for r in rows)

	pfml_employee = _money(_sum_state_key(slips, "WA", "wa_pfml_employee"))
	pfml_employer = _money(_sum_state_key(slips, "WA", "wa_pfml_employer"))
	cares_employee = _money(_sum_state_key(slips, "WA", "wa_cares_employee"))
	li_employee = _money(_sum_state_key(slips, "WA", "wa_li_employee"))
	li_employer = _money(_sum_state_key(slips, "WA", "wa_li_employer"))
	ui_tax = _money(total_taxable * ui_rate)

	if ui_rate <= 0:
		warnings.append(
			"no unemployment-insurance rate supplied, so the UI tax is zero. ESD "
			"assigns each employer its own rate each year and this app does not know it."
		)
	if capped:
		warnings.append(
			f"{len(capped)} employee(s) reached the ${ui_base:,.2f} Washington UI "
			f"taxable wage base inside this quarter; the balance is reported as "
			f"excess wages."
		)
	if not ytd:
		warnings.append(
			"no year-to-date wages supplied, so the UI wage base was applied to this "
			"quarter's wages alone. Correct for Q1; for a later quarter it overstates "
			"taxable wages for anybody who had already reached the base."
		)
	missing_hours = [r["employee"] for r in rows if r["hours"] <= 0 and r["wages"] > 0]
	if missing_hours:
		warnings.append(
			f"{len(missing_hours)} employee(s) have Washington wages and no hours. "
			f"ESD requires hours worked per employee and will reject the report: "
			f"{', '.join(sorted(missing_hours))}."
		)
	missing_ssn = [r["employee"] for r in rows if not ssn_map.get(r["employee"])]
	if missing_ssn:
		warnings.append(
			f"{len(missing_ssn)} employee(s) have no Social Security number on file; "
			f"the report cannot be filed until each one is completed by hand."
		)
	state_ids = company_info.get("state_ids") or {}
	if not state_ids.get("WA"):
		warnings.append("no Washington ESD account number recorded on the company.")
	if not slips:
		warnings.append(f"no Washington payroll slips found for {quarter} {year}; every total is zero.")

	return {
		"form_type": "WA-ESD",
		"tax_year": int(year),
		"quarter": quarter,
		"period_start": period_start,
		"period_end": period_end,
		"due_date": quarter_due_date(quarter, year),
		"employer": _employer_block(company_info),
		"esd_account_number": str(state_ids.get("WA") or ""),
		"employees": rows,
		"employee_count": len(rows),
		"total_wages": total_wages,
		"total_taxable_wages": total_taxable,
		"total_excess_wages": _money(total_wages - total_taxable),
		"total_hours": total_hours,
		"ui_rate": _money(ui_rate * 100),
		"ui_tax": ui_tax,
		"pfml_employee_premium": pfml_employee,
		"pfml_employer_premium": pfml_employer,
		"pfml_total_premium": _money(pfml_employee + pfml_employer),
		"wa_cares_employee_premium": cares_employee,
		"labor_and_industries_employee": li_employee,
		"labor_and_industries_employer": li_employer,
		"total_due": _money(ui_tax + pfml_employee + pfml_employer + cares_employee),
		"slip_count": len(slips),
		"warnings": warnings,
	}


# ── Dispatch ──────────────────────────────────────────────────────────────


def generate_form_data(
	form_type: str,
	slips: list[dict],
	company_info: dict,
	year: int,
	quarter: str | None = None,
	subject_info: dict | None = None,
	payments: list[dict] | None = None,
) -> dict:
	"""Route to the right generator. The one entry point a tool needs.

	Keeps the "which arguments does this form take" question in one place rather
	than in the tool layer, where it would be repeated once for generate and
	once for regenerate and would drift between the two.
	"""
	subject = subject_info or {}
	if form_type == "W-2":
		return generate_w2_data(subject, slips, company_info, year)
	if form_type == "1099-NEC":
		return generate_1099_nec_data(subject, payments or [], company_info, year)
	if form_type == "941":
		return generate_941_data(slips, company_info, quarter or "Q1", year)
	if form_type == "OR-WR":
		return generate_or_wr_data(slips, company_info, year)
	if form_type == "OQ":
		return generate_or_oq_data(slips, company_info, quarter or "Q1", year)
	if form_type == "WA-ESD":
		return generate_wa_esd_data(slips, company_info, quarter or "Q1", year)
	if form_type == "940":
		from .tax_remittance_calc import generate_940_data

		return generate_940_data(slips, company_info, year)
	if form_type == "W-3":
		return generate_w3_data(slips, company_info, year)
	raise ValueError(f"unknown form_type {form_type!r}. Known forms: {', '.join(sorted(FORM_TYPES))}.")


# ── W-3 ── v0.259.0 ───────────────────────────────────────────────────────


def generate_w3_data(slips: list[dict], company_info: dict, year: int) -> dict:
	"""The W-3 transmittal: every employee's W-2 for the year, totalled box by box.

	Each employee's W-2 is computed with `generate_w2_data` from the same slips, so the transmittal cannot
	disagree with the forms it transmits — the Social Security wage-base cap is applied per person, as on the
	W-2s, before the totals are taken. Box b (kind of payer) comes from `company_info["kind_of_payer"]`: 941 by
	default, and an agricultural employer that files Form 943 ticks 943.
	"""
	warnings: list[str] = []
	by_employee: dict[str, list[dict]] = {}
	for slip in slips or []:
		by_employee.setdefault(str(slip.get("employee") or ""), []).append(slip)
	w2s = [generate_w2_data({"employee": emp}, rows, company_info, year) for emp, rows in sorted(by_employee.items())]

	def total(key: str) -> float:
		return _money(sum(_float(w2.get(key)) for w2 in w2s))

	states: dict[str, dict] = {}
	for w2 in w2s:
		for box in w2.get("state_boxes") or []:
			state = states.setdefault(box["box15_state"], {"box15_state": box["box15_state"],
			                                                "box15_employer_state_id": box["box15_employer_state_id"],
			                                                "box16_state_wages": 0.0, "box17_state_income_tax": 0.0})
			state["box16_state_wages"] = _money(state["box16_state_wages"] + _float(box["box16_state_wages"]))
			state["box17_state_income_tax"] = _money(state["box17_state_income_tax"] + _float(box["box17_state_income_tax"]))
	kind = str(company_info.get("kind_of_payer") or "941")
	if kind not in KINDS_OF_PAYER:
		warnings.append(f"kind_of_payer {kind!r} is not a box b option; it is one of {', '.join(KINDS_OF_PAYER)}.")
	if kind == "941":
		warnings.append(
			"box b is 941. An employer of farmworkers who files Form 943 (the annual agricultural return) ticks 943 "
			"instead — pass kind_of_payer: 943 if that is how this company files.")
	flagged = [w2["employee"]["employee"] for w2 in w2s if w2.get("warnings")]
	if flagged:
		warnings.append(f"{len(flagged)} W-2(s) carry their own warnings ({', '.join(flagged[:6])}"
		                f"{'…' if len(flagged) > 6 else ''}); read those before the transmittal is filed.")
	if not w2s:
		warnings.append(f"no payroll slips found for {year}; every box is zero and there are no W-2s to transmit.")
	warnings.append(
		"boxes 1–6 must equal the year's Forms 941 (or the 943) added together; compare them before filing — a "
		"difference is the SSA/IRS mismatch letter a year later.")
	period_start, period_end = year_period(year)
	return {
		"form_type": "W-3",
		"tax_year": int(year),
		"period_start": period_start,
		"period_end": period_end,
		"employer": _employer_block(company_info),
		"box_b_kind_of_payer": kind,
		"box_b_kind_of_employer": str(company_info.get("kind_of_employer") or "None apply"),
		"box_c_total_w2_forms": len(w2s),
		"box1_wages": total("box1_wages"),
		"box2_federal_income_tax_withheld": total("box2_federal_income_tax_withheld"),
		"box3_social_security_wages": total("box3_social_security_wages"),
		"box4_social_security_tax_withheld": total("box4_social_security_tax_withheld"),
		"box5_medicare_wages": total("box5_medicare_wages"),
		"box6_medicare_tax_withheld": total("box6_medicare_tax_withheld"),
		"box7_social_security_tips": 0.0,
		"box8_allocated_tips": 0.0,
		"box14_income_tax_withheld_by_third_party_payer": 0.0,
		"state_boxes": [states[s] for s in sorted(states)],
		"employees": [w2["employee"]["employee"] for w2 in w2s],
		"slip_count": len(slips or []),
		"warnings": warnings,
	}


# ── Internal helpers ──────────────────────────────────────────────────────


def _money(value) -> float:
	return round(_float(value), 2)


def _float(value) -> float:
	if value is None or value == "":
		return 0.0
	try:
		return float(value)
	except (TypeError, ValueError):
		return 0.0


def _sum(rows: list[dict], key: str) -> float:
	return round(sum(_float(row.get(key)) for row in rows), 2)


def _period_end(slip: dict):
	return slip.get("period_end") or slip.get("pay_period_end")


def _month_of(value) -> int | None:
	"""The month of an ISO date string or a date/datetime, or None."""
	if value is None or value == "":
		return None
	month = getattr(value, "month", None)
	if month is not None:
		return int(month)
	text = str(value).strip()
	parts = text.split("-")
	if len(parts) < 2:
		return None
	try:
		month = int(parts[1][:2])
	except ValueError:
		return None
	return month if 1 <= month <= 12 else None


def _state_detail(slip: dict, state: str) -> dict:
	"""The state engine's result for one state on one slip, or an empty dict."""
	detail = slip.get("state_taxes_detail") or {}
	if not isinstance(detail, dict):
		return {}
	entry = detail.get(state)
	return entry if isinstance(entry, dict) else {}


def _has_state(slip: dict, state: str) -> bool:
	"""Whether a slip has any wages in a state, by allocation or by work_state."""
	explicit = slip.get("state_wages")
	if isinstance(explicit, dict) and explicit:
		return state in explicit
	return slip.get("work_state") == state


def _state_wages_of(slip: dict, state: str) -> float:
	"""The gross attributable to one state on one slip.

	Prefers the caller's explicit allocation; falls back to "all of it landed in
	`work_state`", which is right for the single-state slip and is the assumption
	every generator using this reports in `warnings`.
	"""
	explicit = slip.get("state_wages")
	if isinstance(explicit, dict) and explicit:
		return _float(explicit.get(state))
	return _float(slip.get("gross_pay")) if slip.get("work_state") == state else 0.0


def _state_wage_split(slips: list[dict]) -> tuple[dict[str, float], bool]:
	"""Gross by state across many slips, and whether anything had to be assumed.

	`assumed` is True only where a slip is genuinely ambiguous: it ran more than
	one state's tax engine — so the period crossed a border — and carried no
	`state_wages` allocation saying how the gross was divided. A slip with one
	state on it needs no allocation and does not raise the flag, because there
	is nothing to get wrong.
	"""
	totals: dict[str, float] = {}
	assumed = False
	for slip in slips:
		explicit = slip.get("state_wages")
		if isinstance(explicit, dict) and explicit:
			for state, amount in explicit.items():
				if state:
					totals[state] = totals.get(state, 0.0) + _float(amount)
			continue
		state = slip.get("work_state")
		if state:
			totals[state] = totals.get(state, 0.0) + _float(slip.get("gross_pay"))
		detail = slip.get("state_taxes_detail")
		if isinstance(detail, dict) and len(detail) > 1:
			assumed = True
	return {k: round(v, 2) for k, v in totals.items()}, assumed


def _state_income_tax(slips: list[dict], state: str) -> float:
	"""State *income* tax only — not the levies that belong in box 14."""
	key = STATE_INCOME_TAX_KEY.get(state)
	if not key:
		return 0.0
	return round(sum(_float(_state_detail(s, state).get(key)) for s in slips), 2)


def _sum_state_key(slips: list[dict], state: str, key: str) -> float:
	return round(sum(_float(_state_detail(s, state).get(key)) for s in slips), 2)


def _box14_items(slips: list[dict]) -> list[dict]:
	"""The state levies that are withheld but are not income tax."""
	items = []
	for state, key, code, label in BOX14_ITEMS:
		amount = _sum_state_key(slips, state, key)
		if amount:
			items.append({"code": code, "amount": amount, "description": label, "state": state})
	return items


def _or_totals(slips: list[dict]) -> dict:
	"""Every Oregon number OQ and OR-WR both need, from a list of slips."""
	return {
		"or_wages": round(sum(_state_wages_of(s, "OR") for s in slips), 2),
		"or_income_tax": _sum_state_key(slips, "OR", "or_income_tax"),
		"or_transit_tax": _sum_state_key(slips, "OR", "or_transit_tax"),
		"or_paid_leave_employee": _sum_state_key(slips, "OR", "or_paid_leave_employee"),
		"or_paid_leave_employer": _sum_state_key(slips, "OR", "or_paid_leave_employer"),
		"or_workers_comp": _sum_state_key(slips, "OR", "or_workers_comp"),
	}


def _capped_wages_by_employee(
	slips: list[dict],
	wage_base: float,
	ytd_wages: dict,
	state: str | None = None,
) -> tuple[float, set]:
	"""Apply an annual per-employee wage base to a period's wages.

	Returns the capped total and the set of employees who hit the ceiling. The
	cap is consumed employee by employee because that is how a wage base works —
	applying it to a company's grand total would let one high earner's headroom
	absorb everybody else's excess.
	"""
	by_employee: dict[str, float] = {}
	for slip in slips:
		employee = slip.get("employee") or ""
		amount = _state_wages_of(slip, state) if state else _float(slip.get("gross_pay"))
		by_employee[employee] = by_employee.get(employee, 0.0) + amount

	total = 0.0
	capped = set()
	for employee, wages in by_employee.items():
		remaining = max(wage_base - _float(ytd_wages.get(employee)), 0.0)
		taxable = min(wages, remaining)
		if taxable < wages:
			capped.add(employee or "(unnamed)")
		total += taxable
	return round(total, 2), capped


def _employer_block(company_info: dict) -> dict:
	return {
		"name": company_info.get("name", "") or "",
		"ein": str(company_info.get("ein") or ""),
		"ein_display": str(company_info.get("ein") or "") or "__________  (set the company's Tax ID)",
		"address": company_info.get("address", "") or "",
	}


def _ssn_display(last4) -> str:
	"""`XXX-XX-1234`, or a blank the person filing completes from the I-9.

	The full number is deliberately not stored on this site — see `Related Party`
	and `tools/tax.py`. Four digits is enough to tell two people apart on a
	worksheet and not enough to be worth stealing.
	"""
	digits = str(last4 or "").strip()[-4:]
	if not digits:
		return "XXX-XX-____  (no SSN on file)"
	return f"XXX-XX-{digits}"


def _tin_display(last4) -> str:
	digits = str(last4 or "").strip()[-4:]
	if not digits:
		return "__________  (no taxpayer id on file — obtain a signed W-9)"
	return f"XXX-XX-{digits}"

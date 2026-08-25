# SPDX-License-Identifier: MIT
"""Tax remittance reporting — what is owed, to whom, and by when.

v0.92.0. Five read-only tools over payroll that is already in the system. The
arithmetic is in `tax_remittance_calc.py` and `form_generators.py`, both pure;
this is the half that goes and gets the numbers.

WHY THIS IS SEPARATE FROM `taxforms.py`. That module GENERATES AND STORES a Tax
Form: a record of what an employer told an agency, on a date, kept verbatim so
that correcting payroll afterwards cannot quietly rewrite the W-2 that is
already in an envelope. These five store nothing and are recomputed on every
call, because they answer a different question — not "what did we file" but
"what do we owe right now, and when is it due". A deposit schedule showing last
week's payroll would be worse than useless. `taxforms.py` says in its own
docstring that computing a deposit schedule is a thing it does not do; this is
that thing.

  get_tax_remittance_summary   federal EFTPS + Oregon + Washington, by period
  get_941_prefill              the quarterly federal return, line by line
  get_state_tax_remittance     Oregon's OQ and Form 132; Washington's ESD report
  get_tax_deposit_schedule     every deposit deadline in a period, with the rule
  get_futa_summary             Form 940, the $7,000 cap consumed in date order

WHICH PAYROLL COUNTS is `taxforms._load_slips`' answer and not a second one:
Farm Payroll Entries in `Calculated` or `Submitted` status whose period ends
inside the window. A Draft payroll has not been paid and a Cancelled one was
not. Reusing that loader rather than writing another is deliberate — two
definitions of "paid" in one app is how a remittance summary and the return it
is reconciled against come to disagree by one pay period.

THE EMPLOYER HALVES ARE READ FROM THE SLIP, not doubled from the employee's.
A deposit is both halves of FICA together, and while the employer's Social
Security and Medicare are usually exactly the employee's, they are not when a
worker crosses the Additional Medicare threshold — the 0.9% surcharge is
employee-only and has no employer match. Every slip since v0.30.0 stores
`social_security_employer`, `medicare_employer`, `futa` and
`state_unemployment`; where a slip predates them the amounts are mirrored from
the employee side and `warnings` says so.

THE PAYDAY IS NOT RECORDED ANYWHERE, AND THIS IS THE SHARPEST LIMIT HERE. Every
federal deposit rule keys on the date wages were PAID (26 CFR 31.6302-1(c)), and
a Farm Payroll Entry has no such field — it carries `pay_period_start` and
`pay_period_end` and nothing else, and the slip child table carries no date at
all. Two things are done about it, in order of preference:

  * A run that has reached the ledger has a real date on it. `gl_postings`
    carries `posting_date` per Journal Entry, and where a run has one the latest
    is used as the payday.
  * Everything else falls back to the period end plus `payday_offset_days`,
    which the caller supplies as the farm's usual gap between a period closing
    and the money moving.

Without either, deadlines are computed from the period end and are therefore
EARLY by however long the farm actually takes to pay. Early is the safe
direction — an operator following them deposits too soon rather than too late —
but they are not the real dates, and every result that produces one carries a
`payday_basis` sentence saying which of the three cases it was.

WHAT NONE OF THESE DO. Pay anything, or see anything that was paid. Federal
deposits go through EFTPS, Oregon's through Revenue Online, Washington's through
its agencies' own portals, and this app has no window into any of them. Every
tool that could show a balance takes `deposits` as an argument and reports the
whole liability as outstanding when it is not given one. Read `warnings` on
every result first: it is where each figure that is a floor rather than a fact
says so.
"""

from __future__ import annotations

from datetime import date, timedelta

import frappe

from ..args import as_int, as_str, resolve_company
from ..errors import ToolError
from ..form_generators import (
	generate_941_data,
	generate_or_oq_data,
	generate_wa_esd_data,
	quarter_period,
	year_period,
)
from ..result import ToolResult
from ..tax_remittance_calc import (
	FUTA_DEPOSIT_THRESHOLD,
	LOOKBACK_THRESHOLD,
	NEXT_DAY_THRESHOLD,
	QUARTERS,
	deposit_frequency,
	federal_holidays,
	generate_940_data,
	generate_or_132_data,
	lookback_period,
	monthly_due_date,
	monthly_liability,
	quarter_of_month,
	quarterly_return_due,
	semiweekly_due_date,
)
from . import taxforms
from .employee import require_company_scope, require_hr_role

PAYROLL_ENTRY = "Farm Payroll Entry"

#: The employer-side slip columns none of the form generators want. See the
#: module docstring: a deposit is both halves together.
EMPLOYER_FIELDS = (
	"social_security_employer",
	"medicare_employer",
	"futa",
	"state_unemployment",
	"state_employer_other",
	"total_employer_taxes",
)

#: The two states this app's payroll engine computes. Anything else on a slip is
#: reported under a warning rather than silently dropped.
SUPPORTED_STATES = ("OR", "WA")

#: The largest payday lag this will accept. Sixty days is already absurd for a
#: payroll; the bound exists so that a caller passing a date by mistake is
#: refused rather than handed a deadline a year out.
MAX_PAYDAY_OFFSET = 60

#: What Oregon collects on one combined quarterly report, and the key each
#: amount arrives under from the state engine.
OR_COMPONENTS = (
	("or_income_tax", "Oregon income tax withheld", "Oregon DOR"),
	("or_transit_tax", "Statewide Transit Tax withheld", "Oregon DOR"),
	("or_paid_leave_employee", "Paid Leave Oregon — employee", "Oregon DOR"),
	("or_paid_leave_employer", "Paid Leave Oregon — employer", "Oregon DOR"),
	("or_workers_comp", "Workers' Benefit Fund assessment", "Oregon DCBS"),
)

#: The same for Washington, which has no income tax and so no withholding line.
WA_COMPONENTS = (
	("wa_pfml_employee", "WA Paid Family & Medical Leave — employee", "WA ESD"),
	("wa_pfml_employer", "WA Paid Family & Medical Leave — employer", "WA ESD"),
	("wa_cares_employee", "WA Cares Fund — employee", "WA ESD"),
	("wa_li_employee", "Labor & Industries — employee", "WA L&I"),
	("wa_li_employer", "Labor & Industries — employer", "WA L&I"),
)

#: Said FIRST on every 941 prefill, not as a footnote. This app is installed on
#: a tree-fruit operation, where agricultural labour is the normal case and not
#: the exception — an employer who reads a 941 prefill four times a year and
#: never files a 943 has missed their actual return, and the tool that handed
#: them the numbers is where they would have found out.
FORM_943_NOTE = (
	"FARMWORKERS ARE REPORTED ON FORM 943, NOT FORM 941. Form 943 is the ANNUAL "
	"return for agricultural employees, due 31 January; Form 941 is the quarterly "
	"return for everybody else. They are not alternatives and the choice is made "
	"per worker, not per employer — a farm with office staff files BOTH, each "
	"covering only its own people. Nothing on a payroll slip in this app marks a "
	"worker as agricultural labour, so this prefill cannot make that split: it "
	"totals EVERY slip in the quarter. Where the crew is agricultural, these "
	"figures belong on a 943 for the year rather than a 941 for the quarter."
)


# ── The five tools ────────────────────────────────────────────────────────


def get_tax_remittance_summary(args: dict) -> ToolResult:
	"""Everything owed to every agency for a period, broken down by pay period."""
	actor = require_hr_role()
	company = _company(args)
	require_company_scope(actor, company)
	year, quarter, start, end = _window(args)

	slips = _slips(company, start, end)
	warnings: list[str] = []
	federal = _federal_liability(slips, warnings)
	oregon = _state_liability(slips, "OR", OR_COMPONENTS, warnings)
	washington = _state_liability(slips, "WA", WA_COMPONENTS, warnings)

	by_period = []
	for entry, rows in _by_entry(slips):
		by_period.append(
			{
				"payroll_entry": entry["payroll_entry"],
				"period_start": entry["period_start"],
				"period_end": entry["period_end"],
				"quarter": _quarter_of(entry["period_end"]),
				"employee_count": len({r.get("employee") for r in rows if r.get("employee")}),
				"gross_pay": _money(_sum(rows, "gross_pay")),
				"federal_deposit": _federal_liability(rows, None)["deposit_liability"],
				"oregon_total": _state_liability(rows, "OR", OR_COMPONENTS, None)["total"],
				"washington_total": _state_liability(rows, "WA", WA_COMPONENTS, None)["total"],
			}
		)

	other = sorted(
		{
			str(s.get("work_state"))
			for s in slips
			if s.get("work_state") and str(s.get("work_state")) not in SUPPORTED_STATES
		}
	)
	if other:
		warnings.append(
			f"slips carry work states this app's engine does not compute: {', '.join(other)}. "
			"Their gross pay is in the federal totals, where it belongs, and in no state "
			"total — those states' liabilities are not reported here at all."
		)
	if not slips:
		warnings.append(
			f"no Calculated or Submitted payroll found for {company} between {start} and "
			f"{end}; every total is zero. A Draft payroll has not been paid and is not counted."
		)

	grand_total = _money(
		federal["deposit_liability"] + federal["futa"] + oregon["total"] + washington["total"]
	)

	return ToolResult(
		data={
			"company": company,
			"tax_year": year,
			"quarter": quarter,
			"period_start": start,
			"period_end": end,
			"federal": federal,
			"oregon": oregon,
			"washington": washington,
			"by_period": by_period,
			"payroll_entry_count": len(by_period),
			"employee_count": len({s.get("employee") for s in slips if s.get("employee")}),
			"gross_pay": _money(_sum(slips, "gross_pay")),
			"grand_total_remittance": grand_total,
			"slip_count": len(slips),
			"note": (
				"NOTHING HERE HAS BEEN PAID OR IS VISIBLE AS PAID. These are liabilities "
				"computed from payroll: federal through EFTPS, Oregon through Revenue "
				"Online and OED, Washington through ESD and L&I. This app sees none of "
				"those portals, so no figure below is net of a deposit already made. Use "
				"get_tax_deposit_schedule for when each of these is due."
			),
			"warnings": warnings,
		},
		summary=(
			f"Remittance summary for {company}, {quarter or 'FY'} {year}: "
			f"${grand_total:,.2f} across federal, Oregon and Washington "
			f"over {len(by_period)} pay period(s)."
		),
	)


def get_941_prefill(args: dict) -> ToolResult:
	"""Form 941's lines 1 to 15 for a quarter, computed but not recorded."""
	actor = require_hr_role()
	company = _company(args)
	require_company_scope(actor, company)
	year, quarter, start, end = _window(args, quarter_required=True)

	slips = taxforms._load_slips(company, start, end)
	company_info = taxforms._company_info(company, args)
	company_info["ss_wage_base"] = company_info.get("ss_wage_base") or taxforms._ss_wage_base()

	form = generate_941_data(slips, company_info, quarter, year)
	part2 = monthly_liability(slips, quarter, year, total_tax=form.get("line12_total_taxes_after_credits"))

	# The 943 note goes FIRST. See FORM_943_NOTE: on this site agricultural
	# labour is the normal case, and a warning buried at position six is a
	# warning nobody reads.
	warnings = [FORM_943_NOTE, *(form.get("warnings") or [])]
	if not part2["reconciles"]:
		warnings.append(
			f"Part 2's monthly liabilities total ${part2['reconciled_total']:,.2f} against "
			f"line 12's ${part2['line12_total_tax']:,.2f}. They must agree TO THE CENT or "
			"the return is rejected — check the residual before filing."
		)

	existing = _existing_form("941", company, year, quarter)
	if existing:
		warnings.append(
			f"a Tax Form for 941 {quarter} {year} already exists ({existing['name']}, "
			f"status {existing['status']}). THIS PREFILL IS RECOMPUTED FROM TODAY'S PAYROLL "
			"and may not match what that record stores — which is the reason it is stored. "
			"Compare with get_tax_form before filing anything."
		)

	return ToolResult(
		data={
			"company": company,
			"tax_year": year,
			"quarter": quarter,
			"period_start": start,
			"period_end": end,
			"form_941": {key: value for key, value in form.items() if key != "warnings"},
			"part2_monthly_liability": part2,
			"existing_tax_form": existing,
			"note": (
				"A PREFILL, NOT A FILING AND NOT A RECORD. These values are recomputed from "
				"current payroll on every call. To keep a copy of what was actually reported "
				"to the IRS, use generate_tax_form — it stores the numbers as they stood at "
				"generation, which is what a filed return is."
			),
			"warnings": warnings,
		},
		summary=(
			f"941 prefill for {company} {quarter} {year}: line 12 total tax "
			f"${form.get('line12_total_taxes_after_credits', 0):,.2f} on "
			f"${form.get('line2_wages_tips_other_compensation', 0):,.2f} of wages."
		),
	)


def get_state_tax_remittance(args: dict) -> ToolResult:
	"""Oregon's OQ and Form 132, and Washington's ESD quarterly report."""
	actor = require_hr_role()
	company = _company(args)
	require_company_scope(actor, company)
	year, quarter, start, end = _window(args, quarter_required=True)

	wanted = _state_argument(args)
	company_info = taxforms._company_info(company, args)
	warnings: list[str] = []
	reports: dict[str, dict] = {}
	due = quarterly_return_due(quarter, year).isoformat()

	if "OR" in wanted:
		or_slips = taxforms._load_slips(company, start, end, state="OR")
		oq = generate_or_oq_data(or_slips, company_info, quarter, year)
		info = dict(company_info)
		info["ssn_last4_by_employee"] = taxforms._ssn_last4_map(
			{s.get("employee") for s in or_slips if s.get("employee")}
		)
		detail = generate_or_132_data(or_slips, info, quarter, year)
		reports["OR"] = {
			"agency": "Oregon Department of Revenue / Employment Department",
			"due_date": due,
			"forms": ["OQ", "Form 132"],
			"oq": {key: value for key, value in oq.items() if key not in ("warnings", "due_date")},
			"form_132": {key: value for key, value in detail.items() if key != "warnings"},
			"total_due": oq.get("total_due"),
		}
		warnings.extend(f"[OR OQ] {line}" for line in oq.get("warnings") or [])
		warnings.extend(f"[OR 132] {line}" for line in detail.get("warnings") or [])

		oq_wages = _float(oq.get("subject_wages"))
		detail_wages = _float(detail.get("total_wages"))
		if abs(oq_wages - detail_wages) >= 0.01:
			warnings.append(
				f"[OR] the OQ reports ${oq_wages:,.2f} of subject wages and Form 132's rows "
				f"total ${detail_wages:,.2f}, a difference of ${abs(oq_wages - detail_wages):,.2f}. "
				"Oregon reconciles the two against each other and will reject a filing where "
				"they disagree."
			)

	if "WA" in wanted:
		wa_slips = taxforms._load_slips(company, start, end, state="WA")
		info = dict(company_info)
		info["ssn_last4_by_employee"] = taxforms._ssn_last4_map(
			{s.get("employee") for s in wa_slips if s.get("employee")}
		)
		esd = generate_wa_esd_data(wa_slips, info, quarter, year)
		reports["WA"] = {
			"agency": "Washington Employment Security Department",
			"due_date": due,
			"forms": ["WA-ESD"],
			"esd": {key: value for key, value in esd.items() if key not in ("warnings", "due_date")},
			"total_due": esd.get("total_due"),
		}
		warnings.extend(f"[WA] {line}" for line in esd.get("warnings") or [])

	total = _money(sum(_float(r.get("total_due")) for r in reports.values()))

	return ToolResult(
		data={
			"company": company,
			"tax_year": year,
			"quarter": quarter,
			"period_start": start,
			"period_end": end,
			"states": sorted(wanted),
			"reports": reports,
			"due_date": due,
			"combined_total_due": total,
			"note": (
				"BOTH STATES ARE DUE THE LAST DAY OF THE MONTH AFTER THE QUARTER, the same "
				"date as the federal 941 and the one convenience in this calendar. AN OQ IS "
				"NOT A FILING WITHOUT ITS FORM 132: the OQ carries the employer's totals, "
				"Form 132 the per-employee wages and whole hours that Oregon assesses "
				"benefit eligibility from, and the state reconciles one against the other. "
				"Washington's report likewise carries hours per employee, which is a hard "
				"requirement there rather than an approximation."
			),
			"warnings": warnings,
		},
		summary=(
			f"State remittance for {company} {quarter} {year}: "
			f"{', '.join(sorted(wanted))}, ${total:,.2f} combined, due {due}."
		),
	)


def get_tax_deposit_schedule(args: dict) -> ToolResult:
	"""Every federal deposit deadline in a period, plus the state filing dates."""
	actor = require_hr_role()
	company = _company(args)
	require_company_scope(actor, company)
	year, quarter, start, end = _window(args)

	slips = _slips(company, start, end)
	warnings: list[str] = []
	offset = _payday_offset(args)

	frequency, lookback_note = _deposit_frequency(company, year, args, warnings)

	# Both years, always. When 1 January falls on a Saturday the holiday is
	# observed on 31 December of the PRIOR year — the one federal holiday that
	# lands outside its own calendar year, and the one a table built per-year
	# misses, silently shortening a late-December deadline.
	holidays: dict[str, str] = {}
	for one in (year - 1, year, year + 1):
		holidays.update(federal_holidays(one))

	deposits = []
	assumed_paydays = 0
	for entry, rows in _by_entry(slips):
		liability = _federal_liability(rows, None)
		payday, basis, assumed = _payday(entry, offset)
		if not payday:
			continue
		if assumed:
			assumed_paydays += 1

		row = {
			"payroll_entry": entry["payroll_entry"],
			"period_start": entry["period_start"],
			"period_end": entry["period_end"],
			"payday": payday.isoformat(),
			"payday_basis": basis,
			"payday_is_assumed": assumed,
			"deposit_liability": liability["deposit_liability"],
		}
		if frequency["schedule"] == "Semiweekly":
			row.update(semiweekly_due_date(payday, holidays))
		else:
			row.update(
				{
					"due_date": monthly_due_date(payday.year, payday.month, holidays).isoformat(),
					"rule": (
						f"monthly depositor — everything paid in {payday.strftime('%B %Y')} is "
						"deposited by the 15th of the following month."
					),
					"holiday_extension_days": 0,
				}
			)
		if liability["deposit_liability"] >= NEXT_DAY_THRESHOLD:
			row["next_day_rule"] = True
			row["note"] = (
				f"THIS DEPOSIT IS ${liability['deposit_liability']:,.2f}, at or over the "
				f"${NEXT_DAY_THRESHOLD:,.0f} next-day threshold. It is due by the NEXT "
				"BUSINESS DAY after the payday regardless of the schedule above, and a "
				"monthly depositor who accumulates this much becomes a semiweekly depositor "
				"for the rest of this year and all of next."
			)
			warnings.append(
				f"{entry['payroll_entry']} reaches the ${NEXT_DAY_THRESHOLD:,.0f} next-day "
				f"deposit threshold (${liability['deposit_liability']:,.2f}); its due date is "
				"the next business day after the payday, not the one shown by the schedule."
			)
		deposits.append(row)

	if assumed_paydays:
		warnings.append(
			f"{assumed_paydays} of {len(deposits)} deposit(s) have NO RECORDED PAYDAY and use "
			f"the pay period's END DATE{f' plus {offset} day(s)' if offset else ''}. The "
			"deposit clock runs from the date wages were actually paid, so every one of "
			"those dates is EARLY by however long this farm takes to pay after a period "
			"closes — treat them as the earliest possible deadline rather than the real "
			"one. Early is the safe direction to be wrong in, but pass payday_offset_days "
			"with the farm's usual gap to make them right."
		)

	state_dates = _state_deadlines(year, quarter)
	futa_total = _money(sum(_float(s.get("futa")) for s in slips))
	if futa_total and futa_total <= FUTA_DEPOSIT_THRESHOLD:
		warnings.append(
			f"FUTA in this period is ${futa_total:,.2f}, at or under the "
			f"${FUTA_DEPOSIT_THRESHOLD:,.0f} quarterly threshold — nothing is deposited and "
			"it carries into the next quarter. get_futa_summary walks the whole year's carry."
		)

	return ToolResult(
		data={
			"company": company,
			"tax_year": year,
			"quarter": quarter,
			"period_start": start,
			"period_end": end,
			"deposit_schedule": frequency["schedule"],
			"schedule_basis": frequency["basis"],
			"schedule_assumed": frequency.get("assumed", False),
			"lookback_period": lookback_period(year),
			"lookback_total": frequency.get("lookback_total"),
			"lookback_note": lookback_note,
			"lookback_threshold": LOOKBACK_THRESHOLD,
			"next_day_threshold": NEXT_DAY_THRESHOLD,
			"payday_offset_days": offset,
			"federal_deposits": deposits,
			"federal_deposit_total": _money(sum(d["deposit_liability"] for d in deposits)),
			"monthly_rollup": _monthly_rollup(deposits, frequency["schedule"], holidays),
			"futa_liability_in_period": futa_total,
			"futa_deposit_threshold": FUTA_DEPOSIT_THRESHOLD,
			"state_deadlines": state_dates,
			"federal_holidays": {
				iso: name for iso, name in sorted(holidays.items()) if start <= iso <= _plus_year(end)
			},
			"note": (
				"THE SCHEDULE IS DECIDED BY THE LOOKBACK PERIOD AND NOTHING ELSE — the four "
				f"quarters ending 30 June of the prior year. Above ${LOOKBACK_THRESHOLD:,.0f} "
				"of reported tax in that window an employer deposits semiweekly for the whole "
				"of the following year, below it monthly, and this year's payroll does not "
				"enter into it. Oregon's withholding deposits follow the federal schedule; "
				"the state amounts are reported quarterly on the dates below."
			),
			"warnings": warnings,
		},
		summary=(
			f"Deposit schedule for {company}, {quarter or 'FY'} {year}: "
			f"{frequency['schedule']} depositor, {len(deposits)} deposit(s) totalling "
			f"${sum(d['deposit_liability'] for d in deposits):,.2f}."
		),
	)


def get_futa_summary(args: dict) -> ToolResult:
	"""Form 940 for a calendar year — the $7,000 cap consumed in date order."""
	actor = require_hr_role()
	company = _company(args)
	require_company_scope(actor, company)
	year, _quarter, start, end = _window(args, annual=True)

	slips = _slips(company, start, end)
	company_info = taxforms._company_info(company, args)
	company_info.update(_futa_config(year))
	for key in (
		"exempt_payments",
		"credit_reduction",
		"futa_rate",
		"futa_wage_base",
		"futa_state_credit_max",
		"deposits",
	):
		if args.get(key) not in (None, ""):
			company_info[key] = _float(args[key], key=key)

	form = generate_940_data(slips, company_info, year)
	warnings = list(form.get("warnings") or [])

	# What payroll itself computed, against what the 940 walk produces. They
	# should agree; where they do not the difference is the answer to a question
	# somebody is going to ask, so it is reported rather than reconciled away.
	recorded = _money(sum(_float(s.get("futa")) for s in slips))
	computed = form["line8_futa_tax_before_adjustments"]
	if recorded and abs(recorded - computed) >= 0.02:
		warnings.append(
			f"payroll recorded ${recorded:,.2f} of FUTA across the year's slips, while this "
			f"940 computes ${computed:,.2f} from the wage-base walk — a difference of "
			f"${abs(recorded - computed):,.2f}. The walk is the one that matches the form: it "
			"consumes each employee's wage base in date order across the whole year, which a "
			"single pay period computing its own FUTA cannot do. Investigate before filing."
		)

	return ToolResult(
		data={
			"company": company,
			"tax_year": year,
			"period_start": start,
			"period_end": end,
			"form_940": {key: value for key, value in form.items() if key != "warnings"},
			"futa_recorded_on_slips": recorded,
			"note": (
				"FUTA IS EMPLOYER-ONLY AND IS NEVER WITHHELD FROM ANYBODY'S PAY. It is 6.0% "
				"of the first $7,000 each employee earns in the year, less a credit of up to "
				"5.4% for state unemployment tax actually paid on time — a net 0.6% for an "
				"employer in a state not under credit reduction, which in 2025 neither Oregon "
				"nor Washington is. READ `agricultural_coverage` FIRST: FUTA does not apply to "
				"farm labour at all unless the employer paid $20,000 in cash wages in some "
				"quarter or employed 10 or more farmworkers in 20 or more weeks. An employer "
				"under both tests owes nothing and files no 940 — the tax is not reduced, it "
				"does not apply."
			),
			"warnings": warnings,
		},
		summary=(
			f"FUTA summary for {company} {year}: ${form['line12_total_futa_tax']:,.2f} on "
			f"${form['line7_total_taxable_futa_wages']:,.2f} of taxable wages "
			f"({form['employee_count']} employee(s))."
		),
	)


# ── Internal: the period, the company, the slips ──────────────────────────


def _company(args: dict) -> str:
	"""The company, resolved, required — every tool here is company-scoped."""
	return resolve_company(as_str(args, "company"), required=True)


def _window(args: dict, quarter_required: bool = False, annual: bool = False) -> tuple:
	"""The reporting period, from a year and an optional quarter.

	`fiscal_year` and `year` are the same argument under two names, which is what
	every other tool in this app accepts and so what a model will send.
	"""
	year = as_int(args, "fiscal_year")
	if year is None:
		year = as_int(args, "year")
	if year is None:
		raise ToolError(
			"fiscal_year is required — the calendar year as YYYY, e.g. 2025. Tax periods are "
			"calendar periods and never this site's fiscal year. Nothing was computed."
		)
	if not 2000 <= int(year) <= 2100:
		raise ToolError(
			f"fiscal_year must be a four-digit calendar year between 2000 and 2100, got "
			f"{year!r}. Nothing was computed."
		)
	year = int(year)

	quarter = _quarter_argument(args)
	if annual:
		if quarter:
			raise ToolError(
				f"Form 940 is an ANNUAL return and takes no quarter; got {quarter!r}. Its "
				"quarterly liabilities are on `line16_quarterly_liabilities`, computed from "
				"the whole year because the $7,000 wage base is consumed across it and a "
				"quarter cannot see the ones before it. Nothing was computed."
			)
		start, end = year_period(year)
		return year, None, start, end

	if quarter and quarter not in QUARTERS:
		raise ToolError(
			f"quarter must be one of {', '.join(QUARTERS)}, or the number 1 to 4, got {quarter!r}."
		)
	if quarter_required and not quarter:
		raise ToolError(
			f"quarter is required and must be one of {', '.join(QUARTERS)}, or the number "
			"1 to 4. This is a quarterly return. Nothing was computed."
		)
	if quarter:
		start, end = quarter_period(quarter, year)
	else:
		start, end = year_period(year)
	return year, quarter or None, start, end


def _quarter_argument(args: dict) -> str:
	"""`quarter`, in whichever of its two spellings arrived. "Q3", "q3", 3 and "3".

	v0.92.2. THE HANDSET SENDS THE NUMBER AND EVERY TOOL HERE WAS WRITTEN FOR THE
	STRING. A quarter picker on a phone is four buttons and an integer 1 to 4; a
	model asked for a quarter writes "Q3" because that is what the schema says.
	Both had already decided the same thing, and the second one came back as
	"quarter must be one of Q1, Q2, Q3, Q4, got '3'" — a refusal about spelling,
	raised on a value that was never ambiguous, at the one place a caller cannot
	correct it from (the picker has no other answer to give).

	NORMALISED HERE RATHER THAN AT THE FIVE CALLERS, and here rather than in
	`api/mobile.py`, because `_window` is the one function all five reads take
	their period from — the MCP tools and the mobile routes alike. A conversion
	in the route wrapper would have left the MCP surface strict and made the two
	transports disagree about what a valid argument is, which is the failure this
	file's `fiscal_year`/`year` pair already avoids by accepting both names once.

	ANYTHING ELSE IS RETURNED UNCHANGED so the caller sees their own value in the
	refusal `_window` raises. "Q5", "third" and "2026-Q2" are all wrong, and each
	is more useful quoted back than replaced by a guess.
	"""
	text = (as_str(args, "quarter") or "").strip().upper()
	body = text[1:] if text.startswith("Q") else text
	if body.isdigit() and 1 <= int(body) <= 4:
		return f"Q{int(body)}"
	return text


def _state_argument(args: dict) -> set:
	"""Which states to report. Both, unless one is named."""
	value = (as_str(args, "state") or "").strip().upper()
	if not value:
		return set(SUPPORTED_STATES)
	if value not in SUPPORTED_STATES:
		raise ToolError(
			f"state must be one of {', '.join(SUPPORTED_STATES)}, got {value!r}. Those are the "
			"two this app's payroll engine computes. Leave it out for both."
		)
	return {value}


def _payday_offset(args: dict) -> int:
	"""The farm's usual gap in days between a period closing and the money moving."""
	offset = as_int(args, "payday_offset_days", 0) or 0
	if offset < 0 or offset > MAX_PAYDAY_OFFSET:
		raise ToolError(
			f"payday_offset_days must be between 0 and {MAX_PAYDAY_OFFSET}, got {offset}. It "
			"is a number of DAYS between a pay period closing and the money moving, not a "
			"date. Nothing was computed."
		)
	return offset


def _slips(company: str, start: str, end: str) -> list[dict]:
	"""The period's paid slips, carrying the employer halves as well."""
	return taxforms._load_slips(company, start, end, extra_fields=EMPLOYER_FIELDS)


def _by_entry(slips: list[dict]) -> list[tuple[dict, list[dict]]]:
	"""Group slips back into the payroll runs they came from, in date order."""
	groups: dict[str, dict] = {}
	for slip in slips:
		key = str(slip.get("payroll_entry") or "")
		bucket = groups.setdefault(
			key,
			{
				"entry": {
					"payroll_entry": key,
					"period_start": slip.get("period_start"),
					"period_end": slip.get("period_end"),
				},
				"rows": [],
			},
		)
		bucket["rows"].append(slip)
	return [
		(group["entry"], group["rows"])
		for group in sorted(groups.values(), key=lambda g: str(g["entry"]["period_end"] or ""))
	]


def _payday(entry: dict, offset: int) -> tuple[date | None, str, bool]:
	"""The best available date for when this run's wages were actually paid.

	A RUN THAT REACHED THE LEDGER HAS A REAL DATE ON IT. `gl_postings` carries a
	`posting_date` per Journal Entry, and the latest of them is the closest thing
	this app has to a payday. Everything else falls back to the period end plus
	the caller's offset, which is an assumption and is returned as one.
	"""
	period_end = _as_date(entry.get("period_end"))
	posted = _gl_posting_date(entry.get("payroll_entry"))
	if posted:
		return (
			posted,
			(
				f"the latest GL posting date on {entry['payroll_entry']} — this run reached the "
				"ledger, so this is a recorded date rather than an assumption."
			),
			False,
		)
	if not period_end:
		return None, "no readable period end.", True
	if offset:
		return (
			period_end + timedelta(days=offset),
			(
				f"the pay period end plus the {offset}-day payday_offset_days supplied by the "
				"caller. Not a recorded date, but the farm's own stated lag."
			),
			True,
		)
	return (
		period_end,
		(
			"THE PAY PERIOD END, used because no GL posting date exists and no "
			"payday_offset_days was given. The deposit clock runs from the date wages were "
			"PAID, so this date is early by however long this farm takes to pay."
		),
		True,
	)


def _gl_posting_date(payroll_entry: str | None) -> date | None:
	"""The latest posting date among a run's GL postings, where it has any.

	THROUGH THE PARENT DOCUMENT RATHER THAN A QUERY ON THE CHILD TABLE, which is
	how `taxforms._load_slips` reads slips and for the same reason: a child row
	is only reliably reachable through the document that owns it. Querying
	`Farm Payroll GL Posting` by `parent` works against MariaDB, where a child
	table is its own table, and finds nothing against the standalone harness,
	where a document is stored whole — so the query form passes on a bench and
	silently returns no date in the suite.
	"""
	if not payroll_entry:
		return None
	try:
		doc = frappe.get_doc(PAYROLL_ENTRY, payroll_entry)
	except Exception:
		return None
	dates = []
	for row in doc.get("gl_postings") or []:
		get = row.get if isinstance(row, dict) else (lambda k, d=None: getattr(row, k, d))
		moment = _as_date(get("posting_date"))
		if moment:
			dates.append(moment)
	return max(dates) if dates else None


# ── Internal: the arithmetic that needs a slip rather than a total ────────


def _federal_liability(slips: list[dict], warnings: list | None) -> dict:
	"""What a federal deposit for these slips comes to, both halves together.

	`social_security_employer` and `medicare_employer` are read from the slip
	rather than mirrored from the employee side, because the two are NOT always
	equal: Additional Medicare is a 0.9% employee-only surcharge with no employer
	match, so doubling the employee's Medicare overstates the deposit for anybody
	over the threshold. Where a slip predates those columns its amounts are
	mirrored, per slip rather than wholesale, and `warnings` says how many.
	"""
	federal_withheld = _sum(slips, "federal_withholding")
	ss_employee = _sum(slips, "social_security")
	medicare_employee = _sum(slips, "medicare")
	additional = _sum(slips, "additional_medicare")

	# Mirrored PER SLIP. Mirroring the whole set when one row is short, or
	# reading the whole set when one row has it, both produce a total that
	# reconciles against nothing.
	mirrored = [s for s in slips if not s.get("social_security_employer") and s.get("gross_pay")]
	stored = [s for s in slips if s.get("social_security_employer")]
	ss_employer = _sum(stored, "social_security_employer") + _sum(mirrored, "social_security")
	medicare_employer = (
		_sum(stored, "medicare_employer") + _sum(mirrored, "medicare") - _sum(mirrored, "additional_medicare")
	)

	if mirrored and warnings is not None:
		warnings.append(
			f"{len(mirrored)} of {len(slips)} slip(s) carry no employer-side FICA amount, so "
			"the employer half was MIRRORED from the employee half on those rows. That is "
			"right except where somebody crossed the Additional Medicare threshold — the "
			"0.9% surcharge is employee-only and has no employer match — and there it "
			"overstates the deposit slightly."
		)

	deposit = federal_withheld + ss_employee + ss_employer + medicare_employee + medicare_employer

	return {
		"federal_income_tax_withheld": _money(federal_withheld),
		"social_security_employee": _money(ss_employee),
		"social_security_employer": _money(ss_employer),
		"medicare_employee": _money(medicare_employee),
		"medicare_employer": _money(medicare_employer),
		"additional_medicare": _money(additional),
		"deposit_liability": _money(deposit),
		"futa": _money(_sum(slips, "futa")),
		"agency": "IRS (EFTPS)",
		"note": (
			"`deposit_liability` is what one EFTPS deposit covers: federal income tax withheld "
			"plus BOTH halves of Social Security and Medicare. FUTA is reported beside it "
			"because it is deposited separately and on its own quarterly rule."
		),
	}


def _state_liability(slips: list[dict], state: str, components: tuple, warnings: list | None) -> dict:
	"""One state's programs, totalled off what the state engine put on each slip."""
	relevant = [s for s in slips if _has_state(s, state)]
	amounts = {}
	for key, label, agency in components:
		# `state_taxes_detail` is what the state engine returned for this slip,
		# keyed by state — a cross-state pay period carries both states' figures.
		total = sum(
			_float((slip.get("state_taxes_detail") or {}).get(state, {}).get(key)) for slip in relevant
		)
		amounts[key] = {"label": label, "agency": agency, "amount": _money(total)}

	unemployment = _money(
		sum(_float(s.get("state_unemployment")) for s in relevant if s.get("work_state") == state)
	)
	amounts["state_unemployment"] = {
		"label": f"{state} unemployment insurance (employer)",
		"agency": "Oregon Employment Department" if state == "OR" else "WA ESD",
		"amount": unemployment,
	}

	total = _money(sum(row["amount"] for row in amounts.values()))
	if relevant and not unemployment and warnings is not None:
		warnings.append(
			f"[{state}] no unemployment insurance is recorded on any slip, so it is zero in "
			"this total and the total is short by whatever UI actually costs. Each state "
			"assigns an employer its own UI rate every year and payroll computes nothing "
			"without one — check the State Tax Configuration."
		)

	return {
		"state": state,
		"components": amounts,
		"total": total,
		"employee_count": len({s.get("employee") for s in relevant if s.get("employee")}),
		"wages": _money(_state_wages(relevant, state)),
		"slip_count": len(relevant),
	}


def _deposit_frequency(company: str, year: int, args: dict, warnings: list) -> tuple[dict, str]:
	"""Monthly or semiweekly, and how much to trust the figure it was decided on.

	THE LOOKBACK TOTAL COMPUTED FROM THIS SITE IS A FLOOR, NOT A FIGURE. It can
	only see payroll this app ran, and a quarter in which this app ran none reads
	as zero rather than as unknown — which pushes a genuine semiweekly depositor
	onto a monthly schedule, and that is the expensive direction to be wrong in.
	So the computed value is reported with the number of quarters that actually
	had data, an explicit `lookback_total` argument overrides it, and `schedule`
	overrides the whole test.
	"""
	window = lookback_period(year)
	supplied = args.get("lookback_total")

	if supplied not in (None, ""):
		frequency = deposit_frequency(_float(supplied, key="lookback_total"))
		note = "supplied by the caller, which is the only figure that can be right — it comes off the four filed returns."
	else:
		total, quarters_with_data = _lookback_from_site(company, window)
		if quarters_with_data == 0:
			frequency = deposit_frequency(None)
			note = (
				"no payroll at all in the lookback period on this site, so nothing could be "
				"computed and the new-employer default of monthly is assumed."
			)
		else:
			frequency = deposit_frequency(total)
			note = (
				f"computed from this site's own payroll, which had data in "
				f"{quarters_with_data} of the 4 lookback quarters. THIS IS A FLOOR: any "
				"quarter this app did not run reads as zero rather than as unknown."
			)
		if quarters_with_data < 4:
			warnings.append(
				f"the lookback period ({window['start']} to {window['end']}) has payroll in "
				f"only {quarters_with_data} of its 4 quarters on this site, so the computed "
				f"total of ${frequency.get('lookback_total') or 0:,.2f} is a FLOOR and the "
				"deposit schedule derived from it may be wrong. A total understated below "
				f"${LOOKBACK_THRESHOLD:,.0f} puts a semiweekly depositor on a monthly "
				"schedule, which is the direction that causes late deposits. Pass "
				"lookback_total off the four filed 941s, or schedule directly."
			)

	override = as_str(args, "schedule")
	if override:
		choice = override.strip().title()
		if choice not in ("Monthly", "Semiweekly"):
			raise ToolError(
				f"schedule must be 'Monthly' or 'Semiweekly', got {override!r}. Leave it out "
				"to have it decided from the lookback period. Nothing was computed."
			)
		frequency = {
			**frequency,
			"schedule": choice,
			"basis": f"supplied as {choice} by the caller, overriding the lookback test.",
			"assumed": False,
		}
		note = "not used — the schedule was supplied directly."

	return frequency, note


def _lookback_from_site(company: str, window: dict) -> tuple[float, int]:
	"""This site's own employment tax in the lookback period, and its coverage."""
	slips = taxforms._load_slips(company, window["start"], window["end"])
	total = 0.0
	quarters = set()
	for slip in slips:
		total += (
			_float(slip.get("federal_withholding"))
			+ _float(slip.get("social_security")) * 2
			+ (_float(slip.get("medicare")) - _float(slip.get("additional_medicare"))) * 2
			+ _float(slip.get("additional_medicare"))
		)
		moment = _as_date(slip.get("period_end"))
		if moment:
			quarters.add((moment.year, quarter_of_month(moment.month)))
	return _money(total), len(quarters)


def _monthly_rollup(deposits: list[dict], schedule: str, holidays: dict) -> list[dict]:
	"""One row per calendar month, which is what a monthly depositor actually pays.

	Reported for a semiweekly depositor too, because the month is still how the
	liability is summarised on the return and how anybody checks a total.
	"""
	months: dict[tuple[int, int], float] = {}
	for row in deposits:
		payday = _as_date(row.get("payday"))
		if not payday:
			continue
		key = (payday.year, payday.month)
		months[key] = months.get(key, 0.0) + row["deposit_liability"]

	return [
		{
			"month": f"{year}-{month:02d}",
			"quarter": quarter_of_month(month),
			"liability": _money(amount),
			"due_date": monthly_due_date(year, month, holidays).isoformat(),
			"applies": schedule == "Monthly",
		}
		for (year, month), amount in sorted(months.items())
	]


def _state_deadlines(year: int, quarter: str | None) -> list[dict]:
	"""When each return is due. Federal, Oregon and Washington share the date."""
	rows = []
	for one in [quarter] if quarter else list(QUARTERS):
		due = quarterly_return_due(one, year).isoformat()
		rows.extend(
			[
				{
					"jurisdiction": "Federal",
					"form": "941",
					"agency": "IRS",
					"quarter": one,
					"due_date": due,
					"note": "Farmworkers go on Form 943 instead, filed annually. See get_941_prefill.",
				},
				{
					"jurisdiction": "OR",
					"form": "OQ + Form 132",
					"agency": "Oregon DOR / OED",
					"quarter": one,
					"due_date": due,
					"note": "Oregon withholding DEPOSITS follow the federal schedule; this is the report.",
				},
				{
					"jurisdiction": "WA",
					"form": "WA-ESD",
					"agency": "WA Employment Security Department",
					"quarter": one,
					"due_date": due,
					"note": "UI, Paid Family & Medical Leave and WA Cares are reported together.",
				},
			]
		)
	return rows


def _futa_config(year: int) -> dict:
	"""The FUTA rate, base and state credit off the FICA Configuration for a year.

	Absent, the statutory defaults in `tax_remittance_calc` apply and
	`generate_940_data` is left to say so. A missing configuration is not an
	error here: the statutory rates have not moved in years, and a summary that
	refused to compute without a row an operator has never had to create would be
	a summary nobody could run.
	"""
	try:
		row = frappe.db.get_value(
			"FICA Configuration",
			{"tax_year": int(year)},
			["futa_rate", "futa_wage_base", "futa_state_credit_max"],
			as_dict=True,
		)
	except Exception:
		return {}
	return {key: value for key, value in (row or {}).items() if value not in (None, "")}


def _existing_form(form_type: str, company: str, year: int, quarter: str | None) -> dict | None:
	"""Whether a Tax Form has already been generated and stored for this period."""
	try:
		row = frappe.db.get_value(
			"Tax Form",
			{
				"form_type": form_type,
				"company": company,
				"fiscal_year": str(year),
				"quarter": quarter or "",
			},
			["name", "status", "filed_date"],
			as_dict=True,
		)
	except Exception:
		return None
	return (
		{key: (str(value) if hasattr(value, "isoformat") else value) for key, value in row.items()}
		if row
		else None
	)


# ── Internal: small shared helpers ────────────────────────────────────────


def _has_state(slip: dict, state: str) -> bool:
	"""Whether a slip has anything to say about a state.

	`taxforms._slip_has_state`'s rule and for its reason: a slip whose primary
	work state is the other one can still carry this state's taxes, because a
	cross-state pay period runs both engines.
	"""
	if slip.get("work_state") == state:
		return True
	return bool((slip.get("state_taxes_detail") or {}).get(state))


def _state_wages(slips: list[dict], state: str) -> float:
	"""The wages attributed to one state, using the split where a slip has one."""
	total = 0.0
	for slip in slips:
		split = slip.get("state_wages") or {}
		if isinstance(split, dict) and state in split:
			total += _float(split.get(state))
		elif slip.get("work_state") == state:
			total += _float(slip.get("gross_pay"))
	return total


def _quarter_of(value) -> str | None:
	"""Which quarter a date falls in, or None if it is unreadable."""
	moment = _as_date(value)
	return quarter_of_month(moment.month) if moment else None


def _plus_year(end: str) -> str:
	"""One year past a period end — the window a due date can be pushed into."""
	moment = _as_date(end)
	if not moment:
		return end
	return date(moment.year + 1, moment.month, min(moment.day, 28)).isoformat()


def _as_date(value) -> date | None:
	"""A date from an ISO string, a date or a datetime. None if unreadable."""
	if isinstance(value, date):
		return value
	if not value:
		return None
	try:
		return date.fromisoformat(str(value)[:10])
	except ValueError:
		return None


def _float(value, default: float = 0.0, key: str = "") -> float:
	"""A number from a document field or an argument."""
	if value in (None, ""):
		return default
	try:
		return float(value)
	except (TypeError, ValueError):
		if key:
			raise ToolError(f"{key} must be a number, got {value!r}. Nothing was computed.") from None
		return default


def _money(value) -> float:
	"""Round to cents."""
	return round(_float(value), 2)


def _sum(rows: list[dict], key: str) -> float:
	"""Total one key across rows."""
	return sum(_float(row.get(key)) for row in rows)

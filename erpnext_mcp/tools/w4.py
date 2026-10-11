# SPDX-License-Identifier: MIT
"""W-4 / federal withholding tools.

v0.28.0. Structured W-4 processing and the withholding engine that reads it.

The W-4 superseding pattern: a new W-4 for the same employee + tax_year sets
the old one to Superseded with a superseded_by link. Only one Active W-4 per
employee per tax_year.

────────────────────────────────────────────────────────────────────────────
v0.48.0: THE EMPLOYER HALF, AND THE FORM ITSELF
────────────────────────────────────────────────────────────────────────────

FOUR RELEASES COLLECTED THE ELECTIONS AND NONE OF THEM PRODUCED THE FORM. The
same gap `render_i9_pdf` closed for the I-9 in v0.47.1: what an employer keeps
for an employee's withholding is Form W-4, and what this app could produce was a
Desk print of a doctype. `render_w4_pdf` fills the IRS's own fillable PDF, which
this app now ships at `templates/w4_form.pdf`; `w4_pdf.py` is the field table
and argues its own case, including the four things it leaves deliberately blank.

AND THE FORM HAS A BLOCK THE APP HAD NOTHING FOR. Step 5's Employers Only row
asks for the employer's name and address, the first date of employment, and the
EIN. All three were on the site and none was reachable from a W-4:
`_employer_for` resolves them at render time — the employer block from I-9
Settings or the Company, exactly as Section 2 of the I-9 resolves it, and the
first date of employment from `Employee.date_of_joining`. Resolved rather than
copied onto every row, because a farm that changes its registered address should
not have a hundred W-4s carrying the old one.

WHAT IS STORED IS WHO PROCESSED IT. `submit_w4` writes `employer_signer_name`,
`employer_signer_title` and `employer_signed_at` off the authorized signer
roster — a fact about this form on this day that nothing else on the site
records, and the W-4 half of what `tools/signers.py` exists for.
"""

from __future__ import annotations

import frappe
from frappe.utils import now

from .. import compat, w4_pdf
from ..args import as_bool, as_int, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult
from ..withholding import (
	FILING_STATUS_MAP,
	PERIODS_PER_YEAR,
	SEED_TAX_YEAR,
	calculate_federal_withholding,
)
from . import artifacts, files, signers
from . import employee as employee_tool

W4_FORM = "W-4 Form"
FEDERAL_TAX_TABLE = "Federal Tax Table"
FICA_CONFIG = "FICA Configuration"
EMPLOYEE = "Employee"


def _resolve_employee(args: dict) -> str:
	emp = as_str(args, "employee") or as_str(args, "name") or as_str(args, "employee_name")
	if not emp:
		raise ToolError("employee is required.")
	if frappe.db.exists(EMPLOYEE, emp):
		return emp
	found = frappe.db.get_value(EMPLOYEE, {"employee_name": emp}, "name")
	if found:
		return str(found)
	raise ToolError(f"no Employee called {emp!r} on this site.")


def _w4_fields() -> list[str]:
	return [
		"name",
		"employee",
		"employee_name",
		"company",
		"tax_year",
		"status",
		"effective_date",
		"superseded_by",
		"filing_status",
		"multiple_jobs",
		"additional_income_from_other_jobs",
		"dependents_under_17_count",
		"dependents_under_17_amount",
		"other_dependents_count",
		"other_dependents_amount",
		"total_dependents_credit",
		"other_income",
		"deductions",
		"extra_withholding_per_period",
		"signed_at",
		"signed_ip",
		# v0.48.0. Who processed the form for the employer, and when. NOT the
		# employer's own name, address or EIN — those are resolved at render
		# time by `_employer_for`, so they cannot go stale on a stored row.
		"employer_signer_name",
		"employer_signer_title",
		"employer_signed_at",
		"generated_pdf",
		"generated_pdf_on",
		# v0.291.0. The employee's Oregon OR-W-4, where this site has the columns.
		*compat.existing_fields(W4_FORM, ("or_w4_on_file", "or_filing_status", "or_allowances",
		                                   "or_additional_withholding", "or_exempt")),
		# v0.294.0. The OR-W-4 as its own form kind: line 4a, the Redetermination box, what Oregon withholding is
		# based on (the fallback order), its own signature date, and whether the DOR must be sent a copy.
		*compat.existing_fields(W4_FORM, OR_EXTRA_FIELDS),
	]


# ── read-only tools ──────────────────────────────────────────────────────


def get_w4(args: dict) -> ToolResult:
	"""Current active W-4 for an employee."""
	employee = _resolve_employee(args)
	tax_year = as_int(args, "tax_year")

	filters = {"employee": employee, "status": "Active"}
	if tax_year:
		filters["tax_year"] = tax_year

	name = frappe.db.get_value(
		W4_FORM,
		filters,
		"name",
		order_by="tax_year desc, effective_date desc",
	)
	if not name:
		raise ToolError(f"no active W-4 Form for employee {employee!r}.")

	row = frappe.db.get_value(W4_FORM, name, _w4_fields(), as_dict=True)
	data = {k: (str(v) if v is not None else None) for k, v in row.items()}
	log_access(name, "get_w4")
	return ToolResult(
		data=data, summary=f"W-4 for {employee}: {row.get('filing_status')}, tax year {row.get('tax_year')}"
	)


def list_w4_forms(args: dict) -> ToolResult:
	"""All W-4 forms with filtering."""
	filters = {}
	company = as_str(args, "company")
	if company:
		filters["company"] = resolve_company(company)
	status = as_str(args, "status")
	if status:
		filters["status"] = status
	tax_year = as_int(args, "tax_year")
	if tax_year:
		filters["tax_year"] = tax_year
	limit = as_int(args, "limit", 100)
	if limit and limit > 500:
		limit = 500

	rows = frappe.db.get_all(
		W4_FORM,
		filters=filters,
		fields=[
			"name",
			"employee",
			"employee_name",
			"company",
			"tax_year",
			"status",
			"effective_date",
			"filing_status",
		],
		limit_page_length=limit,
		order_by="modified desc",
	)
	data = {"forms": [dict(r) for r in rows], "count": len(rows)}
	return ToolResult(data=data, summary=f"{len(rows)} W-4 form(s)")


def get_fica_config(args: dict) -> ToolResult:
	"""Current FICA rates."""
	try:
		doc = frappe.get_doc(FICA_CONFIG)
	except Exception:
		return ToolResult(
			data={"note": "FICA Configuration does not exist yet. Run bench migrate."},
			summary="FICA Configuration not found",
		)
	data = {
		"tax_year": int(doc.tax_year or 2025),
		"social_security_rate_employee": float(doc.social_security_rate_employee or 6.2),
		"social_security_rate_employer": float(doc.social_security_rate_employer or 6.2),
		"social_security_wage_base": float(doc.social_security_wage_base or 176100),
		"medicare_rate_employee": float(doc.medicare_rate_employee or 1.45),
		"medicare_rate_employer": float(doc.medicare_rate_employer or 1.45),
		"additional_medicare_threshold": float(doc.additional_medicare_threshold or 200000),
		"additional_medicare_rate": float(doc.additional_medicare_rate or 0.9),
		"futa_rate": float(doc.futa_rate or 6.0),
		"futa_wage_base": float(doc.futa_wage_base or 7000),
		"futa_state_credit_max": float(doc.futa_state_credit_max or 5.4),
	}
	return ToolResult(data=data, summary=f"FICA config for tax year {data['tax_year']}")


def get_federal_tax_table(args: dict) -> ToolResult:
	"""Withholding brackets for a filing status and payroll period."""
	tax_year = as_int(args, "tax_year")
	if not tax_year:
		raise ToolError("tax_year is required.")
	filing_status = as_str(args, "filing_status", required=True)
	payroll_period = as_str(args, "payroll_period", required=True)

	rows = frappe.db.get_all(
		FEDERAL_TAX_TABLE,
		filters={
			"tax_year": tax_year,
			"filing_status": filing_status,
			"payroll_period": payroll_period,
		},
		fields=["bracket_floor", "bracket_ceiling", "base_tax", "marginal_rate"],
		order_by="bracket_floor asc",
	)
	data = {
		"brackets": [dict(r) for r in rows],
		"count": len(rows),
		"tax_year": tax_year,
		"filing_status": filing_status,
		"payroll_period": payroll_period,
	}
	return ToolResult(
		data=data, summary=f"{len(rows)} bracket(s) for {filing_status}, {payroll_period}, {tax_year}"
	)


def preview_federal_withholding(args: dict) -> ToolResult:
	"""Dry-run withholding calculation showing exactly what would be withheld."""
	employee = _resolve_employee(args)
	gross_pay = as_float(args, "gross_pay", required=True)
	pay_frequency = as_str(args, "pay_frequency", required=True)
	if pay_frequency not in PERIODS_PER_YEAR:
		raise ToolError(f"pay_frequency must be one of: {', '.join(PERIODS_PER_YEAR)}.")

	w4_data, tax_year = _load_w4_data(employee, as_int(args, "tax_year"))
	fica = _load_fica_config()
	tax_table = _load_tax_table(tax_year, w4_data["filing_status"], pay_frequency,
	                            checkbox=bool(w4_data.get("multiple_jobs")))

	ytd_gross = as_float(args, "ytd_gross", 0.0)
	ytd_ss = as_float(args, "ytd_ss_withheld", 0.0)

	result = calculate_federal_withholding(
		gross_pay,
		pay_frequency,
		w4_data,
		ytd_gross,
		ytd_ss,
		fica,
		tax_table,
	)
	result["employee"] = employee
	result["tax_year"] = tax_year
	result["gross_pay"] = gross_pay
	result["pay_frequency"] = pay_frequency

	return ToolResult(
		data=result,
		summary=f"Withholding preview for {employee}: ${result['total_employee_tax']} employee, "
		f"${result['total_employer_tax']} employer on ${gross_pay} {pay_frequency}",
	)


def list_employees_missing_w4(args: dict) -> ToolResult:
	"""Employees with no active W-4."""
	company = as_str(args, "company")
	emp_filters = {"status": "Active"}
	if company:
		emp_filters["company"] = resolve_company(company)

	employees = frappe.db.get_all(
		EMPLOYEE,
		filters=emp_filters,
		fields=["name", "employee_name", "company", "date_of_joining"],
	)

	if _is_or_w4(args):
		return _missing_or_w4(employees)

	missing = []
	for emp in employees:
		has_w4 = federal_row_name(emp["name"]) if compat.has_field(W4_FORM, "form_kind") else frappe.db.exists(
			W4_FORM, {"employee": emp["name"], "status": "Active"})
		if not has_w4:
			missing.append(dict(emp))

	data = {"employees": missing, "count": len(missing)}
	return ToolResult(data=data, summary=f"{len(missing)} active employee(s) without a W-4")


def _missing_or_w4(employees: list) -> ToolResult:
	"""v0.294.0. `form=or_w4`: who Oregon withholds a flat 8% from (no OR-W-4 and nothing in the fallback order), and
	which certificates must still be sent to the DOR."""
	missing, to_send = [], []
	for emp in employees:
		row = oregon_row(emp["name"])
		if not row:
			missing.append({**dict(emp), "withheld_at": "flat 8% (ORS 316.182) until an OR-W-4 is entered"})
		elif int(row.get("or_forward_required") or 0) and not row.get("or_forwarded_on"):
			to_send.append({"employee": emp["name"], "employee_name": emp.get("employee_name"), "form": row["name"],
			                "why": row.get("or_forward_reasons"), "send_by": str(row.get("or_forward_due") or "")[:10]})
	to_send.sort(key=lambda r: r["send_by"])
	return ToolResult(
		data={"form": "OR-W-4", "employees": missing, "count": len(missing), "send_to_dor": to_send,
		      "note": "Only workers paid Oregon wages need one; an employee who works only in Washington can be "
		              "ignored here."},
		summary=f"{len(missing)} active employee(s) without an OR-W-4; {len(to_send)} certificate(s) to send to "
		        "the Oregon DOR")


def calculate_payroll_taxes(args: dict) -> ToolResult:
	"""Run the full calc engine and return the breakdown (read-only calc)."""
	employee = _resolve_employee(args)
	gross_pay = as_float(args, "gross_pay", required=True)
	pay_frequency = as_str(args, "pay_frequency", required=True)
	if pay_frequency not in PERIODS_PER_YEAR:
		raise ToolError(f"pay_frequency must be one of: {', '.join(PERIODS_PER_YEAR)}.")

	w4_data, tax_year = _load_w4_data(employee, as_int(args, "tax_year"))
	fica = _load_fica_config()
	tax_table = _load_tax_table(tax_year, w4_data["filing_status"], pay_frequency,
	                            checkbox=bool(w4_data.get("multiple_jobs")))

	ytd_gross = as_float(args, "ytd_gross", 0.0)
	ytd_ss = as_float(args, "ytd_ss_withheld", 0.0)

	result = calculate_federal_withholding(
		gross_pay,
		pay_frequency,
		w4_data,
		ytd_gross,
		ytd_ss,
		fica,
		tax_table,
	)
	result["employee"] = employee
	result["tax_year"] = tax_year

	return ToolResult(
		data=result,
		summary=f"Payroll taxes for {employee}: ${result['total_employee_tax']} employee, "
		f"${result['total_employer_tax']} employer",
	)


# ── mutating tools ───────────────────────────────────────────────────────


def submit_w4(args: dict) -> ToolResult:
	"""Create a W-4 Form, superseding any prior active W-4 for same employee+tax_year.

	v0.48.0: WHO PROCESSED IT IS RECORDED. Form W-4's Employers Only block is
	completed by somebody acting for the employer, and until this release the
	only trace of who that was lived in the MCP Action Log — which answers "which
	account made a call" and not "which authorised person processed this
	employee's withholding". Where I-9 Settings has an authorized signer roster,
	the calling account has to be on it with `can_sign_w4`, and their printed
	name and title are stored; where there is no roster, the calling account's
	own name is stored and nothing is refused, which is every site on the day it
	upgrades. `tools/signers.py` carries the whole rule.

	NOTHING ABOUT THE WITHHOLDING CHANGED. The signer is recorded beside the
	elections, not consulted about them — the engine reads the same columns it
	always did.

	v0.94.0: `require_hiring_role`, WHERE THERE WAS NO ROLE GATE. A W-4 is the
	worker's own withholding election, collected by whoever is sitting with them
	— which on this farm is the foreman — and processing one was previously open
	to any enrolled account. TWO GATES NOW STAND HERE AND THEY ANSWER DIFFERENT
	QUESTIONS: this one decides who may PROCESS a W-4, and `resolve_signature`
	below decides whose name may go in the Employers Only block. The second is a
	per-person roster and is unchanged by this release.
	"""
	employee_tool.require_hiring_role()
	if _is_or_w4(args):
		return _submit_or_w4(args)
	employee = _resolve_employee(args)
	company = resolve_company(as_str(args, "company"), required=True)
	tax_year = as_int(args, "tax_year")
	if not tax_year:
		raise ToolError("tax_year is required.")
	filing_status = as_str(args, "filing_status", required=True)

	valid_statuses = ["Single or Married Filing Separately", "Married Filing Jointly", "Head of Household"]
	if filing_status not in valid_statuses:
		raise ToolError(f"filing_status must be one of: {', '.join(valid_statuses)}.")

	multiple_jobs = bool(args.get("multiple_jobs"))
	dependents_under_17 = as_int(args, "dependents_under_17_count", 0) or 0
	other_dependents = as_int(args, "other_dependents_count", 0) or 0
	other_income = as_float(args, "other_income", 0.0)
	deductions = as_float(args, "deductions", 0.0)
	extra_withholding = as_float(args, "extra_withholding_per_period", 0.0)
	additional_other_jobs = as_float(args, "additional_income_from_other_jobs", 0.0)

	# Resolved BEFORE the insert, so a caller who is not authorized to process a
	# W-4 is refused having written nothing at all.
	signature = signers.resolve_signature(
		args, "W-4", "employer_signer_name", "employer_signer_title", required=False
	)

	# Supersede any existing active W-4 for this employee + tax_year
	existing = frappe.db.get_all(
		W4_FORM,
		filters={"employee": employee, "tax_year": tax_year, "status": "Active"},
		fields=list(compat.existing_fields(W4_FORM, ("name", "form_kind"))),
	)
	# v0.294.0. A federal W-4 supersedes federal W-4s only — an OR-W-4 filed on its own stands.
	existing = [row for row in existing if _kind(row) == "W-4"]
	oregon = _oregon_w4(args, employee)
	if oregon and compat.has_field(W4_FORM, "or_forward_required") and any(k in args for k in OR_KEYS):
		oregon.update(or_forwarding(oregon, employee, args, frappe.utils.today()))
	if compat.has_field(W4_FORM, "form_kind"):
		oregon["form_kind"] = "W-4"

	doc = frappe.get_doc(
		{
			"doctype": W4_FORM,
			"employee": employee,
			"company": company,
			"tax_year": tax_year,
			"status": "Active",
			"effective_date": frappe.utils.today(),
			"filing_status": filing_status,
			"multiple_jobs": 1 if multiple_jobs else 0,
			"additional_income_from_other_jobs": additional_other_jobs,
			"dependents_under_17_count": dependents_under_17,
			"other_dependents_count": other_dependents,
			"other_income": other_income,
			"deductions": deductions,
			"extra_withholding_per_period": extra_withholding,
			**oregon,
			"employer_signer_name": signature["name"],
			"employer_signer_title": signature["title"],
			"employer_signed_at": now(),
			"signed_at": now(),
			"signed_ip": (
				frappe.local.request.remote_addr
				if hasattr(frappe, "local") and hasattr(frappe.local, "request") and frappe.local.request
				else ""
			),
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()

	# Mark old ones as superseded
	for old in existing:
		frappe.db.set_value(
			W4_FORM,
			old["name"],
			{
				"status": "Superseded",
				"superseded_by": doc.name,
			},
		)

	emp_name = frappe.db.get_value(EMPLOYEE, employee, "employee_name") or employee
	return ToolResult(
		data={
			"name": doc.name,
			"employee": employee,
			"employee_name": emp_name,
			"tax_year": tax_year,
			"filing_status": filing_status,
			"status": "Active",
			"superseded": [old["name"] for old in existing],
			"employer_signer_name": signature["name"],
			"employer_signer_title": signature["title"] or None,
			"signer_roster_enforced": bool(signature["configured"]),
		},
		summary=f"W-4 submitted for {emp_name}: {filing_status}, tax year {tax_year}"
		+ (f", processed by {signature['name']}" if signature["name"] else "")
		+ (f", superseded {len(existing)} prior W-4(s)" if existing else ""),
	)


OR_STATUSES = ("Single", "Married", "Married, withhold at higher Single rate")
#: v0.294.0. The fallback order in the 2026 OR-W-4 instructions (Rev. 02-17-26), then a refused certificate.
OR_SOURCES = ("OR-W-4", "Pre-2020 Oregon-only W-4", "Pre-2020 federal W-4", "Refused")
OR_KEYS = ("or_w4_on_file", "or_filing_status", "or_allowances", "or_additional_withholding", "or_exempt")
OR_EXTRA_FIELDS = ("form_kind", "or_exemption_code", "or_redetermination", "or_source", "or_federally_exempt",
                   "or_signed_at",
                   "or_signed_ip", "or_forward_required", "or_forward_reasons", "or_forward_due", "or_forwarded_on")


def _kind(row: dict) -> str:
	return str(row.get("form_kind") or "W-4")


def _active_rows(employee: str, fields: list) -> list:
	"""The employee's Active W-4 Form rows, newest first."""
	return [dict(r) for r in frappe.db.get_all(
		W4_FORM, filters={"employee": employee, "status": "Active"}, fields=list(compat.existing_fields(W4_FORM, fields)),
		order_by="effective_date desc, creation desc", limit=50) or []]


def _federal_2020_plus(employee: str) -> dict:
	"""v0.308.0. The employee's newest Active federal W-4 of tax year 2020 or later, or {}."""
	rows = [r for r in _active_rows(employee, ["name", "form_kind", "tax_year", "effective_date"])
	        if _kind(r) == "W-4" and int(r.get("tax_year") or 0) >= 2020]
	rows.sort(key=lambda r: (int(r.get("tax_year") or 0), str(r.get("effective_date") or "")), reverse=True)
	return rows[0] if rows else {}


def federal_row_name(employee: str) -> str:
	"""v0.294.0. The employee's active FEDERAL W-4 — never an OR-W-4 filed on its own."""
	rows = _active_rows(employee, ["name", "form_kind", "tax_year", "effective_date"])
	rows.sort(key=lambda r: (int(r.get("tax_year") or 0), str(r.get("effective_date") or "")), reverse=True)
	return next((r["name"] for r in rows if _kind(r) == "W-4"), "")


def oregon_row(employee: str) -> dict:
	"""v0.294.0. The newest Active row carrying the employee's Oregon elections — an OR-W-4 filed on its own, or a W-4
	that carries the Oregon fields — or {}."""
	for row in _active_rows(employee, ["name", *OR_KEYS, *OR_EXTRA_FIELDS, "tax_year", "effective_date"]):
		if _kind(row) == "OR-W-4" or int(row.get("or_w4_on_file") or 0) or row.get("or_source"):
			return row
	return {}


def oregon_for(employee: str) -> dict:
	"""What Oregon withholding follows for this employee (`state_withholding` reads it). The fallback order:
	the OR-W-4; a pre-2020 Oregon-only W-4; a pre-2020 federal W-4; then the flat 8% (ORS 316.182). A REFUSED
	certificate is Single with no allowances (OAR 150-316-0290)."""
	row = oregon_row(employee)
	source = str(row.get("or_source") or ("OR-W-4" if int(row.get("or_w4_on_file") or 0) else ""))
	federal = _federal_2020_plus(employee)
	if not source:
		return {"on_file": False, "source": "", "federal_2020_plus": bool(federal)}
	# v0.308.0. A pre-2020 federal W-4 stands in for an OR-W-4 only until the worker files a 2020-or-later federal
	# W-4; after that there is no Oregon election left on file and OAR 150-316-0234 makes it a flat 8%.
	# (A 2020+ federal W-4 that carried the old Oregon fields forward is itself that newer form.)
	if source == "Pre-2020 federal W-4" and federal and (
			federal.get("name") == row.get("name")
			or str(federal.get("effective_date") or "") > str(row.get("effective_date") or "")):
		return {"on_file": False, "source": "", "federal_2020_plus": True,
		        "reason": f"pre-2020 federal W-4 {row.get('name')} superseded by 2020+ federal W-4 {federal.get('name')}"}
	if source == "Refused":
		return {"on_file": True, "source": source, "filing_status": "Single", "allowances": 0,
		        "additional_withholding": 0.0, "exempt": False, "tax_year": int(row.get("tax_year") or 0) or None,
		        "form": row.get("name")}
	return {"on_file": True, "source": source, "filing_status": row.get("or_filing_status") or "Single",
	        "allowances": int(row.get("or_allowances") or 0),
	        "additional_withholding": float(row.get("or_additional_withholding") or 0),
	        "exempt": bool(int(row.get("or_exempt") or 0)), "tax_year": int(row.get("tax_year") or 0) or None,
	        # v0.295.0. "No" is an Oregon-only exemption (8%, Pub 150-211-602); blank is not recorded.
	        "federally_exempt": str(row.get("or_federally_exempt") or "") != "No",
	        "form": row.get("name")}


def _expected_weekly_wages(employee: str, args: dict) -> float | None:
	"""For the exempt-over-$200-a-week test: what the caller says, else 40 hours at the hourly rate, else unknown."""
	if args.get("or_expected_weekly_wages") not in (None, ""):
		return as_float(args, "or_expected_weekly_wages", 0.0)
	row = frappe.db.get_value("Farm Salary Structure", {"employee": employee, "is_active": 1},
	                          ["pay_type", "base_rate"], as_dict=True) if compat.doctype_exists(
		"Farm Salary Structure") else None
	if row and str(row.get("pay_type") or "") == "Hourly" and float(row.get("base_rate") or 0) > 0:
		return round(float(row["base_rate"]) * 40, 2)
	return None


def or_forwarding(values: dict, employee: str, args: dict, signed_on: str, params: dict | None = None) -> dict:
	"""v0.294.0. Whether a copy goes to the Oregon DOR (OAR 150-316-0290(4)), why, and by when. The thresholds are
	the payroll setting `oregon_withholding`'s `forwarding` — config, not code."""
	import datetime

	from .. import payroll_settings

	rule = {**{"max_allowances": 10, "exempt_weekly_wages": 200.0, "days": 20, "source": "OAR 150-316-0290(4)"},
	        **((params or payroll_settings.oregon_withholding(signed_on)).get("forwarding") or {})}
	reasons = []
	allowances = int(values.get("or_allowances") or 0)
	if allowances > int(rule["max_allowances"]):
		reasons.append(f"claims {allowances} allowances (more than {rule['max_allowances']})")
	if int(values.get("or_exempt") or 0):
		federal = str(values.get("or_federally_exempt") or "")
		if federal == "No":
			reasons.append("claims an Oregon-only exemption (not exempt federally) — withheld at 8% meanwhile "
			               "(Publication 150-211-602)")
		else:
			weekly = _expected_weekly_wages(employee, args)
			if weekly is None:
				reasons.append(f"claims exempt and expected wages are not known — send it if they are over "
				               f"${float(rule['exempt_weekly_wages']):,.0f} a week (or_expected_weekly_wages)")
			elif weekly > float(rule["exempt_weekly_wages"]):
				reasons.append(f"claims exempt with expected wages of ${weekly:,.2f} a week (over "
				               f"${float(rule['exempt_weekly_wages']):,.0f})")

	if not reasons:
		return {"or_forward_required": 0, "or_forward_reasons": None, "or_forward_due": None}
	# v0.312.0. The policy says what the days count from (forwarding.basis) — config, not code.
	basis = str(rule.get("basis") or "signature")
	start = signed_on if basis == "signature" else (as_str(args, "or_received_on") or frappe.utils.today())
	due = datetime.date.fromisoformat(str(start)[:10]) + datetime.timedelta(days=int(rule["days"]))
	return {"or_forward_required": 1, "or_forward_reasons": "; ".join(reasons) + f" — {rule['source']}"
	        + f" ({int(rule['days'])} days from the {'signature' if basis == 'signature' else 'day received'})",
	        "or_forward_due": due.isoformat()}


def _is_or_w4(args: dict) -> bool:
	return str(args.get("form") or "").strip().lower().replace("-", "_") in ("or_w4", "orw4", "oregon")


def _request_ip() -> str:
	return (frappe.local.request.remote_addr
	        if hasattr(frappe, "local") and hasattr(frappe.local, "request") and frappe.local.request else "")


def _submit_or_w4(args: dict) -> ToolResult:
	"""v0.294.0. Form OR-W-4 on its own (`form=or_w4`): the employee's Oregon elections, line 4a, the Redetermination
	box, what Oregon withholding is based on, its own signature date — and whether the DOR must be sent a copy, and
	by when. Supersedes the employee's earlier OR-W-4; the federal W-4 is untouched."""
	if not compat.has_field(W4_FORM, "form_kind"):
		raise ToolError("this site has no OR-W-4 form kind yet — run `bench migrate` after upgrading. Nothing was saved.")
	employee = _resolve_employee(args)
	company = resolve_company(as_str(args, "company"), required=True)
	keys = (*OR_KEYS, "or_exemption_code", "or_redetermination", "or_source", "or_federally_exempt")
	if not any(key in args for key in keys):
		raise ToolError("an OR-W-4 needs its elections: or_filing_status and or_allowances (or or_exempt, or "
		                f"or_source — one of {', '.join(OR_SOURCES)}). Nothing was saved.")
	oregon = _oregon_w4(args, employee)
	today = frappe.utils.today()
	oregon.update(or_forwarding(oregon, employee, args, today))
	existing = [r for r in frappe.db.get_all(W4_FORM, filters={"employee": employee, "status": "Active"},
	                                         fields=["name", "form_kind"]) or [] if _kind(r) == "OR-W-4"]
	doc = frappe.get_doc({
		"doctype": W4_FORM, "form_kind": "OR-W-4", "employee": employee, "company": company,
		"tax_year": as_int(args, "tax_year", int(today[:4])), "status": "Active", "effective_date": today,
		**oregon, "or_signed_at": now(), "or_signed_ip": _request_ip(),
	})
	doc.flags.ignore_permissions = True
	doc.insert()
	for old in existing:
		frappe.db.set_value(W4_FORM, old["name"], {"status": "Superseded", "superseded_by": doc.name})
	emp_name = frappe.db.get_value(EMPLOYEE, employee, "employee_name") or employee
	forward = bool(oregon.get("or_forward_required"))
	return ToolResult(
		data={"name": doc.name, "form": "OR-W-4", "employee": employee, "employee_name": emp_name,
		      "or_source": oregon.get("or_source"), "or_filing_status": oregon.get("or_filing_status"),
		      "or_allowances": oregon.get("or_allowances"), "or_exempt": bool(oregon.get("or_exempt")),
		      "warnings": ([] if not oregon.get("or_exempt") or oregon.get("or_federally_exempt") else [
		          "exempt, but whether the employee is also exempt federally is not recorded (or_federally_exempt) — "
		          "an Oregon-only exemption is withheld at 8% (Publication 150-211-602)"]) + ([] if not oregon.get(
		          "or_exempt") or oregon.get("or_exemption_code") else ["exempt without a line 4a exemption code — "
		                                                                "the form is incomplete"]),
		      "esignature": esignature_status(),
		      "superseded": [r["name"] for r in existing], "send_to_dor": forward,
		      "send_to_dor_reasons": oregon.get("or_forward_reasons"), "send_by": oregon.get("or_forward_due"),
		      "signature": "collect it with collect_form_signature doctype='W-4 Form' field='or_signature'"},
		summary=f"OR-W-4 recorded for {emp_name}"
		+ (f" — send a copy to the Oregon DOR by {oregon.get('or_forward_due')}" if forward else ""),
	)


def _oregon_w4(args: dict, employee: str) -> dict:
	"""v0.291.0. The employee's Form OR-W-4 — Oregon withholds by it, and at a flat 8% without one (HB 2119). Given
	when any or_* argument is passed; otherwise carried from the employee's current W-4 so a federal change never
	drops it."""
	if not compat.has_field(W4_FORM, "or_w4_on_file"):
		return {}
	keys = OR_KEYS
	extra = tuple(k for k in ("or_exemption_code", "or_redetermination", "or_source", "or_federally_exempt")
	              if compat.has_field(W4_FORM, k))
	if not any(key in args for key in keys + extra):
		current = oregon_row(employee)
		return {k: v for k, v in current.items() if k in keys + extra and v not in (None, "")}
	status = as_str(args, "or_filing_status") or "Single"
	if status not in OR_STATUSES:
		raise ToolError(f"or_filing_status is one of: {', '.join(OR_STATUSES)}. Nothing was saved.")
	allowances = as_int(args, "or_allowances", 0)
	if allowances < 0:
		raise ToolError("or_allowances can't be negative. Nothing was saved.")
	source = as_str(args, "or_source") or ""
	if source and source not in OR_SOURCES:
		raise ToolError(f"or_source is one of: {', '.join(OR_SOURCES)}. Nothing was saved.")
	on_file = args.get("or_w4_on_file", source in ("", "OR-W-4")) not in (False, 0, "0", "false")
	values = {"or_w4_on_file": 1 if on_file else 0,
	          "or_filing_status": status, "or_allowances": allowances,
	          "or_additional_withholding": max(as_float(args, "or_additional_withholding", 0.0), 0.0),
	          "or_exempt": 1 if args.get("or_exempt") else 0}
	code = (as_str(args, "or_exemption_code") or "").strip().upper()
	if code:
		from .. import or_w4_pdf

		if code not in or_w4_pdf.EXEMPTION_CODES:
			raise ToolError(f"or_exemption_code is one letter from the OR-W-4 exemption chart: "
			                f"{', '.join(f'{k} ({v})' for k, v in or_w4_pdf.EXEMPTION_CODES.items())}. Nothing was saved.")
	federal = as_str(args, "or_federally_exempt") or ""
	if federal and federal.lower() in ("1", "true", "yes"):
		federal = "Yes"
	elif federal and federal.lower() in ("0", "false", "no"):
		federal = "No"
	if extra:
		values.update({"or_exemption_code": code or None,
		               "or_redetermination": 1 if args.get("or_redetermination") else 0,
		               "or_source": source or ("OR-W-4" if on_file else None)})
		if "or_federally_exempt" in extra:
			values["or_federally_exempt"] = federal or None
	return values


def _resolve_w4(args: dict) -> str:
	"""One W-4 Form docname, from a docname or from whoever it belongs to.

	THE DOCNAME IS TRIED FIRST AND `name` IS TRIED AS BOTH, the same reading
	`i9._resolve_form` takes and for the same reason: every other tool in this
	module is asked about a person, and this one is asked about a form.
	`W4-2026-0001` is what a Desk user has in front of them.
	"""
	explicit = as_str(args, "w4_form") or as_str(args, "form")
	if explicit:
		if frappe.db.exists(W4_FORM, explicit):
			return explicit
		raise ToolError(
			f"no W-4 Form called {explicit!r} on this site. list_w4_forms has the register; "
			f"pass employee= to look one up by the person it belongs to instead."
		)
	docname = as_str(args, "name")
	if docname and frappe.db.exists(W4_FORM, docname):
		return docname

	employee = _resolve_employee(args)
	tax_year = as_int(args, "tax_year")
	filters = {"employee": employee, "status": "Active"}
	if tax_year:
		filters["tax_year"] = tax_year
	# v0.294.0. By employee, the FEDERAL W-4 — an OR-W-4 filed on its own is reached with form=or_w4.
	rows = frappe.db.get_all(W4_FORM, filters=filters, fields=list(compat.existing_fields(W4_FORM, ("name", "form_kind"))),
	                         order_by="tax_year desc, effective_date desc", limit=50) or []
	found = next((r["name"] for r in rows if _kind(r) == "W-4"), None)
	if not found:
		raise ToolError(
			f"no active W-4 Form for employee {employee!r}"
			+ (f" in tax year {tax_year}" if tax_year else "")
			+ ". Submit one with submit_w4, or name a superseded one by docname."
		)
	return str(found)


def _employee_for(employee: str) -> dict:
	"""Step 1(a): the employee's name and address, structured.

	READ OFF THEIR I-9 FIRST, and that is the interesting half. Form W-4 asks
	for a street line and a separate city/state/ZIP line; ERPNext's Employee
	keeps `current_address` as one free-text blob, and splitting a blob into two
	boxes on a federal form is guesswork. THE STRUCTURED ADDRESS ALREADY EXISTS
	on the I-9 Form — `address_street`, `address_city`, `address_state`,
	`address_zip`, collected in Section 1 by the same worker — so it is read
	from there and the Employee record is the fallback.

	That is the same call this app makes everywhere: one place per fact. A
	second address on the W-4 doctype would be a second thing to keep current
	and a second thing to be wrong.

	NEVER RAISES. An employee whose name will not read is an employee whose
	boxes print empty for a pen, which is a worse form and not a failed render.
	"""
	row = (
		frappe.db.get_value(
			EMPLOYEE,
			employee,
			["first_name", "middle_name", "last_name", "employee_name", "current_address"],
			as_dict=True,
		)
		or {}
	)

	person = {
		"first_name": str(row.get("first_name") or "").strip(),
		"middle_name": str(row.get("middle_name") or "").strip(),
		"last_name": str(row.get("last_name") or "").strip(),
		"address": str(row.get("current_address") or "").strip(),
		"city": "",
		"state": "",
		"zip": "",
	}

	try:
		i9 = (
			frappe.db.get_value(
				"I-9 Form",
				{"employee": employee},
				[
					"legal_first_name",
					"legal_middle_name",
					"legal_last_name",
					"address_street",
					"address_city",
					"address_state",
					"address_zip",
				],
				as_dict=True,
			)
			or {}
		)
	except Exception:  # pragma: no cover - a site without the I-9 doctype
		i9 = {}

	for key, column in (
		("first_name", "legal_first_name"),
		("middle_name", "legal_middle_name"),
		("last_name", "legal_last_name"),
	):
		value = str(i9.get(column) or "").strip()
		if value:
			person[key] = value
	street = str(i9.get("address_street") or "").strip()
	if street:
		person["address"] = street
		person["city"] = str(i9.get("address_city") or "").strip()
		person["state"] = str(i9.get("address_state") or "").strip()
		person["zip"] = str(i9.get("address_zip") or "").strip()

	if not (person["first_name"] or person["last_name"]):
		# A site that fills only `employee_name` still gets a name on the page.
		whole = str(row.get("employee_name") or "").strip()
		if whole:
			parts = whole.rsplit(" ", 1)
			person["first_name"] = parts[0] if len(parts) == 2 else ""
			person["last_name"] = parts[-1]
	return person


def _employer_for(company: str) -> dict:
	"""The Employers Only block: the employer's name, address and EIN.

	READ THROUGH `i9._employer_block` RATHER THAN REIMPLEMENTED. That function
	already answers exactly this question — I-9 Settings' `business_legal_name`,
	`business_address` and `business_ein` first, the Company and its linked
	Address second — and it is the same employer identifying itself on both
	federal forms. A farm that put `FAFO Farms LLC` in I-9 Settings so its I-9s
	carry the name on the EIN should not have to put it somewhere else again for
	its W-4s.

	Imported inside the function rather than at module scope: `tools/i9` is a
	heavier module than this one needs at import time, and a site without the
	I-9 doctype still renders a W-4 — with an empty employer block, which is
	three boxes somebody writes in.
	"""
	try:
		from . import i9 as i9_tools

		return i9_tools._employer_block(company)
	except Exception:  # pragma: no cover - a site without the I-9 doctype
		return {"name": str(company or "").strip(), "address": "", "ein": ""}


def _signature_capture(record: dict) -> bytes | None:
	"""The employee's signature image off the record, as bytes, for `fill_w4_pdf`.

	Site read on this side of the line because `w4_pdf` is a pure function, and
	a capture that cannot be read costs the SIGNATURE rather than the render —
	the same posture, and the same reasoning, as `i9._signature_captures`.
	"""
	url = str(record.get("signature") or "").strip()
	if not url:
		return None
	try:
		docname = frappe.db.get_value("File", {"file_url": url}, "name")
		if not docname:
			return None
		return files.read_file_bytes(str(docname))
	except Exception:
		return None


def render_w4_pdf(args: dict) -> ToolResult:
	"""Fill the IRS Form W-4 from this record and attach it to the record.

	THE PAGE IS THE GOVERNMENT'S. `w4_pdf.py` opens the IRS fillable PDF this
	app ships, writes the collected values into its own named fields, and hands
	back a copy — so what comes out is Form W-4 with the boxes filled, not a
	reproduction of it and not a field dump. See that module for what it
	deliberately leaves blank: the SSN, the exempt tick, and the worksheets the
	employee keeps.

	THE EMPLOYER BLOCK IS RESOLVED HERE, NOT STORED. `_employer_for` reads the
	name, address and EIN off I-9 Settings or the Company, and the first date of
	employment off `Employee.date_of_joining` — so a form rendered today carries
	today's registered address rather than whatever was true when the employee
	filled the W-4 in.

	A SNAPSHOT, NOT A VIEW, exactly as `render_i9_pdf` is. The attached PDF is
	the record as it was when the call was made, so a second render REFUSES
	unless `overwrite=true`: the likeliest thing in that field is the copy
	somebody already printed and had signed.

	RENDERING MOVES NO STATUS. A W-4 is retained by the employer rather than
	filed with anybody, and an Active W-4 stays Active.

	THE TAX YEAR IS REPORTED RATHER THAN ENFORCED. The shipped template prints
	one year in its masthead and the IRS revises the form annually; a 2025
	election rendered on the 2026 page is a readable record, and no form at all
	is not. `template_tax_year_matches` in the result is how a caller knows.
	"""
	if _is_or_w4(args):
		return _render_or_w4(args)
	w4_pdf.require()
	name = _resolve_w4(args)
	row = frappe.db.get_value(W4_FORM, name, _w4_fields(), as_dict=True)
	if not row:  # pragma: no cover - resolved a moment ago
		raise ToolError(f"no W-4 Form called {name!r} on this site.")

	overwrite = as_bool(args, "overwrite", False)
	existing = str(row.get("generated_pdf") or "").strip()
	if existing and not overwrite:
		raise ToolError(
			f"W-4 {name} already has a rendered PDF at {existing}. The likeliest thing in "
			f"that field is the copy somebody printed and had signed. Pass overwrite=true to "
			f"render a fresh page and repoint the field; the existing File stays attached to "
			f"the record either way. Nothing was changed."
		)

	employee = str(row.get("employee") or "")
	record = {key: value for key, value in row.items()}
	record["name"] = name
	record["first_date_of_employment"] = frappe.db.get_value(EMPLOYEE, employee, "date_of_joining")
	# Read here rather than added to `_w4_fields`, for the reason
	# `render_i9_pdf` gives at the same line: that list is what `get_w4`
	# answers with, and the URL of somebody's ink is not something a reader of
	# the record needs. `_signature_capture` below looks for exactly this key
	# and, until v0.57.1, was handed a dict that never carried it — so the
	# rendered W-4 had an empty signature line on every site, on a form whose
	# employee signature is the only thing that makes it a valid election.
	record["signature"] = frappe.db.get_value(W4_FORM, name, "signature")

	person = _employee_for(employee)
	employer = _employer_for(str(row.get("company") or ""))

	pdf = w4_pdf.fill_w4_pdf(record, person, employer, signature=_signature_capture(record))
	file_name = w4_pdf.file_name_for(record, person)
	attachment = artifacts.attach_bytes(W4_FORM, name, file_name, pdf, field="generated_pdf")
	frappe.db.set_value(W4_FORM, name, "generated_pdf_on", now(), update_modified=False)

	tax_year = int(row.get("tax_year") or 0)
	data = {
		"name": name,
		"employee": employee,
		"employee_name": row.get("employee_name"),
		"tax_year": tax_year,
		"status": row.get("status"),
		"edition": w4_pdf.EDITION,
		"template_tax_year": w4_pdf.TEMPLATE_TAX_YEAR,
		"template_tax_year_matches": tax_year == w4_pdf.TEMPLATE_TAX_YEAR,
		"file_name": file_name,
		"file_url": attachment.get("file_url"),
		"bytes": len(pdf),
		"replaced": existing or None,
		"employer": employer,
		"first_date_of_employment": (
			str(record["first_date_of_employment"]) if record["first_date_of_employment"] else None
		),
		"employer_block_truncated": w4_pdf.employer_block_overflows(employer),
		"incomplete": _incomplete_w4_boxes(record, person, employer),
		"note": _W4_RENDER_NOTE,
	}
	summary = f"W-4 {name} rendered onto the IRS form as {file_name} ({len(pdf):,} bytes) and attached" + (
		f", replacing {existing}" if existing else ""
	)
	if not data["template_tax_year_matches"] and tax_year:
		summary += (
			f" — the election is for {tax_year} and the shipped template is the "
			f"{w4_pdf.TEMPLATE_TAX_YEAR} edition"
		)
	if data["incomplete"]:
		summary += f" — {len(data['incomplete'])} box(es) left blank for a pen"
	log_access(name, "render_w4_pdf")
	return ToolResult(data=data, summary=summary)


def _or_w4_name(args: dict) -> str:
	"""The OR-W-4 row a render is about: a named W-4 Form, else the employee's current Oregon elections."""
	explicit = as_str(args, "w4_form") or as_str(args, "name")
	if explicit and frappe.db.exists(W4_FORM, explicit):
		return explicit
	employee = _resolve_employee(args)
	row = oregon_row(employee)
	if not row:
		raise ToolError(f"{employee} has no OR-W-4 on file — record one with submit_w4 form=or_w4. Nothing was changed.")
	return str(row["name"])


def esignature_status(as_of: str = "") -> dict:
	"""v0.295.0. Whether the Oregon DOR has confirmed it accepts e-signed OR-W-4s — a policy value Tim/the CPA sets
	(payroll setting `oregon_withholding` → `esignature`), shown wherever an OR-W-4 is signed or rendered."""
	from .. import payroll_settings

	value = esignature_rule(as_of)
	confirmed = bool(value.get("confirmed"))
	return {"confirmed": confirmed, "status": "confirmed by the Oregon DOR" if confirmed else (
		value.get("status") or "awaiting DOR written confirmation"),
		"standard": value.get("standard"), "state_law": value.get("state_law"),
		"requirements": {"authenticated_signer": bool(value.get("require_verified_signer")),
		                 "verified_methods": value.get("verified_methods"),
		                 "perjury_statement_before_signature": True,
		                 "signature_is_final_entry": bool(value.get("lock_after_signature")),
		                 "access_log": bool(value.get("access_log")),
		                 "hard_copy": value.get("hard_copy")},
		"note": value.get("note") or (
		"Oregon DOR acceptance of electronically signed OR-W-4s is awaiting the DOR's written confirmation. The "
		"e-signature is built to the IRS standard (26 CFR 31.3402(f)(5)-1(c)); keep a wet-signed copy until the DOR "
		"confirms (CPA/DOR item for Tim)." if not confirmed else "Confirmed by the Oregon DOR.")}


def esignature_rule(as_of: str = "") -> dict:
	"""v0.308.0. The e-signature policy for W-4 Forms (`oregon_withholding.esignature`), over the built-in defaults."""
	from .. import payroll_settings
	from ..state_withholding import OR_WITHHOLDING_2026

	return {**OR_WITHHOLDING_2026["esignature"],
	        **dict(payroll_settings.oregon_withholding(as_of).get("esignature") or {})}


def log_access(name: str, method: str) -> None:
	"""v0.308.0. A read or render of a SIGNED W-4 Form, in Frappe's own Access Log (26 CFR 31.3402(f)(5)-1(c)).
	Best effort — never fatal."""
	try:
		if not esignature_rule().get("access_log") or not compat.doctype_exists("Access Log"):
			return
		signed = frappe.db.get_value(W4_FORM, name, list(compat.existing_fields(W4_FORM, ("signature", "or_signature"))),
		                             as_dict=True) or {}
		if not any(signed.values()):
			return
		from .. import security

		doc = frappe.get_doc({"doctype": "Access Log", "export_from": W4_FORM, "reference_document": name,
		                      "method": method, "user": security.caller_identity() or frappe.session.user,
		                      "file_type": "PDF" if "render" in method else ""})
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
	except Exception:  # pragma: no cover - the log never takes the read down
		pass


def _render_or_w4(args: dict) -> ToolResult:
	"""v0.295.0. Fill the DOR's own Form OR-W-4 (150-101-402) from the record and attach it to `or_generated_pdf`."""
	from .. import or_w4_pdf

	or_w4_pdf.require()
	name = _or_w4_name(args)
	row = frappe.db.get_value(W4_FORM, name, _w4_fields(), as_dict=True)
	existing = str(frappe.db.get_value(W4_FORM, name, "or_generated_pdf") or "").strip() if compat.has_field(
		W4_FORM, "or_generated_pdf") else ""
	if existing and not as_bool(args, "overwrite", False):
		raise ToolError(f"{name} already has a rendered OR-W-4 at {existing} — likely the copy somebody printed and "
		                "had signed. Pass overwrite=true to render a fresh page. Nothing was changed.")
	employee = str(row.get("employee") or "")
	record = {**dict(row), "name": name}
	person = _employee_for(employee)
	employer = _employer_for(str(row.get("company") or ""))
	ink = _signature_capture({"signature": frappe.db.get_value(W4_FORM, name, "or_signature")}) if compat.has_field(
		W4_FORM, "or_signature") else None
	pdf, notes = or_w4_pdf.fill_or_w4_pdf(record, person, employer, signature=ink)
	file_name = or_w4_pdf.file_name_for(record, person)
	field = "or_generated_pdf" if compat.has_field(W4_FORM, "or_generated_pdf") else None
	attachment = artifacts.attach_bytes(W4_FORM, name, file_name, pdf, field=field)
	if compat.has_field(W4_FORM, "or_generated_pdf_on"):
		frappe.db.set_value(W4_FORM, name, "or_generated_pdf_on", now(), update_modified=False)
	tax_year = int(row.get("tax_year") or 0)
	esign = esignature_status()
	data = {"name": name, "form": "OR-W-4", "employee": employee, "employee_name": row.get("employee_name"),
	        "edition": or_w4_pdf.EDITION, "source": or_w4_pdf.SOURCE_URL,
	        "template_tax_year_matches": tax_year in (0, or_w4_pdf.TEMPLATE_TAX_YEAR),
	        "file_name": file_name, "file_url": attachment.get("file_url"), "bytes": len(pdf),
	        "replaced": existing or None, "signed": bool(ink), "notes": notes, "esignature": esign,
	        "note": "The SSN boxes are left for the employee. The employer block is drawn from I-9 Settings / the "
	                "Company. Rendering moved no status."}
	summary = f"OR-W-4 {name} rendered onto the DOR's form as {file_name} ({len(pdf):,} bytes) and attached"
	if ink and not esign["confirmed"]:
		summary += " — e-signature acceptance by the Oregon DOR is unconfirmed; keep a wet-signed copy"
	if notes:
		summary += f" — {len(notes)} note(s)"
	log_access(name, "render_w4_pdf form=or_w4")
	return ToolResult(data=data, summary=summary)


#: Said on every render, because a filled federal form is the thing somebody is
#: most likely to mistake for a completed one.
_W4_RENDER_NOTE = (
	"The employee's signature and date lines are blank because the IRS form has NO signature "
	"field at all — they are printed rules, not boxes. Step 1(b), the Social Security number, "
	"is left for the employee: a W-4 is completed by the employee and the number is theirs to "
	"write. Print the page, have them sign it, and file it with the employee's records. "
	"Rendering moved no status — the W-4 is still whatever it was."
)

#: Which of the boxes a complete W-4 needs are empty on this record, and what
#: each is called on the form. Reported rather than refused: a W-4 rendered to
#: hand somebody a page with their own details already on it is a real and
#: useful thing to do.
_W4_REQUIRED_BOXES = (("filing_status", "Step 1(c): filing status"),)


def _incomplete_w4_boxes(record: dict, person: dict, employer: dict) -> list[str]:
	"""The named boxes a printed copy of this record will have nothing in."""
	missing = [label for column, label in _W4_REQUIRED_BOXES if not str(record.get(column) or "").strip()]
	if not (person.get("first_name") or person.get("last_name")):
		missing.append("Step 1(a): employee name")
	if not person.get("address"):
		missing.append("Step 1(a): employee address")
	if not employer.get("name"):
		missing.append("Employers Only: employer name and address")
	if not employer.get("ein"):
		missing.append("Employers Only: employer identification number (EIN)")
	if not record.get("first_date_of_employment"):
		missing.append("Employers Only: first date of employment")
	return missing


def update_fica_config(args: dict) -> ToolResult:
	"""Update FICA rates for a new tax year."""
	try:
		doc = frappe.get_doc(FICA_CONFIG)
	except Exception:
		raise ToolError("FICA Configuration does not exist yet. Run bench migrate.") from None

	fields = [
		"tax_year",
		"social_security_rate_employee",
		"social_security_rate_employer",
		"social_security_wage_base",
		"medicare_rate_employee",
		"medicare_rate_employer",
		"additional_medicare_threshold",
		"additional_medicare_rate",
		"futa_rate",
		"futa_wage_base",
		"futa_state_credit_max",
	]
	updated = []
	for field in fields:
		val = args.get(field)
		if val is not None:
			if field == "tax_year":
				setattr(doc, field, int(val))
			else:
				setattr(doc, field, float(val))
			updated.append(field)

	if not updated:
		raise ToolError("no fields to update. Pass at least one FICA field.")

	doc.flags.ignore_permissions = True
	doc.save()

	return ToolResult(
		data={"updated_fields": updated, "tax_year": int(doc.tax_year or 0)},
		summary=f"FICA config updated: {', '.join(updated)}",
	)


def import_federal_tax_table(args: dict) -> ToolResult:
	"""Bulk import withholding brackets for a new tax year."""
	tax_year = as_int(args, "tax_year")
	if not tax_year:
		raise ToolError("tax_year is required.")
	brackets = args.get("brackets")
	if not brackets:
		raise ToolError("brackets is required — a list of bracket objects.")

	created = 0
	for bracket in brackets:
		if not isinstance(bracket, dict):
			continue
		doc = frappe.get_doc(
			{
				"doctype": FEDERAL_TAX_TABLE,
				"tax_year": tax_year,
				"filing_status": bracket.get("filing_status", ""),
				"payroll_period": bracket.get("payroll_period", ""),
				"bracket_floor": float(bracket.get("bracket_floor", 0)),
				"bracket_ceiling": float(bracket["bracket_ceiling"])
				if bracket.get("bracket_ceiling")
				else None,
				"base_tax": float(bracket.get("base_tax", 0)),
				"marginal_rate": float(bracket.get("marginal_rate", 0)),
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		created += 1

	return ToolResult(
		data={"tax_year": tax_year, "created": created},
		summary=f"Imported {created} bracket(s) for tax year {tax_year}",
	)


# ── internal helpers ─────────────────────────────────────────────────────


def _load_w4_data(employee: str, tax_year: int | None = None) -> tuple[dict, int]:
	"""Load the active W-4 for an employee and return (w4_data, tax_year)."""
	filters = {"employee": employee, "status": "Active"}
	if tax_year:
		filters["tax_year"] = tax_year

	name = frappe.db.get_value(
		W4_FORM,
		filters,
		"name",
		order_by="tax_year desc, effective_date desc",
	)
	if not name:
		raise ToolError(f"no active W-4 for employee {employee!r}. Submit one first.")

	row = frappe.db.get_value(W4_FORM, name, _w4_fields(), as_dict=True)
	filing_status = row.get("filing_status", "Single or Married Filing Separately")
	table_status = FILING_STATUS_MAP.get(filing_status, "Single")

	w4_data = {
		"filing_status": table_status,
		"multiple_jobs": bool(int(row.get("multiple_jobs") or 0)),
		"additional_income_from_other_jobs": float(row.get("additional_income_from_other_jobs") or 0),
		"dependents_under_17_count": int(row.get("dependents_under_17_count") or 0),
		"other_dependents_count": int(row.get("other_dependents_count") or 0),
		"total_dependents_credit": float(row.get("total_dependents_credit") or 0),
		"other_income": float(row.get("other_income") or 0),
		"deductions": float(row.get("deductions") or 0),
		"extra_withholding_per_period": float(row.get("extra_withholding_per_period") or 0),
	}
	return w4_data, int(row.get("tax_year") or SEED_TAX_YEAR)


def _load_fica_config() -> dict:
	"""Load FICA configuration as a plain dict."""
	try:
		doc = frappe.get_doc(FICA_CONFIG)
	except Exception:
		raise ToolError("FICA Configuration does not exist. Run bench migrate.") from None

	return {
		"social_security_rate_employee": float(doc.social_security_rate_employee or 6.2),
		"social_security_rate_employer": float(doc.social_security_rate_employer or 6.2),
		"social_security_wage_base": float(doc.social_security_wage_base or 176100),
		"medicare_rate_employee": float(doc.medicare_rate_employee or 1.45),
		"medicare_rate_employer": float(doc.medicare_rate_employer or 1.45),
		"additional_medicare_threshold": float(doc.additional_medicare_threshold or 200000),
		"additional_medicare_rate": float(doc.additional_medicare_rate or 0.9),
		"futa_rate": float(doc.futa_rate or 6.0),
		"futa_wage_base": float(doc.futa_wage_base or 7000),
		"futa_state_credit_max": float(doc.futa_state_credit_max or 5.4),
	}


def _load_tax_table(tax_year: int, filing_status: str, payroll_period: str, checkbox: bool = False) -> list[dict]:
	"""Load tax brackets from the database."""
	from .payroll import _load_federal_tax_table  # v0.291.0: one loader (Annual rows, the W-4's schedule)

	rows = _load_federal_tax_table(tax_year, filing_status, payroll_period, checkbox=checkbox)
	if not rows:
		raise ToolError(
			f"no Federal Tax Table brackets for {filing_status}, {payroll_period}, {tax_year}. "
			"Import brackets with import_federal_tax_table or run bench migrate to seed 2025 defaults."
		)
	return [dict(r) for r in rows]


def as_float(args: dict, key: str, default: float = 0.0, required: bool = False) -> float:
	"""Coerce to float, with a clear error message."""
	val = args.get(key)
	if val is None or val == "":
		if required:
			raise ToolError(f"{key} is required.")
		return default
	try:
		return float(val)
	except (TypeError, ValueError):
		raise ToolError(f"{key} must be a number, got {val!r}.") from None

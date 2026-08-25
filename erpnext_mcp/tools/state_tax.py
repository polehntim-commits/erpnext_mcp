# SPDX-License-Identifier: MIT
"""State tax tools — v0.29.0.

Oregon and Washington state withholding: configuration, brackets, and the
calculation engines that read them. The work_state field on Farm Shift is
the canonical source for which state's rules apply per shift.
"""

from __future__ import annotations

import itertools

import frappe

from ..args import as_int, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult
from ..state_withholding import (
	OR_FILING_STATUS_MAP,
	PERIODS_PER_YEAR,
	SUPPORTED_STATES,
	calculate_all_payroll_taxes,
	calculate_state_withholding,
)
from ..withholding import FILING_STATUS_MAP

STATE_TAX_CONFIG = "State Tax Configuration"
STATE_TAX_TABLE = "State Tax Table"
EMPLOYEE = "Employee"
W4_FORM = "W-4 Form"
FEDERAL_TAX_TABLE = "Federal Tax Table"
FICA_CONFIG = "FICA Configuration"
FARM_SHIFT = "Farm Shift"


def _as_float(args: dict, key: str, default: float = 0.0, required: bool = False) -> float:
	val = args.get(key)
	if val is None or val == "":
		if required:
			raise ToolError(f"{key} is required.")
		return default
	try:
		return float(val)
	except (TypeError, ValueError):
		raise ToolError(f"{key} must be a number, got {val!r}.") from None


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


def _validate_state(state: str) -> str:
	state = state.upper().strip()
	if state not in SUPPORTED_STATES:
		raise ToolError(f"state must be one of: {', '.join(SUPPORTED_STATES)}. Got {state!r}.")
	return state


# ── read-only tools ──────────────────────────────────────────────────────


def get_state_tax_config(args: dict) -> ToolResult:
	"""Current state tax configuration for a company+state+tax_year."""
	company = resolve_company(as_str(args, "company"), required=True)
	state = _validate_state(as_str(args, "state", required=True))
	tax_year = as_int(args, "tax_year")
	if not tax_year:
		raise ToolError("tax_year is required.")

	filters = {"company": company, "state": state, "tax_year": tax_year, "status": "Active"}
	name = frappe.db.get_value(STATE_TAX_CONFIG, filters, "name")
	if not name:
		raise ToolError(
			f"no active State Tax Configuration for {company}, {state}, {tax_year}. "
			"Create one with create_state_tax_config."
		)

	doc = frappe.get_doc(STATE_TAX_CONFIG, name)
	data = {
		f: (str(getattr(doc, f, None)) if getattr(doc, f, None) is not None else None)
		for f in _config_fields()
	}
	data["name"] = doc.name
	return ToolResult(data=data, summary=f"State tax config for {company}, {state}, {tax_year}")


def list_state_tax_configs(args: dict) -> ToolResult:
	"""All state tax configurations, optionally filtered by company."""
	filters = {}
	company = as_str(args, "company")
	if company:
		filters["company"] = resolve_company(company)
	state = as_str(args, "state")
	if state:
		filters["state"] = _validate_state(state)
	status = as_str(args, "status")
	if status:
		filters["status"] = status
	limit = as_int(args, "limit", 100)
	if limit and limit > 500:
		limit = 500

	rows = frappe.db.get_all(
		STATE_TAX_CONFIG,
		filters=filters,
		fields=["name", "company", "state", "tax_year", "status"],
		limit_page_length=limit,
		order_by="tax_year desc, state asc",
	)
	data = {"configs": [dict(r) for r in rows], "count": len(rows)}
	return ToolResult(data=data, summary=f"{len(rows)} state tax config(s)")


def get_state_tax_table(args: dict) -> ToolResult:
	"""State income tax brackets for a state, tax_year, and filing_status."""
	state = _validate_state(as_str(args, "state", required=True))
	tax_year = as_int(args, "tax_year")
	if not tax_year:
		raise ToolError("tax_year is required.")
	filing_status = as_str(args, "filing_status", required=True)

	rows = frappe.db.get_all(
		STATE_TAX_TABLE,
		filters={
			"state": state,
			"tax_year": tax_year,
			"filing_status": filing_status,
		},
		fields=["bracket_floor", "bracket_ceiling", "base_tax", "marginal_rate"],
		order_by="bracket_floor asc",
	)
	data = {
		"brackets": [dict(r) for r in rows],
		"count": len(rows),
		"state": state,
		"tax_year": tax_year,
		"filing_status": filing_status,
	}
	if state == "WA" and not rows:
		data["note"] = "Washington has no state income tax — no brackets expected."
	return ToolResult(
		data=data,
		summary=f"{len(rows)} bracket(s) for {state}, {filing_status}, {tax_year}",
	)


def preview_state_withholding(args: dict) -> ToolResult:
	"""Dry-run state withholding calculation for an employee."""
	employee = _resolve_employee(args)
	gross_pay = _as_float(args, "gross_pay", required=True)
	pay_frequency = as_str(args, "pay_frequency", required=True)
	if pay_frequency not in PERIODS_PER_YEAR:
		raise ToolError(f"pay_frequency must be one of: {', '.join(PERIODS_PER_YEAR)}.")
	work_state = _validate_state(as_str(args, "work_state", required=True))

	filing_status = _resolve_filing_status(employee, as_int(args, "tax_year"))
	# `is None`, not `or`: a stated `tax_year: 0` used to take the employee's default
	# year. It loads no config and no table, so it is refused rather than computed.
	tax_year = as_int(args, "tax_year")
	if tax_year is None:
		tax_year = _default_tax_year(employee)
	elif tax_year <= 0:
		raise ToolError(
			f"tax_year must be a four-digit calendar year, got {tax_year}. Omit it to take the "
			"year from the employee's active W-4. Nothing was computed."
		)
	config = _load_state_config(employee, work_state, tax_year)
	table = _load_state_table(work_state, tax_year, filing_status) if work_state == "OR" else []

	result = calculate_state_withholding(
		gross_pay,
		pay_frequency,
		work_state,
		filing_status,
		config,
		table,
	)
	result["employee"] = employee
	result["tax_year"] = tax_year
	result["gross_pay"] = gross_pay
	result["pay_frequency"] = pay_frequency

	ee_key = f"total_{work_state.lower()}_employee"
	er_key = f"total_{work_state.lower()}_employer"
	return ToolResult(
		data=result,
		summary=f"{work_state} withholding for {employee}: ${result.get(ee_key, 0)} employee, "
		f"${result.get(er_key, 0)} employer on ${gross_pay} {pay_frequency}",
	)


def preview_total_payroll_taxes(args: dict) -> ToolResult:
	"""Combined federal + state payroll tax preview."""
	employee = _resolve_employee(args)
	gross_pay = _as_float(args, "gross_pay", required=True)
	pay_frequency = as_str(args, "pay_frequency", required=True)
	if pay_frequency not in PERIODS_PER_YEAR:
		raise ToolError(f"pay_frequency must be one of: {', '.join(PERIODS_PER_YEAR)}.")
	work_state = _validate_state(as_str(args, "work_state", required=True))

	w4_data, tax_year = _load_w4_data(employee, as_int(args, "tax_year"))
	fica = _load_fica_config()
	fed_table = _load_federal_table(tax_year, w4_data["filing_status"], pay_frequency)

	filing_status = w4_data["filing_status"]
	state_config = _load_state_config(employee, work_state, tax_year)
	state_table = _load_state_table(work_state, tax_year, filing_status) if work_state == "OR" else []

	ytd_gross = _as_float(args, "ytd_gross", 0.0)
	ytd_ss = _as_float(args, "ytd_ss_withheld", 0.0)

	result = calculate_all_payroll_taxes(
		gross_pay,
		pay_frequency,
		work_state,
		filing_status,
		w4_data,
		ytd_gross,
		ytd_ss,
		fica,
		fed_table,
		state_config,
		state_table,
	)
	result["employee"] = employee
	result["tax_year"] = tax_year
	result["gross_pay"] = gross_pay
	result["pay_frequency"] = pay_frequency

	return ToolResult(
		data=result,
		summary=f"Total payroll taxes for {employee} ({work_state}): "
		f"${result['grand_total_employee']} employee, "
		f"${result['grand_total_employer']} employer on ${gross_pay}",
	)


def list_employees_by_work_state(args: dict) -> ToolResult:
	"""Employees grouped by their primary work state (from shift records)."""
	company = as_str(args, "company")
	emp_filters = {"status": "Active"}
	if company:
		emp_filters["company"] = resolve_company(company)

	employees = frappe.db.get_all(
		EMPLOYEE,
		filters=emp_filters,
		fields=["name", "employee_name", "company"],
	)

	by_state: dict = {}
	no_state = []
	for emp in employees:
		state = _employee_primary_state(emp["name"])
		if state:
			by_state.setdefault(state, []).append(dict(emp))
		else:
			no_state.append(dict(emp))

	data = {
		"by_state": by_state,
		"no_work_state": no_state,
		"total_employees": len(employees),
	}
	parts = [f"{st}: {len(emps)}" for st, emps in sorted(by_state.items())]
	if no_state:
		parts.append(f"no state: {len(no_state)}")
	return ToolResult(data=data, summary=f"Employees by work state: {', '.join(parts) or 'none'}")


# ── mutating tools ───────────────────────────────────────────────────────


def create_state_tax_config(args: dict) -> ToolResult:
	"""Create a State Tax Configuration for a company+state+tax_year."""
	company = resolve_company(as_str(args, "company"), required=True)
	state = _validate_state(as_str(args, "state", required=True))
	tax_year = as_int(args, "tax_year")
	if not tax_year:
		raise ToolError("tax_year is required.")

	existing = frappe.db.get_value(
		STATE_TAX_CONFIG,
		{"company": company, "state": state, "tax_year": tax_year, "status": "Active"},
		"name",
	)
	if existing:
		raise ToolError(
			f"an active State Tax Configuration already exists for {company}, {state}, {tax_year}: "
			f"{existing}. Use update_state_tax_config to change rates."
		)

	doc_data = {
		"doctype": STATE_TAX_CONFIG,
		"company": company,
		"state": state,
		"tax_year": tax_year,
		"status": "Active",
	}
	for field in _config_rate_fields(state):
		val = args.get(field)
		if val is not None:
			doc_data[field] = float(val) if field != "wa_cares_exempt_employees" else str(val)

	doc = frappe.get_doc(doc_data)
	doc.flags.ignore_permissions = True
	doc.insert()

	return ToolResult(
		data={"name": doc.name, "company": company, "state": state, "tax_year": tax_year},
		summary=f"State tax config created for {company}, {state}, {tax_year}",
	)


def update_state_tax_config(args: dict) -> ToolResult:
	"""Update rates on an existing State Tax Configuration."""
	company = resolve_company(as_str(args, "company"), required=True)
	state = _validate_state(as_str(args, "state", required=True))
	tax_year = as_int(args, "tax_year")
	if not tax_year:
		raise ToolError("tax_year is required.")

	name = frappe.db.get_value(
		STATE_TAX_CONFIG,
		{"company": company, "state": state, "tax_year": tax_year, "status": "Active"},
		"name",
	)
	if not name:
		raise ToolError(
			f"no active State Tax Configuration for {company}, {state}, {tax_year}. "
			"Create one first with create_state_tax_config."
		)

	doc = frappe.get_doc(STATE_TAX_CONFIG, name)
	updated = []
	for field in _config_rate_fields(state):
		val = args.get(field)
		if val is not None:
			if field == "wa_cares_exempt_employees":
				setattr(doc, field, str(val))
			else:
				setattr(doc, field, float(val))
			updated.append(field)

	if not updated:
		raise ToolError("no fields to update. Pass at least one rate field.")

	doc.flags.ignore_permissions = True
	doc.save()

	return ToolResult(
		data={"name": name, "updated_fields": updated},
		summary=f"State tax config updated for {company}, {state}, {tax_year}: {', '.join(updated)}",
	)


#: The filing statuses State Tax Table will accept, which are the Select options
#: the doctype ships. Checked here rather than left to Frappe because a Select
#: does not validate on insert — a misspelled status would store fine and then
#: never match `_load_state_table`, which filters on it exactly. The failure
#: would be an import that reported success and a withholding of zero.
TABLE_FILING_STATUSES = ("Single", "Married Filing Jointly", "Head of Household")


def import_state_tax_table(args: dict) -> ToolResult:
	"""Bulk import state income tax brackets for a tax year.

	THE WHOLE PAYLOAD IS VALIDATED BEFORE ANYTHING IS WRITTEN, and that ordering
	is the point rather than a nicety: a bracket table half-replaced is worse than
	one not replaced at all, because it withholds from real cheques and looks
	populated while doing it.

	`replace` is what makes a correction possible. Oregon revises a bracket after
	an operator has already imported the year; without it the only options were a
	second import — which used to duplicate every row silently, leaving
	`_load_state_table` to return two overlapping tables and the bracket walk to
	pick whichever sorted last — or deleting rows by hand in the Desk. With it,
	the year's rows for this state are cleared and rewritten as one unit.
	"""
	state = _validate_state(as_str(args, "state", required=True))
	tax_year = as_int(args, "tax_year")
	if not tax_year:
		raise ToolError("tax_year is required.")
	brackets = args.get("brackets")
	if not brackets:
		raise ToolError("brackets is required — a list of bracket objects.")
	if not isinstance(brackets, (list, tuple)):
		raise ToolError("brackets must be a list of bracket objects.")
	replace = bool(args.get("replace"))

	rows = [_validated_bracket(b, i, state, tax_year) for i, b in enumerate(brackets)]
	_check_bracket_coverage(rows)

	existing = frappe.db.count(STATE_TAX_TABLE, {"state": state, "tax_year": tax_year})
	if existing and not replace:
		raise ToolError(
			f"{existing} {state} bracket(s) already exist for tax year {tax_year}. Importing "
			"again would leave two overlapping tables for the same year, and the withholding "
			"calculation reads every row that matches — so the duplicate would change what is "
			"withheld without failing. Pass replace=true to clear the year and rewrite it, or "
			"import a different tax_year. Nothing was written."
		)

	deleted = 0
	if replace and existing:
		for name in frappe.db.get_all(
			STATE_TAX_TABLE,
			filters={"state": state, "tax_year": tax_year},
			pluck="name",
		):
			frappe.delete_doc(STATE_TAX_TABLE, name, force=True, ignore_permissions=True)
			deleted += 1

	created = 0
	for row in rows:
		doc = frappe.get_doc({"doctype": STATE_TAX_TABLE, **row})
		doc.flags.ignore_permissions = True
		doc.insert()
		created += 1

	statuses = sorted({r["filing_status"] for r in rows})
	summary = f"Imported {created} {state} bracket(s) for tax year {tax_year}"
	if deleted:
		summary += f", replacing {deleted}"
	return ToolResult(
		data={
			"state": state,
			"tax_year": tax_year,
			"created": created,
			"deleted": deleted,
			"replaced": bool(deleted),
			"filing_statuses": statuses,
		},
		summary=f"{summary} ({', '.join(statuses)})",
	)


# ── internal helpers ─────────────────────────────────────────────────────


def _validated_bracket(bracket, index: int, state: str, tax_year: int) -> dict:
	"""One bracket, checked and normalised, or a ToolError naming its position.

	The index is in the message because a rejected import is a list of forty rows
	somebody pasted, and "bracket 17" is the difference between a fix and a hunt.
	"""
	where = f"bracket {index + 1}"
	if not isinstance(bracket, dict):
		raise ToolError(f"{where} is not an object. Nothing was written.")

	status = str(bracket.get("filing_status") or "").strip()
	if not status:
		raise ToolError(f"{where} has no filing_status. Nothing was written.")
	if status not in TABLE_FILING_STATUSES:
		raise ToolError(
			f"{where} has filing_status {status!r}, which State Tax Table does not accept. "
			f"Use one of: {', '.join(TABLE_FILING_STATUSES)}. Nothing was written."
		)

	def _number(key: str, required: bool = False) -> float | None:
		value = bracket.get(key)
		if value is None or value == "":
			if required:
				raise ToolError(f"{where} has no {key}. Nothing was written.")
			return None
		try:
			return float(value)
		except (TypeError, ValueError):
			raise ToolError(f"{where} has a non-numeric {key}: {value!r}. Nothing was written.") from None

	floor = _number("bracket_floor", required=True)
	ceiling = _number("bracket_ceiling")
	base = _number("base_tax", required=True)
	rate = _number("marginal_rate", required=True)

	if floor < 0:
		raise ToolError(f"{where} has a negative bracket_floor ({floor}). Nothing was written.")
	if base < 0:
		raise ToolError(f"{where} has a negative base_tax ({base}). Nothing was written.")
	if rate < 0 or rate > 100:
		raise ToolError(
			f"{where} has a marginal_rate of {rate}, which is not a percentage between 0 and 100. "
			"Rates are stated as 9.9 for 9.9%, not 0.099. Nothing was written."
		)
	if ceiling is not None and ceiling <= floor:
		raise ToolError(
			f"{where} has a bracket_ceiling ({ceiling}) at or below its bracket_floor ({floor}). "
			"Leave the ceiling blank for the top bracket. Nothing was written."
		)

	return {
		"state": state,
		"tax_year": int(tax_year),
		"filing_status": status,
		"bracket_floor": floor,
		"bracket_ceiling": ceiling,
		"base_tax": base,
		"marginal_rate": rate,
	}


def _check_bracket_coverage(rows: list[dict]) -> None:
	"""Per filing status: start at zero, no gaps, no overlaps, one open top.

	`_calc_state_income_tax` walks the sorted brackets and keeps the last one
	whose floor the annual gross clears. That walk cannot report a hole — a gap
	between 10,200 and 12,000 just means everybody in it is taxed at the 10,200
	bracket, quietly and wrongly — so the shape has to be checked at import, which
	is the last moment anybody is looking.
	"""
	by_status: dict[str, list[dict]] = {}
	for row in rows:
		by_status.setdefault(row["filing_status"], []).append(row)

	for status, group in by_status.items():
		group = sorted(group, key=lambda r: r["bracket_floor"])
		if group[0]["bracket_floor"] != 0:
			raise ToolError(
				f"the {status} brackets start at {group[0]['bracket_floor']} rather than 0, so "
				"income below that has no bracket. The lowest bracket must have a bracket_floor "
				"of 0. Nothing was written."
			)

		open_top = [r for r in group if r["bracket_ceiling"] is None]
		if not open_top:
			raise ToolError(
				f"every {status} bracket has a bracket_ceiling, so income above the highest one "
				"has no bracket. Leave the top bracket's ceiling blank. Nothing was written."
			)
		if len(open_top) > 1:
			raise ToolError(
				f"{len(open_top)} {status} brackets have a blank bracket_ceiling. Only the top "
				"bracket may be open-ended. Nothing was written."
			)
		if open_top[0] is not group[-1]:
			raise ToolError(
				f"the open-ended {status} bracket starts at {open_top[0]['bracket_floor']}, but "
				f"a bracket starting at {group[-1]['bracket_floor']} sits above it. The bracket "
				"with no ceiling must be the highest. Nothing was written."
			)

		for lower, upper in itertools.pairwise(group):
			if lower["bracket_ceiling"] != upper["bracket_floor"]:
				raise ToolError(
					f"the {status} brackets do not meet: one ends at {lower['bracket_ceiling']} "
					f"and the next begins at {upper['bracket_floor']}. Each bracket's ceiling must "
					"equal the next one's floor. Nothing was written."
				)


def _config_fields() -> list[str]:
	return [
		"company",
		"state",
		"tax_year",
		"status",
		"or_income_tax_enabled",
		"or_transit_tax_rate",
		"or_paid_leave_rate",
		"or_paid_leave_employee_share",
		"or_paid_leave_employer_share",
		"or_paid_leave_small_employer",
		"or_workers_comp_rate",
		"wa_pfml_rate",
		"wa_pfml_employee_share",
		"wa_pfml_employer_share",
		"wa_pfml_wage_base",
		"wa_cares_rate",
		"wa_cares_employee_only",
		"wa_cares_exempt_employees",
		"wa_li_rate_employee",
		"wa_li_rate_employer",
		*MIN_WAGE_FIELDS,
	]


#: v0.63.0. THE WAGE FLOOR, WRITABLE ON EVERY STATE RATHER THAN ON ONE.
#:
#: Every other field in this module is state-specific by construction — a PFML
#: rate is a Washington fact and a transit tax is an Oregon one — and
#: `_config_rate_fields` branches accordingly. The minimum wage is not: every
#: state has one, so these three are appended to BOTH branches rather than
#: sorted into either.
#:
#: The two regional rates are Oregon's alone (ORS 653.025 sets three by
#: geography; RCW 49.46.020 sets one) and are still offered on a Washington row,
#: for the reason `state_min_wage_rates` ignores a zero: a column nobody fills in
#: costs nothing, and a branch that REFUSED them would have to be revisited the
#: day a third state with regional rates is added. What protects a Washington row
#: from a stray value is the lookup, which asks that state's own table for the
#: region and falls back to its standard rate when it is not there.
MIN_WAGE_FIELDS = (
	"minimum_wage",
	"minimum_wage_non_urban",
	"minimum_wage_portland_metro",
)


def _config_rate_fields(state: str) -> list[str]:
	if state == "OR":
		return [
			"or_income_tax_enabled",
			"or_transit_tax_rate",
			"or_paid_leave_rate",
			"or_paid_leave_employee_share",
			"or_paid_leave_employer_share",
			"or_paid_leave_small_employer",
			"or_workers_comp_rate",
			*MIN_WAGE_FIELDS,
		]
	elif state == "WA":
		return [
			"wa_pfml_rate",
			"wa_pfml_employee_share",
			"wa_pfml_employer_share",
			"wa_pfml_wage_base",
			"wa_cares_rate",
			"wa_cares_exempt_employees",
			"wa_li_rate_employee",
			"wa_li_rate_employer",
			*MIN_WAGE_FIELDS,
		]
	return []


def _load_state_config(employee: str, state: str, tax_year: int) -> dict:
	company = frappe.db.get_value(EMPLOYEE, employee, "company")
	if not company:
		raise ToolError(f"employee {employee!r} has no company.")

	name = frappe.db.get_value(
		STATE_TAX_CONFIG,
		{"company": company, "state": state, "tax_year": tax_year, "status": "Active"},
		"name",
	)
	if not name:
		raise ToolError(
			f"no active State Tax Configuration for {company}, {state}, {tax_year}. "
			"Create one with create_state_tax_config."
		)

	doc = frappe.get_doc(STATE_TAX_CONFIG, name)
	return {f: (getattr(doc, f, None) or 0) for f in _config_rate_fields(state)}


def _load_state_table(state: str, tax_year: int, filing_status: str) -> list[dict]:
	rows = frappe.db.get_all(
		STATE_TAX_TABLE,
		filters={"state": state, "tax_year": tax_year, "filing_status": filing_status},
		fields=["bracket_floor", "bracket_ceiling", "base_tax", "marginal_rate"],
		order_by="bracket_floor asc",
	)
	return [dict(r) for r in rows]


def _resolve_filing_status(employee: str, tax_year: int | None = None) -> str:
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
		return "Single"
	filing = frappe.db.get_value(W4_FORM, name, "filing_status") or ""
	return OR_FILING_STATUS_MAP.get(filing, "Single")


def _default_tax_year(employee: str) -> int:
	filters = {"employee": employee, "status": "Active"}
	name = frappe.db.get_value(
		W4_FORM,
		filters,
		"name",
		order_by="tax_year desc, effective_date desc",
	)
	if name:
		ty = frappe.db.get_value(W4_FORM, name, "tax_year")
		if ty:
			return int(ty)
	return 2025


def _load_w4_data(employee: str, tax_year: int | None = None) -> tuple[dict, int]:
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

	fields = [
		"tax_year",
		"filing_status",
		"multiple_jobs",
		"additional_income_from_other_jobs",
		"dependents_under_17_count",
		"other_dependents_count",
		"total_dependents_credit",
		"other_income",
		"deductions",
		"extra_withholding_per_period",
	]
	row = frappe.db.get_value(W4_FORM, name, fields, as_dict=True)
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
	return w4_data, int(row.get("tax_year") or 2025)


def _load_fica_config() -> dict:
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


def _load_federal_table(tax_year: int, filing_status: str, payroll_period: str) -> list[dict]:
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
	if not rows:
		raise ToolError(
			f"no Federal Tax Table brackets for {filing_status}, {payroll_period}, {tax_year}. "
			"Import brackets with import_federal_tax_table or run bench migrate."
		)
	return [dict(r) for r in rows]


def _employee_primary_state(employee: str) -> str | None:
	"""Most recent shift's work_state for an employee."""
	from ..compat import doctype_exists

	if not doctype_exists(FARM_SHIFT):
		return None
	crew_dt = "Farm Shift Crew Member"
	if not doctype_exists(crew_dt):
		return None
	row = frappe.db.sql(
		"""
        SELECT fs.work_state
        FROM `tabFarm Shift` fs
        JOIN `tabFarm Shift Crew Member` cm ON cm.parent = fs.name
        WHERE cm.employee = %s AND fs.work_state IS NOT NULL AND fs.work_state != ''
        ORDER BY fs.start_datetime DESC
        LIMIT 1
        """,
		(employee,),
		as_dict=True,
	)
	return row[0]["work_state"] if row else None

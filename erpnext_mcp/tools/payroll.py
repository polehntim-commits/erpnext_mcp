# SPDX-License-Identifier: MIT
"""Salary structure + payroll tools.

v0.30.0. Links the calc engine to the MCP surface. Read tools are dry-run
previews and lookups; mutating tools create salary structures and payroll
entries.

v0.35.0. THE HOURS NOW COME OFF THE SHIFT REGISTER. `_load_shifts` used to
return the crew-wide span for every worker with zero overtime and zero piece
units, so a payroll run was a shell around numbers somebody keyed in by hand.
It now goes through `payroll_integration`, which is pure and testable, and
every tool here — including the three v0.30.0 ones — reads the same
per-worker spans, the same weekly overtime split and the same piece counts.

THE THREE NEW TOOLS ARE COMPANY-WIDE. `preview_payroll_for_period` and
`run_payroll_for_period` do for a whole company what `preview_payroll` does for
one person, and `get_employee_timesheet_summary` is the hours WITHOUT the
money — the read a foreman opens when somebody asks why their cheque is what it
is, and the one that does not need the payroll switches turned on to answer.

WHERE THE PIECE UNITS COME FROM IS A PROPERTY OF THE SITE, and every result
says which source answered. The BucketLog bridge is written by whichever version
of the iPad app is in the field this season, and Farm Task Assignment may or may
not carry a count; `_load_piece_rows` looks for both, uses what is there, and
NAMES what is not rather than reporting a piece-rate worker's day as zero
buckets without comment.
"""

from __future__ import annotations

import json

import frappe
from frappe.utils import today

from .. import breaks as breaks_mod
from .. import bucket_bridge, compat, pay_stub_pdf, payroll_deductions, payroll_integration, wage_defaults
from ..args import as_date, as_int, as_str, resolve_company
from ..errors import ToolError
from ..payroll_calc import (
	MIN_WAGE_REGION_KEYS,
	calculate_full_payroll,
	normalise_min_wage_region,
	state_min_wage_rates,
)
from ..result import ToolResult
from ..state_withholding import SUPPORTED_STATES
from ..withholding import FILING_STATUS_MAP, PERIODS_PER_YEAR
from . import artifacts, wagedefaults

SALARY_STRUCTURE = "Farm Salary Structure"
PAYROLL_ENTRY = "Farm Payroll Entry"
PAYROLL_SLIP = "Farm Payroll Slip"
PAYROLL_DEDUCTION = "Farm Payroll Deduction"

#: What a payroll run needs off a deduction row. Narrower than the register's
#: own field list on purpose — `notes` and the audit columns are not arithmetic
#: and a run that carried them would put them in every slip's stored JSON.
_DEDUCTION_FIELDS = (
	"name",
	"employee",
	"company",
	"deduction_type",
	"deduction_category",
	"status",
	"priority",
	"amount_type",
	"amount",
	"basis",
	"max_per_period",
	"pre_tax",
	"fica_exempt",
	"supports_other_dependents",
	"arrears_over_12_weeks",
	"exempt_amount",
	"effective_from",
	"effective_to",
	"reference",
	"label",
)
EMPLOYEE = "Employee"
W4_FORM = "W-4 Form"
FICA_CONFIG = "FICA Configuration"
FEDERAL_TAX_TABLE = "Federal Tax Table"
STATE_TAX_CONFIG = "State Tax Configuration"
STATE_TAX_TABLE = "State Tax Table"
FARM_SHIFT = "Farm Shift"
CREW_MEMBER = "Farm Shift Crew Member"
FARM_TASK_ASSIGNMENT = "Farm Task Assignment"

#: The BucketLog bridge. Optional, external, and written by whichever version of
#: the iPad app is in the field this season — so every column it might carry is
#: looked for rather than assumed. `compliance_fields.py` says the same thing at
#: length about the five traceability columns it grafts on.
BUCKET_LOG = "Bucket Log Entry"

#: Most shifts one payroll run will read. A fortnight of a large crew is a few
#: hundred; five thousand is a date range somebody typed wrong, and paying out
#: against it silently would be worse than refusing.
SHIFT_CAP = 5000


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


def _num(value, default: float = 0.0) -> float:
	"""A float off a row, with a default where the column is absent or empty.

	Distinct from `_as_float` above, which coerces a tool ARGUMENT and refuses
	what it cannot read. A database column that came back None is not a caller
	mistake and should not raise into a payroll run.
	"""
	if value is None or value == "":
		return default
	try:
		return float(value)
	except (TypeError, ValueError):
		return default


def _structure_activity_fields() -> list:
	"""`piecework_activity` where the site has migrated to v0.61.0, else nothing.

	The same posture every optional column in this module gets: a site that has
	not run `bench migrate` yet reads exactly as it did before, and the
	company-wide fallback is simply unreachable there rather than an exception on
	payday.
	"""
	return compat.existing_fields(SALARY_STRUCTURE, ("piecework_activity",))


#: The region key → the Select option as the doctype spells it. The inverse of
#: `payroll_calc.MIN_WAGE_REGION_KEYS`, kept here rather than there because the
#: label is a fact about the doctype and the key is a fact about the rate table.
_REGION_LABELS = {
	"standard": "Standard",
	"non_urban": "Non-Urban",
	"portland_metro": "Portland Metro",
}


def _structure_region_fields() -> list:
	"""`min_wage_region` where the site has migrated to v0.63.0, else nothing.

	The same posture `_structure_activity_fields` takes, and the same reason: a
	site that has not run `bench migrate` reads exactly as it did before, and every
	worker on it is owed the standard rate — which is what `normalise_min_wage_region`
	returns for a value that is not there.
	"""
	return compat.existing_fields(SALARY_STRUCTURE, ("min_wage_region",))


def _min_wage_regions(structure: dict, rates: dict) -> dict:
	"""`{"OR": "portland_metro", "WA": "portland_metro"}` for one salary structure.

	THE SAME REGION AGAINST EVERY STATE, WHICH LOOKS WRONG AND IS NOT. The engine
	asks `min_wage_regions.get(state, "standard")` per state and then
	`applicable_minimum_wage` looks that region up in THAT STATE's own rates,
	falling back to its standard where the key is absent. Washington defines one
	rate, so a Portland-metro worker who spent a week over the river is owed
	Washington's standard $16.66 — which is exactly what naming the region against
	both states produces, with no per-state table on the structure and no branch
	here that would have to know which states have regions.

	A structure with no region — every one on a site that has not migrated — gets
	`standard` everywhere, which is the pre-v0.63.0 behaviour unchanged.
	"""
	region = normalise_min_wage_region(structure.get("min_wage_region"))
	if region == "standard":
		return {}
	return {state: region for state in (rates or {})}


def _resolve_piece_rates(structures: dict, company: str, on_date: str) -> tuple[dict, list]:
	"""Fill `base_rate` from the company Piecework Rate table where a structure names none.

	THE LOOKUP ORDER LIVES IN `wage_defaults.resolve_piece_rate` and this is only
	its database half — the structures that need an answer, the company's rate
	rows, and the per-employee catch. See that module's docstring for why the
	structure's own rate wins and why a miss is an error rather than a zero.

	`on_date` is the pay period's END, so a period straddling a rate change is
	paid at the rate in force when it closed.

	Mutates the structure dicts in place and returns
	`(resolved_by_employee, could_not_resolve)`. A miss is RETURNED rather than
	raised because the caller decides what to do with it: a single-employee
	preview has nobody else to hold up and re-raises, a company run reports it
	and pays everybody else — the posture `run_payroll_for_period` has taken
	towards a missing salary structure since v0.35.0.
	"""
	needing = [
		structure
		for structure in (structures or {}).values()
		if str(structure.get("pay_type") or "") == "Piece Rate" and _num(structure.get("base_rate")) <= 0
	]
	if not needing:
		return {}, []

	rates = wagedefaults.rates_for_company(company)
	resolved: dict = {}
	missing: list = []
	for structure in needing:
		candidate = dict(structure)
		candidate.setdefault("company", company)
		try:
			answer = wage_defaults.resolve_piece_rate(candidate, rates, on_date)
		except ToolError as exc:
			missing.append(
				{
					"employee": structure.get("employee"),
					"employee_name": structure.get("employee_name") or structure.get("employee"),
					"salary_structure": structure.get("name"),
					"piecework_activity": (
						wage_defaults.normalize_activity(structure.get("piecework_activity")) or None
					),
					"reason": str(exc),
				}
			)
			continue
		structure["base_rate"] = answer["rate"]
		structure["piece_rate_source"] = answer["source"]
		structure["piecework_rate"] = answer["piecework_rate"]
		structure["piecework_activity"] = answer["activity"] or structure.get("piecework_activity")
		if answer["source"] == wage_defaults.SOURCE_COMPANY:
			resolved[str(structure.get("employee") or "")] = {
				"employee": structure.get("employee"),
				"employee_name": structure.get("employee_name") or structure.get("employee"),
				"activity": answer["activity"],
				"rate": answer["rate"],
				"piecework_rate": answer["piecework_rate"],
			}
	return resolved, missing


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


# ── Read tools ────────────────────────────────────────────────────────────


def get_salary_structure(args: dict) -> ToolResult:
	"""Get the active salary structure for an employee."""
	employee = _resolve_employee(args)

	filters = {"employee": employee, "is_active": 1}
	company = as_str(args, "company")
	if company:
		filters["company"] = resolve_company(company)

	name = frappe.db.get_value(
		SALARY_STRUCTURE,
		filters,
		"name",
		order_by="effective_from desc",
	)
	if not name:
		raise ToolError(f"no active salary structure for employee {employee!r}.")

	fields = [
		"name",
		"employee",
		"employee_name",
		"company",
		"pay_type",
		"base_rate",
		*compat.existing_fields(SALARY_STRUCTURE, ("hourly_rate",)),
		*_structure_activity_fields(),
		"effective_from",
		"effective_to",
		"is_active",
		"notes",
	]
	row = frappe.db.get_value(SALARY_STRUCTURE, name, fields, as_dict=True)
	data = {k: (str(v) if v is not None else None) for k, v in row.items()}

	# WHAT THIS WORKER IS ACTUALLY PAID, which since v0.61.0 is not always the
	# number in the column. A piece-rate structure with base_rate 0 inherits the
	# company rate, and a read of the structure that showed the zero and stopped
	# would be a read that says a picker earns nothing.
	structure = dict(row)
	structure.setdefault("employee", employee)
	effective_rate = _num(row.get("base_rate"))
	rate_source = wage_defaults.SOURCE_STRUCTURE
	if str(row.get("pay_type") or "") == "Piece Rate" and effective_rate <= 0:
		resolved, unresolved = _resolve_piece_rates({employee: structure}, row.get("company") or "", today())
		if unresolved:
			data["effective_rate"] = None
			data["rate_source"] = None
			data["rate_note"] = unresolved[0]["reason"]
			return ToolResult(
				data=data,
				summary=(
					f"Salary structure for {employee}: {row.get('pay_type')} with NO RATE — "
					"none on the structure and none in the company table"
				),
			)
		effective_rate = _num(structure.get("base_rate"))
		rate_source = wage_defaults.SOURCE_COMPANY
		data["piecework_rate"] = structure.get("piecework_rate")
		data["rate_note"] = (
			f"base_rate is 0 on the structure, so payroll reads the company-wide Piecework "
			f"Rate {structure.get('piecework_rate')} for "
			f"{structure.get('piecework_activity') or 'this activity'}. Raise it there and "
			"this worker follows it."
		)
		if resolved:
			data["piecework_activity"] = structure.get("piecework_activity")
	data["effective_rate"] = effective_rate
	data["rate_source"] = rate_source

	return ToolResult(
		data=data,
		summary=(
			f"Salary structure for {employee}: {row.get('pay_type')} at {effective_rate}"
			+ (" (from the company piecework rate)" if rate_source == wage_defaults.SOURCE_COMPANY else "")
		),
	)


def list_salary_structures(args: dict) -> ToolResult:
	"""List salary structures with optional filters."""
	filters = {}
	company = as_str(args, "company")
	if company:
		filters["company"] = resolve_company(company)
	pay_type = as_str(args, "pay_type")
	if pay_type:
		filters["pay_type"] = pay_type
	is_active = args.get("is_active")
	if is_active is not None:
		filters["is_active"] = int(is_active)
	employee = as_str(args, "employee")
	if employee:
		filters["employee"] = _resolve_employee({"employee": employee})
	limit = as_int(args, "limit", 100)
	if limit and limit > 500:
		limit = 500

	rows = frappe.db.get_all(
		SALARY_STRUCTURE,
		filters=filters,
		fields=[
			"name",
			"employee",
			"employee_name",
			"company",
			"pay_type",
			"base_rate",
			*compat.existing_fields(SALARY_STRUCTURE, ("hourly_rate",)),
			*_structure_activity_fields(),
			"effective_from",
			"effective_to",
			"is_active",
		],
		limit_page_length=limit,
		order_by="modified desc",
	)
	structures = [dict(r) for r in rows]
	# A piece-rate structure with base_rate 0 is not a worker on nothing — it is a
	# worker on the company rate. Flagged rather than resolved, because resolving
	# every row would mean one rate-table read per company per list call and the
	# answer is one `get_salary_structure` away.
	inheriting = [
		row["name"]
		for row in structures
		if str(row.get("pay_type") or "") == "Piece Rate" and _num(row.get("base_rate")) <= 0
	]
	data = {
		"structures": structures,
		"count": len(rows),
		"inheriting_company_piecework_rate": inheriting,
	}
	if inheriting:
		data["note"] = (
			f"{len(inheriting)} structure(s) have base_rate 0 and pay type Piece Rate, which "
			"means they take the company-wide Piecework Rate for their activity rather than a "
			"rate of their own. get_salary_structure resolves what one of them actually pays; "
			"list_piecework_rates has the table."
		)
	return ToolResult(data=data, summary=f"{len(rows)} salary structure(s)")


def preview_payroll(args: dict) -> ToolResult:
	"""Dry-run payroll for a single employee — no records created."""
	employee = _resolve_employee(args)
	pay_period_start = as_date(args, "pay_period_start", required=True)
	pay_period_end = as_date(args, "pay_period_end", required=True)
	pay_frequency = as_str(args, "pay_frequency") or "Biweekly"
	if pay_frequency not in PERIODS_PER_YEAR:
		raise ToolError(f"pay_frequency must be one of: {', '.join(PERIODS_PER_YEAR)}.")

	company = as_str(args, "company")
	if company:
		company = resolve_company(company)

	# Load salary structure
	ss_filters = {"employee": employee, "is_active": 1}
	if company:
		ss_filters["company"] = company
	ss_name = frappe.db.get_value(
		SALARY_STRUCTURE,
		ss_filters,
		"name",
		order_by="effective_from desc",
	)
	if not ss_name:
		raise ToolError(f"no active salary structure for {employee}.")

	ss = frappe.db.get_value(
		SALARY_STRUCTURE,
		ss_name,
		[
			"name",
			"company",
			"pay_type",
			"base_rate",
			"employee_name",
			*compat.existing_fields(SALARY_STRUCTURE, ("hourly_rate",)),
			*_structure_activity_fields(),
			*_structure_region_fields(),
		],
		as_dict=True,
	)

	# The company-wide fallback, for a piece-rate structure that names no rate of
	# its own. `_resolve_piece_rates` RETURNS a miss rather than raising it, so a
	# whole-company run can report and carry on — here there is nobody else to
	# carry on for, so the miss is the answer and it is raised.
	structure = {"employee": employee, **dict(ss)}
	rate_company = company or ss.get("company") or ""
	_resolved, unresolved = _resolve_piece_rates({employee: structure}, rate_company, str(pay_period_end))
	if unresolved:
		raise ToolError(unresolved[0]["reason"])

	# Load shifts
	shifts = _load_shifts(employee, pay_period_start, pay_period_end, company)

	# Load tax config
	tax_config = _build_tax_config(employee, pay_frequency, company)

	employee_name = (
		ss.get("employee_name") or frappe.db.get_value(EMPLOYEE, employee, "employee_name") or employee
	)

	result = calculate_full_payroll(
		{"employee": employee, "employee_name": employee_name},
		shifts,
		{
			"pay_type": ss.pay_type,
			"base_rate": _num(structure.get("base_rate")),
			"hourly_rate": ss.get("hourly_rate"),
			"name": ss_name,
			# v0.63.0. WHICH of the state's floors this worker's hours are owed.
			# Declared by the engine since v0.49.0 and supplied by nobody, so
			# Oregon's Portland metro rate — the highest of the three, and the one
			# an orchard inside the urban growth boundary is on — was unreachable
			# from any tool in this app.
			"min_wage_regions": _min_wage_regions(structure, tax_config.get("min_wage_rates")),
		},
		tax_config,
	)
	result = _with_minimum_wage_view(result, tax_config, structure)

	# Where the piece rate came from, on the preview that used it. "Why is this
	# picker on $1.30" should be answerable off the preview rather than by opening
	# two registers and comparing them.
	if structure.get("piece_rate_source") == wage_defaults.SOURCE_COMPANY:
		result = dict(result)
		result["piece_rate_source"] = wage_defaults.SOURCE_COMPANY
		result["piecework_rate"] = structure.get("piecework_rate")
		result["piecework_activity"] = structure.get("piecework_activity") or None
		result["piece_rate_note"] = (
			f"{employee_name}'s salary structure names no base_rate, so the rate came from "
			f"{structure.get('piecework_rate')} — the company-wide Piecework Rate for "
			f"{structure.get('piecework_activity') or 'this activity'} in force on {pay_period_end}."
		)

	makeup = _num(result.get("minimum_wage_makeup"))
	return ToolResult(
		data=result,
		summary=f"Payroll preview for {employee_name}: gross ${result['gross_pay']}, "
		f"net ${result['net_pay']}, {len(shifts)} shift(s)"
		# ON THE SUMMARY LINE, NOT ONLY IN THE DATA. A makeup is the number that
		# says a piece rate is set below the lawful floor, and the whole reason a
		# preview exists is to be READ before anybody posts. It has been in the
		# answer since v0.49.0 and in a nested key nobody opens.
		+ (
			f" — ${makeup:,.2f} of that is minimum wage makeup, so this rate did not "
			f"clear the floor on its own"
			if makeup > 0.005
			else ""
		),
	)


def _with_minimum_wage_view(slip: dict, tax_config: dict, structure: dict) -> dict:
	"""One slip plus the `minimum_wage` block a person reads before posting.

	WHY A BLOCK RATHER THAN MORE TOP-LEVEL KEYS. The engine already answers
	`minimum_wage_makeup`, `minimum_wage_by_state`, `minimum_wage_check` and
	`effective_hourly_rate`, and all four have been in every preview since
	v0.49.0. What none of them says is WHERE THE FLOOR CAME FROM — which is the
	question somebody asks the moment they disagree with the number, and until
	v0.63.0 it had one answer on every site because the table was compiled in.
	Now it is a row somebody can edit, so the preview has to say which row.

	`compliant` IS THE VERDICT AND IT IS TRUE ON A TOPPED-UP SLIP, which is not a
	contradiction: the higher-of rule PAYS the floor, so a slip that needed makeup
	is compliant BECAUSE the makeup is on it. `makeup` is the number that says the
	RATE is too low. A reader who wants "is this lawful" reads the first; a reader
	who wants "is our piece rate right" reads the second, and conflating them would
	either report every underpriced bucket as a violation or hide it entirely.
	"""
	out = dict(slip)
	rates = tax_config.get("min_wage_rates") or {}
	region = normalise_min_wage_region(structure.get("min_wage_region"))
	makeup = _num(out.get("minimum_wage_makeup"))
	configured = sorted(
		state
		for state, config in (tax_config.get("state_configs") or {}).items()
		if isinstance(config, dict) and _num(config.get("minimum_wage")) > 0
	)
	out["minimum_wage"] = {
		"region": region,
		"rates": rates,
		# Which states took their floor off a State Tax Configuration rather than
		# off the shipped table. Named rather than counted, because "the floor is
		# not what I set it to" is answered by knowing whether the row was read at
		# all.
		"configured_states": configured,
		"applies": str(out.get("pay_type") or "") != "Salary",
		"compliant": bool(out.get("minimum_wage_check")),
		"makeup": makeup,
		"floor": _num(out.get("minimum_wage_floor")),
		"earned_gross": _num(out.get("earned_gross")),
		"effective_hourly_rate": _num(out.get("effective_hourly_rate")),
		"by_state": out.get("minimum_wage_by_state") or {},
		"note": _minimum_wage_note(out, makeup, region),
	}
	return out


def _minimum_wage_note(slip: dict, makeup: float, region: str) -> str:
	"""The sentence under the figures, or "" where there is nothing to say.

	THREE CASES AND THEY ARE DIFFERENT FACTS. A salaried slip is not tested at
	all and must say so rather than reporting a silent pass; a topped-up slip is
	lawful AND has a rate problem; everything else needs no sentence, and a note
	on a compliant hourly slip would be noise on every line of every run.
	"""
	if str(slip.get("pay_type") or "") == "Salary":
		return (
			"Salary structures are not topped up here. Whether a salaried employee is exempt "
			"from the minimum wage — executive, administrative, professional, or one of the "
			"agricultural exemptions — is a fact about their job that this app does not hold, "
			"and raising an exempt supervisor's pay because a sixty-hour harvest week divided "
			"their salary below the floor would be inventing an obligation. Any shortfall is "
			"reported in minimum_wage_detail_flat for somebody who knows the answer to decide."
		)
	if makeup > 0.005:
		hours = _num(slip.get("total_hours"))
		earned = _num(slip.get("earned_gross"))
		rate = round(earned / hours, 2) if hours > 0 else 0.0
		return (
			f"The work earned ${earned:,.2f} over {hours:,.2f} hour(s) — ${rate:,.2f} an hour — "
			f"and the hours are owed ${_num(slip.get('minimum_wage_floor')):,.2f} at the "
			f"{region.replace('_', ' ')} floor, so ${makeup:,.2f} of makeup is ON this slip and "
			f"the worker is paid lawfully. THE RATE IS THE PROBLEM, not the pay: a piece rate "
			f"that needs makeup every period is a rate set below what the hours are worth, and "
			f"the employer carries the difference on every one of them."
		)
	return ""


def get_payroll_entry(args: dict) -> ToolResult:
	"""Get a payroll entry with all its slips."""
	name = as_str(args, "name") or as_str(args, "payroll_entry")
	if not name:
		raise ToolError("name (payroll entry docname) is required.")
	if not frappe.db.exists(PAYROLL_ENTRY, name):
		raise ToolError(f"no Farm Payroll Entry called {name!r}.")

	doc = frappe.get_doc(PAYROLL_ENTRY, name)
	slips = []
	for slip in doc.get("slips") or []:
		_g = slip.get if isinstance(slip, dict) else lambda k, d=None: getattr(slip, k, d)
		slip_data = {
			"employee": _g("employee"),
			"employee_name": _g("employee_name"),
			"pay_type": _g("pay_type"),
			"work_state": _g("work_state"),
			"total_hours": float(_g("total_hours") or 0),
			"regular_hours": float(_g("regular_hours") or 0),
			"overtime_hours": float(_g("overtime_hours") or 0),
			"piece_units": float(_g("piece_units") or 0),
			"piece_rate": float(_g("piece_rate") or 0),
			"gross_pay": float(_g("gross_pay") or 0),
			# Both halves of gross, so a stored run answers "why is this bigger
			# than the buckets times the rate" without anybody recomputing it.
			# Slips written before v0.49.0 have neither, and read back as zero and
			# as gross — which is exactly what they were.
			"earned_gross": float(_g("earned_gross") or _g("gross_pay") or 0),
			"minimum_wage_makeup": float(_g("minimum_wage_makeup") or 0),
			"federal_withholding": float(_g("federal_withholding") or 0),
			"state_withholding": float(_g("state_withholding") or 0),
			"social_security": float(_g("social_security") or 0),
			"medicare": float(_g("medicare") or 0),
			"total_deductions": float(_g("total_deductions") or 0),
			"net_pay": float(_g("net_pay") or 0),
			"social_security_employer": float(_g("social_security_employer") or 0),
			"medicare_employer": float(_g("medicare_employer") or 0),
			"futa": float(_g("futa") or 0),
			"state_unemployment": float(_g("state_unemployment") or 0),
			"state_employer_other": float(_g("state_employer_other") or 0),
			"total_employer_taxes": float(_g("total_employer_taxes") or 0),
			"salary_structure": _g("salary_structure"),
			"minimum_wage_check": bool(int(_g("minimum_wage_check") or 0)),
			"effective_hourly_rate": float(_g("effective_hourly_rate") or 0),
		}
		raw_detail = _g("state_taxes_detail")
		if raw_detail:
			try:
				slip_data["state_taxes_detail"] = json.loads(raw_detail)
			except (json.JSONDecodeError, TypeError):
				slip_data["state_taxes_detail"] = raw_detail
		slips.append(slip_data)

	data = {
		"name": doc.name,
		"company": doc.company,
		"pay_period_start": str(doc.pay_period_start),
		"pay_period_end": str(doc.pay_period_end),
		"pay_frequency": doc.pay_frequency,
		"status": doc.status,
		"total_gross": float(doc.total_gross or 0),
		"total_deductions": float(doc.total_deductions or 0),
		"total_net": float(doc.total_net or 0),
		"employee_count": int(doc.employee_count or 0),
		"slips": slips,
	}
	return ToolResult(
		data=data,
		summary=f"Payroll entry {name}: {doc.status}, {len(slips)} slip(s), "
		f"gross ${doc.total_gross}, net ${doc.total_net}",
	)


def list_payroll_entries(args: dict) -> ToolResult:
	"""List payroll entries with optional filters."""
	filters = {}
	company = as_str(args, "company")
	if company:
		filters["company"] = resolve_company(company)
	status = as_str(args, "status")
	if status:
		filters["status"] = status
	pay_frequency = as_str(args, "pay_frequency")
	if pay_frequency:
		filters["pay_frequency"] = pay_frequency
	limit = as_int(args, "limit", 100)
	if limit and limit > 500:
		limit = 500

	rows = frappe.db.get_all(
		PAYROLL_ENTRY,
		filters=filters,
		fields=[
			"name",
			"company",
			"pay_period_start",
			"pay_period_end",
			"pay_frequency",
			"status",
			"total_gross",
			"total_deductions",
			"total_net",
			"employee_count",
		],
		limit_page_length=limit,
		order_by="pay_period_start desc",
	)
	data = {"entries": [dict(r) for r in rows], "count": len(rows)}
	return ToolResult(data=data, summary=f"{len(rows)} payroll entry(ies)")


# ── Mutating tools ────────────────────────────────────────────────────────


def create_salary_structure(args: dict) -> ToolResult:
	"""Create a salary structure for an employee.

	v0.61.0. TWO RATES CAN NOW COME FROM A COMPANY TABLE INSTEAD OF THE CALLER,
	and the two arrive by different routes because they are different kinds of
	fact:

	  * The HOURLY rate is COPIED ONTO THE STRUCTURE at creation, from the
	    Position Wage Default for this employee's Designation. From then on it is
	    this worker's number and editing the default does not reach back through
	    it — an hourly wage is what somebody was hired at.
	  * The PIECE rate is NOT copied. A piece-rate structure created with
	    `base_rate` 0 inherits from the company's Piecework Rate table on every
	    payroll run, which is what makes a mid-season raise one edit instead of a
	    hundred. What is checked HERE is only that the inheritance will resolve:
	    a structure that would fail on payday should fail now, in front of the
	    person creating it, rather than in a run three weeks later.
	"""
	employee = _resolve_employee(args)
	company = resolve_company(as_str(args, "company"), required=True)
	pay_type = as_str(args, "pay_type", required=True)
	if pay_type not in ("Piece Rate", "Hourly", "Salary"):
		raise ToolError("pay_type must be Piece Rate, Hourly, or Salary.")
	piecework_activity = as_str(args, "piecework_activity")
	if piecework_activity and pay_type != "Piece Rate":
		raise ToolError(
			f"piecework_activity is only meaningful on a Piece Rate structure, and this one is "
			f"{pay_type}. It names which company piecework rate pays this worker. Nothing was created."
		)

	designation = frappe.db.get_value(EMPLOYEE, employee, "designation") or ""
	effective_from = as_date(args, "effective_from") or today()
	seeded: dict = {}

	base_rate = _as_float(args, "base_rate", 0.0)
	if base_rate < 0:
		raise ToolError("base_rate cannot be negative.")
	if base_rate == 0 and pay_type == "Hourly":
		# An Hourly structure's base_rate IS the hourly rate, so the Position Wage
		# Default seeds it directly.
		default_row = wagedefaults.default_hourly_rate(company, designation, effective_from)
		if default_row:
			base_rate = float(default_row.get("hourly_rate") or 0)
			seeded["base_rate"] = {
				"from": "position_wage_default",
				"position_wage_default": default_row.get("name"),
				"designation": designation,
				"effective_on": effective_from,
			}
	if base_rate <= 0 and pay_type != "Piece Rate":
		raise ToolError(
			"base_rate must be positive"
			+ (
				f", and {company} has no active Position Wage Default for {designation!r} to take it from"
				if pay_type == "Hourly" and designation
				else ""
			)
			+ (
				". This employee has no designation, so there is no position wage default to look up either"
				if pay_type == "Hourly" and not designation
				else ""
			)
			+ ". Zero is only meaningful on a Piece Rate structure, where it means 'inherit the "
			"company-wide Piecework Rate'. Nothing was created."
		)

	# What an hour of non-piece work pays this worker. Optional, and only ever read
	# for a shift whose own pay type says Hourly — a picker moved onto irrigation
	# for the afternoon. Zero is allowed and means "not set": those hours then earn
	# nothing at the rate and are carried to the minimum wage by the makeup, which
	# is a visible figure on the slip rather than a silent one.
	hourly_rate = _as_float(args, "hourly_rate", 0.0)
	if hourly_rate < 0:
		raise ToolError("hourly_rate cannot be negative.")
	if hourly_rate == 0 and pay_type != "Hourly":
		default_row = wagedefaults.default_hourly_rate(company, designation, effective_from)
		if default_row:
			hourly_rate = float(default_row.get("hourly_rate") or 0)
			seeded["hourly_rate"] = {
				"from": "position_wage_default",
				"position_wage_default": default_row.get("name"),
				"designation": designation,
				"effective_on": effective_from,
			}

	effective_to = as_date(args, "effective_to")
	notes = as_str(args, "notes")

	# v0.63.0. Which of a state's geographic minimum wage rates these hours are
	# owed. REFUSED rather than defaulted when it is not one of the three, because
	# unlike the payroll path — where an unreadable value on an existing row must
	# not hold up a whole company's pay — this is somebody typing it, once, in
	# front of the answer. `Standard` is the default and covers every Washington
	# worker and most Oregon ones.
	region = as_str(args, "min_wage_region") or as_str(args, "region")
	if region and region.strip().casefold() not in MIN_WAGE_REGION_KEYS:
		raise ToolError(
			f"min_wage_region must be Standard, Non-Urban or Portland Metro, got {region!r}. "
			f"Oregon sets three rates by geography under ORS 653.025; Washington sets one, so "
			f"every Washington worker is Standard. Nothing was created."
		)

	# A Piece Rate structure with no rate of its own has to be able to find one,
	# and it is better to say so now than on payday. `resolve_piece_rate` raises
	# the sentence that names the company and the activity.
	inherited = None
	if pay_type == "Piece Rate" and base_rate == 0:
		candidate = {
			"employee": employee,
			"employee_name": frappe.db.get_value(EMPLOYEE, employee, "employee_name") or employee,
			"company": company,
			"base_rate": 0,
			"piecework_activity": piecework_activity,
		}
		inherited = wage_defaults.resolve_piece_rate(
			candidate, wagedefaults.rates_for_company(company), effective_from
		)

	values = {
		"doctype": SALARY_STRUCTURE,
		"employee": employee,
		"company": company,
		"pay_type": pay_type,
		"base_rate": base_rate,
		"hourly_rate": hourly_rate or 0,
		"effective_from": effective_from,
		"effective_to": effective_to or None,
		"is_active": 1,
		"notes": notes or None,
	}
	if _structure_activity_fields():
		values["piecework_activity"] = piecework_activity or None
	if _structure_region_fields():
		values["min_wage_region"] = _REGION_LABELS[normalise_min_wage_region(region)]

	doc = frappe.get_doc(values)
	doc.flags.ignore_permissions = True
	doc.insert()

	emp_name = frappe.db.get_value(EMPLOYEE, employee, "employee_name") or employee
	return ToolResult(
		data={
			"name": doc.name,
			"employee": employee,
			"employee_name": emp_name,
			"designation": designation or None,
			"pay_type": pay_type,
			"base_rate": base_rate,
			"hourly_rate": hourly_rate,
			"min_wage_region": _REGION_LABELS[normalise_min_wage_region(region)],
			"piecework_activity": wage_defaults.normalize_activity(piecework_activity) or None,
			"effective_from": str(effective_from),
			"seeded_from_defaults": seeded or None,
			"inherits_piecework_rate": (
				{
					"piecework_rate": inherited["piecework_rate"],
					"activity": inherited["activity"],
					"rate_per_unit": inherited["rate"],
				}
				if inherited
				else None
			),
			"note": (
				(
					f"base_rate is 0, so every payroll run reads the company-wide Piecework Rate "
					f"for {inherited['activity']} — today that is {inherited['piecework_rate']} at "
					f"{inherited['rate']} per unit. Raise the rate there and this worker follows "
					"it; put a number on this structure instead and the structure wins. "
					if inherited
					else ""
				)
				+ (
					f"The hourly rate was taken from the {designation} position wage default "
					f"({(seeded.get('hourly_rate') or seeded.get('base_rate') or {}).get('position_wage_default')}). "
					"It is a copy: editing that default later will not change what this worker is "
					"paid. "
					if seeded
					else ""
				)
			).strip()
			or None,
		},
		summary=f"Salary structure created for {emp_name}: {pay_type} at {base_rate}"
		+ (f" (inherited from {inherited['piecework_rate']})" if inherited else "")
		+ (" (hourly rate from the position wage default)" if seeded else ""),
	)


def deactivate_salary_structure(args: dict) -> ToolResult:
	"""Soft-deactivate a salary structure."""
	name = as_str(args, "name") or as_str(args, "salary_structure")
	if not name:
		employee = as_str(args, "employee")
		if employee:
			employee = _resolve_employee({"employee": employee})
			name = frappe.db.get_value(
				SALARY_STRUCTURE,
				{"employee": employee, "is_active": 1},
				"name",
				order_by="effective_from desc",
			)
	if not name:
		raise ToolError("name or employee is required to identify the salary structure.")
	if not frappe.db.exists(SALARY_STRUCTURE, name):
		raise ToolError(f"no Farm Salary Structure called {name!r}.")

	frappe.db.set_value(
		SALARY_STRUCTURE,
		name,
		{
			"is_active": 0,
			"effective_to": today(),
		},
	)

	emp = frappe.db.get_value(SALARY_STRUCTURE, name, "employee")
	emp_name = frappe.db.get_value(EMPLOYEE, emp, "employee_name") if emp else name
	return ToolResult(
		data={"name": name, "is_active": 0, "effective_to": today()},
		summary=f"Salary structure {name} deactivated for {emp_name}",
	)


def calculate_payroll(args: dict) -> ToolResult:
	"""Generate a full payroll entry for a pay period (Draft status)."""
	company = resolve_company(as_str(args, "company"), required=True)
	pay_period_start = as_date(args, "pay_period_start", required=True)
	pay_period_end = as_date(args, "pay_period_end", required=True)
	pay_frequency = as_str(args, "pay_frequency") or "Biweekly"
	if pay_frequency not in PERIODS_PER_YEAR:
		raise ToolError(f"pay_frequency must be one of: {', '.join(PERIODS_PER_YEAR)}.")

	# Find all active employees with salary structures
	structures = frappe.db.get_all(
		SALARY_STRUCTURE,
		filters={"company": company, "is_active": 1},
		fields=[
			"name",
			"employee",
			"employee_name",
			"pay_type",
			"base_rate",
			*_structure_activity_fields(),
			*_structure_region_fields(),
		],
	)
	if not structures:
		raise ToolError(f"no active salary structures for company {company}.")

	# The company-wide piece rate, for every structure that names none. Reported
	# rather than raised, for the reason `run_payroll_for_period` gives at length:
	# one worker's missing rate does not hold up everybody else's pay.
	by_employee = {str(row.get("employee") or ""): row for row in structures}
	_from_company, missing_rates = _resolve_piece_rates(by_employee, company, str(pay_period_end))

	# Create the payroll entry
	entry = frappe.get_doc(
		{
			"doctype": PAYROLL_ENTRY,
			"company": company,
			"pay_period_start": pay_period_start,
			"pay_period_end": pay_period_end,
			"pay_frequency": pay_frequency,
			"status": "Draft",
		}
	)

	total_gross = 0.0
	total_deductions = 0.0
	total_net = 0.0
	# v0.63.0. The minimum wage picture for the whole run, gathered as it is
	# calculated. See `_minimum_wage_summary` for why it is on the RESULT of this
	# tool rather than only inside the entry it writes.
	minimum_wage_rows: list[dict] = []

	for ss in structures:
		employee = ss.employee
		shifts = _load_shifts(employee, pay_period_start, pay_period_end, company)
		tax_config = _build_tax_config(employee, pay_frequency, company)

		slip = calculate_full_payroll(
			{"employee": employee, "employee_name": ss.employee_name},
			shifts,
			{
				"pay_type": ss.pay_type,
				"base_rate": float(ss.base_rate or 0),
				"name": ss.name,
				"min_wage_regions": _min_wage_regions(ss, tax_config.get("min_wage_rates")),
			},
			tax_config,
		)
		minimum_wage_rows.append(_minimum_wage_row(ss, slip))

		state_detail = json.dumps(slip.get("state_taxes_detail", {}), default=str)

		entry.append(
			"slips",
			{
				"employee": employee,
				"employee_name": ss.employee_name,
				"salary_structure": ss.name,
				"pay_type": slip["pay_type"],
				"work_state": slip["work_state"],
				"total_hours": slip["total_hours"],
				"regular_hours": slip["regular_hours"],
				"overtime_hours": slip["overtime_hours"],
				"piece_units": slip["piece_units"],
				"piece_rate": slip["piece_rate"],
				"gross_pay": slip["gross_pay"],
				"federal_withholding": slip["federal_withholding"],
				"state_withholding": slip["state_withholding"],
				"social_security": slip["social_security"],
				"medicare": slip["medicare"],
				"state_taxes_detail": state_detail,
				"total_deductions": slip["total_deductions"],
				"net_pay": slip["net_pay"],
				"minimum_wage_check": 1 if slip["minimum_wage_check"] else 0,
				# v0.63.0. THE TWO COLUMNS THE SLIP DOCTYPE HAS CARRIED SINCE v0.49.0
				# AND THIS TOOL NEVER WROTE. `_slip_row` — the period path — has
				# filled both from the start; this path stored the topped-up gross
				# and nothing saying it had been topped up, so a stored row could not
				# answer "how much of this was makeup" and the audit trail for a
				# below-floor piece rate stopped at the preview.
				"earned_gross": slip.get("earned_gross") or 0,
				"minimum_wage_makeup": slip.get("minimum_wage_makeup") or 0,
				"effective_hourly_rate": slip["effective_hourly_rate"],
				"social_security_employer": slip.get("social_security_employer") or 0,
				"medicare_employer": slip.get("medicare_employer") or 0,
				"futa": slip.get("futa") or 0,
				"state_unemployment": slip.get("state_unemployment") or 0,
				"state_employer_other": slip.get("state_employer_other") or 0,
				"total_employer_taxes": slip.get("total_employer_taxes") or 0,
			},
		)

		total_gross += slip["gross_pay"]
		total_deductions += slip["total_deductions"]
		total_net += slip["net_pay"]

	entry.total_gross = round(total_gross, 2)
	entry.total_deductions = round(total_deductions, 2)
	entry.total_net = round(total_net, 2)
	entry.employee_count = len(structures)
	entry.status = "Calculated"

	entry.flags.ignore_permissions = True
	entry.insert()

	# Every bucket that just fed a slip is spoken for. Without this, the exact
	# same Accepted captures are still sitting there Pending/Linked for the next
	# run — a correction, a re-run, a later period whose window laps this one —
	# to read and pay a second time.
	for ss in structures:
		_mark_bucket_entries_paid(ss.employee, pay_period_start, pay_period_end, company)

	minimum_wage = _minimum_wage_summary(minimum_wage_rows)
	return ToolResult(
		data={
			"name": entry.name,
			"company": company,
			"pay_period": f"{pay_period_start} to {pay_period_end}",
			"pay_frequency": pay_frequency,
			"status": "Calculated",
			"employee_count": len(structures),
			"total_gross": entry.total_gross,
			"total_deductions": entry.total_deductions,
			"total_net": entry.total_net,
			"employees_missing_piece_rates": missing_rates,
			# v0.63.0. THE ENTRY IS A DRAFT AND THIS IS WHAT SOMEBODY READS BEFORE
			# SUBMITTING IT. Every figure here was computed on the way through and
			# stored on the slips; what was missing was a place to see it without
			# opening forty child rows, on the one call that produces the document
			# a person is about to approve.
			"minimum_wage": minimum_wage,
		},
		summary=f"Payroll entry {entry.name}: {len(structures)} employee(s), "
		f"gross ${entry.total_gross}, net ${entry.total_net}"
		+ (
			f" — ${minimum_wage['total_makeup']:,.2f} of minimum wage makeup across "
			f"{len(minimum_wage['topped_up'])} worker(s)"
			if minimum_wage["total_makeup"] > 0.005
			else ""
		)
		+ (
			f" — {len(minimum_wage['below_floor'])} slip(s) BELOW THE FLOOR"
			if minimum_wage["below_floor"]
			else ""
		)
		+ (
			f" — {len(missing_rates)} piece-rate worker(s) with no rate on their structure "
			"and none in the company table"
			if missing_rates
			else ""
		),
	)


def _minimum_wage_row(structure, slip: dict) -> dict:
	"""One worker's minimum wage picture, for the run summary above."""
	return {
		"employee": slip.get("employee"),
		"employee_name": slip.get("employee_name"),
		"salary_structure": structure.get("name"),
		"pay_type": slip.get("pay_type"),
		"region": normalise_min_wage_region(structure.get("min_wage_region")),
		"total_hours": _num(slip.get("total_hours")),
		"earned_gross": _num(slip.get("earned_gross")),
		"minimum_wage_floor": _num(slip.get("minimum_wage_floor")),
		"minimum_wage_makeup": _num(slip.get("minimum_wage_makeup")),
		"effective_hourly_rate": _num(slip.get("effective_hourly_rate")),
		"meets_minimum_wage": bool(slip.get("minimum_wage_check")),
		"by_state": slip.get("minimum_wage_by_state") or {},
	}


def _minimum_wage_summary(rows: list) -> dict:
	"""The run's wage-floor picture, as three lists that mean three things.

	`topped_up` IS NOT A VIOLATION LIST and `below_floor` is. The higher-of rule
	pays the floor, so a worker who needed makeup was paid lawfully and the makeup
	is the number that says THE RATE is set too low — a real problem, on the
	employer's own cost rather than on the worker's cheque, and one that recurs
	every period until somebody changes the rate. `below_floor` is the slip that
	came out short ANYWAY, which after v0.49.0 should be empty for every piece-rate
	and hourly worker and is where a salaried shortfall surfaces.

	`salaried_shortfall` is that third case named rather than folded into the
	second. Whether a salaried employee is exempt from the minimum wage is a fact
	about their job this app does not hold, so their pay is not raised and the gap
	is reported for somebody who knows the answer.
	"""
	topped_up = [row for row in rows if row["minimum_wage_makeup"] > 0.005]
	below = [row for row in rows if not row["meets_minimum_wage"] and row["pay_type"] != "Salary"]
	salaried = [row for row in rows if row["pay_type"] == "Salary" and not row["meets_minimum_wage"]]
	total = round(sum(row["minimum_wage_makeup"] for row in rows), 2)
	note = ""
	if topped_up:
		note = (
			f"{len(topped_up)} worker(s) were topped up to the minimum wage, ${total:,.2f} in "
			f"total. THEY WERE PAID LAWFULLY — the floor is paid, not merely compared against — "
			f"and the makeup is the figure that says their rate is set below what their hours "
			f"are worth. It recurs every period until the rate changes."
		)
	if below:
		note += (
			(" " if note else "")
			+ f"{len(below)} slip(s) are BELOW THE FLOOR after the makeup, which should not "
			f"happen on a piece-rate or hourly structure: check that every shift carries a "
			f"work_state, because a shift with none has no legislature behind it and no floor "
			f"is applied."
		)
	if salaried:
		note += (
			(" " if note else "")
			+ f"{len(salaried)} salaried slip(s) divide below the floor and were NOT topped up. "
			f"Whether a salaried employee is exempt is a fact about their job this app does not "
			f"hold; somebody who knows decides."
		)
	return {
		"total_makeup": total,
		"topped_up": topped_up,
		"below_floor": below,
		"salaried_shortfall": salaried,
		"note": note,
	}


def submit_payroll(args: dict) -> ToolResult:
	"""Move a payroll entry from Calculated to Submitted."""
	name = as_str(args, "name") or as_str(args, "payroll_entry")
	if not name:
		raise ToolError("name (payroll entry docname) is required.")
	if not frappe.db.exists(PAYROLL_ENTRY, name):
		raise ToolError(f"no Farm Payroll Entry called {name!r}.")

	status = frappe.db.get_value(PAYROLL_ENTRY, name, "status")
	if status != "Calculated":
		raise ToolError(f"payroll entry {name} is {status!r}. Only Calculated entries can be submitted.")

	frappe.db.set_value(PAYROLL_ENTRY, name, "status", "Submitted")

	return ToolResult(
		data={"name": name, "status": "Submitted"},
		summary=f"Payroll entry {name} submitted",
		docstatus_delta="Calculated → Submitted",
	)


# ── v0.35.0: the integrated run ───────────────────────────────────────────


def get_employee_timesheet_summary(args: dict) -> ToolResult:
	"""One employee's hours in a date range, as payroll will read them.

	THE HOURS WITHOUT THE MONEY, and that is the point of it being its own tool.
	"Why is my cheque this?" is answered by the timesheet, not the withholding,
	and a foreman who can show somebody their own spans, their own overtime and
	which week it fell in has answered the question before it becomes a dispute.
	It needs none of the payroll switches, because the hours are not the payroll.
	"""
	employee = _resolve_employee(args)
	start = as_date(args, "start_date") or as_date(args, "pay_period_start", required=True)
	end = as_date(args, "end_date") or as_date(args, "pay_period_end", required=True)
	company = as_str(args, "company")
	if company:
		company = resolve_company(company)

	# v0.235.0: the workweek threshold is the `overtime_rule` in force at the period's end
	# (built-in 40 until a version is published); an explicit argument still wins.
	from .. import payroll_settings

	rule = payroll_settings.overtime(str(end))
	threshold = _as_float(args, "overtime_threshold", rule["weekly_threshold_hours"])
	anchor = as_date(args, "workweek_anchor")

	shifts, provenance = _load_period_shifts(company, start, end, employees=[employee])
	aggregates = payroll_integration.aggregate_shifts_for_period(
		shifts,
		start,
		end,
		overtime_threshold=threshold,
		workweek_anchor=anchor,
	)
	aggregate = aggregates.get(employee) or payroll_integration.empty_aggregate(employee)

	employee_name = (
		aggregate.get("employee_name") or frappe.db.get_value(EMPLOYEE, employee, "employee_name") or employee
	)
	aggregate["employee_name"] = employee_name

	structure = None
	ss_filters = {"employee": employee, "is_active": 1}
	if company:
		ss_filters["company"] = company
	ss_name = frappe.db.get_value(
		SALARY_STRUCTURE,
		ss_filters,
		"name",
		order_by="effective_from desc",
	)
	if ss_name:
		row = frappe.db.get_value(
			SALARY_STRUCTURE,
			ss_name,
			[
				"name",
				"pay_type",
				"base_rate",
				*compat.existing_fields(SALARY_STRUCTURE, ("hourly_rate",)),
			],
			as_dict=True,
		)
		structure = {
			"name": row.get("name"),
			"pay_type": row.get("pay_type"),
			"base_rate": _num(row.get("base_rate")),
			"hourly_rate": row.get("hourly_rate"),
		}

	data = dict(aggregate)
	data["period_start"] = str(start)
	data["period_end"] = str(end)
	data["company"] = company
	data["salary_structure"] = structure
	data["sources"] = provenance

	if structure is None:
		data["no_salary_structure"] = (
			f"{employee_name} has no active salary structure, so these hours cannot be turned "
			"into pay. create_salary_structure is what makes them payable."
		)

	return ToolResult(
		data=data,
		summary=(
			f"{employee_name}: {aggregate['total_hours']}h ({aggregate['overtime_hours']}h OT) "
			f"over {aggregate['shift_count']} shift(s), {start} to {end}"
		),
	)


def _period_run(args: dict, creating: bool) -> tuple[dict, list[dict], dict]:
	"""Everything both period tools do before they diverge.

	One function because a preview that computed anything differently from the
	run it previews would be worse than no preview. The only difference between
	the two tools is whether the slips are written down.
	"""
	company = resolve_company(as_str(args, "company"), required=True)
	start = as_date(args, "pay_period_start", required=True)
	end = as_date(args, "pay_period_end", required=True)
	if str(end) < str(start):
		raise ToolError(
			f"pay_period_end ({end}) is before pay_period_start ({start}). Nothing was "
			f"{'created' if creating else 'calculated'}."
		)
	pay_frequency = as_str(args, "pay_frequency") or "Biweekly"
	if pay_frequency not in PERIODS_PER_YEAR:
		raise ToolError(f"pay_frequency must be one of: {', '.join(PERIODS_PER_YEAR)}.")

	# v0.235.0: the workweek threshold is the `overtime_rule` in force at the period's end
	# (built-in 40 until a version is published); an explicit argument still wins.
	from .. import payroll_settings

	rule = payroll_settings.overtime(str(end))
	threshold = _as_float(args, "overtime_threshold", rule["weekly_threshold_hours"])
	anchor = as_date(args, "workweek_anchor")
	include_unworked = args.get("include_unworked")
	include_unworked = True if include_unworked is None else bool(int(include_unworked))

	only = as_str(args, "employee")
	employees = [_resolve_employee({"employee": only})] if only else None

	structures = _load_structures(company, employees)
	# Before the shifts, because a piece-rate structure with no rate of its own is
	# not payable until the company table has answered — and the answer has to be
	# on the structure before `run_integrated_payroll` reads it. Dated to the
	# period END so a period straddling a rate change pays what was in force when
	# it closed.
	company_rates, missing_rates = _resolve_piece_rates(structures, company, str(end))
	shifts, provenance = _load_period_shifts(company, start, end, employees=employees)

	# v0.63.0. THE WAGE FLOOR, READ OFF THE SITE AND NAMED PER WORKER. Two things
	# the engine has declared since v0.49.0 and nothing ever supplied: the rate
	# table (so an Oregon rate change is a row somebody edits, not a release) and
	# which of a state's geographic rates each structure is on (so the Portland
	# metro floor is reachable at all). Both are resolved here, once, and handed
	# down — a per-employee lookup inside the loop would read the same three
	# configurations forty times for one answer.
	state_configs = _load_state_configs(company)
	min_wage_rates = state_min_wage_rates(state_configs)
	for structure in structures.values():
		structure["min_wage_regions"] = _min_wage_regions(structure, min_wage_rates)

	# Everybody a slip could be owed to: whoever worked, plus whoever has a
	# structure. The union rather than either half, because a worker with no
	# structure has to be reported and a salaried employee with no shift has to
	# be paid.
	worked = sorted({member["employee"] for shift in shifts for member in shift.get("crew") or []})
	known = sorted(set(worked) | set(structures))

	# THE GARNISHMENTS AND THE VOLUNTARY ELECTIONS. Read once for the whole run
	# rather than per employee inside the loop, the same reason the wage floor
	# and the tax tables are. Dated to the period END: a support order that
	# started mid-period is withheld from the period it started in, which is what
	# an employer served part way through a fortnight is required to do.
	deductions_by_employee, deduction_sources = _load_deductions(company, str(end), known)

	slips = payroll_integration.run_integrated_payroll(
		shifts,
		structures,
		_load_w4_map(known),
		state_configs,
		_load_fica_config(),
		start,
		end,
		company=company,
		federal_tax_tables=_load_federal_tables(pay_frequency),
		state_tax_tables=_load_state_tax_tables(),
		min_wage_rates=min_wage_rates,
		pay_frequency=pay_frequency,
		ytd_by_employee=_load_ytd(company, start, known),
		overtime_threshold=threshold,
		workweek_anchor=anchor,
		include_unworked=include_unworked,
		deductions_by_employee=deductions_by_employee,
	)

	aggregates = payroll_integration.aggregate_shifts_for_period(
		shifts,
		start,
		end,
		overtime_threshold=threshold,
		workweek_anchor=anchor,
	)
	missing = payroll_integration.employees_missing_structures(aggregates, structures)
	for row in missing:
		row["employee_name"] = (
			row.get("employee_name")
			or frappe.db.get_value(EMPLOYEE, row["employee"], "employee_name")
			or row["employee"]
		)

	context = {
		"company": company,
		# v0.235.0. Which payroll settings this run used — a pay run is answerable to its rules.
		"config_versions": {"overtime_rule": rule["version"]},
		"pay_period_start": str(start),
		"pay_period_end": str(end),
		"pay_frequency": pay_frequency,
		"overtime_threshold": threshold,
		"workweek_anchor": str(anchor) if anchor else str(start),
		"employees_missing_structures": missing,
		# v0.61.0. Two lists rather than one number, because they are opposite
		# facts. `piece_rates_from_company` is the fallback WORKING — these
		# workers were paid the company rate because their own structure named
		# none, which is the whole point of the table. `employees_missing_piece_rates`
		# is the fallback finding nothing, and it is the loud version of a failure
		# that used to be silent: a picker rated at zero still gets paid, by the
		# minimum wage makeup, and the slip balances. Nobody reading the totals
		# would know a rate was never set.
		"piece_rates_from_company": [company_rates[key] for key in sorted(company_rates)],
		"employees_missing_piece_rates": missing_rates,
		# v0.63.0. Where the floor these slips were tested against came from.
		# Named rather than assumed, because until this release there was only one
		# possible answer and now there are two — the shipped table, or a State Tax
		# Configuration somebody edited — and "the floor is not what I set it to"
		# is answered by knowing which.
		# Where the deductions came from, and whether the register was there to
		# read at all. A site that has not migrated has no Farm Payroll Deduction
		# table, and a run that silently withheld nothing would look identical to
		# a run where nobody has a garnishment — which is the difference between
		# "nothing to withhold" and "the orders were never read".
		"deductions": deduction_sources,
		"minimum_wage_rates": min_wage_rates,
		"minimum_wage_states_configured": sorted(
			state
			for state, config in (state_configs or {}).items()
			if isinstance(config, dict) and _num(config.get("minimum_wage")) > 0
		),
		"sources": provenance,
	}
	return context, slips, payroll_integration.summarize_payroll_run(slips)


def _slip_view(slip: dict, verbose: bool) -> dict:
	"""One slip as a caller reads it. The detail is opt-in, and it is large."""
	view = {
		"employee": slip.get("employee"),
		"employee_name": slip.get("employee_name"),
		"salary_structure": slip.get("salary_structure"),
		"pay_type": slip.get("pay_type"),
		"work_state": slip.get("work_state"),
		"total_hours": slip.get("total_hours"),
		"regular_hours": slip.get("regular_hours"),
		"overtime_hours": slip.get("overtime_hours"),
		"break_hours": slip.get("break_hours"),
		"unpaid_break_hours": slip.get("unpaid_break_hours"),
		"piece_units": slip.get("piece_units"),
		"piece_rate": slip.get("piece_rate"),
		"shift_count": slip.get("shift_count"),
		"hours_by_state": slip.get("hours_by_state"),
		"overtime_hours_by_state": slip.get("overtime_hours_by_state"),
		"state_wages": slip.get("state_wages"),
		"gross_pay": slip.get("gross_pay"),
		"earned_gross": slip.get("earned_gross"),
		"minimum_wage_makeup": slip.get("minimum_wage_makeup"),
		"minimum_wage_makeup_by_state": slip.get("minimum_wage_makeup_by_state"),
		"federal_withholding": slip.get("federal_withholding"),
		"state_withholding": slip.get("state_withholding"),
		"social_security": slip.get("social_security"),
		"medicare": slip.get("medicare"),
		# Garnishments and voluntary deductions. `total_deductions` INCLUDES
		# these, so `net_pay = gross_pay - total_deductions` still holds; the
		# taxes on their own are `statutory_deductions`.
		"statutory_deductions": slip.get("statutory_deductions") or 0,
		"total_deduction_withholdings": slip.get("total_deduction_withholdings") or 0,
		"pre_tax_deductions": slip.get("pre_tax_deductions") or 0,
		"post_tax_deductions": slip.get("post_tax_deductions") or 0,
		"garnishment_total": slip.get("garnishment_total") or 0,
		"deduction_lines": slip.get("deduction_lines") or [],
		"deduction_shortfalls": slip.get("deduction_shortfalls") or [],
		"federal_taxable_gross": slip.get("federal_taxable_gross") or 0,
		"fica_taxable_gross": slip.get("fica_taxable_gross") or 0,
		"disposable_earnings": slip.get("disposable_earnings") or 0,
		"total_deductions": slip.get("total_deductions"),
		"net_pay": slip.get("net_pay"),
		"social_security_employer": slip.get("social_security_employer"),
		"medicare_employer": slip.get("medicare_employer"),
		"futa": slip.get("futa"),
		"state_unemployment": slip.get("state_unemployment"),
		"state_employer_other": slip.get("state_employer_other"),
		"total_employer_taxes": slip.get("total_employer_taxes"),
		"total_cost_of_employment": slip.get("total_cost_of_employment"),
		"effective_hourly_rate": slip.get("effective_hourly_rate"),
		"minimum_wage_check": slip.get("minimum_wage_check"),
		"minimum_wage_detail": slip.get("minimum_wage_detail"),
		"minimum_wage_by_state": slip.get("minimum_wage_by_state"),
		"weeks": slip.get("weeks"),
		"open_shifts": slip.get("open_shifts"),
	}
	if verbose:
		view["timesheet"] = slip.get("timesheet")
		view["state_taxes_detail"] = slip.get("state_taxes_detail")
		view["gross_detail"] = slip.get("gross_detail")
		view["federal_detail"] = slip.get("federal_detail")
	return view


def preview_payroll_for_period(args: dict) -> ToolResult:
	"""The whole company's payroll for a period, computed and not written.

	The same arithmetic `run_payroll_for_period` performs, off the same shifts,
	through the same code path — and no record at the end of it. Read the three
	lists before the totals: who has no salary structure, whose state fell below
	its minimum wage, and whose shift never got an end time.
	"""
	context, slips, totals = _period_run(args, creating=False)
	verbose = bool(int(args.get("detail") or 0))

	data = dict(context)
	data["slips"] = [_slip_view(slip, verbose) for slip in slips]
	data["totals"] = totals
	data["created"] = None
	data["note"] = (
		"Nothing was written. run_payroll_for_period performs the identical "
		"calculation and stores it as a Farm Payroll Entry."
	)

	# v0.251.0 (AFB-2026-00032). Punches in the period no supervisor has reviewed: a warning only —
	# pay is computed exactly as before.
	from .. import time_review

	punch_warning = time_review.payroll_warning(context["company"], context["pay_period_start"], context["pay_period_end"])
	if punch_warning:
		data["punches_not_reviewed"] = punch_warning
	return ToolResult(
		data=data,
		summary=(
			(f"WARNING: {punch_warning['punches_not_reviewed']} punch(es) not reviewed "
			 f"({punch_warning['flagged']} flagged). " if punch_warning else "")
			+ f"Payroll preview for {context['company']}, {context['pay_period_start']} to "
			f"{context['pay_period_end']}: {totals['employee_count']} employee(s), "
			f"{totals['total_hours']}h ({totals['total_overtime_hours']}h OT), "
			f"gross ${totals['total_gross']}, net ${totals['total_net']}"
			+ (
				f" — {len(context['employees_missing_structures'])} worker(s) with no salary structure"
				if context["employees_missing_structures"]
				else ""
			)
			+ (
				f" — {len(context['employees_missing_piece_rates'])} piece-rate worker(s) with no rate "
				"on their structure and none in the company table"
				if context["employees_missing_piece_rates"]
				else ""
			)
			+ (
				f" — {len(totals['below_minimum_wage'])} below minimum wage"
				if totals["below_minimum_wage"]
				else ""
			)
		),
	)


def run_payroll_for_period(args: dict) -> ToolResult:
	"""Compute the period off the shift register and store it as a payroll entry.

	Creates a Farm Payroll Entry in **Calculated** status with one slip per
	employee. Calculated rather than Submitted, always: submitting is
	`submit_payroll`, behind its own switch, because the two acts are different —
	one is arithmetic anybody can redo and the other is a statement that this is
	what the farm is paying.

	IT DOES NOT REFUSE A RUN WITH PROBLEMS IN IT. A worker below minimum wage, a
	shift with no end time, a picker with no salary structure — all reported, all
	on the result, none of them a reason to withhold a payroll everybody else is
	waiting on. Same posture `end_shift` takes towards a shift with an unmet
	obligation: state it, keep it.
	"""
	context, slips, totals = _period_run(args, creating=True)
	if not slips:
		raise ToolError(
			f"no payroll slips could be computed for {context['company']} between "
			f"{context['pay_period_start']} and {context['pay_period_end']}. "
			+ (
				"Workers were on shift but none of them has an active salary structure — "
				f"{', '.join(row['employee_name'] for row in context['employees_missing_structures'])}. "
				"create_salary_structure is what makes their hours payable."
				if context["employees_missing_structures"]
				else "No shifts and no active salary structures in this period. Nothing was created."
			)
		)

	entry = frappe.get_doc(
		{
			"doctype": PAYROLL_ENTRY,
			"company": context["company"],
			"pay_period_start": context["pay_period_start"],
			"pay_period_end": context["pay_period_end"],
			"pay_frequency": context["pay_frequency"],
			"status": "Draft",
		}
	)

	for slip in slips:
		entry.append("slips", _slip_row(slip))

	entry.total_gross = totals["total_gross"]
	entry.total_deductions = totals["total_deductions"]
	entry.total_net = totals["total_net"]
	entry.employee_count = totals["employee_count"]
	entry.status = "Calculated"
	entry.flags.ignore_permissions = True
	entry.insert()

	# See the sibling note in calculate_payroll: a bucket this run just paid
	# must not still read as payable to the next one.
	for slip in slips:
		if slip.get("employee"):
			_mark_bucket_entries_paid(
				slip["employee"], context["pay_period_start"], context["pay_period_end"], context["company"]
			)

	data = dict(context)
	data["name"] = entry.name
	data["status"] = "Calculated"
	data["totals"] = totals
	data["slips"] = [_slip_view(slip, False) for slip in slips]

	return ToolResult(
		data=data,
		summary=(
			f"Payroll entry {entry.name} for {context['company']}, "
			f"{context['pay_period_start']} to {context['pay_period_end']}: "
			f"{totals['employee_count']} employee(s), {totals['total_hours']}h "
			f"({totals['total_overtime_hours']}h OT), gross ${totals['total_gross']}, "
			f"net ${totals['total_net']}"
			+ (
				f" — {len(context['employees_missing_piece_rates'])} piece-rate worker(s) with no rate "
				"on their structure and none in the company table"
				if context["employees_missing_piece_rates"]
				else ""
			)
			+ (
				f" — {len(totals['below_minimum_wage'])} below minimum wage"
				if totals["below_minimum_wage"]
				else ""
			)
		),
		docstatus_delta="none → Calculated",
	)


def _slip_row(slip: dict) -> dict:
	"""One computed slip, as a Farm Payroll Slip child row."""
	detail = dict(slip.get("state_taxes_detail") or {})
	detail["_wages_by_state"] = slip.get("state_wages") or {}
	detail["_hours_by_state"] = slip.get("hours_by_state") or {}
	detail["_minimum_wage"] = slip.get("minimum_wage_detail") or {}
	detail["_minimum_wage_makeup"] = slip.get("minimum_wage_by_state") or {}
	if slip.get("gross_detail", {}).get("segments"):
		detail["_pay_segments"] = slip["gross_detail"]["segments"]

	# The itemised deductions travel in the same JSON column the state detail
	# does, so a stub printed three years from now can name each line rather
	# than showing one "Other" figure. The TOTALS get their own columns below —
	# a figure a report sums has to be a column, not a key inside a blob.
	if slip.get("deduction_lines"):
		detail["_deduction_lines"] = slip["deduction_lines"]
	if slip.get("garnishment_detail"):
		detail["_garnishment_detail"] = slip["garnishment_detail"]
	if slip.get("deduction_shortfalls"):
		detail["_deduction_shortfalls"] = slip["deduction_shortfalls"]
	minimum = slip.get("minimum_wage_detail") or {}
	meets = minimum.get("meets_minimum_wage")
	if meets is None:
		meets = slip.get("minimum_wage_check", True)

	return {
		"employee": slip.get("employee"),
		"employee_name": slip.get("employee_name"),
		"salary_structure": slip.get("salary_structure"),
		"pay_type": slip.get("pay_type"),
		"work_state": slip.get("work_state"),
		"total_hours": slip.get("total_hours"),
		"regular_hours": slip.get("regular_hours"),
		"overtime_hours": slip.get("overtime_hours"),
		"piece_units": slip.get("piece_units"),
		"piece_rate": slip.get("piece_rate"),
		"gross_pay": slip.get("gross_pay"),
		# v0.49.0. Gross is `earned_gross + minimum_wage_makeup` and both halves are
		# stored, because "why is this bigger than the buckets times the rate" is a
		# question the row has to be able to answer three years later in an audit.
		"earned_gross": slip.get("earned_gross"),
		"minimum_wage_makeup": slip.get("minimum_wage_makeup") or 0,
		"federal_withholding": slip.get("federal_withholding"),
		"state_withholding": slip.get("state_withholding"),
		"social_security": slip.get("social_security"),
		"medicare": slip.get("medicare"),
		"state_taxes_detail": json.dumps(detail, default=str),
		# Garnishments and voluntary deductions. `total_deductions` INCLUDES
		# these — net pay is what the worker is handed — so a report that used
		# to read the total as "taxes" should read `statutory_deductions`.
		"statutory_deductions": slip.get("statutory_deductions") or 0,
		"total_deduction_withholdings": slip.get("total_deduction_withholdings") or 0,
		"pre_tax_deductions": slip.get("pre_tax_deductions") or 0,
		"post_tax_deductions": slip.get("post_tax_deductions") or 0,
		"garnishment_total": slip.get("garnishment_total") or 0,
		"voluntary_deduction_total": slip.get("voluntary_deduction_total") or 0,
		# The two bases a W-2 is built from. They differ by exactly the 401(k)
		# deferral, which is what Box 1 and Box 3 are supposed to differ by.
		"federal_taxable_gross": slip.get("federal_taxable_gross") or 0,
		"fica_taxable_gross": slip.get("fica_taxable_gross") or 0,
		"disposable_earnings": slip.get("disposable_earnings") or 0,
		"total_deductions": slip.get("total_deductions"),
		"net_pay": slip.get("net_pay"),
		"minimum_wage_check": 1 if meets else 0,
		"effective_hourly_rate": slip.get("effective_hourly_rate"),
		# v0.40.0. Computed since v0.28.0 and stored nowhere until now. None of
		# it is deducted from anybody — it is what the farm owes on top — and
		# without it a payroll journal entry books the wages and leaves the
		# employer's own taxes off the ledger entirely.
		"social_security_employer": slip.get("social_security_employer") or 0,
		"medicare_employer": slip.get("medicare_employer") or 0,
		"futa": slip.get("futa") or 0,
		"state_unemployment": slip.get("state_unemployment") or 0,
		"state_employer_other": slip.get("state_employer_other") or 0,
		"total_employer_taxes": slip.get("total_employer_taxes") or 0,
	}


# ── v0.91.0: the two outputs a payroll run never had ──────────────────────
#
# THE ARITHMETIC HAS BEEN RIGHT SINCE v0.30.0 AND NOBODY COULD READ IT OUT.
# `get_payroll_entry` answers "what did this one run come to" and nothing
# answered the two questions an operator actually asks on payday: what did the
# whole period cost across everybody, and what does one worker get handed with
# their pay. `get_payroll_register` is the first and `render_pay_stub` is the
# second; both read STORED slips and neither recomputes anything, for the reason
# `taxforms.py` gives at length — a page that recomputed would disagree with the
# record it claims to be a view of, and on wages that disagreement is a claim.


#: Statuses a register counts by default. THE SAME RULE `_load_ytd` AND
#: `tools/taxforms._load_slips` APPLY, and for the same reason: a Draft payroll
#: has not been paid and a Cancelled one was not, so neither belongs in a total
#: somebody reconciles a bank account against. `include_drafts` adds Draft back
#: and the result always names which statuses it counted.
REGISTER_STATUSES = ("Calculated", "Submitted")

#: Most payroll runs one register will read. A year of weekly runs is 52 and a
#: year of biweekly is 26; two hundred is a date range somebody typed wrong.
#: REFUSED RATHER THAN TRUNCATED, the posture `bulk_render_tax_form_pdfs` takes
#: — a register that silently stopped short would look like it had covered the
#: period, and its totals would be wrong in the direction nobody checks.
REGISTER_ENTRY_CAP = 200

#: The per-employee columns, in the order a register is read, and where each one
#: comes off a Farm Payroll Slip. `other_deductions` is absent on purpose: it is
#: DERIVED below rather than read, which is what makes this table survive a
#: release that adds a deduction column this one has never heard of.
_REGISTER_COLUMNS = (
	("gross_pay", "gross_pay"),
	("federal_tax", "federal_withholding"),
	("state_tax", "state_withholding"),
	("ss_employee", "social_security"),
	("medicare_employee", "medicare"),
	("total_deductions", "total_deductions"),
	("net_pay", "net_pay"),
	("hours_worked", "total_hours"),
	("piece_units", "piece_units"),
)

#: The employer half. Not deducted from anybody, not in any employee's net, and
#: reported as its own block for exactly that reason.
_EMPLOYER_COLUMNS = (
	("ss_employer", "social_security_employer"),
	("medicare_employer", "medicare_employer"),
	("futa", "futa"),
	("suta", "state_unemployment"),
	("state_employer_other", "state_employer_other"),
)


def _slip_get(row):
	"""One accessor for a slip whichever shape it arrives in.

	A child row read back through `frappe.get_doc` is a Document on a bench and a
	plain dict in the standalone double, and every reader in this module has had
	to say so. See `get_payroll_entry`, which does the same thing inline.
	"""
	if isinstance(row, dict):
		return row.get
	return lambda key, default=None: getattr(row, key, default)


def _register_window(args: dict, company: str) -> tuple[str, str, str]:
	"""`(date_from, date_to, pay_period)` from either way of asking.

	`pay_period` NAMES A FARM PAYROLL ENTRY and its own start and end become the
	window, so "the register for this run" is one argument rather than two dates
	somebody copies off the record and gets wrong by a day. The dates are the
	other way, and one of the two is required — a register with no window is the
	whole site's payroll, which is not a default this offers.
	"""
	pay_period = as_str(args, "pay_period") or as_str(args, "payroll_entry")
	if pay_period:
		if not frappe.db.exists(PAYROLL_ENTRY, pay_period):
			raise ToolError(
				f"no Farm Payroll Entry called {pay_period!r}. `pay_period` names one payroll "
				f"run and takes its period from it; pass date_from and date_to instead to "
				f"cover a range of runs. List them with list_payroll_entries."
			)
		row = (
			frappe.db.get_value(
				PAYROLL_ENTRY,
				pay_period,
				["company", "pay_period_start", "pay_period_end"],
				as_dict=True,
			)
			or {}
		)
		if str(row.get("company") or "") != company:
			raise ToolError(
				f"payroll entry {pay_period} belongs to {row.get('company')!r}, not to "
				f"{company!r}. Nothing was read."
			)
		return str(row.get("pay_period_start") or ""), str(row.get("pay_period_end") or ""), pay_period

	date_from = as_date(args, "date_from") or as_date(args, "from_date") or as_date(args, "pay_period_start")
	date_to = as_date(args, "date_to") or as_date(args, "to_date") or as_date(args, "pay_period_end")
	if not (date_from and date_to):
		raise ToolError(
			"a period is required: either pay_period (a Farm Payroll Entry docname) or both "
			"date_from and date_to as YYYY-MM-DD. A register with no window would be every "
			"payroll run on the site, which is not a total anybody asked for."
		)
	if date_from > date_to:
		raise ToolError(f"date_from {date_from} is after date_to {date_to}. Nothing was read.")
	return date_from, date_to, ""


def get_payroll_register(args: dict) -> ToolResult:
	"""The payroll register for a period: every employee, every column, and the totals.

	WHAT A REGISTER IS FOR is reconciliation — the sheet somebody puts beside a
	bank statement and a general ledger and checks that three numbers agree. So
	every figure here is READ off stored Farm Payroll Slips and none of it is
	recomputed, and the result names the payroll runs it added up so a
	disagreement can be traced to a run rather than argued about.

	THE WINDOW IS ON `pay_period_end`, NOT ON OVERLAP. A run whose period ended
	inside the window is counted whole; one that ended outside it is not counted
	at all, even if some of its days fall in the window. That is how a payroll
	register is read everywhere — the unit is the RUN, because the taxes on it
	were computed for that run's period and cannot be split across a date
	somebody chose afterwards — and splitting one would produce a register whose
	withholding totals reconcile against no deposit that was ever made.

	`other_deductions` IS DERIVED, NOT READ: `total_deductions` less federal,
	state, Social Security and Medicare. Deriving it is what makes this tool
	survive a release that adds a deduction the slip has never carried — a
	garnishment or a 401(k) row lands in `total_deductions` and appears here the
	day it lands, rather than going silently missing from a column that reads a
	field list written before it existed.

	TWO TOTALS OF COST ARE REPORTED AND THEY ARE DIFFERENT NUMBERS.
	`grand_total_labor_cost` is net pay plus every employer tax, which is the
	figure the brief for this tool asked for and the one to use when the question
	is "what did the cheques and the employer's own taxes come to".
	`total_cost_of_employment` is GROSS pay plus every employer tax, and it is
	the money that actually leaves the farm — the withheld income tax and the
	employee's FICA are the employer's to remit, so they leave too, they are just
	not in anybody's net. The two differ by exactly the employees' withholding,
	which is reported beside them as `total_employee_withholding` so the
	arithmetic can be checked on the face of the result rather than taken on
	trust.
	"""
	company = resolve_company(as_str(args, "company"), required=True)
	date_from, date_to, pay_period = _register_window(args, company)

	statuses = list(REGISTER_STATUSES)
	if args.get("include_drafts") in (1, "1", True, "true", "True"):
		statuses.append("Draft")

	filters = {
		"company": company,
		"status": ("in", tuple(statuses)),
		"pay_period_end": ("between", (date_from, date_to)),
	}
	if pay_period:
		# A run named EXPLICITLY is read whatever its status: the caller asked for
		# that run and not for a window, and refusing to show them a Draft they
		# named by docname would be answering a question they did not ask.
		# `statuses_counted` is corrected from what actually came back, below.
		filters = {"name": pay_period}

	entries = frappe.db.get_all(
		PAYROLL_ENTRY,
		filters=filters,
		fields=["name", "pay_period_start", "pay_period_end", "pay_frequency", "status"],
		order_by="pay_period_end asc",
		limit_page_length=0,
	)
	if len(entries) > REGISTER_ENTRY_CAP:
		raise ToolError(
			f"{len(entries)} payroll runs end between {date_from} and {date_to}, and this tool "
			f"reads at most {REGISTER_ENTRY_CAP} — a register that quietly stopped short would "
			f"look like it had covered the period. Narrow the dates. Nothing was read."
		)

	by_employee: dict[str, dict] = {}
	employer: dict[str, float] = {key: 0.0 for key, _field in _EMPLOYER_COLUMNS}
	counted = []

	for entry in entries:
		try:
			doc = frappe.get_doc(PAYROLL_ENTRY, entry["name"])
		except Exception:  # pragma: no cover - a row that vanished between two reads
			continue
		slip_count = 0
		for slip in doc.get("slips") or []:
			get = _slip_get(slip)
			employee = str(get("employee") or "")
			if not employee:
				continue
			slip_count += 1
			row = by_employee.setdefault(
				employee,
				{
					"employee_id": employee,
					"employee_name": get("employee_name") or employee,
					"periods": 0,
					**{key: 0.0 for key, _field in _REGISTER_COLUMNS},
					"other_deductions": 0.0,
				},
			)
			row["periods"] += 1
			if not row.get("employee_name"):
				row["employee_name"] = get("employee_name") or employee
			for key, field in _REGISTER_COLUMNS:
				row[key] += _num(get(field))
			row["other_deductions"] += _num(get("total_deductions")) - (
				_num(get("federal_withholding"))
				+ _num(get("state_withholding"))
				+ _num(get("social_security"))
				+ _num(get("medicare"))
			)
			for key, field in _EMPLOYER_COLUMNS:
				employer[key] += _num(get(field))
		counted.append(
			{
				"name": entry["name"],
				"pay_period_start": str(entry.get("pay_period_start") or ""),
				"pay_period_end": str(entry.get("pay_period_end") or ""),
				"pay_frequency": entry.get("pay_frequency"),
				"status": entry.get("status"),
				"slips": slip_count,
			}
		)

	employees = sorted(
		(
			{key: (round(value, 2) if isinstance(value, float) else value) for key, value in row.items()}
			for row in by_employee.values()
		),
		key=lambda row: (str(row.get("employee_name") or ""), str(row.get("employee_id") or "")),
	)

	totals = {"employees": len(employees), "periods": sum(row["periods"] for row in employees)}
	for key in [name for name, _field in _REGISTER_COLUMNS] + ["other_deductions"]:
		totals[key] = round(sum(row[key] for row in employees), 2)

	employer = {key: round(value, 2) for key, value in employer.items()}
	employer_total = round(sum(employer.values()), 2)
	withheld = round(totals["gross_pay"] - totals["net_pay"], 2)

	data = {
		"company": company,
		"date_from": date_from,
		"date_to": date_to,
		"pay_period": pay_period or None,
		"statuses_counted": (sorted({str(row["status"]) for row in counted}) if pay_period else statuses),
		"payroll_entries": counted,
		"payroll_entry_count": len(counted),
		"employees": employees,
		"totals": totals,
		"employer_costs": {**employer, "total_employer_taxes": employer_total},
		# See the docstring: two different questions, two different numbers, and
		# the difference between them named so it can be checked here.
		"grand_total_labor_cost": round(totals["net_pay"] + employer_total, 2),
		"total_cost_of_employment": round(totals["gross_pay"] + employer_total, 2),
		"total_employee_withholding": withheld,
		"window_rule": (
			"payroll runs whose pay_period_end falls between date_from and date_to, counted "
			"whole. A run is never split across the window."
		),
		"other_deductions_rule": (
			"total_deductions less federal, state, Social Security and Medicare — derived "
			"rather than read, so a deduction column added by a later release is counted here "
			"without this tool changing."
		),
	}
	return ToolResult(
		data=data,
		summary=f"Payroll register for {company}, {date_from} to {date_to}: {len(employees)} "
		f"employee(s) over {len(counted)} run(s), gross ${totals['gross_pay']:,.2f}, "
		f"net ${totals['net_pay']:,.2f}, employer taxes ${employer_total:,.2f}, "
		f"grand total labor cost ${data['grand_total_labor_cost']:,.2f}",
	)


def _stub_slip(doc, employee: str) -> tuple[dict, str]:
	"""The one slip on `doc` that is about `employee`, or a refusal naming who is on it.

	An employee is named by docname OR by the name printed on the slip, because
	the person asking for a stub is holding a piece of paper with a name on it
	rather than a docname. A miss lists the run's own employees: "not on this
	run" and "spelled differently" are the two things it can be, and only one of
	them is the caller's mistake.
	"""
	wanted = str(employee or "").strip()
	on_the_run = []
	folded = wanted.casefold()
	match = None
	for slip in doc.get("slips") or []:
		get = _slip_get(slip)
		name = str(get("employee") or "")
		label = str(get("employee_name") or "")
		on_the_run.append(f"{name} ({label})" if label else name)
		if match is None and wanted in (name, label):
			match = (slip, name)
		elif match is None and folded and folded in (name.casefold(), label.casefold()):
			match = (slip, name)
	if match is None:
		raise ToolError(
			f"no slip for {wanted!r} on payroll entry {doc.name}. This run covers: "
			f"{', '.join(on_the_run) or '<no slips>'}. Nothing was rendered."
		)
	slip, name = match
	get = _slip_get(slip)
	fields = (
		"employee",
		"employee_name",
		"salary_structure",
		"pay_type",
		"work_state",
		"total_hours",
		"regular_hours",
		"overtime_hours",
		"piece_units",
		"piece_rate",
		"gross_pay",
		"earned_gross",
		"minimum_wage_makeup",
		"federal_withholding",
		"state_withholding",
		"social_security",
		"medicare",
		"total_deductions",
		"net_pay",
		"effective_hourly_rate",
		"social_security_employer",
		"medicare_employer",
		"futa",
		"state_unemployment",
		"state_employer_other",
		"total_employer_taxes",
	)
	payload = {field: get(field) for field in fields}
	payload["earned_gross"] = payload.get("earned_gross") or payload.get("gross_pay")
	return payload, name


def _stub_hourly_rate(salary_structure: str, pay_type: str) -> float:
	"""What one hour of this worker's HOURLY work pays, for the earnings lines.

	NOT `effective_hourly_rate`, which is a piece-rate worker's earnings divided
	by their hours — a derived figure that is right for the minimum wage check
	and wrong on a printed line reading "Regular hours at ...". Pricing piece
	work at it and then also printing the units at the piece rate would state the
	same earnings twice.

	A structure that names no hourly rate returns 0.0 and the stub prints the
	hours WITHOUT a rate or an amount, with everything landing on the balancing
	line. See `pay_stub_pdf.earnings_lines`: a line that cannot be priced from
	the record is better absent than priced from a guess.
	"""
	if not salary_structure:
		return 0.0
	row = (
		frappe.db.get_value(
			SALARY_STRUCTURE,
			salary_structure,
			["pay_type", "base_rate", "hourly_rate"],
			as_dict=True,
		)
		or {}
	)
	hourly = _num(row.get("hourly_rate"))
	if hourly:
		return hourly
	if str(row.get("pay_type") or pay_type or "") == "Hourly":
		return _num(row.get("base_rate"))
	return 0.0


def _stub_ytd(company: str, employee: str, period_end: str, entry_name: str) -> dict:
	"""Every withheld and paid figure for this person so far in the CALENDAR year.

	CALENDAR, NOT THIS SITE'S FISCAL YEAR, and `pay_stub_pdf` says why at length:
	a withholding year is a calendar year, the W-2 covers January to December,
	and a stub whose YTD federal withholding cannot be reconciled against the
	form the IRS will see is worse than one carrying no YTD at all.

	THIS PERIOD IS INCLUDED. "Year to date" on a stub means through today's
	cheque — a worker adding their stubs up expects the last one to reach the
	total, not to sit one period behind it.

	Only `Calculated` and `Submitted` runs count, the rule `_load_ytd` applies
	for the same reason. Returns `{}` where nothing could be summed, and the page
	then omits the section rather than drawing a column of zeros.
	"""
	year = str(period_end)[:4]
	if not year:
		return {}
	entries = frappe.db.get_all(
		PAYROLL_ENTRY,
		filters={
			"company": company,
			"status": ("in", REGISTER_STATUSES),
			"pay_period_end": ("<=", str(period_end)),
		},
		fields=["name", "pay_period_end"],
		order_by="pay_period_end asc",
		limit_page_length=0,
	)
	keys = (
		"gross_pay",
		"federal_withholding",
		"state_withholding",
		"social_security",
		"medicare",
		"total_deductions",
		"net_pay",
	)
	totals = {key: 0.0 for key in keys}
	periods = 0
	for entry in entries:
		if str(entry.get("pay_period_end") or "")[:4] != year:
			continue
		try:
			doc = frappe.get_doc(PAYROLL_ENTRY, entry["name"])
		except Exception:  # pragma: no cover - a row that vanished between two reads
			continue
		for slip in doc.get("slips") or []:
			get = _slip_get(slip)
			if str(get("employee") or "") != employee:
				continue
			periods += 1
			for key in keys:
				totals[key] += _num(get(key))
	if not periods:
		return {}
	# The run being rendered may itself be a Draft, in which case the loop above
	# skipped it and the stub in the reader's hand would be missing from its own
	# year to date. Naming the count rather than the entries is enough for the
	# page; `get_payroll_register` is where a run-by-run breakdown belongs.
	return {
		"year": year,
		"periods": periods,
		"includes_this_period": any(entry["name"] == entry_name for entry in entries),
		**{key: round(value, 2) for key, value in totals.items()},
	}


def render_pay_stub(args: dict) -> ToolResult:
	"""Draw one employee's pay stub for one payroll run and attach it to the run.

	THE STATEMENT ORS 652.610 AND RCW 49.46.020 BOTH REQUIRE, and the one page on
	this site that is the record it looks like rather than a working copy of one
	— see `pay_stub_pdf` for why that distinction changes what is printed at the
	foot of it.

	NOTHING IS RECOMPUTED. Every figure is read off the stored Farm Payroll Slip,
	so the page cannot disagree with the run it claims to be a statement of. The
	only things resolved at render time are the hourly rate for the earnings
	lines (off the salary structure, because the slip stores hours and not a
	wage) and the year-to-date block.

	THE PDF IS ATTACHED TO THE FARM PAYROLL ENTRY AND NOT TO A FIELD. A run
	carries one stub per employee and the doctype has one document; a field would
	hold whichever was rendered last and lose the rest. `overwrite` is what
	re-renders one — a correction, most often — and the File that was there stays
	attached to the record either way, because a stub somebody was handed is a
	statement that was made and deleting it would not unmake it.
	"""
	pay_stub_pdf.require()

	entry_name = as_str(args, "payroll_entry") or as_str(args, "name")
	if not entry_name:
		raise ToolError("payroll_entry (the Farm Payroll Entry docname) is required.")
	if not frappe.db.exists(PAYROLL_ENTRY, entry_name):
		raise ToolError(f"no Farm Payroll Entry called {entry_name!r}.")
	employee_arg = as_str(args, "employee") or as_str(args, "employee_name")
	if not employee_arg:
		raise ToolError(
			"employee is required — a payroll entry carries one slip per person and a stub is "
			"one person's. Read the run with get_payroll_entry to see who is on it."
		)

	doc = frappe.get_doc(PAYROLL_ENTRY, entry_name)
	slip, employee = _stub_slip(doc, employee_arg)

	stub = {
		**slip,
		"payroll_entry": doc.name,
		"company": doc.company,
		"pay_period_start": str(doc.pay_period_start),
		"pay_period_end": str(doc.pay_period_end),
		"pay_frequency": doc.pay_frequency,
		"status": doc.status,
		"hourly_rate": _stub_hourly_rate(slip.get("salary_structure"), slip.get("pay_type")),
	}
	stub["ytd"] = _stub_ytd(doc.company, employee, str(doc.pay_period_end), doc.name)

	file_name = pay_stub_pdf.file_name_for(stub)
	overwrite = bool(args.get("overwrite"))
	existing = frappe.db.get_all(
		"File",
		filters={
			"attached_to_doctype": PAYROLL_ENTRY,
			"attached_to_name": doc.name,
			"file_name": file_name,
		},
		fields=["name", "file_url"],
		limit_page_length=0,
	)
	if existing and not overwrite:
		raise ToolError(
			f"a pay stub for {employee} on payroll entry {doc.name} is already attached at "
			f"{existing[0].get('file_url')}. The most likely thing there is the statement this "
			f"worker was already handed. Pass overwrite=true to render a fresh one; the "
			f"existing File stays attached to the record either way. Nothing was changed."
		)

	pdf = pay_stub_pdf.render_pay_stub(
		stub,
		_stub_company_info(doc.company, args),
		show_employer_contributions=bool(args.get("show_employer_contributions")),
	)
	attachment = artifacts.attach_bytes(PAYROLL_ENTRY, doc.name, file_name, pdf)

	data = {
		"payroll_entry": doc.name,
		"company": doc.company,
		"employee": employee,
		"employee_name": slip.get("employee_name"),
		"pay_period_start": str(doc.pay_period_start),
		"pay_period_end": str(doc.pay_period_end),
		"status": doc.status,
		"gross_pay": _num(slip.get("gross_pay")),
		"total_deductions": _num(slip.get("total_deductions")),
		"net_pay": _num(slip.get("net_pay")),
		"hourly_rate": stub["hourly_rate"] or None,
		"ytd": stub["ytd"] or None,
		"employer_contributions_shown": bool(args.get("show_employer_contributions")),
		"replaced": (existing[0].get("file_url") if existing else None),
		"file_url": attachment.get("file_url"),
		"file_name": file_name,
		"attachment": artifacts.describe_attachment(attachment, pdf),
		"note": _PAY_STUB_NOTE,
	}
	return ToolResult(
		data=data,
		summary=f"Pay stub for {slip.get('employee_name') or employee} on {doc.name} "
		f"({doc.pay_period_start} to {doc.pay_period_end}): net ${_num(slip.get('net_pay')):,.2f}, "
		f"rendered to {file_name} ({len(pdf):,} bytes) and attached"
		+ (f", replacing {existing[0].get('file_url')}" if existing else ""),
	)


#: Said on every stub result, because the page looks like a payroll record and
#: the two things a reader most needs to know about it are that it changed
#: nothing and that its year-to-date year is not this site's fiscal year.
_PAY_STUB_NOTE = (
	"Rendering a stub recomputes nothing and moves no status: every figure is read off the "
	"stored Farm Payroll Slip, and the run is still whatever it was. Year-to-date figures are "
	"the CALENDAR year through this period, which is what a W-2 is built from and is not "
	"necessarily this site's fiscal year."
)


def _stub_company_info(company: str, args: dict) -> dict:
	"""The employer block on the page: name, address and EIN.

	`company_address` is an argument for the reason `tools/taxforms._company_info`
	takes one — ERPNext keeps no address on Company, and an employer's address is
	something a wage statement is expected to carry.
	"""
	fields = compat.existing_fields("Company", ("name", "tax_id"))
	row = frappe.db.get_value("Company", company, fields, as_dict=True) or {}
	return {
		"name": company,
		"ein": str(row.get("tax_id") or "").strip(),
		"address": as_str(args, "company_address") or as_str(args, "employer_address"),
	}


# ── Internal helpers ──────────────────────────────────────────────────────


def _load_period_shifts(
	company: str | None,
	start: str,
	end: str,
	employees: list[str] | None = None,
) -> tuple[list[dict], dict]:
	"""Farm Shifts in the window, each with its crew and its piece counts.

	Returns the shift dicts in the shape `payroll_integration` reads, plus a
	provenance dict naming every source that answered and every source that was
	not on this site. The second half is not decoration: a piece-rate run that
	found no bucket log has produced a set of zeros, and whether that means
	nobody picked anything or means the bridge is not installed is the whole
	difference between a payroll and a mistake.
	"""
	provenance: dict = {"sources": [], "notes": []}

	if not compat.doctype_exists(FARM_SHIFT):
		provenance["notes"].append(
			f"The {FARM_SHIFT} doctype is not on this site, so no hours could be read from "
			"the shift register. Run `bench migrate` after installing v0.19.3 or later."
		)
		return [], provenance

	filters: list = [
		["start_datetime", ">=", f"{start} 00:00:00"],
		["start_datetime", "<=", f"{end} 23:59:59"],
	]
	if company:
		filters.append(["company", "=", company])

	# `pay_type` and `pay_rate` ship on Farm Shift as of v0.49.0 — the day a
	# picker spent on irrigation says so on the shift. Read through
	# `existing_fields` anyway: a bench running this code against a database it
	# has not migrated yet should lose the override, not the payroll run.
	shift_pay_fields = compat.existing_fields(FARM_SHIFT, ("pay_type", "pay_rate"))
	break_policy_fields = compat.existing_fields(FARM_SHIFT, ("break_policy",))

	rows = frappe.db.get_all(
		FARM_SHIFT,
		filters=filters,
		fields=[
			"name",
			"company",
			"work_state",
			"start_datetime",
			"end_datetime",
			"status",
			"cancelled",
			"shift_type",
			"location",
			*shift_pay_fields,
			*break_policy_fields,
		],
		order_by="start_datetime asc",
		limit_page_length=SHIFT_CAP,
	)

	kept = []
	cancelled = 0
	for row in rows:
		if compat.checked(row.get("cancelled")) or row.get("status") == "Cancelled":
			cancelled += 1
			continue
		kept.append(row)

	if len(rows) >= SHIFT_CAP:
		provenance["notes"].append(
			f"The shift read stopped at {SHIFT_CAP} records. This period covers more shifts "
			"than one payroll run should — check the dates before paying anything out."
		)
	if cancelled:
		provenance["notes"].append(
			f"{cancelled} cancelled shift(s) in this period were not counted. A cancelled "
			"shift is a shift that did not happen."
		)

	# A per-worker piece count on the crew row is not a column this app ships —
	# Farm Shift Crew Member records who and when, not how much. A site that has
	# added one is recording exactly the right thing in exactly the right place,
	# so it is read where it exists rather than ignored in favour of a bridge
	# doctype that may not be installed.
	units_field = compat.first_field(
		CREW_MEMBER,
		"piece_units",
		"units",
		"bins",
		"bucket_count",
		"quantity",
	)
	break_field = compat.first_field(CREW_MEMBER, "break_hours", "paid_break_hours")
	meal_field = compat.first_field(CREW_MEMBER, "unpaid_break_hours", "meal_break_hours")
	crew_pay_fields = compat.existing_fields(CREW_MEMBER, ("pay_type", "pay_rate"))

	names = [row["name"] for row in kept]
	crew_by_shift: dict[str, list[dict]] = {}
	if names:
		wanted = set(employees) if employees else None
		fields = ["parent", "employee", "employee_name", "joined_at", "left_at"]
		fields += [f for f in (units_field, break_field, meal_field) if f]
		fields += list(crew_pay_fields)
		for member in frappe.db.get_all(
			CREW_MEMBER,
			filters={"parent": ("in", names)},
			fields=fields,
			limit_page_length=0,
		):
			if not member.get("employee"):
				continue
			if wanted is not None and member["employee"] not in wanted:
				continue
			row = {
				"employee": member["employee"],
				"employee_name": member.get("employee_name") or "",
				"joined_at": member.get("joined_at"),
				"left_at": member.get("left_at"),
			}
			if units_field and member.get(units_field) not in (None, ""):
				row["piece_units"] = _num(member.get(units_field))
			if break_field and member.get(break_field) not in (None, ""):
				row["break_hours"] = _num(member.get(break_field))
			if meal_field and member.get(meal_field) not in (None, ""):
				row["unpaid_break_hours"] = _num(member.get(meal_field))
			# Only where the row actually says something. An empty pay type means
			# "paid the way this worker's structure says", which is every ordinary
			# day, and writing a blank onto the segment would be indistinguishable.
			if member.get("pay_type"):
				row["pay_type"] = str(member["pay_type"])
			if member.get("pay_rate") not in (None, "", 0, 0.0, "0"):
				row["pay_rate"] = _num(member.get("pay_rate"))
			crew_by_shift.setdefault(member["parent"], []).append(row)

	shifts = []
	for row in kept:
		crew = crew_by_shift.get(row["name"])
		if not crew:
			continue
		entry = {
			"name": row["name"],
			"company": row.get("company"),
			"work_state": row.get("work_state") or "",
			"shift_type": row.get("shift_type"),
			"location": row.get("location"),
			"start_datetime": row.get("start_datetime"),
			"end_datetime": row.get("end_datetime"),
			"crew": crew,
		}
		if row.get("pay_type"):
			entry["pay_type"] = str(row["pay_type"])
		if row.get("pay_rate") not in (None, "", 0, 0.0, "0"):
			entry["pay_rate"] = _num(row.get("pay_rate"))
		shifts.append(entry)

	# v0.58.0. Read compliance events with a break_kind, read the shift's
	# break_policy, and compute per-crew-row break hours. This is the wiring
	# that makes break_hours reach payroll_calc — the four lines the design doc
	# says close §0 finding 1.
	_attach_break_hours(shifts, kept, names)

	provenance["sources"].append(FARM_SHIFT)
	provenance["shift_count"] = len(shifts)

	piece_rows, piece_provenance = _load_piece_rows(company, start, end, employees)
	provenance["sources"].extend(piece_provenance["sources"])
	provenance["notes"].extend(piece_provenance["notes"])

	unmatched = _attach_piece_rows(shifts, piece_rows)
	if unmatched:
		shifts.extend(_orphan_piece_shifts(unmatched, shifts))
		provenance["piece_rows_without_a_shift"] = len(unmatched)
		provenance["notes"].append(
			f"{len(unmatched)} piece record(s) fell on a day the worker has no shift on. They "
			"are PAID — the buckets are in the barn either way — but they carry no hours, so "
			"they add nothing to the overtime count and nothing to the minimum wage divisor. "
			"A day of picking with no shift behind it is a gap in the shift register."
		)
	provenance["piece_row_count"] = len(piece_rows)

	return shifts, provenance


COMPLIANCE_EVENT = "Farm Shift Compliance Event"
BREAK_POLICY = "Labor Break Policy"


def _attach_break_hours(shifts_list: list, kept: list, names: list) -> None:
	"""Read break events and policies, compute per-crew-row break_hours.

	Writes `break_hours` and `unpaid_break_hours` onto each crew dict in place.
	Only runs when the compliance event doctype has a break_kind field.
	"""
	if not names or not compat.doctype_exists(COMPLIANCE_EVENT):
		return
	if not compat.first_field(COMPLIANCE_EVENT, "break_kind"):
		return

	break_fields = [
		"parent",
		"event_type",
		"event_datetime",
		"break_kind",
		"ended_at",
		"duration_minutes",
		"duration_source",
		"applies_to",
		"employee",
	]
	existing = compat.existing_fields(COMPLIANCE_EVENT, tuple(break_fields))
	if "break_kind" not in existing:
		return

	events_by_shift: dict[str, list[dict]] = {}
	for ev in frappe.db.get_all(
		COMPLIANCE_EVENT,
		filters={"parent": ("in", names), "break_kind": ("is", "set")},
		fields=list(existing),
		limit_page_length=0,
	):
		events_by_shift.setdefault(ev["parent"], []).append(dict(ev))

	if not events_by_shift:
		return

	policy_cache: dict[str, dict] = {}
	shift_policy: dict[str, str] = {}
	for row in kept:
		bp = row.get("break_policy")
		if bp:
			shift_policy[row["name"]] = bp

	for entry in shifts_list:
		shift_name = entry["name"]
		break_events = events_by_shift.get(shift_name)
		if not break_events:
			continue

		policy_name = shift_policy.get(shift_name, "")
		policy_dict: dict = {}
		if policy_name:
			if policy_name not in policy_cache:
				if compat.doctype_exists(BREAK_POLICY):
					try:
						pdoc = frappe.get_doc(BREAK_POLICY, policy_name)
						policy_cache[policy_name] = dict(pdoc.as_dict())
					except Exception:
						policy_cache[policy_name] = {}
				else:
					policy_cache[policy_name] = {}
			policy_dict = policy_cache[policy_name]

		if not policy_dict:
			continue

		for member in entry.get("crew") or []:
			if member.get("break_hours") not in (None, "", 0, 0.0):
				continue
			seg = {
				"employee": member.get("employee"),
				"joined_at": member.get("joined_at") or entry.get("start_datetime"),
				"left_at": member.get("left_at") or entry.get("end_datetime"),
			}
			wb = breaks_mod.worker_breaks(seg, break_events, policy_dict)
			if wb["paid_break_hours"] > 0:
				member["break_hours"] = wb["paid_break_hours"]
			if wb["unpaid_break_hours"] > 0:
				member["unpaid_break_hours"] = wb["unpaid_break_hours"]


def _load_piece_rows(
	company: str | None,
	start: str,
	end: str,
	employees: list[str] | None = None,
) -> tuple[list[dict], dict]:
	"""Piece counts for the period, from whichever source this site has.

	Two candidate sources, both optional and neither owned by this app, so both
	are inspected rather than assumed: the BucketLog bridge's Bucket Log Entry,
	and Farm Task Assignment where the site has added a count column to it. What
	is absent is named in the notes.
	"""
	provenance: dict = {"sources": [], "notes": []}
	rows: list[dict] = []
	wanted = set(employees) if employees else None

	rows.extend(_bucket_log_rows(company, start, end, wanted, provenance))
	rows.extend(_task_assignment_rows(company, start, end, wanted, provenance))
	return rows, provenance


def _bucket_log_rows(company, start, end, wanted, provenance) -> list[dict]:
	"""Piece counts off the BucketLog bridge, or a note saying why there are none."""
	if not compat.doctype_exists(BUCKET_LOG):
		provenance["notes"].append(
			f"The {BUCKET_LOG} doctype (the BucketLog bridge) is not on this site, so no "
			"bucket counts were read. Piece-rate pay for this period comes from whatever "
			"units were recorded elsewhere."
		)
		return []

	picker_field = compat.first_field(BUCKET_LOG, "picker_id", "employee", "picker", "worker")
	if not picker_field:
		provenance["notes"].append(
			f"{BUCKET_LOG} is installed but carries no picker column on this site, so a "
			"bucket cannot be attributed to anybody. `import_farm_app_fields` installs the "
			"five traceability columns, of which picker_id is one."
		)
		return []

	date_field = compat.first_field(
		BUCKET_LOG,
		"timestamp",
		"logged_at",
		"log_date",
		"date",
		"posting_date",
		"creation",
	)
	unit_field = compat.first_field(
		BUCKET_LOG,
		"piece_units",
		"units",
		"quantity",
		"qty",
		"bucket_count",
		"count",
		"bins",
	)
	# `verdict` is this app's own Bucket Log Entry field — bucket_bridge.py's
	# canonical name for "the ML model kept this one". A site running an older
	# or third-party bucket log with no such column has nothing to filter on,
	# and every row is read the way it always was: as a bucket, full stop.
	verdict_field = compat.first_field(BUCKET_LOG, "verdict")
	# `status` is this app's own Pending/Linked/Paid lifecycle — see
	# bucket_bridge.STATUS_PAID. A bucket already paid on an earlier run must
	# not be read again by a later or overlapping one, which is what
	# `_mark_bucket_entries_paid` below stamps once this run commits. A
	# third-party bucket log with no such column is read the way it always
	# was: there is nothing here to say a row was paid already.
	status_field = compat.first_field(BUCKET_LOG, "status")

	filters: list = []
	if date_field:
		filters.append([date_field, ">=", f"{start} 00:00:00"])
		filters.append([date_field, "<=", f"{end} 23:59:59"])
	if company and compat.has_field(BUCKET_LOG, "company"):
		filters.append(["company", "=", company])
	if status_field:
		filters.append([status_field, "!=", bucket_bridge.STATUS_PAID])
	if verdict_field:
		filters.append([verdict_field, "=", bucket_bridge.VERDICT_ACCEPTED])

	fields = ["name", picker_field]
	if date_field:
		fields.append(date_field)
	if unit_field:
		fields.append(unit_field)

	rows = frappe.db.get_all(
		BUCKET_LOG,
		filters=filters,
		fields=fields,
		limit_page_length=0,
	)

	out = []
	for row in rows:
		employee = str(row.get(picker_field) or "")
		if not employee or (wanted is not None and employee not in wanted):
			continue
		# A row with no count column IS one bucket. The row is the record of the
		# bucket, so counting it as one is reading it, not guessing at it.
		units = 1.0 if not unit_field else _num(row.get(unit_field), 1.0)
		out.append(
			{
				"employee": employee,
				"date": str(row.get(date_field) or "")[:10] if date_field else "",
				"units": units,
				"source": BUCKET_LOG,
				"entry_name": row.get("name"),
			}
		)

	provenance["sources"].append(BUCKET_LOG)
	if verdict_field:
		provenance["notes"].append(
			f"{BUCKET_LOG} carries a verdict column, so only Accepted captures were counted — a "
			"Rejected one is the model saying the bucket was not actually filled."
		)
	if not unit_field:
		provenance["notes"].append(
			f"{BUCKET_LOG} carries no count column on this site, so each entry was counted as "
			"ONE unit. That is what the record means — one row, one bucket — but a site that "
			"logs a bin per row is being paid per bin at the per-bucket rate."
		)
	return out


def _mark_bucket_entries_paid(employee: str, start: str, end: str, company: str | None) -> None:
	"""Stamp every Bucket Log Entry this run just paid as `status=Paid`.

	Without this, `_bucket_log_rows` above has nothing to exclude and a second
	run over an overlapping period — a correction, a re-preview turned into a
	commit, a semi-monthly run whose window laps a prior weekly one — reads and
	pays the same bucket twice. The filter is the same one `_bucket_log_rows`
	queried with (date window, company, verdict, not already Paid), scoped to
	this one employee so a slip only claims the buckets it actually paid for.
	Best-effort and silent about a site with no BucketLog bridge, for the same
	reason `_bucket_log_rows` is: an absent doctype is not a bug in this run.
	"""
	if not compat.doctype_exists(BUCKET_LOG):
		return
	picker_field = compat.first_field(BUCKET_LOG, "picker_id", "employee", "picker", "worker")
	status_field = compat.first_field(BUCKET_LOG, "status")
	if not picker_field or not status_field:
		return
	date_field = compat.first_field(
		BUCKET_LOG, "timestamp", "logged_at", "log_date", "date", "posting_date", "creation"
	)
	verdict_field = compat.first_field(BUCKET_LOG, "verdict")

	filters: list = [[picker_field, "=", employee], [status_field, "!=", bucket_bridge.STATUS_PAID]]
	if date_field:
		filters.append([date_field, ">=", f"{start} 00:00:00"])
		filters.append([date_field, "<=", f"{end} 23:59:59"])
	if company and compat.has_field(BUCKET_LOG, "company"):
		filters.append(["company", "=", company])
	if verdict_field:
		filters.append([verdict_field, "=", bucket_bridge.VERDICT_ACCEPTED])

	names = frappe.db.get_all(BUCKET_LOG, filters=filters, pluck="name", limit_page_length=0)
	for name in names:
		frappe.db.set_value(BUCKET_LOG, name, status_field, bucket_bridge.STATUS_PAID)


def _task_assignment_rows(company, start, end, wanted, provenance) -> list[dict]:
	"""Piece counts off completed Farm Task Assignments, where the site has them."""
	if not compat.doctype_exists(FARM_TASK_ASSIGNMENT):
		return []

	unit_field = compat.first_field(
		FARM_TASK_ASSIGNMENT,
		"piece_units",
		"units",
		"units_completed",
		"quantity",
		"qty",
		"bins",
		"bucket_count",
	)
	if not unit_field:
		provenance["notes"].append(
			f"{FARM_TASK_ASSIGNMENT} carries no piece-count column on this site, so completed "
			"field work contributed no units. Task completion records duration, not output; a "
			"site paying piece rate off tasks needs a count column on the assignment."
		)
		return []

	filters: list = [
		["completed_at", ">=", f"{start} 00:00:00"],
		["completed_at", "<=", f"{end} 23:59:59"],
		["state", "=", "Completed"],
	]
	if company:
		filters.append(["company", "=", company])

	rows = frappe.db.get_all(
		FARM_TASK_ASSIGNMENT,
		filters=filters,
		fields=["name", "assigned_to", "completed_at", unit_field],
		limit_page_length=0,
	)

	out = []
	for row in rows:
		employee = str(row.get("assigned_to") or "")
		if not employee or (wanted is not None and employee not in wanted):
			continue
		out.append(
			{
				"employee": employee,
				"date": str(row.get("completed_at") or "")[:10],
				"units": _num(row.get(unit_field), 0.0),
				"source": FARM_TASK_ASSIGNMENT,
			}
		)

	provenance["sources"].append(FARM_TASK_ASSIGNMENT)
	return out


def _attach_piece_rows(shifts: list[dict], piece_rows: list[dict]) -> list[dict]:
	"""Hang each piece row on the shift its worker was on that day.

	Matched by (employee, date) rather than by a link, because neither candidate
	source has one — a bucket log knows a picker and a timestamp, and the shift
	that timestamp fell inside is a join this app has to make for itself.

	Returns the rows that matched nothing, for the caller to report.
	"""
	index: dict[tuple, dict] = {}
	for shift in shifts:
		day = str(shift.get("start_datetime") or "")[:10]
		for member in shift.get("crew") or []:
			index.setdefault((member["employee"], day), shift)

	unmatched = []
	for row in piece_rows:
		shift = index.get((row["employee"], row["date"]))
		if shift is None:
			unmatched.append(row)
			continue
		shift.setdefault("piece_rows", []).append(
			{
				"employee": row["employee"],
				"piece_units": row["units"],
			}
		)
	return unmatched


def _orphan_piece_shifts(unmatched: list[dict], shifts: list[dict]) -> list[dict]:
	"""Zero-hour stand-in shifts so unmatched piece work is still paid.

	The alternative is dropping the units, and dropping somebody's buckets
	because the shift register has a hole in it would be paying them nothing for
	a day they worked. The stand-in carries the units and NO hours, which is the
	honest shape: the money is owed, the time was never recorded, and both facts
	survive into the slip.

	The work state is taken from where that worker spent their other hours in the
	period, because a bucket log does not carry one and the alternative is
	allocating the earnings to no state at all.
	"""
	dominant: dict[str, dict[str, float]] = {}
	for shift in shifts:
		state = shift.get("work_state") or ""
		if not state:
			continue
		for member in shift.get("crew") or []:
			tally = dominant.setdefault(member["employee"], {})
			tally[state] = tally.get(state, 0) + 1

	grouped: dict[tuple, float] = {}
	for row in unmatched:
		key = (row["employee"], row["date"])
		grouped[key] = grouped.get(key, 0.0) + row["units"]

	out = []
	for (employee, day), units in sorted(grouped.items()):
		tally = dominant.get(employee) or {}
		state = max(tally, key=tally.get) if tally else ""
		when = f"{day} 00:00:00" if day else None
		out.append(
			{
				"name": "",
				"work_state": state,
				"start_datetime": when,
				"end_datetime": when,
				"piece_units_only": True,
				"crew": [{"employee": employee, "hours": 0.0, "piece_units": units}],
			}
		)
	return out


def _load_shifts(employee: str, start: str, end: str, company: str | None = None) -> list[dict]:
	"""One employee's period, as the per-state rows `calculate_full_payroll` reads.

	The v0.30.0 entry point, rewired. It answers the same question with the same
	shape and gets it right now: per-worker spans rather than the crew's, weekly
	overtime rather than none, and piece units rather than zero.

	It still swallows its own failures. A payroll preview on a site whose shift
	register is half-migrated should report no hours, not refuse to run — the
	same posture the Attendance bridge takes on a close.
	"""
	try:
		shifts, _ = _load_period_shifts(company, start, end, employees=[employee])
		aggregates = payroll_integration.aggregate_shifts_for_period(shifts, start, end)
		aggregate = aggregates.get(employee)
		if not aggregate:
			return []
		return payroll_integration.engine_shift_rows(aggregate)
	except Exception:
		return []


def _load_deductions(
	company: str | None,
	on_date: str,
	employees: list[str] | None = None,
) -> tuple[dict, dict]:
	"""Every deduction in force on `on_date`, grouped by employee.

	Returns `(by_employee, provenance)`. The provenance half is the same idea as
	`_load_period_shifts`': a run that withheld nothing because nobody has an
	order and a run that withheld nothing because the register was never migrated
	produce identical slips, and only one of them is fine.

	SCOPED TO THE COMPANY, like every other read on a personnel file. A support
	order filed against a worker at one entity does not reach a cheque written by
	another, even where the same person works for both.

	FILTERED IN PYTHON RATHER THAN IN SQL, and deliberately: a row with no
	`effective_to` is in force forever, and `("<=", date)` on an empty column
	excludes it — so the obvious query would silently drop every open-ended
	support order, which is most of them. `payroll_deductions.is_active` is the
	one place that rule lives.
	"""
	if not compat.doctype_exists(PAYROLL_DEDUCTION):
		return {}, {
			"available": False,
			"reason": (
				"Farm Payroll Deduction is not on this site, so no garnishment or voluntary "
				"deduction was applied. It ships with erpnext_mcp — run `bench --site <site> "
				"migrate` after upgrading the app."
			),
			"count": 0,
		}

	filters = {}
	if company:
		filters["company"] = company
	if employees:
		filters["employee"] = ("in", list(employees))

	rows = frappe.db.get_all(
		PAYROLL_DEDUCTION,
		filters=filters,
		fields=list(compat.existing_fields(PAYROLL_DEDUCTION, _DEDUCTION_FIELDS)),
		limit=10000,
	)
	live = [row for row in rows if payroll_deductions.is_active(row, on_date)]

	by_employee: dict[str, list[dict]] = {}
	for row in live:
		by_employee.setdefault(str(row.get("employee") or ""), []).append(dict(row))

	return by_employee, {
		"available": True,
		"count": len(live),
		"on_file": len(rows),
		"in_force_on": on_date,
		"employees": sorted(by_employee),
		"garnishment_count": len([row for row in live if payroll_deductions.row_type(row) == "garnishment"]),
		"voluntary_count": len([row for row in live if payroll_deductions.row_type(row) == "voluntary"]),
	}


def _load_structures(company: str, employees: list[str] | None = None) -> dict:
	"""Active salary structures for a company, keyed by employee.

	Newest `effective_from` wins where a site has left two active. Deactivating
	the old one is what `deactivate_salary_structure` is for, but a payroll run
	is the wrong moment to refuse over it — the most recent rate is the one the
	employer meant, and the duplicate is a data problem for another tool.
	"""
	filters: dict = {"company": company, "is_active": 1}
	if employees:
		filters["employee"] = ("in", employees)

	rows = frappe.db.get_all(
		SALARY_STRUCTURE,
		filters=filters,
		fields=[
			"name",
			"employee",
			"employee_name",
			"pay_type",
			"base_rate",
			*compat.existing_fields(SALARY_STRUCTURE, ("hourly_rate",)),
			*_structure_activity_fields(),
			*_structure_region_fields(),
			"effective_from",
		],
		order_by="effective_from asc",
		limit_page_length=0,
	)

	structures: dict[str, dict] = {}
	for row in rows:
		employee = row.get("employee")
		if not employee:
			continue
		structures[employee] = {
			"name": row.get("name"),
			"employee": employee,
			"employee_name": row.get("employee_name") or "",
			"pay_type": row.get("pay_type") or "Hourly",
			"base_rate": _num(row.get("base_rate")),
			# Which piecework this worker's rate is for — the other half of the
			# (company, activity) pair the company-wide table is keyed by, and
			# read only where `base_rate` is 0. Blank is normal and is not a
			# problem on a site with one piecework rate in force.
			"piecework_activity": row.get("piecework_activity") or "",
			# What non-piece hours are worth to a piece-rate worker. None rather
			# than zero where the site has not set one: the engine treats None as
			# "no rate on file" and pays those hours up to the minimum wage, and it
			# should not be able to confuse that with a rate deliberately set to
			# nothing.
			"hourly_rate": row.get("hourly_rate"),
			# v0.63.0. Which of a state's geographic floors this worker's hours are
			# owed. Carried as the raw Select value rather than normalised here,
			# because `_period_run` is where the rate table exists to name states
			# against — see `_min_wage_regions`.
			"min_wage_region": row.get("min_wage_region"),
		}
	return structures


def _load_w4_map(employees: list[str]) -> dict:
	"""Each employee's active W-4, or the single-no-adjustments default."""
	return {employee: _load_w4_data(employee) for employee in employees}


def _load_state_configs(company: str | None = None) -> dict:
	"""Every supported state's tax configuration this company has."""
	configs = {}
	for state in SUPPORTED_STATES:
		config = _load_state_config(state, company)
		if config:
			configs[state] = config
	return configs


def _load_state_tax_tables(tax_year: int = 2025, filing_status: str = "Single") -> dict:
	"""State income tax brackets, for the states that have them.

	Only Oregon: Washington has no personal income tax, so there is no table to
	load and `state_withholding` never asks for one.
	"""
	tables = {}
	for state in SUPPORTED_STATES:
		if state != "OR":
			continue
		rows = _load_state_tax_table(state, tax_year, filing_status)
		if rows:
			tables[state] = rows
	return tables


def _load_federal_tables(pay_frequency: str, tax_year: int = 2025) -> dict:
	"""Federal brackets for every filing status, keyed by status.

	Loaded once per run rather than once per employee. A company of forty people
	is forty identical bracket reads otherwise, and they are the same three
	tables every time.
	"""
	tables = {}
	for status in sorted(set(FILING_STATUS_MAP.values())):
		rows = _load_federal_tax_table(tax_year, status, pay_frequency)
		if rows:
			tables[status] = rows
	return tables


def _load_ytd(company: str, period_start: str, employees: list[str]) -> dict:
	"""Year-to-date gross and Social Security withheld, before this period.

	The Social Security wage base is an annual per-person cap, so a period run
	in isolation would restart it and over-withhold on anybody who has already
	crossed $176,100. Only `Calculated` and `Submitted` entries count — a Draft
	payroll has not been paid and a Cancelled one was not, which is the same rule
	`tools/taxforms.py` applies for the same reason.
	"""
	year = str(period_start)[:4]
	wanted = set(employees)
	ytd: dict[str, dict] = {}

	entries = frappe.db.get_all(
		PAYROLL_ENTRY,
		filters={
			"company": company,
			"status": ("in", ("Calculated", "Submitted")),
			"pay_period_end": ("<", str(period_start)),
		},
		fields=["name", "pay_period_start", "pay_period_end"],
		order_by="pay_period_end asc",
		limit_page_length=0,
	)

	for entry in entries:
		if str(entry.get("pay_period_end") or "")[:4] != year:
			continue
		try:
			doc = frappe.get_doc(PAYROLL_ENTRY, entry["name"])
		except Exception:
			continue
		for row in doc.get("slips") or []:
			get = row.get if isinstance(row, dict) else (lambda k, d=None: getattr(row, k, d))
			employee = get("employee")
			if not employee or employee not in wanted:
				continue
			bucket = ytd.setdefault(employee, {"ytd_gross": 0.0, "ytd_ss_withheld": 0.0})
			bucket["ytd_gross"] += _num(get("gross_pay"))
			bucket["ytd_ss_withheld"] += _num(get("social_security"))

	return {employee: {k: round(v, 2) for k, v in bucket.items()} for employee, bucket in ytd.items()}


def _build_tax_config(employee: str, pay_frequency: str, company: str | None = None) -> dict:
	"""Build the tax_config dict needed by calculate_full_payroll."""
	w4_data = _load_w4_data(employee)
	fica_config = _load_fica_config()
	filing_status = w4_data.get("filing_status", "Single")
	tax_year = w4_data.get("_tax_year", 2025)
	federal_tax_table = _load_federal_tax_table(
		tax_year,
		filing_status,
		pay_frequency,
	)
	if not federal_tax_table:
		# `calculate_federal_withholding` treats an empty table as "no brackets
		# available" and returns $0 federal income tax — a silent wrong number
		# rather than a missing one, and the one direction a payroll calculation
		# must never guess in. Refusing here is what makes the failure loud
		# instead of a paycheck nobody can trust the withholding line on.
		raise ToolError(
			f"no Federal Tax Table rows for tax_year={tax_year}, filing_status="
			f"{filing_status!r}, payroll_period={pay_frequency!r}. import_federal_tax_table "
			"loads a year's brackets; nothing was calculated."
		)

	state_configs = {}
	state_tax_tables = {}
	for state in SUPPORTED_STATES:
		sc = _load_state_config(state, company)
		if sc:
			state_configs[state] = sc
			if state == "OR":
				st = _load_state_tax_table(state, w4_data.get("_tax_year", 2025), filing_status)
				if st:
					state_tax_tables[state] = st

	return {
		"w4_data": w4_data,
		"fica_config": fica_config,
		"federal_tax_table": federal_tax_table,
		"pay_frequency": pay_frequency,
		"ytd_gross": 0.0,
		"ytd_ss_withheld": 0.0,
		"state_configs": state_configs,
		"state_tax_tables": state_tax_tables,
		# v0.63.0. THE FLOOR, READ OFF THE SITE. `calculate_full_payroll` has taken
		# `min_wage_rates` since v0.49.0 and nothing ever supplied one, so every
		# run on every install used the table compiled into `payroll_calc` — which
		# meant an Oregon rate change was a release rather than a row. Built from
		# the SAME state configurations the withholding engines are handed, so what
		# a state charges and what it says an hour is worth cannot come from two
		# different rows.
		"min_wage_rates": state_min_wage_rates(state_configs),
	}


def _load_w4_data(employee: str) -> dict:
	"""Load the active W-4 for an employee."""
	name = frappe.db.get_value(
		W4_FORM,
		{"employee": employee, "status": "Active"},
		"name",
		order_by="tax_year desc, effective_date desc",
	)
	if not name:
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
			"_tax_year": 2025,
		}

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
	raw_status = row.get("filing_status", "Single or Married Filing Separately")
	table_status = FILING_STATUS_MAP.get(raw_status, "Single")

	return {
		"filing_status": table_status,
		"multiple_jobs": bool(int(row.get("multiple_jobs") or 0)),
		"additional_income_from_other_jobs": float(row.get("additional_income_from_other_jobs") or 0),
		"dependents_under_17_count": int(row.get("dependents_under_17_count") or 0),
		"other_dependents_count": int(row.get("other_dependents_count") or 0),
		"total_dependents_credit": float(row.get("total_dependents_credit") or 0),
		"other_income": float(row.get("other_income") or 0),
		"deductions": float(row.get("deductions") or 0),
		"extra_withholding_per_period": float(row.get("extra_withholding_per_period") or 0),
		"_tax_year": int(row.get("tax_year") or 2025),
	}


def _load_fica_config() -> dict:
	"""Load FICA configuration as a plain dict."""
	try:
		doc = frappe.get_doc(FICA_CONFIG)
	except Exception:
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


def _load_federal_tax_table(tax_year: int, filing_status: str, pay_frequency: str) -> list[dict]:
	"""Load federal tax brackets."""
	rows = frappe.db.get_all(
		FEDERAL_TAX_TABLE,
		filters={
			"tax_year": tax_year,
			"filing_status": filing_status,
			"payroll_period": pay_frequency,
		},
		fields=["bracket_floor", "bracket_ceiling", "base_tax", "marginal_rate"],
		order_by="bracket_floor asc",
	)
	return [dict(r) for r in rows]


def _load_state_config(state: str, company: str | None = None) -> dict | None:
	"""Load state tax configuration."""
	filters = {"state": state, "status": "Active"}
	if company:
		filters["company"] = company

	name = frappe.db.get_value(STATE_TAX_CONFIG, filters, "name")
	if not name:
		return None

	doc = frappe.get_doc(STATE_TAX_CONFIG, name)
	return doc.as_dict()


def _load_state_tax_table(state: str, tax_year: int, filing_status: str) -> list[dict]:
	"""Load state income tax brackets."""
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
	return [dict(r) for r in rows]

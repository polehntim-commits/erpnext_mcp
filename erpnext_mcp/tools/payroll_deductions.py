# SPDX-License-Identifier: MIT
"""The register of standing instructions to withhold money that is not a tax.

WHY THIS IS A REGISTER AND NOT A COLUMN ON THE SALARY STRUCTURE. A worker has
one pay rate and may have five deductions at once — a support order, a levy, a
401(k) election, a health premium and union dues — each with its own start date,
its own end date, its own reference document and its own legal rank. Columns
would cap the count and lose the dates, and the dates are the whole of what
makes a garnishment defensible: an employer answers to the court for what it
withheld and when it started withholding it.

WHY IT IS NOT A DESK-ONLY FORM. A record only reachable through the Desk cannot
be audited through MCP Action Log and cannot be included in a compliance packet.
The five tools here are the same five guards every HR tool in this app wears —
role gate, company scope, kill switch, audit row, and mutating-default-OFF.

WHAT THESE TOOLS DO NOT DO. They do not withhold anything. A row here says what
to take and from when; each payroll run reads it and decides what it can
actually take out of that period's pay, against the CCPA ceilings and in legal
priority order. The two are different numbers whenever a ceiling binds, and the
difference lands on the slip as a shortfall rather than being written back here.
`erpnext_mcp.payroll_deductions` owns the arithmetic and the statute behind each
ceiling; this module owns the file.

NOTHING HERE DELETES. `status` retires a row — Completed for an order that has
been satisfied, Suspended for one a court has stayed. A garnishment removed from
the file cannot answer the court that asks why the withholding stopped, and a
satisfied order is the record proving it was paid off.
"""

import frappe

from .. import compat, payroll_deductions
from ..args import as_bool, as_choice, as_date, as_float, as_int, as_limit, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult
from .employee import require_hr_role

PAYROLL_DEDUCTION = "Farm Payroll Deduction"
EMPLOYEE = "Employee"

#: What a caller may set on create, and the same list on update. Declared rather
#: than inferred from the doctype's meta, for the reason the fourteen-field
#: allowlist on the Employee writers exists: a personnel-file writer that accepts
#: whatever it is handed will one day write a field somebody added for another
#: purpose. `employee` and `company` are absent deliberately — see `update`.
_WRITABLE = (
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
	# v0.292.0. Arrears stated on the order, the order's own catch-up amount per period, and a Washington
	# consumer-debt judgment (RCW 6.27.150(4)).
	"arrears_opening",
	"arrears_per_period",
	"consumer_debt",
	"effective_from",
	"effective_to",
	"reference",
	"label",
	"notes",
)

#: The Selects, and the field each one is validated against. `as_choice` matches
#: case-insensitively and hands back the site's own casing, so what is stored is
#: what a list-view filter will look for.
_SELECTS = ("deduction_type", "deduction_category", "status", "amount_type", "basis")

#: The Checks. Kept apart because a Check that was never passed has to stay
#: unset rather than become 0 — the engine reads "unset" as "follow the
#: category", and writing a 0 would silently turn a health premium post-tax.
_CHECKS = ("pre_tax", "fica_exempt", "supports_other_dependents", "arrears_over_12_weeks", "consumer_debt")

_FIELDS = (
	"name",
	"employee",
	"employee_name",
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
	"arrears_opening",
	"arrears_per_period",
	"consumer_debt",
	"arrears_balance",
	"effective_from",
	"effective_to",
	"reference",
	"label",
	"notes",
	"creation",
	"modified",
	"owner",
)


def _require() -> None:
	compat.require_doctype(
		PAYROLL_DEDUCTION,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)


def _resolve_employee(value: str, label: str = "employee") -> str:
	"""An Employee docname from a docname or from a name somebody typed.

	Refuses an ambiguous name rather than picking one. Two workers called Maria
	Lopez is the ordinary case on a farm crew, and garnishing the wrong one is
	not a mistake that shows up in a reconciliation — it shows up when somebody's
	pay is short and they have no order against them.
	"""
	value = str(value or "").strip()
	if not value:
		raise ToolError(f"{label} is required.")
	if frappe.db.exists(EMPLOYEE, value):
		return value

	matches = frappe.db.get_all(
		EMPLOYEE,
		filters={"employee_name": ("like", f"%{value}%")},
		pluck="name",
		limit=10,
	)
	if not matches:
		raise ToolError(f"no Employee matches {value!r}. Nothing was changed.")
	if len(matches) > 1:
		named = ", ".join(matches[:5])
		raise ToolError(
			f"{value!r} matches {len(matches)} employees ({named}). Pass the docname — "
			"withholding from the wrong person is not an error that shows up in a "
			"reconciliation. Nothing was changed."
		)
	return matches[0]


def _describe(row: dict) -> dict:
	"""One deduction as a caller reads it, with what the engine will make of it.

	The stored row plus the DERIVED facts — the effective priority, whether it is
	pre-tax, whether it also leaves the FICA base, and which ceiling governs it.
	Those four are what actually decide the money and none of them is necessarily
	on the row: they fall back to the category. A reader who had to re-derive
	them would be reimplementing the engine to answer "what will this do".
	"""
	described = dict(row)
	described["effective_priority"] = payroll_deductions.row_priority(row)
	described["effective_type"] = payroll_deductions.row_type(row)
	described["effective_pre_tax"] = payroll_deductions.row_is_pre_tax(row)
	described["effective_fica_exempt"] = payroll_deductions.row_is_fica_exempt(row)
	described["pay_stub_label"] = payroll_deductions.row_label(row)
	described["limit_rule"] = payroll_deductions._limit_of(row)
	described["in_force_today"] = payroll_deductions.is_active(row, frappe.utils.today())
	# Does the stored category actually name one this app knows? `_key` falls back
	# to `other` rather than raising, which is right for a payroll run — a
	# category nobody recognises is not worth refusing a whole company's pay over
	# — but the fallback is SILENT, and the place it stops being silent is here.
	# See `_notes` for what it costs when it is not caught.
	described["deduction_category_recognised"] = _recognised(row)
	return described


def _recognised(row: dict) -> bool:
	"""Is the stored category one of the twelve, or is it falling back to `other`?

	A row whose category is genuinely `Other` is recognised; one that says `401k`
	is not, and the two are indistinguishable downstream because both resolve to
	the same spec.
	"""
	raw = str(row.get("deduction_category") or "").strip()
	if not raw:
		return False
	return payroll_deductions._key(raw, payroll_deductions.CATEGORIES, "") != ""


def _notes(row: dict) -> list[str]:
	"""What is worth saying about a row that saved cleanly.

	Warnings rather than refusals, because each of these is legitimate somewhere
	and wrong almost everywhere, and refusing would block the case that is real.
	"""
	notes = []
	category = payroll_deductions._key(
		row.get("deduction_category"),
		payroll_deductions.CATEGORIES,
		"other",
	)
	kind = payroll_deductions.row_type(row)

	# FIRST, because it changes what every other note below is talking about: an
	# unrecognised category resolves to the `other` spec, which decides the
	# priority, the ceiling and the pre-tax treatment — so the row is being
	# processed as something other than what somebody meant.
	#
	# THE COST IS PAID ON A WORKER'S PAY STATEMENT. `row_label` falls back to the
	# spec's label, so a row filed as `401k` prints as an unlabelled "Other" line
	# with a real amount against it, indistinguishable from a genuine `Other`
	# deduction by the time it is on the page.
	#
	# `create_payroll_deduction` and `update_payroll_deduction` REFUSE this by
	# name — `as_choice` validates against the doctype's own Select — and the
	# Desk field is a Select too. So a row that reaches here unrecognised was
	# written around both: a bench console, a patch, or a data import. That is
	# exactly the row nobody is watching, which is why it is said on every read
	# rather than only at write time.
	if not _recognised(row):
		notes.append(
			f"Category {str(row.get('deduction_category') or '')!r} is not one this app "
			f"recognises, so it is being processed as 'Other' — priority "
			f"{payroll_deductions.category_spec('other')['priority']}, no statutory ceiling of "
			"its own, and post-tax. It will also print as an unlabelled 'Other' line on a pay "
			"stub. The tools refuse this at write time, so this row was written around them "
			f"(a patch, an import, or the console). Valid categories: "
			f"{', '.join(payroll_deductions.CATEGORIES)}."
		)

	if kind == "garnishment" and not str(row.get("reference") or "").strip():
		notes.append(
			"No reference recorded. A garnishment is defended by the order it was made "
			"under — the court case number, the levy notice — and an employer asked why "
			"it withheld has nothing else to point at."
		)
	if category == "tax_levy" and not float(row.get("exempt_amount") or 0):
		notes.append(
			"No exempt amount recorded on a tax levy. IRC 6334(d) leaves the employee an "
			"exempt amount the IRS computes from filing status and dependents and prints on "
			"the levy notice (Publication 1494). Left at zero the levy takes what it asks "
			"for, bounded only by the pay."
		)
	if category == "retirement_401k" and row.get("fica_exempt"):
		notes.append(
			"This 401(k) is marked exempt from FICA. A traditional elective deferral is "
			"exempt from income tax and STAYS in the Social Security and Medicare wage base "
			"(IRC 402(e)(3) defers the income tax; 3121(v)(1)(A) keeps it in FICA). Unless "
			"this is a Roth or a plan with an unusual structure, this under-withholds FICA "
			"on every deferral."
		)
	# A percentage with no ceiling is the one that surprises somebody in a
	# harvest week: the same rate against a 70-hour cheque is a much larger
	# number, and a plan usually caps it per period.
	if str(row.get("amount_type") or "") == "Percentage" and not float(row.get("max_per_period") or 0):
		notes.append(
			"A percentage with no Maximum Per Period. On a harvest week with heavy "
			"overtime the same rate withholds a much larger amount; set a cap if the plan "
			"or the order has one."
		)
	if not row.get("effective_to") and kind == "voluntary":
		notes.append("Open-ended: this is withheld every period until its status changes.")
	return notes


# ── reads ───────────────────────────────────────────────────────────────────


def arrears_view(name: str, row: dict | None = None) -> dict:
	"""v0.292.0. A garnishment's arrears now and every catch-up and carry-forward in its ledger."""
	from .. import garnishment_ledger

	row = row or {}
	if payroll_deductions.row_type(row) != "garnishment" or not garnishment_ledger.ready():
		return {}
	return {"arrears_opening": row.get("arrears_opening") or 0, "arrears_per_period": row.get("arrears_per_period") or 0,
	        "balance": garnishment_ledger.balance(name, row.get("arrears_opening")),
	        "ledger": garnishment_ledger.report(deduction=name)}


def get_payroll_deduction(args: dict) -> ToolResult:
	"""One deduction in full, with what the payroll engine will make of it.

	v0.94.0: `require_hr_role`. THE WRITES IN THIS MODULE ALWAYS CARRIED IT AND
	THE THREE READS DID NOT, which made the register readable by any principal
	the transport let through while only HR could change it. Closing it here
	rather than only in the mobile wrapper is what closes the MCP path too: a
	wrapper gate protects one transport, and this module is reachable from both.
	"""
	_require()
	require_hr_role()
	name = as_str(args, "deduction", required=True)
	if not frappe.db.exists(PAYROLL_DEDUCTION, name):
		raise ToolError(f"no Farm Payroll Deduction called {name!r}.")

	row = frappe.db.get_value(
		PAYROLL_DEDUCTION,
		name,
		compat.existing_fields(PAYROLL_DEDUCTION, _FIELDS),
		as_dict=True,
	)
	described = _describe(dict(row))
	data = {"deduction": described}
	arrears = arrears_view(name, dict(row))
	if arrears:
		data["arrears"] = arrears
	notes = _notes(dict(row))
	if notes:
		data["notes"] = notes
	return ToolResult(
		data=data,
		summary=(
			f"read deduction {name} ({described.get('deduction_category')} for "
			f"{described.get('employee_name') or described.get('employee')})"
		),
	)


def list_payroll_deductions(args: dict) -> ToolResult:
	"""The register, filtered — by employee, type, category, status or reference.

	COMPANY-SCOPED like everything else that reads a personnel file. The totals
	beside the rows are per period and are what the deductions ASK for, not what
	a run will take: the ceilings are a property of each employee's pay and this
	list does not have anybody's pay in front of it.

	v0.94.0: `require_hr_role`. THE WRITES IN THIS MODULE ALWAYS CARRIED IT AND
	THE THREE READS DID NOT, which made the register readable by any principal
	the transport let through while only HR could change it. Closing it here
	rather than only in the mobile wrapper is what closes the MCP path too: a
	wrapper gate protects one transport, and this module is reachable from both.
	"""
	_require()
	require_hr_role()
	company = resolve_company(as_str(args, "company"))
	filters = {}
	if company:
		filters["company"] = company
	if args.get("employee"):
		filters["employee"] = _resolve_employee(as_str(args, "employee"))
	for key in ("deduction_type", "deduction_category", "status"):
		value = as_str(args, key)
		if value:
			filters[key] = as_choice(PAYROLL_DEDUCTION, key, value, key)
	reference = as_str(args, "reference")
	if reference:
		filters["reference"] = ("like", f"%{reference}%")

	# A date makes this "what was in force then", which is the question an audit
	# asks — and it cannot be a filter, because a row with no `effective_to` is
	# in force forever and `("<=", date)` on an empty column excludes it.
	on_date = as_date(args, "in_force_on")

	rows = frappe.db.get_all(
		PAYROLL_DEDUCTION,
		filters=filters,
		fields=list(compat.existing_fields(PAYROLL_DEDUCTION, _FIELDS)),
		order_by="employee_name asc, effective_from desc",
		limit=as_limit(args),
	)
	if on_date:
		rows = [row for row in rows if payroll_deductions.is_active(row, on_date)]

	described = [_describe(dict(row)) for row in rows]
	active = [row for row in described if row["status"] == "Active"]
	return ToolResult(
		data={
			"deductions": described,
			"count": len(described),
			"active_count": len(active),
			"garnishment_count": len([r for r in active if r["effective_type"] == "garnishment"]),
			"voluntary_count": len([r for r in active if r["effective_type"] == "voluntary"]),
			"employees_with_deductions": sorted({row["employee"] for row in active}),
			"company": company or "",
			"in_force_on": on_date or "",
		},
		summary=f"listed {len(described)} payroll deductions",
	)


def list_employee_deductions(args: dict) -> ToolResult:
	"""Everything standing against one worker's pay, in the order it comes out.

	THE ORDER IS THE ANSWER. Sorted the way a payroll run will process them —
	child support, then tax levies, then student loans, then other garnishments,
	then the voluntary elections — so the list reads as what will actually happen
	rather than as a set of rows somebody has to rank.

	`gross_pay` and `pay_frequency` turn this from a list into a PREVIEW: given a
	period's pay it prices each line against the CCPA ceilings and shows what
	would be withheld and what would fall short. Without them the amounts are
	what each order asks for, which for a percentage is not yet a number.

	v0.94.0: `require_hr_role`. THE WRITES IN THIS MODULE ALWAYS CARRIED IT AND
	THE THREE READS DID NOT, which made the register readable by any principal
	the transport let through while only HR could change it. Closing it here
	rather than only in the mobile wrapper is what closes the MCP path too: a
	wrapper gate protects one transport, and this module is reachable from both.
	"""
	require_hr_role()
	_require()
	employee = _resolve_employee(as_str(args, "employee", required=True))
	on_date = as_date(args, "in_force_on") or frappe.utils.today()
	include_inactive = as_bool(args, "include_inactive", False)

	filters = {"employee": employee}
	rows = frappe.db.get_all(
		PAYROLL_DEDUCTION,
		filters=filters,
		fields=list(compat.existing_fields(PAYROLL_DEDUCTION, _FIELDS)),
		limit=500,
	)
	live = payroll_deductions.active_deductions(rows, on_date)
	shown = (
		live
		if not include_inactive
		else sorted(
			[dict(row) for row in rows],
			key=lambda row: (payroll_deductions.row_priority(row), str(row.get("effective_from") or "")),
		)
	)

	data = {
		"employee": employee,
		"employee_name": frappe.db.get_value(EMPLOYEE, employee, "employee_name") or employee,
		"in_force_on": on_date,
		"deductions": [_describe(dict(row)) for row in shown],
		"active_count": len(live),
		"total_count": len(rows),
	}

	gross = as_float(args.get("gross_pay"), "gross_pay")
	if gross > 0:
		# A preview, and it says so. The statutory withholding is NOT computed
		# here — this module has no W-4, no bracket table and no state
		# configuration — so the caller supplies it, and where they do not the
		# disposable earnings figure is gross itself, which OVERSTATES what a
		# garnishment could take. Named rather than hidden, because a preview
		# that quietly assumed zero tax would price a support order high.
		pay_frequency = as_str(args, "pay_frequency") or "Biweekly"
		statutory = as_float(args.get("statutory_withholding"), "statutory_withholding")
		pre_tax = payroll_deductions.calculate_pre_tax_deductions(gross, live)
		disposable = payroll_deductions.disposable_earnings(gross, statutory)
		# v0.292.0. The same dated limits, catch-up policy and arrears balances a run uses. The state's own rule
		# applies when `work_state` is given.
		from .. import garnishment_ledger, payroll_settings

		limits = payroll_settings.garnishment_limits(on_date, as_str(args, "work_state").upper())
		garnishments = payroll_deductions.apply_garnishments(
			disposable, garnishment_ledger.with_balances([dict(r) for r in live]), pay_frequency,
			limits={"federal": limits["federal"], "state": limits["state"]},
			policy=payroll_settings.garnishment_policy(on_date))
		cash = max(gross - pre_tax["total"] - statutory - garnishments["total"], 0.0)
		post_tax = payroll_deductions.apply_post_tax_deductions(cash, live, gross, disposable)
		lines = pre_tax["lines"] + garnishments["lines"] + post_tax["lines"]

		data["preview"] = {
			"gross_pay": round(gross, 2),
			"pay_frequency": pay_frequency,
			"statutory_withholding": round(statutory, 2),
			"statutory_withholding_supplied": bool(statutory),
			"disposable_earnings": disposable,
			"federal_taxable_gross": pre_tax["federal_taxable_gross"],
			"fica_taxable_gross": pre_tax["fica_taxable_gross"],
			"lines": lines,
			"ccpa": garnishments["ccpa"],
			"child_support": garnishments["child_support"],
			"shortfalls": pre_tax["shortfalls"] + garnishments["shortfalls"] + post_tax["shortfalls"],
			**payroll_deductions.summarize_deductions(lines),
			"estimated_net_pay": round(
				max(gross - statutory - payroll_deductions.summarize_deductions(lines)["total"], 0.0),
				2,
			),
		}
		if not statutory:
			data["preview"]["basis_warning"] = (
				"No statutory_withholding was supplied, so disposable earnings were taken as "
				"gross pay. That OVERSTATES what a garnishment may take — the CCPA measures "
				"its ceilings against pay after the legally required withholding. Pass the "
				"period's federal, state, Social Security and Medicare total for a real figure, "
				"or read the slip a payroll run produces."
			)

	return ToolResult(
		data=data,
		summary=(
			f"listed {len(shown)} deductions for {data['employee_name']} ({len(live)} in force on {on_date})"
		),
	)


# ── writes ──────────────────────────────────────────────────────────────────


def create_payroll_deduction(args: dict) -> ToolResult:
	"""File one standing instruction to withhold against one worker's pay."""
	_require()
	actor = require_hr_role()
	company = resolve_company(as_str(args, "company"), required=True)
	employee = _resolve_employee(as_str(args, "employee", required=True))

	doc = frappe.new_doc(PAYROLL_DEDUCTION)
	doc.employee = employee
	doc.company = company
	_apply(doc, args, creating=True)

	# A second live order with the same reference against the same worker is
	# almost always the same order filed twice — a support order re-keyed on a
	# hire day, a levy entered by two people. Withholding it twice is money
	# actually taken from somebody, so it is refused by name rather than warned
	# about. A genuine second order has a different case number.
	reference = str(doc.reference or "").strip()
	if reference:
		duplicate = frappe.db.get_value(
			PAYROLL_DEDUCTION,
			{
				"employee": employee,
				"reference": reference,
				"status": "Active",
				"deduction_category": doc.deduction_category,
			},
			"name",
		)
		if duplicate:
			raise ToolError(
				f"{employee} already has an active {doc.deduction_category} deduction with "
				f"reference {reference!r} ({duplicate}). Filing it twice would withhold it "
				"twice. Update that row, or use a different reference if this really is a "
				"second order. Nothing was created."
			)

	doc.insert(ignore_permissions=True)
	row = dict(doc.as_dict())
	described = _describe(row)

	data = {"deduction": described, "actor": actor}
	notes = _notes(row)
	if notes:
		data["notes"] = notes
	return ToolResult(
		data=data,
		summary=(
			f"filed {doc.deduction_category} deduction {doc.name} against "
			f"{doc.employee_name or employee} from {doc.effective_from}"
		),
		docstatus_delta="none → 0 (created)",
	)


def update_payroll_deduction(args: dict) -> ToolResult:
	"""Change a filed deduction. Every change is echoed back as before → after.

	CANNOT RE-KEY IT. `employee` and `company` are not writable: a deduction
	moved to another worker would take a court order made against one person and
	apply it to another, and the audit trail would show a row that had quietly
	always been theirs. Retire this one and file a new one.

	SUSPENDING AND COMPLETING ARE STATUS CHANGES, NOT DELETIONS. See the module
	docstring — a garnishment removed from the file cannot answer the court that
	asks why the withholding stopped.
	"""
	_require()
	actor = require_hr_role()
	name = as_str(args, "deduction", required=True)
	if not frappe.db.exists(PAYROLL_DEDUCTION, name):
		raise ToolError(f"no Farm Payroll Deduction called {name!r}. Nothing was changed.")

	for locked in ("employee", "company"):
		if args.get(locked):
			raise ToolError(
				f"{locked} cannot be changed on a filed deduction. Moving one to another "
				"worker would apply an order made against one person to somebody else, and "
				"leave an audit trail saying it had always been theirs. Retire this row "
				"(status Completed) and file a new one. Nothing was changed."
			)

	doc = frappe.get_doc(PAYROLL_DEDUCTION, name)
	before = dict(doc.as_dict())
	changed = _apply(doc, args, creating=False)
	if not changed:
		raise ToolError(
			f"nothing to change: no writable field was supplied. Writable fields are {', '.join(_WRITABLE)}."
		)

	doc.save(ignore_permissions=True)
	row = dict(doc.as_dict())
	described = _describe(row)

	changes = [
		{"field": field, "before": before.get(field), "after": row.get(field)}
		for field in changed
		if before.get(field) != row.get(field)
	]
	data = {"deduction": described, "changes": changes, "actor": actor}
	notes = _notes(row)
	if notes:
		data["notes"] = notes
	return ToolResult(
		data=data,
		summary=(
			f"updated deduction {name} ({len(changes)} field{'' if len(changes) == 1 else 's'} changed)"
		),
		docstatus_delta="0 → 0 (amended)",
	)


def _apply(doc, args: dict, creating: bool) -> list[str]:
	"""Set every writable field the caller supplied. Returns the ones it touched.

	The allowlist is `_WRITABLE` and nothing else is honoured — a writer touching
	a personnel file declares what it writes and refuses the rest by name, which
	is the model every HR tool in this app follows.
	"""
	touched = []

	for field in _WRITABLE:
		if field not in args or args.get(field) in (None, ""):
			continue

		if field in _SELECTS:
			doc.set(field, as_choice(PAYROLL_DEDUCTION, field, as_str(args, field), field))
		elif field in _CHECKS:
			value = as_bool(args, field)
			if value is None:
				continue
			doc.set(field, 1 if value else 0)
		elif field in ("effective_from", "effective_to"):
			doc.set(field, as_date(args, field))
		elif field in ("amount", "max_per_period", "exempt_amount", "arrears_opening", "arrears_per_period"):
			doc.set(field, as_float(args.get(field), field))
		elif field == "priority":
			doc.set(field, as_int(args, field) or 0)
		else:
			doc.set(field, as_str(args, field))
		touched.append(field)

	if creating:
		# The defaults that make a row valid without asking a caller to restate
		# what the category already implies. `deduction_type` comes off the
		# CATEGORY and is set whenever the caller did not name one — tested on
		# the caller's arguments rather than on `doc.deduction_type`, because
		# `frappe.new_doc` has already filled that in from the doctype's own
		# default and a truthiness check would therefore never fire. A support
		# order created without a type would then be filed as a voluntary
		# election: behind the union dues in the queue and outside the ceiling
		# that governs it.
		if "deduction_type" not in touched:
			spec = payroll_deductions.category_spec(doc.deduction_category)
			doc.deduction_type = spec["deduction_type"].title()
		if not doc.status:
			doc.status = "Active"
		if not doc.amount_type:
			doc.amount_type = "Fixed"
		if not doc.effective_from:
			doc.effective_from = frappe.utils.today()

	return touched

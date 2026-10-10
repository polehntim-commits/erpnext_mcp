# SPDX-License-Identifier: MIT
"""The file of court orders this employer has been served with.

WHAT THIS IS AND WHAT `payroll_deductions.py` IS. A Farm Payroll Deduction is a
standing instruction — take this much, from this date, under this ceiling. That
is everything a payroll run needs and nothing a court needs. A Farm Garnishment
is the ORDER: the case number, who issued it, when the employer was served, what
the whole debt is, how much of it has been collected, and the letter that went
back saying withholding had begun.

ONE CALL FILES BOTH. `create_garnishment` inserts the order and then creates its
deduction THROUGH `create_payroll_deduction` — the same writer, the same
allowlist, the same duplicate refusal, the same category-to-law mapping. A
second deduction writer here would be a second place for the CCPA ceilings to be
got wrong, and the ceilings are the whole of what makes this defensible.

WHICH SWITCH GOVERNS THAT. `allow_create_garnishment`, and only that one.
Filing an order and instructing payroll to honour it are one act — an operator
who enabled the first and got a record that withholds nothing would have a
garnishment on the file that the payroll run cannot see, which is the failure
this doctype exists to prevent. The tool says so in its own description rather
than leaving an operator to infer it.

────────────────────────────────────────────────────────────────────────────
THE PRIORITY FIELD IS NOT THE ENGINE'S PRIORITY FIELD
────────────────────────────────────────────────────────────────────────────

Federal precedence is child support 1, tax levy 2, student loan 3, creditor 4,
and the doctype derives it from the type on every save. The payroll engine sorts
by the DEDUCTION's category — 10, 20, 30, 40 — and `_deduction_payload` passes
no priority at all so the category speaks. Pushing 1..4 across would put a
creditor order at 4 ahead of a support order at 10 and invert the exact
precedence the field records. The two sequences agree about the order and
disagree about the integers; this module is where that is known.

────────────────────────────────────────────────────────────────────────────
WHAT STOPS THE MONEY
────────────────────────────────────────────────────────────────────────────

`total_withheld` is the running sum of what payroll actually took, which is less
than what the order asked for on any period where a ceiling bound. When it
reaches `total_owed` the controller sets the status to Satisfied and its
`on_update` retires the linked deduction. Withholding past a satisfied judgment
is money taken under an authority that has expired, and it is the employer that
took it.

A ZERO `total_owed` IS NOT A SATISFIED DEBT. It is a field nobody filled in, and
every child support order is one — an ongoing obligation with no principal to run
down. The arithmetic only runs where a balance exists; see the controller.
"""

import frappe

from .. import compat, garnishment_response_pdf
from ..args import as_bool, as_choice, as_date, as_float, as_limit, as_str, resolve_company
from ..erpnext_mcp.doctype.farm_garnishment.farm_garnishment import (
	DEDUCTION_CATEGORY,
	PRIORITY_BY_TYPE,
	STATUTORY_CEILING,
)
from ..errors import ToolError
from ..result import ToolResult
from . import artifacts
from .employee import require_company_scope, require_hr_role
from .payroll_deductions import create_payroll_deduction

GARNISHMENT = "Farm Garnishment"
PAYROLL_DEDUCTION = "Farm Payroll Deduction"
EMPLOYEE = "Employee"

#: What a caller may set, on create and on update alike. Declared rather than
#: inferred from the doctype's meta, for the reason every personnel-file writer
#: in this app declares one: a writer that accepts whatever it is handed will one
#: day write a field somebody added for another purpose. `employee` and
#: `company` are absent deliberately — see `update_garnishment`.
_WRITABLE = (
	"garnishment_type",
	"status",
	"case_number",
	"issuing_court_or_agency",
	"received_date",
	"effective_date",
	"withholding_type",
	"withholding_amount",
	"max_disposable_earnings_percentage",
	"total_owed",
	"total_withheld",
	"notes",
)

_SELECTS = ("garnishment_type", "status", "withholding_type")
_DATES = ("received_date", "effective_date")
_FLOATS = (
	"withholding_amount",
	"max_disposable_earnings_percentage",
	"total_owed",
	"total_withheld",
)

#: The two fields that, changed here, have to reach the deduction as well. An
#: order that says $200 and a payroll run that takes $150 is not a discrepancy
#: somebody will notice — both numbers look deliberate.
_MIRRORED = ("withholding_amount", "withholding_type")

_FIELDS = (
	"name",
	"employee",
	"employee_name",
	"company",
	"garnishment_type",
	"priority",
	"status",
	"case_number",
	"issuing_court_or_agency",
	"received_date",
	"effective_date",
	"withholding_type",
	"withholding_amount",
	"max_disposable_earnings_percentage",
	"payroll_deduction",
	"total_owed",
	"total_withheld",
	"remaining_balance",
	"satisfied_on",
	"response_letter",
	"response_letter_on",
	"notes",
	"creation",
	"modified",
	"owner",
)

_REQUIRES = "the Farm Garnishment DocType, which ships with erpnext_mcp — run `bench migrate`"


def _require() -> None:
	compat.require_doctype(
		GARNISHMENT,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)


def _resolve_employee(value: str, label: str = "employee") -> str:
	"""An Employee docname from a docname or from a name somebody typed.

	Refuses an ambiguous name rather than picking one, exactly as the deduction
	writer does. Two workers called Maria Lopez is the ordinary case on a farm
	crew, and garnishing the wrong one shows up when somebody's pay is short and
	they have no order against them.
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
			"withholding under a court order made against somebody else is not an error "
			"that shows up in a reconciliation. Nothing was changed."
		)
	return matches[0]


def _resolve(args: dict, key: str = "garnishment") -> dict:
	"""One garnishment row from a docname, or from a case number that is unique.

	A case number is what a court quotes and a docname is what this app assigns,
	so both are accepted. An ambiguous case number is refused rather than
	resolved: the same number against two workers is exactly the shape a
	multi-defendant judgment takes.
	"""
	value = as_str(args, key, required=True)
	if frappe.db.exists(GARNISHMENT, value):
		return frappe.db.get_value(
			GARNISHMENT,
			value,
			compat.existing_fields(GARNISHMENT, _FIELDS),
			as_dict=True,
		)

	matches = frappe.db.get_all(
		GARNISHMENT,
		filters={"case_number": value},
		fields=list(compat.existing_fields(GARNISHMENT, _FIELDS)),
		limit=10,
	)
	if not matches:
		raise ToolError(f"no Farm Garnishment called {value!r}, and no order carries that case number.")
	if len(matches) > 1:
		named = ", ".join(
			f"{row['name']} ({row.get('employee_name') or row['employee']})" for row in matches[:5]
		)
		raise ToolError(
			f"case number {value!r} is on {len(matches)} orders ({named}). Pass the docname. "
			"Nothing was changed."
		)
	return matches[0]


def _describe(row: dict) -> dict:
	"""One order as a caller reads it, plus what is derived rather than stored."""
	described = dict(row)
	kind = str(row.get("garnishment_type") or "")
	described["federal_priority"] = PRIORITY_BY_TYPE.get(kind, 0)
	described["deduction_category"] = DEDUCTION_CATEGORY.get(kind, "Wage Garnishment")
	described["statutory_ceiling_percentage"] = STATUTORY_CEILING.get(kind)
	# `total_owed` of 0 means there is no balance to run down, not a debt of
	# nothing — so `remaining_balance` on such a row is an absence and says so
	# here rather than reading as a paid-off order.
	described["has_balance"] = float(row.get("total_owed") or 0) > 0
	described["effective_ceiling_percentage"] = float(
		row.get("max_disposable_earnings_percentage") or 0
	) or STATUTORY_CEILING.get(kind)
	return described


def _notes(row: dict) -> list[str]:
	"""What is worth saying about an order that saved cleanly.

	Warnings, not refusals. Every one of these is legitimate somewhere — an
	order may lawfully state a ceiling below the statute, a levy may name a
	percentage even though the CCPA does not reach it — and refusing would block
	the case that is real.
	"""
	notes = []
	kind = str(row.get("garnishment_type") or "")
	stated = float(row.get("max_disposable_earnings_percentage") or 0)
	statutory = STATUTORY_CEILING.get(kind)

	if stated and statutory is not None and stated > statutory:
		notes.append(
			f"This order states a ceiling of {stated:g}% of disposable earnings, above the "
			f"{statutory:g}% federal maximum for a {kind.lower()} garnishment. An order may set a "
			"ceiling LOWER than the statute; it cannot raise one. The payroll engine applies the "
			"statutory ceiling regardless — check the order was read correctly."
		)
	if stated and kind == "Tax Levy":
		notes.append(
			"A tax levy is outside the CCPA entirely (29 CFR 870.11(b)(2)), so a percentage "
			"ceiling does not bound it. What bounds it is the exempt amount the notice leaves the "
			"worker, from IRS Publication 1494 — put that on the linked deduction's exempt_amount."
		)
	if kind != "Child Support" and float(row.get("total_owed") or 0) <= 0:
		notes.append(
			"No total_owed, so there is no balance to run down and this order will never become "
			"Satisfied by arithmetic — it will withhold until somebody terminates it. A creditor, "
			"levy or student loan garnishment normally collects a stated sum; set it."
		)
	if kind == "Child Support" and float(row.get("total_owed") or 0) > 0:
		notes.append(
			"Child support is an ongoing obligation rather than a balance to pay off, and a "
			"total_owed on one will mark it Satisfied and STOP the withholding once that figure "
			"is reached. Use it only for an arrears-only order that really does end at a number."
		)
	received, effective = str(row.get("received_date") or ""), str(row.get("effective_date") or "")
	if received and effective and effective < received:
		notes.append(
			f"Withholding is dated from {effective}, before the order was served on "
			f"{received}. An employer cannot withhold from wages it has already paid; the "
			"acknowledgment letter will state this date to the court."
		)
	if not str(row.get("issuing_court_or_agency") or "").strip():
		notes.append(
			"No issuing court or agency, so render_garnishment_response has nobody to address the "
			"acknowledgment letter to. It is also who the employer answers when withholding stops."
		)
	if kind and kind not in PRIORITY_BY_TYPE:
		notes.append(
			f"{kind!r} is not one of the four types this app ranks "
			f"({', '.join(PRIORITY_BY_TYPE)}), so it has been ranked BEHIND all of them and "
			"is being processed under the ordinary creditor category. That is the safe "
			"direction — an unranked order taking a support order's share of the pool would "
			"be the other one — but somebody has to give it a rank in PRIORITY_BY_TYPE."
		)
	if not row.get("payroll_deduction"):
		notes.append(
			"No linked payroll deduction, so NOTHING IS BEING WITHHELD under this order. File one "
			"with create_payroll_deduction and it will be honoured from its own effective date."
		)
	return notes


def _competing(row: dict) -> list[dict]:
	"""The other live orders against the same worker, in the order law ranks them.

	ON EVERY CREATE AND EVERY READ, because the second order is where this gets
	decided: an employer served with a creditor judgment against somebody who
	already has a support order does not get a fresh 25% pool for it — 29 CFR
	870.11(b)(1) gives it what is LEFT of the 25% after support has come out,
	which is frequently nothing. A caller that could not see the other orders
	would price this one alone.
	"""
	others = frappe.db.get_all(
		GARNISHMENT,
		filters={"employee": row.get("employee"), "status": "Active"},
		fields=[
			"name",
			"garnishment_type",
			"priority",
			"case_number",
			"withholding_type",
			"withholding_amount",
			"remaining_balance",
		],
		limit=50,
	)
	others = [item for item in others if item["name"] != row.get("name")]
	return sorted(others, key=lambda item: (int(item.get("priority") or 99), str(item.get("name"))))


# ── reads ───────────────────────────────────────────────────────────────────


def get_garnishment(args: dict) -> ToolResult:
	"""One order in full: what it says, what has been collected, and what else competes with it."""
	_require()
	require_hr_role()
	row = _resolve(args)

	described = _describe(dict(row))
	data = {"garnishment": described, "competing_orders": _competing(dict(row))}
	# v0.292.0. The arrears and every catch-up and carry-forward — what is explained to the worker and the agency.
	deduction = str(row.get("payroll_deduction") or "")
	if deduction and frappe.db.exists(PAYROLL_DEDUCTION, deduction):
		from .payroll_deductions import arrears_view

		arrears = arrears_view(deduction, dict(frappe.get_doc(PAYROLL_DEDUCTION, deduction).as_dict()))
		if arrears:
			data["arrears"] = arrears
	notes = _notes(dict(row))
	if notes:
		data["notes"] = notes
	return ToolResult(
		data=data,
		summary=(
			f"read garnishment {row['name']} ({row.get('garnishment_type')} "
			f"{row.get('case_number')} against {row.get('employee_name') or row.get('employee')})"
		),
	)


def list_garnishments(args: dict) -> ToolResult:
	"""The file, filtered by employee, company, status or type — ranked as law ranks them.

	SORTED BY FEDERAL PRIORITY WITHIN EACH WORKER, so the list reads as the order
	the money comes out in rather than as rows somebody has to rank. Company
	scoped like everything that reads a personnel file.
	"""
	_require()
	require_hr_role()
	company = resolve_company(as_str(args, "company"))
	filters = {}
	if company:
		filters["company"] = company
	if args.get("employee"):
		filters["employee"] = _resolve_employee(as_str(args, "employee"))
	for key in ("garnishment_type", "status"):
		value = as_str(args, key)
		if value:
			filters[key] = as_choice(GARNISHMENT, key, value, key)
	case_number = as_str(args, "case_number")
	if case_number:
		filters["case_number"] = ("like", f"%{case_number}%")

	rows = frappe.db.get_all(
		GARNISHMENT,
		filters=filters,
		fields=list(compat.existing_fields(GARNISHMENT, _FIELDS)),
		limit=as_limit(args),
	)
	rows = sorted(
		rows,
		key=lambda row: (
			str(row.get("employee_name") or row.get("employee") or ""),
			int(row.get("priority") or 99),
			str(row.get("effective_date") or ""),
		),
	)
	described = [_describe(dict(row)) for row in rows]
	active = [row for row in described if row.get("status") == "Active"]

	by_type: dict = {}
	for row in active:
		by_type[row.get("garnishment_type") or "?"] = by_type.get(row.get("garnishment_type") or "?", 0) + 1

	# Somebody with two or more live orders is where the shared-pool rule bites,
	# and it is the fact a payroll clerk most needs surfaced from a list.
	counts: dict = {}
	for row in active:
		counts[row["employee"]] = counts.get(row["employee"], 0) + 1

	return ToolResult(
		data={
			"garnishments": described,
			"count": len(described),
			"active_count": len(active),
			"active_by_type": by_type,
			"employees_with_orders": sorted(counts),
			"employees_with_competing_orders": sorted(name for name, n in counts.items() if n > 1),
			"unremitted_balance": round(
				sum(float(row.get("remaining_balance") or 0) for row in active),
				2,
			),
			"company": company or "",
		},
		summary=f"listed {len(described)} garnishments ({len(active)} active)",
	)


# ── writes ──────────────────────────────────────────────────────────────────


def create_garnishment(args: dict) -> ToolResult:
	"""File one court order and put the withholding it commands into payroll.

	TWO RECORDS, ONE ACT. The order is inserted first and its deduction second,
	so a deduction can never exist without the order that authorises it; the
	dispatcher rolls the whole call back if the second half fails, which is why
	the order is not left behind either.
	"""
	_require()
	actor = require_hr_role()
	company = resolve_company(as_str(args, "company"), required=True)
	require_company_scope(actor, company)
	employee = _resolve_employee(as_str(args, "employee", required=True))

	case_number = as_str(args, "case_number", required=True)
	# The same case number against the same worker, still live, is the same
	# order filed twice — re-keyed on a hire day, or entered by two people. Two
	# orders means two deductions means the money is taken twice, out of
	# somebody's actual pay, so it is refused by name rather than warned about.
	duplicate = frappe.db.get_value(
		GARNISHMENT,
		{"employee": employee, "case_number": case_number, "status": "Active"},
		"name",
	)
	if duplicate:
		raise ToolError(
			f"{employee} already has an active garnishment on case {case_number!r} ({duplicate}). "
			"Filing it twice would withhold it twice. Update that order, or use the case number "
			"the second order actually carries. Nothing was created."
		)

	doc = frappe.new_doc(GARNISHMENT)
	doc.employee = employee
	doc.company = company
	# WRITTEN RATHER THAN FETCHED. `employee_name` carries `fetch_from` for the
	# Desk's benefit, and a fetch is a client-side convenience that does not run
	# on every path a document can be inserted through. The name is what the
	# ACKNOWLEDGMENT LETTER puts in front of a court — a letter naming the worker
	# as HR-EMP-00001 is one a court cannot match to its own defendant — so it is
	# set here, from the Employee, rather than hoped for.
	# NOT `or employee`. A docname written into a NAME column is a fiction that
	# every reader downstream then treats as a name — including the letter, which
	# would post "HR-EMP-00009" to a court as a person. Left empty where there is
	# no name, so the readers that fall back can say what they are falling back to.
	doc.employee_name = frappe.db.get_value(EMPLOYEE, employee, "employee_name") or ""
	_apply(doc, args, creating=True)
	doc.insert(ignore_permissions=True)

	deduction = _file_deduction(doc, args)
	frappe.db.set_value(GARNISHMENT, doc.name, "payroll_deduction", deduction, update_modified=False)
	doc.payroll_deduction = deduction

	row = dict(doc.as_dict())
	described = _describe(row)
	data = {
		"garnishment": described,
		"payroll_deduction": deduction,
		"competing_orders": _competing(row),
		"actor": actor,
	}
	notes = _notes(row)
	if notes:
		data["notes"] = notes
	if data["competing_orders"]:
		data["shared_pool_note"] = (
			f"{doc.employee_name or employee} has {len(data['competing_orders'])} other live "
			"order(s). Where support and an ordinary garnishment compete, 29 CFR 870.11(b)(1) "
			"gives the ordinary one only what is LEFT of the 25% pool after support has come "
			"out — frequently nothing, which is the correct answer rather than a failure to "
			"collect. list_employee_deductions with a gross_pay prices them together."
		)
	return ToolResult(
		data=data,
		summary=(
			f"filed {doc.garnishment_type} garnishment {doc.name} (case {case_number}) against "
			f"{doc.employee_name or employee} from {doc.effective_date}, withholding via {deduction}"
		),
		docstatus_delta="none → 0 (created)",
	)


def _deduction_payload(doc, args: dict) -> dict:
	"""The arguments `create_payroll_deduction` is called with.

	NO `priority`. The order's own priority is federal precedence 1..4 and the
	engine's queue runs 10/20/30/40 off the category — passing 1..4 across would
	sort a creditor order ahead of a support order and invert the precedence.
	Leaving it unset is what makes the category speak, which is the engine's own
	default and the correct one.

	`basis` is Net After Tax for a percentage, because DISPOSABLE EARNINGS is
	what every court order is written against — gross less what the law requires
	withheld (29 CFR 870.10), which is not net pay and not gross.
	"""
	kind = str(doc.garnishment_type or "")
	percentage = str(doc.withholding_type or "") == "Percentage of Disposable"
	payload = {
		"employee": doc.employee,
		"company": doc.company,
		"deduction_category": DEDUCTION_CATEGORY.get(kind, "Wage Garnishment"),
		"deduction_type": "Garnishment",
		"amount_type": "Percentage" if percentage else "Fixed",
		"amount": float(doc.withholding_amount or 0),
		"effective_from": doc.effective_date,
		"status": "Active" if str(doc.status or "Active") == "Active" else "Completed",
		"reference": doc.case_number,
		"label": as_str(args, "pay_stub_label") or "",
		"notes": f"Filed from Farm Garnishment {doc.name}.",
		# v0.311.0. What the order is for. A creditor order with no type is taken as consumer debt — the most
		# protective reading for the worker (RCW 6.27.150(4)); say otherwise with debt_type.
		"debt_type": as_str(args, "debt_type") or {"Child Support": "Child support", "Tax Levy": "Tax levy",
		                                            "Student Loan": "Federal student loan (AWG)"}.get(
			kind, "Consumer debt"),
	}
	if percentage:
		payload["basis"] = "Net After Tax"
	# The two child-support facts that pick 50/55/60/65 under 1673(b)(2) are
	# properties of the ORDER, so they are taken here and land on the deduction
	# where the engine reads them.
	if kind == "Child Support":
		for flag in ("supports_other_dependents", "arrears_over_12_weeks"):
			value = as_bool(args, flag)
			if value is not None:
				payload[flag] = value
	if kind == "Tax Levy":
		exempt = as_float(args.get("exempt_amount"), "exempt_amount")
		if exempt:
			payload["exempt_amount"] = exempt
	# v0.292.0. Arrears stated on the order and the order's own catch-up schedule, if it sets one.
	for field in ("arrears_opening", "arrears_per_period"):
		value = as_float(args.get(field), field)
		if value:
			payload[field] = value
	if as_bool(args, "consumer_debt"):
		payload["consumer_debt"] = True
	return {key: value for key, value in payload.items() if value not in (None, "")}


def _file_deduction(doc, args: dict) -> str:
	"""Create the deduction THROUGH the deduction writer, and return its docname.

	Not a second implementation. `create_payroll_deduction` owns the allowlist,
	the duplicate refusal and the category-to-law mapping, and a copy of that
	here would be a second place for the CCPA ceilings to drift.
	"""
	result = create_payroll_deduction(_deduction_payload(doc, args))
	name = str((result.data.get("deduction") or {}).get("name") or "")
	if not name:  # pragma: no cover - the writer always names what it inserted
		raise ToolError(
			"the payroll deduction was not created, so nothing would be withheld under this "
			"order. Nothing was filed."
		)
	return name


def update_garnishment(args: dict) -> ToolResult:
	"""Change a filed order. Every change is echoed back as before → after.

	CANNOT RE-KEY IT. `employee` and `company` are not writable: an order moved
	to another worker would apply a court's finding about one person to somebody
	else and leave an audit trail saying it had always been theirs. Terminate
	this one and file a new one.

	SATISFYING AND TERMINATING ARE STATUS CHANGES, NOT DELETIONS, and either one
	retires the linked deduction so the withholding actually stops. An order
	removed from the file cannot answer the court that asks why it stopped.

	`add_withheld` EXISTS BECAUSE A PAYROLL RUN KNOWS THE PERIOD, NOT THE TOTAL.
	Making every caller read the running sum and write it back is a lost update
	the moment two runs post together, and the lost update is money that was
	taken and is not on the balance.
	"""
	_require()
	actor = require_hr_role()
	row = _resolve(args)
	name = row["name"]

	for locked in ("employee", "company"):
		if args.get(locked):
			raise ToolError(
				f"{locked} cannot be changed on a filed garnishment. Moving one to another worker "
				"would apply a court's finding about one person to somebody else, and leave an "
				"audit trail saying it had always been theirs. Terminate this order and file a "
				"new one. Nothing was changed."
			)

	doc = frappe.get_doc(GARNISHMENT, name)
	require_company_scope(actor, str(doc.company or ""))
	before = dict(doc.as_dict())
	changed = _apply(doc, args, creating=False)

	increment = as_float(args.get("add_withheld"), "add_withheld")
	if increment:
		if increment < 0:
			raise ToolError(
				"add_withheld cannot be negative. Payroll adds to what has been collected; a "
				"correction that reduces it is a total_withheld written outright, so that the "
				"change reads as a correction rather than as a refund nobody made. "
				"Nothing was changed."
			)
		doc.total_withheld = float(doc.total_withheld or 0) + increment
		changed.append("total_withheld")

	# v0.292.0. The arrears live on the deduction payroll reads, so they are written there.
	arrears_updates = {}
	for field in ("arrears_opening", "arrears_per_period"):
		if args.get(field) not in (None, ""):
			arrears_updates[field] = as_float(args.get(field), field)
	if as_bool(args, "consumer_debt") is not None:
		arrears_updates["consumer_debt"] = 1 if as_bool(args, "consumer_debt") else 0
	deduction_name = str(doc.payroll_deduction or "").strip()
	if arrears_updates and not (deduction_name and frappe.db.exists(PAYROLL_DEDUCTION, deduction_name)):
		raise ToolError(f"{name} has no payroll deduction to hold arrears. Nothing was changed.")

	if not changed and not arrears_updates:
		raise ToolError(
			"nothing to change: no writable field was supplied. Writable fields are "
			f"{', '.join(_WRITABLE)}, plus add_withheld, arrears_opening, arrears_per_period and consumer_debt."
		)

	doc.save(ignore_permissions=True)
	mirrored = _mirror_to_deduction(doc, changed)
	for field, value in arrears_updates.items():
		was = frappe.db.get_value(PAYROLL_DEDUCTION, deduction_name, field)
		if was != value:
			frappe.db.set_value(PAYROLL_DEDUCTION, deduction_name, field, value)
			mirrored.append({"deduction": deduction_name, "field": field, "before": was, "after": value})
	if "arrears_opening" in arrears_updates:
		from .. import garnishment_ledger

		frappe.db.set_value(PAYROLL_DEDUCTION, deduction_name, "arrears_balance",
		                    garnishment_ledger.balance(deduction_name))

	row = dict(doc.as_dict())
	described = _describe(row)
	changes = [
		{"field": field, "before": before.get(field), "after": row.get(field)}
		for field in sorted(set(changed) | {"status", "remaining_balance", "priority"})
		if before.get(field) != row.get(field)
	]
	data = {"garnishment": described, "changes": changes, "actor": actor}
	if mirrored:
		data["deduction_changes"] = mirrored
	notes = _notes(row)
	if notes:
		data["notes"] = notes
	if before.get("status") == "Active" and row.get("status") == "Satisfied":
		data["satisfied_note"] = (
			f"The balance on case {row.get('case_number')} reached zero, so the order is now "
			"Satisfied and its deduction has been retired. Withholding past a satisfied judgment "
			"is money taken under an authority that has expired."
		)
	return ToolResult(
		data=data,
		summary=(
			f"updated garnishment {name} ({len(changes)} field{'' if len(changes) == 1 else 's'} changed)"
		),
		docstatus_delta="0 → 0 (amended)",
	)


def _mirror_to_deduction(doc, changed: list) -> list:
	"""Carry an amount or type change onto the deduction payroll actually reads.

	AN ORDER THAT SAYS $200 AND A RUN THAT TAKES $150 IS NOT A DISCREPANCY
	SOMEBODY NOTICES — both figures look deliberate, and the shortfall is the
	employer's. The status is not mirrored here: the controller's `on_update`
	owns that, and it knows about a court-ordered stay this does not.
	"""
	deduction = str(doc.payroll_deduction or "").strip()
	touched = [field for field in _MIRRORED if field in changed]
	if not deduction or not touched or not frappe.db.exists(PAYROLL_DEDUCTION, deduction):
		return []

	updates = {}
	if "withholding_amount" in touched:
		updates["amount"] = float(doc.withholding_amount or 0)
	if "withholding_type" in touched:
		percentage = str(doc.withholding_type or "") == "Percentage of Disposable"
		updates["amount_type"] = "Percentage" if percentage else "Fixed"
		if percentage:
			updates["basis"] = "Net After Tax"

	applied = []
	for field, value in updates.items():
		was = frappe.db.get_value(PAYROLL_DEDUCTION, deduction, field)
		if was == value:
			continue
		frappe.db.set_value(PAYROLL_DEDUCTION, deduction, field, value)
		applied.append({"deduction": deduction, "field": field, "before": was, "after": value})
	return applied


def _apply(doc, args: dict, creating: bool) -> list:
	"""Set every writable field the caller supplied. Returns the ones it touched."""
	touched = []
	for field in _WRITABLE:
		if field not in args or args.get(field) in (None, ""):
			continue
		if field in _SELECTS:
			doc.set(field, as_choice(GARNISHMENT, field, as_str(args, field), field))
		elif field in _DATES:
			doc.set(field, as_date(args, field))
		elif field in _FLOATS:
			doc.set(field, as_float(args.get(field), field))
		else:
			doc.set(field, as_str(args, field))
		touched.append(field)

	if creating:
		if not doc.status:
			doc.status = "Active"
		if not doc.withholding_type:
			doc.withholding_type = "Fixed Amount"
		if not doc.received_date:
			doc.received_date = frappe.utils.today()
	return touched


# ── the letter back to the court ────────────────────────────────────────────


def render_garnishment_response(args: dict) -> ToolResult:
	"""Draw the employer's acknowledgment of the order and attach it privately.

	WHAT AN EMPLOYER OWES ON BEING SERVED IS AN ANSWER, and the answer has a
	shape: we employ this person, we received your order on this date, we will
	withhold this much from this date, and here is what else already stands
	against their wages. Until this existed the app could withhold correctly and
	could not say so to anybody.

	IT IS THE EMPLOYER'S OWN LETTER AND IT SAYS SO ON THE PAGE. Several regimes
	prescribe their own answer form — the federal Income Withholding for Support
	order carries one, and many states' writs of garnishment require a sworn
	answer on a court-supplied form within a stated number of days. This does not
	replace those and does not pretend to. It is the acknowledgment that
	accompanies one, and the record that the employer answered at all.

	A SNAPSHOT, NOT A VIEW, as every rendered page in this app is: a second
	render refuses without `overwrite=true`, because the likeliest thing in that
	field is the copy that was already posted to the court.
	"""
	_require()
	garnishment_response_pdf.require()
	actor = require_hr_role()
	row = _resolve(args)
	require_company_scope(actor, str(row.get("company") or ""))

	existing = str(row.get("response_letter") or "").strip()
	if existing and not as_bool(args, "overwrite", False):
		raise ToolError(
			f"{row['name']} already has a response letter at {existing}. The likeliest thing in "
			"that field is the copy that was posted to the court. Pass overwrite=true to draw a "
			"fresh page and repoint the field; the existing File stays attached to the record "
			"either way. Nothing was changed."
		)

	described = _describe(dict(row))
	pdf = garnishment_response_pdf.render_response(
		described,
		company={
			"name": row.get("company"),
			"address": as_str(args, "company_address") or as_str(args, "employer_address"),
		},
		signatory={
			"name": as_str(args, "signatory_name") or actor,
			"title": as_str(args, "signatory_title") or "Authorized Representative",
		},
		competing=_competing(dict(row)),
		pay_frequency=as_str(args, "pay_frequency") or "",
	)
	attachment = artifacts.attach_bytes(
		GARNISHMENT,
		row["name"],
		garnishment_response_pdf.file_name_for(described),
		pdf,
		field="response_letter",
	)
	frappe.db.set_value(
		GARNISHMENT,
		row["name"],
		"response_letter_on",
		frappe.utils.now(),
		update_modified=False,
	)

	data = {
		"garnishment": row["name"],
		"name": row["name"],
		"actor": actor,
		"case_number": row.get("case_number"),
		"addressed_to": row.get("issuing_court_or_agency") or "",
		"withholding_begins": row.get("effective_date"),
		# `file_url` at the TOP LEVEL, which is where every other renderer in
		# this app answers with it and where a sealing chain looks for it.
		"file_url": attachment.get("file_url"),
		"file_name": attachment.get("file_name"),
		"file": attachment.name,
		# The digest and size, WITHOUT the `name` key `describe_attachment`
		# carries — that one names the File, and this result's `name` is the
		# garnishment. Spreading it whole would silently rename the record.
		"attachment": artifacts.describe_attachment(attachment, pdf),
		"replaced": existing or None,
		"note": (
			"The employer's own acknowledgment. Where the issuing state or agency prescribes a "
			"sworn answer on its own form, this accompanies that form and does not replace it — "
			"the page says so in its own words."
		),
	}
	if not str(row.get("issuing_court_or_agency") or "").strip():
		data["addressee_note"] = (
			"No issuing court or agency is on the order, so the letter is addressed to the case "
			"number alone. Set issuing_court_or_agency and render again before posting it."
		)
	return ToolResult(
		data=data,
		summary=(
			f"drew the employer response for {row['name']} (case {row.get('case_number')}), "
			f"withholding from {row.get('effective_date')}"
		),
		docstatus_delta="0 → 0 (amended)",
	)

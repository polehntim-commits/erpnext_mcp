# SPDX-License-Identifier: MIT
"""HR tools. Present only on sites that have the `hrms` app installed.

These three are gated by an availability predicate rather than by degrading at
call time: on a site without `hrms` they do not appear in `tools/list` at all.
The difference matters for a model, which decides what is possible from the
catalogue — a tool that is listed and always fails is a trap, and a client that
sees `get_leave_balance` will eventually try to answer a leave question with it.

`hrms` was split out of ERPNext at v14, so on older sites the same doctypes live
under `erpnext.hr`. `_leave_balance_api` looks in both places before giving up,
which is the same "ask, do not assume" rule the rest of `compat` follows.

WHY THE SUMMARY IS AGGREGATED. `get_attendance_summary` returns counts per
employee, not one row per day. A month of attendance for forty people is 1,200
rows that say nothing an aggregate does not, and the question behind the call is
always "who was out".
"""

import frappe

from .. import compat, minors
from ..args import as_bool, as_date, as_filter, as_limit, as_str
from ..errors import ToolError
from ..result import ToolResult

#: The statuses ERPNext's Attendance doctype uses. Anything else a site has added
#: is counted under its own name rather than dropped.
ATTENDANCE_STATUSES = ("Present", "Absent", "Half Day", "On Leave", "Work From Home")


# ── 28. list_employees ──────────────────────────────────────────────────────
def list_employees(args: dict) -> ToolResult:
	"""Employee records, active ones by default."""
	compat.require_doctype("Employee", "It comes with the Frappe HR (hrms) app.")
	status = as_filter(args, "status", default="Active")
	department = as_str(args, "department")
	designation = as_str(args, "designation")
	company = as_str(args, "company")
	limit = as_limit(args)

	filters = {}
	if status:
		filters["status"] = status
	if department:
		filters["department"] = department
	if designation:
		filters["designation"] = designation
	if company:
		filters["company"] = company

	fields = compat.existing_fields(
		"Employee",
		[
			"name",
			"employee_name",
			"employee_number",
			"department",
			"designation",
			"status",
			"date_of_joining",
			"relieving_date",
			"company",
			"user_id",
			"reports_to",
			"branch",
			"employment_type",
			# v0.98.0. FETCHED SO `is_minor` CAN BE DERIVED, and derived rather
			# than stored — see `minors.py`. Through `compat.existing_fields` like
			# everything else here, so a site whose Employee has been customised
			# without it loses the two derived keys rather than the read.
			"date_of_birth",
		],
	)
	rows = frappe.db.get_all(
		"Employee", filters=filters, fields=fields, order_by="employee_name asc", limit=limit
	)

	today = frappe.utils.today()
	by_department = {}
	minor_count = 0
	for row in rows:
		key = row.get("department") or "<none>"
		by_department[key] = by_department.get(key, 0) + 1
		row.update(minors.describe(row.get("date_of_birth"), today))
		if row.get("is_minor"):
			minor_count += 1

	data = {
		"employees": rows,
		"count": len(rows),
		"limit": limit,
		"truncated": len(rows) == limit,
		"by_department": by_department,
		"filters": {
			"status": status or "any",
			"department": department or None,
			"designation": designation or None,
			"company": company or None,
		},
		"minors": minor_count,
		"note": (
			"`name` is the Employee docname (HR-EMP-…); `employee_number` is the "
			"payroll number an operator will recognise. Pass `name` to the other "
			"HR tools."
		),
		"minor_note": (
			"`is_minor` is DERIVED from `date_of_birth` on every read and is stored nowhere — a "
			"fifteen-year-old hired in April is sixteen in July, and a ticked column would still "
			"say otherwise. It is THREE-VALUED: null means no date of birth is on file, which is "
			"not the same as an adult. `minor_band` is under-16 or 16-17, because the hour and "
			"time-of-day limits differ between them, and `minor_limits` carries the ceiling with "
			"its citation."
		),
	}
	return ToolResult(
		data,
		f"{len(rows)} employee(s) ({status or 'any status'})"
		+ (f", {minor_count} under 18" if minor_count else ""),
	)


# ── 29. get_attendance_summary ──────────────────────────────────────────────
def get_attendance_summary(args: dict) -> ToolResult:
	"""Per-employee counts of each attendance status over a date range."""
	compat.require_doctype("Attendance", "It comes with the Frappe HR (hrms) app.")
	from_date = as_date(args, "from_date", required=True)
	to_date = as_date(args, "to_date", required=True)
	if from_date > to_date:
		raise ToolError(f"from_date {from_date} is after to_date {to_date}")
	employee = as_str(args, "employee")
	department = as_str(args, "department")

	filters = {"attendance_date": ("between", [from_date, to_date])}
	# Attendance is submittable, and a draft or cancelled row is not a fact about
	# whether somebody turned up.
	filters["docstatus"] = 1
	if employee:
		filters["employee"] = _resolve_employee(employee)
	if department and compat.has_field("Attendance", "department"):
		filters["department"] = department

	fields = compat.existing_fields(
		"Attendance",
		["employee", "employee_name", "attendance_date", "status", "department", "company"],
	)
	rows = frappe.db.get_all("Attendance", filters=filters, fields=fields, order_by="employee asc")

	summary, totals = {}, {}
	for row in rows:
		key = row.get("employee")
		entry = summary.setdefault(
			key,
			{
				"employee": key,
				"employee_name": row.get("employee_name"),
				"department": row.get("department"),
				"counts": {},
				"total_marked": 0,
			},
		)
		status = row.get("status") or "<unset>"
		entry["counts"][status] = entry["counts"].get(status, 0) + 1
		entry["total_marked"] += 1
		totals[status] = totals.get(status, 0) + 1

	# Give every employee the full set of keys so a model does not have to guess
	# whether a missing key means zero or means "this site does not track it".
	seen_statuses = sorted(set(totals) | set(ATTENDANCE_STATUSES))
	for entry in summary.values():
		entry["counts"] = {status: entry["counts"].get(status, 0) for status in seen_statuses}

	employees = sorted(summary.values(), key=lambda entry: entry["employee_name"] or "")
	data = {
		"from_date": from_date,
		"to_date": to_date,
		"employees": employees,
		"employee_count": len(employees),
		"totals": {status: totals.get(status, 0) for status in seen_statuses},
		"records_counted": len(rows),
		"statuses": seen_statuses,
		"filters": {"employee": filters.get("employee"), "department": department or None},
		"note": (
			"Submitted Attendance only (docstatus 1) — drafts and cancelled rows "
			"are not evidence of anything. Days with no Attendance record at all "
			"are absent from these counts rather than counted as Absent."
		),
	}
	return ToolResult(
		data,
		f"attendance {from_date}..{to_date}: {len(employees)} employee(s), {len(rows)} record(s)",
	)


# ── 30. get_leave_balance ───────────────────────────────────────────────────
def get_leave_balance(args: dict) -> ToolResult:
	"""Remaining leave for one employee, per leave type.

	Uses HR's own `get_leave_balance_on`, which nets allocations against
	applications and handles carry-forward and expiry. Those rules are the whole
	difficulty of the question, and a subtraction done here would be confidently
	wrong on any site with a carry-forward policy.
	"""
	compat.require_doctype("Leave Allocation", "It comes with the Frappe HR (hrms) app.")
	employee = _resolve_employee(as_str(args, "employee", required=True))
	leave_type = as_str(args, "leave_type")
	as_of = as_date(args, "as_of") or frappe.utils.today()

	balance_on = _leave_balance_api()
	if balance_on is None:
		raise ToolError(
			"this site's HR app does not export get_leave_balance_on, so a leave "
			"balance cannot be computed correctly here. Use the Leave Balance "
			"report via run_report instead."
		)

	types = [leave_type] if leave_type else _allocated_leave_types(employee, as_of)
	if leave_type and not frappe.db.exists("Leave Type", leave_type):
		raise ToolError(
			f"no Leave Type named {leave_type!r}. This employee has allocations "
			f"for: {', '.join(_allocated_leave_types(employee, as_of)) or '<none>'}"
		)

	balances, failed = [], []
	for name in types:
		try:
			balance = balance_on(employee, name, as_of)
		except Exception as exc:
			# One misconfigured leave type must not take the whole answer down.
			failed.append({"leave_type": name, "error": f"{type(exc).__name__}: {exc}"})
			continue
		balances.append({"leave_type": name, "balance": _number(balance)})

	employee_name = frappe.db.get_value("Employee", employee, "employee_name")
	data = {
		"employee": employee,
		"employee_name": employee_name,
		"as_of": as_of,
		"balances": balances,
		"count": len(balances),
		"total_balance": round(sum(row["balance"] for row in balances), 3),
		"leave_type_filter": leave_type or None,
		"computed_via": "HR get_leave_balance_on",
	}
	if failed:
		data["failed"] = failed
	return ToolResult(
		data,
		f"{employee_name or employee} leave balance as of {as_of}: "
		f"{len(balances)} type(s), {data['total_balance']} day(s) total",
	)


# ── shared ──────────────────────────────────────────────────────────────────
def _resolve_employee(value: str) -> str:
	"""An Employee docname from a docname, an employee_number, or a name."""
	value = (value or "").strip()
	if not value:
		raise ToolError("employee is required")
	if frappe.db.exists("Employee", value):
		return value
	for filters in (
		{"employee_number": value},
		{"employee_name": value},
		{"user_id": value},
	):
		matches = frappe.db.get_all("Employee", filters=filters, pluck="name", limit=10)
		if len(matches) == 1:
			return matches[0]
		if len(matches) > 1:
			raise ToolError(
				f"{value!r} matches {len(matches)} employees: "
				f"{', '.join(sorted(matches))}. Pass the Employee docname."
			)
	raise ToolError(
		f"no Employee matching {value!r} (tried docname, employee_number, "
		"employee_name and user_id). Use list_employees to find it."
	)


def _allocated_leave_types(employee: str, as_of: str) -> list[str]:
	"""Leave types this employee has an allocation covering `as_of`.

	Better than listing every Leave Type on the site: a balance for a type nobody
	allocated is always zero and only adds noise.
	"""
	filters = {
		"employee": employee,
		"docstatus": 1,
		"from_date": ("<=", as_of),
		"to_date": (">=", as_of),
	}
	types = frappe.db.get_all("Leave Allocation", filters=filters, pluck="leave_type")
	return sorted(set(types))


def _leave_balance_api():
	"""HR's `get_leave_balance_on`, from wherever this site keeps it.

	`hrms` since v14; `erpnext.hr` before the split.
	"""
	paths = (
		"hrms.hr.doctype.leave_application.leave_application",
		"erpnext.hr.doctype.leave_application.leave_application",
	)
	for path in paths:
		try:
			module = __import__(path, fromlist=["get_leave_balance_on"])
		except Exception:
			continue
		function = getattr(module, "get_leave_balance_on", None)
		if callable(function):
			return function
	return None


def _number(value) -> float:
	try:
		return round(float(value or 0), 3)
	except (TypeError, ValueError):
		return 0.0


# ══════════════════════════════════════════════════════════════════════════════
# Leave requests
#
# THE READ SHIPPED WITHOUT THE WRITE. `get_leave_balance` has told a worker how
# many days they have left since v0.18.1, and there has been no way to ask for
# one of them — the balance was a number on a screen with no button under it.
#
# EVERY REFUSAL BELOW IS MADE HERE RATHER THAN LEFT TO hrms, and that is the
# whole shape of this section. `LeaveApplication.validate` does check the
# balance, the overlap and the date order — but it throws hrms's sentence, which
# names neither the argument that was wrong nor what this site actually offers.
# A worker whose request is refused needs "you have 2 days of Sick Leave and
# asked for 3", not "Insufficient leave balance". The tests are written against
# THIS version of each check, because on a bench-less repo it is the only version
# that can be run at all — the double does not carry hrms's controller.
#
# NOTHING HERE COUNTS DAYS OR NETS A BALANCE FOR ITSELF, for the reason
# `get_leave_balance` gives at length: carry-forward, expiry, holiday lists and
# half days are the whole difficulty of the question, and an arithmetic answer
# written here would be confidently wrong on any site with a policy. Both are
# delegated to hrms and the answer says which function ran.
# ══════════════════════════════════════════════════════════════════════════════

LEAVE_APPLICATION = "Leave Application"

#: The statuses hrms's own Select offers, read off the deployed site rather than
#: guessed. `Open` is the default and the one a draft is created in.
LEAVE_STATUSES = ("Open", "Approved", "Rejected", "Cancelled")

#: Most applications one read returns before it says there are more.
LEAVE_CAP = 200


def _leave_days_api():
	"""HR's `get_number_of_leave_days`, from wherever this site keeps it."""
	paths = (
		"hrms.hr.doctype.leave_application.leave_application",
		"erpnext.hr.doctype.leave_application.leave_application",
	)
	for path in paths:
		try:
			module = __import__(path, fromlist=["get_number_of_leave_days"])
		except Exception:
			continue
		function = getattr(module, "get_number_of_leave_days", None)
		if callable(function):
			return function
	return None


def _require_leave() -> None:
	compat.require_doctype(LEAVE_APPLICATION, "It comes with the Frappe HR (hrms) app.")


def _leave_type_for(employee: str, requested: str, as_of: str) -> str:
	"""The Leave Type to file under, or a refusal naming what the site offers.

	THREE ANSWERS AND ONLY TWO ARE ANSWERS, which is the same shape
	`asset_mirror._category` settled on for the same reason: choosing between
	several on somebody's behalf is inventing a decision, and choosing when
	there is exactly one is not.
	"""
	allocated = _allocated_leave_types(employee, as_of)
	if requested:
		if not frappe.db.exists("Leave Type", requested):
			known = frappe.db.get_all("Leave Type", pluck="name", limit=LEAVE_CAP) or []
			raise ToolError(
				f"no Leave Type named {requested!r} on this site. It has: "
				f"{', '.join(sorted(str(name) for name in known)) or '<none>'}. Nothing was filed."
			)
		return requested
	if len(allocated) == 1:
		return allocated[0]
	if allocated:
		raise ToolError(
			f"this employee has allocations for {len(allocated)} leave types "
			f"({', '.join(allocated)}), so there is no single one to file under. Pass leave_type. "
			"Nothing was filed."
		)
	raise ToolError(
		"this employee has no leave allocation of any type, so there is nothing to file against "
		"and no way to guess what was meant. Allocate leave to them first, or pass a leave_type "
		"whose `is_lwp` is set — unpaid leave is the one kind that needs no allocation. Nothing "
		"was filed."
	)


def _is_lwp(leave_type: str) -> bool:
	try:
		return bool(int(frappe.db.get_value("Leave Type", leave_type, "is_lwp") or 0))
	except Exception:  # pragma: no cover - a site whose Leave Type lacks the column
		return False


def _overlapping(employee: str, from_date: str, to_date: str, exclude: str = "") -> list:
	"""Applications already covering any part of this span. Drafts count.

	A draft counts because it is a request somebody has made and not yet had
	answered; filing a second over the top of it is how one absence becomes two
	rows and a double deduction the moment both are approved.
	"""
	rows = (
		frappe.db.get_all(
			LEAVE_APPLICATION,
			filters={
				"employee": employee,
				"docstatus": ("<", 2),
				"status": ("!=", "Rejected"),
				"from_date": ("<=", to_date),
				"to_date": (">=", from_date),
			},
			fields=["name", "leave_type", "from_date", "to_date", "status"],
			limit=LEAVE_CAP,
		)
		or []
	)
	return [dict(row) for row in rows if str(row.get("name")) != exclude]


def _leave_row(name: str) -> dict:
	fields = compat.existing_fields(
		LEAVE_APPLICATION,
		(
			"name",
			"employee",
			"employee_name",
			"leave_type",
			"from_date",
			"to_date",
			"half_day",
			"half_day_date",
			"total_leave_days",
			"description",
			"posting_date",
			"status",
			"company",
			"leave_approver",
			"docstatus",
		),
	)
	row = frappe.db.get_value(LEAVE_APPLICATION, name, fields, as_dict=True)
	if not row:
		raise ToolError(f"no Leave Application named {name!r} on this site. Nothing was changed.")
	return dict(row)


def _described(row: dict) -> dict:
	out = dict(row)
	out["half_day"] = bool(frappe.utils.cint(row.get("half_day")))
	out["total_leave_days"] = _number(row.get("total_leave_days"))
	out["docstatus"] = int(row.get("docstatus") or 0)
	out["submitted"] = out["docstatus"] == 1
	return out


# ── 30e. list_leave_types ───────────────────────────────────────────────────
def list_leave_types(args: dict) -> ToolResult:
	"""What this site offers, and which of it this employee may actually draw.

	IT EXISTS SO A PICKER IS NOT A LIST OF TRAPS. Without `employee` this is the
	site's Leave Types and nothing more, which is a list where four of five
	choices refuse the moment somebody taps them. With it, every row carries
	`allocated`, `balance` and `requestable` — so a handset can grey out what
	cannot be filed instead of finding out at submission.

	`requestable` IS THE COLUMN THE APP SHOULD DRAW ON, and it is not the same as
	`balance > 0`. An unpaid type is requestable with no allocation and no balance
	at all, because `is_lwp` means there is nothing to draw down — which on a farm
	that has not set up Leave Allocations is the only kind of leave anybody can
	file. Sorting on balance alone would put the one usable row last.
	"""
	compat.require_doctype("Leave Type", "It comes with the Frappe HR (hrms) app.")
	employee = as_str(args, "employee")
	resolved = _resolve_employee(employee) if employee else ""
	as_of = as_date(args, "as_of") or frappe.utils.today()

	fields = compat.existing_fields("Leave Type", ("name", "is_lwp", "max_leaves_allowed"))
	rows = frappe.db.get_all("Leave Type", fields=fields, order_by="name asc", limit=LEAVE_CAP) or []

	allocated = set(_allocated_leave_types(resolved, str(as_of))) if resolved else set()
	balance_on = _leave_balance_api() if resolved else None

	types = []
	for row in rows:
		name = str(row.get("name"))
		lwp = bool(frappe.utils.cint(row.get("is_lwp")))
		entry = {
			"leave_type": name,
			"is_lwp": lwp,
			"max_leaves_allowed": _number(row.get("max_leaves_allowed")) or None,
		}
		if resolved:
			has_allocation = name in allocated
			balance = None
			if has_allocation and balance_on is not None:
				try:
					balance = _number(balance_on(resolved, name, str(as_of)))
				except Exception:  # pragma: no cover - one bad type must not lose the list
					balance = None
			entry.update(
				allocated=has_allocation,
				balance=balance,
				# See the docstring: unpaid leave needs no allocation, so it is
				# requestable on a site that has never made one.
				requestable=bool(lwp or (has_allocation and (balance or 0) > 0)),
			)
		types.append(entry)

	data = {
		"leave_types": types,
		"count": len(types),
		"employee": resolved or None,
		"as_of": str(as_of),
	}
	if resolved:
		data["requestable_count"] = sum(1 for row in types if row.get("requestable"))
		data["employee_name"] = frappe.db.get_value("Employee", resolved, "employee_name")
	return ToolResult(
		data,
		f"{len(types)} leave type(s)"
		+ (
			f", {data['requestable_count']} requestable by "
			f"{data.get('employee_name') or resolved}"
			if resolved
			else ""
		),
	)


# ── 30a. create_leave_request ───────────────────────────────────────────────
def create_leave_request(args: dict) -> ToolResult:
	"""File a leave request as a DRAFT. Approval is a separate, gated act.

	IT COMES BACK docstatus 0, ON PURPOSE. A submitted Leave Application writes
	Leave Ledger Entries and moves a balance; that is the approval, and approving
	is not the same act as asking. `approve_leave_request` is a different tool
	with a different switch, so a site can let its crew file requests without
	letting anything approve them — which is the ordinary arrangement and the one
	that makes this safe to put on a handset.

	THE BALANCE IS CHECKED HERE AND THE REFUSAL SAYS BOTH NUMBERS. hrms checks it
	too and throws "Insufficient leave balance", which does not say what the
	balance was, what was asked for, or which of the two to change.

	UNPAID LEAVE IS EXEMPT, because `is_lwp` means there is no balance to draw
	down — that is hrms's own rule and it is the escape hatch for a farm that has
	not set up Leave Allocations at all. A site in that state gets a refusal that
	says so rather than "you have 0 days", which reads as an entitlement spent.

	`company` IS DERIVED FROM THE EMPLOYEE AND IS NOT AN ARGUMENT. It is `reqd`
	on hrms's doctype, the Desk form fills it in from the same place, and a
	caller who could name a different one would file a worker's absence against
	an entity they do not work for.
	"""
	_require_leave()
	employee = _resolve_employee(as_str(args, "employee", required=True))
	from_date = as_date(args, "from_date", required=True)
	to_date = as_date(args, "to_date", required=True)
	if str(to_date) < str(from_date):
		raise ToolError(
			f"from_date {from_date} is after to_date {to_date}. Nothing was filed."
		)

	leave_type = _leave_type_for(employee, as_str(args, "leave_type"), str(from_date))

	half_day = bool(as_bool(args, "half_day", False))
	half_day_date = as_date(args, "half_day_date")
	if half_day and not half_day_date:
		# hrms defaults it the same way when the span is one day; on a longer one
		# there is no single day it could mean, so it is asked for by name.
		if str(from_date) == str(to_date):
			half_day_date = from_date
		else:
			raise ToolError(
				f"half_day is set over {from_date} to {to_date}, which is more than one day, so "
				"half_day_date has to say which day is the half. Nothing was filed."
			)
	if half_day_date and not (str(from_date) <= str(half_day_date) <= str(to_date)):
		raise ToolError(
			f"half_day_date {half_day_date} is outside the requested span {from_date} to "
			f"{to_date}. Nothing was filed."
		)

	clashes = _overlapping(employee, str(from_date), str(to_date))
	if clashes:
		first = clashes[0]
		raise ToolError(
			f"this employee already has a leave application covering part of {from_date} to "
			f"{to_date}: {first['name']} ({first['leave_type']}, {first['from_date']} to "
			f"{first['to_date']}, {first['status']}). Two rows over one absence deduct twice the "
			"moment both are approved. Withdraw or amend that one first. Nothing was filed."
		)

	days_api = _leave_days_api()
	if days_api is None:
		raise ToolError(
			"this site's HR app does not export get_number_of_leave_days, so the length of this "
			"request cannot be computed correctly — holidays and the employee's own holiday list "
			"are what make it more than a subtraction. File it in the Desk. Nothing was filed."
		)
	total_days = _number(
		days_api(employee, leave_type, from_date, to_date, half_day, half_day_date)
	)
	if total_days <= 0:
		raise ToolError(
			f"{from_date} to {to_date} works out at {total_days} leave day(s) for this employee — "
			"every day in the span is a holiday or a non-working day on their calendar, so there "
			"is nothing to request. Nothing was filed."
		)

	balance = None
	if not _is_lwp(leave_type):
		balance_on = _leave_balance_api()
		if balance_on is None:
			raise ToolError(
				"this site's HR app does not export get_leave_balance_on, so the balance behind "
				"this request cannot be checked. Nothing was filed."
			)
		if leave_type not in _allocated_leave_types(employee, str(from_date)):
			raise ToolError(
				f"nobody has allocated {leave_type!r} to this employee for {from_date}, so there "
				"is no entitlement to draw this against — which is a different thing from having "
				"spent it. Create a Leave Allocation for them, or file this against a leave type "
				"whose `is_lwp` is set. Nothing was filed."
			)
		balance = _number(balance_on(employee, leave_type, str(from_date)))
		if balance < total_days:
			raise ToolError(
				f"this employee has {balance} day(s) of {leave_type!r} as of {from_date} and this "
				f"request is {total_days}. Shorten it, allocate more, or file the remainder "
				"against an unpaid leave type. Nothing was filed."
			)

	company = str(frappe.db.get_value("Employee", employee, "company") or "")
	if not company:
		raise ToolError(
			f"Employee {employee} has no company on their record, and Leave Application requires "
			"one. Set it on the Employee. Nothing was filed."
		)

	doc = frappe.new_doc(LEAVE_APPLICATION)
	doc.employee = employee
	doc.leave_type = leave_type
	doc.from_date = from_date
	doc.to_date = to_date
	doc.half_day = 1 if half_day else 0
	if half_day_date:
		doc.half_day_date = half_day_date
	doc.total_leave_days = total_days
	doc.description = as_str(args, "reason") or None
	doc.posting_date = as_date(args, "posting_date") or frappe.utils.today()
	# `Open` is hrms's own default and the only status a request that nobody has
	# answered yet can honestly carry.
	doc.status = "Open"
	doc.company = company
	if compat.has_field(LEAVE_APPLICATION, "employee_name"):
		doc.employee_name = frappe.db.get_value("Employee", employee, "employee_name")
	if balance is not None and compat.has_field(LEAVE_APPLICATION, "leave_balance"):
		doc.leave_balance = balance
	approver = as_str(args, "leave_approver")
	if approver:
		if not frappe.db.exists("User", approver):
			raise ToolError(f"no User named {approver!r} on this site. Nothing was filed.")
		doc.leave_approver = approver
	doc.insert(ignore_permissions=True)

	data = {
		"leave_application": doc.name,
		"request": _described(_leave_row(doc.name)),
		"balance_before": balance,
		"balance_checked": balance is not None,
		"days_counted_via": "HR get_number_of_leave_days",
		"note": (
			f"Filed as a DRAFT ({doc.name}, status Open). Nothing has been deducted and no Leave "
			"Ledger Entry exists yet — approval is approve_leave_request, which submits it, and "
			"is a separate switch."
			+ (
				f" {leave_type!r} is an unpaid type, so no balance was checked."
				if balance is None
				else f" Balance before this request: {balance} day(s)."
			)
		),
	}
	return ToolResult(
		data,
		f"filed {doc.name}: {total_days} day(s) of {leave_type} for "
		f"{data['request'].get('employee_name') or employee}, {from_date} to {to_date} (draft)",
		docstatus_delta="0",
	)


# ── 30b. list_leave_requests ────────────────────────────────────────────────
def list_leave_requests(args: dict) -> ToolResult:
	"""The leave register: who asked for what, and what was answered.

	DEFAULTS TO EVERYTHING RATHER THAN TO PENDING. A manager opening this wants
	the week, and a filter that had to be turned off to see an approved day is a
	filter that hides the answer to "did that get approved". `status` narrows it
	when the question really is the queue.
	"""
	_require_leave()
	filters: dict = {}
	employee = as_str(args, "employee")
	if employee:
		filters["employee"] = _resolve_employee(employee)
	status = as_str(args, "status")
	if status:
		if status not in LEAVE_STATUSES:
			raise ToolError(
				f"{status!r} is not a Leave Application status. This site's are: "
				f"{', '.join(LEAVE_STATUSES)}."
			)
		filters["status"] = status
	for key, column in (("leave_type", "leave_type"), ("company", "company")):
		value = as_str(args, key)
		if value:
			filters[column] = value
	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	# The window asks "which absences touch these dates", not "which were filed
	# in them" — an application that started last week and runs into this one is
	# the answer to "who is off on Tuesday".
	if to_date:
		filters["from_date"] = ("<=", to_date)
	if from_date:
		filters["to_date"] = (">=", from_date)

	fields = compat.existing_fields(
		LEAVE_APPLICATION,
		(
			"name",
			"employee",
			"employee_name",
			"leave_type",
			"from_date",
			"to_date",
			"half_day",
			"total_leave_days",
			"description",
			"status",
			"company",
			"leave_approver",
			"posting_date",
			"docstatus",
		),
	)
	rows = (
		frappe.db.get_all(
			LEAVE_APPLICATION,
			filters=filters,
			fields=fields,
			order_by="from_date desc",
			limit=min(as_limit(args), LEAVE_CAP),
		)
		or []
	)
	requests = [_described(dict(row)) for row in rows]
	by_status: dict = {}
	for row in requests:
		key = str(row.get("status") or "Open")
		by_status[key] = by_status.get(key, 0) + 1

	data = {
		"requests": requests,
		"count": len(requests),
		"by_status": by_status,
		"pending_count": by_status.get("Open", 0),
		"total_days": round(sum(row["total_leave_days"] for row in requests), 3),
		"filters": {
			"employee": filters.get("employee"),
			"status": status or None,
			"leave_type": as_str(args, "leave_type") or None,
			"company": as_str(args, "company") or None,
			"from_date": str(from_date) if from_date else None,
			"to_date": str(to_date) if to_date else None,
		},
	}
	return ToolResult(
		data,
		f"{len(requests)} leave request(s), {by_status.get('Open', 0)} awaiting an answer",
	)


def _answer_leave(args: dict, *, status: str, verb: str, reason_required: bool) -> ToolResult:
	"""The shared body of approve and reject. They differ by one word and a reason.

	SUBMITTING IS THE ANSWER, and it is what makes the two tools worth having
	separate switches from `create_leave_request`. `on_submit` is where hrms
	writes the Leave Ledger Entry that actually moves the balance, so this is the
	call that spends somebody's entitlement.
	"""
	_require_leave()
	name = as_str(args, "leave_application", required=True)
	row = _leave_row(name)
	docstatus = int(row.get("docstatus") or 0)
	if docstatus == 1:
		raise ToolError(
			f"Leave Application {name} was already answered — it is submitted with status "
			f"{row.get('status')!r}. Changing an answer that has moved a balance is a "
			"cancellation and an amendment, which is done in the Desk. Nothing was changed."
		)
	if docstatus == 2:
		raise ToolError(f"Leave Application {name} is cancelled. Nothing was changed.")

	reason = as_str(args, "reason")
	if reason_required and len(reason) < 4:
		raise ToolError(
			"reason is required and must be a real explanation — a refusal a worker cannot read "
			"is one they will ask about in person anyway. Nothing was changed."
		)

	doc = frappe.get_doc(LEAVE_APPLICATION, name)
	doc.status = status
	if reason:
		existing = str(doc.get("description") or "").strip()
		doc.description = f"{existing}\n\n{verb.title()}: {reason}".strip() if existing else reason
	approver = as_str(args, "leave_approver")
	if approver:
		if not frappe.db.exists("User", approver):
			raise ToolError(f"no User named {approver!r} on this site. Nothing was changed.")
		doc.leave_approver = approver
	doc.save(ignore_permissions=True)
	# `submit()` TAKES NO `ignore_permissions` ARGUMENT and reads the flag off the
	# document instead, so setting it here is not belt-and-braces — without it
	# `_submit` runs its own permission check against `frappe.session.user`, which
	# on the MCP transport is the system user rather than the named
	# `leave_approver`. The double has no such check and cannot show this.
	doc.flags.ignore_permissions = True
	doc.submit()

	answered = _described(_leave_row(name))
	data = {
		"leave_application": name,
		"status": status,
		"request": answered,
		"reason": reason or None,
		"note": (
			f"{name} is {status.lower()} and submitted. "
			+ (
				"hrms writes the Leave Ledger Entry on submit, so the balance has moved."
				if status == "Approved"
				else "A rejected application is submitted so the answer is on the record, and it "
				"draws down no balance."
			)
		),
	}
	return ToolResult(
		data,
		f"{verb} {name}: {answered['total_leave_days']} day(s) of {answered.get('leave_type')} "
		f"for {answered.get('employee_name') or answered.get('employee')}",
		docstatus_delta="0 → 1",
	)


# ── 30c. approve_leave_request ──────────────────────────────────────────────
def approve_leave_request(args: dict) -> ToolResult:
	"""Approve a draft leave request and submit it. THIS MOVES A BALANCE.

	`on_submit` is where hrms writes the Leave Ledger Entry, so this is the call
	that actually spends the entitlement `create_leave_request` only asked for.
	It is a separate tool with a separate switch for exactly that reason: a farm
	can put filing on every handset and keep approving in one pair of hands.
	"""
	return _answer_leave(args, status="Approved", verb="approved", reason_required=False)


# ── 30d. reject_leave_request ───────────────────────────────────────────────
def reject_leave_request(args: dict) -> ToolResult:
	"""Reject a draft leave request, with a reason, and submit the refusal.

	`reason` IS MANDATORY HERE AND OPTIONAL ON APPROVAL, and the asymmetry is the
	point: an approval explains itself and a refusal does not. It is written onto
	the application's own description, so the worker reads the answer on the
	record rather than hearing it second-hand.

	IT IS SUBMITTED RATHER THAN LEFT AS A DRAFT. hrms accepts a submitted
	Rejected application and draws no balance down for it; leaving it open would
	leave a request that has been answered looking like one that has not.
	"""
	return _answer_leave(args, status="Rejected", verb="rejected", reason_required=True)

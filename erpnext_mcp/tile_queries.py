# SPDX-License-Identifier: MIT
"""The queries a tile's badge or list may run — an allowlist. v0.207.0.

docs/design/phone_config_and_compliance_loop.md §3.3. A tile names a query by
id; it never carries a filter, SQL or code. Each query answers
`{count, rows (≤50), tone}` for one user and never raises out of `badge`.
"""

from __future__ import annotations

import frappe

from . import compat, compliance_loop, phone_config

MAX_ROWS = 50
MANAGER_ROLES = ("System Manager", "Farm Manager")
OPEN_STATES = ("Claimed", "In-Progress", "Paused")


def _employee(user: str) -> str:
	if not compat.doctype_exists("Employee"):
		return ""
	return frappe.db.get_value("Employee", {"user_id": user}, "name") or ""


def _companies(user: str, company: str) -> list:
	return [company] if company else (phone_config.person_of(user).get("companies") or [])


def _answer(rows: list, tone: str = "") -> dict:
	count = len(rows)
	return {"count": count, "rows": rows[:MAX_ROWS], "tone": tone or ("attention" if count else "neutral")}


def _task_row(task: dict) -> dict:
	return {
		"doctype": "Farm Task",
		"name": task["name"],
		"title": task.get("task_name") or task["name"],
		"subtitle": task.get("location") or "",
		"state": task.get("state"),
		"due_date": None,
	}


def my_tasks_open(user, company, params):
	employee = _employee(user)
	if not employee:
		return _answer([])
	filters = {"assigned_to": employee, "state": ("in", OPEN_STATES)}
	companies = _companies(user, company)
	if companies:
		filters["company"] = ("in", companies)
	rows = frappe.db.get_all(
		"Farm Task", filters=filters, fields=["name", "task_name", "location", "state"], limit=500
	)
	return _answer([_task_row(dict(r)) for r in rows])


def my_tasks_overdue(user, company, params):
	open_rows = my_tasks_open(user, company, params)["rows"]
	today = str(frappe.utils.today())
	out = []
	for row in open_rows:
		alert = frappe.db.get_value("Farm Task", row["name"], "source_alert")
		due = str(frappe.db.get_value("Compliance Alert", alert, "due_date") or "")[:10] if alert else ""
		if due and due < today:
			out.append({**row, "due_date": due})
	return _answer(out, "critical" if out else "neutral")


def available_tasks(user, company, params):
	person = phone_config.person_of(user)
	filters = {"state": "Available"}
	companies = _companies(user, company)
	if companies:
		filters["company"] = ("in", companies)
	rows = frappe.db.get_all(
		"Farm Task",
		filters=filters,
		fields=["name", "task_name", "location", "state", "skill_required"],
		limit=500,
	)
	skills = {s.casefold() for s in person.get("skills") or []}
	mine = [
		dict(r) for r in rows if not r.get("skill_required") or str(r["skill_required"]).casefold() in skills
	]
	return _answer([_task_row(r) for r in mine])


def _inbox_rows(items: list) -> list:
	return [
		{
			"doctype": "Compliance Alert",
			"name": item["alert"],
			"title": item.get("title"),
			"subtitle": item.get("regulation") or "",
			"state": item.get("state"),
			"due_date": item.get("due_date"),
		}
		for item in items
	]


def compliance_inbox(user, company, params):
	box = compliance_loop.inbox(user, company)
	rows = _inbox_rows(box["overdue"] + box["blocked"] + box["due"])
	tone = "critical" if box["overdue"] else ("attention" if rows else "neutral")
	return _answer(rows, tone)


def compliance_overdue(user, company, params):
	rows = _inbox_rows(compliance_loop.inbox(user, company)["overdue"])
	return _answer(rows, "critical" if rows else "neutral")


def compliance_blocked(user, company, params):
	return _answer(_inbox_rows(compliance_loop.inbox(user, company)["blocked"]))


def my_inspections_open(user, company, params):
	employee = _employee(user)
	if not employee or not compat.doctype_exists("Inspection Session"):
		return _answer([])
	rows = frappe.db.get_all(
		"Inspection Session",
		filters={"worker": employee, "state": ("not in", ("Submitted", "Cancelled"))},
		fields=["name", "template", "location", "state"],
		limit=500,
	)
	return _answer(
		[
			{
				"doctype": "Inspection Session",
				"name": r["name"],
				"title": r.get("template"),
				"subtitle": r.get("location") or "",
				"state": r.get("state"),
				"due_date": None,
			}
			for r in rows
		]
	)


def my_feedback_answered(user, company, params):
	if not compat.doctype_exists("App Feedback"):
		return _answer([])
	since = str(frappe.utils.add_days(frappe.utils.today(), -14))
	rows = frappe.db.get_all(
		"App Feedback",
		filters={"user": user, "status": ("in", ("Resolved", "Won't Fix")), "resolved_at": (">=", since)},
		fields=["name", "feedback_text", "status", "resolved_at"],
		limit=200,
	)
	return _answer(
		[
			{
				"doctype": "App Feedback",
				"name": r["name"],
				"title": str(r.get("feedback_text") or "")[:80],
				"subtitle": r.get("status"),
				"state": r.get("status"),
				"due_date": None,
			}
			for r in rows
		]
	)


def triage_queue(user, company, params):
	if not compat.has_field("App Feedback", "triage_state"):
		return _answer([])
	rows = frappe.db.get_all(
		"App Feedback",
		filters={"triage_state": ("in", ("Auto-classified", "Proposed", "Ticketed"))},
		fields=["name", "triage_summary", "triage_state"],
		limit=200,
	)
	return _answer(
		[
			{
				"doctype": "App Feedback",
				"name": r["name"],
				"title": str(r.get("triage_summary") or "")[:80],
				"subtitle": r.get("triage_state"),
				"state": r.get("triage_state"),
				"due_date": None,
			}
			for r in rows
		]
	)


def my_print_jobs(user, company, params):
	"""v0.208.0. The caller's cards still in the queue, and any that failed."""
	from . import card_print

	# Amendment 4 §D3: no role, no queue — not even a count of one's own.
	if not compat.doctype_exists("Card Print Job") or not card_print.can_request(user):
		return _answer([])
	rows = frappe.db.get_all(
		"Card Print Job",
		filters={"requested_by": user, "status": ("in", ("Queued", "Printing", "Failed"))},
		fields=["name", "reference_title", "job_type", "status"],
		limit=200,
	)
	failed = any(r.get("status") == "Failed" for r in rows)
	return _answer(
		[
			{
				"doctype": "Card Print Job",
				"name": r["name"],
				"title": r.get("reference_title") or r["name"],
				"subtitle": r.get("job_type"),
				"state": r.get("status"),
				"due_date": None,
			}
			for r in rows
		],
		"critical" if failed else "",
	)


def payroll_calendar(user, company, params):
	"""v0.224.0. Tax deposits and filings: overdue, due, upcoming. docs/design/payroll_windows.md §2."""
	from . import payroll_calendar as calendar

	if not calendar.enabled():
		return _answer([])
	companies = [company] if company else calendar._companies_of(user)
	rows = [
		{
			"doctype": None,
			"name": r["name"],
			"title": r["title"],
			"subtitle": r["subtitle"],
			"state": r["state"],
			"due_date": r["due_date"],
		}
		for r in calendar.items(companies)
		if r["state"] != "done"
	]
	urgent = [r for r in rows if r["state"] in ("overdue", "due")]
	answer = _answer(rows, "critical" if any(r["state"] == "overdue" for r in rows) else "")
	answer["count"] = len(urgent)
	return answer


def punch_reviews(user, company, params):
	"""v0.227.0. Punch times received long after the tap, waiting for a manager."""
	from . import punch_times

	rows = [
		{
			"doctype": None,
			"name": r["row"],
			"title": r.get("employee_name") or r.get("employee") or r["row"],
			"subtitle": r.get("reason") or "",
			"state": "due",
		}
		for r in punch_times.pending([company] if company else None)
	]
	answer = _answer(rows)
	answer["count"] = len(rows)
	return answer


#: id → (function, params schema {name: type}, roles gate or ()).
QUERIES = {
	"my_tasks_open": (my_tasks_open, {}, ()),
	"my_tasks_overdue": (my_tasks_overdue, {}, ()),
	"available_tasks": (available_tasks, {}, ()),
	"compliance_inbox": (compliance_inbox, {}, ()),
	"compliance_overdue": (compliance_overdue, {}, ()),
	"compliance_blocked": (compliance_blocked, {}, ()),
	"my_inspections_open": (my_inspections_open, {}, ()),
	"my_feedback_answered": (my_feedback_answered, {}, ()),
	"triage_queue": (triage_queue, {}, MANAGER_ROLES),
	"my_print_jobs": (my_print_jobs, {}, ("Card Print Requester", "Farm Manager", "System Manager")),
	# v0.224.0. Off until `payroll_calendar_enabled`; see AVAILABLE.
	"payroll_calendar": (
		payroll_calendar,
		{},
		("System Manager", "HR Manager", "HR User", "Accounts Manager"),
	),
	# v0.227.0. Punches to review (`punch_times`).
	"punch_reviews": (punch_reviews, {}, ("Farm Manager", "HR Manager", "HR User", "System Manager")),
}


#: v0.224.0. A query switched off by a setting hides its tile entirely (tiles.hidden_reason).
def _payroll_calendar_on() -> bool:
	from . import payroll_calendar

	return payroll_calendar.enabled()


AVAILABLE = {"payroll_calendar": _payroll_calendar_on}


def available(query: str) -> bool:
	check = AVAILABLE.get(str(query or ""))
	try:
		return True if check is None else bool(check())
	except Exception:
		return False


def problems(spec, audience: dict) -> list:
	if not isinstance(spec, dict) or spec.get("query") not in QUERIES:
		return [f"query must be one of {', '.join(QUERIES)}"]
	_function, schema, gate = QUERIES[spec["query"]]
	params = spec.get("params") or {}
	if not isinstance(params, dict):
		return ["query params must be an object"]
	out = [f"query {spec['query']} takes no parameter {name!r}" for name in params if name not in schema]
	if gate:
		roles = set((audience or {}).get("roles") or [])
		if not roles or not roles <= set(gate):
			out.append(
				f"query {spec['query']} is for {' / '.join(gate)} — limit the audience's roles to those"
			)
	return out


def run(query: str, user: str, company: str = "", params=None) -> dict:
	function, _schema, gate = QUERIES[query]
	if gate and not set(gate) & set(frappe.get_roles(user) or []):
		return _answer([])
	return function(user, company, params or {})


def badge(spec, user: str, company: str = ""):
	"""{count, tone} for a tile, or None. A failing query is None, never an error."""
	if not spec or spec.get("query") not in QUERIES:
		return None
	try:
		answer = run(spec["query"], user, company, spec.get("params"))
	except Exception:
		frappe.log_error(title=f"tile badge {spec.get('query')}", message=frappe.get_traceback())
		return None
	return {"count": answer["count"], "tone": answer["tone"]}

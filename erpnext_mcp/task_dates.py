# SPDX-License-Identifier: MIT
"""Start and due dates on Farm Tasks; blocked-by. v0.236.0. docs/design/ccf_core_work_timing.md §6.

Approved queue item 2 (Tim, 2026-10-04). The dates are fields; everything that READS them
in a policy way — overdue alerts, reminders two days before at 06:00 site time — is a CCF
rule (`farm_task_due`, seeded off for a person to approve: decision 5), not code here.

* `start_date`, `due_date`, `starts_after` (free text, "after the hail net is up") on the task;
  `default_start_after_days` / `default_due_after_days` on a template fill them when a task is
  raised from it and the caller gave none.
* BLOCKED BY: a `blocked_by` Farm Task Link stops START until the blocker is finished. A blocker
  that was CANCELLED or REJECTED no longer blocks, and the start says so (decision 30).
* Overdue is computed, never stored: a phone computes it offline from the cached date too.
"""

from __future__ import annotations

import datetime

import frappe

from . import compat

FIELDS = ("start_date", "due_date", "starts_after")
TEMPLATE_FIELDS = ("default_start_after_days", "default_due_after_days")

#: States in which a task is still work to do.
OPEN_STATES = ("Draft", "Available", "Claimed", "In-Progress", "Paused")
#: A blocker in one of these is done with, one way or the other.
FINISHED = ("Completed", "Awaiting-Review")
GONE = ("Cancelled", "Rejected", "Merged")

#: The reminder band (decision 32): two days before.
DUE_SOON_DAYS = 2


def _date(value) -> datetime.date | None:
	try:
		return datetime.date.fromisoformat(str(value)[:10])
	except (TypeError, ValueError):
		return None


def _today() -> datetime.date:
	return _date(frappe.utils.today()) or datetime.date.today()


def apply(doc, args: dict, template: str = "") -> None:
	"""Set the dates on a new task from `args`, else from the template's defaults."""
	for field in FIELDS:
		if not compat.has_field("Farm Task", field):
			return
	start = _date(args.get("start_date"))
	due = _date(args.get("due_date"))
	if template and (start is None or due is None) and compat.doctype_exists("Farm Task Template"):
		defaults = frappe.db.get_value(
			"Farm Task Template", template, compat.existing_fields("Farm Task Template", TEMPLATE_FIELDS), as_dict=True
		) or {}
		today = _today()
		if start is None and defaults.get("default_start_after_days") not in (None, ""):
			start = today + datetime.timedelta(days=int(defaults["default_start_after_days"] or 0))
		if due is None and defaults.get("default_due_after_days") not in (None, ""):
			due = today + datetime.timedelta(days=int(defaults["default_due_after_days"] or 0))
	if start and due and due < start:
		raise ValueError(f"due_date {due} is before start_date {start}.")
	doc.start_date = start.isoformat() if start else None
	doc.due_date = due.isoformat() if due else None
	doc.starts_after = str(args.get("starts_after") or "").strip()[:140] or None


def describe(row: dict, today: datetime.date | None = None) -> dict:
	"""The date facts a list or a phone shows: dates, days until due, overdue, due soon."""
	today = today or _today()
	due = _date(row.get("due_date"))
	start = _date(row.get("start_date"))
	open_ = str(row.get("state") or "") in OPEN_STATES
	days = (due - today).days if due else None
	return {
		"start_date": start.isoformat() if start else None,
		"due_date": due.isoformat() if due else None,
		"starts_after": row.get("starts_after") or None,
		"days_until_due": days,
		"overdue": bool(open_ and days is not None and days < 0),
		"due_soon": bool(open_ and days is not None and 0 <= days <= DUE_SOON_DAYS),
		"not_before": bool(open_ and start and start > today),
	}


def blockers(task: str) -> tuple[list, list]:
	"""(open blockers, released blockers) — tasks this one is `blocked_by`.

	Open: still to be done. Released: cancelled or rejected, so no longer blocking — the
	start carries a note naming them (decision 30).
	"""
	if not frappe.db.exists("Farm Task", task):
		return [], []
	# Read off the task, as `link_farm_tasks` writes them: one row per link, on both records.
	links = frappe.get_doc("Farm Task", task).get("linked_tasks") or []
	names = [
		str(link.get("linked_task"))
		for link in links
		if link.get("linked_task") and str(link.get("relationship") or "") == "blocked_by"
	]
	if not names:
		return [], []
	rows = frappe.db.get_all("Farm Task", filters={"name": ("in", names)}, fields=["name", "task_name", "state"])
	open_, released = [], []
	for row in rows or []:
		state = str(row.get("state") or "")
		entry = {"task": row["name"], "task_name": row.get("task_name") or row["name"], "state": state}
		if state in GONE:
			released.append(entry)
		elif state not in FINISHED:
			open_.append(entry)
	return open_, released


def overdue(company: str = "", limit: int = 200) -> list:
	"""Open tasks whose due date has passed, oldest first."""
	if not compat.has_field("Farm Task", "due_date"):
		return []
	filters = {"due_date": ("<", _today().isoformat()), "state": ("in", list(OPEN_STATES))}
	if company:
		filters["company"] = company
	rows = frappe.db.get_all(
		"Farm Task",
		filters=filters,
		fields=compat.existing_fields(
			"Farm Task", ("name", "task_name", "state", "company", "location", "assigned_to", "assigned_to_name",
			              "start_date", "due_date", "starts_after", "urgency")
		),
		order_by="due_date asc",
		limit=limit,
	)
	return [{**dict(row), **describe(dict(row))} for row in rows or []]


def rule_spec() -> dict:
	"""The overdue / due-soon rule, as a CCF primitive rule: seeded OFF (decision 5)."""
	return {
		"rule_id": "farm_task_due",
		"title": "Farm task due or overdue",
		"category": "Work Timing",
		"target_doctype": "Farm Task",
		"date_field": "due_date",
		"date_field_role": "Clock",
		"cadence_days": 0,
		"threshold_critical_days": 0,
		"threshold_warning_days": DUE_SOON_DAYS,
		"severity_expired": "Critical",
		"scope_filters": [{"field": "state", "op": "in", "value": list(OPEN_STATES)}],
		"kairotic_gate_description": (
			"An open task with a due date: Warning two days before (the 06:00 reminder band), Critical once it "
			"is past. A finished, cancelled or undated task raises nothing."
		),
		"message_template": "{{ task_name or name }} is due {{ due_date }} ({{ days_remaining }} day(s)).",
		"regimes": ["Internal"],
		"enabled": 0,
		"authored_by": "System",
		"purpose": "Work that has a date is seen before it is late, by whoever holds it and their supervisor.",
	}

# SPDX-License-Identifier: MIT
"""Crew tasks: many people on one Farm Task, each with their own time. v0.213.0.

docs/design/crew_tasks.md.

A Farm Task has one live holder, and "these twelve people are pruning Block 4"
could not be said. A crew task keeps that one holder — THE LEAD, the supervisor
who starts it and closes it with one evidence set — and adds one Farm Task
Assignment per worker per stint in two states of their own, `On Crew` and
`Off Crew`. Those are not live states, so the one-live-assignment rule, the
claim limit and "what is this worker holding" never see them and an individual
task behaves exactly as it did.

A CREW ROW IS AN ASSIGNMENT WITH THE COLUMNS AN ASSIGNMENT ALREADY HAS:
`started_at`, `completed_at`, `actual_duration_minutes`, `farm_shift`. That is
the whole of how labour reaches a block's cost centre — the payroll split
(`tools/payroll.py`, v0.101.0) reads every assignment by person, start and
minutes and places it on the task's block.

NOTHING HERE IS DELETED. Coming off a crew closes the row with a time and a
reason; coming back is a new row.

This module is the reads, the roll-up and the closes. The gated tools are
`tools/crew_tasks.py`.
"""

from __future__ import annotations

import json

import frappe

from . import compat

FARM_TASK = "Farm Task"
ASSIGNMENT = "Farm Task Assignment"
FARM_SHIFT = "Farm Shift"
BUCKET_LOG = "Bucket Log Entry"

CREW = "Crew"
INDIVIDUAL = "Individual"
ON_CREW = "On Crew"
OFF_CREW = "Off Crew"

#: Who may add, remove, set the lead and close. Crew Leader is a role operators
#: create by hand (see `tools/employee.SHIFT_ROLES`); naming it costs nothing on
#: a site without one.
ROLES = ("System Manager", "Farm Manager", "Foreman", "Crew Leader")

#: Task states in which a crew may be on the job.
WORKING_STATES = ("Claimed", "In-Progress", "Paused")

MEMBER_FIELDS = (
	"name",
	"task",
	"assigned_to",
	"assigned_to_name",
	"state",
	"company",
	"started_at",
	"completed_at",
	"actual_duration_minutes",
	"farm_shift",
	"pieces",
	"piece_unit",
	"section",
	"part_done",
	"member_notes",
	"end_reason",
	"added_by",
	"ended_by",
)

MEMBER_CAP = 500


def ready() -> bool:
	"""Whether this site has migrated the crew columns."""
	return compat.has_field(FARM_TASK, "is_crew_task") and compat.has_field(ASSIGNMENT, "end_reason")


def is_crew(task: dict) -> bool:
	return compat.checked(task.get("is_crew_task")) or str(task.get("work_mode") or "") == CREW


def mode_argument(args: dict) -> bool | None:
	"""`is_crew_task` / `work_mode` off the arguments, or None when neither was sent."""
	mode = str(args.get("work_mode") or "").strip().casefold()
	if mode:
		if mode not in ("crew", "individual"):
			from .errors import ToolError

			raise ToolError("work_mode is Individual or Crew. Nothing was changed.")
		return mode == "crew"
	if args.get("is_crew_task") is None:
		return None
	return compat.checked(args.get("is_crew_task")) or str(args.get("is_crew_task")).casefold() == "true"


# ── sections ────────────────────────────────────────────────────────────────
def sections_of(raw) -> list:
	"""The stored sections, as a list. Never raises on a hand-edited column."""
	try:
		value = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or [])
	except Exception:
		return []
	if isinstance(value, dict):
		value = value.get("sections") or []
	return [dict(entry) for entry in value if isinstance(entry, dict) and entry.get("key")]


def _row_number(value):
	try:
		return int(value) if value not in (None, "") else None
	except (TypeError, ValueError):
		return None


def normalise_sections(value, existing=()) -> list:
	"""A caller's sections, cleaned; the done-state of a key that survives is kept."""
	from .errors import ToolError

	if isinstance(value, str):
		try:
			value = json.loads(value) if value.strip() else []
		except Exception as error:
			raise ToolError(f"sections is not valid JSON ({error}). Nothing was changed.") from error
	if value in (None, ""):
		return []
	if not isinstance(value, (list, tuple)):
		raise ToolError(
			"sections is a list — [{label, from_row, to_row}] or ['North half', 'South half']. "
			"Nothing was changed."
		)
	before = {str(entry.get("key")): entry for entry in existing or []}
	out, seen = [], set()
	for index, entry in enumerate(value, start=1):
		if isinstance(entry, str):
			entry = {"label": entry}
		if not isinstance(entry, dict):
			raise ToolError(f"section {index} is not an object or a label. Nothing was changed.")
		first, last = _row_number(entry.get("from_row")), _row_number(entry.get("to_row"))
		if first is not None and last is not None and last < first:
			raise ToolError(
				f"section {index} runs from row {first} to row {last}, which is backwards. "
				"Nothing was changed."
			)
		label = str(entry.get("label") or "").strip()
		if not label:
			if first is None:
				raise ToolError(f"section {index} has no label and no rows. Nothing was changed.")
			label = f"Rows {first}–{last}" if last is not None and last != first else f"Row {first}"
		key = str(entry.get("key") or "").strip() or (
			f"rows_{first}_{last if last is not None else first}" if first is not None else f"s{index}"
		)
		if key in seen:
			raise ToolError(f"two sections share the key {key!r}. Nothing was changed.")
		seen.add(key)
		kept = before.get(key) or {}
		out.append(
			{
				"key": key,
				"label": label,
				"from_row": first,
				"to_row": last,
				"done": bool(kept.get("done")),
				"done_at": kept.get("done_at") or None,
				"done_by": kept.get("done_by") or None,
			}
		)
	return out


def progress(sections: list) -> dict | None:
	if not sections:
		return None
	done = [entry for entry in sections if entry.get("done")]
	todo = [entry for entry in sections if not entry.get("done")]
	parts = []
	if done:
		parts.append(", ".join(str(entry["label"]) for entry in done) + " done")
	if todo:
		parts.append(", ".join(str(entry["label"]) for entry in todo) + " open")
	return {
		"done": len(done),
		"total": len(sections),
		"percent": round(100.0 * len(done) / len(sections)),
		"summary": "; ".join(parts),
	}


# ── rows ────────────────────────────────────────────────────────────────────
def _rows(filters: dict, limit: int = MEMBER_CAP) -> list:
	if not ready():
		return []
	return [
		dict(row)
		for row in frappe.db.get_all(
			ASSIGNMENT,
			filters=filters,
			fields=compat.existing_fields(ASSIGNMENT, MEMBER_FIELDS),
			order_by="creation asc",
			limit=limit,
		)
		or []
	]


def member_rows(task: str) -> list:
	"""Every crew row this task has ever had, oldest first."""
	return _rows({"task": task, "state": ("in", [ON_CREW, OFF_CREW])})


def open_rows(task: str = "", employee: str = "", shift: str = "") -> list:
	filters: dict = {"state": ON_CREW}
	if task:
		filters["task"] = task
	if employee:
		filters["assigned_to"] = employee
	if shift:
		filters["farm_shift"] = shift
	return _rows(filters)


def on_crew(task: str, employee: str) -> dict:
	rows = open_rows(task=task, employee=employee)
	return rows[0] if rows else {}


def _seconds(value) -> float | None:
	if not value:
		return None
	try:
		return frappe.utils.get_datetime(str(value)).timestamp()
	except Exception:
		return None


def minutes_between(start, end) -> int:
	a, b = _seconds(start), _seconds(end)
	if a is None or b is None or b <= a:
		return 0
	return round((b - a) / 60.0)


def minutes_of(row: dict, now: str = "") -> int:
	"""A closed row's stored minutes; an open row's start to now."""
	if str(row.get("state")) == ON_CREW:
		return minutes_between(row.get("started_at"), now or frappe.utils.now())
	stored = int(row.get("actual_duration_minutes") or 0)
	return stored or minutes_between(row.get("started_at"), row.get("completed_at"))


def buckets_for(employee: str, start, end) -> int | None:
	"""Accepted bucket scans for one picker inside one stretch of time, or None.

	The existing BucketLog path, READ and never written. None where the site has
	no bucket log or it cannot say whose a bucket is — which is different from
	zero, and is why the key is then left off the answer.
	"""
	if not (employee and start and compat.doctype_exists(BUCKET_LOG)):
		return None
	picker = compat.first_field(BUCKET_LOG, "picker_id", "employee", "picker", "worker")
	when = compat.first_field(BUCKET_LOG, "timestamp", "logged_at", "creation")
	if not picker or not when:
		return None
	try:
		filters: list = [[picker, "=", employee], [when, ">=", str(start)]]
		filters.append([when, "<=", str(end or frappe.utils.now())])
		if compat.has_field(BUCKET_LOG, "verdict"):
			from . import bucket_bridge

			filters.append(["verdict", "=", bucket_bridge.VERDICT_ACCEPTED])
		return len(frappe.db.get_all(BUCKET_LOG, filters=filters, pluck="name", limit_page_length=0) or [])
	except Exception:
		return None


def describe_member(row: dict, now: str = "", default_unit: str = "") -> dict:
	open_now = str(row.get("state")) == ON_CREW
	out = {
		"assignment": row.get("name"),
		"employee": row.get("assigned_to"),
		"employee_name": row.get("assigned_to_name") or row.get("assigned_to"),
		"state": row.get("state"),
		"on_now": open_now,
		"started_at": str(row.get("started_at") or "") or None,
		"ended_at": None if open_now else (str(row.get("completed_at") or "") or None),
		"minutes": minutes_of(row, now),
		"farm_shift": row.get("farm_shift") or None,
		"pieces": float(row.get("pieces") or 0) or None,
		"piece_unit": row.get("piece_unit") or default_unit or None,
		"section": row.get("section") or None,
		"part_done": compat.checked(row.get("part_done")),
		"notes": row.get("member_notes") or None,
		"end_reason": row.get("end_reason") or None,
	}
	buckets = buckets_for(
		str(row.get("assigned_to") or ""),
		row.get("started_at"),
		None if open_now else row.get("completed_at"),
	)
	if buckets is not None:
		out["buckets"] = buckets
	return out


def describe(task: dict, with_members: bool = True) -> dict | None:
	"""The `crew` block of a crew task (contract §3), or None for an individual one."""
	if not is_crew(task):
		return None
	now = str(frappe.utils.now())
	unit = str(task.get("crew_piece_unit") or "")
	members = [describe_member(row, now, unit) for row in member_rows(str(task.get("name") or ""))]
	sections = sections_of(task.get("crew_sections"))
	lead = str(task.get("assigned_to") or "")
	out = {
		"is_crew_task": True,
		"lead": lead or None,
		"lead_name": (task.get("assigned_to_name") or lead) or None,
		"piece_unit": unit or None,
		"on_now": len([m for m in members if m["on_now"]]),
		"headcount": len({m["employee"] for m in members}),
		"person_minutes": sum(m["minutes"] for m in members),
		"pieces": round(sum(float(m["pieces"] or 0) for m in members), 3),
		"sections": sections,
		"progress": progress(sections),
	}
	if any("buckets" in m for m in members):
		out["buckets"] = sum(int(m.get("buckets") or 0) for m in members)
	if with_members:
		# On the job first, then who has left, each in the order they arrived.
		out["members"] = sorted(members, key=lambda m: (not m["on_now"], str(m["started_at"] or "")))
	return out


def stamp_rollup(task: str) -> dict | None:
	"""Write the four roll-up columns. `set_value`, so no controller runs. Never raises."""
	try:
		row = dict(
			frappe.db.get_value(
				FARM_TASK,
				task,
				compat.existing_fields(
					FARM_TASK,
					(
						"name",
						"is_crew_task",
						"work_mode",
						"crew_piece_unit",
						"crew_sections",
						"assigned_to",
						"assigned_to_name",
					),
				),
				as_dict=True,
			)
			or {}
		)
		crew = describe(row, with_members=False)
		if crew is None:
			return None
		for column, value in (
			("crew_headcount", crew["headcount"]),
			("crew_on_now", crew["on_now"]),
			("crew_person_minutes", crew["person_minutes"]),
			("crew_pieces", crew["pieces"]),
		):
			if compat.has_field(FARM_TASK, column):
				frappe.db.set_value(FARM_TASK, task, column, value)
		return crew
	except Exception:  # pragma: no cover - a roll-up is never worth losing a write
		return None


# ── closes ──────────────────────────────────────────────────────────────────
def close_row(name: str, when: str = "", reason: str = "", by: str = "", part_done: bool = False) -> dict:
	"""Take one person off a crew: a time, a reason, and the row kept."""
	doc = frappe.get_doc(ASSIGNMENT, name)
	if str(doc.state) != ON_CREW:
		return dict(doc.as_dict())
	now = str(frappe.utils.now())
	end = str(when or now)
	started = str(doc.started_at or doc.claimed_at or end)
	# Never before they started, and never in the future.
	if (_seconds(end) or 0) < (_seconds(started) or 0):
		end = started
	if (_seconds(end) or 0) > (_seconds(now) or 0):
		end = now
	doc.state = OFF_CREW
	doc.completed_at = end
	doc.actual_duration_minutes = minutes_between(started, end)
	doc.end_reason = (reason or "Removed from the crew")[:500]
	doc.ended_by = by or None
	if part_done:
		doc.part_done = 1
	doc.save(ignore_permissions=True)
	return dict(doc.as_dict())


def close_all(task: str, when: str = "", reason: str = "", by: str = "") -> int:
	rows = open_rows(task=task)
	for row in rows:
		close_row(str(row["name"]), when, reason, by)
	if rows:
		stamp_rollup(task)
	return len(rows)


def close_for_shift(shift: str, employee: str = "", when: str = "", reason: str = "") -> int:
	"""A worker left their shift (or it ended): their time on a crew task ends with it.

	NEVER RAISES. It is called from the shift tools after their own write, and a
	crew row that would not close must not cost a foreman the shift close.
	"""
	try:
		rows = open_rows(shift=shift, employee=employee)
		for row in rows:
			close_row(str(row["name"]), when, reason or "Left the shift")
		for task in {str(row.get("task")) for row in rows}:
			stamp_rollup(task)
		return len(rows)
	except Exception:  # pragma: no cover
		return 0


def close_when_task_stops(doc) -> None:
	"""Farm Task `on_update`: a crew task that is no longer being worked has no crew on it."""
	try:
		if not ready() or not is_crew(doc.as_dict() if hasattr(doc, "as_dict") else dict(doc)):
			return
		state = str(doc.get("state") or "")
		if state in WORKING_STATES or state == "Draft":
			return
		if not open_rows(task=doc.name):
			return
		reason = {
			"Completed": "Task completed",
			"Awaiting-Review": "Task completed",
			"Rejected": "Task rejected",
			"Cancelled": "Task cancelled",
			"Merged": "Task merged",
			"Available": "Task handed back",
		}.get(state, f"Task is {state}")
		close_all(doc.name, str(doc.get("completed_at") or ""), reason)
	except Exception:  # pragma: no cover - never worth losing the task's own save
		pass


def my_role(task: dict, employee: str) -> str | None:
	"""`lead`, `member` or None — what this person is on this crew task."""
	if not employee or not is_crew(task):
		return None
	if str(task.get("assigned_to") or "") == employee:
		return "lead"
	return "member" if on_crew(str(task.get("name") or ""), employee) else None

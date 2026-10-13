# SPDX-License-Identifier: MIT
"""On Hold — a Farm Task that nobody should pick up yet, and why. v0.324.0.

WHY. Tim's note AFB-2026-00034: "We have not started removing trees yet." The Bing removal parent FT-2026-10-00002
had been Claimed since Oct 3 and showed as active work, while its notes said ON HOLD until the Mill Creek land
transfer closes — a hold that lived only in free text, which no screen and no rule could read.

WHAT IT IS. A task state of its own, `On Hold`, with the reason (required), what it waits for (`hold_until_event`,
"Mill Creek land transfer closes"), an optional date, who held it and when, and the state it was in. It is NOT
`Paused`, which is a worker stopping the clock mid-job.

- HOLDING closes any live assignment as `Held` (the minutes worked are kept; evidence and history are untouched)
  and takes the task off the pool and off the worker's list. `cascade` holds the step tasks under it too.
- WHILE HELD it cannot be claimed, assigned or started; the refusal names the reason and what it waits for.
- RELEASING puts it back where it came from (Draft stays Draft; anything that was being worked returns to the pool,
  Available) — by hand, or by the daily sweep on `hold_until_date` when `hold_auto_release` is ticked, which then
  tells the person who held it and the task's creator (a push to their phones and a comment on the task).

Every hold and release is a comment on the task's timeline; through MCP it is also an MCP Action Log row.
"""

from __future__ import annotations

import frappe

from . import compat

TASK = "Farm Task"
ASSIGNMENT = "Farm Task Assignment"
ON_HOLD = "On Hold"
HELD = "Held"  # an assignment ended because its task went on hold
HOLDABLE = ("Draft", "Available", "Claimed", "In-Progress", "Paused")
LIVE_ASSIGNMENT = ("Claimed", "In-Progress", "Paused")
FIELDS = ("hold_reason", "hold_until_event", "hold_until_date", "held_by", "held_at", "held_from_state",
          "hold_auto_release")
MAX_CASCADE = 50


class HoldError(ValueError):
	pass


def ready() -> bool:
	return compat.has_field(TASK, "hold_reason")


def _row(task: str) -> dict:
	cols = compat.existing_fields(TASK, ("name", "task_name", "state", "owner", "company", "parent_task",
	                                     "parent_doctype", *FIELDS))
	row = frappe.db.get_value(TASK, task, cols, as_dict=True)
	if not row:
		raise HoldError(f"no Farm Task called {task!r}.")
	return dict(row)


def describe(row: dict) -> dict | None:
	"""The hold as a task payload carries it, or None for a task that is not held."""
	if row.get("state") != ON_HOLD:
		return None
	return {"reason": row.get("hold_reason") or None, "until_event": row.get("hold_until_event") or None,
	        "until_date": str(row.get("hold_until_date") or "") or None, "held_by": row.get("held_by") or None,
	        "held_at": str(row.get("held_at") or "") or None, "auto_release": bool(row.get("hold_auto_release"))}


def sentence(row: dict) -> str:
	"""'On hold: <reason> — until <event / date>.' The refusal every claim and start of a held task gets."""
	until = " / ".join(str(x) for x in (row.get("hold_until_event"), row.get("hold_until_date")) if x)
	return f"{row.get('name')} is on hold: {row.get('hold_reason') or 'no reason recorded'}" + (
		f" — until {until}." if until else ".")


def refuse_if_held(row: dict) -> None:
	"""Called by claim / assign / start: a held task is not picked up."""
	if row.get("state") == ON_HOLD:
		raise HoldError(sentence(row) + " Release the hold first (release_farm_task).")


def _children(task: str) -> list:
	if not compat.has_field(TASK, "parent_task"):
		return []
	return [str(n) for n in frappe.db.get_all(TASK, filters={"parent_task": task, "parent_doctype": TASK},
	                                          pluck="name", limit=MAX_CASCADE) or []]


def _comment(task: str, text: str) -> None:
	try:
		frappe.get_doc(TASK, task).add_comment("Info", text)
	except Exception:  # noqa: BLE001 — the timeline line is a courtesy; the hold itself is the record
		pass


def _end_assignments(task: str, reason: str) -> list:
	from .tools import dispatch

	ended = []
	# A crew on the job comes off it — the crew rows are not live claims, but nobody stays on held work.
	for name in frappe.db.get_all(ASSIGNMENT, filters={"task": task, "state": "On Crew"}, pluck="name") or []:
		frappe.db.set_value(ASSIGNMENT, name, {"state": "Off Crew", "completed_at": frappe.utils.now()})
		ended.append({"assignment": name, "crew": True})
	for name in frappe.db.get_all(ASSIGNMENT, filters={"task": task, "state": ("in", list(LIVE_ASSIGNMENT))},
	                              pluck="name") or []:
		doc = frappe.get_doc(ASSIGNMENT, name)
		now = frappe.utils.now()
		try:
			dispatch._close_segment(doc, now, "hold", reason)
			doc.actual_duration_minutes = dispatch.active_minutes(doc)
		except Exception:  # noqa: BLE001 — a crew row without a clock still ends
			pass
		doc.state = HELD
		doc.completed_at = now
		doc.paused_at = None
		if compat.has_field(ASSIGNMENT, "end_reason"):
			doc.end_reason = f"Task put on hold: {reason}"[:140]
		doc.save(ignore_permissions=True)
		ended.append({"assignment": doc.name, "worker": doc.get("assigned_to_name") or doc.get("assigned_to")})
	return ended


def hold(task: str, reason: str, *, until_event: str = "", until_date=None, auto_release: bool = False,
         cascade: bool = False, actor: str = "") -> dict:
	"""Put a task (and with `cascade` its step tasks) on hold. Validated before anything is written."""
	if not ready():
		raise HoldError("this site has not migrated to v0.324.0 yet (Farm Task has no hold fields).")
	reason = str(reason or "").strip()
	if not reason:
		raise HoldError("hold_reason is required — what the work is waiting for is the whole point of a hold.")
	if until_date:
		until_date = str(frappe.utils.getdate(until_date))
	if auto_release and not until_date:
		raise HoldError("auto-release needs hold_until_date — the day the sweep releases it.")
	root = _row(task)
	targets = [root] + ([_row(c) for c in _children(task)] if cascade else [])
	if root["state"] not in HOLDABLE and root["state"] != ON_HOLD:
		raise HoldError(f"{task} is {root['state']}; a finished task is not put on hold.")
	actor = actor or str(frappe.session.user or "")
	out = []
	for row in targets:
		if row["state"] not in HOLDABLE:
			out.append({"task": row["name"], "state": row["state"], "held": False,
			            "note": "already on hold" if row["state"] == ON_HOLD else f"{row['state']} — left alone"})
			continue
		ended = _end_assignments(row["name"], reason)
		frappe.db.set_value(TASK, row["name"], {
			"state": ON_HOLD, "hold_reason": reason[:500], "hold_until_event": (until_event or "")[:140] or None,
			"hold_until_date": until_date or None, "held_by": actor or None, "held_at": frappe.utils.now(),
			"held_from_state": row["state"], "hold_auto_release": 1 if auto_release else 0,
			"assigned_to": None, "assigned_to_name": None})
		_comment(row["name"], f"On hold ({actor}): {reason}" + (
			f" — until {' / '.join(str(x) for x in (until_event, until_date) if x)}" if (until_event or until_date) else ""))
		out.append({"task": row["name"], "was": row["state"], "held": True, "assignments_ended": ended})
	return {"task": task, "cascade": cascade, "reason": reason, "until_event": until_event or None,
	        "until_date": until_date or None, "auto_release": bool(auto_release), "tasks": out,
	        "held_count": sum(1 for r in out if r["held"])}


def release(task: str, *, cascade: bool = False, actor: str = "", note: str = "", automatic: bool = False) -> dict:
	"""Take a task (and with `cascade` its step tasks) off hold, back to Draft or the pool."""
	root = _row(task)
	targets = [root] + ([_row(c) for c in _children(task)] if cascade else [])
	if root["state"] != ON_HOLD:
		raise HoldError(f"{task} is {root['state']}, not on hold.")
	actor = actor or str(frappe.session.user or "")
	out = []
	for row in targets:
		if row["state"] != ON_HOLD:
			out.append({"task": row["name"], "state": row["state"], "released": False})
			continue
		back = "Draft" if row.get("held_from_state") == "Draft" else "Available"
		frappe.db.set_value(TASK, row["name"], {"state": back, "hold_auto_release": 0})
		_comment(row["name"], ("Hold released automatically on its date" if automatic else f"Hold released ({actor})")
		         + (f": {note}" if note else "") + f" — was: {row.get('hold_reason') or ''}")
		out.append({"task": row["name"], "released": True, "to": back, "was_held_for": row.get("hold_reason")})
	return {"task": task, "cascade": cascade, "tasks": out, "released_count": sum(1 for r in out if r["released"])}


def auto_release_due() -> dict:
	"""Daily: release every held task whose `hold_until_date` has come and that asked to be. Never raises."""
	report = {"released": [], "failed": []}
	if not ready():
		return report
	today = frappe.utils.today()
	for row in frappe.db.get_all(TASK, filters={"state": ON_HOLD, "hold_auto_release": 1,
	                                            "hold_until_date": ("<=", today)},
	                             fields=["name", "task_name", "held_by", "owner", "hold_reason"], limit=500) or []:
		try:
			release(row["name"], actor="Administrator", automatic=True)
			_notify(row)
			report["released"].append(row["name"])
		except Exception as exc:  # noqa: BLE001
			report["failed"].append({"task": row["name"], "error": str(exc)[:200]})
	return report


def _notify(row: dict) -> None:
	"""The person who held it and the task's creator hear that it is back in play."""
	from .services import push as push_service

	users = {u for u in (row.get("held_by"), row.get("owner")) if u and u not in ("Administrator", "Guest")}
	employees = [e for e in (frappe.db.get_value("Employee", {"user_id": u}, "name") for u in users) if e] \
		if compat.doctype_exists("Employee") and compat.has_field("Employee", "user_id") else []
	if not employees:
		return
	try:
		push_service.send_push_to_employees(employees, push_service.task_payload(
			task=row["name"], task_name=f"Off hold: {row.get('task_name') or row['name']}", location="",
			urgency="", reassigned=False), collapse_id=row["name"])
	except Exception:  # noqa: BLE001 — a phone that cannot be reached does not undo the release
		pass

# SPDX-License-Identifier: MIT
"""The irrigation schedule: planned sets, raised as tasks, checked against what the valves logged.
v0.248.0. Tim's decision 18 (2026-10-04): no overhead-water flag; build a real schedule reusing zones,
valves and runtime. Approved queue item 9.

* NO NEW REGISTER: a zone's schedule is a table on the Irrigation Zone (Irrigation Schedule Line): the valve
  (an Asset Register valve), which days ("Mon Wed Fri", "daily", "every 3"), start time, minutes, season.
* RAISED AS WORK: just after midnight each scheduled set for the day becomes an Irrigation Farm Task on the
  zone's block, with the valve as its asset — so it is on the irrigator's phone, a Work Timing rule (the
  rain preset, OFF) can say "rain forecast — needed?", and opening and closing the valve from the task logs
  the runtime as it always has. Off until `irrigation_schedule_enabled`.
* PLANNED VS RAN: the valve's own log (`irrigation._runs_for`, the one runtime sum in this app) is set
  against the plan per day: done (90%+), short, missed, extra (ran with nothing planned), or skipped (the
  task was cancelled — with its reason).
"""

from __future__ import annotations

import datetime
import json

import frappe

from . import compat, settings

ZONE = "Irrigation Zone"
LINE = "Irrigation Schedule Line"
TASK_TYPE = "Irrigation"
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DONE_SHARE = 0.9


def installed() -> bool:
	return compat.has_field(ZONE, "schedule")


def enabled() -> bool:
	return bool(int(settings.get_settings().get("irrigation_schedule_enabled") or 0))


def _date(value) -> datetime.date | None:
	try:
		return datetime.date.fromisoformat(str(value or "")[:10])
	except ValueError:
		return None


def parse_days(raw: str) -> dict:
	"""'Mon Wed Fri' / 'daily' / 'every 3' → {weekdays: [...]} or {every: n}. Raises ValueError."""
	text = str(raw or "").strip().lower().replace(",", " ")
	if not text or text in ("daily", "every day", "every 1"):
		return {"weekdays": list(range(7))}
	if text.startswith("every"):
		try:
			n = int(text.split()[1])
		except (IndexError, ValueError):
			raise ValueError(f"days {raw!r}: write 'every 3' (every third day from the season start).") from None
		if not 1 <= n <= 30:
			raise ValueError("every N days: N is 1 to 30.")
		return {"every": n}
	days = []
	for word in text.split():
		key = word[:3]
		if key not in DAYS:
			raise ValueError(f"days {raw!r}: use Mon Tue Wed Thu Fri Sat Sun, 'daily' or 'every 3'.")
		days.append(DAYS.index(key))
	return {"weekdays": sorted(set(days))}


def occurs(line: dict, day: datetime.date) -> bool:
	if not int(line.get("active") if line.get("active") is not None else 1):
		return False
	start, end = _date(line.get("season_start")), _date(line.get("season_end"))
	if (start and day < start) or (end and day > end):
		return False
	rule = parse_days(line.get("days"))
	if "every" in rule:
		anchor = start or _date(line.get("creation")) or day
		return (day - anchor).days % rule["every"] == 0
	return day.weekday() in rule["weekdays"]


def lines(zone: str) -> list:
	doc = frappe.get_doc(ZONE, zone)
	return [dict(r.as_dict() if hasattr(r, "as_dict") else r) for r in doc.get("schedule") or []]


def planned(zone: str, start: datetime.date, end: datetime.date) -> list:
	out = []
	day = start
	rows = lines(zone)
	while day <= end:
		for row in rows:
			if occurs(row, day):
				out.append({"date": day.isoformat(), "line": row.get("name"), "valve": row.get("valve"),
				            "start_time": str(row.get("start_time") or "")[:5] or None, "minutes": int(row.get("minutes") or 0)})
		day += datetime.timedelta(days=1)
	return out


def set_schedule(zone: str, rows: list) -> list:
	"""Replace a zone's schedule. Each row: valve, days, start_time, minutes, season_start/end, active, note."""
	doc = frappe.get_doc(ZONE, zone)
	clean = []
	for index, row in enumerate(rows or []):
		where = f"schedule[{index}]"
		valve = str(row.get("valve") or "")
		if not valve or not frappe.db.exists("Asset Register", valve):
			raise ValueError(f"{where}.valve: no Asset Register {valve!r}.")
		parse_days(row.get("days"))
		minutes = int(row.get("minutes") or 0)
		if not 1 <= minutes <= 24 * 60:
			raise ValueError(f"{where}.minutes is 1 to 1440.")
		start = str(row.get("start_time") or "").strip()
		try:
			hour, minute = (int(x) for x in start.split(":")[:2])
			start = f"{hour:02d}:{minute:02d}:00"
			datetime.time(hour, minute)
		except ValueError:
			raise ValueError(f"{where}.start_time is HH:MM.") from None
		clean.append({"valve": valve, "days": str(row.get("days") or "daily"), "start_time": start, "minutes": minutes,
		              "season_start": row.get("season_start") or None, "season_end": row.get("season_end") or None,
		              "active": 0 if row.get("active") in (0, False, "0") else 1, "note": row.get("note") or None})
	doc.set("schedule", [])
	for row in clean:
		doc.append("schedule", row)
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	return lines(zone)


# ── raising the day's tasks ─────────────────────────────────────────────────
def _key(zone: str, line: str, day: str) -> str:
	return f"irrigation:{zone}:{line}:{day}"


def raise_for(day: datetime.date) -> list:
	"""Create the day's Irrigation tasks (idempotent). Returns the task names created."""
	if not installed() or not enabled():
		return []
	made = []
	for zone in frappe.db.get_all(ZONE, fields=["name", "zone_name", "field", "owning_entity"], limit=2000) or []:
		for row in lines(zone["name"]):
			if not occurs(row, day):
				continue
			key = _key(zone["name"], str(row.get("name")), day.isoformat())
			if frappe.db.exists("Farm Task", {"source_workorder": key}):
				continue
			at = str(row.get("start_time") or "")[:5]
			doc = frappe.get_doc(
				{
					"doctype": "Farm Task",
					"task_name": f"Irrigate {zone.get('zone_name') or zone['name']} — {row.get('valve')}, {at}, {row.get('minutes')} min",
					"task_type": TASK_TYPE,
					"origin": "compliance_rule",
					"state": "Available",
					"urgency": "Normal",
					"company": zone.get("owning_entity") or None,
					"location_doctype": "Field" if zone.get("field") else None,
					"location": zone.get("field") or None,
					"asset": row.get("valve"),
					"start_date": day.isoformat(),
					"due_date": day.isoformat(),
					"starts_after": f"at {at}",
					"estimated_duration_minutes": int(row.get("minutes") or 0),
					"source_workorder": key,
					"evidence_required": json.dumps({"hours": True}),
					"notes": f"Scheduled set: open {row.get('valve')} at {at} for {row.get('minutes')} minutes.",
				}
			).insert(ignore_permissions=True)
			made.append(doc.name)
	return made


def daily() -> None:
	"""The cron entry (00:15): today's sets. Never raises."""
	try:
		raise_for(datetime.date.fromisoformat(str(frappe.utils.today())[:10]))
	except Exception:
		frappe.log_error(title="irrigation schedule: tasks not raised", message=frappe.get_traceback())


# ── planned vs ran ──────────────────────────────────────────────────────────
def compare(zone: str, start: datetime.date, end: datetime.date) -> dict:
	from .tools import irrigation

	plan = planned(zone, start, end)
	valves = sorted({p["valve"] for p in plan if p["valve"]} | {r.get("valve") for r in lines(zone) if r.get("valve")})
	now = frappe.utils.now()
	days = []
	day = start
	while day <= end:
		opened, closed = f"{day.isoformat()} 00:00:00", f"{day.isoformat()} 23:59:59"
		for valve in valves:
			row = frappe.db.get_value("Asset Register", valve, ["name", "asset_type"], as_dict=True) or {"name": valve}
			ran = irrigation._runs_for(dict(row), opened, closed, now)
			minutes = float(ran["runtime_minutes"]) + float(ran.get("open_run_minutes") or 0)
			wanted = sum(p["minutes"] for p in plan if p["date"] == day.isoformat() and p["valve"] == valve)
			if not wanted and not minutes:
				continue
			cancelled = frappe.db.get_value(
				"Farm Task",
				{"source_workorder": ("like", f"irrigation:{zone}:%:{day.isoformat()}"), "asset": valve, "state": "Cancelled"},
				["name", "notes"], as_dict=True,
			)
			if not wanted:
				status = "extra"
			elif cancelled and minutes == 0:
				status = "skipped"
			elif minutes >= wanted * DONE_SHARE:
				status = "done"
			elif minutes > 0:
				status = "short"
			else:
				status = "missed"
			days.append({"date": day.isoformat(), "valve": valve, "planned_minutes": wanted, "ran_minutes": round(minutes, 1),
			             "status": status, "still_open": bool(ran.get("still_open")),
			             **({"skipped_task": cancelled.get("name")} if status == "skipped" and cancelled else {})})
		day += datetime.timedelta(days=1)
	totals = {s: sum(1 for d in days if d["status"] == s) for s in ("done", "short", "missed", "skipped", "extra")}
	return {"zone": zone, "from": start.isoformat(), "to": end.isoformat(), "plan": plan, "days": days,
	        "planned_minutes": sum(p["minutes"] for p in plan), "ran_minutes": round(sum(d["ran_minutes"] for d in days), 1),
	        "totals": totals}

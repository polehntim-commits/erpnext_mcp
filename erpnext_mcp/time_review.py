# SPDX-License-Identifier: MIT
"""Punch review as a compliance item. v0.251.0. Tim, AFB-2026-00032: "Shouldn't the punch times to
review be part of the compliance?" — yes.

A PUNCH is one person's row on a Farm Shift (Farm Shift Crew Member: in at `joined_at`, out at
`left_at`). Each is judged for six things that need a supervisor's eye:

* MISSING CLOCK-OUT on a closed shift, or a RUNAWAY — still clocked in long after the shift should
  have ended (`crew_view.STALE_AFTER_HOURS`);
* OFFLINE — a punch sent late, whose phone time and server time differ beyond the offline window
  (`punch_times`, v0.227.0), and nobody has resolved it yet;
* EDITED / OVERRIDDEN — the server time replaced the phone's, a time was corrected, or an
  administrative close set the clock-out;
* OUTSIDE THE BLOCK — a GPS fix during the shift beyond `punch_geofence_meters` of the shift's place;
* BREAKS SHORT — fewer rest or meal periods than the shift's break policy owes;
* and, per person and week, OREGON AG OVERTIME approaching or over the threshold (40 h, the
  payroll setting in force).

AS COMPLIANCE, THROUGH CCF — DATA, NOT CODE. Two providers publish the facts (`punch.*` on a Farm
Shift, `labor.*` on an Employee) and seven seeded Compliance Rules read them with condition trees.
Their alerts land in the Compliance inbox, its tile and the compliance status like any other, and an
operator retunes or switches one off as data.

THE SUPERVISOR REVIEWS each punch or a whole period: APPROVE, or FIX the time with a reason (or
approve a flagged punch with a reason). A reviewed punch is LOCKED — its times cannot change until a
manager reopens it, with a reason. Payroll preview warns on any punch in the period not yet reviewed
(pay itself is unchanged), and every review is evidence in the DOL audit packet.
"""

from __future__ import annotations

import datetime
import json

import frappe

from . import audit, compat, settings
from .errors import ToolError

SHIFT = "Farm Shift"
ROW = "Farm Shift Crew Member"
APPROVED, FIXED = "Approved", "Fixed"
REVIEWED = (APPROVED, FIXED)
APPROVE_ROLES = ("Foreman", "Farm Manager", "HR Manager", "HR User", "System Manager")
REOPEN_ROLES = ("Farm Manager", "HR Manager", "System Manager")
FLAGS = {
	"missing_clock_out": "No clock-out on a closed shift",
	"runaway": "Still clocked in long after the shift should have ended",
	"offline": "Sent late: phone and server times differ beyond the offline window",
	"edited": "Time edited or overridden",
	"outside_block": "GPS outside the shift's block",
	"breaks_short": "Fewer breaks than the break policy owes",
}
DEFAULT_GEOFENCE_M = 800
DEFAULT_WINDOW_DAYS = 31
POOR_ACCURACY_M = 50
OVERRIDDEN = ("Server time used", "Corrected")
STALE_MARK = "ADMINISTRATIVE CLOSE"


def installed() -> bool:
	return compat.has_field(ROW, "time_review_status")


def _setting(key: str, default):
	value = settings.get_settings().get(key)
	try:
		return type(default)(value) if value not in (None, "", 0, "0") else default
	except (TypeError, ValueError):
		return default


def _dt(value) -> datetime.datetime | None:
	if not value:
		return None
	try:
		return frappe.utils.get_datetime(value)
	except Exception:
		return None


def _now() -> datetime.datetime:
	return frappe.utils.get_datetime(frappe.utils.now())


# ── judging one punch ───────────────────────────────────────────────────────
def _shift_point(shift: dict) -> tuple | None:
	raw = str(shift.get("farm_location_gps") or "").replace(" ", "")
	if "," in raw:
		try:
			lat, lon = (float(x) for x in raw.split(",")[:2])
			return lat, lon
		except ValueError:
			pass
	location = shift.get("location")
	if location and compat.doctype_exists("Field") and frappe.db.exists("Field", location):
		row = frappe.db.get_value("Field", location, ["boundary_centroid_lat", "boundary_centroid_lon"], as_dict=True) or {}
		try:
			return float(row["boundary_centroid_lat"]), float(row["boundary_centroid_lon"])
		except (KeyError, TypeError, ValueError):
			return None
	return None


def _outside(shift: dict, row: dict) -> bool:
	if not compat.doctype_exists("Shift Location Log"):
		return False
	point = _shift_point(shift)
	if not point:
		return False
	from .asset_moves import distance_m

	limit = _setting("punch_geofence_meters", DEFAULT_GEOFENCE_M)
	fixes = frappe.db.get_all(
		"Shift Location Log",
		filters={"shift": shift["name"], "employee": row.get("employee")},
		fields=["latitude", "longitude", "accuracy_meters"],
		limit=500,
	) or []
	for fix in fixes:
		accuracy = fix.get("accuracy_meters")
		if accuracy not in (None, "") and float(accuracy) > POOR_ACCURACY_M:
			continue
		try:
			if distance_m(point[0], point[1], float(fix["latitude"]), float(fix["longitude"])) > limit:
				return True
		except (TypeError, ValueError):
			continue
	return False


def _short_on_breaks(shift: dict, crew: list) -> set:
	if not shift.get("break_policy"):
		return set()
	try:
		from . import shifts

		events = frappe.get_doc(SHIFT, shift["name"]).get("compliance_events") or []
		summary = shifts._break_summary(shift, [dict(r) for r in crew], [dict(e.as_dict() if hasattr(e, "as_dict") else e) for e in events])
		return {w.get("employee") for w in (summary or {}).get("workers_short") or []}
	except Exception:
		frappe.log_error(title="punch review: breaks not reconciled", message=frappe.get_traceback())
		return set()


def flags_for(shift: dict, row: dict, short: set | None = None) -> list:
	"""The flag keys one punch carries now."""
	from .tools import crew_view

	out = []
	ended = bool(shift.get("end_datetime")) or str(shift.get("status") or "") == "Closed"
	if not row.get("left_at"):
		joined = _dt(row.get("joined_at")) or _dt(shift.get("start_datetime"))
		if ended:
			out.append("missing_clock_out")
		elif joined and (_now() - joined).total_seconds() > crew_view.STALE_AFTER_HOURS * 3600:
			out.append("runaway")
	if int(row.get("punch_review") or 0) and not row.get("punch_resolution"):
		out.append("offline")
	if row.get("punch_resolution") in OVERRIDDEN or (
		STALE_MARK in str(shift.get("foreman_notes") or "") and row.get("left_at")
		and str(row.get("left_at"))[:16] == str(shift.get("end_datetime") or "")[:16]
	):
		out.append("edited")
	if _outside(shift, row):
		out.append("outside_block")
	if short and row.get("employee") in short:
		out.append("breaks_short")
	return out


def _shift_row(name: str) -> dict:
	fields = compat.existing_fields(SHIFT, ("name", "company", "status", "start_datetime", "end_datetime", "location",
	                                       "farm_location_gps", "break_policy", "foreman", "foreman_notes"))
	return dict(frappe.db.get_value(SHIFT, name, fields, as_dict=True) or {})


def _crew(name: str) -> list:
	return [dict(r.as_dict() if hasattr(r, "as_dict") else r) for r in frappe.get_doc(SHIFT, name).get("crew") or []]


def judge_shift(name: str) -> list:
	"""[(row, flags)] for every punch on one shift."""
	shift = _shift_row(name)
	if not shift:
		return []
	crew = _crew(name)
	short = _short_on_breaks(shift, crew)
	return [(row, flags_for(shift, row, short)) for row in crew]


# ── CCF providers ───────────────────────────────────────────────────────────
def _in_window(shift: dict) -> bool:
	start = _dt(shift.get("start_datetime"))
	return bool(start) and (_now() - start).days <= _setting("time_review_days", DEFAULT_WINDOW_DAYS)


def punch_values(subject: dict, ctx: dict) -> dict:
	"""Counts of UNREVIEWED punches on a Farm Shift carrying each flag."""
	if (not installed() or not subject.get("name") or not _in_window(subject)
	        or str(subject.get("status") or "") == "Cancelled"):
		return {key: 0 for key in FLAGS} | {"flagged": 0, "unreviewed": 0}
	counts = {key: 0 for key in FLAGS}
	flagged = unreviewed = 0
	for row, flags in judge_shift(subject["name"]):
		if row.get("time_review_status") in REVIEWED:
			continue
		unreviewed += 1
		flagged += bool(flags)
		for flag in flags:
			counts[flag] += 1
	return {**counts, "flagged": flagged, "unreviewed": unreviewed}


def threshold(as_of: str = "") -> float:
	from . import payroll_settings

	return float(payroll_settings.overtime(as_of).get("weekly_threshold_hours") or 40)


def labor_values(subject: dict, ctx: dict) -> dict:
	"""This workweek's hours for an Employee against the overtime threshold in force."""
	from . import shifts

	if not subject.get("name"):
		return {}
	day = str(ctx.get("as_of_date") or frappe.utils.today())[:10]
	worked = shifts.hours_worked_by(subject["name"], day)
	week = float(worked.get("week") or 0)
	limit = threshold(day)
	return {"week_hours": round(week, 2), "week_threshold": limit, "week_remaining": round(limit - week, 2)}


def register(ccf) -> None:
	p = ccf._p
	ccf.register(
		ccf.Provider(
			"punch",
			{
				**{key: p("number", f"Unreviewed punches on the shift: {label.lower()}.", example=0) for key, label in FLAGS.items()},
				"flagged": p("number", "Unreviewed punches with any flag.", example=0),
				"unreviewed": p("number", "Punches not yet reviewed by a supervisor.", example=3),
			},
			lambda subject, ctx: punch_values(subject, ctx),
			past=False,
			description="Punch review on a Farm Shift (v0.251.0): what its unreviewed punches need a supervisor to look at.",
		)
	)
	ccf.register(
		ccf.Provider(
			"labor",
			{
				"week_hours": p("number", "Hours on shifts this workweek (Monday start).", "h", 36),
				"week_threshold": p("number", "The weekly overtime threshold in force (payroll setting).", "h", 40),
				"week_remaining": p("number", "Hours left before overtime this week (negative = over).", "h", 4),
			},
			lambda subject, ctx: labor_values(subject, ctx),
			past=False,
			description="An Employee's hours this workweek against Oregon ag overtime (v0.251.0).",
		)
	)


# ── the rules (seeded ON: Tim asked for these, AFB-2026-00032) ──────────────
def _rule(rule_id, title, target, path, op, value, reason_en, reason_es, severity="Warning", tree=None):
	reason = {"en": reason_en, "es": reason_es}
	leaf = tree or {"id": rule_id, "path": path, "op": op, "value": value, "reason": reason}
	return {
		"rule_id": rule_id,
		"title": title,
		"category": "Workforce",
		"target_doctype": target,
		"condition_tree": leaf,
		"evaluation": {"when": ["sweep"]},
		"actions": [{"type": "alert"}],
		"default_severity": severity,
		"due_date_mode": "Today",
		"message_template": "{{ employee_name or name }}: {{ hold }}" if target == "Employee" else "{{ name }} ({{ start_datetime }}): {{ hold }}",
		"kairotic_gate_description": f"Punch review (AFB-2026-00032): raised while {reason_en.lower()}; clears when a supervisor reviews it.",
		"purpose": "Every punch is seen by a supervisor before payroll locks it.",
		"regimes": ["Internal"],
		"extra_parameters": {"notify_roles": list(APPROVE_ROLES)},
		"scope_filters": [{"field": "status", "op": "eq", "value": "Active"}] if target == "Employee" else [],
		"enabled": 1,
		"authored_by": "System",
	}


def rule_specs() -> list:
	return [
		_rule("punch_missing_clock_out", "Punch review: missing clock-out", SHIFT, "punch.missing_clock_out", "eq", 0,
		      "A punch has no clock-out", "Un registro no tiene salida"),
		_rule("punch_runaway", "Punch review: still clocked in", SHIFT, "punch.runaway", "eq", 0,
		      "Someone is still clocked in long after the shift", "Alguien sigue registrado mucho después del turno", "Critical"),
		_rule("punch_offline_window", "Punch review: sent late beyond the offline window", SHIFT, "punch.offline", "eq", 0,
		      "A punch was sent late beyond the offline window", "Un registro llegó tarde, fuera de la ventana sin conexión"),
		_rule("punch_edited", "Punch review: edited or overridden", SHIFT, "punch.edited", "eq", 0,
		      "A punch time was edited or overridden", "Se editó o reemplazó una hora de registro"),
		_rule("punch_outside_block", "Punch review: GPS outside the block", SHIFT, "punch.outside_block", "eq", 0,
		      "A GPS fix was outside the shift's block", "Una ubicación GPS quedó fuera del bloque del turno"),
		_rule("punch_breaks_short", "Punch review: breaks missing", SHIFT, "punch.breaks_short", "eq", 0,
		      "Someone had fewer breaks than the policy owes", "Alguien tuvo menos descansos de los que corresponden"),
		# Approaching = 4 hours or fewer left this week (the 4 is data: edit the rule to change it).
		_rule("overtime_week_approaching", "Oregon ag overtime approaching", "Employee", "", "", None,
		      "Within 4 hours of the weekly overtime threshold", "A 4 horas o menos del límite semanal de horas extra",
		      tree={"id": "overtime_week_approaching", "reason": {"en": "Within 4 hours of the weekly overtime threshold",
		                                                          "es": "A 4 horas o menos del límite semanal de horas extra"},
		            "any": [{"path": "labor.week_remaining", "op": "gt", "value": 4},
		                    {"path": "labor.week_remaining", "op": "lt", "value": 0}]}),
		_rule("overtime_week_exceeded", "Oregon ag overtime exceeded", "Employee", "labor.week_remaining", "gte", 0,
		      "Over the weekly overtime threshold", "Por encima del límite semanal de horas extra", "Critical"),
	]


def seed() -> list:
	"""Create-only; never raises."""
	from . import compliance_rules

	made = []
	if not compat.doctype_exists(compliance_rules.DOCTYPE):
		return made
	now = frappe.utils.now()
	for spec in rule_specs():
		try:
			if frappe.db.exists(compliance_rules.DOCTYPE, {"rule_id": spec["rule_id"]}):
				continue
			doc = compliance_rules.build_rule({**spec, "human_approved_by": "Administrator", "human_approved_on": now})
			doc.insert(ignore_permissions=True)
			made.append(spec["rule_id"])
		except Exception:
			frappe.log_error(title=f"punch review rule {spec['rule_id']} not seeded", message=frappe.get_traceback())
	return made


RULE_IDS = ("punch_missing_clock_out", "punch_runaway", "punch_offline_window", "punch_edited", "punch_outside_block",
            "punch_breaks_short", "overtime_week_approaching", "overtime_week_exceeded")


# ── listing and reviewing ───────────────────────────────────────────────────
def _shifts_between(companies, start: str, end: str, shift: str = "") -> list:
	if shift:
		return [shift] if frappe.db.exists(SHIFT, shift) else []
	filters = {"start_datetime": ("between", [f"{start} 00:00:00", f"{end} 23:59:59"])}
	if companies:
		filters["company"] = ("in", list(companies))
	return [r["name"] for r in frappe.db.get_all(SHIFT, filters=filters, fields=["name"], order_by="start_datetime asc",
	                                             limit=2000) or []
	        if str(frappe.db.get_value(SHIFT, r["name"], "status") or "") != "Cancelled"]


def describe_row(shift: dict, row: dict, flags: list) -> dict:
	return {
		"row": row.get("name"),
		"shift": shift.get("name"),
		"company": shift.get("company"),
		"date": str(shift.get("start_datetime") or "")[:10],
		"employee": row.get("employee"),
		"employee_name": row.get("employee_name"),
		"in": str(row.get("joined_at") or "") or None,
		"out": str(row.get("left_at") or "") or None,
		"flags": flags,
		"flag_text": [FLAGS[f] for f in flags],
		"status": row.get("time_review_status") or "Not reviewed",
		"reviewed_by": row.get("time_reviewed_by"),
		"reviewed_at": str(row.get("time_reviewed_at") or "") or None,
		"reason": row.get("time_review_reason"),
	}


def listing(companies, start: str, end: str, employee: str = "", shift: str = "", status: str = "pending") -> list:
	out = []
	for name in _shifts_between(companies, start, end, shift):
		meta = _shift_row(name)
		for row, flags in judge_shift(name):
			if employee and row.get("employee") != employee:
				continue
			reviewed = row.get("time_review_status") in REVIEWED
			if status == "pending" and reviewed:
				continue
			if status == "flagged" and (reviewed or not flags):
				continue
			if status == "reviewed" and not reviewed:
				continue
			out.append(describe_row(meta, row, flags))
	return out


def _require(user: str, roles, what: str) -> None:
	if user == "Administrator" or set(frappe.get_roles(user) or []) & set(roles):
		return
	raise ToolError(f"{what} is restricted to {', '.join(roles)}. Nothing was changed.")


def _set(row_name: str, values: dict) -> None:
	frappe.db.set_value(ROW, row_name, values)


def review(user: str, action: str, rows: list, reason: str = "", corrections: dict | None = None) -> dict:
	"""approve / fix / reopen punch rows (by row name). Returns what changed and what was refused."""
	reason = str(reason or "").strip()
	corrections = corrections or {}
	if action not in ("approve", "fix", "reopen"):
		raise ToolError("action is approve, fix or reopen. Nothing was changed.")
	_require(user, REOPEN_ROLES if action == "reopen" else APPROVE_ROLES,
	         "Reopening a reviewed punch" if action == "reopen" else "Reviewing punches")
	if action in ("fix", "reopen") and len(reason) < 5:
		raise ToolError(f"{action} needs a reason (a few words). Nothing was changed.")
	done, refused, companies = [], [], set()
	now = frappe.utils.now()
	by_shift: dict = {}
	for name in rows:
		parent = frappe.db.get_value(ROW, name, "parent")
		if not parent:
			refused.append({"row": name, "why": "no such punch"})
			continue
		by_shift.setdefault(parent, []).append(name)
	for shift_name, names in by_shift.items():
		meta = _shift_row(shift_name)
		companies.add(meta.get("company") or "")
		judged = {row["name"]: (row, flags) for row, flags in judge_shift(shift_name)}
		for name in names:
			row, flags = judged.get(name, ({}, []))
			status = row.get("time_review_status")
			if action == "reopen":
				if status not in REVIEWED:
					refused.append({"row": name, "why": "not reviewed"})
					continue
				_set(name, {"time_review_status": "", "time_reviewed_by": user, "time_reviewed_at": now,
				            "time_review_reason": f"Reopened: {reason}"})
			elif status in REVIEWED:
				refused.append({"row": name, "why": f"already {status} — a manager reopens it first"})
				continue
			elif action == "fix":
				change = corrections.get(name) or {}
				values = {k: v for k, v in (("joined_at", change.get("in")), ("left_at", change.get("out"))) if v}
				if not values:
					refused.append({"row": name, "why": "fix needs the corrected in and/or out time"})
					continue
				new_in = _dt(values.get("joined_at") or row.get("joined_at"))
				new_out = _dt(values.get("left_at") or row.get("left_at"))
				if new_in and new_out and new_out <= new_in:
					refused.append({"row": name, "why": "out must be after in"})
					continue
				_set(name, {**values, "time_review_status": FIXED, "time_reviewed_by": user, "time_reviewed_at": now,
				            "time_review_reason": reason, "time_review_flags": "\n".join(FLAGS[f] for f in flags)})
			else:
				if "missing_clock_out" in flags or "runaway" in flags:
					refused.append({"row": name, "why": "no clock-out — fix it with the time they left"})
					continue
				if flags and len(reason) < 5:
					refused.append({"row": name, "why": "flagged (" + ", ".join(FLAGS[f].lower() for f in flags) + ") — approve with a reason"})
					continue
				_set(name, {"time_review_status": APPROVED, "time_reviewed_by": user, "time_reviewed_at": now,
				            "time_review_reason": reason or None, "time_review_flags": "\n".join(FLAGS[f] for f in flags)})
			done.append({"row": name, "shift": shift_name, "employee": row.get("employee"), "flags": flags})
	if done:
		audit.record("review_punches", {"action": action, "rows": [d["row"] for d in done], "reason": reason})
		_rerun(companies)
	return {"action": action, "done": done, "refused": refused}


def _rerun(companies) -> None:
	"""Re-judge the punch rules now, so an approval clears its alert without waiting for the sweep."""
	try:
		from .alerts import base

		for company in companies or {""}:
			base.refresh_compliance_alerts(company=company or "", alert_types=list(RULE_IDS))
	except Exception:
		frappe.log_error(title="punch review: alerts not refreshed", message=frappe.get_traceback())


def period_rows(companies, start: str, end: str, employee: str = "") -> list:
	"""Every punch in a period with its review state — what an approve-the-period acts on."""
	return listing(companies, start, end, employee=employee, status="all")


# ── the lock ────────────────────────────────────────────────────────────────
LOCKED_FIELDS = ("employee", "joined_at", "left_at", "pay_type", "pay_rate")


def check_locked_rows(doc) -> None:
	"""Farm Shift validate: a reviewed punch's times cannot change until a manager reopens it."""
	if not installed() or doc.is_new():
		return
	saved = {r["name"]: r for r in frappe.db.get_all(ROW, filters={"parent": doc.name},
	                                                  fields=["name", "time_review_status", *LOCKED_FIELDS])}
	for row in doc.get("crew") or []:
		before = saved.get(row.get("name"))
		if not before or before.get("time_review_status") not in REVIEWED:
			continue
		for field in LOCKED_FIELDS:
			if str(row.get(field) or "")[:19] != str(before.get(field) or "")[:19]:
				frappe.throw(
					f"{row.get('employee_name') or row.get('employee')}'s punch on {doc.name} was reviewed "
					f"({before.get('time_review_status')}) and is locked for payroll. A manager reopens it first "
					"(review_punches action reopen, with a reason).",
					frappe.ValidationError,
				)
	removed = [n for n, r in saved.items() if r.get("time_review_status") in REVIEWED
	           and n not in {row.get("name") for row in doc.get("crew") or []}]
	if removed:
		frappe.throw("A reviewed punch cannot be removed from the shift; a manager reopens it first.", frappe.ValidationError)


def refuse_if_locked(row_name: str, what: str = "changed") -> None:
	if installed() and frappe.db.get_value(ROW, row_name, "time_review_status") in REVIEWED:
		raise ToolError(f"that punch was reviewed and is locked for payroll; it cannot be {what} until a manager "
		                "reopens it. Nothing was changed.")


# ── payroll and the audit packet ────────────────────────────────────────────
def payroll_warning(company: str, start: str, end: str) -> dict:
	"""Punches in a pay period not yet reviewed (pay is unchanged; this is a warning)."""
	if not installed():
		return {}
	rows = listing([company] if company else None, str(start)[:10], str(end)[:10], status="pending")
	if not rows:
		return {}
	flagged = [r for r in rows if r["flags"]]
	return {
		"punches_not_reviewed": len(rows),
		"flagged": len(flagged),
		"rows": rows[:50],
		"note": f"{len(rows)} punch(es) in this period are not reviewed ({len(flagged)} flagged). Review them "
		        "(review_punches) before payroll so the times are locked.",
	}


def audit_rows(company: str, start: str, end: str) -> list:
	return listing([company] if company else None, str(start)[:10], str(end)[:10], status="all")

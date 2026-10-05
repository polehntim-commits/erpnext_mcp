# SPDX-License-Identifier: MIT
"""Suggested tasks: work the conditions favour today. v0.242.0. Decision 14 (approved; queue item 5).

Managers still create the work (decision 14); this module only READS it back in the order the weather and
the crop stage say to do it. A suggestion is an existing open Farm Task that a Work Timing rule speaks to
and that is Go today, ranked:

1. WINDOW CLOSING — Go today, Hold tomorrow: do it now or wait for the next window.
2. JUST CLEARED — was on Hold and the latest check cleared it: the work that was waiting.
3. VERIFY STAGE — Go once somebody confirms the stage in the field.
4. GO — the rest, soonest due first.

Each suggestion says why, in English and Spanish. Nothing is written: the verdict for today is the one
the 06:00 check recorded; tomorrow's is judged here, live, and not stored.
"""

from __future__ import annotations

import datetime
import json

import frappe

from . import compat, go_hold

CLOSING, CLEARED, VERIFY, GO = "window_closing", "just_cleared", "verify_stage", "go"
RANK = {CLOSING: 0, CLEARED: 1, VERIFY: 2, GO: 3}
WHY = {
	CLOSING: ("Go today, on Hold tomorrow — do it today.", "Se puede hoy, en espera mañana — hazlo hoy."),
	CLEARED: ("Was on Hold; conditions cleared.", "Estaba en espera; las condiciones mejoraron."),
	VERIFY: ("Go — verify the crop stage in the field first.", "Adelante — verifica primero la etapa en el campo."),
	GO: ("Go today.", "Se puede hoy."),
}
FIELDS = ("name", "task_name", "task_type", "state", "company", "location_doctype", "location", "assigned_to",
          "assigned_to_name", "due_date", "urgency", "go_hold", "go_hold_reasons", "go_hold_checked_at", "go_hold_log",
          "template")


def _tomorrow() -> str:
	return (datetime.date.fromisoformat(str(frappe.utils.today())[:10]) + datetime.timedelta(days=1)).isoformat()


def _cleared_recently(row: dict) -> bool:
	try:
		log = json.loads(row.get("go_hold_log") or "[]")
	except ValueError:
		return False
	today = str(frappe.utils.today())[:10]
	return bool(log) and bool(log[0].get("cleared")) and str(log[0].get("at") or "")[:10] == today


def suggestions(company: str = "", worker: str = "", location: str = "", limit: int = 50) -> list:
	if not go_hold.installed():
		return []
	filters = {"state": ("in", ["Available", "Claimed", "Paused"]), "go_hold": ("in", [go_hold.GO, go_hold.VERIFY])}
	if company:
		filters["company"] = company
	if location:
		filters["location"] = location
	rows = frappe.db.get_all("Farm Task", filters=filters, fields=compat.existing_fields("Farm Task", FIELDS), limit=2000)
	out = []
	tomorrow = _tomorrow()
	for row in rows or []:
		row = dict(row)
		if worker and row.get("assigned_to") not in (worker, None, ""):
			continue
		if row.get("go_hold") == go_hold.VERIFY:
			kind = VERIFY
		elif _cleared_recently(row):
			kind = CLEARED
		else:
			kind = GO
		next_day = ""
		try:
			ahead = go_hold.evaluate(row, as_of=tomorrow)
			next_day = ahead["status"]
			if kind != VERIFY and ahead["status"] == go_hold.HOLD:
				kind = CLOSING
		except Exception:
			frappe.log_error(title="suggested task: tomorrow not judged", message=frappe.get_traceback())
		en, es = WHY[kind]
		out.append(
			{
				"task": row["name"],
				"task_name": row.get("task_name"),
				"task_type": row.get("task_type"),
				"location": row.get("location"),
				"assigned_to": row.get("assigned_to") or None,
				"assigned_to_name": row.get("assigned_to_name") or None,
				"due_date": str(row.get("due_date") or "") or None,
				"urgency": row.get("urgency"),
				"kind": kind,
				"why": {"en": en, "es": es},
				"today": row.get("go_hold"),
				"tomorrow": next_day or None,
			}
		)
	out.sort(key=lambda s: (RANK[s["kind"]], s["due_date"] or "9999-12-31", str(s["task"])))
	return out[: max(1, min(int(limit or 50), 200))]

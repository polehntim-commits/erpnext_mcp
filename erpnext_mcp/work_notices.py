# SPDX-License-Identifier: MIT
"""No-work notices. v0.243.0. docs/design/ccf_core_work_timing.md §8 (approved; queue item 5).

WHEN AN ENFORCED WORK TIMING RULE HOLDS TOMORROW'S WORK, the crew should hear it the evening before —
from their supervisor, not from a machine.

* DRAFT (cutoff minus the supervisor lead, default 17:00): every open, assigned task under an Enforced
  rule (one with `block_start`) is judged for TOMORROW. A Hold drafts one Work Notice per task and day:
  the reasons, the rules and versions, the first forecast day it would be Go ("next possible"), the
  recipients (the assignee and a crew task's members) each in their own language, and the text. The
  supervisor (decision 29: the assignee's Reports To, else whoever dispatched the task) is pushed.
  Advisory rules never draft anything.
* DECIDE: the supervisor sends it, sends it with other work ("brush pickup, Block 9, 7 am" — see
  `list_suggested_tasks`), or decides no notice is needed.
* CUTOFF (default 18:00): a notice nobody answered is ESCALATED to management (decision 34) — pushed to
  the users named in settings, else every Farm Manager. IT IS NEVER SENT AUTOMATICALLY.
* LIFTED: if a sent notice's day turns Go before it comes, the supervisor is asked to send a resume notice.
* EVIDENCE: who was told what, when, in which language, and whether a push reached a handset. Anyone not
  reached is listed to call.
"""

from __future__ import annotations

import datetime
import json

import frappe

from . import compat, go_hold, settings

DOCTYPE = "Work Notice"
AWAITING, ESCALATED, SENT, OTHER, NONE, LIFTED, RESUMED = (
	"Awaiting Supervisor", "Escalated", "Sent", "Other Work Sent", "No Notice", "Lifted", "Resume Sent",
)
OPEN = (AWAITING, ESCALATED, LIFTED)
CHOICES = ("send", "other_work", "no_notice", "resume")
MANAGER_ROLES = ("Farm Manager",)
DEFAULT_CUTOFF = "18:00"
DEFAULT_LEAD = 60
LOOKAHEAD_DAYS = 7


# ── settings ────────────────────────────────────────────────────────────────
def installed() -> bool:
	return compat.doctype_exists(DOCTYPE)


def enabled() -> bool:
	return installed() and bool(int(settings.get_settings().get("work_notices_enabled") or 0))


def cutoff() -> datetime.time:
	raw = str(settings.get_settings().get("work_notice_cutoff") or DEFAULT_CUTOFF)
	try:
		hour, minute = (int(x) for x in raw.split(":")[:2])
		return datetime.time(hour, minute)
	except ValueError:
		return datetime.time(18, 0)


def lead_minutes() -> int:
	value = settings.get_settings().get("work_notice_supervisor_lead_minutes")
	try:
		return max(0, int(value)) if value not in (None, "") else DEFAULT_LEAD
	except (TypeError, ValueError):
		return DEFAULT_LEAD


def _now() -> datetime.datetime:
	return frappe.utils.get_datetime(frappe.utils.now())


def _today() -> datetime.date:
	return datetime.date.fromisoformat(str(frappe.utils.today())[:10])


# ── people ──────────────────────────────────────────────────────────────────
def _employee(name: str) -> dict:
	if not name or not frappe.db.exists("Employee", name):
		return {}
	fields = compat.existing_fields("Employee", ("name", "employee_name", "user_id", "reports_to", "preferred_language"))
	return dict(frappe.db.get_value("Employee", name, fields, as_dict=True) or {})


def _employee_of_user(user: str) -> str:
	return str(frappe.db.get_value("Employee", {"user_id": user}, "name") or "") if user else ""


def supervisor_for(task: dict) -> dict:
	"""Decision 29: the assignee's Reports To, else whoever dispatched (created) the task."""
	worker = _employee(str(task.get("assigned_to") or ""))
	boss = _employee(str(worker.get("reports_to") or ""))
	if boss:
		return {"employee": boss["name"], "user": boss.get("user_id") or "", "how": "reports to"}
	owner = str(task.get("owner") or "")
	if owner and owner not in ("Administrator", "Guest"):
		return {"employee": _employee_of_user(owner), "user": owner, "how": "dispatched the task"}
	return {"employee": "", "user": "", "how": "none — management decides"}


def managers() -> list:
	raw = str(settings.get_settings().get("work_notice_managers") or "")
	named = [p.strip() for p in raw.replace(";", ",").replace("\n", ",").split(",") if p.strip()]
	if named:
		return named
	users = set()
	if compat.doctype_exists("Has Role"):
		for role in MANAGER_ROLES:
			users |= set(frappe.db.get_all("Has Role", filters={"role": role, "parenttype": "User"}, pluck="parent"))
	return sorted(u for u in users if u not in ("Administrator", "Guest"))


def recipients_for(task: dict) -> list:
	names = []
	if task.get("assigned_to"):
		names.append(str(task["assigned_to"]))
	try:
		from . import crew_tasks

		crew = crew_tasks.describe(task) or {}
		for member in crew.get("members") or []:
			if member.get("active", True) and member.get("employee"):
				names.append(str(member["employee"]))
	except Exception:
		frappe.log_error(title="work notice: crew not read", message=frappe.get_traceback())
	out, seen = [], set()
	for name in names:
		if name in seen:
			continue
		seen.add(name)
		emp = _employee(name)
		lang = "es" if str(emp.get("preferred_language") or "").lower().startswith("es") else "en"
		out.append({"employee": name, "employee_name": emp.get("employee_name") or name, "language": lang})
	return out


# ── text ────────────────────────────────────────────────────────────────────
def texts(task: dict, for_date: str, reasons_en: list, reasons_es: list, next_possible: str,
          alternative: str = "", resume: bool = False) -> dict:
	what = task.get("task_name") or task.get("name")
	where = task.get("location") or ""
	at_en = f" at {where}" if where else ""
	at_es = f" en {where}" if where else ""
	if resume:
		return {
			"en": f"{what}{at_en} is ON again for {for_date}. Conditions cleared.",
			"es": f"{what}{at_es} vuelve a estar en pie para el {for_date}. Las condiciones mejoraron.",
		}
	nxt_en = f"Next possible: {next_possible}." if next_possible else "We'll confirm the evening before."
	nxt_es = f"Próximo día posible: {next_possible}." if next_possible else "Te confirmamos la tarde anterior."
	en = f"No {what}{at_en} on {for_date}: {'; '.join(reasons_en)}. {nxt_en}"
	es = f"No hay {what}{at_es} el {for_date}: {'; '.join(reasons_es)}. {nxt_es}"
	if alternative:
		en += f" Instead: {alternative}."
		es += f" En su lugar: {alternative}."
	return {"en": en, "es": es}


def _next_possible(task: dict, start: datetime.date) -> str:
	for offset in range(1, LOOKAHEAD_DAYS):
		day = (start + datetime.timedelta(days=offset)).isoformat()
		try:
			if go_hold.evaluate(task, as_of=day)["status"] in (go_hold.GO, go_hold.VERIFY):
				return day
		except Exception:
			return ""
	return ""


# ── the evening run ─────────────────────────────────────────────────────────
TASK_FIELDS = ("name", "task_name", "task_type", "state", "company", "location_doctype", "location", "assigned_to",
               "assigned_to_name", "start_date", "template", "is_crew_task", "work_mode", "owner")


def _existing(task: str, for_date: str) -> dict:
	rows = frappe.db.get_all(DOCTYPE, filters={"task": task, "for_date": for_date}, fields=["name", "status"], limit=1)
	return dict(rows[0]) if rows else {}


def draft(for_date: str = "") -> list:
	"""Draft tomorrow's notices; flag sent ones whose day turned Go. Returns the notices touched."""
	if not enabled() or not go_hold.installed():
		return []
	day = for_date or (_today() + datetime.timedelta(days=1)).isoformat()
	answer_by = f"{_today().isoformat()} {cutoff().strftime('%H:%M')}:00"
	touched = []
	rows = frappe.db.get_all("Farm Task", filters={"state": ("in", list(go_hold.OPEN_STATES))},
	                         fields=compat.existing_fields("Farm Task", TASK_FIELDS), limit=2000)
	for row in rows or []:
		task = dict(row)
		if str(task.get("start_date") or "") > day:
			continue
		try:
			rules = go_hold.rules_for(task)
			if not any(go_hold.enforced(r) for r in rules):
				continue
			verdict = go_hold.evaluate(task, as_of=day)
			es = go_hold.evaluate(task, "es", as_of=day)
		except Exception:
			frappe.log_error(title="work notice: task not judged", message=frappe.get_traceback())
			continue
		existing = _existing(task["name"], day)
		if verdict["status"] != go_hold.HOLD or not verdict["enforced_hold"]:
			if existing.get("status") in (SENT, OTHER):
				frappe.db.set_value(DOCTYPE, existing["name"], "status", LIFTED)
				_tell_supervisor(existing["name"], lifted=True)
				touched.append(existing["name"])
			continue
		if existing:
			continue
		who = recipients_for(task)
		if not who:
			continue
		boss = supervisor_for(task)
		nxt = _next_possible(task, datetime.date.fromisoformat(day))
		text = texts(task, day, verdict["reasons"], es["reasons"], nxt)
		doc = frappe.new_doc(DOCTYPE)
		doc.update(
			{
				"company": task.get("company") or None,
				"for_date": day,
				"status": AWAITING,
				"task": task["name"],
				"task_name": task.get("task_name"),
				"location": task.get("location"),
				"supervisor": boss["employee"] or None,
				"supervisor_user": boss["user"] or None,
				"answer_by": answer_by,
				"hold_reasons": "\n".join(verdict["reasons"]),
				"rules": json.dumps([{"rule_id": r["rule_id"], "version": r["version"], "verdict": r["verdict"]}
				                     for r in verdict["rules"]]),
				"next_possible": nxt or None,
				"text_en": text["en"],
				"text_es": text["es"],
			}
		)
		for person in who:
			doc.append("recipients", {**person, "text": text[person["language"]]})
		doc.insert(ignore_permissions=True)
		_tell_supervisor(doc.name)
		touched.append(doc.name)
	return touched


def escalate(for_date: str = "") -> list:
	"""At the cutoff: every notice still awaiting the supervisor goes to management. Never sent."""
	if not enabled():
		return []
	day = for_date or (_today() + datetime.timedelta(days=1)).isoformat()
	people = managers()
	out = []
	for row in frappe.db.get_all(DOCTYPE, filters={"for_date": day, "status": AWAITING}, fields=["name", "task_name"]) or []:
		frappe.db.set_value(DOCTYPE, row["name"], {"status": ESCALATED, "escalated_to": "\n".join(people),
		                                           "escalated_at": frappe.utils.now()})
		_push([_employee_of_user(u) for u in people],
		      f"Unanswered no-work notice: {row.get('task_name')} on {day}",
		      "The supervisor did not answer by the cutoff. Send it, offer other work, or decide no notice.",
		      row["name"])
		out.append(row["name"])
	return out


def hourly() -> None:
	"""The scheduler entry: draft at cutoff minus the lead, escalate at the cutoff. Never raises."""
	try:
		if not enabled():
			return
		now = _now()
		cut = datetime.datetime.combine(now.date(), cutoff())
		if now >= cut - datetime.timedelta(minutes=lead_minutes()):
			draft()
		if now >= cut:
			escalate()
	except Exception:
		frappe.log_error(title="work notices: evening run failed", message=frappe.get_traceback())


# ── the answer ──────────────────────────────────────────────────────────────
def may_answer(doc, user: str) -> bool:
	if user == "Administrator" or set(frappe.get_roles(user) or []) & {"System Manager", "Farm Manager"}:
		return True
	return bool(doc.get("supervisor_user")) and doc.get("supervisor_user") == user


def answer(name: str, choice: str, user: str, alternative: str = "", alternative_task: str = "") -> dict:
	if choice not in CHOICES:
		raise ValueError(f"choice is one of: {', '.join(CHOICES)}.")
	doc = frappe.get_doc(DOCTYPE, name)
	if not may_answer(doc, user):
		raise PermissionError(f"{user} is not this notice's supervisor or a manager.")
	if str(doc.for_date) < _today().isoformat():
		raise ValueError(f"{name} was for {doc.for_date}, which has passed.")
	if choice == "resume" and doc.status != LIFTED:
		raise ValueError(f"{name} is {doc.status}; a resume notice follows a lifted Hold.")
	if choice != "resume" and doc.status not in (AWAITING, ESCALATED):
		raise ValueError(f"{name} is already {doc.status}.")
	if choice == "other_work" and not str(alternative or "").strip() and not alternative_task:
		raise ValueError("other_work needs the work, e.g. 'brush pickup, Block 9, 7 am', or alternative_task.")
	now = frappe.utils.now()
	doc.choice = choice
	doc.answered_by = user
	doc.answered_at = now
	if choice == "no_notice":
		doc.status = NONE
		doc.save(ignore_permissions=True)
		return describe(doc.name)
	task = dict(frappe.get_doc("Farm Task", doc.task).as_dict()) if doc.task and frappe.db.exists("Farm Task", doc.task) else {
		"name": doc.task, "task_name": doc.task_name, "location": doc.location}
	alt = str(alternative or "").strip()
	if alternative_task and frappe.db.exists("Farm Task", alternative_task):
		other = frappe.db.get_value("Farm Task", alternative_task, ["task_name", "location"], as_dict=True) or {}
		alt = alt or f"{other.get('task_name') or alternative_task}" + (f", {other.get('location')}" if other.get("location") else "")
		doc.alternative_task = alternative_task
	doc.alternative = alt or None
	if choice == "resume":
		text = texts(task, str(doc.for_date), [], [], "", resume=True)
		doc.status = RESUMED
	else:
		reasons_en = [r for r in str(doc.hold_reasons or "").splitlines() if r]
		es = go_hold.evaluate(task, "es", as_of=str(doc.for_date)) if doc.task else {"reasons": []}
		text = texts(task, str(doc.for_date), reasons_en, es.get("reasons") or reasons_en,
		             str(doc.next_possible or ""), alt)
		doc.status = OTHER if choice == "other_work" else SENT
	doc.text_en, doc.text_es = text["en"], text["es"]
	not_reached = []
	for row in doc.get("recipients") or []:
		lang = row.get("language") or "en"
		body = text["es"] if lang == "es" else text["en"]
		report = _push([row.get("employee")], "Work notice" if lang == "en" else "Aviso de trabajo", body, doc.name)
		reached = bool((report or {}).get("sent"))
		for field, value in (("text", body), ("sent", 1), ("sent_at", now), ("reached", int(reached))):
			if hasattr(row, "set") and not isinstance(row, dict):
				row.set(field, value)
			else:
				row[field] = value
		if not reached:
			not_reached.append(row.get("employee_name") or row.get("employee"))
	doc.save(ignore_permissions=True)
	out = describe(doc.name)
	out["not_reached"] = not_reached
	if not_reached:
		out["call"] = "Not reached by push — call: " + ", ".join(not_reached)
	return out


def _tell_supervisor(name: str, lifted: bool = False) -> None:
	doc = frappe.get_doc(DOCTYPE, name)
	if lifted:
		title, body = f"{doc.task_name} is Go again for {doc.for_date}", "Send a resume notice to the crew?"
	else:
		title = f"No-work notice to decide: {doc.task_name} on {doc.for_date}"
		body = f"{(doc.hold_reasons or '').splitlines()[0] if doc.hold_reasons else 'On Hold'}. Answer by {str(doc.answer_by)[11:16]}."
	if doc.supervisor:
		_push([doc.supervisor], title, body, name)


def _push(employees, title: str, body: str, notice: str) -> dict:
	try:
		from .services import push

		return push.send_push_to_employees(
			[e for e in employees if e],
			{"aps": {"alert": {"title": title, "body": body}}, "kind": "work_notice", "work_notice": notice},
		)
	except Exception:
		frappe.log_error(title="work notice push failed", message=frappe.get_traceback())
		return {}


# ── reads ───────────────────────────────────────────────────────────────────
def describe(name: str) -> dict:
	doc = frappe.get_doc(DOCTYPE, name)
	row = doc.as_dict()
	return {
		**{k: (str(row.get(k)) if row.get(k) is not None else None) for k in (
			"name", "company", "for_date", "status", "task", "task_name", "location", "supervisor", "supervisor_user",
			"answer_by", "escalated_at", "next_possible", "choice", "alternative", "alternative_task", "answered_by",
			"answered_at", "text_en", "text_es")},
		"hold_reasons": [r for r in str(row.get("hold_reasons") or "").splitlines() if r],
		"rules": json.loads(row.get("rules") or "[]"),
		"escalated_to": [u for u in str(row.get("escalated_to") or "").splitlines() if u],
		"recipients": [
			{k: r.get(k) for k in ("employee", "employee_name", "language", "sent", "sent_at", "reached", "opened_at")}
			for r in (doc.get("recipients") or [])
		],
	}


def listing(status: str = "", for_date: str = "", company: str = "", limit: int = 100) -> list:
	filters = {}
	if status == "open":
		filters["status"] = ("in", list(OPEN))
	elif status:
		filters["status"] = status
	if for_date:
		filters["for_date"] = for_date
	if company:
		filters["company"] = company
	rows = frappe.db.get_all(DOCTYPE, filters=filters, fields=["name"], order_by="for_date desc", limit=limit) or []
	return [describe(r["name"]) for r in rows]

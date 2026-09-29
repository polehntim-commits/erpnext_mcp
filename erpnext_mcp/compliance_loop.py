# SPDX-License-Identifier: MIT
"""Closing the compliance loop. v0.207.0.

docs/design/phone_config_and_compliance_loop.md §4. A rule that raises work is
only useful when that work reaches a phone that can render it, in the hands of
somebody who can see it, and when finishing it clears the alert. This module
answers those three questions for a rule (`audit`), shows them end to end
without writing anything (`preview`), and builds each person's Compliance inbox
(`inbox`).
"""

from __future__ import annotations

import json

import frappe

from . import compat, phone_config

RULE = "Compliance Rule"
ALERT = "Compliance Alert"
OPEN_TASK_STATES = ("Available", "Claimed", "In-Progress", "Paused", "Awaiting Review")
BLOCK_REASONS = {
	"certification": ("needs {what}, which you don't hold", "requiere {what}, que usted no tiene"),
	"paused": ("the task is paused", "la tarea está en pausa"),
	"approval": ("waiting for an approval step", "esperando una aprobación"),
	"gap": (
		"the farm hasn't set up a way to do this on the phone",
		"la granja no ha preparado cómo hacerlo en el teléfono",
	),
	"device": (
		"this phone can't show a required step — update the app",
		"este teléfono no puede mostrar un paso requerido — actualice la app",
	),
}


# ── the rule's path to a phone ──────────────────────────────────────────────
def rule_of(reference) -> dict:
	from . import compliance_rules

	name = compliance_rules.resolve(str(reference or ""))
	return compliance_rules.rule_row(name) if name else {}


def _extra(rule: dict) -> dict:
	from . import compliance_rules

	try:
		return compliance_rules.as_object(rule.get("extra_parameters_json"), "extra_parameters")
	except Exception:
		return {}


def path_of(rule: dict) -> dict:
	"""{kind, ref, version, source} — the first thing the rule names, in §4.1's order."""
	from .tools import dispatch

	template = str(rule.get("producer_task_template") or "").strip()
	if template:
		version = (
			frappe.db.get_value("Farm Task Template", template, "version")
			if frappe.db.exists("Farm Task Template", template)
			else None
		)
		return {
			"kind": "task_template",
			"ref": template,
			"version": version,
			"source": "producer_task_template",
		}
	inspection = str(rule.get("producer_inspection_template") or "").strip()
	if inspection:
		from . import sessions

		name = sessions.resolve_template(inspection) or ""
		version = frappe.db.get_value(sessions.TEMPLATE_DOCTYPE, name, "version") if name else None
		return {
			"kind": "inspection",
			"ref": name or inspection,
			"version": version,
			"source": "producer_inspection_template",
		}
	wizard = str(rule.get("producer_wizard") or "").strip()
	if wizard:
		doc = phone_config.doc_of("Wizard", wizard, status=phone_config.PUBLISHED)
		return {
			"kind": "wizard",
			"ref": wizard,
			"version": phone_config.version_string(doc) if doc else None,
			"source": "producer_wizard",
		}
	rule_id = str(rule.get("rule_id") or "")
	if rule_id in dispatch.ALERT_TASK_MAP:
		return {"kind": "task", "ref": rule_id, "version": None, "source": "ALERT_TASK_MAP"}
	if str(rule.get("producer_farm_task_type") or "").strip():
		return {"kind": "task", "ref": rule_id, "version": None, "source": "inline producer fields"}
	return {"kind": "none", "ref": "", "version": None, "source": ""}


def fields_of(path: dict) -> list:
	"""The form the phone renders for this path (empty for an evidence-only task)."""
	from . import form_schema

	kind, ref = path["kind"], path["ref"]
	try:
		if kind == "task_template" and frappe.db.exists("Farm Task Template", ref):
			return form_schema.as_fields(frappe.db.get_value("Farm Task Template", ref, "form_schema"))
		if kind == "inspection":
			from . import sessions

			out = []
			for section in sessions.sections_of(ref):
				out += sessions.section_fields(section)
			return out
		if kind == "wizard":
			doc = phone_config.doc_of("Wizard", ref, status=phone_config.PUBLISHED)
			body = phone_config.body_of(doc) if doc else {}
			return [field for step in body.get("steps") or [] for field in step.get("form") or []]
	except form_schema.SchemaError:
		return []
	return []


def _requirements(path: dict) -> dict:
	"""{skill, certification} the path's work demands."""
	kind, ref = path["kind"], path["ref"]
	if kind == "task_template" and frappe.db.exists("Farm Task Template", ref):
		row = (
			frappe.db.get_value(
				"Farm Task Template", ref, ["skill_required", "required_certification"], as_dict=True
			)
			or {}
		)
		return {
			"skill": row.get("skill_required") or "",
			"certification": row.get("required_certification") or "",
		}
	if kind == "inspection" and ref:
		from . import sessions

		row = sessions.template_row(ref)
		return {
			"skill": row.get("skill_required") or "",
			"certification": row.get("required_certification") or "",
		}
	if kind == "task":
		from .tools import dispatch

		recipe = dispatch.ALERT_TASK_MAP.get(ref) or {}
		return {"skill": recipe.get("skill_required") or "", "certification": ""}
	return {"skill": "", "certification": ""}


def audience_of(rule: dict, path: dict, company: str = "") -> dict:
	need = _requirements(path)
	notify = [str(r).strip() for r in _extra(rule).get("notify_roles") or [] if str(r).strip()]
	doers = phone_config.audience_people(
		{
			"skills": [need["skill"]] if need["skill"] else [],
			"certifications": [need["certification"]] if need["certification"] else [],
		},
		company,
	)
	if path["kind"] == "wizard":
		doc = phone_config.doc_of("Wizard", path["ref"], status=phone_config.PUBLISHED)
		roles = (phone_config.body_of(doc) if doc else {}).get("required_roles") or []
		if roles:
			doers = [p for p in doers if set(roles) & set(p["roles"])]
	notified = phone_config.audience_people({"roles": notify}, company) if notify else []
	people = {p["user"]: p for p in doers + notified}
	return {
		"skill": need["skill"] or None,
		"certification": need["certification"] or None,
		"notify_roles": notify,
		"people": len(people),
		"sample": sorted(people)[:10],
		"_people": list(people.values()),
	}


def _inbox_tile_reaches(person: dict) -> bool:
	doc, body = phone_config.in_force("Tile", "compliance_inbox", person["user"], "", person["roles"])
	return bool(doc) and phone_config.matches(person, (body or {}).get("audience"))


def entry_points(path: dict, people: list) -> list:
	out = []
	if path["kind"] in ("task_template", "task"):
		out.append("Work: My tasks / Available")
	if any(_inbox_tile_reaches(p) for p in people):
		out.append("tile compliance_inbox")
	target_kind = {
		"inspection": "inspection_template",
		"wizard": "wizard",
		"task_template": "task_template",
	}.get(path["kind"])
	for doc, body in phone_config.served("Tile"):
		target = body.get("target") or {}
		if (
			target.get("kind") == target_kind
			and str(target.get("template") or target.get("wizard") or "") == path["ref"]
		):
			if any(phone_config.matches(p, body.get("audience")) for p in people):
				out.append(f"tile {doc.config_key}")
	return out


def dismissal(rule: dict, path: dict) -> dict:
	if path["kind"] == "none":
		return {"mode": "manual", "how": "no work is raised, so only a person dismissing it clears the alert"}
	return {
		"mode": "auto",
		"how": (
			"completion re-runs the rule for the produced record (_evaluate_compliance_after); "
			"when the condition no longer holds the alert is auto-dismissed"
		),
	}


def report(rule: dict, company: str = "") -> dict:
	"""One rule's loop, with every gap named."""
	from . import device_capabilities, form_schema

	path = path_of(rule)
	gaps: list = []
	fields = fields_of(path)
	render_errors = []
	if path["kind"] == "none":
		gaps.append(
			"the rule names no work (producer_task_template, producer_inspection_template or producer_wizard)"
		)
	elif path["kind"] == "task_template":
		if not frappe.db.exists("Farm Task Template", path["ref"]):
			gaps.append(f"producer_task_template {path['ref']!r} does not exist")
		elif not compat.checked(frappe.db.get_value("Farm Task Template", path["ref"], "enabled")):
			gaps.append(f"Farm Task Template {path['ref']!r} is disabled")
	elif path["kind"] == "inspection" and not path.get("version"):
		gaps.append(f"no live Inspection Template {path['ref']!r}")
	elif path["kind"] == "wizard" and not path.get("version"):
		gaps.append(f"no Published wizard {path['ref']!r}")
	if fields:
		render_errors = [f"{f['path']}: {f['message']}" for f in form_schema.validate(fields)["errors"]]
		gaps += [f"the form does not render: {e}" for e in render_errors]
	who = audience_of(rule, path, company)
	device = device_capabilities.problems(fields, company) if fields else []
	blocking = [line for line in device if line.startswith("BLOCKING")]
	gaps += blocking
	# STRUCTURAL gaps (above) gate enabling the rule. REACH gaps (below) are
	# reported but do not gate: a farm with no enrolled phone yet must still be
	# able to switch rules on and dispatch from the Desk.
	blocking = list(gaps)
	if path["kind"] != "none" and not who["people"]:
		gaps.append("nobody with a phone can act on it (check the skill, certification and notify_roles)")
	entries = entry_points(path, who["_people"]) if who["people"] else []
	if path["kind"] in ("inspection", "wizard") and who["people"] and not entries:
		gaps.append("no tile or inbox reaches the people who can do it")
	who.pop("_people", None)
	return {
		"rule": rule.get("name"),
		"rule_id": rule.get("rule_id"),
		"title": rule.get("title"),
		"enabled": bool(compat.checked(rule.get("enabled"))),
		"path": path,
		"renderable": not render_errors and not blocking and path["kind"] != "none",
		"device_problems": device,
		"audience": who,
		"entry_points": entries,
		"dismissal": dismissal(rule, path),
		"loop_gap_accepted": rule.get("loop_gap_accepted") or None,
		"gaps": gaps,
		"blocking_gaps": blocking,
		"ok": not gaps,
	}


def audit(company: str = "", rule=None) -> dict:
	from . import compliance_rules

	if rule:
		row = rule_of(rule)
		if not row:
			raise ValueError(f"no Compliance Rule {rule!r}")
		rows = [row]
	else:
		rows = [r for r in compliance_rules.rule_rows() if compat.checked(r.get("enabled"))]
	reports = [report(row, company) for row in rows]
	return {
		"rules": reports,
		"count": len(reports),
		"ok": sum(1 for r in reports if r["ok"]),
		"with_gaps": sum(1 for r in reports if not r["ok"]),
		"company": company or None,
	}


def enable_gaps(rule: dict) -> list:
	"""What stops a rule from being enabled (§4.1): the STRUCTURAL gaps only."""
	return report(rule)["blocking_gaps"]


# ── end to end, writing nothing ─────────────────────────────────────────────
def preview(rule_reference, alert=None, as_user: str = "", language: str = "en") -> dict:
	from . import form_schema, task_templates

	rule = rule_of(rule_reference)
	if not rule:
		raise ValueError(f"no Compliance Rule {rule_reference!r}")
	row = report(rule)
	sample = None
	if alert and frappe.db.exists(ALERT, alert):
		sample = frappe.db.get_value(
			ALERT, alert, ["name", "alert_message", "due_date", "company", "subject_employee"], as_dict=True
		)
	if not sample:
		found = frappe.db.get_all(
			ALERT,
			filters={"alert_type": rule.get("rule_id"), "dismissed": 0},
			fields=["name", "alert_message", "due_date", "company", "subject_employee"],
			limit=1,
		)
		sample = (
			dict(found[0])
			if found
			else {
				"name": "(synthetic)",
				"alert_message": rule.get("message_template") or rule.get("title"),
				"due_date": None,
				"company": None,
			}
		)
	path = row["path"]
	fields = fields_of(path)
	snapshot = {}
	if path["kind"] == "task_template" and frappe.db.exists("Farm Task Template", path["ref"]):
		try:
			snapshot = {
				k: v
				for k, v in (task_templates.snapshot(path["ref"]) or {}).items()
				if k
				in (
					"task_type",
					"evidence_required",
					"creates_record",
					"required_certification",
					"skill_required",
				)
			}
		except Exception:
			snapshot = {}
	phone = {lang: form_schema.resolve_language(form_schema.for_phone(fields), lang) for lang in ("en", "es")}
	viewer = None
	if as_user:
		person = phone_config.person_of(as_user)
		items = inbox(as_user)
		viewer = {
			"user": as_user,
			"in_inbox": any(
				i["alert"] == sample.get("name")
				for part in ("due", "overdue", "blocked")
				for i in items[part]
			),
			"blocked_reason": next(
				(i.get("blocked_reason") for i in items["blocked"] if i["alert"] == sample.get("name")), None
			),
			"roles": person.get("roles"),
		}
	produces = snapshot.get("creates_record") or (
		"Inspection Session"
		if path["kind"] == "inspection"
		else (_wizard_produces(path["ref"]) if path["kind"] == "wizard" else None)
	)
	return {
		"rule": rule.get("name"),
		"stages": [
			{"stage": "alert", "alert": sample},
			{"stage": "work", "path": path, "snapshot": snapshot},
			{"stage": "phone", "form": phone, "device_problems": row["device_problems"]},
			{
				"stage": "who",
				"audience": row["audience"],
				"entry_points": row["entry_points"],
				"viewer": viewer,
			},
			{"stage": "completion", "produces": produces},
			{"stage": "dismissal", **row["dismissal"]},
		],
		"gaps": row["gaps"],
		"ok": row["ok"],
		"wrote_nothing": True,
	}


def _wizard_produces(key: str):
	from . import wizard_config

	doc = phone_config.doc_of("Wizard", key, status=phone_config.PUBLISHED)
	handler = ((phone_config.body_of(doc) if doc else {}).get("submit") or {}).get("handler")
	return (wizard_config.HANDLERS.get(handler) or {}).get("produces")


# ── the Compliance inbox (§4.3) ─────────────────────────────────────────────
def _due_days() -> int:
	from . import flags

	try:
		return int(flags.value("compliance_inbox_due_days", default=14) or 14)
	except (TypeError, ValueError):
		return 14


def _blocked(person: dict, task: dict | None, row: dict) -> str:
	from . import qualifications

	if task:
		cert = str(task.get("required_certification") or "").strip()
		if cert and not (person.get("employee") and qualifications.qualification(person["employee"], cert)):
			return "certification:" + cert
		if task.get("state") == "Paused":
			return "paused"
		approvals = task.get("approvals")
		if approvals and "pending" in str(approvals).lower():
			return "approval"
	if row.get("blocking_gaps"):
		return "gap"
	return ""


def _reason(code: str) -> dict:
	kind, _, what = code.partition(":")
	en, es = BLOCK_REASONS.get(kind, (code, code))
	return {"en": en.format(what=what), "es": es.format(what=what)}


def inbox(user: str, company: str = "") -> dict:

	person = phone_config.person_of(user)
	companies = [company] if company else (person.get("companies") or [])
	out = {"due": [], "overdue": [], "blocked": [], "counts": {}, "evaluated_at": frappe.utils.now()}
	if not companies or not compat.doctype_exists(ALERT):
		out["counts"] = {"due": 0, "overdue": 0, "blocked": 0, "total": 0}
		return out
	today = str(frappe.utils.today())
	horizon = str(frappe.utils.add_days(today, _due_days()))
	alerts = frappe.db.get_all(
		ALERT,
		filters={"dismissed": 0, "company": ("in", companies)},
		fields=[
			"name",
			"alert_type",
			"alert_message",
			"due_date",
			"company",
			"subject_employee",
			"snoozed_until",
			"severity",
			"regime",
		],
		limit=2000,
	)
	tasks = {}
	for task in frappe.db.get_all(
		"Farm Task",
		filters={
			"source_alert": ("in", [a["name"] for a in alerts] or [""]),
			"state": ("in", OPEN_TASK_STATES),
		},
		fields=[
			"name",
			"source_alert",
			"state",
			"assigned_to",
			"skill_required",
			"required_certification",
			"approvals",
			"template",
			"inspection_session",
		],
		limit=5000,
	):
		tasks[task["source_alert"]] = dict(task)
	rules: dict = {}
	for alert in alerts:
		snoozed = str(alert.get("snoozed_until") or "")[:10]
		if snoozed and snoozed > today:
			continue
		alert_type = alert.get("alert_type") or ""
		if alert_type not in rules:
			rule = rule_of(alert_type)
			rules[alert_type] = (
				rule,
				report(rule, alert["company"]) if rule else {"gaps": [], "path": {"kind": "none", "ref": ""}},
			)
		rule, loop = rules[alert_type]
		task = tasks.get(alert["name"])
		notify = set(_extra(rule).get("notify_roles") or []) if rule else set()
		mine = (
			(person.get("employee") and alert.get("subject_employee") == person["employee"])
			or (task and person.get("employee") and task.get("assigned_to") == person["employee"])
			or (
				task
				and task.get("state") == "Available"
				and (not task.get("skill_required") or task["skill_required"] in person.get("skills", []))
			)
			or bool(notify & set(person.get("roles") or []))
		)
		if not mine:
			continue
		code = _blocked(person, task, loop)
		action = _action(alert, task, loop.get("path") or {"kind": "none", "ref": ""})
		item = {
			"alert": alert["name"],
			"title": alert.get("alert_message"),
			"due_date": str(alert.get("due_date") or "") or None,
			"severity": alert.get("severity"),
			"regulation": alert.get("regime"),
			"subject": alert.get("subject_employee"),
			"company": alert.get("company"),
			"action": action,
		}
		due = str(alert.get("due_date") or "")[:10]
		if code:
			item["state"] = "blocked"
			item["blocked_reason"] = _reason(code)
			out["blocked"].append(item)
		elif due and due < today:
			item["state"] = "overdue"
			out["overdue"].append(item)
		elif not due or due <= horizon:
			item["state"] = "due"
			out["due"].append(item)
	for part in ("due", "overdue", "blocked"):
		out[part].sort(key=lambda i: (i["due_date"] or "9999", i["alert"]))
	out["counts"] = {
		"due": len(out["due"]),
		"overdue": len(out["overdue"]),
		"blocked": len(out["blocked"]),
		"total": len(out["due"]) + len(out["overdue"]) + len(out["blocked"]),
	}
	return out


def _action(alert: dict, task: dict | None, path: dict) -> dict | None:
	context = {"source_alert": alert["name"]}
	if task:
		if task.get("inspection_session"):
			return {
				"kind": "inspection",
				"ref": task["inspection_session"],
				"context": context,
				"task": task["name"],
			}
		return {"kind": "task", "ref": task["name"], "context": context}
	kind = path.get("kind")
	if kind == "inspection":
		return {"kind": "inspection", "ref": path["ref"], "context": context}
	if kind == "wizard":
		return {"kind": "wizard", "ref": path["ref"], "context": context}
	if kind == "task_template":
		return {"kind": "task_template", "ref": path["ref"], "context": context}
	return None


def after_wizard(source_alert: str) -> None:
	"""A wizard filed against an alert re-runs that alert's rule (§4.4). Never raises."""
	try:
		row = frappe.db.get_value(ALERT, source_alert, ["alert_type", "company"], as_dict=True)
		if not row:
			return
		from .alerts import base as alerts_base

		alerts_base.refresh_compliance_alerts(row["company"], alert_types=[row["alert_type"]])
	except Exception:
		frappe.log_error(title="compliance loop: wizard re-scan", message=frappe.get_traceback())


def json_safe(value):
	return json.loads(json.dumps(value, default=str))

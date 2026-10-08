# SPDX-License-Identifier: MIT
"""Seasonal work lists as data — Fall winterize and Spring start-up first. v0.270.0
(docs/contracts/seasonal_v0_270.yaml; Tim, 2026-10-07: "needed this fall").

A SEASONAL PROGRAM is a Farm Config Version (kind "Seasonal Program") a person publishes. It says WHEN (a fixed
date Tim edits, and/or the first forecast hard freeze inside its season window) and WHAT: per asset type, which
checklist template; per company, the farm-level items (frost protection, chemical storage). When it fires, one task
per active asset of each type is raised from that template — photo evidence, completion per asset — and anything
still open as the freeze nears becomes a compliance alert naming the asset.

NOTHING RUNS UNTIL TIM SAYS SO: the programs ship as Drafts and their checklist templates ship DISABLED. A template
he has not enabled raises nothing, even inside a published program.
"""

from __future__ import annotations

import datetime

import frappe

from . import compat

KIND = "Seasonal Program"
TEMPLATE = "Farm Task Template"
TASK = "Farm Task"
ASSET = "Asset Register"
ALERT = "Compliance Alert"
ORIGIN = "compliance_rule"
OPEN_STATES = ("Draft", "Available", "Claimed", "In-Progress", "Paused", "Awaiting-Review")
KEY_PREFIX = "seasonal"
FALL, SPRING = "fall_winterize", "spring_startup"


# ── the checklists (DISABLED drafts; Tim edits and enables) ──────────────────
def _t(name, es, types, minutes, items, instructions="", es_instructions="", scope="asset", skill=""):
	return {
		"template_name": name,
		"title_es": es,
		"task_type": "Maintenance",
		"description": instructions or name,
		"instructions": instructions,
		"instructions_es": es_instructions,
		"estimated_duration_minutes": minutes,
		"dispatch_mode": "Either",
		"default_urgency": "Normal",
		"evidence_required": {"photos": True},
		"applies_to_asset_types": list(types),
		"checklist": [{"item_name": item, "required": True} for item in items]
		+ [{"item_name": "Photo of the finished job", "required": True, "evidence_type": "Photo"}],
		"skill_required": skill,
		"enabled": 0,
		"_scope": scope,
	}


WINTERIZE = (
	_t("Winterize — Sprayer", "Preparar para el invierno — Aspersora", ("Sprayer",), 90, (
		"Drain the tank, pump, lines and filters",
		"Run RV antifreeze through the pump and lines",
		"Clean the nozzles and screens",
		"Store the nozzles dry",
	), "Freeze damage to a pump or manifold is the costliest winter repair on the farm.",
		"Una bomba o un múltiple congelado es la reparación de invierno más cara de la finca."),
	_t("Winterize — Tractor / machine / vehicle", "Preparar para el invierno — Tractor / máquina / vehículo",
	   ("Tractor", "Vehicle"), 60, (
		"Coolant freeze test (protection to at least -30 °F)",
		"Battery check; tender connected if it sits",
		"Fuel stabilizer added and run through",
		"Grease every fitting",
		"Tires checked and inflated",
		"Block heater checked",
	), "Includes the mini excavator if it is registered as a Tractor or Vehicle — add its type here if not.",
		"Incluye la mini excavadora si está registrada como Tractor o Vehículo; agrega su tipo aquí si no."),
	_t("Winterize — Mower / implement", "Preparar para el invierno — Desbrozadora / implemento", ("Implement",), 45, (
		"Clean it down",
		"Blades checked / sharpened or noted for the shop queue",
		"Grease every fitting",
		"Stored under cover",
	)),
	_t("Winterize — Irrigation", "Preparar para el invierno — Riego", ("Irrigation Zone", "Water Source"), 120, (
		"Blow out or drain the mainlines and laterals",
		"Drain the pumps",
		"Valves left open; mark each valve Winterized on the phone",
		"Filters drained and cleaned",
		"Backflow devices drained / protected",
	), "One task per zone and per water source, not per valve; the valve state is set with the valve's own Winterize action.",
		"Una tarea por zona y por fuente de agua, no por válvula; marca cada válvula con su acción Invernar.",
		skill="irrigation"),
	_t("Winterize — Wind machine (off-season check)", "Preparar para el invierno — Máquina de viento (revisión fuera de temporada)",
	   ("Wind Machine",), 60, (
		"Engine / motor off-season service noted or done",
		"Gearbox oil level checked",
		"Fuel or power safely shut off",
		"Blades and tower visually checked",
	)),
	_t("Winterize — Shop / pump house", "Preparar para el invierno — Taller / caseta de bomba", ("Storage",), 45, (
		"Heat tape checked and plugged in",
		"Insulation in place",
		"Water lines drained or protected",
	)),
	_t("Frost protection readiness", "Preparación para protección contra heladas", (), 60, (
		"Wind machines start and run",
		"Frost alarms / thermometers checked",
		"Fuel on hand for frost nights",
		"Call list current",
	), scope="company"),
	_t("Chemical storage — freeze-sensitive products", "Almacén de químicos — productos sensibles a congelación", (), 45, (
		"Freeze-sensitive products identified from the labels",
		"Moved to heated storage",
		"Inventory and SDS binder checked",
	), "Check each label's storage temperature; most flowables and emulsifiable concentrates must not freeze.",
		"Revisa la temperatura de almacenamiento en cada etiqueta; la mayoría de los fluidos y concentrados emulsionables no deben congelarse.",
		scope="company"),
)

STARTUP = (
	_t("Spring start-up — Sprayer", "Arranque de primavera — Aspersora", ("Sprayer",), 90, (
		"Flush the antifreeze out", "Check pump, lines and filters for cracks", "Nozzles back in; calibrate",
	)),
	_t("Spring start-up — Irrigation", "Arranque de primavera — Riego", ("Irrigation Zone", "Water Source"), 120, (
		"Close drains; prime the pumps", "Pressure test the mainlines and laterals",
		"Un-winterize the valves on the phone", "Flush and check the filters and backflow devices",
	), skill="irrigation"),
	_t("Spring start-up — Tractor / machine / vehicle", "Arranque de primavera — Tractor / máquina / vehículo",
	   ("Tractor", "Vehicle"), 45, ("Battery off the tender and tested", "Fluids and tires checked", "Grease every fitting")),
)

SEED_TEMPLATES = WINTERIZE + STARTUP


def _program(key, title, es, mmdd, freeze, window, templates, alert_days):
	return {
		"title": {"en": title, "es": es},
		"trigger": {"fixed_mmdd": mmdd, "freeze_tmin_f": freeze, "freeze_lookahead_days": 10,
		            "window_from_mmdd": window[0], "window_to_mmdd": window[1]},
		"alert_lead_days": alert_days,
		"items": [{"template": t["template_name"], "asset_types": list(t["applies_to_asset_types"]), "scope": t["_scope"]}
		          for t in templates],
		"companies": [],
	}


SEED_PROGRAMS = {
	FALL: _program(FALL, "Fall winterize", "Preparación para el invierno", "10-25", 28, ("09-01", "12-31"),
	               WINTERIZE, 3),
	SPRING: _program(SPRING, "Spring start-up", "Arranque de primavera", "03-01", None, ("02-01", "05-31"),
	                 STARTUP, 3),
}


# ── validation (phone_config kind) ───────────────────────────────────────────
def _mmdd(value) -> bool:
	try:
		datetime.date.fromisoformat(f"2024-{value}")
		return True
	except (TypeError, ValueError):
		return False


def validate(body: dict, *, key: str = "", for_publish: bool = False) -> dict:
	errors, warnings = [], []
	trigger = body.get("trigger") or {}
	if not trigger.get("fixed_mmdd") and trigger.get("freeze_tmin_f") in (None, ""):
		errors.append("trigger: a fixed_mmdd date, a freeze_tmin_f, or both")
	for name in ("fixed_mmdd", "window_from_mmdd", "window_to_mmdd"):
		if trigger.get(name) and not _mmdd(trigger[name]):
			errors.append(f"trigger.{name}: MM-DD, e.g. 10-25")
	items = body.get("items") or []
	if not items:
		errors.append("items: at least one {template, asset_types | scope: company}")
	for item in items:
		if not item.get("template"):
			errors.append("items: every item names a template")
		elif for_publish and compat.doctype_exists(TEMPLATE) and not frappe.db.exists(TEMPLATE, {"template_name": item["template"]}):
			errors.append(f"items: no Farm Task Template {item['template']!r}")
		elif compat.doctype_exists(TEMPLATE) and not frappe.db.get_value(TEMPLATE, {"template_name": item["template"]}, "enabled"):
			warnings.append(f"{item['template']} is disabled — it raises nothing until it is enabled")
		if item.get("scope", "asset") == "asset" and not item.get("asset_types"):
			errors.append(f"items: {item.get('template')} needs asset_types (or scope: company)")
	return {"errors": errors, "warnings": warnings}


# ── when ─────────────────────────────────────────────────────────────────────
def _today() -> datetime.date:
	return datetime.date.fromisoformat(str(frappe.utils.today())[:10])


def _on(year: int, mmdd: str) -> datetime.date:
	return datetime.date.fromisoformat(f"{year}-{mmdd}")


def in_window(body: dict, day: datetime.date) -> bool:
	trigger = body.get("trigger") or {}
	start, end = trigger.get("window_from_mmdd"), trigger.get("window_to_mmdd")
	if not start or not end:
		return True
	return _on(day.year, start) <= day <= _on(day.year, end)


def company_point(company: str):
	"""A point to ask the forecast about: the first block of this company with an outline."""
	if not compat.doctype_exists("Field"):
		return None
	cols = compat.existing_fields("Field", ("boundary_centroid_lat", "boundary_centroid_lon"))
	if len(cols) < 2:
		return None
	rows = frappe.db.get_all("Field", filters={"owning_entity": company, "boundary_centroid_lat": ("is", "set")},
	                         fields=cols, limit=1) or []
	try:
		return float(rows[0]["boundary_centroid_lat"]), float(rows[0]["boundary_centroid_lon"])
	except (IndexError, KeyError, TypeError, ValueError):
		return None


def forecast_freeze(company: str, threshold_f, lookahead_days: int, forecast=None) -> dict | None:
	"""{date, tmin_f} of the first forecast day at or below the threshold, or None. `forecast` for tests."""
	if threshold_f in (None, ""):
		return None
	if forecast is None:
		from . import ccf_providers

		point = company_point(company)
		if not point or not ccf_providers.forecast_enabled():
			return None
		forecast = ccf_providers.fetch_forecast(*point) or {}
	today = _today()
	last = today + datetime.timedelta(days=int(lookahead_days or 10))
	for day in ((forecast.get("forecast") or {}).get("daily") or []):
		try:
			when = datetime.date.fromisoformat(str(day.get("date"))[:10])
		except ValueError:
			continue
		if today <= when <= last and day.get("tmin_f") is not None and float(day["tmin_f"]) <= float(threshold_f):
			return {"date": str(when), "tmin_f": float(day["tmin_f"])}
	return None


def triggered(body: dict, company: str, forecast=None) -> dict:
	"""Whether the program has fired for this company today, why, and the deadline (freeze day or fixed date)."""
	today = _today()
	trigger = body.get("trigger") or {}
	out = {"fired": False, "why": None, "deadline": None, "freeze": None}
	if not in_window(body, today):
		return out
	fixed = _on(today.year, trigger["fixed_mmdd"]) if trigger.get("fixed_mmdd") else None
	freeze = forecast_freeze(company, trigger.get("freeze_tmin_f"), trigger.get("freeze_lookahead_days") or 10, forecast)
	out["freeze"] = freeze
	candidates = [d for d in (fixed, datetime.date.fromisoformat(freeze["date"]) if freeze else None) if d]
	out["deadline"] = str(min(candidates)) if candidates else None
	if fixed and today >= fixed:
		out.update(fired=True, why=f"the fixed date {trigger['fixed_mmdd']}")
	elif freeze:
		out.update(fired=True, why=f"a forecast low of {freeze['tmin_f']:g} °F on {freeze['date']}")
	return out


# ── what ─────────────────────────────────────────────────────────────────────
def published() -> list:
	from . import phone_config

	out = []
	for key in sorted(SEED_PROGRAMS) + sorted({r["config_key"] for r in phone_config.rows(KIND)} - set(SEED_PROGRAMS)):
		doc = phone_config.doc_of(KIND, key, status=phone_config.PUBLISHED)
		if doc is not None:
			out.append((key, phone_config.body_of(doc)))
	return out


def _companies(body: dict) -> list:
	wanted = [c for c in body.get("companies") or [] if c]
	return wanted or (frappe.db.get_all("Company", pluck="name", limit=100) or [])


def _template_enabled(name: str) -> bool:
	return bool(compat.doctype_exists(TEMPLATE) and frappe.db.get_value(TEMPLATE, {"template_name": name}, "enabled"))


def targets(body: dict, company: str) -> list:
	"""[(template, asset | None)] this program would raise for this company — active assets of each type, and one
	farm-level task per company item."""
	out = []
	for item in body.get("items") or []:
		if item.get("scope") == "company":
			out.append((item["template"], None))
			continue
		if not compat.doctype_exists(ASSET):
			continue
		filters = {"asset_type": ("in", list(item.get("asset_types") or [])), "company": company}
		if compat.has_field(ASSET, "retired_at"):
			filters["retired_at"] = ("is", "not set")
		for asset in frappe.db.get_all(ASSET, filters=filters, pluck="name", order_by="name asc", limit=2000) or []:
			out.append((item["template"], asset))
	return out


def task_key(program: str, year: int, company: str, template: str, asset) -> str:
	return f"{KEY_PREFIX}:{program}:{year}:{asset or company}:{template}"[:140]


def _existing(key: str) -> str:
	return str(frappe.db.get_value(TASK, {"source_workorder": key}, "name") or "")


def raise_tasks(program: str, body: dict, company: str, deadline: str | None, why: str) -> dict:
	"""One task per target, once per season; a template Tim has not enabled raises nothing."""
	from .tools import tasktemplates

	year = _today().year
	report = {"created": [], "present": [], "skipped_disabled": []}
	for template, asset in targets(body, company):
		key = task_key(program, year, company, template, asset)
		if _existing(key):
			report["present"].append(key)
			continue
		if not _template_enabled(template):
			if template not in report["skipped_disabled"]:
				report["skipped_disabled"].append(template)
			continue
		args = {"template": template, "company": company,
		        "notes": f"{(body.get('title') or {}).get('en') or program}: due before {deadline or 'the freeze'} ({why})."}
		if deadline:
			args["due_date"] = deadline
		if asset:
			args["task_name"] = f"{template} — {asset}"
		result = tasktemplates.create_task_from_template(args, origin=ORIGIN, fields={"asset": asset, "source_workorder": key}
		                                                 if asset else {"source_workorder": key})
		report["created"].append((result.data or {}).get("name"))
	return report


def open_tasks(program: str, year: int | None = None, company: str = "") -> list:
	year = year or _today().year
	filters = {"source_workorder": ("like", f"{KEY_PREFIX}:{program}:{year}:%")}
	if company:
		filters["company"] = company
	cols = compat.existing_fields(TASK, ("name", "task_name", "state", "asset", "company", "due_date", "source_workorder"))
	return [dict(r) for r in frappe.db.get_all(TASK, filters=filters, fields=cols, limit=5000) or []]


def status(program: str = FALL, year: int | None = None, company: str = "") -> dict:
	"""Done / open per asset for one season — what get_winterize_status answers."""
	rows = open_tasks(program, year, company)
	done = [r for r in rows if r.get("state") == "Completed"]
	pending = [r for r in rows if r.get("state") in OPEN_STATES]
	return {
		"program": program,
		"year": year or _today().year,
		"total": len(rows),
		"done": len(done),
		"open": len(pending),
		"open_tasks": sorted(({"task": r["name"], "name": r.get("task_name"), "asset": r.get("asset"), "state": r.get("state"),
		                       "due_date": str(r.get("due_date") or "") or None, "company": r.get("company")}
		                      for r in pending), key=lambda r: (r["due_date"] or "", r["name"] or "")),
		"done_tasks": sorted((r["name"] for r in done)),
	}


# ── the overdue alert ────────────────────────────────────────────────────────
def _alert(program: str, body: dict, company: str, task: dict, deadline: str) -> str:
	from .alerts import base as alerts

	key = alerts.alert_key(f"seasonal_{program}", TASK, task["name"])
	message = (f"{task.get('task_name') or task['name']} is not done and the deadline is {deadline} "
	           f"({(body.get('title') or {}).get('en') or program}).")
	if frappe.db.exists(ALERT, key):
		doc = frappe.get_doc(ALERT, key)
		doc.alert_message = message
		doc.last_refreshed = frappe.utils.now()
		doc.save(ignore_permissions=True)
		return doc.name
	doc = frappe.new_doc(ALERT)
	doc.alert_key = key
	doc.alert_type = f"seasonal_{program}"
	doc.severity = "Warning"
	doc.category = "Other"
	doc.company = company or None
	doc.source_doctype = TASK
	doc.source_docname = task["name"]
	doc.alert_message = message
	doc.due_date = deadline
	doc.first_seen = frappe.utils.today()
	doc.last_refreshed = frappe.utils.now()
	doc.insert(ignore_permissions=True)
	return doc.name


def alert_overdue(program: str, body: dict, company: str, deadline: str | None) -> list:
	"""Open tasks once the deadline is within `alert_lead_days` (or past) become alerts naming the asset."""
	if not deadline or not compat.doctype_exists(ALERT):
		return []
	days_left = (datetime.date.fromisoformat(deadline) - _today()).days
	if days_left > int(body.get("alert_lead_days") or 3):
		return []
	return [_alert(program, body, company, t, deadline) for t in open_tasks(program, company=company)
	        if t.get("state") in OPEN_STATES]


# ── the daily run ────────────────────────────────────────────────────────────
def run(forecast_for=None) -> dict:
	"""Every published program, every company: raise when fired, alert when late. `forecast_for(company)` for tests."""
	out = {}
	for program, body in published():
		for company in _companies(body):
			fc = forecast_for(company) if forecast_for else None
			state = triggered(body, company, fc)
			if not state["fired"] and not state["freeze"]:
				continue
			entry = {"why": state["why"], "deadline": state["deadline"]}
			if state["fired"]:
				entry["raised"] = raise_tasks(program, body, company, state["deadline"], state["why"])
			entry["alerts"] = alert_overdue(program, body, company, state["deadline"])
			out[f"{program}:{company}"] = entry
	return out


def daily() -> None:
	"""The scheduled job. NEVER RAISES."""
	try:
		run()
	except Exception:  # pragma: no cover
		frappe.log_error(title="erpnext_mcp seasonal programs")


def seed() -> dict:
	"""The two programs as DRAFTS (create-only). Templates are seeded by task_templates (disabled)."""
	from . import phone_config

	made = []
	for key, body in SEED_PROGRAMS.items():
		if phone_config.rows(KIND, key):
			continue
		phone_config.save_draft(KIND, key, body, "Seeded draft — edit the dates and checklists, enable the templates, "
		                        "then publish.", "System")
		made.append(key)
	return {"drafts": made}

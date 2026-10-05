# SPDX-License-Identifier: MIT
"""Daily checks are Farm Tasks; equipment in use is checked by its operator. v0.237.0.

docs/design/ccf_core_work_timing.md §7 and Tim's decision 33 (2026-10-04): "Daily checks are
Farm Tasks. When equipment is used, the operator checks it per its SOP." Approved queue item 3.

* A Farm Task Template marked `daily_check` = "Start of Day" (or "End of Day") with NO asset
  types is the BASE check; one whose `applies_to_asset_types` names a type is an ADD-ON for that
  type (e.g. Mini Excavator: tracks and rollers, bucket pins, hydraulic hoses, swing lock). A
  machine's check is the base items followed by its type's add-ons — composition is data.
* When a worker STARTS a task on an asset (`Farm Task.asset`), and has no check for that asset
  today, one is raised and dispatched to them: "Daily check — <asset>". ADVISORY (decision 39):
  the work is not held up; the start answer names the check.
* The check is completed like any task — offline, idempotent, photos durable — because it is one.
* Off until `daily_checks_enabled` is ticked.
"""

from __future__ import annotations

import json

import frappe

from . import compat

TEMPLATE = "Farm Task Template"
START, END = "Start of Day", "End of Day"
KINDS = (START, END)


def enabled() -> bool:
	try:
		from . import settings

		return bool(compat.checked(settings.get_settings().get("daily_checks_enabled")))
	except Exception:
		return False


def _templates(kind: str) -> list:
	if not compat.has_field(TEMPLATE, "daily_check"):
		return []
	return frappe.db.get_all(
		TEMPLATE,
		filters={"daily_check": kind, "enabled": 1},
		fields=compat.existing_fields(TEMPLATE, ("name", "template_name", "applies_to_asset_types")),
		order_by="name asc",
	) or []


def _types(row: dict) -> list:
	return [t.strip() for t in str(row.get("applies_to_asset_types") or "").replace(",", "\n").splitlines() if t.strip()]


def templates_for(kind: str, asset_type: str = "") -> tuple:
	"""(base template or None, [add-on templates for this asset type])."""
	rows = _templates(kind)
	base = next((r for r in rows if not _types(r)), None)
	addons = [r for r in rows if asset_type and asset_type in _types(r)]
	return base, addons


def compose(kind: str, asset_type: str = "") -> dict:
	"""The check for one machine: base items, then its type's add-ons; repeated items once."""
	from . import task_templates

	base, addons = templates_for(kind, asset_type)
	items, seen, sources = [], set(), []
	for row in ([base] if base else []) + addons:
		sources.append(row["name"])
		for item in task_templates.checklist_of(row["name"]):
			key = str(item.get("item_name") or "").strip().lower()
			if not key or key in seen:
				continue
			seen.add(key)
			items.append(
				{
					"item_name": item["item_name"],
					"required": bool(item.get("required")),
					"evidence_type": str(item.get("evidence_type") or "None"),
					"sort_order": len(items) + 1,
					"done": False,
					"note": "",
				}
			)
	return {"base": base["name"] if base else None, "addons": [r["name"] for r in addons], "items": items,
	        "sources": sources}


def check_templates() -> list:
	"""Every daily-check template's name (a check started on a machine raises no further check)."""
	return [r["name"] for kind in (*KINDS, "Person — Start of Day", "Person — End of Day") for r in _templates(kind)]


def existing_today(worker: str, asset: str) -> str:
	"""The operator's check on this machine raised today, if any."""
	today = str(frappe.utils.today())
	templates = [r["name"] for r in _templates(START)]
	if not templates:
		return ""
	rows = frappe.db.get_all(
		"Farm Task",
		filters={"assigned_to": worker, "asset": asset, "template": ("in", templates),
		         "creation": (">=", f"{today} 00:00:00")},
		fields=["name"],
		limit=1,
	)
	return rows[0]["name"] if rows else ""


def ensure_equipment_check(worker: str, asset: str, company: str = "") -> dict | None:
	"""Raise today's check of `asset` for `worker` if there is none. None when not applicable."""
	if not (enabled() and worker and asset) or not compat.doctype_exists("Asset Register"):
		return None
	if not frappe.db.exists("Asset Register", asset):
		return None
	already = existing_today(worker, asset)
	if already:
		return {"task": already, "created": False}
	asset_type = str(frappe.db.get_value("Asset Register", asset, "asset_type") or "")
	composed = compose(START, asset_type)
	template = composed["base"] or (composed["addons"][0] if composed["addons"] else None)
	if not template or not composed["items"]:
		return None
	from .tools import tasktemplates

	args = {"template": template, "assigned_to": worker, "task_name": f"Daily check — {asset}"[:140]}
	if company:
		args["company"] = company
	created = tasktemplates.create_task_from_template(args, fields={"asset": asset})
	name = created.data.get("name") if isinstance(created.data, dict) else None
	if not name:
		return None
	frappe.db.set_value("Farm Task", name, "checklist_status", json.dumps({"items": composed["items"]}))
	return {"task": name, "created": True, "items": len(composed["items"]), "asset_type": asset_type or None,
	        "from": composed["sources"]}


#: Seeded DISABLED-SAFE: the templates exist, nothing is raised until `daily_checks_enabled`.
SEEDS = (
	{
		"template_name": "Daily equipment check",
		"task_type": "Inspection",
		"daily_check": START,
		"description": "The operator's walk-round before a machine is used today, per its SOP.",
		"evidence_required": {"signature": True},
		"checklist": [
			"Walk round: no fresh leaks under the machine",
			"Fluids checked: engine oil, coolant, hydraulic",
			"Tires or tracks: no damage, correct tension or pressure",
			"Guards and shields in place",
			"Seat belt and ROPS / cab in good order",
			"Lights, horn and backup alarm work",
			"Fire extinguisher present and charged",
		],
	},
	{
		"template_name": "Daily check add-on — Tractor",
		"task_type": "Inspection",
		"daily_check": START,
		"applies_to_asset_types": ["Tractor"],
		"description": "What a tractor adds to the daily check.",
		"evidence_required": {"signature": True},
		"checklist": ["PTO shield in place", "Three-point hitch pins and clips secure", "SMV sign clean and visible"],
	},
	{
		"template_name": "Daily check add-on — Mini Excavator",
		"task_type": "Inspection",
		"daily_check": START,
		"applies_to_asset_types": ["Mini Excavator"],
		"description": "What a mini excavator (track hoe) adds to the daily check.",
		"evidence_required": {"signature": True},
		"checklist": [
			"Tracks and rollers: no damage, tension right",
			"Bucket pins and bushings greased",
			"Hydraulic hoses: no chafing or weeping",
			"Quick coupler locked",
			"Swing lock and travel alarm work",
		],
	},
)


def seed() -> list:
	"""Create the three templates where absent. Create-only."""
	from . import task_templates

	if not compat.has_field(TEMPLATE, "daily_check"):
		return []
	made = []
	for spec in SEEDS:
		if frappe.db.exists(TEMPLATE, {"template_name": spec["template_name"]}):
			continue
		doc = task_templates.build_template(dict(spec))
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
		made.append(doc.name)
	return made


# ── v0.252.0: each person's own Start / End of Day (approved queue item 3, part 2) ──────────────
#
# ONCE PER PERSON PER DAY, AS FARM TASKS (decision 33's shape, not a new doctype). Start of Day is
# raised at the person's first task start of the day, or when the phone asks ("Start my day"); End of
# Day at clock-out, or when the phone asks ("End my day").
#
# THE END OF DAY FORM IS BUILT FOR THAT PERSON AND DAY: the template's items, then an hour-meter
# reading for each metered machine they used today, then the crop stage of each block they worked
# whose stage is missing or over a week old (decision 27's End-of-Day prompt). Completing it files the
# readings through `engine_hours.record_completion_reading` and the stages through
# `growth_stage.record` — the same paths a task completion and the stage picker use.
#
# Off until `personal_day_checks_enabled`.
PERSON_START, PERSON_END = "Person — Start of Day", "Person — End of Day"
PERSON_KINDS = (PERSON_START, PERSON_END)

PERSON_SEEDS = (
	{
		"template_name": "Start of Day — me",
		"task_type": "Inspection",
		"daily_check": PERSON_START,
		"description": "Each worker's own check before the day's work.",
		"evidence_required": {"signature": True},
		"checklist": [
			"I am fit for work today",
			"PPE for today's work is on (gloves, eyes, ears as the job needs)",
			"I know where water and shade are today",
			"I know today's hazards (machines, sprays, heat, slopes)",
		],
	},
	{
		"template_name": "End of Day — me",
		"task_type": "Inspection",
		"daily_check": PERSON_END,
		"description": "Each worker's own close-out: injuries or near misses, equipment returned, hour meters and crop stage.",
		"evidence_required": {"signature": True},
		"checklist": [
			"No injury or near miss today (or I reported it)",
			"Equipment I used is parked, shut down and keys returned",
		],
	},
)


def personal_enabled() -> bool:
	try:
		from . import settings

		return bool(compat.checked(settings.get_settings().get("personal_day_checks_enabled")))
	except Exception:
		return False


def seed_personal() -> list:
	from . import task_templates

	if not compat.has_field(TEMPLATE, "daily_check"):
		return []
	made = []
	for spec in PERSON_SEEDS:
		if frappe.db.exists(TEMPLATE, {"template_name": spec["template_name"]}):
			continue
		doc = task_templates.build_template(dict(spec))
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
		made.append(doc.name)
	return made


def _today() -> str:
	return str(frappe.utils.today())[:10]


def personal_today(worker: str, kind: str) -> str:
	templates = [r["name"] for r in _templates(kind)]
	if not templates or not worker:
		return ""
	rows = frappe.db.get_all("Farm Task", filters={"assigned_to": worker, "template": ("in", templates),
	                                              "creation": (">=", f"{_today()} 00:00:00")},
	                         fields=["name"], limit=1)
	return rows[0]["name"] if rows else ""


def _worked_today(worker: str) -> list:
	"""Farm Tasks this person started today (from their assignments)."""
	rows = frappe.db.get_all("Farm Task Assignment",
	                         filters={"assigned_to": worker, "started_at": (">=", f"{_today()} 00:00:00")},
	                         fields=["task"], limit=200) or []
	names = sorted({r["task"] for r in rows if r.get("task")})
	if not names:
		return []
	return [dict(r) for r in frappe.db.get_all("Farm Task", filters={"name": ("in", names)},
	                                          fields=compat.existing_fields("Farm Task", ("name", "asset", "location_doctype",
	                                                                                      "location", "template")),
	                                          limit=200) or []]


def _slug(text: str) -> str:
	import re

	return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")[:40] or "x"


def end_of_day_extras(worker: str) -> tuple[list, dict]:
	"""(extra form fields, targets {key: {kind, asset|block}}) for this person's day."""
	from . import growth_stage
	from .tools import engine_hours

	fields, targets = [], {}
	worked = [t for t in _worked_today(worker) if t.get("template") not in check_templates()]
	for asset in sorted({t["asset"] for t in worked if t.get("asset")}):
		row = frappe.db.get_value("Asset Register", asset, ["asset_type"], as_dict=True) or {}
		if not engine_hours.has_hour_meter(str(row.get("asset_type") or "")):
			continue
		last = engine_hours.last_reading(asset) or {}
		key = f"hours_{_slug(asset)}"
		field = {"key": key, "type": "number", "required": False,
		         "label": {"en": f"Hour meter now — {asset}", "es": f"Horómetro ahora — {asset}"}}
		if last.get("engine_hours") not in (None, ""):
			field["min"] = float(last["engine_hours"])
			field["help"] = {"en": f"Last reading {last['engine_hours']}", "es": f"Última lectura {last['engine_hours']}"}
		fields.append(field)
		targets[key] = {"kind": "hours", "asset": asset}
	if growth_stage.installed():
		for block in sorted({t["location"] for t in worked if t.get("location_doctype") == "Field" and t.get("location")}):
			ask = growth_stage.prompt({"location_doctype": "Field", "location": block}, "end_of_day")
			if not ask:
				continue
			key = f"stage_{_slug(block)}"
			last = f" (last BBCH {ask['last_bbch']})" if ask.get("last_bbch") else ""
			fields.append({"key": key, "type": "number", "required": False, "min": 0, "max": 99,
			               "label": {"en": f"Crop stage now (BBCH) — {block}{last}",
			                         "es": f"Etapa del cultivo ahora (BBCH) — {block}{last}"}})
			targets[key] = {"kind": "stage", "block": block}
	return fields, targets


def ensure_personal_check(worker: str, kind: str, company: str = "") -> dict | None:
	"""Raise today's Start / End of Day for `worker` if there is none. None when not applicable."""
	if kind not in PERSON_KINDS or not (personal_enabled() and worker):
		return None
	already = personal_today(worker, kind)
	if already:
		return {"task": already, "created": False, "kind": kind}
	rows = _templates(kind)
	if not rows:
		return None
	from . import form_schema, task_templates
	from .tools import tasktemplates

	template = rows[0]["name"]
	label = "Start of Day" if kind == PERSON_START else "End of Day"
	name_of = frappe.db.get_value("Employee", worker, "employee_name") or worker
	args = {"template": template, "assigned_to": worker, "task_name": f"{label} — {name_of}"[:140]}
	if company:
		args["company"] = company
	created = tasktemplates.create_task_from_template(args)
	name = created.data.get("name") if isinstance(created.data, dict) else None
	if not name:
		return None
	extras, targets = ([], {}) if kind == PERSON_START else end_of_day_extras(worker)
	if extras:
		base = form_schema.legacy_fields(task_templates.checklist_of(template))
		frappe.db.set_value("Farm Task", name, {
			"form_schema": json.dumps(base + [{"key": "readings_heading", "type": "info",
			                                   "label": {"en": "Readings for today", "es": "Lecturas de hoy"}}] + extras),
			"creates_record_data": json.dumps({"end_of_day": targets}),
		})
	return {"task": name, "created": True, "kind": kind, "readings": len([t for t in targets.values() if t["kind"] == "hours"]),
	        "stages": len([t for t in targets.values() if t["kind"] == "stage"])}


def after_complete(task: dict, answers: dict | None, user: str, worker: str = "") -> dict | None:
	"""File an End of Day's hour-meter readings and crop stages. Never raises; reports what it did."""
	if not answers or task.get("template") not in [r["name"] for r in _templates(PERSON_END)]:
		return None
	try:
		data = json.loads(task.get("creates_record_data") or "{}")
	except ValueError:
		data = {}
	targets = (data or {}).get("end_of_day") or {}
	out = {"readings": [], "stages": []}
	for key, target in targets.items():
		value = answers.get(key)
		if value in (None, ""):
			continue
		try:
			if target["kind"] == "hours":
				from .tools import engine_hours

				out["readings"].append(engine_hours.record_completion_reading(
					target["asset"], value, performed_by=user, task=task["name"]))
			elif target["kind"] == "stage":
				from . import growth_stage

				code = str(int(float(value))).zfill(2)
				out["stages"].append(growth_stage.record(block=target["block"], code=code, observer=user,
				                                         source_task=task["name"], company=str(task.get("company") or "")))
		except Exception as exc:
			out.setdefault("problems", []).append(f"{key}: {type(exc).__name__}: {exc}")
			frappe.log_error(title="End of Day reading not filed", message=frappe.get_traceback())
	return out

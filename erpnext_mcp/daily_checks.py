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
	return [r["name"] for kind in KINDS for r in _templates(kind)]


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

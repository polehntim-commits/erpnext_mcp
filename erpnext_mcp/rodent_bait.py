# SPDX-License-Identifier: MIT
"""Rodent bait at housing and buildings — part of camp maintenance. v0.203.0.

Tim, 2026-09-27: pest control for all housing and buildings, occupied ones in
the strictest tier. The contract is `docs/design/rodent_bait_program.md`; this
module is the one place the program's facts are computed, so the task
controller, the dispatch refusals, the stock trigger, the inspection sessions,
the housing gate, the sweep's scanners and the calendar cannot disagree about
whether a cabin is occupied or a round is cleared.

THE WORK IS FIVE FARM TASK TEMPLATES, and every template is data an operator
edits. What this module adds is what a template cannot say:

  * OCCUPANCY — an active Housing Assignment, the place's own `occupied` flag,
    or people working there. The flag is first-class: Mill Creek's four houses
    are occupied at takeover and their tenants never come through an assignment.
  * THE ROUND — a placement up to the next COMPLETED Removal and Clearance.
  * THE LOCATION'S BAIT STATE — Active or Maintenance, which with the season
    decides whether the next check is due in 7 days or 30.

NOTHING HERE ENABLES ANYTHING. A disabled template raises no task; every
trigger reports that it skipped and why.
"""

from __future__ import annotations

import datetime
import json
import re

import frappe

from . import compat
from .errors import ToolError

FARM_TASK = "Farm Task"
HOUSING_UNIT = "Housing Unit"
ASSET_REGISTER = "Asset Register"
HOUSING_ASSIGNMENT = "Housing Assignment"
TEMPLATE_DOCTYPE = "Farm Task Template"

EXTERIOR = "Rodent Bait Placement - Exterior"
INTERIOR = "Rodent Bait Placement - Interior"
CHECK = "Rodent Bait Check"
REMOVAL = "Rodent Bait Removal and Clearance"
NOTICE = "Rodent Bait Occupant Notice (EN/ES)"
TEMPLATES = (EXTERIOR, INTERIOR, CHECK, REMOVAL, NOTICE)
PLACEMENT_SIDE = {EXTERIOR: "Exterior", INTERIOR: "Interior"}
TEMPLATE_FOR_SIDE = {side: template for template, side in PLACEMENT_SIDE.items()}

#: The registers a bait location may be in.
LOCATION_DOCTYPES = (HOUSING_UNIT, ASSET_REGISTER)
#: Asset types that are buildings, where bait at "the asset" means at a building.
BUILDING_ASSET_TYPES = ("Cabin", "House", "Housing Unit", "Storage", "Cold Storage")

OCCUPIED = "Occupied"
UNOCCUPIED = "Unoccupied"
ACTIVE = "Active"
MAINTENANCE = "Maintenance"
CLEARED = "Cleared"
ACTIVITY = "Activity"
NO_ACTIVITY = "No activity"

COMPLETED = "Completed"
#: States in which a placement means bait is (or is about to be) on the ground.
PLACED_STATES = ("In-Progress", "Awaiting-Review", "Completed")
TERMINAL_STATES = ("Completed", "Rejected", "Cancelled", "Merged")

PEST_CONTROL_GROUP = "Pest Control Products"

#: What placement and removal require (v0.205.0: `required_certification` on the
#: template, enforced by `qualifications` for every template — no longer a setting).
APPLICATOR_CERTIFICATION = "Applicator License"
APPLICATOR_SKILL = "applicator"
CREW_SKILL = "camp_maintenance"
ALERT_ROLE = "Farm Manager"

#: `rodent_bait_check_overdue`'s extra_parameters when the rule carries none.
DEFAULT_INTERVALS = {
	"active_interval_days": 7,
	"knockdown_days": 10,
	"occupied": {"in_season_days": 7, "off_season_days": 30},
	"unoccupied": {"in_season_days": 7, "off_season_days": 30},
}


# ── what a task is ──────────────────────────────────────────────────────────
def is_bait_template(template) -> bool:
	return str(template or "") in TEMPLATES


def placement_side(template) -> str:
	return PLACEMENT_SIDE.get(str(template or ""), "")


def _location_of(row) -> tuple:
	get = row.get
	return str(get("location_doctype") or ""), str(get("location") or "")


# ── occupancy and the season (generic since v0.205.0: `occupancy.py`) ─────
from .occupancy import (  # noqa: E402  — re-exported; callers and tests use these names
	DEFAULT_SEASON as _DEFAULT_SEASON,  # noqa: F401
)
from .occupancy import active_assignment as _active_assignment  # noqa: E402, F401
from .occupancy import asset_state as _asset_state  # noqa: E402, F401
from .occupancy import in_season, occupancy, season_of, valid_mmdd  # noqa: E402, F401
from .occupancy import mmdd as _mmdd  # noqa: E402, F401

# ── bait tasks at one place ─────────────────────────────────────────────────
_TASK_FIELDS = (
	"name",
	"template",
	"state",
	"location_doctype",
	"location",
	"completed_at",
	"modified",
	"creation",
	"occupancy_at_creation",
	"bait_placement",
	"bait_product",
	"bait_activity",
	"company",
	"materials_used",
	"checklist_status",
)


def bait_tasks(location_doctype: str, location: str) -> list:
	"""Every bait task at one place, oldest first."""
	if not compat.doctype_exists(FARM_TASK) or not location:
		return []
	rows = frappe.db.get_all(
		FARM_TASK,
		filters={"location_doctype": location_doctype, "location": location},
		fields=compat.existing_fields(FARM_TASK, _TASK_FIELDS),
		limit=5000,
	)
	rows = [dict(row) for row in rows or [] if is_bait_template(row.get("template"))]
	rows.sort(key=lambda row: str(row.get("completed_at") or row.get("creation") or ""))
	return rows


def _done_at(row: dict) -> str:
	return str(row.get("completed_at") or "") if row.get("state") == COMPLETED else ""


def round_start(location_doctype: str, location: str, rows: list | None = None) -> str:
	"""When the current round began: the latest COMPLETED Removal and Clearance, or ''."""
	rows = bait_tasks(location_doctype, location) if rows is None else rows
	done = [_done_at(row) for row in rows if row.get("template") == REMOVAL and _done_at(row)]
	return max(done) if done else ""


def uncleared_interior(location_doctype: str, location: str, rows: list | None = None) -> list:
	"""Interior placements at a place that no LATER completed Removal and Clearance cleared."""
	rows = bait_tasks(location_doctype, location) if rows is None else rows
	removals = [_done_at(row) for row in rows if row.get("template") == REMOVAL and _done_at(row)]
	out = []
	for row in rows:
		if row.get("template") != INTERIOR or row.get("state") not in PLACED_STATES:
			continue
		placed = str(row.get("completed_at") or row.get("modified") or "")
		if any(done > placed for done in removals):
			continue
		out.append(str(row["name"]))
	return out


def completed_notice(location_doctype: str, location: str, rows: list | None = None) -> str:
	"""A Completed Occupant Notice for this round at this place, or ''."""
	rows = bait_tasks(location_doctype, location) if rows is None else rows
	since = round_start(location_doctype, location, rows)
	for row in reversed(rows):
		if row.get("template") == NOTICE and _done_at(row) and _done_at(row) >= since:
			return str(row["name"])
	return ""


def open_task(location_doctype: str, location: str, template: str, rows: list | None = None) -> str:
	rows = bait_tasks(location_doctype, location) if rows is None else rows
	for row in reversed(rows):
		if row.get("template") == template and row.get("state") not in TERMINAL_STATES:
			return str(row["name"])
	return ""


# ── the controller's hooks ──────────────────────────────────────────────────
def stamp_task(doc) -> None:
	"""Called from Farm Task `validate`: the occupancy snapshot and the placement side.

	Stamped ONCE — a snapshot is never recomputed, which is what makes it a
	record of the tier the work was raised in rather than of today's.
	"""
	template = str(doc.get("template") or "")
	if not is_bait_template(template):
		return
	side = placement_side(template)
	if side and not doc.get("bait_placement"):
		doc.bait_placement = side
	location_doctype, location = _location_of(doc)
	if location and not doc.get("occupancy_at_creation"):
		answer = occupancy(location_doctype, location)
		doc.occupancy_at_creation = OCCUPIED if answer["occupied"] else UNOCCUPIED
		doc.occupancy_source = answer["source"]


def on_task_completed(doc) -> None:
	"""Called from Farm Task `on_update` on the transition to Completed. Never raises."""
	try:
		template = str(doc.get("template") or "")
		location_doctype, location = _location_of(doc)
		if not is_bait_template(template) or location_doctype not in LOCATION_DOCTYPES or not location:
			return
		if not compat.has_field(location_doctype, "rodent_bait_state"):
			return
		values: dict = {"rodent_bait_state_since": doc.get("completed_at") or frappe.utils.now()}
		if template in PLACEMENT_SIDE:
			values["rodent_bait_state"] = ACTIVE
			values["rodent_bait_last_placement"] = doc.name
		elif template == CHECK:
			values["rodent_bait_state"] = ACTIVE if doc.get("bait_activity") != NO_ACTIVITY else MAINTENANCE
		elif template == REMOVAL:
			values["rodent_bait_state"] = CLEARED
		else:
			return
		for key, value in values.items():
			frappe.db.set_value(location_doctype, location, key, value, update_modified=False)
		# v0.205.0. The generic per-program mirror (docs/design/programs_and_field_kinds.md A6).
		if compat.has_field(location_doctype, "program_state"):
			raw = frappe.db.get_value(location_doctype, location, "program_state")
			try:
				states = json.loads(raw) if raw else {}
			except ValueError:
				states = {}
			entry = dict(states.get("rodent_bait") or {})
			entry.update(
				{"state": values["rodent_bait_state"], "since": str(values["rodent_bait_state_since"])}
			)
			if template in PLACEMENT_SIDE:
				entry["last_start"] = doc.name
			states["rodent_bait"] = entry
			frappe.db.set_value(
				location_doctype, location, "program_state", json.dumps(states), update_modified=False
			)
	except Exception:  # pragma: no cover - a completion must never fail over a mirror
		frappe.log_error(title="erpnext_mcp: rodent bait state not updated", message=compat.traceback_text())


# ── what a check found ──────────────────────────────────────────────────────
_NONE_WORDS = {"", "none", "no", "0", "nil", "n/a", "na", "zero", "no consumption", "not eaten"}


def activity_of(explicit, checklist_items: list) -> bool:
	"""Did a check find consumption, fresh signs of feeding or carcasses? §7.

	The explicit answer first; then a ticked "Rodent activity found" item; then
	the notes on the consumption and carcass items. Nothing said at all counts
	as ACTIVITY — an unrecorded check keeps the station on 7-day checks.
	"""
	if explicit is not None and explicit != "":
		if isinstance(explicit, str):
			return explicit.strip().lower() not in ("0", "false", "no", "none", "no activity")
		return bool(explicit)
	said = None
	for item in checklist_items or []:
		name = str(item.get("item_name") or "").casefold()
		note = str(item.get("note") or "").strip().casefold()
		if name.startswith("rodent activity found"):
			if item.get("done"):
				return True
			said = False if said is None else said
		elif "consumption" in name and note:
			if note not in _NONE_WORDS:
				return True
			said = False
		elif "carcass" in name and note:
			digits = re.findall(r"\d+", note)
			if (digits and int(digits[0]) > 0) or (not digits and note not in _NONE_WORDS):
				return True
			said = False
	return said is None


# ── who may do it (generic since v0.205.0: `qualifications.py`) ──────────
def applicator_qualification(employee: str, requirement: str = APPLICATOR_CERTIFICATION) -> str:
	"""Kept for callers: what satisfies the applicator requirement, or ''."""
	from . import qualifications

	return qualifications.qualification(employee, requirement)


def refuse_unqualified(task: dict, employee: str, verb: str) -> None:
	"""Kept for callers: the task's own `required_certification`, as for every task."""
	from . import qualifications

	qualifications.refuse_unqualified(task, employee, verb)


def refuse_start_without_notice(task: dict) -> None:
	"""A placement at an occupied place may not start before the Occupant Notice is done. §5."""
	if str(task.get("template") or "") not in PLACEMENT_SIDE:
		return
	location_doctype, location = _location_of(task)
	if not location:
		return
	occupied_then = task.get("occupancy_at_creation") == OCCUPIED
	occupied_now = occupancy(location_doctype, location)["occupied"]
	if not (occupied_then or occupied_now):
		return
	rows = bait_tasks(location_doctype, location)
	if completed_notice(location_doctype, location, rows):
		return
	pending = open_task(location_doctype, location, NOTICE, rows)
	raise ToolError(
		f"{location} is occupied, and bait may not be placed there before the English/Spanish "
		"occupant notice is posted. "
		+ (
			f"Complete {pending} (Rodent Bait Occupant Notice) first."
			if pending
			else "Raise a 'Rodent Bait Occupant Notice (EN/ES)' task for it and complete that first."
		)
		+ " Nothing was started."
	)


# ── the product's label ─────────────────────────────────────────────────────
LABEL_FIELDS = (
	"tamper_resistant_station_required",
	"max_distance_from_structure_ft",
	"burrow_baiting_allowed",
	"min_bait_days",
	"interior_use_allowed",
)


def product_label(item: str) -> dict:
	if not item or not compat.doctype_exists("Item") or not frappe.db.exists("Item", item):
		return {}
	fields = compat.existing_fields("Item", ("name", "item_name", "item_group", *LABEL_FIELDS))
	return dict(frappe.db.get_value("Item", item, fields, as_dict=True) or {})


def is_pest_control_item(item: str) -> bool:
	if not item or not compat.doctype_exists("Item"):
		return False
	return str(frappe.db.get_value("Item", item, "item_group") or "") == PEST_CONTROL_GROUP


def label_line(item: str) -> str:
	"""One line of the product's label rules, for the top of a placement task."""
	label = product_label(item)
	if not label:
		return ""
	parts = []
	if compat.checked(label.get("tamper_resistant_station_required")):
		parts.append("tamper-resistant stations required")
	distance = label.get("max_distance_from_structure_ft")
	if distance not in (None, "", 0, 0.0):
		parts.append(f"within {float(distance):g} ft of structures")
	if label.get("burrow_baiting_allowed") == "No":
		parts.append("no burrow baiting")
	if label.get("interior_use_allowed") == "No":
		parts.append("OUTDOOR USE ONLY")
	days = int(label.get("min_bait_days") or 0)
	if days:
		parts.append(f"keep bait out at least {days} days")
	if not parts:
		return f"Label ({label.get('item_name') or item}): read the label — its placement rules are not on the record yet."
	return f"Label ({label.get('item_name') or item}): " + " · ".join(parts) + "."


def materials_items(raw) -> list:
	"""The item codes on a task's `materials_used` JSON."""
	try:
		value = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or [])
	except Exception:
		return []
	return [
		str(row.get("item_code")) for row in value or [] if isinstance(row, dict) and row.get("item_code")
	]


def product_of(task: dict) -> str:
	if task.get("bait_product"):
		return str(task["bait_product"])
	for item in materials_items(task.get("materials_used")):
		if is_pest_control_item(item):
			return item
	return ""


# ── raising the work ────────────────────────────────────────────────────────
def template_exists(template: str) -> bool:
	try:
		return compat.doctype_exists(TEMPLATE_DOCTYPE) and bool(frappe.db.exists(TEMPLATE_DOCTYPE, template))
	except Exception:
		return False


def template_enabled(template: str) -> bool:
	if not compat.doctype_exists(TEMPLATE_DOCTYPE) or not frappe.db.exists(TEMPLATE_DOCTYPE, template):
		return False
	return compat.checked(frappe.db.get_value(TEMPLATE_DOCTYPE, template, "enabled"))


def raise_placement(
	location_doctype: str,
	location: str,
	side: str = "Exterior",
	*,
	product: str = "",
	materials: list | None = None,
	reason: str = "",
	company: str = "",
) -> dict:
	"""Raise the placement task a stock move or an inspection calls for. Never raises.

	Returns `{location_doctype, location, template, task, action, message}` with
	action `created`, `linked`, `skipped` or `refused`, and `notice` when an
	Occupant Notice task was raised with it.
	"""
	side = "Interior" if str(side or "").strip().casefold() == "interior" else "Exterior"
	template = TEMPLATE_FOR_SIDE[side]
	out = {
		"location_doctype": location_doctype,
		"location": location,
		"template": template,
		"task": None,
		"action": "skipped",
		"message": "",
	}
	try:
		if location_doctype not in LOCATION_DOCTYPES or not location:
			out["message"] = "No housing unit or building was named, so no bait task was raised."
			return out
		if not frappe.db.exists(location_doctype, location):
			out["message"] = f"No {location_doctype} called {location}; no bait task was raised."
			return out
		if side == "Interior" and product and product_label(product).get("interior_use_allowed") == "No":
			out["action"] = "refused"
			out["message"] = (
				f"{product}'s label does not allow use inside buildings, so no interior placement "
				f"was raised at {location}. Place it outside, or use a product labelled for indoor use."
			)
			return out
		rows = bait_tasks(location_doctype, location)
		existing = open_task(location_doctype, location, template, rows)
		if existing:
			out["task"] = existing
			out["action"] = "linked"
			out["message"] = (
				f"{existing} is already open for {side.lower()} bait at {location}; this was noted on it."
			)
			if reason:
				_note(existing, reason)
			return out
		if not template_enabled(template):
			out["message"] = (
				f"The '{template}' template is disabled or missing, so no task was raised at {location}. "
				"Enable it with update_farm_task_template when the program starts."
			)
			return out
		name = _create(template, location_doctype, location, company, product, materials, reason)
		out["task"] = name
		out["action"] = "created"
		out["message"] = f"Raised {name} ({side.lower()} rodent bait placement) at {location}."
		if occupancy(location_doctype, location)["occupied"] and not open_task(
			location_doctype, location, NOTICE, rows
		):
			if completed_notice(location_doctype, location, rows):
				pass
			elif template_enabled(NOTICE):
				notice = _create(NOTICE, location_doctype, location, company, "", None, reason)
				out["notice"] = notice
				out["message"] += (
					f" {location} is occupied, so {notice} (occupant notice) was raised too — it must be done first."
				)
			else:
				out["message"] += (
					f" {location} is occupied and the Occupant Notice template is disabled, so the "
					"placement cannot start until a notice task is raised and completed."
				)
	except Exception as exc:  # pragma: no cover - a trigger never fails its caller
		out["action"] = "skipped"
		out["message"] = f"No bait task was raised at {location}: {exc}"
	return out


def _create(template, location_doctype, location, company, product, materials, reason) -> str:
	"""One task off the template, through the same function every other path uses."""
	from .tools import tasktemplates

	notes = "\n".join(part for part in (label_line(product) if product else "", reason) if part)
	args = {"template": template, "location_doctype": location_doctype, "location": location}
	if company:
		args["company"] = company
	if notes:
		args["notes"] = notes
	fields: dict = {}
	if product and template in PLACEMENT_SIDE:
		fields["bait_product"] = product
	if materials and template in PLACEMENT_SIDE:
		fields["materials_used"] = json.dumps(materials)
	result = tasktemplates.create_task_from_template(args, origin="field_reported", fields=fields)
	return str(result.data.get("name"))


def _note(task: str, text: str) -> None:
	try:
		current = str(frappe.db.get_value(FARM_TASK, task, "notes") or "")
		frappe.db.set_value(
			FARM_TASK, task, "notes", (current + "\n\n" + text).strip(), update_modified=False
		)
	except Exception:  # pragma: no cover
		pass


# ── the next check (the generic `cadence` primitive since v0.205.0) ────────
#: `rodent_bait_check_overdue`'s grouped_cadence — docs/design/programs_and_field_kinds.md A4.
def cadence_params(extra: dict | None = None) -> dict:
	"""The rodent block, with a rule's own intervals laid over the defaults."""
	merged = intervals(extra)
	return {
		"group_by": "location",
		"anchor_filters": [
			{"field": "template", "op": "in", "value": [EXTERIOR, INTERIOR, CHECK]},
			{"field": "state", "op": "eq", "value": COMPLETED},
		],
		"start_filters": [
			{"field": "template", "op": "in", "value": [EXTERIOR, INTERIOR]},
			{"field": "state", "op": "eq", "value": COMPLETED},
		],
		"end_filters": [
			{"field": "template", "op": "eq", "value": REMOVAL},
			{"field": "state", "op": "eq", "value": COMPLETED},
		],
		"date_field": "completed_at",
		"state": {"field": "bait_activity", "active_values": [ACTIVITY], "blank_is_active": True},
		"knockdown_days": merged["knockdown_days"],
		"active_interval_days": merged["active_interval_days"],
		"tier": "occupancy",
		"intervals": {"occupied": merged["occupied"], "unoccupied": merged["unoccupied"]},
		"season": "company",
	}


def intervals(extra: dict | None = None) -> dict:
	merged = json.loads(json.dumps(DEFAULT_INTERVALS))
	for key, value in (extra or {}).items():
		if key in ("occupied", "unoccupied") and isinstance(value, dict):
			merged[key].update({k: v for k, v in value.items() if k in ("in_season_days", "off_season_days")})
		elif key in ("active_interval_days", "knockdown_days"):
			merged[key] = value
	return merged


def next_check(
	location_doctype: str,
	location: str,
	today: str | None = None,
	extra: dict | None = None,
	company: str = "",
) -> dict | None:
	"""When the next Rodent Bait Check is due at a place, and why. None when no bait is out."""
	from . import cadence

	today = str(today or frappe.utils.today())[:10]
	params = cadence.params_of((extra or {}).get("grouped_cadence") or cadence_params(extra))
	rows = [dict(row) for row in bait_tasks(location_doctype, location)]
	return cadence.evaluate(rows, params, (location_doctype, location), today)


def _state_of(location_doctype: str, location: str) -> str:
	if not compat.has_field(location_doctype, "rodent_bait_state"):
		return ""
	return str(frappe.db.get_value(location_doctype, location, "rodent_bait_state") or "")


def _days_between(start: str, end: str) -> int:
	return (datetime.date.fromisoformat(end[:10]) - datetime.date.fromisoformat(start[:10])).days


def bait_locations(company: str = "") -> list:
	"""Every (location_doctype, location) with a bait task on it."""
	if not compat.doctype_exists(FARM_TASK):
		return []
	filters = {"template": ("in", list(TEMPLATES))}
	if company:
		filters["company"] = company
	rows = frappe.db.get_all(FARM_TASK, filters=filters, fields=["location_doctype", "location"], limit=10000)
	seen = []
	for row in rows or []:
		key = (str(row.get("location_doctype") or ""), str(row.get("location") or ""))
		if key[0] in LOCATION_DOCTYPES and key[1] and key not in seen:
			seen.append(key)
	return seen


# ── the pre-occupancy gate ──────────────────────────────────────────────────
GATE = "housing_preoccupancy_bait_clearance"


def preoccupancy_gate(unit: str, company: str = "") -> dict:
	"""Run a new Housing Assignment through the clearance control. §8.

	Advisory files an alert and allows; Enforced raises `ToolError` naming the
	Removal and Clearance to complete; Off (the seed) does nothing. Call it
	BEFORE anything is written — see `enforcement.evaluate`.
	"""
	from . import enforcement

	findings = []
	for placement in uncleared_interior(HOUSING_UNIT, unit):
		pending = open_task(HOUSING_UNIT, unit, REMOVAL)
		findings.append(
			enforcement.Finding(
				control_point=GATE,
				message=(
					f"{unit} has interior rodent bait from {placement} that has not been cleared. "
					"Nobody should be assigned to it until a Rodent Bait Removal and Clearance there "
					"is completed with photos."
				),
				remedy=(
					f"Complete {pending} (Rodent Bait Removal and Clearance) at {unit}, then assign."
					if pending
					else f"Raise and complete a 'Rodent Bait Removal and Clearance' task at {unit}, then assign."
				),
				source_doctype=FARM_TASK,
				source_docname=placement,
				company=company,
				detail={"unit": unit, "placement": placement, "removal_task": pending or None},
			)
		)
	return enforcement.evaluate(GATE, findings, company=company)


# ── the stock trigger ───────────────────────────────────────────────────────
def check_stock_bait_args(args: dict) -> tuple:
	"""`(location_doctype, location, side)` from a stock call, refused when malformed.

	Checked BEFORE the entry is written, so a typo'd cabin never leaves a stock
	entry behind with no task for it.
	"""
	location = str(args.get("bait_location") or "").strip()
	location_doctype = str(args.get("bait_location_doctype") or "").strip()
	side = str(args.get("bait_placement") or "").strip()
	if not location:
		if location_doctype or side:
			raise ToolError(
				"bait_location_doctype / bait_placement were sent with no bait_location — name the "
				"housing unit or building the bait is going to. Nothing was created."
			)
		return "", "", ""
	if not location_doctype:
		location_doctype = HOUSING_UNIT if frappe.db.exists(HOUSING_UNIT, location) else ASSET_REGISTER
	if location_doctype not in LOCATION_DOCTYPES:
		raise ToolError(
			f"bait_location_doctype must be {' or '.join(LOCATION_DOCTYPES)}, got {location_doctype!r}. "
			"Nothing was created."
		)
	if not compat.doctype_exists(location_doctype) or not frappe.db.exists(location_doctype, location):
		raise ToolError(f"no {location_doctype} called {location!r} on this site. Nothing was created.")
	if side and side.casefold() not in ("exterior", "interior"):
		raise ToolError(f"bait_placement must be Exterior or Interior, got {side!r}. Nothing was created.")
	return location_doctype, location, ("Interior" if side.casefold() == "interior" else "Exterior")


def from_stock_entry(items: list, bait: tuple, company: str, entry: str) -> list | None:
	"""The placement tasks a stock entry of Pest Control products calls for. §4.1.

	`items` are the entry's lines (item_code, qty, uom, t_warehouse). `bait` is
	`check_stock_bait_args`'s answer. None when nothing here is rodent bait.
	"""
	pest = [row for row in items or [] if is_pest_control_item(str(row.get("item_code") or ""))]
	location_doctype, location, side = bait
	if not pest:
		if location:
			return [
				{
					"location_doctype": location_doctype,
					"location": location,
					"template": TEMPLATE_FOR_SIDE[side],
					"task": None,
					"action": "skipped",
					"message": f"Nothing on this entry is in {PEST_CONTROL_GROUP}, so no bait task was raised at {location}.",
				}
			]
		return None
	reason = f"Raised from Stock Entry {entry}."
	out = []
	targets: list = []
	if location:
		targets.append((location_doctype, location, side, pest))
	else:
		for row in pest:
			warehouse = str(row.get("t_warehouse") or "")
			for asset in _assets_holding(warehouse):
				targets.append((ASSET_REGISTER, asset, "Exterior", [row]))
	for doctype, place, where, lines in targets:
		materials = [
			{key: row.get(key) for key in ("item_code", "qty", "uom") if row.get(key) not in (None, "")}
			for row in lines
		]
		out.append(
			raise_placement(
				doctype,
				place,
				where,
				product=str(lines[0].get("item_code") or ""),
				materials=materials,
				reason=reason,
				company=company,
			)
		)
	return out


def _assets_holding(warehouse: str) -> list:
	"""Building assets whose `warehouse` is this one — a move there is a move to the building."""
	if not warehouse or not compat.has_field(ASSET_REGISTER, "warehouse"):
		return []
	rows = frappe.db.get_all(
		ASSET_REGISTER, filters={"warehouse": warehouse}, fields=["name", "asset_type"], limit=50
	)
	return [
		str(row["name"]) for row in rows or [] if str(row.get("asset_type") or "") in BUILDING_ASSET_TYPES
	]


# ── the inspection hooks ────────────────────────────────────────────────────
CLEARED_KEY = "rodent_bait_cleared"
ACTIVITY_KEY = "rodent_activity_seen"


def _truthy(value) -> bool:
	if isinstance(value, str):
		return value.strip().casefold() in ("1", "true", "yes", "y", "seen")
	return bool(value)


def review_clearance(session: dict, submitted: dict, template_sections: dict | None = None) -> list:
	"""§4.3. `rodent_bait_cleared` passes only when every interior round here is cleared.

	Mutates a submitted section: the key is set False and a sentence is added to
	its notes, which is what files the Housing Inspection as Corrective Action
	Required whatever the worker ticked. The section is optional (an older phone
	never shows it), so where the TEMPLATE carries it and the phone did not send
	it, the sentence goes onto the first submitted section that files a Housing
	Inspection — the judgement is the server's either way.
	"""
	location_doctype, location = _location_of(session)
	if location_doctype not in LOCATION_DOCTYPES or not location:
		return []
	carriers = [
		name
		for name, entry in submitted.items()
		if not entry.get("skipped") and CLEARED_KEY in (entry.get("checklist_items") or {})
	]
	asks = bool(carriers) or any(
		CLEARED_KEY in ((section.get("evidence_contract") or {}).get("checklist_items") or [])
		for section in (template_sections or {}).values()
	)
	if not asks:
		return []
	pending = uncleared_interior(location_doctype, location)
	if not pending:
		return []
	if not carriers:
		carriers = [
			name
			for name, entry in submitted.items()
			if not entry.get("skipped")
			and ((template_sections or {}).get(name) or {}).get("produces_record_doctype")
			== "Housing Inspection"
		][:1]
	sentence = (
		f"Rodent bait NOT cleared: interior placement {', '.join(pending)} has no completed "
		"Rodent Bait Removal and Clearance after it. Nobody may move in until it is done."
	)
	out = []
	for name in carriers:
		entry = submitted[name]
		checklist = entry.get("checklist_items") or {}
		if CLEARED_KEY in checklist:
			checklist[CLEARED_KEY] = False
			entry["checklist_items"] = checklist
		entry["notes"] = (str(entry.get("notes") or "").strip() + " " + sentence).strip()
		out.append({"section": name, "cleared": False, "placements": pending, "message": sentence})
	return out


def activity_tasks(session: dict, submitted: dict) -> list:
	"""§4.2. "Rodent activity seen?" answered yes raises a field-reported exterior placement."""
	location_doctype, location = _location_of(session)
	if location_doctype not in LOCATION_DOCTYPES or not location:
		return []
	for _name, entry in submitted.items():
		checklist = entry.get("checklist_items") or {}
		if not entry.get("skipped") and _truthy(checklist.get(ACTIVITY_KEY)):
			return [
				raise_placement(
					location_doctype,
					location,
					"Exterior",
					reason=f"Rodent activity reported on Inspection Session {session.get('name')}.",
					company=str(session.get("company") or ""),
				)
			]
	return []


# ── what a new site is seeded with ──────────────────────────────────────────
#: The five templates, DISABLED — the program is Tim's to switch on. Content is
#: the drafts on OML (2026-09-27) with Tim's decisions applied: checks and
#: notices are camp maintenance work, placement and removal are applicator work,
#: and the check cadence is the rule's, not the template's.
_OCC = "Occupancy tier stated: OCCUPIED (active housing assignment, marked occupied, or people work here) or UNOCCUPIED"
SEED_TASK_TEMPLATES = (
	{
		"template_name": EXTERIOR,
		"task_type": "Other",
		"enabled": 0,
		"description": (
			"Place rodent bait OUTSIDE a housing unit or building with a product from 'Pest Control "
			"Products'. One task per place per round. OCCUPIED: tamper-resistant stations only and the "
			"EN/ES occupant notice done BEFORE placement (the start is refused until it is)."
		),
		"skill_required": "applicator",
		"required_certification": APPLICATOR_CERTIFICATION,
		"estimated_duration_minutes": 45,
		"dispatch_mode": "Dispatched",
		"default_urgency": "Normal",
		"evidence_required": {"photos": True, "signature": True, "findings_text": True, "gps": True},
		"instructions": (
			"Read the product label before you start: the label is the law. Waterproof gloves on. "
			"OCCUPIED: every placement inside a locked tamper-resistant station, no loose pacs or "
			"blocks. Photograph EVERY station closed and locked. Count stations and pacs/blocks. "
			"Away from vents, food and food-contact surfaces. No place pacs in burrows. Stay within "
			"the label distance of the structure."
		),
		"regimes": ["OR-OSHA", "Internal"],
		"checklist": (
			{"item_name": "Product and EPA Reg. No. recorded", "evidence_type": "Text"},
			{"item_name": _OCC, "evidence_type": "Text"},
			{"item_name": "PPE worn: waterproof gloves (plus any on the label)", "evidence_type": "Photo"},
			{
				"item_name": "Every placement in a tamper-resistant station, locked - photo of each",
				"evidence_type": "Photo",
			},
			{"item_name": "Count: stations placed and pacs/blocks loaded", "evidence_type": "Measurement"},
			{
				"item_name": "Within the label's distance of the structure and spacing",
				"evidence_type": "Text",
			},
			{
				"item_name": "Not in burrows (unless the label allows), not near vents, food or food-contact surfaces"
			},
			{"item_name": "Station locations noted or tagged", "evidence_type": "Text"},
		),
	},
	{
		"template_name": INTERIOR,
		"task_type": "Other",
		"enabled": 0,
		"description": (
			"Place rodent bait INSIDE a housing unit or building — the highest-exposure use. Farm "
			"Manager approval first; locked tamper-resistant stations only; the EN/ES notice done "
			"first where occupied. Nobody is assigned to the unit until a Removal and Clearance is completed."
		),
		"skill_required": "applicator",
		"required_certification": APPLICATOR_CERTIFICATION,
		"estimated_duration_minutes": 45,
		"dispatch_mode": "Dispatched",
		"default_urgency": "High",
		"evidence_required": {"photos": True, "signature": True, "findings_text": True, "gps": True},
		"instructions": (
			"Only with the Farm Manager's approval. Waterproof gloves on. Locked tamper-resistant "
			"station for every placement, no loose pacs or blocks. Photograph EVERY station with the "
			"room visible. Never near vents, food, dishes, counters or food-contact surfaces; out of "
			"reach of children and pets. Write the rooms in the findings."
		),
		"regimes": ["OR-OSHA", "Internal"],
		"checklist": (
			{
				"item_name": "Farm Manager approval for interior placement (name/date)",
				"evidence_type": "Text",
			},
			{"item_name": "Product and EPA Reg. No. recorded", "evidence_type": "Text"},
			{"item_name": _OCC, "evidence_type": "Text"},
			{"item_name": "PPE worn: waterproof gloves (plus any on the label)", "evidence_type": "Photo"},
			{
				"item_name": "Every placement in a locked tamper-resistant station - photo of each with the room visible",
				"evidence_type": "Photo",
			},
			{
				"item_name": "Count: stations placed and pacs/blocks loaded, by room",
				"evidence_type": "Measurement",
			},
			{"item_name": "Not near vents, food or food-contact surfaces; out of reach of children and pets"},
		),
	},
	{
		"template_name": CHECK,
		"task_type": "Inspection",
		"enabled": 0,
		"description": (
			"Check every active bait station at one place: consumption, replenish, carcasses, station "
			"intact and locked. Due every 7 days while bait is active or in season; every 30 days off "
			"season for maintenance stations with no activity (rodent_bait_check_overdue)."
		),
		"skill_required": "camp_maintenance",
		"estimated_duration_minutes": 30,
		"dispatch_mode": "Dispatched",
		"default_urgency": "Normal",
		"evidence_required": {"photos": True, "findings_text": True, "gps": True},
		"instructions": (
			"Waterproof gloves on. Visit every station, not a sample. Photograph each open (showing "
			"bait) and locked. Record consumption and what you added. Bag every carcass and dispose of "
			"it as the label says. Say whether you found activity — consumption, fresh signs of feeding "
			"or carcasses: that decides whether the next check is in a week or a month."
		),
		"regimes": ["OR-OSHA", "Internal"],
		"checklist": (
			{"item_name": _OCC, "evidence_type": "Text"},
			{
				"item_name": "Every station visited - photo of each (open, then locked)",
				"evidence_type": "Photo",
			},
			{"item_name": "Consumption per station (none / partial / all)", "evidence_type": "Text"},
			{"item_name": "Pacs/blocks replenished (count)", "evidence_type": "Measurement"},
			{
				"item_name": "Carcasses collected with waterproof gloves (count) and disposed per label",
				"evidence_type": "Measurement",
			},
			{"item_name": "Stations intact, locked, not moved", "evidence_type": "Photo"},
			{
				"item_name": "Rodent activity found (consumption, fresh signs of feeding or carcasses) - tick only if found",
				"required": False,
			},
		),
	},
	{
		"template_name": REMOVAL,
		"task_type": "Housing-Cleanup",
		"enabled": 0,
		"description": (
			"End a baiting round at one place: every station removed, leftover bait and carcasses "
			"collected, disposed per label. For interior bait at a housing unit this is the "
			"PRE-OCCUPANCY CLEARANCE: only a COMPLETED one clears the unit."
		),
		"skill_required": "applicator",
		"required_certification": APPLICATOR_CERTIFICATION,
		"estimated_duration_minutes": 45,
		"dispatch_mode": "Dispatched",
		"default_urgency": "High",
		"evidence_required": {"photos": True, "signature": True, "findings_text": True, "gps": True},
		"instructions": (
			"Waterproof gloves on. The number you remove must match the number placed. Photograph "
			"each former station location empty. Collect leftover bait and every carcass; dispose of "
			"them and the packaging as the label's Storage and Disposal section says. Return sealed "
			"product to the chemical store. Sign when done."
		),
		"regimes": ["OR-OSHA", "Internal"],
		"checklist": (
			{
				"item_name": "Occupancy tier stated and whether this is a pre-occupancy clearance",
				"evidence_type": "Text",
			},
			{"item_name": "Stations removed (count) matches stations placed", "evidence_type": "Measurement"},
			{"item_name": "Photo of each former placement location, empty", "evidence_type": "Photo"},
			{"item_name": "Leftover bait collected and disposed per label", "evidence_type": "Measurement"},
			{
				"item_name": "Carcasses collected with waterproof gloves (count) and disposed per label",
				"evidence_type": "Measurement",
			},
			{
				"item_name": "Interior: surfaces near former stations free of bait residue",
				"evidence_type": "Photo",
			},
			{"item_name": "Unused sealed product returned to the chemical store", "required": False},
		),
	},
	{
		"template_name": NOTICE,
		"task_type": "Other",
		"enabled": 0,
		"description": (
			"Post and hand out the English/Spanish notice BEFORE rodent bait is placed at an occupied "
			"unit or building. A placement there cannot start until this is completed for the round. "
			"Internal policy."
		),
		"skill_required": "camp_maintenance",
		"estimated_duration_minutes": 15,
		"dispatch_mode": "Dispatched",
		"default_urgency": "High",
		"evidence_required": {"photos": True, "findings_text": True, "gps": True},
		"instructions": (
			"Post the EN/ES notice at every entrance and near each exterior station group: bait is in "
			"locked stations; do not open, move or touch them; keep children and pets away; do not "
			"handle dead rodents - report them; who to call; start and expected removal dates; product "
			"name and EPA Reg. No. Tell each occupant in person where you can. Photograph every notice."
		),
		"regimes": ["Internal"],
		"checklist": (
			{"item_name": "English notice posted at each entrance", "evidence_type": "Photo"},
			{"item_name": "Spanish notice posted at each entrance", "evidence_type": "Photo"},
			{"item_name": "Product, EPA Reg. No., dates and contact on the notice", "evidence_type": "Text"},
			{"item_name": "Occupants told in person (names or 'none present')", "evidence_type": "Text"},
		),
	},
)


# ── the sweep ───────────────────────────────────────────────────────────────
def _extra_of(context: dict) -> dict:
	from .compliance_rules import as_object

	try:
		return as_object((context.get("rule") or {}).get("extra_parameters_json"), "extra_parameters")
	except ValueError:
		return {}


#: The v0.203.0 message, as a template the generic scan renders.
CHECK_MESSAGE = (
	"Rodent bait check {% if days_overdue == 0 %}due today{% else %}{{ days_overdue }} day(s) overdue{% endif %} "
	"at {{ location }} ({{ tier|lower }}, {% if in_season %}in{% else %}off{% endif %} season, "
	"{% if phase == 'knockdown' %}a new placement is still in its knockdown window"
	"{% elif phase == 'active' %}the last check found activity (or bait is newly placed)"
	"{% else %}a maintenance station with no activity{% endif %}): every {{ interval_days }} days, "
	"last bait task {{ anchor_task }} on {{ anchor[:10] }}. Check every station — consumption, replenish, "
	"carcasses with waterproof gloves, stations locked — and record whether there was activity."
)


def scan_check_overdue(context: dict) -> list:
	"""The builtin name, kept: a row that still names it runs the generic primitive."""
	from . import cadence

	extra = _extra_of(context)
	rule = dict(context.get("rule") or {})
	rule.setdefault("target_doctype", FARM_TASK)
	rule["message_template"] = rule.get("message_template") or CHECK_MESSAGE
	rule["category"] = rule.get("category") or "Housing"
	return cadence.scan(rule, context, extra.get("grouped_cadence") or cadence_params(extra))


def scan_label_conformance(context: dict) -> list:
	"""`rodent_bait_label_conformance` (§8): each placement against its product's label."""
	from .alerts.base import SEVERITY_CRITICAL, SEVERITY_WARNING, Observation

	company = str(context.get("company") or "")
	out = []
	for location_doctype, location in bait_locations(company):
		rows = bait_tasks(location_doctype, location)
		for row in rows:
			template = row.get("template")
			if template not in PLACEMENT_SIDE or row.get("state") not in PLACED_STATES:
				continue
			product = product_of(row)
			label = product_label(product)
			name = str(row["name"])
			if template == INTERIOR and label.get("interior_use_allowed") == "No":
				out.append(
					Observation(
						source_doctype=FARM_TASK,
						source_docname=name,
						message=(
							f"{name} placed {label.get('item_name') or product} INSIDE {location}, and its label "
							"does not allow use inside buildings. Remove it (Rodent Bait Removal and Clearance) "
							"and use a product labelled for indoor use."
						),
						severity=SEVERITY_CRITICAL,
						company=str(row.get("company") or ""),
						category="Housing",
					)
				)
			if (
				compat.checked(label.get("tamper_resistant_station_required"))
				and row.get("occupancy_at_creation") == OCCUPIED
				and row.get("state") == COMPLETED
				and not _ticked(row, "tamper-resistant")
			):
				out.append(
					Observation(
						source_doctype=FARM_TASK,
						source_docname=name,
						message=(
							f"{name} at {location} (occupied) used {label.get('item_name') or product}, whose "
							"label requires tamper-resistant stations, and the placement does not record them. "
							"Confirm every placement is in a locked station."
						),
						severity=SEVERITY_WARNING,
						company=str(row.get("company") or ""),
						category="Housing",
					)
				)
		minimum = 0
		since = ""
		for row in rows:
			if row.get("template") in PLACEMENT_SIDE and _done_at(row):
				label = product_label(product_of(row))
				minimum = max(minimum, int(label.get("min_bait_days") or 0))
				since = since or _done_at(row)
			if row.get("template") == REMOVAL and _done_at(row) and since:
				days = _days_between(since[:10], _done_at(row)[:10])
				last_check = [
					r
					for r in rows
					if r.get("template") == CHECK and _done_at(r) and since <= _done_at(r) <= _done_at(row)
				]
				quiet = bool(last_check) and last_check[-1].get("bait_activity") == NO_ACTIVITY
				if minimum and days < minimum and not quiet:
					out.append(
						Observation(
							source_doctype=FARM_TASK,
							source_docname=str(row["name"]),
							message=(
								f"{row['name']} removed the bait at {location} after {days} day(s); the label asks "
								f"for at least {minimum}. Record why, or re-bait if rodents are still active."
							),
							severity=SEVERITY_WARNING,
							company=str(row.get("company") or ""),
							category="Housing",
						)
					)
				minimum, since = 0, ""
	return out


def _ticked(row: dict, needle: str) -> bool:
	from .erpnext_mcp.doctype.farm_task.farm_task import checklist_items

	return any(
		needle in str(item.get("item_name") or "").casefold() and item.get("done")
		for item in checklist_items(row.get("checklist_status"))
	)


_TIERED = (
	"OCCUPIED = an active Housing Assignment, the place marked Occupied, or people work there "
	"(the people-work-here list in ERPNext MCP Settings); everything else is UNOCCUPIED."
)
_CITES = (
	"Product label directions (FIFRA §12(a)(2)(G), 7 U.S.C. 136j); 29 CFR 1910.142(j) insect and "
	"rodent control in temporary labor camps; OAR 437-004-1120 agricultural labor housing"
)


def rule_seed_specs() -> list:
	"""The four rodent bait rules, DISABLED and unapproved — the program is Tim's to switch on."""
	role = ALERT_ROLE
	return [
		{
			"rule_id": "rodent_bait_interior_placement",
			"title": "Rodent bait has been placed INSIDE a unit or building and has not been cleared",
			"category": "Housing",
			"target_doctype": FARM_TASK,
			"requires_doctypes": FARM_TASK,
			"enabled": 0,
			"date_field": "completed_at",
			"date_field_role": "Timestamp",
			"due_date_mode": "None",
			"missing_date_behaviour": "Raise",
			"cadence_days": 0,
			"threshold_critical_days": 30,
			"threshold_warning_days": 90,
			"severity_expired": "Critical",
			"default_severity": "Critical",
			"scope_filters": [
				{"field": "template", "op": "eq", "value": INTERIOR},
				{"field": "state", "op": "in", "value": list(PLACED_STATES)},
			],
			"superseded_by_later_clean": {
				"subject_field": "location",
				"date_field": "completed_at",
				"finding_date_fields": ["completed_at", "modified"],
				"clean_filters": [
					{"field": "template", "op": "eq", "value": REMOVAL},
					{"field": "state", "op": "eq", "value": COMPLETED},
				],
				"unreadable_counts_as_dirty": True,
			},
			"extra_parameters": {
				"severity_by_field": {"field": "occupancy_at_creation", "map": {UNOCCUPIED: "Warning"}},
				"notify_roles": [role],
				"notify_severities": ["Critical", "Warning"],
			},
			"message_template": (
				"INTERIOR rodent bait at {{ row.location or row.task_name }} ({{ row.occupancy_at_creation "
				"or 'occupancy unknown' }}; task {{ row.name }}, {{ row.state }}). Confirm it was approved, "
				"every placement is in a locked tamper-resistant station and the EN/ES notice was posted "
				"first. A housing unit must not be assigned until a Rodent Bait Removal and Clearance there "
				"is COMPLETED."
			),
			"regimes": ["OR-OSHA", "Internal"],
			"regulation_citations": _CITES,
			"retention_years": 3,
			"audit_packet_types": ["OSHA", "EPA"],
			"purpose": (
				"Interior bait where people live or work is the highest-exposure use of a rodenticide. "
				"The Farm Manager must know about every interior placement while it is in place."
			),
			"kairotic_gate_description": (
				"Fires on STATE: an Interior placement task In-Progress, Awaiting-Review or Completed. "
				"Critical where the place was occupied when the task was raised, Warning where it was "
				f"not. {_TIERED} Silenced ONLY by a later COMPLETED Rodent Bait Removal and Clearance at "
				"the same place — a removal that is merely raised or in progress clears nothing."
			),
			"authored_by": "System",
		},
		{
			"rule_id": "rodent_bait_check_overdue",
			"title": "Rodent bait at a unit or building is overdue for its station check",
			"category": "Housing",
			"target_doctype": FARM_TASK,
			"requires_doctypes": FARM_TASK,
			"enabled": 0,
			# v0.205.0. Declarative: the generic grouped_cadence primitive carries
			# every tunable (docs/design/programs_and_field_kinds.md A4).
			"cadence_days": 7,
			"message_template": CHECK_MESSAGE,
			"date_field": "completed_at",
			"due_date_mode": "None",
			# The Link only where the template exists: the rules seed before the
			# templates on a fresh site, and a dangling Link refuses the whole rule.
			**({"producer_task_template": CHECK} if template_exists(CHECK) else {}),
			"extra_parameters": {
				**json.loads(json.dumps(DEFAULT_INTERVALS)),
				"grouped_cadence": cadence_params(),
				"notify_roles": [role],
				"notify_severities": ["Critical"],
			},
			"regimes": ["OR-OSHA", "Internal"],
			"regulation_citations": _CITES,
			"retention_years": 3,
			"audit_packet_types": ["OSHA", "EPA"],
			"purpose": (
				"Unchecked stations get opened, moved or emptied, carcasses sit where children and pets "
				"find them, and consumption goes unrecorded. Regular checks make baiting near people "
				"defensible."
			),
			"kairotic_gate_description": (
				"Fires when a place's next check is due, measured from its latest COMPLETED placement or "
				"check (completed_at). Every 7 days while bait is ACTIVE — a new placement's first "
				"knockdown_days (10), or a last check that found consumption, fresh feeding signs or "
				"carcasses — in season and off. A MAINTENANCE station (last check found no activity) is "
				"checked every in_season_days in the company's pest season and every off_season_days (30) "
				f"outside it. Tier by today's occupancy: {_TIERED} Silenced by a later completed check or "
				"a completed Removal and Clearance. Tim, 2026-09-27: 'seven days should be fine … maybe "
				"out to once a month during the off season.'"
			),
			"authored_by": "System",
		},
		{
			"rule_id": "pest_control_label_fields_missing",
			"title": "A pest control product is missing the label fields the bait rules read",
			"category": "Records",
			"target_doctype": "Item",
			"requires_doctypes": "Item",
			"enabled": 0,
			"missing_date_behaviour": "Skip",
			"due_date_mode": "None",
			"severity_expired": "Warning",
			"default_severity": "Warning",
			"requires_fields": [
				"epa_registration_number",
				"application_rate",
				"ppe_requirements",
				"storage_disposal",
				"label_scan_validation",
			],
			"scope_filters": [
				{"field": "item_group", "op": "eq", "value": PEST_CONTROL_GROUP},
				{"field": "disabled", "op": "isfalse"},
				{
					"field": "",
					"op": "any",
					"value": [
						{"field": field, "op": "isnull"}
						for field in (
							"epa_registration_number",
							"application_rate",
							"ppe_requirements",
							"storage_disposal",
							"label_scan_validation",
							"interior_use_allowed",
							"min_bait_days",
						)
					],
				},
			],
			"message_template": (
				"{{ row.item_name }} ({{ row.name }}) is missing label data the bait rules read: "
				"{% if not row.epa_registration_number %}EPA Reg. No.; {% endif %}"
				"{% if not row.application_rate %}application rate; {% endif %}"
				"{% if not row.ppe_requirements %}PPE; {% endif %}"
				"{% if not row.storage_disposal %}storage/disposal; {% endif %}"
				"{% if not row.label_scan_validation %}confirmed label scan; {% endif %}"
				"{% if not row.interior_use_allowed %}interior use allowed?; {% endif %}"
				"{% if not row.min_bait_days %}minimum bait days; {% endif %}"
				"Fill these in from the label before it is used at any unit or building."
			),
			"regimes": ["Internal"],
			"regulation_citations": "Product label directions (FIFRA §12(a)(2)(G), 7 U.S.C. 136j)",
			"retention_years": 3,
			"purpose": (
				"The bait rules follow whatever product is used. A product with nothing on the record "
				"cannot be placed by the book, because the worker has nothing to follow."
			),
			"kairotic_gate_description": (
				"Fires on STATE: an enabled Pest Control Products item with any of the label fields "
				"empty. Silences when they are filled."
			),
			"authored_by": "System",
		},
		{
			"rule_id": "rodent_bait_label_conformance",
			"title": "A rodent bait placement does not conform to its product's label",
			"category": "Housing",
			"target_doctype": FARM_TASK,
			"requires_doctypes": FARM_TASK,
			"enabled": 0,
			"builtin_scanner": "rodent_bait_label_conformance",
			"extra_parameters": {"notify_roles": [role], "notify_severities": ["Critical"]},
			"regimes": ["OR-OSHA", "Internal"],
			"regulation_citations": _CITES,
			"retention_years": 3,
			"audit_packet_types": ["OSHA", "EPA"],
			"purpose": "Use inconsistent with the label is unlawful. Each placement is checked against its own product.",
			"kairotic_gate_description": (
				"Fires on a placement whose product's label it contradicts: interior use of a product "
				"labelled for outdoor use (Critical); an occupied placement of a tamper-resistant-station "
				"product that does not record the stations (Warning); a removal sooner than the label's "
				"minimum bait days when the last check still found activity (Warning). Distance and "
				"burrow placement are printed on the task and not machine-checked."
			),
			"authored_by": "System",
		},
		{
			"rule_id": "pesticide_label_unavailable",
			"title": "A pesticide was applied with no label on file",
			"category": "Spray and Pesticides",
			"target_doctype": FARM_TASK,
			"requires_doctypes": FARM_TASK,
			"requires_fields": ["label_available", "label_snapshot"],
			"enabled": 0,
			"date_field": "completed_at",
			"date_field_role": "Timestamp",
			"due_date_mode": "None",
			"missing_date_behaviour": "Raise",
			"threshold_critical_days": -1,
			"threshold_warning_days": -1,
			"severity_expired": "Warning",
			"default_severity": "Warning",
			"scope_filters": [
				{"field": "state", "op": "eq", "value": COMPLETED},
				{"field": "label_available", "op": "isfalse"},
				{"field": "label_snapshot", "op": "isnotnull"},
			],
			"message_template": (
				"{{ row.name }} ({{ row.task_name }}) handled a product with no EPA label on file when "
				"the work was done. Attach the label (attach_epa_label, or 'Add label photos' on the "
				"product) so the next applicator has it in hand."
			),
			"regimes": ["WPS", "Internal"],
			"regulation_citations": "FIFRA §12(a)(2)(G) (use consistent with the label); 40 CFR 170.311 (labeling information available to handlers)",
			"retention_years": 3,
			"purpose": "The label is the law, and the applicator must be able to read it where the work is done.",
			"kairotic_gate_description": (
				"Fires on a COMPLETED task that handled a product (bait, a form's product, or its "
				"materials) whose EPA label PDF was not on file at completion. Informational: the "
				"completion itself is never refused."
			),
			"authored_by": "System",
		},
	]


# ── camp maintenance: the calendar and the audit packet ─────────────────────
def _check_rule_extra() -> dict:
	try:
		from . import compliance_rules

		name = compliance_rules.resolve("rodent_bait_check_overdue")
		if not name:
			return {}
		raw = frappe.db.get_value(compliance_rules.DOCTYPE, name, "extra_parameters_json")
		return compliance_rules.as_object(raw, "extra_parameters")
	except Exception:
		return {}


def camp_maintenance(company: str, today: str, housing_alerts: list) -> dict:
	"""§11. The calendar's camp maintenance block: housing alerts, open bait work, next checks."""
	open_tasks = []
	next_checks = []
	extra = _check_rule_extra()
	for location_doctype, location in bait_locations(company):
		for row in bait_tasks(location_doctype, location):
			if row.get("state") in TERMINAL_STATES:
				continue
			open_tasks.append(
				{
					"task": row["name"],
					"template": row.get("template"),
					"state": row.get("state"),
					"location_doctype": location_doctype,
					"location": location,
					"occupancy_at_creation": row.get("occupancy_at_creation") or None,
				}
			)
		due = next_check(location_doctype, location, today, extra)
		if due:
			next_checks.append(due)
	next_checks.sort(key=lambda row: row["due_date"])
	return {
		"alerts": housing_alerts,
		"open_bait_tasks": open_tasks,
		"next_bait_checks": next_checks,
		"note": (
			"Camp and housing maintenance: the Housing alerts (habitability, detectors, corrective "
			"actions, rodent bait) with the open rodent bait work and when each baited place is next "
			"due a station check."
		),
	}


def audit_rows(company: str, start: str, end: str) -> list:
	"""§11. The bait tasks completed or raised in a period, grouped by place."""
	out = []
	for location_doctype, location in bait_locations(company):
		tasks = []
		for row in bait_tasks(location_doctype, location):
			stamp = str(row.get("completed_at") or row.get("creation") or "")[:10]
			if stamp and ((start and stamp < start) or (end and stamp > end)):
				continue
			tasks.append(
				{
					"task": row["name"],
					"template": row.get("template"),
					"state": row.get("state"),
					"occupancy_at_creation": row.get("occupancy_at_creation") or None,
					"completed_at": str(row.get("completed_at") or "") or None,
					"bait_product": row.get("bait_product") or None,
					"bait_activity": row.get("bait_activity") or None,
				}
			)
		if tasks:
			out.append({"location_doctype": location_doctype, "location": location, "tasks": tasks})
	return out


# ── v0.204.0: the five templates, rebuilt on the form schema ────────────────
#: The v0.203.0 descriptions, so `patches/rebuild_rodent_templates` can tell a
#: template this app seeded (and nobody edited) from anybody else's — OML's own
#: drafts carry different words and are never touched.
V203_DESCRIPTIONS = {spec["template_name"]: spec["description"] for spec in SEED_TASK_TEMPLATES}

BUILDING_TYPES = ("Cabin", "House", "Housing Unit", "Storage", "Cold Storage")
_OCCUPIED = {"context": "occupancy_at_creation", "equals": OCCUPIED}


def _t(en: str, es: str) -> dict:
	return {"en": en, "es": es}


def _product(required: bool = True) -> dict:
	return {
		"key": "product",
		"type": "link",
		"label": _t("Product", "Producto"),
		"help": _t(
			"Scan or pick the bait. Its label opens from here.",
			"Escanea o elige el cebo. Su etiqueta se abre aquí.",
		),
		"required": required,
		"link": {"doctype": "Item", "filters": {"item_group": PEST_CONTROL_GROUP}},
	}


def _gloves() -> dict:
	return {
		"key": "ppe_gloves",
		"type": "attestation",
		"label": _t("PPE", "EPP"),
		"statement": _t(
			"I am wearing waterproof gloves and any other PPE the label requires.",
			"Llevo guantes impermeables y cualquier otro EPP que exija la etiqueta.",
		),
		"required": True,
	}


def _notice_posted() -> dict:
	return {
		"key": "notice_posted",
		"type": "attestation",
		"label": _t("Occupant notice", "Aviso a ocupantes"),
		"statement": _t(
			"The English/Spanish occupant notice is posted and occupants were told before placement.",
			"El aviso en inglés/español está colocado y se informó a los ocupantes antes de colocar el cebo.",
		),
		"required": True,
		"show_if": _OCCUPIED,
	}


def _stations(where_label: dict, interior: bool) -> dict:
	fields = [
		{
			"key": "photo",
			"type": "photo",
			"label": _t("Station, lid closed and locked", "Estación, tapa cerrada y con llave"),
			"min_count": 1,
			"max_count": 3,
			"required": True,
		},
		{"key": "where", "type": "text", "label": where_label, "required": True},
		{
			"key": "count",
			"type": "measurement",
			"label": _t("Bait placed in this station", "Cebo colocado en esta estación"),
			"uom": {"from_field": "product"},
			"min": 0,
			"required": True,
		},
	]
	if not interior:
		fields.insert(
			2,
			{
				"key": "distance_ft",
				"type": "number",
				"label": _t("Distance from the building (ft)", "Distancia al edificio (pies)"),
				"min": 0,
				"required": False,
			},
		)
	return {
		"key": "stations",
		"type": "group",
		"label": _t("Stations", "Estaciones"),
		"help": _t("One entry per station.", "Una entrada por estación."),
		"min_count": 1,
		"max_count": 50,
		"required": True,
		"fields": fields,
	}


def _locked_only() -> dict:
	return {
		"key": "tamper_resistant",
		"type": "attestation",
		"label": _t("Tamper-resistant stations", "Estaciones a prueba de manipulación"),
		"statement": _t(
			"Every placement is inside a locked tamper-resistant station; no loose pacs or blocks.",
			"Cada colocación está dentro de una estación con llave a prueba de manipulación; sin paquetes ni bloques sueltos.",
		),
		"required": True,
		"required_if": _OCCUPIED,
	}


_COMMON = {
	"enabled": 0,
	"task_type": "Pest Control",
	"applies_to_asset_types": list(BUILDING_TYPES),
	"regimes": ["OR-OSHA", "Internal"],
}

SEED_TASK_TEMPLATES_V204 = (
	{
		**_COMMON,
		"template_name": EXTERIOR,
		"title_es": "Colocación de cebo para roedores - Exterior",
		"description": "Rodent bait outside a housing unit or building, from Pest Control Products. Occupied: locked tamper-resistant stations only, occupant notice first.",
		"skill_required": "applicator",
		"required_certification": APPLICATOR_CERTIFICATION,
		"estimated_duration_minutes": 45,
		"dispatch_mode": "Dispatched",
		"default_urgency": "Normal",
		"evidence_required": {"signature": True, "gps": True},
		"creates_record": "Pest Control Application",
		"instructions": "Read the label before you start — the label is the law. Stay within the label's distance of the building. Keep bait away from vents, food and food-contact surfaces. No place pacs in burrows.",
		"instructions_es": "Lea la etiqueta antes de empezar: la etiqueta es la ley. Manténgase dentro de la distancia al edificio que indica la etiqueta. Mantenga el cebo lejos de ventilaciones, alimentos y superficies en contacto con alimentos. No ponga paquetes en madrigueras.",
		"form_schema": [
			_product(),
			_notice_posted(),
			_gloves(),
			_locked_only(),
			_stations(_t("Where (side of building, tag)", "Dónde (lado del edificio, etiqueta)"), False),
			{
				"key": "away_from_food",
				"type": "attestation",
				"label": _t("Placement", "Colocación"),
				"statement": _t(
					"Not in burrows (unless the label allows), not near vents, food or food-contact surfaces.",
					"No en madrigueras (salvo que la etiqueta lo permita), ni cerca de ventilaciones, alimentos o superficies en contacto con alimentos.",
				),
				"required": True,
			},
		],
	},
	{
		**_COMMON,
		"template_name": INTERIOR,
		"title_es": "Colocación de cebo para roedores - Interior",
		"description": "Rodent bait INSIDE a housing unit or building — the highest-exposure use. Farm Manager approval first; locked tamper-resistant stations only; nobody is assigned to the unit until a Removal and Clearance is completed.",
		"skill_required": "applicator",
		"required_certification": APPLICATOR_CERTIFICATION,
		"estimated_duration_minutes": 45,
		"dispatch_mode": "Dispatched",
		"default_urgency": "High",
		"evidence_required": {"signature": True, "gps": True},
		"creates_record": "Pest Control Application",
		"instructions": "Only with the Farm Manager's approval. Every placement in a locked station, out of reach of children and pets, never near vents, food, dishes or counters. Photograph each station with the room visible.",
		"instructions_es": "Solo con la aprobación del gerente. Cada colocación en una estación con llave, fuera del alcance de niños y mascotas, nunca cerca de ventilaciones, alimentos, platos o mostradores. Fotografíe cada estación con la habitación visible.",
		"form_schema": [
			{
				"key": "manager_approval",
				"type": "approval",
				"label": _t(
					"Farm Manager approval for interior bait", "Aprobación del gerente para cebo interior"
				),
				"role": "Farm Manager",
				"before_start": True,
				"required": True,
			},
			_product(),
			_notice_posted(),
			_gloves(),
			{
				**_locked_only(),
				"required": True,
			},
			_stations(_t("Room", "Habitación"), True),
			{
				"key": "out_of_reach",
				"type": "attestation",
				"label": _t("Placement", "Colocación"),
				"statement": _t(
					"Out of reach of children and pets; not near vents, food, dishes or food-contact surfaces.",
					"Fuera del alcance de niños y mascotas; no cerca de ventilaciones, alimentos, platos o superficies en contacto con alimentos.",
				),
				"required": True,
			},
		],
	},
	{
		**_COMMON,
		"template_name": CHECK,
		"title_es": "Revisión de cebo para roedores",
		"description": "Check every active bait station at one place. Every 7 days while active or in season; every 30 days off season for maintenance stations with no activity.",
		"skill_required": "camp_maintenance",
		"estimated_duration_minutes": 30,
		"dispatch_mode": "Dispatched",
		"default_urgency": "Normal",
		"evidence_required": {"gps": True},
		"creates_record": "",
		"instructions": "Visit every station, not a sample. Bag every carcass and dispose of it as the label says.",
		"instructions_es": "Visite todas las estaciones, no una muestra. Embolse cada cadáver y deséchelo como indica la etiqueta.",
		"form_schema": [
			_product(required=False),
			_gloves(),
			{
				"key": "stations",
				"type": "group",
				"label": _t("Stations checked", "Estaciones revisadas"),
				"min_count": 1,
				"max_count": 50,
				"required": True,
				"fields": [
					{
						"key": "photo",
						"type": "photo",
						"label": _t("Station, open then locked", "Estación, abierta y luego con llave"),
						"min_count": 1,
						"max_count": 3,
						"required": True,
					},
					{
						"key": "where",
						"type": "text",
						"label": _t("Station (room/side, tag)", "Estación (habitación/lado, etiqueta)"),
						"required": True,
					},
					{
						"key": "consumption",
						"type": "select",
						"label": _t("Bait eaten", "Cebo consumido"),
						"options": [
							{"value": "none", "label": _t("None", "Nada")},
							{"value": "partial", "label": _t("Some", "Algo")},
							{"value": "all", "label": _t("All", "Todo")},
						],
						"required": True,
					},
					{
						"key": "replenished",
						"type": "measurement",
						"label": _t("Bait added", "Cebo añadido"),
						"uom": {"from_field": "product"},
						"min": 0,
					},
					{
						"key": "intact",
						"type": "check",
						"label": _t(
							"Station intact, locked, not moved", "Estación intacta, con llave, sin mover"
						),
						"required": True,
					},
				],
			},
			{
				"key": "carcasses",
				"type": "number",
				"label": _t("Carcasses collected", "Cadáveres recogidos"),
				"min": 0,
				"required": True,
			},
			{
				"key": "rodent_activity_found",
				"type": "select",
				"label": _t(
					"Rodent activity found? (bait eaten, fresh signs of feeding, or carcasses)",
					"¿Actividad de roedores? (cebo consumido, señales frescas o cadáveres)",
				),
				"options": [
					{"value": "yes", "label": _t("Yes", "Sí")},
					{"value": "no", "label": _t("No", "No")},
				],
				"required": True,
			},
			{
				"key": "notice_still_posted",
				"type": "check",
				"label": _t("Occupant notice still posted", "El aviso a ocupantes sigue colocado"),
				"show_if": _OCCUPIED,
				"required_if": _OCCUPIED,
			},
		],
	},
	{
		**_COMMON,
		"template_name": REMOVAL,
		"title_es": "Retiro y limpieza de cebo para roedores",
		"description": "End a baiting round: every station removed, leftover bait and carcasses collected and disposed per label. For interior bait at a housing unit this is the PRE-OCCUPANCY CLEARANCE.",
		"skill_required": "applicator",
		"required_certification": APPLICATOR_CERTIFICATION,
		"estimated_duration_minutes": 45,
		"dispatch_mode": "Dispatched",
		"default_urgency": "High",
		"evidence_required": {"signature": True, "gps": True},
		"creates_record": "Pest Control Application",
		"instructions": "The number removed must match the number placed. Dispose of bait, carcasses and packaging as the label's Storage and Disposal section says.",
		"instructions_es": "El número retirado debe coincidir con el colocado. Deseche el cebo, los cadáveres y el empaque como indica la sección de Almacenamiento y Eliminación de la etiqueta.",
		"form_schema": [
			_product(required=False),
			_gloves(),
			{
				"key": "preoccupancy",
				"type": "select",
				"label": _t("Is this a pre-occupancy clearance?", "¿Es una limpieza antes de ocupar?"),
				"options": [
					{"value": "yes", "label": _t("Yes", "Sí")},
					{"value": "no", "label": _t("No", "No")},
				],
				"required": True,
			},
			{
				"key": "stations",
				"type": "group",
				"label": _t("Stations removed", "Estaciones retiradas"),
				"min_count": 1,
				"max_count": 50,
				"required": True,
				"fields": [
					{
						"key": "photo",
						"type": "photo",
						"label": _t("Former location, empty", "Ubicación anterior, vacía"),
						"min_count": 1,
						"max_count": 2,
						"required": True,
					},
					{
						"key": "where",
						"type": "text",
						"label": _t("Station (room/side, tag)", "Estación (habitación/lado, etiqueta)"),
						"required": True,
					},
					{
						"key": "count",
						"type": "measurement",
						"label": _t("Leftover bait collected", "Cebo sobrante recogido"),
						"uom": {"from_field": "product"},
						"min": 0,
					},
				],
			},
			{
				"key": "carcasses",
				"type": "number",
				"label": _t("Carcasses collected", "Cadáveres recogidos"),
				"min": 0,
				"required": True,
			},
			{
				"key": "residue_free",
				"type": "photo",
				"label": _t("Surfaces free of bait residue", "Superficies sin residuos de cebo"),
				"min_count": 1,
				"required_if": {"context": "bait_placement", "equals": "Interior"},
			},
			{
				"key": "disposed_per_label",
				"type": "attestation",
				"label": _t("Disposal", "Eliminación"),
				"statement": _t(
					"Leftover bait, carcasses and packaging were disposed of as the label says; sealed product returned to the chemical store.",
					"El cebo sobrante, los cadáveres y el empaque se desecharon según la etiqueta; el producto sellado volvió al almacén de químicos.",
				),
				"required": True,
			},
		],
	},
	{
		**_COMMON,
		"template_name": NOTICE,
		"title_es": "Aviso a ocupantes sobre cebo para roedores (EN/ES)",
		"description": "Post and hand out the English/Spanish notice BEFORE rodent bait is placed at an occupied unit or building. Placement cannot start until this is completed for the round.",
		"skill_required": "camp_maintenance",
		"estimated_duration_minutes": 15,
		"dispatch_mode": "Dispatched",
		"default_urgency": "High",
		"evidence_required": {"gps": True},
		"creates_record": "",
		"instructions": "Post the notice at every entrance and near each outside station group: bait in locked stations; do not touch them; keep children and pets away; report dead rodents; who to call; start and removal dates; product and EPA Reg. No.",
		"instructions_es": "Coloque el aviso en cada entrada y cerca de cada grupo de estaciones exteriores: cebo en estaciones con llave; no tocarlas; mantener alejados a niños y mascotas; reportar roedores muertos; a quién llamar; fechas de inicio y retiro; producto y No. de Reg. EPA.",
		"form_schema": [
			_product(),
			{
				"key": "notice_en",
				"type": "photo",
				"label": _t("English notice at each entrance", "Aviso en inglés en cada entrada"),
				"min_count": 1,
				"max_count": 10,
				"required": True,
			},
			{
				"key": "notice_es",
				"type": "photo",
				"label": _t("Spanish notice at each entrance", "Aviso en español en cada entrada"),
				"min_count": 1,
				"max_count": 10,
				"required": True,
			},
			{
				"key": "removal_date",
				"type": "date",
				"label": _t("Expected removal date on the notice", "Fecha de retiro prevista en el aviso"),
				"required": True,
			},
			{
				"key": "occupants_told",
				"type": "text",
				"label": _t(
					"Occupants told in person (names, or 'none present')",
					"Ocupantes informados en persona (nombres, o 'nadie presente')",
				),
				"required": True,
			},
		],
	},
)

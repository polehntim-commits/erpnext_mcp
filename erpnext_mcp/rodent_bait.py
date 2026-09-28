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

#: The program's settings and their defaults (ERPNext MCP Settings).
DEFAULTS = {
	"pest_applicator_skill": "applicator",
	"pest_applicator_certification": "Applicator License",
	"pest_require_applicator": 1,
	"pest_crew_skill": "camp_maintenance",
	"pest_alert_role": "Farm Manager",
	"pest_people_work_here_types": "Barn\nShop\nKitchen\nBath House\nToilet-Shower\nStorage\nCold Storage",
}

#: The season when a company names none: March through October.
DEFAULT_SEASON = ("03-01", "10-31")

#: `rodent_bait_check_overdue`'s extra_parameters when the rule carries none.
DEFAULT_INTERVALS = {
	"active_interval_days": 7,
	"knockdown_days": 10,
	"occupied": {"in_season_days": 7, "off_season_days": 30},
	"unoccupied": {"in_season_days": 7, "off_season_days": 30},
}


# ── settings ────────────────────────────────────────────────────────────────
def setting(fieldname: str):
	"""One program setting, its default when unset. Never raises."""
	try:
		from . import settings

		value = settings._value(fieldname)
	except Exception:
		value = None
	if value in (None, ""):
		return DEFAULTS.get(fieldname)
	return value


def people_work_here_types() -> set:
	raw = str(setting("pest_people_work_here_types") or "")
	return {line.strip().casefold() for line in raw.replace(",", "\n").splitlines() if line.strip()}


def applicator_skill() -> str:
	return str(setting("pest_applicator_skill") or "").strip()


# ── what a task is ──────────────────────────────────────────────────────────
def is_bait_template(template) -> bool:
	return str(template or "") in TEMPLATES


def placement_side(template) -> str:
	return PLACEMENT_SIDE.get(str(template or ""), "")


def _location_of(row) -> tuple:
	get = row.get
	return str(get("location_doctype") or ""), str(get("location") or "")


# ── occupancy ───────────────────────────────────────────────────────────────
def occupancy(location_doctype: str, location: str, on: str | None = None) -> dict:
	"""`{occupied, source, detail}` for one place. The first source that says yes wins.

	1. An active Housing Assignment on a Housing Unit.
	2. The place's own `occupied` flag — first-class, not a fallback.
	3. People work there: the `people_work_here` flag, or a type on the
	   people-work-here list (ERPNext MCP Settings).
	Otherwise unoccupied. Never raises: a place this cannot read is reported as
	OCCUPIED, the strict tier, because guessing empty is the unsafe direction.
	"""
	on = str(on or frappe.utils.today())[:10]
	if location_doctype not in LOCATION_DOCTYPES or not location:
		return {"occupied": False, "source": "none", "detail": "no housing unit or building named"}
	try:
		if not compat.doctype_exists(location_doctype) or not frappe.db.exists(location_doctype, location):
			return {"occupied": True, "source": "unreadable", "detail": f"{location} could not be read"}
		if location_doctype == HOUSING_UNIT:
			assignment = _active_assignment(location, on)
			if assignment:
				return {
					"occupied": True,
					"source": "housing_assignment",
					"detail": f"Housing Assignment {assignment} is current",
				}
		fields = compat.existing_fields(
			location_doctype,
			("name", "occupied", "people_work_here", "unit_type", "asset_type", "current_state"),
		)
		row = frappe.db.get_value(location_doctype, location, fields, as_dict=True) or {}
		if compat.checked(row.get("occupied")):
			return {"occupied": True, "source": "manual_flag", "detail": f"{location} is marked Occupied"}
		if _asset_state(row.get("current_state")) == "occupied":
			return {
				"occupied": True,
				"source": "manual_flag",
				"detail": f"{location}'s state is occupied (mark_occupied on the asset)",
			}
		if compat.checked(row.get("people_work_here")):
			return {"occupied": True, "source": "people_work_here", "detail": f"people work at {location}"}
		kind = str(row.get("unit_type") or row.get("asset_type") or "")
		if kind and kind.casefold() in people_work_here_types():
			return {
				"occupied": True,
				"source": "people_work_here",
				"detail": f"{location} is a {kind}, which counts as a place people work",
			}
	except Exception:
		return {"occupied": True, "source": "unreadable", "detail": f"{location} could not be read"}
	return {"occupied": False, "source": "none", "detail": f"nobody lives or works at {location}"}


def _asset_state(raw) -> str:
	try:
		value = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or {})
	except Exception:
		return ""
	return str(value.get("state") or "") if isinstance(value, dict) else ""


def _active_assignment(unit: str, on: str) -> str:
	if not compat.doctype_exists(HOUSING_ASSIGNMENT):
		return ""
	rows = frappe.db.get_all(
		HOUSING_ASSIGNMENT,
		filters={"unit": unit},
		fields=["name", "status", "assigned_date", "end_date"],
		limit=500,
	)
	for row in rows or []:
		if str(row.get("status") or "Current") != "Current":
			continue
		start = str(row.get("assigned_date") or "")[:10]
		end = str(row.get("end_date") or "")[:10]
		if start and start > on:
			continue
		if end and end < on:
			continue
		return str(row["name"])
	return ""


# ── the season ──────────────────────────────────────────────────────────────
_MMDD = re.compile(r"^(\d{1,2})-(\d{1,2})$")


def _mmdd(value, fallback: str) -> str:
	match = _MMDD.match(str(value or "").strip())
	if not match:
		return fallback
	month, day = int(match.group(1)), int(match.group(2))
	if not (1 <= month <= 12 and 1 <= day <= 31):
		return fallback
	return f"{month:02d}-{day:02d}"


def season_of(company: str) -> tuple:
	"""`(start, end)` as MM-DD for a company; March–October when unset."""
	start, end = DEFAULT_SEASON
	if company and compat.doctype_exists("Company"):
		fields = compat.existing_fields("Company", ("pest_season_start", "pest_season_end"))
		if fields:
			row = frappe.db.get_value("Company", company, fields, as_dict=True) or {}
			raw_start, raw_end = row.get("pest_season_start"), row.get("pest_season_end")
			if str(raw_start or "").strip() and str(raw_end or "").strip():
				start, end = _mmdd(raw_start, start), _mmdd(raw_end, end)
	return start, end


def in_season(company: str, on: str | None = None) -> bool:
	"""Is `on` inside the company's pest season? Inclusive; a window may wrap the year."""
	day = str(on or frappe.utils.today())[5:10]
	start, end = season_of(company)
	if start <= end:
		return start <= day <= end
	return day >= start or day <= end


def valid_mmdd(value) -> bool:
	return bool(_MMDD.match(str(value or "").strip())) and _mmdd(value, "") != ""


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


# ── who may do it ───────────────────────────────────────────────────────────
def needs_applicator(task: dict) -> bool:
	skill = applicator_skill()
	return bool(
		skill
		and is_bait_template(task.get("template"))
		and str(task.get("skill_required") or "").strip() == skill
		and compat.checked(setting("pest_require_applicator"))
	)


def applicator_qualification(employee: str) -> str:
	"""What qualifies this worker for applicator work, or '' when nothing does."""
	if not employee:
		return ""
	skill = applicator_skill()
	if compat.doctype_exists("Employee"):
		from .tools import fieldwork

		field = compat.first_field("Employee", *fieldwork._SKILL_FIELDS)
		if field:
			listed = str(frappe.db.get_value("Employee", employee, field) or "")
			tokens = {token.strip().casefold() for token in re.split(r"[,\n;]", listed) if token.strip()}
			if skill and skill.casefold() in tokens:
				return f"Employee.{field} lists {skill}"
	cert_type = str(setting("pest_applicator_certification") or "").strip()
	if not cert_type or not compat.doctype_exists("Certification"):
		return ""
	names = {employee.casefold()}
	if compat.doctype_exists("Employee"):
		full = str(frappe.db.get_value("Employee", employee, "employee_name") or "").strip()
		if full:
			names.add(full.casefold())
	today = frappe.utils.today()
	for row in frappe.db.get_all(
		"Certification",
		filters={"cert_type": cert_type},
		fields=["name", "holder", "status", "expiration_date"],
		limit=2000,
	):
		if str(row.get("holder") or "").strip().casefold() not in names:
			continue
		if str(row.get("status") or "Active") != "Active":
			continue
		expires = str(row.get("expiration_date") or "")[:10]
		if expires and expires < today:
			continue
		return f"Certification {row['name']} ({cert_type})"
	return ""


def refuse_unqualified(task: dict, employee: str, verb: str) -> None:
	"""Refuse applicator work for a worker without the qualification. §6."""
	if not needs_applicator(task) or not employee:
		return
	if applicator_qualification(employee):
		return
	cert_type = setting("pest_applicator_certification")
	raise ToolError(
		f"{task.get('name')} is rodent bait {'placement' if task.get('template') in PLACEMENT_SIDE else 'removal'} "
		f"and needs the applicator qualification, which {employee} does not have on record: no current "
		f"{cert_type!r} Certification held by them, and no {applicator_skill()!r} on their Employee "
		f"skills. Record the licence with create_certification, or send a licensed applicator. "
		f"Nothing was {verb}."
	)


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


# ── the next check ──────────────────────────────────────────────────────────
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
	"""When the next Rodent Bait Check is due at a place, and why. None when no bait is out. §7."""
	today = str(today or frappe.utils.today())[:10]
	rows = bait_tasks(location_doctype, location)
	since = round_start(location_doctype, location, rows)
	placements = [
		row
		for row in rows
		if row.get("template") in PLACEMENT_SIDE and _done_at(row) and _done_at(row) > since
	]
	if not placements:
		return None
	anchors = [
		row
		for row in rows
		if row.get("template") in (EXTERIOR, INTERIOR, CHECK) and _done_at(row) and _done_at(row) > since
	]
	anchor_row = max(anchors, key=_done_at)
	anchor = _done_at(anchor_row)
	placed = max(_done_at(row) for row in placements)
	settings = intervals(extra)
	company = company or str(anchor_row.get("company") or "")
	tier = OCCUPIED if occupancy(location_doctype, location, today)["occupied"] else UNOCCUPIED
	season = in_season(company, today)
	state = _state_of(location_doctype, location) or ACTIVE
	knockdown = _days_between(placed[:10], anchor[:10]) < int(settings["knockdown_days"])
	if knockdown or state == ACTIVE:
		interval, phase = int(settings["active_interval_days"]), ("knockdown" if knockdown else "active")
	else:
		band = settings["occupied" if tier == OCCUPIED else "unoccupied"]
		interval = int(band["in_season_days" if season else "off_season_days"])
		phase = "maintenance"
	due = (datetime.date.fromisoformat(anchor[:10]) + datetime.timedelta(days=interval)).isoformat()
	return {
		"location_doctype": location_doctype,
		"location": location,
		"anchor_task": str(anchor_row["name"]),
		"anchor": anchor,
		"latest_placement": placed,
		"tier": tier,
		"in_season": season,
		"phase": phase,
		"interval_days": interval,
		"due_date": due,
		"days_remaining": _days_between(today, due),
		"company": company,
	}


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


def scan_check_overdue(context: dict) -> list:
	"""`rodent_bait_check_overdue` (§7–§8): each place's next check, raised when due.

	Warning from the due date; Critical once more than `active_interval_days`
	overdue. Raised on the place's latest bait task, so a later completed Check
	or Removal moves or silences it by itself.
	"""
	from .alerts.base import SEVERITY_CRITICAL, SEVERITY_WARNING, Observation

	today = str(context.get("today") or frappe.utils.today())[:10]
	company = str(context.get("company") or "")
	extra = _extra_of(context)
	settings = intervals(extra)
	out = []
	for location_doctype, location in bait_locations(company):
		due = next_check(location_doctype, location, today, extra)
		if not due or due["days_remaining"] > 0:
			continue
		overdue = -int(due["days_remaining"])
		severity = SEVERITY_CRITICAL if overdue > int(settings["active_interval_days"]) else SEVERITY_WARNING
		why = {
			"knockdown": "a new placement is still in its knockdown window",
			"active": "the last check found activity (or bait is newly placed)",
			"maintenance": "a maintenance station with no activity",
		}[due["phase"]]
		out.append(
			Observation(
				source_doctype=FARM_TASK,
				source_docname=due["anchor_task"],
				message=(
					f"Rodent bait check {'due today' if overdue == 0 else f'{overdue} day(s) overdue'} at "
					f"{location} ({due['tier'].lower()}, {'in' if due['in_season'] else 'off'} season, "
					f"{why}): every {due['interval_days']} days, last bait task {due['anchor_task']} on "
					f"{due['anchor'][:10]}. Check every station — consumption, replenish, carcasses with "
					"waterproof gloves, stations locked — and record whether there was activity."
				),
				severity=severity,
				due_date=due["due_date"],
				company=due["company"],
				category="Housing",
			)
		)
	return out


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
	role = str(setting("pest_alert_role") or "Farm Manager")
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
			"builtin_scanner": "rodent_bait_check_overdue",
			"cadence_days": 7,
			# The Link only where the template exists: the rules seed before the
			# templates on a fresh site, and a dangling Link refuses the whole rule.
			**({"producer_task_template": CHECK} if template_exists(CHECK) else {}),
			"extra_parameters": {
				**json.loads(json.dumps(DEFAULT_INTERVALS)),
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

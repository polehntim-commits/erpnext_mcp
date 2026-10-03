# SPDX-License-Identifier: MIT
"""The camp: what buildings there are, and who is in them.

WHY THIS IS A REGISTER AND NOT A NOTE IN A SPREADSHEET. Employer-provided farm
housing sits at the intersection of three regimes that each want a different
fact about the same cabin, and none of them accept "we know who lives there" as
an answer:

  * IRS Section 119 lets lodging be excluded from wages when it is on the
    business premises, for the employer's convenience, and a condition of
    employment. The exclusion is defended with records of who was housed where
    and when — which is what a Housing Assignment is.
  * Oregon's ORS 653 and OAR 839-015 constrain deducting housing from wages.
    The question is asked of the *arrangement*, not the building, so the answer
    lives on the assignment rather than on the unit.
  * The FSMA Produce Safety Rule, Subpart L, sets sanitation requirements for
    worker facilities that touch produce contact areas. `fsma_worker_facility`
    is what surfaces which of fifty buildings those are.

None of these flags is a determination and this app does not give legal advice.
They record what somebody decided and when, so the decision can be defended or
revisited.

WHY OVERLAP IS REFUSED BY DEFAULT AND ALLOWED ON REQUEST. Two people in one
cabin on the same night is a data-entry mistake most of the time and the whole
point of a Multi-Unit Building the rest of the time. Refusing outright would
make the barracks unusable; allowing silently would let a typo become a bed
somebody does not have. So it refuses, names the assignment already there, and
takes `allow_multi_occupancy=true` from a caller who means it.

NOTHING HERE DELETES AN ASSIGNMENT. Ending one writes an end date. An assignment
removed when the person leaves cannot defend a Section 119 classification, cannot
answer a wage claim, and cannot tell an investigator who was in the camp the week
in question — and those are the three moments the record exists for.
"""

import frappe

from .. import compat, offline_create, rodent_bait
from ..args import as_bool, as_choice, as_date, as_float, as_int, as_limit, as_str, resolve_company
from ..erpnext_mcp.doctype.housing_assignment.housing_assignment import overlaps
from ..erpnext_mcp.doctype.housing_unit.housing_unit import (
	BARRACKS_CAPACITY,
	NON_RESIDENTIAL_TYPES,
	SQ_FT_PER_OCCUPANT,
	lawful_occupancy,
)
from ..errors import ToolError
from ..result import ToolResult
from .realestate import parcel_row

HOUSING_UNIT = "Housing Unit"
HOUSING_ASSIGNMENT = "Housing Assignment"
PARCEL = "Parcel"

#: The HR app that owns the Employee register. Optional: a camp roster that
#: cannot be written until an HR module is installed is a camp roster nobody
#: keeps, so an employee id is validated only where there is something to
#: validate it against.
HR_APPS = ("farm_hr", "hrms")
EMPLOYEE = "Employee"

#: How far past its last habitability inspection a unit is called overdue.
#: Annual is the cadence a camp is walked on; nothing in this app fires on it —
#: the number exists so `get_housing_capacity` can count, and the counting is
#: what makes somebody go and look.
INSPECTION_DAYS = 365

#: How far past its last detector test a worker-facility building is called
#: overdue. Annual, under OAR 437-004-1120 and the fire code the camp is
#: inspected against, and it matches `alerts.rules.DETECTOR_DAYS` for the same
#: reason INSPECTION_DAYS matches its counterpart: the camp register and the
#: compliance calendar must not disagree about which cabins are behind.
DETECTOR_DAYS = 365

REGISTER_CAP = 500

_UNIT_FIELDS = (
	"name",
	"unit_name",
	"unit_type",
	"parcel",
	"owning_entity",
	"square_footage",
	"capacity",
	"year_built",
	"condition",
	"related_asset",
	"access_card_zone",
	# v0.32.0. Where the building actually stands. Selected through
	# `compat.existing_fields`, so a site that has not migrated yet reads a unit
	# without them rather than failing on a column that is not there.
	"gps_latitude",
	"gps_longitude",
	"fsma_worker_facility",
	"or_housing_law_compliant",
	"max_occupants_per_or_law",
	"last_habitability_inspection",
	"smoke_detector_last_test",
	"co_detector_last_test",
	# v0.203.0. Occupancy and rodent bait (docs/design/rodent_bait_program.md §2, §7).
	"occupied",
	"people_work_here",
	"rodent_bait_state",
	"rodent_bait_state_since",
	"rodent_bait_last_placement",
	"notes",
	"creation",
	"owner",
)

_ASSIGNMENT_FIELDS = (
	"name",
	"unit",
	"employee",
	"employee_name",
	"parcel",
	"assigned_date",
	"end_date",
	"status",
	"deposit_paid",
	"deposit_returned",
	"housing_deduction_from_wages",
	"notes",
	"creation",
	"owner",
)


# ── shared ──────────────────────────────────────────────────────────────────
def _require(doctype: str) -> None:
	compat.require_doctype(
		doctype,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)


def _entity(args: dict, required: bool = False) -> str | None:
	requested = as_str(args, "owning_entity") or as_str(args, "company")
	return resolve_company(requested, required=required)


def _date_str(value) -> str | None:
	return str(value or "") or None


def hr_installed() -> bool:
	"""Is there an Employee register on this site worth validating against?

	Both the app and the doctype, because an app can be installed with its
	doctypes half migrated and a query against a table that is not there is a
	SQL error rather than an empty result.
	"""
	try:
		installed = set(frappe.get_installed_apps() or [])
	except Exception:
		return False
	return bool(installed & set(HR_APPS)) and compat.doctype_exists(EMPLOYEE)


def unit_row(unit: str, company: str = "") -> dict:
	"""One Housing Unit as a dict, from its docname or its bare unit name."""
	unit = (unit or "").strip()
	if not unit:
		raise ToolError("unit is required (a Housing Unit docname, or the unit name such as 'MC-Cabin-01')")
	fields = compat.existing_fields(HOUSING_UNIT, _UNIT_FIELDS)

	if frappe.db.exists(HOUSING_UNIT, unit):
		row = dict(frappe.db.get_value(HOUSING_UNIT, unit, fields, as_dict=True) or {})
		if company and row.get("owning_entity") and row["owning_entity"] != company:
			raise ToolError(f"Housing Unit {unit!r} belongs to {row['owning_entity']!r}, not {company!r}")
		return row

	filters = {"unit_name": unit}
	if company:
		filters["owning_entity"] = company
	matches = frappe.db.get_all(HOUSING_UNIT, filters=filters, fields=fields, limit=25)
	if len(matches) == 1:
		return dict(matches[0])
	if len(matches) > 1:
		names = ", ".join(sorted(str(match.get("name")) for match in matches))
		raise ToolError(
			f"{unit!r} matches {len(matches)} units: {names}. Pass the docname, or set parcel to narrow it."
		)
	raise ToolError(f"no Housing Unit called {unit!r}. list_housing_units has the register.")


def _describe_unit(row: dict, today: str = "") -> dict:
	square_footage = round(float(row.get("square_footage") or 0), 2)
	lawful = int(row.get("max_occupants_per_or_law") or 0)
	capacity = int(row.get("capacity") or 0)
	inspection = _date_str(row.get("last_habitability_inspection"))
	smoke = _date_str(row.get("smoke_detector_last_test"))
	co = _date_str(row.get("co_detector_last_test"))
	# The three conditions `housing_detector_test_stale` scopes on, read here so
	# the register and the calendar name the same set of buildings.
	required = (
		compat.checked(row.get("fsma_worker_facility"))
		and (row.get("unit_type") or "") not in NON_RESIDENTIAL_TYPES
		and (row.get("condition") or "") != "Uninhabitable"
	)
	return {
		"name": row.get("name"),
		"unit_name": row.get("unit_name"),
		"unit_type": row.get("unit_type") or None,
		"parcel": row.get("parcel"),
		"owning_entity": row.get("owning_entity") or None,
		"square_footage": square_footage,
		"capacity": capacity or None,
		"year_built": int(row.get("year_built") or 0) or None,
		"condition": row.get("condition") or None,
		"related_asset": row.get("related_asset") or None,
		"access_card_zone": row.get("access_card_zone") or None,
		# NEVER `{lat: 0, lon: 0}`. Null island is a real place and an unset
		# coordinate looks exactly like it, so an unmapped cabin reports nothing
		# rather than a point in the Gulf of Guinea a map would fly to.
		"gps": _gps(row),
		"residential": (row.get("unit_type") or "") not in NON_RESIDENTIAL_TYPES,
		"fsma_worker_facility": compat.checked(row.get("fsma_worker_facility")),
		"or_housing_law_compliant": row.get("or_housing_law_compliant") or "Unknown",
		"max_occupants_per_or_law": lawful or None,
		"capacity_over_lawful_occupancy": bool(lawful and capacity and capacity > lawful),
		"last_habitability_inspection": inspection,
		"inspection_overdue": _overdue(inspection, today),
		"smoke_detector_last_test": smoke,
		"co_detector_last_test": co,
		# SCOPED THE SAME WAY `housing_detector_test_stale` IS, and reporting None
		# rather than False when the unit is out of scope. A shed on the parcel is
		# not a bunkhouse and is never asked for a detector test; answering False
		# would read as "tested and fine", which is the one wrong answer — the
		# same reason the safety rates come back None rather than 0.0 when nobody
		# supplied the hours.
		"detectors_required": required,
		"smoke_detector_overdue": _stale(smoke, today, DETECTOR_DAYS) if required else None,
		"co_detector_overdue": _stale(co, today, DETECTOR_DAYS) if required else None,
		"detector_test_overdue": (
			(_stale(smoke, today, DETECTOR_DAYS) or _stale(co, today, DETECTOR_DAYS)) if required else None
		),
		"detectors_overdue": (
			[label for label, value in (("smoke", smoke), ("CO", co)) if _stale(value, today, DETECTOR_DAYS)]
			if required
			else []
		),
		# v0.203.0. The manual occupancy flags, and what rodent bait is doing here.
		"occupied": compat.checked(row.get("occupied")),
		"people_work_here": compat.checked(row.get("people_work_here")),
		"rodent_bait_state": row.get("rodent_bait_state") or None,
		"rodent_bait_state_since": str(row.get("rodent_bait_state_since") or "") or None,
		"notes": row.get("notes") or None,
	}


def _gps(row: dict) -> dict | None:
	"""`{lat, lon}` for a unit somebody has located, or None."""
	latitude = row.get("gps_latitude")
	longitude = row.get("gps_longitude")
	if latitude in (None, "") or longitude in (None, ""):
		return None
	if not float(latitude) and not float(longitude):
		return None
	return {"lat": round(float(latitude), 7), "lon": round(float(longitude), 7)}


def check_coordinates(latitude, longitude) -> tuple:
	"""A lat/lon pair, checked to be on Earth, or a ToolError saying which is wrong.

	BOTH OR NEITHER. Half a coordinate is not a position, and a unit carrying a
	latitude and no longitude would sit on a map at longitude zero — off the coast
	of Ghana, on the same meridian as everything else anybody forgot to finish.
	"""
	given = [value for value in (latitude, longitude) if value not in (None, "")]
	if not given:
		return None, None
	if len(given) == 1:
		raise ToolError(
			"gps_latitude and gps_longitude have to be set together. Half a coordinate is not a "
			"position — a unit with a latitude and no longitude sits on a map off the coast of "
			"Ghana. Nothing was changed."
		)
	latitude = as_float(latitude, "gps_latitude")
	longitude = as_float(longitude, "gps_longitude")
	if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
		raise ToolError(
			f"[{longitude}, {latitude}] is not a point on Earth. Latitude runs -90 to 90 and "
			"longitude -180 to 180 — a latitude past 90 is almost always the pair the wrong way "
			"round, which is the one version of this mistake a computer can catch. Nothing was "
			"changed."
		)
	return latitude, longitude


def _stale(anchor: str | None, today: str, days: int) -> bool:
	"""Is `anchor` missing, or older than `days`?

	NEVER COUNTS AS STALE, which is the whole point and the opposite of what a
	naive date comparison does. A cabin nobody has ever inspected is the most
	overdue cabin in the camp, and "no date, no flag" hides exactly the unit
	somebody needs to walk. `alerts/rules.py` makes the same argument at length
	for `missing_date_behaviour = Raise`.
	"""
	if not anchor:
		return True
	try:
		return frappe.utils.date_diff(today or frappe.utils.today(), anchor) > days
	except Exception:  # pragma: no cover - an unparseable stored date
		return True


def _overdue(inspection: str | None, today: str = "") -> bool:
	"""Has it been more than a year, or has nobody ever looked?

	Never inspected counts as overdue for a residential unit, which is the
	answer that gets somebody to go and look. A calendar tick is not what makes
	this true — the state is: a unit either has a recent inspection or it does
	not, and this reports which.
	"""
	today = today or frappe.utils.today()
	if not inspection:
		return True
	try:
		return frappe.utils.date_diff(today, inspection) > INSPECTION_DAYS
	except Exception:  # pragma: no cover - an unparseable stored date
		return True


def _describe_assignment(row: dict) -> dict:
	paid = round(float(row.get("deposit_paid") or 0), 2)
	returned = round(float(row.get("deposit_returned") or 0), 2)
	return {
		"name": row.get("name"),
		"unit": row.get("unit"),
		"employee": row.get("employee") or None,
		"employee_name": row.get("employee_name") or row.get("employee") or None,
		"parcel": row.get("parcel") or None,
		"assigned_date": _date_str(row.get("assigned_date")),
		"end_date": _date_str(row.get("end_date")),
		"current": not row.get("end_date"),
		"status": row.get("status") or ("Ended" if row.get("end_date") else "Current"),
		"deposit_paid": paid,
		"deposit_returned": returned,
		"deposit_outstanding": round(paid - returned, 2),
		"housing_deduction_from_wages": row.get("housing_deduction_from_wages") or "Unknown",
		"notes": row.get("notes") or None,
	}


def _current_assignments(units) -> dict:
	"""Every open assignment for these units, keyed by unit."""
	if not units:
		return {}
	rows = frappe.db.get_all(
		HOUSING_ASSIGNMENT,
		filters={"unit": ("in", sorted(units)), "end_date": ("is", "not set")},
		fields=compat.existing_fields(HOUSING_ASSIGNMENT, _ASSIGNMENT_FIELDS),
		limit=REGISTER_CAP * 4,
	)
	out: dict = {}
	for row in rows or []:
		out.setdefault(row.get("unit"), []).append(_describe_assignment(dict(row)))
	return out


# ── 111. list_housing_units ─────────────────────────────────────────────────
def list_housing_units(args: dict) -> ToolResult:
	"""The camp register: every unit, its capacity, its condition and who is in it."""
	_require(HOUSING_UNIT)
	company = _entity(args)
	limit = as_limit(args)

	filters = {}
	if company:
		filters["owning_entity"] = company
	parcel = as_str(args, "parcel")
	if parcel:
		filters["parcel"] = str(parcel_row(parcel, company or "")["name"])
	for key in ("unit_type", "condition", "or_housing_law_compliant"):
		value = as_str(args, key)
		if value:
			filters[key] = value
	if "fsma_worker_facility" in args:
		filters["fsma_worker_facility"] = 1 if as_bool(args, "fsma_worker_facility") else 0

	today = frappe.utils.today()
	rows = frappe.db.get_all(
		HOUSING_UNIT,
		filters=filters,
		fields=compat.existing_fields(HOUSING_UNIT, _UNIT_FIELDS),
		order_by="parcel asc, unit_name asc",
		limit=min(limit, REGISTER_CAP),
	)
	units = [_describe_unit(dict(row), today) for row in rows]
	occupied = _current_assignments([unit["name"] for unit in units])

	for unit in units:
		unit["currently_assigned"] = len(occupied.get(unit["name"], []))
		unit["occupants"] = [entry["employee_name"] for entry in occupied.get(unit["name"], [])]

	residential = [unit for unit in units if unit["residential"]]
	by_type: dict = {}
	for unit in units:
		by_type[unit["unit_type"] or "(unrecorded)"] = by_type.get(unit["unit_type"] or "(unrecorded)", 0) + 1

	capacity = sum(unit["capacity"] or 0 for unit in residential)
	assigned = sum(unit["currently_assigned"] for unit in residential)

	return ToolResult(
		data={
			"company": company,
			"unit_count": len(units),
			"residential_unit_count": len(residential),
			"total_capacity": capacity,
			"currently_assigned": assigned,
			"open_beds": max(0, capacity - assigned),
			"by_unit_type": dict(sorted(by_type.items())),
			"overdue_inspections": [unit["name"] for unit in residential if unit["inspection_overdue"]],
			# The register used to name the habitability backlog and stay silent
			# about the detector backlog, while the calendar raised warnings for
			# both. A camp manager reading this to plan the morning walked the
			# cabins it listed and left the detector warnings open.
			"overdue_detector_tests": [unit["name"] for unit in units if unit["detector_test_overdue"]],
			"uninhabitable": [unit["name"] for unit in units if unit["condition"] == "Uninhabitable"],
			"fsma_worker_facilities": [unit["name"] for unit in units if unit["fsma_worker_facility"]],
			"over_lawful_occupancy": [
				unit["name"] for unit in units if unit["capacity_over_lawful_occupancy"]
			],
			"units": units,
		},
		summary=(
			f"{len(units)} unit(s), capacity {capacity}, {assigned} assigned, "
			f"{max(0, capacity - assigned)} open"
		),
	)


# ── 112. get_housing_unit ───────────────────────────────────────────────────
def get_housing_unit(args: dict) -> ToolResult:
	"""One unit in full, with everyone who has ever been assigned to it."""
	_require(HOUSING_UNIT)
	company = _entity(args)
	row = unit_row(as_str(args, "unit", required=True), company or "")
	described = _describe_unit(row, frappe.utils.today())

	history = [
		_describe_assignment(dict(entry))
		for entry in frappe.db.get_all(
			HOUSING_ASSIGNMENT,
			filters={"unit": row["name"]},
			fields=compat.existing_fields(HOUSING_ASSIGNMENT, _ASSIGNMENT_FIELDS),
			order_by="assigned_date desc",
			limit=REGISTER_CAP,
		)
	]
	current = [entry for entry in history if entry["current"]]

	return ToolResult(
		data={
			**described,
			"currently_assigned": len(current),
			"open_beds": max(0, (described["capacity"] or 0) - len(current)),
			"assignment_count": len(history),
			"current_assignments": current,
			"assignment_history": history,
			"compliance_notes": _unit_notes(described),
		},
		summary=(
			f"{row['name']}: {described['unit_type'] or 'type unrecorded'}, capacity "
			f"{described['capacity'] or 0}, {len(current)} assigned"
		),
	)


def _unit_notes(unit: dict) -> list:
	"""What is worth saying about a unit without refusing to record it."""
	out = []
	if unit["capacity_over_lawful_occupancy"]:
		out.append(
			f"Capacity {unit['capacity']} exceeds the {unit['max_occupants_per_or_law']} the floor "
			f"area allows at {SQ_FT_PER_OCCUPANT} sq ft per occupant. Either the square footage is "
			"understated or the unit is over-filled."
		)
	if unit["residential"] and unit["inspection_overdue"]:
		out.append(
			"No habitability inspection on record in the last year."
			if unit["last_habitability_inspection"]
			else "No habitability inspection has ever been recorded for this unit."
		)
	if unit["residential"] and not unit["smoke_detector_last_test"]:
		out.append("No smoke detector test on record.")
	if unit["residential"] and not unit["co_detector_last_test"]:
		out.append(
			"No CO detector test on record. Required wherever there is a fuel-burning appliance, "
			"which on a camp cabin usually means a propane heater."
		)
	if unit["condition"] == "Uninhabitable":
		out.append("Marked Uninhabitable. Assignments into it are refused.")
	if unit["fsma_worker_facility"]:
		out.append(
			"Subject to FSMA Produce Safety Rule Subpart L worker facility requirements — "
			"sanitation, water and supplies have to be maintained and the maintenance recorded."
		)
	return out


# ── 113. create_housing_unit ────────────────────────────────────────────────
def create_housing_unit(args: dict) -> ToolResult:
	"""Register one building on a camp."""
	_require(HOUSING_UNIT)
	_require(PARCEL)
	company = _entity(args)
	parcel = str(parcel_row(as_str(args, "parcel", required=True), company or "")["name"])
	unit_name = as_str(args, "unit_name", required=True)

	# v0.226.0. Made on a phone with no signal — see `offline_create`. A resend
	# answers with what the first send made; a cabin of the same name on the same
	# parcel goes to a person (It's the same one), never merged here.
	offline = offline_create.options(args)
	if offline["active"]:
		offline_create.require_ready(HOUSING_UNIT)
		done = offline_create.replayed(HOUSING_UNIT, offline)
		if done:
			data = _describe_unit(dict(frappe.get_doc(HOUSING_UNIT, done).as_dict()), frappe.utils.today())
			data.update(
				{
					"created": False,
					"outcome": "replayed",
					"replayed": True,
					"tag_uuid": offline["tag_uuid"] or None,
				}
			)
			return ToolResult(
				data=data, summary=f"{done} was already registered by this request; nothing new written"
			)
		if offline["link_to_existing"]:
			data = offline_create.link(HOUSING_UNIT, offline["link_to_existing"], offline)
			return ToolResult(data=data, summary=f"tag {offline['tag_uuid']} linked to {data['name']}")
		offline_create.claim_tag(offline)

	existing = frappe.db.get_value(HOUSING_UNIT, {"unit_name": unit_name, "parcel": parcel}, "name")
	if existing and offline["active"] and not offline["confirm_new"]:
		row = _describe_unit(dict(frappe.get_doc(HOUSING_UNIT, existing).as_dict()), frappe.utils.today())
		found = offline_create.candidates(HOUSING_UNIT, [{**row, "why": "same name on the same parcel"}])
		# The docname is built from the parcel and the name, so "keep as new" needs a new name.
		found["can_keep_new"] = False
		return ToolResult(
			data=found, summary=f"{unit_name}: possible duplicate of {existing}; nothing created"
		)
	if existing:
		raise ToolError(
			f"{parcel} already has a unit called {unit_name!r} ({existing}). Every camp numbers "
			"its cabins from one, which is why the parcel is part of the docname and the name "
			"has to be unique inside it. Nothing was created.",
			"error.asset.name_taken" if offline["active"] else "",
		)

	doc = frappe.new_doc(HOUSING_UNIT)
	doc.unit_name = unit_name
	doc.parcel = parcel
	doc.square_footage = as_float(args.get("square_footage"), "square_footage")
	doc.capacity = as_int(args, "capacity") or 0
	doc.year_built = as_int(args, "year_built")
	doc.access_card_zone = as_str(args, "access_card_zone")
	doc.notes = as_str(args, "notes")
	doc.max_occupants_per_or_law = as_int(args, "max_occupants_per_or_law") or 0

	unit_type = as_str(args, "unit_type")
	if unit_type:
		doc.unit_type = as_choice(HOUSING_UNIT, "unit_type", unit_type, "unit_type")
	condition = as_str(args, "condition")
	if condition:
		doc.condition = as_choice(HOUSING_UNIT, "condition", condition, "condition")
	compliant = as_str(args, "or_housing_law_compliant")
	if compliant:
		doc.or_housing_law_compliant = as_choice(
			HOUSING_UNIT, "or_housing_law_compliant", compliant, "or_housing_law_compliant"
		)

	asset = as_str(args, "related_asset")
	if asset:
		# The parcel's entity, not `doc.owning_entity`: that field is filled in by
		# the controller during `validate`, which has not run yet — reading it here
		# gave "" and made the cross-company check silently pass everything.
		entity = frappe.db.get_value(PARCEL, parcel, "owning_entity") or company or ""
		doc.related_asset = _check_asset(asset, entity, parcel)

	latitude, longitude = check_coordinates(args.get("gps_latitude"), args.get("gps_longitude"))
	if latitude is not None:
		doc.gps_latitude = latitude
		doc.gps_longitude = longitude

	for key in ("last_habitability_inspection", "smoke_detector_last_test", "co_detector_last_test"):
		doc.set(key, as_date(args, key))
	fsma = as_bool(args, "fsma_worker_facility")
	if fsma is not None:
		doc.fsma_worker_facility = 1 if fsma else 0
	for key in ("occupied", "people_work_here"):
		flag = as_bool(args, key)
		if flag is not None and compat.has_field(HOUSING_UNIT, key):
			doc.set(key, 1 if flag else 0)

	offline_create.stamp(doc, HOUSING_UNIT, offline)
	doc.insert(ignore_permissions=True)
	offline_create.record(HOUSING_UNIT, doc.name, offline)
	described = _describe_unit(dict(doc.as_dict()), frappe.utils.today())

	warnings = list(_unit_notes(described))
	if (
		described["capacity"]
		and described["capacity"] > BARRACKS_CAPACITY
		and described["unit_type"] != "Multi-Unit Building"
	):
		warnings.insert(
			0,
			f"Capacity {described['capacity']} in a unit typed "
			f"{described['unit_type'] or '(unrecorded)'}. Anything over {BARRACKS_CAPACITY} is "
			"barracks by another name — some really are, so this is recorded rather than refused, "
			"but Multi-Unit Building is probably the type you want.",
		)
	if not described["square_footage"]:
		warnings.append(
			"No square footage recorded, so the lawful occupancy cannot be computed and the unit "
			"has no defensible occupancy limit."
		)

	data = {
		**described,
		"lawful_occupancy_basis": (
			f"{SQ_FT_PER_OCCUPANT} sq ft of sleeping area per occupant "
			"(29 CFR 1910.142(b)(1), which Oregon's agricultural labor housing rules follow)"
		),
	}
	if warnings:
		data["warnings"] = warnings
	if offline["active"]:
		data.update(
			{
				"created": True,
				"outcome": "created",
				"replayed": False,
				"tag_uuid": offline["tag_uuid"] or None,
			}
		)
	return ToolResult(
		data=data,
		summary=(
			f"registered housing unit {doc.name} "
			f"({described['unit_type'] or 'type unrecorded'}, capacity {described['capacity'] or 0})"
		),
		docstatus_delta="none → 0 (created)",
	)


def _check_asset(asset: str, company: str, parcel: str) -> str:
	if not frappe.db.exists("Asset", asset):
		raise ToolError(
			f"no Asset named {asset!r} on this site. Assets are created with create_asset. "
			"Nothing was created."
		)
	owner = frappe.db.get_value("Asset", asset, "company")
	if company and owner and owner != company:
		raise ToolError(
			f"Asset {asset!r} is on {owner!r}'s books and {parcel} is on {company!r}'s. A building "
			"and the asset carrying it have to be on the same books. Nothing was created."
		)
	claimed = frappe.db.get_value(HOUSING_UNIT, {"related_asset": asset}, "name")
	if claimed:
		raise ToolError(
			f"Asset {asset!r} already carries housing unit {claimed!r}. One asset, one building — "
			"if it really covers both, split the cost first. Nothing was created."
		)
	return asset


# ── 114. update_housing_unit ────────────────────────────────────────────────
def update_housing_unit(args: dict) -> ToolResult:
	"""Change a registered unit. Cannot re-key it and cannot move it to another parcel."""
	_require(HOUSING_UNIT)
	company = _entity(args)
	row = unit_row(as_str(args, "unit", required=True), company or "")

	if as_str(args, "unit_name"):
		raise ToolError(
			"unit_name cannot be changed: the docname is built from it and every assignment "
			"points at that docname. Nothing was changed."
		)
	if as_str(args, "parcel"):
		raise ToolError(
			"a building cannot move between parcels. If it really was moved — and manufactured "
			"homes are — retire this record and register it where it stands, so the assignment "
			"history stays attached to the ground it happened on. Nothing was changed."
		)

	doc = frappe.get_doc(HOUSING_UNIT, row["name"])
	changes = {}

	for key in ("access_card_zone", "notes"):
		if key in args:
			_stage(changes, doc, key, as_str(args, key))
	if "square_footage" in args:
		_stage(changes, doc, "square_footage", as_float(args.get("square_footage"), "square_footage"))
	for key in ("capacity", "year_built", "max_occupants_per_or_law"):
		if key in args:
			_stage(changes, doc, key, as_int(args, key) or 0)
	for key in ("unit_type", "condition", "or_housing_law_compliant"):
		if key in args:
			value = as_str(args, key)
			_stage(changes, doc, key, as_choice(HOUSING_UNIT, key, value, key) if value else "")
	for key in ("last_habitability_inspection", "smoke_detector_last_test", "co_detector_last_test"):
		if key in args:
			_stage(changes, doc, key, as_date(args, key) or "")
	if "fsma_worker_facility" in args:
		_stage(changes, doc, "fsma_worker_facility", 1 if as_bool(args, "fsma_worker_facility") else 0)
	# v0.203.0. Occupied at takeover with no assignment behind it (Mill Creek), or
	# a building people work in: the rodent bait program's strict tier.
	for key in ("occupied", "people_work_here"):
		if key in args and compat.has_field(HOUSING_UNIT, key):
			_stage(changes, doc, key, 1 if as_bool(args, key) else 0)

	# v0.32.0. THE PAIR MOVES TOGETHER OR NOT AT ALL. Accepting one on its own
	# would let a caller correct a longitude and leave the old latitude behind,
	# which produces a coordinate that is somewhere — just not anywhere either
	# reading of the record meant. `check_coordinates` refuses the half-call; the
	# two-field carry below is what makes the other half available to it when only
	# one was passed.
	if "gps_latitude" in args or "gps_longitude" in args:
		latitude, longitude = check_coordinates(
			args.get("gps_latitude", row.get("gps_latitude")),
			args.get("gps_longitude", row.get("gps_longitude")),
		)
		_stage(changes, doc, "gps_latitude", latitude)
		_stage(changes, doc, "gps_longitude", longitude)
	if "related_asset" in args:
		asset = as_str(args, "related_asset")
		if asset and asset != row.get("related_asset"):
			asset = _check_asset(asset, row.get("owning_entity") or company or "", row["parcel"])
		_stage(changes, doc, "related_asset", asset)

	if not changes:
		raise ToolError(
			"nothing to change. Pass at least one of: unit_type, square_footage, capacity, "
			"year_built, condition, related_asset, access_card_zone, gps_latitude, gps_longitude, "
			"fsma_worker_facility, or_housing_law_compliant, max_occupants_per_or_law, "
			"last_habitability_inspection, smoke_detector_last_test, co_detector_last_test, "
			"occupied, people_work_here, notes."
		)

	# The controller only fills in a blank lawful occupancy, so a square footage
	# that changes leaves a stale computed limit behind unless the caller also
	# changed it. Clearing it hands the recomputation back to the controller and
	# keeps a number somebody typed themselves.
	if "square_footage" in changes and "max_occupants_per_or_law" not in changes:
		previous = int(row.get("max_occupants_per_or_law") or 0)
		if previous == lawful_occupancy(row.get("square_footage")):
			doc.max_occupants_per_or_law = 0
			changes["max_occupants_per_or_law"] = [previous or None, None]

	doc.save(ignore_permissions=True)
	described = _describe_unit(dict(doc.as_dict()), frappe.utils.today())
	if "max_occupants_per_or_law" in changes:
		changes["max_occupants_per_or_law"][1] = described["max_occupants_per_or_law"]

	return ToolResult(
		data={
			**described,
			"changed": {key: [before, after] for key, (before, after) in changes.items()},
			"compliance_notes": _unit_notes(described),
		},
		summary=f"{doc.name}: {len(changes)} field(s) changed",
		docstatus_delta="0 → 0 (updated)",
	)


def _stage(changes: dict, doc, field: str, wanted) -> None:
	"""Set one field, and record before → after only when it actually moved."""
	before = doc.get(field)
	before = "" if before is None else before
	if str(before) == str(wanted):
		return
	changes[field] = [before or None, wanted or None]
	doc.set(field, wanted if wanted != "" else None)


# ── 115. list_housing_assignments ───────────────────────────────────────────
def list_housing_assignments(args: dict) -> ToolResult:
	"""Who is housed where, currently or over a period."""
	_require(HOUSING_ASSIGNMENT)
	company = _entity(args)
	limit = as_limit(args)

	filters = {}
	unit = as_str(args, "unit")
	if unit:
		filters["unit"] = unit_row(unit, company or "")["name"]
	parcel = as_str(args, "parcel")
	if parcel:
		filters["parcel"] = str(parcel_row(parcel, company or "")["name"])
	employee = as_str(args, "employee")
	if employee:
		filters["employee"] = employee

	current_only = as_bool(args, "current_only", True)
	if current_only:
		filters["end_date"] = ("is", "not set")
	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	if from_date and not current_only:
		filters["assigned_date"] = (">=", from_date)
	if to_date and not current_only:
		filters["assigned_date"] = ("<=", to_date) if not from_date else ("between", (from_date, to_date))

	rows = frappe.db.get_all(
		HOUSING_ASSIGNMENT,
		filters=filters,
		fields=compat.existing_fields(HOUSING_ASSIGNMENT, _ASSIGNMENT_FIELDS),
		order_by="assigned_date desc",
		limit=min(limit, REGISTER_CAP),
	)
	assignments = [_describe_assignment(dict(row)) for row in rows]
	deducted = [entry for entry in assignments if entry["housing_deduction_from_wages"] == "Yes"]

	return ToolResult(
		data={
			"company": company,
			"current_only": bool(current_only),
			"assignment_count": len(assignments),
			"currently_assigned": len([entry for entry in assignments if entry["current"]]),
			"distinct_units": len({entry["unit"] for entry in assignments}),
			"distinct_people": len(
				{
					entry["employee"] or entry["employee_name"]
					for entry in assignments
					if entry["employee_name"]
				}
			),
			"with_wage_deduction": [entry["name"] for entry in deducted],
			"deposits_outstanding": round(
				sum(entry["deposit_outstanding"] for entry in assignments if entry["current"]), 2
			),
			"assignments": assignments,
		},
		summary=(
			f"{len(assignments)} assignment(s), "
			f"{len([entry for entry in assignments if entry['current']])} current"
		),
	)


# ── 116. create_housing_assignment ──────────────────────────────────────────


def _company_housing_deduction(unit: dict) -> str:
	"""This entity's standing answer to "is rent taken out of wages", or "".

	v0.94.0, AND THE POINT IS THAT THE ANSWER IS WRITTEN ONTO THE ROW. A housing
	deduction is a wage deduction, so it is not a question a foreman should be
	answering per bunk — and on this farm the answer is "No" every time, because
	no rent is charged for labor camps at all. Asking it forty times a season
	produced a column that said "Unknown" wherever somebody skipped it, which is
	a disclosure ORS 653 / OAR 839-015 require and nobody made.

	RESOLVED AT CREATION, NEVER AT READ TIME, AND THAT IS THE WHOLE TRAP. The
	audit packet and the camp register both read the per-assignment column
	directly; a default resolved when a report runs would leave every row created
	after this release reporting "Unknown" to an auditor while looking correct in
	the app. So it is read here, once, and stored — the row remains the disclosure.

	AN EXPLICIT ARGUMENT STILL WINS, because one arrangement genuinely can differ
	from the entity's norm and the Housing Unit doctype's own help text is right
	that the question belongs to the arrangement. This supplies an answer where
	the caller did not; it never overrides one.

	A SITE WITHOUT THE FIELD GETS "" AND THE OLD BEHAVIOUR. The column is a Custom
	Field installed by `compliance_fields`, so a site mid-upgrade, a site without
	ERPNext's Company doctype, or a Housing Unit whose owning entity is blank all
	fall through to exactly what happened before this existed.
	"""
	company = str(unit.get("owning_entity") or "")
	if not company or not compat.has_field("Company", "default_housing_deduction_from_wages"):
		return ""
	return str(frappe.db.get_value("Company", company, "default_housing_deduction_from_wages") or "")


def create_housing_assignment(args: dict) -> ToolResult:
	"""Put one person in one unit, from a date."""
	_require(HOUSING_ASSIGNMENT)
	_require(HOUSING_UNIT)
	company = _entity(args)
	unit = unit_row(as_str(args, "unit", required=True), company or "")
	assigned_date = as_date(args, "assigned_date", required=True)
	end_date = as_date(args, "end_date")
	employee = as_str(args, "employee")
	employee_name = as_str(args, "employee_name")

	if not employee and not employee_name:
		raise ToolError(
			"employee or employee_name is required — an assignment with no person in it is a "
			"unit, not an assignment. Nothing was created."
		)

	if employee and hr_installed():
		if not frappe.db.exists(EMPLOYEE, employee):
			match = frappe.db.get_value(EMPLOYEE, {"employee_name": employee}, "name")
			if not match:
				raise ToolError(
					f"no Employee {employee!r} on this site, and Frappe HR is installed — so an "
					"assignment naming somebody not on file is a roster that has already drifted "
					"from payroll. Create the Employee first. Nothing was created."
				)
			employee = match
		employee_name = employee_name or (
			frappe.db.get_value(EMPLOYEE, employee, "employee_name") or employee
		)

	if end_date and end_date < assigned_date:
		raise ToolError(
			f"end_date {end_date} is before assigned_date {assigned_date}. Nobody moved out "
			"before they moved in. Nothing was created."
		)

	if unit.get("unit_type") in NON_RESIDENTIAL_TYPES:
		raise ToolError(
			f"{unit['name']} is a {unit['unit_type']}. Nobody is assigned to a shower block or a "
			"shop — if people really sleep there, its type is wrong. Nothing was created."
		)
	if unit.get("condition") == "Uninhabitable":
		raise ToolError(
			f"{unit['name']} is marked Uninhabitable. Change the condition once it has been "
			"repaired and inspected, then assign. Nothing was created."
		)

	allow_multi = as_bool(args, "allow_multi_occupancy", False)
	clashes = _clashes(unit["name"], assigned_date, end_date)
	if clashes and not allow_multi:
		first = clashes[0]
		raise ToolError(
			f"{unit['name']} already has {first['name']} — {first['employee_name']} from "
			f"{first['assigned_date']}"
			+ (f" to {first['end_date']}" if first["end_date"] else " (still current)")
			+ ". Two people in one cabin on one night is usually a typo and sometimes a bunk "
			"room: pass allow_multi_occupancy=true if this unit really is shared. Nothing was "
			"created."
		)

	# v0.203.0. THE PRE-OCCUPANCY GATE, BEFORE ANYTHING IS WRITTEN. Interior
	# rodent bait not yet cleared by a COMPLETED Removal and Clearance: Advisory
	# reports it and allows, Enforced refuses. Seeded Off.
	bait_gate = rodent_bait.preoccupancy_gate(unit["name"], company or unit.get("owning_entity") or "")

	deposit_paid = as_float(args.get("deposit_paid"), "deposit_paid")
	doc = frappe.new_doc(HOUSING_ASSIGNMENT)
	doc.unit = unit["name"]
	doc.employee = employee
	doc.employee_name = employee_name or employee
	doc.assigned_date = assigned_date
	doc.end_date = end_date
	doc.deposit_paid = deposit_paid
	doc.deposit_returned = as_float(args.get("deposit_returned"), "deposit_returned")
	doc.notes = as_str(args, "notes")
	deduction = as_str(args, "housing_deduction_from_wages") or _company_housing_deduction(unit)
	if deduction:
		doc.housing_deduction_from_wages = as_choice(
			HOUSING_ASSIGNMENT,
			"housing_deduction_from_wages",
			deduction,
			"housing_deduction_from_wages",
		)
	doc.insert(ignore_permissions=True)

	described = _describe_assignment(dict(doc.as_dict()))
	# `clashes` was computed BEFORE the insert. Recomputing here would count the
	# row just written as one of its own overlaps and report one occupant too many.
	occupants = len(clashes) + 1
	warnings = []
	if clashes:
		warnings.append(
			f"Shared occupancy accepted: {len(clashes)} other assignment(s) overlap this one on "
			f"{unit['name']}."
		)
	if unit.get("capacity") and occupants > int(unit["capacity"]):
		warnings.append(
			f"{unit['name']} now holds {occupants} against a recorded capacity of {unit['capacity']}."
		)
	if described["housing_deduction_from_wages"] == "Unknown":
		warnings.append(
			"Whether a housing charge was deducted from wages is unrecorded. ORS 653 and OAR "
			"839-015 constrain that deduction, and Unknown is the answer that cannot be defended."
		)
	if employee and not hr_installed():
		warnings.append(
			"No HR app on this site, so the employee id was stored as text and not checked "
			"against an Employee register."
		)

	return ToolResult(
		data={
			**described,
			"unit_capacity": int(unit.get("capacity") or 0) or None,
			"occupants_after": occupants,
			"multi_occupancy": bool(clashes),
			"warnings": warnings,
			# v0.203.0. What the pre-occupancy rodent bait control said.
			"bait_clearance": bait_gate,
			"section_119_note": (
				"This record is the audit trail for an IRS Section 119 exclusion: lodging on the "
				"business premises, for the employer's convenience, and required as a condition "
				"of employment. It records the facts; it does not make the determination."
			),
		},
		summary=(
			f"assigned {described['employee_name']} to {unit['name']} from {assigned_date}"
			+ (f" to {end_date}" if end_date else "")
		),
		docstatus_delta="none → 0 (created)",
	)


def _entity_filter(companies) -> dict:
	"""`{"owning_entity": …}` for one company, several, or none at all.

	The two readers below are called from the mobile surface, where the caller's
	scope is a LIST of entities and naming one is optional — so a helper that took
	a single company would push the multi-entity case back to the caller and it
	would be got wrong once per caller.
	"""
	if not companies:
		return {}
	if isinstance(companies, str):
		return {"owning_entity": companies}
	names = sorted({str(name) for name in companies if name})
	if not names:
		return {}
	return {"owning_entity": ("in", names)}


def parcels_for_branch(branch: str, company: str = "") -> list:
	"""Every Parcel tagged with this operating unit, as docnames, sorted.

	v0.54.0, AND IT IS THE ONLY JOIN BETWEEN A PERSON AND A CABIN. An Employee
	carries a Branch and a Housing Unit stands on a Parcel; before `Parcel.branch`
	existed there was no column connecting the two at all, so a wizard that asked
	which camp somebody was hired to could not then show that camp's housing.

	A BRANCH MAY HOLD SEVERAL PARCELS AND THE ANSWER IS A LIST, not a single
	docname. A camp is a place, not a deed: a labor camp that grew across a fence
	line is two parcels, and a mapping that could only hold one would drop half
	the cabins on exactly the operations big enough to need the feature. The
	reverse — two branches on one parcel — is a parcel that carries one of them,
	which is the honest limit of a single column and is stated rather than
	papered over.

	An empty list means nobody has tagged any ground with this branch. That is a
	different answer from "this camp is full" and every caller here is careful to
	keep it different.
	"""
	branch = str(branch or "").strip()
	if not branch or not compat.has_field(PARCEL, "branch"):
		return []
	filters = {"branch": branch, **_entity_filter(company)}
	rows = frappe.db.get_all(PARCEL, filters=filters, pluck="name", limit=REGISTER_CAP) or []
	return sorted(str(name) for name in rows)


def branch_parcel_map(branches, company: str = "") -> dict:
	"""`{branch: [parcel, …]}` for many branches in one read.

	The list form of `parcels_for_branch`, because
	`list_onboarding_reference_data` needs the mapping for every branch on the
	site and one query per branch is one query per dropdown row.
	"""
	wanted = {str(name).strip() for name in (branches or []) if str(name or "").strip()}
	if not wanted or not compat.has_field(PARCEL, "branch"):
		return {}
	filters = {"branch": ("in", sorted(wanted)), **_entity_filter(company)}
	rows = frappe.db.get_all(PARCEL, filters=filters, fields=["name", "branch"], limit=REGISTER_CAP)
	out: dict = {}
	for row in rows or []:
		out.setdefault(str(row.get("branch") or ""), []).append(str(row.get("name")))
	return {branch: sorted(parcels) for branch, parcels in out.items() if branch}


def occupancy_for(unit: str, start: str, end: str | None = None) -> list:
	"""Who else is in this unit over these dates. The public name for `_clashes`.

	v0.54.0. `api/mobile.assign_housing` has to know the answer BEFORE it writes,
	because it refuses to overfill a cabin where `create_housing_assignment`
	merely warns — see that endpoint's docstring for why the two differ. It reads
	through this rather than reaching into `_clashes` so the overlap rule stays
	one function: inclusive at both ends, blank end date meaning still there.
	"""
	return _clashes(unit, start, end)


def _clashes(unit: str, start: str, end: str | None, exclude: str = "") -> list:
	"""Assignments on this unit whose dates share a day with the given range."""
	rows = frappe.db.get_all(
		HOUSING_ASSIGNMENT,
		filters={"unit": unit},
		fields=compat.existing_fields(HOUSING_ASSIGNMENT, _ASSIGNMENT_FIELDS),
		order_by="assigned_date asc",
		limit=REGISTER_CAP,
	)
	out = []
	for row in rows or []:
		if row.get("name") == exclude:
			continue
		if overlaps(str(row.get("assigned_date") or ""), str(row.get("end_date") or ""), start, end or ""):
			out.append(_describe_assignment(dict(row)))
	return out


# ── 117. end_housing_assignment ─────────────────────────────────────────────
def end_housing_assignment(args: dict) -> ToolResult:
	"""Write the date somebody moved out. Never deletes the record."""
	_require(HOUSING_ASSIGNMENT)
	name = as_str(args, "assignment", required=True)
	if not frappe.db.exists(HOUSING_ASSIGNMENT, name):
		raise ToolError(
			f"no Housing Assignment called {name!r}. list_housing_assignments has the register. "
			"Nothing was changed."
		)
	row = dict(
		frappe.db.get_value(
			HOUSING_ASSIGNMENT,
			name,
			compat.existing_fields(HOUSING_ASSIGNMENT, _ASSIGNMENT_FIELDS),
			as_dict=True,
		)
		or {}
	)
	end_date = as_date(args, "end_date", required=True)

	if row.get("end_date"):
		raise ToolError(
			f"{name} already ended on {row['end_date']}. Re-dating a departure is a correction, "
			"and this tool only closes an open assignment — edit it in the Desk if the date was "
			"genuinely wrong. Nothing was changed."
		)
	if end_date < str(row.get("assigned_date") or ""):
		raise ToolError(
			f"end date {end_date} is before {row.get('assigned_date')}, when the assignment "
			"started. Nobody moved out before they moved in. Nothing was changed."
		)

	returned = args.get("deposit_returned")
	paid = round(float(row.get("deposit_paid") or 0), 2)
	if returned is not None and returned != "":
		returned = as_float(returned, "deposit_returned")
		if returned > paid:
			raise ToolError(
				f"deposit_returned {returned} is more than the {paid} on record as paid. That is "
				"a refund of money nobody took. Nothing was changed."
			)
	else:
		returned = None

	dry_run = as_bool(args, "dry_run", False)
	if dry_run:
		return ToolResult(
			data={
				**_describe_assignment(row),
				"would_end_on": end_date,
				"would_return": returned,
				"dry_run": True,
				"changed": False,
			},
			summary=f"dry run: would end {name} on {end_date}",
		)

	doc = frappe.get_doc(HOUSING_ASSIGNMENT, name)
	doc.end_date = end_date
	if returned is not None:
		doc.deposit_returned = returned
	notes = as_str(args, "notes")
	if notes:
		doc.notes = f"{doc.get('notes')}\n{notes}".strip() if doc.get("notes") else notes
	doc.save(ignore_permissions=True)

	described = _describe_assignment(dict(doc.as_dict()))
	warnings = []
	if described["deposit_outstanding"] > 0:
		warnings.append(
			f"{described['deposit_outstanding']} of the deposit is still held. Record the refund "
			"with deposit_returned, or say in notes why it was retained."
		)
	return ToolResult(
		data={**described, "changed": True, "warnings": warnings},
		summary=f"{name} ended {end_date} ({described['employee_name']} out of {described['unit']})",
		docstatus_delta="0 → 0 (updated)",
	)


# ── 118. get_housing_capacity ───────────────────────────────────────────────
def get_housing_capacity(args: dict) -> ToolResult:
	"""Beds, bodies and what is overdue — per parcel or across the whole operation."""
	_require(HOUSING_UNIT)
	company = _entity(args)

	filters = {}
	if company:
		filters["owning_entity"] = company
	parcel = as_str(args, "parcel")
	if parcel:
		filters["parcel"] = str(parcel_row(parcel, company or "")["name"])

	today = frappe.utils.today()
	rows = frappe.db.get_all(
		HOUSING_UNIT,
		filters=filters,
		fields=compat.existing_fields(HOUSING_UNIT, _UNIT_FIELDS),
		order_by="parcel asc, unit_name asc",
		limit=REGISTER_CAP,
	)
	units = [_describe_unit(dict(row), today) for row in rows]
	occupied = _current_assignments([unit["name"] for unit in units])

	by_parcel: dict = {}
	for unit in units:
		bucket = by_parcel.setdefault(
			unit["parcel"],
			{
				"parcel": unit["parcel"],
				"unit_count": 0,
				"residential_units": 0,
				"capacity": 0,
				"lawful_capacity": 0,
				"assigned": 0,
				"open_beds": 0,
				"by_unit_type": {},
				"overdue_inspections": [],
				"overdue_detector_tests": [],
				"uninhabitable": [],
				"over_lawful_occupancy": [],
			},
		)
		bucket["unit_count"] += 1
		kind = unit["unit_type"] or "(unrecorded)"
		bucket["by_unit_type"][kind] = bucket["by_unit_type"].get(kind, 0) + 1
		if unit["condition"] == "Uninhabitable":
			bucket["uninhabitable"].append(unit["name"])
		if unit["capacity_over_lawful_occupancy"]:
			bucket["over_lawful_occupancy"].append(unit["name"])
		# Counted BEFORE the residential guard below. The detector obligation is
		# scoped on `fsma_worker_facility` rather than on being somewhere people
		# sleep, and `_describe_unit` has already applied the same three
		# conditions the alert rule uses — so this reads its answer rather than
		# re-deciding it here, where the two would drift.
		if unit["detector_test_overdue"]:
			bucket["overdue_detector_tests"].append(unit["name"])
		if not unit["residential"]:
			continue
		bucket["residential_units"] += 1
		bucket["capacity"] += unit["capacity"] or 0
		bucket["lawful_capacity"] += unit["max_occupants_per_or_law"] or 0
		bucket["assigned"] += len(occupied.get(unit["name"], []))
		if unit["inspection_overdue"]:
			bucket["overdue_inspections"].append(unit["name"])

	for bucket in by_parcel.values():
		bucket["open_beds"] = max(0, bucket["capacity"] - bucket["assigned"])
		bucket["by_unit_type"] = dict(sorted(bucket["by_unit_type"].items()))

	parcels = [by_parcel[key] for key in sorted(by_parcel)]
	capacity = sum(bucket["capacity"] for bucket in parcels)
	assigned = sum(bucket["assigned"] for bucket in parcels)
	overdue = sum(len(bucket["overdue_inspections"]) for bucket in parcels)
	detectors = sum(len(bucket["overdue_detector_tests"]) for bucket in parcels)

	lines = []
	for bucket in parcels:
		line = (
			f"{bucket['parcel']}: {bucket['residential_units']} residential units, capacity "
			f"{bucket['capacity']}, currently {bucket['assigned']} assigned, "
			f"{bucket['open_beds']} open."
		)
		if bucket["overdue_inspections"]:
			line += f" Overdue habitability inspections: {len(bucket['overdue_inspections'])}."
		if bucket["overdue_detector_tests"]:
			line += f" Overdue detector tests: {len(bucket['overdue_detector_tests'])}."
		lines.append(line)

	return ToolResult(
		data={
			"company": company,
			"parcel_count": len(parcels),
			"unit_count": len(units),
			"total_capacity": capacity,
			"total_lawful_capacity": sum(bucket["lawful_capacity"] for bucket in parcels),
			"currently_assigned": assigned,
			"open_beds": max(0, capacity - assigned),
			"overdue_inspection_count": overdue,
			"overdue_detector_test_count": detectors,
			"by_parcel": parcels,
			"readout": lines,
			"inspection_window_days": INSPECTION_DAYS,
			"detector_window_days": DETECTOR_DAYS,
		},
		summary=(
			f"capacity {capacity}, {assigned} assigned, {max(0, capacity - assigned)} open, "
			f"{overdue} overdue inspection(s), {detectors} overdue detector test(s)"
		),
	)


# ── 119. get_employee_housing_history ───────────────────────────────────────
def get_employee_housing_history(args: dict) -> ToolResult:
	"""Everywhere one person has been housed, in order, with the gaps visible."""
	_require(HOUSING_ASSIGNMENT)
	person = as_str(args, "employee", required=True)

	rows = frappe.db.get_all(
		HOUSING_ASSIGNMENT,
		filters={"employee": person},
		fields=compat.existing_fields(HOUSING_ASSIGNMENT, _ASSIGNMENT_FIELDS),
		order_by="assigned_date asc",
		limit=REGISTER_CAP,
	)
	if not rows:
		rows = frappe.db.get_all(
			HOUSING_ASSIGNMENT,
			filters={"employee_name": person},
			fields=compat.existing_fields(HOUSING_ASSIGNMENT, _ASSIGNMENT_FIELDS),
			order_by="assigned_date asc",
			limit=REGISTER_CAP,
		)
	if not rows:
		raise ToolError(
			f"no housing assignment names {person!r}, under either an employee id or an employee "
			"name. list_housing_assignments has the register."
		)

	assignments = [_describe_assignment(dict(row)) for row in rows]
	current = [entry for entry in assignments if entry["current"]]
	name = assignments[-1]["employee_name"] or person

	lines = []
	for entry in assignments:
		lines.append(
			f"{name} assigned {entry['unit']} {entry['assigned_date']} → " + (entry["end_date"] or "present")
		)
	if not current:
		lines.append(f"{name} is currently unassigned.")

	return ToolResult(
		data={
			"employee": person,
			"employee_name": name,
			"assignment_count": len(assignments),
			"currently_assigned": [entry["unit"] for entry in current],
			"units_lived_in": sorted({entry["unit"] for entry in assignments}),
			"first_assigned": assignments[0]["assigned_date"],
			"last_assigned": assignments[-1]["assigned_date"],
			"deposits_paid": round(sum(entry["deposit_paid"] for entry in assignments), 2),
			"deposits_returned": round(sum(entry["deposit_returned"] for entry in assignments), 2),
			"deposits_outstanding": round(sum(entry["deposit_outstanding"] for entry in assignments), 2),
			"wage_deduction_taken": [
				entry["name"] for entry in assignments if entry["housing_deduction_from_wages"] == "Yes"
			],
			"assignments": assignments,
			"readout": lines,
		},
		summary=(
			f"{name}: {len(assignments)} assignment(s), "
			+ (f"currently in {current[0]['unit']}" if current else "currently unassigned")
		),
	)


# ── my_housing ── v0.195.0, SERVER_CHANGES §42 ──────────────────────────────
def my_housing(employee: str) -> dict:
	"""One worker's current bed, in the shape the phone's My Records screen reads.

	THE CALLER'S OWN EMPLOYEE ONLY — the mobile wrapper resolves it from the
	login and never takes one from the body, because a shared handset must not
	show one worker another's cabin. `{"assignment": None, "unit": None}` when
	they hold none, which is a real answer rather than an error.

	`bed` IS ALWAYS NULL, AND THAT IS THE REGISTER'S TRUTH: this farm assigns a
	UNIT, and no Housing Assignment has ever carried a bed. `camp` is the unit's
	Parcel — where the cabins stand — and `address` is that parcel's. `status`
	is this register's own word, `Current`.
	"""
	empty = {"assignment": None, "unit": None}
	if not employee or not compat.doctype_exists(HOUSING_ASSIGNMENT):
		return empty
	rows = frappe.db.get_all(
		HOUSING_ASSIGNMENT,
		filters={"employee": employee, "end_date": ("is", "not set")},
		fields=compat.existing_fields(HOUSING_ASSIGNMENT, _ASSIGNMENT_FIELDS),
		order_by="assigned_date desc, creation desc",
		limit=1,
	)
	if not rows:
		return empty
	assignment = _describe_assignment(dict(rows[0]))
	unit_name = assignment["unit"]
	unit_row = {}
	if unit_name and frappe.db.exists(HOUSING_UNIT, unit_name):
		unit_row = dict(
			frappe.db.get_value(
				HOUSING_UNIT, unit_name, compat.existing_fields(HOUSING_UNIT, _UNIT_FIELDS), as_dict=True
			)
			or {}
		)
	parcel = unit_row.get("parcel") or assignment.get("parcel") or None
	address = None
	if parcel and compat.has_field("Parcel", "address"):
		address = str(frappe.db.get_value("Parcel", parcel, "address") or "") or None
	occupancy = (
		frappe.db.count(HOUSING_ASSIGNMENT, {"unit": unit_name, "end_date": ("is", "not set")})
		if unit_name
		else None
	)
	return {
		"assignment": {
			"name": assignment["name"],
			"housing_unit": unit_name,
			"unit_label": unit_row.get("unit_name") or unit_name,
			"bed": None,
			"camp": parcel,
			"start_date": assignment["assigned_date"],
			"end_date": assignment["end_date"],
			"status": assignment["status"],
		},
		"unit": {
			"address": address,
			"occupancy": occupancy,
			"capacity": int(unit_row.get("capacity") or 0) or None,
			"last_habitability_inspection": _date_str(unit_row.get("last_habitability_inspection")),
		},
	}

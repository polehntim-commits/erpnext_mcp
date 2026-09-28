# SPDX-License-Identifier: MIT
"""Is anybody there, and is it the season? Generic, for any program. v0.205.0.

docs/design/programs_and_field_kinds.md A3/A5. Moved out of `rodent_bait` so a
rule, a template or another program can ask the same two questions:

  * OCCUPANCY of a Housing Unit or an Asset Register row: an active Housing
    Assignment; the place's own `occupied` flag (first-class — Mill Creek's
    houses at takeover); the asset's state `occupied`; the place's
    `people_work_here` flag; or its TYPE — the Farm Asset Type named after the
    asset type (or the Housing Unit's `unit_type`) carrying `people_present`.
    The last used to be a list in ERPNext MCP Settings; it is data now.
  * THE SEASON of a company: `season_start` / `season_end` (MM-DD) on Company,
    03-01 → 10-31 when unset. A window may wrap the year end.
"""

from __future__ import annotations

import json
import re

import frappe

from . import compat

HOUSING_UNIT = "Housing Unit"
ASSET_REGISTER = "Asset Register"
HOUSING_ASSIGNMENT = "Housing Assignment"
ASSET_TYPE = "Farm Asset Type"
LOCATION_DOCTYPES = (HOUSING_UNIT, ASSET_REGISTER)
DEFAULT_SEASON = ("03-01", "10-31")


def type_has_people(kind: str) -> bool:
	"""Does the Farm Asset Type of this name say people live or work there?"""
	if not kind or not compat.has_field(ASSET_TYPE, "people_present"):
		return False
	return compat.checked(frappe.db.get_value(ASSET_TYPE, kind, "people_present"))


def occupancy(location_doctype: str, location: str, on: str | None = None) -> dict:
	"""`{occupied, source, detail}` for one place. The first source that says yes wins.

	Never raises: a place this cannot read is reported OCCUPIED — the strict
	tier — because guessing empty is the unsafe direction.
	"""
	on = str(on or frappe.utils.today())[:10]
	if location_doctype not in LOCATION_DOCTYPES or not location:
		return {"occupied": False, "source": "none", "detail": "no housing unit or building named"}
	try:
		if not compat.doctype_exists(location_doctype) or not frappe.db.exists(location_doctype, location):
			return {"occupied": True, "source": "unreadable", "detail": f"{location} could not be read"}
		if location_doctype == HOUSING_UNIT:
			assignment = active_assignment(location, on)
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
		if asset_state(row.get("current_state")) == "occupied":
			return {
				"occupied": True,
				"source": "manual_flag",
				"detail": f"{location}'s state is occupied (mark_occupied on the asset)",
			}
		if compat.checked(row.get("people_work_here")):
			return {"occupied": True, "source": "people_work_here", "detail": f"people work at {location}"}
		kind = str(row.get("unit_type") or row.get("asset_type") or "")
		if type_has_people(kind):
			return {
				"occupied": True,
				"source": "people_work_here",
				"detail": f"{location} is a {kind}, a type where people live or work",
			}
	except Exception:
		return {"occupied": True, "source": "unreadable", "detail": f"{location} could not be read"}
	return {"occupied": False, "source": "none", "detail": f"nobody lives or works at {location}"}


def asset_state(raw) -> str:
	try:
		value = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or {})
	except Exception:
		return ""
	return str(value.get("state") or "") if isinstance(value, dict) else ""


def active_assignment(unit: str, on: str) -> str:
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
SEASON_FIELDS = ("season_start", "season_end")


def mmdd(value, fallback: str) -> str:
	match = _MMDD.match(str(value or "").strip())
	if not match:
		return fallback
	month, day = int(match.group(1)), int(match.group(2))
	if not (1 <= month <= 12 and 1 <= day <= 31):
		return fallback
	return f"{month:02d}-{day:02d}"


def valid_mmdd(value) -> bool:
	return bool(_MMDD.match(str(value or "").strip())) and mmdd(value, "") != ""


def season_of(company: str) -> tuple:
	"""`(start, end)` as MM-DD for a company; March–October when unset."""
	start, end = DEFAULT_SEASON
	if company and compat.doctype_exists("Company"):
		fields = compat.existing_fields("Company", SEASON_FIELDS)
		if fields:
			row = frappe.db.get_value("Company", company, fields, as_dict=True) or {}
			raw_start, raw_end = row.get("season_start"), row.get("season_end")
			if str(raw_start or "").strip() and str(raw_end or "").strip():
				start, end = mmdd(raw_start, start), mmdd(raw_end, end)
	return start, end


def in_season(company: str, on: str | None = None) -> bool:
	"""Is `on` inside the company's season? Inclusive; a window may wrap the year."""
	day = str(on or frappe.utils.today())[5:10]
	start, end = season_of(company)
	if start <= end:
		return start <= day <= end
	return day >= start or day <= end

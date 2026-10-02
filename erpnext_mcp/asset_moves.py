# SPDX-License-Identifier: MIT
"""Fixed assets do not move by accident. v0.214.0.

docs/design/badge_photo_and_fixed_assets.md, Part B.

WHAT HAPPENED. 40-WM-SE, a wind machine, moved 180 metres. `scan_asset` had
always written the handset's GPS fix onto the asset on every scan, and nothing
recorded that it did — so the pin followed whoever last pointed a phone at the
tag, from wherever they were standing, and there was no history to put it back
from. A wind machine does not move. The phone that scanned it does.

THREE RULES, AND ALL THREE ARE ENFORCED HERE OR IN THE CONTROLLER:

  1. A FIXED asset's position changes only through an explicit move — a named
     person, a reason, and a row in the Asset State Log with where it was and
     where it went. `Asset Register.validate` calls `guard`, so the Desk form
     and every code path that saves the document are covered, not only the tools.
  2. Every position change is HISTORY: a move, an undo, a correction over MCP,
     and a mobile asset re-sighted by a scan.
  3. A move can be undone for a day.

`fixed_location` is a column on `Farm Asset Type` — data, so a farm decides what
moves on its own ground.
"""

from __future__ import annotations

import math

import frappe

from . import compat

ASSET = "Asset Register"
TYPE = "Farm Asset Type"
LOG = "Asset State Log"

MOVED = "Moved"
UNDONE = "Move undone"

#: Who may move an asset.
ROLES = ("System Manager", "Farm Manager", "Foreman")

#: How long a move can be undone.
UNDO_HOURS = 24

#: A mobile asset re-sighted by a scan writes a history row past this distance.
#: Below it the difference is the GPS, not the tractor.
SCAN_MOVE_METRES = 25.0

#: What ships fixed. Everything else — and any type this app has never heard of
#: — is mobile until a farm ticks the box.
FIXED_TYPES = (
	"Irrigation Valve",
	"Irrigation Zone",
	"Water Source",
	"Wind Machine",
	"Fuel Tank",
	"Gas Tank",
	"Storage",
	"Cold Storage",
	"Block",
	"Housing Unit",
	"Cabin",
	"House",
	"Barn",
	"Shop",
	"Kitchen",
	"Bath House",
	"Toilet-Shower",
	"Well",
	"Pump",
)


def ready() -> bool:
	return compat.has_field(TYPE, "fixed_location")


def is_fixed(asset_type) -> bool:
	"""Whether assets of this type stay where they are. The register's answer; the shipped list before migrate."""
	name = str(asset_type or "").strip()
	if not name:
		return False
	try:
		if ready() and frappe.db.exists(TYPE, name):
			return compat.checked(frappe.db.get_value(TYPE, name, "fixed_location"))
	except Exception:  # pragma: no cover - a site mid-migrate
		pass
	return name in FIXED_TYPES


def _number(value) -> float:
	if value in (None, ""):
		return 0.0
	try:
		return float(value)
	except (TypeError, ValueError):
		return 0.0


def has_position(latitude, longitude) -> bool:
	"""0,0 is Frappe's unset Float, not a place on this farm."""
	return _number(latitude) != 0 or _number(longitude) != 0


def point(latitude, longitude) -> dict | None:
	if not has_position(latitude, longitude):
		return None
	return {"latitude": round(_number(latitude), 7), "longitude": round(_number(longitude), 7)}


def distance_m(a_lat, a_lon, b_lat, b_lon) -> float:
	"""Metres between two fixes (haversine)."""
	p1, p2 = math.radians(_number(a_lat)), math.radians(_number(b_lat))
	dp = p2 - p1
	dl = math.radians(_number(b_lon) - _number(a_lon))
	h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
	return round(2 * 6371008.8 * math.asin(min(1.0, math.sqrt(h))), 1)


def same_place(a_lat, a_lon, b_lat, b_lon) -> bool:
	return abs(_number(a_lat) - _number(b_lat)) < 1e-7 and abs(_number(a_lon) - _number(b_lon)) < 1e-7


# ── the controller's guard ──────────────────────────────────────────────────
def guard(doc) -> None:
	"""`Asset Register.validate`: a fixed asset's position changes only through a move."""
	if getattr(doc.flags, "move_authorised", False):
		return
	if not doc.name or not frappe.db.exists(ASSET, doc.name):
		return  # being registered: a first position is not a move
	before = frappe.db.get_value(ASSET, doc.name, ["gps_latitude", "gps_longitude"], as_dict=True) or {}
	if not has_position(before.get("gps_latitude"), before.get("gps_longitude")):
		return  # no position yet: placing it is not a move
	if same_place(
		before.get("gps_latitude"), before.get("gps_longitude"), doc.gps_latitude, doc.gps_longitude
	):
		return
	if not is_fixed(doc.asset_type):
		return
	metres = distance_m(
		before.get("gps_latitude"), before.get("gps_longitude"), doc.gps_latitude, doc.gps_longitude
	)
	frappe.throw(
		f"{doc.name} is a {doc.asset_type}, which has a fixed location, and this would move it "
		f"{metres:g} m. A fixed asset's position changes only through Move asset (move_asset, or "
		"update_registered_asset with a reason), which records who moved it, why, and where it "
		"was. Nothing was changed.",
		title="Fixed location",
	)


# ── the write ───────────────────────────────────────────────────────────────
def _actor(by: str = "") -> str:
	user = str(by or getattr(frappe.session, "user", "") or "")
	try:
		return user if user and frappe.db.exists("User", user) else ""
	except Exception:  # pragma: no cover
		return ""


def log(
	asset: str, asset_type: str, before, after, action: str, reason: str, by: str = "", request: str = ""
) -> dict:
	"""One Asset State Log row for a position change. State is untouched."""
	if not compat.doctype_exists(LOG):
		return {}
	state = ""
	try:
		from .tools import asset_tags

		state = asset_tags._current_state_value(frappe.db.get_value(ASSET, asset, "current_state")) or ""
	except Exception:
		state = ""
	metres = (
		distance_m(before["latitude"], before["longitude"], after["latitude"], after["longitude"])
		if before and after
		else 0.0
	)
	row = frappe.new_doc(LOG)
	row.asset_name = asset
	row.asset_type = asset_type
	row.action = action
	row.from_state = state or None
	row.to_state = state or None
	row.performed_by = _actor(by) or None
	row.performed_at = frappe.utils.now()
	row.notes = (reason or "").strip()[:1000] or None
	if after:
		row.gps_latitude = after["latitude"]
		row.gps_longitude = after["longitude"]
	if compat.has_field(LOG, "from_latitude"):
		if before:
			row.from_latitude = before["latitude"]
			row.from_longitude = before["longitude"]
		row.distance_m = metres
		row.client_request_id = request or None
	row.insert(ignore_permissions=True)
	return {"log": row.name, "distance_m": metres, "moved_at": str(row.performed_at)}


def move(
	doc, latitude, longitude, *, reason: str = "", by: str = "", action: str = MOVED, request: str = ""
) -> dict:
	"""Put an asset somewhere, authorised, and write the history row. Saves `doc`."""
	before = point(doc.gps_latitude, doc.gps_longitude)
	after = point(latitude, longitude)
	doc.gps_latitude = after["latitude"] if after else 0
	doc.gps_longitude = after["longitude"] if after else 0
	doc.flags.move_authorised = True
	doc.save(ignore_permissions=True)
	written = log(doc.name, str(doc.asset_type or ""), before, after, action, reason, by, request)
	return {"from": before, "to": after, **written}


# ── reads ───────────────────────────────────────────────────────────────────
def _hours_since(stamp) -> float:
	try:
		then = frappe.utils.get_datetime(str(stamp))
		now = frappe.utils.get_datetime(str(frappe.utils.now()))
		return (now - then).total_seconds() / 3600.0
	except Exception:
		return 1e9


def move_rows(asset: str, limit: int = 20) -> list:
	if not (compat.doctype_exists(LOG) and compat.has_field(LOG, "from_latitude")):
		return []
	fields = compat.existing_fields(
		LOG,
		(
			"name",
			"action",
			"performed_by",
			"performed_at",
			"notes",
			"gps_latitude",
			"gps_longitude",
			"from_latitude",
			"from_longitude",
			"distance_m",
			"client_request_id",
			"creation",
		),
	)
	return [
		dict(row)
		for row in frappe.db.get_all(
			LOG,
			filters={"asset_name": asset, "action": ("in", [MOVED, UNDONE])},
			fields=fields,
			order_by="creation desc",
			limit=limit,
		)
		or []
	]


def describe(row: dict) -> dict:
	when = str(row.get("performed_at") or row.get("creation") or "")
	before = point(row.get("from_latitude"), row.get("from_longitude"))
	undoable = str(row.get("action")) == MOVED and before is not None and _hours_since(when) < UNDO_HOURS
	undo_until = None
	if undoable:
		try:
			undo_until = str(frappe.utils.add_to_date(when, hours=UNDO_HOURS))
		except Exception:
			undo_until = None
	return {
		"log": row.get("name"),
		"action": row.get("action"),
		"moved_at": when or None,
		"moved_by": row.get("performed_by") or None,
		"from": before,
		"to": point(row.get("gps_latitude"), row.get("gps_longitude")),
		"distance_m": round(_number(row.get("distance_m")), 1),
		"reason": row.get("notes") or None,
		"can_undo": undoable,
		"undo_until": undo_until,
	}


def last_move(asset: str) -> dict | None:
	"""The latest position change, with whether it can still be undone."""
	rows = move_rows(asset, limit=1)
	return describe(rows[0]) if rows else None

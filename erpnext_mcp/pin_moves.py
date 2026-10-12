# SPDX-License-Identifier: MIT
"""Put a pin in the right spot, by hand — and record who did it and when. v0.323.0.

Tim: "We used to have a way of moving a pin in the Desk, controllable on the map." Asset Register kept its draggable
pin (v0.145.0 / v0.154.0); Housing Unit, Farm Task and Field Pin never had one. This is the one server path a hand
placement goes through, from the Desk map, the phone (long-press, queued offline) or MCP:

- a TASK gets a Work Spot pin (`task_locations`), which overrides its blocks' combined centroid;
- an ASSET or a HOUSING UNIT gets its `gps_latitude` / `gps_longitude` (a unit and its Asset Register tag move
  together);
- a FIELD PIN (entrance, delivery spot, hive drop, work spot) moves itself.

WHO AND WHEN are stamped on the record — `position_set_by` / `position_set_at` / `position_source` on Asset Register
and Housing Unit, `moved_by` / `moved_at` on Field Pin — by the controllers, so a Desk form save that changes the
coordinates is stamped exactly as a phone move is. WHO MAY: the roles that run the farm (System Manager, Farm Manager,
Foreman); the phone route adds the caller's entity scope.
"""

from __future__ import annotations

import frappe

from . import compat, pins

ASSET = "Asset Register"
HOUSING = "Housing Unit"
TASK = "Farm Task"
PIN = pins.PIN
KINDS = {"task": TASK, "asset": ASSET, "housing_unit": HOUSING, "field_pin": PIN}
MOVER_ROLES = ("System Manager", "Farm Manager", "Foreman")
SOURCES = ("Desk", "Phone", "MCP", "Scan")


class MoveError(ValueError):
	pass


def may_move(user: str = "") -> bool:
	user = user or str(frappe.session.user or "")
	if user == "Administrator":
		return True
	return bool(set(frappe.get_roles(user)) & set(MOVER_ROLES))


def source() -> str:
	"""How this write reached the server: set by the phone route; MCP calls carry their auth method; else the Desk."""
	given = getattr(frappe.flags, "pin_source", None)
	if given in SOURCES:
		return given
	if getattr(frappe.flags, "mcp_auth_method", None):
		return "MCP"
	return "Desk"


def _moved(doc, lat_field: str, lon_field: str) -> bool:
	if doc.is_new():
		return False
	before = doc.get_doc_before_save() if hasattr(doc, "get_doc_before_save") else getattr(doc, "_doc_before_save", None)
	if before is None:
		return False
	return (round(float(before.get(lat_field) or 0), 7), round(float(before.get(lon_field) or 0), 7)) != (
		round(float(doc.get(lat_field) or 0), 7), round(float(doc.get(lon_field) or 0), 7))


def stamp_position(doc) -> None:
	"""Controller hook (Asset Register, Housing Unit): who moved the pin, when, and from where."""
	if not compat.has_field(doc.doctype, "position_set_at") or not _moved(doc, "gps_latitude", "gps_longitude"):
		return
	doc.position_set_by = str(frappe.session.user or "") or None
	doc.position_set_at = frappe.utils.now()
	if compat.has_field(doc.doctype, "position_source"):
		doc.position_source = source()


def stamp_pin(doc) -> None:
	"""Controller hook (Field Pin): who moved it and when."""
	if not _moved(doc, "latitude", "longitude"):
		return
	doc.moved_by = str(frappe.session.user or "") or doc.get("moved_by")
	if compat.has_field(PIN, "moved_at"):
		doc.moved_at = frappe.utils.now()


def follow_housing_tag(doc) -> None:
	"""Asset Register on_update: a cabin's tag moved by hand moves the cabin too (and the reverse is housing_assets)."""
	if not compat.has_field(HOUSING, "asset_tag") or not _moved(doc, "gps_latitude", "gps_longitude"):
		return
	unit = frappe.db.get_value(HOUSING, {"asset_tag": doc.name}, "name")
	if unit:
		updates = {"gps_latitude": doc.gps_latitude, "gps_longitude": doc.gps_longitude}
		for key in ("position_set_by", "position_set_at", "position_source"):
			if compat.has_field(HOUSING, key) and doc.get(key):
				updates[key] = doc.get(key)
		frappe.db.set_value(HOUSING, unit, updates, update_modified=False)


def move(kind: str, name: str, latitude, longitude, *, user: str = "", clear: bool = False, reason: str = "") -> dict:
	"""Move one pin. Validated; role-gated. Returns {kind, name, before, latitude, longitude, moved_by, moved_at}."""
	kind = str(kind or "").strip().lower()
	if kind not in KINDS:
		raise MoveError(f"kind is one of: {', '.join(KINDS)}.")
	doctype = KINDS[kind]
	if not compat.doctype_exists(doctype) or not frappe.db.exists(doctype, name):
		raise MoveError(f"no {doctype} called {name!r}.")
	if not may_move(user):
		raise MoveError(f"moving a pin needs {', '.join(MOVER_ROLES)}.")
	if clear and kind != "task":
		raise MoveError("only a task's work spot can be cleared; an asset or a cabin keeps a position.")
	point = None if clear else pins._point(latitude, longitude)

	if kind == "task":
		from . import task_locations

		before = task_locations.work_spot(name)
		task_locations.set_locations(name, pin=point, clear_pin=clear)
		spot = pins.one(TASK, name, task_locations.PIN_KIND) or {}
		if spot and compat.has_field(PIN, "moved_at"):
			frappe.db.set_value(PIN, spot["name"], {"moved_by": user or frappe.session.user,
			                                         "moved_at": frappe.utils.now()}, update_modified=False)
		after = task_locations.describe(name)["map_point"] or {}
		return {"kind": kind, "name": name, "before": before, "cleared": clear,
		        "latitude": after.get("latitude"), "longitude": after.get("longitude"),
		        "basis": after.get("basis"), "moved_by": user or frappe.session.user, "moved_at": frappe.utils.now()}

	if kind == "asset":
		# THE ONE WAY AN ASSET MOVES BY HAND: `move_asset` — a fixed asset (valve, building, cabin) refuses a plain
		# save that moves it (v0.214.0), and this path records the reason, who and where it was. A first placement
		# needs no reason; moving one that has a position does.
		from .errors import ToolError
		from .tools import asset_tags

		before = dict(frappe.db.get_value(ASSET, name, ["gps_latitude", "gps_longitude"], as_dict=True) or {})
		args = {"asset_name": name, "gps_latitude": point[0], "gps_longitude": point[1]}
		if reason:
			args["reason"] = reason
		try:
			data = asset_tags.move_asset(args).data
		except ToolError as exc:
			raise MoveError(str(exc)) from None
		row = frappe.db.get_value(ASSET, name, ["position_set_by", "position_set_at"], as_dict=True) or {}
		return {"kind": kind, "name": name, "before": {"latitude": before.get("gps_latitude"),
		                                               "longitude": before.get("gps_longitude")},
		        "latitude": point[0], "longitude": point[1], "moved": data.get("moved", True),
		        "moved_by": row.get("position_set_by"), "moved_at": row.get("position_set_at")}

	lat_field, lon_field = ("latitude", "longitude") if kind == "field_pin" else ("gps_latitude", "gps_longitude")
	doc = frappe.get_doc(doctype, name)
	before = {"latitude": doc.get(lat_field), "longitude": doc.get(lon_field)}
	doc.set(lat_field, point[0])
	doc.set(lon_field, point[1])
	doc.save(ignore_permissions=True)
	return {"kind": kind, "name": name, "before": before, "latitude": point[0], "longitude": point[1],
	        "moved_by": doc.get("position_set_by") or doc.get("moved_by"),
	        "moved_at": doc.get("position_set_at") or doc.get("moved_at")}


# ── Desk ────────────────────────────────────────────────────────────────────
@frappe.whitelist(methods=["GET"])
def task_map(task: str) -> dict:
	"""What the Farm Task form's map draws: every place's outline, the pin and where it came from."""
	if not frappe.has_permission(TASK, "read", task):
		frappe.throw("Not permitted", frappe.PermissionError)
	from . import task_locations

	described = task_locations.describe(task, with_outline=True)
	spot = pins.one(TASK, task, task_locations.PIN_KIND) if compat.doctype_exists(PIN) else None
	described["work_spot_moved"] = {"by": spot.get("moved_by"), "at": str(spot.get("moved_at") or "") or None} \
		if spot else None
	described["may_move"] = may_move() and frappe.has_permission(TASK, "write", task)
	return described


@frappe.whitelist(methods=["POST"])
def move_asset_pin(asset: str, latitude=None, longitude=None, reason=None) -> dict:
	"""The Asset Register form's draggable pin: move it through Move asset (a reason once it has a position)."""
	if not frappe.has_permission(ASSET, "write", asset):
		frappe.throw("Not permitted", frappe.PermissionError)
	frappe.flags.pin_source = "Desk"
	try:
		return move("asset", asset, latitude, longitude, reason=str(reason or "").strip())
	except (MoveError, pins.PinError, ValueError) as exc:
		frappe.throw(str(exc))


@frappe.whitelist(methods=["POST"])
def set_task_work_spot(task: str, latitude=None, longitude=None, clear=0) -> dict:
	"""The Farm Task form's draggable pin: set (or clear) the task's Work Spot right away."""
	if not frappe.has_permission(TASK, "write", task):
		frappe.throw("Not permitted", frappe.PermissionError)
	frappe.flags.pin_source = "Desk"
	try:
		return move("task", task, latitude, longitude, clear=bool(int(clear or 0)))
	except (MoveError, pins.PinError, ValueError) as exc:
		frappe.throw(str(exc))

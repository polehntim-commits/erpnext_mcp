# SPDX-License-Identifier: MIT
"""Where a Farm Task is: one or more blocks, tags or parcels, and an optional explicit pin. v0.323.0.

WHY. Tim's Bing removal tasks covered two blocks (MC-CP and MC-WM) but a task held ONE location, so they were filed
against the parcel and the phone dropped the pin at the parcel's centre — nowhere near the work. Now a task carries a
child list of locations (`Farm Task Location`: Field, Asset Register or Parcel). The phone pin goes at their combined
centroid (area-weighted where the outlines are drawn), or at an explicit pin when somebody sets one. The task detail
draws the union of the outlines.

THE FIRST LOCATION IS STILL `location_doctype` / `location`, mirrored on every write, so every reader written for one
location (scan, dispatch, offline pack, reports) keeps working and sees the primary block.

THE PIN REUSES THE FIELD-PIN PATTERN: a `Field Pin` of kind "Work Spot" sourced on the task (`pins.put`), so it is one
more pin on the map's pin layer rather than two new columns.

SETTING THEM IS VALIDATED HERE, not through the Updatable Fields whitelist: each location must exist, be one of the
three registers, be on the task's company, and not be a retired tag; a closed task's place is history and is refused.
Blocks resolve by docname, name, ticker or alias, and a named group ("Bing Block") brings all of its blocks.
"""

from __future__ import annotations

import json

import frappe

from . import compat, field_names, pins

TASK = "Farm Task"
CHILD = "Farm Task Location"
TABLE = "locations"
COUNT = "location_count"
SPOT_FLAG = "has_work_spot"
PIN_KIND = "Work Spot"
REGISTERS = ("Field", "Asset Register", "Parcel")
COMPANY_FIELD = {"Field": "owning_entity", "Asset Register": "company", "Parcel": "owning_entity"}
LABEL_FIELD = {"Field": "field_name", "Asset Register": "description", "Parcel": "parcel_name"}
CLOSED_STATES = ("Completed", "Cancelled", "Rejected")
MAX_LOCATIONS = 25
_GEOMETRY = ("boundary_geojson", "boundary_centroid_lat", "boundary_centroid_lon", "area_computed_acres",
             "gps_latitude", "gps_longitude")


class LocationError(ValueError):
	pass


def ready() -> bool:
	return compat.doctype_exists(CHILD) and compat.has_field(TASK, TABLE)


# ── reading ─────────────────────────────────────────────────────────────────
def rows(task: str) -> list:
	"""[{location_type, location, label}] in order; the primary location alone for a task with no child rows."""
	if ready():
		found = frappe.db.get_all(CHILD, filters={"parent": task, "parenttype": TASK},
		                          fields=["location_doctype", "location", "label"], order_by="idx asc", limit=100) or []
		if found:
			return [{"location_type": r["location_doctype"], "location": r["location"], "label": r.get("label") or None}
			        for r in found]
	primary = frappe.db.get_value(TASK, task, ["location_doctype", "location"], as_dict=True) or {}
	if primary.get("location_doctype") and primary.get("location"):
		return [{"location_type": primary["location_doctype"], "location": primary["location"],
		         "label": _label(primary["location_doctype"], primary["location"])}]
	return []


def work_spot(task: str) -> dict | None:
	if not compat.doctype_exists(pins.PIN):
		return None
	row = pins.one(TASK, task, PIN_KIND)
	if not row:
		return None
	return {"latitude": row.get("latitude"), "longitude": row.get("longitude"), "label": row.get("label") or None}


def _geometry(doctype: str, name: str) -> dict:
	cols = compat.existing_fields(doctype, _GEOMETRY)
	row = dict(frappe.db.get_value(doctype, name, cols, as_dict=True) or {}) if cols else {}
	if row.get("boundary_geojson") and not (row.get("boundary_centroid_lat") or row.get("boundary_centroid_lon")):
		row.update(_derived(row["boundary_geojson"]))
	return row


def _derived(raw) -> dict:
	"""A centroid and area from an outline whose derived columns were never written (no shapely needed)."""
	from . import geo

	try:
		shape = json.loads(raw) if isinstance(raw, str) else raw
		if isinstance(shape, dict) and shape.get("type") == "Feature":
			shape = shape.get("geometry")
		polygons = [shape["coordinates"]] if shape.get("type") == "Polygon" else list(shape.get("coordinates") or [])
		ring = [pt for polygon in polygons for pt in (polygon[0] if polygon else [])]
		if not ring:
			return {}
		return {"boundary_centroid_lat": sum(p[1] for p in ring) / len(ring),
		        "boundary_centroid_lon": sum(p[0] for p in ring) / len(ring),
		        "area_computed_acres": geo.area_acres(shape)}
	except (ValueError, TypeError, KeyError, AttributeError, IndexError):
		return {}


def point(task: str, locations: list | None = None) -> tuple:
	"""(latitude, longitude, basis) — the explicit pin, else the combined centroid, else (None, None, None)."""
	spot = work_spot(task)
	if spot and spot.get("latitude") is not None:
		return float(spot["latitude"]), float(spot["longitude"]), "pin"
	weighted, plain = [], []
	for loc in locations if locations is not None else rows(task):
		geo = _geometry(loc["location_type"], loc["location"])
		lat = float(geo.get("boundary_centroid_lat") or 0)
		lon = float(geo.get("boundary_centroid_lon") or 0)
		if not (lat or lon):
			lat, lon = float(geo.get("gps_latitude") or 0), float(geo.get("gps_longitude") or 0)
		if not (lat or lon):
			continue
		area = float(geo.get("area_computed_acres") or 0) if geo.get("boundary_geojson") else 0
		(weighted if area > 0 else plain).append((lat, lon, area))
	if weighted and not plain:
		total = sum(a for _, _, a in weighted)
		return (sum(la * a for la, _, a in weighted) / total, sum(lo * a for _, lo, a in weighted) / total,
		        "area-weighted centroid")
	points = weighted + plain
	if not points:
		return None, None, None
	return (sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points),
	        "centroid" if len(points) == 1 else "mean of centroids")


def outline(task: str, locations: list | None = None) -> dict | None:
	"""The union of the locations' outlines as one GeoJSON MultiPolygon (parts kept separate), or None."""
	polygons = []
	for loc in locations if locations is not None else rows(task):
		raw = _geometry(loc["location_type"], loc["location"]).get("boundary_geojson")
		try:
			shape = json.loads(raw) if isinstance(raw, str) and raw.strip() else raw
		except ValueError:
			continue
		if isinstance(shape, dict) and shape.get("type") == "Feature":
			shape = shape.get("geometry")
		if not isinstance(shape, dict):
			continue
		if shape.get("type") == "Polygon":
			polygons.append(shape.get("coordinates"))
		elif shape.get("type") == "MultiPolygon":
			polygons.extend(shape.get("coordinates") or [])
	return {"type": "MultiPolygon", "coordinates": polygons} if polygons else None


def describe(task: str, *, with_outline: bool = False) -> dict:
	locations = rows(task)
	latitude, longitude, basis = point(task, locations)
	out = {"locations": locations, "location_count": len(locations), "work_spot": work_spot(task),
	       "map_point": {"latitude": latitude, "longitude": longitude, "basis": basis} if basis else None}
	if with_outline:
		out["outline_geojson"] = outline(task, locations)
	return out


def tasks_at(doctype: str, name: str) -> list:
	"""Every task with this place in its location list (primary or not)."""
	names = set(frappe.db.get_all(TASK, filters={"location_doctype": doctype, "location": name}, pluck="name") or [])
	if ready():
		names |= set(frappe.db.get_all(CHILD, filters={"parenttype": TASK, "location_doctype": doctype,
		                                               "location": name}, pluck="parent") or [])
	return sorted(names)


# ── writing ─────────────────────────────────────────────────────────────────
def _label(doctype: str, name: str) -> str | None:
	field = LABEL_FIELD.get(doctype)
	if not field or not compat.has_field(doctype, field):
		return None
	return (str(frappe.db.get_value(doctype, name, field) or "")[:140]) or None


def _resolve(entry, company: str) -> list:
	"""[(doctype, name)] for one entry: {location_type|location_doctype, location} or a bare name / ticker / alias."""
	if isinstance(entry, dict):
		doctype = str(entry.get("location_type") or entry.get("location_doctype") or entry.get("doctype") or "").strip()
		name = str(entry.get("location") or entry.get("name") or "").strip()
		if not name:
			raise LocationError("a location needs a name.")
		if doctype:
			if doctype not in REGISTERS:
				raise LocationError(f"a task's location is a {', '.join(REGISTERS)} — not {doctype!r}.")
			if doctype == "Field" and not frappe.db.exists("Field", name):
				try:
					return [("Field", f) for f in field_names.resolve_many(name, [company] if company else None)]
				except ValueError as exc:
					raise LocationError(str(exc)) from None
			if not frappe.db.exists(doctype, name):
				raise LocationError(f"no {doctype} called {name!r} on this site.")
			return [(doctype, name)]
		entry = name
	text = str(entry or "").strip()
	if not text:
		raise LocationError("a location needs a name.")
	found = field_names.find(text, [company] if company else None)
	if found["resolved"]:
		return [("Field", f) for f in found["resolved"]]
	if found["candidates"]:
		listed = "; ".join(c["field"] for c in found["candidates"])
		raise LocationError(f"'{text}' is more than one block — {listed}. Name one of them.")
	for doctype in ("Field", "Asset Register", "Parcel"):
		# Exact docnames, unscoped — so another company's block is refused by `_check` with its owner named,
		# rather than reported as not existing.
		if compat.doctype_exists(doctype) and frappe.db.exists(doctype, text):
			return [(doctype, text)]
	raise LocationError(f"nothing called '{text}' — no block (by name, ticker or alias), tag or parcel.")


def _check(doctype: str, name: str, company: str) -> None:
	owner_field = COMPANY_FIELD[doctype]
	owner = frappe.db.get_value(doctype, name, owner_field) if compat.has_field(doctype, owner_field) else None
	if company and owner and owner != company:
		raise LocationError(f"{doctype} {name} belongs to {owner}, and this task is {company}'s. A task covers its own "
		                    "company's ground.")
	if doctype == "Asset Register" and compat.has_field(doctype, "retired_at") and \
			frappe.db.get_value(doctype, name, "retired_at"):
		raise LocationError(f"tag {name} is retired.")


def resolve_all(locations, company: str) -> list:
	"""[(doctype, name)] for a whole `locations` argument — resolved, checked and de-duplicated. Raises LocationError."""
	raw = locations if isinstance(locations, (list, tuple)) else [locations]
	if not raw:
		raise LocationError("locations is empty. A task covers at least one place.")
	resolved: list = []
	for entry in raw:
		for doctype, name in _resolve(entry, company):
			_check(doctype, name, company)
			if (doctype, name) not in resolved:
				resolved.append((doctype, name))
	if len(resolved) > MAX_LOCATIONS:
		raise LocationError(f"{len(resolved)} places is more than {MAX_LOCATIONS}; split the job.")
	return resolved


def parse_pin(pin) -> tuple | None:
	if pin is None:
		return None
	try:
		if isinstance(pin, dict):
			return pins._point(pin.get("latitude"), pin.get("longitude"))
		return pins._point(*pin)
	except (pins.PinError, TypeError) as exc:
		raise LocationError(str(exc)) from None


def set_locations(task: str, locations=None, *, pin=None, clear_pin: bool = False) -> dict:
	"""Replace a task's locations (when `locations` is given) and/or its explicit pin. Validated; all or nothing."""
	if not ready():
		raise LocationError("this site has not migrated to v0.323.0 yet (Farm Task Location is missing).")
	if not frappe.db.exists(TASK, task):
		raise LocationError(f"no Farm Task called {task!r}.")
	doc = frappe.get_doc(TASK, task)
	if int(doc.get("docstatus") or 0) == 2 or doc.get("state") in CLOSED_STATES:
		raise LocationError(f"{task} is {doc.get('state') or 'cancelled'}; where a closed job was is history and is "
		                    "not moved.")
	company = str(doc.get("company") or "")
	before = rows(task)
	resolved = resolve_all(locations, company) if locations is not None else []
	point_ = parse_pin(pin)

	if resolved:
		doc.set(TABLE, [{"location_doctype": d, "location": n, "label": _label(d, n)} for d, n in resolved])
		doc.location_doctype, doc.location = resolved[0]
		if compat.has_field(TASK, COUNT):
			doc.set(COUNT, len(resolved))
		doc.save(ignore_permissions=True)
	if point_ is not None:
		first_field = next((n for d, n in (resolved or [(r["location_type"], r["location"]) for r in before])
		                    if d == "Field"), "")
		pins.put(TASK, task, PIN_KIND, *point_, label=f"Work spot — {doc.get('task_name') or task}"[:140],
		         field=first_field, company=company)
		if compat.has_field(TASK, SPOT_FLAG):
			frappe.db.set_value(TASK, task, SPOT_FLAG, 1, update_modified=False)
	elif clear_pin:
		pins.clear(TASK, task, PIN_KIND)
		if compat.has_field(TASK, SPOT_FLAG):
			frappe.db.set_value(TASK, task, SPOT_FLAG, 0, update_modified=False)
	return {"task": task, "before": before, **describe(task, with_outline=False)}

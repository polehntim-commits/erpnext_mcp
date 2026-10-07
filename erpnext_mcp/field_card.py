# SPDX-License-Identifier: MIT
"""What a tap on a block shows: the card. v0.269.0 (docs/contracts/field_self_service_v0_269.yaml).

Name, aliases, acreage (and where it comes from), crop and variety, open tasks, the hazards inside its outline (valves
and hazard pins) and the recent history. Pure-Python point-in-polygon, so it answers on a bench without shapely.
"""

from __future__ import annotations

import json

import frappe

from . import compat, field_names, history

FIELD = "Field"
HAZARD_TYPES = ("Irrigation Valve", "Hazard Marker")
OPEN_LIMIT = 25


def _rings(geojson) -> list:
	try:
		shape = json.loads(geojson) if isinstance(geojson, str) else (geojson or {})
	except (TypeError, ValueError):
		return []
	if shape.get("type") == "Feature":
		shape = shape.get("geometry") or {}
	if shape.get("type") == "Polygon":
		return [shape["coordinates"]]
	if shape.get("type") == "MultiPolygon":
		return list(shape["coordinates"])
	return []


def _inside_ring(lon, lat, ring) -> bool:
	inside = False
	j = len(ring) - 1
	for i in range(len(ring)):
		xi, yi = ring[i][0], ring[i][1]
		xj, yj = ring[j][0], ring[j][1]
		if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi:
			inside = not inside
		j = i
	return inside


def contains(geojson, lat, lon) -> bool:
	"""Inside the outer ring and outside every hole, for any polygon of a (multi)polygon."""
	for polygon in _rings(geojson):
		if polygon and _inside_ring(lon, lat, polygon[0]) and not any(_inside_ring(lon, lat, h) for h in polygon[1:]):
			return True
	return False


def hazards(field: str, boundary=None) -> list:
	"""Valves and hazard pins standing inside the block's outline."""
	if boundary is None:
		boundary = frappe.db.get_value(FIELD, field, "boundary_geojson")
	if not boundary or not compat.doctype_exists("Asset Register"):
		return []
	cols = compat.existing_fields("Asset Register", ("name", "asset_type", "gps_latitude", "gps_longitude",
	                                                "description", "valve_type", "retired_at"))
	rows = frappe.db.get_all("Asset Register", filters={"asset_type": ("in", list(HAZARD_TYPES))}, fields=cols,
	                         limit=5000) or []
	out = []
	for r in rows:
		if r.get("retired_at") or r.get("gps_latitude") in (None, "") or r.get("gps_longitude") in (None, ""):
			continue
		lat, lon = float(r["gps_latitude"]), float(r["gps_longitude"])
		if contains(boundary, lat, lon):
			out.append({"name": r["name"], "asset_type": r.get("asset_type"), "valve_type": r.get("valve_type") or None,
			            "latitude": lat, "longitude": lon, "description": r.get("description") or None})
	return sorted(out, key=lambda h: h["name"])


def open_tasks(field: str) -> list:
	if not compat.doctype_exists("Farm Task"):
		return []
	cols = compat.existing_fields("Farm Task", ("name", "task_name", "task_type", "state", "urgency"))
	return [dict(r) for r in frappe.db.get_all(
		"Farm Task", filters={"location_doctype": FIELD, "location": field,
		                      "state": ("not in", sorted(history.TASK_DONE))},
		fields=cols, order_by="creation desc", limit=OPEN_LIMIT) or []]


def card(field: str, *, include_sensitive=False, history_limit=50) -> dict:
	cols = compat.existing_fields(FIELD, ("name", "field_name", "aliases", "parcel", "owning_entity", "acreage",
	                                      "acreage_source", "area_computed_acres", "crop", "variety", "boundary_geojson"))
	row = dict(frappe.db.get_value(FIELD, field, cols, as_dict=True) or {})
	if not row:
		raise ValueError(f"no block {field!r}.")
	season = int(str(frappe.utils.today())[:4])
	recent = history.field_history(field, from_date=f"{season - 1}-01-01", limit=history_limit,
	                               include_sensitive=include_sensitive)
	return {
		"field": row["name"],
		"field_name": row.get("field_name"),
		"aliases": field_names.aliases_of(row),
		"parcel": row.get("parcel"),
		"owning_entity": row.get("owning_entity"),
		"acreage": row.get("acreage"),
		"acreage_source": row.get("acreage_source") or None,
		"area_computed_acres": row.get("area_computed_acres"),
		"crop": row.get("crop"),
		"variety": row.get("variety"),
		"has_boundary": bool(row.get("boundary_geojson")),
		"open_tasks": open_tasks(row["name"]),
		"hazards": hazards(row["name"], row.get("boundary_geojson")),
		"history": recent,
	}

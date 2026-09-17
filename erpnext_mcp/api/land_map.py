# SPDX-License-Identifier: MIT
"""The land planning map's server side: what to draw, what a drawn line means, where it is saved.

v0.171.0. `/app/land-map` is a Desk page where somebody traces a proposed lot
line on satellite imagery and gets back what a surveyor needs to price the job:
bearings, distances, acreage, closure, a draft metes-and-bounds description, and
the easements the line runs through.

────────────────────────────────────────────────────────────────────────────
THREE METHODS, AND EVERY ONE OF THEM REUSES SOMETHING THAT ALREADY EXISTS
────────────────────────────────────────────────────────────────────────────

`land_map` calls `farm_overview.farm_overview` for the parcels, the fields and
the zones — the same whitelisted method `/app/farm-overview` has used since
v0.110.0, with its entity picker, its permission refusals and its "this boundary
will not parse" reporting intact. This module adds ONE layer that page does not
draw: the county tax lots, which are the unit a lot line adjustment is written
in. Nothing about the farm map is re-implemented here.

`survey_preview` is `surveying.py` plus the records. The arithmetic lives there
and is tested there; this method's job is to decide WHICH records a drawn line
should be tied to and measured against, under the caller's own read permissions.

`save_proposal` writes through `tools/land.lla_create` and `lla_update` and not
through `frappe.get_doc`. Those tools carry the role gate, the status machine,
the after-submit field lock and the party checks that `land_adjustment.py`
argues at length; a second write path in a Desk method would be a way around
every one of them. It is the same decision `api/gis.save_boundary` made for
boundaries, for the same reason.

────────────────────────────────────────────────────────────────────────────
WHAT IT REFUSES TO INVENT
────────────────────────────────────────────────────────────────────────────

A DRAWN LINE IS NOT A SURVEY, and `surveying.DISCLAIMER` travels with every
description this module returns.

BEFORE-AND-AFTER ACREAGE IS REPORTED AS A PAIR OF POSSIBILITIES, not as a
result. A polygon drawn across a boundary does not say which side is giving the
ground up, and this module will not guess: each affected lot comes back with the
acreage it has now, the acreage of the overlap, and what it would hold if it
GAVE that piece and if it RECEIVED it. Where the adjustment already exists as a
record, `land_adjustment.side_plan` answers the question properly from the
pieces and their parties, and that answer is returned alongside.

AN OVERLAP NEEDS shapely, and without it the overlap acreage is `None` with the
reason attached rather than a number computed some other way. `geo.available()`
is the app's existing answer to that question.
"""

from __future__ import annotations

import json

import frappe

from .. import compat, farm_overview, geo, land_adjustment, surveying
from ..errors import ToolError
from ..tools import land as land_tools
from .gis import speaks_frappe

COUNTY_TAX_LOT = land_adjustment.COUNTY_TAX_LOT
LOT_LINE_ADJUSTMENT = land_adjustment.LOT_LINE_ADJUSTMENT

#: How many tax lots and adjustments one answer will carry. The farm map's own
#: cap is `farm_overview.DRAW_CAP` and this matches it: a page that silently
#: stopped drawing at some other number would be two different maps.
DRAW_CAP = farm_overview.DRAW_CAP

#: The colour the page draws tax lots in. The parcels, fields and zones keep the
#: colours `farm_overview.LAYERS` gives them — a lot line adjustment is read
#: against the county's lots, so those are the layer that must look different.
TAX_LOT_COLOUR = "#b8860b"


def _may_read(doctype: str) -> bool:
	return farm_overview._may_read(doctype)


def _points(value) -> list:
	"""The drawn line as a list of `[lon, lat]`, from whatever the browser sent.

	Accepts a JSON string, a list of pairs, a list of `{"lat", "lng"}` (which is
	what Leaflet hands back), a Feature or a geometry. One reader rather than
	four call sites each guessing.
	"""
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except ValueError as error:
			raise ToolError(f"the drawn shape is not JSON: {error}") from None
	if isinstance(value, dict):
		geometry = value.get("geometry") if value.get("type") == "Feature" else value
		if isinstance(geometry, dict) and geometry.get("coordinates") is not None:
			return surveying.geo_points(geometry)
		raise ToolError("the drawn shape carries no coordinates.")
	out = []
	for item in value or []:
		if isinstance(item, dict):
			longitude, latitude = item.get("lng", item.get("lon")), item.get("lat")
		elif isinstance(item, (list, tuple)) and len(item) >= 2:
			longitude, latitude = item[0], item[1]
		else:
			raise ToolError(f"{item!r} is not a coordinate pair.")
		try:
			out.append([round(float(longitude), 7), round(float(latitude), 7)])
		except (TypeError, ValueError):
			raise ToolError(f"{item!r} is not a coordinate pair.") from None
	if len(out) < 2:
		raise ToolError("draw at least two points before asking what they mean.")
	return out


# ── the tax lot layer ───────────────────────────────────────────────────────
def _tax_lots() -> dict:
	"""Every County Tax Lot this login may read, as shapes the page can draw.

	A tax lot belongs to no company on this site — it is the county's statement
	about a piece of ground, cached — so it is NOT narrowed by the entity picker
	the way the parcels are. That is the point of the layer: the lot on the other
	side of the line is somebody else's, and an adjustment is an agreement with
	them.
	"""
	if not compat.doctype_exists(COUNTY_TAX_LOT):
		return {"shapes": [], "count": 0, "refused": False, "unreadable": []}
	if not _may_read(COUNTY_TAX_LOT):
		return {"shapes": [], "count": 0, "refused": True, "unreadable": []}

	rows = (
		frappe.db.get_all(
			COUNTY_TAX_LOT,
			fields=[
				"name",
				"map_taxlot",
				"county",
				"account",
				"owner_of_record",
				"situs",
				"acres_gis",
				"geometry",
			],
			limit=DRAW_CAP,
			order_by="modified desc",
		)
		or []
	)

	shapes, unreadable = [], []
	for row in rows:
		geometry, reason = farm_overview.parse_geometry(row.get("geometry"))
		name = str(row.get("name") or "")
		if reason:
			unreadable.append({"doctype": COUNTY_TAX_LOT, "name": name, "label": name, "reason": reason})
			continue
		if not geometry:
			continue
		detail = " · ".join(
			part
			for part in (
				f"Account {row.get('account')}" if row.get("account") else "",
				str(row.get("owner_of_record") or ""),
				f"{float(row.get('acres_gis') or 0):g} ac GIS" if row.get("acres_gis") else "",
			)
			if part
		)
		shapes.append(
			{
				"doctype": COUNTY_TAX_LOT,
				"name": name,
				"label": str(row.get("map_taxlot") or name),
				"route": farm_overview._route(COUNTY_TAX_LOT, name),
				"county": row.get("county") or None,
				"account": row.get("account") or None,
				"owner": row.get("owner_of_record") or None,
				"situs": row.get("situs") or None,
				"acres": float(row.get("acres_gis") or 0) or None,
				"geometry": geometry,
				"centre": farm_overview._middle(geometry),
				"detail": detail,
			}
		)
	return {"shapes": shapes, "count": len(rows), "refused": False, "unreadable": unreadable}


def _adjustments() -> list:
	"""The lot line adjustments this login may read, with their mapped corridors."""
	if not compat.doctype_exists(LOT_LINE_ADJUSTMENT) or not _may_read(LOT_LINE_ADJUSTMENT):
		return []
	rows = (
		frappe.db.get_all(
			LOT_LINE_ADJUSTMENT,
			fields=[
				"name",
				"title",
				"status",
				"docstatus",
				"lot_1",
				"lot_2",
				"proposed_geometry",
				"easement_geometry",
				"generated_legal_description",
			],
			limit=DRAW_CAP,
			order_by="modified desc",
		)
		or []
	)
	out = []
	for row in rows:
		proposed, proposed_reason = farm_overview.parse_geometry(row.get("proposed_geometry"))
		out.append(
			{
				"name": row.get("name"),
				"title": row.get("title"),
				"status": row.get("status"),
				"docstatus": int(row.get("docstatus") or 0),
				"lots": [lot for lot in (row.get("lot_1"), row.get("lot_2")) if lot],
				"route": f"/app/lot-line-adjustment/{row.get('name')}",
				"proposed_geometry": proposed,
				"proposed_unreadable": proposed_reason,
				"has_description": bool(str(row.get("generated_legal_description") or "").strip()),
				"easements": _corridors(row.get("name"), row.get("easement_geometry")),
			}
		)
	return out


def _corridors(adjustment: str, raw) -> list:
	"""Easement corridors stored on one adjustment, as labelled geometries.

	The field holds a GeoJSON FeatureCollection so several corridors — an
	irrigation ditch, a shared driveway, a power line — live in one column and
	each keeps its own label. A bare geometry is accepted and gets the
	adjustment's name.
	"""
	if not str(raw or "").strip():
		return []
	try:
		payload = json.loads(raw)
	except ValueError:
		return []
	features = []
	if isinstance(payload, dict) and payload.get("type") == "FeatureCollection":
		features = payload.get("features") or []
	elif isinstance(payload, dict) and payload.get("type") == "Feature":
		features = [payload]
	elif isinstance(payload, dict):
		features = [{"type": "Feature", "geometry": payload, "properties": {}}]

	out = []
	for index, feature in enumerate(features, start=1):
		geometry = (feature or {}).get("geometry")
		if not isinstance(geometry, dict) or not geometry.get("coordinates"):
			continue
		properties = feature.get("properties") or {}
		out.append(
			{
				"adjustment": adjustment,
				"label": str(properties.get("label") or f"{adjustment} easement {index}"),
				"easement_type": str(properties.get("easement_type") or ""),
				"burdened": str(properties.get("burdened") or ""),
				"benefited": str(properties.get("benefited") or ""),
				"geometry": geometry,
			}
		)
	return out


def _reference_shapes(answer: dict, tax_lots: list) -> list:
	"""Everything a point of beginning may be tied to: lots first, then the rest.

	Tax lots lead because a lot line adjustment is written between them and a
	surveyor reading the description will already have the county's map open.
	"""
	references = [
		{"label": shape["label"], "geometry": shape["geometry"], "doctype": COUNTY_TAX_LOT}
		for shape in tax_lots
	]
	for layer in answer.get("layers") or []:
		for shape in layer.get("shapes") or []:
			if shape.get("geometry"):
				references.append(
					{
						"label": shape.get("label") or shape.get("name"),
						"geometry": shape["geometry"],
						"doctype": layer.get("doctype"),
					}
				)
	return references


# ── what the drawn line means ───────────────────────────────────────────────
def _overlaps(points: list, references: list) -> list:
	"""Which mapped shapes the drawn figure falls on, and by how much.

	See the module docstring: this reports what each lot would hold if it GAVE
	the piece and if it RECEIVED it, and never decides which. Without shapely the
	overlap is `None` and carries the sentence that says why.
	"""
	shape = surveying.polygon(points)
	if not shape:
		return []
	have_shapely = geo.available()

	out = []
	for reference in references:
		geometry = reference.get("geometry")
		if not isinstance(geometry, dict):
			continue
		overlap = None
		note = ""
		if have_shapely:
			try:
				from shapely.geometry import mapping
				from shapely.geometry import shape as to_shape

				intersection = to_shape(geometry).buffer(0).intersection(to_shape(shape).buffer(0))
				if intersection.is_empty:
					continue
				overlap = round(geo.area_acres(mapping(intersection)), 4)
			except Exception as error:  # pragma: no cover - a geometry shapely refuses
				note = f"the overlap could not be computed: {type(error).__name__}: {error}"
		else:
			# Without shapely, a cheap containment test still answers "does this
			# lot have anything to do with the drawn piece" — which is the
			# question the page asks first.
			ring = surveying.ring(points)
			rings = surveying._corridor_rings(geometry)
			touches = any(
				any(surveying.point_in_ring(point, ring_points) for point in ring) for ring_points in rings
			)
			if not touches:
				continue
			note = (
				f"this site is missing {geo.requires_sentence()}, so the overlapping acreage "
				"was not computed. The lot is listed because the drawn piece falls inside it."
			)

		before = reference.get("acres")
		out.append(
			{
				"doctype": reference.get("doctype"),
				"label": reference.get("label"),
				"name": reference.get("name"),
				"acres_before": before,
				"overlap_acres": overlap,
				"acres_if_giving": round(before - overlap, 4) if (before and overlap is not None) else None,
				"acres_if_receiving": round(before + overlap, 4)
				if (before and overlap is not None)
				else None,
				"note": note,
			}
		)
	out.sort(key=lambda row: -(row.get("overlap_acres") or 0))
	return out[:20]


def _land_map(company=None) -> dict:
	"""Everything the page draws, in one answer."""
	answer = farm_overview.farm_overview(company=company)
	lots = _tax_lots()
	answer["tax_lots"] = {
		"doctype": COUNTY_TAX_LOT,
		"label": "County Tax Lots",
		"colour": TAX_LOT_COLOUR,
		"shapes": lots["shapes"],
		"count": lots["count"],
		"refused": lots["refused"],
	}
	answer["unreadable"] = list(answer.get("unreadable") or []) + lots["unreadable"]
	answer["adjustments"] = _adjustments()
	answer["may_write_adjustment"] = bool(
		compat.doctype_exists(LOT_LINE_ADJUSTMENT) and frappe.has_permission(LOT_LINE_ADJUSTMENT, "write")
	)
	answer["disclaimer"] = surveying.DISCLAIMER
	answer["geometry_support"] = {
		"shapely": geo.available(),
		"note": "" if geo.available() else geo.requires_sentence(),
	}
	# The tax lots widen the box the page opens on: an adjustment is read against
	# the neighbour's ground, which is exactly the part the farm map leaves out.
	points = []
	for shape in lots["shapes"]:
		points.extend(farm_overview.points_of(shape["geometry"]))
	if points:
		existing = answer.get("bounds")
		box = farm_overview.bounds_of(
			points
			+ ([(existing[0][1], existing[0][0]), (existing[1][1], existing[1][0])] if existing else [])
		)
		answer["bounds"] = box or existing
	return answer


def _survey_preview(points=None, adjustment=None, easements=None, company=None) -> dict:
	"""Courses, closure, acreage, the draft description, and what the line hits."""
	drawn = _points(points)
	answer = farm_overview.farm_overview(company=company)
	lots = _tax_lots()
	references = _reference_shapes(answer, lots["shapes"])

	# The references carry their acreage for the before/after pair; the tie-in
	# only needs their corners.
	measured = [
		{
			"label": shape["label"],
			"name": shape["name"],
			"geometry": shape["geometry"],
			"acres": shape.get("acres"),
			"doctype": COUNTY_TAX_LOT,
		}
		for shape in lots["shapes"]
	]
	for layer in answer.get("layers") or []:
		for shape in layer.get("shapes") or []:
			if shape.get("geometry"):
				measured.append(
					{
						"label": shape.get("label") or shape.get("name"),
						"name": shape.get("name"),
						"geometry": shape["geometry"],
						"acres": shape.get("acres"),
						"doctype": layer.get("doctype"),
					}
				)

	corridors = []
	for row in _adjustments():
		if not adjustment or row["name"] == adjustment:
			corridors.extend(row["easements"])
	if easements:
		# Corridors the page is holding but has not saved yet, so a line can be
		# checked against a ditch before either is written down.
		raw = easements if isinstance(easements, str) else json.dumps(easements)
		corridors.extend(_corridors(str(adjustment or "drawn"), raw))

	tie = surveying.tie_in(drawn[0], references)
	note = ""
	if adjustment and compat.doctype_exists(LOT_LINE_ADJUSTMENT):
		title = frappe.db.get_value(LOT_LINE_ADJUSTMENT, adjustment, "title")
		note = f"Drafted for {adjustment}{f' — {title}' if title else ''}."
	description = surveying.legal_description(drawn, tie.get("description") or "", note)

	out = {
		"points": drawn,
		"geometry": surveying.polygon(drawn),
		"courses": description["courses"],
		"acres": description["acres"],
		"closure": description["closure"],
		"tie_in": tie,
		"legal_description": description["text"],
		"disclaimer": surveying.DISCLAIMER,
		"note": description.get("note") or "",
		"easement_crossings": surveying.crossings(drawn, corridors),
		"easements_considered": len(corridors),
		"affected": _overlaps(drawn, measured),
		"geometry_support": {
			"shapely": geo.available(),
			"note": "" if geo.available() else geo.requires_sentence(),
		},
	}
	if adjustment:
		out["adjustment"] = adjustment
		out["sides"] = _sides(adjustment)
	return out


def _sides(adjustment: str) -> list:
	"""`land_adjustment.side_plan` for both parties, or the reason there is none.

	This is the honest before-and-after: it reads the pieces, their parties and
	their surveyed acreage off the record, which is the only place that intent is
	written down.
	"""
	if not compat.doctype_exists(LOT_LINE_ADJUSTMENT):
		return []
	if not frappe.db.exists(LOT_LINE_ADJUSTMENT, adjustment):
		raise ToolError(f"no Lot Line Adjustment called {adjustment!r}.")
	frappe.has_permission(LOT_LINE_ADJUSTMENT, "read", doc=adjustment, throw=True)
	doc = frappe.get_doc(LOT_LINE_ADJUSTMENT, adjustment)
	out = []
	for which in (land_adjustment.PARTY_1, land_adjustment.PARTY_2):
		try:
			out.append(land_adjustment.side_plan(doc, which))
		except Exception as error:  # pragma: no cover - a half-filled draft
			out.append({"side": which, "warnings": [f"{type(error).__name__}: {error}"]})
	return out


# ── saving ──────────────────────────────────────────────────────────────────
def _save_proposal(
	adjustment=None,
	title=None,
	lot_1=None,
	lot_2=None,
	proposed_geometry=None,
	legal_description=None,
	easement_geometry=None,
) -> dict:
	"""Write the proposal onto a Lot Line Adjustment, through the LLA tools.

	An existing `adjustment` is updated. With no adjustment and a `title`, one is
	created as a Draft — and `lla_create` then applies every check it applies to
	an adjustment made from a console, including the parties. A create with no
	title is refused rather than named something forgettable.
	"""
	fields = {}
	if proposed_geometry is not None:
		fields["proposed_geometry"] = _geojson_text(proposed_geometry, "proposed_geometry")
	if easement_geometry is not None:
		fields["easement_geometry"] = _geojson_text(easement_geometry, "easement_geometry")
	if legal_description is not None:
		fields["generated_legal_description"] = str(legal_description or "")
	if not fields:
		raise ToolError("nothing to save: draw a boundary or generate a description first.")

	if adjustment:
		result = land_tools.lla_update({"name": adjustment, "fields": fields})
		return {
			"name": adjustment,
			"created": False,
			"route": f"/app/lot-line-adjustment/{adjustment}",
			"summary": result.summary,
		}

	if not str(title or "").strip():
		raise ToolError(
			"to create a new adjustment from the map, give it a title. To add this to one that "
			"already exists, choose it in the picker."
		)
	arguments = {"title": str(title).strip(), **fields}
	if lot_1:
		arguments["lot_1"] = lot_1
	if lot_2:
		arguments["lot_2"] = lot_2
	result = land_tools.lla_create(arguments)
	name = result.data.get("name") if isinstance(result.data, dict) else None
	return {
		"name": name,
		"created": True,
		"route": f"/app/lot-line-adjustment/{name}",
		"summary": result.summary,
	}


def _geojson_text(value, label: str) -> str:
	"""A geometry as the JSON string the column holds, validated on the way in."""
	if isinstance(value, str):
		if not value.strip():
			return ""
		try:
			value = json.loads(value)
		except ValueError as error:
			raise ToolError(f"{label} is not JSON: {error}") from None
	if not isinstance(value, dict):
		raise ToolError(f"{label} must be a GeoJSON object.")
	if value.get("type") in ("Polygon", "MultiPolygon", "LineString", "MultiLineString", "Point"):
		geo.validate_geometry(value, label)
	elif value.get("type") not in ("Feature", "FeatureCollection", "GeometryCollection"):
		raise ToolError(f"{label} is not GeoJSON: it has no usable type.")
	return json.dumps(value)


# ── the whitelisted surface: three methods ──────────────────────────────────
@frappe.whitelist()
def land_map(company=None):
	"""Everything /app/land-map draws: the farm map's layers plus the county tax lots."""
	return speaks_frappe(_land_map, company=company)


@frappe.whitelist()
def survey_preview(points=None, adjustment=None, easements=None, company=None):
	"""What a drawn line means: courses, closure, acreage, description, crossings."""
	return speaks_frappe(
		_survey_preview, points=points, adjustment=adjustment, easements=easements, company=company
	)


@frappe.whitelist()
def save_proposal(
	adjustment=None,
	title=None,
	lot_1=None,
	lot_2=None,
	proposed_geometry=None,
	legal_description=None,
	easement_geometry=None,
):
	"""Save the proposal onto a Lot Line Adjustment, through the LLA tools."""
	return speaks_frappe(
		_save_proposal,
		adjustment=adjustment,
		title=title,
		lot_1=lot_1,
		lot_2=lot_2,
		proposed_geometry=proposed_geometry,
		legal_description=legal_description,
		easement_geometry=easement_geometry,
	)

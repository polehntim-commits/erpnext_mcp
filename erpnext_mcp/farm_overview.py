# SPDX-License-Identifier: MIT
"""The whole farm on one map, as a page somebody can open and point at.

v0.110.0. `/app/farm-overview`. THE QUESTION THIS ANSWERS IS THE ONE THAT WAS
ASKED: "is there a place we can go to see the Fields and zones for the whole
farm?" Until now there was not. This app has stored polygons since v0.12.0 and
drawn them since v0.32.0, and every one of those drawings is of ONE record: open
a Field, see that block; open the next Field, see that block. Nothing has ever
put two of them on the same map.

WHICH MEANS THE MISTAKES THAT ONLY SHOW UP BETWEEN RECORDS HAVE NEVER BEEN
VISIBLE. A block traced twice under two names, a zone drawn on the neighbour's
ground, two parcels overlapping by four acres, a cabin whose GPS was typed with
the longitude positive — every one of those is invisible on a form and obvious
on a map of the farm. That is the same argument `geo_map_widget.js` makes for
drawing a single boundary at all, applied to the set rather than the record:
a boundary nobody can look at is a boundary nobody checks.

────────────────────────────────────────────────────────────────────────────
IT IS A READ AND THERE IS NO SAVE PATH OF ANY KIND
────────────────────────────────────────────────────────────────────────────

Not an oversight and not a phase one. `api/gis.save_boundary` exists, it is the
one door a drawn boundary goes through, and it takes a doctype and a docname —
because the thing it does is compare a polygon against ONE record's recorded
acreage and refuse a disagreement past a quarter. A map of forty blocks has no
record in front of it, so a draw tool here would be a draw tool with nothing to
check the shape against. The three forms that can be drawn on are still the
three places a boundary is set, and this page LINKS to them: every popup carries
the route to the record, which is where the editing already lives.

So the whole of this module is four register reads, a JSON parse and a bounding
box.

────────────────────────────────────────────────────────────────────────────
THE FOUR REGISTERS ARE READ THROUGH THEIR OWN TOOLS
────────────────────────────────────────────────────────────────────────────

`list_fields`, `list_irrigation_zones`, `list_parcels` and `list_housing_units`,
which is the same call `api/mobile._location_rows` makes and for the same
reason: each of those tools is the ONE place that says what its register
reports, and a second reader going straight to the columns behind them drifts
the first time one of them grows a derivation. A Field's county is exactly that
— read through its parcel on every call and stored nowhere — and a column reader
would have missed it.

NO `allow_<tool>` SWITCH IS IN PLAY, because the tool FUNCTIONS are called
directly rather than through `mcp.handle`. That is the call `badge_sheet`,
`asset_tag_sheet` and `mobile_onboarding` all made before this one: those
switches are the AI's leash, and a farm that will not let the model list its
blocks should not thereby lose the ability to look at its own map.

ONE COLUMN IS READ OUTSIDE THE TOOLS AND IT IS SAID SO HERE. `_describe_parcel`
deliberately withholds the polygon — "a boundary is a few kilobytes of
coordinates and every list of parcels would carry one per row" — and it is
right, for a register listing. This page is the caller that wants exactly that,
for parcels it has ALREADY been handed by `list_parcels`, so `_parcel_shapes`
fetches the one withheld column by docname. It reads no row the register did not
already return, which is what keeps the scoping the tool applied.

────────────────────────────────────────────────────────────────────────────
THE GATE, AND THE THING IT DOES NOT DO
────────────────────────────────────────────────────────────────────────────

`frappe.has_permission(<register>, "read")` per register, and a register the
caller may not read CONTRIBUTES NOTHING AND IS NAMED. Not a refusal of the whole
page: a farm where the office manager may read Fields and Parcels but not the
housing register should get the map with the buildings missing and a line saying
which layer was withheld, rather than a page that will not open.

THE COMPANY LIST IS FILTERED BY PERMISSION ON THE COMPANY ITSELF, one check per
Company, and that is where a multi-entity farm's scoping actually bites: all
four registers hang off `owning_entity`, so "which entities may this person see"
is the same question as "which Companies may this person read", and a User
Permission on Company is the mechanism an operator already uses to answer it.

WHAT THAT DOES NOT DO, STATED RATHER THAN DISCOVERED: the four tools read with
`frappe.db.get_all`, which does not apply User Permissions to the ROWS. So a
person scoped to one Company sees that Company in the picker and no other, and
the rows behind it are that Company's — but the gate doing the work is the
company filter, not a per-row check. A register whose rows carry no owning
entity at all is visible to anybody who may read the register, which is the same
rule `guard.scoped` applies to a task with no company: ground that names no
entity belongs to the operation rather than to one of its companies.

────────────────────────────────────────────────────────────────────────────
NO SHAPELY, NO H3, AND THAT IS DELIBERATE
────────────────────────────────────────────────────────────────────────────

`geo.parse` would have been the obvious way to turn the stored text into a
shape, and it calls `geo.require()` — shapely, which is an optional-at-import
dependency this app is careful to degrade around. A bench without it loses the
six geospatial TOOLS, which is correct, because those tools compute areas and
containment. Losing the ability to LOOK AT boundaries that are already stored
would not be correct: nothing here computes anything about the geometry beyond
the smallest box that holds it, and `json.loads` plus a walk over the
coordinates does that in the standard library.

A ROW WHOSE STORED TEXT DOES NOT PARSE IS REPORTED, NOT DROPPED. It is the one
row on the whole farm somebody needs to know about — a boundary that silently
does not draw looks exactly like a block that was never traced — so it comes
back in `unreadable` with the docname and the reason.
"""

from __future__ import annotations

import json
from urllib.parse import quote

import frappe

from . import compat, overlays
from .errors import ToolError
from .tools import farm as farm_tools
from .tools import housing as housing_tools
from .tools import realestate as realestate_tools

#: The Page's own name, its route and the key `frappe.pages` is keyed on. One
#: constant because `mobile_onboarding` learned it the hard way: the JSON, the
#: folder name and the string the script keys on are three spellings of one
#: fact, and nothing at runtime notices when they stop agreeing — Frappe renders
#: an empty panel and moves on.
PAGE_ROUTE = "farm-overview"
PAGE_TITLE = "Farm Overview"

FIELD = "Field"
IRRIGATION_ZONE = "Irrigation Zone"
PARCEL = "Parcel"
HOUSING_UNIT = "Housing Unit"
COMPANY = "Company"

#: The three registers that carry a polygon, in the order they are DRAWN — which
#: is largest first, because a Leaflet layer added later sits on top of one added
#: earlier. Parcels are the ground everything else sits on and go down first;
#: zones are the smallest and go on last, so a zone inside a block inside a
#: parcel is still clickable rather than buried under its own parent.
#:
#: THE COLOURS MEAN A REGISTER HERE, WHICH IS THE OPPOSITE OF THE CALL
#: `geo_map_widget.js` MAKES. On a form the layers are one record's shape, its
#: container and a proposal, so colour has to mean "which of these may I
#: change". Nothing on this page is editable and every shape belongs to a
#: different record, so the only question a reader has is which register they are
#: looking at — and the legend says so in words as well.
LAYERS = (
	{
		"doctype": PARCEL,
		"label": "Parcels",
		"colour": "#8250df",
		"fill_opacity": 0.06,
		"weight": 2,
		"dash_array": "6 4",
	},
	{
		"doctype": FIELD,
		"label": "Fields",
		"colour": "#1a7f37",
		"fill_opacity": 0.20,
		"weight": 3,
		"dash_array": None,
	},
	{
		"doctype": IRRIGATION_ZONE,
		"label": "Irrigation zones",
		"colour": "#0969da",
		"fill_opacity": 0.22,
		"weight": 2,
		"dash_array": None,
	},
)

#: Per register: the tool that lists it, the key its rows come back under, the
#: column holding a readable name, and the column holding the acreage a popup
#: prints. A table rather than four branches, so a register that grows a fifth
#: sibling is one entry rather than four edits.
REGISTER_SPECS = {
	FIELD: {
		"module": farm_tools,
		"tool": "list_fields",
		"key": "fields",
		"label": "field_name",
		"acreage": "acreage",
	},
	IRRIGATION_ZONE: {
		"module": farm_tools,
		"tool": "list_irrigation_zones",
		"key": "zones",
		"label": "zone_name",
		"acreage": "area_acres",
	},
	PARCEL: {
		"module": realestate_tools,
		"tool": "list_parcels",
		"key": "parcels",
		"label": "parcel_name",
		"acreage": "acreage",
	},
	HOUSING_UNIT: {
		"module": housing_tools,
		"tool": "list_housing_units",
		"key": "units",
		"label": "unit_name",
		"acreage": None,
	},
}

#: How many rows of one register the page will draw. `farm_tools.REGISTER_CAP` is
#: the app's standard ceiling and the same one the location picker uses; a farm
#: past it gets the cap said out loud rather than a map that quietly stops.
DRAW_CAP = farm_tools.REGISTER_CAP


def _may_read(doctype: str) -> bool:
	"""`frappe.has_permission`, never raising.

	A permission check that threw would take the whole page down over one layer,
	which is the opposite of the per-layer refusal this page is built on.

	v0.116.0 MOVED THE BODY TO `overlays.may_read` AND LEFT THIS AS THE CALL. The
	overlay engine makes the identical promise for the identical reason, and two
	implementations of "never raise" is two chances for one of them to start
	raising. The name stays because it is what the rest of this module reads as.
	"""
	return overlays.may_read(doctype)


def _may_read_doc(doctype: str, name: str) -> bool:
	"""Permission on one specific document, never raising.

	Used only for `Company`, where the number of documents is the number of
	entities a farm has rather than the number of blocks it farms.
	"""
	try:
		return bool(frappe.has_permission(doctype, "read", doc=name))
	except Exception:  # pragma: no cover - a site mid-migrate with no meta
		return False


def readable_companies() -> list:
	"""Every Company on this site the caller may read, in the site's own order.

	THE PICKER AND THE SCOPE ARE ONE LIST. What this returns is both what the
	page draws as options and what `farm_overview` will accept in `company`, so
	an entity that is not offered is also not reachable by typing its name into
	the request.
	"""
	if not _may_read(COMPANY):
		return []
	names = [
		str(row.get("name")) for row in frappe.db.get_all(COMPANY, fields=["name"], order_by="name asc") or []
	]
	return [name for name in names if _may_read_doc(COMPANY, name)]


def _company(requested, allowed: list) -> str:
	"""The entity to draw, or a refusal naming what may be drawn instead.

	AN ENTITY THE CALLER MAY NOT READ IS REFUSED BY NAME rather than quietly
	swapped for one they may. A map silently showing a different farm than the
	one that was asked for is the worst available failure here: every shape on it
	looks perfectly plausible.

	NOTHING ASKED FOR MEANS THE FIRST ENTITY, AND NOT "ALL OF THEM". That is the
	same call `api/mobile._create_one_location` makes — `require_company(...) or
	allowed[0]` — and here it fixes a specific bug rather than merely matching:
	`list_parcels` REQUIRES a company and the other three registers do not, so a
	multi-entity site opened with nothing chosen would draw every block and every
	zone on the site and NO parcels at all, silently, with an empty layer that
	looks exactly like a farm that has not registered its titles. One entity
	drawn, named in a picker the reader can change, is the honest answer.
	"""
	wanted = str(requested or "").strip()
	if not wanted:
		return allowed[0] if allowed else ""
	for name in allowed:
		if name.lower() == wanted.lower():
			return name
	frappe.throw(
		frappe._("{0} is not an entity you may read. This login can see: {1}").format(
			wanted, ", ".join(allowed) or frappe._("none")
		),
		frappe.PermissionError,
		title=frappe._(PAGE_TITLE),
	)


def _rows(doctype: str, company: str) -> list:
	"""One register's described rows, or an empty list.

	A REGISTER THIS SITE HAS NOT INSTALLED CONTRIBUTES NOTHING RATHER THAN
	FAILING THE PAGE, and neither does one whose tool refuses. That is the same
	call `_location_rows` makes: the four tools each refuse a missing doctype by
	name, which is right on a console and wrong here — a farm with no irrigation
	zones registered should get a map with three layers on it, not an error.
	"""
	if not compat.doctype_exists(doctype):
		return []
	spec = REGISTER_SPECS[doctype]
	arguments = {"limit": DRAW_CAP}
	if company:
		arguments["company"] = company
	try:
		result = getattr(spec["module"], spec["tool"])(arguments)
	except ToolError:
		# `list_parcels` REQUIRES a company and the other three do not, so a
		# multi-entity site reached with no entity chosen lands here. One
		# populated layer and two empty ones is a worse answer than a page that
		# says which entity to pick.
		return []
	return list(result.data.get(spec["key"]) or [])


def _parcel_shapes(names: list) -> dict:
	"""`{docname: stored boundary text}` for parcels the register already returned.

	THE ONE COLUMN THIS MODULE READS OUTSIDE A TOOL, and the module docstring
	says why: `_describe_parcel` withholds the polygon on purpose, because a
	register listing that carried one per row would be a few kilobytes of
	coordinates per parcel for a caller that only wanted the names. This page is
	the caller that wants them.

	IT READS NO ROW THE REGISTER DID NOT ALREADY HAND BACK. The filter is the
	list of docnames `list_parcels` returned, which is the scoped set, so this
	cannot widen what the page can see.
	"""
	if not names or not compat.has_field(PARCEL, "boundary_geojson"):
		return {}
	rows = (
		frappe.db.get_all(
			PARCEL,
			filters={"name": ("in", list(names))},
			fields=["name", "boundary_geojson"],
			limit=len(names),
		)
		or []
	)
	return {str(row.get("name")): row.get("boundary_geojson") for row in rows}


def parse_geometry(raw):
	"""A GeoJSON geometry from a stored Long Text field, or a reason it is not one.

	Returns `(geometry, reason)`; exactly one of the two is ever set, and a field
	that is simply empty returns `(None, None)` — an untraced block is not a
	broken one.

	IT ACCEPTS THE THREE SHAPES THE BOUNDARY TOOLS ACCEPT, for the same reason
	they do and with the same order of preference: somebody exporting from QGIS
	gets whichever of a geometry, a Feature or a FeatureCollection the export
	button produced, and all three are already stored on real sites.

	A PURE FUNCTION OVER A STRING. No frappe, no shapely — see the module
	docstring for why the geospatial dependency is deliberately not reached for.
	"""
	text = str(raw or "").strip()
	if not text:
		return None, None
	try:
		parsed = json.loads(text)
	except (TypeError, ValueError) as error:
		return None, f"the stored text is not JSON ({error})"
	if not isinstance(parsed, dict):
		return None, f"the stored JSON is a {type(parsed).__name__}, not a GeoJSON object"

	kind = str(parsed.get("type") or "")
	if kind == "FeatureCollection":
		features = [entry for entry in (parsed.get("features") or []) if isinstance(entry, dict)]
		if len(features) == 1:
			parsed = features[0]
			kind = str(parsed.get("type") or "")
		else:
			# More than one feature, or none. Handed to Leaflet whole rather than
			# picked from: `L.geoJSON` draws a collection perfectly well, and
			# choosing one of several features for somebody would draw half a
			# parcel and say nothing about the other half.
			return (parsed, None) if features else (None, "the FeatureCollection holds no features")
	if kind == "Feature":
		geometry = parsed.get("geometry")
		if not isinstance(geometry, dict):
			return None, "the Feature carries no geometry"
		parsed = geometry
		kind = str(parsed.get("type") or "")

	if not kind:
		return None, "the stored JSON has no type"
	if parsed.get("coordinates") is None and kind != "GeometryCollection":
		return None, f"the {kind} carries no coordinates"
	return parsed, None


def _walk(coordinates, out: list) -> None:
	"""Every `[lon, lat]` pair anywhere inside a nest of coordinate arrays.

	GeoJSON nests a Point one deep, a Polygon three and a MultiPolygon four, and
	this page has to bound all of them without caring which it was handed.

	`[lon, lat]` AND NOT `[lat, lon]`. GeoJSON is longitude first and every other
	pair in this app is latitude first; getting it round the wrong way produces a
	bounding box somewhere off the coast of Somalia rather than an error, which
	is the same trap `api/gis._query_county_parcels` comments at length.
	"""
	if not isinstance(coordinates, (list, tuple)):
		return
	if (
		len(coordinates) >= 2
		and isinstance(coordinates[0], (int, float))
		and isinstance(coordinates[1], (int, float))
		and not isinstance(coordinates[0], bool)
		and not isinstance(coordinates[1], bool)
	):
		out.append((float(coordinates[1]), float(coordinates[0])))
		return
	for entry in coordinates:
		_walk(entry, out)


def points_of(geometry) -> list:
	"""Every `(lat, lon)` in one geometry, Feature or FeatureCollection."""
	if not isinstance(geometry, dict):
		return []
	kind = str(geometry.get("type") or "")
	out: list = []
	if kind == "FeatureCollection":
		for feature in geometry.get("features") or []:
			out.extend(points_of(feature))
		return out
	if kind == "Feature":
		return points_of(geometry.get("geometry"))
	if kind == "GeometryCollection":
		for entry in geometry.get("geometries") or []:
			out.extend(points_of(entry))
		return out
	_walk(geometry.get("coordinates"), out)
	return out


def _on_earth(latitude, longitude) -> bool:
	"""Whether a pair is a coordinate rather than an unset Float or a typo.

	NULL ISLAND IS REFUSED, the same call `geo_map_widget.point_of` and
	`housing._gps` both make: an unset Float pair reads as `[0, 0]`, which is a
	real place in the Gulf of Guinea — and a map that flies there looks exactly
	like a map showing you where something is.
	"""
	try:
		latitude = float(latitude)
		longitude = float(longitude)
	except (TypeError, ValueError):
		return False
	if not latitude and not longitude:
		return False
	return -90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0


def bounds_of(points: list):
	"""The smallest `[[south, west], [north, east]]` box holding every point.

	`None` when nothing on the farm has a position, which is what tells the page
	to open on `HOME_VIEW` rather than fitting to a box of nothing.

	COORDINATES OFF EARTH ARE DROPPED HERE AND NOT EARLIER. A single vertex typed
	with an extra digit would otherwise stretch the box across a continent and
	draw every real boundary as a dot — the shape is still drawn, because it is
	the record and the record is what somebody has to go and fix, but it does not
	get to decide where the map opens.
	"""
	usable = [(lat, lon) for lat, lon in points if _on_earth(lat, lon)]
	if not usable:
		return None
	latitudes = [entry[0] for entry in usable]
	longitudes = [entry[1] for entry in usable]
	return [
		[round(min(latitudes), 7), round(min(longitudes), 7)],
		[round(max(latitudes), 7), round(max(longitudes), 7)],
	]


def _route(doctype: str, name: str) -> str:
	"""The Desk route of one record. Where a popup sends somebody to edit.

	`quote` AND NOT A BARE CONCATENATION. A Parcel autonames as
	`"<parcel_name> - <abbr>"` and a Field as `"<field_name> - <abbr>"`, so every
	docname on this page has spaces in it and several have a slash — Frappe’s own
	`quoted` does exactly this, and a route built without it lands on the list
	view of the doctype rather than on the record.
	"""
	return "/app/" + doctype.lower().replace(" ", "-") + "/" + quote(str(name or ""), safe="")


def _acres(row: dict, key) -> float | None:
	if not key:
		return None
	value = row.get(key)
	try:
		return round(float(value), 2) if value not in (None, "") else None
	except (TypeError, ValueError):  # pragma: no cover - a register with junk in a Float
		return None


def _shape(doctype: str, row: dict, raw) -> dict:
	"""One register row as a polygon the page can draw, or as a stated reason not to."""
	spec = REGISTER_SPECS[doctype]
	name = str(row.get("name") or "")
	geometry, reason = parse_geometry(raw)
	centroid = row.get("boundary_centroid") or None
	stored = (
		[centroid["lat"], centroid["lon"]]
		if isinstance(centroid, dict) and _on_earth(centroid.get("lat"), centroid.get("lon"))
		else None
	)
	return {
		"doctype": doctype,
		"name": name,
		"label": str(row.get(spec["label"]) or "") or name,
		"route": _route(doctype, name),
		"company": row.get("owning_entity") or None,
		"parcel": row.get("parcel") or None,
		"acres": _acres(row, spec["acreage"]),
		"computed_acres": row.get("area_computed_acres"),
		"geometry": geometry,
		"centroid": stored,
		# WHERE TO PRINT WHEN THERE IS NO MAP TO DRAW ON, which is not the same
		# figure as `centroid` and is deliberately a second key rather than a
		# fallback written into the first.
		#
		# `boundary_centroid` is shapely's centroid, computed by the boundary tool
		# and stored on the record; it is the number every other reader of this
		# app means by "where is this block". A row can be missing it — a polygon
		# pasted straight into the Long Text field bypasses the tools, which
		# `api/gis.py` says out loud is possible — and the fallback table exists
		# precisely to print coordinates, so a blank column there is the one place
		# this page fails at its own job.
		#
		# So the substitute is the MIDDLE OF THE SHAPE'S BOUNDING BOX, which is a
		# different thing from a centroid and is honest for the purpose: it points
		# somebody at the right part of the county. Nothing computes acreage,
		# containment or a geofence from it, and merging it into `centroid` would
		# have put an approximation under the name of a stored measurement.
		"centre": stored or _middle(geometry),
		"unreadable": reason,
		"detail": _detail(doctype, row),
	}


def _middle(geometry):
	"""The centre of a geometry's bounding box, or None. See `_shape`."""
	box = bounds_of(points_of(geometry)) if geometry else None
	if not box:
		return None
	return [round((box[0][0] + box[1][0]) / 2, 7), round((box[0][1] + box[1][1]) / 2, 7)]


def _detail(doctype: str, row: dict) -> str:
	"""The one line under the name in a popup. What a person asks next.

	DIFFERENT PER REGISTER ON PURPOSE, because the four registers are genuinely
	different records — the same call `locations.py` makes about its own detail
	line. What is planted on a block, what waters a zone, which county holds a
	title, how many a cabin sleeps.
	"""
	if doctype == FIELD:
		planted = " ".join(str(part) for part in (row.get("variety"), row.get("crop")) if part)
		return planted or str(row.get("condition") or "")
	if doctype == IRRIGATION_ZONE:
		source = str(row.get("water_source") or "")
		block = str(row.get("field") or "")
		return " · ".join(part for part in (source, block) if part)
	if doctype == PARCEL:
		county = str(row.get("county") or "")
		state = str(row.get("state") or "")
		return ", ".join(part for part in (county, state) if part)
	unit_type = str(row.get("unit_type") or "")
	capacity = row.get("capacity")
	sleeps = frappe._("sleeps {0}").format(capacity) if capacity else ""
	return " · ".join(part for part in (unit_type, sleeps) if part)


def _markers(rows: list) -> list:
	"""Every housing unit somebody has stood at with a phone, as a pin.

	A UNIT WITH NO GPS IS NOT A PIN AT `0, 0` — `housing._gps` already refuses
	null island, so an unlocated cabin arrives here as None and is counted in
	`without_position` instead. The count is the useful half: "eleven cabins, four
	of them nowhere" is a morning's work somebody can go and do.
	"""
	out = []
	for row in rows:
		gps = row.get("gps") or None
		if not isinstance(gps, dict) or not _on_earth(gps.get("lat"), gps.get("lon")):
			continue
		name = str(row.get("name") or "")
		out.append(
			{
				"doctype": HOUSING_UNIT,
				"name": name,
				"label": str(row.get("unit_name") or "") or name,
				"route": _route(HOUSING_UNIT, name),
				"company": row.get("owning_entity") or None,
				"parcel": row.get("parcel") or None,
				"unit_type": row.get("unit_type") or None,
				"point": [round(float(gps["lat"]), 7), round(float(gps["lon"]), 7)],
				"detail": _detail(HOUSING_UNIT, row),
			}
		)
	return out


# ── the one whitelisted method ──────────────────────────────────────────────
@frappe.whitelist()
def farm_overview(company=None, overlay=None) -> dict:
	"""Every boundary and every building this login may read, on one answer.

	ONE CALL AND NOT FIVE. The page draws nothing until it can fit the map to the
	whole farm, and a bounding box over four registers is not a thing four
	separate responses can agree on without the browser holding them all anyway.

	NOTHING HERE IS CACHED. A boundary somebody just traced on a Field form is
	the first thing they will come here to look at, and a page that showed them
	yesterday's shape would be worse than no page — see `on_page_show` in the
	script, which re-reads on every return for the same reason.

	────────────────────────────────────────────────────────────────────────
	v0.116.0: `overlay` — ONE OPERATIONAL LAYER AT A TIME, AND NONE BY DEFAULT
	────────────────────────────────────────────────────────────────────────

	The five layers `overlays.py` computes are what is TRUE of a block right now
	rather than what shape it is, and this page is where a farm looks at them.

	ONE AT A TIME IS THE WHOLE DESIGN AND NOT A PHASE. `overlays.py` argues that
	a screen carrying five overlapping colour schemes is a screen nobody reads
	the one that matters off, and a map is the sharpest case of it: every layer
	wants to colour the same polygon. So the picker is a single choice, the shape
	takes that layer's colour, and the popup carries that layer's sentence.

	NONE BY DEFAULT, because the layers cost queries the boundary map does not —
	the valve log, the restriction register, the observation register — and
	somebody opening this page to check a polygon should not pay for them. The
	OPTIONS are always returned, computed from the caller's roles alone with no
	register read at all, so the picker draws before anything is chosen.

	A LAYER THE CALLER'S ROLES DO NOT SHOW IS REFUSED BY NAME in
	`overlay_refused`, never silently ignored — a picker that accepted a choice
	and drew nothing would read as a farm with no restrictions on it.
	"""
	allowed = readable_companies()
	entity = _company(company, allowed)

	layers = []
	unreadable = []
	refused = []
	points: list = []
	counts = {}

	for spec in LAYERS:
		doctype = spec["doctype"]
		if not _may_read(doctype):
			refused.append(doctype)
			counts[doctype] = 0
			continue
		rows = _rows(doctype, entity)
		shapes_by_name = (
			_parcel_shapes([str(row.get("name") or "") for row in rows]) if doctype == PARCEL else {}
		)
		shapes = []
		unparsed = 0
		for row in rows:
			raw = (
				shapes_by_name.get(str(row.get("name") or ""))
				if doctype == PARCEL
				else row.get("boundary_geojson")
			)
			shape = _shape(doctype, row, raw)
			if shape["unreadable"]:
				unparsed += 1
				unreadable.append(
					{
						"doctype": doctype,
						"name": shape["name"],
						"label": shape["label"],
						"route": shape["route"],
						"reason": shape["unreadable"],
					}
				)
				continue
			if not shape["geometry"]:
				continue
			points.extend(points_of(shape["geometry"]))
			if shape["centroid"]:
				points.append((shape["centroid"][0], shape["centroid"][1]))
			shapes.append(shape)
		counts[doctype] = len(rows)
		layers.append(
			{
				"doctype": doctype,
				"label": spec["label"],
				"colour": spec["colour"],
				"fill_opacity": spec["fill_opacity"],
				"weight": spec["weight"],
				"dash_array": spec["dash_array"],
				"shapes": shapes,
				"drawn": len(shapes),
				"total": len(rows),
				# The gap between the two, which is the number worth printing.
				# "Forty blocks, nine of them never traced" is a job; "thirty-one
				# blocks" is a map that quietly lies about the size of the farm.
				"unreadable": unparsed,
				"without_boundary": len(rows) - len(shapes) - unparsed,
			}
		)

	housing_readable = _may_read(HOUSING_UNIT)
	if not housing_readable:
		refused.append(HOUSING_UNIT)
	units = _rows(HOUSING_UNIT, entity) if housing_readable else []
	markers = _markers(units)
	counts[HOUSING_UNIT] = len(units)
	points.extend((marker["point"][0], marker["point"][1]) for marker in markers)

	return {
		"company": entity or None,
		"companies": allowed,
		"layers": layers,
		"markers": markers,
		"housing": {
			"label": frappe._("Structures"),
			"total": len(units),
			"drawn": len(markers),
			"without_position": len(units) - len(markers),
		},
		"bounds": bounds_of(points),
		"counts": counts,
		"unreadable": unreadable,
		"refused": refused,
		"cap": DRAW_CAP,
		"capped": [doctype for doctype, total in counts.items() if total >= DRAW_CAP],
		"page_route": PAGE_ROUTE,
		**_overlay(entity, overlay),
	}


def _overlay(entity: str, requested) -> dict:
	"""The picker's options, and the one layer that was asked for. See above.

	THE OPTIONS COST NOTHING. `overlays.layers_for` reads the caller's roles and
	no register at all, so a page opened with no layer chosen pays for the four
	boundary reads it already made and not a query more.
	"""
	options = overlays.layers_for(frappe.session.user)
	answer = {
		"overlay_options": options,
		# The picker's own rows: key, label, what it means and which shape it
		# colours. SENT RATHER THAN HELD IN THE SCRIPT, which is the mistake
		# `roles.ROLE_INDICATORS` exists to undo on the handset — a copy of this
		# app's vocabulary compiled into a client goes stale the release a layer
		# is added, and the symptom is a picker that silently cannot reach it.
		"overlay_layers": [
			overlays.LAYER_BY_KEY[key] for key in options["visible"] if key in overlays.LAYER_BY_KEY
		],
		"overlay": None,
		"overlay_refused": [],
	}
	key = str(requested or "").strip().lower()
	if not key:
		return answer
	wanted, refused = overlays.requested_layers([key], options["visible"])
	answer["overlay_refused"] = refused
	if wanted:
		drawn = overlays.build(company=entity, visible=wanted, limit=DRAW_CAP)
		drawn["key"] = wanted[0]
		drawn["subject"] = overlays.LAYER_BY_KEY[wanted[0]]["subject"]
		answer["overlay"] = drawn
	return answer


__all__ = [
	"PAGE_ROUTE",
	"PAGE_TITLE",
	"bounds_of",
	"farm_overview",
	"parse_geometry",
	"points_of",
	"readable_companies",
]

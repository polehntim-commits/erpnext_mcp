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

from . import asset_types, compat, overlays, slope_aspect, slope_grade
from .errors import ToolError
from .tools import asset_tags as asset_tools
from .tools import dispatch as dispatch_tools
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
ASSET_REGISTER = "Asset Register"
FARM_TASK = "Farm Task"

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
	# v0.161.0. Two more registers, read the same way and for the same reason.
	# `Asset Register` autonames from the tag a person reads off the machine, so
	# the docname IS the label — there is no separate name column to point at.
	ASSET_REGISTER: {
		"module": asset_tools,
		"tool": "list_assets",
		"key": "assets",
		"label": "name",
		"acreage": None,
	},
	FARM_TASK: {
		"module": dispatch_tools,
		"tool": "list_dispatch_board",
		"key": "columns",
		"label": "task_name",
		"acreage": None,
	},
}

#: v0.161.0. The glyph a marker carries. LETTERS AND NOT AN ICON FONT, and a
#: `L.divIcon` rather than `L.marker`: the same reasoning `farm_overview.js`
#: gives for drawing structures as circles is that Leaflet's default marker
#: reaches for a sprite at a path relative to its stylesheet, which resolves
#: against the CDN and is one more request that can fail on its own. A glyph this
#: app draws itself cannot 404.
#:
#: v0.162.0. THE GLYPH NOW COMES OFF THE `Farm Asset Type` RECORD and this table
#: holds only the COLOURS. That is the whole point of the register: a farm that
#: adds a Fuel Tank gives it an icon in the Desk and the map draws it, with no
#: release. This table was the fourth of the four disagreeing asset-type lists
#: and it was the shortest — it knew four types out of thirteen, so nine of them
#: shared one grey badge.
#:
#: COLOURS STAY IN CODE AND ARE NOT A COLUMN, deliberately. A palette is a
#: property of the MAP — the five overlay layers and three register colours all
#: have to stay distinguishable from each other and from these — and an operator
#: picking a hex value per type would be choosing one half of a scheme they
#: cannot see the rest of. A type with no colour here takes the default, which is
#: readable against every base layer.
ASSET_COLOURS = {
	"Irrigation Valve": "#0969da",
	"Irrigation Zone": "#218bff",
	"Water Source": "#0a6c74",
	"Tractor": "#9a6700",
	"Implement": "#7d4e00",
	"Sprayer": "#8250df",
	"Vehicle": "#953800",
	"Wind Machine": "#1a7f37",
	"Fuel Tank": "#cf222e",
	"Gas Tank": "#a40e26",
	"Storage": "#6639ba",
	"Cold Storage": "#3192aa",
	"Block": "#4c8c2b",
	"Housing Unit": "#bc4c00",
	"General": "#57606a",
}

#: What an asset type this table does not colour is drawn as.
ASSET_ICON_DEFAULT = {"glyph": "A", "colour": "#57606a", "label": "General"}


def asset_icon(asset_type) -> dict:
	"""The badge one asset type is drawn with: glyph from the register, colour from here.

	A TYPE THIS APP HAS NEVER HEARD OF STILL GETS A PIN, which is the failure
	this function exists to avoid: an operator adds `Cider Press`, registers
	three, and they are silently missing from the map. It gets its own initial
	and the default colour, and `list_asset_types` is where somebody gives it a
	better one.
	"""
	label = str(asset_type or "").strip()
	return {
		"glyph": asset_types.icon_for(label),
		"colour": ASSET_COLOURS.get(label, ASSET_ICON_DEFAULT["colour"]),
		"label": label or ASSET_ICON_DEFAULT["label"],
	}


#: v0.161.0. A task marker's colour, by urgency. THE FOUR THE DOCTYPE DECLARES —
#: `Farm Task.urgency` is a Select of Low/Normal/High/Critical — and the order
#: matters on the map as well as in the legend: a Critical job has to be the one
#: colour that reads at a glance across forty blocks.
#:
#: AN UNKNOWN URGENCY TAKES `Normal`'s COLOUR AND IS NOT PROMOTED. `.get(key, 0)`
#: on an ordering table is the bug `an-unknown-select-option-ranks-first` names:
#: a value the table does not know must never sort ABOVE the ones it does, or a
#: typo in a Select becomes the most urgent thing on the farm.
URGENCY_COLOURS = {
	"Critical": "#cf222e",
	"High": "#bc4c00",
	"Normal": "#0969da",
	"Low": "#6e7781",
}
URGENCY_DEFAULT = "#0969da"

#: Worst first, which is the order the legend prints and the order the markers
#: are added in — a Critical pin sits on top of a Normal one at the same block.
URGENCY_ORDER = ("Critical", "High", "Normal", "Low")

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
	payload = result.data.get(spec["key"])
	if isinstance(payload, dict):
		# v0.161.0. `list_dispatch_board` answers the Kanban's own shape — a dict
		# of state to the tasks in it — because that is what a board is. This
		# page wants the tasks, so the columns are flattened HERE rather than by
		# giving the board a second return shape: the board's grouping is the
		# thing every other caller of it reads.
		out = []
		for entries in payload.values():
			out.extend(entries or [])
		return out
	return list(payload or [])


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
		# v0.161.0. WHAT THE BLOCK IS CALLED ON THE FARM, which is not what it is
		# called in the database. `field_name` is "Yellow Camp Block 3"; the
		# ticker is "YC3" and is what is painted on the bin, said on the radio
		# and written on the tally sheet. It is the label a picker recognises
		# from a map, so it is drawn ON the polygon rather than only in a popup.
		# Absent on every other register, and `None` rather than "" so the script
		# can tell "this register has no ticker" from "this block has not been
		# given one".
		"block_ticker": (str(row.get("block_ticker") or "") or None) if doctype == FIELD else None,
		"crop": (row.get("crop") or None) if doctype == FIELD else None,
		"variety": (row.get("variety") or None) if doctype == FIELD else None,
		"condition": (row.get("condition") or None) if doctype == FIELD else None,
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
	if doctype == ASSET_REGISTER:
		# What it is, then what it is doing. A tag reads `40-5-MPH` and the
		# description is the only line that says which machine that is.
		kind = str(row.get("asset_type") or "")
		description = str(row.get("description") or "")
		return " · ".join(part for part in (kind, description) if part)
	if doctype == FARM_TASK:
		# What kind of job and how it stands. NOT the urgency, which is the
		# marker's own colour and would be one fact said twice.
		kind = str(row.get("task_type") or "")
		state = str(row.get("state") or "")
		return " · ".join(part for part in (kind, state) if part)
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


def _asset_markers(rows: list) -> list:
	"""Every tagged asset somebody has stood at with a phone, as a typed pin.

	THE PAIR IS CHECKED AND NOT EACH AXIS, exactly as `_markers` does for a
	cabin. `_describe_asset` already turns a stored 0.0 into None — every Frappe
	Float column is `NOT NULL DEFAULT 0`, so an asset nobody has located reads
	back 0.0 and would otherwise be a pin in the Gulf of Guinea — and
	`_on_earth` refuses null island a second time for the rows that arrive from
	somewhere else. An asset with no position is COUNTED, not dropped: "ninety
	tags, sixty of them nowhere" is a morning's work with a phone.

	A RETIRED ASSET IS NOT HERE AT ALL, and that is `list_assets`' doing rather
	than this function's — its default filter is `retired_at is not set`. A map
	of what is on the farm should not carry the tractor that was sold.
	"""
	out = []
	for row in rows:
		latitude = row.get("gps_latitude")
		longitude = row.get("gps_longitude")
		if not _on_earth(latitude, longitude):
			continue
		name = str(row.get("name") or "")
		asset_type = str(row.get("asset_type") or "")
		icon = asset_icon(asset_type)
		out.append(
			{
				"doctype": ASSET_REGISTER,
				"name": name,
				"label": name,
				"route": _route(ASSET_REGISTER, name),
				"company": row.get("company") or None,
				"asset_type": asset_type or None,
				"icon": icon["glyph"],
				"colour": icon["colour"],
				"icon_label": icon["label"],
				"point": [round(float(latitude), 7), round(float(longitude), 7)],
				"description": row.get("description") or None,
				# THE READABLE HALF OF A JSON COLUMN, THROUGH THE FUNCTION THAT
				# ALREADY OWNS THAT READING. `current_state` is a JSON blob — a
				# valve's open/closed, a sprayer's full/empty — and the word a
				# person wants is under its `state` key.
				# `asset_tags._current_state_value` is what `scan_asset` and the
				# action menu both read it with, and pulling `.get("state")` out
				# here instead would be a third implementation of one extraction,
				# in the module whose whole docstring is about not becoming a
				# second reader of somebody else's register.
				"current_state": asset_tools._current_state_value(row.get("current_state")) or None,
				# The blob as well, because a valve carries more than the word —
				# a popup prints the word and a client that wants the rest has it
				# without a second call.
				"state_detail": row.get("current_state") or None,
				"last_scan_at": row.get("last_scan_at") or None,
				"last_scan_by": row.get("last_scan_by") or None,
				"last_service_date": row.get("last_service_date") or None,
				"service_interval_days": row.get("service_interval_days"),
				"service_interval_hours": row.get("service_interval_hours"),
				"current_hours": row.get("current_hours"),
				"parent_asset": row.get("parent_asset") or None,
				"detail": _detail(ASSET_REGISTER, row),
			}
		)
	return out


def _placements(layers: list, markers: list) -> dict:
	"""`{(doctype, docname): [lat, lon]}` for everything this answer already drew.

	WHERE A TASK'S PIN COMES FROM, AND WHY IT COSTS NO QUERY. `Farm Task.location`
	is a Dynamic Link over `location_doctype`, so a task points at a Field, a
	Parcel, an Irrigation Zone or a Housing Unit — and this response has just
	finished computing a centre for every one of those that the caller may read.
	Reading the registers again to place the tasks would be four more queries for
	coordinates already in hand, and worse, it would be a SECOND answer to "where
	is this block" that could disagree with the polygon drawn underneath it.

	`centre` AND NOT `centroid`, deliberately. `_shape` keeps the two apart: the
	first is the stored centroid where there is one and the middle of the
	bounding box where there is not. A task pin is a "go here" and the bounding
	box middle is honest for that; nothing computes acreage or containment from
	it.
	"""
	places = {}
	for layer in layers:
		for shape in layer["shapes"]:
			if shape.get("centre"):
				places[(layer["doctype"], shape["name"])] = shape["centre"]
	for marker in markers:
		places[(marker["doctype"], marker["name"])] = marker["point"]
	return places


def _task_markers(rows: list, places: dict) -> tuple:
	"""Open tasks as pins on the ground they are about, and a count of the rest.

	THREE WAYS A TASK HAS NOWHERE TO GO, AND THEY ARE DIFFERENT FACTS. It names
	no location at all; it names one in a register this login may not read or
	this site has not installed; or it names one that has never been traced or
	stood at. The first is a dispatch gap, the second is a permission, the third
	is a boundary somebody owes — so they are counted apart rather than summed
	into one "not shown" that answers none of the three.

	WORST FIRST, WHICH IS THE READING ORDER AND NOT THE DRAWING ORDER. The list
	comes back sorted by `URGENCY_ORDER` because that is what a list wants: the
	fallback table prints it straight through and the Critical jobs are at the
	top of it. A MAP wants the reverse — several jobs on one block share a
	centroid exactly, Leaflet draws later markers over earlier ones, and the pin
	that has to be on top is the urgent one — so `farm_overview.js` walks this
	list backwards for the markers alone. Sorting it the other way here would
	have fixed the map and quietly buried every Critical job at the bottom of the
	table somebody reads when there is no map at all.
	"""
	placed = []
	unlocated = 0
	unplaceable = []
	for row in rows:
		doctype = str(row.get("location_doctype") or "")
		name = str(row.get("location") or "")
		if not doctype or not name:
			unlocated += 1
			continue
		point = places.get((doctype, name))
		if not point:
			unplaceable.append(
				{
					"name": str(row.get("name") or ""),
					"task_name": row.get("task_name") or None,
					"location_doctype": doctype,
					"location": name,
					"route": _route(FARM_TASK, str(row.get("name") or "")),
				}
			)
			continue
		urgency = str(row.get("urgency") or "Normal")
		placed.append(
			{
				"doctype": FARM_TASK,
				"name": str(row.get("name") or ""),
				"label": str(row.get("task_name") or "") or str(row.get("name") or ""),
				"route": _route(FARM_TASK, str(row.get("name") or "")),
				"company": row.get("company") or None,
				"point": point,
				"urgency": urgency,
				# NOT `.get(urgency, first)`. An urgency this table does not know
				# takes Normal's colour rather than the top of the list — a typo
				# in a Select must not become the most urgent thing on the farm.
				"colour": URGENCY_COLOURS.get(urgency, URGENCY_DEFAULT),
				"state": row.get("state") or None,
				"task_type": row.get("task_type") or None,
				"assigned_to": row.get("assigned_to") or None,
				"assigned_to_name": row.get("assigned_to_name") or None,
				"skill_required": row.get("skill_required") or None,
				"location_doctype": doctype,
				"location": name,
				"location_route": _route(doctype, name),
				"detail": _detail(FARM_TASK, row),
			}
		)
	placed.sort(
		key=lambda task: (
			-_urgency_rank(task["urgency"]),
			str(task["name"]),
		)
	)
	return placed, unlocated, unplaceable


def _urgency_rank(urgency) -> int:
	"""How far up the list one urgency sits. An unknown one ranks LAST.

	`URGENCY_ORDER.index` would raise on a value the table does not know, and a
	`.get(key, 0)` style default would rank it FIRST — which is
	`an-unknown-select-option-ranks-first`, and on this page it would put an
	unrecognised urgency on top of every Critical pin at the same block.
	"""
	order = list(reversed(URGENCY_ORDER))
	return order.index(urgency) if urgency in order else -1


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

	# v0.161.0. THE TWO REGISTERS THAT ARE ON THE GROUND WITHOUT BEING SHAPED.
	# Both are pins rather than polygons and both are gated per register exactly
	# as the four above are: a login that may not read the asset register gets
	# the map without the machines and a line saying so, not a page that will
	# not open.
	assets_readable = _may_read(ASSET_REGISTER)
	if not assets_readable:
		refused.append(ASSET_REGISTER)
	asset_rows = _rows(ASSET_REGISTER, entity) if assets_readable else []
	assets = _asset_markers(asset_rows)
	counts[ASSET_REGISTER] = len(asset_rows)
	points.extend((marker["point"][0], marker["point"][1]) for marker in assets)

	# TASKS ARE PLACED ON WHAT IS ALREADY DRAWN AND ADD NO POINT OF THEIR OWN.
	# A task sits at the centre of the block it is about, so its pin is inside
	# the bounding box by construction — extending the box with it would be
	# arithmetic that cannot change the answer, and `_placements` is built from
	# `layers` and `markers` after both are final for exactly that reason.
	tasks_readable = _may_read(FARM_TASK)
	if not tasks_readable:
		refused.append(FARM_TASK)
	task_rows = _rows(FARM_TASK, entity) if tasks_readable else []
	tasks, tasks_unlocated, tasks_unplaceable = _task_markers(
		task_rows, _placements(layers, markers + assets)
	)
	counts[FARM_TASK] = len(task_rows)

	by_urgency = {}
	for task in tasks:
		by_urgency[task["urgency"]] = by_urgency.get(task["urgency"], 0) + 1
	by_type = {}
	for marker in assets:
		key = marker["asset_type"] or frappe._("(unrecorded)")
		by_type[key] = by_type.get(key, 0) + 1

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
		"assets": assets,
		"asset_summary": {
			"label": frappe._("Assets"),
			"total": len(asset_rows),
			"drawn": len(assets),
			"without_position": len(asset_rows) - len(assets),
			"by_asset_type": dict(sorted(by_type.items())),
			# THE ICONS FOR THE TYPES ACTUALLY ON THIS MAP, plus the fallback.
			# Sending the whole register would be fifteen entries for a legend
			# that keys three, and it would go stale the moment somebody added a
			# type — this is derived from what was drawn.
			"icons": {**{kind: asset_icon(kind) for kind in by_type}, "": ASSET_ICON_DEFAULT},
		},
		"tasks": tasks,
		"task_summary": {
			"label": frappe._("Open tasks"),
			"total": len(task_rows),
			"drawn": len(tasks),
			# THE THREE REASONS A TASK IS NOT ON THE MAP, KEPT APART. See
			# `_task_markers`: no location named, a location whose ground has
			# never been traced, and — implicitly — a register this login cannot
			# read, which is already in `refused`. Summing them would answer none
			# of the three.
			"without_location": tasks_unlocated,
			"without_a_drawn_place": len(tasks_unplaceable),
			"unplaceable": tasks_unplaceable[:DRAW_CAP],
			"by_urgency": by_urgency,
			"urgency_order": list(URGENCY_ORDER),
			"colours": dict(URGENCY_COLOURS),
		},
		"bounds": bounds_of(points),
		"counts": counts,
		"unreadable": unreadable,
		"refused": refused,
		"cap": DRAW_CAP,
		"capped": [doctype for doctype, total in counts.items() if total >= DRAW_CAP],
		"page_route": PAGE_ROUTE,
		**_overlay(entity, overlay),
		"terrain_layers": _terrain_layers(),
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


# ── v0.185.0: the terrain, under everything else ────────────────────────────
#: The Desk's door to the two terrain rasters. The phone's is the farmops
#: sidecar (`/farmops/api/tiles/...`), which authenticates a DEVICE — a Desk
#: browser holds a Frappe session and no device credential, so it would get
#: 401 on every tile. Same PNG bytes, from the same `tile_png`, through the door
#: this login already has.
TERRAIN_TILE_METHOD = "/api/method/erpnext_mcp.farm_overview.terrain_tile"

#: The two layers, keyed as `slope_aspect.describe` and `slope_grade.describe`
#: key them. Aspect first: it is the one the page was asked for.
TERRAIN_LAYERS = {
	"slope_aspect": (slope_aspect.describe, lambda z, x, y: slope_aspect.tile_png(z, x, y)),
	"slope_grade": (slope_grade.describe, lambda z, x, y: slope_grade.tile_png(z, x, y)),
}

#: The terrain is computed over Parcel and Field boundaries, so a login that may
#: read neither has no business being shown where the farm's ground lies.
TERRAIN_SOURCES = (PARCEL, FIELD)


def _may_see_terrain() -> bool:
	return any(_may_read(doctype) for doctype in TERRAIN_SOURCES)


def _terrain_layers() -> list:
	"""The two slope rasters as the page draws them, or `[]`.

	RASTERS UNDER THE BLOCKS, NOT A SIXTH OPERATIONAL LAYER. `overlays.py`'s
	five colour a polygon by what is true of it now, one at a time; terrain is a
	picture of the ground that does not change between one morning and the next.
	It is a tile layer in the map's own layer control, OFF by default, and it can
	sit under any operational layer — which way a restricted block faces is a
	fair question.

	`describe()` reads one small JSON file and no register, so an unbuilt layer
	costs nothing and still arrives, `available: false` with the sentence that
	says how to build it. The URL is swapped for the Desk's own, and the standard
	grade scheme is the only one offered here: a per-machine rollover map is a
	question about one tractor, and the phone that is sitting on it asks it.
	"""
	if not _may_see_terrain():
		return []
	out = []
	for key, (describe, _render) in TERRAIN_LAYERS.items():
		try:
			spec = describe()
		except Exception:  # pragma: no cover - a cache folder the worker cannot read
			continue
		spec["tile_url_template"] = f"{TERRAIN_TILE_METHOD}?layer={key}&z={{z}}&x={{x}}&y={{y}}"
		out.append(spec)
	return out


@frappe.whitelist()
def terrain_tile(layer=None, z=None, x=None, y=None) -> None:
	"""One slope tile for the Desk map, as `image/png`. v0.185.0.

	The bytes `/farmops/api/tiles/<layer>/...` serves the phone, from the same
	cache. Gated on reading Parcel or Field — see `TERRAIN_SOURCES` — and
	nothing else: the pixels are public USGS survey, not a record. No audit row,
	for the reason `farmops_api._slope_aspect_tile` gives: a map pan asks for
	dozens of tiles a second.

	FRAPPE'S OWN EXCEPTIONS AND NOT `ToolError`, which out of a whitelisted
	method is a 500 (see `api/gis.speaks_frappe`). And not `speaks_frappe`
	either: its channel is a modal, and the caller here is an `<img>` that no
	modal reaches. A 4xx is the whole answer a tile request can use.
	"""
	key = str(layer or "").strip()
	if key not in TERRAIN_LAYERS:
		raise frappe.ValidationError(f"layer must be one of {', '.join(TERRAIN_LAYERS)}, got {key!r}.")
	if not slope_aspect.valid_tile(z, x, y):
		raise frappe.ValidationError(f"{z}/{x}/{y} is not a map tile address.")
	if not _may_see_terrain():
		raise frappe.PermissionError(
			frappe._(
				"The slope layers are drawn over Parcel and Field boundaries, and this login may read neither."
			)
		)
	try:
		png = TERRAIN_LAYERS[key][1](int(z), int(x), int(y))
	except ToolError as error:  # numpy is not installed on this bench
		raise frappe.ValidationError(str(error)) from None
	if png is None:
		raise frappe.DoesNotExistError(
			frappe._(
				"The slope layers have not been built on this site. An operator runs "
				"build_slope_aspect_layer once to fetch the elevation and cache the tiles."
			)
		)
	bag = frappe.local.response
	bag["filename"] = f"{key}-{int(z)}-{int(x)}-{int(y)}.png"
	bag["filecontent"] = png
	bag["type"] = "download"
	# INLINE, or the browser treats every tile as a file to save.
	bag["display_content_as"] = "inline"
	bag["content_type"] = "image/png"


__all__ = [
	"PAGE_ROUTE",
	"PAGE_TITLE",
	"bounds_of",
	"farm_overview",
	"parse_geometry",
	"points_of",
	"readable_companies",
	"terrain_tile",
]

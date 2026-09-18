# SPDX-License-Identifier: MIT
"""What a surveyor, a county clerk and a GIS desk are each handed: KML, GeoJSON, PDF.

v0.173.0. v0.171.0 put a drawing surface on `/app/land-map` and stored what it
produced on the Lot Line Adjustment — `proposed_geometry`, `easement_geometry`
and `generated_legal_description`. This module is the four ways that record
leaves the site:

  KML                  Google Earth and a handheld GPS. Colour-coded: the
                       proposed parcels one way, the easement corridors another,
                       the county's lots underneath as context.
  GeoJSON              QGIS and ArcGIS, served with `application/geo+json` so a
                       browser hands it to the GIS and not to a text editor.
  Legal description    the metes-and-bounds text as a page, with the lots, the
                       acreage before and after, the date and the disclaimer.
  Survey packet        that page with a drawn map above it — the sheet somebody
                       staples to a county filing or emails to a surveyor.

────────────────────────────────────────────────────────────────────────────
IT EXPORTS WHAT IS STORED. IT COMPUTES NO GEOMETRY AND NO GEODESY.
────────────────────────────────────────────────────────────────────────────

Acreage comes from `geo.area_acres`, courses and the description come from
`surveying.py`, the before/after pair comes from `land_adjustment.side_plan`,
and the projection is `slope_aspect.to_mercator` — the one this app already
draws tiles with. Nothing here recomputes any of them. The rule is the one
`surveying.py` and `geo_map_widget.js` both state: a second implementation of a
number is how two answers appear with no way to tell which is wrong.

THE DISCLAIMER TRAVELS. `surveying.DISCLAIMER` is on the legal description page
and on the packet, and a KML placemark carries it in its description. A drawing
on an aerial photograph is a draft for a licensed surveyor; every artefact that
leaves here says so, because these are the files somebody forwards.

────────────────────────────────────────────────────────────────────────────
THE PDF IS wkhtmltopdf's, AND THERE IS A PLAINER ONE BEHIND IT
────────────────────────────────────────────────────────────────────────────

`frappe.utils.pdf.get_pdf` is the renderer on a bench, and it is what draws the
SVG map. Where the binary is missing — the standalone suite, a bench image built
without it — `render/pdf.py` sets the same words in Courier and says in the
document itself that the map is on the HTML version. That is the contract
`mou_print_format` already keeps, and `renderer` on every answer says which one
produced the file. A caller that needs the map can always take the HTML.

NO EXTERNAL RESOURCE IS REFERENCED. No stylesheet link, no web font, no image
URL: wkhtmltopdf fetches every URL it finds, with no timeout worth the name,
and one that does not answer hangs the render. The map is an SVG drawn from the
geometry, which is also why it needs no tile server and no network.

THE MAP GOES IN AS AN `<img>` WITH A `data:` URI, NOT AS INLINE `<svg>`. v0.173.0
inlined it, and every packet wkhtmltopdf printed had a heading, a legend and no
map. Two things killed it, and either one alone was enough. `frappe.utils.pdf.get_pdf`
re-serialises the page through BeautifulSoup's `html.parser`, which lowercases
attribute names, so `viewBox` became `viewbox` and the SVG lost its coordinate
system. And an inline SVG sized `width="100%"` with no height gets no height in
wkhtmltopdf's QtWebKit. An image's bytes go through neither parser, and Frappe's
`scrub_urls` leaves a `data:` source alone, so the map prints on a bench exactly
as it looks in a browser. The same WebKit ignores `paint-order`, so each label is
drawn twice, a white halo and then the ink, rather than relying on it.
"""

from __future__ import annotations

import base64
import html
import json
import math

import frappe

from . import geo, land_adjustment, surveying
from .errors import ToolError
from .slope_aspect import to_mercator

LOT_LINE_ADJUSTMENT = land_adjustment.LOT_LINE_ADJUSTMENT
COUNTY_TAX_LOT = land_adjustment.COUNTY_TAX_LOT
PARCEL = "Parcel"
FIELD = "Field"

KML_CONTENT_TYPE = "application/vnd.google-earth.kml+xml"
GEOJSON_CONTENT_TYPE = "application/geo+json"
PDF_CONTENT_TYPE = "application/pdf"

#: Which column holds the shape, per land record. The one table this module
#: reads: a doctype that is not here is refused by name rather than guessed at.
GEOMETRY_SOURCES = {
	PARCEL: ("boundary_geojson",),
	FIELD: ("boundary_geojson",),
	COUNTY_TAX_LOT: ("geometry",),
	LOT_LINE_ADJUSTMENT: ("proposed_geometry", "easement_geometry"),
}

#: What each layer is called and how it is drawn. The SVG colours are ordinary
#: `#rrggbb`; `kml_colour` turns each into KML's own spelling.
LAYERS = {
	"proposed": {
		"label": "Proposed boundary",
		"svg_line": "#1f6fb2",
		"svg_fill": "#2490ef",
		"svg_opacity": 0.25,
	},
	"easement": {
		"label": "Easement corridors",
		"svg_line": "#b8860b",
		"svg_fill": "#e8a51e",
		"svg_opacity": 0.3,
	},
	"lot": {
		"label": "County tax lots",
		"svg_line": "#707070",
		"svg_fill": "#9e9e9e",
		"svg_opacity": 0.12,
	},
}


def kml_colour(rgb: str, alpha: int) -> str:
	"""`#2490ef` at alpha 255 is `ffef9024`. KML's colour is **aabbggrr**.

	Alpha first and the RGB bytes REVERSED, which is the detail that makes a
	hand-written KML come out with the red and the blue swapped and nobody quite
	sure why the parcels are orange.
	"""
	value = str(rgb or "").lstrip("#")
	if len(value) != 6:
		raise ToolError(f"{rgb!r} is not a #rrggbb colour.")
	red, green, blue = value[0:2], value[2:4], value[4:6]
	opacity = max(0, min(255, int(alpha)))
	return f"{opacity:02x}{blue}{green}{red}".lower()


def document_title(doc) -> str:
	title = str(doc.get("title") or "").strip() or "Lot line adjustment"
	return f"{doc.name} — {title}"


# ── reading the record ──────────────────────────────────────────────────────
def adjustment(name: str):
	"""One Lot Line Adjustment, or a refusal naming it."""
	name = str(name or "").strip()
	if not name:
		raise ToolError("name (the Lot Line Adjustment docname, e.g. LLA-2026-0001) is required.")
	if not frappe.db.exists(LOT_LINE_ADJUSTMENT, name):
		raise ToolError(f"no Lot Line Adjustment called {name!r}. lla_list has them.")
	return frappe.get_doc(LOT_LINE_ADJUSTMENT, name)


def geometries(value) -> list:
	"""Every Polygon/LineString in a stored column, as `(kind, coordinates)` shapes.

	The column may hold a bare geometry, a Feature, a FeatureCollection or a
	GeometryCollection — `land_map._geojson_text` accepts all four on the way in,
	so all four have to come out.
	"""
	shape = land_adjustment.json_value(value)
	return _flatten(shape)


def _flatten(shape) -> list:
	if not isinstance(shape, dict):
		return []
	kind = shape.get("type")
	if kind == "FeatureCollection":
		out = []
		for feature in shape.get("features") or []:
			out.extend(_flatten(feature))
		return out
	if kind == "Feature":
		found = _flatten(shape.get("geometry"))
		for entry in found:
			entry["properties"] = shape.get("properties") or {}
		return found
	if kind == "GeometryCollection":
		out = []
		for geometry in shape.get("geometries") or []:
			out.extend(_flatten(geometry))
		return out
	if kind in ("Polygon", "MultiPolygon", "LineString", "MultiLineString", "Point"):
		return [{"type": kind, "coordinates": shape.get("coordinates"), "properties": {}}]
	return []


def _acres(shape: dict):
	if shape.get("type") not in ("Polygon", "MultiPolygon"):
		return None
	try:
		return round(geo.area_acres({"type": shape["type"], "coordinates": shape["coordinates"]}), 3)
	except Exception:
		return None


def _label(shape: dict, fallback: str, index: int, total: int) -> str:
	named = str((shape.get("properties") or {}).get("name") or "").strip()
	return named or (fallback if total == 1 else f"{fallback} {index}")


def shapes_of(doc) -> list:
	"""Every shape on an adjustment, each tagged with the layer it belongs to.

	The proposal, the easement corridors, and the two county lots the adjustment
	names — the lots last so they draw underneath, and labelled by their tax lot
	number, which is the identifier a surveyor and a clerk both work from.
	"""
	out = []
	for layer, column in (("proposed", "proposed_geometry"), ("easement", "easement_geometry")):
		found = geometries(doc.get(column))
		for index, shape in enumerate(found, start=1):
			out.append(
				{
					**shape,
					"layer": layer,
					"label": _label(shape, LAYERS[layer]["label"], index, len(found)),
					"acres": _acres(shape),
				}
			)
	for key in ("lot_1", "lot_2"):
		lot = land_adjustment.tax_lot_row(doc.get(key))
		if not lot:
			continue
		for shape in geometries(lot.get("geometry")):
			out.append(
				{
					**shape,
					"layer": "lot",
					"label": lot.get("map_taxlot") or doc.get(key),
					"acres": round(float(lot["acres_gis"]), 2) if lot.get("acres_gis") else None,
					"account": lot.get("account"),
					"owner_of_record": lot.get("owner_of_record"),
				}
			)
	return out


def acreage_table(doc) -> list:
	"""Before and after for each side, and where each figure came from.

	`land_adjustment.side_plan` is the one that knows: it reads the pieces, their
	parties and their surveyed acres. What this adds is the BEFORE figure and the
	honesty about which of the two bases produced the after — a stated survey
	acreage, or the county's GIS acreage adjusted by the surveyed pieces.
	"""
	rows = []
	for index, which in enumerate(land_adjustment.SIDES, start=1):
		lot = land_adjustment.tax_lot_row(doc.get(f"lot_{index}")) or {}
		before = round(float(lot["acres_gis"]), 2) if lot.get("acres_gis") else None
		try:
			plan = land_adjustment.side_plan(doc, which)
		except Exception:  # pragma: no cover - a bench without shapely mid-computation
			plan = {
				"acres_after": None,
				"acreage_basis": None,
				"acres_given_surveyed": None,
				"acres_received_surveyed": None,
			}
		after = plan.get("acres_after")
		rows.append(
			{
				"side": which,
				"party": doc.get(f"party_{index}"),
				"lot": doc.get(f"lot_{index}"),
				"acres_before": before,
				"acres_after": after,
				"acres_given": plan.get("acres_given_surveyed"),
				"acres_received": plan.get("acres_received_surveyed"),
				"change": round(after - before, 3) if (after is not None and before is not None) else None,
				"basis": plan.get("acreage_basis"),
			}
		)
	return rows


# ── KML ─────────────────────────────────────────────────────────────────────
def _coords(pairs) -> str:
	"""KML's `lon,lat,0` triples. Altitude is zero because none was surveyed."""
	return " ".join(f"{float(point[0]):.8f},{float(point[1]):.8f},0" for point in pairs)


def _kml_geometry(shape: dict) -> str:
	kind, coordinates = shape.get("type"), shape.get("coordinates") or []
	if kind == "Point":
		return f"<Point><coordinates>{_coords([coordinates])}</coordinates></Point>"
	if kind == "LineString":
		return (
			"<LineString><tessellate>1</tessellate>"
			f"<coordinates>{_coords(coordinates)}</coordinates></LineString>"
		)
	if kind == "MultiLineString":
		parts = "".join(_kml_geometry({"type": "LineString", "coordinates": line}) for line in coordinates)
		return f"<MultiGeometry>{parts}</MultiGeometry>"
	if kind == "Polygon":
		rings = list(coordinates)
		if not rings:
			return ""
		outer = f"<outerBoundaryIs><LinearRing><coordinates>{_coords(rings[0])}</coordinates></LinearRing></outerBoundaryIs>"
		inner = "".join(
			f"<innerBoundaryIs><LinearRing><coordinates>{_coords(ring)}</coordinates></LinearRing></innerBoundaryIs>"
			for ring in rings[1:]
		)
		return f"<Polygon><tessellate>1</tessellate>{outer}{inner}</Polygon>"
	if kind == "MultiPolygon":
		parts = "".join(_kml_geometry({"type": "Polygon", "coordinates": polygon}) for polygon in coordinates)
		return f"<MultiGeometry>{parts}</MultiGeometry>"
	return ""


def _kml_data(pairs) -> str:
	rows = "".join(
		f'<Data name="{html.escape(str(key))}"><value>{html.escape(str(value))}</value></Data>'
		for key, value in pairs
		if value not in (None, "")
	)
	return f"<ExtendedData>{rows}</ExtendedData>" if rows else ""


def kml(doc) -> str:
	"""The adjustment as KML: one folder per layer, styled, with the numbers attached."""
	shapes = shapes_of(doc)
	if not shapes:
		raise ToolError(
			f"{doc.name} has no geometry to export. Draw the proposal on /app/land-map and save it, "
			"or set the lots so the county's shapes come with it."
		)
	styles = "".join(_kml_style(key, layer) for key, layer in LAYERS.items())
	folders = []
	for key, layer in LAYERS.items():
		placemarks = []
		for shape in [entry for entry in shapes if entry["layer"] == key]:
			geometry = _kml_geometry(shape)
			if not geometry:
				continue
			data = _kml_data(
				[
					("Lot line adjustment", doc.name),
					("Title", doc.get("title")),
					("Layer", layer["label"]),
					("Acres", f"{shape['acres']:,.3f}" if shape.get("acres") is not None else None),
					("Tax lot", shape.get("label") if key == "lot" else None),
					("Assessor account", shape.get("account")),
					("Owner of record", shape.get("owner_of_record")),
					("Status", doc.get("status")),
				]
			)
			acreage = f"{shape['acres']:,.3f} acres. " if shape.get("acres") is not None else ""
			label = html.escape(str(shape["label"]))
			note = html.escape(f"{acreage}{surveying.DISCLAIMER}")
			placemarks.append(
				f"<Placemark><name>{label}</name><description>{note}</description>"
				f"<styleUrl>#{key}</styleUrl>{data}{geometry}</Placemark>"
			)
		if placemarks:
			folder_name = html.escape(layer["label"])
			body = "".join(placemarks)
			folders.append(f"<Folder><name>{folder_name}</name>{body}</Folder>")
	name = html.escape(document_title(doc))
	disclaimer = html.escape(surveying.DISCLAIMER)
	body = "".join(folders)
	return (
		'<?xml version="1.0" encoding="UTF-8"?>\n'
		'<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
		f"<name>{name}</name><description>{disclaimer}</description>{styles}{body}"
		"</Document></kml>\n"
	)


def _kml_style(key: str, layer: dict) -> str:
	line = kml_colour(layer["svg_line"], 255)
	fill = kml_colour(layer["svg_fill"], 85)
	return (
		f'<Style id="{key}"><LineStyle><color>{line}</color><width>2.2</width></LineStyle>'
		f"<PolyStyle><color>{fill}</color></PolyStyle></Style>"
	)


# ── GeoJSON ─────────────────────────────────────────────────────────────────
def geojson(doctype: str, name: str) -> dict:
	"""`{"geojson", "filename", "content_type", "features", "warnings"}` for one land record.

	A FeatureCollection rather than a bare geometry: QGIS and ArcGIS both open
	either, and the properties are what makes the layer readable once it is open
	— which record this is, what it is called, and how big it is.
	"""
	doctype = str(doctype or "").strip()
	if doctype not in GEOMETRY_SOURCES:
		raise ToolError(
			f"{doctype!r} is not a land record with a boundary. It is one of: "
			f"{', '.join(sorted(GEOMETRY_SOURCES))}."
		)
	name = str(name or "").strip()
	if not name or not frappe.db.exists(doctype, name):
		raise ToolError(f"no {doctype} called {name!r} on this site.")

	features, warnings = [], []
	if doctype == LOT_LINE_ADJUSTMENT:
		doc = frappe.get_doc(LOT_LINE_ADJUSTMENT, name)
		for shape in shapes_of(doc):
			features.append(
				_feature(
					shape,
					{
						"doctype": doctype,
						"name": name,
						"label": shape["label"],
						"layer": LAYERS[shape["layer"]]["label"],
						"acres": shape.get("acres"),
						"status": doc.get("status"),
						"title": doc.get("title"),
					},
				)
			)
	else:
		column = GEOMETRY_SOURCES[doctype][0]
		row = frappe.db.get_value(doctype, name, ["name", column], as_dict=True) or {}
		for shape in geometries(row.get(column)):
			features.append(
				_feature(shape, {"doctype": doctype, "name": name, "label": name, "acres": _acres(shape)})
			)
	if not features:
		warnings.append(f"{doctype} {name} has no boundary stored, so the file carries no features.")
	return {
		"geojson": {"type": "FeatureCollection", "features": features},
		"filename": f"{_slug(name)}.geojson",
		"content_type": GEOJSON_CONTENT_TYPE,
		"feature_count": len(features),
		"warnings": warnings,
	}


def _feature(shape: dict, properties: dict) -> dict:
	return {
		"type": "Feature",
		"properties": {key: value for key, value in properties.items() if value not in (None, "")},
		"geometry": {"type": shape["type"], "coordinates": shape["coordinates"]},
	}


def _slug(text: str) -> str:
	out = "".join(character if character.isalnum() or character in "-_" else "-" for character in str(text))
	while "--" in out:
		out = out.replace("--", "-")
	return out.strip("-") or "export"


# ── the map, as SVG ─────────────────────────────────────────────────────────
#: The drawing box, in points at 96 dpi — a landscape half of US Letter, which is
#: what the packet gives the map above the description.
SVG_WIDTH = 720.0
SVG_HEIGHT = 470.0
SVG_PAD = 26.0

#: A scale bar is drawn at the longest of these that fits a third of the map.
SCALE_STEPS_FEET = (50, 100, 200, 300, 500, 800, 1000, 1320, 2000, 2640, 5280)


def _rings(shape: dict) -> list:
	"""Every ring or line in one shape, as lists of `[lon, lat]`."""
	kind, coordinates = shape.get("type"), shape.get("coordinates") or []
	if kind == "Point":
		return [[coordinates]]
	if kind == "LineString":
		return [list(coordinates)]
	if kind == "MultiLineString":
		return [list(line) for line in coordinates]
	if kind == "Polygon":
		return [list(ring) for ring in coordinates]
	if kind == "MultiPolygon":
		return [list(ring) for polygon in coordinates for ring in polygon]
	return []


def projection(shapes: list) -> dict:
	"""How to put these shapes on the page: mercator metres to SVG units.

	`slope_aspect.to_mercator` is the projection, which is the one this app's
	tiles are drawn in. The scale is whichever axis runs out first, so the map
	fills the box without distorting the shapes.
	"""
	points = [point for shape in shapes for ring in _rings(shape) for point in ring]
	if not points:
		raise ToolError("there is no geometry to draw.")
	projected = [to_mercator(float(point[0]), float(point[1])) for point in points]
	xs = [point[0] for point in projected]
	ys = [point[1] for point in projected]
	width = max(max(xs) - min(xs), 1e-6)
	height = max(max(ys) - min(ys), 1e-6)
	scale = min((SVG_WIDTH - 2 * SVG_PAD) / width, (SVG_HEIGHT - 2 * SVG_PAD) / height)
	latitudes = [float(point[1]) for point in points]
	return {
		"xmin": min(xs),
		"ymax": max(ys),
		"scale": scale,
		"x_offset": (SVG_WIDTH - width * scale) / 2,
		"y_offset": (SVG_HEIGHT - height * scale) / 2,
		"mid_latitude": (min(latitudes) + max(latitudes)) / 2,
	}


def place(view: dict, point) -> tuple:
	x, y = to_mercator(float(point[0]), float(point[1]))
	return (
		round(view["x_offset"] + (x - view["xmin"]) * view["scale"], 2),
		round(view["y_offset"] + (view["ymax"] - y) * view["scale"], 2),
	)


def _path(view: dict, ring: list) -> str:
	points = [place(view, point) for point in ring]
	return "M " + " L ".join(f"{x},{y}" for x, y in points) + " Z"


def _centroid(view: dict, shape: dict) -> tuple:
	points = [place(view, point) for ring in _rings(shape) for point in ring]
	return (
		round(sum(point[0] for point in points) / len(points), 2),
		round(sum(point[1] for point in points) / len(points), 2),
	)


def scale_bar(view: dict) -> dict:
	"""A round distance in feet, and how long it is on the page.

	Mercator metres are not ground metres: at 45° N a mercator metre is about
	0.71 of one. The bar is drawn from the GROUND distance at the middle
	latitude, which is what a reader measures with.
	"""
	ground_per_unit = math.cos(math.radians(view["mid_latitude"])) / view["scale"]
	widest = (SVG_WIDTH - 2 * SVG_PAD) / 3
	feet = SCALE_STEPS_FEET[0]
	for step in SCALE_STEPS_FEET:
		if (step * surveying.METRES_PER_FOOT) / ground_per_unit <= widest:
			feet = step
	length = (feet * surveying.METRES_PER_FOOT) / ground_per_unit
	return {
		"feet": feet,
		"metres": round(feet * surveying.METRES_PER_FOOT),
		"length": round(length, 2),
	}


def svg_map(shapes: list, title: str = "") -> str:
	"""The shapes drawn on a page: polygons, labels, a north arrow and a scale bar.

	Pure string building. No tiles, no images, no script — the sheet has to print
	the same on a bench with no route to the internet as on one with.
	"""
	view = projection(shapes)
	_label_attribute = html.escape(title or "Proposed lot line")
	parts = [
		f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SVG_WIDTH:g} {SVG_HEIGHT:g}" '
		f'width="{SVG_WIDTH:g}" height="{SVG_HEIGHT:g}" role="img" aria-label="{_label_attribute}">',
		f'<rect x="0" y="0" width="{SVG_WIDTH:g}" height="{SVG_HEIGHT:g}" fill="#ffffff" stroke="#333" stroke-width="1"/>',
	]
	# The lots first, so the proposal draws on top of them.
	order = ("lot", "easement", "proposed")
	for layer in order:
		style = LAYERS[layer]
		for shape in [entry for entry in shapes if entry.get("layer", "proposed") == layer]:
			for ring in _rings(shape):
				if len(ring) < 2:
					continue
				closed = shape.get("type") in ("Polygon", "MultiPolygon")
				path = _path(view, ring) if closed else _path(view, ring)[:-2]
				dashes = "" if closed else ' stroke-dasharray="6 4"'
				fill = style["svg_fill"] if closed else "none"
				opacity = style["svg_opacity"] if closed else 0
				stroke = style["svg_line"]
				width = 2.0 if layer == "proposed" else 1.4
				parts.append(
					f'<path d="{path}" fill="{fill}" fill-opacity="{opacity}" stroke="{stroke}" '
					f'stroke-width="{width}"{dashes}/>'
				)
	for layer in order:
		for shape in [entry for entry in shapes if entry.get("layer", "proposed") == layer]:
			x, y = _centroid(view, shape)
			if layer == "lot":
				# Just inside the lot's top edge: the proposal sits inside the lot
				# and usually near its middle, so two centred labels collide.
				y = round(min(place(view, point)[1] for ring in _rings(shape) for point in ring) + 16, 2)
			label = str(shape.get("label") or "")
			acres = f" — {shape['acres']:,.2f} ac" if shape.get("acres") is not None else ""
			text = html.escape(label + acres)
			font = f'x="{x}" y="{y}" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="11"'
			# Halo, then ink. wkhtmltopdf ignores paint-order, and a stroke drawn
			# over the glyphs whites the label out.
			parts.append(
				f'<text {font} fill="#ffffff" stroke="#ffffff" stroke-width="3" '
				f'stroke-linejoin="round">{text}</text>'
			)
			parts.append(f'<text {font} fill="#111">{text}</text>')
	parts.append(_north_arrow())
	parts.append(_scale_bar_svg(scale_bar(view)))
	parts.append(
		f'<text x="{SVG_PAD}" y="{SVG_PAD - 8}" font-family="Helvetica, Arial, sans-serif" font-size="12" '
		f'fill="#111">{html.escape(title or "")}</text>'
	)
	parts.append("</svg>")
	return "".join(parts)


def map_image(shapes: list, title: str = "") -> str:
	"""`svg_map` as an `<img>` whose source is the SVG itself, base64 in a `data:` URI.

	The form wkhtmltopdf draws. The module docstring says why inline `<svg>` is
	not: Frappe's HTML parser lowercases `viewBox` on the way to the renderer.
	"""
	encoded = base64.b64encode(svg_map(shapes, title).encode("utf-8")).decode("ascii")
	alt = html.escape(title or "Proposed lot line")
	return (
		f'<img src="data:image/svg+xml;base64,{encoded}" alt="{alt}" '
		f'width="{SVG_WIDTH:g}" height="{SVG_HEIGHT:g}" style="width:100%;height:auto">'
	)


def _north_arrow() -> str:
	x, y = SVG_WIDTH - 44, 30
	return (
		f'<g><path d="M {x},{y + 34} L {x},{y} M {x},{y} L {x - 7},{y + 12} M {x},{y} L {x + 7},{y + 12}" '
		'stroke="#111" stroke-width="2" fill="none"/>'
		f'<text x="{x}" y="{y + 50}" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" '
		'font-size="12" fill="#111">N</text></g>'
	)


def _scale_bar_svg(bar: dict) -> str:
	x, y = SVG_PAD, SVG_HEIGHT - SVG_PAD
	return (
		f'<g><rect x="{x}" y="{y - 6}" width="{bar["length"]}" height="6" fill="#111"/>'
		f'<rect x="{x}" y="{y - 6}" width="{round(bar["length"] / 2, 2)}" height="6" fill="#ffffff" stroke="#111" stroke-width="1"/>'
		f'<text x="{x}" y="{y + 12}" font-family="Helvetica, Arial, sans-serif" font-size="11" fill="#111">'
		f'0</text><text x="{x + bar["length"]}" y="{y + 12}" text-anchor="middle" '
		'font-family="Helvetica, Arial, sans-serif" font-size="11" fill="#111">'
		f"{bar['feet']:,} ft ({bar['metres']:,} m)</text></g>"
	)


# ── the documents ───────────────────────────────────────────────────────────
def _today() -> str:
	return str(frappe.utils.nowdate())


def _row(label: str, value) -> str:
	if value in (None, ""):
		return ""
	return f"<tr><th>{html.escape(label)}</th><td>{html.escape(str(value))}</td></tr>"


def _header_html(doc) -> str:
	lots = []
	for index in (1, 2):
		named = doc.get(f"lot_{index}")
		if not named:
			continue
		cached = land_adjustment.tax_lot_row(named) or {}
		account = f" (account {cached['account']})" if cached.get("account") else ""
		party = doc.get(f"party_{index}") or "party not named"
		lots.append(f"{named}{account} — {party}")
	return (
		"<table class='meta'>"
		+ _row("Lot line adjustment", doc.name)
		+ _row("Title", doc.get("title"))
		+ _row("County", f"{doc.get('county')} County, {doc.get('state')}" if doc.get("county") else None)
		+ _row("Tax lots", "; ".join(lots) or "to be confirmed")
		+ _row("Status", doc.get("status"))
		+ _row("Recording", doc.get("recording_number"))
		+ _row("Survey reference", doc.get("survey_reference"))
		+ _row("Printed", _today())
		+ "</table>"
	)


def _acreage_html(doc) -> str:
	rows = acreage_table(doc)
	if not any(row["acres_before"] is not None or row["acres_after"] is not None for row in rows):
		return "<p class='muted'>No acreage is recorded for either lot yet.</p>"
	body = "".join(_acreage_row(row) for row in rows)
	basis = next((row["basis"] for row in rows if row.get("basis")), "")
	note = f"<p class='muted'>{html.escape(str(basis))}</p>" if basis else ""
	return (
		"<table class='grid'><tr><th>Side</th><th>Party</th><th>Tax lot</th>"
		"<th class='num'>Acres before</th><th class='num'>Acres after</th><th class='num'>Change</th></tr>"
		f"{body}</table>{note}"
	)


def _acreage_row(row: dict) -> str:
	side = html.escape(str(row["side"]))
	party = html.escape(str(row["party"] or ""))
	lot = html.escape(str(row["lot"] or ""))
	before = f"{row['acres_before']:,.2f}" if row["acres_before"] is not None else "—"
	after = f"{row['acres_after']:,.3f}" if row["acres_after"] is not None else "—"
	change = f"{row['change']:+,.3f}" if row["change"] is not None else "—"
	return (
		f"<tr><td>{side}</td><td>{party}</td><td>{lot}</td>"
		f"<td class='num'>{before}</td><td class='num'>{after}</td><td class='num'>{change}</td></tr>"
	)


STYLE = """
body { font-family: Georgia, "Times New Roman", serif; font-size: 11pt; color: #111; }
h1 { font-size: 15pt; margin: 0 0 2pt; }
h2 { font-size: 12pt; border-bottom: 1px solid #333; margin: 14pt 0 6pt; }
.sub { margin: 0 0 10pt; }
table { border-collapse: collapse; width: 100%; margin: 4pt 0; }
table.meta th { text-align: left; width: 26%; vertical-align: top; padding: 2pt 6pt 2pt 0; }
table.meta td { padding: 2pt 0; }
table.grid th, table.grid td { border: 1px solid #888; padding: 3pt 5pt; text-align: left; }
table.grid td.num, table.grid th.num { text-align: right; }
pre.legal { font-family: "Courier New", monospace; font-size: 10pt; white-space: pre-wrap; }
.disclaimer { border: 1px solid #888; padding: 6pt; font-size: 9.5pt; }
.muted { color: #555; font-size: 9.5pt; }
.map { border: 0; margin: 0 0 4pt; }
.figure { page-break-inside: avoid; }
"""


def description_text(doc) -> str:
	"""The stored description, or a refusal that says how to make one."""
	text = str(doc.get("generated_legal_description") or "").strip()
	if not text:
		raise ToolError(
			f"{doc.name} carries no legal description. Draw the line on /app/land-map and save it to "
			"this adjustment, which stores the draft description with it."
		)
	return text


def legal_description_html(doc) -> str:
	text = description_text(doc)
	disclaimer = surveying.DISCLAIMER
	body = text[: -len(disclaimer)].strip() if text.endswith(disclaimer) else text
	return (
		f"<style>{STYLE}</style>"
		"<h1>Draft legal description</h1>"
		f"<p class='sub'>{html.escape(document_title(doc))}</p>"
		f"{_header_html(doc)}"
		"<h2>Acreage before and after</h2>"
		f"{_acreage_html(doc)}"
		"<h2>Metes and bounds</h2>"
		f"<pre class='legal'>{html.escape(body)}</pre>"
		f"<div class='disclaimer'>{html.escape(disclaimer)}</div>"
	)


def survey_packet_html(doc) -> str:
	shapes = shapes_of(doc)
	if not shapes:
		raise ToolError(
			f"{doc.name} has no geometry to draw. Draw the proposal on /app/land-map and save it to "
			"this adjustment."
		)
	title = document_title(doc)
	return (
		f"<style>{STYLE}</style>"
		"<h1>Survey packet</h1>"
		f"<p class='sub'>{html.escape(title)}</p>"
		f"{_header_html(doc)}"
		f"{_before_figure(doc, shapes)}"
		f"{_figure('Proposed boundary', 'After', shapes, title)}"
		"<h2>Acreage before and after</h2>"
		f"{_acreage_html(doc)}"
		"<h2>Metes and bounds</h2>"
		f"<pre class='legal'>{html.escape(_body_only(doc))}</pre>"
		f"<div class='disclaimer'>{html.escape(surveying.DISCLAIMER)}</div>"
	)


def _legend(shapes: list) -> str:
	return " &nbsp;·&nbsp; ".join(
		f"<span style='color:{LAYERS[key]['svg_line']}'>&#9632;</span> {html.escape(LAYERS[key]['label'])}"
		for key in ("proposed", "easement", "lot")
		if any(shape["layer"] == key for shape in shapes)
	)


def _figure(heading: str, caption: str, shapes: list, title: str) -> str:
	return (
		f"<div class='figure'><h2>{html.escape(heading)}</h2>"
		f"<div class='map'>{map_image(shapes, title)}</div>"
		f"<p class='muted'>{html.escape(caption)}: {_legend(shapes)}</p></div>"
	)


def _before_figure(doc, shapes: list) -> str:
	"""The county's lots as they stand, so the sheet shows before AND after.

	Drawn from the lots alone, on their own extent. With no lot shape cached
	there is nothing to draw, and the sheet says which record would supply it.
	"""
	lots = [shape for shape in shapes if shape["layer"] == "lot"]
	if lots:
		return _figure("Existing tax lots", "Before", lots, f"{doc.name} — tax lots as recorded")
	return (
		"<div class='figure'><h2>Existing tax lots</h2>"
		"<p class='muted'>No county tax lot shape is stored for this adjustment's lots, so the "
		"before map cannot be drawn. Set Lot 1 and Lot 2 to County Tax Lots that carry a geometry.</p></div>"
	)


def _body_only(doc) -> str:
	text = description_text(doc)
	disclaimer = surveying.DISCLAIMER
	return text[: -len(disclaimer)].strip() if text.endswith(disclaimer) else text


# ── rendering ───────────────────────────────────────────────────────────────
def render_pdf(html_text: str, doc, kind: str) -> dict:
	"""`{"pdf", "renderer", "note"}` — wkhtmltopdf where the bench has it.

	The fallback sets the same words through `render/pdf.py` and says, in the
	document, that the map is on the HTML version. Never raises for want of a
	renderer: a packet that cannot be drawn is still a packet that can be read.
	"""
	try:
		from frappe.utils.pdf import get_pdf
	except Exception as exc:
		note = f"this bench has no PDF renderer ({type(exc).__name__})"
	else:
		try:
			pdf = get_pdf(html_text)
			if pdf:
				return {"pdf": pdf, "renderer": "frappe.utils.pdf (wkhtmltopdf)", "note": ""}
			note = "the PDF renderer produced nothing"
		except Exception as exc:  # pragma: no cover - depends on the bench image
			note = f"the PDF renderer failed ({type(exc).__name__}: {exc})"
	return {
		"pdf": plain_pdf(doc, kind),
		"renderer": "erpnext_mcp render/pdf.py",
		"note": (
			f"{note}, so the document was set by this app's own writer. The words are the same; the "
			"drawn map is on the HTML version of this document, which the tool returns with "
			"include_html."
		),
	}


def plain_pdf(doc, kind: str) -> bytes:
	"""The same content in Courier, for a bench with no wkhtmltopdf."""
	from .render.pdf import PdfDocument

	heading = "Survey packet" if kind == "packet" else "Draft legal description"
	pdf = PdfDocument(
		title=f"{heading} — {doc.name}",
		author="erpnext_mcp",
		subject=f"Lot line adjustment {doc.name}",
		footer=f"{doc.name} — {heading} — a draft for a licensed surveyor",
	)
	pdf.title_block(
		heading,
		document_title(doc),
		f"{doc.get('county')} County, {doc.get('state')} · printed {_today()}",
	)
	lots = []
	for index in (1, 2):
		named = doc.get(f"lot_{index}")
		if named:
			party = doc.get(f"party_{index}") or "party not named"
			lots.append(f"{named} ({party})")
	pdf.key_values(
		[
			("Tax lots", "; ".join(lots) or "to be confirmed"),
			("Status", doc.get("status") or ""),
			("Recording number", doc.get("recording_number") or "—"),
			("Survey reference", doc.get("survey_reference") or "—"),
		]
	)
	if kind == "packet":
		pdf.heading("Proposed boundary")
		pdf.paragraph(
			"The drawn map is not set in this version of the document — this bench has no HTML-to-PDF "
			"renderer. The HTML version carries it, and the courses below describe the same figure."
		)
	pdf.heading("Acreage before and after")
	pdf.table(
		["Side", "Party", "Tax lot", "Before", "After", "Change"],
		[
			[
				row["side"],
				row["party"] or "",
				row["lot"] or "",
				f"{row['acres_before']:,.2f}" if row["acres_before"] is not None else "-",
				f"{row['acres_after']:,.3f}" if row["acres_after"] is not None else "-",
				f"{row['change']:+,.3f}" if row["change"] is not None else "-",
			]
			for row in acreage_table(doc)
		],
		align=["l", "l", "l", "r", "r", "r"],
	)
	pdf.heading("Metes and bounds")
	for paragraph in _body_only(doc).split("\n\n"):
		pdf.paragraph(paragraph)
		pdf.spacer(4)
	pdf.heading("Disclaimer")
	pdf.paragraph(surveying.DISCLAIMER)
	return pdf.render()


def document(doc, kind: str) -> dict:
	"""One of the two documents, as HTML and PDF, with its filename."""
	if kind not in ("legal", "packet"):  # pragma: no cover - callers pass a literal
		raise ToolError(f"{kind!r} is not a document this module renders.")
	html_text = survey_packet_html(doc) if kind == "packet" else legal_description_html(doc)
	rendered = render_pdf(html_text, doc, kind)
	suffix = "survey-packet" if kind == "packet" else "legal-description"
	return {
		**rendered,
		"html": html_text,
		"filename": f"{_slug(doc.name)}-{suffix}.pdf",
		"content_type": PDF_CONTENT_TYPE,
	}


def kml_document(doc) -> dict:
	return {
		"kml": kml(doc),
		"filename": f"{_slug(doc.name)}.kml",
		"content_type": KML_CONTENT_TYPE,
	}


def geojson_text(answer: dict) -> str:
	return json.dumps(answer["geojson"], separators=(",", ":"), sort_keys=False)

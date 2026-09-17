# SPDX-License-Identifier: MIT
"""The land exports, as tools: KML, GeoJSON and two PDFs. v0.173.0.

`erpnext_mcp/land_export.py` is the engine and argues the design; this file is
the four doors onto it, and every one of them is READ-ONLY. An export writes
nothing: it hands back what the record already holds, in the format the person
on the other end opens.

  export_lla_kml                     Google Earth and a handheld GPS
  export_boundary_geojson            QGIS and ArcGIS, for any land record
  export_lla_legal_description_pdf   the metes-and-bounds page
  export_lla_survey_packet_pdf       that page with the drawn map above it

THE DESK HAS THE SAME FOUR as file downloads — `api/land_map.export_kml`,
`export_geojson` and `export_pdf`, wired to the buttons on `/app/land-map`.
Both doors call this module's engine, so the file a surveyor gets from the map
page and the one a model hands over are the same bytes.
"""

from __future__ import annotations

import base64

from .. import land_adjustment as land
from .. import land_export
from ..args import as_bool, as_str
from ..result import ToolResult


def _lla(args: dict, action: str):
	"""One adjustment, once the caller has proved it may read them."""
	land.require_roles(land.AGREEMENT_ROLES, action, "Nothing was exported.")
	return land_export.adjustment(as_str(args, "name"))


def export_lla_kml(args: dict) -> ToolResult:
	"""The adjustment's shapes as KML, colour-coded by layer."""
	doc = _lla(args, "export a lot line adjustment")
	answer = land_export.kml_document(doc)
	shapes = land_export.shapes_of(doc)
	counts: dict = {}
	for shape in shapes:
		label = land_export.LAYERS[shape["layer"]]["label"]
		counts[label] = counts.get(label, 0) + 1
	return ToolResult(
		data={
			"name": doc.name,
			"title": doc.get("title"),
			"filename": answer["filename"],
			"content_type": answer["content_type"],
			"kml": answer["kml"],
			"bytes": len(answer["kml"].encode("utf-8")),
			"placemarks": counts,
			"disclaimer": land_export.surveying.DISCLAIMER,
			"note": (
				"Open it in Google Earth, or load it onto a handheld GPS. The proposed boundary, the "
				"easement corridors and the county's lots are separate folders, each styled."
			),
		},
		summary=f"{doc.name}: KML with {len(shapes)} placemark(s)",
	)


def export_boundary_geojson(args: dict) -> ToolResult:
	"""The stored boundary of any land record, as a GeoJSON FeatureCollection."""
	doctype = as_str(args, "doctype", required=True)
	name = as_str(args, "name", required=True)
	if doctype == land.LOT_LINE_ADJUSTMENT:
		land.require_roles(land.AGREEMENT_ROLES, "export a lot line adjustment", "Nothing was exported.")
	elif doctype == land.COUNTY_TAX_LOT:
		land.require_roles(land.TAX_LOT_ROLES, "export a county tax lot", "Nothing was exported.")
	answer = land_export.geojson(doctype, name)
	return ToolResult(
		data={
			"doctype": doctype,
			"name": name,
			"filename": answer["filename"],
			"content_type": answer["content_type"],
			"feature_count": answer["feature_count"],
			"geojson": answer["geojson"],
			"warnings": answer["warnings"],
			"note": (
				"WGS84 degrees, the projection every record on this site stores. Save it with the "
				"filename above and QGIS or ArcGIS opens it as a layer."
			),
		},
		summary=f"{doctype} {name}: {answer['feature_count']} feature(s) of GeoJSON",
	)


def _pdf_result(doc, kind: str, args: dict, heading: str) -> ToolResult:
	answer = land_export.document(doc, kind)
	pdf = answer["pdf"]
	return ToolResult(
		data={
			"name": doc.name,
			"title": doc.get("title"),
			"filename": answer["filename"],
			"content_type": answer["content_type"],
			"pdf_bytes": len(pdf),
			"pdf_base64": base64.b64encode(pdf).decode("ascii"),
			"renderer": answer["renderer"],
			"html": answer["html"] if as_bool(args, "include_html", False) else None,
			"printed_on": land_export._today(),
			"disclaimer": land_export.surveying.DISCLAIMER,
			"note": answer["note"] or None,
		},
		summary=f"{doc.name}: {heading}, {len(pdf)} byte PDF",
	)


def export_lla_legal_description_pdf(args: dict) -> ToolResult:
	"""The draft metes-and-bounds description as a page."""
	doc = _lla(args, "export a lot line adjustment")
	return _pdf_result(doc, "legal", args, "draft legal description")


def export_lla_survey_packet_pdf(args: dict) -> ToolResult:
	"""The drawn map and the description on one sheet."""
	doc = _lla(args, "export a lot line adjustment")
	return _pdf_result(doc, "packet", args, "survey packet")

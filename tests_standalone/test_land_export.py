# SPDX-License-Identifier: MIT
"""The land document exports — KML, GeoJSON and two PDFs. v0.173.0.

SEVEN CLAIMS.

1. `TheKmlIsKml` — it parses as XML, the folders and styles are there, the
   colours are KML's own aabbggrr, and **the coordinates are lon,lat** — the
   order that silently puts an Oregon parcel in Somalia when it is wrong.
2. `TheGeoJsonOpensInAGis` — every land record's stored boundary, with the
   right content type, the properties a layer needs, and an empty record that
   says so rather than failing.
3. `TheMapIsDrawnFromTheGeometry` — north is up, east is right, the scale bar
   is GROUND feet and not mercator feet, and nothing external is referenced.
4. `TheLegalDescriptionPage` — the header, the before/after acreage, the
   description and the disclaimer; refused when there is no description.
5. `TheSurveyPacket` — the same page with two maps in it, before and after,
   each as an image wkhtmltopdf actually draws, and the plain-PDF fallback that
   says where the maps went.
   `ThePacketIsWhatASurveyorWorksFrom` — the maps are outlines only, and the
   courses, corners, parties, pieces, easements and open questions are tabled
   off the record in both PDFs.
6. `TheDeskDownloads` — the three whitelisted methods answer a FILE, under
   Frappe's own read permission on the record.
7. `TheButtonsAreOnTheMap` — the page carries them, and running the script
   proves each one opens the method it says it does.
"""

import base64
import json
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import ClassVar

import frappe

from erpnext_mcp import land_adjustment as land
from erpnext_mcp import land_export, surveying
from erpnext_mcp.api import land_map
from erpnext_mcp.errors import ToolError

from .fixtures import MAIN
from .harness import STORE
from .test_land_map import DRAWN, EAST, LOT, NEIGHBOUR, NORTH, PAGE_DIR, SOUTH, WEST, LandMapTestCase, box

try:
	from pypdf import PdfReader
except Exception:  # pragma: no cover - the first CI leg has no pypdf
	PdfReader = None

KML_NS = {"kml": "http://www.opengis.net/kml/2.2"}

#: The second county lot the full fixture names, east of the first.
NEXT_LOT = "1N 13E 3 1100"

#: The easement the map saves: a corridor as a line, not an area.
EASEMENT = {
	"type": "LineString",
	"coordinates": [[WEST + 0.0002, SOUTH + 0.0002], [EAST - 0.0002, SOUTH + 0.0002]],
}


def proposal() -> dict:
	return {"type": "Polygon", "coordinates": [[*DRAWN, DRAWN[0]]]}


class ExportTestCase(LandMapTestCase):
	SWITCHES: ClassVar[dict] = {
		f"allow_{name}": 1
		for name in (
			"create_parcel",
			"get_parcel",
			"lla_create",
			"lla_update",
			"lla_get",
			"export_lla_kml",
			"export_boundary_geojson",
			"export_lla_legal_description_pdf",
			"export_lla_survey_packet_pdf",
		)
	}

	def an_export_adjustment(self, description=True, geometry=True, easement=True, lots=True) -> str:
		"""The fixture every export reads: a drawn proposal saved to an adjustment."""
		if lots:
			self.a_lot()
		fields = {}
		if lots:
			fields["lot_1"] = LOT
		name = self.an_adjustment(**fields)
		update = {}
		if geometry:
			update["proposed_geometry"] = json.dumps(proposal())
		if easement:
			update["easement_geometry"] = json.dumps(EASEMENT)
		if description:
			update["generated_legal_description"] = surveying.legal_description(
				DRAWN, f"Beginning at the southwest corner of {LOT}"
			)["text"]
		if update:
			self.tool_data("lla_update", {"name": name, "fields": update})
		return name

	def doc(self, name):
		return frappe.get_doc(land.LOT_LINE_ADJUSTMENT, name)

	def a_full_adjustment(self) -> str:
		"""An adjustment filled in the way one is by the time it goes to a surveyor."""
		self.a_lot()
		land.upsert_tax_lot(
			{
				"map_taxlot": NEXT_LOT,
				"county": "Wasco",
				"account": "7504",
				"owner_of_record": "Mill Creek Farms",
				"situs": "4120 Dry Hollow Rd",
				"acres_gis": 38.5,
				"source": "Manual",
				"geometry": box(EAST, SOUTH, EAST + 0.003, NORTH),
			}
		)
		name = self.an_adjustment(
			lot_1=LOT,
			lot_2=NEXT_LOT,
			lender="Columbia Bank",
			lender_conditions="Written consent required before recording.",
			target_close="2026-12-31",
			notes="Fence line on the north side is 3 ft off the drawn line; surveyor to decide.",
			pieces=[
				{
					"piece_name": "Parcel A",
					"from_party": MAIN,
					"to_party": NEIGHBOUR,
					"acres_gis": 1.5,
					"improvements": "Pump house",
					"line_notes": "Follows the fence on the south side.",
					"geometry": proposal(),
				}
			],
			easements=[
				{
					"type": "access",
					"burdened": NEIGHBOUR,
					"benefited": MAIN,
					"notes": "Truck access to the shop.",
				}
			],
			open_items=[
				{
					"item": "Bank written consent",
					"question": "Columbia Bank must consent.",
					"owner": "A Signer",
				}
			],
		)
		self.tool_data(
			"lla_update",
			{
				"name": name,
				"fields": {
					"proposed_geometry": json.dumps(proposal()),
					"easement_geometry": json.dumps(EASEMENT),
					"generated_legal_description": surveying.legal_description(
						DRAWN, f"Beginning at the southwest corner of {LOT}"
					)["text"],
				},
			},
		)
		return name


# ── 1 ───────────────────────────────────────────────────────────────────────
class TheKmlIsKml(ExportTestCase):
	def kml(self, **kwargs) -> str:
		return self.tool_data("export_lla_kml", {"name": self.an_export_adjustment(**kwargs)})["kml"]

	def test_it_parses_and_carries_a_folder_per_layer(self):
		root = ET.fromstring(self.kml())
		self.assertTrue(root.tag.endswith("kml"))
		folders = [node.text for node in root.findall(".//kml:Folder/kml:name", KML_NS)]
		self.assertEqual(folders, ["Proposed boundary", "Easement corridors", "County tax lots"])
		styles = {node.get("id") for node in root.findall(".//kml:Style", KML_NS)}
		self.assertEqual(styles, {"proposed", "easement", "lot"})

	def test_the_coordinates_are_lon_lat_and_not_lat_lon(self):
		"""The bug that puts an Oregon parcel off the coast of Somalia. GeoJSON and
		KML are both lon-first, and a swap parses perfectly."""
		root = ET.fromstring(self.kml())
		text = root.find(".//kml:Placemark/kml:Polygon//kml:coordinates", KML_NS).text
		first = text.split()[0].split(",")
		self.assertAlmostEqual(float(first[0]), DRAWN[0][0], places=6)
		self.assertAlmostEqual(float(first[1]), DRAWN[0][1], places=6)
		self.assertEqual(first[2], "0")
		for triple in text.split():
			longitude, latitude, _altitude = triple.split(",")
			self.assertLess(float(longitude), -100.0)
			self.assertGreater(float(latitude), 40.0)

	def test_a_colour_is_kml_s_own_alpha_first_reversed_hex(self):
		self.assertEqual(land_export.kml_colour("#2490ef", 255), "ffef9024")
		self.assertEqual(land_export.kml_colour("#000000", 0), "00000000")
		root = ET.fromstring(self.kml())
		colours = {
			node.get("id"): node.find("kml:LineStyle/kml:color", KML_NS).text
			for node in root.findall(".//kml:Style", KML_NS)
		}
		self.assertEqual(colours["proposed"], land_export.kml_colour("#1f6fb2", 255))
		self.assertNotEqual(colours["proposed"], colours["easement"])
		self.assertNotEqual(colours["easement"], colours["lot"])

	def test_an_easement_corridor_is_a_line_and_the_lot_is_the_county_s_polygon(self):
		root = ET.fromstring(self.kml())
		easement = root.findall(".//kml:Folder", KML_NS)[1]
		self.assertIsNotNone(easement.find(".//kml:LineString", KML_NS))
		lot = root.findall(".//kml:Folder", KML_NS)[2]
		self.assertEqual(lot.find(".//kml:Placemark/kml:name", KML_NS).text, LOT)

	def test_every_placemark_carries_its_numbers_and_the_disclaimer(self):
		root = ET.fromstring(self.kml())
		placemark = root.find(".//kml:Placemark", KML_NS)
		data = {
			node.get("name"): node.find("kml:value", KML_NS).text
			for node in placemark.findall(".//kml:Data", KML_NS)
		}
		self.assertIn("Acres", data)
		self.assertEqual(data["Layer"], "Proposed boundary")
		self.assertIn(surveying.DISCLAIMER, placemark.find("kml:description", KML_NS).text)
		lot_data = {
			node.get("name"): node.find("kml:value", KML_NS).text
			for node in root.findall(".//kml:Folder", KML_NS)[2].findall(".//kml:Data", KML_NS)
		}
		self.assertEqual(lot_data["Assessor account"], "7503")
		self.assertEqual(lot_data["Owner of record"], "Highland LLC")

	def test_the_answer_says_what_it_is_and_what_is_in_it(self):
		data = self.tool_data("export_lla_kml", {"name": self.an_export_adjustment()})
		self.assertTrue(data["filename"].endswith(".kml"))
		self.assertEqual(data["content_type"], "application/vnd.google-earth.kml+xml")
		self.assertEqual(
			data["placemarks"], {"Proposed boundary": 1, "Easement corridors": 1, "County tax lots": 1}
		)

	def test_an_adjustment_with_nothing_drawn_is_refused_by_name(self):
		name = self.an_export_adjustment(geometry=False, easement=False, lots=False)
		self.assertIn("no geometry", self.tool_error("export_lla_kml", {"name": name}))

	def test_it_needs_the_land_agreements_role(self):
		name = self.an_export_adjustment()
		self.as_roles("Land Reference")
		self.assertIn("Land Agreements", self.tool_error("export_lla_kml", {"name": name}))


# ── 2 ───────────────────────────────────────────────────────────────────────
class TheGeoJsonOpensInAGis(ExportTestCase):
	def test_a_parcel_s_boundary_comes_back_as_a_feature_collection(self):
		parcel = self.a_parcel()
		data = self.tool_data("export_boundary_geojson", {"doctype": "Parcel", "name": parcel})
		self.assertEqual(data["content_type"], "application/geo+json")
		self.assertTrue(data["filename"].endswith(".geojson"))
		self.assertEqual(data["geojson"]["type"], "FeatureCollection")
		feature = data["geojson"]["features"][0]
		self.assertEqual(feature["geometry"], box(WEST, SOUTH, EAST, NORTH))
		self.assertEqual(feature["properties"]["doctype"], "Parcel")
		self.assertEqual(feature["properties"]["name"], parcel)
		self.assertGreater(feature["properties"]["acres"], 0)

	def test_a_county_tax_lot_comes_off_its_own_column(self):
		self.a_lot()
		data = self.tool_data("export_boundary_geojson", {"doctype": "County Tax Lot", "name": LOT})
		self.assertEqual(data["feature_count"], 1)
		self.assertEqual(data["geojson"]["features"][0]["properties"]["name"], LOT)

	def test_an_adjustment_carries_every_layer_labelled(self):
		name = self.an_export_adjustment()
		data = self.tool_data("export_boundary_geojson", {"doctype": "Lot Line Adjustment", "name": name})
		layers = [feature["properties"]["layer"] for feature in data["geojson"]["features"]]
		self.assertEqual(layers, ["Proposed boundary", "Easement corridors", "County tax lots"])
		self.assertEqual(data["geojson"]["features"][1]["geometry"]["type"], "LineString")

	def test_a_record_with_no_boundary_is_an_empty_collection_that_says_so(self):
		parcel = self.tool_data(
			"create_parcel", {"owning_entity": "Example Trading Co", "parcel_name": "Unmapped", "acreage": 5}
		)["name"]
		data = self.tool_data("export_boundary_geojson", {"doctype": "Parcel", "name": parcel})
		self.assertEqual((data["feature_count"], data["geojson"]["features"]), (0, []))
		self.assertIn("no boundary stored", data["warnings"][0])

	def test_a_doctype_that_is_not_land_and_a_name_that_is_not_there_are_refused(self):
		self.assertIn(
			"not a land record",
			self.tool_error("export_boundary_geojson", {"doctype": "Employee", "name": "x"}),
		)
		self.assertIn(
			"no Parcel called",
			self.tool_error("export_boundary_geojson", {"doctype": "Parcel", "name": "Nope"}),
		)

	def test_a_field_boundary_is_exportable_too(self):
		"""Field is where a block's outline lives, and a block is what a grower is
		asked for by an auditor with a GIS."""
		self.assertIn("Field", land_export.GEOMETRY_SOURCES)
		self.assertEqual(land_export.GEOMETRY_SOURCES["Field"], ("boundary_geojson",))


# ── 3 ───────────────────────────────────────────────────────────────────────
class TheMapIsDrawnFromTheGeometry(ExportTestCase):
	def shapes(self):
		return land_export.shapes_of(self.doc(self.an_export_adjustment()))

	def test_north_is_up_and_east_is_right(self):
		"""SVG's y grows downward and mercator's grows upward. Getting that wrong
		draws the whole farm upside down, and every polygon still closes."""
		view = land_export.projection(
			[
				{
					"type": "Polygon",
					"coordinates": [
						[[WEST, SOUTH], [EAST, SOUTH], [EAST, NORTH], [WEST, NORTH], [WEST, SOUTH]]
					],
				}
			]
		)
		south_west = land_export.place(view, [WEST, SOUTH])
		north_east = land_export.place(view, [EAST, NORTH])
		self.assertGreater(south_west[1], north_east[1])
		self.assertLess(south_west[0], north_east[0])

	def test_the_scale_bar_is_ground_feet_and_not_mercator_feet(self):
		"""A mercator metre at 45° N is about 0.71 of a ground metre. A bar drawn
		off the projected units would be a third short and nothing would say so."""
		view = land_export.projection(self.shapes())
		bar = land_export.scale_bar(view)
		self.assertIn(bar["feet"], land_export.SCALE_STEPS_FEET)
		start = land_export.place(view, [WEST, (SOUTH + NORTH) / 2])
		metres_per_unit = (
			1.0 / view["scale"] * __import__("math").cos(__import__("math").radians(view["mid_latitude"]))
		)
		self.assertAlmostEqual(
			bar["length"] * metres_per_unit, bar["feet"] * surveying.METRES_PER_FOOT, delta=0.5
		)
		self.assertGreater(start[0], 0)

	def test_the_sheet_carries_a_north_arrow_a_scale_and_a_label_per_shape(self):
		svg = land_export.svg_map(self.shapes(), "LLA-0001")
		self.assertTrue(svg.startswith("<svg"))
		self.assertIn(">N<", svg)
		self.assertIn("ft (", svg)
		self.assertEqual(svg.count("<path"), 4)  # three shapes plus the north arrow
		self.assertIn("Proposed boundary", svg)
		self.assertIn(LOT, svg)

	def test_nothing_external_is_referenced(self):
		"""wkhtmltopdf fetches every URL it finds, with no timeout worth the name."""
		svg = land_export.svg_map(self.shapes(), "LLA-0001")
		for forbidden in ("<img", "http://", "https://", "<script", "url("):
			with self.subTest(forbidden=forbidden):
				self.assertNotIn(forbidden, svg.replace('xmlns="http://www.w3.org/2000/svg"', ""))

	def test_a_map_with_no_shapes_is_refused_rather_than_drawn_empty(self):
		with self.assertRaises(ToolError):
			land_export.svg_map([], "nothing")


# ── 4 ───────────────────────────────────────────────────────────────────────
class TheLegalDescriptionPage(ExportTestCase):
	def test_the_page_carries_the_header_the_acreage_and_the_disclaimer(self):
		name = self.an_export_adjustment()
		data = self.tool_data("export_lla_legal_description_pdf", {"name": name, "include_html": True})
		html = data["html"]
		self.assertIn("Draft legal description", html)
		self.assertIn(LOT, html)
		self.assertIn("account 7503", html)
		self.assertIn("Acreage before and after", html)
		self.assertIn("thence", html)
		self.assertIn(surveying.DISCLAIMER, html)
		self.assertIn(frappe.utils.nowdate(), html)
		self.assertEqual(data["printed_on"], frappe.utils.nowdate())

	def test_the_before_and_after_acreage_comes_from_the_record(self):
		name = self.an_export_adjustment()
		rows = land_export.acreage_table(self.doc(name))
		self.assertEqual([row["side"] for row in rows], ["Party 1", "Party 2"])
		self.assertEqual(rows[0]["acres_before"], 40.0)
		self.assertEqual(rows[0]["lot"], LOT)

	def test_it_is_a_real_pdf_with_the_description_in_it(self):
		data = self.tool_data("export_lla_legal_description_pdf", {"name": self.an_export_adjustment()})
		pdf = base64.b64decode(data["pdf_base64"])
		self.assertTrue(pdf.startswith(b"%PDF"))
		self.assertEqual(data["pdf_bytes"], len(pdf))
		self.assertTrue(data["filename"].endswith("-legal-description.pdf"))
		self.assertEqual(data["content_type"], "application/pdf")
		self.assertIsNone(data["html"])
		if PdfReader is not None:
			text = " ".join(page.extract_text() for page in PdfReader(__import__("io").BytesIO(pdf)).pages)
			self.assertIn("thence", text)
			self.assertIn("ACREAGE BEFORE AND AFTER", text.upper())

	def test_an_adjustment_with_no_description_is_refused_by_name(self):
		name = self.an_export_adjustment(description=False)
		error = self.tool_error("export_lla_legal_description_pdf", {"name": name})
		self.assertIn("no legal description", error)
		self.assertIn("land-map", error)

	def test_the_renderer_is_named_and_wkhtmltopdf_is_used_where_there_is_one(self):
		name = self.an_export_adjustment()
		data = self.tool_data("export_lla_legal_description_pdf", {"name": name})
		self.assertEqual(data["renderer"], "erpnext_mcp render/pdf.py")
		self.assertIn("no PDF renderer", data["note"])

		import types

		module = types.ModuleType("frappe.utils.pdf")
		module.get_pdf = lambda html, **kwargs: b"%PDF-1.4 rendered " + str(len(html)).encode()
		import sys

		sys.modules["frappe.utils.pdf"] = module
		self.addCleanup(sys.modules.pop, "frappe.utils.pdf", None)
		data = self.tool_data("export_lla_legal_description_pdf", {"name": name})
		self.assertEqual(data["renderer"], "frappe.utils.pdf (wkhtmltopdf)")
		self.assertIsNone(data["note"])
		self.assertTrue(base64.b64decode(data["pdf_base64"]).startswith(b"%PDF-1.4 rendered"))


# ── 5 ───────────────────────────────────────────────────────────────────────
def reparsed(page: str) -> str:
	"""The page after the pass `frappe.utils.pdf.get_pdf` gives it.

	`inline_private_images` re-serialises through BeautifulSoup's `html.parser`,
	which is the stdlib parser below, and it hands tags back with every attribute
	name LOWERCASED. That turned the inline map's `viewBox` into `viewbox`.
	"""
	from html.parser import HTMLParser

	out = []

	class Echo(HTMLParser):
		def handle_starttag(self, tag, attrs):
			rendered = "".join(f' {key}="{value}"' for key, value in attrs)
			out.append(f"<{tag}{rendered}>")

		def handle_endtag(self, tag):
			out.append(f"</{tag}>")

		def handle_data(self, data):
			out.append(data)

	Echo(convert_charrefs=True).feed(page)
	return "".join(out)


class TheSurveyPacket(ExportTestCase):
	PREFIX = 'src="data:image/svg+xml;base64,'

	def maps(self, page: str) -> list:
		"""Every map on the page, decoded back to the SVG the renderer draws."""
		found = []
		for chunk in page.split(self.PREFIX)[1:]:
			found.append(base64.b64decode(chunk.split('"', 1)[0]).decode("utf-8"))
		return found

	def packet(self, **kwargs) -> str:
		return self.tool_data(
			"export_lla_survey_packet_pdf",
			{"name": self.an_export_adjustment(**kwargs), "include_html": True},
		)["html"]

	def test_the_maps_survive_the_parser_frappe_runs_before_wkhtmltopdf(self):
		"""v0.173.0 inlined the SVG, and every PDF printed a legend with no map:
		the parser lowercased viewBox. An image's bytes are out of its reach."""
		page = self.packet()
		self.assertNotIn("<svg", page)
		after = self.maps(reparsed(page))
		self.assertEqual(len(after), 2)
		for svg in after:
			self.assertIn('viewBox="0 0 720 470"', svg)
			self.assertIn('height="470"', svg)

	def test_the_negative_control_the_old_inline_map_loses_its_viewbox(self):
		svg = land_export.svg_map(land_export.shapes_of(self.doc(self.an_export_adjustment())), "x")
		self.assertIn("viewBox", svg)
		self.assertNotIn("viewBox", reparsed(f"<div class='map'>{svg}</div>"))

	def test_before_is_the_county_lots_and_after_is_the_proposal_over_them(self):
		before, after = self.maps(self.packet())
		self.assertIn(LOT, before)
		self.assertNotIn("Proposed boundary", before)
		self.assertIn("Proposed boundary", after)
		self.assertIn(LOT, after)
		page = self.packet()
		self.assertLess(page.index("Existing tax lots"), page.index("<h2>Proposed boundary"))

	def test_with_no_county_lot_shape_the_before_map_says_why_it_is_missing(self):
		page = self.packet(lots=False)
		self.assertEqual(len(self.maps(page)), 1)
		self.assertIn("before map cannot be drawn", page)

	def test_a_label_is_drawn_as_halo_then_ink_without_paint_order(self):
		"""wkhtmltopdf ignores paint-order, and the white stroke hid every label."""
		svg = land_export.svg_map(land_export.shapes_of(self.doc(self.an_export_adjustment())), "x")
		self.assertNotIn("paint-order", svg)
		self.assertIn(f'fill="#111">{LOT}', svg)

	def test_the_packet_is_the_description_with_the_map_above_it(self):
		data = self.tool_data(
			"export_lla_survey_packet_pdf", {"name": self.an_export_adjustment(), "include_html": True}
		)
		html = data["html"]
		self.assertIn("Survey packet", html)
		self.assertEqual(len(self.maps(html)), 2)
		self.assertIn("thence", html)
		self.assertIn("Acreage before and after", html)
		self.assertIn(surveying.DISCLAIMER, html)
		self.assertTrue(data["filename"].endswith("-survey-packet.pdf"))

	def test_without_wkhtmltopdf_the_words_survive_and_the_document_says_where_the_map_went(self):
		data = self.tool_data("export_lla_survey_packet_pdf", {"name": self.an_export_adjustment()})
		pdf = base64.b64decode(data["pdf_base64"])
		self.assertTrue(pdf.startswith(b"%PDF"))
		self.assertIn("include_html", data["note"])
		if PdfReader is not None:
			text = " ".join(page.extract_text() for page in PdfReader(__import__("io").BytesIO(pdf)).pages)
			self.assertIn("no HTML-to-PDF", text)
			self.assertIn("thence", text)

	def test_a_packet_needs_geometry_and_says_which_is_missing(self):
		no_shape = self.an_export_adjustment(geometry=False, easement=False, lots=False)
		self.assertIn("no geometry", self.tool_error("export_lla_survey_packet_pdf", {"name": no_shape}))
		no_words = self.an_export_adjustment(description=False)
		self.assertIn(
			"no legal description", self.tool_error("export_lla_survey_packet_pdf", {"name": no_words})
		)


# ── 5b ──────────────────────────────────────────────────────────────────────
class ThePacketIsWhatASurveyorWorksFrom(TheSurveyPacket):
	"""v0.174.2. "Just the outline, and the information someone will need."

	The maps are outlines and nothing else; the tables are read off the record
	and every number in them is `surveying.py`'s, so the packet cannot state a
	bearing the land map did not.
	"""

	def full(self) -> tuple:
		name = self.a_full_adjustment()
		doc = self.doc(name)
		shapes = land_export.shapes_of(doc)
		runs = land_export.traverses(shapes)
		sections = {section["heading"]: section for section in land_export.packet_sections(doc, shapes, runs)}
		return name, doc, runs, sections

	def test_the_maps_are_outlines_with_no_fill_and_no_picture(self):
		before, after = self.maps(self.packet_of(self.a_full_adjustment()))
		for svg in (before, after):
			shapes = svg.split("<path")[1:]
			self.assertTrue(shapes)
			for path in shapes:
				self.assertIn('fill="none"', path.split("/>", 1)[0])
			for forbidden in ("fill-opacity", "<image", "<img", "href", "pattern"):
				self.assertNotIn(forbidden, svg)
		self.assertIn('stroke="#111111" stroke-width="2.6"', after)
		self.assertNotIn('stroke-width="2.6"', before)

	def test_the_after_map_numbers_every_corner_and_marks_the_point_of_beginning(self):
		before, after = self.maps(self.packet_of(self.a_full_adjustment()))
		self.assertEqual(after.count("<circle"), len(DRAWN))
		self.assertIn(">1 POB<", after)
		for number in range(2, len(DRAWN) + 1):
			self.assertIn(f">{number}<", after)
		self.assertNotIn("<circle", before)

	def test_every_course_is_surveying_s_own_numbers_between_numbered_corners(self):
		_name, _doc, runs, sections = self.full()
		expected = surveying.courses(DRAWN)
		rows = sections["Courses"]["rows"]
		self.assertEqual(len(rows), len(expected))
		for row, course in zip(rows, expected, strict=True):
			self.assertEqual(row[3], course["bearing"])
			self.assertEqual(row[4], f"{course['distance_ft']:,.2f}")
			self.assertEqual(row[5], f"{course['distance_m']:,.3f}")
		self.assertEqual([row[1:3] for row in rows], [["1", "2"], ["2", "3"], ["3", "4"], ["4", "1"]])
		summary = sections["Courses"]["notes"][0]
		self.assertIn(f"{surveying.acres(DRAWN):,.3f} acres", summary)
		self.assertIn(surveying.closure(DRAWN)["precision_text"], summary)
		self.assertEqual(runs[0]["corners"][0]["point"], [float(value) for value in DRAWN[0]])

	def test_the_corner_table_is_latitude_then_longitude_in_drawn_order(self):
		_name, _doc, _runs, sections = self.full()
		rows = sections["Corner coordinates"]["rows"]
		self.assertEqual(rows[0][0], "1 (POB)")
		for row, point in zip(rows, DRAWN, strict=True):
			self.assertEqual(row[1], f"{point[1]:.7f}")
			self.assertEqual(row[2], f"{point[0]:.7f}")
		self.assertIn(f"latitude {DRAWN[0][1]:.7f}", sections["Point of beginning"]["notes"][0])
		self.assertIn(f"southwest corner of {LOT}", sections["Point of beginning"]["notes"][1])

	def test_the_people_the_lots_and_the_lender_come_off_the_record(self):
		name, doc, _runs, sections = self.full()
		parties = sections["Parties and tax lots"]["rows"]
		self.assertEqual(parties[0][2], "A Signer, President")
		self.assertEqual(parties[0][4], "7503")
		self.assertEqual(parties[1][3:7], [NEXT_LOT, "7504", "Mill Creek Farms", "4120 Dry Hollow Rd"])
		header = dict(land_export.packet_header(doc))
		self.assertEqual(header["Lender"], "Columbia Bank")
		self.assertEqual(header["Target close"], "2026-12-31")
		self.assertIn("TRUE NORTH".lower(), header["Basis of bearings"].lower())
		page = self.packet_of(name)
		self.assertIn("Fence line on the north side", page)

	def test_pieces_easements_and_open_questions_are_tabled(self):
		_name, _doc, _runs, sections = self.full()
		piece = sections["Land changing hands"]["rows"][0]
		self.assertEqual(piece[0], "Parcel A")
		self.assertEqual(piece[1], f"Party 1 — {MAIN}")
		self.assertEqual(piece[4], "—")  # a Float 0 is "not surveyed yet", never "0.000 acres"
		self.assertEqual(piece[5], "Pump house")
		self.assertEqual(
			sections["Easements on the record"]["rows"][0][:3],
			["Access", f"Party 2 — {NEIGHBOUR}", f"Party 1 — {MAIN}"],
		)
		corridor = sections["Easement corridors drawn"]["rows"][0]
		self.assertEqual(corridor[1], "centre line")
		self.assertEqual(corridor[3], "no")
		self.assertEqual(sections["Open questions"]["rows"][0][0], "Bank written consent")

	def test_a_resolved_question_is_not_on_the_sheet(self):
		name = self.a_full_adjustment()
		doc = self.doc(name)
		for row in doc.get("open_items"):
			row.update({"status": "Resolved"})
		shapes = land_export.shapes_of(doc)
		headings = [
			s["heading"] for s in land_export.packet_sections(doc, shapes, land_export.traverses(shapes))
		]
		self.assertNotIn("Open questions", headings)

	def test_a_corridor_the_new_line_cuts_is_called_out(self):
		name = self.a_full_adjustment()
		middle = (DRAWN[0][0] + DRAWN[1][0]) / 2
		across = {"type": "LineString", "coordinates": [[middle, SOUTH + 0.0001], [middle, NORTH - 0.0001]]}
		self.tool_data("lla_update", {"name": name, "fields": {"easement_geometry": json.dumps(across)}})
		doc = self.doc(name)
		shapes = land_export.shapes_of(doc)
		sections = {
			s["heading"]: s for s in land_export.packet_sections(doc, shapes, land_export.traverses(shapes))
		}
		self.assertEqual(sections["Easement corridors drawn"]["rows"][0][3], "yes — crosses it")

	def test_a_proposed_line_that_is_a_line_reads_as_an_open_traverse(self):
		line = {"type": "LineString", "coordinates": [list(point) for point in DRAWN[:3]]}
		runs = land_export.traverses([{**line, "layer": "proposed", "label": "Proposed boundary"}])
		self.assertFalse(runs[0]["closed"])
		self.assertEqual(
			[(row["from_corner"], row["to_corner"]) for row in runs[0]["courses"]], [(1, 2), (2, 3)]
		)
		self.assertIsNone(runs[0]["closure"])

	def test_the_plain_pdf_carries_the_same_tables(self):
		name = self.a_full_adjustment()
		data = self.tool_data("export_lla_survey_packet_pdf", {"name": name})
		self.assertEqual(data["renderer"], "erpnext_mcp render/pdf.py")
		if PdfReader is None:
			self.skipTest("pypdf is not installed")
		pdf = base64.b64decode(data["pdf_base64"])
		text = " ".join(page.extract_text() for page in PdfReader(__import__("io").BytesIO(pdf)).pages)
		doc = self.doc(name)
		shapes = land_export.shapes_of(doc)
		for section in land_export.packet_sections(doc, shapes, land_export.traverses(shapes)):
			with self.subTest(section=section["heading"]):
				self.assertIn(section["heading"].upper(), text)  # render/pdf.py sets headings in capitals
		self.assertIn(f"{DRAWN[0][1]:.7f}", text)
		self.assertIn("Mill Creek", text)
		self.assertIn("7504", text)

	def packet_of(self, name: str) -> str:
		return self.tool_data("export_lla_survey_packet_pdf", {"name": name, "include_html": True})["html"]

	def test_a_bench_shaped_child_row_does_not_take_the_packet_down(self):
		"""v0.174.3. The double hands back dict rows; a bench hands back Documents,
		and `dict(row)` on one raised TypeError — a 500 on Tim's first export."""
		doc = BenchDoc(self.doc(self.a_full_adjustment()))
		with self.assertRaises(TypeError):
			dict(doc.get("easements")[0])  # the control: this is what the bench does
		page = land_export.survey_packet_html(doc)
		for expected in ("Parcel A", "Truck access to the shop.", "Bank written consent"):
			self.assertIn(expected, page)
		self.assertTrue(land_export.plain_pdf(doc, "packet").startswith(b"%PDF"))
		self.assertEqual(land.side_plan(doc, land.PARTY_1)["pieces_given"], ["Parcel A"])

	def test_child_rows_reads_both_shapes_alike(self):
		doc = self.doc(self.a_full_adjustment())
		self.assertEqual(
			[row["piece_name"] for row in land.child_rows(BenchDoc(doc), "pieces")],
			[row["piece_name"] for row in land.child_rows(doc, "pieces")],
		)
		self.assertEqual(land.child_rows(BenchDoc(doc), "no_such_table"), [])


class BenchRow:
	"""A child row the way a bench hands one back: fields, `get`, `as_dict`,
	`update` — and no mapping protocol, so `dict(row)` raises as Frappe's
	`BaseDocument` does."""

	def __init__(self, values: dict):
		self.__dict__.update(values)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)

	def as_dict(self) -> dict:
		return dict(self.__dict__)

	def update(self, values: dict) -> None:
		self.__dict__.update(values)


class BenchDoc:
	"""A harness document whose child tables come back as `BenchRow`s."""

	TABLES = ("pieces", "easements", "open_items")

	def __init__(self, doc):
		self._doc = doc
		self.name = doc.name

	def get(self, key, default=None):
		value = self._doc.get(key, default)
		if key in self.TABLES:
			return [BenchRow(dict(row)) for row in value or []]
		return value


# ── 6 ───────────────────────────────────────────────────────────────────────
class TheDeskDownloads(ExportTestCase):
	def response(self) -> dict:
		return dict(frappe.local.response)

	def setUp(self):
		super().setUp()
		frappe.local.response.clear()
		self.addCleanup(frappe.local.response.clear)

	def test_the_kml_method_answers_a_file(self):
		land_map.export_kml(name=self.an_export_adjustment())
		answer = self.response()
		self.assertEqual(answer["type"], "download")
		self.assertEqual(answer["content_type"], land_export.KML_CONTENT_TYPE)
		self.assertTrue(answer["filename"].endswith(".kml"))
		self.assertTrue(answer["filecontent"].startswith(b"<?xml"))

	def test_the_geojson_method_answers_the_right_content_type(self):
		parcel = self.a_parcel()
		land_map.export_geojson(doctype="Parcel", name=parcel)
		answer = self.response()
		self.assertEqual(answer["content_type"], "application/geo+json")
		self.assertEqual(json.loads(answer["filecontent"])["type"], "FeatureCollection")

	def test_the_pdf_method_serves_both_documents(self):
		name = self.an_export_adjustment()
		land_map.export_pdf(name=name, kind="legal")
		self.assertTrue(self.response()["filename"].endswith("-legal-description.pdf"))
		land_map.export_pdf(name=name, kind="packet")
		answer = self.response()
		self.assertTrue(answer["filename"].endswith("-survey-packet.pdf"))
		self.assertTrue(answer["filecontent"].startswith(b"%PDF"))
		self.assertEqual(answer["content_type"], "application/pdf")

	def test_a_login_without_read_permission_on_the_record_is_refused(self):
		name = self.an_export_adjustment()
		STORE.denied_permissions.add((land.LOT_LINE_ADJUSTMENT, "read"))
		self.addCleanup(STORE.denied_permissions.discard, (land.LOT_LINE_ADJUSTMENT, "read"))
		with self.assertRaises(frappe.PermissionError):
			land_map.export_kml(name=name)
		self.assertEqual(self.response(), {})

	def test_guest_gets_nothing(self):
		name = self.an_export_adjustment()
		frappe.local.session.user = "Guest"
		self.addCleanup(setattr, frappe.local.session, "user", "Administrator")
		with self.assertRaises(frappe.PermissionError):
			land_map.export_kml(name=name)

	def test_a_document_kind_that_is_not_one_is_refused(self):
		name = self.an_export_adjustment()
		with self.assertRaises(Exception) as caught:
			land_map.export_pdf(name=name, kind="poster")
		self.assertIn("poster", str(caught.exception))


# ── 7 ───────────────────────────────────────────────────────────────────────
class TheButtonsAreOnTheMap(unittest.TestCase):
	def script(self) -> str:
		return (PAGE_DIR / "land_map.js").read_text()

	def template(self) -> str:
		return (PAGE_DIR / "land_map.html").read_text()

	def test_every_export_button_exists_in_both_files(self):
		template, script = self.template(), self.script()
		for hook in ("lm-export-kml", "lm-export-geojson", "lm-export-pdf"):
			with self.subTest(hook=hook):
				self.assertIn(hook, template)
				self.assertIn(hook, script)

	def test_the_methods_the_buttons_name_are_whitelisted(self):
		script = self.script()
		for method in ("export_kml", "export_geojson", "export_pdf"):
			with self.subTest(method=method):
				self.assertIn(f"erpnext_mcp.api.land_map.{method}", script)
				self.assertTrue(getattr(getattr(land_map, method), "whitelisted", True))

	def test_the_template_carries_no_straight_apostrophe(self):
		"""Frappe compiles the page HTML into a SINGLE-quoted JS string."""
		self.assertNotIn("'", self.template())


@unittest.skipUnless(shutil.which("node"), "needs node to execute the page script")
class TheButtonsRun(unittest.TestCase):
	"""THE BUTTONS, CLICKED. A substring assertion matches the whole file; this
	drives them and reads the URLs the page actually opened."""

	report: ClassVar[dict] = {}

	@classmethod
	def setUpClass(cls):
		from .test_land_map import HARNESS, page_fixtures

		harness = (
			HARNESS.replace(
				# Clicked BEFORE anything is saved: an export reads the stored record, so
				# with no adjustment chosen the page must say so and open nothing.
				'		root.find(".lm-compute").click();',
				"""		["lm-export-kml", "lm-export-geojson", "lm-export-pdf"].forEach((button) => {
			root.find("." + button).click();
		});
		calls.opened_before_saving = calls.opened.length;
		root.find(".lm-compute").click();""",
			)
			.replace(
				'		calls.legal = root.find(".lm-legal").val();',
				"""		["lm-export-kml", "lm-export-geojson", "lm-export-pdf"].forEach((button) => {
			root.find("." + button).click();
		});
		calls.legal = root.find(".lm-legal").val();""",
			)
			.replace(
				# STRUCTURAL, NOT THE LINE AS IT READS TODAY. This used to name the
				# whole `const calls = {...}` line verbatim, so v0.174.0 adding one
				# key to it in `test_land_map` turned this into a silent no-op:
				# `calls.opened` was never created and all three tests here failed
				# inside `window.open` with a TypeError that named neither file.
				# Anchoring on the closing brace survives another key being added.
				"indicators: []",
				"indicators: [], opened: []",
			)
			.replace(
				# The harness points `window` at the global object AFTER the stubs are
				# built, so the recorder has to be hung on it afterwards or it is replaced.
				"global.window = global;",
				"global.window = global;\nglobal.window.open = function (url) { calls.opened.push(url); };",
			)
			.replace(
				# `save_proposal` answers the SAVED RECORD on a bench, and the page reads
				# `name` off it to know which adjustment it is now on — which is the
				# adjustment an export then asks for. The base harness answers the map
				# payload to every call, so this one is spelled out.
				'const answer = options.method.endsWith("survey_preview") ? PREVIEW : ANSWER;',
				'let answer = options.method.endsWith("survey_preview") ? PREVIEW : ANSWER;\n'
				'\t\tif (options.method.endsWith("save_proposal")) {\n'
				'\t\t\tanswer = { name: "LLA-2026-0001", created: false, summary: "saved" };\n'
				"\t\t}",
			)
		)
		# EVERY PATCH ABOVE IS A STRING REPLACE INTO ANOTHER TEST FILE'S HARNESS,
		# and `str.replace` that matches nothing returns the string unchanged and
		# says nothing. That is how v0.174.0 broke this class: one key added to
		# `calls` in test_land_map, one anchor here quietly matching nothing, and
		# three failures inside `window.open` naming neither cause. A miss is now
		# a named failure here rather than a TypeError over there.
		for fragment in (
			"calls.opened.push(url)",
			"opened: []",
			"calls.opened_before_saving",
			'answer = { name: "LLA-2026-0001"',
		):
			assert fragment in harness, (
				f"the export harness patch for {fragment!r} matched nothing — "
				"test_land_map.HARNESS changed under it, so re-anchor the replace above"
			)

		answer, preview = page_fixtures()
		with tempfile.TemporaryDirectory() as folder:
			path = Path(folder) / "harness.js"
			path.write_text(harness)
			result = subprocess.run(
				["node", str(path), str(PAGE_DIR / "land_map.js"), json.dumps(answer), json.dumps(preview)],
				capture_output=True,
				text=True,
				timeout=60,
			)
		if result.returncode != 0:
			raise AssertionError(f"the page script would not run: {result.stderr[-2000:]}")
		cls.report = json.loads(result.stdout.strip().splitlines()[-1])

	def test_it_ran(self):
		self.assertNotIn("error", self.report, self.report.get("error", ""))

	def test_an_export_before_the_drawing_is_saved_opens_nothing_and_says_why(self):
		self.assertEqual(self.report.get("opened_before_saving"), 0)
		said = [line for line in self.report["methods"] if str(line).startswith("msgprint:")]
		self.assertTrue(said, self.report["methods"])
		self.assertIn("save this drawing", said[0])

	def test_each_button_opens_its_own_method_for_the_chosen_adjustment(self):
		opened = self.report.get("opened", [])
		self.assertEqual(len(opened), 3)
		self.assertIn("export_kml?name=LLA-2026-0001", opened[0])
		self.assertIn("export_geojson?name=LLA-2026-0001", opened[1])
		self.assertIn("doctype=Lot%20Line%20Adjustment", opened[1])
		self.assertIn("export_pdf?name=LLA-2026-0001", opened[2])
		self.assertIn("kind=packet", opened[2])

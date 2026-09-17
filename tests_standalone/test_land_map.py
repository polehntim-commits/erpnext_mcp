# SPDX-License-Identifier: MIT
"""The land planning map: the three methods, the page on disk, and the page running.

FIVE CLAIMS.

1. `TheAnswer` — one call carries the farm map's own layers PLUS the county tax
   lots, and the box it opens on covers the neighbour's ground too.
2. `TheDrawnLine` — what a traced line comes back as: courses, closure, acreage,
   a tie-in, the easements it crosses and the lots it falls on.
3. `TheSave` — the proposal is written THROUGH `lla_update`/`lla_create`, so the
   role gate and the status machine apply. A caller without the role is refused.
4. `ThePageOnDisk` — the Page record, the script and the template agree with each
   other, and every method the script names exists and is whitelisted.
5. `ThePageRuns` — the script is EXECUTED under a stubbed Desk. A substring
   assertion on a page script matches the whole file; the question is whether
   clicking Compute posts the drawn points to `survey_preview` and puts a row on
   screen for every course that comes back, and only running it answers that.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

import frappe

from erpnext_mcp import land_adjustment as land
from erpnext_mcp import surveying
from erpnext_mcp.api import land_map
from erpnext_mcp.errors import ToolError

from .fixtures import MAIN, V12TestCase
from .harness import ROLES, STORE

REPO = Path(__file__).resolve().parent.parent
PAGE_DIR = REPO / "erpnext_mcp" / "erpnext_mcp" / "page" / "land_map"
WIDGET = REPO / "erpnext_mcp" / "public" / "js" / "geo_map_widget.js"

LOT = "1N 13E 3 1000"
STORED_CORRIDOR = "Recorded ditch easement"
#: The well on LLA-2026-0002, as its own notes write it. Wasco County is at
#: longitude 121 WEST, so the stored longitude is NEGATIVE.
WELL = (45.577088, -121.1940661)
NEIGHBOUR = "Highland LLC"

SOUTH, NORTH = 45.5800, 45.5820
WEST, EAST = -121.1640, -121.1610


def box(west, south, east, north) -> dict:
	return {
		"type": "Polygon",
		"coordinates": [[[west, south], [east, south], [east, north], [west, north], [west, south]]],
	}


#: The drawn line: a wedge inside the tax lot, traced as four clicks.
DRAWN = [
	[WEST + 0.0005, SOUTH + 0.0005],
	[WEST + 0.0015, SOUTH + 0.0005],
	[WEST + 0.0015, SOUTH + 0.0012],
	[WEST + 0.0005, SOUTH + 0.0012],
]


class LandMapTestCase(V12TestCase):
	SWITCHES: ClassVar[dict] = {
		f"allow_{name}": 1 for name in ("create_parcel", "get_parcel", "lla_create", "lla_update", "lla_get")
	}

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **self.SWITCHES)
		ROLES["Administrator"] = ["System Manager", land.LAND_AGREEMENTS_ROLE]
		# An adjustment is between two owners, so the fixture needs a neighbour.
		STORE.seed(
			"Company",
			[
				{
					"name": NEIGHBOUR,
					"abbr": "HLD",
					"default_currency": "USD",
					"country": "United States",
					"is_group": 0,
				}
			],
		)

	def as_roles(self, *roles):
		ROLES["Administrator"] = list(roles)

	def a_lot(self, geometry=None) -> str:
		name, _created = land.upsert_tax_lot(
			{
				"map_taxlot": LOT,
				"county": "Wasco",
				"account": "7503",
				"owner_of_record": "Highland LLC",
				"acres_gis": 40.0,
				"source": "Manual",
				"geometry": geometry or box(WEST, SOUTH, EAST, NORTH),
			}
		)
		return name

	def a_parcel(self) -> str:
		parcel = self.tool_data(
			"create_parcel",
			{
				"owning_entity": MAIN,
				"parcel_name": "Mill Creek",
				"acreage": 40.0,
				"county": "Wasco",
				"state": "OR",
			},
		)["name"]
		frappe.db.set_value("Parcel", parcel, "boundary_geojson", json.dumps(box(WEST, SOUTH, EAST, NORTH)))
		return parcel

	def an_adjustment(self, **fields) -> str:
		return self.tool_data(
			"lla_create",
			{
				"title": "Mill Creek / Highland lot line adjustment",
				"party_1_type": "Company",
				"party_1": MAIN,
				"party_2_type": "Company",
				"party_2": NEIGHBOUR,
				"signer_1": "A Signer",
				"signer_1_title": "President",
				"signer_2": "B Signer",
				"signer_2_title": "Member",
				**fields,
			},
		)["name"]


# ── 1 ───────────────────────────────────────────────────────────────────────
class TheAnswer(LandMapTestCase):
	def test_one_call_carries_the_farm_layers_and_the_tax_lots(self):
		self.a_parcel()
		self.a_lot()
		answer = land_map._land_map()
		self.assertIn("layers", answer)
		self.assertEqual(answer["tax_lots"]["doctype"], "County Tax Lot")
		self.assertEqual([shape["label"] for shape in answer["tax_lots"]["shapes"]], [LOT])

	def test_a_tax_lot_carries_what_its_popup_prints(self):
		self.a_lot()
		shape = land_map._land_map()["tax_lots"]["shapes"][0]
		self.assertEqual(shape["acres"], 40.0)
		self.assertIn("Account 7503", shape["detail"])
		self.assertIn("Highland LLC", shape["detail"])
		self.assertTrue(shape["route"].startswith("/app/county-tax-lot/"))
		self.assertEqual(shape["geometry"]["type"], "Polygon")

	def test_the_box_opens_on_the_tax_lots_too(self):
		"""An adjustment is read against the neighbour's ground, which is exactly
		what the farm map leaves out — so a page fitted to the farm alone would
		open with the other lot off screen."""
		self.a_lot(box(WEST - 0.05, SOUTH - 0.05, WEST - 0.04, SOUTH - 0.04))
		answer = land_map._land_map()
		bounds = answer["bounds"]
		self.assertLessEqual(bounds[0][0], SOUTH - 0.05)
		self.assertLessEqual(bounds[0][1], WEST - 0.05)

	def test_a_lot_whose_geometry_will_not_parse_is_reported_not_drawn(self):
		self.a_lot()
		frappe.db.set_value(land.COUNTY_TAX_LOT, LOT, "geometry", "{not json")
		answer = land_map._land_map()
		self.assertEqual(answer["tax_lots"]["shapes"], [])
		self.assertTrue(any(row["name"] == LOT for row in answer["unreadable"]))

	def test_a_register_this_login_may_not_read_takes_its_layer_and_not_the_page(self):
		self.a_lot()
		STORE.denied_permissions.add((land.COUNTY_TAX_LOT, "read"))
		self.addCleanup(STORE.denied_permissions.discard, (land.COUNTY_TAX_LOT, "read"))
		answer = land_map._land_map()
		self.assertTrue(answer["tax_lots"]["refused"])
		self.assertEqual(answer["tax_lots"]["shapes"], [])

	def test_the_adjustments_come_with_their_proposals(self):
		name = self.an_adjustment()
		frappe.db.set_value(
			land.LOT_LINE_ADJUSTMENT, name, "proposed_geometry", json.dumps(surveying.polygon(DRAWN))
		)
		row = next(row for row in land_map._land_map()["adjustments"] if row["name"] == name)
		self.assertEqual(row["proposed_geometry"]["type"], "Polygon")
		self.assertFalse(row["has_description"])
		self.assertEqual(row["route"], f"/app/lot-line-adjustment/{name}")

	def test_easement_corridors_are_read_off_the_adjustment(self):
		name = self.an_adjustment()
		corridors = {
			"type": "FeatureCollection",
			"features": [
				{
					"type": "Feature",
					"properties": {"label": "Dry Hollow ditch", "easement_type": "Irrigation"},
					"geometry": {"type": "LineString", "coordinates": [[WEST, SOUTH], [EAST, NORTH]]},
				}
			],
		}
		frappe.db.set_value(land.LOT_LINE_ADJUSTMENT, name, "easement_geometry", json.dumps(corridors))
		row = next(row for row in land_map._land_map()["adjustments"] if row["name"] == name)
		self.assertEqual([corridor["label"] for corridor in row["easements"]], ["Dry Hollow ditch"])
		self.assertEqual(row["easements"][0]["easement_type"], "Irrigation")

	def test_the_answer_says_whether_shapely_is_here(self):
		answer = land_map._land_map()
		self.assertIn("shapely", answer["geometry_support"])
		self.assertTrue(answer["disclaimer"].startswith("This description was generated"))


# ── 2 ───────────────────────────────────────────────────────────────────────
class TheDrawnLine(LandMapTestCase):
	def test_it_measures_what_was_drawn(self):
		self.a_lot()
		answer = land_map._survey_preview(points=json.dumps(DRAWN))
		self.assertEqual(len(answer["courses"]), 4)
		self.assertTrue(answer["courses"][-1]["closing"])
		self.assertGreater(answer["acres"], 0)
		self.assertIn("1 part in", answer["closure"]["precision_text"])

	def test_the_description_is_tied_to_a_mapped_corner(self):
		self.a_lot()
		answer = land_map._survey_preview(points=json.dumps(DRAWN))
		self.assertTrue(answer["tie_in"]["found"])
		self.assertIn(LOT, answer["tie_in"]["description"])
		self.assertIn("thence", answer["legal_description"])
		self.assertTrue(answer["legal_description"].endswith(surveying.DISCLAIMER))

	def test_leaflets_own_shape_is_accepted(self):
		"""The browser hands back `{lat, lng}`, GeoJSON says `[lon, lat]`, and a
		page that had to convert would be a second place to get it backwards."""
		self.a_lot()
		leafletish = [{"lat": point[1], "lng": point[0]} for point in DRAWN]
		answer = land_map._survey_preview(points=leafletish)
		self.assertEqual(answer["points"], [[round(point[0], 7), round(point[1], 7)] for point in DRAWN])

	def test_a_geojson_feature_is_accepted_too(self):
		self.a_lot()
		feature = {"type": "Feature", "properties": {}, "geometry": surveying.polygon(DRAWN)}
		answer = land_map._survey_preview(points=feature)
		self.assertEqual(len(answer["courses"]), 4)

	def test_one_point_is_refused_by_name(self):
		with self.assertRaises(ToolError) as caught:
			land_map._survey_preview(points=[[WEST, SOUTH]])
		self.assertIn("at least two points", str(caught.exception))

	def test_an_easement_it_crosses_is_flagged(self):
		self.a_lot()
		name = self.an_adjustment()
		ditch = {
			"type": "LineString",
			"coordinates": [[WEST, SOUTH + 0.0008], [EAST, SOUTH + 0.0008]],
		}
		frappe.db.set_value(
			land.LOT_LINE_ADJUSTMENT,
			name,
			"easement_geometry",
			json.dumps({"type": "Feature", "properties": {"label": "Ditch"}, "geometry": ditch}),
		)
		answer = land_map._survey_preview(points=json.dumps(DRAWN), adjustment=name)
		self.assertEqual([row["label"] for row in answer["easement_crossings"]], ["Ditch"])

	def test_an_easement_somewhere_else_is_not_flagged(self):
		"""THE NEGATIVE CONTROL. A flag that is always raised is not a check."""
		self.a_lot()
		name = self.an_adjustment()
		far = {"type": "LineString", "coordinates": [[WEST + 1, SOUTH + 1], [EAST + 1, NORTH + 1]]}
		frappe.db.set_value(
			land.LOT_LINE_ADJUSTMENT,
			name,
			"easement_geometry",
			json.dumps({"type": "Feature", "properties": {"label": "Far ditch"}, "geometry": far}),
		)
		answer = land_map._survey_preview(points=json.dumps(DRAWN), adjustment=name)
		self.assertEqual(answer["easement_crossings"], [])
		self.assertEqual(answer["easements_considered"], 1)

	def test_the_affected_lot_is_reported_both_ways_and_never_decided(self):
		"""A polygon drawn across a line does not say who is giving the ground
		up, and this refuses to guess. See the module docstring in api/land_map."""
		self.a_lot()
		answer = land_map._survey_preview(points=json.dumps(DRAWN))
		row = next(row for row in answer["affected"] if row["label"] == LOT)
		self.assertEqual(row["acres_before"], 40.0)
		if row["overlap_acres"] is not None:
			self.assertLess(row["acres_if_giving"], row["acres_before"])
			self.assertGreater(row["acres_if_receiving"], row["acres_before"])
		else:
			self.assertIn("shapely", row["note"])

	def test_a_lot_the_drawing_misses_is_not_listed(self):
		self.a_lot(box(WEST + 1, SOUTH + 1, EAST + 1, NORTH + 1))
		answer = land_map._survey_preview(points=json.dumps(DRAWN))
		self.assertEqual([row["label"] for row in answer["affected"] if row["label"] == LOT], [])

	def test_naming_an_adjustment_adds_its_own_before_and_after(self):
		self.a_lot()
		name = self.an_adjustment(lot_1=LOT)
		answer = land_map._survey_preview(points=json.dumps(DRAWN), adjustment=name)
		self.assertEqual(answer["adjustment"], name)
		self.assertEqual([side["side"] for side in answer["sides"]], [land.PARTY_1, land.PARTY_2])
		self.assertIn(name, answer["legal_description"])


# ── 3 ───────────────────────────────────────────────────────────────────────
class TheSave(LandMapTestCase):
	def test_it_writes_the_proposal_onto_an_existing_adjustment(self):
		name = self.an_adjustment()
		answer = land_map._save_proposal(
			adjustment=name,
			proposed_geometry=surveying.polygon(DRAWN),
			legal_description="Beginning at a point; thence due North, 100.00 feet.",
		)
		self.assertEqual((answer["name"], answer["created"]), (name, False))
		stored = frappe.db.get_value(land.LOT_LINE_ADJUSTMENT, name, "proposed_geometry")
		self.assertEqual(json.loads(stored)["type"], "Polygon")

	def test_it_goes_through_the_tool_so_the_role_gate_applies(self):
		"""`lla_update` carries the role gate, the status machine and the
		after-submit lock. A Desk method writing the document itself would be a
		way round all three."""
		name = self.an_adjustment()
		self.as_roles("Land Reference")
		with self.assertRaises(ToolError) as caught:
			land_map._save_proposal(adjustment=name, proposed_geometry=surveying.polygon(DRAWN))
		self.assertIn(land.LAND_AGREEMENTS_ROLE, str(caught.exception))

	def test_it_can_create_one_when_given_a_title(self):
		answer = land_map._save_proposal(
			title="Drawn on the map",
			proposed_geometry=surveying.polygon(DRAWN),
			legal_description="Draft.",
		)
		self.assertTrue(answer["created"])
		self.assertEqual(
			frappe.db.get_value(land.LOT_LINE_ADJUSTMENT, answer["name"], "title"), "Drawn on the map"
		)

	def test_creating_without_a_title_is_refused_by_name(self):
		with self.assertRaises(ToolError) as caught:
			land_map._save_proposal(proposed_geometry=surveying.polygon(DRAWN))
		self.assertIn("give it a title", str(caught.exception))

	def test_saving_nothing_is_refused(self):
		name = self.an_adjustment()
		with self.assertRaises(ToolError) as caught:
			land_map._save_proposal(adjustment=name)
		self.assertIn("nothing to save", str(caught.exception))

	def test_a_geometry_that_is_not_geojson_is_refused_before_it_is_stored(self):
		name = self.an_adjustment()
		with self.assertRaises(ToolError) as caught:
			land_map._save_proposal(adjustment=name, proposed_geometry='{"type": "Banana"}')
		self.assertIn("proposed_geometry", str(caught.exception))
		self.assertFalse(frappe.db.get_value(land.LOT_LINE_ADJUSTMENT, name, "proposed_geometry"))

	def test_a_feature_collection_of_easements_is_stored_whole(self):
		name = self.an_adjustment()
		corridors = {
			"type": "FeatureCollection",
			"features": [
				{
					"type": "Feature",
					"properties": {"label": "Ditch"},
					"geometry": {"type": "LineString", "coordinates": [[WEST, SOUTH], [EAST, NORTH]]},
				}
			],
		}
		land_map._save_proposal(adjustment=name, easement_geometry=corridors)
		stored = json.loads(frappe.db.get_value(land.LOT_LINE_ADJUSTMENT, name, "easement_geometry"))
		self.assertEqual(stored["features"][0]["properties"]["label"], "Ditch")

	def test_the_three_columns_survive_submission(self):
		"""The surveyor's answer arrives after the adjustment has gone to the
		county, and the drawing it corrects is this one."""
		for fieldname in ("proposed_geometry", "easement_geometry", "generated_legal_description"):
			with self.subTest(fieldname=fieldname):
				self.assertIn(fieldname, land.after_submit_fields())


# ── 4 ───────────────────────────────────────────────────────────────────────
class ThePageOnDisk(unittest.TestCase):
	def record(self) -> dict:
		return json.loads((PAGE_DIR / "land_map.json").read_text())

	def script(self) -> str:
		return (PAGE_DIR / "land_map.js").read_text()

	def template(self) -> str:
		return (PAGE_DIR / "land_map.html").read_text()

	def test_the_record_is_a_standard_page_in_this_apps_module(self):
		record = self.record()
		self.assertEqual(record["doctype"], "Page")
		self.assertEqual(record["module"], "ERPNext MCP")
		self.assertEqual(record["standard"], "Yes")
		self.assertEqual(record["roles"], [])

	def test_the_route_is_the_one_the_script_keys_on(self):
		"""Frappe keys `frappe.pages` on the Page's own name."""
		record = self.record()
		self.assertEqual(record["name"], "land-map")
		self.assertEqual(record["page_name"], "land-map")
		self.assertIn('frappe.pages["land-map"]', self.script())

	def test_the_folder_is_the_scrubbed_route(self):
		self.assertEqual(PAGE_DIR.name, "land_map")
		self.assertTrue((PAGE_DIR / "__init__.py").is_file())

	def test_the_script_carries_the_licence_header_on_its_first_line(self):
		self.assertEqual(self.script().split("\n", 1)[0], "// SPDX-License-Identifier: MIT")

	def test_every_method_the_script_calls_exists_and_is_whitelisted(self):
		script = self.script()
		for method in ("land_map", "survey_preview", "save_proposal"):
			with self.subTest(method=method):
				self.assertIn(f'"erpnext_mcp.api.land_map.{method}"', script)
				self.assertTrue(getattr(getattr(land_map, method), "whitelisted", True))

	def test_the_script_holds_no_tile_url_or_cdn_of_its_own(self):
		"""The CDN, the tile URLs and the attributions live in geo_map_widget.js
		and a second copy is a second place for one to be dropped."""
		script = self.script()
		for forbidden in ("arcgisonline", "tile.openstreetmap", "cdnjs.cloudflare", "leaflet.js"):
			with self.subTest(forbidden=forbidden):
				self.assertNotIn(forbidden, script)
		self.assertIn("erpnext_mcp.geo_map.add_base_layers", script)

	def test_the_widget_exports_the_draw_loader_this_page_reaches_for(self):
		widget = WIDGET.read_text()
		for export in ("load_draw", "load_leaflet", "add_base_layers", "MAX_FIT_ZOOM", "HOME_VIEW"):
			with self.subTest(export=export):
				self.assertIn(f"erpnext_mcp.geo_map.{export} =", widget)
				self.assertIn(f"erpnext_mcp.geo_map.{export}", self.script())

	def test_the_page_fetches_the_widget_from_this_apps_asset_path(self):
		self.assertIn('"/assets/erpnext_mcp/js/geo_map_widget.js"', self.script())

	def test_the_template_opens_on_its_own_root_element(self):
		"""jQuery turns a leading comment into a node of its own, and every
		`.find()` in the script would then miss."""
		self.assertTrue(self.template().lstrip().startswith('<div class="lm-wrap">'))

	def test_the_template_holds_no_straight_apostrophe(self):
		"""Frappe compiles this into a single-quoted JS string."""
		self.assertNotIn("'", self.template())

	def test_the_template_carries_every_hook_the_script_reaches_for(self):
		template = self.template()
		script = self.script()
		for hook in (
			"lm-company",
			"lm-adjustment",
			"lm-map",
			"lm-fallback",
			"lm-status",
			"lm-course-rows",
			"lm-flags",
			"lm-acreage",
			"lm-legal",
			"lm-print",
			"lm-draw-line",
			"lm-draw-area",
			"lm-draw-easement",
			"lm-compute",
			"lm-save",
		):
			with self.subTest(hook=hook):
				self.assertIn(hook, template)
				self.assertIn(hook, script)

	def test_the_map_canvas_has_a_height(self):
		"""A Leaflet container with no height renders a map nobody can see."""
		self.assertRegex(self.template(), r"\.lm-map\s*\{[^}]*height:")

	def test_the_form_button_routes_to_this_page(self):
		self.assertIn('frappe.set_route("land-map")', self.form_script())
		self.assertIn("lot_line_adjustment: frm.doc.name", self.form_script())
		self.assertIn("lot_line_adjustment", self.script())

	# ── v0.174.0: the form draws a map of its own ───────────────────────────
	@staticmethod
	def form_script() -> str:
		return (REPO / "erpnext_mcp" / "public" / "js" / "lot_line_adjustment_map.js").read_text()

	def test_the_form_script_carries_the_licence_header_on_its_first_line(self):
		self.assertEqual(self.form_script().split("\n", 1)[0], "// SPDX-License-Identifier: MIT")

	def test_the_form_asks_for_the_method_that_exists_and_is_whitelisted(self):
		self.assertIn('"erpnext_mcp.api.land_map.adjustment_map"', self.form_script())
		self.assertTrue(getattr(land_map.adjustment_map, "whitelisted", True))

	def test_the_form_draws_through_the_shared_widget(self):
		"""Not its own Leaflet, not its own tiles — the widget every other
		map-carrying form in this app renders through."""
		self.assertIn("erpnext_mcp.geo_map.render", self.form_script())
		for forbidden in ("arcgisonline", "tile.openstreetmap", "cdnjs.cloudflare", "leaflet.js"):
			with self.subTest(forbidden=forbidden):
				self.assertNotIn(forbidden, self.form_script())

	def test_the_form_never_writes_a_corridor_into_the_proposed_column(self):
		"""AN ACCESS CORRIDOR IS NOT A LOT LINE.

		`proposed_geometry` drives the legal description, the before-and-after
		acreage and the survey packet. A corridor written there would have the
		app computing "acres if giving" for a driveway and printing it into a
		description as somebody's new boundary. The form is read-only, so the
		only correct number of writes is none at all.
		"""
		script = self.form_script()
		for forbidden in ("proposed_geometry:", "easement_geometry:", "save_proposal", "frm.set_value"):
			with self.subTest(forbidden=forbidden):
				self.assertNotIn(forbidden, script)

	def test_the_widget_takes_the_list_of_fixes_the_form_hands_it(self):
		"""`points` is the plural the form passes; `point` is the singular the
		other seven forms pass, and it is still there."""
		widget = WIDGET.read_text()
		self.assertIn("spec.points || []", widget)
		self.assertIn("if (spec.point) {", widget)
		self.assertIn("points: answer.points || []", self.form_script())


# ── 6 ───────────────────────────────────────────────────────────────────────
class TheGPSInTheNotes(unittest.TestCase):
	"""Reading a coordinate out of prose, and — mostly — refusing to.

	A marker in the wrong place is worse than no marker, because a map that
	rendered looks like a map that is right. Every refusal here is a shape this
	app's own notes actually contain.
	"""

	def test_a_fix_with_a_degree_sign_is_read(self):
		notes = "Well at GPS 45.577088\u00b0N, 121.1940661\u00b0W (asset HI-BUNN-WELL)."
		self.assertEqual(
			land_map._gps_points(notes, "Well"),
			[{"lat": 45.577088, "lon": -121.1940661, "label": "Well"}],
		)

	def test_a_fix_without_a_degree_sign_is_read(self):
		notes = "shared well at GPS 45.577088N, 121.1940661W. Includes access corridor"
		self.assertEqual(land_map._gps_points(notes)[0]["lat"], 45.577088)

	def test_west_is_negative(self):
		"""THE ONE THAT PUTS A WELL IN KAZAKHSTAN.

		Wasco County is at longitude 121 WEST. Every geometry this app stores
		writes that as -121, and a note writes it as 121W. Taking the number at
		face value draws a marker, cleanly, 5,000 miles away.
		"""
		self.assertEqual(land_map._gps_points("45.5N, 121.19W")[0]["lon"], -121.19)

	def test_south_is_negative(self):
		self.assertEqual(land_map._gps_points("12.5S, 45.25E")[0]["lat"], -12.5)

	def test_a_township_and_range_is_not_a_coordinate(self):
		"""`1N 13E 9 2700` IS THE DOCNAME OF THE TAX LOT THIS FEATURE IS FOR.

		Read as a fix it is 1\u00b0N 13\u00b0E, in the Gulf of Guinea, and it would
		draw without complaint on the very records this parser runs against.
		"""
		for notes in ("Section 9, T1N R13E", "lot 1N 13E 9 2700", "T2S R14E of the W.M."):
			with self.subTest(notes=notes):
				self.assertEqual(land_map._gps_points(notes), [])

	def test_a_statute_citation_is_not_a_coordinate(self):
		notes = "Formal easement per ORS 105.170-105.185. Min 20-ft radius around well."
		self.assertEqual(land_map._gps_points(notes), [])

	def test_an_unsigned_pair_gets_no_marker_rather_than_a_guessed_one(self):
		self.assertEqual(land_map._gps_points("45.577088, -121.1940661"), [])

	def test_either_order_is_read(self):
		self.assertEqual(
			land_map._gps_points("121.1940661W, 45.577088N"),
			land_map._gps_points("45.577088N, 121.1940661W"),
		)

	def test_an_out_of_range_pair_is_refused(self):
		self.assertEqual(land_map._gps_points("95.5N, 200.5W"), [])

	def test_a_child_row_reads_the_same_in_both_shapes(self):
		"""THE SUITE ONLY EVER SEES ONE OF THESE.

		The standalone double hands back dicts; a bench hands back `Document`
		objects. Only one branch runs here, so the other is asserted directly —
		otherwise the accessor is correct for the harness and silently empty on
		the site, which is the one place it matters.
		"""

		class Bench:  # what a real Frappe child row behaves like
			def __init__(self, notes):
				self.notes = notes

			def get(self, key):
				return getattr(self, key, None)

		notes = "GPS 45.577088N, 121.1940661W"
		self.assertEqual(land_map._row_field({"notes": notes}, "notes"), notes)
		self.assertEqual(land_map._row_field(Bench(notes), "notes"), notes)
		self.assertIsNone(land_map._row_field({}, "notes"))
		self.assertIsNone(land_map._row_field(Bench(None), "notes"))

	def test_empty_prose_is_no_points_and_no_exception(self):
		for notes in (None, "", "   ", "no coordinates here at all"):
			with self.subTest(notes=notes):
				self.assertEqual(land_map._gps_points(notes), [])


# ── 7 ───────────────────────────────────────────────────────────────────────
class TheAdjustmentsOwnMap(LandMapTestCase):
	"""What the Lot Line Adjustment form draws, read from the record itself."""

	def test_it_carries_both_lots_with_their_geometry(self):
		lot = self.a_lot()
		name = self.an_adjustment(lot_1=lot)
		answer = land_map._adjustment_map(name)
		sides = {shape["side"]: shape for shape in answer["lots"]}
		self.assertEqual(sides["Lot 1"]["label"], LOT)
		self.assertEqual(sides["Lot 1"]["geometry"]["type"], "Polygon")
		self.assertEqual(sides["Lot 1"]["acres"], 40.0)

	def test_a_lot_that_is_not_linked_is_simply_absent(self):
		name = self.an_adjustment(lot_1=self.a_lot())
		self.assertEqual([shape["side"] for shape in land_map._adjustment_map(name)["lots"]], ["Lot 1"])

	def test_the_stored_corridors_come_back_labelled(self):
		name = self.an_adjustment(lot_1=self.a_lot())
		frappe.db.set_value(
			land.LOT_LINE_ADJUSTMENT,
			name,
			"easement_geometry",
			json.dumps(
				{
					"type": "FeatureCollection",
					"features": [
						{
							"type": "Feature",
							"properties": {"label": "Dry Hollow ditch"},
							"geometry": {"type": "LineString", "coordinates": [[WEST, SOUTH], [EAST, SOUTH]]},
						}
					],
				}
			),
		)
		corridors = land_map._adjustment_map(name)["easements"]
		self.assertEqual([corridor["label"] for corridor in corridors], ["Dry Hollow ditch"])

	def test_a_gps_fix_in_the_notes_becomes_a_point(self):
		name = self.an_adjustment(lot_1=self.a_lot())
		frappe.db.set_value(
			land.LOT_LINE_ADJUSTMENT, name, "notes", "Well at GPS 45.577088\u00b0N, 121.1940661\u00b0W."
		)
		points = land_map._adjustment_map(name)["points"]
		self.assertEqual(points[0]["lat"], 45.577088)
		self.assertEqual(points[0]["lon"], -121.1940661)

	def test_a_gps_fix_on_an_easement_ROW_becomes_a_point(self):
		"""THE FIX IS USUALLY TYPED ON THE ROW, NOT THE HEADER.

		The easement child rows are read through the parent document. Filtering
		the child doctype by `parent` answers on a bench and answers nothing at
		all here, which is how a marker that never appears ships green.
		"""
		name = self.an_adjustment(lot_1=self.a_lot())
		self.configure(enabled=1, allow_lla_add_easement=1, **self.SWITCHES)
		self.tool_data(
			"lla_add_easement",
			{
				"name": name,
				"easement_type": "Access",
				"burdened": "Party 1",
				"benefited": "Party 2",
				"notes": "well at GPS 45.577088\u00b0N, 121.1940661\u00b0W, 20-ft radius",
			},
		)
		points = land_map._adjustment_map(name)["points"]
		self.assertEqual([point["lat"] for point in points], [45.577088])
		self.assertEqual(points[0]["lon"], -121.1940661)
		self.assertEqual(points[0]["label"], "Access")

	def test_a_record_with_nothing_mapped_answers_empty_rather_than_raising(self):
		answer = land_map._adjustment_map(self.an_adjustment())
		self.assertEqual(answer["easements"], [])
		self.assertEqual(answer["points"], [])
		self.assertIsNone(answer["proposed_geometry"])

	def test_it_names_the_record_and_carries_the_disclaimer(self):
		name = self.an_adjustment()
		answer = land_map._adjustment_map(name)
		self.assertEqual(answer["name"], name)
		self.assertEqual(answer["route"], f"/app/lot-line-adjustment/{name}")
		self.assertEqual(answer["disclaimer"], surveying.DISCLAIMER)

	def test_asking_for_nothing_is_refused_by_name(self):
		with self.assertRaises(ToolError) as caught:
			land_map._adjustment_map("")
		self.assertIn("name is required", str(caught.exception))

	def test_asking_for_a_record_that_is_not_there_is_refused_by_name(self):
		"""Past the permission check, a missing row is named rather than
		answered with an empty map that looks like a record with no ground."""
		with self.assertRaises(ToolError) as caught:
			land_map._adjustment_map("LLA-9999-9999")
		self.assertIn("LLA-9999-9999", str(caught.exception))

	def test_a_login_without_read_permission_is_refused(self):
		"""The gate is Frappe's own `read` on THIS record, so a User Permission
		that scopes somebody to one company is enforced per document."""
		name = self.an_adjustment()
		# The double allows by default, so a role change alone proves nothing
		# here: this is the lever that actually makes it say no.
		STORE.denied_permissions.add((land.LOT_LINE_ADJUSTMENT, name, "read"))
		with self.assertRaises(Exception):
			land_map._adjustment_map(name)


# ── 5 ───────────────────────────────────────────────────────────────────────
HARNESS = r"""// Run the real page script under a stubbed Desk. Prints JSON on stdout.
const fs = require("fs");
const vm = require("vm");

const SCRIPT = process.argv[2];
const ANSWER = JSON.parse(process.argv[3]);
const PREVIEW = JSON.parse(process.argv[4]);

const calls = { methods: [], args: [], drawn: [], rows: 0, layers: 0, indicators: [], markers: [] };

function node(selector) {
	const self = {
		selector: selector,
		handlers: {},
		value: "",
		html_content: "",
		text_content: "",
		children: [],
		find: function (child) { return node(child); },
		on: function (event, fn) { self.handlers[event] = fn.bind(self); registry[selector] = self; return self; },
		text: function (value) { if (value === undefined) return self.text_content; self.text_content = value; return self; },
		html: function (value) {
			if (value === undefined) return self.html_content;
			self.html_content = value;
			if (selector === ".lm-course-rows") { calls.rows = (String(value).match(/<tr>/g) || []).length; }
			return self;
		},
		val: function (value) { if (value === undefined) return self.value; self.value = value; return self; },
		appendTo: function () { return self; },
		toggle: function () { return self; },
		show: function () { return self; },
		hide: function () { return self; },
		empty: function () { self.html_content = ""; return self; },
		click: function () { if (self.handlers.click) self.handlers.click(); },
		0: { id: "map-canvas" },
	};
	return self;
}

const registry = {};
const root = node(".lm-wrap");
root.find = function (selector) {
	if (!registry[selector]) { registry[selector] = node(selector); }
	return registry[selector];
};

// `$(this)` inside a change handler must answer the element that changed, not
// the page root — otherwise the adjustment picker reads the wrong value and no
// harness can drive it.
global.$ = function (thing) { return thing && thing.selector ? thing : root; };
global.__ = function (text, args) {
	let out = String(text);
	(args || []).forEach((value, index) => { out = out.replace("{" + index + "}", value); });
	return out;
};

const latlng = (lat, lng) => ({ lat: lat, lng: lng });
const layer_group = () => {
	const group = {
		layers: [],
		addTo: function () { calls.layers += 1; return group; },
		addLayer: function (l) { group.layers.push(l); return group; },
		clearLayers: function () { group.layers = []; return group; },
	};
	return group;
};

const L = {
	map: function () {
		const map = {
			on: function (event, fn) { map._created = fn; return map; },
			fitBounds: function () { return map; },
			setView: function () { return map; },
			remove: function () {},
		};
		L._map = map;
		return map;
	},
	featureGroup: layer_group,
	geoJSON: function (geometry) {
		const g = { bindPopup: function () { return g; }, addTo: function () { return g; }, geometry: geometry };
		return g;
	},
	latLng: latlng,
	latLngBounds: function (a, b) { return [a, b]; },
	marker: function (position) {
		const m = { position: position, bindPopup: function () { return m; }, addTo: function () { return m; } };
		calls.markers.push(position);
		return m;
	},
	Draw: {
		Event: { CREATED: "draw:created" },
		Polyline: function () { this.enable = function () { calls.drawn.push("line"); }; this.disable = function () {}; },
		Polygon: function () { this.enable = function () { calls.drawn.push("area"); }; this.disable = function () {}; },
	},
};

global.window = global;
global.document = { createElement: () => ({}), head: { appendChild: () => {} } };
global.erpnext_mcp = {
	geo_map: {
		load_leaflet: () => Promise.resolve(L),
		load_draw: () => Promise.resolve(L),
		add_base_layers: () => ({}),
		MAX_FIT_ZOOM: 18,
		HOME_VIEW: { centre: [45.6, -121.18], zoom: 12 },
	},
};
global.frappe = {
	// Frappe creates the page object before it evaluates the script; a harness
	// that did not would fail on the first line and look like a broken page.
	pages: { "land-map": {} },
	route_options: {},
	ui: {
		make_app_page: () => ({ main: {}, set_indicator: (text) => calls.indicators.push(text) }),
		Dialog: function (options) { this.show = () => { frappe._dialog = options; }; this.hide = () => {}; },
	},
	render_template: () => "<div></div>",
	utils: { escape_html: (value) => String(value) },
	format: (value) => String(value),
	datetime: { now_datetime: () => "2026-09-17 09:00:00" },
	msgprint: (text) => calls.methods.push("msgprint:" + text),
	prompt: (fields, callback) => callback({ label: "Dry Hollow ditch" }),
	show_alert: () => {},
	set_route: () => {},
	call: function (options) {
		calls.methods.push(options.method);
		calls.args.push(options.args);
		const answer = options.method.endsWith("survey_preview") ? PREVIEW : ANSWER;
		return Promise.resolve({ message: answer });
	},
};

vm.runInThisContext(fs.readFileSync(SCRIPT, "utf8"), { filename: SCRIPT });

const wrapper = {};
frappe.pages["land-map"].on_page_load(wrapper);

// Let the load settle, then drive the buttons the way a person does.
Promise.resolve()
	.then(() => new Promise((resolve) => setTimeout(resolve, 0)))
	.then(() => new Promise((resolve) => setTimeout(resolve, 0)))
	.then(() => {
		// An easement corridor first: it must not become the proposal.
		root.find(".lm-draw-easement").click();
		if (L._map && L._map._created) {
			L._map._created({
				layer: {
					toGeoJSON: () => ({
						type: "Feature",
						properties: {},
						geometry: { type: "LineString", coordinates: [[-121.164, 45.5808], [-121.161, 45.5808]] },
					}),
				},
			});
		}
		root.find(".lm-draw-line").click();
		if (L._map && L._map._created) {
			L._map._created({
				layer: { toGeoJSON: () => ({ geometry: { type: "Polygon", coordinates: [PREVIEW.points] } }) },
			});
		}
		root.find(".lm-compute").click();
		return new Promise((resolve) => setTimeout(resolve, 0));
	})
	.then(() => new Promise((resolve) => setTimeout(resolve, 0)))
	.then(() => {
		root.find(".lm-save").click();
		if (frappe._dialog) {
			frappe._dialog.primary_action({ adjustment: "LLA-2026-0001", title: "" });
		}
		return new Promise((resolve) => setTimeout(resolve, 0));
	})
	.then(() => {
		calls.legal = root.find(".lm-legal").val();
		calls.summary = root.find(".lm-summary").text();
		calls.flags = root.find(".lm-flags").html();
		console.log(JSON.stringify(calls));
	})
	.catch((error) => {
		console.log(JSON.stringify({ error: String((error && error.stack) || error) }));
	});
"""


def page_fixtures() -> tuple:
	"""The map answer and the survey preview the page harness is driven with.

	A function rather than a block inside one test class: `test_land_export` runs
	the same page with the same fixtures to click the export buttons, and two
	copies of a fixture is two pages being tested.
	"""
	answer = {
		"company": MAIN,
		"companies": [MAIN],
		"bounds": [[SOUTH, WEST], [NORTH, EAST]],
		"disclaimer": surveying.DISCLAIMER,
		"may_write_adjustment": True,
		"layers": [
			{
				"doctype": "Parcel",
				"label": "Parcels",
				"colour": "#2490ef",
				"shapes": [
					{
						"name": "Mill Creek",
						"label": "Mill Creek",
						"route": "/app/parcel/Mill%20Creek",
						"acres": 40.0,
						"detail": "Wasco",
						"geometry": box(WEST, SOUTH, EAST, NORTH),
						"centre": [WEST, SOUTH],
					}
				],
			}
		],
		"tax_lots": {
			"doctype": "County Tax Lot",
			"label": "County Tax Lots",
			"colour": "#b8860b",
			"shapes": [
				{
					"name": LOT,
					"label": LOT,
					"route": "/app/county-tax-lot/x",
					"acres": 40.0,
					"detail": "Account 7503",
					"geometry": box(WEST, SOUTH, EAST, NORTH),
					"centre": [WEST, SOUTH],
				}
			],
		},
		"adjustments": [
			{
				"name": "LLA-2026-0001",
				"title": "Mill Creek",
				"status": "Draft",
				"docstatus": 0,
				"lots": [LOT],
				"route": "/app/lot-line-adjustment/LLA-2026-0001",
				"proposed_geometry": None,
				"has_description": False,
				#: A corridor agreed long before this visit, and the well it
				#: reaches. Both are on the RECORD, which is the case the page
				#: used to be blind to — and blind in a way that deleted them.
				"easements": [
					{
						"adjustment": "LLA-2026-0001",
						"label": STORED_CORRIDOR,
						"easement_type": "Access",
						"burdened": "Party 1",
						"benefited": "Party 2",
						"geometry": {
							"type": "LineString",
							"coordinates": [[WEST, SOUTH], [EAST, SOUTH]],
						},
					}
				],
				"points": [{"lat": WELL[0], "lon": WELL[1], "label": "Well"}],
			}
		],
	}
	description = surveying.legal_description(DRAWN, "Beginning at the southwest corner of " + LOT)
	preview = {
		"points": DRAWN,
		"geometry": surveying.polygon(DRAWN),
		"courses": description["courses"],
		"acres": description["acres"],
		"closure": description["closure"],
		"tie_in": {"found": True, "description": "Beginning at the southwest corner of " + LOT},
		"legal_description": description["text"],
		"disclaimer": surveying.DISCLAIMER,
		"easement_crossings": [
			{"label": "Ditch", "note": "the proposed line crosses this easement corridor"}
		],
		"easements_considered": 1,
		"affected": [
			{
				"label": LOT,
				"acres_before": 40.0,
				"overlap_acres": 2.5,
				"acres_if_giving": 37.5,
				"acres_if_receiving": 42.5,
				"note": "",
			}
		],
		"sides": [],
	}
	return answer, preview


# ── 8 ───────────────────────────────────────────────────────────────────────
FORM_HARNESS = r"""// Run the real Lot Line Adjustment form script under a stubbed Desk.
const fs = require("fs");
const vm = require("vm");
const SCRIPT = process.argv[2];
const ANSWER = JSON.parse(process.argv[3]);

const out = { handlers: [], spec: null, comments: [], buttons: [], methods: [], error: null };

global.window = global;
global.__ = function (text, args) {
	let s = String(text);
	(args || []).forEach((v, i) => { s = s.replace("{" + i + "}", v); });
	return s;
};
global.erpnext_mcp = { geo_map: { render: (frm, spec) => { out.spec = spec; } } };
global.frappe = {
	ui: { form: { on: (doctype, handlers) => { out.handlers.push(doctype); global.__refresh = handlers.refresh; } } },
	call: (options) => { out.methods.push(options.method); out.args = options.args; return Promise.resolve({ message: ANSWER }); },
	set_route: () => {},
	route_options: {},
	utils: { escape_html: (v) => String(v) },
};

vm.runInThisContext(fs.readFileSync(SCRIPT, "utf8"), { filename: SCRIPT });

const frm = {
	doc: { name: "LLA-2026-0002", proposed_geometry: null, generated_legal_description: null },
	is_new: () => false,
	add_custom_button: (label) => { out.buttons.push(label); },
	dashboard: { add_comment: (text, colour) => out.comments.push({ text: text, colour: colour }) },
};

Promise.resolve()
	.then(() => global.__refresh(frm))
	.then(() => new Promise((r) => setTimeout(r, 0)))
	.then(() => new Promise((r) => setTimeout(r, 0)))
	.then(() => console.log(JSON.stringify(out)))
	.catch((e) => { out.error = String((e && e.stack) || e); console.log(JSON.stringify(out)); });
"""


def form_answer() -> dict:
	"""LLA-2026-0002 as the server describes it: one lot mapped, one not.

	The unmapped second lot is the realistic half. A tax lot is only on the map
	once somebody has run a county lookup for it, and a form that drew one lot
	and said nothing would look like a complete picture of one side.
	"""
	return {
		"name": "LLA-2026-0002",
		"title": "Well Access Easement",
		"route": "/app/lot-line-adjustment/LLA-2026-0002",
		"lots": [
			{
				"side": "Lot 1",
				"name": "1N 13E 9 2700",
				"label": "1N 13E 9 2700",
				"owner": "HIGHLAND LTD LIABILITY CO",
				"acres": 65.6,
				"geometry": box(WEST, SOUTH, EAST, NORTH),
				"unreadable": "",
			},
			{
				"side": "Lot 2",
				"name": "1N 13E 9 2500",
				"label": "1N 13E 9 2500",
				"owner": "POLEHN DONELLA TRUSTEE",
				"acres": 39.67,
				"geometry": None,
				"unreadable": "",
			},
		],
		"easements": [
			{
				"adjustment": "LLA-2026-0002",
				"label": "Well access",
				"easement_type": "Access",
				"burdened": "Party 1",
				"benefited": "Party 2",
				"geometry": {"type": "LineString", "coordinates": [[WEST, SOUTH], [EAST, SOUTH]]},
			}
		],
		"proposed_geometry": None,
		"proposed_unreadable": "",
		"points": [{"lat": WELL[0], "lon": WELL[1], "label": "Access"}],
		"disclaimer": surveying.DISCLAIMER,
	}


@unittest.skipUnless(shutil.which("node"), "needs node to execute the form script")
class TheFormRuns(unittest.TestCase):
	"""THE FORM SCRIPT, EXECUTED.

	Every other assertion about this file is a substring match, and a substring
	matches the whole file whether or not the line it is on ever runs. This
	opens the record and reads what the script actually handed the widget.
	"""

	report: ClassVar[dict] = {}

	@classmethod
	def setUpClass(cls):
		with tempfile.TemporaryDirectory() as folder:
			harness = Path(folder) / "form_harness.js"
			harness.write_text(FORM_HARNESS)
			result = subprocess.run(
				[
					"node",
					str(harness),
					str(REPO / "erpnext_mcp" / "public" / "js" / "lot_line_adjustment_map.js"),
					json.dumps(form_answer()),
				],
				capture_output=True,
				text=True,
				timeout=60,
			)
		if result.returncode != 0:
			raise AssertionError(f"the form script would not run: {result.stderr[-2000:]}")
		cls.report = json.loads(result.stdout.strip().splitlines()[-1])

	def test_it_ran_at_all(self):
		self.assertIsNone(self.report["error"])
		self.assertEqual(self.report["handlers"], ["Lot Line Adjustment"])

	def test_it_asks_the_server_for_this_records_map(self):
		self.assertEqual(self.report["methods"], ["erpnext_mcp.api.land_map.adjustment_map"])
		self.assertEqual(self.report["args"]["name"], "LLA-2026-0002")

	def test_the_mapped_lot_is_drawn_and_named(self):
		labels = [shape["label"] for shape in self.report["spec"]["geometries"]]
		self.assertTrue(any("1N 13E 9 2700" in label for label in labels))
		self.assertTrue(any("HIGHLAND" in label for label in labels))

	def test_the_corridor_is_drawn_and_says_who_it_burdens(self):
		corridor = next(
			shape for shape in self.report["spec"]["geometries"] if "Well access" in shape["label"]
		)
		self.assertEqual(corridor["geometry"]["type"], "LineString")
		self.assertIn("burdens Party 1", corridor["label"])
		self.assertIn("benefits Party 2", corridor["label"])

	def test_the_corridor_is_drawn_more_heavily_than_the_ground_it_crosses(self):
		"""It is the smallest shape on the map and the reason the map is open."""
		shapes = {shape["label"]: shape for shape in self.report["spec"]["geometries"]}
		corridor = next(shape for label, shape in shapes.items() if "Well access" in label)
		lot = next(shape for label, shape in shapes.items() if "1N 13E 9 2700" in label)
		self.assertGreater(corridor["fill_opacity"], lot["fill_opacity"])

	def test_the_well_is_handed_to_the_widget_as_a_point(self):
		self.assertEqual(self.report["spec"]["points"], [{"lat": WELL[0], "lon": WELL[1], "label": "Access"}])

	def test_a_lot_with_no_cached_boundary_is_said_out_loud_in_red(self):
		"""The failure that is invisible on a map: the other lot draws, the view
		fits to it, and one side of the agreement is silently missing."""
		red = [comment for comment in self.report["comments"] if comment["colour"] == "red"]
		self.assertTrue(red, self.report["comments"])
		self.assertIn("1N 13E 9 2500", red[0]["text"])
		self.assertIn("no cached boundary", red[0]["text"])

	def test_it_counts_what_it_drew(self):
		summary = " ".join(comment["text"] for comment in self.report["comments"])
		self.assertIn("1 access corridor(s) mapped", summary)
		self.assertIn("1 of 2 lots drawn", summary)

	def test_it_offers_both_doors_to_the_page_that_traces(self):
		self.assertIn("Land Map", self.report["buttons"])
		self.assertIn("Draw an access corridor", self.report["buttons"])

	def test_it_hands_the_widget_nothing_it_was_not_given(self):
		"""A read-only map: no editable block, so no drawing surface and no
		second save path competing with the land map's."""
		self.assertNotIn("editable", self.report["spec"])


@unittest.skipUnless(shutil.which("node"), "needs node to execute the page script")
class ThePageRuns(unittest.TestCase):
	"""THE PAGE, EXECUTED. A substring assertion matches the whole file; this
	drives the buttons and reads what the script actually did."""

	report: ClassVar[dict] = {}

	@classmethod
	def setUpClass(cls):
		answer, preview = page_fixtures()
		# CHOOSE THE ADJUSTMENT FROM THE PICKER, which is the door this class is
		# about: the page opened from the sidebar with nothing attached and a
		# record is selected now. The base harness stays unattached because
		# `test_land_export` drives it too, and its subject is what an export
		# does when nothing has been chosen at all.
		script = HARNESS.replace(
			'		root.find(".lm-draw-easement").click();',
			"""		const picker = root.find(".lm-adjustment");
		picker.value = "LLA-2026-0001";
		picker.handlers.change();
		calls.seeded_on_pick = true;
		root.find(".lm-draw-easement").click();""",
		)
		assert "seeded_on_pick" in script, "the picker patch matched nothing — re-anchor it"

		with tempfile.TemporaryDirectory() as folder:
			harness = Path(folder) / "harness.js"
			harness.write_text(script)
			result = subprocess.run(
				[
					"node",
					str(harness),
					str(PAGE_DIR / "land_map.js"),
					json.dumps(answer),
					json.dumps(preview),
				],
				capture_output=True,
				text=True,
				timeout=60,
			)
		if result.returncode != 0:
			raise AssertionError(f"the page script would not run: {result.stderr[-2000:]}")
		cls.report = json.loads(result.stdout.strip().splitlines()[-1])

	def test_it_ran_at_all(self):
		self.assertNotIn("error", self.report, self.report.get("error", ""))

	def test_loading_the_page_asks_the_server_for_the_map(self):
		self.assertEqual(self.report["methods"][0], "erpnext_mcp.api.land_map.land_map")

	def test_every_layer_is_drawn(self):
		"""One feature group per layer, plus the drawing layer."""
		self.assertGreaterEqual(self.report["layers"], 3)

	def test_the_draw_button_starts_a_polyline(self):
		self.assertIn("line", self.report["drawn"])

	def test_compute_posts_the_drawn_points(self):
		index = self.report["methods"].index("erpnext_mcp.api.land_map.survey_preview")
		self.assertEqual(json.loads(self.report["args"][index]["points"]), DRAWN)

	def test_every_course_that_comes_back_is_a_row_on_screen(self):
		self.assertEqual(self.report["rows"], 4)

	def test_the_description_lands_in_the_box(self):
		self.assertIn("thence", self.report["legal"])
		self.assertTrue(self.report["legal"].endswith(surveying.DISCLAIMER))

	def test_the_summary_states_the_acreage_and_the_closure(self):
		self.assertIn("acres", self.report["summary"])
		self.assertIn("closure", self.report["summary"])

	def test_a_crossed_easement_is_shown_as_a_warning(self):
		self.assertIn("lm-warn", self.report["flags"])
		self.assertIn("Ditch", self.report["flags"])

	def test_a_drawn_easement_is_sent_with_the_measure_request(self):
		index = self.report["methods"].index("erpnext_mcp.api.land_map.survey_preview")
		corridors = json.loads(self.report["args"][index]["easements"])
		self.assertEqual(corridors["type"], "FeatureCollection")
		self.assertIn(
			"Dry Hollow ditch",
			[feature["properties"]["label"] for feature in corridors["features"]],
		)

	def test_a_drawn_easement_does_not_become_the_proposal(self):
		"""The corridor and the proposed line are two different drawings, and a
		page that confused them would save a ditch as somebody's new lot line."""
		index = self.report["methods"].index("erpnext_mcp.api.land_map.survey_preview")
		self.assertEqual(json.loads(self.report["args"][index]["points"]), DRAWN)

	# ── v0.174.0: the corridors already on the record ───────────────────────
	def test_a_corridor_on_the_record_is_drawn_when_the_page_opens(self):
		"""A strip of ground somebody agreed to last year is the main thing the
		person drawing this year's line needs to see."""
		self.assertIn(STORED_CORRIDOR, json.dumps(self.report["args"]))

	def test_a_gps_fix_in_the_notes_becomes_a_marker(self):
		self.assertIn([WELL[0], WELL[1]], self.report["markers"])

	def test_saving_keeps_the_corridors_that_were_already_there(self):
		"""THE REGRESSION THIS RELEASE EXISTS FOR.

		`save` writes the whole `easement_geometry` column from the page's list.
		The list used to hold only what was traced in the current visit, so
		opening a record with a corridor on it, tracing a second, and pressing
		Save wrote a column containing the second alone — the first was deleted,
		under a green toast, with nothing on screen to say so.
		"""
		index = self.report["methods"].index("erpnext_mcp.api.land_map.save_proposal")
		saved = json.loads(self.report["args"][index]["easement_geometry"])
		labels = [feature["properties"]["label"] for feature in saved["features"]]
		self.assertIn(STORED_CORRIDOR, labels, "the corridor already on the record was dropped")
		self.assertIn("Dry Hollow ditch", labels, "the corridor just traced was dropped")

	def test_the_measure_request_carries_both_corridors(self):
		index = self.report["methods"].index("erpnext_mcp.api.land_map.survey_preview")
		corridors = json.loads(self.report["args"][index]["easements"])
		labels = [feature["properties"]["label"] for feature in corridors["features"]]
		self.assertIn(STORED_CORRIDOR, labels)
		self.assertIn("Dry Hollow ditch", labels)

	def test_a_stored_corridor_does_not_become_the_proposal(self):
		"""Seeding puts corridors on the map. It must not put them in the line."""
		index = self.report["methods"].index("erpnext_mcp.api.land_map.save_proposal")
		proposed = json.loads(self.report["args"][index]["proposed_geometry"])
		self.assertEqual(proposed["type"], "Polygon")
		self.assertNotIn(STORED_CORRIDOR, json.dumps(proposed))

	def test_saving_posts_the_geometry_to_the_save_method(self):
		index = self.report["methods"].index("erpnext_mcp.api.land_map.save_proposal")
		arguments = self.report["args"][index]
		self.assertEqual(arguments["adjustment"], "LLA-2026-0001")
		self.assertEqual(json.loads(arguments["proposed_geometry"])["type"], "Polygon")
		self.assertIn("thence", arguments["legal_description"])
		self.assertIn(
			"Dry Hollow ditch",
			[
				feature["properties"]["label"]
				for feature in json.loads(arguments["easement_geometry"])["features"]
			],
		)

# SPDX-License-Identifier: MIT
"""The whole-farm map — `/app/farm-overview` and what stands behind it.

v0.110.0. The question this page answers had been asked out loud: "is there a
place we can go to see the Fields and zones for the whole farm?" There was not.
Boundaries have been stored since v0.12.0 and drawn since v0.32.0, one record at
a time, on the form of the record — so the mistakes that only show up BETWEEN
records have never once been visible.

The whole risk in adding it is the one `test_mobile_onboarding.py` names for the
enrolment page: that a Desk surface quietly becomes a SECOND IMPLEMENTATION with
a weaker set of gates than the tools it wraps, or a second reader of columns four
register tools already own. Every claim below is about that, or about a number on
the page being one somebody can act on.

SIX CLAIMS.

1. `TheGeometryParser` — pure, over a string, with no shapely anywhere near it.
   It takes the three shapes the boundary tools take, and a row it cannot read
   comes back with a REASON rather than being dropped. That is the claim that
   matters most: a boundary which silently does not draw looks exactly like a
   block that was never traced, and the two are opposite problems.
2. `TheBoundingBox` — longitude first on the way in, latitude first on the way
   out, and coordinates that are not on Earth do not get to decide where the map
   opens. Null island is refused, because an unset Float pair is [0, 0] and a map
   that flies to the Gulf of Guinea looks exactly like a map that works.
3. `TheAnswer` — four registers in one call, the polygon `list_parcels`
   deliberately withholds fetched for the parcels the register already returned,
   structures as points, and every count reported as a gap somebody can close.
4. `TheGate` — a register this login may not read is NAMED and contributes
   nothing, rather than taking the page down; an entity it may not read is
   refused by name rather than quietly swapped for one it may.
5. `ItWritesNothing` — the negative control. Reading the map does not change a
   single stored boundary, and the module exposes no way to.
6. `ThePageOnDisk` — the Page record, the script and the template agree with each
   other and with the module, and the method the script calls exists and is
   whitelisted.
"""

import json
import pathlib
import unittest

import frappe

from erpnext_mcp import asset_types, farm_overview, farm_task_map_action

from .fixtures import MAIN, OTHER, V12TestCase
from .harness import INSTALLED_DOCTYPES, STORE

PAGE_DIR = (
	pathlib.Path(__file__).resolve().parent.parent / "erpnext_mcp" / "erpnext_mcp" / "page" / "farm_overview"
)

#: A rectangle on Dry Hollow Road, the same ground `test_geo.py` uses. Small
#: enough to be a real block, which is what makes the bounding box a number that
#: can be checked by hand rather than a shape that fills a continent.
BLOCK = {
	"type": "Polygon",
	"coordinates": [
		[
			[-121.1800, 45.6000],
			[-121.1760, 45.6000],
			[-121.1760, 45.6030],
			[-121.1800, 45.6030],
			[-121.1800, 45.6000],
		]
	],
}

ZONE = {
	"type": "Polygon",
	"coordinates": [
		[
			[-121.1790, 45.6010],
			[-121.1770, 45.6010],
			[-121.1770, 45.6020],
			[-121.1790, 45.6020],
			[-121.1790, 45.6010],
		]
	],
}

ON = {
	"allow_create_parcel": 1,
	"allow_create_field": 1,
	"allow_create_irrigation_zone": 1,
	"allow_create_housing_unit": 1,
}


class OverviewTestCase(V12TestCase):
	"""A farm with a parcel, two blocks, a zone and two cabins on it.

	THE BOUNDARIES ARE WRITTEN TO THE COLUMN RATHER THAN SET THROUGH THE TOOLS,
	and that is deliberate rather than a shortcut. `set_field_boundary` needs
	shapely, and this page is built NOT to — the whole argument in
	`farm_overview.py` is that a bench which cannot compute an area can still draw
	the shapes it already has. A fixture that went through the tools would have
	skipped every test here on the CI run that proves it.
	"""

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)

	def a_farm(self):
		parcel = self.tool_data(
			"create_parcel",
			{
				"owning_entity": MAIN,
				"parcel_name": "Mill Creek",
				"acreage": 131.43,
				"county": "Wasco",
				"state": "OR",
			},
		)["name"]
		mapped = self.tool_data(
			"create_field",
			{
				"parcel": parcel,
				"field_name": "Yellow Camp Block 3",
				"acreage": 25.7,
				"crop": "Cherry",
				"variety": "Skeena",
			},
		)["name"]
		untraced = self.tool_data(
			"create_field",
			{"parcel": parcel, "field_name": "Ridge Top", "acreage": 12.5, "crop": "Cherry"},
		)["name"]
		zone = self.tool_data(
			"create_irrigation_zone",
			{"field": mapped, "zone_name": "YC3-Zone2", "area_sq_ft": 110000},
		)["name"]
		located = self.tool_data(
			"create_housing_unit",
			{"parcel": parcel, "unit_name": "Cabin 1", "unit_type": "Cabin", "capacity": 4},
		)["name"]
		nowhere = self.tool_data(
			"create_housing_unit",
			{"parcel": parcel, "unit_name": "Shop", "unit_type": "Shop"},
		)["name"]

		frappe.db.set_value("Parcel", parcel, "boundary_geojson", json.dumps(BLOCK))
		frappe.db.set_value("Field", mapped, "boundary_geojson", json.dumps(BLOCK))
		frappe.db.set_value("Irrigation Zone", zone, "boundary_geojson", json.dumps(ZONE))
		frappe.db.set_value("Housing Unit", located, "gps_latitude", 45.6015)
		frappe.db.set_value("Housing Unit", located, "gps_longitude", -121.1780)

		return {
			"parcel": parcel,
			"mapped": mapped,
			"untraced": untraced,
			"zone": zone,
			"located": located,
			"nowhere": nowhere,
		}

	def deny(self, doctype, ptype="read"):
		STORE.denied_permissions.add((doctype, ptype))
		self.addCleanup(STORE.denied_permissions.discard, (doctype, ptype))

	def layer(self, answer, doctype):
		return next(entry for entry in answer["layers"] if entry["doctype"] == doctype)


# ── 1 ─────────────────────────────────────────────────────────────────────────
class TheGeometryParser(unittest.TestCase):
	"""No site, no session, no shapely — which is the point of it being pure."""

	def test_a_bare_geometry_comes_back_as_itself(self):
		geometry, reason = farm_overview.parse_geometry(json.dumps(BLOCK))
		self.assertEqual(geometry["type"], "Polygon")
		self.assertIsNone(reason)

	def test_a_feature_is_unwrapped_to_its_geometry(self):
		"""A QGIS export button produces one of three shapes and this app stores
		whichever it was handed. The boundary tools take all three."""
		wrapped = json.dumps({"type": "Feature", "properties": {}, "geometry": BLOCK})
		geometry, reason = farm_overview.parse_geometry(wrapped)
		self.assertEqual(geometry, BLOCK)
		self.assertIsNone(reason)

	def test_a_single_feature_collection_is_unwrapped_too(self):
		wrapped = json.dumps(
			{"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": BLOCK}]}
		)
		geometry, _ = farm_overview.parse_geometry(wrapped)
		self.assertEqual(geometry, BLOCK)

	def test_a_multi_feature_collection_is_handed_over_whole(self):
		"""Picking one of several features would draw half a parcel and say
		nothing about the other half. Leaflet draws a collection perfectly well."""
		collection = {
			"type": "FeatureCollection",
			"features": [
				{"type": "Feature", "geometry": BLOCK},
				{"type": "Feature", "geometry": ZONE},
			],
		}
		geometry, reason = farm_overview.parse_geometry(json.dumps(collection))
		self.assertEqual(geometry["type"], "FeatureCollection")
		self.assertIsNone(reason)
		# And both features still count towards where the map opens.
		self.assertEqual(len(farm_overview.points_of(geometry)), 10)

	def test_an_empty_field_is_not_a_broken_one(self):
		"""An untraced block and a corrupted one are opposite problems and the
		page says different things about them."""
		for empty in ("", "   ", None):
			with self.subTest(value=empty):
				self.assertEqual(farm_overview.parse_geometry(empty), (None, None))

	def test_text_that_is_not_json_comes_back_with_a_reason(self):
		geometry, reason = farm_overview.parse_geometry("{oops")
		self.assertIsNone(geometry)
		self.assertIn("not JSON", reason)

	def test_json_that_is_not_a_geojson_object_says_which_it_is(self):
		geometry, reason = farm_overview.parse_geometry("[1, 2, 3]")
		self.assertIsNone(geometry)
		self.assertIn("list", reason)

	def test_a_feature_with_no_geometry_is_named_rather_than_drawn(self):
		wrapped = json.dumps({"type": "Feature", "properties": {"name": "Block 3"}})
		geometry, reason = farm_overview.parse_geometry(wrapped)
		self.assertIsNone(geometry)
		self.assertIn("no geometry", reason)

	def test_a_geometry_with_no_coordinates_is_named(self):
		geometry, reason = farm_overview.parse_geometry(json.dumps({"type": "Polygon"}))
		self.assertIsNone(geometry)
		self.assertIn("no coordinates", reason)


# ── 2 ─────────────────────────────────────────────────────────────────────────
class TheBoundingBox(unittest.TestCase):
	def test_geojson_is_longitude_first_and_the_box_is_latitude_first(self):
		"""The mistake this test exists to stop does not raise. Read the pair the
		wrong way round and the farm gets a bounding box off the coast of Somalia,
		which is a map of somewhere else rather than an error."""
		box = farm_overview.bounds_of(farm_overview.points_of(BLOCK))
		self.assertEqual(box, [[45.6, -121.18], [45.603, -121.176]])

	def test_a_point_a_line_and_a_polygon_all_bound(self):
		"""GeoJSON nests a Point one deep, a LineString two and a Polygon three,
		and this page has to bound whichever it was handed."""
		point = {"type": "Point", "coordinates": [-121.18, 45.6]}
		line = {"type": "LineString", "coordinates": [[-121.18, 45.6], [-121.176, 45.603]]}
		for geometry in (point, line, BLOCK):
			with self.subTest(kind=geometry["type"]):
				self.assertIsNotNone(farm_overview.bounds_of(farm_overview.points_of(geometry)))

	def test_a_geometry_collection_is_walked(self):
		collection = {"type": "GeometryCollection", "geometries": [BLOCK, ZONE]}
		self.assertEqual(len(farm_overview.points_of(collection)), 10)

	def test_nothing_positioned_is_no_box_rather_than_a_box_of_nought(self):
		"""None is what tells the page to open on HOME_VIEW. A [[0,0],[0,0]] box
		would fit the map to null island and look like it worked."""
		self.assertIsNone(farm_overview.bounds_of([]))
		self.assertIsNone(farm_overview.bounds_of([(0, 0), (0.0, 0.0)]))

	def test_a_coordinate_off_earth_does_not_decide_where_the_map_opens(self):
		"""One vertex typed with an extra digit would otherwise stretch the frame
		across a continent and draw every real boundary as a dot. The shape is
		still on the map — it is the record, and the record is what somebody has
		to go and fix — but it does not get a vote on the view."""
		points = [*farm_overview.points_of(BLOCK), (450.0, -121.18)]
		self.assertEqual(farm_overview.bounds_of(points), [[45.6, -121.18], [45.603, -121.176]])


# ── 3 ─────────────────────────────────────────────────────────────────────────
class TheAnswer(OverviewTestCase):
	def test_one_call_carries_all_four_registers(self):
		self.a_farm()
		answer = farm_overview.farm_overview(company=MAIN)
		self.assertEqual(
			[layer["doctype"] for layer in answer["layers"]],
			["Parcel", "Field", "Irrigation Zone"],
		)
		self.assertEqual(answer["counts"]["Housing Unit"], 2)

	def test_the_parcel_polygon_is_fetched_although_the_register_withholds_it(self):
		"""`_describe_parcel` deliberately reports `mapped` and a centroid and not
		the shape, because a register listing carrying one polygon per row is a
		few kilobytes per parcel for a caller that wanted names. This page is the
		caller that wants them, so the withheld column is read by docname — and
		a page that quietly had no parcel layer would look like a farm with no
		titles registered."""
		self.a_farm()
		answer = farm_overview.farm_overview(company=MAIN)
		parcels = self.layer(answer, "Parcel")
		self.assertEqual(parcels["drawn"], 1)
		self.assertEqual(parcels["shapes"][0]["geometry"]["type"], "Polygon")

	def test_a_block_with_no_boundary_is_counted_rather_than_hidden(self):
		"""Forty blocks, nine of them never traced, is a morning of work somebody
		can do. Thirty-one blocks is a map that lies about the size of the farm."""
		farm = self.a_farm()
		fields = self.layer(farm_overview.farm_overview(company=MAIN), "Field")
		self.assertEqual(fields["total"], 2)
		self.assertEqual(fields["drawn"], 1)
		self.assertEqual(fields["without_boundary"], 1)
		self.assertNotIn(farm["untraced"], [shape["name"] for shape in fields["shapes"]])

	def test_a_boundary_that_will_not_parse_is_reported_with_its_reason(self):
		"""The one row on the whole farm somebody has to know about. Dropping it
		would make a corrupted boundary indistinguishable from an untraced block,
		and those are opposite problems with opposite fixes."""
		farm = self.a_farm()
		frappe.db.set_value("Field", farm["untraced"], "boundary_geojson", "{oops")
		answer = farm_overview.farm_overview(company=MAIN)
		self.assertEqual([entry["name"] for entry in answer["unreadable"]], [farm["untraced"]])
		self.assertIn("not JSON", answer["unreadable"][0]["reason"])
		self.assertIn(farm["untraced"].replace(" ", "%20"), answer["unreadable"][0]["route"])
		# And it is not double-counted as merely untraced.
		fields = self.layer(answer, "Field")
		self.assertEqual(fields["unreadable"], 1)
		self.assertEqual(fields["without_boundary"], 0)

	def test_a_structure_with_gps_is_a_pin_and_one_without_is_a_number(self):
		"""`housing._gps` already refuses null island, so an unlocated cabin
		arrives as None. Drawing it at [0, 0] would put a shop in the Gulf of
		Guinea and the map would look like it worked."""
		farm = self.a_farm()
		answer = farm_overview.farm_overview(company=MAIN)
		self.assertEqual([marker["name"] for marker in answer["markers"]], [farm["located"]])
		self.assertEqual(answer["markers"][0]["point"], [45.6015, -121.178])
		self.assertEqual(answer["housing"]["without_position"], 1)

	def test_every_shape_carries_the_route_to_its_own_record(self):
		"""The page draws no boundary editor and links to the three forms that
		do. A route built without quoting lands on the list view instead, and
		every docname here has spaces in it."""
		farm = self.a_farm()
		answer = farm_overview.farm_overview(company=MAIN)
		shape = self.layer(answer, "Field")["shapes"][0]
		self.assertEqual(shape["name"], farm["mapped"])
		self.assertTrue(shape["route"].startswith("/app/field/"))
		self.assertNotIn(" ", shape["route"])

	def test_a_shape_with_no_stored_centroid_still_has_somewhere_to_print(self):
		"""THE FALLBACK TABLE IS THE ONE PLACE THIS PAGE HAS NO MAP TO POINT AT,
		so a blank coordinate column there is the page failing at the job the
		fallback exists to do. `boundary_centroid` is shapely's, written by the
		boundary tool — and a polygon pasted straight into the Long Text field
		bypasses the tools, which `api/gis.py` says out loud is possible, so the
		column really can be empty on a real site.

		`centre` IS A SECOND KEY AND NOT A FALLBACK INSIDE THE FIRST. The
		substitute is the middle of the bounding box, which is not a centroid;
		merging them would have put an approximation under the name of a stored
		measurement, and nothing that computes acreage or containment should ever
		be able to pick it up by accident."""
		self.a_farm()
		shape = self.layer(farm_overview.farm_overview(company=MAIN), "Field")["shapes"][0]
		self.assertIsNone(shape["centroid"])
		self.assertEqual(shape["centre"], [45.6015, -121.178])

	def test_a_stored_centroid_wins_over_the_computed_middle(self):
		"""The two are different numbers for a shape that is not a rectangle, and
		the stored one is the measurement every other reader of this app means."""
		farm = self.a_farm()
		frappe.db.set_value("Field", farm["mapped"], "boundary_centroid_lat", 45.6011)
		frappe.db.set_value("Field", farm["mapped"], "boundary_centroid_lon", -121.1772)
		shape = self.layer(farm_overview.farm_overview(company=MAIN), "Field")["shapes"][0]
		self.assertEqual(shape["centroid"], [45.6011, -121.1772])
		self.assertEqual(shape["centre"], shape["centroid"])

	def test_the_popup_figures_are_both_acreages_and_not_one(self):
		"""What is recorded and what the polygon encloses are two independent
		measurements, and the whole reason to look at a map is that they can
		disagree. Reporting one of them would hide the disagreement."""
		farm = self.a_farm()
		frappe.db.set_value("Field", farm["mapped"], "area_computed_acres", 25.67)
		shape = self.layer(farm_overview.farm_overview(company=MAIN), "Field")["shapes"][0]
		self.assertEqual(shape["acres"], 25.7)
		self.assertEqual(shape["computed_acres"], 25.67)

	def test_the_box_fits_every_layer_and_every_pin(self):
		self.a_farm()
		answer = farm_overview.farm_overview(company=MAIN)
		self.assertEqual(answer["bounds"], [[45.6, -121.18], [45.603, -121.176]])

	def test_a_farm_with_nothing_traced_has_no_box_at_all(self):
		"""Which is what tells the page to open on HOME_VIEW and say so, rather
		than fitting the map to a box of nothing."""
		self.tool_data(
			"create_parcel", {"owning_entity": MAIN, "parcel_name": "Mill Creek", "acreage": 131.43}
		)
		self.assertIsNone(farm_overview.farm_overview(company=MAIN)["bounds"])

	def test_a_register_this_site_has_not_installed_contributes_nothing(self):
		"""A farm with no irrigation zones registered should get a map with the
		other layers on it, not an error. The four tools each refuse a missing
		doctype BY NAME, which is right on a console and wrong here."""
		self.a_farm()
		INSTALLED_DOCTYPES.discard("Irrigation Zone")
		self.addCleanup(INSTALLED_DOCTYPES.add, "Irrigation Zone")
		answer = farm_overview.farm_overview(company=MAIN)
		self.assertEqual(self.layer(answer, "Irrigation Zone")["total"], 0)
		self.assertEqual(self.layer(answer, "Field")["drawn"], 1)

	def test_the_detail_line_differs_per_register_on_purpose(self):
		"""Four genuinely different records. What is planted on a block, what
		waters a zone, which county holds a title, how many a cabin sleeps."""
		self.a_farm()
		answer = farm_overview.farm_overview(company=MAIN)
		self.assertIn("Skeena", self.layer(answer, "Field")["shapes"][0]["detail"])
		self.assertIn("Wasco", self.layer(answer, "Parcel")["shapes"][0]["detail"])
		self.assertIn("sleeps 4", answer["markers"][0]["detail"])


# ── 4 ─────────────────────────────────────────────────────────────────────────
class TheGate(OverviewTestCase):
	def test_a_register_this_login_may_not_read_is_named_and_left_out(self):
		"""Not a refusal of the whole page. An office manager who may read Fields
		and Parcels but not the housing register should get the map with the
		buildings missing and a line saying which layer was withheld."""
		self.a_farm()
		self.deny("Housing Unit")
		answer = farm_overview.farm_overview(company=MAIN)
		self.assertEqual(answer["refused"], ["Housing Unit"])
		self.assertEqual(answer["markers"], [])
		self.assertEqual(answer["counts"]["Housing Unit"], 0)
		self.assertEqual(self.layer(answer, "Field")["drawn"], 1)

	def test_a_denied_polygon_register_takes_its_layer_and_not_the_page(self):
		"""THE LAYER IS ABSENT RATHER THAN EMPTY, and the difference is the point.
		An entry reading "Parcels — 0" in the legend would say the farm has no
		titles registered; the layer missing plus the register named in `refused`
		says the truth, which is that this login was not allowed to look."""
		self.a_farm()
		self.deny("Parcel")
		answer = farm_overview.farm_overview(company=MAIN)
		self.assertIn("Parcel", answer["refused"])
		self.assertNotIn("Parcel", [layer["doctype"] for layer in answer["layers"]])
		self.assertEqual(answer["counts"]["Parcel"], 0)
		self.assertEqual(self.layer(answer, "Field")["drawn"], 1)

	def test_an_entity_the_caller_may_not_read_is_refused_by_name(self):
		"""Quietly drawing a different farm than the one that was asked for is
		the worst available failure here: every shape on it looks plausible."""
		self.a_farm()
		STORE.denied_permissions.add(("Company", OTHER))
		self.addCleanup(STORE.denied_permissions.discard, ("Company", OTHER))
		self.assertNotIn(OTHER, farm_overview.readable_companies())
		with self.assertRaises(frappe.PermissionError) as caught:
			farm_overview.farm_overview(company=OTHER)
		self.assertIn(OTHER, str(caught.exception))
		self.assertIn(MAIN, str(caught.exception))

	def test_nothing_asked_for_is_the_first_readable_entity_and_not_all_of_them(self):
		"""`list_parcels` REQUIRES a company and the other three registers do not,
		so a multi-entity site opened with nothing chosen would draw every block
		and zone on the site and NO parcels at all — an empty layer that looks
		exactly like a farm which has not registered its titles."""
		self.a_farm()
		answer = farm_overview.farm_overview()
		self.assertEqual(answer["company"], farm_overview.readable_companies()[0])
		self.assertEqual(self.layer(answer, "Parcel")["drawn"], 1)

	def test_the_picker_offers_exactly_what_the_scope_accepts(self):
		"""One list, so an entity that is not offered is also not reachable by
		typing its name into the request."""
		answer = farm_overview.farm_overview(company=MAIN)
		self.assertIn(MAIN, answer["companies"])
		for name in answer["companies"]:
			with self.subTest(company=name):
				self.assertEqual(farm_overview.farm_overview(company=name)["company"], name)


# ── 5 ─────────────────────────────────────────────────────────────────────────
class ItWritesNothing(OverviewTestCase):
	"""The negative control, and the claim the module docstring opens with.

	`api/gis.save_boundary` compares a polygon against ONE record's recorded
	acreage before it commits; a map of forty blocks has no record in front of
	it, so a draw tool here would be a draw tool with nothing to check against.
	"""

	def test_reading_the_map_changes_no_stored_boundary(self):
		farm = self.a_farm()
		before = {
			doctype: frappe.db.get_value(doctype, name, "boundary_geojson")
			for doctype, name in (
				("Parcel", farm["parcel"]),
				("Field", farm["mapped"]),
				("Irrigation Zone", farm["zone"]),
			)
		}
		farm_overview.farm_overview(company=MAIN)
		for doctype, name in (
			("Parcel", farm["parcel"]),
			("Field", farm["mapped"]),
			("Irrigation Zone", farm["zone"]),
		):
			with self.subTest(doctype=doctype):
				self.assertEqual(frappe.db.get_value(doctype, name, "boundary_geojson"), before[doctype])

	def test_the_module_publishes_one_method_and_it_is_a_read(self):
		"""A whitelisted name is a reachable URL. This module has exactly one, and
		its own `methods` is Frappe's default GET-and-POST read."""
		published = {
			name
			for name in dir(farm_overview)
			if not name.startswith("_")
			and getattr(getattr(farm_overview, name), "__wrapped_whitelisted__", False)
		}
		self.assertEqual(published, {"farm_overview"})


# ── 6 ─────────────────────────────────────────────────────────────────────────
class ThePageOnDisk(unittest.TestCase):
	"""A Page is three files that have to agree, and nothing at runtime notices
	when they stop agreeing — Frappe renders an empty panel and moves on."""

	def record(self) -> dict:
		return json.loads((PAGE_DIR / "farm_overview.json").read_text(encoding="utf-8"))

	def script(self) -> str:
		return (PAGE_DIR / "farm_overview.js").read_text(encoding="utf-8")

	def template(self) -> str:
		return (PAGE_DIR / "farm_overview.html").read_text(encoding="utf-8")

	def test_the_record_is_a_standard_page_in_this_apps_module(self):
		record = self.record()
		self.assertEqual(record["doctype"], "Page")
		self.assertEqual(record["standard"], "Yes")
		self.assertEqual(record["module"], "ERPNext MCP")

	def test_the_page_record_names_no_role(self):
		"""A standard Page is rewritten from this app's JSON at every migrate, so
		a role list stored there is a decision an operator makes and loses. The
		gate this page actually applies is Frappe's own read permission per
		register, asked at call time."""
		self.assertEqual(self.record()["roles"], [])

	def test_the_route_is_the_one_the_module_and_the_script_both_name(self):
		"""Frappe keys `frappe.pages` on the Page's own name. A script that keyed
		on anything else would define a handler nothing ever calls."""
		self.assertEqual(self.record()["name"], farm_overview.PAGE_ROUTE)
		self.assertEqual(self.record()["page_name"], farm_overview.PAGE_ROUTE)
		self.assertIn(f'frappe.pages["{farm_overview.PAGE_ROUTE}"]', self.script())

	def test_the_title_is_the_one_the_module_names(self):
		self.assertEqual(self.record()["title"], farm_overview.PAGE_TITLE)

	def test_the_folder_is_the_scrubbed_route(self):
		"""`Page.load_assets` looks for `<module path>/page/<scrub(name)>/`, and a
		folder named anything else means the script and the template are simply
		never loaded."""
		self.assertEqual(PAGE_DIR.name, farm_overview.PAGE_ROUTE.replace("-", "_"))
		for suffix in (".js", ".html", ".json"):
			with self.subTest(suffix=suffix):
				self.assertTrue((PAGE_DIR / f"{PAGE_DIR.name}{suffix}").is_file())

	def test_the_script_carries_the_licence_header_on_its_first_line(self):
		"""CI walks `git ls-files '*.js'` and reads the first three lines. One
		missing header fails the whole standalone job on both Python versions."""
		self.assertEqual(self.script().split("\n", 1)[0], "// SPDX-License-Identifier: MIT")

	def test_the_method_the_script_calls_exists_and_is_whitelisted(self):
		"""A page pointed at a method that is not whitelisted answers 403 and
		nothing on the page says so."""
		self.assertIn('const METHOD = "erpnext_mcp.farm_overview.farm_overview";', self.script())
		self.assertTrue(getattr(farm_overview.farm_overview, "__wrapped_whitelisted__", False))

	def test_the_script_holds_no_tile_url_of_its_own(self):
		"""THE CLAIM THE WIDGET'S OWN DOCSTRING MAKES, enforced rather than
		repeated: "seven copies of a Leaflet bootstrap is seven places for the CDN
		URL, the tile attribution and the zoom defaults to drift apart." This page
		is the eighth caller and copies none of them — and the attributions are
		the CONDITION OF USE for Esri and OpenStreetMap, so a second copy is a
		second place for one to be quietly dropped."""
		script = self.script()
		for forbidden in ("arcgisonline", "tile.openstreetmap", "cdnjs.cloudflare", "leaflet.js"):
			with self.subTest(constant=forbidden):
				self.assertNotIn(forbidden, script)
		self.assertIn("erpnext_mcp.geo_map.load_leaflet", script)
		self.assertIn("add_base_layers", script)

	def test_the_widget_exports_everything_the_page_reaches_for(self):
		"""The two files are wired by property name across a namespace and nothing
		checks it at runtime: a rename is a page that silently has no map."""
		widget = (
			pathlib.Path(__file__).resolve().parent.parent
			/ "erpnext_mcp"
			/ "public"
			/ "js"
			/ "geo_map_widget.js"
		).read_text(encoding="utf-8")
		for export in ("load_leaflet", "add_base_layers", "MAX_FIT_ZOOM", "HOME_VIEW"):
			with self.subTest(export=export):
				self.assertIn(f"erpnext_mcp.geo_map.{export} =", widget)
				self.assertIn(export, self.script())

	def test_the_page_fetches_the_widget_from_this_apps_asset_path(self):
		"""`doctype_js` puts the widget on the seven forms that carry geometry and
		there is no `app_include_js`, both deliberate per hooks.py. A Page is not
		a form, so it fetches the file itself — from the path `bench build` links
		this app's `public/` directory to."""
		self.assertIn('"/assets/erpnext_mcp/js/geo_map_widget.js"', self.script())

	def test_the_template_opens_on_its_own_root_element(self):
		"""`$(frappe.render_template(...))` turns a leading comment into a node of
		its own, so a template that opened with one would hand back a collection
		and every `.find()` in the script would miss."""
		first = next(line for line in self.template().splitlines() if line.strip())
		self.assertTrue(first.startswith('<div class="fo-wrap"'), first)

	def test_the_template_holds_no_straight_apostrophe(self):
		"""Frappe compiles a page template into
		`frappe.templates["..."] = '...';` — a SINGLE-quoted JS string — and the
		only thing between a straight apostrophe and a syntax error that takes the
		whole page script down is one `str.replace` in `scrub_html_template`. The
		entity costs nothing and does not depend on which Frappe is installed."""
		self.assertNotIn("'", self.template())

	def test_the_template_carries_every_hook_the_script_reaches_for(self):
		"""Wired by class name, with nothing checking it at runtime: a renamed
		class is a control that silently does nothing."""
		template = self.template()
		script = self.script()
		for hook in (
			"fo-company-row",
			"fo-summary",
			"fo-notices",
			"fo-panel",
			"fo-map",
			"fo-legend",
			"fo-fallback",
			"fo-empty",
		):
			with self.subTest(hook=hook):
				self.assertIn(hook, template)
				self.assertIn(hook, script)

	def test_the_template_names_the_id_the_script_reads(self):
		self.assertIn('id="fo-company"', self.template())
		self.assertIn("#fo-company", self.script())

	def test_the_map_canvas_is_given_a_height_in_the_stylesheet(self):
		"""Leaflet measures the container it is handed. A container with no height
		gets nought and draws a single tile in the corner, which looks like a
		broken map rather than a layout problem — the oldest bug in embedding
		this library and the reason it is asserted rather than assumed."""
		self.assertRegex(self.template(), r"\.fo-map\s*\{[^}]*height:\s*\d+px")


# ── 7: v0.161.0 — the machines, the jobs, and the door from the board ────────

#: A tagged wind machine standing in the middle of the mapped block, and a
#: tractor nobody has stood at with a phone. The pair is the whole positioning
#: claim: one pin and one number.
ASSETS = [
	{
		"name": "40-5-MPH",
		"asset_type": "Wind Machine",
		"description": "Orchard-Rite 5-blade",
		"gps_latitude": 45.6015,
		"gps_longitude": -121.1780,
		# A JSON COLUMN AND NOT A BARE WORD, which is what the doctype declares
		# and what `_current_state_value` reads. A fixture storing "Running" here
		# would make the popup's state line come back empty for a reason that has
		# nothing to do with this page.
		"current_state": '{"state": "Running"}',
		"last_scan_at": "2026-09-01 08:00:00",
		"last_scan_by": "HR-EMP-00001",
	},
	{
		"name": "TRAC-01",
		"asset_type": "Tractor",
		"description": "Kubota M7",
		# THE VALUE A BENCH ACTUALLY RETURNS for a machine nobody has located.
		# Every Frappe Float column is NOT NULL DEFAULT 0, so this is 0.0 and not
		# None on a real site — seeding None here would make the null-island
		# refusal below pass for a reason that does not exist off this harness.
		# See `the-standalone-double-nests-what-mariadb-flattens`.
		"gps_latitude": 0.0,
		"gps_longitude": 0.0,
	},
]


class TheMachinesAndTheJobs(OverviewTestCase):
	"""v0.161.0. Two registers that are ON the ground without being shaped.

	The map has drawn boundaries since v0.110.0 and buildings with them. What it
	could not show is the two things that MOVE: the machines somebody has to walk
	to, and the work that is outstanding on the ground underneath. A dispatch
	Kanban answers "what is open and who has it" and cannot answer "where" — six
	Critical cards in one block is an afternoon, six across four parcels is a day
	and a truck, and nothing on that screen tells them apart.
	"""

	def a_machine_and_a_job(self, farm, **task):
		STORE.seed("Asset Register", [{**row, "company": MAIN} for row in ASSETS])
		payload = {
			"name": "TASK-MAP-1",
			"task_name": "Repair the line",
			"task_type": "Repair",
			"state": "Available",
			"urgency": "Critical",
			"company": MAIN,
			"location_doctype": "Field",
			"location": farm["mapped"],
		}
		payload.update(task)
		STORE.seed("Farm Task", [payload])
		return payload["name"]

	# ── assets ──────────────────────────────────────────────────────────
	def test_a_tagged_asset_with_gps_is_a_pin(self):
		farm = self.a_farm()
		self.a_machine_and_a_job(farm)
		answer = farm_overview.farm_overview(company=MAIN)
		pins = {entry["name"]: entry for entry in answer["assets"]}
		self.assertEqual(sorted(pins), ["40-5-MPH"])
		self.assertEqual(pins["40-5-MPH"]["point"], [45.6015, -121.178])

	def test_an_asset_at_null_island_is_a_number_and_not_a_pin(self):
		"""THE COUNT, which is the half of this that is worth printing: "ninety
		tags, sixty of them nowhere" is a morning's work with a phone.

		It is NOT the test of the pair check — a fully unset asset is already
		`None, None` by the time this module sees it, because `_describe_asset`
		nulls the zeros. See `test_half_a_coordinate_is_not_a_pin_either` for the
		case that actually reaches `_on_earth`.
		"""
		farm = self.a_farm()
		self.a_machine_and_a_job(farm)
		summary = farm_overview.farm_overview(company=MAIN)["asset_summary"]
		self.assertEqual(summary["total"], 2)
		self.assertEqual(summary["drawn"], 1)
		self.assertEqual(summary["without_position"], 1)

	def test_half_a_coordinate_is_not_a_pin_either(self):
		"""THE CASE `_on_earth` ACTUALLY DECIDES HERE, and the reason the guard is
		not redundant with the one inside `list_assets`.

		`_describe_asset` turns a stored `0.0` into `None` before this module sees
		it, so a FULLY unpositioned asset is already gone by the time
		`_asset_markers` runs — which means a test seeding `0.0, 0.0` passes with
		or without the pair check and proves nothing about it. What reaches this
		guard is a HALF-set pair: a latitude that is really unset beside a
		longitude somebody typed. Drawn, that is a pin in the Atlantic off Africa
		with a farm's name on it.
		"""
		self.a_farm()
		STORE.seed(
			"Asset Register",
			[
				{
					"name": "HALF-1",
					"asset_type": "Storage",
					"company": MAIN,
					"gps_latitude": 0.0,
					"gps_longitude": -121.1780,
				}
			],
		)
		answer = farm_overview.farm_overview(company=MAIN)
		self.assertEqual(answer["assets"], [])
		self.assertEqual(answer["asset_summary"]["without_position"], 1)

	def test_a_coordinate_off_earth_is_not_a_pin(self):
		"""A longitude typed with a digit too many. Drawn, it would stretch the
		bounding box across a hemisphere and render the farm as a speck."""
		self.a_farm()
		STORE.seed(
			"Asset Register",
			[
				{
					"name": "TYPO-1",
					"asset_type": "Tractor",
					"company": MAIN,
					"gps_latitude": 45.6015,
					"gps_longitude": -1211.780,
				}
			],
		)
		self.assertEqual(farm_overview.farm_overview(company=MAIN)["assets"], [])

	def test_the_pin_carries_what_the_popup_prints(self):
		farm = self.a_farm()
		self.a_machine_and_a_job(farm)
		pin = farm_overview.farm_overview(company=MAIN)["assets"][0]
		self.assertEqual(pin["description"], "Orchard-Rite 5-blade")
		self.assertEqual(pin["last_scan_at"], "2026-09-01 08:00:00")
		self.assertEqual(pin["route"], "/app/asset-register/40-5-MPH")

	def test_the_state_is_the_word_and_not_the_blob(self):
		"""`current_state` is a JSON column — a valve's open/closed, a sprayer's
		full/empty — and the word a person wants is under its `state` key. Read
		through the function `scan_asset` already uses, rather than a third
		implementation of one extraction."""
		farm = self.a_farm()
		self.a_machine_and_a_job(farm)
		pin = farm_overview.farm_overview(company=MAIN)["assets"][0]
		self.assertEqual(pin["current_state"], "Running")

	def test_the_glyph_comes_off_the_type_record(self):
		"""v0.162.0 MOVED THE GLYPH INTO THE REGISTER. It was a four-entry table
		in this module — the shortest of the four asset-type lists that existed —
		so nine of the thirteen types shared one grey badge. An operator changes
		it in the Desk now, which is the whole point of the register."""
		farm = self.a_farm()
		self.a_machine_and_a_job(farm)
		pin = farm_overview.farm_overview(company=MAIN)["assets"][0]
		self.assertEqual(pin["icon"], asset_types.icon_for("Wind Machine"))
		self.assertEqual(pin["icon_label"], "Wind Machine")

	def test_editing_the_type_record_changes_the_badge(self):
		"""The claim the register exists to make. No release, no redeploy."""
		farm = self.a_farm()
		self.a_machine_and_a_job(farm)
		frappe.db.set_value("Farm Asset Type", "Wind Machine", "icon", "F")
		self.assertEqual(farm_overview.farm_overview(company=MAIN)["assets"][0]["icon"], "F")

	def test_every_shipped_type_has_a_glyph_of_its_own(self):
		"""Fifteen types, fifteen distinct badges. Two identical letters on one
		map is worse than an unmemorable one — and the colours differ as well,
		so a collision would have to be read off the legend."""
		glyphs = [icon for _name, icon, _order, _detail in asset_types.SEEDED]
		self.assertEqual(len(glyphs), len(set(glyphs)))

	def test_an_asset_type_the_map_does_not_colour_still_gets_a_pin(self):
		"""A type an operator invented and gave no icon. It takes its OWN initial
		rather than a generic badge — 'Cider Press' reads as C, which is at least
		the right thing — and the default colour. A type silently missing from
		the map is the failure this fallback exists to stop, and from v0.162.0 an
		operator can add one without touching this app at all."""
		self.a_farm()
		STORE.seed(
			"Asset Register",
			[
				{
					"name": "WEIRD-1",
					"asset_type": "Cider Press",
					"company": MAIN,
					"gps_latitude": 45.6015,
					"gps_longitude": -121.1780,
				}
			],
		)
		pin = farm_overview.farm_overview(company=MAIN)["assets"][0]
		self.assertEqual(pin["icon"], "C")
		self.assertEqual(pin["icon_label"], "Cider Press")
		self.assertEqual(pin["colour"], farm_overview.ASSET_ICON_DEFAULT["colour"])

	# ── tasks ───────────────────────────────────────────────────────────
	def test_an_open_task_is_a_pin_at_the_centre_of_its_ground(self):
		farm = self.a_farm()
		self.a_machine_and_a_job(farm)
		answer = farm_overview.farm_overview(company=MAIN)
		task = answer["tasks"][0]
		block = self.layer(answer, "Field")["shapes"][0]
		self.assertEqual(task["name"], "TASK-MAP-1")
		self.assertEqual(task["point"], block["centre"])

	def test_placing_a_task_costs_no_extra_register_read(self):
		"""THE DESIGN CLAIM. A task's location is a Dynamic Link over four
		registers this answer has just finished computing a centre for, so the
		pin comes off `_placements` rather than a fifth query — which would also
		be a SECOND answer to "where is this block" that could disagree with the
		polygon drawn underneath it."""
		farm = self.a_farm()
		places = farm_overview._placements(
			[
				{
					"doctype": "Field",
					"shapes": [{"name": farm["mapped"], "centre": [45.6015, -121.178]}],
				}
			],
			[{"doctype": "Housing Unit", "name": farm["located"], "point": [45.6, -121.1]}],
		)
		self.assertEqual(places[("Field", farm["mapped"])], [45.6015, -121.178])
		self.assertEqual(places[("Housing Unit", farm["located"])], [45.6, -121.1])

	def test_a_task_naming_no_location_is_counted_apart(self):
		farm = self.a_farm()
		self.a_machine_and_a_job(farm)
		STORE.seed(
			"Farm Task",
			[
				{
					"name": "TASK-NOWHERE",
					"task_name": "Order parts",
					"state": "Available",
					"urgency": "Normal",
					"company": MAIN,
				}
			],
		)
		summary = farm_overview.farm_overview(company=MAIN)["task_summary"]
		self.assertEqual(summary["drawn"], 1)
		self.assertEqual(summary["without_location"], 1)
		self.assertEqual(summary["without_a_drawn_place"], 0)

	def test_a_task_on_ground_nobody_has_traced_is_a_different_count(self):
		"""THE THREE WAYS A TASK HAS NOWHERE TO GO ARE THREE JOBS OF WORK. No
		location is a dispatch gap; ground that was never traced is a BOUNDARY
		somebody owes and the task is fine. Merging them into one "not shown"
		would answer neither."""
		farm = self.a_farm()
		self.a_machine_and_a_job(farm)
		STORE.seed(
			"Farm Task",
			[
				{
					"name": "TASK-UNTRACED",
					"task_name": "Scout the ridge",
					"state": "Available",
					"urgency": "Normal",
					"company": MAIN,
					"location_doctype": "Field",
					"location": farm["untraced"],
				}
			],
		)
		summary = farm_overview.farm_overview(company=MAIN)["task_summary"]
		self.assertEqual(summary["without_location"], 0)
		self.assertEqual(summary["without_a_drawn_place"], 1)
		self.assertEqual(summary["unplaceable"][0]["location"], farm["untraced"])
		self.assertEqual(summary["unplaceable"][0]["route"], "/app/farm-task/TASK-UNTRACED")

	def test_urgency_decides_the_colour(self):
		farm = self.a_farm()
		self.a_machine_and_a_job(farm)
		self.assertEqual(farm_overview.farm_overview(company=MAIN)["tasks"][0]["colour"], "#cf222e")

	def test_an_urgency_the_table_does_not_know_takes_normals_colour(self):
		"""NOT THE TOP OF THE LIST. `.get(key, first)` on an ordering table is how
		a typo in a Select becomes the most urgent thing on the farm — see
		`an-unknown-select-option-ranks-first`."""
		farm = self.a_farm()
		self.a_machine_and_a_job(farm, name="TASK-ODD", urgency="Blocker")
		task = farm_overview.farm_overview(company=MAIN)["tasks"][0]
		self.assertEqual(task["urgency"], "Blocker")
		self.assertEqual(task["colour"], farm_overview.URGENCY_DEFAULT)

	def test_an_unknown_urgency_sorts_last_and_not_first(self):
		"""The other half of the same trap: an unrecognised value must not be
		drawn on top of every Critical pin at the same block."""
		self.assertEqual(farm_overview._urgency_rank("Blocker"), -1)
		self.assertLess(farm_overview._urgency_rank("Blocker"), farm_overview._urgency_rank("Low"))

	def test_the_worst_job_is_first_in_the_list(self):
		"""WORST FIRST IS THE READING ORDER. The fallback table prints this list
		straight through, so the Critical jobs are at the top of the one surface
		that exists when there is no map at all. The script walks it BACKWARDS
		for the markers, because Leaflet draws later ones on top."""
		farm = self.a_farm()
		self.a_machine_and_a_job(farm)
		STORE.seed(
			"Farm Task",
			[
				{
					"name": "TASK-CALM",
					"task_name": "Tidy",
					"state": "Available",
					"urgency": "Low",
					"company": MAIN,
					"location_doctype": "Field",
					"location": farm["mapped"],
				}
			],
		)
		order = [task["urgency"] for task in farm_overview.farm_overview(company=MAIN)["tasks"]]
		self.assertEqual(order, ["Critical", "Low"])

	def test_a_task_pin_never_stretches_the_bounding_box(self):
		"""It sits at a centre already inside the box by construction, so the
		frame is the same with the tasks as without them."""
		farm = self.a_farm()
		before = farm_overview.farm_overview(company=MAIN)["bounds"]
		self.a_machine_and_a_job(farm)
		self.assertEqual(farm_overview.farm_overview(company=MAIN)["bounds"], before)

	# ── the block ticker ────────────────────────────────────────────────
	def test_the_block_ticker_rides_on_the_field_shape(self):
		"""What is painted on the bin, said on the radio and written on the tally
		sheet — which is not what the block is called in the database."""
		farm = self.a_farm()
		frappe.db.set_value("Field", farm["mapped"], "block_ticker", "YC3")
		shape = self.layer(farm_overview.farm_overview(company=MAIN), "Field")["shapes"][0]
		self.assertEqual(shape["block_ticker"], "YC3")
		self.assertEqual(shape["crop"], "Cherry")
		self.assertEqual(shape["variety"], "Skeena")

	def test_only_a_field_carries_a_ticker(self):
		"""`None` rather than "" so the script can tell "this register has no
		ticker" from "this block has not been given one"."""
		self.a_farm()
		answer = farm_overview.farm_overview(company=MAIN)
		for doctype in ("Parcel", "Irrigation Zone"):
			with self.subTest(doctype=doctype):
				for shape in self.layer(answer, doctype)["shapes"]:
					self.assertIsNone(shape["block_ticker"])

	# ── the gate ────────────────────────────────────────────────────────
	def test_a_register_this_login_may_not_read_is_named_and_left_out(self):
		farm = self.a_farm()
		self.a_machine_and_a_job(farm)
		self.deny("Asset Register")
		answer = farm_overview.farm_overview(company=MAIN)
		self.assertIn("Asset Register", answer["refused"])
		self.assertEqual(answer["assets"], [])
		# AND THE REST OF THE MAP IS STILL THERE. A farm whose office manager may
		# not read the tag register should get the boundaries and the jobs.
		self.assertTrue(answer["tasks"])
		self.assertTrue(self.layer(answer, "Field")["shapes"])

	def test_denying_the_task_register_leaves_the_machines(self):
		farm = self.a_farm()
		self.a_machine_and_a_job(farm)
		self.deny("Farm Task")
		answer = farm_overview.farm_overview(company=MAIN)
		self.assertIn("Farm Task", answer["refused"])
		self.assertEqual(answer["tasks"], [])
		self.assertTrue(answer["assets"])


class TheDoorFromTheBoard(V12TestCase):
	"""v0.161.0. The Map View entry on the Farm Task list and its Kanban."""

	def setUp(self):
		super().setUp()
		STORE.rows("Client Script").clear()

	def test_it_creates_the_entry(self):
		report = farm_task_map_action.seed_farm_task_map_action()
		self.assertTrue(report["created"])
		self.assertTrue(farm_task_map_action._existing())

	def test_it_is_bound_to_the_farm_task_list(self):
		farm_task_map_action.seed_farm_task_map_action()
		row = STORE.rows("Client Script")[0]
		self.assertEqual(row["dt"], "Farm Task")
		self.assertEqual(row["view"], "List")

	def test_a_second_migrate_creates_nothing(self):
		farm_task_map_action.seed_farm_task_map_action()
		second = farm_task_map_action.seed_farm_task_map_action()
		self.assertFalse(second["created"])
		self.assertEqual(second["reason"], "already present")
		self.assertEqual(len(STORE.rows("Client Script")), 1)

	def test_it_does_not_overwrite_the_list_settings_object(self):
		source = farm_task_map_action.SCRIPT_SOURCE
		self.assertIn('frappe.listview_settings["Farm Task"] || {}', source)
		self.assertIn("previous_onload", source)

	def test_it_is_a_menu_item_and_not_an_actions_entry(self):
		"""A KANBAN HAS NO CHECKBOXES. `add_actions_menu_item` puts an entry in
		the menu that appears once rows are ticked, which on the one view this
		exists for is a door that never opens."""
		source = farm_task_map_action.SCRIPT_SOURCE
		self.assertIn("add_menu_item", source)
		self.assertNotIn("add_actions_menu_item", source)

	def test_it_does_not_stack_up_when_the_view_is_switched(self):
		"""`onload` runs again every time somebody moves between List, Report and
		Kanban, and Frappe's menu does not de-duplicate."""
		self.assertIn("__erpnext_mcp_map_view", farm_task_map_action.SCRIPT_SOURCE)

	def test_it_opens_the_page_this_app_actually_ships(self):
		"""A button pointing at a route that does not exist is a 404 an operator
		reads as a broken app."""
		self.assertEqual(farm_task_map_action.MAP_ROUTE, farm_overview.PAGE_ROUTE)
		self.assertIn(f'frappe.set_route("{farm_overview.PAGE_ROUTE}")', farm_task_map_action.SCRIPT_SOURCE)

	def test_it_carries_no_board_filter(self):
		"""A Kanban filtered to one crew does not mean the map should hide the
		other crew's work. The question the map is opened to answer is "where is
		all of this"."""
		self.assertNotIn("get_filters", farm_task_map_action.SCRIPT_SOURCE)
		self.assertNotIn("current_view", farm_task_map_action.SCRIPT_SOURCE)

	def test_an_operators_edit_survives_every_future_migrate(self):
		farm_task_map_action.seed_farm_task_map_action()
		name = farm_task_map_action._existing()
		edited = farm_task_map_action.SCRIPT_SOURCE + "\n// mine\n"
		frappe.db.set_value("Client Script", name, "script", edited)

		farm_task_map_action.seed_farm_task_map_action()
		self.assertEqual(frappe.db.get_value("Client Script", name, "script"), edited)

	def test_uninstall_takes_it_away(self):
		farm_task_map_action.seed_farm_task_map_action()
		self.assertTrue(farm_task_map_action.remove_farm_task_map_action()["removed"])
		self.assertEqual(farm_task_map_action._existing(), "")

	def test_it_does_not_collide_with_the_asset_register_rows(self):
		"""Three Client Scripts from one app on three doctypes; each seeder has to
		find its OWN by marker."""
		from erpnext_mcp import asset_tag_list_action

		asset_tag_list_action.seed_asset_tag_list_action()
		farm_task_map_action.seed_farm_task_map_action()
		self.assertEqual(len(STORE.rows("Client Script")), 2)
		self.assertNotEqual(asset_tag_list_action._existing(), farm_task_map_action._existing())

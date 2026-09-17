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
		button = (REPO / "erpnext_mcp" / "public" / "js" / "lot_line_adjustment_map.js").read_text()
		self.assertIn('frappe.set_route("land-map")', button)
		self.assertIn("lot_line_adjustment: frm.doc.name", button)
		self.assertIn("lot_line_adjustment", self.script())


# ── 5 ───────────────────────────────────────────────────────────────────────
HARNESS = r"""// Run the real page script under a stubbed Desk. Prints JSON on stdout.
const fs = require("fs");
const vm = require("vm");

const SCRIPT = process.argv[2];
const ANSWER = JSON.parse(process.argv[3]);
const PREVIEW = JSON.parse(process.argv[4]);

const calls = { methods: [], args: [], drawn: [], rows: 0, layers: 0, indicators: [] };

function node(selector) {
	const self = {
		selector: selector,
		handlers: {},
		value: "",
		html_content: "",
		text_content: "",
		children: [],
		find: function (child) { return node(child); },
		on: function (event, fn) { self.handlers[event] = fn; registry[selector] = self; return self; },
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

global.$ = function () { return root; };
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
				"easements": [],
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


@unittest.skipUnless(shutil.which("node"), "needs node to execute the page script")
class ThePageRuns(unittest.TestCase):
	"""THE PAGE, EXECUTED. A substring assertion matches the whole file; this
	drives the buttons and reads what the script actually did."""

	report: ClassVar[dict] = {}

	@classmethod
	def setUpClass(cls):
		answer, preview = page_fixtures()
		with tempfile.TemporaryDirectory() as folder:
			harness = Path(folder) / "harness.js"
			harness.write_text(HARNESS)
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
		self.assertEqual(corridors["features"][0]["properties"]["label"], "Dry Hollow ditch")

	def test_a_drawn_easement_does_not_become_the_proposal(self):
		"""The corridor and the proposed line are two different drawings, and a
		page that confused them would save a ditch as somebody's new lot line."""
		index = self.report["methods"].index("erpnext_mcp.api.land_map.survey_preview")
		self.assertEqual(json.loads(self.report["args"][index]["points"]), DRAWN)

	def test_saving_posts_the_geometry_to_the_save_method(self):
		index = self.report["methods"].index("erpnext_mcp.api.land_map.save_proposal")
		arguments = self.report["args"][index]
		self.assertEqual(arguments["adjustment"], "LLA-2026-0001")
		self.assertEqual(json.loads(arguments["proposed_geometry"])["type"], "Polygon")
		self.assertIn("thence", arguments["legal_description"])
		self.assertEqual(
			json.loads(arguments["easement_geometry"])["features"][0]["properties"]["label"],
			"Dry Hollow ditch",
		)

# SPDX-License-Identifier: MIT
"""The whole-farm map, executed rather than grepped. v0.161.0.

`test_farm_overview.py` proves what the SERVER answers and that the three files
of the Page agree with each other. It cannot prove that the page draws, because
a substring assertion on a Desk script matches the whole file — `grep layerGroup`
is true of a build that creates the groups and never adds a shape to one.

WHAT THIS FILE ASKS INSTEAD is what `draw()` actually did with a payload: which
layer group each shape landed in, which order the task pins were added in, which
polygons got a permanent label, and what the layer control was handed. Every one
of those is a question about control flow, and v0.161.0 shipped a bug in exactly
one of them that reading caught and no grep would have:

  THE TASK MARKERS HAD TO BE ADDED IN REVERSE. The server sorts `tasks` worst
  first, because that is the order the fallback TABLE wants — Critical at the
  top of the one surface that exists when there is no map. A map wants the
  opposite: several jobs on one block share a centroid exactly, Leaflet draws
  later markers over earlier ones, and the pin that has to be clickable is the
  urgent one. Iterating the list straight through buries every Critical job
  under a Low one, on a map, silently, and looks completely fine in a screenshot
  taken on a farm that never has two jobs in one block.

THE HARNESS IS `test_asset_map.py`'S, pointed at a Page instead of a form. It
stubs the Desk — `frappe.pages`, `frappe.render_template`, a jQuery thin enough
to satisfy the page's `.find()` calls, and a Leaflet whose layer groups record
what was added to them — runs the real file under `node:vm`, and reports what
happened as JSON.

SKIPPED WITHOUT `node`, and a skip here is a statement about the machine. CI has
node; a bench does not need it, because this tests a file the browser runs.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "erpnext_mcp"
PAGE = APP_DIR / "erpnext_mcp" / "page" / "farm_overview" / "farm_overview.js"

#: A farm with one traced block carrying a ticker, one parcel, a cabin, a wind
#: machine and three jobs at the SAME centroid — which is the case the drawing
#: order is about and the case a one-task fixture cannot see.
PAYLOAD = {
	"company": "Orchard Meadow, LLC",
	"companies": ["Orchard Meadow, LLC"],
	"bounds": [[45.6, -121.18], [45.603, -121.176]],
	"layers": [
		{
			"doctype": "Parcel",
			"label": "Parcels",
			"colour": "#8250df",
			"fill_opacity": 0.06,
			"weight": 2,
			"dash_array": "6 4",
			"drawn": 1,
			"total": 1,
			"unreadable": 0,
			"without_boundary": 0,
			"shapes": [
				{
					"doctype": "Parcel",
					"name": "MC",
					"label": "Mill Creek",
					"route": "/app/parcel/MC",
					"geometry": {"type": "Point", "coordinates": [-121.178, 45.6015]},
					"centre": [45.6015, -121.178],
					"block_ticker": None,
					"detail": "Wasco, OR",
				}
			],
		},
		{
			"doctype": "Field",
			"label": "Fields",
			"colour": "#1a7f37",
			"fill_opacity": 0.2,
			"weight": 3,
			"dash_array": None,
			"drawn": 2,
			"total": 2,
			"unreadable": 0,
			"without_boundary": 0,
			"shapes": [
				{
					"doctype": "Field",
					"name": "YC3",
					"label": "Yellow Camp Block 3",
					"route": "/app/field/YC3",
					"geometry": {"type": "Point", "coordinates": [-121.178, 45.6015]},
					"centre": [45.6015, -121.178],
					"block_ticker": "YC3",
					"detail": "Skeena Cherry",
				},
				{
					"doctype": "Field",
					"name": "RT",
					"label": "Ridge Top",
					"route": "/app/field/RT",
					"geometry": {"type": "Point", "coordinates": [-121.177, 45.601]},
					"centre": [45.601, -121.177],
					"block_ticker": None,
					"detail": "Cherry",
				},
			],
		},
		{
			# AN EMPTY REGISTER. It must not be offered as a toggle: a control
			# entry that switches nothing is a control nobody trusts.
			"doctype": "Irrigation Zone",
			"label": "Irrigation zones",
			"colour": "#0969da",
			"fill_opacity": 0.22,
			"weight": 2,
			"dash_array": None,
			"drawn": 0,
			"total": 0,
			"unreadable": 0,
			"without_boundary": 0,
			"shapes": [],
		},
	],
	"markers": [
		{
			"doctype": "Housing Unit",
			"name": "C1",
			"label": "Cabin 1",
			"route": "/app/housing-unit/C1",
			"point": [45.6, -121.18],
			"detail": "Cabin · sleeps 4",
		}
	],
	"housing": {"label": "Structures", "total": 1, "drawn": 1, "without_position": 0},
	"assets": [
		{
			"doctype": "Asset Register",
			"name": "40-5-MPH",
			"label": "40-5-MPH",
			"route": "/app/asset-register/40-5-MPH",
			"point": [45.6015, -121.178],
			"asset_type": "Wind Machine",
			"icon": "W",
			"colour": "#1a7f37",
			"icon_label": "Wind machine",
			"description": "Orchard-Rite 5-blade",
			"current_state": "Running",
			"last_scan_at": "2026-09-01 08:00:00",
			"last_scan_by": "HR-EMP-00001",
		}
	],
	"asset_summary": {
		"label": "Assets",
		"total": 2,
		"drawn": 1,
		"without_position": 1,
		"by_asset_type": {"Wind Machine": 1},
		"icons": {"Wind Machine": {"glyph": "W", "colour": "#1a7f37", "label": "Wind machine"}},
	},
	# WORST FIRST, which is what the server answers.
	"tasks": [
		{
			"doctype": "Farm Task",
			"name": "T-CRIT",
			"label": "Fix the line",
			"route": "/app/farm-task/T-CRIT",
			"point": [45.6015, -121.178],
			"urgency": "Critical",
			"colour": "#cf222e",
			"state": "Available",
			"task_type": "Repair",
			"assigned_to": None,
			"location": "YC3",
			"location_route": "/app/field/YC3",
		},
		{
			"doctype": "Farm Task",
			"name": "T-HIGH",
			"label": "Check the pump",
			"route": "/app/farm-task/T-HIGH",
			"point": [45.6015, -121.178],
			"urgency": "High",
			"colour": "#bc4c00",
			"state": "Claimed",
			"task_type": "Inspection",
			"assigned_to": "HR-EMP-00002",
			"assigned_to_name": "Ana Ruiz",
			"location": "YC3",
			"location_route": "/app/field/YC3",
		},
		{
			"doctype": "Farm Task",
			"name": "T-LOW",
			"label": "Tidy the headland",
			"route": "/app/farm-task/T-LOW",
			"point": [45.6015, -121.178],
			"urgency": "Low",
			"colour": "#6e7781",
			"state": "Available",
			"task_type": "Scouting",
			"assigned_to": None,
			"location": "YC3",
			"location_route": "/app/field/YC3",
		},
	],
	"task_summary": {
		"label": "Open tasks",
		"total": 4,
		"drawn": 3,
		"without_location": 1,
		"without_a_drawn_place": 0,
		"unplaceable": [],
		"by_urgency": {"Critical": 1, "High": 1, "Low": 1},
		"urgency_order": ["Critical", "High", "Normal", "Low"],
		"colours": {"Critical": "#cf222e", "High": "#bc4c00", "Normal": "#0969da", "Low": "#6e7781"},
	},
	"counts": {},
	"unreadable": [],
	"refused": [],
	"cap": 500,
	"capped": [],
	"page_route": "farm-overview",
	"overlay": None,
	"overlay_layers": [],
	"overlay_refused": None,
}

HARNESS = r"""// Drive the real page script under a stubbed Desk. Prints JSON on stdout.
const fs = require("fs");
const vm = require("vm");

const SCRIPT = process.argv[2];
const PAYLOAD = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));

const calls = {
	groups: {},          // layer group id -> what was added to it
	tooltips: [],        // permanent labels drawn on the ground
	control: null,       // what add_base_layers was handed as overlays
	popups: [],
	fitted: null,
	html: {},            // selector -> last html() written to it
	tiles: [],           // v0.185.0: every L.tileLayer, with its url and options
	overlays: [],        // labels handed to control.addOverlay, in order
	on_map: [],          // tile layers currently on the map, by key
	after: [],           // what each scripted action left behind
};

// v0.185.0. A MAP THAT FIRES WHAT LEAFLET FIRES. Control.Layers listens to every
// overlay it holds and fires `overlayadd`/`overlayremove` on the map whenever one
// is added or removed BY ANY MEANS — a tick in the control, `addTo(map)` or
// `map.removeLayer` — so the page's own restore-on-reload and its one-at-a-time
// removal both go through these handlers. A stub that fired only on a simulated
// click would test a Leaflet that does not exist.
//
// AND A CONTROL THAT CLICKS THE WAY LEAFLET 1.9's DOES. `_onInputClick` does not
// add the one layer that was ticked: it walks EVERY input, removes the unticked
// ones and adds the ticked ones, with `_handlingClick` set so the checkboxes are
// not redrawn until it is done. A handler that removes a sibling during that walk
// has it re-added by the walk a moment later — the bug the live bench showed on
// 2026-09-26 and a stub that fired one add per tick could not.
const handlers = {};
const in_control = new Set();
const control_order = [];
const checked = new Map();
const present = new Set();
let handling_click = false;
function fire(type, layer) {
	(handlers[type] || []).forEach(function (handler) { handler({ layer: layer }); });
}
function sync(layer) {
	// `_onLayerChange`: the boxes follow the map, except mid-click.
	if (!handling_click) { checked.set(layer, present.has(layer)); }
}
function put(layer) {
	if (present.has(layer)) { return; }
	present.add(layer);
	if (in_control.has(layer)) { sync(layer); fire("overlayadd", layer); }
}
function take(layer) {
	if (!present.has(layer)) { return; }
	present.delete(layer);
	if (in_control.has(layer)) { sync(layer); fire("overlayremove", layer); }
}
function click(layer) {
	checked.set(layer, !checked.get(layer));
	handling_click = true;
	const added = [];
	const removed = [];
	for (let i = control_order.length - 1; i >= 0; i--) {
		(checked.get(control_order[i]) ? added : removed).push(control_order[i]);
	}
	removed.forEach(function (entry) { if (present.has(entry)) { take(entry); } });
	added.forEach(function (entry) { if (!present.has(entry)) { put(entry); } });
	handling_click = false;
}
function tile_key(layer) {
	const match = /layer=([a-z_]+)/.exec(layer.__url);
	return match ? match[1] : layer.__url;
}

let next_group = 0;
function layerGroup() {
	const id = "g" + next_group++;
	calls.groups[id] = [];
	const group = {
		__id: id,
		addTo: function () { return group; },
		addLayer: function () { return group; },
	};
	return group;
}

function shapeish(kind, detail) {
	const self = {
		__kind: kind,
		__detail: detail || {},
		addTo: function (target) {
			if (target && target.__id) { calls.groups[target.__id].push(self); }
			return self;
		},
		bindPopup: function (html) { self.__popup = String(html); calls.popups.push(String(html)); return self; },
		bindTooltip: function () { return self; },
		setContent: function (text) { self.__detail.content = String(text); return self; },
		setLatLng: function (point) { self.__detail.point = point; return self; },
	};
	return self;
}

const L = {
	map: function () {
		return {
			// A removed map takes its listeners and its layers with it, as
			// Leaflet's does — or a reload would be tested against the old map.
			// AND IN LEAFLET's ORDER: `unload` first, then every layer removed,
			// each removal of a control overlay firing `overlayremove` while the
			// page's handlers are still attached. Found on the live bench,
			// 2026-09-26: a handler that read that as "switched off" lost the
			// layer on every Refresh, and a stub that cleared silently hid it.
			remove: function () {
				fire("unload", null);
				Array.from(present).forEach(take);
				Object.keys(handlers).forEach(function (type) { delete handlers[type]; });
				present.clear();
				in_control.clear();
				control_order.length = 0;
				checked.clear();
			},
			invalidateSize: function () {},
			setView: function () {}, getContainer: function () { return { offsetWidth: 900 }; },
			fitBounds: function (bounds) { calls.fitted = bounds; },
			getSize: function () { return { x: 900, y: 620 }; },
			addLayer: function (layer) { put(layer); },
			removeLayer: function (layer) { take(layer); },
			hasLayer: function (layer) { return present.has(layer); },
			getZoom: function () { return 15; },
			on: function (type, handler) { (handlers[type] = handlers[type] || []).push(handler); },
			// Leaflet's `off(type)` with no function drops every listener of that type.
			off: function (type) { delete handlers[type]; },
		};
	},
	layerGroup: layerGroup,
	geoJSON: function (geometry, options) { return shapeish("geojson", { options: options }); },
	circleMarker: function (point, options) { return shapeish("circle", { point: point, options: options }); },
	marker: function (point, options) { return shapeish("marker", { point: point, options: options }); },
	divIcon: function (options) { return options; },
	tooltip: function (options) {
		const tip = shapeish("tooltip", { options: options });
		const add = tip.addTo;
		tip.addTo = function (target) { calls.tooltips.push(tip.__detail); return add(target); };
		return tip;
	},
	latLngBounds: function (a, b) {
		const bounds = b === undefined ? a : [a, b];
		return { pad: function () { return bounds; }, __bounds: bounds };
	},
	tileLayer: function (url, options) {
		const layer = { __url: url, __options: options || null };
		layer.addTo = function () { put(layer); return layer; };
		// Base imagery is created with no options object worth recording.
		if (options && options.maxNativeZoom !== undefined) {
			calls.tiles.push({ url: url, options: options });
		}
		return layer;
	},
};

// A jQuery thin enough for this page: chainable, records `html()`, and every
// `find` answers a node that behaves the same way.
function $node(selector) {
	const node = {
		__selector: selector,
		0: { getBoundingClientRect: function () { return { width: 900, height: 620 }; } },
		length: 1,
		find: function (inner) { return $node(selector + " " + inner); },
		html: function (value) { if (value !== undefined) { calls.html[selector] = String(value); } return node; },
		css: function () { return node; },
		text: function () { return node; },
		append: function () { return node; },
		after: function () { return node; },
		appendTo: function () { return node; },
		remove: function () { return node; },
		show: function () { return node; },
		hide: function () { return node; },
		empty: function () { return node; },
		on: function () { return node; },
		val: function () { return ""; },
		first: function () { return node; },
	};
	return node;
}

// FRAPPE CREATES THE OBJECT BEFORE IT LOADS THE SCRIPT, and the script assigns
// handlers onto it. Starting from `{}` here would fail with "Cannot set
// properties of undefined", which is a fact about this harness rather than about
// the page.
const pages = { "farm-overview": {} };
const sandbox = {
	console: console, setTimeout: setTimeout, clearTimeout: clearTimeout,
	Promise: Promise, Object: Object, Array: Array, String: String, Number: Number,
	JSON: JSON, Map: Map, Math: Math, parseFloat: parseFloat, isNaN: isNaN,
	$: $node,
	window: null,
	__: function (text, args) {
		let out = String(text);
		(args || []).forEach(function (value, index) { out = out.split("{" + index + "}").join(String(value)); });
		return out;
	},
	document: { querySelector: function () { return null; }, head: { appendChild: function () {} } },
	ResizeObserver: function (cb) {
		return { observe: function () { cb(); }, disconnect: function () {} };
	},
	frappe: {
		pages: pages,
		provide: function () {},
		render_template: function () { return "<div class='fo-wrap'></div>"; },
		ui: {
			make_app_page: function () {
				return {
					body: $node(".page-body"),
					set_indicator: function () {},
					set_primary_action: function () {},
					add_menu_item: function () {},
					add_inner_button: function () {},
					clear_primary_action: function () {},
				};
			},
		},
		call: function () { return Promise.resolve({ message: PAYLOAD }); },
		set_route: function () {},
		msgprint: function () {},
	},
	erpnext_mcp: {},
};
sandbox.window = sandbox;
vm.createContext(sandbox);

// The page fetches the widget itself; the harness supplies it directly, which is
// what `load_widget()` resolves to on a Desk that has already cached the asset.
sandbox.erpnext_mcp.geo_map = {
	MAX_FIT_ZOOM: 18,
	POINT_ZOOM: 16,
	HOME_VIEW: { centre: [45.6, -121.1], zoom: 10 },
	load_leaflet: function () { return Promise.resolve(L); },
	add_base_layers: function (lib, map, overlays) {
		calls.control = Object.keys(overlays || {});
		return {
			control: {
				addOverlay: function (layer, label) {
					in_control.add(layer);
					control_order.push(layer);
					checked.set(layer, present.has(layer));
					calls.overlays.push(label);
				},
			},
		};
	},
};

vm.runInContext(fs.readFileSync(SCRIPT, "utf8"), sandbox, { filename: SCRIPT });

const wrapper = {};
pages["farm-overview"].on_page_load(wrapper);

function snapshot() {
	return {
		on_map: Array.from(present).filter(function (layer) { return layer.__url && calls.tiles.some(function (t) { return t.url === layer.__url; }); }).map(tile_key),
		ticked: control_order.filter(function (layer) { return checked.get(layer); }).map(tile_key),
		terrain_legend: calls.html[Object.keys(calls.html).filter(function (key) { return key.indexOf("fo-terrain-legend") >= 0; })[0]] || "",
	};
}

// `__actions`: ["click", key] is a person ticking or unticking that box. Each is
// followed by a pause, so anything the page deferred has run before the snapshot.
function act(done) {
	const actions = (PAYLOAD.__actions || []).slice();
	(function next() {
		const action = actions.shift();
		if (!action) { done(); return; }
		const layer = Array.from(in_control).find(function (entry) { return tile_key(entry) === action[1]; });
		if (!layer) { calls.after.push({ missing: action[1] }); next(); return; }
		click(layer);
		setTimeout(function () { calls.after.push(snapshot()); next(); }, 20);
	})();
}

function report(before, after_reload) {
	const summary = {};
	Object.keys(calls.groups).forEach(function (id) {
		summary[id] = calls.groups[id].map(function (entry) {
			return {
				kind: entry.__kind,
				point: entry.__detail.point || null,
				content: entry.__detail.content || null,
				icon: (entry.__detail.options && entry.__detail.options.icon) || null,
				popup: entry.__popup || null,
			};
		});
	});
	console.log(JSON.stringify({
		groups: summary,
		tooltips: calls.tooltips.map(function (t) { return { content: t.content, point: t.point }; }),
		control: calls.control,
		fitted: calls.fitted,
		legend: Object.keys(calls.html)
			.filter(function (key) { return key.indexOf("fo-legend") >= 0; })
			.map(function (key) { return calls.html[key]; })
			.join(""),
		html_keys: Object.keys(calls.html),
		tiles: calls.tiles,
		overlays: calls.overlays,
		terrain_before: before,
		after: calls.after,
		notices: Object.keys(calls.html)
			.filter(function (key) { return key.indexOf("fo-notices") >= 0; })
			.map(function (key) { return calls.html[key]; })
			.join(""),
		after_reload: after_reload,
	}));
}

function finish(before) {
	if (!PAYLOAD.__reload) {
		report(before, null);
		return;
	}
	// What on_page_show and every entity change do: re-read and rebuild the map.
	calls.tiles = [];
	calls.overlays = [];
	wrapper.farm_overview.reload();
	setTimeout(function () { report(before, snapshot()); }, 80);
}

setTimeout(function () {
	const before = snapshot();
	act(function () { finish(before); });
}, 80);
"""


def drive(payload: dict) -> dict:
	"""Run the real page script under a stubbed Desk and report what it drew."""
	with tempfile.TemporaryDirectory() as work:
		harness = Path(work) / "harness.js"
		harness.write_text(HARNESS, encoding="utf-8")
		data = Path(work) / "payload.json"
		data.write_text(json.dumps(payload), encoding="utf-8")
		out = subprocess.run(
			["node", str(harness), str(PAGE), str(data)],
			capture_output=True,
			text=True,
			timeout=60,
		)
	if out.returncode != 0:
		raise AssertionError(f"harness failed:\n{out.stdout}\n{out.stderr}")
	return json.loads(out.stdout.strip().splitlines()[-1])


@unittest.skipUnless(shutil.which("node"), "node is not installed on this machine")
class ThePageDraws(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.drawn = drive(PAYLOAD)

	def group_for(self, kind_and_point):
		"""The layer group holding a given entry, by (kind, point)."""
		for group, entries in self.drawn["groups"].items():
			for entry in entries:
				if (entry["kind"], tuple(entry["point"] or [])) == kind_and_point:
					return group, entries
		raise AssertionError(f"nothing drawn matching {kind_and_point}: {self.drawn['groups']}")

	# ── the drawing order that a screenshot cannot show ──────────────────
	def test_the_critical_task_is_added_last_and_therefore_sits_on_top(self):
		"""THE BUG THIS FILE EXISTS FOR. Three jobs share one centroid exactly.
		Leaflet draws later markers over earlier ones, so the LAST one added is
		the one a finger lands on — and it has to be the Critical job. The server
		answers worst-first because the fallback table wants that order, so the
		script walks it backwards; iterating straight through buries the urgent
		pin under the tidying job, silently, on a map that looks perfect."""
		markers = [
			entry
			for entries in self.drawn["groups"].values()
			for entry in entries
			if entry["kind"] == "marker" and entry["popup"] and "farm-task" in entry["popup"]
		]
		order = [
			"T-CRIT" if "T-CRIT" in entry["popup"] else "T-HIGH" if "T-HIGH" in entry["popup"] else "T-LOW"
			for entry in markers
		]
		self.assertEqual(order, ["T-LOW", "T-HIGH", "T-CRIT"])

	# ── one group per register ───────────────────────────────────────────
	def test_every_register_gets_its_own_layer_group(self):
		"""Six groups: three registers plus structures, assets and tasks. They
		are what the layer control toggles, so a shape added straight to the map
		would be one nobody can switch off."""
		self.assertEqual(len(self.drawn["groups"]), 6)

	def test_the_two_blocks_share_one_group_and_the_parcel_does_not(self):
		"""Three polygons, two groups. Drawn straight onto the map they would all
		land in one place and none of them could be switched off."""
		# The parcel and the fields are drawn into different groups, which is the
		# whole point of having them.
		groups_with_shapes = [
			group
			for group, rows in self.drawn["groups"].items()
			if any(row["kind"] == "geojson" for row in rows)
		]
		self.assertEqual(len(groups_with_shapes), 2)

	def test_the_asset_pin_is_a_lettered_badge_and_not_a_default_marker(self):
		"""`L.marker` with no icon reaches for a sprite at a path relative to the
		CDN stylesheet — one more request that can fail on a page whose whole
		posture is that a missing library must not look like a missing farm."""
		badges = [
			entry
			for entries in self.drawn["groups"].values()
			for entry in entries
			if entry["kind"] == "marker" and entry["icon"] and "fo-asset-icon" in str(entry["icon"])
		]
		self.assertEqual(len(badges), 1)
		self.assertIn("W", badges[0]["icon"]["html"])

	# ── the ticker ───────────────────────────────────────────────────────
	def test_only_the_block_with_a_ticker_is_labelled_on_the_ground(self):
		"""Two Fields, one of which has been given a ticker. A permanent tooltip
		on every polygon would be a map you cannot read; one on none of them
		would leave the label a picker recognises a block by off the map."""
		self.assertEqual([tip["content"] for tip in self.drawn["tooltips"]], ["YC3"])
		self.assertEqual(self.drawn["tooltips"][0]["point"], [45.6015, -121.178])

	# ── the layer control ────────────────────────────────────────────────
	def test_the_control_is_offered_the_layers_that_drew_something(self):
		self.assertEqual(
			self.drawn["control"],
			["Parcels (1)", "Fields (2)", "Structures (1)", "Assets (1)", "Open tasks (3)"],
		)

	def test_an_empty_register_is_not_offered_as_a_toggle(self):
		"""Irrigation zones drew nothing. A control entry that switches nothing
		is a control nobody trusts."""
		self.assertNotIn("Irrigation zones (0)", self.drawn["control"])

	def test_the_count_rides_in_the_label(self):
		"""It answers "did this layer draw nothing, or is it switched off"
		without unticking anything — which is the question somebody asks the
		moment a layer looks empty."""
		self.assertIn("Fields (2)", self.drawn["control"])

	# ── the popups ───────────────────────────────────────────────────────
	def test_the_task_popup_links_to_the_job_and_to_the_ground(self):
		"""Both, because they answer different questions. A pin at a centroid has
		nothing written on it, so "which block is this" is the one a map is
		uniquely bad at answering."""
		popup = next(
			entry["popup"]
			for entries in self.drawn["groups"].values()
			for entry in entries
			if entry["popup"] and "T-CRIT" in entry["popup"]
		)
		self.assertIn("/app/farm-task/T-CRIT", popup)
		self.assertIn("/app/field/YC3", popup)

	def test_an_unclaimed_task_says_so_rather_than_leaving_a_blank(self):
		popup = next(
			entry["popup"]
			for entries in self.drawn["groups"].values()
			for entry in entries
			if entry["popup"] and "T-CRIT" in entry["popup"]
		)
		self.assertIn("Nobody has claimed it", popup)

	def test_the_asset_popup_carries_the_scan_line(self):
		popup = next(
			entry["popup"]
			for entries in self.drawn["groups"].values()
			for entry in entries
			if entry["popup"] and "40-5-MPH" in entry["popup"]
		)
		self.assertIn("Last scanned 2026-09-01 08:00:00", popup)
		self.assertIn("Running", popup)

	def test_a_tag_nobody_has_ever_scanned_says_that_instead_of_nothing(self):
		""" "Never scanned" is the most actionable thing this popup can say — a
		blank where it should be reads as a popup that failed to load."""
		payload = json.loads(json.dumps(PAYLOAD))
		payload["assets"][0]["last_scan_at"] = None
		payload["assets"][0]["last_scan_by"] = None
		drawn = drive(payload)
		popup = next(
			entry["popup"]
			for entries in drawn["groups"].values()
			for entry in entries
			if entry["popup"] and "40-5-MPH" in entry["popup"]
		)
		self.assertIn("Never scanned", popup)

	# ── the legend ───────────────────────────────────────────────────────
	def test_the_legend_keys_the_assets_by_type_and_the_tasks_by_urgency(self):
		legend = self.drawn["legend"]
		self.assertIn("Wind machine", legend)
		for urgency in ("Critical", "High", "Low"):
			with self.subTest(urgency=urgency):
				self.assertIn(urgency, legend)

	def test_an_urgency_with_no_job_in_it_is_not_in_the_key(self):
		"""A key listing a colour nothing on screen is using is a key somebody
		has to check against the map rather than read."""
		self.assertNotIn("Normal", self.drawn["legend"])


# ── v0.185.0: the slope layers ──────────────────────────────────────────────
DESK = "/api/method/erpnext_mcp.farm_overview.terrain_tile"
BOUNDS = {"west": -121.19, "south": 45.59, "east": -121.17, "north": 45.61}

ASPECT = {
	"key": "slope_aspect",
	"label": "Slope aspect",
	"detail": "Which way the ground faces.",
	"tile_url_template": f"{DESK}?layer=slope_aspect&z={{z}}&x={{x}}&y={{y}}",
	"tile_size": 256,
	"min_zoom": 11,
	"max_zoom": 17,
	"legend": [
		{"aspect": "N", "bearing": 0, "color": "#285fcd"},
		{"aspect": "S", "bearing": 180, "color": "#cd232d"},
	],
	"flat_slope_degrees": 2.0,
	"flat_color": "#969696",
	"source": "USGS 3DEP 1/3 arc-second DEM (The National Map)",
	"available": True,
	"bounds": BOUNDS,
}
GRADE = {
	"key": "slope_grade",
	"label": "Slope grade",
	"detail": "How steep the ground is.",
	"tile_url_template": f"{DESK}?layer=slope_grade&z={{z}}&x={{x}}&y={{y}}",
	"tile_size": 256,
	"min_zoom": 11,
	"max_zoom": 17,
	"legend": [
		{"label": "Gentle", "min_degrees": 0.0, "max_degrees": 8.0, "color": "#2e9e48"},
		{"label": "Very steep", "min_degrees": 25.0, "max_degrees": None, "color": "#d02428"},
	],
	"source": "USGS 3DEP 1/3 arc-second DEM (The National Map)",
	"available": True,
	"bounds": BOUNDS,
}


def with_terrain(*specs, actions=(), reload=False):
	payload = json.loads(json.dumps(PAYLOAD))
	payload["terrain_layers"] = [json.loads(json.dumps(spec)) for spec in specs]
	payload["__actions"] = [list(action) for action in actions]
	payload["__reload"] = reload
	return payload


@unittest.skipUnless(shutil.which("node"), "node is not installed on this machine")
class TheSlopeLayers(unittest.TestCase):
	def test_both_are_offered_in_the_layer_control_and_neither_is_on(self):
		"""OFF BY DEFAULT: a raster over the whole farm hides the imagery a
		boundary check needs."""
		drawn = drive(with_terrain(ASPECT, GRADE))
		self.assertEqual(drawn["overlays"], ["Slope aspect", "Slope grade"])
		self.assertEqual(drawn["terrain_before"]["on_map"], [])
		self.assertEqual(drawn["terrain_before"]["ticked"], [])
		self.assertEqual(drawn["terrain_before"]["terrain_legend"], "")

	def test_the_register_toggles_are_unchanged(self):
		"""The rasters are added to the control, not to the register dict the
		control was built from — so the register list is what it always was."""
		drawn = drive(with_terrain(ASPECT, GRADE))
		self.assertEqual(
			drawn["control"],
			["Parcels (1)", "Fields (2)", "Structures (1)", "Assets (1)", "Open tasks (3)"],
		)

	def test_the_tiles_come_from_the_url_the_server_sent(self):
		"""The page holds no tile URL of its own — see ThePageOnDisk — and
		enlarges zoom 17 rather than asking for tiles that do not exist."""
		drawn = drive(with_terrain(ASPECT, GRADE))
		by_url = {tile["url"]: tile["options"] for tile in drawn["tiles"]}
		self.assertEqual(sorted(by_url), sorted([ASPECT["tile_url_template"], GRADE["tile_url_template"]]))
		options = by_url[ASPECT["tile_url_template"]]
		self.assertEqual(options["minZoom"], 11)
		self.assertEqual(options["maxNativeZoom"], 17)
		self.assertEqual(options["bounds"]["__bounds"], [[45.59, -121.19], [45.61, -121.17]])

	def test_switching_aspect_on_draws_the_compass_key_and_the_flat_grey(self):
		drawn = drive(with_terrain(ASPECT, GRADE, actions=[("click", "slope_aspect")]))
		(after,) = drawn["after"]
		self.assertEqual(after["on_map"], ["slope_aspect"])
		legend = after["terrain_legend"]
		self.assertIn("N-facing", legend)
		self.assertIn("#cd232d", legend)
		self.assertIn("Flat (under 2°)", legend)
		self.assertIn("#969696", legend)
		self.assertIn("USGS 3DEP", legend)

	def test_one_at_a_time_turning_grade_on_takes_aspect_off(self):
		"""Both palettes colour every cell of the same ground and both use red."""
		drawn = drive(
			with_terrain(ASPECT, GRADE, actions=[("click", "slope_aspect"), ("click", "slope_grade")])
		)
		_first, second = drawn["after"]
		self.assertEqual(second["on_map"], ["slope_grade"])
		# THE CHECKBOXES AGREE WITH THE MAP. Found on the live bench, 2026-09-26:
		# removing aspect inside the click let Leaflet's own walk re-add it, and
		# the control showed both ticked over a map drawing aspect.
		self.assertEqual(second["ticked"], ["slope_grade"])
		self.assertIn("Very steep (25° and over)", second["terrain_legend"])
		self.assertIn("Gentle (0–8°)", second["terrain_legend"])
		self.assertNotIn("N-facing", second["terrain_legend"])

	def test_switching_it_off_clears_the_key(self):
		drawn = drive(
			with_terrain(ASPECT, GRADE, actions=[("click", "slope_grade"), ("click", "slope_grade")])
		)
		self.assertEqual(drawn["after"][-1], {"on_map": [], "ticked": [], "terrain_legend": ""})

	def test_a_layer_switched_on_survives_the_page_re_reading(self):
		"""The page rebuilds its map on every return and every entity change. A
		slope layer that vanished each time would be switched off by picking an
		entity, which nobody would guess."""
		drawn = drive(with_terrain(ASPECT, GRADE, actions=[("click", "slope_grade")], reload=True))
		self.assertEqual(drawn["after_reload"]["on_map"], ["slope_grade"])
		self.assertEqual(drawn["after_reload"]["ticked"], ["slope_grade"])
		self.assertIn("Very steep", drawn["after_reload"]["terrain_legend"])

	def test_a_layer_switched_off_stays_off_after_the_re_read(self):
		drawn = drive(
			with_terrain(
				ASPECT, GRADE, actions=[("click", "slope_grade"), ("click", "slope_grade")], reload=True
			)
		)
		self.assertEqual(drawn["after_reload"], {"on_map": [], "ticked": [], "terrain_legend": ""})

	def test_an_unbuilt_layer_is_not_offered_and_the_notice_says_how_to_build_it(self):
		unbuilt = [
			{**spec, "available": False, "reason": "Run build_slope_aspect_layer once."}
			for spec in (ASPECT, GRADE)
		]
		for spec in unbuilt:
			spec.pop("bounds")
		drawn = drive(with_terrain(*unbuilt))
		self.assertEqual(drawn["overlays"], [])
		self.assertEqual(drawn["tiles"], [])
		self.assertIn("Slope aspect, Slope grade", drawn["notices"])
		self.assertIn("build_slope_aspect_layer", drawn["notices"])

	def test_a_built_site_carries_no_slope_notice(self):
		self.assertNotIn("slope", drive(with_terrain(ASPECT, GRADE))["notices"].lower())

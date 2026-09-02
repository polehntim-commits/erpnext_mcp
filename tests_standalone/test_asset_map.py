# SPDX-License-Identifier: MIT
"""The pin on an Asset Register form, and whether a finger can move it.

v0.145.0 made the map pin draggable for `Irrigation Valve` records and left
every other asset with the read-only marker v0.32.0 drew. v0.154.0 merged the
two scripts so the pin is draggable on the whole register, and this file is the
proof — because the only way a bench-less repo can check a Desk form script is
to execute it.

WHY THIS EXECUTES THE JAVASCRIPT INSTEAD OF GREPPING IT. A substring assertion
on a form script matches the whole file: `grep draggable` was already true of
the tree before this change, because `irrigation_valve_map.js` contained the
word and refused to run on a tractor. The question is not whether the string is
present, it is whether `refresh` on a Tractor ends with a draggable marker on
the map and a `dragend` handler that writes both fields — and that is a question
about control flow through two promise hops.

So `HARNESS` stubs the Desk — `frappe.ui.form.on`, `erpnext_mcp.geo_map`, a
Leaflet whose marker records how it was constructed, and a `frm` whose
`set_value` records what it was asked to write — runs the real file under
`node:vm`, fires `refresh`, then fires `dragend` and reports what happened as
JSON. Every claim below is read off that.

THE HARNESS ALSO STUBS `geo_map.render`, WHICH THIS SCRIPT NO LONGER CALLS. That
is deliberate: it is the read-only path the pre-v0.154.0 file took for a
non-valve, and keeping it makes the harness fair to both versions, so the same
run against the parent commit is a real negative control rather than a crash.
Run against v0.153.0 the four behavioural tests below fail — old
`asset_register_map.js` on a Tractor reports `read_only_renders: 1` and no
marker; old `irrigation_valve_map.js` on a Tractor returns early and reports
nothing at all.

SKIPPED WITHOUT `node`, and a skip here is a statement about the machine. CI has
node; a bench does not need it, because this tests a file the browser runs.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "erpnext_mcp"
SCRIPT = APP_DIR / "public" / "js" / "asset_register_map.js"

#: Every value `Asset Register.asset_type` offers, read off the doctype rather
#: than listed here — so a fourteenth type added later is covered by these tests
#: without anybody remembering they exist.
def asset_types() -> list:
	payload = json.loads(
		(APP_DIR / "erpnext_mcp" / "doctype" / "asset_register" / "asset_register.json").read_text()
	)
	field = next(f for f in payload["fields"] if f["fieldname"] == "asset_type")
	return [line for line in str(field["options"]).split("\n") if line.strip()]


HARNESS = r"""// Drive the real form script under a stubbed Desk. Prints JSON on stdout.
const fs = require("fs");
const vm = require("vm");

const SCRIPT = process.argv[2];
const ASSET_TYPE = process.argv[3];
const HAS_GPS = process.argv[4] === "1";

const calls = { set_value: [], sections: [], markers: [], dragend_wired: 0, read_only_renders: 0 };

function el() {
	const node = { style: {}, className: "", innerHTML: "", textContent: "", children: [] };
	node.appendChild = function (child) { node.children.push(child); return child; };
	return node;
}

const marker = {
	_drag: null,
	addTo: function () { return marker; },
	bindPopup: function () { return marker; },
	setLatLng: function () { return marker; },
	getLatLng: function () { return { lat: 46.1234567891, lng: -119.9876543219 }; },
	on: function (event, cb) { if (event === "dragend") { calls.dragend_wired += 1; marker._drag = cb; } },
};

const L = {
	map: function () {
		return {
			setView: function () {}, fitBounds: function () {}, panTo: function () {},
			removeLayer: function () {}, invalidateSize: function () {},
			once: function () {}, addLayer: function () {},
		};
	},
	marker: function (point, options) { calls.markers.push({ point: point, options: options || {} }); return marker; },
	geoJSON: function () { return { addTo: function () { return this; }, bindPopup: function () { return this; }, getBounds: function () { return { pad: function () { return {}; }, extend: function () {} }; } }; },
	tileLayer: function () { return { addTo: function () {} }; },
};

const frm = {
	doc: {
		name: "MC-Tractor-07",
		asset_type: ASSET_TYPE,
		gps_latitude: HAS_GPS ? 46.2 : null,
		gps_longitude: HAS_GPS ? -119.2 : null,
		irrigation_zone: "",
	},
	dashboard: { add_section: function (w, title) { calls.sections.push(String(title)); }, show: function () {} },
	set_value: function (field, value) { calls.set_value.push([field, value]); frm.doc[field] = value; },
	$wrapper: { find: function () { return { first: function () { return { prepend: function () {} }; } }; } },
};

const handlers = {};
const sandbox = {
	console: console, setTimeout: setTimeout, Promise: Promise, parseFloat: parseFloat, Math: Math,
	$: function (x) { return x; },
	__: function (s) { return s; },
	document: { createElement: el, body: { contains: function () { return false; } } },
	frappe: {
		ui: { form: { on: function (doctype, map) { handlers.doctype = doctype; Object.assign(handlers, map); } } },
		utils: { escape_html: function (s) { return String(s); } },
	},
	erpnext_mcp: {
		geo_map: {
			MAX_FIT_ZOOM: 18,
			point_of: function (lat, lng) {
				if (lat === null || lat === undefined || lng === null || lng === undefined) return null;
				return [Number(lat), Number(lng)];
			},
			fetch_boundary: function () { return Promise.resolve(null); },
			load_leaflet: function () { return Promise.resolve(L); },
			add_base_layers: function () {},
			// The read-only path the pre-v0.154.0 script took for non-valves.
			render: function () { calls.read_only_renders += 1; },
		},
	},
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(SCRIPT, "utf8"), sandbox, { filename: SCRIPT });

if (typeof handlers.refresh !== "function") {
	console.log(JSON.stringify({ error: "no refresh handler registered", doctype: handlers.doctype || null }));
	process.exit(0);
}
handlers.refresh(frm);

// Two promise hops (zone, then leaflet) plus the setTimeout.
setTimeout(function () {
	if (marker._drag) { marker._drag(); }
	console.log(JSON.stringify({
		doctype: handlers.doctype,
		handlers: Object.keys(handlers).filter(function (k) { return typeof handlers[k] === "function"; }).sort(),
		markers: calls.markers,
		dragend_wired: calls.dragend_wired,
		set_value: calls.set_value,
		sections: calls.sections,
		read_only_renders: calls.read_only_renders,
	}));
}, 50);
"""


def drive(asset_type: str, has_gps: bool = True, script: Path = SCRIPT) -> dict:
	"""Run the real form script under a stubbed Desk and report what it did."""
	with tempfile.TemporaryDirectory() as work:
		harness = Path(work) / "harness.js"
		harness.write_text(HARNESS, encoding="utf-8")
		out = subprocess.run(
			["node", str(harness), str(script), asset_type, "1" if has_gps else "0"],
			capture_output=True,
			text=True,
			timeout=60,
		)
	if out.returncode != 0:
		raise AssertionError(f"harness failed for {asset_type!r}:\n{out.stderr}")
	return json.loads(out.stdout.strip().splitlines()[-1])


@unittest.skipUnless(shutil.which("node"), "node is not installed on this machine")
class ThePinIsDraggableOnEveryAsset(unittest.TestCase):
	"""THE CLAIM OF v0.154.0, ASSET TYPE BY ASSET TYPE."""

	def test_every_asset_type_gets_a_draggable_marker(self):
		"""Including the eleven that got a read-only one until this release. A
		scan records where the SCANNER stood, which is as wrong for a pump in a
		four-acre yard as it was for a valve on the wrong lateral."""
		for asset_type in asset_types():
			with self.subTest(asset_type=asset_type):
				result = drive(asset_type)
				self.assertEqual(len(result["markers"]), 1, "exactly one pin")
				self.assertTrue(
					result["markers"][0]["options"].get("draggable"),
					f"the pin on a {asset_type} form is not draggable",
				)

	def test_dragging_writes_both_coordinate_fields(self):
		"""The half that makes the pin worth dragging. `dragend` is fired by the
		harness and the values `set_value` was called with are read back."""
		for asset_type in asset_types():
			with self.subTest(asset_type=asset_type):
				written = dict(drive(asset_type)["set_value"])
				self.assertEqual(
					sorted(written), ["gps_latitude", "gps_longitude"], "both fields, not one"
				)
				self.assertAlmostEqual(written["gps_latitude"], 46.1234568, places=7)
				self.assertAlmostEqual(written["gps_longitude"], -119.9876543, places=7)

	def test_the_written_value_is_rounded_to_the_fields_own_precision(self):
		"""`gps_latitude` and `gps_longitude` are Float with `precision: "7"` on
		the doctype. Writing more than the column keeps means the pin and the
		field disagree the moment the form is saved."""
		written = dict(drive("Tractor")["set_value"])
		for field, value in written.items():
			with self.subTest(field=field):
				self.assertEqual(value, round(value, 7))
				self.assertLessEqual(len(str(value).split(".")[1]), 7)

	def test_no_asset_type_falls_through_to_the_read_only_renderer(self):
		"""THE NEGATIVE CONTROL, AND THE ONE THAT FAILS ON v0.153.0. The old
		`asset_register_map.js` answered a Tractor with `geo_map.render` — a
		marker nobody can move. The harness still stubs that call, so a
		regression to it is counted rather than crashing."""
		for asset_type in asset_types():
			with self.subTest(asset_type=asset_type):
				self.assertEqual(drive(asset_type)["read_only_renders"], 0)

	def test_the_valve_keeps_its_own_section_title(self):
		"""Merging the scripts must not rename the section on the form the farm
		actually uses. Everything else says "Asset Location", which is what this
		script's section was called before the merge."""
		self.assertEqual(drive("Irrigation Valve")["sections"], ["Valve Location"])
		self.assertEqual(drive("Tractor")["sections"], ["Asset Location"])

	def test_a_record_with_no_position_draws_no_pin_and_says_so(self):
		"""A pin at [0, 0] is an assertion that the tractor is in the Gulf of
		Guinea. Nothing is placed until somebody clicks the map."""
		result = drive("Tractor", has_gps=False)
		self.assertEqual(result["markers"], [])
		self.assertEqual(result["set_value"], [])

	def test_the_field_handlers_are_registered_for_the_doctype(self):
		"""Editing the numbers by hand has to move the pin the other way, or the
		two halves of the form disagree about where the asset is."""
		result = drive("Tractor")
		self.assertEqual(result["doctype"], "Asset Register")
		self.assertEqual(result["handlers"], ["gps_latitude", "gps_longitude", "refresh"])


class TheScriptsWereMerged(unittest.TestCase):
	"""Structure, asserted without node so it holds on every machine."""

	def test_the_valve_only_script_is_gone(self):
		"""It existed to do for valves what the register now does for everything.
		Leaving it on disk would leave a second `frappe.ui.form.on("Asset
		Register")` that re-registers every handler this one installs."""
		self.assertFalse((APP_DIR / "public" / "js" / "irrigation_valve_map.js").exists())

	def test_asset_register_has_one_map_script_again(self):
		from erpnext_mcp import hooks

		self.assertEqual(
			hooks.doctype_js["Asset Register"],
			["public/js/geo_map_widget.js", "public/js/asset_register_map.js"],
		)

	def test_asset_type_decides_the_title_and_nothing_else(self):
		"""THE SHAPE OF THE BUG THIS RELEASE FIXED, asserted as a property of the
		file rather than of one run: a form script that decides WHETHER TO DRAW by
		reading `asset_type`. There is exactly one reference left and it chooses
		the section's name — the old valve script had four, three of them guards
		that returned early.

		A count rather than a "does not contain" pair, because the strings to
		exclude are unbounded: `!== VALVE`, `!= "Irrigation Valve"`, an
		`indexOf`, a lookup table. One reference cannot be a guard and a title at
		the same time, and the line it sits on is asserted underneath.
		"""
		source = SCRIPT.read_text(encoding="utf-8")
		references = [line.strip() for line in source.splitlines() if "asset_type" in line]
		self.assertEqual(len(references), 1, f"asset_type is read more than once: {references}")
		self.assertIn("Valve Location", references[0])
		self.assertIn("Asset Location", references[0])

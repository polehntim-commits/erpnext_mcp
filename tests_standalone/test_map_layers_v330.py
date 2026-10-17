# SPDX-License-Identifier: MIT
"""One Layers button on every map — Desk, share link and phone. v0.330.0.

Tim: "where we have maps on the satellite / street view it may be a good idea to have the aspect and slope maps
available there as well", and then "we may need to have a way of changing what layer we are looking at". Proved here:

- a template carries the overlay its tasks' maps open on (`default_map_overlay`), set through
  `update_farm_task_template`, refused when it is not None / Slope / Aspect, and reaching the phone's task payload;
- the Desk's forms read the slope descriptors from one GET (`farm_overview.terrain_layers`);
- a share link's page gets the same descriptors with tile URLs under its own token, and its tiles are gated: a
  bad token is the same 404, a tile away from the job's blocks is the transparent tile, and an unbuilt layer says so;
- `map_layers.js` — run under node — normalises overlay names, reads GeoJSON outlines, judges coverage, opens on the
  remembered choice before the template's default, and flags the dozer bands; and it never writes `innerHTML` or a
  `style` attribute, which the share link's CSP would refuse.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import frappe

from erpnext_mcp import farm_overview, job_links, slope_aspect, task_templates
from erpnext_mcp.api import shape

from .fixtures import V12TestCase
from .harness import STORE
from .test_farmops_api import PREFIX
from .test_job_links import JobCase

APP = Path(__file__).resolve().parent.parent / "erpnext_mcp"
SWITCHER = APP / "public" / "js" / "map_layers.js"
WIDGET = APP / "public" / "js" / "geo_map_widget.js"
JOB_JS = APP / "public" / "job_page" / "job.js"


class TheTemplateSaysWhatTheMapOpensOn(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, allow_update_farm_task_template=1)
		task_templates.seed_farm_task_templates()
		STORE.commit()
		self.template = frappe.db.get_all("Farm Task Template", pluck="name", limit=1)[0]

	def test_the_spellings_a_person_types_all_land_on_one_value(self):
		for raw, want in (("slope", "Slope"), ("Slope", "Slope"), ("slope_grade", "Slope"), ("grade", "Slope"),
		                  ("aspect", "Aspect"), ("slope_aspect", "Aspect"), ("none", "None"), ("", "")):
			with self.subTest(raw=raw):
				self.assertEqual(task_templates.map_overlay_value(raw), want)
		with self.assertRaises(ValueError):
			task_templates.map_overlay_value("irrigation")

	def test_set_through_the_tool_read_back_and_cleared(self):
		self.tool_data("update_farm_task_template", {"template": self.template, "default_map_overlay": "slope"})
		STORE.commit()
		self.assertEqual(task_templates.default_map_overlay(self.template), "Slope")
		self.assertEqual(task_templates.describe(self.template)["default_map_overlay"], "Slope")
		self.tool_data("update_farm_task_template", {"template": self.template, "default_map_overlay": ""})
		STORE.commit()
		self.assertIsNone(task_templates.default_map_overlay(self.template))
		self.assertNotIn("default_map_overlay", task_templates.describe(self.template))

	def test_a_word_that_is_not_an_overlay_is_refused_and_nothing_changes(self):
		error = self.tool_error("update_farm_task_template", {"template": self.template, "default_map_overlay": "REI"})
		self.assertIn("None, Slope, Aspect", error)
		self.assertIsNone(task_templates.default_map_overlay(self.template))

	def test_the_phones_task_payload_carries_it_only_where_set(self):
		row = {"name": "FT-X", "task_name": "Burn the slash", "template": self.template, "state": "Available"}
		self.assertNotIn("default_map_overlay", shape.task(dict(row)))
		self.tool_data("update_farm_task_template", {"template": self.template, "default_map_overlay": "Slope"})
		STORE.commit()
		self.assertEqual(shape.task(dict(row))["default_map_overlay"], "Slope")


class TheDeskReadsOneDescriptorList(V12TestCase):
	def test_the_forms_get_what_the_overview_carries(self):
		self.configure(enabled=1)
		frappe.local.session = frappe._dict(user="Administrator", data=frappe._dict())
		listed = farm_overview.terrain_layers()
		self.assertEqual(sorted(spec["key"] for spec in listed), ["slope_aspect", "slope_grade"])
		for spec in listed:
			with self.subTest(key=spec["key"]):
				self.assertIn("erpnext_mcp.farm_overview.terrain_tile", spec["tile_url_template"])


class TheShareLinkHasItsOwnDoor(JobCase):
	def test_the_descriptors_point_at_this_links_tiles_and_name_no_company(self):
		_data, token = self.ready_and_link()
		answer = self.post(f"{PREFIX}/job/{token}/terrain", method="GET", credential=False)
		self.assertEqual(answer.status_code, 200)
		layers = json.loads(answer.get_data(as_text=True))["layers"]
		self.assertEqual(sorted(spec["key"] for spec in layers), ["slope_aspect", "slope_grade"])
		for spec in layers:
			with self.subTest(key=spec["key"]):
				self.assertTrue(spec["tile_url_template"].startswith(f"/farmops/api/job/{token}/tiles/{spec['key']}/"))
				self.assertNotIn("company", spec)
				self.assertFalse(spec["available"], "this site never built the layer")

	def test_tiles_are_gated_by_the_token_and_the_jobs_own_ground(self):
		_data, token = self.ready_and_link()
		x, y = slope_aspect.to_mercator(-121.2276, 45.5838)
		((tx, ty),) = slope_aspect.tiles_covering((x, y, x, y), 15)
		near = self.post(f"{PREFIX}/job/{token}/tiles/slope_grade/15/{tx}/{ty}.png", method="GET", credential=False)
		self.assertEqual(near.status_code, 404, "inside the job but never built — said, not drawn blank")
		far = self.post(f"{PREFIX}/job/{token}/tiles/slope_grade/15/{tx + 40}/{ty}.png", method="GET", credential=False)
		self.assertEqual(far.status_code, 200)
		self.assertEqual(far.get_data(), slope_aspect.TRANSPARENT_TILE, "away from the job's blocks: nothing to read")
		bad = self.post(f"{PREFIX}/job/{token}x/tiles/slope_grade/15/{tx}/{ty}.png", method="GET", credential=False)
		self.assertEqual(bad.status_code, 404)
		wrong = self.post(f"{PREFIX}/job/{token}/tiles/irrigation/15/{tx}/{ty}.png", method="GET", credential=False)
		self.assertEqual(wrong.status_code, 400)

	def test_the_page_loads_the_switcher_and_the_switcher_is_a_fixed_asset(self):
		_data, token = self.ready_and_link()
		page = self.post(f"{PREFIX}/job/{token}", method="GET", credential=False).get_data(as_text=True)
		self.assertLess(page.index("map_layers.js"), page.index("job.js"))
		asset = self.post(f"{PREFIX}/job-assets/map_layers.js", method="GET", credential=False)
		self.assertEqual(asset.status_code, 200)
		self.assertIn("FarmMapLayers", asset.get_data(as_text=True))

	def test_the_extent_is_the_jobs_blocks_padded(self):
		box = job_links.terrain_extent(self.job)
		self.assertIsNotNone(box)
		self.assertLess(box[0], box[2])
		self.assertLess(box[1], box[3])


class TheScriptsKeepTheCSP(unittest.TestCase):
	def test_the_switcher_never_writes_markup_or_a_style_attribute(self):
		source = SWITCHER.read_text()
		self.assertFalse("innerHTML" in source)
		self.assertFalse('setAttribute("style"' in source)
		self.assertFalse("createObjectURL" in source, "img-src has no blob: on the share link")

	def test_every_map_goes_through_one_button(self):
		widget = WIDGET.read_text()
		self.assertIn("map_layers.js", widget)
		self.assertIn("FarmMapLayers", widget)
		self.assertIn("FarmMapLayers", JOB_JS.read_text())


NODE = shutil.which("node")

HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const sandbox = { console: console };
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), sandbox);
const M = sandbox.FarmMapLayers;
const square = { type: "Polygon", coordinates: [[[-121.23, 45.58], [-121.22, 45.58], [-121.22, 45.59], [-121.23, 45.58]]] };
const bounds = { west: -121.24, south: 45.57, east: -121.18, north: 45.60 };
const grade = { key: "slope_grade", legend: [
  { level: 0, label: "Gentle", min_degrees: 0, max_degrees: 8, color: "#2fa84f" },
  { level: 1, label: "Moderate", min_degrees: 8, max_degrees: 15, color: "#e5c12e" },
  { level: 2, label: "Steep", min_degrees: 15, max_degrees: 25, color: "#f08c1e" },
  { level: 3, label: "Very steep", min_degrees: 25, max_degrees: null, color: "#d93a2f" }] };
const aspect = { key: "slope_aspect", flat_color: "#969696", legend: [
  { aspect: "S", bearing: 180, color: "#d93a2f" }, { aspect: "N", bearing: 0, color: "#2f6fdb" }] };
console.log(JSON.stringify({
  names: ["Slope", "slope_grade", "Aspect", "slope_aspect", "None", "", "REI"].map(M.normalise_overlay),
  rings: M.rings_of([{ geometry: square }, JSON.stringify({ type: "Feature", geometry: square })]),
  cover: [M.coverage(M.rings_of(square), bounds), M.coverage([[[46, -120]]], bounds), M.coverage([], bounds)],
  fresh: M.initial_state(null, null, { default_overlay: "Slope" }, ["satellite", "street"], null),
  remembered: M.initial_state({ base: "street", opacity: 0.05, legend: false }, "none", { default_overlay: "Slope" }, ["satellite", "street"], null),
  gone: M.initial_state(null, "slope_aspect", {}, ["satellite"], ["slope_grade"]),
  dozer: M.legend_rows(grade).filter(function (r) { return r.dozer; }).map(function (r) { return r.label; }),
  ranges: M.legend_rows(grade).map(function (r) { return r.range; }),
  aspect_rows: M.legend_rows(aspect).map(function (r) { return r.label; }),
  wheel: M.wheel_gradient(aspect),
  url: M.tile_url("/t/{z}/{x}/{y}.png", 15, 2, 3),
}));
"""


@unittest.skipUnless(NODE, "node is not installed on this machine")
class TheSwitcherRunsUnderNode(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
			handle.write(HARNESS)
			script = handle.name
		out = subprocess.run([NODE, script, str(SWITCHER)], capture_output=True, text=True, timeout=30, check=True)
		cls.r = json.loads(out.stdout)

	def test_overlay_names(self):
		self.assertEqual(self.r["names"], ["slope_grade", "slope_grade", "slope_aspect", "slope_aspect", "none", "none", "none"])

	def test_outlines_from_geometry_entries_and_strings(self):
		self.assertEqual(len(self.r["rings"]), 2)
		self.assertEqual(self.r["rings"][0][0], [45.58, -121.23], "lat, lng — GeoJSON is lng, lat")

	def test_coverage(self):
		self.assertEqual(self.r["cover"], ["full", "none", "unknown"])

	def test_the_templates_default_opens_a_fresh_map(self):
		self.assertEqual(self.r["fresh"], {"base": "satellite", "overlay": "slope_grade", "opacity": 1, "legend": True})

	def test_a_remembered_choice_beats_the_default_and_opacity_is_floored(self):
		self.assertEqual(self.r["remembered"], {"base": "street", "overlay": "none", "opacity": 0.2, "legend": False})

	def test_a_remembered_layer_the_farm_no_longer_serves_falls_back_to_none(self):
		self.assertEqual(self.r["gone"]["overlay"], "none")

	def test_steep_and_very_steep_are_the_dozer_bands(self):
		self.assertEqual(self.r["dozer"], ["Steep", "Very steep"])
		self.assertEqual(self.r["ranges"], ["0–8°", "8–15°", "15–25°", "25°+"])

	def test_aspect_legend_in_bearing_order_with_flat(self):
		self.assertEqual(self.r["aspect_rows"], ["N-facing", "S-facing", "Flat"])
		self.assertEqual(self.r["wheel"], "conic-gradient(#2f6fdb 0deg, #d93a2f 180deg, #2f6fdb 360deg)")

	def test_tile_url(self):
		self.assertEqual(self.r["url"], "/t/15/2/3.png")


if __name__ == "__main__":  # pragma: no cover
	unittest.main()

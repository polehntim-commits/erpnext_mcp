# SPDX-License-Identifier: MIT
"""v0.150.0 — a building drawn as a building, on one map with everything else.

TWO THINGS, AND THEY ARE HALVES OF ONE.

`set_asset_boundary` gives an asset a FOOTPRINT — the outline a shed, a pump
house, a cabin or a cold store occupies — through the same `geo.derive` that
computes Field, Parcel and Irrigation Zone boundaries, into the same six
columns. A seventh shape stored a seventh way would need its own reader in
every consumer.

`list_field_boundaries(include_assets=true)` then hands the map blocks, parcels,
zones, valves, equipment AND buildings in one answer, each row saying whether it
draws as a pin or an outline. That per-row `geometry` is the whole reason this
is one map instead of four: a client draws a marker or a shape without knowing
this app's asset vocabulary.

THE FAILURE THESE GUARD AGAINST IS PLOTTING NOTHING AS SOMETHING. A Frappe Float
is NOT NULL DEFAULT 0, so an asset nobody took a fix on reads exactly 0.0 —
indistinguishable from a reading. Null Island is 1,600 km off the coast of
Ghana. An asset with neither a fix nor an outline is left OFF the map and
counted, because a map quietly missing half the valves is worse than one that
says so, and a map showing the farm's whole register in the Gulf of Guinea is
worse than both.
"""

import json
import unittest

from erpnext_mcp import geo
from erpnext_mcp.api import mobile as mobile_api

from .fixtures import MAIN
from .harness import STORE, add_field, frappe, register_doctype
from .test_asset_mirror import ALL_ON, YARD, MirrorTestCase
from .test_wave2_mobile_surface import ON, WORKER, Wave2TestCase

#: A 40x60-foot shed, which is about 0.055 acres — small enough that a bug
#: inflating the area is obvious rather than plausible.
SHED = {
	"type": "Polygon",
	"coordinates": [
		[
			[-121.1800, 45.6000],
			[-121.17985, 45.6000],
			[-121.17985, 45.60011],
			[-121.1800, 45.60011],
			[-121.1800, 45.6000],
		]
	],
}

#: A trace with two vertices swapped — the bow tie a hand-drawn walk produces.
#: It has an area a computer will happily report and a containment test nobody
#: can trust, which is why it is refused rather than warned about.
BOW_TIE = {
	"type": "Polygon",
	"coordinates": [
		[
			[-121.1800, 45.6000],
			[-121.17985, 45.60011],
			[-121.17985, 45.6000],
			[-121.1800, 45.60011],
			[-121.1800, 45.6000],
		]
	],
}

FOOTPRINT_ON = dict(ALL_ON, allow_set_asset_boundary=1)


@unittest.skipUnless(geo.available(), "needs shapely>=2.0 and h3>=4.0.0")
class FootprintTestCase(MirrorTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, mirror_assets_to_erpnext=1, **FOOTPRINT_ON)

	def a_shed(self, name="MC-Shed-01", **kw):
		payload = {"name": name, "asset_type": "Storage", "company": MAIN}
		payload.update(kw)
		return self.tool_data("register_asset", payload)

	def trace(self, name="MC-Shed-01", geometry=None, **kw):
		payload = {"asset_name": name, "boundary_geojson": geometry or SHED}
		payload.update(kw)
		return self.tool_data("set_asset_boundary", payload)

	def stored(self, name="MC-Shed-01"):
		return next(r for r in STORE.rows("Asset Register") if r["name"] == name)


# ── the outline itself ──────────────────────────────────────────────────────
class TheFootprint(FootprintTestCase):
	def test_it_stores_the_polygon_and_everything_derived_from_it(self):
		self.a_shed()
		data = self.trace()
		row = self.stored()
		self.assertTrue(data["changed"])
		self.assertEqual(json.loads(row["boundary_geojson"])["type"], "Polygon")
		for derived in (
			"boundary_centroid_lat",
			"boundary_centroid_lon",
			"boundary_bbox_geojson",
			"h3_cells",
			"area_computed_acres",
		):
			with self.subTest(column=derived):
				self.assertTrue(row.get(derived), f"{derived} was not computed")

	def test_a_shed_is_a_fraction_of_an_acre(self):
		"""The area is real and not a shape the size of a county. A bug that
		read the coordinates as something other than degrees shows up here as a
		number nobody could mistake for a building."""
		self.a_shed()
		acres = self.trace()["area_computed_acres"]
		self.assertGreater(acres, 0.01)
		self.assertLess(acres, 0.2)

	def test_a_self_intersecting_trace_is_refused(self):
		"""A bow tie has an area a computer will report and a containment test
		nobody can trust. It is what a walk with two vertices swapped produces."""
		self.a_shed()
		error = self.tool_error(
			"set_asset_boundary", {"asset_name": "MC-Shed-01", "boundary_geojson": BOW_TIE}
		)
		self.assertIn("valid polygon", error)
		self.assertFalse(self.stored().get("boundary_geojson"))

	def test_a_dry_run_computes_everything_and_writes_nothing(self):
		self.a_shed()
		data = self.trace(dry_run=True)
		self.assertFalse(data["changed"])
		self.assertTrue(data["area_computed_acres"])
		self.assertFalse(self.stored().get("boundary_geojson"))

	def test_an_outline_on_a_valve_is_warned_about_and_stored(self):
		"""NOT REFUSED. Guessing which asset types a farm may trace is how a
		generator pad becomes unrecordable — but a polygon on a tractor is
		almost certainly the wrong asset, and saying nothing helps nobody."""
		self.register("MC-Valve-05", "Irrigation Valve")
		data = self.trace("MC-Valve-05")
		self.assertTrue(data["changed"])
		self.assertTrue(any("position rather than a footprint" in w for w in data["warnings"]))

	def test_a_shed_gets_no_such_warning(self):
		"""The negative control for the clause above: a warning on everything is
		a warning on nothing."""
		self.a_shed()
		data = self.trace()
		self.assertFalse(any("position rather than a footprint" in w for w in data["warnings"]))

	def test_a_pin_outside_the_outline_is_reported_not_refused(self):
		"""A fix taken from across the yard is a real thing. Refusing it makes
		the building unrecordable; saying nothing leaves a pin and an outline
		disagreeing on a map with nobody told which to believe."""
		self.a_shed(gps_latitude=45.7000, gps_longitude=-121.2000)
		data = self.trace()
		self.assertIs(data["recorded_position_inside_boundary"], False)
		self.assertTrue(any("outside the outline" in w for w in data["warnings"]))
		self.assertTrue(data["changed"])

	def test_a_pin_inside_the_outline_says_so_and_warns_about_nothing(self):
		self.a_shed(gps_latitude=45.60005, gps_longitude=-121.17992)
		data = self.trace()
		self.assertIs(data["recorded_position_inside_boundary"], True)
		self.assertFalse(any("outside the outline" in w for w in data["warnings"]))

	def test_an_asset_with_no_fix_checks_nothing_rather_than_failing(self):
		self.a_shed()
		self.assertIsNone(self.trace()["recorded_position_inside_boundary"])


# ── the outline reaches the books ───────────────────────────────────────────
class TheFootprintOnTheAsset(FootprintTestCase):
	def test_it_travels_to_the_erpnext_asset(self):
		self.a_shed(purchase_value=8000, acquired_on="2026-03-01")
		data = self.trace()
		self.assertTrue(data["erpnext_asset"])
		stored = self.asset(data["erpnext_asset"])["boundary_geojson"]
		self.assertEqual(json.loads(stored)["type"], "Polygon")

	def test_a_shed_not_on_the_books_still_keeps_its_outline(self):
		"""No purchase value, so no Asset — and the footprint is a fact about the
		ground either way. The tag keeps it and reports that nothing mirrored."""
		self.a_shed()
		data = self.trace()
		self.assertIsNone(data["erpnext_asset"])
		self.assertTrue(self.stored()["boundary_geojson"])


# ── one map, everything on it ───────────────────────────────────────────────
@unittest.skipUnless(geo.available(), "needs shapely>=2.0 and h3>=4.0.0")
class TheUnifiedMap(Wave2TestCase):
	"""`list_field_boundaries` is a MOBILE route behind an enrolled credential,
	so these run as a picker rather than through `tool_data` — the map every
	handset opens first is the one this layer has to appear on."""

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, mirror_assets_to_erpnext=1, **dict(ON, **FOOTPRINT_ON))
		register_doctype("Location", [{"fieldname": "name"}])
		STORE.seed("Location", [{"name": YARD}])
		for column, kind in (
			("asset_register", "Link"),
			("farm_asset_type", "Data"),
			("asset_register_synced_at", "Datetime"),
			("gps_latitude", "Float"),
			("gps_longitude", "Float"),
			("boundary_geojson", "Long Text"),
		):
			add_field("Asset", column, kind)

	def register(self, name, asset_type, **kw):
		return self.tool_data(
			"register_asset", {"name": name, "asset_type": asset_type, "company": MAIN, **kw}
		)

	def a_shed(self, name="MC-Shed-01", **kw):
		return self.register(name, "Storage", **kw)

	def trace(self, name="MC-Shed-01", geometry=None, **kw):
		return self.tool_data(
			"set_asset_boundary",
			{"asset_name": name, "boundary_geojson": geometry or SHED, **kw},
		)

	def read(self, **kw):
		# The writes above go through `tool_data`, which is the MCP endpoint and
		# runs as the system user; the READ is a mobile route and has to be a
		# picker holding an enrolled credential. Switching here rather than in
		# setUp is what `test_field_boundaries_route` does and for the same
		# reason: the two surfaces are different callers.
		self.be(WORKER)
		payload = {"company": MAIN, "include_assets": True, "include_overlays": False}
		payload.update(kw)
		return mobile_api.list_field_boundaries(**payload)

	def test_assets_are_opt_in(self):
		"""Five hundred valves is five hundred rows a caller drawing only blocks
		has no use for — the same reason parcels are opt in."""
		self.a_shed(gps_latitude=45.60, gps_longitude=-121.18)
		self.be(WORKER)
		data = mobile_api.list_field_boundaries(company=MAIN, include_overlays=False)
		self.assertEqual(data["assets"], [])
		self.assertFalse(data["include_assets"])

	def test_equipment_comes_back_as_a_pin(self):
		self.register("MC-Tractor-01", "Tractor", gps_latitude=45.60, gps_longitude=-121.18)
		row = next(r for r in self.read()["assets"] if r["name"] == "MC-Tractor-01")
		self.assertEqual(row["geometry"], "point")
		self.assertAlmostEqual(row["gps_latitude"], 45.60, places=5)
		self.assertIsNone(row["boundary_geojson"])

	def test_a_building_comes_back_as_an_outline(self):
		self.a_shed(gps_latitude=45.60005, gps_longitude=-121.17992)
		self.trace()
		row = next(r for r in self.read()["assets"] if r["name"] == "MC-Shed-01")
		self.assertEqual(row["geometry"], "polygon")
		self.assertEqual(json.loads(row["boundary_geojson"])["type"], "Polygon")

	def test_a_building_with_no_outline_yet_falls_back_to_its_pin(self):
		"""Rather than vanishing off the map. Nobody has traced this shed yet and
		it is still a place a dispatcher sends somebody."""
		self.a_shed(gps_latitude=45.60, gps_longitude=-121.18)
		row = next(r for r in self.read()["assets"] if r["name"] == "MC-Shed-01")
		self.assertEqual(row["geometry"], "point")

	def test_everything_is_on_the_one_answer(self):
		"""THE POINT OF THE FEATURE. Ground and the things standing on it, in one
		call — a dispatcher deciding where to send somebody is looking for all of
		them at once."""
		self.register("MC-Tractor-01", "Tractor", gps_latitude=45.60, gps_longitude=-121.18)
		self.a_shed(gps_latitude=45.60005, gps_longitude=-121.17992)
		self.trace()
		data = self.read()
		self.assertEqual(data["asset_count"], 2)
		self.assertEqual(data["asset_polygon_count"], 1)
		self.assertIn("fields", data)
		self.assertIn("parcels", data)

	def test_an_asset_with_nowhere_to_be_drawn_is_left_off_and_counted(self):
		"""A Frappe Float is NOT NULL DEFAULT 0, so an asset nobody took a fix on
		reads exactly 0.0. Plotting those puts the farm's whole register in the
		Gulf of Guinea; dropping them silently makes a map that is missing half
		the valves and does not say so."""
		self.register("MC-Valve-05", "Irrigation Valve")
		self.register("MC-Tractor-01", "Tractor", gps_latitude=45.60, gps_longitude=-121.18)
		data = self.read()
		self.assertEqual([row["name"] for row in data["assets"]], ["MC-Tractor-01"])
		self.assertEqual(data["asset_unplaced_count"], 1)

	def test_half_a_fix_is_nowhere_to_be_drawn(self):
		"""A latitude with no longitude plots on the prime meridian."""
		self.register("MC-Tractor-01", "Tractor", gps_latitude=45.60)
		data = self.read()
		self.assertEqual(data["assets"], [])
		self.assertEqual(data["asset_unplaced_count"], 1)

	def test_a_retired_asset_is_off_the_map(self):
		"""A map is a picture of what is out there now. A decommissioned pump
		drawn beside a working one is a dispatch sent to the wrong machine."""
		self.register("MC-Tractor-01", "Tractor", gps_latitude=45.60, gps_longitude=-121.18)
		self.tool_data("retire_asset", {"asset_name": "MC-Tractor-01"})
		self.assertEqual(self.read()["assets"], [])

	def test_a_traced_building_carries_a_centre_without_a_fix(self):
		"""So a client can label the shape without computing a centroid of its
		own. `boundary_centroid_*` are what those columns are for."""
		self.a_shed()
		self.trace()
		row = next(r for r in self.read()["assets"] if r["name"] == "MC-Shed-01")
		self.assertIsNone(row["gps_latitude"])
		self.assertAlmostEqual(row["centroid_lat"], 45.60005, places=3)

	def test_a_building_on_the_equator_keeps_its_own_centroid(self):
		"""THE CASE THE `or` CHAIN GOT WRONG. `boundary_centroid_lat` is a stored
		Frappe Float — NOT NULL DEFAULT 0 — so a centroid that legitimately reads
		0.0 is indistinguishable from an unset column, and `centroid or pin`
		would silently prefer the pin for every building in Ghana, Kenya,
		Ecuador or the west of England. Branching on whether there IS a shape is
		what makes the zero survive."""
		self.a_shed(gps_latitude=45.60, gps_longitude=-121.18)
		frappe.db.set_value(
			"Asset Register",
			"MC-Shed-01",
			{
				"boundary_geojson": json.dumps(SHED),
				"boundary_centroid_lat": 0.0,
				"boundary_centroid_lon": 0.0,
			},
		)
		row = next(r for r in self.read()["assets"] if r["name"] == "MC-Shed-01")
		self.assertEqual(row["geometry"], "polygon")
		self.assertEqual(row["centroid_lat"], 0.0)
		self.assertEqual(row["centroid_lon"], 0.0)


# ── the two halves cannot drift ─────────────────────────────────────────────
class TheColumnsExist(unittest.TestCase):
	"""The six derived columns `geo.derive` produces have to be ON the doctype.

	`geo.derive` returns a dict and `doc.set` on a Frappe document accepts a key
	the doctype does not have and drops it at save. So a boundary column missing
	from `asset_register.json` is not an error — it is a computed value that
	silently goes nowhere, and the only symptom is a map that cannot draw the
	shape somebody traced.
	"""

	def test_every_derived_column_is_on_the_asset_register(self):
		from erpnext_mcp.tools import asset_tags

		doctype = frappe.get_meta(asset_tags.ASSET_REGISTER)
		names = {field.fieldname for field in doctype.fields}
		for column in (
			"boundary_geojson",
			"boundary_centroid_lat",
			"boundary_centroid_lon",
			"boundary_bbox_geojson",
			"h3_cells",
			"area_computed_acres",
		):
			with self.subTest(column=column):
				self.assertIn(column, names)

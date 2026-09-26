# SPDX-License-Identifier: MIT
"""The slope layers on /app/farm-overview. v0.185.0.

The phone has drawn slope aspect and grade since v0.167.0/v0.168.0, through the
farmops sidecar's tile routes — which authenticate a DEVICE. A Desk browser
holds a Frappe session and no device credential, so the page needs its own
door: `farm_overview.terrain_tile`, serving the same bytes from the same cache.

What is proved here is the server half: the answer carries both descriptors
with the Desk URL in them, an unbuilt layer still arrives saying so, the tile
method hands back an inline PNG, and a login that may read neither Parcel nor
Field is shown no terrain at all. `test_farm_overview_page.py` proves the page
draws what this answers.
"""

import io
import unittest

import frappe

from erpnext_mcp import farm_overview, slope_aspect

from .fixtures import V12TestCase
from .harness import STORE
from .test_slope_aspect import SlopeSiteMixin, needs_numpy

try:  # pragma: no cover - whichever the machine has
	from PIL import Image
except Exception:  # pragma: no cover
	Image = None

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
DESK = "/api/method/erpnext_mcp.farm_overview.terrain_tile"


class TerrainTestCase(SlopeSiteMixin, V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1)
		self.clear_cache()
		frappe.local.response.clear()
		self.addCleanup(frappe.local.response.clear)

	def deny(self, doctype, ptype="read"):
		STORE.denied_permissions.add((doctype, ptype))
		self.addCleanup(STORE.denied_permissions.discard, (doctype, ptype))

	def built(self):
		self.a_farm()
		self.fake_usgs()
		frappe.local.session = frappe._dict(user="Administrator", data=frappe._dict())
		slope_aspect.build()

	@staticmethod
	def on_the_farm(z=15):
		x, y = slope_aspect.to_mercator(-120.345, 47.402)
		((tx, ty),) = slope_aspect.tiles_covering((x, y, x, y), z)
		return z, tx, ty

	@staticmethod
	def by_key(answer):
		return {spec["key"]: spec for spec in answer["terrain_layers"]}


class TheAnswerCarriesTheTerrain(TerrainTestCase):
	def test_an_unbuilt_site_offers_both_layers_as_unavailable_and_says_why(self):
		"""The page shows no toggle for them, so this sentence is the only place
		a farm learns the layers exist."""
		layers = self.by_key(farm_overview.farm_overview())
		self.assertEqual(sorted(layers), ["slope_aspect", "slope_grade"])
		for key, spec in layers.items():
			with self.subTest(key=key):
				self.assertFalse(spec["available"])
				self.assertIn("build_slope_aspect_layer", spec["reason"])

	def test_the_tile_url_is_the_desks_and_not_the_phones(self):
		"""The phone's URL authenticates a device; a Desk browser would get 401
		on every tile of it."""
		for key, spec in self.by_key(farm_overview.farm_overview()).items():
			with self.subTest(key=key):
				url = spec["tile_url_template"]
				self.assertTrue(url.startswith(DESK), url)
				self.assertIn(f"layer={key}", url)
				self.assertIn("z={z}&x={x}&y={y}", url)
				self.assertNotIn("/farmops/", url)

	def test_the_aspect_key_carries_the_flat_grey(self):
		"""Flat ground has no bearing, so `legend()` does not key it — and a
		client drawing the key must not carry its own copy of the colour."""
		spec = self.by_key(farm_overview.farm_overview())["slope_aspect"]
		self.assertEqual(spec["flat_color"], slope_aspect.hex_colour(slope_aspect.FLAT_GREY))

	def test_a_login_that_may_read_neither_source_register_is_shown_no_terrain(self):
		self.deny("Parcel")
		self.deny("Field")
		self.assertEqual(farm_overview.farm_overview()["terrain_layers"], [])

	def test_either_source_register_is_enough(self):
		self.deny("Parcel")
		self.assertEqual(len(farm_overview.farm_overview()["terrain_layers"]), 2)

	@needs_numpy
	def test_a_built_site_offers_both_with_their_bounds(self):
		self.built()
		layers = self.by_key(farm_overview.farm_overview())
		for key, spec in layers.items():
			with self.subTest(key=key):
				self.assertTrue(spec["available"])
				self.assertEqual(set(spec["bounds"]), {"west", "south", "east", "north"})
		# The per-machine rollover scheme is the phone's question; the Desk
		# draws the standard bands.
		self.assertEqual(layers["slope_grade"]["mode"], "standard")


class TheTileMethod(TerrainTestCase):
	@needs_numpy
	def test_a_tile_on_the_farm_is_an_inline_png_with_ground_on_it(self):
		self.built()
		z, x, y = self.on_the_farm()
		for key in ("slope_aspect", "slope_grade"):
			with self.subTest(key=key):
				frappe.local.response.clear()
				farm_overview.terrain_tile(layer=key, z=str(z), x=str(x), y=str(y))
				answer = dict(frappe.local.response)
				self.assertEqual(answer["type"], "download")
				self.assertEqual(answer["content_type"], "image/png")
				# INLINE, or the browser offers every tile as a file to save.
				self.assertEqual(answer["display_content_as"], "inline")
				self.assertTrue(answer["filecontent"].startswith(PNG_MAGIC))
				if Image is not None:
					self.assertIsNotNone(Image.open(io.BytesIO(answer["filecontent"])).getbbox())

	@needs_numpy
	def test_the_bytes_are_the_phones(self):
		"""One cache, two doors. A second renderer would be a second answer to
		what colour the same hillside is."""
		self.built()
		z, x, y = self.on_the_farm()
		farm_overview.terrain_tile(layer="slope_aspect", z=z, x=x, y=y)
		self.assertEqual(frappe.local.response["filecontent"], slope_aspect.tile_png(z, x, y))

	def test_never_built_is_does_not_exist_and_names_the_tool(self):
		z, x, y = self.on_the_farm()
		with self.assertRaises(frappe.DoesNotExistError) as caught:
			farm_overview.terrain_tile(layer="slope_aspect", z=z, x=x, y=y)
		self.assertIn("build_slope_aspect_layer", str(caught.exception))

	def test_an_unknown_layer_is_refused_by_name(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			farm_overview.terrain_tile(layer="soil", z=15, x=1, y=1)
		self.assertIn("slope_aspect", str(caught.exception))

	def test_a_tile_address_off_the_world_is_refused(self):
		for z, x, y in ((15, -1, 0), (3, 8, 0), ("a", 0, 0)):
			with self.subTest(address=(z, x, y)), self.assertRaises(frappe.ValidationError):
				farm_overview.terrain_tile(layer="slope_aspect", z=z, x=x, y=y)

	def test_a_login_that_may_read_neither_source_register_is_refused(self):
		self.deny("Parcel")
		self.deny("Field")
		with self.assertRaises(frappe.PermissionError):
			farm_overview.terrain_tile(layer="slope_aspect", z=15, x=1, y=1)
		self.assertEqual(dict(frappe.local.response), {})


if __name__ == "__main__":  # pragma: no cover
	unittest.main()

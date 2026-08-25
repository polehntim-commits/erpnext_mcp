# SPDX-License-Identifier: MIT
"""`list_field_boundaries` — v0.125.0. The map iOS's Today tab draws first.

Every other read this route composes already has its own suite:
`list_fields`/`list_parcels`'s totals and filters are `test_farm.py`'s and
`test_realestate.py`'s, `overlays.build`'s layers are `test_overlays.py`'s, and
the boundary columns themselves are `test_mobile_boundaries.py`'s. What is
new here, and what those suites cannot show, is the ASSEMBLY: that a field's
row still carries its polygon after passing through this wrapper, that a
parcel's — stripped from `list_parcels` on purpose — is read back rather than
left `None`, and that the whole thing is reachable by a picker rather than
only a foreman.
"""

import json
import unittest

from erpnext_mcp import geo
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.farmops_api import routes as farmops_routes

from .test_mobile_boundaries import BLOCK, BOUNDARY_ON
from .test_wave2_mobile_surface import MAIN, MANAGER, ON, WORKER, Wave2TestCase


class FieldBoundariesTestCase(Wave2TestCase):
	def setUp(self):
		super().setUp()
		# `configure` replaces the switch set rather than merging into it, so the
		# boundary switches and Wave2's own — needed for `a_block`/`a_parcel`,
		# which go through `tool_data` and therefore through `mcp.handle` — are
		# combined into the one call, exactly as `test_mobile_boundaries.
		# BoundaryTestCase` does.
		self.configure(enabled=1, **dict(BOUNDARY_ON, **ON))


# ── 1. the route itself ──────────────────────────────────────────────────────
class TheRouteIsMounted(FieldBoundariesTestCase):
	def test_it_is_in_the_table_and_reaches_the_guard(self):
		by_path = {route.path: route for route in farmops_routes.ROUTES}
		route = by_path["/mobile/list_field_boundaries"]
		self.assertFalse(route.mutating)
		self.assertEqual(route.handler.farm_ops_method, "list_field_boundaries")


# ── 2. open on enrolment ─────────────────────────────────────────────────────
class TheReadIsOpenToAPicker(FieldBoundariesTestCase):
	def test_a_field_worker_is_not_refused(self):
		self.a_block("Ridge Top")
		self.be(WORKER)
		data = mobile_api.list_field_boundaries()
		self.assertEqual(len(data["fields"]), 1)

	def test_an_empty_farm_still_answers_rather_than_refuses(self):
		self.be(WORKER)
		data = mobile_api.list_field_boundaries()
		self.assertEqual(data["fields"], [])
		self.assertEqual(data["field_count"], 0)


# ── 3. the field row ─────────────────────────────────────────────────────────
class TheFieldRowCarriesItsShape(FieldBoundariesTestCase):
	def test_a_field_with_no_boundary_reports_none_rather_than_a_guess(self):
		self.a_block("Ridge Top")
		self.be(WORKER)
		row = mobile_api.list_field_boundaries()["fields"][0]
		self.assertFalse(row["has_boundary"])
		self.assertIsNone(row["boundary_geojson"])
		self.assertIsNone(row["boundary_centroid"])

	def test_the_row_carries_the_metadata_a_map_pin_needs(self):
		block = self.a_block("Ridge Top", crop="Cherry")
		self.be(WORKER)
		row = mobile_api.list_field_boundaries()["fields"][0]
		self.assertEqual(row["doctype"], "Field")
		self.assertEqual(row["name"], block)
		self.assertEqual(row["label"], "Ridge Top")
		self.assertEqual(row["company"], MAIN)
		self.assertEqual(row["parcel"], self.a_parcel())
		self.assertEqual(row["acreage"], 12.5)
		self.assertEqual(row["crop"], "Cherry")

	@unittest.skipUnless(geo.available(), "needs shapely>=2.0 and h3>=4.0.0")
	def test_a_walked_boundary_comes_back_whole(self):
		block = self.a_block("Yellow Camp Block 3", acreage=25.7)
		self.be(MANAGER)
		mobile_api.set_field_boundary(field=block, boundary_geojson=json.dumps(BLOCK))
		self.be(WORKER)
		row = mobile_api.list_field_boundaries()["fields"][0]
		self.assertTrue(row["has_boundary"])
		self.assertEqual(json.loads(row["boundary_geojson"])["type"], "Polygon")
		self.assertIsNotNone(row["boundary_centroid"])
		self.assertIn("lat", row["boundary_centroid"])
		self.assertIn("lon", row["boundary_centroid"])


# ── 4. parcels are opt in ────────────────────────────────────────────────────
class TheParcelIsOptIn(FieldBoundariesTestCase):
	def test_parcels_are_absent_by_default(self):
		self.a_block("Ridge Top")
		self.be(WORKER)
		data = mobile_api.list_field_boundaries()
		self.assertEqual(data["parcels"], [])
		self.assertEqual(data["parcel_count"], 0)
		self.assertFalse(data["include_parcels"])

	def test_asking_for_them_returns_the_register(self):
		self.a_block("Ridge Top")
		self.be(WORKER)
		data = mobile_api.list_field_boundaries(include_parcels=True)
		self.assertEqual(len(data["parcels"]), 1)
		self.assertEqual(data["parcels"][0]["name"], self.a_parcel())
		self.assertEqual(data["parcels"][0]["doctype"], "Parcel")
		self.assertTrue(data["include_parcels"])

	@unittest.skipUnless(geo.available(), "needs shapely>=2.0 and h3>=4.0.0")
	def test_a_parcels_boundary_is_read_back_even_though_list_parcels_strips_it(self):
		"""`realestate.list_parcels` reports `mapped` and a centroid and never the
		polygon itself — this route is the one caller that wants it anyway."""
		# THE ACREAGE HAS TO MATCH THE POLYGON OR THE WRITE IS REFUSED BEFORE
		# THIS TEST GETS TO ASSERT ANYTHING. `BLOCK` is the field-sized polygon
		# `test_mobile_boundaries` uses — 25.7 acres — and `a_parcel`'s default
		# is 131.43, so setting one on the other is an 80.4% disagreement and
		# `set_parcel_boundary` refuses it by the guard that has been there
		# since v0.32.0. That guard is right: the two figures would be about
		# different ground. The acreage is incidental to what is under test
		# here, which is that a parcel's polygon is READ BACK rather than
		# stripped the way `list_parcels` strips it, so the fixture is made
		# consistent rather than the guard loosened.
		parcel = self.a_parcel(acreage=25.7)
		self.be(MANAGER)
		mobile_api.set_parcel_boundary(parcel=parcel, boundary_geojson=json.dumps(BLOCK))
		self.be(WORKER)
		row = mobile_api.list_field_boundaries(include_parcels=True)["parcels"][0]
		self.assertTrue(row["has_boundary"])
		self.assertEqual(json.loads(row["boundary_geojson"])["type"], "Polygon")


# ── 5. the overlay layers ────────────────────────────────────────────────────
class TheOverlayRidesAlong(FieldBoundariesTestCase):
	def test_overlays_are_present_by_default(self):
		self.a_block("Ridge Top")
		self.be(WORKER)
		data = mobile_api.list_field_boundaries()
		self.assertIsNotNone(data["overlays"])
		self.assertIn("blocks", data["overlays"])
		self.assertEqual(len(data["overlays"]["blocks"]), 1)

	def test_they_can_be_turned_off_for_a_caller_that_only_wants_geometry(self):
		self.a_block("Ridge Top")
		self.be(WORKER)
		data = mobile_api.list_field_boundaries(include_overlays=False)
		self.assertIsNone(data["overlays"])
		# The field itself is still answered — turning off the colour does not
		# turn off the shape.
		self.assertEqual(len(data["fields"]), 1)


# ── 6. scope ──────────────────────────────────────────────────────────────────
class TheAnswerIsScopedToTheCallersEntity(FieldBoundariesTestCase):
	def test_every_row_names_an_entity_this_caller_may_reach(self):
		self.a_block("Ridge Top")
		self.be(WORKER)
		data = mobile_api.list_field_boundaries(include_parcels=True)
		for row in data["fields"] + data["parcels"]:
			self.assertIn(row["company"], (None, MAIN))
		self.assertEqual(data["company"], MAIN)

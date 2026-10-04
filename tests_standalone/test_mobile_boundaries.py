# SPDX-License-Identifier: MIT
"""A boundary walked with a phone — the three routes, and the sentence they reverse.

v0.110.0. `routes.py` has carried this line since v0.98.0:

    `set_field_boundary`, `set_zone_boundary`, `set_parcel_boundary` … are
    DELIBERATELY ABSENT. Drawing a boundary … is a desk act with a document open.

TRUE OF DRAWING ONE, NOT OF WALKING ONE. A boundary traced with a mouse on
satellite imagery is a guess at where the canopy ends in a photograph taken in
some other season; a boundary recorded by carrying a phone round the edge of a
block is a ring of fixes taken by somebody standing on the corner. Over a farm
the difference between those two comes to acres, and a block's shape is what
every geofence answer, every "was the crew in an authorised area" and every
Worker Protection Standard answer about which block was sprayed resolves
through.

THE RISK IS THE ONE EVERY WRAPPER ON THIS SURFACE CARRIES, and it is sharper
here than for the creates: a boundary is the one field on these registers whose
wrong value is invisible. A polygon that is valid, on Earth, and about the
neighbour's ground passes every check a schema can make. So the tests below are
mostly about the checks that are NOT schema checks, and about the fact that
every one of them belongs to the tool rather than to the wrapper.

FIVE CLAIMS.

1. `TheGateIsFarmManager` — the same gate as the five creates, not the dispatch
   role, and it runs BEFORE anything is read. The refusal names the alternative,
   because "I cannot save this walk" has to read as something other than the
   feature being broken.
2. `TheScopeIsHandMade` — `guard.require_scoped_doc` reads a column called
   `company` and all three of these registers call theirs `owning_entity`, so it
   would pass every docname on the bench. `_scoped_location` is the check that
   actually holds, and it answers NOT FOUND rather than refused.
3. `TheToolDoesTheWork` — the wrapper reimplements nothing. The area
   disagreement past a quarter is still refused, containment is still reported
   and never enforced, and every derived field is still recomputed from the
   polygon. The negative control is the one that matters: the wrapper must not
   be able to be talked into skipping any of it.
4. `TheAbsentArguments` — `owning_entity` and `company` are not on any of the
   three signatures, so `bind` drops them and no body can file a polygon against
   an entity the account is not scoped to. The entity is read off the record.
5. `TheRoutesAreMounted` — three paths, all three declared mutating, all three
   running the one shared write.

WHAT NEEDS SHAPELY AND WHAT DOES NOT is split on purpose. The gate, the scope
check and the missing-polygon refusal all fire before `geo.require()` does, so
they run on the CI job that installs no geospatial libraries — which is the job
where a wrapper that had quietly moved a check ahead of the gate would show up.
"""

import ast
import inspect
import json
import textwrap
import unittest

import frappe

from erpnext_mcp import geo
from erpnext_mcp.api import guard
from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.farmops_api import routes as farmops_routes

from .fixtures import MAIN, OTHER
from .harness import STORE
from .test_wave2_mobile_surface import MANAGER, WORKER, Wave2TestCase

#: The same rectangle on Dry Hollow Road `test_geo.py` walks, about 25.7 acres.
#: Small enough to be a real block, which is what makes the area check a number
#: that can be reasoned about rather than a shape the size of a county.
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

#: The same walk with one corner cut — a quarter of the block missing, which is
#: what a phone in a pocket losing fixes actually produces. It is a perfectly
#: valid polygon and it is about a different piece of ground.
SHORT_WALK = {
	"type": "Polygon",
	"coordinates": [
		[
			[-121.1800, 45.6000],
			[-121.1780, 45.6000],
			[-121.1780, 45.6015],
			[-121.1800, 45.6015],
			[-121.1800, 45.6000],
		]
	],
}

BOUNDARY_ON = {
	"allow_set_field_boundary": 1,
	"allow_set_zone_boundary": 1,
	"allow_set_parcel_boundary": 1,
}


def calls_in(function) -> set:
	"""Every dotted name this function actually CALLS, read off its AST.

	NOT A SUBSTRING SEARCH OVER THE SOURCE, and the difference is not
	fastidiousness. Every function on this surface carries a docstring and a
	comment block naming the helper it deliberately does NOT use and why —
	`guard.require_scoped_doc` is named at length in `_set_one_boundary` for
	exactly that reason — so a text search answers "yes it is in there" about
	prose explaining that it is not called. That is a check which cannot fail in
	the direction that matters.
	"""
	tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
	found = set()
	for node in ast.walk(tree):
		if not isinstance(node, ast.Call):
			continue
		target = node.func
		parts = []
		while isinstance(target, ast.Attribute):
			parts.append(target.attr)
			target = target.value
		if isinstance(target, ast.Name):
			parts.append(target.id)
			found.add(".".join(reversed(parts)))
	return found


class BoundaryTestCase(Wave2TestCase):
	"""Wave2's site, plus a block whose recorded acreage the walk can be checked
	against.

	THE SWITCHES ARE ON IN THE FIXTURE AND THEY ARE NOT THE GATE. `guard.endpoint`
	deliberately does not consult `allow_<tool>` — those switches are the AI's
	leash, and an operator who distrusts the model must not thereby lose the
	ability to record a walk. They are configured here only because the fixture's
	own `tool_data` calls go through `mcp.handle`, which does consult them.
	"""

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **dict(BOUNDARY_ON, **self._wave2_switches()))

	def _wave2_switches(self):
		from .test_wave2_mobile_surface import ON

		return ON

	def a_walked_block(self, acreage=25.7):
		"""A block whose recorded acreage agrees with `BLOCK`, ready to be walked."""
		block = self.a_block("Yellow Camp Block 3", acreage=acreage)
		return block

	def a_walked_zone(self):
		block = self.a_walked_block()
		return self.tool_data(
			"create_irrigation_zone",
			{"field": block, "zone_name": "YC3-Zone2", "area_sq_ft": 110000},
		)["name"]


# ── 1 ─────────────────────────────────────────────────────────────────────────
class TheGateIsFarmManager(BoundaryTestCase):
	"""Not the dispatch role, and not mere enrolment.

	A register entry is permanent in a way a task is not, and a boundary is the
	more consequential half of it. `guard.LOCATION_ROLES` argues the whole thing;
	these tests assert the three routes are behind it.
	"""

	def test_a_field_worker_is_refused_and_told_who_can(self):
		block = self.a_walked_block()
		self.be(WORKER)
		with self.assertRaises(frappe.PermissionError) as caught:
			mobile_api.set_field_boundary(field=block, boundary_geojson=json.dumps(BLOCK))
		message = str(caught.exception)
		self.assertIn("Farm Manager", message)

	def test_the_gate_runs_before_anything_is_read(self):
		"""A refusal that first told the caller their block does not exist would
		leak the register to an account that may not read it."""
		self.be(WORKER)
		with self.assertRaises(frappe.PermissionError):
			mobile_api.set_field_boundary(field="No Such Block", boundary_geojson=json.dumps(BLOCK))

	def test_all_three_registers_carry_the_same_gate(self):
		block = self.a_walked_block()
		zone = self.a_walked_zone()
		parcel = self.a_parcel()
		self.be(WORKER)
		for handler, argument, docname in (
			(mobile_api.set_field_boundary, "field", block),
			(mobile_api.set_zone_boundary, "zone", zone),
			(mobile_api.set_parcel_boundary, "parcel", parcel),
		):
			with self.subTest(route=handler.__name__):
				with self.assertRaises(frappe.PermissionError):
					handler(**{argument: docname, "boundary_geojson": json.dumps(BLOCK)})

	def test_a_manager_gets_past_the_gate_and_reaches_the_tool(self):
		"""Which on a bench with no shapely is a NAMED refusal from `geo.require`
		rather than a permission error — the point being that the gate let them
		through to it."""
		block = self.a_walked_block()
		self.be(MANAGER)
		try:
			mobile_api.set_field_boundary(field=block, boundary_geojson=json.dumps(BLOCK))
		except frappe.PermissionError:  # pragma: no cover - would be the bug
			self.fail("a Farm Manager was refused by the role gate")
		except frappe.ValidationError as error:
			self.assertNotIn("restricted to", str(error))


# ── 2 ─────────────────────────────────────────────────────────────────────────
class TheScopeIsHandMade(BoundaryTestCase):
	def test_a_block_in_another_entity_reads_as_not_found(self):
		"""`guard.require_scoped_doc` READS A COLUMN CALLED `company` AND THESE
		REGISTERS CALL THEIRS `owning_entity`, so it returns None, skips its own
		check and accepts a docname from another farm on the same bench. The
		refusal has to be hand-made, and it has to be NOT FOUND rather than
		refused so a caller cannot map another entity's docnames by watching which
		error comes back."""
		block = self.a_walked_block()
		frappe.db.set_value("Field", block, "owning_entity", OTHER)
		self.be(MANAGER)
		with self.assertRaises(frappe.DoesNotExistError):
			mobile_api.set_field_boundary(field=block, boundary_geojson=json.dumps(BLOCK))

	def test_the_scoped_check_is_the_one_that_runs(self):
		"""Asserted on the code rather than at runtime, because the trap is
		invisible on a single-entity fixture: the wrong helper PASSES rather than
		raising, so a test that only exercised it would go green either way.

		AND ASSERTED ON THE AST RATHER THAN ON THE TEXT, because the function's
		own comment names `guard.require_scoped_doc` at length in order to explain
		why it is not called — a substring search would find the explanation and
		call it the bug."""
		called = calls_in(mobile_api._set_one_boundary)
		self.assertIn("_scoped_location", called)
		self.assertNotIn("guard.require_scoped_doc", called)

	def test_a_block_that_does_not_exist_is_refused_by_name(self):
		self.be(MANAGER)
		with self.assertRaises((frappe.DoesNotExistError, frappe.ValidationError)) as caught:
			mobile_api.set_field_boundary(field="No Such Block", boundary_geojson=json.dumps(BLOCK))
		self.assertIn("No Such Block", str(caught.exception))


# ── 3 ─────────────────────────────────────────────────────────────────────────
class TheMissingPolygon(BoundaryTestCase):
	"""Before shapely is reached, because a body with no shape in it is not a
	question about geometry."""

	def test_a_call_with_no_polygon_is_refused_in_words(self):
		block = self.a_walked_block()
		self.be(MANAGER)
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.set_field_boundary(field=block)
		message = str(caught.exception)
		self.assertIn("boundary_geojson is required", message)
		self.assertIn("Nothing was changed", message)

	def test_a_polygon_that_arrived_decoded_is_re_encoded_rather_than_refused(self):
		"""`frappe.call` posts a JS object as JSON and some bodies arrive already
		decoded, so the polygon is a dict about as often as it is a string. A
		wrapper that only read a string would answer "your polygon is invalid"
		about a polygon that is perfectly fine."""
		self.assertIn("json.dumps", calls_in(mobile_api._set_one_boundary))


# ── 4 ─────────────────────────────────────────────────────────────────────────
@unittest.skipUnless(geo.available(), "needs shapely>=2.0 and h3>=4.0.0")
class TheToolDoesTheWork(BoundaryTestCase):
	"""Every check belongs to the tool. The wrapper reimplements none of them and
	must not be able to be talked into skipping one."""

	def test_a_walk_that_agrees_with_the_deed_is_saved_and_derives_everything(self):
		block = self.a_walked_block()
		self.be(MANAGER)
		data = mobile_api.set_field_boundary(field=block, boundary_geojson=json.dumps(BLOCK))
		self.assertTrue(data["changed"])
		self.assertAlmostEqual(data["area_computed_acres"], 25.67, delta=0.1)
		row = STORE.get_raw("Field", block)
		self.assertTrue(row["boundary_geojson"])
		self.assertTrue(row["boundary_centroid_lat"])
		self.assertTrue(row["h3_cells"])

	def test_a_walk_that_cut_a_corner_is_refused_with_both_figures_named(self):
		"""THE CHECK THAT EARNS THIS ROUTE. A walk that stopped early, cut a
		corner, or lost fixes in a pocket produces a polygon that is valid, is on
		Earth, and encloses noticeably less ground than the block is recorded as.
		Nothing about the shape itself says so."""
		block = self.a_walked_block()
		self.be(MANAGER)
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.set_field_boundary(field=block, boundary_geojson=json.dumps(SHORT_WALK))
		message = str(caught.exception)
		self.assertIn("25.7", message)
		self.assertIn("Nothing was changed", message)
		self.assertFalse(STORE.get_raw("Field", block).get("boundary_geojson"))

	def test_a_dry_run_computes_everything_and_writes_nothing(self):
		"""The handset's obvious use, and the difference between a correction that
		takes thirty seconds and one that takes a drive back out."""
		block = self.a_walked_block()
		self.be(MANAGER)
		data = mobile_api.set_field_boundary(field=block, boundary_geojson=json.dumps(BLOCK), dry_run=1)
		self.assertTrue(data["dry_run"])
		self.assertFalse(data["changed"])
		self.assertAlmostEqual(data["area_computed_acres"], 25.67, delta=0.1)
		self.assertFalse(STORE.get_raw("Field", block).get("boundary_geojson"))

	def test_a_self_intersecting_walk_is_refused(self):
		"""A bowtie has an area a computer will happily report and a containment
		test nobody can trust."""
		bowtie = {
			"type": "Polygon",
			"coordinates": [
				[
					[-121.1800, 45.6000],
					[-121.1760, 45.6030],
					[-121.1760, 45.6000],
					[-121.1800, 45.6030],
					[-121.1800, 45.6000],
				]
			],
		}
		block = self.a_walked_block()
		self.be(MANAGER)
		with self.assertRaises(frappe.ValidationError):
			mobile_api.set_field_boundary(field=block, boundary_geojson=json.dumps(bowtie))

	def test_a_zone_outside_its_block_is_reported_and_never_refused(self):
		"""A shared water line crosses a boundary, a pump house sits on the
		headland, a mainline runs down a road easement. Refusing those would make
		them unrecordable."""
		zone = self.a_walked_zone()
		frappe.db.set_value("Irrigation Zone", zone, "area_acres", 25.7)
		frappe.db.set_value("Irrigation Zone", zone, "area_sq_ft", 25.7 * mobile_api.SQ_FT_PER_ACRE)
		self.be(MANAGER)
		data = mobile_api.set_zone_boundary(zone=zone, boundary_geojson=json.dumps(BLOCK))
		self.assertTrue(data["changed"])
		self.assertIn("boundary_contained_in_field", data)

	def test_the_answer_carries_the_pair_the_handset_sends_back(self):
		"""The screen that posted this walk is a location screen, and the next
		thing it does is name the place it just measured."""
		block = self.a_walked_block()
		self.be(MANAGER)
		data = mobile_api.set_field_boundary(field=block, boundary_geojson=json.dumps(BLOCK))
		self.assertEqual(data["doctype"], "Field")
		self.assertEqual(data["location_type"], "Field")
		self.assertEqual(data["location"], block)

	def test_the_handsets_own_spelling_of_the_record_is_accepted(self):
		"""`name` is what the handset calls it and `field` is the register's word.
		`bind` drops what a signature does not name, so a method that took one of
		them would 404 the argument for whichever caller guessed the other."""
		block = self.a_walked_block()
		self.be(MANAGER)
		data = mobile_api.set_field_boundary(name=block, boundary_geojson=json.dumps(BLOCK))
		self.assertEqual(data["location"], block)


# ── 5 ─────────────────────────────────────────────────────────────────────────
class TheAbsentArguments(BoundaryTestCase):
	def test_no_body_can_name_the_entity_a_polygon_is_filed_against(self):
		"""THE ABSENT ARGUMENTS ARE THE POINT. `bind` keeps only what a signature
		names, so the entity is unreachable from a handset rather than merely
		discouraged — and the tools resolve a company when they are not given one,
		which on a multi-entity site is a refusal rather than a guess."""
		for handler in (
			mobile_api.set_field_boundary,
			mobile_api.set_zone_boundary,
			mobile_api.set_parcel_boundary,
		):
			accepted = self.accepts(handler)
			for argument in ("owning_entity", "company"):
				with self.subTest(route=handler.__name__, argument=argument):
					self.assertNotIn(argument, accepted)

	def test_the_entity_is_read_off_the_record_instead(self):
		"""Which is the same call `api/gis._save_boundary` makes for the Desk map:
		the entity comes from the document the caller already proved they may
		reach, so there is nothing for a body to name."""
		self.assertIn("frappe.db.get_value", calls_in(mobile_api._set_one_boundary))
		block = self.a_walked_block()
		self.be(MANAGER)
		# The wrapper hands the tool an entity it never received. Proven through
		# the tool's own refusal surface rather than by reading the source: a
		# block on MAIN resolves, and the wrapper did not ask anybody which.
		frappe.db.set_value("Field", block, "owning_entity", MAIN)
		try:
			mobile_api.set_field_boundary(field=block, boundary_geojson=json.dumps(BLOCK))
		except frappe.ValidationError as error:
			self.assertNotIn("company is required", str(error))

	def test_each_route_accepts_only_the_four_a_walk_needs(self):
		for handler, own in (
			(mobile_api.set_field_boundary, "field"),
			(mobile_api.set_zone_boundary, "zone"),
			(mobile_api.set_parcel_boundary, "parcel"),
		):
			with self.subTest(route=handler.__name__):
				self.assertEqual(set(self.accepts(handler)), {own, "name", "boundary_geojson", "dry_run"})


# ── 6 ─────────────────────────────────────────────────────────────────────────
class TheRoutesAreMounted(BoundaryTestCase):
	def test_the_three_paths_exist(self):
		paths = {route.path for route in farmops_routes.ROUTES}
		for path in (
			"/mobile/set_field_boundary",
			"/mobile/set_zone_boundary",
			"/mobile/set_parcel_boundary",
		):
			with self.subTest(path=path):
				self.assertIn(path, paths)

	def test_every_one_of_them_is_declared_mutating(self):
		by_path = {route.path: route for route in farmops_routes.ROUTES}
		for path in (
			"/mobile/set_field_boundary",
			"/mobile/set_zone_boundary",
			"/mobile/set_parcel_boundary",
		):
			with self.subTest(path=path):
				self.assertTrue(by_path[path].mutating, path)

	def test_all_three_run_the_one_write(self):
		"""One implementation, three spellings — asserted rather than assumed,
		because three copies of a permission check is three places to forget one."""
		for handler in (
			mobile_api.set_field_boundary,
			mobile_api.set_zone_boundary,
			mobile_api.set_parcel_boundary,
		):
			with self.subTest(route=handler.__name__):
				self.assertIn("_set_one_boundary", calls_in(handler))
		self.assertIn("guard.require_location_role", calls_in(mobile_api._set_one_boundary))

	def test_the_register_table_is_closed_and_holds_no_method_name_from_a_caller(self):
		"""Not the dispatcher `routes.py` refuses. The three registers are a
		literal in `api/mobile.py` and the caller chooses none of them — each
		route names its own."""
		self.assertEqual(set(mobile_api.BOUNDARY_REGISTERS), {"Field", "Irrigation Zone", "Parcel"})
		for spec in mobile_api.BOUNDARY_REGISTERS.values():
			self.assertTrue(spec["tool"].startswith("set_"))

	def test_the_three_registers_are_the_ones_the_desk_map_also_allows(self):
		"""`api/gis.SAVEABLE` is the Desk map's allowlist and this is the phone's.
		Two tables because the two transports gate differently, but the SET of
		registers that carry a polygon is one fact and they must agree about it."""
		from erpnext_mcp.api import gis

		self.assertEqual(set(gis.SAVEABLE), set(mobile_api.BOUNDARY_REGISTERS))

	def _fill_bucket(self, hits: int) -> None:
		"""Put `hits` in this method's rate-limit bucket, through the real counter.

		THE BUCKET IS FILLED RATHER THAN DRIVEN, and that is about a flake and not
		about speed. `guard._count` keys on `int(time.time() // 60)`, so a test
		that made the whole ceiling in real calls fails whenever the loop happens
		to cross a wall-clock minute: the window rolls, the bucket empties, and the
		call that should have been refused sails through. Two tests already in this
		suite are written that way and both have been seen to fail on a green
		tree — see `test_fallback_auth.test_the_rate_limit_still_meters_a_fallback_caller`.
		Filling alone narrowed that gap without closing it; what closed it is
		`harness.pin_rate_limit_clock`, which every test now runs under.

		FILLING IT THROUGH `_count` RATHER THAN WRITING `_BUCKETS` DIRECTLY is what
		keeps the key derivation in one place. A test that built the slot string
		itself would still pass on the day `_count` changed how it spells one.
		"""
		guard._BUCKETS.clear()
		for _ in range(hits):
			guard._count(f"set_field_boundary:{WORKER}", 60)

	def test_it_is_metered_at_the_write_limit_and_not_the_read_limit(self):
		"""Ten a minute, not sixty. A boundary write reaches the geospatial stack
		and rewrites five derived columns, and the difference between the two
		ceilings is what a handset retrying a failed upload in a loop would find.

		DRIVEN AS A WORKER ON PURPOSE. `guard.endpoint` meters BEFORE it checks the
		role — a rejected caller is exactly the one worth metering, since every
		refusal writes an audit row — so a call that gets past the limit is refused
		by the gate instead. That ordering is asserted here as much as the number
		is: the pair below is a limit of exactly WRITE_LIMIT and not one either
		side of it.
		"""
		self.be(WORKER)
		self._fill_bucket(guard.WRITE_LIMIT - 1)
		with self.assertRaises(frappe.PermissionError):
			mobile_api.set_field_boundary(field="X", boundary_geojson=json.dumps(BLOCK))

		self.be(WORKER)
		self._fill_bucket(guard.WRITE_LIMIT)
		with self.assertRaises(guard.RateLimited):
			mobile_api.set_field_boundary(field="X", boundary_geojson=json.dumps(BLOCK))

	def test_the_read_limit_would_not_have_caught_it(self):
		"""The negative control for the test above, and the reason it is worth
		one: `READ_LIMIT` is sixty, so a route wearing the read ceiling by mistake
		would pass every assertion made at ten. Filling to just under the READ
		limit and getting a refusal is what proves the ceiling is the lower one."""
		self.assertGreater(guard.READ_LIMIT, guard.WRITE_LIMIT)
		self.be(WORKER)
		self._fill_bucket(guard.READ_LIMIT - 1)
		with self.assertRaises(guard.RateLimited):
			mobile_api.set_field_boundary(field="X", boundary_geojson=json.dumps(BLOCK))

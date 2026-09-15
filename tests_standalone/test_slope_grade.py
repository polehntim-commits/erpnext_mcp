# SPDX-License-Identifier: MIT
"""Slope grade — how steep the ground is, coloured by rollover danger. v0.168.0.

The fixture is `test_slope_aspect`'s: a ridge whose two faces fall at atan(0.3)
≈ 16.7°. That one number sits in a different band under every scheme, which is
what lets each test below prove the breakpoints moved rather than merely exist:

    standard (8/15/25)             steep       orange  level 2
    ROPS tractor, 25°  (15/20/25)  approaching yellow  level 1
    ATV/UTV,      20°  (12/16/20)  at_limit    orange  level 2
    no-ROPS default 15° (9/12/15)  over_limit  red     level 3
    sprayer,      12°              over_limit  red     level 3

1. **A CELL ON A BREAK TAKES THE STEEPER BAND**, and no data is transparent.
   `TheBandsAreTheAgreedOnes`.
2. **AN UNSET LIMIT IS THE TYPE'S CAUTIOUS FIGURE, NEVER ZERO**, and a type with
   no figure is refused by name. `TheLimitComesFromTheMachine`.
3. **NOTHING IS FETCHED.** Grade tiles, blocks and points are read from the
   aspect build's cached slope with the network unplugged. `ItReusesTheCachedTerrain`.
4. **A MACHINE'S LIMIT MOVES THE COLOUR ON THE TILE.** `TheTilesShiftPerMachine`.
5. **EVERY BLOCK IS CLASSIFIED AGAINST THE MACHINE.** `TheBlocksAreClassified`.
6. **THE LIMIT IS RECORDED ON THE ASSET REGISTER.** `TheAssetCarriesItsLimit`.
7. **THE PHONE GETS THE SAME ANSWER THROUGH THE SAME GATES**, and `asset` is
   scoped. `ThePhone`.
"""

import io
import json
import math
import os
import unittest
from pathlib import Path

import frappe

from erpnext_mcp import asset_types, slope_aspect, slope_grade
from erpnext_mcp.tools import asset_tags

from .fixtures import MAIN, OTHER, V12TestCase
from .test_slope_aspect import HAVE_SHAPELY, RIDGE_LAT, SlopeSiteMixin, needs_numpy

try:
	import numpy as np
except Exception:  # pragma: no cover
	np = None

try:
	from PIL import Image
except Exception:  # pragma: no cover
	Image = None

RIDGE_DEGREES = math.degrees(math.atan(0.3))
GREEN, YELLOW, ORANGE, RED = (tuple(rgb) for rgb in slope_grade.COLOURS)

#: The middle of the south-facing block, well clear of the ridge line.
SOUTH_POINT = (47.402, -120.345)

SWITCHES = {
	"allow_get_slope_aspect_layer": 1,
	"allow_build_slope_aspect_layer": 1,
	"allow_get_slope_grade_layer": 1,
	"allow_register_asset": 1,
	"allow_update_registered_asset": 1,
	"allow_get_asset_detail": 1,
}


class Unplugged:
	"""Stands in for `slope_aspect._http_get` once the terrain is built: any call fails the test."""

	def __init__(self):
		self.calls = []

	def __call__(self, url, params=None, timeout=None):
		self.calls.append(url)
		raise AssertionError(f"the grade layer fetched {url}; it must read the cached terrain")


class GradeSiteMixin(SlopeSiteMixin):
	def built_farm(self):
		names = self.a_farm()
		self.fake_usgs()
		frappe.local.session = frappe._dict(user="Administrator", data=frappe._dict())
		slope_aspect.build()
		unplugged = Unplugged()
		slope_aspect._http_get = unplugged
		return names, unplugged

	def machine(self, name, asset_type, company=MAIN, **kw):
		return asset_tags.register_asset(
			{"name": name, "asset_type": asset_type, "company": company, **kw}
		).data

	@staticmethod
	def tile_address(z=16, point=SOUTH_POINT):
		x, y = slope_aspect.to_mercator(point[1], point[0])
		((tx, ty),) = slope_aspect.tiles_covering((x, y, x, y), z)
		xmin, _ymin, xmax, ymax = slope_aspect.tile_bounds(z, tx, ty)
		span = xmax - xmin
		px = int((x - xmin) / span * 256)
		py = int((ymax - y) / span * 256)
		return z, tx, ty, px, py

	@staticmethod
	def pixel(png, px, py):
		return Image.open(io.BytesIO(png)).convert("RGBA").getpixel((px, py))


# ── 1. the bands ────────────────────────────────────────────────────────────
@needs_numpy
class TheBandsAreTheAgreedOnes(unittest.TestCase):
	def levels(self, values, breaks=slope_grade.STANDARD_BREAKS):
		return [int(v) for v in slope_grade.classify(np.array(values, dtype="float64"), breaks)]

	def test_standard_breaks_are_8_15_25_and_a_break_goes_up(self):
		self.assertEqual(self.levels([0, 7.99, 8.0, 14.99, 15.0, 24.99, 25.0, 60]), [0, 0, 1, 1, 2, 2, 3, 3])

	def test_no_data_is_level_minus_one_and_transparent(self):
		self.assertEqual(self.levels([np.nan]), [-1])
		rgba = slope_grade.colourise(np.array([[np.nan]]), slope_grade.STANDARD_BREAKS)
		self.assertEqual(tuple(int(v) for v in rgba[0, 0]), (0, 0, 0, 0))

	def test_the_colours_run_green_yellow_orange_red(self):
		rgba = slope_grade.colourise(np.array([[2.0, 10.0, 20.0, 30.0]]), slope_grade.STANDARD_BREAKS)
		self.assertEqual(
			[tuple(int(v) for v in rgba[0, i, :3]) for i in range(4)], [GREEN, YELLOW, ORANGE, RED]
		)
		self.assertTrue((rgba[0, :, 3] == slope_grade.ALPHA).all())
		g_r, g_g, _ = GREEN
		r_r, r_g, _ = RED
		self.assertGreater(g_g, g_r)
		self.assertGreater(r_r, r_g * 3)

	def test_an_equipment_scheme_is_60_80_100_percent_of_the_limit(self):
		scheme = slope_grade.equipment_scheme(25)
		self.assertEqual(scheme["breaks"], (15.0, 20.0, 25.0))
		self.assertEqual(self.levels([14.9, 15.0, 19.9, 24.9, 25.0], scheme["breaks"]), [0, 1, 1, 2, 3])
		self.assertEqual(slope_grade.equipment_scheme(12)["breaks"], (7.2, 9.6, 12.0))

	def test_the_legend_carries_degrees_percent_and_the_open_top_band(self):
		legend = slope_grade.legend(slope_grade.standard_scheme())
		self.assertEqual([entry["level"] for entry in legend], [0, 1, 2, 3])
		self.assertEqual([entry["band"] for entry in legend], ["gentle", "moderate", "steep", "very_steep"])
		self.assertEqual((legend[1]["min_degrees"], legend[1]["max_degrees"]), (8.0, 15.0))
		self.assertIsNone(legend[3]["max_degrees"])
		self.assertIsNone(legend[3]["max_percent"])
		# 45° is 100% grade; 25° is 46.6%.
		self.assertEqual(slope_grade.percent_grade(45), 100.0)
		self.assertEqual(legend[3]["min_percent"], 46.6)
		self.assertEqual(legend[3]["color"], "#d02428")
		equipment = slope_grade.legend(slope_grade.equipment_scheme(20))
		self.assertEqual(equipment[3]["band"], "over_limit")
		self.assertEqual(equipment[3]["min_degrees"], 20.0)


# ── 2. the limit ────────────────────────────────────────────────────────────
class TheLimitComesFromTheMachine(unittest.TestCase):
	def rating(self, **row):
		return slope_grade.rating_of({"name": "MC-X", **row})

	def test_the_machines_own_limit_wins(self):
		answer = self.rating(asset_type="Tractor", max_safe_slope_degrees=25.0)
		self.assertEqual(answer["max_safe_slope_degrees"], 25.0)
		self.assertEqual(answer["max_safe_slope_source"], "asset")

	def test_a_stored_zero_is_unset_not_a_limit_of_nothing(self):
		"""A Frappe Float reads back 0.0 when never set. Read literally it would paint
		every slope red; read as "unset" it falls to the type."""
		answer = self.rating(asset_type="Tractor", max_safe_slope_degrees=0.0)
		self.assertEqual(answer["max_safe_slope_source"], "type_default")
		self.assertEqual(answer["max_safe_slope_degrees"], 15.0)

	def test_each_rated_type_has_its_cautious_default(self):
		expected = {"Tractor": 15.0, "Vehicle": 20.0, "Sprayer": 12.0, "Implement": 15.0}
		for asset_type, degrees in expected.items():
			with self.subTest(asset_type=asset_type):
				self.assertEqual(self.rating(asset_type=asset_type)["max_safe_slope_degrees"], degrees)

	def test_a_tractor_defaults_to_the_no_rops_figure_not_the_rops_one(self):
		defaults = dict(slope_grade.EQUIPMENT_DEFAULTS)
		self.assertEqual(slope_grade.TYPE_DEFAULTS["Tractor"], defaults["Tractor without ROPS"])
		self.assertLess(slope_grade.TYPE_DEFAULTS["Tractor"], defaults["Tractor with ROPS"])

	def test_a_type_with_no_figure_is_refused_by_name(self):
		with self.assertRaises(slope_grade.AssetNotRated) as caught:
			self.rating(asset_type="Irrigation Valve")
		self.assertIn("Irrigation Valve", str(caught.exception))
		self.assertIn("max_safe_slope_degrees", str(caught.exception))

	def test_a_typed_limit_is_checked(self):
		self.assertIsNone(slope_grade.validate_rating(None, ""))
		self.assertEqual(slope_grade.validate_rating("22.46", ""), 22.5)
		for bad in (0, -5, 46, "steep", float("nan")):
			with self.subTest(value=bad), self.assertRaises(slope_grade.ToolError):
				slope_grade.validate_rating(bad, "")

	def test_the_brief_s_five_equipment_figures_are_published(self):
		self.assertEqual(
			dict(slope_grade.EQUIPMENT_DEFAULTS),
			{
				"Tractor with ROPS": 25.0,
				"Tractor without ROPS": 15.0,
				"ATV/UTV": 20.0,
				"Sprayer": 12.0,
				"Mower": 15.0,
			},
		)

	def test_the_form_shows_the_field_for_exactly_the_rated_types(self):
		path = Path(slope_grade.__file__).parent / "erpnext_mcp/doctype/asset_register/asset_register.json"
		data = json.loads(path.read_text(encoding="utf-8"))
		by_name = {field["fieldname"]: field for field in data["fields"]}
		wanted = (
			"eval:"
			+ json.dumps(list(slope_grade.SLOPE_RATED_ASSET_TYPES), separators=(",", ":"))
			+ ".includes(doc.asset_type)"
		)
		for field in ("slope_section", "max_safe_slope_degrees"):
			with self.subTest(field=field):
				self.assertIn(field, data["field_order"])
				self.assertEqual(by_name[field]["depends_on"], wanted)
		self.assertEqual(by_name["max_safe_slope_degrees"]["fieldtype"], "Float")
		self.assertLessEqual(set(slope_grade.SLOPE_RATED_ASSET_TYPES), set(asset_types.SEEDED_NAMES))


# ── 3. the cache ────────────────────────────────────────────────────────────
@needs_numpy
class ItReusesTheCachedTerrain(GradeSiteMixin, V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **SWITCHES)
		self.clear_cache()

	def test_tiles_blocks_and_points_are_served_with_the_network_unplugged(self):
		_names, unplugged = self.built_farm()
		z, x, y, px, py = self.tile_address()
		png = slope_grade.tile_png(z, x, y)
		self.assertEqual(self.pixel(png, px, py)[:3], ORANGE)
		data = self.tool_data(
			"get_slope_grade_layer", {"latitude": SOUTH_POINT[0], "longitude": SOUTH_POINT[1]}
		)
		self.assertTrue(data["available"])
		self.assertEqual(data["point"]["band"], "steep")
		self.assertEqual(unplugged.calls, [])

	def test_the_negative_control_the_unplugged_fetch_does_fail_when_called(self):
		self.built_farm()
		with self.assertRaises(AssertionError):
			slope_aspect._http_get(slope_aspect.EXPORT_URL)

	def test_never_built_means_not_available_and_no_tile(self):
		unplugged = Unplugged()
		original = slope_aspect._http_get
		slope_aspect._http_get = unplugged
		self.addCleanup(setattr, slope_aspect, "_http_get", original)
		data = self.tool_data("get_slope_grade_layer")
		self.assertFalse(data["available"])
		self.assertIn("build_slope_aspect_layer", data["reason"])
		self.assertEqual(data["tile_url_template"], "/farmops/api/tiles/slope_grade/{z}/{x}/{y}.png")
		self.assertIsNone(slope_grade.tile_png(16, 0, 0))
		self.assertEqual(unplugged.calls, [])

	def test_a_grade_tile_is_kept_and_a_rebuild_retires_it(self):
		self.built_farm()
		z, x, y, _px, _py = self.tile_address(z=17)
		kept = os.path.join(slope_aspect.cache_dir(), "grade", "v1-standard", str(z), str(x), f"{y}.png")
		self.assertFalse(os.path.exists(kept))
		slope_grade.tile_png(z, x, y)
		self.assertTrue(os.path.exists(kept))
		self.fake_usgs()
		slope_aspect.build()
		self.assertFalse(os.path.exists(kept))

	def test_off_the_farm_and_out_of_zoom_are_the_transparent_tile(self):
		self.built_farm()
		self.assertEqual(slope_grade.tile_png(5, 0, 0), slope_aspect.TRANSPARENT_TILE)
		z, x, y, _px, _py = self.tile_address(z=14, point=(40.0, -100.0))
		self.assertEqual(slope_grade.tile_png(z, x, y), slope_aspect.TRANSPARENT_TILE)


# ── 4. the tiles ────────────────────────────────────────────────────────────
@needs_numpy
class TheTilesShiftPerMachine(GradeSiteMixin, V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **SWITCHES)
		self.clear_cache()
		self.built_farm()
		self.address = self.tile_address()

	def colour(self, scheme=None):
		z, x, y, px, py = self.address
		return self.pixel(slope_grade.tile_png(z, x, y, scheme), px, py)[:3]

	def test_the_same_cell_is_four_colours_under_four_limits(self):
		self.assertAlmostEqual(
			float(slope_grade.grade_at(*SOUTH_POINT)["slope_degrees"]), RIDGE_DEGREES, delta=0.6
		)
		self.assertEqual(self.colour(), ORANGE)
		self.assertEqual(self.colour(slope_grade.equipment_scheme(25)), YELLOW)
		self.assertEqual(self.colour(slope_grade.equipment_scheme(20)), ORANGE)
		self.assertEqual(self.colour(slope_grade.equipment_scheme(15)), RED)
		self.assertEqual(self.colour(slope_grade.equipment_scheme(40)), GREEN)

	def test_each_limit_keeps_its_own_tiles(self):
		self.colour(slope_grade.equipment_scheme(25))
		self.colour(slope_grade.equipment_scheme(12))
		grade = os.path.join(slope_aspect.cache_dir(), "grade")
		self.assertEqual(sorted(os.listdir(grade)), ["v1-max-12", "v1-max-25"])
		# Asked again, the kept 25° tile is still yellow — not the 12° one's red.
		self.assertEqual(self.colour(slope_grade.equipment_scheme(25)), YELLOW)

	def test_a_grade_pixel_lands_where_the_aspect_pixel_does(self):
		z, x, y, _px, _py = self.address
		aspect = Image.open(io.BytesIO(slope_aspect.tile_png(z, x, y))).getchannel("A")
		grade = Image.open(io.BytesIO(slope_grade.tile_png(z, x, y))).getchannel("A")
		self.assertEqual(aspect.tobytes(), grade.tobytes())


# ── 5. the blocks ───────────────────────────────────────────────────────────
@needs_numpy
@unittest.skipUnless(HAVE_SHAPELY, "block summaries need shapely")
class TheBlocksAreClassified(GradeSiteMixin, V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **SWITCHES)
		self.clear_cache()
		self.names, _unplugged = self.built_farm()

	def blocks(self, **args):
		data = self.tool_data("get_slope_grade_layer", args)
		return data, {row["field_name"]: row for row in data["blocks"]}

	def test_standard_the_ridge_faces_are_steep_and_their_stats_are_right(self):
		data, blocks = self.blocks()
		self.assertEqual(data["mode"], "standard")
		self.assertIsNone(data["equipment"])
		south = blocks["South Face"]
		self.assertEqual((south["level"], south["band"]), (2, "steep"))
		self.assertAlmostEqual(south["mean_slope_degrees"], RIDGE_DEGREES, delta=1.0)
		self.assertGreaterEqual(south["max_slope_degrees"], south["p90_slope_degrees"])
		self.assertGreater(south["band_shares"]["steep"], 0.9)
		self.assertGreater(south["share_at_or_above_degrees"]["15"], 0.9)
		self.assertEqual(south["share_at_or_above_degrees"]["25"], 0.0)
		self.assertAlmostEqual(sum(south["band_shares"].values()), 1.0, delta=0.005)
		self.assertNotIn("cells_over_limit", south)
		self.assertEqual(sorted(row["steepness_rank"] for row in data["blocks"]), [1, 2])

	def test_a_rops_tractor_is_approaching_its_limit(self):
		self.machine("MC-Tractor-01", "Tractor", max_safe_slope_degrees=25)
		data, blocks = self.blocks(asset="MC-Tractor-01")
		self.assertEqual(data["mode"], "equipment")
		self.assertEqual(data["equipment"]["max_safe_slope_source"], "asset")
		self.assertEqual(data["breakpoints_degrees"], [15.0, 20.0, 25.0])
		self.assertEqual(
			data["tile_url_template"], "/farmops/api/tiles/slope_grade/{z}/{x}/{y}.png?asset=MC-Tractor-01"
		)
		self.assertEqual(blocks["South Face"]["band"], "approaching")
		self.assertEqual(blocks["South Face"]["cells_over_limit"], 0)

	def test_an_unrated_tractor_uses_the_no_rops_figure_and_is_over_the_limit(self):
		self.machine("MC-Tractor-02", "Tractor")
		data, blocks = self.blocks(asset="MC-Tractor-02")
		self.assertEqual(data["equipment"]["max_safe_slope_source"], "type_default")
		self.assertEqual(data["equipment"]["max_safe_slope_degrees"], 15.0)
		for label in ("North Face", "South Face"):
			with self.subTest(block=label):
				self.assertEqual((blocks[label]["level"], blocks[label]["band"]), (3, "over_limit"))
				self.assertGreater(blocks[label]["share_over_limit"], 0.9)

	def test_an_atv_is_at_its_limit_and_a_sprayer_is_over(self):
		self.machine("MC-Quad-01", "Vehicle")
		self.machine("MC-Sprayer-01", "Sprayer")
		self.assertEqual(self.blocks(asset="MC-Quad-01")[1]["South Face"]["band"], "at_limit")
		self.assertEqual(self.blocks(asset="MC-Sprayer-01")[1]["South Face"]["band"], "over_limit")

	def test_one_over_limit_cell_makes_the_block_red(self):
		"""The steepest cell decides, and the share says how much of the block it is."""
		scheme = slope_grade.equipment_scheme(20)
		cells = np.array([5.0] * 99 + [21.0])
		summary = slope_grade.summarise(cells, scheme)
		self.assertEqual((summary["level"], summary["band"]), (3, "over_limit"))
		self.assertEqual(summary["cells_over_limit"], 1)
		self.assertEqual(summary["band_shares"]["over_limit"], 0.01)
		self.assertEqual(summary["band_shares"]["within"], 0.99)

	def test_the_most_dangerous_block_ranks_first(self):
		# Flatten everything north of the ridge: the north face goes gentle.
		meta = slope_aspect.read_meta()
		grid = meta["grid"]
		_x, y = slope_aspect.to_mercator(-120.345, RIDGE_LAT)
		slope_aspect.load_grid(meta)["slope"][: int((grid["ymax"] - y) // grid["cell"])] = 3.0
		_data, blocks = self.blocks()
		self.assertEqual(blocks["North Face"]["band"], "gentle")
		self.assertEqual(blocks["South Face"]["steepness_rank"], 1)
		self.assertEqual(blocks["North Face"]["steepness_rank"], 2)

	def test_an_asset_that_cannot_be_rated_or_found_is_refused(self):
		self.machine("MC-Valve-01", "Irrigation Valve")
		self.assertIn("Irrigation Valve", self.tool_error("get_slope_grade_layer", {"asset": "MC-Valve-01"}))
		self.assertIn("No Asset Register record", self.tool_error("get_slope_grade_layer", {"asset": "NOPE"}))

	def test_a_point_is_classified_against_the_machine(self):
		self.machine("MC-Tractor-01", "Tractor", max_safe_slope_degrees=25)
		point = self.tool_data(
			"get_slope_grade_layer",
			{"asset": "MC-Tractor-01", "latitude": SOUTH_POINT[0], "longitude": SOUTH_POINT[1]},
		)["point"]
		self.assertEqual((point["level"], point["band"]), (1, "approaching"))
		self.assertAlmostEqual(point["slope_percent"], 30.0, delta=1.5)


# ── 6. the asset register ───────────────────────────────────────────────────
class TheAssetCarriesItsLimit(GradeSiteMixin, V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **SWITCHES)

	def stored(self, name):
		return frappe.db.get_value("Asset Register", name, "max_safe_slope_degrees")

	def test_register_asset_stores_it(self):
		data = self.tool_data(
			"register_asset",
			{"name": "MC-Tractor-01", "asset_type": "Tractor", "company": MAIN, "max_safe_slope_degrees": 25},
		)
		self.assertEqual(data["max_safe_slope_degrees"], 25.0)
		self.assertEqual(self.stored("MC-Tractor-01"), 25.0)

	def test_it_is_refused_on_a_type_that_does_not_drive(self):
		error = self.tool_error(
			"register_asset",
			{
				"name": "MC-Valve-01",
				"asset_type": "Irrigation Valve",
				"company": MAIN,
				"max_safe_slope_degrees": 10,
			},
		)
		self.assertIn("max_safe_slope_degrees", error)
		self.assertFalse(frappe.db.exists("Asset Register", "MC-Valve-01"))

	def test_a_zero_is_refused_rather_than_stored_as_unset(self):
		error = self.tool_error(
			"register_asset",
			{"name": "MC-Tractor-01", "asset_type": "Tractor", "company": MAIN, "max_safe_slope_degrees": 0},
		)
		self.assertIn("between 1 and 45", error)
		self.assertFalse(frappe.db.exists("Asset Register", "MC-Tractor-01"))

	def test_update_sets_it_and_null_clears_it_back_to_the_type(self):
		self.machine("MC-Tractor-01", "Tractor")
		data = self.tool_data(
			"update_registered_asset", {"asset_name": "MC-Tractor-01", "max_safe_slope_degrees": 25}
		)
		self.assertEqual(self.stored("MC-Tractor-01"), 25.0)
		self.assertIn("max_safe_slope_degrees", data["changed"])
		detail = self.tool_data("get_asset_detail", {"asset_name": "MC-Tractor-01"})
		self.assertEqual(detail["slope_rating"]["max_safe_slope_source"], "asset")
		self.tool_data(
			"update_registered_asset", {"asset_name": "MC-Tractor-01", "max_safe_slope_degrees": None}
		)
		self.assertFalse(self.stored("MC-Tractor-01"))
		detail = self.tool_data("get_asset_detail", {"asset_name": "MC-Tractor-01"})
		self.assertIsNone(detail["max_safe_slope_degrees"])
		self.assertEqual(detail["slope_rating"]["max_safe_slope_degrees"], 15.0)
		self.assertEqual(detail["slope_rating"]["max_safe_slope_source"], "type_default")

	def test_clearing_a_limit_that_was_never_set_is_nothing_to_change(self):
		self.machine("MC-Tractor-01", "Tractor")
		self.frappe_zero("MC-Tractor-01")
		error = self.tool_error(
			"update_registered_asset", {"asset_name": "MC-Tractor-01", "max_safe_slope_degrees": None}
		)
		self.assertIn("nothing to change", error)

	def frappe_zero(self, name):
		"""What a bench reads back for a Float nobody set."""
		frappe.db.set_value("Asset Register", name, "max_safe_slope_degrees", 0.0)


# ── 7. the phone ────────────────────────────────────────────────────────────
from .test_farmops_api import PREFIX, FarmOpsAPITestCase  # noqa: E402

TILE = f"{PREFIX}/tiles/slope_grade"
LAYER = f"{PREFIX}/mobile/get_slope_grade_layer"


@needs_numpy
class ThePhone(GradeSiteMixin, FarmOpsAPITestCase):
	def setUp(self):
		super().setUp()
		self.clear_cache()

	def build(self):
		self.built_farm()
		z, x, y, px, py = self.tile_address()
		self.px = (px, py)
		return f"{TILE}/{z}/{x}/{y}.png"

	def get(self, path):
		return self.post(path, method="GET")

	def test_a_standard_tile_is_a_png_coloured_by_the_standard_bands(self):
		path = self.build()
		response = self.get(path)
		self.assertEqual(response.status_code, 200, response.get_data()[:200])
		self.assertEqual(response.headers["Content-Type"], "image/png")
		self.assertEqual(self.pixel(response.get_data(), *self.px)[:3], ORANGE)

	def test_an_asset_moves_the_colour_on_the_tile(self):
		path = self.build()
		self.machine("MC-Tractor-01", "Tractor", max_safe_slope_degrees=25)
		self.machine("MC-Sprayer-01", "Sprayer")
		tractor = self.get(f"{path}?asset=MC-Tractor-01")
		sprayer = self.get(f"{path}?asset=MC-Sprayer-01")
		self.assertEqual(tractor.status_code, 200, tractor.get_data()[:200])
		self.assertEqual(self.pixel(tractor.get_data(), *self.px)[:3], YELLOW)
		self.assertEqual(self.pixel(sprayer.get_data(), *self.px)[:3], RED)

	def test_another_entitys_machine_is_404_worded_as_an_absent_one(self):
		path = self.build()
		self.machine("OT-Tractor-01", "Tractor", company=OTHER, max_safe_slope_degrees=25)
		theirs = self.refusal(f"{path}?asset=OT-Tractor-01", method="GET")
		absent = self.refusal(f"{path}?asset=NO-SUCH-01", method="GET")
		self.assertEqual((theirs[0], absent[0]), (404, 404))
		self.assertEqual(
			theirs[1]["error"].replace("OT-Tractor-01", "X"), absent[1]["error"].replace("NO-SUCH-01", "X")
		)

	def test_a_machine_with_no_limit_is_400_not_standard_colours(self):
		path = self.build()
		self.machine("MC-Valve-01", "Irrigation Valve")
		status, body = self.refusal(f"{path}?asset=MC-Valve-01", method="GET")
		self.assertEqual(status, 400)
		self.assertIn("max_safe_slope_degrees", body["error"])

	def test_never_built_is_404_and_no_credential_is_401(self):
		status, body = self.refusal(f"{TILE}/16/0/0.png", method="GET")
		self.assertEqual(status, 404)
		self.assertIn("build_slope_aspect_layer", body["error"])
		self.assertEqual(self.refusal(f"{TILE}/16/0/0.png", method="GET", credential=False)[0], 401)

	def test_post_is_405_and_a_malformed_address_is_404(self):
		path = self.build()
		self.assertEqual(self.refusal(path)[0], 405)
		self.assertEqual(self.refusal(f"{TILE}/15/abc/1.png", method="GET")[0], 404)

	def test_the_kill_switch_closes_the_grade_tiles_too(self):
		path = self.build()
		self.configure(enabled=1, farm_ops_mobile_enabled=0)
		self.assertEqual(self.refusal(path, method="GET")[0], 503)

	def test_the_descriptor_standard_then_for_a_machine(self):
		before = self.message(LAYER)
		self.assertFalse(before["available"])
		self.build()
		self.machine("MC-Tractor-01", "Tractor", max_safe_slope_degrees=25)
		standard = self.message(LAYER)
		self.assertTrue(standard["available"])
		self.assertEqual(standard["mode"], "standard")
		self.assertNotIn("blocks", standard)
		machine = self.message(LAYER, {"asset": "MC-Tractor-01"})
		self.assertEqual(machine["mode"], "equipment")
		self.assertEqual(machine["equipment"]["max_safe_slope_degrees"], 25.0)
		self.assertTrue(machine["tile_url_template"].endswith("?asset=MC-Tractor-01"))
		self.assertEqual([entry["min_degrees"] for entry in machine["legend"]], [0.0, 15.0, 20.0, 25.0])

	@unittest.skipUnless(HAVE_SHAPELY, "block summaries need shapely")
	def test_the_descriptor_classifies_blocks_for_the_machine_when_asked(self):
		self.build()
		self.machine("MC-Sprayer-01", "Sprayer")
		answer = self.message(LAYER, {"asset": "MC-Sprayer-01", "include_blocks": True})
		self.assertEqual({row["band"] for row in answer["blocks"]}, {"over_limit"})

	def test_the_descriptor_refuses_an_unrated_machine_with_400(self):
		self.build()
		self.machine("MC-Valve-01", "Irrigation Valve")
		self.assertEqual(self.refusal(LAYER, {"asset": "MC-Valve-01"})[0], 400)

	def test_the_descriptor_reads_another_entitys_machine_as_not_found(self):
		self.build()
		self.machine("OT-Tractor-01", "Tractor", company=OTHER)
		self.assertEqual(self.refusal(LAYER, {"asset": "OT-Tractor-01"})[0], 404)

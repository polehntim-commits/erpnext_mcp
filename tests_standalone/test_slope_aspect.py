# SPDX-License-Identifier: MIT
"""Slope aspect — which way the ground faces, as tiles and as a block ranking. v0.167.0.

1. **THE BEARING IS THE DIRECTION THE SLOPE FACES, NOT THE ONE IT RISES.**
   `TheArithmeticFacesDownhill`. Ground rising to the north faces SOUTH, and a
   sign error here paints every early block as a late one — a mistake that
   looks exactly like a working map. Planes in all four directions, a cone, and
   the latitude correction mercator needs for steepness but not for aspect.

2. **FLAT GROUND FACES NOWHERE.** `FlatIsGrey`. Survey noise on a valley floor
   must not be coloured as a hillside.

3. **THE PALETTE IS THE ONE THAT WAS AGREED.** `ThePaletteIsACompass`. North
   blue, east green, south red, west yellow, steepness as saturation, no data
   transparent.

4. **A TILE PUTS EACH CELL WHERE IT IS ON EARTH.** `TilesLandWhereTheGroundIs`.

5. **THE BUILD ASKS USGS FOR 1/3 ARC-SECOND AND NOTHING ELSE**, and a failed
   build leaves the previous one standing. `TheBuildIsPinnedAndAtomic`.

6. **A SOUTH-FACING BLOCK RANKS AHEAD OF A NORTH-FACING ONE.** `TheRanking`.

7. **THE TILE ROUTE RUNS THE ENROLMENT GATES AND ANSWERS PNG.** `TheTileRoute`.
"""

import io
import json
import math
import os
import shutil
import unittest

import frappe

from erpnext_mcp import slope_aspect

from .fixtures import MAIN, V12TestCase

try:
	import numpy as np
except Exception:  # pragma: no cover - the first CI leg has no numpy
	np = None

try:
	from PIL import Image
except Exception:  # pragma: no cover
	Image = None

try:
	import shapely  # noqa: F401

	HAVE_SHAPELY = True
except Exception:  # pragma: no cover
	HAVE_SHAPELY = False

needs_numpy = unittest.skipUnless(np is not None and Image is not None, "needs numpy and Pillow")

#: A farm on the Wenatchee side of the Cascades, two blocks either side of a
#: ridge that runs east-west through latitude 47.405.
PARCEL_BOX = (-120.350, 47.400, -120.340, 47.410)
NORTH_BLOCK = (-120.3480, 47.4065, -120.3420, 47.4095)
SOUTH_BLOCK = (-120.3480, 47.4005, -120.3420, 47.4035)
RIDGE_LAT = 47.405


def polygon(box):
	west, south, east, north = box
	return {
		"type": "Polygon",
		"coordinates": [[[west, south], [east, south], [east, north], [west, north], [west, south]]],
	}


def tiff_bytes(dem) -> bytes:
	buffer = io.BytesIO()
	Image.fromarray(np.asarray(dem, dtype="float32"), mode="F").save(buffer, format="TIFF")
	return buffer.getvalue()


class FakeUSGS:
	"""Stands in for `slope_aspect._http_get`. Records every request.

	The DEM is a ridge at `RIDGE_LAT`: ground falls away 0.3 m per ground metre
	either side of it, so the north side faces north and the south side south,
	both at atan(0.3) ≈ 16.7°.
	"""

	HREF = "https://elevation.nationalmap.gov/arcgis/rest/directories/elevation_output/fake.tif"

	def __init__(self, fail_export=False):
		self.calls = []
		self.fail_export = fail_export
		self.params = None

	def __call__(self, url, params=None, timeout=None):
		self.calls.append((url, dict(params or {})))
		if url == slope_aspect.EXPORT_URL:
			if self.fail_export:
				return 502, b"<html>bad gateway</html>"
			self.params = dict(params)
			return 200, json.dumps({"href": self.HREF}).encode()
		if url == self.HREF:
			return 200, tiff_bytes(self.ridge())
		if url == slope_aspect.TNM_PRODUCTS_URL:
			return 200, json.dumps(
				{
					"items": [
						{
							"title": "USGS 1/3 Arc Second n48w121 20120801",
							"publicationDate": "2012-08-01",
							"downloadURL": "https://prd-tnm.s3.amazonaws.com/x.tif",
							"sourceId": "abc",
						}
					]
				}
			).encode()
		raise AssertionError(f"unexpected fetch {url}")

	def ridge(self):
		width, height = (int(part) for part in self.params["size"].split(","))
		xmin, _ymin, _xmax, ymax = (float(part) for part in self.params["bbox"].split(","))
		cell = (float(self.params["bbox"].split(",")[2]) - xmin) / width
		ys = ymax - (np.arange(height) + 0.5) * cell
		lats = np.degrees(2 * np.arctan(np.exp(ys / slope_aspect.EARTH_RADIUS)) - math.pi / 2)
		# Ground metres from the ridge, north positive.
		metres = (lats - RIDGE_LAT) * 111_320.0
		column = 600.0 - 0.3 * np.abs(metres)
		return np.tile(column.reshape(-1, 1), (1, width)).astype("float32")


# ── 1. the arithmetic ───────────────────────────────────────────────────────
@needs_numpy
class TheArithmeticFacesDownhill(unittest.TestCase):
	def setUp(self):
		self.cells = np.full(21, 10.0)
		self.rows, self.cols = np.mgrid[0:21, 0:21].astype("float64")

	def bearing(self, dem, row=10, col=10):
		aspect, slope = slope_aspect.gradient(dem, self.cells)
		return float(aspect[row, col]), float(slope[row, col])

	def test_ground_rising_to_the_north_faces_south(self):
		# Row 0 is the northern edge, so elevation falling with row rises northward.
		bearing, slope = self.bearing(-self.rows * 2.0)
		self.assertAlmostEqual(bearing, 180.0, places=4)
		self.assertAlmostEqual(slope, math.degrees(math.atan(0.2)), places=4)

	def test_the_other_three_cardinal_directions(self):
		self.assertAlmostEqual(self.bearing(self.rows * 2.0)[0], 0.0, places=4)
		self.assertAlmostEqual(self.bearing(self.cols * 2.0)[0], 270.0, places=4)
		self.assertAlmostEqual(self.bearing(-self.cols * 2.0)[0], 90.0, places=4)

	def test_a_diagonal_is_a_diagonal(self):
		# Rising toward the north-east faces south-west.
		self.assertAlmostEqual(self.bearing(self.cols - self.rows)[0], 225.0, places=4)

	def test_a_cone_faces_away_from_its_summit_all_the_way_round(self):
		dem = 100.0 - np.hypot(self.rows - 10, self.cols - 10) * 5.0
		aspect, _slope = slope_aspect.gradient(dem, self.cells)
		self.assertAlmostEqual(float(aspect[10, 16]), 90.0, delta=1e-6)
		self.assertAlmostEqual(float(aspect[16, 10]), 180.0, delta=1e-6)
		self.assertAlmostEqual(float(aspect[10, 4]), 270.0, delta=1e-6)
		self.assertAlmostEqual(float(aspect[4, 10]) % 360, 0.0, delta=1e-6)
		self.assertAlmostEqual(float(aspect[16, 16]), 135.0, delta=1e-6)

	def test_steepness_uses_ground_metres_not_mercator_metres(self):
		"""At 60° N a mercator cell is twice its ground size. A plane rising one
		metre per GROUND metre is 45°, and reading the grid's own cell size would
		call it 26.6°."""
		lat = math.radians(60.0)
		y = slope_aspect.EARTH_RADIUS * math.log(math.tan(math.pi / 4 + lat / 2))
		grid = {"xmin": 0.0, "ymax": y + 10.0, "cell": 20.0, "width": 21, "height": 1}
		ground = slope_aspect.row_ground_cells(grid)
		self.assertAlmostEqual(float(ground[0]), 10.0, delta=0.01)
		dem = (self.cols * ground[0])[:1].repeat(3, axis=0)
		_aspect, slope = slope_aspect.gradient(dem, np.full(3, ground[0]))
		self.assertAlmostEqual(float(slope[1, 10]), 45.0, places=3)

	def test_a_missing_neighbour_makes_the_cell_unknown_rather_than_guessed(self):
		dem = -self.rows * 2.0
		dem[9, 9] = np.nan
		aspect, slope = slope_aspect.gradient(dem, self.cells)
		self.assertTrue(math.isnan(float(slope[10, 10])))
		self.assertTrue(math.isnan(float(aspect[10, 10])))
		self.assertFalse(math.isnan(float(slope[15, 15])))


# ── 2. flat ─────────────────────────────────────────────────────────────────
@needs_numpy
class FlatIsGrey(unittest.TestCase):
	def test_ground_under_the_flat_threshold_has_no_aspect_and_is_grey(self):
		rows = np.mgrid[0:11, 0:11][0].astype("float64")
		dem = -rows * 0.1  # 0.57°, facing south
		aspect, slope = slope_aspect.gradient(dem, np.full(11, 10.0))
		self.assertTrue(math.isnan(float(aspect[5, 5])))
		self.assertLess(float(slope[5, 5]), slope_aspect.FLAT_DEGREES)
		rgba = slope_aspect.colourise(aspect, slope)
		self.assertEqual(tuple(int(v) for v in rgba[5, 5, :3]), slope_aspect.FLAT_GREY)
		self.assertEqual(int(rgba[5, 5, 3]), slope_aspect.ALPHA)

	def test_the_negative_control_a_steeper_plane_does_get_an_aspect(self):
		rows = np.mgrid[0:11, 0:11][0].astype("float64")
		aspect, _slope = slope_aspect.gradient(-rows * 1.0, np.full(11, 10.0))
		self.assertAlmostEqual(float(aspect[5, 5]), 180.0, places=4)

	def test_a_flat_block_summary_counts_its_cells_as_flat(self):
		summary = slope_aspect.summarise(np.full(4, np.nan), np.full(4, 0.5))
		self.assertEqual(summary["flat_share"], 1.0)
		self.assertIsNone(summary["mean_aspect"])
		self.assertEqual(summary["southness_index"], 0.0)


# ── 3. the palette ──────────────────────────────────────────────────────────
@needs_numpy
class ThePaletteIsACompass(unittest.TestCase):
	def colour(self, bearing, slope=30.0):
		rgba = slope_aspect.colourise(np.array([[bearing]], dtype="float64"), np.array([[slope]]))
		return tuple(int(v) for v in rgba[0, 0])

	def test_north_is_blue_east_green_south_red_west_yellow(self):
		r, g, b, _a = self.colour(0)
		self.assertGreater(b, max(r, g))
		r, g, b, _a = self.colour(90)
		self.assertGreater(g, max(r, b))
		r, g, b, _a = self.colour(180)
		self.assertGreater(r, max(g, b) * 2)
		r, g, b, _a = self.colour(270)
		self.assertGreater(min(r, g), b * 3)

	def test_the_wheel_closes_359_is_next_to_1(self):
		near = [abs(a - b) for a, b in zip(self.colour(359.5)[:3], self.colour(0.5)[:3], strict=True)]
		self.assertLessEqual(max(near), 3)

	def test_steepness_is_saturation(self):
		gentle = self.colour(180, slope=5.0)
		steep = self.colour(180, slope=25.0)
		self.assertGreater(steep[0] - steep[1], gentle[0] - gentle[1])

	def test_no_data_is_transparent(self):
		rgba = slope_aspect.colourise(np.array([[np.nan]]), np.array([[np.nan]]))
		self.assertEqual(tuple(int(v) for v in rgba[0, 0]), (0, 0, 0, 0))

	def test_the_legend_names_eight_points_and_the_labels_round_to_them(self):
		self.assertEqual([entry["aspect"] for entry in slope_aspect.legend()][::2], ["N", "E", "S", "W"])
		self.assertEqual(slope_aspect.aspect_label(350), "N")
		self.assertEqual(slope_aspect.aspect_label(200), "S")
		self.assertEqual(slope_aspect.aspect_label(247), "SW")
		self.assertIsNone(slope_aspect.aspect_label(None))


# ── 4. tiles ────────────────────────────────────────────────────────────────
@needs_numpy
class TilesLandWhereTheGroundIs(unittest.TestCase):
	def test_tile_bounds_follow_the_xyz_scheme_with_y_zero_at_the_north(self):
		xmin, ymin, xmax, ymax = slope_aspect.tile_bounds(1, 0, 0)
		self.assertAlmostEqual(xmin, -slope_aspect.MERCATOR_HALF)
		self.assertAlmostEqual(ymax, slope_aspect.MERCATOR_HALF)
		self.assertAlmostEqual(xmax, 0.0, places=6)
		self.assertAlmostEqual(ymin, 0.0, places=6)

	def test_the_tile_covering_a_point_contains_it(self):
		x, y = slope_aspect.to_mercator(-120.345, 47.405)
		((tx, ty),) = slope_aspect.tiles_covering((x, y, x, y), 15)
		xmin, ymin, xmax, ymax = slope_aspect.tile_bounds(15, tx, ty)
		self.assertTrue(xmin <= x <= xmax and ymin <= y <= ymax)
		lon, lat = slope_aspect.to_lonlat(x, y)
		self.assertAlmostEqual(lon, -120.345, places=9)
		self.assertAlmostEqual(lat, 47.405, places=9)

	def test_a_grid_that_is_exactly_one_tile_fills_it_cell_for_cell(self):
		z, tx, ty = 16, 11000, 23000
		xmin, _ymin, xmax, ymax = slope_aspect.tile_bounds(z, tx, ty)
		grid = {"xmin": xmin, "ymax": ymax, "cell": (xmax - xmin) / 4, "width": 4, "height": 4}
		rgba = np.zeros((4, 4, 4), dtype="uint8")
		rgba[0, 3] = (255, 0, 0, 255)  # north-east cell
		rgba[3, 0] = (0, 0, 255, 255)  # south-west cell
		pixels = slope_aspect.render_tile_pixels(grid, rgba, z, tx, ty)
		self.assertEqual(tuple(pixels[10, 250]), (255, 0, 0, 255))
		self.assertEqual(tuple(pixels[250, 10]), (0, 0, 255, 255))
		self.assertEqual(int(pixels[128, 128, 3]), 0)

	def test_the_png_decodes_to_the_pixels_it_was_given(self):
		pixels = np.zeros((256, 256, 4), dtype="uint8")
		pixels[:, :128] = (205, 35, 45, 160)
		image = Image.open(io.BytesIO(slope_aspect.png_rgba(pixels)))
		self.assertEqual(image.mode, "RGBA")
		self.assertEqual(image.getpixel((5, 5)), (205, 35, 45, 160))
		self.assertEqual(image.getpixel((200, 5)), (0, 0, 0, 0))

	def test_the_empty_tile_is_a_real_transparent_png(self):
		image = Image.open(io.BytesIO(slope_aspect.TRANSPARENT_TILE))
		self.assertEqual(image.size, (256, 256))
		self.assertIsNone(image.getbbox())

	def test_a_tile_address_outside_the_world_is_not_a_tile(self):
		self.assertTrue(slope_aspect.valid_tile(3, 7, 7))
		self.assertFalse(slope_aspect.valid_tile(3, 8, 0))
		self.assertFalse(slope_aspect.valid_tile("x", 0, 0))


# ── the site fixture ────────────────────────────────────────────────────────
class SlopeSiteMixin:
	def a_farm(self):
		parcel = frappe.new_doc("Parcel")
		parcel.update({"owning_entity": MAIN, "parcel_name": "Ridge Ranch", "acreage": 180})
		parcel.insert()
		self.bound("Parcel", parcel.name, PARCEL_BOX)
		names = {}
		for label, box in (("North Face", NORTH_BLOCK), ("South Face", SOUTH_BLOCK)):
			field = frappe.new_doc("Field")
			field.update({"owning_entity": MAIN, "parcel": parcel.name, "field_name": label, "acreage": 20})
			field.insert()
			self.bound("Field", field.name, box)
			names[label] = field.name
		return names

	def bound(self, doctype, name, box):
		frappe.db.set_value(doctype, name, "boundary_geojson", json.dumps(polygon(box)))
		frappe.db.set_value(doctype, name, "boundary_bbox_geojson", json.dumps(polygon(box)))

	def fake_usgs(self, **kwargs):
		fake = FakeUSGS(**kwargs)
		original = slope_aspect._http_get
		slope_aspect._http_get = fake
		self.addCleanup(setattr, slope_aspect, "_http_get", original)
		return fake

	def clear_cache(self):
		shutil.rmtree(slope_aspect.cache_dir(), ignore_errors=True)
		slope_aspect._GRID_CACHE.clear()
		self.addCleanup(shutil.rmtree, slope_aspect.cache_dir(), True)
		self.addCleanup(slope_aspect._GRID_CACHE.clear)


SWITCHES = {"allow_get_slope_aspect_layer": 1, "allow_build_slope_aspect_layer": 1}


# ── 5. the build ────────────────────────────────────────────────────────────
@needs_numpy
class TheBuildIsPinnedAndAtomic(SlopeSiteMixin, V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **SWITCHES)
		self.clear_cache()

	def test_the_build_is_off_by_default_like_every_write(self):
		self.configure(enabled=1)
		self.a_farm()
		self.fake_usgs()
		self.assertIn("switched off", self.tool_error("build_slope_aspect_layer"))

	def test_it_asks_for_the_1_3_arc_second_rasters_in_mercator_over_the_farm(self):
		self.a_farm()
		fake = self.fake_usgs()
		data = self.tool_data("build_slope_aspect_layer")
		params = fake.params
		self.assertEqual(params["bboxSR"], 3857)
		self.assertEqual(params["imageSR"], 3857)
		self.assertEqual(params["pixelType"], "F32")
		self.assertEqual(json.loads(params["mosaicRule"]), slope_aspect.MOSAIC_RULE)
		self.assertIn("LowPS > 5 AND LowPS < 20", slope_aspect.MOSAIC_RULE["where"])
		xmin, ymin, xmax, ymax = (float(v) for v in params["bbox"].split(","))
		west, south = slope_aspect.to_lonlat(xmin, ymin)
		east, north = slope_aspect.to_lonlat(xmax, ymax)
		self.assertLess(west, PARCEL_BOX[0])
		self.assertLess(south, PARCEL_BOX[1])
		self.assertGreater(east, PARCEL_BOX[2])
		self.assertGreater(north, PARCEL_BOX[3])
		# Ten ground metres a cell, whatever the latitude.
		width = int(params["size"].split(",")[0])
		ground = (xmax - xmin) / width * math.cos(math.radians(47.405))
		self.assertAlmostEqual(ground, slope_aspect.NATIVE_GROUND_METRES, delta=0.01)
		self.assertEqual(data["boundaries"], {"parcels": 1, "fields": 2})
		self.assertGreater(data["tiles_prerendered"], 0)
		self.assertEqual(data["products"][0]["publication_date"], "2012-08-01")

	def test_the_layer_it_writes_is_served(self):
		self.a_farm()
		self.fake_usgs()
		self.tool_data("build_slope_aspect_layer")
		meta = slope_aspect.read_meta()
		self.assertIsNotNone(meta)
		x, y = slope_aspect.to_mercator(-120.345, 47.402)
		((tx, ty),) = slope_aspect.tiles_covering((x, y, x, y), 16)
		image = Image.open(io.BytesIO(slope_aspect.tile_png(16, tx, ty)))
		self.assertIsNotNone(image.getbbox())
		# Level 17 is not pre-rendered; it renders on request and is kept.
		((tx, ty),) = slope_aspect.tiles_covering((x, y, x, y), 17)
		kept = os.path.join(slope_aspect.cache_dir(), "tiles", "17", str(tx), f"{ty}.png")
		self.assertFalse(os.path.exists(kept))
		self.assertTrue(Image.open(io.BytesIO(slope_aspect.tile_png(17, tx, ty))).getbbox())
		self.assertTrue(os.path.exists(kept))

	def test_a_tile_far_from_the_farm_is_the_transparent_tile(self):
		self.a_farm()
		self.fake_usgs()
		self.tool_data("build_slope_aspect_layer")
		x, y = slope_aspect.to_mercator(-100.0, 40.0)
		((tx, ty),) = slope_aspect.tiles_covering((x, y, x, y), 14)
		self.assertEqual(slope_aspect.tile_png(14, tx, ty), slope_aspect.TRANSPARENT_TILE)
		self.assertEqual(slope_aspect.tile_png(5, 0, 0), slope_aspect.TRANSPARENT_TILE)

	def test_the_point_query_reads_the_ridge_the_right_way_round(self):
		self.a_farm()
		self.fake_usgs()
		self.tool_data("build_slope_aspect_layer")
		south = self.tool_data("get_slope_aspect_layer", {"latitude": 47.402, "longitude": -120.345})["point"]
		north = self.tool_data("get_slope_aspect_layer", {"latitude": 47.408, "longitude": -120.345})["point"]
		self.assertEqual(south["aspect"], "S")
		self.assertEqual(north["aspect"], "N")
		self.assertAlmostEqual(south["slope_degrees"], math.degrees(math.atan(0.3)), delta=0.6)

	def test_a_dry_run_fetches_nothing_and_writes_nothing(self):
		self.a_farm()
		fake = self.fake_usgs()
		data = self.tool_data("build_slope_aspect_layer", {"dry_run": True})
		self.assertTrue(data["dry_run"])
		self.assertGreater(data["tiles_to_prerender"], 0)
		self.assertEqual(fake.calls, [])
		self.assertIsNone(slope_aspect.read_meta())

	def test_no_boundaries_is_refused_by_name(self):
		fake = self.fake_usgs()
		self.assertIn("has a boundary", self.tool_error("build_slope_aspect_layer"))
		self.assertEqual(fake.calls, [])

	def test_boundaries_in_two_counties_are_refused_before_anything_is_fetched(self):
		names = self.a_farm()
		self.bound("Field", names["North Face"], (-119.0, 46.0, -118.99, 46.01))
		fake = self.fake_usgs()
		self.assertIn("too far apart", self.tool_error("build_slope_aspect_layer"))
		self.assertEqual(fake.calls, [])

	def test_a_usgs_failure_leaves_the_previous_build_standing(self):
		self.a_farm()
		self.fake_usgs()
		first = self.tool_data("build_slope_aspect_layer")["built_at"]
		self.fake_usgs(fail_export=True)
		self.assertIn("HTTP 502", self.tool_error("build_slope_aspect_layer"))
		self.assertEqual(slope_aspect.read_meta()["built_at"], first)

	def test_an_out_of_range_buffer_is_refused(self):
		self.a_farm()
		self.fake_usgs()
		self.assertIn("buffer_metres", self.tool_error("build_slope_aspect_layer", {"buffer_metres": 99999}))

	def test_a_cache_from_another_algorithm_version_is_not_served(self):
		self.a_farm()
		self.fake_usgs()
		self.tool_data("build_slope_aspect_layer")
		path = slope_aspect._meta_path()
		with open(path, encoding="utf-8") as handle:
			meta = json.load(handle)
		meta["algorithm_version"] = slope_aspect.ALGORITHM_VERSION + 1
		with open(path, "w", encoding="utf-8") as handle:
			json.dump(meta, handle)
		self.assertIsNone(slope_aspect.read_meta())
		self.assertIsNone(slope_aspect.tile_png(14, 0, 0))


# ── 6. the ranking ──────────────────────────────────────────────────────────
@needs_numpy
@unittest.skipUnless(HAVE_SHAPELY, "block summaries need shapely")
class TheRanking(SlopeSiteMixin, V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **SWITCHES)
		self.clear_cache()

	def test_before_a_build_the_read_says_not_built_and_fetches_nothing(self):
		fake = self.fake_usgs()
		data = self.tool_data("get_slope_aspect_layer")
		self.assertFalse(data["available"])
		self.assertIn("build_slope_aspect_layer", data["reason"])
		self.assertEqual(data["tile_url_template"], "/farmops/api/tiles/slope_aspect/{z}/{x}/{y}.png")
		self.assertEqual(fake.calls, [])

	def test_the_south_facing_block_ranks_first(self):
		names = self.a_farm()
		self.fake_usgs()
		self.tool_data("build_slope_aspect_layer")
		data = self.tool_data("get_slope_aspect_layer")
		self.assertTrue(data["available"])
		by_name = {row["field"]: row for row in data["blocks"]}
		south, north = by_name[names["South Face"]], by_name[names["North Face"]]
		self.assertEqual(south["earliness_rank"], 1)
		self.assertEqual(north["earliness_rank"], 2)
		self.assertGreater(south["southness_index"], 0.25)
		self.assertLess(north["southness_index"], -0.25)
		self.assertEqual(south["dominant_aspect"], "S")
		self.assertEqual(north["dominant_aspect"], "N")
		self.assertGreater(south["south_facing_share"], 0.9)
		self.assertAlmostEqual(south["mean_slope_degrees"], math.degrees(math.atan(0.3)), delta=1.0)

	def test_the_ranking_narrows_to_a_named_entity(self):
		self.a_farm()
		self.fake_usgs()
		self.tool_data("build_slope_aspect_layer")
		self.assertEqual(self.tool_data("get_slope_aspect_layer", {"company": MAIN})["block_count"], 2)

	def test_latitude_without_longitude_is_refused(self):
		self.assertIn("go together", self.tool_error("get_slope_aspect_layer", {"latitude": 47.4}))

	def test_a_longitude_of_zero_is_a_longitude(self):
		"""The negative control for `_number`: 0 is Greenwich, not "not passed"."""
		data = self.tool_data("get_slope_aspect_layer", {"latitude": 51.48, "longitude": 0})
		self.assertIn("point", data)


# ── 7. the phone ────────────────────────────────────────────────────────────
from .test_api_mobile import WORKER  # noqa: E402
from .test_farmops_api import PREFIX, FarmOpsAPITestCase  # noqa: E402

TILE = f"{PREFIX}/tiles/slope_aspect"
LAYER = f"{PREFIX}/mobile/get_slope_aspect_layer"


@needs_numpy
class TheTileRoute(SlopeSiteMixin, FarmOpsAPITestCase):
	PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

	def setUp(self):
		super().setUp()
		self.clear_cache()

	def build(self):
		self.a_farm()
		self.fake_usgs()
		frappe.local.session = frappe._dict(user="Administrator", data=frappe._dict())
		slope_aspect.build()
		x, y = slope_aspect.to_mercator(-120.345, 47.402)
		((tx, ty),) = slope_aspect.tiles_covering((x, y, x, y), 15)
		return f"{TILE}/15/{tx}/{ty}.png"

	def get(self, path, **kwargs):
		return self.post(path, method="GET", **kwargs)

	def test_an_enrolled_worker_gets_a_png_with_ground_on_it(self):
		path = self.build()
		response = self.get(path)
		self.assertEqual(response.status_code, 200, response.get_data()[:200])
		self.assertEqual(response.headers["Content-Type"], "image/png")
		self.assertIn("max-age", response.headers["Cache-Control"])
		data = response.get_data()
		self.assertTrue(data.startswith(self.PNG_MAGIC))
		self.assertIsNotNone(Image.open(io.BytesIO(data)).getbbox())

	def test_a_tile_off_the_farm_is_transparent_not_an_error(self):
		self.build()
		response = self.get(f"{TILE}/15/0/0.png")
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.get_data(), slope_aspect.TRANSPARENT_TILE)

	def test_never_built_is_a_404_that_says_so(self):
		status, body = self.refusal(f"{TILE}/15/0/0.png", method="GET")
		self.assertEqual(status, 404)
		self.assertIn("build_slope_aspect_layer", body["error"])

	def test_no_credential_is_401_not_a_png(self):
		path = self.build()
		status, body = self.refusal(path, method="GET", credential=False)
		self.assertEqual(status, 401)
		self.assertIn("usable Farm Ops credential", body["error"])

	def test_a_real_credential_with_no_mobile_grant_is_403(self):
		"""v0.175.0: a real credential is a device row; this one sits on a grant
		that is not Active, so it verifies and the grant gate refuses it."""
		path = self.build()
		credential = self.device_credential("Administrator", state="Expired", role="Foreman")
		status, _body = self.refusal(path, method="GET", credential=credential)
		self.assertEqual(status, 403)

	def test_a_revoked_grant_closes_the_tiles_too(self):
		path = self.build()
		grant = frappe.db.get_value("Mobile Access Grant", {"user": WORKER}, "name")
		frappe.db.set_value("Mobile Access Grant", grant, "state", "Revoked")
		status, _body = self.refusal(path, method="GET")
		self.assertEqual(status, 403)

	def test_the_kill_switch_is_503(self):
		path = self.build()
		self.configure(enabled=1, farm_ops_mobile_enabled=0)
		status, _body = self.refusal(path, method="GET")
		self.assertEqual(status, 503)

	def test_post_is_405_and_a_malformed_address_is_404(self):
		path = self.build()
		self.assertEqual(self.refusal(path)[0], 405)
		self.assertEqual(self.refusal(f"{TILE}/15/abc/1.png", method="GET")[0], 404)
		self.assertEqual(self.refusal(f"{TILE}/3/9/0.png", method="GET")[0], 404)

	def test_the_toggle_descriptor_says_not_built_then_built(self):
		before = self.message(LAYER)
		self.assertFalse(before["available"])
		self.build()
		after = self.message(LAYER)
		self.assertTrue(after["available"])
		self.assertEqual(after["min_zoom"], slope_aspect.MIN_ZOOM)
		self.assertEqual(after["max_zoom"], slope_aspect.MAX_ZOOM)
		self.assertLess(after["bounds"]["west"], PARCEL_BOX[0])
		self.assertNotIn("blocks", after)

	@unittest.skipUnless(HAVE_SHAPELY, "block summaries need shapely")
	def test_the_descriptor_ranks_blocks_when_asked(self):
		self.build()
		answer = self.message(LAYER, {"include_blocks": True})
		self.assertEqual(answer["blocks"][0]["field_name"], "South Face")

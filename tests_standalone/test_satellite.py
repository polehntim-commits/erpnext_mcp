# SPDX-License-Identifier: MIT
"""`satellite` — what to ask a provider for, and what an unknown index must not answer.

Cycle 2 of the Farm App retirement. The transport is deliberately not in the
module and so is not tested here; everything that can go wrong QUIETLY is. Four
claims.

1. **AN UNKNOWN INDEX IS AN ERROR AND NOT NDVI.** `NoSilentNdvi`. The farm_app's
   `get_evalscript` ended `scripts.get(metric_type, scripts['ndvi'])`, so a
   caller asking for `"nvdi"` — or for `"moisture"` before that key existed — got
   NDVI, stored it under the name it asked for, and charted a moisture series
   that was secretly greenness. Nothing errored anywhere.

2. **A REQUEST THAT WOULD SUCCEED AND RETURN THE WRONG RASTER IS REFUSED.**
   `ThePayload`. An inverted bbox returns an empty raster and a 200, which is
   the failure that looks like a block with no vegetation.

3. **CLOUD DECIDES, AND NEWEST CLEAR WINS.** `ChoosingAPass`. A five-day-old
   pass at 20% cloud beats a twenty-day-old one at 2%, because the question is
   what the block looks like NOW. When nothing is clear enough, it refuses
   rather than returning the least-bad option.

4. **ONLY NDVI LANDS ON `Field`.** `WhatLandsOnTheBlock`. The three stored
   columns say `ndvi` in their own names, and writing a moisture mean into
   `last_ndvi_mean` would corrupt the one series the anomaly detector reads.
"""

import unittest

from erpnext_mcp import satellite

BOUNDS = (-120.55, 46.55, -120.50, 46.60)


class NoSilentNdvi(unittest.TestCase):
	def test_a_typo_is_refused_by_name(self):
		with self.assertRaises(satellite.SatelliteError) as caught:
			satellite.evalscript("nvdi")
		self.assertIn("nvdi", str(caught.exception))
		self.assertIn("Available:", str(caught.exception))

	def test_the_farm_apps_own_alias_still_resolves(self):
		"""`moisture` is in stored rows and has to keep working — the refusal is
		for names nobody defined, not for the ones the data uses."""
		self.assertEqual(satellite.resolve_metric("moisture"), "ndmi")
		self.assertEqual(satellite.resolve_metric("Red-Edge"), "ndre")

	def test_each_index_declares_its_own_bands(self):
		self.assertEqual(satellite.bands_for("ndvi"), ("B04", "B08"))
		self.assertEqual(satellite.bands_for("ndre"), ("B05", "B08"))
		self.assertEqual(satellite.bands_for("ndmi"), ("B08", "B11"))

	def test_every_evalscript_declares_the_bands_it_uses_and_the_data_mask(self):
		for metric in satellite.METRICS:
			script = satellite.evalscript(metric)
			self.assertIn("//VERSION=3", script)
			self.assertIn("dataMask", script)
			for band in satellite.bands_for(metric):
				self.assertIn(f'"{band}"', script)
				self.assertIn(f"sample.{band}", script)

	def test_two_indices_do_not_produce_the_same_script(self):
		"""The negative control for the silent fallback: if `evalscript` ever
		fell back again, these would be equal."""
		self.assertNotEqual(satellite.evalscript("ndvi"), satellite.evalscript("ndmi"))

	def test_the_index_conversion_round_trips_on_each_indexs_own_range(self):
		for metric in satellite.METRICS:
			for raw in (-0.5, 0.0, 0.42):
				index = satellite.to_index(raw, metric)
				self.assertAlmostEqual(satellite.from_index(index, metric), raw, places=6, msg=metric)

	def test_savi_and_ndvi_land_on_one_scale_so_a_chart_can_carry_both(self):
		self.assertEqual(satellite.to_index(0.0, "ndvi"), 50.0)
		self.assertEqual(satellite.to_index(0.0, "savi"), 50.0)
		self.assertNotEqual(satellite.to_index(0.8, "ndvi"), satellite.to_index(0.8, "savi"))

	def test_what_is_not_a_reading_converts_to_nothing(self):
		for value in (None, "", "x", True):
			self.assertIsNone(satellite.to_index(value), repr(value))


class ThePayload(unittest.TestCase):
	def test_it_carries_the_bbox_the_window_and_the_script(self):
		payload = satellite.request_payload(BOUNDS, "2026-08-01", "2026-08-22")
		self.assertEqual(payload["input"]["bounds"]["bbox"], list(BOUNDS))
		window = payload["input"]["data"][0]["dataFilter"]["timeRange"]
		self.assertEqual(window["from"], "2026-08-01T00:00:00Z")
		self.assertEqual(window["to"], "2026-08-22T23:59:59Z")
		self.assertIn("evaluatePixel", payload["evalscript"])

	def test_the_mosaicking_order_is_stated_rather_than_left_to_the_provider(self):
		payload = satellite.request_payload(BOUNDS, "2026-08-01", "2026-08-22")
		self.assertEqual(payload["input"]["data"][0]["dataFilter"]["mosaickingOrder"], "leastCC")

	def test_an_inverted_bbox_is_refused_rather_than_returning_an_empty_raster(self):
		with self.assertRaises(satellite.SatelliteError) as caught:
			satellite.request_payload((-120.50, 46.55, -120.55, 46.60), "2026-08-01", "2026-08-22")
		self.assertIn("inverted or empty", str(caught.exception))

	def test_a_window_that_runs_backwards_is_refused(self):
		with self.assertRaises(satellite.SatelliteError) as caught:
			satellite.request_payload(BOUNDS, "2026-08-22", "2026-08-01")
		self.assertIn("backwards", str(caught.exception))

	def test_coordinates_that_are_not_degrees_are_refused(self):
		"""A bbox in projected metres is a plausible thing to hand in and would
		return a raster of somewhere else entirely."""
		with self.assertRaises(satellite.SatelliteError):
			satellite.request_payload((600000, 5100000, 601000, 5101000), "2026-08-01", "2026-08-02")

	def test_a_raster_bigger_than_the_provider_takes_is_refused(self):
		with self.assertRaises(satellite.SatelliteError):
			satellite.request_payload(BOUNDS, "2026-08-01", "2026-08-02", size=(9000, 9000))

	def test_a_plan_needs_a_stored_bounding_box_and_says_so(self):
		"""An acreage figure is not a shape on the ground."""
		with self.assertRaises(satellite.SatelliteError) as caught:
			satellite.plan_pull({"name": "Block A4", "acreage": 12.5})
		self.assertIn("Block A4", str(caught.exception))
		self.assertIn("boundary", str(caught.exception))

	def test_a_plan_reads_the_stored_bbox_and_builds_the_window_backwards_from_today(self):
		block = {
			"name": "Block A4",
			"boundary_bbox_geojson": (
				'{"type":"Polygon","coordinates":[[[-120.55,46.55],[-120.5,46.55],'
				"[-120.5,46.6],[-120.55,46.6],[-120.55,46.55]]]}"
			),
		}
		plan = satellite.plan_pull(block, "ndvi", days_back=14, today="2026-08-22")
		self.assertEqual(plan["field"], "Block A4")
		self.assertEqual(plan["start"], "2026-08-08")
		self.assertEqual(plan["end"], "2026-08-22")
		self.assertEqual([round(value, 2) for value in plan["bounds"]], [-120.55, 46.55, -120.5, 46.6])


class ChoosingAPass(unittest.TestCase):
	PASSES = (
		{"date": "2026-08-21", "cloud_cover": 88},
		{"date": "2026-08-20", "cloud_cover": 12},
		{"date": "2026-08-15", "cloud_cover": 2},
	)

	def test_newest_clear_wins_over_older_and_clearer(self):
		"""An older cleaner image answers a question about a different week."""
		chosen = satellite.pick_acquisition(self.PASSES, now="2026-08-22")
		self.assertEqual(chosen["chosen"]["date"], "2026-08-20")
		self.assertIn("12% cloud", chosen["reason"])

	def test_nothing_clear_enough_refuses_rather_than_returning_the_least_bad(self):
		"""Storing a 90%-cloud pass would chart the cloud as a crop decline, and
		the anomaly detector would raise a scouting task on the weather."""
		verdict = satellite.pick_acquisition([self.PASSES[0]], now="2026-08-22")
		self.assertIsNone(verdict["chosen"])
		self.assertIn("weather fact about the block", verdict["reason"])

	def test_every_rejected_pass_carries_its_reason(self):
		verdict = satellite.pick_acquisition(self.PASSES, now="2026-08-22")
		reasons = {row["date"]: row["why"] for row in verdict["rejected"]}
		self.assertIn("88% cloud", reasons["2026-08-21"])

	def test_a_pass_older_than_the_horizon_is_dropped(self):
		verdict = satellite.pick_acquisition([{"date": "2026-06-01", "cloud_cover": 0}], now="2026-08-22")
		self.assertIsNone(verdict["chosen"])
		self.assertIn("older than 30 days", verdict["rejected"][0]["why"])

	def test_a_pass_with_no_cloud_figure_is_not_a_clear_pass(self):
		verdict = satellite.pick_acquisition([{"date": "2026-08-20"}], now="2026-08-22")
		self.assertIsNone(verdict["chosen"])
		self.assertIn("unmeasured pass", verdict["rejected"][0]["why"])

	def test_a_pass_dated_in_the_future_is_dropped(self):
		verdict = satellite.pick_acquisition([{"date": "2026-09-20", "cloud_cover": 0}], now="2026-08-22")
		self.assertIsNone(verdict["chosen"])

	def test_two_orbits_on_one_day_go_to_the_clearer(self):
		same_day = [{"date": "2026-08-20", "cloud_cover": 25}, {"date": "2026-08-20", "cloud_cover": 4}]
		self.assertEqual(satellite.pick_acquisition(same_day, now="2026-08-22")["chosen"]["cloud_cover"], 4)

	def test_the_cloud_limit_is_the_callers(self):
		"""Thirty percent over a whole tile routinely means zero over one
		twenty-acre block."""
		verdict = satellite.pick_acquisition([self.PASSES[0]], max_cloud_pct=95, now="2026-08-22")
		self.assertIsNotNone(verdict["chosen"])

	def test_no_passes_at_all_is_a_refusal_and_not_a_crash(self):
		self.assertIsNone(satellite.pick_acquisition([])["chosen"])


class WhatLandsOnTheBlock(unittest.TestCase):
	def test_a_masked_pixel_is_dropped_and_not_scored_as_zero(self):
		"""A masked pixel scored as 0.0 pulls a block's mean towards bare ground
		in proportion to how much of it was under cloud."""
		summary = satellite.summarise_pixels([0.7, 0.75, 0.8, None, "x", 5.0, float("nan")])
		self.assertEqual(summary["count"], 3)
		self.assertAlmostEqual(summary["mean"], 0.75)

	def test_the_standard_deviation_is_the_population_one(self):
		"""These are all the pixels in the block, not a sample of them."""
		summary = satellite.summarise_pixels([0.7, 0.8])
		self.assertAlmostEqual(summary["stddev"], 0.05, places=6)

	def test_a_raster_with_nothing_usable_answers_none_rather_than_zero(self):
		summary = satellite.summarise_pixels(["x", None])
		self.assertEqual(summary["count"], 0)
		self.assertIsNone(summary["mean"])

	def test_ndvi_writes_the_three_columns(self):
		update = satellite.field_update(satellite.summarise_pixels([0.7, 0.8]), "2026-08-20")
		self.assertEqual(update["last_ndvi_mean"], 0.75)
		self.assertEqual(update["last_ndvi_pull_date"], "2026-08-20")
		self.assertEqual(update["satellite_provider"], satellite.DATA_COLLECTION)

	def test_any_other_index_writes_nothing_at_all(self):
		"""The claim this class exists for: a moisture mean in `last_ndvi_mean`
		corrupts the series the anomaly detector reads."""
		summary = satellite.summarise_pixels([0.3, 0.4], "ndmi")
		self.assertEqual(satellite.field_update(summary, "2026-08-20", metric="ndmi"), {})

	def test_a_pull_that_found_nothing_writes_nothing(self):
		self.assertEqual(satellite.field_update(satellite.summarise_pixels([]), "2026-08-20"), {})

	def test_the_uint16_decode_is_the_inverse_of_the_encoding(self):
		self.assertAlmostEqual(satellite.decode_uint16(65535), 1.0, places=6)
		self.assertAlmostEqual(satellite.decode_uint16(0), -1.0, places=6)
		self.assertAlmostEqual(satellite.decode_uint16(32767.5), 0.0, places=6)

	def test_the_uint16_decode_knows_which_index_it_is_decoding(self):
		"""SAVI's range is wider, so the same 16-bit pixel is a different value."""
		self.assertNotEqual(satellite.decode_uint16(1000), satellite.decode_uint16(1000, "savi"))


if __name__ == "__main__":  # pragma: no cover
	unittest.main()

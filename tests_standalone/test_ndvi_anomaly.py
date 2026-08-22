# SPDX-License-Identifier: MIT
"""`ndvi_anomaly` — deciding whether a greenness drop is worth a scout's morning.

Cycle 2 of the Farm App retirement. The decision is cheap to get wrong in both
directions and the guards below each suppressed a real false alarm on the
farm_app. Four claims.

1. **THE TWO INDEX SCALES DO NOT PRODUCE TWO ANSWERS.** `EitherScale`. This app
   stores raw NDVI (-1..1) on `Field.last_ndvi_mean`; the farm_app stored a
   0-100 index. A relative drop computed on raw NDVI is unsound near zero and
   meaningless across it — 0.05 → 0.01 is an 80% "drop" on bare ground where
   nothing happened. Every comparison is made on the index, and the negative
   control is that the raw and indexed forms of the same reading agree.

2. **EACH GUARD SUPPRESSES, AND SAYS WHY.** `TheGuards`. A stale baseline, a
   block already flagged, a baseline with no canopy, and a crop that is
   supposed to be yellowing. `explain()` names which one fired, because "no
   alert" is not an answer anybody can act on.

3. **SEVERITY IS A THRESHOLD AND NOT A GRADIENT.** `Severity`. Nothing returns
   `low`: a drop under the threshold produces no alert at all rather than a
   quiet one, because a quiet alert is a queue entry nobody reads.

4. **THE TASK TEXT SAYS ENOUGH TO WALK THE BLOCK ON.** `TheMessage`. Both
   figures, both dates, the percentage — the scout needs to know how big a
   change to look for and when it started.
"""

import unittest
from datetime import datetime

from erpnext_mcp import ndvi_anomaly

BASELINE = datetime(2026, 8, 1)
LATEST = datetime(2026, 8, 8)


class EitherScale(unittest.TestCase):
	def test_raw_ndvi_and_the_index_are_the_same_reading(self):
		self.assertEqual(ndvi_anomaly.to_index(0.8), 90.0)
		self.assertEqual(ndvi_anomaly.to_index(90), 90.0)
		self.assertEqual(ndvi_anomaly.to_index(-1.0), 0.0)
		self.assertEqual(ndvi_anomaly.to_index(0.0), 50.0)

	def test_the_same_drop_reads_the_same_on_both_scales(self):
		raw = ndvi_anomaly.evaluate(0.8, BASELINE, 0.5, LATEST)
		indexed = ndvi_anomaly.evaluate(90, BASELINE, 75, LATEST)
		self.assertEqual(raw["drop_pct"], indexed["drop_pct"])
		self.assertEqual(raw["severity"], indexed["severity"])

	def test_a_relative_drop_is_never_computed_on_bare_ground(self):
		"""0.05 → 0.01 is an 80% fall in raw NDVI and nothing at all on the
		ground. On the index those are 52.5 and 50.5 — a 4% change, under the
		threshold — which is the whole reason the conversion happens first."""
		self.assertIsNone(ndvi_anomaly.evaluate(0.05, BASELINE, 0.01, LATEST))

	def test_a_negative_baseline_has_nothing_to_be_a_percentage_of(self):
		verdict = ndvi_anomaly.explain(-0.95, BASELINE, -0.99, LATEST)
		self.assertFalse(verdict["alert"])
		self.assertIn("below the 5", verdict["reason"])

	def test_what_is_not_a_reading_reads_as_nothing(self):
		for value in (None, "", "n/a", True, float("nan"), 250):
			self.assertIsNone(ndvi_anomaly.to_index(value), repr(value))

	def test_a_missing_reading_produces_no_alert_and_says_so(self):
		self.assertIsNone(ndvi_anomaly.evaluate(None, BASELINE, 0.5, LATEST))
		self.assertIn("missing", ndvi_anomaly.explain(None, BASELINE, 0.5, LATEST)["reason"])


class TheGuards(unittest.TestCase):
	def test_a_real_drop_alerts(self):
		verdict = ndvi_anomaly.evaluate(0.8, BASELINE, 0.5, LATEST)
		self.assertEqual(verdict["gap_days"], 7)
		self.assertGreater(verdict["drop_pct"], ndvi_anomaly.DROP_PCT)

	def test_a_stale_baseline_is_a_season_and_not_an_event(self):
		verdict = ndvi_anomaly.explain(0.8, BASELINE, 0.5, datetime(2026, 9, 1))
		self.assertFalse(verdict["alert"])
		self.assertIn("31 days apart", verdict["reason"])

	def test_a_block_already_flagged_this_week_is_not_flagged_again(self):
		"""The decline that triggered Monday's alert is still there on Thursday.
		Without this a single event produces an alert per satellite pass."""
		verdict = ndvi_anomaly.explain(
			0.8, BASELINE, 0.5, LATEST, last_alert_ts=datetime(2026, 8, 6), now=LATEST
		)
		self.assertFalse(verdict["alert"])
		self.assertIn("quiet period", verdict["reason"])

	def test_an_old_alert_no_longer_suppresses(self):
		verdict = ndvi_anomaly.explain(
			0.8, BASELINE, 0.5, LATEST, last_alert_ts=datetime(2026, 7, 1), now=LATEST
		)
		self.assertTrue(verdict["alert"])

	def test_a_ripening_block_is_supposed_to_be_losing_greenness(self):
		"""Stages 8x and 9x are ripening and senescence. An alert raised on
		those sends somebody to look at a block that is on schedule."""
		for stage in ("87", "BBCH 87", "92", 8):
			verdict = ndvi_anomaly.explain(0.8, BASELINE, 0.5, LATEST, growth_stage=stage)
			self.assertFalse(verdict["alert"], stage)
			self.assertIn("expected to fall", verdict["reason"])

	def test_a_flowering_block_is_not_suppressed(self):
		"""The negative control for the stage guard: 65 is full flowering, where
		a 37% fall in a week is exactly what somebody should go and look at."""
		self.assertTrue(ndvi_anomaly.explain(0.8, BASELINE, 0.5, LATEST, growth_stage="65")["alert"])

	def test_an_unreadable_stage_does_not_suppress(self):
		"""A missed real drop costs more than a walked healthy block, so a stage
		column nobody can parse gets the alert."""
		self.assertTrue(ndvi_anomaly.explain(0.8, BASELINE, 0.5, LATEST, growth_stage="petal fall")["alert"])

	def test_readings_in_the_wrong_order_are_refused(self):
		verdict = ndvi_anomaly.explain(0.8, LATEST, 0.5, BASELINE)
		self.assertFalse(verdict["alert"])
		self.assertIn("not newer", verdict["reason"])

	def test_a_naive_and_an_aware_timestamp_compare_without_raising(self):
		"""Frappe hands back naive datetimes and a caller may hand in an aware
		one. Comparing the two raises, which would take out the sweep."""
		verdict = ndvi_anomaly.explain(0.8, "2026-08-01 00:00:00", 0.5, "2026-08-08T00:00:00Z")
		self.assertTrue(verdict["alert"])

	def test_a_date_only_timestamp_still_parses(self):
		self.assertTrue(ndvi_anomaly.explain(0.8, "2026-08-01", 0.5, "2026-08-08")["alert"])

	def test_evaluate_says_nothing_about_which_guard_fired(self):
		"""Deliberate: the reasons are not ranked and a caller branching on them
		would be re-implementing this decision one guard at a time."""
		self.assertIsNone(ndvi_anomaly.evaluate(0.8, BASELINE, 0.5, datetime(2026, 9, 1)))


class Severity(unittest.TestCase):
	def test_thirty_percent_is_where_high_begins(self):
		self.assertEqual(ndvi_anomaly.severity_for_drop(0.29), "medium")
		self.assertEqual(ndvi_anomaly.severity_for_drop(0.30), "high")

	def test_a_drop_under_the_threshold_produces_no_alert_at_all(self):
		verdict = ndvi_anomaly.explain(0.8, BASELINE, 0.78, LATEST)
		self.assertFalse(verdict["alert"])
		self.assertIsNone(verdict["severity"])
		self.assertIn("under the 15% threshold", verdict["reason"])

	def test_nothing_ever_returns_low(self):
		for drop in (0.15, 0.2, 0.3, 0.9):
			self.assertIn(ndvi_anomaly.severity_for_drop(drop), ("medium", "high"))


class TheMessage(unittest.TestCase):
	def test_it_names_the_block_the_figures_and_the_dates(self):
		text = ndvi_anomaly.message("Block A4", 0.8, BASELINE, 0.5, LATEST, 0.1667)
		self.assertIn("Block A4", text)
		self.assertIn("90.0", text)
		self.assertIn("75.0", text)
		self.assertIn("2026-08-01", text)
		self.assertIn("2026-08-08", text)

	def test_the_figures_are_quoted_on_the_index_whatever_came_in(self):
		"""Two tasks raised from two sources have to be comparable to the person
		reading them."""
		raw = ndvi_anomaly.message("A", 0.8, BASELINE, 0.5, LATEST, 0.1667)
		indexed = ndvi_anomaly.message("A", 90, BASELINE, 75, LATEST, 0.1667)
		self.assertEqual(raw, indexed)

	def test_an_unnamed_block_still_produces_a_readable_task(self):
		self.assertIn("an unnamed block", ndvi_anomaly.message("", 0.8, BASELINE, 0.5, LATEST, 0.16))


if __name__ == "__main__":  # pragma: no cover
	unittest.main()

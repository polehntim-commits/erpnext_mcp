# SPDX-License-Identifier: MIT
"""`simulation` — heat, chill, frost and heat stress from a run of temperatures.

Cycle 2 of the Farm App retirement. The farm_app's version of this could not be
tested at all — it reached into its weather module, its ORM and numpy, and its
`integrate_pest_boost` called a function that does not exist in the file.
Nothing caught that, because nothing could run it. Four claims.

1. **THE THRESHOLD IS CELSIUS AND THE SERIES CAN BE ANYTHING.** `UnitsAreCarried`.
   An earlier draft of this module converted the threshold along with the
   series, so `heat(fahrenheit, threshold_c=35, unit="F")` compared against
   1.7°C and reported every hour of the summer as heat stress. That is the
   negative control below.

2. **DEGREE DAYS MATCH THE METHOD THE PEST TABLES WERE BUILT WITH.**
   `DegreeDays`. Averaging with an upper cutoff, both extremes clamped BEFORE
   the mean. Using single-sine instead shifts accumulations 5-10% against the
   biofix dates `ipm_reference` publishes — the difference between spraying for
   codling moth on the right day and the wrong one.

3. **CHILL IS TWO MODELS AND THEY DISAGREE ON PURPOSE.** `Chill`. Weinberger
   counts hours in a band; Utah gives partial credit and DEBITS warm hours. A
   warm January moves the two figures in opposite directions, which is the whole
   reason an orchardist looks at both.

4. **EXPOSURE IS DEPTH AS WELL AS DURATION.** `Exposure`. Bloom damage is a
   function of both and neither alone predicts it.
"""

import unittest

from erpnext_mcp import simulation


class UnitsAreCarried(unittest.TestCase):
	def test_the_threshold_is_celsius_whatever_the_series_is_in(self):
		"""The regression. 95, 100 and 80°F are 35.0, 37.8 and 26.7°C, so
		exactly one hour is above 35°C. The bug reported all three."""
		exposed = simulation.heat([95, 100, 80], threshold_c=35, unit="F")
		self.assertEqual(exposed["hours"], 1)
		self.assertEqual(exposed["threshold_c"], 35.0)

	def test_frost_reads_a_fahrenheit_series_against_freezing(self):
		exposed = simulation.frost([28, 35, 20], threshold_c=0, unit="F")
		self.assertEqual(exposed["hours"], 2)
		self.assertAlmostEqual(exposed["extreme"], -6.67, places=2)

	def test_a_column_that_names_its_own_unit_outranks_the_argument(self):
		"""`temperature_f` says Fahrenheit in its own name. A row carrying it
		must not be read as Celsius because the caller passed a default."""
		self.assertEqual(simulation.readings([{"temperature_f": 50}], unit="C"), [10.0])
		self.assertEqual(simulation.readings([{"temperature_c": 10}], unit="F"), [10.0])

	def test_a_unit_this_does_not_know_is_refused(self):
		with self.assertRaises(simulation.SimulationError):
			simulation.to_celsius(10, "kelvin")

	def test_what_is_not_a_temperature_is_dropped_rather_than_zeroed(self):
		self.assertEqual(simulation.readings([10, None, "x", True, float("nan"), 12]), [10.0, 12.0])

	def test_an_absurdly_long_series_is_refused_rather_than_summed(self):
		"""Over two years of hourly data in one call is a caller that meant to
		filter by date and did not."""
		with self.assertRaises(simulation.SimulationError):
			simulation.readings([10.0] * (simulation.MAX_HOURS + 2))


class DegreeDays(unittest.TestCase):
	def test_the_upper_cutoff_clamps_the_extremes_before_the_mean(self):
		"""18-38°C with a 30°C cutoff and a 10°C base is (18+30)/2 − 10 = 14,
		NOT (18+38)/2 − 10 = 18. Clamping the result instead of the inputs is
		the version that gets this wrong."""
		self.assertEqual(simulation.growing_degree_days(18, 38), 14.0)

	def test_a_cold_day_contributes_nothing_rather_than_a_negative(self):
		self.assertEqual(simulation.growing_degree_days(5, 9), 0.0)

	def test_the_base_is_the_callers_and_the_pest_tables_each_state_their_own(self):
		self.assertEqual(simulation.growing_degree_days(10, 20, base_c=7.2), 7.8)

	def test_extremes_the_wrong_way_round_are_read_rather_than_refused(self):
		"""A station that reported its min and max swapped is a data problem
		with an obvious right answer."""
		self.assertEqual(simulation.growing_degree_days(38, 18), simulation.growing_degree_days(18, 38))

	def test_an_accumulation_keeps_the_day_a_threshold_was_crossed(self):
		"""How many is rarely useful without when — a biofix model is asked for
		the date."""
		run = simulation.accumulate_gdd(
			[{"date": "2026-05-01", "min": 10, "max": 20}, {"date": "2026-05-02", "min": 12, "max": 24}]
		)
		self.assertEqual(run["total"], 13.0)
		self.assertEqual(run["daily"][0], {"date": "2026-05-01", "gdd": 5.0})

	def test_pairs_work_as_well_as_dicts(self):
		self.assertEqual(simulation.accumulate_gdd([(10, 20), (12, 24)])["total"], 13.0)

	def test_a_day_that_cannot_be_read_is_skipped_and_counted(self):
		run = simulation.accumulate_gdd([(10, 20), "not a day", {"min": None, "max": None}])
		self.assertEqual(run["days"], 1)
		self.assertEqual(run["skipped"], 2)


class Chill(unittest.TestCase):
	def test_weinberger_counts_hours_inside_the_band_and_its_endpoints(self):
		self.assertEqual(simulation.chill_hours([1, 3, 7, 7.2, 7.3, -1, 0]), 5.0)

	def test_below_freezing_does_not_count_as_chill(self):
		"""The band starts at 0°C. Hours below it do nothing for dormancy and
		counting them would overstate a cold winter's chill."""
		self.assertEqual(simulation.chill_hours([-5, -1, 3]), 1.0)

	def test_utah_debits_warm_winter_hours(self):
		"""The claim the second model exists for: a warm spell REMOVES chill the
		orchard had already banked, so the two models move in opposite
		directions on the same series."""
		cold = [5.0] * 10
		warm = cold + [20.0] * 6
		self.assertEqual(simulation.chill_hours(cold), simulation.chill_hours(warm))
		self.assertLess(simulation.chill_units(warm), simulation.chill_units(cold))

	def test_a_warm_enough_winter_can_bank_negative_chill(self):
		self.assertLess(simulation.chill_units([22.0] * 20), 0)

	def test_the_utah_bands_give_full_credit_in_the_middle(self):
		self.assertEqual(simulation.chill_units([5.0] * 10), 10.0)
		self.assertEqual(simulation.chill_units([1.0] * 10), 0.0)


class Exposure(unittest.TestCase):
	def test_it_reports_duration_and_depth_and_the_extreme(self):
		exposed = simulation.frost([2, -1, -3, 0.5])
		self.assertEqual(exposed["hours"], 2)
		self.assertEqual(exposed["extreme"], -3.0)
		self.assertEqual(exposed["mean_excess"], 2.0)

	def test_two_nights_of_equal_duration_differ_by_depth(self):
		"""Which is why duration alone does not predict bloom damage."""
		shallow = simulation.frost([-0.5, -0.5])
		deep = simulation.frost([-6.0, -6.0])
		self.assertEqual(shallow["hours"], deep["hours"])
		self.assertLess(shallow["mean_excess"], deep["mean_excess"])

	def test_a_series_that_never_crossed_reports_zero_and_still_names_the_extreme(self):
		exposed = simulation.frost([2, 3, 4])
		self.assertEqual(exposed["hours"], 0)
		self.assertEqual(exposed["extreme"], 2.0)
		self.assertEqual(exposed["mean_excess"], 0.0)

	def test_an_empty_series_is_zeroes_rather_than_an_error(self):
		self.assertEqual(simulation.frost([])["hours"], 0)
		self.assertIsNone(simulation.frost([])["extreme"])

	def test_a_direction_this_does_not_know_is_refused(self):
		with self.assertRaises(simulation.SimulationError):
			simulation.exposure([1, 2], 0, "sideways")


class TheSummary(unittest.TestCase):
	def test_it_answers_all_four_models_over_one_series(self):
		summary = simulation.summarise([float(hour % 20) for hour in range(48)])
		for key in ("chill_hours", "chill_units", "frost", "heat", "gdd_estimated", "mean_c"):
			self.assertIn(key, summary)
		self.assertEqual(summary["readings"], 48)
		self.assertEqual(summary["gdd_blocks"], 2)

	def test_it_refuses_a_series_with_nothing_in_it(self):
		with self.assertRaises(simulation.SimulationError):
			simulation.summarise([None, "x"])

	def test_the_degree_day_figure_is_labelled_an_estimate(self):
		"""It groups the series in 24-hour blocks in the order given, which is
		right for a genuinely hourly series and wrong for anything else — so it
		is not called `gdd`."""
		summary = simulation.summarise([15.0] * 24)
		self.assertIn("gdd_estimated", summary)
		self.assertNotIn("gdd", summary)


if __name__ == "__main__":  # pragma: no cover
	unittest.main()

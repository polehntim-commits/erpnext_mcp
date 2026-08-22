# SPDX-License-Identifier: MIT
"""`nutrient_calcs` — the fertiliser label, the oxide trap, and what is missing from a total.

Cycle 2 of the Farm App retirement. Three claims, and the first one is the
reason this module is longer than the farm_app's.

1. **THE LABEL IS IN OXIDES AND THE AGRONOMY IS NOT.** `TheOxideTrap`. A grade
   `10-20-10` is 10% N, 20% **P₂O₅** and 10% **K₂O**. Reporting the label
   figures as elemental over-states phosphorus by a factor of 2.3, and
   phosphorus is the nutrient with the discharge limit on it. Both bases come
   back and both are asserted against hand-worked figures.

2. **WHAT COULD NOT BE READ IS REPORTED, NOT COUNTED AS ZERO.** `Unaccounted`.
   A nutrient plan that silently treats an unreadable label as clean
   under-reports, and under-reporting is the direction that gets a farm fined.

3. **A UNIT IS NEVER GUESSED.** `Units`. Every conversion bug this module could
   have is a factor of 1.12 or 2.2 that looks plausible on a report, so an
   unknown unit is refused by name and a liquid rate refuses without a density.
"""

import unittest

from erpnext_mcp import nutrient_calcs as nutrients

ACRE_M2 = nutrients.M2_PER_ACRE


class ReadingTheLabel(unittest.TestCase):
	def test_a_grade_is_read_out_of_a_product_name(self):
		self.assertEqual(nutrients.parse_grade("Urea 46-0-0 (50lb)"), (46.0, 0.0, 0.0))
		self.assertEqual(nutrients.parse_grade("46-0-0"), (46.0, 0.0, 0.0))
		self.assertEqual(nutrients.parse_grade("10 - 20 - 10"), (10.0, 20.0, 10.0))
		self.assertEqual(nutrients.parse_grade("10/20/10"), (10.0, 20.0, 10.0))

	def test_a_fourth_figure_is_ignored_rather_than_refused(self):
		"""`12-12-12-2S` is a real bag. The first three numbers are still the
		grade."""
		self.assertEqual(nutrients.parse_grade("12-12-12-2S"), (12.0, 12.0, 12.0))

	def test_a_decimal_grade_is_real_and_parses(self):
		self.assertEqual(nutrients.parse_grade("0.5-0-0 foliar"), (0.5, 0.0, 0.0))

	def test_a_date_is_not_a_grade(self):
		"""Three numbers separated by dashes. The two-digit bound on each figure
		is what keeps a receiving date out of a nutrient plan."""
		self.assertIsNone(nutrients.parse_grade("2026-08-01"))

	def test_three_numbers_that_cannot_be_a_bag_are_not_a_grade(self):
		"""A bag cannot be more than 100% fertiliser, so `55/60/70` is a lot
		code somebody wrote in the name field."""
		self.assertIsNone(nutrients.parse_grade("lot 55/60/70"))

	def test_nothing_readable_is_none_and_not_a_zero_grade(self):
		for text in ("Compost", "", None, "10-10"):
			self.assertIsNone(nutrients.parse_grade(text), repr(text))

	def test_an_explicit_field_beats_the_item_name(self):
		"""An operator who typed a grade into a field meant it to win over
		whatever is in the item's name."""
		product = {"item_name": "Urea 46-0-0", "npk_grade": "34-0-0"}
		self.assertEqual(nutrients.grade_of(product), (34.0, 0.0, 0.0))

	def test_a_site_with_no_npk_fields_still_reads_the_name(self):
		"""Which is every site today — and is what makes this usable before any
		schema change ships."""
		self.assertEqual(nutrients.grade_of({"item_name": "Triple 15 15-15-15"}), (15.0, 15.0, 15.0))

	def test_separate_percent_columns_are_read_when_present(self):
		product = {"npk_nitrogen": 21, "npk_phosphorus": 0, "npk_potassium": 0, "item_name": "AMS"}
		self.assertEqual(nutrients.grade_of(product), (21.0, 0.0, 0.0))

	def test_an_object_works_as_well_as_a_dict(self):
		class Item:
			item_name = "Urea 46-0-0"

		self.assertEqual(nutrients.grade_of(Item()), (46.0, 0.0, 0.0))


class TheOxideTrap(unittest.TestCase):
	def test_a_hundred_pounds_of_urea_is_forty_six_pounds_of_nitrogen(self):
		"""The figure any grower can check in their head, which is why it is the
		first assertion here."""
		applied = nutrients.nutrients(100, "lb/acre", (46, 0, 0), ACRE_M2 * 10)
		self.assertAlmostEqual(applied["n_lb_acre"], 46.0, places=4)
		self.assertAlmostEqual(applied["n_lb"], 460.0, places=3)

	def test_the_label_figure_is_p2o5_and_the_elemental_figure_is_less_than_half(self):
		applied = nutrients.nutrients(200, "lb/acre", (10, 20, 10), ACRE_M2)
		self.assertAlmostEqual(applied["p2o5_lb_acre"], 40.0, places=4)
		self.assertAlmostEqual(applied["p_lb_acre"], 40.0 * nutrients.P2O5_TO_P, places=4)
		self.assertLess(applied["p_lb_acre"], applied["p2o5_lb_acre"] / 2)

	def test_potassium_converts_too_and_by_a_different_factor(self):
		applied = nutrients.nutrients(200, "lb/acre", (10, 20, 10), ACRE_M2)
		self.assertAlmostEqual(applied["k2o_lb_acre"], 20.0, places=4)
		self.assertAlmostEqual(applied["k_lb_acre"], 20.0 * nutrients.K2O_TO_K, places=4)

	def test_nitrogen_has_no_oxide_twin_because_the_label_figure_is_elemental(self):
		applied = nutrients.nutrients(100, "lb/acre", (46, 0, 0))
		self.assertNotIn("n2o5_lb_acre", applied)
		self.assertIn("n_lb_acre", applied)

	def test_totals_appear_only_when_an_area_is_given(self):
		self.assertNotIn("n_lb", nutrients.nutrients(100, "lb/acre", (46, 0, 0)))
		self.assertIn("n_lb", nutrients.nutrients(100, "lb/acre", (46, 0, 0), ACRE_M2))

	def test_a_product_with_no_grade_is_refused_rather_than_computed_as_zero(self):
		with self.assertRaises(nutrients.NutrientError):
			nutrients.nutrients(100, "lb/acre", None, ACRE_M2)


class Units(unittest.TestCase):
	def test_the_four_solid_units_convert(self):
		one_acre_of_kg = nutrients.convert_rate(1, "lb/acre")
		self.assertAlmostEqual(one_acre_of_kg * ACRE_M2 * nutrients.LB_PER_KG, 1.0, places=6)
		self.assertAlmostEqual(nutrients.convert_rate(1, "kg/ha") * 10000, 1.0, places=6)
		self.assertAlmostEqual(nutrients.convert_rate(2, "kg/m2"), 2.0)
		self.assertAlmostEqual(nutrients.convert_rate(1, "ton/acre"), one_acre_of_kg * 2000, places=9)

	def test_the_spellings_an_operator_types_are_understood(self):
		for spelling in ("lb/acre", "LB/ACRE", "lbs/acre", "lb/ac", " lb / acre "):
			self.assertAlmostEqual(
				nutrients.convert_rate(100, spelling), nutrients.convert_rate(100, "lb/acre"), places=12
			)

	def test_a_unit_this_does_not_convert_is_refused_by_name(self):
		with self.assertRaises(nutrients.NutrientError) as caught:
			nutrients.convert_rate(100, "bushels/rood")
		self.assertIn("bushels/rood", str(caught.exception))
		self.assertIn("Understood:", str(caught.exception))

	def test_a_liquid_rate_refuses_without_a_density(self):
		"""A gallon of 32% UAN is 1.32 kg/L, not water's 1.0 — converting at
		water's density is wrong by 28%."""
		with self.assertRaises(nutrients.NutrientError) as caught:
			nutrients.convert_rate(10, "gal/acre")
		self.assertIn("density", str(caught.exception))

	def test_a_liquid_rate_with_a_density_converts(self):
		litres_per_m2 = 3.785411784 * 10 / ACRE_M2
		self.assertAlmostEqual(nutrients.convert_rate(10, "gal/acre", 1.32), litres_per_m2 * 1.32, places=12)

	def test_the_conversions_round_trip(self):
		self.assertAlmostEqual(nutrients.lb_to_kg(nutrients.kg_to_lb(5)), 5.0, places=10)
		self.assertAlmostEqual(nutrients.m2_to_acres(nutrients.acres_to_m2(3)), 3.0, places=10)

	def test_per_acre_over_no_area_is_zero_rather_than_a_division(self):
		"""An aggregate over applications that carried no area is a legitimate
		thing to ask for."""
		self.assertEqual(nutrients.per_acre(100, 0), 0.0)
		self.assertEqual(nutrients.per_acre(100, None), 0.0)


class Unaccounted(unittest.TestCase):
	def plan(self):
		return [
			{"label": "urea", "rate": 100, "unit": "lb/acre", "grade": (46, 0, 0), "area_m2": ACRE_M2 * 10},
			{"label": "mystery", "rate": 50, "unit": "lb/acre", "product": {"item_name": "Compost"}},
			{"label": "liquid", "rate": 10, "unit": "gal/acre", "grade": (32, 0, 0), "area_m2": ACRE_M2},
		]

	def test_the_readable_application_is_counted(self):
		total = nutrients.aggregate(self.plan())
		self.assertAlmostEqual(total["n_lb"], 460.0, places=2)
		self.assertAlmostEqual(total["area_acres"], 10.0, places=4)

	def test_both_kinds_of_unreadable_are_named_with_their_reason(self):
		total = nutrients.aggregate(self.plan())
		labels = {row["label"] for row in total["unaccounted"]}
		self.assertEqual(labels, {"mystery", "liquid"})
		reasons = {row["label"]: row["reason"] for row in total["unaccounted"]}
		self.assertIn("no readable NPK grade", reasons["mystery"])
		self.assertIn("density", reasons["liquid"])

	def test_an_unreadable_product_contributes_nothing_to_the_totals(self):
		"""The claim the whole class exists for: the totals over a plan with two
		unreadable rows equal the totals over the one readable row alone."""
		full = nutrients.aggregate(self.plan())
		readable_only = nutrients.aggregate(self.plan()[:1])
		self.assertEqual(full["n_lb"], readable_only["n_lb"])
		self.assertEqual(full["p_lb"], readable_only["p_lb"])

	def test_the_per_acre_figure_uses_the_area_that_was_actually_counted(self):
		total = nutrients.aggregate(self.plan())
		self.assertAlmostEqual(total["n_lb_acre"], 46.0, places=2)

	def test_an_empty_plan_is_zeroes_rather_than_an_error(self):
		total = nutrients.aggregate([])
		self.assertEqual(total["n_lb"], 0.0)
		self.assertEqual(total["applications"], [])
		self.assertEqual(total["unaccounted"], [])

	def test_a_grade_is_read_off_the_product_when_none_is_given(self):
		total = nutrients.aggregate(
			[{"rate": 100, "unit": "lb/acre", "product": {"item_name": "Urea 46-0-0"}, "area_m2": ACRE_M2}]
		)
		self.assertEqual(total["unaccounted"], [])
		self.assertAlmostEqual(total["n_lb"], 46.0, places=3)


if __name__ == "__main__":  # pragma: no cover
	unittest.main()

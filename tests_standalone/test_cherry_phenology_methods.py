# SPDX-License-Identifier: MIT
"""Cherry phenology methods per variety and block. v0.310.0 (Tim, 2026-10-10; supersedes the 40 °F default).

Default: March 1 at 2 °C (Kaiser, Coop & Meland, OSU 2014 — 'Van' full bloom at 254 °C·day). Alternates: Jan 1 at
4.4 °C / 40 °F, and starting after the chill requirement is met. A block's method is `by_block`, else its variety's
`by_variety`, else the default — and every answer carries the method it used. Pest models keep their own bases.
"""

import datetime
from unittest import mock

from erpnext_mcp import degree_days, pest_dd

from .test_degree_days import DegreeDayTestCase

CHERRY = pest_dd.DEFAULT_PHENOLOGY["crops"]["sweet cherry"]


def with_row(**changes):
	return {**pest_dd.DEFAULT_PHENOLOGY, "crops": {"sweet cherry": {**CHERRY, **changes}}}


class ChoosingTheMethod(DegreeDayTestCase):
	def test_van_on_the_default_method_has_its_published_bloom(self):
		entry = degree_days.crop_entry("Sweet Cherry", "Van")
		self.assertEqual(entry["stages"], [(457.2, "65")], "254 °C·day = 457.2 °F·day")
		self.assertIn("variety Van", entry["stages_source"])

	def test_a_variety_can_take_another_method(self):
		with pest_dd.overlay(pest_dd.PHENOLOGY_KEY, with_row(by_variety={"Bing": "jan1_4_4c"})):
			entry = degree_days.crop_entry("Sweet Cherry", "bing")
		self.assertEqual((entry["base"], entry["biofix"], entry["method"]["chosen_by"]), (40.0, "01-01", "variety"))

	def test_a_block_overrides_its_variety(self):
		with pest_dd.overlay(pest_dd.PHENOLOGY_KEY, with_row(by_variety={"Bing": "jan1_4_4c"},
		                                                     by_block={"B7": "mar1_2c"})):
			entry = degree_days.crop_entry("Sweet Cherry", "Bing", "B7")
		self.assertEqual((entry["method"]["name"], entry["method"]["chosen_by"]), ("mar1_2c", "block"))


class AfterChill(DegreeDayTestCase):
	def test_a_days_chill_hours_are_estimated_from_its_range(self):
		self.assertAlmostEqual(degree_days.chill_hours(50, 30), 15.6)
		self.assertEqual(degree_days.chill_hours(60, 50), 0.0)
		self.assertEqual(degree_days.chill_hours(40, 34), 24.0)

	def test_the_count_starts_the_day_after_the_requirement_is_met(self):
		temps = {f"2025-11-{d:02d}": (40, 34) for d in range(1, 31)}  # 24 chill hours a day
		met, hours = degree_days.chill_biofix(temps, datetime.date(2025, 11, 1), 100)
		self.assertEqual((met, hours), (datetime.date(2025, 11, 6), 120.0))

	def test_a_block_on_after_chill_reports_it_and_waits_until_met(self):
		methods = {**CHERRY["methods"], "after_chill": {**CHERRY["methods"]["after_chill"],
		                                                "chill_hours_required": 999999}}
		body = with_row(by_block={"B7": "after_chill"}, methods=methods)
		with pest_dd.overlay(pest_dd.PHENOLOGY_KEY, body), \
				mock.patch.object(degree_days, "_crop_of", return_value="Sweet Cherry"):
			data = degree_days.for_block("B7")
		self.assertEqual(data["method"]["name"], "after_chill")
		self.assertIsNone(data["biofix"])
		self.assertIn("chill requirement not yet met", data["note"])


class TheGuard(DegreeDayTestCase):
	def test_the_seed_is_valid(self):
		self.assertEqual(pest_dd.validate(pest_dd.seed_body(pest_dd.PHENOLOGY_KEY), pest_dd.PHENOLOGY_KEY)["errors"], [])

	def test_an_unknown_method_and_a_chill_method_without_hours_are_refused(self):
		methods = {**CHERRY["methods"], "after_chill": {**CHERRY["methods"]["after_chill"], "chill_hours_required": 0}}
		report = pest_dd.validate(with_row(by_variety={"Van": "nope"}, methods=methods), pest_dd.PHENOLOGY_KEY)
		self.assertTrue(any("not one of its methods" in e for e in report["errors"]))
		self.assertTrue(any("chill_hours_required" in e for e in report["errors"]))

	def test_fifty_is_never_a_cherry_method(self):
		methods = {**CHERRY["methods"], "insect": {"biofix": "03-01", "base_f": 50.0, "citation": "x"}}
		report = pest_dd.validate(with_row(methods=methods), pest_dd.PHENOLOGY_KEY)
		self.assertTrue(any("insect" in e for e in report["errors"]))

	def test_pest_models_keep_their_own_bases(self):
		models, _version = pest_dd.models()
		self.assertEqual(float(models["western-cherry-fruit-fly"]["base_f"]), 41.0)

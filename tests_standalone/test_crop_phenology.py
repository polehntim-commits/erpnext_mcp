# SPDX-License-Identifier: MIT
"""Crop phenology bases, per crop, as config. v0.297.0 (Tim, 2026-10-09).

v0.310.0 (supersedes the 40.1 °F default): sweet cherry phenology defaults to March 1 at 2 °C (Kaiser, Coop & Meland,
OSU 2014), with Jan 1 at 4.4 °C / 40 °F and after-chill as alternate methods per variety or block; the 50 °F base is
never applied to cherry phenology; pest models keep their own published bases.
"""

from unittest import mock

from erpnext_mcp import degree_days, pest_dd

from .harness import STORE
from .test_degree_days import DegreeDayTestCase


class TheSeed(DegreeDayTestCase):
	def test_sweet_cherry_is_march_first_at_two_celsius(self):
		entry = degree_days.crop_entry("Sweet Cherry")
		self.assertEqual((entry["base"], entry["biofix"]), (35.6, "03-01"))
		self.assertIn("Kaiser, Coop & Meland", entry["base_source"])
		self.assertEqual((entry["method"]["name"], entry["method"]["chosen_by"]), ("mar1_2c", "default"))

	def test_by_alias(self):
		self.assertEqual(degree_days.crop_entry("cherries")["base"], 35.6)

	def test_other_crops_keep_the_default(self):
		self.assertEqual(degree_days.crop_entry("Apple")["base"], 50.0)

	def test_a_fifty_degree_table_line_is_not_applied_to_cherry(self):
		self.configure(degree_day_stage_table="Sweet Cherry: base = 50\nSweet Cherry: 51 = 100")
		entry = degree_days.crop_entry("Sweet Cherry")
		self.assertEqual(entry["base"], 35.6)
		self.assertIn("not used", entry["base_note"])
		self.assertEqual(entry["stages"], [(100.0, "51")], "the table still supplies the stages")

	def test_a_table_base_still_works_for_a_crop_without_a_row(self):
		self.configure(degree_day_stage_table="Pear: base = 43")
		self.assertEqual(degree_days.crop_entry("Pear")["base"], 43.0)

	def test_switching_to_the_january_method_is_config(self):
		body = {**pest_dd.DEFAULT_PHENOLOGY,
		        "crops": {"sweet cherry": {**pest_dd.DEFAULT_PHENOLOGY["crops"]["sweet cherry"],
		                                   "default_method": "jan1_4_4c"}}}
		with pest_dd.overlay(pest_dd.PHENOLOGY_KEY, body):
			entry = degree_days.crop_entry("Sweet Cherry")
		self.assertEqual((entry["base"], entry["biofix"]), (40.0, "01-01"))

	def test_the_block_reports_its_base(self):
		data = self.tool_data("get_degree_days", {"block": "B7", "crop": "Sweet Cherry"})
		self.assertEqual(data["base_f"], 35.6)
		self.assertEqual(data["method"]["name"], "mar1_2c")
		self.assertAlmostEqual(degree_days.gdd(70, 40, 35.6, 86), 19.4)


class TheGuard(DegreeDayTestCase):
	def test_fifty_for_cherry_is_refused(self):
		body = {"crops": {"sweet cherry": {"crop": "Sweet Cherry", "base_f": 50.0, "citation": "x"}},
		        "default": {"base_f": 50.0, "citation": "x"}}
		report = pest_dd.validate(body, pest_dd.PHENOLOGY_KEY)
		self.assertTrue(any("insect" in e for e in report["errors"]))

	def test_celsius_and_fahrenheit_must_agree(self):
		body = {"crops": {"sweet cherry": {"crop": "Sweet Cherry", "base_f": 41.0, "base_c": 4.5, "citation": "x"}},
		        "default": {"base_f": 50.0, "citation": "x"}}
		self.assertTrue(pest_dd.validate(body, pest_dd.PHENOLOGY_KEY)["errors"])

	def test_the_seed_is_valid(self):
		self.assertEqual(pest_dd.validate(pest_dd.seed_body(pest_dd.PHENOLOGY_KEY), pest_dd.PHENOLOGY_KEY)["errors"], [])

	def test_pest_models_keep_their_own_bases(self):
		models, _version = pest_dd.models()
		self.assertTrue(models)
		self.assertNotIn(35.6, {float(m["base_f"]) for m in models.values()})

	def test_preview_shows_the_base_now_and_drafted(self):
		STORE.seed("Field", [{"name": "B8", "field_name": "B8"}])
		body = {**pest_dd.DEFAULT_PHENOLOGY,
		        "crops": {"sweet cherry": {**pest_dd.DEFAULT_PHENOLOGY["crops"]["sweet cherry"],
		                                   "default_method": "jan1_4_4c"}}}
		with mock.patch.object(degree_days, "_crop_of", return_value="Sweet Cherry"):
			got = pest_dd.preview(pest_dd.PHENOLOGY_KEY, body, ["B8"])
		self.assertEqual((got["blocks"][0]["base_now"], got["blocks"][0]["base_draft"]), (35.6, 40.0))


class TheStageTableIsConfig(DegreeDayTestCase):
	"""v0.303.0. The crop's stages and biofix live in crop_phenology; the Settings text table is the fallback."""

	def body(self, **cherry):
		row = {**pest_dd.DEFAULT_PHENOLOGY["crops"]["sweet cherry"], **cherry}
		return {**pest_dd.DEFAULT_PHENOLOGY, "crops": {"sweet cherry": row}}

	def test_config_stages_win(self):
		with pest_dd.overlay(pest_dd.PHENOLOGY_KEY, self.body(stages=[[51, 80], [65, 300]])):
			entry = degree_days.crop_entry("Sweet Cherry")
		self.assertEqual(entry["stages"], [(80.0, "51"), (300.0, "65")])
		self.assertIn("crop_phenology", entry["stages_source"])

	def test_the_settings_table_is_the_fallback(self):
		self.configure(degree_day_stage_table="Sweet Cherry: 51 = 100")
		entry = degree_days.crop_entry("Sweet Cherry")
		self.assertEqual(entry["stages"], [(100.0, "51")])
		self.assertIn("Settings", entry["stages_source"])

	def test_stages_must_rise_and_be_bbch(self):
		errors = pest_dd.validate(self.body(stages=[[51, 300], [65, 100]]), pest_dd.PHENOLOGY_KEY)["errors"]
		self.assertTrue(any("must rise" in e for e in errors))
		errors = pest_dd.validate(self.body(stages=[["bloom", 100]]), pest_dd.PHENOLOGY_KEY)["errors"]
		self.assertTrue(any("two-digit" in e for e in errors))

	def test_a_crop_biofix(self):
		with pest_dd.overlay(pest_dd.PHENOLOGY_KEY, self.body(biofix="03-01")):
			self.assertEqual(degree_days.crop_entry("Sweet Cherry")["biofix"], "03-01")
		self.assertEqual(str(degree_days.biofix(2026, "03-01")), "2026-03-01")
		self.assertTrue(pest_dd.validate(self.body(biofix="13-40"), pest_dd.PHENOLOGY_KEY)["errors"])

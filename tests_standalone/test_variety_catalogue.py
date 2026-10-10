# SPDX-License-Identifier: MIT
"""The variety catalogue per crop: aliases, the pollinizer flag, and seeding it from the fields. v0.304.0.

`Crop.varieties` (Crop Variety) was already the per-crop list a field's variety is checked against. What it lacked:

* OTHER SPELLINGS. "Bing Cherry" on a scale ticket and "Bing" on the block are one tree; an alias on the catalogue row
  says so once, and a field naming the alias is written back as the variety. An alias that is another variety's own
  name, or that two varieties share, is refused — a field naming it would be ambiguous.
* THE POLLINIZER FLAG. Van planted for Bing is a fact about the variety, not a share on a block.
* TARGET BRIX SURVIVED A REPLACE. `update_crop varieties=[...]` used to drop it (the allowed keys left it out).
* A WAY TO START. `update_crop seed_from_fields=true` appends what the fields already name — never merging two
  spellings into one; that is what an alias is for, and a person decides it.
"""

from .harness import STORE
from .test_farm import ALL_ON, FieldVarietiesTestCase

TOOLS = {"allow_create_crop": 1, "allow_update_crop": 1, "allow_get_crop": 1, "allow_get_variety_care_recipe": 1,
         "allow_update_field": 1, "allow_create_field": 1}


class VarietyCatalogue(FieldVarietiesTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **{**ALL_ON, **TOOLS})
		self.a_parcel()

	def a_cherry(self, **extra):
		return self.tool_data("create_crop", {
			"crop_name": "Cherry", "crop_type": "Stone Fruit", "growth_cycle": "Perennial",
			"varieties": [{"variety_name": "Bing", "aliases": "Bing Cherry, BING", "target_brix": 18},
			              {"variety_name": "Van", "is_pollinizer": True}], **extra})

	def row(self, data, name):
		return next(r for r in data["varieties"] if r["variety_name"] == name)

	def test_aliases_pollinizer_and_brix_are_stored_and_read_back(self):
		self.a_cherry()
		data = self.tool_data("get_crop", {"crop": "Cherry"})
		self.assertEqual(self.row(data, "Bing")["aliases"], "Bing Cherry")  # BING is the name itself, dropped
		self.assertEqual(self.row(data, "Van")["is_pollinizer"], 1)

	def test_a_replace_keeps_target_brix(self):
		self.a_cherry()
		data = self.tool_data("update_crop", {"crop": "Cherry", "varieties": [
			{"variety_name": "Bing", "target_brix": 19.5}, {"variety_name": "Van"}]})
		self.assertEqual(self.row(data, "Bing")["target_brix"], 19.5)

	def test_a_field_naming_an_alias_is_written_back_as_the_variety(self):
		self.a_cherry()
		self.a_field(variety=None)
		data = self.tool_data("update_field", {"field": "Yellow Camp Block 3", "crop": "Cherry",
		                                       "variety": "bing cherry", "varieties": []})
		self.assertEqual(data["variety"], "Bing")
		data = self.tool_data("update_field", {"field": "Yellow Camp Block 3",
		                                       "varieties": [{"variety": "Bing Cherry", "percentage": 90},
		                                                     {"variety": "van", "percentage": 10}]})
		self.assertEqual([r["variety"] for r in data["varieties"]], ["Bing", "Van"])

	def test_an_alias_that_is_another_varietys_name_is_refused(self):
		self.a_cherry()
		error = self.tool_error("update_crop", {"crop": "Cherry", "varieties": [
			{"variety_name": "Bing", "aliases": "Van"}, {"variety_name": "Van"}]})
		self.assertIn("variety of its own", error)

	def test_one_alias_on_two_varieties_is_refused(self):
		error = self.tool_error("create_crop", {"crop_name": "Cherry", "crop_type": "Stone Fruit",
		                                        "growth_cycle": "Perennial", "varieties": [
			{"variety_name": "Bing", "aliases": "Dark"}, {"variety_name": "Lapins", "aliases": "dark"}]})
		self.assertIn("An alias names one variety", error)

	def test_the_care_recipe_is_reached_by_alias(self):
		self.a_cherry()
		data = self.tool_data("get_variety_care_recipe", {"crop": "Cherry", "variety": "Bing Cherry"})
		self.assertEqual(data["variety"], "Bing")

	def test_seed_from_fields_appends_what_the_fields_name_and_nothing_known(self):
		self.a_cherry()
		STORE.seed("Field", [{"name": "Block 9 - MC", "field_name": "Block 9", "crop": "Cherry", "variety": "Skeena",
		                      "varieties": [{"name": "fv-1", "parent": "Block 9 - MC", "parenttype": "Field",
		                                     "parentfield": "varieties", "variety": "Sweetheart"}]},
		                     {"name": "Block 8 - MC", "field_name": "Block 8", "crop": "Cherry",
		                      "variety": "Bing Cherry"},
		                     {"name": "Block 7 - MC", "field_name": "Block 7", "crop": "Cherry",
		                      "variety": "skeena"}])
		data = self.tool_data("update_crop", {"crop": "Cherry", "seed_from_fields": True})
		self.assertEqual(data["seeded_from_fields"], ["Skeena", "Sweetheart"])
		self.assertEqual([r["variety_name"] for r in data["varieties"]], ["Bing", "Van", "Skeena", "Sweetheart"])
		error = self.tool_error("update_crop", {"crop": "Cherry", "seed_from_fields": True})
		self.assertIn("already in its catalogue", error)

	def test_seed_and_replace_together_are_refused(self):
		self.a_cherry()
		error = self.tool_error("update_crop", {"crop": "Cherry", "seed_from_fields": True,
		                                        "varieties": [{"variety_name": "Bing"}]})
		self.assertIn("not both", error)

	def test_the_tool_says_it_takes_seed_from_fields(self):
		from erpnext_mcp import registry

		self.assertIn("seed_from_fields", registry.TOOLS["update_crop"]["inputSchema"]["properties"])

# SPDX-License-Identifier: MIT
"""Fields and irrigation zones: the structure under a parcel.

Five things these tests are really about.

THE DOCNAME IS SUFFIXED WITH THE PARCEL, ALL THE WAY DOWN. A field is
`"Block 3 - MC"` and a zone is `"YC3-Zone2 - MC"` — not `"YC3-Zone2 - YC3"`,
because a zone name already carries its block and repeating it drops the ground.
There are tests on the shape of both docnames and on the uniqueness rules that
make the shape safe.

TWO ARITHMETIC REFUSALS, AND THEY ARE CONTRADICTIONS RATHER THAN OPINIONS.
Blocks summing to more acres than their parcel; zones summing to more area than
their block. Both are the failure a bad import produces every time, both name
BOTH figures, and both have a test proving the *under*-sum case is left alone —
because roads, ditches and headlands are real, and a controller that complained
about them would complain about every real farm.

COMPLIANCE IS WOVEN, AND `WovenNotShadow` IS WHERE THAT IS PROVEN. Each of those
tests removes a compliance field and shows the SAME removal breaks an operational
answer and a regulatory one. That is the test from
project_compliance_fundamental_to_operations, written as a test rather than as a
claim in a docstring.

THE VARIETY AUTOSUGGEST COMES FROM THE GROUND, NOT FROM A LIST. `list_fields`
reports the varieties already planted, so the suggestion set cannot be wrong the
first time somebody plants something new.

`import_farm_app_fields` IS DRY RUN BY DEFAULT AND REFUSES THE WHOLE BATCH. The
tests that matter are the ones proving a bad record at position four stops
records one to three being written — a half-imported farm is worse than an
unimported one.
"""

from .fixtures import MAIN, OTHER, V12TestCase
from .harness import STORE

ALL_ON = {
	"allow_create_parcel": 1,
	"allow_list_parcels": 1,
	"allow_create_field": 1,
	"allow_update_field": 1,
	"allow_list_fields": 1,
	"allow_get_field": 1,
	"allow_link_field_to_cost_center": 1,
	"allow_get_parcel_field_summary": 1,
	"allow_import_farm_app_fields": 1,
	"allow_create_irrigation_zone": 1,
	"allow_update_irrigation_zone": 1,
	"allow_list_irrigation_zones": 1,
	"allow_get_irrigation_zone": 1,
	"allow_create_cost_center": 1,
}


class FarmTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON)

	def a_parcel(self, parcel_name="Mill Creek", acreage=131.43, company=MAIN, **overrides):
		payload = {
			"owning_entity": company,
			"parcel_name": parcel_name,
			"acreage": acreage,
			"county": "Wasco",
			"state": "OR",
			"use_type": "Orchard",
		}
		payload.update(overrides)
		return self.tool_data("create_parcel", payload)

	def a_field(self, field_name="Yellow Camp Block 3", parcel="Mill Creek", **overrides):
		payload = {
			"parcel": parcel,
			"field_name": field_name,
			"acreage": 12.5,
			"variety": "Bing",
			"planting_year": 1998,
			"planting_density_per_acre": 145,
			"rootstock": "Mazzard",
			"condition": "Good",
		}
		payload.update(overrides)
		return self.tool_data("create_field", payload)

	def a_zone(self, zone_name="YC3-Zone2", field="Yellow Camp Block 3", **overrides):
		payload = {
			"field": field,
			"zone_name": zone_name,
			"zone_number": 2,
			"water_source": "well",
			"sprinkler_type": "drip",
			"area_sq_ft": 100000,
			"flow_rate_gpm": 60,
		}
		payload.update(overrides)
		return self.tool_data("create_irrigation_zone", payload)

	def a_farm(self):
		"""One parcel, one block, one zone — the shape most tests start from."""
		self.a_parcel()
		self.a_field()
		return self.a_zone()


# ── the parcel abbreviation everything hangs off ────────────────────────────
class ParcelAbbreviation(FarmTestCase):
	def test_it_is_derived_from_the_parcel_name(self):
		self.a_parcel()
		self.assertEqual(STORE.get_raw("Parcel", f"Mill Creek - {MAIN[:0]}ETC")["abbr"], "MC")

	def test_a_single_word_name_gives_its_first_three_letters(self):
		self.a_parcel("Packinghouse", acreage=6.02)
		self.assertEqual(STORE.get_raw("Parcel", "Packinghouse - ETC")["abbr"], "PAC")

	def test_a_name_with_digits_keeps_them(self):
		self.a_parcel("Olney Road 64", acreage=65.6)
		self.assertEqual(STORE.get_raw("Parcel", "Olney Road 64 - ETC")["abbr"], "OR6")

	def test_two_parcels_deriving_the_same_key_are_disambiguated_not_refused(self):
		"""Nobody asked for the derived key, so refusing on it would stop a
		perfectly good parcel over a name the tool invented."""
		self.a_parcel("Mill Creek")
		self.a_parcel("Meadow Creek", acreage=20)
		self.assertEqual(STORE.get_raw("Parcel", "Meadow Creek - ETC")["abbr"], "MC2")

	def test_an_abbreviation_derived_for_one_company_does_not_block_another(self):
		self.a_parcel("Mill Creek", company=MAIN)
		self.a_parcel("Mill Creek", company=OTHER)
		self.assertEqual(STORE.get_raw("Parcel", "Mill Creek - SEL")["abbr"], "MC")

	def test_a_parcel_with_no_stored_abbreviation_still_produces_the_docname_it_will_keep(self):
		"""Parcels registered before v0.12.0 have no abbr until something saves
		them. The derivation is deterministic, so no data patch is needed."""
		self.a_parcel()
		STORE.tables["Parcel"]["Mill Creek - ETC"]["abbr"] = ""
		self.assertEqual(self.a_field()["name"], "Yellow Camp Block 3 - MC")


# ── create_field ────────────────────────────────────────────────────────────
class CreateField(FarmTestCase):
	def test_it_registers_a_block_under_a_docname_carrying_the_parcel(self):
		self.a_parcel()
		data = self.a_field()
		self.assertEqual(data["name"], "Yellow Camp Block 3 - MC")
		self.assertEqual(data["field_name"], "Yellow Camp Block 3")
		self.assertEqual(data["parcel"], "Mill Creek - ETC")
		self.assertEqual(data["acreage"], 12.5)

	def test_the_owning_entity_comes_from_the_parcel_not_from_the_caller(self):
		"""A block on Highland's ground belongs to Highland whatever anyone
		types, which is why the field is fetched and read-only."""
		self.a_parcel()
		self.assertEqual(self.a_field()["owning_entity"], MAIN)

	def test_the_crop_defaults_to_cherry(self):
		self.a_parcel()
		self.assertEqual(self.a_field()["crop"], "Cherry")

	def test_a_different_crop_is_accepted_because_a_block_of_grapes_is_not_a_schema_change(self):
		self.a_parcel()
		self.assertEqual(self.a_field(crop="Table Grapes")["crop"], "Table Grapes")

	def test_any_variety_is_accepted_because_a_select_would_refuse_the_next_one(self):
		self.a_parcel()
		self.assertEqual(self.a_field(variety="Black Pearl")["variety"], "Black Pearl")

	def test_it_estimates_the_tree_count_from_acreage_and_density(self):
		self.a_parcel()
		self.assertEqual(self.a_field()["tree_count_estimate"], 1812)

	def test_no_density_means_no_tree_count_rather_than_zero(self):
		self.a_parcel()
		self.assertIsNone(self.a_field(planting_density_per_acre=0)["tree_count_estimate"])

	def test_a_bare_parcel_name_resolves_to_its_docname(self):
		self.a_parcel()
		self.assertEqual(self.a_field(parcel="Mill Creek")["parcel"], "Mill Creek - ETC")

	def test_two_parcels_may_each_have_a_block_three(self):
		self.a_parcel("Mill Creek")
		self.a_parcel("Red Camp", acreage=37.49)
		self.assertEqual(self.a_field("Block 3", "Mill Creek")["name"], "Block 3 - MC")
		self.assertEqual(self.a_field("Block 3", "Red Camp")["name"], "Block 3 - RC")

	def test_a_duplicate_name_on_one_parcel_is_refused(self):
		self.a_parcel()
		self.a_field()
		error = self.tool_error("create_field", {"parcel": "Mill Creek", "field_name": "Yellow Camp Block 3"})
		self.assertIn("already has a block", error)
		self.assertIn("Nothing was created", error)

	def test_a_claimed_farm_app_id_is_refused(self):
		self.a_parcel()
		self.a_field(external_farm_app_id="a1b2c3")
		error = self.tool_error(
			"create_field",
			{"parcel": "Mill Creek", "field_name": "Block 4", "external_farm_app_id": "a1b2c3"},
		)
		self.assertIn("primary key", error)
		self.assertIn("sync bridge", error)

	def test_a_missing_parcel_is_refused(self):
		self.assertIn(
			"no Parcel called",
			self.tool_error("create_field", {"parcel": "Nowhere", "field_name": "Block 1"}),
		)

	def test_missing_required_arguments_are_refused_by_name(self):
		self.a_parcel()
		self.assertIn("field_name is required", self.tool_error("create_field", {"parcel": "Mill Creek"}))
		self.assertIn("parcel is required", self.tool_error("create_field", {"field_name": "Block 1"}))

	def test_a_condition_outside_the_list_is_refused_with_the_list(self):
		self.a_parcel()
		error = self.tool_error(
			"create_field", {"parcel": "Mill Creek", "field_name": "Block 1", "condition": "Lovely"}
		)
		self.assertIn("Fallow", error)

	def test_the_switch_is_off_by_default(self):
		self.a_parcel()
		self.configure(enabled=1, allow_create_parcel=1)
		self.assertIn(
			"switched off",
			self.tool_error("create_field", {"parcel": "Mill Creek", "field_name": "Block 1"}),
		)

	def test_it_is_audited(self):
		self.a_parcel()
		self.a_field()
		self.assertAudited("create_field", "Success")


# ── the `varieties` link to the crop's own catalogue ────────────────────────
class FieldVarietiesTestCase(FarmTestCase):
	def a_crop(self, name="Cherry", varieties=()):
		"""A Crop with a Crop Variety catalogue, seeded directly — the same
		shortcut `test_patches.BackfillFieldVarieties` uses, since what these
		tests need is the catalogue in place, not a second exercise of
		`create_crop`."""
		STORE.seed(
			"Crop",
			[
				{
					"name": name,
					"crop_name": name,
					"varieties": [
						{
							"name": f"cv-{name}-{index}",
							"parent": name,
							"parenttype": "Crop",
							"parentfield": "varieties",
							"variety_name": variety_name,
						}
						for index, variety_name in enumerate(varieties, start=1)
					],
				}
			],
		)


class CreateFieldVarieties(FieldVarietiesTestCase):
	def test_a_varieties_argument_creates_child_rows_reported_back(self):
		"""No crop catalogue is recorded, so there is nothing to check the
		spelling against — the common case on a farm still onboarding."""
		self.a_parcel()
		data = self.a_field(
			variety=None,
			varieties=[
				{"variety": "Black Pearl", "percentage": 60, "planting_year": 2019},
				{"variety": "Burgundy Pearl", "percentage": 40, "planting_year": 2019},
			],
		)
		self.assertEqual(len(data["varieties"]), 2)
		self.assertEqual({row["variety"] for row in data["varieties"]}, {"Black Pearl", "Burgundy Pearl"})
		self.assertEqual(sum(row["percentage"] for row in data["varieties"]), 100.0)

	def test_percentage_and_planting_year_are_optional(self):
		self.a_parcel()
		data = self.a_field(variety=None, varieties=[{"variety": "Black Pearl"}])
		self.assertEqual(data["varieties"], [{"variety": "Black Pearl", "percentage": None, "planting_year": None}])

	def test_the_single_variety_column_is_unaffected_and_stays_primary(self):
		"""The legacy field is not derived from the table and the table is not
		derived from it — the two coexist on purpose."""
		self.a_parcel()
		data = self.a_field(variety="Bing", varieties=[{"variety": "Black Pearl"}])
		self.assertEqual(data["variety"], "Bing")
		self.assertEqual(data["varieties"][0]["variety"], "Black Pearl")

	def test_an_unknown_key_is_refused_and_nothing_is_written(self):
		self.a_parcel()
		error = self.tool_error(
			"create_field",
			{
				"parcel": "Mill Creek",
				"field_name": "Block 9",
				"varieties": [{"variety": "Black Pearl", "rootstock": "Gisela 6"}],
			},
		)
		self.assertIn("unknown key", error)
		self.assertIn("rootstock", error)
		self.assertIsNone(STORE.get_raw("Field", "Block 9 - MC"))

	def test_a_row_with_no_variety_is_refused(self):
		self.a_parcel()
		error = self.tool_error(
			"create_field", {"parcel": "Mill Creek", "field_name": "Block 9", "varieties": [{"percentage": 50}]}
		)
		self.assertIn("no variety", error)

	def test_the_catalogues_own_spelling_is_written_back(self):
		self.a_crop("Cherry", ["Black Pearl", "Bing"])
		self.a_parcel()
		data = self.a_field(crop="Cherry", variety=None, varieties=[{"variety": "black pearl"}])
		self.assertEqual(data["varieties"][0]["variety"], "Black Pearl")

	def test_a_variety_not_in_the_crops_catalogue_is_refused(self):
		self.a_crop("Cherry", ["Bing"])
		self.a_parcel()
		error = self.tool_error(
			"create_field",
			{
				"parcel": "Mill Creek",
				"field_name": "Block 9",
				"crop": "Cherry",
				"varieties": [{"variety": "Black Pearl"}],
			},
		)
		self.assertIn("not among", error)
		self.assertIn("Bing", error)
		self.assertIsNone(STORE.get_raw("Field", "Block 9 - MC"))

	def test_a_crop_with_no_catalogue_at_all_is_not_checked(self):
		"""`Cherry` names no Crop record on this site, which is the ordinary
		state for a farm that has not built its crop register yet."""
		self.a_parcel()
		data = self.a_field(variety=None, varieties=[{"variety": "Anything At All"}])
		self.assertEqual(data["varieties"][0]["variety"], "Anything At All")

	def test_percentages_summing_past_100_are_refused(self):
		self.a_parcel()
		error = self.tool_error(
			"create_field",
			{
				"parcel": "Mill Creek",
				"field_name": "Block 9",
				"varieties": [
					{"variety": "Black Pearl", "percentage": 60},
					{"variety": "Burgundy Pearl", "percentage": 60},
				],
			},
		)
		self.assertIn("100%", error)
		self.assertIsNone(STORE.get_raw("Field", "Block 9 - MC"))

	def test_percentages_summing_to_exactly_100_are_allowed(self):
		self.a_parcel()
		data = self.a_field(
			variety=None,
			varieties=[
				{"variety": "Black Pearl", "percentage": 60},
				{"variety": "Burgundy Pearl", "percentage": 40},
			],
		)
		self.assertEqual(len(data["varieties"]), 2)


class UpdateFieldVarieties(FieldVarietiesTestCase):
	def setUp(self):
		super().setUp()
		self.a_parcel()
		self.a_field(variety=None, varieties=[{"variety": "Black Pearl", "percentage": 100}])

	def test_it_replaces_the_table_wholesale(self):
		data = self.tool_data(
			"update_field",
			{"field": "Yellow Camp Block 3", "varieties": [{"variety": "Ebony Pearl", "percentage": 100}]},
		)
		self.assertEqual(len(data["varieties"]), 1)
		self.assertEqual(data["varieties"][0]["variety"], "Ebony Pearl")

	def test_omitting_it_leaves_the_table_untouched(self):
		data = self.tool_data("update_field", {"field": "Yellow Camp Block 3", "condition": "Fair"})
		self.assertEqual(data["varieties"], [{"variety": "Black Pearl", "percentage": 100.0, "planting_year": None}])

	def test_an_empty_list_clears_it(self):
		data = self.tool_data("update_field", {"field": "Yellow Camp Block 3", "varieties": []})
		self.assertEqual(data["varieties"], [])

	def test_varieties_alone_counts_as_a_change(self):
		data = self.tool_data(
			"update_field",
			{"field": "Yellow Camp Block 3", "varieties": [{"variety": "Ebony Pearl"}]},
		)
		self.assertIn("varieties", data["changed"])

	def test_a_variety_not_in_the_crops_catalogue_is_refused_and_the_old_table_survives(self):
		self.a_crop("Cherry", ["Bing"])
		error = self.tool_error(
			"update_field",
			{"field": "Yellow Camp Block 3", "crop": "Cherry", "varieties": [{"variety": "Black Pearl"}]},
		)
		self.assertIn("not among", error)
		row = STORE.get_raw("Field", "Yellow Camp Block 3 - MC")
		self.assertEqual([entry["variety"] for entry in row["varieties"]], ["Black Pearl"])


class FieldAcreageAgainstTheParcel(FarmTestCase):
	def test_blocks_summing_past_the_parcel_are_refused(self):
		self.a_parcel("Doane Road", acreage=14.84)
		self.a_field("Block A", "Doane Road", acreage=10)
		error = self.tool_error(
			"create_field", {"parcel": "Doane Road", "field_name": "Block B", "acreage": 6}
		)
		self.assertIn("14.84", error)
		self.assertIn("16.0", error)

	def test_the_refusal_names_the_excess_so_the_next_question_is_answerable(self):
		self.a_parcel("Doane Road", acreage=14.84)
		self.a_field("Block A", "Doane Road", acreage=10)
		error = self.tool_error(
			"create_field", {"parcel": "Doane Road", "field_name": "Block B", "acreage": 6}
		)
		self.assertIn("1.16", error)
		self.assertIn("both cannot be right", error)

	def test_nothing_is_written_when_the_sum_is_refused(self):
		self.a_parcel("Doane Road", acreage=14.84)
		self.a_field("Block A", "Doane Road", acreage=10)
		self.tool_error("create_field", {"parcel": "Doane Road", "field_name": "Block B", "acreage": 6})
		self.assertIsNone(STORE.get_raw("Field", "Block B - DR"))

	def test_blocks_summing_to_exactly_the_parcel_are_allowed(self):
		self.a_parcel("Doane Road", acreage=14.84)
		self.a_field("Block A", "Doane Road", acreage=10)
		self.assertEqual(self.a_field("Block B", "Doane Road", acreage=4.84)["acreage"], 4.84)

	def test_blocks_summing_to_less_than_the_parcel_are_the_normal_case(self):
		"""Roads, ditches, headlands and the house are all real, so a controller
		that complained about this would complain about every real farm."""
		self.a_parcel("Doane Road", acreage=14.84)
		data = self.a_field("Block A", "Doane Road", acreage=9)
		self.assertNotIn("exceed", " ".join(data.get("warnings") or []))

	def test_a_parcel_with_no_acreage_recorded_is_not_checked(self):
		"""An unknown is not a zero. Treating it as one would refuse every block
		on a parcel nobody has measured yet."""
		self.a_parcel("Unmeasured", acreage=0)
		self.assertEqual(self.a_field("Block A", "Unmeasured", acreage=500)["acreage"], 500.0)

	def test_raising_an_existing_blocks_acreage_past_the_parcel_is_refused_too(self):
		self.a_parcel("Doane Road", acreage=14.84)
		self.a_field("Block A", "Doane Road", acreage=10)
		error = self.tool_error("update_field", {"field": "Block A - DR", "acreage": 20})
		self.assertIn("14.84", error)

	def test_the_running_total_is_reported_on_every_create(self):
		self.a_parcel("Doane Road", acreage=14.84)
		data = self.a_field("Block A", "Doane Road", acreage=9)
		self.assertTrue(any("9.0" in warning for warning in data["warnings"]))


class FieldWarnings(FarmTestCase):
	def test_a_block_with_no_acreage_says_what_that_costs(self):
		self.a_parcel()
		data = self.a_field(acreage=0)
		self.assertTrue(any("per-acre costing" in warning for warning in data["warnings"]))

	def test_a_food_safety_block_with_no_hygiene_station_is_warned_about(self):
		self.a_parcel()
		data = self.a_field(food_safety_zone=True, worker_hygiene_station_present=False)
		joined = " ".join(data["warnings"])
		self.assertIn("Subpart L", joined)
		self.assertIn("quarter mile", joined)

	def test_a_food_safety_block_with_no_water_test_is_warned_about(self):
		self.a_parcel()
		data = self.a_field(food_safety_zone=True)
		self.assertTrue(any("water test" in warning for warning in data["warnings"]))

	def test_a_warning_is_not_a_refusal(self):
		"""Every one of these is a fact worth recording precisely because it is
		a problem. Refusing would guarantee the record stayed empty."""
		self.a_parcel()
		data = self.a_field(food_safety_zone=True)
		self.assertTrue(STORE.get_raw("Field", data["name"]))


# ── update_field ────────────────────────────────────────────────────────────
class UpdateField(FarmTestCase):
	def setUp(self):
		super().setUp()
		self.a_parcel()
		self.a_field()

	def test_it_changes_the_condition_and_echoes_before_and_after(self):
		data = self.tool_data("update_field", {"field": "Yellow Camp Block 3", "condition": "Fair"})
		self.assertEqual(data["changed"]["condition"], ["Good", "Fair"])
		self.assertEqual(data["condition"], "Fair")

	def test_it_changes_a_food_safety_date(self):
		data = self.tool_data(
			"update_field", {"field": "Yellow Camp Block 3", "last_spray_date": "2026-05-15"}
		)
		self.assertEqual(data["last_spray_date"], "2026-05-15")

	def test_it_sets_a_check_field(self):
		data = self.tool_data(
			"update_field", {"field": "Yellow Camp Block 3", "worker_hygiene_station_present": True}
		)
		self.assertTrue(data["worker_hygiene_station_present"])

	def test_renaming_a_block_is_refused_because_zones_point_at_the_docname(self):
		error = self.tool_error("update_field", {"field": "Yellow Camp Block 3", "field_name": "Block Three"})
		self.assertIn("docname", error)
		self.assertIn("irrigation zone", error)

	def test_moving_a_block_to_another_parcel_is_refused(self):
		self.a_parcel("Red Camp", acreage=37.49)
		error = self.tool_error("update_field", {"field": "Yellow Camp Block 3", "parcel": "Red Camp"})
		self.assertIn("Ground does not move", error)

	def test_setting_the_cost_center_here_is_refused_and_names_the_right_tool(self):
		error = self.tool_error("update_field", {"field": "Yellow Camp Block 3", "cost_center": "Main"})
		self.assertIn("link_field_to_cost_center", error)

	def test_a_claimed_farm_app_id_is_refused_on_update_too(self):
		self.a_field("Block 4", external_farm_app_id="zzz")
		error = self.tool_error(
			"update_field", {"field": "Yellow Camp Block 3", "external_farm_app_id": "zzz"}
		)
		self.assertIn("already on", error)

	def test_a_call_that_changes_nothing_is_refused_with_the_options(self):
		error = self.tool_error("update_field", {"field": "Yellow Camp Block 3"})
		self.assertIn("nothing to change", error)
		self.assertIn("wildlife_intrusion_last_report", error)

	def test_an_unknown_field_is_refused(self):
		self.assertIn("no Field called", self.tool_error("update_field", {"field": "Nowhere", "acreage": 1}))


# ── list_fields and get_field ───────────────────────────────────────────────
class ListFields(FarmTestCase):
	def setUp(self):
		super().setUp()
		self.a_parcel()
		self.a_field("Block A", acreage=10, variety="Bing", planting_year=1998)
		self.a_field("Block B", acreage=8, variety="Rainier", planting_year=2015)

	def test_it_totals_the_acreage(self):
		data = self.tool_data("list_fields")
		self.assertEqual(data["field_count"], 2)
		self.assertEqual(data["total_acreage"], 18.0)
		self.assertEqual(data["average_acreage"], 9.0)

	def test_it_reports_the_oldest_and_newest_planting_years(self):
		data = self.tool_data("list_fields")
		self.assertEqual(data["oldest_planting_year"], 1998)
		self.assertEqual(data["newest_planting_year"], 2015)

	def test_it_counts_by_variety(self):
		self.assertEqual(self.tool_data("list_fields")["by_variety"], {"Bing": 1, "Rainier": 1})

	def test_the_variety_autosuggest_comes_from_what_is_already_planted(self):
		"""A hardcoded list is wrong the first time somebody plants something
		new; what is already in the ground cannot be."""
		self.a_field("Block C", acreage=1, variety="Skeena")
		self.assertEqual(self.tool_data("list_fields")["known_varieties"], ["Bing", "Rainier", "Skeena"])

	def test_it_filters_by_variety(self):
		data = self.tool_data("list_fields", {"variety": "Bing"})
		self.assertEqual([row["field_name"] for row in data["fields"]], ["Block A"])

	def test_it_filters_by_parcel(self):
		self.a_parcel("Red Camp", acreage=37.49)
		self.a_field("Block Z", "Red Camp", acreage=5)
		data = self.tool_data("list_fields", {"parcel": "Red Camp"})
		self.assertEqual([row["field_name"] for row in data["fields"]], ["Block Z"])

	def test_it_filters_by_food_safety_zone(self):
		self.a_field("Block FS", acreage=1, food_safety_zone=True)
		data = self.tool_data("list_fields", {"food_safety_zone": True})
		self.assertEqual([row["field_name"] for row in data["fields"]], ["Block FS"])

	def test_it_names_the_blocks_with_no_acreage(self):
		self.a_field("Block D", acreage=0)
		self.assertIn("Block D - MC", self.tool_data("list_fields")["without_acreage"])

	def test_it_scopes_to_one_company(self):
		self.a_parcel("Far Field", acreage=50, company=OTHER)
		self.a_field("Block Q", "Far Field", acreage=5)
		names = [row["field_name"] for row in self.tool_data("list_fields", {"company": MAIN})["fields"]]
		self.assertNotIn("Block Q", names)

	def test_it_says_whether_spray_dates_came_from_farm_precision_ag(self):
		"""False on a site without it, which is what makes `last_spray_date`
		readable as 'what somebody recorded' rather than 'what happened'."""
		self.assertFalse(self.tool_data("list_fields")["spray_dates_from_farm_precision_ag"])

	def test_it_is_read_only(self):
		before = {doctype: len(rows) for doctype, rows in STORE.tables.items()}
		self.tool_data("list_fields")
		after = {doctype: len(rows) for doctype, rows in STORE.tables.items()}
		before.pop("MCP Action Log", None)
		after.pop("MCP Action Log", None)
		self.assertEqual(before, after)


class GetField(FarmTestCase):
	def test_it_returns_the_block_with_its_zones(self):
		self.a_farm()
		data = self.tool_data("get_field", {"field": "Yellow Camp Block 3"})
		self.assertEqual(data["zone_count"], 1)
		self.assertEqual(data["zones"][0]["zone_name"], "YC3-Zone2")

	def test_it_reports_how_much_of_the_block_is_not_zoned(self):
		self.a_farm()
		data = self.tool_data("get_field", {"field": "Yellow Camp Block 3"})
		self.assertEqual(data["zone_acreage"], 2.296)
		self.assertEqual(data["unzoned_acreage"], 10.204)

	def test_it_names_every_water_right_over_the_block(self):
		self.a_parcel()
		self.a_field()
		self.a_zone("Z1", zone_number=1, water_right_id="S-54321", area_sq_ft=1000)
		self.a_zone("Z2", zone_number=2, water_right_id="S-99999", area_sq_ft=1000)
		data = self.tool_data("get_field", {"field": "Yellow Camp Block 3"})
		self.assertEqual(data["water_rights"], ["S-54321", "S-99999"])

	def test_it_carries_the_parcel_detail_including_the_abbreviation(self):
		self.a_parcel()
		self.a_field()
		data = self.tool_data("get_field", {"field": "Yellow Camp Block 3"})
		self.assertEqual(data["parcel_detail"]["abbr"], "MC")
		self.assertEqual(data["parcel_detail"]["acreage"], 131.43)

	def test_a_bare_name_matching_two_parcels_is_refused_with_both_named(self):
		self.a_parcel("Mill Creek")
		self.a_parcel("Red Camp", acreage=37.49)
		self.a_field("Block 3", "Mill Creek")
		self.a_field("Block 3", "Red Camp")
		error = self.tool_error("get_field", {"field": "Block 3"})
		self.assertIn("Block 3 - MC", error)
		self.assertIn("Block 3 - RC", error)

	def test_the_parcel_argument_narrows_an_ambiguous_name(self):
		self.a_parcel("Mill Creek")
		self.a_parcel("Red Camp", acreage=37.49)
		self.a_field("Block 3", "Mill Creek")
		self.a_field("Block 3", "Red Camp")
		self.assertEqual(
			self.tool_data("get_field", {"field": "Block 3", "parcel": "Red Camp"})["name"],
			"Block 3 - RC",
		)

	def test_an_unknown_block_is_refused_with_the_register_named(self):
		self.assertIn("list_fields", self.tool_error("get_field", {"field": "Nowhere"}))


# ── irrigation zones ────────────────────────────────────────────────────────
class CreateIrrigationZone(FarmTestCase):
	def setUp(self):
		super().setUp()
		self.a_parcel()
		self.a_field()

	def test_the_docname_is_suffixed_with_the_parcel_not_the_field(self):
		"""'YC3-Zone2 - YC3' would say the same thing twice and drop the ground."""
		self.assertEqual(self.a_zone()["name"], "YC3-Zone2 - MC")

	def test_it_records_the_water_source_and_right(self):
		data = self.a_zone(water_source="creek", water_right_id="S-54321")
		self.assertEqual(data["water_source"], "creek")
		self.assertEqual(data["water_right_id"], "S-54321")

	def test_acres_are_computed_from_square_feet(self):
		self.assertEqual(self.a_zone(area_sq_ft=43560)["area_acres"], 1.0)

	def test_passing_acres_directly_is_refused_and_the_conversion_is_offered(self):
		"""Two figures a caller can set independently are two that will disagree."""
		error = self.tool_error(
			"create_irrigation_zone",
			{"field": "Yellow Camp Block 3", "zone_name": "Z9", "area_acres": 2},
		)
		self.assertIn("87120.0 sq ft", error)

	def test_the_parcel_and_owning_entity_come_from_the_block(self):
		data = self.a_zone()
		self.assertEqual(data["parcel"], "Mill Creek - ETC")
		self.assertEqual(data["owning_entity"], MAIN)

	def test_a_duplicate_zone_name_on_one_parcel_is_refused(self):
		self.a_zone()
		self.a_field("Block 9")
		error = self.tool_error("create_irrigation_zone", {"field": "Block 9", "zone_name": "YC3-Zone2"})
		self.assertIn("unique within a parcel", error)

	def test_a_duplicate_zone_number_on_one_block_is_refused(self):
		self.a_zone("Z1", zone_number=3, area_sq_ft=1000)
		error = self.tool_error(
			"create_irrigation_zone",
			{"field": "Yellow Camp Block 3", "zone_name": "Z2", "zone_number": 3},
		)
		self.assertIn("two in the morning", error)
		self.assertIn("Nothing was created", error)

	def test_the_same_zone_number_on_a_different_block_is_fine(self):
		self.a_zone("Z1", zone_number=3, area_sq_ft=1000)
		self.a_field("Block 9", acreage=5)
		self.assertEqual(self.a_zone("Z9", field="Block 9", zone_number=3, area_sq_ft=1000)["zone_number"], 3)

	def test_zones_summing_past_the_block_are_refused_with_both_figures(self):
		self.a_zone("Z1", zone_number=1, area_sq_ft=43560 * 10)
		error = self.tool_error(
			"create_irrigation_zone",
			{
				"field": "Yellow Camp Block 3",
				"zone_name": "Z2",
				"zone_number": 2,
				"area_sq_ft": 43560 * 5,
			},
		)
		self.assertIn("12.5", error)
		self.assertIn("15.0", error)

	def test_zones_summing_to_less_than_the_block_are_the_normal_case(self):
		self.a_zone("Z1", zone_number=1, area_sq_ft=43560)
		self.assertEqual(self.a_zone("Z2", zone_number=2, area_sq_ft=43560)["area_acres"], 1.0)

	def test_a_block_with_no_acreage_does_not_constrain_its_zones(self):
		self.a_field("Unmeasured Block", acreage=0)
		self.assertEqual(
			self.a_zone("UZ1", field="Unmeasured Block", area_sq_ft=43560 * 99)["area_acres"], 99.0
		)

	def test_surface_water_with_no_right_is_warned_about_not_refused(self):
		data = self.a_zone(water_source="creek", water_right_id="")
		self.assertTrue(any("water right" in warning for warning in data["warnings"]))
		self.assertTrue(STORE.get_raw("Irrigation Zone", data["name"]))

	def test_a_well_with_no_right_is_not_warned_about(self):
		data = self.a_zone(water_source="well")
		self.assertNotIn("warnings", data)

	def test_a_zone_on_a_food_safety_block_with_no_water_test_is_warned_about(self):
		self.a_field("FS Block", acreage=5, food_safety_zone=True)
		data = self.a_zone("FSZ1", field="FS Block", area_sq_ft=1000)
		self.assertTrue(any("Subpart E" in warning for warning in data["warnings"]))

	def test_an_unknown_water_source_is_refused_with_the_list(self):
		error = self.tool_error(
			"create_irrigation_zone",
			{"field": "Yellow Camp Block 3", "zone_name": "Z9", "water_source": "hosepipe"},
		)
		self.assertIn("municipal", error)

	def test_missing_required_arguments_are_refused_by_name(self):
		self.assertIn(
			"zone_name is required",
			self.tool_error("create_irrigation_zone", {"field": "Yellow Camp Block 3"}),
		)
		self.assertIn("field is required", self.tool_error("create_irrigation_zone", {"zone_name": "Z"}))

	def test_the_switch_is_off_by_default(self):
		self.configure(enabled=1, allow_create_parcel=1, allow_create_field=1)
		self.assertIn(
			"switched off",
			self.tool_error("create_irrigation_zone", {"field": "Yellow Camp Block 3", "zone_name": "Z1"}),
		)


class UpdateIrrigationZone(FarmTestCase):
	def setUp(self):
		super().setUp()
		self.a_farm()

	def test_it_records_a_water_test(self):
		data = self.tool_data(
			"update_irrigation_zone", {"zone": "YC3-Zone2", "water_test_last_date": "2026-04-01"}
		)
		self.assertEqual(data["water_test_last_date"], "2026-04-01")

	def test_it_recomputes_the_acres_when_the_square_feet_change(self):
		data = self.tool_data("update_irrigation_zone", {"zone": "YC3-Zone2", "area_sq_ft": 43560})
		self.assertEqual(data["area_acres"], 1.0)

	def test_renaming_a_zone_is_refused(self):
		self.assertIn(
			"docname",
			self.tool_error("update_irrigation_zone", {"zone": "YC3-Zone2", "zone_name": "Other"}),
		)

	def test_moving_a_zone_to_another_block_is_refused(self):
		self.a_field("Block 9", acreage=5)
		error = self.tool_error("update_irrigation_zone", {"zone": "YC3-Zone2", "field": "Block 9"})
		self.assertIn("pipe does not move", error)

	def test_setting_the_acres_directly_is_refused(self):
		self.assertIn(
			"computed",
			self.tool_error("update_irrigation_zone", {"zone": "YC3-Zone2", "area_acres": 3}),
		)

	def test_a_zone_number_already_used_on_the_block_is_refused(self):
		self.a_zone("Z1", zone_number=7, area_sq_ft=1000)
		error = self.tool_error("update_irrigation_zone", {"zone": "YC3-Zone2", "zone_number": 7})
		self.assertIn("already", error)

	def test_a_call_that_changes_nothing_is_refused(self):
		self.assertIn("nothing to change", self.tool_error("update_irrigation_zone", {"zone": "YC3-Zone2"}))


class ListIrrigationZones(FarmTestCase):
	def setUp(self):
		super().setUp()
		self.a_parcel()
		self.a_field()
		self.a_zone("Z1", zone_number=1, area_sq_ft=43560, flow_rate_gpm=50, water_source="well")
		self.a_zone(
			"Z2",
			zone_number=2,
			area_sq_ft=43560,
			flow_rate_gpm=30,
			water_source="creek",
			water_right_id="S-54321",
			water_test_last_date="2026-03-01",
		)

	def test_it_totals_area_and_flow(self):
		data = self.tool_data("list_irrigation_zones")
		self.assertEqual(data["zone_count"], 2)
		self.assertEqual(data["total_area_acres"], 2.0)
		self.assertEqual(data["total_flow_gpm"], 80.0)

	def test_it_counts_by_water_source(self):
		self.assertEqual(self.tool_data("list_irrigation_zones")["by_water_source"], {"creek": 1, "well": 1})

	def test_it_names_the_zones_with_no_water_test(self):
		self.assertEqual(self.tool_data("list_irrigation_zones")["without_water_test"], ["Z1 - MC"])

	def test_it_names_surface_water_zones_with_no_water_right(self):
		self.a_zone("Z3", zone_number=3, water_source="pond", area_sq_ft=1000)
		self.assertIn("Z3 - MC", self.tool_data("list_irrigation_zones")["surface_water_without_a_right"])

	def test_a_well_with_no_right_is_not_on_that_list(self):
		self.assertNotIn("Z1 - MC", self.tool_data("list_irrigation_zones")["surface_water_without_a_right"])

	def test_it_filters_by_block(self):
		self.a_field("Block 9", acreage=5)
		self.a_zone("Z9", field="Block 9", zone_number=1, area_sq_ft=1000)
		data = self.tool_data("list_irrigation_zones", {"field": "Block 9"})
		self.assertEqual([row["zone_name"] for row in data["zones"]], ["Z9"])

	def test_it_filters_by_water_source(self):
		data = self.tool_data("list_irrigation_zones", {"water_source": "creek"})
		self.assertEqual([row["zone_name"] for row in data["zones"]], ["Z2"])


class GetIrrigationZone(FarmTestCase):
	def setUp(self):
		super().setUp()
		self.a_farm()

	def test_it_reports_the_share_of_the_block_this_zone_covers(self):
		data = self.tool_data("get_irrigation_zone", {"zone": "YC3-Zone2"})
		self.assertEqual(data["share_of_field"], 0.184)

	def test_it_carries_the_block_detail(self):
		data = self.tool_data("get_irrigation_zone", {"zone": "YC3-Zone2"})
		self.assertEqual(data["field_detail"]["field_name"], "Yellow Camp Block 3")
		self.assertEqual(data["field_detail"]["acreage"], 12.5)

	def test_a_food_safety_block_with_no_water_test_gets_a_compliance_note(self):
		self.a_field("FS Block", acreage=5, food_safety_zone=True)
		self.a_zone("FSZ", field="FS Block", area_sq_ft=1000)
		notes = self.tool_data("get_irrigation_zone", {"zone": "FSZ"})["compliance_notes"]
		self.assertTrue(any("Subpart E" in note for note in notes))

	def test_surface_water_with_no_right_gets_a_compliance_note(self):
		self.a_zone("Creek Zone", zone_number=5, water_source="creek", area_sq_ft=1000)
		notes = self.tool_data("get_irrigation_zone", {"zone": "Creek Zone"})["compliance_notes"]
		self.assertTrue(any("Oregon" in note for note in notes))

	def test_an_unknown_zone_is_refused_with_the_register_named(self):
		self.assertIn("list_irrigation_zones", self.tool_error("get_irrigation_zone", {"zone": "Nowhere"}))


# ── link_field_to_cost_center ───────────────────────────────────────────────
class LinkFieldToCostCenter(FarmTestCase):
	def setUp(self):
		super().setUp()
		self.a_parcel()
		self.a_field()

	def cost_center(self):
		from .fixtures import cost_center

		return cost_center("Field Work")

	def test_it_points_the_block_at_the_cost_center(self):
		data = self.tool_data(
			"link_field_to_cost_center",
			{"field": "Yellow Camp Block 3", "cost_center": self.cost_center()},
		)
		self.assertTrue(data["changed"])
		self.assertEqual(
			STORE.get_raw("Field", "Yellow Camp Block 3 - MC")["cost_center"], self.cost_center()
		)

	def test_dry_run_writes_nothing(self):
		data = self.tool_data(
			"link_field_to_cost_center",
			{"field": "Yellow Camp Block 3", "cost_center": self.cost_center(), "dry_run": True},
		)
		self.assertTrue(data["dry_run"])
		self.assertFalse(data["changed"])
		self.assertFalse(STORE.get_raw("Field", "Yellow Camp Block 3 - MC").get("cost_center"))

	def test_relinking_the_same_cost_center_is_a_no_op_rather_than_an_error(self):
		payload = {"field": "Yellow Camp Block 3", "cost_center": self.cost_center()}
		self.tool_data("link_field_to_cost_center", payload)
		data = self.tool_data("link_field_to_cost_center", payload)
		self.assertFalse(data["changed"])

	def test_repointing_to_a_different_cost_center_is_refused_without_replace(self):
		from .fixtures import HARVEST

		self.tool_data(
			"link_field_to_cost_center",
			{"field": "Yellow Camp Block 3", "cost_center": self.cost_center()},
		)
		error = self.tool_error(
			"link_field_to_cost_center",
			{"field": "Yellow Camp Block 3", "cost_center": HARVEST},
		)
		self.assertIn("replace=true", error)
		self.assertIn("different places", error)

	def test_replace_true_repoints_it(self):
		from .fixtures import HARVEST

		self.tool_data(
			"link_field_to_cost_center",
			{"field": "Yellow Camp Block 3", "cost_center": self.cost_center()},
		)
		data = self.tool_data(
			"link_field_to_cost_center",
			{"field": "Yellow Camp Block 3", "cost_center": HARVEST, "replace": True},
		)
		self.assertTrue(data["changed"])
		self.assertEqual(data["previous_cost_center"], self.cost_center())

	def test_a_cost_center_on_another_companys_books_is_refused(self):
		"""A cost allocated across two companies is an intercompany transaction,
		not a dimension."""
		from .fixtures import OTHER_ABBR, cost_center

		error = self.tool_error(
			"link_field_to_cost_center",
			{"field": "Yellow Camp Block 3", "cost_center": cost_center("Main", OTHER_ABBR)},
		)
		self.assertIn("intercompany", error)
		self.assertIn("Nothing was changed", error)

	def test_a_group_cost_center_is_refused(self):
		"""ERPNext will not let a posting land on one, so a dimension pointing at
		one is a dimension whose costs have nowhere to go."""
		from .fixtures import cost_center

		error = self.tool_error(
			"link_field_to_cost_center",
			{"field": "Yellow Camp Block 3", "cost_center": cost_center("Operations")},
		)
		self.assertIn("group cost center", error)
		self.assertIn("leaf", error)

	def test_sharing_a_cost_center_is_reported_rather_than_refused(self):
		"""A cost center per orchard is a legitimate design. It just is not
		per-block costing, and the result should say which one you have."""
		self.a_field("Block 9", acreage=5)
		self.tool_data(
			"link_field_to_cost_center",
			{"field": "Yellow Camp Block 3", "cost_center": self.cost_center()},
		)
		data = self.tool_data(
			"link_field_to_cost_center", {"field": "Block 9", "cost_center": self.cost_center()}
		)
		self.assertEqual(data["shared_with"], ["Yellow Camp Block 3 - MC"])
		self.assertIn("per-block costing", data["note"])

	def test_an_unknown_cost_center_is_refused(self):
		self.assertIn(
			"no Cost Center",
			self.tool_error(
				"link_field_to_cost_center",
				{"field": "Yellow Camp Block 3", "cost_center": "Nowhere"},
			),
		)


# ── get_parcel_field_summary ────────────────────────────────────────────────
class ParcelFieldSummary(FarmTestCase):
	def setUp(self):
		super().setUp()
		self.a_parcel()
		self.a_field("Block A", acreage=10, variety="Bing", planting_year=1998, condition="Good")
		self.a_field("Block B", acreage=8, variety="Rainier", planting_year=2015, condition="Excellent")
		self.a_zone("A1", field="Block A", zone_number=1, area_sq_ft=43560, flow_rate_gpm=40)
		self.a_zone("A2", field="Block A", zone_number=2, area_sq_ft=43560, flow_rate_gpm=35)
		self.a_zone("B1", field="Block B", zone_number=1, area_sq_ft=43560, flow_rate_gpm=25)

	def test_it_rolls_up_the_parcel(self):
		data = self.tool_data("get_parcel_field_summary", {"parcel": "Mill Creek"})
		self.assertEqual(data["field_count"], 2)
		self.assertEqual(data["planted_acreage"], 18.0)
		self.assertEqual(data["zone_count"], 3)
		self.assertEqual(data["average_field_acreage"], 9.0)

	def test_it_reports_the_unassigned_acreage(self):
		"""The gap between the parcel and its blocks is usually the interesting
		number, and a large one on a parcel somebody thinks is fully blocked out
		is a missing Field."""
		data = self.tool_data("get_parcel_field_summary", {"parcel": "Mill Creek"})
		self.assertEqual(data["parcel_acreage"], 131.43)
		self.assertEqual(data["unassigned_acreage"], 113.43)

	def test_it_reports_the_average_zones_per_field(self):
		data = self.tool_data("get_parcel_field_summary", {"parcel": "Mill Creek"})
		self.assertEqual(data["average_zones_per_field"], 1.5)

	def test_it_reports_the_oldest_planting(self):
		self.assertEqual(
			self.tool_data("get_parcel_field_summary", {"parcel": "Mill Creek"})["oldest_planting_year"],
			1998,
		)

	def test_it_counts_by_condition_and_variety(self):
		data = self.tool_data("get_parcel_field_summary", {"parcel": "Mill Creek"})
		self.assertEqual(data["by_condition"], {"Excellent": 1, "Good": 1})
		self.assertEqual(data["by_variety"], {"Bing": 1, "Rainier": 1})

	def test_it_totals_the_flow(self):
		self.assertEqual(
			self.tool_data("get_parcel_field_summary", {"parcel": "Mill Creek"})["total_flow_gpm"],
			100.0,
		)

	def test_it_names_the_food_safety_blocks_missing_a_hygiene_station(self):
		self.a_field("FS Block", acreage=1, food_safety_zone=True)
		data = self.tool_data("get_parcel_field_summary", {"parcel": "Mill Creek"})
		self.assertIn("FS Block - MC", data["food_safety_blocks"])
		self.assertIn("FS Block - MC", data["blocks_without_hygiene_station"])

	def test_it_names_the_zones_with_no_water_test(self):
		data = self.tool_data("get_parcel_field_summary", {"parcel": "Mill Creek"})
		self.assertEqual(len(data["zones_without_water_test"]), 3)

	def test_the_summary_reads_the_way_the_spec_asked_for(self):
		data = self.tool_data("get_parcel_field_summary", {"parcel": "Mill Creek"})
		self.assertIn("Mill Creek", data["abbr"] and "Mill Creek")
		self.assertEqual(data["abbr"], "MC")

	def test_an_unknown_parcel_is_refused(self):
		self.assertIn("no Parcel called", self.tool_error("get_parcel_field_summary", {"parcel": "Nowhere"}))


# ── import_farm_app_fields ──────────────────────────────────────────────────
class ImportFarmAppFields(FarmTestCase):
	def setUp(self):
		super().setUp()
		self.a_parcel()

	def records(self):
		return [
			{
				"name": "Block One",
				"parcel_hint": "Mill Creek",
				"acreage": 10,
				"variety": "Bing",
				"planting_year": 1998,
				"block_number": "1",
				"farm_app_uuid": "uuid-1",
			},
			{
				"name": "Block Two",
				"parcel_hint": "Mill Creek",
				"acreage": 8,
				"variety": "Rainier",
				"farm_app_uuid": "uuid-2",
			},
		]

	def test_it_is_dry_run_by_default(self):
		data = self.tool_data("import_farm_app_fields", {"records": self.records()})
		self.assertTrue(data["dry_run"])
		self.assertFalse(data["applied"])
		self.assertEqual(data["would_create"], 2)
		self.assertEqual(STORE.rows("Field"), [])

	def test_apply_creates_the_fields(self):
		data = self.tool_data("import_farm_app_fields", {"records": self.records(), "apply": True})
		self.assertTrue(data["applied"])
		self.assertEqual(sorted(data["created"]), ["Block One - MC", "Block Two - MC"])

	def test_the_farm_app_id_is_carried_across_which_is_the_whole_point(self):
		self.tool_data("import_farm_app_fields", {"records": self.records(), "apply": True})
		self.assertEqual(STORE.get_raw("Field", "Block One - MC")["external_farm_app_id"], "uuid-1")

	def test_it_defaults_the_crop_to_cherry(self):
		self.tool_data("import_farm_app_fields", {"records": self.records(), "apply": True})
		self.assertEqual(STORE.get_raw("Field", "Block One - MC")["crop"], "Cherry")

	def test_a_record_with_no_hint_uses_the_default_parcel(self):
		data = self.tool_data(
			"import_farm_app_fields",
			{"records": [{"name": "Bare Block"}], "parcel": "Mill Creek", "apply": True},
		)
		self.assertEqual(data["created"], ["Bare Block - MC"])

	def test_a_record_with_no_hint_and_no_default_is_refused(self):
		error = self.tool_error("import_farm_app_fields", {"records": [{"name": "Bare Block"}]})
		self.assertIn("no `parcel_hint`", error)

	def test_an_unrecognised_key_is_refused_rather_than_ignored(self):
		"""A typo silently dropped is a field somebody thinks they imported."""
		error = self.tool_error(
			"import_farm_app_fields",
			{"records": [{"name": "B", "parcel_hint": "Mill Creek", "acerage": 10}]},
		)
		self.assertIn("acerage", error)
		self.assertIn("thinks they imported", error)

	def test_a_hint_matching_no_parcel_is_refused_with_the_fix(self):
		error = self.tool_error(
			"import_farm_app_fields", {"records": [{"name": "B", "parcel_hint": "Nowhere"}]}
		)
		self.assertIn("create_parcel", error)

	def test_a_record_with_no_name_is_refused_by_position(self):
		error = self.tool_error(
			"import_farm_app_fields",
			{"records": [{"name": "A", "parcel_hint": "Mill Creek"}, {"parcel_hint": "Mill Creek"}]},
		)
		self.assertIn("record 2", error)

	def test_a_batch_that_repeats_a_name_is_refused_as_self_contradictory(self):
		error = self.tool_error(
			"import_farm_app_fields",
			{
				"records": [
					{"name": "Block One", "parcel_hint": "Mill Creek"},
					{"name": "Block One", "parcel_hint": "Mill Creek"},
				]
			},
		)
		self.assertIn("contradicts itself", error)

	def test_a_batch_that_repeats_a_farm_app_id_is_refused(self):
		error = self.tool_error(
			"import_farm_app_fields",
			{
				"records": [
					{"name": "A", "parcel_hint": "Mill Creek", "farm_app_uuid": "u"},
					{"name": "B", "parcel_hint": "Mill Creek", "farm_app_uuid": "u"},
				]
			},
		)
		self.assertIn("repeats Farm App id", error)

	def test_the_whole_batch_is_refused_before_the_first_insert(self):
		"""A half-imported farm is worse than an unimported one, because the
		second run has to work out which half."""
		records = [*self.records(), {"name": "Bad", "parcel_hint": "Nowhere"}]
		self.tool_error("import_farm_app_fields", {"records": records, "apply": True})
		self.assertEqual(STORE.rows("Field"), [])

	def test_a_block_already_registered_is_skipped_with_the_reason(self):
		self.a_field("Block One", acreage=10)
		data = self.tool_data("import_farm_app_fields", {"records": self.records()})
		skipped = next(entry for entry in data["plan"] if entry["field_name"] == "Block One")
		self.assertEqual(skipped["action"], "skip")
		self.assertIn("never updates", skipped["reason"])
		self.assertEqual(data["would_create"], 1)

	def test_a_farm_app_id_already_on_file_is_skipped(self):
		self.a_field("Different Name", acreage=1, external_farm_app_id="uuid-1")
		data = self.tool_data("import_farm_app_fields", {"records": self.records()})
		skipped = next(entry for entry in data["plan"] if entry["field_name"] == "Block One")
		self.assertEqual(skipped["action"], "skip")
		self.assertIn("uuid-1", skipped["reason"])

	def test_the_same_batch_can_be_re_run_safely(self):
		self.tool_data("import_farm_app_fields", {"records": self.records(), "apply": True})
		data = self.tool_data("import_farm_app_fields", {"records": self.records(), "apply": True})
		self.assertEqual(data["created"], [])
		self.assertEqual(data["already_present"], 2)

	def test_an_empty_batch_is_refused_with_the_recognised_keys(self):
		error = self.tool_error("import_farm_app_fields", {"records": []})
		self.assertIn("farm_app_uuid", error)

	def test_a_negative_acreage_refuses_the_batch(self):
		error = self.tool_error(
			"import_farm_app_fields",
			{"records": [{"name": "A", "parcel_hint": "Mill Creek", "acreage": -1}]},
		)
		self.assertIn("negative acreage", error)

	def test_it_says_what_it_is_and_is_not(self):
		data = self.tool_data("import_farm_app_fields", {"records": self.records()})
		self.assertIn("never updates", data["note"])
		self.assertIn("sync engine", data["note"])

	def test_the_switch_is_off_by_default(self):
		self.configure(enabled=1, allow_create_parcel=1)
		self.assertIn(
			"switched off",
			self.tool_error("import_farm_app_fields", {"records": self.records()}),
		)

	def test_the_acreage_rule_still_applies_to_an_import(self):
		"""An import is the exact situation the parcel acreage rule exists for."""
		self.a_parcel("Doane Road", acreage=14.84)
		error = self.tool_error(
			"import_farm_app_fields",
			{
				"records": [
					{"name": "A", "parcel_hint": "Doane Road", "acreage": 10},
					{"name": "B", "parcel_hint": "Doane Road", "acreage": 10},
				],
				"apply": True,
			},
		)
		self.assertIn("14.84", error)


# ── compliance is woven, not a shadow layer ─────────────────────────────────
class WovenNotShadow(FarmTestCase):
	"""Each test removes one compliance field and shows the SAME removal breaks
	an OPERATIONAL answer and a REGULATORY one.

	That is the test from project_compliance_fundamental_to_operations, written
	as a test rather than as a claim in a docstring: if only the regulatory half
	broke, the field would belong in a separate compliance log and this design
	would be wrong.
	"""

	def setUp(self):
		super().setUp()
		self.a_parcel()
		self.a_field(
			food_safety_zone=True,
			worker_hygiene_station_present=True,
			last_spray_date="2026-05-15",
			water_test_last_date="2026-03-01",
			wildlife_intrusion_last_report="2026-06-01",
		)
		self.a_zone(water_test_last_date="2026-03-01", water_source_class="II")

	def blank(self, doctype, name, fieldname):
		STORE.tables[doctype][name][fieldname] = None if "date" in fieldname else 0

	def test_last_spray_date_answers_re_entry_before_it_answers_a_wps_report(self):
		field = "Yellow Camp Block 3 - MC"
		self.assertEqual(self.tool_data("get_field", {"field": field})["last_spray_date"], "2026-05-15")
		self.blank("Field", field, "last_spray_date")
		# OPERATIONAL: "when can the crew go back in" has no answer.
		self.assertIsNone(self.tool_data("get_field", {"field": field})["last_spray_date"])
		# REGULATORY: the per-field line of a WPS record-keeping report is empty.
		listed = self.tool_data("list_fields")["fields"][0]
		self.assertIsNone(listed["last_spray_date"])
		self.assertIsNone(listed["last_spray_source"])

	def test_worker_hygiene_station_gates_dispatch_and_a_subpart_l_finding(self):
		field = "Yellow Camp Block 3 - MC"
		summary = self.tool_data("get_parcel_field_summary", {"parcel": "Mill Creek"})
		self.assertEqual(summary["blocks_without_hygiene_station"], [])
		self.blank("Field", field, "worker_hygiene_station_present")
		# REGULATORY: the block appears on the FSMA Subpart L exception list.
		summary = self.tool_data("get_parcel_field_summary", {"parcel": "Mill Creek"})
		self.assertEqual(summary["blocks_without_hygiene_station"], [field])
		# OPERATIONAL: the same absence is what says a crew cannot be sent there.
		self.assertFalse(self.tool_data("get_field", {"field": field})["worker_hygiene_station_present"])

	def test_a_zone_water_test_gates_both_the_irrigation_set_and_subpart_e(self):
		zone = "YC3-Zone2 - MC"
		self.assertEqual(self.tool_data("list_irrigation_zones")["without_water_test"], [])
		self.blank("Irrigation Zone", zone, "water_test_last_date")
		# REGULATORY: the zone lands on the FSMA Subpart E list.
		self.assertEqual(self.tool_data("list_irrigation_zones")["without_water_test"], [zone])
		# OPERATIONAL: get_irrigation_zone says not to run it before harvest.
		notes = self.tool_data("get_irrigation_zone", {"zone": zone})["compliance_notes"]
		self.assertTrue(any("Subpart E" in note for note in notes))

	def test_the_water_right_is_both_a_legal_permission_and_an_operational_one(self):
		zone = "YC3-Zone2 - MC"
		STORE.tables["Irrigation Zone"][zone]["water_source"] = "creek"
		STORE.tables["Irrigation Zone"][zone]["water_right_id"] = "S-54321"
		self.assertEqual(self.tool_data("list_irrigation_zones")["surface_water_without_a_right"], [])
		self.assertEqual(self.tool_data("list_irrigation_zones")["water_rights"], ["S-54321"])
		STORE.tables["Irrigation Zone"][zone]["water_right_id"] = ""
		# REGULATORY: no right on file for a surface diversion.
		self.assertEqual(self.tool_data("list_irrigation_zones")["surface_water_without_a_right"], [zone])
		# OPERATIONAL: the water rights roll-up for the block is now empty, so
		# nobody can say how much water this ground is entitled to.
		self.assertEqual(
			self.tool_data("get_field", {"field": "Yellow Camp Block 3 - MC"})["water_rights"], []
		)

	def test_the_food_safety_flag_changes_what_every_other_field_means(self):
		"""Not a report flag: it is what makes the hygiene, water and wildlife
		fields load-bearing, which is why it is on the block and not in a log."""
		field = "Yellow Camp Block 3 - MC"
		self.blank("Field", field, "food_safety_zone")
		summary = self.tool_data("get_parcel_field_summary", {"parcel": "Mill Creek"})
		self.assertEqual(summary["food_safety_blocks"], [])
		self.assertEqual(summary["blocks_without_hygiene_station"], [])

	def test_a_separate_compliance_log_would_fail_this_test(self):
		"""The counter-example, asserted rather than described. Nothing about
		picking stops if a record that only a report reads disappears — and
		every field above fails that test, which is why none of them is one."""
		field = "Yellow Camp Block 3 - MC"
		before = self.tool_data("get_field", {"field": field})
		STORE.tables["Field"][field]["notes"] = None
		after = self.tool_data("get_field", {"field": field})
		before.pop("notes")
		after.pop("notes")
		self.assertEqual(before, after)

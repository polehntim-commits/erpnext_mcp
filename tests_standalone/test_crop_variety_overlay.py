# SPDX-License-Identifier: MIT
"""The per-variety overlays, and the rootstock that was recorded at the wrong grain.

v0.114.0. Four things these tests are really about.

THE FALLBACK IS PER FIELD, AND THAT IS THE ONE THING A CALLER GETS WRONG. A crop
records water demand by growth stage; a variety may depart from it; the overlay
is sparse. Resolving per ROW — take the override row if there is one, else the
crop row — is the obvious implementation and it silently discards the crop's
weekly depth every time a variety overrides only the Kc. There is a test that
pins the per-field behaviour on exactly that case, because both readings look
right in a payload and only one of them waters the block correctly.

BLANK IS NOT ZERO, AGAIN, AND HERE IT IRRIGATES. An override with an empty Kc is
a variety with no opinion about Kc, so the crop's figure stands. An override of
`0.0` is a variety that genuinely takes no water at that stage. `float(x or 0)`
collapses the two, which would let a half-finished override water a block at
zero, so both are tested and they are tested adjacently.

A ROW THAT CANNOT FIRE IS REFUSED RATHER THAN STORED. The overlays hang off the
CROP and name their variety as text — Frappe has no nested child tables and Crop
Variety is itself a child — so a row can name a variety the catalogue does not
list. It stores perfectly well, resolves to nothing, and leaves a form showing
what looks like a recorded decision while the reader falls back to the crop
default. Invisible from both ends, which is why it is worth a refusal. The same
reasoning refuses an override carrying neither of its two numbers.

THE ROOTSTOCK IS THE PLANTING'S, NOT THE CATALOGUE'S. `Crop Variety` has one row
per variety, so it holds one rootstock for 'Bing' while the farm has Bing on
Mazzard in the old block and Bing on Gisela 6 in the 2019 planting — different
trees, different vigour, different yields. The backfill patch carries the
catalogue value down onto plantings that record none, and the tests below pin
the two properties that make it safe to run twice: it only ever fills a blank,
and it never rewrites a value somebody typed against a block.
"""

from erpnext_mcp.patches import backfill_planting_rootstock as rootstock_patch

from .fixtures import V12TestCase
from .harness import STORE, frappe

ALL_ON = {
	"allow_get_crop": 1,
	"allow_create_crop": 1,
	"allow_get_variety_care_recipe": 1,
}

#: The crop's own demand, deliberately covering only four of the seven stages —
#: a real record is partial, and a fixture that filled all seven would hide the
#: "variety adds a stage the crop never modelled" case entirely.
CROP_STAGES = (
	{"growth_stage": "Dormant", "crop_coefficient_kc": 0.2, "water_inches_per_week": 0.0},
	{"growth_stage": "Bloom", "crop_coefficient_kc": 0.55, "water_inches_per_week": 0.9},
	{"growth_stage": "Fruit Development", "crop_coefficient_kc": 1.0, "water_inches_per_week": 1.6},
	{"growth_stage": "Harvest", "crop_coefficient_kc": 0.9, "water_inches_per_week": 1.4},
)


class OverlayTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON)

	def a_crop(self, crop_name="Sweet Cherry", **overrides):
		payload = {
			"crop_name": crop_name,
			"crop_type": "Stone Fruit",
			"growth_cycle": "Perennial",
			"default_phi_days": 3,
			"varieties": [
				{"variety_name": "Bing", "rootstock": "Mazzard", "pollination_group": "S3S4"},
				{"variety_name": "Rainier", "rootstock": "Mazzard", "pollination_group": "S1S4"},
			],
			"water_requirements": [dict(row) for row in CROP_STAGES],
		}
		payload.update(overrides)
		return self.tool_data("create_crop", payload)

	def overlay(self, crop="Sweet Cherry", water=(), protocols=()):
		"""Append overlay rows through the real document, so the controller runs.

		Written as a save rather than a `STORE.seed` on purpose: every refusal
		these tests assert lives in `Crop.validate`, and seeding rows straight
		into the table would bypass the thing under test.
		"""
		doc = frappe.get_doc("Crop", crop)
		for row in water:
			doc.append("variety_water_requirements", dict(row))
		for row in protocols:
			doc.append("variety_protocols", dict(row))
		doc.save()
		return doc

	def recipe(self, variety="Bing", crop="Sweet Cherry"):
		return self.tool_data("get_variety_care_recipe", {"crop": crop, "variety": variety})

	def stage(self, schedule, name):
		return next(row for row in schedule if row["growth_stage"] == name)


class TheFallbackIsPerField(OverlayTestCase):
	def test_a_variety_with_no_overrides_gets_the_crop_everywhere(self):
		self.a_crop()
		data = self.recipe()
		self.assertEqual(data["stages_overridden"], [])
		self.assertEqual(len(data["water_schedule"]), len(CROP_STAGES))
		for row in data["water_schedule"]:
			self.assertEqual(row["crop_coefficient_kc_source"], "crop default")
			self.assertFalse(row["is_overridden"])

	def test_overriding_only_the_kc_leaves_the_crops_weekly_depth_standing(self):
		"""THE TEST THIS MODULE EXISTS FOR.

		Resolving per row would hand back `water_inches_per_week: None` here,
		because the override row has no depth in it — and a caller would read
		that as "this variety needs no water at bloom" rather than "this variety
		wants the crop's 0.9 inches at a lower coefficient".
		"""
		self.a_crop()
		self.overlay(water=[{"variety": "Bing", "growth_stage": "Bloom", "crop_coefficient_kc": 0.45}])

		bloom = self.stage(self.recipe()["water_schedule"], "Bloom")
		self.assertEqual(bloom["crop_coefficient_kc"], 0.45)
		self.assertEqual(bloom["crop_coefficient_kc_source"], "variety override")
		self.assertEqual(bloom["water_inches_per_week"], 0.9)
		self.assertEqual(bloom["water_inches_per_week_source"], "crop default")
		self.assertTrue(bloom["is_overridden"])

	def test_the_crop_default_is_reported_beside_the_override(self):
		"""What it would have been, so a reader can see the size of the departure."""
		self.a_crop()
		self.overlay(water=[{"variety": "Bing", "growth_stage": "Bloom", "crop_coefficient_kc": 0.45}])
		bloom = self.stage(self.recipe()["water_schedule"], "Bloom")
		self.assertEqual(bloom["crop_default_kc"], 0.55)
		self.assertEqual(bloom["crop_default_water_inches_per_week"], 0.9)

	def test_an_override_only_touches_the_variety_it_names(self):
		self.a_crop()
		self.overlay(water=[{"variety": "Bing", "growth_stage": "Bloom", "crop_coefficient_kc": 0.45}])
		self.assertEqual(self.recipe("Rainier")["stages_overridden"], [])
		self.assertEqual(
			self.stage(self.recipe("Rainier")["water_schedule"], "Bloom")["crop_coefficient_kc"], 0.55
		)

	def test_an_override_is_matched_ignoring_case(self):
		"""'bing' and 'Bing' are one tree. A miss here falls back silently."""
		self.a_crop()
		self.overlay(water=[{"variety": "bing", "growth_stage": "Bloom", "crop_coefficient_kc": 0.45}])
		self.assertEqual(self.recipe("BING")["stages_overridden"], ["Bloom"])


class BlankIsNotZero(OverlayTestCase):
	def test_an_override_of_zero_kc_is_kept_as_zero(self):
		"""A variety that genuinely takes no water at a stage, and says so."""
		self.a_crop()
		self.overlay(
			water=[
				{
					"variety": "Bing",
					"growth_stage": "Dormant",
					"crop_coefficient_kc": 0.0,
					"water_inches_per_week": 0.0,
				}
			]
		)
		dormant = self.stage(self.recipe()["water_schedule"], "Dormant")
		self.assertEqual(dormant["crop_coefficient_kc"], 0.0)
		self.assertEqual(dormant["crop_coefficient_kc_source"], "variety override")

	def test_a_blank_kc_defers_to_the_crop_rather_than_reading_as_zero(self):
		self.a_crop()
		self.overlay(water=[{"variety": "Bing", "growth_stage": "Bloom", "water_inches_per_week": 1.2}])
		bloom = self.stage(self.recipe()["water_schedule"], "Bloom")
		self.assertEqual(bloom["crop_coefficient_kc"], 0.55)
		self.assertEqual(bloom["crop_coefficient_kc_source"], "crop default")
		self.assertEqual(bloom["water_inches_per_week"], 1.2)


class TheScheduleReadsLikeASeason(OverlayTestCase):
	def test_stages_come_back_in_season_order_not_alphabetical(self):
		"""Alphabetical puts Bloom before Bud Break and Dormant in the middle,
		which reads as a corrupted record rather than as a schedule."""
		self.a_crop()
		self.assertEqual(
			[row["growth_stage"] for row in self.recipe()["water_schedule"]],
			["Dormant", "Bloom", "Fruit Development", "Harvest"],
		)

	def test_a_stage_only_the_variety_records_is_still_returned(self):
		"""A late variety needing post-harvest water its crop never modelled.

		Dropping it would lose the only row that said so, so it comes back with
		its crop-level figures reported as None rather than as zero.
		"""
		self.a_crop()
		self.overlay(
			water=[
				{
					"variety": "Bing",
					"growth_stage": "Post-Harvest",
					"crop_coefficient_kc": 0.8,
					"water_inches_per_week": 1.1,
				}
			]
		)
		schedule = self.recipe()["water_schedule"]
		self.assertEqual(schedule[-1]["growth_stage"], "Post-Harvest")
		post = self.stage(schedule, "Post-Harvest")
		self.assertEqual(post["crop_coefficient_kc"], 0.8)
		self.assertIsNone(post["crop_default_kc"])
		self.assertIsNone(post["water_inches_per_week_source"] and post["crop_default_water_inches_per_week"])

	def test_a_crop_with_no_water_rows_says_so_rather_than_returning_zeroes(self):
		self.a_crop("Hops", water_requirements=[], varieties=[{"variety_name": "Cascade"}])
		data = self.recipe("Cascade", crop="Hops")
		self.assertEqual(data["water_schedule"], [])
		self.assertTrue(any("not a schedule of zero" in note for note in data["agronomy_notes"]))


class TheProtocolIsACareRecipe(OverlayTestCase):
	def test_steps_are_grouped_by_practice(self):
		self.a_crop()
		self.overlay(
			protocols=[
				{
					"variety": "Bing",
					"practice": "Gibberellic Acid",
					"timing_stage": "Fruit Set",
					"rate": "30 ppm",
				},
				{"variety": "Bing", "practice": "Thinning", "timing_stage": "Bloom"},
			]
		)
		data = self.recipe()
		self.assertEqual(data["practices_recorded"], ["Gibberellic Acid", "Thinning"])
		self.assertEqual(len(data["protocol_by_practice"]["Gibberellic Acid"]), 1)

	def test_a_program_of_several_applications_is_kept_as_several_steps(self):
		"""The deliberate NON-rule. A GA program is two or three applications at
		different timings, and a uniqueness rule on (variety, practice) would
		refuse the commonest real recipe in the file."""
		self.a_crop()
		self.overlay(
			protocols=[
				{
					"variety": "Bing",
					"practice": "Gibberellic Acid",
					"timing_stage": "Fruit Set",
					"timing_detail": "straw colour",
					"rate": "30 ppm",
				},
				{
					"variety": "Bing",
					"practice": "Gibberellic Acid",
					"timing_stage": "Fruit Development",
					"timing_detail": "10 days later",
					"rate": "20 ppm",
				},
			]
		)
		steps = self.recipe()["protocol_by_practice"]["Gibberellic Acid"]
		self.assertEqual(len(steps), 2)
		# Sorted into season order within the practice, so the program reads as
		# the sequence it is rather than as insertion order.
		self.assertEqual([row["timing_stage"] for row in steps], ["Fruit Set", "Fruit Development"])

	def test_the_rate_survives_as_text_with_its_units(self):
		"""ppm and pints per acre do not convert without a dilution, so a float
		here would be a number that looks computable and is not."""
		self.a_crop()
		self.overlay(
			protocols=[{"variety": "Bing", "practice": "Plant Growth Regulator", "rate": "1 qt/100 gal"}]
		)
		self.assertEqual(self.recipe()["protocol_steps"][0]["rate"], "1 qt/100 gal")

	def test_a_variety_with_no_protocol_is_named_as_a_gap(self):
		self.a_crop()
		notes = " ".join(self.recipe()["agronomy_notes"])
		self.assertIn("No cultural practice protocol", notes)

	def test_it_says_a_protocol_is_a_plan_and_not_an_application(self):
		self.a_crop()
		self.assertIn("Spray Application", self.recipe()["protocol_caveat"])


class ARowThatCannotFireIsRefused(OverlayTestCase):
	def test_an_override_for_a_variety_the_crop_does_not_list(self):
		self.a_crop()
		with self.assertRaises(Exception) as caught:
			self.overlay(water=[{"variety": "Lapins", "growth_stage": "Bloom", "crop_coefficient_kc": 0.5}])
		message = str(caught.exception)
		self.assertIn("Lapins", message)
		# Names what DOES exist — the failure is nearly always a spelling.
		self.assertIn("Bing", message)

	def test_two_overrides_for_one_variety_and_stage(self):
		self.a_crop()
		with self.assertRaises(Exception) as caught:
			self.overlay(
				water=[
					{"variety": "Bing", "growth_stage": "Bloom", "crop_coefficient_kc": 0.45},
					{"variety": "Bing", "growth_stage": "Bloom", "crop_coefficient_kc": 0.60},
				]
			)
		self.assertIn("row order", str(caught.exception))

	def test_an_override_carrying_neither_number(self):
		"""It resolves, it matches a stage, and it changes nothing."""
		self.a_crop()
		with self.assertRaises(Exception) as caught:
			self.overlay(water=[{"variety": "Bing", "growth_stage": "Bloom", "notes": "runs dry early"}])
		self.assertIn("changes nothing", str(caught.exception))

	def test_a_kc_above_the_ceiling_is_a_decimal_point_in_the_wrong_place(self):
		self.a_crop()
		with self.assertRaises(Exception) as caught:
			self.overlay(water=[{"variety": "Bing", "growth_stage": "Bloom", "crop_coefficient_kc": 8.5}])
		self.assertIn("decimal point", str(caught.exception))

	def test_the_same_protocol_step_entered_twice(self):
		self.a_crop()
		with self.assertRaises(Exception) as caught:
			self.overlay(
				protocols=[
					{"variety": "Bing", "practice": "Thinning", "timing_stage": "Bloom", "product": ""},
					{"variety": "Bing", "practice": "Thinning", "timing_stage": "Bloom", "product": ""},
				]
			)
		self.assertIn("entered twice", str(caught.exception))

	def test_a_protocol_for_a_variety_the_crop_does_not_list(self):
		self.a_crop()
		with self.assertRaises(Exception) as caught:
			self.overlay(protocols=[{"variety": "Lapins", "practice": "Thinning"}])
		self.assertIn("Lapins", str(caught.exception))

	def test_the_catalogue_spelling_is_written_back_onto_the_row(self):
		"""So the stored row and the variety list agree exactly, and the
		resolver's lookup is a plain match rather than a second casefold."""
		self.a_crop()
		doc = self.overlay(water=[{"variety": "bInG", "growth_stage": "Bloom", "crop_coefficient_kc": 0.45}])
		self.assertEqual(doc.variety_water_requirements[0].variety, "Bing")

	def test_a_recipe_for_an_unknown_variety_names_the_ones_that_exist(self):
		self.a_crop()
		message = self.tool_error("get_variety_care_recipe", {"crop": "Sweet Cherry", "variety": "Lapins"})
		self.assertIn("Bing", message)
		self.assertIn("Rainier", message)


class GetCropReportsTheOverlaysRaw(OverlayTestCase):
	def test_it_lists_the_varieties_that_depart_from_the_crop(self):
		self.a_crop()
		self.overlay(
			water=[{"variety": "Bing", "growth_stage": "Bloom", "crop_coefficient_kc": 0.45}],
			protocols=[{"variety": "Rainier", "practice": "Thinning"}],
		)
		data = self.tool_data("get_crop", {"crop": "Sweet Cherry"})
		self.assertEqual(data["varieties_with_water_overrides"], ["Bing"])
		self.assertEqual(data["varieties_with_protocols"], ["Rainier"])

	def test_it_points_at_the_resolver_rather_than_inviting_hand_resolution(self):
		self.a_crop()
		self.assertIn(
			"get_variety_care_recipe", self.tool_data("get_crop", {"crop": "Sweet Cherry"})["overlay_caveat"]
		)

	def test_it_says_the_catalogue_rootstock_is_not_the_binding_one(self):
		self.a_crop()
		caveat = self.tool_data("get_crop", {"crop": "Sweet Cherry"})["rootstock_caveat"]
		self.assertIn("Planting Season.rootstock", caveat)
		self.assertIn("Field.rootstock", caveat)

	def test_the_switch_is_respected(self):
		self.a_crop()
		self.configure(enabled=1, allow_create_crop=1, allow_get_variety_care_recipe=0)
		self.assertIn(
			"allow_get_variety_care_recipe",
			self.tool_error("get_variety_care_recipe", {"crop": "Sweet Cherry", "variety": "Bing"}),
		)


class TheRootstockBackfill(OverlayTestCase):
	"""The patch that carries the catalogue default down onto the plantings."""

	def a_catalogue(self):
		"""A crop whose catalogue names a rootstock for one variety and not the other.

		The child rows are seeded INSIDE the Crop rather than as their own table,
		because that is where the double keeps them (see `CHILD_TABLE_SOURCES` in
		`harness.py`) and it is what `frappe.db.get_all` on the child reads back.
		"""
		STORE.seed(
			"Crop",
			[
				{
					"name": "Sweet Cherry",
					"crop_name": "Sweet Cherry",
					"varieties": [
						{
							"name": "cv-bing",
							"parent": "Sweet Cherry",
							"parenttype": "Crop",
							"parentfield": "varieties",
							"variety_name": "Bing",
							"rootstock": "Mazzard",
						},
						{
							"name": "cv-rainier",
							"parent": "Sweet Cherry",
							"parenttype": "Crop",
							"parentfield": "varieties",
							"variety_name": "Rainier",
							"rootstock": "",
						},
					],
				}
			],
		)

	def a_planting(self, name, variety="Bing", rootstock="", crop="Sweet Cherry"):
		STORE.seed(
			"Planting Season",
			[{"name": name, "crop": crop, "variety": variety, "rootstock": rootstock}],
		)

	def test_it_fills_a_blank_rootstock_from_the_catalogue(self):
		self.a_catalogue()
		self.a_planting("ps-1")
		report = rootstock_patch.backfill_planting_rootstock()
		self.assertEqual(report["filled"]["Planting Season"], 1)
		self.assertEqual(STORE.get_raw("Planting Season", "ps-1")["rootstock"], "Mazzard")

	def test_it_never_overwrites_a_rootstock_typed_against_a_block(self):
		"""The planting is the better record by construction — somebody typed it
		against that block. No 'which is right' question is raised."""
		self.a_catalogue()
		self.a_planting("ps-1", rootstock="Gisela 6")
		rootstock_patch.backfill_planting_rootstock()
		self.assertEqual(STORE.get_raw("Planting Season", "ps-1")["rootstock"], "Gisela 6")

	def test_a_variety_the_catalogue_does_not_name_is_left_blank(self):
		self.a_catalogue()
		self.a_planting("ps-1", variety="Lapins")
		report = rootstock_patch.backfill_planting_rootstock()
		self.assertEqual(report["unmatched"]["Planting Season"], 1)
		self.assertEqual(STORE.get_raw("Planting Season", "ps-1")["rootstock"], "")

	def test_a_variety_whose_catalogue_row_names_no_rootstock_is_left_blank(self):
		self.a_catalogue()
		self.a_planting("ps-1", variety="Rainier")
		rootstock_patch.backfill_planting_rootstock()
		self.assertEqual(STORE.get_raw("Planting Season", "ps-1")["rootstock"], "")

	def test_it_matches_on_the_crop_as_well_as_the_variety(self):
		"""Two crops can spell a variety the same way with different rootstocks."""
		self.a_catalogue()
		self.a_planting("ps-1", crop="Apple")
		rootstock_patch.backfill_planting_rootstock()
		self.assertEqual(STORE.get_raw("Planting Season", "ps-1")["rootstock"], "")

	def test_it_matches_ignoring_case_and_surrounding_space(self):
		self.a_catalogue()
		self.a_planting("ps-1", variety=" bing ", crop="sweet cherry")
		rootstock_patch.backfill_planting_rootstock()
		self.assertEqual(STORE.get_raw("Planting Season", "ps-1")["rootstock"], "Mazzard")

	def test_running_it_twice_changes_nothing_the_second_time(self):
		"""A seed, not a sync. After it runs the two columns are free to
		disagree, and re-running must not undo a correction."""
		self.a_catalogue()
		self.a_planting("ps-1")
		rootstock_patch.backfill_planting_rootstock()
		frappe.db.set_value("Planting Season", "ps-1", "rootstock", "Krymsk 5")
		second = rootstock_patch.backfill_planting_rootstock()
		self.assertEqual(second["filled"]["Planting Season"], 0)
		self.assertEqual(STORE.get_raw("Planting Season", "ps-1")["rootstock"], "Krymsk 5")

	def test_it_reports_what_it_did_in_a_sentence_that_names_the_new_rule(self):
		self.a_catalogue()
		self.a_planting("ps-1")
		lines = " ".join(rootstock_patch.report_lines(rootstock_patch.backfill_planting_rootstock()))
		self.assertIn("THE PLANTING IS NOW THE BINDING ANSWER", lines)

	def test_it_says_nothing_and_raises_nothing_with_no_catalogue(self):
		"""Inside `bench migrate` an exception aborts the migration for the whole
		bench, and a blank rootstock is what every site had this morning."""
		report = rootstock_patch.backfill_planting_rootstock()
		self.assertTrue(report["skipped"])
		self.assertEqual(rootstock_patch.report_lines(report)[0].count("were not backfilled"), 1)

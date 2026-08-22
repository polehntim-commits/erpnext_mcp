# SPDX-License-Identifier: MIT
"""`bbch` — reading a growth stage, and offering the ones a crop actually has.

Cycle 2 of the Farm App retirement. The farm_app's `bbch_helper` built the
picker every scouting form and every iOS growth-stage dropdown draws from, and
the payload shape it produced is a published contract. Four claims, ordered by
what breaking each one costs.

1. **A STAGE COLUMN THAT IS NOT A STAGE READS AS NOTHING, NOT AS A NUMBER.**
   `WhatParses`. `Crop Observation.growth_stage_code` is free text and holds
   `"BBCH 87"`, `" 87 "` and `"2026-08-01"`. The date is the one that matters:
   answering `20` for it would file an observation under a stage nobody
   recorded, and no test downstream would ever see it.

2. **THE PICKER'S SHAPE IS THE iOS CONTRACT.** `ThePayloadShape`. Groups,
   options with `code`/`desc`/`depth`, a `desc_map`, varieties, `is_perennial`.
   Cycle 3 remaps the farm_app's `/api/refdata` route onto this app; a payload
   that changed shape on the way breaks the picker on every phone in the field.

3. **VALID AND OFFERED ARE TWO QUESTIONS.** `TwoQuestionsAboutOneCode`. `29` is
   a real BBCH code and a nonsense cherry stage. A caller that wants to warn
   about the second without refusing the first needs both answers, separately.

4. **THE IRRIGATION DERIVATION IS AN AGRONOMIC CLAIM, NOT A CONSTANT.**
   `TheWaterSchedule`. Bloom and fruit set tighten the allowable depletion,
   establishment loosens it, and rooting depth grows with the stage. Each of the
   three is asserted, because each is a number somebody will irrigate on.
"""

import unittest

from erpnext_mcp import bbch

#: A small perennial scale in the shape a Crop stores one, with the dict form of
#: `children` — the form the farm_app's later seeds wrote.
CHERRY = {
	"is_perennial": True,
	"stages": [
		{
			"code": "6",
			"desc": "6 — Flowering",
			"children": {
				"60": {"desc": "60 — First flowers open"},
				"65": {"desc": "65 — Full flowering"},
				"69": {"desc": "69 — End of flowering"},
			},
		},
		{
			"code": "8",
			"desc": "8 — Ripening",
			"children": {"81": {"desc": "81 — Beginning of ripening"}, "87": {"desc": "87 — Harvest"}},
		},
	],
	"varieties": [
		{"name": "Bing", "description": "the standard dark sweet"},
		{"name": "", "description": "an unnamed row nobody can select"},
	],
}

#: The same scale in the LIST form of `children`, which the farm_app's earlier
#: seeds wrote and which is still in the data.
CHERRY_LIST_FORM = {
	"is_perennial": True,
	"stages": [
		{
			"code": "6",
			"description": "Flowering",
			"children": [
				{"code": "60", "description": "First flowers open"},
				{"code": "65", "description": "Full flowering"},
				{"code": "69", "description": "End of flowering"},
			],
		}
	],
}


class WhatParses(unittest.TestCase):
	def test_the_spellings_a_free_text_column_actually_holds(self):
		for written, expected in (
			("87", 87),
			(" 87 ", 87),
			("BBCH 87", 87),
			("87 — Hard dough", 87),
			(87, 87),
			("0", 0),
			("00", 0),
		):
			self.assertEqual(bbch.parse(written), expected, written)

	def test_a_date_in_the_stage_column_is_not_a_stage(self):
		"""The claim worth its own test: three runs of digits is not a code, and
		reading `20` out of `2026-08-01` files an observation under a stage
		nobody recorded."""
		self.assertIsNone(bbch.parse("2026-08-01"))
		self.assertIsNone(bbch.parse("2026"))

	def test_words_and_blanks_are_nothing_rather_than_an_error(self):
		for written in ("petal fall", "", "   ", None, "BBCH", "n/a"):
			self.assertIsNone(bbch.parse(written), repr(written))

	def test_a_boolean_is_not_a_stage_however_much_python_says_it_is_an_int(self):
		self.assertIsNone(bbch.parse(True))
		self.assertIsNone(bbch.parse(False))

	def test_a_single_digit_is_a_principal_stage_and_reads_as_itself(self):
		self.assertEqual(bbch.parse("8"), 8)
		self.assertEqual(bbch.principal("8"), 8)
		self.assertEqual(bbch.principal("87"), 8)
		self.assertEqual(bbch.principal("00"), 0)

	def test_out_of_range_is_refused_in_both_directions(self):
		self.assertIsNone(bbch.parse(-1))
		self.assertIsNone(bbch.parse(100))
		self.assertIsNone(bbch.parse("100"))

	def test_normalise_gives_the_two_character_spelling_things_sort_by(self):
		self.assertEqual(bbch.normalise("BBCH 7"), "07")
		self.assertEqual(bbch.normalise(7), "07")
		self.assertEqual(bbch.normalise("65"), "65")
		self.assertEqual(bbch.normalise("petal fall"), "")
		self.assertLess(bbch.normalise(7), bbch.normalise(65))

	def test_the_code_prefix_comes_off_whichever_dash_wrote_it(self):
		for written in ("87 — Hard dough", "87 – Hard dough", "87 - Hard dough"):
			self.assertEqual(bbch.strip_code_prefix(written), "Hard dough", written)
		self.assertEqual(bbch.strip_code_prefix("Hard dough"), "Hard dough")


class ThePayloadShape(unittest.TestCase):
	def test_the_generic_scale_offers_all_ten_principal_stages(self):
		payload = bbch.picker()
		self.assertEqual(len(payload["groups"]), 10)
		self.assertEqual([group["options"][0]["code"] for group in payload["groups"]], list("0123456789"))

	def test_every_option_carries_a_code_a_description_and_a_depth(self):
		for group in bbch.picker(CHERRY)["groups"]:
			self.assertTrue(group["label"])
			for option in group["options"]:
				self.assertEqual(set(option), {"code", "desc", "depth"})
				self.assertIsInstance(option["depth"], int)

	def test_the_principal_stage_is_itself_the_first_option_in_its_group(self):
		"""A farm records `"8"` when it has not looked closely enough to say
		which eight, so the group header has to be selectable as well as a
		heading."""
		groups = {group["options"][0]["code"]: group for group in bbch.picker(CHERRY)["groups"]}
		self.assertEqual(groups["6"]["options"][0], {"code": "6", "desc": "Flowering", "depth": 0})
		self.assertEqual([option["code"] for option in groups["6"]["options"]], ["6", "60", "65", "69"])

	def test_both_stored_forms_of_children_produce_the_same_picker(self):
		"""Two seasons of the farm_app wrote children two ways and both are in
		the data. A picker that read one of them would silently offer an empty
		group for every crop seeded in the other year."""
		dict_form = bbch.picker(CHERRY)["groups"][0]
		list_form = bbch.picker(CHERRY_LIST_FORM)["groups"][0]
		self.assertEqual(dict_form, list_form)

	def test_the_desc_map_prepends_the_code_and_the_option_does_not(self):
		"""The web template reads `desc_map` and prints it whole; the iOS client
		reads `code` and `desc` and joins them itself. A `desc` that already
		carried the code would print `87 — 87 — Harvest` on the phone."""
		payload = bbch.picker(CHERRY)
		self.assertEqual(payload["desc_map"]["87"], "87 — Harvest")
		option = [item for group in payload["groups"] for item in group["options"] if item["code"] == "87"]
		self.assertEqual(option[0]["desc"], "Harvest")

	def test_an_unnamed_variety_is_not_offered(self):
		varieties = bbch.picker(CHERRY)["varieties"]
		self.assertEqual(varieties, [{"name": "Bing", "display": "Bing — the standard dark sweet"}])

	def test_perennial_survives_and_defaults_to_false(self):
		self.assertTrue(bbch.picker(CHERRY)["is_perennial"])
		self.assertFalse(bbch.picker()["is_perennial"])

	def test_a_scale_with_no_usable_stages_falls_back_rather_than_rendering_empty(self):
		"""An empty picker is indistinguishable to the user from a broken form,
		so a crop whose stage list is missing or malformed gets the generic
		scale — while keeping its own `is_perennial`."""
		for broken in ({}, {"stages": []}, {"stages": "not a list"}, {"stages": [1, 2, 3]}, None, "x"):
			payload = bbch.picker(broken)
			self.assertEqual(len(payload["groups"]), 10, repr(broken))
		self.assertTrue(bbch.picker({"is_perennial": True, "stages": []})["is_perennial"])

	def test_a_cycle_in_a_stored_scale_terminates_rather_than_hanging(self):
		"""A scale built with a shared dict nests forever. The walk stops at
		`MAX_DEPTH`, because a hung scheduler worker is worse than a truncated
		dropdown."""
		looping = {"code": "1", "desc": "1 — Leaf"}
		looping["children"] = {"11": looping}
		payload = bbch.picker({"stages": [looping]})
		self.assertLessEqual(
			max(option["depth"] for option in payload["groups"][0]["options"]), bbch.MAX_DEPTH
		)


class TwoQuestionsAboutOneCode(unittest.TestCase):
	def test_a_valid_code_this_crop_does_not_offer_is_valid_and_not_offered(self):
		verdict = bbch.validate("29", CHERRY)
		self.assertTrue(verdict["valid"])
		self.assertFalse(verdict["in_scale"])
		self.assertIn("does not offer", verdict["reason"])

	def test_a_code_the_crop_does_offer_is_both(self):
		verdict = bbch.validate("65", CHERRY)
		self.assertTrue(verdict["valid"])
		self.assertTrue(verdict["in_scale"])
		self.assertEqual(verdict["reason"], "")
		self.assertEqual(verdict["principal"], 6)

	def test_something_that_is_not_a_code_is_neither(self):
		verdict = bbch.validate("petal fall", CHERRY)
		self.assertFalse(verdict["valid"])
		self.assertFalse(verdict["in_scale"])
		self.assertEqual(verdict["label"], bbch.UNKNOWN_LABEL)

	def test_a_stage_the_crop_no_longer_offers_still_describes(self):
		"""A stage recorded before somebody trimmed the crop's scale is a
		historical fact, and a report of it that says "Unknown stage" has lost
		information the record still holds."""
		self.assertEqual(bbch.describe("29", CHERRY), "29 — End of tillering")

	def test_the_crops_own_words_beat_the_generic_ones(self):
		self.assertEqual(bbch.describe("87", CHERRY), "87 — Harvest")
		self.assertEqual(bbch.describe("87"), "87 — Hard dough")

	def test_codes_in_normalises_so_seven_and_oh_seven_are_one_entry(self):
		offered = bbch.codes_in(CHERRY)
		self.assertIn("06", offered)
		self.assertIn("65", offered)
		self.assertEqual(len(offered), len(set(offered)))
		self.assertEqual(list(offered), sorted(offered))


class TheWaterSchedule(unittest.TestCase):
	def schedule(self, **coefficients):
		return bbch.water_management(
			CHERRY, {"kc_by_stage": {"0": 0.4, "65": 1.15, "89": 0.8}, **coefficients}
		)

	def test_bloom_and_fruit_set_tighten_the_allowable_depletion(self):
		"""Stages 60-79 are where water stress costs fruit that cannot be got
		back, so MAD is capped at 0.35 whatever the crop-wide figure says."""
		stages = self.schedule(mad=0.5)["stages"]
		self.assertTrue(stages["65"]["critical"])
		self.assertEqual(stages["65"]["mad"], 0.35)
		self.assertIn("critical water demand", stages["65"]["notes"])

	def test_establishment_loosens_it_and_senescence_moderates_it(self):
		stages = self.schedule(mad=0.5)["stages"]
		self.assertEqual(stages["0"]["mad"], 0.7)
		self.assertEqual(stages["89"]["mad"], 0.6)
		self.assertFalse(stages["89"]["critical"])

	def test_a_tight_crop_wide_mad_is_never_loosened_past_itself(self):
		"""The bloom cap is a `min`, so a crop already irrigated at 0.25 keeps
		0.25 rather than being relaxed to the critical-stage figure."""
		self.assertEqual(self.schedule(mad=0.25)["stages"]["65"]["mad"], 0.25)

	def test_rooting_depth_grows_from_the_first_stage_to_the_last(self):
		stages = self.schedule(root_depth_mm=900)["stages"]
		depths = [stages[code]["root_depth_mm"] for code in ("0", "65", "89")]
		self.assertEqual(depths, sorted(depths))
		self.assertEqual(depths[-1], 900)
		self.assertLess(depths[0], 100)

	def test_a_crop_with_no_kc_gets_no_schedule_rather_than_an_invented_one(self):
		"""Without a Kc there is no per-stage schedule to derive, and inventing
		one produces a number an operator would irrigate on."""
		derived = bbch.water_management(CHERRY, {"mad": 0.4, "root_depth_mm": 800})
		self.assertEqual(derived["stages"], {})
		self.assertEqual(derived["defaults"], {"kc": 1.0, "mad": 0.4, "root_depth_mm": 800.0})

	def test_a_stored_mad_of_zero_is_an_operators_number_and_not_a_missing_one(self):
		"""`float(value or fallback)` would read 0 as 0.5 and irrigate on
		somebody else's figure."""
		self.assertEqual(bbch.water_management(CHERRY, {"mad": 0})["defaults"]["mad"], 0.0)

	def test_the_stage_notes_carry_the_crops_own_words(self):
		self.assertIn("Full flowering", self.schedule()["stages"]["65"]["notes"])

	def test_a_kc_key_that_is_not_a_stage_is_dropped_rather_than_sorted_as_zero(self):
		derived = bbch.water_management(CHERRY, {"kc_by_stage": {"65": 1.1, "harvest": 0.8}})
		self.assertEqual(sorted(derived["stages"]), ["65"])


if __name__ == "__main__":  # pragma: no cover
	unittest.main()

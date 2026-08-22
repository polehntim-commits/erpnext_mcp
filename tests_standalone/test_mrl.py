# SPDX-License-Identifier: MIT
"""Residue limits, and the IPM book behind the spray that produces them.

1. **A LIMIT WITH NO SOURCE IS REFUSED.** `TheSourceIsTheWholePoint`. A load is
   rejected at a border against a named regulation on a named date, and 'we had
   0.5 written down' is not a defence. A bare number is worse than nothing
   because it looks identical to a checked one.

2. **THE LANE LOOKUP NEVER GUESSES.** `TheLaneLookupDoesNotGuess`. A miss returns
   a miss. The negative control is the substance of this file: the same
   ingredient IS on file for another market, and the tool is asserted NOT to
   return that market's figure — it returns it as labelled research instead.

3. **ZERO IS A REAL LIMIT AND NOT A MISSING ONE.** `TheZeroLimit`. A non-detect
   requirement is the strictest limit there is, and treating it as absent
   converts it into no limit at all.

4. **A BAN IS NOT AN MRL.** `TheBanIsNotALimit`. A banned substance still carries
   a default figure, and the load is refused on the ban regardless of the residue
   found.

5. **THE IPM BOOK IS LITERATURE, NOT THIS FARM'S RECORDS.**
   `TheIPMReferenceIsLiterature`. It reads no doctype at all, says so in every
   result, and refuses to fuzzy-match names that must not be bridged.
"""

from .fixtures import V12TestCase, seed_masters

ALL_ON = {
	f"allow_{name}": 1
	for name in (
		"create_mrl_record",
		"get_mrl_record",
		"list_mrl_records",
		"update_mrl_record",
		"get_mrl_for_chemical_crop_market",
		"get_ipm_reference",
		"create_crop",
		"create_market",
	)
}

CHERRY = "Sweet Cherry"
JAPAN = "Japan Fresh"
KOREA = "Korea Fresh"


class MRLTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		seed_masters()
		self.configure(enabled=1, **ALL_ON)
		self.tool_data("create_crop", {"crop_name": CHERRY, "crop_type": "Stone Fruit"})
		for market in (JAPAN, KOREA):
			self.tool_data(
				"create_market",
				{"market_name": market, "market_type": "Export", "region": "Asia"},
			)

	def a_limit(self, **kw):
		payload = {
			"chemical": "spinetoram",
			"crop": CHERRY,
			"market": JAPAN,
			"mrl_ppm": 0.7,
			"source": "Japan Positive List, MAFF",
			"source_tier": "1",
		}
		payload.update(kw)
		return self.tool_data("create_mrl_record", payload)


class TheSourceIsTheWholePoint(MRLTestCase):
	def test_a_limit_is_recorded_with_its_provenance(self):
		created = self.a_limit()
		self.assertEqual(created["mrl_ppm"], 0.7)
		self.assertEqual(created["source"], "Japan Positive List, MAFF")
		self.assertTrue(created["official_source"])
		self.assertEqual(created["warnings"], [])

	def test_a_limit_with_no_source_is_refused(self):
		error = self.tool_error(
			"create_mrl_record",
			{"chemical": "spinetoram", "crop": CHERRY, "market": JAPAN, "mrl_ppm": 0.7},
		)
		self.assertIn("source", error.lower())

	def test_an_inferred_limit_is_kept_and_flagged(self):
		"""Tier 4 is worth having. It is not worth shipping on unconfirmed."""
		created = self.a_limit(source_tier="4", source="Harmonises with Codex CXL 0140")
		self.assertFalse(created["official_source"])
		self.assertTrue(any("inferred" in w for w in created["warnings"]))

	def test_a_blanket_default_says_nobody_has_set_a_limit(self):
		created = self.a_limit(is_default_mrl=True, mrl_ppm=0.01)
		self.assertTrue(any("nobody has set one" in w for w in created["warnings"]))

	def test_a_crop_group_match_is_reported_as_one_step_further_out(self):
		created = self.a_limit(crop_group_match=True)
		self.assertTrue(any("crop GROUP" in w for w in created["warnings"]))

	def test_a_stale_limit_is_named_by_the_register(self):
		fresh = self.a_limit(expiry_date="2099-01-01")
		stale = self.a_limit(market=KOREA, expiry_date="2020-01-01", source="Korea MFDS")

		listed = self.tool_data("list_mrl_records", {})
		self.assertEqual(listed["record_count"], 2)
		self.assertEqual(listed["needs_recheck"], [stale["name"]])
		self.assertNotIn(fresh["name"], listed["needs_recheck"])

	def test_a_second_limit_on_the_same_lane_is_refused(self):
		self.a_limit()
		error = self.tool_error(
			"create_mrl_record",
			{
				"chemical": "spinetoram",
				"crop": CHERRY,
				"market": JAPAN,
				"mrl_ppm": 0.5,
				"source": "Somewhere else",
			},
		)
		self.assertIn("spinetoram", error)

	def test_a_limit_for_a_market_that_does_not_exist_is_refused(self):
		error = self.tool_error(
			"create_mrl_record",
			{
				"chemical": "spinetoram",
				"crop": CHERRY,
				"market": "Atlantis",
				"mrl_ppm": 0.7,
				"source": "Nowhere",
			},
		)
		self.assertIn("Atlantis", error)

	def test_moving_the_number_without_the_source_is_reported_back(self):
		created = self.a_limit()
		changed = self.tool_data("update_mrl_record", {"mrl_record": created["name"], "mrl_ppm": 0.4})
		self.assertIn("cites a document", changed["note"])

	def test_moving_both_together_carries_no_such_note(self):
		"""The negative control: the note must not fire on every revision."""
		created = self.a_limit()
		changed = self.tool_data(
			"update_mrl_record",
			{
				"mrl_record": created["name"],
				"mrl_ppm": 0.4,
				"source": "Japan Positive List, MAFF, revised 2026-04",
			},
		)
		self.assertNotIn("note", changed)

	def test_a_recheck_date_before_the_effective_date_is_refused(self):
		error = self.tool_error(
			"create_mrl_record",
			{
				"chemical": "spinetoram",
				"crop": CHERRY,
				"market": JAPAN,
				"mrl_ppm": 0.7,
				"source": "MAFF",
				"effective_date": "2026-06-01",
				"expiry_date": "2026-01-01",
			},
		)
		self.assertIn("re-check", error.lower())


class TheZeroLimit(MRLTestCase):
	def test_zero_is_stored_and_read_back_as_zero(self):
		created = self.a_limit(mrl_ppm=0)
		self.assertEqual(created["mrl_ppm"], 0.0)
		read = self.tool_data("get_mrl_record", {"mrl_record": created["name"]})
		self.assertEqual(read["mrl_ppm"], 0.0)

	def test_the_lane_lookup_finds_a_zero_limit(self):
		"""The strictest limit there is must not read as no limit at all."""
		self.a_limit(mrl_ppm=0)
		answer = self.tool_data(
			"get_mrl_for_chemical_crop_market",
			{"chemical": "spinetoram", "crop": CHERRY, "market": JAPAN},
		)
		self.assertTrue(answer["found"])
		self.assertEqual(answer["mrl_ppm"], 0.0)

	def test_a_negative_limit_is_refused(self):
		error = self.tool_error(
			"create_mrl_record",
			{
				"chemical": "spinetoram",
				"crop": CHERRY,
				"market": JAPAN,
				"mrl_ppm": -1,
				"source": "MAFF",
			},
		)
		self.assertIn("negative", error.lower())


class TheBanIsNotALimit(MRLTestCase):
	def test_a_banned_substance_carries_a_figure_and_a_warning(self):
		created = self.a_limit(chemical="chlorpyrifos", substance_status="Banned", mrl_ppm=0.01)
		self.assertEqual(created["mrl_ppm"], 0.01)
		self.assertTrue(any("regardless of the residue" in w for w in created["warnings"]))

	def test_the_register_names_the_banned_substances(self):
		self.a_limit()
		banned = self.a_limit(chemical="chlorpyrifos", substance_status="Banned", mrl_ppm=0.01)
		listed = self.tool_data("list_mrl_records", {})
		self.assertEqual(listed["banned_substances"], [banned["name"]])

	def test_the_lane_lookup_carries_the_ban_warning_through(self):
		self.a_limit(chemical="chlorpyrifos", substance_status="Banned", mrl_ppm=0.01)
		answer = self.tool_data(
			"get_mrl_for_chemical_crop_market",
			{"chemical": "chlorpyrifos", "crop": CHERRY, "market": JAPAN},
		)
		self.assertTrue(any("not a permission" in w for w in answer["warnings"]))


class TheLaneLookupDoesNotGuess(MRLTestCase):
	def test_a_hit_answers_with_the_figure(self):
		self.a_limit()
		answer = self.tool_data(
			"get_mrl_for_chemical_crop_market",
			{"chemical": "spinetoram", "crop": CHERRY, "market": JAPAN},
		)
		self.assertTrue(answer["found"])
		self.assertEqual(answer["mrl_ppm"], 0.7)
		self.assertEqual(answer["source"], "Japan Positive List, MAFF")

	def test_a_miss_is_a_miss_even_when_another_market_has_a_figure(self):
		"""The claim this tool exists for.

		Japan has a limit for spinetoram. Korea does not. Asking about Korea must
		NOT return Japan's number, because the question is whether a load can
		ship into Korea.
		"""
		self.a_limit(market=JAPAN, mrl_ppm=0.7)
		answer = self.tool_data(
			"get_mrl_for_chemical_crop_market",
			{"chemical": "spinetoram", "crop": CHERRY, "market": KOREA},
		)
		self.assertFalse(answer["found"])
		self.assertIsNone(answer["mrl_ppm"])
		self.assertIn("does not fall back", answer["why"])

	def test_the_miss_returns_the_neighbouring_evidence_as_research(self):
		self.a_limit(market=JAPAN, mrl_ppm=0.7)
		self.a_limit(chemical="cyantraniliprole", market=KOREA, mrl_ppm=1.5, source="Korea MFDS")

		answer = self.tool_data(
			"get_mrl_for_chemical_crop_market",
			{"chemical": "spinetoram", "crop": CHERRY, "market": KOREA},
		)
		self.assertEqual(len(answer["same_chemical_other_markets"]), 1)
		self.assertEqual(answer["same_chemical_other_markets"][0]["market"], JAPAN)
		self.assertEqual(len(answer["same_market_other_chemicals"]), 1)
		self.assertEqual(answer["same_market_other_chemicals"][0]["chemical"], "cyantraniliprole")

	def test_the_miss_points_at_the_preserved_research_prompt(self):
		answer = self.tool_data(
			"get_mrl_for_chemical_crop_market",
			{"chemical": "spinetoram", "crop": CHERRY, "market": KOREA},
		)
		self.assertIn("mrl_research_single", answer["research_prompt"])

	def test_every_answer_carries_the_staleness_caveat(self):
		self.a_limit()
		answer = self.tool_data(
			"get_mrl_for_chemical_crop_market",
			{"chemical": "spinetoram", "crop": CHERRY, "market": JAPAN},
		)
		self.assertIn("live read of the regulator", answer["caveat"])


class TheIPMReferenceIsLiterature(MRLTestCase):
	def test_the_index_says_what_the_book_holds(self):
		book = self.tool_data("get_ipm_reference", {})
		self.assertFalse(book["is_site_data"])
		self.assertIn("pesticide_labels", book["tables_available"])
		self.assertGreater(book["row_counts"]["beneficial_toxicity"], 50)
		self.assertIn("only the index", book["next_step"])

	def test_a_pest_returns_its_degree_day_model(self):
		book = self.tool_data("get_ipm_reference", {"pest": "Spotted Wing Drosophila"})
		model = book["pest_model"]["emergence_logic"]
		self.assertEqual(model["model"], "degree_day")
		self.assertEqual(model["base_temp_c"], 7.2)

	def test_a_pest_returns_the_products_that_work_on_it_best_first(self):
		book = self.tool_data("get_ipm_reference", {"pest": "Spotted Wing Drosophila"})
		efficacies = [row["efficacy"] for row in book["effective_products"]]
		self.assertEqual(efficacies, sorted(efficacies, reverse=True))
		self.assertTrue(all(row["moa_group"] for row in book["effective_products"]))

	def test_a_product_returns_what_it_costs_the_beneficials(self):
		"""The half almost no label carries."""
		book = self.tool_data("get_ipm_reference", {"product": "Warrior II"})
		self.assertTrue(book["beneficial_toxicity"])
		worst = book["beneficial_toxicity"][0]
		self.assertEqual(worst["toxicity_category"], "lethal")
		self.assertIn("field_safe_days", worst)

	def test_a_product_returns_its_label_intervals(self):
		book = self.tool_data("get_ipm_reference", {"product": "Captan 80 WDG", "crop": "Cherries"})
		self.assertEqual(len(book["labels"]), 1)
		label = book["labels"][0]
		self.assertEqual(label["phi_days"], 0)
		self.assertEqual(label["rei_hours"], 24)

	def test_a_zero_phi_is_a_value_and_not_a_gap(self):
		"""Several protectants may be applied on the day of harvest."""
		book = self.tool_data("get_ipm_reference", {"product": "Captan 80 WDG", "crop": "Cherries"})
		self.assertIsNotNone(book["labels"][0]["phi_days"])
		self.assertEqual(book["labels"][0]["phi_days"], 0)

	def test_a_product_can_be_found_by_epa_registration_number(self):
		book = self.tool_data("get_ipm_reference", {"product": "66222-58"})
		self.assertEqual(book["product"]["product"], "Captan 80 WDG")

	def test_rotation_partners_exclude_the_same_mode_of_action_group(self):
		book = self.tool_data(
			"get_ipm_reference", {"product": "Warrior II", "pest": "Spotted Wing Drosophila"}
		)
		own = "3A"
		self.assertTrue(book["rotation_partners"])
		self.assertTrue(all(row["moa_group"] != own for row in book["rotation_partners"]))
		self.assertTrue(all(row["product"] != "Warrior II" for row in book["rotation_partners"]))

	def test_a_beneficial_returns_what_harms_it_worst_first(self):
		book = self.tool_data("get_ipm_reference", {"beneficial": "Ladybug"})
		self.assertTrue(book["harmed_by"])
		scores = [row["toxicity_score"] for row in book["harmed_by"]]
		self.assertEqual(scores, sorted(scores, reverse=True))

	def test_matching_is_case_insensitive(self):
		book = self.tool_data("get_ipm_reference", {"pest": "spotted wing drosophila"})
		self.assertTrue(book["pest_model"])

	def test_an_unknown_pest_returns_a_miss_and_says_why_there_is_no_fuzzy_match(self):
		book = self.tool_data("get_ipm_reference", {"pest": "Codling Moths"})
		self.assertIsNone(book["pest_model"])
		self.assertIn("no fuzzy matching", book["pest_miss"])

	def test_an_unknown_table_is_refused_with_the_list(self):
		error = self.tool_error("get_ipm_reference", {"table": "nonsense"})
		self.assertIn("pest_models", error)

	def test_a_whole_table_can_be_read(self):
		book = self.tool_data("get_ipm_reference", {"table": "pest_damage"})
		self.assertEqual(len(book["rows"]), book["row_counts"]["pest_damage"])
		self.assertIn("vulnerable_bbch_start", book["rows"][0])

	def test_every_answer_carries_the_label_caveat_and_its_sources(self):
		book = self.tool_data("get_ipm_reference", {"pest": "Brown Rot"})
		self.assertIn("label in the applicator's hand governs", book["caveat"])
		self.assertTrue(any("UC IPM" in source for source in book["sources"]))

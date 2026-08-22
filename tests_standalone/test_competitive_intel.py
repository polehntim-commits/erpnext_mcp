# SPDX-License-Identifier: MIT
"""Who else is in this market, what they did, and what it would take to buy them.

1. **THE FOUR FIT SCORES FAIL SEPARATELY AND THE MEAN HIDES IT.**
   `TheFourScoresAreSeparate`. A target scoring 0.9/0.9/0.9 on strategy, finance
   and synergy and 0.1 on cultural fit has a respectable composite and is a deal
   that closes and then does not work. `weakest_dimension` is the answer, and
   `accretive_score` is refused as an argument so it cannot drift from its inputs.

2. **AN UNSCORED TARGET SCORES NOTHING, NOT ZERO.** `TheUnscoredTarget`. Zero is
   an answer; a target nobody has assessed must not sort beside one assessed as
   worthless.

3. **THE PIPELINE COUNT THAT GETS QUOTED IS THE WRONG ONE BY DEFAULT.**
   `ThePipelineIsMostlySettled`. Forty targets of which thirty-five are Passed is
   a pipeline of five.

4. **THE GAP BETWEEN RECOMMENDED AND ACTUAL IS THE POINT OF THE MOVE REGISTER.**
   `TheUnansweredMoves`. It is invisible move by move and is the most
   instructive thing in the file.

5. **A FUTURE OBSERVATION IS NOT AN OBSERVATION.** `TheMoveRegisterIsEvidence`.
   Speculation in this register stops it answering whether a pattern is real.
"""

from .fixtures import MAIN, V12TestCase, seed_masters

ALL_ON = {
	f"allow_{name}": 1
	for name in (
		"create_market_participant",
		"get_market_participant",
		"list_market_participants",
		"update_market_participant",
		"create_competitive_move",
		"get_competitive_move",
		"list_competitive_moves",
		"update_competitive_move",
		"create_acquisition_target",
		"get_acquisition_target",
		"list_acquisition_targets",
		"update_acquisition_target",
	)
}


class CompetitiveIntelTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		seed_masters()
		self.configure(enabled=1, **ALL_ON)

	def a_participant(self, **kw):
		payload = {
			"company": MAIN,
			"participant_name": "Cascade Orchards",
			"participant_type": "Competitor",
			"geography": "The Dalles",
			"market_position": "Challenger",
		}
		payload.update(kw)
		return self.tool_data("create_market_participant", payload)

	def a_move(self, participant, **kw):
		payload = {
			"company": MAIN,
			"market_participant": participant,
			"move_type": "Price Change",
			"description": "Dropped their July field-run price by 12%.",
			"observed_date": "2026-07-14",
		}
		payload.update(kw)
		return self.tool_data("create_competitive_move", payload)

	def a_target(self, **kw):
		payload = {"company": MAIN, "entity_name": "Wilson Family Orchard"}
		payload.update(kw)
		return self.tool_data("create_acquisition_target", payload)


class TheParticipantRegister(CompetitiveIntelTestCase):
	def test_a_participant_is_recorded_with_its_estimates_labelled(self):
		created = self.a_participant(estimated_acreage=340.0, estimated_revenue=2_800_000)
		self.assertEqual(created["participant_type"], "Competitor")
		self.assertEqual(created["estimated_acreage"], 340.0)
		self.assertIn("read of a private business", created["caveat"])

	def test_the_multi_line_fields_come_back_as_lists(self):
		created = self.a_participant(
			strengths="Own packing line\nLong buyer relationships",
			vulnerability_windows="Succession, owner is 71\nNote matures 2027",
		)
		self.assertEqual(created["strengths"], ["Own packing line", "Long buyer relationships"])
		self.assertEqual(len(created["vulnerability_windows"]), 2)

	def test_the_same_organisation_twice_under_one_company_is_refused(self):
		self.a_participant()
		error = self.tool_error(
			"create_market_participant",
			{
				"company": MAIN,
				"participant_name": "Cascade Orchards",
				"participant_type": "Supplier",
			},
		)
		self.assertIn("Cascade Orchards", error)

	def test_a_market_share_over_one_hundred_is_refused(self):
		error = self.tool_error(
			"create_market_participant",
			{
				"company": MAIN,
				"participant_name": "Impossible Co",
				"participant_type": "Competitor",
				"market_share_pct": 140,
			},
		)
		self.assertIn("0 to 100", error)

	def test_the_register_names_the_unassessed(self):
		self.a_participant()
		self.a_participant(participant_name="Riverbend Farms", strengths="Cheap labour")
		listed = self.tool_data("list_market_participants", {"company": MAIN})
		self.assertEqual(listed["participant_count"], 2)
		self.assertEqual(len(listed["without_assessment"]), 1)

	def test_the_register_filters_by_type_and_geography(self):
		self.a_participant()
		self.a_participant(participant_name="Valley Packing", participant_type="Partner", geography="Yakima")
		by_type = self.tool_data("list_market_participants", {"company": MAIN, "participant_type": "Partner"})
		self.assertEqual(by_type["participant_count"], 1)
		by_place = self.tool_data("list_market_participants", {"company": MAIN, "geography": "Yakima"})
		self.assertEqual(by_place["participant_count"], 1)

	def test_an_update_reports_what_changed(self):
		created = self.a_participant()
		changed = self.tool_data(
			"update_market_participant",
			{"market_participant": created["name"], "market_position": "Leader"},
		)
		self.assertEqual(changed["market_position"], "Leader")
		self.assertEqual(changed["changed"]["market_position"], ["Challenger", "Leader"])

	def test_an_update_with_nothing_in_it_is_refused(self):
		created = self.a_participant()
		error = self.tool_error("update_market_participant", {"market_participant": created["name"]})
		self.assertIn("nothing to change", error)


class TheMoveRegisterIsEvidence(CompetitiveIntelTestCase):
	def test_a_move_is_recorded_against_a_participant(self):
		participant = self.a_participant()
		move = self.a_move(participant["name"])
		self.assertEqual(move["move_type"], "Price Change")
		self.assertEqual(move["observed_date"], "2026-07-14")
		self.assertIn("Nothing has been done", move["next_step"])

	def test_a_move_against_nobody_is_refused(self):
		error = self.tool_error(
			"create_competitive_move",
			{
				"company": MAIN,
				"market_participant": "MKTP-99999",
				"move_type": "Expansion",
				"description": "Bought the Miller place.",
			},
		)
		self.assertIn("MKTP-99999", error)

	def test_an_observation_dated_in_the_future_is_refused(self):
		participant = self.a_participant()
		error = self.tool_error(
			"create_competitive_move",
			{
				"company": MAIN,
				"market_participant": participant["name"],
				"move_type": "Expansion",
				"description": "Will buy the Miller place.",
				"observed_date": "2099-01-01",
			},
		)
		self.assertIn("future", error.lower())

	def test_a_response_date_with_no_response_is_refused(self):
		participant = self.a_participant()
		move = self.a_move(participant["name"])
		error = self.tool_error(
			"update_competitive_move",
			{"competitive_move": move["name"], "response_date": "2026-07-20"},
		)
		self.assertIn("recommended", error.lower())

	def test_a_response_before_the_observation_is_refused(self):
		participant = self.a_participant()
		move = self.a_move(participant["name"])
		error = self.tool_error(
			"update_competitive_move",
			{
				"competitive_move": move["name"],
				"actual_response": "Held price.",
				"response_date": "2026-07-01",
			},
		)
		self.assertIn("before", error.lower())

	def test_low_confidence_moves_are_reported_rather_than_filtered(self):
		"""Three low-confidence sightings of one thing are themselves evidence."""
		participant = self.a_participant()
		self.a_move(participant["name"], confidence="Low", source="grower meeting")
		self.a_move(participant["name"], observed_date="2026-07-15", confidence="High")

		listed = self.tool_data("list_competitive_moves", {"company": MAIN})
		self.assertEqual(listed["move_count"], 2)
		self.assertEqual(len(listed["low_confidence"]), 1)


class TheUnansweredMoves(CompetitiveIntelTestCase):
	def test_the_register_names_the_unanswered_and_the_urgent_ones_separately(self):
		participant = self.a_participant()
		answered = self.a_move(participant["name"])
		self.tool_data(
			"update_competitive_move",
			{
				"competitive_move": answered["name"],
				"actual_response": "Held our price and told the buyers why.",
				"response_date": "2026-07-20",
			},
		)
		urgent = self.a_move(participant["name"], observed_date="2026-07-20", response_urgency="Urgent")
		self.a_move(participant["name"], observed_date="2026-07-21", response_urgency="Monitor")

		listed = self.tool_data("list_competitive_moves", {"company": MAIN})
		self.assertEqual(listed["move_count"], 3)
		self.assertEqual(len(listed["unanswered"]), 2)
		self.assertEqual(listed["urgent_unanswered"], [urgent["name"]])

	def test_the_participant_read_carries_its_own_unanswered_list(self):
		participant = self.a_participant()
		self.a_move(participant["name"])
		read = self.tool_data("get_market_participant", {"market_participant": participant["name"]})
		self.assertEqual(read["move_count"], 1)
		self.assertEqual(len(read["moves_without_response"]), 1)

	def test_the_unanswered_filter_selects_both_ways(self):
		participant = self.a_participant()
		answered = self.a_move(participant["name"])
		self.tool_data(
			"update_competitive_move",
			{
				"competitive_move": answered["name"],
				"actual_response": "Held price.",
				"response_date": "2026-07-20",
			},
		)
		self.a_move(participant["name"], observed_date="2026-07-22")

		open_ones = self.tool_data("list_competitive_moves", {"company": MAIN, "unanswered": True})
		self.assertEqual(open_ones["move_count"], 1)
		closed = self.tool_data("list_competitive_moves", {"company": MAIN, "unanswered": False})
		self.assertEqual(closed["move_count"], 1)


class TheFourScoresAreSeparate(CompetitiveIntelTestCase):
	def test_the_composite_is_the_mean_of_what_was_scored(self):
		target = self.a_target(
			strategic_fit_score=0.9,
			financial_health_score=0.9,
			synergy_score=0.9,
			cultural_fit_score=0.1,
		)
		self.assertEqual(target["accretive_score"], 0.7)

	def test_the_weakest_dimension_is_named_beside_the_mean(self):
		"""The claim: a deal fails on its weakest score, not on its average."""
		target = self.a_target(
			strategic_fit_score=0.9,
			financial_health_score=0.9,
			synergy_score=0.9,
			cultural_fit_score=0.1,
		)
		self.assertEqual(target["weakest_dimension"], "cultural_fit_score")

		read = self.tool_data("get_acquisition_target", {"acquisition_target": target["name"]})
		self.assertTrue(any("cultural_fit_score" in note for note in read["notes_on_scoring"]))

	def test_the_composite_cannot_be_set_by_hand(self):
		target = self.a_target(strategic_fit_score=0.5)
		error = self.tool_error(
			"update_acquisition_target",
			{"acquisition_target": target["name"], "accretive_score": 0.99},
		)
		self.assertIn("accretive_score", error)
		self.assertIn("Nothing was changed", error)

	def test_the_composite_follows_its_inputs_on_every_save(self):
		target = self.a_target(strategic_fit_score=0.4, financial_health_score=0.6)
		self.assertEqual(target["accretive_score"], 0.5)
		changed = self.tool_data(
			"update_acquisition_target",
			{"acquisition_target": target["name"], "financial_health_score": 1.0},
		)
		self.assertEqual(changed["accretive_score"], 0.7)

	def test_a_fit_score_of_exactly_zero_is_stored(self):
		"""Zero is a judgement, not a blank, and the two must not collapse.

		This is the regression test for a staging helper that compared
		`str(before or "")` — under which `0 or ""` is `""`, so a score of zero
		over an empty column staged nothing and the write was silently lost. A
		cultural fit of zero is exactly the score somebody most needs recorded.
		"""
		target = self.a_target(cultural_fit_score=0, strategic_fit_score=0.8)
		self.assertEqual(target["cultural_fit_score"], 0.0)
		self.assertEqual(target["scores_recorded"], 2)
		self.assertEqual(target["accretive_score"], 0.4)
		self.assertEqual(target["weakest_dimension"], "cultural_fit_score")

	def test_a_score_updated_down_to_zero_is_stored(self):
		target = self.a_target(cultural_fit_score=0.7)
		changed = self.tool_data(
			"update_acquisition_target",
			{"acquisition_target": target["name"], "cultural_fit_score": 0},
		)
		self.assertEqual(changed["cultural_fit_score"], 0.0)
		self.assertEqual(changed["changed"]["cultural_fit_score"], [0.7, 0.0])

	def test_a_score_outside_zero_to_one_is_refused(self):
		error = self.tool_error(
			"create_acquisition_target",
			{"company": MAIN, "entity_name": "Percent Farm", "synergy_score": 80},
		)
		self.assertIn("0 to 1", error)

	def test_the_asset_breakdown_is_totalled_against_the_going_concern(self):
		target = self.a_target(
			estimated_value=3_000_000,
			land_value_appreciation=2_000_000,
			water_rights_value=1_400_000,
			infrastructure_value=250_000,
		)
		self.assertEqual(target["asset_value_total"], 3_650_000)
		read = self.tool_data("get_acquisition_target", {"acquisition_target": target["name"]})
		self.assertTrue(any("for the ground" in note for note in read["notes_on_scoring"]))

	def test_closing_without_a_close_date_is_refused(self):
		target = self.a_target(strategic_fit_score=0.8)
		error = self.tool_error(
			"update_acquisition_target",
			{"acquisition_target": target["name"], "status": "Closed"},
		)
		self.assertIn("close date", error.lower())

	def test_a_close_before_the_identification_is_refused(self):
		error = self.tool_error(
			"create_acquisition_target",
			{
				"company": MAIN,
				"entity_name": "Backwards Farm",
				"identified_date": "2026-06-01",
				"target_close_date": "2026-01-01",
			},
		)
		self.assertIn("before", error.lower())


class TheUnscoredTarget(CompetitiveIntelTestCase):
	def test_an_unscored_target_has_no_composite_rather_than_a_zero(self):
		target = self.a_target()
		self.assertIsNone(target["accretive_score"])
		self.assertEqual(target["scores_recorded"], 0)
		self.assertIsNone(target["weakest_dimension"])

	def test_a_partially_scored_target_says_which_are_missing(self):
		target = self.a_target(strategic_fit_score=0.8, synergy_score=0.6)
		self.assertEqual(target["scores_recorded"], 2)
		self.assertIn("cultural_fit_score", target["next_step"])
		self.assertIn("Cultural fit is the one most often skipped", target["next_step"])

	def test_the_register_separates_unscored_from_partially_scored(self):
		self.a_target()
		self.a_target(entity_name="Half Scored", strategic_fit_score=0.5)
		self.a_target(
			entity_name="Fully Scored",
			strategic_fit_score=0.5,
			financial_health_score=0.5,
			synergy_score=0.5,
			cultural_fit_score=0.5,
		)
		listed = self.tool_data("list_acquisition_targets", {"company": MAIN})
		self.assertEqual(listed["target_count"], 3)
		self.assertEqual(len(listed["unscored"]), 1)
		self.assertEqual(len(listed["partially_scored"]), 1)


class ThePipelineIsMostlySettled(CompetitiveIntelTestCase):
	def test_the_live_count_is_reported_separately_from_the_total(self):
		self.a_target(entity_name="Live One", status="Evaluating", acreage=80.0)
		self.a_target(entity_name="Passed One", status="Passed")
		self.a_target(entity_name="Closed One", status="Closed", actual_close_date="2026-03-01")

		listed = self.tool_data("list_acquisition_targets", {"company": MAIN})
		self.assertEqual(listed["target_count"], 3)
		self.assertEqual(listed["live_count"], 1)
		self.assertEqual(listed["live_acreage"], 80.0)

	def test_a_passed_target_is_kept_rather_than_deleted(self):
		"""Why a deal was passed on is the useful thing four years later."""
		target = self.a_target(
			entity_name="Passed One", status="Passed", rationale="Water rights did not transfer."
		)
		read = self.tool_data("get_acquisition_target", {"acquisition_target": target["name"]})
		self.assertEqual(read["status"], "Passed")
		self.assertIn("Water rights", read["rationale"])

	def test_the_pipeline_is_ordered_best_scored_first(self):
		self.a_target(entity_name="Weak", strategic_fit_score=0.2)
		self.a_target(entity_name="Strong", strategic_fit_score=0.95)
		listed = self.tool_data("list_acquisition_targets", {"company": MAIN})
		self.assertEqual(listed["targets"][0]["entity_name"], "Strong")

	def test_a_target_links_back_to_its_participant(self):
		participant = self.a_participant()
		target = self.a_target(market_participant=participant["name"])
		read = self.tool_data("get_acquisition_target", {"acquisition_target": target["name"]})
		self.assertEqual(read["participant_detail"]["participant_name"], "Cascade Orchards")

		from_participant = self.tool_data(
			"get_market_participant", {"market_participant": participant["name"]}
		)
		self.assertEqual(len(from_participant["acquisition_targets"]), 1)

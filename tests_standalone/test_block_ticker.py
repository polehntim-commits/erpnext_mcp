# SPDX-License-Identifier: MIT
"""The buyer's name for a block, and the promise that it means one block.

1. **THE TICKER IS UNIQUE PER COMPANY AND NOT PER PARCEL.**
   `TheTickerIsAPromise`. Two blocks under one company answering to 'YC-3' means
   a buyer quoting it on an order gets whichever one a query happened to return.
   This is the claim the field exists for.

2. **CASE IS FOLDED BEFORE THE UNIQUENESS CHECK, NOT AFTER.**
   `TheTickerIsFolded`. 'yc-3' and 'YC-3' reaching the check as different strings
   would let both exist, and the duplicate would be found by a buyer receiving
   the wrong block's fruit. The negative control is here: the same pair asserted
   to be refused, so a regression that stops folding fails this file rather than
   passing it quietly.

3. **EMPTY IS THE NORMAL STATE AND NEVER COLLIDES.** `TheUntickeredBlock`. Most
   blocks are never sold by name. If '' were treated as a value, the first
   untickered block would lock out every other one.

4. **A SEASON KEEPS THE TICKER THE BLOCK CARRIED THAT YEAR.**
   `TheSeasonRemembersWhatItWasCalled`. Re-tickering a block in a later season
   must not relabel the earlier one — the settlements that quoted the old code
   would stop agreeing with the season record they were settled against.
"""

from .fixtures import MAIN, V12TestCase, seed_masters
from .harness import frappe

CHERRY = "Cherry"

ALL_ON = {
	f"allow_{name}": 1
	for name in (
		"create_parcel",
		"create_field",
		"get_field",
		"list_fields",
		"update_field",
		"create_planting_season",
		"get_planting_season",
	)
}


class BlockTickerTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		seed_masters()
		self.configure(enabled=1, **ALL_ON)
		self.tool_data(
			"create_parcel",
			{
				"owning_entity": MAIN,
				"parcel_name": "Mill Creek",
				"acreage": 131.43,
				"county": "Wasco",
				"state": "OR",
				"use_type": "Orchard",
			},
		)

	def a_block(self, name, **kw):
		payload = {
			"parcel": "Mill Creek",
			"field_name": name,
			"acreage": 12.5,
			"crop": CHERRY,
			"variety": "Bing",
			"planting_year": 2021,
		}
		payload.update(kw)
		return self.tool_data("create_field", payload)


class TheTickerIsAPromise(BlockTickerTestCase):
	def test_a_ticker_is_stored_and_reported(self):
		created = self.a_block("Yellow Camp Block 3", block_ticker="YC-3")
		self.assertEqual(created["block_ticker"], "YC-3")

		read = self.tool_data("get_field", {"field": created["name"]})
		self.assertEqual(read["block_ticker"], "YC-3")

	def test_the_ticker_is_not_the_block_number(self):
		"""Both are reported, because they answer different questions."""
		created = self.a_block("Yellow Camp Block 3", block_number="3A", block_ticker="YC-3")
		self.assertEqual(created["block_number"], "3A")
		self.assertEqual(created["block_ticker"], "YC-3")

	def test_a_second_block_cannot_claim_the_same_ticker(self):
		self.a_block("Yellow Camp Block 3", block_ticker="YC-3")
		error = self.tool_error(
			"create_field",
			{
				"parcel": "Mill Creek",
				"field_name": "Yellow Camp Block 4",
				"acreage": 12.5,
				"block_ticker": "YC-3",
			},
		)
		self.assertIn("YC-3", error)

	def test_the_collision_is_refused_across_parcels_not_only_within_one(self):
		"""The whole point: a buyer says the ticker without knowing the parcel."""
		self.tool_data(
			"create_parcel",
			{
				"owning_entity": MAIN,
				"parcel_name": "Skyline",
				"acreage": 60.0,
				"county": "Wasco",
				"state": "OR",
				"use_type": "Orchard",
			},
		)
		self.a_block("Yellow Camp Block 3", block_ticker="YC-3")
		error = self.tool_error(
			"create_field",
			{"parcel": "Skyline", "field_name": "Skyline Block 1", "block_ticker": "YC-3"},
		)
		self.assertIn("YC-3", error)

	def test_a_ticker_longer_than_ten_characters_is_refused(self):
		error = self.tool_error(
			"create_field",
			{
				"parcel": "Mill Creek",
				"field_name": "Yellow Camp Block 3",
				"block_ticker": "YELLOWCAMP-3-NORTH",
			},
		)
		self.assertIn("characters", error.lower())

	def test_update_field_can_set_and_clear_a_ticker(self):
		created = self.a_block("Yellow Camp Block 3")
		self.assertIsNone(created["block_ticker"])

		changed = self.tool_data("update_field", {"field": created["name"], "block_ticker": "YC-3"})
		self.assertEqual(changed["block_ticker"], "YC-3")
		self.assertIn("block_ticker", changed["changed"])

		cleared = self.tool_data("update_field", {"field": created["name"], "block_ticker": ""})
		self.assertIsNone(cleared["block_ticker"])

	def test_update_field_refuses_a_ticker_another_block_holds(self):
		self.a_block("Yellow Camp Block 3", block_ticker="YC-3")
		other = self.a_block("Yellow Camp Block 4")
		error = self.tool_error("update_field", {"field": other["name"], "block_ticker": "YC-3"})
		self.assertIn("YC-3", error)

	def test_a_block_keeps_its_own_ticker_on_an_unrelated_edit(self):
		"""The uniqueness check must not refuse a row against itself."""
		created = self.a_block("Yellow Camp Block 3", block_ticker="YC-3")
		changed = self.tool_data("update_field", {"field": created["name"], "acreage": 13.0})
		self.assertEqual(changed["block_ticker"], "YC-3")


class TheTickerIsFolded(BlockTickerTestCase):
	def test_a_lowercase_ticker_is_stored_upper(self):
		created = self.a_block("Yellow Camp Block 3", block_ticker="yc-3")
		self.assertEqual(created["block_ticker"], "YC-3")

	def test_the_negative_control_a_case_variant_still_collides(self):
		"""If folding ever stops happening, this is the test that goes red.

		Written as its own case rather than as an assertion inside the folding
		test, because "it stored YC-3" and "a second block cannot claim yc-3" are
		different claims and only the second one is the reason folding matters.
		"""
		self.a_block("Yellow Camp Block 3", block_ticker="YC-3")
		error = self.tool_error(
			"create_field",
			{"parcel": "Mill Creek", "field_name": "Yellow Camp Block 4", "block_ticker": "yc-3"},
		)
		self.assertIn("YC-3", error)

	def test_surrounding_whitespace_does_not_make_a_new_ticker(self):
		self.a_block("Yellow Camp Block 3", block_ticker="YC-3")
		error = self.tool_error(
			"create_field",
			{"parcel": "Mill Creek", "field_name": "Yellow Camp Block 4", "block_ticker": "  YC-3 "},
		)
		self.assertIn("YC-3", error)

	def test_a_buyer_quoting_lower_case_finds_the_block(self):
		self.a_block("Yellow Camp Block 3", block_ticker="YC-3")
		found = self.tool_data("list_fields", {"owning_entity": MAIN, "block_ticker": "yc-3"})
		self.assertEqual(found["field_count"], 1)
		self.assertEqual(found["fields"][0]["block_ticker"], "YC-3")


class TheUntickeredBlock(BlockTickerTestCase):
	def test_two_untickered_blocks_coexist(self):
		"""An empty ticker is not a value, so it cannot be claimed."""
		self.a_block("Yellow Camp Block 3")
		self.a_block("Yellow Camp Block 4")
		listed = self.tool_data("list_fields", {"owning_entity": MAIN})
		self.assertEqual(listed["field_count"], 2)
		self.assertEqual([row["block_ticker"] for row in listed["fields"]], [None, None])

	def test_the_register_reports_a_null_rather_than_an_empty_string(self):
		created = self.a_block("Yellow Camp Block 3")
		read = self.tool_data("get_field", {"field": created["name"]})
		self.assertIsNone(read["block_ticker"])


class TheSeasonRemembersWhatItWasCalled(BlockTickerTestCase):
	def a_season(self, field, season_year=2026):
		return self.tool_data(
			"create_planting_season",
			{
				"field": field,
				"crop": CHERRY,
				"variety": "Bing",
				"plant_year": 2021,
				"season_year": season_year,
				"acres": 12.5,
				"company": MAIN,
			},
		)

	def test_a_season_takes_the_ticker_from_its_block(self):
		block = self.a_block("Yellow Camp Block 3", block_ticker="YC-3")
		season = self.a_season(block["name"])
		stored = frappe.db.get_value("Planting Season", season["name"], "block_ticker")
		self.assertEqual(stored, "YC-3")

	def test_re_tickering_the_block_does_not_relabel_a_closed_season(self):
		"""The reason this is a copy and not a fetch_from.

		A settlement that quoted YC-3 in 2026 must still agree with the 2026
		season record after somebody re-letters the block in 2027.
		"""
		block = self.a_block("Yellow Camp Block 3", block_ticker="YC-3")
		season = self.a_season(block["name"])

		self.tool_data("update_field", {"field": block["name"], "block_ticker": "MC-11"})

		self.assertEqual(frappe.db.get_value("Planting Season", season["name"], "block_ticker"), "YC-3")
		self.assertEqual(self.tool_data("get_field", {"field": block["name"]})["block_ticker"], "MC-11")

	def test_a_season_on_an_untickered_block_carries_none(self):
		block = self.a_block("Yellow Camp Block 4")
		season = self.a_season(block["name"])
		self.assertFalse(frappe.db.get_value("Planting Season", season["name"], "block_ticker"))

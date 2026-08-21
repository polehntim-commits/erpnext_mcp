# SPDX-License-Identifier: MIT
"""The operational map — what is true of a block right now. v0.116.0.

Cycle 5, Precision Ag Map Phase 3. `farm_overview` has drawn SHAPE since
v0.110.0; five registers already held every operational fact about that shape
and not one of them was reachable from the map. Each class below is one claim
about the join that closes it, ordered by how much damage getting it wrong does.

1. **AN UNMEASURED BLOCK MUST NEVER WEAR A MEASURED BLOCK'S COLOUR.**
   `UnknownIsNeverGreen`. This is the failure the whole surface exists to avoid,
   and it is the comfortable one: a zone with no valve history, a block with no
   scouting round and a crop with no Brix target all have an obvious wrong
   answer that looks like the map working. Every one of them comes back
   `unknown`, in grey, with the reason — and grey is asserted as NOT green in
   hex, because that is the bug a reader would never see in a screenshot.

2. **A LIVE RESTRICTION OUTRANKS EVERY SOIL CONSIDERATION.**
   `AccessIsOrderedNotAveraged`. Driving a sprayer into a treated block is an
   entry under 40 CFR §170.407 and the operator is a person. A composite that
   averaged its three inputs would let a very dry block outvote a federal
   restriction, which is why the verdict is an ORDER and the answer names which
   input decided it.

3. **THE COMPACTION LAYER IS NOT THE RESTRICTED-ENTRY LAYER.**
   `TwoQuestionsTwoLayers`. One is about a machine on wet ground, the other is
   about a person in a treated block. The negative control is that they are
   never merged: a block can be green on one and red on the other at the same
   moment, and the answer has to say both.

4. **NEITHER NUMBER ALONE CALLS A PICK.** `TheStageAndTheSugar`. The Crop
   Observation doctype's own description states the rule — Brix rises while the
   stage stands still in a hot week and the stage advances while Brix stalls in
   a wet one — so `pick_now` needs both, and `short_of` names which half is
   missing across all four ways it can be.

5. **THE YELLOW BAND CANNOT BE EMPTY.** `TheSoilBookRefusesTheSilentTypo`. A
   yellow figure under the red one leaves no caution band at all: every wet
   block goes straight from red to green when the red hours pass and the
   drying-out warning is never drawn. Nothing anywhere reports an error, which
   is what makes it worth a controller.

6. **THE HOURS COME FROM THE SOIL, AND THE ANSWER SAYS WHICH SOIL.**
   `TheSoilDecidesTheHours`. The same elapsed hours colour differently on sand
   and on clay; a block naming no profile falls back and reports `default`; one
   naming a deleted or a retired profile falls back and NAMES it, because a
   colour that changed across a farm on the day somebody unticked a row has to
   be traceable to that row.

7. **THE ROLE FILTER IS A DISPLAY FILTER AND RESTRICTED ENTRY IS NEVER IN IT.**
   `TheRoleFilterKeepsTheSafetyLayer`. A field worker gets the one layer that
   keeps them out of a treated block; a foreman gets all five; an account with
   none of this app's roles is not filtered at all, because a picker always
   carries one and a role-less login is the MCP system user or a Desk session.

8. **IT WRITES NOTHING.** `TheMapWritesNothing`. The negative control. Reading
   the map does not change a stored record of any kind.

9. **THE PAGE, THE TOOL AND THE PHONE READ ONE ENGINE.** `OneEngineThreeDoors`.
   Three surfaces drawing the same colours from three implementations is three
   chances to disagree about whether a block is safe.
"""

import json

import frappe

from erpnext_mcp import agronomy_seed, farm_overview, overlays
from erpnext_mcp.erpnext_mcp.doctype.soil_compaction_profile import soil_compaction_profile as profile_doc

from .fixtures import MAIN, V12TestCase
from .harness import STORE, set_roles

ON = {
	"allow_create_parcel": 1,
	"allow_create_field": 1,
	"allow_create_irrigation_zone": 1,
	"allow_register_asset": 1,
	"allow_get_map_overlays": 1,
	"allow_list_soil_compaction_profiles": 1,
	"allow_create_soil_compaction_profile": 1,
	"allow_update_soil_compaction_profile": 1,
	"allow_assign_soil_profile": 1,
}

#: The harness clock. `frappe.utils.now()` in the double is 2026-07-24 09:00:00
#: plus one second per call, so every "hours ago" below is counted back from
#: nine in the morning on that day and is a figure a reader can check by hand.
NOW_DAY = "2026-07-24"
NINE_AM = "2026-07-24 09:00:00"

BLOCK = "Yellow Camp Block 3 - MC"
ZONE = "YC3-Zone2 - MC"
VALVE = "MC-Valve-05"

GREY = overlays.PALETTE[overlays.UNKNOWN]
GREEN = overlays.PALETTE["green"]


class MapTestCase(V12TestCase):
	"""A parcel, a block, a zone on it, and the soil book seeded.

	THE SOIL BOOK IS SEEDED EXPLICITLY rather than left to the installer, the
	same call `test_agronomy.py` makes about its units: these tests are about
	RESOLUTION, and a fixture that depended on seed order would fail for a reason
	that had nothing to do with what it was checking.
	"""

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		agronomy_seed.seed_soil_profiles()

	# ── fixture builders ────────────────────────────────────────────────────
	def a_farm(self, crop="Cherry", variety="Skeena"):
		parcel = self.tool_data(
			"create_parcel",
			{
				"owning_entity": MAIN,
				"parcel_name": "Mill Creek",
				"acreage": 131.43,
				"county": "Wasco",
				"state": "OR",
			},
		)["name"]
		block = self.tool_data(
			"create_field",
			{
				"parcel": parcel,
				"field_name": "Yellow Camp Block 3",
				"acreage": 25.7,
				"crop": crop,
				"variety": variety,
			},
		)["name"]
		zone = self.tool_data(
			"create_irrigation_zone",
			{
				"field": block,
				"zone_name": "YC3-Zone2",
				"zone_number": 2,
				"water_source": "well",
				"area_sq_ft": 40000,
			},
		)["name"]
		self.parcel = parcel
		return parcel, block, zone

	def a_valve(self, zone, name=VALVE):
		return self.tool_data(
			"register_asset",
			{"name": name, "asset_type": "Irrigation Valve", "company": MAIN, "irrigation_zone": zone},
		)

	def valve_event(self, asset, action, when):
		"""One Asset State Log row at a timestamp this test chose.

		Written straight to the table, exactly as `test_irrigation_runtime.py`
		does, because these tests are about how the overlay READS the log and a
		fixture that went through `log_asset_state_change` would be testing the
		cascade as well.
		"""
		rows = STORE.tables.setdefault("Asset State Log", {})
		name = f"ASL-{len(rows) + 1:04d}"
		rows[name] = {
			"name": name,
			"docstatus": 0,
			"asset_name": asset,
			"asset_type": "Irrigation Valve",
			"action": action,
			"from_state": "closed" if action == "open_valve" else "open",
			"to_state": "open" if action == "open_valve" else "closed",
			"performed_by": "Administrator",
			"performed_at": when,
			"creation": when,
		}
		return name

	def an_observation(self, block, **overrides):
		rows = STORE.tables.setdefault("Crop Observation", {})
		name = f"OBS-{len(rows) + 1:04d}"
		rows[name] = {
			"name": name,
			"docstatus": 0,
			"company": MAIN,
			"block_doctype": "Field",
			"block": block,
			"observation_type": "Harvest Readiness",
			"observed_on": NOW_DAY,
			"observed_at": NINE_AM,
			"observer": "Administrator",
			"crop": "Cherry",
			"growth_stage_code": "",
			"brix_reading": None,
			"brix_method": "",
			"creation": NINE_AM,
			**overrides,
		}
		return name

	def a_restriction(self, block, hours_remaining=3.0, product="SURROUND-WP"):
		"""One live Spray REI row, expiring `hours_remaining` from nine o'clock."""
		rows = STORE.tables.setdefault("Spray REI", {})
		name = f"REI-{len(rows) + 1:04d}"
		rows[name] = {
			"name": name,
			"docstatus": 0,
			"status": "Active",
			"block_doctype": "Field",
			"block": block,
			"company": MAIN,
			"product": product,
			"product_name": product,
			"rei_hours": 24,
			"all_products": json.dumps([{"item_code": product, "rei_hours": 24}]),
			"started_at": frappe.utils.add_to_date(NINE_AM, hours=hours_remaining - 24),
			"expires_at": frappe.utils.add_to_date(NINE_AM, hours=hours_remaining),
			"creation": NINE_AM,
		}
		return name

	def a_crop(self, target_brix=19.0, variety="Skeena", variety_brix=None):
		"""A Crop with a pick target, and optionally a per-variety override."""
		# THE VARIETY OVERLAY LIVES INSIDE ITS PARENT in the double, exactly as a
		# child table does on a bench — so it is seeded on the Crop rather than
		# written to a table of its own, and re-seeding replaces the whole row
		# rather than leaving two variety lines for one variety.
		STORE.seed(
			"Crop",
			[
				{
					"name": "Cherry",
					"crop_name": "Cherry",
					"crop_type": "Stone Fruit",
					"target_brix": target_brix,
					"varieties": [{"variety_name": variety, "target_brix": variety_brix}],
				}
			],
		)

	def a_picker(self, login="picker@example.test", *held):
		"""One enrolled worker, with their roles where Frappe keeps them.

		ON THE USER'S OWN `roles` CHILD TABLE and not in a table of Has Role
		rows, which is what `roles.roles_of` reads through and what
		`test_role_indicator.a_user` already established.
		"""
		held = held or ("Field Worker",)
		STORE.seed(
			"User",
			[{"name": login, "enabled": 1, "full_name": login, "roles": [{"role": name} for name in held]}],
		)
		set_roles(login, list(held))
		frappe.local.session = frappe._dict(user=login, data=frappe._dict())
		return login

	# ── convenience ─────────────────────────────────────────────────────────
	def overlays_for(self, **arguments):
		return self.tool_data("get_map_overlays", {"company": MAIN, **arguments})

	def block_layer(self, answer, layer, name=BLOCK):
		row = next(entry for entry in answer["blocks"] if entry["name"] == name)
		return row[layer]

	def zone_layer(self, answer, name=ZONE):
		return next(entry for entry in answer["zones"] if entry["zone"] == name)


# ── 1 ───────────────────────────────────────────────────────────────────────
class UnknownIsNeverGreen(MapTestCase):
	"""The claim the rest of the file is built on. See the module docstring."""

	def test_a_zone_with_no_valve_tagged_to_it_is_unknown_and_says_why(self):
		_, _, zone = self.a_farm()
		state = self.zone_layer(self.overlays_for(layers=["irrigation"]))
		self.assertEqual(state["status"], "unknown")
		self.assertIn("No valve in the Asset Register names this zone", state["reason"])
		self.assertEqual(state["zone"], zone)

	def test_a_zone_whose_valves_have_never_been_logged_is_a_different_sentence(self):
		"""Two ways to have no answer, two jobs. One is in the asset register and
		one is in the orchard, and a single "no data" line would send somebody to
		the wrong one."""
		_, _, zone = self.a_farm()
		self.a_valve(zone)
		state = self.zone_layer(self.overlays_for(layers=["irrigation"]))
		self.assertEqual(state["status"], "unknown")
		self.assertIn("none has ever been opened or closed", state["reason"])
		self.assertEqual(state["valves"], 1)
		self.assertEqual(state["valves_with_history"], 0)

	def test_unknown_is_grey_in_hex_and_is_not_green(self):
		"""The assertion a screenshot would never catch."""
		self.a_farm()
		state = self.zone_layer(self.overlays_for(layers=["irrigation"]))
		self.assertEqual(state["colour"], GREY)
		self.assertNotEqual(state["colour"], GREEN)

	def test_a_block_with_no_observation_is_unknown_rather_than_not_ready(self):
		self.a_farm()
		state = self.block_layer(self.overlays_for(layers=["harvest"]), "harvest")
		self.assertEqual(state["status"], "unknown")
		self.assertFalse(state["ready"])
		self.assertEqual(state["colour"], GREY)
		self.assertIn("No Crop Observation on this block", state["reason"])

	def test_a_crop_with_no_brix_target_does_not_make_a_ripe_block(self):
		"""A fabricated target would turn every block on the farm the same colour
		on one day, which is the failure mode that looks most like working."""
		self.a_farm()
		self.a_crop(target_brix=None, variety_brix=None)
		self.an_observation(BLOCK, growth_stage_code="88", brix_reading=21.0, brix_method="Refractometer")
		state = self.block_layer(self.overlays_for(layers=["harvest"]), "harvest")
		self.assertFalse(state["ready"])
		self.assertEqual(state["target_source"], "none")
		self.assertEqual(state["short_of"], "brix_target")

	def test_a_block_with_no_zone_rolls_up_to_unknown_not_green(self):
		parcel = self.tool_data(
			"create_parcel",
			{
				"owning_entity": MAIN,
				"parcel_name": "Mill Creek",
				"acreage": 40,
				"county": "Wasco",
				"state": "OR",
			},
		)["name"]
		block = self.tool_data(
			"create_field", {"parcel": parcel, "field_name": "Ridge Top", "acreage": 12.5}
		)["name"]
		answer = self.overlays_for(layers=["irrigation"])
		state = self.block_layer(answer, "irrigation", block)
		self.assertEqual(state["status"], "unknown")
		self.assertEqual(state["zones"], 0)
		self.assertIn("No irrigation zone on this site names this block", state["reason"])


# ── 2 ───────────────────────────────────────────────────────────────────────
class AccessIsOrderedNotAveraged(MapTestCase):
	"""A composite that averaged its inputs would let dry ground outvote a
	federal restriction. See `overlays.access_overlay`."""

	def test_a_live_restriction_blocks_a_block_whose_ground_is_bone_dry(self):
		_, block, zone = self.a_farm()
		self.a_valve(zone)
		# Water off a fortnight ago — green on every soil in the book.
		self.valve_event(VALVE, "open_valve", "2026-07-09 06:00:00")
		self.valve_event(VALVE, "close_valve", "2026-07-09 09:00:00")
		self.a_restriction(block, hours_remaining=3.0)

		answer = self.overlays_for(layers=["irrigation", "spray_rei", "equipment_access"])
		self.assertEqual(self.zone_layer(answer)["status"], "green")
		access = self.block_layer(answer, "equipment_access")
		self.assertEqual(access["status"], "blocked")
		self.assertEqual(access["decided_by"], "spray_rei")
		self.assertEqual(access["water_status"], "green")

	def test_wet_ground_with_no_restriction_is_caution_and_not_a_refusal(self):
		"""Whether a pass is worth a rut is the foreman's judgement, and the
		answer hands them the zone that made it their problem."""
		_, _, zone = self.a_farm()
		self.a_valve(zone)
		self.valve_event(VALVE, "open_valve", "2026-07-24 03:00:00")
		self.valve_event(VALVE, "close_valve", "2026-07-24 05:00:00")

		answer = self.overlays_for(layers=["irrigation", "equipment_access"])
		access = self.block_layer(answer, "equipment_access")
		self.assertEqual(access["status"], "caution")
		self.assertEqual(access["decided_by"], "irrigation")
		self.assertEqual(access["driving_zone"], ZONE)

	def test_an_unmeasured_block_is_caution_and_never_open(self):
		self.a_farm()
		access = self.block_layer(self.overlays_for(layers=["equipment_access"]), "equipment_access")
		self.assertEqual(access["status"], "caution")
		self.assertEqual(access["water_status"], "unknown")

	def test_soil_moisture_is_named_as_missing_rather_than_weighted_at_zero(self):
		"""An `open` verdict here means "nothing we can measure is against it",
		not "we checked everything", and the answer has to be able to say so."""
		_, _, zone = self.a_farm()
		self.a_valve(zone)
		self.valve_event(VALVE, "open_valve", "2026-07-09 06:00:00")
		self.valve_event(VALVE, "close_valve", "2026-07-09 09:00:00")
		access = self.block_layer(self.overlays_for(layers=["equipment_access"]), "equipment_access")
		self.assertEqual(access["status"], "open")
		self.assertEqual(access["inputs_missing"], ["soil_moisture"])

	def test_the_wettest_zone_on_a_block_is_the_block_s_answer(self):
		"""A machine has to cross the wet quarter. Averaging the hours would
		produce a green block with a bog in the middle of it."""
		_, block, zone = self.a_farm()
		other = self.tool_data(
			"create_irrigation_zone",
			{"field": block, "zone_name": "YC3-Zone3", "zone_number": 3, "water_source": "well"},
		)["name"]
		self.a_valve(zone, name="MC-Valve-05")
		self.a_valve(other, name="MC-Valve-06")
		# Zone 2 dry a fortnight, zone 3 shut two hours ago.
		self.valve_event("MC-Valve-05", "open_valve", "2026-07-09 06:00:00")
		self.valve_event("MC-Valve-05", "close_valve", "2026-07-09 09:00:00")
		self.valve_event("MC-Valve-06", "open_valve", "2026-07-24 05:00:00")
		self.valve_event("MC-Valve-06", "close_valve", "2026-07-24 07:00:00")

		answer = self.overlays_for(layers=["irrigation"])
		rolled = self.block_layer(answer, "irrigation")
		self.assertEqual(rolled["status"], "red")
		self.assertEqual(rolled["driving_zone"], other)
		self.assertEqual(rolled["zones"], 2)


# ── 3 ───────────────────────────────────────────────────────────────────────
class TwoQuestionsTwoLayers(MapTestCase):
	"""Compaction is about a machine on wet ground; restricted entry is about a
	person in a treated block. The negative control is that they never merge."""

	def test_a_block_can_be_green_on_one_layer_and_red_on_the_other(self):
		_, block, zone = self.a_farm()
		self.a_valve(zone)
		self.valve_event(VALVE, "open_valve", "2026-07-09 06:00:00")
		self.valve_event(VALVE, "close_valve", "2026-07-09 09:00:00")
		self.a_restriction(block, hours_remaining=6.0)

		answer = self.overlays_for(layers=["irrigation", "spray_rei"])
		self.assertEqual(self.block_layer(answer, "irrigation")["status"], "green")
		self.assertEqual(self.block_layer(answer, "spray_rei")["status"], "red")

	def test_the_restricted_entry_sentence_is_the_registers_own_words(self):
		"""A worker who reads one wording at a gate and another on a work order
		has been given two rules — `spray_rei.warning_line` is emphatic, and this
		is one more screen it is read off rather than a second author of it."""
		_, block, _ = self.a_farm()
		self.a_restriction(block, hours_remaining=3.0, product="SURROUND-WP")
		state = self.block_layer(self.overlays_for(layers=["spray_rei"]), "spray_rei")
		self.assertTrue(state["restricted"])
		self.assertIn("do not enter without PPE", state["warning"])
		self.assertEqual(state["product"], "SURROUND-WP")

	def test_the_longest_live_restriction_is_the_countdown_that_is_printed(self):
		"""Reporting the first to expire would clear a block hours early with a
		number that looked precise."""
		_, block, _ = self.a_farm()
		self.a_restriction(block, hours_remaining=2.0, product="SHORT-ONE")
		self.a_restriction(block, hours_remaining=9.0, product="LONG-ONE")
		state = self.block_layer(self.overlays_for(layers=["spray_rei"]), "spray_rei")
		self.assertEqual(state["windows"], 2)
		self.assertEqual(state["product"], "LONG-ONE")
		self.assertGreater(state["hours_remaining"], 8.0)

	def test_a_pre_harvest_window_does_not_restrict_entry(self):
		"""The two spray layers are two rules with two subjects. A block inside a
		PHI may be walked into and worked; it may not be PICKED."""
		_, block, _ = self.a_farm()
		# `blocks` GOES ON THE PARENT. Frappe forbids a standalone child row and
		# the double keeps them inside their parents, so a Spray Application
		# Block written to a table of its own is a row `_blocks_by_application`
		# cannot see — and the PHI layer would read as clear on a block that is
		# not, which is the direction this test exists to catch.
		STORE.seed(
			"Spray Application",
			[
				{
					"name": "SPRAY-0001",
					"docstatus": 0,
					"status": "Applied",
					"company": MAIN,
					"phi_clears_on": "2026-07-30",
					"phi_days": 14,
					"phi_source_item": "SURROUND-WP",
					"completed_at": "2026-07-16 10:00:00",
					"blocks": [{"block_doctype": "Field", "block": block, "acres": 25.7}],
				}
			],
		)
		answer = self.overlays_for(layers=["spray_rei", "spray_phi"])
		self.assertFalse(self.block_layer(answer, "spray_rei")["restricted"])
		phi = self.block_layer(answer, "spray_phi")
		self.assertTrue(phi["restricted"])
		self.assertEqual(phi["clears_on"], "2026-07-30")
		self.assertEqual(phi["product"], "SURROUND-WP")


# ── 4 ───────────────────────────────────────────────────────────────────────
class TheStageAndTheSugar(MapTestCase):
	"""`Crop Observation.brix_reading`'s own description states this rule: either
	number alone will call a pick wrong."""

	def setUp(self):
		super().setUp()
		self.a_farm()
		self.a_crop(target_brix=19.0)

	def test_both_say_ready_and_only_then_is_it_pick_now(self):
		self.an_observation(BLOCK, growth_stage_code="88", brix_reading=19.4, brix_method="Refractometer")
		state = self.block_layer(self.overlays_for(layers=["harvest"]), "harvest")
		self.assertEqual(state["status"], overlays.READY_NOW)
		self.assertTrue(state["ready"])
		self.assertIsNone(state["short_of"])

	def test_the_stage_is_ripe_and_the_sugar_is_short(self):
		self.an_observation(BLOCK, growth_stage_code="88", brix_reading=16.4, brix_method="Refractometer")
		state = self.block_layer(self.overlays_for(layers=["harvest"]), "harvest")
		self.assertFalse(state["ready"])
		self.assertEqual(state["short_of"], "brix")
		self.assertEqual(state["status"], overlays.STAGE_NEAR)

	def test_the_stage_is_ripe_and_nobody_took_a_brix(self):
		"""Not a promotion to pick_now. The figure a buyer's specification quotes
		is exactly the one nobody can tell apart afterwards."""
		self.an_observation(BLOCK, growth_stage_code="87")
		state = self.block_layer(self.overlays_for(layers=["harvest"]), "harvest")
		self.assertFalse(state["ready"])
		self.assertTrue(state["brix_missing"])
		self.assertEqual(state["short_of"], "brix_reading")

	def test_the_sugar_is_there_and_the_fruit_is_not(self):
		self.an_observation(BLOCK, growth_stage_code="83", brix_reading=20.1, brix_method="Refractometer")
		state = self.block_layer(self.overlays_for(layers=["harvest"]), "harvest")
		self.assertFalse(state["ready"])
		self.assertEqual(state["short_of"], "stage")

	def test_a_variety_target_beats_its_crops_and_says_so(self):
		"""The v0.114.0 overlay shape: sparse, per field, with provenance. A
		caller handed 22.0 cannot otherwise tell a variety's considered figure
		from its crop's default."""
		self.a_crop(target_brix=19.0, variety="Skeena", variety_brix=22.0)
		self.an_observation(BLOCK, growth_stage_code="88", brix_reading=20.5, brix_method="Refractometer")
		state = self.block_layer(self.overlays_for(layers=["harvest"]), "harvest")
		self.assertEqual(state["target"], 22.0)
		self.assertEqual(state["target_source"], "variety")
		self.assertFalse(state["ready"])

	def test_a_fortnight_old_round_is_reported_and_flagged_rather_than_dropped(self):
		"""Dropping it would draw an unscouted block over one scouted a fortnight
		ago — opposite problems with the same picture."""
		self.an_observation(
			BLOCK,
			observed_on="2026-07-08",
			observed_at="2026-07-08 07:00:00",
			growth_stage_code="88",
			brix_reading=19.9,
			brix_method="Refractometer",
		)
		state = self.block_layer(self.overlays_for(layers=["harvest"]), "harvest")
		self.assertTrue(state["stale"])
		self.assertEqual(state["observed_days_ago"], 16)
		self.assertEqual(state["brix"], 19.9)

	def test_the_latest_round_wins_when_a_block_is_walked_twice(self):
		self.an_observation(
			BLOCK,
			observed_on="2026-07-20",
			growth_stage_code="83",
			brix_reading=15.0,
			brix_method="Refractometer",
		)
		self.an_observation(
			BLOCK,
			observed_on="2026-07-23",
			growth_stage_code="88",
			brix_reading=19.6,
			brix_method="Refractometer",
		)
		state = self.block_layer(self.overlays_for(layers=["harvest"]), "harvest")
		self.assertEqual(state["observed_on"], "2026-07-23")
		self.assertTrue(state["ready"])

	def test_the_bbch_bands_split_stage_eight_rather_than_lumping_it(self):
		"""81 is the very beginning of colouring and 89 is fully ripe. A map that
		drew those alike would have a fortnight of the season in one band."""
		self.assertEqual(overlays.bbch_band("81")[0], overlays.STAGE_COLOURING)
		self.assertEqual(overlays.bbch_band("85")[0], overlays.STAGE_NEAR)
		self.assertEqual(overlays.bbch_band("87")[0], overlays.STAGE_READY)
		self.assertEqual(overlays.bbch_band("65")[0], overlays.STAGE_VEGETATIVE)
		self.assertEqual(overlays.bbch_band("92")[0], overlays.STAGE_POST_HARVEST)

	def test_a_stage_column_that_is_not_a_code_is_unknown_and_not_an_error(self):
		"""`growth_stage_code` is a Data column on purpose — the farm types what
		the farm keeps — and refusing the whole observation over it would throw
		away the Brix that parsed."""
		self.assertEqual(overlays.bbch_band("petal fall")[0], overlays.UNKNOWN)
		self.assertEqual(overlays.bbch_band("")[0], overlays.UNKNOWN)
		self.assertEqual(overlays.bbch_band("BBCH 87")[0], overlays.STAGE_READY)

		self.an_observation(BLOCK, growth_stage_code="petal fall", brix_reading=19.9, brix_method="Estimate")
		state = self.block_layer(self.overlays_for(layers=["harvest"]), "harvest")
		self.assertEqual(state["stage"], overlays.UNKNOWN)
		self.assertEqual(state["brix"], 19.9)


# ── 5 ───────────────────────────────────────────────────────────────────────
class TheSoilBookRefusesTheSilentTypo(MapTestCase):
	"""A yellow at or under the red leaves NO caution band. Nothing reports an
	error; the first symptom is a rutted block."""

	def test_a_yellow_under_the_red_is_refused_with_the_consequence_named(self):
		message = self.tool_error(
			"create_soil_compaction_profile",
			{"soil_type": "Made Ground", "red_hours": 40, "yellow_hours": 20},
		)
		self.assertIn("no caution band", message)
		self.assertFalse(frappe.db.exists("Soil Compaction Profile", "Made Ground"))

	def test_a_yellow_equal_to_the_red_is_refused_too(self):
		self.tool_error(
			"create_soil_compaction_profile",
			{"soil_type": "Made Ground", "red_hours": 24, "yellow_hours": 24},
		)

	def test_zero_hours_is_refused_because_a_blank_float_arrives_as_zero(self):
		message = self.tool_error(
			"create_soil_compaction_profile",
			{"soil_type": "Made Ground", "red_hours": 0, "yellow_hours": 48},
		)
		self.assertIn("never too wet to drive on", message)

	def test_an_update_that_omits_an_hour_figure_leaves_it_alone(self):
		"""Not passed and passed as zero are different, and collapsing them would
		take a working profile down on an update meaning to change the notes."""
		self.tool_data("update_soil_compaction_profile", {"soil_type": "Loam", "notes": "our own trial"})
		row = frappe.db.get_value(
			"Soil Compaction Profile", "Loam", ["red_hours", "yellow_hours", "notes"], as_dict=True
		)
		self.assertEqual(float(row["red_hours"]), 24.0)
		self.assertEqual(float(row["yellow_hours"]), 48.0)
		self.assertEqual(row["notes"], "our own trial")

	def test_an_update_reports_how_many_blocks_it_recolours(self):
		"""A typo discovered by a tractor is an expensive way to learn that."""
		self.a_farm()
		self.tool_data("assign_soil_profile", {"field": BLOCK, "soil_profile": "Loam"})
		data = self.tool_data(
			"update_soil_compaction_profile", {"soil_type": "Loam", "red_hours": 30, "yellow_hours": 70}
		)
		self.assertEqual(data["blocks_recoloured"], 1)
		self.assertIn(BLOCK, data["block_names"])

	def test_the_band_boundaries_are_closed_on_the_lower_side(self):
		""" "Is 24.0 hours still red" is a question somebody at a gate asks out
		loud, so it is answered here rather than discovered."""
		self.assertEqual(profile_doc.band(23.9, 24, 48), "red")
		self.assertEqual(profile_doc.band(24.0, 24, 48), "yellow")
		self.assertEqual(profile_doc.band(47.9, 24, 48), "yellow")
		self.assertEqual(profile_doc.band(48.0, 24, 48), "green")
		self.assertEqual(profile_doc.band(None, 24, 48), "unknown")


# ── 6 ───────────────────────────────────────────────────────────────────────
class TheSoilDecidesTheHours(MapTestCase):
	"""The same elapsed hours colour differently on sand and on clay, and every
	answer says which figures it used and where they came from."""

	def a_zone_watered_hours_ago(self, hours):
		_, block, zone = self.a_farm()
		self.a_valve(zone)
		self.valve_event(VALVE, "open_valve", frappe.utils.add_to_date(NINE_AM, hours=-(hours + 2)))
		self.valve_event(VALVE, "close_valve", frappe.utils.add_to_date(NINE_AM, hours=-hours))
		return block

	def test_twenty_hours_is_green_on_sand_and_red_on_clay(self):
		"""Sand is green past 16 hours and clay is red until 60. The SPREAD is
		the whole reason the figures are a record: one hard-coded twenty-four
		would keep machinery off dry sand for a day and send it onto wet clay."""
		block = self.a_zone_watered_hours_ago(20)

		self.tool_data("assign_soil_profile", {"field": block, "soil_profile": "Sand"})
		self.assertEqual(self.zone_layer(self.overlays_for(layers=["irrigation"]))["status"], "green")

		self.tool_data("assign_soil_profile", {"field": block, "soil_profile": "Clay", "clear": False})
		state = self.zone_layer(self.overlays_for(layers=["irrigation"]))
		self.assertEqual(state["status"], "red")
		self.assertEqual(state["soil_profile"], "Clay")
		self.assertEqual(state["thresholds_source"], "profile")

	def test_a_block_with_no_profile_falls_back_and_says_default(self):
		self.a_zone_watered_hours_ago(12)
		state = self.zone_layer(self.overlays_for(layers=["irrigation"]))
		self.assertEqual(state["thresholds_source"], "default")
		self.assertIsNone(state["soil_profile"])
		self.assertEqual(state["red_hours"], overlays.DEFAULT_RED_HOURS)
		self.assertEqual(state["status"], "red")

	def test_a_profile_that_was_deleted_falls_back_and_names_it(self):
		"""A Link whose target is gone is a data fix, not a colour choice."""
		block = self.a_zone_watered_hours_ago(12)
		self.tool_data("assign_soil_profile", {"field": block, "soil_profile": "Sand"})
		STORE.tables["Soil Compaction Profile"].pop("Sand")
		state = self.zone_layer(self.overlays_for(layers=["irrigation"]))
		self.assertEqual(state["thresholds_source"], "missing")
		self.assertEqual(state["soil_profile"], "Sand")
		self.assertEqual(state["red_hours"], overlays.DEFAULT_RED_HOURS)

	def test_a_profile_somebody_retired_falls_back_and_names_it(self):
		"""So a colour that changed across a farm on the day a row was unticked
		can be traced to that row."""
		block = self.a_zone_watered_hours_ago(12)
		self.tool_data("assign_soil_profile", {"field": block, "soil_profile": "Sand"})
		self.tool_data("update_soil_compaction_profile", {"soil_type": "Sand", "enabled": False})
		state = self.zone_layer(self.overlays_for(layers=["irrigation"]))
		self.assertEqual(state["thresholds_source"], "disabled")
		self.assertEqual(state["soil_profile"], "Sand")

	def test_assigning_a_retired_profile_is_refused_rather_than_warned(self):
		"""It would leave the block on the default while its own form claimed a
		measurement — the worst of both."""
		_, block, _ = self.a_farm()
		self.tool_data("update_soil_compaction_profile", {"soil_type": "Sand", "enabled": False})
		message = self.tool_error("assign_soil_profile", {"field": block, "soil_profile": "Sand"})
		self.assertIn("is disabled", message)
		self.assertFalse(frappe.db.get_value("Field", block, "soil_profile"))

	def test_an_open_valve_is_its_own_state_and_names_the_valve_to_shut(self):
		"""Water on the ground right now is not "very red"; it is a state with an
		action attached."""
		_, _, zone = self.a_farm()
		self.a_valve(zone)
		self.valve_event(VALVE, "open_valve", "2026-07-24 06:00:00")
		state = self.zone_layer(self.overlays_for(layers=["irrigation"]))
		self.assertEqual(state["status"], "irrigating")
		self.assertEqual(state["open_valves"], [VALVE])
		self.assertEqual(state["hours_since_water_off"], 0.0)
		self.assertNotEqual(state["colour"], overlays.PALETTE["red"])

	def test_the_listing_reports_how_many_blocks_are_still_on_the_default(self):
		"""The number that says whether any of this is wired up at all."""
		self.a_farm()
		before = self.tool_data("list_soil_compaction_profiles", {})
		self.assertEqual(before["blocks_without_profile"], 1)
		self.tool_data("assign_soil_profile", {"field": BLOCK, "soil_profile": "Loam"})
		after = self.tool_data("list_soil_compaction_profiles", {})
		self.assertEqual(after["blocks_without_profile"], 0)
		loam = next(row for row in after["profiles"] if row["name"] == "Loam")
		self.assertEqual(loam["blocks"], 1)

	def test_clear_puts_a_block_back_on_the_default_deliberately(self):
		self.a_farm()
		self.tool_data("assign_soil_profile", {"field": BLOCK, "soil_profile": "Loam"})
		data = self.tool_data("assign_soil_profile", {"field": BLOCK, "clear": True})
		self.assertIsNone(data["soil_profile"])
		self.assertEqual(data["previous_soil_profile"], "Loam")
		self.assertEqual(data["thresholds_source"], "default")

	def test_the_seeded_book_says_it_is_a_shipped_default(self):
		"""Every seeded figure has to be able to answer "where did this come
		from" honestly, so the ones nobody has reviewed stay visible."""
		rows = self.tool_data("list_soil_compaction_profiles", {})["profiles"]
		self.assertEqual(len(rows), len(agronomy_seed.SEED_SOIL_PROFILES))
		for row in rows:
			with self.subTest(soil=row["name"]):
				self.assertIn("shipped default", row["source"])
				self.assertGreater(row["yellow_hours"], row["red_hours"])

	def test_the_seeder_never_overwrites_an_edited_figure(self):
		self.tool_data(
			"update_soil_compaction_profile",
			{"soil_type": "Loam", "red_hours": 33, "source": "our own 2024 wheel-rut trial"},
		)
		agronomy_seed.seed_soil_profiles()
		row = frappe.db.get_value("Soil Compaction Profile", "Loam", ["red_hours", "source"], as_dict=True)
		self.assertEqual(float(row["red_hours"]), 33.0)
		self.assertEqual(row["source"], "our own 2024 wheel-rut trial")


# ── 7 ───────────────────────────────────────────────────────────────────────
class TheRoleFilterKeepsTheSafetyLayer(MapTestCase):
	"""It is a display filter and never a gate — `frappe.has_permission` is the
	gate — but the one entry that is a SAFETY rule cannot be configured away."""

	def test_a_field_worker_gets_restricted_entry_and_nothing_else(self):
		self.assertEqual(overlays.ROLE_LAYERS["Field Worker"], overlays.ALWAYS)
		self.assertEqual(set(overlays.ALWAYS), {overlays.LAYER_REI})

	def test_every_role_in_the_table_includes_restricted_entry(self):
		"""The hardest rule in the file, asserted over the whole table rather than
		on the one role somebody thought of."""
		for role, layers in overlays.ROLE_LAYERS.items():
			with self.subTest(role=role):
				self.assertIn(overlays.LAYER_REI, layers)

	def test_a_foreman_and_a_farm_manager_get_all_five(self):
		self.assertEqual(set(overlays.ROLE_LAYERS["Foreman"]), set(overlays.LAYER_KEYS))
		self.assertEqual(set(overlays.ROLE_LAYERS["Farm Manager"]), set(overlays.LAYER_KEYS))

	def test_a_compliance_officer_gets_the_regulated_windows_and_nothing_operational(self):
		layers = set(overlays.ROLE_LAYERS["Compliance Officer"])
		self.assertEqual(layers, {overlays.LAYER_REI, overlays.LAYER_PHI})
		self.assertNotIn(overlays.LAYER_IRRIGATION, layers)

	def test_a_worker_is_narrowed_and_told_what_was_held_back(self):
		self.a_farm()
		self.a_picker()
		shown = overlays.layers_for("picker@example.test")
		self.assertEqual(shown["visible"], [overlays.LAYER_REI])
		self.assertFalse(shown["unfiltered"])
		self.assertEqual(
			{entry["key"] for entry in shown["withheld"]},
			set(overlays.LAYER_KEYS) - {overlays.LAYER_REI},
		)

	def test_an_account_with_no_farm_role_is_not_filtered_at_all(self):
		"""A picker HAS the Field Worker role — `create_mobile_user` grants one —
		so a login with none is the MCP system user or a Desk session, and
		narrowing it to one layer would hide four from the operator's own
		console."""
		shown = overlays.layers_for("Administrator")
		self.assertEqual(shown["visible"], list(overlays.LAYER_KEYS))
		self.assertTrue(shown["unfiltered"])
		self.assertEqual(shown["farm_roles_held"], [])

	def test_a_layer_the_roles_do_not_show_is_refused_by_name(self):
		"""Silently dropping it would draw an empty legend and read as a farm with
		no observations on it."""
		keep, refused = overlays.requested_layers(["harvest"], [overlays.LAYER_REI])
		self.assertEqual(keep, [])
		self.assertEqual(refused[0]["key"], "harvest")
		self.assertIn("roles this login holds", refused[0]["reason"])

	def test_a_layer_that_is_not_a_layer_names_the_five_that_are(self):
		keep, refused = overlays.requested_layers(["frost"], list(overlays.LAYER_KEYS))
		self.assertEqual(keep, [])
		self.assertIn("not a layer", refused[0]["reason"])

	def test_a_layer_not_asked_for_is_not_computed(self):
		"""A picker's phone asking for restricted entry should pay for one query
		and not for the observation register."""
		self.a_farm()
		self.an_observation(BLOCK, growth_stage_code="88", brix_reading=20.0, brix_method="Refractometer")
		answer = self.overlays_for(layers=["spray_rei"])
		block = next(entry for entry in answer["blocks"] if entry["name"] == BLOCK)
		self.assertIn("spray_rei", block)
		self.assertNotIn("harvest", block)
		self.assertNotIn("irrigation", block)
		self.assertEqual(answer["zones"], [])


# ── 8 ───────────────────────────────────────────────────────────────────────
class TheMapWritesNothing(MapTestCase):
	"""The negative control. Every layer is a read over a register somebody else
	owns, and the sweep `spray_rei.active_rows` runs is that register's own."""

	def test_reading_every_layer_changes_no_stored_record(self):
		_, block, zone = self.a_farm()
		self.a_valve(zone)
		self.valve_event(VALVE, "open_valve", "2026-07-24 03:00:00")
		self.valve_event(VALVE, "close_valve", "2026-07-24 05:00:00")
		self.a_restriction(block, hours_remaining=4.0)
		self.a_crop()
		self.an_observation(BLOCK, growth_stage_code="88", brix_reading=19.9, brix_method="Refractometer")

		before = {doctype: len(rows) for doctype, rows in STORE.tables.items()}
		self.overlays_for()
		self.overlays_for(layers=["harvest", "equipment_access"])
		after = {doctype: len(rows) for doctype, rows in STORE.tables.items()}
		after.pop("MCP Action Log", None)
		before.pop("MCP Action Log", None)
		self.assertEqual(before, after)

	def test_the_tool_is_declared_read_only(self):
		from erpnext_mcp import registry

		self.assertIn("get_map_overlays", registry.READ_TOOLS)
		self.assertIn("list_soil_compaction_profiles", registry.READ_TOOLS)
		for name in (
			"create_soil_compaction_profile",
			"update_soil_compaction_profile",
			"assign_soil_profile",
		):
			with self.subTest(tool=name):
				self.assertIn(name, registry.MUTATING_TOOLS)


# ── 9 ───────────────────────────────────────────────────────────────────────
class OneEngineThreeDoors(MapTestCase):
	"""Three surfaces drawing the same colours from three implementations is
	three chances to disagree about whether a block is safe."""

	def test_the_desk_page_returns_the_same_layer_the_tool_does(self):
		_, _, zone = self.a_farm()
		self.a_valve(zone)
		self.valve_event(VALVE, "open_valve", "2026-07-24 03:00:00")
		self.valve_event(VALVE, "close_valve", "2026-07-24 05:00:00")

		page = farm_overview.farm_overview(company=MAIN, overlay="irrigation")
		tool = self.overlays_for(layers=["irrigation"])
		self.assertEqual(page["overlay"]["zones"][0]["status"], tool["zones"][0]["status"])
		self.assertEqual(page["overlay"]["key"], "irrigation")
		self.assertEqual(page["overlay"]["subject"], "zone")

	def test_the_page_computes_no_layer_until_one_is_picked(self):
		"""The options cost no register read at all, so opening the page to check
		a boundary does not pay for the valve log."""
		self.a_farm()
		page = farm_overview.farm_overview(company=MAIN)
		self.assertIsNone(page["overlay"])
		self.assertEqual([spec["key"] for spec in page["overlay_layers"]], list(overlays.LAYER_KEYS))

	def test_the_page_refuses_a_layer_the_roles_do_not_show_by_name(self):
		self.a_farm()
		self.a_picker()
		page = farm_overview.farm_overview(company=MAIN, overlay="harvest")
		self.assertIsNone(page["overlay"])
		self.assertEqual(page["overlay_refused"][0]["key"], "harvest")

	def test_every_layer_dict_carries_a_hex_colour_so_no_client_maps_one(self):
		"""Two clients holding their own status-to-colour tables do not diverge
		loudly — they diverge on one status on one client, which reads as a block
		that is simply a different colour on the phone."""
		_, block, zone = self.a_farm()
		self.a_valve(zone)
		self.valve_event(VALVE, "open_valve", "2026-07-24 03:00:00")
		self.valve_event(VALVE, "close_valve", "2026-07-24 05:00:00")
		self.a_restriction(block, hours_remaining=4.0)
		self.a_crop()
		self.an_observation(BLOCK, growth_stage_code="88", brix_reading=19.9, brix_method="Refractometer")

		answer = self.overlays_for()
		row = next(entry for entry in answer["blocks"] if entry["name"] == BLOCK)
		for key in overlays.LAYER_KEYS:
			with self.subTest(layer=key):
				state = row[key] if key != "irrigation" else row["irrigation"]
				self.assertTrue(str(state["colour"]).startswith("#"), state)
		self.assertTrue(str(answer["zones"][0]["colour"]).startswith("#"))

	def test_an_unrecognised_status_is_grey_and_never_green(self):
		"""A client meeting a status from a newer server should draw the shape as
		unmeasured, which is the honest reading of "this build does not know"."""
		self.assertEqual(overlays.colour_of("some_future_status"), GREY)
		self.assertEqual(overlays.colour_of(None), GREY)
		self.assertNotEqual(overlays.colour_of("some_future_status"), GREEN)

	def test_the_mobile_route_narrows_to_one_block(self):
		"""How a scan becomes a map answer: one docname is one register read
		rather than five hundred."""
		parcel, _, _ = self.a_farm()
		self.tool_data("create_field", {"parcel": parcel, "field_name": "Ridge Top", "acreage": 12.5})
		answer = self.overlays_for(blocks=[BLOCK])
		self.assertEqual([entry["name"] for entry in answer["blocks"]], [BLOCK])

	def test_a_block_this_login_cannot_read_is_named_rather_than_dropped(self):
		self.a_farm()
		answer = self.overlays_for(blocks=[BLOCK, "Somebody Elses Block - XX"])
		self.assertEqual([entry["name"] for entry in answer["blocks"]], [BLOCK])
		self.assertTrue(any("Somebody Elses Block" in note for note in answer["warnings"]))

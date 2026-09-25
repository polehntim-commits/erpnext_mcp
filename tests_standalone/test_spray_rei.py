# SPDX-License-Identifier: MIT
"""Restricted entry: the window a spray opens, and the sentence a worker reads.

THIS IS THE COMPLIANCE-CRITICAL MODULE OF v0.78.0 AND THE TESTS ARE WRITTEN THAT
WAY. Every class below is one claim about the thing that would put a person in a
treated row.

1. **A WINDOW NOBODY CAN COMPUTE IS NOT A WINDOW OF ZERO HOURS.**
   `TheRefusalWhenNothingCanBeComputed`. A spray of a product with no `rei_hours`
   creates NOTHING and says why, because a record saying "restricted for 0 hours"
   reads on every screen as "this block is clear". That is the single worst
   failure this feature can have, and it is the first test in the file.

2. **THE LONGEST INTERVAL IN THE TANK WINS.** `TheLongestIntervalWins`. A mix of
   a 4-hour and a 24-hour product restricts for 24, and the block does not become
   half-enterable at hour twelve.

3. **ONE BLOCK, ONE RECORD.** `OneRecordPerBlock`. A tank over four blocks writes
   four rows, so "is block 7 clear" is one indexed query rather than a scan of
   the task register.

4. **CLOSING IS AN ACT AND EVERY READ PERFORMS IT.** `TheSweepIsOnTheReadPath`.
   The status column is real so the query is possible; the read sweeps first so a
   wedged scheduler still tells the truth at a gate.

5. **THE STATE MACHINE MAY FAIL AND THE RECORD MAY NOT.** `TheRecordSurvivesTheMachine`.
   A sprayer nobody marked `in_use` still restricts the blocks it sprayed.

6. **ONE SENTENCE, EVERYWHERE.** `TheWarningIsOneSentence`. A worker who reads one
   wording at a gate and another on a work order has been given two rules.

THE WINDOWS ARE SEEDED WHERE THE ARITHMETIC IS UNDER TEST, and recorded through
the tool where the RECORDING is. `frappe.utils.now()` advances a second per call
in this harness, so a window opened and read through the tool is always live and
never nearly-expired — which is exactly the case an expiry test needs.
"""

import contextlib

from erpnext_mcp import compliance_fields

from .fixtures import MAIN, OTHER, SPRAY, STORES, V12TestCase, seed_masters, seed_stock
from .harness import META, STORE, frappe

#: A second chemical, so "the longest interval wins" has two to choose between.
SULFUR = "SULFUR-90"

#: A foliar nutrient with no interval at all. What proves that "no window" is a
#: real answer rather than an absent computation.
NUTRIENT = "FOLIAR-N"

BLOCK = "Yellow Camp Block 3 - MC"
BLOCK_TWO = "Yellow Camp Block 4 - MC"

ALL_ON = {
	f"allow_{name}": 1
	for name in (
		"record_spray_application",
		"get_active_rei",
		"list_active_reis",
		"cancel_spray_rei",
		"register_asset",
		"log_asset_state_change",
		"scan_asset",
		"create_parcel",
		"create_field",
		"create_farm_task",
		"assign_farm_task",
		"get_asset_status_report",
	)
}


class REITestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		seed_masters()
		seed_stock()
		self.configure(enabled=1, **ALL_ON)
		self._install_item_intervals()
		self._farm()

	def _install_item_intervals(self):
		"""The REI columns, installed through the real installer.

		Not written onto the Item by hand: the whole mechanism turns on
		`compat.has_field` seeing the column, and a fixture that added it
		directly would prove the window works on a site configured in a way this
		app never produces.
		"""
		compliance_fields.install_compliance_fields()
		STORE.seed("UOM", [{"name": "Gal", "enabled": 1}])
		STORE.seed(
			"Item",
			[
				{
					"name": SULFUR,
					"item_code": SULFUR,
					"item_name": "Micronised Sulfur 90",
					"stock_uom": "Lb",
					"is_stock_item": 1,
					"disabled": 0,
					"item_defaults": [{"company": MAIN, "default_warehouse": STORES}],
					"reorder_levels": [],
					"rei_hours": 24,
					"phi_days": 1,
				},
				{
					"name": NUTRIENT,
					"item_code": NUTRIENT,
					"item_name": "Foliar Nitrogen",
					"stock_uom": "Gal",
					"is_stock_item": 1,
					"disabled": 0,
					"item_defaults": [{"company": MAIN, "default_warehouse": STORES}],
					"reorder_levels": [],
					"rei_hours": 0,
					"phi_days": 0,
				},
			],
		)
		spray = STORE.get_raw("Item", SPRAY)
		spray["rei_hours"] = 4
		spray["phi_days"] = 14

	def _farm(self):
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
		for name in ("Yellow Camp Block 3", "Yellow Camp Block 4"):
			self.tool_data(
				"create_field",
				{
					"parcel": "Mill Creek",
					"field_name": name,
					"acreage": 12.5,
					"variety": "Bing",
					"planting_year": 1998,
					"condition": "Good",
				},
			)

	# ── helpers ────────────────────────────────────────────────────────────
	def a_sprayer(self, name="MC-Sprayer-01", **kw):
		return self.tool_data(
			"register_asset",
			{"name": name, "asset_type": "Sprayer", "company": MAIN, **kw},
		)

	def spray(self, blocks=(BLOCK,), materials=None, **kw):
		payload = {
			"blocks": list(blocks),
			"materials_used": materials if materials is not None else [{"item_code": SPRAY, "qty": 5}],
			"company": MAIN,
		}
		payload.update(kw)
		return self.tool_data("record_spray_application", payload)

	def rei_rows(self):
		return list(STORE.tables.get("Spray REI", {}).values())

	@contextlib.contextmanager
	def without_field(self, doctype: str, fieldname: str):
		"""A site whose installer never ran, for the length of a `with`."""
		meta = META[doctype]
		field = meta._by_name.pop(fieldname, None)
		if field is not None:
			meta.fields.remove(field)
		try:
			yield
		finally:
			if field is not None:
				meta.add(field)


# ── 1. the refusal that matters most ─────────────────────────────────────────
class TheRefusalWhenNothingCanBeComputed(REITestCase):
	def test_a_tank_with_no_interval_creates_nothing(self):
		"""A window of zero hours reads as 'this block is clear' on every screen.
		That is the one wrong answer that puts somebody in a treated row, so the
		spray is refused rather than recorded with a meaningless window."""
		error = self.tool_error(
			"record_spray_application",
			{"blocks": [BLOCK], "materials_used": [{"item_code": NUTRIENT, "qty": 2}], "company": MAIN},
		)
		self.assertIn("no restricted-entry interval could be computed", error)
		self.assertIn("NOTHING WAS RECORDED", error)
		self.assertEqual(self.rei_rows(), [])

	def test_the_refusal_names_the_fix_on_a_site_with_no_column_at_all(self):
		with self.without_field("Item", "rei_hours"), self.without_field("Item", "phi_days"):
			error = self.tool_error(
				"record_spray_application",
				{"blocks": [BLOCK], "materials_used": [{"item_code": SPRAY, "qty": 5}], "company": MAIN},
			)
		self.assertIn("install_compliance_fields", error)
		self.assertEqual(self.rei_rows(), [])

	def test_a_stated_interval_of_zero_is_refused_too(self):
		error = self.tool_error(
			"record_spray_application",
			{"blocks": [BLOCK], "rei_hours": 0, "company": MAIN},
		)
		self.assertIn("greater than zero", error)
		self.assertEqual(self.rei_rows(), [])

	def test_a_spray_with_no_block_restricts_nobody_and_is_refused(self):
		error = self.tool_error(
			"record_spray_application",
			{"materials_used": [{"item_code": SPRAY, "qty": 5}], "company": MAIN},
		)
		self.assertIn("fact about a place", error)


# ── 2. the strictest product in the tank ─────────────────────────────────────
class TheLongestIntervalWins(REITestCase):
	def test_twenty_four_beats_four(self):
		data = self.spray(materials=[{"item_code": SPRAY, "qty": 5}, {"item_code": SULFUR, "qty": 10}])
		self.assertEqual(data["rei_hours"], 24)
		self.assertEqual(data["product"], SULFUR)

	def test_the_block_does_not_half_clear_at_hour_twelve(self):
		"""There is no partial state. The record carries one expiry and the
		window is that expiry, whatever else was in the tank."""
		data = self.spray(materials=[{"item_code": SPRAY, "qty": 5}, {"item_code": SULFUR, "qty": 10}])
		row = self.rei_rows()[0]
		self.assertEqual(float(row["rei_hours"]), 24.0)
		self.assertEqual(str(row["expires_at"]), data["expires_at"])

	def test_every_product_in_the_tank_is_stored_not_just_the_strictest(self):
		"""The question asked after somebody feels ill is about the mix."""
		data = self.spray(materials=[{"item_code": SPRAY, "qty": 5}, {"item_code": NUTRIENT, "qty": 2}])
		codes = {line["item_code"] for line in data["products"]}
		self.assertEqual(codes, {SPRAY, NUTRIENT})

	def test_a_product_with_no_interval_is_warned_about_rather_than_ignored(self):
		"""A missing interval is not a zero one, and a restricted-use product
		nobody entered a label for looks exactly like a foliar nutrient."""
		data = self.spray(materials=[{"item_code": SPRAY, "qty": 5}, {"item_code": NUTRIENT, "qty": 2}])
		self.assertTrue(any(NUTRIENT in warning for warning in data["warnings"]))

	def test_a_stated_interval_overrides_every_label_in_the_tank(self):
		"""For a state or certifier interval longer than the federal label."""
		data = self.spray(materials=[{"item_code": SPRAY, "qty": 5}], rei_hours=48)
		self.assertEqual(data["rei_hours"], 48)
		self.assertEqual(data["rei_source"], "the rei_hours argument")


# ── 3. one block, one record ─────────────────────────────────────────────────
class OneRecordPerBlock(REITestCase):
	def test_a_tank_over_two_blocks_writes_two_rows(self):
		data = self.spray(blocks=(BLOCK, BLOCK_TWO))
		self.assertEqual(data["block_count"], 2)
		self.assertEqual(len(self.rei_rows()), 2)

	def test_both_rows_carry_the_same_window(self):
		"""They were sprayed from one tank in one pass and they clear together."""
		self.spray(blocks=(BLOCK, BLOCK_TWO))
		expiries = {str(row["expires_at"]) for row in self.rei_rows()}
		self.assertEqual(len(expiries), 1)

	def test_the_same_block_twice_in_one_call_is_one_record(self):
		data = self.spray(blocks=(BLOCK, BLOCK))
		self.assertEqual(data["block_count"], 1)

	def test_asking_about_one_block_does_not_return_the_other(self):
		self.spray(blocks=(BLOCK, BLOCK_TWO))
		data = self.tool_data("get_active_rei", {"block": BLOCK})
		self.assertEqual(data["active_rei_count"], 1)
		self.assertEqual(data["active_reis"][0]["block"], BLOCK)

	def test_a_block_that_is_in_no_register_is_refused_by_name(self):
		error = self.tool_error(
			"record_spray_application",
			{"blocks": ["Nowhere Block"], "materials_used": [{"item_code": SPRAY, "qty": 5}]},
		)
		self.assertIn("Nowhere Block", error)
		self.assertIn("searched", error)

	def test_the_blocks_last_spray_date_is_stamped(self):
		"""Two accounts of one morning is what this avoids: the compliance rules
		read `Field.last_spray_date`, and a spray recorded here that did not
		touch it would leave them disagreeing."""
		self.spray(blocks=(BLOCK,), completed_at="2026-08-14 07:30:00")
		self.assertEqual(str(STORE.get_raw("Field", BLOCK)["last_spray_date"]), "2026-08-14")


# ── 4. the longest live window is the answer ─────────────────────────────────
class TwoSpraysOnOneBlock(REITestCase):
	def test_the_block_clears_when_the_last_window_does(self):
		"""A worker told 'two hours' because that was the nearer window would
		walk in under a live restriction."""
		self.seed_window(BLOCK, hours=2)
		self.seed_window(BLOCK, hours=30)
		data = self.tool_data("get_active_rei", {"block": BLOCK})
		self.assertEqual(data["active_rei_count"], 2)
		self.assertGreater(data["hours_remaining"], 20)

	def seed_window(self, block, hours, status="Active", started=None, doctype="Field"):
		"""One Spray REI at a time this test chose.

		Seeded rather than recorded because `frappe.utils.now()` advances a
		second per call here, so every window opened through the tool is live and
		nearly full — and an expiry test needs one that is not.
		"""
		started = started or str(frappe.utils.now())
		rows = STORE.tables.setdefault("Spray REI", {})
		name = f"SREI-{len(rows) + 1:04d}"
		rows[name] = {
			"name": name,
			"docstatus": 0,
			"status": status,
			"block_doctype": doctype,
			"block": block,
			"company": MAIN,
			"sprayer": None,
			"applicator": "Administrator",
			"source_task": None,
			"product": SPRAY,
			"product_name": "Surround WP",
			"rei_hours": float(hours),
			"all_products": "[]",
			"started_at": started,
			"expires_at": str(frappe.utils.add_to_date(started, hours=hours)),
			"closed_at": None,
			"closed_by_job": 0,
			"notes": None,
			"creation": started,
		}
		return name


# ── 5. closing is an act, and every read performs it ─────────────────────────
class TheSweepIsOnTheReadPath(TwoSpraysOnOneBlock):
	def test_a_window_whose_moment_has_passed_reads_as_clear(self):
		self.seed_window(BLOCK, hours=4, started=str(frappe.utils.add_to_date(frappe.utils.now(), hours=-9)))
		data = self.tool_data("get_active_rei", {"block": BLOCK})
		self.assertFalse(data["restricted"])
		self.assertEqual(data["active_rei_count"], 0)

	def test_the_read_closed_it_rather_than_merely_filtering_it_out(self):
		"""The status column is real so the query is possible; a read that only
		filtered would leave a Desk report showing an Active row for ever."""
		name = self.seed_window(
			BLOCK, hours=4, started=str(frappe.utils.add_to_date(frappe.utils.now(), hours=-9))
		)
		self.tool_data("get_active_rei", {"block": BLOCK})
		self.assertEqual(STORE.get_raw("Spray REI", name)["status"], "Expired")
		self.assertTrue(STORE.get_raw("Spray REI", name)["closed_by_job"])

	def test_the_sweep_is_idempotent(self):
		from erpnext_mcp.tools import spray_rei

		self.seed_window(BLOCK, hours=4, started=str(frappe.utils.add_to_date(frappe.utils.now(), hours=-9)))
		self.assertEqual(spray_rei.close_expired_reis()["closed_count"], 1)
		self.assertEqual(spray_rei.close_expired_reis()["closed_count"], 0)

	def test_the_sweep_never_raises_without_the_doctype(self):
		"""Every scheduled job in this app makes that promise."""
		from erpnext_mcp.tools import spray_rei

		from .harness import INSTALLED_DOCTYPES

		INSTALLED_DOCTYPES.discard("Spray REI")
		try:
			self.assertEqual(spray_rei.close_expired_reis()["closed_count"], 0)
		finally:
			INSTALLED_DOCTYPES.add("Spray REI")

	def test_a_live_window_survives_the_sweep(self):
		name = self.seed_window(BLOCK, hours=8)
		self.tool_data("get_active_rei", {"block": BLOCK})
		self.assertEqual(STORE.get_raw("Spray REI", name)["status"], "Active")


# ── 6. the record survives the state machine ─────────────────────────────────
class TheRecordSurvivesTheMachine(REITestCase):
	def test_a_sprayer_that_was_never_marked_in_use_still_restricts_the_block(self):
		"""A compliance record must not depend on somebody having pressed the
		right button first."""
		self.a_sprayer()
		data = self.spray(sprayer="MC-Sprayer-01")
		self.assertEqual(data["block_count"], 1)
		self.assertIsNone(data["state_change"])
		self.assertTrue(any("state was left alone" in w for w in data["warnings"]))

	def test_a_sprayer_that_is_spraying_is_ended(self):
		self.a_sprayer()
		self.tool_data("log_asset_state_change", {"asset_name": "MC-Sprayer-01", "action": "fill_tank"})
		self.tool_data("log_asset_state_change", {"asset_name": "MC-Sprayer-01", "action": "start_spray"})

		data = self.spray(sprayer="MC-Sprayer-01")
		self.assertEqual(data["state_change"]["to_state"], "empty")

	def test_a_machine_that_is_not_a_sprayer_is_refused_before_anything_is_written(self):
		"""A window filed against a tractor is one nobody scanning the sprayer
		will ever see."""
		self.tool_data("register_asset", {"name": "MC-Tractor-09", "asset_type": "Tractor", "company": MAIN})
		error = self.tool_error(
			"record_spray_application",
			{
				"blocks": [BLOCK],
				"sprayer": "MC-Tractor-09",
				"materials_used": [{"item_code": SPRAY, "qty": 5}],
			},
		)
		self.assertIn("not a Sprayer", error)
		self.assertEqual(self.rei_rows(), [])


# ── 7. one sentence, everywhere ──────────────────────────────────────────────
class TheWarningIsOneSentence(REITestCase):
	def test_it_names_the_product_the_time_left_and_the_ppe(self):
		self.spray()
		warning = self.tool_data("get_active_rei", {"block": BLOCK})["warning"]
		self.assertIn("REI active", warning)
		self.assertIn("Surround", warning)
		self.assertIn("do not enter without PPE", warning)

	def test_a_task_dispatched_to_a_restricted_block_carries_it(self):
		"""Told, not refused: §170.607 permits early entry for specific tasks
		with the label's PPE, so a server refusing outright would be stricter
		than the regulation and would train foremen to route around this app."""
		self.spray()
		task = self.tool_data(
			"create_farm_task",
			{
				"task_name": "Thin Block 3",
				"task_type": "Harvest",
				"evidence_required": {"photos": True},
				"company": MAIN,
				"location_doctype": "Field",
				"location": BLOCK,
			},
		)
		self.assertTrue(any("REI active" in warning for warning in task["warnings"]))

	def test_a_task_on_a_clear_block_carries_no_rei_warning(self):
		task = self.tool_data(
			"create_farm_task",
			{
				"task_name": "Thin Block 4",
				"task_type": "Harvest",
				"evidence_required": {"photos": True},
				"company": MAIN,
				"location_doctype": "Field",
				"location": BLOCK_TWO,
			},
		)
		self.assertFalse(any("REI active" in warning for warning in task.get("warnings", [])))


# ── 8. the board ─────────────────────────────────────────────────────────────
class TheFarmWideBoard(REITestCase):
	def test_it_lists_every_restricted_block(self):
		self.spray(blocks=(BLOCK, BLOCK_TWO))
		data = self.tool_data("list_active_reis", {"company": MAIN})
		self.assertEqual(data["restricted_block_count"], 2)
		self.assertEqual(sorted(data["restricted_blocks"]), sorted([BLOCK, BLOCK_TWO]))

	def test_it_is_empty_on_a_farm_with_nothing_sprayed(self):
		data = self.tool_data("list_active_reis", {"company": MAIN})
		self.assertEqual(data["active_count"], 0)
		self.assertEqual(data["restricted_blocks"], [])

	def test_it_can_be_narrowed_to_one_machine(self):
		self.a_sprayer()
		self.a_sprayer(name="MC-Sprayer-02")
		self.spray(blocks=(BLOCK,), sprayer="MC-Sprayer-01")
		self.spray(blocks=(BLOCK_TWO,), sprayer="MC-Sprayer-02")

		data = self.tool_data("list_active_reis", {"sprayer": "MC-Sprayer-01"})
		self.assertEqual(data["restricted_blocks"], [BLOCK])

	def test_another_entitys_restriction_is_not_on_this_board(self):
		self.spray()
		data = self.tool_data("list_active_reis", {"company": OTHER})
		self.assertEqual(data["active_count"], 0)


# ── 9. cancellation is a record, not a deletion ──────────────────────────────
class Cancellation(REITestCase):
	def test_it_needs_a_reason(self):
		self.spray()
		name = self.rei_rows()[0]["name"]
		error = self.tool_error("cancel_spray_rei", {"rei": name})
		self.assertIn("reason", error)

	def test_the_row_survives_as_cancelled(self):
		"""'Why did this block show as closed on Tuesday morning' has an answer,
		and a deleted row has none."""
		self.spray()
		name = self.rei_rows()[0]["name"]
		self.tool_data("cancel_spray_rei", {"rei": name, "reason": "logged against the wrong block"})

		row = STORE.get_raw("Spray REI", name)
		self.assertEqual(row["status"], "Cancelled")
		self.assertIn("wrong block", row["notes"])

	def test_a_cancelled_window_stops_restricting(self):
		self.spray()
		name = self.rei_rows()[0]["name"]
		self.tool_data("cancel_spray_rei", {"rei": name, "reason": "wrong block"})
		self.assertFalse(self.tool_data("get_active_rei", {"block": BLOCK})["restricted"])

	def test_cancelling_twice_is_refused(self):
		self.spray()
		name = self.rei_rows()[0]["name"]
		self.tool_data("cancel_spray_rei", {"rei": name, "reason": "wrong block"})
		self.assertIn(
			"already cancelled", self.tool_error("cancel_spray_rei", {"rei": name, "reason": "again"})
		)


# ── 10. the window runs from the spray, not from the filing ──────────────────
class TheWindowRunsFromTheSpray(REITestCase):
	def test_completed_at_anchors_the_expiry(self):
		"""A handset out of signal for two hours must not restrict a block for
		two hours longer than the label says."""
		data = self.spray(completed_at="2026-08-14 06:00:00")
		self.assertEqual(data["expires_at"], "2026-08-14 10:00:00")

	def test_the_stored_row_carries_both_ends(self):
		self.spray(completed_at="2026-08-14 06:00:00")
		row = self.rei_rows()[0]
		self.assertEqual(str(row["started_at"]), "2026-08-14 06:00:00")
		self.assertEqual(str(row["expires_at"]), "2026-08-14 10:00:00")


# ── 11. the phone's spray path opens no Spray REI row ────────────────────────
class ThePhonesSprayTaskIsARestriction(REITestCase):
	"""A Spray task finished on a phone stamps `rei_expires_at` on the Farm Task
	and writes no Spray REI row. Before this release `get_active_rei` read the
	register alone and answered "clear" for that block — the reason the route was
	held back from the phone (TELL_THE_FARM_AUDIT.md, F2)."""

	TASK = "FT-SPRAY-PHONE"

	def a_phone_spray(self, name=TASK, hours_left=4, state="Completed", company=MAIN, location=BLOCK):
		now = frappe.utils.now()
		STORE.seed(
			"Farm Task",
			[
				{
					"name": name,
					"title": "Spray block 3",
					"task_type": "Spray",
					"state": state,
					"company": company,
					"location_doctype": "Field",
					"location": location,
					"spray_completed_at": str(frappe.utils.add_to_date(now, hours=-1)),
					"rei_expires_at": str(frappe.utils.add_to_date(now, hours=hours_left)),
					"rei_source_item": SPRAY,
				}
			],
		)

	def test_a_completed_phone_spray_restricts_the_block(self):
		self.a_phone_spray()
		data = self.tool_data("get_active_rei", {"block": BLOCK, "company": MAIN})
		self.assertIs(data["restricted"], True)
		self.assertEqual(data["active_rei_count"], 1)
		window = data["active_reis"][0]
		self.assertEqual(window["source_doctype"], "Farm Task")
		self.assertEqual(window["source_task"], self.TASK)
		self.assertEqual(window["product"], SPRAY)
		self.assertGreater(data["hours_remaining"], 3)
		self.assertIn("REI active", data["warning"])
		self.assertEqual(data["products"], ["Surround WP"])

	def test_an_expired_task_window_is_clear(self):
		self.a_phone_spray(hours_left=-2)
		data = self.tool_data("get_active_rei", {"block": BLOCK, "company": MAIN})
		self.assertIs(data["restricted"], False)

	def test_a_task_that_was_never_finished_is_not_a_spray(self):
		self.a_phone_spray(state="In-Progress")
		data = self.tool_data("get_active_rei", {"block": BLOCK, "company": MAIN})
		self.assertIs(data["restricted"], False)

	def test_a_task_the_register_already_cites_is_counted_once(self):
		self.a_phone_spray()
		self.spray(blocks=(BLOCK,), source_task=self.TASK)
		data = self.tool_data("get_active_rei", {"block": BLOCK, "company": MAIN})
		self.assertEqual(data["active_rei_count"], 1)
		self.assertEqual(data["active_reis"][0]["source_doctype"], "Spray REI")

	def test_another_entitys_task_is_not_on_this_block(self):
		self.a_phone_spray(company=OTHER)
		data = self.tool_data("get_active_rei", {"block": BLOCK, "company": MAIN})
		self.assertIs(data["restricted"], False)

	def test_a_window_with_no_company_is_kept_rather_than_dropped(self):
		"""`company` is optional on both registers. An equality filter would drop
		the row, and dropping it here tells somebody a sprayed block is clear."""
		self.a_phone_spray(company="")
		data = self.tool_data("get_active_rei", {"block": BLOCK, "company": MAIN})
		self.assertIs(data["restricted"], True)

	def test_a_register_row_with_no_company_is_kept_too(self):
		self.spray(blocks=(BLOCK,))
		name = self.rei_rows()[0]["name"]
		STORE.get_raw("Spray REI", name)["company"] = None
		data = self.tool_data("get_active_rei", {"block": BLOCK, "company": MAIN})
		self.assertIs(data["restricted"], True)

	def test_the_board_includes_phone_sprays(self):
		self.a_phone_spray()
		self.spray(blocks=(BLOCK_TWO,))
		data = self.tool_data("list_active_reis", {"company": MAIN})
		self.assertEqual(sorted(data["restricted_blocks"]), sorted([BLOCK, BLOCK_TWO]))
		self.assertIs(data["spray_tasks_included"], True)

	def test_a_board_narrowed_to_one_machine_says_it_left_tasks_out(self):
		self.a_phone_spray()
		data = self.tool_data("list_active_reis", {"company": MAIN, "sprayer": "MC-Sprayer-01"})
		self.assertEqual(data["restricted_blocks"], [])
		self.assertIs(data["spray_tasks_included"], False)

	def test_the_recently_cleared_view_includes_a_task_that_cleared(self):
		self.a_phone_spray(hours_left=-2)
		data = self.tool_data("list_active_reis", {"company": MAIN, "include_expired": True})
		self.assertEqual([window["source_task"] for window in data["reis"]], [self.TASK])
		self.assertIs(data["reis"][0]["active"], False)
		self.assertEqual(data["active_count"], 0)

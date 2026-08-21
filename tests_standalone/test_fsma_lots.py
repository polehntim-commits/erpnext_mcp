# SPDX-License-Identifier: MIT
"""FSMA 204 — the lot code, the events, and the recall drill. v0.111.0.

WHAT IS ACTUALLY BEING TESTED is that a lot code survives the three things the
free-text chain does not: a hand-off, a reused id, and a transformation. Each
class below is one of those, or one of the breaks that must be NAMED rather than
returned as an empty list.

    THE CODE IS AN IDENTIFIER      it is generated from the block, the variety
                                   and the day; it is the docname; it is unique;
                                   and asking for the same afternoon's fruit
                                   twice returns the SAME lot rather than a
                                   second one. Two codes for one afternoon split
                                   a recall in half and neither half names the
                                   other.

    THE GRAPH SURVIVES A PACK LINE four field lots combined into a pallet trace
                                   forward to the pallet's customers and backward
                                   to the four blocks and their sprays. This is
                                   the hop nothing in the older chain can make.

    THE BREAKS ARE NAMED           a lot with no Shipping event, a shipment with
                                   no receiver, a pointer at a deleted record.
                                   Each is reported as what it is.

    THE INDEXER IS IDEMPOTENT      running it twice over one week writes nothing
                                   the second time. This is what makes it safe to
                                   schedule and safe to run over a season.

`NobodyToTelephone` AND `TheDrillChangesNothing` ARE THE TWO TO READ IF YOU ONLY
READ TWO. An empty `parties_to_notify` and a complete one look identical to
anybody skimming, and only one of them means the recall cannot be executed; and a
drill that quietly set a lot to Recalled would make the rehearsal
indistinguishable from the event.
"""

from erpnext_mcp import compliance_fields
from erpnext_mcp.tools import lots

from .fixtures import MAIN, V12TestCase, seed_masters, seed_stock
from .harness import STORE

ALL_ON = {
	f"allow_{name}": 1
	for name in (
		"create_traceability_lot",
		"get_traceability_lot",
		"list_traceability_lots",
		"record_cte",
		"trace_lot_forward",
		"trace_lot_backward",
		"recall_drill",
		"get_lot_timeline",
		"index_lot_events",
		"create_parcel",
		"create_field",
		"create_spray_application",
		"seal_bin",
		"trace_forward",
		"trace_backward",
	)
}

BLOCK3 = "Yellow Camp Block 3 - MC"
BLOCK4 = "Yellow Camp Block 4 - MC"
CAPTAN = "CAPTAN-80WDG"
HARVEST = "2026-07-20"


class LotTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		seed_masters()
		seed_stock()
		self.configure(enabled=1, **ALL_ON)
		compliance_fields.install_compliance_fields()
		STORE.seed("UOM", [{"name": "Lb", "enabled": 1}])
		STORE.seed(
			"Item",
			[
				{
					"name": CAPTAN,
					"item_code": CAPTAN,
					"item_name": "Captan 80 WDG",
					"stock_uom": "Lb",
					"is_stock_item": 1,
					"disabled": 0,
					"item_defaults": [],
					"reorder_levels": [],
					"rei_hours": 24,
					"phi_days": 14,
				}
			],
		)
		self.tool_data(
			"create_parcel", {"owning_entity": MAIN, "parcel_name": "Mill Creek", "acreage": 131.43}
		)
		for name in ("Yellow Camp Block 3", "Yellow Camp Block 4"):
			self.tool_data(
				"create_field",
				{"parcel": "Mill Creek", "field_name": name, "acreage": 12.5, "crop": "Cherry"},
			)

	# ── furniture ───────────────────────────────────────────────────────────
	def raw(self, doctype, name):
		"""The stored row, not a tool's answer.

		The tests that assert a read CHANGED NOTHING have to look past the read,
		because a tool that quietly rewrote a lot would return the rewritten one
		and the assertion would pass against its own side effect.
		"""
		return STORE.get_raw(doctype, name)

	def a_lot(self, field=BLOCK3, variety="Bing", harvest_date=HARVEST, **overrides):
		payload = {"field": field, "variety": variety, "harvest_date": harvest_date, "company": MAIN}
		payload.update(overrides)
		return self.tool_data("create_traceability_lot", payload)

	def a_shipping_event(self, lot_code, **overrides):
		payload = {
			"lot_code": lot_code,
			"event_type": "Shipping",
			"event_datetime": "2026-07-22 14:10:00",
			"destination_location": "Hood River Cold Storage",
			"receiver": "Columbia Packing Co",
			"carrier": "Nordby Transport",
			"quantity": 42,
			"quantity_uom": "bin",
		}
		payload.update(overrides)
		return self.tool_data("record_cte", payload)

	def a_spray(self, blocks=(BLOCK3,), completed_at="2026-05-20 11:30:00", **overrides):
		payload = {
			"company": MAIN,
			"blocks": [{"block": block, "acres": 12.5} for block in blocks],
			"products": [
				{"item_code": CAPTAN, "rate_per_acre": 5, "rate_uom": "Lb", "epa_reg_number": "66222-242"}
			],
			"applicator_license": "OR-PA-88213",
			"started_at": "2026-05-20 07:00:00",
			"completed_at": completed_at,
		}
		payload.update(overrides)
		return self.tool_data("create_spray_application", payload)


class TheCodeIsAnIdentifier(LotTestCase):
	"""A bin tag is a sticker somebody else printed. This is not."""

	def test_the_code_is_the_block_the_variety_and_the_day(self):
		data = self.a_lot()
		self.assertEqual(data["lot_code"], "YCB3-BING-20260720-01")
		self.assertEqual(data["field"], BLOCK3)
		self.assertEqual(data["status"], "Active")

	def test_the_code_is_the_docname_so_a_link_carries_the_code_itself(self):
		"""`autoname: field:lot_code`. A Link to a hash would mean every trace
		read had to resolve a name nobody standing at a pack line holds."""
		data = self.a_lot()
		row = self.raw("Traceability Lot Code", data["lot_code"])
		self.assertEqual(row["name"], data["lot_code"])

	def test_two_sibling_blocks_do_not_collide_on_one_day(self):
		"""THE NEGATIVE CONTROL FOR THE NAMING SCHEME. Truncating the Field name
		gives 'YELLOWCAMP' for both Block 3 and Block 4, so the codes would
		differ only by sequence number — unique, and useless to anybody reading
		one off a bin."""
		third = self.a_lot(field=BLOCK3)["lot_code"]
		fourth = self.a_lot(field=BLOCK4)["lot_code"]
		self.assertEqual(third, "YCB3-BING-20260720-01")
		self.assertEqual(fourth, "YCB4-BING-20260720-01")

	def test_the_second_lot_off_one_block_and_day_takes_the_next_sequence(self):
		first = self.a_lot()["lot_code"]
		second = self.a_lot(allow_duplicate=1)["lot_code"]
		self.assertEqual(first, "YCB3-BING-20260720-01")
		self.assertEqual(second, "YCB3-BING-20260720-02")

	def test_asking_twice_for_one_afternoon_returns_the_same_lot(self):
		"""IDEMPOTENT. Two codes for one afternoon's fruit split a recall in half
		and neither half names the other."""
		first = self.a_lot()
		again = self.a_lot()
		self.assertTrue(again["already_existed"])
		self.assertEqual(again["lot_code"], first["lot_code"])
		self.assertEqual(len(STORE.rows("Traceability Lot Code")), 1)

	def test_allow_duplicate_is_how_a_block_picked_twice_gets_two_codes(self):
		"""The negative control for the idempotency above: it is a default, not
		a prohibition."""
		self.a_lot()
		self.a_lot(allow_duplicate=1)
		self.assertEqual(len(STORE.rows("Traceability Lot Code")), 2)

	def test_a_lot_with_neither_a_block_nor_a_source_is_refused(self):
		error = self.tool_error("create_traceability_lot", {"variety": "Bing", "company": MAIN})
		self.assertIn("origin nobody wrote down", error)

	def test_a_code_that_already_exists_is_refused_rather_than_doubled(self):
		self.a_lot()
		error = self.tool_error(
			"create_traceability_lot",
			{"field": BLOCK4, "variety": "Rainier", "lot_code": "YCB3-BING-20260720-01", "company": MAIN},
		)
		self.assertIn("names ONE lot", error)

	def test_the_company_comes_off_the_blocks_owning_entity(self):
		"""`owning_entity` AND NOT `company`. Field spells its entity column
		differently from everything else on the site, and a lookup for `company`
		returns nothing on every site — the lot would arrive unscoped with no
		error anywhere."""
		data = self.tool_data(
			"create_traceability_lot", {"field": BLOCK3, "variety": "Bing", "harvest_date": HARVEST}
		)
		self.assertEqual(data["company"], MAIN)

	def test_creating_a_lot_files_its_own_creating_event(self):
		data = self.a_lot()
		self.assertTrue(data["opening_event"]["created"])
		self.assertEqual(data["opening_event"]["event_type"], "Creating")


class TheEventIsAPointerNotACopy(LotTestCase):
	def test_an_event_names_the_register_that_holds_the_detail(self):
		lot = self.a_lot()["lot_code"]
		data = self.tool_data(
			"record_cte",
			{
				"lot_code": lot,
				"event_type": "Receiving",
				"reference_doctype": "Field",
				"reference_name": BLOCK3,
				"description": "Bins taken in.",
			},
		)
		self.assertEqual(data["event"]["reference_doctype"], "Field")
		self.assertEqual(data["event"]["reference_name"], BLOCK3)

	def test_the_same_source_record_is_one_event_however_often_it_is_filed(self):
		"""IDEMPOTENT ON THE TUPLE, deliberately NOT including the timestamp: a
		corrected `completed_at` on a spray must not produce a second Growing
		event for one pass."""
		lot = self.a_lot()["lot_code"]
		payload = {
			"lot_code": lot,
			"event_type": "Growing",
			"reference_doctype": "Field",
			"reference_name": BLOCK3,
		}
		self.tool_data("record_cte", payload)
		again = self.tool_data("record_cte", {**payload, "event_datetime": "2026-06-01 09:00:00"})
		self.assertTrue(again["already_recorded"])
		self.assertEqual(len(STORE.rows("Critical Tracking Event")), 2)  # the Creating event, plus one

	def test_two_hand_entered_events_with_no_reference_are_two_loads(self):
		"""THE NEGATIVE CONTROL. There is nothing to deduplicate on, and
		collapsing them would delete a load that left."""
		lot = self.a_lot()["lot_code"]
		self.a_shipping_event(lot)
		self.a_shipping_event(lot, receiver="Second Buyer")
		shipping = [row for row in STORE.rows("Critical Tracking Event") if row["event_type"] == "Shipping"]
		self.assertEqual(len(shipping), 2)

	def test_an_event_never_changes_the_lot(self):
		"""A Shipping event does not decrement a quantity or set a status. Those
		are two measurements taken by two people, and a silent rewrite would make
		the lot disagree with its own history."""
		lot = self.a_lot(quantity=100, quantity_uom="bin")["lot_code"]
		self.a_shipping_event(lot, quantity=42)
		row = self.raw("Traceability Lot Code", lot)
		self.assertEqual(row["quantity"], 100)
		self.assertEqual(row["status"], "Active")

	def test_an_unknown_event_type_is_refused_with_the_five_the_rule_names(self):
		lot = self.a_lot()["lot_code"]
		error = self.tool_error("record_cte", {"lot_code": lot, "event_type": "Harvest"})
		self.assertIn("Growing, Receiving, Transforming, Creating, Shipping", error)

	def test_a_pointer_at_a_record_that_does_not_exist_is_kept_and_reported(self):
		"""An event refused over a pointer is an event nobody records. The reads
		report an unresolved reference as the data fault it is."""
		lot = self.a_lot()["lot_code"]
		data = self.tool_data(
			"record_cte",
			{
				"lot_code": lot,
				"event_type": "Shipping",
				"reference_doctype": "Trade Shipment",
				"reference_name": "TSHIP-NOT-A-THING",
				"receiver": "Columbia Packing Co",
			},
		)
		self.assertIn("not a record on this site", data["unresolved_reference_note"])
		self.assertTrue(data["event"]["name"])

	def test_a_shipping_event_naming_nobody_says_so_at_the_moment_it_is_filed(self):
		lot = self.a_lot()["lot_code"]
		data = self.tool_data("record_cte", {"lot_code": lot, "event_type": "Shipping"})
		self.assertIn("cannot be traced to anybody", data["no_destination_note"])


class TheGraphSurvivesAPackLine(LotTestCase):
	"""Four field lots into one pallet. The hop the free-text chain cannot make."""

	def a_pallet(self):
		third = self.a_lot(field=BLOCK3)["lot_code"]
		fourth = self.a_lot(field=BLOCK4)["lot_code"]
		pallet = self.tool_data(
			"create_traceability_lot",
			{
				"variety": "Mixed",
				"harvest_date": "2026-07-22",
				"company": MAIN,
				"source_lots": [
					{"source_lot": third, "quantity_contributed": 20, "quantity_uom": "bin"},
					fourth,
				],
			},
		)
		return third, fourth, pallet["lot_code"]

	def test_a_transformation_lot_needs_no_block_of_its_own(self):
		_, _, pallet = self.a_pallet()
		row = self.raw("Traceability Lot Code", pallet)
		self.assertFalse(row.get("field"))

	def test_a_transformation_lot_opens_with_a_transforming_event(self):
		third = self.a_lot()["lot_code"]
		data = self.tool_data(
			"create_traceability_lot",
			{"variety": "Mixed", "harvest_date": "2026-07-22", "company": MAIN, "source_lots": [third]},
		)
		self.assertEqual(data["opening_event"]["event_type"], "Transforming")

	def test_forward_from_a_field_lot_reaches_the_pallets_customers(self):
		third, _, pallet = self.a_pallet()
		self.a_shipping_event(pallet)
		data = self.tool_data("trace_lot_forward", {"lot_code": third})
		self.assertEqual(data["counts"]["downstream_lots"], 1)
		self.assertEqual([row["destination"] for row in data["destinations"]], ["Hood River Cold Storage"])
		self.assertEqual(data["destinations"][0]["receiver"], "Columbia Packing Co")

	def test_backward_from_the_pallet_reaches_both_blocks(self):
		_, _, pallet = self.a_pallet()
		data = self.tool_data("trace_lot_backward", {"lot_code": pallet})
		self.assertEqual(set(data["blocks"]), {BLOCK3, BLOCK4})
		self.assertEqual(data["counts"]["upstream_lots"], 2)

	def test_backward_from_the_pallet_reaches_the_spray_register(self):
		"""THE RESIDUE QUESTION. This is the hop the whole feature exists for:
		from a pallet code a buyer is holding to what the blocks were given."""
		self.a_spray(blocks=(BLOCK3,))
		_, _, pallet = self.a_pallet()
		data = self.tool_data("trace_lot_backward", {"lot_code": pallet})
		self.assertEqual(data["counts"]["spray_applications"], 1)
		self.assertEqual(data["spray_applications"][0]["block"], BLOCK3)

	def test_a_pass_made_after_the_fruit_came_off_is_not_named(self):
		"""THE DATE BOUND. Naming it sends somebody to investigate a tank that
		was never on that crop."""
		self.a_spray(blocks=(BLOCK3,), completed_at="2026-08-02 11:30:00")
		lot = self.a_lot(field=BLOCK3)["lot_code"]
		data = self.tool_data("trace_lot_backward", {"lot_code": lot})
		self.assertEqual(data["counts"]["spray_applications"], 0)

	def test_a_lot_cannot_be_made_from_itself(self):
		lot = self.a_lot()["lot_code"]
		row = self.raw("Traceability Lot Code", lot)
		self.assertTrue(row)
		error = self.tool_error(
			"create_traceability_lot",
			{"lot_code": lot, "variety": "Mixed", "source_lots": [lot], "company": MAIN},
		)
		self.assertIn("already a lot on this site", error)

	def test_a_source_lot_that_is_not_a_lot_is_refused(self):
		error = self.tool_error(
			"create_traceability_lot",
			{"variety": "Mixed", "source_lots": ["NOT-A-LOT-01"], "company": MAIN},
		)
		self.assertIn("transformation edge", error)


class NobodyToTelephone(LotTestCase):
	"""An empty destination list and a complete one look identical to anybody
	skimming, and only one of them means the recall cannot be executed."""

	def test_a_lot_that_never_shipped_is_a_break_not_an_empty_list(self):
		lot = self.a_lot()["lot_code"]
		data = self.tool_data("trace_lot_forward", {"lot_code": lot})
		self.assertEqual(data["destinations"], [])
		missing = [entry["missing"] for entry in data["breaks"]]
		self.assertIn("shipping events", missing)
		note = next(e["note"] for e in data["breaks"] if e["missing"] == "shipping events")
		self.assertIn("DIFFERENT ANSWERS", note)

	def test_a_shipment_naming_nobody_is_counted_and_the_scope_is_widened(self):
		lot = self.a_lot()["lot_code"]
		self.a_shipping_event(lot, destination_location="", receiver="", carrier="")
		data = self.tool_data("trace_lot_forward", {"lot_code": lot})
		self.assertEqual(data["counts"]["unplaced_shipments"], 1)
		self.assertIn("a destination", [entry["missing"] for entry in data["breaks"]])

	def test_a_drill_with_no_party_at_all_refuses_to_read_as_a_clean_bill(self):
		lot = self.a_lot()["lot_code"]
		data = self.tool_data("recall_drill", {"lot_code": lot})
		self.assertEqual(data["parties_to_notify"], [])
		self.assertIn("do not read this as a clean bill", data["scope_warning"].lower())


class TheDrillChangesNothing(LotTestCase):
	def test_the_drill_names_the_party_the_lot_codes_and_the_dates(self):
		lot = self.a_lot()["lot_code"]
		self.a_shipping_event(lot)
		data = self.tool_data("recall_drill", {"lot_code": lot})
		party = data["parties_to_notify"][0]
		self.assertEqual(party["party"], "Columbia Packing Co")
		self.assertEqual(party["destination"], "Hood River Cold Storage")
		self.assertEqual(party["carriers"], ["Nordby Transport"])
		self.assertEqual(party["lot_codes"], [lot])
		self.assertEqual(party["first_shipped_at"], "2026-07-22 14:10:00")

	def test_the_drill_writes_nothing_and_recalls_nothing(self):
		"""A drill is run on fruit nobody is worried about — that is what makes
		it a drill. A read that changed a status would make the rehearsal
		indistinguishable from the event."""
		lot = self.a_lot()["lot_code"]
		self.a_shipping_event(lot)
		before = len(STORE.rows("Critical Tracking Event"))
		self.tool_data("recall_drill", {"lot_code": lot})
		self.assertEqual(self.raw("Traceability Lot Code", lot)["status"], "Active")
		self.assertEqual(len(STORE.rows("Critical Tracking Event")), before)

	def test_the_drill_is_advertised_as_read_only(self):
		"""`readOnlyHint` is the inverse of `mutating` by construction, and a
		recall drill that advertised itself as a write is one nobody runs."""
		from erpnext_mcp import registry

		self.assertFalse(registry.TOOLS["recall_drill"]["mutating"])
		self.assertTrue(registry.TOOLS["recall_drill"]["annotations"]["readOnlyHint"])


class TheTimelineResolvesItsPointers(LotTestCase):
	def test_the_referenced_record_comes_back_beside_the_event(self):
		lot = self.a_lot()["lot_code"]
		self.tool_data(
			"record_cte",
			{
				"lot_code": lot,
				"event_type": "Growing",
				"reference_doctype": "Field",
				"reference_name": BLOCK3,
			},
		)
		data = self.tool_data("get_lot_timeline", {"lot_code": lot})
		growing = next(row for row in data["timeline"] if row["event_type"] == "Growing")
		self.assertTrue(growing["reference_detail"]["resolved"])
		self.assertEqual(growing["reference_detail"]["name"], BLOCK3)

	def test_a_pointer_at_a_deleted_record_says_which_of_the_two_faults_it_is(self):
		"""An uninstalled register and a deleted record are different problems,
		and only one of them means evidence went missing."""
		lot = self.a_lot()["lot_code"]
		self.tool_data(
			"record_cte",
			{
				"lot_code": lot,
				"event_type": "Shipping",
				"reference_doctype": "Trade Shipment",
				"reference_name": "TSHIP-GONE",
				"receiver": "Columbia Packing Co",
			},
		)
		data = self.tool_data("get_lot_timeline", {"lot_code": lot})
		broken = next(row for row in data["timeline"] if row["reference_name"] == "TSHIP-GONE")
		self.assertFalse(broken["reference_detail"]["resolved"])
		self.assertIn("deleted, renamed or never existed", broken["reference_detail"]["reason"])

	def test_the_timeline_is_ordered_by_when_the_event_happened(self):
		"""Not by creation order. A phone that posts an afternoon of events when
		the signal comes back would otherwise produce a timeline reading in the
		order the bars returned."""
		lot = self.a_lot()["lot_code"]
		self.tool_data(
			"record_cte",
			{"lot_code": lot, "event_type": "Shipping", "event_datetime": "2026-07-25 08:00:00"},
		)
		self.tool_data(
			"record_cte",
			{"lot_code": lot, "event_type": "Growing", "event_datetime": "2026-05-01 08:00:00"},
		)
		stamps = [
			row["event_datetime"] for row in self.tool_data("get_lot_timeline", {"lot_code": lot})["timeline"]
		]
		self.assertEqual(stamps, sorted(stamps))

	def test_a_timeline_with_no_growing_event_says_so(self):
		lot = self.a_lot()["lot_code"]
		self.a_shipping_event(lot)
		data = self.tool_data("get_lot_timeline", {"lot_code": lot})
		self.assertIn("a Growing event", [entry["missing"] for entry in data["breaks"]])


class TheIndexerIsIdempotent(LotTestCase):
	"""It is a TOOL and not a `doc_events` hook — `hooks.py` promises this app
	installs none and the suite fails the build over one. That makes re-running
	it the ordinary case rather than the exception."""

	def a_seal(self, tag="OML-4471", **overrides):
		payload = {
			"bin_tag": tag,
			"bucket_count": 42,
			"field": BLOCK3,
			"crop": "Bing",
			"company": MAIN,
			"sealed_at": f"{HARVEST} 11:30:00",
		}
		payload.update(overrides)
		return self.tool_data("seal_bin", payload)

	def a_ticket(self, name="ST-0001", **overrides):
		payload = {
			"name": name,
			"ticket_number": name,
			"date": HARVEST,
			"customer": "Columbia Packing Co",
			"company": MAIN,
			"variety": "Bing",
			"net_weight": 18000,
			"weight_uom": "Lb",
			"destination": "Hood River Cold Storage",
			"field": BLOCK3,
			"status": "Submitted",
		}
		payload.update(overrides)
		STORE.seed("Scale Ticket", [payload])
		return name

	def sweep(self, **overrides):
		payload = {"date_from": HARVEST, "date_to": HARVEST, "company": MAIN}
		payload.update(overrides)
		return self.tool_data("index_lot_events", payload)

	def test_a_bin_seal_creates_the_lot_it_implies(self):
		self.a_seal()
		data = self.sweep()
		self.assertEqual(data["counts"]["lots_created"], 1)
		self.assertEqual(data["lots_created"], ["YCB3-BING-20260720-01"])

	def test_the_seal_is_filed_as_a_receiving_event_against_that_lot(self):
		self.a_seal()
		self.sweep()
		timeline = self.tool_data("get_lot_timeline", {"lot_code": "YCB3-BING-20260720-01"})["timeline"]
		receiving = next(row for row in timeline if row["event_type"] == "Receiving")
		self.assertEqual(receiving["reference_doctype"], "Bin Seal")
		self.assertEqual(receiving["quantity"], 42)
		self.assertEqual(receiving["quantity_uom"], "bucket")

	def test_a_scale_ticket_is_filed_as_a_shipping_event_with_the_customer(self):
		"""A ticket is a load weighed onto somebody else's scale — the grower's
		fruit arriving at the packer. That is a departure from THIS operation,
		and it is what makes recall_drill able to name anybody at all."""
		self.a_seal()
		self.a_ticket()
		self.sweep()
		drill = self.tool_data("recall_drill", {"lot_code": "YCB3-BING-20260720-01"})
		self.assertEqual(drill["parties_to_notify"][0]["party"], "Columbia Packing Co")
		self.assertEqual(drill["parties_to_notify"][0]["destination"], "Hood River Cold Storage")

	def test_a_spray_that_reached_the_block_is_filed_as_a_growing_event(self):
		self.a_spray(blocks=(BLOCK3,))
		self.a_seal()
		self.sweep()
		timeline = self.tool_data("get_lot_timeline", {"lot_code": "YCB3-BING-20260720-01"})["timeline"]
		growing = [row for row in timeline if row["event_type"] == "Growing"]
		self.assertEqual(len(growing), 1)
		self.assertEqual(growing[0]["reference_doctype"], "Spray Application")

	def test_running_it_twice_writes_nothing_the_second_time(self):
		self.a_spray(blocks=(BLOCK3,))
		self.a_seal()
		self.a_ticket()
		first = self.sweep()
		second = self.sweep()
		self.assertGreater(first["counts"]["events_written"], 0)
		self.assertEqual(second["counts"]["events_written"], 0)
		self.assertEqual(second["counts"]["lots_created"], 0)
		self.assertEqual(second["counts"]["events_already_present"], first["counts"]["events_written"])

	def test_a_seal_with_no_block_is_counted_rather_than_dropped_silently(self):
		"""A lot code IS a block and a day. Those bins are traceable to a shift
		and no further, and the number is the honest report of it."""
		self.a_seal(field=None)
		data = self.sweep()
		self.assertEqual(data["skipped"]["bin_seals_without_a_block"], 1)
		self.assertEqual(data["counts"]["lots_created"], 0)

	def test_a_ticket_matching_no_lot_is_counted_and_told_how_to_close_it(self):
		self.a_ticket()
		data = self.sweep()
		self.assertEqual(data["skipped"]["scale_tickets_without_a_lot"], 1)
		self.assertTrue(any("widening the window" in note for note in data["notes"]))

	def test_a_ticket_whose_variety_differs_from_the_seals_crop_still_matches(self):
		"""A bin seal writes `crop` and a ticket writes `variety`, and the two
		are genuinely different words for overlapping things. Matching on block
		and day after the exact match fails is what stops a whole day's tickets
		falling on the floor over a vocabulary nobody agreed."""
		self.a_seal(crop="Cherry")
		self.a_ticket(variety="Bing")
		data = self.sweep()
		self.assertEqual(data["skipped"]["scale_tickets_without_a_lot"], 0)

	def test_it_says_plainly_that_it_does_not_index_trade_shipments(self):
		"""No silent cap. A shipment carries no lot column and guessing its lots
		off a date would put fruit on a truck it was never on."""
		data = self.sweep()
		self.assertTrue(any("Trade Shipments are NOT indexed" in note for note in data["notes"]))

	def test_a_sweep_with_no_window_is_refused(self):
		error = self.tool_error("index_lot_events", {"company": MAIN})
		self.assertIn("date_from and date_to are both required", error)


class TheOlderTraceToolsAreUntouched(LotTestCase):
	"""The whole feature is additive. These are the four assertions that say so."""

	def test_trace_forward_still_takes_a_block_and_not_a_lot_code(self):
		from erpnext_mcp import registry

		schema = registry.TOOLS["trace_forward"]["inputSchema"]["properties"]
		self.assertIn("block", schema)
		self.assertNotIn("lot_code", schema)

	def test_trace_backward_still_takes_a_shipment_and_not_a_lot_code(self):
		from erpnext_mcp import registry

		schema = registry.TOOLS["trace_backward"]["inputSchema"]["properties"]
		self.assertIn("shipment", schema)
		self.assertNotIn("lot_code", schema)

	def test_the_new_traces_are_separate_tools_with_their_own_switches(self):
		from erpnext_mcp import registry, settings

		for name in ("trace_lot_forward", "trace_lot_backward"):
			with self.subTest(tool=name):
				self.assertIn(name, registry.TOOLS)
				self.assertTrue(settings.tool_enabled(name))

	def test_this_app_still_installs_no_doc_events(self):
		"""The auto-indexing is a tool BECAUSE of this promise. If a later
		release hangs the CTE writer off a document hook, this is the test that
		says the promise was the thing that was broken."""
		from erpnext_mcp import hooks

		self.assertFalse(hasattr(hooks, "doc_events"))


class TheLotRegisterReads(LotTestCase):
	def test_the_register_narrows_by_block_variety_and_day(self):
		self.a_lot(field=BLOCK3, variety="Bing")
		self.a_lot(field=BLOCK4, variety="Rainier")
		data = self.tool_data("list_traceability_lots", {"field": BLOCK4, "company": MAIN})
		self.assertEqual(data["count"], 1)
		self.assertEqual(data["lots"][0]["variety"], "Rainier")

	def test_the_register_does_not_carry_events_and_says_why(self):
		self.a_lot()
		data = self.tool_data("list_traceability_lots", {"company": MAIN})
		self.assertNotIn("events", data["lots"][0])
		self.assertIn("get_traceability_lot has one lot in full", data["note"])

	def test_one_lot_comes_back_with_its_events_and_its_sources(self):
		third = self.a_lot(field=BLOCK3)["lot_code"]
		pallet = self.tool_data(
			"create_traceability_lot",
			{"variety": "Mixed", "harvest_date": "2026-07-22", "company": MAIN, "source_lots": [third]},
		)["lot_code"]
		data = self.tool_data("get_traceability_lot", {"lot_code": pallet})["lot"]
		self.assertEqual([row["source_lot"] for row in data["source_lots"]], [third])
		self.assertEqual(data["event_types"], ["Transforming"])

	def test_a_lot_code_is_matched_case_insensitively(self):
		"""It is read off a bin and typed down a telephone, and the version
		somebody types is whichever case their keyboard was in."""
		lot = self.a_lot()["lot_code"]
		data = self.tool_data("get_traceability_lot", {"lot_code": lot.lower()})
		self.assertEqual(data["lot"]["lot_code"], lot)

	def test_a_lot_nobody_has_created_refuses_and_says_where_the_register_is(self):
		error = self.tool_error("get_traceability_lot", {"lot_code": "NOT-A-LOT-01"})
		self.assertIn("list_traceability_lots", error)


class TheCodeGeneratorIsCalledDirectly(LotTestCase):
	"""Unit coverage for the two helpers the whole naming scheme rests on."""

	def test_initials_keep_the_part_people_say_out_loud(self):
		self.assertEqual(lots._initials("Yellow Camp Block 3 - MC"), "YCB3")
		self.assertEqual(lots._initials("Mill Creek Block 4"), "MCB4")

	def test_a_block_number_on_the_register_beats_the_derived_initials(self):
		STORE.seed("Field", [{"name": BLOCK3, "block_number": "YC3"}])
		self.assertEqual(lots._field_code(BLOCK3), "YC3")

	def test_a_lot_with_no_block_still_gets_a_code(self):
		self.assertEqual(lots._field_code(""), "LOT")

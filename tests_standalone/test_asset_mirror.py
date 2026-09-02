# SPDX-License-Identifier: MIT
"""v0.148.0 — the tag in the field and the asset on the books, made one machine.

WHAT THESE TESTS ARE REALLY ABOUT, in the order the module can hurt somebody.

ERPNEXT MUST NOT ALSO BE DEPRECIATING THE MIRROR. `sync` writes
`calculate_depreciation = 0` for exactly the reason `create_asset` does:
ERPNext's own daily job posts for every asset that has it set, and this app's
`run_depreciation_cycle` owns the schedule. If that line were removed, every
mirrored tractor would depreciate twice a month, silently, and nothing else in
this repository would notice. There is a test that reads the flag off the stored
Asset.

A COST BASIS NOBODY MEASURED MUST NEVER REACH THE BOOKS. The whole design turns
on refusing to invent `gross_purchase_amount` and `purchase_date`, so the
refusals are tested as carefully as the writes — including the case that looks
like an accident and is not: a `purchase_value` of exactly 0.0, which is what all
thirty-three valves on the real site carry, and which ERPNext itself throws on.

A FAILED MIRROR MUST NOT UNDO A REGISTRATION. Every refusal below is asserted
twice: the Asset was not written, AND the Asset Register row was.

MONEY IS WRITTEN ONCE. An edit to a tag's `purchase_value` must not restate
`gross_purchase_amount` on an Asset that already exists — the ledger has been
reconciled against that figure. This is a NEGATIVE control and it is the one
most likely to rot, because the obvious "keep the mirror in sync" refactor
breaks it and every other test here still passes.
"""

import unittest

from erpnext_mcp import asset_mirror, compliance_fields
from erpnext_mcp.tools import asset_tags

from .fixtures import MAIN, MAIN_ABBR, OTHER, OTHER_ABBR, V12TestCase
from .harness import STORE, add_field, frappe, register_doctype

ALL_ON = {
	"allow_list_assets": 1,
	"allow_get_asset_detail": 1,
	"allow_register_asset": 1,
	"allow_update_registered_asset": 1,
	"allow_retire_asset": 1,
	"allow_bulk_create_assets": 1,
}

#: The one ERPNext Location the fixture site has. `Asset.location` is `reqd` on
#: ERPNext v15 and the standalone double deliberately ships without the Location
#: doctype (see the comment in harness.py), so these tests register it the way
#: `test_employee` registers Address — per test, because the absence is a real
#: configuration that other tests are about.
YARD = "Home Ranch"


class MirrorTestCase(V12TestCase):
	"""The fixture site, the three Asset columns, and one Location to file under."""

	MIRROR_ON = True

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, mirror_assets_to_erpnext=1 if self.MIRROR_ON else 0, **ALL_ON)
		# The three columns `install_compliance_fields` puts on ERPNext's Asset.
		# Added rather than installed so these tests fail for their own reason
		# when the installer changes, and not for the installer's.
		add_field("Asset", "asset_register", "Link", options="Asset Register")
		add_field("Asset", "farm_asset_type", "Data")
		add_field("Asset", "asset_register_synced_at", "Datetime")
		self.a_location(YARD)

	def a_location(self, *names):
		register_doctype("Location", [{"fieldname": "name"}])
		STORE.seed("Location", [{"name": name} for name in names])

	def register(self, name="MC-Tractor-01", asset_type="Tractor", **kw):
		payload = {"name": name, "asset_type": asset_type, "company": MAIN, **kw}
		return self.tool_data("register_asset", payload)

	def costed(self, name="MC-Tractor-01", **kw):
		"""A registration carrying the two facts ERPNext demands."""
		payload = {"purchase_value": 42000, "acquired_on": "2026-03-01"}
		payload.update(kw)
		return self.register(name=name, **payload)

	def assets(self):
		return STORE.rows("Asset")

	def asset(self, name):
		"""The stored Asset row, straight out of the double.

		NOT `frappe.db.get_value(..., ["*"])`. The double answers a column list
		and does not expand a star, so every assertion below would have read a
		KeyError as a missing column.
		"""
		for row in STORE.rows("Asset"):
			if row.get("name") == name:
				return dict(row)
		raise AssertionError(f"no stored Asset called {name!r}: {[r.get('name') for r in self.assets()]}")


# ── the switch ──────────────────────────────────────────────────────────────
class TheSwitch(MirrorTestCase):
	MIRROR_ON = False

	def test_it_is_off_and_nothing_reaches_the_books(self):
		"""Every mutating capability in this app ships off and this is a stronger
		case than most: it writes into a doctype ERPNext owns, on a trigger a
		worker with a handset pulls."""
		data = self.costed()
		self.assertEqual(data["name"], "MC-Tractor-01")
		self.assertEqual(self.assets(), [])
		self.assertIsNone(data["erpnext_asset"])

	def test_the_answer_says_which_checkbox(self):
		"""A silent no-op is indistinguishable from a broken feature. The reply
		names the setting rather than leaving somebody to find it."""
		data = self.costed()
		self.assertIn("Mirror Field Assets into ERPNext Assets", data["erpnext_asset_note"])

	def test_the_registration_is_untouched_by_the_switch(self):
		self.costed()
		row = next(r for r in STORE.rows("Asset Register") if r["name"] == "MC-Tractor-01")
		self.assertEqual(row["asset_type"], "Tractor")
		self.assertEqual(float(row["purchase_value"]), 42000.0)


# ── what it writes ──────────────────────────────────────────────────────────
class WhatItWrites(MirrorTestCase):
	def test_a_costed_registration_reaches_the_books(self):
		data = self.costed()
		self.assertTrue(data["erpnext_asset"])
		self.assertTrue(data["erpnext_asset_created"])
		self.assertTrue(frappe.db.exists("Asset", data["erpnext_asset"]))

	def test_erpnexts_own_depreciation_is_switched_off(self):
		"""THE line. ERPNext's daily scheduler posts depreciation for every asset
		with `calculate_depreciation` set, and `run_depreciation_cycle` owns the
		schedule for anything this app creates. Both posting means depreciating
		twice, silently, monthly, and no other test would see it."""
		data = self.costed()
		self.assertEqual(int(self.asset(data["erpnext_asset"])["calculate_depreciation"] or 0), 0)

	def test_it_is_marked_an_existing_asset(self):
		"""ERPNext checks `gross_purchase_amount == purchase_amount` unless this
		is set, and a mirror is by definition a machine the farm already owns
		rather than one being booked off a purchase invoice."""
		data = self.costed()
		self.assertEqual(int(self.asset(data["erpnext_asset"])["is_existing_asset"] or 0), 1)

	def test_the_money_and_the_date_are_the_tags_own(self):
		data = self.costed()
		asset = self.asset(data["erpnext_asset"])
		self.assertEqual(float(asset["gross_purchase_amount"]), 42000.0)
		self.assertEqual(str(asset["purchase_date"]), "2026-03-01")

	def test_it_carries_the_link_back_to_the_tag(self):
		"""The one column here that is not derivable from somewhere else. Without
		it the same machine exists twice on one site and nothing can say so."""
		data = self.costed()
		self.assertEqual(self.asset(data["erpnext_asset"])["asset_register"], "MC-Tractor-01")

	def test_it_carries_the_farm_vocabulary_and_a_sync_stamp(self):
		data = self.costed()
		asset = self.asset(data["erpnext_asset"])
		self.assertEqual(asset["farm_asset_type"], "Tractor")
		self.assertTrue(asset["asset_register_synced_at"])

	def test_the_asset_name_leads_with_the_printed_tag(self):
		"""It is the string on the sticker and the string somebody standing at
		the machine will search the Asset list for."""
		data = self.costed(description="Kubota M7")
		self.assertTrue(self.asset(data["erpnext_asset"])["asset_name"].startswith("MC-Tractor-01"))
		self.assertIn("Kubota M7", self.asset(data["erpnext_asset"])["asset_name"])

	def test_it_is_filed_under_the_sites_location(self):
		data = self.costed()
		self.assertEqual(self.asset(data["erpnext_asset"])["location"], YARD)

	def test_it_is_a_draft(self):
		"""Same posture as `create_asset`: submitting an asset is somebody
		deciding the purchase is real, and that is not a field registration."""
		data = self.costed()
		self.assertEqual(int(self.asset(data["erpnext_asset"])["docstatus"] or 0), 0)


# ── the fixed-asset Item ────────────────────────────────────────────────────
class TheItem(MirrorTestCase):
	def test_one_item_is_created_per_asset_type(self):
		data = self.costed()
		self.assertEqual(self.asset(data["erpnext_asset"])["item_code"], "FARM-ASSET-TRACTOR")
		item = next(r for r in STORE.rows("Item") if r["name"] == "FARM-ASSET-TRACTOR")
		self.assertEqual(int(item["is_fixed_asset"]), 1)
		self.assertEqual(int(item["is_stock_item"]), 0)

	def test_a_second_machine_of_the_same_type_reuses_it(self):
		"""ERPNext wants an Item to say what KIND of thing this is. Forty valves
		are forty assets of one kind, not forty kinds."""
		self.costed(name="MC-Tractor-01")
		self.costed(name="MC-Tractor-02")
		items = [row["name"] for row in STORE.rows("Item") if row["name"].startswith("FARM-ASSET-")]
		self.assertEqual(items, ["FARM-ASSET-TRACTOR"])

	def test_an_existing_item_that_is_not_a_fixed_asset_is_refused_not_edited(self):
		"""Flipping `is_fixed_asset` on an item that may have stock movements is
		a change to the site's inventory, made from a handset."""
		STORE.seed(
			"Item",
			[{"name": "FARM-ASSET-SPRAYER", "item_code": "FARM-ASSET-SPRAYER", "is_fixed_asset": 0}],
		)
		data = self.costed(name="MC-Sprayer-01", asset_type="Sprayer")
		self.assertIsNone(data["erpnext_asset"])
		self.assertIn("not flagged as a fixed asset", data["erpnext_asset_note"])
		self.assertEqual(int(frappe.db.get_value("Item", "FARM-ASSET-SPRAYER", "is_fixed_asset") or 0), 0)
		self.assertTrue(frappe.db.exists("Asset Register", "MC-Sprayer-01"))


# ── the Asset Category ──────────────────────────────────────────────────────
class TheCategory(MirrorTestCase):
	def test_it_uses_the_category_for_the_type_when_the_site_has_it(self):
		STORE.seed("Asset Category", [{"name": "Tractor", "asset_category_name": "Tractor"}])
		data = self.costed()
		self.assertEqual(self.asset(data["erpnext_asset"])["asset_category"], "Tractor")

	def test_a_site_without_it_still_gets_the_asset(self):
		"""`asset_category` is optional on ERPNext's Asset and is only consulted
		when depreciation is calculated, which for a mirror it never is. Refusing
		over a master the site can create whenever it likes would be worse."""
		data = self.costed()
		self.assertTrue(data["erpnext_asset"])
		self.assertFalse(self.asset(data["erpnext_asset"]).get("asset_category"))

	def test_it_never_files_a_machine_under_a_category_meant_for_something_else(self):
		"""A wrong category names the wrong depreciation account. The fixture's
		only category is "Farm Equipment", which is not the one a tractor maps
		to, so nothing is chosen."""
		self.assertTrue(frappe.db.exists("Asset Category", "Farm Equipment"))
		data = self.costed()
		self.assertNotEqual(self.asset(data["erpnext_asset"]).get("asset_category"), "Farm Equipment")

	def test_a_block_is_not_filed_as_equipment(self):
		"""A block is planted ground: its establishment cost is capitalised
		against the planting. `CATEGORY_BY_TYPE` omits it deliberately."""
		STORE.seed(
			"Asset Category",
			[{"name": "Machinery & Equipment", "asset_category_name": "Machinery & Equipment"}],
		)
		data = self.costed(name="MC-Block-A", asset_type="Block")
		self.assertTrue(data["erpnext_asset"])
		self.assertFalse(self.asset(data["erpnext_asset"]).get("asset_category"))


# ── what it refuses, and what survives the refusal ──────────────────────────
class WhatItRefuses(MirrorTestCase):
	def test_a_tag_with_no_money_and_no_date_does_not_reach_the_books(self):
		data = self.register()
		self.assertIsNone(data["erpnext_asset"])
		self.assertEqual(self.assets(), [])

	def test_the_registration_survives_every_refusal(self):
		"""The tag is the record; the mirror is a second copy of it. Losing a
		tractor's registration because its Asset could not be built would be the
		wrong way round."""
		data = self.register()
		self.assertEqual(data["name"], "MC-Tractor-01")
		self.assertTrue(frappe.db.exists("Asset Register", "MC-Tractor-01"))

	def test_both_missing_facts_are_named_at_once(self):
		"""A caller told to supply a purchase value, who supplies it, and is then
		told about an acquisition date has been made to do the work twice."""
		note = self.register()["erpnext_asset_note"]
		self.assertIn("purchase_value", note)
		self.assertIn("acquired_on", note)

	def test_only_the_missing_one_is_named(self):
		note = self.register(purchase_value=42000)["erpnext_asset_note"]
		self.assertIn("acquired_on", note)
		self.assertNotIn("purchase_value", note)

	def test_a_purchase_value_of_zero_is_not_a_purchase_value(self):
		"""THE CASE THAT LOOKS LIKE AN ACCIDENT AND IS NOT. All thirty-three
		valves on the real site read exactly 0.0, and ERPNext itself throws
		`MandatoryError("Gross Purchase Amount is mandatory")` on it. Mirroring
		one would put a cost basis of nothing into the depreciation run."""
		data = self.register(purchase_value=0, acquired_on="2026-03-01")
		self.assertIsNone(data["erpnext_asset"])
		self.assertIn("purchase_value", data["erpnext_asset_note"])
		self.assertEqual(self.assets(), [])

	def test_the_note_says_how_to_fix_it(self):
		self.assertIn("update_registered_asset", self.register()["erpnext_asset_note"])


# ── the Location ERPNext demands ────────────────────────────────────────────
class TheLocation(MirrorTestCase):
	def test_two_locations_refuse_rather_than_guess(self):
		"""Filing a machine at whichever place sorted first is a wrong answer
		that looks like a right one."""
		self.a_location(YARD, "River Block Shop")
		data = self.costed()
		self.assertIsNone(data["erpnext_asset"])
		self.assertIn("asset_location", data["erpnext_asset_note"])
		self.assertIn("River Block Shop", data["erpnext_asset_note"])

	def test_naming_one_settles_it(self):
		self.a_location(YARD, "River Block Shop")
		data = self.costed(asset_location="River Block Shop")
		self.assertEqual(self.asset(data["erpnext_asset"])["location"], "River Block Shop")

	def test_a_location_that_does_not_exist_is_refused(self):
		data = self.costed(asset_location="Nowhere")
		self.assertIsNone(data["erpnext_asset"])
		self.assertIn("Nowhere", data["erpnext_asset_note"])
		self.assertTrue(frappe.db.exists("Asset Register", "MC-Tractor-01"))

	def test_a_retry_carrying_only_the_location_is_not_nothing_to_change(self):
		"""The refusal above names an argument that changes nothing on the
		register. Refusing the retry as a no-op would make the refusal
		unactionable through the door that raised it."""
		self.a_location(YARD, "River Block Shop")
		self.assertIsNone(self.costed()["erpnext_asset"])
		data = self.tool_data(
			"update_registered_asset",
			{"asset_name": "MC-Tractor-01", "asset_location": "River Block Shop"},
		)
		self.assertTrue(data["erpnext_asset"])

	def test_a_bare_update_with_nothing_at_all_is_still_refused(self):
		"""The negative control for the clause above: widening the gate by one
		argument must not have opened it."""
		self.costed()
		error = self.tool_error("update_registered_asset", {"asset_name": "MC-Tractor-01"})
		self.assertIn("nothing to change", error)


# ── one machine, one Asset ──────────────────────────────────────────────────
class ItDoesNotDuplicate(MirrorTestCase):
	def test_an_update_does_not_create_a_second_asset(self):
		first = self.costed()
		self.tool_data("update_registered_asset", {"asset_name": "MC-Tractor-01", "description": "Kubota M7"})
		self.assertEqual(len(self.assets()), 1)
		self.assertEqual(self.assets()[0]["name"], first["erpnext_asset"])

	def test_an_update_refreshes_identity(self):
		data = self.costed()
		self.tool_data("update_registered_asset", {"asset_name": "MC-Tractor-01", "asset_type": "Vehicle"})
		self.assertEqual(self.asset(data["erpnext_asset"])["farm_asset_type"], "Vehicle")

	def test_an_update_never_restates_the_money(self):
		"""THE NEGATIVE CONTROL, and the one most likely to rot. The obvious
		"keep the mirror in sync" refactor copies `purchase_value` onto
		`gross_purchase_amount` on every edit — and that figure is what the
		ledger has been reconciled against. Restating it from a handset moves a
		balance from a screen where nobody can see that is what they are doing.
		"""
		data = self.costed()
		self.tool_data("update_registered_asset", {"asset_name": "MC-Tractor-01", "purchase_value": 99000})
		self.assertEqual(float(self.asset(data["erpnext_asset"])["gross_purchase_amount"]), 42000.0)
		self.assertEqual(
			float(frappe.db.get_value("Asset Register", "MC-Tractor-01", "purchase_value")), 99000.0
		)

	def test_costing_a_tag_afterwards_is_what_puts_it_on_the_books(self):
		"""The real rollout: tag it in the field now, cost it at the desk later."""
		self.assertIsNone(self.register()["erpnext_asset"])
		data = self.tool_data(
			"update_registered_asset",
			{"asset_name": "MC-Tractor-01", "purchase_value": 42000, "acquired_on": "2026-03-01"},
		)
		self.assertTrue(data["erpnext_asset_created"])
		self.assertEqual(len(self.assets()), 1)


# ── retirement ──────────────────────────────────────────────────────────────
class Retiring(MirrorTestCase):
	def test_retiring_a_tag_does_not_dispose_of_the_asset(self):
		"""ERPNext disposes of an asset through a scrap or sale journal that
		posts to the general ledger. This writes a date on a register row. They
		are not the same act."""
		created = self.costed()
		self.tool_data("retire_asset", {"asset_name": "MC-Tractor-01", "reason": "sold"})
		asset = self.asset(created["erpnext_asset"])
		self.assertEqual(int(asset["docstatus"] or 0), 0)
		self.assertNotIn(str(asset.get("status") or ""), ("Scrapped", "Sold", "Cancelled"))

	def test_it_says_the_asset_is_still_on_the_books(self):
		created = self.costed()
		data = self.tool_data("retire_asset", {"asset_name": "MC-Tractor-01"})
		self.assertEqual(data["erpnext_asset"], created["erpnext_asset"])
		self.assertIn("UNCHANGED", data["erpnext_asset_note"])

	def test_a_tag_that_never_reached_the_books_retires_quietly(self):
		self.register()
		data = self.tool_data("retire_asset", {"asset_name": "MC-Tractor-01"})
		self.assertIsNone(data["erpnext_asset"])
		self.assertNotIn("erpnext_asset_note", data)


# ── the reads ───────────────────────────────────────────────────────────────
class TheReads(MirrorTestCase):
	def test_list_assets_says_which_are_on_the_books(self):
		self.costed(name="MC-Tractor-01")
		self.register(name="MC-Valve-05", asset_type="Irrigation Valve")
		data = self.tool_data("list_assets", {"company": MAIN})
		by_name = {row["name"]: row for row in data["assets"]}
		self.assertTrue(by_name["MC-Tractor-01"]["erpnext_asset"])
		self.assertIsNone(by_name["MC-Valve-05"]["erpnext_asset"])

	def test_list_assets_counts_the_gap(self):
		""" "How much of the tag register is on the books" is the question this
		feature exists to make answerable, and counting a column by hand across
		five hundred rows is not answering it."""
		self.costed(name="MC-Tractor-01")
		self.register(name="MC-Valve-05", asset_type="Irrigation Valve")
		data = self.tool_data("list_assets", {"company": MAIN})
		self.assertEqual(data["asset_count"], 2)
		self.assertEqual(data["on_the_books_count"], 1)

	def test_get_asset_detail_names_the_asset(self):
		created = self.costed()
		data = self.tool_data("get_asset_detail", {"asset_name": "MC-Tractor-01"})
		self.assertEqual(data["erpnext_asset"], created["erpnext_asset"])

	def test_a_tag_with_two_assets_reports_neither(self):
		"""Two Assets for one tag is two sets of books for one machine.
		Returning whichever came back first would make every later sync update an
		arbitrary half of them and leave the other drifting in silence."""
		created = self.costed()
		STORE.seed(
			"Asset",
			[{**self.asset(created["erpnext_asset"]), "name": "ACC-ASS-DUPLICATE"}],
		)
		data = self.tool_data("get_asset_detail", {"asset_name": "MC-Tractor-01"})
		self.assertIsNone(data["erpnext_asset"])

	def test_a_duplicate_is_reported_rather_than_added_to(self):
		created = self.costed()
		STORE.seed(
			"Asset",
			[{**self.asset(created["erpnext_asset"]), "name": "ACC-ASS-DUPLICATE"}],
		)
		data = self.tool_data("update_registered_asset", {"asset_name": "MC-Tractor-01", "description": "x"})
		self.assertIn("more sets of books than there are machines", data["erpnext_asset_note"])
		self.assertEqual(len(self.assets()), 2)


# ── the rollout path ────────────────────────────────────────────────────────
class BulkRollout(MirrorTestCase):
	def test_a_batch_carrying_values_reaches_the_books(self):
		data = self.tool_data(
			"bulk_create_assets",
			{
				"company": MAIN,
				"assets": [
					{
						"name": "MC-Wind-01",
						"asset_type": "Wind Machine",
						"purchase_value": 18000,
						"acquired_on": "2026-02-01",
					},
					{"name": "MC-Valve-05", "asset_type": "Irrigation Valve"},
				],
			},
		)
		self.assertEqual(data["created_count"], 2)
		self.assertEqual(data["mirrored_count"], 1)

	def test_the_shape_this_tool_has_always_taken_still_works(self):
		"""Both new keys are optional and both were absent before v0.148.0, so a
		caller sending the old shape gets exactly what it always got."""
		data = self.tool_data(
			"bulk_create_assets",
			{"company": MAIN, "assets": [{"name": "MC-Valve-05", "asset_type": "Irrigation Valve"}]},
		)
		self.assertEqual(data["created_count"], 1)
		self.assertEqual(data["mirrored_count"], 0)
		self.assertTrue(frappe.db.exists("Asset Register", "MC-Valve-05"))

	def test_one_mistyped_price_does_not_lose_the_batch(self):
		"""A rollout of five hundred valves should not be lost to one bad cell.
		The row is registered and it simply does not reach the books."""
		data = self.tool_data(
			"bulk_create_assets",
			{
				"company": MAIN,
				"assets": [
					{"name": "MC-Wind-01", "asset_type": "Wind Machine", "purchase_value": "not a number"},
					{
						"name": "MC-Wind-02",
						"asset_type": "Wind Machine",
						"purchase_value": 18000,
						"acquired_on": "2026-02-01",
					},
				],
			},
		)
		self.assertEqual(data["created_count"], 2)
		self.assertEqual(data["mirrored_count"], 1)


# ── the site that has not migrated ──────────────────────────────────────────
class BeforeTheColumnsExist(V12TestCase):
	"""A site with the switch on and `bench migrate` not yet run.

	Deliberately NOT a `MirrorTestCase`: the point is the absence of the columns
	that class adds, and inheriting them would make the test vacuous.
	"""

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, mirror_assets_to_erpnext=1, **ALL_ON)

	def test_it_refuses_and_names_the_migration(self):
		data = self.tool_data(
			"register_asset",
			{
				"name": "MC-Tractor-01",
				"asset_type": "Tractor",
				"company": MAIN,
				"purchase_value": 42000,
				"acquired_on": "2026-03-01",
			},
		)
		self.assertIsNone(data["erpnext_asset"])
		self.assertIn("bench", data["erpnext_asset_note"])
		self.assertEqual(STORE.rows("Asset"), [])

	def test_the_registration_still_happens(self):
		self.tool_data(
			"register_asset",
			{"name": "MC-Tractor-01", "asset_type": "Tractor", "company": MAIN, "purchase_value": 42000},
		)
		self.assertTrue(frappe.db.exists("Asset Register", "MC-Tractor-01"))


# ── scoping, and the cost center ERPNext insists on ─────────────────────────
class TheCompany(MirrorTestCase):
	def test_the_asset_belongs_to_the_tags_company(self):
		"""Not the session's, and not the first one the site has. An asset filed
		against the wrong entity is a depreciation line in the wrong book."""
		frappe.db.set_value("Company", OTHER, "cost_center", f"Main - {OTHER_ABBR}")
		data = self.costed(name="OTH-Tractor-01", company=OTHER)
		self.assertEqual(self.asset(data["erpnext_asset"])["company"], OTHER)

	def test_the_cost_center_is_the_companys_own(self):
		data = self.costed()
		self.assertEqual(self.asset(data["erpnext_asset"])["cost_center"], f"Main - {MAIN_ABBR}")

	def test_a_company_with_no_cost_center_refuses_and_says_which(self):
		"""ERPNext's `validate_cost_center` throws unless the asset or the company
		names one, and it throws from three layers below the argument that was
		wrong. Caught here, where the entity can be named."""
		data = self.costed(name="OTH-Tractor-01", company=OTHER)
		self.assertIsNone(data["erpnext_asset"])
		self.assertIn(OTHER, data["erpnext_asset_note"])
		self.assertIn("cost center", data["erpnext_asset_note"])
		self.assertTrue(frappe.db.exists("Asset Register", "OTH-Tractor-01"))


# ── the two halves cannot drift ─────────────────────────────────────────────
class TheColumnsAgree(unittest.TestCase):
	"""`asset_mirror` writes three columns and `compliance_fields` creates them.

	A column added to one and not the other announces itself in neither
	direction: the mirror writes into a field that does not exist and Frappe
	drops it, or the Desk grows a column nobody ever fills in. Both halves are a
	single change and this is what says so.

	Deliberately a plain TestCase. It compares two Python literals; standing up
	the whole fake site to do that would be pretence.
	"""

	def test_every_column_the_mirror_writes_is_one_the_installer_creates(self):
		target = next(t for t in compliance_fields.TARGETS if t.doctype == "Asset")
		declared = {spec.fieldname for spec in target.fields}
		for column in asset_mirror.CUSTOM_FIELDS:
			with self.subTest(column=column):
				self.assertIn(
					column,
					declared,
					f"asset_mirror writes Asset.{column} and compliance_fields does not "
					"create it. Frappe drops a write to a column that is not there.",
				)

	def test_the_link_points_at_the_register(self):
		"""A Link with the wrong `options` is a Link the Desk will not follow and
		a filter that matches nothing."""
		target = next(t for t in compliance_fields.TARGETS if t.doctype == "Asset")
		spec = next(s for s in target.fields if s.fieldname == asset_mirror.LINK_FIELD)
		self.assertEqual(spec.fieldtype, "Link")
		self.assertEqual(spec.options, "Asset Register")

	def test_all_three_are_read_only(self):
		"""They are written by the mirror and by nothing else. A denormalised copy
		a second person can type over is a copy that will one day lie."""
		target = next(t for t in compliance_fields.TARGETS if t.doctype == "Asset")
		for column in asset_mirror.CUSTOM_FIELDS:
			with self.subTest(column=column):
				spec = next(s for s in target.fields if s.fieldname == column)
				self.assertTrue(spec.read_only, f"Asset.{column} is editable in the Desk")

	def test_every_asset_type_maps_to_a_category_or_deliberately_to_none(self):
		"""`CATEGORY_BY_TYPE` is checked against the register's own Select rather
		than against a second hand-typed list. A type added to the doctype and not
		here mirrors with no category, which is the safe direction — but it should
		be a decision somebody made, and `Block` is the only one so far."""
		unmapped = set(asset_tags.ASSET_TYPES) - set(asset_mirror.CATEGORY_BY_TYPE)
		self.assertEqual(unmapped, {"Block"}, "an asset type gained or lost a category mapping")

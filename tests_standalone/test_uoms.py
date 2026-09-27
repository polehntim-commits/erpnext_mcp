# SPDX-License-Identifier: MIT
"""The unit register, and a label rate tied back to a real unit (v0.197.0).

The first class is the whole reason this release exists: OML App Feedback
AFB-2026-00023, PROWLER™ rodent bait refused over a unit called 'Noi' — the
first letters of "Norway rats", the TARGET of the rate, read as its unit.
"""

import frappe

from erpnext_mcp import ag_uom, agronomy_seed, compliance_fields, registry, uom_resolve
from erpnext_mcp.tools import masters

from .fixtures import MastersTestCase
from .harness import ROLES, STORE, set_roles

PROWLER_RATE = (
	"Norway rats: 1 or 2 blocks of bait; roof rats: 1 or 2 blocks of bait; house mice: 1 block of bait"
)
PROWLER_UPC = "048745228174"

READ_TOOLS = ("list_uoms", "get_uom", "resolve_uom")
WRITE_TOOLS = (
	"create_uom",
	"update_uom",
	"disable_uom",
	"set_uom_conversion_factor",
	"delete_uom_conversion_factor",
	"create_ag_uom_context",
	"update_ag_uom_context",
	"add_uom_to_context",
	"remove_uom_from_context",
)
ALL_ON = {f"allow_{name}": 1 for name in (*READ_TOOLS, *WRITE_TOOLS, "create_item", "update_item")}

#: The units the tests resolve against. ERPNext's own spellings sit beside this
#: app's seeded ones on a real site, so both are here.
UNITS = (
	("Nos", 1),
	("Block", 1),
	("Bin", 1),
	("Pound", 0),
	("Ounce", 0),
	("Gallon", 0),
	("Fluid Ounce", 0),
	("Pint, Liquid (US)", 0),
	("Acre", 0),
)


class UnitTestCase(MastersTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON)
		compliance_fields.install_compliance_fields(respect_switch=False)
		STORE.seed(
			"UOM",
			[
				{"name": name, "uom_name": name, "enabled": 1, "must_be_whole_number": whole}
				for name, whole in UNITS
				if not frappe.db.exists("UOM", name)
			],
		)
		saved = {user: list(held) for user, held in ROLES.items()}

		def restore():
			ROLES.clear()
			ROLES.update(saved)

		self.addCleanup(restore)

	def a_context(self, name="Bait", applies_to="Count", units=(("Block", 1),)):
		return self.tool_data(
			"create_ag_uom_context",
			{
				"context_name": name,
				"applies_to": applies_to,
				"uoms": [{"uom": uom, "is_default": default} for uom, default in units],
			},
		)


# ── the resolver ────────────────────────────────────────────────────────────
class TheRateReader(UnitTestCase):
	def test_the_unit_is_what_follows_the_quantity_never_the_pest_before_it(self):
		answer = uom_resolve.resolve_rate(PROWLER_RATE)
		self.assertEqual(answer["uom"], "Block")
		self.assertEqual(answer["phrase"], "blocks of bait")
		self.assertEqual(answer["quantity"], {"min": 1.0, "max": 2.0})
		self.assertEqual(answer["status"], "resolved")
		self.assertFalse(answer["mixed"])
		self.assertEqual([c["target"] for c in answer["clauses"]], ["Norway rats", "roof rats", "house mice"])
		for clause in answer["clauses"]:
			self.assertNotIn("Nor", clause["phrase"])

	def test_a_spray_rate_reads_its_numerator(self):
		answer = uom_resolve.resolve_rate("Cherries: 2.56-3.84 fl oz/acre (PHI 14 d)")
		self.assertEqual(answer["uom"], "Fluid Ounce")
		self.assertEqual(answer["quantity"], {"min": 2.56, "max": 3.84})

	def test_erpnexts_own_spelling_is_found_through_the_alias(self):
		self.assertEqual(uom_resolve.resolve_rate("2 pt/acre")["uom"], "Pint, Liquid (US)")
		self.assertEqual(uom_resolve.resolve_unit("lbs")["uom"], "Pound")
		self.assertEqual(uom_resolve.resolve_unit("Blocks")["matched_by"], "plural")

	def test_an_unknown_unit_is_an_answer_with_candidates_not_an_error(self):
		answer = uom_resolve.resolve_unit("Noi")
		self.assertIsNone(answer["uom"])
		self.assertEqual(answer["status"], "unresolved")
		self.assertIn("Nos", answer["candidates"])

	def test_a_disabled_unit_is_never_the_answer(self):
		STORE.get_raw("UOM", "Block")["enabled"] = 0
		self.assertIsNone(uom_resolve.resolve_rate(PROWLER_RATE)["uom"])

	def test_two_units_in_one_rate_are_flagged(self):
		# The masters fixture site calls its pound "Lb", and a site's own
		# spelling beats this app's alias for it.
		answer = uom_resolve.resolve_rate("Apples: 2 lb/acre; Cherries: 3 fl oz/acre")
		self.assertEqual(answer["uom"], "Lb")
		self.assertTrue(answer["mixed"])

	def test_the_tool_answers_the_same_shape(self):
		data = self.tool_data("resolve_uom", {"text": PROWLER_RATE})
		self.assertEqual(data["uom"], "Block")
		data = self.tool_data("resolve_uom", {"text": "Noi"})
		self.assertEqual(data["status"], "unresolved")


# ── AFB-2026-00023, end to end ──────────────────────────────────────────────
class ProwlerIsRegistered(UnitTestCase):
	def register(self, **extra):
		return self.tool_data(
			"create_item",
			{
				"item_code": "PROWLER",
				"item_name": "PROWLER™",
				"barcode": PROWLER_UPC,
				"application_rate": PROWLER_RATE,
				**extra,
			},
		)

	def test_the_rate_unit_is_block_and_the_product_is_stocked_in_blocks(self):
		data = self.register()
		self.assertEqual(data["application_rate_uom"], "Block")
		self.assertEqual(data["stock_uom"], "Block", "a whole-number rate unit is the stock unit")
		self.assertEqual(data["stock_uom_resolution"]["status"], "defaulted")
		self.assertEqual(data["needs_review"], [])
		row = STORE.get_raw("Item", "PROWLER")
		self.assertEqual(row["application_rate_uom"], "Block")
		self.assertEqual(row["stock_uom"], "Block")

	def test_the_noi_that_was_refused_now_registers_and_is_flagged(self):
		data = self.register(stock_uom="Noi")
		self.assertEqual(data["stock_uom"], "Block")
		self.assertEqual(data["stock_uom_resolution"]["status"], "fallback")
		self.assertEqual(data["needs_review"], ["stock_uom"])
		self.assertIsNotNone(STORE.get_raw("Item", "PROWLER"))

	def test_a_site_without_block_still_registers_it_and_asks_for_the_unit(self):
		STORE.get_raw("UOM", "Block")["enabled"] = 0
		data = self.register()
		self.assertIsNone(data["application_rate_uom"])
		self.assertEqual(data["rate_uom"]["status"], "unresolved")
		self.assertEqual(data["stock_uom"], "Nos")
		self.assertEqual(data["needs_review"], ["application_rate_uom"])

	def test_the_review_is_answered_with_update_item(self):
		STORE.get_raw("UOM", "Block")["enabled"] = 0
		self.register()
		STORE.get_raw("UOM", "Block")["enabled"] = 1
		data = self.tool_data(
			"update_item", {"item_code": "PROWLER", "application_rate_uom": "blocks", "stock_uom": "Block"}
		)
		self.assertEqual(data["changed"]["application_rate_uom"], [None, "Block"])
		self.assertEqual(data["changed"]["stock_uom"], ["Nos", "Block"])

	def test_update_item_refuses_a_picked_unit_the_site_lacks(self):
		self.register()
		message = self.tool_error("update_item", {"item_code": "PROWLER", "application_rate_uom": "Noi"})
		self.assertIn("Did you mean", message)
		self.assertIn("Nothing was changed", message)

	def test_the_stock_unit_is_fixed_after_the_first_transaction(self):
		self.register()
		STORE.seed("Stock Ledger Entry", [{"name": "SLE-1", "item_code": "PROWLER", "actual_qty": 4}])
		message = self.tool_error("update_item", {"item_code": "PROWLER", "stock_uom": "Nos"})
		self.assertIn("already has stock transactions", message)


# ── the register ────────────────────────────────────────────────────────────
class TheRegister(UnitTestCase):
	def test_reads_are_on_and_writes_are_off_out_of_the_box(self):
		self.configure(enabled=1)
		for name in READ_TOOLS:
			self.assertFalse(registry.TOOLS[name]["mutating"], name)
		for name in WRITE_TOOLS:
			self.assertTrue(registry.TOOLS[name]["mutating"], name)
			self.assertIn("MUTATING", registry.TOOLS[name]["description"])
			self.assertIn("switched off", self.tool_error(name, {}))

	def test_lists_with_flags_measure_and_contexts(self):
		self.a_context()
		data = self.tool_data("list_uoms", {"must_be_whole_number": True})
		by_name = {row["name"]: row for row in data["uoms"]}
		self.assertIn("Block", by_name)
		self.assertNotIn("Pound", by_name)
		self.assertEqual(by_name["Block"]["measures"], "Count")
		self.assertEqual(by_name["Block"]["contexts"], ["Bait"])
		data = self.tool_data("list_uoms", {"context": "Bait"})
		self.assertEqual([row["name"] for row in data["uoms"]], ["Block"])

	def test_get_answers_for_a_label_spelling(self):
		data = self.tool_data("get_uom", {"uom": "pounds"})
		self.assertEqual(data["name"], "Pound")
		self.assertEqual(data["measures"], "Weight")

	def test_creates_a_whole_number_unit(self):
		data = self.tool_data("create_uom", {"uom_name": "Station", "must_be_whole_number": True})
		self.assertEqual(data["name"], "Station")
		self.assertEqual(STORE.get_raw("UOM", "Station")["must_be_whole_number"], 1)

	def test_refuses_a_second_spelling_of_a_unit_the_site_has(self):
		self.assertIn("already exists", self.tool_error("create_uom", {"uom_name": "block"}))
		message = self.tool_error("create_uom", {"uom_name": "Blocks"})
		self.assertIn("already means Block", message)
		self.assertIn("Nothing was created", message)

	def test_the_role_gate_names_the_account_and_the_roles(self):
		set_roles("Administrator", ["Accounts User"])
		message = self.tool_error("create_uom", {"uom_name": "Station"})
		self.assertIn("Farm Manager", message)
		self.assertIn("mcp_system_user", message)
		self.assertIsNone(STORE.get_raw("UOM", "Station"))
		set_roles("Administrator", ["Farm Manager"])
		self.tool_data("create_uom", {"uom_name": "Station"})

	def test_update_changes_flags_and_never_renames(self):
		data = self.tool_data("update_uom", {"uom": "Pound", "must_be_whole_number": True})
		self.assertEqual(data["changed"], {"must_be_whole_number": [False, True]})
		message = self.tool_error("update_uom", {"uom": "Pound", "uom_name": "Lb"})
		self.assertIn("disable_uom", message)

	def test_disable_is_refused_while_an_active_context_offers_the_unit(self):
		self.a_context()
		message = self.tool_error("disable_uom", {"uom": "Block"})
		self.assertIn("Bait", message)
		self.tool_data("update_ag_uom_context", {"context": "Bait", "is_active": False})
		data = self.tool_data("disable_uom", {"uom": "Block"})
		self.assertFalse(data["already_disabled"])
		self.assertEqual(STORE.get_raw("UOM", "Block")["enabled"], 0)


class ConversionFactors(UnitTestCase):
	def test_records_updates_and_deletes_a_factor(self):
		data = self.tool_data(
			"set_uom_conversion_factor",
			{"from_uom": "Pound", "to_uom": "Ounce", "value": 16, "category": "Mass"},
		)
		self.assertEqual(data["category"], "Mass")
		self.assertTrue(data["category_created"])
		data = self.tool_data(
			"set_uom_conversion_factor", {"from_uom": "Pound", "to_uom": "Ounce", "value": 16.0}
		)
		self.assertEqual(data["changed"], {})
		data = self.tool_data("get_uom", {"uom": "Ounce"})
		self.assertEqual(data["conversion_factors"][0]["value"], 16.0)
		self.tool_data("delete_uom_conversion_factor", {"from_uom": "Pound", "to_uom": "Ounce"})
		self.assertEqual(STORE.rows("UOM Conversion Factor"), [])

	def test_a_reverse_row_that_disagrees_is_refused(self):
		self.tool_data(
			"set_uom_conversion_factor",
			{"from_uom": "Pound", "to_uom": "Ounce", "value": 16, "category": "Mass"},
		)
		message = self.tool_error(
			"set_uom_conversion_factor", {"from_uom": "Ounce", "to_uom": "Pound", "value": 16}
		)
		self.assertIn("either way", message)
		data = self.tool_data(
			"set_uom_conversion_factor", {"from_uom": "Ounce", "to_uom": "Pound", "value": 0.0625}
		)
		self.assertEqual(data["already_recorded"], "reversed")

	def test_a_new_pair_with_no_category_to_inherit_asks_for_one(self):
		message = self.tool_error(
			"set_uom_conversion_factor", {"from_uom": "Gallon", "to_uom": "Fluid Ounce", "value": 128}
		)
		self.assertIn("Pass category", message)

	def test_deleting_names_the_direction_it_was_recorded_in(self):
		self.tool_data(
			"set_uom_conversion_factor",
			{"from_uom": "Pound", "to_uom": "Ounce", "value": 16, "category": "Mass"},
		)
		message = self.tool_error("delete_uom_conversion_factor", {"from_uom": "Ounce", "to_uom": "Pound"})
		self.assertIn("other way round", message)


class Contexts(UnitTestCase):
	def test_creates_bait_and_adds_and_removes_units(self):
		data = self.a_context()
		self.assertEqual(data["default_uom"], "Block")
		self.tool_data("create_uom", {"uom_name": "Station", "must_be_whole_number": True})
		data = self.tool_data("add_uom_to_context", {"context": "Bait", "uom": "Station", "is_default": True})
		self.assertEqual(data["valid_uoms"], ["Block", "Station"])
		self.assertEqual(data["default_uom"], "Station")
		data = self.tool_data("remove_uom_from_context", {"context": "Bait", "uom": "Station"})
		self.assertEqual(data["valid_uoms"], ["Block"])

	def test_a_weight_is_refused_in_a_count_context(self):
		self.a_context()
		message = self.tool_error("add_uom_to_context", {"context": "Bait", "uom": "Pound"})
		self.assertIn("Pound measures Weight", message)

	def test_the_last_unit_is_not_removed(self):
		self.a_context()
		message = self.tool_error("remove_uom_from_context", {"context": "Bait", "uom": "Block"})
		self.assertIn("switch the context off", message.lower())

	def test_the_default_must_be_one_of_its_units(self):
		self.a_context(name="Dry Product", applies_to="Weight", units=(("Pound", 1), ("Ounce", 0)))
		data = self.tool_data("update_ag_uom_context", {"context": "Dry Product", "default_uom": "Ounce"})
		self.assertEqual(data["default_uom"], "Ounce")
		self.assertIn(
			"not one of",
			self.tool_error("update_ag_uom_context", {"context": "Dry Product", "default_uom": "Block"}),
		)


class TheSeed(UnitTestCase):
	def test_block_and_ounce_are_seeded_with_their_contexts(self):
		STORE.rows("UOM").clear()
		agronomy_seed.seed_agricultural_masters()
		self.assertEqual(STORE.get_raw("UOM", "Block")["must_be_whole_number"], 1)
		self.assertIsNotNone(STORE.get_raw("UOM", "Ounce"))
		self.assertIsNotNone(STORE.get_raw("Agricultural UOM Context", "Bait"))
		self.assertIsNotNone(STORE.get_raw("Agricultural UOM Context", "Dry Product"))
		self.assertEqual(ag_uom.dimension_of("Block"), "Count")

	def test_the_rate_field_is_the_one_the_installer_adds(self):
		self.assertEqual(masters.RATE_UOM_FIELD, "application_rate_uom")

# SPDX-License-Identifier: MIT
"""Items, item groups, suppliers, customers, warehouses and prices.

The tests worth reading first are the three `company` classes. `company` means a
different thing on each of these doctypes, and the whole of what this module can
get quietly wrong is answering as if it meant the same one everywhere.
"""

import json
from typing import ClassVar

import frappe

from erpnext_mcp import compat, compliance_fields, registry
from erpnext_mcp.tools import masters

from .fixtures import (
	CHEMICALS,
	CONSUMABLES,
	ITEM_GROUP_ROOT,
	MAIN,
	MAIN_ABBR,
	MAIN_WAREHOUSE_ROOT,
	MASTER_CUSTOMER,
	MASTER_SUPPLIER,
	OTHER,
	OTHER_ABBR,
	OTHER_WAREHOUSE_ROOT,
	RETIRED_ITEM,
	RETIRED_SUPPLIER,
	SPRAY,
	STANDARD_BUYING,
	STANDARD_SELLING,
	STORES,
	TWINE,
	MastersTestCase,
)
from .harness import META, STORE, register_doctype

#: Every switch this release added, so the "on by default" posture is asserted
#: once against the registry rather than remembered per test.
READ_TOOLS = (
	"list_items",
	"get_item",
	"list_item_groups",
	"list_suppliers",
	"get_supplier",
	"list_customers",
	"get_customer",
	"list_warehouses",
	"list_price_lists",
	"get_item_price",
)
WRITE_TOOLS = (
	"create_item",
	"update_item",
	"create_item_group",
	"create_supplier",
	"update_supplier",
	"create_customer",
	"update_customer",
	"create_warehouse",
	"set_item_price",
)

ALL = READ_TOOLS + WRITE_TOOLS

#: Write tools are off out of the box, so every mutating test has to turn its
#: own on. Kept as one dict rather than per-test kwargs.
WRITES_ON = {f"allow_{name}": 1 for name in WRITE_TOOLS}


class ItemGroups(MastersTestCase):
	def test_lists_the_tree_flat_with_each_parent(self):
		data = self.tool_data("list_item_groups")
		by_name = {row["name"]: row for row in data["item_groups"]}
		self.assertEqual(by_name[CONSUMABLES]["parent_item_group"], ITEM_GROUP_ROOT)
		self.assertEqual(data["roots"], [ITEM_GROUP_ROOT])

	def test_is_group_comes_back_as_a_boolean_not_a_string(self):
		"""A Check read as `bool("0")` is True, which would make every leaf a
		branch — and a branch is what create_item_group demands as a parent."""
		data = self.tool_data("list_item_groups")
		by_name = {row["name"]: row for row in data["item_groups"]}
		self.assertIs(by_name[CONSUMABLES]["is_group"], False)
		self.assertIs(by_name[ITEM_GROUP_ROOT]["is_group"], True)

	def test_filters_to_the_children_of_one_group(self):
		data = self.tool_data("list_item_groups", {"parent_item_group": ITEM_GROUP_ROOT})
		self.assertEqual([row["name"] for row in data["item_groups"]], [CONSUMABLES])

	def test_an_unknown_parent_is_refused_rather_than_returning_nothing(self):
		message = self.tool_error("list_item_groups", {"parent_item_group": "Nope"})
		self.assertIn("no Item Group called 'Nope'", message)

	def test_creates_a_group_under_the_root(self):
		self.configure(enabled=1, **WRITES_ON)
		data = self.tool_data("create_item_group", {"item_group_name": CHEMICALS})
		self.assertEqual(data["name"], CHEMICALS)
		self.assertEqual(data["parent_item_group"], ITEM_GROUP_ROOT)
		self.assertIs(data["is_group"], False)
		self.assertEqual(STORE.rows("Item Group")[-1]["item_group_name"], CHEMICALS)

	def test_a_leaf_cannot_be_a_parent(self):
		"""ERPNext would raise from inside the insert; this refuses first, and
		says which node it was and what to pass instead."""
		self.configure(enabled=1, **WRITES_ON)
		message = self.tool_error(
			"create_item_group",
			{"item_group_name": CHEMICALS, "parent_item_group": CONSUMABLES},
		)
		self.assertIn("is a leaf, not a group", message)
		self.assertIn("Nothing was created", message)

	def test_a_duplicate_name_is_refused_because_the_name_is_the_docname(self):
		self.configure(enabled=1, **WRITES_ON)
		message = self.tool_error("create_item_group", {"item_group_name": CONSUMABLES})
		self.assertIn("already exists", message)

	def test_an_unknown_parent_is_refused_with_the_sites_own_group_nodes(self):
		self.configure(enabled=1, **WRITES_ON)
		message = self.tool_error(
			"create_item_group", {"item_group_name": CHEMICALS, "parent_item_group": "Imaginary"}
		)
		self.assertIn(ITEM_GROUP_ROOT, message)


class Items(MastersTestCase):
	def test_lists_items_with_the_fields_the_brief_asked_for(self):
		data = self.tool_data("list_items")
		row = next(item for item in data["items"] if item["item_code"] == SPRAY)
		self.assertEqual(row["item_name"], "Surround WP")
		self.assertEqual(row["item_group"], CONSUMABLES)
		self.assertEqual(row["stock_uom"], "Lb")
		self.assertIs(row["is_stock_item"], True)
		self.assertIs(row["disabled"], False)

	def test_counts_by_item_group(self):
		data = self.tool_data("list_items")
		self.assertEqual(data["by_item_group"], {CONSUMABLES: 3})

	def test_filters_by_disabled(self):
		data = self.tool_data("list_items", {"disabled": True})
		self.assertEqual([row["item_code"] for row in data["items"]], [RETIRED_ITEM])

	def test_filters_by_item_group(self):
		data = self.tool_data("list_items", {"item_group": CONSUMABLES})
		self.assertEqual(data["count"], 3)

	def test_an_unknown_item_group_is_refused(self):
		message = self.tool_error("list_items", {"item_group": "Nope"})
		self.assertIn("no Item Group called 'Nope'", message)

	def test_searches_on_the_item_name(self):
		data = self.tool_data("list_items", {"search": "surround"})
		self.assertEqual([row["item_code"] for row in data["items"]], [SPRAY])

	def test_truncation_is_reported(self):
		data = self.tool_data("list_items", {"limit": 1})
		self.assertEqual(data["count"], 1)
		self.assertTrue(data["truncated"])

	def test_gets_one_item_with_its_per_company_defaults(self):
		data = self.tool_data("get_item", {"item_code": SPRAY})
		self.assertEqual(data["description"], "Kaolin clay particle film")
		self.assertEqual(
			data["item_defaults"],
			[
				{
					"company": MAIN,
					"default_warehouse": STORES,
					"default_price_list": None,
					"buying_cost_center": None,
					"selling_cost_center": None,
					"expense_account": None,
					"income_account": None,
				}
			],
		)

	def test_the_top_level_default_warehouse_is_filled_from_a_single_row(self):
		"""A site with the child table has no flat field; the convenience key is
		filled only when there is exactly one row, and says so."""
		data = self.tool_data("get_item", {"item_code": SPRAY})
		self.assertEqual(data["default_warehouse"], STORES)
		self.assertIn("item_defaults child table", data["default_warehouse_note"])

	def test_an_item_with_no_defaults_reports_no_default_warehouse(self):
		data = self.tool_data("get_item", {"item_code": TWINE})
		self.assertIsNone(data["default_warehouse"])
		self.assertEqual(data["item_defaults"], [])

	def test_an_item_can_be_fetched_by_its_display_name(self):
		data = self.tool_data("get_item", {"item_code": "Surround WP"})
		self.assertEqual(data["item_code"], SPRAY)

	def test_an_unknown_item_names_the_tool_that_finds_one(self):
		message = self.tool_error("get_item", {"item_code": "NOPE"})
		self.assertIn("list_items", message)


class ItemCompanyScope(MastersTestCase):
	"""`company` on an Item is not what `company` on a Warehouse is."""

	def test_the_company_filter_is_the_item_default_rows(self):
		data = self.tool_data("list_items", {"company": MAIN})
		self.assertEqual([row["item_code"] for row in data["items"]], [SPRAY])

	def test_and_the_response_says_what_it_hid(self):
		"""The dangerous half. An item with no default row is usable by every
		company and is NOT in this list; a caller who reads the count as "the
		items this company has" is wrong, so the tool says so."""
		data = self.tool_data("list_items", {"company": MAIN})
		self.assertIn("is NOT company-scoped", data["company_scope"])
		self.assertIn("Omit company", data["company_scope"])

	def test_a_company_with_no_item_defaults_gets_nothing_not_everything(self):
		"""The empty-list trap: a filter built from an empty set must match
		nothing. Falling through to "no filter" would answer a question about
		OTHER with MAIN's catalogue."""
		data = self.tool_data("list_items", {"company": OTHER})
		self.assertEqual(data["count"], 0)

	def test_no_company_means_every_item(self):
		data = self.tool_data("list_items")
		self.assertEqual(data["count"], 3)
		self.assertNotIn("company_scope", data)


class CreateItem(MastersTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **WRITES_ON)

	def test_creates_an_item_in_a_group_with_a_unit(self):
		data = self.tool_data(
			"create_item",
			{
				"item_code": "SURROUND-2",
				"item_name": "Surround WP 25lb",
				"item_group": CONSUMABLES,
				"stock_uom": "Lb",
				"description": "Kaolin",
			},
		)
		self.assertEqual(data["name"], "SURROUND-2")
		self.assertEqual(data["stock_uom"], "Lb")
		self.assertIs(data["is_stock_item"], True)
		row = STORE.get_raw("Item", "SURROUND-2")
		self.assertEqual(row["item_group"], CONSUMABLES)
		self.assertEqual(row["description"], "Kaolin")

	def test_the_item_code_becomes_the_docname(self):
		self.tool_data("create_item", {"item_code": "FUEL-DIESEL"})
		self.assertIsNotNone(STORE.get_raw("Item", "FUEL-DIESEL"))

	def test_it_says_plainly_that_there_is_no_draft(self):
		"""The brief asked for "creates as draft". An ERPNext Item has no
		docstatus, so promising one would be a lie a caller would act on."""
		data = self.tool_data("create_item", {"item_code": "FUEL-DIESEL"})
		self.assertIs(data["submittable"], False)
		self.assertIn("no docstatus", data["note"])

	def test_disabled_is_the_nearest_thing_to_a_draft_and_it_works(self):
		self.tool_data("create_item", {"item_code": "FUEL-DIESEL", "disabled": True})
		self.assertEqual(STORE.get_raw("Item", "FUEL-DIESEL")["disabled"], 1)

	def test_the_stock_uom_defaults_to_nos(self):
		data = self.tool_data("create_item", {"item_code": "FUEL-DIESEL"})
		self.assertEqual(data["stock_uom"], "Nos")

	def test_an_unknown_unit_is_refused_with_the_units_the_site_has(self):
		message = self.tool_error("create_item", {"item_code": "X", "stock_uom": "Furlong"})
		self.assertIn("no UOM called 'Furlong'", message)
		self.assertIn("Lb", message)
		self.assertIn("Nothing was created", message)

	def test_a_duplicate_item_code_is_refused(self):
		message = self.tool_error("create_item", {"item_code": SPRAY})
		self.assertIn("already exists", message)

	def test_an_unknown_item_group_is_refused_and_names_the_maker(self):
		message = self.tool_error("create_item", {"item_code": "X", "item_group": "Imaginary"})
		self.assertIn("create_item_group", message)

	def test_a_default_warehouse_lands_on_an_item_default_row(self):
		data = self.tool_data("create_item", {"item_code": "FUEL-DIESEL", "default_warehouse": STORES})
		self.assertEqual(data["default_warehouse_stored_on"], f"Item Default row for {MAIN}")
		row = STORE.get_raw("Item", "FUEL-DIESEL")
		self.assertEqual(row["item_defaults"][0]["company"], MAIN)
		self.assertEqual(row["item_defaults"][0]["default_warehouse"], STORES)

	def test_the_company_is_inferred_from_the_warehouse(self):
		"""A two-company fixture, so this cannot pass by `resolve_company`
		guessing: the warehouse names the company, and that is where it comes
		from."""
		self.tool_data("create_item", {"item_code": "FUEL-DIESEL", "default_warehouse": STORES})
		self.assertEqual(STORE.get_raw("Item", "FUEL-DIESEL")["item_defaults"][0]["company"], MAIN)

	def test_a_warehouse_from_another_company_is_refused(self):
		message = self.tool_error(
			"create_item",
			{"item_code": "X", "default_warehouse": OTHER_WAREHOUSE_ROOT, "company": MAIN},
		)
		self.assertIn("belongs to company", message)

	def test_an_unknown_warehouse_is_refused(self):
		message = self.tool_error("create_item", {"item_code": "X", "default_warehouse": "Shed"})
		self.assertIn("no Warehouse called 'Shed'", message)

	def test_it_is_off_by_default(self):
		self.configure(enabled=1)
		message = self.tool_error("create_item", {"item_code": "X"})
		self.assertIn("create_item", message)


class UpdateItem(MastersTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **WRITES_ON)

	def test_changes_the_description_and_reports_before_and_after(self):
		data = self.tool_data("update_item", {"item_code": SPRAY, "description": "Kaolin clay, OMRI listed"})
		self.assertEqual(
			data["changed"]["description"],
			["Kaolin clay particle film", "Kaolin clay, OMRI listed"],
		)
		self.assertEqual(STORE.get_raw("Item", SPRAY)["description"], "Kaolin clay, OMRI listed")

	def test_disables_an_item(self):
		data = self.tool_data("update_item", {"item_code": SPRAY, "disabled": True})
		self.assertEqual(data["changed"]["disabled"], [False, True])
		self.assertEqual(STORE.get_raw("Item", SPRAY)["disabled"], 1)

	def test_moves_an_item_to_another_group(self):
		self.tool_data("create_item_group", {"item_group_name": CHEMICALS})
		self.tool_data("update_item", {"item_code": SPRAY, "item_group": CHEMICALS})
		self.assertEqual(STORE.get_raw("Item", SPRAY)["item_group"], CHEMICALS)

	def test_an_unknown_group_changes_nothing(self):
		message = self.tool_error("update_item", {"item_code": SPRAY, "item_group": "Nope"})
		self.assertIn("Nothing was changed", message)
		self.assertEqual(STORE.get_raw("Item", SPRAY)["item_group"], CONSUMABLES)

	def test_nothing_to_change_is_refused_with_the_list_of_what_it_takes(self):
		message = self.tool_error("update_item", {"item_code": SPRAY})
		self.assertIn("nothing to change", message)
		self.assertIn("reorder_level", message)

	def test_a_value_that_already_matches_is_reported_rather_than_saved(self):
		data = self.tool_data("update_item", {"item_code": SPRAY, "item_group": CONSUMABLES})
		self.assertEqual(data["changed"], {})

	def test_a_reorder_rule_lands_on_the_items_default_warehouse(self):
		data = self.tool_data("update_item", {"item_code": SPRAY, "reorder_level": 50, "reorder_qty": 200})
		self.assertEqual(data["reorder"]["warehouse"], STORES)
		self.assertIs(data["reorder"]["created"], True)
		row = STORE.get_raw("Item", SPRAY)["reorder_levels"][0]
		self.assertEqual(row["warehouse_reorder_level"], 50.0)
		self.assertEqual(row["warehouse_reorder_qty"], 200.0)
		self.assertEqual(row["material_request_type"], "Purchase")

	def test_a_second_reorder_write_updates_the_row_rather_than_adding_one(self):
		self.tool_data("update_item", {"item_code": SPRAY, "reorder_level": 50})
		data = self.tool_data("update_item", {"item_code": SPRAY, "reorder_level": 75})
		self.assertIs(data["reorder"]["created"], False)
		self.assertEqual(len(STORE.get_raw("Item", SPRAY)["reorder_levels"]), 1)
		self.assertEqual(STORE.get_raw("Item", SPRAY)["reorder_levels"][0]["warehouse_reorder_level"], 75.0)

	def test_a_reorder_rule_with_no_warehouse_anywhere_is_refused(self):
		"""TWINE has no default warehouse, and ERPNext keys the row by one. The
		refusal names the argument to pass rather than picking a shed."""
		message = self.tool_error("update_item", {"item_code": TWINE, "reorder_level": 10})
		self.assertIn("reorder_warehouse", message)
		self.assertIn("Nothing was changed", message)

	def test_an_explicit_reorder_warehouse_is_used(self):
		data = self.tool_data(
			"update_item", {"item_code": TWINE, "reorder_level": 10, "reorder_warehouse": STORES}
		)
		self.assertEqual(data["reorder"]["warehouse"], STORES)


class PesticideLabelFields(MastersTestCase):
	"""create_item / update_item and the label columns `compliance_fields` installs.

	The columns go on through the real installer rather than onto the fixture by
	hand, because the handler refuses a column `compat.has_field` cannot see and
	that refusal is the behaviour a half-migrated site gets.
	"""

	LABEL: ClassVar[dict] = {
		"epa_registration_number": "100-1234",
		"signal_word": "warning",
		"restricted_use": True,
		"active_ingredients": [{"name": "Lambda-cyhalothrin", "concentration": 22.8, "unit": "%"}],
		"rei_hours": 24,
		"phi_days": 14,
		"phi_crop": "Cherries",
		"application_rate": "2.56-3.84 fl oz/acre",
		"ppe_requirements": "Coveralls, chemical-resistant gloves",
	}

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **WRITES_ON)
		compliance_fields.install_compliance_fields(respect_switch=False)

	def test_the_handler_and_the_schema_name_the_same_fields(self):
		for tool in ("create_item", "update_item"):
			properties = registry.TOOLS[tool]["inputSchema"]["properties"]
			self.assertLessEqual(set(masters.PESTICIDE_FIELDS), set(properties), tool)
		installed = {field.fieldname for field in compliance_fields._ITEM_FIELDS}
		self.assertEqual(set(masters.PESTICIDE_FIELDS), installed)

	def test_restricted_use_is_installed_on_item(self):
		self.assertTrue(compat.has_field("Item", "restricted_use"))

	def test_create_stores_every_label_field(self):
		data = self.tool_data(
			"create_item", {"item_code": "WARRIOR-II", "item_group": CONSUMABLES, **self.LABEL}
		)
		row = STORE.get_raw("Item", "WARRIOR-II")
		self.assertEqual(row["epa_registration_number"], "100-1234")
		self.assertEqual(row["signal_word"], "Warning")
		self.assertEqual(row["restricted_use"], 1)
		self.assertEqual(row["rei_hours"], 24)
		self.assertEqual(row["phi_days"], 14)
		self.assertEqual(row["phi_crop"], "Cherries")
		self.assertEqual(
			json.loads(row["active_ingredients"]),
			[{"name": "Lambda-cyhalothrin", "concentration": 22.8, "unit": "%"}],
		)
		self.assertEqual(data["pesticide_label"]["active_ingredients"][0]["name"], "Lambda-cyhalothrin")
		self.assertEqual(row["item_group"], CONSUMABLES, "an explicit group wins over the EPA default")

	def test_an_epa_number_with_no_group_files_it_under_crop_protection(self):
		self.assertFalse(frappe.db.exists("Item Group", masters.CROP_PROTECTION_GROUP))
		data = self.tool_data(
			"create_item", {"item_code": "WARRIOR-II", "epa_registration_number": "100-1234"}
		)
		self.assertEqual(data["item_group"], masters.CROP_PROTECTION_GROUP)
		self.assertIs(data["item_group_created"], True)
		group = STORE.get_raw("Item Group", masters.CROP_PROTECTION_GROUP)
		self.assertEqual(group["parent_item_group"], ITEM_GROUP_ROOT)
		self.assertEqual(group["is_group"], 0)
		second = self.tool_data("create_item", {"item_code": "ASSAIL", "epa_registration_number": "8033-36"})
		self.assertEqual(second["item_group"], masters.CROP_PROTECTION_GROUP)
		self.assertNotIn("item_group_created", second)

	def test_no_epa_number_keeps_the_old_default_group(self):
		data = self.tool_data("create_item", {"item_code": "TWINE-2", "rei_hours": 0})
		self.assertEqual(data["item_group"], ITEM_GROUP_ROOT)

	def test_a_refused_create_does_not_make_the_group(self):
		self.tool_error(
			"create_item", {"item_code": "X", "epa_registration_number": "1-2", "signal_word": "Deadly"}
		)
		self.assertFalse(frappe.db.exists("Item Group", masters.CROP_PROTECTION_GROUP))

	def test_an_unknown_signal_word_is_refused_with_the_four(self):
		message = self.tool_error("create_item", {"item_code": "X", "signal_word": "Deadly"})
		for word in ("Danger", "Warning", "Caution", "None"):
			self.assertIn(word, message)
		self.assertIsNone(STORE.get_raw("Item", "X"))

	def test_negative_intervals_are_refused(self):
		for key in ("rei_hours", "phi_days"):
			message = self.tool_error("create_item", {"item_code": "X", key: -1})
			self.assertIn(key, message)
			self.assertIn("at least 0", message)

	def test_a_fractional_rei_is_refused_rather_than_truncated(self):
		message = self.tool_error("create_item", {"item_code": "X", "rei_hours": 4.5})
		self.assertIn("whole number", message)

	def test_zero_is_a_real_interval_and_is_stored(self):
		self.tool_data("create_item", {"item_code": "FOLIAR-N", "rei_hours": 0, "phi_days": 0})
		row = STORE.get_raw("Item", "FOLIAR-N")
		self.assertEqual((row["rei_hours"], row["phi_days"]), (0, 0))

	def test_active_ingredients_as_a_json_string_is_accepted(self):
		self.tool_data(
			"create_item",
			{
				"item_code": "X",
				"active_ingredients": '[{"name": "Captan", "concentration": 80, "unit": "%"}]',
			},
		)
		self.assertEqual(json.loads(STORE.get_raw("Item", "X")["active_ingredients"])[0]["name"], "Captan")

	def test_invalid_active_ingredients_are_refused(self):
		cases = (
			("not json", "valid JSON"),
			('{"name": "Captan"}', "list"),
			([{"concentration": 80}], "needs a name"),
			([{"name": "Captan", "concentration": "lots"}], "must be a number"),
			([{"name": "Captan", "moa": "M4"}], "moa"),
		)
		for value, expected in cases:
			message = self.tool_error("create_item", {"item_code": "X", "active_ingredients": value})
			self.assertIn(expected, message, value)
		self.assertIsNone(STORE.get_raw("Item", "X"))

	def test_an_unknown_label_scan_is_refused(self):
		message = self.tool_error("create_item", {"item_code": "X", "label_scan_validation": "DV-NOPE"})
		self.assertIn("no Document Validation called 'DV-NOPE'", message)

	def test_a_site_without_the_columns_refuses_rather_than_dropping_them(self):
		frappe.get_meta("Item")  # warm
		meta = META["Item"]
		field = meta._by_name.pop("restricted_use")
		meta.fields.remove(field)
		try:
			message = self.tool_error("create_item", {"item_code": "X", "restricted_use": True})
		finally:
			meta.add(field)
		self.assertIn("install_compliance_fields", message)
		self.assertIsNone(STORE.get_raw("Item", "X"))

	def test_update_changes_label_fields_and_reports_before_and_after(self):
		self.tool_data("create_item", {"item_code": "WARRIOR-II", **self.LABEL})
		data = self.tool_data(
			"update_item",
			{"item_code": "WARRIOR-II", "rei_hours": 48, "phi_days": 14, "signal_word": "Danger"},
		)
		self.assertEqual(data["changed"]["rei_hours"], [24, 48])
		self.assertEqual(data["changed"]["signal_word"], ["Warning", "Danger"])
		self.assertNotIn("phi_days", data["changed"], "an unchanged value is not reported as a change")
		self.assertEqual(STORE.get_raw("Item", "WARRIOR-II")["rei_hours"], 48)

	def test_update_can_set_an_interval_to_zero(self):
		self.tool_data("create_item", {"item_code": "WARRIOR-II", **self.LABEL})
		data = self.tool_data("update_item", {"item_code": "WARRIOR-II", "rei_hours": 0})
		self.assertEqual(data["changed"]["rei_hours"], [24, 0])
		self.assertEqual(STORE.get_raw("Item", "WARRIOR-II")["rei_hours"], 0)

	def test_update_with_only_a_label_field_is_not_nothing_to_change(self):
		data = self.tool_data("update_item", {"item_code": SPRAY, "restricted_use": True})
		self.assertEqual(data["changed"]["restricted_use"], [0, 1])

	def test_update_validates_like_create(self):
		message = self.tool_error("update_item", {"item_code": SPRAY, "phi_days": -3})
		self.assertIn("at least 0", message)
		self.assertIn("Nothing was changed", message)

	def test_update_does_not_move_the_group(self):
		before = STORE.get_raw("Item", SPRAY)["item_group"]
		self.tool_data("update_item", {"item_code": SPRAY, "epa_registration_number": "70051-2"})
		self.assertEqual(STORE.get_raw("Item", SPRAY)["item_group"], before)


class Suppliers(MastersTestCase):
	def test_lists_suppliers_with_their_group_and_type(self):
		data = self.tool_data("list_suppliers")
		row = next(s for s in data["suppliers"] if s["name"] == MASTER_SUPPLIER)
		self.assertEqual(row["supplier_group"], "Services")
		self.assertEqual(row["supplier_type"], "Company")

	def test_filters_by_disabled(self):
		data = self.tool_data("list_suppliers", {"disabled": False})
		self.assertEqual([s["name"] for s in data["suppliers"]], [MASTER_SUPPLIER])

	def test_filters_by_group(self):
		data = self.tool_data("list_suppliers", {"supplier_group": "Services"})
		self.assertEqual(data["count"], 2)

	def test_an_unknown_group_is_refused_with_the_known_ones(self):
		message = self.tool_error("list_suppliers", {"supplier_group": "Nope"})
		self.assertIn("Services", message)

	def test_gets_one_supplier_with_its_company_account_overrides(self):
		data = self.tool_data("get_supplier", {"name": MASTER_SUPPLIER})
		self.assertEqual(
			data["company_accounts"],
			[{"company": MAIN, "account": f"2100 - Accounts Payable - {MAIN_ABBR}"}],
		)

	def test_a_site_with_no_address_module_reports_no_addresses_rather_than_failing(self):
		"""The default double never installed ERPNext's address module, which is a
		real configuration. An empty list is the answer; an exception is not."""
		data = self.tool_data("get_supplier", {"name": MASTER_SUPPLIER})
		self.assertEqual(data["addresses"], [])

	def test_it_finds_the_addresses_through_the_link_row_on_the_address(self):
		"""ERPNext links an Address to a party from the ADDRESS side, through a
		Dynamic Link row — not with a field on the party. A tool that looked on
		the Supplier would find nothing on every real site."""
		self.seed_address()
		data = self.tool_data("get_supplier", {"name": MASTER_SUPPLIER})
		self.assertEqual(data["addresses"], [f"{MASTER_SUPPLIER}-Billing"])

	def seed_address(self):
		"""Register and seed ERPNext's address pair, as test_employee does."""
		register_doctype("Address", [{"fieldname": name} for name in ("name", "address_title", "city")])
		register_doctype(
			"Dynamic Link",
			[{"fieldname": name} for name in ("name", "parent", "parenttype", "link_doctype", "link_name")],
		)
		STORE.seed(
			"Address",
			[{"name": f"{MASTER_SUPPLIER}-Billing", "address_title": MASTER_SUPPLIER, "city": "Wenatchee"}],
		)
		STORE.seed(
			"Dynamic Link",
			[
				{
					"name": "DL-SUP-1",
					"parent": f"{MASTER_SUPPLIER}-Billing",
					"parenttype": "Address",
					"link_doctype": "Supplier",
					"link_name": MASTER_SUPPLIER,
				}
			],
		)

	def test_a_supplier_can_be_fetched_by_alias(self):
		data = self.tool_data("get_supplier", {"supplier": MASTER_SUPPLIER})
		self.assertEqual(data["supplier_name"], MASTER_SUPPLIER)

	def test_an_unknown_supplier_is_refused(self):
		message = self.tool_error("get_supplier", {"name": "Nobody"})
		self.assertIn("no Supplier called 'Nobody'", message)


class PartyCompanyScope(MastersTestCase):
	"""A Supplier and a Customer are site-wide, and the tools say so."""

	def test_a_company_filter_on_suppliers_is_reported_as_not_applied(self):
		data = self.tool_data("list_suppliers", {"company": MAIN})
		self.assertIn("was NOT applied", data["company_scope"])
		self.assertEqual(data["count"], 2)

	def test_the_company_is_still_validated(self):
		"""Not applied is not the same as ignored: a company that does not exist
		is a mistake worth hearing about."""
		message = self.tool_error("list_customers", {"company": "Nonexistent Farms"})
		self.assertIn("no Company named", message)

	def test_a_company_on_create_is_validated_and_reported_as_not_stored(self):
		self.configure(enabled=1, **WRITES_ON)
		data = self.tool_data("create_supplier", {"supplier_name": "Wilbur-Ellis", "company": MAIN})
		self.assertEqual(data["company"], MAIN)
		self.assertIn("NOT stored", data["company_scope"])


class CreateSupplier(MastersTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **WRITES_ON)

	def test_creates_a_supplier_in_the_default_group(self):
		"""v0.67.0: the default is the first NON-GROUP group, not the tree root.

		`All Supplier Groups` is a branch, and ERPNext puts an `is_group = 0` link
		filter on `Supplier.supplier_group` — so the old default was refused by
		the framework on every call that did not name a group, which is every call
		the tool exists to make easy.
		"""
		data = self.tool_data("create_supplier", {"supplier_name": "Wilbur-Ellis"})
		self.assertEqual(data["name"], "Wilbur-Ellis")
		self.assertEqual(data["supplier_group"], "Services")
		self.assertIsNotNone(STORE.get_raw("Supplier", "Wilbur-Ellis"))

	def test_the_supplier_type_is_matched_case_insensitively(self):
		data = self.tool_data(
			"create_supplier", {"supplier_name": "Wilbur-Ellis", "supplier_type": "company"}
		)
		self.assertEqual(data["supplier_type"], "Company")

	def test_a_supplier_type_the_site_does_not_offer_is_refused_with_the_options(self):
		message = self.tool_error(
			"create_supplier", {"supplier_name": "Wilbur-Ellis", "supplier_type": "Partnership"}
		)
		self.assertIn("Company", message)
		self.assertIn("Individual", message)

	def test_an_unknown_group_is_refused_with_the_known_ones(self):
		message = self.tool_error(
			"create_supplier", {"supplier_name": "Wilbur-Ellis", "supplier_group": "Nope"}
		)
		self.assertIn("Services", message)
		self.assertIn("Nothing was created", message)

	def test_a_duplicate_supplier_is_refused(self):
		message = self.tool_error("create_supplier", {"supplier_name": MASTER_SUPPLIER})
		self.assertIn("already exists", message)

	def test_the_tax_id_is_carried_through(self):
		self.tool_data("create_supplier", {"supplier_name": "Wilbur-Ellis", "tax_id": "91-1234567"})
		self.assertEqual(STORE.get_raw("Supplier", "Wilbur-Ellis")["tax_id"], "91-1234567")


class UpdateSupplier(MastersTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **WRITES_ON)

	def test_moves_a_supplier_to_another_group(self):
		data = self.tool_data(
			"update_supplier", {"name": MASTER_SUPPLIER, "supplier_group": "All Supplier Groups"}
		)
		self.assertEqual(data["changed"]["supplier_group"], ["Services", "All Supplier Groups"])

	def test_disables_a_supplier(self):
		self.tool_data("update_supplier", {"name": MASTER_SUPPLIER, "disabled": True})
		self.assertEqual(STORE.get_raw("Supplier", MASTER_SUPPLIER)["disabled"], 1)

	def test_restores_a_disabled_supplier(self):
		self.tool_data("update_supplier", {"name": RETIRED_SUPPLIER, "disabled": False})
		self.assertEqual(STORE.get_raw("Supplier", RETIRED_SUPPLIER)["disabled"], 0)

	def test_nothing_that_differs_is_refused_rather_than_saved(self):
		message = self.tool_error("update_supplier", {"name": MASTER_SUPPLIER, "supplier_group": "Services"})
		self.assertIn("nothing to change", message)


class Customers(MastersTestCase):
	def test_lists_customers_with_their_group_and_territory(self):
		data = self.tool_data("list_customers")
		row = data["customers"][0]
		self.assertEqual(row["customer_group"], "Packers")
		self.assertEqual(row["territory"], "Washington")

	def test_filters_by_territory(self):
		data = self.tool_data("list_customers", {"territory": "Washington"})
		self.assertEqual(data["count"], 1)

	def test_an_unknown_territory_is_refused(self):
		message = self.tool_error("list_customers", {"territory": "Mars"})
		self.assertIn("no Territory called 'Mars'", message)

	def test_gets_one_customer(self):
		data = self.tool_data("get_customer", {"name": MASTER_CUSTOMER})
		self.assertEqual(data["customer_type"], "Company")
		self.assertEqual(data["company_accounts"], [])

	def test_creates_a_customer_in_a_group_and_territory(self):
		self.configure(enabled=1, **WRITES_ON)
		data = self.tool_data(
			"create_customer",
			{
				"customer_name": "Stemilt Growers",
				"customer_group": "Packers",
				"customer_type": "Company",
				"territory": "Washington",
			},
		)
		self.assertEqual(data["name"], "Stemilt Growers")
		self.assertEqual(data["territory"], "Washington")
		self.assertIsNotNone(STORE.get_raw("Customer", "Stemilt Growers"))

	def test_the_group_defaults_to_a_leaf_and_the_territory_to_the_root(self):
		"""v0.67.0, and the asymmetry is the point.

		A Customer Group default of `All Customer Groups` was refused on a bench:
		it is the root of the tree, ERPNext filters `Customer.customer_group` to
		`is_group = 0`, and every `create_customer` call that did not name a group
		— the whole convenience of the tool — failed. So the group now defaults to
		the site's alphabetically first non-group node.

		The TERRITORY still defaults to `All Territories`, which is also a group
		node, because ERPNext accepts a group territory on a Customer and its own
		Selling Settings default is exactly that. Changing it would be fixing a
		bug nobody has.
		"""
		self.configure(enabled=1, **WRITES_ON)
		data = self.tool_data("create_customer", {"customer_name": "Stemilt Growers"})
		self.assertEqual(data["customer_group"], "Packers")
		self.assertEqual(data["territory"], "All Territories")

	def test_a_named_group_still_wins_over_the_leaf_default(self):
		"""The default only applies when nothing was asked for. A caller who
		names a group — even a group NODE — gets what they named."""
		self.configure(enabled=1, **WRITES_ON)
		STORE.seed("Customer Group", [{"name": "Wholesale"}])
		data = self.tool_data(
			"create_customer", {"customer_name": "Stemilt Growers", "customer_group": "Wholesale"}
		)
		self.assertEqual(data["customer_group"], "Wholesale")

	def test_the_leaf_default_is_alphabetical_and_not_insertion_order(self):
		"""A default that depended on insertion order would differ between two
		sites nobody could tell apart."""
		self.configure(enabled=1, **WRITES_ON)
		STORE.seed("Customer Group", [{"name": "Wholesale"}, {"name": "Commercial"}])
		data = self.tool_data("create_customer", {"customer_name": "Stemilt Growers"})
		self.assertEqual(data["customer_group"], "Commercial")

	def test_a_site_with_only_group_nodes_is_refused_with_the_reason(self):
		"""Not "no Customer Group called X" — that reads as a typo and sends
		somebody looking for a record that could never have been there."""
		self.configure(enabled=1, **WRITES_ON)
		STORE.tables["Customer Group"] = {
			"All Customer Groups": {"name": "All Customer Groups", "is_group": 1},
			"Trade": {"name": "Trade", "is_group": 1},
		}
		message = self.tool_error("create_customer", {"customer_name": "Stemilt Growers"})
		self.assertIn("no non-group Customer Group", message)
		self.assertIn("Nothing was created", message)

	def test_a_duplicate_customer_is_refused(self):
		self.configure(enabled=1, **WRITES_ON)
		message = self.tool_error("create_customer", {"customer_name": MASTER_CUSTOMER})
		self.assertIn("already exists", message)

	def test_updates_a_customers_territory(self):
		self.configure(enabled=1, **WRITES_ON)
		data = self.tool_data("update_customer", {"name": MASTER_CUSTOMER, "territory": "All Territories"})
		self.assertEqual(data["changed"]["territory"], ["Washington", "All Territories"])


class Warehouses(MastersTestCase):
	def test_lists_warehouses_flat_with_their_parents(self):
		data = self.tool_data("list_warehouses")
		by_name = {row["name"]: row for row in data["warehouses"]}
		self.assertEqual(by_name[STORES]["parent_warehouse"], MAIN_WAREHOUSE_ROOT)
		self.assertEqual(sorted(data["roots"]), [MAIN_WAREHOUSE_ROOT, OTHER_WAREHOUSE_ROOT])

	def test_the_company_filter_here_really_is_exact(self):
		"""Unlike Items and parties. This is the one of the three where the
		obvious reading of `company` is the right one."""
		data = self.tool_data("list_warehouses", {"company": OTHER})
		self.assertEqual([row["name"] for row in data["warehouses"]], [OTHER_WAREHOUSE_ROOT])
		self.assertNotIn("company_scope", data)

	def test_filters_to_stock_holding_leaves(self):
		data = self.tool_data("list_warehouses", {"is_group": False})
		self.assertEqual([row["name"] for row in data["warehouses"]], [STORES])

	def test_creates_a_warehouse_named_after_the_company_abbreviation(self):
		self.configure(enabled=1, **WRITES_ON)
		data = self.tool_data("create_warehouse", {"warehouse_name": "Chemical Shed", "company": MAIN})
		self.assertEqual(data["name"], f"Chemical Shed - {MAIN_ABBR}")
		self.assertEqual(data["parent_warehouse"], MAIN_WAREHOUSE_ROOT)
		self.assertIsNotNone(STORE.get_raw("Warehouse", f"Chemical Shed - {MAIN_ABBR}"))

	def test_the_predicted_docname_is_what_refuses_a_duplicate(self):
		self.configure(enabled=1, **WRITES_ON)
		message = self.tool_error("create_warehouse", {"warehouse_name": "Stores", "company": MAIN})
		self.assertIn(f"Stores - {MAIN_ABBR}", message)
		self.assertIn("Nothing was created", message)

	def test_the_same_name_in_another_company_is_fine(self):
		"""Because the abbreviation is part of the docname, two companies can
		each have a Stores — which is why the check has to be on the predicted
		name and not on the warehouse_name."""
		self.configure(enabled=1, **WRITES_ON)
		data = self.tool_data("create_warehouse", {"warehouse_name": "Stores", "company": OTHER})
		self.assertEqual(data["name"], f"Stores - {OTHER_ABBR}")

	def test_a_parent_in_another_company_is_refused(self):
		self.configure(enabled=1, **WRITES_ON)
		message = self.tool_error(
			"create_warehouse",
			{"warehouse_name": "Chemical Shed", "company": MAIN, "parent_warehouse": OTHER_WAREHOUSE_ROOT},
		)
		self.assertIn("belongs to company", message)

	def test_a_leaf_parent_is_refused(self):
		self.configure(enabled=1, **WRITES_ON)
		message = self.tool_error(
			"create_warehouse",
			{"warehouse_name": "Chemical Shed", "company": MAIN, "parent_warehouse": STORES},
		)
		self.assertIn("is a leaf, not a group", message)

	def test_an_unknown_warehouse_type_is_refused_with_the_known_ones(self):
		self.configure(enabled=1, **WRITES_ON)
		message = self.tool_error(
			"create_warehouse",
			{"warehouse_name": "Chemical Shed", "company": MAIN, "warehouse_type": "Fridge"},
		)
		self.assertIn("Transit", message)

	def test_company_is_required_on_a_multi_company_site(self):
		self.configure(enabled=1, **WRITES_ON)
		message = self.tool_error("create_warehouse", {"warehouse_name": "Chemical Shed"})
		self.assertIn("company is required", message)


class Prices(MastersTestCase):
	def test_lists_the_price_lists_with_their_flags(self):
		data = self.tool_data("list_price_lists")
		by_name = {row["name"]: row for row in data["price_lists"]}
		self.assertIs(by_name[STANDARD_BUYING]["buying"], True)
		self.assertIs(by_name[STANDARD_BUYING]["selling"], False)

	def test_filters_to_selling_lists(self):
		data = self.tool_data("list_price_lists", {"selling": True})
		self.assertEqual([row["name"] for row in data["price_lists"]], [STANDARD_SELLING])

	def test_gets_every_price_for_an_item(self):
		data = self.tool_data("get_item_price", {"item_code": SPRAY})
		self.assertEqual(data["count"], 2)

	def test_the_rate_is_left_null_when_more_than_one_row_applies(self):
		"""Two prices and no date is not a price. Picking one would be this tool
		inventing ERPNext's pricing rules."""
		data = self.tool_data("get_item_price", {"item_code": SPRAY})
		self.assertIsNone(data["price_list_rate"])

	def test_an_as_of_date_narrows_it_to_the_row_in_force(self):
		data = self.tool_data("get_item_price", {"item_code": SPRAY, "as_of": "2026-08-01"})
		self.assertEqual(data["price_list_rate"], 2.40)
		self.assertEqual([row["name"] for row in data["applicable"]], ["IP-0002"])

	def test_an_open_ended_window_is_still_in_force(self):
		data = self.tool_data("get_item_price", {"item_code": SPRAY, "as_of": "2030-01-01"})
		self.assertEqual([row["name"] for row in data["applicable"]], ["IP-0002"])

	def test_a_date_inside_the_closed_window_picks_the_older_row(self):
		data = self.tool_data("get_item_price", {"item_code": SPRAY, "as_of": "2026-03-01"})
		self.assertEqual(data["price_list_rate"], 2.15)

	def test_the_full_list_survives_an_as_of_filter(self):
		"""`applicable` narrows; `prices` does not. "What was it in March" and
		"what prices exist" are both questions somebody asks."""
		data = self.tool_data("get_item_price", {"item_code": SPRAY, "as_of": "2026-03-01"})
		self.assertEqual(data["count"], 2)

	def test_an_unknown_price_list_is_refused_with_the_known_ones(self):
		message = self.tool_error("get_item_price", {"item_code": SPRAY, "price_list": "Wholesale"})
		self.assertIn(STANDARD_BUYING, message)


class SetItemPrice(MastersTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **WRITES_ON)

	def test_creates_a_price_where_none_exists(self):
		data = self.tool_data(
			"set_item_price",
			{"item_code": TWINE, "price_list": STANDARD_BUYING, "rate": 42.5},
		)
		self.assertIs(data["created"], True)
		self.assertIsNone(data["previous_rate"])
		self.assertEqual(data["currency"], "USD")

	def test_the_currency_comes_from_the_price_list(self):
		data = self.tool_data(
			"set_item_price", {"item_code": TWINE, "price_list": STANDARD_SELLING, "rate": 60}
		)
		self.assertEqual(data["currency"], "USD")

	def test_updates_the_row_whose_whole_key_matches(self):
		data = self.tool_data(
			"set_item_price",
			{
				"item_code": SPRAY,
				"price_list": STANDARD_BUYING,
				"uom": "Lb",
				"valid_from": "2026-07-01",
				"rate": 2.65,
			},
		)
		self.assertIs(data["created"], False)
		self.assertEqual(data["previous_rate"], 2.40)
		self.assertEqual(STORE.get_raw("Item Price", "IP-0002")["price_list_rate"], 2.65)

	def test_a_different_valid_from_is_a_different_row(self):
		"""The key includes the date window. Matching on item and list alone
		would overwrite July's price with September's."""
		data = self.tool_data(
			"set_item_price",
			{
				"item_code": SPRAY,
				"price_list": STANDARD_BUYING,
				"uom": "Lb",
				"valid_from": "2026-09-01",
				"rate": 2.80,
			},
		)
		self.assertIs(data["created"], True)
		self.assertEqual(STORE.get_raw("Item Price", "IP-0002")["price_list_rate"], 2.40)

	def test_a_customer_and_a_supplier_together_are_refused(self):
		message = self.tool_error(
			"set_item_price",
			{
				"item_code": SPRAY,
				"price_list": STANDARD_BUYING,
				"rate": 1,
				"customer": MASTER_CUSTOMER,
				"supplier": MASTER_SUPPLIER,
			},
		)
		self.assertIn("not both", message)

	def test_a_negative_rate_is_refused(self):
		message = self.tool_error(
			"set_item_price", {"item_code": SPRAY, "price_list": STANDARD_BUYING, "rate": -1}
		)
		self.assertIn("cannot be negative", message)

	def test_an_inverted_validity_window_is_refused(self):
		message = self.tool_error(
			"set_item_price",
			{
				"item_code": SPRAY,
				"price_list": STANDARD_BUYING,
				"rate": 1,
				"valid_from": "2026-12-01",
				"valid_upto": "2026-01-01",
			},
		)
		self.assertIn("is after", message)
		self.assertIn("Nothing was written", message)

	def test_an_unknown_price_list_is_refused(self):
		message = self.tool_error(
			"set_item_price", {"item_code": SPRAY, "price_list": "Wholesale", "rate": 1}
		)
		self.assertIn("Nothing was written", message)

	def test_a_missing_rate_is_refused(self):
		message = self.tool_error("set_item_price", {"item_code": SPRAY, "price_list": STANDARD_BUYING})
		self.assertIn("rate is required", message)


class SwitchesAndAudit(MastersTestCase):
	def test_the_read_tools_are_on_by_default(self):
		for name in READ_TOOLS:
			with self.subTest(tool=name):
				self.assertIn(name, registry.READ_TOOLS)

	def test_the_write_tools_are_mutating_and_off_by_default(self):
		for name in WRITE_TOOLS:
			with self.subTest(tool=name):
				self.assertIn(name, registry.MUTATING_TOOLS)
				self.assertNotIn(name, registry.DEFAULT_ON_MUTATING_TOOLS)

	def test_every_tool_declares_the_erpnext_requirement(self):
		"""All nineteen wrap stock ERPNext doctypes, so a Frappe-only bench must
		see them as unavailable rather than as broken."""
		for name in ALL:
			with self.subTest(tool=name):
				self.assertEqual(registry.TOOLS[name]["requires"], "the ERPNext app")

	def test_a_creation_is_audited(self):
		self.configure(enabled=1, **WRITES_ON)
		self.tool_data("create_supplier", {"supplier_name": "Wilbur-Ellis"})
		self.assertAudited("create_supplier", "Success")

	def test_a_refusal_is_audited_too(self):
		self.configure(enabled=1, **WRITES_ON)
		self.tool_error("create_supplier", {"supplier_name": MASTER_SUPPLIER})
		self.assertAudited("create_supplier", "Error")


class ReadsWorkOnAFrappeOnlyBench(MastersTestCase):
	"""Every tool here needs ERPNext, and says so rather than failing."""

	def test_they_are_hidden_when_erpnext_is_absent(self):
		STORE.installed_apps = ["frappe"]
		body, _ = self.call("tools/list")
		names = [tool["name"] for tool in body["result"]["tools"]]
		for name in ALL:
			with self.subTest(tool=name):
				self.assertNotIn(name, names)

	def test_calling_one_anyway_says_what_is_missing(self):
		STORE.installed_apps = ["frappe"]
		message = self.tool_error("list_items")
		self.assertIn("ERPNext", message)

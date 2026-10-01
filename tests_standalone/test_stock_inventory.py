# SPDX-License-Identifier: MIT
"""Stock Entry, stock balances, the ledger, warehouse summaries and reorder rules."""

from .fixtures import (
	CASE_FACTOR,
	MAIN,
	OTHER,
	OTHER_STORES,
	RETIRED_ITEM,
	SHOP,
	SPRAY,
	STORES,
	TWINE,
	StockTestCase,
)
from .harness import STORE, frappe, post_stock_entry_ledger, register_doctype

#: Every mutating tool this module adds. Read tools default on and need no
#: override; these default off, so a test that expects one to run has to turn it
#: on first — `WriteEnabledTestCase` does that once for every class below that
#: exercises a create, a submit or a reorder write.
ALL_ON = {f"allow_{name}": 1 for name in ("create_stock_entry", "submit_stock_entry", "set_reorder_level")}


class WriteEnabledTestCase(StockTestCase):
	def setUp(self):
		super().setUp()
		self.configure(**ALL_ON)


def _receipt(**overrides):
	args = {
		"entry_type": "Material Receipt",
		"company": MAIN,
		"items": [{"item_code": SPRAY, "qty": 40, "warehouse": STORES, "basic_rate": 2.5}],
	}
	args.update(overrides)
	return args


def _transfer(**overrides):
	args = {
		"entry_type": "Material Transfer",
		"company": MAIN,
		"items": [{"item_code": SPRAY, "qty": 10, "warehouse": STORES, "target_warehouse": SHOP}],
	}
	args.update(overrides)
	return args


class CreateStockEntry(WriteEnabledTestCase):
	def test_creates_a_draft_that_has_moved_nothing(self):
		data = self.tool_data("create_stock_entry", _receipt())
		self.assertEqual(data["docstatus"], 0)
		self.assertEqual(data["status"], "Draft")
		self.assertEqual(data["entry_type"], "Material Receipt")
		self.assertEqual(data["total_qty"], 40.0)
		self.assertEqual(data["total_value"], 100.0)
		self.assertTrue(data["name"])
		# The balance the fixture seeded is untouched: a draft writes no ledger.
		balance = self.tool_data("get_stock_balance", {"item_code": SPRAY, "warehouse": STORES})
		self.assertEqual(balance["total_qty"], 80.0)

	def test_a_receipt_puts_the_warehouse_in_the_target_column(self):
		data = self.tool_data("create_stock_entry", _receipt())
		line = data["items"][0]
		self.assertEqual(line["target_warehouse"], STORES)
		self.assertIsNone(line["source_warehouse"])

	def test_an_issue_puts_the_warehouse_in_the_source_column(self):
		data = self.tool_data(
			"create_stock_entry",
			_receipt(
				entry_type="Material Issue",
				items=[{"item_code": SPRAY, "qty": 5, "warehouse": STORES}],
			),
		)
		line = data["items"][0]
		self.assertEqual(line["source_warehouse"], STORES)
		self.assertIsNone(line["target_warehouse"])

	def test_a_transfer_carries_both_warehouses(self):
		data = self.tool_data("create_stock_entry", _transfer())
		line = data["items"][0]
		self.assertEqual(line["source_warehouse"], STORES)
		self.assertEqual(line["target_warehouse"], SHOP)

	def test_a_transfer_without_a_target_is_refused(self):
		message = self.tool_error(
			"create_stock_entry",
			_transfer(items=[{"item_code": SPRAY, "qty": 10, "warehouse": STORES}]),
		)
		self.assertIn("target_warehouse is required", message)
		self.assertEqual(STORE.rows("Stock Entry"), [])

	def test_a_transfer_to_the_same_warehouse_is_refused(self):
		message = self.tool_error(
			"create_stock_entry",
			_transfer(
				items=[{"item_code": SPRAY, "qty": 10, "warehouse": STORES, "target_warehouse": STORES}]
			),
		)
		self.assertIn("moves nothing", message)

	def test_a_target_warehouse_on_a_receipt_is_refused_rather_than_ignored(self):
		"""The two readings of "I passed both" have opposite consequences."""
		message = self.tool_error(
			"create_stock_entry",
			_receipt(items=[{"item_code": SPRAY, "qty": 40, "warehouse": STORES, "target_warehouse": SHOP}]),
		)
		self.assertIn("not accepted on a Material Receipt", message)
		self.assertEqual(STORE.rows("Stock Entry"), [])

	def test_qty_must_be_positive(self):
		message = self.tool_error(
			"create_stock_entry",
			_receipt(items=[{"item_code": SPRAY, "qty": -5, "warehouse": STORES}]),
		)
		self.assertIn("qty must be positive", message)
		self.assertIn("Material Issue", message)

	def test_an_unknown_entry_type_is_refused_with_the_three_that_work(self):
		message = self.tool_error("create_stock_entry", _receipt(entry_type="Manufacture"))
		self.assertIn("Material Receipt", message)
		self.assertIn("Material Transfer", message)

	def test_an_unknown_item_is_refused_by_name(self):
		message = self.tool_error(
			"create_stock_entry",
			_receipt(items=[{"item_code": "NOPE-01", "qty": 1, "warehouse": STORES}]),
		)
		self.assertIn("NOPE-01", message)
		self.assertIn("list_items", message)

	def test_a_warehouse_from_another_company_is_refused(self):
		message = self.tool_error(
			"create_stock_entry",
			_receipt(items=[{"item_code": SPRAY, "qty": 1, "warehouse": OTHER_STORES}]),
		)
		self.assertIn(OTHER, message)

	def test_a_batch_is_written_through_on_a_site_that_does_not_track_them(self):
		"""The fixture site has no Batch DocType, and that is a real configuration.

		Refusing every `batch_no` there would break a caller echoing a number
		printed on a drum; validating against a register that does not exist is
		not possible. So the value is stored and the check is the one below.
		"""
		data = self.tool_data(
			"create_stock_entry",
			_receipt(items=[{"item_code": SPRAY, "qty": 1, "warehouse": STORES, "batch_no": "DRUM-7"}]),
		)
		self.assertEqual(data["items"][0]["batch_no"], "DRUM-7")

	def test_a_batch_that_does_not_exist_is_refused_where_the_site_has_batches(self):
		register_doctype("Batch", [{"fieldname": "name", "fieldtype": "Data"}])
		message = self.tool_error(
			"create_stock_entry",
			_receipt(items=[{"item_code": SPRAY, "qty": 1, "warehouse": STORES, "batch_no": "BATCH-NOPE"}]),
		)
		self.assertIn("BATCH-NOPE", message)
		self.assertEqual(STORE.rows("Stock Entry"), [])


class UomConversion(WriteEnabledTestCase):
	"""Three Cases is thirty-six pounds, and a factor of 1 would ship three."""

	def test_a_known_uom_is_converted_to_the_stock_uom(self):
		data = self.tool_data(
			"create_stock_entry",
			_receipt(items=[{"item_code": SPRAY, "qty": 3, "uom": "Case", "warehouse": STORES}]),
		)
		line = data["items"][0]
		self.assertEqual(line["qty"], 3.0)
		self.assertEqual(line["conversion_factor"], CASE_FACTOR)
		self.assertEqual(line["stock_qty"], 3 * CASE_FACTOR)
		self.assertEqual(data["total_qty"], 3 * CASE_FACTOR)

	def test_the_stock_uom_itself_needs_no_conversion(self):
		data = self.tool_data(
			"create_stock_entry",
			_receipt(items=[{"item_code": SPRAY, "qty": 4, "uom": "Lb", "warehouse": STORES}]),
		)
		self.assertEqual(data["items"][0]["conversion_factor"], 1.0)
		self.assertEqual(data["total_qty"], 4.0)

	def test_an_unconvertible_uom_is_refused_and_names_the_stock_uom(self):
		message = self.tool_error(
			"create_stock_entry",
			_receipt(items=[{"item_code": TWINE, "qty": 3, "uom": "Case", "warehouse": STORES}]),
		)
		self.assertIn("Case", message)
		self.assertIn("Nos", message)
		self.assertEqual(STORE.rows("Stock Entry"), [])


class SourceLinkage(WriteEnabledTestCase):
	def test_a_native_doctype_is_stored_in_stock_entrys_own_field(self):
		"""An issue out of one shed received into another is ERPNext's own pattern."""
		first = self.tool_data(
			"create_stock_entry",
			_receipt(
				entry_type="Material Issue",
				items=[{"item_code": SPRAY, "qty": 10, "warehouse": STORES}],
			),
		)
		data = self.tool_data(
			"create_stock_entry",
			_receipt(source_doctype="Stock Entry", source_name=first["name"]),
		)
		self.assertEqual(data["source"]["stored_on"], "Stock Entry.outgoing_stock_entry")
		read_back = self.tool_data("get_stock_entry", {"name": data["name"]})
		self.assertEqual(read_back["source"]["doctype"], "Stock Entry")
		self.assertEqual(read_back["source"]["name"], first["name"])

	def test_a_doctype_erpnext_has_no_field_for_falls_back_to_a_remarks_marker(self):
		data = self.tool_data(
			"create_stock_entry",
			_receipt(source_doctype="Item", source_name=TWINE, remarks="opening count"),
		)
		self.assertEqual(data["source"]["stored_on"], "Stock Entry.remarks marker")
		self.assertIn("opening count", data["remarks"])
		read_back = self.tool_data("get_stock_entry", {"name": data["name"]})
		self.assertEqual(
			read_back["source"],
			{
				"doctype": "Item",
				"name": TWINE,
				"stored_on": "Stock Entry.remarks marker",
			},
		)

	def test_one_half_of_the_pair_is_refused(self):
		message = self.tool_error("create_stock_entry", _receipt(source_doctype="Item"))
		self.assertIn("pass both or neither", message)

	def test_a_source_record_that_does_not_exist_is_refused(self):
		message = self.tool_error("create_stock_entry", _receipt(source_doctype="Item", source_name="NOPE"))
		self.assertIn("no Item called 'NOPE'", message)

	def test_an_entry_with_no_source_reports_none(self):
		data = self.tool_data("create_stock_entry", _receipt())
		self.assertIsNone(data["source"])


class SubmitStockEntry(WriteEnabledTestCase):
	def test_submitting_moves_the_docstatus(self):
		draft = self.tool_data("create_stock_entry", _receipt())
		data = self.tool_data("submit_stock_entry", {"name": draft["name"]})
		self.assertEqual(data["status"], "Submitted")
		self.assertEqual(data["docstatus"], 1)

	def test_submitting_twice_is_refused(self):
		draft = self.tool_data("create_stock_entry", _receipt())
		self.tool_data("submit_stock_entry", {"name": draft["name"]})
		message = self.tool_error("submit_stock_entry", {"name": draft["name"]})
		self.assertIn("already submitted", message)

	def test_an_unknown_name_is_refused(self):
		self.assertIn("NOPE", self.tool_error("submit_stock_entry", {"name": "NOPE"}))

	def test_a_submitted_receipt_moves_the_balance_erpnext_maintains(self):
		"""End to end: create, submit, and read the balance the ledger produced.

		`post_stock_entry_ledger` stands in for ERPNext's own controller — see
		its docstring for why the double makes that step explicit.
		"""
		draft = self.tool_data("create_stock_entry", _receipt())
		self.tool_data("submit_stock_entry", {"name": draft["name"]})
		post_stock_entry_ledger(draft["name"])

		balance = self.tool_data("get_stock_balance", {"item_code": SPRAY, "warehouse": STORES})
		self.assertEqual(balance["total_qty"], 120.0)

		ledger = self.tool_data("get_stock_ledger", {"item_code": SPRAY, "warehouse": STORES})
		self.assertEqual(ledger["movements"][0]["qty_change"], 40.0)
		self.assertEqual(ledger["movements"][0]["balance_qty"], 120.0)
		self.assertEqual(ledger["movements"][0]["voucher_no"], draft["name"])

	def test_a_submitted_transfer_moves_both_ends(self):
		draft = self.tool_data("create_stock_entry", _transfer())
		self.tool_data("submit_stock_entry", {"name": draft["name"]})
		post_stock_entry_ledger(draft["name"])

		balance = self.tool_data("get_stock_balance", {"item_code": SPRAY, "company": MAIN})
		by_warehouse = {row["warehouse"]: row["qty"] for row in balance["balances"]}
		self.assertEqual(by_warehouse[STORES], 70.0)
		self.assertEqual(by_warehouse[SHOP], 55.0)
		# A transfer moves stock between sheds; it creates none.
		self.assertEqual(balance["total_qty"], 125.0)


class WritesAreOffByDefault(StockTestCase):
	"""No `configure(**ALL_ON)`: the out-of-the-box posture."""

	def test_create_is_refused_until_an_operator_enables_it(self):
		message = self.tool_error("create_stock_entry", _receipt())
		self.assertIn("create_stock_entry", message)
		self.assertEqual(STORE.rows("Stock Entry"), [])

	def test_submit_is_refused_until_an_operator_enables_it(self):
		self.assertIn("submit_stock_entry", self.tool_error("submit_stock_entry", {"name": "X"}))

	def test_set_reorder_level_is_refused_until_an_operator_enables_it(self):
		message = self.tool_error(
			"set_reorder_level",
			{"item_code": SPRAY, "warehouse": STORES, "reorder_level": 1, "reorder_qty": 1},
		)
		self.assertIn("set_reorder_level", message)

	def test_the_reads_are_on(self):
		self.assertTrue(self.tool_data("get_stock_balance", {"item_code": SPRAY}))
		self.assertTrue(self.tool_data("get_warehouse_summary", {"warehouse": STORES}))


class GetStockEntry(WriteEnabledTestCase):
	def test_it_reads_back_every_line(self):
		draft = self.tool_data(
			"create_stock_entry",
			_receipt(
				items=[
					{"item_code": SPRAY, "qty": 40, "warehouse": STORES, "basic_rate": 2.5},
					{"item_code": TWINE, "qty": 6, "warehouse": STORES, "basic_rate": 10.0},
				]
			),
		)
		data = self.tool_data("get_stock_entry", {"name": draft["name"]})
		self.assertEqual(data["item_count"], 2)
		self.assertEqual(data["status"], "Draft")
		self.assertEqual(data["total_qty"], 46.0)
		self.assertEqual(data["total_value"], 160.0)

	def test_an_unknown_name_is_refused(self):
		self.assertIn("NOPE", self.tool_error("get_stock_entry", {"name": "NOPE"}))


class ListStockEntries(WriteEnabledTestCase):
	def setUp(self):
		super().setUp()
		self.receipt = self.tool_data("create_stock_entry", _receipt())["name"]
		self.transfer = self.tool_data("create_stock_entry", _transfer())["name"]
		self.twine = self.tool_data(
			"create_stock_entry",
			_receipt(items=[{"item_code": TWINE, "qty": 3, "warehouse": SHOP}]),
		)["name"]

	def names(self, **args):
		data = self.tool_data("list_stock_entries", {"company": MAIN, **args})
		return {row["name"] for row in data["entries"]}

	def test_it_lists_every_entry_for_the_company(self):
		self.assertEqual(self.names(), {self.receipt, self.transfer, self.twine})

	def test_entry_type_narrows_it(self):
		self.assertEqual(self.names(entry_type="Material Transfer"), {self.transfer})

	def test_item_code_filters_on_the_lines(self):
		self.assertEqual(self.names(item_code=TWINE), {self.twine})

	def test_warehouse_matches_either_end_of_a_transfer(self):
		self.assertEqual(self.names(warehouse=SHOP), {self.transfer, self.twine})
		self.assertEqual(self.names(warehouse=STORES), {self.receipt, self.transfer})

	def test_a_line_filter_that_matches_nothing_is_empty_not_unfiltered(self):
		data = self.tool_data("list_stock_entries", {"company": MAIN, "item_code": RETIRED_ITEM})
		self.assertEqual(data["entries"], [])
		self.assertIn("empty result", data["note"])

	def test_a_reversed_date_range_is_refused(self):
		message = self.tool_error(
			"list_stock_entries",
			{"company": MAIN, "from_date": "2026-08-01", "to_date": "2026-07-01"},
		)
		self.assertIn("is after", message)


class GetStockBalance(StockTestCase):
	def test_it_totals_every_warehouse_holding_the_item(self):
		data = self.tool_data("get_stock_balance", {"item_code": SPRAY})
		self.assertEqual(data["item_name"], "Surround WP")
		self.assertEqual(data["uom"], "Lb")
		self.assertEqual(data["warehouse_count"], 3)
		self.assertEqual(data["total_qty"], 625.0)
		self.assertEqual(data["total_value"], 1812.5)

	def test_company_scopes_it_through_the_warehouses(self):
		"""Bin has no company column — see the fixture note on that."""
		data = self.tool_data("get_stock_balance", {"item_code": SPRAY, "company": MAIN})
		self.assertEqual(data["warehouse_count"], 2)
		self.assertEqual(data["total_qty"], 125.0)
		self.assertNotIn(OTHER_STORES, [row["warehouse"] for row in data["balances"]])

	def test_one_warehouse_reports_that_warehouse(self):
		data = self.tool_data("get_stock_balance", {"item_code": SPRAY, "warehouse": SHOP})
		self.assertEqual(data["total_qty"], 45.0)
		self.assertEqual(data["balances"][0]["valuation_rate"], 2.5)
		self.assertEqual(data["balances"][0]["stock_value"], 112.5)

	def test_an_item_that_never_moved_says_so_rather_than_reporting_zero(self):
		data = self.tool_data("get_stock_balance", {"item_code": TWINE})
		self.assertEqual(data["balances"], [])
		self.assertEqual(data["total_qty"], 0)
		self.assertIn("never moved", data["note"])

	def test_it_resolves_an_item_by_its_name(self):
		data = self.tool_data("get_stock_balance", {"item_code": "Surround WP"})
		self.assertEqual(data["item_code"], SPRAY)

	def test_a_warehouse_outside_the_named_company_is_refused(self):
		message = self.tool_error(
			"get_stock_balance", {"item_code": SPRAY, "warehouse": OTHER_STORES, "company": MAIN}
		)
		self.assertIn(OTHER, message)


class GetStockLedger(StockTestCase):
	def test_it_returns_the_movements_newest_first(self):
		data = self.tool_data("get_stock_ledger", {"item_code": SPRAY})
		self.assertEqual([row["posting_date"] for row in data["movements"]], ["2026-07-15", "2026-06-01"])
		self.assertEqual(data["movements"][0]["qty_change"], -120.0)
		self.assertEqual(data["movements"][0]["balance_qty"], 80.0)
		self.assertEqual(data["movements"][0]["voucher_type"], "Stock Entry")
		# v0.211.0: the name, live from the Item — the code alone names nothing.
		self.assertEqual({row["item_name"] for row in data["movements"]}, {"Surround WP"})
		frappe.db.set_value("Item", SPRAY, "item_name", "Surround WP Crop Protectant (25 lb)")
		renamed = self.tool_data("get_stock_ledger", {"item_code": SPRAY})
		self.assertEqual(renamed["movements"][0]["item_name"], "Surround WP Crop Protectant (25 lb)")
		self.assertEqual(renamed["movements"][0]["item_code"], SPRAY)

	def test_a_cancelled_movement_is_excluded(self):
		"""The SHOP row in the fixture is cancelled; it never happened."""
		data = self.tool_data("get_stock_ledger", {"warehouse": SHOP})
		self.assertEqual(data["movements"], [])
		self.assertEqual(data["net_qty_change"], 0)

	def test_the_net_change_is_over_the_rows_returned(self):
		data = self.tool_data("get_stock_ledger", {"item_code": SPRAY})
		self.assertEqual(data["net_qty_change"], 80.0)

	def test_a_date_range_narrows_it(self):
		data = self.tool_data(
			"get_stock_ledger", {"item_code": SPRAY, "from_date": "2026-07-01", "to_date": "2026-07-31"}
		)
		self.assertEqual(data["count"], 1)
		self.assertEqual(data["movements"][0]["posting_date"], "2026-07-15")

	def test_a_reversed_date_range_is_refused(self):
		message = self.tool_error("get_stock_ledger", {"from_date": "2026-08-01", "to_date": "2026-07-01"})
		self.assertIn("is after", message)


class GetWarehouseSummary(StockTestCase):
	def test_it_lists_what_is_on_hand_with_the_reorder_rule(self):
		data = self.tool_data("get_warehouse_summary", {"warehouse": STORES})
		self.assertEqual(data["company"], MAIN)
		self.assertEqual(data["item_count"], 1)
		row = data["items"][0]
		self.assertEqual(row["item_code"], SPRAY)
		self.assertEqual(row["qty"], 80.0)
		self.assertEqual(row["reorder_level"], 100.0)
		self.assertEqual(row["reorder_qty"], 250.0)
		self.assertTrue(row["below_reorder"])
		self.assertEqual(data["below_reorder_count"], 1)
		self.assertEqual(data["total_value"], 200.0)

	def test_a_warehouse_with_no_rule_reports_none_rather_than_zero(self):
		data = self.tool_data("get_warehouse_summary", {"warehouse": SHOP})
		row = data["items"][0]
		self.assertIsNone(row["reorder_level"])
		self.assertFalse(row["below_reorder"])

	def test_an_unknown_warehouse_is_refused(self):
		message = self.tool_error("get_warehouse_summary", {"warehouse": "Nowhere - ETC"})
		self.assertIn("list_warehouses", message)

	def test_a_warehouse_outside_the_named_company_is_refused(self):
		message = self.tool_error("get_warehouse_summary", {"warehouse": OTHER_STORES, "company": MAIN})
		self.assertIn(OTHER, message)


class SetReorderLevel(WriteEnabledTestCase):
	def test_it_writes_a_rule_where_there_was_none(self):
		data = self.tool_data(
			"set_reorder_level",
			{"item_code": SPRAY, "warehouse": SHOP, "reorder_level": 30, "reorder_qty": 60},
		)
		self.assertEqual(data["warehouse"], SHOP)
		self.assertEqual(data["reorder_level"], 30.0)
		self.assertEqual(data["reorder_qty"], 60.0)
		self.assertTrue(data["created"])
		summary = self.tool_data("get_warehouse_summary", {"warehouse": SHOP})
		self.assertEqual(summary["items"][0]["reorder_level"], 30.0)

	def test_it_updates_the_rule_that_is_already_there(self):
		data = self.tool_data(
			"set_reorder_level",
			{"item_code": SPRAY, "warehouse": STORES, "reorder_level": 150, "reorder_qty": 300},
		)
		self.assertFalse(data["created"])
		self.assertEqual(data["reorder_level"], 150.0)
		rules = STORE.get_raw("Item", SPRAY)["reorder_levels"]
		self.assertEqual(len(rules), 1)

	def test_a_negative_level_is_refused(self):
		message = self.tool_error(
			"set_reorder_level",
			{"item_code": SPRAY, "warehouse": STORES, "reorder_level": -1, "reorder_qty": 5},
		)
		self.assertIn("cannot be negative", message)

	def test_a_zero_order_quantity_is_refused(self):
		message = self.tool_error(
			"set_reorder_level",
			{"item_code": SPRAY, "warehouse": STORES, "reorder_level": 10, "reorder_qty": 0},
		)
		self.assertIn("must be positive", message)

	def test_an_unknown_warehouse_is_refused(self):
		message = self.tool_error(
			"set_reorder_level",
			{"item_code": SPRAY, "warehouse": "Nowhere - ETC", "reorder_level": 1, "reorder_qty": 1},
		)
		self.assertIn("Nowhere - ETC", message)


class ListReorderAlerts(StockTestCase):
	def test_it_reports_the_item_below_its_level(self):
		data = self.tool_data("list_reorder_alerts", {})
		by_item = {row["item_code"]: row for row in data["alerts"]}
		self.assertEqual(by_item[SPRAY]["current_qty"], 80.0)
		self.assertEqual(by_item[SPRAY]["reorder_level"], 100.0)
		self.assertEqual(by_item[SPRAY]["shortfall"], 20.0)
		self.assertEqual(by_item[SPRAY]["warehouse"], STORES)

	def test_an_item_with_a_rule_and_no_bin_row_is_reported_at_zero(self):
		"""The opposite of `get_stock_balance`'s treatment, and deliberately."""
		data = self.tool_data("list_reorder_alerts", {})
		by_item = {row["item_code"]: row for row in data["alerts"]}
		self.assertEqual(by_item[TWINE]["current_qty"], 0)
		self.assertEqual(by_item[TWINE]["shortfall"], 20.0)

	def test_the_worst_shortfall_comes_first(self):
		STORE.get_raw("Bin", f"{SPRAY}-{STORES}")["actual_qty"] = 5.0
		data = self.tool_data("list_reorder_alerts", {})
		self.assertEqual(data["alerts"][0]["item_code"], SPRAY)
		self.assertEqual(data["alerts"][0]["shortfall"], 95.0)

	def test_an_item_above_its_level_is_not_an_alert(self):
		STORE.get_raw("Bin", f"{SPRAY}-{STORES}")["actual_qty"] = 500.0
		data = self.tool_data("list_reorder_alerts", {})
		self.assertNotIn(SPRAY, [row["item_code"] for row in data["alerts"]])
		self.assertEqual(data["rules_checked"], 2)

	def test_a_warehouse_filter_narrows_it_to_that_shed(self):
		data = self.tool_data("list_reorder_alerts", {"warehouse": STORES})
		self.assertEqual([row["item_code"] for row in data["alerts"]], [SPRAY])

	def test_a_disabled_item_is_not_something_to_reorder(self):
		STORE.get_raw("Item", RETIRED_ITEM)["reorder_levels"] = [
			{
				"name": "retired-reorder-1",
				"parent": RETIRED_ITEM,
				"parenttype": "Item",
				"parentfield": "reorder_levels",
				"idx": 1,
				"warehouse": STORES,
				"warehouse_reorder_level": 999.0,
				"warehouse_reorder_qty": 10.0,
			}
		]
		data = self.tool_data("list_reorder_alerts", {})
		self.assertNotIn(RETIRED_ITEM, [row["item_code"] for row in data["alerts"]])

	def test_company_scopes_the_rules_through_their_warehouses(self):
		data = self.tool_data("list_reorder_alerts", {"company": OTHER})
		self.assertEqual(data["alerts"], [])


class ReadToolsWriteNothing(StockTestCase):
	"""Every read tool here, run once, must leave the store byte-identical."""

	def test_the_reads_change_nothing(self):
		import copy

		before = copy.deepcopy(STORE.tables)
		self.tool_data("get_stock_balance", {"item_code": SPRAY})
		self.tool_data("get_stock_ledger", {"item_code": SPRAY})
		self.tool_data("get_warehouse_summary", {"warehouse": STORES})
		self.tool_data("list_reorder_alerts", {})
		self.tool_data("list_stock_entries", {"company": MAIN})
		after = {key: value for key, value in STORE.tables.items() if key != "MCP Action Log"}
		self.assertEqual({key: value for key, value in before.items() if key != "MCP Action Log"}, after)


if __name__ == "__main__":
	import unittest

	unittest.main()

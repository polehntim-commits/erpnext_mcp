# SPDX-License-Identifier: MIT
"""Purchase Order, Purchase Receipt, Purchase Invoice, Payment Entry and AP ageing."""

from .fixtures import (
	MAIN,
	MASTER_SUPPLIER,
	RETIRED_SUPPLIER,
	SPRAY,
	STORES,
	PurchasingTestCase,
	cash,
	payable,
	supplies,
)
from .harness import STORE, post_payment_entry_gl, post_purchase_invoice_gl

#: Every mutating tool this module adds. Read tools default on and need no
#: override; these default off, so any test that expects one to actually run
#: has to turn it on first — `WriteEnabledTestCase` does that once for every
#: class below that exercises a create or a submit.
ALL_ON = {
	f"allow_{name}": 1
	for name in (
		"create_purchase_order",
		"submit_purchase_order",
		"create_purchase_receipt",
		"submit_purchase_receipt",
		"create_purchase_invoice",
		"submit_purchase_invoice",
		"delete_draft_purchase_invoice",
		"create_payment_entry",
		"submit_payment_entry",
	)
}


class WriteEnabledTestCase(PurchasingTestCase):
	def setUp(self):
		super().setUp()
		self.configure(**ALL_ON)


def _po_args(**overrides):
	args = {
		"company": MAIN,
		"supplier": MASTER_SUPPLIER,
		"schedule_date": "2026-09-01",
		"items": [{"item_code": SPRAY, "qty": 10, "rate": 12.5, "warehouse": STORES}],
	}
	args.update(overrides)
	return args


def _pi_args(**overrides):
	args = {
		"company": MAIN,
		"supplier": MASTER_SUPPLIER,
		"due_date": "2026-09-01",
		"items": [{"item_code": SPRAY, "qty": 4, "rate": 25.0, "expense_account": supplies()}],
	}
	args.update(overrides)
	return args


class PurchaseOrders(WriteEnabledTestCase):
	def test_creates_a_draft_with_a_computed_grand_total(self):
		data = self.tool_data("create_purchase_order", _po_args())
		self.assertEqual(data["docstatus"], 0)
		self.assertEqual(data["grand_total"], 125.0)
		self.assertTrue(data["name"])

	def test_qty_must_be_positive(self):
		message = self.tool_error(
			"create_purchase_order",
			_po_args(items=[{"item_code": SPRAY, "qty": 0, "rate": 1, "warehouse": STORES}]),
		)
		self.assertIn("qty", message)

	def test_unknown_item_is_refused_by_name(self):
		message = self.tool_error(
			"create_purchase_order",
			_po_args(items=[{"item_code": "NOPE", "qty": 1, "rate": 1, "warehouse": STORES}]),
		)
		self.assertIn("no Item called", message)

	def test_unknown_supplier_is_refused_by_name(self):
		message = self.tool_error("create_purchase_order", _po_args(supplier="Nobody Supply Co"))
		self.assertIn("no Supplier called", message)

	def test_line_schedule_date_falls_back_to_the_header(self):
		data = self.tool_data("create_purchase_order", _po_args())
		read = self.tool_data("get_purchase_order", {"name": data["name"]})
		self.assertEqual(read["items"][0]["schedule_date"], "2026-09-01")

	def test_get_reads_the_seeded_fixture(self):
		data = self.tool_data("get_purchase_order", {"name": "PUR-ORD-2026-00002"})
		self.assertEqual(data["supplier"], "Second Supplier LLC")
		self.assertEqual(data["docstatus_label"], "submitted")

	def test_get_missing_order_is_refused(self):
		message = self.tool_error("get_purchase_order", {"name": "NOPE"})
		self.assertIn("no Purchase Order", message)

	def test_submit_moves_docstatus_and_computes_status(self):
		created = self.tool_data("create_purchase_order", _po_args())
		data = self.tool_data("submit_purchase_order", {"name": created["name"]})
		self.assertEqual(data["docstatus"], 1)
		self.assertEqual(data["status"], "To Receive and Bill")

	def test_submitting_twice_is_refused(self):
		created = self.tool_data("create_purchase_order", _po_args())
		self.tool_data("submit_purchase_order", {"name": created["name"]})
		message = self.tool_error("submit_purchase_order", {"name": created["name"]})
		self.assertIn("already submitted", message)

	def test_submitting_a_missing_order_is_refused(self):
		message = self.tool_error("submit_purchase_order", {"name": "NOPE"})
		self.assertIn("no Purchase Order", message)


class PurchaseReceipts(WriteEnabledTestCase):
	def test_creates_a_draft_without_an_order(self):
		created = self.tool_data(
			"create_purchase_receipt",
			{
				"company": MAIN,
				"supplier": MASTER_SUPPLIER,
				"items": [{"item_code": SPRAY, "qty": 5, "rate": 2.5, "warehouse": STORES}],
			},
		)
		self.assertEqual(created["docstatus"], 0)
		self.assertEqual(created["grand_total"], 12.5)

	def test_purchase_order_supplier_mismatch_is_refused(self):
		# PUR-ORD-2026-00002 (seeded, submitted) is against "Second Supplier LLC".
		message = self.tool_error(
			"create_purchase_receipt",
			{
				"company": MAIN,
				"supplier": MASTER_SUPPLIER,
				"purchase_order": "PUR-ORD-2026-00002",
				"items": [{"item_code": SPRAY, "qty": 1, "warehouse": STORES}],
			},
		)
		self.assertIn("Second Supplier LLC", message)

	def test_receipt_against_a_draft_order_is_refused(self):
		# PUR-ORD-2026-00003 (seeded) is against MASTER_SUPPLIER but docstatus 0.
		message = self.tool_error(
			"create_purchase_receipt",
			{
				"company": MAIN,
				"supplier": MASTER_SUPPLIER,
				"purchase_order": "PUR-ORD-2026-00003",
				"items": [{"item_code": SPRAY, "qty": 1, "warehouse": STORES}],
			},
		)
		self.assertIn("not submitted", message)

	def test_receipt_against_a_submitted_order_for_the_same_supplier(self):
		order = self.tool_data("create_purchase_order", _po_args())
		self.tool_data("submit_purchase_order", {"name": order["name"]})
		created = self.tool_data(
			"create_purchase_receipt",
			{
				"company": MAIN,
				"supplier": MASTER_SUPPLIER,
				"purchase_order": order["name"],
				"items": [{"item_code": SPRAY, "qty": 3, "rate": 12.5, "warehouse": STORES}],
			},
		)
		self.assertEqual(created["purchase_order"], order["name"])

	def test_get_reads_a_seeded_receipt(self):
		data = self.tool_data("get_purchase_receipt", {"name": "PUR-RCPT-2026-00001"})
		self.assertEqual(data["supplier"], MASTER_SUPPLIER)
		self.assertEqual(data["purchase_order"], "PUR-ORD-2026-00002")

	def test_list_filters_by_supplier(self):
		data = self.tool_data("list_purchase_receipts", {"supplier": MASTER_SUPPLIER})
		self.assertEqual(data["count"], 2)

	def test_submit_moves_docstatus_and_status(self):
		created = self.tool_data(
			"create_purchase_receipt",
			{
				"company": MAIN,
				"supplier": MASTER_SUPPLIER,
				"items": [{"item_code": SPRAY, "qty": 1, "rate": 1, "warehouse": STORES}],
			},
		)
		data = self.tool_data("submit_purchase_receipt", {"name": created["name"]})
		self.assertEqual(data["docstatus"], 1)
		self.assertEqual(data["status"], "To Bill")

	def test_submitting_twice_is_refused(self):
		# PUR-RCPT-2026-00001 is seeded docstatus 1 already — submitting it once
		# more is the refusal under test, with no live submit needed first.
		message = self.tool_error("submit_purchase_receipt", {"name": "PUR-RCPT-2026-00001"})
		self.assertIn("already submitted", message)


class PurchaseInvoices(WriteEnabledTestCase):
	def test_expense_account_is_required_per_line(self):
		message = self.tool_error(
			"create_purchase_invoice",
			_pi_args(items=[{"item_code": SPRAY, "qty": 1, "rate": 1}]),
		)
		self.assertIn("expense_account", message)

	def test_creates_a_draft_with_computed_totals_and_default_payable(self):
		data = self.tool_data("create_purchase_invoice", _pi_args())
		self.assertEqual(data["grand_total"], 100.0)
		self.assertEqual(data["outstanding_amount"], 100.0)
		self.assertEqual(data["credit_to"], payable())

	def test_explicit_credit_to_is_honoured(self):
		data = self.tool_data("create_purchase_invoice", _pi_args(credit_to=payable()))
		self.assertEqual(data["credit_to"], payable())

	def test_get_reads_a_seeded_invoice(self):
		data = self.tool_data("get_purchase_invoice", {"name": "ACC-PINV-2026-00001"})
		self.assertEqual(data["supplier"], MASTER_SUPPLIER)
		self.assertEqual(data["grand_total"], 1000)

	def test_list_outstanding_only_excludes_the_settled_invoice(self):
		data = self.tool_data("list_purchase_invoices", {"outstanding_only": True, "company": MAIN})
		names = [row["name"] for row in data["invoices"]]
		self.assertNotIn("ACC-PINV-2026-00007", names)

	def test_list_filters_by_supplier(self):
		data = self.tool_data("list_purchase_invoices", {"supplier": RETIRED_SUPPLIER})
		self.assertEqual(data["count"], 2)

	def test_submit_moves_docstatus_and_sets_status(self):
		created = self.tool_data("create_purchase_invoice", _pi_args())
		data = self.tool_data("submit_purchase_invoice", {"name": created["name"]})
		self.assertEqual(data["docstatus"], 1)
		self.assertEqual(data["outstanding_amount"], data["grand_total"])
		self.assertEqual(data["status"], "Unpaid")

	def test_submitting_a_missing_invoice_is_refused(self):
		message = self.tool_error("submit_purchase_invoice", {"name": "NOPE"})
		self.assertIn("no Purchase Invoice", message)

	def test_posted_gl_entries_debit_expense_and_credit_payable(self):
		created = self.tool_data("create_purchase_invoice", _pi_args())
		self.tool_data("submit_purchase_invoice", {"name": created["name"]})
		rows = post_purchase_invoice_gl(created["name"])
		debits = {row["account"]: row["debit"] for row in rows if row["debit"]}
		credits = {row["account"]: row["credit"] for row in rows if row["credit"]}
		self.assertEqual(debits[supplies()], 100.0)
		self.assertEqual(credits[payable()], 100.0)


class DeleteDraftPurchaseInvoice(WriteEnabledTestCase):
	def draft(self, **overrides):
		return self.tool_data("create_purchase_invoice", _pi_args(**overrides))["name"]

	def payload(self, name, **overrides):
		values = {"name": name, "reason": "OCR matched the receipt to the wrong supplier"}
		values.update(overrides)
		return values

	def test_it_deletes_the_draft(self):
		name = self.draft()
		data = self.tool_data("delete_draft_purchase_invoice", self.payload(name))
		self.assertEqual(data["deleted"]["name"], name)
		self.assertIsNone(STORE.get_raw("Purchase Invoice", name))

	def test_it_reports_what_it_deleted_because_nothing_else_will(self):
		name = self.draft()
		deleted = self.tool_data("delete_draft_purchase_invoice", self.payload(name))["deleted"]
		self.assertEqual(deleted["company"], MAIN)
		self.assertEqual(deleted["supplier"], MASTER_SUPPLIER)
		self.assertTrue(deleted["posting_date"])
		self.assertEqual(deleted["grand_total"], 100.0)
		self.assertEqual(deleted["line_count"], 1)
		line = deleted["items"][0]
		self.assertEqual(line["item_code"], SPRAY)
		self.assertEqual(line["qty"], 4)
		self.assertEqual(line["rate"], 25.0)
		self.assertEqual(line["expense_account"], supplies())
		self.assertIn("cost_center", line)

	def test_the_audit_row_carries_the_reason_and_the_invoice(self):
		name = self.draft()
		self.tool_data("delete_draft_purchase_invoice", self.payload(name))
		row = self.assertAudited("delete_draft_purchase_invoice", status="Success")
		self.assertIn(f"deleted draft Purchase Invoice {name}", row["result_summary"])
		self.assertIn(MASTER_SUPPLIER, row["result_summary"])
		self.assertIn("wrong supplier", row["result_summary"])
		self.assertEqual(row["docstatus_delta"], "0 (draft) → deleted")

	def test_a_submitted_invoice_is_refused_and_pointed_at_cancel(self):
		message = self.tool_error("delete_draft_purchase_invoice", self.payload("ACC-PINV-2026-00001"))
		self.assertIn("Cancel it", message)
		self.assertIn("Nothing was deleted", message)
		self.assertIsNotNone(STORE.get_raw("Purchase Invoice", "ACC-PINV-2026-00001"))

	def test_a_cancelled_invoice_is_refused(self):
		name = self.draft()
		STORE.tables["Purchase Invoice"][name]["docstatus"] = 2
		message = self.tool_error("delete_draft_purchase_invoice", self.payload(name))
		self.assertIn("audit trail with a hole in it", message)
		self.assertIsNotNone(STORE.get_raw("Purchase Invoice", name))

	def test_an_invoice_that_does_not_exist_is_refused(self):
		message = self.tool_error("delete_draft_purchase_invoice", self.payload("ACC-PINV-NOPE"))
		self.assertIn("no Purchase Invoice called", message)

	def test_a_placeholder_reason_is_refused(self):
		name = self.draft()
		message = self.tool_error("delete_draft_purchase_invoice", self.payload(name, reason="x"))
		self.assertIn("real explanation", message)
		self.assertIsNotNone(STORE.get_raw("Purchase Invoice", name))

	def test_a_missing_reason_is_refused(self):
		name = self.draft()
		message = self.tool_error("delete_draft_purchase_invoice", {"name": name})
		self.assertIn("reason is required", message)

	def test_an_invoice_no_receipt_names_releases_nothing(self):
		data = self.tool_data("delete_draft_purchase_invoice", self.payload(self.draft()))
		self.assertEqual(data["receipts_released"], [])
		self.assertIn("nothing else changed", data["next_step"])

	def test_the_switch_keeps_it_shut(self):
		name = self.draft()
		self.configure(enabled=1, allow_create_purchase_invoice=1)
		message = self.tool_error("delete_draft_purchase_invoice", self.payload(name))
		self.assertIn("allow_delete_draft_purchase_invoice", message)
		self.assertIsNotNone(STORE.get_raw("Purchase Invoice", name))


def _pe_args(**overrides):
	args = {
		"company": MAIN,
		"supplier": MASTER_SUPPLIER,
		"paid_from": cash(),
		"paid_amount": 500,
	}
	args.update(overrides)
	return args


class PaymentEntries(WriteEnabledTestCase):
	def test_allocates_against_a_submitted_invoice(self):
		data = self.tool_data(
			"create_payment_entry",
			_pe_args(references=[{"reference_name": "ACC-PINV-2026-00002", "allocated_amount": 500}]),
		)
		self.assertEqual(data["allocated_total"], 500)
		self.assertEqual(data["unallocated_amount"], 0)
		self.assertEqual(data["paid_to"], payable())
		self.assertEqual(data["paid_from"], cash())

	def test_on_account_payment_needs_no_reference(self):
		data = self.tool_data("create_payment_entry", _pe_args(paid_amount=200))
		self.assertEqual(data["allocated_total"], 0)
		self.assertEqual(data["unallocated_amount"], 200)

	def test_allocated_amount_over_outstanding_is_refused(self):
		message = self.tool_error(
			"create_payment_entry",
			_pe_args(
				paid_amount=999999,
				references=[{"reference_name": "ACC-PINV-2026-00002", "allocated_amount": 999999}],
			),
		)
		self.assertIn("exceeds", message)

	def test_reference_to_another_suppliers_invoice_is_refused(self):
		message = self.tool_error(
			"create_payment_entry",
			_pe_args(
				paid_amount=100,
				references=[{"reference_name": "ACC-PINV-2026-00005", "allocated_amount": 100}],
			),
		)
		self.assertIn("billed to", message)

	def test_total_allocated_over_paid_amount_is_refused(self):
		message = self.tool_error(
			"create_payment_entry",
			_pe_args(
				paid_amount=100,
				references=[{"reference_name": "ACC-PINV-2026-00002", "allocated_amount": 500}],
			),
		)
		self.assertIn("references allocate", message)

	def test_submit_reduces_the_invoices_outstanding_amount(self):
		created = self.tool_data(
			"create_payment_entry",
			_pe_args(references=[{"reference_name": "ACC-PINV-2026-00002", "allocated_amount": 500}]),
		)
		self.tool_data("submit_payment_entry", {"name": created["name"]})
		invoice = self.tool_data("get_purchase_invoice", {"name": "ACC-PINV-2026-00002"})
		self.assertEqual(invoice["outstanding_amount"], 0.0)
		self.assertEqual(invoice["status"], "Paid")

	def test_partial_payment_leaves_a_remainder(self):
		created = self.tool_data(
			"create_payment_entry",
			_pe_args(
				paid_amount=300,
				references=[{"reference_name": "ACC-PINV-2026-00002", "allocated_amount": 300}],
			),
		)
		self.tool_data("submit_payment_entry", {"name": created["name"]})
		invoice = self.tool_data("get_purchase_invoice", {"name": "ACC-PINV-2026-00002"})
		self.assertEqual(invoice["outstanding_amount"], 200.0)

	def test_get_reads_back_the_references(self):
		created = self.tool_data(
			"create_payment_entry",
			_pe_args(references=[{"reference_name": "ACC-PINV-2026-00002", "allocated_amount": 500}]),
		)
		data = self.tool_data("get_payment_entry", {"name": created["name"]})
		self.assertEqual(data["references"][0]["reference_name"], "ACC-PINV-2026-00002")

	def test_list_is_pay_and_supplier_only(self):
		self.tool_data("create_payment_entry", _pe_args(paid_amount=50))
		data = self.tool_data("list_payment_entries", {"supplier": MASTER_SUPPLIER})
		self.assertEqual(data["count"], 1)

	def test_posted_gl_entries_debit_payable_and_credit_cash(self):
		created = self.tool_data("create_payment_entry", _pe_args(paid_amount=150))
		self.tool_data("submit_payment_entry", {"name": created["name"]})
		rows = post_payment_entry_gl(created["name"])
		debits = {row["account"]: row["debit"] for row in rows if row["debit"]}
		credits = {row["account"]: row["credit"] for row in rows if row["credit"]}
		self.assertEqual(debits[payable()], 150.0)
		self.assertEqual(list(credits.values()), [150.0])


class APAgeing(PurchasingTestCase):
	def test_buckets_are_placed_by_days_overdue(self):
		data = self.tool_data(
			"get_ap_aging", {"company": MAIN, "supplier": MASTER_SUPPLIER, "as_of": "2026-07-24"}
		)
		by_name = {row["name"]: row["ageing_bucket"] for row in data["suppliers"][0]["invoices"]}
		self.assertEqual(by_name["ACC-PINV-2026-00001"], "current")
		self.assertEqual(by_name["ACC-PINV-2026-00002"], "0-30")
		self.assertEqual(by_name["ACC-PINV-2026-00003"], "31-60")
		self.assertEqual(by_name["ACC-PINV-2026-00004"], "61-90")

	def test_the_settled_invoice_is_excluded_entirely(self):
		data = self.tool_data("get_ap_aging", {"company": MAIN, "supplier": MASTER_SUPPLIER})
		names = [row["name"] for row in data["suppliers"][0]["invoices"]]
		self.assertNotIn("ACC-PINV-2026-00007", names)

	def test_no_due_date_is_unknown_not_current(self):
		data = self.tool_data("get_ap_aging", {"company": MAIN, "supplier": RETIRED_SUPPLIER})
		by_name = {row["name"]: row for row in data["suppliers"][0]["invoices"]}
		self.assertEqual(by_name["ACC-PINV-2026-00006"]["ageing_bucket"], "unknown")
		self.assertIsNone(by_name["ACC-PINV-2026-00006"]["days_overdue"])
		self.assertEqual(by_name["ACC-PINV-2026-00005"]["ageing_bucket"], "90+")

	def test_gl_balance_matches_open_invoices_when_nothing_odd_happened(self):
		data = self.tool_data("get_ap_aging", {"company": MAIN, "supplier": MASTER_SUPPLIER})
		row = data["suppliers"][0]
		self.assertEqual(row["outstanding"], 8500.0)
		self.assertEqual(row["gl_balance"], 8500.0)
		self.assertNotIn("drift", row)

	def test_a_manual_journal_entry_against_payable_shows_up_as_drift(self):
		STORE.seed(
			"GL Entry",
			[
				{
					"name": "GL-WRITEOFF-1",
					"account": payable(),
					"posting_date": "2026-07-01",
					"debit": 0.0,
					"credit": 250.0,
					"company": MAIN,
					"is_cancelled": 0,
					"voucher_type": "Journal Entry",
					"voucher_no": "JE-WRITEOFF-1",
					"voucher_detail_no": "",
					"party_type": "Supplier",
					"party": MASTER_SUPPLIER,
					"cost_center": None,
					"is_opening": "No",
					"docstatus": 1,
				}
			],
		)
		data = self.tool_data("get_ap_aging", {"company": MAIN, "supplier": MASTER_SUPPLIER})
		row = data["suppliers"][0]
		self.assertEqual(row["gl_balance"], 8750.0)
		self.assertEqual(row["drift"], 250.0)
		self.assertIn("drift_note", row)

	def test_totals_across_every_supplier(self):
		data = self.tool_data("get_ap_aging", {"company": MAIN})
		self.assertEqual(data["count"], 2)
		self.assertEqual(data["total_outstanding"], 14100.0)
		self.assertEqual(data["gl_total_outstanding"], 14100.0)

	def test_bucket_totals_add_up_to_the_grand_total(self):
		data = self.tool_data("get_ap_aging", {"company": MAIN})
		self.assertEqual(
			round(sum(b["outstanding"] for b in data["buckets"].values()), 2), data["total_outstanding"]
		)

	def test_no_payable_accounts_is_empty_not_an_error(self):
		STORE.tables["Account"] = {
			name: row
			for name, row in STORE.tables["Account"].items()
			if not (row.get("company") == MAIN and row.get("account_type") == "Payable")
		}
		data = self.tool_data("get_ap_aging", {"company": MAIN})
		self.assertEqual(data["suppliers"], [])
		self.assertIn("no Account typed Payable", data["note"])


class PurchasingToolsDoNotWriteWhenDisabled(PurchasingTestCase):
	def test_mutating_tools_are_off_by_default(self):
		message = self.tool_error("create_purchase_order", _po_args())
		self.assertIn("switched off", message)

	def test_read_tools_are_on_by_default(self):
		data = self.tool_data("list_purchase_receipts", {})
		self.assertIsInstance(data["count"], int)

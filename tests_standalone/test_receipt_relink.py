"""A filed receipt pointed at an invoice or journal entry that already exists. v0.256.0."""

import frappe

from erpnext_mcp.tools import expenses

from .harness import STORE
from .test_reimbursements import ON, ReimbursementTestCase

RELINK = {**ON, "allow_relink_expense_receipt": 1}


class RelinkCase(ReimbursementTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **RELINK)

	def billed(self, **overrides):
		receipt = self.approve(self.expense(**overrides))
		return receipt, self.tool_data("create_purchase_invoice_from_receipt", {"receipt": receipt})["purchase_invoice"]

	def link(self, receipt):
		return tuple(frappe.db.get_value("Expense Receipt", receipt, ["linked_doctype", "linked_document"]))

	def comments(self, receipt):
		return [r.get("content") for r in STORE.rows("Comment") if r.get("reference_name") == receipt]


class PointingAnUnlinkedReceipt(RelinkCase):
	def test_an_approved_receipt_links_to_an_invoice_entered_elsewhere(self):
		_, invoice = self.billed()
		other = self.approve(self.expense(merchant="Coastal Farm & Ranch (duplicate slip)"))
		data = self.tool_data("relink_expense_receipt", {"receipt": other, "linked_doctype": "Purchase Invoice",
		                                                 "linked_document": invoice})
		self.assertEqual(self.link(other), ("Purchase Invoice", invoice))
		self.assertIsNone(data["previous_link"]["document"])
		self.assertTrue(any("already the link of" in w for w in data["warnings"]), "a second receipt on one bill")
		self.assertTrue(any("draft" in w for w in data["warnings"]))
		self.assertTrue(any("nothing → Purchase Invoice" in c for c in self.comments(other)))

	def test_a_total_far_from_the_receipt_is_a_warning(self):
		_, invoice = self.billed(amount=84.50)
		small = self.approve(self.expense(amount=12.00))
		data = self.tool_data("relink_expense_receipt", {"receipt": small, "linked_doctype": "Purchase Invoice",
		                                                 "linked_document": invoice})
		self.assertTrue(any("totals 84.50" in w for w in data["warnings"]))


class MovingOrClearingALink(RelinkCase):
	def test_moving_needs_a_reason_and_the_old_draft_is_named(self):
		receipt, first = self.billed()
		other_receipt, second = self.billed(merchant="Wilco")
		error = self.tool_error("relink_expense_receipt", {"receipt": receipt, "linked_doctype": "Purchase Invoice",
		                                                   "linked_document": second})
		self.assertIn("reason is required", error)
		self.assertEqual(self.link(receipt), ("Purchase Invoice", first), "nothing changed")
		data = self.tool_data("relink_expense_receipt", {"receipt": receipt, "linked_doctype": "Purchase Invoice",
		                                                 "linked_document": second, "reason": "Billed on Wilco's statement"})
		self.assertEqual(data["previous_link"], {"doctype": "Purchase Invoice", "document": first})
		self.assertTrue(any(first in w and "delete_draft_purchase_invoice" in w for w in data["warnings"]))
		self.assertTrue(any("Reason: Billed on Wilco's statement" in c for c in self.comments(receipt)))

	def test_unlink_clears_with_a_reason(self):
		receipt, _ = self.billed()
		self.assertIn("reason", self.tool_error("relink_expense_receipt", {"receipt": receipt, "unlink": True}))
		self.tool_data("relink_expense_receipt", {"receipt": receipt, "unlink": True, "reason": "Wrong receipt billed"})
		self.assertEqual(self.link(receipt), ("", ""))
		self.assertIn("not linked", self.tool_error("relink_expense_receipt", {"receipt": receipt, "unlink": True,
		                                                                       "reason": "again, twice"}))


class WhatIsRefused(RelinkCase):
	def test_unapproved_wrong_type_missing_same_and_cancelled(self):
		receipt, invoice = self.billed()
		draft = self.expense()
		self.assertIn("not Approved", self.tool_error("relink_expense_receipt", {
			"receipt": draft, "linked_doctype": "Purchase Invoice", "linked_document": invoice}))
		self.assertIn("Purchase Invoice or Journal Entry", self.tool_error("relink_expense_receipt", {
			"receipt": receipt, "linked_doctype": "Sales Invoice", "linked_document": invoice, "reason": "testing it"}))
		self.assertIn("no Journal Entry", self.tool_error("relink_expense_receipt", {
			"receipt": receipt, "linked_doctype": "Journal Entry", "linked_document": "JV-NOPE", "reason": "testing it"}))
		self.assertIn("already linked", self.tool_error("relink_expense_receipt", {
			"receipt": receipt, "linked_doctype": "Purchase Invoice", "linked_document": invoice, "reason": "testing it"}))
		frappe.db.set_value("Purchase Invoice", invoice, "docstatus", 2)
		other = self.approve(self.expense())
		self.assertIn("cancelled", self.tool_error("relink_expense_receipt", {
			"receipt": other, "linked_doctype": "Purchase Invoice", "linked_document": invoice}))

	def test_a_vehicle_title_links_nothing(self):
		title = self.approve(self.expense(category=expenses.TITLE_CATEGORY))
		self.assertIn("vehicle document", self.tool_error("relink_expense_receipt", {
			"receipt": title, "linked_doctype": "Journal Entry", "linked_document": "x"}))

	def test_off_by_default(self):
		self.configure(allow_relink_expense_receipt=0)
		self.assertIn("allow_relink_expense_receipt", self.tool_error("relink_expense_receipt", {"receipt": "x"}))

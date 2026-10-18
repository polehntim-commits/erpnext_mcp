# SPDX-License-Identifier: MIT
"""Controller for Expense Receipt — what the machine read, and what a person owes.

v0.31.0. A foreman photographs a slip at the pump or the parts counter, on-device
Vision OCR reads the merchant, the total and the date off it, and the phone posts
the lot here. Everything below exists because the extracted fields are a MACHINE'S
READING OF A PHOTOGRAPH and are wrong often enough to plan for.

THE PHOTO AND THE RAW TEXT ARE KEPT BESIDE THE EXTRACTED FIELDS, not instead of
them. `merchant`, `amount` and `receipt_date` are what the app will reimburse
against; `receipt_image` and `ocr_raw_text` are what settles the argument when
somebody says the total was $84.10 and the field says $84.16. Neither is
derivable from the other after the fact, so both are stored.

`ocr_confidence` IS A SORTING KEY, NOT A GATE. A low-confidence receipt is not
invalid — a crumpled thermal slip photographed in the sun reads badly and is
still a real expense. It is a receipt an approver should open the photo for, and
`list_expense_receipts` sorts on it so the doubtful ones surface first. It is
range-checked to 0..1 here because a scanner that reports 87 meant 0.87 and a
field that silently accepts both makes the sort meaningless.

LINE TOTALS ARE DERIVED WHEN THE SCANNER COULD NOT READ THEM, and left alone when
it could. OCR reads line items far less reliably than it reads a bold total, so a
row that has a quantity and a unit price but no total gets the product; a row that
carries its own total keeps it, because a receipt that charges for four at $3 and
totals $11.50 after a discount is telling the truth and the multiplication is not.

THE LINE ITEMS ARE NEVER RECONCILED AGAINST `amount`, deliberately. Tax, tips,
deposits and core charges all live between the lines and the total, and a
validation that demanded they agree would reject most real receipts. The total is
the field that matters; the lines are detail somebody may want later.
"""

import frappe
from frappe import _
from frappe.model.document import Document

DRAFT = "Draft"
SUBMITTED = "Submitted"
APPROVED = "Approved"
REJECTED = "Rejected"

STATUSES = (DRAFT, SUBMITTED, APPROVED, REJECTED)

#: The statuses from which a review decision may still be taken. An Approved or
#: Rejected receipt has been decided, and deciding it again would overwrite the
#: name and date of whoever decided it first.
REVIEWABLE_STATUSES = (DRAFT, SUBMITTED)


class ExpenseReceipt(Document):
	def validate(self):
		self.validate_amount()
		self.validate_confidence()
		self.fill_in_line_totals()
		self.validate_who_paid()

	def validate_who_paid(self):
		"""v0.332.0. The Desk form's who-paid choice, held to the rules the tools apply."""
		if self.get("card_bank_account") and not self.get("paid_with"):
			self.paid_with = "Company card"
		if self.get("paid_with") == "Personal card" and self.get("card_bank_account"):
			frappe.throw(_("A personal card does not draw on a company bank account. Clear one of them."))
		if self.get("paid_with") == "Personal card" and int(self.get("covered_by_submitter") or 0):
			frappe.throw(
				_('"I\'m covering this" is for a purchase the company paid; untick it for a personal card.')
			)

	def on_update(self):
		"""v0.332.0. Company card + an account + an unknown number: learn the number once.

		Skipped when a tool is saving and learns for itself (`submit_expense_receipt`
		sets the flag), so a capture does not learn twice and report the second.
		"""
		if self.flags.get("who_paid_handled"):
			return
		try:
			from erpnext_mcp.tools import paid_with

			result = paid_with.learn_from_saved_receipt(self)
		except Exception:
			frappe.log_error(title="erpnext_mcp: card learning on save failed")
			return
		if result and result.get("action") == "created":
			frappe.msgprint(
				_("Card {0} saved on {1}.").format(self.get("card_last_four"), self.get("card_bank_account"))
			)
		elif result and result.get("action") == "pending":
			frappe.msgprint(
				_("Card {0} kept for a bookkeeper to confirm.").format(self.get("card_last_four"))
			)

	def validate_amount(self):
		"""A receipt total below zero is a refund, and this is not the doctype for one."""
		if self.amount is not None and float(self.amount or 0) < 0:
			frappe.throw(_("Amount cannot be negative. A refund is a credit note, not an expense receipt."))

	def validate_confidence(self):
		"""0..1, because a scanner reporting 87 meant 0.87.

		Refused rather than rescaled. A value of 87 is unambiguous, but 1.4 is not
		— it could be a percentage the client forgot to divide or a bug — and
		guessing between them would put a fabricated number in the field an
		approver uses to decide what to look at.
		"""
		if self.ocr_confidence in (None, ""):
			return
		confidence = float(self.ocr_confidence)
		if confidence < 0 or confidence > 1:
			frappe.throw(
				_("OCR Confidence is a fraction from 0 to 1, not a percentage — got {0}.").format(confidence)
			)

	def fill_in_line_totals(self):
		"""Quantity times unit price, only where the scanner left the total blank."""
		for row in self.get("items") or []:
			existing = row.get("line_total") if isinstance(row, dict) else getattr(row, "line_total", None)
			if existing not in (None, "", 0, 0.0):
				continue
			quantity = float((row.get("quantity") if isinstance(row, dict) else row.quantity) or 0)
			unit_price = float((row.get("unit_price") if isinstance(row, dict) else row.unit_price) or 0)
			if not quantity or not unit_price:
				continue
			total = round(quantity * unit_price, 2)
			if isinstance(row, dict):
				row["line_total"] = total
			else:
				row.line_total = total

# SPDX-License-Identifier: MIT
"""`get_receipt_image` — v0.164.0. The photograph on a receipt, as bytes.

Workers could file a receipt and not see the photograph again: `get_expense_receipt`
answers a `/private/files/…` link the handset cannot follow, and
`get_attachment_content` does not open files on `Expense Receipt`. Flagged four
times in app feedback.

FIVE CLAIMS.

1. `ThePhotographComesBack` — JPEG and PNG, told apart by their bytes, in the key
   names `get_attachment_content` already answers.
2. `WhichFileIsThePhotograph` — the current `receipt_image` wins, a PDF beside it
   never does, and a receipt with no photograph is an answer rather than an error.
3. `TheFieldIsNotAPointer` — a `receipt_image` naming somebody else's file serves
   nothing. The route reads only the receipt's own attachments.
4. `TheGateIsGetExpenseReceipts` — scope, not found for another entity, and the
   read brokered past a DocPerm the double has to be told to deny, with the
   negative control that proves the denial is real.
5. `TheCeilingIsAPhones` — a photograph past the 2 MiB model-context default still
   opens, and `max_bytes` still lowers it.
"""

import base64

import frappe

from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.errors import ToolError
from erpnext_mcp.tools import files as file_tools

from .fixtures import OTHER
from .harness import STORE
from .test_api_mobile import MobileAPITestCase

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00a receipt from the co-op"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRa receipt, screenshotted"
SLIP_URL = "/private/files/slip.jpg"


class ReceiptImageTestCase(MobileAPITestCase):
	"""An enrolled worker with a receipt filed, and a way to put files on it."""

	def setUp(self):
		super().setUp()
		self.be()

	def receipt(self, **overrides):
		payload = {
			"merchant": "Valley Co-op Fuel",
			"amount": 184.62,
			"receipt_date": "2026-06-14",
			"category": "Fuel",
		}
		payload.update(overrides)
		return mobile_api.create_expense_receipt(**payload)["name"]

	def a_file(
		self, name, parent, content, file_name="slip.jpg", field=None, doctype="Expense Receipt", **extra
	):
		"""One File as the bench leaves it: `attach_files_to_document` sets the parent
		and, for an Attach Image value, `attached_to_field`."""
		row = {
			"name": name,
			"file_name": file_name,
			"file_url": f"/private/files/{file_name}",
			"file_size": len(content),
			"is_private": 1,
			"attached_to_doctype": doctype,
			"attached_to_name": parent,
			"attached_to_field": field,
			"owner": "ana@example.test",
		}
		row.update(extra)
		STORE.seed("File", [row])
		STORE.file_contents[name] = content
		return name


# ── 1. ───────────────────────────────────────────────────────────────────────
class ThePhotographComesBack(ReceiptImageTestCase):
	def test_a_jpeg_filed_through_receipt_image_comes_back_as_bytes(self):
		receipt = self.receipt(receipt_image=SLIP_URL)
		self.a_file("file-slip", receipt, JPEG, field="receipt_image")
		data = mobile_api.get_receipt_image(receipt=receipt)
		self.assertTrue(data["has_image"])
		self.assertEqual(base64.b64decode(data["content"]), JPEG)
		self.assertEqual(data["content_type"], "image/jpeg")
		self.assertEqual(data["encoding"], "base64")
		self.assertEqual(data["file"], "file-slip")
		self.assertEqual(data["receipt"], receipt)
		self.assertEqual(data["name"], receipt)

	def test_a_png_comes_back_as_a_png(self):
		receipt = self.receipt(receipt_image="/private/files/slip.png")
		self.a_file("file-slip", receipt, PNG, file_name="slip.png", field="receipt_image")
		data = mobile_api.get_receipt_image(receipt=receipt)
		self.assertEqual(base64.b64decode(data["content"]), PNG)
		self.assertEqual(data["content_type"], "image/png")

	def test_the_content_type_is_read_off_the_bytes_not_the_name(self):
		"""A PNG saved as `.jpg`. `mimetypes` would call it a JPEG."""
		receipt = self.receipt(receipt_image=SLIP_URL)
		self.a_file("file-slip", receipt, PNG, field="receipt_image")
		self.assertEqual(mobile_api.get_receipt_image(receipt=receipt)["content_type"], "image/png")

	def test_the_keys_are_get_attachment_contents(self):
		"""The client already decodes `content`; the MCP tool's names ride alongside."""
		receipt = self.receipt(receipt_image=SLIP_URL)
		self.a_file("file-slip", receipt, JPEG, field="receipt_image")
		data = mobile_api.get_receipt_image(receipt=receipt)
		self.assertEqual(data["content"], data["content_base64"])
		self.assertEqual(data["content_type"], data["mime_type"])

	def test_name_is_a_second_spelling_of_receipt(self):
		receipt = self.receipt(receipt_image=SLIP_URL)
		self.a_file("file-slip", receipt, JPEG, field="receipt_image")
		self.assertTrue(mobile_api.get_receipt_image(name=receipt)["has_image"])

	def test_neither_spelling_is_refused_by_name(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.get_receipt_image()
		self.assertIn("needs a receipt", str(caught.exception))


# ── 2. ───────────────────────────────────────────────────────────────────────
class WhichFileIsThePhotograph(ReceiptImageTestCase):
	def test_a_receipt_with_no_photograph_is_an_answer_not_an_error(self):
		receipt = self.receipt()
		data = mobile_api.get_receipt_image(receipt=receipt)
		self.assertFalse(data["has_image"])
		self.assertIsNone(data["content"])
		self.assertIsNone(data["file"])

	def test_the_current_receipt_image_wins_over_an_older_one(self):
		"""A retaken photograph. The first File stays attached through the same field,
		and it is the newer row only if nothing better identifies the current one."""
		receipt = self.receipt(receipt_image="/private/files/retake.jpg")
		self.a_file(
			"file-retake",
			receipt,
			JPEG,
			file_name="retake.jpg",
			field="receipt_image",
			creation="2026-06-14 09:00:00",
		)
		self.a_file(
			"file-first",
			receipt,
			b"\xff\xd8\xff old",
			file_name="first.jpg",
			field="receipt_image",
			creation="2026-06-14 10:00:00",
		)
		self.assertEqual(mobile_api.get_receipt_image(receipt=receipt)["file"], "file-retake")

	def test_a_pdf_is_the_slip_when_it_is_the_only_thing_filed(self):
		"""**THE BUG THIS FILE USED TO ASSERT, REPORTED FOUR TIMES.** It read "a
		PDF beside the slip is never the photograph", which was written about a
		receipt that ALSO had a photograph. On this farm the slips ARE the PDFs —
		emailed invoices, co-op statements, fuel accounts — so every receipt
		answered `has_image: false` and the app said it had been filed without a
		photograph about a document sitting on the record.

		A PDF alone is the slip. A PDF beside a photograph is still not, which is
		the test below this one and the rule that survived."""
		receipt = self.receipt()
		self.a_file("file-invoice", receipt, b"%PDF-1.7 a co-op statement", file_name="invoice.pdf")

		data = mobile_api.get_receipt_image(receipt=receipt)
		self.assertTrue(data["has_image"])
		self.assertEqual(data["file"], "file-invoice")
		self.assertEqual(base64.b64decode(data["content"]), b"%PDF-1.7 a co-op statement")

	def test_a_pdf_slip_is_typed_as_a_pdf_off_its_own_bytes(self):
		"""A handset that has to guess draws a broken image over a receipt that is
		perfectly fine — the same argument the JPEG/PNG sniff already makes."""
		receipt = self.receipt()
		self.a_file("file-invoice", receipt, b"%PDF-1.7 x", file_name="invoice.pdf", mime_type="")

		data = mobile_api.get_receipt_image(receipt=receipt)
		self.assertEqual(data["content_type"], "application/pdf")
		self.assertEqual(data["mime_type"], "application/pdf")

	def test_a_pointer_at_a_pdf_is_honoured(self):
		"""`receipt_image` naming the PDF is somebody having SAID which file the
		slip is, and a newer attachment does not overrule it."""
		receipt = self.receipt(receipt_image="/private/files/invoice.pdf")
		self.a_file("file-invoice", receipt, b"%PDF-1.7", file_name="invoice.pdf",
		            file_url="/private/files/invoice.pdf")
		self.a_file("file-later", receipt, JPEG, file_name="later.jpg",
		            creation="2027-01-01 10:00:00")

		self.assertEqual(mobile_api.get_receipt_image(receipt=receipt)["file"], "file-invoice")

	def test_a_file_that_is_neither_is_still_not_the_slip(self):
		"""The filter widened by exactly one format. A spreadsheet somebody
		attached to a receipt is not the receipt."""
		receipt = self.receipt()
		self.a_file("file-notes", receipt, b"PK\x03\x04", file_name="workings.xlsx")

		self.assertFalse(mobile_api.get_receipt_image(receipt=receipt)["has_image"])

	def test_a_photograph_attached_without_the_field_is_still_found(self):
		"""`attach_file_to_document` files against the receipt and sets no field."""
		receipt = self.receipt()
		self.a_file("file-invoice", receipt, b"%PDF-1.7", file_name="invoice.pdf")
		self.a_file("file-photo", receipt, JPEG, file_name="photo.jpeg")
		data = mobile_api.get_receipt_image(receipt=receipt)
		self.assertEqual(data["file"], "file-photo")
		self.assertEqual(base64.b64decode(data["content"]), JPEG)


# ── 3. ───────────────────────────────────────────────────────────────────────
class TheFieldIsNotAPointer(ReceiptImageTestCase):
	def test_a_receipt_image_naming_another_records_file_serves_nothing(self):
		"""`create_expense_receipt` writes `receipt_image` as the phone sends it. If
		the route looked that URL up across the File table, filing a receipt would be
		a way to read any private photograph on the site."""
		self.a_file(
			"file-licence",
			"HR-EMP-00011",
			b"\xff\xd8\xff a driving licence",
			file_name="licence.jpg",
			doctype="Employee",
		)
		receipt = self.receipt(receipt_image="/private/files/licence.jpg")
		data = mobile_api.get_receipt_image(receipt=receipt)
		self.assertFalse(data["has_image"])
		self.assertIsNone(data["content"])
		self.assertEqual(data["receipt_image"], "/private/files/licence.jpg")

	def test_an_unattached_file_at_that_url_is_not_served_either(self):
		self.a_file("file-loose", None, JPEG, doctype=None)
		receipt = self.receipt(receipt_image=SLIP_URL)
		self.assertFalse(mobile_api.get_receipt_image(receipt=receipt)["has_image"])


# ── 4. ───────────────────────────────────────────────────────────────────────
class TheGateIsGetExpenseReceipts(ReceiptImageTestCase):
	def test_another_entitys_receipt_reads_as_absent_rather_than_refused(self):
		receipt = self.receipt(receipt_image=SLIP_URL)
		self.a_file("file-slip", receipt, JPEG, field="receipt_image")
		frappe.db.set_value("Expense Receipt", receipt, "company", OTHER)
		with self.assertRaises(Exception) as caught:
			mobile_api.get_receipt_image(receipt=receipt)
		self.assertIn("not found", str(caught.exception))

	def test_a_guest_is_refused(self):
		receipt = self.receipt(receipt_image=SLIP_URL)
		self.a_file("file-slip", receipt, JPEG, field="receipt_image")
		self.be("Guest")
		with self.assertRaises(frappe.PermissionError):
			mobile_api.get_receipt_image(receipt=receipt)

	def deny_receipt_read(self):
		STORE.denied_permissions.add(("Expense Receipt", "read"))
		self.addCleanup(STORE.denied_permissions.discard, ("Expense Receipt", "read"))

	def test_the_denial_is_real(self):
		"""The negative control: the tool this route does not use refuses."""
		receipt = self.receipt(receipt_image=SLIP_URL)
		self.a_file("file-slip", receipt, JPEG, field="receipt_image")
		self.deny_receipt_read()
		with self.assertRaises(ToolError):
			file_tools.get_attachment_content({"name": "file-slip"})

	def test_a_worker_without_the_docperm_still_sees_the_photograph(self):
		"""`get_expense_receipt` never asks Frappe's DocPerm, so neither does this."""
		receipt = self.receipt(receipt_image=SLIP_URL)
		self.a_file("file-slip", receipt, JPEG, field="receipt_image")
		self.deny_receipt_read()
		self.assertEqual(base64.b64decode(mobile_api.get_receipt_image(receipt=receipt)["content"]), JPEG)


# ── 5. ───────────────────────────────────────────────────────────────────────
class TheCeilingIsAPhones(ReceiptImageTestCase):
	BIG = b"\xff\xd8\xff" + b"\x00" * (3 * 1024 * 1024)

	def test_a_photograph_past_the_model_context_default_still_opens(self):
		self.assertGreater(len(self.BIG), file_tools.DEFAULT_MAX_BYTES)
		receipt = self.receipt(receipt_image=SLIP_URL)
		self.a_file("file-slip", receipt, self.BIG, field="receipt_image")
		self.assertEqual(mobile_api.get_receipt_image(receipt=receipt)["file_size"], len(self.BIG))

	def test_max_bytes_still_lowers_it(self):
		receipt = self.receipt(receipt_image=SLIP_URL)
		self.a_file("file-slip", receipt, self.BIG, field="receipt_image")
		with self.assertRaises(frappe.ValidationError) as caught:
			mobile_api.get_receipt_image(receipt=receipt, max_bytes=1024)
		self.assertIn("over the", str(caught.exception))

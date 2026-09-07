# SPDX-License-Identifier: MIT
"""Expense Receipt Capture — v0.31.0.

TEN CLAIMS.

1. `CaptureAndRead` — a submitted receipt comes back with everything it went in with.
2. `RequiredFields` — merchant, amount, receipt_date, submitted_by and company are refused when absent.
3. `Validation` — category, status, confidence, amount and every Link are checked with a sentence.
4. `LineItems` — a missing line total is derived, a given one is kept, and neither is reconciled against the receipt total.
5. `ApprovalFlow` — approval records who and when.
6. `RejectionFlow` — rejection requires a reason and records who, when and why.
7. `StatusTransitions` — a decided receipt cannot be decided again, in either direction.
8. `Listing` — every filter narrows, the ordering surfaces doubt first, and the total is the total of what matched.
9. `SettingsGates` — all five switches refuse by name, and the read/write defaults are the ones the app promises.
10. `TheDoctypeItself` — the schema is well-formed, the controller is reachable, and the constants agree with the JSON.

WHY THE OCR FIELDS ARE TESTED AS DATA AND NOT AS BEHAVIOUR. Nothing in this app
does OCR — the extraction runs on the phone, in iOS Vision, before any of this is
called. What the server owes is that the machine's reading and the photograph it
was read off SURVIVE TOGETHER, and that a low confidence is visible rather than
silently swallowed. Those are the two things asserted below.
"""

import json

from erpnext_mcp import registry
from erpnext_mcp.erpnext_mcp.doctype.expense_receipt.expense_receipt import ExpenseReceipt
from erpnext_mcp.tools import expenses

from .fixtures import MAIN, OTHER, V12TestCase, install_hrms
from .harness import STORE, _load_app_doctype

EXPENSE_TOOLS = (
	"list_expense_receipts",
	"get_expense_receipt",
	"submit_expense_receipt",
	"approve_expense_receipt",
	"reject_expense_receipt",
)

EXPENSE_TOOLS_ON = {f"allow_{name}": 1 for name in EXPENSE_TOOLS}

#: A receipt off a fuel pump, with the fields an on-device OCR pass would have
#: filled in. Every test that needs "a receipt" starts from this and overrides the
#: one field it is actually about.
FUEL_RECEIPT = {
	"merchant": "Valley Co-op Fuel",
	"amount": 184.62,
	"receipt_date": "2026-06-14",
	"category": "Fuel",
	"company": MAIN,
	"submitted_by": "HR-EMP-00001",
	"receipt_image": "/files/receipt-fuel-2026-06-14.jpg",
	"ocr_raw_text": "VALLEY CO-OP FUEL\n14/06/2026\nDIESEL 42.1 GAL\nTOTAL 184.62",
	"ocr_confidence": 0.94,
}

#: A parts receipt with two lines, one of which the scanner read a total for and
#: one of which it did not. The pair is the whole of the line-total claim: 2 at
#: $31.25 has to come out $62.50, and 4 at $2.10 has to stay the $7.40 the slip
#: says rather than becoming the $8.40 the multiplication would give.
PARTS_RECEIPT = {
	**FUEL_RECEIPT,
	"merchant": "Cascade Ag Parts",
	"category": "Equipment Parts",
	"amount": 96.40,
	"items": [
		{"description": "Hydraulic hose 1/2in", "quantity": 2, "unit_price": 31.25},
		{"description": "Hose clamp", "quantity": 4, "unit_price": 2.10, "line_total": 7.40},
	],
}


class ExpenseTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **EXPENSE_TOOLS_ON)
		install_hrms()
		self._seed_task()

	def _seed_task(self):
		STORE.seed(
			"Farm Task",
			[
				{
					"name": "TASK-2026-0001",
					"title": "Thin block 4",
					"company": MAIN,
					"status": "Open",
				}
			],
		)

	def capture(self, **overrides):
		"""Submit one receipt and return its payload."""
		return self.tool_data("submit_expense_receipt", {**FUEL_RECEIPT, **overrides})


# ── Claim 1: a receipt comes back with what it went in with ──────────────


class CaptureAndRead(ExpenseTestCase):
	def test_submit_returns_the_new_docname_and_the_extracted_fields(self):
		data = self.capture()
		self.assertTrue(data["name"])
		self.assertEqual(data["merchant"], "Valley Co-op Fuel")
		self.assertEqual(data["amount"], 184.62)
		self.assertEqual(data["receipt_date"], "2026-06-14")
		self.assertEqual(data["category"], "Fuel")
		self.assertEqual(data["submitted_by"], "HR-EMP-00001")

	def test_a_captured_receipt_is_submitted_not_draft(self):
		"""The foreman pressed the button. Landing it in Draft would leave every
		receipt waiting on a second action nobody knows they owe."""
		self.assertEqual(self.capture()["status"], "Submitted")

	def test_a_client_with_an_offline_queue_can_post_a_draft(self):
		self.assertEqual(self.capture(status="Draft")["status"], "Draft")

	def test_get_returns_the_photograph_and_the_raw_ocr_text(self):
		"""The two fields that settle an argument about what the slip said."""
		name = self.capture()["name"]
		data = self.tool_data("get_expense_receipt", {"name": name})
		self.assertEqual(data["receipt_image"], "/files/receipt-fuel-2026-06-14.jpg")
		self.assertIn("TOTAL 184.62", data["ocr_raw_text"])
		self.assertEqual(data["ocr_confidence"], 0.94)

	def test_the_list_view_does_not_carry_the_raw_ocr_text(self):
		"""It is the largest field on the record and no list needs it."""
		self.capture()
		row = self.tool_data("list_expense_receipts", {"company": MAIN})["receipts"][0]
		self.assertNotIn("ocr_raw_text", row)
		self.assertIn("receipt_image", row)

	def test_an_expense_can_be_booked_against_a_task(self):
		data = self.capture(farm_task="TASK-2026-0001")
		self.assertEqual(data["farm_task"], "TASK-2026-0001")
		got = self.tool_data("get_expense_receipt", {"name": data["name"]})
		self.assertEqual(got["farm_task"], "TASK-2026-0001")

	def test_a_receipt_with_no_task_is_still_a_receipt(self):
		self.assertIsNone(self.capture()["farm_task"])

	def test_the_employee_can_be_named_rather_than_keyed(self):
		"""A manager in a chat client knows a person's name, not their docname."""
		data = self.capture(submitted_by="Ada Orchard")
		self.assertEqual(data["submitted_by"], "HR-EMP-00001")

	def test_notes_survive_the_round_trip(self):
		name = self.capture(notes="Filled the spray tractor before the block 4 pass.")["name"]
		got = self.tool_data("get_expense_receipt", {"name": name})
		self.assertIn("spray tractor", got["notes"])

	def test_get_accepts_the_documented_aliases(self):
		name = self.capture()["name"]
		for key in ("name", "expense_receipt", "receipt"):
			with self.subTest(alias=key):
				self.assertEqual(self.tool_data("get_expense_receipt", {key: name})["name"], name)

	def test_the_capture_is_audited_with_a_summary_an_operator_can_read(self):
		self.capture()
		rows = self.audit_rows(tool_name="submit_expense_receipt")
		self.assertTrue(rows)
		self.assertIn("Valley Co-op Fuel", rows[-1]["result_summary"])


# ── Claim 2: the fields without which a receipt is not a record ──────────


class RequiredFields(ExpenseTestCase):
	def test_merchant_is_required(self):
		payload = dict(FUEL_RECEIPT)
		payload.pop("merchant")
		self.assertIn("merchant", self.tool_error("submit_expense_receipt", payload))

	def test_amount_is_required(self):
		payload = dict(FUEL_RECEIPT)
		payload.pop("amount")
		self.assertIn("amount", self.tool_error("submit_expense_receipt", payload))

	def test_receipt_date_is_required(self):
		payload = dict(FUEL_RECEIPT)
		payload.pop("receipt_date")
		self.assertIn("receipt_date", self.tool_error("submit_expense_receipt", payload))

	def test_submitted_by_is_required(self):
		payload = dict(FUEL_RECEIPT)
		payload.pop("submitted_by")
		self.assertIn("submitted_by", self.tool_error("submit_expense_receipt", payload))

	def test_company_is_required_on_a_multi_company_site(self):
		"""The fixture has two companies on purpose, which is what makes this
		testable at all — on a one-company site it would be inferred."""
		payload = dict(FUEL_RECEIPT)
		payload.pop("company")
		error = self.tool_error("submit_expense_receipt", payload)
		self.assertIn("company is required", error)
		self.assertIn(MAIN, error)

	def test_a_zero_amount_is_allowed(self):
		"""A comped part still generates a slip somebody has to file."""
		self.assertEqual(self.capture(amount=0)["amount"], 0.0)

	def test_the_registry_declares_the_same_required_fields(self):
		"""The schema a client validates against and the checks the handler runs
		have to agree, or a client is told a call is well-formed and it is not."""
		required = set(registry.TOOLS["submit_expense_receipt"]["inputSchema"]["required"])
		self.assertEqual(required, {"merchant", "amount", "receipt_date", "submitted_by"})


# ── Claim 3: everything else is checked with a sentence ──────────────────


class Validation(ExpenseTestCase):
	def test_an_unknown_category_is_refused_with_the_list(self):
		error = self.tool_error("submit_expense_receipt", {**FUEL_RECEIPT, "category": "Bribes"})
		self.assertIn("category must be one of", error)
		self.assertIn("Fertilizer", error)

	def test_the_category_defaults_to_other(self):
		payload = dict(FUEL_RECEIPT)
		payload.pop("category")
		self.assertEqual(self.tool_data("submit_expense_receipt", payload)["category"], "Other")

	def test_every_shipped_category_is_accepted(self):
		for category in expenses.CATEGORIES:
			with self.subTest(category=category):
				self.assertEqual(self.capture(category=category)["category"], category)

	def test_a_receipt_cannot_be_captured_straight_into_approved(self):
		"""Approval is a separate tool with a separate switch. Letting the capture
		call set it would put the write end of the phone past the gate an operator
		turned off."""
		error = self.tool_error("submit_expense_receipt", {**FUEL_RECEIPT, "status": "Approved"})
		self.assertIn("status must be Draft or Submitted", error)

	def test_a_confidence_above_one_is_refused_as_a_percentage(self):
		error = self.tool_error("submit_expense_receipt", {**FUEL_RECEIPT, "ocr_confidence": 87})
		self.assertIn("fraction from 0 to 1", error)
		self.assertIn("percentage", error)

	def test_a_negative_confidence_is_refused(self):
		self.assertIn(
			"fraction from 0 to 1",
			self.tool_error("submit_expense_receipt", {**FUEL_RECEIPT, "ocr_confidence": -0.2}),
		)

	def test_the_boundary_values_of_confidence_are_allowed(self):
		"""0 and 1 are legitimate readings — an unreadable slip and a perfect one."""
		for value in (0, 1):
			with self.subTest(confidence=value):
				name = self.capture(ocr_confidence=value)["name"]
				got = self.tool_data("get_expense_receipt", {"name": name})
				self.assertEqual(got["ocr_confidence"], float(value))

	def test_a_receipt_with_no_confidence_at_all_is_accepted(self):
		"""A client that does no OCR — somebody typing it in — is not a broken client."""
		payload = dict(FUEL_RECEIPT)
		payload.pop("ocr_confidence")
		payload.pop("ocr_raw_text")
		data = self.tool_data("submit_expense_receipt", payload)
		self.assertTrue(data["name"])

	def test_a_negative_amount_is_refused_and_says_what_to_use_instead(self):
		"""v0.160.0 CHANGED THE REMEDY, NOT THE REFUSAL. Until this release the
		message sent the caller to a credit note, which is a document this app
		does not have — so the advice was correct and unfollowable. It now names
		`is_return`, which exists."""
		error = self.tool_error("submit_expense_receipt", {**FUEL_RECEIPT, "amount": -50})
		self.assertIn("negative", error)
		self.assertIn("is_return", error)
		self.assertNotIn("credit note", error)

	def test_an_unknown_employee_is_refused_by_name(self):
		error = self.tool_error("submit_expense_receipt", {**FUEL_RECEIPT, "submitted_by": "Nobody"})
		self.assertIn("no Employee called", error)
		self.assertIn("Nobody", error)

	def test_an_unknown_task_is_refused_by_name(self):
		error = self.tool_error("submit_expense_receipt", {**FUEL_RECEIPT, "farm_task": "TASK-NOPE"})
		self.assertIn("no Farm Task called", error)

	def test_an_unknown_company_is_refused_with_the_known_ones(self):
		error = self.tool_error("submit_expense_receipt", {**FUEL_RECEIPT, "company": "Nowhere Farms"})
		self.assertIn("no Company named", error)

	def test_a_malformed_date_is_refused(self):
		self.assertIn(
			"YYYY-MM-DD",
			self.tool_error("submit_expense_receipt", {**FUEL_RECEIPT, "receipt_date": "last tuesday"}),
		)

	def test_getting_a_receipt_that_does_not_exist_says_so(self):
		self.assertIn(
			"no Expense Receipt called",
			self.tool_error("get_expense_receipt", {"name": "EXR-2026-9999"}),
		)

	def test_getting_a_receipt_with_no_name_at_all_says_which_argument(self):
		self.assertIn("name", self.tool_error("get_expense_receipt", {}))

	def test_the_controller_refuses_the_same_confidence_the_tool_does(self):
		"""The Desk form is a second door onto the same record, and a check that
		lives only in the tool is one the form walks straight past."""
		import frappe

		doc = frappe.get_doc({**FUEL_RECEIPT, "doctype": "Expense Receipt", "ocr_confidence": 42})
		doc.flags.ignore_permissions = True
		with self.assertRaises(Exception) as caught:
			doc.insert()
		self.assertIn("0 to 1", str(caught.exception))


# ── Claim 4: the line items ──────────────────────────────────────────────


class LineItems(ExpenseTestCase):
	PARTS = PARTS_RECEIPT

	def test_a_missing_line_total_is_derived_from_quantity_and_price(self):
		data = self.tool_data("submit_expense_receipt", self.PARTS)
		self.assertEqual(data["items"][0]["line_total"], 62.50)

	def test_a_line_total_the_scanner_read_is_kept_over_the_multiplication(self):
		"""Four at $2.10 is $8.40 and the receipt says $7.40. The receipt is right —
		it gave a discount — and inventing the product would overwrite the truth."""
		data = self.tool_data("submit_expense_receipt", self.PARTS)
		self.assertEqual(data["items"][1]["line_total"], 7.40)

	def test_the_items_survive_a_re_read(self):
		name = self.tool_data("submit_expense_receipt", self.PARTS)["name"]
		items = self.tool_data("get_expense_receipt", {"name": name})["items"]
		self.assertEqual(len(items), 2)
		self.assertEqual(items[0]["description"], "Hydraulic hose 1/2in")
		self.assertEqual(items[0]["quantity"], 2.0)
		self.assertEqual(items[0]["unit_price"], 31.25)

	def test_the_items_total_is_reported_and_is_not_the_receipt_total(self):
		"""Tax, tips, deposits and core charges live between the lines and the
		total. Reporting both and reconciling neither is the honest answer."""
		name = self.tool_data("submit_expense_receipt", self.PARTS)["name"]
		got = self.tool_data("get_expense_receipt", {"name": name})
		self.assertEqual(got["items_total"], 69.90)
		self.assertEqual(got["amount"], 96.40)

	def test_a_receipt_with_no_legible_lines_is_still_valid(self):
		"""The total is the field that matters. A thermal slip that OCR could not
		itemise is an expense, not an error."""
		name = self.capture()["name"]
		got = self.tool_data("get_expense_receipt", {"name": name})
		self.assertEqual(got["items"], [])
		self.assertEqual(got["items_total"], 0)

	def test_items_that_are_not_a_list_are_refused(self):
		self.assertIn(
			"items must be a list",
			self.tool_error("submit_expense_receipt", {**FUEL_RECEIPT, "items": "hose, clamp"}),
		)

	def test_an_item_that_is_not_an_object_is_refused_by_position(self):
		error = self.tool_error(
			"submit_expense_receipt", {**FUEL_RECEIPT, "items": [{"description": "ok"}, "clamp"]}
		)
		self.assertIn("items[2]", error)

	def test_a_non_numeric_quantity_is_refused_by_position(self):
		error = self.tool_error(
			"submit_expense_receipt",
			{**FUEL_RECEIPT, "items": [{"description": "hose", "quantity": "two", "unit_price": 3}]},
		)
		self.assertIn("items[1].quantity", error)

	def test_a_line_with_a_description_and_no_numbers_is_kept(self):
		"""OCR reads a description far more often than it reads the column beside
		it. Dropping the row would lose the only thing it did read."""
		name = self.tool_data(
			"submit_expense_receipt", {**FUEL_RECEIPT, "items": [{"description": "Misc hardware"}]}
		)["name"]
		items = self.tool_data("get_expense_receipt", {"name": name})["items"]
		self.assertEqual(len(items), 1)
		self.assertEqual(items[0]["description"], "Misc hardware")
		self.assertEqual(items[0]["line_total"], 0.0)


# ── Claim 5: approval ────────────────────────────────────────────────────


class ApprovalFlow(ExpenseTestCase):
	def test_approval_moves_the_status_and_records_who_and_when(self):
		name = self.capture()["name"]
		data = self.tool_data(
			"approve_expense_receipt",
			{"name": name, "approved_by": "HR-EMP-00002", "approved_date": "2026-06-20"},
		)
		self.assertEqual(data["status"], "Approved")
		self.assertEqual(data["approved_by"], "HR-EMP-00002")
		self.assertEqual(data["approved_date"], "2026-06-20")

	def test_the_approval_is_on_the_record_and_not_only_in_the_reply(self):
		name = self.capture()["name"]
		self.tool_data("approve_expense_receipt", {"name": name, "approved_by": "HR-EMP-00002"})
		got = self.tool_data("get_expense_receipt", {"name": name})
		self.assertEqual(got["status"], "Approved")
		self.assertEqual(got["approved_by"], "HR-EMP-00002")
		self.assertTrue(got["approved_date"])

	def test_the_approval_date_defaults_to_today(self):
		name = self.capture()["name"]
		data = self.tool_data("approve_expense_receipt", {"name": name, "approved_by": "HR-EMP-00002"})
		self.assertTrue(data["approved_date"])

	def test_a_draft_can_be_approved_without_being_submitted_first(self):
		"""A bookkeeper looking at a queue of offline-captured drafts should not
		have to walk each one through a state nobody is waiting on."""
		name = self.capture(status="Draft")["name"]
		data = self.tool_data("approve_expense_receipt", {"name": name, "approved_by": "HR-EMP-00002"})
		self.assertEqual(data["status"], "Approved")

	def test_the_approver_can_be_named_rather_than_keyed(self):
		name = self.capture()["name"]
		data = self.tool_data("approve_expense_receipt", {"name": name, "approved_by": "Ben Packhouse"})
		self.assertEqual(data["approved_by"], "HR-EMP-00002")

	def test_an_unknown_approver_is_refused_and_the_receipt_is_untouched(self):
		name = self.capture()["name"]
		self.assertIn(
			"no Employee called",
			self.tool_error("approve_expense_receipt", {"name": name, "approved_by": "Nobody"}),
		)
		self.assertEqual(self.tool_data("get_expense_receipt", {"name": name})["status"], "Submitted")

	def test_approving_a_receipt_that_does_not_exist_says_so(self):
		self.assertIn(
			"no Expense Receipt called",
			self.tool_error(
				"approve_expense_receipt", {"name": "EXR-2026-9999", "approved_by": "HR-EMP-00002"}
			),
		)

	def test_approval_leaves_the_rejection_fields_empty(self):
		name = self.capture()["name"]
		self.tool_data("approve_expense_receipt", {"name": name, "approved_by": "HR-EMP-00002"})
		got = self.tool_data("get_expense_receipt", {"name": name})
		self.assertIsNone(got["rejected_by"])
		self.assertIsNone(got["rejection_reason"])


# ── Claim 6: rejection ───────────────────────────────────────────────────


class RejectionFlow(ExpenseTestCase):
	def test_rejection_records_who_when_and_why(self):
		name = self.capture()["name"]
		data = self.tool_data(
			"reject_expense_receipt",
			{
				"name": name,
				"rejected_by": "HR-EMP-00002",
				"reason": "Personal fuel — not a company vehicle.",
				"rejected_date": "2026-06-21",
			},
		)
		self.assertEqual(data["status"], "Rejected")
		self.assertEqual(data["rejected_by"], "HR-EMP-00002")
		self.assertEqual(data["rejected_date"], "2026-06-21")
		self.assertIn("Personal fuel", data["rejection_reason"])

	def test_the_reason_comes_back_to_the_phone_that_submitted_it(self):
		"""Stored on the record rather than in a comment, so a read of the receipt
		returns it without a second call to a different surface."""
		name = self.capture()["name"]
		self.tool_data(
			"reject_expense_receipt",
			{"name": name, "rejected_by": "HR-EMP-00002", "reason": "No itemised detail."},
		)
		got = self.tool_data("get_expense_receipt", {"name": name})
		self.assertEqual(got["status"], "Rejected")
		self.assertIn("No itemised detail", got["rejection_reason"])

	def test_a_rejection_with_no_reason_is_refused(self):
		name = self.capture()["name"]
		error = self.tool_error(
			"reject_expense_receipt", {"name": name, "rejected_by": "HR-EMP-00002", "reason": ""}
		)
		self.assertIn("reason is required", error)

	def test_a_refused_rejection_leaves_the_receipt_where_it_was(self):
		name = self.capture()["name"]
		self.tool_error("reject_expense_receipt", {"name": name, "rejected_by": "HR-EMP-00002"})
		self.assertEqual(self.tool_data("get_expense_receipt", {"name": name})["status"], "Submitted")

	def test_the_reason_can_be_sent_under_either_name(self):
		name = self.capture()["name"]
		data = self.tool_data(
			"reject_expense_receipt",
			{"name": name, "rejected_by": "HR-EMP-00002", "rejection_reason": "Duplicate of EXR-2026-0004."},
		)
		self.assertIn("Duplicate", data["rejection_reason"])

	def test_rejection_leaves_the_approval_fields_empty(self):
		name = self.capture()["name"]
		self.tool_data(
			"reject_expense_receipt",
			{"name": name, "rejected_by": "HR-EMP-00002", "reason": "Wrong company."},
		)
		got = self.tool_data("get_expense_receipt", {"name": name})
		self.assertIsNone(got["approved_by"])
		self.assertIsNone(got["approved_date"])

	def test_the_rejection_is_audited_with_the_reason_in_the_summary(self):
		name = self.capture()["name"]
		self.tool_data(
			"reject_expense_receipt",
			{"name": name, "rejected_by": "HR-EMP-00002", "reason": "Personal fuel."},
		)
		rows = self.audit_rows(tool_name="reject_expense_receipt")
		self.assertTrue(rows)
		self.assertIn("Personal fuel", rows[-1]["result_summary"])


# ── Claim 7: a decision is taken once ────────────────────────────────────


class StatusTransitions(ExpenseTestCase):
	def _approved(self):
		name = self.capture()["name"]
		self.tool_data("approve_expense_receipt", {"name": name, "approved_by": "HR-EMP-00002"})
		return name

	def _rejected(self):
		name = self.capture()["name"]
		self.tool_data(
			"reject_expense_receipt",
			{"name": name, "rejected_by": "HR-EMP-00002", "reason": "Personal fuel."},
		)
		return name

	def test_an_approved_receipt_cannot_be_approved_again(self):
		error = self.tool_error(
			"approve_expense_receipt", {"name": self._approved(), "approved_by": "HR-EMP-00003"}
		)
		self.assertIn("already 'Approved'", error)

	def test_an_approved_receipt_cannot_be_rejected(self):
		"""Not a policy about second thoughts — it is that overwriting the record
		would erase the name and date of whoever decided it first."""
		error = self.tool_error(
			"reject_expense_receipt",
			{"name": self._approved(), "rejected_by": "HR-EMP-00003", "reason": "Changed my mind."},
		)
		self.assertIn("already 'Approved'", error)

	def test_a_rejected_receipt_cannot_be_approved(self):
		error = self.tool_error(
			"approve_expense_receipt", {"name": self._rejected(), "approved_by": "HR-EMP-00003"}
		)
		self.assertIn("already 'Rejected'", error)

	def test_a_rejected_receipt_cannot_be_rejected_again(self):
		error = self.tool_error(
			"reject_expense_receipt",
			{"name": self._rejected(), "rejected_by": "HR-EMP-00003", "reason": "Still no."},
		)
		self.assertIn("already 'Rejected'", error)

	def test_the_first_decision_stands_after_a_refused_second_one(self):
		name = self._approved()
		self.tool_error(
			"reject_expense_receipt",
			{"name": name, "rejected_by": "HR-EMP-00003", "reason": "Changed my mind."},
		)
		got = self.tool_data("get_expense_receipt", {"name": name})
		self.assertEqual(got["status"], "Approved")
		self.assertEqual(got["approved_by"], "HR-EMP-00002")

	def test_the_two_decidable_statuses_are_the_two_the_capture_tool_can_create(self):
		"""Stated as an identity rather than two lists, so a status added to one
		cannot quietly go missing from the other."""
		self.assertEqual(set(expenses.REVIEWABLE_STATUSES), set(expenses.CREATABLE_STATUSES))


# ── Claim 8: listing ─────────────────────────────────────────────────────


class Listing(ExpenseTestCase):
	def setUp(self):
		super().setUp()
		self.capture(merchant="Valley Co-op Fuel", category="Fuel", amount=100, receipt_date="2026-06-01")
		self.capture(
			merchant="Cascade Ag Parts",
			category="Equipment Parts",
			amount=200,
			receipt_date="2026-06-15",
			submitted_by="HR-EMP-00002",
			ocr_confidence=0.41,
		)
		self.capture(
			merchant="Orchard Supply",
			category="Supplies",
			amount=50,
			receipt_date="2026-07-02",
			farm_task="TASK-2026-0001",
			ocr_confidence=0.99,
		)

	def test_it_lists_everything_for_the_company(self):
		data = self.tool_data("list_expense_receipts", {"company": MAIN})
		self.assertEqual(data["count"], 3)

	def test_the_total_is_the_total_of_what_matched(self):
		data = self.tool_data("list_expense_receipts", {"company": MAIN})
		self.assertEqual(data["total_amount"], 350.0)

	def test_the_least_confident_reading_sorts_first(self):
		"""The receipt nobody can read is the one somebody has to open the photo
		for. Ordering it last would put it where it is never looked at."""
		data = self.tool_data("list_expense_receipts", {"company": MAIN})
		self.assertEqual(data["receipts"][0]["merchant"], "Cascade Ag Parts")
		self.assertEqual(data["receipts"][-1]["merchant"], "Orchard Supply")

	def test_it_filters_by_employee(self):
		data = self.tool_data("list_expense_receipts", {"company": MAIN, "employee": "HR-EMP-00002"})
		self.assertEqual(data["count"], 1)
		self.assertEqual(data["receipts"][0]["merchant"], "Cascade Ag Parts")

	def test_it_filters_by_employee_name(self):
		data = self.tool_data("list_expense_receipts", {"company": MAIN, "employee": "Ben Packhouse"})
		self.assertEqual(data["count"], 1)

	def test_it_filters_by_category(self):
		data = self.tool_data("list_expense_receipts", {"company": MAIN, "category": "Fuel"})
		self.assertEqual(data["count"], 1)
		self.assertEqual(data["total_amount"], 100.0)

	def test_it_filters_by_task(self):
		data = self.tool_data("list_expense_receipts", {"farm_task": "TASK-2026-0001"})
		self.assertEqual(data["count"], 1)
		self.assertEqual(data["receipts"][0]["merchant"], "Orchard Supply")

	def test_it_filters_by_status(self):
		name = self.tool_data("list_expense_receipts", {"company": MAIN})["receipts"][0]["name"]
		self.tool_data("approve_expense_receipt", {"name": name, "approved_by": "HR-EMP-00002"})
		self.assertEqual(
			self.tool_data("list_expense_receipts", {"company": MAIN, "status": "Approved"})["count"], 1
		)
		self.assertEqual(
			self.tool_data("list_expense_receipts", {"company": MAIN, "status": "Submitted"})["count"], 2
		)

	def test_it_filters_by_a_date_range(self):
		data = self.tool_data(
			"list_expense_receipts", {"company": MAIN, "from_date": "2026-06-01", "to_date": "2026-06-30"}
		)
		self.assertEqual(data["count"], 2)
		self.assertEqual(data["total_amount"], 300.0)

	def test_from_date_alone_is_an_open_ended_range(self):
		data = self.tool_data("list_expense_receipts", {"company": MAIN, "from_date": "2026-06-15"})
		self.assertEqual(data["count"], 2)

	def test_to_date_alone_is_an_open_ended_range(self):
		data = self.tool_data("list_expense_receipts", {"company": MAIN, "to_date": "2026-06-01"})
		self.assertEqual(data["count"], 1)

	def test_it_scopes_to_the_company_asked_for(self):
		self.capture(company=OTHER, merchant="Second Co Diesel", amount=999)
		self.assertEqual(self.tool_data("list_expense_receipts", {"company": MAIN})["count"], 3)
		self.assertEqual(self.tool_data("list_expense_receipts", {"company": OTHER})["count"], 1)

	def test_the_limit_is_honoured(self):
		data = self.tool_data("list_expense_receipts", {"company": MAIN, "limit": 2})
		self.assertEqual(data["count"], 2)

	def test_an_unknown_status_filter_is_refused_with_the_list(self):
		error = self.tool_error("list_expense_receipts", {"status": "Pending"})
		self.assertIn("status must be one of", error)
		self.assertIn("Approved", error)

	def test_an_unknown_category_filter_is_refused_with_the_list(self):
		self.assertIn(
			"category must be one of",
			self.tool_error("list_expense_receipts", {"category": "Bribes"}),
		)

	def test_a_filter_that_matches_nothing_is_an_empty_list_not_an_error(self):
		data = self.tool_data("list_expense_receipts", {"company": MAIN, "status": "Rejected"})
		self.assertEqual(data["count"], 0)
		self.assertEqual(data["total_amount"], 0)
		self.assertEqual(data["receipts"], [])

	def test_reading_the_list_writes_nothing(self):
		before = {doctype: len(rows) for doctype, rows in STORE.tables.items()}
		self.tool_data("list_expense_receipts", {"company": MAIN})
		self.tool_data(
			"get_expense_receipt",
			{"name": self.tool_data("list_expense_receipts", {"company": MAIN})["receipts"][0]["name"]},
		)
		after = {doctype: len(rows) for doctype, rows in STORE.tables.items()}
		after.pop("MCP Action Log", None)
		before.pop("MCP Action Log", None)
		self.assertEqual(before, after)


# ── Claim 9: the switches ────────────────────────────────────────────────


class SettingsGates(ExpenseTestCase):
	def test_each_tool_is_refused_by_the_name_of_the_field_to_tick(self):
		"""An operator reading the refusal has to be able to find the checkbox."""
		calls = {
			"list_expense_receipts": {},
			"get_expense_receipt": {"name": "EXR-2026-0001"},
			"submit_expense_receipt": FUEL_RECEIPT,
			"approve_expense_receipt": {"name": "EXR-2026-0001", "approved_by": "HR-EMP-00002"},
			"reject_expense_receipt": {
				"name": "EXR-2026-0001",
				"rejected_by": "HR-EMP-00002",
				"reason": "No.",
			},
		}
		for name, arguments in calls.items():
			with self.subTest(tool=name):
				self.configure(enabled=1, **{**EXPENSE_TOOLS_ON, f"allow_{name}": 0})
				error = self.tool_error(name, arguments)
				self.assertIn(f"allow_{name}", error)
				self.assertIn("switched off", error)

	def test_a_disabled_tool_never_reaches_the_site(self):
		"""Refused on the name alone, before the arguments are looked at — so a
		switched-off capture cannot create a row on its way to being refused."""
		self.configure(enabled=1, **{**EXPENSE_TOOLS_ON, "allow_submit_expense_receipt": 0})
		self.tool_error("submit_expense_receipt", FUEL_RECEIPT)
		self.assertEqual(len(STORE.rows("Expense Receipt")), 0)

	def test_approval_and_rejection_are_switched_separately(self):
		"""The whole reason they are two tools. An operator who wants a manager
		able to approve is not thereby saying they may refuse."""
		self.configure(enabled=1, **{**EXPENSE_TOOLS_ON, "allow_reject_expense_receipt": 0})
		name = self.capture()["name"]
		self.assertIn(
			"allow_reject_expense_receipt",
			self.tool_error(
				"reject_expense_receipt", {"name": name, "rejected_by": "HR-EMP-00002", "reason": "No."}
			),
		)
		self.assertEqual(
			self.tool_data("approve_expense_receipt", {"name": name, "approved_by": "HR-EMP-00002"})[
				"status"
			],
			"Approved",
		)

	def test_the_two_reads_ship_on_and_the_three_writes_ship_off(self):
		fields = {field["fieldname"]: field for field in _load_app_doctype("erpnext_mcp_settings")["fields"]}
		self.assertEqual(fields["allow_list_expense_receipts"]["default"], "1")
		self.assertEqual(fields["allow_get_expense_receipt"]["default"], "1")
		self.assertEqual(fields["allow_submit_expense_receipt"]["default"], "0")
		self.assertEqual(fields["allow_approve_expense_receipt"]["default"], "0")
		self.assertEqual(fields["allow_reject_expense_receipt"]["default"], "0")

	def test_out_of_the_box_the_writes_are_refused_and_the_reads_are_not(self):
		"""The shipped posture, exercised rather than read off the JSON."""
		self.configure(enabled=1)
		self.assertIn("switched off", self.tool_error("submit_expense_receipt", FUEL_RECEIPT))
		self.tool_data("list_expense_receipts", {"company": MAIN})

	def test_the_registry_agrees_with_the_switches_about_which_ones_write(self):
		for name in ("list_expense_receipts", "get_expense_receipt"):
			with self.subTest(tool=name):
				self.assertIn(name, registry.READ_TOOLS)
		for name in ("submit_expense_receipt", "approve_expense_receipt", "reject_expense_receipt"):
			with self.subTest(tool=name):
				self.assertIn(name, registry.MUTATING_TOOLS)

	def test_every_expense_tool_says_what_it_needs_when_the_doctype_is_absent(self):
		for name in EXPENSE_TOOLS:
			with self.subTest(tool=name):
				self.assertIn("Expense Receipt", registry.TOOLS[name]["requires"])
				self.assertIn("v0.31.0", registry.TOOLS[name]["requires"])


# ── Claim 10: the schema itself ──────────────────────────────────────────


class TheDoctypeItself(ExpenseTestCase):
	def setUp(self):
		super().setUp()
		self.receipt = _load_app_doctype("expense_receipt")
		self.item = _load_app_doctype("expense_receipt_item")
		self.by_name = {field["fieldname"]: field for field in self.receipt["fields"]}

	def test_every_field_the_specification_named_is_there(self):
		for fieldname in (
			"merchant",
			"amount",
			"receipt_date",
			"category",
			"receipt_image",
			"ocr_raw_text",
			"ocr_confidence",
			"submitted_by",
			"farm_task",
			"company",
			"notes",
			"status",
			"approved_by",
			"approved_date",
		):
			with self.subTest(field=fieldname):
				self.assertIn(fieldname, self.by_name)

	def test_the_reqd_fields_are_exactly_the_ones_a_receipt_is_useless_without(self):
		"""Asserted as an exact set rather than a membership check: a field that
		quietly became mandatory is a phone that quietly stops being able to post."""
		reqd = {name for name, field in self.by_name.items() if field.get("reqd")}
		self.assertEqual(reqd, {"merchant", "amount", "receipt_date", "submitted_by", "company", "status"})

	def test_the_receipt_image_is_an_image_field_and_not_a_bare_attachment(self):
		"""It is a photograph, and the Desk showing it inline is the difference
		between checking a receipt and downloading one."""
		self.assertEqual(self.by_name["receipt_image"]["fieldtype"], "Attach Image")

	def test_the_raw_ocr_text_is_long_text_and_the_confidence_is_a_float(self):
		self.assertEqual(self.by_name["ocr_raw_text"]["fieldtype"], "Long Text")
		self.assertEqual(self.by_name["ocr_confidence"]["fieldtype"], "Float")

	def test_the_status_options_and_default_are_what_the_tools_assume(self):
		self.assertEqual(self.by_name["status"]["options"].split("\n"), list(expenses.STATUSES))
		self.assertEqual(self.by_name["status"]["default"], "Draft")

	def test_the_category_options_match_the_list_the_tools_validate_against(self):
		"""Two hand-written copies of one list is how a category gets accepted by
		the tool and refused by the database."""
		self.assertEqual(tuple(self.by_name["category"]["options"].split("\n")), expenses.CATEGORIES)

	def test_the_item_table_is_a_child_table_with_the_five_named_columns(self):
		"""`item` joined the four in v0.67.0. It is a Link and it is OPTIONAL:
		the description still says what the slip said, and nothing infers one
		from the other."""
		self.assertEqual(int(self.item.get("istable") or 0), 1)
		fields = {field["fieldname"] for field in self.item["fields"]}
		self.assertEqual(fields, {"description", "item", "quantity", "unit_price", "line_total"})
		item_field = next(f for f in self.item["fields"] if f["fieldname"] == "item")
		self.assertEqual(item_field["fieldtype"], "Link")
		self.assertEqual(item_field["options"], "Item")
		self.assertFalse(item_field.get("reqd"))

	def test_the_receipt_points_at_the_item_table(self):
		self.assertEqual(self.by_name["items"]["fieldtype"], "Table")
		self.assertEqual(self.by_name["items"]["options"], "Expense Receipt Item")

	def test_the_controller_is_importable_and_is_a_document(self):
		"""The v0.7.1 failure: a DocType JSON with no Python module beside it, and
		a `bench migrate` that dies with ModuleNotFoundError."""
		import frappe

		self.assertIsNotNone(frappe.new_doc("Expense Receipt"))
		self.assertIsNotNone(frappe.new_doc("Expense Receipt Item"))
		self.assertTrue(issubclass(ExpenseReceipt, object))

	def test_it_is_named_by_a_series_a_person_can_read(self):
		self.assertEqual(self.receipt["autoname"], "naming_series:")
		self.assertEqual(self.by_name["naming_series"]["default"], "EXR-.YYYY.-.####")

	def test_the_register_is_declared_precious_so_uninstall_warns_about_it(self):
		"""The photograph is the substantiation a deduction rests on, and it
		exists nowhere else once the paper is in a truck door pocket."""
		from erpnext_mcp import install

		precious = dict(install._PRECIOUS_DOCTYPES)
		self.assertIn("Expense Receipt", precious)
		self.assertIn("photograph", precious["Expense Receipt"])

	def test_the_tool_payload_is_json_serialisable(self):
		"""Every tool result goes down the wire as JSON. A Date or a Decimal that
		reached the payload would fail at the transport rather than here."""
		name = self.capture()["name"]
		result = self.tool("get_expense_receipt", {"name": name})
		json.loads(result["content"][0]["text"])


# ── Claim 11: the Supplier and Item links (v0.67.0) ───────────────────────


class VendorAndItemLinks(ExpenseTestCase):
	"""Sprint 1 gave this app the ability to create a Supplier and an Item.
	Sprint 2 lets a receipt point at them — ADDITIVELY, and never by inference.

	The design is one sentence, and it is what every test below is checking: the
	link sits BESIDE the text, not instead of it. A slip printed
	`VALLEY CO-OP #14` and a Supplier record called `Valley Co-operative` are the
	same vendor, and a capture that replaced the first with the second would lose
	the evidence in the act of improving the data.
	"""

	def setUp(self):
		super().setUp()
		STORE.seed(
			"Supplier",
			[
				{
					"name": "Valley Co-operative",
					"supplier_name": "Valley Co-operative",
					"supplier_group": "Services",
				}
			],
		)
		STORE.seed(
			"Item", [{"name": "HOSE-050", "item_name": "Hydraulic hose 1/2in", "item_group": "Consumables"}]
		)

	def test_a_receipt_can_name_the_supplier_behind_the_merchant(self):
		data = self.capture(supplier="Valley Co-operative")
		self.assertEqual(data["supplier"], "Valley Co-operative")

	def test_the_merchant_text_is_untouched_by_the_link(self):
		"""THE POINT OF THE WHOLE FEATURE. The paper still says what it said."""
		data = self.capture(merchant="VALLEY CO-OP #14", supplier="Valley Co-operative")
		self.assertEqual(data["merchant"], "VALLEY CO-OP #14")
		self.assertEqual(data["supplier"], "Valley Co-operative")

	def test_a_receipt_without_a_supplier_is_still_a_receipt(self):
		self.assertIsNone(self.capture()["supplier"])

	def test_nothing_infers_a_supplier_from_the_merchant_string(self):
		"""A wrong link is worse than no link and is indistinguishable from a
		right one afterwards."""
		self.assertIsNone(self.capture(merchant="Valley Co-operative")["supplier"])

	def test_a_supplier_that_does_not_exist_is_refused(self):
		self.assertIn(
			"no Supplier called",
			self.tool_error("submit_expense_receipt", {**FUEL_RECEIPT, "supplier": "Nobody Ltd"}),
		)

	def test_the_register_can_be_filtered_to_one_vendor(self):
		self.capture(supplier="Valley Co-operative")
		self.capture(merchant="Cascade Ag Parts")
		data = self.tool_data("list_expense_receipts", {"supplier": "Valley Co-operative"})
		self.assertEqual(data["count"], 1)
		self.assertEqual(data["receipts"][0]["supplier"], "Valley Co-operative")

	def test_a_line_can_name_the_item_it_bought(self):
		data = self.capture(
			items=[{"description": "HYD HOSE 1/2", "item": "HOSE-050", "quantity": 2, "unit_price": 31.25}]
		)
		self.assertEqual(data["items"][0]["item"], "HOSE-050")
		self.assertEqual(data["items"][0]["description"], "HYD HOSE 1/2")

	def test_an_item_that_does_not_exist_is_refused_naming_the_line(self):
		message = self.tool_error(
			"submit_expense_receipt",
			{**FUEL_RECEIPT, "items": [{"description": "HYD HOSE 1/2", "item": "NOPE-001"}]},
		)
		self.assertIn("no Item called 'NOPE-001'", message)

	def test_a_line_with_no_item_is_still_a_line(self):
		"""`HYD HOSE 1/2` matches four items in a real catalogue, and a guess
		would put a fabricated consumption figure somewhere downstream."""
		data = self.capture(items=[{"description": "HYD HOSE 1/2", "quantity": 2, "unit_price": 31.25}])
		self.assertIsNone(data["items"][0]["item"])
		self.assertEqual(data["items"][0]["line_total"], 62.50)

	def test_the_links_survive_a_read(self):
		name = self.capture(
			supplier="Valley Co-operative",
			items=[{"description": "HYD HOSE 1/2", "item": "HOSE-050", "quantity": 1, "unit_price": 31.25}],
		)["name"]
		data = self.tool_data("get_expense_receipt", {"name": name})
		self.assertEqual(data["supplier"], "Valley Co-operative")
		self.assertEqual(data["items"][0]["item"], "HOSE-050")

	def test_a_bench_with_no_erpnext_refuses_by_the_real_reason(self):
		"""Not "no Supplier called X" — that reads as a typo and sends somebody
		looking for a record that could never have been there."""
		from .harness import INSTALLED_DOCTYPES

		INSTALLED_DOCTYPES.discard("Supplier")
		message = self.tool_error(
			"submit_expense_receipt", {**FUEL_RECEIPT, "supplier": "Valley Co-operative"}
		)
		self.assertIn("has no Supplier doctype", message)
		self.assertIn("captures fine without it", message)

	def test_the_supplier_field_is_optional_on_the_doctype_too(self):
		receipt = _load_app_doctype("expense_receipt")
		by_name = {field["fieldname"]: field for field in receipt["fields"]}
		self.assertEqual(by_name["supplier"]["fieldtype"], "Link")
		self.assertEqual(by_name["supplier"]["options"], "Supplier")
		self.assertFalse(by_name["supplier"].get("reqd"))


# ── v0.68.0: update_expense_receipt, get_expense_summary, get_expense_report ─

RECEIPT_ENHANCEMENT_TOOLS = ("update_expense_receipt", "get_expense_summary", "get_expense_report")
RECEIPT_ENHANCEMENT_TOOLS_ON = {f"allow_{name}": 1 for name in RECEIPT_ENHANCEMENT_TOOLS}


class EnhancementTestCase(ExpenseTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **EXPENSE_TOOLS_ON, **RECEIPT_ENHANCEMENT_TOOLS_ON)
		STORE.seed(
			"Supplier",
			[{"name": "Valley Co-operative", "supplier_name": "Valley Co-operative"}],
		)


class UpdateExpenseReceipt(EnhancementTestCase):
	def test_updates_category_supplier_cost_center_and_notes(self):
		name = self.capture()["name"]
		data = self.tool_data(
			"update_expense_receipt",
			{
				"name": name,
				"category": "Equipment Parts",
				"supplier": "Valley Co-operative",
				"cost_center": "110 - Field Work - ETC",
				"notes": "recoded at month end",
			},
		)
		self.assertEqual(data["fields_changed"], ["category", "cost_center", "notes", "supplier"])
		got = self.tool_data("get_expense_receipt", {"name": name})
		self.assertEqual(got["category"], "Equipment Parts")
		self.assertEqual(got["supplier"], "Valley Co-operative")
		self.assertEqual(got["cost_center"], "110 - Field Work - ETC")
		self.assertIn("recoded at month end", got["notes"])

	def test_works_on_an_approved_receipt(self):
		"""The whole point: a bookkeeper corrects categorisation after review,
		not only before it."""
		name = self.capture()["name"]
		self.tool_data("approve_expense_receipt", {"name": name, "approved_by": "HR-EMP-00001"})
		data = self.tool_data("update_expense_receipt", {"name": name, "category": "Hardware"})
		self.assertEqual(data["after"]["category"], "Hardware")
		got = self.tool_data("get_expense_receipt", {"name": name})
		self.assertEqual(got["status"], "Approved")
		self.assertEqual(got["category"], "Hardware")

	def test_it_never_touches_merchant_amount_or_receipt_date(self):
		required = set(registry.TOOLS["update_expense_receipt"]["inputSchema"]["properties"])
		self.assertNotIn("merchant", required)
		self.assertNotIn("amount", required)
		self.assertNotIn("receipt_date", required)

	def test_no_fields_named_is_refused(self):
		name = self.capture()["name"]
		error = self.tool_error("update_expense_receipt", {"name": name})
		self.assertIn("nothing to update", error)

	def test_no_real_change_is_refused(self):
		name = self.capture(category="Fuel")["name"]
		error = self.tool_error("update_expense_receipt", {"name": name, "category": "Fuel"})
		self.assertIn("already reads what was asked for", error)
		self.assertIn("Nothing to change", error)

	def test_an_unknown_category_is_refused_with_the_list(self):
		name = self.capture()["name"]
		error = self.tool_error("update_expense_receipt", {"name": name, "category": "Bribes"})
		self.assertIn("category must be one of", error)

	def test_a_cost_center_that_does_not_exist_is_refused(self):
		name = self.capture()["name"]
		error = self.tool_error("update_expense_receipt", {"name": name, "cost_center": "Nowhere - ETC"})
		self.assertIn("no Cost Center called", error)

	def test_supplier_can_be_cleared_with_an_empty_string(self):
		name = self.capture(supplier="Valley Co-operative")["name"]
		data = self.tool_data("update_expense_receipt", {"name": name, "supplier": ""})
		self.assertIsNone(data["after"]["supplier"])
		got = self.tool_data("get_expense_receipt", {"name": name})
		self.assertIsNone(got["supplier"])

	def test_a_missing_receipt_is_refused_by_name(self):
		error = self.tool_error("update_expense_receipt", {"name": "EXR-NOPE", "category": "Fuel"})
		self.assertIn("no Expense Receipt called", error)

	def test_the_update_is_audited_with_a_diff_an_operator_can_read(self):
		name = self.capture(category="Fuel")["name"]
		self.tool_data("update_expense_receipt", {"name": name, "category": "Feed"})
		rows = self.audit_rows(tool_name="update_expense_receipt")
		self.assertTrue(rows)
		self.assertIn("Fuel", rows[-1]["result_summary"])
		self.assertIn("Feed", rows[-1]["result_summary"])

	def test_switched_off_by_default(self):
		name = self.capture()["name"]
		self.configure(
			enabled=1,
			**{**EXPENSE_TOOLS_ON, **RECEIPT_ENHANCEMENT_TOOLS_ON, "allow_update_expense_receipt": 0},
		)
		error = self.tool_error("update_expense_receipt", {"name": name, "category": "Feed"})
		self.assertIn("switched off", error)


class ExpenseSummary(EnhancementTestCase):
	def _seed_three_receipts(self):
		self.capture(category="Fuel", amount=100, receipt_date="2026-06-05")
		self.capture(category="Fuel", amount=50, receipt_date="2026-06-20")
		self.capture(category="Equipment Parts", amount=75, receipt_date="2026-07-02")

	def test_totals_by_category(self):
		self._seed_three_receipts()
		data = self.tool_data("get_expense_summary", {"company": MAIN})
		self.assertEqual(data["by_category"]["Fuel"], {"count": 2, "total": 150.0})
		self.assertEqual(data["by_category"]["Equipment Parts"], {"count": 1, "total": 75.0})
		self.assertEqual(data["total_amount"], 225.0)

	def test_trend_is_bucketed_by_month_by_default(self):
		self._seed_three_receipts()
		data = self.tool_data("get_expense_summary", {"company": MAIN, "period": "month"})
		labels = {row["period"] for row in data["trend"]}
		self.assertEqual(labels, {"2026-06", "2026-07"})
		june = next(row for row in data["trend"] if row["period"] == "2026-06")
		self.assertEqual(june["total"], 150.0)

	def test_quarter_bucketing(self):
		self._seed_three_receipts()
		data = self.tool_data("get_expense_summary", {"company": MAIN, "period": "quarter"})
		self.assertEqual({row["period"] for row in data["trend"]}, {"2026-Q2", "2026-Q3"})

	def test_an_unknown_period_is_refused(self):
		error = self.tool_error("get_expense_summary", {"company": MAIN, "period": "fortnight"})
		self.assertIn("period must be one of", error)

	def test_rejected_receipts_are_excluded_by_default_and_the_count_is_reported(self):
		self.capture(category="Fuel", amount=100)
		rejected = self.capture(category="Fuel", amount=999)["name"]
		self.tool_data(
			"reject_expense_receipt", {"name": rejected, "rejected_by": "HR-EMP-00001", "reason": "duplicate"}
		)
		data = self.tool_data("get_expense_summary", {"company": MAIN})
		self.assertEqual(data["total_amount"], 100.0)
		self.assertIn("1 Rejected receipt", data["note"])

	def test_explicit_status_includes_rejected(self):
		rejected = self.capture(category="Fuel", amount=999)["name"]
		self.tool_data(
			"reject_expense_receipt", {"name": rejected, "rejected_by": "HR-EMP-00001", "reason": "duplicate"}
		)
		data = self.tool_data("get_expense_summary", {"company": MAIN, "status": "Rejected"})
		self.assertEqual(data["total_amount"], 999.0)

	def test_group_by_merchant(self):
		self.capture(merchant="Valley Co-op Fuel", amount=100)
		self.capture(merchant="Valley Co-op Fuel", amount=50)
		self.capture(merchant="Cascade Ag Parts", amount=75, category="Equipment Parts")
		data = self.tool_data("get_expense_summary", {"company": MAIN, "group_by": "merchant"})
		self.assertEqual(data["by_merchant"]["Valley Co-op Fuel"]["total"], 150.0)
		self.assertEqual(data["by_merchant"]["Cascade Ag Parts"]["total"], 75.0)

	def test_an_unknown_group_by_is_refused(self):
		error = self.tool_error("get_expense_summary", {"company": MAIN, "group_by": "planet"})
		self.assertIn("group_by must be", error)

	def test_date_range_narrows_the_totals(self):
		self._seed_three_receipts()
		data = self.tool_data(
			"get_expense_summary", {"company": MAIN, "from_date": "2026-07-01", "to_date": "2026-07-31"}
		)
		self.assertEqual(data["total_amount"], 75.0)


class ExpenseReport(EnhancementTestCase):
	def test_lists_every_status_by_default(self):
		self.capture(category="Fuel", amount=100)
		rejected = self.capture(category="Fuel", amount=50)["name"]
		self.tool_data(
			"reject_expense_receipt", {"name": rejected, "rejected_by": "HR-EMP-00001", "reason": "no"}
		)
		data = self.tool_data("get_expense_report", {"company": MAIN})
		self.assertEqual(data["count"], 2)
		self.assertEqual(data["total_amount"], 150.0)
		statuses = {row["status"] for row in data["receipts"]}
		self.assertEqual(statuses, {"Submitted", "Rejected"})

	def test_status_filter_narrows_it(self):
		self.capture(category="Fuel", amount=100)
		data = self.tool_data("get_expense_report", {"company": MAIN, "status": "Submitted"})
		self.assertEqual(data["count"], 1)

	def test_category_filter_narrows_it(self):
		self.capture(category="Fuel", amount=100)
		self.capture(category="Feed", amount=40)
		data = self.tool_data("get_expense_report", {"company": MAIN, "category": "Feed"})
		self.assertEqual(data["count"], 1)
		self.assertEqual(data["receipts"][0]["category"], "Feed")

	def test_csv_export_carries_the_same_rows(self):
		self.capture(merchant="Valley Co-op Fuel", category="Fuel", amount=100)
		data = self.tool_data("get_expense_report", {"company": MAIN, "csv": True})
		self.assertIn("csv", data)
		self.assertIn("Valley Co-op Fuel", data["csv"])
		self.assertIn("merchant", data["csv"].splitlines()[0])

	def test_csv_is_omitted_unless_asked_for(self):
		self.capture()
		data = self.tool_data("get_expense_report", {"company": MAIN})
		self.assertNotIn("csv", data)

	def test_an_unknown_category_is_refused(self):
		error = self.tool_error("get_expense_report", {"company": MAIN, "category": "Bribes"})
		self.assertIn("category must be one of", error)


# ── v0.160.0: correcting what the scanner read, and money that came back ─────

CORRECTION_TOOLS_ON = {
	**EXPENSE_TOOLS_ON,
	"allow_correct_receipt_amount": 1,
	"allow_correct_receipt_date": 1,
	"allow_update_expense_receipt": 1,
	"allow_get_expense_summary": 1,
	"allow_get_expense_report": 1,
}


class CorrectingWhatTheScannerRead(ExpenseTestCase):
	"""v0.160.0. An OCR misread was permanent until this release.

	The two receipts these tools were written for are real and are on the
	Orchard Meadow site: an AutoZone slip dated **2099-01-08** (Vision read the
	card's `EXP 01/29`) and a NAPA slip totalling **$18.18** when the paper says
	$13.99. Neither could ever match a bank line, and neither could be fixed —
	`update_expense_receipt` refuses `amount` and `receipt_date` on purpose.
	"""

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **CORRECTION_TOOLS_ON)

	def correct(self, name, **arguments):
		return self.tool_data("correct_receipt_amount", {"name": name, **arguments})

	# ── the correction itself ────────────────────────────────────────────
	def test_the_amount_is_changed_and_the_original_is_kept(self):
		name = self.capture(amount=18.18)["name"]
		data = self.correct(name, amount=13.99, reason="OCR read the store number as the total")
		self.assertEqual(data["corrected_to"], "13.99")
		self.assertEqual(data["as_captured"], "18.18")
		self.assertTrue(data["first_correction"])
		back = self.tool_data("get_expense_receipt", {"name": name})
		self.assertEqual(back["amount"], 13.99)
		self.assertEqual(back["original_amount"], 18.18)

	def test_the_date_is_changed_and_the_original_is_kept(self):
		"""The 2099 case. A date no matcher will search and no fiscal year holds."""
		name = self.capture(receipt_date="2099-01-08")["name"]
		data = self.tool_data(
			"correct_receipt_date",
			{"name": name, "receipt_date": "2026-08-30", "reason": "OCR read the card expiry"},
		)
		self.assertEqual(data["corrected_to"], "2026-08-30")
		self.assertEqual(data["as_captured"], "2099-01-08")
		back = self.tool_data("get_expense_receipt", {"name": name})
		self.assertEqual(back["receipt_date"], "2026-08-30")
		self.assertEqual(back["original_date"], "2099-01-08")

	def test_the_reason_and_the_actor_and_the_stamp_are_all_recorded(self):
		name = self.capture(amount=18.18)["name"]
		self.correct(name, amount=13.99, reason="transposed digits")
		back = self.tool_data("get_expense_receipt", {"name": name})
		self.assertEqual(back["amount_correction_reason"], "transposed digits")
		self.assertTrue(back["amount_corrected_by"])
		self.assertTrue(back["amount_corrected_at"])
		self.assertTrue(back["amount_corrected"])

	def test_correcting_the_amount_leaves_the_date_trail_alone(self):
		"""Two separate columns for two separate acts. A bookkeeper who fixed a
		total has not said anything about the date."""
		name = self.capture(amount=18.18)["name"]
		self.correct(name, amount=13.99, reason="transposed digits")
		back = self.tool_data("get_expense_receipt", {"name": name})
		self.assertFalse(back["date_corrected"])
		self.assertIsNone(back["date_corrected_at"])
		self.assertIsNone(back["original_date"])

	def test_the_photograph_and_the_raw_ocr_text_survive_a_correction(self):
		"""The whole point of keeping them. A corrected number that erased the
		evidence would make the correction impossible to check."""
		name = self.capture(amount=18.18)["name"]
		self.correct(name, amount=13.99, reason="transposed digits")
		back = self.tool_data("get_expense_receipt", {"name": name})
		self.assertEqual(back["receipt_image"], "/files/receipt-fuel-2026-06-14.jpg")
		self.assertIn("TOTAL 184.62", back["ocr_raw_text"])

	# ── the once-only original ───────────────────────────────────────────
	def test_a_second_correction_does_not_overwrite_the_original(self):
		"""THE LOAD-BEARING ONE. What anybody asks later is what the SCANNER
		read, not what the last-but-one person thought it read. A column that
		tracked the previous value would answer the second question while
		looking exactly like it answered the first."""
		name = self.capture(amount=18.18)["name"]
		self.correct(name, amount=13.99, reason="first go")
		second = self.correct(name, amount=14.99, reason="no, this one")
		self.assertFalse(second["first_correction"])
		self.assertEqual(second["previous_value"], "13.99")
		self.assertEqual(second["as_captured"], "18.18")
		self.assertEqual(self.tool_data("get_expense_receipt", {"name": name})["original_amount"], 18.18)

	def test_a_second_correction_says_it_is_not_the_first(self):
		name = self.capture(amount=18.18)["name"]
		self.correct(name, amount=13.99, reason="first go")
		self.assertIn("not the first correction", self.correct(name, amount=14.99, reason="again")["note"])

	# ── the zero the database manufactures ───────────────────────────────
	def test_an_uncorrected_receipt_reports_no_original_rather_than_a_zero(self):
		"""`original_amount` IS A CURRENCY COLUMN, which on a real site is
		`NOT NULL DEFAULT 0` — so every receipt nobody has corrected reads back
		0.00, and "original amount: $0.00" beside "amount: $184.62" is a zero
		the DATABASE manufactured, not a fact about the slip.

		The row is handed to the reader the way MariaDB would hand it over — a
		literal 0.0, not a seeded None — because a None here would make this
		test pass for a reason that does not exist on a bench. See
		`the-standalone-double-nests-what-mariadb-flattens`.
		"""
		rendered = expenses._row_out(
			{
				"name": "EXR-2026-0001",
				"amount": 184.62,
				"original_amount": 0.0,
				"amount_correction_reason": "",
				"amount_corrected_by": "",
				"amount_corrected_at": None,
			}
		)
		self.assertFalse(rendered["amount_corrected"])
		self.assertIsNone(rendered["original_amount"])
		self.assertIsNone(rendered["amount_corrected_by"])

	def test_a_corrected_receipt_still_reports_an_original_of_zero(self):
		"""And the other direction, which is why the timestamp is the flag: a
		receipt whose scanner read NOTHING and was corrected to $13.99 has a
		genuine original of 0.00, and suppressing that would be the same bug
		with the sign flipped."""
		rendered = expenses._row_out(
			{
				"amount": 13.99,
				"original_amount": 0.0,
				"amount_corrected_at": "2026-09-07 10:00:00",
				"amount_corrected_by": "book@fafo.farm",
			}
		)
		self.assertTrue(rendered["amount_corrected"])
		self.assertEqual(rendered["original_amount"], 0.0)

	# ── refusals ─────────────────────────────────────────────────────────
	def test_a_correction_with_no_reason_is_refused(self):
		name = self.capture(amount=18.18)["name"]
		error = self.tool_error("correct_receipt_amount", {"name": name, "amount": 13.99})
		self.assertIn("reason is required", error)
		self.assertEqual(self.tool_data("get_expense_receipt", {"name": name})["amount"], 18.18)

	def test_a_date_correction_with_no_reason_is_refused(self):
		name = self.capture()["name"]
		error = self.tool_error("correct_receipt_date", {"name": name, "receipt_date": "2026-08-30"})
		self.assertIn("reason is required", error)

	def test_correcting_to_the_value_it_already_has_is_refused(self):
		name = self.capture(amount=18.18)["name"]
		error = self.tool_error(
			"correct_receipt_amount", {"name": name, "amount": 18.18, "reason": "no change"}
		)
		self.assertIn("already reads", error)
		self.assertIn("nothing was changed", error.lower())

	def test_a_negative_amount_is_refused_and_names_the_return_flag(self):
		name = self.capture()["name"]
		error = self.tool_error(
			"correct_receipt_amount", {"name": name, "amount": -13.99, "reason": "it was a refund"}
		)
		self.assertIn("is_return", error)

	def test_an_unknown_receipt_is_refused_by_name(self):
		error = self.tool_error("correct_receipt_amount", {"name": "EXR-NOPE", "amount": 1, "reason": "why"})
		self.assertIn("EXR-NOPE", error)

	def test_a_correction_needs_a_value_to_correct_to(self):
		name = self.capture()["name"]
		self.assertIn(
			"amount is required",
			self.tool_error("correct_receipt_amount", {"name": name, "reason": "why"}),
		)
		self.assertIn(
			"receipt_date is required",
			self.tool_error("correct_receipt_date", {"name": name, "reason": "why"}),
		)

	# ── what a correction deliberately does NOT do ───────────────────────
	def test_the_status_is_not_touched(self):
		name = self.capture(amount=18.18)["name"]
		self.assertEqual(self.correct(name, amount=13.99, reason="fix")["status"], "Submitted")
		self.assertEqual(self.tool_data("get_expense_receipt", {"name": name})["status"], "Submitted")

	def test_an_approved_receipt_is_corrected_and_the_decision_is_flagged(self):
		"""NOT REFUSED. The receipt these tools exist for is exactly the one that
		got approved at the wrong total and then would not reconcile. But an
		approval is a statement about an amount, and the amount just changed
		underneath it — so the response says so and leaves the call to a person.
		"""
		name = self.capture(amount=18.18)["name"]
		self.tool_data("approve_expense_receipt", {"name": name, "approved_by": "HR-EMP-00002"})
		data = self.correct(name, amount=13.99, reason="OCR misread")
		self.assertEqual(data["status"], "Approved")
		self.assertIn("18.18", data["decided_on_the_old_value"])
		self.assertEqual(self.tool_data("get_expense_receipt", {"name": name})["status"], "Approved")

	def test_an_unapproved_receipt_carries_no_decision_warning(self):
		"""The negative control for the test above — otherwise it would pass on
		a key that was always present."""
		name = self.capture(amount=18.18)["name"]
		self.assertNotIn("decided_on_the_old_value", self.correct(name, amount=13.99, reason="fix"))

	def test_correcting_does_not_reach_the_other_receipts(self):
		other = self.capture(amount=50.00)["name"]
		name = self.capture(amount=18.18)["name"]
		self.correct(name, amount=13.99, reason="fix")
		self.assertEqual(self.tool_data("get_expense_receipt", {"name": other})["amount"], 50.0)

	# ── the switches ─────────────────────────────────────────────────────
	def test_each_correction_tool_has_its_own_switch(self):
		"""Separate from `allow_update_expense_receipt` on purpose: an operator
		who wants a bookkeeper able to recode a receipt does not necessarily
		want the same surface able to change what it says was spent."""
		for tool, arguments in (
			("correct_receipt_amount", {"name": "EXR-2026-0001", "amount": 1, "reason": "x"}),
			("correct_receipt_date", {"name": "EXR-2026-0001", "receipt_date": "2026-01-01", "reason": "x"}),
		):
			with self.subTest(tool=tool):
				self.configure(enabled=1, **{**CORRECTION_TOOLS_ON, f"allow_{tool}": 0})
				error = self.tool_error(tool, arguments)
				self.assertIn(f"allow_{tool}", error)

	def test_both_correction_tools_default_off(self):
		"""A mutating tool ships OFF, and neither of these is on the short
		default-on list. 326 of 327 mutating tools work this way by design."""
		for tool in ("correct_receipt_amount", "correct_receipt_date"):
			with self.subTest(tool=tool):
				self.assertTrue(registry.TOOLS[tool]["mutating"])
				self.assertIn(tool, registry.MUTATING_TOOLS)
				self.assertNotIn(tool, registry.DEFAULT_ON_MUTATING_TOOLS)

	def test_update_expense_receipt_still_refuses_the_two_corrected_fields(self):
		"""The separation is the point. If this ever passes, the audit trail can
		no longer tell a recode from a change to what was spent."""
		name = self.capture()["name"]
		for field, value in (("amount", 1.0), ("receipt_date", "2026-01-01"), ("merchant", "Nope")):
			with self.subTest(field=field):
				self.assertNotIn(field, expenses.UPDATABLE_FIELDS)
				error = self.tool_error("update_expense_receipt", {"name": name, field: value})
				self.assertIn("nothing to update", error)


class MoneyThatCameBack(ExpenseTestCase):
	"""v0.160.0. `is_return` — which side of the statement a slip belongs on."""

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **CORRECTION_TOOLS_ON)

	def test_a_return_can_be_captured_from_the_phone(self):
		data = self.capture(amount=13.99, is_return=True)
		self.assertTrue(data["is_return"])
		self.assertEqual(data["amount"], 13.99)

	def test_an_ordinary_capture_is_not_a_return(self):
		self.assertFalse(self.capture()["is_return"])

	def test_the_amount_stays_positive(self):
		"""The magnitude the paper printed. The flag says the direction; a minus
		sign in the amount would net against the wrong side of the statement."""
		name = self.capture(amount=13.99, is_return=True)["name"]
		self.assertEqual(self.tool_data("get_expense_receipt", {"name": name})["amount"], 13.99)

	def test_a_desk_can_flag_a_return_after_capture(self):
		"""Nobody at a returns counter remembers to tick a box, so the flag has
		to be settable later — which is why it is on `update_expense_receipt`."""
		name = self.capture(amount=13.99)["name"]
		data = self.tool_data("update_expense_receipt", {"name": name, "is_return": True})
		self.assertEqual(data["after"]["is_return"], 1)
		self.assertTrue(self.tool_data("get_expense_receipt", {"name": name})["is_return"])

	def test_a_return_ticked_in_error_can_be_unticked(self):
		"""THE ZERO-DROP CASE. `after[key] = value or None` would refuse to write
		the 0 that says the money went OUT — the update would report the change
		and not make it. See `a-change-guard-that-drops-zero`."""
		name = self.capture(amount=13.99, is_return=True)["name"]
		data = self.tool_data("update_expense_receipt", {"name": name, "is_return": False})
		self.assertEqual(data["after"]["is_return"], 0)
		self.assertFalse(self.tool_data("get_expense_receipt", {"name": name})["is_return"])

	def test_setting_a_flag_it_already_has_is_refused_like_any_other_no_op(self):
		name = self.capture(amount=13.99, is_return=True)["name"]
		self.assertIn(
			"already reads",
			self.tool_error("update_expense_receipt", {"name": name, "is_return": True}),
		)

	# ── the totals ───────────────────────────────────────────────────────
	def test_a_return_subtracts_from_the_expense_summary(self):
		"""The part is already in the bucket at what it cost. Adding the refund
		would overstate the category by TWICE the money."""
		self.capture(amount=100.00, category="Equipment Parts")
		self.capture(amount=13.99, category="Equipment Parts", is_return=True)
		data = self.tool_data("get_expense_summary", {"company": MAIN})
		self.assertEqual(data["total_amount"], 86.01)
		self.assertEqual(data["by_category"]["Equipment Parts"]["total"], 86.01)

	def test_a_return_is_still_counted_as_a_receipt(self):
		"""A bucket showing eleven receipts and ten rows would be the next bug."""
		self.capture(amount=100.00, category="Equipment Parts")
		self.capture(amount=13.99, category="Equipment Parts", is_return=True)
		data = self.tool_data("get_expense_summary", {"company": MAIN})
		self.assertEqual(data["count"], 2)
		self.assertEqual(data["by_category"]["Equipment Parts"]["count"], 2)

	def test_the_netting_is_reported_rather_than_silent(self):
		self.capture(amount=100.00, category="Equipment Parts")
		self.capture(amount=13.99, category="Equipment Parts", is_return=True)
		data = self.tool_data("get_expense_summary", {"company": MAIN})
		self.assertEqual(data["returns_count"], 1)
		self.assertEqual(data["returns_amount"], 13.99)
		self.assertIn("SUBTRACTED", data["note"])

	def test_a_farm_with_no_returns_reads_exactly_as_it_did_before(self):
		"""The negative control. If the netting fired on an ordinary receipt,
		every total on every farm would have flipped sign."""
		self.capture(amount=100.00, category="Equipment Parts")
		data = self.tool_data("get_expense_summary", {"company": MAIN})
		self.assertEqual(data["total_amount"], 100.0)
		self.assertEqual(data["returns_count"], 0)
		self.assertNotIn("SUBTRACTED", data["note"])

	def test_a_return_subtracts_from_the_expense_report_total_too(self):
		self.capture(amount=100.00, category="Equipment Parts")
		self.capture(amount=13.99, category="Equipment Parts", is_return=True)
		data = self.tool_data("get_expense_report", {"company": MAIN})
		self.assertEqual(data["total_amount"], 86.01)
		self.assertEqual(data["returns_count"], 1)
		self.assertEqual(data["count"], 2)

	def test_the_report_says_which_way_each_row_went(self):
		"""One row per receipt and no totals to net — a bookkeeper reading a
		$13.99 line needs to see the direction on the line itself."""
		self.capture(amount=13.99, is_return=True)
		row = self.tool_data("get_expense_report", {"company": MAIN})["receipts"][0]
		self.assertTrue(row["is_return"])

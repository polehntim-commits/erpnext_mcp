# SPDX-License-Identifier: MIT
"""Reimbursement Received — a check paying back a share of an expense. v0.186.0.

SIX CLAIMS.

1. `CapturingACheck` — the category is money in (always `is_return`), the payer is
   the merchant, the link to the original is checked, and the checks linked to one
   expense never add up to more than it cost.
2. `RecodingAtADesk` — linking later, moving into and out of the category, and
   the direction staying fixed.
3. `NotABill` — a Purchase Invoice is refused by name, and the expense summary nets
   the check out of spend.
4. `PostingTheCheck` — Dr bank, Cr the original expense's own account (off its
   invoice, or matched from its category), or the account named; the BTN rides in
   cheque_no; every refusal writes nothing.
5. `TheClassifier` — a check with a reason is a confident reimbursement, a bare
   check a capped lean, and a settlement stub, a co-op notice or a fuel slip is not.
6. `FromAPhone` — `reimburses_receipt` reaches the tool, scoped like any docname.
"""

import frappe

from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.tools import expenses, receipts

from .fixtures import BANK, MAIN, MAIN_ABBR, OTHER, PurchasingTestCase, install_hrms, seed_v7, supplies
from .harness import STORE
from .test_api_mobile import WORKER_EMPLOYEE, MobileAPITestCase

CATEGORY = "Reimbursement Received"
CHECK_TEXT = (
	"JANE POLEHN 1024\nPAY TO THE ORDER OF Orchard Meadow LLC $ 42.25\n"
	"Forty-two and 25/100 DOLLARS\nMEMO my share of Coastal\n⑆123000848⑆ 4455⑈"
)

ON = {
	f"allow_{name}": 1
	for name in (
		"submit_expense_receipt",
		"get_expense_receipt",
		"approve_expense_receipt",
		"reject_expense_receipt",
		"update_expense_receipt",
		"get_expense_summary",
		"create_purchase_invoice_from_receipt",
		"classify_receipt",
		"post_reimbursement_receipt",
	)
}

DUE_FROM = f"1310 - Due From Family - {MAIN_ABBR}"
RECEIVABLE = f"1300 - Accounts Receivable - {MAIN_ABBR}"


class ReimbursementTestCase(PurchasingTestCase):
	def setUp(self):
		super().setUp()
		install_hrms()
		# The bank chart and the company's default bank account, which the
		# purchasing fixtures leave out and a deposit needs.
		seed_v7()
		self.configure(enabled=1, **ON)

	def expense(self, **overrides):
		payload = {
			"merchant": "Coastal Farm & Ranch",
			"amount": 84.50,
			"receipt_date": "2026-09-10",
			"category": "Supplies",
			"company": MAIN,
			"submitted_by": "HR-EMP-00001",
		}
		payload.update(overrides)
		return self.tool_data("submit_expense_receipt", payload)["name"]

	def check(self, **overrides):
		payload = {
			"merchant": "Jane Polehn",
			"amount": 42.25,
			"receipt_date": "2026-09-24",
			"category": CATEGORY,
			"company": MAIN,
			"submitted_by": "HR-EMP-00001",
			"receipt_image": "/private/files/check-1024.jpg",
		}
		payload.update(overrides)
		return self.tool_data("submit_expense_receipt", payload)

	def approve(self, name):
		self.tool_data("approve_expense_receipt", {"name": name, "approved_by": "HR-EMP-00001"})
		return name

	def lines(self, journal_entry):
		doc = frappe.get_doc("Journal Entry", journal_entry)
		out = []
		for line in doc.get("accounts"):
			get = line.get if isinstance(line, dict) else (lambda key, line=line: getattr(line, key, None))
			out.append((get("account"), float(get("debit") or 0), float(get("credit") or 0)))
		return sorted(out)


# ── 1. ───────────────────────────────────────────────────────────────────────
class CapturingACheck(ReimbursementTestCase):
	def test_the_category_is_a_real_option_on_the_doctype(self):
		self.assertIn(CATEGORY, expenses.CATEGORIES)
		options = frappe.get_meta("Expense Receipt").get_field("category").options.split("\n")
		self.assertIn(CATEGORY, options)

	def test_a_check_is_money_in_with_its_payer_and_photo(self):
		original = self.expense()
		data = self.check(reimburses_receipt=original)
		self.assertTrue(data["is_return"])
		self.assertEqual(data["payer"], "Jane Polehn")
		self.assertEqual(data["reimburses_receipt"], original)
		self.assertEqual(data["receipt_image"], "/private/files/check-1024.jpg")
		row = self.tool_data("get_expense_receipt", {"name": data["name"]})
		self.assertEqual(row["reimburses_receipt"], original)
		self.assertTrue(row["is_return"])

	def test_the_direction_is_not_a_question(self):
		"""A phone that sent is_return false still captures money in."""
		self.assertTrue(self.check(is_return=False)["is_return"])

	def test_the_link_is_optional(self):
		data = self.check()
		self.assertIsNone(data["reimburses_receipt"])

	def test_a_check_needs_its_amount(self):
		self.assertIn(
			"positive amount",
			self.tool_error(
				"submit_expense_receipt",
				{
					"merchant": "Jane Polehn",
					"amount": 0,
					"receipt_date": "2026-09-24",
					"category": CATEGORY,
					"company": MAIN,
					"submitted_by": "HR-EMP-00001",
				},
			),
		)

	def test_every_bad_link_is_refused_before_anything_is_written(self):
		original = self.expense()
		other = self.expense(company=OTHER, cost_center=None)
		rejected = self.expense()
		self.tool_data(
			"reject_expense_receipt",
			{"name": rejected, "rejected_by": "HR-EMP-00001", "reason": "duplicate"},
		)
		earlier_check = self.check()["name"]
		before = len(STORE.rows("Expense Receipt"))
		for args, words in (
			({"reimburses_receipt": "EXR-NOPE"}, "no Expense Receipt called"),
			({"reimburses_receipt": other}, "belongs to"),
			({"reimburses_receipt": rejected}, "Rejected"),
			({"reimburses_receipt": earlier_check}, "itself a Reimbursement Received"),
			({"reimburses_receipt": original, "amount": 90}, "repay more than was spent"),
			({"reimburses_receipt": original, "category": "Fuel"}, "belongs to a Reimbursement"),
		):
			with self.subTest(args=args):
				payload = {
					"merchant": "Jane Polehn",
					"amount": 42.25,
					"receipt_date": "2026-09-24",
					"category": CATEGORY,
					"company": MAIN,
					"submitted_by": "HR-EMP-00001",
				}
				payload.update(args)
				self.assertIn(words, self.tool_error("submit_expense_receipt", payload))
		self.assertEqual(len(STORE.rows("Expense Receipt")), before)

	def test_partial_checks_add_up_to_the_expense_and_no_further(self):
		"""Two halves of an 84.50 run are fine; a third check is a mistake."""
		original = self.expense()
		self.check(reimburses_receipt=original, amount=42.25)
		self.check(reimburses_receipt=original, amount=42.25)
		error = self.tool_error(
			"submit_expense_receipt",
			{
				"merchant": "Jane Polehn",
				"amount": 0.01,
				"receipt_date": "2026-09-25",
				"category": CATEGORY,
				"company": MAIN,
				"submitted_by": "HR-EMP-00001",
				"reimburses_receipt": original,
			},
		)
		self.assertIn("84.50 of it has already been paid back", error)

	def test_a_rejected_check_frees_its_share(self):
		original = self.expense()
		first = self.check(reimburses_receipt=original, amount=84.50)["name"]
		self.tool_data(
			"reject_expense_receipt",
			{"name": first, "rejected_by": "HR-EMP-00001", "reason": "bounced"},
		)
		self.assertTrue(self.check(reimburses_receipt=original, amount=84.50)["name"])


# ── 2. ───────────────────────────────────────────────────────────────────────
class RecodingAtADesk(ReimbursementTestCase):
	def test_a_check_is_linked_to_its_expense_later(self):
		original = self.expense()
		name = self.check()["name"]
		data = self.tool_data("update_expense_receipt", {"name": name, "reimburses_receipt": original})
		self.assertEqual(data["after"]["reimburses_receipt"], original)
		self.assertEqual(frappe.db.get_value("Expense Receipt", name, "reimburses_receipt"), original)

	def test_linking_later_is_checked_like_linking_at_capture(self):
		original = self.expense(amount=10)
		name = self.check()["name"]
		error = self.tool_error("update_expense_receipt", {"name": name, "reimburses_receipt": original})
		self.assertIn("repay more than was spent", error)

	def test_moving_into_the_category_ticks_is_return(self):
		name = self.expense(merchant="Jane Polehn")
		data = self.tool_data("update_expense_receipt", {"name": name, "category": CATEGORY})
		self.assertEqual(data["after"]["is_return"], 1)
		self.assertEqual(int(frappe.db.get_value("Expense Receipt", name, "is_return")), 1)

	def test_recategorising_and_linking_is_one_call(self):
		original = self.expense()
		name = self.expense(merchant="Jane Polehn", amount=20)
		self.tool_data(
			"update_expense_receipt",
			{"name": name, "category": CATEGORY, "reimburses_receipt": original},
		)
		self.assertEqual(frappe.db.get_value("Expense Receipt", name, "reimburses_receipt"), original)

	def test_moving_out_with_a_link_is_refused_until_it_is_cleared(self):
		original = self.expense()
		name = self.check(reimburses_receipt=original)["name"]
		error = self.tool_error("update_expense_receipt", {"name": name, "category": "Other"})
		self.assertIn("still names", error)
		self.tool_data(
			"update_expense_receipt", {"name": name, "category": "Other", "reimburses_receipt": ""}
		)
		self.assertFalse(frappe.db.get_value("Expense Receipt", name, "reimburses_receipt"))

	def test_the_direction_cannot_be_unticked(self):
		name = self.check()["name"]
		error = self.tool_error("update_expense_receipt", {"name": name, "is_return": False})
		self.assertIn("is_return stays ticked", error)

	def test_a_link_on_another_category_is_refused(self):
		original = self.expense()
		name = self.expense(merchant="Somebody", amount=5)
		error = self.tool_error("update_expense_receipt", {"name": name, "reimburses_receipt": original})
		self.assertIn("belongs to a Reimbursement Received", error)


# ── 3. ───────────────────────────────────────────────────────────────────────
class NotABill(ReimbursementTestCase):
	def test_a_purchase_invoice_is_refused_by_name(self):
		name = self.approve(self.check()["name"])
		error = self.tool_error("create_purchase_invoice_from_receipt", {"receipt": name})
		self.assertIn("post_reimbursement_receipt", error)

	def test_the_summary_nets_the_check_out_of_spend(self):
		"""What the ledger does when the check credits the expense account."""
		original = self.expense()
		self.check(reimburses_receipt=original)
		data = self.tool_data("get_expense_summary", {"company": MAIN, "from_date": "2026-09-01"})
		self.assertEqual(data["total_amount"], 42.25)
		self.assertEqual(data["returns_count"], 1)


# ── 4. ───────────────────────────────────────────────────────────────────────
class PostingTheCheck(ReimbursementTestCase):
	def seed_due_from(self):
		STORE.seed(
			"Account",
			[
				{
					"name": DUE_FROM,
					"account_name": "Due From Family",
					"account_number": "1310",
					"parent_account": f"Application of Funds (Assets) - {MAIN_ABBR}",
					"is_group": 0,
					"root_type": "Asset",
					"account_type": "",
					"company": MAIN,
					"disabled": 0,
				},
				{
					"name": RECEIVABLE,
					"account_name": "Accounts Receivable",
					"account_number": "1300",
					"parent_account": f"Application of Funds (Assets) - {MAIN_ABBR}",
					"is_group": 0,
					"root_type": "Asset",
					"account_type": "Receivable",
					"company": MAIN,
					"disabled": 0,
				},
			],
		)

	def test_a_billed_expense_is_credited_on_its_invoices_account(self):
		original = self.approve(self.expense())
		invoice = self.tool_data("create_purchase_invoice_from_receipt", {"receipt": original})
		name = self.approve(self.check(reimburses_receipt=original)["name"])
		data = self.tool_data("post_reimbursement_receipt", {"receipt": name})
		self.assertEqual(data["docstatus"], 0)
		self.assertEqual(
			self.lines(data["journal_entry"]), sorted([(BANK, 42.25, 0.0), (supplies(), 0.0, 42.25)])
		)
		self.assertEqual(
			data["credit_account_resolved_by"], f"Purchase Invoice {invoice['purchase_invoice']}"
		)
		self.assertTrue(data["cost_center"])
		self.assertEqual(
			frappe.db.get_value("Expense Receipt", name, "linked_document"), data["journal_entry"]
		)

	def test_an_unbilled_expense_is_matched_from_its_category(self):
		original = self.expense()
		name = self.approve(self.check(reimburses_receipt=original)["name"])
		data = self.tool_data("post_reimbursement_receipt", {"receipt": name})
		self.assertEqual(data["credit_account"], supplies())
		self.assertIn("matched from", data["credit_account_resolved_by"])

	def test_a_named_asset_account_takes_no_cost_center(self):
		self.seed_due_from()
		name = self.approve(self.check()["name"])
		data = self.tool_data("post_reimbursement_receipt", {"receipt": name, "credit_account": DUE_FROM})
		self.assertEqual(
			self.lines(data["journal_entry"]), sorted([(BANK, 42.25, 0.0), (DUE_FROM, 0.0, 42.25)])
		)
		self.assertIsNone(data["cost_center"])

	def test_a_matched_deposit_rides_in_cheque_no(self):
		"""Where v0.184.0's duplicate control reads a bank-fed entry's BTN."""
		name = self.approve(self.check(reimburses_receipt=self.expense())["name"])
		frappe.db.set_value("Expense Receipt", name, "bank_transaction", "ACC-BTN-2026-00777")
		data = self.tool_data("post_reimbursement_receipt", {"receipt": name})
		self.assertEqual(data["cheque_no"], "ACC-BTN-2026-00777")
		self.assertEqual(
			frappe.db.get_value("Journal Entry", data["journal_entry"], "cheque_no"), "ACC-BTN-2026-00777"
		)

	def test_every_refusal_writes_nothing(self):
		self.seed_due_from()
		unlinked = self.approve(self.check()["name"])
		unapproved = self.check(reimburses_receipt=self.expense())["name"]
		fuel = self.approve(self.expense(category="Fuel", amount=30))
		entries_before = len(STORE.rows("Journal Entry"))
		for args, words in (
			({"receipt": unlinked}, "does not name the expense"),
			({"receipt": unapproved}, "not Approved"),
			({"receipt": fuel}, "books only"),
			({"receipt": unlinked, "credit_account": RECEIVABLE}, "without a Customer or Supplier"),
		):
			with self.subTest(args=args):
				self.assertIn(words, self.tool_error("post_reimbursement_receipt", args))
		self.assertEqual(len(STORE.rows("Journal Entry")), entries_before)
		self.tool_data("post_reimbursement_receipt", {"receipt": unlinked, "credit_account": DUE_FROM})
		self.assertIn("already linked", self.tool_error("post_reimbursement_receipt", {"receipt": unlinked}))

	def test_the_tool_ships_switched_off(self):
		self.configure(enabled=1, **{**ON, "allow_post_reimbursement_receipt": 0})
		name = self.approve(self.check(reimburses_receipt=self.expense())["name"])
		self.assertIn("switched off", self.tool_error("post_reimbursement_receipt", {"receipt": name}))


# ── 5. ───────────────────────────────────────────────────────────────────────
class TheClassifier(ReimbursementTestCase):
	def classify(self, text, merchant=""):
		return receipts.classify_receipt({"text": text, "merchant": merchant}).data

	def test_a_check_that_says_why_is_a_confident_reimbursement(self):
		data = self.classify(CHECK_TEXT)
		self.assertEqual(data["suggested_category"], CATEGORY)
		self.assertTrue(data["reimbursement"]["has_reason"])
		self.assertGreater(data["confidence"], receipts.CHECK_ONLY_CEILING)
		self.assertIn("my share", data["matched_signals"])

	def test_a_bare_check_is_a_capped_lean(self):
		"""The usual memo line: the store, and nothing about why."""
		data = self.classify(
			"PAY TO THE ORDER OF Orchard Meadow LLC\nForty-two and 25/100 DOLLARS\nMEMO Coastal"
		)
		self.assertEqual(data["suggested_category"], CATEGORY)
		self.assertFalse(data["reimbursement"]["has_reason"])
		self.assertLessEqual(data["confidence"], receipts.CHECK_ONLY_CEILING)
		self.assertIn("confirm rather than assume", data["note"])

	def test_the_payee_line_does_not_make_it_a_scale_ticket(self):
		"""'Orchard' is a weak scale-ticket word; the farm's own name is on every check."""
		self.assertEqual(self.classify(CHECK_TEXT)["reimbursement"]["claimed_by"], [])

	def test_a_packers_check_stub_is_a_settlement(self):
		data = self.classify(
			"PAY TO THE ORDER OF Orchard Meadow LLC ... DOLLARS\nGROWER SETTLEMENT pool return net proceeds"
		)
		self.assertIsNone(data["suggested_category"])
		self.assertEqual(data["receipt_type"], "settlement")
		self.assertIn("settlement", data["reimbursement"]["claimed_by"])

	def test_a_coop_retirement_check_stays_coop(self):
		data = self.classify("PAY TO THE ORDER OF Orchard Meadow LLC DOLLARS equity retirement CHS Inc")
		self.assertEqual(data["suggested_category"], "Co-op Equity")

	def test_a_fuel_slip_is_not_a_check(self):
		data = self.classify("CHEVRON PUMP 4 UNLEADED 12.4 GALLONS SUBTOTAL VISA")
		self.assertIsNone(data["suggested_category"])
		self.assertFalse(data["reimbursement"]["is_check"])

	def test_every_answer_carries_the_block(self):
		"""One shape on every branch, so the phone never has to test for the key."""
		for text in (CHECK_TEXT, "equity retirement notice", "nothing to see", "DIESEL GALLONS PUMP"):
			with self.subTest(text=text):
				self.assertIn("reimbursement", self.classify(text))


# ── 6. ───────────────────────────────────────────────────────────────────────
class FromAPhone(MobileAPITestCase):
	def test_the_link_reaches_the_tool(self):
		self.be()
		original = mobile_api.create_expense_receipt(
			merchant="Coastal Farm & Ranch", amount=84.50, receipt_date="2026-09-10", category="Supplies"
		)["name"]
		data = mobile_api.create_expense_receipt(
			merchant="Jane Polehn",
			amount=42.25,
			receipt_date="2026-09-24",
			category=CATEGORY,
			reimburses_receipt=original,
		)
		self.assertEqual(data["reimburses_receipt"], original)
		self.assertTrue(data["is_return"])

	def test_another_companys_expense_reads_as_absent(self):
		self.be()
		elsewhere = expenses.submit_expense_receipt(
			{
				"merchant": "Coastal Farm & Ranch",
				"amount": 84.50,
				"receipt_date": "2026-09-10",
				"category": "Supplies",
				"company": OTHER,
				"submitted_by": WORKER_EMPLOYEE,
			}
		).data["name"]
		with self.assertRaises(frappe.DoesNotExistError):
			mobile_api.create_expense_receipt(
				merchant="Jane Polehn",
				amount=42.25,
				receipt_date="2026-09-24",
				category=CATEGORY,
				reimburses_receipt=elsewhere,
			)

# SPDX-License-Identifier: MIT
"""Who paid: company card, personal card, or "I'm covering this". v0.332.0.

SIX CLAIMS.

1. `Capture` — the three answers are stored; impossible combinations are refused;
   a company account implies a company card; a missing draw mapping never bounces
   a capture.
2. `LearningACard` — an unknown number on a company card is learned on the chosen
   account by a caller allowed to, kept on the receipt for a bookkeeper otherwise,
   and learned from the Desk path when a bookkeeper saves it.
3. `Matching` — a personal-card slip is never proposed against a company line; an
   account the submitter named is account evidence.
4. `PersonalCard` — post_reimbursement_receipt books Dr expense / Cr what the farm
   owes the submitter, as a draft; a Payable carries the Employee.
5. `Covering` — create_owner_draw books a covered purchase to the submitter's
   mapped draw account, with every default taken from the receipt.
6. `NotABill` — neither is billed as a Purchase Invoice.
"""

from typing import ClassVar

import frappe

from erpnext_mcp.tools import banking_bridge, card_aliases, paid_with

from .fixtures import BANK_ACCOUNT, MAIN, MAIN_ABBR, MEMBER_CAPITAL, MEMBER_DISTRIBUTIONS, OTHER
from .harness import ROLES, STORE, set_roles
from .test_reimbursements import ON as REIMBURSEMENT_ON
from .test_reimbursements import ReimbursementTestCase

EMPLOYEE = "HR-EMP-00001"
MEMBER = "Member-01 - " + MAIN_ABBR
PAYABLE = f"2120 - Employee Reimbursements - {MAIN_ABBR}"

ON = {
	**REIMBURSEMENT_ON,
	**{
		f"allow_{name}": 1
		for name in (
			"create_owner_draw",
			"auto_match_receipts",
			"add_bank_card_alias",
			"update_cap_table_entry",
		)
	},
}


class WhoPaidTestCase(ReimbursementTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		self._roles_before = {user: list(held) for user, held in ROLES.items()}
		self.addCleanup(self._restore_roles)
		set_roles("Administrator", ["System Manager", "Member Manager"])
		STORE.seed(
			"Account",
			[
				{
					"name": PAYABLE,
					"account_name": "Employee Reimbursements",
					"account_number": "2120",
					"company": MAIN,
					"root_type": "Liability",
					"account_type": "Payable",
					"is_group": 0,
				}
			],
		)
		STORE.seed(
			"Cap Table Entry",
			[
				{
					"name": MEMBER,
					"member_id": "Member-01",
					"company": MAIN,
					"legal_entity_name": "Tim",
					"entity_type": "Individual",
					"admission_date": "2020-01-01",
					"ownership_percentage": 100,
					"retired": 0,
					"employee": EMPLOYEE,
					"draw_account": MEMBER_DISTRIBUTIONS,
				}
			],
		)

	def _restore_roles(self):
		ROLES.clear()
		ROLES.update(self._roles_before)

	def receipt(self, name):
		return STORE.get_raw("Expense Receipt", name)


class Capture(WhoPaidTestCase):
	def test_the_select_mirrors_the_doctype(self):
		meta = frappe.get_meta("Expense Receipt")
		options = next(field for field in meta.fields if field.fieldname == "paid_with").options
		self.assertEqual(tuple(option for option in options.split("\n") if option), paid_with.PAID_WITH)

	def test_personal_card_is_stored(self):
		data = self.tool_data("submit_expense_receipt", self._payload(paid_with="Personal card"))
		self.assertEqual(data["paid_with"], "Personal card")
		self.assertEqual(self.receipt(data["name"])["paid_with"], "Personal card")

	def test_a_company_account_implies_a_company_card(self):
		data = self.tool_data("submit_expense_receipt", self._payload(card_bank_account=BANK_ACCOUNT))
		row = self.receipt(data["name"])
		self.assertEqual((row["paid_with"], row["card_bank_account"]), ("Company card", BANK_ACCOUNT))

	def test_covering_is_stored_and_an_unmapped_person_is_told_not_refused(self):
		frappe.db.set_value("Cap Table Entry", MEMBER, "draw_account", None)
		data = self.tool_data("submit_expense_receipt", self._payload(covered_by_submitter=True))
		self.assertTrue(data["covered_by_submitter"])
		self.assertIn("No draw account is mapped", data["covering_note"])

	def test_impossible_combinations_are_refused(self):
		for overrides, words in (
			({"paid_with": "Personal card", "card_bank_account": BANK_ACCOUNT}, "company account"),
			({"paid_with": "Personal card", "covered_by_submitter": True}, "covering this"),
			({"paid_with": "Visa"}, "paid_with must be one of"),
			({"category": "Owner Draw", "covered_by_submitter": True}, "applies to a purchase"),
		):
			with self.subTest(overrides=overrides):
				self.assertIn(words, self.tool_error("submit_expense_receipt", self._payload(**overrides)))

	def test_a_receipt_saying_nothing_is_unchanged(self):
		data = self.tool_data("submit_expense_receipt", self._payload())
		self.assertIsNone(data["paid_with"])
		self.assertFalse(self.receipt(data["name"]).get("covered_by_submitter"))

	def _payload(self, **overrides):
		payload = {
			"merchant": "Coastal Farm & Ranch",
			"amount": 84.50,
			"receipt_date": "2026-09-10",
			"category": "Supplies",
			"company": MAIN,
			"submitted_by": EMPLOYEE,
			"card_last_four": "7637",
		}
		payload.update(overrides)
		return payload


class LearningACard(WhoPaidTestCase):
	def capture(self, **overrides):
		return self.tool_data("submit_expense_receipt", Capture._payload(self, **overrides))

	def test_an_unknown_number_is_learned_on_the_chosen_account(self):
		data = self.capture(card_bank_account=BANK_ACCOUNT, card_alias_label="Tim's iPhone")
		self.assertEqual(data["card_learned"]["action"], "created")
		self.assertEqual(data["card"]["number"], "7637")
		self.assertTrue(self.receipt(data["name"])["card_alias"])
		self.assertIn("7637", card_aliases.load_index(MAIN))

	def test_learned_as_a_device_of_a_known_card(self):
		card_aliases.register(BANK_ACCOUNT, "0845", "0845")
		data = self.capture(card_bank_account=BANK_ACCOUNT, card_alias_card="0845")
		self.assertEqual(data["card_learned"]["alias"]["card_last_four"], "0845")
		self.assertEqual(data["card_learned"]["alias"]["kind"], "Apple Pay")

	def test_a_caller_without_the_role_leaves_it_for_a_bookkeeper(self):
		set_roles("Administrator", ["Employee"])
		data = self.capture(card_bank_account=BANK_ACCOUNT)
		self.assertEqual(data["card_learned"]["action"], "pending")
		self.assertNotIn("7637", card_aliases.load_index(MAIN))
		self.assertEqual(self.receipt(data["name"])["card_bank_account"], BANK_ACCOUNT)
		# A bookkeeper saving it from the Desk learns it.
		set_roles("Administrator", ["Accounts Manager"])
		result = paid_with.learn_from_saved_receipt(frappe.get_doc("Expense Receipt", data["name"]))
		self.assertEqual(result["action"], "created")
		self.assertIn("7637", card_aliases.load_index(MAIN))

	def test_save_card_alias_false_learns_nothing(self):
		data = self.capture(card_bank_account=BANK_ACCOUNT, save_card_alias=False)
		self.assertIsNone(data["card_learned"])
		self.assertNotIn("7637", card_aliases.load_index(MAIN))

	def test_the_desk_choice_over_update_learns_it(self):
		name = self.capture()["name"]
		data = self.tool_data(
			"update_expense_receipt",
			{"name": name, "paid_with": "Company card", "card_bank_account": BANK_ACCOUNT},
		)
		self.assertEqual(data["card_learned"]["action"], "created")

	def test_the_phone_list_says_which_numbers_are_known(self):
		card_aliases.register(BANK_ACCOUNT, "0845", "7637", label="Tim's iPhone")
		data = paid_with.company_cards([MAIN], EMPLOYEE)
		self.assertEqual(data["known_numbers"], ["7637"])
		self.assertTrue(data["draw_mapped"][MAIN])
		account = next(row for row in data["accounts"] if row["bank_account"] == BANK_ACCOUNT)
		self.assertEqual(account["cards"][0]["card_last_four"], "0845")


class Matching(WhoPaidTestCase):
	TRANSACTION: ClassVar[dict] = {
		"name": "BT",
		"date": "2026-09-11",
		"bank_account": BANK_ACCOUNT,
		"description": "COASTAL FARM CARD 0845",
		"gross_amount": 84.50,
		"direction": "Withdrawal",
	}

	def score(self, **receipt):
		row = {
			"name": "R",
			"amount": 84.50,
			"receipt_date": "2026-09-10",
			"merchant": "Coastal",
			"company": MAIN,
		}
		row.update(receipt)
		return banking_bridge.score_match(row, dict(self.TRANSACTION), tolerance=0.02, window=7, cards={})

	def test_a_personal_card_slip_is_never_eligible(self):
		score = self.score(paid_with="Personal card")
		self.assertFalse(score["eligible"])
		self.assertIn("personal card", " ".join(score["blockers"]))

	def test_a_named_account_elsewhere_is_a_mild_penalty(self):
		before = self.score()
		after = self.score(card_bank_account="Savings - Example Bank")
		self.assertTrue(after["signals"]["card_account_mismatch"])
		self.assertLess(after["confidence"], before["confidence"])
		self.assertEqual(self.score(card_bank_account=BANK_ACCOUNT)["confidence"], before["confidence"])


class PersonalCard(WhoPaidTestCase):
	def personal(self, **overrides):
		return self.approve(self.expense(paid_with="Personal card", **overrides))

	def test_dr_expense_cr_the_members_draw(self):
		name = self.personal()
		data = self.tool_data("post_reimbursement_receipt", {"receipt": name})
		self.assertEqual(data["docstatus"], 0)
		self.assertEqual(data["credit_account"], MEMBER_DISTRIBUTIONS)
		self.assertIn("Cap Table Entry", data["credit_account_resolved_by"])
		lines = self.lines(data["journal_entry"])
		self.assertIn((data["expense_account"], 84.5, 0.0), lines)
		self.assertIn((MEMBER_DISTRIBUTIONS, 0.0, 84.5), lines)
		self.assertEqual(self.receipt(name)["linked_document"], data["journal_entry"])

	def test_a_payable_carries_the_employee(self):
		name = self.personal()
		data = self.tool_data("post_reimbursement_receipt", {"receipt": name, "credit_account": PAYABLE})
		doc = frappe.get_doc("Journal Entry", data["journal_entry"])
		credit = next(
			line
			for line in doc.get("accounts")
			if (line.get("account") if isinstance(line, dict) else line.account) == PAYABLE
		)
		get = credit.get if isinstance(credit, dict) else (lambda key: getattr(credit, key, None))
		self.assertEqual((get("party_type"), get("party")), ("Employee", EMPLOYEE))

	def test_no_mapping_and_no_credit_account_is_refused(self):
		frappe.db.set_value("Cap Table Entry", MEMBER, "draw_account", None)
		name = self.personal()
		self.assertIn("credit_account", self.tool_error("post_reimbursement_receipt", {"receipt": name}))

	def test_not_approved_is_refused(self):
		name = self.expense(paid_with="Personal card")
		self.assertIn("not Approved", self.tool_error("post_reimbursement_receipt", {"receipt": name}))

	def test_an_ordinary_expense_is_still_refused(self):
		name = self.approve(self.expense())
		self.assertIn("personal card", self.tool_error("post_reimbursement_receipt", {"receipt": name}))

	def test_who_paid_is_fixed_once_booked(self):
		name = self.personal()
		self.tool_data("post_reimbursement_receipt", {"receipt": name})
		error = self.tool_error("update_expense_receipt", {"name": name, "paid_with": "Company card"})
		self.assertIn("already booked", error)


class Covering(WhoPaidTestCase):
	def covered(self, **overrides):
		return self.approve(
			self.expense(covered_by_submitter=True, card_bank_account=BANK_ACCOUNT, **overrides)
		)

	def test_booked_to_the_mapped_draw_with_every_default_from_the_receipt(self):
		name = self.covered()
		data = self.tool_data("create_owner_draw", {"receipt": name})
		self.assertEqual(data["docstatus"], 0)
		self.assertEqual(data["draw_account"], MEMBER_DISTRIBUTIONS)
		self.assertIn("Cap Table Entry", data["draw_account_resolved_by"])
		self.assertEqual((data["amount"], data["company"]), (84.5, MAIN))
		self.assertTrue(data["covered_by_submitter"])
		self.assertIn("Covered personally", data["narrative"])
		self.assertEqual(self.receipt(name)["linked_document"], data["name"])

	def test_the_mapping_is_set_through_update_cap_table_entry(self):
		self.tool_data("update_cap_table_entry", {"member": MEMBER, "draw_account": MEMBER_CAPITAL})
		data = self.tool_data("create_owner_draw", {"receipt": self.covered()})
		self.assertEqual(data["draw_account"], MEMBER_CAPITAL)

	def test_a_draw_account_must_be_equity(self):
		error = self.tool_error("update_cap_table_entry", {"member": MEMBER, "draw_account": PAYABLE})
		self.assertIn("Equity", error)

	def test_unmapped_is_refused_at_booking(self):
		frappe.db.set_value("Cap Table Entry", MEMBER, "draw_account", None)
		self.assertIn(
			"no draw account is mapped", self.tool_error("create_owner_draw", {"receipt": self.covered()})
		)

	def test_not_approved_is_refused(self):
		name = self.expense(covered_by_submitter=True)
		self.assertIn("not Approved", self.tool_error("create_owner_draw", {"receipt": name}))


class NotABill(WhoPaidTestCase):
	def test_neither_becomes_a_purchase_invoice(self):
		for overrides, words in (
			({"covered_by_submitter": True}, "create_owner_draw"),
			({"paid_with": "Personal card"}, "post_reimbursement_receipt"),
		):
			with self.subTest(overrides=overrides):
				name = self.approve(self.expense(**overrides))
				error = self.tool_error("create_purchase_invoice_from_receipt", {"receipt": name})
				self.assertIn(words, error)


del OTHER


# ── 7. from a phone ─────────────────────────────────────────────────────────
from erpnext_mcp.api import mobile as mobile_api  # noqa: E402

from .test_api_mobile import WORKER, MobileAPITestCase  # noqa: E402


class FromAPhone(MobileAPITestCase):
	def setUp(self):
		super().setUp()
		self._roles_before = {user: list(held) for user, held in ROLES.items()}
		self.addCleanup(lambda: (ROLES.clear(), ROLES.update(self._roles_before)))

	def capture(self, **extra):
		return mobile_api.create_expense_receipt(
			merchant="Chevron",
			amount=47.83,
			receipt_date="2026-09-10",
			category="Fuel",
			card_last_four="6991",
			**extra,
		)

	def test_personal_card_and_covering_reach_the_tool(self):
		self.be()
		self.assertEqual(self.capture(paid_with="Personal card")["paid_with"], "Personal card")
		self.assertTrue(self.capture(covered_by_submitter="1")["covered_by_submitter"])

	def test_a_field_worker_s_card_is_kept_for_a_bookkeeper(self):
		self.be()
		data = self.capture(card_bank_account=BANK_ACCOUNT, card_alias_label="Truck phone")
		self.assertEqual(data["card_learned"]["action"], "pending")
		self.assertNotIn("6991", card_aliases.load_index(MAIN))

	def test_a_farm_manager_s_card_is_learned(self):
		set_roles(WORKER, ["Field Worker", "Farm Manager"])
		self.be()
		data = self.capture(card_bank_account=BANK_ACCOUNT)
		self.assertEqual(data["card_learned"]["action"], "created")
		listed = mobile_api.list_card_numbers()
		self.assertIn("6991", listed["known_numbers"])
		self.assertTrue(listed["may_save_card"])

	def test_the_card_list_for_a_field_worker(self):
		card_aliases.register(BANK_ACCOUNT, "0845", "7637")
		self.be()
		listed = mobile_api.list_card_numbers()
		self.assertEqual(listed["known_numbers"], ["7637"])
		self.assertFalse(listed["may_save_card"])
		self.assertEqual({row["bank_account"] for row in listed["accounts"]}, {BANK_ACCOUNT})

	def test_an_unknown_account_reads_as_absent(self):
		self.be()
		with self.assertRaises(frappe.DoesNotExistError):
			self.capture(card_bank_account="Nowhere - Bank")

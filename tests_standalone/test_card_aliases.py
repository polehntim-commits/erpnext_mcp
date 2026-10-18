# SPDX-License-Identifier: MIT
"""Bank Card Alias — an Apple Pay receipt's device number matched as its card.

v0.331.0. A phone payment prints the DEVICE account number on the slip (••7637)
while the bank prints the card (CARD 0845). These tests hold:

 1. `TheRegister` — add / list / remove, the refusals, and reactivation.
 2. `Scoring` — an alias hit is a fingerprint; unregistered digits change nothing;
    a card registered only on another account costs a mild, non-blocking penalty.
 3. `Tagging` — capture tags the receipt with the card; adding an alias tags the
    receipts scanned before it.
 4. `AutoMatching` — two identical slips, one paid by phone, one by card, are told
    apart instead of contested.
"""

import json
import pathlib

from erpnext_mcp.tools import banking_bridge, card_aliases

from .fixtures import BANK_ACCOUNT, MAIN, SeededTestCase, install_hrms
from .harness import STORE

SAVINGS = "Savings - Example Bank"
EMPLOYEE = "HR-EMP-00001"
BANK_TRANSACTION = banking_bridge.BANK_TRANSACTION
EXPENSE_RECEIPT = banking_bridge.EXPENSE_RECEIPT

TOOLS_ON = {
	f"allow_{name}": 1
	for name in (
		"list_bank_card_aliases",
		"add_bank_card_alias",
		"remove_bank_card_alias",
		"submit_expense_receipt",
		"auto_match_receipts",
		"match_receipt_to_bank_transaction",
	)
}

STATEMENT = (
	("BT-WF-0845", "2026-06-15", "PURCHASE AUTHORIZED ON 06/14 CHEVRON PASCO WA CARD 0845", 47.83),
	("BT-WF-4417", "2026-06-15", "PURCHASE AUTHORIZED ON 06/14 CHEVRON PASCO WA CARD 4417", 47.83),
)


class CardAliasTestCase(SeededTestCase):
	def setUp(self):
		super().setUp()
		install_hrms()
		self.configure(enabled=1, **TOOLS_ON)
		STORE.seed(
			"Bank Account",
			[
				{
					"name": SAVINGS,
					"account_name": "Savings",
					"bank": "Example Bank",
					"company": MAIN,
					"is_company_account": 1,
				}
			],
		)
		STORE.seed(
			BANK_TRANSACTION,
			[
				{
					"name": name,
					"date": date,
					"bank_account": BANK_ACCOUNT,
					"company": MAIN,
					"description": description,
					"status": "Unreconciled",
					"deposit": 0,
					"withdrawal": withdrawal,
					"allocated_amount": 0,
					"unallocated_amount": withdrawal,
					"currency": "USD",
					"docstatus": 1,
					"payment_entries": [],
				}
				for name, date, description, withdrawal in STATEMENT
			],
		)

	def add(self, **payload):
		payload.setdefault("bank_account", BANK_ACCOUNT)
		return self.tool_data("add_bank_card_alias", payload)

	def register_iphone(self):
		self.add(card_last_four="0845", label="WF business card")
		return self.add(card_last_four="0845", number="7637", label="Apple Pay – Tim's iPhone")

	def capture(self, card, **overrides):
		payload = {
			"merchant": "Chevron",
			"amount": 47.83,
			"receipt_date": "2026-06-14",
			"category": "Fuel",
			"company": MAIN,
			"submitted_by": EMPLOYEE,
			"card_last_four": card,
		}
		payload.update(overrides)
		return self.tool_data("submit_expense_receipt", payload)

	def score(self, card, transaction="BT-WF-0845", **receipt):
		row = {
			"name": "R",
			"amount": 47.83,
			"receipt_date": "2026-06-14",
			"merchant": "Chevron",
			"company": MAIN,
			"card_last_four": card,
			**receipt,
		}
		return banking_bridge.score_match(
			row,
			banking_bridge._transaction_row(transaction),
			tolerance=0.02,
			window=7,
			cards=card_aliases.load_index(MAIN),
		)


class TheRegister(CardAliasTestCase):
	def test_kinds_mirror_the_doctype_select(self):
		path = pathlib.Path(card_aliases.__file__).parent.parent / (
			"erpnext_mcp/doctype/bank_card_alias/bank_card_alias.json"
		)
		meta = json.loads(path.read_text())
		kind = next(field for field in meta["fields"] if field["fieldname"] == "kind")
		self.assertEqual(tuple(kind["options"].split("\n")), card_aliases.KINDS)

	def test_a_card_and_its_phone_list_as_one_card(self):
		data = self.register_iphone()
		self.assertEqual(data["alias"]["kind"], "Apple Pay")
		self.assertEqual(data["alias"]["company"], MAIN)
		listed = self.tool_data("list_bank_card_aliases", {"bank_account": BANK_ACCOUNT})
		self.assertEqual(len(listed["cards"]), 1)
		self.assertEqual(sorted(row["number"] for row in listed["cards"][0]["numbers"]), ["0845", "7637"])

	def test_masked_digits_are_accepted_and_a_full_number_is_refused(self):
		self.assertEqual(self.add(card_last_four="••0845")["alias"]["number"], "0845")
		error = self.tool_error(
			"add_bank_card_alias",
			{"bank_account": BANK_ACCOUNT, "card_last_four": "4111111111110845"},
		)
		self.assertIn("LAST FOUR", error)

	def test_a_card_row_must_carry_its_own_number(self):
		error = self.tool_error(
			"add_bank_card_alias",
			{"bank_account": BANK_ACCOUNT, "card_last_four": "0845", "number": "7637", "kind": "Card"},
		)
		self.assertIn("card itself", error)

	def test_duplicates_and_a_number_on_another_card_are_refused(self):
		self.register_iphone()
		self.assertIn(
			"already registered",
			self.tool_error(
				"add_bank_card_alias",
				{"bank_account": BANK_ACCOUNT, "card_last_four": "0845", "number": "7637"},
			),
		)
		self.assertIn(
			"number of card 0845",
			self.tool_error(
				"add_bank_card_alias",
				{"bank_account": BANK_ACCOUNT, "card_last_four": "4417", "number": "7637"},
			),
		)

	def test_remove_deactivates_and_add_brings_it_back(self):
		name = self.register_iphone()["alias"]["name"]
		removed = self.tool_data("remove_bank_card_alias", {"bank_account": BANK_ACCOUNT, "number": "7637"})
		self.assertFalse(removed["deleted"])
		self.assertEqual(STORE.get_raw(card_aliases.DOCTYPE, name)["active"], 0)
		self.assertNotIn("7637", card_aliases.load_index(MAIN))
		self.assertIn("already inactive", self.tool_error("remove_bank_card_alias", {"name": name}))
		back = self.add(card_last_four="0845", number="7637")
		self.assertTrue(back["reactivated"])
		self.assertEqual(back["alias"]["name"], name)


class Scoring(CardAliasTestCase):
	def test_a_device_number_is_not_a_fingerprint_without_the_register(self):
		score = self.score("7637")
		self.assertFalse(score["card_fingerprint"])
		self.assertFalse(score["signals"]["card_alias_hit"])

	def test_a_registered_device_number_is_a_fingerprint(self):
		before = self.score("7637")
		self.register_iphone()
		after = self.score("7637")
		self.assertTrue(after["card_fingerprint"])
		self.assertTrue(after["signals"]["card_alias_hit"])
		self.assertEqual(after["signals"]["card"]["card_last_four"], "0845")
		self.assertEqual(after["signals"]["card"]["number"], "7637")
		self.assertGreater(after["confidence"], before["confidence"])

	def test_the_alias_does_not_confirm_another_cards_line(self):
		self.register_iphone()
		score = self.score("7637", transaction="BT-WF-4417")
		self.assertFalse(score["card_fingerprint"])

	def test_unregistered_digits_score_exactly_as_before(self):
		self.register_iphone()
		with_index = self.score("9999")
		without = banking_bridge.score_match(
			{
				"name": "R",
				"amount": 47.83,
				"receipt_date": "2026-06-14",
				"merchant": "Chevron",
				"company": MAIN,
				"card_last_four": "9999",
			},
			banking_bridge._transaction_row("BT-WF-0845"),
			tolerance=0.02,
			window=7,
		)
		self.assertEqual(with_index["confidence"], without["confidence"])
		self.assertFalse(with_index["signals"]["card_account_mismatch"])

	def test_a_card_on_another_account_costs_a_mild_penalty_not_a_block(self):
		before = self.score("6991")
		self.add(bank_account=SAVINGS, card_last_four="2222", number="6991", label="Savings phone")
		after = self.score("6991")
		self.assertTrue(after["eligible"])
		self.assertTrue(after["signals"]["card_account_mismatch"])
		self.assertAlmostEqual(
			before["confidence"] - after["confidence"], banking_bridge.CARD_ACCOUNT_MISMATCH_PENALTY, places=4
		)
		self.assertGreaterEqual(after["confidence"], banking_bridge.DEFAULT_MIN_CONFIDENCE)


class Tagging(CardAliasTestCase):
	def test_capture_tags_the_card_a_device_number_stands_for(self):
		alias = self.register_iphone()["alias"]["name"]
		data = self.capture("7637")
		self.assertEqual(data["card"]["card_last_four"], "0845")
		self.assertEqual(STORE.get_raw(EXPENSE_RECEIPT, data["name"])["card_alias"], alias)

	def test_an_unknown_card_is_not_tagged(self):
		data = self.capture("9999")
		self.assertIsNone(data["card"])
		self.assertFalse(STORE.get_raw(EXPENSE_RECEIPT, data["name"]).get("card_alias"))

	def test_adding_an_alias_tags_receipts_scanned_before_it(self):
		earlier = self.capture("7637")["name"]
		data = self.register_iphone()
		self.assertEqual(data["receipts_tagged"], [earlier])
		self.assertEqual(STORE.get_raw(EXPENSE_RECEIPT, earlier)["card_alias"], data["alias"]["name"])

	def test_a_colliding_number_tags_nothing(self):
		self.add(bank_account=SAVINGS, card_last_four="2222", number="7637", kind="Google Pay")
		data = self.register_iphone()
		self.assertEqual(data["receipts_tagged"], [])
		self.assertIn("also registered", data["warning"])
		later = self.capture("7637")
		self.assertIsNone(later["card"])
		self.assertFalse(STORE.get_raw(EXPENSE_RECEIPT, later["name"]).get("card_alias"))


class AutoMatching(CardAliasTestCase):
	def test_a_phone_slip_and_a_card_slip_are_told_apart(self):
		"""Two $47.83 fill-ups on one day: one paid by iPhone, one by card 4417."""
		self.register_iphone()
		phone = self.capture("7637")["name"]
		card = self.capture("4417")["name"]
		data = self.tool_data("auto_match_receipts", {"company": MAIN})
		self.assertEqual(data["contested"], [])
		self.assertEqual(data["card_fingerprint_count"], 2)
		by_receipt = {row["expense_receipt"]: row["bank_transaction"] for row in data["proposals"]}
		self.assertEqual(by_receipt, {phone: "BT-WF-0845", card: "BT-WF-4417"})
		self.assertEqual(data["settings"]["card_aliases_registered"], 2)

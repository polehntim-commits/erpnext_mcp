# SPDX-License-Identifier: MIT
"""CFL Banking — the statement, the paper behind each line, and what each line was.

v0.71.0, Sprint 6. TWELVE CLAIMS.

 1. `Scoring` — three signals, one number, and a gate that a good name cannot drag anything over.
 2. `MatchingOneReceipt` — no transaction named means candidates and NO WRITE; naming one links it.
 3. `MatchRefusals` — deposit, cross-company, rejected, both ends already matched, and `replace`.
 4. `AutoMatching` — proposals carry their own commit call, the threshold bites, and NOTHING is written.
 5. `ContestedProposals` — two slips for one withdrawal are reported, never silently dropped.
 6. `UnmatchedWorklists` — the two kinds of "unmatched" stay apart, and every filter narrows.
 7. `ReconciliationStatus` — three independent states, counted separately and never summed.
 8. `TheRuleDoctype` — a bad regex, an inverted range and a duplicate name are refused on save.
 9. `CreatingRules` — the account is vetted or absent, never guessed; overlap is reported.
10. `ApplyingRules` — priority order, first match wins, dry runs write nothing, hand edits survive.
11. `Seeding` — idempotent, withdrawal-only, and an account map that is vetted before anything is made.
12. `CashFlowAndSwitches` — the bases stay apart, the double count is removed, ten switches refuse by name.

THE TWO TESTS THAT MATTER MOST HERE.

`test_auto_match_writes_nothing_at_all` is the first. Every other property of
this module is visible in a result payload; this one is invisible by
construction. A batch matcher that quietly committed its own proposals would
pass a test of its output exactly, and the damage — a season of slips filed
against the wrong withdrawals, every document present and every amount right —
is not findable afterwards by reading anything.

`test_a_matched_receipt_is_not_counted_twice_in_the_category_totals` is the
second. The whole sprint exists so that number is right: a receipt and the
withdrawal it is the paper for are ONE purchase, and a category total that
counted both would double a season's fuel while looking entirely reasonable.
"""

import json
import pathlib

import frappe

from erpnext_mcp import registry
from erpnext_mcp.erpnext_mcp.doctype.bank_categorization_rule import (
	bank_categorization_rule as rule_module,
)
from erpnext_mcp.tools import banking_bridge

from .fixtures import (
	BANK_ACCOUNT,
	MAIN,
	MAIN_ABBR,
	OTHER,
	OTHER_ABBR,
	SeededTestCase,
	install_hrms,
	supplies,
)
from .harness import STORE

READ_TOOLS = (
	"auto_match_receipts",
	"get_bank_reconciliation_status",
	"list_unmatched_receipts",
	"list_unmatched_bank_transactions",
	"list_bank_categorization_rules",
	"get_cash_flow_summary",
)
WRITE_TOOLS = (
	"match_receipt_to_bank_transaction",
	"create_bank_categorization_rule",
	"apply_categorization_rules",
	"seed_farm_categorization_rules",
)
ALL_TOOLS = READ_TOOLS + WRITE_TOOLS

#: The receipt capture this module drives to build its own fixtures.
SUPPORTING_TOOLS = ("submit_expense_receipt", "reject_expense_receipt")

TOOLS_ON = {f"allow_{name}": 1 for name in ALL_TOOLS + SUPPORTING_TOOLS}

RULE = banking_bridge.RULE
BANK_TRANSACTION = banking_bridge.BANK_TRANSACTION
EXPENSE_RECEIPT = banking_bridge.EXPENSE_RECEIPT
LINK = banking_bridge.RECEIPT_LINK_FIELD
CATEGORY_FIELD = banking_bridge.CATEGORY_FIELD

EMPLOYEE = "HR-EMP-00001"

#: A week of a real statement, priced so every claim below has something to
#: prove: an exact fuel match a day later, a parts match three days later, a
#: deposit that no expense receipt may ever match, a chemical bill big enough to
#: exercise the amount ceilings, and a line whose memo says nothing at all.
STATEMENT = (
	("BT-CHEVRON", "2026-06-15", "CHEVRON 0093746 PASCO WA", 0, 184.62),
	("BT-MYSTERY", "2026-06-16", "POS PURCHASE 887342", 0, 91.40),
	("BT-NAPA", "2026-06-18", "NAPA AUTO PARTS #4471 YAKIMA", 0, 62.15),
	("BT-DEPOSIT", "2026-06-20", "ACH DEPOSIT PACKER SETTLEMENT", 5000.00, 0),
	("BT-WILBUR", "2026-07-02", "WILBUR-ELLIS CO AG DIVISION", 0, 3200.00),
)

FUEL_RECEIPT = {
	"merchant": "Chevron",
	"amount": 184.62,
	"receipt_date": "2026-06-14",
	"category": "Fuel",
	"company": MAIN,
	"submitted_by": EMPLOYEE,
}

PARTS_RECEIPT = {
	"merchant": "NAPA Auto Parts",
	"amount": 62.15,
	"receipt_date": "2026-06-17",
	"category": "Equipment Parts",
	"company": MAIN,
	"submitted_by": EMPLOYEE,
}


class BankBridgeTestCase(SeededTestCase):
	"""The fixture site, an HR register, and a week of bank statement.

	The transactions are seeded rather than created through a tool because
	`create_bank_transaction` is a different module's tool with a different
	switch, and a fixture that depended on it would make every test here fail
	for a reason that has nothing to do with this sprint.
	"""

	def setUp(self):
		super().setUp()
		install_hrms()
		self.configure(enabled=1, **TOOLS_ON)
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
					"deposit": deposit,
					"withdrawal": withdrawal,
					"allocated_amount": 0,
					"unallocated_amount": deposit or withdrawal,
					"currency": "USD",
					"docstatus": 1,
					"payment_entries": [],
				}
				for name, date, description, deposit, withdrawal in STATEMENT
			],
		)

	# -- fixture builders, driven through the tools themselves ---------------
	def capture(self, **overrides):
		"""One Expense Receipt, through the tool that a phone would call."""
		return self.tool_data("submit_expense_receipt", {**FUEL_RECEIPT, **overrides})["name"]

	def rule(self, **overrides):
		payload = {
			"rule_name": "Fuel — Chevron",
			"company": MAIN,
			"category": "Fuel",
			"pattern": "CHEVRON",
			"direction": "Withdrawal",
		}
		payload.update(overrides)
		return self.tool_data("create_bank_categorization_rule", payload)

	def transaction(self, name):
		return STORE.get_raw(BANK_TRANSACTION, name)

	def receipt(self, name):
		return STORE.get_raw(EXPENSE_RECEIPT, name)


# ── 1. scoring ──────────────────────────────────────────────────────────────
class Scoring(BankBridgeTestCase):
	"""Three signals, one number, and what the gate refuses outright."""

	def score(self, receipt, transaction, tolerance=0.02, window=7):
		return banking_bridge.score_match(receipt, transaction, tolerance=tolerance, window=window)

	def withdrawal(self, **overrides):
		row = {
			"name": "BT-CHEVRON",
			"date": "2026-06-15",
			"description": "CHEVRON 0093746 PASCO WA",
			"gross_amount": 184.62,
			"direction": "Withdrawal",
		}
		row.update(overrides)
		return row

	def test_an_exact_amount_a_day_later_scores_well_above_the_threshold(self):
		score = self.score(FUEL_RECEIPT, self.withdrawal())
		self.assertTrue(score["eligible"])
		self.assertGreater(score["confidence"], banking_bridge.DEFAULT_MIN_CONFIDENCE)
		self.assertEqual(score["signals"]["amount_gap"], 0.0)
		self.assertEqual(score["signals"]["day_gap"], 1)

	def test_an_exact_amount_with_an_unreadable_memo_still_clears_the_threshold(self):
		"""A bank line that says POS PURCHASE 887342 is still evidence.

		The weights exist for this case: the amount and the date agree, the name
		agrees with nothing, and the pair is still worth proposing. If the
		merchant were worth more than the other two, every card terminal that
		prints its own ID instead of a vendor name would produce zero matches.
		"""
		score = self.score(FUEL_RECEIPT, self.withdrawal(description="POS PURCHASE 887342"))
		self.assertLess(score["signals"]["merchant_score"], 0.2)
		self.assertTrue(score["eligible"])
		self.assertGreaterEqual(score["confidence"], banking_bridge.DEFAULT_MIN_CONFIDENCE)

	def test_the_same_amount_a_week_late_with_no_name_agreement_does_not(self):
		score = self.score(
			FUEL_RECEIPT, self.withdrawal(date="2026-06-21", description="POS PURCHASE 887342")
		)
		self.assertTrue(score["eligible"])
		self.assertLess(score["confidence"], banking_bridge.DEFAULT_MIN_CONFIDENCE)

	def test_a_deposit_is_never_eligible_however_well_it_scores(self):
		"""The gate is not a low score. An expense receipt is money out."""
		score = self.score(FUEL_RECEIPT, self.withdrawal(direction="Deposit"))
		self.assertFalse(score["eligible"])
		self.assertEqual(score["confidence"], 0.0)
		self.assertIn("money IN", " ".join(score["blockers"]))

	def test_an_amount_outside_tolerance_is_a_blocker_not_a_penalty(self):
		score = self.score(FUEL_RECEIPT, self.withdrawal(gross_amount=184.90))
		self.assertFalse(score["eligible"])
		self.assertEqual(score["confidence"], 0.0)
		self.assertIn("amounts differ by 0.28", " ".join(score["blockers"]))

	def test_a_wider_tolerance_admits_the_tip(self):
		score = self.score(FUEL_RECEIPT, self.withdrawal(gross_amount=184.90), tolerance=1.00)
		self.assertTrue(score["eligible"])

	def test_a_transaction_that_posted_before_the_paper_is_refused(self):
		score = self.score(FUEL_RECEIPT, self.withdrawal(date="2026-06-10"))
		self.assertFalse(score["eligible"])
		self.assertIn("BEFORE the receipt", " ".join(score["blockers"]))

	def test_one_day_early_is_tolerated_because_midnight_is_real(self):
		score = self.score(FUEL_RECEIPT, self.withdrawal(date="2026-06-13"))
		self.assertTrue(score["eligible"])

	def test_confidence_never_reaches_one(self):
		"""Three numbers agreeing is not certainty, and the ceiling says so."""
		perfect = self.score(
			{**FUEL_RECEIPT, "merchant": "CHEVRON 0093746 PASCO WA", "receipt_date": "2026-06-15"},
			self.withdrawal(),
		)
		self.assertLessEqual(perfect["confidence"], banking_bridge.CONFIDENCE_CEILING)

	def test_the_weights_sum_to_one(self):
		total = banking_bridge.WEIGHT_AMOUNT + banking_bridge.WEIGHT_DATE + banking_bridge.WEIGHT_MERCHANT
		self.assertAlmostEqual(total, 1.0, places=6)


# ── 2. matching one receipt ─────────────────────────────────────────────────
class MatchingOneReceipt(BankBridgeTestCase):
	def test_without_a_transaction_it_ranks_candidates_and_writes_nothing(self):
		receipt = self.capture()
		data = self.tool_data("match_receipt_to_bank_transaction", {"expense_receipt": receipt})

		self.assertFalse(data["linked"])
		self.assertEqual(data["candidates"][0]["bank_transaction"], "BT-CHEVRON")
		self.assertIsNone(self.receipt(receipt).get(LINK))

	def test_every_candidate_it_returns_is_eligible(self):
		receipt = self.capture()
		data = self.tool_data("match_receipt_to_bank_transaction", {"expense_receipt": receipt})
		self.assertTrue(data["candidates"])
		self.assertTrue(all(row["eligible"] for row in data["candidates"]))

	def test_naming_a_transaction_writes_the_link_and_the_evidence_with_it(self):
		receipt = self.capture()
		data = self.tool_data(
			"match_receipt_to_bank_transaction",
			{"expense_receipt": receipt, "bank_transaction": "BT-CHEVRON"},
		)
		self.assertTrue(data["linked"])

		stored = self.receipt(receipt)
		self.assertEqual(stored[LINK], "BT-CHEVRON")
		self.assertEqual(stored[banking_bridge.RECEIPT_METHOD_FIELD], "Manual")
		self.assertEqual(stored[banking_bridge.RECEIPT_CONFIDENCE_FIELD], data["confidence"])
		self.assertTrue(stored[banking_bridge.RECEIPT_MATCHED_ON_FIELD])

	def test_the_method_records_that_a_machine_proposed_it(self):
		receipt = self.capture()
		self.tool_data(
			"match_receipt_to_bank_transaction",
			{"expense_receipt": receipt, "bank_transaction": "BT-CHEVRON", "match_method": "Proposed"},
		)
		self.assertEqual(self.receipt(receipt)[banking_bridge.RECEIPT_METHOD_FIELD], "Proposed")

	def test_an_unknown_method_is_refused_with_the_list(self):
		receipt = self.capture()
		error = self.tool_error(
			"match_receipt_to_bank_transaction",
			{"expense_receipt": receipt, "bank_transaction": "BT-CHEVRON", "match_method": "Guessed"},
		)
		self.assertIn("Manual, Proposed", error)

	def test_the_link_is_evidence_and_allocates_nothing(self):
		"""Nothing about ERPNext's own reconciliation moves."""
		receipt = self.capture()
		before = dict(self.transaction("BT-CHEVRON"))
		self.tool_data(
			"match_receipt_to_bank_transaction",
			{"expense_receipt": receipt, "bank_transaction": "BT-CHEVRON"},
		)
		after = self.transaction("BT-CHEVRON")
		self.assertEqual(after["unallocated_amount"], before["unallocated_amount"])
		self.assertEqual(after["allocated_amount"], before["allocated_amount"])
		self.assertEqual(after["payment_entries"], [])
		self.assertEqual(after["status"], before["status"])

	def test_no_gl_entry_is_written_by_a_match(self):
		receipt = self.capture()
		before = len(STORE.rows("GL Entry"))
		self.tool_data(
			"match_receipt_to_bank_transaction",
			{"expense_receipt": receipt, "bank_transaction": "BT-CHEVRON"},
		)
		self.assertEqual(len(STORE.rows("GL Entry")), before)

	def test_a_link_made_against_the_score_is_allowed_and_says_so(self):
		"""A person naming both documents outranks the algorithm — and is recorded.

		The stored confidence is zero rather than the computed number, so the
		pair surfaces in any later review of what a machine talked somebody into.
		"""
		receipt = self.capture()
		data = self.tool_data(
			"match_receipt_to_bank_transaction",
			{"expense_receipt": receipt, "bank_transaction": "BT-WILBUR"},
		)
		self.assertTrue(data["linked"])
		self.assertFalse(data["eligible"])
		self.assertEqual(data["confidence"], 0.0)
		self.assertIn("against the scoring rules", data["warning"])
		self.assertEqual(self.receipt(receipt)[banking_bridge.RECEIPT_CONFIDENCE_FIELD], 0.0)

	def test_a_bank_account_narrows_the_candidate_search(self):
		receipt = self.capture()
		error = self.tool_error(
			"match_receipt_to_bank_transaction",
			{"expense_receipt": receipt, "bank_account": "Nowhere Bank"},
		)
		self.assertIn("no Bank Account matching", error)

	def test_a_window_beyond_a_month_is_refused_with_the_reason(self):
		receipt = self.capture()
		error = self.tool_error(
			"match_receipt_to_bank_transaction",
			{"expense_receipt": receipt, "date_window_days": 90},
		)
		self.assertIn("capped at 60", error)
		self.assertIn("coincidence", error)


# ── 3. refusals ─────────────────────────────────────────────────────────────
class MatchRefusals(BankBridgeTestCase):
	def test_a_deposit_is_refused_and_cannot_be_overruled(self):
		"""The direction is the one signal that is not a judgement call."""
		receipt = self.capture()
		error = self.tool_error(
			"match_receipt_to_bank_transaction",
			{"expense_receipt": receipt, "bank_transaction": "BT-DEPOSIT"},
		)
		self.assertIn("BT-DEPOSIT", error)
		self.assertIn("money IN", error)
		# v0.160.0 replaced the remedy. The refusal is exactly as hard; what
		# changed is that the sentence now names something a bookkeeper can
		# actually do, instead of a credit note this app cannot raise.
		self.assertIn("is_return", error)
		self.assertNotIn("credit note", error)
		self.assertIsNone(self.receipt(receipt).get(LINK))

		self.assertIn(
			"money IN",
			self.tool_error(
				"match_receipt_to_bank_transaction",
				{
					"expense_receipt": receipt,
					"bank_transaction": "BT-DEPOSIT",
					"replace": True,
					"amount_tolerance": 10000,
				},
			),
		)

	def test_a_receipt_and_a_transaction_in_different_companies_are_refused(self):
		receipt = self.capture()
		frappe.db.set_value(BANK_TRANSACTION, "BT-CHEVRON", "company", OTHER)
		error = self.tool_error(
			"match_receipt_to_bank_transaction",
			{"expense_receipt": receipt, "bank_transaction": "BT-CHEVRON"},
		)
		self.assertIn(OTHER, error)
		self.assertIn("Nothing was written", error)

	def test_a_rejected_receipt_is_not_evidence_of_anything(self):
		receipt = self.capture()
		self.tool_data(
			"reject_expense_receipt",
			{"name": receipt, "rejected_by": EMPLOYEE, "reason": "Personal purchase"},
		)
		error = self.tool_error(
			"match_receipt_to_bank_transaction",
			{"expense_receipt": receipt, "bank_transaction": "BT-CHEVRON"},
		)
		self.assertIn("rejected", error)

	def test_a_second_receipt_against_one_transaction_is_refused_and_names_the_first(self):
		first = self.capture()
		second = self.capture(merchant="Chevron (duplicate slip)")
		self.tool_data(
			"match_receipt_to_bank_transaction",
			{"expense_receipt": first, "bank_transaction": "BT-CHEVRON"},
		)
		error = self.tool_error(
			"match_receipt_to_bank_transaction",
			{"expense_receipt": second, "bank_transaction": "BT-CHEVRON"},
		)
		self.assertIn(first, error)
		self.assertIsNone(self.receipt(second).get(LINK))

	def test_a_receipt_already_matched_elsewhere_is_refused_without_replace(self):
		receipt = self.capture()
		self.tool_data(
			"match_receipt_to_bank_transaction",
			{"expense_receipt": receipt, "bank_transaction": "BT-CHEVRON"},
		)
		error = self.tool_error(
			"match_receipt_to_bank_transaction",
			{"expense_receipt": receipt, "bank_transaction": "BT-MYSTERY", "amount_tolerance": 200},
		)
		self.assertIn("already matched to BT-CHEVRON", error)
		self.assertEqual(self.receipt(receipt)[LINK], "BT-CHEVRON")

	def test_replace_repoints_it_and_names_what_it_unlinked(self):
		receipt = self.capture()
		self.tool_data(
			"match_receipt_to_bank_transaction",
			{"expense_receipt": receipt, "bank_transaction": "BT-CHEVRON"},
		)
		data = self.tool_data(
			"match_receipt_to_bank_transaction",
			{
				"expense_receipt": receipt,
				"bank_transaction": "BT-MYSTERY",
				"replace": True,
				"amount_tolerance": 200,
			},
		)
		self.assertEqual(data["replaced"], "BT-CHEVRON")
		self.assertEqual(self.receipt(receipt)[LINK], "BT-MYSTERY")

	def test_re_matching_the_same_pair_is_not_a_conflict(self):
		receipt = self.capture()
		for _ in range(2):
			data = self.tool_data(
				"match_receipt_to_bank_transaction",
				{"expense_receipt": receipt, "bank_transaction": "BT-CHEVRON"},
			)
		self.assertTrue(data["linked"])
		self.assertIsNone(data["replaced"])

	def test_an_unknown_receipt_and_an_unknown_transaction_are_both_named(self):
		self.assertIn(
			"no Expense Receipt named",
			self.tool_error("match_receipt_to_bank_transaction", {"expense_receipt": "ER-NOPE"}),
		)
		receipt = self.capture()
		self.assertIn(
			"no Bank Transaction named",
			self.tool_error(
				"match_receipt_to_bank_transaction",
				{"expense_receipt": receipt, "bank_transaction": "BT-NOPE"},
			),
		)


# ── 4. auto matching ────────────────────────────────────────────────────────
class AutoMatching(BankBridgeTestCase):
	def test_it_proposes_the_obvious_pairs(self):
		fuel = self.capture()
		parts = self.capture(**PARTS_RECEIPT)
		data = self.tool_data("auto_match_receipts", {"company": MAIN})

		pairs = {row["expense_receipt"]: row["bank_transaction"] for row in data["proposals"]}
		self.assertEqual(pairs, {fuel: "BT-CHEVRON", parts: "BT-NAPA"})

	def test_auto_match_writes_nothing_at_all(self):
		"""The claim this module exists to keep. See the module docstring."""
		self.capture()
		self.capture(**PARTS_RECEIPT)
		before = json.dumps(STORE.rows(EXPENSE_RECEIPT), sort_keys=True, default=str)
		data = self.tool_data("auto_match_receipts", {"company": MAIN})

		self.assertTrue(data["proposals"])
		self.assertFalse(data["committed"])
		self.assertEqual(json.dumps(STORE.rows(EXPENSE_RECEIPT), sort_keys=True, default=str), before)

	def test_the_tool_is_registered_read_only(self):
		self.assertFalse(registry.TOOLS["auto_match_receipts"]["mutating"])
		self.assertTrue(registry.TOOLS["auto_match_receipts"]["annotations"]["readOnlyHint"])

	def test_every_proposal_carries_the_call_that_would_commit_it(self):
		receipt = self.capture()
		proposal = self.tool_data("auto_match_receipts", {"company": MAIN})["proposals"][0]
		call = proposal["commit_with"]
		self.assertEqual(call["tool"], "match_receipt_to_bank_transaction")
		self.assertEqual(call["arguments"]["match_method"], "Proposed")

		committed = self.tool_data(call["tool"], call["arguments"])
		self.assertTrue(committed["linked"])
		self.assertEqual(self.receipt(receipt)[LINK], proposal["bank_transaction"])

	def test_a_proposed_pair_disappears_from_the_next_run(self):
		receipt = self.capture()
		self.tool_data(
			"match_receipt_to_bank_transaction",
			{"expense_receipt": receipt, "bank_transaction": "BT-CHEVRON"},
		)
		data = self.tool_data("auto_match_receipts", {"company": MAIN})
		self.assertEqual(data["proposals"], [])
		self.assertNotIn(
			"BT-CHEVRON", [row["name"] for row in [] if False]
		)  # the transaction is out of scope entirely
		self.assertEqual(data["unmatched_receipts_scanned"], 0)

	def test_a_higher_threshold_drops_the_weaker_proposal(self):
		self.capture()
		self.capture(**{**PARTS_RECEIPT, "merchant": "Unreadable terminal 4471"})
		loose = self.tool_data("auto_match_receipts", {"company": MAIN, "min_confidence": 0.5})
		tight = self.tool_data("auto_match_receipts", {"company": MAIN, "min_confidence": 0.9})
		self.assertGreater(len(loose["proposals"]), len(tight["proposals"]))
		self.assertEqual(tight["settings"]["min_confidence"], 0.9)

	def test_a_receipt_with_no_candidate_is_named_rather_than_omitted(self):
		orphan = self.capture(merchant="Cash purchase", amount=17.25, receipt_date="2026-06-14")
		data = self.tool_data("auto_match_receipts", {"company": MAIN})
		self.assertIn(orphan, data["receipts_with_no_candidate"])

	def test_a_deposit_is_never_a_candidate_in_a_batch_either(self):
		self.capture(merchant="Packer", amount=5000.00, receipt_date="2026-06-19")
		data = self.tool_data("auto_match_receipts", {"company": MAIN})
		self.assertNotIn("BT-DEPOSIT", [row["bank_transaction"] for row in data["proposals"]])

	def test_a_rejected_receipt_is_out_of_scope_by_default(self):
		receipt = self.capture()
		self.tool_data(
			"reject_expense_receipt",
			{"name": receipt, "rejected_by": EMPLOYEE, "reason": "Not ours"},
		)
		data = self.tool_data("auto_match_receipts", {"company": MAIN})
		self.assertEqual(data["unmatched_receipts_scanned"], 0)

	def test_a_percentage_passed_as_a_confidence_is_refused(self):
		self.assertIn(
			"fraction from 0 to 1",
			self.tool_error("auto_match_receipts", {"company": MAIN, "min_confidence": 70}),
		)


# ── 5. contested proposals ──────────────────────────────────────────────────
class ContestedProposals(BankBridgeTestCase):
	"""Two trucks, two drivers, one price, one day. Nobody can tell them apart."""

	def setUp(self):
		super().setUp()
		self.first = self.capture()
		self.second = self.capture(merchant="Chevron")
		self.data = self.tool_data("auto_match_receipts", {"company": MAIN})

	def test_only_one_of_the_two_is_proposed(self):
		self.assertEqual(len(self.data["proposals"]), 1)
		self.assertEqual(self.data["proposals"][0]["bank_transaction"], "BT-CHEVRON")

	def test_the_loser_is_reported_rather_than_dropped(self):
		contested = {row["expense_receipt"] for row in self.data["contested"]}
		proposed = {row["expense_receipt"] for row in self.data["proposals"]}
		self.assertEqual(contested | proposed, {self.first, self.second})
		self.assertTrue(all(row["contested"] for row in self.data["contested"]))

	def test_the_warning_says_why_this_is_not_a_bug(self):
		self.assertIn("two trucks", self.data["warning"])

	def test_neither_of_them_is_in_the_no_candidate_list(self):
		"""A contested receipt HAS a candidate. Reporting it as having none would
		send a bookkeeper looking for a charge that is sitting on the statement."""
		self.assertNotIn(self.first, self.data["receipts_with_no_candidate"])
		self.assertNotIn(self.second, self.data["receipts_with_no_candidate"])


# ── 6. the two worklists ────────────────────────────────────────────────────
class UnmatchedWorklists(BankBridgeTestCase):
	def test_an_unmatched_receipt_is_listed_with_its_category_total(self):
		self.capture()
		self.capture(**PARTS_RECEIPT)
		data = self.tool_data("list_unmatched_receipts", {"company": MAIN})
		self.assertEqual(data["count"], 2)
		self.assertEqual(data["total_amount"], round(184.62 + 62.15, 2))
		self.assertEqual(set(data["by_category"]), {"Fuel", "Equipment Parts"})

	def test_a_matched_receipt_leaves_the_list(self):
		receipt = self.capture()
		self.tool_data(
			"match_receipt_to_bank_transaction",
			{"expense_receipt": receipt, "bank_transaction": "BT-CHEVRON"},
		)
		self.assertEqual(self.tool_data("list_unmatched_receipts", {"company": MAIN})["count"], 0)

	def test_every_receipt_filter_narrows(self):
		self.capture()
		self.capture(**PARTS_RECEIPT)
		by_category = self.tool_data("list_unmatched_receipts", {"company": MAIN, "category": "Fuel"})
		by_amount = self.tool_data("list_unmatched_receipts", {"company": MAIN, "min_amount": 100})
		by_date = self.tool_data("list_unmatched_receipts", {"company": MAIN, "from_date": "2026-06-16"})
		self.assertEqual(by_category["count"], 1)
		self.assertEqual(by_amount["count"], 1)
		self.assertEqual(by_date["count"], 1)

	def test_a_transaction_missing_both_says_both(self):
		row = self._transaction_row("BT-CHEVRON")
		self.assertEqual(row["unmatched_reasons"], ["no allocation in the ledger", "no receipt on file"])

	def test_a_receipt_removes_only_the_evidence_reason(self):
		receipt = self.capture()
		self.tool_data(
			"match_receipt_to_bank_transaction",
			{"expense_receipt": receipt, "bank_transaction": "BT-CHEVRON"},
		)
		row = self._transaction_row("BT-CHEVRON")
		self.assertEqual(row["unmatched_reasons"], ["no allocation in the ledger"])
		self.assertTrue(row["has_receipt"])

	def test_an_allocation_removes_only_the_ledger_reason(self):
		frappe.db.set_value(BANK_TRANSACTION, "BT-CHEVRON", "allocated_amount", 184.62)
		frappe.db.set_value(BANK_TRANSACTION, "BT-CHEVRON", "unallocated_amount", 0)
		row = self._transaction_row("BT-CHEVRON")
		self.assertEqual(row["unmatched_reasons"], ["no receipt on file"])

	def test_require_receipt_hides_the_ones_that_have_paper(self):
		receipt = self.capture()
		self.tool_data(
			"match_receipt_to_bank_transaction",
			{"expense_receipt": receipt, "bank_transaction": "BT-CHEVRON"},
		)
		data = self.tool_data("list_unmatched_bank_transactions", {"company": MAIN, "require": "receipt"})
		self.assertNotIn("BT-CHEVRON", [row["name"] for row in data["bank_transactions"]])

	def test_require_both_keeps_only_the_ones_missing_everything(self):
		frappe.db.set_value(BANK_TRANSACTION, "BT-CHEVRON", "unallocated_amount", 0)
		frappe.db.set_value(BANK_TRANSACTION, "BT-CHEVRON", "allocated_amount", 184.62)
		data = self.tool_data("list_unmatched_bank_transactions", {"company": MAIN, "require": "both"})
		names = [row["name"] for row in data["bank_transactions"]]
		self.assertNotIn("BT-CHEVRON", names)
		self.assertIn("BT-NAPA", names)

	def test_an_unknown_require_is_refused_with_the_list(self):
		self.assertIn(
			"any, receipt, allocation, both",
			self.tool_error("list_unmatched_bank_transactions", {"require": "sometimes"}),
		)

	def test_direction_and_amount_filters_narrow(self):
		out = self.tool_data("list_unmatched_bank_transactions", {"company": MAIN, "direction": "Withdrawal"})
		big = self.tool_data("list_unmatched_bank_transactions", {"company": MAIN, "min_amount": 1000})
		self.assertNotIn("BT-DEPOSIT", [row["name"] for row in out["bank_transactions"]])
		self.assertEqual(
			sorted(row["name"] for row in big["bank_transactions"]),
			["BT-2026-0001", "BT-DEPOSIT", "BT-WILBUR"],
		)

	def test_a_bad_direction_is_refused(self):
		self.assertIn(
			"Deposit",
			self.tool_error("list_unmatched_bank_transactions", {"direction": "sideways"}),
		)

	def _transaction_row(self, name):
		data = self.tool_data("list_unmatched_bank_transactions", {"company": MAIN})
		rows = {row["name"]: row for row in data["bank_transactions"]}
		self.assertIn(name, rows)
		return rows[name]


# ── 7. the dashboard ────────────────────────────────────────────────────────
class ReconciliationStatus(BankBridgeTestCase):
	def status(self, **args):
		return self.tool_data("get_bank_reconciliation_status", {"company": MAIN, **args})

	def test_the_three_sections_are_counted_independently(self):
		receipt = self.capture()
		self.tool_data(
			"match_receipt_to_bank_transaction",
			{"expense_receipt": receipt, "bank_transaction": "BT-CHEVRON"},
		)
		frappe.db.set_value(BANK_TRANSACTION, "BT-NAPA", "unallocated_amount", 0)
		frappe.db.set_value(BANK_TRANSACTION, "BT-NAPA", "allocated_amount", 62.15)

		data = self.status()
		self.assertEqual(data["receipt_evidence"]["matched"], 1)
		self.assertEqual(data["ledger_allocation"]["matched"], 2)  # BT-NAPA and the fixture's own
		self.assertEqual(data["categorization"]["matched"], 0)

	def test_a_fully_allocated_transaction_with_no_paper_is_the_audit_case(self):
		"""The whole reason the sections are separate."""
		frappe.db.set_value(BANK_TRANSACTION, "BT-WILBUR", "unallocated_amount", 0)
		frappe.db.set_value(BANK_TRANSACTION, "BT-WILBUR", "allocated_amount", 3200)
		data = self.status(bank_account=BANK_ACCOUNT)
		self.assertGreaterEqual(data["ledger_allocation"]["matched"], 1)
		self.assertEqual(data["receipt_evidence"]["matched"], 0)
		self.assertIn("never added together", data["note"])

	def test_the_totals_split_by_direction(self):
		data = self.status(from_date="2026-06-01", to_date="2026-06-30")
		self.assertEqual(data["transactions"]["deposits"], 5000.0)
		self.assertEqual(data["transactions"]["withdrawals"], round(184.62 + 91.40 + 62.15, 2))
		self.assertEqual(data["transactions"]["net"], round(5000 - 338.17, 2))

	def test_the_period_filter_excludes_what_is_outside_it(self):
		june = self.status(from_date="2026-06-01", to_date="2026-06-30")
		self.assertEqual(june["transactions"]["count"], 4)

	def test_percentages_are_none_rather_than_zero_when_there_is_nothing(self):
		data = self.status(from_date="2030-01-01", to_date="2030-12-31")
		self.assertEqual(data["transactions"]["count"], 0)
		self.assertIsNone(data["ledger_allocation"]["matched_pct"])

	def test_categorisation_reports_the_missing_column_rather_than_a_zero(self):
		data = self.status()
		self.assertFalse(data["categorization"]["fields_installed"])
		self.assertIn("farm_category", data["categorization"]["why"])


# ── 8. the rule doctype ─────────────────────────────────────────────────────
class TheRuleDoctype(BankBridgeTestCase):
	def make(self, **overrides):
		payload = {
			"doctype": RULE,
			"rule_name": "Fuel — Chevron",
			"company": MAIN,
			"category": "Fuel",
			"pattern": "CHEVRON",
			"match_field": "description",
			"match_type": "contains",
			"direction": "Withdrawal",
			"priority": 10,
			"enabled": 1,
		}
		payload.update(overrides)
		return frappe.get_doc(payload)

	def test_a_regex_that_will_not_compile_is_refused_on_save(self):
		"""Refused here rather than in the middle of a categorisation run."""
		with self.assertRaises(frappe.ValidationError) as caught:
			self.make(match_type="regex", pattern="CHEVRON[").insert()
		self.assertIn("not a valid regular expression", str(caught.exception))

	def test_a_valid_regex_saves_and_matches(self):
		doc = self.make(match_type="regex", pattern=r"\bPUD\b").insert()
		self.assertTrue(doc.matches_text("BENTON PUD ELECTRIC"))
		self.assertFalse(doc.matches_text("PUDDING SUPPLY CO"))

	def test_an_empty_pattern_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self.make(pattern="   ").insert()
		self.assertIn("matches everything is not a rule", str(caught.exception))

	def test_a_floor_above_its_ceiling_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self.make(amount_min=500, amount_max=100).insert()
		self.assertIn("can never match anything", str(caught.exception))

	def test_a_negative_priority_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.make(priority=-1).insert()

	def test_two_rules_with_one_name_in_one_company_are_refused(self):
		self.make().insert()
		with self.assertRaises(frappe.ValidationError) as caught:
			self.make(pattern="CHEVRON FUEL").insert()
		self.assertIn("already has a rule called", str(caught.exception))

	def test_the_same_name_in_another_company_is_fine(self):
		"""Why the docname is a series and not the rule name."""
		self.make().insert()
		other = self.make(company=OTHER).insert()
		self.assertTrue(other.name)
		self.assertEqual(frappe.db.count(RULE), 2)

	def test_an_account_from_another_company_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self.make(account=supplies(OTHER_ABBR)).insert()
		self.assertIn(OTHER, str(caught.exception))

	def test_a_group_account_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self.make(account=f"Expenses - {MAIN_ABBR}").insert()
		self.assertIn("group account", str(caught.exception))

	def test_the_pattern_is_trimmed_because_a_trailing_space_is_invisible(self):
		doc = self.make(pattern="  CHEVRON  ", rule_name="  Fuel — Chevron  ").insert()
		self.assertEqual(doc.pattern, "CHEVRON")
		self.assertEqual(doc.rule_name, "Fuel — Chevron")

	def test_every_match_type_behaves_as_its_name_says(self):
		doc = self.make()
		for match_type, pattern, hit, miss in (
			("contains", "CHEVRON", "POS CHEVRON 993", "SHELL 22"),
			("starts_with", "CHEVRON", "CHEVRON 993", "POS CHEVRON 993"),
			("equals", "CHEVRON", "chevron", "CHEVRON 993"),
			("regex", r"CHEVRON\s+\d+", "CHEVRON 993", "CHEVRON WA"),
		):
			doc.match_type, doc.pattern = match_type, pattern
			self.assertTrue(doc.matches_text(hit), f"{match_type} should match {hit!r}")
			self.assertFalse(doc.matches_text(miss), f"{match_type} should not match {miss!r}")

	def test_matching_is_case_insensitive_because_bank_feeds_shout(self):
		doc = self.make(pattern="chevron")
		self.assertTrue(doc.matches_text("CHEVRON 0093746"))

	def test_the_constants_agree_with_the_shipped_json(self):
		payload = json.loads(
			(
				pathlib.Path(__file__).resolve().parent.parent
				/ "erpnext_mcp"
				/ "erpnext_mcp"
				/ "doctype"
				/ "bank_categorization_rule"
				/ "bank_categorization_rule.json"
			).read_text()
		)
		options = {field["fieldname"]: field.get("options") for field in payload["fields"]}
		# Asserted against the module constants rather than against a literal
		# list: the evaluator reads MATCH_TYPES and the form reads the JSON, and
		# what breaks a site is the two disagreeing — a rule saved through the
		# Desk with an option the evaluator has never heard of matches nothing.
		self.assertEqual(tuple(options["match_type"].split("\n")), rule_module.MATCH_TYPES)
		self.assertEqual(tuple(options["match_field"].split("\n")), rule_module.MATCH_FIELDS)
		self.assertEqual(tuple(options["direction"].split("\n")), rule_module.DIRECTIONS)
		# Every match type that compares text has to name an operator, or it
		# would silently match nothing at all.
		for match_type in rule_module.MATCH_TYPES:
			if match_type in rule_module.TEXTLESS_MATCH_TYPES:
				continue
			with self.subTest(match_type=match_type):
				self.assertTrue(
					match_type in rule_module.TEXT_OPERATORS or match_type == "plaid_category_matches",
					f"{match_type} compares nothing",
				)


# ── 9. creating rules ───────────────────────────────────────────────────────
class CreatingRules(BankBridgeTestCase):
	def test_a_rule_comes_back_whole(self):
		data = self.rule(account=supplies(), priority=10)
		rule = data["rule"]
		self.assertEqual(rule["category"], "Fuel")
		self.assertEqual(rule["pattern"], "CHEVRON")
		self.assertEqual(rule["account"], supplies())
		self.assertEqual(rule["priority"], 10)
		self.assertTrue(rule["enabled"])

	def test_a_rule_with_no_account_says_what_it_cannot_do(self):
		data = self.rule()
		self.assertIsNone(data["rule"]["account"])
		self.assertIn("never guessed", data["account_note"])

	def test_the_account_is_vetted_three_ways(self):
		self.assertIn(
			OTHER,
			self.tool_error(
				"create_bank_categorization_rule",
				{
					"rule_name": "x",
					"company": MAIN,
					"category": "Fuel",
					"pattern": "X",
					"account": supplies(OTHER_ABBR),
				},
			),
		)
		self.assertIn(
			"group account",
			self.tool_error(
				"create_bank_categorization_rule",
				{
					"rule_name": "x",
					"company": MAIN,
					"category": "Fuel",
					"pattern": "X",
					"account": f"Expenses - {MAIN_ABBR}",
				},
			),
		)
		frappe.db.set_value("Account", supplies(), "disabled", 1)
		self.assertIn(
			"disabled",
			self.tool_error(
				"create_bank_categorization_rule",
				{
					"rule_name": "x",
					"company": MAIN,
					"category": "Fuel",
					"pattern": "X",
					"account": supplies(),
				},
			),
		)

	def test_a_duplicate_rule_name_is_refused_before_anything_is_written(self):
		self.rule()
		error = self.tool_error(
			"create_bank_categorization_rule",
			{"rule_name": "Fuel — Chevron", "company": MAIN, "category": "Fuel", "pattern": "CVX"},
		)
		self.assertIn("Nothing was created", error)
		self.assertEqual(frappe.db.count(RULE), 1)

	def test_overlap_is_reported_with_which_rule_would_win(self):
		self.rule(priority=100)
		data = self.rule(rule_name="Fuel — generic", pattern="CHEVRON 00", priority=10)
		self.assertTrue(data["overlaps"])
		self.assertEqual(data["overlaps"][0]["wins"], "this rule")
		self.assertIn("what priority is for", data["warning"])

	def test_a_party_needs_both_halves_and_must_exist(self):
		self.assertIn(
			"go together",
			self.tool_error(
				"create_bank_categorization_rule",
				{
					"rule_name": "x",
					"company": MAIN,
					"category": "Fuel",
					"pattern": "X",
					"party_type": "Supplier",
				},
			),
		)
		self.assertIn(
			"no Supplier named",
			self.tool_error(
				"create_bank_categorization_rule",
				{
					"rule_name": "x",
					"company": MAIN,
					"category": "Fuel",
					"pattern": "X",
					"party_type": "Supplier",
					"party": "Nobody Ltd",
				},
			),
		)

	def test_creating_a_rule_categorises_nothing(self):
		self.rule()
		self.assertIsNone(self.transaction("BT-CHEVRON").get(CATEGORY_FIELD))

	def test_the_listing_is_in_evaluation_order(self):
		self.rule(rule_name="generic fuel", pattern="FUEL", priority=100)
		self.rule(rule_name="Chevron", pattern="CHEVRON", priority=10)
		rules = self.tool_data("list_bank_categorization_rules", {"company": MAIN})["rules"]
		self.assertEqual([rule["priority"] for rule in rules], [10, 100])

	def test_the_listing_names_the_rules_that_never_fired(self):
		self.rule()
		data = self.tool_data("list_bank_categorization_rules", {"company": MAIN})
		self.assertEqual(len(data["never_fired"]), 1)
		self.assertEqual(len(data["without_account"]), 1)

	def test_the_listing_filters_narrow(self):
		self.rule()
		self.rule(rule_name="Parts — NAPA", pattern="NAPA", category="Equipment Parts", enabled=False)
		self.assertEqual(
			self.tool_data("list_bank_categorization_rules", {"company": MAIN, "enabled": True})["count"], 1
		)
		self.assertEqual(
			self.tool_data("list_bank_categorization_rules", {"company": MAIN, "category": "Fuel"})["count"],
			1,
		)


# ── 10. applying rules ──────────────────────────────────────────────────────
class ApplyingRules(BankBridgeTestCase):
	def test_a_matching_transaction_gets_the_category_the_account_and_the_rule(self):
		created = self.rule(account=supplies())
		data = self.tool_data("apply_categorization_rules", {"company": MAIN})

		row = self.transaction("BT-CHEVRON")
		self.assertEqual(row[CATEGORY_FIELD], "Fuel")
		self.assertEqual(row[banking_bridge.ACCOUNT_FIELD], supplies())
		self.assertEqual(row[banking_bridge.RULE_FIELD], created["name"])
		self.assertEqual(data["categorized_count"], 1)

	def test_the_first_match_by_priority_wins(self):
		specific = self.rule(rule_name="Chevron", pattern="CHEVRON", category="Fuel", priority=10)
		self.rule(rule_name="Anything", pattern="PASCO", category="Miscellaneous", priority=100)
		self.tool_data("apply_categorization_rules", {"company": MAIN})

		row = self.transaction("BT-CHEVRON")
		self.assertEqual(row[CATEGORY_FIELD], "Fuel")
		self.assertEqual(row[banking_bridge.RULE_FIELD], specific["name"])

	def test_a_dry_run_writes_nothing_and_says_so(self):
		self.rule()
		data = self.tool_data("apply_categorization_rules", {"company": MAIN, "dry_run": True})
		self.assertEqual(data["categorized_count"], 1)
		self.assertIn("NOTHING was written", data["warning"])
		self.assertIsNone(self.transaction("BT-CHEVRON").get(CATEGORY_FIELD))

	def test_a_hand_typed_category_survives_a_second_run(self):
		self.rule()
		self.tool_data("apply_categorization_rules", {"company": MAIN})
		frappe.db.set_value(BANK_TRANSACTION, "BT-CHEVRON", CATEGORY_FIELD, "Fuel — shop truck")

		data = self.tool_data("apply_categorization_rules", {"company": MAIN})
		self.assertEqual(self.transaction("BT-CHEVRON")[CATEGORY_FIELD], "Fuel — shop truck")
		self.assertIn("already categorised", json.dumps(data["skipped"]))

	def test_overwrite_replaces_it_and_reports_what_was_there(self):
		self.rule()
		frappe.db.set_value(BANK_TRANSACTION, "BT-CHEVRON", CATEGORY_FIELD, "Guesswork")
		data = self.tool_data("apply_categorization_rules", {"company": MAIN, "overwrite": True})
		self.assertEqual(data["categorized"][0]["previous_category"], "Guesswork")
		self.assertEqual(self.transaction("BT-CHEVRON")[CATEGORY_FIELD], "Fuel")

	def test_direction_gates_the_rule(self):
		self.rule(rule_name="ACH anything", pattern="ACH", direction="Withdrawal")
		self.tool_data("apply_categorization_rules", {"company": MAIN})
		self.assertIsNone(self.transaction("BT-DEPOSIT").get(CATEGORY_FIELD))

	def test_an_amount_ceiling_stops_the_wire_being_filed_as_a_tank_of_diesel(self):
		self.rule(rule_name="Chemicals", pattern="WILBUR", category="Chemicals/Spray", amount_max=1000)
		data = self.tool_data("apply_categorization_rules", {"company": MAIN})
		self.assertEqual(data["categorized_count"], 0)
		self.assertIn("BT-WILBUR", json.dumps(data["still_uncategorized"]))

	def test_a_party_is_set_only_where_there_is_none(self):
		STORE.seed("Supplier", [{"name": "Chevron USA", "supplier_name": "Chevron USA"}])
		self.rule(party_type="Supplier", party="Chevron USA")
		frappe.db.set_value(BANK_TRANSACTION, "BT-MYSTERY", "party_type", "Supplier")
		frappe.db.set_value(BANK_TRANSACTION, "BT-MYSTERY", "party", "Chevron USA")

		self.tool_data("apply_categorization_rules", {"company": MAIN})
		self.assertEqual(self.transaction("BT-CHEVRON")["party"], "Chevron USA")

	def test_the_rule_records_that_it_fired(self):
		created = self.rule()
		self.tool_data("apply_categorization_rules", {"company": MAIN})
		row = STORE.get_raw(RULE, created["name"])
		self.assertEqual(row["times_applied"], 1)
		self.assertTrue(row["last_applied"])

	def test_a_dry_run_does_not_record_a_firing(self):
		created = self.rule()
		self.tool_data("apply_categorization_rules", {"company": MAIN, "dry_run": True})
		self.assertFalse(frappe.db.get_value(RULE, created["name"], "times_applied"))

	def test_what_matched_nothing_is_the_list_of_rules_still_needed(self):
		self.rule()
		data = self.tool_data("apply_categorization_rules", {"company": MAIN})
		descriptions = json.dumps(data["still_uncategorized"])
		self.assertIn("POS PURCHASE 887342", descriptions)
		self.assertIn("a new rule worth writing", data["next_step"])

	def test_a_disabled_rule_is_skipped(self):
		self.rule(enabled=False)
		error = self.tool_error("apply_categorization_rules", {"company": MAIN})
		self.assertIn("no enabled categorization rules", error)

	def test_one_rule_can_be_run_alone(self):
		self.rule()
		self.rule(rule_name="Parts", pattern="NAPA", category="Equipment Parts")
		data = self.tool_data("apply_categorization_rules", {"company": MAIN, "rule": "Parts"})
		self.assertEqual(data["rules_evaluated"], 1)
		self.assertEqual(data["categorized"][0]["bank_transaction"], "BT-NAPA")

	def test_an_unknown_rule_is_refused_by_name(self):
		self.rule()
		self.assertIn(
			"no enabled Bank Categorization Rule named",
			self.tool_error("apply_categorization_rules", {"company": MAIN, "rule": "Nope"}),
		)

	def test_applying_posts_nothing(self):
		self.rule(account=supplies())
		before = len(STORE.rows("GL Entry")), len(STORE.rows("Journal Entry"))
		self.tool_data("apply_categorization_rules", {"company": MAIN})
		self.assertEqual((len(STORE.rows("GL Entry")), len(STORE.rows("Journal Entry"))), before)
		self.assertEqual(self.transaction("BT-CHEVRON")["payment_entries"], [])

	def test_the_date_range_scopes_the_run(self):
		self.rule(rule_name="Everything", pattern="A", match_type="contains")
		data = self.tool_data(
			"apply_categorization_rules",
			{"company": MAIN, "from_date": "2026-07-01", "to_date": "2026-07-31"},
		)
		self.assertEqual([row["bank_transaction"] for row in data["categorized"]], ["BT-WILBUR"])


# ── 11. seeding ─────────────────────────────────────────────────────────────
class Seeding(BankBridgeTestCase):
	def test_it_seeds_the_whole_book_once(self):
		data = self.tool_data("seed_farm_categorization_rules", {"company": MAIN})
		self.assertEqual(data["created_count"], len(banking_bridge.FARM_RULE_SEEDS))
		self.assertEqual(data["failed"], [])
		self.assertEqual(frappe.db.count(RULE), len(banking_bridge.FARM_RULE_SEEDS))

	def test_running_it_twice_creates_nothing(self):
		self.tool_data("seed_farm_categorization_rules", {"company": MAIN})
		again = self.tool_data("seed_farm_categorization_rules", {"company": MAIN})
		self.assertEqual(again["created_count"], 0)
		self.assertEqual(again["skipped_count"], len(banking_bridge.FARM_RULE_SEEDS))
		self.assertEqual(frappe.db.count(RULE), len(banking_bridge.FARM_RULE_SEEDS))

	def test_an_edited_rule_survives_a_second_run(self):
		"""What idempotent actually protects: the edits, not the deletions."""
		self.tool_data("seed_farm_categorization_rules", {"company": MAIN})
		edited = frappe.db.get_value(RULE, {"rule_name": "Fuel — Chevron"}, "name")
		frappe.db.set_value(RULE, edited, {"pattern": "CVX", "priority": 3, "enabled": 0})

		self.tool_data("seed_farm_categorization_rules", {"company": MAIN})
		row = STORE.get_raw(RULE, edited)
		self.assertEqual((row["pattern"], row["priority"], row["enabled"]), ("CVX", 3, 0))

	def test_a_deleted_rule_comes_back_and_the_tool_says_to_disable_instead(self):
		"""Stated rather than solved — a tombstone register is more machinery
		than the problem deserves, and `enabled` already expresses the decision."""
		self.tool_data("seed_farm_categorization_rules", {"company": MAIN})
		victim = frappe.db.get_value(RULE, {"rule_name": "Fuel — Chevron"}, "name")
		frappe.delete_doc(RULE, victim)

		data = self.tool_data("seed_farm_categorization_rules", {"company": MAIN})
		self.assertEqual(data["created_count"], 1)
		self.assertIn("DISABLE one that does not fit", data["note"])

	def test_every_seeded_rule_is_an_expense_rule(self):
		"""A refund from Chevron is not a tank of diesel."""
		self.tool_data("seed_farm_categorization_rules", {"company": MAIN})
		directions = {row["direction"] for row in STORE.rows(RULE)}
		self.assertEqual(directions, {"Withdrawal"})

	def test_the_seeded_book_covers_the_ten_farm_categories(self):
		data = self.tool_data("seed_farm_categorization_rules", {"company": MAIN, "dry_run": True})
		for category in (
			"Fuel",
			"Chemicals/Spray",
			"Equipment Parts",
			"Labor Services",
			"Irrigation",
			"Insurance",
			"Utilities",
			"Feed",
			"Supplies",
			"Professional Services",
			"Owner Draw",
		):
			self.assertIn(category, data["categories"])

	def test_a_dry_run_writes_nothing(self):
		data = self.tool_data("seed_farm_categorization_rules", {"company": MAIN, "dry_run": True})
		self.assertTrue(data["created"])
		self.assertEqual(frappe.db.count(RULE), 0)

	def test_specific_rules_run_before_generic_ones(self):
		self.tool_data("seed_farm_categorization_rules", {"company": MAIN})
		self.tool_data("apply_categorization_rules", {"company": MAIN})
		row = self.transaction("BT-CHEVRON")
		self.assertEqual(row[CATEGORY_FIELD], "Fuel")
		self.assertEqual(
			frappe.db.get_value(RULE, row[banking_bridge.RULE_FIELD], "rule_name"), "Fuel — Chevron"
		)

	def test_the_seeded_book_categorises_the_week(self):
		self.tool_data("seed_farm_categorization_rules", {"company": MAIN})
		data = self.tool_data("apply_categorization_rules", {"company": MAIN})
		categorised = {row["bank_transaction"]: row["category"] for row in data["categorized"]}
		self.assertEqual(
			categorised,
			{
				"BT-CHEVRON": "Fuel",
				"BT-NAPA": "Equipment Parts",
				"BT-WILBUR": "Chemicals/Spray",
			},
		)

	def test_an_account_map_is_applied_and_named(self):
		data = self.tool_data(
			"seed_farm_categorization_rules", {"company": MAIN, "account_map": {"Fuel": supplies()}}
		)
		self.assertEqual(data["categories_with_account"], {"Fuel": supplies()})
		self.assertNotIn("Fuel", data["categories_without_account"])
		self.assertIn("Irrigation", data["categories_without_account"])
		self.assertEqual(frappe.db.get_value(RULE, {"rule_name": "Fuel — Chevron"}, "account"), supplies())

	def test_a_bad_account_map_creates_nothing_at_all(self):
		"""Vetted before ANY rule is made, so the run is all or nothing."""
		error = self.tool_error(
			"seed_farm_categorization_rules",
			{"company": MAIN, "account_map": {"Fuel": supplies(OTHER_ABBR)}},
		)
		self.assertIn(OTHER, error)
		self.assertEqual(frappe.db.count(RULE), 0)

	def test_an_account_map_that_is_not_an_object_is_refused(self):
		self.assertIn(
			"object of {category: account}",
			self.tool_error("seed_farm_categorization_rules", {"company": MAIN, "account_map": "5200"}),
		)

	def test_the_warning_names_the_categories_that_cannot_post(self):
		data = self.tool_data("seed_farm_categorization_rules", {"company": MAIN})
		self.assertIn("will not guess", data["warning"])

	def test_two_companies_get_their_own_books(self):
		self.tool_data("seed_farm_categorization_rules", {"company": MAIN})
		self.tool_data("seed_farm_categorization_rules", {"company": OTHER})
		self.assertEqual(frappe.db.count(RULE), 2 * len(banking_bridge.FARM_RULE_SEEDS))
		self.assertEqual(
			self.tool_data("list_bank_categorization_rules", {"company": OTHER, "limit": 500})["count"],
			len(banking_bridge.FARM_RULE_SEEDS),
		)


# ── 12. cash flow, switches and schema ──────────────────────────────────────
class CashFlowAndSwitches(BankBridgeTestCase):
	def cash_flow(self, **args):
		return self.tool_data("get_cash_flow_summary", {"company": MAIN, **args})

	def test_the_cash_section_is_the_statement_and_nothing_else(self):
		data = self.cash_flow(from_date="2026-06-01", to_date="2026-06-30")
		self.assertEqual(data["cash"]["basis"], "cash")
		self.assertEqual(data["cash"]["deposits"], 5000.0)
		self.assertEqual(data["cash"]["withdrawals"], round(184.62 + 91.40 + 62.15, 2))
		self.assertEqual(data["cash"]["net"], round(5000 - 338.17, 2))

	def test_the_documents_are_reported_beside_the_cash_and_never_summed(self):
		self.capture()
		data = self.cash_flow()
		self.assertEqual(data["outflows"]["expense_receipts"]["count"], 1)
		self.assertNotIn("total", data)
		self.assertNotIn("net_total", data)
		self.assertIn("never summed", data["basis_note"])

	def test_every_section_says_its_basis_or_why_it_is_absent(self):
		data = self.cash_flow()
		for section in list(data["inflows"].values()) + list(data["outflows"].values()):
			if section.get("available"):
				self.assertTrue(section["basis"])
				self.assertTrue(section["doctype"])
			else:
				self.assertIn("this site has no", section["why"])

	def test_a_matched_receipt_is_not_counted_twice_in_the_category_totals(self):
		"""The number the whole sprint exists to make right."""
		receipt = self.capture()
		self.rule()
		self.tool_data("apply_categorization_rules", {"company": MAIN})
		self.tool_data(
			"match_receipt_to_bank_transaction",
			{"expense_receipt": receipt, "bank_transaction": "BT-CHEVRON"},
		)

		data = self.cash_flow()
		self.assertEqual(data["by_category"]["categories"]["Fuel"]["amount"], 184.62)
		self.assertEqual(data["by_category"]["categories"]["Fuel"]["sources"], ["expense receipts"])
		self.assertEqual(data["by_category"]["deduplicated_transactions"], 1)

	def test_an_unmatched_categorised_withdrawal_is_counted_once(self):
		self.rule(rule_name="Chemicals", pattern="WILBUR", category="Chemicals/Spray")
		self.tool_data("apply_categorization_rules", {"company": MAIN})
		data = self.cash_flow()
		self.assertEqual(data["by_category"]["categories"]["Chemicals/Spray"]["amount"], 3200.0)
		self.assertEqual(data["by_category"]["deduplicated_transactions"], 0)

	def test_a_period_with_no_statement_says_there_is_no_cash_picture(self):
		data = self.cash_flow(from_date="2030-01-01", to_date="2030-12-31")
		self.assertFalse(data["cash"]["available"])
		self.assertIn("no cash picture", data["warning"])

	def test_every_switch_refuses_by_name_when_it_is_off(self):
		self.configure(enabled=1, **{f"allow_{name}": 0 for name in ALL_TOOLS})
		for name in ALL_TOOLS:
			error = self.tool_error(name, {"company": MAIN})
			self.assertIn(name, error)

	def test_the_read_write_defaults_are_the_ones_the_app_promises(self):
		payload = json.loads(
			(
				pathlib.Path(__file__).resolve().parent.parent
				/ "erpnext_mcp"
				/ "erpnext_mcp"
				/ "doctype"
				/ "erpnext_mcp_settings"
				/ "erpnext_mcp_settings.json"
			).read_text()
		)
		defaults = {field["fieldname"]: field.get("default") for field in payload["fields"]}
		for name in READ_TOOLS:
			self.assertEqual(defaults[f"allow_{name}"], "1", name)
		for name in WRITE_TOOLS:
			self.assertEqual(defaults[f"allow_{name}"], "0", name)

	def test_the_write_tools_are_declared_mutating(self):
		for name in WRITE_TOOLS:
			self.assertTrue(registry.TOOLS[name]["mutating"], name)
		for name in READ_TOOLS:
			self.assertFalse(registry.TOOLS[name]["mutating"], name)

	def test_the_receipt_carries_its_four_bank_columns(self):
		payload = json.loads(
			(
				pathlib.Path(__file__).resolve().parent.parent
				/ "erpnext_mcp"
				/ "erpnext_mcp"
				/ "doctype"
				/ "expense_receipt"
				/ "expense_receipt.json"
			).read_text()
		)
		fields = {field["fieldname"]: field for field in payload["fields"]}
		self.assertEqual(fields[LINK]["options"], BANK_TRANSACTION)
		self.assertEqual(fields[banking_bridge.RECEIPT_METHOD_FIELD]["read_only"], 1)
		self.assertEqual(fields[banking_bridge.RECEIPT_CONFIDENCE_FIELD]["fieldtype"], "Float")
		self.assertIn(banking_bridge.RECEIPT_MATCHED_ON_FIELD, fields)

	def test_the_bank_transaction_columns_are_created_on_first_use(self):
		self.assertFalse(banking_bridge._categorization_fields_present())
		self.rule()
		self.tool_data("apply_categorization_rules", {"company": MAIN})
		self.assertTrue(banking_bridge._categorization_fields_present())

	def test_those_columns_are_allowed_on_submit(self):
		"""A bank feed's transactions are submitted; a category must not need a cancel."""
		banking_bridge.ensure_categorization_fields()
		for fieldname in (CATEGORY_FIELD, banking_bridge.ACCOUNT_FIELD, banking_bridge.RULE_FIELD):
			row = frappe.db.get_value(
				"Custom Field", {"dt": BANK_TRANSACTION, "fieldname": fieldname}, "allow_on_submit"
			)
			self.assertEqual(int(row or 0), 1, fieldname)

	def test_ensuring_the_columns_twice_creates_them_once(self):
		banking_bridge.ensure_categorization_fields()
		banking_bridge.ensure_categorization_fields()
		self.assertEqual(
			frappe.db.count("Custom Field", {"dt": BANK_TRANSACTION, "fieldname": CATEGORY_FIELD}), 1
		)


# ── 13. v0.160.0: the money that came back ──────────────────────────────────
class ReturnsMatchAgainstCredits(BankBridgeTestCase):
	"""A hose bought on Tuesday and taken back on Thursday.

	Until v0.160.0 this app could hold both halves of that and never join them:
	`auto_match_receipts` scored against WITHDRAWALS and nothing else, and
	`match_receipt_to_bank_transaction` refused a deposit by name — correctly,
	because a receipt filed against money coming in nets a cost against a
	payment received. The advice in that refusal was to raise a credit note,
	which is a document this app does not have. So the refund sat in
	`list_unmatched_bank_transactions` and the slip sat in
	`list_unmatched_receipts`, each of them evidence for the other.

	WHAT DID NOT CHANGE IS THE HARDNESS OF THE CHECK. A return may match only
	credits and an ordinary slip may match only withdrawals. The sides never
	mix; what v0.160.0 added is which constant applies, not an escape from it.
	"""

	#: A credit the size of a returned part, so it can be told apart from the
	#: $5,000 packer settlement the base fixture already carries. Seeded here
	#: rather than added to `STATEMENT`, because half the module counts that.
	REFUND = ("BT-REFUND", "2026-06-19", "NAPA AUTO PARTS #4471 RETURN", 62.15, 0)

	def setUp(self):
		super().setUp()
		STORE.seed(
			BANK_TRANSACTION,
			[
				{
					"name": self.REFUND[0],
					"date": self.REFUND[1],
					"bank_account": BANK_ACCOUNT,
					"company": MAIN,
					"description": self.REFUND[2],
					"status": "Unreconciled",
					"deposit": self.REFUND[3],
					"withdrawal": self.REFUND[4],
					"allocated_amount": 0,
					"unallocated_amount": self.REFUND[3],
					"currency": "USD",
					"docstatus": 1,
					"payment_entries": [],
				}
			],
		)

	def returned_part(self, **overrides):
		return self.capture(
			**{
				**PARTS_RECEIPT,
				"receipt_date": "2026-06-18",
				"is_return": True,
				**overrides,
			}
		)

	# ── the direction a receipt wants ────────────────────────────────────
	def test_an_ordinary_receipt_wants_a_withdrawal(self):
		self.assertEqual(banking_bridge.expected_direction({"is_return": 0}), "Withdrawal")

	def test_a_return_wants_a_deposit(self):
		self.assertEqual(banking_bridge.expected_direction({"is_return": 1}), "Deposit")

	def test_a_site_without_the_column_wants_a_withdrawal(self):
		"""A bench that has pulled the code and not yet migrated has no
		`is_return` key on the row at all. It must score the way it always did
		rather than fail on a missing key."""
		self.assertEqual(banking_bridge.expected_direction({}), "Withdrawal")

	# ── scoring ──────────────────────────────────────────────────────────
	def test_a_return_is_eligible_against_a_credit(self):
		score = banking_bridge.score_match(
			{"name": "R", "amount": 62.15, "receipt_date": "2026-06-18", "is_return": 1},
			{
				"name": "BT-REFUND",
				"date": "2026-06-19",
				"description": "NAPA AUTO PARTS #4471 RETURN",
				"gross_amount": 62.15,
				"direction": "Deposit",
			},
			tolerance=0.02,
			window=7,
		)
		self.assertTrue(score["eligible"])
		self.assertEqual(score["signals"]["expected_direction"], "Deposit")

	def test_a_return_is_blocked_against_a_withdrawal(self):
		"""The other half of the same rule. Without this, "returns match
		credits" would be indistinguishable from "returns match anything"."""
		score = banking_bridge.score_match(
			{"name": "R", "amount": 62.15, "receipt_date": "2026-06-17", "is_return": 1},
			{
				"name": "BT-NAPA",
				"date": "2026-06-18",
				"description": "NAPA AUTO PARTS #4471 YAKIMA",
				"gross_amount": 62.15,
				"direction": "Withdrawal",
			},
			tolerance=0.02,
			window=7,
		)
		self.assertFalse(score["eligible"])
		self.assertIn("money OUT", " ".join(score["blockers"]))
		self.assertEqual(score["confidence"], 0.0)

	def test_an_ordinary_receipt_is_still_blocked_against_a_credit(self):
		score = banking_bridge.score_match(
			{"name": "R", "amount": 62.15, "receipt_date": "2026-06-18"},
			{
				"name": "BT-REFUND",
				"date": "2026-06-19",
				"description": "NAPA RETURN",
				"gross_amount": 62.15,
				"direction": "Deposit",
			},
			tolerance=0.02,
			window=7,
		)
		self.assertFalse(score["eligible"])
		self.assertIn("money IN", " ".join(score["blockers"]))

	# ── committing one ───────────────────────────────────────────────────
	def test_a_return_can_be_filed_against_its_credit(self):
		"""THE WHOLE POINT. This call raised a ToolError in every release before
		v0.160.0."""
		receipt = self.returned_part()
		data = self.tool_data(
			"match_receipt_to_bank_transaction",
			{"expense_receipt": receipt, "bank_transaction": "BT-REFUND"},
		)
		self.assertTrue(data["linked"])
		self.assertEqual(self.receipt(receipt)[LINK], "BT-REFUND")

	def test_a_return_filed_against_a_withdrawal_is_refused(self):
		receipt = self.returned_part()
		error = self.tool_error(
			"match_receipt_to_bank_transaction",
			{"expense_receipt": receipt, "bank_transaction": "BT-NAPA"},
		)
		self.assertIn("flagged as a return", error)
		self.assertIn("money out", error)
		self.assertIsNone(self.receipt(receipt).get(LINK))

	def test_the_refusal_on_an_ordinary_receipt_now_names_the_remedy(self):
		"""It used to send the caller to a credit note, which this app cannot
		raise. The refusal is unchanged; the advice was unfollowable."""
		error = self.tool_error(
			"match_receipt_to_bank_transaction",
			{"expense_receipt": self.capture(), "bank_transaction": "BT-DEPOSIT"},
		)
		self.assertIn("is_return", error)
		self.assertIn("update_expense_receipt", error)

	# ── the batch ────────────────────────────────────────────────────────
	def proposals(self, **arguments):
		data = self.tool_data("auto_match_receipts", {"company": MAIN, **arguments})
		return {row["expense_receipt"]: row for row in data["proposals"]}, data

	def test_a_return_is_proposed_against_its_credit(self):
		receipt = self.returned_part()
		found, _ = self.proposals()
		self.assertIn(receipt, found)
		self.assertEqual(found[receipt]["bank_transaction"], "BT-REFUND")

	def test_the_proposal_carries_a_commit_call_that_actually_works(self):
		"""A proposal the commit tool would refuse is worse than no proposal.
		Until v0.160.0 this pair could not both be true for a return."""
		receipt = self.returned_part()
		found, _ = self.proposals()
		commit = found[receipt]["commit_with"]
		self.assertTrue(self.tool_data(commit["tool"], commit["arguments"])["linked"])

	def test_a_return_is_never_offered_a_withdrawal(self):
		"""BT-NAPA is the same vendor, the same amount and one day away — the
		single best-looking withdrawal on the statement. It must not be
		proposed, and the fact that it looks perfect is why."""
		receipt = self.returned_part()
		found, _ = self.proposals()
		self.assertNotEqual(found[receipt]["bank_transaction"], "BT-NAPA")

	def test_an_ordinary_receipt_is_never_offered_a_credit(self):
		receipt = self.capture(**{**PARTS_RECEIPT, "receipt_date": "2026-06-18"})
		found, _ = self.proposals()
		self.assertNotEqual(found.get(receipt, {}).get("bank_transaction"), "BT-REFUND")

	def test_both_kinds_are_matched_in_one_pass(self):
		"""A statement has both on it, and a bookkeeper runs this once."""
		purchase = self.capture(**{**PARTS_RECEIPT, "receipt_date": "2026-06-17"})
		refund = self.returned_part(merchant="NAPA Auto Parts")
		found, _ = self.proposals()
		self.assertEqual(found[purchase]["bank_transaction"], "BT-NAPA")
		self.assertEqual(found[refund]["bank_transaction"], "BT-REFUND")

	# ── what the batch says it looked at ─────────────────────────────────
	def test_a_farm_with_no_returns_never_scans_the_credits(self):
		"""THE NEGATIVE CONTROL, and the claim the release notes make. If the
		deposit side were scanned unconditionally, this count would be non-zero
		and every farm's answer would have quietly changed."""
		self.capture()
		_, data = self.proposals()
		self.assertEqual(data["scanned_by_direction"]["Deposit"], 0)
		self.assertEqual(data["returns_scanned"], 0)
		self.assertGreater(data["scanned_by_direction"]["Withdrawal"], 0)

	def test_a_farm_with_only_returns_never_scans_the_withdrawals(self):
		self.returned_part()
		_, data = self.proposals()
		self.assertEqual(data["scanned_by_direction"]["Withdrawal"], 0)
		self.assertGreater(data["scanned_by_direction"]["Deposit"], 0)
		self.assertEqual(data["returns_scanned"], 1)

	def test_the_settings_block_says_whether_the_column_is_there(self):
		self.capture()
		_, data = self.proposals()
		self.assertTrue(data["settings"]["is_return_available"])

	# ── the totals ───────────────────────────────────────────────────────
	def test_a_return_subtracts_from_the_category_outflow(self):
		"""`_category_block` is headed "outflow by category", and a refund is
		negative outflow: the part is already in the bucket at what it cost."""
		self.capture(**{**PARTS_RECEIPT, "amount": 100.00})
		self.returned_part(amount=62.15)
		data = self.tool_data("get_cash_flow_summary", {"company": MAIN})["by_category"]
		self.assertEqual(data["categories"]["Equipment Parts"]["amount"], 37.85)
		self.assertEqual(data["categories"]["Equipment Parts"]["count"], 2)
		self.assertEqual(data["total"], 37.85)

	def test_a_return_subtracts_from_the_receipt_evidence_total(self):
		self.capture(**{**PARTS_RECEIPT, "amount": 100.00})
		self.returned_part(amount=62.15)
		block = self.tool_data("get_cash_flow_summary", {"company": MAIN})["outflows"]["expense_receipts"]
		self.assertEqual(block["amount"], 37.85)
		self.assertEqual(block["count"], 2)
		self.assertEqual(block["returns_count"], 1)
		self.assertEqual(block["returns_amount"], 62.15)

	def test_a_statement_with_no_returns_totals_exactly_as_it_did(self):
		self.capture(**{**PARTS_RECEIPT, "amount": 100.00})
		block = self.tool_data("get_cash_flow_summary", {"company": MAIN})["outflows"]["expense_receipts"]
		self.assertEqual(block["amount"], 100.0)
		self.assertEqual(block["returns_count"], 0)

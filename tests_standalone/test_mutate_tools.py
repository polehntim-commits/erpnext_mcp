# SPDX-License-Identifier: MIT
"""The five mutating tools, and the switches that keep them shut.

Most of these tests are about what does NOT happen.
"""

from erpnext_mcp import registry

from .fixtures import (
	ALEX,
	BANK_ACCOUNT,
	MAIN,
	OTHER,
	SeededTestCase,
	V12TestCase,
	cash,
	sales,
	supplies,
)
from .harness import STORE, post_journal_entry_gl

ALL_ON = {f"allow_{name}": 1 for name in registry.MUTATING_TOOLS}


class DefaultsAreOff(SeededTestCase):
	def test_every_mutating_tool_is_refused_out_of_the_box(self):
		"""Nothing mutates on a fresh install — and the refusal says which kind.

		A tool whose site prerequisite is missing is refused for THAT reason
		rather than for the switch, because `dispatch` checks availability first
		and the two need different words: "your operator turned this off" is a
		request to go and ask them, while "this site has no shapely" is a request
		to stop trying. Both are refusals and neither writes anything, which is
		what this test is really about.

		THE ONE EXCEPTION IS SKIPPED HERE AND ASSERTED BELOW.
		`registry.DEFAULT_ON_MUTATING_TOOLS` names it and argues for it; skipping
		it silently would let a second exception hide inside this loop.
		"""
		for name in registry.MUTATING_TOOLS:
			if name in registry.DEFAULT_ON_MUTATING_TOOLS:
				continue
			with self.subTest(tool=name):
				message = self.tool_error(name, {})
				if registry.is_available(name):
					self.assertIn(f"allow_{name}", message)
				else:
					self.assertIn("not available on this site", message)
					self.assertIn("not something an operator can switch on", message)

	def test_the_default_on_installer_runs_out_of_the_box_and_touches_no_record(self):
		"""The exception, asserted rather than assumed.

		`install_compliance_fields` ships enabled, so on a fresh install it RUNS
		rather than refusing — and the thing that makes that acceptable is that it
		writes COLUMNS and not DATA. Both halves are checked here: the call
		succeeds, and not one document of any kind was created or changed by it.
		"""
		watched = ("Journal Entry", "GL Entry", "Employee", "Housing Unit", "Field")
		before = {doctype: len(STORE.rows(doctype)) for doctype in watched}
		data = self.tool_data("install_compliance_fields", {})
		self.assertTrue(data["enabled"])
		for doctype, count in before.items():
			self.assertEqual(
				len(STORE.rows(doctype)),
				count,
				f"{doctype} rows changed — the installer is supposed to add columns, not data",
			)

	def test_the_default_on_installer_is_refused_when_an_operator_turns_it_off(self):
		"""Shipping ON is not the same as being unswitchable.

		An operator who does not want this app touching their Spray Log turns the
		switch off, and then nothing is added — through the tool OR through
		`after_migrate`, which asks the same switch.
		"""
		self.configure(allow_install_compliance_fields=0)
		message = self.tool_error("install_compliance_fields", {})
		self.assertIn("allow_install_compliance_fields", message)

	def test_a_refused_mutation_writes_nothing(self):
		before = len(STORE.rows("Journal Entry"))
		self.tool_error(
			"create_journal_entry",
			{
				"company": MAIN,
				"posting_date": "2026-03-01",
				"user_remark": "should not happen",
				"accounts": [
					{"account": cash(), "debit": 10},
					{"account": sales(), "credit": 10},
				],
			},
		)
		self.assertEqual(len(STORE.rows("Journal Entry")), before)

	def test_a_refused_mutation_is_logged_as_blocked(self):
		self.tool_error("submit_journal_entry", {"name": "ACC-JV-2026-00002"})
		row = self.assertAudited("submit_journal_entry", status="Blocked")
		self.assertIn("allow_submit_journal_entry is off", row["result_summary"])

	def test_enabling_one_does_not_enable_the_others(self):
		self.configure(enabled=1, allow_create_journal_entry=1)
		self.tool_data(
			"create_journal_entry",
			{
				"company": MAIN,
				"posting_date": "2026-03-01",
				"user_remark": "Fine",
				"accounts": [
					{"account": cash(), "debit": 10},
					{"account": sales(), "credit": 10},
				],
			},
		)
		self.assertIn(
			"allow_submit_journal_entry",
			self.tool_error("submit_journal_entry", {"name": "JE-00001"}),
		)


class CreateJournalEntry(SeededTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON)

	def entry(self, **overrides):
		payload = {
			"company": MAIN,
			"posting_date": "2026-03-01",
			"user_remark": "Reclassify supplies",
			"accounts": [
				{"account": supplies(), "debit": 100},
				{"account": cash(), "credit": 100},
			],
		}
		payload.update(overrides)
		return payload

	def test_creates_a_draft(self):
		data = self.tool_data("create_journal_entry", self.entry())
		self.assertEqual(data["docstatus"], 0)
		self.assertEqual(data["docstatus_label"], "draft")
		self.assertEqual(data["total_debit"], 100)
		self.assertEqual(data["total_credit"], 100)
		stored = STORE.get_raw("Journal Entry", data["name"])
		self.assertEqual(stored["docstatus"], 0)

	def test_says_the_draft_affects_no_balance(self):
		"""The model has to understand that it has not posted anything."""
		data = self.tool_data("create_journal_entry", self.entry())
		self.assertIn("affects no balance", data["next_step"])

	def test_unbalanced_entry_creates_nothing(self):
		before = len(STORE.rows("Journal Entry"))
		message = self.tool_error(
			"create_journal_entry",
			self.entry(
				accounts=[
					{"account": supplies(), "debit": 100},
					{"account": cash(), "credit": 90},
				]
			),
		)
		self.assertIn("do not equal credits", message)
		self.assertIn("difference 10.0", message)
		self.assertIn("Nothing was created", message)
		self.assertEqual(len(STORE.rows("Journal Entry")), before)

	def test_rounding_within_half_a_cent_is_accepted(self):
		data = self.tool_data(
			"create_journal_entry",
			self.entry(
				accounts=[
					{"account": supplies(), "debit": 33.33},
					{"account": supplies(), "debit": 33.33},
					{"account": supplies(), "debit": 33.34},
					{"account": cash(), "credit": 100},
				]
			),
		)
		self.assertEqual(data["line_count"], 4)

	def test_a_single_line_is_refused(self):
		message = self.tool_error(
			"create_journal_entry", self.entry(accounts=[{"account": cash(), "debit": 10}])
		)
		self.assertIn("at least two lines", message)

	def test_a_line_with_both_debit_and_credit_is_refused(self):
		message = self.tool_error(
			"create_journal_entry",
			self.entry(
				accounts=[
					{"account": cash(), "debit": 10, "credit": 10},
					{"account": sales(), "credit": 10},
				]
			),
		)
		self.assertIn("one or the other", message)

	def test_a_line_with_neither_is_refused(self):
		message = self.tool_error(
			"create_journal_entry",
			self.entry(accounts=[{"account": cash()}, {"account": sales()}]),
		)
		self.assertIn("neither a debit nor a credit", message)

	def test_a_negative_amount_is_refused_rather_than_silently_flipped(self):
		message = self.tool_error(
			"create_journal_entry",
			self.entry(
				accounts=[
					{"account": cash(), "debit": -10},
					{"account": sales(), "credit": -10},
				]
			),
		)
		self.assertIn("negative amount", message)

	def test_posting_to_a_group_account_is_refused(self):
		message = self.tool_error(
			"create_journal_entry",
			self.entry(
				accounts=[
					{"account": "Current Assets", "debit": 10},
					{"account": sales(), "credit": 10},
				]
			),
		)
		self.assertIn("group account", message)

	def test_an_unsupported_line_field_is_named(self):
		"""A model that sent `amount` needs to be told the field is debit or
		credit, not handed a zero-value entry."""
		message = self.tool_error(
			"create_journal_entry",
			self.entry(
				accounts=[
					{"account": cash(), "amount": 10},
					{"account": sales(), "credit": 10},
				]
			),
		)
		self.assertIn("unsupported field(s): amount", message)
		self.assertIn("debit", message)

	def test_accounts_must_be_a_list(self):
		message = self.tool_error("create_journal_entry", self.entry(accounts="cash 100"))
		self.assertIn("non-empty list", message)

	def test_user_remark_is_required(self):
		payload = self.entry()
		del payload["user_remark"]
		self.assertIn("user_remark is required", self.tool_error("create_journal_entry", payload))

	def test_company_is_required_on_a_multi_company_site(self):
		payload = self.entry()
		del payload["company"]
		self.assertIn("company is required", self.tool_error("create_journal_entry", payload))

	def test_an_account_from_another_company_is_refused(self):
		message = self.tool_error(
			"create_journal_entry",
			self.entry(
				company=OTHER,
				accounts=[
					{"account": cash(), "debit": 10},
					{"account": sales("SEL"), "credit": 10},
				],
			),
		)
		self.assertIn("belongs to company", message)

	def test_optional_line_fields_are_carried_through(self):
		data = self.tool_data(
			"create_journal_entry",
			self.entry(
				accounts=[
					{"account": supplies(), "debit": 100, "cost_center": "Main - ETC"},
					{"account": cash(), "credit": 100, "user_remark": "petty cash"},
				]
			),
		)
		stored = STORE.get_raw("Journal Entry", data["name"])
		self.assertEqual(stored["accounts"][0]["cost_center"], "Main - ETC")
		self.assertEqual(stored["accounts"][1]["user_remark"], "petty cash")

	def test_the_audit_row_records_the_docstatus_delta(self):
		self.tool_data("create_journal_entry", self.entry())
		row = self.assertAudited("create_journal_entry", status="Success")
		self.assertEqual(row["docstatus_delta"], "none → 0 (draft)")


class SubmitJournalEntry(SeededTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON)

	def test_submits_a_draft(self):
		data = self.tool_data("submit_journal_entry", {"name": "ACC-JV-2026-00002"})
		self.assertEqual(data["docstatus"], 1)
		self.assertEqual(STORE.get_raw("Journal Entry", "ACC-JV-2026-00002")["docstatus"], 1)

	def test_records_the_delta(self):
		self.tool_data("submit_journal_entry", {"name": "ACC-JV-2026-00002"})
		row = self.assertAudited("submit_journal_entry", status="Success")
		self.assertEqual(row["docstatus_delta"], "0 → 1 (submitted)")

	def test_an_already_submitted_entry_is_refused(self):
		message = self.tool_error("submit_journal_entry", {"name": "ACC-JV-2026-00001"})
		self.assertIn("already submitted", message)

	def test_a_cancelled_entry_is_refused(self):
		STORE.get_raw("Journal Entry", "ACC-JV-2026-00002")["docstatus"] = 2
		message = self.tool_error("submit_journal_entry", {"name": "ACC-JV-2026-00002"})
		self.assertIn("cancelled and cannot be submitted", message)

	def test_an_unknown_entry_is_refused(self):
		message = self.tool_error("submit_journal_entry", {"name": "ACC-JV-9999-1"})
		self.assertIn("no Journal Entry named", message)

	def test_it_cannot_be_asked_to_create_anything(self):
		"""The schema is a name and nothing else — that is the whole safety
		property of splitting create from submit."""
		schema = registry.TOOLS["submit_journal_entry"]["inputSchema"]
		self.assertEqual(list(schema["properties"]), ["name"])
		self.assertFalse(schema["additionalProperties"])


class CancelJournalEntry(SeededTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON)

	def test_cancels_a_submitted_entry(self):
		data = self.tool_data(
			"cancel_journal_entry",
			{"name": "ACC-JV-2026-00001", "reason": "Duplicate of the February entry"},
		)
		self.assertEqual(data["docstatus"], 2)
		self.assertEqual(STORE.get_raw("Journal Entry", "ACC-JV-2026-00001")["docstatus"], 2)

	def test_the_reason_lands_on_the_document_and_in_the_log(self):
		reason = "Posted to the wrong period"
		self.tool_data("cancel_journal_entry", {"name": "ACC-JV-2026-00001", "reason": reason})
		comments = [c for c in STORE.comments if c.get("name") == "ACC-JV-2026-00001"]
		self.assertTrue(any(reason in c["text"] for c in comments))
		self.assertIn(reason, self.assertAudited("cancel_journal_entry")["result_summary"])

	def test_a_reason_is_mandatory(self):
		message = self.tool_error("cancel_journal_entry", {"name": "ACC-JV-2026-00001"})
		self.assertIn("reason is required", message)

	def test_a_placeholder_reason_is_refused(self):
		message = self.tool_error("cancel_journal_entry", {"name": "ACC-JV-2026-00001", "reason": "x"})
		self.assertIn("real explanation", message)

	def test_a_draft_is_refused_with_advice(self):
		message = self.tool_error(
			"cancel_journal_entry", {"name": "ACC-JV-2026-00002", "reason": "Not wanted"}
		)
		self.assertIn("is a draft", message)
		self.assertIn("delete it in ERPNext", message)

	def test_it_is_annotated_as_destructive(self):
		self.assertTrue(registry.TOOLS["cancel_journal_entry"]["annotations"]["destructiveHint"])

	def test_nothing_is_deleted(self):
		data = self.tool_data(
			"cancel_journal_entry", {"name": "ACC-JV-2026-00001", "reason": "Wrong account"}
		)
		self.assertIn("nothing was deleted", data["note"].lower())
		self.assertIsNotNone(STORE.get_raw("Journal Entry", "ACC-JV-2026-00001"))


class CreateBankTransaction(SeededTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON)

	def test_a_positive_amount_becomes_a_deposit(self):
		data = self.tool_data(
			"create_bank_transaction",
			{
				"bank_account": BANK_ACCOUNT,
				"date": "2026-03-01",
				"amount": 500,
				"description": "Refund received",
			},
		)
		stored = STORE.get_raw("Bank Transaction", data["name"])
		self.assertEqual(stored["deposit"], 500)
		self.assertEqual(stored["withdrawal"], 0)

	def test_a_negative_amount_becomes_a_withdrawal(self):
		data = self.tool_data(
			"create_bank_transaction",
			{
				"bank_account": BANK_ACCOUNT,
				"date": "2026-03-01",
				"amount": -75.25,
				"description": "Bank fee",
			},
		)
		stored = STORE.get_raw("Bank Transaction", data["name"])
		self.assertEqual(stored["deposit"], 0)
		self.assertEqual(stored["withdrawal"], 75.25)

	def test_it_lands_as_a_draft(self):
		data = self.tool_data(
			"create_bank_transaction",
			{
				"bank_account": BANK_ACCOUNT,
				"date": "2026-03-01",
				"amount": 10,
				"description": "Interest",
			},
		)
		self.assertEqual(data["docstatus"], 0)
		self.assertIn("not reconcilable", data["next_step"])

	def test_company_and_currency_come_from_the_bank_account(self):
		data = self.tool_data(
			"create_bank_transaction",
			{
				"bank_account": "Operating",
				"date": "2026-03-01",
				"amount": 10,
				"description": "Interest",
			},
		)
		stored = STORE.get_raw("Bank Transaction", data["name"])
		self.assertEqual(stored["company"], MAIN)
		self.assertEqual(stored["currency"], "USD")

	def test_a_zero_amount_is_refused(self):
		message = self.tool_error(
			"create_bank_transaction",
			{
				"bank_account": BANK_ACCOUNT,
				"date": "2026-03-01",
				"amount": 0,
				"description": "Nothing",
			},
		)
		self.assertIn("non-zero", message)

	def test_a_non_numeric_amount_says_so(self):
		message = self.tool_error(
			"create_bank_transaction",
			{
				"bank_account": BANK_ACCOUNT,
				"date": "2026-03-01",
				"amount": "five hundred",
				"description": "Nope",
			},
		)
		self.assertIn("amount must be a number", message)


class ReconcileBankTransaction(SeededTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON)

	def voucher(self, amount=400, name="PE-0002"):
		return {
			"name": "BT-2026-0001",
			"payment_entries": [
				{
					"payment_document": "Payment Entry",
					"payment_entry": name,
					"allocated_amount": amount,
				}
			],
		}

	def test_attaches_a_voucher(self):
		data = self.tool_data("reconcile_bank_transaction", self.voucher())
		self.assertEqual(data["allocated_now"], 400)
		self.assertEqual(len(data["payment_entries"]), 1)
		stored = STORE.get_raw("Bank Transaction", "BT-2026-0001")
		self.assertEqual(stored["payment_entries"][0]["payment_entry"], "PE-0002")

	def test_over_allocation_is_refused_and_changes_nothing(self):
		message = self.tool_error("reconcile_bank_transaction", self.voucher(amount=1500))
		self.assertIn("would exceed", message)
		self.assertIn("Nothing was changed", message)
		self.assertEqual(STORE.get_raw("Bank Transaction", "BT-2026-0001")["payment_entries"], [])

	def test_allocation_against_an_already_reconciled_transaction_is_refused(self):
		message = self.tool_error(
			"reconcile_bank_transaction",
			{
				"name": "BT-2026-0002",
				"payment_entries": [
					{
						"payment_document": "Payment Entry",
						"payment_entry": "PE-0002",
						"allocated_amount": 1,
					}
				],
			},
		)
		self.assertIn("remaining 0.0", message)

	def test_a_voucher_that_does_not_exist_is_refused(self):
		message = self.tool_error("reconcile_bank_transaction", self.voucher(name="PE-9999"))
		self.assertIn("no Payment Entry named", message)

	def test_an_unknown_voucher_doctype_is_refused(self):
		message = self.tool_error(
			"reconcile_bank_transaction",
			{
				"name": "BT-2026-0001",
				"payment_entries": [
					{
						"payment_document": "Invented Doctype",
						"payment_entry": "X-1",
						"allocated_amount": 1,
					}
				],
			},
		)
		self.assertIn("no such DocType", message)

	def test_a_non_positive_allocation_is_refused(self):
		message = self.tool_error("reconcile_bank_transaction", self.voucher(amount=-5))
		self.assertIn("must be positive", message)

	def test_incomplete_voucher_fields_are_named(self):
		message = self.tool_error(
			"reconcile_bank_transaction",
			{"name": "BT-2026-0001", "payment_entries": [{"allocated_amount": 5}]},
		)
		self.assertIn("payment_document", message)
		self.assertIn("payment_entry", message)

	def as_erpnext_v15(self, gl=None):
		"""Patch `get_doc` so a Bank Transaction carries ERPnext 15's own methods.

		`add_payment_entries` is ERPNext 15.x's body VERBATIM — read out of
		`polehntim/erpnext-umbrel:15.1.4` — down to the two keys it indexes and
		the save it does not do. The stubs this replaced read `payment_entry` and
		`allocated_amount` and saved themselves: they described the handler's
		assumption, not ERPNext, and so passed against a call that raised
		KeyError: 'payment_doctype' on every real site.

		`allocate_payment_entries` is simplified to its arithmetic: each
		zero-allocated row takes its voucher's GL amount (`gl`, keyed by docname),
		capped at what the transaction has left, and clears it.
		"""
		from erpnext_mcp.tools import mutate

		gl = gl or {}
		original = mutate.frappe.get_doc
		test = self

		def value(row, key):
			return row.get(key) if isinstance(row, dict) else getattr(row, key, None)

		def patched(*args, **kwargs):
			doc = original(*args, **kwargs)
			if not (args and args[0] == "Bank Transaction"):
				return doc

			def gross():
				return abs(float(doc.get("withdrawal") or 0) - float(doc.get("deposit") or 0))

			def update_allocated_amount():
				allocated = sum(
					float(value(p, "allocated_amount") or 0) for p in doc.get("payment_entries") or []
				)
				doc.allocated_amount = allocated
				doc.unallocated_amount = gross() - allocated

			def add_payment_entries(vouchers):
				update_allocated_amount()
				if 0.0 >= doc.unallocated_amount:
					raise AssertionError(f"Bank Transaction {doc.name} is already fully reconciled")
				for voucher in vouchers:
					doc.append(
						"payment_entries",
						{
							"payment_document": voucher["payment_doctype"],
							"payment_entry": voucher["payment_name"],
							"allocated_amount": 0.0,  # Temporary
						},
					)

			def allocate_payment_entries():
				update_allocated_amount()
				remaining = doc.unallocated_amount
				for row in doc.get("payment_entries") or []:
					if float(value(row, "allocated_amount") or 0) != 0:
						continue
					amount = min(gl.get(value(row, "payment_entry"), remaining), remaining)
					if isinstance(row, dict):
						row["allocated_amount"] = amount
					else:
						row.allocated_amount = amount
					remaining -= amount
				doc.clearance_date = "2026-01-15"
				test.calls.append("allocate_payment_entries")

			def set_status():
				doc.status = "Reconciled" if doc.unallocated_amount <= 0 else "Unreconciled"

			doc.add_payment_entries = add_payment_entries
			doc.allocate_payment_entries = allocate_payment_entries
			doc.update_allocated_amount = update_allocated_amount
			doc.set_status = set_status
			return doc

		self.calls = []
		mutate.frappe.get_doc = patched
		self.addCleanup(setattr, mutate.frappe, "get_doc", original)

	def test_erpnexts_method_is_handed_its_own_key_names(self):
		"""THE v0.176.5 BUG. ERPNext's `add_payment_entries` indexes
		`voucher["payment_doctype"]` and `voucher["payment_name"]`; this tool's
		schema, correctly, takes the child table's `payment_document` and
		`payment_entry`. Handing one to the other raised KeyError on every site
		that has the method — which is every ERPNext 15 site."""
		self.as_erpnext_v15(gl={"PE-0002": 400})
		data = self.tool_data("reconcile_bank_transaction", self.voucher())
		self.assertEqual(data["applied_via"], "ERPNext add_payment_entries")
		self.assertEqual(data["payment_entries"][0]["payment_document"], "Payment Entry")
		self.assertEqual(data["payment_entries"][0]["payment_entry"], "PE-0002")

	def test_the_rows_erpnext_appends_are_saved_not_reloaded_away(self):
		"""`add_payment_entries` only appends. The reload that used to follow it
		would have discarded the rows even once the keys were right."""
		self.as_erpnext_v15(gl={"PE-0002": 400})
		self.tool_data("reconcile_bank_transaction", self.voucher())
		stored = STORE.get_raw("Bank Transaction", "BT-2026-0001")["payment_entries"]
		self.assertEqual([row["payment_entry"] for row in stored], ["PE-0002"])
		self.assertEqual(float(stored[0]["allocated_amount"]), 400)
		self.assertEqual(self.calls, ["allocate_payment_entries"])

	def test_the_fallback_path_is_labelled(self):
		data = self.tool_data("reconcile_bank_transaction", self.voucher())
		self.assertIn("legacy ERPNext", data["applied_via"])

	def test_payment_doctype_is_named_as_the_wrong_field(self):
		"""`payment_doctype` is the name the field has almost everywhere else in
		Frappe, so it is the one a model reaches for first; on Bank Transaction
		Payments it is `payment_document`. Being told which costs one round trip.
		Guessing would make this the only tool in the app that reads a key it was
		not given."""
		message = self.tool_error(
			"reconcile_bank_transaction",
			{
				"name": "BT-2026-0001",
				"payment_entries": [
					{
						"payment_doctype": "Payment Entry",
						"payment_entry": "PE-0002",
						"allocated_amount": 400,
					}
				],
			},
		)
		self.assertIn("the field is called payment_document", message)
		self.assertIn("'Payment Entry'", message)
		self.assertIn("Nothing was changed", message)
		self.assertEqual(STORE.get_raw("Bank Transaction", "BT-2026-0001")["payment_entries"], [])

	def test_a_journal_entry_round_trips_from_creation_to_allocation(self):
		"""The whole path an operator takes off a bank statement: draft the entry,
		post it, allocate it against the transaction that paid for it."""
		self.configure(enabled=1, **ALL_ON)
		created = self.tool_data(
			"create_journal_entry",
			{
				"company": MAIN,
				"posting_date": "2026-01-15",
				"user_remark": "Customer deposit per bank statement",
				"accounts": [
					{"account": cash(), "debit": 600},
					{"account": sales(), "credit": 600},
				],
			},
		)
		self.tool_data("submit_journal_entry", {"name": created["name"]})
		data = self.tool_data(
			"reconcile_bank_transaction",
			{
				"name": "BT-2026-0001",
				"payment_entries": [
					{
						"payment_document": "Journal Entry",
						"payment_entry": created["name"],
						"allocated_amount": 600,
					}
				],
			},
		)
		self.assertEqual(data["allocated_now"], 600)
		row = STORE.get_raw("Bank Transaction", "BT-2026-0001")["payment_entries"][0]
		self.assertEqual(row["payment_document"], "Journal Entry")
		self.assertEqual(row["payment_entry"], created["name"])
		self.assertEqual(row["allocated_amount"], 600)

	def test_erpnexts_own_method_gets_the_allocation_and_sets_the_clearance_date(self):
		"""Clearance dates and the allocated/unallocated split are ERPNext's to
		write; this asserts that the result is read back."""
		self.as_erpnext_v15(gl={"PE-0002": 400})
		data = self.tool_data("reconcile_bank_transaction", self.voucher(amount=400))
		self.assertEqual(data["allocated_total"], 400)
		self.assertEqual(data["unallocated_amount"], 600)
		self.assertEqual(data["status"], "Unreconciled")
		self.assertEqual(STORE.get_raw("Bank Transaction", "BT-2026-0001")["clearance_date"], "2026-01-15")

	def test_it_reports_what_erpnext_allocated_not_what_was_asked(self):
		"""ERPNext allocates the voucher's own GL figure, so a request for 400
		against a 250 voucher allocates 250 — and saying 400 would claim an
		allocation nobody made."""
		self.as_erpnext_v15(gl={"PE-0002": 250})
		data = self.tool_data("reconcile_bank_transaction", self.voucher(amount=400))
		self.assertEqual(data["allocated_now"], 250)
		self.assertEqual(data["allocated_requested"], 400)


class AccountCurrencyAmounts(SeededTestCase):
	"""The v0.8.0 bug: a line with only `debit` posts as zero.

	ERPNext derives `debit` FROM `debit_in_account_currency` on every validate,
	so a Journal Entry built with `debit` alone is written to the database with
	its amounts zeroed and is refused on submit — a draft that cannot be posted
	and does not say why. Four auto-generated opening-balance entries did exactly
	that on a live site. `harness.JournalEntryDocument` models the derivation, so
	these tests fail against the unfixed code rather than passing against it.
	"""

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON)

	def entry(self, **overrides):
		payload = {
			"company": MAIN,
			"posting_date": "2026-03-01",
			"user_remark": "Cash sale",
			"accounts": [
				{"account": cash(), "debit": 100},
				{"account": sales(), "credit": 100},
			],
		}
		payload.update(overrides)
		return self.tool_data("create_journal_entry", payload)

	def lines(self, name):
		return STORE.get_raw("Journal Entry", name)["accounts"]

	def test_every_line_carries_both_amount_columns(self):
		lines = self.lines(self.entry()["name"])
		self.assertEqual(lines[0]["debit_in_account_currency"], 100)
		self.assertEqual(lines[0]["credit_in_account_currency"], 0)
		self.assertEqual(lines[1]["credit_in_account_currency"], 100)
		self.assertEqual(lines[1]["debit_in_account_currency"], 0)

	def test_the_amounts_survive_the_insert(self):
		"""The bug's first symptom: a draft that reads 0.00 the moment it is saved."""
		lines = self.lines(self.entry()["name"])
		self.assertEqual([line["debit"] for line in lines], [100, 0])
		self.assertEqual([line["credit"] for line in lines], [0, 100])

	def test_the_entry_can_actually_be_submitted(self):
		"""The bug's second symptom, and the one that cost a day."""
		name = self.entry()["name"]
		data = self.tool_data("submit_journal_entry", {"name": name})
		self.assertEqual(data["docstatus"], 1)
		self.assertEqual(data["total_debit"], 100)
		self.assertEqual(data["total_credit"], 100)

	def test_an_amount_given_only_in_account_currency_is_understood(self):
		data = self.entry(
			accounts=[
				{"account": cash(), "debit_in_account_currency": 100},
				{"account": sales(), "credit_in_account_currency": 100},
			]
		)
		self.assertEqual(data["total_debit"], 100)
		self.assertEqual([line["debit"] for line in self.lines(data["name"])], [100, 0])

	def test_a_rate_of_one_copies_rather_than_divides(self):
		"""No arithmetic may put a fraction of a cent between two columns of the
		same number, so the common case copies. Asserted on the line this app
		builds rather than the stored row, because ERPNext rounds to the field's
		precision afterwards and would hide the difference."""
		from erpnext_mcp.tools import mutate

		lines = mutate.validated_journal_lines(
			[{"account": cash(), "debit": 33.335}, {"account": sales(), "credit": 33.335}], MAIN
		)
		self.assertEqual(lines[0]["debit_in_account_currency"], 33.335)
		self.assertEqual(lines[1]["credit_in_account_currency"], 33.335)

	def test_an_explicit_exchange_rate_divides_into_the_account_currency(self):
		STORE.tables["Account"][cash()]["account_currency"] = "CAD"
		data = self.entry(
			accounts=[
				{"account": cash(), "debit": 200, "exchange_rate": 2},
				{"account": sales(), "credit": 200},
			]
		)
		self.assertEqual(self.lines(data["name"])[0]["debit_in_account_currency"], 100)

	def test_a_supplied_account_currency_amount_is_not_recomputed(self):
		"""The caller knows what the invoice said and this does not."""
		STORE.tables["Account"][cash()]["account_currency"] = "CAD"
		data = self.entry(
			accounts=[
				{
					"account": cash(),
					"debit": 275,
					"debit_in_account_currency": 137.5,
					"exchange_rate": 2,
				},
				{"account": sales(), "credit": 275},
			]
		)
		line = self.lines(data["name"])[0]
		self.assertEqual(line["debit_in_account_currency"], 137.5)
		self.assertEqual(line["debit"], 275)

	def test_two_amounts_that_disagree_are_refused_rather_than_one_chosen(self):
		"""ERPNext posts the account-currency figure times the rate. An entry that
		balances on `debit` and not on that is one that balances here and not on
		the site."""
		STORE.tables["Account"][cash()]["account_currency"] = "CAD"
		message = self.tool_error(
			"create_journal_entry",
			{
				"company": MAIN,
				"posting_date": "2026-03-01",
				"user_remark": "Cash sale",
				"accounts": [
					{
						"account": cash(),
						"debit": 200,
						"debit_in_account_currency": 137.5,
						"exchange_rate": 2,
					},
					{"account": sales(), "credit": 200},
				],
			},
		)
		self.assertIn("comes to 275.0", message)
		self.assertIn("cannot both be right", message)

	def test_a_foreign_account_with_no_rate_is_refused(self):
		STORE.tables["Account"][cash()]["account_currency"] = "CAD"
		message = self.tool_error(
			"create_journal_entry",
			{
				"company": MAIN,
				"posting_date": "2026-03-01",
				"user_remark": "Cash sale",
				"accounts": [
					{"account": cash(), "debit": 100},
					{"account": sales(), "credit": 100},
				],
			},
		)
		self.assertIn("held in CAD", message)
		self.assertIn("exchange_rate", message)
		self.assertIn("Nothing was created", message)

	def test_a_zero_or_negative_rate_is_refused(self):
		message = self.tool_error(
			"create_journal_entry",
			{
				"company": MAIN,
				"posting_date": "2026-03-01",
				"user_remark": "Cash sale",
				"accounts": [
					{"account": cash(), "debit": 100, "exchange_rate": -1},
					{"account": sales(), "credit": 100},
				],
			},
		)
		self.assertIn("must be positive", message)


class BulkSubmitJournalEntries(SeededTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON)

	def draft(self, amount=10):
		return self.tool_data(
			"create_journal_entry",
			{
				"company": MAIN,
				"posting_date": "2026-03-01",
				"user_remark": f"Batch entry {amount}",
				"accounts": [
					{"account": cash(), "debit": amount},
					{"account": sales(), "credit": amount},
				],
			},
		)["name"]

	def test_it_posts_every_draft_in_the_batch(self):
		names = [self.draft(10), self.draft(20), self.draft(30)]
		data = self.tool_data("bulk_submit_journal_entries", {"names": names})
		self.assertEqual(data["total"], 3)
		self.assertEqual(data["submitted"], 3)
		self.assertEqual(data["failed"], 0)
		self.assertEqual(data["submitted_names"], names)
		for name in names:
			self.assertEqual(STORE.get_raw("Journal Entry", name)["docstatus"], 1)

	def test_an_already_submitted_entry_is_skipped_not_failed(self):
		"""A half-finished batch has to be safe to retry whole."""
		names = [self.draft(10), self.draft(20)]
		self.tool_data("submit_journal_entry", {"name": names[0]})
		data = self.tool_data("bulk_submit_journal_entries", {"names": names})
		self.assertEqual(data["submitted"], 1)
		self.assertEqual(data["skipped"], 1)
		self.assertEqual(data["failed"], 0)
		skipped = next(row for row in data["results"] if row["name"] == names[0])
		self.assertTrue(skipped["ok"])
		self.assertEqual(skipped["skipped"], "already_submitted")

	def test_one_bad_entry_does_not_abort_the_batch(self):
		names = [self.draft(10), "ACC-JV-NOPE", self.draft(20)]
		data = self.tool_data("bulk_submit_journal_entries", {"names": names})
		self.assertEqual(data["submitted"], 2)
		self.assertEqual(data["failed"], 1)
		self.assertEqual(data["failed_names"], ["ACC-JV-NOPE"])
		self.assertIn("no Journal Entry named", data["results"][1]["error"])
		self.assertEqual(STORE.get_raw("Journal Entry", names[2])["docstatus"], 1)

	def test_the_entries_that_posted_stay_posted(self):
		"""The whole point of a transaction per document."""
		good = self.draft(10)
		data = self.tool_data("bulk_submit_journal_entries", {"names": [good, "ACC-JV-NOPE"]})
		self.assertEqual(data["failed"], 1)
		self.assertEqual(STORE.get_raw("Journal Entry", good)["docstatus"], 1)

	def test_a_cancelled_entry_is_a_failure_not_a_skip(self):
		name = self.draft(10)
		self.tool_data("submit_journal_entry", {"name": name})
		self.tool_data("cancel_journal_entry", {"name": name, "reason": "keyed twice"})
		data = self.tool_data("bulk_submit_journal_entries", {"names": [name]})
		self.assertEqual(data["failed"], 1)
		self.assertIn("cancelled", data["results"][0]["error"])

	def test_it_refuses_when_submit_journal_entry_is_off(self):
		"""That switch is where posting is allowed or not; this does not go round it."""
		name = self.draft(10)
		self.configure(enabled=1, allow_bulk_submit_journal_entries=1)
		message = self.tool_error("bulk_submit_journal_entries", {"names": [name]})
		self.assertIn("allow_submit_journal_entry", message)
		self.assertIn("Nothing was submitted", message)
		self.assertEqual(STORE.get_raw("Journal Entry", name)["docstatus"], 0)

	def test_it_refuses_when_its_own_switch_is_off(self):
		name = self.draft(10)
		self.configure(enabled=1, allow_create_journal_entry=1, allow_submit_journal_entry=1)
		message = self.tool_error("bulk_submit_journal_entries", {"names": [name]})
		self.assertIn("allow_bulk_submit_journal_entries", message)

	def test_an_empty_list_is_refused(self):
		message = self.tool_error("bulk_submit_journal_entries", {"names": []})
		self.assertIn("non-empty list", message)

	def test_a_repeated_name_is_submitted_once(self):
		name = self.draft(10)
		data = self.tool_data("bulk_submit_journal_entries", {"names": [name, name]})
		self.assertEqual(data["total"], 1)
		self.assertEqual(data["submitted"], 1)

	def test_a_batch_larger_than_the_limit_is_refused_before_anything_posts(self):
		message = self.tool_error("bulk_submit_journal_entries", {"names": [f"JE-{n}" for n in range(501)]})
		self.assertIn("the limit is 500", message)
		self.assertIn("Nothing was submitted", message)

	def test_the_next_step_names_what_to_do_about_failures(self):
		data = self.tool_data("bulk_submit_journal_entries", {"names": [self.draft(10), "JE-NOPE"]})
		self.assertIn("call this again with just those names", data["next_step"])

	def test_a_clean_batch_says_nothing_is_outstanding(self):
		data = self.tool_data("bulk_submit_journal_entries", {"names": [self.draft(10)]})
		self.assertIn("Nothing is outstanding", data["next_step"])


class DeleteDraftJournalEntry(SeededTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON)

	def payload(self, name="ACC-JV-2026-00002", **overrides):
		values = {"name": name, "reason": "duplicate of ACC-JV-2026-00001, keyed twice"}
		values.update(overrides)
		return values

	def test_it_deletes_the_draft(self):
		data = self.tool_data("delete_draft_journal_entry", self.payload())
		self.assertEqual(data["deleted"]["name"], "ACC-JV-2026-00002")
		self.assertIsNone(STORE.get_raw("Journal Entry", "ACC-JV-2026-00002"))

	def test_it_reports_what_it_deleted_because_nothing_else_will(self):
		data = self.tool_data("delete_draft_journal_entry", self.payload())
		deleted = data["deleted"]
		self.assertEqual(deleted["company"], MAIN)
		self.assertEqual(deleted["posting_date"], "2026-02-10")
		self.assertEqual(deleted["line_count"], 2)
		self.assertEqual(deleted["total_debit"], 250)
		self.assertEqual([row["account"] for row in deleted["accounts"]], [supplies(), cash()])

	def test_the_audit_row_carries_the_reason_and_the_entry(self):
		self.tool_data("delete_draft_journal_entry", self.payload())
		row = self.assertAudited("delete_draft_journal_entry", status="Success")
		self.assertIn("deleted draft Journal Entry ACC-JV-2026-00002", row["result_summary"])
		self.assertIn("keyed twice", row["result_summary"])
		self.assertEqual(row["docstatus_delta"], "0 (draft) → deleted")

	def test_a_submitted_entry_is_refused_and_pointed_at_cancel(self):
		message = self.tool_error("delete_draft_journal_entry", self.payload("ACC-JV-2026-00001"))
		self.assertIn("cancel_journal_entry", message)
		self.assertIn("Nothing was deleted", message)
		self.assertIsNotNone(STORE.get_raw("Journal Entry", "ACC-JV-2026-00001"))

	def test_a_cancelled_entry_is_refused(self):
		STORE.tables["Journal Entry"]["ACC-JV-2026-00002"]["docstatus"] = 2
		message = self.tool_error("delete_draft_journal_entry", self.payload())
		self.assertIn("audit trail with a hole in it", message)
		self.assertIsNotNone(STORE.get_raw("Journal Entry", "ACC-JV-2026-00002"))

	def test_an_entry_that_does_not_exist_is_refused(self):
		message = self.tool_error("delete_draft_journal_entry", self.payload("ACC-JV-NOPE"))
		self.assertIn("no Journal Entry named", message)

	def test_a_placeholder_reason_is_refused(self):
		message = self.tool_error("delete_draft_journal_entry", self.payload(reason="x"))
		self.assertIn("real explanation", message)
		self.assertIsNotNone(STORE.get_raw("Journal Entry", "ACC-JV-2026-00002"))

	def test_a_missing_reason_is_refused(self):
		message = self.tool_error("delete_draft_journal_entry", {"name": "ACC-JV-2026-00002"})
		self.assertIn("reason is required", message)

	def test_the_switch_keeps_it_shut(self):
		self.configure(enabled=1)
		message = self.tool_error("delete_draft_journal_entry", self.payload())
		self.assertIn("allow_delete_draft_journal_entry", message)
		self.assertIsNotNone(STORE.get_raw("Journal Entry", "ACC-JV-2026-00002"))


# ── v0.13.0: update_journal_entry_party ─────────────────────────────────────
PARTY_EDIT_ON = {
	"allow_create_journal_entry": 1,
	"allow_submit_journal_entry": 1,
	"allow_cancel_journal_entry": 1,
	"allow_update_journal_entry_party": 1,
	"allow_create_family_member": 1,
	"allow_get_journal_entry": 1,
	"allow_generate_1099_prefill": 1,
	"allow_register_party_types": 1,
}


class UpdateJournalEntryParty(V12TestCase):
	"""Attribution on a submitted entry, in both places the party lives.

	Three things these tests are really about.

	IT WRITES TWICE OR IT HAS NOT WRITTEN. `tabJournal Entry Account` is what the
	voucher shows and `tabGL Entry` is what every ageing report reads. A tool that
	updated one and not the other leaves the two disagreeing with nothing to say
	which is right, which is worse than not having edited at all. The GL rows are
	found by `voucher_detail_no` — the line's own docname — so an entry with two
	lines to the same account for the same amount stays distinguishable.

	IT CANNOT MOVE A BALANCE. Account, debit, credit and date are not arguments,
	and there is a test asserting the trial balance is identical afterwards.
	That is what makes editing a submitted document defensible.

	THE REFUSALS ARE ABOUT MEANING, NOT ABOUT CAUTION. A cancelled entry is
	evidence of something that was undone. A Round Off line is a fraction of a
	cent ERPNext invented. A bank line with a party makes an ageing report claim
	somebody owes the account balance. None of those is a party's, and the last
	two have no escape hatch on purpose.
	"""

	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **PARTY_EDIT_ON)
		# The v0.12 fixture seeds ERPNext's four stock party types and NOT this
		# app's two, so that `register_party_types` has something real to do. A
		# Family attribution needs the party type to exist, so it is registered
		# here through the tool that registers it rather than by seeding a row —
		# which is also the sequence a real site goes through.
		self.tool_data("register_party_types", {})

	def a_draft(self, first_line=None, account=None):
		line = {"account": account or supplies(), "debit": 1200}
		line.update(first_line or {})
		return self.tool_data(
			"create_journal_entry",
			{
				"company": MAIN,
				"posting_date": "2026-03-01",
				"user_remark": "Apple Cash — which son is not yet established",
				"accounts": [line, {"account": cash(), "credit": 1200}],
			},
		)["name"]

	def a_posted_entry(self, **kwargs):
		"""A submitted entry WITH the GL rows a real submit would have written.

		The GL rows are seeded rather than derived because this double does not
		implement ERPNext's posting engine — but they are pointed back at the real
		child-row docnames the insert assigned, which is the part the tool depends
		on and the part a hand-written fixture would get wrong.
		"""
		name = self.a_draft(**kwargs)
		self.tool_data("submit_journal_entry", {"name": name})
		self.post_gl(name)
		return name

	def post_gl(self, name):
		"""Post the GL rows a real ERPNext submit would post.

		This used to be a hand-written seed that set
		`voucher_detail_no = <the account line's docname>`, and that one line is
		why v0.13.0 shipped a tool that matched zero GL rows on every submitted
		entry on a real site. See `harness.post_journal_entry_gl` for what ERPNext
		actually does and why the difference is load-bearing.
		"""
		return post_journal_entry_gl(name)

	def gl_rows(self, name):
		return [row for row in STORE.rows("GL Entry") if row.get("voucher_no") == name]

	def line(self, name, index=1):
		return STORE.get_raw("Journal Entry", name)["accounts"][index - 1]

	# -- the happy path ------------------------------------------------------
	def test_it_names_a_family_member_on_a_line_that_had_nobody(self):
		name = self.a_posted_entry()
		data = self.tool_data(
			"update_journal_entry_party",
			{
				"journal_entry": name,
				"line_index": 1,
				"party_type": "Family",
				"party": ALEX,
				"reason": "Apple Cash receipt establishes this was Alex's phone",
			},
		)
		self.assertTrue(data["updated"])
		self.assertEqual(data["after"], {"party_type": "Family", "party": ALEX})
		self.assertEqual(self.line(name)["party"], ALEX)

	def test_it_updates_the_gl_entry_row_as_well_as_the_voucher(self):
		"""The one that matters. Without this, every ageing report keeps saying
		what the voucher no longer says."""
		name = self.a_posted_entry()
		data = self.tool_data(
			"update_journal_entry_party",
			{
				"journal_entry": name,
				"line_index": 1,
				"party_type": "Family",
				"party": ALEX,
				"reason": "receipt establishes the attribution",
			},
		)
		self.assertEqual(data["gl_entries_updated"], 1)
		moved = [row for row in self.gl_rows(name) if row["party"] == ALEX]
		self.assertEqual(len(moved), 1)
		self.assertEqual(moved[0]["party_type"], "Family")

	def test_it_updates_only_the_line_asked_for(self):
		name = self.a_posted_entry()
		self.tool_data(
			"update_journal_entry_party",
			{
				"journal_entry": name,
				"line_index": 1,
				"party_type": "Family",
				"party": ALEX,
				"reason": "receipt establishes the attribution",
			},
		)
		self.assertIsNone(self.line(name, 2).get("party"))
		self.assertEqual([row["party"] for row in self.gl_rows(name) if row["party"]], [ALEX])

	def two_identical_lines(self):
		name = self.tool_data(
			"create_journal_entry",
			{
				"company": MAIN,
				"posting_date": "2026-03-01",
				"user_remark": "two identical transfers on one voucher",
				"accounts": [
					{"account": supplies(), "debit": 600},
					{"account": supplies(), "debit": 600},
					{"account": cash(), "credit": 1200},
				],
			},
		)["name"]
		self.tool_data("submit_journal_entry", {"name": name})
		self.post_gl(name)
		return name

	def test_two_identical_lines_are_refused_rather_than_guessed_between(self):
		"""v0.13.0 believed two identical lines produced two GL rows told apart by
		`voucher_detail_no`. They do not: a Journal Entry's GL rows carry no line
		docname, and ERPNext merges the two into one summed row. There is nothing
		in the ledger distinguishing them, so the tool says so and writes nothing
		rather than moving a party onto whichever row it saw first."""
		name = self.two_identical_lines()
		message = self.tool_error(
			"update_journal_entry_party",
			{
				"journal_entry": name,
				"line_index": 2,
				"party_type": "Family",
				"party": ALEX,
				"reason": "the second of the two was Alex's",
			},
		)
		self.assertIn("coin toss", message)
		self.assertIn("investigate_je_gl_link", message)
		self.assertIsNone(self.line(name, 1).get("party"))
		self.assertIsNone(self.line(name, 2).get("party"))
		self.assertEqual([row["party"] for row in self.gl_rows(name) if row["party"]], [])

	def test_the_ambiguous_case_can_be_forced_and_says_the_ledger_now_disagrees(self):
		"""The escape hatch, because a refusal a caller cannot get past is how a
		safety gate becomes the failure. It writes the voucher only and the result
		leads with the disagreement."""
		name = self.two_identical_lines()
		data = self.tool_data(
			"update_journal_entry_party",
			{
				"journal_entry": name,
				"line_index": 2,
				"party_type": "Family",
				"party": ALEX,
				"reason": "the second of the two was Alex's",
				"allow_unmatched_gl": True,
			},
		)
		self.assertEqual(data["gl_entries_updated"], 0)
		self.assertEqual(self.line(name, 2)["party"], ALEX)
		self.assertIn("DISAGREE", data["warning"])
		self.assertIn("coin toss", data["gl_match_problem"])
		self.assertEqual([row["party"] for row in self.gl_rows(name) if row["party"]], [])

	def test_merged_gl_rows_are_refused(self):
		"""ERPNext collapses lines sharing an account, party and cost center into
		one summed GL row. Writing a party onto it would attribute the other
		line's money to this line's party."""
		name = self.two_identical_lines()
		rows = [row for row in self.gl_rows(name) if row["account"] == supplies()]
		self.assertEqual(len(rows), 1, "the two 600 lines should have merged into one row")
		self.assertEqual(rows[0]["debit"], 1200)

	def test_a_journal_entrys_gl_rows_carry_no_line_docname(self):
		"""The fact the whole of Feature E turns on, asserted directly."""
		name = self.a_posted_entry()
		lines = {row["name"] for row in STORE.get_raw("Journal Entry", name)["accounts"]}
		for row in self.gl_rows(name):
			self.assertEqual(row["voucher_detail_no"], "")
			self.assertNotIn(row["voucher_detail_no"], lines)

	def test_it_changes_one_party_to_another(self):
		"""The case that started this: attributing Apple Cash to the right son."""
		name = self.a_posted_entry(first_line={"party_type": "Family", "party": ALEX})
		self.tool_data("create_family_member", {"family_name": "Antony Polehn", "relationship": "Son"})
		data = self.tool_data(
			"update_journal_entry_party",
			{
				"journal_entry": name,
				"line_index": 1,
				"party_type": "Family",
				"party": "Antony Polehn",
				"reason": "the receipt is Antony's, not Alex's",
			},
		)
		self.assertEqual(data["before"]["party"], ALEX)
		self.assertEqual(data["after"]["party"], "Antony Polehn")

	def test_it_clears_an_attribution_with_two_empty_strings(self):
		name = self.a_posted_entry(first_line={"party_type": "Family", "party": ALEX})
		data = self.tool_data(
			"update_journal_entry_party",
			{
				"journal_entry": name,
				"line_index": 1,
				"party_type": "",
				"party": "",
				"reason": "this line was never anybody's — the transfer was operational",
			},
		)
		self.assertEqual(data["after"], {"party_type": None, "party": None})
		self.assertIsNone(self.line(name)["party"])

	def test_it_moves_no_balance(self):
		"""The claim the whole tool rests on."""
		name = self.a_posted_entry()
		before = [(row["account"], row["debit"], row["credit"]) for row in self.gl_rows(name)]
		self.tool_data(
			"update_journal_entry_party",
			{
				"journal_entry": name,
				"line_index": 1,
				"party_type": "Family",
				"party": ALEX,
				"reason": "receipt establishes the attribution",
			},
		)
		self.assertEqual(
			[(row["account"], row["debit"], row["credit"]) for row in self.gl_rows(name)], before
		)

	def test_the_change_is_readable_through_get_journal_entry(self):
		name = self.a_posted_entry()
		self.tool_data(
			"update_journal_entry_party",
			{
				"journal_entry": name,
				"line_index": 1,
				"party_type": "Family",
				"party": ALEX,
				"reason": "receipt establishes the attribution",
			},
		)
		lines = self.tool_data("get_journal_entry", {"name": name})["accounts"]
		self.assertEqual(lines[0]["party"], ALEX)
		self.assertEqual(lines[0]["party_type"], "Family")

	# -- the trail ----------------------------------------------------------
	def test_it_writes_the_reason_onto_the_entrys_own_timeline(self):
		"""For the other reader: an accountant with the voucher open in the Desk,
		who will never see the MCP action log."""
		name = self.a_posted_entry()
		self.tool_data(
			"update_journal_entry_party",
			{
				"journal_entry": name,
				"line_index": 1,
				"party_type": "Family",
				"party": ALEX,
				"reason": "Apple Cash receipt establishes this was Alex's phone",
			},
		)
		text = STORE.comments[-1]["text"]
		self.assertIn("line 1", text)
		self.assertIn(ALEX, text)
		self.assertIn("Apple Cash receipt", text)

	def test_the_comment_docname_comes_back(self):
		name = self.a_posted_entry()
		data = self.tool_data(
			"update_journal_entry_party",
			{
				"journal_entry": name,
				"line_index": 1,
				"party_type": "Family",
				"party": ALEX,
				"reason": "receipt establishes the attribution",
			},
		)
		self.assertTrue(data["comment_added"])
		self.assertTrue(STORE.get_raw("Comment", data["comment_added"]))

	def test_the_reason_reaches_the_audit_log(self):
		name = self.a_posted_entry()
		self.tool_data(
			"update_journal_entry_party",
			{
				"journal_entry": name,
				"line_index": 1,
				"party_type": "Family",
				"party": ALEX,
				"reason": "receipt establishes the attribution",
			},
		)
		self.assertIn(
			"receipt establishes the attribution",
			self.assertAudited("update_journal_entry_party", status="Success")["result_summary"],
		)

	# -- dry run ------------------------------------------------------------
	def test_dry_run_reports_the_plan_and_writes_nothing(self):
		name = self.a_posted_entry()
		data = self.tool_data(
			"update_journal_entry_party",
			{
				"journal_entry": name,
				"line_index": 1,
				"party_type": "Family",
				"party": ALEX,
				"reason": "receipt establishes the attribution",
				"dry_run": True,
			},
		)
		self.assertTrue(data["dry_run"])
		self.assertFalse(data["updated"])
		self.assertEqual(data["gl_entries_matched"], 1)
		self.assertIsNone(self.line(name).get("party"))
		self.assertIsNone(self.gl_rows(name)[0].get("party"))

	def test_dry_run_still_refuses_what_the_real_call_would(self):
		name = self.a_posted_entry()
		message = self.tool_error(
			"update_journal_entry_party",
			{
				"journal_entry": name,
				"line_index": 99,
				"party_type": "Family",
				"party": ALEX,
				"reason": "receipt establishes the attribution",
				"dry_run": True,
			},
		)
		self.assertIn("outside Journal Entry", message)

	# -- refusals -----------------------------------------------------------
	def test_a_cancelled_entry_is_refused(self):
		"""Evidence with a hole in it. The cancelled entry and its reversing rows
		are the record that a posting was made and undone."""
		name = self.a_posted_entry()
		self.tool_data("cancel_journal_entry", {"name": name, "reason": "posted to the wrong month"})
		message = self.tool_error(
			"update_journal_entry_party",
			{
				"journal_entry": name,
				"line_index": 1,
				"party_type": "Family",
				"party": ALEX,
				"reason": "receipt establishes the attribution",
			},
		)
		self.assertIn("cancelled", message)
		self.assertIn("never happened", message)

	def test_a_line_index_past_the_end_is_refused_with_the_count(self):
		name = self.a_posted_entry()
		message = self.tool_error(
			"update_journal_entry_party",
			{
				"journal_entry": name,
				"line_index": 7,
				"party_type": "Family",
				"party": ALEX,
				"reason": "receipt establishes the attribution",
			},
		)
		self.assertIn("has 2 line(s)", message)

	def test_line_index_is_one_based(self):
		name = self.a_posted_entry()
		message = self.tool_error(
			"update_journal_entry_party",
			{
				"journal_entry": name,
				"line_index": 0,
				"party_type": "Family",
				"party": ALEX,
				"reason": "receipt establishes the attribution",
			},
		)
		self.assertIn("numbered from 1", message)

	def test_a_round_off_line_is_refused(self):
		"""A fraction of a cent ERPNext invented is not a fact about a person."""
		STORE.seed(
			"Account",
			[
				{
					"name": "5210 - Round Off - ETC",
					"account_name": "Round Off",
					"account_number": "5210",
					"parent_account": "Expenses - ETC",
					"is_group": 0,
					"root_type": "Expense",
					"account_type": "Round Off",
					"account_currency": "USD",
					"company": MAIN,
				}
			],
		)
		name = self.a_posted_entry(account="5210 - Round Off - ETC")
		message = self.tool_error(
			"update_journal_entry_party",
			{
				"journal_entry": name,
				"line_index": 1,
				"party_type": "Family",
				"party": ALEX,
				"reason": "receipt establishes the attribution",
			},
		)
		self.assertIn("rounding/write-off line", message)

	def test_the_companys_nominated_write_off_account_is_refused_too(self):
		"""A site that spells it differently is still a site whose write-off line
		nobody wrote on purpose."""
		STORE.get_raw("Company", MAIN)["write_off_account"] = supplies()
		name = self.a_posted_entry()
		message = self.tool_error(
			"update_journal_entry_party",
			{
				"journal_entry": name,
				"line_index": 1,
				"party_type": "Family",
				"party": ALEX,
				"reason": "receipt establishes the attribution",
			},
		)
		self.assertIn("rounding/write-off line", message)

	def test_a_bank_or_cash_line_is_refused_with_no_escape_hatch(self):
		"""A party on the operation's own money makes an ageing report claim they
		owe its balance. There is no argument that opens this."""
		name = self.a_posted_entry()
		message = self.tool_error(
			"update_journal_entry_party",
			{
				"journal_entry": name,
				"line_index": 2,
				"party_type": "Family",
				"party": ALEX,
				"reason": "receipt establishes the attribution",
				"allow_non_party_account": True,
			},
		)
		self.assertIn("the operation's own money", message)

	def test_a_party_type_this_site_has_not_registered_is_refused(self):
		message = self.tool_error(
			"update_journal_entry_party",
			{
				"journal_entry": self.a_posted_entry(),
				"line_index": 1,
				"party_type": "Beneficiary",
				"party": ALEX,
				"reason": "receipt establishes the attribution",
			},
		)
		self.assertIn("not a Party Type on this site", message)

	def test_a_party_that_is_not_on_the_register_is_refused(self):
		"""The Dynamic Link's own rule, enforced with words that name the register
		rather than with a framework error."""
		message = self.tool_error(
			"update_journal_entry_party",
			{
				"journal_entry": self.a_posted_entry(),
				"line_index": 1,
				"party_type": "Family",
				"party": "Never Added",
				"reason": "receipt establishes the attribution",
			},
		)
		self.assertIn("no Family called 'Never Added'", message)
		self.assertIn("list_family_members", message)

	def test_a_party_type_without_a_party_is_refused(self):
		message = self.tool_error(
			"update_journal_entry_party",
			{
				"journal_entry": self.a_posted_entry(),
				"line_index": 1,
				"party_type": "Family",
				"party": "",
				"reason": "receipt establishes the attribution",
			},
		)
		self.assertIn("Set both", message)

	def test_omitting_one_of_the_pair_entirely_is_refused(self):
		message = self.tool_error(
			"update_journal_entry_party",
			{
				"journal_entry": self.a_posted_entry(),
				"line_index": 1,
				"party_type": "Family",
				"reason": "receipt establishes the attribution",
			},
		)
		self.assertIn("must BOTH be given", message)

	def test_a_change_that_changes_nothing_is_refused(self):
		name = self.a_posted_entry(first_line={"party_type": "Family", "party": ALEX})
		message = self.tool_error(
			"update_journal_entry_party",
			{
				"journal_entry": name,
				"line_index": 1,
				"party_type": "Family",
				"party": ALEX,
				"reason": "receipt establishes the attribution",
			},
		)
		self.assertIn("Nothing to change", message)

	def test_a_placeholder_reason_is_refused(self):
		message = self.tool_error(
			"update_journal_entry_party",
			{
				"journal_entry": self.a_posted_entry(),
				"line_index": 1,
				"party_type": "Family",
				"party": ALEX,
				"reason": "fix",
			},
		)
		self.assertIn("real explanation", message)

	def test_an_unknown_entry_is_refused(self):
		self.assertIn(
			"no Journal Entry named",
			self.tool_error(
				"update_journal_entry_party",
				{
					"journal_entry": "ACC-JV-9999-99999",
					"line_index": 1,
					"party_type": "",
					"party": "",
					"reason": "receipt establishes the attribution",
				},
			),
		)

	def test_a_refused_call_writes_nothing(self):
		name = self.a_posted_entry()
		self.tool_error(
			"update_journal_entry_party",
			{
				"journal_entry": name,
				"line_index": 2,
				"party_type": "Family",
				"party": ALEX,
				"reason": "receipt establishes the attribution",
			},
		)
		self.assertIsNone(self.line(name, 2).get("party"))
		self.assertFalse([row for row in self.gl_rows(name) if row.get("party")])

	# -- the softer gate ----------------------------------------------------
	def test_an_account_type_that_does_not_normally_carry_a_party_is_refused(self):
		STORE.get_raw("Account", supplies())["account_type"] = "Expense Account"
		message = self.tool_error(
			"update_journal_entry_party",
			{
				"journal_entry": self.a_posted_entry(),
				"line_index": 1,
				"party_type": "Family",
				"party": ALEX,
				"reason": "receipt establishes the attribution",
			},
		)
		self.assertIn("allow_non_party_account=true", message)

	def test_and_goes_through_when_the_caller_says_they_meant_it(self):
		"""A refusal a caller cannot get past is how a safety gate becomes the
		failure. This one names the way through."""
		STORE.get_raw("Account", supplies())["account_type"] = "Expense Account"
		data = self.tool_data(
			"update_journal_entry_party",
			{
				"journal_entry": self.a_posted_entry(),
				"line_index": 1,
				"party_type": "Family",
				"party": ALEX,
				"reason": "the phone was bought for Alex and the expense is his",
				"allow_non_party_account": True,
			},
		)
		self.assertTrue(data["updated"])

	def test_an_untyped_expense_account_needs_no_escape_hatch(self):
		"""Which is the commonest case by far, and the one this tool was built
		for — an ordinary expense account carries no account_type at all."""
		data = self.tool_data(
			"update_journal_entry_party",
			{
				"journal_entry": self.a_posted_entry(),
				"line_index": 1,
				"party_type": "Family",
				"party": ALEX,
				"reason": "receipt establishes the attribution",
			},
		)
		self.assertTrue(data["updated"])

	# -- drafts -------------------------------------------------------------
	def test_a_draft_goes_through_the_document_instead(self):
		"""It has written no GL Entries and can still be saved, so it is saved —
		and the doctype's own validation runs."""
		name = self.a_draft()
		data = self.tool_data(
			"update_journal_entry_party",
			{
				"journal_entry": name,
				"line_index": 1,
				"party_type": "Family",
				"party": ALEX,
				"reason": "attributing before this is posted at all",
			},
		)
		self.assertEqual(data["tables"], ["Journal Entry"])
		self.assertEqual(data["gl_entries_updated"], 0)
		self.assertEqual(self.line(name)["party"], ALEX)

	# -- what it must not change --------------------------------------------
	def test_the_1099_prefill_still_excludes_a_family_attribution(self):
		"""Attributing a transfer correctly does not make it reportable. A
		transfer to a relative is not compensation for services."""
		before = self.tool_data("generate_1099_prefill", {"company": MAIN, "tax_year": 2025, "dry_run": True})
		name = self.a_posted_entry()
		self.tool_data(
			"update_journal_entry_party",
			{
				"journal_entry": name,
				"line_index": 1,
				"party_type": "Family",
				"party": ALEX,
				"reason": "receipt establishes the attribution",
			},
		)
		after = self.tool_data("generate_1099_prefill", {"company": MAIN, "tax_year": 2025, "dry_run": True})
		self.assertEqual(
			[row["recipient"] for row in after["recipients"]],
			[row["recipient"] for row in before["recipients"]],
		)
		self.assertNotIn(ALEX, [row["recipient"] for row in after["recipients"]])

	def test_the_switch_is_off_out_of_the_box(self):
		self.configure(enabled=1)
		self.assertIn(
			"allow_update_journal_entry_party",
			self.tool_error("update_journal_entry_party", {}),
		)

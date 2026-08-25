# SPDX-License-Identifier: MIT
"""The two document generators, and the filesystem rule they share.

THE KAIROTIC GATE IS THE POINT OF THE FIRST HALF. `generate_quarterly_investment_report`
has four preconditions, and there is a test for each one FAILING and a test for
each one PASSING — a gate that never fires is decoration, and a gate that always
fires is a wall. The state-driven pair is the shape Tim's principle asks for:
the trigger DOES fire when the world is ready and does NOT when it is not.

THE ARITHMETIC IS THE POINT OF THE SECOND HALF. `generate_1099_prefill` has one
rule with two halves — debits only on payables, debits minus credits elsewhere —
and the fixture has a vendor booked each way so both halves are exercised against
real account types. Every classification branch has a vendor too, including the
incorporated law firm that the obvious rule gets wrong.

THE FILESYSTEM RULE HAS ITS OWN CLASS. `output_path` can write to disk, so the
tests that matter are the refusals: an absolute path outside the site, a `..`
escape, a symlink pointing out, and an existing file that is not clobbered.
"""

import io
import os
import zipfile

import frappe

from .fixtures import (
	BRIGHT_ORCHARD,
	COOPER,
	FRIEND_REAGAN,
	MAIN,
	MAIN_ABBR,
	MARKETABLE_SECURITIES,
	MITCHELL,
	QUARTER,
	QUARTER_END,
	QUILL,
	SORREN,
	STATEMENT_DOC,
	TAX_YEAR,
	V11TestCase,
	cash,
	cost_center,
)
from .harness import SITE_ROOT, STORE, get_site_path

ALL_ON = {
	"allow_generate_quarterly_investment_report": 1,
	"allow_generate_1099_prefill": 1,
	"allow_create_related_party": 1,
	"allow_create_journal_entry": 1,
	"allow_attach_governance_document": 1,
}


class DocumentTestCase(V11TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ALL_ON)

	def report(self, **overrides):
		payload = {"company": MAIN, "quarter": QUARTER}
		payload.update(overrides)
		return self.tool_data("generate_quarterly_investment_report", payload)

	def report_error(self, **overrides):
		payload = {"company": MAIN, "quarter": QUARTER}
		payload.update(overrides)
		return self.tool_error("generate_quarterly_investment_report", payload)

	def prefill(self, **overrides):
		payload = {"company": MAIN, "tax_year": TAX_YEAR}
		payload.update(overrides)
		return self.tool_data("generate_1099_prefill", payload)

	def prefill_error(self, **overrides):
		payload = {"company": MAIN, "tax_year": TAX_YEAR}
		payload.update(overrides)
		return self.tool_error("generate_1099_prefill", payload)

	def stored(self, attachment: dict) -> bytes:
		return STORE.file_contents[attachment["name"]]


# ── quarter parsing ─────────────────────────────────────────────────────────
class QuarterArgument(DocumentTestCase):
	def test_it_reads_the_documented_form(self):
		from erpnext_mcp.tools.investment_report import parse_quarter

		quarter = parse_quarter("2026-Q2")
		self.assertEqual((quarter["start"], quarter["end"]), ("2026-04-01", "2026-06-30"))

	def test_all_four_quarters_bound_correctly(self):
		from erpnext_mcp.tools.investment_report import parse_quarter

		self.assertEqual(
			[
				(parse_quarter(f"2025-Q{n}")["start"], parse_quarter(f"2025-Q{n}")["end"])
				for n in (1, 2, 3, 4)
			],
			[
				("2025-01-01", "2025-03-31"),
				("2025-04-01", "2025-06-30"),
				("2025-07-01", "2025-09-30"),
				("2025-10-01", "2025-12-31"),
			],
		)

	def test_a_malformed_quarter_is_refused_with_the_shape_to_send(self):
		message = self.report_error(quarter="Q2 of last year")
		self.assertIn("'2026-Q2'", message)

	def test_a_fifth_quarter_is_refused(self):
		self.assertIn("'2026-Q2'", self.report_error(quarter="2026-Q5"))


# ── the kairotic gate: each precondition, failing and passing ────────────────
class QuarterNotClosed(DocumentTestCase):
	def test_a_quarter_still_running_is_refused_in_those_words(self):
		message = self.report_error(quarter="2026-Q3")
		self.assertIn("quarter not yet closed", message)
		self.assertIn("2026-09-30", message)
		self.assertIn("Nothing was created", message)

	def test_a_closed_quarter_passes_the_same_check(self):
		data = self.report(dry_run=True)
		check = next(row for row in data["preconditions"] if row["check"] == "quarter_closed")
		self.assertTrue(check["met"])


class StatementNotFiled(DocumentTestCase):
	def test_a_quarter_with_no_statement_on_file_is_refused_with_the_tool_that_files_one(self):
		STORE.tables["Governance Document"].pop(STATEMENT_DOC)
		message = self.report_error()
		self.assertIn("no Prior Statement governance document is filed", message)
		self.assertIn("attach_governance_document", message)

	def test_a_statement_outside_the_quarter_does_not_count(self):
		frappe.db.set_value("Governance Document", STATEMENT_DOC, "effective_date", "2026-03-31")
		self.assertIn("no Prior Statement", self.report_error())

	def test_the_filed_statement_is_named_in_the_report(self):
		data = self.report(dry_run=True)
		self.assertEqual(data["reconciliation"]["statement"]["name"], STATEMENT_DOC)


class ActivityNotSubmitted(DocumentTestCase):
	def a_draft(self):
		return self.tool_data(
			"create_journal_entry",
			{
				"company": MAIN,
				"posting_date": "2026-05-20",
				"user_remark": "An unposted trade",
				"accounts": [
					{"account": MARKETABLE_SECURITIES, "debit": 1000},
					{"account": cash(), "credit": 1000},
				],
			},
		)

	def test_a_draft_touching_the_portfolio_refuses_the_report_and_names_it(self):
		draft = self.a_draft()
		message = self.report_error()
		self.assertIn("still", message)
		self.assertIn("draft", message)
		self.assertIn(draft["name"], message)

	def test_a_draft_elsewhere_in_the_ledger_does_not_block_it(self):
		"""The gate is about the investment accounts, not about every draft on
		the site — a report that waited for an unrelated entry would never run."""
		self.tool_data(
			"create_journal_entry",
			{
				"company": MAIN,
				"posting_date": "2026-05-20",
				"user_remark": "Unrelated draft",
				"accounts": [
					{"account": cash(), "debit": 10},
					{"account": f"4100 - Sales - {MAIN_ABBR}", "credit": 10},
				],
			},
		)
		self.assertTrue(self.report(dry_run=True)["preconditions"][2]["met"])

	def test_a_draft_outside_the_quarter_does_not_block_it(self):
		self.tool_data(
			"create_journal_entry",
			{
				"company": MAIN,
				"posting_date": "2026-07-20",
				"user_remark": "Next quarter's draft",
				"accounts": [
					{"account": MARKETABLE_SECURITIES, "debit": 10},
					{"account": cash(), "credit": 10},
				],
			},
		)
		self.assertTrue(self.report(dry_run=True)["preconditions"][2]["met"])


class BankNotReconciled(DocumentTestCase):
	def an_unreconciled_transaction(self, date="2026-05-05"):
		STORE.seed(
			"Bank Transaction",
			[
				{
					"name": "BT-Q2-1",
					"date": date,
					"company": MAIN,
					"bank_account": "Operating - Example Bank",
					"status": "Pending",
					"deposit": 500,
					"withdrawal": 0,
					"unallocated_amount": 500,
					"docstatus": 1,
				}
			],
		)

	def test_an_unreconciled_transaction_in_the_quarter_refuses_the_report(self):
		self.an_unreconciled_transaction()
		message = self.report_error()
		self.assertIn("unreconciled", message)
		self.assertIn("BT-Q2-1", message)
		self.assertIn("reconcile_bank_transaction", message)

	def test_a_reconciled_transaction_does_not_block_it(self):
		self.an_unreconciled_transaction()
		frappe.db.set_value("Bank Transaction", "BT-Q2-1", "unallocated_amount", 0)
		frappe.db.set_value("Bank Transaction", "BT-Q2-1", "status", "Reconciled")
		data = self.report(dry_run=True)
		self.assertEqual(data["reconciliation"]["bank_transactions_outstanding"], 0)
		self.assertEqual(data["reconciliation"]["bank_transactions_checked"], 1)

	def test_an_unreconciled_transaction_outside_the_quarter_does_not_block_it(self):
		self.an_unreconciled_transaction(date="2026-07-05")
		self.assertTrue(self.report(dry_run=True)["preconditions"][3]["met"])


class EveryFailureAtOnce(DocumentTestCase):
	def test_the_refusal_lists_all_of_them_in_one_reply(self):
		"""One call should answer 'what is left', not send the caller round the
		loop once per precondition."""
		STORE.tables["Governance Document"].pop(STATEMENT_DOC)
		message = self.report_error(quarter="2026-Q3")
		self.assertIn("quarter not yet closed", message)
		self.assertIn("no Prior Statement", message)
		self.assertIn("All 2 of these are listed at once", message)

	def test_the_quarter_not_closed_reason_comes_first(self):
		STORE.tables["Governance Document"].pop(STATEMENT_DOC)
		message = self.report_error(quarter="2026-Q3")
		self.assertLess(message.index("quarter not yet closed"), message.index("no Prior Statement"))


# ── the figures ─────────────────────────────────────────────────────────────
class ReportFigures(DocumentTestCase):
	def test_assets_under_management_come_from_the_ledger(self):
		data = self.report(dry_run=True)
		self.assertEqual(data["aum"]["opening"], 1400000.0)
		self.assertEqual(data["aum"]["closing"], 1475000.0)
		self.assertEqual(data["aum"]["change"], 75000.0)
		self.assertEqual(data["aum"]["opening_as_of"], "2026-03-31")

	def test_the_investment_accounts_are_discovered_and_named(self):
		data = self.report(dry_run=True)
		self.assertEqual(data["accounts"]["investment"], [MARKETABLE_SECURITIES])
		self.assertIn("matched by name", data["accounts"]["discovery"])

	def test_naming_the_accounts_explicitly_overrides_the_match(self):
		data = self.report(dry_run=True, investment_accounts=[MARKETABLE_SECURITIES])
		self.assertIn("named by the caller", data["accounts"]["discovery"])

	def test_a_chart_with_no_investment_account_is_refused_rather_than_guessed_at(self):
		frappe.db.set_value("Account", MARKETABLE_SECURITIES, "account_name", "Sundry")
		message = self.report_error()
		self.assertIn("will not guess", message)
		self.assertIn("marketable securities", message)

	def test_the_activity_table_covers_the_quarter_only(self):
		data = self.report(dry_run=True)
		self.assertEqual(data["activity"]["count"], 2)
		self.assertEqual(data["activity"]["debit_total"], 100000.0)
		self.assertEqual(data["activity"]["credit_total"], 25000.0)

	def test_the_fee_accrual_is_on_average_assets_and_posts_nothing(self):
		data = self.report(dry_run=True)
		self.assertEqual(data["fees"]["average_aum"], 1437500.0)
		self.assertEqual(data["fees"]["manager_fee"], 3593.75)
		self.assertEqual(data["fees"]["custody_fee"], 3593.75)
		self.assertIn("accrual, not a posting", data["fees"]["note"])

	def test_fee_rates_are_arguments_with_the_agreement_as_the_default(self):
		self.assertEqual(self.report(dry_run=True)["fees"]["manager_fee_percent"], 1.0)
		data = self.report(dry_run=True, manager_fee_percent=1.25, custody_fee_percent=0.75)
		self.assertEqual(data["fees"]["manager_fee_percent"], 1.25)
		self.assertEqual(data["fees"]["combined_percent"], 2.0)
		self.assertFalse(data["fees"]["over_cap"])

	def test_fees_above_the_agreements_cap_are_flagged_not_refused(self):
		data = self.report(dry_run=True, manager_fee_percent=2.0, custody_fee_percent=1.0)
		self.assertTrue(data["fees"]["over_cap"])
		self.assertTrue(any("above the 2.0% cap" in warning for warning in data["warnings"]))

	def test_an_impossible_fee_rate_is_refused(self):
		self.assertIn("between 0 and 100", self.report_error(manager_fee_percent=150))

	def test_no_benchmark_means_no_performance_fee_rather_than_a_zero_one(self):
		"""A fee computed against an assumed benchmark of nothing overstates what
		the manager is owed."""
		data = self.report(dry_run=True)
		self.assertFalse(data["performance"]["computed"])
		self.assertIsNone(data["performance"]["performance_fee"])
		self.assertIn("not zero and they are not estimated", data["performance"]["reason"])

	def test_the_benchmark_is_quartered_and_the_excess_computed(self):
		data = self.report(dry_run=True, benchmark_rate_percent=4.25)
		performance = data["performance"]
		self.assertEqual(performance["benchmark_quarter_percent"], 1.0625)
		self.assertEqual(performance["benchmark_gain"], 14875.0)
		self.assertEqual(performance["gain_over_benchmark"], 60125.0)
		self.assertEqual(performance["performance_fee"], 12025.0)

	def test_a_high_water_mark_caps_the_fee_eligible_gain(self):
		data = self.report(dry_run=True, benchmark_rate_percent=4.25, high_water_mark=1450000)
		self.assertEqual(data["performance"]["fee_eligible_gain"], 25000.0)
		self.assertEqual(data["performance"]["performance_fee"], 5000.0)

	def test_below_the_high_water_mark_no_fee_is_earned_however_the_quarter_went(self):
		data = self.report(dry_run=True, benchmark_rate_percent=4.25, high_water_mark=1600000)
		self.assertEqual(data["performance"]["performance_fee"], 0.0)
		self.assertIn("no performance fee is earned", data["performance"]["high_water_mark_note"])

	def test_net_contributions_change_the_return(self):
		data = self.report(dry_run=True, benchmark_rate_percent=4.25, net_contributions=75000)
		self.assertEqual(data["performance"]["period_gain"], 0.0)
		self.assertEqual(data["performance"]["performance_fee"], 0.0)

	def test_omitting_net_contributions_says_so_rather_than_assuming_silently(self):
		self.assertIn("treated as zero", self.report(dry_run=True)["performance"]["note"])

	def test_cash_clearing_is_reported_clear_when_it_is(self):
		data = self.report(dry_run=True)
		self.assertTrue(data["cash_clearing"]["clear"])
		self.assertIn("nothing was in transit", data["cash_clearing"]["note"])

	def test_a_non_zero_clearing_balance_is_a_warning(self):
		STORE.seed(
			"GL Entry",
			[
				{
					"name": "GL-CLEARING",
					"account": f"1190 - Cash Clearing - {MAIN_ABBR}",
					"posting_date": "2026-06-29",
					"debit": 4000,
					"credit": 0,
					"company": MAIN,
					"is_cancelled": 0,
					"voucher_type": "Journal Entry",
					"voucher_no": "ACC-JV-CLEAR",
					"is_opening": "No",
				}
			],
		)
		data = self.report(dry_run=True)
		self.assertFalse(data["cash_clearing"]["clear"])
		self.assertTrue(any("in transit" in warning for warning in data["warnings"]))


#: A custodian snapshot that does NOT agree with the ledger, on purpose: the
#: variance is the behaviour worth testing.
SNAPSHOT = (
	{"symbol": "BRK.B", "description": "Berkshire Hathaway B", "quantity": 1200, "price": 460.5},
	{"symbol": "AAPL", "description": "Apple Inc", "market_value": 642600, "cost_basis": 500000},
)


class Holdings(DocumentTestCase):
	@property
	def SNAPSHOT(self):
		return [dict(position) for position in SNAPSHOT]

	def test_without_a_snapshot_the_ledger_is_named_as_the_only_source(self):
		data = self.report(dry_run=True)
		self.assertFalse(data["holdings"]["supplied"])
		self.assertIn("custodian's positions are not on it", data["holdings"]["note"])

	def test_market_value_is_derived_from_quantity_and_price_when_absent(self):
		data = self.report(dry_run=True, holdings=self.SNAPSHOT)
		self.assertEqual(data["holdings"]["positions"][0]["market_value"], 552600.0)

	def test_a_snapshot_agreeing_with_the_ledger_reconciles(self):
		data = self.report(
			dry_run=True,
			holdings=[{"symbol": "X", "market_value": 1475000}],
		)
		self.assertTrue(data["holdings"]["reconciles"])
		self.assertEqual(data["holdings"]["variance"], 0.0)

	def test_a_variance_is_reported_rather_than_reconciled_away(self):
		data = self.report(dry_run=True, holdings=self.SNAPSHOT)
		self.assertFalse(data["holdings"]["reconciles"])
		self.assertEqual(data["holdings"]["market_value"], 1195200.0)
		self.assertEqual(data["holdings"]["variance"], -279800.0)
		self.assertTrue(any("mark-to-market" in warning for warning in data["warnings"]))

	def test_a_malformed_snapshot_is_refused(self):
		self.assertIn("list of position objects", self.report_error(holdings="BRK.B"))
		self.assertIn("is not an object", self.report_error(holdings=["BRK.B"]))

	# ── a stated zero is a fact the custodian reported ───────────────────────
	#
	# `quantity`, `price` and `cost_basis` used to be coerced and then run through
	# `or None`, which reported a position the custodian had priced at zero as
	# though the custodian had said nothing about it. A lot sold out during the
	# quarter, an option that expired worthless and a holding written down to
	# nothing are all real, and all of them arrive as zeroes. The tests below are
	# the only thing that can see this: the tool succeeds either way and returns a
	# payload that reads perfectly well, so nothing else in the suite would notice.

	def test_a_quantity_stated_as_zero_is_reported_as_zero_and_not_as_null(self):
		data = self.report(
			dry_run=True,
			holdings=[{"symbol": "CLOSED", "quantity": 0, "price": 12.5, "market_value": 0}],
		)
		position = data["holdings"]["positions"][0]
		self.assertEqual(position["quantity"], 0.0)
		self.assertIsNotNone(position["quantity"])

	def test_a_quantity_that_was_never_stated_is_still_null(self):
		"""The negative control. Without it the test above passes on a fix that
		simply stopped answering None, which would lose the other half of the
		distinction rather than restore it."""
		data = self.report(
			dry_run=True,
			holdings=[{"symbol": "AAPL", "market_value": 642600}],
		)
		self.assertIsNone(data["holdings"]["positions"][0]["quantity"])

	def test_a_price_and_a_cost_basis_stated_as_zero_survive_too(self):
		data = self.report(
			dry_run=True,
			holdings=[{"symbol": "GIFTED", "quantity": 100, "price": 0, "cost_basis": 0}],
		)
		position = data["holdings"]["positions"][0]
		self.assertEqual(position["price"], 0.0)
		self.assertEqual(position["cost_basis"], 0.0)

	def test_a_price_and_a_cost_basis_never_stated_are_still_null(self):
		data = self.report(
			dry_run=True,
			holdings=[{"symbol": "BRK.B", "quantity": 1200, "market_value": 552600}],
		)
		position = data["holdings"]["positions"][0]
		self.assertIsNone(position["price"])
		self.assertIsNone(position["cost_basis"])

	def test_a_zero_quantity_still_derives_a_zero_market_value_from_the_price(self):
		"""The derivation reads the same raw values, so keeping the zero must not
		have moved which positions get a computed market value."""
		data = self.report(
			dry_run=True,
			holdings=[{"symbol": "CLOSED", "quantity": 0, "price": 12.5}],
		)
		self.assertEqual(data["holdings"]["positions"][0]["market_value"], 0.0)

	def test_the_printed_page_shows_the_zero_rather_than_an_empty_cell(self):
		"""The payload is not where a reader meets this number. The holdings table
		carried a truthiness gate of its own, so a fix that stopped at the payload
		would still have printed a blank where the custodian wrote nought."""
		data = self.report(
			holdings=[{"symbol": "CLOSED", "quantity": 0, "price": 0, "cost_basis": 0}],
		)
		payload = self.stored(data["document"])
		self.assertIn(b"0.0000", payload)


# ── what the report produces ────────────────────────────────────────────────
class ReportOutput(DocumentTestCase):
	def test_a_dry_run_writes_nothing(self):
		before = len(STORE.rows("Governance Document"))
		data = self.report(dry_run=True)
		self.assertTrue(data["dry_run"])
		self.assertEqual(len(STORE.rows("Governance Document")), before)
		self.assertNotIn("document", data)

	def test_it_files_a_prior_statement_with_the_pdf_attached(self):
		data = self.report()
		archive = frappe.get_doc("Governance Document", data["governance_document"])
		self.assertEqual(archive.category, "Prior Statement")
		self.assertEqual(str(archive.effective_date), QUARTER_END)
		self.assertTrue(data["document"]["file_name"].endswith(".pdf"))
		self.assertTrue(data["document"]["is_private"])

	def test_the_attached_bytes_really_are_a_pdf_carrying_the_figures(self):
		data = self.report()
		payload = self.stored(data["document"])
		self.assertTrue(payload.startswith(b"%PDF-1.4"))
		self.assertIn(b"1,475,000.00", payload)
		self.assertIn(b"QUARTERLY INVESTMENT REPORT", payload)

	def test_the_pdf_carries_the_preconditions_it_was_produced_under(self):
		"""A reader six months from now should not have to take on trust that
		they were checked."""
		payload = self.stored(self.report()["document"])
		self.assertIn(b"quarter_closed", payload)
		self.assertIn(b"statement_filed", payload)
		self.assertIn(b"bank_reconciled", payload)

	def test_docx_is_available_and_is_not_the_default(self):
		self.assertTrue(self.report()["document"]["file_name"].endswith(".pdf"))
		data = self.report(title="Q2 editable", output_format="docx")
		self.assertTrue(data["document"]["file_name"].endswith(".docx"))
		payload = self.stored(data["document"])
		body = zipfile.ZipFile(io.BytesIO(payload)).read("word/document.xml").decode()
		self.assertIn("1,475,000.00", body)

	def test_an_unknown_output_format_is_refused_and_says_why_pdf(self):
		message = self.report_error(output_format="rtf")
		self.assertIn("'pdf' or 'docx'", message)
		self.assertIn("may not be able to open", message)

	def test_reporting_the_same_quarter_twice_is_refused_naming_the_first(self):
		first = self.report()
		message = self.report_error()
		self.assertIn(first["governance_document"], message)
		self.assertIn("report of record", message)

	def test_a_title_distinguishes_a_re_run(self):
		self.report()
		second = self.report(title="Quarterly Investment Report 2026-Q2 — restated")
		self.assertNotEqual(second["governance_document"], "")

	def test_the_fee_is_an_accrual_and_the_next_step_says_who_books_it(self):
		data = self.report()
		self.assertIn("ACCRUAL", data["next_step"])
		self.assertIn("create_journal_entry", data["next_step"])

	def test_it_posts_nothing_to_the_ledger(self):
		before = len(STORE.rows("GL Entry"))
		self.report()
		self.assertEqual(len(STORE.rows("GL Entry")), before)

	def test_the_result_carries_the_audit_row_id(self):
		self.assertIsNotNone(self.report()["mcp_action_log_id"])


# ── 1099: the arithmetic ────────────────────────────────────────────────────
class PrefillArithmetic(DocumentTestCase):
	def totals(self, data):
		return {row["recipient"]: row["total_payments"] for row in data["recipients"]}

	def test_a_vendor_booked_straight_from_expense_sums_its_debits(self):
		self.assertEqual(self.totals(self.prefill(dry_run=True))[SORREN], 24360.0)

	def test_a_refund_reduces_the_total_on_a_non_payable_account(self):
		self.assertEqual(self.totals(self.prefill(dry_run=True))[MITCHELL], 1250.0)

	def test_only_debits_count_on_a_payable_account(self):
		"""A debit to payables is a bill being paid; a credit is one being raised.
		Netting them would report zero for a vendor who was paid in full."""
		data = self.prefill(dry_run=True)
		self.assertEqual(self.totals(data)[COOPER], 3200.0)
		row = next(item for item in data["recipients"] if item["recipient"] == COOPER)
		account = row["by_account"][f"2110 - Accounts Payable - {MAIN_ABBR}"]
		self.assertTrue(account["payable"])
		self.assertEqual((account["debit"], account["credit"], account["counted"]), (3200.0, 3200.0, 3200.0))

	def test_an_opening_entry_is_excluded_and_counted(self):
		data = self.prefill(dry_run=True)
		self.assertEqual(data["excluded"]["opening_entries"], 1)
		self.assertEqual(self.totals(data)[SORREN], 24360.0)

	def test_a_cancelled_voucher_never_reaches_the_total(self):
		self.assertNotIn(888.0, list(self.totals(self.prefill(dry_run=True)).values()))

	def test_the_next_years_payments_are_not_in_this_year(self):
		self.assertEqual(self.totals(self.prefill(dry_run=True))[SORREN], 24360.0)

	def test_employee_payments_are_excluded_and_reported_rather_than_silent(self):
		data = self.prefill(dry_run=True)
		self.assertEqual(data["excluded"]["employee_party_postings"], 1)
		self.assertEqual(data["excluded"]["employee_party_total"], 45000.0)
		self.assertIn("W-2 territory", data["excluded"]["note"])

	def test_the_cost_centre_breakdown_adds_up_to_the_total(self):
		row = next(item for item in self.prefill(dry_run=True)["recipients"] if item["recipient"] == SORREN)
		self.assertEqual(sum(row["by_cost_center"].values()), row["total_payments"])
		self.assertEqual(row["by_cost_center"][cost_center("Operations")], 12180.0)

	def test_first_and_last_payment_dates_bound_the_activity(self):
		row = next(item for item in self.prefill(dry_run=True)["recipients"] if item["recipient"] == SORREN)
		self.assertEqual((row["first_payment"], row["last_payment"]), ("2025-03-31", "2025-12-31"))
		self.assertEqual(row["voucher_count"], 4)


class PrefillClassification(DocumentTestCase):
	def verdicts(self, **overrides):
		data = self.prefill(dry_run=True, **overrides)
		out = {row["recipient"]: row["classification"] for row in data["recipients"]}
		out.update({row["recipient"]: row["classification"] for row in data["exempt_above_threshold"]})
		return out

	def test_an_individual_supplier_is_reportable(self):
		self.assertEqual(self.verdicts()[MITCHELL], "reportable")

	def test_an_llc_is_borderline_because_only_the_w9_settles_it(self):
		self.assertEqual(self.verdicts()[SORREN], "borderline")

	def test_a_corporation_is_exempt(self):
		self.assertEqual(self.verdicts()[BRIGHT_ORCHARD], "exempt")

	def test_an_incorporated_law_firm_is_borderline_not_exempt(self):
		"""'Ends in PC, skip it' is the wrong rule: attorneys are reportable even
		when incorporated, and Friend & Reagan PC is exactly the case it fails."""
		self.assertEqual(self.verdicts()[FRIEND_REAGAN], "borderline")
		row = next(
			item for item in self.prefill(dry_run=True)["recipients"] if item["recipient"] == FRIEND_REAGAN
		)
		self.assertIn("EVEN IF the firm is incorporated", row["reason"])

	def test_a_vendor_with_nothing_recorded_is_borderline_with_the_remedy(self):
		row = next(item for item in self.prefill(dry_run=True)["recipients"] if item["recipient"] == COOPER)
		self.assertEqual(row["classification"], "borderline")
		self.assertIn("Register it, or read the W-9", row["reason"])

	def test_the_related_party_register_settles_a_borderline_case(self):
		"""This is why the two features shipped together."""
		self.tool_data(
			"create_related_party",
			{
				"company": MAIN,
				"party_name": COOPER,
				"party_type": "Partnership",
				"relationship_to_company": "Vendor",
				"effective_date": "2020-01-01",
				"supplier": COOPER,
				"tax_id_type": "EIN",
				"tax_id_last4": "4411",
				"address": "3555 Upper Three Mile Rd, The Dalles OR 97058",
			},
		)
		row = next(item for item in self.prefill(dry_run=True)["recipients"] if item["recipient"] == COOPER)
		self.assertEqual(row["classification"], "reportable")
		self.assertEqual(row["tin_type"], "EIN")
		self.assertEqual(row["tin_last4"], "4411")
		self.assertTrue(row["address_on_file"])

	def test_a_registered_individual_beats_a_name_that_looks_legal(self):
		"""What the register says about an entity beats what its name looks like.
		Somebody called `Law` is not a firm, and a registered Individual is
		reportable either way."""
		from erpnext_mcp.tools.tax import _verdict

		verdict, reason = _verdict("Michael Law", {}, {"party_type": "Individual"})
		self.assertEqual(verdict, "reportable")
		self.assertIn("Individual", reason)

	def test_a_registered_corporation_that_is_a_law_firm_is_still_borderline(self):
		"""The one exception the IRS makes, and the one place a name signal beats
		the register: an attorney is reportable even when incorporated."""
		from erpnext_mcp.tools.tax import _verdict

		verdict, reason = _verdict("Friend & Reagan PC", {}, {"party_type": "Corporation"})
		self.assertEqual(verdict, "borderline")
		self.assertIn("EVEN IF the firm is incorporated", reason)

	def test_a_related_party_can_also_make_a_vendor_exempt(self):
		self.tool_data(
			"create_related_party",
			{
				"company": MAIN,
				"party_name": COOPER,
				"party_type": "Corporation",
				"relationship_to_company": "Vendor",
				"effective_date": "2020-01-01",
				"supplier": COOPER,
			},
		)
		self.assertEqual(self.verdicts()[COOPER], "exempt")

	def test_a_disclosable_relationship_is_flagged_on_the_recipient(self):
		self.tool_data(
			"create_related_party",
			{
				"company": MAIN,
				"party_name": MITCHELL,
				"party_type": "Family Member",
				"relationship_to_company": "Family",
				"effective_date": "2020-01-01",
				"supplier": MITCHELL,
			},
		)
		data = self.prefill(dry_run=True)
		self.assertEqual(data["related_party_recipients"], [MITCHELL])
		row = next(item for item in data["recipients"] if item["recipient"] == MITCHELL)
		self.assertTrue(row["disclosable"])

	def test_a_name_containing_law_inside_a_word_is_not_a_law_firm(self):
		"""`Lawson` is not an attorney, and a vendor wrongly ruled one gets a 1099
		it should not have had."""
		from erpnext_mcp.tools.tax import _verdict

		verdict, _reason = _verdict("Lawson Supply Co", {}, {})
		self.assertEqual(verdict, "exempt")

	def test_a_government_name_is_flagged_rather_than_dropped(self):
		from erpnext_mcp.tools.tax import _verdict

		verdict, reason = _verdict("City of The Dalles", {}, {})
		self.assertEqual(verdict, "borderline")
		self.assertIn("hint, not a determination", reason)

	def test_somebody_who_is_also_an_employee_is_flagged_on_the_row(self):
		STORE.seed(
			"Employee",
			[{"name": "HR-EMP-00001", "employee_name": MITCHELL, "company": MAIN, "status": "Active"}],
		)
		row = next(item for item in self.prefill(dry_run=True)["recipients"] if item["recipient"] == MITCHELL)
		self.assertTrue(row["possible_employee"])


class PrefillThreshold(DocumentTestCase):
	def test_a_vendor_under_the_threshold_is_listed_rather_than_absent(self):
		data = self.prefill(dry_run=True)
		below = {row["recipient"]: row["total_payments"] for row in data["below_threshold"]}
		self.assertEqual(below[QUILL], 120.0)
		self.assertNotIn(QUILL, [row["recipient"] for row in data["recipients"]])

	def test_lowering_the_threshold_brings_them_in(self):
		data = self.prefill(dry_run=True, threshold=100)
		self.assertIn(QUILL, [row["recipient"] for row in data["recipients"]])

	def test_the_default_threshold_is_six_hundred(self):
		self.assertEqual(self.prefill(dry_run=True)["threshold"], 600.0)

	def test_an_explicit_null_threshold_still_gets_the_statutory_floor(self):
		"""`as_float(None)` is 0.0, which would quietly include every vendor."""
		self.assertEqual(self.prefill(dry_run=True, threshold=None)["threshold"], 600.0)

	def test_a_negative_threshold_is_refused(self):
		self.assertIn("zero or more", self.prefill_error(threshold=-1))

	def test_an_exempt_vendor_over_the_threshold_gets_no_form(self):
		data = self.prefill(dry_run=True)
		self.assertIn(BRIGHT_ORCHARD, [row["recipient"] for row in data["exempt_above_threshold"]])
		self.assertNotIn(BRIGHT_ORCHARD, [row["recipient"] for row in data["recipients"]])


class PrefillGate(DocumentTestCase):
	def test_a_tax_year_that_has_not_ended_is_refused(self):
		message = self.prefill_error(tax_year=2026)
		self.assertIn("has not ended", message)
		self.assertIn("2027-01-01", message)

	def test_a_year_with_no_supplier_postings_is_refused_with_what_to_check(self):
		message = self.prefill_error(tax_year=2020)
		self.assertIn("nothing to report", message)
		self.assertIn("get_journal_entries", message)

	def test_a_missing_tax_year_is_refused(self):
		self.assertIn("tax_year is required", self.tool_error("generate_1099_prefill", {"company": MAIN}))

	def test_an_implausible_year_is_refused(self):
		self.assertIn("four-digit year", self.prefill_error(tax_year=99))


class PrefillOutput(DocumentTestCase):
	def test_a_dry_run_writes_nothing_but_names_what_it_would_produce(self):
		before = len(STORE.rows("Governance Document"))
		data = self.prefill(dry_run=True)
		self.assertEqual(len(STORE.rows("Governance Document")), before)
		self.assertEqual(len(data["would_produce"]["forms"]), 4)
		self.assertIn("1099-NEC-2025", data["would_produce"]["workbook"])

	def test_it_files_a_tax_filing_with_the_workbook_and_a_form_per_recipient(self):
		data = self.prefill()
		archive = frappe.get_doc("Governance Document", data["governance_document"])
		self.assertEqual(archive.category, "Tax Filing")
		self.assertTrue(data["workbook"]["file_name"].endswith(".xlsx"))
		self.assertEqual(len(data["forms"]), 4)
		self.assertEqual(len(data["forms"]), data["recipient_count"])

	def test_the_workbook_is_a_readable_spreadsheet_with_the_four_sheets(self):
		payload = self.stored(self.prefill()["workbook"])
		archive = zipfile.ZipFile(io.BytesIO(payload))
		self.assertEqual(len([name for name in archive.namelist() if name.startswith("xl/worksheets/")]), 4)
		sheet = archive.read("xl/worksheets/sheet1.xml").decode()
		self.assertIn(SORREN, sheet)
		self.assertIn("<v>24360.0</v>", sheet)

	def test_the_provenance_sheet_records_how_the_figures_were_reached(self):
		archive = zipfile.ZipFile(io.BytesIO(self.stored(self.prefill()["workbook"])))
		sheet = archive.read("xl/worksheets/sheet4.xml").decode()
		self.assertIn("Payable-type accounts", sheet)
		self.assertIn("erpnext_mcp", sheet)

	def test_each_form_is_a_pdf_with_three_copies(self):
		data = self.prefill()
		form = next(row for row in data["forms"] if row["recipient"] == SORREN)
		payload = self.stored(form)
		self.assertTrue(payload.startswith(b"%PDF-1.4"))
		for copy in (b"Copy A", b"Copy B", b"Copy C"):
			self.assertIn(copy, payload)
		self.assertIn(b"24,360.00", payload)

	def test_copy_a_is_stamped_as_not_filable(self):
		"""Printing a self-generated Copy A and mailing it is not a filing."""
		payload = self.stored(self.prefill()["forms"][0])
		self.assertIn(b"NOT FOR IRS SUBMISSION", payload)
		self.assertIn(b"red-ink", payload)

	def test_a_recipient_tin_prints_as_four_digits_and_says_to_finish_it(self):
		self.tool_data(
			"create_related_party",
			{
				"company": MAIN,
				"party_name": MITCHELL,
				"party_type": "Individual",
				"relationship_to_company": "Vendor",
				"effective_date": "2020-01-01",
				"supplier": MITCHELL,
				"tax_id_type": "SSN",
				"tax_id_last4": "6789",
			},
		)
		data = self.prefill()
		form = next(row for row in data["forms"] if row["recipient"] == MITCHELL)
		payload = self.stored(form)
		self.assertIn(b"XXX-XX-6789", payload)
		self.assertIn(b"complete from the W-9", payload)

	def test_a_recipient_with_no_tin_is_told_to_get_a_w9(self):
		payload = self.stored(self.prefill()["forms"][0])
		self.assertIn(b"obtain a signed W-9", payload)

	def test_the_payers_tin_reaches_the_form_and_not_the_result(self):
		frappe.db.set_value("Company", MAIN, "tax_id", "93-7654321")
		data = self.prefill()
		self.assertTrue(data["payer"]["tin_on_file"])
		self.assertNotIn("93-7654321", str(data))
		self.assertIn(b"93-7654321", self.stored(data["forms"][0]))

	def test_forms_can_be_skipped_for_the_workbook_alone(self):
		data = self.prefill(include_forms=False)
		self.assertEqual(data["forms"], [])
		self.assertTrue(data["workbook"]["file_size"] > 0)

	def test_borderline_recipients_produce_a_warning_before_anything_is_mailed(self):
		data = self.prefill()
		self.assertIn("Do not mail those without checking the W-9", data["warning"])

	def test_running_the_same_year_twice_is_refused_naming_the_first(self):
		first = self.prefill()
		message = self.prefill_error()
		self.assertIn(first["governance_document"], message)
		self.assertIn("run of record", message)

	def test_it_posts_nothing_to_the_ledger(self):
		before = len(STORE.rows("GL Entry"))
		self.prefill()
		self.assertEqual(len(STORE.rows("GL Entry")), before)


# ── output_path: the filesystem rule ────────────────────────────────────────
class OutputPath(DocumentTestCase):
	def private_files(self, *parts):
		# Resolved, because the tool resolves: on macOS `/var` is a symlink to
		# `/private/var`, and a comparison against the unresolved path would fail
		# for a reason that has nothing to do with the confinement rule.
		return os.path.realpath(get_site_path("private", "files", *parts))

	def test_a_relative_path_lands_under_the_sites_private_files(self):
		data = self.report(output_path="reports/q2.pdf")
		written = data["written_to_disk"][0]
		self.assertEqual(written["path"], self.private_files("reports", "q2.pdf"))
		self.assertTrue(os.path.exists(written["path"]))
		with open(written["path"], "rb") as handle:
			self.assertTrue(handle.read().startswith(b"%PDF-1.4"))

	def test_a_directory_gets_the_generated_name(self):
		data = self.report(output_path="reports/")
		self.assertTrue(data["written_to_disk"][0]["path"].endswith("-Example-Trading-Co.pdf"))

	def test_the_sha256_in_the_result_matches_the_bytes_on_disk(self):
		import hashlib

		data = self.report(output_path="reports/q2.pdf")
		with open(data["written_to_disk"][0]["path"], "rb") as handle:
			self.assertEqual(hashlib.sha256(handle.read()).hexdigest(), data["written_to_disk"][0]["sha256"])

	def test_an_absolute_path_outside_the_site_is_refused(self):
		message = self.report_error(output_path="/etc/erpnext-mcp-report.pdf")
		self.assertIn("outside this site's file storage", message)
		self.assertIn("Nothing was written", message)
		self.assertFalse(os.path.exists("/etc/erpnext-mcp-report.pdf"))

	def test_a_dot_dot_escape_is_refused_on_the_resolved_path(self):
		message = self.report_error(output_path="../../site_config.json")
		self.assertIn("outside this site's file storage", message)

	def test_a_refused_path_leaves_no_archive_entry_behind(self):
		"""Failing safe means failing before the first write, not during."""
		before = len(STORE.rows("Governance Document"))
		self.report_error(output_path="/etc/nope.pdf")
		self.assertEqual(len(STORE.rows("Governance Document")), before)

	def test_an_existing_file_is_not_clobbered_by_default(self):
		self.report(output_path="reports/q2.pdf")
		message = self.report_error(
			output_path="reports/q2.pdf", title="Quarterly Investment Report 2026-Q2 — restated"
		)
		self.assertIn("already exists", message)
		self.assertIn("previous report", message)

	def test_overwrite_replaces_it_and_says_so(self):
		self.report(output_path="reports/q2.pdf")
		data = self.report(
			output_path="reports/q2.pdf",
			overwrite=True,
			title="Quarterly Investment Report 2026-Q2 — restated",
		)
		self.assertTrue(data["written_to_disk"][0]["overwrote"])

	def test_the_1099_output_path_is_a_directory_of_files(self):
		data = self.prefill(output_path="tax/2025")
		names = sorted(os.path.basename(row["path"]) for row in data["written_to_disk"])
		self.assertEqual(len(names), 5)
		self.assertTrue(names[0].endswith(".pdf"))
		self.assertTrue(any(name.endswith(".xlsx") for name in names))
		for row in data["written_to_disk"]:
			self.assertTrue(os.path.exists(row["path"]))

	def test_the_1099_refuses_a_path_that_is_an_existing_file(self):
		os.makedirs(self.private_files(), exist_ok=True)
		with open(self.private_files("taken"), "wb") as handle:
			handle.write(b"x")
		message = self.prefill_error(output_path="taken")
		self.assertIn("needs a directory", message)

	def test_a_symlink_pointing_out_of_the_site_is_refused(self):
		"""The check is on the resolved real path, so a link cannot be used to
		step outside the storage area."""
		os.makedirs(self.private_files(), exist_ok=True)
		link = self.private_files("escape")
		os.symlink(os.path.dirname(SITE_ROOT), link)
		message = self.report_error(output_path="escape/q2.pdf")
		self.assertIn("outside this site's file storage", message)

	def test_no_output_path_writes_nothing_to_disk(self):
		self.assertEqual(self.report()["written_to_disk"], [])


# ── switches ────────────────────────────────────────────────────────────────
class Switches(DocumentTestCase):
	def test_both_generators_ship_off(self):
		self.configure(enabled=1)
		for tool, arguments in (
			("generate_quarterly_investment_report", {"company": MAIN, "quarter": QUARTER}),
			("generate_1099_prefill", {"company": MAIN, "tax_year": TAX_YEAR}),
		):
			with self.subTest(tool=tool):
				message = self.tool_error(tool, arguments)
				self.assertIn(f"allow_{tool}", message)
				self.assertIn("switched off", message)

	def test_a_disabled_generator_writes_nothing(self):
		self.configure(enabled=1)
		before = len(STORE.rows("Governance Document"))
		self.tool_error("generate_1099_prefill", {"company": MAIN, "tax_year": TAX_YEAR})
		self.assertEqual(len(STORE.rows("Governance Document")), before)

	def test_they_disappear_without_the_governance_archive(self):
		from .harness import INSTALLED_DOCTYPES

		INSTALLED_DOCTYPES.discard("Governance Document")
		message = self.tool_error(
			"generate_quarterly_investment_report", {"company": MAIN, "quarter": QUARTER}
		)
		self.assertIn("not available on this site", message)

	def test_both_generators_are_audited_on_success_and_on_refusal(self):
		self.report()
		self.assertAudited("generate_quarterly_investment_report", "Success")
		self.report_error(quarter="2026-Q3")
		self.assertAudited("generate_quarterly_investment_report", "Error")
		self.prefill()
		self.assertAudited("generate_1099_prefill", "Success")
		self.prefill_error(tax_year=2026)
		self.assertAudited("generate_1099_prefill", "Error")

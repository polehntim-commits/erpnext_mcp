# SPDX-License-Identifier: MIT
"""The ten read tools, over the fixture site."""

from .fixtures import (
	BANK_ACCOUNT,
	MAIN,
	MAIN_ABBR,
	OTHER,
	SeededTestCase,
	cash,
	sales,
)
from .harness import INSTALLED_DOCTYPES, STORE

#: The ten read tools v0.1.0 shipped — what the no-write snapshot below covers.
ACCOUNTING_READ_TOOLS = (
	"get_company_topology",
	"get_account_balance",
	"get_journal_entries",
	"get_journal_entry",
	"list_bank_transactions",
	"get_bank_statement",
	"list_fiscal_years",
	"get_chart_of_accounts",
	"list_unreconciled_bank_transactions",
	"search_accounts",
)


class Topology(SeededTestCase):
	def test_reports_both_companies_with_their_roots(self):
		data = self.tool_data("get_company_topology")
		self.assertEqual(data["count"], 2)
		names = [company["name"] for company in data["companies"]]
		self.assertEqual(names, [MAIN, OTHER])
		main = data["companies"][0]
		self.assertEqual(main["root_types"], ["Asset", "Expense", "Income", "Liability"])

	def test_reports_the_default_cost_center_under_whichever_fieldname(self):
		data = self.tool_data("get_company_topology")
		self.assertEqual(data["companies"][0]["default_cost_center"], f"Main - {MAIN_ABBR}")

	def test_a_company_without_a_cost_center_is_null_not_an_error(self):
		"""A Company created before its chart of accounts is a real state."""
		data = self.tool_data("get_company_topology")
		self.assertIsNone(data["companies"][1]["default_cost_center"])

	def test_company_scoped_and_global_fiscal_years_both_appear(self):
		data = self.tool_data("get_company_topology")
		main_years = {year["name"] for year in data["companies"][0]["fiscal_years"]}
		other_years = {year["name"] for year in data["companies"][1]["fiscal_years"]}
		self.assertEqual(main_years, {"2026", "2025"})
		# 2026 is linked to MAIN only; 2025 has no links so it applies to all.
		self.assertEqual(other_years, {"2025"})

	def test_reports_which_optional_doctypes_the_site_has(self):
		INSTALLED_DOCTYPES.discard("Bank Statement")
		data = self.tool_data("get_company_topology")
		self.assertFalse(data["optional_doctypes"]["Bank Statement"])
		self.assertTrue(data["optional_doctypes"]["Bank Transaction"])


class AccountBalance(SeededTestCase):
	def test_sums_the_ledger_up_to_the_as_of_date(self):
		data = self.tool_data("get_account_balance", {"account": cash(), "as_of": "2026-06-30"})
		self.assertEqual(data["total_debit"], 1000)
		self.assertEqual(data["total_credit"], 250)
		self.assertEqual(data["balance"], 750)

	def test_excludes_cancelled_gl_entries(self):
		"""The 9999 debit in the fixture is cancelled. Including it is the single
		easiest way to compute a wrong balance."""
		data = self.tool_data("get_account_balance", {"account": cash(), "as_of": "2026-06-30"})
		self.assertEqual(data["gl_entry_count"], 2)
		self.assertNotEqual(data["balance"], 750 + 9999)

	def test_later_entries_are_excluded_by_as_of(self):
		early = self.tool_data("get_account_balance", {"account": cash(), "as_of": "2026-06-30"})
		late = self.tool_data("get_account_balance", {"account": cash(), "as_of": "2026-12-31"})
		self.assertEqual(early["balance"], 750)
		self.assertEqual(late["balance"], 1250)

	def test_asset_balance_natural_matches_the_raw_balance(self):
		data = self.tool_data("get_account_balance", {"account": cash(), "as_of": "2026-06-30"})
		self.assertEqual(data["balance"], data["balance_natural"])

	def test_income_balance_natural_is_flipped(self):
		"""A credit balance on an Income account is normal, and must not be
		reported as a negative number a model will read as a loss."""
		data = self.tool_data("get_account_balance", {"account": sales(), "as_of": "2026-06-30"})
		self.assertEqual(data["balance"], -1000)
		self.assertEqual(data["balance_natural"], 1000)

	def test_resolves_an_account_by_number(self):
		data = self.tool_data(
			"get_account_balance", {"account": "1100", "company": MAIN, "as_of": "2026-06-30"}
		)
		self.assertEqual(data["account"], cash())

	def test_resolves_an_account_by_name(self):
		data = self.tool_data(
			"get_account_balance", {"account": "Cash", "company": MAIN, "as_of": "2026-06-30"}
		)
		self.assertEqual(data["account"], cash())

	def test_an_ambiguous_name_lists_the_candidates(self):
		""" "Cash" exists in both companies. Guessing one would be worse than
		asking."""
		message = self.tool_error("get_account_balance", {"account": "Cash"})
		self.assertIn("matches 2 accounts", message)
		self.assertIn("set company", message.lower())

	def test_an_unknown_account_points_at_search_accounts(self):
		message = self.tool_error("get_account_balance", {"account": "Petty Cash"})
		self.assertIn("search_accounts", message)

	def test_an_account_from_the_wrong_company_is_refused(self):
		message = self.tool_error("get_account_balance", {"account": cash(), "company": OTHER})
		self.assertIn("belongs to company", message)

	def test_a_group_account_says_so(self):
		data = self.tool_data("get_account_balance", {"account": "Current Assets", "company": MAIN})
		self.assertTrue(data["is_group"])
		self.assertIn("group account", data["note"])

	def test_defaults_as_of_to_today(self):
		data = self.tool_data("get_account_balance", {"account": cash(), "company": MAIN})
		self.assertEqual(data["as_of"], "2026-07-24")


class JournalEntries(SeededTestCase):
	def test_lists_headers_in_a_date_range_newest_first(self):
		data = self.tool_data("get_journal_entries", {"from_date": "2026-01-01", "to_date": "2026-12-31"})
		self.assertEqual(
			[row["name"] for row in data["journal_entries"]],
			["ACC-JV-2026-00002", "ACC-JV-2026-00001"],
		)

	def test_range_excludes_earlier_years(self):
		data = self.tool_data("get_journal_entries", {"from_date": "2026-01-01", "to_date": "2026-12-31"})
		self.assertNotIn("ACC-JV-2025-00009", [row["name"] for row in data["journal_entries"]])

	def test_filters_by_docstatus_word_or_number(self):
		by_word = self.tool_data(
			"get_journal_entries",
			{"from_date": "2020-01-01", "to_date": "2030-01-01", "docstatus": "draft"},
		)
		by_number = self.tool_data(
			"get_journal_entries",
			{"from_date": "2020-01-01", "to_date": "2030-01-01", "docstatus": 0},
		)
		self.assertEqual([row["name"] for row in by_word["journal_entries"]], ["ACC-JV-2026-00002"])
		self.assertEqual(by_word["journal_entries"], by_number["journal_entries"])

	def test_labels_the_docstatus(self):
		data = self.tool_data("get_journal_entries", {"from_date": "2026-01-01", "to_date": "2026-12-31"})
		labels = {row["name"]: row["docstatus_label"] for row in data["journal_entries"]}
		self.assertEqual(labels["ACC-JV-2026-00001"], "submitted")
		self.assertEqual(labels["ACC-JV-2026-00002"], "draft")

	def test_filters_by_an_account_on_any_line(self):
		data = self.tool_data(
			"get_journal_entries",
			{"from_date": "2020-01-01", "to_date": "2030-01-01", "account": sales()},
		)
		self.assertEqual([row["name"] for row in data["journal_entries"]], ["ACC-JV-2026-00001"])

	def test_an_account_no_entry_touches_returns_empty_not_everything(self):
		data = self.tool_data(
			"get_journal_entries",
			{
				"from_date": "2020-01-01",
				"to_date": "2030-01-01",
				"account": "2100 - Accounts Payable - ETC",
			},
		)
		self.assertEqual(data["count"], 0)

	def test_inverted_date_range_is_refused(self):
		message = self.tool_error("get_journal_entries", {"from_date": "2026-12-31", "to_date": "2026-01-01"})
		self.assertIn("is after", message)

	def test_limit_is_capped_and_truncation_is_reported(self):
		data = self.tool_data(
			"get_journal_entries",
			{"from_date": "2020-01-01", "to_date": "2030-01-01", "limit": 1},
		)
		self.assertEqual(data["count"], 1)
		self.assertTrue(data["truncated"])

	def test_absurd_limit_is_clamped_not_rejected(self):
		data = self.tool_data(
			"get_journal_entries",
			{"from_date": "2020-01-01", "to_date": "2030-01-01", "limit": 100000},
		)
		self.assertEqual(data["limit"], 500)

	def test_a_malformed_date_says_what_to_send(self):
		message = self.tool_error(
			"get_journal_entries", {"from_date": "last tuesday", "to_date": "2026-01-01"}
		)
		self.assertIn("YYYY-MM-DD", message)

	def test_a_loose_date_is_normalised_rather_than_rejected(self):
		data = self.tool_data("get_journal_entries", {"from_date": "2026-1-1", "to_date": "2026-12-31"})
		self.assertEqual(data["filters"]["from_date"], "2026-01-01")


class JournalEntryDetail(SeededTestCase):
	def test_returns_every_line(self):
		data = self.tool_data("get_journal_entry", {"name": "ACC-JV-2026-00001"})
		self.assertEqual(len(data["accounts"]), 2)
		self.assertEqual(data["accounts"][0]["account"], cash())
		self.assertEqual(data["accounts"][0]["debit"], 1000)

	def test_reports_whether_it_balances(self):
		data = self.tool_data("get_journal_entry", {"name": "ACC-JV-2026-00001"})
		self.assertTrue(data["balanced"])

	def test_unknown_name_is_a_clean_error(self):
		message = self.tool_error("get_journal_entry", {"name": "ACC-JV-9999-99999"})
		self.assertIn("no Journal Entry named", message)


class BankTransactions(SeededTestCase):
	def test_lists_with_a_normalised_signed_amount(self):
		data = self.tool_data("list_bank_transactions", {"bank_account": BANK_ACCOUNT})
		by_name = {row["name"]: row for row in data["bank_transactions"]}
		self.assertEqual(by_name["BT-2026-0001"]["amount_signed"], 1000)
		self.assertEqual(by_name["BT-2026-0002"]["amount_signed"], -250)
		self.assertEqual(data["amount_layout"], "deposit_withdrawal")

	def test_resolves_a_bank_account_by_its_account_name(self):
		data = self.tool_data("list_bank_transactions", {"bank_account": "Operating"})
		self.assertEqual(data["filters"]["bank_account"], BANK_ACCOUNT)

	def test_unknown_bank_account_lists_the_known_ones(self):
		message = self.tool_error("list_bank_transactions", {"bank_account": "Savings"})
		self.assertIn(BANK_ACCOUNT, message)

	def test_filters_by_date_range_and_status(self):
		data = self.tool_data(
			"list_bank_transactions",
			{"from_date": "2026-02-01", "to_date": "2026-02-28"},
		)
		self.assertEqual([row["name"] for row in data["bank_transactions"]], ["BT-2026-0002"])

		data = self.tool_data("list_bank_transactions", {"status": "Unreconciled"})
		self.assertEqual([row["name"] for row in data["bank_transactions"]], ["BT-2026-0001"])

	def test_open_ended_date_filters_work(self):
		data = self.tool_data("list_bank_transactions", {"from_date": "2026-02-01"})
		self.assertEqual(data["count"], 1)
		data = self.tool_data("list_bank_transactions", {"to_date": "2026-01-31"})
		self.assertEqual(data["count"], 1)


class Unreconciled(SeededTestCase):
	def test_lists_only_transactions_with_money_left(self):
		data = self.tool_data("list_unreconciled_bank_transactions", {"bank_account": BANK_ACCOUNT})
		self.assertEqual([row["name"] for row in data["unreconciled"]], ["BT-2026-0001"])
		self.assertEqual(data["unreconciled"][0]["unallocated_amount_effective"], 1000)
		self.assertEqual(data["unallocated_source"], "column")

	def test_computes_the_remainder_when_the_column_is_absent(self):
		"""Older ERPNext has no unallocated_amount, so the tool has to do the
		arithmetic itself and still give the same answer."""
		from .harness import META

		meta = META["Bank Transaction"]
		removed = meta._by_name.pop("unallocated_amount")
		try:
			data = self.tool_data("list_unreconciled_bank_transactions", {"bank_account": BANK_ACCOUNT})
			self.assertEqual([row["name"] for row in data["unreconciled"]], ["BT-2026-0001"])
			self.assertEqual(data["unallocated_source"], "computed (gross - allocated)")
			self.assertEqual(data["unreconciled"][0]["unallocated_amount_effective"], 1000)
		finally:
			meta._by_name["unallocated_amount"] = removed


class BankStatement(SeededTestCase):
	def test_returns_the_whole_document(self):
		data = self.tool_data("get_bank_statement", {"name": "BS-2026-01"})
		self.assertEqual(data["closing_balance"], 1000)

	def test_is_not_advertised_on_a_site_without_the_doctype(self):
		INSTALLED_DOCTYPES.discard("Bank Statement")
		body, _ = self.call("tools/list")
		self.assertNotIn("get_bank_statement", [tool["name"] for tool in body["result"]["tools"]])

	def test_degrades_politely_on_a_site_without_the_doctype(self):
		INSTALLED_DOCTYPES.discard("Bank Statement")
		message = self.tool_error("get_bank_statement", {"name": "BS-2026-01"})
		self.assertIn("not available on this site", message)
		self.assertIn("Bank Statement DocType", message)


class FiscalYears(SeededTestCase):
	def test_lists_all_years_newest_first(self):
		data = self.tool_data("list_fiscal_years")
		self.assertEqual([year["name"] for year in data["fiscal_years"]], ["2026", "2025"])

	def test_scopes_to_one_company_including_global_years(self):
		data = self.tool_data("list_fiscal_years", {"company": OTHER})
		self.assertEqual([year["name"] for year in data["fiscal_years"]], ["2025"])
		self.assertEqual(data["company_agnostic_years"], ["2025"])

	def test_accepts_a_company_abbreviation(self):
		data = self.tool_data("list_fiscal_years", {"company": MAIN_ABBR})
		self.assertEqual(data["company"], MAIN)


class ChartOfAccounts(SeededTestCase):
	def test_nests_children_under_their_parents(self):
		data = self.tool_data("get_chart_of_accounts", {"company": MAIN})
		roots = {node["account_name"]: node for node in data["accounts"]}
		self.assertIn("Application of Funds (Assets)", roots)
		current = roots["Application of Funds (Assets)"]["children"][0]
		self.assertEqual(current["account_name"], "Current Assets")
		self.assertEqual(
			sorted(child["account_name"] for child in current["children"]),
			["Bank Checking", "Cash", "Cash Clearing"],
		)

	def test_scopes_to_one_company(self):
		data = self.tool_data("get_chart_of_accounts", {"company": MAIN})
		self.assertEqual(data["flat_count"], 11)

	def test_root_type_filter_reparents_orphans_to_the_top(self):
		"""Filtering to Income cuts the group above the leaf, so the leaf has to
		surface rather than vanish."""
		data = self.tool_data("get_chart_of_accounts", {"company": MAIN, "root_type": "Income"})
		names = [node["account_name"] for node in data["accounts"]]
		self.assertEqual(names, ["Income"])
		self.assertEqual(data["accounts"][0]["children"][0]["account_name"], "Sales")

	def test_bad_root_type_lists_the_valid_ones(self):
		message = self.tool_error("get_chart_of_accounts", {"company": MAIN, "root_type": "Assets"})
		self.assertIn("Asset, Liability, Income, Expense, Equity", message)

	def test_company_is_required_on_a_multi_company_site(self):
		message = self.tool_error("get_chart_of_accounts")
		self.assertIn("multi-company", message)
		self.assertIn(MAIN, message)

	def test_company_is_inferred_on_a_single_company_site(self):
		STORE.tables["Company"].pop(OTHER)
		data = self.tool_data("get_chart_of_accounts")
		self.assertEqual(data["company"], MAIN)


class SearchAccounts(SeededTestCase):
	def test_finds_by_substring_of_the_name(self):
		data = self.tool_data("search_accounts", {"query": "clearing", "company": MAIN})
		self.assertEqual([row["account_name"] for row in data["matches"]], ["Cash Clearing"])

	def test_ranks_an_exact_number_first(self):
		data = self.tool_data("search_accounts", {"query": "1100", "company": MAIN})
		self.assertEqual(data["matches"][0]["account_name"], "Cash")

	def test_ranks_an_exact_name_above_a_substring(self):
		"""'Cash' must beat 'Cash Clearing' — the top hit being right is what
		saves the follow-up call."""
		data = self.tool_data("search_accounts", {"query": "cash", "company": MAIN})
		self.assertEqual(data["matches"][0]["account_name"], "Cash")
		self.assertIn("Cash Clearing", [row["account_name"] for row in data["matches"]])

	def test_is_case_insensitive(self):
		lower = self.tool_data("search_accounts", {"query": "cash", "company": MAIN})
		upper = self.tool_data("search_accounts", {"query": "CASH", "company": MAIN})
		self.assertEqual([row["name"] for row in lower["matches"]], [row["name"] for row in upper["matches"]])

	def test_searches_all_companies_when_none_is_given(self):
		data = self.tool_data("search_accounts", {"query": "clearing"})
		self.assertEqual(data["count"], 2)

	def test_no_match_is_an_empty_list_not_an_error(self):
		data = self.tool_data("search_accounts", {"query": "cryptocurrency"})
		self.assertEqual(data["count"], 0)

	def test_reports_how_many_were_found_before_the_limit(self):
		data = self.tool_data("search_accounts", {"query": "a", "limit": 2})
		self.assertEqual(data["count"], 2)
		self.assertGreater(data["total_before_limit"], 2)


class ReadToolsDoNotWrite(SeededTestCase):
	def test_no_read_tool_changes_a_row(self):
		"""The whole read surface, checked against a snapshot of the site."""
		before = {doctype: len(rows) for doctype, rows in STORE.tables.items()}
		calls = {
			"get_company_topology": {},
			"get_account_balance": {"account": cash(), "company": MAIN},
			"get_journal_entries": {"from_date": "2020-01-01", "to_date": "2030-01-01"},
			"get_journal_entry": {"name": "ACC-JV-2026-00001"},
			"list_bank_transactions": {},
			"get_bank_statement": {"name": "BS-2026-01"},
			"list_fiscal_years": {},
			"get_chart_of_accounts": {"company": MAIN},
			"list_unreconciled_bank_transactions": {"bank_account": BANK_ACCOUNT},
			"search_accounts": {"query": "cash"},
		}
		for name, arguments in calls.items():
			self.tool_data(name, arguments)
		after = {doctype: len(rows) for doctype, rows in STORE.tables.items()}
		# Only the audit log grew.
		after.pop("MCP Action Log", None)
		before.pop("MCP Action Log", None)
		self.assertEqual(before, after)

	def test_the_snapshot_covered_every_accounting_read_tool(self):
		"""The v0.1.0 ledger surface, all ten of it. Later categories have their
		own no-write coverage in their own test modules."""
		from erpnext_mcp import registry

		self.assertEqual(len(registry.READ_TOOLS), 432)
		self.assertTrue(set(ACCOUNTING_READ_TOOLS) <= set(registry.READ_TOOLS))


class DisabledReadTool(SeededTestCase):
	def test_a_disabled_read_tool_is_refused_with_the_field_to_tick(self):
		self.configure(enabled=1, allow_search_accounts=0)
		message = self.tool_error("search_accounts", {"query": "cash"})
		self.assertIn("allow_search_accounts", message)
		self.assertIn("switched off", message)

	def test_a_disabled_tool_never_sees_its_arguments(self):
		"""It is refused on the name alone, so nothing about the arguments — valid
		or not — can leak back."""
		self.configure(enabled=1, allow_get_account_balance=0)
		message = self.tool_error("get_account_balance", {"account": "definitely not real"})
		self.assertNotIn("search_accounts", message)
		self.assertIn("allow_get_account_balance", message)

	def test_unknown_tool_is_named_in_the_error(self):
		message = self.tool_error("drop_all_tables")
		self.assertIn("unknown tool", message)

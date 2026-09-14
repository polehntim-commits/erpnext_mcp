# SPDX-License-Identifier: MIT
"""Co-op equity and patronage dividends — v0.166.0.

EIGHT CLAIMS.

1. `TheDoctypeCarriesTheColumns` — the co-op columns show for exactly the two
   co-op categories and cost center hides for them, derived from the code's tuple.
2. `CapturingCoopMoney` — Co-op Equity sets `is_balance_sheet_item`, Patronage
   does not; the amount must be positive; cost center and `coop_name` are refused
   where they do not belong; a recategorisation carries the flag with it.
3. `NotAnExpense` — the expense summary leaves both out, and a Purchase Invoice is
   refused by name.
4. `TheAccountsFollowTheChart` — equity created under 1800 at 1830, moved to the
   next free number when one is taken, idempotent, dry-runnable, and answered
   company by company; Dividend Income found by name and never created.
5. `PostingAReceipt` — the draft Journal Entry's lines for equity, a retirement and
   a patronage split, and every refusal before anything is written.
6. `TheSummary` — positions by company and co-op, retained patronage read off the
   Journal Entry, rejected receipts left out.
7. `TheClassifier` — patronage and equity wording choose a category; a co-op's
   name alone does not, and a co-op fuel slip is still fuel.
8. `FromAPhone` — `coop_name` reaches the tool through the mobile route.
"""

import json
from pathlib import Path

import frappe

from erpnext_mcp.api import mobile as mobile_api
from erpnext_mcp.tools import expenses

from .fixtures import BANK, MAIN, MAIN_ABBR, OTHER, OTHER_ABBR, PurchasingTestCase, V12TestCase, install_hrms
from .harness import STORE
from .test_api_mobile import MobileAPITestCase

DOCTYPES = Path(__file__).resolve().parents[1] / "erpnext_mcp" / "erpnext_mcp" / "doctype"

ON = {
	f"allow_{name}": 1
	for name in (
		"submit_expense_receipt",
		"get_expense_receipt",
		"list_expense_receipts",
		"approve_expense_receipt",
		"reject_expense_receipt",
		"update_expense_receipt",
		"get_expense_summary",
		"create_purchase_invoice_from_receipt",
		"classify_receipt",
		"ensure_coop_accounts",
		"post_coop_receipt",
		"list_coop_equity_summary",
	)
}

EQUITY = f"1830 - Co-op Equity Investments - {MAIN_ABBR}"
PATRONAGE = f"4230 - Dividend Income - {MAIN_ABBR}"


def seed_groups(company=MAIN, abbr=MAIN_ABBR, dividend_number="4230"):
	"""A `1800 - Investments` group and a `Dividend Income` ledger, as the design doc's chart has.

	`dividend_number` is there because the Umbrel site files Dividend Income as 4220 and
	uses 4230 for Realized Capital Gains; the account is found by name either way.
	"""
	rows = [
		{
			"name": f"1800 - Investments - {abbr}",
			"account_name": "Investments",
			"account_number": "1800",
			"parent_account": f"Application of Funds (Assets) - {abbr}",
			"is_group": 1,
			"root_type": "Asset",
			"account_type": "",
			"account_currency": "USD",
			"disabled": 0,
			"company": company,
		},
		{
			"name": f"{dividend_number} - Dividend Income - {abbr}",
			"account_name": "Dividend Income",
			"account_number": dividend_number,
			"parent_account": f"Income - {abbr}",
			"is_group": 0,
			"root_type": "Income",
			"account_type": "Income Account",
			"account_currency": "USD",
			"disabled": 0,
			"company": company,
		},
	]
	STORE.seed("Account", rows)


class CoopTestCase(V12TestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1, **ON)
		install_hrms()

	def capture(self, **overrides):
		payload = {
			"merchant": "Farm Credit West",
			"amount": 2500,
			"receipt_date": "2026-03-15",
			"category": "Co-op Equity",
			"company": MAIN,
			"submitted_by": "HR-EMP-00001",
		}
		payload.update(overrides)
		return self.tool_data("submit_expense_receipt", payload)

	def approved(self, **overrides):
		name = self.capture(**overrides)["name"]
		self.tool_data("approve_expense_receipt", {"name": name, "approved_by": "HR-EMP-00001"})
		return name

	def ready(self):
		seed_groups()
		self.tool_data("ensure_coop_accounts", {"company": MAIN})

	def lines(self, journal_entry):
		doc = frappe.get_doc("Journal Entry", journal_entry)
		out = []
		for line in doc.get("accounts"):
			get = line.get if isinstance(line, dict) else (lambda key, line=line: getattr(line, key, None))
			out.append((get("account"), float(get("debit") or 0), float(get("credit") or 0)))
		return sorted(out)


# ── 1. ───────────────────────────────────────────────────────────────────────
class TheDoctypeCarriesTheColumns(V12TestCase):
	def test_the_columns_show_for_exactly_the_coop_categories(self):
		data = json.loads((DOCTYPES / "expense_receipt" / "expense_receipt.json").read_text())
		by_name = {field["fieldname"]: field for field in data["fields"]}
		listed = json.dumps(list(expenses.COOP_CATEGORIES), separators=(",", ":"))
		for field in ("coop_section", "coop_name", "is_balance_sheet_item"):
			with self.subTest(field=field):
				self.assertIn(field, data["field_order"])
				self.assertEqual(by_name[field]["depends_on"], f"eval:{listed}.includes(doc.category)")
		self.assertEqual(by_name["cost_center"]["depends_on"], f"eval:!{listed}.includes(doc.category)")
		self.assertEqual(by_name["is_balance_sheet_item"]["read_only"], 1)
		self.assertEqual(tuple(by_name["category"]["options"].split("\n")), expenses.CATEGORIES)


# ── 2. ───────────────────────────────────────────────────────────────────────
class CapturingCoopMoney(CoopTestCase):
	def test_an_equity_purchase_is_a_balance_sheet_item(self):
		data = self.capture()
		self.assertTrue(data["is_balance_sheet_item"])
		self.assertEqual(data["coop_name"], "Farm Credit West")
		back = self.tool_data("get_expense_receipt", {"name": data["name"]})
		self.assertIs(back["is_balance_sheet_item"], True)
		listed = self.tool_data("list_expense_receipts", {"company": MAIN})["receipts"][0]
		self.assertIs(listed["is_balance_sheet_item"], True)

	def test_a_patronage_dividend_is_not(self):
		data = self.capture(category="Patronage Dividend", coop_name="Tree Top")
		self.assertFalse(data["is_balance_sheet_item"])
		self.assertEqual(data["coop_name"], "Tree Top")
		self.assertIs(
			self.tool_data("get_expense_receipt", {"name": data["name"]})["is_balance_sheet_item"], False
		)

	def test_an_ordinary_receipt_reads_false(self):
		data = self.capture(category="Fuel", merchant="Valley Co-op Fuel")
		self.assertIs(
			self.tool_data("get_expense_receipt", {"name": data["name"]})["is_balance_sheet_item"], False
		)

	def test_the_amount_must_be_positive(self):
		for amount in (None, 0):
			with self.subTest(amount=amount):
				payload = {
					"merchant": "Farm Credit West",
					"receipt_date": "2026-03-15",
					"category": "Patronage Dividend",
					"company": MAIN,
					"submitted_by": "HR-EMP-00001",
				}
				if amount is not None:
					payload["amount"] = amount
				error = self.tool_error("submit_expense_receipt", payload)
				self.assertIn("amount", error)
		self.assertEqual(len(STORE.rows("Expense Receipt")), 0)

	def test_a_cost_center_is_refused(self):
		error = self.tool_error(
			"submit_expense_receipt",
			{
				"merchant": "Farm Credit West",
				"amount": 100,
				"receipt_date": "2026-03-15",
				"category": "Co-op Equity",
				"company": MAIN,
				"submitted_by": "HR-EMP-00001",
				"cost_center": f"Main - {MAIN_ABBR}",
			},
		)
		self.assertIn("cost_center does not apply", error)

	def test_coop_name_is_refused_on_an_expense(self):
		error = self.tool_error(
			"submit_expense_receipt",
			{
				"merchant": "Valley Co-op Fuel",
				"amount": 100,
				"receipt_date": "2026-03-15",
				"category": "Fuel",
				"company": MAIN,
				"submitted_by": "HR-EMP-00001",
				"coop_name": "Valley Co-op",
			},
		)
		self.assertIn("coop_name belongs to", error)

	def test_recategorising_carries_the_flag_both_ways(self):
		name = self.capture(category="Other", merchant="CHS Inc")["name"]
		into = self.tool_data("update_expense_receipt", {"name": name, "category": "Co-op Equity"})
		self.assertEqual(into["after"]["is_balance_sheet_item"], 1)
		self.assertEqual(frappe.db.get_value("Expense Receipt", name, "coop_name"), "CHS Inc")
		out = self.tool_data("update_expense_receipt", {"name": name, "category": "Supplies"})
		self.assertEqual(out["after"]["is_balance_sheet_item"], 0)
		self.assertEqual(int(frappe.db.get_value("Expense Receipt", name, "is_balance_sheet_item") or 0), 0)

	def test_recategorising_into_coop_needs_the_cost_center_cleared(self):
		name = self.capture(category="Other", cost_center=f"Main - {MAIN_ABBR}")["name"]
		error = self.tool_error("update_expense_receipt", {"name": name, "category": "Co-op Equity"})
		self.assertIn("cost_center as ''", error)
		done = self.tool_data(
			"update_expense_receipt", {"name": name, "category": "Co-op Equity", "cost_center": ""}
		)
		self.assertEqual(done["after"]["is_balance_sheet_item"], 1)


# ── 3. ───────────────────────────────────────────────────────────────────────
class NotAnExpense(CoopTestCase):
	def test_the_expense_summary_leaves_both_out(self):
		self.capture(category="Fuel", merchant="Valley Co-op Fuel", amount=180)
		self.capture()
		self.capture(category="Patronage Dividend", amount=900)
		data = self.tool_data(
			"get_expense_summary", {"company": MAIN, "from_date": "2026-01-01", "to_date": "2026-12-31"}
		)
		self.assertEqual(data["total_amount"], 180.0)
		self.assertEqual(data["count"], 1)
		self.assertEqual(data["coop_excluded"], 2)
		self.assertNotIn("Co-op Equity", data["by_category"])
		self.assertIn("list_coop_equity_summary", data["note"])


class NoPurchaseInvoiceForCoopMoney(PurchasingTestCase):
	def setUp(self):
		super().setUp()
		install_hrms()
		self.configure(enabled=1, **ON)

	def test_both_categories_are_refused_by_name(self):
		for category in expenses.COOP_CATEGORIES:
			with self.subTest(category=category):
				name = CoopTestCase.approved(self, category=category)
				error = self.tool_error("create_purchase_invoice_from_receipt", {"receipt": name})
				self.assertIn("post_coop_receipt", error)

	capture = CoopTestCase.capture


# ── 4. ───────────────────────────────────────────────────────────────────────
class TheAccountsFollowTheChart(CoopTestCase):
	def test_both_accounts_are_created_at_the_preferred_numbers(self):
		seed_groups()
		data = self.tool_data("ensure_coop_accounts", {"company": MAIN})
		row = data["companies"][0]
		self.assertEqual(row["equity"]["account"], EQUITY)
		self.assertEqual(row["equity"]["parent_account"], f"1800 - Investments - {MAIN_ABBR}")
		self.assertEqual(row["patronage"]["account"], PATRONAGE)
		self.assertEqual(frappe.db.get_value("Account", EQUITY, "root_type"), "Asset")
		self.assertEqual(row["patronage"]["action"], "existing")
		self.assertEqual(data["created_count"], 1)

	def test_running_it_again_creates_nothing(self):
		self.ready()
		again = self.tool_data("ensure_coop_accounts", {"company": MAIN})
		self.assertEqual(again["created_count"], 0)
		self.assertEqual(again["companies"][0]["equity"]["action"], "existing")

	def test_a_taken_number_moves_to_the_next_free_one(self):
		"""The deployed site's 1830 is Accumulated Depreciation - Machinery."""
		seed_groups()
		STORE.seed(
			"Account",
			[
				{
					"name": f"1830 - Accumulated Depreciation - Machinery - {MAIN_ABBR}",
					"account_name": "Accumulated Depreciation - Machinery",
					"account_number": "1830",
					"parent_account": f"Application of Funds (Assets) - {MAIN_ABBR}",
					"is_group": 0,
					"root_type": "Asset",
					"company": MAIN,
				}
			],
		)
		row = self.tool_data("ensure_coop_accounts", {"company": MAIN})["companies"][0]
		self.assertEqual(row["equity"]["account"], f"1831 - Co-op Equity Investments - {MAIN_ABBR}")
		self.assertIn("Accumulated Depreciation", row["equity"]["note"])

	def test_a_dry_run_creates_nothing(self):
		seed_groups()
		data = self.tool_data("ensure_coop_accounts", {"company": MAIN, "dry_run": True})
		self.assertEqual(data["companies"][0]["equity"]["action"], "would_create")
		self.assertFalse(frappe.db.exists("Account", EQUITY))

	def test_a_chart_without_the_groups_is_refused_in_its_own_row(self):
		seed_groups()  # MAIN only
		data = self.tool_data("ensure_coop_accounts", {})
		rows = {row["company"]: row for row in data["companies"]}
		self.assertEqual(rows[MAIN]["equity"]["action"], "created")
		self.assertEqual(rows[OTHER]["equity"]["action"], "refused")
		self.assertIn("equity_parent", rows[OTHER]["equity"]["reason"])
		self.assertEqual(rows[OTHER]["patronage"]["action"], "missing")
		self.assertEqual(data["dividend_income_missing_count"], 1)
		self.assertFalse(
			frappe.db.get_all("Account", filters={"company": OTHER, "account_name": "Dividend Income"})
		)

	def test_a_named_parent_puts_it_somewhere_else(self):
		data = self.tool_data(
			"ensure_coop_accounts",
			{
				"company": OTHER,
				"equity_parent": f"Application of Funds (Assets) - {OTHER_ABBR}",
			},
		)
		row = data["companies"][0]
		self.assertEqual(row["equity"]["account"], f"1830 - Co-op Equity Investments - {OTHER_ABBR}")
		self.assertEqual(row["patronage"]["action"], "missing")


# ── 5. ───────────────────────────────────────────────────────────────────────
class PostingAReceipt(CoopTestCase):
	def test_an_equity_purchase_debits_the_equity_account(self):
		self.ready()
		name = self.approved()
		data = self.tool_data("post_coop_receipt", {"receipt": name})
		self.assertEqual(data["docstatus"], 0)
		self.assertEqual(
			self.lines(data["journal_entry"]), sorted([(EQUITY, 2500.0, 0.0), (BANK, 0.0, 2500.0)])
		)
		self.assertEqual(
			frappe.db.get_value("Expense Receipt", name, "linked_document"), data["journal_entry"]
		)

	def test_a_retirement_posts_the_other_way(self):
		self.ready()
		name = self.approved(is_return=True)
		data = self.tool_data("post_coop_receipt", {"receipt": name})
		self.assertEqual(
			self.lines(data["journal_entry"]), sorted([(BANK, 2500.0, 0.0), (EQUITY, 0.0, 2500.0)])
		)

	def test_patronage_splits_cash_and_retained_equity(self):
		self.ready()
		name = self.approved(category="Patronage Dividend", amount=1000)
		data = self.tool_data("post_coop_receipt", {"receipt": name, "retained_amount": 800})
		self.assertEqual(
			self.lines(data["journal_entry"]),
			sorted([(BANK, 200.0, 0.0), (EQUITY, 800.0, 0.0), (PATRONAGE, 0.0, 1000.0)]),
		)
		self.assertEqual(data["cost_center"], f"Main - {MAIN_ABBR}")

	def test_every_refusal_writes_nothing(self):
		self.ready()
		fuel = self.approved(category="Fuel", merchant="Valley Co-op Fuel")
		unapproved = self.capture()["name"]
		equity = self.approved()
		patronage = self.approved(category="Patronage Dividend", amount=1000)
		entries_before = len(STORE.rows("Journal Entry"))
		for args, words in (
			({"receipt": fuel}, "books only"),
			({"receipt": unapproved}, "not Approved"),
			({"receipt": equity, "retained_amount": 10}, "applies to a patronage"),
			({"receipt": patronage, "retained_amount": 1001}, "between 0 and"),
		):
			with self.subTest(args=args):
				self.assertIn(words, self.tool_error("post_coop_receipt", args))
		self.assertEqual(len(STORE.rows("Journal Entry")), entries_before)
		self.tool_data("post_coop_receipt", {"receipt": equity})
		self.assertIn("already linked", self.tool_error("post_coop_receipt", {"receipt": equity}))

	def test_a_site_names_its_own_accounts(self):
		"""The MCP pathway: nothing in the module has to know this chart's names."""
		STORE.seed(
			"Account",
			[
				{
					"name": f"1850 - Member Capital - {MAIN_ABBR}",
					"account_name": "Member Capital",
					"account_number": "1850",
					"parent_account": f"Application of Funds (Assets) - {MAIN_ABBR}",
					"is_group": 0,
					"root_type": "Asset",
					"company": MAIN,
				},
				{
					"name": f"4260 - Co-op Refunds - {MAIN_ABBR}",
					"account_name": "Co-op Refunds",
					"account_number": "4260",
					"parent_account": f"Income - {MAIN_ABBR}",
					"is_group": 0,
					"root_type": "Income",
					"company": MAIN,
				},
			],
		)
		member_capital = f"1850 - Member Capital - {MAIN_ABBR}"
		name = self.approved(category="Patronage Dividend", amount=1000)
		data = self.tool_data(
			"post_coop_receipt",
			{
				"receipt": name,
				"retained_amount": 600,
				"equity_account": member_capital,
				"income_account": "Co-op Refunds",
			},
		)
		self.assertEqual(data["equity_account_resolved_by"], "argument")
		self.assertEqual(data["income_account"], f"4260 - Co-op Refunds - {MAIN_ABBR}")
		self.assertEqual(
			self.lines(data["journal_entry"]),
			sorted([(BANK, 400.0, 0.0), (member_capital, 600.0, 0.0), (data["income_account"], 0.0, 1000.0)]),
		)
		summary = self.tool_data("list_coop_equity_summary", {"company": MAIN})
		self.assertEqual(summary["positions"][0]["patronage_retained"], 600.0)

	def test_a_named_account_of_the_wrong_kind_is_refused(self):
		self.ready()
		name = self.approved(category="Patronage Dividend", amount=1000)
		for args, words in (
			({"equity_account": PATRONAGE}, "equity_account"),
			({"income_account": BANK}, "income_account"),
		):
			with self.subTest(args=args):
				error = self.tool_error("post_coop_receipt", {"receipt": name, **args})
				self.assertIn(words, error)
				self.assertIn("root type", error)
		self.assertFalse(frappe.db.get_value("Expense Receipt", name, "linked_document"))

	def test_income_account_is_refused_on_equity(self):
		self.ready()
		name = self.approved()
		error = self.tool_error("post_coop_receipt", {"receipt": name, "income_account": PATRONAGE})
		self.assertIn("income_account applies to a patronage dividend", error)

	def test_dividend_income_is_found_by_name_not_by_4230(self):
		"""The Umbrel site: Dividend Income is 4220 and 4230 is Realized Capital Gains."""
		seed_groups(dividend_number="4220")
		STORE.seed(
			"Account",
			[
				{
					"name": f"4230 - Realized Capital Gains - {MAIN_ABBR}",
					"account_name": "Realized Capital Gains",
					"account_number": "4230",
					"parent_account": f"Income - {MAIN_ABBR}",
					"is_group": 0,
					"root_type": "Income",
					"company": MAIN,
				}
			],
		)
		self.tool_data("ensure_coop_accounts", {"company": MAIN})
		name = self.approved(category="Patronage Dividend", amount=400)
		data = self.tool_data("post_coop_receipt", {"receipt": name})
		self.assertEqual(data["income_account"], f"4220 - Dividend Income - {MAIN_ABBR}")

	def test_patronage_without_dividend_income_is_refused_by_name(self):
		STORE.seed(
			"Account",
			[
				{
					"name": f"1800 - Investments - {MAIN_ABBR}",
					"account_name": "Investments",
					"account_number": "1800",
					"parent_account": f"Application of Funds (Assets) - {MAIN_ABBR}",
					"is_group": 1,
					"root_type": "Asset",
					"company": MAIN,
				}
			],
		)
		self.tool_data("ensure_coop_accounts", {"company": MAIN})
		name = self.approved(category="Patronage Dividend", amount=400)
		error = self.tool_error("post_coop_receipt", {"receipt": name})
		self.assertIn("which a patronage dividend posts to", error)
		# ensure_coop_accounts never creates Dividend Income, so it must not be the advice.
		self.assertNotIn("ensure_coop_accounts", error)
		self.assertFalse(frappe.db.get_value("Expense Receipt", name, "linked_document"))

	def test_a_company_without_the_accounts_is_told_to_create_them(self):
		name = self.approved()
		self.assertIn("ensure_coop_accounts", self.tool_error("post_coop_receipt", {"receipt": name}))


# ── 6. ───────────────────────────────────────────────────────────────────────
class TheSummary(CoopTestCase):
	def test_positions_by_company_and_coop(self):
		self.ready()
		self.approved(amount=2500, receipt_date="2026-02-01")
		retired = self.approved(amount=500, is_return=True, receipt_date="2026-05-01")
		patronage = self.approved(category="Patronage Dividend", amount=1000, receipt_date="2026-04-01")
		self.tool_data("post_coop_receipt", {"receipt": patronage, "retained_amount": 800})
		self.tool_data("post_coop_receipt", {"receipt": retired})
		self.capture(merchant="Tree Top", amount=300)
		rejected = self.capture(amount=9999)["name"]
		self.tool_data(
			"reject_expense_receipt",
			{"name": rejected, "reason": "duplicate of the March notice", "rejected_by": "HR-EMP-00001"},
		)

		data = self.tool_data("list_coop_equity_summary", {"company": MAIN})
		farm_credit = next(p for p in data["positions"] if p["coop_name"] == "Farm Credit West")
		self.assertEqual(farm_credit["equity_invested"], 2500.0)
		self.assertEqual(farm_credit["equity_redeemed"], 500.0)
		self.assertEqual(farm_credit["patronage_received"], 1000.0)
		self.assertEqual(farm_credit["patronage_retained"], 800.0)
		self.assertEqual(farm_credit["net_equity"], 2800.0)
		self.assertEqual(farm_credit["transaction_count"], 3)
		self.assertEqual(farm_credit["unposted_count"], 1)
		self.assertEqual((farm_credit["first_date"], farm_credit["last_date"]), ("2026-02-01", "2026-05-01"))
		self.assertEqual(data["companies"][0]["coop_count"], 2)
		self.assertEqual(data["companies"][0]["net_equity"], 3100.0)

	def test_companies_are_kept_apart(self):
		self.capture()
		self.capture(company=OTHER, submitted_by="HR-EMP-00001")
		data = self.tool_data("list_coop_equity_summary", {})
		self.assertEqual(sorted(row["company"] for row in data["companies"]), sorted([MAIN, OTHER]))

	def test_the_coop_filter_narrows(self):
		self.capture()
		self.capture(merchant="Tree Top")
		data = self.tool_data("list_coop_equity_summary", {"coop_name": "tree"})
		self.assertEqual([p["coop_name"] for p in data["positions"]], ["Tree Top"])


# ── 7. ───────────────────────────────────────────────────────────────────────
class TheClassifier(CoopTestCase):
	def classify(self, **args):
		return self.tool_data("classify_receipt", args)

	def test_a_patronage_notice_suggests_patronage(self):
		data = self.classify(
			text="AgWest Farm Credit\nPATRONAGE REFUND 2025\nCash portion 20%\nAllocated equity 80%"
		)
		self.assertEqual(data["receipt_type"], "expense")
		self.assertEqual(data["suggested_category"], "Patronage Dividend")
		self.assertEqual(data["coop"]["coop_name"], "Farm Credit")
		self.assertIn("patronage refund", data["matched_signals"])

	def test_an_equity_retirement_is_equity_coming_back(self):
		data = self.classify(text="CHS Inc\nEQUITY RETIREMENT CHECK\nMember #4471")
		self.assertEqual(data["suggested_category"], "Co-op Equity")
		self.assertTrue(data["coop"]["is_return"])
		self.assertEqual(data["coop"]["coop_name"], "CHS Inc.")

	def test_a_coop_fuel_slip_is_still_fuel(self):
		data = self.classify(
			merchant="Valley Co-op Fuel", text="VALLEY CO-OP FUEL\nPUMP 4\nDIESEL 42.1 GALLONS\nTOTAL 184.62"
		)
		self.assertIsNone(data["suggested_category"])
		self.assertIn("gallons", data["matched_signals"])

	def test_a_coops_name_alone_names_it_and_chooses_nothing(self):
		data = self.classify(text="TREE TOP INC\nGROWER STATEMENT\nPOOL RETURN\nNET PROCEEDS 9240.00")
		self.assertEqual(data["receipt_type"], "settlement")
		self.assertIsNone(data["suggested_category"])
		self.assertEqual(data["coop"]["coop_name"], "Tree Top")


# ── 8. ───────────────────────────────────────────────────────────────────────
class FromAPhone(MobileAPITestCase):
	def test_coop_name_reaches_the_tool(self):
		self.be()
		data = mobile_api.create_expense_receipt(
			merchant="AGWEST FARM CREDIT",
			amount=1200,
			receipt_date="2026-03-15",
			category="Co-op Equity",
			coop_name="AgWest Farm Credit",
		)
		self.assertEqual(data["coop_name"], "AgWest Farm Credit")
		self.assertTrue(data["is_balance_sheet_item"])

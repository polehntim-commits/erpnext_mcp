# SPDX-License-Identifier: MIT
"""Trailing twelve months on the financial statements. v0.333.0.

FIVE CLAIMS.

1. `TheWindow` — twelve full months ending at the last month-end on or before as_of.
2. `TheFilters` — ERPNext's own statement is asked for a Date Range: Yearly for one
   column, Monthly for twelve, a running balance only on the Balance Sheet.
3. `TheShapes` — one TTM column, twelve plus Total, and the prior TTM with the change.
4. `TheDeskReport` — Financial Statements TTM hands its filters to the same code.
5. `OverMCP` — run_report period="TTM" through the Desk's runner; get_windowed_report period="TTM".

`statement()` below stands in for ERPNext: the same period keys (`%b_%Y` of each
period's end), the same Total-only-when-not-Yearly rule and the same running
balances, read from a ledger of monthly amounts. The real execute was checked
against the image's ERPNext source (financial_statements.get_period_list,
get_columns) when this was written.
"""

import datetime
from unittest import mock

import frappe

from erpnext_mcp.erpnext_mcp.report.financial_statements_ttm import financial_statements_ttm as desk_report
from erpnext_mcp.services import ttm_statements as ttm

from .fixtures import MAIN, SeededTestCase
from .harness import STORE

SALES = "4110 - Sales"
FUEL = "5100 - Fuel"
CASH = "1110 - Cash"
OLD = "5900 - Discontinued"


def _months(start, end):
	day = datetime.date.fromisoformat(start).replace(day=1)
	stop = datetime.date.fromisoformat(end)
	while day <= stop:
		yield day
		day = (day.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)


def _month_end(day):
	return (day.replace(day=28) + datetime.timedelta(days=4)).replace(day=1) - datetime.timedelta(days=1)


#: 1,000 of sales and 100 of fuel every month; the discontinued account only before 2025-10.
def amount(account, month):
	if account == SALES:
		return 1000.0
	if account == FUEL:
		return 100.0
	if account == OLD:
		return 50.0 if month < datetime.date(2025, 10, 1) else 0.0
	return 0.0


def statement(name, filters, calls=None):
	"""ERPNext's statement shape for a Date Range, as get_period_list and get_columns make it."""
	if calls is not None:
		calls.append(dict(filters))
	months = list(_months(filters["period_start_date"], filters["period_end_date"]))
	step = 12 if filters["periodicity"] == "Yearly" else 1
	periods = [months[i : i + step] for i in range(0, len(months), step)]
	keys = [_month_end(chunk[-1]).strftime("%b_%Y").lower() for chunk in periods]
	columns = [{"fieldname": "account", "label": "Account", "fieldtype": "Link"}]
	columns += [{"fieldname": key, "label": key, "fieldtype": "Currency"} for key in keys]
	if filters["periodicity"] != "Yearly" and not filters["accumulated_values"]:
		columns.append({"fieldname": "total", "label": "Total", "fieldtype": "Currency"})
	accounts = [CASH] if name == ttm.BALANCE_SHEET else [SALES, FUEL, OLD]
	rows = []
	for account in accounts:
		row = {"account": account, "parent_account": None, "indent": 0}
		total = 0.0
		for key, chunk in zip(keys, periods, strict=True):
			if name == ttm.BALANCE_SHEET:
				# A running balance: everything up to the period's end, from 2024-01.
				value = 900.0 * len(list(_months("2024-01-01", _month_end(chunk[-1]).isoformat())))
			elif name == ttm.CASH_FLOW and filters["periodicity"] == "Yearly":
				# ERPNext's own quirk, seen on a live bench: one Yearly Cash Flow period across a
				# fiscal-year boundary counts only the months after it.
				year = _month_end(chunk[-1]).year
				value = sum(amount(account, month) for month in chunk if month.year == year)
			else:
				value = sum(amount(account, month) for month in chunk)
			row[key] = value
			total += value
		if name != ttm.BALANCE_SHEET and not any(row[key] for key in keys):
			continue  # ERPNext leaves out a line with nothing in the period
		if "total" in [column["fieldname"] for column in columns]:
			row["total"] = total
		rows.append(row)
	rows.append({})  # the blank spacer ERPNext puts between sections
	return columns, rows, None, {"data": "chart"}, [{"label": "summary"}]


class TheWindow(SeededTestCase):
	def test_mid_month_ends_at_the_last_month_end(self):
		span = ttm.window("2026-10-17")
		self.assertEqual((span["period_start"], span["period_end"]), ("2025-10-01", "2026-09-30"))
		self.assertEqual((span["prior_start"], span["prior_end"]), ("2024-10-01", "2025-09-30"))
		self.assertEqual(span["label"], "TTM Oct 2025 – Sep 2026")

	def test_a_month_end_is_itself_the_end(self):
		self.assertEqual(ttm.window("2026-09-30")["period_end"], "2026-09-30")

	def test_february(self):
		span = ttm.window("2028-03-01")
		self.assertEqual((span["period_start"], span["period_end"]), ("2027-03-01", "2028-02-29"))

	def test_statement_names(self):
		self.assertEqual(ttm.statement_name("P&L"), ttm.PROFIT_AND_LOSS)
		self.assertEqual(ttm.statement_name("balance sheet"), ttm.BALANCE_SHEET)
		self.assertEqual(ttm.statement_name("General Ledger"), "")


class TheFilters(SeededTestCase):
	SPAN = ttm.window("2026-10-17")

	def test_one_ttm_column_is_a_yearly_date_range(self):
		got = ttm.report_filters(
			ttm.PROFIT_AND_LOSS,
			{
				"company": MAIN,
				"filter_based_on": "Fiscal Year",
				"statement": "x",
				"cost_center": "Main, Block 4",
			},
			self.SPAN,
			monthly=False,
		)
		self.assertEqual(got["filter_based_on"], "Date Range")
		self.assertEqual((got["period_start_date"], got["period_end_date"]), ("2025-10-01", "2026-09-30"))
		self.assertEqual((got["periodicity"], got["accumulated_values"]), ("Yearly", 0))
		self.assertEqual(got["cost_center"], ["Main", "Block 4"])
		self.assertNotIn("statement", got)

	def test_monthly_and_the_balance_sheet(self):
		self.assertEqual(
			ttm.report_filters(ttm.CASH_FLOW, {}, self.SPAN, monthly=True)["periodicity"], "Monthly"
		)
		balance = ttm.report_filters(ttm.BALANCE_SHEET, {}, self.SPAN, monthly=True)
		self.assertEqual(balance["accumulated_values"], 1)

	def test_the_prior_run_is_always_one_column(self):
		got = ttm.report_filters(ttm.PROFIT_AND_LOSS, {}, self.SPAN, monthly=True, prior=True)
		self.assertEqual((got["period_start_date"], got["period_end_date"]), ("2024-10-01", "2025-09-30"))
		self.assertEqual(got["periodicity"], "Yearly")


class TheShapes(SeededTestCase):
	def run_it(self, name=ttm.PROFIT_AND_LOSS, **kwargs):
		calls = []
		result = ttm.run(
			name, {"company": MAIN}, as_of="2026-10-17", runner=lambda n, f: statement(n, f, calls), **kwargs
		)
		return result, calls

	def row(self, result, account):
		return next(row for row in result["data"] if row.get("account") == account)

	def test_one_column_is_the_ttm_and_says_so(self):
		result, calls = self.run_it()
		self.assertEqual(len(calls), 1)
		self.assertEqual(self.row(result, SALES)["sep_2026"], 12000.0)
		label = next(c["label"] for c in result["columns"] if c["fieldname"] == "sep_2026")
		self.assertEqual(label, "TTM Oct 2025 – Sep 2026")
		self.assertEqual(result["chart"], {"data": "chart"})

	def test_monthly_columns_keep_erpnexts_total(self):
		result, _ = self.run_it(monthly=True)
		fields = ttm.period_fields(result["columns"])
		self.assertEqual(len(fields), 12)
		self.assertEqual((fields[0], fields[-1]), ("oct_2025", "sep_2026"))
		self.assertEqual(self.row(result, FUEL)["total"], 1200.0)

	def test_compare_adds_the_prior_ttm_and_the_change(self):
		result, calls = self.run_it(compare=True)
		self.assertEqual(len(calls), 2)
		self.assertEqual(calls[1]["period_end_date"], "2025-09-30")
		sales = self.row(result, SALES)
		self.assertEqual((sales[ttm.PRIOR_FIELD], sales[ttm.CHANGE_FIELD]), (12000.0, 0.0))
		labels = {c["fieldname"]: c["label"] for c in result["columns"]}
		self.assertEqual(labels[ttm.PRIOR_FIELD], "Prior TTM Oct 2024 – Sep 2025")

	def test_a_line_only_in_the_prior_year_is_not_lost(self):
		result, _ = self.run_it(compare=True, monthly=True)
		old = self.row(result, OLD)
		self.assertEqual(old[ttm.PRIOR_FIELD], 600.0)
		self.assertEqual(old[ttm.CHANGE_FIELD], -600.0)
		self.assertEqual(self.row(result, FUEL)[ttm.CHANGE_FIELD], 0.0)  # Total vs prior, monthly

	def test_balance_sheet_is_as_of_and_a_year_earlier(self):
		result, calls = self.run_it(ttm.BALANCE_SHEET, compare=True)
		self.assertEqual(calls[0]["accumulated_values"], 1)
		cash = self.row(result, CASH)
		# Balances from 2024-01: 33 months at 2026-09-30, 21 at 2025-09-30.
		self.assertEqual((cash["sep_2026"], cash[ttm.PRIOR_FIELD]), (29700.0, 18900.0))
		self.assertEqual(cash[ttm.CHANGE_FIELD], 10800.0)
		labels = {c["fieldname"]: c["label"] for c in result["columns"]}
		self.assertEqual(
			(labels["sep_2026"], labels[ttm.PRIOR_FIELD]), ("As of 30 Sep 2026", "As of 30 Sep 2025")
		)

	def test_cash_flow_ttm_is_twelve_months_across_a_fiscal_year_boundary(self):
		result, calls = self.run_it(ttm.CASH_FLOW, compare=True)
		self.assertEqual({call["periodicity"] for call in calls}, {"Monthly"})
		sales = self.row(result, SALES)
		self.assertEqual((sales["sep_2026"], sales[ttm.PRIOR_FIELD]), (12000.0, 12000.0))
		self.assertEqual(ttm.period_fields(result["columns"]), ["sep_2026"])
		self.assertNotIn("total", [c["fieldname"] for c in result["columns"]])

	def test_a_prior_period_that_cannot_run_is_a_warning(self):
		def runner(name, filters):
			if filters["period_end_date"] == "2025-09-30":
				raise frappe.ValidationError("Date 2024-10-01 is not in any active Fiscal Year")
			return statement(name, filters)

		result = ttm.run(
			ttm.PROFIT_AND_LOSS, {"company": MAIN}, as_of="2026-10-17", compare=True, runner=runner
		)
		self.assertIn("no comparison", result["message"])
		self.assertNotIn(ttm.PRIOR_FIELD, [c["fieldname"] for c in result["columns"]])

	def test_not_a_statement_is_refused(self):
		with self.assertRaises(ValueError):
			ttm.run("General Ledger", {}, runner=statement)


class TheDeskReport(SeededTestCase):
	def test_execute_runs_erpnexts_statement_over_the_window(self):
		calls = []
		with mock.patch.object(ttm, "run_erpnext", side_effect=lambda n, f: statement(n, f, calls)):
			_columns, data, message, _chart, _summary = desk_report.execute(
				frappe._dict(
					company=MAIN,
					statement="Cash Flow",
					as_of="2026-10-17",
					monthly_columns=1,
					compare_prior=0,
				)
			)
		self.assertEqual(calls[0]["periodicity"], "Monthly")
		self.assertIn("2025-10-01 to 2026-09-30", message)
		self.assertTrue(data)

	def test_the_report_and_its_script_ship(self):
		import json
		import pathlib

		folder = pathlib.Path(desk_report.__file__).parent
		meta = json.loads((folder / "financial_statements_ttm.json").read_text())
		self.assertEqual((meta["name"], meta["report_type"]), ("Financial Statements TTM", "Script Report"))
		script = (folder / "financial_statements_ttm.js").read_text()
		self.assertIn('frappe.query_reports["Financial Statements TTM"]', script)
		for fieldname in ("statement", "as_of", "monthly_columns", "compare_prior"):
			self.assertIn(f'fieldname: "{fieldname}"', script)


class OverMCP(SeededTestCase):
	def setUp(self):
		super().setUp()
		self.configure(enabled=1)
		STORE.seed(
			"Report",
			[
				{
					"name": name,
					"report_name": name,
					"ref_doctype": "GL Entry",
					"report_type": "Script Report",
					"module": "Accounts",
					"is_standard": "Yes",
					"disabled": 0,
				}
				for name in (ttm.PROFIT_AND_LOSS, ttm.BALANCE_SHEET, "General Ledger")
			],
		)
		self.calls = []
		for name in (ttm.PROFIT_AND_LOSS, ttm.BALANCE_SHEET):
			STORE.report_runners[name] = self._runner(name)
		self.addCleanup(
			lambda: [STORE.report_runners.pop(n, None) for n in (ttm.PROFIT_AND_LOSS, ttm.BALANCE_SHEET)]
		)

	def _runner(self, name):
		def run(filters, user):
			columns, rows, *_ = statement(name, dict(filters), self.calls)
			return {"columns": columns, "result": rows}

		return run

	def test_run_report_period_ttm(self):
		data = self.tool_data(
			"run_report",
			{
				"name": ttm.PROFIT_AND_LOSS,
				"filters": {"company": MAIN},
				"period": "TTM",
				"as_of": "2026-10-17",
				"compare_prior": True,
			},
		)
		self.assertEqual(data["ttm"]["period_end"], "2026-09-30")
		self.assertEqual(data["filters_applied"]["filter_based_on"], "Date Range")
		self.assertEqual(len(self.calls), 2)
		self.assertIn(ttm.PRIOR_FIELD, [c["fieldname"] for c in data["columns"]])
		self.assertIn("TTM window", data["executed_via"])

	def test_ttm_is_refused_on_a_report_that_is_not_a_statement(self):
		error = self.tool_error(
			"run_report", {"name": "General Ledger", "filters": {"company": MAIN}, "period": "TTM"}
		)
		self.assertIn("applies to", error)

	def test_another_period_is_refused(self):
		error = self.tool_error(
			"run_report", {"name": ttm.PROFIT_AND_LOSS, "filters": {"company": MAIN}, "period": "QTD"}
		)
		self.assertIn("period takes 'TTM'", error)

	def test_a_company_is_required(self):
		error = self.tool_error("run_report", {"name": ttm.BALANCE_SHEET, "period": "TTM"})
		self.assertIn("filters.company", error)

	def test_without_period_nothing_changes(self):
		self.tool_data(
			"run_report",
			{
				"name": ttm.PROFIT_AND_LOSS,
				"filters": {
					"company": MAIN,
					"filter_based_on": "Date Range",
					"period_start_date": "2026-01-01",
					"period_end_date": "2026-03-31",
					"periodicity": "Monthly",
					"accumulated_values": 0,
				},
			},
		)
		self.assertEqual(self.calls[0]["period_start_date"], "2026-01-01")

	def test_windowed_period_ttm(self):
		from erpnext_mcp.tools import kpi

		self.assertEqual(kpi._window_args({"period": "ttm"})["window_type"], "TTM")
		for args, words in (
			({"period": "YTD"}, "period takes 'TTM'"),
			({"period": "TTM", "window_type": "YTD"}, "disagree"),
			({"period": "TTM", "window_months": 6}, "twelve months"),
		):
			with self.subTest(args=args), self.assertRaises(Exception) as caught:
				kpi._window_args(args)
			self.assertIn(words, str(caught.exception))


# ── the Desk script, executed ───────────────────────────────────────────────
import json as _json  # noqa: E402
import pathlib as _pathlib  # noqa: E402
import shutil as _shutil  # noqa: E402
import subprocess as _subprocess  # noqa: E402
import unittest as _unittest  # noqa: E402

_DRIVER = r"""
const vm = require("vm"), fs = require("fs");
let statement = "Profit and Loss Statement";
const calls = [];
const ctx = {
  window: {}, __: (s) => s,
  frappe: {
    query_reports: {},
    query_report: { get_filter_value: (f) => (f === "statement" ? statement : undefined) },
    datetime: { get_today: () => "2026-10-17", add_months: (d, n) => "2026-09-17", month_end: (d) => "2026-09-30" },
    defaults: { get_user_default: () => "Orchard Meadow LLC" },
    set_route: (...a) => calls.push(a),
  },
};
ctx.window.erpnext = { financial_statements: { formatter: function () { return "erp:" + this.name_field; }, open_general_ledger: () => 1 } };
ctx.erpnext = ctx.window.erpnext;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), ctx);
const r = ctx.frappe.query_reports["Financial Statements TTM"];
const out = { pl: [r.name_field, r.parent_field, r.formatter()] };
statement = "Cash Flow";
out.cf = [r.name_field, r.parent_field, r.formatter()];
out.as_of = r.filters.find((f) => f.fieldname === "as_of").default;
out.compare = r.filters.find((f) => f.fieldname === "compare_prior").default;
const menu = []; r.onload({ page: { add_custom_button_group: () => "g", add_custom_menu_item: (g, l, fn) => menu.push([l, fn]) }, get_values: () => ({ company: "X" }) });
menu[0][1]();
out.menu = menu.map((m) => m[0]); out.route = calls[0];
console.log(JSON.stringify(out));
"""


@_unittest.skipUnless(_shutil.which("node"), "node is not installed on this machine")
class TheDeskScriptRuns(_unittest.TestCase):
	def test_tree_keys_follow_the_statement_and_erpnexts_look_is_borrowed(self):
		script = _pathlib.Path(desk_report.__file__).parent / "financial_statements_ttm.js"
		out = _subprocess.run(
			["node", "-e", _DRIVER, str(script)], capture_output=True, text=True, check=True, timeout=30
		)
		got = _json.loads(out.stdout.strip().splitlines()[-1])
		self.assertEqual(got["pl"], ["account", "parent_account", "erp:account"])
		self.assertEqual(got["cf"], ["section", "parent_section", "erp:section"])
		self.assertEqual((got["as_of"], got["compare"]), ("2026-09-30", 1))
		self.assertEqual(got["menu"], ["Profit and Loss", "Balance Sheet", "Cash Flow Statement"])
		self.assertEqual(got["route"], ["query-report", "Profit and Loss Statement", {"company": "X"}])

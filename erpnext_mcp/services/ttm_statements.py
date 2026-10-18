# SPDX-License-Identifier: MIT
"""Trailing twelve months on ERPNext's own financial statements.

v0.333.0. Tim: "On the financial statements is there a way to do a trailing 12
month view. Right now we have Fiscal Year, and date range."

THE STATEMENTS ARE ERPNext's, UNTOUCHED. Profit and Loss, Balance Sheet and Cash
Flow already take `filter_based_on = Date Range`; a TTM view is the right date
range and the right periodicity handed to the same `execute`, so every number is
the statutory report's own — period closing, dimensions, finance book and all.
Nothing in ERPNext is patched and no script is added to its screens; the Desk
gets one report of this app's (`Financial Statements (TTM)`) and MCP gets
`period="TTM"` on `run_report`.

THE WINDOW IS THE WINDOW STANDARD'S (`windowed_reports`): the twelve FULL months
ending at the last month-end on or before `as_of`, so a view read on the 17th
ends on the last day of the month before, and the part-finished month is never
in it. Default `as_of` is today, which makes the default the last month-end.

THREE SHAPES:

  * plain — one column, the TTM (a flow) or the balance as of the window's end;
  * `monthly` — the twelve months as columns, plus ERPNext's own Total for a flow;
  * `compare` — the prior TTM beside it (the balance a year earlier on the
    Balance Sheet) and the change.

THE COMPARISON IS A SECOND RUN OF THE SAME REPORT, merged by row. A 24-month
Yearly range would give both columns in one run, but not beside twelve monthly
ones, and one rule for every shape is easier to trust than two.
"""

from __future__ import annotations

import frappe

from . import windowed_reports as windows

PROFIT_AND_LOSS = "Profit and Loss Statement"
BALANCE_SHEET = "Balance Sheet"
CASH_FLOW = "Cash Flow"

#: ERPNext's report name → its execute, and whether it is a flow or a balance.
STATEMENTS = {
	PROFIT_AND_LOSS: {
		"execute": "erpnext.accounts.report.profit_and_loss_statement.profit_and_loss_statement.execute",
		"kind": "flow",
	},
	BALANCE_SHEET: {
		"execute": "erpnext.accounts.report.balance_sheet.balance_sheet.execute",
		"kind": "balance",
	},
	# MONTHLY ONLY: ERPNext's Cash Flow run as ONE Yearly period across a fiscal-year boundary
	# counts only the months after the boundary (found on a live bench: Oct–Sep read 9,000 of
	# 12,000; the Monthly run's own Total read 12,000). So it is always run monthly, and the
	# TTM column is ERPNext's Total.
	CASH_FLOW: {
		"execute": "erpnext.accounts.report.cash_flow.cash_flow.execute",
		"kind": "flow",
		"monthly_only": True,
	},
}

#: What people call them.
ALIASES = {
	"profit and loss": PROFIT_AND_LOSS,
	"profit and loss statement": PROFIT_AND_LOSS,
	"p&l": PROFIT_AND_LOSS,
	"pnl": PROFIT_AND_LOSS,
	"income statement": PROFIT_AND_LOSS,
	"balance sheet": BALANCE_SHEET,
	"cash flow": CASH_FLOW,
	"cash flow statement": CASH_FLOW,
}

#: This module's own filter keys, removed before ERPNext sees the filters.
OWN_KEYS = ("statement", "as_of", "monthly_columns", "compare_prior", "period")

#: ERPNext filters a TTM run sets itself; a caller's value would be overwritten.
SET_KEYS = (
	"filter_based_on",
	"period_start_date",
	"period_end_date",
	"periodicity",
	"accumulated_values",
	"from_fiscal_year",
	"to_fiscal_year",
)

PRIOR_FIELD = "prior_ttm"
CHANGE_FIELD = "ttm_change"


def statement_name(value: str) -> str:
	"""ERPNext's report name for a statement, from any of the usual names; "" if not one."""
	text = str(value or "").strip()
	if text in STATEMENTS:
		return text
	return ALIASES.get(text.lower(), "")


def window(as_of=None) -> dict:
	"""The current and prior TTM windows: full months, ending at the last month-end <= as_of."""
	end = windows.last_completed_boundary(as_of or frappe.utils.today(), windows.STEP_MONTHLY)
	start = windows.first_of_month(windows.add_months(windows.first_of_month(end), -11))
	prior_end = windows.last_of_month(windows.add_months(windows.first_of_month(end), -12))
	prior_start = windows.first_of_month(windows.add_months(windows.first_of_month(end), -23))
	return {
		"as_of": windows.iso(as_of or frappe.utils.today()),
		"period_start": windows.iso(start),
		"period_end": windows.iso(end),
		"prior_start": windows.iso(prior_start),
		"prior_end": windows.iso(prior_end),
		"label": f"TTM {_month(start)} – {_month(end)}",
		"prior_label": f"Prior TTM {_month(prior_start)} – {_month(prior_end)}",
	}


def _month(value) -> str:
	return windows.as_date(value).strftime("%b %Y")


def _day(value) -> str:
	day = windows.as_date(value)
	return f"{day.day} {day.strftime('%b %Y')}"


def report_filters(statement: str, filters: dict, span: dict, *, monthly: bool, prior: bool = False) -> dict:
	"""The ERPNext filters for one run: the caller's, less ours, with the window set."""
	kind = STATEMENTS[statement]["kind"]
	monthly_only = STATEMENTS[statement].get("monthly_only", False)
	out = {key: value for key, value in dict(filters or {}).items() if key not in OWN_KEYS + SET_KEYS}
	start, end = (
		(span["prior_start"], span["prior_end"]) if prior else (span["period_start"], span["period_end"])
	)
	out.update(
		{
			"filter_based_on": "Date Range",
			"period_start_date": start,
			"period_end_date": end,
			# The comparison is always one column: the prior TTM total, or the prior balance.
			"periodicity": "Monthly" if (monthly and not prior) or monthly_only else "Yearly",
			# A balance is a running total; a flow is the movement in each column.
			"accumulated_values": 1 if kind == "balance" else 0,
		}
	)
	for key in ("cost_center", "project"):
		if isinstance(out.get(key), str) and out[key]:
			out[key] = [part.strip() for part in out[key].split(",") if part.strip()]
	return out


def run_erpnext(statement: str, filters: dict):
	"""ERPNext's own execute, as the Desk would call it."""
	return frappe.get_attr(STATEMENTS[statement]["execute"])(frappe._dict(filters))


def _parts(result) -> dict:
	"""(columns, data, message, chart, summary, ...) or a dict, as one dict."""
	if isinstance(result, dict):
		return {
			"columns": list(result.get("columns") or []),
			"data": list(result.get("result") or result.get("data") or []),
			"message": result.get("message"),
			"chart": result.get("chart"),
			"report_summary": result.get("report_summary"),
		}
	result = list(result or [])
	result += [None] * (5 - len(result))
	return {
		"columns": list(result[0] or []),
		"data": list(result[1] or []),
		"message": result[2],
		"chart": result[3],
		"report_summary": result[4],
	}


def row_key(row) -> str:
	"""Which line of the statement a row is: its account, or its Cash Flow section."""
	if not isinstance(row, dict):
		return ""
	for field in ("account", "section", "section_name", "account_name"):
		if row.get(field):
			return str(row[field])
	return ""


def period_fields(columns: list) -> list:
	"""The period value columns, in order — not the Total, not the currency."""
	out = []
	for column in columns:
		if not isinstance(column, dict):
			continue
		name = column.get("fieldname")
		if column.get("fieldtype") == "Currency" and name not in ("total", PRIOR_FIELD, CHANGE_FIELD):
			out.append(name)
	return out


def _current_value(row: dict, fields: list, kind: str, monthly: bool):
	"""The figure the comparison is made against: the TTM total, or the closing balance."""
	if not fields:
		return None
	if kind == "flow" and monthly:
		return row.get("total")
	return row.get(fields[-1])


def run(
	statement: str, filters: dict, *, as_of=None, monthly: bool = False, compare: bool = False, runner=None
) -> dict:
	"""One statement over the TTM window, optionally monthly and against the prior TTM."""
	statement = statement_name(statement)
	if not statement:
		raise ValueError(f"not a financial statement; one of: {', '.join(STATEMENTS)}")
	runner = runner or run_erpnext
	kind = STATEMENTS[statement]["kind"]
	span = window(as_of)
	warnings = []

	monthly_only = STATEMENTS[statement].get("monthly_only", False)
	current = _parts(runner(statement, report_filters(statement, filters, span, monthly=monthly)))
	columns, data = current["columns"], current["data"]
	if monthly_only and not monthly:
		columns, data = _collapse_to_total(columns, data)
	fields = period_fields(columns)
	if not monthly and fields:
		label = span["label"] if kind == "flow" else f"As of {_day(span['period_end'])}"
		for column in columns:
			if isinstance(column, dict) and column.get("fieldname") == fields[-1]:
				column["label"] = label

	if compare:
		try:
			prior = _parts(
				runner(statement, report_filters(statement, filters, span, monthly=False, prior=True))
			)
		except Exception as exc:
			prior = None
			warnings.append(
				f"the prior period ({span['prior_start']} to {span['prior_end']}) could not be run, "
				f"so there is no comparison: {exc}"
			)
		if prior is not None:
			if monthly_only:
				prior["columns"], prior["data"] = _collapse_to_total(prior["columns"], prior["data"])
			_merge_prior(columns, data, prior, fields, kind, monthly, span)

	note = (
		f"Trailing twelve months {span['period_start']} to {span['period_end']} (full months, ending at the "
		f"last month-end on or before {span['as_of']})."
		if kind == "flow"
		else f"Balances as of {span['period_end']}, the last month-end on or before {span['as_of']}."
	)
	return {
		"columns": columns,
		"data": data,
		"message": " ".join([note, *warnings]),
		"chart": None if compare else current["chart"],
		"report_summary": current["report_summary"],
		"window": span,
		"statement": statement,
		"monthly_columns": bool(monthly),
		"compare_prior": bool(compare),
		"warnings": warnings,
		"erpnext_filters": report_filters(statement, filters, span, monthly=monthly),
	}


def _collapse_to_total(columns: list, data: list) -> tuple:
	"""Twelve monthly columns and ERPNext's Total, as one column holding the Total."""
	fields = period_fields(columns)
	if not fields:
		return columns, data
	keep = fields[-1]
	drop = set(fields[:-1]) | {"total"}
	columns = [c for c in columns if not (isinstance(c, dict) and c.get("fieldname") in drop)]
	out = []
	for row in data:
		if isinstance(row, dict) and row:
			had_value = keep in row or "total" in row
			row = {k: v for k, v in row.items() if k not in drop or k == "total"}
			total = row.pop("total", None)
			if had_value:
				row[keep] = total
		out.append(row)
	return columns, out


def _merge_prior(columns: list, data: list, prior: dict, fields: list, kind: str, monthly: bool, span: dict):
	"""Add the prior column and the change, line by line; lines only in the prior period are appended."""
	prior_fields = period_fields(prior["columns"])
	prior_field = prior_fields[-1] if prior_fields else None
	values, prior_rows = {}, {}
	for row in prior["data"]:
		key = row_key(row)
		if key and prior_field:
			values[key] = row.get(prior_field)
			prior_rows[key] = row
	columns.append(
		{
			"fieldname": PRIOR_FIELD,
			"label": span["prior_label"] if kind == "flow" else f"As of {_day(span['prior_end'])}",
			"fieldtype": "Currency",
			"options": "currency",
			"width": 160,
		}
	)
	columns.append(
		{
			"fieldname": CHANGE_FIELD,
			"label": "Change",
			"fieldtype": "Currency",
			"options": "currency",
			"width": 140,
		}
	)
	seen = set()
	for row in data:
		key = row_key(row)
		if not key:
			continue
		seen.add(key)
		before = values.get(key)
		row[PRIOR_FIELD] = before
		now = _current_value(row, fields, kind, monthly)
		if now is not None or before is not None:
			row[CHANGE_FIELD] = round(float(now or 0) - float(before or 0), 2)
	for key, row in prior_rows.items():
		if key in seen:
			continue
		extra = {k: v for k, v in row.items() if not (k in prior_fields or k == "total")}
		extra[PRIOR_FIELD] = values.get(key)
		extra[CHANGE_FIELD] = round(-float(values.get(key) or 0), 2)
		data.append(extra)

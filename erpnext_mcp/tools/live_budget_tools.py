# SPDX-License-Identifier: MIT
"""The live budget — the MCP side. v0.274.0 (docs/contracts/live_budget_v0_274.yaml).

Reads on; writes OFF until switched on. Importing makes a DRAFT plan a person publishes; asking for inputs and
drafting ERPNext Budgets are refused for a company until `live_budget_enabled` is on for it. Nothing is submitted.
"""

from __future__ import annotations

import frappe

from .. import live_budget, xlsx_lite
from ..args import as_int, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult


def _actor() -> str:
	from .. import security

	return security.caller_identity() or str(getattr(frappe.session, "user", "") or "")


def _wrap(fn):
	try:
		return fn()
	except (live_budget.BudgetError, xlsx_lite.XlsxError) as exc:
		raise ToolError(f"{exc} Nothing was changed.") from None


def _year(args: dict) -> int:
	return as_int(args, "year", int(str(frappe.utils.today())[:4]))


def import_input_plan(args: dict) -> ToolResult:
	"""The pro forma workbook (an uploaded File) → a DRAFT Input Plan for one company and year."""
	from . import files

	company = resolve_company(as_str(args, "company"), required=True)
	file = as_str(args, "file", required=True)
	if not frappe.db.exists("File", file):
		raise ToolError(f"no File {file!r} — upload the workbook first.")
	name = frappe.db.get_value("File", file, "file_name") or file
	data = _wrap(lambda: live_budget.import_pro_forma(files.read_file_bytes(file), company, _year(args),
	                                                  actor=_actor(), source=str(name)))
	return ToolResult(data=data, summary=f"Input Plan {data['plan']} v{data['version']} drafted: {data['lines']} "
	                                     f"line(s), {data['annual_total']:,.2f}; {len(data['unmapped'])} unmapped",
	                  docstatus_delta="0 → 0 (draft)")


def get_live_budget(args: dict) -> ToolResult:
	company = resolve_company(as_str(args, "company"), required=True)
	data = _wrap(lambda: live_budget.live(company, _year(args), as_str(args, "as_of")))
	flagged = [r for r in data["rows"] if r["level"] != "ok"]
	return ToolResult(data=data, summary=f"{company} {data['year']}: actual {data['total']['actual']:,.0f} + "
	                                     f"committed {data['total']['committed']:,.0f} of {data['total']['budget']:,.0f}; "
	                                     f"forecast {data['total']['forecast']:,.0f}; {len(flagged)} line(s) flagged")


def forecast_input_needs(args: dict) -> ToolResult:
	company = resolve_company(as_str(args, "company"), required=True)
	data = _wrap(lambda: live_budget.forecast_needs(company, _year(args), as_str(args, "as_of")))
	return ToolResult(data=data, summary=f"{len(data['applications'])} spray material line(s) ahead, "
	                                     f"{data['total']:,.0f}")


def request_inputs(args: dict) -> ToolResult:
	company = resolve_company(as_str(args, "company"), required=True)
	items = args.get("items")
	if isinstance(items, str):
		import json

		try:
			items = json.loads(items)
		except ValueError:
			raise ToolError("items: a JSON list of {item_code, qty}.") from None
	data = _wrap(lambda: live_budget.request_inputs(company, items, needed_by=as_str(args, "needed_by"), actor=_actor()))
	summary = (f"draft {data['material_request']}, about {data['estimated_value']:,.2f}"
	           + ("; needs approval" if data.get("needs_approval") else "")) if data.get("material_request") else \
		"nothing to order — the shed holds it"
	return ToolResult(data=data, summary=summary, docstatus_delta="none → 0 (draft)" if data.get("material_request") else "")


def draft_erpnext_budget(args: dict) -> ToolResult:
	company = resolve_company(as_str(args, "company"), required=True)
	data = _wrap(lambda: live_budget.draft_erpnext_budget(company, _year(args), actor=_actor()))
	return ToolResult(data=data, summary=f"{len(data['budgets'])} draft Budget(s), action Warn; "
	                                     f"{len(data['skipped'])} cost centre(s) skipped",
	                  docstatus_delta="none → 0 (draft)" if data["budgets"] else "")


def get_input_cost_by_block(args: dict) -> ToolResult:
	company = resolve_company(as_str(args, "company"), required=True)
	data = live_budget.input_cost_by_block(company, _year(args))
	return ToolResult(data=data, summary=f"{len(data['blocks'])} block(s), {data['total']:,.2f} in spray materials")

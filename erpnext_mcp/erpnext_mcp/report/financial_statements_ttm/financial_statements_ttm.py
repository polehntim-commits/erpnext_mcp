# SPDX-License-Identifier: MIT
"""Financial Statements TTM — Profit and Loss, Balance Sheet or Cash Flow over the
trailing twelve months, from ERPNext's own statements. v0.333.0.

The arithmetic and the window live in `services/ttm_statements.py`; this is the
Desk's door to it. Fiscal Year and Date Range stay on ERPNext's own reports.
"""

from frappe.utils import cint

from erpnext_mcp.services import ttm_statements


def execute(filters=None):
	filters = dict(filters or {})
	result = ttm_statements.run(
		filters.get("statement") or ttm_statements.PROFIT_AND_LOSS,
		filters,
		as_of=filters.get("as_of") or None,
		monthly=bool(cint(filters.get("monthly_columns"))),
		compare=bool(cint(filters.get("compare_prior"))),
	)
	return result["columns"], result["data"], result["message"], result["chart"], result["report_summary"]

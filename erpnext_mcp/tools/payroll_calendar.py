# SPDX-License-Identifier: MIT
"""get_payroll_calendar. v0.224.0 — docs/design/payroll_windows.md §2."""

from __future__ import annotations

from .. import payroll_calendar
from ..args import as_str
from ..result import ToolResult
from .employee import require_company_scope, require_hr_role


def get_payroll_calendar(args: dict) -> ToolResult:
	"""Read-only. Tax deposits and filings due: overdue, due, upcoming, done."""
	actor = require_hr_role()
	company = as_str(args, "company")
	if company:
		require_company_scope(actor, company)
	companies = [company] if company else payroll_calendar._companies_of(actor)
	rows = payroll_calendar.items(companies)
	urgent = [r for r in rows if r["state"] in ("overdue", "due")]
	return ToolResult(
		data={
			"companies": companies,
			"reminder_days": payroll_calendar.reminder_days(),
			"tile_enabled": payroll_calendar.enabled(),
			"items": rows,
			"note": (
				"Dates only. Paying (EFTPS, Oregon Revenue Online) and filing stay with a person or the CPA. "
				"Agricultural employers whose workers are farmworkers file 943 annually instead of 941."
			),
		},
		summary=f"{len(urgent)} payroll item(s) due or overdue, {len(rows)} in the window",
	)

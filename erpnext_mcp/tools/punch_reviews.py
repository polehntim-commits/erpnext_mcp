# SPDX-License-Identifier: MIT
"""Punches sent long after the tap, for a manager. v0.227.0 — `punch_times`."""

from __future__ import annotations

import frappe

from .. import punch_times, roles, security
from ..args import as_str
from ..result import ToolResult


def _actor() -> str:
	return security.caller_identity() or str(getattr(frappe.session, "user", "") or "")


def list_punch_reviews(args: dict) -> ToolResult:
	actor = _actor()
	rows = punch_times.pending(roles.companies_for(actor) or None)
	return ToolResult(
		data={"punches": rows, "count": len(rows), "window_hours": punch_times.window_hours()},
		summary=f"{len(rows)} punch(es) waiting for review",
	)


def resolve_punch_review(args: dict) -> ToolResult:
	actor = _actor()
	data = punch_times.resolve(
		actor,
		as_str(args, "row", required=True),
		as_str(args, "resolution", required=True),
		kind=as_str(args, "kind"),
		corrected_at=args.get("corrected_at"),
		note=as_str(args, "note"),
		companies=roles.companies_for(actor) or None,
	)
	return ToolResult(
		data=data, summary=f"{data['row']}: {data['resolution']}", docstatus_delta="0 → 0 (updated)"
	)

# SPDX-License-Identifier: MIT
"""Punch review: the MCP tools. v0.251.0 (AFB-2026-00032). See `erpnext_mcp.time_review`."""

from __future__ import annotations

import datetime

import frappe

from .. import time_review
from ..args import as_date, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult


def _installed() -> None:
	if not time_review.installed():
		raise ToolError("this site has not migrated to v0.251.0 (no punch review on Farm Shift crew rows).")


def _window(args: dict) -> tuple[str, str]:
	today = datetime.date.fromisoformat(str(frappe.utils.today())[:10])
	start = str(as_date(args, "from") or (today - datetime.timedelta(days=today.weekday())))[:10]
	end = str(as_date(args, "to") or today)[:10]
	if end < start:
		raise ToolError("to is before from.")
	return start, end


def list_time_reviews(args: dict) -> ToolResult:
	"""Punches in a window with their flags and review state (default this week, pending)."""
	_installed()
	company = resolve_company(as_str(args, "company")) if as_str(args, "company") else ""
	start, end = _window(args)
	status = as_str(args, "status") or "pending"
	rows = time_review.listing([company] if company else None, start, end, as_str(args, "employee"),
	                           as_str(args, "shift"), status)
	flagged = sum(1 for r in rows if r["flags"])
	return ToolResult(
		data={"from": start, "to": end, "status": status, "punches": rows, "count": len(rows), "flagged": flagged},
		summary=f"{len(rows)} punch(es) {status} {start}–{end}, {flagged} flagged",
	)


def review_punches(args: dict) -> ToolResult:
	"""Approve, fix (with a reason) or reopen punches — named rows, one shift, or a whole period."""
	_installed()
	action = as_str(args, "action", required=True)
	rows = list(args.get("rows") or [])
	if not rows:
		company = resolve_company(as_str(args, "company")) if as_str(args, "company") else ""
		if not (as_str(args, "shift") or as_date(args, "from")):
			raise ToolError("name rows, a shift, or a period (from / to). Nothing was changed.")
		start, end = _window(args)
		wanted = "reviewed" if action == "reopen" else "pending"
		rows = [r["row"] for r in time_review.listing([company] if company else None, start, end,
		                                             as_str(args, "employee"), as_str(args, "shift"), wanted)]
	corrections = args.get("corrections") or {}
	if not isinstance(corrections, dict):
		raise ToolError("corrections is {row: {in, out}}. Nothing was changed.")
	data = time_review.review(frappe.session.user, action, rows, as_str(args, "reason"), corrections)
	return ToolResult(
		data=data,
		summary=f"{action}: {len(data['done'])} punch(es) done, {len(data['refused'])} refused",
		docstatus_delta="0 → 0 (updated)",
	)

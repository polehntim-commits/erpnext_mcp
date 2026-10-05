# SPDX-License-Identifier: MIT
"""No-work notices: the MCP tools. v0.243.0. See `erpnext_mcp.work_notices`."""

from __future__ import annotations

import frappe

from .. import work_notices
from ..args import as_date, as_int, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult


def _installed() -> None:
	if not work_notices.installed():
		raise ToolError("this site has not migrated to v0.243.0 (no Work Notice doctype).")


def list_work_notices(args: dict) -> ToolResult:
	"""Notices by status ('open' = awaiting, escalated or lifted) and day."""
	_installed()
	company = resolve_company(as_str(args, "company")) if as_str(args, "company") else ""
	rows = work_notices.listing(as_str(args, "status"), str(as_date(args, "for_date") or ""), company,
	                            max(1, min(as_int(args, "limit", 100), 500)))
	waiting = sum(1 for r in rows if r["status"] in work_notices.OPEN)
	return ToolResult(
		data={"notices": rows, "count": len(rows), "need_an_answer": waiting, "enabled": work_notices.enabled()},
		summary=f"{len(rows)} work notice(s), {waiting} needing an answer",
	)


def send_work_notice(args: dict) -> ToolResult:
	"""The supervisor's (or a manager's) answer. Nothing is ever sent without one (decision 34)."""
	_installed()
	name = as_str(args, "notice", required=True)
	if not frappe.db.exists(work_notices.DOCTYPE, name):
		raise ToolError(f"no Work Notice {name!r}. Nothing was sent.")
	try:
		data = work_notices.answer(name, as_str(args, "choice", required=True), frappe.session.user,
		                           as_str(args, "alternative"), as_str(args, "alternative_task"))
	except (ValueError, PermissionError) as exc:
		raise ToolError(f"{exc} Nothing was sent.") from None
	sent = sum(1 for r in data["recipients"] if r.get("sent"))
	return ToolResult(
		data=data,
		summary=f"{name}: {data['status']}" + (f", sent to {sent}" if sent else "")
		+ (f"; call {', '.join(data['not_reached'])}" if data.get("not_reached") else ""),
		docstatus_delta="0 → 0 (updated)",
	)

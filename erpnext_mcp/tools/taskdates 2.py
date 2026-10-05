# SPDX-License-Identifier: MIT
"""Start / due dates: the MCP tools. v0.236.0. See `erpnext_mcp.task_dates`."""

from __future__ import annotations

import frappe

from .. import compat, task_dates
from ..args import as_int, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult


def list_overdue_tasks(args: dict) -> ToolResult:
	"""Open tasks past their due date, oldest first."""
	company = resolve_company(as_str(args, "company")) if as_str(args, "company") else ""
	rows = task_dates.overdue(company, max(1, min(as_int(args, "limit", 200), 500)))
	return ToolResult(data={"tasks": rows, "count": len(rows)}, summary=f"{len(rows)} overdue task(s)")


def set_task_dates(args: dict) -> ToolResult:
	"""Change a task's start date, due date or 'starts after' note."""
	name = as_str(args, "task", required=True)
	if not frappe.db.exists("Farm Task", name):
		raise ToolError(f"no Farm Task {name!r}. Nothing was changed.")
	if not compat.has_field("Farm Task", "due_date"):
		raise ToolError("this site has not migrated to v0.236.0 (no due_date on Farm Task). Nothing was changed.")
	doc = frappe.get_doc("Farm Task", name)
	before = task_dates.describe(dict(doc.as_dict()))
	merged = {field: args.get(field, doc.get(field)) for field in task_dates.FIELDS}
	try:
		task_dates.apply(doc, merged)
	except ValueError as exc:
		raise ToolError(f"{exc} Nothing was changed.") from None
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	after = task_dates.describe(dict(doc.as_dict()))
	return ToolResult(
		data={"task": name, "before": before, "after": after},
		summary=f"{name}: start {after['start_date'] or '—'}, due {after['due_date'] or '—'}",
		docstatus_delta="0 → 0 (updated)",
	)

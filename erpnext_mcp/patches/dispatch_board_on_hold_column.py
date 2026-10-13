# SPDX-License-Identifier: MIT
"""Add the On Hold column to an existing Farm Task Dispatch board. v0.324.0.

`install_dispatch_board` leaves an existing board exactly as an operator arranged it, so a board built before
v0.324.0 has no column for the new state and its held cards would not show. This appends one column for that one
new state, if it is missing — nothing is reordered, renamed or removed. Idempotent; never raises.
"""

from __future__ import annotations

import frappe

from erpnext_mcp import compat, dashboard

COLUMN = "On Hold"


def execute() -> None:
	try:
		if not compat.doctype_exists(dashboard.KANBAN) or not frappe.db.exists(dashboard.KANBAN,
		                                                                        dashboard.DISPATCH_BOARD_NAME):
			return
		doc = frappe.get_doc(dashboard.KANBAN, dashboard.DISPATCH_BOARD_NAME)
		if any(str(row.get("column_name")) == COLUMN for row in (doc.get("columns") or [])):
			return
		row = {"column_name": COLUMN}
		indicator = dashboard._select_value(dashboard.KANBAN_COLUMN, "indicator", "gray")
		if indicator:
			row["indicator"] = indicator
		status = dashboard._select_value(dashboard.KANBAN_COLUMN, "status", "Active")
		if status:
			row["status"] = status
		doc.append("columns", row)
		doc.save(ignore_permissions=True)
		print("erpnext_mcp: Farm Task Dispatch board — added the On Hold column.")
	except Exception:  # noqa: BLE001 — a board that cannot take the column still works through list_dispatch_board
		frappe.log_error(title="dispatch board On Hold column", message=frappe.get_traceback())

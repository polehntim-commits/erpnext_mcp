# SPDX-License-Identifier: MIT
"""Desk method: turn one MCP tool switch on for N minutes. v0.217.0. System Manager only."""

from __future__ import annotations

import frappe

from .. import switch_timer, switch_windows
from ..errors import ToolError


@frappe.whitelist(methods=["POST"])
def enable_for(tool=None, minutes=None) -> dict:
	frappe.only_for("System Manager")
	try:
		return switch_timer.enable_for(tool, minutes, str(frappe.session.user or ""))
	except ToolError as exc:
		frappe.throw(str(exc), frappe.ValidationError)


@frappe.whitelist(methods=["POST"])
def open_window(group=None, minutes=None) -> dict:
	"""v0.224.0. A payroll / bookkeeping window. docs/design/payroll_windows.md §1."""
	frappe.only_for("System Manager")
	try:
		return switch_windows.open_window(group, minutes, str(frappe.session.user or ""))
	except ToolError as exc:
		frappe.throw(str(exc), frappe.ValidationError)


@frappe.whitelist(methods=["POST", "GET"])
def window_groups() -> dict:
	frappe.only_for("System Manager")
	return {"groups": switch_windows.groups(), "default_minutes": switch_windows.DEFAULT_MINUTES}

# SPDX-License-Identifier: MIT
"""Desk method: turn one MCP tool switch on for N minutes. v0.217.0. System Manager only."""

from __future__ import annotations

import frappe

from .. import switch_timer
from ..errors import ToolError


@frappe.whitelist(methods=["POST"])
def enable_for(tool=None, minutes=None) -> dict:
	frappe.only_for("System Manager")
	try:
		return switch_timer.enable_for(tool, minutes, str(frappe.session.user or ""))
	except ToolError as exc:
		frappe.throw(str(exc), frappe.ValidationError)

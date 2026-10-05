# SPDX-License-Identifier: MIT
"""Degree days: the MCP tool. v0.249.0. See `erpnext_mcp.degree_days`."""

from __future__ import annotations

import frappe

from .. import degree_days
from ..args import as_bool, as_str
from ..errors import ToolError
from ..result import ToolResult


def get_degree_days(args: dict) -> ToolResult:
	block = as_str(args, "block", required=True)
	if not frappe.db.exists("Field", block):
		raise ToolError(f"no Field {block!r}.")
	data = degree_days.for_block(block, as_str(args, "crop"), as_str(args, "as_of"))
	if not as_bool(args, "with_series", False):
		data.pop("series", None)
	if not data.get("available"):
		return ToolResult(data=data, summary=f"{block}: no degree days — {data['note']}")
	estimate = f", about BBCH {data['estimated_bbch']}" if data.get("estimated_bbch") else ""
	return ToolResult(data=data, summary=f"{block}: {data['gdd_season']:g} degree days since {data['biofix']}{estimate}")

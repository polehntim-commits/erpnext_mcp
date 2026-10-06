# SPDX-License-Identifier: MIT
"""The MCP System User plan: the MCP half. v0.257.0. See `erpnext_mcp.mcp_user_plan`."""

from __future__ import annotations

from .. import mcp_user_plan
from ..args import as_str
from ..result import ToolResult


def plan_mcp_system_user(args: dict) -> ToolResult:
	"""Read-only: the roles the enabled tools need, a dry run for a candidate, and the Desk steps."""
	candidate = as_str(args, "candidate")
	data = mcp_user_plan.plan(candidate)
	current = data["current"]
	if candidate:
		check = data["candidate"]
		verdict = ("ready" if check.get("ready") else
		           ("does not exist yet" if not check.get("exists") else
		            f"{len(check.get('gaps') or [])} gap(s), {len(check.get('problems') or [])} problem(s)"))
		summary = f"candidate {candidate}: {verdict}; recommended roles: {', '.join(data['recommended_roles']) or 'none'}"
	else:
		summary = (f"MCP runs as {current['runs_as']}" + ("" if current["dedicated"] else " (not a dedicated user)")
		           + f"; recommended roles: {', '.join(data['recommended_roles']) or 'none'}")
	return ToolResult(data=data, summary=summary)

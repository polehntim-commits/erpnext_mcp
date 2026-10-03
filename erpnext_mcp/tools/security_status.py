# SPDX-License-Identifier: MIT
"""get_security_status and list_mcp_switches — read tools. v0.217.0.

docs/design/security_status_and_alerts.md §1–§2.
"""

from __future__ import annotations

from .. import security_status, switch_catalog
from ..args import as_bool, as_str
from ..result import ToolResult

SWITCH_ARGUMENTS = ("tier", "enabled", "contains")
STATUS_ARGUMENTS = ("probe_public",)


def list_mcp_switches(args: dict) -> ToolResult:
	"""Read-only. Every tool switch: on or off, its danger tier, any time limit."""
	rows = switch_catalog.catalogue()
	wanted_tier = as_str(args, "tier").strip().lower()
	contains = as_str(args, "contains").strip().lower()
	enabled = args.get("enabled")
	shown = [
		row
		for row in rows
		if (not wanted_tier or row["tier"] == wanted_tier)
		and (not contains or contains in row["tool"])
		and (enabled in (None, "") or bool(row["enabled"]) == as_bool(args, "enabled"))
	]
	shown.sort(key=lambda row: (switch_catalog.TIERS.index(row["tier"]), row["tool"]))
	summary = switch_catalog.summary(rows)
	return ToolResult(
		data={"count": len(shown), "switches": shown, **summary},
		summary=(
			f"{len(shown)} switch(es); {len(summary['dangerous_enabled'])} dangerous switch(es) on"
			+ (f": {', '.join(summary['dangerous_enabled'][:8])}" if summary["dangerous_enabled"] else "")
		),
	)


def get_security_status(args: dict) -> ToolResult:
	"""Read-only. One scored checklist of this site's security posture."""
	report = security_status.report(probe_public=as_bool(args, "probe_public", False))
	return ToolResult(data=report, summary=report["summary"])

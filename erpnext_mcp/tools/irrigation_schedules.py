# SPDX-License-Identifier: MIT
"""The irrigation schedule: the MCP tools. v0.248.0. See `erpnext_mcp.irrigation_schedule`."""

from __future__ import annotations

import datetime

import frappe

from .. import irrigation_schedule
from ..args import as_date, as_str
from ..errors import ToolError
from ..result import ToolResult


def _zone(args: dict) -> str:
	if not irrigation_schedule.installed():
		raise ToolError("this site has not migrated to v0.248.0 (no schedule on Irrigation Zone).")
	zone = as_str(args, "zone", required=True)
	if not frappe.db.exists(irrigation_schedule.ZONE, zone):
		raise ToolError(f"no Irrigation Zone {zone!r}.")
	return zone


def set_irrigation_schedule(args: dict) -> ToolResult:
	"""Replace a zone's schedule (the whole list; an empty list clears it)."""
	zone = _zone(args)
	rows = args.get("schedule")
	if not isinstance(rows, list):
		raise ToolError("schedule is a list of {valve, days, start_time, minutes, …}. Nothing was changed.")
	try:
		data = irrigation_schedule.set_schedule(zone, rows)
	except ValueError as exc:
		raise ToolError(f"{exc} Nothing was changed.") from None
	return ToolResult(
		data={"zone": zone, "schedule": data, "raising_tasks": irrigation_schedule.enabled()},
		summary=f"{zone}: {len(data)} scheduled set(s)",
		docstatus_delta="0 → 0 (updated)",
	)


def get_irrigation_schedule(args: dict) -> ToolResult:
	"""The plan for a window and, for days already gone, what the valves actually ran."""
	zone = _zone(args)
	today = datetime.date.fromisoformat(str(frappe.utils.today())[:10])
	start = datetime.date.fromisoformat(str(as_date(args, "from") or (today - datetime.timedelta(days=today.weekday()))))
	end = datetime.date.fromisoformat(str(as_date(args, "to") or (start + datetime.timedelta(days=6))))
	if end < start or (end - start).days > 92:
		raise ToolError("from / to: a window of up to 93 days, from before to.")
	data = irrigation_schedule.compare(zone, start, end)
	data["schedule"] = irrigation_schedule.lines(zone)
	totals = data["totals"]
	return ToolResult(
		data=data,
		summary=(f"{zone} {data['from']}–{data['to']}: planned {data['planned_minutes']} min, ran {data['ran_minutes']} min"
		         f" ({totals['done']} done, {totals['short']} short, {totals['missed']} missed, {totals['extra']} extra)"),
	)

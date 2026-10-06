# SPDX-License-Identifier: MIT
"""Go / Hold: the MCP tools. v0.240.0. See `erpnext_mcp.go_hold`."""

from __future__ import annotations

import frappe

from .. import go_hold
from ..args import as_int, as_str
from ..errors import ToolError
from ..result import ToolResult


def _task(args: dict) -> str:
	name = as_str(args, "task", required=True)
	if not frappe.db.exists("Farm Task", name):
		raise ToolError(f"no Farm Task {name!r}.")
	if not go_hold.installed():
		raise ToolError("this site has not migrated to v0.240.0 (no go_hold on Farm Task).")
	return name


def check_go_hold(args: dict) -> ToolResult:
	"""Judge one task now against every Work Timing rule that speaks to it. Writes nothing."""
	name = _task(args)
	language = as_str(args, "language") or "en"
	task = dict(frappe.get_doc("Farm Task", name).as_dict())
	verdict = go_hold.evaluate(task, language, as_str(args, "as_of"))
	data = {
		"task": name,
		"status": verdict["status"] or "No rule",
		"reasons": verdict["reasons"],
		"enforced_hold": verdict["enforced_hold"],
		"rules": verdict["rules"],
		"recorded": go_hold.describe(task),
		"written": False,
	}
	if not verdict["rules"]:
		data["note"] = "No live Work Timing rule speaks to this task (enable a preset in the Desk to start)."
	return ToolResult(data=data, summary=f"{name}: {data['status']}")


def override_hold(args: dict) -> ToolResult:
	"""A supervisor lets a held task start today, with a reason (decision 17)."""
	name = _task(args)
	user = frappe.session.user
	try:
		entry = go_hold.override(name, as_str(args, "reason"), user, as_str(args, "employee"))
	except (ValueError, PermissionError) as exc:
		raise ToolError(f"{exc} Nothing was changed.") from None
	return ToolResult(
		data={"task": name, "override": entry, "note": "Recorded on the task's Go / Hold log; it lapses at midnight."},
		summary=f"{name}: Hold overridden for today by {user}",
		docstatus_delta="0 → 0 (updated)",
	)


def get_forecast_verification(args: dict) -> ToolResult:
	"""v0.253.0 (decision 21). Weather-gated work scored against the rain that actually fell."""
	from .. import weather_verify

	days = as_int(args, "days", 90)
	if not 1 <= days <= 730:
		raise ToolError("days is 1 to 730.")
	data = weather_verify.summary(days, as_str(args, "rule_id"), as_str(args, "block"), as_str(args, "company"))
	if not data["tasks_checked"]:
		data["note"] = ("Nothing checked yet. The archive check (04:45 daily) scores completed tasks that started "
		                "under a Go / Hold rule reading the rain forecast, once the rule's window has passed.")
	return ToolResult(
		data=data,
		summary=(f"{data['tasks_checked']} task(s) checked, rained after {data['rained']}; Brier {data['brier']}"
		         if data["tasks_checked"] else "no archive checks yet"),
	)


def list_suggested_tasks(args: dict) -> ToolResult:
	"""v0.242.0. Open tasks the conditions favour today, window-closing first (decision 14)."""
	from .. import suggested_tasks
	from ..args import as_int, resolve_company

	company = resolve_company(as_str(args, "company")) if as_str(args, "company") else ""
	rows = suggested_tasks.suggestions(company, as_str(args, "worker"), as_str(args, "location"),
	                                   as_int(args, "limit", 50))
	closing = sum(1 for r in rows if r["kind"] == suggested_tasks.CLOSING)
	return ToolResult(
		data={"suggestions": rows, "count": len(rows), "window_closing": closing,
		      "note": "Existing open tasks only — managers create the work (decision 14). Nothing is written."},
		summary=f"{len(rows)} suggested task(s)" + (f", {closing} with the window closing tomorrow" if closing else ""),
	)

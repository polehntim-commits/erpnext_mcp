# SPDX-License-Identifier: MIT
"""The shop queue — the MCP side. v0.273.0 (docs/contracts/shop_queue_v0_273.yaml).

Two reads (the backlog with its suggestions; a day's weather verdict and shop list) and one write, OFF until switched
on (turn a suggestion — or anything — into a shop task from a template).
"""

from __future__ import annotations

import frappe

from .. import shop_queue
from ..args import as_int, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult


def _actor() -> str:
	from .. import security

	return security.caller_identity() or str(getattr(frappe.session, "user", "") or "")


def list_shop_backlog(args: dict) -> ToolResult:
	company = resolve_company(as_str(args, "company"), required=True)
	items = shop_queue.backlog(company)
	ideas = shop_queue.suggestions(company)
	minutes = sum(i["minutes"] for i in items)
	return ToolResult(data={"company": company, "items": items, "count": len(items), "hours": round(minutes / 60, 1),
	                        "suggestions": ideas},
	                  summary=f"{len(items)} shop job(s), about {minutes / 60:.1f} h; {len(ideas)} suggestion(s)")


def get_weather_day_plan(args: dict) -> ToolResult:
	company = resolve_company(as_str(args, "company"), required=True)
	plan = shop_queue.day_plan(company, as_str(args, "date"), as_int(args, "people", 1))
	verdict = ("a shop day — " + ", ".join(plan["reasons"])) if plan["shop_day"] else "a working day outside"
	return ToolResult(data=plan, summary=f"{plan['date']}: {verdict}; {len(plan['would_do'])} job(s) fit "
	                                     f"{plan['hours']:g} h")


def add_shop_item(args: dict) -> ToolResult:
	company = resolve_company(as_str(args, "company"), required=True)
	try:
		data = shop_queue.add_item(company, as_str(args, "template", required=True), title=as_str(args, "title"),
		                           asset=as_str(args, "asset"), notes=as_str(args, "notes"), actor=_actor())
	except ValueError as exc:
		raise ToolError(f"{exc} Nothing was created.") from None
	return ToolResult(data=data, summary=f"shop task {data['task']} from {data['template']}",
	                  docstatus_delta="none → 0 (created)")

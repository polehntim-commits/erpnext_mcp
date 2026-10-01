# SPDX-License-Identifier: MIT
"""The card print queue over MCP. v0.208.0. docs/design/card_print_queue.md §4.5.

Five tools over `card_print`: list jobs and stations (read), and request, cancel
and retry (write, default off). The requester is the caller's own identity where
the call carries one, else the account this app acts as — and that account needs
the Card Print Requester role like anybody else.
"""

from __future__ import annotations

import frappe

from .. import card_print, roles, security
from ..args import as_bool, as_str
from ..errors import ToolError
from ..result import ToolResult


def _actor() -> str:
	return security.caller_identity() or str(getattr(frappe.session, "user", "") or "")


def _companies(actor: str):
	return roles.companies_for(actor) or None


def _run(function, *args, **kwargs):
	try:
		return function(*args, **kwargs)
	except card_print.CardPrintError as exc:
		raise ToolError(str(exc)) from exc


def request_card_print(args: dict) -> ToolResult:
	actor = _actor()
	allowed = _companies(actor)

	def may_read(doctype, name, company):
		return card_print.in_scope(company, allowed)

	data = _run(
		card_print.request,
		actor,
		as_str(args, "job_type", required=True),
		as_str(args, "reference_name", required=True),
		as_str(args, "client_request_id", required=True),
		copies=args.get("copies"),
		sides=as_str(args, "sides"),
		reprint_reason=as_str(args, "reprint_reason"),
		requested_from="API",
		may_read=may_read,
	)
	job = data["job"]
	verb = (
		"queued" if data["created"] else ("already queued" if data["already_queued"] else "already requested")
	)
	return ToolResult(
		data=data,
		summary=f"{job['name']}: {job['job_type']} for {job['reference_title']} {verb}",
		docstatus_delta="none → 0 (draft)" if data["created"] else "",
	)


def list_card_print_jobs(args: dict) -> ToolResult:
	actor = _actor()
	data = _run(
		card_print.list_jobs,
		actor,
		_companies(actor),
		as_str(args, "status"),
		as_bool(args, "mine_only", False),
		as_str(args, "reference_name"),
		args.get("limit") or 50,
	)
	return ToolResult(data=data, summary=f"{data['count']} print job(s)")


def list_card_print_stations(args: dict) -> ToolResult:
	rows = [
		{
			**card_print.station_state(s),
			"priority": s.get("priority"),
			"companies": card_print.lines(s.get("companies")),
			"agent_version": s.get("agent_version"),
		}
		for s in card_print.stations()
	]
	return ToolResult(data={"stations": rows, "count": len(rows)}, summary=f"{len(rows)} print station(s)")


def cancel_card_print_job(args: dict) -> ToolResult:
	actor = _actor()
	data = _run(card_print.cancel, as_str(args, "name", required=True), actor, _companies(actor))
	return ToolResult(
		data=data, summary=f"{data['job']['name']}: Cancelled", docstatus_delta="0 → 0 (updated)"
	)


def mark_card_print_job(args: dict) -> ToolResult:
	"""v0.210.0. A card printed by hand: mark its job Printed, or Failed with the reason."""
	actor = _actor()
	printed = as_bool(args, "printed", True)
	data = _run(
		card_print.mark,
		as_str(args, "name", required=True),
		actor,
		_companies(actor),
		printed,
		as_str(args, "error"),
	)
	return ToolResult(
		data=data,
		summary=f"{data['job']['name']}: {data['job']['status']}",
		docstatus_delta="0 → 0 (updated)",
	)


def retry_card_print_job(args: dict) -> ToolResult:
	actor = _actor()
	data = _run(card_print.retry, as_str(args, "name", required=True), actor, _companies(actor))
	return ToolResult(
		data=data, summary=f"{data['job']['name']}: back to Queued", docstatus_delta="0 → 0 (updated)"
	)


def download_card_pdf(args: dict) -> ToolResult:
	"""v0.209.0. Render one card (front and back) and record it as Downloaded."""
	actor = _actor()
	allowed = _companies(actor)

	def may_read(doctype, name, company):
		return card_print.in_scope(company, allowed)

	doc, pdf, warnings = _run(
		card_print.download,
		actor,
		as_str(args, "job_type", required=True),
		as_str(args, "reference_name", required=True),
		as_str(args, "reprint_reason"),
		may_read,
	)
	return ToolResult(
		data={
			"job": card_print.describe(doc, actor),
			"file_url": doc.get("artwork"),
			"file_name": f"{doc.name}.pdf",
			"bytes": len(pdf),
			"warnings": warnings,
			"print_settings": "Paper Size CR80 / ISO 7810, 100 %, no fit, Auto Rotate off",
		},
		summary=f"{doc.name}: card PDF for {doc.reference_title} ({len(pdf)} bytes), recorded as Downloaded",
		docstatus_delta="none → 0 (draft)",
	)


def request_card_back(args: dict) -> ToolResult:
	actor = _actor()
	data = _run(
		card_print.request_back,
		as_str(args, "name", required=True),
		actor,
		_companies(actor),
		as_str(args, "client_request_id"),
		"API",
	)
	return ToolResult(
		data=data,
		summary=f"{data['job']['name']}: the back of {data['job']['reference_title']} queued",
		docstatus_delta="none → 0 (draft)" if data["created"] else "",
	)

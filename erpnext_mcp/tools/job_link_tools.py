# SPDX-License-Identifier: MIT
"""Contractor and supplier jobs and their share links — the MCP side. v0.271.0 (docs/contracts/job_links_v0_271.yaml).

Writes are OFF until switched on. Publishing a job template is the generic config tools / the Desk. Sharing a link
stays off per company until `job_links_enabled` is turned on for it.
"""

from __future__ import annotations

import frappe

from .. import job_links
from ..args import as_bool, as_int, as_str
from ..errors import ToolError
from ..result import ToolResult


def _actor() -> str:
	from .. import security

	return security.caller_identity() or str(getattr(frappe.session, "user", "") or "")


def _wrap(fn):
	try:
		return fn()
	except job_links.JobError as exc:
		raise ToolError(f"{exc} Nothing was changed.") from None


def _point(raw):
	if not raw:
		return None
	if isinstance(raw, (list, tuple)) and len(raw) == 2:
		return float(raw[0]), float(raw[1])
	parts = [p.strip() for p in str(raw).split(",")]
	if len(parts) != 2:
		raise ToolError("a point is 'lat, lon'.")
	return float(parts[0]), float(parts[1])


def create_contractor_job(args: dict) -> ToolResult:
	"""A Draft job from a template — fields by name or alias; prep tasks raised per block."""
	fields = args.get("fields") or []
	if isinstance(fields, str):
		fields = [f.strip() for f in fields.replace("+", ",").split(",") if f.strip()]
	name = _wrap(lambda: job_links.create_job(
		as_str(args, "template", required=True), as_str(args, "company", required=True), fields=fields,
		supplier=as_str(args, "supplier"), purchase_order=as_str(args, "purchase_order"), title=as_str(args, "title"),
		scope=as_str(args, "scope"), start_date=as_str(args, "start_date"), end_date=as_str(args, "end_date"),
		contact_name=as_str(args, "contact_name"), contact_phone=as_str(args, "contact_phone"),
		show_contact_phone=as_bool(args, "show_contact_phone", False), entrance=_point(args.get("entrance")),
		gate_notes=as_str(args, "gate_notes"), delivery_spot=_point(args.get("delivery_spot")),
		delivery_spot_label=as_str(args, "delivery_spot_label"), actor=_actor()))
	state = job_links.readiness(name)
	return ToolResult(data={"job": name, **state},
	                  summary=f"{name} drafted; {len(state['prep_tasks'])} prep task(s)"
	                  + ("" if state["sharing_enabled"] else "; sharing is off for this company"),
	                  docstatus_delta="0 → 0 (created)")


def get_contractor_job(args: dict) -> ToolResult:
	"""A job: readiness, what its page shows, and its links (with views)."""
	job = as_str(args, "job", required=True)
	if not frappe.db.exists(job_links.JOB, job):
		raise ToolError(f"no Contractor Job {job!r}.")
	doc = frappe.get_doc(job_links.JOB, job)
	data = {**job_links.readiness(job), "page": job_links.page_data(job), "links": job_links.links_of(job),
	        "events": [{"event": e.get("event"), "at": str(e.get("at") or ""), "note": e.get("note"), "name_given": e.get("name_given"), "file": e.get("file")}
	                   for e in doc.get("events") or []]}
	return ToolResult(data=data, summary=f"{job}: {doc.status}")


def mark_contractor_job_ready(args: dict) -> ToolResult:
	data = _wrap(lambda: job_links.mark_ready(as_str(args, "job", required=True), _actor(), as_str(args, "override_reason")))
	return ToolResult(data=data, summary=f"{data['job']} ready", docstatus_delta="0 → 0 (updated)")


def create_job_link(args: dict) -> ToolResult:
	data = _wrap(lambda: job_links.issue_link(as_str(args, "job", required=True), _actor(), as_int(args, "days")))
	return ToolResult(data=data, summary=f"link for {data['job']}, live until {data['expires_at']} — shown once",
	                  docstatus_delta="0 → 0 (created)")


def extend_job_link(args: dict) -> ToolResult:
	data = _wrap(lambda: job_links.extend(as_str(args, "link", required=True), _actor(), as_int(args, "days", 7),
	                                      as_str(args, "until")))
	return ToolResult(data=data, summary=f"link {data['link']} now live until {data['expires_at']}",
	                  docstatus_delta="0 → 0 (updated)")


def revoke_job_link(args: dict) -> ToolResult:
	data = _wrap(lambda: job_links.revoke(as_str(args, "link", required=True), _actor(), as_str(args, "reason")))
	return ToolResult(data=data, summary=f"link {data['link']} revoked", docstatus_delta="0 → 0 (updated)")


def list_job_links(args: dict) -> ToolResult:
	rows = job_links.links_of(as_str(args, "job"), as_str(args, "company"), as_bool(args, "include_views", True))
	return ToolResult(data={"links": rows, "count": len(rows)}, summary=f"{len(rows)} link(s)")


def close_contractor_job(args: dict) -> ToolResult:
	data = _wrap(lambda: job_links.close_job(as_str(args, "job", required=True), _actor(), as_bool(args, "cancel", False)))
	return ToolResult(data=data, summary=f"{data['job']} {data['status']}; its links are expired",
	                  docstatus_delta="0 → 0 (updated)")

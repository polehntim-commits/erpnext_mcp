# SPDX-License-Identifier: MIT
"""The card print queue for the Desk and for the print agent. v0.208.0.

docs/design/card_print_queue.md §4. Called at
`/api/method/erpnext_mcp.api.card_print.<name>` with a Frappe session (Desk
buttons) or `Authorization: token <key>:<secret>` (the Mac agent, whose user
holds only Card Print Station). The phone does not come through here — it uses
the guarded routes in `api/mobile.py`. Every rule lives in `card_print`.

`requested_by` and `claimed_by` are ALWAYS `frappe.session.user`; nothing here
reads an identity from a request body.
"""

import frappe

from erpnext_mcp import card_print


def _user() -> str:
	return str(frappe.session.user)


def _may_read(doctype: str, name: str, company: str) -> bool:
	"""Read permission on the record AND its company among the caller's (Amendment 4 §D4)."""
	return bool(frappe.has_permission(doctype, "read", doc=name)) and card_print.in_scope(
		company, _companies()
	)


def _companies():
	from erpnext_mcp import roles

	return roles.companies_for(_user()) or None


# ── requester ───────────────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
def request_card_print(
	job_type=None, reference_name=None, copies=1, sides=None, client_request_id=None, reprint_reason=None
):
	return card_print.desk(
		card_print.request,
		_user(),
		str(job_type or ""),
		str(reference_name or ""),
		str(client_request_id or ""),
		copies=copies,
		sides=str(sides or ""),
		reprint_reason=str(reprint_reason or ""),
		requested_from="Desk",
		may_read=_may_read,
	)


@frappe.whitelist()
def list_card_print_jobs(status=None, mine_only=1, reference_name=None, limit=50):
	mine = str(mine_only).strip().lower() not in ("0", "false", "no", "")
	return card_print.desk(
		card_print.list_jobs, _user(), _companies(), str(status or ""), mine, str(reference_name or ""), limit
	)


@frappe.whitelist(methods=["POST"])
def cancel_card_print_job(name=None):
	return card_print.desk(card_print.cancel, str(name or ""), _user(), _companies())


@frappe.whitelist(methods=["POST"])
def retry_card_print_job(name=None):
	return card_print.desk(card_print.retry, str(name or ""), _user(), _companies())


@frappe.whitelist(methods=["POST"])
def mark_card_print_job(name=None, printed=1, error=None):
	"""A person closes a job they printed by hand: Printed, or Failed with what went wrong."""
	return card_print.desk(card_print.mark, str(name or ""), _user(), _companies(), printed, str(error or ""))


# ── the print station ───────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
def claim_next_card_print_job(
	print_station=None, printer_state=None, printer_message=None, agent_version=None
):
	return card_print.desk(
		card_print.claim,
		str(print_station or ""),
		_user(),
		str(printer_state or ""),
		str(printer_message or ""),
		str(agent_version or ""),
	)


@frappe.whitelist(methods=["POST"])
def complete_card_print_job(name=None, success=None, error=None, retryable=0, cups_job=None):
	return card_print.desk(
		card_print.complete,
		str(name or ""),
		_user(),
		success,
		str(error or ""),
		retryable,
		str(cups_job or ""),
	)


@frappe.whitelist(methods=["POST"])
def card_print_heartbeat(print_station=None, printer_state=None, printer_message=None, agent_version=None):
	return card_print.desk(
		card_print.heartbeat,
		str(print_station or ""),
		_user(),
		str(printer_state or ""),
		str(printer_message or ""),
		str(agent_version or ""),
	)


# ── v0.209.0: preview, download and the back of a simplex card ───────────────
@frappe.whitelist()
def preview_card(job_type=None, reference_name=None):
	"""The card as SVG (front and back), the station's state and any artwork warning.

	What the Desk dialog shows before anything is queued or downloaded."""
	return card_print.desk(
		card_print.preview, _user(), str(job_type or ""), str(reference_name or ""), _may_read
	)


@frappe.whitelist(methods=["POST"])
def download_card_pdf(job_type=None, reference_name=None, reprint_reason=None):
	"""One card-sized PDF (front and back pages) to print by hand — and a record of it.

	Returns the private File's URL; the Desk opens it. Print from Preview with
	Paper Size CR80, 100 %, no fit, Auto Rotate off."""
	doc, pdf, warnings = card_print.desk(
		card_print.download,
		_user(),
		str(job_type or ""),
		str(reference_name or ""),
		str(reprint_reason or ""),
		_may_read,
	)
	return {
		"job": card_print.describe(doc, _user()),
		"file_url": doc.get("artwork"),
		"file_name": f"{doc.name}.pdf",
		"bytes": len(pdf),
		"warnings": warnings,
	}


@frappe.whitelist(methods=["POST"])
def request_card_back(name=None, client_request_id=None):
	return card_print.desk(
		card_print.request_back, str(name or ""), _user(), _companies(), str(client_request_id or "")
	)

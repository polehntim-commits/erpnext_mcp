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
	return bool(frappe.has_permission(doctype, "read", doc=name))


def _companies():
	from erpnext_mcp import roles

	return roles.companies_for(_user()) or None


# ── requester ───────────────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
def request_card_print(
	job_type=None, reference_name=None, copies=1, sides="Single", client_request_id=None, reprint_reason=None
):
	return card_print.desk(
		card_print.request,
		_user(),
		str(job_type or ""),
		str(reference_name or ""),
		str(client_request_id or ""),
		copies=copies,
		sides=str(sides or "Single"),
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

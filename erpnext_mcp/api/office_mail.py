# SPDX-License-Identifier: MIT
"""Desk: review, approve and send an office@ reply. v0.222.0.

docs/design/office_reply_drafts.md §3. The person logged into the Desk is the
approver; every check of `mail_drafts.approve` applies (role for the class,
Frappe's `email` permission on the linked record, explicit attachments, the
financial confirmation).
"""

from __future__ import annotations

import json

import frappe

from .. import mail_drafts


def _list(raw) -> list:
	if isinstance(raw, list):
		return raw
	try:
		value = json.loads(raw or "[]")
	except (TypeError, ValueError):
		return []
	return value if isinstance(value, list) else []


def _throw(exc) -> None:
	frappe.throw(str(exc), frappe.ValidationError)


@frappe.whitelist(methods=["POST"])
def approve(name=None, text=None, attachments=None, confirm_financial_details=0) -> dict:
	try:
		return mail_drafts.approve(
			str(name or ""),
			str(frappe.session.user or ""),
			"desk",
			text=text,
			attachments=_list(attachments),
			confirm_financial_details=bool(int(confirm_financial_details or 0)),
		)
	except mail_drafts.Refused as exc:
		_throw(exc)


@frappe.whitelist(methods=["POST"])
def discard(name=None, reason=None) -> dict:
	try:
		return mail_drafts.discard(str(name or ""), str(frappe.session.user or ""), str(reason or ""))
	except mail_drafts.Refused as exc:
		_throw(exc)


@frappe.whitelist(methods=["POST", "GET"])
def available_attachments(name=None) -> list:
	"""Files on the linked record a reply may carry (each must be ticked)."""
	if not frappe.has_permission("Office Mail", "read", doc=str(name or "")):
		frappe.throw("Not permitted", frappe.PermissionError)
	try:
		return mail_drafts._available_files(mail_drafts._row(str(name or "")))
	except mail_drafts.Refused as exc:
		_throw(exc)

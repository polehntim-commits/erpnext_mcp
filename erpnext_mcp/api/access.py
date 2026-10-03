# SPDX-License-Identifier: MIT
"""Desk: approve or deny an access request. v0.219.0. System Manager only.

docs/design/device_client_enrollment.md §6.2–§6.3. The fallback to a manager's
phone; a person logged into the Desk decides. No MCP tool can. v0.220.0: the
code may name an AI client's OAuth request; then `profile` (read, read_farm) or
`scopes` say what it gets, never more than it asked for.
"""

from __future__ import annotations

import frappe

from .. import device_enrollment, device_keys


@frappe.whitelist(methods=["POST"])
def decide(code=None, user=None, decision=None, profile=None, scopes=None) -> dict:
	frappe.only_for("System Manager")
	if not device_keys.any_approval_enabled():
		frappe.throw("Approving phones and AI clients is switched off on this farm.", frappe.ValidationError)
	try:
		return device_keys.decide(
			str(frappe.session.user or ""),
			str(code or ""),
			str(user or ""),
			str(decision or ""),
			"desk",
			scopes=str(scopes or ""),
			profile=str(profile or ""),
		)
	except device_enrollment.EnrollmentRefused as exc:
		frappe.throw(str(exc), frappe.ValidationError)


@frappe.whitelist(methods=["POST"])
def peek(code=None) -> dict:
	"""What a code belongs to, before deciding. v0.220.0."""
	frappe.only_for("System Manager")
	if not device_keys.any_approval_enabled():
		frappe.throw("Approving phones and AI clients is switched off on this farm.", frappe.ValidationError)
	try:
		return device_keys.peek(str(frappe.session.user or ""), str(code or ""))
	except device_enrollment.EnrollmentRefused as exc:
		frappe.throw(str(exc), frappe.ValidationError)


@frappe.whitelist(methods=["POST"])
def revoke_client(client=None, reason=None) -> dict:
	"""End an AI client from the Desk. v0.220.0."""
	from .. import oauth

	frappe.only_for("System Manager")
	try:
		return oauth.revoke_client(str(client or ""), str(frappe.session.user or ""), str(reason or ""))
	except device_enrollment.EnrollmentRefused as exc:
		frappe.throw(str(exc), frappe.ValidationError)

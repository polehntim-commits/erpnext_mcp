# SPDX-License-Identifier: MIT
"""Desk: approve or deny a new phone's request. v0.219.0. System Manager only.

docs/design/device_client_enrollment.md §6.2. The fallback to a manager's phone;
a person logged into the Desk decides. No MCP tool can.
"""

from __future__ import annotations

import frappe

from .. import device_enrollment, device_keys


@frappe.whitelist(methods=["POST"])
def decide(code=None, user=None, decision=None) -> dict:
	frappe.only_for("System Manager")
	if not device_keys.approval_enabled():
		frappe.throw("Phone approval is switched off on this farm.", frappe.ValidationError)
	try:
		return device_keys.decide(
			str(frappe.session.user or ""), str(code or ""), str(user or ""), str(decision or ""), "desk"
		)
	except device_enrollment.EnrollmentRefused as exc:
		frappe.throw(str(exc), frappe.ValidationError)

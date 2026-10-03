# SPDX-License-Identifier: MIT
"""Desk: approve, reject, or record a prenote return on a direct-deposit change. v0.225.0.

docs/design/direct_deposit_setup.md §1.6–1.8. HR Manager / System Manager; never
the employee themself. No MCP tool does any of this.
"""

from __future__ import annotations

import frappe

from .. import direct_deposit

ROLES = direct_deposit.APPROVER_ROLES


def _run(call):
	frappe.only_for(ROLES)
	try:
		return call()
	except direct_deposit.Refused as exc:
		frappe.throw(str(exc), frappe.ValidationError)


@frappe.whitelist(methods=["POST"])
def approve(account=None) -> dict:
	return _run(lambda: direct_deposit.approve(str(account or ""), str(frappe.session.user or "")))


@frappe.whitelist(methods=["POST"])
def reject(account=None, reason=None) -> dict:
	return _run(
		lambda: direct_deposit.reject(str(account or ""), str(frappe.session.user or ""), str(reason or ""))
	)


@frappe.whitelist(methods=["POST"])
def prenote_returned(account=None) -> dict:
	return _run(lambda: direct_deposit.prenote_returned(str(account or ""), str(frappe.session.user or "")))

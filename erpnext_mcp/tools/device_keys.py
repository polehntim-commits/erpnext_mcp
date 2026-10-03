# SPDX-License-Identifier: MIT
"""issue_enrollment_link, list_access_inventory, report_lost_device. v0.218.0.

v0.220.0: revoke_mcp_client, and `clients` in the inventory.

docs/design/device_client_enrollment.md §3, §7. The two writes ship OFF like
every mutating tool; `issue_enrollment_link` also needs `device_keys_enabled`.
"""

from __future__ import annotations

import base64

import frappe

from .. import device_enrollment, device_keys, oauth
from ..args import as_bool, as_int, as_str
from ..errors import ToolError
from ..render import qr
from ..result import ToolResult


def issue_enrollment_link(args: dict) -> ToolResult:
	"""A single-use, short-lived pickup link (and its QR) for one account's new phone."""
	user = as_str(args, "user", required=True).strip().lower()
	try:
		opened = device_keys.issue_link(
			user,
			device_name=as_str(args, "device_name"),
			minutes=as_int(args, "minutes", None),
			issued_by=str(frappe.session.user or ""),
		)
	except device_enrollment.EnrollmentRefused as exc:
		raise ToolError(str(exc)) from None
	data = {**opened}
	if qr.available():
		drawn = qr.render(opened["link"], error="M")
		data["qr_png_base64"] = base64.b64encode(drawn["png"]).decode("ascii")
	data["security_note"] = (
		f"Single use, dead at {opened['expires_at']} or on first use. It carries no credential: the "
		"phone makes its own keys and only their public halves reach the server."
	)
	return ToolResult(data=data, summary=f"Pickup link for {user}, valid {opened['minutes']} minute(s)")


def list_access_inventory(args: dict) -> ToolResult:
	"""Read-only. Every phone (and, from the OAuth phase, every MCP client)."""
	rows = device_keys.inventory(
		as_str(args, "user").strip().lower(), as_bool(args, "include_revoked", False)
	)
	legacy = [row for row in rows if row["legacy_secret"]]
	clients = oauth.clients(as_bool(args, "include_revoked", False))
	return ToolResult(
		data={
			"count": len(rows),
			"devices": rows,
			"clients": clients,
			"key_bound": sum(1 for row in rows if row["key_bound"]),
			"legacy_secret": len(legacy),
			"ready_to_disable_legacy_secrets": not legacy,
		},
		summary=f"{len(rows)} device(s); {len(legacy)} still on a legacy secret",
	)


def report_lost_device(args: dict) -> ToolResult:
	"""Revoke a lost phone (or every phone of a person), end its tokens, alert."""
	user = as_str(args, "user", required=True).strip().lower()
	device = as_str(args, "device").strip()
	try:
		answer = device_keys.report_lost(user, device, str(frappe.session.user or ""), as_str(args, "note"))
	except device_enrollment.EnrollmentRefused as exc:
		raise ToolError(str(exc)) from None
	return ToolResult(
		data=answer, summary=f"{len(answer['revoked_devices'])} device(s) of {user} revoked as lost"
	)


def revoke_mcp_client(args: dict) -> ToolResult:
	"""End an OAuth AI client and every token it holds. v0.220.0."""
	client = as_str(args, "client", required=True).strip()
	try:
		answer = oauth.revoke_client(client, str(frappe.session.user or ""), as_str(args, "reason"))
	except device_enrollment.EnrollmentRefused as exc:
		raise ToolError(str(exc)) from None
	return ToolResult(
		data=answer, summary=f"MCP client {client} revoked; {answer['tokens_revoked']} token(s) ended"
	)

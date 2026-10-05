# SPDX-License-Identifier: MIT
"""One-time upload links: the MCP half. v0.244.0. See `erpnext_mcp.upload_links`."""

from __future__ import annotations

import frappe

from .. import upload_links
from ..args import as_bool, as_int, as_str
from ..result import ToolResult
from . import mobile as mobile_tools


def _user() -> str:
	return str(getattr(frappe.session, "user", "") or "")


def request_upload_url(args: dict) -> ToolResult:
	"""Issue one link. The URL in the answer is the only copy of the token."""
	data = upload_links.issue(
		user=_user(),
		target_doctype=as_str(args, "doctype", required=True),
		target_name=as_str(args, "name", required=True),
		kinds=args.get("kinds"),
		max_bytes=args.get("max_bytes"),
		is_private=bool(as_bool(args, "is_private", True)),
		expected_sha256=as_str(args, "sha256"),
		expires_minutes=args.get("expires_minutes"),
		note=as_str(args, "note"),
		base_url=mobile_tools._mobile_base_url({}),
	)
	data["how"] = (
		"PUT the file's bytes to url (Content-Type of the file), or POST it as multipart/form-data with "
		"one file part. One file, once, before expires_at. The URL is the credential: share it only "
		"with whoever is sending the file."
	)
	return ToolResult(
		data=data,
		summary=f"upload link {data['upload_id']} for {data['target']['doctype']} {data['target']['name']}, "
		f"up to {data['max_bytes']} bytes, until {data['expires_at']}",
		docstatus_delta="none → 0 (Upload Link created)",
	)


def get_upload_status(args: dict) -> ToolResult:
	"""One link: its state and, once Done, the File it made."""
	row = upload_links.get(as_str(args, "upload_id", required=True))
	return ToolResult(data=row, summary=f"{row['upload_id']}: {row['status']}")


def list_upload_links(args: dict) -> ToolResult:
	"""Recent links, newest first, optionally one status or one target."""
	filters: dict = {}
	status = as_str(args, "status")
	if status:
		filters["status"] = status
	if as_str(args, "doctype"):
		filters["target_doctype"] = as_str(args, "doctype")
	if as_str(args, "name"):
		filters["target_name"] = as_str(args, "name")
	limit = max(1, min(as_int(args, "limit", 50), 200))
	rows = frappe.db.get_all(
		upload_links.DOCTYPE, filters=filters, fields=["*"], order_by="creation desc", limit=limit
	)
	links = [upload_links.describe(row) for row in rows]
	return ToolResult(data={"links": links, "count": len(links)}, summary=f"{len(links)} upload link(s)")


def revoke_upload_link(args: dict) -> ToolResult:
	"""Close an Open link before it is used. Irreversible."""
	row = upload_links.revoke(as_str(args, "upload_id", required=True), _user())
	return ToolResult(data=row, summary=f"revoked {row['upload_id']}", docstatus_delta="0 → 0 (Revoked)")

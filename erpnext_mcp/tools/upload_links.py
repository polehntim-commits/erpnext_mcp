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
		file_name=as_str(args, "file_name"),
		expected_size=args.get("expected_size"),
		company=as_str(args, "company"),
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


def _download_target(args: dict) -> str:
	"""v0.337.0. The File a download link is for: `file` (docname or file_url), or doctype + name (+ file_name), or a
	governance_document's attached file."""
	from ..errors import ToolError
	from . import governance

	if as_str(args, "file"):
		return as_str(args, "file")
	if as_str(args, "governance_document"):
		name = as_str(args, "governance_document")
		if not frappe.db.exists(governance.GOVERNANCE_DOCUMENT, name):
			raise ToolError(f"no Governance Document {name!r}. Nothing was issued.")
		url = frappe.db.get_value(governance.GOVERNANCE_DOCUMENT, name, "attached_file")
		found = frappe.db.get_value("File", {"file_url": url, "attached_to_doctype": governance.GOVERNANCE_DOCUMENT,
		                                     "attached_to_name": name}, "name") if url else None
		found = found or (frappe.db.get_value("File", {"file_url": url}, "name") if url else None)
		if not found:
			raise ToolError(f"Governance Document {name} has no attached file. Nothing was issued.")
		return str(found)
	doctype, name = as_str(args, "doctype"), as_str(args, "name")
	if not (doctype and name):
		raise ToolError("say which file: file (a File docname or its file_url), or doctype + name (+ file_name), or "
		                "governance_document. Nothing was issued.")
	filters = {"attached_to_doctype": doctype, "attached_to_name": name}
	if as_str(args, "file_name"):
		filters["file_name"] = as_str(args, "file_name")
	rows = frappe.db.get_all("File", filters=filters, fields=["name", "file_name"], order_by="creation desc", limit=20)
	if not rows:
		raise ToolError(f"no file{' called ' + repr(as_str(args, 'file_name')) if as_str(args, 'file_name') else ''} "
		                f"is attached to {doctype} {name}. list_attachments shows what is. Nothing was issued.")
	if len(rows) > 1 and not as_str(args, "file_name"):
		names = ", ".join(str(r.get("file_name")) for r in rows)
		raise ToolError(f"{doctype} {name} has {len(rows)} files ({names}); pass file_name. Nothing was issued.")
	return str(rows[0]["name"])


def get_attachment_download_link(args: dict) -> ToolResult:
	"""One short-lived link to one private File on the public host — the bytes leave as plain HTTP, not base64.

	v0.325.0; v0.337.0 to Tim's spec (make_download_link): always the public /farmops route, ttl_seconds (600 default,
	3600 max), single_use (default true), sha256 / mime / size in the answer, refused with no public URL.
	"""
	data = upload_links.issue_download(
		user=_user(),
		file=_download_target(args),
		ttl_seconds=args.get("ttl_seconds"),
		expires_minutes=args.get("expires_minutes"),
		single_use=bool(as_bool(args, "single_use", True)),
		note=as_str(args, "note"),
	)
	data["how"] = (
		"GET url before expires_at, no cookies or headers needed — e.g. `curl -fOJ '<url>'` (saves under the file's own "
		"name), then check its sha256 against this answer. "
		+ ("It works once: a second GET is 404. " if data["single_use"] else "It works until expires_at. ")
		+ "The URL is the credential; it is not shown again. revoke_upload_link with link_id closes it early."
	)
	return ToolResult(data=data, summary=f"download link {data['link_id']} for {data['file_name']} "
	                  f"({data['size_bytes']} bytes, sha256 {data['sha256'][:12]}), until {data['expires_at']}",
	                  docstatus_delta="none → 0 (Upload Link created, direction Download)")


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
	direction = as_str(args, "direction")
	if direction:  # v0.325.0: Upload or Download
		filters["direction"] = direction.capitalize()
	if as_bool(args, "active_only", False):  # v0.337.0: Open and not yet expired
		filters["status"] = upload_links.OPEN
		filters["expires_at"] = (">", upload_links._stamp(upload_links._now()))
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

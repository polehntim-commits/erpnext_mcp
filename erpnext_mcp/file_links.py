# SPDX-License-Identifier: MIT
"""File links on the Desk's own host. v0.325.0 — see `erpnext_mcp.upload_links` for the engine and the token rules.

`GET /api/method/erpnext_mcp.file_links.download?token=…` streams one File once, with Content-Disposition.
`PUT|POST /api/method/erpnext_mcp.file_links.upload?token=…` takes one file onto the document the link names.

These are served by ERPNext itself, so a link works wherever the Desk works — on this farm, over Tailscale — and is
not on the public Funnel, which publishes only /farmops. The sidecar's public routes are used instead only when an
operator sets File Links: Host to Public (Funnel).

GUEST-REACHABLE BY DESIGN: the token is the credential (32 random bytes, only its hash stored, single use, minutes
long). Every token that will not work — unknown, expired, used, revoked, wrong direction, feature off — answers the
same 404, and both routes are metered per address before the token is looked at.
"""

from __future__ import annotations

import mimetypes
from urllib.parse import quote

import frappe

from . import upload_links

NOT_FOUND = upload_links.NOT_FOUND
#: Requests per address per minute, counted before the token is looked at.
LIMIT_PER_MINUTE = 30


def _ip() -> str:
	return str(getattr(frappe.local, "request_ip", "") or "")


def _metered(kind: str) -> bool:
	from .api import guard

	try:
		return guard._count(f"file-link-{kind}:{_ip() or 'unknown'}", 60) > LIMIT_PER_MINUTE
	except Exception:  # noqa: BLE001 — a meter that cannot count does not shut the door
		return False


def _response(status: int, message: str):
	from werkzeug.wrappers import Response

	return Response(message, status=status, mimetype="text/plain")


def content_disposition(file_name: str) -> str:
	"""attachment; filename="…"; filename*=UTF-8''… — safe for any name, readable by curl -OJ and every browser."""
	ascii_name = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in file_name) or "download"
	return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(file_name)}"


@frappe.whitelist(allow_guest=True, methods=["GET"])
def download(token=None):
	"""Stream one File, once. 404 for any token that does not work; 429 when an address floods."""
	from werkzeug.wrappers import Response
	from werkzeug.wsgi import wrap_file

	if _metered("download"):
		return _response(429, "Too many requests. Wait a minute.")
	if not upload_links.enabled():
		return _response(404, NOT_FOUND)
	request = getattr(frappe.local, "request", None)
	agent = str(request.headers.get("User-Agent") or "") if request is not None else ""
	got = upload_links.open_download(str(token or ""), _ip(), agent)
	if not got:
		frappe.db.commit()  # `find` may have marked a link Expired
		return _response(404, NOT_FOUND)
	handle = open(got["path"], "rb")  # noqa: SIM115 — closed by the WSGI server when the stream ends
	body = wrap_file(request.environ, handle) if request is not None else handle
	mimetype = mimetypes.guess_type(got["file_name"])[0] or "application/octet-stream"
	response = Response(body, mimetype=mimetype, direct_passthrough=True)
	response.headers["Content-Disposition"] = content_disposition(got["file_name"])
	response.headers["Content-Length"] = str(got["size"])
	response.headers["Cache-Control"] = "no-store"
	return response


@frappe.whitelist(allow_guest=True, methods=["PUT", "POST"])
def upload(token=None):
	"""Take one file onto the linked document, once. Same checks as the sidecar's /farmops/api/upload route."""
	if _metered("upload"):
		return _response(429, "Too many requests. Wait a minute.")
	if not upload_links.enabled():
		return _response(404, NOT_FOUND)
	link = upload_links.find(str(token or ""))
	if link is None or link.get("direction") == upload_links.DOWNLOAD:
		frappe.db.commit()
		return _response(404, NOT_FOUND)
	request = frappe.local.request
	try:
		upload_links.claim(link["name"])
	except upload_links.UploadRefused as exc:
		return _response(exc.status, str(exc))
	upload_links.note_attempt(link["name"], _ip(), str(request.headers.get("User-Agent") or ""))
	frappe.db.commit()
	try:
		if request.method == "PUT":
			name = str(request.args.get("filename") or link.get("preset_file_name") or "")
			disposition = str(request.headers.get("Content-Disposition") or "")
			if not name and "filename=" in disposition:
				name = disposition.split("filename=", 1)[1].strip().strip('"')
			result = upload_links.receive(link, request.stream, declared_length=request.content_length, filename=name)
		else:
			parts = [part for part in request.files.values() if part and part.filename]
			if len(parts) != 1:
				raise upload_links.UploadRefused(400, "Send exactly one file part.")
			result = upload_links.receive(link, parts[0].stream, filename=parts[0].filename)
	except upload_links.UploadRefused as exc:
		frappe.db.rollback()
		upload_links.fail(link["name"], str(exc))
		return _response(exc.status, str(exc))
	upload_links._log("used", link["name"], link["upload_id"], {"direction": upload_links.UPLOAD,
	                                                             "file": result.get("file"), "ip": _ip()})
	frappe.db.commit()
	return {"file": result.get("file"), "sha256": result.get("sha256"), "file_name": result.get("file_name"),
	        "attached_to": result.get("attached_to")}

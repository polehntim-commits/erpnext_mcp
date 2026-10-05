# SPDX-License-Identifier: MIT
"""One-time upload links. v0.244.0 (built as v0.232.0, held, approved 2026-10-04). docs/design/upload_links.md (also
the threat model). Tim's decisions: no video uploads (47); private by default, public only with an admin switch (48).

WHY: the sidecar refuses any body over 4 MB before reading it, the inline attach
tool stops at 8 MB, and chunked staging keeps every chunk in a table. A 5 MB
operating agreement or an SOP binder fits none of them. So ONE
route is let past the 4 MB check — only after its token is proved — and it streams
to disk, never to the database and never whole into memory.

THE TOKEN IS THE CREDENTIAL, so it is treated like one:

* 32 random bytes; only its SHA-256 is stored (`token_hash`). A database dump
  hands out no working link.
* Unknown, expired, used and revoked all answer the SAME 404 with the same
  wording, so a caller learns nothing by guessing.
* Used once: the claim (Open → Receiving) is row-locked, and a second request
  with the same token is refused while the first is in flight and forever after.
* Nothing in the URL but the token; the file name, the path and the target are
  never taken from the client.

WHAT ARRIVES IS CHECKED THREE WAYS: the extension must be one the link allows,
the first bytes must be what that extension claims (`_MAGIC`), and executables,
scripts, markup and archives are refused whatever they are called. A declared
SHA-256 must match what was counted while streaming.

Off unless `upload_links_enabled` is ticked: the route answers 404 and the issuing
tool refuses.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import os
import re
import secrets

import frappe

from . import compat, settings
from .errors import ToolError

DOCTYPE = "Upload Link"

OPEN = "Open"
RECEIVING = "Receiving"
DONE = "Done"
FAILED = "Failed"
EXPIRED = "Expired"
REVOKED = "Revoked"

#: Who may issue a link. The office, not the field: a link is a door into a record.
ISSUER_ROLES = ("System Manager", "Farm Manager", "Accounts Manager")

#: What each kind of link accepts, by extension.
KINDS = {
	"photo": frozenset({"jpg", "jpeg", "png", "heic", "heif"}),
	"document": frozenset({"pdf", "jpg", "jpeg", "png", "heic", "heif"}),
}

#: Refused on every link, whatever the kind list says. Markup and SVG can carry
#: script; archives hide what is inside them from every check above.
REFUSED_EXTENSIONS = frozenset(
	{
		"exe", "dll", "bat", "cmd", "com", "msi", "sh", "bash", "zsh", "ps1", "py", "js", "mjs",
		"php", "pl", "rb", "jar", "app", "dmg", "pkg", "apk", "ipa", "deb", "rpm",
		"html", "htm", "xhtml", "svg", "svgz", "xml",
		"zip", "tar", "gz", "tgz", "bz2", "xz", "7z", "rar",
	}
)

DEFAULT_MAX_BYTES = 25 * 1024 * 1024
#: The ceiling for a photo or document link, whatever is asked for.
DOCUMENT_CAP_BYTES = 100 * 1024 * 1024

DEFAULT_EXPIRY_MINUTES = 15
MAX_EXPIRY_MINUTES = 24 * 60

#: A link left Receiving this long is a dead transfer: Failed by the sweep.
STALE_RECEIVING = datetime.timedelta(hours=6)

CHUNK = 1024 * 1024
PART_SUFFIX = ".part"

#: The one answer for every token that will not work. Same words, same status.
NOT_FOUND = "Not found."


class UploadRefused(Exception):
	"""A refusal the route turns into a status code and a sentence."""

	def __init__(self, status: int, message: str):
		super().__init__(message)
		self.status = status


# ── settings ────────────────────────────────────────────────────────────────


def enabled() -> bool:
	return bool(compat.checked(settings.get_settings().get("upload_links_enabled")))


def public_allowed() -> bool:
	"""Decision 48: a link that files a PUBLIC file needs an admin to have switched that on."""
	return bool(compat.checked(settings.get_settings().get("upload_link_allow_public")))


# ── helpers ─────────────────────────────────────────────────────────────────


def hash_token(token: str) -> str:
	return hashlib.sha256(str(token or "").encode()).hexdigest()


def _now() -> datetime.datetime:
	return datetime.datetime.fromisoformat(str(frappe.utils.now())[:19])


def _stamp(moment: datetime.datetime) -> str:
	return moment.strftime("%Y-%m-%d %H:%M:%S")


def _parse(value) -> datetime.datetime | None:
	try:
		return datetime.datetime.fromisoformat(str(value)[:19])
	except (TypeError, ValueError):
		return None


def kinds_argument(raw) -> list[str]:
	"""`photo` or `document` — a list or a comma string. Refuses anything else (no video: decision 47)."""
	if raw in (None, "", []):
		return ["document"]
	items = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
	kinds = []
	for item in items:
		kind = str(item or "").strip().lower()
		if not kind:
			continue
		if kind not in KINDS:
			raise ToolError(f"kinds is photo or document — not {kind!r} (video uploads are not offered). Nothing was issued.")
		if kind not in kinds:
			kinds.append(kind)
	return kinds or ["document"]


def allowed_extensions(kinds) -> frozenset:
	out: set = set()
	for kind in kinds:
		out |= KINDS.get(kind, frozenset())
	return frozenset(out - REFUSED_EXTENSIONS)


def cap_for(kinds) -> int:
	return DOCUMENT_CAP_BYTES


def safe_filename(raw: str) -> str:
	"""The last path component, with nothing a file system or a shell would read twice.

	Separators of both kinds, NUL and control characters, `..`, and leading dots
	are removed; the result is at most 120 characters with the extension kept. The
	stored name is `<upload_id>-<this>`, so the client never chooses a path.
	"""
	name = str(raw or "").replace("\\", "/").split("/")[-1]
	name = "".join(ch for ch in name if ch.isprintable() and ch not in "\x00")
	name = name.replace("..", "").strip().lstrip(".").strip()
	name = re.sub(r"[^\w.\- ()]", "_", name)
	if not name:
		name = "upload"
	stem, ext = os.path.splitext(name)
	if len(name) > 120:
		name = stem[: 120 - len(ext)] + ext
	return name


def extension(name: str) -> str:
	return os.path.splitext(name)[1].lstrip(".").lower()


#: What the first bytes of each allowed extension are. Checked against the
#: extension, so an executable renamed `.jpg` is refused.
def _magic_matches(ext: str, head: bytes) -> bool:
	if ext in ("jpg", "jpeg"):
		return head.startswith(b"\xff\xd8\xff")
	if ext == "png":
		return head.startswith(b"\x89PNG\r\n\x1a\n")
	if ext == "pdf":
		return head.startswith(b"%PDF-")
	if ext in ("heic", "heif", "mp4", "mov", "m4v"):
		# ISO base media: a box size, then `ftyp`.
		return len(head) >= 12 and head[4:8] == b"ftyp"
	return False


# ── issuing ─────────────────────────────────────────────────────────────────


def require_issuer(user: str) -> None:
	held = set(frappe.get_roles(user))
	if not held.intersection(ISSUER_ROLES):
		raise ToolError(
			f"issuing an upload link is restricted to {', '.join(ISSUER_ROLES)}. Nothing was issued."
		)


def issue(
	*,
	user: str,
	target_doctype: str,
	target_name: str,
	kinds=None,
	max_bytes=None,
	is_private: bool = True,
	expected_sha256: str = "",
	expires_minutes=None,
	note: str = "",
	base_url: str = "",
) -> dict:
	"""Make one link. Returns the URL — the only time the token exists — and the facts."""
	from .tools import files

	if not enabled():
		raise ToolError(
			"upload links are switched off on this site (ERPNext MCP Settings → Upload Links "
			"Enabled). Nothing was issued."
		)
	require_issuer(user)
	tail = "Nothing was issued."
	# The same checks an attach makes, before anybody uploads anything: the target
	# exists, the caller may write it, it is not cancelled, and it has room.
	parent = files._require_parent_write(target_doctype, target_name, tail=tail)
	files._check_docstatus(target_doctype, target_name, parent, False, tail=tail)
	files._check_attachment_limit(
		target_doctype, target_name, len(files._existing_attachments(target_doctype, target_name)), tail=tail
	)

	if not is_private and not public_allowed():
		raise ToolError(
			"a public file needs an admin to allow it (ERPNext MCP Settings → Upload Links: Allow Public Files). "
			f"Leave is_private true. {tail}"
		)
	kind_list = kinds_argument(kinds)
	cap = cap_for(kind_list)
	try:
		size = int(max_bytes) if max_bytes not in (None, "") else min(DEFAULT_MAX_BYTES, cap)
	except (TypeError, ValueError):
		raise ToolError(f"max_bytes must be a whole number of bytes. {tail}") from None
	if size <= 0:
		raise ToolError(f"max_bytes must be above zero. {tail}")
	if size > cap:
		raise ToolError(
			f"max_bytes {size} is over this link's ceiling of {cap} bytes. {tail}"
		)

	try:
		minutes = int(expires_minutes) if expires_minutes not in (None, "") else DEFAULT_EXPIRY_MINUTES
	except (TypeError, ValueError):
		raise ToolError(f"expires_minutes must be a whole number. {tail}") from None
	if minutes <= 0 or minutes > MAX_EXPIRY_MINUTES:
		raise ToolError(f"expires_minutes is 1 to {MAX_EXPIRY_MINUTES} (24 hours). {tail}")

	digest = str(expected_sha256 or "").strip().lower()
	if digest and not re.fullmatch(r"[0-9a-f]{64}", digest):
		raise ToolError(f"sha256 must be 64 hex characters. {tail}")

	token = secrets.token_urlsafe(32)
	upload_id = "UPL-" + secrets.token_hex(6).upper()
	now = _now()
	expires = now + datetime.timedelta(minutes=minutes)
	doc = frappe.get_doc(
		{
			"doctype": DOCTYPE,
			"upload_id": upload_id,
			"status": OPEN,
			"target_doctype": target_doctype,
			"target_name": target_name,
			"allowed_kinds": ", ".join(kind_list),
			"max_bytes": size,
			"is_private": 1 if is_private else 0,
			"expected_sha256": digest,
			"expires_at": _stamp(expires),
			"note": str(note or "")[:500],
			"token_hash": hash_token(token),
			"issued_by": user,
			"issued_at": _stamp(now),
			"attempts": 0,
		}
	).insert(ignore_permissions=True)
	base = str(base_url or "").rstrip("/")
	return {
		"upload_id": upload_id,
		"link": doc.name,
		"url": f"{base}/farmops/api/upload/{token}",
		"expires_at": _stamp(expires),
		"max_bytes": size,
		"kinds": kind_list,
		"extensions": sorted(allowed_extensions(kind_list)),
		"is_private": bool(is_private),
		"target": {"doctype": target_doctype, "name": target_name},
	}


# ── using ───────────────────────────────────────────────────────────────────


def find(token: str) -> dict | None:
	"""The usable link this token opens, or None — for every reason, alike."""
	token = str(token or "")
	if not token or len(token) > 100:
		return None
	digest = hash_token(token)
	rows = frappe.db.get_all(
		DOCTYPE,
		filters={"token_hash": digest},
		fields=["name", "status", "expires_at", "token_hash"],
		limit=1,
	)
	if not rows or not hmac.compare_digest(str(rows[0].get("token_hash") or ""), digest):
		return None
	row = rows[0]
	if row.get("status") != OPEN:
		return None
	expires = _parse(row.get("expires_at"))
	if expires is None or expires <= _now():
		frappe.db.set_value(DOCTYPE, row["name"], "status", EXPIRED, update_modified=False)
		return None
	return frappe.get_doc(DOCTYPE, row["name"]).as_dict()


def claim(name: str) -> None:
	"""Open → Receiving, row-locked. A second claim on the same link is 409."""
	try:
		status = frappe.db.get_value(DOCTYPE, name, "status", for_update=True)
	except TypeError:  # an older Frappe without for_update on get_value
		status = frappe.db.get_value(DOCTYPE, name, "status")
	if status != OPEN:
		raise UploadRefused(409, "This upload link is already in use.")
	frappe.db.set_value(DOCTYPE, name, "status", RECEIVING, update_modified=False)
	frappe.db.commit()


def note_attempt(name: str, ip: str, user_agent: str) -> None:
	row = frappe.db.get_value(DOCTYPE, name, ["attempts"], as_dict=True) or {}
	frappe.db.set_value(
		DOCTYPE,
		name,
		{
			"attempts": int(row.get("attempts") or 0) + 1,
			"last_attempt_at": _stamp(_now()),
			"last_ip": str(ip or "")[:140],
			"last_user_agent": str(user_agent or "")[:140],
		},
		update_modified=False,
	)


def fail(name: str, reason: str) -> None:
	frappe.db.set_value(DOCTYPE, name, {"status": FAILED, "failure_reason": reason[:500]}, update_modified=False)
	frappe.db.commit()


def files_dir() -> str:
	path = frappe.get_site_path("private", "files")
	os.makedirs(path, exist_ok=True)
	return path


def receive(link: dict, stream, *, declared_length=None, filename: str = "") -> dict:
	"""Stream one file to disk, check it, file it on the target, finish the link.

	`stream` has `.read(n)`. The link must already be claimed. Every refusal raises
	`UploadRefused` with the partial file removed; the caller marks the link Failed.
	"""
	from .tools import files

	cap = int(link.get("max_bytes") or 0)
	if declared_length not in (None, ""):
		try:
			declared = int(declared_length)
		except (TypeError, ValueError):
			declared = 0
		if declared > cap:
			raise UploadRefused(413, f"This file is larger than the link allows ({cap} bytes).")

	safe = safe_filename(filename)
	ext = extension(safe)
	kinds = kinds_argument(link.get("allowed_kinds"))
	allowed = allowed_extensions(kinds)
	if ext in REFUSED_EXTENSIONS or ext not in allowed:
		raise UploadRefused(
			415, f"This link takes {', '.join(sorted(allowed))} files, not {('.' + ext) if ext else 'this file'}."
		)

	final_name = f"{link['upload_id']}-{safe}"
	directory = files_dir()
	part = os.path.join(directory, f".{link['upload_id']}{PART_SUFFIX}")
	digest = hashlib.sha256()
	size = 0
	head = b""
	try:
		with open(part, "wb") as out:
			while True:
				piece = stream.read(CHUNK)
				if not piece:
					break
				size += len(piece)
				if size > cap:
					raise UploadRefused(413, f"This file is larger than the link allows ({cap} bytes).")
				if len(head) < 16:
					head += piece[: 16 - len(head)]
				digest.update(piece)
				out.write(piece)
		if size == 0:
			raise UploadRefused(400, "Nothing was sent.")
		if not _magic_matches(ext, head):
			raise UploadRefused(415, f"The file's contents are not a .{ext} file.")
		sha = digest.hexdigest()
		expected = str(link.get("expected_sha256") or "")
		if expected and not hmac.compare_digest(expected, sha):
			raise UploadRefused(422, "The file's SHA-256 does not match the one the link was issued with.")

		# THE TARGET IS CHECKED AGAIN NOW, not trusted from issue time: it may have been
		# deleted, cancelled or filled since. The write permission was the issuer's.
		files.check_attachable(
			link["target_doctype"],
			link["target_name"],
			final_name,
			tail="Nothing was attached.",
			require_parent_write=False,
		)
		destination = os.path.join(directory, final_name)
		os.replace(part, destination)
	except UploadRefused:
		_remove(part)
		raise
	except ToolError as exc:
		_remove(part)
		raise UploadRefused(409, str(exc)) from None
	except Exception:
		_remove(part)
		raise

	is_private = bool(compat.checked(link.get("is_private")))
	file_url = ("/private/files/" if is_private else "/files/") + final_name
	if not is_private:
		public_dir = frappe.get_site_path("public", "files")
		os.makedirs(public_dir, exist_ok=True)
		os.replace(destination, os.path.join(public_dir, final_name))
	attachment = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": final_name,
			"file_url": file_url,
			"is_private": 1 if is_private else 0,
			"file_size": size,
			"attached_to_doctype": link["target_doctype"],
			"attached_to_name": link["target_name"],
		}
	).insert(ignore_permissions=True)
	frappe.db.set_value(
		DOCTYPE,
		link["name"],
		{
			"status": DONE,
			"file": attachment.name,
			"file_name": final_name,
			"file_size": size,
			"file_sha256": sha,
			"received_at": _stamp(_now()),
		},
		update_modified=False,
	)
	return {
		"upload_id": link["upload_id"],
		"file": attachment.name,
		"file_name": final_name,
		"file_size": size,
		"sha256": sha,
		"attached_to": {"doctype": link["target_doctype"], "name": link["target_name"]},
	}


def _remove(path: str) -> None:
	try:
		os.remove(path)
	except OSError:
		pass


# ── reading, revoking, sweeping ─────────────────────────────────────────────


def describe(row: dict) -> dict:
	return {
		key: row.get(key)
		for key in (
			"name", "upload_id", "status", "target_doctype", "target_name", "allowed_kinds", "max_bytes",
			"is_private", "expires_at", "note", "issued_by", "issued_at", "revoked_by", "revoked_at",
			"file", "file_name", "file_size", "file_sha256", "received_at", "attempts",
			"last_attempt_at", "last_ip", "failure_reason",
		)
	}


def get(name_or_id: str) -> dict:
	name = str(name_or_id or "").strip()
	row = None
	if name and frappe.db.exists(DOCTYPE, name):
		row = frappe.get_doc(DOCTYPE, name).as_dict()
	elif name:
		found = frappe.db.get_all(DOCTYPE, filters={"upload_id": name}, fields=["name"], limit=1)
		if found:
			row = frappe.get_doc(DOCTYPE, found[0]["name"]).as_dict()
	if not row:
		raise ToolError(f"no Upload Link named {name!r}.")
	return describe(row)


def revoke(name_or_id: str, user: str) -> dict:
	require_issuer(user)
	row = get(name_or_id)
	if row["status"] not in (OPEN,):
		raise ToolError(f"{row['upload_id']} is {row['status']}; only an Open link can be revoked. Nothing changed.")
	frappe.db.set_value(
		DOCTYPE,
		row["name"],
		{"status": REVOKED, "revoked_by": user, "revoked_at": _stamp(_now())},
		update_modified=False,
	)
	return get(row["name"])


def sweep() -> dict:
	"""Hourly. Expire Open links past their time, fail dead transfers, delete stray parts."""
	now = _now()
	expired = failed = removed = 0
	for row in frappe.db.get_all(DOCTYPE, filters={"status": OPEN}, fields=["name", "expires_at"]):
		moment = _parse(row.get("expires_at"))
		if moment is None or moment <= now:
			frappe.db.set_value(DOCTYPE, row["name"], "status", EXPIRED, update_modified=False)
			expired += 1
	for row in frappe.db.get_all(DOCTYPE, filters={"status": RECEIVING}, fields=["name", "last_attempt_at"]):
		moment = _parse(row.get("last_attempt_at"))
		if moment is None or now - moment > STALE_RECEIVING:
			frappe.db.set_value(
				DOCTYPE, row["name"], {"status": FAILED, "failure_reason": "The transfer never finished."},
				update_modified=False,
			)
			failed += 1
	try:
		directory = files_dir()
		for entry in os.listdir(directory):
			if entry.startswith(".UPL-") and entry.endswith(PART_SUFFIX):
				path = os.path.join(directory, entry)
				age = datetime.datetime.now().timestamp() - os.path.getmtime(path)
				if age > STALE_RECEIVING.total_seconds():
					_remove(path)
					removed += 1
	except OSError:
		pass
	if expired or failed or removed:
		frappe.db.commit()
	return {"expired": expired, "failed": failed, "parts_removed": removed}

# SPDX-License-Identifier: MIT
"""The two evidence-upload methods the Farm Ops app calls.

    POST /api/method/erpnext_mcp.api.files.stage_file_chunk
    POST /api/method/erpnext_mcp.api.files.finalize_staged_file

Photos and signatures go up in 512 KB slices before the completion call, then
finalise into one File whose docname the phone sends back as a `file_token`.
The staging machinery is Sprint 6's (`tools/uploads.py`, v0.14.0) and is not
reimplemented here; what this module adds is the argument translation and four
refusals the general path does not make.

WHY THE APP'S ARGUMENT NAMES DIFFER, AND WHY THE TRANSLATION IS HERE. The tool
speaks `session_id / total_chunks / chunk_base64 / expected_size`; the app speaks
`upload_id / chunk_count / data / total_bytes`. Neither is wrong and the app is
already in TestFlight, so the rename lives on the server — a shipped build is a
worse place to fix a key name than a wrapper is.

────────────────────────────────────────────────────────────────────────────
THE FOUR REFUSALS THAT ARE NOT IN `tools/uploads.py`
────────────────────────────────────────────────────────────────────────────

1. **NO ATTACHMENT TARGET IS ACCEPTED.** `commit_staged_file` takes
   `attach_to_doctype` / `attach_to_name`, and forwarding them would let a field
   worker hang a file off ANY document on the site — a Journal Entry, another
   entity's lease, a Governance Document. Evidence is committed unattached and
   private, and it reaches its Housing Inspection through
   `complete_task_via_mobile`, which checks the task is theirs. The
   `governance_document=true` path is unreachable for the same reason.

2. **PRIVATE, ALWAYS.** `is_private` is not an argument here. A public File is
   readable by anyone who can guess the URL, and a signature capture is the last
   file on this site that should be.

3. **AN EXTENSION ALLOWLIST.** `commit_staged_file` only checks extensions when
   there IS an attachment target — `_check_extension` runs inside
   `check_attachable` — so an unattached commit, which is exactly what this
   module does, skips it. Evidence is photographs and signatures, so the list is
   photographs and signatures, and `.html` / `.svg` (both of which execute script
   when served) are not on it.

4. **THE FILENAME IS REDUCED TO A BASENAME.** A name carrying `/` or `..` never
   reaches storage.

WHAT SESSION OWNERSHIP ALREADY DOES, so it is not rebuilt: `_assert_owner` binds
a staging session to whoever staged its first piece, compared against
`frappe.session.user`. On this transport that IS the worker — not the MCP System
User the MCP endpoint would present — so one worker cannot resume, read or
finalise another's upload, and that check comes free with running as the caller.
"""

from __future__ import annotations

import os

import frappe

from ..errors import ToolError
from ..tools import uploads
from . import guard

#: What evidence is allowed to be. Photographs, signature captures, and the one
#: document type a lab result arrives as. Deliberately short: this endpoint
#: exists to receive a camera roll, not a file drop.
_ALLOWED_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".pdf"})

#: Mobile sessions live in their own namespace so a phone's `upload_id` can never
#: collide with a staging session an operator opened through the MCP endpoint.
#: Ownership is what actually separates two callers; this separates two *uses*.
_PREFIX = "farmops:"


def _session(upload_id) -> str:
	value = str(upload_id or "").strip()
	if not value:
		frappe.throw("upload_id is required.", frappe.ValidationError)
	if len(value) > 100 or any(character in value for character in "\n\r\t"):
		frappe.throw("upload_id is not a valid upload identifier.", frappe.ValidationError)
	return f"{_PREFIX}{value}"


def _file_name(file_name) -> str:
	"""A basename this site is willing to store, or a refusal naming the reason."""
	raw = str(file_name or "").strip()
	if not raw:
		frappe.throw("file_name is required.", frappe.ValidationError)
	name = os.path.basename(raw.replace("\\", "/")).strip()
	if not name or name.startswith(".") or len(name) > 140:
		frappe.throw(f"file_name {raw!r} is not a usable file name.", frappe.ValidationError)
	extension = os.path.splitext(name)[1].lower()
	if extension not in _ALLOWED_EXTENSIONS:
		frappe.throw(
			f"{extension or 'that'} is not an evidence file type. Photographs and signature "
			f"captures only: {', '.join(sorted(_ALLOWED_EXTENSIONS))}.",
			frappe.ValidationError,
		)
	return name


# ── 10. stage_file_chunk ────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("stage_file_chunk", limit=guard.UPLOAD_LIMIT, mutating=True)
def stage_file_chunk(
	user: str,
	upload_id=None,
	file_name=None,
	chunk_index=None,
	chunk_count=None,
	total_bytes=None,
	data=None,
) -> dict:
	"""One 512 KB slice into staging. Creates the session on the first call.

	Idempotent per index: re-sending a slice that already arrived replaces it,
	which is the only safe answer for a caller whose call timed out and who
	therefore does not know whether it landed. The app uploads sequentially — a
	thin field link finishes three photographs faster in series than in parallel,
	and a failure in series is attributable.
	"""
	session = _session(upload_id)
	name = _file_name(file_name)

	result = uploads.stage_file_chunk(
		{
			"session_id": session,
			"chunk_index": chunk_index,
			"total_chunks": chunk_count,
			"chunk_base64": data,
			"expected_size": total_bytes,
		}
	)
	staged = result.data
	return {
		"upload_id": str(upload_id),
		"file_name": name,
		"chunk_index": staged.get("received"),
		"chunks_received": staged.get("chunks_received"),
		"chunk_count": staged.get("total_chunks"),
		"complete": staged.get("complete"),
		"next_expected_index": staged.get("next_expected_index"),
		"staged_bytes": staged.get("staged_bytes_so_far"),
	}


# ── 11. finalize_staged_file ────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
@guard.endpoint("finalize_staged_file", limit=guard.UPLOAD_LIMIT, mutating=True)
def finalize_staged_file(user: str, upload_id=None, file_name=None, sha256=None, total_bytes=None) -> dict:
	"""Assemble the slices into one private File and hand back its handle.

	THE HASH IS CHECKED, NOT RECORDED. The app computes the SHA-256 at capture
	and it travels with the file; its contract asks the server to verify it and
	reject on mismatch, because an audit trail that records evidence hashes it
	never checked is recording a claim rather than a fact. `declare_expectations`
	puts the caller's digest on the session before assembly, so
	`commit_staged_file` compares it against the bytes it actually assembled and
	refuses the commit if they differ — leaving the staged pieces untouched so
	the app can re-send the bad slice rather than the whole file.

	`file_token` IS THE File DOCNAME. The app treats it as opaque and sends it
	straight back in `complete_task_via_mobile`, where `normalise_evidence`
	refuses any docname that is not a real File on this site. `file_url` goes
	back too because the app's contract accepts it alone as the handle.
	"""
	session = _session(upload_id)
	name = _file_name(file_name)

	# v0.226.0. A finalize whose reply was lost: the session is gone because it
	# worked, so the second call answers with the File the first one made.
	done = _committed(session)
	if done and not frappe.db.exists(uploads.SESSION_DOCTYPE, {"session_id": session}):
		return done

	uploads.declare_expectations(session, expected_sha256=sha256, expected_size=total_bytes)
	result = uploads.commit_staged_file(
		{
			"session_id": session,
			"file_name": name,
			# Hardcoded, not forwarded. See the module docstring: an evidence
			# upload has no business naming where it lands or who may read it.
			"is_private": True,
			"governance_document": False,
			"dry_run": False,
		}
	)
	committed = result.data
	token = committed.get("file")
	if not token:
		raise ToolError("the upload committed but produced no File record. Nothing was attached.")

	answer = {
		"file_token": token,
		"file_url": str(frappe.db.get_value("File", token, "file_url") or ""),
		"file_name": committed.get("file_name"),
		"sha256": committed.get("sha256"),
		"sha256_verified": committed.get("sha256_verified"),
		"total_bytes": committed.get("file_size"),
		"is_private": True,
	}
	_remember_committed(session, answer)
	return answer


# ── v0.226.0: which pieces arrived, so an interrupted upload resumes ─────────
#: How long a finished upload's answer is kept for a phone that lost the reply.
COMMITTED_TTL = uploads.SESSION_TTL_HOURS * 3600
_LOCAL_COMMITTED: dict = {}


def _committed_key(session: str) -> str:
	return f"erpnext_mcp:committed_upload:{session}"


def _remember_committed(session: str, answer: dict) -> None:
	value = {**answer, "owner": str(frappe.session.user)}
	try:
		frappe.cache().set_value(_committed_key(session), value, expires_in_sec=COMMITTED_TTL)
	except Exception:
		_LOCAL_COMMITTED[_committed_key(session)] = value


def _committed(session: str) -> dict:
	"""The answer a finished upload gave, to the user who finished it — else {}."""
	try:
		value = frappe.cache().get_value(_committed_key(session))
	except Exception:
		value = None
	value = value or _LOCAL_COMMITTED.get(_committed_key(session))
	if not isinstance(value, dict) or value.get("owner") != str(frappe.session.user):
		return {}
	if not frappe.db.exists("File", value.get("file_token")):
		return {}
	return {key: item for key, item in value.items() if key != "owner"}


@frappe.whitelist(methods=["POST", "GET"])
@guard.endpoint("get_staged_upload", limit=guard.UPLOAD_LIMIT)
def get_staged_upload(user: str, upload_id=None) -> dict:
	"""Where one of the caller's uploads stands: the pieces that arrived, or the File it became.

	THE PHONE SENDS ONLY WHAT IS MISSING. A photo sent over one bar of signal is
	cut off half way more often than not; with a stable `upload_id` the phone
	asks this first and skips every piece the server already holds. Read-only,
	and the caller's own uploads only — the staging layer's owner check.
	"""
	session = _session(upload_id)
	row = frappe.db.get_value(
		uploads.SESSION_DOCTYPE, {"session_id": session}, ["name", "total_chunks", "owner"], as_dict=True
	)
	if row:
		uploads._assert_owner(row, session, "Nothing was read.")
		total = int(row.get("total_chunks") or 0)
		# The pieces hang off the session's row, not its id.
		received = uploads._received_indexes(row["name"])
		missing = uploads._missing_indexes(received, total)
		return {
			"upload_id": str(upload_id),
			"state": "partial",
			"chunk_count": total,
			"received": received,
			"missing": missing,
			"complete": total > 0 and not missing,
		}
	done = _committed(session)
	if done:
		return {"upload_id": str(upload_id), "state": "committed", **done}
	return {"upload_id": str(upload_id), "state": "none", "received": [], "missing": [], "complete": False}

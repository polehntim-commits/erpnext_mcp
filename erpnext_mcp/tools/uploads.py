# SPDX-License-Identifier: MIT
"""Streaming uploads: move a file onto this site across as many calls as it takes.

THE PROBLEM, WHICH IS NOT THE ONE IT LOOKS LIKE. `attach_file_to_document` and
`attach_governance_document` both accept up to 8 MB of base64 in a single call.
That ceiling has never been what stopped anybody. The real ceiling is that an AI
operator has to *compose* the argument, and a base64 string lives in the tool
call it is writing — which runs out at a couple of hundred kilobytes long before
the API does. So the tool advertised 8 MB and could be handed 200 KB, and every
file-bearing operation through Sprint 5 and Sprint 6 collapsed into the same four
manual steps: Claude writes a Python script, Tim scp's it to the box, docker cp's
it into the container, docker exec's it. Per-parcel appraisal PDFs. The master
appraisal. Re-filing after a conveyance, three times. Backfilling suppliers. Tim,
2026-07-30, in one sentence: "So we don't have to run these scripts."

THE SHAPE OF THE FIX. `stage_file_chunk` takes one piece at a time and puts it in
a table. `commit_staged_file` reassembles the pieces, checks them against a hash
the caller computed before sending anything, and turns them into a File — either
attached to a document or filed as a Governance Document. `cancel_staged_upload`
throws a dead upload away. `list_staged_uploads` shows what is in flight, which
is the tool you want when call 43 of 60 fails and you need to know where to
resume.

WHY A TABLE AND NOT THE CACHE. Redis is the obvious place to stage half a file
and it is the wrong one. A 5 MB upload is a hundred round trips over some
minutes, and in that window a `bench restart`, a worker recycle or an eviction
under memory pressure throws the lot away — and the caller finds out at commit,
having spent the whole upload. Rows in a table survive every one of those.
"Restart the bench halfway through and finish the upload" is a test in this
suite, and it is the design decision the whole feature turns on.

EACH CHUNK IS THE BASE64 OF ITS OWN SLICE OF THE FILE. This is the one thing a
caller can get wrong, so it is said here, in the tool description, in the
argument description and in the refusal. Take the file's bytes, cut them into
pieces, and base64 each piece separately. Do NOT base64 the whole file and then
cut the resulting string up: the middle pieces of that are not valid base64, they
cannot be checked when they arrive, and their per-piece hashes mean nothing. The
slices need not be any particular size or a multiple of anything — the pieces are
decoded individually and their bytes concatenated in index order.

WHAT THE HASHES ARE FOR. Not security; the transport already had a bearer token.
Diagnosis. A hundred separate calls each of which said "fine" can still add up to
a file that is not the one somebody meant to send, and `expected_sha256` is the
only thing that proves otherwise. Every piece additionally records the hash of
its own bytes, so a file that fails its aggregate check can be narrowed to the
call that carried the bad piece rather than reported as "the upload is wrong".

STAGING IS TRANSIENT AND CLEANS UP AFTER ITSELF, TWICE. A session is deleted on
commit and on cancel. Sessions nobody has touched for a day are swept by
`collect_expired_sessions`, which runs on the daily scheduler AND opportunistically
at the top of every `stage_file_chunk` call. The second is the kairotic one: the
right moment to clear out abandoned uploads is when somebody is uploading, not at
three in the morning, and it means a bench with the scheduler switched off does
not quietly accumulate ninety megabytes of a PDF nobody finished sending.

OWNERSHIP. A session belongs to the user who staged its first piece. Only that
user may add to it, commit it or cancel it. This is not paranoia about other
operators; it is that two callers who happen to pick the same session id would
otherwise interleave their pieces into one corrupt file, and the failure would
look like corruption rather than like a collision.
"""

import base64
import hashlib

import frappe

from .. import compat
from ..args import as_bool, as_int, as_str
from ..errors import ToolError
from ..result import ToolResult
from . import files, governance

SESSION_DOCTYPE = "Staged File Upload Session"
CHUNK_DOCTYPE = "Staged File Chunk"

#: Longest `chunk_base64` one call will take, in characters. 200 KB was the old
#: ceiling — it fit the model-context constraint MCP tool callers work under. But
#: v0.18.0's farmops-api sidecar has no such constraint, and Farm Ops iOS uses
#: 512 KB raw slices (per `FarmOpsKit.FarmOpsConfig.uploadChunkBytes`), which
#: base64-encode to ~700 KB per chunk. Every iPhone upload before v0.18.4 hit
#: the old ceiling as a 400 and iOS marked the completion failed. 800 KB gives
#: iOS the room its shipped build already assumes with a small margin, and MCP
#: callers keep breaking their own uploads into smaller pieces because their
#: context window is the true constraint, not this one.
MAX_CHUNK_BASE64 = 800 * 1024

#: Most pieces one upload may declare. 600 × 800 KB of base64 is roughly 360 MB
#: of file. Also declared on the DocType, so a session saved by hand obeys it too.
MAX_TOTAL_CHUNKS = 600

#: Ceiling on the whole assembled file. Past this the Desk's own upload control
#: or a `file_url` this site can fetch is the better tool, and saying so is more
#: use than letting somebody discover it at chunk 700.
MAX_TOTAL_BYTES = 100 * 1024 * 1024

#: How long an untouched session is kept before it is swept. A day is long enough
#: to survive a night, a redeploy and somebody going home mid-upload, and short
#: enough that abandoned megabytes do not become the site's problem.
SESSION_TTL_HOURS = 24

#: Longest a session id may be. It is a Data column and a human has to read it in
#: `list_staged_uploads`; a UUID is 36 characters.
MAX_SESSION_ID = 140

#: Piece size for `stage_internal_bytes`, which is not composing an argument in a
#: model's context and is therefore not bound by `MAX_CHUNK_BASE64`. One megabyte
#: of raw bytes is a row a database is comfortable with and a checkpoint fine
#: enough to be worth having.
INTERNAL_CHUNK_BYTES = 1024 * 1024


# ── 129. stage_file_chunk ───────────────────────────────────────────────────
def stage_file_chunk(args: dict) -> ToolResult:
	"""Put one piece of a file into staging. Creates the session on the first call.

	Idempotent per piece: re-sending an index that already arrived REPLACES it and
	says so. That is deliberate. A caller whose call timed out does not know
	whether the piece landed, and a refusal would leave them with no safe move —
	retry is the only thing they can do, so retry has to work.
	"""
	_require_doctypes()
	collect_expired_sessions()

	session_id = _session_id(as_str(args, "session_id", required=True))
	total_chunks = _positive(args, "total_chunks", required=True)
	chunk_index = as_int(args, "chunk_index")
	if chunk_index is None:
		raise ToolError(
			"chunk_index is required and counts from 0. It is where this piece goes in the "
			"finished file, so a caller that omits it is asking for the pieces to be assembled "
			"in whatever order they happen to be read back. Nothing was staged."
		)
	if total_chunks > MAX_TOTAL_CHUNKS:
		raise ToolError(
			f"total_chunks is {total_chunks}, over this app's ceiling of {MAX_TOTAL_CHUNKS}. "
			f"That is roughly {files.human_size(MAX_TOTAL_CHUNKS * MAX_CHUNK_BASE64 * 3 // 4)} of "
			"file. Something that big should go through the Desk's upload control, or be put "
			"somewhere this site can fetch it and recorded with file_url. Nothing was staged."
		)
	if chunk_index < 0 or chunk_index >= total_chunks:
		raise ToolError(
			f"chunk_index {chunk_index} is outside the {total_chunks} piece(s) this upload "
			f"declared. Pieces are numbered 0 to {total_chunks - 1}. Nothing was staged."
		)

	raw = as_str(args, "chunk_base64", required=True)
	if len(raw) > MAX_CHUNK_BASE64:
		raise ToolError(
			f"chunk_base64 is {len(raw)} characters, over the {MAX_CHUNK_BASE64}-character "
			"per-piece limit. Cut the file into smaller slices — the limit is per CALL, not per "
			"file, and there is no penalty for using more pieces. Nothing was staged."
		)
	content = _decode_chunk(raw, chunk_index)

	expected_sha256 = _sha256_arg(args, "expected_sha256")
	expected_size = as_int(args, "expected_size")
	if expected_size is not None and expected_size < 0:
		raise ToolError("expected_size cannot be negative. Nothing was staged.")

	session = _open_session(session_id, total_chunks, expected_sha256, expected_size)
	existing = _chunk_row(session.name, chunk_index)

	staged_after = (
		int(session.staged_bytes or 0) + len(content) - int((existing or {}).get("chunk_bytes") or 0)
	)
	if staged_after > MAX_TOTAL_BYTES:
		raise ToolError(
			f"staging this piece would take the upload to {files.human_size(staged_after)}, over "
			f"this app's {files.human_size(MAX_TOTAL_BYTES)} ceiling for one assembled file. "
			"Nothing was staged, and the pieces already staged are untouched — cancel_staged_upload "
			f"{session_id!r} clears them."
		)

	digest = hashlib.sha256(content).hexdigest()
	if existing:
		frappe.db.set_value(
			CHUNK_DOCTYPE,
			existing["name"],
			{"chunk_base64": raw, "chunk_bytes": len(content), "chunk_sha256": digest},
		)
	else:
		# v0.18.3: `guard.endpoint` has already validated the caller as a Farm
		# Ops role with an active Mobile Access Grant — that IS the permission
		# boundary for evidence uploads. The Staged File Upload Session /
		# Staged File Chunk doctypes' Desk permissions are System Manager and
		# Accounts Manager only (they exist to keep operators out of Desk lists
		# of half-uploaded photos), so leaving `ignore_permissions=False` on
		# these inserts makes every Farm Manager / Foreman / Farm Worker upload
		# refuse with "That request could not be completed" — the exact 403
		# that broke the whole iOS evidence path.
		frappe.get_doc(
			{
				"doctype": CHUNK_DOCTYPE,
				"session": session.name,
				"chunk_index": chunk_index,
				"chunk_base64": raw,
				"chunk_bytes": len(content),
				"chunk_sha256": digest,
			}
		).insert(ignore_permissions=True)

	received = _received_indexes(session.name)
	session.chunks_received = len(received)
	session.staged_bytes = staged_after
	session.save(ignore_permissions=True)

	missing = _missing_indexes(received, total_chunks)
	data = {
		"session_id": session_id,
		"received": chunk_index,
		"replaced": bool(existing),
		"chunk_bytes": len(content),
		"chunk_sha256": digest,
		"chunks_received": len(received),
		"total_chunks": total_chunks,
		"staged_bytes_so_far": staged_after,
		"staged_size_human": files.human_size(staged_after),
		"next_expected_index": missing[0] if missing else None,
		"missing_chunks": _ranges(missing),
		"complete": not missing,
		"expected_sha256": session.get("expected_sha256") or None,
		"expected_size": int(session.get("expected_size") or 0) or None,
		"note": (
			f"All {total_chunks} piece(s) are staged. Call commit_staged_file with this "
			"session_id and a file_name to assemble and attach them."
			if not missing
			else (
				f"{len(missing)} piece(s) still to come. Each chunk_base64 must be the base64 of "
				"ITS OWN slice of the file's bytes, not a slice of the base64 of the whole file."
			)
		),
	}
	return ToolResult(
		data,
		f"staged chunk {chunk_index} of {total_chunks} for {session_id!r} "
		f"({files.human_size(len(content))}, sha256 {digest[:12]}) — "
		f"{len(received)}/{total_chunks} pieces, {files.human_size(staged_after)} so far"
		+ (" — REPLACED an earlier copy of this piece" if existing else ""),
		docstatus_delta="",
	)


# ── 130. commit_staged_file ─────────────────────────────────────────────────
def commit_staged_file(args: dict) -> ToolResult:
	"""Reassemble a staged upload into a File, and clear the staging behind it.

	THE ORDER OF THE CHECKS IS THE DESIGN. Everything that can be decided without
	the bytes is decided first — is the session complete, does the parent document
	exist, is it writable, is it cancelled, does it already have a file by this
	name, does this ERPNext accept the extension. Finding out that the parent was
	cancelled AFTER reassembling ninety megabytes in memory is finding out too
	late, and it is the difference between a refusal and a refusal that also
	stalled the worker.

	NOTHING IS DELETED UNTIL THE FILE EXISTS. The chunks outlive every failure
	path, so a commit that is refused for any reason leaves the upload exactly
	where it was and the caller can fix the argument and try again rather than
	re-send the file.
	"""
	_require_doctypes()
	session_id = _session_id(as_str(args, "session_id", required=True))
	file_name = as_str(args, "file_name", required=True)
	is_private = bool(as_bool(args, "is_private", True))
	as_governance = bool(as_bool(args, "governance_document", False))
	dry_run = bool(as_bool(args, "dry_run", False))
	tail = "Nothing was written, and the staged pieces are untouched."

	session = _require_session(session_id, tail)
	total_chunks = int(session.get("total_chunks") or 0)
	received = _received_indexes(session["name"])
	missing = _missing_indexes(received, total_chunks)
	if missing:
		raise ToolError(
			f"upload {session_id!r} is missing piece(s) {', '.join(_ranges(missing))} of "
			f"{total_chunks} — {len(received)} arrived. A file assembled with a hole in it is "
			"worse than no file, because nothing about it says which part is absent. Stage the "
			f"missing piece(s) with stage_file_chunk and commit again. {tail}"
		)

	target = _commit_target(args, as_governance, file_name, tail)

	content = _assemble(session["name"], total_chunks)
	digest = hashlib.sha256(content).hexdigest()
	_verify(session, content, digest, session_id, tail)

	plan = {
		"session_id": session_id,
		"file_name": file_name,
		"file_size": len(content),
		"size_human": files.human_size(len(content)),
		"sha256": digest,
		"sha256_verified": bool(session.get("expected_sha256")),
		"size_verified": bool(int(session.get("expected_size") or 0)),
		"chunks_assembled": total_chunks,
		"is_private": is_private,
		"destination": target["destination"],
		"attach_to_doctype": target["doctype"] or None,
		"attach_to_name": target["name"] or None,
	}

	if dry_run:
		return ToolResult(
			{
				**plan,
				"dry_run": True,
				"committed": False,
				"note": (
					"Nothing was written and nothing was cleared. The pieces were reassembled in "
					"memory and checked, so this result's sha256 IS the sha256 of the file that "
					"would be created. Call again with dry_run=false to write it."
				),
			},
			f"dry run: {session_id!r} would commit {file_name} "
			f"({files.human_size(len(content))}, sha256 {digest[:12]}) to {target['destination']}",
		)

	if as_governance:
		result = governance.file_governance_document(args, content=content, file_name=file_name, tail=tail)
		created = result.data
		attachment_name = (created.get("attachment") or {}).get("name")
		attached_doctype, attached_name = governance.GOVERNANCE_DOCUMENT, created.get("name")
	else:
		attachment = files.insert_attachment(
			file_name,
			content,
			is_private=is_private,
			doctype=target["doctype"],
			name=target["name"],
			parent_authorized=True,
		)
		created = None
		attachment_name = attachment.name
		attached_doctype, attached_name = target["doctype"] or None, target["name"] or None

	cleared = _clear_session(session["name"])

	data = {
		**plan,
		"dry_run": False,
		"committed": True,
		"file": attachment_name,
		"attached_to_doctype": attached_doctype,
		"attached_to_name": attached_name,
		"governance_document": created,
		"chunks_cleared": cleared,
		"note": (
			f"The {total_chunks} staged piece(s) and the session were deleted — the File is the "
			"record now, and staging that outlives the file it built is just rubbish on the site."
			+ (
				" The file is PRIVATE: reading it back requires read permission on the document it hangs off."
				if is_private
				else " The file is PUBLIC: anyone who can guess the URL can read it."
			)
		),
		"next_step": (
			"get_governance_document_content reads it back."
			if as_governance
			else (
				f"list_attachments on {attached_doctype} {attached_name} now shows it."
				if attached_doctype
				else "The file is on the site but hangs off no document. Attach it with "
				"attach_file_to_document(file_url=...) if it belongs somewhere."
			)
		),
	}
	return ToolResult(
		data,
		f"committed {file_name} ({files.human_size(len(content))}, sha256 {digest[:12]}) from "
		f"{total_chunks} staged piece(s) to {target['destination']}"
		+ (" — sha256 verified against the caller's own" if session.get("expected_sha256") else ""),
		docstatus_delta="none → 0 (File created)",
	)


# ── 131. cancel_staged_upload ───────────────────────────────────────────────
def cancel_staged_upload(args: dict) -> ToolResult:
	"""Throw away a staged upload without committing it.

	Deliberately cheap and deliberately not called destructive in the catalogue.
	What it destroys is half a file nobody has committed; the alternative to
	having it is a caller who mis-sent a piece being unable to start again.
	"""
	_require_doctypes()
	session_id = _session_id(as_str(args, "session_id", required=True))
	session = _require_session(session_id, "Nothing was cleared.")
	staged_bytes = int(session.get("staged_bytes") or 0)
	cleared = _clear_session(session["name"])
	data = {
		"session_id": session_id,
		"chunks_cleared": cleared,
		"bytes_discarded": staged_bytes,
		"size_human": files.human_size(staged_bytes),
		"note": ("Nothing was committed and no File was created. The session id is free to use again."),
	}
	return ToolResult(
		data,
		f"cancelled staged upload {session_id!r}: {cleared} piece(s), "
		f"{files.human_size(staged_bytes)} discarded",
		docstatus_delta="",
	)


# ── 132. list_staged_uploads ────────────────────────────────────────────────
def list_staged_uploads(args: dict) -> ToolResult:
	"""Every upload currently in flight, with the gaps in each one.

	The tool you want when call 43 of 60 failed and you need to know where to
	resume rather than re-sending five megabytes. A System Manager sees every
	session on the site; anybody else sees their own, which is the same set they
	are allowed to commit.
	"""
	_require_doctypes()
	user = frappe.session.user
	everyones = "System Manager" in set(frappe.get_roles(user) or [])
	filters = {} if everyones else {"owner": user}
	rows = frappe.db.get_all(
		SESSION_DOCTYPE,
		filters=filters,
		fields=compat.existing_fields(
			SESSION_DOCTYPE,
			(
				"name",
				"session_id",
				"total_chunks",
				"chunks_received",
				"staged_bytes",
				"last_activity",
				"expected_sha256",
				"expected_size",
				"owner",
				"creation",
			),
		),
		order_by="modified desc",
		limit=200,
	)

	uploads = []
	for row in rows or []:
		total = int(row.get("total_chunks") or 0)
		missing = _missing_indexes(_received_indexes(row["name"]), total)
		staged = int(row.get("staged_bytes") or 0)
		uploads.append(
			{
				"session_id": row.get("session_id"),
				"owner": row.get("owner"),
				"chunks_received": int(row.get("chunks_received") or 0),
				"total_chunks": total,
				"missing_chunks": _ranges(missing),
				"next_expected_index": missing[0] if missing else None,
				"complete": not missing,
				"staged_bytes": staged,
				"staged_size_human": files.human_size(staged),
				"expected_size": int(row.get("expected_size") or 0) or None,
				"expected_sha256": row.get("expected_sha256") or None,
				"last_activity": str(row.get("last_activity") or ""),
				"started": str(row.get("creation") or ""),
				"expires_after_hours_idle": SESSION_TTL_HOURS,
			}
		)

	ready = [upload for upload in uploads if upload["complete"]]
	data = {
		"uploads": uploads,
		"count": len(uploads),
		"ready_to_commit": [upload["session_id"] for upload in ready],
		"scope": "every session on this site" if everyones else f"sessions owned by {user}",
		"total_staged_bytes": sum(upload["staged_bytes"] for upload in uploads),
		"note": (
			"A session with gaps is an upload that died partway through: stage the pieces in "
			f"`missing_chunks` and commit, or clear it with cancel_staged_upload. Anything idle "
			f"for {SESSION_TTL_HOURS} hours is swept automatically."
		),
	}
	return ToolResult(
		data,
		f"{len(uploads)} staged upload(s), {len(ready)} ready to commit, "
		f"{files.human_size(data['total_staged_bytes'])} staged",
	)


# ── the sweeper ─────────────────────────────────────────────────────────────
def collect_expired_sessions() -> int:
	"""Delete sessions nobody has touched for `SESSION_TTL_HOURS`, and their pieces.

	Runs on the daily scheduler AND at the top of every `stage_file_chunk` call.
	The second is the one that matters: the right moment to clear out abandoned
	uploads is when somebody is uploading, and a bench with its scheduler switched
	off — which is most dev benches, and was this project's for a while — would
	otherwise accumulate ninety megabytes of a PDF nobody finished sending.

	NEVER RAISES. It runs beside real work, and a sweeper that took down a staging
	call would be worse than the litter it exists to remove.
	"""
	try:
		if not compat.doctype_exists(SESSION_DOCTYPE):
			return 0
		cutoff = frappe.utils.add_to_date(
			frappe.utils.now(), hours=-SESSION_TTL_HOURS, as_string=True, as_datetime=True
		)
		stale = frappe.db.get_all(
			SESSION_DOCTYPE,
			filters={"modified": ("<", cutoff)},
			fields=["name"],
			limit=200,
		)
		swept = 0
		for row in stale or []:
			_clear_session(row["name"])
			swept += 1
		return swept
	except Exception:
		frappe.log_error(
			title="erpnext_mcp: could not sweep expired staged uploads",
			message=compat.traceback_text(),
		)
		return 0


# ── plumbing ────────────────────────────────────────────────────────────────
def _require_doctypes() -> None:
	compat.require_doctype(
		SESSION_DOCTYPE,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)
	compat.require_doctype(CHUNK_DOCTYPE, "It ships with erpnext_mcp — run `bench migrate`.")


def _session_id(value: str) -> str:
	"""A session id that is safe to store, read back and put in a message."""
	text = str(value or "").strip()
	if len(text) > MAX_SESSION_ID:
		raise ToolError(
			f"session_id is {len(text)} characters, over the {MAX_SESSION_ID}-character limit. It "
			"is a handle a human has to be able to read in list_staged_uploads — a UUID is 36. "
			"Nothing was staged."
		)
	if any(character in text for character in "\n\r\t"):
		raise ToolError("session_id cannot contain newlines or tabs. Nothing was staged.")
	return text


def _positive(args: dict, key: str, required: bool = False) -> int:
	value = as_int(args, key)
	if value is None:
		if required:
			raise ToolError(f"{key} is required. Nothing was staged.")
		return 0
	if value < 1:
		raise ToolError(f"{key} must be at least 1, got {value}. Nothing was staged.")
	return value


def _sha256_arg(args: dict, key: str) -> str:
	text = as_str(args, key).lower()
	if not text:
		return ""
	if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
		raise ToolError(
			f"{key} must be 64 hexadecimal characters — that is what a SHA-256 digest is. Got "
			f"{len(text)} character(s). Nothing was staged."
		)
	return text


def _decode_chunk(raw: str, chunk_index: int) -> bytes:
	"""One piece's bytes, with the commonest way of getting this wrong named."""
	cleaned = "".join(str(raw).split())
	try:
		content = base64.b64decode(cleaned, validate=True)
	except Exception as exc:
		raise ToolError(
			f"chunk {chunk_index} is not valid base64 ({type(exc).__name__}). Each chunk_base64 "
			"must be the base64 of ITS OWN slice of the file's bytes. If you base64'd the whole "
			"file and then cut the resulting STRING into pieces, the middle pieces are not valid "
			"base64 on their own and this is what that looks like. Cut the bytes first, encode "
			"each slice second. Nothing was staged."
		) from None
	if not content:
		raise ToolError(f"chunk {chunk_index} decoded to zero bytes. Nothing was staged.")
	return content


def _open_session(session_id: str, total_chunks: int, expected_sha256: str, expected_size):
	"""The session for this id, created on the first piece and vetted on every one."""
	existing = frappe.db.get_value(SESSION_DOCTYPE, {"session_id": session_id}, "name")
	if not existing:
		# v0.18.3: `ignore_permissions=True` — see the chunk insert below for
		# the reasoning. Session and chunk share the same permission story.
		return frappe.get_doc(
			{
				"doctype": SESSION_DOCTYPE,
				"session_id": session_id,
				"total_chunks": total_chunks,
				"chunks_received": 0,
				"staged_bytes": 0,
				"expected_sha256": expected_sha256,
				"expected_size": expected_size or 0,
			}
		).insert(ignore_permissions=True)

	doc = frappe.get_doc(SESSION_DOCTYPE, existing)
	_assert_owner(doc, session_id, "Nothing was staged.")
	if int(doc.total_chunks or 0) != total_chunks:
		raise ToolError(
			f"upload {session_id!r} was opened declaring {doc.total_chunks} piece(s) and this "
			f"call says {total_chunks}. A piece count that changes mid-upload means the pieces "
			"already staged belong to a different file. Cancel it with cancel_staged_upload and "
			"start again, or use a session id of its own. Nothing was staged."
		)
	_merge_expectation(doc, "expected_sha256", expected_sha256, session_id)
	_merge_expectation(doc, "expected_size", expected_size, session_id)
	return doc


def _merge_expectation(doc, field: str, value, session_id: str) -> None:
	"""Let a later call supply an expectation, and refuse one that contradicts.

	A caller that only knows the whole file's hash after the last slice should be
	able to say so on the last call. A caller whose hash CHANGES mid-upload is
	describing a different file, and quietly taking the newest answer would make
	the check the caller asked for meaningless.
	"""
	if value in (None, "", 0):
		return
	current = doc.get(field)
	if current in (None, "", 0):
		doc.set(field, value)
		return
	if str(current) != str(value):
		raise ToolError(
			f"upload {session_id!r} already expects {field}={current!r} and this call says "
			f"{value!r}. Two different answers to 'what should the finished file be' means one "
			"of them is about a different file. Nothing was staged."
		)


def declare_expectations(session_id: str, expected_sha256: str = "", expected_size=None) -> dict:
	"""Record what the caller says the finished file will be, before committing.

	v0.17.1. `_merge_expectation` already lets a later `stage_file_chunk` supply a
	hash the caller only learned after the last slice — its docstring makes that
	argument. A client that learns it at FINALISE time rather than at staging
	time has the same honest need, and the Farm Ops app is one: it computes the
	SHA-256 at capture and sends it with the finalise call, not with the chunks.

	So this is the same merge, reachable from `api/files.finalize_staged_file`,
	and it inherits both of the properties that make the merge safe: a value that
	CONTRADICTS one already recorded is refused rather than quietly taking the
	newest answer, and the ownership check runs first. Without it the app's
	sha256 would arrive too late to be checked and the audit trail would record
	a hash nothing had verified — which is worse than recording none.
	"""
	session_id = _session_id(session_id)
	row = _require_session(session_id, "Nothing was changed.")
	doc = frappe.get_doc(SESSION_DOCTYPE, row["name"])
	_merge_expectation(
		doc,
		"expected_sha256",
		_sha256_arg({"expected_sha256": expected_sha256}, "expected_sha256"),
		session_id,
	)
	_merge_expectation(
		doc,
		"expected_size",
		as_int({"expected_size": expected_size}, "expected_size"),
		session_id,
	)
	doc.save(ignore_permissions=True)  # v0.18.3 — see _open_session
	return {
		"session_id": session_id,
		"expected_sha256": doc.get("expected_sha256") or None,
		"expected_size": int(doc.get("expected_size") or 0) or None,
	}


def _require_session(session_id: str, tail: str) -> dict:
	row = frappe.db.get_value(
		SESSION_DOCTYPE,
		{"session_id": session_id},
		compat.existing_fields(
			SESSION_DOCTYPE,
			(
				"name",
				"session_id",
				"total_chunks",
				"chunks_received",
				"staged_bytes",
				"expected_sha256",
				"expected_size",
				"owner",
			),
		),
		as_dict=True,
	)
	if not row:
		raise ToolError(
			f"no staged upload called {session_id!r}. It may have been committed already, "
			f"cancelled, or swept after {SESSION_TTL_HOURS} hours idle. list_staged_uploads "
			f"shows what is in flight. {tail}"
		)
	_assert_owner(row, session_id, tail)
	return dict(row)


def _assert_owner(row, session_id: str, tail: str) -> None:
	"""A session belongs to whoever staged its first piece, and to nobody else.

	Not paranoia about other operators. Two callers who happened to pick the same
	session id would otherwise interleave their pieces into one file, and the
	failure would present as corruption rather than as the collision it is.
	"""
	owner = str(row.get("owner") or "")
	user = frappe.session.user
	if owner and owner != user:
		raise ToolError(
			f"staged upload {session_id!r} belongs to {owner}, not {user}. An upload is only "
			"ever finished by the user who started it — pieces from two callers assembled into "
			f"one file would be corruption that looks like nothing. {tail}"
		)


def _chunk_row(session: str, chunk_index: int):
	return frappe.db.get_value(
		CHUNK_DOCTYPE,
		{"session": session, "chunk_index": chunk_index},
		["name", "chunk_bytes"],
		as_dict=True,
		order_by=None,
	)


def _received_indexes(session: str) -> list:
	"""Which piece numbers are staged. Deliberately does NOT read the payloads."""
	rows = frappe.db.get_all(
		CHUNK_DOCTYPE,
		filters={"session": session},
		fields=["chunk_index"],
		limit=MAX_TOTAL_CHUNKS + 1,
	)
	return sorted({int(row["chunk_index"]) for row in rows or []})


def _missing_indexes(received: list, total: int) -> list:
	present = set(received)
	return [index for index in range(total) if index not in present]


def _ranges(indexes: list) -> list:
	"""Compact a list of piece numbers into readable runs: [0,1,2,7] → ['0-2', '7'].

	A missing-pieces message that reads `3, 4, 5, 6, ... 208 more` is a message
	nobody acts on. This is the difference between a refusal and a to-do list.
	"""
	out: list = []
	for index in indexes:
		if out and index == out[-1][1] + 1:
			out[-1][1] = index
		else:
			out.append([index, index])
	return [str(low) if low == high else f"{low}-{high}" for low, high in out]


def _assemble(session: str, total_chunks: int) -> bytes:
	"""The staged pieces, decoded and concatenated in index order.

	Reads the payloads one page at a time rather than all at once, so peak memory
	is the file plus one page rather than the file plus its base64 — which for a
	90 MB upload is the difference between 90 MB and 210 MB.
	"""
	page = 25
	pieces: list = []
	for start in range(0, total_chunks, page):
		rows = frappe.db.get_all(
			CHUNK_DOCTYPE,
			# A list of conditions rather than a dict, because a dict cannot carry
			# two conditions on one column and a window needs both ends.
			filters=[
				["session", "=", session],
				["chunk_index", ">=", start],
				["chunk_index", "<", start + page],
			],
			fields=["chunk_index", "chunk_base64"],
			order_by="chunk_index asc",
			limit=page,
		)
		for row in sorted(rows or [], key=lambda row: int(row["chunk_index"])):
			pieces.append(base64.b64decode("".join(str(row["chunk_base64"]).split()), validate=True))
	return b"".join(pieces)


def _verify(session: dict, content: bytes, digest: str, session_id: str, tail: str) -> None:
	"""The caller's own answers about the finished file, checked against it."""
	expected_size = int(session.get("expected_size") or 0)
	if expected_size and expected_size != len(content):
		raise ToolError(
			f"upload {session_id!r} was declared as {expected_size} bytes and the staged pieces "
			f"assemble to {len(content)}. Every declared piece arrived, so the count the caller "
			"computed and the pieces the caller sent are describing different files — the likely "
			"cause is a slice that was cut or encoded twice. Every piece carries the hash of its "
			f"own bytes; compare those against what you sent. {tail}"
		)
	expected_sha256 = str(session.get("expected_sha256") or "")
	if expected_sha256 and expected_sha256 != digest:
		raise ToolError(
			f"upload {session_id!r} was declared with sha256 {expected_sha256} and the staged "
			f"pieces assemble to {digest}. The bytes on this site are not the bytes that were "
			"sent. Every piece carries the hash of its own content — compare those against what "
			"you sent to find which call carried the bad one, re-stage that piece, and commit "
			f"again. {tail}"
		)


def _commit_target(args: dict, as_governance: bool, file_name: str, tail: str) -> dict:
	"""Where the assembled file is going, vetted before a byte is reassembled."""
	doctype = as_str(args, "attach_to_doctype")
	name = as_str(args, "attach_to_name")

	if as_governance:
		if doctype or name:
			raise ToolError(
				"governance_document=true files a NEW Governance Document and attaches the file "
				"to that, so attach_to_doctype and attach_to_name have nothing to point at. Pass "
				f"one or the other, not both. {tail}"
			)
		if not as_str(args, "title") or not as_str(args, "category"):
			raise ToolError(
				"governance_document=true needs at least `title` and `category` — an archive "
				"entry with no title is one nobody can find and one with no category is one "
				f"nobody can file. {tail}"
			)
		return {"destination": "a new Governance Document", "doctype": "", "name": ""}

	if bool(doctype) != bool(name):
		raise ToolError(
			f"attach_to_doctype is {doctype!r} and attach_to_name is {name!r}. Give both to "
			"attach the file to a document, or neither to leave it on the site unattached. "
			f"{tail}"
		)
	if not doctype:
		return {"destination": "the site, attached to nothing", "doctype": "", "name": ""}

	files.check_attachable(
		doctype,
		name,
		file_name,
		allow_cancelled=bool(as_bool(args, "allow_cancelled", False)),
		company=as_str(args, "company"),
		tail=tail,
	)
	return {"destination": f"{doctype} {name}", "doctype": doctype, "name": name}


def _clear_session(session: str) -> int:
	"""Delete a session and every piece staged against it. Returns the piece count."""
	rows = frappe.db.get_all(
		CHUNK_DOCTYPE, filters={"session": session}, fields=["name"], limit=MAX_TOTAL_CHUNKS + 1
	)
	for row in rows or []:
		frappe.delete_doc(CHUNK_DOCTYPE, row["name"], force=True, ignore_permissions=True)
	frappe.delete_doc(SESSION_DOCTYPE, session, force=True, ignore_permissions=True)
	return len(rows or [])


# ── the in-app door onto the same pipeline ──────────────────────────────────
def stage_internal_bytes(content: bytes, session_id: str, chunk_bytes: int = INTERNAL_CHUNK_BYTES) -> dict:
	"""Run bytes this SITE produced through the staging tables, then hand them back.

	WHY A SERVER-SIDE PRODUCER WOULD WANT THIS AT ALL, since the bytes never cross
	the MCP boundary and no chunking is needed to get them here. The answer is not
	transport, it is CHECKPOINTING. `generate_audit_packet` assembles a document
	out of several hundred records and then has to write it; a worker killed
	between "assembled" and "written" loses the assembly and the caller finds out
	as a timeout. Staged in pieces first, the same failure leaves a resumable
	session and a per-piece digest naming exactly how far it got.

	For a two-hundred-kilobyte packet that is ceremony, and `generate_audit_packet`
	says so and lets a caller turn it off. For the forty-megabyte FSMA packet a
	real season produces it is the difference between a retry and a mystery.

	Returns `{"session": <docname>, "chunks": n, "sha256": ..., "content": bytes}`
	with the content READ BACK OUT of staging rather than passed through — so a
	round trip that corrupted anything is caught here rather than in a PDF nobody
	can open.

	The caller is responsible for `clear_internal_session`. Deliberately: the
	session has to outlive this function for the checkpoint to be worth anything,
	and it is cleared once the File exists.
	"""
	_require_doctypes()
	session_id = _session_id(session_id)
	if len(content) > MAX_TOTAL_BYTES:
		raise ToolError(
			f"this document is {files.human_size(len(content))}, over the "
			f"{files.human_size(MAX_TOTAL_BYTES)} ceiling for one assembled file. Narrow the "
			"period. Nothing was staged."
		)
	chunk_bytes = max(1, int(chunk_bytes))
	pieces = [content[start : start + chunk_bytes] for start in range(0, len(content), chunk_bytes)] or [b""]
	if len(pieces) > MAX_TOTAL_CHUNKS:
		# Widen the pieces rather than refuse. The per-piece ceiling exists because
		# a MODEL has to compose the argument, and nothing here is composed by a
		# model — these bytes came off this site's own disk.
		chunk_bytes = (len(content) // MAX_TOTAL_CHUNKS) + 1
		pieces = [content[start : start + chunk_bytes] for start in range(0, len(content), chunk_bytes)]

	digest = hashlib.sha256(content).hexdigest()
	if frappe.db.exists(SESSION_DOCTYPE, {"session_id": session_id}):
		# A previous run that died after staging and before committing. Its pieces
		# are of a document assembled from data that may since have changed, so they
		# are discarded rather than resumed — a packet half from Tuesday and half
		# from Thursday would be worse than either.
		row = frappe.db.get_value(SESSION_DOCTYPE, {"session_id": session_id}, "name")
		_clear_session(row)

	session = _open_session(session_id, len(pieces), digest, len(content))
	staged = 0
	for index, piece in enumerate(pieces):
		encoded = base64.b64encode(piece).decode("ascii")
		frappe.get_doc(
			{
				"doctype": CHUNK_DOCTYPE,
				"session": session.name,
				"chunk_index": index,
				"chunk_base64": encoded,
				"chunk_bytes": len(piece),
				"chunk_sha256": hashlib.sha256(piece).hexdigest(),
			}
		).insert(ignore_permissions=True)  # v0.18.3 — see _open_session
		staged += len(piece)
	session.chunks_received = len(pieces)
	session.staged_bytes = staged
	session.save(ignore_permissions=True)  # v0.18.3

	# Read it back out of staging rather than trusting the bytes we still hold.
	# A round trip that corrupted something is caught here, not in a PDF nobody
	# can open three weeks later.
	assembled = _assemble(session.name, len(pieces))
	if hashlib.sha256(assembled).hexdigest() != digest:  # pragma: no cover - a genuinely broken site
		_clear_session(session.name)
		raise ToolError(
			"the document did not survive a round trip through this site's own staging tables. "
			"That is a database problem rather than a compliance one. Nothing was written."
		)
	return {"session": session.name, "chunks": len(pieces), "sha256": digest, "content": assembled}


def clear_internal_session(session: str) -> int:
	"""Delete a staging session once the File it built exists."""
	try:
		return _clear_session(session)
	except Exception:
		return 0

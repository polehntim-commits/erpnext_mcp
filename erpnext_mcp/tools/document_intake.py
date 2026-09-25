# SPDX-License-Identifier: MIT
"""Document intake — email with attachments, read and routed onto the record it belongs to.

v0.177.0. ERPNext's Email Account already pulls office@ into `Communication`, and
every attachment on a message is a `File` hanging off that Communication. What
was missing is the step after: somebody reading the licence renewal, the
training certificate, the fuel receipt, and putting it on the Employee, the
Certification, the Expense Receipt it is evidence for. These four tools are that
step for an agent.

THE QUEUE IS THE UNLINKED ONES. A received Communication with an attachment and
no `reference_doctype` is one nobody has filed yet — ERPNext itself links a
message it can place (a reply on an Issue, an email to an invoice's thread), so
what is left over is exactly the pile. `sent_or_received = "Received"` and
`communication_type = "Communication"` narrow it further: an email this site SENT
with an attachment and no reference is outgoing mail, not intake.

ROUTING MOVES NO BYTES THROUGH THE TOOL CALL. Each attachment goes onto the
target through `files.attach_file_to_document` by its `file_url`, the path that
tool already had for a file that lives on the server. Frappe's File insert
reads the blob off disk, finds the email's own File by content hash and reuses
its `file_url` — read out of `frappe/core/doctype/file/file.py` in the shipped
image, `save_file` — so the site keeps ONE copy with two File rows over it,
and a PDF never passes through base64 on the way. That is the whole point: a
round trip through a model's context is where an attachment gets corrupted.

LINKING THE MESSAGE IS FRAPPE'S OWN "RELINK". `frappe.email.relink` — the Desk's
Relink button — checks write on the Communication and sets `reference_doctype`,
`reference_name` and `status = "Linked"` directly, WITHOUT a save. That matters:
a Communication save runs `update_parent_document_on_communication`, which can
flip the target's status as though a customer had replied. This does what Relink
does and no more.

THE AUDIT TRAIL IS A COMMENT ON THE MESSAGE, and `list_document_intake_log` is
built from those comments rather than from linked Communications — every sent
invoice email is a linked Communication with an attachment, and none of them
was routed by anybody. The comment is the only thing that says the agent did it.
"""

from __future__ import annotations

import mimetypes
import re
from html import unescape
from html.parser import HTMLParser

import frappe

from ..args import as_date, as_limit, as_str
from ..errors import ToolError
from ..result import ToolResult
from . import files

COMMUNICATION = "Communication"

#: The first words of every routing comment. `list_document_intake_log` finds
#: its rows by this prefix, so changing it orphans every routing already logged.
AUDIT_PREFIX = "Document Intake Agent routed to "

#: A classification is a tag, not prose: lower-case words joined by underscores.
#: The shape is what lets the log parse it back out of a sentence — it can hold
#: no space and no full stop, and the comment ends it with one.
CLASSIFICATION_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

#: PDFs at or under this size come back inline from `get_incoming_document`.
#: `get_attachment_content`'s own default ceiling, so the two agree on what fits.
INLINE_PDF_MAX_BYTES = files.DEFAULT_MAX_BYTES

DEFAULT_QUEUE_LIMIT = 20
DEFAULT_LOG_LIMIT = 50

_AUDIT_LINE = re.compile(
	r"^" + re.escape(AUDIT_PREFIX) + r"(?P<target>.+) as (?P<classification>[a-z][a-z0-9_]*)\.$"
)


# ── list_incoming_documents ─────────────────────────────────────────────────
def list_incoming_documents(args: dict) -> ToolResult:
	"""Received email with attachments that nobody has filed yet, newest first."""
	sender = as_str(args, "sender")
	since_date = as_date(args, "since_date")
	limit = _limit(args, DEFAULT_QUEUE_LIMIT)

	filters = _unrouted_filters()
	if sender:
		filters["sender"] = ["like", f"%{sender}%"]
	if since_date:
		filters["creation"] = [">=", since_date]

	rows = frappe.get_list(
		COMMUNICATION,
		filters=filters,
		fields=["name", "subject", "sender", "creation"],
		order_by="creation desc",
		limit_page_length=limit,
	)
	attached = _files_by_communication([row["name"] for row in rows])
	documents = []
	for row in rows:
		names = [item.get("file_name") for item in attached.get(row["name"], [])]
		documents.append(
			{
				"name": row["name"],
				"subject": row.get("subject"),
				"sender": row.get("sender"),
				"creation": str(row.get("creation") or ""),
				"attachment_count": len(names),
				"attachment_filenames": names,
			}
		)

	data = {
		"documents": documents,
		"count": len(documents),
		"limit": limit,
		"truncated": len(documents) == limit,
		"filters": {"sender": sender or None, "since_date": since_date},
		"next_step": (
			"get_incoming_document with a `name` reads the message and its attachments; "
			"route_incoming_document files it onto the record it belongs to."
			if documents
			else "Nothing is waiting: every received email with an attachment is linked to a record."
		),
	}
	return ToolResult(data, f"{len(documents)} unrouted incoming document(s)")


# ── get_incoming_document ───────────────────────────────────────────────────
def get_incoming_document(args: dict) -> ToolResult:
	"""One received email: its text, who sent it, and every attachment on it."""
	name = as_str(args, "name", required=True)
	doc = _communication(name)
	if not frappe.has_permission(COMMUNICATION, "read", doc=name):
		raise ToolError(f"{frappe.session.user} is not permitted to read Communication {name}.")

	attachments = []
	for row in _files_by_communication([name]).get(name, []):
		entry = {
			"name": row.get("name"),
			"file_name": row.get("file_name"),
			"file_url": row.get("file_url"),
			"file_size": int(row.get("file_size") or 0),
			"size_human": files.human_size(row.get("file_size")),
			"mime_type": mimetypes.guess_type(str(row.get("file_name") or ""))[0],
			"is_private": bool(row.get("is_private")),
		}
		if entry["mime_type"] == "application/pdf" and entry["file_size"] <= INLINE_PDF_MAX_BYTES:
			# `get_attachment_content` itself, so the permission check, the size
			# ceilings and the encoding are the ones every other reader gets.
			try:
				payload = files.get_attachment_content({"name": row.get("name")}).data
				entry["encoding"] = payload["encoding"]
				entry["content_base64"] = payload["content_base64"]
			except ToolError as exc:
				# One unreadable PDF must not hide the message and the other files.
				entry["content_error"] = str(exc)
		attachments.append(entry)

	data = {
		"name": doc.name,
		"subject": doc.get("subject"),
		"sender": doc.get("sender"),
		"sender_full_name": doc.get("sender_full_name"),
		"recipients": doc.get("recipients"),
		"creation": str(doc.get("creation") or ""),
		"content": html_to_text(doc.get("content")),
		"attachments": attachments,
		"attachment_count": len(attachments),
		"reference_doctype": doc.get("reference_doctype") or None,
		"reference_name": doc.get("reference_name") or None,
		"routed": bool(doc.get("reference_doctype")),
		"note": (
			f"PDFs up to {files.human_size(INLINE_PDF_MAX_BYTES)} come back as `content_base64`; "
			"anything else is named with its file_url. route_incoming_document moves the files "
			"by file_url, so nothing here has to be sent back."
		),
	}
	return ToolResult(
		data,
		f"Communication {doc.name}: {doc.get('subject') or '(no subject)'} from {doc.get('sender')}, "
		f"{len(attachments)} attachment(s)",
	)


# ── route_incoming_document ─────────────────────────────────────────────────
def route_incoming_document(args: dict) -> ToolResult:
	"""File every attachment on a received email onto one record, and link the email to it."""
	name = as_str(args, "communication", required=True)
	target_doctype = as_str(args, "target_doctype", required=True)
	target_name = as_str(args, "target_name", required=True)
	classification = as_str(args, "classification", required=True).lower()
	note = as_str(args, "note")

	if not CLASSIFICATION_RE.match(classification):
		raise ToolError(
			f"classification {classification!r} must be a lower-case tag of letters, digits and "
			"underscores, e.g. training_certificate, expense_receipt, applicator_license. "
			"Nothing was routed."
		)
	doc = _communication(name, tail="Nothing was routed.")
	if doc.get("reference_doctype"):
		raise ToolError(
			f"Communication {name} is already linked to {doc.get('reference_doctype')} "
			f"{doc.get('reference_name')}. Routing it again would put a second copy of every "
			"attachment on another record and overwrite the link. Relink it in the Desk if "
			"the first filing was wrong. Nothing was routed."
		)
	if target_doctype == COMMUNICATION:
		raise ToolError("a Communication cannot be routed onto another Communication. Nothing was routed.")
	if not frappe.has_permission(COMMUNICATION, "write", doc=name):
		raise ToolError(
			f"{frappe.session.user} is not permitted to write Communication {name}, so it "
			"cannot be linked to a record. Nothing was routed."
		)

	sources = _files_by_communication([name]).get(name, [])
	if not sources:
		raise ToolError(
			f"Communication {name} has no attached files"
			+ (" although it is flagged has_attachment" if doc.get("has_attachment") else "")
			+ ", so there is nothing to route. Nothing was routed."
		)

	# Every check `attach_file_to_document` makes — the target exists, the caller
	# may write it, it is not cancelled, the extension is allowed, the name is not
	# already on it, max_attachments — runs per file below. The first refusal
	# raises, and the dispatcher rolls back the files already attached before it.
	routed = []
	for source in sources:
		result = files.attach_file_to_document(
			{
				"doctype": target_doctype,
				"name": target_name,
				"file_name": source.get("file_name"),
				"file_url": source.get("file_url"),
				"is_private": bool(source.get("is_private")),
			}
		).data
		routed.append(
			{
				"source_file": source.get("name"),
				"file": result.get("file"),
				"file_name": source.get("file_name"),
				"file_url": result.get("file_url"),
			}
		)

	frappe.db.set_value(
		COMMUNICATION,
		name,
		{"reference_doctype": target_doctype, "reference_name": target_name, "status": "Linked"},
	)
	text = f"{AUDIT_PREFIX}{target_doctype} {target_name} as {classification}."
	if note:
		text += f"\nNote: {note}"
	comment = doc.add_comment("Comment", text)

	data = {
		"communication": name,
		"target_doctype": target_doctype,
		"target_name": target_name,
		"classification": classification,
		"attachments_routed": len(routed),
		"file_names": [row["file_name"] for row in routed],
		"files": routed,
		"audit_comment": getattr(comment, "name", None),
		"note": (
			f"Each file is a new File row on {target_doctype} {target_name} over the SAME stored "
			"blob as the email's — nothing was re-encoded or re-uploaded. The email is linked "
			"to the record and leaves list_incoming_documents; list_document_intake_log shows it."
		),
	}
	return ToolResult(
		data,
		f"routed Communication {name} to {target_doctype} {target_name} as {classification}: "
		f"{len(routed)} file(s)",
		docstatus_delta=f"none → 0 (File created) × {len(routed)}",
	)


# ── list_document_intake_log ────────────────────────────────────────────────
def list_document_intake_log(args: dict) -> ToolResult:
	"""What the intake agent has routed, newest first, read off its own audit comments."""
	classification = as_str(args, "classification").lower()
	target_doctype = as_str(args, "target_doctype")
	since_date = as_date(args, "since_date")
	limit = _limit(args, DEFAULT_LOG_LIMIT)

	# The prefix and the two filters narrow in SQL; `_AUDIT_LINE` below is the
	# exact test. A classification is a slug that can hold no full stop, so
	# " as <tag>." cannot match a longer tag that merely starts the same way.
	pattern = AUDIT_PREFIX + (f"{target_doctype} " if target_doctype else "") + "%"
	if classification:
		pattern += f" as {classification}.%"
	filters = {
		"reference_doctype": COMMUNICATION,
		"comment_type": "Comment",
		"content": ["like", pattern],
	}
	if since_date:
		filters["creation"] = [">=", since_date]
	comments = frappe.db.get_all(
		"Comment",
		filters=filters,
		fields=["name", "reference_name", "content", "creation", "owner"],
		order_by="creation desc",
		limit_page_length=limit,
	)

	parsed = []
	for comment in comments:
		lines = str(comment.get("content") or "").splitlines() or [""]
		match = _AUDIT_LINE.match(lines[0].strip())
		if not match:
			continue
		if classification and match["classification"] != classification:
			continue
		note_line = next((line for line in lines[1:] if line.startswith("Note: ")), "")
		parsed.append((comment, match["classification"], note_line[len("Note: ") :] or None))

	names = sorted({str(comment.get("reference_name")) for comment, _, _ in parsed})
	messages = {}
	if names:
		for row in frappe.get_list(
			COMMUNICATION,
			filters={"name": ["in", names]},
			fields=["name", "subject", "sender", "creation", "reference_doctype", "reference_name"],
			limit_page_length=len(names),
		):
			messages[row["name"]] = row

	entries = []
	for comment, tag, note in parsed:
		message = messages.get(str(comment.get("reference_name")))
		if message is None:
			# Deleted since, or not readable by this account. The comment alone
			# is not enough to report a routing someone may not see.
			continue
		if target_doctype and message.get("reference_doctype") != target_doctype:
			continue
		entries.append(
			{
				"communication": message["name"],
				"subject": message.get("subject"),
				"sender": message.get("sender"),
				"creation": str(message.get("creation") or ""),
				"reference_doctype": message.get("reference_doctype"),
				"reference_name": message.get("reference_name"),
				"classification": tag,
				"note": note,
				"routed_at": str(comment.get("creation") or ""),
				"routed_by": comment.get("owner"),
			}
		)

	data = {
		"entries": entries,
		"count": len(entries),
		"limit": limit,
		"truncated": len(comments) == limit,
		"filters": {
			"classification": classification or None,
			"target_doctype": target_doctype or None,
			"since_date": since_date,
		},
	}
	return ToolResult(data, f"{len(entries)} routed document(s) in the intake log")


# ── helpers ─────────────────────────────────────────────────────────────────
def _limit(args: dict, default: int) -> int:
	"""`as_limit` with this tool's own default, which is smaller than the app's."""
	raw = args.get("limit")
	return as_limit({"limit": default if raw in (None, "") else raw})


def _unrouted_filters() -> dict:
	return {
		"communication_type": "Communication",
		"sent_or_received": "Received",
		"has_attachment": 1,
		# "is not set" is Frappe's own NULL-or-empty test.
		"reference_doctype": ["is", "not set"],
	}


def _communication(name: str, tail: str = ""):
	if not frappe.db.exists(COMMUNICATION, name):
		raise ToolError(f"no Communication named {name!r} on this site. {tail}".strip())
	return frappe.get_doc(COMMUNICATION, name)


def _files_by_communication(names: list) -> dict:
	"""The File rows on each Communication, oldest first — the order the message carried them."""
	if not names:
		return {}
	rows = frappe.db.get_all(
		"File",
		filters={
			"attached_to_doctype": COMMUNICATION,
			"attached_to_name": ["in", list(names)],
			"is_folder": 0,
		},
		fields=["name", "file_name", "file_url", "file_size", "is_private", "attached_to_name"],
		order_by="creation asc",
	)
	grouped: dict = {}
	for row in rows or []:
		grouped.setdefault(str(row.get("attached_to_name")), []).append(row)
	return grouped


class _TextExtractor(HTMLParser):
	"""Visible text out of an email body. Block elements become line breaks."""

	_BREAKS = frozenset({"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"})
	_HIDDEN = frozenset({"script", "style", "head", "title"})

	def __init__(self):
		super().__init__(convert_charrefs=True)
		self.parts: list = []
		self._hidden = 0

	def handle_starttag(self, tag, attrs):
		if tag in self._HIDDEN:
			self._hidden += 1
		elif tag in self._BREAKS:
			self.parts.append("\n")

	def handle_endtag(self, tag):
		if tag in self._HIDDEN:
			self._hidden = max(0, self._hidden - 1)
		elif tag in self._BREAKS:
			self.parts.append("\n")

	def handle_data(self, data):
		if not self._hidden:
			self.parts.append(data)


def html_to_text(html) -> str:
	"""An email's HTML body as plain text: tags gone, entities decoded, blank runs collapsed."""
	raw = str(html or "")
	if "<" not in raw:
		return unescape(raw).strip()
	parser = _TextExtractor()
	parser.feed(raw)
	parser.close()
	lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
	text = "\n".join(lines)
	return re.sub(r"\n{3,}", "\n\n", text).strip()

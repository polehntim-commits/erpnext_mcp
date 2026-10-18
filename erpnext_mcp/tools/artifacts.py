# SPDX-License-Identifier: MIT
"""Where a generated document goes: onto a record, and — carefully — onto disk.

TWO DESTINATIONS, AND ONLY ONE OF THEM IS THE DELIVERABLE. A generated report's
durable home is a private File attached to a Governance Document: permissioned,
version-tracked, backed up with the site, and readable through
`get_attachment_content` with the same permission check every other attachment
gets. `output_path` is the second destination, and it exists for one reason — a
batch of thirty 1099 forms that somebody has to print is thirty round trips
through base64 otherwise.

THE FILESYSTEM RULE, AND WHY IT IS THIS STRICT. A tool that writes to an
arbitrary path is a tool that can write anywhere the bench user can: into
`sites/*/site_config.json`, into an app's source tree, over a backup. So the
allowed area is exactly the two directories a Frappe site already treats as file
storage — `<site>/private/files` and `<site>/public/files` — and everything else
is refused, naming the roots. A relative path lands under `private/files`. A
symlink cannot be used to escape, because the check is on the *resolved* real
path, not the one that was typed. And an existing file is never overwritten
unless the caller says so, because the most likely thing at a path a report
generator was pointed at twice is last quarter's report.

Nothing here decides *whether* a file may be written. That is the tool switch,
and the dispatcher has already asked.
"""

from __future__ import annotations

import hashlib
import os

import frappe

from ..errors import ToolError

#: The only directories under the site folder a generated artefact may be
#: written into. Both are storage Frappe already owns and already backs up.
ALLOWED_SUBDIRS = (("private", "files"), ("public", "files"))


def allowed_roots() -> list[str]:
	"""Absolute, symlink-resolved paths of the writable directories."""
	roots = []
	for parts in ALLOWED_SUBDIRS:
		try:
			root = frappe.get_site_path(*parts)
		except Exception:  # pragma: no cover - a site path we cannot resolve
			continue
		roots.append(os.path.realpath(root))
	return roots


def _inside(path: str, root: str) -> bool:
	"""Is `path` inside `root`? Compared on separators, not on a bare prefix.

	`/a/files-old/x` starts with `/a/files` and is not inside it. A prefix test
	would call that containment, which is exactly the shape of an escape.
	"""
	root = root.rstrip(os.sep)
	return path == root or path.startswith(root + os.sep)


def resolve_output_path(output_path: str, default_name: str) -> str | None:
	"""Turn an `output_path` argument into an absolute path inside the site, or refuse.

	Returns None when no path was asked for, which is the ordinary case: the
	attachment is the deliverable. A path naming a directory — trailing separator,
	or an existing directory — gets `default_name` appended, so a caller can point
	at a folder and let the tool name the file.
	"""
	output_path = (output_path or "").strip()
	if not output_path:
		return None

	roots = allowed_roots()
	if not roots:  # pragma: no cover - a site with no resolvable file directories
		raise ToolError(
			"this site has no resolvable private/files directory, so output_path cannot be "
			"honoured. Take the attachment instead — it is the durable copy either way."
		)

	wants_directory = output_path.endswith(("/", os.sep))
	candidate = output_path if os.path.isabs(output_path) else os.path.join(roots[0], output_path)
	candidate = os.path.realpath(candidate)
	if wants_directory or os.path.isdir(candidate):
		candidate = os.path.join(candidate, default_name)

	target = os.path.realpath(candidate)
	parent = os.path.realpath(os.path.dirname(target))
	if not any(_inside(target, root) and _inside(parent, root) for root in roots):
		raise ToolError(
			f"output_path {output_path!r} resolves to {target!r}, which is outside this site's "
			f"file storage. Generated documents may only be written under: "
			f"{', '.join(roots)}. Pass a relative path — it lands under private/files — or omit "
			"output_path entirely and read the attachment back with get_attachment_content. "
			"Nothing was written."
		)
	if os.path.basename(target) in ("", "."):  # pragma: no cover - defensive
		raise ToolError(f"output_path {output_path!r} does not name a file. Nothing was written.")
	return target


def resolve_output_dir(output_path: str) -> str | None:
	"""Same rule as `resolve_output_path`, for a tool that writes several files.

	A 1099 run produces a workbook and a form per recipient, so its `output_path`
	names a folder rather than a file — one meaning per argument, rather than an
	argument that means a directory sometimes and a filename other times.
	"""
	output_path = (output_path or "").strip()
	if not output_path:
		return None
	roots = allowed_roots()
	if not roots:  # pragma: no cover - a site with no resolvable file directories
		raise ToolError(
			"this site has no resolvable private/files directory, so output_path cannot be "
			"honoured. Take the attachments instead — they are the durable copies either way."
		)
	candidate = output_path if os.path.isabs(output_path) else os.path.join(roots[0], output_path)
	target = os.path.realpath(candidate)
	if not any(_inside(target, root) for root in roots):
		raise ToolError(
			f"output_path {output_path!r} resolves to {target!r}, which is outside this site's "
			f"file storage. Generated documents may only be written under: {', '.join(roots)}. "
			"Pass a relative path — it lands under private/files — or omit output_path and read "
			"the attachments back with get_attachment_content. Nothing was written."
		)
	if os.path.exists(target) and not os.path.isdir(target):
		raise ToolError(
			f"output_path {output_path!r} is an existing file, and this tool writes several "
			"documents — it needs a directory. Nothing was written."
		)
	return target


def write_output(path: str, payload: bytes, overwrite: bool) -> dict:
	"""Write `payload` to an already-resolved path. Refuses to clobber by default."""
	existed = os.path.exists(path)
	if existed and not overwrite:
		raise ToolError(
			f"{path} already exists. The most likely thing at a path a report generator was "
			"pointed at twice is the previous report, so this refuses rather than replaces it. "
			"Pass overwrite=true, or choose another name. Nothing was written."
		)
	directory = os.path.dirname(path)
	if directory:
		os.makedirs(directory, exist_ok=True)
	with open(path, "wb") as handle:
		handle.write(payload)
	return {
		"path": path,
		"bytes": len(payload),
		"sha256": hashlib.sha256(payload).hexdigest(),
		"overwrote": existed,
	}


def attach_bytes(
	doctype: str,
	name: str,
	file_name: str,
	content: bytes,
	field: str = "",
	*,
	parent_authorized: bool = False,
):
	"""Store `content` as a PRIVATE File attached to one document.

	Private is not an argument. A quarterly report names positions and balances,
	and a 1099 names people and what they were paid; neither belongs at a public
	URL because somebody passed the wrong flag.

	`parent_authorized` (v0.336.0) is `files.insert_attachment`'s: True only once the caller's own gate
	has passed, because Frappe 15.12x otherwise asks for write on the parent — which a worker fetching
	their own pay stub, or HR rendering a W-4 they may not edit in the Desk, does not hold.
	"""
	payload = {
		"doctype": "File",
		"file_name": file_name,
		"is_private": 1,
		"content": content,
		"attached_to_doctype": doctype,
		"attached_to_name": name,
	}
	if field:
		payload["attached_to_field"] = field
	attachment = frappe.get_doc(payload)
	if parent_authorized:
		attachment.flags.ignore_permissions = True
	attachment = attachment.insert(ignore_permissions=True) if parent_authorized else attachment.insert()
	if field:
		frappe.db.set_value(doctype, name, field, attachment.get("file_url"))
	return attachment


def describe_attachment(attachment, content: bytes) -> dict:
	"""The metadata a result carries about a file it produced. Never the bytes.

	A tool that returned the PDF inline would put a megabyte of base64 into a
	context window to say something the file_url says in forty characters.
	"""
	return {
		"name": attachment.name,
		"file_name": attachment.get("file_name"),
		"file_url": attachment.get("file_url"),
		"file_size": len(content),
		"sha256": hashlib.sha256(content).hexdigest(),
		"is_private": True,
	}

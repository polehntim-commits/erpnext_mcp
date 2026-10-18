# SPDX-License-Identifier: MIT
"""The document either side of a signature: what was shown, and what was sealed.

v0.63.0. `tools/signing_evidence.py` names five steps that turn a drawn shape
into a record which survives a challenge, and says which module owns each. Two of
them had no owner:

  * STEP 1 — THE DOCUMENT WAS PRESENTED. The signer saw the form before signing
    it. `API_CONTRACT.md` §17.5 records why the app could not show them: both
    renderers answer with a private `file_url`, the handset authenticates to the
    sidecar with `X-FarmOps-Token` rather than to Frappe, and a private URL is a
    login page to it. So the app could show the COMPLETED form after signing —
    the bytes travel in `submit_form_signature`'s answer — and not the blank one
    before. `get_document_preview` is the read that closes that.

  * STEP 5 — THE ARTEFACT IS TAMPER-EVIDENT. `seal_signed_document` appends the
    verification page `pdf_seal.py` draws, hashes the finished file, and records
    the hash on the Signing Evidence rows the page describes.

────────────────────────────────────────────────────────────────────────────
WHY THESE TWO ARE ONE MODULE AND NEITHER IS IN `tools/signatures.py`
────────────────────────────────────────────────────────────────────────────

`signatures.py` owns THE WRITE: the closed table of signable boxes, the roster
gate, the identity check, the refusal to overwrite an attestation. Neither
function here writes a signature or can — the preview is a read and the seal
runs after the ink is already on the record — and putting them there would put
two paths that must never collect a signature inside the module whose whole job
is collecting one.

What they DO share with it is the vocabulary: which doctypes are forms, how a
caller's spelling of one resolves, and which function draws each. That lives in
`signatures.FORM_HANDLERS` and `DOCTYPE_ALIASES` and is READ from here rather
than copied, so a fourth form added there is a fourth form both of these handle.

────────────────────────────────────────────────────────────────────────────
THE PREVIEW IS A READ THAT WILL DRAW ONCE, AND THAT IS ARGUED FOR
────────────────────────────────────────────────────────────────────────────

`signatures._redraw` sets out at length why rendering a federal form nobody
asked for is this app deciding something that is not its to decide. The same
argument gives the answer here: a caller asking for a preview by name, in that
call, for the purpose of putting the page in front of the person about to sign
it, HAS asked. So the preview hands back the page already on the record, and
draws one only where the record has none — which on a fresh I-9 is every time,
and is the difference between this route working and it 404ing on the common
case.

WHAT IT WILL NOT DO IS SILENTLY REPLACE A PAGE THAT EXISTS. `render_i9_pdf`
refuses a second render without `overwrite` because the likeliest thing in that
field is the copy somebody already printed and had signed. A preview that
re-rendered on every screen open would repoint that field a dozen times a hire
day. So a stale page is REPORTED — `stale: true`, with the record's own modified
time beside it — and refreshed only when the caller passes `refresh`.
"""

from __future__ import annotations

import base64

import frappe

from .. import compat, pdf_seal
from ..args import as_bool, as_str
from ..errors import ToolError
from ..result import ToolResult
from . import artifacts, files, signatures, signing_evidence

#: The columns a sealed artefact writes back onto an evidence row. See
#: `_record_seal` for why these three may move on a row that is otherwise
#: immutable, and why nothing else on it ever does.
SEAL_FIELDS = ("sealed_pdf", "sealed_pdf_hash", "sealed_at")

#: Suffix on a sealed file's name, so a private files directory can be read by
#: eye and a sealed copy is never mistaken for the working page.
SEAL_STEM = "sealed"

#: The link column a signed form carries to the person it is about. Only forms
#: that HAVE one are cross-filed — an employer return is signed by an officer
#: and belongs to nobody's personnel folder, which is why this is looked up on
#: the doctype rather than assumed.
EMPLOYEE_LINK = "employee"

EMPLOYEE = "Employee"


# ── resolving which form ────────────────────────────────────────────────────
def resolve_document(args: dict) -> tuple[str, str]:
	"""`(doctype, docname)` for the form a caller named, or the refusal.

	THE SAME TWO REFUSALS `signatures._box` MAKES, and the second is the useful
	one: a caller naming a W-2 is not making a typo that a list of doctypes will
	fix, they are asking about a form with no signature line, and
	`UNSIGNED_REASONS` ends that conversation rather than starting a search.

	Resolution of the DOCNAME is delegated to the form's own resolver through
	`FORM_HANDLERS`, so `employee=` works here exactly as it works on
	`render_i9_pdf`, and a returning picker's form can be named by the person it
	belongs to rather than by a docname nobody on a handset has.
	"""
	raw = as_str(args, "document_type") or as_str(args, "doctype") or as_str(args, "form_doctype")
	unsigned = signatures.UNSIGNED_FORMS.get(raw.casefold())
	if unsigned:
		raise ToolError(
			f"{signatures.UNSIGNED_REASONS[unsigned]} There is no signature on a {unsigned} for "
			f"this call to be about. Nothing was read."
		)
	doctype = signatures.DOCTYPE_ALIASES.get(raw.casefold(), raw)
	if not doctype:
		raise ToolError(
			"document_type is required — the form: 'I-9 Form', 'W-4 Form' or 'Tax Form'. An "
			"alert or Farm Task raised from a missing-signature rule carries it in "
			"subject_doctype. Nothing was read."
		)
	handler = signatures.FORM_HANDLERS.get(doctype)
	if handler is None:
		raise ToolError(
			f"{doctype} is not a form this app renders. It renders these: "
			+ ", ".join(sorted(signatures.FORM_HANDLERS))
			+ ". Nothing was read."
		)
	compat.require_doctype(
		doctype,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)

	given = (
		as_str(args, "document_name")
		or as_str(args, "docname")
		or as_str(args, "name")
		or as_str(args, "form")
	)
	inner = dict(args)
	if given:
		inner["name"] = given
	return doctype, str(handler["resolve"](inner))


def _require(doctype: str, name: str, permission: str) -> None:
	"""Frappe's own check on the form, not a role list this app invented.

	The same gate `signatures._require_write` applies and for the same reason: who
	may read or write an I-9 on a given site is that site's permission rules'
	question — a role, a User Permission on a company, a share — and a second
	answer compiled in here would drift from the Desk's.
	"""
	try:
		permitted = frappe.has_permission(doctype, permission, doc=name)
	except Exception:  # pragma: no cover - a site whose permission cache is cold
		permitted = frappe.has_permission(doctype, permission)
	if not permitted:
		raise ToolError(
			f"this account may not {permission} {doctype} {name}. Nothing was "
			f"{'read' if permission == 'read' else 'changed'}."
		)


def _destroyed(doctype: str, name: str) -> None:
	"""Refuse a destroyed I-9, which is the one record that must not be redrawn.

	`destroy_i9` certifies that the record was disposed of at the end of its
	retention period, and producing a printable copy of it afterwards is the one
	thing that certificate says did not happen. `render_i9_pdf` refuses for the
	same reason; this refuses BEFORE the render so the sentence names the
	destruction rather than a rendering failure.
	"""
	if doctype != signatures.I9_FORM:
		return
	try:
		status = str(frappe.db.get_value(doctype, name, "status") or "")
	except Exception:  # pragma: no cover - a site whose column is not migrated
		return
	if status == "Destroyed":
		raise ToolError(
			f"I-9 {name} was destroyed at the end of its retention period. Reconstituting a "
			f"copy of it would contradict the destruction it certifies. Nothing was read."
		)


# ── step 1: the document, before the signature ──────────────────────────────
def get_document_preview(args: dict) -> ToolResult:
	"""Read-only. The form as it stands, as BYTES, so a signer can be shown it.

	THE PRESENTATION STEP, WHICH THE APP COULD NOT MAKE. See the module docstring
	and `API_CONTRACT.md` §17.5: the renderers answer with a private `file_url`
	and the handset cannot follow one, so the page travels as base64 here exactly
	as the signed page travels back from `submit_form_signature` and the `.pkpass`
	travels back from `get_employee_badge_pass`.

	NO SIGNATURE IS INVOLVED AND NONE CAN BE. This function does not take an
	image, does not write a signature column and does not touch the Signing
	Evidence register. What it can write is the rendered page itself, once, where
	the record has none — see the module docstring on why that is a caller's
	decision being honoured rather than routed around.

	`refresh` REDRAWS ON PURPOSE and defaults off. The honest default is to hand
	back what is on the record and say whether it has gone stale; a caller who
	needs certainty that the page matches the record — which the presentation step
	genuinely does, because the fingerprint taken at signing covers the RECORD —
	passes it and gets a fresh draw.

	`stale` IS THE KEY WORTH BRANCHING ON. True means the record has been modified
	since the page was drawn, so what is in `base64` is not what the record says
	now. A client showing a stale page to a signer is showing them something other
	than the thing whose content will be hashed.
	"""
	doctype, name = resolve_document(args)
	_require(doctype, name, "read")
	_destroyed(doctype, name)

	handler = signatures.FORM_HANDLERS[doctype]
	field = handler["pdf_field"]
	existing = _page_url(doctype, name, field)
	stale = _is_stale(doctype, name)
	refresh = as_bool(args, "refresh", False)

	rendered = False
	note = ""
	if not existing or refresh:
		drawn = _draw(doctype, name, handler)
		if drawn.get("file_url"):
			rendered = True
			existing = drawn["file_url"]
			stale = False
		else:
			note = drawn.get("note") or ""

	payload = _page_bytes(existing) if existing else {}
	data = {
		"document_type": doctype,
		"document_name": name,
		# The tool's own spellings alongside the contract's, so a caller written
		# against either reads the same record. The same tolerance
		# `get_attachment_content` carries.
		"doctype": doctype,
		"docname": name,
		"employee": frappe.db.get_value(doctype, name, "employee")
		if compat.has_field(doctype, "employee")
		else None,
		"status": frappe.db.get_value(doctype, name, "status")
		if compat.has_field(doctype, "status")
		else None,
		"available": bool(payload.get("content")),
		"rendered": rendered,
		"stale": bool(stale),
		"modified": str(frappe.db.get_value(doctype, name, "modified") or "") or None,
		"file_url": existing or None,
		"file_name": payload.get("file_name") or (existing.rsplit("/", 1)[-1] if existing else None),
		"content_type": "application/pdf",
		"encoding": "base64" if payload.get("content") else None,
		# THREE SPELLINGS OF ONE FACT, none of them a rename — `content` is what
		# `API_CONTRACT.md` decodes, `content_base64` is what this app's file tools
		# answer with, and `base64` is what `submit_form_signature` puts the signed
		# page under. A preview that spelled it a fourth way would be the one call
		# in the flow a client has to special-case.
		"content": payload.get("content"),
		"content_base64": payload.get("content"),
		"base64": payload.get("content"),
		"bytes": payload.get("bytes"),
		"signature_boxes": _boxes_for(doctype, name),
		"note": note or payload.get("note") or _preview_note(doctype, existing, stale),
	}
	summary = (
		f"{doctype} {name}: "
		+ (f"{data['bytes']:,} byte page" if data["available"] else "no page could be read")
		+ (" (redrawn)" if rendered else "")
		+ (" — STALE, the record has changed since it was drawn" if data["stale"] else "")
	)
	return ToolResult(data=data, summary=summary)


def _page_url(doctype: str, name: str, field: str) -> str:
	try:
		return str(frappe.db.get_value(doctype, name, field) or "").strip()
	except Exception:  # pragma: no cover - a site whose column is not migrated
		return ""


def _is_stale(doctype: str, name: str) -> bool:
	"""Has the record changed since its page was drawn? None-answer is False.

	Answerable only where the doctype records WHEN it drew — the I-9 and the W-4
	carry `generated_pdf_on` and the Tax Form does not. A doctype that cannot
	answer reports not-stale rather than stale, because a preview that cried stale
	on every Tax Form on the site is one nobody would read.
	"""
	if not compat.has_field(doctype, "generated_pdf_on"):
		return False
	try:
		row = frappe.db.get_value(doctype, name, ["generated_pdf_on", "modified"], as_dict=True) or {}
	except Exception:  # pragma: no cover
		return False
	drawn = str(row.get("generated_pdf_on") or "")
	modified = str(row.get("modified") or "")
	if not drawn or not modified:
		return False
	return modified > drawn


def _draw(doctype: str, name: str, handler: dict) -> dict:
	"""Render the form. NEVER RAISES — a preview with no page is not a failure.

	The renderers need `pypdf` and the blank federal form on disk, and a site
	missing either has a record it can still collect a signature against. Throwing
	here would make the presentation step refuse to open a pad that would have
	worked.
	"""
	try:
		result = handler["render"](name)
	except Exception as exc:
		return {
			"note": (
				f"no page could be drawn for this form ({exc}). The signature pad still works — "
				f"the attestation goes on the record either way — but the signer cannot be shown "
				f"the page, so this is a presentation step that did not happen."
			)
		}
	data = getattr(result, "data", None) or {}
	return {"file_url": data.get("file_url"), "file_name": data.get("file_name")}


def _page_bytes(url: str) -> dict:
	"""The File at `url`, read back as base64. NEVER RAISES — see `_signed_pdf`.

	A File row written in a transaction that has not committed, or a private files
	directory that moved, both end the same way: the caller is told there is no
	page rather than shown a failure for a record that is perfectly fine.
	"""
	try:
		docname = str(frappe.db.get_value("File", {"file_url": url}, "name") or "")
		content = files.read_file_bytes(docname) if docname else b""
	except Exception as exc:
		return {"note": f"the page at {url} could not be read back ({exc})."}
	if not content:
		return {"note": f"the page at {url} read back empty."}
	return {
		"content": base64.b64encode(content).decode("ascii"),
		"bytes": len(content),
		"file_name": url.rsplit("/", 1)[-1] or None,
	}


def _boxes_for(doctype: str, name: str) -> list:
	"""Which signature boxes this form has, and which already carry one.

	ON THE PREVIEW BECAUSE THE PREVIEW IS WHAT DECIDES WHAT HAPPENS NEXT. A pad
	opening over a Form I-9 needs to know that Section 1 is signed and Section 2
	is not before it asks anybody to draw anything, and the alternative is
	discovering it by submitting a signature and being told the box is taken.
	"""
	rows = []
	for box in signatures.SIGNATURE_BOXES:
		if box.doctype != doctype:
			continue
		signed = None
		if not box.child_table and compat.has_field(doctype, box.field):
			try:
				signed = bool(str(frappe.db.get_value(doctype, name, box.field) or "").strip())
			except Exception:  # pragma: no cover
				signed = None
		rows.append(
			{
				"field": box.field,
				"label": box.label,
				"signer_role": box.signer_role,
				"section": box.section_label,
				# The sentence being sworn to, in the government's own words, so
				# the presentation step shows what §17.5 says it shows. Never
				# paraphrased and never composed on the phone.
				"attestation": box.attestation,
				"child_table": box.child_table or None,
				"signed": signed,
			}
		)
	return rows


def _preview_note(doctype: str, url: str, stale: bool) -> str:
	if not url:
		return f"no page is on this {doctype} and none could be drawn. The signer cannot be shown the form."
	if stale:
		return (
			"this page was drawn before the record's last change, so it is not what the record "
			"says now. Pass refresh=true to redraw it before showing it to a signer — the "
			"fingerprint taken at signing covers the RECORD, not this page."
		)
	return ""


# ── step 5: the artefact, after the signature ───────────────────────────────
def seal_signed_document(args: dict) -> ToolResult:
	"""Append the verification page to a signed form, hash it, and file the hash.

	STEP 5 OF THE CHAIN `tools/signing_evidence.py` OPENS WITH, and the one that
	module deferred. It runs AFTER the signature, never instead of it, and it is
	the caller's own step: `submit_form_signature` takes it automatically and
	reports what happened, and this tool is how an operator seals a form signed
	before v0.63.0 or re-seals one that has since gained a second signature.

	WHAT IT PRODUCES, in order:

	  1. THE FORM, REDRAWN. `render_i9_pdf` and `render_w4_pdf` stamp every
	     captured signature into the page CONTENT at the form's own named widget
	     rectangles and then flatten the AcroForm away — so the base document
	     already has the ink where the government put the line, and this does not
	     place a single pixel of it. A second implementation of that geometry here
	     would put the same signature in a slightly different place.
	  2. THE VERIFICATION PAGE, from the Signing Evidence rows for this document —
	     every one of them, oldest first. A Form I-9 signed by the worker in July
	     and the employer in August gets both blocks; an appendix naming one of two
	     signatures would look complete and not be.
	  3. A SHA-256 OF THE FINISHED FILE, which cannot be printed on the file it is
	     taken over — see `pdf_seal`. It goes on the evidence rows instead.
	  4. THE SEALED PDF, attached to the document. It does NOT repoint
	     `generated_pdf`: that field is the working page somebody prints, this is
	     the retained artefact, and collapsing the two would mean the next
	     signature's redraw silently threw the seal away.

	IT REFUSES AN UNSIGNED FORM, and that refusal is the point rather than
	caution. A verification page appended to a form nobody has signed is a
	document that LOOKS sealed, carries an official-sounding appendix, and vouches
	for nothing — which is worse than no page at all, because somebody would file
	it.

	A SIGNED FORM WITH NO EVIDENCE ROW IS SEALED ANYWAY, with the appendix saying
	so in as many words. Every signature collected before v0.60.0 is in that state
	and cannot grow a row retrospectively — the badge scan, the device and the
	coordinates were never captured, and inventing them is the one thing an
	evidence register must never do.
	"""
	pdf_seal.require()
	doctype, name = resolve_document(args)
	_require(doctype, name, "write")
	_destroyed(doctype, name)

	rows = evidence_for(doctype, name)
	signed = _signed_boxes(doctype, name)
	if not (rows or signed):
		raise ToolError(
			f"{doctype} {name} carries no signature in any of its boxes, so there is nothing to "
			f"seal. A verification page on an unsigned form is an official-looking appendix that "
			f"vouches for nothing. collect_form_signature is what puts a signature on it. "
			f"Nothing was changed."
		)

	handler = signatures.FORM_HANDLERS[doctype]
	url = _base_page(doctype, name, handler)
	base = _page_bytes(url)
	if not base.get("bytes"):
		raise ToolError(
			f"{doctype} {name} was redrawn to {url or 'nowhere'} and the page could not be read "
			f"back ({base.get('note') or 'no content'}). Nothing was sealed and nothing was "
			f"changed on the evidence rows."
		)

	content = base64.b64decode(base["content"])
	note = (
		""
		if rows
		else (
			"This document carries a signature and no Signing Evidence row. It was signed before "
			"the evidence register existed (erpnext_mcp v0.60.0); the identity, device and location "
			"of that signing were never captured and are not recoverable."
		)
	)
	sealed = pdf_seal.seal(content, rows, note=note)
	digest = pdf_seal.sha256_of(sealed)

	attachment = artifacts.attach_bytes(
		doctype, name, _seal_file_name(doctype, name, url), sealed, parent_authorized=True
	)
	recorded = _record_seal(rows, attachment.get("file_url"), digest)
	filed = _file_on_employee(doctype, name, attachment)

	data = {
		"document_type": doctype,
		"document_name": name,
		"doctype": doctype,
		"docname": name,
		"sealed": True,
		"file_url": attachment.get("file_url"),
		"file_name": attachment.get("file_name"),
		"bytes": len(sealed),
		"sealed_pdf_hash": digest,
		"base_pdf": url,
		"base_bytes": base["bytes"],
		"signatures_on_page": len(rows),
		"signed_boxes": signed,
		"evidence": [row.get("name") for row in rows],
		"evidence_updated": recorded["updated"],
		# v0.64.1. ALWAYS PRESENT, and `filed` is false where the form names
		# nobody. A caller that had to test for the key would have two code paths
		# where it needs one — see `shift_evidence` on `complete_farm_task`.
		"employee_copy": filed,
		"note": recorded["note"] or note or "",
	}
	summary = (
		f"{doctype} {name} sealed as {attachment.get('file_name')} ({len(sealed):,} bytes), "
		f"{len(rows)} signature(s) on the verification page"
		+ (f", {len(recorded['updated'])} evidence row(s) stamped" if recorded["updated"] else "")
		+ (f", filed on {filed['employee']}" if filed.get("filed") else "")
	)
	return ToolResult(data=data, summary=summary)


def _file_on_employee(doctype: str, name: str, attachment) -> dict:
	"""Cross-file the sealed copy onto the Employee the form is about. NEVER RAISES.

	────────────────────────────────────────────────────────────────────────
	WHY THE SEALED PDF BELONGS IN TWO PLACES AND THE WORKING PAGE DOES NOT
	────────────────────────────────────────────────────────────────────────

	v0.64.1, and it is a gap somebody found by looking for a completed I-9 where
	an inspection would look for it. `seal_signed_document` attached the retained
	artefact to the FORM, which is correct and was the whole of it: the personnel
	folder — the Employee record, which is what anybody opens when they are
	asked "show me this worker's paperwork" — showed nothing. The I-9 was
	complete, sealed, hashed and findable only by somebody who already knew the
	I-9 Form docname, which an auditor standing at a desk does not.

	IT IS A SECOND `File` ROW AT THE SAME `file_url`, NOT A SECOND COPY OF THE
	BYTES. `insert_attachment` writes a row pointing at a URL when it is given no
	content, which is the same door `attach_file_to_document` uses for a file that
	already lives on the site. Two links to one artefact is what a cross-reference
	is; two copies would be two documents that can drift apart, and the one thing
	a tamper-evident artefact must not do is exist twice with one hash.

	ONLY THE SEAL TRAVELS, NEVER `generated_pdf`. The working page is redrawn
	every time a signature lands, and a personnel folder accumulating a copy of
	each intermediate draw would bury the one document that matters under the
	four that were superseded. The seal is the retained artefact by construction
	— that is the argument `seal_signed_document` already makes for not
	repointing `generated_pdf` — so it is the one that gets filed.

	A FORM THAT NAMES NOBODY IS NOT AN ERROR. A Tax Form is an employer return
	signed by an officer; it has no `employee` column and belongs in no personnel
	folder. `filed: false` with the reason is the honest answer, and inventing a
	link would put a 941 in somebody's file.

	NEVER RAISES, for the reason `_record_seal` does not: the sealed artefact is
	already written and hashed by the time this runs, and a cross-reference that
	could undo it would trade the irreplaceable thing for the convenient one.
	"""
	url = str(attachment.get("file_url") or "").strip()
	if not url:
		return {"filed": False, "reason": "the sealed copy has no file_url to point a second link at."}
	if not compat.has_field(doctype, EMPLOYEE_LINK):
		return {
			"filed": False,
			"reason": (
				f"{doctype} carries no {EMPLOYEE_LINK} column — it is not a form about one person, "
				f"so there is no personnel folder for the sealed copy to appear in."
			),
		}
	try:
		employee = str(frappe.db.get_value(doctype, name, EMPLOYEE_LINK) or "").strip()
	except Exception as exc:  # pragma: no cover - a site mid-migrate
		return {"filed": False, "reason": f"the {EMPLOYEE_LINK} on {name} could not be read ({exc})."}
	if not employee:
		return {
			"filed": False,
			"reason": f"{doctype} {name} names no employee, so there is no folder to file it in.",
		}
	if not compat.doctype_exists(EMPLOYEE):
		return {"filed": False, "reason": "this site has no Employee DocType."}

	# ALREADY THERE IS A SUCCESS, NOT A SKIP. `seal_signed_document` is documented
	# as re-runnable — an operator re-seals a form that has gained a second
	# signature — and a re-seal that filed a duplicate link every time would turn
	# a personnel folder into a changelog of one document.
	try:
		existing = frappe.db.get_all(
			"File",
			filters={
				"file_url": url,
				"attached_to_doctype": EMPLOYEE,
				"attached_to_name": employee,
			},
			pluck="name",
			limit=1,
		)
	except Exception:  # pragma: no cover - a site mid-migrate
		existing = []
	if existing:
		return {
			"filed": True,
			"employee": employee,
			"file": existing[0],
			"file_url": url,
			"already_linked": True,
		}

	try:
		linked = files.insert_attachment(
			str(attachment.get("file_name") or "").strip() or url.rsplit("/", 1)[-1],
			b"",
			is_private=True,
			doctype=EMPLOYEE,
			name=employee,
			file_url=url,
			parent_authorized=True,
		)
	except Exception as exc:
		return {
			"filed": False,
			"employee": employee,
			"reason": (
				f"the sealed copy is attached to {doctype} {name} and could not be cross-filed on "
				f"{EMPLOYEE} {employee} ({exc}). attach_file_to_document with this file_url files it "
				f"without producing anything again."
			),
		}
	return {
		"filed": True,
		"employee": employee,
		"file": linked.name,
		"file_url": url,
		"already_linked": False,
	}


def _base_page(doctype: str, name: str, handler: dict) -> str:
	"""The page to seal: the one on the record where it is current, else a fresh draw.

	THE REUSE IS NOT A MICRO-OPTIMISATION, it is what keeps the signature path
	from rendering the same four-page USCIS form twice in one call.
	`signatures._redraw` has already redrawn the form with the new capture stamped
	in by the time `submit_form_signature` reaches the seal, and re-rendering
	produces a byte-for-byte equivalent page at real cost on a handset waiting in
	an orchard.

	IT IS ONLY SAFE WHERE THE DOCTYPE CAN ANSWER "IS THIS CURRENT". `_is_stale`
	compares `generated_pdf_on` against `modified`, and a doctype carrying no such
	column — a Tax Form — cannot say, so it is redrawn rather than trusted. Sealing
	a stale page would produce a verification record vouching for a document other
	than the one on the record, which is the exact failure the seal exists to make
	detectable.
	"""
	existing = _page_url(doctype, name, handler["pdf_field"])
	if existing and compat.has_field(doctype, "generated_pdf_on") and not _is_stale(doctype, name):
		return existing
	drawn = handler["render"](name)
	return str((getattr(drawn, "data", None) or {}).get("file_url") or "").strip()


def evidence_for(doctype: str, name: str) -> list:
	"""Every Signing Evidence row for one document, oldest first, with its label.

	OLDEST FIRST because the verification page reads as a chronology: a Form I-9's
	Section 1 is the worker's attestation and Section 2 is the employer's response
	to it, and printing them the other way round would describe the employer
	verifying documents before anybody said who they were.

	`label` IS GRAFTED ON FROM `SIGNATURE_BOXES` rather than stored: the register
	holds the fieldname, which is the durable fact, and what a box is CALLED is a
	wording choice that has already changed once — Supplement B is what the form
	used to call Section 3, and rows written under the old name must print the
	current one.

	NEVER RAISES. A site whose Signing Evidence doctype is not migrated has no
	rows, and a seal with no blocks on its page is a smaller problem than a seal
	that refused to run.
	"""
	if not signing_evidence.available():
		return []
	fields = compat.existing_fields(signing_evidence.SIGNING_EVIDENCE, list(signing_evidence.DETAIL_FIELDS))
	try:
		rows = frappe.db.get_all(
			signing_evidence.SIGNING_EVIDENCE,
			filters={"document_type": doctype, "document_name": name},
			fields=fields,
			order_by="signed_at asc, creation asc",
			limit_page_length=0,
		)
	except Exception:  # pragma: no cover - a site mid-migrate
		return []
	out = []
	for row in rows or []:
		entry = dict(row)
		box = signatures.BOXES_BY_KEY.get(f"{doctype}.{entry.get('signature_field') or ''}")
		if box is not None:
			entry["label"] = box.label
		out.append(entry)
	return out


def _signed_boxes(doctype: str, name: str) -> list:
	"""The fieldnames on this form that actually carry an image right now.

	READ OFF THE FORM RATHER THAN OFF THE REGISTER, because they answer different
	questions and this one is "is there anything here to seal". A signature
	collected before v0.60.0 has no evidence row and is still a signature.
	"""
	found = []
	for box in signatures.SIGNATURE_BOXES:
		if box.doctype != doctype or box.child_table:
			continue
		if not compat.has_field(doctype, box.field):
			continue
		try:
			if str(frappe.db.get_value(doctype, name, box.field) or "").strip():
				found.append(box.field)
		except Exception:  # pragma: no cover
			continue
	return found


def _seal_file_name(doctype: str, name: str, base_url: str) -> str:
	"""`I-9-I9-2026-0001-Garcia-Maria-sealed.pdf`, off the base page's own name.

	Derived from what the renderer just called the page rather than composed
	again, so the sealed copy sits next to the working one in a directory listing
	and the two are obviously the same form.
	"""
	stem = (base_url or "").rsplit("/", 1)[-1]
	if stem.lower().endswith(".pdf"):
		stem = stem[: -len(".pdf")]
	if not stem:
		stem = f"{doctype}-{name}".replace(" ", "-")
	safe = "".join(character if character.isalnum() or character in "-_" else "-" for character in stem)
	while "--" in safe:
		safe = safe.replace("--", "-")
	return f"{safe.strip('-')}-{SEAL_STEM}.pdf"


def _record_seal(rows: list, file_url, digest: str) -> dict:
	"""Stamp the seal onto every evidence row the page describes. NEVER RAISES.

	────────────────────────────────────────────────────────────────────────
	WHY THREE COLUMNS MOVE ON A ROW THAT IS OTHERWISE IMMUTABLE
	────────────────────────────────────────────────────────────────────────

	`SigningEvidence.before_save` refuses every write after the insert, and the
	controller argues why: an evidence row that could be edited is not evidence.
	Nothing here goes through it — the three seal columns are written with
	`frappe.db.set_value(..., update_modified=False)`, the same door
	`render_i9_pdf` uses for `generated_pdf_on`, and the reason is that they are
	not part of what the row ASSERTS.

	Every other column says something about the signature: who made it, how they
	were identified, when, where, and what the document said at the time. Those
	are fixed at the moment of signing and may never move. These three say WHICH
	SEALED FILE THIS ATTESTATION CURRENTLY APPEARS IN — a fact about an artefact
	produced afterwards, and one that legitimately changes: a Form I-9 signed in
	Section 1 in July is sealed once; when the employer signs Section 2 in August
	the form is sealed again, and the July attestation now appears in a two-block
	document. Freezing the July row at the one-block file would point an auditor
	at a copy that is missing half the signatures on the form it describes.

	NOTHING IS DELETED. Each seal is a new File attached to the document, and the
	previous one stays attached — so the chain of sealed copies is itself a record,
	and the row's `sealed_pdf` names the current member of it rather than the only
	one that ever existed.

	EVERY ROW FOR THE DOCUMENT IS STAMPED, not just the newest. All of them are
	described by the page that was just drawn; a row left unstamped would say this
	attestation appears in no sealed copy, which would be false.
	"""
	updated: list = []
	failed: list = []
	if not signing_evidence.available():
		return {"updated": [], "note": ""}
	columns = compat.existing_fields(signing_evidence.SIGNING_EVIDENCE, list(SEAL_FIELDS))
	if len(columns) < len(SEAL_FIELDS):
		return {
			"updated": [],
			"note": (
				f"the sealed copy is attached to the document and the Signing Evidence rows do "
				f"not carry the seal columns on this site — run `bench --site <site> migrate` "
				f"after upgrading erpnext_mcp. The file hash is {digest} and is in this answer "
				f"only."
			),
		}
	payload = {
		"sealed_pdf": file_url,
		"sealed_pdf_hash": digest,
		"sealed_at": frappe.utils.now(),
	}
	for row in rows:
		name = str(row.get("name") or "")
		if not name:
			continue
		try:
			frappe.db.set_value(signing_evidence.SIGNING_EVIDENCE, name, payload, update_modified=False)
			updated.append(name)
		except Exception:  # pragma: no cover - a site mid-migrate
			failed.append(name)
	note = ""
	if failed:
		note = (
			f"the sealed copy is attached to the document and {len(failed)} evidence row(s) "
			f"could not be stamped with its hash ({', '.join(failed)}). The seal stands; the "
			f"register does not point at it."
		)
	return {"updated": updated, "note": note}


__all__ = [
	"SEAL_FIELDS",
	"evidence_for",
	"get_document_preview",
	"resolve_document",
	"seal_signed_document",
]

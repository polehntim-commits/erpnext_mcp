# SPDX-License-Identifier: MIT
"""The evidence packet a signature leaves behind. v0.60.0.

────────────────────────────────────────────────────────────────────────────
A SIGNATURE IMAGE IS NOT EVIDENCE. AN EVIDENCE PACKET IS.
────────────────────────────────────────────────────────────────────────────

`tools/signatures.py` has stored the drawn image, the moment and the address on
the form itself since v0.55.0, and 8 CFR § 274a.2(h) asks for exactly those
three. What none of them answers is the question an auditor asks second:

    "Fine — but how do you know it was HIM?"

A PNG in a column proves that somebody drew a shape on a piece of glass. The
five things that turn that into a record which survives a challenge are:

  1. THE DOCUMENT WAS PRESENTED. The signer saw the form before signing it.
  2. IDENTITY WAS VERIFIED AT THE PAD — a badge scanned, an employee ID tapped,
     a photograph taken. Not asserted afterwards by whoever filed the form.
  3. THE SIGNATURE WAS CAPTURED.
  4. CONTEXT WAS CAPTURED — when, on what device, at what coordinates, from
     what address.
  5. THE ARTEFACT IS TAMPER-EVIDENT — the content that was shown is hashed, so
     a form altered after signing stops matching what the signer attested to.

This module is where 1, 2 and 4 are written down and where 5's hash is taken;
step 3 stays where it was. The rendered page that composites the whole packet
is a later release, and it will be drawn FROM these rows rather than from a
second copy of the same facts.

────────────────────────────────────────────────────────────────────────────
WHY A DOCTYPE AND NOT SIX MORE COLUMNS ON THE I-9
────────────────────────────────────────────────────────────────────────────

Because a signature event is not a property of a form. One Form I-9 carries
three of them, signed by two different people in two different capacities on
two different days; a Form W-4 carries one; a training acknowledgment carries
one per attendee. Columns on each form would mean `section_2_signer_badge`,
`section_2_device_id`, `section_2_gps_latitude` — and then the same six again
for Supplement B, and the whole set again on every form that grows a signature
line. That is the table sprawl this app's design philosophy names outright, and
it makes "show me every signature this foreman collected in July" a query that
has to know every form in advance.

ONE ROW PER SIGNATURE EVENT IS THE SHAPE THE QUESTION HAS. Filterable by
signer, by document, by date, by company, across every form at once.

────────────────────────────────────────────────────────────────────────────
CREATING ONE IS BEST-EFFORT, AND THAT IS ARGUED FOR RATHER THAN CONVENIENT
────────────────────────────────────────────────────────────────────────────

`signatures.py` opens with the ordering that is the whole of its design — store
the image, write it onto the form, close the task, redraw the PDF, and each step
may fail without undoing the one before it, because THE SIGNATURE IS THE
IRREPLACEABLE ARTEFACT and the person who drew it has gone back to work.

Evidence creation joins that list at the end and inherits the same rule. A
phone that captured a signature, wrote it to the federal record, and then could
not write an evidence row has still done the part § 274a asks for; refusing the
signature to keep this register tidy would throw away the only thing that cannot
be recovered. So `record` never raises, and what it did or could not do is
REPORTED in the tool result rather than swallowed — an operator who finds
`evidence.recorded: false` in an answer has been told, which is the difference
between best-effort and silent.

WHAT IS *NOT* BEST-EFFORT IS THE CHECKING. A badge that resolves to the wrong
person and a caller claiming a capacity the box contradicts are both refused
before anything is written, by `signatures.py`, on the way in. Verification that
fails open is not verification.
"""

from __future__ import annotations

import hashlib
import json

import frappe

from ..args import as_date, as_limit, as_str, resolve_company
from ..compat import doctype_exists, existing_fields
from ..errors import ToolError
from ..result import ToolResult

SIGNING_EVIDENCE = "Signing Evidence"
EMPLOYEE = "Employee"

#: How identity may be established at the pad, in the doctype's own spelling.
#: A CLOSED LIST for the reason `SIGNATURE_BOXES` is closed: "we verified them
#: somehow" is not a method, and a free-text column here would fill up with one.
VERIFICATION_METHODS = ("Badge QR", "Employee ID", "Photo")

#: Spellings a client will reasonably send for each. The iOS build says
#: `badge_qr`; a model composing the call from a sentence says `badge`.
VERIFICATION_ALIASES = {
	"badge qr": "Badge QR",
	"badge_qr": "Badge QR",
	"badgeqr": "Badge QR",
	"badge": "Badge QR",
	"qr": "Badge QR",
	"employee id": "Employee ID",
	"employee_id": "Employee ID",
	"employeeid": "Employee ID",
	"id": "Employee ID",
	"photo": "Photo",
	"photograph": "Photo",
	"selfie": "Photo",
}

#: The capacities a signature may be made in. `signatures.SIGNATURE_BOXES` names
#: the first four in its own lower-case vocabulary and `ROLES_BY_BOX` maps
#: between them; `Witness` and `Inspector` are here for the acknowledgment and
#: inspection forms this register is built to cover next.
SIGNATURE_ROLES = (
	"Employee",
	"Employer Representative",
	"Preparer or Translator",
	"Witness",
	"Officer",
	"Inspector",
)

#: `SignatureBox.signer_role` → the capacity written on the evidence row. THE BOX
#: DECIDES, NOT THE CALLER — see `signatures._evidence_role`. Section 1 and
#: Section 2 of one form are an employee's attestation and an employer's, and a
#: client that could choose between them could file an employer's attestation as
#: a worker's.
ROLES_BY_BOX = {
	"employee": "Employee",
	"employer": "Employer Representative",
	"preparer": "Preparer or Translator",
	"witness": "Witness",
	"officer": "Officer",
	"inspector": "Inspector",
}

#: The capacities that are the EMPLOYER speaking rather than the worker. These
#: are the ones `signatures._require_signer` gates on the authorized-signer
#: roster, and the reason the list is named here rather than spelled out at the
#: call site is that "which roles are the employer" is a fact about the
#: vocabulary, and the vocabulary lives in this module.
EMPLOYER_ROLES = ("Employer Representative",)

#: Framework bookkeeping, which says nothing about what a signer was shown and
#: moves for reasons they never saw. `modified` changes on every save, `name` and
#: `idx` change when a child row is renumbered, and the underscore-prefixed
#: columns are tags, comments and assignment state.
#:
#: THE SIGNATURE COLUMNS ARE EXCLUDED TOO and they are not listed here, because
#: which columns those are is a fact about `signatures.SIGNATURE_BOXES` and lives
#: there — see `hash_exclusions`. The reason is the same: the hash is taken of the
#: document AS PRESENTED, before the signature exists, so a check that included it
#: could never match.
_VOLATILE = (
	"modified",
	"modified_by",
	"creation",
	"owner",
	"idx",
	"docstatus",
	"parent",
	"parentfield",
	"parenttype",
	"_user_tags",
	"_comments",
	"_assign",
	"_liked_by",
	"__islocal",
	"__unsaved",
	"__onload",
)

#: The fields a list row carries. `document_hash` is not among them and
#: `get_signing_evidence` has it: a list of forty rows of hex is noise, and the
#: hash is checked one document at a time.
LIST_FIELDS = (
	"name",
	"signer",
	"signer_name",
	"signer_user",
	"signer_badge",
	"verification_method",
	"signed_at",
	"status",
	"company",
	"document_type",
	"document_name",
	"signature_role",
	"signature_field",
	"supersedes",
)

DETAIL_FIELDS = (
	*LIST_FIELDS,
	"signature_image",
	"document_hash",
	"hashed_fields",
	"device_id",
	"ip_address",
	"gps_latitude",
	"gps_longitude",
	# v0.63.0. The tamper-evident copy this attestation appears in, and the hash
	# of its finished bytes. Read through `existing_fields` like everything else
	# here, so a site that has not migrated past v0.60.0 reports the row without
	# them rather than failing the query.
	"sealed_pdf",
	"sealed_pdf_hash",
	"sealed_at",
)


def available() -> bool:
	return doctype_exists(SIGNING_EVIDENCE)


def _require() -> None:
	if not available():
		raise ToolError(
			f"{SIGNING_EVIDENCE} does not exist on this site — run `bench --site <site> migrate` "
			f"after upgrading erpnext_mcp."
		)


# ── the vocabulary ──────────────────────────────────────────────────────────
def normalise_verification_method(value) -> str:
	"""`badge_qr` → `Badge QR`, "" for none, or a refusal naming the three.

	EMPTY IS ALLOWED AND MEANS NOTHING WAS DONE. A signature collected without an
	identity step is a real thing — an operator signing a 941 at a desk has no
	badge to scan — and the record says so in `status` rather than inventing a
	method. What is refused is a method this app does not recognise, because a
	column that will hold any string is a column that proves nothing.
	"""
	raw = str(value or "").strip()
	if not raw:
		return ""
	resolved = VERIFICATION_ALIASES.get(raw.casefold())
	if resolved:
		return resolved
	for method in VERIFICATION_METHODS:
		if method.casefold() == raw.casefold():
			return method
	raise ToolError(
		f"verification_method must be one of {', '.join(VERIFICATION_METHODS)}, got {value!r}. "
		f"Omit it where no identity check was made — the evidence row records that honestly "
		f"as Unverified rather than claiming a check that did not happen. Nothing was changed."
	)


def normalise_signature_role(value) -> str:
	"""One of `SIGNATURE_ROLES`, or a refusal naming them all."""
	raw = str(value or "").strip()
	if not raw:
		return ""
	mapped = ROLES_BY_BOX.get(raw.casefold())
	if mapped:
		return mapped
	for role in SIGNATURE_ROLES:
		if role.casefold() == raw.casefold():
			return role
	raise ToolError(
		f"signature_role must be one of {', '.join(SIGNATURE_ROLES)}, got {value!r}. "
		f"It is the capacity somebody signs in, and the capacities are a closed list because "
		f"'Employee' and 'Employer Representative' are different legal acts. Nothing was changed."
	)


# ── the hash ────────────────────────────────────────────────────────────────
def document_fingerprint(doc, exclude=(), only=None) -> dict:
	"""`{"hash": "sha256:<hex>", "fields": (...)}` for the record as it stands now.

	TAKEN BEFORE THE SIGNATURE IS WRITTEN, which is the only moment that answers
	the question it exists to answer. "What did they see" is a fact about the
	document at presentation; a hash taken afterwards would include the signature
	and would prove only that the app had just written one.

	DETERMINISTIC BY CONSTRUCTION: keys sorted, values stringified, framework
	bookkeeping dropped. `default=str` rather than a custom encoder because a
	Frappe document carries dates, decimals and child rows, and the ONE property
	this needs is that the same content produces the same string twice — not that
	the string is a faithful serialisation of anything.

	THE EXCLUSIONS ARE APPLIED AT EVERY LEVEL, child rows included. A Form I-9's
	Supplement B attestation is a column on a `reverifications` ROW, so a pruning
	that only reached the top of the document would leave the signature inside the
	hash on exactly the box where the hash matters most.

	────────────────────────────────────────────────────────────────────────
	EMPTY COLUMNS ARE NOT IN THE HASH, AND THAT IS THE RULE THAT MAKES IT USEFUL
	────────────────────────────────────────────────────────────────────────

	The naive fingerprint — every column, whatever it holds — is correct and
	useless. A Form I-9 is signed by the worker in July and by the employer in
	August, and the employer's half FILLS IN COLUMNS THAT WERE EMPTY IN JULY:
	`verification_date`, `verifier_name`, the document titles and numbers off
	Section 2. Under a whole-document hash, completing a form correctly would
	report the worker's own attestation as a tampered record — and an integrity
	check that fires on every properly-handled form is one nobody reads, which is
	worse than not having one.

	So the fingerprint covers THE COLUMNS THAT HELD SOMETHING WHEN THE DOCUMENT
	WAS PRESENTED, which is also the more honest reading of what it claims:

	  * information ADDED after the signature does not change it. The signer was
	    shown a blank box and attested to nothing about it;
	  * information CHANGED after the signature does. The value in the hash is
	    the one they were shown, and it is not the one there now;
	  * information ERASED after the signature does. The key drops out of the
	    recomputed set, so the hashes differ.

	`0` and `False` are kept — an unticked checkbox is something the signer saw,
	and a box that becomes ticked afterwards is a change to the page they read.
	Only `None` and the empty string are treated as absent.

	WHICH IS WHY `fields` COMES BACK BESIDE THE HASH AND IS STORED WITH IT. A
	fingerprint over "the columns that held something" cannot be re-derived from
	the document later, because by then some of the empty ones hold something —
	recomputing over the new set would produce a different hash from a form
	nobody altered, which is the false alarm this rule exists to remove. So the
	evidence row carries the field list, `verify_fingerprint` passes it back as
	`only`, and the recomputation is over exactly the columns the signature
	covered. It also answers a question an auditor would otherwise have to take on
	trust: WHICH PARTS OF THIS FORM does this signature vouch for.

	NEVER RAISES. A document this cannot read produces an empty hash and an
	evidence row that says `status: Unverified`, which is a true statement about a
	weaker packet. Throwing here would refuse a signature over a hash.
	"""
	empty = {"hash": "", "fields": ()}
	try:
		content = doc.as_dict() if hasattr(doc, "as_dict") else dict(doc)
	except Exception:  # pragma: no cover - a document that will not flatten
		return empty
	dropped = set(_VOLATILE) | {str(name) for name in exclude if name}
	payload = _prune(content, dropped)
	if only is not None:
		payload = {key: value for key, value in payload.items() if key in only}
	try:
		blob = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=True)
	except Exception:  # pragma: no cover - a value even `str` will not render
		return empty
	return {
		"hash": "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest(),
		"fields": tuple(sorted(payload)),
	}


def _prune(value, dropped: set):
	"""One document's content with `dropped` and every empty column removed.

	`name` survives at the top level and is removed from child rows, which is not
	an inconsistency: the docname of the form is part of what was presented, and
	a child row's generated name is a value that changes when Frappe renumbers the
	table and never when anything a signer saw does.
	"""
	if isinstance(value, dict):
		return {
			str(key): _prune(inner, dropped | {"name"})
			for key, inner in value.items()
			if str(key) not in dropped and not str(key).startswith("_") and inner not in (None, "")
		}
	if isinstance(value, (list, tuple)):
		return [_prune(item, dropped) for item in value]
	return value


def verify_fingerprint(row: dict) -> dict:
	"""Does the signed document still hash to what the evidence row recorded?

	THE POINT OF THE WHOLE COLUMN, and it is answered here rather than left to
	whoever holds the row, because recomputing it means knowing which fields were
	excluded — and getting that wrong produces a confident "altered" on a document
	nobody touched.

	Three outcomes, and `matches` is null for two of them: an evidence row with
	no hash was never tamper-evident, and a document that has since been deleted
	or cannot be read cannot be compared. Neither is a mismatch and neither may be
	reported as one.

	`hashed_fields` IS WHAT MAKES THE RECOMPUTATION EXACT — see
	`document_fingerprint`. A row that carries none is compared over everything
	the pruning leaves, which is the honest fallback: it is what the hash on such a
	row was taken over.
	"""
	stored = str(row.get("document_hash") or "").strip()
	doctype = str(row.get("document_type") or "")
	docname = str(row.get("document_name") or "")
	if not stored:
		return {
			"matches": None,
			"note": (
				"this signature was recorded without a document hash, so there is nothing to "
				"compare. It is evidence that a signature was made and not evidence of what was "
				"on the page when it was."
			),
		}
	if not (doctype and docname) or not doctype_exists(doctype):
		return {"matches": None, "note": f"{doctype or 'the document type'} is not on this site."}
	try:
		doc = frappe.get_doc(doctype, docname)
	except Exception:
		return {
			"matches": None,
			"note": f"{doctype} {docname} could not be read back, so the hash was not checked.",
		}
	covered = _hashed_fields(row)
	recomputed = document_fingerprint(doc, exclude=_signature_columns(doctype), only=covered)
	current = recomputed["hash"]
	if not current:
		return {"matches": None, "note": "the document could not be hashed."}
	if current == stored:
		return {
			"matches": True,
			"current_hash": current,
			"covers": list(covered) if covered is not None else None,
			"note": (
				"the document hashes to what was recorded at signing — nothing this signature "
				"vouched for has been altered since."
			),
		}
	return {
		"matches": False,
		"current_hash": current,
		"covers": list(covered) if covered is not None else None,
		"note": (
			f"{doctype} {docname} NO LONGER HASHES to what was recorded when it was signed. The "
			f"hash covers the columns that HELD SOMETHING when the signer was shown the record, "
			f"and not its signature columns, its rendered PDF or its workflow status — so this is "
			f"not a later section being completed and it is not this app's own bookkeeping. "
			f"Something the signer was shown has been changed or erased since they attested to it."
		),
	}


def _hashed_fields(row: dict):
	"""The columns the stored hash was taken over, or None where none were saved."""
	raw = str(row.get("hashed_fields") or "").strip()
	if not raw:
		return None
	return {chunk.strip() for chunk in raw.split(",") if chunk.strip()}


def _signature_columns(doctype: str) -> tuple:
	"""The columns the hash was taken without, for one doctype.

	The stored hash was computed BEFORE the signature was written, so a check has
	to exclude the same columns or every row on the site would read as altered.
	Which columns those are is a fact about `signatures.SIGNATURE_BOXES`, so
	`signatures` is asked rather than a second copy of the table being kept here.
	"""
	from . import signatures

	return signatures.hash_exclusions(doctype)


# ── writing one ─────────────────────────────────────────────────────────────
def record(
	*,
	document_type: str,
	document_name: str,
	signature_role: str,
	signed_at: str,
	company: str = "",
	signer: str = "",
	signer_name: str = "",
	signer_user: str = "",
	signer_badge: str = "",
	verification_method: str = "",
	signature_field: str = "",
	signature_image: str = "",
	document_hash: str = "",
	hashed_fields: str = "",
	device_id: str = "",
	ip_address: str = "",
	gps_latitude=None,
	gps_longitude=None,
	supersedes: str = "",
) -> dict:
	"""Write one evidence row. NEVER RAISES — see the module docstring.

	Returns `{"recorded": bool, "evidence": name|None, "status": ..., "note": ...}`,
	which is what `collect_form_signature` puts in its answer. A caller reading
	`recorded: false` has been told the packet is incomplete; a caller that got
	nothing back would not have been.

	`status` IS DERIVED HERE AND NOT PASSED IN. It is the one-word summary of two
	columns — was identity verified, and was the document hashed — and computing
	it at the call site would be a third place for the definition to live.
	"""
	if not available():
		return {
			"recorded": False,
			"evidence": None,
			"note": (
				f"{SIGNING_EVIDENCE} is not on this site, so the signature was filed and no "
				f"evidence row was written. Run `bench --site <site> migrate`."
			),
		}

	verified = bool(str(verification_method or "").strip())
	hashed = bool(str(document_hash or "").strip())
	status = "Recorded" if (verified and hashed) else "Unverified"
	payload = {
		"doctype": SIGNING_EVIDENCE,
		# BOTH LINKS ARE DROPPED RATHER THAN GUESSED AT WHEN THEIR TARGET IS
		# ABSENT. An employer representative may have no Employee record and an
		# operator working through a bearer token may be no User at all; a Link
		# pointed at a row that is not there fails the whole insert, which would
		# trade a complete evidence packet for none at all over a corroborating
		# column.
		"signer": _link(EMPLOYEE, signer),
		"signer_name": signer_name or "",
		"signer_user": _link("User", signer_user),
		"signer_badge": signer_badge or "",
		"verification_method": verification_method or "",
		"signed_at": signed_at,
		"status": status,
		"company": company,
		"document_type": document_type,
		"document_name": document_name,
		"signature_role": signature_role,
		"signature_field": signature_field or "",
		"supersedes": supersedes or None,
		"signature_image": signature_image or "",
		"document_hash": document_hash or "",
		"hashed_fields": hashed_fields or "",
		"device_id": device_id or "",
		"ip_address": ip_address or "",
		# `None` HERE COMES BACK AS `0.0`, AND THAT CANNOT BE FIXED ON THIS SIDE.
		# Both columns are `Float`, which MariaDB stores `NOT NULL DEFAULT 0`, so
		# a signature that reported no fix is indistinguishable in the row from
		# one taken at 0°N 0°E. Passing `None` is still the honest argument — it
		# is what the caller knows — and the reader is where the distinction has
		# to be made: `pdf_seal._coordinates` treats the exact pair (0, 0) as
		# unset and prints nothing, rather than placing the signer in the Gulf of
		# Guinea. Do not "fix" this by writing a sentinel; a Float column has no
		# room for one that is not also a coordinate.
		"gps_latitude": gps_latitude,
		"gps_longitude": gps_longitude,
	}
	try:
		doc = frappe.get_doc(payload)
		doc.flags.ignore_permissions = True
		doc.insert()
	except Exception as exc:
		return {
			"recorded": False,
			"evidence": None,
			"status": status,
			"note": (
				f"the signature was filed and the evidence row was not written ({type(exc).__name__}: "
				f"{exc}). Nothing about the signature depends on it — the image, the moment and the "
				f"address are on the form itself — but the identity and location record for this "
				f"attestation is missing and cannot be reconstructed later."
			),
		}
	out = {"recorded": True, "evidence": doc.name, "status": status, "note": ""}
	if status == "Unverified":
		out["note"] = (
			"recorded as Unverified: "
			+ (
				""
				if verified
				else "no identity check was made at the pad (no badge scan, employee ID or photo)"
			)
			+ ("" if verified or hashed else "; ")
			+ ("" if hashed else "the document was not hashed, so the row does not say what was on the page")
			+ ". The signature stands; the packet around it is the weaker kind."
		)
	return out


def _link(doctype: str, value: str):
	"""`value` if it is a row in `doctype`, else None. See `record`'s payload."""
	name = str(value or "").strip()
	if not name:
		return None
	try:
		return name if frappe.db.exists(doctype, name) else None
	except Exception:  # pragma: no cover - a table this site cannot read
		return None


def supersede_target(document_type: str, document_name: str, signature_field: str) -> str:
	"""The newest evidence row for this exact box, or "".

	Matched on the FIELD as well as the record, for the reason
	`signatures._task_for` matches on the alert type: a Form I-9 carries three
	signatures, and naming Section 1's evidence as the row Section 2 replaced
	would be a false claim in the one register that exists to avoid making them.
	"""
	if not available():
		return ""
	filters = {"document_type": document_type, "document_name": document_name}
	if signature_field:
		filters["signature_field"] = signature_field
	try:
		rows = frappe.db.get_all(
			SIGNING_EVIDENCE,
			filters=filters,
			fields=["name"],
			order_by="signed_at desc, creation desc",
			limit_page_length=1,
		)
	except Exception:  # pragma: no cover - a site mid-migrate
		return ""
	return str(rows[0]["name"]) if rows else ""


# ── reading them back ───────────────────────────────────────────────────────
def list_signing_evidence(args: dict) -> ToolResult:
	"""Read-only. Signature events, filtered by document, person, company or date.

	THE QUESTION THIS ANSWERS IS THE ONE AN AUDITOR ASKS ON ARRIVAL: show me
	every signature you collected, and tell me for each one how you knew who was
	standing there. `unverified` in the summary is the number of rows that cannot
	answer the second half — surfaced rather than buried, because a register that
	reports only its total is a register nobody checks.
	"""
	_require()
	filters = {}

	document_type = as_str(args, "document_type") or as_str(args, "doctype")
	if document_type:
		filters["document_type"] = _resolve_document_type(document_type)
	document_name = as_str(args, "document_name") or as_str(args, "document") or as_str(args, "docname")
	if document_name:
		filters["document_name"] = document_name

	signer = as_str(args, "signer") or as_str(args, "employee")
	if signer:
		filters["signer"] = _resolve_employee(signer)
	badge = as_str(args, "signer_badge") or as_str(args, "badge_id") or as_str(args, "badge")
	if badge:
		filters["signer_badge"] = badge

	company = resolve_company(as_str(args, "company"), required=False)
	if company:
		filters["company"] = company

	role = normalise_signature_role(as_str(args, "signature_role") or as_str(args, "role"))
	if role:
		filters["signature_role"] = role
	method = normalise_verification_method(as_str(args, "verification_method"))
	if method:
		filters["verification_method"] = method
	status = as_str(args, "status")
	if status:
		filters["status"] = _resolve_status(status)

	# BOTH ENDS INCLUSIVE, and `to_date` reaches the end of its day. `signed_at`
	# is a Datetime, so a bare `<= '2026-07-31'` would silently drop every
	# signature collected after midnight on the last day of the range — which on
	# a farm is all of them.
	start = as_date(args, "from_date") or as_date(args, "start_date")
	end = as_date(args, "to_date") or as_date(args, "end_date")
	if start and end:
		filters["signed_at"] = ("between", [f"{start} 00:00:00", f"{end} 23:59:59"])
	elif start:
		filters["signed_at"] = (">=", f"{start} 00:00:00")
	elif end:
		filters["signed_at"] = ("<=", f"{end} 23:59:59")

	limit = as_limit(args)
	rows = frappe.db.get_all(
		SIGNING_EVIDENCE,
		filters=filters,
		fields=list(existing_fields(SIGNING_EVIDENCE, list(LIST_FIELDS))),
		order_by="signed_at desc",
		limit_page_length=limit,
	)
	entries = [dict(row) for row in rows or []]
	unverified = [row for row in entries if str(row.get("status") or "") != "Recorded"]
	data = {
		"evidence": entries,
		"count": len(entries),
		"unverified_count": len(unverified),
		"filters": {key: value for key, value in filters.items() if key != "signed_at"},
		"from_date": start,
		"to_date": end,
	}
	if unverified:
		data["note"] = (
			f"{len(unverified)} of these {len(entries)} signature(s) are Unverified — captured "
			f"without an identity check at the pad, without a document hash, or both. They are "
			f"still signatures; they are not the packet that answers 'how do you know it was him'. "
			f"get_signing_evidence names which half is missing on any one of them."
		)
	summary = f"{len(entries)} signature event(s)"
	if unverified:
		summary += f", {len(unverified)} unverified"
	return ToolResult(data=data, summary=summary)


def get_signing_evidence(args: dict) -> ToolResult:
	"""Read-only. One signature event in full, with the document hash re-checked.

	THE HASH IS RECOMPUTED ON EVERY READ rather than reported as a stored string.
	A column of hex that nobody ever compares is decoration; the fact worth
	handing back is whether the document still hashes to what the signer was
	shown, and that is a question only the server can answer — it is the one that
	knows which columns were excluded.
	"""
	_require()
	name = as_str(args, "name") or as_str(args, "evidence") or as_str(args, "signing_evidence")
	if not name:
		raise ToolError(
			"name is required — the Signing Evidence docname. list_signing_evidence finds them "
			"by document, signer or date."
		)
	row = frappe.db.get_value(
		SIGNING_EVIDENCE,
		name,
		list(existing_fields(SIGNING_EVIDENCE, list(DETAIL_FIELDS))),
		as_dict=True,
	)
	if not row:
		raise ToolError(
			f"no {SIGNING_EVIDENCE} called {name!r} on this site. list_signing_evidence has the register."
		)

	data = dict(row)
	data["tamper_check"] = verify_fingerprint(data)
	data["superseded_by"] = _superseded_by(name)
	data["identity_verified"] = bool(str(row.get("verification_method") or "").strip())
	# v0.63.0. Whether step 5 has happened for this attestation. Reported as its
	# own boolean rather than left to be inferred from a URL, because "there is a
	# sealed copy" and "the document still hashes to what was signed" are the two
	# halves of tamper-evidence and a reader should not have to know that an empty
	# Attach field means the first one is missing.
	data["sealed"] = bool(str(row.get("sealed_pdf") or "").strip())
	data["gps"] = (
		{"latitude": row.get("gps_latitude"), "longitude": row.get("gps_longitude")}
		if row.get("gps_latitude") or row.get("gps_longitude")
		else None
	)
	summary = (
		f"{row.get('signature_role')} signature on {row.get('document_type')} "
		f"{row.get('document_name')} at {row.get('signed_at')}"
	)
	if data["tamper_check"].get("matches") is False:
		summary += " — THE DOCUMENT HAS CHANGED SINCE IT WAS SIGNED"
	return ToolResult(data=data, summary=summary)


def _superseded_by(name: str) -> str | None:
	try:
		found = frappe.db.get_value(SIGNING_EVIDENCE, {"supersedes": name}, "name")
	except Exception:  # pragma: no cover - a site mid-migrate
		return None
	return str(found) if found else None


def _resolve_status(value: str) -> str:
	for status in ("Recorded", "Unverified"):
		if status.casefold() == value.strip().casefold():
			return status
	raise ToolError(f"status must be Recorded or Unverified, got {value!r}.")


def _resolve_document_type(value: str) -> str:
	"""A doctype name, taking the same aliases the signature tools take.

	`i9` is what a model sends and `I-9 Form` is what the column holds, and a
	filter that answered nothing for the first spelling would look exactly like
	a farm that has never collected a signature.
	"""
	from . import signatures

	return signatures.DOCTYPE_ALIASES.get(value.strip().casefold(), value.strip())


def _resolve_employee(value: str) -> str:
	if frappe.db.exists(EMPLOYEE, value):
		return value
	found = frappe.db.get_value(EMPLOYEE, {"employee_name": value}, "name")
	if found:
		return str(found)
	raise ToolError(f"no Employee called {value!r} on this site. Pass the docname or the employee_name.")


__all__ = [
	"EMPLOYER_ROLES",
	"ROLES_BY_BOX",
	"SIGNATURE_ROLES",
	"SIGNING_EVIDENCE",
	"VERIFICATION_METHODS",
	"available",
	"document_fingerprint",
	"get_signing_evidence",
	"list_signing_evidence",
	"normalise_signature_role",
	"normalise_verification_method",
	"record",
	"supersede_target",
	"verify_fingerprint",
]

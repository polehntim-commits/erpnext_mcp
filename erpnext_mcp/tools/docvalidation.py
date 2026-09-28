# SPDX-License-Identifier: MIT
"""Document Intelligence — v0.69.0, Sprint 4. Five tools, and two of them write.

WHAT THIS IS FOR, IN ONE SENTENCE. A phone reads a piece of paper; this decides
whether to believe it.

────────────────────────────────────────────────────────────────────────────
THE ARITHMETIC IS NOT HERE
────────────────────────────────────────────────────────────────────────────

Every rule — the EPA registration number's shape, the restricted-entry interval
against the active ingredient it names, whether a PHI shorter than its own REI
can be true, whether a name on a licence is the name on the record it is filed
against — lives in `erpnext_mcp/document_intel.py`, which is pure and reads no
database. Same split `model_registry.py`, `budget_engine.py` and `payroll_gl.py`
keep, and for the same payoff: the rules can be exercised over a hundred
extractions in a unit test without a bench, and this module stays what it is —
the only place that reads or writes a `Document Validation` document.

────────────────────────────────────────────────────────────────────────────
THE ONE THING THIS MODULE ADDS THAT A PURE FUNCTION CANNOT: THE RECORD
────────────────────────────────────────────────────────────────────────────

`document_intel.validate_extraction` can say a licence names Ana Ruiz. It
cannot say whether Ana Ruiz is who this licence is being filed against, because
that answer is in the Employee register. So `_context_for` is this module's
real job: it reads the record named by `source_doctype`/`source_name` and hands
the pure layer the ONE fact it needs from outside the document. Nothing else
about the source record is consulted, and a source that does not exist is a
warning on the validation rather than a refusal — a foreman photographing a
label at a chemical shed is capturing something before the Item exists, and a
capture that requires master data first is a capture that does not happen.

────────────────────────────────────────────────────────────────────────────
`auto_store` IS TRUE BY DEFAULT, AND WHY THAT IS THE SAFE DIRECTION
────────────────────────────────────────────────────────────────────────────

Storing is the point. A validation that is computed, returned and forgotten
cannot be revalidated when a label is revised, cannot be counted when somebody
asks how many pesticide labels on this site have never been read by a person,
and cannot be found again when a residue detection sends somebody looking for
what was sprayed. `auto_store=false` exists for the one honest case — a client
checking an extraction mid-capture, before the worker has decided to keep the
photograph — and it is the caller saying so rather than the default.

The record it writes is inert. No Compliance Alert, no ToDo, no email, nothing
scheduled. `revalidation_due` is a column `list_revalidation_due` reads; the
sweep that would act on it is a later release's decision and is deliberately
not made here.

────────────────────────────────────────────────────────────────────────────
WHO MAY VALIDATE
────────────────────────────────────────────────────────────────────────────

`VALIDATION_ROLES` is wider than `kpi.KPI_ROLES` and narrower than everybody:
a foreman is on it because a foreman is the person standing at the chemical
shed with the label in one hand, and an HR Manager is on it because the I-9 and
training branches are that register's paperwork. The reads are gated by their
own switches and nothing else, the same way every read tool in this app is.
"""

from __future__ import annotations

import json

import frappe

from .. import compat, document_intel, extraction_config, flags, roles, security
from ..args import as_bool, as_date, as_limit, as_str, resolve_company
from ..errors import ToolError
from ..result import ToolResult

DOCTYPE = "Document Validation"

_MIGRATE_HINT = "It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app."

#: Who may write a validation. See the module docstring's "WHO MAY VALIDATE".
VALIDATION_ROLES = ("System Manager", "Farm Manager", "HR Manager", "Foreman")

#: The fields `list_document_validations` reads. Deliberately NOT `ocr_text` or
#: `extraction_json`: a list of forty validations carrying forty pages of OCR
#: text is a payload a phone on a field connection cannot use, and the whole
#: point of a list is to choose which one to open.
_LIST_FIELDS = (
	"name",
	"document_type",
	"company",
	"source_doctype",
	"source_name",
	"validation_status",
	"overall_confidence",
	"human_confirmed",
	"human_confirmed_at",
	"human_confirmed_by",
	"revalidation_due",
	"last_revalidated",
	"revalidation_count",
	"llm_model",
	"modified",
)

#: `source_doctype` → the field on that doctype holding the name a document
#: should agree with. What `_context_for` consults, and the only thing this
#: module reads out of a source record.
#:
#: The two HR ones are the point: "does the name on this licence match the
#: employee record" is a question about the Employee register, and no pure
#: function can answer it. `Item` is absent on purpose — a pesticide label
#: names a product, and comparing a product name to a licensee name would
#: produce a mismatch on every correctly filed label.
_NAME_FIELD_BY_DOCTYPE = {
	"Employee": ("employee_name",),
	"Certification": ("employee_name", "person_name", "holder_name"),
	"Training": ("employee_name",),
	"Employee Training Record": ("employee_name",),
	"I-9 Form": ("employee_name", "last_name"),
	"User": ("full_name",),
}


#: Where a source record keeps its entity, in the order they are tried. Two
#: spellings because this app's own registers are split between them — `Parcel`
#: and `Housing Unit` say `owning_entity` where everything else says `company`.
_COMPANY_FIELDS = ("company", "owning_entity")


def _require() -> None:
	compat.require_doctype(DOCTYPE, _MIGRATE_HINT)


def _company_for(args: dict, source_doctype: str, source_name: str) -> str:
	"""Which entity this validation belongs to, and why it is worth resolving.

	A Company User Permission restricts every document that links to a Company
	at the framework level. Without a value here an applicator licence's OCR
	text and an I-9 document's number would be readable by anybody on a
	multi-entity site who can read the doctype at all — which is one entity's
	personnel data. See the field's own description.

	THREE SOURCES, IN THIS ORDER, AND THE SECOND IS THE USEFUL ONE. An explicit
	argument wins. Otherwise the SOURCE RECORD's own entity, because a licence
	filed against an Employee belongs to whoever that Employee belongs to and
	nobody should have to say so twice. Otherwise the site's default, which
	resolves on a single-company site and answers "" on a multi-company one —
	where "" is the honest answer and the record is simply unscoped until
	somebody files it against something.
	"""
	explicit = as_str(args, "company")
	if explicit:
		return resolve_company(explicit, required=True) or ""

	if source_doctype and source_name and compat.doctype_exists(source_doctype):
		for fieldname in _COMPANY_FIELDS:
			if not compat.has_field(source_doctype, fieldname):
				continue
			value = frappe.db.get_value(source_doctype, source_name, fieldname)
			if value:
				return str(value)

	return resolve_company(required=False) or ""


def require_validation_role() -> str:
	"""The principal this validation is attributed to, once it has proved it may
	write one. Same identity resolution as `kpi.require_kpi_role` and
	`fill_pipeline.require_foreman_role` — whichever of the request's
	authenticated caller and the session user is present — against
	`VALIDATION_ROLES`."""
	actor = security.caller_identity() or str(getattr(frappe.session, "user", "") or "")
	if not actor or actor == "Guest":
		raise ToolError(
			"this call has no identity to attribute a document validation to. A validation is a "
			"claim about a document somebody photographed, and an unattributable one is worth "
			"less than the photograph. Nothing was changed."
		)
	held = set(frappe.get_roles(actor) or []) or set(roles.all_roles_of(actor) or [])
	if not held & set(VALIDATION_ROLES):
		raise ToolError(
			f"{actor} may not validate documents: it holds none of {', '.join(VALIDATION_ROLES)}. "
			"The reads — get_document_validation, list_document_validations, "
			"list_revalidation_due — are open to anyone the operator has switched them on for. "
			"Nothing was changed."
		)
	return actor


# ── arguments ───────────────────────────────────────────────────────────────


def _as_object(args: dict, key: str, required: bool = False) -> dict:
	"""`args[key]` as a dict — a native JSON object or a JSON string, since a
	model context sends both. `{}` when absent and not required."""
	raw = args.get(key)
	if raw in (None, ""):
		if required:
			raise ToolError(
				f"{key} is required, as a JSON object. It is what on-device extraction pulled "
				f"out of the OCR text — for a pesticide label, epa_registration_number, "
				f"signal_word, rei_hours, phi_days, phi_crop and the rest. Nothing was changed."
			)
		return {}
	if isinstance(raw, dict):
		return raw
	if isinstance(raw, str):
		try:
			parsed = json.loads(raw)
		except (TypeError, ValueError):
			raise ToolError(f"{key} must be valid JSON. Nothing was changed.") from None
		if isinstance(parsed, dict):
			return parsed
	raise ToolError(f"{key} must be a JSON object. Nothing was changed.")


def _document_type(args: dict) -> str:
	raw = as_str(args, "document_type", required=True)
	resolved = document_intel.normalise_document_type(raw)
	if not resolved:
		raise ToolError(
			f"{raw!r} is not a document type. It is one of: "
			f"{', '.join(document_intel.DOCUMENT_TYPES)}. The type decides which rules run, so "
			f"there is no sensible default to fall back on. Nothing was changed."
		)
	return resolved


def _stored_json(doc, fieldname: str) -> dict:
	"""A JSON column read back as a dict, tolerating both shapes Frappe stores
	one in — the parsed value on a fresh document, the string from the database.

	Never raises. A column that will not parse is a record written by something
	other than this module, and refusing to READ it would make a bad row
	unfixable as well as bad.
	"""
	raw = doc.get(fieldname)
	if isinstance(raw, dict):
		return raw
	if isinstance(raw, str) and raw.strip():
		try:
			parsed = json.loads(raw)
		except (TypeError, ValueError):
			return {}
		if isinstance(parsed, dict):
			return parsed
	return {}


# ── the one fact the rules need from outside the document ──────────────────


def _context_for(args: dict, source_doctype: str, source_name: str) -> tuple:
	"""`(context, notes)` — what the pure layer needs from this site.

	`expected_name` passed explicitly wins over anything read from a record: a
	caller filing a licence against an Employee whose legal name differs from
	the one on the credential knows something this lookup does not.

	`notes` are `info` issues about the LOOKUP rather than about the document,
	and they are separate for a reason worth stating: "the record you filed
	this against does not exist" is a fact about the filing, and putting it in
	the same list as "this licence expired" without saying which is which is
	how an issue list stops being read.
	"""
	context: dict = {}
	notes: list = []

	as_of = as_date(args, "as_of")
	if as_of:
		context["as_of"] = as_of

	# v0.201.0. What EPA registers for this product, when the caller looked it
	# up (`/mobile/register_product_label`): checked against the reading as
	# warnings. Internal — no tool schema declares it.
	record = args.get("epa_record")
	if isinstance(record, dict) and record:
		context["epa_record"] = record

	expected = as_str(args, "expected_name")
	if expected:
		context["expected_name"] = expected
		return context, notes

	if not source_doctype or not source_name:
		return context, notes

	candidates = _NAME_FIELD_BY_DOCTYPE.get(source_doctype)
	if not candidates:
		return context, notes

	if not compat.doctype_exists(source_doctype):
		notes.append(
			document_intel.issue(
				"source_doctype_absent",
				document_intel.INFO,
				"source_doctype",
				f"{source_doctype} is not a doctype on this site, so the name on the document "
				f"was not checked against anything. The validation below is the document's own "
				f"consistency and nothing more.",
			)
		)
		return context, notes

	if not frappe.db.exists(source_doctype, source_name):
		notes.append(
			document_intel.issue(
				"source_record_absent",
				document_intel.INFO,
				"source_name",
				f"There is no {source_doctype} called {source_name!r} on this site yet, so the "
				f"name on the document was not checked against it. This is expected when a "
				f"document is captured before the record it belongs to is created.",
			)
		)
		return context, notes

	for fieldname in candidates:
		if not compat.has_field(source_doctype, fieldname):
			continue
		value = frappe.db.get_value(source_doctype, source_name, fieldname)
		if value:
			context["expected_name"] = str(value)
			return context, notes

	notes.append(
		document_intel.issue(
			"source_record_unnamed",
			document_intel.INFO,
			"source_name",
			f"{source_doctype} {source_name} carries no name field this check knows about, so "
			f"the name on the document was not compared to it.",
		)
	)
	return context, notes


# ── reading a document ──────────────────────────────────────────────────────


def _describe(doc, include_payload: bool = True) -> dict:
	out = {
		"name": doc.name,
		"document_type": doc.document_type,
		"company": doc.get("company") or None,
		"validation_status": doc.validation_status,
		"overall_confidence": float(doc.get("overall_confidence") or 0.0),
		"source_doctype": doc.get("source_doctype") or None,
		"source_name": doc.get("source_name") or None,
		"scan_file_url": doc.get("scan_file_url") or None,
		"llm_model": doc.get("llm_model") or None,
		"human_confirmed": bool(compat.checked(doc.get("human_confirmed"))),
		"human_confirmed_at": str(doc.get("human_confirmed_at") or "") or None,
		"human_confirmed_by": doc.get("human_confirmed_by") or None,
		"revalidation_due": str(doc.get("revalidation_due") or "") or None,
		"last_revalidated": str(doc.get("last_revalidated") or "") or None,
		"revalidation_count": int(doc.get("revalidation_count") or 0),
	}
	if include_payload:
		out["ocr_text"] = doc.get("ocr_text") or ""
		out["extraction"] = _stored_json(doc, "extraction_json")
		out["llm_assessment"] = _stored_json(doc, "llm_assessment_json")
	return out


def _reference(args: dict) -> str:
	"""The docname, under either spelling a caller reaches for.

	`name` is what the schema declares; `validation_id` is what
	`validate_document_extraction` HANDS BACK, so a caller holding one naturally
	passes it under that name. The refusal names `name`, because that is the
	argument somebody has to add.
	"""
	return as_str(args, "validation_id") or as_str(args, "name", required=True)


def _resolve(reference: str):
	reference = str(reference or "").strip()
	if not reference:
		raise ToolError(
			"name is required — a Document Validation docname, e.g. DVAL-2026-0001. "
			"list_document_validations has the register."
		)
	if not frappe.db.exists(DOCTYPE, reference):
		raise ToolError(
			f"no Document Validation called {reference!r} on this site. "
			f"list_document_validations has the register."
		)
	return frappe.get_doc(DOCTYPE, reference)


# ── writing one ─────────────────────────────────────────────────────────────


def _store(
	document_type: str,
	ocr_text: str,
	fields: dict,
	assessment_raw,
	result: dict,
	source_doctype: str,
	source_name: str,
	scan_file_url: str,
	company: str,
):
	"""The Document Validation this run becomes. Returns the saved document.

	`llm_assessment_json` holds what the CALLER SENT, not what
	`validate_llm_assessment` made of it. The two differ when an assessment
	arrives half-shaped, and storing the cleaned version would erase the
	evidence of the thing worth finding — a client whose judgement is being
	quietly dropped every time.
	"""
	doc = frappe.new_doc(DOCTYPE)
	doc.document_type = document_type
	doc.company = company or None
	doc.validation_status = result["status"]
	doc.overall_confidence = result["confidence"]
	doc.source_doctype = source_doctype or None
	doc.source_name = source_name or None
	doc.scan_file_url = scan_file_url or None
	doc.ocr_text = ocr_text or None
	doc.extraction_json = json.dumps(fields, default=str)
	doc.llm_assessment_json = json.dumps(assessment_raw, default=str) if assessment_raw else None
	doc.llm_model = result.get("llm_model") or None
	doc.revalidation_due = result.get("revalidation_due") or None
	doc.revalidation_count = 0
	# v0.206.0 (config_flags_triage.md §1.3, §2): which extraction config read
	# it, and the flags in force — what the phone sent, else what the server used.
	if compat.has_field(DOCTYPE, "config_version"):
		doc.config_version = result.get("config_version") or None
	if compat.has_field(DOCTYPE, "feature_flags") and result.get("feature_flags"):
		flags.stamp_all(doc, result["feature_flags"])
	doc.insert(ignore_permissions=True)
	return doc


def _config_in_force(document_type: str) -> str:
	try:
		return extraction_config.active(document_type)[1] or ""
	except Exception:
		return ""


def _payload(result: dict, validation_id: str, stored: bool) -> dict:
	"""The answer, in the shape the frozen contract names: `validation_id`,
	`status`, `confidence`, `issues`, `corrected_fields`."""
	return {
		"validation_id": validation_id,
		"document_type": result.get("document_type"),
		"status": result["status"],
		"confidence": result["confidence"],
		"issues": result["issues"],
		"corrected_fields": result["corrected_fields"],
		"reasoning": result.get("reasoning", ""),
		"coverage": result.get("coverage"),
		"error_count": result.get("error_count", 0),
		"warning_count": result.get("warning_count", 0),
		"llm_available": bool(result.get("llm_available")),
		"llm_model": result.get("llm_model") or "",
		"revalidation_due": result.get("revalidation_due") or "",
		"stored": stored,
		# v0.201.0. Crop or Non-crop, for a pesticide label — which rules it was held to.
		"pesticide_use_scope": result.get("pesticide_use_scope"),
		# v0.206.0. Which extraction config this was read and judged with.
		"config_version": result.get("config_version") or None,
	}


def _summary(document_type: str, result: dict, validation_id: str, stored: bool) -> str:
	where = validation_id if stored else "not stored"
	return (
		f"{document_type}: {result['status']} at confidence {result['confidence']} — "
		f"{result.get('error_count', 0)} error(s), {result.get('warning_count', 0)} warning(s) "
		f"({where})"
	)


# ── 1. validate_document_extraction ─────────────────────────────────────────


def validate_document_extraction(args: dict) -> ToolResult:
	"""MUTATING (default OFF). Check what on-device extraction read off a
	scanned document, and — unless `auto_store=false` — keep the answer.

	The deterministic rules always run. An `llm_assessment` supplied by the
	caller is merged on top of them; without one the status is `Pending` and an
	issue says so by name. See `document_intel` for why the model call is the
	client's rather than this app's.
	"""
	_require()
	require_validation_role()

	document_type = _document_type(args)
	ocr_text = as_str(args, "ocr_text")
	fields = _as_object(args, "extracted_fields", required=True)
	source_doctype = as_str(args, "source_doctype")
	source_name = as_str(args, "source_name")
	scan_file_url = as_str(args, "scan_file_url")
	auto_store = as_bool(args, "auto_store", default=True)

	if source_name and not source_doctype:
		raise ToolError(
			"source_name was given without source_doctype, so there is no register to look the "
			"record up in. Pass both or neither. Nothing was changed."
		)

	assessment_raw = args.get("llm_assessment")
	assessment, assessment_problems = document_intel.validate_llm_assessment(assessment_raw)
	llm_model = as_str(args, "llm_model")

	context, notes = _context_for(args, source_doctype, source_name)

	deterministic = document_intel.validate_extraction(document_type, ocr_text, fields, context)
	deterministic["issues"] = list(deterministic["issues"]) + notes + assessment_problems
	result = document_intel.merge_llm_assessment(deterministic, assessment, llm_model)
	# v0.206.0. The phone says which config it read with; a server-side run
	# records the one in force for the type.
	sent_version = as_str(args, "config_version")[:140]
	result["config_version"] = sent_version or result.get("config_version") or _config_in_force(document_type)
	result["feature_flags"] = flags.clean(args.get("feature_flags"))

	validation_id = ""
	if auto_store:
		doc = _store(
			document_type,
			ocr_text,
			fields,
			assessment_raw,
			result,
			source_doctype,
			source_name,
			scan_file_url,
			_company_for(args, source_doctype, source_name),
		)
		validation_id = doc.name

	return ToolResult(
		data=_payload(result, validation_id, bool(auto_store)),
		summary=_summary(document_type, result, validation_id, bool(auto_store)),
		docstatus_delta="none → 0 (draft)" if auto_store else "",
	)


# ── 2. get_document_validation ──────────────────────────────────────────────


def get_document_validation(args: dict) -> ToolResult:
	"""One Document Validation in full, OCR text and stored extraction included.
	Read-only."""
	_require()
	doc = _resolve(_reference(args))
	return ToolResult(
		data=_describe(doc),
		summary=(
			f"{doc.name}: {doc.document_type} — {doc.validation_status} at confidence "
			f"{float(doc.get('overall_confidence') or 0.0)}"
		),
	)


# ── 3. list_document_validations ────────────────────────────────────────────


def list_document_validations(args: dict) -> ToolResult:
	"""The validation register, newest first. Read-only.

	Carries neither `ocr_text` nor the stored extraction — see `_LIST_FIELDS`.
	"""
	_require()
	filters: dict = {}

	document_type = as_str(args, "document_type")
	if document_type:
		resolved = document_intel.normalise_document_type(document_type)
		if not resolved:
			raise ToolError(
				f"{document_type!r} is not a document type. It is one of: "
				f"{', '.join(document_intel.DOCUMENT_TYPES)}."
			)
		filters["document_type"] = resolved

	status = as_str(args, "status") or as_str(args, "validation_status")
	if status:
		resolved_status = document_intel.normalise_status(status)
		if not resolved_status:
			raise ToolError(
				f"{status!r} is not a validation status. It is one of: "
				f"{', '.join(document_intel.VALIDATION_STATUSES)}."
			)
		filters["validation_status"] = resolved_status

	# Only when the caller asked for one. `resolve_company("")` would answer with
	# the site's own company on a single-company site, and a filter nobody
	# requested would quietly hide any record that carries no entity at all.
	if as_str(args, "company"):
		filters["company"] = resolve_company(as_str(args, "company"), required=True)

	for key in ("source_doctype", "source_name"):
		value = as_str(args, key)
		if value:
			filters[key] = value

	confirmed = as_bool(args, "human_confirmed", default=None)
	if confirmed is not None:
		filters["human_confirmed"] = 1 if confirmed else 0

	rows = frappe.db.get_all(
		DOCTYPE,
		filters=filters,
		fields=list(_LIST_FIELDS),
		order_by="modified desc",
		limit=as_limit(args),
	)
	for row in rows:
		row["human_confirmed"] = bool(compat.checked(row.get("human_confirmed")))

	return ToolResult(
		data={"count": len(rows), "filters": filters, "validations": rows},
		summary=f"{len(rows)} document validation(s)"
		+ (f" matching {filters}" if filters else " (unfiltered)"),
	)


# ── 4. revalidate_document ──────────────────────────────────────────────────


def revalidate_document(args: dict) -> ToolResult:
	"""MUTATING (default OFF). Re-run the checks against what is already stored.

	NOTHING IS RE-PHOTOGRAPHED AND NOTHING IS RE-EXTRACTED. The stored
	`ocr_text` and `extraction_json` are the input, which is the entire reason
	they are kept — a label whose registered intervals were revised, or a
	licence that has since expired, gets a fresh answer without anybody walking
	back to the chemical shed.

	`extracted_fields` MAY be passed to replace the stored extraction, for the
	one case that is not a re-run: a person corrected a misread field and wants
	the checks made against the corrected reading. When it is passed, the
	stored extraction is replaced and the record says so through
	`revalidation_count` like any other re-run.
	"""
	_require()
	require_validation_role()

	doc = _resolve(_reference(args))
	reason = as_str(args, "reason")

	replacement = _as_object(args, "extracted_fields")
	fields = replacement or _stored_json(doc, "extraction_json")
	ocr_text = as_str(args, "ocr_text") or (doc.get("ocr_text") or "")

	if not fields:
		raise ToolError(
			f"{doc.name} carries no stored extraction, so there is nothing to re-check against. "
			f"Pass extracted_fields to supply one, or run validate_document_extraction again "
			f"with the document. Nothing was changed."
		)

	if "llm_assessment" in args:
		assessment_raw = args.get("llm_assessment")
	else:
		# The stored assessment is reused rather than dropped: a re-run that
		# silently lost the judgement half would move a Validated record to
		# Pending and look like the document had gone stale, when what changed
		# was only that nobody re-sent the assessment.
		assessment_raw = _stored_json(doc, "llm_assessment_json") or None
	assessment, assessment_problems = document_intel.validate_llm_assessment(assessment_raw)
	llm_model = as_str(args, "llm_model") or (doc.get("llm_model") or "")

	context, notes = _context_for(args, doc.get("source_doctype") or "", doc.get("source_name") or "")

	deterministic = document_intel.validate_extraction(doc.document_type, ocr_text, fields, context)
	deterministic["issues"] = list(deterministic["issues"]) + notes + assessment_problems
	result = document_intel.merge_llm_assessment(deterministic, assessment, llm_model)

	was_status = doc.validation_status
	was_confidence = float(doc.get("overall_confidence") or 0.0)

	doc.validation_status = result["status"]
	doc.overall_confidence = result["confidence"]
	doc.llm_model = result.get("llm_model") or None
	doc.revalidation_due = result.get("revalidation_due") or None
	doc.last_revalidated = frappe.utils.now()
	doc.revalidation_count = int(doc.get("revalidation_count") or 0) + 1
	if replacement:
		doc.extraction_json = json.dumps(fields, default=str)
	if as_str(args, "ocr_text"):
		doc.ocr_text = ocr_text
	if "llm_assessment" in args:
		doc.llm_assessment_json = json.dumps(assessment_raw, default=str) if assessment_raw else None
	doc.save(ignore_permissions=True)

	payload = _payload(result, doc.name, stored=True)
	payload.update(
		{
			"revalidation_count": doc.revalidation_count,
			"last_revalidated": str(doc.last_revalidated),
			"previous_status": was_status,
			"previous_confidence": was_confidence,
			"status_changed": was_status != result["status"],
			"extraction_replaced": bool(replacement),
			"reason": reason,
		}
	)
	changed = (
		f"{was_status} → {result['status']}"
		if was_status != result["status"]
		else f"still {result['status']}"
	)
	return ToolResult(
		data=payload,
		summary=(
			f"{doc.name} revalidated (#{doc.revalidation_count}): {changed} at confidence "
			f"{result['confidence']}" + (f" — {reason}" if reason else "")
		),
	)


# ── 5. list_revalidation_due ────────────────────────────────────────────────


def list_revalidation_due(args: dict) -> ToolResult:
	"""Which stored validations are due to be re-checked, soonest first. Read-only.

	`revalidation_due` is empty on the document types that never go stale, and
	an empty date is NOT due — a receipt does not become questionable by
	sitting. That is enforced by the filter rather than left to a reader, so a
	site with ten thousand receipts and forty licences gets a list of forty.
	"""
	_require()
	as_of = as_date(args, "as_of") or frappe.utils.today()

	filters = {"revalidation_due": ("<=", as_of)}
	if as_str(args, "company"):
		filters["company"] = resolve_company(as_str(args, "company"), required=True)
	document_type = as_str(args, "document_type")
	if document_type:
		resolved = document_intel.normalise_document_type(document_type)
		if not resolved:
			raise ToolError(
				f"{document_type!r} is not a document type. It is one of: "
				f"{', '.join(document_intel.DOCUMENT_TYPES)}."
			)
		filters["document_type"] = resolved

	rows = [
		row
		for row in frappe.db.get_all(
			DOCTYPE,
			filters=filters,
			fields=list(_LIST_FIELDS),
			order_by="revalidation_due asc",
			limit=as_limit(args),
		)
		if row.get("revalidation_due")
	]
	for row in rows:
		row["human_confirmed"] = bool(compat.checked(row.get("human_confirmed")))
		row["days_overdue"] = _days_between(row.get("revalidation_due"), as_of)

	never_confirmed = len([row for row in rows if not row["human_confirmed"]])
	return ToolResult(
		data={
			"as_of": as_of,
			"count": len(rows),
			"never_human_confirmed": never_confirmed,
			"validations": rows,
		},
		summary=(
			f"{len(rows)} document validation(s) due for revalidation as of {as_of}"
			f" — {never_confirmed} of them never confirmed by a person"
		),
	)


def _days_between(due, as_of) -> int:
	"""How many days past `as_of` the due date is. Zero rather than negative for
	one falling today, since the filter has already excluded the future."""
	try:
		return max(0, int(frappe.utils.date_diff(as_of, due)))
	except Exception:
		return 0

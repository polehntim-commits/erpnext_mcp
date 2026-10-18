# SPDX-License-Identifier: MIT
"""Producing an audit packet, and filing it where it can be found again.

THE PACKET IS A DOCUMENT, NOT A RESULT. `generate_compliance_packet` returns its
payload inline, because a reconciliation packet is a value somebody reads once.
An audit packet is a thing that gets printed, carried into a room, and produced
again in two years when the same auditor asks what happened. So it is rendered to
a PDF, attached to a Governance Document in the company's archive, and the tool
returns forty characters of file_url rather than a megabyte of base64 — which is
also the only way it fits inside the per-call ceiling.

PDF IS THE DEFAULT AND DOCX IS AVAILABLE. That order is deliberate and was
learned rather than chosen: a .docx handed to somebody who cannot open it is a
document that did not arrive, which happened on 2026-07-29 and is why
`generate_quarterly_investment_report` defaults the same way.

IDEMPOTENCE IS BY (audit_type, company, period). A second call for the same three
is refused unless `overwrite=true`, because the most likely thing at that
coordinate is the packet somebody produced last week — and two packets for one
audit period, differing in whatever changed in between, is a question nobody wants
to be asked.

THE KAIROTIC GATE LIVES IN `audit_packets.readiness` and is a REFUSAL. A packet
asserts a compliant period; an open corrective action inside that period
contradicts the assertion, and a warning at the top of a printed document is not
read by the person the document is handed to. Overriding is possible, awkward, and
puts the open items in a section at the FRONT.

`stage_via_chunks` ROUTES THE ASSEMBLED BYTES THROUGH THE STAGING PIPELINE, and
it is worth being straight about when that matters. The bytes never cross the MCP
boundary — this tool builds them on the site — so no chunking is needed to get
them anywhere. What staging buys is a CHECKPOINT: a worker killed between
assembling a forty-megabyte season and writing it leaves a resumable session and a
per-piece digest rather than a timeout. On a two-hundred-kilobyte packet that is
ceremony, and this tool says so in the result rather than pretending otherwise.
"""

import frappe

from .. import audit_packets, compat, training
from ..args import as_bool, as_date, as_str, resolve_company
from ..errors import ToolError
from ..render import docx as docx_render
from ..render import pdf as pdf_render
from ..result import ToolResult
from . import artifacts, files, uploads

GOVERNANCE_DOCUMENT = "Governance Document"

#: The Governance Document category an audit packet is filed under. Added to the
#: doctype's Select in v0.15.0; a site whose doctype predates that gets "Other"
#: and a note, rather than a refusal — the packet is the point, not its label.
CATEGORY = "Audit Packet"
CATEGORY_FALLBACK = "Other"

#: Past this, staging is worth the extra rows. Below it, the checkpoint costs
#: more than the failure it protects against. See the module docstring.
STAGING_WORTH_IT_BYTES = 2 * 1024 * 1024


def list_audit_packet_types(args: dict) -> ToolResult:
	"""Which audit types this site can assemble a packet for, and what each pulls in."""
	described = []
	for key in audit_packets.names():
		spec = audit_packets.TYPES[key]
		entry = spec.describe()
		missing = []
		for section in entry["sections"]:
			for doctype in _SECTION_DOCTYPES.get(section, ()):
				if not compat.doctype_exists(doctype):
					missing.append(f"{section} → {doctype}")
		entry["sections_that_will_be_empty_here"] = sorted(set(missing))
		described.append(entry)

	return ToolResult(
		data={
			"audit_types": described,
			"count": len(described),
			"sections": list(audit_packets.SECTION_ORDER),
			"note": (
				"A section listed under `sections_that_will_be_empty_here` has no DocType behind "
				"it on this site, and the packet will SAY SO rather than omit the section. An "
				"absent spray-records section reads as an operation with nothing to declare; a "
				"section that names the DocType it could not find reads as the truth."
			),
			"kairotic_gate": (
				"Every type refuses on a period that is not genuinely closed: one that has not "
				"finished, or one whose corrective actions are still open. Neither is a warning "
				"— a warning at the top of a printed document is not read by the person the "
				"document is handed to."
			),
		},
		summary=f"{len(described)} audit packet type(s) available",
	)


#: Which doctypes each section needs, for the availability report above.
_SECTION_DOCTYPES = {
	"policies": ("Compliance Policy",),
	"certifications": ("Certification",),
	"workforce": ("Employee",),
	"training": ("Employee Training Record",),
	# v0.90.0. Spray Application is erpnext_mcp's OWN doctype and ships with the
	# app, so this section stopped being one that "will be empty here" for the
	# five packet types that carry it. farm_precision_ag's Spray Log is still
	# read by the section builder for a site that sprayed under the old app —
	# it is not named here because its absence no longer empties anything.
	"spray_records": ("Spray Application",),
	"water": ("Field",),
	"traceability": ("Bucket Log Entry",),
	"housing": ("Housing Unit",),
	"filings": ("Regulatory Filing",),
	"audits": ("Audit Event",),
	"open_actions": ("Audit Event",),
}


def generate_audit_packet(args: dict) -> ToolResult:
	"""Assemble one audit type's evidence for one period, and file it."""
	audit_type = as_str(args, "audit_type", required=True)
	spec = audit_packets.get(audit_type)
	if spec is None:
		raise ToolError(
			f"no audit packet type {audit_type!r}. This app knows: "
			f"{', '.join(audit_packets.names())}. list_audit_packet_types describes each one. "
			"Nothing was created."
		)

	company = resolve_company(as_str(args, "company"), required=True)
	start = as_date(args, "period_start", required=True)
	end = as_date(args, "period_end", required=True)
	if end < start:
		raise ToolError(f"period_end {end} is before period_start {start}. Nothing was created.")

	regime = as_str(args, "regime")
	if regime:
		canonical = training.canon(regime)
		if not canonical:
			raise ToolError(
				f"regime {regime!r} is not one this app knows. {training.vocabulary_note()} It "
				"narrows the TRAINING and OPEN-ITEMS sections to one scheme and changes nothing "
				"else in the packet. Nothing was created."
			)
		# v0.19.2: `regime` scopes two sections now, so a packet type carrying
		# EITHER is one the argument does something to. Refusing on the absence of
		# `training` alone would have started refusing calls that are now
		# meaningful.
		scoped = [key for key in ("training", "alerts") if key in spec.sections]
		if not scoped:
			raise ToolError(
				f"the {spec.key} packet has neither a training nor an open-items section, so a "
				"regime filter would change nothing about it. Drop `regime`, or choose an audit "
				"type that carries one: "
				f"{', '.join(sorted(key for key, value in audit_packets.TYPES.items() if {'training', 'alerts'} & set(value.sections)))}. "
				"Nothing was created."
			)
		regime = canonical

	output_format = (as_str(args, "output_format") or "pdf").strip().lower()
	if output_format not in ("pdf", "docx"):
		raise ToolError(
			f"output_format must be 'pdf' or 'docx', got {output_format!r}. PDF is the default "
			"and the one to use: a .docx is a file the recipient may not be able to open, which "
			"is exactly what happened on 2026-07-29. Nothing was created."
		)

	overwrite = as_bool(args, "overwrite", False)
	allow_open = as_bool(args, "allow_open_actions", False)
	title = _archive_title(spec, company, start, end, regime)

	existing = frappe.db.get_value(GOVERNANCE_DOCUMENT, {"title": title, "company": company}, "name")
	if existing and not overwrite:
		raise ToolError(
			f"an audit packet for {spec.key} covering {start} to {end} is already filed as "
			f"{existing}. Two packets for one audit period, differing in whatever changed in "
			"between, is a question nobody wants to be asked — so this refuses rather than "
			"files a second. Pass overwrite=true to replace it, or read the one that is there "
			"with get_governance_document_content. Nothing was created."
		)

	readiness = audit_packets.readiness(spec, company, start, end)
	if not readiness["ready"] and not allow_open:
		_refuse(spec, readiness, start, end)

	packet = audit_packets.build(
		spec,
		company,
		start,
		end,
		allow_open_actions=allow_open and bool(readiness["blockers"]),
		regime=regime,
	)

	if as_bool(args, "dry_run", False):
		return ToolResult(
			data={
				**_plan(packet, spec, company, start, end, title, output_format),
				"dry_run": True,
				"created": False,
				"readiness": readiness,
				"note": (
					"Nothing was written and no Governance Document was created. The packet was "
					"assembled in memory, so every count above is the count the real packet "
					"would carry. Call again with dry_run=false to file it."
				),
			},
			summary=(
				f"dry run: would file a {spec.key} audit packet for {company} covering "
				f"{start} to {end} — {packet['total_records']} record(s)"
			),
		)

	content, file_name = _render(packet, spec, company, start, end, output_format)
	staged = _stage(content, spec, company, start, end, args)

	if existing and overwrite:
		document = frappe.get_doc(GOVERNANCE_DOCUMENT, existing)
		document.notes = _archive_notes(packet, readiness)
		document.save(ignore_permissions=True)
	else:
		document = _file_governance_document(title, company, start, end, packet, readiness)

	attachment = artifacts.attach_bytes(
		GOVERNANCE_DOCUMENT,
		document.name,
		file_name,
		content,
		field="attached_file",
		parent_authorized=True,
	)
	if staged and staged.get("staged"):
		# The File is the record now. Staging that outlives the file it built is
		# just rubbish on the site — and the checkpoint's whole job ended the
		# moment there was something durable to point at.
		uploads.clear_internal_session(staged["session"])

	written = None
	output_path = artifacts.resolve_output_path(as_str(args, "output_path"), file_name)
	if output_path:
		written = artifacts.write_output(output_path, content, overwrite)

	data = {
		**_plan(packet, spec, company, start, end, title, output_format),
		"dry_run": False,
		"created": True,
		"replaced": bool(existing and overwrite),
		"governance_document": document.name,
		"attachment": artifacts.describe_attachment(attachment, content),
		"output_file": written,
		"readiness": readiness,
		"staging": staged,
		"packet": packet,
		"note": (
			f"Filed as Governance Document {document.name} in {company}'s archive, with the "
			f"{output_format.upper()} attached as a PRIVATE File. "
			"get_governance_document_content reads it back. The bytes are NOT returned inline: a "
			"packet is measured in megabytes and a file_url is measured in characters."
		),
		"next_step": (
			"get_governance_document_content reads the packet back. list_governance_documents "
			f"shows every packet filed for {company}."
		),
	}
	if packet["produced_over_open_actions"]:
		data["warning"] = (
			"THIS PACKET WAS PRODUCED OVER OPEN CORRECTIVE ACTIONS, because allow_open_actions "
			"was passed. They are disclosed in a section at the FRONT of the document rather "
			"than buried — an operation handing something over mid-remediation is better served "
			"by disclosing the remediation than by having the auditor find it. Do not hand this "
			"over believing it asserts a closed period."
		)
	return ToolResult(
		data=data,
		summary=(
			f"filed {spec.key} audit packet for {company} covering {start} to {end} as "
			f"{document.name} ({packet['total_records']} record(s), "
			f"{files.human_size(len(content))})"
			+ (" — OVER OPEN CORRECTIVE ACTIONS" if packet["produced_over_open_actions"] else "")
		),
		docstatus_delta="none → 0 (created)",
	)


def _refuse(spec, readiness: dict, start: str, end: str) -> None:
	"""The kairotic gate, as a sentence somebody can act on."""
	lines = []
	for blocker in readiness["blockers"]:
		lines.append(blocker["detail"])
		for action in blocker.get("actions") or ():
			overdue = f", {action['days_overdue']} day(s) overdue" if action.get("days_overdue") else ""
			lines.append(
				f"    {action['audit']} action {action['index']} ({action['severity']}, due "
				f"{action['due_date'] or 'no deadline'}{overdue}): "
				f"{str(action['finding'] or '')[:120]}"
			)
	raise ToolError(
		f"the period {start} to {end} is not closed, so a {spec.key} audit packet for it would "
		"assert something that is not yet true.\n\n"
		+ "\n".join(lines)
		+ "\n\nClose each action with update_audit_event's close_corrective_action, saying what "
		"actually changed, then close the audit with close_audit_event. If this genuinely has "
		"to go out mid-remediation, pass allow_open_actions=true — the open items are then "
		"listed in a section at the FRONT of the document, which is the honest way to hand over "
		"an unfinished period. Nothing was created."
	)


def _plan(packet: dict, spec, company: str, start: str, end: str, title: str, output_format: str) -> dict:
	return {
		"training_regime": packet.get("training_regime_override"),
		"audit_type": spec.key,
		"title": spec.title,
		"regulator": spec.regulator,
		"company": company,
		"period_start": start,
		"period_end": end,
		"output_format": output_format,
		"archive_title": title,
		"section_counts": packet["section_counts"],
		"total_records": packet["total_records"],
		"disclosures": packet["disclosures"],
	}


def _archive_title(spec, company: str, start: str, end: str, regime: str = "") -> str:
	"""The Governance Document title, which is also the idempotence key.

	Deterministic in (audit_type, company, period, regime) and nothing else. A
	title carrying the generation date would make every run a different document
	and the overwrite check would never fire.

	THE REGIME IS PART OF THE KEY (v0.19.0) because a WPS-narrowed GAP packet and
	a full GAP packet are different documents with different contents. Filing the
	second over the first would silently replace a buyer's evidence bundle with a
	narrower one, and the operator would find out when the buyer did.
	"""
	scoped = f" [{regime} training]" if regime else ""
	return f"{spec.key} Audit Packet{scoped} — {company} — {start} to {end}"


def _archive_notes(packet: dict, readiness: dict) -> str:
	lines = [
		f"{packet['title']} audit packet covering {packet['period_start']} to {packet['period_end']}.",
		f"{packet['total_records']} record(s) across {len(packet['sections'])} section(s).",
		f"Assembled by erpnext_mcp {packet['generator_version']} on {packet['generated_at']} "
		f"by {packet['generated_by']}.",
	]
	if packet["produced_over_open_actions"]:
		lines.append(
			f"PRODUCED OVER {readiness['open_action_count']} OPEN CORRECTIVE ACTION(S). The open "
			"items are disclosed in the first section of the document."
		)
	for entry in packet["disclosures"]:
		lines.append(f"Disclosure — {entry['section']}: {entry['detail']}")
	return "\n".join(lines)


def _file_governance_document(title: str, company: str, start: str, end: str, packet: dict, readiness: dict):
	doc = frappe.new_doc(GOVERNANCE_DOCUMENT)
	doc.title = title
	doc.company = company
	doc.category = _category()
	doc.effective_date = end
	doc.notes = _archive_notes(packet, readiness)
	doc.insert(ignore_permissions=True)
	return doc


def _category() -> str:
	""" "Audit Packet" where the site's doctype offers it, "Other" where it does not.

	A site running an older migration has a Governance Document whose category
	Select predates v0.15.0, and refusing to file the packet over a label would be
	losing the document to protect its filing. The note on the document says which
	it got.
	"""
	try:
		field = compat.field_meta(GOVERNANCE_DOCUMENT, "category")
		options = [line.strip() for line in str((field or {}).get("options") or "").split("\n")]
		return CATEGORY if CATEGORY in options else CATEGORY_FALLBACK
	except Exception:  # pragma: no cover
		return CATEGORY_FALLBACK


def _render(packet: dict, spec, company: str, start: str, end: str, output_format: str):
	sections = list(audit_packets.document_sections(packet))
	stem = f"{spec.key}-audit-packet-{_slug(company)}-{start}-to-{end}"
	if packet.get("training_regime_override"):
		stem = f"{stem}-{_slug(packet['training_regime_override'])}-training"
	if output_format == "docx":
		return _render_docx(packet, sections), f"{stem}.docx"
	return _render_pdf(packet, sections), f"{stem}.pdf"


def _slug(value: str) -> str:
	out = "".join(character if character.isalnum() else "-" for character in str(value))
	while "--" in out:
		out = out.replace("--", "-")
	return out.strip("-").lower() or "company"


def _render_pdf(packet: dict, sections) -> bytes:
	document = pdf_render.PdfDocument(
		title=f"{packet['title']} audit packet",
		author="erpnext_mcp",
		subject=f"{packet['audit_type']} {packet['period_start']} to {packet['period_end']}",
		footer=(
			f"{packet['company'] or 'all companies'} — {packet['audit_type']} "
			f"{packet['period_start']}/{packet['period_end']} — erpnext_mcp "
			f"{packet['generator_version']}"
		),
	)
	for kind, payload in sections:
		if kind == "title":
			document.title_block(payload[0], *payload[1])
		elif kind == "heading":
			document.heading(payload)
		elif kind == "subheading":
			document.subheading(payload)
		elif kind == "paragraph":
			document.paragraph(payload)
		elif kind == "bullets":
			document.bullets(payload)
		elif kind == "key_values":
			document.key_values(payload)
		elif kind == "table":
			document.table(payload[0], payload[1], align=payload[2])
		elif kind == "page_break":
			document.page_break()
	return document.render()


def _render_docx(packet: dict, sections) -> bytes:
	document = docx_render.DocxDocument(
		title=f"{packet['title']} audit packet",
		subject=f"{packet['audit_type']} {packet['period_start']} to {packet['period_end']}",
	)
	for kind, payload in sections:
		if kind == "title":
			document.title_block(payload[0], *payload[1])
		elif kind == "heading":
			document.heading(payload)
		elif kind == "subheading":
			document.subheading(payload)
		elif kind == "paragraph":
			document.paragraph(payload)
		elif kind == "bullets":
			document.bullets(payload)
		elif kind == "key_values":
			document.key_values(payload)
		elif kind == "table":
			document.table(payload[0], payload[1])
		elif kind == "page_break":
			document.page_break()
	return document.render()


def _stage(content: bytes, spec, company: str, start: str, end: str, args: dict):
	"""Checkpoint the assembled bytes, where that is worth doing.

	Default TRUE and honest about it: below `STAGING_WORTH_IT_BYTES` the checkpoint
	costs more than the failure it protects against, so it is skipped and the
	result says why. A caller who wants it either way passes it explicitly.
	"""
	wanted = as_bool(args, "stage_via_chunks", None)
	if wanted is False:
		return None
	if wanted is None and len(content) < STAGING_WORTH_IT_BYTES:
		return {
			"staged": False,
			"reason": (
				f"the packet is {files.human_size(len(content))}, under the "
				f"{files.human_size(STAGING_WORTH_IT_BYTES)} at which checkpointing earns its "
				"keep, so it was written in one go. Staging protects a long assembly from a "
				"worker restart; on a document this size the extra rows cost more than the "
				"failure they guard against. Pass stage_via_chunks=true to stage it anyway."
			),
		}
	try:
		result = uploads.stage_internal_bytes(content, f"audit-packet-{spec.key}-{_slug(company)}-{start}")
	except Exception as exc:
		# Staging is a checkpoint, not a requirement. Losing the checkpoint is not
		# a reason to lose the packet.
		return {
			"staged": False,
			"reason": f"staging was attempted and failed ({type(exc).__name__}: {exc}). The packet was written directly.",
		}
	return {
		"staged": True,
		"session": result["session"],
		"chunks": result["chunks"],
		"sha256": result["sha256"],
		"note": (
			"The assembled document was written through this site's staging tables in "
			f"{result['chunks']} checkpointed piece(s), read back out and verified against its "
			"own digest before the File was created. The session was cleared once the File "
			"existed."
		),
	}

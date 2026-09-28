# SPDX-License-Identifier: MIT
"""The three compliance records a task completion produces, and their tools.

A Housing Inspection, a Detector Test and a Water Test are the evidence half of
the loop Sprint 7 opened. Sprint 7 could tell an operation that a cabin had not
been walked in fourteen months; it had no way for anybody to walk it and say so.
These are the records that say so, and their existence is what dismisses the
alert — not a button somebody presses on the alert itself.

THAT DISTINCTION IS THE WHOLE ARCHITECTURE. An alert is a statement about the
state of the world, computed nightly from operational records. The only honest
way to make one go away is to change the world and let the sweep notice. So
recording an inspection moves the unit's `last_habitability_inspection`, and
`housing_inspection_overdue` auto-dismisses on the next sweep because its
condition is no longer true. Nothing here touches a Compliance Alert directly,
and nothing here needs to.

BOTH DOORS ARE OPEN, DELIBERATELY. A record can be written by completing a Farm
Task — which is the designed path, carrying the evidence contract and the chain
of custody — or created directly with these tools by somebody who walked a cabin
because they were passing. A compliance record that can ONLY be written by
finishing a dispatched task is a compliance record nobody writes on the day the
dispatch board is down, and the walk still happened.

THE WORKFLOW BRANCH LIVES IN THE CONTROLLER, NOT HERE. `erpnext_mcp/records.py`
argues it: the state is a function of the findings, computed on every save, so a
record typed into the Desk behaves exactly as one written through a tool. These
tools report what the branch decided; they do not decide it.

WHAT `update_*` IS FOR, AND WHY IT IS NOT A SECOND CREATE. A laboratory answers
three days after the sample was taken. A closure note arrives a fortnight after
the finding. These are corrections and completions of one record, not new
records, and filing them as new ones would produce two rows about one sample
whose only difference is which was typed second.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from dataclasses import field as dataclass_field

import frappe

from .. import compat, records
from ..args import as_bool, as_choice, as_date, as_limit, as_str, resolve_company
from ..erpnext_mcp.doctype.detector_test.detector_test import DETECTORS, FAIL, NOT_PRESENT, PASS
from ..erpnext_mcp.doctype.farm_task.farm_task import AVAILABLE, DISPATCH_DISPATCHED
from ..errors import ToolError
from ..result import ToolResult

HOUSING_INSPECTION = "Housing Inspection"
DETECTOR_TEST = "Detector Test"
WATER_TEST = "Water Test"
FARM_TASK = "Farm Task"
EVIDENCE_CHILD = "Farm Task Evidence"

RECORD_CAP = 500

#: How many evidence rows one call will file. Twenty photographs of one cabin is
#: a thorough inspection; two hundred is somebody's camera roll.
EVIDENCE_CAP = 40


@dataclass(frozen=True)
class RecordSpec:
	"""What the three records have in common, named once so reads stay generic."""

	doctype: str
	subject_field: str
	subject_doctype: str
	date_field: str
	person_field: str
	person_label: str
	photo_field: str
	fields: tuple = dataclass_field(default_factory=tuple)


_COMMON = (
	"name",
	"workflow_state",
	"company",
	"source_task",
	"findings",
	"corrective_action_closed",
	"closure_note",
	"notes",
	"creation",
	"owner",
)

SPECS = {
	HOUSING_INSPECTION: RecordSpec(
		doctype=HOUSING_INSPECTION,
		subject_field="unit",
		subject_doctype="Housing Unit",
		date_field="inspection_date",
		person_field="inspector",
		person_label="inspector",
		photo_field="photos",
		fields=(
			*_COMMON,
			"unit",
			"inspection_date",
			"inspector",
			"inspector_name",
			"corrective_action",
			"signature",
		),
	),
	DETECTOR_TEST: RecordSpec(
		doctype=DETECTOR_TEST,
		subject_field="unit",
		subject_doctype="Housing Unit",
		date_field="test_date",
		person_field="tester",
		person_label="tester",
		photo_field="photos",
		fields=(
			*_COMMON,
			"unit",
			"test_date",
			"tester",
			"tester_name",
			"smoke_detector_result",
			"co_detector_result",
			"replacement_needed",
			"replacement_task",
		),
	),
	WATER_TEST: RecordSpec(
		doctype=WATER_TEST,
		subject_field="source",
		subject_doctype="Irrigation Zone",
		date_field="test_date",
		person_field="tester",
		person_label="sampler",
		photo_field="sample_photos",
		fields=(
			*_COMMON,
			"source",
			"block",
			"farm_location_gps",
			"test_date",
			"tester",
			"tester_name",
			"laboratory",
			"lab_sample_id",
			"sample_collected_on",
			"lab_reported_on",
			"coliform_result",
			"ecoli_result",
			"contamination_detected",
			"lab_report",
		),
	),
}


# ── shared ──────────────────────────────────────────────────────────────────
def _require(doctype: str) -> None:
	compat.require_doctype(
		doctype,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)


def _company(args: dict) -> str | None:
	return resolve_company(as_str(args, "company") or as_str(args, "owning_entity"), required=False)


def _row(doctype: str, name: str) -> dict:
	spec = SPECS[doctype]
	name = (name or "").strip()
	if not name:
		raise ToolError(f"{doctype.lower()} is required (a {doctype} docname).")
	if not frappe.db.exists(doctype, name):
		raise ToolError(
			f"no {doctype} called {name!r} on this site. list_{_slug(doctype)}s has the register."
		)
	return dict(
		frappe.db.get_value(doctype, name, compat.existing_fields(doctype, spec.fields), as_dict=True) or {}
	)


def _slug(doctype: str) -> str:
	return doctype.replace(" ", "_").lower()


def _describe(doctype: str, row: dict, with_evidence: bool = False) -> dict:
	spec = SPECS[doctype]
	out = {key: row.get(key) for key in spec.fields if key in row}
	out["doctype"] = doctype
	out["name"] = row.get("name")
	out["subject"] = row.get(spec.subject_field)
	out["subject_doctype"] = spec.subject_doctype
	out["date"] = str(row.get(spec.date_field) or "") or None
	out["state"] = row.get("workflow_state") or records.DRAFT
	out["is_draft"] = out["state"] == records.DRAFT
	out["found_something"] = out["state"] == records.CORRECTIVE_ACTION_REQUIRED
	out["corrective_action_open"] = bool(
		out["found_something"] and not str(row.get("corrective_action_closed") or "").strip()
	)
	for key in ("creation", "corrective_action_closed"):
		if out.get(key) is not None:
			out[key] = str(out[key]) or None
	if with_evidence:
		out["evidence"] = _evidence_of(doctype, row.get("name"), spec.photo_field)
	return out


def _evidence_of(doctype: str, name: str, photo_field: str) -> list:
	if not compat.doctype_exists(EVIDENCE_CHILD):
		return []
	rows = frappe.db.get_all(
		EVIDENCE_CHILD,
		filters={"parent": name, "parenttype": doctype, "parentfield": photo_field},
		fields=["name", "evidence_type", "file", "file_url", "caption", "captured_on", "idx"],
		order_by="idx asc",
		limit=EVIDENCE_CAP * 2,
	)
	return [
		{
			"evidence_type": row.get("evidence_type"),
			"file": row.get("file"),
			"file_url": row.get("file_url"),
			"caption": row.get("caption"),
			"captured_on": str(row.get("captured_on") or "") or None,
		}
		for row in rows or []
	]


def normalise_evidence(raw, label: str = "evidence_files", *, enrich: bool = False) -> list:
	"""Turn whatever a caller sent into evidence rows, or refuse saying why.

	Three shapes are accepted because three are what a caller genuinely has: a
	File docname (what `commit_staged_file` hands back), a file URL (what an
	older attachment flow has), and a full object with a type, a caption and —
	since v0.96.0 — a `phase` (what a mobile app that took the photograph knows).

	A FILE DOCNAME THAT DOES NOT EXIST IS REFUSED. Evidence pointing at nothing
	is worse than no evidence: it satisfies a contract, files a row, and produces
	a compliance record whose proof is a broken link nobody discovers until an
	auditor clicks it.
	"""
	if raw in (None, ""):
		return []
	if isinstance(raw, (str, dict)):
		raw = [raw]
	if not isinstance(raw, list):
		raise ToolError(
			f"{label} must be a list of File docnames, file URLs, or objects like "
			'{"file": "<File docname>", "evidence_type": "Photo", "caption": "..."}. '
			f"Got {type(raw).__name__}."
		)
	if len(raw) > EVIDENCE_CAP:
		raise ToolError(
			f"{label} has {len(raw)} entries, past the {EVIDENCE_CAP} cap. Twenty photographs of "
			"one cabin is a thorough inspection; two hundred is a camera roll. Nothing was written."
		)

	out = []
	for index, entry in enumerate(raw):
		if isinstance(entry, str):
			entry = {"file_url": entry} if _looks_like_url(entry) else {"file": entry}
		if not isinstance(entry, dict):
			raise ToolError(f"{label}[{index}] must be a string or an object, got {type(entry).__name__}.")

		# v0.18.2: the Farm Ops iOS app serializes StagedFileRef as
		# `{"file_token": ..., "file_name": ..., "sha256": ..., "kind": ...}`
		# per fafo_ios/FarmOpsKit/Sources/FarmOpsKit/Models/CompletionSubmission.swift:29.
		# `file_token` IS the File docname (finalize_staged_file returns it that way
		# in api/files.py). Accepting it alongside `file` means the mobile app's
		# completion doesn't need to rename its serializer, and MCP callers that
		# have always sent `file` still work. Same for `kind` alongside `evidence_type`.
		file_name = str(entry.get("file") or entry.get("file_token") or "").strip()
		file_url = str(entry.get("file_url") or "").strip()
		if not (file_name or file_url):
			raise ToolError(f"{label}[{index}] names neither a `file` nor a `file_url`.")

		if file_name:
			if not frappe.db.exists("File", file_name):
				raise ToolError(
					f"{label}[{index}] points at File {file_name!r}, which is not on this site. "
					"Evidence pointing at nothing satisfies a contract and proves nothing — it is "
					"the one kind of missing evidence nobody discovers until an auditor clicks it. "
					"Upload it with stage_file_chunk and commit_staged_file first. Nothing was written."
				)
			file_url = file_url or str(frappe.db.get_value("File", file_name, "file_url") or "")

		# v0.18.2: `kind` is the iOS spelling — LocalEvidenceFile.kind is a
		# lowercase enum ("photo", "signature"). Title-case it before the choice
		# validator sees it; unknown values fall through and are refused by
		# as_choice with the doctype's own error, which is the same refusal as
		# an unknown evidence_type sent by any other caller.
		evidence_type_raw = entry.get("evidence_type") or entry.get("kind") or ""
		evidence_type = str(evidence_type_raw).strip()
		if evidence_type and evidence_type[0].islower():
			evidence_type = evidence_type[:1].upper() + evidence_type[1:]
		if not evidence_type:
			evidence_type = "Photo"
		# v0.96.0: `phase` is before-or-after, and UNSET IS THE ORDINARY CASE.
		# Most completions have no before frame and never will — the crew is
		# gone and the cabin is clean — so a phase that defaulted to anything, or
		# that was required, would refuse the one submission a worker cannot
		# redo. It is emitted ONLY when the caller sent one, which is what keeps
		# the row identical to a pre-v0.96.0 row for every existing caller and
		# for the other child tables this normaliser feeds.
		#
		# LOWER-CASED BEFORE THE VALIDATOR, the mirror of what `kind` gets above:
		# the handset's `EvidencePhase` is a Swift String enum whose raw values
		# are `before` and `after`, so those are the stored spellings, and an MCP
		# caller who writes "Before" is meeting the same tolerance rather than a
		# refusal. Anything else falls through to `as_choice`, which refuses with
		# the doctype's own list.
		row = {
			"evidence_type": as_choice(EVIDENCE_CHILD, "evidence_type", evidence_type, "evidence_type"),
			"file": file_name or None,
			"file_url": file_url or None,
			"caption": str(entry.get("caption") or "").strip() or None,
			"captured_on": _captured_on(entry),
		}
		phase = str(entry.get("phase") or "").strip().lower()
		if not phase and enrich:
			phase = _phase_from_name(entry.get("file_name") or entry.get("caption") or file_url)
		if phase:
			row["phase"] = as_choice(EVIDENCE_CHILD, "phase", phase, f"{label}[{index}].phase")
		if enrich:
			for key, column in (("latitude", "gps_latitude"), ("longitude", "gps_longitude")):
				value = _coordinate(entry.get(key) if entry.get(key) is not None else entry.get(column))
				if value is not None and compat.has_field(EVIDENCE_CHILD, column):
					row[column] = value
		out.append(row)
	return out


def _captured_on(entry: dict) -> str | None:
	"""When the photo was taken, in the SITE'S zone, from either spelling.

	v0.198.0. `captured_at` is the handset's name for it; `captured_on` is the
	column's. An ISO stamp with an offset is moved into the site's zone — the
	column sits beside `completed_at` and every other site-local stamp — and an
	unreadable one is dropped rather than failing a completion over its least
	important field (MariaDB 1292 is what a raw `…Z` used to earn here).
	"""
	raw = entry.get("captured_at") or entry.get("captured_on")
	if raw in (None, ""):
		return None
	from .. import datetimes, timezones

	return datetimes.as_site_datetime(raw, timezones.site_timezone()[0]) or None


#: The token every shipped build of the phone writes into a photo's file name —
#: `FT-…_photo_before_<uuid>.jpg` — because it named the phase that way before
#: the column existed. See `api/mobile._evidence`.
_PHASE_TOKENS = {"_before_": "before", "_after_": "after"}


def _phase_from_name(name) -> str:
	"""`before` / `after` off a file name the phone wrote, or "" when it says neither.

	v0.198.0. FT-2026-09-00004's six photos said before and after in their names
	and nothing in their `phase` column, because the build that took them never
	sent the key. A name that says it is the answer; one that does not is left
	unset, as a phase always has been when nobody knew.
	"""
	text = str(name or "").lower()
	for token, phase in _PHASE_TOKENS.items():
		if token in text:
			return phase
	return ""


def _coordinate(value) -> float | None:
	"""A latitude or longitude as a float, or None — never a refusal, for `_captured_on`'s reason."""
	if value in (None, ""):
		return None
	try:
		number = float(value)
	except (TypeError, ValueError):
		return None
	return number if -180.0 <= number <= 180.0 else None


def _looks_like_url(value: str) -> bool:
	value = (value or "").strip()
	return value.startswith("/") or value.startswith("http://") or value.startswith("https://")


def _attach_evidence(doc, fieldname: str, rows) -> None:
	for row in rows or []:
		doc.append(fieldname, dict(row))


def _file_docname(reference) -> str:
	"""The File docname behind a docname or a file URL, or "".

	Evidence arrives as EITHER — `normalise_evidence` accepts both and keeps
	them in different keys, and an Attach field holds the URL rather than the
	docname. A permission cascade that only understood one of the two spellings
	would leave the other half of the evidence on a record unreadable, which is
	the whole bug this exists to close.
	"""
	value = str(reference or "").strip()
	if not value:
		return ""
	try:
		if frappe.db.exists("File", value):
			return value
		if value.startswith("/") or value.startswith("http"):
			return str(frappe.db.get_value("File", {"file_url": value}, "name") or "")
	except Exception:  # pragma: no cover - a site that cannot read its own File table
		return ""
	return ""


def _link_evidence_files_to_parent(parent_doc, rows, *attach_fields) -> None:
	"""Make the File records point at the compliance record they belong to.

	Farm Ops uploads Files as private, owned by the mobile user who took the
	photo. Left that way, only the uploader can read them — an auditor opening
	a Housing Inspection sees the child rows but the Preview refuses with
	"Insufficient Permission" because the File itself is not attached to any
	document Frappe knows to cascade permissions from.

	Setting `attached_to_doctype` / `attached_to_name` is the Frappe idiom for
	"this file belongs to that record" — Frappe's own File.has_permission then
	answers "yes" for anyone who can read the parent, which is the correct
	audience for evidence. Called AFTER the parent is inserted so we have a
	real name to point at.

	v0.18.5 EXTENDS IT TO THE ATTACH FIELDS, and to evidence rows that named a
	URL rather than a docname. v0.18.4 walked `rows` and read only `row["file"]`,
	which covers the child tables and nothing else. Two evidence files on these
	doctypes do not live in a child table:

	  * `Housing Inspection.signature` — the attestation. `complete_farm_task`
	    puts `signature_file` there rather than in `photos`, so every completion
	    filed through the MCP tool with a signature (as opposed to the app, which
	    sends it as a Signature-typed evidence row) left the ONE file an auditor
	    is most certain to open readable only by the person who uploaded it.
	  * `Water Test.lab_report` — the laboratory's own PDF, which is the entire
	    evidentiary content of a water test.

	The failure was silent in exactly the way v0.18.4's was: the record renders,
	the field shows a filename, and the Preview refuses.
	"""
	if not (getattr(parent_doc, "name", None) and getattr(parent_doc, "doctype", None)):
		return

	references = []
	for row in rows or []:
		if not isinstance(row, dict):
			continue
		references.append(row.get("file") or row.get("file_url"))
	for fieldname in attach_fields:
		references.append(parent_doc.get(fieldname))

	seen = set()
	for reference in references:
		try:
			file_name = _file_docname(reference)
			if not file_name or file_name in seen:
				continue
			seen.add(file_name)
			frappe.db.set_value(
				"File",
				file_name,
				{
					"attached_to_doctype": parent_doc.doctype,
					"attached_to_name": parent_doc.name,
					# Kept private — the parent's permission is now the gate.
				},
				update_modified=False,
			)
		except Exception:
			# One file that could not be linked must not block the whole
			# completion — the row in the child table is still there and the
			# link can be rebuilt with a repair pass.
			continue


def _register_writes(doc) -> list:
	"""What the controller's write-back did, for the tool to report.

	The controller stashes it on `flags` rather than returning it, because Frappe
	calls `on_update` and discards the return value. Reading it here is what lets
	a caller be told 'the unit's inspection date moved to 2026-07-31' instead of
	being left to diff the register afterwards.
	"""
	single = getattr(doc.flags, "register_write", None)
	if single:
		return [single]
	return list(getattr(doc.flags, "register_writes", None) or [])


def _outcome(doc, writes: list) -> dict:
	state = str(doc.workflow_state or records.DRAFT)
	advanced = [write for write in writes if write.get("advanced")]
	held = [write for write in writes if write.get("skipped")]
	out = {
		"workflow_state": state,
		"found_something": state == records.CORRECTIVE_ACTION_REQUIRED,
		"register_writes": writes,
		"registers_advanced": [f"{w['doctype']} {w['name']}.{w['fieldname']} → {w['now']}" for w in advanced],
	}
	if state == records.DRAFT:
		out["note"] = (
			"This record is a Draft. Nothing was written to the register and no compliance alert "
			"will dismiss because of it — a draft is a note, not evidence. Clear keep_as_draft "
			"when the record is finished."
		)
	elif state == records.CORRECTIVE_ACTION_REQUIRED:
		out["note"] = (
			"Findings were recorded, so this went to Corrective Action Required rather than "
			"Recorded. The work itself IS recorded — the register moved forward and the alert that "
			"asked for it will dismiss on the next sweep — and a Critical alert now stands against "
			"this record until the finding is closed or a later clean record for the same subject "
			"supersedes it. That is the point: doing the work and finding a problem are two "
			"different facts and both are true."
		)
	if held:
		out["registers_not_moved"] = [write["skipped"] for write in held]
	return out


def _person(args: dict, key: str, name_key: str) -> tuple:
	"""An employee id and a display name, validated where there is a register."""
	from .housing import EMPLOYEE, hr_installed

	worker = as_str(args, key)
	display = as_str(args, name_key)
	if worker and hr_installed() and not frappe.db.exists(EMPLOYEE, worker):
		raise ToolError(
			f"no Employee {worker!r} on this site. A compliance record naming somebody payroll has "
			"never heard of has already drifted from the operation it describes. "
			"list_employees has the register. Nothing was created."
		)
	if worker and not display and compat.doctype_exists("Employee"):
		display = str(frappe.db.get_value("Employee", worker, "employee_name") or "")
	return worker, display or worker


# ── reads ───────────────────────────────────────────────────────────────────
def _list(doctype: str, args: dict) -> ToolResult:
	_require(doctype)
	spec = SPECS[doctype]
	company = _company(args)
	limit = min(as_limit(args), RECORD_CAP)

	filters = {}
	if company:
		filters["company"] = company
	subject = as_str(args, "subject") or as_str(args, spec.subject_field)
	if subject:
		filters[spec.subject_field] = subject
	state = as_str(args, "state") or as_str(args, "workflow_state")
	if state:
		filters["workflow_state"] = as_choice(doctype, "workflow_state", state, "state")
	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	if from_date and to_date:
		filters[spec.date_field] = ("between", [from_date, to_date])
	elif from_date:
		filters[spec.date_field] = (">=", from_date)
	elif to_date:
		filters[spec.date_field] = ("<=", to_date)
	if as_bool(args, "source_task_only", False):
		filters["source_task"] = ("is", "set")

	rows = frappe.db.get_all(
		doctype,
		filters=filters,
		fields=compat.existing_fields(doctype, spec.fields),
		order_by=f"{spec.date_field} desc",
		limit=limit,
	)
	described = [_describe(doctype, dict(row)) for row in rows or []]
	open_actions = [row["name"] for row in described if row["corrective_action_open"]]
	drafts = [row["name"] for row in described if row["is_draft"]]

	data = {
		"doctype": doctype,
		"company": company,
		"count": len(described),
		"limit": limit,
		"truncated": len(described) >= limit,
		"open_corrective_actions": open_actions,
		"drafts": drafts,
		"records": described,
		"note": (
			f"{len(open_actions)} record(s) found something nobody has closed. Those are the list "
			"worth acting on: an operation is judged on closing findings, not on having none."
			if open_actions
			else "No open findings in this selection."
		),
	}
	if drafts:
		data["draft_note"] = (
			f"{len(drafts)} record(s) are still Draft. A draft writes nothing to the register and "
			"dismisses no alert — the work it describes is not evidence yet."
		)
	return ToolResult(
		data=data,
		summary=(
			f"{len(described)} {doctype.lower()} record(s); {len(open_actions)} with an open finding, "
			f"{len(drafts)} draft"
		),
	)


def _get(doctype: str, args: dict) -> ToolResult:
	_require(doctype)
	spec = SPECS[doctype]
	row = _row(doctype, as_str(args, "record") or as_str(args, _slug(doctype), required=True))
	described = _describe(doctype, row, with_evidence=True)

	subject = row.get(spec.subject_field)
	history = []
	if subject:
		history = [
			{
				"name": entry.get("name"),
				"date": str(entry.get(spec.date_field) or "") or None,
				"state": entry.get("workflow_state"),
			}
			for entry in frappe.db.get_all(
				doctype,
				filters={spec.subject_field: subject},
				fields=["name", spec.date_field, "workflow_state"],
				order_by=f"{spec.date_field} desc",
				limit=50,
			)
			or []
		]
	described["subject_history"] = history
	described["superseded_by"] = _superseding(doctype, row)
	return ToolResult(
		data=described,
		summary=(
			f"{doctype} {row.get('name')} on {described['subject']} — {described['state']}"
			+ (", finding still open" if described["corrective_action_open"] else "")
		),
	)


def superseding_record(doctype: str, row: dict) -> str | None:
	"""A later CLEAN record for the same subject, which supersedes this finding.

	This is how a corrective action closes without anybody remembering a field: a
	cabin re-inspected in September with nothing found says more about the water
	stain from July than a checkbox does.
	"""
	spec = SPECS[doctype]
	subject = row.get(spec.subject_field)
	date = str(row.get(spec.date_field) or "")
	if not (subject and date):
		return None
	later = frappe.db.get_all(
		doctype,
		filters={
			spec.subject_field: subject,
			spec.date_field: (">", date),
			"workflow_state": records.RECORDED,
		},
		fields=["name"],
		order_by=f"{spec.date_field} asc",
		limit=1,
	)
	return str(later[0]["name"]) if later else None


def _superseding(doctype: str, row: dict):
	if str(row.get("workflow_state") or "") != records.CORRECTIVE_ACTION_REQUIRED:
		return None
	return superseding_record(doctype, row)


# ── the record builders, shared with complete_farm_task ─────────────────────
def build_housing_inspection(payload: dict, evidence: list) -> object:
	"""Insert one Housing Inspection. Used by the tool AND by task completion."""
	_require(HOUSING_INSPECTION)
	unit = str(payload.get("unit") or "").strip()
	if not unit:
		raise ToolError("unit is required (the Housing Unit that was walked).")
	if not frappe.db.exists("Housing Unit", unit):
		raise ToolError(
			f"no Housing Unit called {unit!r} on this site. list_housing_units has the camp "
			"register. Nothing was created."
		)

	doc = frappe.new_doc(HOUSING_INSPECTION)
	doc.unit = unit
	doc.inspection_date = payload.get("inspection_date") or frappe.utils.today()
	doc.inspector = payload.get("inspector") or ""
	doc.inspector_name = payload.get("inspector_name") or payload.get("inspector") or ""
	doc.findings = payload.get("findings") or ""
	doc.corrective_action = payload.get("corrective_action") or ""
	doc.signature = payload.get("signature") or ""
	doc.notes = payload.get("notes") or ""
	doc.source_task = payload.get("source_task") or None
	doc.keep_as_draft = 1 if payload.get("keep_as_draft") else 0
	_attach_evidence(doc, "photos", evidence)
	doc.insert(ignore_permissions=True)
	# v0.18.4+: without this, uploader-owned private Files are unreadable
	# by anyone else — including the operator opening the record in Desk.
	# v0.18.5 adds `signature`, which is an Attach rather than a child row and
	# was therefore the one piece of evidence the v0.18.4 pass walked straight past.
	_link_evidence_files_to_parent(doc, evidence, "signature")
	return doc


def build_detector_test(payload: dict, evidence: list) -> object:
	"""Insert one Detector Test, and raise a replacement task where one is needed."""
	_require(DETECTOR_TEST)
	unit = str(payload.get("unit") or "").strip()
	if not unit:
		raise ToolError("unit is required (the Housing Unit whose detectors were tested).")
	if not frappe.db.exists("Housing Unit", unit):
		raise ToolError(
			f"no Housing Unit called {unit!r} on this site. list_housing_units has the camp "
			"register. Nothing was created."
		)

	doc = frappe.new_doc(DETECTOR_TEST)
	doc.unit = unit
	doc.test_date = payload.get("test_date") or frappe.utils.today()
	doc.tester = payload.get("tester") or ""
	doc.tester_name = payload.get("tester_name") or payload.get("tester") or ""
	doc.smoke_detector_result = _detector_result(
		payload.get("smoke_detector_result"), "smoke_detector_result"
	)
	doc.co_detector_result = _detector_result(payload.get("co_detector_result"), "co_detector_result")
	doc.replacement_needed = 1 if payload.get("replacement_needed") else 0
	doc.findings = payload.get("findings") or ""
	doc.notes = payload.get("notes") or ""
	doc.source_task = payload.get("source_task") or None
	doc.keep_as_draft = 1 if payload.get("keep_as_draft") else 0
	_attach_evidence(doc, "photos", evidence)
	doc.insert(ignore_permissions=True)
	_link_evidence_files_to_parent(doc, evidence)  # v0.18.4+

	if doc.replacement_needed and doc.workflow_state != records.DRAFT:
		task = raise_replacement_task(doc)
		if task:
			frappe.db.set_value(DETECTOR_TEST, doc.name, "replacement_task", task)
			doc.replacement_task = task
	return doc


def _detector_result(value, label: str) -> str:
	value = str(value or "").strip() or PASS
	if value not in (PASS, FAIL, NOT_PRESENT):
		raise ToolError(f"{label} must be one of: {PASS}, {FAIL}, {NOT_PRESENT}. Got {value!r}.")
	return value


def raise_replacement_task(doc) -> str | None:
	"""Raise a Farm Task to go and fit the detector this test says is missing.

	'Replacement needed' as a checkbox with nobody dispatched against it is a
	finding that stays true until the next annual test discovers it again. This
	is the one place a compliance record creates work rather than merely
	recording it, and it is deliberate: a failed CO detector in an occupied cabin
	is not a reporting problem.

	It NEVER RAISES. A detector test that could not be filed because the task it
	wanted to create was refused would lose the test as well as the task, and the
	test is the evidence.
	"""
	if not compat.doctype_exists(FARM_TASK):
		return None
	faults = doc.faults() or ["a detector is at the end of its service life"]
	try:
		task = frappe.new_doc(FARM_TASK)
		task.task_name = f"Fit detector — {doc.unit}"
		task.task_type = "Repair"
		task.state = AVAILABLE
		task.urgency = "Critical" if doc.faults() else "Normal"
		task.company = doc.company
		task.location_doctype = "Housing Unit"
		task.location = doc.unit
		task.skill_required = "camp_maintenance"
		# Dispatched rather than Self-pick: somebody has to be sent, and a
		# building where the CO detector does not work is not work that waits in
		# a pool for whoever fancies it.
		task.dispatch_mode = DISPATCH_DISPATCHED
		task.estimated_duration_minutes = 30
		task.evidence_required = '{"photos": true, "findings_text": true}'
		task.creates_record = DETECTOR_TEST
		task.creates_record_data = json.dumps({"unit": doc.unit})
		task.notes = (
			f"Detector test {doc.name} on {doc.test_date} found: "
			+ "; ".join(faults)
			+ ". Fit or repair, then record a fresh Detector Test — a clean one supersedes this "
			"finding and clears the alert."
		)
		task.insert(ignore_permissions=True)
		return task.name
	except Exception:
		return None


def build_water_test(payload: dict, evidence: list) -> object:
	"""Insert one Water Test."""
	_require(WATER_TEST)
	source = str(payload.get("source") or "").strip()
	if not source:
		raise ToolError("source is required (the Irrigation Zone the sample came from).")
	if not frappe.db.exists("Irrigation Zone", source):
		raise ToolError(
			f"no Irrigation Zone called {source!r} on this site. list_irrigation_zones has the "
			"register. Nothing was created."
		)

	doc = frappe.new_doc(WATER_TEST)
	doc.source = source
	doc.test_date = payload.get("test_date") or frappe.utils.today()
	doc.tester = payload.get("tester") or ""
	doc.tester_name = payload.get("tester_name") or payload.get("tester") or ""
	doc.laboratory = payload.get("laboratory") or ""
	doc.lab_sample_id = payload.get("lab_sample_id") or ""
	doc.sample_collected_on = payload.get("sample_collected_on") or None
	doc.lab_reported_on = payload.get("lab_reported_on") or None
	doc.coliform_result = payload.get("coliform_result") or ""
	doc.ecoli_result = payload.get("ecoli_result") or ""
	doc.lab_report = payload.get("lab_report") or ""
	doc.findings = payload.get("findings") or ""
	doc.notes = payload.get("notes") or ""
	doc.source_task = payload.get("source_task") or None
	# v0.19.1. §112.161(a)(1)(i) asks for the location as well as the name; the
	# zone says which water, this says which standpipe. See the doctype field.
	doc.farm_location_gps = payload.get("farm_location_gps") or ""
	doc.keep_as_draft = 1 if payload.get("keep_as_draft") else 0
	_attach_evidence(doc, "sample_photos", evidence)
	doc.insert(ignore_permissions=True)
	# `lab_report` is an Attach and is the entire evidentiary content of a water
	# test — see `_link_evidence_files_to_parent` on why v0.18.4 missed it.
	_link_evidence_files_to_parent(doc, evidence, "lab_report")  # v0.18.4+, v0.18.5
	return doc


#: Which builder produces which doctype. `complete_farm_task` reads this to
#: decide whether it can honour a task's `creates_record` promise, and
#: `create_farm_task` reads it to warn when it cannot.
BUILDERS = {
	HOUSING_INSPECTION: build_housing_inspection,
	DETECTOR_TEST: build_detector_test,
	WATER_TEST: build_water_test,
}


# ── Housing Inspection ──────────────────────────────────────────────────────
def list_housing_inspections(args: dict) -> ToolResult:
	"""Every habitability walk, newest first, with the open findings named."""
	return _list(HOUSING_INSPECTION, args)


def get_housing_inspection(args: dict) -> ToolResult:
	"""One walk in full, with its evidence and the unit's whole inspection history."""
	return _get(HOUSING_INSPECTION, args)


def create_housing_inspection(args: dict) -> ToolResult:
	"""Record one habitability walk, and move the unit's inspection date forward."""
	_require(HOUSING_INSPECTION)
	inspector, inspector_name = _person(args, "inspector", "inspector_name")
	evidence = normalise_evidence(args.get("photos"), "photos")
	doc = build_housing_inspection(
		{
			"unit": as_str(args, "unit", required=True),
			"inspection_date": as_date(args, "inspection_date"),
			"inspector": inspector,
			"inspector_name": inspector_name,
			"findings": as_str(args, "findings"),
			"corrective_action": as_str(args, "corrective_action"),
			"signature": as_str(args, "signature"),
			"notes": as_str(args, "notes"),
			"source_task": as_str(args, "source_task"),
			"keep_as_draft": as_bool(args, "keep_as_draft", False),
		},
		evidence,
	)
	writes = _register_writes(doc)
	return ToolResult(
		data={
			**_describe(HOUSING_INSPECTION, dict(doc.as_dict()), with_evidence=True),
			**_outcome(doc, writes),
			"evidence_count": len(evidence),
		},
		summary=(
			f"recorded housing inspection {doc.name} on {doc.unit} ({doc.workflow_state}, "
			f"{len(evidence)} evidence file(s))"
		),
		docstatus_delta="none → 0 (created)",
	)


def update_housing_inspection(args: dict) -> ToolResult:
	"""Correct or close one walk: findings, corrective action, closure, evidence."""
	return _update(HOUSING_INSPECTION, args, ("findings", "corrective_action", "signature", "notes"))


# ── Detector Test ───────────────────────────────────────────────────────────
def list_detector_tests(args: dict) -> ToolResult:
	"""Every detector test, newest first, with the failures and gaps named."""
	return _list(DETECTOR_TEST, args)


def get_detector_test(args: dict) -> ToolResult:
	"""One test in full, with its evidence and the building's testing history."""
	return _get(DETECTOR_TEST, args)


def create_detector_test(args: dict) -> ToolResult:
	"""Record one smoke and CO detector test, and move the unit's dates forward."""
	_require(DETECTOR_TEST)
	tester, tester_name = _person(args, "tester", "tester_name")
	evidence = normalise_evidence(args.get("photos"), "photos")
	doc = build_detector_test(
		{
			"unit": as_str(args, "unit", required=True),
			"test_date": as_date(args, "test_date"),
			"tester": tester,
			"tester_name": tester_name,
			"smoke_detector_result": as_str(args, "smoke_detector_result"),
			"co_detector_result": as_str(args, "co_detector_result"),
			"replacement_needed": as_bool(args, "replacement_needed", False),
			"findings": as_str(args, "findings"),
			"notes": as_str(args, "notes"),
			"source_task": as_str(args, "source_task"),
			"keep_as_draft": as_bool(args, "keep_as_draft", False),
		},
		evidence,
	)
	writes = _register_writes(doc)
	data = {
		**_describe(DETECTOR_TEST, dict(doc.as_dict()), with_evidence=True),
		**_outcome(doc, writes),
		"faults": doc.faults(),
		"evidence_count": len(evidence),
	}
	if doc.replacement_task:
		data["replacement_task"] = doc.replacement_task
		data["replacement_note"] = (
			f"Farm Task {doc.replacement_task} was raised to go and fit it. 'Replacement needed' "
			"as a checkbox with nobody dispatched against it is a finding that stays true until "
			"next year's test discovers it again."
		)
	if any(str(doc.get(fieldname) or "") == NOT_PRESENT for fieldname, _d, _l in DETECTORS):
		data["not_present_note"] = (
			"A detector recorded as Not Present writes NO date to the unit, so "
			"housing_detector_test_stale goes on saying nobody knows whether it works — which is "
			"the truth, because there is nothing there to have tested."
		)
	return ToolResult(
		data=data,
		summary=(
			f"recorded detector test {doc.name} on {doc.unit} "
			f"(smoke {doc.smoke_detector_result}, CO {doc.co_detector_result}, {doc.workflow_state})"
		),
		docstatus_delta="none → 0 (created)",
	)


def update_detector_test(args: dict) -> ToolResult:
	"""Correct or close one detector test: results, findings, closure, evidence."""
	return _update(
		DETECTOR_TEST,
		args,
		("findings", "notes"),
		choices=("smoke_detector_result", "co_detector_result"),
		checks=("replacement_needed",),
	)


# ── Water Test ──────────────────────────────────────────────────────────────
def list_water_tests(args: dict) -> ToolResult:
	"""Every agricultural water sample, newest first, with contamination named."""
	return _list(WATER_TEST, args)


def get_water_test(args: dict) -> ToolResult:
	"""One sample in full, with its laboratory report and the zone's history."""
	return _get(WATER_TEST, args)


def create_water_test(args: dict) -> ToolResult:
	"""Record one water sample, and move the zone's AND the block's test date forward."""
	_require(WATER_TEST)
	tester, tester_name = _person(args, "tester", "tester_name")
	evidence = normalise_evidence(args.get("sample_photos"), "sample_photos")
	doc = build_water_test(
		{
			"source": as_str(args, "source", required=True),
			"test_date": as_date(args, "test_date"),
			"tester": tester,
			"tester_name": tester_name,
			"laboratory": as_str(args, "laboratory"),
			"lab_sample_id": as_str(args, "lab_sample_id"),
			"sample_collected_on": as_str(args, "sample_collected_on"),
			"lab_reported_on": as_date(args, "lab_reported_on"),
			"coliform_result": as_str(args, "coliform_result"),
			"ecoli_result": as_str(args, "ecoli_result"),
			"lab_report": as_str(args, "lab_report"),
			"findings": as_str(args, "findings"),
			"notes": as_str(args, "notes"),
			"source_task": as_str(args, "source_task"),
			"farm_location_gps": as_str(args, "farm_location_gps"),
			"keep_as_draft": as_bool(args, "keep_as_draft", False),
		},
		evidence,
	)
	writes = _register_writes(doc)
	data = {
		**_describe(WATER_TEST, dict(doc.as_dict()), with_evidence=True),
		**_outcome(doc, writes),
		"concerns": doc.concerns(),
		"evidence_count": len(evidence),
		"ecoli_action_level_cfu_per_100ml": records.ECOLI_ACTION_LEVEL_CFU,
	}
	if not doc.lab_report:
		data["missing_lab_report"] = (
			"No laboratory report is attached. Every other field on this record is a "
			"transcription of one, and the report is the only part nobody can reconstruct."
		)
	return ToolResult(
		data=data,
		summary=(
			f"recorded water test {doc.name} on {doc.source} "
			f"(coliform {doc.coliform_result or 'unrecorded'}, E. coli "
			f"{doc.ecoli_result or 'unrecorded'}, {doc.workflow_state})"
		),
		docstatus_delta="none → 0 (created)",
	)


def update_water_test(args: dict) -> ToolResult:
	"""File the laboratory's answer against a sample, or close a contamination finding."""
	return _update(
		WATER_TEST,
		args,
		(
			"laboratory",
			"lab_sample_id",
			"coliform_result",
			"ecoli_result",
			"lab_report",
			"findings",
			"notes",
		),
		dates=("lab_reported_on",),
	)


# ── the shared update ───────────────────────────────────────────────────────
def _update(
	doctype: str, args: dict, texts: tuple, choices: tuple = (), checks: tuple = (), dates: tuple = ()
) -> ToolResult:
	"""Change one record, echoing every change as before → after.

	The closure fields are common to all three, so they are handled here rather
	than repeated: closing a finding is the same act whether the finding was a
	water stain, a dead detector or a coliform count.
	"""
	_require(doctype)
	spec = SPECS[doctype]
	row = _row(doctype, as_str(args, "record") or as_str(args, _slug(doctype), required=True))
	doc = frappe.get_doc(doctype, row["name"])

	changes = {}
	for key in texts:
		if key in args:
			_stage(changes, doc, key, as_str(args, key))
	for key in choices:
		if key in args:
			_stage(changes, doc, key, as_choice(doctype, key, as_str(args, key), key))
	for key in checks:
		if key in args:
			_stage(changes, doc, key, 1 if as_bool(args, key) else 0)
	for key in (*dates, "corrective_action_closed"):
		if key in args:
			_stage(changes, doc, key, as_date(args, key) or "")
	if "closure_note" in args:
		_stage(changes, doc, "closure_note", as_str(args, "closure_note"))
	if "keep_as_draft" in args:
		_stage(changes, doc, "keep_as_draft", 1 if as_bool(args, "keep_as_draft") else 0)
	if spec.date_field in args:
		_stage(changes, doc, spec.date_field, as_date(args, spec.date_field))

	evidence = normalise_evidence(args.get(spec.photo_field) or args.get("evidence_files"), spec.photo_field)
	if evidence:
		_attach_evidence(doc, spec.photo_field, evidence)
		changes[spec.photo_field] = {"before": "", "after": f"+{len(evidence)} file(s)"}

	if not changes:
		fields = ", ".join(
			sorted(
				set(
					texts
					+ choices
					+ checks
					+ dates
					+ ("corrective_action_closed", "closure_note", "keep_as_draft", spec.photo_field)
				)
			)
		)
		raise ToolError(f"nothing to change. Pass at least one of: {fields}.")

	was = str(
		doc.get_doc_before_save().workflow_state
		if doc.get_doc_before_save()
		else row.get("workflow_state") or ""
	)
	doc.save(ignore_permissions=True)
	writes = _register_writes(doc)

	return ToolResult(
		data={
			**_describe(doctype, dict(doc.as_dict()), with_evidence=True),
			**_outcome(doc, writes),
			"changes": changes,
			"state_was": was or None,
		},
		summary=(
			f"updated {doctype.lower()} {doc.name}: {', '.join(sorted(changes))} "
			f"({was or '?'} → {doc.workflow_state})"
		),
		docstatus_delta="0 → 0 (updated)",
	)


def _stage(changes: dict, doc, fieldname: str, value) -> None:
	before = doc.get(fieldname)
	if str(before or "") == str(value or ""):
		return
	changes[fieldname] = {"before": str(before or "") or None, "after": str(value or "") or None}
	doc.set(fieldname, value)

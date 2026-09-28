# SPDX-License-Identifier: MIT
"""Inspection templates and the sessions worked from them — the v0.21.0 tools.

ELEVEN TOOLS. Ten of them make templates authorable and sessions workable through
MCP. The eleventh, `propose_inspection_template_from_regulation`, was declared in
v0.21.0 and refused — the surface an AI proposer would occupy, reserved before
anything occupied it — and v0.37.0 filled it, along with the
`approve_inspection_template` it needed: a template is live the instant it is
written, so a draft nobody approved has to land INACTIVE, and inactive is only
useful if there is a way to turn it on with somebody's name against it.

WHAT `submit_inspection_session` ACTUALLY DOES, IN ORDER, BECAUSE IT IS THE ONE
TOOL HERE WITH TEETH:

  1. Reads the sections off THE VERSION THE SESSION PINNED, not off whatever the
     template says now.
  2. Refuses a submission naming a section the pinned version does not have.
  3. Refuses a submission short of any REQUIRED section, listing them. That is
     the evidence contract, one layer above Farm Task's, and it is the reason
     the whole framework can be trusted to have produced complete records.
  4. Checks each submitted section against its own contract and refuses the ones
     that are short, naming what is missing.
  5. Groups the submitted sections by (produced doctype, subject) and creates ONE
     compliance record per group — see `sessions.merge_key` for why two sections
     that produce a Detector Test for one cabin on one day must not produce two
     Detector Tests.
  6. Writes the produced record's docname back onto every section submission in
     the group, files the evidence, moves the session to Submitted.

NOTHING IS WRITTEN IF ANY OF 2, 3 OR 4 REFUSES. The dispatcher rolls back on a
raised ToolError, so a submission that is short of one required section leaves no
half-filed Housing Inspection behind — which matters more here than on a single
completion, because half a visit is a set of compliance records that look
complete and are not.

WHAT IT DOES NOT REFUSE. A section whose findings are alarming; a visit that took
four hours; a session submitted weeks after it was started. Every one of those is
a fact worth recording, and a tool that refused to record it would guarantee the
record stayed empty. A produced record that found something routes itself to
Corrective Action Required in its own controller and raises its own Critical
alert, exactly as it would have from a single-task completion.
"""

from __future__ import annotations

import json

import frappe

from .. import compat, form_schema, proposals, rodent_bait, sessions
from .. import training as regimes_vocabulary
from ..args import as_bool, as_date, as_int, as_limit, as_str, as_visit_id, resolve_company
from ..errors import ToolError
from ..result import ToolResult
from . import inspections

TEMPLATE = sessions.TEMPLATE_DOCTYPE
SESSION = sessions.SESSION_DOCTYPE

SESSION_FIELDS = (
	"name",
	"template",
	"template_version",
	"state",
	"company",
	"location",
	"location_doctype",
	"farm_location_gps",
	"worker",
	"worker_name",
	"foreman",
	"foreman_name",
	"visit_id",
	"farm_task",
	"source_alerts",
	"started_at",
	"submitted_at",
	"notes",
	"creation",
	"owner",
)

SUBMISSION_FIELDS = (
	"name",
	"section_name",
	"submitted_at",
	"skipped",
	"produces_record_doctype",
	"produced_record_link",
	"notes",
	"checklist_values_json",
	"measurements_json",
	"evidence_files_json",
	"idx",
)


# ── shared ──────────────────────────────────────────────────────────────────
def _require(doctype: str = TEMPLATE) -> None:
	compat.require_doctype(
		doctype,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)


def _template_or_refuse(reference: str, label: str = "template") -> str:
	_require()
	name = sessions.resolve_template(reference)
	if not name:
		raise ToolError(
			f"no Inspection Template called {reference!r} on this site — neither as a docname nor "
			"as the name of a live template. list_inspection_templates has the register. Nothing "
			"was created."
		)
	return name


def _describe_template(name: str, with_sections: bool = False) -> dict:
	row = sessions.template_row(name)
	if not row:
		raise ToolError(f"no Inspection Template called {name!r} on this site.")
	out = {
		"name": row.get("name"),
		"template_name": row.get("template_name"),
		"description": row.get("description"),
		"applies_to_asset_type": row.get("applies_to_asset_type"),
		"skill_required": row.get("skill_required") or None,
		"estimated_duration_minutes": int(row.get("estimated_duration_minutes") or 0) or None,
		"cadence_trigger_expression": row.get("cadence_trigger_expression") or None,
		"regulation_citations": row.get("regulation_citations") or None,
		"regimes": sessions.regimes_of(name),
		"active": compat.checked(row.get("active")),
		"version": int(row.get("version") or 1),
		"superseded_by": row.get("superseded_by") or None,
		"created": str(row.get("creation") or "") or None,
		# v0.37.0. Who wrote this form, where they read it, and who let it reach a
		# handset. On every template rather than only on the proposed ones, because
		# "AI-proposed" only means anything if the other two words are also said.
		"authored_by": row.get("authored_by") or proposals.AUTHOR_OPERATOR,
		"ai_source_citation": row.get("ai_source_citation") or None,
		"ai_review_flags": proposals.read_flags(row.get("ai_review_flags")),
		"human_approved_by": row.get("human_approved_by") or None,
		"human_approved_on": str(row.get("human_approved_on") or "") or None,
	}
	section_rows = sessions.sections_of(name)
	out["section_count"] = len(section_rows)
	out["produces"] = sorted(
		{
			str(section.get("produces_record_doctype") or "").strip()
			for section in section_rows
			if str(section.get("produces_record_doctype") or "").strip()
		}
	)
	if with_sections:
		out["sections"] = [sessions.describe_section(section) for section in section_rows]
	if not out["active"]:
		out["note"] = (
			"This template is not active, so no new session starts from it. Every session already "
			"worked from it stays readable and every compliance record it produced stays in the "
			"register and in the audit packet — that is what deactivating is for."
		)
	return out


def _session_row(name: str) -> dict:
	name = str(name or "").strip()
	if not name:
		raise ToolError("name is required (an Inspection Session docname).")
	compat.require_doctype(SESSION, "It ships with erpnext_mcp — run `bench migrate`.")
	if not frappe.db.exists(SESSION, name):
		raise ToolError(
			f"no Inspection Session called {name!r} on this site. list_inspection_sessions has them."
		)
	row = frappe.db.get_value(SESSION, name, compat.existing_fields(SESSION, SESSION_FIELDS), as_dict=True)
	return dict(row or {})


def _describe_session(row: dict, full: bool = False) -> dict:
	out = {
		"name": row.get("name"),
		"template": row.get("template"),
		"template_version": int(row.get("template_version") or 0) or None,
		"state": row.get("state"),
		"company": row.get("company"),
		"location": row.get("location"),
		"location_doctype": row.get("location_doctype"),
		"farm_location_gps": row.get("farm_location_gps") or None,
		"worker": row.get("worker") or None,
		"worker_name": row.get("worker_name") or None,
		"foreman": row.get("foreman") or None,
		"foreman_name": row.get("foreman_name") or None,
		"visit_id": row.get("visit_id") or None,
		"farm_task": row.get("farm_task") or None,
		"source_alerts": sessions.alert_names(row.get("source_alerts")),
		"started_at": str(row.get("started_at") or "") or None,
		"submitted_at": str(row.get("submitted_at") or "") or None,
		"notes": row.get("notes") or None,
	}
	submissions = _submissions_of(row.get("name"))
	out["sections_submitted"] = len(submissions)
	# BY RECORD AND NOT BY SUBMISSION. Two sections that produced ONE Detector
	# Test are one Detector Test, and counting them twice would tell a reader —
	# or an audit packet built from this — that there is a record they cannot
	# find. The sections that produced each one are named beside it instead.
	records = {}
	for entry in submissions:
		link = entry.get("produced_record_link")
		if not link:
			continue
		found = records.setdefault(
			link, {"doctype": entry["produces_record_doctype"], "record": link, "sections": []}
		)
		found["sections"].append(entry["section_name"])
	out["produced_records"] = list(records.values())
	if full:
		out["section_submissions"] = submissions
		out["evidence"] = _evidence_of(row.get("name"))
		out["template_detail"] = _describe_template(row.get("template"), with_sections=True)
	return out


def _submissions_of(name: str) -> list:
	if not (name and compat.doctype_exists(sessions.SUBMISSION_DOCTYPE)):
		return []
	rows = frappe.db.get_all(
		sessions.SUBMISSION_DOCTYPE,
		filters={"parent": name, "parenttype": SESSION, "parentfield": "section_submissions"},
		fields=compat.existing_fields(sessions.SUBMISSION_DOCTYPE, SUBMISSION_FIELDS),
		order_by="idx asc",
		limit=sessions.SECTION_CAP * 2,
	)
	out = []
	for row in rows or []:
		out.append(
			{
				"section_name": row.get("section_name"),
				"submitted_at": str(row.get("submitted_at") or "") or None,
				"skipped": compat.checked(row.get("skipped")),
				"produces_record_doctype": row.get("produces_record_doctype") or None,
				"produced_record_link": row.get("produced_record_link") or None,
				"notes": row.get("notes"),
				"checklist_values": _quietly(row.get("checklist_values_json")),
				"measurements": _quietly(row.get("measurements_json")),
				"evidence_files": _quietly_list(row.get("evidence_files_json")),
			}
		)
	return out


def _evidence_of(name: str) -> list:
	if not (name and compat.doctype_exists(sessions.EVIDENCE_DOCTYPE)):
		return []
	rows = frappe.db.get_all(
		sessions.EVIDENCE_DOCTYPE,
		filters={"parent": name, "parenttype": SESSION, "parentfield": "evidence_files"},
		fields=["evidence_type", "file", "file_url", "section_name", "caption", "captured_on", "idx"],
		order_by="idx asc",
		limit=sessions.EVIDENCE_CAP * 2,
	)
	return [
		{
			"evidence_type": row.get("evidence_type"),
			"file": row.get("file"),
			"file_url": row.get("file_url"),
			"section_name": row.get("section_name") or None,
			"caption": row.get("caption"),
			"captured_on": str(row.get("captured_on") or "") or None,
		}
		for row in rows or []
	]


def _quietly(raw) -> dict:
	"""A stored JSON object, or {}. A READ path: one malformed blob must not take
	a whole audit packet down."""
	try:
		return sessions.as_object(raw, "value")
	except ValueError:
		return {}


def _quietly_list(raw) -> list:
	try:
		return sessions.as_list(raw, "value")
	except ValueError:
		return []


# ── template reads ──────────────────────────────────────────────────────────
def list_inspection_templates(args: dict) -> ToolResult:
	"""Every template, with what each one produces and which version is live."""
	_require()
	asset_type = as_str(args, "applies_to_asset_type")
	regime = as_str(args, "regime")
	active = as_bool(args, "active", None)
	limit = min(as_limit(args), sessions.SESSION_CAP)

	filters = {}
	if asset_type:
		filters["applies_to_asset_type"] = asset_type
	if active is not None:
		filters["active"] = 1 if active else 0
	rows = frappe.db.get_all(
		TEMPLATE,
		filters=filters,
		fields=["name"],
		order_by="template_name asc, version asc",
		limit=limit,
	)
	described = [_describe_template(str(row["name"])) for row in rows or []]
	if regime:
		# By TOKEN, never substring — "GlobalGAP" contains "GAP", and a LIKE would
		# put every GLOBALG.A.P. template in a USDA GAP answer.
		try:
			regimes_vocabulary.require([regime], "regime")
		except ValueError as exc:
			raise ToolError(str(exc)) from None
		described = [entry for entry in described if regimes_vocabulary.matches(entry["regimes"], regime)]

	live = [entry["name"] for entry in described if entry["active"] and not entry["superseded_by"]]
	data = {
		"count": len(described),
		"limit": limit,
		"live_templates": live,
		"templates": described,
		"note": (
			"A template that is superseded or inactive is still listed, because the sessions "
			"worked from it are still readable and an auditor asking 'what did the close-down "
			"look like last October' is asking about one of those. `live_templates` is the set a "
			"new session can start from."
		),
	}
	return ToolResult(
		data=data,
		summary=f"{len(described)} inspection template(s); {len(live)} live",
	)


def get_inspection_template(args: dict) -> ToolResult:
	"""One template in full: every section, its contract, its renderer, what it produces."""
	name = _template_or_refuse(as_str(args, "name") or as_str(args, "template", required=True), "name")
	described = _describe_template(name, with_sections=True)
	return ToolResult(
		data=described,
		summary=(
			f"{described['template_name']} v{described['version']} — {described['section_count']} "
			f"section(s), produces {', '.join(described['produces']) or 'no standalone record'}"
		),
	)


# ── template writes ─────────────────────────────────────────────────────────
def create_inspection_template(args: dict) -> ToolResult:
	"""Author one template. It is live on the next fetch — no release, no build."""
	_require()
	spec = _template_spec_from_args(args, required=True)
	doc = sessions.build_template(spec)
	doc.insert(ignore_permissions=True)
	described = _describe_template(doc.name, with_sections=True)
	return ToolResult(
		data={
			**described,
			"note": (
				"This template is live. It reaches the handset on the next fetch and the rule "
				"engine can match it on the next sweep — no app release and no DocType edit was "
				"involved, which is the whole of the templates-as-data claim."
			),
		},
		summary=(
			f"created inspection template {doc.name} ({doc.template_name} v1, "
			f"{len(spec['sections'])} section(s))"
		),
		docstatus_delta="none → 0 (created)",
	)


def update_inspection_template(args: dict) -> ToolResult:
	"""Supersede a template with a new version. The old one stays readable."""
	name = _template_or_refuse(as_str(args, "name") or as_str(args, "template", required=True), "name")
	old = sessions.template_row(name)
	if str(old.get("superseded_by") or "").strip():
		raise ToolError(
			f"Inspection Template {name} was already superseded by {old['superseded_by']}. Edit "
			"that one — or pass the template's NAME rather than its docname, which always "
			"resolves to the live version. Nothing was written."
		)

	current = _describe_template(name, with_sections=True)
	spec = _template_spec_from_args(args, required=False, current=current)
	spec["version"] = int(old.get("version") or 1) + 1
	spec["active"] = 1

	# The old row is stood down FIRST, so the "one live template per name" rule
	# in the controller sees a clear field when the new row is inserted. If the
	# insert then fails, the dispatcher's rollback puts the old row back exactly
	# as it was — there is no ordering here that can leave a name with no live
	# template.
	frappe.db.set_value(TEMPLATE, name, "active", 0, update_modified=False)
	doc = sessions.build_template(spec)
	try:
		doc.insert(ignore_permissions=True)
	except Exception:
		frappe.db.set_value(TEMPLATE, name, "active", int(old.get("active") or 0), update_modified=False)
		raise
	frappe.db.set_value(TEMPLATE, name, "superseded_by", doc.name, update_modified=False)

	described = _describe_template(doc.name, with_sections=True)
	return ToolResult(
		data={
			**described,
			"supersedes": name,
			"previous_version": int(old.get("version") or 1),
			"note": (
				f"{name} was NOT edited. It is still on this site in full, deactivated and pointing "
				f"at {doc.name} — so every session worked from it can still be read against the "
				"sections the worker actually saw. That is the whole reason templates are versioned "
				"by copy rather than in place, and it is why a session started against the old "
				"version while this call was running is unaffected."
			),
		},
		summary=(
			f"superseded {name} (v{old.get('version') or 1}) with {doc.name} "
			f"(v{spec['version']}, {len(spec['sections'])} section(s))"
		),
		docstatus_delta="none → 0 (created)",
	)


def deactivate_inspection_template(args: dict) -> ToolResult:
	"""Stop new sessions starting from a template. Nothing already worked is touched."""
	name = _template_or_refuse(as_str(args, "name") or as_str(args, "template", required=True), "name")
	reason = as_str(args, "reason", required=True)
	if len(reason) < 10:
		raise ToolError(
			"reason must say something. A template withdrawn with no sentence beside it is a "
			"change nobody can explain to the operator who asks next season why the close-down "
			"form vanished. Nothing was written."
		)
	row = sessions.template_row(name)
	if not compat.checked(row.get("active")):
		raise ToolError(f"Inspection Template {name} is already inactive. Nothing was written.")

	worked = frappe.db.count(SESSION, {"template": name}) if compat.doctype_exists(SESSION) else 0
	frappe.db.set_value(TEMPLATE, name, "active", 0)
	note = str(row.get("description") or "")
	frappe.db.set_value(
		TEMPLATE,
		name,
		"description",
		(note + f"\n\nDeactivated {frappe.utils.today()}: {reason}").strip()[:1000],
		update_modified=False,
	)
	return ToolResult(
		data={
			**_describe_template(name),
			"reason": reason,
			"sessions_already_worked": worked,
			"note": (
				f"{worked} session(s) were worked from this template and every one of them is "
				"still readable, with its pinned version and its sections as they were. Every "
				"compliance record they produced is still in the register, still dismissing the "
				"alerts it dismissed, and still in the audit packet. Deactivating hides a template "
				"from new work; it destroys nothing."
			),
		},
		summary=f"deactivated inspection template {name} ({row.get('template_name')})",
		docstatus_delta="0 → 0 (updated)",
	)


def _template_spec_from_args(args: dict, required: bool, current: dict | None = None) -> dict:
	"""The plain dict `sessions.build_template` takes, from tool arguments.

	`current` is the existing template on an update, so an argument left out
	means "unchanged" rather than "cleared" — which is what somebody correcting
	one section of a six-section close-down means, and the opposite of what they
	would get from a create-shaped reader.
	"""
	current = current or {}
	spec = {
		"template_name": as_str(args, "template_name", required=required)
		or current.get("template_name")
		or "",
		"description": as_str(args, "description", required=required) or current.get("description") or "",
		"applies_to_asset_type": as_str(args, "applies_to_asset_type")
		or current.get("applies_to_asset_type")
		or "General",
		"skill_required": (
			as_str(args, "skill_required") if "skill_required" in args else current.get("skill_required")
		)
		or "",
		# v0.205.0. docs/design/programs_and_field_kinds.md A1.
		"required_certification": (
			as_str(args, "required_certification")
			if "required_certification" in args
			else current.get("required_certification")
		)
		or "",
		"estimated_duration_minutes": (
			as_int(args, "estimated_duration_minutes")
			if "estimated_duration_minutes" in args
			else current.get("estimated_duration_minutes")
		)
		or 0,
		"cadence_trigger_expression": (
			as_str(args, "cadence_trigger_expression")
			if "cadence_trigger_expression" in args
			else current.get("cadence_trigger_expression")
		)
		or "",
		"regulation_citations": (
			as_str(args, "regulation_citations")
			if "regulation_citations" in args
			else current.get("regulation_citations")
		)
		or "",
	}
	if spec["applies_to_asset_type"] not in sessions.ASSET_TYPES:
		raise ToolError(
			f"applies_to_asset_type must be one of: {', '.join(sessions.ASSET_TYPES)}. "
			f"Got {spec['applies_to_asset_type']!r}."
		)

	if "regimes" in args:
		try:
			spec["regimes"] = regimes_vocabulary.require(args.get("regimes"), "regimes")
		except ValueError as exc:
			raise ToolError(f"{exc} Nothing was written.") from None
	else:
		spec["regimes"] = current.get("regimes") or []

	# v0.37.0. PROVENANCE IS CARRIED, NEVER TAKEN FROM ARGUMENTS. An edit to a
	# template a model drafted is still a template a model drafted, and the review
	# flags go with it — otherwise superseding a flagged draft would be a way to
	# launder it clean, which is a hole shaped exactly like the gate it bypasses.
	# `propose_inspection_template_from_regulation` sets these AFTER this reader,
	# which is the one door they are written through.
	if current:
		spec["authored_by"] = current.get("authored_by") or proposals.AUTHOR_OPERATOR
		spec["ai_source_citation"] = current.get("ai_source_citation") or ""
		spec["ai_review_flags"] = proposals.dump_flags(current.get("ai_review_flags"))
		spec["human_approved_by"] = current.get("human_approved_by") or None
		spec["human_approved_on"] = current.get("human_approved_on") or None

	if "sections" in args:
		spec["sections"] = _sections_from_args(args.get("sections"))
	elif current.get("sections"):
		spec["sections"] = [_section_from_current(entry) for entry in current["sections"]]
	else:
		raise ToolError(
			"sections is required. A template with no sections is a name — the whole content of "
			"one is what a worker is asked to do."
		)
	return spec


def _sections_from_args(raw) -> list:
	if not isinstance(raw, list) or not raw:
		raise ToolError(
			"sections must be a non-empty list of objects like "
			'{"section_name": "Habitability walk", "produces_record_doctype": "Housing Inspection", '
			'"renderer_hint": "multi-photo", "required": true, '
			'"evidence_contract": {"photos": true, "signature": true}}.'
		)
	if len(raw) > sessions.SECTION_CAP:
		raise ToolError(
			f"sections has {len(raw)} entries, past the {sessions.SECTION_CAP} cap. Nothing was written."
		)
	out = []
	for index, entry in enumerate(raw):
		if not isinstance(entry, dict):
			raise ToolError(f"sections[{index}] must be an object, got {type(entry).__name__}.")
		name = str(entry.get("section_name") or "").strip()
		if not name:
			raise ToolError(f"sections[{index}] has no section_name.")
		hint = str(entry.get("renderer_hint") or "checklist").strip()
		if hint not in sessions.RENDERER_HINTS:
			raise ToolError(
				f"sections[{index}].renderer_hint must be one of: "
				f"{', '.join(sessions.RENDERER_HINTS)}. Got {hint!r}."
			)
		try:
			contract = sessions.check_contract(
				sessions.as_object(
					entry.get("evidence_contract", entry.get("evidence_contract_json")),
					f"sections[{index}].evidence_contract",
				),
				f"sections[{index}].evidence_contract",
			)
			defaults = sessions.as_object(
				entry.get("produces_record_data", entry.get("produces_record_data_json")),
				f"sections[{index}].produces_record_data",
			)
			prompts = sessions.as_prompts(
				entry.get("field_prompts", entry.get("field_prompts_json")),
				f"sections[{index}].field_prompts",
			)
		except ValueError as exc:
			raise ToolError(str(exc)) from exc
		out.append(
			{
				"section_name": name,
				"section_description": str(entry.get("section_description") or "").strip(),
				"order_index": entry.get("order_index"),
				"produces_record_doctype": str(entry.get("produces_record_doctype") or "").strip(),
				"renderer_hint": hint,
				"required": entry.get("required", True),
				"evidence_contract": contract,
				"produces_record_data": defaults,
				"field_prompts": prompts,
			}
		)
	return out


def _section_from_current(entry: dict) -> dict:
	return {
		"section_name": entry.get("section_name"),
		"section_description": entry.get("section_description") or "",
		"order_index": entry.get("order_index"),
		"produces_record_doctype": entry.get("produces_record_doctype") or "",
		"renderer_hint": entry.get("renderer_hint") or "checklist",
		"required": entry.get("required", True),
		"evidence_contract": entry.get("evidence_contract") or {},
		"produces_record_data": entry.get("produces_record_data") or {},
		"field_prompts": entry.get("field_prompts") or {},
	}


# ── sessions ────────────────────────────────────────────────────────────────
def start_inspection_session(args: dict) -> ToolResult:
	"""Open one visit against one template at one place. Writes no compliance record."""
	compat.require_doctype(SESSION, "It ships with erpnext_mcp — run `bench migrate`.")
	template = _template_or_refuse(as_str(args, "template", required=True))
	location = as_str(args, "location", required=True)

	doc = frappe.new_doc(SESSION)
	doc.template = template
	doc.location = location
	doc.location_doctype = as_str(args, "location_doctype")
	doc.farm_location_gps = as_str(args, "farm_location_gps")
	doc.worker = _employee(as_str(args, "worker"), "worker")
	doc.foreman = _employee(as_str(args, "foreman"), "foreman")
	# v0.205.0. A template's required certification, enforced on the named worker
	# exactly as on a task (docs/design/programs_and_field_kinds.md A1).
	_refuse_unqualified_worker(template, doc.worker, "started")
	doc.company = resolve_company(as_str(args, "company"), required=False)
	doc.visit_id = as_visit_id(args)
	doc.notes = as_str(args, "notes")
	doc.state = sessions.STATE_IN_PROGRESS if as_bool(args, "in_progress", True) else sessions.STATE_DRAFT
	farm_task = as_str(args, "farm_task")
	if farm_task:
		if not frappe.db.exists("Farm Task", farm_task):
			raise ToolError(f"no Farm Task called {farm_task!r} on this site. Nothing was created.")
		doc.farm_task = farm_task
	doc.insert(ignore_permissions=True)
	if farm_task:
		frappe.db.set_value("Farm Task", farm_task, "inspection_session", doc.name)

	described = _describe_session(dict(doc.as_dict()), full=True)
	return ToolResult(
		data={
			**described,
			"note": (
				f"Template version {doc.template_version} is pinned to this session. A template "
				"edited between now and submission does not change what this visit is read to have "
				"been — the version it was worked from is a different document and is never "
				"touched. Nothing has been written to any register yet: the compliance records are "
				"created by submit_inspection_session and not before."
			),
		},
		summary=(
			f"started inspection session {doc.name} — {described['template_detail']['template_name']} "
			f"v{doc.template_version} at {doc.location}"
		),
		docstatus_delta="none → 0 (created)",
	)


def _refuse_unqualified_worker(template: str, worker, verb: str) -> None:
	from .. import qualifications

	if not worker or not compat.has_field(sessions.TEMPLATE_DOCTYPE, "required_certification"):
		return
	requirement = str(
		frappe.db.get_value(sessions.TEMPLATE_DOCTYPE, template, "required_certification") or ""
	)
	qualifications.refuse_unqualified(
		{"required_certification": requirement}, str(worker), verb, what=f"Inspection template {template}"
	)


def _employee(value: str, label: str) -> str | None:
	from .housing import EMPLOYEE, hr_installed

	if not value:
		return None
	if hr_installed() and not frappe.db.exists(EMPLOYEE, value):
		raise ToolError(
			f"no Employee {value!r} on this site ({label}). A compliance record naming somebody "
			"payroll has never heard of has already drifted from the operation it describes. "
			"Nothing was created."
		)
	return value


def list_inspection_sessions(args: dict) -> ToolResult:
	"""Every visit, newest first, with what each one produced."""
	compat.require_doctype(SESSION, "It ships with erpnext_mcp — run `bench migrate`.")
	limit = min(as_limit(args), sessions.SESSION_CAP)
	filters = {}
	company = resolve_company(as_str(args, "company"), required=False)
	if company:
		filters["company"] = company
	for key, field in (
		("location", "location"),
		("worker", "worker"),
		("template", "template"),
		("visit_id", "visit_id"),
	):
		value = as_str(args, key)
		if value:
			filters[field] = value
	state = as_str(args, "state")
	if state:
		if state not in sessions.SESSION_STATES:
			raise ToolError(f"state must be one of: {', '.join(sessions.SESSION_STATES)}. Got {state!r}.")
		filters["state"] = state
	from_date = as_date(args, "from_date")
	to_date = as_date(args, "to_date")
	if from_date and to_date:
		filters["started_at"] = ("between", [from_date, f"{to_date} 23:59:59"])
	elif from_date:
		filters["started_at"] = (">=", from_date)
	elif to_date:
		filters["started_at"] = ("<=", f"{to_date} 23:59:59")

	rows = frappe.db.get_all(
		SESSION,
		filters=filters,
		fields=compat.existing_fields(SESSION, SESSION_FIELDS),
		order_by="started_at desc, name desc",
		limit=limit,
	)
	described = [_describe_session(dict(row)) for row in rows or []]
	produced = sum(len(entry["produced_records"]) for entry in described)
	open_sessions = [entry["name"] for entry in described if entry["state"] in sessions.OPEN_SESSION_STATES]
	return ToolResult(
		data={
			"company": company,
			"count": len(described),
			"limit": limit,
			"truncated": len(described) >= limit,
			"compliance_records_produced": produced,
			"open_sessions": open_sessions,
			"sessions": described,
			"note": (
				f"{len(open_sessions)} session(s) are still open — started and not submitted, so "
				"they have written nothing to any register. A visit somebody is part-way through "
				"and one they abandoned look the same from here; `started_at` is what tells them "
				"apart."
				if open_sessions
				else "Every session in this selection has been submitted."
			),
		},
		summary=(
			f"{len(described)} inspection session(s); {produced} compliance record(s) produced, "
			f"{len(open_sessions)} still open"
		),
	)


def get_inspection_session(args: dict) -> ToolResult:
	"""One visit in full: the pinned template, every section submitted, the tray, the records."""
	row = _session_row(as_str(args, "name") or as_str(args, "session", required=True))
	described = _describe_session(row, full=True)
	return ToolResult(
		data=described,
		summary=(
			f"inspection session {row.get('name')} at {row.get('location')} — {row.get('state')}, "
			f"{len(described['produced_records'])} compliance record(s)"
		),
	)


# ── the one with teeth ──────────────────────────────────────────────────────
def preview_inspection_template(args: dict) -> ToolResult:
	"""What the phone renders for each section of a template, and what it cannot. v0.204.0 §6."""
	reference = as_str(args, "template") or as_str(args, "name", required=True)
	name = sessions.resolve_template(reference) if hasattr(sessions, "resolve_template") else reference
	if not name or not frappe.db.exists(sessions.TEMPLATE_DOCTYPE, name):
		raise ToolError(f"no Inspection Template called {reference!r} on this site.")
	language = (as_str(args, "language") or "en").lower()[:2]
	out = []
	problems = []
	for section in sessions.sections_of(name):
		described = sessions.describe_section(section)
		fields = sessions.section_fields(described)
		report = form_schema.validate(fields)
		for finding in report["errors"] + report["warnings"]:
			problems.append(f"{described['section_name']} › {finding['path']}: {finding['message']}")
		from .. import device_capabilities

		problems.extend(
			f"{described['section_name']} › {line}" for line in device_capabilities.problems(fields)
		)
		out.append(
			{
				"section_name": described["section_name"],
				"required": described["required"],
				"renderer_hint": described["renderer_hint"],
				"evidence_contract": described["evidence_contract"],
				"form": fields,
				"rendered": form_schema.resolve_language(fields, language),
				"form_is_legacy_prompts": not isinstance(described.get("field_prompts"), list),
			}
		)
	return ToolResult(
		data={"template": name, "language": language, "sections": out, "problems": problems},
		summary=f"preview of {name}: {len(out)} section(s), {len(problems)} problem(s)",
	)


def submit_inspection_session(args: dict) -> ToolResult:
	"""File every section, and write the compliance records the sections promise."""
	row = _session_row(as_str(args, "name") or as_str(args, "session", required=True))
	if row.get("state") in (sessions.STATE_SUBMITTED, sessions.STATE_REVIEWED):
		raise ToolError(
			f"Inspection Session {row['name']} was already submitted on {row.get('submitted_at')} "
			f"and produced its compliance records then. Correct one of those records with "
			"update_housing_inspection / update_detector_test / update_water_test — a second "
			"submission would file a second account of one afternoon. Nothing was written."
		)
	if row.get("state") == sessions.STATE_SUPERSEDED:
		raise ToolError(f"Inspection Session {row['name']} was superseded. Nothing was written.")

	template_sections = {
		str(section.get("section_name") or ""): sessions.describe_section(section)
		for section in sessions.sections_of(row["template"])
	}
	if not template_sections:
		raise ToolError(
			f"Inspection Template {row['template']} has no sections on this site. Nothing was written."
		)

	args["_session_context"] = {
		"location_doctype": row.get("location_doctype") or "",
		"template": row.get("template") or "",
	}
	submitted = _submitted_sections(args, template_sections)
	_refuse_missing_required(template_sections, submitted)
	_refuse_incomplete(template_sections, submitted)

	doc = frappe.get_doc(SESSION, row["name"])
	# WHO DID IT IS KNOWN AT SUBMISSION AND NOT BEFORE. A session the rule engine
	# raised was created before anybody had claimed the task, so it names nobody;
	# the handset filing it knows. These only ever FILL A BLANK — a session that
	# already names a worker is not re-attributed by whoever pressed submit,
	# because reassigning a completion to somebody else is the one thing a chain
	# of custody must not let a client do quietly.
	for key, value in (
		("worker", _employee(as_str(args, "worker"), "worker")),
		("foreman", _employee(as_str(args, "foreman"), "foreman")),
		("visit_id", as_visit_id(args)),
	):
		if value and not doc.get(key):
			doc.set(key, value)
	if doc.get("worker") and not doc.get("worker_name") and compat.doctype_exists("Employee"):
		doc.worker_name = str(frappe.db.get_value("Employee", doc.worker, "employee_name") or "")
	row.update({key: doc.get(key) for key in ("worker", "worker_name", "foreman", "visit_id")})
	_refuse_unqualified_worker(row["template"], doc.get("worker"), "submitted")

	now = frappe.utils.now()
	# v0.203.0. "Bait cleared / no rodent signs" is the server's to judge: it
	# passes only when every interior bait round here ended in a COMPLETED Removal
	# and Clearance (docs/design/rodent_bait_program.md §4.3). Before the records
	# are written, so a failure files Corrective Action Required.
	bait_clearance = rodent_bait.review_clearance(row, submitted, template_sections)
	produced = _write_the_records(doc, row, template_sections, submitted, args)

	doc.set("section_submissions", [])
	for name, entry in submitted.items():
		section = template_sections[name]
		doc.append(
			"section_submissions",
			{
				"section_name": name,
				"submitted_at": entry.get("submitted_at") or now,
				"skipped": 1 if entry.get("skipped") else 0,
				"produces_record_doctype": section["produces_record_doctype"],
				"produced_record_link": entry.get("produced_record_link"),
				"notes": entry.get("notes"),
				"checklist_values_json": json.dumps(entry.get("checklist_items") or {}),
				"measurements_json": json.dumps(entry.get("measurements") or {}),
				"evidence_files_json": json.dumps(entry.get("file_names") or []),
			},
		)
	_file_the_tray(doc, submitted)
	doc.state = sessions.STATE_SUBMITTED
	doc.submitted_at = now
	doc.save(ignore_permissions=True)

	described = _describe_session(dict(doc.as_dict()), full=True)
	skipped = [name for name, entry in submitted.items() if entry.get("skipped")]
	compliance_eval = _evaluate_compliance_after(produced, str(doc.get("company") or ""))
	# v0.203.0. "Rodent activity seen?" answered yes raises a placement task (§4.2).
	bait_tasks = rodent_bait.activity_tasks(row, submitted)
	data = {
		**described,
		"produced": produced,
		"skipped_sections": skipped,
		# v0.64.1. ALWAYS PRESENT, null where nothing this session produced could
		# have changed a rule's answer. See `_evaluate_compliance_after`.
		"compliance_evaluation": compliance_eval,
		"note": (
			f"{len(produced)} compliance record(s) were created — separately, at their own "
			"cadences, exactly as they would have been from three separate visits. That is the "
			"whole point: the worker made one trip and the register got the records each regulator "
			"asks for on its own schedule. Every one carries the evidence from this session's "
			"shared tray, so the photograph on the Housing Inspection and the photograph on the "
			"Detector Test are the same photograph rather than two of the same wall."
		),
	}
	if bait_clearance:
		data["bait_clearance"] = bait_clearance
	if bait_tasks:
		data["bait_tasks"] = bait_tasks
	if skipped:
		data["skipped_note"] = (
			f"{len(skipped)} optional section(s) were marked not applicable and produced nothing. "
			"That is a statement somebody made and it is on the record as one — an empty space "
			"would not be."
		)
	found = [entry for entry in produced if entry.get("found_something")]
	if found:
		data["findings_note"] = (
			f"{len(found)} of the records found something and routed themselves to Corrective "
			"Action Required, raising their own Critical alerts. The work IS done and the "
			"registers DID move — what needs a person is the finding."
		)
	return ToolResult(
		data=data,
		summary=(
			f"submitted inspection session {doc.name} — {len(submitted)} section(s), "
			f"{len(produced)} compliance record(s)"
		),
		docstatus_delta="0 → 0 (updated)",
	)


def _evaluate_compliance_after(produced: list, company: str) -> dict | None:
	"""Re-run the rules the records this session wrote could have made untrue.

	v0.64.1, and it is `complete_farm_task`'s v0.64.0 behaviour reaching the OTHER
	door that writes compliance registers. A session is the multi-section version
	of the same afternoon: one walk round a cabin that files a Housing Inspection
	and a Detector Test at their own cadences. Both of those registers are exactly
	what `housing_inspection_overdue` and `housing_detector_test_stale` are
	discharged through, and until now a worker who did the whole round through a
	session watched both alerts sit there until the hourly sweep — while the same
	work done as two Farm Tasks cleared them on the spot. One farm, one afternoon,
	two different answers depending on which screen it was filed from.

	NARROWED BY WHAT WAS ACTUALLY PRODUCED, through the same backwards read of
	`ALERT_TASK_MAP` the task path uses — so the two doors cannot come to disagree
	about which rule a Housing Inspection answers.

	IT IS THE SWEEP, CALLED SOONER. A session whose record found something routes
	that record to Corrective Action Required and raises its own Critical alert;
	the rules re-run here read the register and decide for themselves, so the walk
	that found a fault clears the "nobody has walked this" alert and leaves the
	new finding standing. Both facts are true and both survive.

	NEVER RAISES. Every record is written and every section is filed by the time
	this runs.
	"""
	from .dispatch import _rules_reading_register

	names = sorted(
		{
			alert_type
			for entry in produced or []
			for alert_type in _rules_reading_register(str((entry or {}).get("doctype") or ""))
		}
	)
	if not names:
		return None
	try:
		from ..alerts import base as alerts_base

		report = alerts_base.refresh_compliance_alerts(company=company or "", alert_types=names)
	except Exception as exc:
		return {
			"rules_asked": names,
			"evaluated": False,
			"why_not": (
				f"the narrowed sweep did not run: {type(exc).__name__}: {exc}. Every record this "
				"session produced is written and its evidence is filed, and the scheduled sweep "
				"will reach these rules on its next pass."
			),
		}
	dismissed = [
		entry["name"] for entry in report.get("alerts") or [] if entry.get("outcome") == "auto_dismissed"
	]
	out = {
		"rules_asked": names,
		"evaluated": True,
		"auto_dismissed": dismissed,
		"created": report.get("created", 0),
		"refreshed": report.get("refreshed", 0),
		"reopened": report.get("reopened", 0),
		"note": (
			"These are the rules whose register this session wrote to. THE SWEEP DECIDED, not the "
			"submission: an alert here went away because its rule looked again and found its "
			"condition no longer true."
		),
	}
	if not dismissed:
		out["standing_note"] = (
			"No alert dismissed. That is a normal and often correct outcome — the work is done and "
			"the condition that raised the alert may still be true."
		)
	return out


def _submitted_sections(args: dict, template_sections: dict) -> dict:
	"""The caller's `section_submissions`, normalised and checked against the pinned version."""
	raw = args.get("section_submissions")
	if not isinstance(raw, list) or not raw:
		raise ToolError(
			"section_submissions must be a non-empty list of objects like "
			'{"section_name": "Habitability walk", "evidence_file_tokens": ["<File docname>"], '
			'"checklist_values": {"beds_made": true}, "notes": ""}. Nothing was written.'
		)
	out = {}
	for index, entry in enumerate(raw):
		if not isinstance(entry, dict):
			raise ToolError(f"section_submissions[{index}] must be an object.")
		name = str(entry.get("section_name") or "").strip()
		if not name:
			raise ToolError(f"section_submissions[{index}] has no section_name.")
		if name not in template_sections:
			raise ToolError(
				f"section_submissions[{index}] names section {name!r}, which is not in the version "
				f"of this template the session was worked from. That version has: "
				f"{', '.join(sorted(template_sections))}. A section that arrived in a later version "
				"of the template is not a section this worker was ever shown. Nothing was written."
			)
		if name in out:
			raise ToolError(f"section {name!r} was submitted twice. Nothing was written.")

		tokens = entry.get("evidence_file_tokens", entry.get("evidence_files", entry.get("evidence")))
		evidence = inspections.normalise_evidence(tokens, f"section_submissions[{index}].evidence")
		signature = str(entry.get("signature_file") or "").strip()
		if signature:
			evidence = evidence + inspections.normalise_evidence(
				[{"file": signature, "evidence_type": "Signature"}]
				if frappe.db.exists("File", signature)
				else [{"file_url": signature, "evidence_type": "Signature"}],
				f"section_submissions[{index}].signature_file",
			)
		try:
			checklist = sessions.as_object(
				entry.get("checklist_values"), f"section_submissions[{index}].checklist_values"
			)
			measurements = sessions.as_object(
				entry.get("measurements"), f"section_submissions[{index}].measurements"
			)
			record_data = sessions.as_object(
				entry.get("record_data"), f"section_submissions[{index}].record_data"
			)
		except ValueError as exc:
			raise ToolError(str(exc)) from exc

		# v0.204.0. `answers` to a section whose prompts are form fields: validated
		# like a task's form, and each answer also counts as a checklist value, so
		# the evidence contract's `checklist_items` keys are met by them (§1.7).
		fields = sessions.section_fields(template_sections[name])
		if entry.get("answers") not in (None, "", {}) and fields and not entry.get("skipped"):
			report = form_schema.check_answers(
				fields, entry.get("answers"), dict(args.get("_session_context") or {})
			)
			if report["problems"]:
				raise ToolError(
					f"section {name!r} is not finished:\n"
					+ "\n".join(f"  - {problem}" for problem in report["problems"])
					+ "\nNothing was written."
				)
			checklist = {**checklist, **report["answers"]}

		out[name] = {
			"section_name": name,
			"skipped": bool(entry.get("skipped")),
			"evidence": evidence,
			"file_names": [str(item.get("file") or item.get("file_url") or "") for item in evidence if item],
			"photos": [item for item in evidence if item.get("evidence_type") == "Photo"],
			"signature": [item for item in evidence if item.get("evidence_type") == "Signature"],
			"checklist_items": checklist,
			"measurements": measurements,
			"record_data": record_data,
			"witness": str(entry.get("witness") or "").strip(),
			"notes": entry.get("notes"),
			"submitted_at": str(entry.get("submitted_at") or "").strip() or None,
		}
	return out


def _refuse_missing_required(template_sections: dict, submitted: dict) -> None:
	missing = [
		name
		for name, section in template_sections.items()
		if section["required"] and (name not in submitted or submitted[name]["skipped"])
	]
	if missing:
		raise ToolError(
			f"these required section(s) were not submitted: {', '.join(sorted(missing))}. A visit "
			"filed short of one is a set of compliance records that LOOK complete and are not, "
			"which is worse than no records at all — an auditor reading them has no way to know "
			"the detector was never tested. Nothing was written. Mark an optional section "
			'`{"skipped": true}` where it genuinely did not apply; a required one cannot be.'
		)


def _refuse_incomplete(template_sections: dict, submitted: dict) -> None:
	shortfalls = []
	for name, entry in submitted.items():
		if entry["skipped"]:
			continue
		gaps = sessions.unmet(template_sections[name]["evidence_contract"], entry)
		if gaps:
			shortfalls.append(f"{name} is missing {', '.join(gaps)}")
	if shortfalls:
		raise ToolError(
			"the submission does not meet the template's evidence contract: "
			+ "; ".join(sorted(shortfalls))
			+ ". The contract is what the template promised these records would carry, and a "
			"record filed without it is one an auditor is trained to disbelieve. Nothing was "
			"written."
		)


def _write_the_records(doc, row: dict, template_sections: dict, submitted: dict, args: dict) -> list:
	"""One compliance record per (doctype, subject) group. See sessions.merge_key."""
	groups = {}
	for name, entry in submitted.items():
		if entry["skipped"]:
			continue
		section = template_sections[name]
		doctype = section["produces_record_doctype"]
		if not doctype:
			continue
		builder = inspections.BUILDERS.get(doctype)
		if builder is None:
			raise ToolError(
				f"section {name!r} says it produces a {doctype}, and this app has no builder for "
				f"that doctype — it can create {', '.join(sorted(inspections.BUILDERS))}. Point the "
				"section at one of those, or leave produces_record_doctype empty so the section "
				"gathers evidence and produces no standalone record. Nothing was written."
			)
		spec = inspections.SPECS[doctype]
		subject = _subject_for(spec, section, entry, row, name)
		groups.setdefault(sessions.merge_key(doctype, subject), []).append((name, entry, section))

	produced = []
	for (doctype, subject), members in groups.items():
		spec = inspections.SPECS[doctype]
		payload = {
			spec.subject_field: subject,
			spec.date_field: as_date(args, "record_date") or str(frappe.utils.today()),
			spec.person_field: row.get("worker") or "",
			f"{spec.person_field}_name": row.get("worker_name") or row.get("worker") or "",
			"source_task": row.get("farm_task") or None,
		}
		findings = []
		evidence = []
		seen_files = set()
		signature = ""
		for name, entry, section in members:
			payload.update(section["produces_record_data"] or {})
			payload.update(entry["record_data"] or {})
			text = entry.get("notes")
			if text:
				findings.append(f"{name}: {text}")
			for item in entry["evidence"]:
				key = str(item.get("file") or item.get("file_url") or "")
				if key and key in seen_files:
					continue
				if key:
					seen_files.add(key)
				evidence.append(item)
				if item.get("evidence_type") == "Signature" and not signature:
					signature = str(item.get("file_url") or item.get("file") or "")
		payload["findings"] = "; ".join(findings)
		if signature and compat.has_field(doctype, "signature"):
			payload["signature"] = signature
		if compat.has_field(doctype, "farm_location_gps") and row.get("farm_location_gps"):
			payload.setdefault("farm_location_gps", row["farm_location_gps"])

		record = inspections.BUILDERS[doctype](payload, evidence)
		for _name, entry, _section in members:
			entry["produced_record_link"] = record.name
		produced.append(
			{
				"doctype": doctype,
				"record": record.name,
				"subject": subject,
				"sections": [name for name, _entry, _section in members],
				"state": str(record.workflow_state),
				"found_something": str(record.workflow_state) == "Corrective Action Required",
				"evidence_count": len(evidence),
			}
		)
	return produced


def _subject_for(spec, section: dict, entry: dict, row: dict, name: str) -> str:
	"""What the produced record is ABOUT, which is not always where the visit was.

	A Housing Inspection is about the cabin the session happened at, so the
	session's own location answers. A Water Test is about an Irrigation Zone, and
	a session at a cabin cannot say which — one cabin can draw from several
	sources, so this app refuses to guess and asks the section or the submission
	to name it.
	"""
	explicit = str(
		(entry["record_data"] or {}).get(spec.subject_field)
		or (section["produces_record_data"] or {}).get(spec.subject_field)
		or ""
	).strip()
	if explicit:
		return explicit
	if str(row.get("location_doctype") or "") == spec.subject_doctype:
		return str(row.get("location") or "")
	raise ToolError(
		f"section {name!r} produces a {spec.doctype}, which is about a {spec.subject_doctype}, and "
		f"this session happened at a {row.get('location_doctype') or 'place with no register named'}. "
		f'Name it in the section submission: {{"record_data": {{"{spec.subject_field}": '
		f'"<{spec.subject_doctype} docname>"}}}}. One cabin can draw from several water sources and '
		"this app will not guess which. Nothing was written."
	)


def _file_the_tray(doc, submitted: dict) -> None:
	"""Every distinct file from every section, once, tagged with the section it answered."""
	doc.set("evidence_files", [])
	seen = set()
	count = 0
	for name, entry in submitted.items():
		for item in entry["evidence"]:
			key = (str(item.get("file") or item.get("file_url") or ""), name)
			if not key[0] or key in seen:
				continue
			seen.add(key)
			count += 1
			if count > sessions.EVIDENCE_CAP:
				return
			doc.append(
				"evidence_files",
				{
					"evidence_type": item.get("evidence_type"),
					"file": item.get("file"),
					"file_url": item.get("file_url"),
					"section_name": name,
					"caption": item.get("caption"),
					"captured_on": item.get("captured_on"),
				},
			)


# ── v0.37.0: the surface Phase 2 reserved, filled ───────────────────────────
def propose_inspection_template_from_regulation(args: dict) -> ToolResult:
	"""Draft a template read off a regulation. It lands INACTIVE and marked AI-proposed.

	DECLARED IN v0.21.0, WIRED IN v0.37.0, AND IT CALLS NO MODEL. The AI doing
	the proposing IS THE CLIENT — a model that has read a regulation and drafted
	the sections it asks for, handing them over as arguments. This tool is the
	validator and the gate: it refuses the draft where it is the wrong shape,
	stamps `AI-proposed` and the source it was read from, lands it INACTIVE so no
	handset can fetch it, and flags what needs more than a skim. The rails, and
	the reasoning behind each of them, are in `erpnext_mcp/proposals.py`.

	A DRAFT FOR A NAME THAT IS ALREADY LIVE IS AN UPDATE PROPOSAL. It is written
	at version+1 and the live template is untouched — a worker starting a visit
	this afternoon gets the form somebody approved, not the one a model wrote an
	hour ago. `approve_inspection_template` is where the swap happens, by a
	person, with the supersession recorded.
	"""
	_require()

	offered = proposals.offered_approval_fields(args)
	if offered:
		raise ToolError(
			f"a proposal cannot fill in {', '.join(offered)}. Those fields ARE the approval, and the "
			"approval is the whole of what separates a form a model drafted from a form this "
			"operation asks a worker to fill in. approve_inspection_template is the one door they go "
			"through, and it records whoever is authenticated on that call. Nothing was written."
		)
	authored = as_str(args, "authored_by")
	if authored and authored != proposals.AUTHOR_AI:
		raise ToolError(
			f"propose_inspection_template_from_regulation writes authored_by={proposals.AUTHOR_AI!r} "
			f"and will not write {authored!r}. A proposal that could describe itself as "
			"operator-authored would make the provenance field mean nothing on exactly the rows it "
			"exists for. If a person authored this template, let them author it: "
			"create_inspection_template. Nothing was written."
		)

	try:
		citation = proposals.citation(
			url=as_str(args, "regulation_url"),
			section=as_str(args, "regulation_section"),
			explicit=as_str(args, "ai_source_citation"),
			text=as_str(args, "regulation_text"),
			read_on=as_date(args, "read_on") or frappe.utils.today(),
		)
	except ValueError as exc:
		raise ToolError(f"{exc}. Nothing was written.") from None

	spec = _template_spec_from_args(args, required=True)
	live = sessions.live_template(spec["template_name"]) or ""
	spec["version"] = _next_template_version(spec["template_name"])
	spec["authored_by"] = proposals.AUTHOR_AI
	spec["ai_source_citation"] = citation
	# Forced, all three. A template that arrived active would be on a handset
	# before anybody had read it against the regulation it claims to answer.
	spec["active"] = 0
	spec["human_approved_by"] = None
	spec["human_approved_on"] = None
	flags = proposals.template_flags(spec["sections"], supersedes_live=bool(live))
	spec["ai_review_flags"] = proposals.dump_flags(flags)

	doc = sessions.build_template(spec)
	doc.insert(ignore_permissions=True)
	described = _describe_template(doc.name, with_sections=True)

	data = {
		**described,
		"proposal": True,
		"review_flags": proposals.notes_for(flags),
		"supersedes_on_approval": live or None,
		"next": ["get_inspection_template", "approve_inspection_template"],
	}
	if live:
		data["proposed_against"] = _describe_template(live, with_sections=True)
	data["note"] = _template_proposal_note(described, flags, live)
	return ToolResult(
		data=data,
		summary=(
			f"proposed inspection template {doc.name} ({doc.template_name} v{spec['version']}, "
			f"{len(spec['sections'])} section(s), AI-proposed, inactive"
			f"{f', {len(flags)} review flag(s)' if flags else ''})"
		),
		docstatus_delta="none → 0 (created)",
	)


def approve_inspection_template(args: dict) -> ToolResult:
	"""Read a drafted template, accept it, and turn it on — with a name against it.

	THE COUNTERPART TO `approve_compliance_rule`, and it exists for the same
	reason: v0.37.0 let a model draft a form, and a form a worker is asked to
	fill in is a compliance artefact whether a person or a model wrote it. The
	name and the date go on the record rather than into a log, so an auditor
	asking who decided the close-down should ask for the propane reading gets the
	answer off the template.

	It is also where a proposed replacement swaps in: if a live template already
	holds this name at a lower version, that row is deactivated and pointed here
	— never edited, so every session already worked from it stays readable
	against the sections the worker actually saw.
	"""
	name = _template_or_refuse(as_str(args, "name") or as_str(args, "template", required=True), "name")
	row = sessions.template_row(name)
	if str(row.get("superseded_by") or "").strip():
		raise ToolError(
			f"Inspection Template {name} was superseded by {row['superseded_by']} and cannot be "
			"activated — two live templates with one name are two answers to 'start a "
			f"{row.get('template_name')} here'. Approve that one instead. Nothing was written."
		)
	if compat.checked(row.get("active")):
		raise ToolError(
			f"Inspection Template {name} ({row.get('template_name')}) is already active. Nothing was written."
		)

	approver = as_str(args, "approver") or _calling_user()
	superseded = _stand_down_the_live_template(row)

	doc = frappe.get_doc(TEMPLATE, name)
	doc.active = 1
	doc.human_approved_by = approver
	doc.human_approved_on = frappe.utils.now()
	doc.save(ignore_permissions=True)
	if superseded:
		frappe.db.set_value(TEMPLATE, superseded, "superseded_by", name, update_modified=False)

	described = _describe_template(name, with_sections=True)
	return ToolResult(
		data={
			**described,
			"supersedes": superseded or None,
			"was_proposed_by": row.get("authored_by") or proposals.AUTHOR_OPERATOR,
			"note": (
				f"{described['template_name']} v{described['version']} is live: it reaches the "
				f"handset on the next fetch and the rule engine can match it on the next sweep. "
				f"{approver} accepted it, and that is on the record."
				+ (
					" It was AI-proposed, so what was accepted is a person's reading of the sections "
					"against the regulation, not the model's."
					if described["authored_by"] == proposals.AUTHOR_AI
					else ""
				)
				+ (
					f" {superseded} held this name and is now inactive and pointing here. It was NOT "
					"edited — every session worked from it is still readable against the sections "
					"the worker actually saw, and every compliance record they produced is still in "
					"the register."
					if superseded
					else ""
				)
			),
		},
		summary=(
			f"approved and activated {name} ({described['template_name']} v{described['version']})"
			+ (f", superseding {superseded}" if superseded else "")
		),
		docstatus_delta="0 → 0 (updated)",
	)


def _next_template_version(template_name: str) -> int:
	"""One past the highest version any row holding this name has.

	ANY row: a second proposal made while a first is still in the queue is a
	third version, and two drafts sharing a number would make the queue
	unreadable at the moment somebody is comparing them.
	"""
	rows = frappe.db.get_all(
		TEMPLATE,
		filters={"template_name": template_name},
		fields=["version"],
		order_by="version desc",
		limit=1,
	)
	return (int(rows[0]["version"] or 1) + 1) if rows else 1


def _stand_down_the_live_template(row: dict) -> str:
	"""Deactivate the live template holding this name. Only a LATER over an EARLIER.

	`superseded_by` is set by the caller AFTER the new row saves, so a save that
	fails leaves the old row pointing at nothing rather than at a template that
	was never activated — and the dispatcher's rollback puts it back either way.
	"""
	name = str(row.get("name") or "")
	template_name = str(row.get("template_name") or "")
	if not (name and template_name):
		return ""
	live = sessions.live_template(template_name)
	if not live or live == name:
		return ""
	live_row = sessions.template_row(live)
	if int(live_row.get("version") or 1) >= int(row.get("version") or 1):
		return ""
	frappe.db.set_value(TEMPLATE, live, "active", 0, update_modified=False)
	return live


def _template_proposal_note(described: dict, flags: list, live: str) -> str:
	lines = [
		"THIS TEMPLATE IS INACTIVE AND NO HANDSET WILL FETCH IT. It is marked AI-proposed with the "
		"source it was read from, and it stays marked that way after approval — what approval adds "
		"is a person's name, not a change of authorship.",
		"Read the sections against the regulation, then approve_inspection_template. What to look "
		"at first: whether each section asks for evidence somebody can actually produce standing in "
		"the place, and whether the ones that produce a compliance record point at the right "
		"doctype — a section producing the wrong record files a real document about the wrong thing.",
	]
	if live:
		lines.append(
			f"There is already a LIVE template with this name: {live}. It is untouched, and a worker "
			"starting a visit this afternoon gets that one. Approving this draft deactivates it and "
			"points it here, at that point, by a person's decision."
		)
	if flags:
		lines.append("REVIEW FLAGS: " + ", ".join(flags) + ". `review_flags` says what each one means.")
	return "\n\n".join(lines)


def _calling_user() -> str:
	"""Whoever Frappe authenticated, else the session user, else Administrator.

	The same reader `tools/rules.py` uses for an approval, and for the same
	reason: an approver named in the ARGUMENTS is a name anybody can type, and
	the point of the field is that somebody is answerable for what it says.
	"""
	try:
		from ..security import caller_identity

		user = str(caller_identity() or "").strip()
		if user:
			return user
	except Exception:
		pass
	user = str(getattr(frappe.session, "user", "") or "").strip()
	return user if user and user != "Guest" else "Administrator"

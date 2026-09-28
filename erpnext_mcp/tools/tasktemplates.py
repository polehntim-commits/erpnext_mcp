# SPDX-License-Identifier: MIT
"""Farm Task Templates: author them, read them, and raise work from them.

FIVE TOOLS, AND THEY ARE THE POINT OF v0.41.0 in the same way the seven KPI
tools were the point of v0.39.0. Everything else in the release — the DocType,
the child table, the snapshot, the seeder, the wiring into the compliance
generator — exists so that these can exist: an operation can define the shape of
a job its own regulator asks for, and the next task raised has that shape, with
no release, no deploy and no engineer.

WHAT THE FIVE ARE FOR, AND WHAT EACH ONE REFUSES:

  * `create_farm_task_template` writes one. It refuses a missing or nonsense
    evidence contract, for exactly the reason `create_farm_task` does — a
    template with no contract raises tasks with no contract, and Farm Task will
    not insert those, so a template that saved without one would be a trap that
    springs in front of whoever is stood in the cabin.
  * `update_farm_task_template` EDITS IN PLACE and says so. Unlike
    `update_compliance_rule` and `update_inspection_template`, which supersede by
    copy. The difference is what reads back: a task copies its template ONCE, at
    creation, and is self-contained afterwards — so an edit changes what future
    tasks look like and cannot reach a task already raised, already claimed or
    already half-worked. `erpnext_mcp/task_templates.py` argues it in full.
  * `list_farm_task_templates` and `get_farm_task_template` are the register.
    `get` reports how many tasks have been raised from a template, which is the
    number to read before editing one: a template nothing has ever used is a
    template nobody has noticed is wrong.
  * `create_task_from_template` is the door a foreman uses. Everything comes off
    the template; `location`, `assigned_to` and `urgency` are the three things
    that are true of the CASE rather than of the job, and they are the three
    overrides.

WHAT IS NOT HERE, ON PURPOSE. There is no `delete_farm_task_template`. A template
that has raised work is the answer to 'what did this job ask for last season',
and every task raised from one links back to it — deleting one turns those links
into a dangling reference on a record an auditor is reading. `enabled=false`
through `update_farm_task_template` is the honest way to retire one, and it is
the same doctrine `deactivate_inspection_template` and Training Type's `active`
flag follow.
"""

from __future__ import annotations

import json

import frappe

from .. import compat, task_templates
from .. import training as regimes_vocabulary
from ..args import as_bool, as_choice, as_int, as_limit, as_str, resolve_company
from ..erpnext_mcp.doctype.farm_task.farm_task import (
	AVAILABLE,
	CLAIMED,
	DISPATCH_DISPATCHED,
	DRAFT,
	EVIDENCE_KEYS,
	ORIGIN_FOREMAN_DISPATCH,
	parse_evidence_required,
)
from ..errors import ToolError
from ..result import ToolResult
from . import dispatch, inspections

TEMPLATE = task_templates.TEMPLATE_DOCTYPE
FARM_TASK = dispatch.FARM_TASK


# ── shared ──────────────────────────────────────────────────────────────────
def _require() -> None:
	compat.require_doctype(
		TEMPLATE,
		"It ships with erpnext_mcp — run `bench --site <site> migrate` after upgrading the app.",
	)


def _template_or_refuse(reference: str) -> str:
	_require()
	name = task_templates.resolve(reference)
	if not name:
		raise ToolError(
			f"no Farm Task Template called {reference!r} on this site — neither as a docname nor "
			"as a template name. list_farm_task_templates has the register. Nothing was written."
		)
	return name


def _tasks_raised(name: str) -> int:
	"""How many Farm Tasks name this template. The number to read before editing."""
	if not compat.doctype_exists(FARM_TASK):
		return 0
	return frappe.db.count(FARM_TASK, {"template": name})


def _checklist_argument(raw, label: str = "checklist") -> list:
	"""The checklist off the arguments, as a list of items or bare strings."""
	if raw in (None, ""):
		return []
	if isinstance(raw, str):
		raise ToolError(
			f"{label} must be a JSON list of items, not a string. Either a list of sentences — "
			'["Press the smoke alarm", "Press the CO alarm"] — or a list of objects with '
			"`item_name`, `required` and `evidence_type`. Nothing was written."
		)
	if not isinstance(raw, list):
		raise ToolError(f"{label} must be a JSON list, got {type(raw).__name__}. Nothing was written.")
	return list(raw)


def _regimes_argument(raw, label: str = "compliance_regimes") -> list:
	if raw in (None, "", []):
		return []
	try:
		return regimes_vocabulary.require(raw, label)
	except ValueError as exc:
		raise ToolError(str(exc)) from None


def _creates_record_warning(creates_record: str) -> str:
	"""What to say about a `creates_record` this site cannot actually produce.

	A WARNING AND NOT A REFUSAL, and the DocType's own docstring argues why: this
	app installs doctypes over time, an operation may template work against a
	record a later release adds, and refusing here makes a template unsaveable
	today for a spelling that will be right in a month. The refusal that matters
	is at TASK creation, where somebody is about to be sent somewhere.
	"""
	if not creates_record:
		return ""
	if not compat.doctype_exists(creates_record):
		return (
			f"This template says completing its tasks produces a {creates_record!r} and this site "
			"has no such DocType. The template saved — a record a later release adds is a "
			"legitimate thing to template against — but create_task_from_template will REFUSE "
			"until it exists, because a task promising a record nobody can write is a promise "
			"that fails in front of a worker stood in a cabin."
		)
	if creates_record not in inspections.BUILDERS:
		return (
			f"{creates_record} exists on this site but erpnext_mcp has no completion builder for "
			"it, so completing a task from this template will file the evidence against the "
			"assignment and report that it could not produce the record itself. The three it can "
			f"build are: {', '.join(sorted(inspections.BUILDERS))}."
		)
	return ""


# ── 1. create_farm_task_template ────────────────────────────────────────────
def create_farm_task_template(args: dict) -> ToolResult:
	"""Define the shape of one recurring job. The next task raised has that shape."""
	_require()
	name = as_str(args, "template_name", required=True)
	if task_templates.resolve(name):
		raise ToolError(
			f"a Farm Task Template called {name!r} already exists on this site. Templates are "
			"edited IN PLACE — update_farm_task_template changes it, and the change reaches "
			"FUTURE tasks only. Nothing was written."
		)

	raw_evidence = args.get("evidence_required")
	if raw_evidence in (None, ""):
		raise ToolError(
			"evidence_required is required, for the same reason it is required on a task: a "
			"template with no evidence contract raises tasks with no evidence contract, and "
			"create_farm_task refuses those — so this would be a template that saves happily and "
			"fails the first time anybody stood in a cabin tries to use it. Say what closing one "
			'of these obliges somebody to produce, as JSON — {"photos": true, "findings_text": '
			f"true}}. The keys are: {', '.join(sorted(EVIDENCE_KEYS))}. Nothing was written."
		)

	spec = {
		"template_name": name,
		"task_type": as_choice(FARM_TASK, "task_type", as_str(args, "task_type", required=True), "task_type"),
		"description": as_str(args, "description"),
		"skill_required": as_str(args, "skill_required"),
		"estimated_duration_minutes": as_int(args, "estimated_duration_minutes") or 0,
		"dispatch_mode": as_choice(
			TEMPLATE, "dispatch_mode", as_str(args, "dispatch_mode") or "Either", "dispatch_mode"
		),
		"default_urgency": as_choice(
			TEMPLATE, "default_urgency", as_str(args, "default_urgency") or "Normal", "default_urgency"
		),
		"evidence_required": parse_evidence_required(raw_evidence),
		"creates_record": as_str(args, "creates_record"),
		"creates_record_data": task_templates.as_object(
			args.get("creates_record_data"), "creates_record_data"
		),
		"instructions": as_str(args, "instructions"),
		"checklist": _checklist_argument(args.get("checklist")),
		"company": resolve_company(as_str(args, "company")) if args.get("company") else None,
		"enabled": as_bool(args, "enabled", True),
		"compliance_regimes": _regimes_argument(args.get("compliance_regimes")),
	}

	doc = task_templates.build_template(spec)
	doc.insert(ignore_permissions=True)
	described = task_templates.describe(doc.name, with_checklist=True)

	warnings = [line for line in (_creates_record_warning(spec["creates_record"]),) if line]
	if not spec["checklist"]:
		warnings.append(
			"This template has no checklist, which is the ordinary case and usually right — a "
			"one-item list saying 'do the task' is a form people learn to tick without reading. "
			"Add one only where the work genuinely subdivides into steps somebody could forget."
		)
	data = {
		**described,
		"note": (
			"This template is live. The next task raised from it — by a foreman through "
			"create_task_from_template, or by the sweep through a Compliance Rule naming it as "
			"producer_task_template — has this shape, with no app release involved. A task "
			"SNAPSHOTS this record at creation, so editing it later changes what FUTURE tasks "
			"look like and reaches nothing already raised."
		),
	}
	if warnings:
		data["warnings"] = warnings
	return ToolResult(
		data=data,
		summary=(
			f"created farm task template {doc.name} ({described['task_type']}, "
			f"{described['checklist_item_count']} checklist item(s))"
		),
		docstatus_delta="none → 0 (created)",
	)


# ── 2. update_farm_task_template ────────────────────────────────────────────
#: Fields `update` reads straight off the arguments, and the reader each one goes
#: through. ONE TABLE so `create` and `update` cannot drift into accepting
#: different spellings of the same template.
_TEXT_FIELDS = ("description", "skill_required", "creates_record", "instructions")
_CHOICE_FIELDS = (("task_type", FARM_TASK), ("dispatch_mode", TEMPLATE), ("default_urgency", TEMPLATE))


def update_farm_task_template(args: dict) -> ToolResult:
	"""Edit one template in place. It changes what FUTURE tasks look like, nothing else."""
	name = _template_or_refuse(as_str(args, "template", required=True))
	before = task_templates.describe(name, with_checklist=True)
	doc = frappe.get_doc(TEMPLATE, name)
	changes = {}

	for field in _TEXT_FIELDS:
		if field in args:
			value = as_str(args, field)
			if str(getattr(doc, field, "") or "") != value:
				changes[field] = {"from": getattr(doc, field, "") or "", "to": value}
			setattr(doc, field, value)

	for field, doctype in _CHOICE_FIELDS:
		if field in args:
			value = as_choice(doctype, field, as_str(args, field, required=True), field)
			if str(getattr(doc, field, "") or "") != value:
				changes[field] = {"from": getattr(doc, field, "") or "", "to": value}
			setattr(doc, field, value)

	if "estimated_duration_minutes" in args:
		value = as_int(args, "estimated_duration_minutes") or 0
		if int(doc.estimated_duration_minutes or 0) != value:
			changes["estimated_duration_minutes"] = {
				"from": int(doc.estimated_duration_minutes or 0),
				"to": value,
			}
		doc.estimated_duration_minutes = value

	if "enabled" in args:
		value = 1 if as_bool(args, "enabled", True) else 0
		if int(compat.checked(doc.enabled)) != value:
			changes["enabled"] = {"from": bool(compat.checked(doc.enabled)), "to": bool(value)}
		doc.enabled = value

	if "company" in args:
		value = resolve_company(as_str(args, "company")) if args.get("company") else None
		if (doc.company or None) != value:
			changes["company"] = {"from": doc.company or None, "to": value}
		doc.company = value

	if "evidence_required" in args:
		value = parse_evidence_required(args.get("evidence_required"))
		if before["evidence_required"] != value:
			changes["evidence_required"] = {"from": before["evidence_required"], "to": value}
		doc.evidence_required = json.dumps(value)

	if "creates_record_data" in args:
		value = task_templates.as_object(args.get("creates_record_data"), "creates_record_data")
		if before["creates_record_data"] != value:
			changes["creates_record_data"] = {"from": before["creates_record_data"], "to": value}
		doc.creates_record_data = json.dumps(value)

	if "compliance_regimes" in args:
		value = _regimes_argument(args.get("compliance_regimes"))
		if before["compliance_regimes"] != value:
			changes["compliance_regimes"] = {"from": before["compliance_regimes"], "to": value}
		doc.set("compliance_regimes", [])
		for regime in regimes_vocabulary.to_rows(value):
			doc.append("compliance_regimes", dict(regime))

	# THE CHECKLIST IS REPLACED WHOLE, never edited one row at a time by index —
	# the same doctrine `update_compliance_rule` applies to its ordered heuristic
	# tables, and for the same reason: a list edited by index is one somebody
	# reorders by accident, and in an ordered checklist the order is content.
	if "checklist" in args:
		rows = [
			task_templates._checklist_row(item, index)
			for index, item in enumerate(_checklist_argument(args.get("checklist")))
		]
		doc.set("checklist", [])
		for row in rows:
			doc.append("checklist", row)
		changes["checklist"] = {
			"from": [entry["item_name"] for entry in before.get("checklist") or []],
			"to": [row["item_name"] for row in rows],
		}

	if not changes:
		raise ToolError(
			f"nothing to change on {name} — every argument given matches what is already on the "
			"record. Nothing was written."
		)

	doc.save(ignore_permissions=True)
	described = task_templates.describe(name, with_checklist=True)
	raised = _tasks_raised(name)
	warnings = [line for line in (_creates_record_warning(described["creates_record"] or ""),) if line]
	data = {
		**described,
		"changed": changes,
		"tasks_already_raised": raised,
	}
	if warnings:
		data["warnings"] = warnings
	return ToolResult(
		data={
			**data,
			"note": (
				f"THIS EDIT REACHES FUTURE TASKS ONLY. The {raised} task(s) already raised from "
				"this template snapshotted it at creation — their type, skill, duration, evidence "
				"contract, instructions and checklist are on the tasks themselves and did not "
				"move. That is why this record is edited IN PLACE rather than superseded by copy "
				"the way a Compliance Rule and an Inspection Template are: nothing reads back from "
				"it, so there is no live document whose meaning this could change underneath "
				"somebody halfway through the work."
			),
		},
		summary=f"updated farm task template {name} ({', '.join(sorted(changes))})",
		docstatus_delta="0 → 0 (updated)",
	)


# ── 3. list_farm_task_templates ─────────────────────────────────────────────
def list_farm_task_templates(args: dict) -> ToolResult:
	"""The register: what shapes of work this operation has defined."""
	_require()
	limit = min(as_limit(args), task_templates.TEMPLATE_CAP)
	task_type = as_str(args, "task_type")
	skill = as_str(args, "skill_required")
	enabled = as_bool(args, "enabled", None)
	company = as_str(args, "company")
	regime = as_str(args, "regime")

	filters = {}
	if task_type:
		filters["task_type"] = task_type
	if skill:
		filters["skill_required"] = skill
	if enabled is not None:
		filters["enabled"] = 1 if enabled else 0
	if company:
		filters["company"] = resolve_company(company)

	rows = frappe.db.get_all(
		TEMPLATE, filters=filters, fields=["name"], order_by="template_name asc", limit=limit
	)
	described = [task_templates.describe(str(row["name"])) for row in rows or []]
	if regime:
		# By TOKEN, never substring — "GlobalGAP" contains "GAP", and a LIKE would
		# put every GLOBALG.A.P. template in a USDA GAP answer.
		try:
			regimes_vocabulary.require([regime], "regime")
		except ValueError as exc:
			raise ToolError(str(exc)) from None
		described = [
			entry for entry in described if regimes_vocabulary.matches(entry["compliance_regimes"], regime)
		]

	live = [entry["name"] for entry in described if entry["enabled"]]
	return ToolResult(
		data={
			"count": len(described),
			"limit": limit,
			"enabled_templates": live,
			"templates": described,
			"note": (
				"A disabled template is still listed, because every task ever raised from it is "
				"still readable and an auditor asking 'what did this job ask for last season' is "
				"asking about one of those. `enabled_templates` is the set new work can be raised "
				"from. Templates are DATA: create_farm_task_template and "
				"update_farm_task_template change what a job looks like with no app release."
			),
		},
		summary=f"{len(described)} farm task template(s); {len(live)} enabled",
	)


# ── 4. get_farm_task_template ───────────────────────────────────────────────
def get_farm_task_template(args: dict) -> ToolResult:
	"""One template in full, with its checklist and how much work it has raised."""
	name = _template_or_refuse(as_str(args, "template", required=True))
	described = task_templates.describe(name, with_checklist=True)
	raised = _tasks_raised(name)
	problems = []
	creates_record = described["creates_record"] or ""
	warning = _creates_record_warning(creates_record)
	if warning:
		problems.append(warning)
	if not described["evidence_required"]:
		problems.append(
			"This template has no readable evidence contract, so create_task_from_template will "
			"refuse. Fix it with update_farm_task_template."
		)
	rules = _rules_producing(name)
	data = {
		**described,
		"tasks_raised": raised,
		"rules_using_this_template": rules,
		"snapshot_note": (
			"Everything above is COPIED onto a task at creation — the type, the skill, the "
			"duration, the dispatch mode, the evidence contract, the record it produces and its "
			"defaults, the instructions and the whole checklist. A task reads this record exactly "
			"once. Editing it changes what future tasks look like and reaches none of the "
			f"{raised} already raised."
		),
	}
	if problems:
		data["problems"] = problems
	return ToolResult(
		data=data,
		summary=(
			f"{described['template_name']} — {described['task_type']}, "
			f"{described['checklist_item_count']} checklist item(s), {raised} task(s) raised"
		),
	)


def _rules_producing(name: str) -> list:
	"""The Compliance Rules that raise their work through this template."""
	if not compat.doctype_exists("Compliance Rule"):
		return []
	rows = frappe.db.get_all(
		"Compliance Rule",
		filters={"producer_task_template": name},
		fields=["name", "rule_id", "title", "enabled"],
		limit=200,
	)
	return [
		{
			"name": str(row["name"]),
			"rule_id": str(row.get("rule_id") or ""),
			"title": str(row.get("title") or ""),
			"enabled": compat.checked(row.get("enabled")),
		}
		for row in rows or []
	]


# ── 5. create_task_from_template ────────────────────────────────────────────
def create_task_from_template(args: dict, *, origin: str = "", fields: dict | None = None) -> ToolResult:
	"""Raise one task pre-filled from a template. Location and assignee are the overrides."""
	name = _template_or_refuse(as_str(args, "template", required=True))
	row = task_templates.template_row(name)
	if not compat.checked(row.get("enabled")):
		raise ToolError(
			f"Farm Task Template {name} is disabled, so no new work may be raised from it. Every "
			"task already raised from it is unaffected and still completable. Re-enable it with "
			"update_farm_task_template(enabled=true) if the operation has started doing it again. "
			"Nothing was created."
		)

	shape = task_templates.snapshot(name)
	location_doctype = as_str(args, "location_doctype")
	location = as_str(args, "location")
	if location and not location_doctype:
		raise ToolError(
			"location was given with no location_doctype, so nothing can resolve it. Pass the "
			"register it is in: 'Housing Unit', 'Field', 'Irrigation Zone' or 'Parcel'. Nothing "
			"was created."
		)
	if location_doctype and location:
		if not compat.doctype_exists(location_doctype):
			raise ToolError(
				f"this site has no {location_doctype!r} DocType, so {location!r} cannot be "
				"resolved. Nothing was created."
			)
		if not frappe.db.exists(location_doctype, location):
			raise ToolError(
				f"no {location_doctype} called {location!r} on this site. A task about a place "
				"that does not exist cannot be routed to anybody stood in it. Nothing was created."
			)

	creates_record = shape.get("creates_record") or ""
	if creates_record and not compat.doctype_exists(creates_record):
		raise ToolError(
			f"template {name} says completing its tasks produces a {creates_record!r} and this "
			"site does not have that DocType. A task promising a record nobody can write is a "
			"promise that fails in front of a worker stood in a cabin. Fix the template with "
			"update_farm_task_template. Nothing was created."
		)

	source_alert = as_str(args, "source_alert")
	if source_alert:
		if not compat.doctype_exists(dispatch.ALERT) or not frappe.db.exists(dispatch.ALERT, source_alert):
			raise ToolError(f"no Compliance Alert called {source_alert!r} on this site. Nothing was created.")
		existing = frappe.db.get_value(FARM_TASK, {"source_alert": source_alert}, "name")
		if existing:
			raise ToolError(
				f"Farm Task {existing} already answers alert {source_alert}. One task per alert — "
				"two people sent to walk the same cabin is the thing the source link exists to "
				"prevent. Nothing was created."
			)

	worker = dispatch._worker(args, "assigned_to", required=False)
	draft = as_bool(args, "draft", False)
	company = resolve_company(as_str(args, "company")) if args.get("company") else shape.get("company")

	doc = frappe.new_doc(FARM_TASK)
	doc.template = shape["template"]
	# THE TASK NAME IS THE TEMPLATE NAME AND THE PLACE, unless somebody says
	# otherwise. 'Smoke Detector Test — MC-Cabin-01' is what a foreman reads on a
	# board; 'Smoke Detector Test' fifty-four times is a board nobody can work
	# from.
	default_name = row.get("template_name") or name
	if location:
		default_name = f"{default_name} — {location}"
	doc.task_name = (as_str(args, "task_name") or default_name)[:140]
	doc.task_type = shape["task_type"]
	# v0.203.0. `origin` and `fields` are for this app's own triggers (a bait
	# product issued to a cabin raises a `field_reported` placement carrying the
	# product) and are not arguments a caller can send.
	doc.origin = origin or ORIGIN_FOREMAN_DISPATCH
	for key, value in (fields or {}).items():
		doc.set(key, value)
	doc.urgency = (
		as_choice(FARM_TASK, "urgency", as_str(args, "urgency"), "urgency")
		if args.get("urgency")
		else shape["urgency"]
	)
	doc.dispatch_mode = shape["dispatch_mode"]
	doc.company = company or dispatch._company(args)
	doc.location_doctype = location_doctype or None
	doc.location = location or None
	doc.skill_required = shape["skill_required"]
	doc.estimated_duration_minutes = shape["estimated_duration_minutes"]
	doc.source_alert = source_alert or None
	doc.creates_record = creates_record
	doc.creates_record_data = json.dumps(
		{
			**shape["creates_record_data"],
			**task_templates.as_object(args.get("creates_record_data"), "creates_record_data"),
		}
	)
	doc.evidence_required = json.dumps(shape["evidence_required"])
	doc.checklist_status = json.dumps(shape["checklist_status"])
	# The template's instructions first, then anything true of THIS case. The
	# order is the point: a worker reads the standing instruction and then the
	# note about the particular cabin, which is the order they need them in.
	extra = as_str(args, "notes")
	doc.notes = "\n\n".join(part for part in (shape["notes"], extra) if part).strip()
	doc.state = DRAFT if draft else AVAILABLE
	if worker:
		doc.assigned_to = worker
		doc.assigned_to_name = dispatch._worker_name(worker, as_str(args, "assigned_to_name"))
		doc.state = DRAFT if draft else CLAIMED
	doc.insert(ignore_permissions=True)

	assignment = None
	if worker and not draft:
		assignment = dispatch._open_assignment(doc, worker, doc.assigned_to_name, dispatched=True)

	described = dispatch._describe_task(dict(doc.as_dict()))
	warnings = []
	if creates_record and creates_record not in inspections.BUILDERS:
		warnings.append(
			f"{creates_record} exists on this site but erpnext_mcp has no builder for it, so "
			"completing this task will file the evidence against the assignment and report that "
			"it could not produce the record itself."
		)
	if not location:
		warnings.append(
			"This task names no location, so it cannot be routed to whoever is already stood in "
			"the right place. Legitimate for desk work — a certificate renewal happens at a desk."
		)
	if described["dispatch_mode"] == DISPATCH_DISPATCHED and not worker:
		warnings.append(
			"This template's dispatch mode is Dispatched and nobody was assigned, so the task "
			"will sit in Available where no worker may claim it. Somebody has to be sent with "
			"assign_farm_task."
		)
	data = {
		**described,
		"template": shape["template"],
		"assignment": assignment,
		"checklist": task_templates.snapshot(name)["checklist_status"]["items"],
		"note": (
			f"Everything about this task's SHAPE came off {name} and was COPIED onto the task — "
			"the type, the skill, the duration, the dispatch mode, the evidence contract, the "
			"record it produces and the checklist. Editing the template from here on changes what "
			"future tasks look like and cannot reach this one."
		),
	}
	if warnings:
		data["warnings"] = warnings
	return ToolResult(
		data=data,
		summary=(
			f"raised farm task {doc.name} from template {row.get('template_name')} "
			f"({doc.task_type}, {doc.urgency}, {doc.state})"
			+ (f" assigned to {doc.assigned_to_name}" if worker else "")
		),
		docstatus_delta="none → 0 (created)",
	)

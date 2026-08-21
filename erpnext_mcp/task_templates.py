# SPDX-License-Identifier: MIT
"""Farm Task Templates: the shape of one recurring job, as data rather than code.

WHAT WAS WRONG BEFORE v0.41.0. The template concept existed and was spread across
three places that could not be edited together. `ALERT_TASK_MAP` in
`tools/dispatch.py` held thirteen recipes as a Python dict — the type, the skill,
the minutes, the dispatch mode and the evidence contract for every shipped
compliance rule — and moving one of them was a code release. The Compliance Rule
DocType held `producer_farm_task_type`, `producer_skill_required` and
`evidence_contract_json` as loose fields, so a rule an operator wrote could carry
a producer recipe but nothing reusable: two rules asking for the same job stated
it twice, in full, and drifted. And `producer_task_template` pointed at an
Inspection Template, which is a MULTI-SECTION VISIT and the wrong size of thing
for 'test the smoke detector'.

This module is the missing record. A Farm Task Template says what one job looks
like — once — and a rule, a foreman or a worker raises tasks from it.

────────────────────────────────────────────────────────────────────────────
A TASK SNAPSHOTS ITS TEMPLATE. THIS IS THE LOAD-BEARING RULE.
────────────────────────────────────────────────────────────────────────────

`snapshot` copies everything a task needs onto the task's own fields: the type,
the skill, the duration, the dispatch mode, the evidence contract, the record it
produces and its defaults, the instructions, and the whole checklist with a
`done` flag beside each item. After that the task is SELF-CONTAINED. It can be
claimed, worked, evidenced and closed with the template deleted.

Three things follow, and they are the reasons the design is worth having:

  * EDITING A TEMPLATE CHANGES WHAT FUTURE TASKS LOOK LIKE AND NOTHING ELSE. A
    worker halfway through a five-item habitability walk whose template lost an
    item does not find their evidence attached to a list that no longer contains
    it. Nobody's contract tightens under them mid-job.
  * THE TEMPLATE NEEDS NO VERSIONING BY COPY. An Inspection Template and a
    Compliance Rule are both versioned that way because a live document reads
    back from them — a session submits against its template's sections weeks
    after starting, an alert is read against the rule that raised it. Nothing
    reads back from this one. Versioning it would buy an audit trail
    `track_changes` already keeps, at the cost of a register where the eight
    templates an operation runs are lost among forty superseded rows.
  * A TASK IS STILL AUDITABLE WITHOUT THE TEMPLATE. `Farm Task.template` is
    provenance — 'where did this shape come from' — never a lookup.

────────────────────────────────────────────────────────────────────────────
THE SEEDED TEMPLATES, AND WHY THOSE
────────────────────────────────────────────────────────────────────────────

The first five are the five shipped compliance rules whose work has a repeatable shape,
and every one of them is seeded FROM `ALERT_TASK_MAP` in spirit and by hand in
fact: the same task type, the same skill, the same minutes, the same dispatch
mode and the same evidence contract the thirteen recipes have used since
v0.16.0. That is deliberate and it is the backward-compatibility guarantee —
a site that seeds these templates and points its rules at them raises exactly
the tasks it raised before, plus a checklist.

THE SIXTH IS THE ODD ONE OUT AND SAYS SO. `Field Scouting` (v0.115.0) answers
no compliance rule and no alert — nothing raises it on a schedule, a foreman or
an agronomist raises it because a block needs walking. It is here because it is
the FIRST template whose completion produces an agronomic record rather than a
compliance one, and the shape of that is worth shipping rather than leaving
every operation to invent: a photograph, a growth stage, a sugar reading and a
coordinate, which is the minimum from which a pick date can later be argued.

It is also the first template whose record is written by a SWEEP rather than at
completion — see `tools/scouting.py`. That is invisible from here on purpose:
the template names `Crop Observation` in `creates_record` exactly as the
detector template names `Detector Test`, and which side of the line a producer
falls on is not something a template should have to know.

SEEDED, NOT FIXTURED. `test_hooks.py` forbids the word `fixtures` by name, and
for the reason it always has: a Frappe fixture is imported by `bench migrate`
with no ability to skip what a site has since edited, so an operator who added
an item to the detector checklist would lose it on every upgrade. The seeder
checks by template name across every row and creates only what is not there.
"""

from __future__ import annotations

import json

import frappe

from . import compat
from . import training as regimes_vocabulary
from .erpnext_mcp.doctype.farm_task.farm_task import (
	parse_evidence_required,
	parse_json_object,
)
from .erpnext_mcp.doctype.farm_task_template.farm_task_template import checklist_rows

TEMPLATE_DOCTYPE = "Farm Task Template"
CHECKLIST_DOCTYPE = "Farm Task Template Checklist Item"

#: Most templates one list call returns. A site with two hundred templates has a
#: problem this cap does not solve, but a cap that is reported beats a register
#: that silently stops somewhere nobody chose.
TEMPLATE_CAP = 200

#: The fields a task copies off a template, read together so the snapshot and
#: the register cannot disagree about what a template holds.
TEMPLATE_FIELDS = (
	"name",
	"template_name",
	"task_type",
	"description",
	"skill_required",
	"estimated_duration_minutes",
	"dispatch_mode",
	"default_urgency",
	"evidence_required",
	"creates_record",
	"creates_record_data",
	"instructions",
	"company",
	"enabled",
	"creation",
	"modified",
	"owner",
)


#: The SOP attachment on a template, per language. `{fieldname: language tag}`.
#:
#: v0.98.0, AND THE LANGUAGE IS THE COLUMN RATHER THAN THE FILENAME. The handset
#: read it off the file name until now — `..._es.pdf`, `..._spanish.pdf` — which
#: is a convention rather than a fact: it works while the office follows it and
#: fails SILENTLY when they do not, because a document the app cannot place is
#: offered with no language badge at all. A Spanish-speaking picker is then shown
#: an unlabelled button and has to open it to find out. Two declared fields make
#: the answer a fact, and make "there is no Spanish version of this procedure" a
#: gap somebody can see and close.
#:
#: DELIBERATELY NOT IN `TEMPLATE_FIELDS` AND DELIBERATELY NOT IN `snapshot`. That
#: tuple is what a task COPIES, and copying a procedure onto every task raised
#: would freeze last season's PDF onto work still on the board. A task carries a
#: `template` link (v0.96.0, item 7) and reads the current document through it,
#: so replacing the procedure reaches every open task at once — which is the
#: behaviour an SOP actually needs and the opposite of the snapshot rule the
#: checklist and the evidence contract need.
SOP_DOCUMENT_FIELDS = (("sop_document_en", "en"), ("sop_document_es", "es"))


def sop_documents(name: str) -> dict:
	"""`{"en": url|None, "es": url|None}` for one template.

	READ DEFENSIVELY, one field at a time, because this is the newest thing on
	the record: a site that has the app and has not yet run `bench migrate` has
	the doctype and not these two columns, and a register read that raised there
	would take the whole template list down over an attachment nobody had filed
	yet. A missing column and an empty one report the same thing — no procedure
	on file — which is the honest answer in both cases.
	"""
	out = {tag: None for _, tag in SOP_DOCUMENT_FIELDS}
	if not name or not compat.doctype_exists(TEMPLATE_DOCTYPE):
		return out
	wanted = [field for field, _ in SOP_DOCUMENT_FIELDS if compat.has_field(TEMPLATE_DOCTYPE, field)]
	if not wanted:
		return out
	row = dict(frappe.db.get_value(TEMPLATE_DOCTYPE, name, wanted, as_dict=True) or {})
	for field, tag in SOP_DOCUMENT_FIELDS:
		out[tag] = str(row.get(field) or "").strip() or None
	return out


def template_row(name: str) -> dict:
	"""One template as a plain dict, or {}."""
	if not compat.doctype_exists(TEMPLATE_DOCTYPE) or not name:
		return {}
	row = frappe.db.get_value(TEMPLATE_DOCTYPE, name, TEMPLATE_FIELDS, as_dict=True)
	return dict(row) if row else {}


def resolve(reference: str) -> str | None:
	"""A template docname from a docname or a template name.

	The two are the same string today — the DocType names itself after
	`template_name` — and this exists anyway, for the same reason
	`sessions.resolve_template` does: a caller should not have to know that, and
	the day somebody renames a template the lookup by name still has to find it.
	"""
	reference = str(reference or "").strip()
	if not reference or not compat.doctype_exists(TEMPLATE_DOCTYPE):
		return None
	if frappe.db.exists(TEMPLATE_DOCTYPE, reference):
		return reference
	found = frappe.db.get_value(TEMPLATE_DOCTYPE, {"template_name": reference}, "name")
	return str(found) if found else None


def checklist_of(name: str) -> list:
	"""One template's checklist as plain dicts, in worked order."""
	if not compat.doctype_exists(CHECKLIST_DOCTYPE):
		return []
	return checklist_rows(name)


def regimes_of(name: str) -> list:
	"""The regime tags on one template, as canonical tokens."""
	if not compat.doctype_exists("Compliance Regime Link"):
		return []
	rows = frappe.db.get_all(
		"Compliance Regime Link",
		filters={"parent": name, "parenttype": TEMPLATE_DOCTYPE, "parentfield": "compliance_regimes"},
		fields=["regime"],
		limit=100,
	)
	return regimes_vocabulary.from_rows([dict(row) for row in rows or []])


def as_object(raw, label: str) -> dict:
	"""A JSON object from a string, a dict or nothing. The write-side parser."""
	return parse_json_object(raw, label)


def evidence_of(row: dict) -> dict:
	"""A template's evidence contract, read tolerantly.

	A READ path: a stored contract nobody can parse is reported as empty rather
	than taking a whole register listing down. The refusal lives on the write
	side, in the controller, where somebody is looking at what they typed.
	"""
	try:
		value = json.loads(row.get("evidence_required") or "{}")
	except Exception:
		return {}
	return dict(value) if isinstance(value, dict) else {}


def record_data_of(row: dict) -> dict:
	"""A template's produced-record defaults, read tolerantly."""
	try:
		value = json.loads(row.get("creates_record_data") or "{}")
	except Exception:
		return {}
	return dict(value) if isinstance(value, dict) else {}


def describe(name: str, with_checklist: bool = False) -> dict:
	"""One template as the tools report it."""
	row = template_row(name)
	if not row:
		return {}
	checklist = checklist_of(name)
	described = {
		"name": str(row.get("name") or ""),
		"template_name": str(row.get("template_name") or ""),
		"task_type": str(row.get("task_type") or ""),
		"description": str(row.get("description") or ""),
		"skill_required": str(row.get("skill_required") or ""),
		"estimated_duration_minutes": int(row.get("estimated_duration_minutes") or 0),
		"dispatch_mode": str(row.get("dispatch_mode") or "Either"),
		"default_urgency": str(row.get("default_urgency") or "Normal"),
		"evidence_required": evidence_of(row),
		"creates_record": str(row.get("creates_record") or "") or None,
		"creates_record_data": record_data_of(row),
		"instructions": str(row.get("instructions") or ""),
		"company": row.get("company") or None,
		"enabled": compat.checked(row.get("enabled")),
		"compliance_regimes": regimes_of(name),
		"checklist_item_count": len(checklist),
		"required_checklist_item_count": len([item for item in checklist if item.get("required")]),
	}
	# v0.98.0. The two procedure attachments, under BOTH the shapes a caller
	# might read: the flat `sop_document_en` / `sop_document_es` the iOS
	# `FarmTaskTemplate` decodes by name, and one `sop_documents` map for a
	# caller choosing by the language it holds rather than by writing an if.
	# Same two values twice rather than a rename in either direction, which is
	# the call `list_attachments` already made about `content_type`/`mime_type`.
	sop = sop_documents(described["name"])
	described["sop_documents"] = sop
	for field, tag in SOP_DOCUMENT_FIELDS:
		described[field] = sop.get(tag)
	described["sop_languages"] = [tag for _, tag in SOP_DOCUMENT_FIELDS if sop.get(tag)]
	if with_checklist:
		described["checklist"] = checklist
	return described


def snapshot(name: str) -> dict:
	"""Everything a task copies off one template, in the task's own vocabulary.

	THE ONE FUNCTION THAT DEFINES WHAT 'PRE-FILLED FROM A TEMPLATE' MEANS, and it
	is used by all three doors: `create_task_from_template`, the compliance-alert
	generator, and the seeder's own tests. One function so the three cannot drift
	into pre-filling different subsets — which would make 'the same template'
	produce two different tasks depending on who raised them.

	The returned keys are FARM TASK FIELD NAMES, not template field names, so a
	caller writes `doc.update(snapshot(...))`-shaped code and the translation
	lives here. `default_urgency` becomes `urgency`; `instructions` becomes
	`notes`, which is what the Farm Task has always called the field a worker
	reads; the checklist becomes `checklist_status` with every item undone.
	"""
	row = template_row(name)
	if not row:
		return {}
	checklist = checklist_of(name)
	return {
		"template": str(row.get("name") or ""),
		"task_type": str(row.get("task_type") or ""),
		"skill_required": str(row.get("skill_required") or ""),
		"estimated_duration_minutes": int(row.get("estimated_duration_minutes") or 0),
		"dispatch_mode": str(row.get("dispatch_mode") or "Either"),
		"urgency": str(row.get("default_urgency") or "Normal"),
		"evidence_required": evidence_of(row),
		"creates_record": str(row.get("creates_record") or ""),
		"creates_record_data": record_data_of(row),
		"notes": str(row.get("instructions") or ""),
		"company": row.get("company") or None,
		"checklist_status": {
			"items": [
				{
					"item_name": item["item_name"],
					"required": bool(item.get("required")),
					"evidence_type": str(item.get("evidence_type") or "None"),
					"sort_order": int(item.get("sort_order") or index + 1),
					"done": False,
					"note": "",
				}
				for index, item in enumerate(checklist)
			]
		},
	}


def build_template(spec: dict):
	"""One unsaved Farm Task Template document from a plain dict.

	Shared by the seeder and by `create_farm_task_template`, so a template this
	app ships and one somebody types through MCP are the same shape of record —
	including the parts a seeder could quietly have skipped.
	"""
	doc = frappe.new_doc(TEMPLATE_DOCTYPE)
	doc.template_name = str(spec.get("template_name") or "").strip()
	doc.task_type = str(spec.get("task_type") or "").strip()
	doc.description = str(spec.get("description") or "").strip()
	doc.skill_required = str(spec.get("skill_required") or "").strip()
	doc.estimated_duration_minutes = int(spec.get("estimated_duration_minutes") or 0)
	doc.dispatch_mode = str(spec.get("dispatch_mode") or "Either").strip()
	doc.default_urgency = str(spec.get("default_urgency") or "Normal").strip()
	doc.evidence_required = json.dumps(
		parse_evidence_required(spec.get("evidence_required") or spec.get("evidence"))
	)
	doc.creates_record = str(spec.get("creates_record") or "").strip()
	doc.creates_record_data = json.dumps(as_object(spec.get("creates_record_data"), "creates_record_data"))
	doc.instructions = str(spec.get("instructions") or "").strip()
	doc.company = spec.get("company") or None
	doc.enabled = 1 if spec.get("enabled", 1) else 0
	for regime in regimes_vocabulary.to_rows(spec.get("compliance_regimes") or spec.get("regimes") or []):
		doc.append("compliance_regimes", dict(regime))
	for index, item in enumerate(spec.get("checklist") or ()):
		doc.append("checklist", _checklist_row(item, index))
	return doc


def _checklist_row(item, index: int) -> dict:
	"""One checklist child row from a dict — or from a bare string.

	A BARE STRING IS ACCEPTED AND MEANS 'REQUIRED, NO EVIDENCE, IN THIS ORDER',
	because the commonest checklist anybody writes through a chat client is a list
	of sentences, and making them type four keys per item to get the obvious
	defaults is how a checklist ends up not being written at all.
	"""
	if not isinstance(item, dict):
		item = {"item_name": str(item or "")}
	order = item.get("sort_order")
	return {
		"item_name": str(item.get("item_name") or "").strip(),
		"required": 1 if item.get("required", True) else 0,
		"evidence_type": str(item.get("evidence_type") or "None").strip() or "None",
		"sort_order": int(order) if order not in (None, "") else index + 1,
	}


# ── the five this app ships ─────────────────────────────────────────────────
#: One entry per shipped compliance rule whose work has a repeatable shape.
#:
#: EVERY TYPE, SKILL, DURATION, DISPATCH MODE AND EVIDENCE CONTRACT HERE MATCHES
#: `tools/dispatch.ALERT_TASK_MAP`, deliberately and to the letter. A site that
#: seeds these and points its rules at them raises exactly the tasks it raised in
#: v0.16.0 — plus a checklist, which is the only thing that is new. The two
#: tables are checked against each other in `test_task_templates.py`, so the day
#: somebody edits one the other cannot quietly stay behind.
#:
#: THE CHECKLISTS ARE SHORT AND EVERY ITEM IS SOMETHING SOMEBODY COULD FORGET.
#: A checklist that restates the task name in five ways is a form people learn to
#: tick without reading, and once that habit is formed the required flag stops
#: meaning anything on every template.
SEED_TEMPLATES = (
	{
		"template_name": "Cabin Habitability Inspection",
		"task_type": "Inspection",
		"description": (
			"The habitability walk on an occupied cabin: the structure, the water, the heat, "
			"the beds and the facilities, against 29 CFR 1910.142 and OAR 437-004-1120."
		),
		"skill_required": "camp_maintenance",
		"estimated_duration_minutes": 45,
		"dispatch_mode": "Self-pick",
		"default_urgency": "Normal",
		"evidence_required": {"photos": True, "signature": True, "findings_text": True},
		"creates_record": "Housing Inspection",
		"instructions": (
			"Walk the whole cabin before you write anything down. Photograph what you find "
			"whether or not it is wrong — an inspection with photographs only of faults is one "
			"an auditor cannot tell from an inspection somebody skipped. If the cabin is "
			"occupied, knock and say what you are doing before you go in."
		),
		"regimes": ["OR-OSHA", "FSMA"],
		"checklist": (
			{"item_name": "Structure, roof and windows sound", "evidence_type": "Photo"},
			{"item_name": "Potable water running at every tap", "evidence_type": "Photo"},
			{"item_name": "Heating works and is safe to leave on", "evidence_type": "Photo"},
			{"item_name": "Toilets, showers and sinks working and clean", "evidence_type": "Photo"},
			{"item_name": "Beds, bedding and storage sufficient for the occupancy"},
			{"item_name": "Refuse containers present and emptied"},
			{
				"item_name": "Occupants asked whether anything needs fixing",
				"required": False,
				"evidence_type": "Text",
			},
		),
	},
	{
		"template_name": "Smoke Detector Test",
		"task_type": "Test",
		"description": (
			"Press-to-test every smoke and carbon monoxide detector in one unit and record what "
			"sounded. Both results go on one Detector Test record, which is why they are one task."
		),
		"skill_required": "camp_maintenance",
		"estimated_duration_minutes": 20,
		"dispatch_mode": "Self-pick",
		"default_urgency": "Normal",
		"evidence_required": {"photos": True, "findings_text": True},
		"creates_record": "Detector Test",
		"instructions": (
			"Test every detector in the unit, not a sample. A detector that does not sound is a "
			"Critical finding and the cabin should not be slept in until it is replaced — say so "
			"in the findings rather than fixing it silently, so the replacement is on the record too."
		),
		"regimes": ["OR-OSHA"],
		"checklist": (
			{"item_name": "Every smoke detector pressed and heard", "evidence_type": "Photo"},
			{"item_name": "Every CO detector pressed and heard", "evidence_type": "Photo"},
			{"item_name": "Batteries replaced where the unit chirped", "required": False},
			{"item_name": "Detector count matches the unit's record", "evidence_type": "Measurement"},
		),
	},
	{
		"template_name": "Water Quality Test",
		"task_type": "Water-Sampling",
		"description": (
			"Take an agricultural or potable water sample and send it to the laboratory. FSMA "
			"Subpart E for agricultural water; the state drinking water rule for a camp supply."
		),
		"skill_required": "water_sampling",
		"estimated_duration_minutes": 60,
		"dispatch_mode": "Self-pick",
		"default_urgency": "Normal",
		"evidence_required": {"photos": True, "findings_text": True},
		"creates_record": "Water Test",
		"instructions": (
			"Sterile bottle, gloves on, run the tap or the line before you fill. Photograph the "
			"sampling point and the labelled bottle together — a sample whose origin is a word in "
			"a box is one the laboratory result cannot be tied back to. Get it to the laboratory "
			"the same day; a sample held overnight is a result about the bottle."
		),
		"regimes": ["FSMA"],
		"checklist": (
			{"item_name": "Sampling point identified and photographed", "evidence_type": "Photo"},
			{"item_name": "Line or tap flushed before filling"},
			{"item_name": "Sterile bottle filled and labelled", "evidence_type": "Photo"},
			{"item_name": "Chain of custody form completed", "evidence_type": "Text"},
			{"item_name": "Delivered to the laboratory the same day"},
		),
	},
	{
		"template_name": "Certification Renewal",
		"task_type": "Compliance-Audit",
		"description": (
			"Renew a certificate or licence before it lapses — the paperwork, the fee, the "
			"inspection date and the new expiry on the register."
		),
		"skill_required": "compliance_admin",
		"estimated_duration_minutes": 90,
		"dispatch_mode": "Dispatched",
		"default_urgency": "High",
		"evidence_required": {"findings_text": True},
		"creates_record": "",
		"instructions": (
			"Renewing is not one act: the application, the fee and the certifier's own timetable "
			"are three, and the last one is not yours. Start early enough that a certifier who "
			"takes six weeks does not put the operation out of certification. Update the "
			"Certification record's expiry date when the new certificate arrives — the task "
			"closing is not what dismisses the alert; the register moving is."
		),
		"regimes": ["NOP"],
		"checklist": (
			{"item_name": "Renewal application submitted", "evidence_type": "Text"},
			{"item_name": "Fee paid", "evidence_type": "Text"},
			{"item_name": "Inspection scheduled where the certifier requires one", "required": False},
			{"item_name": "New certificate received and filed", "required": False, "evidence_type": "Photo"},
			{"item_name": "Certification record updated with the new expiry date"},
		),
	},
	{
		"template_name": "Field Scouting",
		"task_type": "Scouting",
		"description": (
			"Walk one block and record what it looks like: the growth stage, a Brix reading off "
			"a refractometer, a photograph, and whatever was found. Completing it produces a Crop "
			"Observation — the register the pest-pressure engine and the harvest-readiness map "
			"both read."
		),
		"skill_required": "crop_scouting",
		"estimated_duration_minutes": 30,
		"dispatch_mode": "Either",
		"default_urgency": "Normal",
		"evidence_required": {"photos": True, "findings_text": True, "gps": True},
		"creates_record": "Crop Observation",
		# THE ROUND AS SHIPPED IS A GENERAL ONE, AND THE COMPLETION NARROWS IT.
		# A scout who counted an organism sends observation_type "Pest Scout"
		# with the threat and the count in record_data and gets the threshold
		# engine; a round called to decide a pick sends "Harvest Readiness".
		# The DEFAULT cannot be "Pest Scout", because that is the one type whose
		# record is invalid without a threat — a template that shipped a
		# mandatory field it has no way to fill would refuse every completion
		# from a walk where nothing was found, which is most of them.
		"creates_record_data": {"observation_type": "General", "scouting_method": "Visual"},
		"instructions": (
			"Both numbers, every time. Sugar climbs while the stage stands still in a hot week "
			"and the stage advances while sugar stalls in a wet one, so a pick argued from either "
			"one alone gets called wrong — that is why the contract asks for both rather than "
			"whichever you have.\n\nSay how you took the Brix. A refractometer reading and an "
			"estimate are not the same measurement, and the figure that ends up quoted into a "
			"buyer's specification is the one nobody can tell apart afterwards.\n\nIf you "
			"counted something — mites on leaves, flies in a trap — say what and how many out of "
			"how many examined, and send observation_type 'Pest Scout' with it. That is what "
			"moves the block's pressure and can raise a recommendation; a round filed without it "
			"is a walk that was not looking for the pest, which is a true and useful thing to "
			"record and a different one.\n\nStand where the fruit is. The phone takes the "
			"coordinate on its own, and it is what lets next week's round of the same corner be "
			"compared to this one."
		),
		"regimes": [],
		"checklist": (
			{"item_name": "Growth stage read and recorded as a BBCH code", "evidence_type": "Measurement"},
			{"item_name": "Brix read, with the method it was read by", "evidence_type": "Measurement"},
			{"item_name": "Representative photograph of the fruit or the canopy", "evidence_type": "Photo"},
			{
				"item_name": "Pest or disease counted, with the sample size",
				"required": False,
				"evidence_type": "Measurement",
			},
			{"item_name": "What the block looked like, in words", "evidence_type": "Text"},
		),
	},
	{
		"template_name": "Training Record",
		"task_type": "Training",
		"description": (
			"Deliver a training session and file it: the topics actually covered, in the language "
			"it was delivered in, with the trainees' own signatures."
		),
		"skill_required": "hr_admin",
		"estimated_duration_minutes": 120,
		"dispatch_mode": "Dispatched",
		"default_urgency": "Normal",
		"evidence_required": {"findings_text": True, "signature": True},
		"creates_record": "",
		"instructions": (
			"COMPLETING THIS TASK DOES NOT FILE THE TRAINING RECORD — `record_training` does, and "
			"that is deliberate: a record has to name the topics that were actually covered and "
			"carry each trainee's signature, and no completion builder can invent either. A "
			"session that ran out of time before the emergency-response topic did not cover it, "
			"and a record claiming otherwise is the document an auditor disallows. Write what was "
			"covered in the findings, then file it."
		),
		"regimes": ["FSMA", "WPS"],
		"checklist": (
			{"item_name": "Trainer, date, language and location recorded", "evidence_type": "Text"},
			{"item_name": "Topics actually covered written down", "evidence_type": "Text"},
			{"item_name": "Every attendee signed the sheet", "evidence_type": "Photo"},
			{"item_name": "Filed with record_training"},
		),
	},
)


def seed_farm_task_templates() -> dict:
	"""One Farm Task Template per entry in `SEED_TEMPLATES`. Idempotent.

	CHECKED BY NAME, AND AN EDITED TEMPLATE IS LEFT ALONE — the same contract
	`sessions.seed_inspection_templates` and `compliance_rules.seed_compliance_rules`
	keep, and the whole difference between this and a Frappe `fixtures` entry.
	An operator who added an item to the detector checklist keeps it. One who
	disabled a template their operation does not run keeps it disabled.

	NEVER RAISES. It runs inside `bench migrate`, where an exception aborts the
	migration for the whole bench. Everything it could not do lands in `failed`
	and is printed by the installer.
	"""
	report = {"created": [], "present": [], "failed": []}
	if not compat.doctype_exists(TEMPLATE_DOCTYPE):
		return report
	for spec in SEED_TEMPLATES:
		name = spec["template_name"]
		try:
			if frappe.db.exists(TEMPLATE_DOCTYPE, {"template_name": name}):
				report["present"].append(name)
				continue
			build_template(spec).insert(ignore_permissions=True)
			report["created"].append(name)
		except Exception as exc:  # pragma: no cover - reported, never raised
			report["failed"].append({"name": name, "reason": f"{type(exc).__name__}: {exc}"})
	return report

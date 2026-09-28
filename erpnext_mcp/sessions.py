# SPDX-License-Identifier: MIT
"""Templated inspection sessions: the visit as a shape of data, not a shape of code.

WHY THIS MODULE EXISTS. A worker experiences one visit — walk into MC-Cabin-01,
do everything the cabin needs, walk out. Compliance sees three regulated cadences
that MUST stay separate: a Housing Inspection is annual under 29 CFR 1910.142, a
Detector Test is on the fire code's cycle, a Water Test is Subpart E's ninety
days. Merging them into one record would make one record due on three schedules
at once, and the first quarterly detector test between two annual walks would
have nowhere to go.

So: **the UX is grouped and the records stay separate.** An Inspection Session is
the afternoon. The records it produces are the register, and they are produced
exactly as they would have been if the worker had made three trips — same
doctypes, same registers advanced, same alerts dismissed, same audit packet rows.
What changes is that the photographs and the signature are captured once, and an
auditor can ask which visit produced a given Housing Inspection and get an answer.

────────────────────────────────────────────────────────────────────────────
TEMPLATES ARE DATA. THIS IS THE LOAD-BEARING CLAIM.
────────────────────────────────────────────────────────────────────────────

A template is a Frappe record. `create_inspection_template` writes one and it is
live: it reaches the handset on the next fetch, it can be matched by the rule
engine on the next sweep, and no line of code, no DocType JSON and no TestFlight
build was involved. Adding a template is adding a ROW.

That is the same argument the Configurable Compliance Framework makes about
rules, one layer up, and it is made for the same reason: regulations move. OR-OSHA
renumbered the heat rule. OTCO added a Fraud Prevention Plan. A system whose
response to "the close-down checklist now needs the propane logged" is a code
release is a system that responds in weeks.

────────────────────────────────────────────────────────────────────────────
THE RUNTIME IS DETERMINISTIC. NO AI IN THE TRIGGER PATH.
────────────────────────────────────────────────────────────────────────────

`match_template` picks a template by set inclusion over the compliance record
doctypes a location's pending alerts are asking for, tie-broken by two integers
and a docname. No model, no natural-language interpretation, no probability.
For every session an auditor questions, the answer traces to: these alerts were
open at this place, this template's sections produce a superset of the records
they asked for, it was the tightest fit, here is its version.

AI's role is at AUTHORING time — drafting a template from a regulation for a
human to review and enable — and `propose_inspection_template_from_regulation`
carries it. Declared and refusing in v0.21.0, wired in v0.37.0, and it calls no
model: the proposer is the MCP client, and the tool is the validator and the gate
that lands the draft INACTIVE with `AI-proposed` and its source on the record.
`approve_inspection_template` is the only door from there to a handset, and it
writes the approver's name onto the row. See `erpnext_mcp/proposals.py`.

────────────────────────────────────────────────────────────────────────────
VERSIONING IS BY COPY, AND IT IS WHY A SESSION STAYS READABLE
────────────────────────────────────────────────────────────────────────────

`update_inspection_template` does NOT edit the row. It writes a NEW row at
version+1 carrying the changes, sets the old row's `superseded_by` to the new
one and deactivates the old row. A session links the row it was worked from, so:

  * a template edited in July does not retroactively change how April's session
    reads — April's row is still on the site, with its sections as they were;
  * a session started against v1 while v2 is being authored is unaffected,
    because v2 is a different document and v1 was never touched. There is no
    window in which a running session's definition changes underneath it;
  * `template_version` on the session states the number without anybody having
    to open the template, which is what makes a printed packet self-contained.

The cost is that `template_name` cannot be unique at the schema level — v1 and
v2 share a name by construction. What is enforced instead, in the controller, is
that only one LIVE row (active, not superseded) may hold a name.

────────────────────────────────────────────────────────────────────────────
TWO SECTIONS THAT PRODUCE THE SAME RECORD FOR THE SAME SUBJECT PRODUCE ONE
────────────────────────────────────────────────────────────────────────────

`Detector Test` holds both a smoke result and a CO result, and both are required
fields. A template that tests them as two sections — which is the right shape for
the worker, who walks to one detector and then to the other — cannot produce two
Detector Test records for one cabin on one day without each of them asserting
something it was never told about the other detector. Two records that
contradict each other is the failure this whole app exists to prevent.

So `merge_key` groups section submissions by (produced doctype, resolved
subject), and a group produces ONE record whose payload is the sections' layered
in order. Both section submissions link it. Two sections of work, one compliance
record, and the trail from either section to the record is intact.

The alternative — one record per section — was rejected rather than overlooked.
"""

from __future__ import annotations

import json

import frappe

from . import compat, proposals
from . import training as regimes_vocabulary

TEMPLATE_DOCTYPE = "Inspection Template"
SECTION_DOCTYPE = "Inspection Template Section"
SESSION_DOCTYPE = "Inspection Session"
EVIDENCE_DOCTYPE = "Inspection Session Evidence"
SUBMISSION_DOCTYPE = "Inspection Session Section Submission"

#: The session states, in the order a visit moves through them.
STATE_DRAFT = "Draft"
STATE_IN_PROGRESS = "In Progress"
STATE_SUBMITTED = "Submitted"
STATE_REVIEWED = "Reviewed"
STATE_SUPERSEDED = "Superseded"

SESSION_STATES = (
	STATE_DRAFT,
	STATE_IN_PROGRESS,
	STATE_SUBMITTED,
	STATE_REVIEWED,
	STATE_SUPERSEDED,
)

#: States in which a session has not yet written its compliance records. These
#: are the ones the rule engine treats as already answering an alert, because a
#: submitted session's records have moved the registers and the alerts it
#: answered will dismiss themselves on the next sweep.
OPEN_SESSION_STATES = (STATE_DRAFT, STATE_IN_PROGRESS)

#: What a section's `renderer_hint` may be. A HINT, not a contract — a client
#: that does not know one falls back to a freeform form and the submission is
#: still valid, which is what lets a template using a renderer added later reach
#: a handset that has not been updated.
RENDERER_HINTS = (
	"photo",
	"signature",
	"checklist",
	"freeform",
	"gps",
	"timestamp",
	"multi-photo",
	"measurement",
	"attachment",
)

#: The evidence contract's vocabulary. The first four are Farm Task's own keys,
#: deliberately spelled identically — a session's contract and a task's contract
#: are the same kind of promise, and a second spelling of "signature" would be a
#: second thing to check. The last two are what a sectioned form adds.
CONTRACT_KEYS = {
	"photos": "at least one photograph filed against this section",
	"signature": "a signature capture — the worker attesting to what they found",
	"findings_text": "what they actually saw, in words, whether or not anything was wrong",
	"witness": "the name of somebody else who was there and saw the same thing",
	"checklist_items": "every named checklist item ticked, by key",
	"measurements": "every named measurement recorded, by key",
}

#: What `applies_to_asset_type` may say. The first three name real registers on
#: this site and are ENFORCED against a session's `location_doctype`; the last
#: three are labels for an operator and are not, because refusing a location on
#: the strength of a word this app keeps no register behind would be a refusal it
#: cannot justify.
ASSET_TYPES = ("Housing Unit", "Field", "Irrigation Zone", "Sprayer", "Cabin", "General")

#: The subset the rule engine will match automatically. A "Cabin" template is
#: almost certainly about a Housing Unit and the engine still will not assume it:
#: an automatic bundling of somebody's compliance work is exactly the place not
#: to guess. Set `applies_to_asset_type` to the register's own name and it
#: matches; leave it as a label and the template stays spawnable by hand.
MATCHABLE_ASSET_TYPES = ("Housing Unit", "Field", "Irrigation Zone")

#: Most sections one template may carry. A visit with more parts than this is not
#: a visit, it is a day, and a form nobody finishes is worse than three forms.
SECTION_CAP = 40

#: Most sessions one read returns.
SESSION_CAP = 500

#: Most evidence rows one session's tray may hold. Forty photographs of one cabin
#: is a thorough visit; four hundred is a camera roll. The same cap
#: `tools/inspections.py` applies per record, applied here per visit.
EVIDENCE_CAP = 120


# ── JSON blobs ──────────────────────────────────────────────────────────────
def as_object(raw, label: str) -> dict:
	"""A JSON object from a string, a dict, or nothing. Anything else is refused.

	Same contract as `farm_task.parse_json_object`, reimplemented here rather
	than imported because this module is loaded by the doctype controllers and
	importing a sibling controller for one function is a circular-import waiting
	for the first person who adds a second one.
	"""
	if raw in (None, ""):
		return {}
	if isinstance(raw, dict):
		return dict(raw)
	try:
		value = json.loads(raw)
	except Exception as exc:
		raise ValueError(f"{label} is not valid JSON: {str(raw)[:200]}") from exc
	if not isinstance(value, dict):
		raise ValueError(f"{label} must be a JSON object, got {type(value).__name__}.")
	return value


def as_list(raw, label: str) -> list:
	"""A JSON list from a string, a list, or nothing."""
	if raw in (None, ""):
		return []
	if isinstance(raw, (list, tuple)):
		return list(raw)
	try:
		value = json.loads(raw)
	except Exception as exc:
		raise ValueError(f"{label} is not valid JSON: {str(raw)[:200]}") from exc
	if not isinstance(value, list):
		raise ValueError(f"{label} must be a JSON list, got {type(value).__name__}.")
	return value


def check_contract(contract: dict, label: str) -> dict:
	"""Refuse a contract key outside the vocabulary, and normalise the two lists.

	A key outside the set is REFUSED rather than ignored, for the reason Farm
	Task refuses one: `{"photo": true}` where `"photos"` was meant asks for
	nothing and looks like it asks for something, which is the worst of both.
	"""
	out = {}
	for key, value in (contract or {}).items():
		if key not in CONTRACT_KEYS:
			raise ValueError(
				f"{label} names {key!r}, which is not an evidence contract key. "
				f"The vocabulary is: {', '.join(sorted(CONTRACT_KEYS))}. A key outside it asks for "
				"nothing and looks like it asks for something."
			)
		if key in ("checklist_items", "measurements"):
			if value in (None, "", False):
				continue
			if not isinstance(value, (list, tuple)):
				raise ValueError(f"{label}.{key} must be a list of names, got {type(value).__name__}.")
			names = [str(entry).strip() for entry in value if str(entry).strip()]
			if names:
				out[key] = names
			continue
		out[key] = bool(value)
	return out


def unmet(contract: dict, submission: dict) -> list:
	"""What this submission is short of, in sentences. Empty means it is complete.

	READ AGAINST THE SECTION, NOT THE VISIT. A signature given once for the whole
	trip satisfies every section that asks for one — the tray is shared and the
	caller passes the same file reference against each — but a section asking for
	a photograph is not satisfied by a photograph of somewhere else, which is why
	the check is per section and the tray carries a `section_name`.
	"""
	missing = []
	contract = contract or {}
	if contract.get("photos") and not submission.get("photos"):
		missing.append(CONTRACT_KEYS["photos"])
	if contract.get("signature") and not submission.get("signature"):
		missing.append(CONTRACT_KEYS["signature"])
	if contract.get("findings_text") and submission.get("notes") is None:
		# `is None` and not falsiness: an EMPTY STRING is a positive statement
		# that nothing was wrong, and it is the commonest answer. Treating it as
		# absent would refuse every clean section.
		missing.append(CONTRACT_KEYS["findings_text"])
	if contract.get("witness") and not str(submission.get("witness") or "").strip():
		missing.append(CONTRACT_KEYS["witness"])
	for key, label in (("checklist_items", "checklist"), ("measurements", "measurement")):
		wanted = contract.get(key) or []
		given = submission.get(key) or {}
		absent = [name for name in wanted if name not in given]
		if absent:
			missing.append(f"the {label} value(s) {', '.join(absent)}")
	return missing


# ── templates ───────────────────────────────────────────────────────────────
TEMPLATE_FIELDS = (
	"name",
	"template_name",
	"description",
	"applies_to_asset_type",
	"skill_required",
	"estimated_duration_minutes",
	"cadence_trigger_expression",
	"regulation_citations",
	"active",
	"version",
	"superseded_by",
	"authored_by",
	"ai_source_citation",
	"ai_review_flags",
	"human_approved_by",
	"human_approved_on",
	"creation",
	"modified",
	"owner",
)

SECTION_FIELDS = (
	"name",
	"section_name",
	"section_description",
	"order_index",
	"produces_record_doctype",
	"renderer_hint",
	"evidence_contract_json",
	"produces_record_data_json",
	"field_prompts_json",
	"required",
	"idx",
)


def template_row(name: str) -> dict:
	"""One template's own fields, or {}."""
	if not (name and compat.doctype_exists(TEMPLATE_DOCTYPE)):
		return {}
	row = frappe.db.get_value(
		TEMPLATE_DOCTYPE, name, compat.existing_fields(TEMPLATE_DOCTYPE, TEMPLATE_FIELDS), as_dict=True
	)
	return dict(row or {})


def sections_of(name: str) -> list:
	"""One template's sections, in working order.

	Ordered by `order_index` and then by `idx`, so a template whose author left
	the index alone still comes back in the order they typed it.
	"""
	if not (name and compat.doctype_exists(SECTION_DOCTYPE)):
		return []
	rows = frappe.db.get_all(
		SECTION_DOCTYPE,
		filters={"parent": name, "parenttype": TEMPLATE_DOCTYPE, "parentfield": "sections"},
		fields=compat.existing_fields(SECTION_DOCTYPE, SECTION_FIELDS),
		order_by="order_index asc, idx asc",
		limit=SECTION_CAP * 2,
	)
	return [dict(row) for row in rows or []]


def as_prompts(raw, label: str):
	"""`field_prompts`: a list of form fields (v0.204.0, validated) or the legacy dict.

	docs/design/form_schema_and_labels.md §1.7. A list the phone cannot render is
	refused with every error; the legacy `{key: {type, label, label_es}}` dict is
	still read.
	"""
	from . import form_schema

	if isinstance(raw, str) and raw.strip().startswith("["):
		try:
			raw = json.loads(raw)
		except ValueError as exc:
			raise ValueError(f"{label} is not valid JSON: {str(raw)[:200]}") from exc
	if isinstance(raw, list):
		try:
			return form_schema.require_valid(raw)
		except form_schema.SchemaError as exc:
			raise ValueError(f"{label} cannot be rendered on the phone: {exc}") from exc
	return as_object(raw, label)


def section_fields(section: dict) -> list:
	"""A section's prompts as form fields, whichever shape they were stored in."""
	from . import form_schema

	try:
		return form_schema.as_fields(section.get("field_prompts"))
	except form_schema.SchemaError:
		return []


def describe_section(row: dict) -> dict:
	"""One section as a client reads it, with both blobs already parsed."""
	try:
		contract = as_object(row.get("evidence_contract_json"), "evidence_contract")
	except ValueError:
		contract = {}
	try:
		prompts = as_prompts(row.get("field_prompts_json"), "field_prompts")
	except ValueError:
		prompts = {}
	try:
		defaults = as_object(row.get("produces_record_data_json"), "produces_record_data")
	except ValueError:
		defaults = {}
	return {
		"section_name": row.get("section_name"),
		"section_description": row.get("section_description") or None,
		"order_index": int(row.get("order_index") or 0),
		"produces_record_doctype": row.get("produces_record_doctype") or None,
		"renderer_hint": row.get("renderer_hint") or "freeform",
		"required": compat.checked(row.get("required")),
		"evidence_contract": contract,
		"produces_record_data": defaults,
		"field_prompts": prompts,
	}


def produced_doctypes(name: str) -> set:
	"""Every compliance doctype this template's sections can produce."""
	return {
		str(row.get("produces_record_doctype") or "").strip()
		for row in sections_of(name)
		if str(row.get("produces_record_doctype") or "").strip()
	}


def live_template(template_name: str) -> str | None:
	"""The docname of the one LIVE row holding this template name, or None.

	Live means active and not superseded. There is at most one by construction —
	the controller refuses a second — and the ordering is a belt to that brace so
	a site that somehow grew two gets the newest version rather than an arbitrary
	one.
	"""
	if not (template_name and compat.doctype_exists(TEMPLATE_DOCTYPE)):
		return None
	rows = frappe.db.get_all(
		TEMPLATE_DOCTYPE,
		filters={"template_name": template_name, "active": 1, "superseded_by": ("in", ("", None))},
		fields=["name", "version"],
		order_by="version desc, name desc",
		limit=2,
	)
	return str(rows[0]["name"]) if rows else None


def resolve_template(reference: str) -> str | None:
	"""A template docname from a docname or a template name.

	A DOCNAME WINS. `INSPT-2026-0001` names one exact version and is what a
	session pins; a template name means "whichever version is live", which is
	what somebody starting a visit today means. Both spellings are accepted
	because both are things a caller genuinely has.
	"""
	reference = str(reference or "").strip()
	if not reference:
		return None
	if compat.doctype_exists(TEMPLATE_DOCTYPE) and frappe.db.exists(TEMPLATE_DOCTYPE, reference):
		return reference
	return live_template(reference)


def regimes_of(name: str) -> list:
	"""The regime tokens on one template."""
	if not compat.doctype_exists(regimes_vocabulary.REGIME_LINK_DOCTYPE):
		return []
	rows = frappe.db.get_all(
		regimes_vocabulary.REGIME_LINK_DOCTYPE,
		filters={"parent": name, "parenttype": TEMPLATE_DOCTYPE, "parentfield": "regimes"},
		fields=["regime"],
		order_by="idx asc",
		limit=50,
	)
	return regimes_vocabulary.from_rows(rows)


# ── matching, and it is set inclusion and two integers ──────────────────────
def match_template(location_doctype: str, wanted: set, company: str = "") -> dict:
	"""The one template whose sections cover every wanted record doctype, or {}.

	    THE RULE, IN FULL, SO IT CAN BE ARGUED WITH:

	  1. Candidates are ACTIVE templates whose `applies_to_asset_type` is
	     exactly the location's register. A label — Sprayer, Cabin, General — is
	     never matched automatically. See `MATCHABLE_ASSET_TYPES`.
	  2. A candidate COVERS the location if the set of doctypes its sections
	     produce is a SUPERSET of `wanted`. Superset and not equality: a template
	     that also does a water test is still the right template for a cabin
	     whose water is not due, and the worker marks that section skipped.
	  3. Ties are broken by (extra sections beyond what was wanted, total
	     sections, docname). Tightest fit first, so "Mid-season Habitability"
	     beats "Pre-season Cabin Opening" for a cabin needing a walk and a
	     detector test, and the docname makes it total — two templates that are
	     genuinely equivalent resolve the same way on every run and on every
	     site, which is what makes the sweep reproducible.

	NO MATCH IS A FIRST-CLASS ANSWER and the ordinary one: one alert at a place
	is one task, and a place whose pending alerts nothing covers gets the
	unchanged per-alert path. Returning {} rather than a best-effort partial is
	the whole of the deterministic claim — a session that covers three of four
	overdue things, silently, would leave the fourth answered by nothing.
	"""
	if not wanted or not compat.doctype_exists(TEMPLATE_DOCTYPE):
		return {}
	if str(location_doctype or "") not in MATCHABLE_ASSET_TYPES:
		return {}

	filters = {"active": 1, "applies_to_asset_type": location_doctype}
	rows = frappe.db.get_all(
		TEMPLATE_DOCTYPE,
		filters=filters,
		fields=["name", "template_name", "version", "skill_required", "estimated_duration_minutes"],
		order_by="name asc",
		limit=SECTION_CAP * 10,
	)
	best = None
	for row in rows or []:
		sections = sections_of(row["name"])
		if not sections:
			continue
		produced = {
			str(section.get("produces_record_doctype") or "").strip()
			for section in sections
			if str(section.get("produces_record_doctype") or "").strip()
		}
		if not wanted.issubset(produced):
			continue
		extras = len(produced - wanted)
		rank = (extras, len(sections), str(row["name"]))
		if best is None or rank < best[0]:
			best = (rank, row, sections, produced)
	if best is None:
		return {}
	_rank, row, sections, produced = best
	return {
		"template": str(row["name"]),
		"template_name": row.get("template_name"),
		"version": int(row.get("version") or 1),
		"skill_required": row.get("skill_required") or "",
		"estimated_duration_minutes": int(row.get("estimated_duration_minutes") or 0),
		"section_count": len(sections),
		"produces": sorted(produced),
		"covers": sorted(wanted),
		"extra_sections": sorted(produced - wanted),
	}


# ── the alerts a session already answers ────────────────────────────────────
def alert_names(raw) -> list:
	"""The alert docnames on a session, parsed from the stored text.

	SPLIT AND COMPARED AS WHOLE TOKENS, NEVER AS A SUBSTRING. `training.matches`
	makes the same point about regimes and for the same reason: an alert docname
	is a prefix of the next one often enough that a LIKE would answer yes about
	the wrong record, and the consequence here is a compliance alert silently
	treated as answered.
	"""
	if not raw:
		return []
	if isinstance(raw, (list, tuple)):
		pieces = [str(entry) for entry in raw]
	else:
		pieces = str(raw).replace(",", "\n").split("\n")
	out = []
	for piece in pieces:
		name = piece.strip()
		if name and name not in out:
			out.append(name)
	return out


def alerts_answered_by_open_sessions(company: str = "") -> dict:
	"""alert docname → the session (and its task) that already answers it.

	SCOPED TO THE STATES IN WHICH THE WORK IS STILL OUTSTANDING, which is what
	keeps the query small on a site that has run for seasons. A SUBMITTED session
	has written its compliance records; those moved the registers; the alerts it
	answered dismiss themselves on the next sweep and never reach the generator,
	which only ever looks at alerts that are still open.
	"""
	if not compat.doctype_exists(SESSION_DOCTYPE):
		return {}
	filters = {"state": ("in", list(OPEN_SESSION_STATES)), "source_alerts": ("is", "set")}
	if company:
		filters["company"] = company
	rows = frappe.db.get_all(
		SESSION_DOCTYPE,
		filters=filters,
		fields=["name", "farm_task", "source_alerts", "template", "location"],
		limit=SESSION_CAP * 2,
	)
	out = {}
	for row in rows or []:
		for alert in alert_names(row.get("source_alerts")):
			out[alert] = {
				"session": str(row["name"]),
				"task": str(row.get("farm_task") or "") or None,
				"template": row.get("template"),
				"location": row.get("location"),
			}
	return out


# ── merging, so two sections cannot file two contradictory records ──────────
def merge_key(doctype: str, subject: str) -> tuple:
	"""What makes two section submissions one compliance record. See the module docstring."""
	return (str(doctype or ""), str(subject or ""))


# ── the seeded templates ────────────────────────────────────────────────────
#: The four shapes of visit an Oregon tree-fruit operation already runs, seeded
#: on install so the first session has something to be worked from. Stated as
#: data here for the same reason the whole feature is data: the day one of them
#: is wrong, the fix is an `update_inspection_template` call on a live site, not
#: this tuple and a release.
#:
#: NOT A FRAPPE `fixtures` ENTRY — `test_hooks.py` forbids the word by name, and
#: the reason is exactly this feature's reason. A fixture is imported by `bench
#: migrate` with no ability to skip what a site already has, so an operator who
#: added a section to their close-down would get it silently removed on the next
#: upgrade, and the first anybody would know is a winter with no propane check.
#:
#: v0.203.0. The two rodent bait sections (docs/design/rodent_bait_program.md
#: §4.2–§4.3) are defined once here, used by the seed below and by the patch
#: `add_rodent_sections_to_inspection_templates` for sites seeded before them.
RODENT_BAIT_CLEARED_SECTION = {
	"section_name": "Rodent bait cleared",
	"section_description": (
		"Before anybody moves in: no rodent bait left inside, and no fresh signs of rodents. The "
		"server checks this against the bait record — it passes only when every interior bait "
		"round here ended in a COMPLETED Rodent Bait Removal and Clearance, whatever is ticked."
	),
	"produces_record_doctype": "Housing Inspection",
	"renderer_hint": "checklist",
	# OPTIONAL so a handset that has never seen this section still files the
	# visit. The server judges the clearance whether or not it is submitted.
	"required": 0,
	"evidence_contract": {"checklist_items": ["rodent_bait_cleared"]},
	"field_prompts": {
		"rodent_bait_cleared": {
			"type": "boolean",
			"label": "Bait cleared and no rodent signs inside",
			"label_es": "Cebo retirado y sin señales de roedores adentro",
		}
	},
}
RODENT_ACTIVITY_SECTION = {
	"section_name": "Rodent activity",
	"section_description": (
		"Droppings, gnaw marks, burrows by the foundation, nesting, or a rodent seen. Yes raises a "
		"rodent bait placement task for this building."
	),
	"produces_record_doctype": "",
	"renderer_hint": "checklist",
	# OPTIONAL so a handset that has never seen this section still files the visit.
	"required": 0,
	"evidence_contract": {"checklist_items": ["rodent_activity_seen"]},
	"field_prompts": {
		"rodent_activity_seen": {
			"type": "boolean",
			"label": "Rodent activity seen?",
			"label_es": "¿Se vio actividad de roedores?",
		}
	},
}
#: template_name → the rodent section it carries.
RODENT_SECTIONS = {
	"Pre-season Cabin Opening": RODENT_BAIT_CLEARED_SECTION,
	"Mid-season Habitability": RODENT_ACTIVITY_SECTION,
}
#: `seed_inspection_templates` checks before it writes and leaves an edited
#: template exactly as it is, including one somebody deactivated.
SEED_TEMPLATES = (
	{
		"template_name": "Pre-season Cabin Opening",
		"applies_to_asset_type": "Housing Unit",
		"skill_required": "camp_maintenance",
		"estimated_duration_minutes": 120,
		"description": (
			"Everything a cabin needs before anybody sleeps in it: the habitability walk, both "
			"detectors, the water supply, and the readiness check that is not a regulated record "
			"and is the one an operation is judged on anyway. Worked once, in one trip, before the "
			"crew arrives."
		),
		"regulation_citations": "OAR 437-004-1120; 29 CFR 1910.142; FSMA Subpart L",
		"regimes": ("OR-OSHA", "FSMA"),
		"sections": (
			{
				"section_name": "Habitability walk",
				"section_description": (
					"Walk the whole cabin. Floors, walls, windows, doors, roof, plumbing, wiring, "
					"vermin. Write what you actually saw — an empty findings box is a positive "
					"statement that it was clean, and it is the commonest answer."
				),
				"produces_record_doctype": "Housing Inspection",
				"renderer_hint": "multi-photo",
				"required": 1,
				"evidence_contract": {
					"photos": True,
					"signature": True,
					"findings_text": True,
					"checklist_items": ["structure_sound", "windows_screens_intact", "plumbing_works"],
				},
			},
			{
				"section_name": "Smoke Detector Test",
				"section_description": (
					"Press the test button on every smoke detector. Not present is a finding, not "
					"an omission."
				),
				"produces_record_doctype": "Detector Test",
				"renderer_hint": "checklist",
				"required": 1,
				"evidence_contract": {"photos": True, "checklist_items": ["smoke_alarm_sounds"]},
			},
			{
				"section_name": "CO Detector Test",
				"section_description": (
					"Press the test button on the carbon monoxide detector. A cabin heated with "
					"propane and no working CO detector is the most dangerous state this app records."
				),
				"produces_record_doctype": "Detector Test",
				"renderer_hint": "checklist",
				"required": 1,
				"evidence_contract": {"photos": True, "checklist_items": ["co_alarm_sounds"]},
			},
			{
				"section_name": "Water Supply Test",
				"section_description": (
					"Sample the supply and send it to the laboratory. OPTIONAL, and name the "
					"Irrigation Zone the sample came from in record_data — a cabin can draw from "
					"more than one source and this app will not guess which."
				),
				"produces_record_doctype": "Water Test",
				"renderer_hint": "measurement",
				"required": 0,
				"evidence_contract": {"photos": True, "measurements": ["chlorine_ppm"]},
			},
			{
				"section_name": "Cabin Readiness",
				"section_description": (
					"Beds, lighting, cleanliness. Produces no compliance record — nobody regulates "
					"it as its own document, and a photograph of a made bed is still what a "
					"returning crew is entitled to."
				),
				"produces_record_doctype": "",
				"renderer_hint": "multi-photo",
				"required": 1,
				"evidence_contract": {
					"photos": True,
					"checklist_items": ["beds_made", "lighting_works", "cabin_clean"],
				},
			},
			RODENT_BAIT_CLEARED_SECTION,
		),
	},
	{
		"template_name": "Mid-season Habitability",
		"applies_to_asset_type": "Housing Unit",
		"skill_required": "camp_maintenance",
		"estimated_duration_minutes": 60,
		"description": (
			"The in-season walk and detector check on an occupied cabin. The template the rule "
			"engine reaches for when a habitability inspection and a detector test come due at the "
			"same place — one trip instead of two."
		),
		"cadence_trigger_expression": (
			"housing_inspection_overdue AND housing_detector_test_stale for the same location"
		),
		"regulation_citations": "OAR 437-004-1120; 29 CFR 1910.142",
		"regimes": ("OR-OSHA",),
		"sections": (
			{
				"section_name": "Habitability walk",
				"section_description": "The in-season walk of an occupied cabin.",
				"produces_record_doctype": "Housing Inspection",
				"renderer_hint": "multi-photo",
				"required": 1,
				"evidence_contract": {"photos": True, "signature": True, "findings_text": True},
			},
			{
				"section_name": "Smoke Detector Test",
				"section_description": "Press the test button on every smoke detector.",
				"produces_record_doctype": "Detector Test",
				"renderer_hint": "checklist",
				"required": 1,
				"evidence_contract": {"checklist_items": ["smoke_alarm_sounds"]},
			},
			{
				"section_name": "CO Detector Test",
				"section_description": "Press the test button on the carbon monoxide detector.",
				"produces_record_doctype": "Detector Test",
				"renderer_hint": "checklist",
				"required": 1,
				"evidence_contract": {"checklist_items": ["co_alarm_sounds"]},
			},
			RODENT_ACTIVITY_SECTION,
		),
	},
	{
		"template_name": "Post-harvest Cabin Close-down",
		"applies_to_asset_type": "Housing Unit",
		"skill_required": "camp_maintenance",
		"estimated_duration_minutes": 90,
		"description": (
			"Shutting a cabin for the winter: the refrigerator and the food storage emptied, the "
			"heater off and the propane disconnected, a final detector test and a final walk. Most "
			"of it produces no regulated record and all of it is what stops a cabin being opened in "
			"April to a burst pipe and a fridge somebody left plugged in."
		),
		"regulation_citations": "OAR 437-004-1120; internal cabin close-down SOP",
		"regimes": ("OR-OSHA", "Internal"),
		"sections": (
			{
				"section_name": "Refrigerator Empty Check",
				"section_description": "Photograph the refrigerator with the door open. Empty, cleaned, unplugged.",
				"produces_record_doctype": "",
				"renderer_hint": "photo",
				"required": 1,
				"evidence_contract": {
					"photos": True,
					"checklist_items": ["fridge_empty", "fridge_cleaned", "fridge_unplugged"],
				},
			},
			{
				"section_name": "Food Storage Empty",
				"section_description": "Photograph the pantry and cabinets. Nothing left to attract vermin over the winter.",
				"produces_record_doctype": "",
				"renderer_hint": "photo",
				"required": 1,
				"evidence_contract": {"photos": True, "checklist_items": ["pantry_empty", "cabinets_empty"]},
			},
			{
				"section_name": "Heater Status",
				"section_description": "Photograph the heater switched off, and the propane disconnected at the tank.",
				"produces_record_doctype": "",
				"renderer_hint": "photo",
				"required": 1,
				"evidence_contract": {
					"photos": True,
					"checklist_items": ["heater_off", "propane_disconnected", "gas_shut_off"],
				},
			},
			{
				"section_name": "Detector Test — Smoke + CO",
				"section_description": (
					"One final test of both detectors before the cabin is shut. A detector that "
					"fails now is fitted in April by somebody who was told in October."
				),
				"produces_record_doctype": "Detector Test",
				"renderer_hint": "checklist",
				"required": 1,
				"evidence_contract": {"checklist_items": ["smoke_alarm_sounds", "co_alarm_sounds"]},
			},
			{
				"section_name": "Winterization Checklist",
				"section_description": "Water lines drained, propane disconnected, windows secured, doors locked.",
				"produces_record_doctype": "",
				"renderer_hint": "checklist",
				"required": 1,
				"evidence_contract": {
					"checklist_items": [
						"water_lines_drained",
						"propane_disconnected",
						"windows_secured",
						"doors_locked",
					]
				},
			},
			{
				"section_name": "Final Habitability Walk",
				"section_description": "The season-end record of what condition the cabin was left in.",
				"produces_record_doctype": "Housing Inspection",
				"renderer_hint": "multi-photo",
				"required": 1,
				"evidence_contract": {"photos": True, "signature": True, "findings_text": True},
			},
		),
	},
	{
		"template_name": "Spray Day Inspection",
		"applies_to_asset_type": "Sprayer",
		"skill_required": "applicator",
		"estimated_duration_minutes": 30,
		"description": (
			"The pre-application check on the sprayer and its operator: personal protective "
			"equipment worn, tank clean, product and rate written down before the first row. "
			"Applies to the machine rather than to a block, which is why it is not matched "
			"automatically by the rule engine — this app keeps no sprayer register to match against."
		),
		"regulation_citations": "40 CFR 170.401 WPS handler training; product-specific EPA labels",
		"regimes": ("WPS", "OR-OSHA"),
		"sections": (
			{
				"section_name": "Applicator PPE Check",
				"section_description": (
					"Photograph the applicator in the PPE the label requires. The photograph is the "
					"evidence; the checklist is what it is checked against."
				),
				"produces_record_doctype": "",
				"renderer_hint": "photo",
				"required": 1,
				"evidence_contract": {
					"photos": True,
					"checklist_items": ["respirator", "coveralls", "gloves", "eye_protection"],
				},
			},
			{
				"section_name": "Tank Cleanliness",
				"section_description": "Photograph the tank empty and rinsed before it is filled.",
				"produces_record_doctype": "",
				"renderer_hint": "photo",
				"required": 1,
				"evidence_contract": {"photos": True, "checklist_items": ["tank_empty", "tank_rinsed"]},
			},
			{
				"section_name": "Product + Rate Recording",
				"section_description": (
					"Product, EPA registration number, rate, and the REI and PHI off the label. "
					"PRODUCES NO RECORD IN v0.21.0 — there is no Spray Record doctype on this site "
					"yet, and a section pointing at a doctype that does not exist would refuse "
					"every submission. It is captured as evidence and as measurements; the day the "
					"doctype ships, one update_inspection_template call points this section at it "
					"and every session worked before then is still readable against the version it "
					"was worked from."
				),
				"produces_record_doctype": "",
				"renderer_hint": "freeform",
				"required": 1,
				"evidence_contract": {
					"findings_text": True,
					"measurements": ["rate_per_acre", "rei_hours", "phi_days"],
				},
			},
		),
	},
)


def seed_inspection_templates() -> dict:
	"""One Inspection Template per entry in `SEED_TEMPLATES`. Idempotent.

	CHECKED BY NAME, AND AN EDITED TEMPLATE IS LEFT ALONE. The check is "does any
	row hold this template_name" and not "does a LIVE row" — deliberately, and it
	is the difference between this seeder and a fixture: a template somebody
	deactivated because their operation does not do it that way stays deactivated,
	and one somebody superseded with their own version 2 does not get version 1
	seeded back beside it every migrate.

	NEVER RAISES. It runs inside `bench migrate`, where an exception aborts the
	migration for the whole bench. Everything it could not do lands in `failed`
	and is printed by `install._report_failures`.
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
			# `System`, because a template this app ships is answerable to whoever
			# shipped it. v0.37.0 gave the DocType the same three-word provenance
			# the Compliance Rule carries, and a seeded row reading `Operator`
			# would put the words in somebody's mouth.
			build_template({**spec, "authored_by": proposals.AUTHOR_SYSTEM}).insert(ignore_permissions=True)
			report["created"].append(name)
		except Exception as exc:  # pragma: no cover - reported, never raised
			report["failed"].append({"name": name, "reason": f"{type(exc).__name__}: {exc}"})
	return report


def build_template(spec: dict):
	"""One unsaved Inspection Template document from a plain dict.

	Shared by the seeder and by `create_inspection_template`, so a template
	somebody types through MCP and one this app ships are the same shape of
	record — including the parts a seeder could quietly have skipped.
	"""
	doc = frappe.new_doc(TEMPLATE_DOCTYPE)
	doc.template_name = str(spec.get("template_name") or "").strip()
	doc.description = str(spec.get("description") or "").strip()
	doc.applies_to_asset_type = str(spec.get("applies_to_asset_type") or "General").strip()
	doc.skill_required = str(spec.get("skill_required") or "").strip()
	doc.estimated_duration_minutes = int(spec.get("estimated_duration_minutes") or 0)
	doc.cadence_trigger_expression = str(spec.get("cadence_trigger_expression") or "").strip()
	doc.regulation_citations = str(spec.get("regulation_citations") or "").strip()
	doc.active = 1 if spec.get("active", 1) else 0
	doc.version = int(spec.get("version") or 1)
	# v0.37.0. Provenance is written by the same builder the seeder uses, so a
	# template this app ships, one an operator typed and one a model drafted are
	# the same shape of record — and the difference between them is a field
	# somebody can filter on rather than a thing you have to know.
	doc.authored_by = str(spec.get("authored_by") or proposals.AUTHOR_OPERATOR).strip()
	doc.ai_source_citation = str(spec.get("ai_source_citation") or "").strip()
	doc.ai_review_flags = str(spec.get("ai_review_flags") or "").strip()
	doc.human_approved_by = spec.get("human_approved_by") or None
	doc.human_approved_on = spec.get("human_approved_on") or None
	for regime in regimes_vocabulary.to_rows(spec.get("regimes") or []):
		doc.append("regimes", dict(regime))
	for index, section in enumerate(spec.get("sections") or ()):
		doc.append("sections", _section_row(section, index))
	return doc


def _section_row(section: dict, index: int) -> dict:
	"""One section child row from a plain dict, with the blobs serialised."""
	contract = section.get("evidence_contract")
	if contract is None:
		contract = as_object(section.get("evidence_contract_json"), "evidence_contract")
	prompts = section.get("field_prompts")
	if prompts is None:
		prompts = as_prompts(section.get("field_prompts_json"), "field_prompts")
	defaults = section.get("produces_record_data")
	if defaults is None:
		defaults = as_object(section.get("produces_record_data_json"), "produces_record_data")
	order = section.get("order_index")
	return {
		"section_name": str(section.get("section_name") or "").strip(),
		"section_description": str(section.get("section_description") or "").strip(),
		"order_index": int(order) if order not in (None, "") else index + 1,
		"produces_record_doctype": str(section.get("produces_record_doctype") or "").strip() or None,
		"renderer_hint": str(section.get("renderer_hint") or "checklist").strip(),
		"required": 1 if section.get("required", 1) else 0,
		"evidence_contract_json": json.dumps(check_contract(contract, "evidence_contract")),
		"produces_record_data_json": json.dumps(defaults or {}),
		"field_prompts_json": json.dumps(prompts or {}),
	}

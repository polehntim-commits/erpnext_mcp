# SPDX-License-Identifier: MIT
"""Compliance Rule as data: the vocabulary, the readers, and the seeder.

v0.22.0, and it is the change the whole Configurable Compliance Framework rests
on. Until now a compliance rule was a Python function: changing a threshold,
adding a citation, or turning one off for a season meant a code change, a
release and a deploy. Regulations do not move on a release cadence. OR-OSHA
renumbered heat illness from -1130 to -1131; OTCO added a Fraud Prevention Plan
requirement; FDA re-phased FSMA Produce Safety. Every one of those is a data
change wearing a code change's clothes, and this module is where it takes them
off.

WHAT MOVED, AND WHAT DELIBERATELY DID NOT:

  * THE RULE DEFINITIONS ARE NOW RECORDS. Thresholds, scope, citations,
    regimes, message text, the kairotic gate, the switch — all of it is on a
    Compliance Rule row an operator can edit, and every edit is versioned,
    approved and audit-logged.
  * THE ENGINE IS STILL CODE. `alerts/base.py` sweeps, dedups, upserts and
    auto-dismisses exactly as it did in v0.21.0 — none of that is editable and
    none of it should be.
  * RUNTIME IS DETERMINISTIC. The engine reads the current state of the target
    records and evaluates a declarative expression. There is NO model in the
    trigger path — no classifier, no natural-language interpretation, nothing
    probabilistic. AI's role is confined to AUTHORING: proposing a rule
    definition a human then reads, approves and enables. That is what makes an
    alert defensible: for every one an auditor questions, the answer traces to
    a row, its `regulation_citations`, its `human_approved_by` and
    `human_approved_on`, and the specific field that crossed a threshold.

THREE SHAPES OF RULE, AND THE SPLIT IS THE INTERESTING PART:

  DECLARATIVE     the record says everything: target doctype, date field(s),
                  cadence, thresholds, scope filters, gates, supersession,
                  heuristics, message template. ELEVEN of the thirteen shipped
                  rules are this since v0.22.1, which added the four primitives
                  v0.22.0's notes said the built-ins were waiting for.
  BUILT-IN        the record still owns every tunable, but the SHAPE of the
                  scan is a named scanner that ships with the app, reviewed and
                  tested. TWO of the thirteen, and both are PERMANENT rather
                  than pending: `audit_action_overdue` folds a child table to
                  its worst row, which is an aggregation rather than a filter,
                  and `supervisor_review_lapsed` walks a table of doctypes on a
                  clock that measures days ELAPSED where every other rule's
                  thresholds mean days REMAINING.
  CUSTOM PYTHON   a restricted program in a field. ZERO of the thirteen use it.
                  It exists for the rule an operator or an AI proposer writes
                  that the primitives do not reach yet, and every use of it is
                  a request for a primitive — see `docs/configurable_compliance_framework.md`.

That ordering is the design. `custom_python` is an escape hatch, not a
load-bearing wall: it runs in `alerts/sandbox.py`, which is an AST interpreter
rather than an `exec`, and the thing to do with a rule that needs it is to grow
the declarative vocabulary until it does not.

SEEDED, NOT FIXTURED. `test_hooks.py` forbids the word `fixtures` by name, and
for the reason it always has: a Frappe fixture is imported by `bench migrate`
with no ability to skip what a site already has, so an operator who raised a
threshold would have it corrected back on the next upgrade. `seed_compliance_rules`
checks for the rule_id BEFORE it writes, so a rule somebody edited, superseded
or disabled stays that way across every future migrate.
"""

from __future__ import annotations

import json
import re

import frappe

from . import compat, proposals
from . import training as regimes_vocabulary

DOCTYPE = "Compliance Rule"

#: The three shapes above, as the value `describe` reports and tests assert on.
SHAPE_DECLARATIVE = "declarative"
SHAPE_BUILTIN = "builtin_scanner"
SHAPE_CUSTOM = "custom_python"

#: What `missing_date_behaviour` may say.
ON_MISSING_SKIP = "Skip"
ON_MISSING_RAISE = "Raise"

#: What `due_date_mode` may say.
DUE_FROM_ANCHOR = "From Anchor"
DUE_TODAY = "Today"
DUE_NONE = "None"

#: v0.22.1. What `date_field_role` may say, and it is the smallest of the new
#: fields and the one that unlocked the two supersession rules.
#:
#: `Clock` is every rule up to now: the date is a DEADLINE, and how far away it
#: is decides the severity band. `Timestamp` says the date is WHEN THE THING WAS
#: FOUND — a finding is not more or less true for being three weeks old, so the
#: date is read for the message and for the supersession test and bands nothing.
#: Every matching row raises `severity_expired`, and a row with no date raises
#: too, because a corrective action nobody dated is still open.
DATE_ROLE_CLOCK = "Clock"
DATE_ROLE_TIMESTAMP = "Timestamp"

#: v0.22.5. The third role, and the one that made `default_severity` necessary.
#:
#: `State` says the rule fires on a DATA STATE rather than on any distance from a
#: date. Its gates decide whether it fires; the date field is read for the
#: message and bands nothing; and the severity is `default_severity` rather than
#: any of the three band severities, because a rule with no clock has no band to
#: be in and no expiry to be past.
#:
#: IT IS A THIRD VALUE AND NOT A FOURTH THRESHOLD, and the distinction is the
#: whole reason this field exists rather than a convention. `threshold_*_days`
#: are Int columns, so "no threshold" and "a threshold of zero" are one value in
#: the database — and zero is a real setting meaning "fire on the due date
#: itself". Nothing the engine can read off the numbers distinguishes a rule with
#: no clock from a rule whose clock is tight, so the rule has to SAY so.
#:
#: v0.22.1 refused a third `date_field_role` value deliberately, and this is not
#: the one it refused. What it refused was a role that inverted the SIGN of every
#: threshold beside it — a number meaning days elapsed where the same number on
#: twelve other rules means days remaining. `State` reads no thresholds at all,
#: so there is no number left to misread.
DATE_ROLE_STATE = "State"

#: v0.22.1. Where `gate_date_field` is read from. `Direct` is a column on the
#: row being scanned; `Latest Related` is the newest date on another doctype
#: that points back at it, which is what "when was this block last sprayed"
#: looks like on a site keeping a spray log rather than a column.
GATE_DIRECT = "Direct"
GATE_LATEST_RELATED = "Latest Related"

#: v0.22.5. The comparisons a threshold condition may make. Deliberately a
#: SHORTER list than `FILTER_OPS`: a threshold is a number read against a number,
#: and `contains` on a temperature is a question nobody meant to ask.
THRESHOLD_OPS = ("gte", "gt", "lte", "lt", "eq", "ne")

#: v0.22.5. Where a threshold condition may read its NUMBER from, instead of
#: carrying a literal on the rule.
#:
#: A CLOSED REGISTRY, and it is closed for the same reason `custom_python` runs
#: in an interpreter this app wrote: "read a number from somewhere" is one
#: sentence away from "read anything from anywhere". Each key names one setting
#: this app already resolves per company, and nothing else is reachable.
#:
#: THE POINT IS THE PER-COMPANY OVERRIDE. OR-OSHA's heat threshold is 80 °F, and
#: an entity that has decided its own is 75 sets it once on Weather Settings —
#: where the v0.19.4 shift sweep already reads it. A literal on the rule would
#: make the alert layer and the operational layer disagree about what "hot" means
#: on the same afternoon, on the same shift, and the disagreement would be
#: invisible until somebody compared two records.
THRESHOLD_SOURCES = {
	"weather.heat_threshold_temp_f": (
		"the ambient-temperature heat threshold on Weather Settings, per company (default 80 °F)"
	),
	"weather.heat_threshold_heat_index_f": (
		"the heat-index threshold on Weather Settings, per company (default 80 °F)"
	),
	"weather.wind_threshold_mph_spray_block": (
		"the spray-block wind threshold on Weather Settings, per company (default 10 mph)"
	),
}

#: What `latest_child_field_threshold.match` may say.
MATCH_ANY = "any"
MATCH_ALL = "all"

#: What `authored_by` may say. `System` is a rule this app shipped and seeded.
AUTHOR_SYSTEM = "System"
AUTHOR_OPERATOR = "Operator"
AUTHOR_AI = "AI-proposed"

#: Categories a rule's alerts can land under, matching Compliance Alert.
CATEGORIES = (
	"Audits",
	"Certifications",
	"Filings",
	# v0.39.0. A financial KPI past its threshold, on the same calendar as
	# everything else — see `alerts/rules.py::financial_kpi_threshold_breach`.
	"Finance",
	"Housing",
	# v0.69.0. A chemical below its reorder level — see
	# `alerts/rules.py::item_below_reorder`. NEW IN THE ALERT DOCTYPE TOO, and
	# the only category here that was: every other value already existed on
	# Compliance Alert and this vocabulary was catching up with it. An orchard
	# out of the product it books a spray window against is an operational
	# problem with a compliance consequence, and filing it under "Other" would
	# put it on a board where "Other" means nobody has decided.
	"Inventory",
	"Policies",
	"Records",
	# v0.69.0, and it was in the Compliance Alert doctype from v0.15.0 with no
	# rule using it. The REI and PHI window rules are the first two.
	"Spray and Pesticides",
	"Water and Sanitation",
	"Workforce",
)

SEVERITIES = ("Critical", "Warning", "Info")

#: v0.22.5. What a `State` rule raises at when its record says nothing.
#:
#: WARNING RATHER THAN CRITICAL, deliberately. A state-driven rule fires the
#: moment a condition becomes true, which for the shipped one is the first
#: reading at or above the threshold — and a threshold cross is a heads-up, not
#: a finding that something has stopped being lawful. Critical belongs to
#: sustained exposure and to worker-reported symptoms, which are different rules
#: watching different facts. A default of Critical would put every hot afternoon
#: at the top of a board where "Critical" is supposed to mean somebody stops what
#: they are doing.
SEVERITY_DEFAULT = "Warning"

#: The scope-filter operators, and what each one means when the column is EMPTY.
#: That last column is the whole reason filters are evaluated in Python rather
#: than pushed into SQL: `status != 'Active'` in SQL excludes every row whose
#: status was never set, and every one of the shipped rules that reads a status
#: treats an unset one as the default. A filter that silently dropped those rows
#: would make a rule quiet on exactly the records nobody has touched.
FILTER_OPS = {
	"eq": "equal to, as text",
	"ne": "not equal to, as text",
	"gt": "greater than — numeric where both sides are numbers, else lexical (which is correct for ISO dates)",
	"lt": "less than, same comparison",
	"gte": "greater than or equal, same comparison",
	"lte": "less than or equal, same comparison",
	"in": "one of a list",
	"nin": "not one of a list",
	"isnull": "the column is empty (value is ignored)",
	"isnotnull": "the column has something in it (value is ignored)",
	"istrue": "a CHECK BOX is ticked (value is ignored) — never `isnotnull`, because a Check read back before the database layer holds the string '0', which every truthiness test calls true",
	"isfalse": "a check box is not ticked (value is ignored)",
	"contains": "the value appears in the column, case-insensitively",
	"ncontains": "the value does not appear in the column",
	"any": (
		"at least ONE of the filters nested in `value` passes — the only disjunction in this "
		"vocabulary, and it names no `field` of its own"
	),
}

#: v0.138.0. THE ONE OR, AND WHY THERE IS EXACTLY ONE. Every other entry above
#: asks one question of one column, and the list they sit in is ANDed — which is
#: the right default and covers every shipped rule but one. `i9_section_1_unsigned`
#: is that one: an I-9 Section is attested EITHER by a signature image captured
#: at a pad this server was holding OR by the sealed page arriving with both
#: signing moments recorded beside it (see `tools/i9.phone_attested`), and "not
#: attested" is therefore an AND of an OR. No arrangement of ANDed filters says
#: it, and the alternative was moving two shipped rules out of the declarative
#: vocabulary and into Python — which is a bigger, more permanent widening than
#: this.
#:
#: ONE LEVEL DEEP AND NO FURTHER, refused at authoring time. `any` inside `any`
#: is a boolean expression language, and this app already has `custom_python` in
#: a sandbox for the case a fixed vocabulary does not reach. A nested-group
#: vocabulary that stopped short of that would be a worse version of both.
_GROUP_OPS = ("any",)

#: Ops that take no `value`.
_NULLARY_OPS = ("isnull", "isnotnull", "istrue", "isfalse")

#: Ops whose `value` must be a list.
_LIST_OPS = ("in", "nin")

#: v0.68.0. Dynamic values a scope-filter `value` may name instead of a literal,
#: matched WHOLE-STRING — `"{{current_year}}"` resolves, `"before {{current_year}}"`
#: does not, because a filter value is compared against a column as a number or as
#: exact text, never as a blended string. This is what `w4_tax_year_outdated` was
#: missing: "current year" is not a fact a rule authored today can hardcode, since
#: the rule is still meant to be true next January. Each resolver is called AT SWEEP
#: TIME, not at authoring time, so the same rule reads a different year every
#: calendar year without an edit.
#:
#: A CLOSED REGISTRY, same reasoning as `THRESHOLD_SOURCES` above: "resolve a
#: template" is one sentence away from "evaluate an expression", and this app's
#: rule already has `custom_python` for the case a fixed vocabulary does not reach.
#: v0.69.0 adds the FOURTH, and it is the one that made a rule with an hour hand
#: possible. Every threshold in this vocabulary counts days, which is right for a
#: certificate and useless for a restricted-entry interval: a four-hour REI on a
#: block sprayed at two in the afternoon expires at six, and a rule that could
#: only compare dates would either hold the alert until midnight or drop it at
#: breakfast. `{{current_datetime}}` against a Datetime column compares as ISO
#: TEXT — which sorts correctly, to the second — so `rei_expires_at gt
#: {{current_datetime}}` is true exactly while the block is shut and false the
#: minute it is not. THAT COMPARISON IS THE AUTO-DISMISS: the sweep stops
#: observing, and `alerts/base.py` dismisses what it stops observing. Nothing
#: had to learn about hours.
TEMPLATE_VARIABLES = {
	"current_year": lambda: frappe.utils.getdate().year,
	"current_date": lambda: frappe.utils.today(),
	"current_month": lambda: frappe.utils.getdate().month,
	"current_datetime": lambda: str(frappe.utils.now()),
}

_TEMPLATE_RE = re.compile(r"^\{\{\s*(\w+)\s*\}\}$")


def _is_template(value) -> bool:
	return isinstance(value, str) and bool(_TEMPLATE_RE.match(value.strip()))


def _template_name(value: str) -> str:
	return _TEMPLATE_RE.match(value.strip()).group(1)


def resolve_template(value):
	"""A scope-filter `value`, with any `{{...}}` template resolved to its current
	value. Lists resolve element-wise, for `in`/`nin`; anything else that is not a
	templated string passes through unchanged.

	Called at EVALUATION time (`_passes`), not at parse time — `parse_filters`
	validates the template name is one this app knows but leaves the string alone,
	because "unresolved" is what makes the value re-evaluate on every sweep rather
	than freeze at whatever year the rule happened to be saved in.
	"""
	if isinstance(value, list):
		return [resolve_template(entry) for entry in value]
	if not _is_template(value):
		return value
	name = _template_name(value)
	resolver = TEMPLATE_VARIABLES.get(name)
	if resolver is None:  # pragma: no cover - parse_filters refuses this first
		return value
	return resolver()


#: Evidence-contract keys, shared with Inspection Template sections so a rule's
#: producer task and a template section ask for evidence in one vocabulary.
CONTRACT_KEYS = (
	"photos",
	"signature",
	"findings_text",
	"witness",
	"checklist_items",
	"measurements",
)

#: Most rules one site may have. A rule set larger than this is not a rule set,
#: it is an import that went wrong, and the sweep would spend its night on it.
RULE_CAP = 200

#: Longest a `rule_id` may be. It is the first segment of every alert docname
#: and `alert_key` has 140 characters to fit the whole thing into.
MAX_RULE_ID = 60

#: The fields `rule_rows` reads. Named rather than `*` so a column added in a
#: later version cannot silently change what the sweep is reading.
RULE_FIELDS = (
	"name",
	"rule_id",
	"title",
	"category",
	"enabled",
	# v0.80.0. Selected on every read because the SWEEP filters on it: a rule
	# carrying a control point is a transaction-time gate and is skipped by
	# `engine.rule_set`. `existing_fields` drops both on a site mid-migrate, and
	# the filter then sees a blank and sweeps nothing differently — which is the
	# correct behaviour for a site that has no gates yet.
	"control_point",
	"enforcement_mode",
	"version",
	"superseded_by",
	"active_row_flag",
	"target_doctype",
	"requires_doctypes",
	"requires_fields",
	"builtin_scanner",
	"custom_python",
	"target_doctypes_json",
	"date_field",
	"date_field_role",
	"date_fields_json",
	"cadence_days",
	"window_field",
	"missing_date_behaviour",
	"due_date_mode",
	"gate_date_field",
	"gate_within_days",
	"gate_scope",
	"gate_related_table_json",
	"latest_child_field_threshold_json",
	"superseded_by_later_clean_json",
	"regime_heuristics_json",
	"category_heuristics_json",
	"threshold_critical_days",
	"threshold_warning_days",
	"severity_critical",
	"severity_warning",
	"severity_expired",
	"default_severity",
	"scope_filters_json",
	"extra_parameters_json",
	"message_template",
	"regimes_from_field",
	"producer_task_template",
	"producer_farm_task_type",
	"producer_skill_required",
	"producer_assigned_to_expression",
	"evidence_contract_json",
	"regulation_citations",
	"retention_years",
	"audit_packet_types",
	"kairotic_gate_description",
	"purpose",
	"authored_by",
	"ai_source_citation",
	"ai_review_flags",
	"human_approved_by",
	"human_approved_on",
	"approver_employee",
	"approver_signature",
	"modified",
	"owner",
)


# ── JSON blobs, parsed the same way everywhere ──────────────────────────────
def as_object(raw, label: str = "the JSON object") -> dict:
	"""A JSON object from a text column, or a ValueError naming the column."""
	if raw in (None, ""):
		return {}
	if isinstance(raw, dict):
		return dict(raw)
	try:
		parsed = json.loads(raw)
	except Exception as exc:
		raise ValueError(f"{label} is not valid JSON: {exc}") from None
	if parsed in (None, ""):
		return {}
	if not isinstance(parsed, dict):
		raise ValueError(f"{label} must be a JSON object, got {type(parsed).__name__}.")
	return parsed


def as_list(raw, label: str) -> list:
	"""A JSON list from a text column, or a ValueError naming the column."""
	if raw in (None, ""):
		return []
	if isinstance(raw, (list, tuple)):
		return list(raw)
	try:
		parsed = json.loads(raw)
	except Exception as exc:
		raise ValueError(f"{label} is not valid JSON: {exc}") from None
	if parsed in (None, ""):
		return []
	if not isinstance(parsed, list):
		raise ValueError(f"{label} must be a JSON list, got {type(parsed).__name__}.")
	return parsed


def parse_filters(raw, label: str = "scope_filters", nested: bool = False) -> list:
	"""Validate a scope-filter list, or refuse saying which entry and why.

	REFUSED AT AUTHORING TIME, which is the whole point of doing it here: a
	filter with a typo'd operator asks for nothing and looks like it asks for
	something, and discovering that from an empty calendar three weeks later is
	the failure this app is written against.

	`nested` is set when this is parsing the inside of an `any` group, and all it
	does is refuse a second one — see `_GROUP_OPS`.
	"""
	out = []
	for index, entry in enumerate(as_list(raw, label)):
		if not isinstance(entry, dict):
			raise ValueError(
				f'{label}[{index}] must be an object like {{"field": "status", "op": "eq", '
				f'"value": "Active"}}, got {type(entry).__name__}.'
			)
		op = str(entry.get("op") or "eq").strip().lower()
		if op not in FILTER_OPS:
			raise ValueError(
				f"{label}[{index}] uses operator {op!r}, which is not one of: "
				f"{', '.join(sorted(FILTER_OPS))}."
			)
		if op in _GROUP_OPS:
			out.append(_parse_group(entry, op, label, index, nested))
			continue
		field = str(entry.get("field") or "").strip()
		if not field:
			raise ValueError(f"{label}[{index}] names no `field`.")
		value = entry.get("value")
		if op in _LIST_OPS:
			if not isinstance(value, (list, tuple)):
				raise ValueError(f"{label}[{index}] uses `{op}`, whose `value` must be a list.")
			value = list(value)
		elif op in _NULLARY_OPS:
			value = None
		for candidate in value if isinstance(value, list) else [value]:
			if isinstance(candidate, str) and "{{" in candidate and not _is_template(candidate):
				raise ValueError(
					f"{label}[{index}].value {candidate!r} looks like a template but is not one — "
					'a template must be the WHOLE value, like "{{current_year}}", not embedded in '
					"other text."
				)
			if _is_template(candidate) and _template_name(candidate) not in TEMPLATE_VARIABLES:
				raise ValueError(
					f"{label}[{index}].value names template variable "
					f"{_template_name(candidate)!r}, which is not one of: "
					f"{', '.join(sorted(TEMPLATE_VARIABLES))}."
				)
		row = {"field": field, "op": op, "value": value}
		if "default" in entry:
			row["default"] = entry["default"]
		out.append(row)
	return out


def _parse_group(entry: dict, op: str, label: str, index: int, nested: bool) -> dict:
	"""One `any` group: its own `field` is empty and its `value` is a filter list."""
	if nested:
		raise ValueError(
			f"{label}[{index}] nests `{op}` inside another one. Groups go one level deep: a "
			"boolean expression language belongs in custom_python, which this app already has."
		)
	members = parse_filters(entry.get("value"), f"{label}[{index}].value", nested=True)
	if not members:
		# An empty group passes every row, which reads as a filter and scopes
		# nothing — the exact failure this parser exists to catch.
		raise ValueError(
			f"{label}[{index}] uses `{op}` with no filters in its `value`. An empty group passes "
			"every row, so the rule would look scoped and be unscoped."
		)
	# `field` is present and empty rather than absent, because every reader of a
	# parsed filter list indexes it — see `filter_fields` for the one that has to
	# look past it.
	return {"field": "", "op": op, "value": members}


def filter_fields(filters: list) -> list:
	"""Every column a parsed filter list reads, groups included, in order.

	The callers that build a SELECT from a filter list cannot just read `field`
	off each entry any more: an `any` group names no column of its own and every
	column it does read is one level down. A SELECT missing those columns is not a
	crash — `row_matches` skips a filter whose column is not present, which widens
	the scan rather than narrowing it — so this would have been a rule that quietly
	stopped scoping.
	"""
	out = []
	for entry in filters or []:
		if entry.get("op") in _GROUP_OPS:
			out.extend(filter_fields(entry.get("value") or []))
			continue
		field = str(entry.get("field") or "")
		if field:
			out.append(field)
	return out


# ── v0.22.1: the primitives that took five scanners declarative ─────────────
#
# Each parser below refuses AT AUTHORING TIME for the same reason `parse_filters`
# does: every one of these blobs asks for nothing when it is wrong and looks like
# it asks for something, and finding that out from a quiet calendar three weeks
# later is the failure this app is written against.


def parse_target_doctypes(raw, label: str = "target_doctypes") -> list:
	"""The doctypes one rule walks, where it walks more than one.

	`target_doctype` is singular by design and stays the default. This exists
	because ONE shipped rule is genuinely about two record types —
	`housing_corrective_action_open` reads Housing Inspection and Detector Test —
	and splitting it into two rule_ids would split one afternoon's walk round the
	camp across two alert types and change every alert docname on every site.

	Each entry: `doctype` (required), `date_field` (defaults to the rule's), and
	`label`, which is the fragment the message template says instead of the
	doctype name — "the habitability inspection" reads like the errand it is and
	"Housing Inspection" reads like a table.
	"""
	out = []
	for index, entry in enumerate(as_list(raw, label)):
		if isinstance(entry, str):
			entry = {"doctype": entry}
		if not isinstance(entry, dict):
			raise ValueError(
				f'{label}[{index}] must be an object like {{"doctype": "Detector Test", '
				f'"date_field": "test_date", "label": "the detector test"}}, got {type(entry).__name__}.'
			)
		doctype = str(entry.get("doctype") or "").strip()
		if not doctype:
			raise ValueError(f"{label}[{index}] names no `doctype`.")
		out.append(
			{
				"doctype": doctype,
				"date_field": str(entry.get("date_field") or "").strip(),
				"label": str(entry.get("label") or "").strip(),
			}
		)
	return out


def parse_date_fields(raw, label: str = "date_fields") -> list:
	"""Several anchors of the SAME kind, where either being stale fires the rule.

	`housing_detector_test_stale` is the case: a cabin has a smoke detector and a
	CO detector, they are tested independently, and an alert saying only "a
	detector is overdue" sends somebody to test the wrong one. So each entry
	carries a `label`, and the message enumerates the stale ones by name.

	The label is a FRAGMENT, not a sentence — "smoke" and "CO", because the
	template around it supplies "detector". A rule whose labels read as sentences
	produces a message that reads as a list.
	"""
	out = []
	for index, entry in enumerate(as_list(raw, label)):
		if isinstance(entry, str):
			entry = {"field": entry}
		if not isinstance(entry, dict):
			raise ValueError(
				f'{label}[{index}] must be an object like {{"field": "co_detector_last_test", '
				f'"label": "CO"}}, got {type(entry).__name__}.'
			)
		field = str(entry.get("field") or "").strip()
		if not field:
			raise ValueError(f"{label}[{index}] names no `field`.")
		out.append({"field": field, "label": str(entry.get("label") or field).strip()})
	return out


def parse_supersession(raw, label: str = "superseded_by_later_clean") -> dict:
	"""The one gate in the engine that is a question about OTHER rows.

	A finding stops being true when a LATER CLEAN RECORD FOR THE SAME SUBJECT
	supersedes it: a cabin re-inspected in September with nothing found says more
	about July's water stain than a checkbox does, and it requires nobody to
	remember a field. No filter on the finding's own row can answer that.

	  subject_field             what the two records have in common (`unit`, `source`)
	  date_field                the date "later" is measured on; defaults to the target's
	  doctype                   where to look for the clean record; defaults to the target
	  clean_state_field         the column that says a record found nothing
	  clean_state_values        the values of it that count as clean
	  unreadable_counts_as_dirty  default TRUE: a record whose state is empty or
	                            unreadable does NOT supersede. A result nobody can
	                            interpret is not evidence that the water is safe,
	                            and treating it as clean is how a compliance file
	                            becomes a clean record of nothing.
	"""
	config = as_object(raw, label)
	if not config:
		return {}
	subject = str(config.get("subject_field") or "").strip()
	if not subject:
		raise ValueError(
			f"{label} names no `subject_field`, so there is nothing for a later record to be "
			"'about the same thing' as. It is the column the finding and the clean record share — "
			"`unit` on a camp record, `source` on a water test."
		)
	state_field = str(config.get("clean_state_field") or "").strip()
	if not state_field:
		raise ValueError(f"{label} names no `clean_state_field`, so no record can be read as clean.")
	values = [
		str(entry or "").strip()
		for entry in as_list(config.get("clean_state_values"), f"{label}.clean_state_values")
	]
	values = [entry for entry in values if entry]
	if not values:
		raise ValueError(
			f"{label} lists no `clean_state_values`. With none, NOTHING supersedes and the rule can "
			"only ever be silenced by hand — which is the behaviour this primitive exists to remove."
		)
	return {
		"doctype": str(config.get("doctype") or "").strip(),
		"subject_field": subject,
		"date_field": str(config.get("date_field") or "").strip(),
		"clean_state_field": state_field,
		"clean_state_values": values,
		"unreadable_counts_as_dirty": bool(config.get("unreadable_counts_as_dirty", True)),
	}


def parse_gate_table(raw, label: str = "gate_related_table") -> dict:
	"""Where a `Latest Related` gate date is read from.

	`{doctype, subject_field, date_field, scope_filters}` — the newest `date_field`
	on a row of `doctype` whose `subject_field` points back at the row being
	scanned. `scope_filters` narrows which of those rows count, in the same
	vocabulary as the rule's own filters.

	`subject_key` is the column on the SCANNED row that the related rows name,
	and it defaults to `name`, which is what a Link column holds.
	"""
	config = as_object(raw, label)
	if not config:
		return {}
	out = {"subject_key": str(config.get("subject_key") or "name").strip() or "name"}
	for key in ("doctype", "subject_field", "date_field"):
		value = str(config.get(key) or "").strip()
		if not value:
			raise ValueError(
				f"{label} names no `{key}`. A related-table gate needs all three of doctype, "
				"subject_field and date_field, or it reads nothing and silences the rule entirely."
			)
		out[key] = value
	out["scope_filters"] = parse_filters(config.get("scope_filters"), f"{label}.scope_filters")
	return out


# ── v0.22.5: the gate about the LATEST ROW of a child table ─────────────────
def parse_latest_child_threshold(raw, label: str = "latest_child_field_threshold") -> dict:
	"""The newest child row of each scanned record, and a number read off it.

	SIBLING OF `gate_related_table`, NOT AN EXTENSION OF IT, and the difference is
	the fold rather than the syntax. `gate_related_table` folds a related doctype
	to ONE VALUE PER SUBJECT — the maximum date — and asks how old it is. This one
	folds to ONE ROW per subject, the latest, and then asks a question about its
	OTHER columns. A maximum over dates cannot answer "and what was the
	temperature on that row", because the answer is not a maximum of anything: the
	85 °F reading at noon says nothing about compliance at four o'clock if a 72 °F
	reading was written at half past three.

	It is also what puts the whole row into the message template, under
	`context_key`. An alert that says a threshold was crossed and cannot say WHAT
	the reading was is an alert somebody has to go and look up before they can act
	on it.

	  child_doctype     where the rows live — a child table's own doctype
	  parent_field      the column on the child naming the scanned row. `parent`
	                    by default, which is what a child table uses.
	  parentfield       which child table of the parent, where the doctype is used
	                    as more than one. Optional; narrows nothing when empty.
	  subject_key       the column on the SCANNED row the children name. `name`.
	  order_by          the column "latest" is measured on. REQUIRED — without it
	                    there is no latest and the rule would read whichever row
	                    the database handed back first, which is a rule whose
	                    answer changes when somebody adds an index.
	  context_key       what the message template calls the row. `latest_child`.
	  match             `any` (the default, an OR over the conditions) or `all`.
	  conditions        [{field, op, threshold | threshold_source}]
	  scope_filters     which child rows count at all, in the rule's own filter
	                    vocabulary.

	A SUBJECT WITH NO CHILD ROW IS GATED OUT, the same direction `gate_date_field`
	takes and for the same reason. A shift whose weather timeline is empty is not
	a shift that is cool; it is a shift nobody has a reading for, and raising a
	heat alert off no reading would be this app asserting a fact it does not have.
	"""
	config = as_object(raw, label)
	if not config:
		return {}

	child = str(config.get("child_doctype") or "").strip()
	if not child:
		raise ValueError(
			f"{label} names no `child_doctype`, so there are no rows to find the latest of. It is "
			"the doctype the child rows are — `Farm Shift Weather Reading`, not `Farm Shift`."
		)
	order_by = str(config.get("order_by") or "").strip()
	if not order_by:
		raise ValueError(
			f"{label} names no `order_by`, so there is no LATEST row — the rule would read whichever "
			"of a shift's thirty readings the database handed back first, and that is an answer that "
			"changes when somebody adds an index. Name the column time runs on."
		)

	raw_conditions = config.get("conditions")
	if raw_conditions in (None, "", []) and "field" in config:
		# The flat single-condition shape, because most gates are one comparison
		# and making somebody write a one-element list for it is a paper cut on
		# the commonest case.
		raw_conditions = [
			{
				"field": config.get("field"),
				"op": config.get("op"),
				"threshold": config.get("threshold"),
				"threshold_source": config.get("threshold_source"),
			}
		]
	conditions = []
	for index, entry in enumerate(as_list(raw_conditions, f"{label}.conditions")):
		if not isinstance(entry, dict):
			raise ValueError(
				f'{label}.conditions[{index}] must be an object like {{"field": "temp_f", "op": '
				f'"gte", "threshold": 80}}, got {type(entry).__name__}.'
			)
		field = str(entry.get("field") or "").strip()
		if not field:
			raise ValueError(f"{label}.conditions[{index}] names no `field`.")
		op = str(entry.get("op") or "gte").strip().lower()
		if op not in THRESHOLD_OPS:
			raise ValueError(
				f"{label}.conditions[{index}] uses operator {op!r}, which is not one of: "
				f"{', '.join(THRESHOLD_OPS)}. A threshold is a number read against a number."
			)
		source = str(entry.get("threshold_source") or "").strip()
		if source and source not in THRESHOLD_SOURCES:
			raise ValueError(
				f"{label}.conditions[{index}] reads its number from {source!r}, which is not a "
				f"setting this app resolves. The sources are: {', '.join(sorted(THRESHOLD_SOURCES))}."
			)
		has_literal = entry.get("threshold") not in (None, "")
		if not (source or has_literal):
			raise ValueError(
				f"{label}.conditions[{index}] names neither `threshold` nor `threshold_source`, so "
				"there is no number to compare against. A condition with no number matches nothing "
				"and looks like it matches something."
			)
		threshold = None
		if has_literal:
			try:
				threshold = float(entry["threshold"])
			except (TypeError, ValueError):
				raise ValueError(
					f"{label}.conditions[{index}] has a `threshold` of {entry['threshold']!r}, which "
					"is not a number."
				) from None
		conditions.append({"field": field, "op": op, "threshold": threshold, "threshold_source": source})
	if not conditions:
		raise ValueError(
			f"{label} lists no `conditions`. A gate that tests nothing gates NOTHING IN — every row "
			"with a child record would fire, which on an open-shift rule is every crew on the farm."
		)

	match = str(config.get("match") or MATCH_ANY).strip().lower()
	if match not in (MATCH_ANY, MATCH_ALL):
		raise ValueError(
			f"{label}.match is {match!r}; it is {MATCH_ANY!r} (an OR over the conditions, the "
			f"default) or {MATCH_ALL!r} (an AND)."
		)

	return {
		"child_doctype": child,
		"parent_field": str(config.get("parent_field") or "parent").strip() or "parent",
		"parentfield": str(config.get("parentfield") or "").strip(),
		"subject_key": str(config.get("subject_key") or "name").strip() or "name",
		"order_by": order_by,
		"context_key": str(config.get("context_key") or "latest_child").strip() or "latest_child",
		"match": match,
		"conditions": conditions,
		"scope_filters": parse_filters(config.get("scope_filters"), f"{label}.scope_filters"),
	}


def threshold_from_source(source: str, company: str = ""):
	"""One number out of `THRESHOLD_SOURCES`, for one company. None where unreadable.

	LAZILY IMPORTED, because the settings this reaches live in a service that
	imports the shift tools, and this module is imported by the DocType
	controller. None on failure rather than a raise: a rule whose threshold could
	not be read falls back to the literal on the condition, which is the number
	the regulation says — and a sweep that died because a Single had not migrated
	would take the whole calendar with it.
	"""
	key = str(source or "").strip()
	if key not in THRESHOLD_SOURCES:
		return None
	namespace, _, fieldname = key.partition(".")
	if namespace != "weather":
		return None  # pragma: no cover - the registry has one namespace today
	try:
		from .services import weather

		return float(weather.thresholds_for(str(company or ""))[fieldname])
	except Exception:
		return None


#: The two outcome vocabularies `parse_heuristics` understands, and the key each
#: one's default is written under.
HEURISTIC_OUTCOMES = {
	"regimes": ("then_regimes", "default_regimes"),
	"category": ("then_category", "default_category"),
}

#: The matchers a heuristic entry may use. `if_field_contains` is a substring
#: read of a name; `if_field_in` is exact membership of a closed list.
HEURISTIC_MATCHERS = ("if_field_contains", "if_field_in")


def parse_heuristics(raw, label: str, outcome: str = "regimes") -> list:
	"""An ORDERED lookup table, as data. First match wins, and the order is the content.

	`regimes_from_field` copies tags off a column. This is for the case where
	there is no column — only a NAME TO READ. A certificate's regimes come from
	its type through an ordered eleven-row needle table, and the ordering is the
	whole content: `globalgap` must be checked before `gap`, because "GlobalGAP"
	contains "GAP" and a USDA GAP packet must not be handed another scheme's
	certificate.

	THE FIELD ORDER IS THE OUTER LOOP. Where entries name more than one field,
	every entry is tried against the first-named field before ANY entry is tried
	against the second. That is not an implementation detail: a certificate typed
	"Food Safety Certificate" and named "WPS refresher" is a food-safety
	certificate, and a table that scanned entry-first would retag it from a word
	somebody typed on the day. The type is consulted first and the name is the
	fallback, exactly as the Python it replaces did.

	```json
	[
	  {"if_field_contains": {"field": ["cert_type", "cert_name"], "value": ["wps", "worker protection"]},
	   "then_regimes": ["WPS"]},
	  {"if_field_contains": {"field": ["cert_type", "cert_name"], "value": "globalgap"},
	   "then_regimes": ["GlobalGAP"]},
	  {"default_regimes": ["Internal"]}
	]
	```
	"""
	if outcome not in HEURISTIC_OUTCOMES:
		raise ValueError(
			f"{label}: unknown outcome {outcome!r}."
		)  # pragma: no cover - callers pass constants
	then_key, default_key = HEURISTIC_OUTCOMES[outcome]
	out = []
	for index, entry in enumerate(as_list(raw, label)):
		if not isinstance(entry, dict):
			raise ValueError(
				f'{label}[{index}] must be an object like {{"if_field_contains": {{"field": "cert_type", '
				f'"value": "wps"}}, "{then_key}": [...]}}, got {type(entry).__name__}.'
			)
		if default_key in entry:
			out.append({default_key: _heuristic_outcome(entry[default_key], outcome, label, index)})
			continue
		matcher = next((key for key in HEURISTIC_MATCHERS if key in entry), "")
		if not matcher:
			raise ValueError(
				f"{label}[{index}] has no matcher and no {default_key}. Each entry either tests "
				f"something ({', '.join(HEURISTIC_MATCHERS)}) or is the fallback."
			)
		test = entry[matcher]
		if not isinstance(test, dict):
			raise ValueError(f"{label}[{index}].{matcher} must be an object with `field` and `value`.")
		fields = test.get("field")
		fields = [fields] if isinstance(fields, str) else list(fields or [])
		fields = [str(field or "").strip() for field in fields]
		fields = [field for field in fields if field]
		if not fields:
			raise ValueError(f"{label}[{index}].{matcher} names no `field`.")
		values = test.get("value")
		values = [values] if isinstance(values, (str, int, float)) else list(values or [])
		values = [str(value or "").strip() for value in values]
		values = [value for value in values if value]
		if not values:
			raise ValueError(
				f"{label}[{index}].{matcher} names no `value`. An entry that matches on nothing "
				"matches EVERYTHING that reaches it, which is a fallback wearing a test's clothes."
			)
		if then_key not in entry:
			raise ValueError(f"{label}[{index}] tests something but says no `{then_key}`.")
		# NORMALISED INTO THE VOCABULARY IT WAS WRITTEN IN, not into an internal
		# shape. Every one of these parsers has to accept its own output, because
		# the column is parsed on save, stored, and parsed again on every sweep —
		# a parser whose output it cannot read is a rule that validates once and
		# then quietly stops matching anything, which is the worst failure
		# available to a compliance rule. It also means an operator reading the
		# stored JSON reads the same words the documentation uses.
		out.append(
			{
				matcher: {
					"field": fields,
					"value": values,
					"case_insensitive": bool(test.get("case_insensitive", True)),
				},
				then_key: _heuristic_outcome(entry[then_key], outcome, label, index),
			}
		)
	return out


def _heuristic_outcome(value, outcome: str, label: str, index: int):
	if outcome == "category":
		text = str(value or "").strip()
		if text and text not in CATEGORIES:
			raise ValueError(
				f"{label}[{index}] produces category {text!r}, which is not one of: "
				f"{', '.join(CATEGORIES)}. A category nothing groups by puts the alert in a "
				"section of the calendar nobody opens."
			)
		return text
	values = [value] if isinstance(value, str) else list(value or [])
	try:
		return regimes_vocabulary.require(values, f"{label}[{index}]")
	except ValueError as exc:
		raise ValueError(str(exc)) from None


def match_heuristics(entries: list, row: dict, outcome: str = "regimes"):
	"""Evaluate a parsed heuristic table against one row. See `parse_heuristics`."""
	then_key, default_key = HEURISTIC_OUTCOMES[outcome]
	tests = []
	for entry in entries:
		matcher = next((key for key in HEURISTIC_MATCHERS if key in entry), "")
		if matcher:
			tests.append((matcher, entry[matcher], entry.get(then_key)))
	order: list = []
	for _matcher, test, _outcome in tests:
		for field in test["field"]:
			if field not in order:
				order.append(field)
	# THE FIELD ORDER IS THE OUTER LOOP. See `parse_heuristics`: the whole table
	# is tried against the type before any of it is tried against the name.
	for field in order:
		for matcher, test, produced in tests:
			if field in test["field"] and _heuristic_hit(matcher, test, row.get(field)):
				return produced
	for entry in entries:
		if default_key in entry:
			return entry[default_key]
	return None


def _heuristic_hit(matcher: str, test: dict, value) -> bool:
	text = str(value if value is not None else "")
	needles = list(test["value"])
	if test.get("case_insensitive", True):
		text = text.lower()
		needles = [needle.lower() for needle in needles]
	if not text:
		return False
	if matcher == "if_field_in":
		return text in needles
	return any(needle in text for needle in needles)


def parse_contract(raw, label: str = "evidence_contract") -> dict:
	"""An evidence contract, refusing any key outside the shared vocabulary."""
	contract = as_object(raw, label)
	unknown = [key for key in contract if key not in CONTRACT_KEYS]
	if unknown:
		raise ValueError(
			f"{label} names {', '.join(repr(key) for key in sorted(unknown))}, which no producer "
			f"task understands. The vocabulary is: {', '.join(CONTRACT_KEYS)}. A key outside it "
			'asks for nothing and looks like it asks for something — {"photo": true} is the one '
			"that catches everybody."
		)
	return contract


def parse_packet_types(raw, label: str = "audit_packet_types") -> list:
	"""Audit packet keys, checked against the packets this app can actually build."""
	values = [str(entry or "").strip() for entry in as_list(raw, label)]
	values = [entry for entry in values if entry]
	try:
		from . import audit_packets

		known = set(audit_packets.names())
	except Exception:  # pragma: no cover - a site mid-import
		known = set()
	if known:
		unknown = [entry for entry in values if entry not in known]
		if unknown:
			raise ValueError(
				f"{label} names packet type(s) {', '.join(repr(entry) for entry in unknown)}, which "
				f"this app does not build. The packets are: {', '.join(sorted(known))}. A packet "
				"name nobody generates is a claim that an alert reaches a document it never reaches."
			)
	return values


# ── the filter engine ───────────────────────────────────────────────────────
def row_matches(row: dict, filters: list, present_fields: set | None = None) -> tuple:
	"""Does one row pass every filter? Returns (matched, warnings).

	IN PYTHON, NOT IN SQL, and that is a decision rather than an oversight. Three
	of the shipped rules read a column whose EMPTY value means something specific
	— a Compliance Policy with no status counts as Active, a Regulatory Filing
	with no status is neither Draft nor Withdrawn, a Housing Unit with no
	condition is not Uninhabitable — and every one of those rows is excluded by
	the equivalent SQL comparison, silently. Reading `default` here reproduces
	the legacy Python exactly, and says out loud what the legacy `or "Active"`
	said in an idiom.

	A filter naming a field the site has not got is SKIPPED AND REPORTED rather
	than failing the row. Half this app's compliance columns are installed on
	demand, and a rule that refused every row on a site that had not run
	`install_compliance_fields` would look exactly like a clean operation.
	"""
	warnings = []
	for entry in filters or []:
		if entry.get("op") in _GROUP_OPS:
			matched = _group_passes(row, entry, present_fields, warnings)
			if not matched:
				return False, warnings
			continue
		answered, passed = _entry_passes(row, entry, present_fields, warnings)
		if answered and not passed:
			return False, warnings
	return True, warnings


def _entry_passes(row: dict, entry: dict, present_fields: set | None, warnings: list) -> tuple:
	"""(could this site answer the filter, did the row pass it).

	Split out of `row_matches` so an `any` group can ask the same question of its
	members and get the SAME answer to "this column is not on this site" — which
	is the whole reason it is one function and not two: a group that treated an
	absent column as a FAILED test could fail the whole group, and narrow a rule on
	a site that has simply not installed the field. Every skip here widens the scan
	and says so; none of them narrows it.
	"""
	field = entry["field"]
	if present_fields is not None and field not in present_fields:
		warning = (
			f"scope filter on {field!r} was skipped: this site's target doctype has no such "
			"column. The rule ran without it, so it scanned MORE rows than it was scoped to, "
			"not fewer."
		)
		if warning not in warnings:
			warnings.append(warning)
		return False, True
	value = row.get(field)
	if value in (None, "") and "default" in entry:
		value = entry["default"]
	return True, _passes(value, entry["op"], entry.get("value"))


def _group_passes(row: dict, entry: dict, present_fields: set | None, warnings: list) -> bool:
	"""Does the row pass at least one member of an `any` group?

	A GROUP NONE OF WHOSE COLUMNS THIS SITE HAS PASSES, and it is the same
	direction every other absent column takes here: the rule scans MORE rows than
	it was scoped to and says so in a warning, rather than going quiet on a site
	that has not run `install_compliance_fields`. A group is a NARROWING — it can
	only ever exclude rows — so passing it is the fail-safe answer.
	"""
	answerable = False
	for member in entry.get("value") or []:
		answered, passed = _entry_passes(row, member, present_fields, warnings)
		if not answered:
			continue
		answerable = True
		if passed:
			return True
	return not answerable


def _passes(value, op: str, wanted) -> bool:
	wanted = resolve_template(wanted)
	if op == "isnull":
		return not str(value or "").strip()
	if op == "isnotnull":
		return bool(str(value or "").strip())
	# CHECK BOXES GO THROUGH `compat.checked` AND NOTHING ELSE. A Check field read
	# back before it has been through the database layer carries the STRING "0",
	# which `isnotnull` — and every other truthiness test — calls true. On the rule
	# that uses this the wrong answer is "this shed is a worker facility", which
	# puts a building nobody sleeps in on the camp's inspection list and, worse,
	# would have put a real bunkhouse off it had the sense been the other way.
	if op == "istrue":
		return compat.checked(value)
	if op == "isfalse":
		return not compat.checked(value)

	text = str(value if value is not None else "")
	if op == "eq":
		return text == str(wanted if wanted is not None else "")
	if op == "ne":
		return text != str(wanted if wanted is not None else "")
	if op == "in":
		return text in [str(entry if entry is not None else "") for entry in wanted or []]
	if op == "nin":
		return text not in [str(entry if entry is not None else "") for entry in wanted or []]
	if op == "contains":
		return str(wanted or "").lower() in text.lower()
	if op == "ncontains":
		return str(wanted or "").lower() not in text.lower()

	left, right = _comparable(value, wanted)
	if left is None or right is None:
		return False
	if op == "gt":
		return left > right
	if op == "lt":
		return left < right
	if op == "gte":
		return left >= right
	if op == "lte":
		return left <= right
	return False  # pragma: no cover - parse_filters refused everything else


def passes_threshold(value, op: str, threshold) -> bool:
	"""One numeric comparison, for `latest_child_field_threshold`. NUMBERS ONLY.

	Separate from `_passes` and deliberately narrower. `_passes` compares
	numerically where both sides are numbers and falls back to a LEXICAL
	comparison otherwise, which is right for its callers — the values reaching an
	ordering comparison in a scope filter are overwhelmingly ISO dates, and ISO
	dates sort correctly as strings.

	IT IS EXACTLY WRONG ON A THERMOMETER. A reading whose column somebody filled
	in as "warm", or that an import wrote as "n/a", is lexically greater than
	"80" — and would raise a heat alert off a word. A value that is not a number
	answers False here, which leaves the row alone: this app does not know how hot
	it was, and not knowing is not the same as knowing it was hot.
	"""
	try:
		left, right = float(value), float(threshold)
	except (TypeError, ValueError):
		return False
	if op == "gte":
		return left >= right
	if op == "gt":
		return left > right
	if op == "lte":
		return left <= right
	if op == "lt":
		return left < right
	if op == "eq":
		return left == right
	if op == "ne":
		return left != right
	return False  # pragma: no cover - parse_latest_child_threshold refused everything else


def _comparable(left, right):
	"""Both sides as numbers where both are numeric, else both as text.

	Text is the right fallback because the values that reach an ordering
	comparison in a compliance rule are overwhelmingly ISO dates, and ISO dates
	sort correctly as strings. A comparison this cannot make answers False, which
	excludes the row rather than raising — a rule that died on one malformed cell
	would take the whole night's sweep of that rule with it.
	"""
	try:
		return float(left), float(right)
	except (TypeError, ValueError):
		pass
	if left is None or right is None:
		return None, None
	return str(left), str(right)


# ── reading the rule set ────────────────────────────────────────────────────
def rule_rows(include_inactive: bool = False, limit: int = RULE_CAP) -> list:
	"""Every Compliance Rule row, live ones by default.

	LIVE MEANS ENABLED AND UNSUPERSEDED, and the two conditions are separate on
	purpose. A superseded row is history — the definition an alert was raised
	under. A disabled row is a decision somebody made this season. Neither runs;
	both stay readable.
	"""
	if not compat.doctype_exists(DOCTYPE):
		return []
	filters = {}
	if not include_inactive:
		filters = {"enabled": 1, "superseded_by": ("in", ("", None))}
	rows = frappe.db.get_all(
		DOCTYPE,
		filters=filters,
		fields=compat.existing_fields(DOCTYPE, RULE_FIELDS),
		order_by="rule_id asc, version asc",
		limit=limit,
	)
	return [dict(row) for row in rows or []]


def rule_row(name: str) -> dict:
	"""One rule by docname, with its regimes attached. Raises ValueError if absent."""
	if not compat.doctype_exists(DOCTYPE):
		raise ValueError(
			f"this site has no {DOCTYPE} DocType, which ships with erpnext_mcp — run "
			"`bench --site <site> migrate`."
		)
	name = str(name or "").strip()
	if not name or not frappe.db.exists(DOCTYPE, name):
		raise ValueError(
			f"no Compliance Rule called {name!r} on this site. list_compliance_rules has the "
			"register, and it accepts a rule_id as well as a docname."
		)
	row = dict(
		frappe.db.get_value(DOCTYPE, name, compat.existing_fields(DOCTYPE, RULE_FIELDS), as_dict=True) or {}
	)
	row["regimes"] = regimes_of(name)
	return row


def resolve(reference: str) -> str:
	"""A docname from a docname OR a rule_id, preferring the live version.

	Somebody asking about `training_expiring` today means whichever version is
	live, and making them look the docname up first would make every tool call a
	two-step. A docname is still accepted and still means that exact version,
	which is what somebody reading history is after.
	"""
	reference = str(reference or "").strip()
	if not reference or not compat.doctype_exists(DOCTYPE):
		return ""
	if frappe.db.exists(DOCTYPE, reference):
		return reference
	live = frappe.db.get_all(
		DOCTYPE,
		filters={"rule_id": reference, "superseded_by": ("in", ("", None))},
		fields=["name"],
		order_by="version desc",
		limit=1,
	)
	if live:
		return str(live[0]["name"])
	any_version = frappe.db.get_all(
		DOCTYPE, filters={"rule_id": reference}, fields=["name"], order_by="version desc", limit=1
	)
	return str(any_version[0]["name"]) if any_version else ""


def regimes_of(name: str) -> list:
	"""The regime tokens on one rule, read off its child table."""
	try:
		return regimes_vocabulary.rows_for_parents(DOCTYPE, [name], "regimes").get(name, [])
	except Exception:  # pragma: no cover - a site mid-migration
		return []


def shape_of(row: dict) -> str:
	"""Which of the three shapes this row is. The order is the precedence."""
	if str(row.get("custom_python") or "").strip():
		return SHAPE_CUSTOM
	if str(row.get("builtin_scanner") or "").strip():
		return SHAPE_BUILTIN
	return SHAPE_DECLARATIVE


def names_list(raw) -> list:
	"""A comma/newline separated column as a list of trimmed names."""
	out = []
	for chunk in str(raw or "").replace("\n", ",").split(","):
		entry = chunk.strip()
		if entry:
			out.append(entry)
	return out


def describe(row: dict, with_definition: bool = False) -> dict:
	"""One rule as a tool reports it.

	The keys `list_compliance_rules` returned before v0.22.0 are all here and
	mean what they always meant — `alert_type`, `title`, `category`, `purpose`,
	`kairotic_gate`, `framework`, `regimes`, `requires`, `available`. Everything
	new is additive, because a client that read the old shape has to go on
	working: this app's own iOS build reads that list.
	"""
	requires = names_list(row.get("requires_doctypes")) or (
		[str(row.get("target_doctype") or "")] if row.get("target_doctype") else []
	)
	out = {
		"alert_type": row.get("rule_id"),
		"title": row.get("title"),
		"category": row.get("category"),
		"purpose": str(row.get("purpose") or ""),
		"kairotic_gate": str(row.get("kairotic_gate_description") or ""),
		"framework": str(row.get("regulation_citations") or ""),
		"regimes": row.get("regimes") if row.get("regimes") is not None else regimes_of(row.get("name")),
		"requires": requires,
		"available": all(compat.doctype_exists(doctype) for doctype in requires)
		and all(
			compat.has_field(row.get("target_doctype"), field)
			for field in names_list(row.get("requires_fields"))
		),
		# v0.22.0 additions.
		"name": row.get("name"),
		"rule_id": row.get("rule_id"),
		"enabled": bool(compat.checked(row.get("enabled"))),
		"version": int(row.get("version") or 1),
		"superseded_by": str(row.get("superseded_by") or "") or None,
		"shape": shape_of(row),
		"target_doctype": row.get("target_doctype"),
		# v0.80.0. WHETHER THIS RULE IS A GATE OR A SWEEP, ON THE LIST SHAPE, because
		# it changes what the row MEANS more than any other field on it. A swept rule
		# that is enabled watches the site nightly; a gate that is enabled is
		# consulted at the moment of a transaction and may refuse it. Somebody
		# scanning the register to answer "what does this system stop me doing"
		# should not have to open each row to find the two that can.
		"control_point": str(row.get("control_point") or "") or None,
		"is_gate": bool(str(row.get("control_point") or "").strip()),
		"enforcement_mode": str(row.get("enforcement_mode") or "") or None,
		"enforced": str(row.get("enforcement_mode") or "") == "Enforced",
		# The same text as `framework` above, under the name the DocType and the
		# tools use. Both, because `framework` is the key every client since
		# v0.19.2 reads and `regulation_citations` is the field somebody editing
		# a rule passes — and a reader who had to know that those are one thing
		# would be reading a trap.
		"regulation_citations": str(row.get("regulation_citations") or ""),
		"authored_by": row.get("authored_by"),
		# v0.37.0. On the LIST shape rather than only in `definition`, because a
		# draft carrying a program is a different thing to review from a draft
		# that moved a threshold, and somebody scanning the review queue should
		# not have to open each one to find out which.
		"ai_review_flags": proposals.read_flags(row.get("ai_review_flags")),
		"human_approved_by": row.get("human_approved_by"),
		"human_approved_on": str(row.get("human_approved_on") or "") or None,
		"retention_years": int(row.get("retention_years") or 0) or None,
		# v0.22.5, and both are here rather than only in `definition` because both
		# change what somebody reading a LIST of rules should expect. `state_driven`
		# says this rule's alerts do not move with the calendar; the expression says
		# its producer task lands on one named person rather than in a skill pool.
		"date_field_role": row.get("date_field_role") or DATE_ROLE_CLOCK,
		"state_driven": str(row.get("date_field_role") or "") == DATE_ROLE_STATE,
		"default_severity": row.get("default_severity") or SEVERITY_DEFAULT,
		"producer_assigned_to_expression": str(row.get("producer_assigned_to_expression") or "") or None,
	}
	if with_definition:
		out["definition"] = {
			"date_field": row.get("date_field"),
			"date_field_role": row.get("date_field_role") or DATE_ROLE_CLOCK,
			"cadence_days": int(row.get("cadence_days") or 0),
			"window_field": row.get("window_field"),
			"missing_date_behaviour": row.get("missing_date_behaviour") or ON_MISSING_SKIP,
			"due_date_mode": row.get("due_date_mode") or DUE_FROM_ANCHOR,
			"threshold_critical_days": int(row.get("threshold_critical_days") or 0),
			"threshold_warning_days": int(row.get("threshold_warning_days") or 0),
			"severity_critical": row.get("severity_critical") or "Critical",
			"severity_warning": row.get("severity_warning") or "Warning",
			"severity_expired": row.get("severity_expired") or "Critical",
			"scope_filters": _quietly(parse_filters, row.get("scope_filters_json")),
			"extra_parameters": _quietly(as_object, row.get("extra_parameters_json"), {}),
			# v0.22.1's primitives. NULLABLE AND ADDITIVE: a rule that uses none of
			# them reports empties here, and every key `get_compliance_rule` returned
			# before still means what it meant.
			"target_doctypes": _quietly(parse_target_doctypes, row.get("target_doctypes_json")),
			"date_fields": _quietly(parse_date_fields, row.get("date_fields_json")),
			"superseded_by_later_clean": _quietly(
				parse_supersession, row.get("superseded_by_later_clean_json"), {}
			),
			"gate_date_field": row.get("gate_date_field"),
			"gate_within_days": int(row.get("gate_within_days") or 0),
			"gate_scope": row.get("gate_scope") or GATE_DIRECT,
			"gate_related_table": _quietly(parse_gate_table, row.get("gate_related_table_json"), {}),
			# v0.22.5.
			"latest_child_field_threshold": _quietly(
				parse_latest_child_threshold, row.get("latest_child_field_threshold_json"), {}
			),
			"default_severity": row.get("default_severity") or SEVERITY_DEFAULT,
			"regime_heuristics": _quietly(
				lambda raw: parse_heuristics(raw, "regime_heuristics", "regimes"),
				row.get("regime_heuristics_json"),
			),
			"category_heuristics": _quietly(
				lambda raw: parse_heuristics(raw, "category_heuristics", "category"),
				row.get("category_heuristics_json"),
			),
			"message_template": row.get("message_template"),
			"regimes_from_field": row.get("regimes_from_field"),
			"builtin_scanner": row.get("builtin_scanner"),
			"custom_python": row.get("custom_python"),
			"requires_fields": names_list(row.get("requires_fields")),
			"producer_task_template": row.get("producer_task_template"),
			"producer_farm_task_type": row.get("producer_farm_task_type"),
			"producer_skill_required": row.get("producer_skill_required"),
			"producer_assigned_to_expression": row.get("producer_assigned_to_expression"),
			"evidence_contract": _quietly(parse_contract, row.get("evidence_contract_json"), {}),
			"audit_packet_types": _quietly(parse_packet_types, row.get("audit_packet_types")),
			"ai_source_citation": row.get("ai_source_citation"),
			"approver_employee": row.get("approver_employee"),
			"approver_signature": row.get("approver_signature"),
		}
	return out


def _quietly(parser, raw, fallback=None):
	"""Parse for a READ. A malformed blob on a stored row must not break a list."""
	try:
		return parser(raw)
	except Exception:
		return [] if fallback is None else fallback


# ── writing one ─────────────────────────────────────────────────────────────
#: Every field `build_rule` copies off a plain dict, and the default where the
#: spec is silent. One table so the seeder, `create_compliance_rule` and
#: `update_compliance_rule` cannot drift into writing different shapes of row.
RULE_DEFAULTS = {
	"rule_id": "",
	"title": "",
	"category": "Records",
	"enabled": 0,
	# v0.80.0. Both default to "" so a swept rule written through `build_rule` is
	# explicitly not a gate, rather than inheriting whatever the column happened
	# to hold. The controller blanks `enforcement_mode` on a rule with no control
	# point and normalises it to Advisory on one that has.
	"control_point": "",
	"enforcement_mode": "",
	"version": 1,
	"target_doctype": "",
	"requires_doctypes": "",
	"requires_fields": "",
	"builtin_scanner": "",
	"custom_python": "",
	"date_field": "",
	"date_field_role": DATE_ROLE_CLOCK,
	"cadence_days": 0,
	"window_field": "",
	"missing_date_behaviour": ON_MISSING_SKIP,
	"due_date_mode": DUE_FROM_ANCHOR,
	"gate_date_field": "",
	"gate_within_days": 0,
	"gate_scope": GATE_DIRECT,
	"threshold_critical_days": 30,
	"threshold_warning_days": 90,
	"severity_critical": "Critical",
	"severity_warning": "Warning",
	"severity_expired": "Critical",
	"default_severity": SEVERITY_DEFAULT,
	"message_template": "",
	"regimes_from_field": "",
	"producer_task_template": None,
	"producer_farm_task_type": "",
	"producer_skill_required": "",
	"producer_assigned_to_expression": "",
	"regulation_citations": "",
	"retention_years": 0,
	"kairotic_gate_description": "",
	"purpose": "",
	"authored_by": AUTHOR_OPERATOR,
	"ai_source_citation": "",
	"ai_review_flags": "",
	"human_approved_by": None,
	"human_approved_on": None,
	"approver_employee": None,
	"approver_signature": "",
}


#: v0.22.1's four primitives (and the two helpers they needed), as
#: (spec key, column, parser). One table so the seeder, `create_compliance_rule`,
#: `update_compliance_rule`, the controller and `describe` cannot drift into
#: validating different things — the drift that would let a malformed blob
#: through one door and be refused at another.
_PRIMITIVE_BLOBS = (
	("target_doctypes", "target_doctypes_json", parse_target_doctypes),
	("date_fields", "date_fields_json", parse_date_fields),
	("superseded_by_later_clean", "superseded_by_later_clean_json", parse_supersession),
	("gate_related_table", "gate_related_table_json", parse_gate_table),
	(
		"latest_child_field_threshold",
		"latest_child_field_threshold_json",
		parse_latest_child_threshold,
	),
	(
		"regime_heuristics",
		"regime_heuristics_json",
		lambda raw, label="regime_heuristics": parse_heuristics(raw, label, "regimes"),
	),
	(
		"category_heuristics",
		"category_heuristics_json",
		lambda raw, label="category_heuristics": parse_heuristics(raw, label, "category"),
	),
)


def build_rule(spec: dict):
	"""One unsaved Compliance Rule from a plain dict.

	Shared by the seeder and by the MCP tools, so a rule this app ships and one
	somebody authors through MCP are the same shape of record — including the
	parts a seeder could quietly have skipped.
	"""
	doc = frappe.new_doc(DOCTYPE)
	for fieldname, default in RULE_DEFAULTS.items():
		value = spec.get(fieldname, default)
		if isinstance(default, str) and value is not None and not isinstance(value, str):
			value = str(value)
		if isinstance(default, str):
			value = str(value or "").strip()
		doc.set(fieldname, value)
	doc.scope_filters_json = json.dumps(
		parse_filters(spec.get("scope_filters", spec.get("scope_filters_json")))
	)
	doc.extra_parameters_json = json.dumps(
		as_object(spec.get("extra_parameters", spec.get("extra_parameters_json")), "extra_parameters")
	)
	doc.evidence_contract_json = json.dumps(
		parse_contract(spec.get("evidence_contract", spec.get("evidence_contract_json")))
	)
	doc.audit_packet_types = json.dumps(
		parse_packet_types(spec.get("audit_packet_types", spec.get("audit_packet_types_json")))
	)
	# v0.22.1. Each of these is written as `[]` / `{}` rather than left NULL where
	# the spec is silent, so a rule that does not use a primitive says so in the
	# same shape as one that does — an operator reading two rules side by side
	# should not have to know that a blank column and an empty list are one thing.
	for argument, column, parser in _PRIMITIVE_BLOBS:
		doc.set(column, json.dumps(parser(spec.get(argument, spec.get(column)))))
	for regime in regimes_vocabulary.to_rows(spec.get("regimes") or []):
		doc.append("regimes", dict(regime))
	return doc


# ── the migration of the thirteen ───────────────────────────────────────────
def seed_specs() -> list:
	"""The thirteen shipped rules, as Compliance Rule specs.

	DERIVED FROM THE RULE OBJECTS THEMSELVES rather than restated beside them.
	`alerts/rules.py` holds one `register(Rule(...))` per rule with its title,
	category, purpose, kairotic gate, framework citation, regimes and required
	doctypes, and one `shape(...)` saying how that rule migrates. This function
	joins the two. A rule whose gate prose is edited in `rules.py` therefore
	seeds with the edited prose, and there is no second copy to go stale — which
	matters more here than anywhere else in the app, because the gate and the
	citation are the two fields an auditor reads.
	"""
	from .alerts import rules as shipped

	# The producer half comes from `ALERT_TASK_MAP`, which has been the single
	# definition of "what work answers this alert" since v0.18.0 and is what
	# `generate_tasks_from_compliance_alerts` reads. Copying it onto the rule
	# rather than restating it means the two cannot disagree about which skill a
	# water sample needs — and it is what makes `producer_*` on the record
	# meaningful from the first migrate rather than empty until somebody fills
	# it in.
	try:
		from .tools.dispatch import ALERT_TASK_MAP
	except Exception:  # pragma: no cover - a partial import during a migrate
		ALERT_TASK_MAP = {}

	out = []
	for key in sorted(shipped.RULES):
		rule = shipped.RULES[key]
		shape = dict(shipped.SHAPES.get(key) or {})
		recipe = ALERT_TASK_MAP.get(key) or {}
		spec = {
			"rule_id": rule.key,
			"title": rule.title,
			"category": rule.category,
			"purpose": rule.purpose,
			"kairotic_gate_description": rule.kairotic_gate,
			"regulation_citations": rule.framework,
			"regimes": list(rule.regimes),
			"requires_doctypes": ", ".join(rule.requires),
			"producer_farm_task_type": str(recipe.get("task_type") or ""),
			"producer_skill_required": str(recipe.get("skill") or ""),
			"evidence_contract": dict(recipe.get("evidence") or {}),
			"authored_by": AUTHOR_SYSTEM,
			"enabled": 1,
		}
		spec.update(shape)
		out.append(spec)
	return out


# ── v0.22.5: rules that are ONLY records ────────────────────────────────────
#: Rules this app seeds that have NO shipped Python scanner behind them.
#:
#: THE FIRST OF THEM, AND THAT IS THE POINT OF THE RELEASE. Every rule up to now
#: existed as a `Rule` object in `alerts/rules.py` first and became a record
#: second, because every one of them predates the framework. This one was
#: authored as a record, in the vocabulary, and there is nothing to fall back to.
#: A site that has the app and has not yet migrated the Compliance Rule DocType
#: therefore does not run it — which is correct and is said out loud rather than
#: papered over: the fallback rule set is the DEFINITIONS OF v0.21.0, and a rule
#: invented afterwards is not one of them.
#:
#: It also means this list is the shape an AI-proposed rule will arrive in when
#: `propose_compliance_rule` is wired: a dict of declarative fields, a citation,
#: a kairotic gate, and nothing executable anywhere in it.
def i9_attestation_group() -> dict:
	"""The scope filter that spares an I-9 the phone attestation test satisfies.

	v0.138.0, AND THE COLUMN NAMES COME FROM `tools/i9` RATHER THAN FROM HERE.
	That is the whole point of it being a function. `unsigned_boxes` decides what
	`Complete` means and `i9_section_1_unsigned` / `i9_section_2_unsigned` decide
	what an inspection is warned about, and the release this was written for exists
	because those two disagreed: every phone-built I-9 was signed, sealed and
	retained, rested at `Awaiting Verification` for ever, and carried two Criticals
	saying nobody had signed it. Restating the three column names in this file
	would leave the same disagreement one edit away.

	IT IS THE NEGATION, because a scope filter says WHEN TO FIRE. `phone_attested`
	is true when all three columns are filled; this group passes when at least one
	is empty, which is exactly `not phone_attested`. The pair are tested against
	each other over the whole truth table in `test_i9_phone_attestation.py` —
	sharing the column names stops them naming different columns, not from reading
	them in opposite directions.
	"""
	from .tools import i9

	return {
		"op": "any",
		"value": [
			{"field": field, "op": "isnull"} for field in (i9.SIGNED_COPY_FIELD, *i9.ATTESTATION_TIMESTAMPS)
		],
	}


def declarative_seed_specs() -> list:
	from .services import weather

	return [
		{
			"rule_id": "shift_heat_threshold_crossed",
			"title": "An open shift's latest weather reading has crossed OR-OSHA's heat threshold",
			"category": "Workforce",
			"target_doctype": "Farm Shift",
			"requires_doctypes": "Farm Shift, Farm Shift Weather Reading",
			# THE SHIFT'S OWN START, read for the message and for nothing else. The
			# role below is what says so.
			"date_field": "start_datetime",
			"date_field_role": DATE_ROLE_STATE,
			"default_severity": "Warning",
			"due_date_mode": DUE_NONE,
			# Belt and braces against a site that edits the role back to Clock: a
			# negative threshold is the documented way to say "this band never
			# fires", so even misread the rule cannot start banding on a date.
			"threshold_critical_days": -1,
			"threshold_warning_days": -1,
			"cadence_days": 0,
			"scope_filters": [
				# `end_datetime` IS THE FACT AND `status` IS THE SUMMARY OF IT, which
				# is why v0.19.4's own sweep filters on the first. Both are here
				# because both are true of an open shift and ANDing them is strictly
				# narrower — but the status filter carries a `default` of Active, so
				# a shift written by an import that never set the column is still
				# read as open rather than silently dropped off the hot-day list.
				{"field": "end_datetime", "op": "isnull"},
				{"field": "status", "op": "eq", "value": "Active", "default": "Active"},
			],
			"latest_child_field_threshold": {
				"child_doctype": "Farm Shift Weather Reading",
				"parent_field": "parent",
				"parentfield": "weather_timeline",
				"subject_key": "name",
				"order_by": "reading_datetime",
				"context_key": "latest_weather",
				"match": MATCH_ANY,
				"conditions": [
					{
						"field": "temp_f",
						"op": "gte",
						"threshold": weather.DEFAULTS["heat_threshold_temp_f"],
						"threshold_source": "weather.heat_threshold_temp_f",
					},
					{
						"field": "heat_index_f",
						"op": "gte",
						"threshold": weather.DEFAULTS["heat_threshold_heat_index_f"],
						"threshold_source": "weather.heat_threshold_heat_index_f",
					},
				],
			},
			"message_template": (
				"Heat threshold crossed on {{ row.name }} at {{ row.location }} — latest reading "
				"{{ latest_weather.temp_f }}°F ({{ latest_weather.heat_index_f }}°F heat index) at "
				"{{ latest_weather.reading_datetime }}. Document water/shade/rest breaks per "
				"OAR 437-004-1131."
			),
			"regimes": ["OR-OSHA"],
			"regulation_citations": "OAR 437-004-1131 heat illness prevention",
			"retention_years": 3,
			"audit_packet_types": ["OSHA"],
			# NO Inspection Template and NO skill. The assignee is not a pool; it is
			# the one person who was standing there.
			"producer_task_template": None,
			"producer_farm_task_type": "Compliance-Audit",
			"producer_skill_required": "",
			"producer_assigned_to_expression": "row.foreman",
			"extra_parameters": {
				"producer_task_what": "Document the water, shade and rest cycle the crew took"
			},
			"evidence_contract": {"findings_text": True, "signature": True},
			"purpose": (
				"OR-OSHA -1131 does not ask whether it was hot; it asks what the operation DID about "
				"it, shift by shift. The weather timeline already proves the conditions. What an "
				"inspection turns on is whether somebody wrote down the water, the shade and the "
				"rest cycle while the crew was still in the field — and the person who can write "
				"that is the foreman who called them."
			),
			"kairotic_gate_description": (
				"Fires on a WEATHER FACT, not a date. When the latest reading on an open shift is at "
				"or above the OR-OSHA heat threshold — currently 80°F on the ambient thermometer or "
				"heat index — the foreman gets a task to document the water, shade and rest cycle "
				"their crew took. Not a compliance decision by the app; the record is the foreman's, "
				"signed by the foreman, and this alert is what makes sure it exists. Silences by "
				"itself when the shift closes: an open shift on a hot day is a compliance "
				"obligation, a closed shift on the same hot day is a completed one."
			),
			"authored_by": AUTHOR_SYSTEM,
			"enabled": 1,
		},
		# ── v0.23.0: field report awaiting dispatch ────────────────────────
		{
			"rule_id": "field_flag_awaiting_dispatch",
			"title": "A field-reported task has sat in Available for more than 24 hours without being claimed",
			"category": "Workforce",
			"target_doctype": "Farm Task",
			"requires_doctypes": "Farm Task",
			"date_field": "reported_at",
			"date_field_role": DATE_ROLE_CLOCK,
			"default_severity": SEVERITY_DEFAULT,
			"due_date_mode": DUE_FROM_ANCHOR,
			"threshold_critical_days": -1,
			"threshold_warning_days": 1,
			"cadence_days": 0,
			"scope_filters": [
				{"field": "origin", "op": "eq", "value": "field_reported"},
				{"field": "state", "op": "eq", "value": "Available"},
			],
			"message_template": (
				"Field report {{ row.name }} ('{{ row.task_name }}') has been in Available "
				"state for more than 24 hours since {{ row.reported_at }}. A worker flagged "
				"a problem and nobody has claimed the task. Review and either dispatch "
				"somebody or dismiss the report with a reason."
			),
			"regimes": [],
			"regulation_citations": "",
			"retention_years": 1,
			"audit_packet_types": [],
			"producer_task_template": None,
			"producer_farm_task_type": None,
			"producer_skill_required": "",
			"producer_assigned_to_expression": "",
			"extra_parameters": {},
			"evidence_contract": {"findings_text": True},
			"purpose": (
				"A field report is a worker saying 'I see a problem'. If nobody claims the "
				"resulting task within 24 hours, the report is either being ignored or was "
				"never seen — and an ignored field report teaches the crew that reporting "
				"problems is a waste of their time, which is the fastest way to lose the "
				"eyes this whole feature exists to recruit."
			),
			"kairotic_gate_description": (
				"Fires when a field-reported task (origin = field_reported) has been in "
				"Available state for more than 24 hours. The clock starts at reported_at — "
				"when the worker tapped 'Report a problem' — not at creation, which may "
				"differ by the queue delay. Silences when the task is claimed, completed, "
				"or cancelled."
			),
			"authored_by": AUTHOR_SYSTEM,
			"enabled": 1,
		},
		# ── v0.27.0: I-9 compliance rules ──────────────────────────────────
		{
			"rule_id": "i9_verification_overdue",
			"title": "I-9 employer verification is overdue — hired more than 3 business days ago",
			"category": "Workforce",
			"target_doctype": "I-9 Form",
			"requires_doctypes": "I-9 Form",
			"date_field": "hire_date",
			"date_field_role": DATE_ROLE_CLOCK,
			"default_severity": SEVERITY_DEFAULT,
			"due_date_mode": DUE_FROM_ANCHOR,
			"threshold_critical_days": 5,
			"threshold_warning_days": 3,
			"cadence_days": 0,
			"scope_filters": [
				{"field": "status", "op": "in", "value": ["Section 1 Complete", "Awaiting Verification"]},
			],
			"message_template": (
				"I-9 Section 2 verification for {{ row.employee_name }} (hired {{ row.hire_date }}) "
				"is overdue. Federal law requires employer verification within 3 business days of "
				"the hire date. Complete Section 2 immediately."
			),
			"regimes": [],
			"regulation_citations": "8 CFR § 274a.2(b)(1)(ii) — employer verification deadline",
			"retention_years": 3,
			"audit_packet_types": [],
			"producer_task_template": None,
			"producer_farm_task_type": None,
			"producer_skill_required": "",
			"producer_assigned_to_expression": "",
			"extra_parameters": {},
			"evidence_contract": {},
			"purpose": (
				"Federal regulation requires that Section 2 of Form I-9 be completed within "
				"3 business days of the employee's first day of work. An overdue verification "
				"is a recordable violation whether the employee turns out to be authorized or not."
			),
			"kairotic_gate_description": (
				"Fires when an I-9 Form is in 'Section 1 Complete' or 'Awaiting Verification' "
				"status and the hire date was more than 3 business days ago. Silences when "
				"Section 2 is completed."
			),
			"authored_by": AUTHOR_SYSTEM,
			"enabled": 1,
		},
		{
			"rule_id": "work_authorization_expiring",
			"title": "Employee work authorization is expiring",
			"category": "Workforce",
			"target_doctype": "I-9 Form",
			"requires_doctypes": "I-9 Form",
			"date_field": "alien_work_authorization_expiry",
			"date_field_role": DATE_ROLE_CLOCK,
			"default_severity": SEVERITY_DEFAULT,
			"due_date_mode": DUE_FROM_ANCHOR,
			"threshold_critical_days": 30,
			"threshold_warning_days": 90,
			"cadence_days": 0,
			"scope_filters": [
				{"field": "status", "op": "in", "value": ["Complete", "Reverification Needed"]},
				{
					"field": "citizenship_status",
					"op": "in",
					"value": ["Alien Authorized to Work", "Lawful Permanent Resident"],
				},
			],
			"message_template": (
				"Work authorization for {{ row.employee_name }} expires on "
				"{{ row.alien_work_authorization_expiry }}. Initiate reverification before "
				"the expiration date to maintain employment eligibility."
			),
			"regimes": [],
			"regulation_citations": "8 CFR § 274a.2(b)(1)(viii) — reverification of employment authorization",
			"retention_years": 3,
			"audit_packet_types": [],
			"producer_task_template": None,
			"producer_farm_task_type": None,
			"producer_skill_required": "",
			"producer_assigned_to_expression": "",
			"extra_parameters": {},
			"evidence_contract": {},
			"purpose": (
				"An employee whose work authorization expires without reverification cannot "
				"legally continue working. The employer must reverify before the expiration "
				"date — not after. This rule provides lead time for the process."
			),
			"kairotic_gate_description": (
				"Fires when a Complete or Reverification Needed I-9 for an alien authorized "
				"to work has a work authorization expiry within 90 days (Warning) or 30 days "
				"(Critical). Silences when reverification is completed."
			),
			"authored_by": AUTHOR_SYSTEM,
			"enabled": 1,
		},
		{
			"rule_id": "i9_retention_destruction_eligible",
			"title": "I-9 is past retention date and eligible for destruction",
			"category": "Workforce",
			"target_doctype": "I-9 Form",
			"requires_doctypes": "I-9 Form",
			"date_field": "retention_until",
			"date_field_role": DATE_ROLE_CLOCK,
			"default_severity": "Info",
			"due_date_mode": DUE_FROM_ANCHOR,
			"threshold_critical_days": -1,
			"threshold_warning_days": 0,
			"cadence_days": 0,
			"scope_filters": [
				{"field": "status", "op": "ne", "value": "Destroyed"},
			],
			"message_template": (
				"I-9 for {{ row.employee_name }} has passed its retention date "
				"({{ row.retention_until }}) and is eligible for destruction. Destroy or "
				"archive per your retention policy."
			),
			"regimes": [],
			"regulation_citations": "8 CFR § 274a.2(b)(2)(i)(A) — I-9 retention period",
			"retention_years": 0,
			"audit_packet_types": [],
			"producer_task_template": None,
			"producer_farm_task_type": None,
			"producer_skill_required": "",
			"producer_assigned_to_expression": "",
			"extra_parameters": {},
			"evidence_contract": {},
			"purpose": (
				"An I-9 must be retained for either 3 years after the date of hire or "
				"1 year after termination, whichever is later. After that date the employer "
				"MAY destroy it, and in many cases SHOULD to limit exposure in an audit. "
				"This rule surfaces the decision at the right time."
			),
			"kairotic_gate_description": (
				"Fires when an I-9 Form that is not Destroyed has passed its retention_until "
				"date. An informational alert — it surfaces the choice, it does not make it."
			),
			"authored_by": AUTHOR_SYSTEM,
			"enabled": 1,
		},
		# ── v0.28.0: W-4 compliance ─────────────────────────────────────────
		{
			"rule_id": "employee_missing_w4",
			"title": "Active employee has no current-year W-4 on file",
			"category": "Workforce",
			"target_doctype": "Employee",
			"requires_doctypes": "Employee, W-4 Form",
			"requires_fields": "w4_status",
			"date_field": "date_of_joining",
			"date_field_role": DATE_ROLE_STATE,
			"default_severity": "Warning",
			"due_date_mode": DUE_NONE,
			"threshold_critical_days": -1,
			"threshold_warning_days": -1,
			"cadence_days": 0,
			"scope_filters": [
				{"field": "status", "op": "eq", "value": "Active"},
				{"field": "w4_status", "op": "isnull"},
			],
			"message_template": (
				"{{ row.employee_name }} ({{ row.name }}) is an active employee with no "
				"current-year W-4 on file. Federal withholding cannot be calculated without "
				"a W-4. Submit one with submit_w4."
			),
			"regimes": [],
			"regulation_citations": "26 USC § 3402(f) — employee withholding certificate requirement",
			"retention_years": 0,
			"audit_packet_types": [],
			"producer_task_template": None,
			"producer_farm_task_type": None,
			"producer_skill_required": "",
			"producer_assigned_to_expression": "",
			"extra_parameters": {},
			"evidence_contract": {},
			"purpose": (
				"Every employee must have a Form W-4 on file for the employer to calculate "
				"federal income tax withholding. Without one, the employer cannot run payroll "
				"in compliance with IRS requirements."
			),
			"kairotic_gate_description": (
				"Fires for any Active employee who has no W-4 Form with status Active. "
				"Checked by querying the W-4 Form table for a matching employee + Active status."
			),
			"authored_by": AUTHOR_SYSTEM,
			"enabled": 1,
		},
		{
			"rule_id": "w4_tax_year_outdated",
			"title": "Employee's active W-4 is for a prior tax year",
			"category": "Workforce",
			"target_doctype": "W-4 Form",
			"requires_doctypes": "W-4 Form",
			"date_field": "effective_date",
			"date_field_role": DATE_ROLE_STATE,
			"default_severity": "Info",
			"due_date_mode": DUE_NONE,
			"threshold_critical_days": -1,
			"threshold_warning_days": -1,
			"cadence_days": 0,
			"scope_filters": [
				{"field": "status", "op": "eq", "value": "Active"},
				{"field": "tax_year", "op": "lt", "value": "{{current_year}}"},
			],
			"message_template": (
				"{{ row.employee_name }}'s active W-4 is for tax year {{ row.tax_year }}, "
				"which is a prior year. This may be fine — employees are not required to "
				"file a new W-4 annually — but review whether withholding is still correct."
			),
			"regimes": [],
			"regulation_citations": "IRS Publication 15-T — employers may continue to use a prior-year W-4",
			"retention_years": 0,
			"audit_packet_types": [],
			"producer_task_template": None,
			"producer_farm_task_type": None,
			"producer_skill_required": "",
			"producer_assigned_to_expression": "",
			"extra_parameters": {},
			"evidence_contract": {},
			"purpose": (
				"An employee's W-4 from a prior tax year is valid but may result in "
				"incorrect withholding if their circumstances changed. This informational "
				"rule surfaces them for review without requiring action."
			),
			"kairotic_gate_description": (
				"Fires for any Active W-4 Form whose tax_year is less than the current "
				"calendar year. Informational — some are fine, flagged for review."
			),
			"authored_by": AUTHOR_SYSTEM,
			"enabled": 1,
		},
		# ── v0.55.0: the boxes with nobody's name in them ───────────────────
		#
		# THREE RULES ABOUT ONE FACT, AND THE FACT IS NOT "THE FORM IS
		# INCOMPLETE". Every other I-9 and W-4 rule in this file watches a CLOCK:
		# verification is overdue, authorization expires, retention has run out.
		# These three watch a BOX. A form can be filled in perfectly, filed on
		# time, and carry no signature at all — and an I-9 whose attestation
		# nobody signed is not a late I-9, it is a form that attests to nothing.
		# 8 CFR 274a.2(b)(1)(i)(A) and (ii)(B) ask for a signature under penalty
		# of perjury from two different people, and neither of them is the
		# software.
		#
		# WHY EACH IS A SEPARATE RULE RATHER THAN ONE `i9_unsigned` WITH THREE
		# BRANCHES. The two boxes are signed by two different people at two
		# different moments, and the whole point of these rules is the task each
		# raises: Section 1 goes to whoever supervises the new hire, because the
		# person who has to be found is the EMPLOYEE; Section 2 goes to an
		# authorized signer, because the person who has to be found is the
		# EMPLOYER's representative. One rule covering both would raise one task
		# for two errands and land it on one of the two wrong people.
		#
		# STATE, NOT CLOCK, on all three. `date_field_role` is `State` and both
		# thresholds are -1, so nothing bands and nothing counts down. The
		# deadline these forms live under is already watched by
		# `i9_verification_overdue`; a missing signature does not become more
		# missing on the fourth day, and stacking a second countdown on the same
		# record would put two rows on the calendar for one afternoon's errand.
		{
			"rule_id": "i9_section_1_unsigned",
			"title": "I-9 Section 1 was completed but carries no employee signature",
			"category": "Workforce",
			"target_doctype": "I-9 Form",
			"requires_doctypes": "I-9 Form",
			# READ FOR THE MESSAGE. The role below is what says so, and the pair of
			# -1 thresholds is the belt to its braces: hire_date is the day the
			# signature was due, and printing it is the whole of its job here.
			"date_field": "hire_date",
			"date_field_role": DATE_ROLE_STATE,
			"default_severity": "Critical",
			"due_date_mode": DUE_NONE,
			"threshold_critical_days": -1,
			"threshold_warning_days": -1,
			"cadence_days": 0,
			"scope_filters": [
				# EVERY STATUS PAST DRAFT, stated as an exclusion rather than a
				# list. A Draft has not been submitted, so there is nothing to have
				# signed; a Destroyed form is past its retention period and is not
				# work anybody should be sent to do. Naming the five in between
				# would be a list to keep in step with the doctype's Select, and
				# the day somebody adds a sixth status this rule would go quiet on
				# it without saying so.
				{"field": "status", "op": "nin", "value": ["Draft", "Destroyed"]},
				{"field": "section_1_signature", "op": "isnull"},
				# v0.138.0. AND NOT ONE THE PHONE ALREADY ATTESTED. The column
				# above is a picture of a signature captured at a pad this server
				# was holding, and on the shipped architecture nothing fills it any
				# more: the app builds and seals the retained page itself and files
				# it with `attach_signed_i9`. Without this group the rule raised a
				# Critical on every phone-built I-9 in the register — forms that are
				# signed, sealed and retained — which is worse than a rule that
				# raises nothing: an inspection board where the Criticals are known
				# to be wrong is a board nobody reads. `i9_attestation_group` names
				# the same three columns `tools/i9.phone_attested` reads, from the
				# same place, so the sweep and the status cannot disagree.
				i9_attestation_group(),
			],
			"message_template": (
				"Section 1 of {{ row.employee_name }}'s I-9 ({{ row.name }}) has no employee "
				"signature. The employee attests to their own citizenship or immigration status "
				"under penalty of perjury, and that attestation was due on their first day of "
				"work ({{ row.hire_date }}). Collect the signature with collect_form_signature."
			),
			"regimes": [],
			"regulation_citations": (
				"8 CFR § 274a.2(b)(1)(i)(A) — employee attestation, signed no later than the "
				"first day of employment"
			),
			"retention_years": 3,
			"audit_packet_types": [],
			"producer_task_template": None,
			"producer_farm_task_type": "Hiring",
			"producer_skill_required": "hr_admin",
			"producer_assigned_to_expression": "",
			"extra_parameters": {
				"producer_task_what": "Collect I-9 Section 1 signature",
				"producer_task_minutes": 15,
				"producer_task_subject_field": "employee_name",
				"producer_assignee_resolver": "employee_supervisor",
				"signature_doctype": "I-9 Form",
				"signature_field": "section_1_signature",
			},
			"evidence_contract": {"signature": True},
			"purpose": (
				"An unsigned Section 1 is the single commonest I-9 finding an ICE inspection "
				"writes up, and it is a substantive violation rather than a technical one: "
				"without the employee's own signature the form asserts nothing about their "
				"work authorization. It is also the cheapest to fix while the person is still "
				"on the property, which is what makes it worth raising as work rather than "
				"noting in a report."
			),
			"kairotic_gate_description": (
				"Fires on a BOX, not a date. An I-9 past Draft whose section_1_signature is "
				"empty raises this the moment the sweep sees it, whatever the hire date says — "
				"the deadline is already watched by i9_verification_overdue and a second "
				"countdown on the same record would be two rows for one errand. Silences by "
				"itself the moment a signature is attached, and does not fire on a Draft "
				"(nothing has been submitted to sign) or on a Destroyed form (past retention). "
				"From v0.138.0 it does not fire on a form the handset attested either: a sealed "
				"signed_pdf on the record WITH both section_1_signed_at and section_2_signed_at "
				"beside it is the employer asserting both signatures were made and producing the "
				"retained page to show for it. All three or none — a scan on its own could be a "
				"blank form, and timestamps on their own show nothing."
			),
			"authored_by": AUTHOR_SYSTEM,
			"enabled": 1,
		},
		{
			"rule_id": "i9_section_2_unsigned",
			"title": "I-9 Section 2 was verified but carries no employer signature",
			"category": "Workforce",
			"target_doctype": "I-9 Form",
			"requires_doctypes": "I-9 Form",
			"date_field": "verification_date",
			"date_field_role": DATE_ROLE_STATE,
			"default_severity": "Critical",
			"due_date_mode": DUE_NONE,
			"threshold_critical_days": -1,
			"threshold_warning_days": -1,
			"cadence_days": 0,
			"scope_filters": [
				# THE GATE IS `verification_date`, NOT `status`, and the difference
				# is what the rule is about. A form still awaiting verification has
				# an empty Section 2 and nothing to have signed — raising on it
				# would be raising `i9_verification_overdue` a second time under a
				# different name. A form carrying a verification DATE is one where
				# somebody looked at the documents; this rule asks whether they
				# then put their name to what they saw.
				{"field": "verification_date", "op": "isnotnull"},
				{"field": "section_2_signature", "op": "isnull"},
				{"field": "status", "op": "ne", "value": "Destroyed"},
				# Section 1's twin, and see the note there. The evidence is the same
				# page: it carries both attestations or it is not the retained form.
				i9_attestation_group(),
			],
			"message_template": (
				"Section 2 of {{ row.employee_name }}'s I-9 ({{ row.name }}) records a "
				"verification on {{ row.verification_date }} by {{ row.verifier_name }} and "
				"carries no employer signature. The examiner attests under penalty of perjury "
				"that they examined the documents; an unsigned attestation is not one. Collect "
				"the signature with collect_form_signature."
			),
			"regimes": [],
			"regulation_citations": (
				"8 CFR § 274a.2(b)(1)(ii)(B) — employer or authorized representative attestation"
			),
			"retention_years": 3,
			"audit_packet_types": [],
			"producer_task_template": None,
			"producer_farm_task_type": "Hiring",
			"producer_skill_required": "hr_admin",
			"producer_assigned_to_expression": "",
			"extra_parameters": {
				"producer_task_what": "Collect I-9 Section 2 signature",
				"producer_task_minutes": 15,
				"producer_task_subject_field": "employee_name",
				# NOT the supervisor. Section 2 is signed by whoever examined the
				# documents on the employer's behalf, and `tools/signers.py` holds
				# the roster of who that is allowed to be.
				"producer_assignee_resolver": "i9_authorized_signer",
				"signature_doctype": "I-9 Form",
				"signature_field": "section_2_signature",
			},
			"evidence_contract": {"signature": True},
			"purpose": (
				"Section 2 is the employer's own attestation — that a named person examined "
				"the documents this form lists and found them to relate to this employee. "
				"Unsigned, the operation has recorded that it checked and has produced no "
				"evidence that anybody did, which is the finding an auditor writes up against "
				"the EMPLOYER rather than against the worker."
			),
			"kairotic_gate_description": (
				"Fires when a verification_date is on the record and section_2_signature is "
				"empty — somebody examined the documents and nobody signed for it. A form with "
				"no verification date raises nothing here, because Section 2 has not been "
				"filled in at all and that is i9_verification_overdue's question. Silences the "
				"moment a signature is attached — or, from v0.138.0, when the handset files the "
				"sealed signed_pdf with both section_N_signed_at moments recorded beside it, "
				"which is the same page carrying the same two attestations Section 1 is spared "
				"by."
			),
			"authored_by": AUTHOR_SYSTEM,
			"enabled": 1,
		},
		{
			"rule_id": "w4_signature_missing",
			"title": "An active W-4 carries no employee signature",
			"category": "Workforce",
			"target_doctype": "W-4 Form",
			"requires_doctypes": "W-4 Form",
			"date_field": "effective_date",
			"date_field_role": DATE_ROLE_STATE,
			# WARNING, WHERE THE TWO I-9 RULES ARE CRITICAL, and the gap is the
			# consequence rather than the tidiness of the file. An unsigned I-9 is
			# a §274a violation with a per-form civil penalty attached. An unsigned
			# W-4 means the employer must withhold as Single with no adjustments
			# until it is signed — a real obligation, correctable, and nobody is
			# fined for the day it took to notice.
			"default_severity": "Warning",
			"due_date_mode": DUE_NONE,
			"threshold_critical_days": -1,
			"threshold_warning_days": -1,
			"cadence_days": 0,
			"scope_filters": [
				{"field": "status", "op": "eq", "value": "Active"},
				{"field": "signature", "op": "isnull"},
			],
			"message_template": (
				"{{ row.employee_name }}'s active W-4 ({{ row.name }}, tax year "
				"{{ row.tax_year }}) has no employee signature. Form W-4 is signed under "
				"penalties of perjury, and until it is the employer must withhold at the "
				"default rate rather than at the elections on this form. Collect the "
				"signature with collect_form_signature."
			),
			"regimes": [],
			"regulation_citations": (
				"26 CFR § 31.3402(f)(5)-1 — a withholding certificate must be signed under "
				"penalties of perjury; IRS Publication 15 — treat an unsigned W-4 as invalid"
			),
			"retention_years": 4,
			"audit_packet_types": [],
			"producer_task_template": None,
			"producer_farm_task_type": "Hiring",
			"producer_skill_required": "hr_admin",
			"producer_assigned_to_expression": "",
			"extra_parameters": {
				"producer_task_what": "Collect W-4 signature",
				"producer_task_minutes": 10,
				"producer_task_subject_field": "employee_name",
				"producer_assignee_resolver": "employee_supervisor",
				"signature_doctype": "W-4 Form",
				"signature_field": "signature",
			},
			"evidence_contract": {"signature": True},
			"purpose": (
				"An unsigned W-4 is invalid, and an invalid W-4 does not merely sit in a "
				"folder — it changes the withholding the employer is required to apply. "
				"Publication 15 tells the employer to withhold as if the employee were Single "
				"with no adjustments, which for a married picker claiming three dependents is "
				"the difference between a correct cheque and a complaint."
			),
			"kairotic_gate_description": (
				"Fires for any Active W-4 whose signature box is empty. Superseded and Draft "
				"W-4s raise nothing: a superseded form is not the one withholding is computed "
				"from, and a draft has not been filed. Silences the moment a signature is "
				"attached with collect_form_signature. EXPECT IT TO FIRE BROADLY ON THE FIRST "
				"SWEEP AFTER UPGRADE, and that is the finding rather than a fault in the rule: "
				"submit_w4 has never captured the employee's signature, so every W-4 this app "
				"has ever written is unsigned and every one of them is, to the IRS, invalid."
			),
			"authored_by": AUTHOR_SYSTEM,
			"enabled": 1,
		},
		# ── v0.56.0: the employer's own returns ────────────────────────────
		#
		# THE FOURTH SIGNATURE RULE, AND THE ONE WHOSE SCOPE IS THE INTERESTING
		# PART. A Tax Form on this site can be any of six things, and only four
		# of them are signed:
		#
		#   941      Part 5, "Under penalties of perjury, I declare…"  SIGNED
		#   OR-WR    Oregon annual withholding reconciliation          SIGNED
		#   OQ       Oregon quarterly combined report                  SIGNED
		#   WA-ESD   Washington quarterly wage detail                  SIGNED
		#   W-2      no signature line anywhere on the form            NOT SIGNED
		#   1099-NEC no signature line anywhere on the form            NOT SIGNED
		#
		# THE TWO EXCLUSIONS ARE NOT AN OVERSIGHT AND THEY ARE NOT THIS APP
		# BEING CAUTIOUS. Form W-2 has no signature box: the employee copies are
		# statements rather than declarations, and the penalties-of-perjury
		# declaration for a whole W-2 batch is made ONCE, on the Form W-3
		# transmittal that goes to the SSA with them. Form 1099-NEC is the same
		# shape with Form 1096 as its transmittal. A rule that raised on every
		# W-2 would raise on every employee, every January, for a box that does
		# not exist on the page — which is the fastest way to teach an operation
		# that this calendar is noise.
		#
		# NEITHER W-3 NOR 1096 IS A `form_type` ON THIS DOCTYPE YET. When one is
		# added, it belongs in the list below and nowhere else: that is the whole
		# of what this rule would need to cover it.
		{
			"rule_id": "tax_form_signature_missing",
			"title": "An employer tax return is generated or filed and carries no signature",
			"category": "Filings",
			"target_doctype": "Tax Form",
			"requires_doctypes": "Tax Form",
			"requires_fields": "signature",
			"date_field": "period_end",
			"date_field_role": DATE_ROLE_STATE,
			"default_severity": "Warning",
			"due_date_mode": DUE_NONE,
			"threshold_critical_days": -1,
			"threshold_warning_days": -1,
			"cadence_days": 0,
			"scope_filters": [
				{"field": "form_type", "op": "in", "value": ["941", "OR-WR", "OQ", "WA-ESD"]},
				# A DRAFT HAS NOTHING TO SIGN. The figures are still moving, and a
				# declaration signed over numbers that then changed is worse than
				# an unsigned draft — it is an attestation to a return nobody
				# filed. Generated, Filed and Amended are the three states where
				# the numbers are settled.
				{"field": "status", "op": "in", "value": ["Generated", "Filed", "Amended"]},
				{"field": "signature", "op": "isnull"},
			],
			"message_template": (
				"{{ row.form_type }} for {{ row.company }} covering {{ row.period_start }} to "
				"{{ row.period_end }} ({{ row.name }}) is {{ row.status }} and carries no "
				"signature. This return is made under penalty of perjury by an officer or "
				"authorised agent of the employer; an unsigned one is not a complete return "
				"even where the agency accepted the transmission. Sign it with "
				"collect_form_signature."
			),
			"regimes": [],
			"regulation_citations": (
				"26 USC § 6065 — returns must contain a written declaration made under "
				"penalties of perjury; IRS Form 941 Part 5; ORS 316.202; RCW 50.12.070"
			),
			"retention_years": 4,
			"audit_packet_types": [],
			"producer_task_template": None,
			"producer_farm_task_type": "Compliance-Audit",
			# POOLED, NOT ROUTED TO A NAME, and it is the one signature rule of
			# the five that is. The other four know who signs: an employee signs
			# their own I-9 and W-4, and `tools/signers.py` records who may sign
			# the employer's half of those two. A 941 is signed by an OFFICER of
			# the employer, and this app has no register of officers — the
			# Authorized Signer roster carries `can_sign_i9` and `can_sign_w4`
			# and says nothing about tax returns. Inventing a routing off the
			# wrong flag would send a return to somebody whose authorisation
			# covers a different form; the honest answer is the compliance_admin
			# pool, where somebody who knows who signs picks it up.
			"producer_skill_required": "compliance_admin",
			"producer_assigned_to_expression": "",
			"extra_parameters": {
				"producer_task_what": "Sign the employer tax return before it is filed",
				"producer_task_minutes": 20,
				# NO `producer_task_subject_field`. The other four rules are about
				# a person and name them in the title; a 941 is about a COMPANY
				# and a quarter, and there is no one column that says so — the
				# alert message names the form, the entity and the period, and it
				# becomes the task's notes.
				"signature_doctype": "Tax Form",
				"signature_field": "signature",
			},
			"evidence_contract": {"signature": True},
			"purpose": (
				"26 USC § 6065 makes the declaration part of the return rather than a "
				"formality attached to it: a return without one has not been made, whatever "
				"the transmission receipt says. The exposure is not the penalty on the "
				"signature — it is that an unsigned return leaves the filing itself open, and "
				"the operation finds out from a notice rather than from its own records."
			),
			"kairotic_gate_description": (
				"Fires on a BOX, for the four return types that have one. A Tax Form of type "
				"941, OR-WR, OQ or WA-ESD that has reached Generated, Filed or Amended with an "
				"empty signature column raises this. W-2 and 1099-NEC raise NOTHING and never "
				"will: neither form has a signature line — the declaration for a batch of them "
				"is made once on the W-3 or 1096 transmittal — so a rule that raised on them "
				"would raise on every employee every January for a box that is not on the "
				"page. A Draft raises nothing either: its figures are still moving, and a "
				"declaration signed over numbers that then changed is worse than no signature. "
				"Silences the moment a signature is attached."
			),
			"authored_by": AUTHOR_SYSTEM,
			"enabled": 1,
		},
		# ── v0.69.0: the two intervals a finished spray opens ──────────────
		#
		# BOTH READ A COLUMN THE COMPLETION STAMPED, and that is the whole design.
		# The label facts live on the Item (`rei_hours`, `phi_days`); the WINDOW is
		# a fact about a block and exists from the moment the sprayer switches off,
		# so `complete_farm_task` computes it once — longest product in the tank —
		# and writes `rei_expires_at` and `phi_clears_on` onto the task. These two
		# rules then do nothing cleverer than ask whether the window is still open.
		#
		# WHY THAT IS BETTER THAN A RULE THAT JOINS. A declarative rule walks one
		# doctype; reaching from a spray to its chemicals to their labels is a join
		# the vocabulary does not have and should not grow for this — and a rule
		# that recomputed the interval on every sweep would answer differently the
		# day somebody corrected a product's label, retroactively reopening or
		# shutting a block on a date that has already passed. The window is stamped
		# at the kairotic moment and does not move afterwards, which is what a
		# posting notice on a gate means.
		#
		# THE SEVERITIES ARE THE CONTRACT'S, MAPPED ONTO THIS APP'S THREE. Compliance
		# Alert has Critical/Warning/Info and nothing between: REI is Critical
		# (somebody walks into a treated block and it is an injury), PHI is Warning
		# (a picking plan that has to change, weeks out, and nobody is hurt by it).
		{
			"rule_id": "rei_active_block_entry",
			"title": "A sprayed block is inside its restricted-entry interval",
			"category": "Spray and Pesticides",
			"target_doctype": "Farm Task",
			"requires_doctypes": "Farm Task",
			# THE EXPIRY, READ FOR THE MESSAGE AND BANDING NOTHING. The role below
			# is what says so; the filter underneath is what makes it fire.
			"date_field": "rei_expires_at",
			"date_field_role": DATE_ROLE_STATE,
			"default_severity": "Critical",
			# NO DUE DATE, AND IT IS A TYPE DECISION AS WELL AS A JUDGEMENT.
			# `Compliance Alert.due_date` is a Date; an REI expires at an HOUR, and
			# putting "2026-08-14 18:00:00" into a Date column would either be
			# truncated to a day — which is the whole error this rule exists to
			# stop — or refused. The hour is in the message, where it is readable.
			"due_date_mode": DUE_NONE,
			"threshold_critical_days": -1,
			"threshold_warning_days": -1,
			"cadence_days": 0,
			"missing_date_behaviour": ON_MISSING_SKIP,
			"scope_filters": [
				{"field": "task_type", "op": "eq", "value": "Spray"},
				# A window is only ever stamped by a completion, so this pair is
				# belt and braces rather than the mechanism — but a task somebody
				# re-opened by hand should stop posting a gate.
				{"field": "state", "op": "in", "value": ["Awaiting-Review", "Completed"]},
				{"field": "rei_expires_at", "op": "isnotnull"},
				# THE AUTO-DISMISS, AND IT IS THIS LINE. `{{current_datetime}}`
				# resolves at SWEEP time; ISO datetimes compare as text in the right
				# order; so the row matches exactly while the interval is open and
				# stops matching the sweep after it closes — at which point
				# `alerts/base.py` dismisses the alert because nothing observed it.
				# `gte` rather than `gt` so the alert stands THROUGH the expiry
				# instant rather than a moment before it.
				{"field": "rei_expires_at", "op": "gte", "value": "{{current_datetime}}"},
			],
			"message_template": (
				"REI active — no worker entry until {{ row.rei_expires_at }} on "
				"{{ row.location or row.task_name }}"
				"{% if row.rei_source_item %} ({{ row.rei_source_item }}, "
				"sprayed {{ row.spray_completed_at }}){% endif %}. Post the block and keep "
				"everybody out without PPE until the interval closes — 40 CFR 170.407."
			),
			"regimes": ["WPS", "OR-OSHA"],
			"regulation_citations": "EPA WPS 40 CFR 170.407 restricted-entry interval; OAR 437-004-6501",
			"retention_years": 2,
			"audit_packet_types": ["EPA", "OSHA"],
			# NOTHING TO DISPATCH. The fix for an REI is time passing, and a task
			# asking somebody to go and make four hours elapse is the kind of item
			# that teaches a crew to stop reading the board. `api/rectify.py`
			# answers this one with a refusal that says which kind of refusal it is.
			"producer_task_template": None,
			"producer_farm_task_type": None,
			"producer_skill_required": "",
			"producer_assigned_to_expression": "",
			"extra_parameters": {},
			"evidence_contract": {},
			"purpose": (
				"The restricted-entry interval is the one number in a spray record that a person "
				"can be hurt by. Every other field on the application answers a question later; "
				"this one answers 'can the crew go in' tomorrow morning, and the crew boss making "
				"that call is usually not the applicator who read the label."
			),
			"kairotic_gate_description": (
				"Fires on a CLOCK THAT IS STILL RUNNING, not on a date. A completed spray task "
				"carries the expiry its own tank mix opened — the longest REI of the products in "
				"it, from the hour the spray actually finished — and this rule raises for exactly "
				"as long as that instant is in the future. It silences BY ITSELF, to the hour, "
				"when the interval closes: no work, no dismissal, nobody to remind. A spray of "
				"nothing restricted raises nothing at all, because no window was stamped."
			),
			"authored_by": AUTHOR_SYSTEM,
			"enabled": 1,
		},
		{
			"rule_id": "phi_harvest_window",
			"title": "A sprayed block is inside its pre-harvest interval",
			"category": "Spray and Pesticides",
			"target_doctype": "Farm Task",
			"requires_doctypes": "Farm Task",
			"date_field": "phi_clears_on",
			"date_field_role": DATE_ROLE_STATE,
			# WARNING, WHICH IS THIS APP'S "HIGH". A PHI is weeks of notice about a
			# picking plan; nobody is injured by it and nothing has yet gone wrong.
			# Raising it at Critical beside a live REI would put the two on one
			# board at one level, and the level would stop meaning "stop".
			"default_severity": "Warning",
			# THE DATE THE BLOCK OPENS, ON THE CALENDAR. `phi_clears_on` is a Date
			# and a cadence of zero makes the due date the anchor itself — so a
			# harvest planner reading the compliance calendar sees the pick date
			# rather than having to read it out of a sentence.
			"due_date_mode": DUE_FROM_ANCHOR,
			"threshold_critical_days": -1,
			"threshold_warning_days": -1,
			"cadence_days": 0,
			"missing_date_behaviour": ON_MISSING_SKIP,
			"scope_filters": [
				{"field": "task_type", "op": "eq", "value": "Spray"},
				{"field": "state", "op": "in", "value": ["Awaiting-Review", "Completed"]},
				{"field": "phi_clears_on", "op": "isnotnull"},
				# `gte` AND NOT `gt`, and the day between them is a real decision:
				# the alert stands ON the clearing date and goes away the day after,
				# so a block is never picked on the strength of an alert that
				# vanished at midnight. A day of over-caution against a residue
				# violation on a shipped load is not a close call.
				{"field": "phi_clears_on", "op": "gte", "value": "{{current_date}}"},
			],
			"message_template": (
				"PHI active — no harvest until {{ row.phi_clears_on }} on "
				"{{ row.location or row.task_name }}"
				"{% if row.phi_source_item %} ({{ row.phi_source_item }}, "
				"sprayed {{ row.spray_completed_at }}){% endif %}. A pick inside the interval is "
				"a residue violation on a shipped load."
			),
			"regimes": ["FSMA", "GAP"],
			"regulation_citations": "FIFRA label pre-harvest interval; FDA tolerances 40 CFR 180",
			"retention_years": 2,
			"audit_packet_types": ["EPA", "FSMA", "GAP"],
			"producer_task_template": None,
			"producer_farm_task_type": None,
			"producer_skill_required": "",
			"producer_assigned_to_expression": "",
			"extra_parameters": {},
			"evidence_contract": {},
			"purpose": (
				"The pre-harvest interval is a scheduling fact that becomes a legal one at the "
				"packing house. A load rejected for residue is traced back to a block and a date, "
				"and the operation's own answer has to be a record it kept before the pick rather "
				"than a reconstruction afterwards."
			),
			"kairotic_gate_description": (
				"Fires on a DATE THAT HAS NOT ARRIVED. A completed spray task carries the first "
				"date its block may be picked — the longest PHI of the products in the tank, from "
				"the day the spray finished — and this rule raises while that date is today or "
				"later. It silences by itself the day after: the interval closing IS the fix, and "
				"there is nothing for anybody to do about it in the meantime except plan around "
				"it, which is what the alert is for."
			),
			"authored_by": AUTHOR_SYSTEM,
			"enabled": 1,
		},
	]


def _gate_seed_specs() -> list:
	"""v0.80.0. The IPO-readiness control points, every one of them Advisory.

	Kept behind a function rather than imported at module scope because
	`enforcement` imports `compat` and `errors` only — but the seeder runs during
	`bench migrate`, where a circular import at module load aborts the migration
	for the whole bench rather than for this app. A late import costs nothing and
	cannot do that.

	A GATE RULE IS SEEDED ENABLED AND ADVISORY. Enabled so the control runs and
	starts filling the calendar from the first migrate — an operation should be
	accumulating the evidence for its own enforcement decision before anybody
	configures anything. Advisory so it refuses nothing while it does.
	"""
	from .enforcement import seed_specs as gate_specs

	return list(gate_specs())


def seed_compliance_rules(approver: str = "") -> dict:
	"""One Compliance Rule per shipped rule. Idempotent, and never raises.

	CHECKED BY `rule_id`, AND AN EDITED RULE IS LEFT ALONE. The check is "does
	ANY row hold this rule_id" and not "does a live row" — deliberately, and it
	is the difference between this and a Frappe fixture. A rule somebody disabled
	because their operation does not do it that way stays disabled. One somebody
	superseded with their own version 2 does not get version 1 seeded back beside
	it every migrate, which would give the rule_id two live rows and make the
	sweep's answer depend on sort order.

	`approver` is who the seeded rules are approved by. It has to be somebody,
	because `enabled` is refused without an approver and a seeded rule that
	arrived disabled would silently turn the whole compliance calendar off on
	upgrade — which is the one migration outcome worse than a noisy one.

	NEVER RAISES. It runs inside `bench migrate`, where an exception aborts the
	migration for the whole bench.
	"""
	report = {"created": [], "present": [], "failed": []}
	if not compat.doctype_exists(DOCTYPE):
		return report
	approver = approver or _seed_approver()
	stamped = frappe.utils.now()
	try:
		specs = seed_specs() + declarative_seed_specs() + _gate_seed_specs()
	except Exception as exc:  # pragma: no cover - a site mid-import
		report["failed"].append({"name": "declarative_seed_specs", "reason": f"{type(exc).__name__}: {exc}"})
		specs = seed_specs()
	for spec in specs:
		rule_id = spec["rule_id"]
		try:
			if frappe.db.exists(DOCTYPE, {"rule_id": rule_id}):
				report["present"].append(rule_id)
				continue
			doc = build_rule(
				{
					**spec,
					"human_approved_by": approver,
					"human_approved_on": stamped,
				}
			)
			doc.insert(ignore_permissions=True)
			report["created"].append(rule_id)
		except Exception as exc:  # pragma: no cover - reported, never raised
			report["failed"].append({"name": rule_id, "reason": f"{type(exc).__name__}: {exc}"})
	return report


def _seed_approver() -> str:
	"""Who a migrated rule is approved by: Administrator, which is the truth.

	A rule this app shipped was reviewed by whoever reviewed the release, and the
	site has no record of that person. Attributing it to Administrator says "the
	system installed this" and is checkable; inventing a name would be the one
	thing a provenance field must never do. `authored_by = System` on the same
	row is what distinguishes it from a rule a human here actually read.
	"""
	try:
		if frappe.db.exists("User", "Administrator"):
			return "Administrator"
	except Exception:  # pragma: no cover
		pass
	return "Administrator"

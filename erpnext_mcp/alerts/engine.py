# SPDX-License-Identifier: MIT
"""Turning a Compliance Rule record into something the sweep can run.

THE SWEEP DID NOT CHANGE IN v0.22.0 AND THAT IS THE POINT. `base.py` still walks
a set of `Rule` objects, still asks each one the same question — *is this
condition true right now* — still keys each alert on the rule and the record and
nothing that moves daily, still auto-dismisses what it did not observe. All that
changed is WHERE THE RULE SET COMES FROM: it used to be a dict populated by
`register()` at import time, and it is now assembled from Compliance Rule rows.

This module is the assembly. It reads a row and hands back a `Rule` whose `scan`
is one of three things:

  DECLARATIVE   a closure over the row, evaluated here: query the target
                doctype, apply the scope filters, compute the days remaining
                against the cadence anchor, pick the severity band, render the
                message. Deterministic, bounded, and identical every time for
                identical data.
  BUILT-IN      the shipped scanner the row names, called with the row so it
                can read the row's own thresholds and citations. The tunables
                are still data; only the shape of the join is code.
  CUSTOM        the row's `custom_python`, run by `sandbox.py`.

NO MODEL RUNS HERE. There is no classifier, no embedding, no natural-language
interpretation of anything at sweep time. A rule fires because a date crossed a
threshold or a column matched a filter, and the report can name which. That is
what makes an alert defensible in front of somebody who is allowed to ask.

FALLING BACK IS DELIBERATE AND IS NOT A SILENT FALLBACK. On a site that has the
app but has not yet migrated the DocType, `rule_set()` returns the rules
`rules.py` registered at import, so the compliance calendar keeps working
through the upgrade window rather than going blank in it — and the sweep report
says, in a sentence, that it ran the shipped definitions rather than the site's.
A calendar that quietly emptied itself during a migrate would be the single
worst failure this app could have.
"""

from __future__ import annotations

import datetime

import frappe

from .. import compat, compliance_rules
from .. import training as regimes_vocabulary
from . import sandbox
from .base import (
	SEVERITY_CRITICAL,
	SEVERITY_INFO,
	SEVERITY_WARNING,
	Observation,
	Rule,
	days_since,
	days_until,
)

#: See `_DateTimes` for why these are bound here rather than in the class body.
_DATE = datetime.date
_DATETIME = datetime.datetime
_TIMEDELTA = datetime.timedelta

#: Rows one declarative rule will read from its target doctype. The same 2000
#: `rules.py` has always used, for the same reason: past it, a rule is scanning
#: an operation this app is not shaped for, and the `RULE_CAP` on observations
#: will have bitten long before.
SCAN_CAP = 2000

#: Fieldtypes that are layout rather than data, and are never selected.
_LAYOUT_FIELDTYPES = (
	"Section Break",
	"Column Break",
	"Tab Break",
	"HTML",
	"Heading",
	"Table",
	"Table MultiSelect",
	"Fold",
	"Button",
)

#: The field a target doctype scopes to a company by, in preference order.
#: `Field` and `Housing Unit` say `owning_entity`; everything else says
#: `company`. Read off the doctype rather than configured on the rule, because
#: it is a property of the doctype and a rule that got it wrong would scope to
#: nothing and look like a clean company.
_COMPANY_FIELDS = ("company", "owning_entity")


# ── the rule set ────────────────────────────────────────────────────────────
def rule_set() -> tuple:
	"""(rules, notes) — the live rule set from records, or the shipped fallback.

	`rules` is `{rule_id: Rule}` exactly as `base.RULES` always was, so every
	caller downstream of it — the sweep, `list_compliance_rules`, the regime
	filter, the dispatch board's alert mapping — is unchanged.
	"""
	from . import rules as shipped

	notes = []
	if not compat.doctype_exists(compliance_rules.DOCTYPE):
		notes.append(
			"This site has no Compliance Rule DocType yet, so the sweep ran the rule definitions "
			"that ship with erpnext_mcp rather than the site's own. That is the upgrade window and "
			"nothing else: run `bench --site <site> migrate` and the rules become records you can "
			"edit. Behaviour is identical either way — the shipped definitions ARE what gets seeded."
		)
		return dict(shipped.RULES), notes

	rows = compliance_rules.rule_rows()
	if not rows:
		notes.append(
			"The Compliance Rule DocType is here and holds no live rule, so the sweep ran the "
			"shipped definitions. On a site mid-migrate that is expected and the seeder fixes it. "
			"If somebody disabled every rule on purpose, that is what an empty calendar means and "
			"this note is how you can tell the two apart."
		)
		return dict(shipped.RULES), notes

	assembled = {}
	for row in rows:
		key = str(row.get("rule_id") or "").strip()
		if not key or key in assembled:
			continue
		# v0.80.0. A GATE IS NOT SWEPT. A rule carrying a `control_point` is
		# consulted at the moment of the transaction by the tool performing it —
		# see `enforcement.py` — and has no scan semantics at all: no target
		# doctype to walk, no date field to age, no condition that is true of a
		# record sitting still. Handing it to `_declarative_scan` would walk
		# whatever `target_doctype` happened to be blank and raise nothing, which
		# is harmless, and would put a "could not be assembled" note in front of
		# an operator every night, which is not.
		if str(row.get("control_point") or "").strip():
			continue
		try:
			assembled[key] = rule_from_row(row)
		except Exception as exc:
			notes.append(
				f"Compliance Rule {row.get('name')} ({key}) could not be assembled and did not run: "
				f"{type(exc).__name__}: {exc}. It raised nothing AND DISMISSED NOTHING."
			)
	return assembled, notes


def rule_from_row(row: dict) -> Rule:
	"""One `Rule` from one Compliance Rule record."""
	row = dict(row)
	row.setdefault("regimes", compliance_rules.regimes_of(row.get("name")))
	shape = compliance_rules.shape_of(row)
	requires = tuple(
		compliance_rules.names_list(row.get("requires_doctypes"))
		or ([str(row.get("target_doctype"))] if row.get("target_doctype") else [])
	)
	regimes = tuple(regimes_vocabulary.parse(row.get("regimes")) or ("Internal",))

	if shape == compliance_rules.SHAPE_BUILTIN:
		scan = _builtin_scan(row)
	elif shape == compliance_rules.SHAPE_CUSTOM:
		scan = _custom_scan(row)
	else:
		scan = _declarative_scan(row)

	return Rule(
		key=str(row.get("rule_id")),
		title=str(row.get("title") or row.get("rule_id")),
		category=str(row.get("category") or "Records"),
		scan=scan,
		kairotic_gate=str(row.get("kairotic_gate_description") or ""),
		purpose=str(row.get("purpose") or ""),
		requires=requires,
		framework=str(row.get("regulation_citations") or ""),
		regimes=regimes,
		record=str(row.get("name") or ""),
		version=int(row.get("version") or 1),
		shape=shape,
		authored_by=str(row.get("authored_by") or ""),
	)


def preview(row: dict, context: dict) -> dict:
	"""Run one rule and report what it WOULD observe, writing nothing.

	The read behind `test_compliance_rule`. It is the same code path the sweep
	takes, deliberately — a dry run that used a second implementation would be a
	dry run that could disagree with the real one, which is the one property a
	dry run must not have.
	"""
	rule = rule_from_row(row)
	observations = list(rule.scan(context) or [])
	return {
		"rule_id": rule.key,
		"shape": compliance_rules.shape_of(row),
		"observed": len(observations),
		"observations": [
			{
				"source_doctype": observation.source_doctype,
				"source_docname": observation.source_docname,
				"severity": observation.severity,
				"due_date": observation.due_date or None,
				"company": observation.company or None,
				"category": observation.category or rule.category,
				"regimes": (
					list(observation.regimes) if observation.regimes is not None else list(rule.regimes)
				),
				"message": observation.message,
				"would_be_alert": f"{rule.key}:{observation.source_doctype}:{observation.source_docname}",
			}
			for observation in observations
		],
		"computation_warnings": sorted(
			{warning for observation in observations for warning in (observation.computation_warnings or ())}
		),
	}


# ── the declarative evaluator ───────────────────────────────────────────────
#
# THE ORDER THE GATES ARE EVALUATED IN IS PART OF THE CONTRACT, and v0.22.1's
# primitives made it worth stating rather than leaving to read off the code:
#
#   1. SCOPE FILTERS      define the population. Cheapest, purely local to the
#                         row, and every later gate is a claim ABOUT that
#                         population — a rule scoped to cabins should not be
#                         asking a question about a shower block at all.
#   2. THE GATE DATE      is this row's condition RIPE — is anybody spraying
#                         this block? A row nothing is happening to raises
#                         nothing however stale its other dates.
#   3. THE LATEST CHILD   v0.22.5, and it sits here for the same reason as 2: it
#                         is a claim about whether the condition EXISTS at all,
#                         and it is the cheaper of the two questions about other
#                         rows because its index is keyed on the candidate.
#   4. SUPERSESSION       is this finding STILL the latest word on its subject?
#                         The one gate that reads other rows to decide whether a
#                         TRUE thing has stopped mattering, and therefore the one
#                         worth running last, on the smallest set of candidates
#                         the first three leave.
#   5. THE CLOCK          how far past due, and therefore which severity — or,
#                         on a `State` rule, nothing at all: the gates above have
#                         already decided, and the severity is `default_severity`.
#
# A different order would not merely be slower. Running supersession before the
# scope filters would mean a rule narrowed to one company reading another's
# records to decide what it can see, which is the kind of thing nobody notices
# until an auditor asks why an alert went quiet.
def _declarative_scan(row: dict):
	"""A closure that scans the record's target doctype(s) per its own definition."""

	def scan(context: dict) -> list:
		today = context.get("today") or frappe.utils.today()
		company = context.get("company") or ""
		warnings = []
		try:
			filters = compliance_rules.parse_filters(row.get("scope_filters_json"))
		except ValueError as exc:
			warnings.append(f"scope_filters could not be read ({exc}); the rule scanned unscoped.")
			filters = []

		settings = _definition(row, warnings)
		out = []
		for doctype, date_field, target_label in _targets(row, warnings):
			if not compat.doctype_exists(doctype):
				# Skipped, not fatal. A rule reading two camp records on a site that
				# has only one of them should report on the one it has.
				continue
			# A compliance column this app installs on demand. Absent means the rule
			# has nothing to read, which is an EMPTY SCAN and not a failure — the
			# same answer `_scan_i9` has always given, and it matters because an
			# empty scan auto-dismisses stale alerts while a failure would not.
			if any(
				not compat.has_field(doctype, fieldname)
				for fieldname in compliance_rules.names_list(row.get("requires_fields"))
			):
				continue
			out.extend(
				_scan_one_target(
					row, settings, filters, warnings, doctype, date_field, target_label, today, company
				)
			)
		return out

	return scan


def _targets(row: dict, warnings: list) -> list:
	"""(doctype, date_field, label) for every doctype this rule walks.

	One entry for all but one shipped rule. `target_doctypes_json` is the
	exception and it is a small one — see the field's own description.
	"""
	doctype = str(row.get("target_doctype") or "").strip()
	date_field = str(row.get("date_field") or "").strip()
	out = [(doctype, date_field, "")] if doctype else []
	try:
		extra = compliance_rules.parse_target_doctypes(row.get("target_doctypes_json"))
	except ValueError as exc:
		warnings.append(
			f"target_doctypes could not be read ({exc}); the rule walked only {doctype or 'nothing'}."
		)
		return out
	for entry in extra:
		if entry["doctype"] == doctype and out:
			# The primary, restated to carry its label. Replaced rather than added,
			# because scanning one doctype twice would raise every alert twice and
			# the second would collide with the first on the docname.
			out[0] = (doctype, entry["date_field"] or date_field, entry["label"])
			continue
		out.append((entry["doctype"], entry["date_field"] or date_field, entry["label"]))
	return out


def _definition(row: dict, warnings: list) -> dict:
	"""Everything read off the record ONCE, rather than once per candidate row."""

	def blob(parser, raw, label, fallback):
		try:
			return parser(raw, label)
		except ValueError as exc:
			warnings.append(f"{label} could not be read ({exc}); the rule ran without it.")
			return fallback

	return {
		"date_fields": blob(
			compliance_rules.parse_date_fields, row.get("date_fields_json"), "date_fields", []
		),
		"date_role": str(row.get("date_field_role") or compliance_rules.DATE_ROLE_CLOCK),
		"cadence": int(row.get("cadence_days") or 0),
		"window_field": str(row.get("window_field") or "").strip(),
		"on_missing": str(row.get("missing_date_behaviour") or compliance_rules.ON_MISSING_SKIP),
		"due_mode": str(row.get("due_date_mode") or compliance_rules.DUE_FROM_ANCHOR),
		"critical_days": int(row.get("threshold_critical_days") or 0),
		"warning_days": int(row.get("threshold_warning_days") or 0),
		"severity_critical": str(row.get("severity_critical") or SEVERITY_CRITICAL),
		"severity_warning": str(row.get("severity_warning") or SEVERITY_WARNING),
		"severity_expired": str(row.get("severity_expired") or SEVERITY_CRITICAL),
		"default_severity": str(row.get("default_severity") or compliance_rules.SEVERITY_DEFAULT),
		"regimes_field": str(row.get("regimes_from_field") or "").strip(),
		"rule_regimes": list(regimes_vocabulary.parse(row.get("regimes")) or []),
		"template": str(row.get("message_template") or ""),
		"gate_field": str(row.get("gate_date_field") or "").strip(),
		"gate_days": int(row.get("gate_within_days") or 0),
		"gate_scope": str(row.get("gate_scope") or compliance_rules.GATE_DIRECT),
		"gate_table": blob(
			compliance_rules.parse_gate_table, row.get("gate_related_table_json"), "gate_related_table", {}
		),
		"latest_child": blob(
			compliance_rules.parse_latest_child_threshold,
			row.get("latest_child_field_threshold_json"),
			"latest_child_field_threshold",
			{},
		),
		"supersession": blob(
			compliance_rules.parse_supersession,
			row.get("superseded_by_later_clean_json"),
			"superseded_by_later_clean",
			{},
		),
		"regime_heuristics": blob(
			lambda raw, label: compliance_rules.parse_heuristics(raw, label, "regimes"),
			row.get("regime_heuristics_json"),
			"regime_heuristics",
			[],
		),
		"category_heuristics": blob(
			lambda raw, label: compliance_rules.parse_heuristics(raw, label, "category"),
			row.get("category_heuristics_json"),
			"category_heuristics",
			[],
		),
	}


def _scan_one_target(
	row: dict,
	settings: dict,
	filters: list,
	warnings: list,
	doctype: str,
	date_field: str,
	target_label: str,
	today: str,
	company: str,
) -> list:
	company_field = compat.first_field(doctype, *_COMPANY_FIELDS)
	selected = _selectable_fields(doctype)
	present = set(selected)
	db_filters = {company_field: company} if (company and company_field) else {}
	rows = frappe.db.get_all(doctype, filters=db_filters, fields=selected, limit=SCAN_CAP)

	supersession = settings["supersession"]
	# EVERY INDEX IS BUILT ONCE PER TARGET, NOT ONCE PER CANDIDATE. A camp with
	# fifty cabins and four years of history is two queries, not four hundred —
	# and this is the reason the primitive is a field rather than a `custom_python`
	# program, where the obvious way to write it is the per-row query. v0.22.5's
	# child-row index is the same shape and matters more, not less: twelve open
	# shifts each carrying a reading every fifteen minutes is one query and not
	# twelve, and it is the query that would otherwise run every sweep of the day.
	clean = _clean_index(doctype, date_field, supersession, company, warnings) if supersession else {}
	gate_index = (
		_gate_index(settings["gate_table"], company, warnings)
		if settings["gate_scope"] == compliance_rules.GATE_LATEST_RELATED and settings["gate_table"]
		else {}
	)
	child_config = settings["latest_child"]
	child_index = _latest_child_index(child_config, company, warnings) if child_config else {}
	# Per-company thresholds, resolved once per company that actually appears
	# rather than once per candidate. `thresholds_for` is a cached-Single read, but
	# a rule scanning two thousand rows would still make two thousand of them.
	threshold_cache: dict = {}

	out = []
	for candidate in rows or []:
		candidate = dict(candidate)
		matched, filter_warnings = compliance_rules.row_matches(candidate, filters, present)
		for warning in filter_warnings:
			if warning not in warnings:
				warnings.append(warning)
		if not matched:
			continue

		gate_date, gate_since, gated_in = _gate(candidate, settings, gate_index, today)
		if not gated_in:
			continue

		child_row, crossed, child_ok = _child_gate(
			candidate, child_config, child_index, company_field, threshold_cache
		)
		if not child_ok:
			continue

		if supersession and _superseded(candidate, supersession, clean, date_field):
			continue

		dates = _clock(candidate, settings, date_field, doctype, today, warnings)
		if dates is None:
			continue
		severity, band, window_days = _band(
			dates["days_remaining"],
			candidate,
			settings["window_field"],
			settings["critical_days"],
			settings["warning_days"],
			settings["severity_critical"],
			settings["severity_warning"],
			settings["severity_expired"],
			forced=(settings["date_role"] == compliance_rules.DATE_ROLE_TIMESTAMP),
			state_severity=(
				settings["default_severity"]
				if settings["date_role"] == compliance_rules.DATE_ROLE_STATE
				else ""
			),
		)
		if severity is None:
			continue
		stale = [entry for entry in dates["per_field"] if entry["stale"]]
		if dates["per_field"] and not stale:
			# Plural anchors: the fold said something raises, but no INDIVIDUAL
			# field reaches a band. Nothing to name, so nothing to say.
			continue

		regimes = _regimes_of(candidate, settings)
		category = _category_of(candidate, settings)
		child_context = {}
		if child_config:
			# UNDER ITS OWN NAME AND UNDER A GENERIC ONE. `latest_weather` is what
			# the shipped rule's template reads and is what makes the sentence
			# readable; `latest_child` is what a template written against the
			# primitive rather than against this rule can rely on.
			child_context = {
				child_config["context_key"]: child_row,
				"latest_child": child_row,
				"crossed_conditions": crossed,
			}
		rendered = render_message(
			settings["template"],
			candidate,
			{
				**child_context,
				"today": today,
				"days_remaining": dates["days_remaining"],
				"days_overdue": (None if dates["days_remaining"] is None else -dates["days_remaining"]),
				"days_since_anchor": dates["days_since_anchor"],
				"anchor": dates["anchor"] or None,
				"due_date": dates["due_text"] or None,
				"severity": severity,
				"band": band,
				"window_days": window_days,
				"regimes": regimes if regimes is not None else list(settings["rule_regimes"]),
				"subject": candidate.get("name"),
				"cadence_days": settings["cadence"],
				"threshold_critical_days": settings["critical_days"],
				"threshold_warning_days": settings["warning_days"],
				"rule_title": row.get("title"),
				"regulation_citations": row.get("regulation_citations"),
				# v0.22.1's per-primitive context.
				"target_doctype": doctype,
				"target_label": target_label or doctype,
				"stale_dates": stale,
				"first_stale_label": (stale[0]["label"] if stale else None),
				"gate_date": gate_date or None,
				"gate_days_since": gate_since,
			},
			warnings,
		)

		out.append(
			Observation(
				source_doctype=doctype,
				source_docname=str(candidate.get("name")),
				message=rendered,
				severity=severity,
				due_date=_due_date(settings["due_mode"], dates["due_text"], today),
				company=str(candidate.get(company_field) or "") if company_field else "",
				category=category,
				regimes=regimes,
				computation_warnings=list(warnings) or None,
			)
		)
	return out


# ── primitive 3: the second date, used only as a gate ───────────────────────
def _gate(candidate: dict, settings: dict, gate_index: dict, today: str) -> tuple:
	"""(gate_date, days_since, passes) — is this row's condition ripe at all?

	A ROW WHOSE GATE DATE IS EMPTY IS GATED OUT, and that asymmetry is the whole
	point. `missing_date_behaviour` exists because a cabin nobody has inspected
	is the most overdue cabin there is; a block nobody has ever sprayed is the
	LEAST urgent block there is, because Subpart E is engaged by water contacting
	a crop and not by a date passing. The gate is a claim that the condition
	matters now, and no date is no claim.
	"""
	table = settings["gate_table"]
	related = settings["gate_scope"] == compliance_rules.GATE_LATEST_RELATED and table
	if not (settings["gate_field"] or related):
		return "", None, True
	if related:
		# `subject_key` is the column on THIS row the related rows point at,
		# `name` by default — a spray record names the block, and the block is
		# identified by its docname unless the site keys it some other way.
		value = gate_index.get(str(candidate.get(table.get("subject_key") or "name") or ""))
	else:
		value = candidate.get(settings["gate_field"])
	text = str(value or "").strip()
	since = days_since(today, text) if text else None
	if since is None or since > settings["gate_days"]:
		return text, since, False
	return text, since, True


def _gate_index(table: dict, company: str, warnings: list) -> dict:
	"""subject → the NEWEST gate date on the related doctype. One query."""
	doctype = table["doctype"]
	if not compat.doctype_exists(doctype):
		warnings.append(
			f"the gate reads {doctype}, which this site has not got, so every row was gated OUT and "
			"the rule raised nothing. That is the safe direction for a gate — but it is not the same "
			"as a clean operation, and this note is how you can tell them apart."
		)
		return {}
	company_field = compat.first_field(doctype, *_COMPANY_FIELDS)
	wanted = ["name", table["subject_field"], table["date_field"]]
	wanted.extend(compliance_rules.filter_fields(table["scope_filters"]))
	fields = compat.existing_fields(doctype, dict.fromkeys(wanted))
	rows = frappe.db.get_all(
		doctype,
		filters={company_field: company} if (company and company_field) else {},
		fields=fields,
		limit=SCAN_CAP,
	)
	present = set(fields)
	out: dict = {}
	for entry in rows or []:
		entry = dict(entry)
		matched, _filter_warnings = compliance_rules.row_matches(entry, table["scope_filters"], present)
		if not matched:
			continue
		subject = str(entry.get(table["subject_field"]) or "")
		date = str(entry.get(table["date_field"]) or "").strip()
		if not (subject and date):
			continue
		if date > out.get(subject, ""):
			out[subject] = date
	return out


# ── v0.22.5: the gate about the LATEST ROW of a child table ─────────────────
#
# Standard child columns are selected BY HAND rather than through
# `compat.existing_fields`, and this is not a shortcut. `parent`, `parenttype`
# and `parentfield` are real columns on every child table and are NOT in
# `frappe.get_meta(...).fields` — Frappe keeps them among the standard fields
# every DocType gets for free. Passing them through `has_field` would drop them
# from the SELECT, and a child index with no `parent` column is an index keyed on
# nothing: every row would fold onto one empty subject and the rule would fire on
# whichever shift happened to sort last, on every shift, for ever.
_CHILD_STANDARD_FIELDS = ("name", "parent", "parenttype", "parentfield", "idx", "creation", "modified")


def _latest_child_index(config: dict, company: str, warnings: list) -> dict:
	"""subject → the NEWEST child row for it, whole. One query.

	FOLDED TO A ROW, not to a value, which is the difference between this and
	`_gate_index`. A maximum over `reading_datetime` says when the last reading
	was; it cannot say what the temperature on it was, and that is the entire
	question this primitive exists to ask.
	"""
	doctype = config["child_doctype"]
	if not compat.doctype_exists(doctype):
		warnings.append(
			f"the latest-child gate reads {doctype}, which this site has not got, so every row was "
			"gated OUT and the rule raised nothing. That is the safe direction for a gate — but it "
			"is not the same as a clean operation, and this note is how you can tell them apart."
		)
		return {}

	wanted = [config["parent_field"], config["order_by"]]
	wanted.extend(entry["field"] for entry in config["conditions"])
	wanted.extend(compliance_rules.filter_fields(config["scope_filters"]))
	if config["parentfield"]:
		wanted.append("parentfield")
	fields = ["name"]
	for fieldname in dict.fromkeys(wanted):
		if fieldname in fields:
			continue
		if fieldname in _CHILD_STANDARD_FIELDS or compat.has_field(doctype, fieldname):
			fields.append(fieldname)

	for fieldname in (config["parent_field"], config["order_by"]):
		if fieldname not in fields:
			warnings.append(
				f"the latest-child gate names {doctype}.{fieldname}, which this site has not got, so "
				"no row could be read as the latest and the rule raised nothing."
			)
			return {}

	# Scoped to the sweep's company where the child doctype carries one, the same
	# as every other index here. Most child tables do not — a weather reading
	# belongs to a shift and the shift belongs to an entity — and in that case the
	# subject key does the scoping, because the candidates were already narrowed.
	db_filters = {}
	company_field = compat.first_field(doctype, *_COMPANY_FIELDS)
	if company and company_field:
		db_filters[company_field] = company
	if config["parentfield"] and "parentfield" in fields:
		# A child DOCTYPE can hang off more than one table on more than one parent.
		# Without this the gate would read another table's rows as if they were
		# this one's, which is how a rule about a weather timeline starts answering
		# questions about a crew list.
		db_filters["parentfield"] = config["parentfield"]
	rows = frappe.db.get_all(doctype, filters=db_filters, fields=fields, limit=SCAN_CAP)

	present = set(fields)
	out: dict = {}
	for entry in rows or []:
		entry = dict(entry)
		matched, _filter_warnings = compliance_rules.row_matches(entry, config["scope_filters"], present)
		if not matched:
			continue
		subject = str(entry.get(config["parent_field"]) or "")
		order = str(entry.get(config["order_by"]) or "").strip()
		if not (subject and order):
			# A row with no ordering value cannot be the latest of anything, and
			# guessing would make the answer depend on insertion order.
			continue
		held = out.get(subject)
		if held is None or order > str(held.get(config["order_by"]) or ""):
			out[subject] = entry
	return out


def _child_gate(candidate: dict, config: dict, index: dict, company_field: str, cache: dict) -> tuple:
	"""(latest row, which conditions crossed, passes) for one scanned record.

	A SUBJECT WITH NO CHILD ROW IS GATED OUT. See `parse_latest_child_threshold`:
	a shift whose weather timeline is empty is not a cool shift, it is a shift
	nobody has a reading for, and an alert raised off no reading would be this app
	asserting a fact it does not have.
	"""
	if not config:
		return {}, [], True
	row = index.get(str(candidate.get(config["subject_key"]) or ""))
	if not row:
		return {}, [], False

	company = str(candidate.get(company_field) or "") if company_field else ""
	if company not in cache:
		cache[company] = {
			source: compliance_rules.threshold_from_source(source, company)
			for source in {
				entry["threshold_source"] for entry in config["conditions"] if entry["threshold_source"]
			}
		}
	resolved = cache[company]

	crossed = []
	for entry in config["conditions"]:
		# THE SETTING WINS AND THE LITERAL IS THE FLOOR IT FALLS BACK TO. The
		# literal on the rule is what the regulation says; the setting is what this
		# entity decided, per company, in the one place the v0.19.4 shift sweep
		# already reads it. A site that has not migrated Weather Settings gets the
		# regulation's number rather than nothing — which is the difference between
		# a rule that is conservative and a rule that is silent.
		limit = resolved.get(entry["threshold_source"]) if entry["threshold_source"] else None
		if limit is None:
			limit = entry["threshold"]
		if limit is None:
			continue
		value = row.get(entry["field"])
		if value in (None, ""):
			# A reading with no temperature is not a cool reading. The condition is
			# not met, and `match: all` therefore fails on it — which is the safe
			# direction on a gate that turns into somebody's afternoon.
			continue
		if compliance_rules.passes_threshold(value, entry["op"], limit):
			crossed.append({"field": entry["field"], "op": entry["op"], "threshold": limit, "value": value})

	if config["match"] == compliance_rules.MATCH_ALL:
		return row, crossed, len(crossed) == len(config["conditions"])
	return row, crossed, bool(crossed)


# ── primitive 1: superseded by a later clean record ─────────────────────────
def _clean_index(doctype: str, date_field: str, config: dict, company: str, warnings: list) -> dict:
	"""subject → every date on which a CLEAN record was written for it.

	`date_field` is the TARGET'S, passed in rather than read off the config, so a
	rule walking two doctypes gets each one's own date column — a Housing
	Inspection is dated `inspection_date` and a Detector Test `test_date`, and a
	supersession config naming one of them would silently stop superseding the
	other.
	"""
	target = config.get("doctype") or doctype
	if not compat.doctype_exists(target):
		warnings.append(
			f"supersession reads {target}, which this site has not got, so nothing could supersede "
			"a finding and every open finding stayed open. That is the safe direction — a finding "
			"wrongly left standing is read by somebody, and one wrongly dismissed is not."
		)
		return {}
	subject_field = config["subject_field"]
	date_field = config.get("date_field") or date_field
	state_field = config["clean_state_field"]
	fields = compat.existing_fields(target, dict.fromkeys(["name", subject_field, date_field, state_field]))
	for fieldname in (subject_field, date_field, state_field):
		if fieldname and fieldname not in fields:
			warnings.append(
				f"supersession names {target}.{fieldname}, which this site has not got, so no later "
				"record could supersede a finding of this rule."
			)
			return {}
	company_field = compat.first_field(target, *_COMPANY_FIELDS)
	rows = frappe.db.get_all(
		target,
		filters={company_field: company} if (company and company_field) else {},
		fields=fields,
		limit=SCAN_CAP,
	)
	clean_values = config["clean_state_values"]
	out: dict = {}
	for entry in rows or []:
		state = str(entry.get(state_field) or "").strip()
		if not state:
			if config.get("unreadable_counts_as_dirty", True):
				continue
		elif state not in clean_values:
			continue
		out.setdefault(str(entry.get(subject_field) or ""), []).append(str(entry.get(date_field) or ""))
	return out


def _superseded(candidate: dict, config: dict, clean: dict, date_field: str) -> bool:
	"""Has a later clean record for the same subject overtaken this finding?

	Dates are compared AS TEXT, which is correct for the ISO strings every date
	column in this app holds and is what the Python this replaced did. A record
	with no date supersedes nothing, because `"" > ""` is false and any real date
	is greater than none — the finding stays standing, which is the safe answer.
	"""
	found_on = str(candidate.get(config.get("date_field") or date_field) or "")
	subject = str(candidate.get(config["subject_field"]) or "")
	return any(date > found_on for date in clean.get(subject, ()))


# ── the clock, singular or plural ───────────────────────────────────────────
def _clock(candidate: dict, settings: dict, date_field: str, doctype: str, today: str, warnings: list):
	"""The anchor, the deadline and the days remaining. None means "skip this row".

	Three shapes, and the third is v0.22.1's:

	  NO DATE FIELD    the condition is a filter; there is no clock and the row
	                   raises at `severity_expired`.
	  ONE DATE FIELD   the ordinary case.
	  SEVERAL          `date_fields_json`: each measured against the same cadence,
	                   the severity folded to the WORST of them, and the message
	                   handed the ones that are actually stale so it can name them.

	v0.22.5's `State` role runs through the same code as `Timestamp` HERE and
	diverges in `_band`, which is the honest split: both say the date is read
	rather than measured, so neither may skip a row for a missing one. What they
	disagree about is what the row then raises at, and that is a severity question
	rather than a date question.
	"""
	cadence = settings["cadence"]
	plural = settings["date_fields"]
	timestamp = settings["date_role"] in (
		compliance_rules.DATE_ROLE_TIMESTAMP,
		compliance_rules.DATE_ROLE_STATE,
	)

	if plural:
		per_field = []
		worst = None
		unmeasured = False
		for spec in plural:
			text = str(candidate.get(spec["field"]) or "").strip()
			if not text:
				if settings["on_missing"] == compliance_rules.ON_MISSING_SKIP:
					continue
				per_field.append(
					{
						"label": spec["label"],
						"field": spec["field"],
						"date": None,
						"days_since": None,
						"days_remaining": None,
						"stale": True,
					}
				)
				unmeasured = True
				continue
			due = _due_from(text, cadence)
			remaining = days_until(today, due)
			if remaining is None:
				warnings.append(
					f"{doctype} {candidate.get('name')} has an unreadable {spec['field']} "
					f"({candidate.get(spec['field'])!r}) and that date was not measured."
				)
				continue
			per_field.append(
				{
					"label": spec["label"],
					"field": spec["field"],
					"date": text,
					"days_since": days_since(today, text),
					"days_remaining": remaining,
					"stale": False,
				}
			)
			worst = remaining if worst is None else min(worst, remaining)
		if not per_field:
			return None
		for entry in per_field:
			if entry["days_remaining"] is None:
				continue
			severity, _band_name, _window = _band(
				entry["days_remaining"],
				candidate,
				settings["window_field"],
				settings["critical_days"],
				settings["warning_days"],
				settings["severity_critical"],
				settings["severity_warning"],
				settings["severity_expired"],
				state_severity=(
					settings["default_severity"]
					if settings["date_role"] == compliance_rules.DATE_ROLE_STATE
					else ""
				),
			)
			entry["stale"] = severity is not None
		anchor = next((entry["date"] for entry in per_field if entry["stale"] and entry["date"]), "")
		return {
			"anchor": anchor,
			"due_text": _due_from(anchor, cadence) if anchor else "",
			"days_remaining": None if unmeasured else worst,
			"days_since_anchor": days_since(today, anchor) if anchor else None,
			"per_field": per_field,
		}

	empty = {"anchor": "", "due_text": "", "days_remaining": None, "days_since_anchor": None, "per_field": []}
	if not date_field:
		return empty
	anchor_text = str(candidate.get(date_field) or "").strip()
	if not anchor_text:
		# A TIMESTAMP IS NOT A DEADLINE, so a missing one does not silence the
		# row: a corrective action nobody dated is still open, and the message
		# says "on an unrecorded date" rather than nothing at all.
		if not timestamp and settings["on_missing"] == compliance_rules.ON_MISSING_SKIP:
			return None
		return empty
	due_text = _due_from(anchor_text, cadence)
	remaining = days_until(today, due_text)
	if remaining is None:
		if timestamp:
			return {**empty, "anchor": anchor_text}
		# An unparseable date. Skipped rather than raised: one bad cell must not
		# take a whole rule's night with it.
		warnings.append(
			f"{doctype} {candidate.get('name')} has an unreadable {date_field} "
			f"({candidate.get(date_field)!r}) and was skipped."
		)
		return None
	return {
		"anchor": anchor_text,
		"due_text": due_text,
		"days_remaining": remaining,
		"days_since_anchor": days_since(today, anchor_text),
		"per_field": [],
	}


# ── primitives 2 and its category twin: an ordered table, as data ───────────
def _regimes_of(candidate: dict, settings: dict):
	"""Which audits THIS alert answers to: the heuristics, a column, or the rule.

	The heuristics win where they are set, because they are the most specific
	thing anybody said: a rule tagged with the union of eleven certificate
	schemes is tagged so a one-regime sweep knows to RUN it, and the row is what
	says which of the eleven this particular certificate actually is.
	"""
	if settings["regime_heuristics"]:
		matched = compliance_rules.match_heuristics(settings["regime_heuristics"], candidate, "regimes")
		if matched is not None:
			return list(matched)
	if settings["regimes_field"]:
		return regimes_vocabulary.parse(candidate.get(settings["regimes_field"]))
	return list(settings["rule_regimes"]) if settings["rule_regimes"] else None


def _category_of(candidate: dict, settings: dict) -> str:
	"""The alert's category where it is a property of the ROW rather than the rule.

	Empty means "the rule's own category", which `base.py` has always read as the
	fallback — so a rule with no heuristics behaves exactly as it did.
	"""
	if not settings["category_heuristics"]:
		return ""
	return str(
		compliance_rules.match_heuristics(settings["category_heuristics"], candidate, "category") or ""
	)


def _due_from(anchor: str, cadence: int) -> str:
	"""The deadline: the anchor itself where the cadence is 0, else anchor+cadence.

	The `cadence == 0` branch returns the anchor UNTOUCHED rather than running it
	through `add_days(…, 0)`, because an expiry alert's due date is the string
	that was on the record and a round trip through `getdate` can renormalise it.
	"""
	if not cadence:
		return anchor
	return str(frappe.utils.add_days(frappe.utils.getdate(anchor), cadence))


def _due_date(mode: str, due_text: str, today: str) -> str:
	if mode == compliance_rules.DUE_TODAY:
		return today
	if mode == compliance_rules.DUE_NONE:
		return ""
	return due_text


def _band(
	days_remaining,
	candidate: dict,
	window_field: str,
	critical_days: int,
	warning_days: int,
	severity_critical: str,
	severity_warning: str,
	severity_expired: str,
	forced: bool = False,
	state_severity: str = "",
):
	"""(severity, band, window) — what this row raises, or (None, "", window).

	A NEGATIVE THRESHOLD MEANS THE BAND NEVER FIRES, which is how a rule that
	only has something to say once the date has passed is written without a
	second field to say so. `threshold_warning_days` is also the OUTER window: a
	certificate two hundred days out reaches no band and the rule stays quiet,
	which is the difference between this and a reminder.

	`forced` is `date_field_role = Timestamp`: the date says when the thing was
	FOUND, so there is no band to be in and every row that got this far raises.

	`state_severity` is v0.22.5's `date_field_role = State`, and it is checked
	FIRST and returns before anything else is read. A state-driven rule fires on
	its gates and on nothing else, so the thresholds beside it — including the
	per-row window, which is the one thing that outranks everything on a clock —
	must not be able to silence it. The band it reports is `state`, which is a
	third word rather than a reused one: an alert that says `expired` about a
	condition with no expiry is a word an auditor would be right to query.

	THE OUTER WINDOW IS CHECKED BEFORE THE CRITICAL BAND, and v0.22.1 moved it
	there. It changes nothing for a rule whose window is wider than its critical
	threshold, which is every rule with a fixed pair. It matters the moment the
	window is PER ROW: a certificate whose issuing body turns renewals round in
	ten days is not a critical renewal task thirty days out just because the
	rule's default threshold says thirty. The window is the claim about when the
	work can usefully start, and nothing inside the rule outranks it.
	"""
	if state_severity:
		return state_severity, "state", 0
	window = warning_days
	if window_field:
		try:
			window = int(candidate.get(window_field) or 0) or warning_days
		except (TypeError, ValueError):
			window = warning_days
	if forced:
		return severity_expired, "expired", window
	if days_remaining is None:
		# Either the rule has no clock at all, or the anchor is missing on a rule
		# whose `missing_date_behaviour` is Raise. Both mean "past due by the only
		# measure this rule has".
		return severity_expired, "expired", window
	if days_remaining < 0:
		return severity_expired, "expired", window
	if days_remaining > window:
		return None, "", window
	if 0 <= critical_days and days_remaining <= critical_days:
		return severity_critical, "critical", window
	if 0 <= window:
		return severity_warning, "warning", window
	return None, "", window


def _selectable_fields(doctype: str) -> list:
	"""Every data column on the doctype this site actually has, plus `name`.

	Selected by name rather than with `*` so the query is explicit about what it
	reads, and so a doctype whose meta this site has trimmed produces a shorter
	SELECT rather than an error.
	"""
	candidates = []
	try:
		for field in frappe.get_meta(doctype).fields:
			fieldname = str(getattr(field, "fieldname", "") or "")
			fieldtype = str(getattr(field, "fieldtype", "") or "")
			if fieldname and fieldtype not in _LAYOUT_FIELDTYPES:
				candidates.append(fieldname)
	except Exception:  # pragma: no cover - a doctype whose meta cannot be built
		return ["name"]
	seen = dict.fromkeys(candidates)
	return ["name", *compat.existing_fields(doctype, seen)]


# ── the message ─────────────────────────────────────────────────────────────
def render_message(template: str, row: dict, extra: dict, warnings: list) -> str:
	"""Render one alert's text in a Jinja sandbox with NO frappe in it.

	DELIBERATELY NOT `frappe.render_template`. That builds the site's own Jinja
	environment, which has `frappe` in its globals — and a message template that
	could call `frappe.db.set_value` would be a second, undocumented escape hatch
	sitting next to the one this app spent a module sandboxing. The message is
	text about a row; the row is all it gets.

	A template that fails to render produces a plain, honest fallback rather than
	killing the rule. An alert with an ugly message is a problem somebody fixes;
	an alert that never appeared is one nobody knows about.
	"""
	text = str(template or "").strip()
	context = {**{key: value for key, value in row.items() if not str(key).startswith("_")}, **extra}
	context["row"] = row
	if not text:
		return _fallback_message(context)
	try:
		from jinja2 import StrictUndefined  # noqa: F401 - imported to prove jinja2 is here
		from jinja2.sandbox import SandboxedEnvironment
	except Exception:  # pragma: no cover - Frappe depends on Jinja2, so never on a bench
		warnings.append(
			"jinja2 is not importable on this bench, so message templates were not rendered and "
			"each alert carries a plain description instead. Frappe depends on Jinja2, so this "
			"means the environment is broken rather than that the app is."
		)
		return _fallback_message(context)
	try:
		environment = SandboxedEnvironment(autoescape=False, keep_trailing_newline=False)
		return environment.from_string(text).render(**context).strip()
	except Exception as exc:
		warnings.append(
			f"the message template failed to render ({type(exc).__name__}: {exc}); the alert "
			"carries a plain description instead."
		)
		return _fallback_message(context)


def _fallback_message(context: dict) -> str:
	subject = context.get("subject") or context.get("name") or "a record"
	remaining = context.get("days_remaining")
	if remaining is None:
		return f"{subject} matches this rule's condition."
	if remaining < 0:
		return f"{subject} is {abs(int(remaining))} day(s) past due (due {context.get('due_date') or 'an unrecorded date'})."
	return (
		f"{subject} is due in {int(remaining)} day(s), on {context.get('due_date') or 'an unrecorded date'}."
	)


# ── the built-in scanners ───────────────────────────────────────────────────
def _builtin_scan(row: dict):
	"""A closure calling the shipped scanner the record names.

	The scanner is handed the RECORD as well as the context, so the thresholds,
	the scope filters and the citations it reads are the ones on the row rather
	than the constants it was written beside. That is what keeps a built-in rule
	genuinely configurable: an operator moving the annual housing walk from 365
	days to 300 changes a record, not a release.
	"""

	def scan(context: dict) -> list:
		from . import rules as shipped

		scanner = shipped.SCANNERS.get(str(row.get("builtin_scanner") or "").strip())
		if scanner is None:
			return []
		return list(scanner({**context, "rule": row}) or [])

	return scan


# ── custom_python ───────────────────────────────────────────────────────────
def _custom_scan(row: dict):
	"""A closure running the record's own program in `sandbox.py`."""

	def scan(context: dict) -> list:
		today = context.get("today") or frappe.utils.today()
		company = context.get("company") or ""
		doctype = str(row.get("target_doctype") or "")
		warnings = []
		names = _sandbox_names(row, today, company, doctype, warnings)
		try:
			produced = sandbox.run(str(row.get("custom_python") or ""), names)
		except sandbox.SandboxError as exc:
			# REPORTED, NOT RAISED. A refused operation on one rule must not take
			# the sweep down, and it must not be silent either: the warning lands
			# on the report, and the rule observes nothing — which auto-dismisses
			# nothing, because a rule that could not run is not evidence that
			# anybody did the work.
			warnings.append(f"custom_python was refused or failed: {exc}")
			return [
				Observation(
					source_doctype=compliance_rules.DOCTYPE,
					source_docname=str(row.get("name") or row.get("rule_id")),
					message=(
						f"This rule's custom_python did not run: {exc} Until it does, the condition "
						"it watches is UNWATCHED — nothing was raised for it and nothing was "
						"dismissed. Fix the program with update_compliance_rule, or disable the "
						"rule so the calendar stops claiming to cover this."
					),
					severity=SEVERITY_WARNING,
					category="Records",
					regimes=list(regimes_vocabulary.parse(row.get("regimes")) or []),
					computation_warnings=warnings,
				)
			]
		return _as_observations(produced, row, warnings)

	return scan


def _sandbox_names(row: dict, today: str, company: str, doctype: str, warnings: list) -> dict:
	"""Everything a `custom_python` program can see. There is nothing else."""
	rule_regimes = list(regimes_vocabulary.parse(row.get("regimes")) or [])

	def observation(
		source_doctype: str,
		source_docname: str,
		message: str,
		severity: str = SEVERITY_WARNING,
		due_date: str = "",
		company: str = "",
		category: str = "",
		regimes=None,
	) -> dict:
		"""The one constructor a program should use. Returns a plain dict."""
		return {
			"source_doctype": str(source_doctype or ""),
			"source_docname": str(source_docname or ""),
			"message": str(message or ""),
			"severity": str(severity or SEVERITY_WARNING),
			"due_date": str(due_date or ""),
			"company": str(company or ""),
			"category": str(category or ""),
			"regimes": list(regimes) if regimes is not None else None,
		}

	def warn(message: str) -> None:
		"""Say something the sweep report should carry. Not an error."""
		text = str(message or "").strip()
		if text and text not in warnings:
			warnings.append(text)

	return {
		"frappe": _ReadOnlyFrappe(),
		"today": today,
		"company": company,
		"target_doctype": doctype,
		"doctype_meta": _selectable_fields(doctype) if doctype else [],
		"rule": {
			key: row.get(key)
			for key in (
				"rule_id",
				"title",
				"category",
				"target_doctype",
				"date_field",
				"cadence_days",
				"regulation_citations",
			)
		},
		"regimes": rule_regimes,
		"threshold_critical_days": int(row.get("threshold_critical_days") or 0),
		"threshold_warning_days": int(row.get("threshold_warning_days") or 0),
		"cadence_days": int(row.get("cadence_days") or 0),
		"severity_expired": str(row.get("severity_expired") or SEVERITY_CRITICAL),
		"observation": observation,
		"warn": warn,
		"days_until": days_until,
		"days_since": days_since,
		"datetime": _DateTimes(),
		"timedelta": datetime.timedelta,
		"SEVERITY_CRITICAL": SEVERITY_CRITICAL,
		"SEVERITY_WARNING": SEVERITY_WARNING,
		"SEVERITY_INFO": SEVERITY_INFO,
		"observations": [],
		# The safe half of builtins. Everything that can reach the interpreter —
		# `getattr`, `type`, `open`, `eval` — is absent, and `sandbox.check`
		# refuses those names by hand as well, so a future edit here cannot
		# quietly reopen one.
		"len": len,
		"str": str,
		"int": int,
		"float": float,
		"bool": bool,
		"abs": abs,
		"min": min,
		"max": max,
		"round": round,
		"sum": sum,
		"sorted": sorted,
		"any": any,
		"all": all,
		"list": list,
		"dict": dict,
		"set": set,
		"tuple": tuple,
		"range": range,
		"enumerate": enumerate,
		"zip": zip,
		"reversed": reversed,
	}


class _DateTimes:
	"""`datetime.date` / `datetime.datetime`, without the module around them.

	The three are bound from module-level aliases rather than from `datetime.X`
	directly because a class body executes in its own namespace: the moment
	`datetime = datetime.datetime` runs, the name `datetime` inside this body is
	the CLASS, and the next line asking it for `timedelta` fails.
	"""

	date = _DATE
	datetime = _DATETIME
	timedelta = _TIMEDELTA


class _ReadOnlyFrappe:
	"""The `frappe` a rule program sees: five readers and a utils namespace.

	`get_doc` HANDS BACK A DICT, not a Document. A Document has `.save()` and
	`.delete()` on it, and a read-only sandbox that returns a live document is
	read-only in the same sense a locked door with the key in it is locked.
	"""

	def __init__(self):
		self.utils = _ReadOnlyUtils()
		self.db = self

	def get_all(self, doctype, filters=None, fields=None, order_by=None, limit=None, pluck=None):
		return [
			dict(row) if isinstance(row, dict) else row
			for row in frappe.db.get_all(
				doctype,
				filters=filters,
				fields=fields or ["name"],
				order_by=order_by,
				limit=min(int(limit or SCAN_CAP), SCAN_CAP),
				pluck=pluck,
			)
			or []
		]

	def get_value(self, doctype, filters=None, fieldname="name", as_dict=False):
		value = frappe.db.get_value(doctype, filters, fieldname, as_dict=as_dict)
		return dict(value) if as_dict and value else value

	def get_doc(self, doctype, name):
		"""A frozen snapshot of one document. See the class docstring."""
		try:
			return dict(frappe.get_doc(doctype, name).as_dict())
		except Exception:
			return {}

	def exists(self, doctype, filters=None):
		return frappe.db.exists(doctype, filters)

	def count(self, doctype, filters=None):
		return frappe.db.count(doctype, filters)


class _ReadOnlyUtils:
	"""The handful of `frappe.utils` a date-comparing rule actually needs."""

	def today(self):
		return frappe.utils.today()

	def nowdate(self):
		return frappe.utils.nowdate()

	def add_days(self, date, days):
		return str(frappe.utils.add_days(frappe.utils.getdate(date), int(days)))

	def add_months(self, date, months):
		return str(frappe.utils.add_months(frappe.utils.getdate(date), int(months)))

	def date_diff(self, later, earlier):
		return frappe.utils.date_diff(str(later), str(earlier))

	def getdate(self, value=None):
		return frappe.utils.getdate(value)

	def cint(self, value):
		return frappe.utils.cint(value)

	def flt(self, value):
		return frappe.utils.flt(value)


def _as_observations(produced, row: dict, warnings: list) -> list:
	"""Turn whatever a program returned into Observations, or say why it could not."""
	if produced in (None, ""):
		return []
	if isinstance(produced, dict):
		produced = [produced]
	if not isinstance(produced, (list, tuple)):
		warnings.append(
			f"custom_python returned {type(produced).__name__}; a rule returns a list of "
			"observation(...) objects. Nothing was raised."
		)
		return []

	out = []
	for index, entry in enumerate(produced):
		if isinstance(entry, Observation):
			entry.computation_warnings = list(warnings) or None
			out.append(entry)
			continue
		if not isinstance(entry, dict):
			warnings.append(f"custom_python returned a {type(entry).__name__} at position {index}; skipped.")
			continue
		doctype = str(entry.get("source_doctype") or "").strip()
		docname = str(entry.get("source_docname") or "").strip()
		if not (doctype and docname):
			warnings.append(
				f"observation {index} names no source_doctype/source_docname, so there is no record "
				"for the alert to be about and no stable key for it. Skipped."
			)
			continue
		severity = str(entry.get("severity") or SEVERITY_WARNING)
		if severity not in (SEVERITY_CRITICAL, SEVERITY_WARNING, SEVERITY_INFO):
			warnings.append(f"observation {index} asked for severity {severity!r}; used Warning.")
			severity = SEVERITY_WARNING
		out.append(
			Observation(
				source_doctype=doctype,
				source_docname=docname,
				message=str(entry.get("message") or ""),
				severity=severity,
				due_date=str(entry.get("due_date") or ""),
				company=str(entry.get("company") or ""),
				category=str(entry.get("category") or ""),
				regimes=entry.get("regimes"),
				computation_warnings=list(warnings) or None,
			)
		)
	return out

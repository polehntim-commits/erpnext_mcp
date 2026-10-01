# SPDX-License-Identifier: MIT
"""The thirteen compliance rules, and the state that makes each one ripe.

v0.19.3 ADDED THE THIRTEENTH, AND IT WATCHES A SIGNATURE THAT WAS NEVER PUT ON A
RECORD. `supervisor_review_lapsed` is a different shape of absence from all
twelve above it: the work was done, the record was written, and the second pair
of eyes FSMA §112.161(b) asks for never arrived. That is the most commonly cited
finding against farms whose actual practice is sound — USDA GAP does not ask for
a supervisor's review, so an operation with an immaculate GAP binder has every
training delivered, every trainee signature and no review on any of it, and
finds out from an FDA-contracted inspector. It is written to walk a TABLE of
doctypes carrying the §112.161(b) columns rather than to know about training,
because the requirement reaches every activity record under the part and this app
already ships four more that will grow the columns.

v0.19.0 ADDED THE TWELFTH, AND IT IS THE FIRST ONE ABOUT A PERSON RATHER THAN A
DOCUMENT. Rules 1 and 7 watch certificates — things an agency issued to the
operation. `training_expiring` watches what the crew was actually taught, which
is what four separate regimes ask for (WPS every twelve months, Oregon's heat
rule annually before the first hot shift, FSMA Subpart C on hiring and
periodically, GAP annually) and which lived, until now, in a binder nobody opens
between audits. The consequence is sharper than a certificate's, too: a handler
whose WPS training lapsed cannot lawfully perform an application tomorrow
morning, so the alert takes somebody off the sprayer rather than off a list.

v0.16.0 ADDED RULES 10 AND 11, AND THEY ARE A DIFFERENT SHAPE FROM THE FIRST NINE.
Rules 1-9 fire on IGNORANCE: nobody has walked this cabin, nobody has tested this
water, nobody has renewed this licence. Rules 10 and 11 fire on KNOWLEDGE —
somebody went and looked and found something — and they exist because Sprint 8
gave the operation a way to go and look. A framework that could only tell you
what had not been checked, and had nothing to say when the check came back bad,
would have been half a compliance system.

Both of the new ones close by being SUPERSEDED rather than by being ticked: a
cabin re-inspected with nothing found, a water source re-sampled clean. That is
deliberate. The work that makes the finding untrue is the work anybody would
want done, so it is the work that silences the alert.


EVERY RULE IN THIS FILE READS STATE AND NONE OF THEM READS A CALENDAR. That is
the whole point of the file, so it is worth being concrete about what the
difference looks like in code:

    chronological:  if today.day == 1: warn about water testing
    kairotic:       if this block was sprayed recently AND its agricultural
                    water has not been tested in ninety days: warn

The first fires on fallow ground, on ground tested last week, and on ground
nobody has touched since 2019, twelve times a year. By March it is noise and by
June nobody reads it. The second fires on exactly the blocks where FSMA Subpart E
is engaged, on exactly the days it is engaged, and stops firing by itself the day
the test is done. One of those is a compliance system and the other is a
recurring appointment.

THE THRESHOLDS ARE LEAD TIMES, NOT PREFERENCES. Thirty days for a critical
certificate expiry is how long a renewal actually takes to come back from an
agency; ninety days for agricultural water is the Subpart E testing cadence; three
hundred and sixty-five for a habitability inspection is the annual walk a camp
gets. Every number in this file is the real interval something takes in the world,
and each one is annotated with which. A number nobody can say the provenance of
should not be here.

SEVERITY MEANS THE SAME THING IN ALL NINE. Critical blocks something today: a
person cannot lawfully work, a block cannot lawfully be entered, a certificate
that a buyer requires has lapsed. Warning means it needs doing this month.
Info is context. A rule that raises Critical for something that merely needs
attention devalues the word for the rules that meant it, so the two rules that
can raise Critical do so only when the underlying thing has actually stopped
being lawful, or is inside the window where starting late means missing it.

v0.19.2 GAVE EVERY RULE A `regimes` TAG, AND IT IS THE FIRST THING ON A RULE THAT
IS ABOUT THE READER RATHER THAN THE CONDITION. `framework` was already here and
is prose — the citation, for somebody reading the rule. `regimes` is a key, for a
filter: "everything OR-OSHA will ask about in October" is one afternoon's work
and "everything" is not, and until now the only way to get the first was to know
which of twelve rule names belonged to which agency. Ten of the twelve carry a
constant, because an overdue cabin inspection is an OR-OSHA item whoever is
asking. The two that fire on records of many kinds — certificates and training —
tag each observation from the record itself, because an applicator licence is WPS
evidence and a GlobalGAP certificate is not.

READ-ONLY, ALL OF THEM. A rule returns Observations and writes nothing. That is
what lets the whole rule set run in a dry run, and what lets each one be tested by
calling it with a date rather than by running a sweep and inspecting the database
afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass

import frappe

from .. import compat, minors
from .. import shifts as shift_records
from .. import training as training_records
from ..records import CORRECTIVE_ACTION_REQUIRED, RECORDED
from .base import (
	RULES,
	SEVERITY_CRITICAL,
	SEVERITY_INFO,
	SEVERITY_WARNING,
	Observation,
	Rule,
	days_since,
	days_until,
	register,
)

# ── the intervals, and where each number comes from ─────────────────────────

#: Inside this many days of expiry a certificate stops being a renewal task and
#: becomes an operational risk. Thirty days is roughly the turnaround on an
#: Oregon licence renewal once the paperwork is in — start later and the lapse
#: happens whatever anybody does.
CERT_CRITICAL_DAYS = 30

#: Default lead time where a certificate does not state its own. Ninety days is
#: the GlobalGAP re-audit booking window: the auditor has to be scheduled, not
#: merely paid.
CERT_DEFAULT_WINDOW_DAYS = 90

#: FSMA Produce Safety Rule Subpart E untreated surface water testing cadence.
#: A test older than this is not a current test.
WATER_TEST_DAYS = 90

#: How recently a block must have been sprayed to count as in active rotation.
#: A hundred and twenty days is about one spray season in tree fruit: a block
#: sprayed inside it will be sprayed again, and its water matters now. Ground
#: nobody has sprayed since last year is not urgent and this rule stays quiet on
#: it — which is the kairotic gate, stated as a number.
SPRAY_SEASON_DAYS = 120

#: The annual walk a camp gets. Matches `tools.housing.INSPECTION_DAYS`, which is
#: what `get_housing_capacity` already counts overdue inspections against — one
#: number, so the calendar and the capacity report cannot disagree.
INSPECTION_DAYS = 365

#: Detectors are tested annually under OAR 437-004-1120 and the fire code the
#: camp is inspected against.
DETECTOR_DAYS = 365

#: How far ahead of an agency response deadline to say so. Thirty days is enough
#: to chase a docket before the deadline rather than after it.
FILING_RESPONSE_DAYS = 30

#: A corrective action past due by more than this has stopped being late and
#: become a finding of its own. Most schemes give 28 days for a Major.
ACTION_OVERDUE_GRACE_DAYS = 0

#: FSMA §112.161(b) asks for a supervisor review "within a reasonable time after
#: the records are made". The rule states no number, and the practice it is read
#: against for records generated daily is WEEKLY review — so a fortnight is one
#: clear miss of that cadence: two Fridays gone, and the afternoon still recent
#: enough for the supervisor to remember what they are signing off.
REVIEW_WARNING_DAYS = 14

#: Past a month there is no longer a reading of "reasonable time" that covers it.
#: Thirty days is also the point at which a batch of signatures added at once
#: starts to look like what it is — a record assembled rather than kept, which is
#: the finding rather than the fix.
REVIEW_CRITICAL_DAYS = 30

#: Housing unit types nobody sleeps in, which therefore need no habitability
#: inspection. Imported rather than restated so the two definitions cannot drift.
try:  # pragma: no cover - the import is available on every migrated site
	from ..erpnext_mcp.doctype.housing_unit.housing_unit import NON_RESIDENTIAL_TYPES
except Exception:  # pragma: no cover
	NON_RESIDENTIAL_TYPES = ("Toilet-Shower", "Kitchen", "Barn", "Shop")


def _company_filter(filters: dict, company: str, fieldname: str = "company") -> dict:
	if company:
		filters[fieldname] = company
	return filters


def _rows(doctype: str, filters: dict, fields, limit: int = 2000) -> list:
	"""Read rows, selecting only the columns this site actually has."""
	return [
		dict(row)
		for row in frappe.db.get_all(
			doctype,
			filters=filters,
			fields=compat.existing_fields(doctype, fields),
			limit=limit,
		)
		or []
	]


# ── v0.22.0: reading the numbers off the record ─────────────────────────────
#
# EVERY INTERVAL BELOW IS STILL DECLARED IN THIS FILE, and every one of them is
# now only the FALLBACK. The number the sweep actually uses comes off the
# Compliance Rule row, so an operation whose annual habitability walk is
# contracted at ten months rather than twelve changes a record instead of
# waiting for a release. The constants stay because they are the shipped
# defaults, because they are what the seeder writes, and because a site
# mid-migrate runs these functions with no record behind them at all — and a
# scanner that answered `None` in that window would raise nothing and dismiss
# everything.
#
# `context["rule"]` is the row, put there by `engine._builtin_scan`. It is absent
# when these functions run as the shipped fallback, which is why every read
# below goes through a helper with a fallback rather than through `row[...]`.


def _rule_of(context: dict) -> dict:
	return context.get("rule") or {}


def _extra(context: dict) -> dict:
	"""`extra_parameters_json` — the named intervals one scanner happens to need.

	A rule with TWO intervals — the water cadence and the spray season that gates
	it — has one that maps onto `cadence_days` and one that maps onto nothing.
	Rather than grow a column per scanner, the second lives in a small JSON
	object the scanner reads by name. It is deliberately not a general-purpose
	config bag: every key in it is documented on the rule that reads it.
	"""
	from ..compliance_rules import as_object

	try:
		return as_object(_rule_of(context).get("extra_parameters_json"), "extra_parameters")
	except ValueError:
		return {}


def _interval(context: dict, key: str, fallback: int) -> int:
	"""One tuned interval, off the row where there is one, else the constant."""
	rule = _rule_of(context)
	value = rule.get(key) if key in rule else _extra(context).get(key)
	if value in (None, ""):
		return fallback
	try:
		return int(value)
	except (TypeError, ValueError):
		return fallback


def _severity_of(context: dict, key: str, fallback: str) -> str:
	value = str(_rule_of(context).get(key) or "").strip()
	return value if value in (SEVERITY_CRITICAL, SEVERITY_WARNING, SEVERITY_INFO) else fallback


def _scoped(context: dict, row: dict) -> bool:
	"""Does this row pass the scope filters an OPERATOR added to the rule?

	The shipped rules seed with an empty filter list: the scoping a built-in
	scanner does is part of its shape and stays in code, which is the whole
	distinction between a built-in rule and a declarative one. This is the door
	left open on top of it — somebody who wants `housing_inspection_overdue`
	scoped to one company's camp adds a filter and gets it, without any of the
	thirteen shipped scanners having to grow a company argument.

	The row's own keys are the present-fields set, because the row was fetched
	with `compat.existing_fields` and therefore already reflects what this site
	has. A filter on a column that is not there is skipped rather than failing
	the row — see `compliance_rules.row_matches`.
	"""
	from ..compliance_rules import parse_filters, row_matches

	raw = _rule_of(context).get("scope_filters_json")
	if not str(raw or "").strip() or str(raw).strip() in ("[]", "{}"):
		return True
	try:
		filters = parse_filters(raw)
	except ValueError:
		return True
	matched, _warnings = row_matches(row, filters, set(row.keys()))
	return matched


#: `builtin_scanner` name → the function. A Compliance Rule naming one of these
#: delegates the SHAPE of its scan to reviewed, shipped, tested code while
#: keeping every tunable on the record. The controller refuses a name that is
#: not in here, because a rule pointing at a scanner that does not exist raises
#: nothing, for ever, quietly.
SCANNERS: dict = {}

#: rule key → the Compliance Rule fields that rule migrates to. Written beside
#: each `register(...)` below so the definition and its migration cannot drift,
#: and read by `compliance_rules.seed_specs()`.
SHAPES: dict = {}


def shape(key: str, **fields) -> None:
	"""Declare how one shipped rule becomes a Compliance Rule record."""
	SHAPES[key] = fields


# ── which audits an alert answers to ────────────────────────────────────────
#
# Every rule below carries `regimes`, and most of them carry a constant: an
# overdue cabin inspection is an OR-OSHA item whoever is asking. Two cannot, and
# both take their tags from the record instead — see `Observation.regimes`.
#
# WHAT `Internal` MEANS HERE. Six of the twelve answer to no outside agency: a
# procedure overdue for its own review, a filing deadline, an audit's corrective
# action, a detector nobody but this operation tests annually. Tagging those with
# a regime they do not answer to would put them in a packet where they invite a
# question; leaving them untagged would make them invisible to every regime
# filter, which is worse. `Internal` is the honest third answer — real work, real
# due date, nobody coming to inspect it — and it is a claim somebody can disagree
# with, which "no tag" never is.

#: What a certificate's TYPE says about the audit its expiry belongs to.
#: Evaluated in order, first match wins, and the order is the whole content: an
#: applicator licence is checked before the bare "OSHA" needle because "Applicator
#: License" is WPS evidence, and `globalgap`/`primus` are checked before `gap`
#: because "GlobalGAP" contains "GAP" and a USDA GAP packet must not be handed a
#: different scheme's certificate. Same substring trap, same guard, as
#: `training.TYPE_REGIME_HEURISTICS`.
CERT_REGIME_HEURISTICS = (
	(("wps", "worker protection"), ("WPS",)),
	(("applicator", "pesticide"), ("WPS", "OR-OSHA")),
	(("psa", "produce safety", "fsma"), ("FSMA",)),
	(("osha", "first aid", "cpr"), ("OR-OSHA",)),
	(("otco", "oregon tilth"), ("OTCO",)),
	(("nop", "organic"), ("NOP",)),
	(("globalgap", "global gap"), ("GlobalGAP",)),
	(("primus",), ("PrimusGFS",)),
	(("gap",), ("GAP",)),
	(("water",), ("FSMA",)),
	(("food safety",), ("FSMA", "GAP")),
)


def _certificate_regimes(row: dict) -> list:
	"""Which audit one certificate's expiry is evidence for.

	READS THE TYPE FIRST AND THE NAME ONLY AS A FALLBACK. `cert_type` is a Select
	an operator chose from a closed list; `cert_name` is whatever was typed on the
	day. A name that happens to contain "organic" on a certificate typed as an
	applicator licence should not retag it, so the name is consulted only where
	the type said nothing.

	`Internal` where neither says anything — a licence this app has not modelled
	is still a licence with a real expiry, and an alert nobody can filter to is
	worse than one filed under "ours".
	"""
	for source in (row.get("cert_type"), row.get("cert_name")):
		text = str(source or "").lower()
		if not text:
			continue
		for needles, regimes in CERT_REGIME_HEURISTICS:
			if any(needle in text for needle in needles):
				return training_records.parse(regimes)
	return ["Internal"]


#: Every regime `_certificate_regimes` can produce, DERIVED from the table above
#: rather than restated beside it. `Rule.regimes` on a per-row rule is the union
#: of what it can emit — that is what `refresh_compliance_alerts(regime=...)`
#: matches on when it decides which rules a one-regime sweep has to run — and a
#: union typed by hand is a union that goes stale the first time a needle is
#: added.
CERT_REGIMES = tuple(
	regime
	for regime in training_records.REGIMES
	if regime == "Internal" or any(regime in produced for _needles, produced in CERT_REGIME_HEURISTICS)
)


# ── 1. certification_expiring ───────────────────────────────────────────────
def _scan_certifications(context: dict) -> list:
	today = context["today"]
	critical_days = _interval(context, "threshold_critical_days", CERT_CRITICAL_DAYS)
	default_window = _interval(context, "threshold_warning_days", CERT_DEFAULT_WINDOW_DAYS)
	window_field = str(_rule_of(context).get("window_field") or "renewal_window_days")
	expired_severity = _severity_of(context, "severity_expired", SEVERITY_CRITICAL)
	critical_severity = _severity_of(context, "severity_critical", SEVERITY_CRITICAL)
	warning_severity = _severity_of(context, "severity_warning", SEVERITY_WARNING)
	out = []
	for row in _rows(
		"Certification",
		_company_filter({}, context.get("company") or ""),
		(
			"name",
			"cert_name",
			"cert_type",
			"status",
			"company",
			"holder",
			"expiration_date",
			"renewal_window_days",
			"issuing_body",
		),
	):
		status = str(row.get("status") or "Active")
		if status in ("Superseded", "Revoked"):
			# Superseded means a newer certificate covers this; revoked means it is
			# not coming back. Neither is a renewal task, and alerting on them would
			# put permanent rows on a calendar nothing can ever clear.
			continue
		if not _scoped(context, row):
			continue
		expires = row.get("expiration_date")
		remaining = days_until(today, expires)
		if remaining is None:
			continue
		window = int(row.get(window_field) or 0) or default_window
		if remaining > window:
			# Not ripe. The renewal cannot usefully be started yet and the agency
			# would not accept it.
			continue

		what = f"{row.get('cert_type') or 'Certificate'} {row['name']}"
		holder = f" held by {row['holder']}" if row.get("holder") else ""
		if remaining < 0:
			severity = expired_severity
			message = (
				f"{what}{holder} EXPIRED {abs(remaining)} day(s) ago, on {expires}. It is not a "
				"defence and has not been one since that date"
				+ (f". Renewals go through {row['issuing_body']}." if row.get("issuing_body") else ".")
			)
		elif remaining <= critical_days:
			severity = critical_severity
			message = (
				f"{what}{holder} expires in {remaining} day(s), on {expires}. Inside {critical_days} "
				"days a renewal started now may not come back before the lapse"
				+ (f" — {row['issuing_body']} issues it." if row.get("issuing_body") else ".")
			)
		else:
			severity = warning_severity
			message = (
				f"{what}{holder} expires in {remaining} day(s), on {expires}, which is inside its "
				f"{window}-day renewal window. That window is the lead time the issuing body "
				"actually takes, not a reminder preference — starting after it means missing it."
			)

		out.append(
			Observation(
				source_doctype="Certification",
				source_docname=row["name"],
				message=message,
				severity=severity,
				due_date=str(expires),
				company=str(row.get("company") or ""),
				category=(
					"Workforce"
					if str(row.get("cert_type") or "")
					in ("Applicator License", "Farm Labor Contractor License")
					else "Certifications"
				),
				# PER ROW, not per rule. This one rule fires on eleven kinds of
				# certificate and an applicator licence is WPS evidence while a
				# GlobalGAP certificate is not — so the regime is a property of the
				# certificate, and a constant on the rule would be wrong ten times
				# out of eleven.
				regimes=_certificate_regimes(row),
			)
		)
	return out


register(
	Rule(
		key="certification_expiring",
		title="A certificate or licence is inside its renewal window",
		category="Certifications",
		requires=("Certification",),
		framework="GAP / GlobalGAP / PrimusGFS scheme rules; ORS 634 applicator licensing; ORS 658.405 FLC licensing",
		purpose=(
			"An operation with a lapsed certificate is not an operation with a paperwork "
			"problem — it is one whose fruit cannot be sold to the buyer who required it, "
			"or whose applicator cannot lawfully spray."
		),
		kairotic_gate=(
			"Fires when the certificate is genuinely inside the lead time its own issuing "
			"body takes, read from `renewal_window_days` on the record rather than from a "
			"fixed number of days before a date. A certificate 200 days out raises nothing "
			"however many times the sweep runs; the same certificate raises Warning the day "
			"it enters its window and Critical inside 30 days, when starting a renewal stops "
			"being enough. Superseded and revoked certificates are excluded because neither "
			"is a renewal anybody can perform. Auto-dismisses on renewal, when the expiration "
			"moves back outside the window."
		),
		# Per row: see `_certificate_regimes`. This is the UNION of what it can
		# emit, which is what a one-regime sweep matches on to decide whether
		# this rule has to run at all.
		regimes=CERT_REGIMES,
		scan=_scan_certifications,
	)
)

#: The certificate types that are a WORKFORCE item rather than a Certifications
#: one. An applicator licence and an FLC licence are about a person's authority
#: to do the work; a GlobalGAP certificate is about the operation's paperwork,
#: and filing them under one heading puts a person's lapsed licence in the
#: section somebody reads at audit time rather than the one they read on Monday.
CERT_WORKFORCE_TYPES = ("Applicator License", "Farm Labor Contractor License")

#: v0.22.1. `CERT_REGIME_HEURISTICS` as the ordered lookup the rule record
#: carries, DERIVED from the tuple above rather than restated beside it — a
#: needle added there reaches the seeded rule without anybody remembering to
#: copy it. `cert_type` is named before `cert_name` in every entry, and the
#: engine's field order is the outer loop, which reproduces the legacy
#: "read the type first and the name only as a fallback" exactly.
CERT_REGIME_HEURISTIC_ROWS = [
	{
		"if_field_contains": {
			"field": ["cert_type", "cert_name"],
			"value": list(needles),
			"case_insensitive": True,
		},
		"then_regimes": list(regimes),
	}
	for needles, regimes in CERT_REGIME_HEURISTICS
] + [{"default_regimes": ["Internal"]}]

# DECLARATIVE SINCE v0.22.1, AND IT COST TWO FIELDS. What kept it built-in was
# never the window — `window_field` was added for it in v0.22.0 — but the two
# things derived from the certificate's TYPE through a table:
#   * `regime_heuristics_json` holds the eleven-row needle table as data,
#     ORDERED, because "GlobalGAP" contains "GAP" and a USDA GAP packet must not
#     be handed another scheme's certificate;
#   * `category_heuristics_json` is the same shape producing the category, which
#     is per row for the reason `CERT_WORKFORCE_TYPES` gives above.
#
# The third thing v0.22.1 had to fix was not a field at all: `_band` used to
# check the critical threshold BEFORE the outer window, which is indistinguishable
# from the legacy scanner until the per-row window is NARROWER than the rule's
# critical threshold — a certificate whose issuing body turns renewals round in
# ten days. The window now wins, which is what the Python always did.
shape(
	"certification_expiring",
	target_doctype="Certification",
	date_field="expiration_date",
	cadence_days=0,
	window_field="renewal_window_days",
	threshold_critical_days=CERT_CRITICAL_DAYS,
	threshold_warning_days=CERT_DEFAULT_WINDOW_DAYS,
	severity_critical=SEVERITY_CRITICAL,
	severity_warning=SEVERITY_WARNING,
	severity_expired=SEVERITY_CRITICAL,
	missing_date_behaviour="Skip",
	due_date_mode="From Anchor",
	scope_filters=[
		# Superseded means a newer certificate covers this; revoked means it is not
		# coming back. Neither is a renewal anybody can perform, and alerting on
		# them would put permanent rows on a calendar nothing can ever clear. The
		# `default` is load-bearing: a certificate nobody set a status on is Active.
		{"field": "status", "op": "nin", "value": ["Superseded", "Revoked"], "default": "Active"}
	],
	regime_heuristics=CERT_REGIME_HEURISTIC_ROWS,
	category_heuristics=[
		{
			"if_field_in": {
				"field": "cert_type",
				"value": list(CERT_WORKFORCE_TYPES),
				# EXACT, because `cert_type` is a Select an operator chose from a
				# closed list rather than free text somebody typed on the day.
				"case_insensitive": False,
			},
			"then_category": "Workforce",
		},
		{"default_category": "Certifications"},
	],
	message_template=(
		"{{ cert_type or 'Certificate' }} {{ name }}"
		"{%- if holder %} held by {{ holder }}{% endif %}"
		"{%- if band == 'expired' %} EXPIRED {{ days_overdue }} day(s) ago, on {{ expiration_date }}. "
		"It is not a defence and has not been one since that date"
		"{%- if issuing_body %}. Renewals go through {{ issuing_body }}.{% else %}.{% endif %}"
		"{%- elif band == 'critical' %} expires in {{ days_remaining }} day(s), on "
		"{{ expiration_date }}. Inside {{ threshold_critical_days }} days a renewal started now may "
		"not come back before the lapse"
		"{%- if issuing_body %} — {{ issuing_body }} issues it.{% else %}.{% endif %}"
		"{%- else %} expires in {{ days_remaining }} day(s), on {{ expiration_date }}, which is "
		"inside its {{ window_days }}-day renewal window. That window is the lead time the issuing "
		"body actually takes, not a reminder preference — starting after it means missing it."
		"{%- endif %}"
	),
	audit_packet_types=["GAP", "GlobalGAP", "FSMA"],
	retention_years=3,
)


# ── 2. policy_review_overdue ────────────────────────────────────────────────
def _scan_policies(context: dict) -> list:
	today = context["today"]
	out = []
	for row in _rows(
		"Compliance Policy",
		_company_filter({}, context.get("company") or ""),
		(
			"name",
			"policy_name",
			"category",
			"status",
			"company",
			"version",
			"review_due_date",
			"policy_owner",
		),
	):
		if str(row.get("status") or "Active") != "Active":
			# Draft was never adopted; Superseded and Retired are history. Only a
			# procedure actually in force can be overdue for review.
			continue
		overdue = days_since(today, row.get("review_due_date"))
		if overdue is None or overdue < 0:
			continue
		owner = f" {row['policy_owner']} owns it." if row.get("policy_owner") else ""
		out.append(
			Observation(
				source_doctype="Compliance Policy",
				source_docname=row["name"],
				message=(
					f"{row['name']}"
					+ (f" ({row['version']})" if row.get("version") else "")
					+ f" was due for review on {row['review_due_date']}, {overdue} day(s) ago, and is "
					"still in force. GAP and GlobalGAP both expect an annual review; a procedure "
					"nobody has read in years is a finding whether or not it is still correct." + owner
				),
				severity=SEVERITY_WARNING,
				due_date=str(row["review_due_date"]),
				company=str(row.get("company") or ""),
			)
		)
	return out


register(
	Rule(
		key="policy_review_overdue",
		title="A procedure in force is overdue for review",
		category="Policies",
		requires=("Compliance Policy",),
		framework="GAP / GlobalGAP annual document review; FSMA 21 CFR 112 written procedures",
		purpose=(
			"An SOP nobody has looked at since it was written describes an operation that "
			"has since changed. Auditors ask when it was last reviewed before they ask what "
			"it says."
		),
		kairotic_gate=(
			"Reads the review date ON THE POLICY, which is the date the operation itself "
			"committed to — not an annual tick this app imposes. Only Active policies "
			"qualify: a Draft was never adopted and a Superseded one is history, and neither "
			"can meaningfully be reviewed. Auto-dismisses when the review date is moved "
			"forward, which is what reviewing it looks like in the record."
		),
		# The three schemes that ask for an annual document review, plus FSMA
		# 21 CFR 112's written-procedure requirement. An SOP nobody has read
		# in three years is a finding in each of them.
		regimes=("FSMA", "GAP", "GlobalGAP", "PrimusGFS"),
		scan=_scan_policies,
	)
)

# PURE DECLARATIVE. The gate is one date on one row against one threshold, and
# the scope is one status with a documented default — `or "Active"` in the
# Python above, `"default": "Active"` on the filter here, and the two mean the
# same thing: a policy nobody set a status on is in force.
#
# `threshold_warning_days = 0` is what makes it fire ON the review date rather
# than the day after; `threshold_critical_days = -1` is how a rule says "this
# band never fires", which is right here because a procedure a fortnight overdue
# for review is not blocking anything today.
shape(
	"policy_review_overdue",
	target_doctype="Compliance Policy",
	date_field="review_due_date",
	cadence_days=0,
	threshold_critical_days=-1,
	threshold_warning_days=0,
	severity_warning=SEVERITY_WARNING,
	severity_expired=SEVERITY_WARNING,
	missing_date_behaviour="Skip",
	scope_filters=[{"field": "status", "op": "eq", "value": "Active", "default": "Active"}],
	message_template=(
		"{{ name }}{% if version %} ({{ version }}){% endif %} was due for review on "
		"{{ review_due_date }}, {{ days_overdue }} day(s) ago, and is still in force. GAP and "
		"GlobalGAP both expect an annual review; a procedure nobody has read in years is a "
		"finding whether or not it is still correct."
		"{% if policy_owner %} {{ policy_owner }} owns it.{% endif %}"
	),
	audit_packet_types=["GAP", "GlobalGAP", "FSMA"],
	retention_years=3,
)


# ── 3. water_test_stale ─────────────────────────────────────────────────────
def _scan_water_tests(context: dict) -> list:
	"""Blocks in active spray rotation whose agricultural water is untested.

	THE GATE IS THE SPRAY, NOT THE DATE. FSMA Subpart E is engaged when
	agricultural water contacts the crop, and on a tree fruit block the water that
	contacts the crop is mostly what goes through the sprayer. So an untested
	block that nobody is spraying raises nothing — it is not unsafe, it is
	dormant — and the same block raises Critical the moment it re-enters rotation.
	`SPRAY_SEASON_DAYS` is what "in rotation" means, measured from the block's own
	last spray.
	"""
	today = context["today"]
	test_days = _interval(context, "cadence_days", WATER_TEST_DAYS)
	season_days = _interval(context, "spray_season_days", SPRAY_SEASON_DAYS)
	severity = _severity_of(context, "severity_expired", SEVERITY_CRITICAL)
	out = []
	for row in _rows(
		"Field",
		_company_filter({}, context.get("company") or "", "owning_entity"),
		(
			"name",
			"field_name",
			"owning_entity",
			"condition",
			"crop",
			"last_spray_date",
			"water_test_last_date",
		),
	):
		if str(row.get("condition") or "") == "Fallow":
			continue
		if not _scoped(context, row):
			continue
		sprayed = days_since(today, row.get("last_spray_date"))
		if sprayed is None or sprayed > season_days:
			# Not in rotation. Nothing is being applied to this ground, so no
			# agricultural water is contacting a crop on it, so Subpart E is not
			# engaged and there is nothing to be alarmed about.
			continue

		tested = days_since(today, row.get("water_test_last_date"))
		if tested is not None and tested <= test_days:
			continue

		if tested is None:
			detail = "has NO agricultural water test on record at all"
			due = ""
		else:
			detail = f"was last water-tested {tested} days ago, on {row['water_test_last_date']}"
			due = str(frappe.utils.add_days(frappe.utils.getdate(row["water_test_last_date"]), test_days))

		out.append(
			Observation(
				source_doctype="Field",
				source_docname=row["name"],
				message=(
					f"{row['name']} was sprayed {sprayed} day(s) ago and {detail}. FSMA Produce "
					f"Safety Rule Subpart E wants agricultural water tested within {test_days} "
					"days, and the water going through the sprayer onto a crop being harvested is "
					"agricultural water. This block is in active rotation, so the next application "
					"is not defensible until the test is done."
				),
				severity=severity,
				due_date=due,
				company=str(row.get("owning_entity") or ""),
			)
		)
	return out


register(
	Rule(
		key="water_test_stale",
		title="A block in spray rotation has no current agricultural water test",
		category="Water and Sanitation",
		requires=("Field",),
		framework="FSMA Produce Safety Rule 21 CFR 112 Subpart E; EPA WPS 40 CFR 170",
		purpose=(
			"Water applied to a crop being harvested has to have been tested. This is the "
			"rule that stops the next application being made on an undefensible basis."
		),
		kairotic_gate=(
			"THE CLEAREST CASE IN THE WHOLE ENGINE. The gate is the spray, not the "
			f"calendar: only a block sprayed within the last {SPRAY_SEASON_DAYS} days — i.e. one "
			"genuinely in rotation — can raise this, because Subpart E is engaged by water "
			"contacting a crop and not by a date passing. Fallow ground raises nothing. "
			"Ground dormant since last season raises nothing, and starts raising the day it "
			"is sprayed again. Auto-dismisses the moment a water test is recorded."
		),
		# Subpart E is the rule; OR-OSHA is here because the water going through
		# the sprayer is also the water in the field sanitation requirement, and
		# an untested source is asked about by both inspectors.
		regimes=("FSMA", "OR-OSHA"),
		scan=_scan_water_tests,
	)
)

# DECLARATIVE SINCE v0.22.1, AND IT COST ONE FIELD GROUP. What kept it built-in
# was two dates that only matter together: the declarative engine has ONE cadence
# anchor, and this rule's gate is a CONJUNCTION over two independent fields — the
# block was sprayed inside the season AND its water was tested outside the
# cadence. Neither half fires on its own. Ground nobody is spraying raises
# nothing however stale its water; a block sprayed yesterday raises nothing if
# its water is current.
#
# `gate_date_field` + `gate_within_days` is the second anchor, used ONLY as a
# gate: "consider a row only where <field> is inside <n> days". A block with no
# spray date at all is gated OUT, which is the opposite of what
# `missing_date_behaviour` does for the cadence anchor — and both are right. No
# inspection ever recorded is the most overdue cabin there is; no spray ever
# recorded is the least urgent block there is.
#
# `spray_season_days` STAYS IN `extra_parameters` as well as being the gate
# interval. A site that tuned it in v0.22.0 tuned that key, and the seeder does
# not overwrite an edited rule — so the key an operator may already have moved
# keeps meaning what it meant, and the patch that migrates an existing row reads
# it across into `gate_within_days`.
shape(
	"water_test_stale",
	target_doctype="Field",
	date_field="water_test_last_date",
	cadence_days=WATER_TEST_DAYS,
	threshold_critical_days=-1,
	threshold_warning_days=-1,
	severity_expired=SEVERITY_CRITICAL,
	missing_date_behaviour="Raise",
	due_date_mode="From Anchor",
	gate_date_field="last_spray_date",
	gate_within_days=SPRAY_SEASON_DAYS,
	gate_scope="Direct",
	scope_filters=[{"field": "condition", "op": "ne", "value": "Fallow", "default": ""}],
	extra_parameters={"spray_season_days": SPRAY_SEASON_DAYS},
	message_template=(
		"{{ name }} was sprayed {{ gate_days_since }} day(s) ago and "
		"{%- if days_since_anchor is none %} has NO agricultural water test on record at all"
		"{%- else %} was last water-tested {{ days_since_anchor }} days ago, on "
		"{{ water_test_last_date }}{% endif %}. FSMA Produce Safety Rule Subpart E wants "
		"agricultural water tested within {{ cadence_days }} days, and the water going through the "
		"sprayer onto a crop being harvested is agricultural water. This block is in active "
		"rotation, so the next application is not defensible until the test is done."
	),
	audit_packet_types=["FSMA", "OSHA"],
	retention_years=2,
)


# ── 4. housing_inspection_overdue ───────────────────────────────────────────
def _scan_housing_inspections(context: dict) -> list:
	today = context["today"]
	out = []
	for row in _rows(
		"Housing Unit",
		_company_filter({}, context.get("company") or "", "owning_entity"),
		(
			"name",
			"unit_name",
			"unit_type",
			"parcel",
			"owning_entity",
			"condition",
			"last_habitability_inspection",
		),
	):
		if str(row.get("unit_type") or "") in NON_RESIDENTIAL_TYPES:
			continue
		if str(row.get("condition") or "") == "Uninhabitable":
			# Already off the roster: assignments into it are refused, so there is
			# nobody to protect and an inspection would be a formality. It comes
			# back the moment somebody marks it habitable again.
			continue
		inspected = days_since(today, row.get("last_habitability_inspection"))
		if inspected is not None and inspected <= INSPECTION_DAYS:
			continue

		if inspected is None:
			detail = "has never had a habitability inspection recorded"
			due = ""
		else:
			detail = f"was last inspected {inspected} days ago, on {row['last_habitability_inspection']}"
			due = str(
				frappe.utils.add_days(
					frappe.utils.getdate(row["last_habitability_inspection"]), INSPECTION_DAYS
				)
			)
		out.append(
			Observation(
				source_doctype="Housing Unit",
				source_docname=row["name"],
				message=(
					f"{row['name']}"
					+ (f" on {row['parcel']}" if row.get("parcel") else "")
					+ f" {detail}. Oregon's agricultural labor housing rules and 29 CFR 1910.142 "
					"expect an annual walk, and this unit can currently be assigned to somebody "
					"who will sleep in it tonight."
				),
				severity=SEVERITY_WARNING,
				due_date=due,
				company=str(row.get("owning_entity") or ""),
			)
		)
	return out


register(
	Rule(
		key="housing_inspection_overdue",
		title="A cabin somebody can be assigned to has no current habitability inspection",
		category="Housing",
		requires=("Housing Unit",),
		framework="OAR 437-004-1120 agricultural labor housing; 29 CFR 1910.142; FSMA Subpart L",
		purpose="Somebody sleeps there tonight, and nobody has confirmed this season that it is fit to.",
		kairotic_gate=(
			"Scoped to units that can actually be assigned. A shower block, a kitchen and a "
			"shop take no assignment and raise nothing; a unit already marked Uninhabitable "
			"raises nothing, because assignments into it are already refused and there is "
			"nobody to protect — and it starts raising again the day somebody marks it "
			"habitable. Never-inspected counts as overdue, which is the answer that gets "
			"somebody to go and look. Auto-dismisses when an inspection is recorded."
		),
		# OAR 437-004-1120 is the habitability walk. FSMA Subpart L cares about
		# worker FACILITIES rather than about the annual inspection cycle, so
		# tagging it here would put a camp item in a produce-safety packet.
		regimes=("OR-OSHA",),
		scan=_scan_housing_inspections,
	)
)

# PURE DECLARATIVE, AND IT IS THE RULE THAT PAID FOR TWO PRIMITIVES.
#
# `missing_date_behaviour = Raise` exists because of this rule. A cabin nobody
# has ever inspected is the most overdue cabin in the camp, and every naive
# date-driven engine answers "no date, no alert" — which hides exactly the unit
# somebody needs to walk. The opposite reading is right for an expiry (a
# training with no expiry does not lapse), so it had to be a choice on the
# record rather than a convention in the engine.
#
# Both scope filters carry a `default`, and both defaults are load-bearing. In
# SQL, `condition != 'Uninhabitable'` EXCLUDES every row whose condition was
# never set — which is most of them on a new camp — and the rule would go quiet
# on the units nobody has touched. Read in Python against a default of "", they
# pass, which is what the legacy `str(row.get("condition") or "")` did.
shape(
	"housing_inspection_overdue",
	target_doctype="Housing Unit",
	date_field="last_habitability_inspection",
	cadence_days=INSPECTION_DAYS,
	threshold_critical_days=-1,
	threshold_warning_days=-1,
	severity_expired=SEVERITY_WARNING,
	missing_date_behaviour="Raise",
	scope_filters=[
		{"field": "unit_type", "op": "nin", "value": list(NON_RESIDENTIAL_TYPES), "default": ""},
		{"field": "condition", "op": "ne", "value": "Uninhabitable", "default": ""},
	],
	message_template=(
		"{{ name }}{% if parcel %} on {{ parcel }}{% endif %} "
		"{%- if days_since_anchor is none %} has never had a habitability inspection recorded"
		"{%- else %} was last inspected {{ days_since_anchor }} days ago, on "
		"{{ last_habitability_inspection }}{% endif %}. Oregon's agricultural labor housing rules "
		"and 29 CFR 1910.142 expect an annual walk, and this unit can currently be assigned to "
		"somebody who will sleep in it tonight."
	),
	producer_task_template=None,
	audit_packet_types=["OSHA"],
	retention_years=3,
)


# ── 5. housing_detector_test_stale ──────────────────────────────────────────
def _scan_detectors(context: dict) -> list:
	today = context["today"]
	detector_days = _interval(context, "cadence_days", DETECTOR_DAYS)
	severity = _severity_of(context, "severity_expired", SEVERITY_WARNING)
	out = []
	for row in _rows(
		"Housing Unit",
		_company_filter({}, context.get("company") or "", "owning_entity"),
		(
			"name",
			"unit_name",
			"unit_type",
			"parcel",
			"owning_entity",
			"condition",
			"fsma_worker_facility",
			"smoke_detector_last_test",
			"co_detector_last_test",
		),
	):
		if str(row.get("unit_type") or "") in NON_RESIDENTIAL_TYPES:
			continue
		if str(row.get("condition") or "") == "Uninhabitable":
			continue
		if not compat.checked(row.get("fsma_worker_facility")):
			# The FSMA worker-facility flag is what puts a building inside Subpart L
			# and inside the camp's inspected estate. A shed on the same parcel is
			# not a bunkhouse and does not need a detector test on record.
			continue
		if not _scoped(context, row):
			continue

		stale = []
		for label, fieldname in (("smoke", "smoke_detector_last_test"), ("CO", "co_detector_last_test")):
			since = days_since(today, row.get(fieldname))
			if since is None:
				stale.append(f"no {label} detector test has ever been recorded")
			elif since > detector_days:
				stale.append(f"the {label} detector was last tested {since} days ago, on {row[fieldname]}")
		if not stale:
			continue

		out.append(
			Observation(
				source_doctype="Housing Unit",
				source_docname=row["name"],
				message=(
					f"{row['name']} is a FSMA worker facility and "
					+ " and ".join(stale)
					+ ". A detector nobody has tested inside a year is a detector nobody knows "
					"works, and a camp cabin with a propane heater is exactly where that matters."
				),
				severity=severity,
				due_date="",
				company=str(row.get("owning_entity") or ""),
			)
		)
	return out


register(
	Rule(
		key="housing_detector_test_stale",
		title="A worker-facility building has no current detector test",
		category="Housing",
		requires=("Housing Unit",),
		framework="FSMA Produce Safety Rule Subpart L; OAR 437-004-1120; ORS 479 / ORS 690",
		purpose="A propane heater in a cabin with an untested CO detector is how somebody dies in their sleep.",
		kairotic_gate=(
			"Only buildings flagged `fsma_worker_facility` — the flag is what puts a "
			"building inside Subpart L and inside the camp's inspected estate, and a shed on "
			"the same parcel is not a bunkhouse. Uninhabitable units are excluded for the "
			"same reason as the inspection rule. Auto-dismisses when both detector tests are "
			"recorded inside a year."
		),
		# The fire code and OAR 437-004-1120 both reach it, and `Internal`
		# because an annual detector test is work this operation does whether
		# or not anybody is coming to look at it — which is most years.
		regimes=("OR-OSHA", "Internal"),
		scan=_scan_detectors,
	)
)

# DECLARATIVE SINCE v0.22.1, AND IT COST THE PLURAL ANCHOR. A cabin has a smoke
# detector and a CO detector, they are tested independently, EITHER being stale
# fires the rule, and the message has to NAME WHICH — "the CO detector was last
# tested 400 days ago" is a different errand from "no smoke detector test has
# ever been recorded", and an alert that said only "a detector is overdue" would
# send somebody to test the wrong one.
#
# `date_fields_json` carries both anchors with a label each. The severity folds
# to the worst of them; the message is handed `stale_dates`, which holds ONLY the
# fields that actually reached a band, so a cabin whose smoke test is current and
# whose CO test is four hundred days old produces one alert naming the CO
# detector and not the other.
#
# The labels are FRAGMENTS — "smoke", "CO" — because the sentence around them
# supplies "detector". A label that read as a sentence would produce a message
# that read as a list.
shape(
	"housing_detector_test_stale",
	target_doctype="Housing Unit",
	date_field="smoke_detector_last_test",
	date_fields=[
		{"field": "smoke_detector_last_test", "label": "smoke"},
		{"field": "co_detector_last_test", "label": "CO"},
	],
	cadence_days=DETECTOR_DAYS,
	threshold_critical_days=-1,
	threshold_warning_days=-1,
	severity_expired=SEVERITY_WARNING,
	missing_date_behaviour="Raise",
	due_date_mode="None",
	scope_filters=[
		{"field": "unit_type", "op": "nin", "value": list(NON_RESIDENTIAL_TYPES), "default": ""},
		{"field": "condition", "op": "ne", "value": "Uninhabitable", "default": ""},
		# The FSMA worker-facility flag is what puts a building inside Subpart L and
		# inside the camp's inspected estate. A shed on the same parcel is not a
		# bunkhouse and does not need a detector test on record.
		{"field": "fsma_worker_facility", "op": "istrue", "default": ""},
	],
	message_template=(
		"{{ name }} is a FSMA worker facility and "
		"{%- for entry in stale_dates %}{% if not loop.first %} and{% endif %}"
		"{% if entry.days_since is none %} no {{ entry.label }} detector test has ever been recorded"
		"{% else %} the {{ entry.label }} detector was last tested {{ entry.days_since }} days ago, "
		"on {{ entry.date }}{% endif %}{% endfor %}. A detector nobody has tested inside a year is a "
		"detector nobody knows works, and a camp cabin with a propane heater is exactly where that "
		"matters."
	),
	audit_packet_types=["OSHA", "FSMA"],
	retention_years=3,
)


# ── 6. i9_expired ───────────────────────────────────────────────────────────
def _employee_field(fieldname: str) -> bool:
	return compat.has_field("Employee", fieldname)


def _scan_i9(context: dict) -> list:
	if not _employee_field("i9_status"):
		# The compliance field has not been installed on this site, so there is
		# nothing to read. Reported as an empty scan rather than as a failure:
		# `install_compliance_fields` is the fix and it says so itself.
		return []
	today = context["today"]
	filters = _company_filter({"i9_status": "Expired"}, context.get("company") or "")
	if compat.has_field("Employee", "status"):
		filters["status"] = "Active"
	out = []
	for row in _rows(
		"Employee",
		filters,
		("name", "employee_name", "company", "i9_status", "date_of_joining", "status"),
	):
		out.append(
			Observation(
				source_doctype="Employee",
				source_docname=row["name"],
				message=(
					f"{row.get('employee_name') or row['name']} has an EXPIRED I-9. Employment "
					"eligibility has to be re-verified when a work authorisation document "
					"expires, and until it is this person cannot lawfully be put on a crew "
					"tomorrow. ICE penalties under 8 USC 1324a are assessed per form."
				),
				severity=SEVERITY_CRITICAL,
				due_date=today,
				company=str(row.get("company") or ""),
			)
		)
	return out


register(
	Rule(
		key="i9_expired",
		title="An active employee's I-9 has expired",
		category="Workforce",
		requires=("Employee",),
		framework="IRCA 8 USC 1324a; Form I-9 re-verification",
		purpose="This person cannot lawfully be scheduled tomorrow. It is a rostering fact before it is a filing fact.",
		kairotic_gate=(
			"Reads the I-9 status on the employee record and fires only on Expired — not on "
			"a re-verification date this app has invented, and not on Pending, which is a "
			"person inside their lawful three-day window and not yet a problem. Scoped to "
			"ACTIVE employees where the site records employment status: a former employee's "
			"expired I-9 blocks nothing, and alerting on it would fill the calendar with "
			"people who left. Auto-dismisses when the status moves off Expired."
		),
		# IRCA is federal immigration law, not one of the audit regimes this app
		# models, and there is no I-9 packet to file it in. Tagged `Internal`
		# rather than left bare so it is still reachable by a regime filter.
		regimes=("Internal",),
		scan=_scan_i9,
	)
)

# PURE DECLARATIVE, AND IT IS THE RULE WITH NO CLOCK AT ALL. There is no date to
# count down to: an I-9 is either recorded as Expired or it is not, and the
# moment it is, the person cannot lawfully be put on a crew. `date_field` empty
# therefore means "every matching row raises, at `severity_expired`" — which
# reads oddly until you notice it is the truth: past due by the only measure the
# rule has.
#
# `requires_fields` exists for this rule. `i9_status` is a compliance column this
# app installs on demand, and a site that has not run `install_compliance_fields`
# has no column to read. A rule that scanned anyway would report every employee
# as clean, which is the single most dangerous wrong answer in the file — so it
# returns an EMPTY SCAN instead, which still auto-dismisses stale alerts, rather
# than a failure, which would not.
shape(
	"i9_expired",
	target_doctype="Employee",
	date_field="",
	requires_fields="i9_status",
	severity_expired=SEVERITY_CRITICAL,
	threshold_critical_days=-1,
	threshold_warning_days=-1,
	due_date_mode="Today",
	scope_filters=[
		{"field": "i9_status", "op": "eq", "value": "Expired"},
		# Only people who are still employed. A former employee's expired I-9
		# blocks nothing, and alerting on it fills the calendar with leavers. The
		# default matters: a site whose Employee has no `status` column at all
		# gets the filter skipped (see `row_matches`), which is the legacy
		# `if compat.has_field(...)` guard said as data.
		{"field": "status", "op": "eq", "value": "Active", "default": "Active"},
	],
	message_template=(
		"{{ employee_name or name }} has an EXPIRED I-9. Employment eligibility has to be "
		"re-verified when a work authorisation document expires, and until it is this person "
		"cannot lawfully be put on a crew tomorrow. ICE penalties under 8 USC 1324a are assessed "
		"per form."
	),
	audit_packet_types=["DOL"],
	retention_years=3,
)


# ── 7. flc_license_expiring ─────────────────────────────────────────────────
def _scan_flc(context: dict) -> list:
	if not _employee_field("flc_license_expiration"):
		return []
	today = context["today"]
	filters = _company_filter({}, context.get("company") or "")
	if compat.has_field("Employee", "status"):
		filters["status"] = "Active"
	out = []
	for row in _rows(
		"Employee",
		filters,
		("name", "employee_name", "company", "status", "flc_license_status", "flc_license_expiration"),
	):
		expires = row.get("flc_license_expiration")
		remaining = days_until(today, expires)
		if remaining is None or remaining > CERT_DEFAULT_WINDOW_DAYS:
			continue
		who = row.get("employee_name") or row["name"]
		if remaining < 0:
			severity = SEVERITY_CRITICAL
			message = (
				f"{who}'s farm labor contractor licence EXPIRED {abs(remaining)} day(s) ago, on "
				f"{expires}. They cannot lawfully recruit, supervise or transport agricultural "
				"workers, and under MSPA and ORS 658.405 using an unlicensed contractor is the "
				"grower's violation as well as theirs — take them off the crew schedule."
			)
		elif remaining <= CERT_CRITICAL_DAYS:
			severity = SEVERITY_CRITICAL
			message = (
				f"{who}'s farm labor contractor licence expires in {remaining} day(s), on "
				f"{expires}. Renewal needs a bond and a background check and does not turn round "
				"in a week — a crew boss whose licence lapses mid-harvest is a crew with nobody "
				"who can lawfully supervise it."
			)
		else:
			severity = SEVERITY_WARNING
			message = (
				f"{who}'s farm labor contractor licence expires in {remaining} day(s), on {expires}. "
				"Start the renewal: the bond and background check are the long pole."
			)
		out.append(
			Observation(
				source_doctype="Employee",
				source_docname=row["name"],
				message=message,
				severity=severity,
				due_date=str(expires),
				company=str(row.get("company") or ""),
			)
		)
	return out


register(
	Rule(
		key="flc_license_expiring",
		title="A crew boss's farm labor contractor licence is expiring",
		category="Workforce",
		requires=("Employee",),
		framework="MSPA 29 USC 1801; ORS 658.405 farm labor contractor licensing",
		purpose=(
			"Using an unlicensed contractor is the grower's violation as well as the "
			"contractor's, which makes somebody else's licence the grower's problem."
		),
		kairotic_gate=(
			"Reads the expiration date on the employee record, and only for people who are "
			"still employed. Fires at 90 days, which is what an Oregon renewal actually "
			"takes once the bond and background check are counted, and escalates to Critical "
			"at 30 when starting late means lapsing. Auto-dismisses on renewal."
		),
		# ORS 658.405 licensing is enforced by Oregon BOLI, which is not OR-OSHA
		# and is not a scheme audit. Tagging it OR-OSHA would put it in a packet
		# handed to an inspector with no jurisdiction over it.
		regimes=("Internal",),
		scan=_scan_flc,
	)
)

# PURE DECLARATIVE. Three bands, three sentences, one date — the shape the
# declarative engine was designed around, and the one that made it worth
# building. Ninety days is what an Oregon renewal actually takes once the bond
# and the background check are counted; thirty is where starting late means
# lapsing. Both are now numbers on a record, which is the point: an operation
# whose contractor's bonding agent is slower changes 90 to 120 and does not wait
# for a release.
shape(
	"flc_license_expiring",
	target_doctype="Employee",
	date_field="flc_license_expiration",
	cadence_days=0,
	requires_fields="flc_license_expiration",
	threshold_critical_days=CERT_CRITICAL_DAYS,
	threshold_warning_days=CERT_DEFAULT_WINDOW_DAYS,
	severity_critical=SEVERITY_CRITICAL,
	severity_warning=SEVERITY_WARNING,
	severity_expired=SEVERITY_CRITICAL,
	missing_date_behaviour="Skip",
	scope_filters=[{"field": "status", "op": "eq", "value": "Active", "default": "Active"}],
	message_template=(
		"{%- set who = employee_name or name -%}"
		"{%- if days_remaining < 0 -%}"
		"{{ who }}'s farm labor contractor licence EXPIRED {{ days_overdue }} day(s) ago, on "
		"{{ flc_license_expiration }}. They cannot lawfully recruit, supervise or transport "
		"agricultural workers, and under MSPA and ORS 658.405 using an unlicensed contractor is "
		"the grower's violation as well as theirs — take them off the crew schedule."
		"{%- elif days_remaining <= threshold_critical_days -%}"
		"{{ who }}'s farm labor contractor licence expires in {{ days_remaining }} day(s), on "
		"{{ flc_license_expiration }}. Renewal needs a bond and a background check and does not "
		"turn round in a week — a crew boss whose licence lapses mid-harvest is a crew with "
		"nobody who can lawfully supervise it."
		"{%- else -%}"
		"{{ who }}'s farm labor contractor licence expires in {{ days_remaining }} day(s), on "
		"{{ flc_license_expiration }}. Start the renewal: the bond and background check are the "
		"long pole."
		"{%- endif -%}"
	),
	audit_packet_types=["DOL"],
	retention_years=3,
)


# ── 8. filing_response_due ──────────────────────────────────────────────────
def _scan_filings(context: dict) -> list:
	today = context["today"]
	out = []
	for row in _rows(
		"Regulatory Filing",
		_company_filter({}, context.get("company") or ""),
		(
			"name",
			"filing_name",
			"agency",
			"filing_type",
			"company",
			"status",
			"submission_date",
			"docket_number",
			"response_due_date",
			"response_received_date",
		),
	):
		if row.get("response_received_date"):
			# They answered. Nothing to chase.
			continue
		if str(row.get("status") or "") in ("Draft", "Withdrawn"):
			continue
		remaining = days_until(today, row.get("response_due_date"))
		if remaining is None or remaining > FILING_RESPONSE_DAYS:
			continue
		docket = f" under docket {row['docket_number']}" if row.get("docket_number") else ""
		when = f"was due {abs(remaining)} day(s) ago" if remaining < 0 else f"is due in {remaining} day(s)"
		out.append(
			Observation(
				source_doctype="Regulatory Filing",
				source_docname=row["name"],
				message=(
					f"A response to {row['name']} ({row.get('agency') or 'the agency'}, "
					f"{row.get('filing_type') or 'filing'}{docket}) {when}, on "
					f"{row['response_due_date']}, and none has been recorded. Chasing a docket "
					"before the deadline is a different conversation from chasing it after."
				),
				severity=SEVERITY_INFO if remaining >= 0 else SEVERITY_WARNING,
				due_date=str(row["response_due_date"]),
				company=str(row.get("company") or ""),
			)
		)
	return out


register(
	Rule(
		key="filing_response_due",
		title="An agency response is due on a filing and has not arrived",
		category="Filings",
		requires=("Regulatory Filing",),
		framework="Agency-specific response deadlines (USDA, ODA, DOL, EPA, IRS, OR-DOR)",
		purpose="Chasing a docket before the deadline is a different conversation from chasing it after.",
		kairotic_gate=(
			"Fires only on filings that were ACTUALLY SUBMITTED and have NO response "
			"recorded. A draft raises nothing, because nothing was sent; a filing with a "
			"response date raises nothing, because the thing being waited for has happened. "
			"That second clause is the auto-dismissal: recording the response clears the "
			"alert without anybody switching a reminder off. Escalates from Info to Warning "
			"once the deadline has actually passed — at that point it is no longer a heads-up."
		),
		# The deadline belongs to whichever agency the filing went to, and that is
		# on the filing rather than in this vocabulary. `Internal` is the honest
		# tag: real work, real date, no audit packet asks for it by name.
		regimes=("Internal",),
		scan=_scan_filings,
	)
)

# PURE DECLARATIVE, AND IT IS WHY THE SEVERITY OF EACH BAND IS ITSELF A FIELD.
# This rule escalates from Info to Warning as the deadline passes — the only one
# of the thirteen that does — and an engine with a hardcoded Critical/Warning
# ladder could not express it. `severity_warning = Info` says "the band before
# the deadline is context, not a problem"; `severity_expired = Warning` says
# "once it has passed it is". Two fields, one honest escalation.
#
# The `isnull` filter on `response_received_date` IS the auto-dismissal: record
# the agency's answer and the rule stops observing the filing, so the sweep
# takes the alert off without anybody switching a reminder off.
shape(
	"filing_response_due",
	target_doctype="Regulatory Filing",
	date_field="response_due_date",
	cadence_days=0,
	threshold_critical_days=-1,
	threshold_warning_days=FILING_RESPONSE_DAYS,
	severity_warning=SEVERITY_INFO,
	severity_expired=SEVERITY_WARNING,
	missing_date_behaviour="Skip",
	scope_filters=[
		{"field": "response_received_date", "op": "isnull"},
		{"field": "status", "op": "nin", "value": ["Draft", "Withdrawn"], "default": ""},
	],
	message_template=(
		"A response to {{ name }} ({{ agency or 'the agency' }}, {{ filing_type or 'filing' }}"
		"{%- if docket_number %} under docket {{ docket_number }}{% endif %}) "
		"{% if days_remaining < 0 %}was due {{ days_overdue }} day(s) ago"
		"{% else %}is due in {{ days_remaining }} day(s){% endif %}, on {{ response_due_date }}, "
		"and none has been recorded. Chasing a docket before the deadline is a different "
		"conversation from chasing it after."
	),
	audit_packet_types=["Other"],
	retention_years=3,
)


# ── 9. audit_action_overdue ─────────────────────────────────────────────────
def _scan_corrective_actions(context: dict) -> list:
	"""Open corrective actions past their due date, one alert per audit.

	One alert per AUDIT rather than per action, deliberately. Five open items on
	one PrimusGFS audit are one conversation with one auditor, and five separate
	rows on the calendar would look like five problems. The message names how many
	and which is worst.
	"""
	today = context["today"]
	grace_days = _interval(context, "grace_days", ACTION_OVERDUE_GRACE_DAYS)
	worst_severity = _severity_of(context, "severity_critical", SEVERITY_CRITICAL)
	ordinary_severity = _severity_of(context, "severity_warning", SEVERITY_WARNING)
	out = []
	audits = _rows(
		"Audit Event",
		_company_filter({}, context.get("company") or ""),
		("name", "audit_name", "audit_type", "auditor", "company", "audit_date", "corrective_actions_closed"),
	)
	for row in audits:
		if row.get("corrective_actions_closed"):
			continue
		if not _scoped(context, row):
			continue
		try:
			doc = frappe.get_doc("Audit Event", row["name"])
		except Exception:
			continue
		overdue = []
		worst_due = ""
		for index, action in doc.open_actions():
			due = str(action.get("due_date") or "")
			if not due:
				continue
			late = days_since(today, due)
			if late is None or late < grace_days:
				continue
			overdue.append((index, action, late, due))
			if not worst_due or due < worst_due:
				worst_due = due
		if not overdue:
			continue

		worst = max(overdue, key=lambda entry: entry[2])
		severity = (
			worst_severity
			if any(
				str(action.get("severity") or "") in ("Major", "Critical") for _i, action, _l, _d in overdue
			)
			else ordinary_severity
		)
		out.append(
			Observation(
				source_doctype="Audit Event",
				source_docname=row["name"],
				message=(
					f"{row['name']} ({row.get('audit_type') or 'audit'}"
					+ (f", {row['auditor']}" if row.get("auditor") else "")
					+ f") has {len(overdue)} overdue corrective action(s). The worst is "
					f"{worst[2]} day(s) past its {worst[3]} deadline: "
					f"{str(worst[1].get('finding') or '')[:160]}. An operation is not judged on "
					"having no findings — it is judged on closing them, and an open action "
					"nobody can produce evidence for is how last year's finding becomes this "
					"year's penalty."
				),
				severity=severity,
				due_date=worst_due,
				company=str(row.get("company") or ""),
			)
		)
	return out


register(
	Rule(
		key="audit_action_overdue",
		title="An audit has corrective actions past their deadline",
		category="Audits",
		requires=("Audit Event",),
		framework="GAP / GlobalGAP / PrimusGFS corrective action deadlines; agency inspection follow-up",
		purpose=(
			"The one thing an auditor is certain to ask about on the next visit, and the "
			"gate `generate_audit_packet` refuses an incomplete period on."
		),
		kairotic_gate=(
			"Reads the deadline each SCHEME set on each action, not an interval this app "
			"chose — a Critical usually gets days and a Minor gets weeks, and both are on "
			"the row. Actions with no due date raise nothing, because there is no deadline to "
			"be past. An audit whose closure date is set raises nothing, and the controller "
			"will not let that date be set while an action is open, so the two cannot "
			"disagree. Severity follows the worst open finding: a Major or Critical left late "
			"is Critical, because at that point the certificate is at risk rather than the "
			"paperwork. One alert per audit rather than per action — five open items on one "
			"audit are one conversation, not five problems."
		),
		# An open corrective action is the first thing each scheme's auditor asks
		# about on the next visit — and it is the gate `generate_audit_packet`
		# refuses an incomplete period on, in every packet type.
		regimes=("GAP", "GlobalGAP", "PrimusGFS", "Internal"),
		scan=_scan_corrective_actions,
	)
)

# BUILT-IN, AND THE REASON IS A CHILD TABLE REDUCED TO ONE ROW. The declarative
# engine walks the rows of ONE doctype and asks a question of each. This rule
# walks an Audit Event's corrective-action CHILD ROWS, keeps the overdue ones,
# picks the worst, takes its severity from the worst finding's own severity, and
# raises ONE alert per audit rather than one per action — because five open items
# on one PrimusGFS audit are one conversation with one auditor, and five rows on
# the calendar would look like five problems.
#
# Every part of that is an aggregation, and an aggregation is not a filter. The
# primitive would be a whole second engine — group-by, fold, pick — which is a
# fair description of "write it in Python". This one stays built-in on purpose;
# it is not a gap in the vocabulary, it is a different shape of question.
shape(
	"audit_action_overdue",
	target_doctype="Audit Event",
	builtin_scanner="audit_action_overdue",
	date_field="",
	threshold_critical_days=-1,
	threshold_warning_days=-1,
	severity_critical=SEVERITY_CRITICAL,
	severity_warning=SEVERITY_WARNING,
	severity_expired=SEVERITY_WARNING,
	due_date_mode="None",
	extra_parameters={"grace_days": ACTION_OVERDUE_GRACE_DAYS},
	audit_packet_types=["GAP", "GlobalGAP", "FSMA", "OSHA"],
	retention_years=3,
)


# ── 10. housing_corrective_action_open ──────────────────────────────────────
#: The two camp records a finding can come out of, and how to describe one. Both
#: are read by one rule rather than two, because a cabin with a water stain and a
#: cabin with a dead CO detector are the same conversation with the same person
#: on the same walk round the camp, and splitting them into two alert types would
#: split one afternoon's work across two lists.
_CAMP_RECORDS = (
	("Housing Inspection", "inspection_date", "the habitability inspection", SEVERITY_CRITICAL),
	("Detector Test", "test_date", "the detector test", SEVERITY_CRITICAL),
)


def _scan_camp_corrective_actions(context: dict) -> list:
	"""Camp records that found something nobody has closed or superseded.

	THE GATE IS WHETHER THE FINDING IS STILL TRUE, and there are two ways for it
	to stop being true. Somebody can close it by hand — `corrective_action_closed`
	with a note saying what was done. Or a LATER CLEAN RECORD for the same unit
	can supersede it, which is the one that matters in practice: a cabin
	re-inspected in September with nothing found says more about July's water
	stain than a checkbox ever will, and it requires nobody to remember a field.

	So this fires on a finding that is open AND unsuperseded, and goes quiet by
	itself the moment either happens. That is the difference between a compliance
	rule and a task list somebody has to tidy.
	"""
	today = context["today"]
	out = []
	for doctype, date_field, what, severity in _CAMP_RECORDS:
		if not compat.doctype_exists(doctype):
			continue
		rows = _rows(
			doctype,
			_company_filter({"workflow_state": CORRECTIVE_ACTION_REQUIRED}, context.get("company") or ""),
			("name", "unit", "company", date_field, "workflow_state", "corrective_action_closed", "findings"),
		)
		# Every clean record per unit, so "superseded" is one pass rather than a
		# query per finding. A camp with fifty cabins and four years of history is
		# two queries, not four hundred.
		clean = _clean_records_by_unit(doctype, date_field, context.get("company") or "")

		for row in rows:
			if str(row.get("corrective_action_closed") or "").strip():
				continue
			if not _scoped(context, row):
				continue
			found_on = str(row.get(date_field) or "")
			later = [date for date in clean.get(str(row.get("unit") or ""), ()) if date > found_on]
			if later:
				continue
			open_days = days_since(today, found_on)
			detail = str(row.get("findings") or "").strip() or "a fault was recorded with no detail"
			out.append(
				Observation(
					source_doctype=doctype,
					source_docname=row["name"],
					message=(
						f"{row.get('unit')} — {what} on {found_on or 'an unrecorded date'} found: "
						f"{detail[:200]}"
						+ (f". Open {open_days} day(s)" if open_days is not None else "")
						+ ". Somebody sleeps in this building tonight. It closes when the fault is "
						"fixed and a fresh clean record is written for the same unit, or when the "
						"corrective action is closed by hand with a note saying what was done."
					),
					severity=severity,
					due_date="",
					company=str(row.get("company") or ""),
					category="Housing",
				)
			)
	return out


def _clean_records_by_unit(doctype: str, date_field: str, company: str) -> dict:
	"""unit → every date on which a CLEAN record was written for it."""
	out: dict = {}
	for row in _rows(
		doctype,
		_company_filter({"workflow_state": RECORDED}, company),
		("name", "unit", date_field),
	):
		out.setdefault(str(row.get("unit") or ""), []).append(str(row.get(date_field) or ""))
	return out


register(
	Rule(
		key="housing_corrective_action_open",
		title="A camp inspection or detector test found something nobody has fixed",
		category="Housing",
		requires=("Housing Inspection",),
		framework="OAR 437-004-1120 agricultural labor housing; 29 CFR 1910.142; FSMA Subpart L; ORS 479",
		purpose=(
			"The other half of doing the work. Sprint 7 could say a cabin had not been walked; "
			"this is what happens when somebody walks it and finds a dead CO detector."
		),
		kairotic_gate=(
			"Fires on a finding that is STILL TRUE, and there are exactly two ways for it to "
			"stop being true — both of which silence this rule by themselves. Somebody closes "
			"the corrective action by hand with a note saying what was done; or a later CLEAN "
			"record for the same unit supersedes it, which is the one that happens in practice "
			"and requires nobody to remember a field. A cabin re-inspected in September with "
			"nothing found says more about July's water stain than a checkbox does. Drafts raise "
			"nothing — a draft is a note, not evidence."
		),
		# A finding on a cabin: OAR 437-004-1120 for the camp itself and FSMA
		# Subpart L where the building is a worker facility. Both inspectors
		# ask what was found and whether it was fixed.
		regimes=("OR-OSHA", "FSMA"),
		scan=_scan_camp_corrective_actions,
	)
)

# DECLARATIVE SINCE v0.22.1, AND IT IS THE MIGRATION THAT COST THE MOST. What
# kept it built-in was SUPERSESSION — the one gate in the whole engine that is a
# question about OTHER ROWS. Whether a July water stain is still true depends on
# whether a September inspection of the SAME UNIT came back clean, and there is
# no filter on the finding's own row that can answer that.
#
# `superseded_by_later_clean_json` is that question as four values, and it took
# TWO rules declarative at once. But this rule needed two more things that
# `water_test_contamination` did not, and they are the reason it was the hard one:
#
#   * `target_doctypes_json`, because it walks Housing Inspection AND Detector
#     Test under one rule_id. That is deliberate and not an accident of history:
#     a cabin with a water stain and a cabin with a dead CO detector are the same
#     conversation with the same person on the same walk round the camp. Two
#     rule_ids would be two lists for one afternoon — and would change every
#     alert docname on every site that has one.
#   * `date_field_role = Timestamp`, because `inspection_date` is WHEN THE THING
#     WAS FOUND rather than a deadline. Left as a clock, a finding recorded today
#     has zero days remaining, reaches no band with these thresholds, and would
#     have silently stopped firing on the day it was written — which is exactly
#     the day somebody needs to see it.
shape(
	"housing_corrective_action_open",
	target_doctype="Housing Inspection",
	requires_doctypes="Housing Inspection",
	target_doctypes=[
		{
			"doctype": "Housing Inspection",
			"date_field": "inspection_date",
			"label": "the habitability inspection",
		},
		{"doctype": "Detector Test", "date_field": "test_date", "label": "the detector test"},
	],
	date_field="inspection_date",
	date_field_role="Timestamp",
	cadence_days=0,
	threshold_critical_days=-1,
	threshold_warning_days=-1,
	severity_expired=SEVERITY_CRITICAL,
	due_date_mode="None",
	scope_filters=[
		{"field": "workflow_state", "op": "eq", "value": CORRECTIVE_ACTION_REQUIRED},
		# Closed by hand, with a note saying what was done. The other exit.
		{"field": "corrective_action_closed", "op": "isnull"},
	],
	superseded_by_later_clean={
		"subject_field": "unit",
		"clean_state_field": "workflow_state",
		"clean_state_values": [RECORDED],
		"unreadable_counts_as_dirty": True,
	},
	message_template=(
		"{{ unit }} — {{ target_label }} on {{ anchor or 'an unrecorded date' }} found: "
		# `(findings or '')` and not `findings|trim`: Jinja's `trim` stringifies
		# first, so a NULL column renders the four characters `None` and the alert
		# reports a finding of "None" to somebody at seven in the morning.
		"{{ ((findings or '')|trim or 'a fault was recorded with no detail')[:200] }}"
		"{%- if days_since_anchor is not none %}. Open {{ days_since_anchor }} day(s){% endif %}"
		". Somebody sleeps in this building tonight. It closes when the fault is fixed and a fresh "
		"clean record is written for the same unit, or when the corrective action is closed by hand "
		"with a note saying what was done."
	),
	audit_packet_types=["OSHA", "FSMA"],
	retention_years=3,
)


# ── 11. water_test_contamination ────────────────────────────────────────────
def _scan_water_contamination(context: dict) -> list:
	"""Water samples that came back dirty, or unreadable, and have not been re-tested.

	Same gate as the camp rule and the same two exits: closed by hand, or
	superseded by a later clean sample from the same zone. The second is what a
	real operation does — treat the line, flush it, re-sample — and the rule going
	quiet on its own is what makes the re-sample worth doing rather than worth
	remembering to record.
	"""
	today = context["today"]
	if not compat.doctype_exists("Water Test"):
		return []
	rows = _rows(
		"Water Test",
		_company_filter({"workflow_state": CORRECTIVE_ACTION_REQUIRED}, context.get("company") or ""),
		(
			"name",
			"source",
			"block",
			"company",
			"test_date",
			"coliform_result",
			"ecoli_result",
			"contamination_detected",
			"corrective_action_closed",
			"findings",
		),
	)
	clean: dict = {}
	for row in _rows(
		"Water Test",
		_company_filter({"workflow_state": RECORDED}, context.get("company") or ""),
		("name", "source", "test_date"),
	):
		clean.setdefault(str(row.get("source") or ""), []).append(str(row.get("test_date") or ""))

	severity = _severity_of(context, "severity_expired", SEVERITY_CRITICAL)
	out = []
	for row in rows:
		if str(row.get("corrective_action_closed") or "").strip():
			continue
		if not _scoped(context, row):
			continue
		sampled = str(row.get("test_date") or "")
		if [date for date in clean.get(str(row.get("source") or ""), ()) if date > sampled]:
			continue
		open_days = days_since(today, sampled)
		detail = (
			f"coliform {row.get('coliform_result') or 'unrecorded'}, "
			f"E. coli {row.get('ecoli_result') or 'unrecorded'}"
		)
		out.append(
			Observation(
				source_doctype="Water Test",
				source_docname=row["name"],
				message=(
					f"{row.get('source')}"
					+ (f" (block {row.get('block')})" if row.get("block") else "")
					+ f" was sampled on {sampled or 'an unrecorded date'} and the result was not "
					f"clean: {detail}"
					+ (f". Open {open_days} day(s)" if open_days is not None else "")
					+ ". This is agricultural water going onto a crop being harvested, so FSMA "
					"Produce Safety Rule Subpart E is engaged and the next application is not "
					"defensible until the source is treated or switched and a clean sample comes "
					"back."
				),
				severity=severity,
				due_date="",
				company=str(row.get("company") or ""),
				category="Water and Sanitation",
			)
		)
	return out


register(
	Rule(
		key="water_test_contamination",
		title="An agricultural water source came back contaminated and has not been re-tested",
		category="Water and Sanitation",
		requires=("Water Test",),
		framework="FSMA Produce Safety Rule 21 CFR 112 Subpart E, including the 112.44(b) criterion",
		purpose=(
			"A stale water test is ignorance and this is knowledge. The stale rule stops firing "
			"the moment a sample is taken; this one starts if the sample says the water is dirty."
		),
		kairotic_gate=(
			"Fires on a result that is still the LATEST word on that zone. A later clean sample "
			"from the same source SUPERSEDES it, which is exactly what treating the line and "
			"re-sampling produces — so the rule rewards the work rather than the paperwork. "
			"Closing the corrective action by hand also silences it. An UNREADABLE result counts "
			"as not clean and fires here, because a result nobody can interpret is not evidence "
			"that the water is safe; treating it as clean is how a compliance file becomes a "
			"clean record of nothing."
		),
		# 21 CFR 112.44(b) is the criterion the sample failed. Narrower than the
		# stale-test rule above on purpose: a laboratory result over the limit
		# is a produce-safety fact, not a camp one.
		regimes=("FSMA",),
		scan=_scan_water_contamination,
	)
)

# DECLARATIVE SINCE v0.22.1, AND IT WAS THE CHEAP HALF OF THE SAME PRIMITIVE.
# The gate is "is this result still the LATEST word on that zone" — a question
# about other rows of the same doctype, which no filter on this row can answer.
# One doctype, one date, so it needed `superseded_by_later_clean_json` and
# `date_field_role = Timestamp` and nothing else. The camp rule paid for the
# other two fields; this rule got them for free, which is the two-for-one the
# v0.22.0 backlog predicted.
#
# `unreadable_counts_as_dirty` is the flag whose default matters most. A sample
# whose state nobody can read does NOT supersede a contamination finding —
# because a result nobody can interpret is not evidence that the water is safe,
# and treating it as clean is how a compliance file becomes a clean record of
# nothing.
shape(
	"water_test_contamination",
	target_doctype="Water Test",
	date_field="test_date",
	date_field_role="Timestamp",
	cadence_days=0,
	threshold_critical_days=-1,
	threshold_warning_days=-1,
	severity_expired=SEVERITY_CRITICAL,
	due_date_mode="None",
	scope_filters=[
		{"field": "workflow_state", "op": "eq", "value": CORRECTIVE_ACTION_REQUIRED},
		{"field": "corrective_action_closed", "op": "isnull"},
	],
	superseded_by_later_clean={
		"subject_field": "source",
		"clean_state_field": "workflow_state",
		"clean_state_values": [RECORDED],
		"unreadable_counts_as_dirty": True,
	},
	message_template=(
		"{{ source }}{% if block %} (block {{ block }}){% endif %} was sampled on "
		"{{ anchor or 'an unrecorded date' }} and the result was not clean: coliform "
		"{{ coliform_result or 'unrecorded' }}, E. coli {{ ecoli_result or 'unrecorded' }}"
		"{%- if days_since_anchor is not none %}. Open {{ days_since_anchor }} day(s){% endif %}"
		". This is agricultural water going onto a crop being harvested, so FSMA Produce Safety "
		"Rule Subpart E is engaged and the next application is not defensible until the source is "
		"treated or switched and a clean sample comes back."
	),
	audit_packet_types=["FSMA"],
	retention_years=2,
)


# ── 12. training_expiring ───────────────────────────────────────────────────
def _scan_training(context: dict) -> list:
	"""Training that has lapsed, or is close enough that booking a course is urgent.

	THE TWELFTH RULE, AND THE FIRST ONE ABOUT A PERSON'S QUALIFICATION RATHER THAN
	A DOCUMENT'S DATE. Rules 1 and 7 watch certificates, which are things an agency
	issues to the operation. This one watches what the crew was taught, which is
	the evidence four separate regimes ask for and the one an operation is most
	likely to have on paper in a binder nobody reads.

	IT READS `expires_date`, NOT `status`. `status` is computed in the controller
	and is therefore only correct on records somebody happened to save — so the
	expired ones would be exactly the ones still reading Active. Same argument as
	`_scan_certifications`, same reason.

	NO EXPIRY IS SKIPPED ENTIRELY. A new-hire orientation and a PSA grower
	certificate do not lapse, and raising a renewal alert nobody can clear is how
	a compliance calendar stops being read.
	"""
	today = context["today"]
	if not compat.doctype_exists(training_records.DOCTYPE):
		return []
	out = []
	for row in _rows(
		training_records.DOCTYPE,
		_company_filter({}, context.get("company") or ""),
		(
			"name",
			"employee",
			"employee_name",
			"company",
			"training_type",
			"provider",
			"completed_date",
			"expires_date",
			"regimes",
		),
	):
		expires = row.get("expires_date")
		remaining = days_until(today, expires)
		if remaining is None:
			# One-time training. Nothing to renew, so nothing to say.
			continue
		if remaining > training_records.EXPIRING_WINDOW_DAYS:
			continue

		who = row.get("employee_name") or row.get("employee") or "somebody"
		what = row.get("training_type") or "Training"
		regimes = training_records.parse(row.get("regimes"))
		# The regimes are in the MESSAGE, not merely on the record, because the
		# calendar entry is what somebody reads on a phone at seven in the morning
		# and "expires in 12 days" is a different decision from "WPS handler
		# training expires in 12 days and he cannot lawfully spray after that".
		coverage = (
			f" It is the operation's evidence for {', '.join(regimes)}."
			if regimes
			else " It carries NO regime tag, so no audit packet will pull it — tag it with "
			"record_training so the evidence counts for something."
		)

		if remaining < 0:
			severity = SEVERITY_CRITICAL
			message = (
				f"{what} for {who} EXPIRED {abs(remaining)} day(s) ago, on {expires}."
				+ _training_consequence(regimes)
				+ coverage
			)
		elif remaining <= training_records.CRITICAL_WINDOW_DAYS:
			severity = SEVERITY_CRITICAL
			message = (
				f"{what} for {who} expires in {remaining} day(s), on {expires}. Inside "
				f"{training_records.CRITICAL_WINDOW_DAYS} days, booking a course is no longer "
				"reliably enough to avoid the lapse — courses run on a published schedule and "
				"the next one may be after the date." + _training_consequence(regimes) + coverage
			)
		else:
			severity = SEVERITY_WARNING
			message = (
				f"{what} for {who} expires in {remaining} day(s), on {expires}, which is inside "
				f"the {training_records.EXPIRING_WINDOW_DAYS}-day window a retraining actually "
				"takes to arrange — trainer, crew, language, and a room." + coverage
			)

		out.append(
			Observation(
				source_doctype=training_records.DOCTYPE,
				source_docname=row["name"],
				message=message,
				severity=severity,
				due_date=str(expires),
				company=str(row.get("company") or ""),
				category="Workforce",
				# COPIED FROM THE RECORD, which is the only honest source. The
				# curriculum says what the course normally answers; the record says
				# what THAT afternoon actually covered, and a heat session that ran
				# out of time before the emergency-response topic did not satisfy
				# OAR 437-004-1131 that day. Inheriting from the Training Type would
				# produce an alert asserting evidence the record does not support.
				#
				# An untagged record produces an untagged alert — deliberately. The
				# message already says so and tells the reader how to fix it, and
				# inventing a tag so the row is not bare would make the calendar
				# claim coverage the register does not have.
				regimes=regimes,
			)
		)
	return out


def _training_consequence(regimes: list) -> str:
	"""What actually stops being lawful, per regime, in one sentence.

	Severity says how urgent; this says WHY, and the two are different facts. A
	worker whose WPS handler training has lapsed cannot legally perform an
	application — that is not a paperwork problem, it is a person who has to be
	taken off the sprayer tomorrow morning.
	"""
	if "WPS" in regimes:
		return (
			" A handler whose WPS training is more than twelve months old cannot lawfully "
			"perform an application (40 CFR 170.501; Oregon OAR 437-004-6501), so this takes "
			"somebody off the sprayer rather than merely off a list."
		)
	if "OR-OSHA" in regimes:
		return (
			" Oregon's heat rule (OAR 437-004-1131) requires the training annually BEFORE work "
			"at any site where the heat index will reach 80 °F, so a lapse is a citation the "
			"first hot morning rather than at the next inspection."
		)
	if "FSMA" in regimes:
		return (
			" FSMA Subpart C (§112.21–.30) requires training on hiring and periodically "
			"thereafter, and a covered farm with lapsed worker training has no answer to the "
			"first question of a produce safety inspection."
		)
	if "NOP" in regimes:
		return (
			" Organic handling training is part of the Organic System Plan the certifier "
			"inspects against (7 CFR 205.201), and OSP-practice divergence is the "
			"noncompliance Oregon Tilth writes most often."
		)
	return ""


register(
	Rule(
		key="training_expiring",
		title="A worker's training has lapsed, or is inside the window a retraining takes to arrange",
		category="Workforce",
		requires=(training_records.DOCTYPE,),
		framework=(
			"EPA Worker Protection Standard 40 CFR 170.401/.501 (12-month cycle) and 170.309 "
			"(2-year records); Oregon OSHA OAR 437-004-1131 heat illness, -1110(9) field "
			"sanitation hygiene, -9800 hazard communication, -1005(10) PPE, -0240 seasonal "
			"orientation; FDA FSMA 21 CFR 112.21–.30 and the §112.161 record form; USDA GAP "
			"worker health and hygiene; USDA NOP 7 CFR 205.201 organic handling"
		),
		purpose=(
			"Expiring training is the same shape of fact as an expiring certificate and had "
			"nowhere to be seen. A WPS handler whose training lapsed cannot lawfully spray "
			"tomorrow; an unretrained crew on the first 80 °F morning is a citation under "
			"Oregon's heat rule. Both were previously discoverable only by somebody opening "
			"a binder."
		),
		kairotic_gate=(
			"Fires on the RECORD'S OWN EXPIRY, read from `expires_date` rather than from a "
			"date in a calendar — a training 200 days out raises nothing however many times "
			"the sweep runs, the same one raises Warning at 90 days and Critical at 30, and "
			"it goes away by itself the moment a newer record is filed with a later expiry. "
			"Training with NO expiry — a new-hire orientation, a PSA grower certificate — is "
			"skipped entirely, because a renewal alert nobody can clear is how a calendar "
			"stops being read. Ninety days is what arranging a retraining actually takes "
			"(trainer, crew, language, room); inside thirty, the next scheduled course may "
			"already be after the lapse."
		),
		# Per row: copied off the training record, which can carry any tag in the
		# vocabulary. So the union IS the vocabulary, taken from it directly so
		# a regime added in a later release does not silently exclude this rule.
		regimes=training_records.REGIMES,
		scan=_scan_training,
	)
)

# PURE DECLARATIVE, AND IT IS THE ONE THAT PROVES THE MESSAGE TEMPLATE IS WORTH
# HAVING. This rule's text is three branches, a per-regime consequence clause and
# a coverage clause, and it is the alert somebody actually reads on a phone at
# seven in the morning — "WPS handler training expires in 12 days and he cannot
# lawfully spray after that" is a different decision from "expires in 12 days".
# All of it is now a template on a record, which means an operation can say the
# consequence in their own words, in their crew's language, without a release.
#
# `regimes_from_field` exists for this rule. The tags are copied off the TRAINING
# RECORD rather than off the rule, because the record says what that afternoon
# actually covered — a heat session that ran out of time before the
# emergency-response topic did not satisfy OAR 437-004-1131 that day, and
# inheriting from the curriculum would produce an alert asserting evidence the
# record does not support. An untagged record produces an untagged alert, and the
# message says so and says how to fix it.
shape(
	"training_expiring",
	target_doctype=training_records.DOCTYPE,
	date_field="expires_date",
	cadence_days=0,
	threshold_critical_days=training_records.CRITICAL_WINDOW_DAYS,
	threshold_warning_days=training_records.EXPIRING_WINDOW_DAYS,
	severity_critical=SEVERITY_CRITICAL,
	severity_warning=SEVERITY_WARNING,
	severity_expired=SEVERITY_CRITICAL,
	# Training with NO expiry — a new-hire orientation, a PSA grower certificate —
	# is skipped entirely, because a renewal alert nobody can clear is how a
	# calendar stops being read. That is `Skip`, and it is the opposite of the
	# housing rule's `Raise`, which is exactly why it is a field.
	missing_date_behaviour="Skip",
	regimes_from_field="regimes",
	category="Workforce",
	message_template=(
		"{%- set who = employee_name or employee or 'somebody' -%}"
		"{%- set what = training_type or 'Training' -%}"
		"{%- set consequence %}"
		"{%- if 'WPS' in regimes %} A handler whose WPS training is more than twelve months old "
		"cannot lawfully perform an application (40 CFR 170.501; Oregon OAR 437-004-6501), so "
		"this takes somebody off the sprayer rather than merely off a list."
		"{%- elif 'OR-OSHA' in regimes %} Oregon's heat rule (OAR 437-004-1131) requires the "
		"training annually BEFORE work at any site where the heat index will reach 80 °F, so a "
		"lapse is a citation the first hot morning rather than at the next inspection."
		"{%- elif 'FSMA' in regimes %} FSMA Subpart C (§112.21–.30) requires training on hiring "
		"and periodically thereafter, and a covered farm with lapsed worker training has no "
		"answer to the first question of a produce safety inspection."
		"{%- elif 'NOP' in regimes %} Organic handling training is part of the Organic System "
		"Plan the certifier inspects against (7 CFR 205.201), and OSP-practice divergence is the "
		"noncompliance Oregon Tilth writes most often."
		"{%- endif %}{% endset -%}"
		"{%- set coverage %}"
		"{%- if regimes %} It is the operation's evidence for {{ regimes|join(', ') }}."
		"{%- else %} It carries NO regime tag, so no audit packet will pull it — tag it with "
		"record_training so the evidence counts for something."
		"{%- endif %}{% endset -%}"
		"{%- if days_remaining < 0 -%}"
		"{{ what }} for {{ who }} EXPIRED {{ days_overdue }} day(s) ago, on {{ expires_date }}."
		"{{ consequence }}{{ coverage }}"
		"{%- elif days_remaining <= threshold_critical_days -%}"
		"{{ what }} for {{ who }} expires in {{ days_remaining }} day(s), on {{ expires_date }}. "
		"Inside {{ threshold_critical_days }} days, booking a course is no longer reliably enough "
		"to avoid the lapse — courses run on a published schedule and the next one may be after "
		"the date.{{ consequence }}{{ coverage }}"
		"{%- else -%}"
		"{{ what }} for {{ who }} expires in {{ days_remaining }} day(s), on {{ expires_date }}, "
		"which is inside the {{ threshold_warning_days }}-day window a retraining actually takes "
		"to arrange — trainer, crew, language, and a room.{{ coverage }}"
		"{%- endif -%}"
	),
	audit_packet_types=["FSMA", "GAP", "OSHA", "DOL"],
	retention_years=2,
)


# ── 12b. training_day_missed ── v0.212.0 ────────────────────────────────────
MISSED_DAY_GRACE_DAYS = 30


def _scan_missed_training_days(context: dict) -> list:
	"""Somebody registered on a multi-day course did not attend a required day.

	ONE ALERT PER PERSON PER COURSE, however many days they missed, keyed on their
	registration row on the course — which carries the `employee` link, so the
	alert knows who it is about without reading prose.

	IT CLEARS ITSELF. The finding stops being observed when the person attends a
	later session of the same Training Type (the make-up) or a record is filed for
	them, and an unobserved alert is auto-dismissed.

	THE DUE DATE IS WHEN IT STARTS TO COST SOMETHING: the expiry of the
	Certification the course renews, where the Training Type names one and the
	person holds it; else the course's own expiry; else thirty days after the
	missed day. Critical inside thirty days of that date.
	"""
	from .. import training_courses, training_sessions

	if not training_courses.ready():
		return []
	today = str(context["today"])
	critical_days = _interval(context, "threshold_critical_days", training_records.CRITICAL_WINDOW_DAYS)
	critical = _severity_of(context, "severity_critical", SEVERITY_CRITICAL)
	warning = _severity_of(context, "severity_warning", SEVERITY_WARNING)
	out = []
	for finding in training_courses.missed_days(context.get("company") or ""):
		if not _scoped(context, {"company": finding.get("company")}):
			continue
		registration = next(
			(
				row
				for row in training_sessions.attendees_of(finding["course"])
				if str(row.get("employee") or "") == finding["employee"]
			),
			None,
		)
		if registration is None:
			continue
		cert = finding.get("certification") or {}
		cert_expiry = str(cert.get("expiration_date") or "")[:10]
		due = (
			cert_expiry
			or str(finding.get("course_expires") or "")[:10]
			or str(frappe.utils.add_days(finding["last_missed"], MISSED_DAY_GRACE_DAYS))
		)
		days_left = training_records.days_until(today, due) if due else None
		severity = critical if days_left is not None and days_left <= critical_days else warning
		which = ", ".join(f"Day {day['day_number']} ({day['session_date']})" for day in finding["missed"])
		message = (
			f"{finding['employee_name']} missed {which} of {finding['training_type']} "
			f"({finding['course']}), so the course gives them no credit."
		)
		if cert_expiry:
			label = cert.get("cert_name") or cert.get("cert_type") or "certification"
			if days_left is not None and days_left < 0:
				message += f" Their {label} expired on {cert_expiry}."
			else:
				message += (
					f" Their {label} expires on {cert_expiry}"
					+ (f" ({days_left} day(s))" if days_left is not None else "")
					+ " — a make-up day is needed before then, or it lapses."
				)
		else:
			message += f" Book a make-up day by {due}."
		out.append(
			Observation(
				source_doctype=training_sessions.ATTENDEE_DOCTYPE,
				source_docname=str(registration["name"]),
				message=message,
				severity=severity,
				due_date=due,
				company=str(finding.get("company") or ""),
				category="Workforce",
			)
		)
	return out


register(
	Rule(
		key="training_day_missed",
		title="A registered attendee missed a required day of a multi-day course",
		category="Workforce",
		requires=("Training Session",),
		framework=(
			"The course's own terms: continuing-education credit for a licence renewal (e.g. an "
			"Oregon pesticide applicator licence, ORS 634 / OAR 603-057) is earned by attending "
			"every required session; EPA WPS 40 CFR 170.401/.501 and FSMA 21 CFR 112.21 training "
			"likewise count only when the whole of it was delivered to the person"
		),
		purpose=(
			"A course that runs over two days gives nothing to somebody who came to one. Until "
			"this, day two was a separate session nobody connected to day one, so the person who "
			"missed it simply had no record — and nothing said so until their licence lapsed."
		),
		kairotic_gate=(
			"Fires once a required day is closed (or is more than a day past) with the person "
			"registered and not attended. Stays until they attend a later session of the same "
			"Training Type or a record is filed for them. Due on the expiry of the certification "
			"the course renews; Critical inside thirty days of it."
		),
		regimes=("Internal",),
		scan=_scan_missed_training_days,
	)
)

shape(
	"training_day_missed",
	target_doctype="Training Session Attendee",
	builtin_scanner="training_day_missed",
	date_field="creation",
	threshold_critical_days=training_records.CRITICAL_WINDOW_DAYS,
	threshold_warning_days=training_records.EXPIRING_WINDOW_DAYS,
	severity_critical=SEVERITY_CRITICAL,
	severity_warning=SEVERITY_WARNING,
	severity_expired=SEVERITY_CRITICAL,
	category="Workforce",
	audit_packet_types=["OSHA", "DOL"],
	retention_years=2,
	extra_parameters={"notify_roles": ["Farm Manager", "Foreman"]},
)


# ── 13. supervisor_review_lapsed ────────────────────────────────────────────
#
# THE THIRTEENTH RULE WATCHES A SIGNATURE THAT WAS NEVER PUT ON A RECORD, which
# is a different kind of absence from every rule above it. Rules 1-9 fire because
# nobody went and looked. Rules 10 and 11 fire because somebody looked and found
# something. Rule 12 fires because a qualification ran out. This one fires because
# the work was done, the record was written, and the second pair of eyes the rule
# asks for never arrived.
#
# FSMA 21 CFR 112.161(b) is one sentence and it is the most commonly cited finding
# against farms whose actual practice is fine: records required under Subpart C
# and others must be "reviewed, dated, and signed, within a reasonable time after
# the records are made, by a supervisor or responsible party." USDA GAP does not
# ask for it. So an operation with an immaculate GAP binder — every training
# delivered, every signature from every trainee, every certificate current —
# fails on the one element its own auditor never mentioned, and finds out from an
# FDA-contracted inspector.
#
# WHY IT IS BUILT AS A TABLE RATHER THAN AS A TRAINING RULE. §112.161(b) is not
# about training. It reaches every activity record the rule covers, and this app
# already ships several that will carry the same two columns: Housing Inspection,
# Water Test, Detector Test, Heat Exposure Event, Farm Task Assignment. In
# v0.19.3 only Employee Training Record has them, so the table has one row — and
# the rule is written to walk a table anyway, because the alternative is
# discovering in v0.19.3.1 that the training-shaped version has to be rewritten
# to add the second doctype. A doctype whose columns are absent is skipped by
# name rather than crashing the sweep, which is what makes adding the next row
# a one-line change on a live site.
#
# WHY THE CLOCK RUNS FROM `creation` AND NOT FROM THE ACTIVITY DATE. The rule's
# own words are "after the records are made". A training delivered last March and
# recorded yesterday has a record that is one day old, and the supervisor has not
# been late reviewing it — the operation was late WRITING it, which is a
# §112.161(a)(2) problem and a different finding. Reading the activity date here
# would raise a Critical review alert the moment anybody backfilled a season, and
# a calendar full of alerts nobody can act on is a calendar nobody reads.


@dataclass(frozen=True)
class ReviewTarget:
	"""One doctype carrying the §112.161(b) universal review columns.

	`what` is the phrase the alert uses for the record — "worker training record"
	rather than "Employee Training Record" — because the message is read on a
	phone by somebody who has to decide whether to walk over and sign something,
	and a doctype name is not what they call it.

	`activity_field` is NOT what the clock runs on; see the note above. It is
	carried so the message can say what the record was about, which is the
	difference between "a record from 3 April" and "a record".
	"""

	doctype: str
	what: str
	subject_field: str
	activity_field: str
	reviewed_by_field: str = "supervisor_reviewed_by"
	reviewed_on_field: str = "supervisor_reviewed_on"
	#: Extra columns the message wants. Kept per target because a training record
	#: is described by its curriculum and a water test by its source.
	detail_fields: tuple = ()
	#: What the message says this record is evidence FOR, in one clause.
	stakes: str = ""


#: v0.19.3 ships one. The four commented candidates are the doctypes this app
#: already has that §112.161 reaches, and each is a one-row addition the day it
#: grows the two columns — which is the whole reason this is a table.
REVIEW_TARGETS = (
	ReviewTarget(
		doctype=training_records.DOCTYPE,
		what="worker training record",
		subject_field="employee_name",
		activity_field="completed_date",
		detail_fields=("training_type", "employee", "regimes"),
		stakes=(
			"Worker training records are named in §112.161(b) explicitly, and this is the "
			"element a GAP-only operation most often lacks — FDA writes it up even where "
			"the training itself was fine"
		),
	),
	# Candidates, each waiting only on the two columns:
	#   Housing Inspection  — §112.161 does not reach it, but OAR 437-004-1120 asks
	#                         the same question of a habitability walk
	#   Water Test          — §112.161(b) reaches Subpart E records directly
	#   Heat Exposure Event — carries its own signature today; the review is the
	#                         second pair of eyes on top of the foreman's
	#   Farm Task Assignment — the completion record for everything dispatched
)


def _review_lag(target: ReviewTarget, row: dict, today: str) -> int | None:
	"""Days since the record was MADE, or None where it has already been reviewed.

	A record with a review date and no reviewer counts as unreviewed here, which
	is the same reading `EmployeeTrainingRecord._check_the_review` refuses on
	save: a review date with nobody attached is what an auditor is trained to
	disbelieve, and treating it as satisfied would hide exactly that record.
	"""
	if (
		str(row.get(target.reviewed_by_field) or "").strip()
		and str(row.get(target.reviewed_on_field) or "").strip()
	):
		return None
	return days_since(today, str(row.get("creation") or "")[:10])


def _scan_supervisor_reviews(context: dict) -> list:
	"""Activity records old enough that nobody has reviewed them in time.

	KAIROTIC BECAUSE THE GATE IS THE AGE OF AN UNSIGNED RECORD, not a date. A
	record written this morning raises nothing however many times the sweep runs;
	the same record raises Warning a fortnight later and Critical after a month;
	and it goes away by itself the moment somebody signs it, because a reviewed
	record is not observed and an unobserved alert is auto-dismissed. Nobody has
	to remember to clear it, which is the property that makes the calendar
	readable at all.

	FOURTEEN DAYS IS THE FLOOR AND IT IS A LEAD TIME LIKE EVERY OTHER NUMBER IN
	THIS FILE. The rule's phrase is "within a reasonable time", and the practice
	it is read against for records generated daily is weekly review. A fortnight
	is one clear miss of that cadence — long enough that a supervisor who reviews
	on Fridays has had two Fridays, short enough that the record is still fresh
	enough for somebody to remember the afternoon they are signing off.
	"""
	today = context["today"]
	warning_days = _interval(context, "threshold_warning_days", REVIEW_WARNING_DAYS)
	critical_days = _interval(context, "threshold_critical_days", REVIEW_CRITICAL_DAYS)
	critical_severity = _severity_of(context, "severity_critical", SEVERITY_CRITICAL)
	warning_severity = _severity_of(context, "severity_warning", SEVERITY_WARNING)
	out = []
	for target in REVIEW_TARGETS:
		if not compat.doctype_exists(target.doctype):
			continue
		if not compat.has_field(target.doctype, target.reviewed_by_field):
			# The doctype is here and the columns are not, which on a site
			# mid-upgrade is the ordinary state for an hour. Skipped rather than
			# reported as unreviewed: every record would qualify, the rule would
			# hit its cap, and the calendar would fill with one migration.
			continue
		fields = (
			"name",
			"creation",
			"company",
			target.subject_field,
			target.activity_field,
			target.reviewed_by_field,
			target.reviewed_on_field,
			*target.detail_fields,
		)
		for row in _rows(target.doctype, _company_filter({}, context.get("company") or ""), fields):
			age = _review_lag(target, row, today)
			if age is None or age < warning_days:
				continue
			if not _scoped(context, row):
				continue

			who = row.get(target.subject_field) or row.get("name")
			what = row.get("training_type") or target.what
			activity = str(row.get(target.activity_field) or "") or None
			# Copied off the record where the record carries tags, so a review
			# alert lands in the same packet as the record it is about. An
			# untagged record produces an untagged alert, for the reason
			# `training_expiring` gives: inventing a tag would make the calendar
			# claim coverage the register does not have.
			regimes = training_records.parse(row.get("regimes")) if "regimes" in row else None

			if age > critical_days:
				severity = critical_severity
				message = (
					f"The {target.what} for {who} ({what}"
					+ (f", {activity}" if activity else "")
					+ f") has been on file {age} days with no supervisor review. FSMA "
					"§112.161(b) requires it to be reviewed, dated and signed by a supervisor "
					"or responsible party WITHIN A REASONABLE TIME after the record is made, "
					f"and past {critical_days} days there is no longer a reading of "
					"'reasonable' that covers it — a signature added now is dated now, and a "
					"batch of them signed the week before an inspection is the finding rather "
					f"than the fix. {target.stakes}."
				)
			else:
				severity = warning_severity
				message = (
					f"The {target.what} for {who} ({what}"
					+ (f", {activity}" if activity else "")
					+ f") has been on file {age} days with no supervisor review. FSMA "
					"§112.161(b) asks for a review, dated and signed, within a reasonable time "
					"after the record is made — weekly is the cadence it is read against for "
					"records generated daily, so this has missed two. "
					"sign_training_supervisor_review closes it in one call."
				)

			out.append(
				Observation(
					source_doctype=target.doctype,
					source_docname=row["name"],
					message=message,
					severity=severity,
					# The date it BECAME overdue, not the date it was written. A
					# calendar sorted on due date should put the record that went
					# past its review window first, and that is fourteen days
					# after it was made.
					due_date=str(frappe.utils.add_days(str(row.get("creation") or "")[:10], warning_days)),
					company=str(row.get("company") or ""),
					category="Records",
					regimes=regimes,
				)
			)
	return out


register(
	Rule(
		key="supervisor_review_lapsed",
		title="An activity record has been on file for a fortnight with nobody's review on it",
		category="Records",
		requires=(training_records.DOCTYPE,),
		framework=(
			"FDA FSMA 21 CFR 112.161(b) — records required under this part must be reviewed, "
			"dated and signed, within a reasonable time after the records are made, by a "
			"supervisor or responsible party; 21 CFR 112.161(a)(2) on records created at the "
			"time of the activity"
		),
		purpose=(
			"The single most common FSMA finding against farms whose actual practice is "
			"sound. USDA GAP does not ask for a supervisor's review, so an operation with an "
			"immaculate GAP binder has every training delivered, every trainee signature, "
			"every certificate current — and no second pair of eyes on any of it. Nothing on "
			"this site could see that gap: the record exists, it is complete by GAP's "
			"reckoning, and only the empty review column says otherwise."
		),
		kairotic_gate=(
			"Fires on the AGE OF AN UNSIGNED RECORD, counted from when the record was MADE "
			"rather than from when the activity happened — §112.161(b)'s own clock is 'after "
			"the records are made', and reading the activity date would raise a Critical on "
			"every record of a season somebody backfilled. A record written this morning "
			"raises nothing however often the sweep runs; at fourteen days it raises Warning, "
			"past thirty Critical, and it disappears by itself the moment somebody signs it. "
			"Fourteen days is one clear miss of the weekly review cadence the phrase "
			"'reasonable time' is read against for records generated daily — two Fridays gone, "
			"and the afternoon still recent enough for the supervisor to remember what they "
			"are signing off."
		),
		# §112.161 is FSMA and only FSMA. The other regimes ask for the record and
		# none of them asks for this signature, which is exactly why the gap
		# exists — so tagging it more widely would put it in packets whose
		# auditors never raise it and dilute the one that does. When the table
		# grows a target under OAR 437-004-1120 or Subpart E, the observation
		# carries the record's own tags and this constant becomes the union.
		regimes=("FSMA",),
		scan=_scan_supervisor_reviews,
	)
)

# BUILT-IN, AND THERE ARE THREE REASONS RATHER THAN ONE.
#
#   1. IT WALKS A TABLE OF DOCTYPES, not one. `REVIEW_TARGETS` is the list of
#      records carrying the §112.161(b) review columns, and it is written to grow
#      — Housing Inspection, Water Test, Heat Exposure Event and Farm Task
#      Assignment are all one row away. `target_doctype` is singular by design.
#   2. THE CONDITION IS AN `OR` OF TWO NULLS. A record counts as unreviewed when
#      the reviewer is missing OR the review date is missing — a date with
#      nobody attached is what an auditor is trained to disbelieve. Scope filters
#      are ANDed, deliberately, because an OR-of-filters is a query language and
#      a query language in a text field is the thing `custom_python` already is.
#   3. THE CLOCK RUNS ON `creation`, NOT ON THE ACTIVITY DATE. §112.161(b)'s own
#      words are "after the records are made", and reading the activity date
#      would raise a Critical on every record of a season somebody backfilled.
#      `creation` is a framework column rather than a doctype field, so it is
#      outside what `date_field` can name today.
#
# NOTE THE INVERTED SENSE OF THE THRESHOLDS ON THIS ROW. Everywhere else they are
# days REMAINING; here they are days ELAPSED since the record was made, because
# the thing being measured is an absence getting older rather than a deadline
# approaching. That is the strongest single argument for this rule staying
# built-in: a number on a record that means the opposite of what the same number
# means on the other twelve is a number somebody will eventually misread.
shape(
	"supervisor_review_lapsed",
	target_doctype=training_records.DOCTYPE,
	builtin_scanner="supervisor_review_lapsed",
	date_field="creation",
	threshold_warning_days=REVIEW_WARNING_DAYS,
	threshold_critical_days=REVIEW_CRITICAL_DAYS,
	severity_critical=SEVERITY_CRITICAL,
	severity_warning=SEVERITY_WARNING,
	severity_expired=SEVERITY_WARNING,
	category="Records",
	audit_packet_types=["FSMA"],
	retention_years=2,
)


# ── 21. financial_kpi_threshold_breach ──────────────────────────────────────
#
# v0.39.0, AND IT IS THE FIRST RULE IN THIS FILE THAT IS ABOUT MONEY RATHER THAN
# ABOUT A REGULATOR. It is here, on the same calendar as the expiring certificate
# and the untested water, rather than on a finance board of its own — and that is
# the whole argument for the way v0.39.0 raises threshold alerts. An operation
# with two alerting systems reads neither. A covenant about to be breached is
# exactly as much a Monday-morning problem as a cabin with a dead carbon monoxide
# detector, and it wants the same dismissal, the same snooze, and the same
# auto-clear the moment the condition stops being true.
#
# IT READS THE CACHE AND NEVER COMPUTES. The alert sweep runs hourly beside
# somebody's real work; a scan that recomputed every KPI for every company would
# put minutes of GL arithmetic on that path, every hour, for a figure that moves
# once a month. `refresh_all_kpi_caches` fills the cache overnight and this reads
# the newest snapshot it finds — which is at most a day old and is the SAME
# figure the dashboard is showing, so an alert and a dashboard can never disagree
# about the number.
#
# A KPI WITH NO CACHED VALUE RAISES NOTHING AND DISMISSES NOTHING, which is the
# reading every rule in this file gives to an absent record. A definition created
# this morning has no history until the overnight job reaches it, and treating
# that silence as a pass would be as wrong as treating it as a breach.
KPI_SOURCE_DOCTYPE = "Financial KPI Definition"


def _scan_kpi_thresholds(context: dict) -> list:
	"""Enabled KPI definitions whose newest cached value is past a threshold.

	THE GATE IS THE VALUE, NOT A DATE, and that makes this the most purely
	kairotic rule in the file: nothing here counts days. A KPI inside its
	thresholds raises nothing however many times the sweep runs; the same KPI
	raises Warning the month it crosses the warning line and Critical when it
	crosses the critical one, and the alert clears by itself when the next
	month's figure comes back inside. Nobody closes it, because there was never
	a task — there was a number.

	ONE ALERT PER (DEFINITION, COMPANY), keyed on the definition docname with the
	company in the message. A KPI that applies to every company is genuinely
	several conditions, and merging them would mean Highland's breach dismissing
	Constancy's.
	"""
	try:
		from ..services import kpi_engine
	except Exception:  # pragma: no cover - a bench mid-upgrade
		return []
	if not kpi_engine.available():
		return []

	critical_severity = _severity_of(context, "severity_critical", SEVERITY_CRITICAL)
	warning_severity = _severity_of(context, "severity_warning", SEVERITY_WARNING)
	scoped_company = str(context.get("company") or "")

	try:
		companies = (
			[scoped_company]
			if scoped_company
			else (frappe.db.get_all("Company", pluck="name", limit=200) or [])
		)
	except Exception:  # pragma: no cover
		return []

	out = []
	for row in kpi_engine.enabled_rows():
		if not kpi_engine.thresholds_of(row)["has_any"]:
			# Nothing is watching this KPI, which is a legitimate state and not a
			# silent pass — compute_all_kpis reports it in `unwatched_note`. It is
			# simply not this rule's business.
			continue
		if not _scoped(context, row):
			continue
		for company in companies:
			company = str(company)
			if row.get("company") and row["company"] != company:
				continue
			cached = _newest_cached_kpi(kpi_engine, row, company)
			if not cached or cached.get("value") is None:
				continue
			verdict = kpi_engine.threshold_status(row, cached["value"])
			if not verdict["breached"]:
				continue

			severity = critical_severity if verdict["status"] == "Critical" else warning_severity
			direction = "below" if verdict["direction"] == "low" else "above"
			out.append(
				Observation(
					source_doctype=KPI_SOURCE_DOCTYPE,
					source_docname=str(row["name"]),
					message=(
						f"{row.get('title')} at {company} is {cached['value']}"
						# The unit rides in parentheses rather than as a suffix,
						# because it is a category name and not a symbol: "50.0
						# Currency" reads as a typo where "50.0 (Currency)" reads
						# as what it is. And it belongs in the sentence at all
						# because 0.42 is a catastrophe as a ratio, a fine margin
						# as a percentage and a rounding error as dollars.
						+ (f" ({row.get('unit')})" if row.get("unit") else "")
						+ f", {direction} the {verdict['status'].lower()} threshold of "
						f"{verdict['threshold']}, as of {cached['as_of']}. "
						"THIS IS A CACHED FIGURE, not a fresh computation — it is the same number "
						"the dashboard is showing, and compute_kpi recomputes it live with its "
						"components if the figure itself is what is in question. The alert clears "
						"by itself when the value comes back inside the line; there is nothing to "
						"close, because there was never a task, there was a number."
					),
					severity=severity,
					# NO DUE DATE, and that is not an omission. Every other rule in
					# this file has a date by which something must be done; a KPI
					# past its threshold has no such date, because the remedy is a
					# season of trading rather than a form. A fabricated due date
					# would sort this above genuine deadlines on the calendar.
					due_date="",
					company=company,
					category="Finance",
					# A covenant answers to a lender rather than to an agency, and
					# `Internal` is this file's honest third answer for real work
					# with a real consequence and nobody coming to inspect it.
					regimes=["Internal"],
				)
			)
	return out


def _newest_cached_kpi(kpi_engine, row: dict, company: str) -> dict:
	"""The most recent cached snapshot for one definition on one company, or {}.

	Read against the definition's OWN window type, length and step, because a KPI
	declared as a quarterly snapshot and one declared as a monthly TTM cache under
	different keys — and matching on kpi_key alone would compare a value against a
	threshold that was drawn for a different window.
	"""
	try:
		from ..services import windowed_reports as windows

		if not windows.cache_available():
			return {}
		options = kpi_engine.window_options(row)
		found = frappe.db.get_all(
			windows.DOCTYPE,
			filters={
				"kpi_key": str(row.get("kpi_id")),
				"company": company,
				"computation_step": options["computation_step"],
				"window_type": options["window_type"],
				"window_months": options["window_months"],
			},
			fields=compat.existing_fields(windows.DOCTYPE, ("name", "value", "as_of")),
			order_by="as_of desc",
			limit=1,
		)
		return dict(found[0]) if found else {}
	except Exception:  # pragma: no cover - a cache that cannot be read is a miss
		return {}


register(
	Rule(
		key="financial_kpi_threshold_breach",
		title="A financial KPI is past a threshold somebody drew",
		category="Finance",
		requires=(KPI_SOURCE_DOCTYPE, "Financial KPI History"),
		framework=(
			"No statute. Lender covenants, and this operation's own judgement about what its "
			"numbers may not do — recorded as thresholds on a Financial KPI Definition rather "
			"than remembered."
		),
		purpose=(
			"A covenant is breached weeks before anybody notices, because the number that "
			"breached it lives in a report somebody runs at month end and the covenant lives "
			"in a document nobody has opened since it was signed. This rule puts the two in "
			"the same place: the threshold is a field on the KPI, and crossing it lands on "
			"the same calendar as everything else that needs doing this week."
		),
		kairotic_gate=(
			"THE GATE IS THE VALUE AND NOT A DATE — nothing in this rule counts days, which "
			"makes it the purest ripeness test in the file. A KPI inside its thresholds "
			"raises nothing however often the sweep runs; the same KPI raises Warning the "
			"month its cached figure crosses the warning line and Critical when it crosses "
			"the critical one, and it AUTO-DISMISSES when the next month's figure comes back "
			"inside. Nobody closes it, because there was never a task — there was a number. "
			"It reads the CACHED figure and never computes: the sweep runs hourly beside "
			"somebody's real work, the overnight refresh fills the cache, and reading it is "
			"what guarantees an alert and a dashboard can never disagree about the value. A "
			"KPI with no cached snapshot raises nothing AND DISMISSES NOTHING, which is the "
			"same reading an absent record gets everywhere else here: a definition created "
			"this morning is not evidence of anything yet."
		),
		regimes=("Internal",),
		scan=_scan_kpi_thresholds,
	)
)

# BUILT-IN, AND THERE IS ONE REASON THAT DECIDES IT: THE THRESHOLDS ARE NOT ON
# THIS ROW. Every other rule here is tuned by numbers on its own Compliance Rule
# record — days remaining, days elapsed, a cadence. This rule's thresholds live
# on each Financial KPI Definition, because they are per-KPI and per-unit: a
# ratio's warning line and a dollar figure's warning line are not values that can
# share a column. The record still owns everything an operator would change ABOUT
# THE RULE — the two severities, the scope filters, the switch — and the numbers
# that decide a breach are edited where they belong, with
# update_financial_kpi_definition.
#
# THE SECOND REASON IS THAT THE COMPARISON IS AGAINST A DERIVED VALUE. A
# declarative scan filters and compares fields on a target row; this reads the
# newest cached snapshot for a (KPI, company, window) tuple and compares it
# against four nullable bounds whose meaning depends on which of them are set.
# That is arithmetic, not a filter.
shape(
	"financial_kpi_threshold_breach",
	target_doctype=KPI_SOURCE_DOCTYPE,
	builtin_scanner="financial_kpi_threshold_breach",
	requires_doctypes=f"{KPI_SOURCE_DOCTYPE}, Financial KPI History",
	date_field="",
	threshold_critical_days=-1,
	threshold_warning_days=-1,
	severity_critical=SEVERITY_CRITICAL,
	severity_warning=SEVERITY_WARNING,
	severity_expired=SEVERITY_WARNING,
	due_date_mode="None",
	category="Finance",
	retention_years=7,
)

# ── 22. budget_variance_breach ───────────────────────────────────────────────
#
# v0.42.0, AND IT IS THE SAME MOVE AS RULE 21 POINTED AT A DIFFERENT CACHE. A
# Budget's `refresh_budget` tool writes actual and variance figures onto the
# budget's own line items and KPI targets — see `budget_engine.py` for the
# arithmetic — and this rule reads what was written, exactly as
# `financial_kpi_threshold_breach` reads the KPI history cache rather than
# recomputing. ONE RULE ENGINE DECIDES WHAT REACHES THE COMPLIANCE CALENDAR,
# so a budget that is off-plan gets the same dismissal, snooze and auto-clear
# machinery a lapsing certificate does, instead of a second alerting path that
# none of that applies to.
#
# ONE ALERT PER BUDGET, not per breaching line — unlike every rule above this
# one, which raise one alert per row they scan. A Budget commonly breaches on
# several accounts and KPI targets at once, and `alert_key` is built from the
# RULE and the SOURCE RECORD alone; multiple observations naming the same
# record would collide on one key and the sweep would keep only the last one
# written. So this rule aggregates every breach on one budget into ONE
# Observation, worst severity first, with every breaching line named in the
# message — the same choice `preview_payroll_gl` makes about reporting every
# blocker at once rather than the first.
BUDGET_SOURCE_DOCTYPE = "Budget"
BUDGET_REQUIRES = (BUDGET_SOURCE_DOCTYPE, "Budget Line Item", "Budget KPI Target")


def _budget_result_from_rows(line_items: list, kpi_targets: list) -> dict:
	"""One budget's STORED computed fields, in the shape `budget_engine` reads.

	Never computes — every figure here was written by `refresh_budget` or by
	the overnight sweep, and reading it again is what guarantees this rule and
	`get_budget_variance_report` can never disagree about a breach.
	"""
	return {
		"line_items": [
			{
				"account": row.get("account"),
				"budgeted_amount": row.get("budgeted_amount"),
				"actual_amount": row.get("actual_amount"),
				"variance_amount": row.get("variance_amount"),
				"variance_pct": row.get("variance_pct"),
				"threshold_pct": row.get("threshold_pct"),
			}
			for row in line_items or []
		],
		"kpi_targets": [
			{
				"kpi_definition": row.get("kpi_definition"),
				"target_value": row.get("target_value"),
				"actual_value": row.get("actual_value"),
				"variance_pct": row.get("variance_pct"),
				"threshold_pct": row.get("threshold_pct"),
			}
			for row in kpi_targets or []
		],
	}


def _scan_budget_variances(context: dict) -> list:
	"""Every ACTIVE budget whose stored figures show at least one breach.

	A Draft budget is still being built and a Closed one is finished; neither
	is refreshed by the overnight sweep, so scanning them would either raise
	on numbers nobody has filled in yet or keep re-raising on a budget that is
	done and is not going to be corrected. `refresh_all_active_budgets` and
	this rule agree on exactly one status: Active.
	"""
	try:
		from .. import budget_engine
	except Exception:  # pragma: no cover - a bench mid-upgrade
		return []
	if not all(compat.doctype_exists(doctype) for doctype in BUDGET_REQUIRES):
		return []

	scoped_company = str(context.get("company") or "")
	filters = {"status": "Active"}
	if scoped_company:
		filters["company"] = scoped_company

	try:
		budgets = frappe.db.get_all(
			BUDGET_SOURCE_DOCTYPE,
			filters=filters,
			fields=["name", "budget_name", "company", "fiscal_year"],
			limit=500,
		)
	except Exception:  # pragma: no cover
		return []

	out = []
	for row in budgets:
		if not _scoped(context, row):
			continue
		line_items = _rows(
			"Budget Line Item",
			{"parent": row["name"], "parenttype": BUDGET_SOURCE_DOCTYPE},
			(
				"account",
				"budgeted_amount",
				"actual_amount",
				"variance_amount",
				"variance_pct",
				"threshold_pct",
			),
			limit=500,
		)
		kpi_targets = _rows(
			"Budget KPI Target",
			{"parent": row["name"], "parenttype": BUDGET_SOURCE_DOCTYPE},
			("kpi_definition", "target_value", "actual_value", "variance_pct", "threshold_pct"),
			limit=500,
		)
		result = _budget_result_from_rows(line_items, kpi_targets)
		breaches = budget_engine.check_budget_variances(result)
		if not breaches:
			continue

		critical = [b for b in breaches if b["severity"] == SEVERITY_CRITICAL]
		severity = SEVERITY_CRITICAL if critical else SEVERITY_WARNING
		sentences = "; ".join(budget_engine.breach_message(b) for b in breaches)
		out.append(
			Observation(
				source_doctype=BUDGET_SOURCE_DOCTYPE,
				source_docname=str(row["name"]),
				message=(
					f"{row.get('budget_name')} ({row.get('company')}, FY {row.get('fiscal_year')}) "
					f"has {len(breaches)} variance breach(es) ({len(critical)} critical): "
					f"{sentences}. THESE ARE CACHED FIGURES from this budget's last refresh_budget "
					"call — get_budget_variance_report shows the same breakdown with every "
					"threshold and ratio; refresh_budget recomputes it live from the ledger and "
					"the KPI framework if the figures themselves are what is in question. The "
					"alert clears by itself when the next refresh comes back inside every "
					"threshold; there is nothing to close by hand."
				),
				severity=severity,
				# NO DUE DATE, for the same reason rule 21 has none: the remedy for an
				# off-plan budget is a decision about spending or a season of trading,
				# not a form with a deadline. A fabricated due date would sort this
				# above genuine deadlines on the calendar.
				due_date="",
				company=row.get("company") or "",
				category="Finance",
				regimes=["Internal"],
			)
		)
	return out


register(
	Rule(
		key="budget_variance_breach",
		title="A budget line or KPI target is past its variance threshold",
		category="Finance",
		requires=BUDGET_REQUIRES,
		framework=(
			"No statute. This operation's own budget and the tolerance it set for drift from it — "
			"recorded as threshold_pct on each Budget Line Item and Budget KPI Target rather than "
			"remembered."
		),
		purpose=(
			"A budget that has quietly drifted 40% over on fertilizer or fallen well short of a "
			"lender's covenant is exactly as much a Monday-morning problem as an expiring "
			"certificate, and until this rule it lived only in whatever spreadsheet built the "
			"budget. This rule puts it on the same calendar, with the same dismissal, snooze and "
			"auto-clear."
		),
		kairotic_gate=(
			"THE GATE IS THE VARIANCE, NOT A DATE — nothing here counts days. A budget inside "
			"every line's threshold raises nothing however often the sweep runs; the same budget "
			"raises Warning the refresh its variance first crosses a threshold and Critical past "
			"twice that threshold, and it AUTO-DISMISSES the refresh a line comes back inside. It "
			"reads the STORED figures refresh_budget wrote and never computes: the sweep runs "
			"hourly beside somebody's real work, refresh_budget (or the overnight sweep, for every "
			"Active budget) fills the fields, and reading them is what guarantees this alert and "
			"get_budget_variance_report can never disagree. A budget that has never been refreshed "
			"raises nothing AND DISMISSES NOTHING — an unrefreshed budget is not evidence that "
			"nothing is wrong, the same reading an absent record gets everywhere else here. Only "
			"ACTIVE budgets are scanned; a Draft budget is still being built and a Closed one is "
			"finished."
		),
		regimes=("Internal",),
		scan=_scan_budget_variances,
	)
)

# BUILT-IN, FOR THE SAME REASON RULE 21 IS: THE THRESHOLDS ARE NOT ON THIS ROW.
# Every other rule here is tuned by numbers on its own Compliance Rule record.
# This rule's thresholds live on each Budget Line Item and Budget KPI Target,
# because they are per-line — a fertilizer line watched to 5% and a discretionary
# line watched to 25% are not values one column on a Compliance Rule could hold.
# The record still owns everything an operator would change ABOUT THE RULE — the
# two severities, the scope filters, the switch — and the per-line numbers that
# decide a breach are edited where they belong, with update_budget.
#
# THE SECOND REASON IS THAT THE COMPARISON IS AGAINST DERIVED, MULTI-ROW STATE. A
# declarative scan filters and compares fields on a target row; this reads every
# child row on a budget and runs `budget_engine.check_budget_variances` over all
# of them at once. That is arithmetic across a table, not a filter on one row.
shape(
	"budget_variance_breach",
	target_doctype=BUDGET_SOURCE_DOCTYPE,
	builtin_scanner="budget_variance_breach",
	requires_doctypes=", ".join(BUDGET_REQUIRES),
	date_field="",
	threshold_critical_days=-1,
	threshold_warning_days=-1,
	severity_critical=SEVERITY_CRITICAL,
	severity_warning=SEVERITY_WARNING,
	severity_expired=SEVERITY_WARNING,
	due_date_mode="None",
	category="Finance",
	retention_years=7,
)


# ── 23. i9_supplement_b_unsigned ────────────────────────────────────────────
#
# THE THIRD OF THE MISSING-SIGNATURE RULES, AND THE ONLY ONE THAT IS NOT
# DECLARATIVE. `i9_section_1_unsigned` and `i9_section_2_unsigned` are records
# and nothing else — see `compliance_rules.declarative_seed_specs` — because
# each asks one question of one column on one row, which is exactly what the
# declarative engine is for. This one cannot be, and the reason is worth stating
# because it is the test for whether a future rule belongs here or there.
#
# SUPPLEMENT B LIVES IN A CHILD TABLE. An I-9 Form carries a `reverifications`
# table, one row per reverification or rehire, and each row has its own employer
# attestation — `section_3_signature`. The question "does this form have an
# unsigned reverification" is therefore a question about a SET of rows, and the
# answer has to fold that set down: which of them are unsigned, how many, and
# which is the most recent. That is the same aggregation `audit_action_overdue`
# does over corrective actions, and the same reason it is built in.
#
# THE NEAREST DECLARATIVE PRIMITIVE IS `latest_child_field_threshold`, AND IT
# DOES NOT REACH. It folds a child table to its newest row and then compares a
# NUMBER on it — `passes_threshold` takes two floats and answers False on
# anything else, deliberately, so a thermometer column somebody filled in as
# "warm" cannot raise a heat alert. "This Attach field is empty" is not a
# number, and widening that primitive to take presence operators would put a
# non-numeric comparison inside the one function written to refuse them.
#
# ONE ALERT PER FORM, NOT PER ROW. A worker reverified twice, neither signed, is
# one conversation with one authorized signer holding one form — and two rows on
# the calendar would read as two people to chase. The message names the count
# and the most recent date so the person who picks it up knows what they are
# walking into.
#: The child doctype Supplement B rows are, and the table on I-9 Form they hang
#: off. Named rather than inlined because the scanner reads both and the seeded
#: rule's `requires_doctypes` has to agree with what the scanner will look for.
REVERIFICATION_DOCTYPE = "I-9 Reverification"
REVERIFICATION_TABLE = "reverifications"

#: I-9 statuses this rule will not raise on. A destroyed form is past its
#: retention period; sending somebody to sign it would be asking them to attest
#: to documents that no longer exist.
REVERIFICATION_SKIP_STATUSES = ("Destroyed",)


def _scan_unsigned_reverifications(context: dict) -> list:
	"""I-9 forms whose Supplement B rows carry no employer attestation.

	READS THE CHILD TABLE DIRECTLY rather than loading each parent document. A
	site with four hundred I-9s would be four hundred `get_doc` calls to find the
	handful with a reverification at all; one query over `I-9 Reverification`
	grouped in Python is one query. `audit_action_overdue` loads the parent
	because it needs a controller method (`open_actions`); there is no such
	method here and no reason to pay for one.

	A FORM WITH NO REVERIFICATION RAISES NOTHING, which is not the same as
	raising nothing because it is clean. Most I-9s never need a Supplement B —
	the employee is a citizen, or their authorization has not expired — and a
	rule that treated "no rows" as "unsigned rows" would raise on the whole
	register.
	"""
	if not compat.doctype_exists(REVERIFICATION_DOCTYPE):
		return []
	severity = _severity_of(context, "severity_expired", SEVERITY_CRITICAL)

	# The parents first, so the company scope, the Destroyed exclusion and any
	# operator scope filters are applied ONCE — and so a reverification row
	# orphaned by a deleted form is skipped rather than raising an alert whose
	# source_docname points at nothing.
	#
	# THE STATUS EXCLUSION IS DONE IN PYTHON, not in the filter, for the reason
	# `compliance_rules.row_matches` gives at length: `status != 'Destroyed'` in
	# SQL drops every row whose status column was never written, and a form
	# imported without one is not a destroyed form. Reading it here keeps the
	# unset ones in, which is the direction that fails safe.
	forms = {
		row["name"]: row
		for row in _rows(
			"I-9 Form",
			_company_filter({}, context.get("company") or ""),
			("name", "employee", "employee_name", "company", "status"),
		)
		if str(row.get("status") or "") not in REVERIFICATION_SKIP_STATUSES
	}
	if not forms:
		return []

	# NOT THROUGH `_rows`, and the exception is `parent`. That helper selects
	# only the columns `compat.existing_fields` finds on the doctype's own field
	# list, and `parent` / `parentfield` are not on ANY child doctype's field
	# list — they are columns every child table has by construction. Filtering
	# them out would hand back rows with no idea which form they belong to.
	unsigned: dict = {}
	children = (
		frappe.db.get_all(
			REVERIFICATION_DOCTYPE,
			filters={"parent": ("in", sorted(forms)), "parentfield": REVERIFICATION_TABLE},
			fields=[
				"name",
				"parent",
				"reverification_date",
				"reason",
				"section_3_signature",
				"verifier_name",
			],
			limit=2000,
		)
		or []
	)
	for child in (dict(row) for row in children):
		if str(child.get("section_3_signature") or "").strip():
			continue
		unsigned.setdefault(str(child["parent"]), []).append(child)

	out = []
	for parent, rows in unsigned.items():
		form = forms[parent]
		if not _scoped(context, form):
			continue
		# Newest first, and an undated row sorts LAST rather than first: a
		# reverification nobody dated is a worse record than one dated last March,
		# but it is not evidence that the most recent reverification was undated,
		# and the message quotes the date it leads with.
		rows.sort(key=lambda entry: str(entry.get("reverification_date") or ""), reverse=True)
		newest = rows[0]
		when = str(newest.get("reverification_date") or "").strip()
		person = str(form.get("employee_name") or form.get("employee") or parent)
		count = "1 reverification entry has" if len(rows) == 1 else f"{len(rows)} reverification entries have"
		out.append(
			Observation(
				source_doctype="I-9 Form",
				source_docname=parent,
				message=(
					f"{count} no employer signature on {person}'s I-9 ({parent}) — most recently "
					f"{when or 'an undated entry'}"
					+ (f", reason: {newest.get('reason')}" if newest.get("reason") else "")
					+ ". Supplement B carries its own attestation under penalty of perjury, "
					"separate from Section 2, and an unsigned one leaves the continued "
					"employment unverified. Collect the signature with collect_form_signature."
				),
				severity=severity,
				company=str(form.get("company") or ""),
				category="Workforce",
			)
		)
	return out


register(
	Rule(
		key="i9_supplement_b_unsigned",
		title="An I-9 reverification (Supplement B) carries no employer signature",
		category="Workforce",
		requires=("I-9 Form", "I-9 Reverification"),
		framework="8 CFR § 274a.2(b)(1)(vii) — reverification attestation, Form I-9 Supplement B",
		purpose=(
			"A reverification is the employer saying, again and under penalty of perjury, "
			"that this person may still lawfully work here. Unsigned, the operation has "
			"recorded the document it examined and produced no attestation that anybody "
			"examined it — which on an inspection is the same finding as never having "
			"reverified at all, on a worker who is still on the crew."
		),
		kairotic_gate=(
			"Fires on a BOX in a CHILD TABLE. Any Supplement B row on a non-Destroyed I-9 "
			"whose section_3_signature is empty raises this, whatever its date — a "
			"reverification does not become signed by ageing. ONE alert per form rather than "
			"per row: two unsigned entries on one worker are one conversation with one "
			"signer. A form with no reverification rows raises nothing, which is most of the "
			"register and is why 'no rows' is never read as 'unsigned rows'. Auto-dismisses "
			"the moment every entry on the form carries a signature."
		),
		regimes=("Internal",),
		scan=_scan_unsigned_reverifications,
	)
)

# BUILT-IN, AND THE `date_field` IS EMPTY ON PURPOSE. There is no one date on
# the parent this rule counts from — the dates it cares about are on the child
# rows, and it quotes the newest of them in its own message. An empty
# `date_field` with both thresholds at -1 is this app's existing way of saying
# "every matching row raises, at `severity_expired`", which is what `i9_expired`
# says and for the same reason: the condition is a state, not a countdown.
shape(
	"i9_supplement_b_unsigned",
	target_doctype="I-9 Form",
	builtin_scanner="i9_supplement_b_unsigned",
	requires_doctypes="I-9 Form, I-9 Reverification",
	date_field="",
	threshold_critical_days=-1,
	threshold_warning_days=-1,
	severity_expired=SEVERITY_CRITICAL,
	due_date_mode="None",
	category="Workforce",
	retention_years=3,
	extra_parameters={
		"producer_task_what": "Collect I-9 Supplement B signature",
		"producer_task_subject_field": "employee_name",
		"producer_assignee_resolver": "i9_authorized_signer",
		"signature_doctype": "I-9 Form",
		"signature_field": "reverifications.section_3_signature",
	},
)


# ── v0.69.0: an item below the level somebody set for it ────────────────────
#
# BUILT-IN, AND THE REASON IS A JOIN RATHER THAN AN AGGREGATION. `audit_action_overdue`
# is built-in because it folds a child table; this one is built-in because the
# number and the level live on two different doctypes with no column joining them.
# The balance is `Bin.actual_qty`, keyed on (item, warehouse). The level is a
# reorder rule that lives on `Item Reorder` on a modern site and on `Item.re_order_level`
# on an old one. The declarative engine walks the rows of ONE doctype and asks a
# question of each; there is no question you can ask of a Bin whose answer is on
# an Item's child table, and no primitive short of a general join gets there.
#
# IT READS THE RULES THROUGH `stock_inventory._reorder_rules` AND THE BALANCES
# THROUGH `_bin_rows` — the same two functions `list_reorder_alerts` uses, which
# is the whole reason this rule is thirty lines rather than a hundred. Where a
# reorder rule lives on a given ERPNext vintage is a decision v0.69.0 already
# made once; a second copy of it in the alert engine would be the one that goes
# stale, and it would go stale silently, on the sites running the vintage nobody
# tests against.
#
# THE ROW WITH NO BIN IS THE MOST URGENT ROW, and it is the one a naive scan
# misses. ERPNext writes a Bin the first time stock moves for an item in a
# warehouse — so an item that has never arrived has NO row at all, and "no bin"
# means zero rather than "no opinion". Scanning bins and joining outwards would
# silently skip exactly the shortages that are total. So this walks the RULES —
# the operation's own statement of what it intends to keep on hand — and looks
# the balance up beside each one. Same call `list_reorder_alerts` makes, and the
# two agree by construction rather than by review.
#
# WHAT IT KEYS THE ALERT ON. The ITEM, always, and never the Bin — even though a
# Bin exists for most of them and would carry the warehouse in its docname. An
# alert key must not move, and a bin's does: `alerts/base.py` keys on
# (rule, source_doctype, source_docname), so an item that is short today with no
# bin and short next week with one would raise a SECOND alert and dismiss the
# first, losing its `first_seen` and anybody's snooze. The warehouse is in the
# message, where it is read, rather than in the identity, where it would churn.


def _item_row(item_code: str, cache: dict) -> dict:
	if item_code not in cache:
		try:
			cache[item_code] = dict(
				frappe.db.get_value("Item", item_code, ["item_name", "stock_uom", "disabled"], as_dict=True)
				or {}
			)
		except Exception:  # pragma: no cover - a site without the stock module
			cache[item_code] = {}
	return cache[item_code]


def _scan_item_reorder(context: dict) -> list:
	"""Items whose balance has fallen below the level the operation set for them."""
	from ..tools import stock_inventory

	company = context.get("company") or ""
	severity = _severity_of(context, "severity_expired", SEVERITY_INFO)
	scoped_warehouses = set(stock_inventory._company_warehouses(company)) if company else set()

	items: dict = {}
	out = []
	for (item_code, warehouse), rule in sorted(stock_inventory._reorder_rules().items()):
		# A LEVEL OF ZERO IS NOT A LEVEL. ERPNext writes the row for the reorder
		# QUANTITY as readily as for the level, so a zero here is "nobody said",
		# and raising on it would put every item with a reorder row and an empty
		# shelf on the board — including the ones deliberately kept at nothing.
		level = float(rule.get("reorder_level") or 0)
		if level <= 0 or not item_code:
			continue
		if company and warehouse and scoped_warehouses and warehouse not in scoped_warehouses:
			continue

		item = _item_row(item_code, items)
		# A disabled item is one nobody is allowed to buy or consume, so an alert
		# to reorder it is noise at best and a cancelled purchase order at worst.
		# Same exclusion `list_reorder_alerts` makes, for the same reason.
		if item.get("disabled"):
			continue

		bin_filters: dict = {"item_code": item_code}
		if warehouse:
			bin_filters["warehouse"] = warehouse
		elif company and scoped_warehouses:
			bin_filters["warehouse"] = ("in", sorted(scoped_warehouses))
		bins = stock_inventory._bin_rows(bin_filters)
		current = round(sum(float(row.get("actual_qty") or 0) for row in bins), 6)
		if current >= level:
			continue

		uom = str(item.get("stock_uom") or "")
		subject = {
			"name": item_code,
			"item_code": item_code,
			"item_name": item.get("item_name"),
			"warehouse": warehouse,
			"actual_qty": current,
			"reorder_level": level,
			"company": company,
		}
		if not _scoped(context, subject):
			continue

		where = f" in {warehouse}" if warehouse else ""
		out.append(
			Observation(
				source_doctype="Item",
				source_docname=item_code,
				message=(
					f"Stock below reorder level: {_number(current)} / {_number(level)}"
					f"{(' ' + uom) if uom else ''} — {item_code}{where}. "
					+ (
						"There is no bin for it at all, so the balance is zero rather than low. "
						if not bins
						else ""
					)
					+ "Raise the order now: the interval between ordering and having it on the "
					"shelf is what the reorder level was set to cover, and the day it is needed "
					"is not the day to find out it is short."
				),
				severity=severity,
				company=company,
			)
		)
	return out


def _number(value: float) -> str:
	"""A quantity without a trailing `.0` on the whole numbers most of them are."""
	return str(int(value)) if float(value) == int(value) else str(round(float(value), 3))


register(
	Rule(
		key="item_below_reorder",
		title="An item's stock has fallen below its reorder level",
		category="Inventory",
		requires=("Item", "Bin"),
		framework="Internal — inventory continuity behind the spray, irrigation and harvest calendars",
		purpose=(
			"A reorder level is the operation's own statement of how much lead time it needs "
			"for one item. Crossing it is not a shortage yet, which is precisely why it is "
			"worth an alert: the alert is the lead time. An orchard that finds out it is short "
			"of a product on the morning of the application has already lost the window, and a "
			"spray window missed is a pest cycle, not an errand."
		),
		kairotic_gate=(
			"Fires on a BALANCE against a LEVEL, and only where somebody set a level — an item "
			"with no reorder row raises nothing, for ever, because nobody has said what "
			"'enough' means for it. An item WITH a level and no bin in that warehouse raises at "
			"a balance of zero rather than being skipped, since a bin that does not exist is "
			"the most complete shortage there is. Silences by itself the moment the balance "
			"comes back up to the level: receiving the order IS the fix, and nothing else has "
			"to happen for the alert to go."
		),
		regimes=("Internal",),
		scan=_scan_item_reorder,
	)
)

# BUILT-IN, `date_field` EMPTY, BOTH THRESHOLDS AT -1 — this app's existing way
# of saying "every matching row raises, at `severity_expired`". A stock balance
# has no deadline to be near or past; it is above the level or it is below it.
#
# INFO RATHER THAN WARNING, and that is a deliberate down-grade from what the
# spec asked for. Compliance Alert has three severities and this board is shared
# with a live restricted-entry interval and an unsigned I-9. A reorder point is
# real and it is NOT of that kind: nobody is unsafe, nothing is unlawful, and a
# week of notice is the whole point. Filed at the level that means "know about
# this", so the levels above it keep meaning what they mean.
shape(
	"item_below_reorder",
	target_doctype="Item",
	builtin_scanner="item_below_reorder",
	requires_doctypes="Item, Item Reorder, Bin",
	date_field="",
	threshold_critical_days=-1,
	threshold_warning_days=-1,
	severity_critical=SEVERITY_WARNING,
	severity_warning=SEVERITY_INFO,
	severity_expired=SEVERITY_INFO,
	default_severity=SEVERITY_INFO,
	due_date_mode="None",
	category="Inventory",
	retention_years=1,
	# THE ONE RULE OF THE THREE THIS RELEASE ADDS THAT HAS WORK BEHIND IT. An REI
	# and a PHI are clocks nobody can shorten; a short shelf is somebody going to
	# buy something, so it gets a task recipe and `rectify_alert` can raise it.
	producer_farm_task_type="Other",
	producer_skill_required="",
	extra_parameters={
		"producer_task_what": "Reorder this item — the balance is below the level set for it",
		"producer_task_minutes": 15,
	},
	evidence_contract={"findings_text": True},
)


# ── 24. minor_hours_approaching ─────────────────────────────────────────────
#
# v0.98.0. THE ONE RULE HERE WHOSE SUBJECT IS A PERSON'S AGE, and the reason it
# is a rule rather than a message on `add_worker_to_shift` is that the check at
# the roster call happens when somebody is being ADDED. A fifteen-year-old who
# was rostered on Monday and has been working since is nobody's roster call on
# Thursday afternoon — and Thursday afternoon is when the fortieth hour arrives.
#
# COUNTED OFF THE SHIFTS AND NOT OFF ATTENDANCE, for the reason
# `shifts.hours_worked_by` gives at length: Attendance is written when a shift
# CLOSES, so the register says nothing about the day that is still running,
# which is the only day this rule can do anything about.
#
# THE SEVERITY LADDER IS THE LIMIT ITSELF. Over the weekly ceiling is Critical —
# the hours have already been worked and the employer is already answerable for
# them. Inside `minors.WEEKLY_WARNING_HOURS` of it is a Warning, because there is
# still an afternoon in which somebody can be sent home.
def _scan_minor_hours(context: dict) -> list:
	if not _employee_field("date_of_birth"):
		return []
	today = context["today"]
	filters = _company_filter({}, context.get("company") or "")
	if compat.has_field("Employee", "status"):
		filters["status"] = "Active"
	out = []
	for row in _rows(
		"Employee",
		filters,
		("name", "employee_name", "company", "date_of_birth", "status"),
	):
		described = minors.describe(row.get("date_of_birth"), today)
		if not described["is_minor"]:
			continue
		band = described["minor_band"]
		worked = shift_records.hours_worked_by(row["name"], today)
		if not worked["shifts"]:
			continue
		limits = described["minor_limits"] or {}
		weekly = float(limits.get("weekly_hours") or 0)
		if not weekly:
			continue
		remaining = weekly - worked["week"]
		if remaining > minors.WEEKLY_WARNING_HOURS:
			continue

		who = row.get("employee_name") or row["name"]
		if remaining < 0:
			severity = SEVERITY_CRITICAL
			message = (
				f"{who} is {described['age']} and has worked {worked['week']:.1f} hours in the "
				f"week beginning {worked.get('week_start')} — {abs(remaining):.1f} hour(s) OVER "
				f"the {weekly:.0f}-hour ceiling for the {band} band ({limits.get('citation')}). "
				"The hours have been worked; what is left is to stop them growing and to be able "
				"to say when somebody asks."
			)
		else:
			severity = SEVERITY_WARNING
			message = (
				f"{who} is {described['age']} and has worked {worked['week']:.1f} of the "
				f"{weekly:.0f} hours a {band} worker may work in the week beginning "
				f"{worked.get('week_start')} — {remaining:.1f} left ({limits.get('citation')}). "
				"This is the last part of the week in which sending them home is still a "
				"decision rather than a finding."
			)
		out.append(
			Observation(
				source_doctype="Employee",
				source_docname=row["name"],
				message=message,
				severity=severity,
				due_date=today,
				company=str(row.get("company") or ""),
				category="Workforce",
			)
		)
	return out


register(
	Rule(
		key="minor_hours_approaching",
		title="A worker under eighteen is at or over their weekly hour ceiling",
		category="Workforce",
		requires=("Employee", "Farm Shift", "Farm Shift Crew Member"),
		framework="ORS 653.315 / OAR 839-021-0220 (under 16); OAR 839-021-0104 (16-17)",
		purpose=(
			"A child-labour hour limit is a limit on what may be SCHEDULED, and the moment it "
			"can still be acted on is before the week ends rather than at the next inspection."
		),
		kairotic_gate=(
			"Fires on hours ALREADY WORKED this workweek, counted off the crew rows of every "
			"shift the person is on — including the one still running, which Attendance cannot "
			"see because Attendance is written at the close. Silent until the total is within "
			f"{minors.WEEKLY_WARNING_HOURS:.0f} hours of the band's ceiling, so a minor working "
			"an ordinary week raises nothing at all; Critical once it is past. Clears by itself "
			"at the start of the next workweek, because the figure it reads resets."
		),
		# Oregon BOLI enforces the wage-and-hour side and there is no packet of
		# this app's that a child-labour hour finding belongs in, so `Internal`
		# rather than a regime it does not answer to — the argument every other
		# `Internal` rule here makes.
		regimes=("Internal",),
		scan=_scan_minor_hours,
	)
)

# BUILT-IN AND NOT DECLARATIVE, and it could not be otherwise: the condition is a
# sum over child rows of another doctype, filtered by a workweek, compared with a
# ceiling that depends on the subject's age on the day. `date_field` empty with
# both thresholds at -1 is this file's way of saying "every matching row raises";
# what "matching" means lives in the scanner.
shape(
	"minor_hours_approaching",
	target_doctype="Employee",
	builtin_scanner="minor_hours_approaching",
	requires_doctypes="Employee, Farm Shift, Farm Shift Crew Member",
	requires_fields="date_of_birth",
	date_field="",
	threshold_critical_days=-1,
	threshold_warning_days=-1,
	severity_critical=SEVERITY_CRITICAL,
	severity_warning=SEVERITY_WARNING,
	severity_expired=SEVERITY_WARNING,
	default_severity=SEVERITY_WARNING,
	due_date_mode="Today",
	category="Workforce",
	retention_years=3,
	# NO TASK RECIPE, DELIBERATELY. What answers this alert is a rostering
	# decision somebody makes about tomorrow — send them home, or do not put them
	# on the crew — and a Farm Task saying "do not schedule a person" is a card
	# nobody can complete and evidence of nothing. It clears when the week turns.
)


# ── the scanner registry ────────────────────────────────────────────────────
#
# EVERY SHIPPED RULE'S SCAN IS AVAILABLE AS A NAMED SCANNER, not only the seven
# that migrate as built-ins. Two reasons, and the second is the load-bearing one:
#
#   * an operator who does not like a declarative rewrite of a rule can point
#     their row's `builtin_scanner` at the original and get the shipped
#     behaviour back, exactly, without a release;
#   * the backward-compatibility test in `test_compliance_rule_engine.py` runs
#     each declarative rule AND its legacy scanner over one fixed database and
#     asserts the two produce byte-identical alerts. That test is only possible
#     because both implementations are reachable by name from here.
SCANNERS.update({key: RULES[key].scan for key in RULES})


# v0.203.0. The rodent bait scanners. NOT registered as `Rule`s — a `Rule` is
# also the fallback set a site with no rule records runs, and these are seeded
# DISABLED until Tim switches the program on. Named here so a record may point
# at them (docs/design/rodent_bait_program.md §8).
def _rodent_scanner(name):
	def scan(context: dict) -> list:
		from .. import rodent_bait

		return getattr(rodent_bait, name)(context)

	return scan


SCANNERS["rodent_bait_check_overdue"] = _rodent_scanner("scan_check_overdue")
SCANNERS["rodent_bait_label_conformance"] = _rodent_scanner("scan_label_conformance")
